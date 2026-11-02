# HOST collector forensic preservation, without recovery

`scripts/diagnostic_forensic_snapshot.py` creates a protected HOST-only forensic
copy before separately reviewed recovery. PLAN is default. It never stops,
starts, repairs or replaces collector/API/timer/history/business data.

H03 has31 retained segments without original metadata/receipts. Operator ZIPs
are not complete diagnostic snapshots. `normalized=true` and present-day hashes
do not authenticate original sequence ownership. Old ERROR/debt remain intact.

## Boundary

Included from the explicit installation:

- `.asa/diagnostics/private/**`: index, WAL/SHM, local verification key, resources,
  receipts and existing collector lock;
- `.asa/diagnostics/store/**`: all active/retired files and catalog/health/audit,
  including corrupt or unpublished bytes;
- `.asa/log-collector.json`, canonical `collector-runner.json`, `installation.json`;
  optional `collector-operation.json`.

Independent API `.asa/diagnostics/application/**` is excluded and untouched
(canonical `application-log-store.ts` default). Other `.asa`, PostgreSQL, MinIO
and application `.env` are outside scope. Unknown diagnostic siblings block for
boundary review. This is not a whole-installation/all-stream backup. No API pause.

## Plan and preconditions

Linux/Python3 standard library and trusted root-owned tool. Destination parent
must already be root0700; all ancestors root-owned and not group/world writable.
Symlink ancestors, source links/special files, overlapping paths and an existing
final target are rejected. Space: three times selected bytes plus64MiB and inodes.

```sh
sudo python3 scripts/diagnostic_forensic_snapshot.py \
  --installation /srv/asa-lab \
  --destination /var/backups/asa-forensics/h03-host-20261102
```

PLAN only reads; it neither establishes quiescence nor reserves space. Paths are
operator choices. Prepare the parent separately after review if needed:
`sudo install -d -o root -g root -m0700 /var/backups/asa-forensics`.

## Apply after independent review

Inspect the actual own collector service/timer and ensure no undeclared writer.
The tool reads the canonical runner registration: exact installation/project,
owner account resolved through passwd (no persisted UID is required), mode/name
and scriptDigest. Owner/config permissions, loaded WorkingDirectory/User and
closed source revision contracts are checked. Supported revisions: dfee5badd299d318a8e51ff0cbf88e9656333a5d
uses its canonical FOUR-source digest; reviewed3e32de6987665c4d7c1d33ba7ba82019afdf9cdb
uses FIVE including deployment/operational.py. Unknown revisions, stale/missing
sources and inconsistent metadata fail closed; extending this table needs review.

SYSTEM/system-root uses the system manager. USER uses only the resolved nonzero
UID's existing private0700 runtime directory, owned bus socket and verified
loginctl Linger=yes/RuntimePath. Numeric setpriv drops UID/GID with no shell/PAM,
clear groups and a clean fixed environment before systemctl --user. No user manager
is started, and no system manager is substituted for an unavailable user bus.
The default unit is the registered canonical name, not a guessed legacy unit;
optional --collector-unit only asserts that exact identity. No dummy unit is needed.
Both must be inactive or failed with no running PID; an absent timer is allowed,
a missing service is not. A failed unit's ERROR is not cleared. If running,
separately quiesce only those own writers; the tool never issues stop/start/reset/
disable. Do not stop API. Use the actual existing unit name:

```sh
sudo python3 scripts/diagnostic_forensic_snapshot.py \
  --installation /srv/asa-lab \
  --destination /var/backups/asa-forensics/h03-host-20261102 --apply
```

The existing canonical `diagnostics/private/collector.lock` is held exclusively
without writing it. Directory/file descriptors use no-follow; each read compares
identity/size/times/hash. Whole selected inventory and unit quiescence are checked
again. This assumes trusted root and the canonical cooperating lock, not malicious
root or an undeclared writer. An observed race fails closed.

Raw files are first preserved byte-for-byte. Live SQLite is never opened, avoiding
even creation of live SHM. SQLite backup API runs on a separate frozen working
mirror with copied WAL/SHM. `quick_check` validates the resulting SQLite database,
not history ownership. Original raw DB/WAL/SHM/key remain preserved. Unreadable
SQLite leaves an incomplete private copy, not a final snapshot. Segment corruption
and missing receipts are preserved without repair.

New directories/files are0700/0600. Private manifest records exact source/copied
hashes and identities. Commit uses Linux `renameat2(RENAME_NOREPLACE)` plus fsync;
old targets are never replaced. Only the tool's disposable SQLite working mirror
is removed. On failure, private `.partial-*` evidence remains, never promoted.
Retain it for review; do not restore it as a completed snapshot.

Stdout is closed receipt/error code: no paths, records, key/config or SQLite error
text. Success says `FORENSIC_COPY_ONLY`, `HOST_COLLECTOR_ONLY`,
`recoverySnapshotVerified:false`, `NOT_A_VERIFIED_RECOVERY_SNAPSHOT`, with hashes/
counts and `legacyDebt` (missing metadata/receipt count or `UNKNOWN`). This count
is not a cryptographic audit; neither success nor quick_check makes history green.

## Custody and next steps

The copy contains private payloads and keys. Never upload it to GitHub/CI/chat or
ordinary log exports. Off-host storage requires approved encrypted transport or
age with an external public recipient; no private age key is requested. Verify
manifest hashes in isolation before a later rehearsal.

Recovery, quarantine/new history, restoring old snapshots and resuming collector
are separate reviewed procedures. This tool has no such commands. Never restore
SQLite/catalog/segments independently or erase the31-file debt.

Tests: `python3 -m unittest discover -s tests -p test_diagnostic_forensic_snapshot.py -v`.
Windows runs contract/error tests only; actual fd/flock/WAL/mode/CAS tests need
Linux. No H03 apply or runtime-consistency claim follows from source tests.
