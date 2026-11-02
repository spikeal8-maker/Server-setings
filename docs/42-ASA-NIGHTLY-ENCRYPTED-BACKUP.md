# Ежедневная зашифрованная копия ASA: кандидат, а не принятая установка

Этот срез добавляет исходный код и unit-проверки. На сервере таймер этим срезом **не установлен и не включён**, реальная копия/расшифрование/полное восстановление не выполнялись. Включение возможно только после независимого review точного SHA и отдельно принятой репетиции восстановления.

## Порядок работы

`nightly-asa-backup.py schedule --apply` обращается к существующему локальному MCP с приватным токеном. HTTP fixed loopback, environment proxies и redirects отключены. `asa_admin_execute` получает idempotency key из московской даты и UUID конкретной установки. После потери ответа не запускается обходной backup: оператор проверяет журнал и повторяет только ту же плановую заявку. MCP Store исключает обычное обновление той же установки; сам канонический ASA manager использует `.asa/operation.lock`. Полный root-оператор по-прежнему способен обойти эти соглашения: это не sandbox.

Root-исполнитель вызывает канонический `tools/asa_manager.py backup` **от имени владельца admins**, сохраняя правильного владельца maintenance-state и доступ к Docker. Менеджер ненадолго останавливает writers для согласованного PostgreSQL+MinIO снимка и возобновляет именно старые контейнеры в finally. Не выполняются build/update/migration или перенос живой БД. После экспорта повторяется штатный `verify-backup`.

Проверенный каталог с дампом, объектами, приватной конфигурацией и manifest архивируется, шифруется age с **публичным** recipient из `/etc/server-settings/age-recipients.txt`. Приватный identity остаётся у владельца вне сервера. Шифрование сначала пишет root-only файл; в container-writable spool попадает только готовый ciphertext через exclusive/no-follow descriptor. Затем уже установленный cloud transport отправляет `.age` в отдельный namespace, скачивает его заново и сверяет SHA256. Это проверка ciphertext/транспорта, **не расшифрование и не restore**.

Plaintext staging создаётся только для текущего запуска под каноническими backups с mode0700. Он удаляется лишь после успешной доставки и round-trip; при сбое сохраняется для разбора, но не загружается в облако. Прежние backups и облачные архивы не удаляются. Encrypted spool/копии сохраняются; автоматическая retention пока отсутствует. При свободном месте менее20GiB запуск блокируется и помечается ошибкой. Следовательно, storage-retention и независимая доставка оповещений остаются отдельной эксплуатационной приёмкой.

## Приватная конфигурация

Оператор после review создаёт root-owned0600 `/etc/server-settings/asa-nightly-backup.json`:

```json
{
  "hostname": "ACTUAL_HOSTNAME",
  "root": "/srv/asa-lab",
  "installation_id": "asa-test",
  "installation_instance_id": "ACTUAL_32_HEX_INSTALLATION_UUID",
  "project": "asa-lab-dev",
  "profile": "production",
  "owner_uid": 1000,
  "cloud_project": "asa-test-nightly"
}
```

Значения берутся из фактической канонической установки; не переносить UUID на другой сервер. Код ограничен текущим installation/project/profile; другой продукт требует отдельного адаптера. Публичный recipient provisioning выполняет оператор отдельно: при существующем отличающемся recipient требуется разбор, а не молчаливая замена. Root helper должен оставаться root-owned на защищённом полном parent chain. Новые OAuth-клиенты/пароли не создаются.

## Установка и активация

```bash
# Планы без изменений
python3 scripts/nightly-asa-backup.py run
python3 scripts/install-nightly-asa-backup.py

# Установить code/systemd units без enable таймера
sudo python3 scripts/install-nightly-asa-backup.py --apply \
  --expected-host ACTUAL_HOSTNAME --source-id REVIEWED_40_HEX_SHA
```

Установщик размещает версионный runner в root-controlled `/opt/server-settings/asa-nightly-backup/SHA/`. Он не перезапускает приложение, Docker или SSH. Если прежний timer уже enabled, обновление без явной новой приёмки блокируется **до изменения старых units**. Повтор той же версии возможен лишь при совпадении root-owned bytes.

Первый настоящий backup инициировать существующим `asa_admin_execute` с командой `python3 INSTALLED_RUNNER run --apply`, тем самым сохранив MCP gate. Приватные log/receipt находятся в `/var/lib/asa-nightly-backup`; `status` требует свежую успешную копию не старше26часов, ту же UUID-установку и отсутствие более нового failure. Ошибка сервиса и private failure marker — локальная диагностика; email/чат/внешнее уведомление не отправляются и не считаются принятыми.

До enable нужно скачать и расшифровать конкретную копию внешним ключом владельца; восстановить её в отдельной network-isolated PostgreSQL и клоне MinIO, проверить данные/объекты/права и пользовательские проверки. Не использовать работающую БД как restore target и не считать `pg_restore --list` репетицией. Следующий ограниченный исполнитель должен реализовать/проверить этот отдельный isolated restore; текущий срез не выполняет его.

Root-owned0600 receipt принятой репетиции содержит hostname, installation_instance_id, archive_sha256, accepted_at с timezone и четыре true-поля: decryption_verified, postgres_restored, objects_verified, restored_account_project_checks. SHA должен совпасть с `last-success.json`, срок receipt не более7дней. Это доверенное операторское свидетельство, а не защита от root-фальсификации.

```bash
sudo python3 scripts/install-nightly-asa-backup.py --apply --enable-timer \
  --expected-host ACTUAL_HOSTNAME --source-id REVIEWED_40_HEX_SHA \
  --restore-receipt ROOT_PRIVATE_ACCEPTED_RECEIPT
```

Расписание02:30 Europe/Moscow, Persistent=true: пропущенный запуск может стартовать после следующей загрузки. Обычный update защищён MCP/manager locks; произвольные root-команды требуют координации владельца. Таймер не включается без receipt. Откат: остановить/отключить только `asa-nightly-backup.timer`, не удалять backups/квитанции и не прерывать активный worker вслепую.

## Проверки кандидата

Windows focused:11тестов,10PASS/1POSIX-onlySKIP; Linux CI должен выполнить signal/owner test. Проверены планы без мутаций, MCP-idempotency, отсутствие обхода BUSY/recovery_required, запрет redirects, соответствие/полнота/срок restore-receipt. Pythoncompile и diffcheck PASS. Эти проверки не заменяют live backup, crypto-decrypt round-trip, isolated restore или реальное включение timer.
