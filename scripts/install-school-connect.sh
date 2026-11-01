#!/usr/bin/env bash
# Install only a local owner-facing entrypoint, never changes application data.
set -Eeuo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
TARGET="/usr/local/sbin/asa-school-connect"
if [[ "$#" != 1 || "$1" != "--apply" ]]; then
  echo 'PLAN: add only /usr/local/sbin/asa-school-connect.'
  echo 'No Yandex/Tailscale credentials, network changes, OAuth or container changes.'
  echo 'To install: sudo bash scripts/install-school-connect.sh --apply'
  exit 0
fi
[[ "$EUID" == 0 && "$(hostname)" == "asa-school-01" ]] || {
  echo 'Requires root and hostname asa-school-01' >&2; exit 2;
}
[[ -f "$SRC/school-yandex-connect.py" && -f "$SRC/school-tailscale-connect.py" &&
   -f "$SRC/repair-school-yandex-oauth.py" &&
   -f "$SRC/cloud_storage.py" ]] || {
  echo 'Server-setings required modules missing' >&2; exit 2;
}
bash -n "$SRC/school-connect-entrypoint.sh"
python3 -m py_compile "$SRC/school-yandex-connect.py" "$SRC/school-tailscale-connect.py"
if [[ -e "$TARGET" ]]; then
  if cmp -s "$SRC/school-connect-entrypoint.sh" "$TARGET"; then
    echo 'Owner entrypoint is already installed and matches repository.'
  else
    # Only upgrade the exact first school CLI deployed in October. Unknown
    # local scripts are never overwritten, even when launched as root.
    INITIAL_COMMIT=9105f9b4ac636cac1c4880a0078dafe197eae0a1
    [[ ! -L "$TARGET" && -f "$TARGET" &&
       "$(stat -c '%U:%G:%a' "$TARGET")" == "root:root:755" ]] || {
      echo 'BLOCKED: existing CLI identity differs; refusing upgrade' >&2; exit 2;
    }
    cmp -s "$TARGET" <(git -C "$SRC/.." show "$INITIAL_COMMIT":scripts/school-connect-entrypoint.sh) || {
      echo 'BLOCKED: existing CLI content not trusted; refusing upgrade' >&2; exit 2;
    }
    install -o root -g root -m 0755 "$SRC/school-connect-entrypoint.sh" "$TARGET"
    echo 'Upgraded verified owner CLI with reauthorization support.'
  fi
else
  install -o root -g root -m 0755 "$SRC/school-connect-entrypoint.sh" "$TARGET"
  echo 'Installed self-contained owner enrollment entrypoint.'
fi
"$TARGET" help
