#!/usr/bin/env bash
# ASA Lab VM100 — native on-server owner enrollment for Yandex/Tailscale.
set -Eeuo pipefail
[[ "$EUID" == 0 ]] || { echo 'Запустите через sudo в терминале VM100.' >&2; exit 2; }
[[ "$(hostname)" == "asa-school-01" ]] || { echo 'Неверный сервер. Требуется asa-school-01.' >&2; exit 2; }
SOURCE="/srv/Server-setings/scripts"
[[ -f "$SOURCE/school-yandex-connect.py" && -f "$SOURCE/school-tailscale-connect.py" ]] || {
  echo 'Исходные скрипты Server-setings не найдены.' >&2; exit 2;
}
ACTION=help
if [[ "$#" -ge 1 ]]; then ACTION="$1"; fi
case "$ACTION" in
  yandex)
    exec /usr/bin/python3 "$SOURCE/school-yandex-connect.py" connect --expected-host asa-school-01
    ;;
  yandex-reauthorize)
    exec /usr/bin/python3 "$SOURCE/school-yandex-connect.py" reauthorize --expected-host asa-school-01
    ;;
  tailscale)
    exec /usr/bin/python3 "$SOURCE/school-tailscale-connect.py" connect --expected-host asa-school-01
    ;;
  status)
    echo '=== ЯНДЕКС ДИСК ==='
    /usr/bin/python3 "$SOURCE/school-yandex-connect.py" status --expected-host asa-school-01 || true
    echo '=== TAILSCALE ==='
    /usr/bin/python3 "$SOURCE/school-tailscale-connect.py" status --expected-host asa-school-01 || true
    echo '=== ОСНОВНЫЕ СЛУЖБЫ ==='
    for s in 1panel-core 1panel-agent asa-frpc server-settings-1panel-mcp; do
      printf '%s: ' "$s"
      /usr/bin/systemctl is-active "$s" || true
    done
    ;;
  help|--help|-h)
    printf '%s\n' \
      'ASA Lab School — самостоятельные подключения VM100.' \
      '  sudo asa-school-connect yandex     — первый вход в Яндекс.Диск на VM100' \
      '  sudo asa-school-connect yandex-reauthorize — повторный вход без смены OAuth-приложения' \
      '  sudo asa-school-connect tailscale — присоединить VM100 к Tailnet' \
      '  sudo asa-school-connect status    — показать состояния, без секретов' \
      'Логин и ключи вводятся скрыто НА VM100. D2-R2 не требуется для работы служб.'
    ;;
  *)
    echo 'Неизвестная операция. Выполните asa-school-connect help.' >&2
    exit 2
    ;;
esac
