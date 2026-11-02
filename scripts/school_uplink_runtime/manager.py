import base64
import json
import os
from pathlib import Path
import time

from .adapters import profile_digest, select_interface, start_adapter, stop_process
from .network import (BACKUP_TABLE, MANAGEMENT_TABLE, PRIMARY_DEVICE, PRIMARY_GATEWAY,
                      PRIMARY_IP, PRIMARY_NETWORK, PROTOCOL, baseline, dns_probe, https_probe, route_configuration, management_gate, empty_table)
from .storage import DNSLease, load, lock, private_directory, require, save, Stop


class Manager:
    def __init__(self, commands, config=Path('/etc/asa-school-internet'), state=Path('/var/lib/asa-school-internet'),
                 resolver=Path('/etc/resolv.conf'), hook=Path('/usr/local/libexec/asa-school-dhcp-hook')):
        self.commands = commands
        self.config = private_directory(config)
        self.state = private_directory(state)
        self.journal = self.state / 'journal.json'
        self.proofs = self.state / 'qualification.json'
        self.dns = DNSLease(resolver)
        self.hook = hook

    def profiles(self):
        data = load(self.config / 'profiles.json')
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
        self.remember(['ip', '-4', 'rule', 'add', 'priority', str(priority), *map(str, args)],
                      ['ip', '-4', 'rule', 'del', 'priority', str(priority), *map(str, args)])

    def prepare(self, profile, acquire=None):
        require(not self.journal.exists(), 'RECOVER_EXISTING_JOURNAL_FIRST')
        before = baseline(self.commands, self.dns)
        device = select_interface(profile, self.commands)
        self.record = {'phase': 'preparing', 'mode': profile['mode'], 'profile': profile_digest(profile),
                       'device': device, 'baseline': before, 'undo': [], 'heartbeat': time.time()}
        save(self.journal, self.record)
        try:
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
            self.record.update(phase='standby', fail=0, good=0, changed=time.time())
            save(self.journal, self.record)
        except BaseException:
            self.rollback()
            raise

    def backup_healthy(self):
        lease = self.record['lease']
        require(lease['expires'] > time.time() + 15, 'DHCP_EXPIRED_REQUALIFY')
        current = self.commands.query('link', 'show', 'dev', lease['interface'])
        require(len(current) == 1 and current[0]['address'] == self.record['device']['mac'] and not current[0].get('master'), 'DEVICE_IDENTITY_CHANGED')
        return https_probe(self.commands, lease['address'], lease['interface'], BACKUP_TABLE)

    def qualify(self, mode, acquire=None):
        with self.transaction():
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
        require(proof.get('profile') == profile_digest(profile) and 0 < proof.get('qualified_at', 0) <= time.time(), 'ACTUAL_QUALIFICATION_REQUIRED')
        require(proof.get('mac') == self.record['device']['mac'], 'QUALIFIED_DEVICE_CHANGED')

    def arm(self):
        with self.transaction():
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
        self.record.update(phase='backup', changed=time.time(), good=0)
        save(self.journal, self.record)

    def failback(self):
        # Remove only the exact two global selectors; standby source-bound
        # policies remain available for independent backup probes.
        for priority, args in ((10020, ['lookup', str(BACKUP_TABLE)]), (10010, ['lookup', 'main', 'suppress_prefixlength', '0'])):
            inverse = ['ip', '-4', 'rule', 'del', 'priority', str(priority), *args]
            self.commands.call(*inverse, check=False)
            self.record['undo'] = [entry for entry in self.record['undo'] if entry != inverse]
        if self.record.get('dns'):
            self.dns.restore(self.record['dns'])
            self.record.pop('dns')
            self.record['baseline']['dns'] = self.dns.snapshot()
        self.record.update(phase='standby', changed=time.time(), fail=0, good=0)
        save(self.journal, self.record)

    def tick(self):
        with self.transaction():
            data = self.profiles()
            if not data['armed']:
                if self.journal.exists() and load(self.journal).get('armed_once'):
                    self.rollback()
                return
            require(self.journal.exists(), 'ARMED_WITHOUT_JOURNAL')
            self.record = load(self.journal)
            require(self.record['phase'] in ('standby', 'backup'), 'PARTIAL_TRANSACTION_RECOVER_REQUIRED')
            profile = next(p for p in data['profiles'] if p['mode'] == self.record['mode'])
            self.valid_proof(profile)
            require(route_configuration(self.commands.query('-4', 'route', 'show', 'table', 'main')) == route_configuration(self.record['baseline']['routes']), 'PRIMARY_ROUTES_CHANGED')
            proposal = self.state / ('lease-' + self.record['lease']['interface'] + '.json')
            if proposal.exists():
                renewed = load(proposal)
                require({k: v for k, v in renewed.items() if k != 'expires'} == {k: v for k, v in self.record['lease'].items() if k != 'expires'}, 'DHCP_CHANGED_REQUALIFY')
                self.record['lease']['expires'] = renewed['expires']
            healthy = https_probe(self.commands, PRIMARY_IP, PRIMARY_DEVICE, MANAGEMENT_TABLE)
            self.record['good'] = self.record.get('good', 0) + 1 if healthy else 0
            self.record['fail'] = 0 if healthy else self.record.get('fail', 0) + 1
            try:
                backup_ok = self.backup_healthy()
            except (Stop, OSError):
                backup_ok = False
            if self.record['phase'] == 'standby' and self.record['fail'] >= 3:
                if not backup_ok:
                    self.rotate(data)
                self.activate()
            elif self.record['phase'] == 'backup':
                if self.record['good'] >= 5 and time.time() - self.record['changed'] >= 60:
                    self.failback()
                else:
                    if not backup_ok:
                        self.rotate(data)
                        self.activate()
            self.record['heartbeat'] = time.time()
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
                self.record['armed_once'] = True
                save(self.journal, self.record)
                return
            except (Stop, ValueError, OSError):
                self.rollback()
        require(False, 'NO_HEALTHY_PREVIOUSLY_QUALIFIED_BACKUP')

    def rollback(self):
        if not self.journal.exists():
            return
        self.record = load(self.journal)
        errors = []
        for entry in reversed(self.record['undo']):
            try:
                if isinstance(entry, dict):
                    stop_process(entry)
                    Path(entry['pidfile']).unlink(missing_ok=True)
                else:
                    if entry[0:4] == ['ip', 'link', 'set', 'dev']:
                        require(not self.commands.query('-4', 'address', 'show', 'dev', entry[4])[0].get('addr_info'), 'FOREIGN_ADDRESS_NO_LINK_DOWN')
                    self.commands.call(*entry, check=False)
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

    def disarm(self):
        with self.transaction():
            data = self.profiles()
            data['armed'] = False
            save(self.config / 'profiles.json', data)
            self.rollback()

    def watchdog(self):
        with self.transaction():
            if self.journal.exists() and time.time() - load(self.journal).get('heartbeat', 0) > (45 if self.profiles()['armed'] else 300):
                data = self.profiles()
                data['armed'] = False
                save(self.config / 'profiles.json', data)
                self.rollback()
