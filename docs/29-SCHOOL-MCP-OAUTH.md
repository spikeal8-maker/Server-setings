# School MCP HTTPS/OAuth on the test Ubuntu — deployment and safety record

**Status (2026-10-29): SERVER DEPLOYED / CLIENT OAUTH LOGIN NOT YET ACCEPTED.**
Tracking: [SCHOOL-MCP-HTTPS-001 (#25)](https://github.com/spikeal8-maker/Server-setings/issues/25).
Do not mark ChatGPT as connected merely because a public endpoint returns HTTP 401.

## Separation of installations

| | Existing test MCP | New school MCP |
| --- | --- | --- |
| Public endpoint | `https://test.asa-lab.ru/mcp` | `https://school-mcp.asa-lab.ru/mcp` |
| OAuth gateway | `asa-1panel-mcp-gateway` | `asa-school-mcp-gateway` |
| Gateway HTTP bind | `127.0.0.1:39443` | `127.0.0.1:39453` |
| OAuth data | Existing volume, unchanged | Separate `asa-school-mcp_school_oauth_data` |
| Backend | Test ASA operations | School official `mcp-1panel` `readonly` |
| School backend | n/a | `serv:127.0.0.1:39443/mcp`, 6 tools |

Network flow: ChatGPT -> **public HTTPS 443** on `admins-MS-7D22` / 1Panel-managed OpenResty -> `127.0.0.1:39453` OAuth gateway -> `127.0.0.1:39454` -> authenticated SSH tunnel (test `admins` -> school `root@100.72.211.28` over Tailscale) -> `serv:127.0.0.1:39443` school MCP -> school 1Panel local API. No additional public port on the school host. D2-R2 requires neither Tailscale nor a local MCP process for web/OAuth access.

**Important:** The public HTTPS connection is independent of Tailscale *on the user's PC*, but the **interserver** leg still depends on Tailscale. This does NOT implement the separately desired independent FRP internet tunnel.

## Live components created

- On the **test Ubuntu only**, systemd `asa-school-mcp-ssh-tunnel.service` (`enabled`) runs as `admins`. Its SSH command binds only `127.0.0.1:39454` and has `StrictHostKeyChecking=yes`, `ExitOnForwardFailure=yes` and network ACLs allowing school tailnet and localhost. See `templates/asa-school-mcp-ssh-tunnel.service` for a nonsecret reference.
- On the test Ubuntu, `/opt/1panel/docker/compose/asa-school-mcp/compose.yaml` and a root-only mode-0600 `.env`; Docker Compose project `asa-school-mcp` uses a pinned gateway image and separate volume. See `templates/asa-school-mcp.compose.yaml` (no secrets).
- School's `server-settings-1panel-mcp.service` remains `readonly`, bound to localhost. Its API key and MCP Bearer remain root-only files. **The test gateway receives only the MCP Bearer**, never the school's 1Panel API key.
- A **new** test-1Panel-managed proxy site `school-mcp.asa-lab.ru`, HTTPS with Let's Encrypt certificate (first expiry 2027-01-27, `autoRenew=true`), points only to `http://127.0.0.1:39453`. OpenResty was syntax-tested then gracefully reloaded. Existing test sites/vhosts were not edited.
- A unique school OAuth login password is generated and stored only in `/opt/1panel/docker/compose/asa-school-mcp/.env` under `SCHOOL_MCP_LOGIN_PASSWORD`. **Do not disclose in GitHub/CI/logs/chat.** Owner may read or rotate it through an authenticated local test-1Panel administrative session. It is *not* the school 1Panel login password.

## Verified nonsecret acceptance evidence

As of 2026-10-29:
- School backend `initialize`, `tools/list` (6 readonly methods), and authenticated `tools/call list_websites`: PASS before gateway cutover.
- Ephemeral test SSH port-forward to the school backend: HTTP 401 without token.
- Persistent SSH forwarding service: active, localhost `39454` -> HTTP 401.
- OAuth gateway: active, loopback `39453`, protected resource metadata 200, `resource=https://school-mcp.asa-lab.ru/mcp`, issuer `https://school-mcp.asa-lab.ru/`.
- Public HTTPS with trusted certificate: MCP no-token 401 and invalid bearer 401, protected-resource metadata 200; **D2-R2** observed HTTP 401 and `ssl_verify_result=0`.
- OAuth client dynamic registration (DCR): HTTP 201, public client accepts authorization-code+PKCE metadata. This does not demonstrate successful owner login or code/token exchange.
- Test ASA `https://test.asa-lab.ru/`: HTTP 200; its pre-existing MCP no-token response remains HTTP 401.
- TLS certificate auto-renew setting confirmed in 1Panel, but a future renewal has not yet occurred.

### Not yet verified / not yet enabled

- Actual user OAuth password login, consent, PKCE code exchange, authenticated `tools/list` from ChatGPT through the new public endpoint. **Do not claim CONNECTED until owner signs in and a real tool call succeeds.**
- MFA/central SSO for school OAuth gateway; currently separate strong-password authentication. Review login throttling and upstream patch lifecycle before broader usage.
- Full administrative MCP methods (shell/installation/restarts). School MCP remains **readonly**.
- Recovery after a physical reboot and failure injection; service restart alone cannot substitute for a real reboot test.
- External independent security review / full live vulnerability assessment.

## Owner's ChatGPT step

In ChatGPT plugin settings add a **separate custom MCP connection** named `1Panel — School` using `https://school-mcp.asa-lab.ru/mcp`. Complete OAuth login using the **separate school OAuth password from the root-only test server file**, grant consent, then invoke `list_websites` or `get_system_info`. Do not paste credentials in chat. Existing `1Panel — Test` remains separately connected.

## Scoped operations and rollback

Preflight checks: service is active; only `127.0.0.1:39454` and `127.0.0.1:39453` on test Ubuntu; school MCP only `127.0.0.1:39443`; private `.env` mode 0600; hostname and DNS match; old MCP and ASA are healthy. Any loss of SSH/tailnet should cause a **closed** school backend (502 from proxy), not unprotected direct access.

Isolated service controls (test Ubuntu; no general Docker/host restart):

```bash
systemctl status asa-school-mcp-ssh-tunnel.service --no-pager
docker compose -p asa-school-mcp --env-file /opt/1panel/docker/compose/asa-school-mcp/.env -f /opt/1panel/docker/compose/asa-school-mcp/compose.yaml ps
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:39453/mcp
```

Rollback for school MCP only: disable the newly created public site through **1Panel** (do not delete other sites), stop the isolated `asa-school-mcp` Compose project **without `-v`**, then disable/stop the isolated SSH-tunnel systemd service. Keep the secret `.env`, separate OAuth state volume and code backups until reconciliation. Verify `https://test.asa-lab.ru/` remains 200 and `https://test.asa-lab.ru/mcp` unauthenticated remains 401. Do not delete users' projects or any PostgreSQL/MinIO volumes.

## Security notes

This gateway does not open school API keys or MCP Bearer publicly. However, the test host already has SSH root reachability to school over tailnet: compromise of trusted test credentials is a high-impact risk. Neither school firewall hardening nor school 1Panel MFA is completed here. They remain separate tracked security tasks; do not silently modify SSH, UFW, DNS, VPN or Windows to solve MCP access.
