#!/usr/bin/env bash
# Локальный MCP 1Panel. Никогда не открывает HTTP-сервер в сеть.
set -Eeuo pipefail
MCP_COMMIT=6cd495d74010e762e0ab6ce3ead31730f8ba572a
BIN=/opt/server-settings/bin
CONF=/etc/server-settings
PANEL_KEY=/var/lib/server-settings/1panel-api.key
SERVICE=/etc/systemd/system/server-settings-1panel-mcp.service
MODE=readonly
APPLY=0
for x in "$@"; do
  case "$x" in
    --apply) APPLY=1 ;;
    --readonly) MODE=readonly ;;
    --readwrite) MODE=readwrite ;;
    *) echo "Неизвестный аргумент: $x" >&2; exit 2 ;;
  esac
done
printf 'MCP: версия исходников %s, доступ %s, только 127.0.0.1:39443\n' "$MCP_COMMIT" "$MODE"
if [[ "$APPLY" -ne 1 ]]; then
  echo 'Это план. Установка не выполняется. Для установки: --apply'
  exit 0
fi
[[ "$EUID" -eq 0 ]] || { echo 'Требуется sudo/root' >&2; exit 1; }
[[ -f "$PANEL_KEY" ]] || { echo "Не найден файл ключа 1Panel: $PANEL_KEY" >&2; exit 1; }
[[ "$(stat -c '%a' "$PANEL_KEY")" == 600 ]] || { echo 'Ключ должен иметь mode 0600' >&2; exit 1; }
[[ -f "$SERVICE" ]] && { echo 'Служба MCP уже существует. Изменения требуют ручной проверки.' >&2; exit 1; }
if ! command -v go >/dev/null 2>&1; then
  apt-get update
  apt-get install -y golang-go ca-certificates
fi
mkdir -p -m 0755 "$BIN"
export GOTOOLCHAIN=auto
export GOBIN="$BIN"
go install "github.com/1Panel-dev/mcp-1panel@${MCP_COMMIT}"
[[ -x "$BIN/mcp-1panel" ]] || { echo 'Сборка MCP не создала бинарный файл' >&2; exit 1; }
install -d -m 0700 "$CONF"
# Вывод ключа не допускается ни в логи, ни в Git; EnvironmentFile доступен только root.
PANEL_ACCESS_TOKEN="$(tr -d '\r\n' < "$PANEL_KEY")"
[[ "$PANEL_ACCESS_TOKEN" =~ ^[a-fA-F0-9]{32}$ ]] || { echo 'Некорректный ключ 1Panel' >&2; exit 1; }
MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
umask 077
printf 'PANEL_HOST=http://127.0.0.1:19192\nPANEL_ACCESS_TOKEN=%s\nMCP_AUTH_TOKEN=%s\nMCP_ACCESS_LEVEL=%s\n' "$PANEL_ACCESS_TOKEN" "$MCP_AUTH_TOKEN" "$MODE" > "$CONF/mcp.env"
unset PANEL_ACCESS_TOKEN MCP_AUTH_TOKEN
chmod 0600 "$CONF/mcp.env"
cat > "$SERVICE" <<'UNIT'
[Unit]
Description=1Panel MCP (private, localhost only)
Wants=network-online.target
After=network-online.target 1panel-core.service
[Service]
Type=simple
DynamicUser=yes
EnvironmentFile=/etc/server-settings/mcp.env
ExecStart=/opt/server-settings/bin/mcp-1panel -transport streamable-http -addr http://127.0.0.1:39443/mcp
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=yes
CapabilityBoundingSet=
[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$SERVICE"
systemctl daemon-reload
systemctl enable --now server-settings-1panel-mcp.service
sleep 2
systemctl is-active --quiet server-settings-1panel-mcp.service || { echo 'Служба MCP не запустилась' >&2; exit 1; }
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:39443/mcp)"
[[ "$code" == 401 ]] || { echo "Ожидался отказ без Bearer: HTTP $code" >&2; exit 1; }
echo 'Локальная служба MCP запущена; HTTP без токена отвергается (401).'
echo 'Подключение ChatGPT извне отдельно НЕ настроено. Публикация порта 39443 запрещена.'
