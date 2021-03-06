import json
import errno
import logging
import time
from copy import copy
from threading import Event
from functools import wraps

from mgr_util import create_self_signed_cert, verify_tls, ServerConfigException

import string
try:
    from typing import List, Dict, Optional, Callable, Tuple, TypeVar, Type, \
        Any, NamedTuple, Iterator, Set, Sequence
    from typing import TYPE_CHECKING, cast
except ImportError:
    TYPE_CHECKING = False  # just for type checking


import datetime
import six
import os
import random
import tempfile
import multiprocessing.pool
import re
import shutil
import subprocess
import uuid

from ceph.deployment import inventory, translate
from ceph.deployment.drive_group import DriveGroupSpec
from ceph.deployment.drive_selection.selector import DriveSelection
from ceph.deployment.service_spec import \
    HostPlacementSpec, NFSServiceSpec, ServiceSpec, PlacementSpec, assert_valid_host

from mgr_module import MgrModule, HandleCommandResult, MonCommandFailed
import orchestrator
from orchestrator import OrchestratorError, OrchestratorValidationError, HostSpec, \
    CLICommandMeta

from . import remotes
from . import utils
from .nfs import NFSGanesha
from .osd import RemoveUtil, OSDRemoval
from .inventory import Inventory

try:
    import remoto
    import remoto.process
    import execnet.gateway_bootstrap
except ImportError as e:
    remoto = None
    remoto_import_error = str(e)

try:
    from typing import List
except ImportError:
    pass

logger = logging.getLogger(__name__)

DEFAULT_SSH_CONFIG = """
Host *
  User root
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  ConnectTimeout=30
"""

DATEFMT = '%Y-%m-%dT%H:%M:%S.%f'
CEPH_DATEFMT = '%Y-%m-%dT%H:%M:%S.%fZ'

HOST_CACHE_PREFIX = "host."
SPEC_STORE_PREFIX = "spec."

# ceph daemon types that use the ceph container image.
# NOTE: listed in upgrade order!
CEPH_UPGRADE_ORDER = ['mgr', 'mon', 'crash', 'osd', 'mds', 'rgw', 'rbd-mirror']
CEPH_TYPES = set(CEPH_UPGRADE_ORDER)


# for py2 compat
try:
    from tempfile import TemporaryDirectory # py3
except ImportError:
    # define a minimal (but sufficient) equivalent for <= py 3.2
    class TemporaryDirectory(object): # type: ignore
        def __init__(self):
            self.name = tempfile.mkdtemp()

        def __enter__(self):
            if not self.name:
                self.name = tempfile.mkdtemp()
            return self.name

        def cleanup(self):
            shutil.rmtree(self.name)

        def __exit__(self, exc_type, exc_value, traceback):
            self.cleanup()


class SpecStore():
    def __init__(self, mgr):
        # type: (CephadmOrchestrator) -> None
        self.mgr = mgr
        self.specs = {} # type: Dict[str, ServiceSpec]
        self.spec_created = {} # type: Dict[str, datetime.datetime]

    def load(self):
        # type: () -> None
        for k, v in six.iteritems(self.mgr.get_store_prefix(SPEC_STORE_PREFIX)):
            service_name = k[len(SPEC_STORE_PREFIX):]
            try:
                v = json.loads(v)
                spec = ServiceSpec.from_json(v['spec'])
                created = datetime.datetime.strptime(v['created'], DATEFMT)
                self.specs[service_name] = spec
                self.spec_created[service_name] = created
                self.mgr.log.debug('SpecStore: loaded spec for %s' % (
                    service_name))
            except Exception as e:
                self.mgr.log.warning('unable to load spec for %s: %s' % (
                    service_name, e))
                pass

    def save(self, spec):
        # type: (ServiceSpec) -> None
        self.specs[spec.service_name()] = spec
        self.spec_created[spec.service_name()] = datetime.datetime.utcnow()
        self.mgr.set_store(
            SPEC_STORE_PREFIX + spec.service_name(),
            json.dumps({
                'spec': spec.to_json(),
                'created': self.spec_created[spec.service_name()].strftime(DATEFMT),
            }, sort_keys=True),
        )

    def rm(self, service_name):
        # type: (str) -> bool
        found = service_name in self.specs
        if found:
            del self.specs[service_name]
            del self.spec_created[service_name]
            self.mgr.set_store(SPEC_STORE_PREFIX + service_name, None)
        return found

    def find(self, service_name: Optional[str] = None) -> List[ServiceSpec]:
        specs = []
        for sn, spec in self.specs.items():
            if not service_name or \
                    sn == service_name or \
                    sn.startswith(service_name + '.'):
                specs.append(spec)
        self.mgr.log.debug('SpecStore: find spec for %s returned: %s' % (
            service_name, specs))
        return specs

class HostCache():
    def __init__(self, mgr):
        # type: (CephadmOrchestrator) -> None
        self.mgr: CephadmOrchestrator = mgr
        self.daemons = {}   # type: Dict[str, Dict[str, orchestrator.DaemonDescription]]
        self.last_daemon_update = {}   # type: Dict[str, datetime.datetime]
        self.devices = {}              # type: Dict[str, List[inventory.Device]]
        self.networks = {}             # type: Dict[str, Dict[str, List[str]]]
        self.last_device_update = {}   # type: Dict[str, datetime.datetime]
        self.daemon_refresh_queue = [] # type: List[str]
        self.device_refresh_queue = [] # type: List[str]
        self.daemon_config_deps = {}   # type: Dict[str, Dict[str, Dict[str,Any]]]
        self.last_host_check = {}      # type: Dict[str, datetime.datetime]

    def load(self):
        # type: () -> None
        for k, v in six.iteritems(self.mgr.get_store_prefix(HOST_CACHE_PREFIX)):
            host = k[len(HOST_CACHE_PREFIX):]
            if host not in self.mgr.inventory:
                self.mgr.log.warning('removing stray HostCache host record %s' % (
                    host))
                self.mgr.set_store(k, None)
            try:
                j = json.loads(v)
                if 'last_device_update' in j:
                    self.last_device_update[host] = datetime.datetime.strptime(
                        j['last_device_update'], DATEFMT)
                else:
                    self.device_refresh_queue.append(host)
                # for services, we ignore the persisted last_*_update
                # and always trigger a new scrape on mgr restart.
                self.daemon_refresh_queue.append(host)
                self.daemons[host] = {}
                self.devices[host] = []
                self.networks[host] = {}
                self.daemon_config_deps[host] = {}
                for name, d in j.get('daemons', {}).items():
                    self.daemons[host][name] = \
                        orchestrator.DaemonDescription.from_json(d)
                for d in j.get('devices', []):
                    self.devices[host].append(inventory.Device.from_json(d))
                self.networks[host] = j.get('networks', {})
                for name, d in j.get('daemon_config_deps', {}).items():
                    self.daemon_config_deps[host][name] = {
                        'deps': d.get('deps', []),
                        'last_config': datetime.datetime.strptime(
                            d['last_config'], DATEFMT),
                    }
                if 'last_host_check' in j:
                    self.last_host_check[host] = datetime.datetime.strptime(
                        j['last_host_check'], DATEFMT)
                self.mgr.log.debug(
                    'HostCache.load: host %s has %d daemons, '
                    '%d devices, %d networks' % (
                        host, len(self.daemons[host]), len(self.devices[host]),
                        len(self.networks[host])))
            except Exception as e:
                self.mgr.log.warning('unable to load cached state for %s: %s' % (
                    host, e))
                pass

    def update_host_daemons(self, host, dm):
        # type: (str, Dict[str, orchestrator.DaemonDescription]) -> None
        self.daemons[host] = dm
        self.last_daemon_update[host] = datetime.datetime.utcnow()

    def update_host_devices_networks(self, host, dls, nets):
        # type: (str, List[inventory.Device], Dict[str,List[str]]) -> None
        self.devices[host] = dls
        self.networks[host] = nets
        self.last_device_update[host] = datetime.datetime.utcnow()

    def update_daemon_config_deps(self, host, name, deps, stamp):
        self.daemon_config_deps[host][name] = {
            'deps': deps,
            'last_config': stamp,
        }

    def update_last_host_check(self, host):
        # type: (str) -> None
        self.last_host_check[host] = datetime.datetime.utcnow()

    def prime_empty_host(self, host):
        # type: (str) -> None
        """
        Install an empty entry for a host
        """
        self.daemons[host] = {}
        self.devices[host] = []
        self.networks[host] = {}
        self.daemon_config_deps[host] = {}
        self.daemon_refresh_queue.append(host)
        self.device_refresh_queue.append(host)

    def invalidate_host_daemons(self, host):
        # type: (str) -> None
        self.daemon_refresh_queue.append(host)
        if host in self.last_daemon_update:
            del self.last_daemon_update[host]
        self.mgr.event.set()

    def invalidate_host_devices(self, host):
        # type: (str) -> None
        self.device_refresh_queue.append(host)
        if host in self.last_device_update:
            del self.last_device_update[host]
        self.mgr.event.set()

    def save_host(self, host):
        # type: (str) -> None
        j = {   # type: ignore
            'daemons': {},
            'devices': [],
            'daemon_config_deps': {},
        }
        if host in self.last_daemon_update:
            j['last_daemon_update'] = self.last_daemon_update[host].strftime(DATEFMT) # type: ignore
        if host in self.last_device_update:
            j['last_device_update'] = self.last_device_update[host].strftime(DATEFMT) # type: ignore
        for name, dd in self.daemons[host].items():
            j['daemons'][name] = dd.to_json()  # type: ignore
        for d in self.devices[host]:
            j['devices'].append(d.to_json())  # type: ignore
        j['networks'] = self.networks[host]
        for name, depi in self.daemon_config_deps[host].items():
            j['daemon_config_deps'][name] = {   # type: ignore
                'deps': depi.get('deps', []),
                'last_config': depi['last_config'].strftime(DATEFMT),
            }
        if host in self.last_host_check:
            j['last_host_check']= self.last_host_check[host].strftime(DATEFMT)
        self.mgr.set_store(HOST_CACHE_PREFIX + host, json.dumps(j))

    def rm_host(self, host):
        # type: (str) -> None
        if host in self.daemons:
            del self.daemons[host]
        if host in self.devices:
            del self.devices[host]
        if host in self.networks:
            del self.networks[host]
        if host in self.last_daemon_update:
            del self.last_daemon_update[host]
        if host in self.last_device_update:
            del self.last_device_update[host]
        if host in self.daemon_config_deps:
            del self.daemon_config_deps[host]
        self.mgr.set_store(HOST_CACHE_PREFIX + host, None)

    def get_hosts(self):
        # type: () -> List[str]
        r = []
        for host, di in self.daemons.items():
            r.append(host)
        return r

    def get_daemons(self):
        # type: () -> List[orchestrator.DaemonDescription]
        r = []
        for host, dm in self.daemons.items():
            for name, dd in dm.items():
                r.append(dd)
        return r

    def get_daemons_with_volatile_status(self) -> Iterator[Tuple[str, Dict[str, orchestrator.DaemonDescription]]]:
        for host, dm in self.daemons.items():
            if host in self.mgr.offline_hosts:
                def set_offline(dd: orchestrator.DaemonDescription) -> orchestrator.DaemonDescription:
                    ret = copy(dd)
                    ret.status = -1
                    ret.status_desc = 'host is offline'
                    return ret
                yield host, {name: set_offline(d) for name, d in dm.items()}
            else:
                yield host, dm

    def get_daemons_by_service(self, service_name):
        # type: (str) -> List[orchestrator.DaemonDescription]
        result = []   # type: List[orchestrator.DaemonDescription]
        for host, dm in self.daemons.items():
            for name, d in dm.items():
                if name.startswith(service_name + '.'):
                    result.append(d)
        return result

    def get_daemon_names(self):
        # type: () -> List[str]
        r = []
        for host, dm in self.daemons.items():
            for name, dd in dm.items():
                r.append(name)
        return r

    def get_daemon_last_config_deps(self, host, name):
        if host in self.daemon_config_deps:
            if name in self.daemon_config_deps[host]:
                return self.daemon_config_deps[host][name].get('deps', []), \
                    self.daemon_config_deps[host][name].get('last_config', None)
        return None, None

    def host_needs_daemon_refresh(self, host):
        # type: (str) -> bool
        if host in self.mgr.offline_hosts:
            logger.debug(f'Host "{host}" marked as offline. Skipping daemon refresh')
            return False
        if host in self.daemon_refresh_queue:
            self.daemon_refresh_queue.remove(host)
            return True
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=self.mgr.daemon_cache_timeout)
        if host not in self.last_daemon_update or self.last_daemon_update[host] < cutoff:
            return True
        return False

    def host_needs_device_refresh(self, host):
        # type: (str) -> bool
        if host in self.mgr.offline_hosts:
            logger.debug(f'Host "{host}" marked as offline. Skipping device refresh')
            return False
        if host in self.device_refresh_queue:
            self.device_refresh_queue.remove(host)
            return True
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=self.mgr.device_cache_timeout)
        if host not in self.last_device_update or self.last_device_update[host] < cutoff:
            return True
        return False

    def host_needs_check(self, host):
        # type: (str) -> bool
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=self.mgr.host_check_interval)
        return host not in self.last_host_check or self.last_host_check[host] < cutoff

    def add_daemon(self, host, dd):
        # type: (str, orchestrator.DaemonDescription) -> None
        assert host in self.daemons
        self.daemons[host][dd.name()] = dd

    def rm_daemon(self, host, name):
        if host in self.daemons:
            if name in self.daemons[host]:
                del self.daemons[host][name]


class AsyncCompletion(orchestrator.Completion):
    def __init__(self,
                 _first_promise=None,  # type: Optional[orchestrator.Completion]
                 value=orchestrator._Promise.NO_RESULT,  # type: Any
                 on_complete=None,  # type: Optional[Callable]
                 name=None,  # type: Optional[str]
                 many=False, # type: bool
                 update_progress=False,  # type: bool
                 ):

        assert CephadmOrchestrator.instance is not None
        self.many = many
        self.update_progress = update_progress
        if name is None and on_complete is not None:
            name = getattr(on_complete, '__name__', None)
        super(AsyncCompletion, self).__init__(_first_promise, value, on_complete, name)

    @property
    def _progress_reference(self):
        # type: () -> Optional[orchestrator.ProgressReference]
        if hasattr(self._on_complete_, 'progress_id'):  # type: ignore
            return self._on_complete_  # type: ignore
        return None

    @property
    def _on_complete(self):
        # type: () -> Optional[Callable]
        if self._on_complete_ is None:
            return None

        def callback(result):
            try:
                if self.update_progress:
                    assert self.progress_reference
                    self.progress_reference.progress = 1.0
                self._on_complete_ = None
                self._finalize(result)
            except Exception as e:
                try:
                    self.fail(e)
                except Exception:
                    logger.exception(f'failed to fail AsyncCompletion: >{repr(self)}<')
                    if 'UNITTEST' in os.environ:
                        assert False

        def error_callback(e):
            pass

        def run(value):
            def do_work(*args, **kwargs):
                assert self._on_complete_ is not None
                try:
                    res = self._on_complete_(*args, **kwargs)
                    if self.update_progress and self.many:
                        assert self.progress_reference
                        self.progress_reference.progress += 1.0 / len(value)
                    return res
                except Exception as e:
                    self.fail(e)
                    raise

            assert CephadmOrchestrator.instance
            if self.many:
                if not value:
                    logger.info('calling map_async without values')
                    callback([])
                if six.PY3:
                    CephadmOrchestrator.instance._worker_pool.map_async(do_work, value,
                                                                    callback=callback,
                                                                    error_callback=error_callback)
                else:
                    CephadmOrchestrator.instance._worker_pool.map_async(do_work, value,
                                                                    callback=callback)
            else:
                if six.PY3:
                    CephadmOrchestrator.instance._worker_pool.apply_async(do_work, (value,),
                                                                      callback=callback, error_callback=error_callback)
                else:
                    CephadmOrchestrator.instance._worker_pool.apply_async(do_work, (value,),
                                                                      callback=callback)
            return self.ASYNC_RESULT

        return run

    @_on_complete.setter
    def _on_complete(self, inner):
        # type: (Callable) -> None
        self._on_complete_ = inner


def ssh_completion(cls=AsyncCompletion, **c_kwargs):
    # type: (Type[orchestrator.Completion], Any) -> Callable
    """
    See ./HACKING.rst for a how-to
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args):

            name = f.__name__
            many = c_kwargs.get('many', False)

            # Some weired logic to make calling functions with multiple arguments work.
            if len(args) == 1:
                [value] = args
                if many and value and isinstance(value[0], tuple):
                    return cls(on_complete=lambda x: f(*x), value=value, name=name, **c_kwargs)
                else:
                    return cls(on_complete=f, value=value, name=name, **c_kwargs)
            else:
                if many:
                    self, value = args

                    def call_self(inner_args):
                        if not isinstance(inner_args, tuple):
                            inner_args = (inner_args, )
                        return f(self, *inner_args)

                    return cls(on_complete=call_self, value=value, name=name, **c_kwargs)
                else:
                    return cls(on_complete=lambda x: f(*x), value=args, name=name, **c_kwargs)

        return wrapper
    return decorator


def async_completion(f):
    # type: (Callable) -> Callable[..., AsyncCompletion]
    """
    See ./HACKING.rst for a how-to

    :param f: wrapped function
    """
    return ssh_completion()(f)


def async_map_completion(f):
    # type: (Callable) -> Callable[..., AsyncCompletion]
    """
    See ./HACKING.rst for a how-to

    :param f: wrapped function

    kind of similar to

    >>> def sync_map(f):
    ...     return lambda x: map(f, x)

    """
    return ssh_completion(many=True)(f)


def trivial_completion(f):
    # type: (Callable) -> Callable[..., orchestrator.Completion]
    @wraps(f)
    def wrapper(*args, **kwargs):
        return AsyncCompletion(value=f(*args, **kwargs), name=f.__name__)
    return wrapper


@six.add_metaclass(CLICommandMeta)
class CephadmOrchestrator(orchestrator.Orchestrator, MgrModule):

    _STORE_HOST_PREFIX = "host"

    instance = None
    NATIVE_OPTIONS = []  # type: List[Any]
    MODULE_OPTIONS = [
        {
            'name': 'ssh_config_file',
            'type': 'str',
            'default': None,
            'desc': 'customized SSH config file to connect to managed hosts',
        },
        {
            'name': 'device_cache_timeout',
            'type': 'secs',
            'default': 30 * 60,
            'desc': 'seconds to cache device inventory',
        },
        {
            'name': 'daemon_cache_timeout',
            'type': 'secs',
            'default': 10 * 60,
            'desc': 'seconds to cache service (daemon) inventory',
        },
        {
            'name': 'host_check_interval',
            'type': 'secs',
            'default': 10 * 60,
            'desc': 'how frequently to perform a host check',
        },
        {
            'name': 'mode',
            'type': 'str',
            'enum_allowed': ['root', 'cephadm-package'],
            'default': 'root',
            'desc': 'mode for remote execution of cephadm',
        },
        {
            'name': 'container_image_base',
            'default': 'docker.io/ceph/ceph',
            'desc': 'Container image name, without the tag',
            'runtime': True,
        },
        {
            'name': 'warn_on_stray_hosts',
            'type': 'bool',
            'default': True,
            'desc': 'raise a health warning if daemons are detected on a host '
                    'that is not managed by cephadm',
        },
        {
            'name': 'warn_on_stray_daemons',
            'type': 'bool',
            'default': True,
            'desc': 'raise a health warning if daemons are detected '
                    'that are not managed by cephadm',
        },
        {
            'name': 'warn_on_failed_host_check',
            'type': 'bool',
            'default': True,
            'desc': 'raise a health warning if the host check fails',
        },
        {
            'name': 'log_to_cluster',
            'type': 'bool',
            'default': True,
            'desc': 'log to the "cephadm" cluster log channel"',
        },
        {
            'name': 'allow_ptrace',
            'type': 'bool',
            'default': False,
            'desc': 'allow SYS_PTRACE capability on ceph containers',
            'long_desc': 'The SYS_PTRACE capability is needed to attach to a '
                         'process with gdb or strace.  Enabling this options '
                         'can allow debugging daemons that encounter problems '
                         'at runtime.',
        },
        {
            'name': 'prometheus_alerts_path',
            'type': 'str',
            'default': '/etc/prometheus/ceph/ceph_default_alerts.yml',
            'desc': 'location of alerts to include in prometheus deployments',
        },
    ]

    def __init__(self, *args, **kwargs):
        super(CephadmOrchestrator, self).__init__(*args, **kwargs)
        self._cluster_fsid = self.get('mon_map')['fsid']

        # for serve()
        self.run = True
        self.event = Event()

        if self.get_store('pause'):
            self.paused = True
        else:
            self.paused = False

        # for mypy which does not run the code
        if TYPE_CHECKING:
            self.ssh_config_file = None  # type: Optional[str]
            self.device_cache_timeout = 0
            self.daemon_cache_timeout = 0
            self.host_check_interval = 0
            self.mode = ''
            self.container_image_base = ''
            self.warn_on_stray_hosts = True
            self.warn_on_stray_daemons = True
            self.warn_on_failed_host_check = True
            self.allow_ptrace = False
            self.prometheus_alerts_path = ''

        self._cons = {}  # type: Dict[str, Tuple[remoto.backends.BaseConnection,remoto.backends.LegacyModuleExecute]]

        self.config_notify()

        path = self.get_ceph_option('cephadm_path')
        try:
            with open(path, 'r') as f:
                self._cephadm = f.read()
        except (IOError, TypeError) as e:
            raise RuntimeError("unable to read cephadm at '%s': %s" % (
                path, str(e)))

        self._worker_pool = multiprocessing.pool.ThreadPool(10)

        self._reconfig_ssh()

        CephadmOrchestrator.instance = self

        t = self.get_store('upgrade_state')
        if t:
            self.upgrade_state = json.loads(t)
        else:
            self.upgrade_state = None

        self.health_checks = {}

        self.all_progress_references = list()  # type: List[orchestrator.ProgressReference]

        self.inventory = Inventory(self)

        self.cache = HostCache(self)
        self.cache.load()
        self.rm_util = RemoveUtil(self)

        self.spec_store = SpecStore(self)
        self.spec_store.load()

        # ensure the host lists are in sync
        for h in self.inventory.keys():
            if h not in self.cache.daemons:
                self.cache.prime_empty_host(h)
        for h in self.cache.get_hosts():
            if h not in self.inventory:
                self.cache.rm_host(h)

        # in-memory only.
        self.offline_hosts: Set[str] = set()

    def shutdown(self):
        self.log.debug('shutdown')
        self._worker_pool.close()
        self._worker_pool.join()
        self.run = False
        self.event.set()

    def _kick_serve_loop(self):
        self.log.debug('_kick_serve_loop')
        self.event.set()

    def _check_safe_to_destroy_mon(self, mon_id):
        # type: (str) -> None
        ret, out, err = self.check_mon_command({
            'prefix': 'quorum_status',
        })
        try:
            j = json.loads(out)
        except Exception as e:
            raise OrchestratorError('failed to parse quorum status')

        mons = [m['name'] for m in j['monmap']['mons']]
        if mon_id not in mons:
            self.log.info('Safe to remove mon.%s: not in monmap (%s)' % (
                mon_id, mons))
            return
        new_mons = [m for m in mons if m != mon_id]
        new_quorum = [m for m in j['quorum_names'] if m != mon_id]
        if len(new_quorum) > len(new_mons) / 2:
            self.log.info('Safe to remove mon.%s: new quorum should be %s (from %s)' % (mon_id, new_quorum, new_mons))
            return
        raise OrchestratorError('Removing %s would break mon quorum (new quorum %s, new mons %s)' % (mon_id, new_quorum, new_mons))

    def _wait_for_ok_to_stop(self, s):
        # only wait a little bit; the service might go away for something
        tries = 4
        while tries > 0:
            if s.daemon_type not in ['mon', 'osd', 'mds']:
                self.log.info('Upgrade: It is presumed safe to stop %s.%s' %
                              (s.daemon_type, s.daemon_id))
                return True
            ret, out, err = self.mon_command({
                'prefix': '%s ok-to-stop' % s.daemon_type,
                'ids': [s.daemon_id],
            })
            if not self.upgrade_state or self.upgrade_state.get('paused'):
                return False
            if ret:
                self.log.info('Upgrade: It is NOT safe to stop %s.%s' %
                              (s.daemon_type, s.daemon_id))
                time.sleep(15)
                tries -= 1
            else:
                self.log.info('Upgrade: It is safe to stop %s.%s' %
                              (s.daemon_type, s.daemon_id))
                return True
        return False

    def _clear_upgrade_health_checks(self):
        for k in ['UPGRADE_NO_STANDBY_MGR',
                  'UPGRADE_FAILED_PULL']:
            if k in self.health_checks:
                del self.health_checks[k]
        self.set_health_checks(self.health_checks)

    def _fail_upgrade(self, alert_id, alert):
        self.log.error('Upgrade: Paused due to %s: %s' % (alert_id,
                                                          alert['summary']))
        self.upgrade_state['error'] = alert_id + ': ' + alert['summary']
        self.upgrade_state['paused'] = True
        self._save_upgrade_state()
        self.health_checks[alert_id] = alert
        self.set_health_checks(self.health_checks)

    def _update_upgrade_progress(self, progress):
        if 'progress_id' not in self.upgrade_state:
            self.upgrade_state['progress_id'] = str(uuid.uuid4())
            self._save_upgrade_state()
        self.remote('progress', 'update', self.upgrade_state['progress_id'],
                    ev_msg='Upgrade to %s' % self.upgrade_state['target_name'],
                    ev_progress=progress)

    def _do_upgrade(self):
        # type: () -> None
        if not self.upgrade_state:
            self.log.debug('_do_upgrade no state, exiting')
            return

        target_name = self.upgrade_state.get('target_name')
        target_id = self.upgrade_state.get('target_id', None)
        if not target_id:
            # need to learn the container hash
            self.log.info('Upgrade: First pull of %s' % target_name)
            try:
                target_id, target_version = self._get_container_image_id(target_name)
            except OrchestratorError as e:
                self._fail_upgrade('UPGRADE_FAILED_PULL', {
                    'severity': 'warning',
                    'summary': 'Upgrade: failed to pull target image',
                    'count': 1,
                    'detail': [str(e)],
                })
                return
            self.upgrade_state['target_id'] = target_id
            self.upgrade_state['target_version'] = target_version
            self._save_upgrade_state()
        target_version = self.upgrade_state.get('target_version')
        self.log.info('Upgrade: Target is %s with id %s' % (target_name,
                                                            target_id))

        # get all distinct container_image settings
        image_settings = {}
        ret, out, err = self.mon_command({
            'prefix': 'config dump',
            'format': 'json',
        })
        config = json.loads(out)
        for opt in config:
            if opt['name'] == 'container_image':
                image_settings[opt['section']] = opt['value']

        daemons = self.cache.get_daemons()
        done = 0
        for daemon_type in CEPH_UPGRADE_ORDER:
            self.log.info('Upgrade: Checking %s daemons...' % daemon_type)
            need_upgrade_self = False
            for d in daemons:
                if d.daemon_type != daemon_type:
                    continue
                if d.container_image_id == target_id:
                    self.log.debug('daemon %s.%s version correct' % (
                        daemon_type, d.daemon_id))
                    done += 1
                    continue
                self.log.debug('daemon %s.%s not correct (%s, %s, %s)' % (
                    daemon_type, d.daemon_id,
                    d.container_image_name, d.container_image_id, d.version))

                if daemon_type == 'mgr' and \
                   d.daemon_id == self.get_mgr_id():
                    self.log.info('Upgrade: Need to upgrade myself (mgr.%s)' %
                                  self.get_mgr_id())
                    need_upgrade_self = True
                    continue

                # make sure host has latest container image
                out, err, code = self._run_cephadm(
                    d.hostname, None, 'inspect-image', [],
                    image=target_name, no_fsid=True, error_ok=True)
                if code or json.loads(''.join(out)).get('image_id') != target_id:
                    self.log.info('Upgrade: Pulling %s on %s' % (target_name,
                                                                 d.hostname))
                    out, err, code = self._run_cephadm(
                        d.hostname, None, 'pull', [],
                        image=target_name, no_fsid=True, error_ok=True)
                    if code:
                        self._fail_upgrade('UPGRADE_FAILED_PULL', {
                            'severity': 'warning',
                            'summary': 'Upgrade: failed to pull target image',
                            'count': 1,
                            'detail': [
                                'failed to pull %s on host %s' % (target_name,
                                                                  d.hostname)],
                        })
                        return
                    r = json.loads(''.join(out))
                    if r.get('image_id') != target_id:
                        self.log.info('Upgrade: image %s pull on %s got new image %s (not %s), restarting' % (target_name, d.hostname, r['image_id'], target_id))
                        self.upgrade_state['target_id'] = r['image_id']
                        self._save_upgrade_state()
                        return

                self._update_upgrade_progress(done / len(daemons))

                if not d.container_image_id:
                    if d.container_image_name == target_name:
                        self.log.debug('daemon %s has unknown container_image_id but has correct image name' % (d.name()))
                        continue
                if not self._wait_for_ok_to_stop(d):
                    return
                self.log.info('Upgrade: Redeploying %s.%s' %
                              (d.daemon_type, d.daemon_id))
                ret, out, err = self.mon_command({
                    'prefix': 'config set',
                    'name': 'container_image',
                    'value': target_name,
                    'who': utils.name_to_config_section(daemon_type + '.' + d.daemon_id),
                })
                self._daemon_action(
                    d.daemon_type,
                    d.daemon_id,
                    d.hostname,
                    'redeploy'
                )
                return

            if need_upgrade_self:
                mgr_map = self.get('mgr_map')
                num = len(mgr_map.get('standbys'))
                if not num:
                    self._fail_upgrade('UPGRADE_NO_STANDBY_MGR', {
                        'severity': 'warning',
                        'summary': 'Upgrade: Need standby mgr daemon',
                        'count': 1,
                        'detail': [
                            'The upgrade process needs to upgrade the mgr, '
                            'but it needs at least one standby to proceed.',
                        ],
                    })
                    return

                self.log.info('Upgrade: there are %d other already-upgraded '
                              'standby mgrs, failing over' % num)

                self._update_upgrade_progress(done / len(daemons))

                # fail over
                ret, out, err = self.mon_command({
                    'prefix': 'mgr fail',
                    'who': self.get_mgr_id(),
                })
                return
            elif daemon_type == 'mgr':
                if 'UPGRADE_NO_STANDBY_MGR' in self.health_checks:
                    del self.health_checks['UPGRADE_NO_STANDBY_MGR']
                    self.set_health_checks(self.health_checks)

            # make sure 'ceph versions' agrees
            ret, out, err = self.mon_command({
                'prefix': 'versions',
            })
            j = json.loads(out)
            for version, count in j.get(daemon_type, {}).items():
                if version != target_version:
                    self.log.warning(
                        'Upgrade: %d %s daemon(s) are %s != target %s' %
                        (count, daemon_type, version, target_version))

            # push down configs
            if image_settings.get(daemon_type) != target_name:
                self.log.info('Upgrade: Setting container_image for all %s...' %
                              daemon_type)
                ret, out, err = self.mon_command({
                    'prefix': 'config set',
                    'name': 'container_image',
                    'value': target_name,
                    'who': daemon_type,
                })
            to_clean = []
            for section in image_settings.keys():
                if section.startswith(utils.name_to_config_section(daemon_type) + '.'):
                    to_clean.append(section)
            if to_clean:
                self.log.debug('Upgrade: Cleaning up container_image for %s...' %
                               to_clean)
                for section in to_clean:
                    ret, image, err = self.mon_command({
                        'prefix': 'config rm',
                        'name': 'container_image',
                        'who': section,
                    })

            self.log.info('Upgrade: All %s daemons are up to date.' %
                          daemon_type)

        # clean up
        self.log.info('Upgrade: Finalizing container_image settings')
        ret, out, err = self.mon_command({
            'prefix': 'config set',
            'name': 'container_image',
            'value': target_name,
            'who': 'global',
        })
        for daemon_type in CEPH_UPGRADE_ORDER:
            ret, image, err = self.mon_command({
                'prefix': 'config rm',
                'name': 'container_image',
                'who': utils.name_to_config_section(daemon_type),
            })

        self.log.info('Upgrade: Complete!')
        if 'progress_id' in self.upgrade_state:
            self.remote('progress', 'complete',
                        self.upgrade_state['progress_id'])
        self.upgrade_state = None
        self._save_upgrade_state()
        return

    def _check_host(self, host):
        if host not in self.inventory:
            return
        self.log.debug(' checking %s' % host)
        try:
            out, err, code = self._run_cephadm(
                host, 'client', 'check-host', [],
                error_ok=True, no_fsid=True)
            self.cache.update_last_host_check(host)
            self.cache.save_host(host)
            if code:
                self.log.debug(' host %s failed check' % host)
                if self.warn_on_failed_host_check:
                    return 'host %s failed check: %s' % (host, err)
            else:
                self.log.debug(' host %s ok' % host)
        except Exception as e:
            self.log.debug(' host %s failed check' % host)
            return 'host %s failed check: %s' % (host, e)

    def _check_for_strays(self):
        self.log.debug('_check_for_strays')
        for k in ['CEPHADM_STRAY_HOST',
                  'CEPHADM_STRAY_DAEMON']:
            if k in self.health_checks:
                del self.health_checks[k]
        if self.warn_on_stray_hosts or self.warn_on_stray_daemons:
            ls = self.list_servers()
            managed = self.cache.get_daemon_names()
            host_detail = []     # type: List[str]
            host_num_daemons = 0
            daemon_detail = []  # type: List[str]
            for item in ls:
                host = item.get('hostname')
                daemons = item.get('services')  # misnomer!
                missing_names = []
                for s in daemons:
                    name = '%s.%s' % (s.get('type'), s.get('id'))
                    if host not in self.inventory:
                        missing_names.append(name)
                        host_num_daemons += 1
                    if name not in managed:
                        daemon_detail.append(
                            'stray daemon %s on host %s not managed by cephadm' % (name, host))
                if missing_names:
                    host_detail.append(
                        'stray host %s has %d stray daemons: %s' % (
                            host, len(missing_names), missing_names))
            if self.warn_on_stray_hosts and host_detail:
                self.health_checks['CEPHADM_STRAY_HOST'] = {
                    'severity': 'warning',
                    'summary': '%d stray host(s) with %s daemon(s) '
                    'not managed by cephadm' % (
                        len(host_detail), host_num_daemons),
                    'count': len(host_detail),
                    'detail': host_detail,
                }
            if self.warn_on_stray_daemons and daemon_detail:
                self.health_checks['CEPHADM_STRAY_DAEMON'] = {
                    'severity': 'warning',
                    'summary': '%d stray daemons(s) not managed by cephadm' % (
                        len(daemon_detail)),
                    'count': len(daemon_detail),
                    'detail': daemon_detail,
                }
        self.set_health_checks(self.health_checks)

    def _serve_sleep(self):
        sleep_interval = 600
        self.log.debug('Sleeping for %d seconds', sleep_interval)
        ret = self.event.wait(sleep_interval)
        self.event.clear()

    def serve(self):
        # type: () -> None
        self.log.debug("serve starting")
        while self.run:

            # refresh daemons
            self.log.debug('refreshing hosts')
            bad_hosts = []
            failures = []
            for host in self.cache.get_hosts():
                if self.cache.host_needs_check(host):
                    r = self._check_host(host)
                    if r is not None:
                        bad_hosts.append(r)
                if self.cache.host_needs_daemon_refresh(host):
                    self.log.debug('refreshing %s daemons' % host)
                    r = self._refresh_host_daemons(host)
                    if r:
                        failures.append(r)
                if self.cache.host_needs_device_refresh(host):
                    self.log.debug('refreshing %s devices' % host)
                    r = self._refresh_host_devices(host)
                    if r:
                        failures.append(r)

            health_changed = False
            if 'CEPHADM_HOST_CHECK_FAILED' in self.health_checks:
                del self.health_checks['CEPHADM_HOST_CHECK_FAILED']
                health_changed = True
            if bad_hosts:
                self.health_checks['CEPHADM_HOST_CHECK_FAILED'] = {
                    'severity': 'warning',
                    'summary': '%d hosts fail cephadm check' % len(bad_hosts),
                    'count': len(bad_hosts),
                    'detail': bad_hosts,
                }
                health_changed = True
            if failures:
                self.health_checks['CEPHADM_REFRESH_FAILED'] = {
                    'severity': 'warning',
                    'summary': 'failed to probe daemons or devices',
                    'count': len(failures),
                    'detail': failures,
                }
                health_changed = True
            elif 'CEPHADM_REFRESH_FAILED' in self.health_checks:
                del self.health_checks['CEPHADM_REFRESH_FAILED']
                health_changed = True
            if health_changed:
                self.set_health_checks(self.health_checks)



            self._check_for_strays()

            if self.paused:
                self.health_checks['CEPHADM_PAUSED'] = {
                    'severity': 'warning',
                    'summary': 'cephadm background work is paused',
                    'count': 1,
                    'detail': ["'ceph orch resume' to resume"],
                }
                self.set_health_checks(self.health_checks)
            else:
                if 'CEPHADM_PAUSED' in self.health_checks:
                    del self.health_checks['CEPHADM_PAUSED']
                    self.set_health_checks(self.health_checks)

                self.rm_util._remove_osds_bg()

                if self._apply_all_services():
                    continue  # did something, refresh

                self._check_daemons()

                if self.upgrade_state and not self.upgrade_state.get('paused'):
                    self._do_upgrade()
                    continue

            self._serve_sleep()
        self.log.debug("serve exit")

    def config_notify(self):
        """
        This method is called whenever one of our config options is changed.
        """
        for opt in self.MODULE_OPTIONS:
            setattr(self,
                    opt['name'],  # type: ignore
                    self.get_module_option(opt['name']))  # type: ignore
            self.log.debug(' mgr option %s = %s',
                           opt['name'], getattr(self, opt['name']))  # type: ignore
        for opt in self.NATIVE_OPTIONS:
            setattr(self,
                    opt,  # type: ignore
                    self.get_ceph_option(opt))
            self.log.debug(' native option %s = %s', opt, getattr(self, opt))  # type: ignore

        self.event.set()

    def notify(self, notify_type, notify_id):
        pass

    def pause(self):
        if not self.paused:
            self.log.info('Paused')
            self.set_store('pause', 'true')
            self.paused = True
            # wake loop so we update the health status
            self._kick_serve_loop()

    def resume(self):
        if self.paused:
            self.log.info('Resumed')
            self.paused = False
            self.set_store('pause', None)
        # unconditionally wake loop so that 'orch resume' can be used to kick
        # cephadm
        self._kick_serve_loop()

    def get_unique_name(self, daemon_type, host, existing, prefix=None,
                        forcename=None):
        # type: (str, str, List[orchestrator.DaemonDescription], Optional[str], Optional[str]) -> str
        """
        Generate a unique random service name
        """
        suffix = daemon_type not in [
            'mon', 'crash', 'nfs',
            'prometheus', 'node-exporter', 'grafana', 'alertmanager',
        ]
        if forcename:
            if len([d for d in existing if d.daemon_id == forcename]):
                raise orchestrator.OrchestratorValidationError('name %s already in use', forcename)
            return forcename

        if '.' in host:
            host = host.split('.')[0]
        while True:
            if prefix:
                name = prefix + '.'
            else:
                name = ''
            name += host
            if suffix:
                name += '.' + ''.join(random.choice(string.ascii_lowercase)
                                      for _ in range(6))
            if len([d for d in existing if d.daemon_id == name]):
                if not suffix:
                    raise orchestrator.OrchestratorValidationError('name %s already in use', name)
                self.log.debug('name %s exists, trying again', name)
                continue
            return name

    def get_service_name(self, daemon_type, daemon_id, host):
        # type: (str, str, str) -> (str)
        """
        Returns the generic service name
        """
        p = re.compile(r'(.*)\.%s.*' % (host))
        return '%s.%s' % (daemon_type, p.sub(r'\1', daemon_id))


    def _save_upgrade_state(self):
        self.set_store('upgrade_state', json.dumps(self.upgrade_state))

    def _reconfig_ssh(self):
        temp_files = []  # type: list
        ssh_options = []  # type: List[str]

        # ssh_config
        ssh_config_fname = self.ssh_config_file
        ssh_config = self.get_store("ssh_config")
        if ssh_config is not None or ssh_config_fname is None:
            if not ssh_config:
                ssh_config = DEFAULT_SSH_CONFIG
            f = tempfile.NamedTemporaryFile(prefix='cephadm-conf-')
            os.fchmod(f.fileno(), 0o600)
            f.write(ssh_config.encode('utf-8'))
            f.flush()  # make visible to other processes
            temp_files += [f]
            ssh_config_fname = f.name
        if ssh_config_fname:
            self.validate_ssh_config_fname(ssh_config_fname)
            ssh_options += ['-F', ssh_config_fname]

        # identity
        ssh_key = self.get_store("ssh_identity_key")
        ssh_pub = self.get_store("ssh_identity_pub")
        self.ssh_pub = ssh_pub
        self.ssh_key = ssh_key
        if ssh_key and ssh_pub:
            tkey = tempfile.NamedTemporaryFile(prefix='cephadm-identity-')
            tkey.write(ssh_key.encode('utf-8'))
            os.fchmod(tkey.fileno(), 0o600)
            tkey.flush()  # make visible to other processes
            tpub = open(tkey.name + '.pub', 'w')
            os.fchmod(tpub.fileno(), 0o600)
            tpub.write(ssh_pub)
            tpub.flush()  # make visible to other processes
            temp_files += [tkey, tpub]
            ssh_options += ['-i', tkey.name]

        self._temp_files = temp_files
        if ssh_options:
            self._ssh_options = ' '.join(ssh_options)  # type: Optional[str]
        else:
            self._ssh_options = None

        if self.mode == 'root':
            self.ssh_user = 'root'
        elif self.mode == 'cephadm-package':
            self.ssh_user = 'cephadm'

        self._reset_cons()

    def validate_ssh_config_fname(self, ssh_config_fname):
        if not os.path.isfile(ssh_config_fname):
            raise OrchestratorValidationError("ssh_config \"{}\" does not exist".format(
                ssh_config_fname))

    def _reset_con(self, host):
        conn, r = self._cons.get(host, (None, None))
        if conn:
            self.log.debug('_reset_con close %s' % host)
            conn.exit()
            del self._cons[host]

    def _reset_cons(self):
        for host, conn_and_r in self._cons.items():
            self.log.debug('_reset_cons close %s' % host)
            conn, r = conn_and_r
            conn.exit()
        self._cons = {}

    def offline_hosts_remove(self, host):
        if host in self.offline_hosts:
            self.offline_hosts.remove(host)


    @staticmethod
    def can_run():
        if remoto is not None:
            return True, ""
        else:
            return False, "loading remoto library:{}".format(
                    remoto_import_error)

    def available(self):
        """
        The cephadm orchestrator is always available.
        """
        return self.can_run()

    def process(self, completions):
        """
        Does nothing, as completions are processed in another thread.
        """
        if completions:
            self.log.debug("process: completions={0}".format(orchestrator.pretty_print(completions)))

            for p in completions:
                p.finalize()

    @orchestrator._cli_write_command(
        prefix='cephadm set-ssh-config',
        desc='Set the ssh_config file (use -i <ssh_config>)')
    def _set_ssh_config(self, inbuf=None):
        """
        Set an ssh_config file provided from stdin

        TODO:
          - validation
        """
        if inbuf is None or len(inbuf) == 0:
            return -errno.EINVAL, "", "empty ssh config provided"
        self.set_store("ssh_config", inbuf)
        self.log.info('Set ssh_config')
        return 0, "", ""

    @orchestrator._cli_write_command(
        prefix='cephadm clear-ssh-config',
        desc='Clear the ssh_config file')
    def _clear_ssh_config(self):
        """
        Clear the ssh_config file provided from stdin
        """
        self.set_store("ssh_config", None)
        self.ssh_config_tmp = None
        self.log.info('Cleared ssh_config')
        return 0, "", ""

    @orchestrator._cli_read_command(
        prefix='cephadm get-ssh-config',
        desc='Returns the ssh config as used by cephadm'
    )
    def _get_ssh_config(self):
        if self.ssh_config_file:
            self.validate_ssh_config_fname(self.ssh_config_file)
            with open(self.ssh_config_file) as f:
                return HandleCommandResult(stdout=f.read())
        ssh_config = self.get_store("ssh_config")
        if ssh_config:
            return HandleCommandResult(stdout=ssh_config)
        return HandleCommandResult(stdout=DEFAULT_SSH_CONFIG)


    @orchestrator._cli_write_command(
        'cephadm generate-key',
        desc='Generate a cluster SSH key (if not present)')
    def _generate_key(self):
        if not self.ssh_pub or not self.ssh_key:
            self.log.info('Generating ssh key...')
            tmp_dir = TemporaryDirectory()
            path = tmp_dir.name + '/key'
            try:
                subprocess.check_call([
                    '/usr/bin/ssh-keygen',
                    '-C', 'ceph-%s' % self._cluster_fsid,
                    '-N', '',
                    '-f', path
                ])
                with open(path, 'r') as f:
                    secret = f.read()
                with open(path + '.pub', 'r') as f:
                    pub = f.read()
            finally:
                os.unlink(path)
                os.unlink(path + '.pub')
                tmp_dir.cleanup()
            self.set_store('ssh_identity_key', secret)
            self.set_store('ssh_identity_pub', pub)
            self._reconfig_ssh()
        return 0, '', ''

    @orchestrator._cli_write_command(
        'cephadm clear-key',
        desc='Clear cluster SSH key')
    def _clear_key(self):
        self.set_store('ssh_identity_key', None)
        self.set_store('ssh_identity_pub', None)
        self._reconfig_ssh()
        self.log.info('Cleared cluster SSH key')
        return 0, '', ''

    @orchestrator._cli_read_command(
        'cephadm get-pub-key',
        desc='Show SSH public key for connecting to cluster hosts')
    def _get_pub_key(self):
        if self.ssh_pub:
            return 0, self.ssh_pub, ''
        else:
            return -errno.ENOENT, '', 'No cluster SSH key defined'

    @orchestrator._cli_read_command(
        'cephadm get-user',
        desc='Show user for SSHing to cluster hosts')
    def _get_user(self):
        return 0, self.ssh_user, ''

    @orchestrator._cli_read_command(
        'cephadm check-host',
        'name=host,type=CephString '
        'name=addr,type=CephString,req=false',
        'Check whether we can access and manage a remote host')
    def check_host(self, host, addr=None):
        out, err, code = self._run_cephadm(host, 'client', 'check-host',
                                           ['--expect-hostname', host],
                                           addr=addr,
                                           error_ok=True, no_fsid=True)
        if code:
            return 1, '', ('check-host failed:\n' + '\n'.join(err))
        # if we have an outstanding health alert for this host, give the
        # serve thread a kick
        if 'CEPHADM_HOST_CHECK_FAILED' in self.health_checks:
            for item in self.health_checks['CEPHADM_HOST_CHECK_FAILED']['detail']:
                if item.startswith('host %s ' % host):
                    self.event.set()
        return 0, '%s (%s) ok' % (host, addr), err

    @orchestrator._cli_read_command(
        'cephadm prepare-host',
        'name=host,type=CephString '
        'name=addr,type=CephString,req=false',
        'Prepare a remote host for use with cephadm')
    def _prepare_host(self, host, addr=None):
        out, err, code = self._run_cephadm(host, 'client', 'prepare-host',
                                           ['--expect-hostname', host],
                                           addr=addr,
                                           error_ok=True, no_fsid=True)
        if code:
            return 1, '', ('prepare-host failed:\n' + '\n'.join(err))
        # if we have an outstanding health alert for this host, give the
        # serve thread a kick
        if 'CEPHADM_HOST_CHECK_FAILED' in self.health_checks:
            for item in self.health_checks['CEPHADM_HOST_CHECK_FAILED']['detail']:
                if item.startswith('host %s ' % host):
                    self.event.set()
        return 0, '%s (%s) ok' % (host, addr), err

    def _get_connection(self, host):
        """
        Setup a connection for running commands on remote host.
        """
        conn_and_r = self._cons.get(host)
        if conn_and_r:
            self.log.debug('Have connection to %s' % host)
            return conn_and_r
        n = self.ssh_user + '@' + host
        self.log.debug("Opening connection to {} with ssh options '{}'".format(
            n, self._ssh_options))
        child_logger=self.log.getChild(n)
        child_logger.setLevel('WARNING')
        conn = remoto.Connection(
            n,
            logger=child_logger,
            ssh_options=self._ssh_options)

        r = conn.import_module(remotes)
        self._cons[host] = conn, r

        return conn, r

    def _executable_path(self, conn, executable):
        """
        Remote validator that accepts a connection object to ensure that a certain
        executable is available returning its full path if so.

        Otherwise an exception with thorough details will be raised, informing the
        user that the executable was not found.
        """
        executable_path = conn.remote_module.which(executable)
        if not executable_path:
            raise RuntimeError("Executable '{}' not found on host '{}'".format(
                executable, conn.hostname))
        self.log.debug("Found executable '{}' at path '{}'".format(executable,
            executable_path))
        return executable_path

    def _run_cephadm(self, host, entity, command, args,
                     addr=None,
                     stdin=None,
                     no_fsid=False,
                     error_ok=False,
                     image=None):
        # type: (str, Optional[str], str, List[str], Optional[str], Optional[str], bool, bool, Optional[str]) -> Tuple[List[str], List[str], int]
        """
        Run cephadm on the remote host with the given command + args
        """
        if not addr and host in self.inventory:
            addr = self.inventory.get_addr(host)

        self.offline_hosts_remove(host)

        try:
            try:
                conn, connr = self._get_connection(addr)
            except IOError as e:
                if error_ok:
                    self.log.exception('failed to establish ssh connection')
                    return [], [str("Can't communicate with remote host, possibly because python3 is not installed there")], 1
                raise

            assert image or entity
            if not image:
                daemon_type = entity.split('.', 1)[0] # type: ignore
                if daemon_type in CEPH_TYPES or \
                        daemon_type == 'nfs' or \
                        daemon_type == 'iscsi':
                    # get container image
                    ret, image, err = self.check_mon_command({
                        'prefix': 'config get',
                        'who': utils.name_to_config_section(entity),
                        'key': 'container_image',
                    })
                    image = image.strip() # type: ignore
            self.log.debug('%s container image %s' % (entity, image))

            final_args = []
            if image:
                final_args.extend(['--image', image])
            final_args.append(command)

            if not no_fsid:
                final_args += ['--fsid', self._cluster_fsid]
            final_args += args

            if self.mode == 'root':
                self.log.debug('args: %s' % (' '.join(final_args)))
                if stdin:
                    self.log.debug('stdin: %s' % stdin)
                script = 'injected_argv = ' + json.dumps(final_args) + '\n'
                if stdin:
                    script += 'injected_stdin = ' + json.dumps(stdin) + '\n'
                script += self._cephadm
                python = connr.choose_python()
                if not python:
                    raise RuntimeError(
                        'unable to find python on %s (tried %s in %s)' % (
                            host, remotes.PYTHONS, remotes.PATH))
                try:
                    out, err, code = remoto.process.check(
                        conn,
                        [python, '-u'],
                        stdin=script.encode('utf-8'))
                except RuntimeError as e:
                    self._reset_con(host)
                    if error_ok:
                        return [], [str(e)], 1
                    raise
            elif self.mode == 'cephadm-package':
                try:
                    out, err, code = remoto.process.check(
                        conn,
                        ['sudo', '/usr/bin/cephadm'] + final_args,
                        stdin=stdin)
                except RuntimeError as e:
                    self._reset_con(host)
                    if error_ok:
                        return [], [str(e)], 1
                    raise
            else:
                assert False, 'unsupported mode'

            self.log.debug('code: %d' % code)
            if out:
                self.log.debug('out: %s' % '\n'.join(out))
            if err:
                self.log.debug('err: %s' % '\n'.join(err))
            if code and not error_ok:
                raise RuntimeError(
                    'cephadm exited with an error code: %d, stderr:%s' % (
                        code, '\n'.join(err)))
            return out, err, code

        except execnet.gateway_bootstrap.HostNotFound as e:
            # this is a misleading exception as it seems to be thrown for
            # any sort of connection failure, even those having nothing to
            # do with "host not found" (e.g., ssh key permission denied).
            self.offline_hosts.add(host)
            user = 'root' if self.mode == 'root' else 'cephadm'
            msg = f'Failed to connect to {host} ({addr}).  ' \
                  f'Check that the host is reachable and accepts connections using the cephadm SSH key\n' \
                  f'you may want to run: \n' \
                  f'> ssh -F =(ceph cephadm get-ssh-config) -i =(ceph config-key get mgr/cephadm/ssh_identity_key) {user}@{host}'
            raise OrchestratorError(msg) from e
        except Exception as ex:
            self.log.exception(ex)
            raise

    def _get_hosts(self, label=None):
        # type: (Optional[str]) -> List[str]
        return list(self.inventory.filter_by_label(label))

    @async_completion
    def add_host(self, spec):
        # type: (HostSpec) -> str
        """
        Add a host to be managed by the orchestrator.

        :param host: host name
        """
        assert_valid_host(spec.hostname)
        out, err, code = self._run_cephadm(spec.hostname, 'client', 'check-host',
                                           ['--expect-hostname', spec.hostname],
                                           addr=spec.addr,
                                           error_ok=True, no_fsid=True)
        if code:
            raise OrchestratorError('New host %s (%s) failed check: %s' % (
                spec.hostname, spec.addr, err))

        self.inventory.add_host(spec)
        self.cache.prime_empty_host(spec.hostname)
        self.offline_hosts_remove(spec.hostname)
        self.event.set()  # refresh stray health check
        self.log.info('Added host %s' % spec.hostname)
        return "Added host '{}'".format(spec.hostname)

    @async_completion
    def remove_host(self, host):
        # type: (str) -> str
        """
        Remove a host from orchestrator management.

        :param host: host name
        """
        self.inventory.rm_host(host)
        self.cache.rm_host(host)
        self._reset_con(host)
        self.event.set()  # refresh stray health check
        self.log.info('Removed host %s' % host)
        return "Removed host '{}'".format(host)

    @async_completion
    def update_host_addr(self, host, addr):
        self.inventory.set_addr(host, addr)
        self._reset_con(host)
        self.event.set()  # refresh stray health check
        self.log.info('Set host %s addr to %s' % (host, addr))
        return "Updated host '{}' addr to '{}'".format(host, addr)

    @trivial_completion
    def get_hosts(self):
        # type: () -> List[orchestrator.HostSpec]
        """
        Return a list of hosts managed by the orchestrator.

        Notes:
          - skip async: manager reads from cache.
        """
        return list(self.inventory.all_specs())

    @async_completion
    def add_host_label(self, host, label):
        self.inventory.add_label(host, label)
        self.log.info('Added label %s to host %s' % (label, host))
        return 'Added label %s to host %s' % (label, host)

    @async_completion
    def remove_host_label(self, host, label):
        self.inventory.rm_label(host, label)
        self.log.info('Removed label %s to host %s' % (label, host))
        return 'Removed label %s from host %s' % (label, host)

    def _refresh_host_daemons(self, host):
        try:
            out, err, code = self._run_cephadm(
                host, 'mon', 'ls', [], no_fsid=True)
            if code:
                return 'host %s cephadm ls returned %d: %s' % (
                    host, code, err)
        except Exception as e:
            return 'host %s scrape failed: %s' % (host, e)
        ls = json.loads(''.join(out))
        dm = {}
        for d in ls:
            if not d['style'].startswith('cephadm'):
                continue
            if d['fsid'] != self._cluster_fsid:
                continue
            if '.' not in d['name']:
                continue
            sd = orchestrator.DaemonDescription()
            sd.last_refresh = datetime.datetime.utcnow()
            for k in ['created', 'started', 'last_configured', 'last_deployed']:
                v = d.get(k, None)
                if v:
                    setattr(sd, k, datetime.datetime.strptime(d[k], DATEFMT))
            sd.daemon_type = d['name'].split('.')[0]
            sd.daemon_id = '.'.join(d['name'].split('.')[1:])
            sd.hostname = host
            sd.container_id = d.get('container_id')
            if sd.container_id:
                # shorten the hash
                sd.container_id = sd.container_id[0:12]
            sd.container_image_name = d.get('container_image_name')
            sd.container_image_id = d.get('container_image_id')
            sd.version = d.get('version')
            if 'state' in d:
                sd.status_desc = d['state']
                sd.status = {
                    'running': 1,
                    'stopped': 0,
                    'error': -1,
                    'unknown': -1,
                }[d['state']]
            else:
                sd.status_desc = 'unknown'
                sd.status = None
            dm[sd.name()] = sd
        self.log.debug('Refreshed host %s daemons (%d)' % (host, len(dm)))
        self.cache.update_host_daemons(host, dm)
        self.cache.save_host(host)
        return None

    def _refresh_host_devices(self, host):
        try:
            out, err, code = self._run_cephadm(
                host, 'osd',
                'ceph-volume',
                ['--', 'inventory', '--format=json'])
            if code:
                return 'host %s ceph-volume inventory returned %d: %s' % (
                    host, code, err)
        except Exception as e:
            return 'host %s ceph-volume inventory failed: %s' % (host, e)
        devices = json.loads(''.join(out))
        try:
            out, err, code = self._run_cephadm(
                host, 'mon',
                'list-networks',
                [],
                no_fsid=True)
            if code:
                return 'host %s list-networks returned %d: %s' % (
                    host, code, err)
        except Exception as e:
            return 'host %s list-networks failed: %s' % (host, e)
        networks = json.loads(''.join(out))
        self.log.debug('Refreshed host %s devices (%d) networks (%s)' % (
            host, len(devices), len(networks)))
        devices = inventory.Devices.from_json(devices)
        self.cache.update_host_devices_networks(host, devices.devices, networks)
        self.cache.save_host(host)
        return None

    def _get_spec_size(self, spec):
        if spec.placement.count:
            return spec.placement.count
        elif spec.placement.host_pattern:
            return len(spec.placement.pattern_matches_hosts(self.inventory.keys()))
        elif spec.placement.label:
            return len(self._get_hosts(spec.placement.label))
        elif spec.placement.hosts:
            return len(spec.placement.hosts)
        # hmm!
        return 0

    @trivial_completion
    def describe_service(self, service_type=None, service_name=None,
                         refresh=False):
        if refresh:
            # ugly sync path, FIXME someday perhaps?
            for host in self.inventory.keys():
                self._refresh_host_daemons(host)
        # <service_map>
        sm = {}  # type: Dict[str, orchestrator.ServiceDescription]
        for h, dm in self.cache.get_daemons_with_volatile_status():
            for name, dd in dm.items():
                if service_type and service_type != dd.daemon_type:
                    continue
                n: str = dd.service_name()
                if service_name and service_name != n:
                    continue
                if dd.daemon_type == 'osd':
                    continue                # ignore OSDs for now
                if dd.service_name() in self.spec_store.specs:
                    spec = self.spec_store.specs[dd.service_name()]
                else:
                    spec = ServiceSpec(
                        unmanaged=True,
                        service_type=dd.daemon_type,
                        service_id=dd.service_id(),
                        placement=PlacementSpec(
                            hosts=[dd.hostname]
                        )
                    )
                if n not in sm:
                    sm[n] = orchestrator.ServiceDescription(
                        last_refresh=dd.last_refresh,
                        container_image_id=dd.container_image_id,
                        container_image_name=dd.container_image_name,
                        spec=spec,
                    )
                if dd.service_name() in self.spec_store.specs:
                    sm[n].size = self._get_spec_size(spec)
                    sm[n].created = self.spec_store.spec_created[dd.service_name()]
                    if service_type == 'nfs':
                        spec = cast(NFSServiceSpec, spec)
                        sm[n].rados_config_location = spec.rados_config_location()
                else:
                    sm[n].size = 0
                if dd.status == 1:
                    sm[n].running += 1
                if not sm[n].last_refresh or not dd.last_refresh or dd.last_refresh < sm[n].last_refresh:  # type: ignore
                    sm[n].last_refresh = dd.last_refresh
                if sm[n].container_image_id != dd.container_image_id:
                    sm[n].container_image_id = 'mix'
                if sm[n].container_image_name != dd.container_image_name:
                    sm[n].container_image_name = 'mix'
        for n, spec in self.spec_store.specs.items():
            if n in sm:
                continue
            if service_type is not None and service_type != spec.service_type:
                continue
            if service_name is not None and service_name != n:
                continue
            sm[n] = orchestrator.ServiceDescription(
                spec=spec,
                size=self._get_spec_size(spec),
                running=0,
            )
            if service_type == 'nfs':
                spec = cast(NFSServiceSpec, spec)
                sm[n].rados_config_location = spec.rados_config_location()
        return list(sm.values())

    @trivial_completion
    def list_daemons(self, service_name=None, daemon_type=None, daemon_id=None,
                     host=None, refresh=False):
        if refresh:
            # ugly sync path, FIXME someday perhaps?
            if host:
                self._refresh_host_daemons(host)
            else:
                for hostname in self.inventory.keys():
                    self._refresh_host_daemons(hostname)
        result = []
        for h, dm in self.cache.get_daemons_with_volatile_status():
            if host and h != host:
                continue
            for name, dd in dm.items():
                if daemon_type is not None and daemon_type != dd.daemon_type:
                    continue
                if daemon_id is not None and daemon_id != dd.daemon_id:
                    continue
                if service_name is not None and service_name != dd.service_name():
                    continue
                result.append(dd)
        return result

    def service_action(self, action, service_name):
        args = []
        for host, dm in self.cache.daemons.items():
            for name, d in dm.items():
                if d.matches_service(service_name):
                    args.append((d.daemon_type, d.daemon_id,
                                 d.hostname, action))
        self.log.info('%s service %s' % (action.capitalize(), service_name))
        return self._daemon_actions(args)

    @async_map_completion
    def _daemon_actions(self, daemon_type, daemon_id, host, action):
        return self._daemon_action(daemon_type, daemon_id, host, action)

    def _daemon_action(self, daemon_type, daemon_id, host, action):
        if action == 'redeploy':
            # stop, recreate the container+unit, then restart
            return self._create_daemon(daemon_type, daemon_id, host)
        elif action == 'reconfig':
            return self._create_daemon(daemon_type, daemon_id, host,
                                       reconfig=True)

        actions = {
            'start': ['reset-failed', 'start'],
            'stop': ['stop'],
            'restart': ['reset-failed', 'restart'],
        }
        name = '%s.%s' % (daemon_type, daemon_id)
        for a in actions[action]:
            out, err, code = self._run_cephadm(
                host, name, 'unit',
                ['--name', name, a],
                error_ok=True)
        self.cache.invalidate_host_daemons(host)
        return "{} {} from host '{}'".format(action, name, host)

    def daemon_action(self, action, daemon_type, daemon_id):
        args = []
        for host, dm in self.cache.daemons.items():
            for name, d in dm.items():
                if d.daemon_type == daemon_type and d.daemon_id == daemon_id:
                    args.append((d.daemon_type, d.daemon_id,
                                 d.hostname, action))
        if not args:
            raise orchestrator.OrchestratorError(
                'Unable to find %s.%s daemon(s)' % (
                    daemon_type, daemon_id))
        self.log.info('%s daemons %s' % (
            action.capitalize(),
            ','.join(['%s.%s' % (a[0], a[1]) for a in args])))
        return self._daemon_actions(args)

    def remove_daemons(self, names):
        # type: (List[str]) -> orchestrator.Completion
        args = []
        for host, dm in self.cache.daemons.items():
            for name in names:
                if name in dm:
                    args.append((name, host))
        if not args:
            raise OrchestratorError('Unable to find daemon(s) %s' % (names))
        self.log.info('Remove daemons %s' % [a[0] for a in args])
        return self._remove_daemons(args)

    @trivial_completion
    def remove_service(self, service_name):
        self.log.info('Remove service %s' % service_name)
        found = self.spec_store.rm(service_name)
        if found:
            self._kick_serve_loop()
            return ['Removed service %s' % service_name]
        else:
            # must be idempotent: still a success.
            return [f'Failed to remove service. <{service_name}> was not found.']

    @trivial_completion
    def get_inventory(self, host_filter=None, refresh=False):
        """
        Return the storage inventory of hosts matching the given filter.

        :param host_filter: host filter

        TODO:
          - add filtering by label
        """
        if refresh:
            # ugly sync path, FIXME someday perhaps?
            if host_filter:
                for host in host_filter.hosts:
                    self._refresh_host_devices(host)
            else:
                for host in self.inventory.keys():
                    self._refresh_host_devices(host)

        result = []
        for host, dls in self.cache.devices.items():
            if host_filter and host not in host_filter.hosts:
                continue
            result.append(orchestrator.InventoryHost(host,
                                                     inventory.Devices(dls)))
        return result

    @trivial_completion
    def zap_device(self, host, path):
        self.log.info('Zap device %s:%s' % (host, path))
        out, err, code = self._run_cephadm(
            host, 'osd', 'ceph-volume',
            ['--', 'lvm', 'zap', '--destroy', path],
            error_ok=True)
        self.cache.invalidate_host_devices(host)
        if code:
            raise OrchestratorError('Zap failed: %s' % '\n'.join(out + err))
        return '\n'.join(out + err)

    def blink_device_light(self, ident_fault, on, locs):
        @async_map_completion
        def blink(host, dev, path):
            cmd = [
                'lsmcli',
                'local-disk-%s-led-%s' % (
                    ident_fault,
                    'on' if on else 'off'),
                '--path', path or dev,
            ]
            out, err, code = self._run_cephadm(
                host, 'osd', 'shell', ['--'] + cmd,
                error_ok=True)
            if code:
                raise RuntimeError(
                    'Unable to affect %s light for %s:%s. Command: %s' % (
                        ident_fault, host, dev, ' '.join(cmd)))
            self.log.info('Set %s light for %s:%s %s' % (
                ident_fault, host, dev, 'on' if on else 'off'))
            return "Set %s light for %s:%s %s" % (
                ident_fault, host, dev, 'on' if on else 'off')

        return blink(locs)

    def get_osd_uuid_map(self, only_up=False):
        # type: (bool) -> Dict[str, str]
        osd_map = self.get('osd_map')
        r = {}
        for o in osd_map['osds']:
            # only include OSDs that have ever started in this map.  this way
            # an interrupted osd create can be repeated and succeed the second
            # time around.
            osd_id = o.get('osd')
            if osd_id is None:
                raise OrchestratorError("Could not retrieve osd_id from osd_map")
            if not only_up or (o['up_from'] > 0):
                r[str(osd_id)] = o.get('uuid', '')
        return r

    @trivial_completion
    def apply_drivegroups(self, specs: List[DriveGroupSpec]):
        return [self._apply(spec) for spec in specs]

    def find_destroyed_osds(self) -> Dict[str, List[str]]:
        osd_host_map: Dict[str, List[str]] = dict()
        try:
            ret, out, err = self.check_mon_command({
                'prefix': 'osd tree',
                'states': ['destroyed'],
                'format': 'json'
            })
        except MonCommandFailed as e:
            logger.exception('osd tree failed')
            raise OrchestratorError(str(e))
        try:
            tree = json.loads(out)
        except json.decoder.JSONDecodeError:
            self.log.error(f"Could not decode json -> {out}")
            return osd_host_map

        nodes = tree.get('nodes', {})
        for node in nodes:
            if node.get('type') == 'host':
                osd_host_map.update(
                    {node.get('name'): [str(_id) for _id in node.get('children', list())]}
                )
        return osd_host_map

    @trivial_completion
    def create_osds(self, drive_group: DriveGroupSpec):
        self.log.debug(f"Processing DriveGroup {drive_group}")
        ret = []
        drive_group.osd_id_claims = self.find_destroyed_osds()
        self.log.info(f"Found osd claims for drivegroup {drive_group.service_id} -> {drive_group.osd_id_claims}")
        for host, drive_selection in self.prepare_drivegroup(drive_group):
            self.log.info('Applying %s on host %s...' % (drive_group.service_id, host))
            cmd = self.driveselection_to_ceph_volume(drive_group, drive_selection,
                                                     drive_group.osd_id_claims.get(host, []))
            if not cmd:
                self.log.debug("No data_devices, skipping DriveGroup: {}".format(drive_group.service_id))
                continue
            ret_msg = self._create_osd(host, cmd,
                                       replace_osd_ids=drive_group.osd_id_claims.get(host, []))
            ret.append(ret_msg)
        return ", ".join(ret)

    def prepare_drivegroup(self, drive_group: DriveGroupSpec) -> List[Tuple[str, DriveSelection]]:
        # 1) use fn_filter to determine matching_hosts
        matching_hosts = drive_group.placement.pattern_matches_hosts([x for x in self.cache.get_hosts()])
        # 2) Map the inventory to the InventoryHost object
        host_ds_map = []

        # set osd_id_claims

        def _find_inv_for_host(hostname: str, inventory_dict: dict):
            # This is stupid and needs to be loaded with the host
            for _host, _inventory in inventory_dict.items():
                if _host == hostname:
                    return _inventory
            raise OrchestratorError("No inventory found for host: {}".format(hostname))

        # 3) iterate over matching_host and call DriveSelection
        self.log.debug(f"Checking matching hosts -> {matching_hosts}")
        for host in matching_hosts:
            inventory_for_host = _find_inv_for_host(host, self.cache.devices)
            self.log.debug(f"Found inventory for host {inventory_for_host}")
            drive_selection = DriveSelection(drive_group, inventory_for_host)
            self.log.debug(f"Found drive selection {drive_selection}")
            host_ds_map.append((host, drive_selection))
        return host_ds_map

    def driveselection_to_ceph_volume(self, drive_group: DriveGroupSpec,
                                      drive_selection: DriveSelection,
                                      osd_id_claims: Optional[List[str]] = None,
                                      preview: bool = False) -> Optional[str]:
        self.log.debug(f"Translating DriveGroup <{drive_group}> to ceph-volume command")
        cmd: Optional[str] = translate.to_ceph_volume(drive_group, drive_selection, osd_id_claims, preview=preview).run()
        self.log.debug(f"Resulting ceph-volume cmd: {cmd}")
        return cmd

    def preview_drivegroups(self, drive_group_name: Optional[str] = None,
                            dg_specs: Optional[List[DriveGroupSpec]] = None) -> List[Dict[str, Dict[Any, Any]]]:
        # find drivegroups
        if drive_group_name:
            drive_groups = cast(List[DriveGroupSpec],
                                self.spec_store.find(service_name=drive_group_name))
        elif dg_specs:
            drive_groups = dg_specs
        else:
            drive_groups = []
        ret_all = []
        for drive_group in drive_groups:
            drive_group.osd_id_claims = self.find_destroyed_osds()
            self.log.info(f"Found osd claims for drivegroup {drive_group.service_id} -> {drive_group.osd_id_claims}")
            # prepare driveselection
            for host, ds in self.prepare_drivegroup(drive_group):
                cmd = self.driveselection_to_ceph_volume(drive_group, ds,
                                                         drive_group.osd_id_claims.get(host, []), preview=True)
                if not cmd:
                    self.log.debug("No data_devices, skipping DriveGroup: {}".format(drive_group.service_name()))
                    continue
                out, err, code = self._run_ceph_volume_command(host, cmd)
                if out:
                    concat_out = json.loads(" ".join(out))
                    ret_all.append({'data': concat_out, 'drivegroup': drive_group.service_id, 'host': host})
        return ret_all

    def _run_ceph_volume_command(self, host: str, cmd: str) -> Tuple[List[str], List[str], int]:
        self.inventory.assert_host(host)

        # get bootstrap key
        ret, keyring, err = self.check_mon_command({
            'prefix': 'auth get',
            'entity': 'client.bootstrap-osd',
        })

        # generate config
        ret, config, err = self.check_mon_command({
            "prefix": "config generate-minimal-conf",
        })

        j = json.dumps({
            'config': config,
            'keyring': keyring,
        })

        split_cmd = cmd.split(' ')
        _cmd = ['--config-json', '-', '--']
        _cmd.extend(split_cmd)
        out, err, code = self._run_cephadm(
            host, 'osd', 'ceph-volume',
            _cmd,
            stdin=j,
            error_ok=True)
        return out, err, code

    def _create_osd(self, host, cmd, replace_osd_ids=None):
        out, err, code = self._run_ceph_volume_command(host, cmd)

        if code == 1 and ', it is already prepared' in '\n'.join(err):
            # HACK: when we create against an existing LV, ceph-volume
            # returns an error and the above message.  To make this
            # command idempotent, tolerate this "error" and continue.
            self.log.debug('the device was already prepared; continuing')
            code = 0
        if code:
            raise RuntimeError(
                'cephadm exited with an error code: %d, stderr:%s' % (
                    code, '\n'.join(err)))

        # check result
        out, err, code = self._run_cephadm(
            host, 'osd', 'ceph-volume',
            [
                '--',
                'lvm', 'list',
                '--format', 'json',
            ])
        before_osd_uuid_map = self.get_osd_uuid_map(only_up=True)
        osds_elems = json.loads('\n'.join(out))
        fsid = self._cluster_fsid
        osd_uuid_map = self.get_osd_uuid_map()
        created = []
        for osd_id, osds in osds_elems.items():
            for osd in osds:
                if osd['tags']['ceph.cluster_fsid'] != fsid:
                    self.log.debug('mismatched fsid, skipping %s' % osd)
                    continue
                if osd_id in before_osd_uuid_map and osd_id not in replace_osd_ids:
                    # if it exists but is part of the replacement operation, don't skip
                    continue
                if osd_id not in osd_uuid_map:
                    self.log.debug('osd id {} does not exist in cluster'.format(osd_id))
                    continue
                if osd_uuid_map.get(osd_id) != osd['tags']['ceph.osd_fsid']:
                    self.log.debug('mismatched osd uuid (cluster has %s, osd '
                                   'has %s)' % (
                                       osd_uuid_map.get(osd_id),
                                       osd['tags']['ceph.osd_fsid']))
                    continue

                created.append(osd_id)
                self._create_daemon(
                    'osd', osd_id, host,
                    osd_uuid_map=osd_uuid_map)

        if created:
            self.cache.invalidate_host_devices(host)
            return "Created osd(s) %s on host '%s'" % (','.join(created), host)
        else:
            return "Created no osd(s) on host %s; already created?" % host

    def _calc_daemon_deps(self, daemon_type, daemon_id):
        need = {
            'prometheus': ['mgr', 'alertmanager', 'node-exporter'],
            'grafana': ['prometheus'],
            'alertmanager': ['mgr', 'alertmanager'],
        }
        deps = []
        for dep_type in need.get(daemon_type, []):
            for dd in self.cache.get_daemons_by_service(dep_type):
                deps.append(dd.name())
        return sorted(deps)

    def _get_config_and_keyring(self, daemon_type, daemon_id,
                                keyring=None,
                                extra_ceph_config=None):
        # type: (str, str, Optional[str], Optional[str]) -> Dict[str, Any]
        # keyring
        if not keyring:
            if daemon_type == 'mon':
                ename = 'mon.'
            else:
                ename = utils.name_to_config_section(daemon_type + '.' + daemon_id)
            ret, keyring, err = self.check_mon_command({
                'prefix': 'auth get',
                'entity': ename,
            })

        # generate config
        ret, config, err = self.check_mon_command({
            "prefix": "config generate-minimal-conf",
        })
        if extra_ceph_config:
            config += extra_ceph_config

        return {
            'config': config,
            'keyring': keyring,
        }

    def _create_daemon(self, daemon_type, daemon_id, host,
                       keyring=None,
                       extra_args=None, extra_config=None,
                       reconfig=False,
                       osd_uuid_map=None):
        if not extra_args:
            extra_args = []
        if not extra_config:
            extra_config = {}
        name = '%s.%s' % (daemon_type, daemon_id)

        start_time = datetime.datetime.utcnow()
        deps = []  # type: List[str]
        cephadm_config = {} # type: Dict[str, Any]
        if daemon_type == 'prometheus':
            cephadm_config, deps = self._generate_prometheus_config()
            extra_args.extend(['--config-json', '-'])
        elif daemon_type == 'grafana':
            cephadm_config, deps = self._generate_grafana_config()
            extra_args.extend(['--config-json', '-'])
        elif daemon_type == 'nfs':
            cephadm_config, deps = \
                    self._generate_nfs_config(daemon_type, daemon_id, host)
            extra_args.extend(['--config-json', '-'])
        elif daemon_type == 'alertmanager':
            cephadm_config, deps = self._generate_alertmanager_config()
            extra_args.extend(['--config-json', '-'])
        else:
            # Ceph.daemons (mon, mgr, mds, osd, etc)
            cephadm_config = self._get_config_and_keyring(
                    daemon_type, daemon_id,
                    keyring=keyring,
                    extra_ceph_config=extra_config.pop('config', ''))
            if extra_config:
                cephadm_config.update({'files': extra_config})
            extra_args.extend(['--config-json', '-'])

            # osd deployments needs an --osd-uuid arg
            if daemon_type == 'osd':
                if not osd_uuid_map:
                    osd_uuid_map = self.get_osd_uuid_map()
                osd_uuid = osd_uuid_map.get(daemon_id)
                if not osd_uuid:
                    raise OrchestratorError('osd.%d not in osdmap' % daemon_id)
                extra_args.extend(['--osd-fsid', osd_uuid])

        if reconfig:
            extra_args.append('--reconfig')
        if self.allow_ptrace:
            extra_args.append('--allow-ptrace')

        self.log.info('%s daemon %s on %s' % (
            'Reconfiguring' if reconfig else 'Deploying',
            name, host))

        out, err, code = self._run_cephadm(
            host, name, 'deploy',
            [
                '--name', name,
            ] + extra_args,
            stdin=json.dumps(cephadm_config))
        if not code and host in self.cache.daemons:
            # prime cached service state with what we (should have)
            # just created
            sd = orchestrator.DaemonDescription()
            sd.daemon_type = daemon_type
            sd.daemon_id = daemon_id
            sd.hostname = host
            sd.status = 1
            sd.status_desc = 'starting'
            self.cache.add_daemon(host, sd)
        self.cache.invalidate_host_daemons(host)
        self.cache.update_daemon_config_deps(host, name, deps, start_time)
        self.cache.save_host(host)
        return "{} {} on host '{}'".format(
            'Reconfigured' if reconfig else 'Deployed', name, host)

    @async_map_completion
    def _remove_daemons(self, name, host):
        return self._remove_daemon(name, host)

    def _remove_daemon(self, name, host):
        """
        Remove a daemon
        """
        (daemon_type, daemon_id) = name.split('.', 1)
        if daemon_type == 'mon':
            self._check_safe_to_destroy_mon(daemon_id)

            # remove mon from quorum before we destroy the daemon
            self.log.info('Removing monitor %s from monmap...' % name)
            ret, out, err = self.check_mon_command({
                'prefix': 'mon rm',
                'name': daemon_id,
            })

        args = ['--name', name, '--force']
        self.log.info('Removing daemon %s from %s' % (name, host))
        out, err, code = self._run_cephadm(
            host, name, 'rm-daemon', args)
        if not code:
            # remove item from cache
            self.cache.rm_daemon(host, name)
        self.cache.invalidate_host_daemons(host)
        return "Removed {} from host '{}'".format(name, host)

    def _apply_service(self, spec):
        """
        Schedule a service.  Deploy new daemons or remove old ones, depending
        on the target label and count specified in the placement.
        """
        daemon_type = spec.service_type
        service_name = spec.service_name()
        if spec.unmanaged:
            self.log.debug('Skipping unmanaged service %s spec' % service_name)
            return False
        self.log.debug('Applying service %s spec' % service_name)
        create_fns = {
            'mon': self._create_mon,
            'mgr': self._create_mgr,
            'osd': self.create_osds,
            'mds': self._create_mds,
            'rgw': self._create_rgw,
            'rbd-mirror': self._create_rbd_mirror,
            'nfs': self._create_nfs,
            'grafana': self._create_grafana,
            'alertmanager': self._create_alertmanager,
            'prometheus': self._create_prometheus,
            'node-exporter': self._create_node_exporter,
            'crash': self._create_crash,
            'iscsi': self._create_iscsi,
        }
        config_fns = {
            'mds': self._config_mds,
            'rgw': self._config_rgw,
            'nfs': self._config_nfs,
            'iscsi': self._config_iscsi,
        }
        create_func = create_fns.get(daemon_type, None)
        if not create_func:
            self.log.debug('unrecognized service type %s' % daemon_type)
            return False
        config_func = config_fns.get(daemon_type, None)

        daemons = self.cache.get_daemons_by_service(service_name)

        public_network = None
        if daemon_type == 'mon':
            ret, out, err = self.check_mon_command({
                'prefix': 'config get',
                'who': 'mon',
                'key': 'public_network',
            })
            if '/' in out:
                public_network = out.strip()
                self.log.debug('mon public_network is %s' % public_network)

        def matches_network(host):
            # type: (str) -> bool
            if not public_network:
                return False
            # make sure we have 1 or more IPs for that network on that
            # host
            return len(self.cache.networks[host].get(public_network, [])) > 0

        hosts = HostAssignment(
            spec=spec,
            get_hosts_func=self._get_hosts,
            get_daemons_func=self.cache.get_daemons_by_service,
            filter_new_host=matches_network if daemon_type == 'mon' else None,
        ).place()

        r = False

        if daemon_type == 'osd':
            return False if create_func(spec) else True # type: ignore

        # sanity check
        if daemon_type in ['mon', 'mgr'] and len(hosts) < 1:
            self.log.debug('cannot scale mon|mgr below 1 (hosts=%s)' % hosts)
            return False

        # add any?
        did_config = False
        hosts_with_daemons = {d.hostname for d in daemons}
        self.log.debug('hosts with daemons: %s' % hosts_with_daemons)
        for host, network, name in hosts:
            if host not in hosts_with_daemons:
                if not did_config and config_func:
                    config_func(spec)
                    did_config = True
                daemon_id = self.get_unique_name(daemon_type, host, daemons,
                                                 spec.service_id, name)
                self.log.debug('Placing %s.%s on host %s' % (
                    daemon_type, daemon_id, host))
                if daemon_type == 'mon':
                    create_func(daemon_id, host, network)  # type: ignore
                elif daemon_type == 'nfs':
                    create_func(daemon_id, host, spec)  # type: ignore
                else:
                    create_func(daemon_id, host)           # type: ignore

                # add to daemon list so next name(s) will also be unique
                sd = orchestrator.DaemonDescription(
                    hostname=host,
                    daemon_type=daemon_type,
                    daemon_id=daemon_id,
                )
                daemons.append(sd)
                r = True

        # remove any?
        target_hosts = [h.hostname for h in hosts]
        for d in daemons:
            if d.hostname not in target_hosts:
                # NOTE: we are passing the 'force' flag here, which means
                # we can delete a mon instances data.
                self._remove_daemon(d.name(), d.hostname)
                r = True

        return r

    def _apply_all_services(self):
        r = False
        specs = [] # type: List[ServiceSpec]
        for sn, spec in self.spec_store.specs.items():
            specs.append(spec)
        for spec in specs:
            try:
                if self._apply_service(spec):
                    r = True
            except Exception as e:
                self.log.warning('Failed to apply %s spec %s: %s' % (
                    spec.service_name(), spec, e))
        return r

    def _check_pool_exists(self, pool, service_name):
        logger.info(f'Checking pool "{pool}" exists for service {service_name}')
        if not self.rados.pool_exists(pool):
            raise OrchestratorError(f'Cannot find pool "{pool}" for '
                                    f'service {service_name}')

    def _check_daemons(self):
        # get monmap mtime so we can refresh configs when mons change
        monmap = self.get('mon_map')
        last_monmap: Optional[datetime.datetime] = datetime.datetime.strptime(
            monmap['modified'], CEPH_DATEFMT)
        if last_monmap and last_monmap > datetime.datetime.utcnow():
            last_monmap = None   # just in case clocks are skewed

        daemons = self.cache.get_daemons()
        grafanas = []  # type: List[orchestrator.DaemonDescription]
        for dd in daemons:
            # orphan?
            spec = self.spec_store.specs.get(dd.service_name(), None)
            if not spec and dd.daemon_type not in ['mon', 'mgr', 'osd']:
                # (mon and mgr specs should always exist; osds aren't matched
                # to a service spec)
                self.log.info('Removing orphan daemon %s...' % dd.name())
                self._remove_daemon(dd.name(), dd.hostname)

            # ignore unmanaged services
            if not spec or spec.unmanaged:
                continue

            # dependencies?
            if dd.daemon_type == 'grafana':
                # put running instances at the front of the list
                grafanas.insert(0, dd)
            deps = self._calc_daemon_deps(dd.daemon_type, dd.daemon_id)
            last_deps, last_config = self.cache.get_daemon_last_config_deps(
                dd.hostname, dd.name())
            if last_deps is None:
                last_deps = []
            reconfig = False
            if not last_config:
                self.log.info('Reconfiguring %s (unknown last config time)...'% (
                    dd.name()))
                reconfig = True
            elif last_deps != deps:
                self.log.debug('%s deps %s -> %s' % (dd.name(), last_deps,
                                                     deps))
                self.log.info('Reconfiguring %s (dependencies changed)...' % (
                    dd.name()))
                reconfig = True
            elif last_monmap and \
               last_monmap > last_config and \
               dd.daemon_type in CEPH_TYPES:
                self.log.info('Reconfiguring %s (monmap changed)...' % dd.name())
                reconfig = True
            if reconfig:
                self._create_daemon(dd.daemon_type, dd.daemon_id,
                                    dd.hostname, reconfig=True)

        # make sure the dashboard [does not] references grafana
        try:
            current_url = self.get_module_option_ex('dashboard',
                                                    'GRAFANA_API_URL')
            if grafanas:
                host = grafanas[0].hostname
                url = f'https://{self.inventory.get_addr(host)}:3000'
                if current_url != url:
                    self.log.info('Setting dashboard grafana config to %s' % url)
                    self.set_module_option_ex('dashboard', 'GRAFANA_API_URL',
                                              url)
                    # FIXME: is it a signed cert??
        except Exception as e:
            self.log.debug('got exception fetching dashboard grafana state: %s',
                           e)

    def _add_daemon(self, daemon_type, spec,
                    create_func, config_func=None):
        """
        Add (and place) a daemon. Require explicit host placement.  Do not
        schedule, and do not apply the related scheduling limitations.
        """
        self.log.debug('_add_daemon %s spec %s' % (daemon_type, spec.placement))
        if not spec.placement.hosts:
            raise OrchestratorError('must specify host(s) to deploy on')
        count = spec.placement.count or len(spec.placement.hosts)
        daemons = self.cache.get_daemons_by_service(spec.service_name())
        return self._create_daemons(daemon_type, spec, daemons,
                                    spec.placement.hosts, count,
                                    create_func, config_func)

    def _create_daemons(self, daemon_type, spec, daemons,
                        hosts, count,
                        create_func, config_func=None):
        if count > len(hosts):
            raise OrchestratorError('too few hosts: want %d, have %s' % (
                count, hosts))

        if config_func:
            config_func(spec)

        args = []  # type: List[tuple]
        for host, network, name in hosts:
            daemon_id = self.get_unique_name(daemon_type, host, daemons,
                                             spec.service_id, name)
            self.log.debug('Placing %s.%s on host %s' % (
                daemon_type, daemon_id, host))
            if daemon_type == 'mon':
                args.append((daemon_id, host, network))  # type: ignore
            elif daemon_type == 'nfs':
                args.append((daemon_id, host, spec)) # type: ignore
            elif daemon_type == 'iscsi':
                args.append((daemon_id, host, spec))  # type: ignore
            else:
                args.append((daemon_id, host))  # type: ignore

            # add to daemon list so next name(s) will also be unique
            sd = orchestrator.DaemonDescription(
                hostname=host,
                daemon_type=daemon_type,
                daemon_id=daemon_id,
            )
            daemons.append(sd)

        @async_map_completion
        def create_func_map(*args):
            return create_func(*args)

        return create_func_map(args)

    @trivial_completion
    def apply_mon(self, spec):
        return self._apply(spec)

    def _create_mon(self, name, host, network):
        """
        Create a new monitor on the given host.
        """
        # get mon. key
        ret, keyring, err = self.check_mon_command({
            'prefix': 'auth get',
            'entity': 'mon.',
        })

        extra_config = '[mon.%s]\n' % name
        if network:
            # infer whether this is a CIDR network, addrvec, or plain IP
            if '/' in network:
                extra_config += 'public network = %s\n' % network
            elif network.startswith('[v') and network.endswith(']'):
                extra_config += 'public addrv = %s\n' % network
            elif ':' not in network:
                extra_config += 'public addr = %s\n' % network
            else:
                raise OrchestratorError('Must specify a CIDR network, ceph addrvec, or plain IP: \'%s\'' % network)
        else:
            # try to get the public_network from the config
            ret, network, err = self.check_mon_command({
                'prefix': 'config get',
                'who': 'mon',
                'key': 'public_network',
            })
            network = network.strip() # type: ignore
            if not network:
                raise OrchestratorError('Must set public_network config option or specify a CIDR network, ceph addrvec, or plain IP')
            if '/' not in network:
                raise OrchestratorError('public_network is set but does not look like a CIDR network: \'%s\'' % network)
            extra_config += 'public network = %s\n' % network

        return self._create_daemon('mon', name, host,
                                   keyring=keyring,
                                   extra_config={'config': extra_config})

    def add_mon(self, spec):
        # type: (ServiceSpec) -> orchestrator.Completion
        return self._add_daemon('mon', spec, self._create_mon)

    def _create_mgr(self, mgr_id, host):
        """
        Create a new manager instance on a host.
        """
        # get mgr. key
        ret, keyring, err = self.check_mon_command({
            'prefix': 'auth get-or-create',
            'entity': 'mgr.%s' % mgr_id,
            'caps': ['mon', 'profile mgr',
                     'osd', 'allow *',
                     'mds', 'allow *'],
        })

        return self._create_daemon('mgr', mgr_id, host, keyring=keyring)

    def add_mgr(self, spec):
        # type: (ServiceSpec) -> orchestrator.Completion
        return self._add_daemon('mgr', spec, self._create_mgr)

    def _apply(self, spec: ServiceSpec) -> str:
        if spec.placement.is_empty():
            # fill in default placement
            defaults = {
                'mon': PlacementSpec(count=5),
                'mgr': PlacementSpec(count=2),
                'mds': PlacementSpec(count=2),
                'rgw': PlacementSpec(count=2),
                'iscsi': PlacementSpec(count=1),
                'rbd-mirror': PlacementSpec(count=2),
                'nfs': PlacementSpec(count=1),
                'grafana': PlacementSpec(count=1),
                'alertmanager': PlacementSpec(count=1),
                'prometheus': PlacementSpec(count=1),
                'node-exporter': PlacementSpec(host_pattern='*'),
                'crash': PlacementSpec(host_pattern='*'),
            }
            spec.placement = defaults[spec.service_type]
        elif spec.service_type in ['mon', 'mgr'] and \
             spec.placement.count is not None and \
             spec.placement.count < 1:
            raise OrchestratorError('cannot scale %s service below 1' % (
                spec.service_type))

        HostAssignment(
            spec=spec,
            get_hosts_func=self._get_hosts,
            get_daemons_func=self.cache.get_daemons_by_service,
        ).validate()

        self.log.info('Saving service %s spec with placement %s' % (
            spec.service_name(), spec.placement.pretty_str()))
        self.spec_store.save(spec)
        self._kick_serve_loop()
        return "Scheduled %s update..." % spec.service_name()

    @trivial_completion
    def apply(self, specs: List[ServiceSpec]):
        return [self._apply(spec) for spec in specs]

    @trivial_completion
    def apply_mgr(self, spec):
        return self._apply(spec)

    def add_mds(self, spec: ServiceSpec):
        return self._add_daemon('mds', spec, self._create_mds, self._config_mds)

    @trivial_completion
    def apply_mds(self, spec: ServiceSpec):
        return self._apply(spec)

    def _config_mds(self, spec):
        # ensure mds_join_fs is set for these daemons
        assert spec.service_id
        ret, out, err = self.check_mon_command({
            'prefix': 'config set',
            'who': 'mds.' + spec.service_id,
            'name': 'mds_join_fs',
            'value': spec.service_id,
        })

    def _create_mds(self, mds_id, host):
        # get mgr. key
        ret, keyring, err = self.check_mon_command({
            'prefix': 'auth get-or-create',
            'entity': 'mds.' + mds_id,
            'caps': ['mon', 'profile mds',
                     'osd', 'allow rwx',
                     'mds', 'allow'],
        })
        return self._create_daemon('mds', mds_id, host, keyring=keyring)

    def add_rgw(self, spec):
        return self._add_daemon('rgw', spec, self._create_rgw, self._config_rgw)

    def _config_rgw(self, spec):
        # ensure rgw_realm and rgw_zone is set for these daemons
        ret, out, err = self.check_mon_command({
            'prefix': 'config set',
            'who': f"{utils.name_to_config_section('rgw')}.{spec.service_id}",
            'name': 'rgw_zone',
            'value': spec.rgw_zone,
        })
        ret, out, err = self.check_mon_command({
            'prefix': 'config set',
            'who': f"{utils.name_to_config_section('rgw')}.{spec.rgw_realm}",
            'name': 'rgw_realm',
            'value': spec.rgw_realm,
        })
        ret, out, err = self.check_mon_command({
            'prefix': 'config set',
            'who': f"{utils.name_to_config_section('rgw')}.{spec.service_id}",
            'name': 'rgw_frontends',
            'value': spec.rgw_frontends_config_value(),
        })

        if spec.rgw_frontend_ssl_certificate:
            if isinstance(spec.rgw_frontend_ssl_certificate, list):
                cert_data = '\n'.join(spec.rgw_frontend_ssl_certificate)
            else:
                cert_data = spec.rgw_frontend_ssl_certificate
            ret, out, err = self.check_mon_command({
                'prefix': 'config-key set',
                'key': f'rgw/cert/{spec.rgw_realm}/{spec.rgw_zone}.crt',
                'val': cert_data,
            })

        if spec.rgw_frontend_ssl_key:
            if isinstance(spec.rgw_frontend_ssl_key, list):
                key_data = '\n'.join(spec.rgw_frontend_ssl_key)
            else:
                key_data = spec.rgw_frontend_ssl_key
            ret, out, err = self.check_mon_command({
                'prefix': 'config-key set',
                'key': f'rgw/cert/{spec.rgw_realm}/{spec.rgw_zone}.key',
                'val': key_data,
            })

        logger.info('Saving service %s spec with placement %s' % (
            spec.service_name(), spec.placement.pretty_str()))
        self.spec_store.save(spec)

    def _create_rgw(self, rgw_id, host):
        ret, keyring, err = self.check_mon_command({
            'prefix': 'auth get-or-create',
            'entity': f"{utils.name_to_config_section('rgw')}.{rgw_id}",
            'caps': ['mon', 'allow *',
                     'mgr', 'allow rw',
                     'osd', 'allow rwx'],
        })
        return self._create_daemon('rgw', rgw_id, host, keyring=keyring)

    @trivial_completion
    def apply_rgw(self, spec):
        return self._apply(spec)

    def add_iscsi(self, spec):
        # type: (ServiceSpec) -> orchestrator.Completion
        return self._add_daemon('iscsi', spec, self._create_iscsi, self._config_iscsi)

    def _config_iscsi(self, spec):
        self._check_pool_exists(spec.pool, spec.service_name())

        logger.info('Saving service %s spec with placement %s' % (
            spec.service_name(), spec.placement.pretty_str()))
        self.spec_store.save(spec)

    def _create_iscsi(self, igw_id, host, spec):
        ret, keyring, err = self.check_mon_command({
            'prefix': 'auth get-or-create',
            'entity': utils.name_to_config_section('iscsi') + '.' + igw_id,
            'caps': ['mon', 'allow rw',
                     'osd', f'allow rwx pool={spec.pool}'],
        })

        api_secure = 'false' if spec.api_secure is None else spec.api_secure
        igw_conf = f"""
# generated by cephadm
[config]
cluster_client_name = {utils.name_to_config_section('iscsi')}.{igw_id}
pool = {spec.pool}
trusted_ip_list = {spec.trusted_ip_list or ''}
minimum_gateways = 1
fqdn_enabled = {spec.fqdn_enabled or ''}
api_port = {spec.api_port or ''}
api_user = {spec.api_user or ''}
api_password = {spec.api_password or ''}
api_secure = {api_secure}
"""
        extra_config = {'iscsi-gateway.cfg': igw_conf}
        return self._create_daemon('iscsi', igw_id, host, keyring=keyring,
                                   extra_config=extra_config)

    @trivial_completion
    def apply_iscsi(self, spec):
        return self._apply(spec)

    def add_rbd_mirror(self, spec):
        return self._add_daemon('rbd-mirror', spec, self._create_rbd_mirror)

    def _create_rbd_mirror(self, daemon_id, host):
        ret, keyring, err = self.check_mon_command({
            'prefix': 'auth get-or-create',
            'entity': 'client.rbd-mirror.' + daemon_id,
            'caps': ['mon', 'profile rbd-mirror',
                     'osd', 'profile rbd'],
        })
        return self._create_daemon('rbd-mirror', daemon_id, host,
                                   keyring=keyring)

    @trivial_completion
    def apply_rbd_mirror(self, spec):
        return self._apply(spec)

    def _generate_nfs_config(self, daemon_type, daemon_id, host):
        # type: (str, str, str) -> Tuple[Dict[str, Any], List[str]]
        deps = [] # type: List[str]

        # find the matching NFSServiceSpec
        # TODO: find the spec and pass via _create_daemon instead ??
        service_name = self.get_service_name(daemon_type, daemon_id, host)
        specs = self.spec_store.find(service_name)
        if not specs:
            raise OrchestratorError('Cannot find service spec %s' % (service_name))
        elif len(specs) > 1:
            raise OrchestratorError('Found multiple service specs for %s' % (service_name))
        else:
            # cast to keep mypy happy
            spec = cast(NFSServiceSpec, specs[0])

        nfs = NFSGanesha(self, daemon_id, spec)

        # create the keyring
        entity = nfs.get_keyring_entity()
        keyring = nfs.get_or_create_keyring(entity=entity)

        # update the caps after get-or-create, the keyring might already exist!
        nfs.update_keyring_caps(entity=entity)

        # create the rados config object
        nfs.create_rados_config_obj()

        # generate the cephadm config
        cephadm_config = nfs.get_cephadm_config()
        cephadm_config.update(
                self._get_config_and_keyring(
                    daemon_type, daemon_id,
                    keyring=keyring))

        return cephadm_config, deps

    def add_nfs(self, spec):
        return self._add_daemon('nfs', spec, self._create_nfs, self._config_nfs)

    def _config_nfs(self, spec):
        self._check_pool_exists(spec.pool, spec.service_name())

        logger.info('Saving service %s spec with placement %s' % (
            spec.service_name(), spec.placement.pretty_str()))
        self.spec_store.save(spec)

    def _create_nfs(self, daemon_id, host, spec):
        return self._create_daemon('nfs', daemon_id, host)

    @trivial_completion
    def apply_nfs(self, spec):
        return self._apply(spec)

    def _generate_prometheus_config(self):
        # type: () -> Tuple[Dict[str, Any], List[str]]
        deps = []  # type: List[str]

        # scrape mgrs
        mgr_scrape_list = []
        mgr_map = self.get('mgr_map')
        port = None
        t = mgr_map.get('services', {}).get('prometheus', None)
        if t:
            t = t.split('/')[2]
            mgr_scrape_list.append(t)
            port = '9283'
            if ':' in t:
                port = t.split(':')[1]
        # scan all mgrs to generate deps and to get standbys too.
        # assume that they are all on the same port as the active mgr.
        for dd in self.cache.get_daemons_by_service('mgr'):
            # we consider the mgr a dep even if the prometheus module is
            # disabled in order to be consistent with _calc_daemon_deps().
            deps.append(dd.name())
            if not port:
                continue
            if dd.daemon_id == self.get_mgr_id():
                continue
            addr = self.inventory.get_addr(dd.hostname)
            mgr_scrape_list.append(addr.split(':')[0] + ':' + port)

        # scrape node exporters
        node_configs = ''
        for dd in self.cache.get_daemons_by_service('node-exporter'):
            deps.append(dd.name())
            addr = self.inventory.get_addr(dd.hostname)
            if not node_configs:
                node_configs = """
  - job_name: 'node'
    static_configs:
"""
            node_configs += """    - targets: {}
      labels:
        instance: '{}'
""".format([addr.split(':')[0] + ':9100'],
           dd.hostname)

        # scrape alert managers
        alertmgr_configs = ""
        alertmgr_targets = []
        for dd in self.cache.get_daemons_by_service('alertmanager'):
            deps.append(dd.name())
            addr = self.inventory.get_addr(dd.hostname)
            alertmgr_targets.append("'{}:9093'".format(addr.split(':')[0]))
        if alertmgr_targets:
            alertmgr_configs = """alerting:
  alertmanagers:
    - scheme: http
      path_prefix: /alertmanager
      static_configs:
        - targets: [{}]
""".format(", ".join(alertmgr_targets))

        # generate the prometheus configuration
        r = {
            'files': {
                'prometheus.yml': """# generated by cephadm
global:
  scrape_interval: 5s
  evaluation_interval: 10s
rule_files:
  - /etc/prometheus/alerting/*
{alertmgr_configs}
scrape_configs:
  - job_name: 'ceph'
    static_configs:
    - targets: {mgr_scrape_list}
      labels:
        instance: 'ceph_cluster'
{node_configs}
""".format(
    mgr_scrape_list=str(mgr_scrape_list),
    node_configs=str(node_configs),
    alertmgr_configs=str(alertmgr_configs)
    ),
            },
        }

        # include alerts, if present in the container
        if os.path.exists(self.prometheus_alerts_path):
            with open(self.prometheus_alerts_path, "r") as f:
                alerts = f.read()
            r['files']['/etc/prometheus/alerting/ceph_alerts.yml'] = alerts

        return r, sorted(deps)

    def _generate_grafana_config(self):
        # type: () -> Tuple[Dict[str, Any], List[str]]
        deps = []  # type: List[str]
        def generate_grafana_ds_config(hosts: List[str]) -> str:
            config = '''# generated by cephadm
deleteDatasources:
{delete_data_sources}

datasources:
{data_sources}
'''
            delete_ds_template = '''
  - name: '{name}'
    orgId: 1\n'''.lstrip('\n')
            ds_template = '''
  - name: '{name}'
    type: 'prometheus'
    access: 'proxy'
    orgId: 1
    url: 'http://{host}:9095'
    basicAuth: false
    isDefault: {is_default}
    editable: false\n'''.lstrip('\n')

            delete_data_sources = ''
            data_sources = ''
            for i, host in enumerate(hosts):
                name = "Dashboard %d" % (i + 1)
                data_sources += ds_template.format(
                    name=name,
                    host=host,
                    is_default=str(i == 0).lower()
                )
                delete_data_sources += delete_ds_template.format(
                    name=name
                )
            return config.format(
                delete_data_sources=delete_data_sources,
                data_sources=data_sources,
            )

        prom_services = []  # type: List[str]
        for dd in self.cache.get_daemons_by_service('prometheus'):
            prom_services.append(dd.hostname)
            deps.append(dd.name())

        cert = self.get_store('grafana_crt')
        pkey = self.get_store('grafana_key')
        if cert and pkey:
            try:
                verify_tls(cert, pkey)
            except ServerConfigException as e:
                logger.warning('Provided grafana TLS certificates invalid: %s', str(e))
                cert, pkey = None, None
        if not (cert and pkey):
            cert, pkey = create_self_signed_cert('Ceph', 'cephadm')
            self.set_store('grafana_crt', cert)
            self.set_store('grafana_key', pkey)
            self.check_mon_command({
                'prefix': 'dashboard set-grafana-api-ssl-verify',
                'value': 'false',
            })



        config_file = {
            'files': {
                "grafana.ini": """# generated by cephadm
[users]
  default_theme = light
[auth.anonymous]
  enabled = true
  org_name = 'Main Org.'
  org_role = 'Viewer'
[server]
  domain = 'bootstrap.storage.lab'
  protocol = https
  cert_file = /etc/grafana/certs/cert_file
  cert_key = /etc/grafana/certs/cert_key
  http_port = 3000
[security]
  admin_user = admin
  admin_password = admin
  allow_embedding = true
""",
                'provisioning/datasources/ceph-dashboard.yml': generate_grafana_ds_config(prom_services),
                'certs/cert_file': '# generated by cephadm\n%s' % cert,
                'certs/cert_key': '# generated by cephadm\n%s' % pkey,
            }
        }
        return config_file, sorted(deps)

    def _get_dashboard_url(self):
        # type: () -> str
        return self.get('mgr_map').get('services', {}).get('dashboard', '')

    def _generate_alertmanager_config(self):
        # type: () -> Tuple[Dict[str, Any], List[str]]
        deps = [] # type: List[str]

        # dashboard(s)
        dashboard_urls = []
        mgr_map = self.get('mgr_map')
        port = None
        proto = None  # http: or https:
        url = mgr_map.get('services', {}).get('dashboard', None)
        if url:
            dashboard_urls.append(url)
            proto = url.split('/')[0]
            port = url.split('/')[2].split(':')[1]
        # scan all mgrs to generate deps and to get standbys too.
        # assume that they are all on the same port as the active mgr.
        for dd in self.cache.get_daemons_by_service('mgr'):
            # we consider mgr a dep even if the dashboard is disabled
            # in order to be consistent with _calc_daemon_deps().
            deps.append(dd.name())
            if not port:
                continue
            if dd.daemon_id == self.get_mgr_id():
                continue
            addr = self.inventory.get_addr(dd.hostname)
            dashboard_urls.append('%s//%s:%s/' % (proto, addr.split(':')[0],
                                                 port))

        yml = """# generated by cephadm
# See https://prometheus.io/docs/alerting/configuration/ for documentation.

global:
  resolve_timeout: 5m

route:
  group_by: ['alertname']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 1h
  receiver: 'ceph-dashboard'
receivers:
- name: 'ceph-dashboard'
  webhook_configs:
{urls}
""".format(
    urls='\n'.join(
        ["  - url: '{}api/prometheus_receiver'".format(u)
         for u in dashboard_urls]
    ))
        peers = []
        port = '9094'
        for dd in self.cache.get_daemons_by_service('alertmanager'):
            deps.append(dd.name())
            addr = self.inventory.get_addr(dd.hostname)
            peers.append(addr.split(':')[0] + ':' + port)
        return {
            "files": {
                "alertmanager.yml": yml
            },
            "peers": peers
        }, sorted(deps)

    def add_prometheus(self, spec):
        return self._add_daemon('prometheus', spec, self._create_prometheus)

    def _create_prometheus(self, daemon_id, host):
        return self._create_daemon('prometheus', daemon_id, host)

    @trivial_completion
    def apply_prometheus(self, spec):
        return self._apply(spec)

    def add_node_exporter(self, spec):
        # type: (ServiceSpec) -> AsyncCompletion
        return self._add_daemon('node-exporter', spec,
                                self._create_node_exporter)

    @trivial_completion
    def apply_node_exporter(self, spec):
        return self._apply(spec)

    def _create_node_exporter(self, daemon_id, host):
        return self._create_daemon('node-exporter', daemon_id, host)

    def add_crash(self, spec):
        # type: (ServiceSpec) -> AsyncCompletion
        return self._add_daemon('crash', spec,
                                self._create_crash)

    @trivial_completion
    def apply_crash(self, spec):
        return self._apply(spec)

    def _create_crash(self, daemon_id, host):
        ret, keyring, err = self.check_mon_command({
            'prefix': 'auth get-or-create',
            'entity': 'client.crash.' + host,
            'caps': ['mon', 'profile crash',
                     'mgr', 'profile crash'],
        })
        return self._create_daemon('crash', daemon_id, host, keyring=keyring)

    def add_grafana(self, spec):
        # type: (ServiceSpec) -> AsyncCompletion
        return self._add_daemon('grafana', spec, self._create_grafana)

    @trivial_completion
    def apply_grafana(self, spec: ServiceSpec):
        return self._apply(spec)

    def _create_grafana(self, daemon_id, host):
        # type: (str, str) -> str
        return self._create_daemon('grafana', daemon_id, host)

    def add_alertmanager(self, spec):
        # type: (ServiceSpec) -> AsyncCompletion
        return self._add_daemon('alertmanager', spec, self._create_alertmanager)

    @trivial_completion
    def apply_alertmanager(self, spec: ServiceSpec):
        return self._apply(spec)

    def _create_alertmanager(self, daemon_id, host):
        return self._create_daemon('alertmanager', daemon_id, host)


    def _get_container_image_id(self, image_name):
        # pick a random host...
        host = None
        for host_name in self.inventory.keys():
            host = host_name
            break
        if not host:
            raise OrchestratorError('no hosts defined')
        out, err, code = self._run_cephadm(
            host, None, 'pull', [],
            image=image_name,
            no_fsid=True,
            error_ok=True)
        if code:
            raise OrchestratorError('Failed to pull %s on %s: %s' % (
                image_name, host, '\n'.join(out)))
        j = json.loads('\n'.join(out))
        image_id = j.get('image_id')
        ceph_version = j.get('ceph_version')
        self.log.debug('image %s -> id %s version %s' %
                       (image_name, image_id, ceph_version))
        return image_id, ceph_version

    @trivial_completion
    def upgrade_check(self, image, version):
        if version:
            target_name = self.container_image_base + ':v' + version
        elif image:
            target_name = image
        else:
            raise OrchestratorError('must specify either image or version')

        target_id, target_version = self._get_container_image_id(target_name)
        self.log.debug('Target image %s id %s version %s' % (
            target_name, target_id, target_version))
        r = {
            'target_name': target_name,
            'target_id': target_id,
            'target_version': target_version,
            'needs_update': dict(),
            'up_to_date': list(),
        }
        for host, dm in self.cache.daemons.items():
            for name, dd in dm.items():
                if target_id == dd.container_image_id:
                    r['up_to_date'].append(dd.name())
                else:
                    r['needs_update'][dd.name()] = {
                        'current_name': dd.container_image_name,
                        'current_id': dd.container_image_id,
                        'current_version': dd.version,
                    }
        return json.dumps(r, indent=4, sort_keys=True)

    @trivial_completion
    def upgrade_status(self):
        r = orchestrator.UpgradeStatusSpec()
        if self.upgrade_state:
            r.target_image = self.upgrade_state.get('target_name')
            r.in_progress = True
            if self.upgrade_state.get('error'):
                r.message = 'Error: ' + self.upgrade_state.get('error')
            elif self.upgrade_state.get('paused'):
                r.message = 'Upgrade paused'
        return r

    @trivial_completion
    def upgrade_start(self, image, version):
        if self.mode != 'root':
            raise OrchestratorError('upgrade is not supported in %s mode' % (
                self.mode))
        if version:
            try:
                (major, minor, patch) = version.split('.')
                assert int(minor) >= 0
                assert int(patch) >= 0
            except:
                raise OrchestratorError('version must be in the form X.Y.Z (e.g., 15.2.3)')
            if int(major) < 15 or (int(major) == 15 and int(minor) < 2):
                raise OrchestratorError('cephadm only supports octopus (15.2.0) or later')
            target_name = self.container_image_base + ':v' + version
        elif image:
            target_name = image
        else:
            raise OrchestratorError('must specify either image or version')
        if self.upgrade_state:
            if self.upgrade_state.get('target_name') != target_name:
                raise OrchestratorError(
                    'Upgrade to %s (not %s) already in progress' %
                (self.upgrade_state.get('target_name'), target_name))
            if self.upgrade_state.get('paused'):
                del self.upgrade_state['paused']
                self._save_upgrade_state()
                return 'Resumed upgrade to %s' % self.upgrade_state.get('target_name')
            return 'Upgrade to %s in progress' % self.upgrade_state.get('target_name')
        self.upgrade_state = {
            'target_name': target_name,
            'progress_id': str(uuid.uuid4()),
        }
        self._update_upgrade_progress(0.0)
        self._save_upgrade_state()
        self._clear_upgrade_health_checks()
        self.event.set()
        return 'Initiating upgrade to %s' % (target_name)

    @trivial_completion
    def upgrade_pause(self):
        if not self.upgrade_state:
            raise OrchestratorError('No upgrade in progress')
        if self.upgrade_state.get('paused'):
            return 'Upgrade to %s already paused' % self.upgrade_state.get('target_name')
        self.upgrade_state['paused'] = True
        self._save_upgrade_state()
        return 'Paused upgrade to %s' % self.upgrade_state.get('target_name')

    @trivial_completion
    def upgrade_resume(self):
        if not self.upgrade_state:
            raise OrchestratorError('No upgrade in progress')
        if not self.upgrade_state.get('paused'):
            return 'Upgrade to %s not paused' % self.upgrade_state.get('target_name')
        del self.upgrade_state['paused']
        self._save_upgrade_state()
        self.event.set()
        return 'Resumed upgrade to %s' % self.upgrade_state.get('target_name')

    @trivial_completion
    def upgrade_stop(self):
        if not self.upgrade_state:
            return 'No upgrade in progress'
        target_name = self.upgrade_state.get('target_name')
        if 'progress_id' in self.upgrade_state:
            self.remote('progress', 'complete',
                        self.upgrade_state['progress_id'])
        self.upgrade_state = None
        self._save_upgrade_state()
        self._clear_upgrade_health_checks()
        self.event.set()
        return 'Stopped upgrade to %s' % target_name

    @trivial_completion
    def remove_osds(self, osd_ids: List[str],
                    replace: bool = False,
                    force: bool = False):
        """
        Takes a list of OSDs and schedules them for removal.
        The function that takes care of the actual removal is
        _remove_osds_bg().
        """

        daemons = self.cache.get_daemons_by_service('osd')
        found: Set[OSDRemoval] = set()
        for daemon in daemons:
            if daemon.daemon_id not in osd_ids:
                continue
            found.add(OSDRemoval(daemon.daemon_id, replace, force,
                                 daemon.hostname, daemon.name(),
                                 datetime.datetime.utcnow(), -1))

        not_found = {osd_id for osd_id in osd_ids if osd_id not in [x.osd_id for x in found]}
        if not_found:
            raise OrchestratorError('Unable to find OSD: %s' % not_found)

        self.rm_util.queue_osds_for_removal(found)

        # trigger the serve loop to initiate the removal
        self._kick_serve_loop()
        return "Scheduled OSD(s) for removal"

    @trivial_completion
    def remove_osds_status(self):
        """
        The CLI call to retrieve an osd removal report
        """
        return self.rm_util.report


class BaseScheduler(object):
    """
    Base Scheduler Interface

    * requires a placement_spec

    `place(host_pool)` needs to return a List[HostPlacementSpec, ..]
    """

    def __init__(self, placement_spec):
        # type: (PlacementSpec) -> None
        self.placement_spec = placement_spec

    def place(self, host_pool, count=None):
        # type: (List, Optional[int]) -> List[HostPlacementSpec]
        raise NotImplementedError


class SimpleScheduler(BaseScheduler):
    """
    The most simple way to pick/schedule a set of hosts.
    1) Shuffle the provided host_pool
    2) Select from list up to :count
    """
    def __init__(self, placement_spec):
        super(SimpleScheduler, self).__init__(placement_spec)

    def place(self, host_pool, count=None):
        # type: (List, Optional[int]) -> List[HostPlacementSpec]
        if not host_pool:
            return []
        host_pool = [x for x in host_pool]
        # shuffle for pseudo random selection
        random.shuffle(host_pool)
        return host_pool[:count]


class HostAssignment(object):
    """
    A class to detect if hosts are being passed imperative or declarative
    If the spec is populated via the `hosts/hosts` field it will not load
    any hosts into the list.
    If the spec isn't populated, i.e. when only num or label is present (declarative)
    it will use the provided `get_host_func` to load it from the inventory.

    Schedulers can be assigned to pick hosts from the pool.
    """

    def __init__(self,
                 spec,  # type: ServiceSpec
                 get_hosts_func,  # type: Callable[[Optional[str]],List[str]]
                 get_daemons_func, # type: Callable[[str],List[orchestrator.DaemonDescription]]

                 filter_new_host=None, # type: Optional[Callable[[str],bool]]
                 scheduler=None,  # type: Optional[BaseScheduler]
                 ):
        assert spec and get_hosts_func and get_daemons_func
        self.spec = spec  # type: ServiceSpec
        self.scheduler = scheduler if scheduler else SimpleScheduler(self.spec.placement)
        self.get_hosts_func = get_hosts_func
        self.get_daemons_func = get_daemons_func
        self.filter_new_host = filter_new_host
        self.service_name = spec.service_name()


    def validate(self):
        self.spec.validate()

        if self.spec.placement.hosts:
            explicit_hostnames = {h.hostname for h in self.spec.placement.hosts}
            unknown_hosts = explicit_hostnames.difference(set(self.get_hosts_func(None)))
            if unknown_hosts:
                raise OrchestratorValidationError(
                    f'Cannot place {self.spec.one_line_str()} on {unknown_hosts}: Unknown hosts')

        if self.spec.placement.host_pattern:
            pattern_hostnames = self.spec.placement.pattern_matches_hosts(self.get_hosts_func(None))
            if not pattern_hostnames:
                raise OrchestratorValidationError(
                    f'Cannot place {self.spec.one_line_str()}: No matching hosts')

        if self.spec.placement.label:
            label_hostnames = self.get_hosts_func(self.spec.placement.label)
            if not label_hostnames:
                raise OrchestratorValidationError(
                    f'Cannot place {self.spec.one_line_str()}: No matching '
                    f'hosts for label {self.spec.placement.label}')

    def place(self):
        # type: () -> List[HostPlacementSpec]
        """
        Load hosts into the spec.placement.hosts container.
        """

        self.validate()

        # count == 0
        if self.spec.placement.count == 0:
            return []

        # respect any explicit host list
        if self.spec.placement.hosts and not self.spec.placement.count:
            logger.debug('Provided hosts: %s' % self.spec.placement.hosts)
            return self.spec.placement.hosts

        # respect host_pattern
        if self.spec.placement.host_pattern:
            candidates = [
                HostPlacementSpec(x, '', '')
                for x in self.spec.placement.pattern_matches_hosts(self.get_hosts_func(None))
            ]
            logger.debug('All hosts: {}'.format(candidates))
            return candidates

        count = 0
        if self.spec.placement.hosts and \
           self.spec.placement.count and \
           len(self.spec.placement.hosts) >= self.spec.placement.count:
            hosts = self.spec.placement.hosts
            logger.debug('place %d over provided host list: %s' % (
                count, hosts))
            count = self.spec.placement.count
        elif self.spec.placement.label:
            hosts = [
                HostPlacementSpec(x, '', '')
                for x in self.get_hosts_func(self.spec.placement.label)
            ]
            if not self.spec.placement.count:
                logger.debug('Labeled hosts: {}'.format(hosts))
                return hosts
            count = self.spec.placement.count
            logger.debug('place %d over label %s: %s' % (
                count, self.spec.placement.label, hosts))
        else:
            hosts = [
                HostPlacementSpec(x, '', '')
                for x in self.get_hosts_func(None)
            ]
            if self.spec.placement.count:
                count = self.spec.placement.count
            else:
                # this should be a totally empty spec given all of the
                # alternative paths above.
                assert self.spec.placement.count is None
                assert not self.spec.placement.hosts
                assert not self.spec.placement.label
                count = 1
            logger.debug('place %d over all hosts: %s' % (count, hosts))

        # we need to select a subset of the candidates

        # if a partial host list is provided, always start with that
        if len(self.spec.placement.hosts) < count:
            chosen = self.spec.placement.hosts
        else:
            chosen = []

        # prefer hosts that already have services
        daemons = self.get_daemons_func(self.service_name)
        hosts_with_daemons = {d.hostname for d in daemons}
        # calc existing daemons (that aren't already in chosen)
        chosen_hosts = [hs.hostname for hs in chosen]
        existing = [hs for hs in hosts
                    if hs.hostname in hosts_with_daemons and \
                    hs.hostname not in chosen_hosts]
        if len(chosen + existing) >= count:
            chosen = chosen + self.scheduler.place(
                existing,
                count - len(chosen))
            logger.debug('Hosts with existing daemons: {}'.format(chosen))
            return chosen

        need = count - len(existing + chosen)
        others = [hs for hs in hosts
                  if hs.hostname not in hosts_with_daemons]
        if self.filter_new_host:
            old = others
            others = [h for h in others if self.filter_new_host(h.hostname)]
            logger.debug('filtered %s down to %s' % (old, hosts))
        chosen = chosen + self.scheduler.place(others, need)
        logger.debug('Combine hosts with existing daemons %s + new hosts %s' % (
            existing, chosen))
        return existing + chosen
