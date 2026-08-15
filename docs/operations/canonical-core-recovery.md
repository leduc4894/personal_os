# Canonical Core Recovery Operations Guide

Operator contract for the repository-internal canonical core operations CLI
(`tools/canonical_core_operations.py`): identity bootstrap, canonical
current-source read, canonical backup creation, offline bundle verification,
empty-target restore, and the phase-one acceptance gate. PostgreSQL holds the
canonical state; Cloudflare R2 holds the canonical bytes (see
`docs/operations/object-storage.md`); the recovery bundle store is the only
new durable surface, and it is private, local and immutable.

## Command boundaries

```bash
uv run python tools/canonical_core_operations.py <subcommand>
```

| Subcommand | Flags |
| --- | --- |
| `bootstrap-identity` | `--username`, `--user-display-name`, `--workspace-key`, `--workspace-display-name`, `--device-name`, `--device-kind` (all required) |
| `read-current-source` | `--workspace-id`, `--source-id` (UUID), `--output-file` (all required) |
| `backup-create` | `--confirm-write-admission-disabled` (required for admission) |
| `backup-verify` | `--bundle-id` (UUID, required) |
| `restore-empty` | `--bundle-id`, `--target-database`, `--confirm-target-database` (all required) |
| `phase-one-acceptance` | none |

- **Parse happens strictly before any I/O.** `--help`/`--version` and every
  syntax failure (unknown flag, missing argument, non-UUID value) exit without
  reading a single environment variable, secret file, database or bundle path.
- **No prompts.** Every confirmation is a CLI flag checked before composition;
  the process never asks interactively.
- **One safe JSON document on stdout** (sorted keys, compact); safe registered
  diagnostics go to stderr. Raw object bytes, child `pg_dump`/`pg_restore`
  output, paths, digests, object keys and snapshot tokens never reach either
  stream. `read-current-source` writes bytes only to `--output-file`, opened
  exclusively (`"xb"`); an existing output file refuses with exit `2`
  (`output_file_exists`) and content is never printed.
- **Whole-command bound.** Every recovery/acceptance command runs inside a
  30-minute `asyncio.wait_for`; exceeding it exits `75`
  (`recovery_command_timeout`). `pg_dump` and `pg_restore` each carry their own
  10-minute subprocess bound.

| Exit | Meaning |
| --- | --- |
| `0` | Success — the operation completed and printed its safe JSON result. |
| `2` | CLI syntax error or exclusive-output refusal, decided before any environment or secret read. |
| `65` | Contract failure — validation, conflict or integrity (invalid bootstrap input, invalid or mutated bundle, non-empty target, integrity mismatch). |
| `69` | Dependency unavailable — reserved for a **non-retryable** dependency failure; the closed error registry currently contains no such code, so no live path exits `69` today. |
| `70` | Unexpected internal error. |
| `75` | Busy / retryable dependency failure — pending writers hold the snapshot (`snapshot_busy`), or a dependency (PostgreSQL, R2, Temporal) is unreachable after its bounded retry (all dependency codes in the registry are retryable, including `projection_dispatch_unavailable` for an unreachable Temporal target), or the whole-command timeout fired. |
| `78` | Configuration or authorization refusal — environment gate (`KNOWLEDGE_ENVIRONMENT` not exactly `local`/`test` for the gated subcommands), missing `--confirm-write-admission-disabled`, `--confirm-target-database` not equal to `--target-database`, bad settings/secret files, unusable backup root. |

The 69-vs-75 split is deliberate and closed: a retryable dependency failure is
the busy class (`75`); `69` stays reserved for a future non-retryable
dependency code. Retrying an exit-`75` failure later with the same arguments
is always safe — every gated operation is idempotent or refuse-only.

## Configuration

| Variable | Meaning |
| --- | --- |
| `KNOWLEDGE_ENVIRONMENT` | Must be exactly `local` or `test` for `backup-create`, `restore-empty` and `phase-one-acceptance` (missing defaults to `local`); any other value refuses with exit `78` before any I/O. |
| `KNOWLEDGE_CANONICAL_BACKUP_ROOT` | Absolute path to the operator-owned private backup root. Must exist, be a real directory (no symlink/reparse point), and retain at least **2 GiB** free space (admission refuses with `free_space_reserve` otherwise). |
| `KNOWLEDGE_DATABASE_HOST/_PORT/_NAME/_USER/_PASSWORD_FILE/_SSL_MODE` | The canonical PostgreSQL database, reused from the source-store fragment. The password is **secret-file-only** beneath `KNOWLEDGE_SECRET_ROOT`. |
| `KNOWLEDGE_R2_ENDPOINT`, `KNOWLEDGE_R2_BUCKET_NAME`, `KNOWLEDGE_R2_ACCESS_KEY_ID_FILE`, `KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE`, `KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT` | Reused unchanged from the object-storage fragment; see `docs/operations/object-storage.md` for the secret-file and bucket-isolation rules. |
| `KNOWLEDGE_TEMPORAL_TARGET`, `KNOWLEDGE_TEMPORAL_NAMESPACE`, `KNOWLEDGE_TEMPORAL_TASK_QUEUE` | Temporal dispatch settings used by `phase-one-acceptance` (defaults `127.0.0.1:7233`, `knowledge`, `source-ingestion`); see `docs/operations/source-publication.md`. |

`KNOWLEDGE_CANONICAL_BACKUP_ROOT` may reveal host layout, so the settings
object renders a constant redacted token in `repr`/`str` and the path is
excluded from every diagnostic, metric and error detail. Any other
`KNOWLEDGE_*` key in the environment is a terminal `configuration_unknown_key`
refusal.

**Windows note:** bundle staging paths (`.staging-` + UUIDv7 + random nonce +
`objects/sha256/xx/yy/` + 64-hex digest) can exceed the 260-character
`MAX_PATH` limit when `LongPathsEnabled=0`. Configure the backup root as a
short absolute path on such hosts.

## Identity bootstrap

- **Grammar.** `username` and `workspace-key` match `^[a-z0-9][a-z0-9._-]{0,63}$`
  (exact-trimmed); display names and device name are free text of at most 200
  characters with no control characters; `device-kind` is one of the closed
  tokens `obsidian`, `web`, `system`. Violations refuse with exit `65`
  (`identity_bootstrap_input_invalid` plus a closed reason token).
- **Replay semantics.** Re-running the exact same command returns outcome
  `existing` with the original user/workspace/device ids and the original
  `committed_at`, and writes no new row, no new audit row and no rejection
  event.
- **Conflict posture — never repairs.** A partially existing or drifted
  identity graph (bare user, changed display name, revoked bootstrap device)
  refuses with `identity_bootstrap_state_conflict` and leaves the stored state
  untouched. The bootstrap never deletes, updates or completes existing rows;
  resolving drift is a deliberate human decision outside this CLI.
- **Audit actions.** Success commits exactly one `identity.bootstrap_completed`
  audit row in the same transaction as the graph; a drift rejection writes one
  standalone `identity.bootstrap_rejected` audit row (when a trusted workspace
  exists to attribute it to) and emits the registered
  `identity_bootstrap_rejected` diagnostic event. Rejected values themselves
  never reach an audit row or diagnostic field.

## Backup lifecycle

**Admission gates, in order:** environment gate (`local`/`test`, before any
client, connection or path is opened; a refused gate records no metric or
event) → `--confirm-write-admission-disabled` present (its absence is an
admission refusal, exit `78`) → settings and secret files load →
`pg_dump`/`pg_restore` client tools present at the pinned 18.4 version →
backup root usable and 2 GiB free → quiesced snapshot opens (share-locking
writers; a second concurrent snapshot refuses `snapshot_busy`, exit `75`) →
schema head must equal the pinned revision inside the snapshot → bundle id
must not already exist.

**Bundle layout.** One directory per UUIDv7 bundle id beneath the backup root:

```text
<bundle-id>/
  manifest.json      # canonical JSON, nine keys, contract canonical_core_backup/v1
  manifest.sha256    # 64 lowercase hex + newline: the SHA-256 of manifest.json
  postgres.dump      # the pg_dump sidecar from the quiesced exported snapshot
  objects/sha256/…   # one sidecar file per referenced canonical object, content-addressed
```

Staging happens in an unguessable `.staging-<id>-<nonce>` sibling; every file
is created exclusively with per-file flush and fsync; the manifest and sidecar
are written **last**; the bundle is published with one atomic same-filesystem
rename while the snapshot transaction is still open. POSIX permission bits are
enforced `0700`/`0600` where available. **Bundles are immutable after
publish**: no command writes into, updates or re-finalizes a published bundle,
and `backup-create` refuses an existing bundle id rather than overwriting.

**Bounds.** At most four concurrent verified object reads during a backup;
object copies stream in 1 MiB chunks and re-verify digest and size against the
referenced-object claim; a failure abandons staging (removed completely),
closes readers, releases the snapshot and records the registered
`canonical_backup_failed` event.

**Offline verification (`backup-verify`).** Touches no PostgreSQL, R2 or
Temporal port — only the backup root. Steps in the exact order: path-boundary
resolve beneath the root (symlink/reparse rejected) → final-directory type
(staging-named directories rejected) → exact registered file tree (no extra
file or directory, no missing object) → sidecar grammar and digest against
`manifest.json` → strict manifest parse (duplicate JSON keys, unknown fields,
non-canonical bytes, wrong contract token all refuse) → dump size and streaming
SHA-256 against the manifest entry → every object's size and streaming SHA-256
against its entry → totals. Changed-file-during-verification is detected via
pre/post `fstat` identity, and hard-link aliasing across bundle files is
rejected. Every failure is the closed `canonical_recovery_bundle_invalid`
(exit `65`) with a reason token only.

**What the sidecar does and does not prove.** `manifest.sha256` proves that
`manifest.json` is exactly the bytes the backup wrote — it binds the manifest
to the dump and object digests the verifier then checks file-by-file. It does
**not** prove the bundle's origin (no signature or secret is involved: anyone
who can write the backup root can forge a self-consistent bundle), does not
prove the dump matches any particular live database (only a restore plus
post-restore verification proves recovered state), and does not by itself
verify `postgres.dump` or the objects — those carry their own digests inside
the manifest and are streamed and hashed independently.

## Restore

**Empty-target-only admission.** `--target-database` must name a database that
is application-empty (no canonical rows), whose PostgreSQL server reports
exactly the pinned `18.4` (a different version refuses as
`canonical_recovery_dependency_unavailable` with the `postgresql` dependency
token, exit `75` — retryable so the operator can stand up the right server),
and that carries no pre-existing schema head. `--confirm-target-database` must
equal `--target-database` exactly; any mismatch refuses with exit `78` before
I/O. A non-empty target refuses `canonical_recovery_target_not_empty` (exit
`65`) and is never merged into, truncated or overwritten.

**R2 before PostgreSQL.** Objects are restored and fully verified in canonical
object storage *before* `pg_restore` runs, so the database transaction never
commits references to absent or unverified bytes. Objects are restored
conditionally: an existing object is re-verified in place (never overwritten);
a missing one is streamed from the verified bundle sidecar through the
production `store_stream` full-verify path. At most four concurrent object
writes. A later failure never deletes already-restored objects — they remain
safe unreferenced content-addressed bytes.

**Single-transaction guarantee.** `pg_restore` runs with
`--single-transaction --exit-on-error`: the entire canonical graph (schema,
rows, pointers, audit history) commits once or not at all. A mid-restore
failure leaves the target database empty, exits `65`
(`canonical_recovery_restore_failed`) and requires no partial-state cleanup.

**Post-restore verification list.** Before the safe receipt is returned, the
restore re-verifies: the schema head equals the pinned revision; every
canonical table count equals the manifest's `canonical_counts`; current-pointer
resolution resolves every pointer (zero dangling); every manifest object is
full-verified from object storage **again** (restore-phase receipts are never
reused); and, when an acceptance probe is supplied, one canonical read returns
the exact expected bytes.

## Safety boundary

- **Local/test only.** The gated subcommands refuse any environment other than
  exactly `local` or `test`. There is no production posture for this CLI.
- **Unencrypted bundle placement.** The bundle contains canonical bytes and
  metadata in plain form. The backup root must sit on **encrypted at rest or
  ephemeral** private storage that never leaves the operator's control; the
  bundle must never be copied into shared storage, attached to tickets or
  uploaded as a CI artifact.
- **Prohibited actions.** Never list, delete or overwrite objects in the R2
  bucket by hand to "fix" a backup or restore (the CLI itself never lists or
  deletes from R2 at all). Never edit, re-finalize or delete files inside a
  published bundle — a mutated bundle must fail verification, and a suspect
  bundle is superseded by a new backup, not repaired. Never roll back
  `sources.current_version_id` or any canonical pointer manually. Never
  restore into a non-empty target or attempt a merge restore; recovery is
  empty-target-then-republish, by design.

## Corruption drills

The protected live drills live in
`tests/integration/canonical_core/test_live_r2_acceptance.py` (markers
`local_stack` and `r2_live`): same-size-same-media-type corruption detected
before any byte is exposed, missing referenced object failing closed without
mutation, pre-publication claim mismatch leaving no canonical pointer, backup
containing every referenced object byte-exactly, existing-object reuse with
mismatch never overwriting, and restore matching the source bundle plus a
post-restore canonical read. They run the production services against a real
`R2S3ObjectStore` on the dedicated private test bucket and the disposable
PostgreSQL 18.4 stack, never list the bucket, and clean up only exact keys the
run itself created.

The trusted-surface CI gate is
`.github/workflows/canonical-core-acceptance.yml`: it triggers on protected
`master` pushes, a daily schedule and manual dispatch (never fork pull
requests), composes the live R2 credentials as step-local mode-0600 files with
a credential-shape guard, runs the drill suite on a disposable
`knowledge-ci-<run>-<attempt>` stack, resets the exact project and asserts
zero leftover labelled resources on every exit path, and uploads only the
sanitized JUnit report as evidence. Local execution uses the same suite with
`LOCAL_STACK_TEST_PROJECT=knowledge-ci-<nonce>`, `CI=true`, the `R2_TEST_*`
variables and the two mode-0600 credential files; missing credentials fail,
never skip.

## Acceptance status (2026-08-15)

- Offline gates on branch `canonical-core-acceptance-recovery` (final code
  commit `76202b1` plus the 2026-08-15 final-review fix commits — CLI digest
  removal, diagnostic-sink wiring, restore-side error mapping): **green** —
  `uv run poe python-lint`
  ("All checks passed!"), `uv run poe python-type-check` ("Success: no issues
  found in 78 source files"), `uv run poe format-check` (formatted; TypeScript
  eslint clean), `uv run poe boundary-check` (5
  contracts kept, 8 architecture tests passed), `uv run pytest -q` full
  default suite (1421 passed, 19 skipped, 112 deselected).
- Disposable-stack integration gate: **green.** The Task 13 live run of
  `tests/integration/canonical_core -m local_stack` passed 15/15 (identity,
  canonical read, recovery) on a unique disposable project, including the
  controller's independent 5/5 re-run of the live identity module with zero
  leftover containers.
- Phase-one acceptance composition: the 256-test contract/unit set over the
  composition, CI-security and tool suites passed at Task 15.
- Protected live-R2 CI run: **pending first execution.** The
  `canonical-core-acceptance` workflow has not yet run — it triggers on the
  first protected push to `master` after this branch merges. No CI live-R2
  evidence exists yet; this line must be updated with the workflow run link
  once that first execution completes.
