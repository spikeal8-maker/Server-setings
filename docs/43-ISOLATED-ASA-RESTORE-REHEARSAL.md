# Изолированная проверка восстановления ASA

Статус: SOURCE_ONLY. Реальный запуск не выполнялся; nightly timer не включён.

`scripts/isolated-asa-restore-rehearsal.py` без `--apply` выводит план. Запуск root на домашней Ubuntu допускается после независимого review точного SHA. Работающие PostgreSQL/MinIO и Compose-проект не используются.

## Входные данные

Владелец расшифровывает возвращённый из облака canonical backup на D2-R2 USB-ключом. Приватный age-ключ на сервер не передаётся. Передаются только plaintext tar и root-private receipt; ciphertext уже находится на сервере. Входные файлы root:root 0600, staging 0700.

Decryption receipt: `installation_instance_id`, `decryption_verified: true`, `archive_sha256` (ciphertext), `plaintext_sha256` (tar). Оба SHA проверяются заново; UUID сверяется с конфигурацией, last-success и текущей установкой.

Нужен независимо проверенный root-owned снимок canonical `tools/deployment`: только `__init__.py`, `backup.py`, `contracts.py`, `system.py`, `source-receipt.json`. Receipt содержит `repository: spikeal8-maker/asa-lab`, точный 40-символьный `commit`, `files` с SHA256 четырёх модулей. Файлы 0600, все предки принадлежат root и недоступны на запись группе/остальным; посторонние файлы/pycache запрещены. Самозаписанные хеши не заменяют review кода.

## Запуск после review

Аргументы: `--apply --expected-host admins-MS-7D22 --bundle <private-tar> --archive <private-age> --decryption-receipt <private-json> --source <reviewed-modules> --receipt /var/lib/asa-restore-rehearsal/accepted.json`.

Tar разрешает только обычные файлы/каталоги под `asa-backup`; ссылки, escape-пути, sparse и дубликаты запрещены. Canonical verify_backup проверяет полный инвентарь. Canonical restore_check получает exec-only адаптер исключительно нового nonce-контейнера PostgreSQL. Загружаются globals/dump, проверяются schema, accounts/projects, grants и RLS.

Используются только уже имеющиеся imageID из manifest. Новые PG/MinIO и checksum helper: network none, без портов/host mounts/privileges, уникальные labels/тома, собственные случайные credentials. Проверяются SHA всех файлов объектов, MinIO health, S3 list и чтение непустого объекта. Cleanup удаляет только ресурсы текущего nonce после проверки идентичности. Receipt создаётся только после успешного cleanup; timer не включается.

Private журнал не публикуется. Временная распаковка удаляется; входные архивы/квитанции остаются для контролируемого удаления владельцем. При аварийной остановке остатки проверяются по labels и точным IDs; broad prune запрещён.

## Предел доказательств

Receipt соответствует валидатору install-nightly-asa-backup.py. Counts и запросы восстановленной БД не доказывают равенство всех строк источнику: baseline fingerprints отсутствуют, `source_row_fingerprint_equality` false. Это не миграция, пользовательская приёмка или квалификация нового PG image. Unit tests не заменяют реальную проверку Docker/MinIO/runtime imageID и cleanup после review.
