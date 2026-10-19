#!/usr/bin/env bash
# Шифрованный снимок КОНФИГУРАЦИИ сервера; не заменяет backup PostgreSQL/MinIO.
set -Eeuo pipefail
APPLY=0
for arg in "$@"; do case "$arg" in --apply) APPLY=1;; *) echo "Аргумент не поддерживается: $arg" >&2; exit 2;; esac; done
TARGET=/var/backups/server-settings
RECIPIENTS=/etc/server-settings/age-recipients.txt
if [[ "$APPLY" != 1 ]]; then
  echo "План: архивировать настройки 1Panel/OpenResty, системные службы, UFW и локальный Caddy"
  echo "Архив будет зашифрован age и размещён в $TARGET; ключ расшифрования НЕ сохраняется в Git"
  echo 'Сейчас ничего не изменено. Применение: sudo bash scripts/backup-settings.sh --apply'
  exit 0
fi
[[ "$EUID" == 0 ]] || { echo 'Требуется root' >&2; exit 1; }
command -v age >/dev/null || { echo 'Установите age из репозитория ОС' >&2; exit 1; }
[[ -s "$RECIPIENTS" ]] || { echo "Добавьте публичный age recipient в $RECIPIENTS" >&2; exit 1; }
for line in $(grep -vE '^\s*(#|$)' "$RECIPIENTS"); do
  [[ "$line" =~ ^age1[a-z0-9]+$ ]] || { echo 'Некорректный формат age recipient' >&2; exit 1; }
done
umask 077
mkdir -p -m 0700 "$TARGET"
TMP="$(mktemp -d /var/tmp/server-settings-backup.XXXXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/metadata" "$TMP/db"
printf '%s\n' "$(date --iso-8601=seconds)" > "$TMP/metadata/created.txt"
# SQLite backup API: файловая копия WAL под нагрузкой не гарантирует согласованность.
python3 - "$TMP/db" <<'PY'
import sqlite3,sys
from pathlib import Path
out=Path(sys.argv[1]); src=Path('/opt/1panel/db')
for name in ('core.db','agent.db','xpack.db'):
    file=src/name
    if file.exists():
        read=sqlite3.connect(f'file:{file}?mode=ro',uri=True)
        write=sqlite3.connect(out/name)
        try: read.backup(write)
        finally: write.close();read.close()
PY
LIST="$TMP/files.txt"
: > "$LIST"
for item in /etc/ufw /etc/systemd/system/asa-lab-lan-https.service /etc/systemd/system/asa-lab-public-ip.service \
  /opt/asa-lan-https/Caddyfile /opt/1panel/www/conf.d /opt/1panel/www/sites \
  /opt/1panel/apps/openresty/openresty/conf /opt/1panel/apps/openresty/openresty/docker-compose.yml \
  /opt/1panel/apps/openresty/openresty/data.yml /etc/server-settings; do
  [[ -e "$item" ]] && printf '%s\n' "${item#/}" >> "$LIST"
done
OUT="$TARGET/server-settings-$(date -u +%Y%m%dT%H%M%SZ).tar.age"
# Архивируем выбранные файлы с исходными путями плюс консистентные копии SQLite.
# Второй архив содержит содержимое каталогов, включая корневые закрытые конфигурации.
tar -C / -cf "$TMP/snapshot.tar" --files-from "$LIST"
tar -C "$TMP" -rf "$TMP/snapshot.tar" metadata db
age -R "$RECIPIENTS" -o "$OUT" "$TMP/snapshot.tar"
[[ -s "$OUT" ]] || { echo 'Не создан зашифрованный архив' >&2; exit 1; }
sha256sum "$OUT" > "$OUT.sha256"
chmod 0600 "$OUT" "$OUT.sha256"
echo "Снимок конфигурации: $OUT"
echo 'ВНИМАНИЕ: PostgreSQL, MinIO, проекты ASA Lab и сами образы Docker не входят в этот архив.'
