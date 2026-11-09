#!/bin/sh
# H03-only Chrome launch shim for isolated ASA Lab QA load. Never run as root.
# Chrome's user namespace sandbox is unavailable on this test Linux host;
# the OS user must be nobody and the browser must be restricted to staging.
set -eu

[ "$(hostname)" = "admins-MS-7D22" ] || { echo "QA_CHROME_WRONG_HOST" >&2; exit 2; }
[ "$(id -u)" = "65534" ] || { echo "QA_CHROME_MUST_BE_NOBODY" >&2; exit 2; }
[ "${ASA_SCHOOL_QA_CHROME_UNSANDBOXED:-}" = "I_ACCEPT_H03_QA_ONLY" ] || {
  echo "QA_CHROME_EXPLICIT_OPT_IN_REQUIRED" >&2
  exit 2
}
binary="${ASA_QA_CHROME_BINARY:-}"
case "$binary" in
  /tmp/asa-school-remote-chrome-*/browser-cache/chrome/linux-*/chrome-linux64/chrome) ;;
  *) echo "QA_CHROME_BINARY_OUTSIDE_DEDICATED_TEMP" >&2; exit 2 ;;
esac
[ -f "$binary" ] && [ -x "$binary" ] || { echo "QA_CHROME_BINARY_NOT_EXECUTABLE" >&2; exit 2; }

# No root, no privileged account, no global Chrome flags and no shell eval.
# Puppeteer must provide all its original CLI arguments unchanged.
exec "$binary" --no-sandbox --disable-setuid-sandbox "$@"
