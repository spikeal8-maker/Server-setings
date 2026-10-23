# FRP test: school Windows → Docker on test Ubuntu

Owner explicitly requested FRP in Docker on the test Ubuntu and a school-server path. The initial test uses the existing Tailscale network: it proves cross-machine FRP forwarding, not independent public-Internet NAT traversal. No router, UFW, DNS, SSH or application ports are changed.

## Boundaries

- Official FRP v0.71.1 server image pinned by digest in scripts/frp-test-server.py. Windows archive checksum is pinned in scripts/frp-school-client.ps1; installed binary is compared with that verified archive.
- One Compose project asa-frp-test, root /srv/asa-frp. Control bind100.75.149.26:7000 only. Proxy14610/14620/14630 published on127.0.0.1 only.80/443/19192/4610 and existing projects untouched.
- FRPS nonroot65532, readonly rootfs/mount, no capabilities, no-new-privileges, bounded memory/PIDs/logs. No Docker socket. No dashboard.
- Both sides require certificate authentication and a random token file. CA private key stays in a root-only directory on Ubuntu, outside container mounts. Server/container materials have private permissions.
- The school private key is generated on Windows, protected with ACL, and never transferred. Only the public CSR is transferred to the signer. The returned bundle contains client certificate, public CA and RSA-OAEP-SHA256-encrypted token. It contains no private key/plaintext token.
- Windows client uses an unprivileged S4U scheduled task, not a SYSTEM task pointing to a user-writable executable. No visible terminal, no Windows firewall/VPN/DNS changes, no Docker installation on Windows.

## Reproduction

1. On school Windows, run scripts/frp-school-client.ps1 -Mode Prepare from the current operator account. Requires existing Git OpenSSL and access to the official FRP release URL. Output is a public CSR only; private key/token must never be printed or committed.
2. On test Ubuntu, inspect plan: python3 scripts/frp-test-server.py. Existing Docker, Compose, OpenSSL and the selected Tailscale address must be available.
3. Publish/review exact code first. As root run python3 scripts/frp-test-server.py --apply --enroll-csr /private/stage/school.csr. The deployment refuses unowned/partial existing roots and mismatched enrollment; it never deletes another container/project. Preserve partial state for diagnosis.
4. Transfer only school-public-bundle.zip back to the private Windows client folder. Run client script -Mode Install -Bundle PATH, then -Mode Start. Token decrypts locally using the school key.
5. On Ubuntu, GET http://127.0.0.1:14610/health/ready must match school-local ASA readiness; GET http://127.0.0.1:14620/ must return the school-generated ASA-FRP-SCHOOL-PROOF marker. This second proxy uses FRPC static_file plugin and serves only a dedicated marker subdirectory, never the parent containing keys.
6. Verify failure with no client certificate and with bad credentials, exact listener addresses, then restart only the new FRPS container and verify automatic reconnect. Confirm original ASA HTTPS, SSH and MCP still work.

The ports inside the FRPS container bind0.0.0.0 so bridge port publishing works; only the specified host addresses above are exposed. This differs from host-network FRPS where proxyBindAddr must itself be127.0.0.1.

## Operations and rollback

Inspect: docker compose -p asa-frp-test -f /srv/asa-frp/compose.json ps. Restart/stop only this project. On Windows use -Mode Status or -Mode Stop for ASA-FRP-School-Test; no other FRPC process/task is stopped. For rollback stop the client task, then docker compose -p asa-frp-test -f /srv/asa-frp/compose.json stop frps. Preserve private materials; do not prune Docker or delete unrelated volumes.

At this commit configuration is prepared, not yet accepted as deployed. Actual acceptance is recorded separately in snapshots after the run. The container appears in1Panel's Docker inventory; importing its external Compose is a separate UI operation, not a second deployment.

Public websites require separate1Panel site/domain/HTTPS routes to loopback14610/14620, through panel API/UI. ASA cannot safely be mounted as a full application under an arbitrary URL prefix. A health/marker-only path may be used for a probe; real ASA should receive its own hostname. No production publication or DNS readiness is implied by an internal tunnel test.

For direct Internet FRP later, authorize a public control endpoint and router NAT/firewall rule, validate mTLS and source policy, then change the transport profile. The present test does not open7000 to WAN. Keep backups disabled as requested until ASA testing is accepted.
