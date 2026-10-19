#!/usr/bin/env bash
# Локальный офлайн HTTPS ASA Lab. Не является вторым публичным веб-сервером.
set -Eeuo pipefail
MODE=plan
LAN_IP=''
LAN_CIDR=''
LISTEN_PORT=4610
UPSTREAM_PORT=4610
while (($#)); do
  case "$1" in
    --apply) MODE=apply; shift ;;
    --lan-ip) [[ $# -ge 2 ]] || exit 2; LAN_IP="$2"; shift 2 ;;
    --lan-cidr) [[ $# -ge 2 ]] || exit 2; LAN_CIDR="$2"; shift 2 ;;
    --listen-port) [[ $# -ge 2 ]] || exit 2; LISTEN_PORT="$2"; shift 2 ;;
    --upstream-port) [[ $# -ge 2 ]] || exit 2; UPSTREAM_PORT="$2"; shift 2 ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$LAN_IP" ]] || { echo 'Укажите --lan-ip' >&2; exit 2; }
python3 - "$LAN_IP" "$LAN_CIDR" <<'PY'
import ipaddress,sys
ip=ipaddress.ip_address(sys.argv[1])
if ip.version!=4 or not ip.is_private or ip.is_loopback:raise SystemExit('Нужен частный IPv4 LAN')
if sys.argv[2]:
 network=ipaddress.ip_network(sys.argv[2],strict=False)
 if network.version!=4 or not network.is_private or ip not in network:
  raise SystemExit('LAN CIDR должен быть частной сетью, содержащей IP сервера')
PY
for p in "$LISTEN_PORT" "$UPSTREAM_PORT";do
  [[ "$p" =~ ^[0-9]+$ ]] && ((p>=1 && p<=65535)) || { echo 'Некорректный порт' >&2; exit 2; }
done
cat <<EOF
LAN HTTPS: https://$LAN_IP:$LISTEN_PORT → http://127.0.0.1:$UPSTREAM_PORT
Публичный ingress: исключительно 1Panel/OpenResty 80/443.
Локальный Caddy выпускает собственный внутренний CA; доверие CA требуется настроить на клиентах.
EOF
if [[ "$MODE" != apply ]];then
  echo 'Это план; никаких изменений в Ubuntu. Для нового сервера добавьте --apply.'
  exit 0
fi
[[ "$EUID" -eq 0 ]] || { echo 'Нужен root' >&2; exit 1; }
[[ -n "$LAN_CIDR" ]] || { echo 'Для --apply обязательно указать --lan-cidr' >&2; exit 1; }
command -v ufw >/dev/null || { echo 'UFW не установлен' >&2; exit 1; }
LC_ALL=C ufw status verbose | grep -q '^Status: active$' || { echo 'Сначала включите безопасный UFW' >&2; exit 1; }
LC_ALL=C ufw status verbose | grep -q '^Default: deny (incoming)' || { echo 'В UFW нет deny incoming' >&2; exit 1; }
ip -o -4 addr show | grep -Fq "inet $LAN_IP/" || { echo 'LAN IP отсутствует на Ubuntu' >&2; exit 1; }
[[ ! -e /etc/systemd/system/asa-lab-lan-https.service ]] || { echo 'Служба уже существует: не перезаписываю' >&2; exit 1; }
[[ ! -e /opt/asa-lan-https/Caddyfile ]] || { echo 'Конфигурация Caddy уже существует: не перезаписываю' >&2; exit 1; }
[[ ! -e /etc/systemd/system/asa-lab-public-ip.service ]] || { echo 'Обнаружен исторический публичный Caddy: сначала отдельный аудит' >&2; exit 1; }
command -v ss >/dev/null || { echo 'Требуется утилита ss' >&2; exit 1; }
if ss -H -ltn '( sport = :'"$LISTEN_PORT"' )' | grep -q .;then
  # Проверка выше увидит и backend на 127.0.0.1; он разрешён.
  if ss -H -ltn '( sport = :'"$LISTEN_PORT"' )' | grep -vE '127[.]0[.]0[.]1:'"$LISTEN_PORT"'[[:space:]]' | grep -q .; then
    echo 'Внешний адрес:порт уже занят; отказ от замены' >&2;exit 1
  fi
fi
if ! curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$UPSTREAM_PORT/health/ready" >/dev/null;then
  echo 'Внутренний backend ASA Lab не готов' >&2;exit 1
fi
if ! command -v caddy >/dev/null 2>&1;then
  # Маскируем пакетную caddy.service, чтобы apt не занял публичные 80/443.
  systemctl mask caddy.service
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends caddy
fi
CADDY_BIN="$(command -v caddy)"
CADDY_VERSION="$(caddy version)"
[[ -x "$CADDY_BIN" ]] || { echo 'Не удалось установить Caddy' >&2; exit 1; }
systemctl disable --now caddy.service 2>/dev/null || true
install -d -m 0755 /opt/asa-lan-https
cat > /opt/asa-lan-https/Caddyfile <<EOF
{
    admin off
    auto_https disable_redirects
    skip_install_trust
}
https://$LAN_IP:$LISTEN_PORT {
    bind $LAN_IP
    tls internal
    reverse_proxy 127.0.0.1:$UPSTREAM_PORT
}
EOF
chmod 0644 /opt/asa-lan-https/Caddyfile
"$CADDY_BIN" adapt --config /opt/asa-lan-https/Caddyfile --adapter caddyfile >/dev/null
cat > /etc/systemd/system/asa-lab-lan-https.service <<EOF
[Unit]
Description=ASA Lab — частный локальный HTTPS, не публичный Caddy
Wants=network-online.target
After=network-online.target docker.service
[Service]
Type=simple
ExecStart=$CADDY_BIN run --config /opt/asa-lan-https/Caddyfile --adapter caddyfile
DynamicUser=yes
StateDirectory=asa-lan-https
StateDirectoryMode=0700
Environment=XDG_DATA_HOME=/var/lib/asa-lan-https
Environment=XDG_CONFIG_HOME=/var/lib/asa-lan-https
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
Restart=on-failure
RestartSec=3s
[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/asa-lab-lan-https.service
systemctl daemon-reload
systemctl enable --now asa-lab-lan-https.service
systemctl is-active --quiet asa-lab-lan-https.service || { echo 'Ошибка запуска Caddy LAN' >&2; exit 1; }
LAN_IFACE="$(ip -o -4 addr show | awk -v ip="$LAN_IP" '$4 ~ ("^"ip"/") {print $2; exit}')"
[[ -n "$LAN_IFACE" ]] || { echo 'Не удалось определить LAN интерфейс' >&2; exit 1; }
ufw allow in on "$LAN_IFACE" from "$LAN_CIDR" to "$LAN_IP" port "$LISTEN_PORT" proto tcp
echo "CADDY_VERSION=$CADDY_VERSION"
echo 'Сервис создан; UFW разрешает порт только из указанной LAN CIDR. Проверьте TLS и сертификат CA.'
echo 'Корневая CA (после первого успешного запуска): /var/lib/asa-lan-https/caddy/pki/authorities/local/root.crt'
