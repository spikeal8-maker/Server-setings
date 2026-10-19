#!/usr/bin/env bash
# Локальная systemd-проверка IP/DNS сертификата каждые 6 часов, без изменений 1Panel.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOST=''
APPLY=0
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --host) [[ "$#" -ge 2 ]] || { echo 'Нужен --host' >&2; exit 2; }; HOST="$2"; shift 2;;
    --apply) APPLY=1; shift;;
    *) echo "Неизвестный параметр: $1" >&2; exit 2;;
  esac
done
[[ "$HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo 'Укажите IP или имя целевого сайта через --host' >&2; exit 2; }
[[ "${#HOST}" -le 253 ]] || { echo 'Слишком длинный host' >&2; exit 2; }
echo "План: каждые 6 часов проверять TLS $HOST:443, предупреждать при остатке менее 48 часов"
if [[ "$APPLY" != 1 ]]; then
  echo 'Это план. Для установки требуется --apply'
  exit 0
fi
[[ "$EUID" == 0 ]] || { echo 'Требуется sudo/root' >&2; exit 1; }
[[ ! -f /etc/systemd/system/server-settings-cert-check.service ]] || { echo 'Монитор уже установлен, не перезаписываю' >&2; exit 1; }
install -d -m 0755 /usr/local/lib/server-settings
install -m 0755 "$ROOT/scripts/check-certificate.py" /usr/local/lib/server-settings/check-certificate.py
cat > /etc/systemd/system/server-settings-cert-check.service <<UNIT
[Unit]
Description=Проверка сроков публичного TLS сертификата ($HOST)
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/server-settings/check-certificate.py --host $HOST --warn-hours 48
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
UNIT
cat > /etc/systemd/system/server-settings-cert-check.timer <<'UNIT'
[Unit]
Description=Периодическая проверка TLS сайта Server-setings
[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
Persistent=true
RandomizedDelaySec=15m
Unit=server-settings-cert-check.service
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now server-settings-cert-check.timer
systemctl list-timers server-settings-cert-check.timer --no-pager
echo 'Монитор установлен. Вывод/ошибки: journalctl -u server-settings-cert-check.service'
echo 'ВНИМАНИЕ: пока это только системные логи, не push-уведомления владельцу.'
