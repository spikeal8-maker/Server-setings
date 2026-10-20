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
assert i['id']=='asa-test' and i['root']=='/srv/asa-lab' and i['project']=='asa-lab-dev' and i['profile']=='production'
assert i['owner_uid']==pwd.getpwnam('admins').pw_uid
assert pathlib.Path(i['root']).stat().st_uid==i['owner_uid']
assert i['state_root']=='/var/lib/server-settings/asa-operations'
assert c['port']==39446
assert c['official_command']==['/opt/server-settings/bin/mcp-1panel','-transport','stdio','-access-level','full']
PY
install -d -m 0755 /opt/server-settings/asa-operations/asa_ops
install -m 0644 "$SOURCE"/scripts/asa_ops/*.py /opt/server-settings/asa-operations/asa_ops/
install -d -m 0700 -o admins -g admins /var/lib/server-settings/asa-operations
install -m 0644 "$SOURCE/templates/asa-operations.service" /etc/systemd/system/asa-operations.service
python3 -m compileall -q /opt/server-settings/asa-operations/asa_ops
systemctl daemon-reload
systemctl enable --now asa-operations.service
systemctl is-active --quiet asa-operations.service
echo 'Private backend started. Gateway switch is a separate reviewed 1Panel Compose API operation.'
