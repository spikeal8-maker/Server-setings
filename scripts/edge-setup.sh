#!/usr/bin/env bash
# Идемпотентный второй этап после первичного входа в 1Panel и запуска ASA Lab.
# Публичный сервер — только OpenResty под 1Panel, LAN TLS — отдельный локальный Caddy.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE=''
DOMAIN=''
PUBLIC_IP=''
LAN_IP=''
LAN_CIDR=''
LAN_HTTPS=0
MCP_READONLY=1
CERT_MONITOR=1
ORIGIN_PREPARED=0
APPLY=0
while (($#));do
  case "$1" in
    --profile) [[ $# -ge 2 ]] || exit 2;PROFILE="$2";shift 2 ;;
    --domain) [[ $# -ge 2 ]] || exit 2;DOMAIN="$2";shift 2 ;;
    --public-ip) [[ $# -ge 2 ]] || exit 2;PUBLIC_IP="$2";shift 2 ;;
    --lan-ip) [[ $# -ge 2 ]] || exit 2;LAN_IP="$2";shift 2 ;;
    --lan-cidr) [[ $# -ge 2 ]] || exit 2;LAN_CIDR="$2";shift 2 ;;
    --lan-https) LAN_HTTPS=1;shift ;;
    --no-mcp) MCP_READONLY=0;shift ;;
    --no-cert-monitor) CERT_MONITOR=0;shift ;;
    --origin-prepared) ORIGIN_PREPARED=1;shift ;;
    --apply) APPLY=1;shift ;;
    *) echo "Неизвестная опция: $1" >&2;exit 2 ;;
  esac
done
case "$PROFILE" in home-static|home-dynamic|vps) ;; *) echo 'Требуется --profile home-static, home-dynamic или vps' >&2; exit 2;; esac
if [[ -n "$DOMAIN" && -z "$PUBLIC_IP" ]];then echo 'Для домена нужен --public-ip' >&2;exit 2;fi
if [[ "$PROFILE" == home-dynamic && -n "$DOMAIN" ]];then
  echo 'Профиль home-dynamic/CGNAT не создаёт публичный сайт без подтверждённого ingress/VPS' >&2;exit 2
fi
if ((LAN_HTTPS)) && [[ -z "$LAN_IP" || -z "$LAN_CIDR" ]];then
  echo 'Для LAN TLS нужны одновременно --lan-ip и --lan-cidr' >&2;exit 2
fi
if [[ "$PROFILE" == vps && "$LAN_HTTPS" == 1 ]];then
  echo 'На VPS локальный школьный HTTPS не создаётся автоматически' >&2;exit 2
fi
python3 "$ROOT/scripts/serverctl.py" plan --profile "$PROFILE"
if [[ -n "$PUBLIC_IP" && "$PROFILE" != home-dynamic ]];then
  python3 "$ROOT/scripts/public-ipctl.py" plan --public-ip "$PUBLIC_IP"
fi
if [[ -n "$DOMAIN" ]];then
  python3 "$ROOT/scripts/domainctl.py" plan --domain "$DOMAIN" --public-ip "$PUBLIC_IP"
fi
if ((LAN_HTTPS));then
  bash "$ROOT/scripts/lan-https.sh" --lan-ip "$LAN_IP" --lan-cidr "$LAN_CIDR"
fi
if ((MCP_READONLY));then echo 'ПЛАН: локальный readonly MCP 127.0.0.1:39443 (без публичного входа)';fi
if ((CERT_MONITOR)) && [[ -n "$PUBLIC_IP" && "$PROFILE" != home-dynamic ]];then
  bash "$ROOT/scripts/install-cert-monitor.sh" --host "$PUBLIC_IP"
fi
if (( ! APPLY ));then
  echo 'РЕЖИМ ПРОСМОТРА: никаких настроек не изменено; --apply после проверок.'
  exit 0
fi
[[ "$EUID" -eq 0 ]] || { echo 'Требуется root' >&2;exit 1; }
if [[ -n "$PUBLIC_IP" && "$PROFILE" != home-dynamic && "$ORIGIN_PREPARED" != 1 ]];then
  echo 'Блокировка: origin нового домена должен быть предварительно разрешён ASA Lab (--origin-prepared)' >&2
  exit 1
fi
[[ -s /var/lib/server-settings/1panel-api.key ]] || { echo 'Нет API-ключа: сначала выполните store-panel-key.py' >&2; exit 1; }
command -v curl >/dev/null || { echo 'Не установлен curl' >&2;exit 1; }
curl -fsS --connect-timeout 3 --max-time 5 http://127.0.0.1:4610/health/ready >/dev/null || {
  echo 'ОШИБКА: внутреннее приложение отсутствует. Сначала восстановите ASA Lab и её базу из проверенного снимка.' >&2
  exit 1
}
# Предварительный отчёт API, без вывода ключей.
python3 "$ROOT/scripts/panelctl.py" inspect
if [[ "$PROFILE" != home-dynamic ]];then
  python3 "$ROOT/scripts/panelctl.py" install-openresty --apply
fi
if [[ -n "$PUBLIC_IP" && "$PROFILE" != home-dynamic ]];then
  python3 "$ROOT/scripts/public-ipctl.py" apply --public-ip "$PUBLIC_IP" --origin-prepared
fi
if [[ -n "$DOMAIN" ]];then
  python3 "$ROOT/scripts/domainctl.py" apply --domain "$DOMAIN" --public-ip "$PUBLIC_IP" --origin-prepared
fi
if ((LAN_HTTPS));then
  bash "$ROOT/scripts/lan-https.sh" --lan-ip "$LAN_IP" --lan-cidr "$LAN_CIDR" --apply
fi
if ((MCP_READONLY));then
  if ! systemctl is-active --quiet server-settings-1panel-mcp.service;then
    [[ ! -e /etc/systemd/system/server-settings-1panel-mcp.service ]] || { echo 'Есть сломанная MCP служба: требуется ремонт, не перезаписываю' >&2;exit 1; }
    bash "$ROOT/scripts/setup-mcp.sh" --readonly --apply
  else
    echo 'MCP уже запущен: повторную установку не выполняю'
  fi
fi
if ((CERT_MONITOR)) && [[ -n "$PUBLIC_IP" && "$PROFILE" != home-dynamic ]];then
  if [[ ! -e /etc/systemd/system/server-settings-cert-check.service ]];then
    bash "$ROOT/scripts/install-cert-monitor.sh" --host "$PUBLIC_IP" --apply
  else
    echo 'Монитор срока SSL уже установлен; параметры проверяйте вручную'
  fi
fi
python3 "$ROOT/scripts/panelctl.py" inspect
printf '%s\n' 'Второй этап завершён. Финальная приемка: публичный HTTPS с внешней сети, TLS-сертификат, логин/CSRF, проекты, Scratch, reboot.'
