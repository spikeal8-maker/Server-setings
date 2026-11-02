# Школьный PVE: офлайн-подготовка резервного интернета

Этап1 — исходный кандидат. Автоматическое переключение, подключение оборудования и испытание отказа ещё НЕ реализованы этим этапом. Он не заменяет сетевое управление Proxmox и не создаёт службы/таймеры. Основная сеть, маршруты, DNS, Docker и SSH не перенастраиваются. Владелец пока физически отсутствует; готовность Wi-Fi/USB/SIM не подтверждена.

Наблюдаемый узел: PVE9.2.24, kernel7.0.2-6-pve, ifupdown2, vmbr0 со школьным статическим адресом172.23.104.170/23 через nic1 и gateway172.23.104.1. Свободные nic0/nic2/nic3; USB Wi-Fi mt76x0u обнаружен, но DOWN; модем отсутствует. Единственный resolver10.167.146.13 зависит от школьной сети. Это требует отдельной проверенной DNS-транзакции при будущем переключении, а не только смены маршрута.

Установка CLI после review точного SHA: перенести **два точных Git blob** в root-owned каталог без symlink/group-other-write предков. `python3 scripts/install-school-internet-offline.py` показывает план без чтения приватных файлов. На школьном PVE: `sudo python3 scripts/install-school-internet-offline.py --apply --source-sha REVIEWED_40_HEX_SHA`. Установщик проверяет root/hostname, доверенные пути и baseline основной конфигурации. Он создаёт только версионный CLI под `/opt/server-settings/school-internet-offline/<SHA>/` и `/usr/local/sbin/asa-school-internet`. Ничего не запускает и пакеты не устанавливает. Обновление чужого wrapper или изменённого версионного файла запрещено. Откат CLI — повторная установка ранее проверенной версии, без удаления профилей и без сетевых команд.

Локально на PVE:

```console
sudo asa-school-internet status
sudo asa-school-internet setup
sudo asa-school-internet setup --apply
sudo asa-school-internet packages-plan
sudo asa-school-internet packages-apply --apply
```

`setup` без `--apply` — план. Интерактивный `setup --apply` сохраняет Wi-Fi SSID и скрытый пароль, выбранный свободный Ethernet nic или APN. Пароль никогда не передаётся через argv и не печатается. Секреты сохраняются только `/etc/asa-school-internet/profiles.json` root0600, каталог root0700; локальное обновление защищено flock и atomic fsync. Один профиль на mode; stable profile ID — mode. Формат `{schema:1, armed:false, profiles:[...]}`, варианты `{mode:wifi,ssid,password,qualified:false}`, `{mode:router,interface:nic0|nic2|nic3,qualified:false}`, `{mode:modem,apn,qualified:false}`. Это JSON, не shell/wpa configuration: ввод не интерпретируется как команды. SIM PIN не запрашивается, не угадывается и не передаётся argv. Заблокированная SIM требует отдельной безопасной локальной регистрации; открытые Wi-Fi этим этапом не поддерживаются.

Подготовка пакетов закрепляет пять целевых имён: iw, wpasupplicant, modemmanager, usb-modeswitch, mobile-broadband-provider-info. Сначала APT simulation: новые неизвестные зависимости/любое обновление/удаление требуют нового review. Actual install использует --no-upgrade --no-remove --no-install-recommends и SYSTEMD_OFFLINE=1, без глобального policy-rc.d, без NetworkManager. Если MM/wpa units уже существуют, остановка не разрешается: STOP. Только вновь введённые ModemManager.service/wpa_supplicant.service отключаются после установки. Файлы основной сети и resolver сверяются по исходным SHA, routes/rules сравниваются до/после; приватные квитанции сохраняются в том же root-only каталоге. Ошибка APT/проверки не выдаётся за успех и не лечится автоматической правкой чужой сети. Не включать автоматический запуск служб.

`arm` всегда завершится STOP: это сознательная граница этапа1. Не считать установленные пакеты или сохранённые профили рабочим failover. Следующий отдельный срез должен реализовать IPv4 policy tables/source-pin, отдельный DHCP hook без stock DNS/routes, подтверждённые source/table TLS probes независимых ресурсов, hysteresis/failback, DNS CAS с сохранением исходных байт и метаданных, flock/journal/watchdog rollback и namespace-тесты. Нельзя включать manager/timer до физической проверки резервного интернета и нового SSH/BMC-подключения. Primary management не сохраняется волшебным образом при физическом исчезновении школьной LAN.

Техническая основа policy routing: [iproute2 ip-rule](https://github.com/iproute2/iproute2/blob/main/man/man8/ip-rule.8.in); Wi-Fi/DHCP без NetworkManager: [Debian WiFi](https://wiki.debian.org/WiFi/HowToUse); режим offline systemctl: [systemd ENVIRONMENT](https://github.com/systemd/systemd/blob/main/docs/ENVIRONMENT.md). Эти источники подтверждают отдельные механизмы, не являются доказательством испытания нашего сервера.

Проверки кандидата: 11 focused unit tests PASS на Windows (секреты/инъекции/план/armingSTOP/package pin+offline flags/trust-chain). Namespace и фактическое оборудование этим этапом не тестировались. Закреплён наблюдаемый список22 новых пакетов с точными версиями; новые версии требуют нового review.
