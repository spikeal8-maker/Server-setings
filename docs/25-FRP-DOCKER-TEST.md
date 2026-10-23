# FRP test: school Windows → Docker on test Ubuntu

Owner explicitly requested FRP in Docker on the test Ubuntu and a school-server path. The initial test uses the existing Tailscale network: it proves cross-machine FRP forwarding, not independent public-Internet NAT traversal. No router, UFW, DNS, SSH or application ports are changed.

## Boundaries

- Official FRP v0.71.1 server image pinned by digest in scripts/frp-test-server.py. Windows archive checksum is pinned in scripts/frp-school-client.ps1; installed binary is compared with that verified archive.
- One Compose project asa-frp-test, root /srv/asa-frp. Control bind100.75.149.26:7000 only. Proxy14610/14620/14630 published on127.0.0.1 only.80/443/19192/4610 and existing projects untouched.
- FRPS nonroot65532, readonly rootfs/mount, no capabilities, no-new-privileges, bounded memory/PIDs/logs. No Docker socket. No dashboard.
- Both sides require certificate authentication and a random token file. CA private key stays in a root-only directory on Ubuntu, outside container mounts. Server/container materials have private permissions.
- The school private key is generated on Windows, protected with ACL, and never transferred. Only the public CSR is transferred to the signer. The returned bundle contains client certificate, public CA and RSA-OAEP-SHA256-encrypted token. It contains no private key/plaintext token.
- Windows operator is non-admin; the client uses a hidden owner process and an identity-checked Startup shortcut, never an elevated/SYSTEM task. It reconnects after server failure and starts again on owner logon; unattended startup before logon is not claimed. No visible terminal, no Windows firewall/VPN/DNS changes, no Docker installation on Windows.

## Reproduction

1. On school Windows, run scripts/frp-school-client.ps1 -Mode Prepare from the current operator account. Requires existing Git OpenSSL and access to the official FRP release URL. Output is a public CSR only; private key/token must never be printed or committed.
2. On test Ubuntu, inspect plan: python3 scripts/frp-test-server.py. Existing Docker, Compose, OpenSSL and the selected Tailscale address must be available.
3. Publish/review exact code first. As root run python3 scripts/frp-test-server.py --apply --enroll-csr /private/stage/school.csr. The deployment refuses unowned/partial existing roots and mismatched enrollment; it never deletes another container/project. Preserve partial state for diagnosis.
4. Transfer only school-public-bundle.zip back to the private Windows client folder. Run client script -Mode Install -Bundle PATH, then -Mode Start. Token decrypts locally using the school key.
5. On Ubuntu, GET http://127.0.0.1:14610/health/ready must match school-local ASA readiness; GET http://127.0.0.1:14620/ must return the school-generated ASA-FRP-SCHOOL-PROOF marker. This second proxy uses FRPC static_file plugin and serves only a dedicated marker subdirectory, never the parent containing keys.
6. Verify failure with no client certificate and with bad credentials, exact listener addresses, then restart only the new FRPS container and verify automatic reconnect. Confirm original ASA HTTPS, SSH and MCP still work.

The ports inside the FRPS container bind0.0.0.0 so bridge port publishing works; only the specified host addresses above are exposed. This differs from host-network FRPS where proxyBindAddr must itself be127.0.0.1.

## Operations and rollback

Inspect: docker compose -p asa-frp-test -f /srv/asa-frp/compose.json ps. Restart/stop only this project. On Windows use -Mode Status or -Mode Stop for the recorded client process (PID+start-time+executable); no other process is stopped. Remove the specific owner Startup shortcut to disable logon startup. For rollback stop the recorded client process, then docker compose -p asa-frp-test -f /srv/asa-frp/compose.json stop frps. Preserve private materials; do not prune Docker or delete unrelated volumes.

At this commit configuration is prepared, not yet accepted as deployed. Actual acceptance is recorded separately in snapshots after the run. The container appears in1Panel's Docker inventory; importing its external Compose is a separate UI operation, not a second deployment.

Public websites require separate1Panel site/domain/HTTPS routes to loopback14610/14620, through panel API/UI. ASA cannot safely be mounted as a full application under an arbitrary URL prefix. A health/marker-only path may be used for a probe; real ASA should receive its own hostname. No production publication or DNS readiness is implied by an internal tunnel test.

For direct Internet FRP later, authorize a public control endpoint and router NAT/firewall rule, validate mTLS and source policy, then change the transport profile. The present test does not open7000 to WAN. Keep backups disabled as requested until ASA testing is accepted.

## Actual deployment — 24 October 2026 MSK

Published source3913ece was applied to test Ubuntu. Container asa-frp-test-frps-1 is running with the pinned image, non-root65532, read-only root filesystem and exact bindings listed above. School TCP access to100.75.149.26:7000 succeeds. A real FRPC connection using the valid token but no client certificate was rejected (exit1, session shutdown). Existing ASA HTTPS readiness is200, revision6e80e3f3/schema234, all five application containers remain healthy.

School Prepare generated its private key and public CSR locally, but Windows Defender blocked reading the official Windows release archive: threat2147939874, Trojan:Win32/Kepavll!rfn, action successful. The archive checksum could not be checked, so neither verified binary installation nor client enrollment/start/autostart was completed. This detection is not proof that the upstream archive is malicious, and is not established as a false positive. No exclusion, protection disablement or alternate execution route was attempted. Preserve the evidence and have the school security administrator investigate the detection and release integrity before continuing.

The school-local ASA endpoint still returns200. No working end-to-end school tunnel, school proof marker, reconnect acceptance or public Internet publication is claimed. Server FRP can stay idle while the client is blocked; its only control listener is on Tailscale and proxy listeners are loopback. No router, DNS, firewall, existing services/data or backup automation changed. Evidence: snapshots/2026-10-24/frp-test.json.

### Windows Defender recheck —24October2026

Updated school Defender security intelligence successfully from1.461.163.0 to1.461.171.0 with protection enabled. One fresh official0.71.1 download after the update is still blocked at Get-FileHash, before checksum validation/extraction/execution. No exclusions, quarantine restore or protection changes were made. Upstream maintainer confirms intentional FRProxy PUA classification and declined publisher-led review: https://github.com/fatedier/frp/issues/5571. This does not establish that the separate observed Kepavll detection is benign. School FRPC remains NOT installed/running; end-to-end acceptance remains blocked. A reviewed security-product disposition for the exact artifact is required before trusting it.
