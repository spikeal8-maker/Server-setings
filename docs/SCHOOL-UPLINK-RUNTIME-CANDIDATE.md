# Школьный PVE: IPv4 uplink runtime, source checkpoint

Runtime source проверен в изолированных namespaces и установлен контроллером
в dormant состоянии на PVE. На физическом uplink оборудовании не квалифицирован,
НЕ armed. USB WiFi MT7610U/mt76x0u присутствует, но uplink/SSID не настроен; modem/SIM-router не квалифицированы. Main convergence один раз с phase1 main0378b6d.

## Проверки и границы доказательства

- Код feeb1f22366557871e7a033fec0a49bb93b5a460: 14/14 namespace tests PASS,
  22.045s, существующий WSL ASA-Lab-Ubuntu-Repro-20261018. NET+PID+MOUNT изолированы,
  /run — private tmpfs; Docker не используется, host systemd не вызывается.
- Контроллер независимо выполнил exact feeb1f2 14/14 на настоящем PVE,
  Python3.13/kernel7.0.2-6-pve, 20.367s (до этого be7a0f6:13/13,20.206s).
  Interfaces/resolver hashes, main routes и RPDB до/после точно совпали.
  Это namespace proof, НЕ переключение реального uplink.
- Installer contract tests 7/7 и phase1 compatibility 12/12 PASS; Python compile,
  YAML parse и diff check PASS. Plan installer не меняет систему.
- Exact-head GitHub CI и independent review проверяются по PR60; до их завершения
  кандидат не объявляется принятым для live apply. Реальная hardware qualification,
  radio/carrier/SIM, owner arm и физический reboot не проверялись.

## Owner CLI и dormant install

`install-school-uplink-runtime.py` без флага выводит план. После exact-SHA review:

```sh
python3 scripts/install-school-uplink-runtime.py --source-sha <reviewed-40-character-SHA> --apply
```

Вход — root-owned trusted staged source на exact host pve. Controller должен
сверить file hashes с Git blobs выбранного SHA; receipt сохраняет фактические
hashes и requested_reviewed_sha (label сам по себе не является аттестацией).
Installer проверяет неизменённый deployed phase1 c6e3aee source SHA2561dca342…8c67
и точный старый wrapper, копирует новый versioned bundle в /opt/server-settings,
использует compare-before-switch owner wrapper и отказывается от чужих файлов,
unit fragments/drop-ins. Старый c6 source не переписывается. Private step receipts
сохраняют original wrapper и последнюю завершённую стадию при частичном сбое.

Разрешено только additive wireless-regdb=2026.05.30-1~deb13u1: no upgrades/removals,
изменившийся apt plan блокирует применение. Никаких country/radio reload.
MM/WPA stock units остаются masked. Включается только новый watchdog timer;
manager service не включается/не запускается, profiles не создаются/не armed.

Owner wrapper `asa-school-internet` даёт menu/setup/status/qualify/arm/disarm.
Setup сохраняет private профиль и оставляет сеть выключенной; пароль скрыт.
Ровно один подтверждённый cable carrier свободного DOWN nic0/nic2/nic3 может
предлагаться автоматически; неоднозначность требует выбора из безопасного списка.
Qualify требует подключённого оборудования и --apply; arm — отдельное явное
действие после реального qualification. Arm делает enable--now только own manager,
а disarm disable--now. Recover при shutdown очищает свои ресурсы, сохраняя armed
intent; новый boot заново проверяет previously-qualified device/lease/TLS/DNS.
Watchdog oldboot cleanup также сохраняет intent независимо от порядка запуска.
Обычный stale daemon в текущем boot disarmится и откатывается.

## Config, policy и probes

/etc/asa-school-internet root0700, profiles.json root0600, schema1/armed boolean,
one-per-mode router/wifi/modem. Поле qualified:false legacy не принимается как
proof. Реальные private qualification hash/MAC/boot proof, leases и журнал —
/var/lib/asa-school-internet root0700/0600, без публикации credential digests.
Shared profile.lock сериализует операции. armed=false — authoritative kill switch.

Primary vmbr0 source172.23.104.170/23, connected172.23.104.0/23 и main default via
172.23.104.1 не переписываются. Interfaces file проверяется по исходному hash.
Пустые собственные tables21090/21091, priorities10000/10005/10010/10020 и source
management pin сохраняют SSH/LAN replies. Global main suppress_prefixlength0 и
backup default policy включаются только после failure hysteresis. Main specifics
сохраняются. Это IPv4 failover PVE host, НЕ доказательство guest routing/IPv6 failover.

Source-bound CA-verified HTTPS к one.one.one.one@1.1.1.1 и dns.google@8.8.8.8
с ip route get proof device/table. Оба должны пройти; corporate filtering одного
endpoint может помешать qualification/availability — явное ограничение политики.
FRP/гостевое приложение не является Internet oracle. Первый qualify дополнительно
требует source-bound TLS BMC172.23.104.234 с explicit self-signed leaf pin9fed04…0c997
и SSH banner LAN peer172.23.104.191. После qualification исчезновение LAN/BMC
не блокирует нужный failover на ранее проверенное оборудование.

Три плохих primary samples включают backup; пять хороших и minimum60s hold
возвращают primary. Только previously-qualified неизменённые profile/MAC допускают
авторотацию. Нормальные DHCP IP/gateway/DNS changes, EXPIRE и same-MAC USB replug
вызывают owned rollback/reacquire/TLS+DNS reprobe, без нового human/BMC enrollment.
Пока qualified device недоступен, armed intent ждёт его, не переписывая primary.
Lease/watchdog/hold используют boot-bound monotonic time, не wall-clock expiry.

## DHCP, WiFi, IPv6 и lifecycle

DHCP -sf полностью заменяет stock dhclient-script: hook только сохраняет validated
private proposal. Shell/IP/mask/router/DNS injection, primary subnet overlap,
foreign interfaces/master и unsafe lease отвергаются. Stock routes/resolv hooks
не запускаются. Никакого NetworkManager takeover/ifreload-all/global sysctl.

Только свободный DOWN не enslaved backup. До UP/association его собственные
accept_ra/autoconf/disable_ipv6 journalled и fenced; global/primary IPv6 не меняется.
Foreign/global IPv6 на выбранном интерфейсе блокирует takeover. Настройки этого
же name+ifindex+MAC+boot восстанавливаются при cleanup; replacement device не трогается.

WiFi — WPA2-Personal, hex SSID/derived PSK private config, без argv password;
WPA3-only не поддерживается и не обещается. USB modem — HiLink/Ethernet tether
через rndis_host/cdc_ether/cdc_ncm+DHCP; APN optional, настраивается в устройстве.
APN presence не квалифицирует modem. Raw PPP/MBIM/locked SIM — individual setup,
PIN не угадывается; MM не unmaskится.

До первого network effect требуется active/enabled scheduled независимый timer,
точные unit contents/fragments, отсутствие drop-ins. Write-ahead inverses до ip
команд; failed delete сохраняет undo/journal. Полные numeric rule/route attributes
проверяются до rollback, foreign state не удаляется. Process ownership — boot_id,
stable PID/starttime/executable и pidfd; stopped-child/PDEATHSIG barrier позволяет
journal ownership ДО exec, умаска0077/вывод devnull защищают файлы/логи.

DNS — regular root0644 с исходным sole nameserver10.167.146.13. Backup resolver
проверяется source-bound DNS query с проверкой ответа. Exact bytes/mode/uid/gid,
original/owned inode и write-ahead intent обеспечивают restore/retry после crash.
Foreign observed content не переписывается; already-restored exact original bytes
не требуют повторной записи. Ограничение: compare+replace не kernel atomic CAS
против внешнего root writer, игнорирующего lock; требуется review known-writers
перед live arm. DNS conflict — owner action, не первичная сетевая «починка».

Namespace suite включает реальные routes/TLS/DNS, management source pin,
failover/failback, renewal/expiry/replug, SIGKILL+independent watcher cleanup,
oldboot ordering/resume, PIDFD lifecycle, stale PID/reboot negatives, foreign
route/ifindex/ancestor guards, DNS CAS и rogue DHCP/WiFi input. DHCP lease acquisition
моделируется, wire-level DHCP/radio/SIM остаются обязательной физической приёмкой.

## Контроллер: live dormant acceptance feeb1f2 (2026-11-03)

Independent review разрешил dormant install. Durable job: Result=success,
ExecMainStatus=0; receipt accepted. Watchdog timer enabled/active; manager
disabled/inactive; ModemManager/wpa_supplicant masked/inactive. Status:
armed=false, phase=dormant, hardware_qualified=false, configured_modes=[].
Profiles.json и journal.json отсутствуют. wireless-regdb2026.05.30-1~deb13u1
установлен; country/radio/network reload не выполнялись.

Primary interfaces SHA256e676d55c7ff7e8eaa3cda0896f3caac4209ebe0fa4a56d3c6907a8bc268f0aba
и resolver SHA2563f4c6ebc4d48dbfbd3312cbd06e1a102a0d6767b1e64a3a9949664f24a1af52b
не изменились; main routes/RPDB прежние, pveproxy/pvedaemon/ssh active.
Post checks контроллера: public school panelHTTPS200/TLS1.3, unauthAPI401;
x/test2 ready200; VM100 asa-school-01 FRP/ssh/MCP active, loopbackMCP unauth401.
FRP NRestarts1 остался baseline, FRP не перезапускался.

Все live наблюдения переданы контроллером; этот source executor не применял
сетевые изменения. Hardware qualification/arm/реальный reboot остаются отдельной
будущей приёмкой владельца. GitHub exact-head CI может ещё ожидать runner;
локальный CI-equivalent фиксируется отдельно, а queued не называется PASS.

## Локальный полный static CI-equivalent

Git source archive494d069 (runtime codefeeb1f2) проверен в disposable WSL
NET+PID+MOUNT/private-run namespace. 460 tests run: **459 PASS, 1 conditional SKIP**,
14.789s. Единственный skip: test_real_canonical_dependency_closure_and_import,
требует ASA_CANONICAL_SOURCE_REPO с pinned ASA runtime checkout; CI по умолчанию
также не задаёт этот внешний fixture. Он не называется проверенным restore.

Bash syntax, compileall scripts/tests, home-static/home-dynamic/vps plan и
git diff --check HEAD — PASS. В ephemeral venv установлен exact PyYAML6.0.3
из offline wheel, без глобальной установки пакетов WSL. Отсутствующий ensurepip
обойдён venv --without-pip + ephemeral pip wheel, не изменением исходных tests.

Runner исправлен до повторного gate: Windows Git archive с autocrlf=true менял
Git LF blobs в CRLF (pin fixture закономерно падал), поэтому archive создан с
core.autocrlf=false и побайтно сверен с Git blobs; temporary Git также не меняет
line endings. Дополнительный parent shell воспроизводит обычную CI process
ancestry вместо namespace PID1 для self-admin fixtures. Ни один тест не исключён
и source pin/guard не ослаблен. GitHub workflows ещё queued, это отдельное
состояние, а не удалённый CI PASS. Последующие commits — только proof metadata.
