#!/bin/sh
set -eu
if [ "$(/usr/bin/hostname)" != admins-MS-7D22 ]; then
  printf '%s\n' 'Откройте терминал тестового сервера H03.' >&2
  exit 2
fi
umask 077
launcher=/usr/local/libexec/asa-h03/asa
if [ "$(/usr/bin/id -u)" -eq 0 ]; then
  exec /usr/bin/sudo -n -H -u admins -- "$launcher" "$@"
fi
if [ "$(/usr/bin/id -un)" = admins ]; then
  exec "$launcher" "$@"
fi
printf '%s\n' 'Используйте sudo asa.' >&2
exit 2
