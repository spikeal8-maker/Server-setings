#!/usr/bin/env python3
"""Exact-source patch for sigbit/mcp-auth-proxy v2.10.2, not a general-purpose updater."""
from __future__ import annotations

import argparse
from pathlib import Path


class UnsafePatch(RuntimeError):
    pass


def replace_once(source: str, before: str, after: str, filename: str) -> str:
    count = source.count(before)
    if count != 1:
        raise UnsafePatch(f"{filename}: expected exactly one patch anchor, got {count}: {before[:72]!r}")
    return source.replace(before, after, 1)


def patch_proxy(code: str) -> str:
    name = "pkg/proxy/proxy.go"
    code = replace_once(
        code,
        'OauthProtectedResourceEndpoint = "/.well-known/oauth-protected-resource"',
        'OauthProtectedResourceEndpoint = "/.well-known/oauth-protected-resource"\n'
        '\tOauthProtectedResourceMCPEndpoint = "/.well-known/oauth-protected-resource/mcp"',
        name,
    )
    code = replace_once(
        code,
        'router.GET(OauthProtectedResourceEndpoint, p.handleProtectedResource)',
        'router.GET(OauthProtectedResourceEndpoint, p.handleProtectedResource)\n'
        '\trouter.GET(OauthProtectedResourceMCPEndpoint, p.handleProtectedResource)',
        name,
    )
    code = replace_once(
        code,
        'Resource:             p.externalURL,',
        'Resource:             strings.TrimRight(p.externalURL, "/") + "/mcp",',
        name,
    )
    code = replace_once(
        code,
        'jwt.WithIssuer(p.externalURL), jwt.WithAudience(p.externalURL)',
        'jwt.WithIssuer(p.externalURL), jwt.WithAudience(strings.TrimRight(p.externalURL, "/") + "/mcp")',
        name,
    )
    return code


def patch_idp(code: str) -> str:
    name = "pkg/idp/idp.go"
    code = replace_once(
        code,
        'func (a *IDPRouter) handleAuth(c *gin.Context) {\n\tctx := c.Request.Context()',
        '''func (a *IDPRouter) handleAuth(c *gin.Context) {
\tresource := strings.TrimRight(a.externalURL, "/") + "/mcp"
\trequested := c.QueryArray("resource")
\tif len(requested) > 1 || (len(requested) == 1 && requested[0] != resource) {
\t\tc.JSON(http.StatusBadRequest, gin.H{"error": "invalid_target"})
\t\treturn
\t}
\tctx := c.Request.Context()''',
        name,
    )
    code = replace_once(
        code,
        'func (a *IDPRouter) handleToken(c *gin.Context) {\n\tctx := c.Request.Context()',
        '''func (a *IDPRouter) handleToken(c *gin.Context) {
\tresource := strings.TrimRight(a.externalURL, "/") + "/mcp"
\trequested := c.PostFormArray("resource")
\tif len(requested) > 1 || (len(requested) == 1 && requested[0] != resource) {
\t\tc.JSON(http.StatusBadRequest, gin.H{"error": "invalid_target"})
\t\treturn
\t}
\tctx := c.Request.Context()''',
        name,
    )
    code = replace_once(
        code,
        'ar.GrantAudience(a.externalURL)',
        'ar.GrantAudience(strings.TrimRight(a.externalURL, "/") + "/mcp")',
        name,
    )
    code = replace_once(
        code,
        'Audience:      []string{a.externalURL},',
        'Audience:      []string{strings.TrimRight(a.externalURL, "/") + "/mcp"},',
        name,
    )
    return code


def patch_proxy_test(code: str) -> str:
    name = "pkg/proxy/proxy_test.go"
    code = replace_once(
        code,
        'claims["aud"] = "https://example.com"',
        'claims["aud"] = "https://example.com/mcp"',
        name,
    )
    code = code.replace('"aud": "https://example.com",', '"aud": "https://example.com/mcp",')
    code = replace_once(
        code,
        'assert.Equal(t, "https://example.com/", resp.Resource)',
        'assert.Equal(t, "https://example.com/mcp", resp.Resource)',
        name,
    )
    code += '''
// Regression: RFC 9728 path-specific discovery and exact JWT resource identity.
func TestProtectedResourceMCPPathAndJWTBoundAudience(t *testing.T) {
    private, public, err := generateRSAKeyPair()
    require.NoError(t, err)
    router, err := NewProxyRouter("https://example.com", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
        w.WriteHeader(http.StatusOK)
    }), public, http.Header{}, false, false, nil, "/userinfo")
    require.NoError(t, err)
    gin.SetMode(gin.TestMode)
    app := gin.New()
    router.SetupRoutes(app)

    for _, target := range []string{OauthProtectedResourceEndpoint, OauthProtectedResourceMCPEndpoint} {
        rec := httptest.NewRecorder()
        app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, target, nil))
        require.Equal(t, http.StatusOK, rec.Code)
        var metadata protectedResourceResponse
        require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &metadata))
        assert.Equal(t, "https://example.com/mcp", metadata.Resource)
        assert.Equal(t, []string{"https://example.com"}, metadata.AuthorizationServers)
    }

    for _, trial := range []struct{ aud string; expected int }{
        {"https://example.com", http.StatusUnauthorized},
        {"https://example.com/mcp", http.StatusOK},
    } {
        token, err := createJWT(private, jwt.MapClaims{
            "iss": "https://example.com", "sub": "test-user",
            "aud": trial.aud, "exp": time.Now().Add(time.Hour).Unix(),
        })
        require.NoError(t, err)
        req := httptest.NewRequest(http.MethodGet, "/mcp", nil)
        req.Header.Set("Authorization", "Bearer " + token)
        rec := httptest.NewRecorder()
        app.ServeHTTP(rec, req)
        assert.Equal(t, trial.expected, rec.Code)
    }
}
'''
    return code


def patch_idp_test(code: str) -> str:
    name = "pkg/idp/idp_test.go"
    code = replace_once(
        code,
        'require.Contains(t, aud, "http://localhost:8080", "aud should contain the external URL")',
        'require.Contains(t, aud, "http://localhost:8080/mcp", "aud must be the protected MCP resource")\n'
        '\trequire.NotContains(t, aud, "http://localhost:8080", "root origin cannot be the access-token audience")',
        name,
    )
    code += '''
// Reject OAuth resource substitution; the issuer stays at root but aud is /mcp.
func TestMCPResourceParameterContract(t *testing.T) {
    server, _, _ := setupTestServer(t)
    for _, resource := range []string{"http://localhost:8080/", "https://evil.example/mcp"} {
        address := server.URL + AuthorizationEndpoint + "?resource=" + url.QueryEscape(resource)
        response, err := http.Get(address)
        require.NoError(t, err)
        require.Equal(t, http.StatusBadRequest, response.StatusCode)
        response.Body.Close()

        response, err = http.PostForm(server.URL + TokenEndpoint, url.Values{
            "resource": {resource},
        })
        require.NoError(t, err)
        require.Equal(t, http.StatusBadRequest, response.StatusCode)
        response.Body.Close()
    }
}
'''
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream_checkout", type=Path)
    args = parser.parse_args()
    root = args.upstream_checkout.resolve()
    paths = {
        "pkg/proxy/proxy.go": patch_proxy,
        "pkg/idp/idp.go": patch_idp,
        "pkg/proxy/proxy_test.go": patch_proxy_test,
        "pkg/idp/idp_test.go": patch_idp_test,
    }
    # Fail before writing any files if upstream layout or source anchors changed.
    replacements = {}
    for name, transform in paths.items():
        target = root / name
        old = target.read_text(encoding="utf-8")
        replacements[target] = transform(old)
    for path, updated in replacements.items():
        path.write_text(updated, encoding="utf-8")
        print(f"PATCHED {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
