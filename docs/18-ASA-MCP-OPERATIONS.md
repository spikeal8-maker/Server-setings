# Server-owned ASA update operations

The existing public OAuth endpoint remains `https://test.asa-lab.ru/mcp`.
The existing official 1Panel binary still supplies its 11 full-mode tools.
The additional authenticated backend listens only on `127.0.0.1:39446` and
adds six methods: `asa_update_plan`, `asa_update_start`, `asa_operation_get`,
`asa_operation_list`, `asa_deployment_verify`, `asa_operation_finalize`.
No arbitrary shell, path, SQL, download URL or restoration argument is exposed.

## Process and state ownership

`asa-operations.service` runs as the existing checkout owner `admins`, with the
existing Docker group membership. Code and configuration are root-owned. Its
private SQLite store is `/var/lib/server-settings/asa-operations/`; it is separate
from the updated checkout. Plans are immutable JSON with integrity hashes;
operations, idempotency keys and incremental JSONL logs survive the client.
Detached workers continue after an HTTP disconnect or daemon-main restart.
Boot ID plus process-start identity detects a lost worker without trusting a
reused PID. Lost mutations become `recovery_required`; none are automatically
replayed. The manager's own operation lock is retained.

One active operation owns each installation. A repeated start key returns the
original job even after the plan expires. A conflicting key/payload is rejected.
An explicit retry of a failed finalization requires a new key; reusing its old
key only reads the previous attempt. `asa_operation_list` recovers IDs after chat
loss. `asa_operation_get` accepts a byte cursor and returns only bounded,
structured events, never raw Docker output, credentials or private files.

## Manager integration

Each attempt imports `/srv/asa-lab/tools/asa_manager.py` in a fresh subprocess.
Only its release discovery function is bound to the approved complete release
manifest; update/backup/migration/switch code remains in ASA Lab. A plan records
full source SHA, immutable image references, canonical manifest SHA256 and the
registry manifest reference captured during discovery. A new stable publication
does not change an already stored plan. The expected `RETRY_NEW_MANAGER` handoff
is verified against the selected checkout, trusted pre-source-transition
fingerprint and unchanged runtime settings; at most one fresh-process
continuation is permitted.

Preflight checks owner, repository cleanliness, project/data-volume identity,
prior manager markers/failed receipts, actual database and object-storage size,
backup write/fsync, space budget, trusted TLS and editor entry origins. Exact CI
and immutable images are checked. A disposable target-source Git worktree
validates every Compose overlay and runs the migration plan before the manager
can stop writers. Registry pulls and this read-only migration-plan container may
occur during planning; planning never stops/recreates existing app containers.

Data mode is explicitly `preserve`. Reports identify existing PostgreSQL/MinIO
as the data source; snapshot ID/time remain unknown, never inferred from schema
or a newly created local backup. Restoring a cloud archive is not implemented by
these update methods and cannot happen implicitly.

## Acceptance and interrupted finalization

`asa_deployment_verify` creates a durable job. Technical health is separate from
browser acceptance. The configured hook uses dedicated, owner-authorized test
identities and projects, strict TLS, real APIs and a real Chromium browser. It
must attest teacher/student admission, course/project opening and actual saved
drafts reopening in cookies-only browser contexts. Missing fixtures, a bot
challenge, bad certificate, origin mismatch, missing course or failed editor save
remain failures; the runner never manufactures passes or weakens authentication.
Fixture credentials/screenshots stay private on the server.

The pre-existing failed manager receipt can be imported as a recovery operation
by `asa_update_plan`. `asa_operation_finalize` verifies pinned runtime/images,
volumes, trusted entry, backup integrity/provenance and mandatory browser checks,
then reconciles manager-compatible installed/settings/receipt files. It journals
intent before changes and handles interruption during metadata reconciliation.
It never runs migrations, Compose up or a database restoration. `acknowledge`
alone is never treated as successful finalization.

## Deployment and rollback

Prepare private `/etc/server-settings/asa-operations.json` from the example and
root-only `asa-operations.env` containing `ASA_OPS_TOKEN`, `PANEL_HOST` and
`PANEL_ACCESS_TOKEN`. Never copy these values to Git, PRs or stdout. Install the
reviewed backend with `scripts/install-asa-operations.sh --apply`.

Before changing the gateway, save its existing Compose/.env/inspect privately.
Using the local 1Panel Compose API, retain image, public issuer, state volume,
loopback 39443 and all existing credentials. Change only its backend command to
`-- http://127.0.0.1:39446` and add `PROXY_BEARER_TOKEN` with the private backend
token. Use a base URL without `/mcp` because the transparent proxy retains the
incoming URI. No public ports or vhost changes are needed. Check 17 tools,
unauthenticated rejection, OAuth and old official-tool calls before refreshing
the ChatGPT tool inventory. Existing allow-all preference is owner-authorized.

Rollback restores the saved gateway Compose/.env through 1Panel and verifies
the original 11 tools and ASA readiness. Do not delete the durable store, OAuth
state, application volumes or backups. Do not stop the backend while its jobs
run. The private backend may remain installed during gateway rollback.

## Verification boundary

Unit/integration tests cover transactional concurrency, restart discovery,
immutable-plan tampering, durable idempotency, rejected arbitrary parameters,
blocked plans, bounded manager handoff and actual stdio protocol forwarding.
Live deployment and controlled update acceptance are recorded separately in
`snapshots/`; passing these tests alone does not establish successful deployment,
an application upgrade or fresh cloud data.
