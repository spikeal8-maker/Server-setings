#!/usr/bin/env bash
# Exact pinned test-stand MCP OAuth proxy source. No public listener created.
set -Eeuo pipefail
UPSTREAM=c8868adee557e793b73c7e8a3b3d37e080854c20
SOURCE=/srv/Server-setings
BUILDROOT=/opt/server-settings/build
CHECKOUT="$BUILDROOT/mcp-auth-proxy-$UPSTREAM"
TARGET=/opt/server-settings/bin/asa-mcp-auth-proxy
if [[ $# != 1 || "$1" != --apply ]]; then
  echo 'PLAN: clone pinned sigbit/mcp-auth-proxy v2.10.2, apply reviewed ASA RFC9728 patch, test, build.'
  echo 'No services started or restarted, no exposed ports, and no ASA/1Panel config changes.'
  exit 0
fi
[[ "$EUID" == 0 && "$(hostname)" == asa-school-01 ]] || {
 echo 'BLOCKED: only root on asa-school-01' >&2; exit 2;
}
[[ -f "$SOURCE/scripts/prepare-mcp-release.py" ]] || {
 echo 'BLOCKED: Server-setings source unavailable' >&2; exit 2;
}
[[ ! -e "$TARGET" && ! -L "$TARGET" ]] || {
 echo 'BLOCKED: gateway executable already exists; no implicit overwrite' >&2; exit 2;
}
mkdir -p -m 0700 "$BUILDROOT"
[[ "$(stat -c '%U:%a' "$BUILDROOT")" == root:700 ]] || {
 echo 'BLOCKED: build directory owner/mode' >&2; exit 2;
}
if [[ -e "$CHECKOUT" ]]; then
 echo 'BLOCKED: a previous source checkout exists; inspect before retry' >&2; exit 2;
fi
git clone --quiet https://github.com/sigbit/mcp-auth-proxy.git "$CHECKOUT"
git -C "$CHECKOUT" checkout --quiet --detach "$UPSTREAM"
[[ "$(git -C "$CHECKOUT" rev-parse HEAD)" == "$UPSTREAM" ]] || {
 echo 'BLOCKED: wrong pinned upstream HEAD' >&2; exit 2;
}
python3 "$SOURCE/scripts/prepare-mcp-release.py" "$CHECKOUT"
(
 cd "$CHECKOUT"
 export GOTOOLCHAIN=auto CGO_ENABLED=0 GOMAXPROCS=6 GOPROXY=https://proxy.golang.org,direct
 go test -count=1 ./pkg/proxy ./pkg/idp
 go build -trimpath -ldflags '-w -s' -o "$BUILDROOT/mcp-auth-proxy-candidate" .
)
[[ -f "$BUILDROOT/mcp-auth-proxy-candidate" ]] || { echo 'BLOCKED: missing candidate' >&2; exit 2; }
"$BUILDROOT/mcp-auth-proxy-candidate" --help >/dev/null
install -o root -g root -m 0755 "$BUILDROOT/mcp-auth-proxy-candidate" "$TARGET"
echo "PINNED_MCP_OAUTH_PROXY_BINARY_READY=YES"
echo "UPSTREAM_PIN=$UPSTREAM"
sha256sum "$TARGET"
echo "NO_PUBLIC_MCP_ROUTES_ADDED=YES"
