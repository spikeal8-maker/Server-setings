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

## Reproduce the published deployment

The reviewed deployment source is commit
`1df60558e6ca001179431e67df9db35e74ceee95` in
`spikeal8-maker/Server-setings`; later documentation commits record the native
ChatGPT refresh. Use this exact source when reproducing the recorded result,
not an unpinned branch tip. The live Python modules match `3ad1cd9`; the later
installer commits add the required narrowly scoped host-directory ACLs.

Prerequisites on the existing Ubuntu installation: Python 3.12, Git, `acl`
(`setfacl`), systemd, the existing Docker engine accessible to `admins`, the
canonical ASA checkout/manager at revision
`e142835ed087995235d53f714f8185dfdfb00e0e`, the existing official 1Panel binary
and the already configured OAuth gateway. This is an extension of that host,
not a clean-host bootstrap. Its fixed host paths, UID and Compose identity must
be checked against `templates/asa-operations.config.example.json` first.

From the pinned checkout, run the non-mutating checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts
bash -n scripts/install-asa-operations.sh
bash scripts/install-asa-operations.sh
```

Prepare the browser runtime separately, using the pinned package version:

```bash
sudo python3 -m venv /opt/server-settings/asa-browser-venv
sudo /opt/server-settings/asa-browser-venv/bin/pip install playwright==1.56.0
sudo env PLAYWRIGHT_BROWSERS_PATH=/opt/server-settings/asa-browsers \
  /opt/server-settings/asa-browser-venv/bin/python -m playwright install chromium
```

Install any missing Chromium OS libraries using the official Playwright
prerequisite procedure; do not disable the sandbox or TLS to mask a failure.
Create the private configuration and environment files described above, with
root ownership, JSON mode `0640` and group `admins`, environment mode `0600`.
Generate a fresh backend bearer token of at least 32 characters on the host;
reuse the existing panel host/key privately. Then run the installer with
`--apply`. It grants `admins` traversal on private parent directories and
read/execute only on the exact official MCP binary; it does not expose their
contents. Test the private backend before the separately backed-up gateway
change through 1Panel.

After gateway activation, verify OAuth resource/audience/PKCE, unauthenticated
rejection, exactly 17 tool names and actual existing read-tool responses. In
ChatGPT, refresh the existing plugin's tools with owner authorization; the
recorded UI result is **Write8 / Read9**, with the pre-existing allow-all
preference retained. Never publish real tokens, fixture credentials or backup
contents as reproduction inputs.

For an authenticated MCP client, the first ASA call is:

```json
{"name":"asa_update_plan","arguments":{"installation_id":"asa-test","data_mode":"preserve"}}
```

Only a fresh plan with `ready: true` may be submitted to `asa_update_start`,
using its returned `plan_id` and a retained idempotency key (16–128 characters).
Persist the returned `operation_id`; poll `asa_operation_get` and retain its
log cursor. Reconnect with `asa_operation_list` after client loss. Do not create
a different start key to recover an uncertain response.

The recorded live run deliberately does **not** satisfy update acceptance:
`FAILED_RECEIPT` and `ENTRY_ORIGIN` block the plan, and the domain times out
from Ubuntu. These are reproducible observations, not expected success criteria.
Resolve them explicitly and complete real browser acceptance before finalizing
the imported recovery operation. See the deployment snapshot for the exact
operation ID, gateway rollback directory and test evidence.

## Limits of the recorded verification

Unit/integration tests cover transactional concurrency, restart discovery,
immutable-plan tampering, durable idempotency, rejected arbitrary parameters,
blocked plans, bounded manager handoff and actual stdio protocol forwarding.
Live deployment and controlled update acceptance are recorded separately in
`snapshots/`; passing these tests alone does not establish successful deployment,
an application upgrade or fresh cloud data.
