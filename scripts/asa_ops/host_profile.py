"""Explicit host-only authority; never adapts lifecycle UID guards implicitly.

The root-owned installation stays private. A different unprivileged service UID
owns only the operation store and talks to the existing root executor. That
executor independently re-reads the authoritative installation identity.
"""
import os
from pathlib import Path
import re
import socket
import stat

from .maintenance import validate_service_state
from .store import OpsError

HOST_ONLY = 'host-admin-only'
LIFECYCLE = 'lifecycle'
HOST_TOOLS = frozenset({'asa_host_inventory', 'asa_admin_status',
                        'asa_admin_execute', 'asa_operation_get',
                        'asa_operation_list'})


def require(value, code):
    if not value:
        raise OpsError(code, code)


def mode(config):
    value = config.get('operations_mode', LIFECYCLE)
    require(value in (LIFECYCLE, HOST_ONLY), 'OPERATIONS_MODE')
    return value


def host_only(config):
    return mode(config) == HOST_ONLY


def service_uid(config):
    if not host_only(config):
        return config['installation']['owner_uid']
    uid = config.get('service_uid')
    require(type(uid) is int and uid > 0, 'HOST_SERVICE_UID')
    return uid


def allowed(config, name):
    if host_only(config) and name not in HOST_TOOLS:
        raise OpsError('CAPABILITY_DISABLED', 'This host-only profile does not expose ASA lifecycle or cloud operations')


def trusted_metadata(path, *, directory=False):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, 'HOST_PATH_SYMLINK')
    st = path.lstat()
    require((stat.S_ISDIR if directory else stat.S_ISREG)(st.st_mode)
            and st.st_uid == 0 and not st.st_mode & 0o022, 'HOST_PATH_TRUST')
    for parent in path.parents:
        st = parent.lstat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == 0
                and not st.st_mode & 0o022, 'HOST_PARENT_TRUST')


def validate(config, config_path, *, runtime=False):
    """Host-only validation, without reading the root-private application state."""
    require(host_only(config), 'OPERATIONS_MODE')
    require(config.get('host_admin_enabled') is True, 'HOST_ADMIN_NOT_ENABLED')
    require(config.get('cloud_storage_enabled', False) is False, 'HOST_CLOUD_FORBIDDEN')
    require(config.get('hostname') == socket.gethostname(), 'HOST_IDENTITY')
    uid = service_uid(config)
    if runtime:
        require(os.geteuid() == uid, 'HOST_SERVICE_IDENTITY')
    install = config['installation']
    require(install.get('owner_uid') == 0, 'HOST_INSTALLATION_OWNER')
    require(isinstance(install.get('id'), str)
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', install['id']), 'HOST_INSTALLATION_ID')
    require(isinstance(install.get('installation_instance_id'), str)
            and re.fullmatch(r'[a-f0-9]{32}', install['installation_instance_id']), 'HOST_INSTANCE_ID')
    trusted_metadata(config_path)
    trusted_metadata(install['root'], directory=True)
    validate_service_state(install['state_root'], uid)


def executor_identity(config):
    """Fetch only safe identity fields, after the root executor has attested them."""
    from .admin import request
    value = request(config, {'action': 'status'})
    install = config['installation']
    require(value.get('uid') == 0
            and value.get('hostname') == config['hostname']
            and value.get('installation_id') == install['id']
            and value.get('installation_instance_id') == install['installation_instance_id'],
            'HOST_EXECUTOR_IDENTITY')
    return install['installation_instance_id']
