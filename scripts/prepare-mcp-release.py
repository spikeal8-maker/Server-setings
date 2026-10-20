#!/usr/bin/env python3
"""Prepare the pinned OAuth gateway candidate; never contacts the live server."""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
from pathlib import Path

UPSTREAM = "c8868adee557e793b73c7e8a3b3d37e080854c20"

PROXY_TEST = r'''package proxy

import (
    "crypto/rand"
    "crypto/rsa"
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"
    "github.com/gin-gonic/gin"
    "github.com/stretchr/testify/require"
)

func TestASAReleaseChallenge(t *testing.T) {
    key, err := rsa.GenerateKey(rand.Reader, 2048)
    require.NoError(t, err)
    for _, origin := range []string{"https://test.asa-lab.ru/", "https://94.29.44.211/"} {
        router, err := NewProxyRouter(origin, http.NotFoundHandler(), &key.PublicKey, http.Header{}, false, false, nil, "/userinfo")
        require.NoError(t, err)
        app := gin.New()
        router.SetupRoutes(app)
        expected := `Bearer resource_metadata="` + strings.TrimRight(origin, "/") + `/.well-known/oauth-protected-resource/mcp"`
        for _, method := range []string{http.MethodGet, http.MethodPost} {
            for _, auth := range []string{"", "Basic invalid", "Bearer invalid", "Bearer "} {
                req := httptest.NewRequest(method, "/mcp", nil)
                req.Host = "untrusted.invalid"
                req.Header.Set("Authorization", auth)
                rec := httptest.NewRecorder()
                app.ServeHTTP(rec, req)
                require.Equal(t, http.StatusUnauthorized, rec.Code)
                require.Equal(t, expected, rec.Header().Get("WWW-Authenticate"))
            }
        }
        for _, path := range []string{OauthProtectedResourceEndpoint, OauthProtectedResourceMCPEndpoint} {
            rec := httptest.NewRecorder()
            app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
            require.Equal(t, http.StatusOK, rec.Code)
            require.Empty(t, rec.Header().Get("WWW-Authenticate"))
        }
    }
}
'''

IDP_TEST = r'''package idp

import (
    "bytes"
    "encoding/json"
    "net/http"
    "testing"
    "github.com/stretchr/testify/require"
)

func TestASAReleaseRegistrationDefaults(t *testing.T) {
    server, _, _ := setupTestServer(t)
    response, err := http.Post(server.URL + RegistrationEndpoint, "application/json", bytes.NewBufferString(`{"client_name":"asa-release-test","redirect_uris":["http://127.0.0.1:48199/callback"],"token_endpoint_auth_method":"none"}`))
    require.NoError(t, err)
    defer response.Body.Close()
    require.Equal(t, http.StatusCreated, response.StatusCode)
    var result map[string]any
    require.NoError(t, json.NewDecoder(response.Body).Decode(&result))
    require.Equal(t, []any{"authorization_code"}, result["grant_types"])
    require.Equal(t, []any{"code"}, result["response_types"])
    require.Equal(t, "none", result["token_endpoint_auth_method"])
    require.NotEmpty(t, result["client_id"])
    require.Empty(t, result["client_secret"])
}
'''


def prepare(root: Path) -> None:
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True)
    if sha != UPSTREAM or dirty:
        raise RuntimeError("Refusing a different or modified upstream checkout")
    spec = importlib.util.spec_from_file_location("asa_oauth_patch", Path(__file__).with_name("patch-upstream-mcp-oauth.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the existing PR #12 patch")
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    transforms = {
        "pkg/proxy/proxy.go": patch.patch_proxy,
        "pkg/idp/idp.go": patch.patch_idp,
        "pkg/proxy/proxy_test.go": patch.patch_proxy_test,
        "pkg/idp/idp_test.go": patch.patch_idp_test,
    }
    changes = {}
    for name, transform in transforms.items():
        target = root / name
        if target.is_symlink() or not target.is_file():
            raise RuntimeError("Unexpected upstream file layout")
        changes[name] = transform(target.read_text(encoding="utf-8"))
    proxy = changes["pkg/proxy/proxy.go"]
    for message in ("Unauthorized", "Invalid token"):
        before = 'c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "' + message + '"})'
        after = 'c.Header("WWW-Authenticate", `Bearer resource_metadata="` + strings.TrimRight(p.externalURL, "/") + OauthProtectedResourceMCPEndpoint + `"`)\n\t\t' + before
        proxy = patch.replace_once(proxy, before, after, "pkg/proxy/proxy.go")
    changes["pkg/proxy/proxy.go"] = proxy
    name = "pkg/idp/idp.go"
    anchor = "\tclient := &fosite.DefaultClient{"
    defaults = '\t// RFC 7591 defaults must be reflected in both storage and response.\n\tif len(req.GrantTypes) == 0 { req.GrantTypes = []string{"authorization_code"} }\n\tif len(req.ResponseTypes) == 0 { req.ResponseTypes = []string{"code"} }\n\tif req.TokenEndpointAuthMethod == "" { req.TokenEndpointAuthMethod = "client_secret_basic" }\n'
    changes[name] = patch.replace_once(changes[name], anchor, defaults + anchor, name)
    for name, content in {"pkg/proxy/asa_release_test.go": PROXY_TEST, "pkg/idp/asa_release_test.go": IDP_TEST}.items():
        if (root / name).exists():
            raise RuntimeError("Refusing to overwrite existing release tests")
        changes[name] = content
    # All source checks finish before the first write. No live configuration is read.
    for name, content in changes.items():
        (root / name).write_text(content, encoding="utf-8")
    print("PREPARED_PINNED_MCP_CANDIDATE", UPSTREAM, "files", len(changes))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream_checkout", type=Path)
    args = parser.parse_args()
    prepare(args.upstream_checkout.resolve())


if __name__ == "__main__":
    main()
