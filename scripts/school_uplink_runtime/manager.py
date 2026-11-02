import base64
import json
import os
from pathlib import Path
import time

from .adapters import profile_digest, select_interface, start_adapter, stop_process, boot_id
from .network import (BACKUP_TABLE, MANAGEMENT_TABLE, PRIMARY_DEVICE, PRIMARY_GATEWAY,
                      PRIMARY_IP, PRIMARY_NETWORK, PROTOCOL, baseline, dns_probe, https_probe, route_configuration, management_gate, empty_table, addresses)
from .storage import DNSLease, load, lock, private_directory, require, save, Stop, trust


def watchdog_gate(commands):
    timer = 'asa-school-uplink-watchdog.timer'
    for unit in (timer, 'asa-school-uplink-watchdog.service'):
        installed = Path('/etc/systemd/system') / unit
        expected = Path(__file__).absolute().parents[2] / 'units' / unit
        trust(installed)
        trust(expected)
        require(installed.read_bytes() == expected.read_bytes(), 'WATCHDOG_UNIT_CONTENT_CHANGED')
        require(commands.call('systemctl', 'show', unit, '--property=FragmentPath', '--value').stdout.strip() == str(installed), 'WATCHDOG_FOREIGN_FRAGMENT')
        require(not commands.call('systemctl', 'show', unit, '--property=DropInPaths', '--value').stdout.strip(), 'WATCHDOG_FOREIGN_DROPINS')
    require(commands.call('systemctl', 'is-active', timer).stdout.strip() == 'active', 'WATCHDOG_MUST_BE_ACTIVE_BEFORE_QUALIFY')
    require(commands.call('systemctl', 'is-enabled', timer).stdout.strip() == 'enabled', 'WATCHDOG_MUST_BE_ENABLED_BEFORE_QUALIFY')
    next_time = commands.call('systemctl', 'show', timer, '--property=NextElapseUSecMonotonic', '--value').stdout.strip()
    require(next_time and next_time not in ('0', 'n/a', 'infinity'), 'WATCHDOG_NOT_SCHEDULED')
    return True


class Manager:
    def __init__(self, commands, config=Path('/etc/asa-school-internet'), state=Path('/var/lib/asa-school-internet'),
                 resolver=Path('/etc/resolv.conf'), hook=Path('/usr/local/libexec/asa-school-dhcp-hook'), watchdog=watchdog_gate, acquire=None):
        self.commands = commands
        self.config = private_directory(config)
        self.state = private_directory(state)
        self.journal = self.state / 'journal.json'
        self.proofs = self.state / 'qualification.json'
        self.dns = DNSLease(resolver)
        self.hook = hook
        self.watchdog_ready = watchdog
        self.acquire = acquire

    def profiles(self):
        data = load(self.config / 'profiles.json') if (self.config / 'profiles.json').exists() else {'schema': 1, 'armed': False, 'profiles': []}
        require(data.get('schema') == 1 and isinstance(data.get('armed'), bool) and isinstance(data.get('profiles'), list), 'PROFILE_SCHEMA')
        modes = [p.get('mode') for p in data['profiles']]
        require(len(modes) == len(set(modes)) and set(modes) <= {'router', 'wifi', 'modem'}, 'PROFILE_MODES')
        for p in data['profiles']:
            fields = {'mode', 'qualified'} | {'router': {'interface'}, 'wifi': {'ssid', 'password'}, 'modem': {'apn'}}[p['mode']]
            require(set(p) == fields and isinstance(p['qualified'], bool), 'PROFILE_FIELDS')
        return data

    def transaction(self):
        return lock(self.config / 'profile.lock')

    def remember(self, forward, inverse):
        # Write-ahead intent permits cleanup even if killed between intent and ip.
        self.record['undo'].append(inverse)
        save(self.journal, self.record)
        if forward:
            self.commands.call(*forward)

    def route(self, table, *args):
        self.remember(['ip', '-4', 'route', 'add', 'table', str(table), *map(str, args), 'proto', PROTOCOL],
                      ['ip', '-4', 'route', 'del', 'table', str(table), *map(str, args), 'proto', PROTOCOL])

    def rule(self, priority, *args):
        self.remember(['ip', '-4', 'rule', 'add', 'priority', str(priority), *map(str, args), 'protocol', PROTOCOL],
                      ['ip', '-4', 'rule', 'del', 'priority', str(priority), *map(str, args), 'protocol', PROTOCOL])

    def prepare(self, profile, acquire=None):
        require(self.watchdog_ready(self.commands), 'INDEPENDENT_WATCHDOG_REQUIRED')
        require(not self.journal.exists(), 'RECOVER_EXISTING_JOURNAL_FIRST')
        before = baseline(self.commands, self.dns)
        device = select_interface(profile, self.commands)
        self.record = {'phase': 'preparing', 'mode': profile['mode'], 'profile': profile_digest(profile),
                       'device': device, 'boot_id': boot_id(), 'baseline': before, 'undo': [], 'heartbeat': time.time(), 'heartbeat_mono': time.monotonic()}
        save(self.journal, self.record)
        try:
            # Fence only the selected backup before association/link-UP. Never
            # touch global/primary IPv6; otherwise an RA can bypass IPv4 policy.
            for knob, owned in (('accept_ra', '0'), ('autoconf', '0'), ('disable_ipv6', '1')):
                path = Path('/proc/sys/net/ipv6/conf') / device['interface'] / knob
                old = path.read_text().strip()
                self.record['undo'].append({'kind': 'ipv6', 'path': str(path), 'old': old, 'owned': owned})
                save(self.journal, self.record)
                path.write_text(owned)
                require(path.read_text().strip() == owned, 'IPV6_FENCE_FAILED')
            self.remember(['ip', 'link', 'set', 'dev', device['interface'], 'up'],
                          ['ip', 'link', 'set', 'dev', device['interface'], 'down'])
            acquire = acquire or self.acquire
            lease = acquire(device) if acquire else start_adapter(profile, device, self.commands, self.state, self.remember, self.hook)
            require(lease['interface'] == device['interface'], 'LEASE_DEVICE')
            self.record['lease'] = lease
            save(self.journal, self.record)
            interface = lease['interface']
            self.remember(['ip', '-4', 'addr', 'add', f"{lease['address']}/{lease['prefix']}", 'dev', interface, 'noprefixroute'],
                          ['ip', '-4', 'addr', 'del', f"{lease['address']}/{lease['prefix']}", 'dev', interface])
            self.route(MANAGEMENT_TABLE, str(PRIMARY_NETWORK), 'dev', PRIMARY_DEVICE, 'src', PRIMARY_IP)
            self.route(MANAGEMENT_TABLE, 'default', 'via', PRIMARY_GATEWAY, 'dev', PRIMARY_DEVICE, 'src', PRIMARY_IP)
            self.route(BACKUP_TABLE, lease['network'], 'dev', interface, 'src', lease['address'])
            self.route(BACKUP_TABLE, 'default', 'via', lease['gateway'], 'dev', interface, 'src', lease['address'])
            self.rule(10000, 'from', PRIMARY_IP + '/32', 'lookup', MANAGEMENT_TABLE)
            self.rule(10005, 'from', lease['address'] + '/32', 'lookup', BACKUP_TABLE)
            require(route_configuration(self.commands.query('-4', 'route', 'show', 'table', 'main')) == route_configuration(before['routes']), 'MAIN_CHANGED')
            self.record.update(phase='standby', fail=0, good=0, changed=time.time(), changed_mono=time.monotonic())
            save(self.journal, self.record)
        except BaseException:
            self.rollback()
            raise

    def backup_healthy(self):
        lease = self.record['lease']
        require(lease.get('boot_id') == boot_id() and lease.get('expires_mono', 0) > time.monotonic() + 15, 'DHCP_EXPIRED_REBIND')
        current = self.commands.query('link', 'show', 'dev', lease['interface'])
        require(len(current) == 1 and current[0]['address'] == self.record['device']['mac'] and not current[0].get('master'), 'DEVICE_IDENTITY_CHANGED')
        return https_probe(self.commands, lease['address'], lease['interface'], BACKUP_TABLE)

    def qualify(self, mode, acquire=None):
        with self.transaction():
            require(self.watchdog_ready(self.commands), 'INDEPENDENT_WATCHDOG_REQUIRED')
            data = self.profiles()
            require(not data['armed'], 'DISARM_FIRST')
            profile = next((p for p in data['profiles'] if p['mode'] == mode), None)
            require(profile is not None, 'PROFILE_NOT_FOUND')
            if self.journal.exists():
                self.rollback()
            self.prepare(profile, acquire)
            try:
                require(management_gate(self.commands), 'INDEPENDENT_MANAGEMENT_GATE')
                require(https_probe(self.commands, PRIMARY_IP, PRIMARY_DEVICE, MANAGEMENT_TABLE), 'PRIMARY_MANAGEMENT_PROBE_FAILED')
                require(self.backup_healthy(), 'BACKUP_TLS_PROBE_FAILED')
                lease = self.record['lease']
                require(dns_probe(self.commands, lease['dns'][0], lease['address'], lease['interface']), 'BACKUP_DNS_PROBE_FAILED')
                proofs = load(self.proofs) if self.proofs.exists() else {}
                proofs[mode] = {'profile': profile_digest(profile), 'mac': self.record['device']['mac'], 'qualified_at': time.time()}
                save(self.proofs, proofs)
            except BaseException:
                self.rollback()
                raise

    def valid_proof(self, profile):
        proofs = load(self.proofs) if self.proofs.exists() else {}
        proof = proofs.get(profile['mode'], {})
        require(proof.get('profile') == profile_digest(profile), 'PROFILE_CHANGED_ACTUAL_QUALIFICATION_REQUIRED')
        require(proof.get('qualified_at', 0) > 0, 'ACTUAL_QUALIFICATION_REQUIRED')
        require(proof.get('mac') == self.record['device']['mac'], 'QUALIFIED_DEVICE_CHANGED')

    def arm(self):
        with self.transaction():
            require(self.watchdog_ready(self.commands), 'INDEPENDENT_WATCHDOG_REQUIRED')
            data = self.profiles()
            require(not data['armed'] and self.journal.exists(), 'QUALIFY_BEFORE_ARM')
            self.record = load(self.journal)
            require(self.record['phase'] == 'standby', 'STANDBY_REQUIRED')
            profile = next(p for p in data['profiles'] if p['mode'] == self.record['mode'])
            self.valid_proof(profile)
            require(self.backup_healthy(), 'BACKUP_UNHEALTHY')
            data['armed'] = True
            save(self.config / 'profiles.json', data)
            self.record['armed_once'] = True
            self.record['heartbeat'] = time.time()
            self.record['heartbeat_mono'] = time.monotonic()
            save(self.journal, self.record)

    def activate(self):
        lease = self.record['lease']
        dns_probe(self.commands, lease['dns'][0], lease['address'], lease['interface'])
        original = self.dns.snapshot()
        require(original == self.record['baseline']['dns'], 'DNS_CHANGED_SINCE_QUALIFICATION')
        replacement = ''.join('nameserver ' + ip + '\n' for ip in lease['dns']).encode()
        self.record['dns'] = {'original': original, 'planned': base64.b64encode(replacement).decode()}
        save(self.journal, self.record)
        self.record['dns']['owned'] = self.dns.replace(original, replacement)
        save(self.journal, self.record)
        self.rule(10010, 'lookup', 'main', 'suppress_prefixlength', '0')
        self.rule(10020, 'lookup', BACKUP_TABLE)
        self.record.update(phase='backup', changed=time.time(), changed_mono=time.monotonic(), good=0)
        save(self.journal, self.record)

    def failback(self):
        # Remove only the exact two global selectors; standby source-bound
        # policies remain available for independent backup probes.
        for priority, args in ((10020, ['lookup', str(BACKUP_TABLE)]), (10010, ['lookup', 'main', 'suppress_prefixlength', '0'])):
            inverse = ['ip', '-4', 'rule', 'del', 'priority', str(priority), *args, 'protocol', PROTOCOL]
            self.undo(inverse)
            self.record['undo'] = [entry for entry in self.record['undo'] if entry != inverse]
            save(self.journal, self.record)
        if self.record.get('dns'):
            self.dns.restore(self.record['dns'])
            self.record.pop('dns')
            self.record['baseline']['dns'] = self.dns.snapshot()
        self.record.update(phase='standby', changed=time.time(), changed_mono=time.monotonic(), fail=0, good=0)
        save(self.journal, self.record)

    def tick(self):
        with self.transaction():
            data = self.profiles()
            if not data['armed']:
                if self.journal.exists() and load(self.journal).get('armed_once'):
                    self.rollback()
                return
            if self.journal.exists() and load(self.journal).get('boot_id') != boot_id():
                self.rollback()  # Old kernel state gone; CAS restore old DNS now.
            if not self.journal.exists():
                if not self.resume(data):
                    return  # Armed intent waits for previously qualified device.
            self.record = load(self.journal)
            require(self.record['phase'] in ('standby', 'backup'), 'PARTIAL_TRANSACTION_RECOVER_REQUIRED')
            profile = next(p for p in data['profiles'] if p['mode'] == self.record['mode'])
            self.valid_proof(profile)
            require(route_configuration(self.commands.query('-4', 'route', 'show', 'table', 'main')) == route_configuration(self.record['baseline']['routes']), 'PRIMARY_ROUTES_CHANGED')
            proposal = self.state / ('lease-' + self.record['lease']['interface'] + '.json')
            needs_rebind = False
            if proposal.exists():
                renewed = load(proposal)
                same = {k: v for k, v in renewed.items() if k not in ('expires', 'expires_mono')} == {k: v for k, v in self.record['lease'].items() if k not in ('expires', 'expires_mono')}
                if same and renewed.get('expires_mono', 0) > time.monotonic() + 15:
                    self.record['lease']['expires'] = renewed['expires']
                    self.record['lease']['expires_mono'] = renewed['expires_mono']
                else:
                    needs_rebind = True
            if needs_rebind:
                was_backup = self.record['phase'] == 'backup'
                if not self.rotate(data):
                    return
                if was_backup:
                    self.activate()
            healthy = https_probe(self.commands, PRIMARY_IP, PRIMARY_DEVICE, MANAGEMENT_TABLE)
            self.record['good'] = self.record.get('good', 0) + 1 if healthy else 0
            self.record['fail'] = 0 if healthy else self.record.get('fail', 0) + 1
            try:
                backup_ok = self.backup_healthy()
            except (Stop, OSError):
                backup_ok = False
            if self.record['phase'] == 'standby' and self.record['fail'] >= 3:
                if not backup_ok:
                    if not self.rotate(data):
                        return
                self.activate()
            elif self.record['phase'] == 'backup':
                if self.record['good'] >= 5 and time.monotonic() - self.record['changed_mono'] >= 60:
                    self.failback()
                else:
                    if not backup_ok:
                        if not self.rotate(data):
                            return
                        self.activate()
            self.record['heartbeat'] = time.time()
            self.record['heartbeat_mono'] = time.monotonic()
            save(self.journal, self.record)

    def rotate(self, data):
        """Only previously qualified profiles; no implicit new-device enrollment."""
        current = self.record['mode']
        proofs = load(self.proofs) if self.proofs.exists() else {}
        choices = [p for p in data['profiles'] if p['mode'] != current] + [p for p in data['profiles'] if p['mode'] == current]
        self.rollback()
        for profile in choices:
            if proofs.get(profile['mode'], {}).get('profile') != profile_digest(profile):
                continue
            try:
                self.prepare(profile)
                self.valid_proof(profile)
                require(self.backup_healthy(), 'CANDIDATE_UNHEALTHY')
                lease = self.record['lease']
                dns_probe(self.commands, lease['dns'][0], lease['address'], lease['interface'])
                self.record['armed_once'] = True
                save(self.journal, self.record)
                return True
            except (Stop, ValueError, OSError):
                self.rollback()
        return False

    def resume(self, data):
        proofs = load(self.proofs) if self.proofs.exists() else {}
        for profile in data['profiles']:
            if proofs.get(profile['mode'], {}).get('profile') != profile_digest(profile):
                continue
            try:
                self.prepare(profile)
                self.valid_proof(profile)
                require(self.backup_healthy(), 'CANDIDATE_UNHEALTHY')
                lease = self.record['lease']
                dns_probe(self.commands, lease['dns'][0], lease['address'], lease['interface'])
                self.record['armed_once'] = True
                save(self.journal, self.record)
                return True
            except (Stop, ValueError, OSError):
                self.rollback()
        return False

    def rollback(self):
        if not self.journal.exists():
            return
        self.record = load(self.journal)
        require(route_configuration(self.commands.query('-4', 'route', 'show', 'table', 'main')) == route_configuration(self.record['baseline']['routes']), 'FOREIGN_MAIN_NO_ROLLBACK_MUTATION')
        # Validate all rule/route ownership before dependent address removal can
        # indirectly invalidate a foreign route with the same preferred source.
        for entry in self.record['undo']:
            if isinstance(entry, list) and entry[:4] in (['ip', '-4', 'rule', 'del'], ['ip', '-4', 'route', 'del']):
                self.undo(entry, inspect=True)
        errors = []
        for entry in reversed(self.record['undo']):
            if errors and isinstance(entry, dict) and entry.get('kind') == 'ipv6':
                continue  # Keep the RA fence if address/link cleanup failed.
            try:
                self.undo(entry)
                self.record['undo'].remove(entry)
                save(self.journal, self.record)
            except (OSError, Stop) as exc:
                errors.append(str(exc))
        if self.record.get('dns'):
            try:
                self.dns.restore(self.record['dns'])
            except (OSError, Stop) as exc:
                errors.append(str(exc))
        require(not errors, 'ROLLBACK_REQUIRES_OWNER:' + ','.join(errors))
        remaining = self.commands.query('-4', 'rule', 'show')
        require(not any(r.get('priority') in (10000, 10005, 10010, 10020) for r in remaining), 'POLICY_RULE_CLEANUP_FAILED')
        for table in (MANAGEMENT_TABLE, BACKUP_TABLE):
            result = self.commands.call('ip', '-j', '-4', 'route', 'show', 'table', str(table), check=False)
            require(empty_table(result), 'POLICY_ROUTE_CLEANUP_FAILED')
        require(route_configuration(self.commands.query('-4', 'route', 'show', 'table', 'main')) == route_configuration(self.record['baseline']['routes']), 'MAIN_CHANGED_NOT_REWRITTEN')
        self.journal.unlink()
        for pattern in ('wifi.conf', 'dhclient.leases', 'dhcp-*.json', 'lease-*.json'):
            for path in self.state.glob(pattern):
                path.unlink(missing_ok=True)

    def undo(self, entry, inspect=False):
        if isinstance(entry, dict):
            if entry.get('kind') == 'ipv6':
                if not self.owned_device():
                    return
                path = Path(entry['path'])
                require(path.parent == Path('/proc/sys/net/ipv6/conf') / self.record['device']['interface'] and path.name in ('accept_ra', 'autoconf', 'disable_ipv6'), 'IPV6_UNDO_SCOPE')
                current = path.read_text().strip()
                if current == entry['old']:
                    return
                require(current == entry['owned'], 'FOREIGN_IPV6_SETTING')
                path.write_text(entry['old'])
                require(path.read_text().strip() == entry['old'], 'IPV6_RESTORE_FAILED')
                return
            stop_process(entry)
            if entry.get('pidfile'):
                pidfile = Path(entry['pidfile'])
                if pidfile.exists():
                    trust(pidfile)
                    require(pidfile.read_text().strip() == str(entry['pid']), 'FOREIGN_PIDFILE_NO_DELETE')
                    pidfile.unlink()
            return
        if entry[:4] == ['ip', '-4', 'rule', 'del']:
            if self.record['boot_id'] != boot_id():
                return
            priority = int(entry[5])
            rows = [r for r in json.loads(self.commands.call('ip', '-N', '-j', '-4', 'rule', 'show').stdout) if r.get('priority') == priority]
            if not rows:
                return
            require(len(rows) == 1, 'AMBIGUOUS_RULE_OWNERSHIP')
            expected = {'priority': priority, 'src': 'all'}
            arguments = entry[6:]
            for key, value in zip(arguments[::2], arguments[1::2]):
                expected[{'from': 'src', 'lookup': 'table', 'suppress_prefixlength': 'suppress_prefixlen', 'protocol': 'protocol'}[key]] = value
            expected['table'] = {'main': '254', 'local': '255', 'default': '253'}.get(str(expected['table']), expected['table'])
            row = dict(rows[0])
            row.pop('flags', None)
            # iproute2 prints a /32 source without suffix.
            normalized = lambda data: {k: str(v).removesuffix('/32') for k, v in data.items()}
            require(normalized(row) == normalized(expected), 'FOREIGN_RULE_NO_DELETE')
            if inspect:
                return
            self.commands.call(*entry)
            require(not any(r.get('priority') == priority for r in self.commands.query('-4', 'rule', 'show')), 'RULE_DELETE_NOT_EFFECTIVE')
        elif entry[:4] == ['ip', '-4', 'route', 'del']:
            if self.record['boot_id'] != boot_id():
                return
            table, destination = entry[5:7]
            dump = self.commands.call('ip', '-N', '-j', '-4', 'route', 'show', 'table', table, check=False)
            if empty_table(dump):
                return
            require(dump.returncode == 0, 'ROUTE_OWNERSHIP_DUMP_FAILED')
            rows = json.loads(dump.stdout)
            matches = [r for r in rows if r.get('dst') == destination]
            if not matches:
                return
            require(len(matches) == 1, 'AMBIGUOUS_ROUTE_OWNERSHIP')
            expected = {'dst': destination}
            for key, value in zip(entry[7::2], entry[8::2]):
                expected[{'dev': 'dev', 'src': 'prefsrc', 'via': 'gateway', 'proto': 'protocol'}[key]] = value
            if 'gateway' not in expected:
                expected['scope'] = '253'
            actual = route_configuration(matches)[0]
            if 'table' in actual:
                require(str(actual.pop('table')) == table, 'FOREIGN_ROUTE_TABLE')
            require({k: str(v) for k, v in actual.items()} == expected, 'FOREIGN_ROUTE_NO_DELETE')
            if inspect:
                return
            result = self.commands.call(*entry, check=False)
            # Absence after an interrupted prior rollback is acceptable; other
            # failures are not. Final empty-table verification still applies.
            require(result.returncode == 0 or 'No such process' in result.stderr or 'FIB table does not exist' in result.stderr, 'ROUTE_DELETE_FAILED')
        elif entry[:4] == ['ip', '-4', 'addr', 'del']:
            address, interface = entry[4], entry[6]
            if not self.owned_device():
                return  # Old device disappeared; never mutate a replacement.
            present = lambda: any(a.get('local') + '/' + str(a.get('prefixlen')) == address for a in addresses(self.commands, interface))
            if present():
                self.commands.call(*entry)
            require(not present(), 'ADDRESS_DELETE_FAILED')
        elif entry[:4] == ['ip', 'link', 'set', 'dev']:
            interface = entry[4]
            if not self.owned_device():
                return
            require(not addresses(self.commands, interface), 'FOREIGN_ADDRESS_NO_LINK_DOWN')
            link = self.commands.query('link', 'show', 'dev', interface)[0]
            require(not link.get('master') and link['address'] == self.record['device']['mac'], 'FOREIGN_LINK_NO_CHANGE')
            self.commands.call(*entry)
            require('UP' not in self.commands.query('link', 'show', 'dev', interface)[0].get('flags', []), 'LINK_DOWN_FAILED')
        else:
            require(False, 'UNKNOWN_UNDO')

    def owned_device(self):
        require('boot_id' in self.record, 'TRANSACTION_BOOT_PROOF_REQUIRED')
        if self.record['boot_id'] != boot_id():
            return False
        device = self.record['device']
        links = self.commands.query('link', 'show')
        named = [r for r in links if r.get('ifname') == device['interface']]
        if len(named) == 1 and named[0]['ifindex'] == device['ifindex'] and named[0]['address'] == device['mac']:
            require(not named[0].get('master'), 'DEVICE_FOREIGN_MASTER')
            return True
        require(not any(r['ifindex'] == device['ifindex'] and r['address'] == device['mac'] for r in links), 'DEVICE_MOVED_REQUIRES_OWNER')
        return False  # Old device gone/reused; saved name now belongs to another.

    def disarm(self):
        with self.transaction():
            data = self.profiles()
            data['armed'] = False
            save(self.config / 'profiles.json', data)
            self.rollback()

    def watchdog(self):
        with self.transaction():
            record = load(self.journal) if self.journal.exists() else None
            if record and (record.get('boot_id') != boot_id() or time.monotonic() - record.get('heartbeat_mono', 0) > (45 if self.profiles()['armed'] else 300)):
                data = self.profiles()
                data['armed'] = False
                save(self.config / 'profiles.json', data)
                self.rollback()
