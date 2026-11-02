# Восстановление подключения 1Panel — 2 ноября 2026

Целевой узел admins-MS-7D22 /192.168.1.115 подтверждён hostname и закреплённым SSH host key. Это исправление доступа, не повторная сертификация боевой готовности.

## Причины и выполненное

1. **1Panel не остановлена:** core/agent active, порт19192 доступен из D2 LAN, авторизованный dashboard MCP code200. Панель имеет защищённый entrance; запрос корня не доказывает доступность страницы входа.
2. **Домен ещё не развёрнут:**1panel.asa-lab.ru теперь разрешается в94.29.44.211, но среди4 сайтов и4 сертификатов панели соответствующего имени нет. TLS-доступ по этому имени не принят.19192 с публичногоIP timeout при проверке с D2 — это не независимый внешний pentest. Ничего в DNS/firewall/NAT не изменялось.
3. **Административный MCP сломан ACL:** /var/lib/server-settings root0700 содержал user:admins:--x, но mask::--- делал разрешение неэффективным. В результате asa_admin_status/operation_list/execute возвращали internal error. Восстановлен только named-user traversal без чтения списка каталога. У admins есть доступ к своему private state; nobody traversal denied. Причина изменения mask не установлена; chmod0700 родителя повторно обнулит mask. Root-secret files остаются0600. Без перезапуска служб.
4. **Старый BUSY после восстановления прав:** операция f9f1c8a77cf347d4970142f6978b1e71 от29октября имела worker_lost/recovery_required. Через штатный asa_admin_status прочитана завершённая root-квитанция exit0 и статус согласован. Старые команды/Tailscale-изменения не повторялись. Затем новая read-only root-команда7c67e7caf6b04eea9a546dc5795b10b7 завершилась success/UID0, panel units active, MCP state readable.
5. **Защищённый вход на D2:** новый SSH ed25519 ключ владельца для существующего admins; это полноценный ключ владельца с прежними правами admins, не изолированная tunnel-only роль. Приватный ключ и connection/entrance находятся только в закрытом локальном каталоге. Windows OpenSSH проверяет заранее известный host key, BatchMode/IdentitiesOnly; keepalive30/3. Туннель слушает только127.0.0.1:11992 и направляется к серверному127.0.0.1:19192. Парольный SSH пока не отключён, sshd не перезагружался. Этот LAN endpoint работает с D2 в доступной сети, не обещает доступ из любой внешней сети.
6. На рабочем столе D2 создан ярлык **«1Panel — тестовый сервер»**: запускает/проверяет туннель и открывает private entrance. Реальный вход по новому ключу проверен (`id -u`1000), HTTP через туннель200; в Chrome видна форма1Panel с русским интерфейссом. Ввод пароля и принятие лицензионного соглашения не выполнялись: вход в административную сессию этим тестом не подтверждён. HTTP указан только для loopback браузера; сетевой участок D2→Ubuntu зашифрован SSH, это не новый HTTPS-домен.

## Повторение и откат

```bash
sudo python3 scripts/repair-asa-state-access.py
sudo python3 scripts/repair-asa-state-access.py --apply --expected-host ACTUAL_HOSTNAME
```

Скрипт не меняет владельцев/содержимое БД и не перезапускает сервисы. Проверяет root-controlled config, прямой private state выбранного owner и допустимый узкийACL. Неожиданные дополнительные ACL требуют ручного разбора: нельзя автоматически разблокировать чужие masked grants. Исходный ACL хранится root-only в /var/backups/server-settings-acl/state-parent-before-repair.acl; откат `setfacl --restore=...` вернёт прежний доступ и снова заблокирует MCP.

Для другого компьютера отдельно создать/установить ключ владельца, проверить SSH host key через доверенный канал и сохранить private JSON (вне Git): host,user,key_file,known_hosts,local_port,entry_path. Приватный каталог Windows должен быть доступен только владельцу/SYSTEM/Administrators. Не использовать StrictHostKeyChecking=no. Затем:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File scripts/panel-ssh-tunnel.ps1 -ConnectionFile PRIVATE_CONNECTION_JSON -OpenPanel
```

Для отката туннеля остановить только соответствующий ssh.exe и удалить только новую строку ключа владельца из authorized_keys. Не останавливать общий SSH/Docker/Tailscale. Ярлык/приватные локальные файлы можно удалить после закрытия туннеля. Старые ключи/пароли не удалялись.

## Проверки и остатки

Python compile,4 focused ACL tests, PowerShell parser, diffcheck PASS; реальная ACL/private-state проверка и native key authentication/tunneledHTTP/MCP root command PASS. Defaultplan не выполняет изменениеACL. Публичные адресаSSH/панели не открывались; OpenResty/ASA/БД не перезапускались. Полноценный приёмочный вход в1Panel, внешний Tailscale с D2 (локальный socket PermissionError), HTTPS для1panel.asa-lab.ru и обновлённый production audit остаются отдельными непроверенными пунктами.

Дополнительно найден сертификат публичногоIP94.29.44.211 с expireDate2026-11-06; продление требуется проконтролировать отдельно, в этой работе сертификаты не менялись. Сертификатtest.asa-lab.ru в API до2027-01-17.

Основание: [официальные настройки1Panel](https://1panel.pro/docs/v2/user_manual/settings/) — security entrance, domain binding, panelTLS и ограничения адресов являются разными настройками.
