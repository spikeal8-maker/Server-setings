import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import struct
import subprocess
import time

from .storage import require, Stop

PRIMARY_IP = '172.23.104.170'
PRIMARY_NETWORK = ipaddress.ip_network('172.23.104.0/23')
PRIMARY_GATEWAY = '172.23.104.1'
PRIMARY_DEVICE = 'vmbr0'
MANAGEMENT_TABLE, BACKUP_TABLE = 21090, 21091
PRIORITIES = (10000, 10005, 10010, 10020)
PROTOCOL = '186'
PROBES = (('one.one.one.one', '1.1.1.1', 443), ('dns.google', '8.8.8.8', 443))
BMC_IP = '172.23.104.234'
BMC_LEAF_SHA256 = '9fed04db6e87aa12431ec2cd271e5aaaab3ea27da1dbd3e5bea1abdbfdc0c997'
MANAGEMENT_PEER = '172.23.104.191'


class Commands:
    def call(self, *args, check=True):
        result = subprocess.run(list(args), capture_output=True, text=True, timeout=20, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
        require(not check or result.returncode == 0, 'COMMAND_FAILED:' + args[0])
        return result

    def ip(self, *args):
        return self.call('ip', *map(str, args))

    def query(self, *args):
        return json.loads(self.ip('-j', *args).stdout)


def interface_name(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', value), 'INTERFACE_NAME')
    require(value not in ('lo', 'vmbr0', 'nic1'), 'PRIMARY_INTERFACE_FORBIDDEN')
    return value


def lease_from_environment(env, interface):
    require(env.get('interface') == interface_name(interface), 'DHCP_FOREIGN_INTERFACE')
    require(env.get('reason') in ('BOUND', 'RENEW', 'REBIND', 'REBOOT'), 'DHCP_REASON')
    address = ipaddress.IPv4Address(env.get('new_ip_address', ''))
    network = ipaddress.IPv4Network(f"{address}/{env.get('new_subnet_mask', '')}", strict=False)
    require(1 <= network.prefixlen <= 30 and not network.overlaps(PRIMARY_NETWORK), 'DHCP_PRIMARY_OVERLAP')
    routers = env.get('new_routers', '').split()
    dns = env.get('new_domain_name_servers', '').split()
    require(len(routers) == 1 and 1 <= len(dns) <= 3, 'DHCP_ROUTER_DNS_COUNT')
    gateway = ipaddress.IPv4Address(routers[0])
    servers = [ipaddress.IPv4Address(value) for value in dns]
    for value in [address, gateway] + servers:
        require(not (value.is_unspecified or value.is_multicast or value.is_loopback or value.is_link_local or value in PRIMARY_NETWORK), 'DHCP_UNSAFE_ADDRESS')
    require(address not in (network.network_address, network.broadcast_address) and gateway in network and gateway not in (address, network.network_address, network.broadcast_address), 'DHCP_GATEWAY')
    duration = int(env.get('new_dhcp_lease_time', '0'))
    require(60 <= duration <= 604800, 'DHCP_LEASE_TIME')
    return {'interface': interface, 'address': str(address), 'prefix': network.prefixlen, 'network': str(network), 'gateway': str(gateway), 'dns': list(map(str, servers)),
            'expires': time.time() + duration, 'expires_mono': time.monotonic() + duration, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def check_device(commands, interface, sysfs=Path('/sys/class/net')):
    interface_name(interface)
    rows = commands.query('link', 'show', 'dev', interface)
    require(len(rows) == 1 and not rows[0].get('master'), 'INTERFACE_MASTER_OR_MISSING')
    require('UP' not in rows[0].get('flags', []), 'FREE_DEVICE_MUST_BE_DOWN_NO_TAKEOVER')
    require(not (sysfs / interface / 'master').exists(), 'INTERFACE_MASTER')
    require(not addresses(commands, interface), 'INTERFACE_ALREADY_ADDRESSED')
    ipv6 = [a for row in commands.query('-6', 'address', 'show', 'dev', interface) for a in row.get('addr_info', [])]
    require(not any(a.get('scope') != 'link' for a in ipv6), 'FOREIGN_IPV6_ADDRESS')
    require(not any(r.get('dev') == interface and r.get('dst') != 'fe80::/64' for r in commands.query('-6', 'route', 'show', 'table', 'main')), 'FOREIGN_IPV6_ROUTE')
    return {'interface': interface, 'mac': rows[0]['address'], 'ifindex': rows[0]['ifindex'], 'was_up': 'UP' in rows[0].get('flags', [])}


def addresses(commands, interface):
    return [a for row in commands.query('-4', 'address', 'show', 'dev', interface) for a in row.get('addr_info', [])]


def baseline(commands, dns):
    routes = commands.query('-4', 'route', 'show', 'table', 'main')
    require(len(routes) == 2, 'MAIN_ROUTE_COUNT')
    require(any(r.get('dst') == 'default' and r.get('gateway') == PRIMARY_GATEWAY and r.get('dev') == PRIMARY_DEVICE for r in routes), 'PRIMARY_DEFAULT')
    require(any(r.get('dst') == str(PRIMARY_NETWORK) and r.get('dev') == PRIMARY_DEVICE and r.get('prefsrc') == PRIMARY_IP for r in routes), 'PRIMARY_CONNECTED')
    rules = commands.query('-4', 'rule', 'show')
    require([r.get('priority') for r in rules] == [0, 32766, 32767], 'FOREIGN_POLICY_RULES')
    require([str(r.get('table')) for r in rules] == ['local', 'main', 'default'], 'FOREIGN_POLICY_TABLES')
    for table in (MANAGEMENT_TABLE, BACKUP_TABLE):
        # iproute2 returns ENOENT for empty/unregistered numeric tables.
        result = commands.call('ip', '-j', '-4', 'route', 'show', 'table', str(table), check=False)
        require(empty_table(result), 'POLICY_TABLE_NOT_EMPTY')
    snap = dns.snapshot()
    import base64
    nameservers = [line.split()[1:] for line in base64.b64decode(snap['data']).decode().splitlines() if line.strip().startswith('nameserver')]
    require(nameservers == [['10.167.146.13']], 'PRIMARY_DNS_CHANGED')
    return {'routes': routes, 'rules': rules, 'dns': snap}


def empty_table(result):
    if result.returncode == 0:
        return result.stdout.strip() == '[]'
    # iproute2 can emit the opening JSON '[' before ENOENT. Accept only its
    # explicit missing-FIB error, never an arbitrary failed/partial dump.
    return result.returncode == 2 and result.stdout.strip() in ('', '[', '[]') and 'FIB table does not exist' in result.stderr and 'Dump terminated' in result.stderr


def route_configuration(rows):
    # Carrier/cache flags are live state, not a configuration rewrite.
    return [{k: v for k, v in row.items() if k not in ('flags', 'cache', 'expires')} for row in rows]


def route_proof(commands, destination, source, interface, table=None):
    rows = commands.query('-4', 'route', 'get', destination, 'from', source)
    require(len(rows) == 1 and rows[0].get('dev') == interface, 'PROBE_WRONG_DEVICE')
    require(rows[0].get('from', source) == source, 'PROBE_WRONG_SOURCE')
    if table is not None:
        require(str(rows[0].get('table')) == str(table), 'PROBE_WRONG_TABLE')
    return rows[0]


def https_probe(commands, source, interface, table=None, probes=PROBES, cafile=None):
    passed = 0
    for host, address, port in probes:
        try:
            route_proof(commands, address, source, interface, table)
            context = ssl.create_default_context(cafile=cafile)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
                raw.settimeout(4)
                raw.bind((source, 0))
                raw.connect((address, port))
                with context.wrap_socket(raw, server_hostname=host) as stream:
                    stream.sendall(f'GET / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
                    status = stream.recv(256).split(b' ', 2)
                    require(len(status) >= 2 and 200 <= int(status[1]) <= 399, 'HTTPS_STATUS')
            passed += 1
        except (OSError, ValueError, Stop):
            pass
    return passed == len(probes)


def dns_probe(commands, server, source, interface):
    route_proof(commands, server, source, interface, BACKUP_TABLE)
    transaction = int.from_bytes(__import__('os').urandom(2), 'big')
    name = b'\x03www\x06google\x03com\x00'
    query = struct.pack('!6H', transaction, 0x100, 1, 0, 0, 0) + name + struct.pack('!2H', 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(4)
        sock.bind((source, 0))
        sock.connect((server, 53))
        sock.send(query)
        response = sock.recv(4096)
    require(len(response) >= 12, 'DNS_RESPONSE')
    ident, flags, questions, answers, _, _ = struct.unpack('!6H', response[:12])
    require(ident == transaction and flags & 0x8000 and not flags & 0xF and not flags & 0x200 and questions == 1 and 1 <= answers <= 32, 'DNS_RESOLVER_FAILED')
    question = query[12:]
    require(response[12:12 + len(question)] == question, 'DNS_QUESTION_MISMATCH')
    offset, found = 12 + len(question), False
    for _ in range(answers):
        while True:
            require(offset < len(response), 'DNS_TRUNCATED_NAME')
            length = response[offset]
            offset += 1
            if length & 0xC0 == 0xC0:
                require(offset < len(response) and ((length & 0x3F) << 8 | response[offset]) < len(response), 'DNS_BAD_POINTER')
                offset += 1
                break
            require(length <= 63 and offset + length <= len(response), 'DNS_BAD_LABEL')
            offset += length
            if length == 0:
                break
        require(offset + 10 <= len(response), 'DNS_TRUNCATED_ANSWER')
        kind, family, _, size = struct.unpack('!HHIH', response[offset:offset + 10])
        offset += 10
        require(offset + size <= len(response), 'DNS_TRUNCATED_RDATA')
        if kind == 1 and family == 1 and size == 4:
            value = ipaddress.IPv4Address(response[offset:offset + 4])
            found |= not (value.is_unspecified or value.is_multicast or value.is_loopback)
        offset += size
    require(found, 'DNS_NO_USABLE_IPV4_ANSWER')
    return True


def management_gate(commands):
    """Initial qualification only; pinned self-signed BMC is not CA verification."""
    import hashlib
    route_proof(commands, BMC_IP, PRIMARY_IP, PRIMARY_DEVICE, MANAGEMENT_TABLE)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(4)
        raw.bind((PRIMARY_IP, 0))
        raw.connect((BMC_IP, 443))
        with context.wrap_socket(raw, server_hostname=BMC_IP) as connection:
            require(hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest() == BMC_LEAF_SHA256, 'BMC_PIN_CHANGED')
    route_proof(commands, MANAGEMENT_PEER, PRIMARY_IP, PRIMARY_DEVICE, MANAGEMENT_TABLE)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(4)
        connection.bind((PRIMARY_IP, 0))
        connection.connect((MANAGEMENT_PEER, 22))
        require(connection.recv(256).startswith(b'SSH-2.0-'), 'MANAGEMENT_PEER_SSH_BANNER')
    return True
