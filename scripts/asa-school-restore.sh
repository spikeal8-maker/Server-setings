#!/usr/bin/env bash
# ASA Lab school VM100 — recover encrypted ASA backups from Yandex Disk.
set -Eeuo pipefail
if [[ "$(hostname)" != "asa-school-01" ]]; then
  echo 'Ошибка: команда предназначена для VM100 asa-school-01.' >&2
  exit 2
fi
if [[ "$EUID" != 0 ]]; then
  echo 'Запустите: sudo asa-school-restore list' >&2
  exit 2
fi
exec /usr/bin/python3 /srv/Server-setings/scripts/school-yandex-restore.py "$@"
