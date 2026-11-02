# Школьный PVE: кандидат IPv4 failover manager

SOURCE CANDIDATE, NOT INSTALLED / NOT ARMED. Домашний сервер и школьная сеть
этим срезом не изменялись. Runtime требует независимого review и namespace CI;
отсутствующие WiFi/SIM/USB устройства не объявляются подключёнными.

## Контракт и управление

Новые файлы только: `scripts/school_uplink_runtime`, `school-uplink-runtime.py`,
`asa-school-dhcp-hook`, `units/asa-school-uplink*`, отдельные tests/workflow.
Phase1 CLI/installer/setup files в этом срезе не меняются. Его arm пока STOP.
Интеграция dispatch/status допускается отдельным handoff после review phase1.

Исходный профиль `/etc/asa-school-internet/profiles.json`: schema1, armed:false,
one-per-mode router/wifi/modem, прежние exact fields. Config directory root0700,
file root0600. Runtime берёт общий `profile.lock`. Qualification, lease, journal
и heartbeat находятся только в `/var/lib/asa-school-internet` root0700/0600.
Private profile digest включает секреты и никогда не выводится/не публикуется.
`qualified:false` старого schema не является доказательством; runtime использует
своё реальное qualification proof, profile hash и MAC. `armed` — authoritative
kill switch. Только явный runtime arm меняет его на true; disarm возвращает false.

План: `school-uplink-runtime.py qualify --mode router` не применяет изменений.
После установки/review оператор использует `qualify --mode <mode> --apply`,
затем отдельный `arm --apply`. До этого автономный failover запрещён. Commands
status/qualify/arm/disarm/recover/watchdog/run требуют root и exact host pve.
Установщик этого среза не запускается и никакие units автоматически не включаются.
Для будущего install module должен находиться в `/usr/local/lib/asa-school-uplink`,
CLI рядом с ним с wrapper в `/usr/local/sbin/asa-school-uplink-runtime`, DHCP hook
в `/usr/local/libexec/asa-school-dhcp-hook`. Units требуют отдельной установки.

## Сеть и реальные probes

Main table сохраняет только исходные default via172.23.104.1 и connected
172.23.104.0/23 vmbr0 source172.23.104.170. Не переписываются primary address,
ifupdown2/interfaces, bridge, Docker, SSH, UFW, DNS upstream services или NetworkManager.
CLI требует уже существующий loose rp_filter2 и не меняет sysctl.

При квалификации свободный не enslaved интерфейс получает DHCP address с
noprefixroute. Tables21090/21091 и priorities10000/10005/10010/10020 должны быть
пусты до начала. Source172.23.104.170/32 закрепляется за management table21090;
backup source — за21091. При failover main suppress_prefixlength0 сохраняет все
specific routes, а unbound default выбирается через backup table21091.
Primary main default никогда не заменяется. Это failover IPv4 самого PVE;
не доказательство маршрутизации всех гостей через PVE и не IPv6 failover.

HTTPS probes: реальные CA-verified TLS к one.one.one.one/1.1.1.1 и
dns.google/8.8.8.8, с bind на точный IPv4 source и `ip route get ... from ...`
проверкой device/table. FRP/guest readiness не используется как Internet oracle.
Перед первым qualification обязательны также management-source TLS к BMC
172.23.104.234 с explicit SHA256 leaf pin9fed04…0c997 (self-signed pin, не CA)
и SSH banner TCP22 к LAN peer172.23.104.191. Уже qualified backup не блокируется
исчезновением BMC/LAN во время аварии.

Три плохих primary samples переключают на проверенный backup; пять хороших и
minimum60s hold возвращают primary. Сбой выбранного backup допускает только
другие previously-qualified неизменённые profiles/devices; новые не enrollятся.
Проверяется свежесть DHCP lease. Изменение адреса/маршрута/DNS lease требует
повторной квалификации, одинаковая renewal продлевает expiry.

## DHCP, WiFi и модем

dhclient запускается с собственным `-sf`: stock route/resolvconf scripts не
запускаются вообще. Hook только валидирует и сохраняет private proposal;
никаких ip/resolver команд из DHCP environment. Отбрасываются primary overlap,
чужой интерфейс/master, malformed/injected IP/mask/router/DNS, слишком короткие
lease и более одного gateway. No primary DHCP/interface takeover.

WiFi автоматически выбирает ровно один свободный wireless interface;
wpa_supplicant запускается напрямую с private hex-SSID/derived-PSK config,
без пароля в argv и без unmask/DBus activation stock services. Router использует
только nic0/nic2/nic3. Для modem автоматический путь ограничен USB Ethernet/
HiLink/tether (rndis_host/cdc_ether/cdc_ncm) с DHCP. Наличие APN не означает
qualify; APN не отправляется в modem. Raw serial/PPP/MBIM/locked SIM требуют
индивидуальной настройки, PIN не угадывается, ModemManager не unmaskится.

## DNS и восстановление

Исходный resolver — regular root0644, единственный nameserver10.167.146.13.
Перед сменой backup resolver проверяется source-bound DNS query. Сохраняются
точные bytes/mode/uid/gid/inode, write-ahead intent и owned replacement. Restore
выполняется только по совпадению owned state; foreign bytes/inode не затираются.
Ограничение: filesystem compare+replace не является kernel atomic CAS против
постороннего root writer, игнорирующего lock. Это требует отдельной оценки при
live review; нельзя заявлять исключение всех внешних гонок resolver.

Журнал inverse operations записывается до ip-команды; cleanup не flushит main,
таблицы/тома/интерфейсы других проектов. PID-file intents проверяют process
identity и command marker; daemon cleanup не использует killall. Watchdog
откатывает stale armed45s или unarmed qualification300s. Foreign routing/DNS
conflicts — STOP/owner action, а не автоматический repair primary.

## Доказательство

CI `School uplink runtime isolated namespaces` запускает root tests внутри
`unshare --net --mount-proc`; peer находится во втором disposable namespace.
Тесты используют настоящие ip rules/routes, TLS и DNS server: failover/failback,
management source pin, exact DNS restoration, foreign DNS preservation,
crash after partial route transaction, rogue DHCP rejection и WiFi injection.
DHCP input моделируется validated lease: это не hardware driver/carrier/SIM
qualification и не полноценный wire-level DHCP server test. No host network touched.

До зелёного exact-SHA CI / independent review все эти результаты PENDING.
Production apply, BMC leaf pin refresh, actual hardware qualification и owner
arm являются отдельными действиями. Current installed phase1 остаётся dormant.
