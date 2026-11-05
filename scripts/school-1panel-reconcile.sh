#!/usr/bin/env bash
# VM100: 1Panel managed OpenResty, proxy site and private MCP.
# Plan-only by default. Apply requires an owner API key and a Proxmox checkpoint.
set -Eeuo pipefail
ACTION=plan
APPLY=
if (($#>=1)); then ACTION="$1"; fi
if (($#>=2)); then APPLY="$2"; fi
case "$ACTION" in plan|inspect|openresty|site|mcp) ;; *) echo "INVALID_ACTION" >&2; exit 2;; esac
if [[ "$APPLY" != "" && "$APPLY" != "--apply" ]]; then echo "INVALID_ARGUMENT" >&2; exit 2; fi
test "$(id -u)" = 0
test "$(hostname)" = asa-school-01
test -e /srv/asa-lab/.asa/installed-release.json
test "$(systemctl is-active docker)" = active
test "$(systemctl is-active 1panel-core)" = active
test "$(systemctl is-active 1panel-agent)" = active
test "$(grep -c '^COMPOSE_PROJECT_NAME=asa-lab-school$' /srv/asa-lab/.env)" = 1
test "$(curl --silent --show-error --max-time 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:4610/health/ready)" = 200
PANEL=/srv/Server-setings/scripts/panelctl.py
MCP=/srv/Server-setings/scripts/setup-mcp.sh
KEY=/var/lib/server-settings/1panel-api.key
test -f "$PANEL" && test -f "$MCP"
if [[ "$ACTION" == plan || "$APPLY" != --apply ]]; then
  echo "PLAN_ACTION=$ACTION"
  echo "PANEL_KEY_PRESENT=$([[ -s "$KEY" ]] && echo yes || echo no)"
  echo "OPENRESTY_INSTALLED=$([[ -f /opt/1panel/apps/openresty/openresty/docker-compose.yml ]] && echo yes || echo no)"
  echo "MCP_LOCAL_UNIT=$([[ -f /etc/systemd/system/server-settings-1panel-mcp.service ]] && echo present || echo absent)"
  echo "DATA_MODIFIED=NO"
  exit 0
fi
test -f "$KEY" && test ! -L "$KEY"
test "$(stat -c '%a:%u' "$KEY")" = 600:0
test -e /var/lib/asa-school/1panel-integration-snapshot-approved
python3 "$PANEL" inspect >/dev/null
case "$ACTION" in
  inspect)
    python3 "$PANEL" inspect
    ;;
  openresty)
    if test -f /opt/1panel/apps/openresty/openresty/docker-compose.yml; then
      echo "OPENRESTY_ALREADY_INSTALLED=YES"
      exit 0
    fi
    for port in 80 443; do
      if ss -lnt | awk '{print $4}' | grep -qE ":$port$"; then
        echo "BLOCKED_OPENRESTY_PORT=$port" >&2
        exit 3
      fi
    done
    python3 "$PANEL" install-openresty --openresty-version 1.27.1.2-5-1-focal --apply
    echo "OPENRESTY_INSTALL_REQUESTED_VIA_1PANEL_API"
    ;;
  site)
    test -f /opt/1panel/apps/openresty/openresty/docker-compose.yml
    python3 "$PANEL" create-site --site-host x.asa-lab.ru --site-upstream http://127.0.0.1:4610 --site-alias asa-lab-x --apply
    echo "MANAGED_SITE_CREATED_CHECK_PUBLIC_FRP_BEFORE_SWITCH"
    ;;
  mcp)
    test -x /opt/server-settings/bin/mcp-1panel
    test ! -e /etc/systemd/system/server-settings-1panel-mcp.service
    export GOPATH=/var/cache/asa-school-go
    export GOMODCACHE=/var/cache/asa-school-go/pkg/mod
    export GOCACHE=/var/cache/asa-school-go/cache
    export GOTOOLCHAIN=auto
    export GOMAXPROCS=2
    export HOME=/root
    bash "$MCP" --readonly --apply
    test "$(systemctl is-active server-settings-1panel-mcp)" = active
    test "$(curl --silent --show-error --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:39443/mcp)" = 401
    echo "MCP_LOCAL_READONLY_ACTIVE=YES"
    echo "MCP_PUBLIC_OAUTH_GATEWAY_NOT_CONFIGURED=YES"
    ;;
esac
