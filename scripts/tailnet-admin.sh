#!/usr/bin/env bash
# Авторизация Tailscale на НОВОМ сервере с минимальными правами; без изменений DNS/VPN на компьютере администратора.
set -Eeuo pipefail
MODE=plan
KEY_FILE=''
SSH_PORT=22
PANEL_PORT=19192
while (($#)); do
  case "$1" in
    --apply) MODE=apply; shift ;;
    --auth-key-file) [[ $# -ge 2 ]] || exit 2; KEY_FILE="$2"; shift 2 ;;
    --ssh-port) [[ $# -ge 2 ]] || exit 2; SSH_PORT="$2"; shift 2 ;;
    --panel-port) [[ $# -ge 2 ]] || exit 2; PANEL_PORT="$2"; shift 2 ;;
    *) echo "Неизвестная опция: $1" >&2; exit 2 ;;
  esac
done
for x in "$SSH_PORT" "$PANEL_PORT"; do
  [[ "$x" =~ ^[0-9]+$ ]] && ((x >= 1 && x <= 65535)) || { echo 'Недопустимый порт' >&2; exit 2; }
done
echo 'Tailscale — только приватное администрирование; 1Panel нельзя публиковать через NAT'
printf 'Доверенный интерфейс: tailscale0; SSH: %s; 1Panel: %s\n' "$SSH_PORT" "$PANEL_PORT"
if [[ "$MODE" != apply ]]; then
  echo 'Без изменений. Для авторизованной новой Ubuntu: --apply и при необходимости --auth-key-file.'
  exit 0
fi
[[ "$EUID" -eq 0 ]] || { echo 'Требуется root' >&2; exit 1; }
command -v tailscale >/dev/null || { echo 'Tailscale пока не установлен' >&2; exit 1; }
command -v ufw >/dev/null || { echo 'UFW не установлен: отказ' >&2; exit 1; }
if [[ -n "$KEY_FILE" ]]; then
  [[ -f "$KEY_FILE" ]] || { echo 'Файл auth key не найден' >&2; exit 1; }
  [[ "$(stat -c '%a' "$KEY_FILE")" == 600 ]] || { echo 'Auth key должен иметь mode 0600' >&2; exit 1; }
fi
if tailscale status --json 2>/dev/null | python3 -c 'import sys,json;v=json.load(sys.stdin);sys.exit(0 if v.get("BackendState")=="Running" else 1)'; then
  echo 'Узел уже авторизован в tailnet — не переавторизую и не меняю его DNS/route settings.'
else
  [[ -n "$KEY_FILE" ]] || { echo 'Для безоператорной установки нужен --auth-key-file; либо вручную tailscale up' >&2; exit 1; }
  # file: не раскрывает токен в argv и shell history.
  tailscale up --auth-key="file:$KEY_FILE" --accept-dns=false
fi
# Изменения FW — только разрешения конкретных портов внутри Tailscale.
ufw allow in on tailscale0 to any port "$SSH_PORT" proto tcp
ufw allow in on tailscale0 to any port "$PANEL_PORT" proto tcp
LC_ALL=C ufw status verbose
printf 'TAILNET_IP='; tailscale ip -4
printf '%s\n' 'После применения проверьте с ДРУГОГО доверенного устройства SSH и 1Panel, затем ACL tailnet.'
