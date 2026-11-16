# H03 — исправление порядка загрузки приватных FRP/Tailscale сокетов

Поручение: обеспечить воспроизводимый автозапуск всех систем тестового сервера ASA LAB после штатного обслуживания Ubuntu. Хост **admins-MS-7D22**, Ubuntu 24.04 LTS. **Школьная VM100 не менялась.**

## Проблема, обнаруженная 16 ноября 2026

При загрузке sockets.target был зарегистрирован цикл зависимостей:

- asa-frp-school-panel-tailnet.socket/start deleted to break ordering cycle
- asa-frp-school-ipmi-tailnet.socket/start deleted to break ordering cycle

Обе единицы имели WantedBy=sockets.target и After=tailscaled.service. Но FreeBind=yes позволяет заранее слушать закреплённый частный адрес даже до готовности Tailscale. Ненужный After=tailscaled.service создавал цикл; сокеты оставались enabled, но inactive.

Не путать с активными исходящими asa-frp-school-panel-client и asa-frp-school-ipmi-client: их состояние подтверждалось отдельно. Публичную 1Panel, Docker, маршрутизацию и порты не трогать.

## Принятая ручная поставка на H03

Оба root-owned socket unit изменены только в одном месте: After=tailscaled.service → After=sysinit.target. Проверены исходные hostname, ListenStream, FreeBind=yes, Service, NoDelay=true, owner, режим, отсутствие активного соединения. Рабочие FRP-клиенты и сам tailscaled не перезапускались.

Операция MCP: e84ef73195144a4b816a909c5b6ce6f7.
Защищённые оригиналы: /var/backups/asa-system-maintenance/frp-tailnet-boot-originals-20261116/.

Результат:

| Socket | Фактическое состояние | Адрес |
|---|---|---|
| asa-frp-school-panel-tailnet.socket | enabled, active; After=sysinit.target system.slice | 100.75.149.26:14630 |
| asa-frp-school-ipmi-tailnet.socket | enabled, active; After=sysinit.target system.slice | 100.75.149.26:14610 |

Реальный reboot пока **не выполнялся**. Состояние active/enabled и корректный порядок зависимостей не следует выдавать за принятие полного reboot-test или всех клиентских соединений.

## Переносимая реализация

Сценарий scripts/h03_frp_socket_boot_order.py:
- root + точный hostname;
- план без изменений по умолчанию;
- только два заранее известные сокета с явными адресами/службами;
- контроль типа/владельца/режима файлов и отсутствие неизвестного override;
- отказ при смешанном или неизвестном состоянии;
- защищённый архив до изменения;
- атомарная запись, daemon-reload, запуск только двух socket units;
- восстановление исходных unit при ошибке применения;
- повторный --apply на принятом состоянии — проверка без изменений.

Команды на тестовой Ubuntu из проверенного отдельного checkout Server-setings:

    python3 -B scripts/h03_frp_socket_boot_order.py
    python3 -B scripts/h03_frp_socket_boot_order.py --apply

Нельзя использовать этот скрипт на школьном Linux, Proxmox, Windows, VPS или других Tailscale адресах. Для них требуется собственный разбор зависимостей.

## Приёмка после перезагрузки

1. systemctl is-active tailscaled docker 1panel-core 1panel-agent asa-operations.
2. systemctl is-active asa-frp-school-panel-tailnet.socket asa-frp-school-ipmi-tailnet.socket.
3. systemctl show для обоих сокетов: After, ActiveState и UnitFileState; отсутствие After=tailscaled.service.
4. Подтвердить слушатели 100.75.149.26:14610 и :14630 и ответ авторизованного клиента из Tailscale, без раскрытия приватных логинов/ключей.
5. Внешний HTTPS test.asa-lab.ru, 1panel.asa-lab.ru, izo-test.asa-lab.ru; ASA LAB 6/6 healthy.
6. Проверить журнал загрузки на отсутствие новых ordering cycles для двух units.
7. При сбое использовать удалённую резервную консоль и исходные архивы, не менять домен и не сбрасывать SSH/FRP.

Связанные ограничения: обновление ядра 7.0.0-42→7.0.0-43 и Ubuntu 24.04→26.04 выполняются отдельными операциями, только после полного backup + restore и проверки резервного доступа.
