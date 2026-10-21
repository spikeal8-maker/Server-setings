# Чистая переустановка ASA на тестовой Ubuntu, 2026-10-21

Владелец явно потребовал удалить текущий проект вместе с данными без свежего backup. ОС, 1Panel, OpenResty, MCP gateway, сеть и остальные проекты сохраняются. Это исключение относится только к указанной установке; не применять к школьному серверу.

## Закреплённые входы

- Узел admins-MS-7D22 / 192.168.1.115; проект asa-lab-dev; корень /srv/asa-lab.
- Последний опубликованный выпуск с проверенным CI: 6e80e3f3e87a8f815ac6b254fd0b3ffa942db2d3, схема234. Произвольный main не используется.
- Архив из локальной синхронизированной папки Яндекс Диска: asa-lab-20261017T231505Z-65b68074.tar.age, SHA256 910795bae418b265ac37a8d9ac84e22a92e9c2c870ef11138de4cbeffb7b41e4.
- Расшифрованный каталог /srv/asa-restore-snapshots/20261017T231505Z-65b68074; SHA256 manifest.json 8be098e3c37cd43fb704e7f92f300574bb685445d92697223ba0b158ac2e8a01.
- Листинг облака напрямую не проверялся; это согласованный снимок, а не утверждение о свежести всех облачных файлов.

## Подготовка и проверка до удаления

В /srv/asa-reinstall-20261021/fresh-source нужен чистый checkout закреплённого SHA из официального spikeal8-maker/asa-lab и приватный .env (0600). release.json получается штатным discover_release; assert_ci обязан пройти. fetch_images загружает digest-пины и images-receipt.json фиксирует passed и SHA. Образы не пересобираются из произвольного дерева.

Приватные integration/Scratch/MinIO ключи сохраняются из private.env архива. POSTGRES_PASSWORD и ASA_APP_DB_PASSWORD генерируются заново; все внутренние DB URL согласуются с ними. Исторический CLASSROOM_CODE_SECRET задаётся явно из архивного CLASSROOM_CODE_SECRET либо сырого URL.password архивного APP_DATABASE_URL (без percent decoding), чтобы не изменить derivation старых кодов. Он независим от новых DB паролей. Новые независимые encryption/lookup keyrings StudentCode включаются в compat только после подтверждения отсутствия защищённых исторических значений. Приватный .env не публикуется. Не передавать quoted Compose .env в docker run --env-file: для проверки передавать распарсенные значения через окружение и -e KEYNAME.

Репетиция: штатный restore_check создаёт отдельную *_test БД с ACL; затем tools/migrate.mjs из закреплённого API образа выполняет plan/apply/plan на ней. Не запускать provision-runtime-role против действующего PG: он изменит пароль глобальной роли. Проверить количество записей, max(version) как число, отсутствие исторических ciphertext без старых ключей, валидатор credential-configuration.cjs в NODE_ENV=production и неизменность текущих контейнеров. После успеха удалить только собственную временную БД и записать clean-install-receipt.json phase=rehearsed с revision/schema/credential_config. Квитанция не является пользовательской приёмкой.

Реально выполнено: проверены2062 файла; восстановление212 → миграции234 прошло; 90 аккаунтов,31 класс,418 мест,1414 проектов. Все6 ciphertext колонок пусты; новый production validator прошёл. Временная БД удалена, действующие контейнеры при репетиции не менялись.

## Исполнение через SSH и API 1Panel

Скопировать scripts/asa-clean-reinstall.py на сервер вне /srv/asa-lab. Скрипт работает как root, по умолчанию только план:

```bash
python3 /srv/asa-reinstall-20261021/asa-clean-reinstall.py
python3 /srv/asa-reinstall-20261021/asa-clean-reinstall.py --phase erase --apply
python3 /srv/asa-reinstall-20261021/asa-clean-reinstall.py --phase bootstrap --apply
python3 /srv/asa-reinstall-20261021/asa-clean-reinstall.py --phase restore --apply
python3 /srv/asa-reinstall-20261021/asa-clean-reinstall.py --phase objects --apply
python3 /srv/asa-reinstall-20261021/asa-clean-reinstall.py --phase activate --apply
python3 /srv/asa-reinstall-20261021/asa-clean-reinstall.py --phase verify --apply
```

Это адресный оператор конкретного согласованного сброса, а не универсальный установщик. Он проверяет hostname, SHA checkout/manifest, точные7 сервисов, provenance путей/Compose, метки2 томов и отсутствие чужих потребителей. Удаляет контейнеры через API1Panel, затем только2 доказанных неприсоединённых тома и точный корень проекта. Глобальный prune/down-v не применяется.

Перед удалением блокируются канонический operation.lock и внешний reinstall.lock; проверяется отсутствие queued/running MCP работ, останавливается только asa-operations.service и повторно проверяется отсутствие detached worker/manager. Между фазами сервис остаётся остановленным. Hardlink сохраняет inode operation.lock при замене корня. После технической проверки сервис запускается. При отказе он остаётся в обслуживании; нельзя автоматически переигрывать restore/erase или удалять оставшуюся БД наугад. Проверить квитанцию и восстановить выполнение по фактическому состоянию.

Bootstrap PostgreSQL создаётся через API1Panel, данные восстанавливаются штатным restore_check, миграции проверяются повторно, затем новая БД получает имя asalab. MinIO копируется в новый пустой том и проверяется побайтовыми SHA256 всех файлов. Полный стек запускается API1Panel с canonical compose.yaml + compose.production.yaml и закрепляется в панели. Служебная bootstrap-конфигурация удаляется. Разработческий профиль вместо production недопустим.

## Границы доказательств

На момент подготовки: очистка ещё не запущена. Требуются фактическая техническая проверка и пользовательские teacher/student вход, Scratch save/reopen; до этого user_flow=NOT_RUN. Проверка этого сброса не доказывает установку на второй чистой ОС и не включает неслитый PR680 о wildcard доменах.
