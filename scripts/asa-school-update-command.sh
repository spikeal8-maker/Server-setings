#!/bin/sh
set -eu
if [ "$(/usr/bin/hostname)" != asa-school-01 ]; then
  printf '%s\n' 'Откройте терминал школьного сервера.' >&2
  exit 2
fi
case "${1:-}" in
  check|update|--launcher-verify) exec /usr/local/bin/asa "$@" ;;
  *) printf '%s\n' 'Используйте sudo asa-school check или sudo asa-school update.' >&2; exit 2 ;;
esac
