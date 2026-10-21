#!/usr/bin/env bash
# Installs only the additional private backend. Does not replace the live gateway.
set -Eeuo pipefail
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${1:-}" != --apply ]]; then
  echo 'PLAN: install private ASA operations backend; existing ASA app/data and gateway are untouched.'
  echo 'Requires reviewed /etc/server-settings/asa-operations.json and .env, then --apply.'
  exit 0
fi
[[ "$EUID" == 0 ]] || { echo 'Run as root'; exit 1; }
CONF=/etc/server-settings/asa-operations.json
ENVFILE=/etc/server-settings/asa-operations.env
[[ -f "$CONF" && -f "$ENVFILE" ]] || { echo 'Missing private configuration'; exit 1; }
[[ "$(stat -c '%a' "$ENVFILE")" == 600 ]] || { echo 'Environment file must have mode 0600'; exit 1; }
python3 - "$CONF" <<'PY'
import json, pathlib, pwd, sys
c=json.loads(pathlib.Path(sys.argv[1]).read_text());i=c['installation']
assert pathlib.Path(i['root']).is_absolute() and pathlib.Path(i['root']).resolve()==pathlib.Path(i['root'])
assert i['profile'] in ('production','staging') and isinstance(i['id'],str) and i['id']
assert i['project'] and all(x.isalnum() or x in '_-' for x in i['project'])
assert pwd.getpwuid(i['owner_uid']).pw_uid!=0
assert pathlib.Path(i['root']).stat().st_uid==i['owner_uid']
assert pathlib.Path(i['state_root']).is_absolute() and pathlib.Path(i['state_root']).resolve()==pathlib.Path(i['state_root'])
assert 1024<=c['port']<=65535
assert c['official_command']==['/opt/server-settings/bin/mcp-1panel','-transport','stdio','-access-level','full']
PY
OWNER="$(python3 - "$CONF" <<'PY'
import json,pathlib,pwd,sys
print(pwd.getpwuid(json.loads(pathlib.Path(sys.argv[1]).read_text())['installation']['owner_uid']).pw_name)
PY
)"
STATE_ROOT="$(python3 - "$CONF" <<'PY'
import json,pathlib,sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text())['installation']['state_root'])
PY
)"
WAS_ACTIVE="$(systemctl is-active asa-operations.service || true)"
# Never kill detached workers or modify application services to install the backend.
systemctl stop asa-operations.service 2>/dev/null || true
trap 'if [[ "$WAS_ACTIVE" == active ]]; then systemctl start asa-operations.service; fi' EXIT
python3 - "$STATE_ROOT" <<'PY'
import pathlib,sqlite3,sys
p=pathlib.Path(sys.argv[1])/'operations.sqlite3'
if p.exists():
 with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as db:
  assert db.execute("SELECT count(*) FROM operations WHERE status IN ('queued','running')").fetchone()[0]==0,'Active worker: install deferred'
PY
# Existing server-settings directories may be private root-only. Grant only
# traversal and execution of the official binary to the installation owner.
command -v setfacl >/dev/null
setfacl -m u:"$OWNER":--x /opt/server-settings /opt/server-settings/bin
setfacl -m u:"$OWNER":r-x /opt/server-settings/bin/mcp-1panel
install -d -m 0755 /opt/server-settings/asa-operations/asa_ops
install -m 0644 "$SOURCE"/scripts/asa_ops/*.py /opt/server-settings/asa-operations/asa_ops/
install -d -m 0700 -o "$OWNER" -g "$(id -gn "$OWNER")" "$STATE_ROOT"
setfacl -m u:"$OWNER":--x /var/lib/server-settings
python3 - "$SOURCE/templates/asa-operations.service" "$OWNER" <<'PY'
import pathlib,sys
text=pathlib.Path(sys.argv[1]).read_text().replace('User=admins','User='+sys.argv[2])
import grp,pwd
text=text.replace('Group=admins','Group='+grp.getgrgid(pwd.getpwnam(sys.argv[2]).pw_gid).gr_name)
target=pathlib.Path('/etc/systemd/system/asa-operations.service')
target.write_text(text);target.chmod(0o644)
PY
install -m 0644 "$SOURCE/scripts/prepare-asa-maintenance.py" /opt/server-settings/asa-operations/prepare-asa-maintenance.py
python3 /opt/server-settings/asa-operations/prepare-asa-maintenance.py --config "$CONF" --apply
python3 -m compileall -q /opt/server-settings/asa-operations/asa_ops
systemctl daemon-reload
systemctl enable --now asa-operations.service
systemctl is-active --quiet asa-operations.service
trap - EXIT
echo 'Private backend started. Gateway switch is a separate reviewed 1Panel Compose API operation.'
