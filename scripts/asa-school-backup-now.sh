#!/usr/bin/env bash
# ASA Lab School VM100 — emergency consistent backup at ANY hour.
# Independent of the nightly systemd timer. Uses the same safe exporter.
set -Eeuo pipefail
SOURCE=/srv/Server-setings/scripts
BACKUP="$SOURCE/school-nightly-database-backup.py"
CLOUD="$SOURCE/cloud_storage.py"

if [[ "$(hostname)" != "asa-school-01" ]]; then
  echo 'ОШИБКА: требуется школьная VM100 (asa-school-01).' >&2
  exit 2
fi

ACTION=--run
if [[ "$#" -ge 1 ]]; then ACTION="$1"; fi
case "$ACTION" in
  --run|--apply)
    if [[ "$#" -gt 1 ]]; then
      echo 'Недопустимые аргументы. Выполните asa-school-backup-now --help.' >&2
      exit 2
    fi
    if [[ "$EUID" != 0 ]]; then
      echo 'Запуск: sudo asa-school-backup-now' >&2
      exit 2
    fi
    echo 'ASA Lab VM100: ручное резервирование в любое время.'
    echo 'ВНИМАНИЕ: на время экспорта возможна краткая остановка Web/API/Scratch/MinIO.'
    echo 'PostgreSQL + MinIO → age → закрытая папка Яндекс Диска → SHA256.'
    exec /usr/bin/python3 "$BACKUP" --apply --manual
    ;;
  --check)
    if [[ "$EUID" != 0 || "$#" -ne 1 ]]; then
      echo 'Проверка: sudo asa-school-backup-now --check' >&2
      exit 2
    fi
    exec /usr/bin/python3 "$BACKUP" --preflight
    ;;
  --status)
    if [[ "$#" -ne 1 ]]; then
      echo 'Статус: sudo asa-school-backup-now --status' >&2
      exit 2
    fi
    exec /usr/bin/python3 "$CLOUD" status
    ;;
  --help|-h)
    printf '%s\n' \
      'ASA Lab School VM100 — PostgreSQL и MinIO.' \
      '  sudo asa-school-backup-now           — запустить резервирование НЕМЕДЛЕННО, в любое время' \
      '  sudo asa-school-backup-now --check   — проверить готовность без остановки базы' \
      '  sudo asa-school-backup-now --status  — состояние таймера и последней копии' \
      '  sudo systemctl list-timers asa-school-nightly-database-backup.timer' \
      'Ночной таймер остаётся ежедневным в 03:20 Europe/Moscow.' \
      'Во время экспорта возможно краткое прерывание работы ASA Lab.' \
      'Успех — только после загрузки и скачивания архива с SHA256.'
    ;;
  *)
    echo 'Неизвестная команда. Используйте asa-school-backup-now --help' >&2
    exit 2
    ;;
esac
