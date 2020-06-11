import datetime
import functools
import json
import logging
import os
import socket
import six
import sys
import time
import urllib3
import yaml

from urllib3 import Timeout
from urllib3.exceptions import HTTPError
from six.moves.http_client import HTTPException
from threading import Condition, Lock, Thread

from . import AbstractDCS, Cluster, ClusterConfig, Failover, Leader, Member, SyncState, TimelineHistory
from ..exceptions import DCSError
from ..utils import deep_compare, iter_response_objects, Retry, RetryFailedError, tzutc, uri, USER_AGENT

logger = logging.getLogger(__name__)

KUBE_CONFIG_DEFAULT_LOCATION = os.environ.get('KUBECONFIG', '~/.kube/config')
SERVICE_HOST_ENV_NAME = 'KUBERNETES_SERVICE_HOST'
SERVICE_PORT_ENV_NAME = 'KUBERNETES_SERVICE_PORT'
SERVICE_TOKEN_FILENAME = '/var/run/secrets/kubernetes.io/serviceaccount/token'
SERVICE_CERT_FILENAME = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'


class KubernetesError(DCSError):
    pass


# this function does the same mapping of snake_case => camelCase for > 97% of cases as autogenerated swagger code
def to_camel_case(value):
    reserved = {'api', 'apiv3', 'cidr', 'cpu', 'csi', 'id', 'io', 'ip', 'ipc', 'pid', 'tls', 'uri', 'url', 'uuid'}
    words = value.split('_')
    return words[0] + ''.join(w.upper() if w in reserved else w.title() for w in words[1:])


class K8sConfig(object):

    class ConfigException(Exception):
        pass

    def __init__(self):
        self.pool_config = {'maxsize': 10, 'num_pools': 10}  # configuration for urllib3.PoolManager
        self._make_headers()

    def _make_headers(self, token=None, **kwargs):
        self._headers = urllib3.make_headers(user_agent=USER_AGENT, **kwargs)
        if token:
            self._headers['authorization'] = 'Bearer ' + token

    def load_incluster_config(self):
        if SERVICE_HOST_ENV_NAME not in os.environ or SERVICE_PORT_ENV_NAME not in os.environ:
            raise self.ConfigException('Service host/port is not set.')
        if not os.environ[SERVICE_HOST_ENV_NAME] or not os.environ[SERVICE_PORT_ENV_NAME]:
            raise self.ConfigException('Service host/port is set but empty.')
        if not os.path.isfile(SERVICE_CERT_FILENAME):
            raise self.ConfigException('Service certificate file does not exists.')
        with open(SERVICE_CERT_FILENAME) as f:
            if not f.read():
                raise self.ConfigException('Cert file exists but empty.')
        if not os.path.isfile(SERVICE_TOKEN_FILENAME):
            raise self.ConfigException('Service token file does not exists.')
        with open(SERVICE_TOKEN_FILENAME) as f:
            token = f.read()
            if not token:
                raise self.ConfigException('Token file exists but empty.')
            self._make_headers(token=token)
        self.pool_config['ca_certs'] = SERVICE_CERT_FILENAME
        self._server = uri('https', (os.environ[SERVICE_HOST_ENV_NAME], os.environ[SERVICE_PORT_ENV_NAME]))

    @staticmethod
    def _get_by_name(config, section, name):
        for c in config[section + 's']:
            if c['name'] == name:
                return c[section]

    def load_kube_config(self, context=None):
        with open(os.path.expanduser(KUBE_CONFIG_DEFAULT_LOCATION)) as f:
            config = yaml.safe_load(f)

        context = self._get_by_name(config, 'context', context or config['current-context'])
        cluster = self._get_by_name(config, 'cluster', context['cluster'])
        user = self._get_by_name(config, 'user', context['user'])

        self._server = cluster['server'].rstrip('/')
        if self._server.startswith('https'):
            self.pool_config.update({v: user[k] for k, v in {'client-certificate': 'cert_file',
                                                             'client-key': 'key_file'}.items() if k in user})
            if 'certificate-authority' in cluster:
                self.pool_config['ca_certs'] = cluster['certificate-authority']
            self.pool_config['cert_reqs'] = 'CERT_NONE' if cluster.get('insecure-skip-tls-verify') else 'CERT_REQUIRED'
        if user.get('token'):
            self._make_headers(token=user['token'])
        elif 'username' in user and 'password' in user:
            self._headers = self._make_headers(basic_auth=':'.join((user['username'],  user['password'])))

    @property
    def server(self):
        return self._server

    @property
    def headers(self):
        return self._headers.copy()


class K8sObject(object):

    def __init__(self, kwargs):
        self._dict = {k: self._wrap(k, v) for k, v in kwargs.items()}

    def get(self, name, default=None):
        return self._dict.get(name, default)

    def __getattr__(self, name):
        return self.get(to_camel_case(name))

    @classmethod
    def _wrap(cls, parent, value):
        if isinstance(value, dict):
            # we know that `annotations` and `labels` are dicts and therefore don't want to convert them into K8sObject
            return value if parent in {'annotations', 'labels'} and \
                    all(isinstance(v, six.string_types) for v in value.values()) else cls(value)
        elif isinstance(value, list):
            return [cls._wrap(None, v) for v in value]
        else:
            return value

    def to_dict(self):
        return self._dict

    def __repr__(self):
        return json.dumps(self, indent=4, default=lambda o: o.to_dict())


class K8sClient(object):

    class rest(object):

        class ApiException(Exception):
            def __init__(self, status=None, reason=None, http_resp=None):
                self.status = http_resp.status if http_resp else status
                self.reason = http_resp.reason if http_resp else reason
                self.body = http_resp.data if http_resp else None
                self.headers = http_resp.getheaders() if http_resp else None

            def __str__(self):
                error_message = "({0})\nReason: {1}\n".format(self.status, self.reason)
                if self.headers:
                    error_message += "HTTP response headers: {0}\n".format(self.headers)
                if self.body:
                    error_message += "HTTP response body: {0}\n".format(self.body)
                return error_message

    class ApiClient(object):

        _API_URL_PREFIX = '/api/v1/namespaces/'

        def __init__(self):
            self.pool_manager = urllib3.PoolManager(**k8s_config.pool_config)
            self.set_read_timeout(10)

        def set_read_timeout(self, timeout):
            self._read_timeout = timeout

        @staticmethod
        def _handle_server_response(response, _preload_content):
            if response.status not in range(200, 206):
                raise k8s_client.rest.ApiException(http_resp=response)
            return K8sObject(json.loads(response.data.decode('utf-8'))) if _preload_content else response

        @staticmethod
        def _make_headers(headers):
            ret = k8s_config.headers
            ret.update(headers or {})
            return ret

        def request(self, method, path, timeout=None, **kwargs):
            retries = 0 if timeout else 1
            if timeout:
                if isinstance(timeout, six.integer_types + (float,)):
                    timeout = urllib3.Timeout(total=timeout)
                elif isinstance(timeout, tuple) and len(timeout) == 2:
                    timeout = urllib3.Timeout(connect=timeout[0], read=timeout[1])
            else:
                timeout = self._read_timeout / 2.0
                timeout = urllib3.Timeout(connect=max(1, timeout/2.0), total=timeout)
            kwargs.update(retries=retries, timeout=timeout)
            return self.pool_manager.request(method, k8s_config.server + path, **kwargs)

        def call_api(self, method, path, headers=None, body=None,
                     _preload_content=True, _request_timeout=None, **kwargs):
            headers = self._make_headers(headers)
            fields = {to_camel_case(k): v for k, v in kwargs.items()}  # resource_version => resourceVersion
            body = json.dumps(body, default=lambda o: o.to_dict()) if body is not None else None

            response = self.request(method, self._API_URL_PREFIX + path, headers=headers, fields=fields,
                                    body=body, preload_content=_preload_content, timeout=_request_timeout)

            return self._handle_server_response(response, _preload_content)

    class CoreV1Api(object):

        def __init__(self, api_client=None):
            self._api_client = api_client or k8s_client.ApiClient()

        def __getattr__(self, func):  # `func` name pattern: (action)_namespaced_(kind)
            action, kind = func.split('_namespaced_')  # (read|list|create|patch|replace|delete|delete_collection)
            kind = kind.replace('_', '') + ('s' * int(kind[-1] != 's'))  # plural, single word

            def wrapper(*args, **kwargs):
                method = {'read': 'GET', 'list': 'GET', 'create': 'POST',
                          'replace': 'PUT'}.get(action, action.split('_')[0]).upper()

                if action == 'create' or len(args) == 1:  # namespace is a first argument and name in not in arguments
                    path = '/'.join([args[0], kind])
                else:  # name, namespace followed by optional body
                    path = '/'.join([args[1], kind, args[0]])

                headers = {'Content-Type': 'application/strategic-merge-patch+json'} if action == 'patch' else {}

                if len(args) == 3:  # name, namespace, body
                    body = args[2]
                elif action == 'create':  # namespace, body
                    body = args[1]
                elif action == 'delete':  # name, namespace
                    body = kwargs.pop('body', None)
                else:
                    body = None

                return self._api_client.call_api(method, path, headers, body, **kwargs)
            return wrapper

    class _K8sObjectTemplate(K8sObject):
        """The template for objects which we create locally, e.g. k8s_client.V1ObjectMeta & co"""
        def __init__(self, **kwargs):
            self._dict = {to_camel_case(k): v for k, v in kwargs.items()}

    def __init__(self):
        self.__cls_cache = {}
        self.__cls_lock = Lock()

    def __getattr__(self, name):
        with self.__cls_lock:
            if name not in self.__cls_cache:
                self.__cls_cache[name] = type(name, (self._K8sObjectTemplate,), {})
        return self.__cls_cache[name]


k8s_client = K8sClient()
k8s_config = K8sConfig()


class KubernetesRetriableException(k8s_client.rest.ApiException):

    def __init__(self, orig):
        super(KubernetesRetriableException, self).__init__(orig.status, orig.reason)
        self.body = orig.body
        self.headers = orig.headers

    @property
    def sleeptime(self):
        try:
            return int(self.headers['retry-after'])
        except Exception:
            return None


class CoreV1ApiProxy(object):

    def __init__(self, use_endpoints=False):
        self._api_client = k8s_client.ApiClient()
        self._core_v1_api = k8s_client.CoreV1Api(self._api_client)
        self._use_endpoints = use_endpoints

    def configure_timeouts(self, loop_wait, retry_timeout, ttl):
        # Normally every loop_wait seconds we should have receive something from the socket.
        # If we didn't received anything after the loop_wait + retry_timeout it is a time
        # to start worrying (send keepalive messages). Finally, the connection should be
        # considered as dead if we received nothing from the socket after the ttl seconds.
        cnt = 3
        idle = int(loop_wait + retry_timeout)
        intvl = max(1, int(float(ttl - idle) / cnt))
        self._api_client.pool_manager.connection_pool_kw['socket_options'] = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
            (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle),
            (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, intvl),
            (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, cnt),
            (socket.IPPROTO_TCP, 18, int(ttl * 1000))  # TCP_USER_TIMEOUT
        ]
        self._api_client.set_read_timeout(retry_timeout)

    def __getattr__(self, func):
        if func.endswith('_kind'):
            func = func[:-4] + ('endpoints' if self._use_endpoints else 'config_map')

        def wrapper(*args, **kwargs):
            try:
                return getattr(self._core_v1_api, func)(*args, **kwargs)
            except k8s_client.rest.ApiException as e:
                if e.status in (502, 503, 504) or e.headers and 'retry-after' in e.headers:  # XXX
                    raise KubernetesRetriableException(e)
                raise
        return wrapper


def catch_kubernetes_errors(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except k8s_client.rest.ApiException as e:
            if e.status == 403:
                logger.exception('Permission denied')
            elif e.status != 409:  # Object exists or conflict in resource_version
                logger.exception('Unexpected error from Kubernetes API')
            return False
        except (RetryFailedError, HTTPException, HTTPError, socket.error, socket.timeout):
            return False
    return wrapper


class ObjectCache(Thread):

    def __init__(self, dcs, func, retry, condition):
        Thread.__init__(self)
        self.daemon = True
        self._dcs = dcs
        self._func = func
        self._retry = retry
        self._condition = condition
        self._is_ready = False
        self._object_cache = {}
        self._object_cache_lock = Lock()
        self._annotations_map = {self._dcs.leader_path: self._dcs._LEADER, self._dcs.config_path: self._dcs._CONFIG}
        self.start()

    def _list(self):
        try:
            return self._func(_request_timeout=(self._retry.deadline, Timeout.DEFAULT_TIMEOUT))
        except Exception:
            time.sleep(1)
            raise

    def _watch(self, resource_version):
        return self._func(_request_timeout=(self._retry.deadline, Timeout.DEFAULT_TIMEOUT),
                          _preload_content=False, watch=True, resource_version=resource_version)

    def set(self, name, value):
        with self._object_cache_lock:
            old_value = self._object_cache.get(name)
            ret = not old_value or int(old_value.metadata.resource_version) < int(value.metadata.resource_version)
            if ret:
                self._object_cache[name] = value
        return ret, old_value

    def delete(self, name, resource_version):
        with self._object_cache_lock:
            old_value = self._object_cache.get(name)
            ret = old_value and int(old_value.metadata.resource_version) < int(resource_version)
            if ret:
                del self._object_cache[name]
        return not old_value or ret, old_value

    def copy(self):
        with self._object_cache_lock:
            return self._object_cache.copy()

    def _build_cache(self):
        objects = self._list()
        return_type = 'V1' + objects.kind[:-4]
        with self._object_cache_lock:
            self._object_cache = {item.metadata.name: item for item in objects.items}
        with self._condition:
            self._is_ready = True
            self._condition.notify()

        response = self._watch(objects.metadata.resource_version)
        try:
            for event in iter_response_objects(response):
                obj = event['object']
                if obj.get('code') == 410:
                    break

                ev_type = event['type']
                name = obj['metadata']['name']

                if ev_type in ('ADDED', 'MODIFIED'):
                    obj = K8sObject(obj)
                    success, old_value = self.set(name, obj)
                    if success:
                        new_value = (obj.metadata.annotations or {}).get(self._annotations_map.get(name))
                elif ev_type == 'DELETED':
                    success, old_value = self.delete(name, obj['metadata']['resourceVersion'])
                    new_value = None
                else:
                    logger.warning('Unexpected event type: %s', ev_type)
                    continue

                if success and return_type != 'V1Pod':
                    if old_value:
                        old_value = (old_value.metadata.annotations or {}).get(self._annotations_map.get(name))

                    if old_value != new_value and \
                            (name != self._dcs.config_path or old_value is not None and new_value is not None):
                        logger.debug('%s changed from %s to %s', name, old_value, new_value)
                        self._dcs.event.set()
        finally:
            with self._condition:
                self._is_ready = False
            response.close()
            response.release_conn()

    def run(self):
        while True:
            try:
                self._build_cache()
            except Exception as e:
                with self._condition:
                    self._is_ready = False
                logger.error('ObjectCache.run %r', e)

    def is_ready(self):
        """Must be called only when holding the lock on `_condition`"""
        return self._is_ready


class Kubernetes(AbstractDCS):

    def __init__(self, config):
        self._labels = config['labels']
        self._labels[config.get('scope_label', 'cluster-name')] = config['scope']
        self._label_selector = ','.join('{0}={1}'.format(k, v) for k, v in self._labels.items())
        self._namespace = config.get('namespace') or 'default'
        self._role_label = config.get('role_label', 'role')
        config['namespace'] = ''
        super(Kubernetes, self).__init__(config)
        self._retry = Retry(deadline=config['retry_timeout'], max_delay=1, max_tries=-1,
                            retry_exceptions=(KubernetesRetriableException, HTTPException,
                                              HTTPError, socket.error, socket.timeout))
        self._ttl = None
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config(context=config.get('context', 'local'))

        self.__subsets = None
        use_endpoints = config.get('use_endpoints') and (config.get('patronictl') or 'pod_ip' in config)
        if use_endpoints:
            addresses = [k8s_client.V1EndpointAddress(ip='127.0.0.1' if config.get('patronictl') else config['pod_ip'])]
            ports = []
            for p in config.get('ports', [{}]):
                port = {'port': int(p.get('port', '5432'))}
                port.update({n: p[n] for n in ('name', 'protocol') if p.get(n)})
                ports.append(k8s_client.V1EndpointPort(**port))
            self.__subsets = [k8s_client.V1EndpointSubset(addresses=addresses, ports=ports)]
            self._should_create_config_service = True
        self._api = CoreV1ApiProxy(use_endpoints)
        self.reload_config(config)
        self._leader_observed_record = {}
        self._leader_observed_time = None
        self._leader_resource_version = None
        self._leader_observed_subsets = []
        self._config_resource_version = None
        self.__do_not_watch = False

        self._condition = Condition()

        pods_func = functools.partial(self._api.list_namespaced_pod, self._namespace,
                                      label_selector=self._label_selector)
        self._pods = ObjectCache(self, pods_func, self._retry, self._condition)

        kinds_func = functools.partial(self._api.list_namespaced_kind, self._namespace,
                                       label_selector=self._label_selector)
        self._kinds = ObjectCache(self, kinds_func, self._retry, self._condition)

    def retry(self, *args, **kwargs):
        return self._retry.copy()(*args, **kwargs)

    def client_path(self, path):
        return super(Kubernetes, self).client_path(path)[1:].replace('/', '-')

    @property
    def leader_path(self):
        return self._base_path[1:] if self.__subsets else super(Kubernetes, self).leader_path

    def set_ttl(self, ttl):
        ttl = int(ttl)
        self.__do_not_watch = self._ttl != ttl
        self._ttl = ttl

    @property
    def ttl(self):
        return self._ttl

    def set_retry_timeout(self, retry_timeout):
        self._retry.deadline = retry_timeout

    def reload_config(self, config):
        super(Kubernetes, self).reload_config(config)
        self._api.configure_timeouts(self.loop_wait, self._retry.deadline, self.ttl)

    @staticmethod
    def member(pod):
        annotations = pod.metadata.annotations or {}
        member = Member.from_node(pod.metadata.resource_version, pod.metadata.name, None, annotations.get('status', ''))
        member.data['pod_labels'] = pod.metadata.labels
        return member

    def _wait_caches(self):
        stop_time = time.time() + self._retry.deadline
        while not (self._pods.is_ready() and self._kinds.is_ready()):
            timeout = stop_time - time.time()
            if timeout <= 0:
                raise RetryFailedError('Exceeded retry deadline')
            self._condition.wait(timeout)

    def _load_cluster(self):
        try:
            with self._condition:
                self._wait_caches()

                members = [self.member(pod) for pod in self._pods.copy().values()]
                nodes = self._kinds.copy()

            config = nodes.get(self.config_path)
            metadata = config and config.metadata
            self._config_resource_version = metadata.resource_version if metadata else None
            annotations = metadata and metadata.annotations or {}

            # get initialize flag
            initialize = annotations.get(self._INITIALIZE)

            # get global dynamic configuration
            config = ClusterConfig.from_node(metadata and metadata.resource_version,
                                             annotations.get(self._CONFIG) or '{}',
                                             metadata.resource_version if self._CONFIG in annotations else 0)

            # get timeline history
            history = TimelineHistory.from_node(metadata and metadata.resource_version,
                                                annotations.get(self._HISTORY) or '[]')

            leader = nodes.get(self.leader_path)
            metadata = leader and leader.metadata
            self._leader_resource_version = metadata.resource_version if metadata else None
            self._leader_observed_subsets = leader.subsets if self.__subsets and leader and leader.subsets else []
            annotations = metadata and metadata.annotations or {}

            # get last leader operation
            last_leader_operation = annotations.get(self._OPTIME)
            last_leader_operation = 0 if last_leader_operation is None else int(last_leader_operation)

            # get leader
            leader_record = {n: annotations.get(n) for n in (self._LEADER, 'acquireTime',
                             'ttl', 'renewTime', 'transitions') if n in annotations}
            if (leader_record or self._leader_observed_record) and leader_record != self._leader_observed_record:
                self._leader_observed_record = leader_record
                self._leader_observed_time = time.time()

            leader = leader_record.get(self._LEADER)
            try:
                ttl = int(leader_record.get('ttl')) or self._ttl
            except (TypeError, ValueError):
                ttl = self._ttl

            if not metadata or not self._leader_observed_time or self._leader_observed_time + ttl < time.time():
                leader = None

            if metadata:
                member = Member(-1, leader, None, {})
                member = ([m for m in members if m.name == leader] or [member])[0]
                leader = Leader(metadata.resource_version, None, member)

            # failover key
            failover = nodes.get(self.failover_path)
            metadata = failover and failover.metadata
            failover = Failover.from_node(metadata and metadata.resource_version,
                                          metadata and (metadata.annotations or {}).copy())

            # get synchronization state
            sync = nodes.get(self.sync_path)
            metadata = sync and sync.metadata
            sync = SyncState.from_node(metadata and metadata.resource_version,  metadata and metadata.annotations)

            return Cluster(initialize, config, leader, last_leader_operation, members, failover, sync, history)
        except Exception:
            logger.exception('get_cluster')
            raise KubernetesError('Kubernetes API is not responding properly')

    @staticmethod
    def compare_ports(p1, p2):
        return p1.name == p2.name and p1.port == p2.port and (p1.protocol or 'TCP') == (p2.protocol or 'TCP')

    @staticmethod
    def subsets_changed(last_observed_subsets, subsets):
        """
        >>> Kubernetes.subsets_changed([], [])
        False
        >>> Kubernetes.subsets_changed([], [k8s_client.V1EndpointSubset()])
        True
        >>> s1 = [k8s_client.V1EndpointSubset(addresses=[k8s_client.V1EndpointAddress(ip='1.2.3.4')])]
        >>> s2 = [k8s_client.V1EndpointSubset(addresses=[k8s_client.V1EndpointAddress(ip='1.2.3.5')])]
        >>> Kubernetes.subsets_changed(s1, s2)
        True
        >>> a = [k8s_client.V1EndpointAddress(ip='1.2.3.4')]
        >>> s1 = [k8s_client.V1EndpointSubset(addresses=a, ports=[k8s_client.V1EndpointPort(protocol='TCP', port=1)])]
        >>> s2 = [k8s_client.V1EndpointSubset(addresses=a, ports=[k8s_client.V1EndpointPort(port=5432)])]
        >>> Kubernetes.subsets_changed(s1, s2)
        True
        >>> p1 = k8s_client.V1EndpointPort(name='port1', port=1)
        >>> p2 = k8s_client.V1EndpointPort(name='port2', port=2)
        >>> p3 = k8s_client.V1EndpointPort(name='port3', port=3)
        >>> s1 = [k8s_client.V1EndpointSubset(addresses=a, ports=[p1, p2])]
        >>> s2 = [k8s_client.V1EndpointSubset(addresses=a, ports=[p2, p3])]
        >>> Kubernetes.subsets_changed(s1, s2)
        True
        >>> s2 = [k8s_client.V1EndpointSubset(addresses=a, ports=[p2, p1])]
        >>> Kubernetes.subsets_changed(s1, s2)
        False
        """
        if len(last_observed_subsets) != len(subsets):
            return True
        if subsets == []:
            return False
        if len(last_observed_subsets[0].addresses or []) != 1 or \
                last_observed_subsets[0].addresses[0].ip != subsets[0].addresses[0].ip or \
                len(last_observed_subsets[0].ports) != len(subsets[0].ports):
            return True
        if len(subsets[0].ports) == 1:
            return not Kubernetes.compare_ports(last_observed_subsets[0].ports[0], subsets[0].ports[0])
        observed_ports = {p.name: p for p in last_observed_subsets[0].ports}
        for p in subsets[0].ports:
            if p.name not in observed_ports or not Kubernetes.compare_ports(p, observed_ports.pop(p.name)):
                return True
        return False

    @catch_kubernetes_errors
    def patch_or_create(self, name, annotations, resource_version=None, patch=False, retry=True, subsets=None):
        metadata = {'namespace': self._namespace, 'name': name, 'labels': self._labels, 'annotations': annotations}
        if patch or resource_version:
            if resource_version is not None:
                metadata['resource_version'] = resource_version
            func = functools.partial(self._api.patch_namespaced_kind, name)
        else:
            func = functools.partial(self._api.create_namespaced_kind)
            # skip annotations with null values
            metadata['annotations'] = {k: v for k, v in metadata['annotations'].items() if v is not None}

        metadata = k8s_client.V1ObjectMeta(**metadata)
        if subsets is not None and self.__subsets:
            endpoints = {'metadata': metadata}
            if self.subsets_changed(self._leader_observed_subsets, subsets):
                endpoints['subsets'] = subsets
            body = k8s_client.V1Endpoints(**endpoints)
        else:
            body = k8s_client.V1ConfigMap(metadata=metadata)
        ret = self.retry(func, self._namespace, body) if retry else func(self._namespace, body)
        if ret:
            self._kinds.set(name, ret)
        return ret

    def patch_or_create_config(self, annotations, resource_version=None, patch=False, retry=True):
        # SCOPE-config endpoint requires corresponding service otherwise it might be "cleaned" by k8s master
        if self.__subsets and not patch and not resource_version:
            self._should_create_config_service = True
            self._create_config_service()
        ret = self.patch_or_create(self.config_path, annotations, resource_version, patch, retry)
        if ret:
            self._config_resource_version = ret.metadata.resource_version
        return ret

    def _create_config_service(self):
        metadata = k8s_client.V1ObjectMeta(namespace=self._namespace, name=self.config_path, labels=self._labels)
        body = k8s_client.V1Service(metadata=metadata, spec=k8s_client.V1ServiceSpec(cluster_ip='None'))
        try:
            if not self._api.create_namespaced_service(self._namespace, body):
                return
        except Exception as e:
            if not isinstance(e, k8s_client.rest.ApiException) or e.status != 409:  # Service already exists
                return logger.exception('create_config_service failed')
        self._should_create_config_service = False

    def _write_leader_optime(self, last_operation):
        """Unused"""

    def _update_leader(self):
        """Unused"""

    def update_leader(self, last_operation, access_is_restricted=False):
        now = datetime.datetime.now(tzutc).isoformat()
        annotations = {self._LEADER: self._name, 'ttl': str(self._ttl), 'renewTime': now,
                       'acquireTime': self._leader_observed_record.get('acquireTime') or now,
                       'transitions': self._leader_observed_record.get('transitions') or '0'}
        if last_operation:
            annotations[self._OPTIME] = last_operation

        subsets = [] if access_is_restricted else self.__subsets

        ret = self.patch_or_create(self.leader_path, annotations, self._leader_resource_version, subsets=subsets)
        if ret:
            self._leader_resource_version = ret.metadata.resource_version
        return ret

    def attempt_to_acquire_leader(self, permanent=False):
        now = datetime.datetime.now(tzutc).isoformat()
        annotations = {self._LEADER: self._name, 'ttl': str(sys.maxsize if permanent else self._ttl),
                       'renewTime': now, 'acquireTime': now, 'transitions': '0'}
        if self._leader_observed_record:
            try:
                transitions = int(self._leader_observed_record.get('transitions'))
            except (TypeError, ValueError):
                transitions = 0

            if self._leader_observed_record.get(self._LEADER) != self._name:
                transitions += 1
            else:
                annotations['acquireTime'] = self._leader_observed_record.get('acquireTime') or now
            annotations['transitions'] = str(transitions)
        subsets = [] if self.__subsets else None
        ret = self.patch_or_create(self.leader_path, annotations, self._leader_resource_version, subsets=subsets)
        if ret:
            self._leader_resource_version = ret.metadata.resource_version
        else:
            logger.info('Could not take out TTL lock')
        return ret

    def take_leader(self):
        return self.attempt_to_acquire_leader()

    def set_failover_value(self, value, index=None):
        """Unused"""

    def manual_failover(self, leader, candidate, scheduled_at=None, index=None):
        annotations = {'leader': leader or None, 'member': candidate or None,
                       'scheduled_at': scheduled_at and scheduled_at.isoformat()}
        patch = bool(self.cluster and isinstance(self.cluster.failover, Failover) and self.cluster.failover.index)
        return self.patch_or_create(self.failover_path, annotations, index, bool(index or patch), False)

    def set_config_value(self, value, index=None):
        return self.patch_or_create_config({self._CONFIG: value}, index, bool(self._config_resource_version), False)

    @catch_kubernetes_errors
    def touch_member(self, data, permanent=False):
        cluster = self.cluster
        if cluster and cluster.leader and cluster.leader.name == self._name:
            role = 'promoted' if data['role'] in ('replica', 'promoted') else 'master'
        elif data['state'] == 'running' and data['role'] != 'master':
            role = data['role']
        else:
            role = None

        member = cluster and cluster.get_member(self._name, fallback_to_leader=False)
        pod_labels = member and member.data.pop('pod_labels', None)
        ret = pod_labels is not None and pod_labels.get(self._role_label) == role and deep_compare(data, member.data)

        if not ret:
            metadata = {'namespace': self._namespace, 'name': self._name, 'labels': {self._role_label: role},
                        'annotations': {'status': json.dumps(data, separators=(',', ':'))}}
            body = k8s_client.V1Pod(metadata=k8s_client.V1ObjectMeta(**metadata))
            ret = self._api.patch_namespaced_pod(self._name, self._namespace, body)
        if self.__subsets and self._should_create_config_service:
            self._create_config_service()
        return ret

    def initialize(self, create_new=True, sysid=""):
        cluster = self.cluster
        resource_version = cluster.config.index if cluster and cluster.config and cluster.config.index else None
        return self.patch_or_create_config({self._INITIALIZE: sysid}, resource_version)

    def delete_leader(self):
        if self.cluster and isinstance(self.cluster.leader, Leader) and self.cluster.leader.name == self._name:
            self.patch_or_create(self.leader_path, {self._LEADER: None}, self._leader_resource_version, True, False, [])
            self.reset_cluster()

    def cancel_initialization(self):
        self.patch_or_create_config({self._INITIALIZE: None}, self._config_resource_version, True)

    @catch_kubernetes_errors
    def delete_cluster(self):
        self.retry(self._api.delete_collection_namespaced_kind, self._namespace, label_selector=self._label_selector)

    def set_history_value(self, value):
        return self.patch_or_create_config({self._HISTORY: value}, None, bool(self._config_resource_version), False)

    def set_sync_state_value(self, value, index=None):
        """Unused"""

    def write_sync_state(self, leader, sync_standby, index=None):
        return self.patch_or_create(self.sync_path, self.sync_state(leader, sync_standby), index, False)

    def delete_sync_state(self, index=None):
        return self.write_sync_state(None, None, index)

    def watch(self, leader_index, timeout):
        if self.__do_not_watch:
            self.__do_not_watch = False
            return True

        try:
            return super(Kubernetes, self).watch(None, timeout)
        finally:
            self.event.clear()
