#!/usr/bin/env bash
# Resume exactly the already prepared/pinned ASA mcp-auth-proxy candidate.
# Does not affect any service, site, 1Panel database or public route.
set -Eeuo pipefail
UPSTREAM=c8868adee557e793b73c7e8a3b3d37e080854c20
ROOT=/opt/server-settings/build
CHECKOUT="$ROOT/mcp-auth-proxy-$UPSTREAM"
DEST=/opt/server-settings/bin/asa-mcp-auth-proxy
[[ "$EUID" == 0 && "$(hostname)" == asa-school-01 ]] || exit 2
if [[ "$#" != 1 || "$1" != "--apply" ]]; then
 echo 'PLAN: resume exact patched MCP gateway build with an explicit private Go module cache; never start the gateway.'
 exit 0
fi
[[ ! -e "$DEST" && ! -L "$DEST" ]] || { echo 'EXISTING_GATEWAY_BINARY_STOP'; exit 2; }
[[ "$(stat -c '%U:%a' "$ROOT")" == root:700 ]] || { echo 'BUILD_ROOT_TRUST_FAILED'; exit 2; }
[[ ! -L "$CHECKOUT" && -d "$CHECKOUT" && "$(stat -c '%U' "$CHECKOUT")" == root ]] || { echo 'CHECKOUT_TRUST_FAILED'; exit 2; }
[[ "$(git -C "$CHECKOUT" rev-parse HEAD)" == "$UPSTREAM" ]] || { echo 'UPSTREAM_PIN_FAILED'; exit 2; }
[[ "$(git -C "$CHECKOUT" status --porcelain=v1)" == "$(printf ' M pkg/idp/idp.go\n M pkg/idp/idp_test.go\n M pkg/proxy/proxy.go\n M pkg/proxy/proxy_test.go\n?? pkg/idp/asa_release_test.go\n?? pkg/proxy/asa_release_test.go')" ]] || {
 echo 'SOURCE_PATCH_FILE_LIST_CHANGED'; exit 2;
}
cd "$CHECKOUT"
cat <<'CHECKSUMS' | sha256sum --check --status || { echo 'PATCHED_SOURCES_HASH_MISMATCH'; exit 2; }
84d0365f59ceae4b21af2e9f0d68192bd4c6b75f6019a7a4d91a3db89e0fef65  pkg/proxy/proxy.go
d40b86428d2baff351b0da0f235b5c8663b80363c358c8e99758e0b6c14b9bf4  pkg/idp/idp.go
d36b224fc6ba82e91246726ebb704f2c51bb50e037f99976c34242e65e4d613e  pkg/proxy/proxy_test.go
0bbb3131759e8399a2c6f46c1258b87fe13a7d65be6d9fd8f4c447f23bf5add7  pkg/idp/idp_test.go
dabc325cf74fbef825917a373c9233091d7432718c56c77dab9de1d462528d5a  pkg/proxy/asa_release_test.go
78656d31fe54d8f237cb1c9ad7416f4a4088a18ab6c6c258aac1a3bbe7bc2afb  pkg/idp/asa_release_test.go
CHECKSUMS
mkdir -p -m 0700 "$ROOT/gopath" "$ROOT/gocache"
export GOPATH="$ROOT/gopath"
export GOMODCACHE="$ROOT/gopath/pkg/mod"
export GOCACHE="$ROOT/gocache"
export GOTOOLCHAIN=auto CGO_ENABLED=0 GOMAXPROCS=6 GOPROXY=https://proxy.golang.org,direct
test "$(stat -c '%U' "$ROOT/gopath")" = root
echo 'PINNED_SOURCES_VERIFIED=YES'
go test -count=1 ./pkg/proxy ./pkg/idp
go build -trimpath -ldflags '-w -s' -o "$ROOT/mcp-auth-proxy-candidate" .
"$ROOT/mcp-auth-proxy-candidate" --help >/dev/null
install -o root -g root -m 0755 "$ROOT/mcp-auth-proxy-candidate" "$DEST"
echo "PINNED_MCP_OAUTH_BINARY_BUILT=YES"
sha256sum "$DEST"
echo 'GATEWAY_SERVICE_NOT_STARTED=YES'
echo 'PUBLIC_HTTP_ROUTE_NOT_CREATED=YES'
