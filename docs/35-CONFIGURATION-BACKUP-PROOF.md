# Проверенная зашифрованная копия конфигурации

2 ноября 2026 года существующим скриптом `scripts/backup-settings.sh --apply` создан `server-settings-20261102T191428Z.tar.age` (18 088 440 байт). Это копия выбранной конфигурации и консистентных SQLite-снимков панели, **не** полный backup ASA/PostgreSQL/MinIO и не доказательство восстановления целого сервера.

Публичный age recipient выведен из внешнего ключа владельца на D2 и сохранён на Ubuntu в `/etc/server-settings/age-recipients.txt`, root0600. Приватный identity на Ubuntu не передавался. Значение recipient и тем более приватный ключ не публикуются в Git.

Существующий MCP cloud transport отправил ciphertext в namespace `Server-Backups/server-settings`: upload operation `263d7164ceeb44c58c8276104a7f04b0`, download operation `16bce0fbb3ac4b6f9f881af0b2ddd3d7`. Контрольная сумма SHA256 исходного и скачанного файла:

`6c2b3632aaf765a315a3a27d8f5ea4970a79b0b780396299ff6ab121fe52784f`

Исходный ciphertext также скачан на D2 и реально расшифрован внешним USB identity: age exit0, tar прочитан в памяти,111 записей, присутствуют `db/core.db` и `metadata/created.txt`. Расшифрованные секреты не выводились в отчёт и не загружались на GitHub. Совпадение SHA скачанной облачной копии связывает её с проверенным ciphertext; это не тест фактического применения конфигурации на новой машине.

## Повторение

1. Проверить свой публичный recipient и root-only файл `/etc/server-settings/age-recipients.txt`; существующий отличающийся recipient не перезаписывать молча.
2. Выполнить план `sudo bash scripts/backup-settings.sh`, затем после проверки `sudo bash scripts/backup-settings.sh --apply`.
3. Передать только `.tar.age` в отдельный namespace существующим cloud upload; скачать как новый файл и сравнить SHA256. Прежние архивы не удалять.
4. На машине владельца проверить `age -d -i PRIVATE_IDENTITY ENCRYPTED_FILE` в закрытом локальном каталоге. Не копировать identity на сервер и не передавать значение ключа аргументом или в чат.

После этой проверки отдельно выполнен первый полный ASA backup через кандидат nightly runner. У него другая квитанция, отдельный архив и отдельная обязательная isolated-restore приёмка; данный документ не подменяет её. Регулярный таймер на этом этапе не включён.
