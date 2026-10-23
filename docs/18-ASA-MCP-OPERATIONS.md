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

## Reproduce the published deployment

The reviewed deployment source is commit
`1df60558e6ca001179431e67df9db35e74ceee95` in
`spikeal8-maker/Server-setings`; later documentation commits record the native
ChatGPT refresh. Use this exact source when reproducing the recorded result,
not an unpinned branch tip. The live Python modules match `3ad1cd9`; the later
installer commits add the required narrowly scoped host-directory ACLs.

Prerequisites on the existing Ubuntu installation: Python 3.12 with Ubuntu's
matching `python3.12-venv` package, Git, `acl`
(`setfacl`), systemd, the existing Docker engine accessible to `admins`, the
canonical ASA checkout/manager at revision
`e142835ed087995235d53f714f8185dfdfb00e0e`, the existing official 1Panel binary
and the already configured OAuth gateway. This is an extension of that host,
not a clean-host bootstrap. Its fixed host paths, UID and Compose identity must
be checked against `templates/asa-operations.config.example.json` first.

From the pinned checkout, run these checks without changing services or the
application (`compileall` creates local bytecode):

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
Before activating the gateway, launch and close Chromium as `admins` with
`PLAYWRIGHT_BROWSERS_PATH=/opt/server-settings/asa-browsers` to check the actual
service user's permissions and browser libraries.
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

### Approved archive restore rehearsal, 2026-10-21

The existing backend now implements **two** additional operations (19 total):
`asa_restore_plan` and `asa_restore_rehearse`. `asa_restore_apply` is deliberately
not exposed or claimed implemented. ChatGPT's previously refreshed inventory
still has17 tools until its separate authorized refresh.

The only accepted catalog snapshot is `20261017T231505Z-65b68074`, archive hash
`910795bae418b265ac37a8d9ac84e22a92e9c2c870ef11138de4cbeffb7b41e4` and manifest
hash `8be098e3c37cd43fb704e7f92f300574bb685445d92697223ba0b158ac2e8a01`.
On the owner workstation, verify the encrypted file hash, decrypt with the
existing local age identity, and stream tar over authenticated SSH into
`python3 scripts/asa_ops/stage_snapshot.py` on Ubuntu as `admins`. Its fixed
private parent `/srv/asa-restore-snapshots` must already exist with owner admins
and mode0700. Do not transmit the age identity. The receiver rejects links,
traversal, foreign snapshot roots, duplicates and hash/inventory mismatches;
all decrypted data remains private outside Git and synchronization folders.
Partial imports are retained privately for explicit inspection, never promoted
or automatically retried over an existing directory.

Example tool calls after the reviewed source is installed:

```json
{"name":"asa_restore_plan","arguments":{"installation_id":"asa-test","snapshot_id":"20261017T231505Z-65b68074"}}
```

```json
{"name":"asa_restore_rehearse","arguments":{"installation_id":"asa-test","plan_id":"<returned plan_id>","idempotency_key":"<retained unique key of 16-128 characters>"}}
```

Poll the returned ID with existing `asa_operation_get` or discover it with
`asa_operation_list`. A completed rehearsal is not successful live restoration.
SQLite permits an isolated rehearsal beside an idle failed-update receipt, but
blocks any live worker or finalization from running concurrently. The manager
operation lock spans the rehearsal. An uncertain start response must reuse
its key; process loss is never handled by automatically replaying migrations.

Rehearsal pins the **actual running** release, not the stale installed receipt,
verifies all2062 snapshot files, uses canonical `restore_check` for a new unique
`*_test` database, preserves ACLs/RLS and existing roles (no `globals.sql`
import), and runs the pinned migration image plan/apply/plan only on that DB.
Migration URL overrides are restored before rechecking live identity. The
original signing/encryption/object keys are not merged into live configuration.

A separate labelled temporary MinIO volume receives a physical clone, verified
against all2054 object-file hashes before startup. Its server runs with the
attested live numeric owner, snapshot credentials from a private mode0600 file,
`--network none` and no published ports; real S3 listing succeeded with1846
objects. Cleanup checks exact ownership labels before removing only its own
container/volume. The successful test DB and private receipt remain available
for audit; no automatic database deletion is performed.

Operation `bb1d96f558894cf8aa2f611a5a01c5c5` passed migration212→225 and object
rehearsal. Row-key/content-hash comparison found all1414 archived projects and
drafts identical in current Ubuntu, with **3 additional current projects and
drafts**, newer audit/session/analytics rows and changed operational settings.
Archive accounts90, classrooms31 and seats418 also match current rows exactly.
An additional check excluding created_at/updated_at shows AI and account
verification settings match semantically; auth-provider settings still differ.
This is not an update source for the current DB: exact replacement would roll
back newer data. Comparisons use live reads and are advisory; a future apply
must repeat them after quiescing writers and obtain an explicit replacement
policy for current-only/changed rows.

The unfinished restore script in ASA PR583 at
`e47da431dec765a041e1e1e34443779d639272df` is reference material only. Its
partial-object-move rollback, stale-release selection, missing durable resume
and broad Compose-up issues prevent directly exposing it as an accepted apply
method. Implement and review crash-safe database/object/config cutover and
rollback, full user-flow acceptance and stopped-writer comparison before
enabling `asa_restore_apply`. No live cutover was performed in this work.
See `snapshots/2026-10-21/asa-restore-rehearsal.json` for public aggregate proof;
private row identifiers, secrets, dumps and object contents are never published.

### Controlled entry repair, 2026-10-21

`scripts/repair-asa-entry.py` is a separate operator maintenance command, not
an arbitrary command exposed by MCP. On the recorded existing Ubuntu host:

```bash
sudo python3 scripts/repair-asa-entry.py
sudo python3 scripts/repair-asa-entry.py --apply
```

Default mode validates the proposed configuration without changing it. Apply
holds the canonical manager lock, privately saves `/etc/hosts`, the existing
`.env` and manager state under `/var/backups/asa-entry-<UTC timestamp>`, adds
only `test.asa-lab.ru -> 127.0.0.1` on Ubuntu (to avoid public-IP hairpin timeout),
and aligns the three entry/editor origins with `https://test.asa-lab.ru`.
It preserves explicit existing allowed web origins and adds this domain.
It recreates only API/Scratch/Web with the currently running release's digest
pins and schema, no dependencies/build/pull/migration. PostgreSQL/MinIO container
IDs and mounts, image pins and trusted domain entry are verified afterward.
The Windows PC's resolver/network configuration is not changed.

The first live attempt exposed an old `ASA_EXPECTED_SCHEMA_VERSION=213` hint
overriding the selected schema225 during environment merging and briefly made
readiness return503. Pin precedence was corrected, API configuration reapplied
without migration, and the corrected repair command rerun successfully.
Selected release pins now override private build/schema hints after merging.
The original backup is retained; successful repeat verification is recorded at
`/var/backups/asa-entry-20261020T224948Z`.

Rollback is an explicit configuration transition: preserve a fresh copy first,
restore the chosen private hosts/env files, then use the same running release
pins to recreate only the affected three services and verify entry/data identity.
Do not restore stale receipts or run a database restoration to roll back origins.

After repair, a fresh MCP plan passes trusted TLS and editor-origin checks;
only `FAILED_RECEIPT` remains. Recovery plans now recheck the current state
while retaining the existing operation and its original release pins.
Actual MCP finalization returns `ACCEPTANCE_PENDING`, stage
`teacher_registration_or_login`, code `API_STATUS_403`: the current acceptance
runner's API registration omits the production BotCheck proof. Its fixture also
needs legitimate verified-account/course prerequisites before later user flows
can pass. Do not forge a proof, weaken auth, or mark that receipt successful.
Failed finalization retains `recovery_required` even when old manager marker
files have previously been acknowledged. New runs must use a new finalization
key; reusing the old key returns its original result.

Unit/integration tests cover transactional concurrency, restart discovery,
immutable-plan tampering, durable idempotency, rejected arbitrary parameters,
blocked plans, bounded manager handoff and actual stdio protocol forwarding.
Live deployment and controlled update acceptance are recorded separately in
`snapshots/`; passing these tests alone does not establish successful deployment,
an application upgrade or fresh cloud data.
