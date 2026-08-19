# Canonical Core Acceptance and Recovery Design

**Status:** Approved design

**Phase:** 1 — Bootstrap and canonical core

**Depends on:** `phase-one-workspace-bootstrap-design.md`, `runtime-configuration-and-diagnostics-design.md`, `local-service-stack-design.md`, `canonical-postgresql-baseline-design.md`, `content-addressable-object-storage-design.md`, `source-version-commit-and-idempotency-design.md`

**Followed by:** Phase 2 Obsidian sync

## 1. Objective

Close Phase 1 with one executable proof that the canonical boundary works from an empty environment and can be recovered without inventing data.

The deliverable must:

- bootstrap one active user, one active workspace and one active first device;
- publish synthetic bytes through the real Cloudflare R2 and PostgreSQL path;
- read the current source through a fail-closed verified reader;
- prove exact idempotent replay and durable Temporal dispatch;
- detect a same-size corrupted canonical object before exposing any byte;
- create, verify and restore a consistent PostgreSQL-plus-R2 backup bundle;
- define one evidence-based Phase 1 completion gate.

This is a recovery-mechanics and acceptance deliverable, not the Phase 10 production backup platform. It creates no public API and does not implement source ingestion, projections or client sync.

## 2. Scope

### 2.1 In scope

- Immutable, strictly typed identity-bootstrap command and result.
- One atomic PostgreSQL bootstrap transaction using existing baseline tables.
- Exact-match idempotent bootstrap replay.
- Canonical current-source lookup and verified read service.
- Provider-neutral recovery manifest and bundle verification contracts.
- A repository-internal operations CLI.
- Local/test-only backup creation, offline verification and empty-target restore.
- PostgreSQL custom-format dump synchronized with the exact referenced R2 object set.
- Immutable directory bundles written by staging plus atomic rename.
- Real-R2 write/read, same-size corruption, missing-object and repair drills in the dedicated test bucket.
- Projection-intent dispatch through Temporal up to the Phase 1 boundary.
- Unit, contract, disposable PostgreSQL/Temporal and protected live-R2 gates.
- Canonical documentation, operator runbook and the required implementation handoff.

### 2.2 Out of scope

- Production backup scheduling, retention automation or rotation.
- A second provider, independent backup account or cross-failure-domain object replication.
- Built-in backup encryption or key management.
- WAL archiving, PITR and physical PostgreSQL backup.
- Restore into a non-empty canonical database.
- Merge restore, in-place production replace or automatic activation.
- R2 list, wildcard delete, overwrite or self-repair in production code.
- Automatic current-pointer rollback or source-state mutation after object corruption.
- Qdrant or Neo4j projection contents, rebuild or equivalence testing.
- Registration or execution of `SourceIngestionWorkflow`.
- Public HTTP/MCP endpoints, authentication tokens, sessions or UI.
- Multi-user bootstrap, user recovery or additional-device enrollment.
- Rename, move, delete, restore, object GC or retention enforcement.
- Production deployment readiness. Phase 10 owns production backup storage, encryption, schedules, RPO/RTO evidence and restore operations.

## 3. Selected architecture

Use a layered acceptance orchestrator that reuses production domain services and adapters. Acceptance code may compose the boundaries; it must not reimplement publication, object verification or transaction behavior.

```text
repository-internal operations CLI
  ├─ identity bootstrap service
  │    └─ PostgreSQL bootstrap store
  ├─ source publication service
  │    ├─ PostgreSQL publication store
  │    └─ Cloudflare R2 canonical object store
  ├─ canonical read service
  │    ├─ PostgreSQL current-reference store
  │    └─ Cloudflare R2 verified reader
  ├─ recovery service
  │    ├─ PostgreSQL exported snapshot + pg_dump/pg_restore
  │    ├─ Cloudflare R2 verified reader/conditional store
  │    └─ private local immutable bundle store
  └─ projection dispatcher
       ├─ PostgreSQL projection-intent store
       └─ Temporal workflow starter
```

The accepted alternatives are deliberately not used:

1. **Direct SQL/S3 smoke scripts.** They could pass while production services fail and would duplicate validation, privacy and error mapping.
2. **Test-only fake end to end.** It cannot prove real R2 corruption, PostgreSQL transaction or Temporal duplicate semantics.
3. **Production backup subsystem.** Encryption, scheduling, remote backup storage and operational authorization belong to Phase 10.
4. **Automatic object repair.** A read path must not mutate canonical state or hide an integrity incident.
5. **Merge/replace restore.** Identity conflict and destructive-target behavior are unnecessary for the Phase 1 proof.

## 4. Module and file boundaries

### 4.1 Proposed repository layout

```text
src/personal_os/identity/
  __init__.py
  contracts.py
  ports.py
  bootstrap.py

src/personal_os/recovery/
  __init__.py
  contracts.py
  manifest.py
  ports.py
  service.py

src/personal_os/sources/
  reading.py

packages/postgresql-source-store/src/postgresql_source_store/
  identity_bootstrap.py
  canonical_read.py
  backup_snapshot.py

tools/
  canonical_core_operations.py
  canonical_recovery_bundle.py
  postgresql_dump_process.py

tests/unit/identity/
tests/unit/recovery/
tests/unit/sources/test_canonical_read.py
tests/unit/postgresql_source_store/
tests/contract/canonical_core/
tests/integration/canonical_core/

.github/workflows/canonical-core-acceptance.yml
docs/operations/canonical-core-recovery.md
```

Names may be consolidated only when the resulting unit retains one clear purpose. Generic names such as `utils.py`, `helpers.py` or a phase-number-only module are prohibited.

### 4.2 Import rules

- `personal_os.identity`, `personal_os.recovery` and `personal_os.sources` remain provider-neutral.
- Core may import standard-library types and existing core contracts. It must not import SQLAlchemy, Psycopg, Temporal, aiobotocore, botocore or subprocess composition code.
- `postgresql_source_store` owns SQLAlchemy/Psycopg behavior and may not import R2 or Temporal.
- `r2_object_storage` keeps its existing provider boundary. Recovery adds no production list/delete/overwrite method.
- `tools/canonical_core_operations.py` is the only new cross-infrastructure composition root. It may import the two infrastructure packages and worker Temporal adapter.
- API, MCP and Worker do not import the tools module. No new deployable process or installed public service is created.

### 4.3 Dependencies

No new production Python or TypeScript dependency is required.

The operations CLI uses:

- the exact locked workspace packages;
- PostgreSQL 18.4 `pg_dump` and `pg_restore` binaries already supplied by the pinned stack;
- standard-library filesystem, hashing, JSON and subprocess primitives;
- Temporal Python SDK 1.30.0 through the existing worker adapter.

The CLI checks the PostgreSQL client major/minor contract before backup or restore. A missing, older, newer-major or unexpected binary fails closed before snapshot acquisition.

### 4.4 Ports

```text
IdentityBootstrapStore
  bootstrap(command, diagnostic_context) -> BootstrapIdentityResult

CanonicalSourceReadStore
  resolve_current(command, diagnostic_context) -> CanonicalSourceReference

CanonicalBackupSnapshotStore
  open_quiesced_snapshot(now) -> async context manager of CanonicalBackupSnapshot

RecoveryBundleStore
  create_staging(bundle_id) -> async context manager of RecoveryBundleWriter
  open_verified(bundle_id) -> async context manager of VerifiedRecoveryBundle

PostgresqlDumpProcess
  create_dump(snapshot_token, output_file, timeout_seconds) -> DumpReceipt
  restore_dump(input_file, target, timeout_seconds) -> RestoreReceipt
```

The exported PostgreSQL snapshot token is an infrastructure-private opaque value. It may flow only from `postgresql_source_store.backup_snapshot` to `PostgresqlDumpProcess` inside the composition call; it never enters a manifest, diagnostic, metric or public core result.

## 5. Identity bootstrap contract

### 5.1 Command

```text
BootstrapIdentityCommand
  username: Username
  user_display_name: DisplayName
  workspace_key: WorkspaceKey
  workspace_display_name: DisplayName
  device_name: DeviceName
  device_kind: obsidian | web | system
```

Validation is performed before I/O and matches the existing baseline exactly:

- `username` and `workspace_key` match `^[a-z0-9][a-z0-9._-]{0,63}$`;
- display names are exact-trimmed Unicode with length `1..200`;
- device name is exact-trimmed Unicode with length `1..200`;
- control characters are rejected;
- values are not Unicode-normalized or case-folded;
- device kind is closed;
- no UUID is accepted from the CLI.

The backend allocates UUIDv7 values once on the first successful invocation.

### 5.2 Result

```text
BootstrapIdentityOutcome = created | existing

BootstrapIdentityResult
  user_id: UUID
  workspace_id: UUID
  device_id: UUID
  outcome: BootstrapIdentityOutcome
  committed_at: aware UTC datetime
```

`committed_at` is the original database timestamp. An exact replay returns the same IDs and original timestamp. Names are not returned in diagnostic output.

For `created`, `committed_at` is the workspace row's database `created_at`; all bootstrap inserts use the same transaction timestamp. Replay returns that stored value rather than the replay time.

### 5.3 Atomic transaction

Use one PostgreSQL `READ COMMITTED` transaction:

1. Set the established local lock, statement and idle-transaction timeouts.
2. Acquire one transaction-scoped bootstrap advisory lock in a reserved namespace.
3. Read the canonical user/workspace state.
4. On an empty identity state, allocate the three UUIDv7 values.
5. Insert active user.
6. Insert active workspace owned by that user.
7. Insert active device owned by that user/workspace, with null `last_seen_at` and `revoked_at`.
8. Insert a succeeded audit event with action `identity.bootstrap_completed`, actor kind `system`, target kind `workspace` and target ID equal to the created workspace.
9. Commit once.

A fault after any insert rolls back all four rows. No identity row is created by migration or module import.

### 5.4 Replay and drift

When identity state already exists:

- require exactly one canonical user and one canonical workspace;
- require the requested username/workspace key and every display field to match exactly;
- require both status values to be active;
- find exactly one active device in that workspace with the requested name and kind;
- require the device to belong to the canonical owner;
- return `existing` without mutation or another audit row.

Additional valid devices created by later phases do not invalidate replay. Zero or multiple matching bootstrap devices, partial identity state, another user/workspace, changed display data, archived/disabled state or a revoked bootstrap device returns `identity_bootstrap_state_conflict` without repair.

If a trusted canonical workspace can be established, a rejected bootstrap writes a standalone safe audit action `identity.bootstrap_rejected`. If trust cannot be established, only a registered diagnostic event is emitted. The rejected values themselves are never logged.

## 6. Canonical current-source read

### 6.1 Read command and reference

```text
ReadCurrentSourceCommand
  workspace_id: UUID
  source_id: UUID

CanonicalSourceReference
  workspace_id: UUID
  source_id: UUID
  source_version_id: UUID
  content_version: positive int
  expected_object: ExpectedObject
  committed_at: aware UTC datetime
```

The PostgreSQL adapter loads source, current version and content object in one bounded read. It accepts `active` and `stored_not_indexed`. Missing, pending, deleted, null pointer, cross-source pointer or inconsistent object metadata fails closed.

### 6.2 Verified read flow

```text
validate UUIDs
-> PostgreSQL current-reference lookup
-> construct ExpectedObject from canonical metadata
-> CanonicalObjectStore.open_verified_reader(expected)
-> adapter completes full object read and verification
-> consumer receives bytes
```

The service never trusts client-supplied object metadata. It exposes no byte until the existing R2 adapter has validated exact key, ETag stability, size, media type and SHA-256. A caller cancellation closes the reader and removes local spool state.

Missing/corrupt bytes return the existing typed object-storage error. The read service does not update source state, current pointer, version, event, audit or intent. Successful restoration of the same bytes makes the same immutable version readable again.

The internal `read-current-source` command writes bytes only to an explicitly selected private output file created exclusively. It never prints content to stdout/stderr.

## 7. Full Phase 1 write/read acceptance flow

The acceptance run owns unique synthetic bytes, UUIDs, idempotency key and exact R2 cleanup manifest.

```text
empty disposable stack
-> bootstrap identity
-> bootstrap exact replay
-> construct synthetic source command as bootstrap device
-> source publication preflight miss
-> stream/store/full-verify Cloudflare R2 object
-> atomic PostgreSQL source publication
-> canonical current-source read
-> exact source publication replay
-> claim Qdrant and Neo4j projection intents
-> start/resolve one deterministic Temporal execution
-> verify canonical state and safe diagnostics
```

The run proves:

1. Bootstrap creates one user/workspace/device/audit graph.
2. Bootstrap replay returns the original result and no new row.
3. Publication creates source version 1, current pointer, event, two intents and audit atomically.
4. Canonical read returns the exact synthetic bytes.
5. Exact publication replay returns original version, sequence, outcome and time, performs no R2 call and adds no row.
6. Both intents become dispatched through fenced transitions.
7. Both intents derive identical `source-ingestion/{workspace_id}/{event_id}` workflow ID and closed four-UUID input.
8. Temporal contains one accepted execution. It may remain waiting on `source-ingestion`; Phase 1 does not register the workflow implementation.
9. No Qdrant collection, Neo4j graph data or Redis application state is required.

## 8. Recovery bundle contract

### 8.1 Directory layout

```text
<bundle_id>/
  manifest.json
  manifest.sha256
  postgres.dump
  objects/sha256/{first_2}/{next_2}/{sha256}
```

`bundle_id` is a backend-generated UUIDv7 rendered canonically. The final directory is immutable: creation fails if the target name already exists.

The writer uses a sibling staging directory named from the bundle ID plus an unguessable nonce. It creates directories/files exclusively, flushes and syncs every completed file, writes the manifest last, syncs the staging directory, atomically renames within the same filesystem and syncs the parent directory on POSIX. On Windows it flushes file handles before the same-volume rename; deployment/mount configuration owns crash-consistency guarantees not exposed by the platform API.

### 8.2 Manifest

```text
contract                         canonical_core_backup/v2
bundle_id                        UUIDv7
created_at                       UTC RFC 3339, six fractional digits, Z
source_environment               local | test
postgresql_server_version        18.4
postgresql_schema_revision       20260818_01
postgres_dump.format             custom
postgres_dump.relative_path      postgres.dump
postgres_dump.size_bytes         non-negative integer
postgres_dump.sha256             lowercase SHA-256
canonical_counts                 closed map of 28 table counts
objects[]
  content_sha256                 lowercase SHA-256
  object_key                     canonical derived key
  size_bytes                     0..104857600
  media_type                     canonical media type
  relative_path                  equal to object_key
```

Object entries are sorted by `content_sha256`. Keys and hashes are intentionally present inside the private backup artifact because they are required recovery evidence; they remain forbidden in logs, metrics, JUnit and uploaded CI artifacts.

`manifest.json` uses canonical UTF-8 JSON with sorted keys, compact separators, `ensure_ascii=false`, `allow_nan=false` and a final newline. `manifest.sha256` contains the lowercase SHA-256 of the exact manifest bytes plus one newline.

The sidecar detects accidental or partial bundle change; it is not an authenticity signature because Phase 1 introduces no signing key. Production authenticity, encryption and independent retention belong to Phase 10. Restore trusts a bundle only inside the configured local/test operator boundary and still re-verifies every restored canonical object.

Unknown top-level/member fields, duplicate keys/hashes, noncanonical ordering, path/key disagreement or an unsupported contract version is invalid. Contract evolution requires a new version and explicit reader support; it is never guessed.

The reader retains exact historical `canonical_core_backup/v1` compatibility:
v1 admits only its original nine baseline counts and re-encodes to identical v1
bytes. It also retains the exact branch-local legacy v2 twenty-table shape.
New backups always emit v2 with the current 28-table graph, including the eight
canonical authentication tables. V2 was strengthened in place because its
introduction had not escaped this branch; no third contract token is needed.
A restore verifies the schema revision and exact count set declared by its
validated manifest. Before a v1 target may serve, the operator runs the normal
forward Alembic migration; before either a v1 or legacy v2 target may serve,
the operator creates and verifies a current 28-count v2 backup. No reader
silently widens an older manifest's count witness.

### 8.3 Filesystem safety

- Backup root and bundle path are absolute and configured, never inferred from current directory.
- Every resolved child must remain beneath the configured root.
- Symlink, junction/reparse traversal, device file, FIFO and hard-link aliasing are rejected.
- Files are regular, exclusively created and not group/world writable on POSIX.
- Directory/file creation targets private permissions (`0700`/`0600`) on POSIX. As established by the runtime-configuration contract, Phase 1 does not claim to validate Windows ACL semantics; Windows relies on the explicitly configured operator-owned backup-root boundary and rejects reparse traversal.
- Staging cleanup uses only the exact resolved staging path created by the invocation.
- Bundle verification rejects missing and unregistered extra files.
- The backup root retains a 2 GiB free-space reserve; an admission failure occurs before a new object copy.

The bundle is unencrypted by Phase 1. It is allowed only in `local/test` and must reside on encrypted or ephemeral private storage. Production backup artifacts, storage and encryption belong to Phase 10.

## 9. Consistent backup creation

### 9.1 Environment and operator gate

`backup-create` is refused unless:

- `KNOWLEDGE_ENVIRONMENT` is exactly `local` or `test`;
- the operator supplies the exact confirmation `--confirm-write-admission-disabled`;
- database and R2 settings pass their existing offline validation;
- the database schema head is exactly `20260818_01`;
- the destination bundle does not exist;
- PostgreSQL 18.4 client tools are available;
- backup root safety and free-space checks pass.

The flag records operator intent; PostgreSQL locks provide the correctness barrier.

### 9.2 Quiesced exported snapshot

Use one bounded PostgreSQL transaction:

1. Begin `REPEATABLE READ` before the first query.
2. Acquire `SHARE MODE NOWAIT` table locks in the fixed order:
   `users`, `workspaces`, `devices`, `content_objects`, `sources`,
   `source_versions`, `sync_events`, `projection_intents`, `audit_events`,
   `user_credentials`, `web_sessions`, `totp_credentials`,
   `totp_recovery_codes`, `device_token_families`, `device_tokens`,
   `device_authorization_grants`, `authentication_throttle_buckets`,
   `workspace_policy_state`, `policy_signing_keys`, `policy_keysets`,
   `policy_keyset_signatures`, `source_policies`, `policy_rules`,
   `policy_drafts`, `policy_draft_rules`, `policy_evaluations`,
   `policy_reconciliation_intents`, `small_file_upload_operations`.
3. `SHARE` conflicts with DML `ROW EXCLUSIVE` locks but remains compatible with the `ACCESS SHARE` reads used by `pg_dump`.
4. Export the snapshot with `pg_export_snapshot()`.
5. Query the 28 current-v2 table counts and exact content objects referenced by
   `source_versions` from the same snapshot.
6. Launch one `pg_dump --format=custom --snapshot=<snapshot>` for the application database with no owner/privilege replay.
7. Copy the referenced R2 objects through the verified reader, at most four concurrently.
8. Recheck for observed queued DML before the final cutoff. An observed writer aborts bundle finalization; an operation arriving after the cutoff belongs after the snapshot.
9. Finalize and atomically rename the bundle while the exporting transaction remains open.
10. End the exporting transaction and release locks only after finalization.

Lock acquisition is bounded at 15 seconds. `pg_dump` is bounded at 10 minutes. One R2 read uses the existing five-minute logical-operation bound. The complete recovery command is bounded at 30 minutes.

The transaction performs no mutation. A failure cancels bounded children, closes readers/process handles, rolls back the snapshot transaction and removes only its exact staging directory. It never deletes or changes R2 or PostgreSQL canonical data.

### 9.3 PostgreSQL process credential boundary

The command never uses `DATABASE_URL`, `PGPASSWORD`, a password-bearing DSN or a CLI password.

It creates an ephemeral mode-`0600` libpq password file outside the bundle from the already validated secret-file value, sets only `PGPASSFILE` for the child and passes host, port, database and user as separate arguments. Both tools receive `--no-password`. The file is removed in `finally`, including cancellation. Command arguments, child environment and raw stderr are never logged or attached to errors.

The dump covers the entire canonical application database, including schema `knowledge` and the Alembic revision table; Temporal persistence remains excluded because it uses separate databases. The fixed semantic option set is:

```text
pg_dump
  --format=custom
  --no-owner
  --no-privileges
  --no-password
  --lock-wait-timeout=15000
  --snapshot=<opaque exported snapshot>
  --file=<exclusive staging postgres.dump>
  --host <host> --port <port> --username <user> <database>
```

The implementation passes these as an argument vector without a shell. It never uses `--no-sync`, parallel dump jobs, connection strings or stdout archive streaming.

`pg_dump` output is written directly to the exclusive `postgres.dump` file. The parent computes size and SHA-256 after successful exit and before manifest finalization.

## 10. Offline bundle verification

`backup-verify` performs no PostgreSQL, R2 or Temporal call.

It validates in this order:

1. Environment and root/path boundary.
2. Final directory type and absence of staging suffix.
3. Exact registered file tree without links or special files.
4. `manifest.sha256` grammar and exact manifest digest.
5. Manifest contract, canonical JSON and sorted unique object entries.
6. `postgres.dump` exact size and SHA-256.
7. Every object path/key derivation, size and SHA-256 by streaming reads.
8. Total object count/bytes against manifest totals.

Verification opens files without following links and checks identity/type again after open where the operating system supports it. A changed file during verification is an integrity failure.

Success returns only bundle ID, contract version and safe counts. It does not print paths, object keys, hashes or content.

## 11. Empty-target restore

### 11.1 Admission

`restore-empty` is refused unless:

- environment is `local` or `test`;
- the operator supplies exact target-project confirmation;
- the source bundle passes a fresh complete offline verification;
- target PostgreSQL is version 18.4 and contains no application schema/table or Alembic revision;
- target is not the source database/project;
- R2 configuration is the dedicated test/local bucket, never production;
- no process is permitted to serve the target during restore.

The target database may contain only PostgreSQL-required system objects and the default empty public schema.

### 11.2 Restore order

```text
verify complete bundle
-> restore/verify exact R2 objects
-> pg_restore PostgreSQL in one transaction
-> verify schema and canonical graph
-> canonical read smoke
-> write safe success receipt
```

R2 restores are bounded to four concurrent objects:

- missing key: stream bundle bytes through the existing conditional canonical store and full verification;
- existing key: full-verify exact key/hash/size/media and reuse;
- mismatched key: fail closed without overwrite, delete or fallback.

R2 is restored before PostgreSQL so the restored database never commits references to absent unverified bytes. If later database restore fails, the already restored objects are safe unreferenced CAS objects.

`pg_restore` uses the fixed semantic option set below and receives the archive path as a regular-file argument:

```text
pg_restore
  --single-transaction
  --exit-on-error
  --no-owner
  --no-privileges
  --no-password
  --host <host> --port <port> --username <user> --dbname <empty target>
  <verified postgres.dump>
```

It does not use `--clean`, `--create`, parallel jobs, partial selection or shell execution. It uses the same ephemeral libpq password-file boundary as backup. PostgreSQL documents `--single-transaction` as all commands succeeding or no changes being applied; failure therefore leaves the target application database empty and never partially activated.

### 11.3 Post-restore verification

Before success:

- Alembic head is exactly the revision declared by the verified manifest;
- the normalized migrated catalog matches the baseline contract;
- the exact versioned table-count set matches the manifest (nine for v1,
  twenty for legacy v2, 28 for current v2);
- bootstrap user/workspace/device IDs and status relationships match;
- every source current pointer resolves to its own immutable version/object;
- every referenced object full-verifies from R2;
- the acceptance source returns exact restored bytes through canonical read;
- no Qdrant, Neo4j, Redis or Temporal history is used to invent canonical data.

Success produces a private local receipt containing bundle ID, target project ID, completion time, safe counts and result. It contains no object key/hash/path or content and is not a production activation marker.

## 12. Corruption and missing-object drills

### 12.1 Test-only capabilities

The production `CanonicalObjectStore` remains unchanged. Exact overwrite/delete capability lives only in the protected live-test harness and accepts only a key registered in the current run's cleanup manifest.

The harness:

- never lists a bucket;
- never deletes a prefix or wildcard;
- never touches a key it did not create and register;
- uses the dedicated test bucket and test-only credentials;
- restores/cleans exact keys in `finally` and reports cleanup failure as a failed gate.

### 12.2 Same-size corruption

1. Publish a unique synthetic object and create a verified backup bundle.
2. Replace that exact test-owned key through the raw test client with different bytes of the same size and the same media type.
3. Read through the production canonical read service.
4. Require `object_storage_integrity_failed` after full SHA-256 verification.
5. Assert that zero bytes reached the consumer.
6. Assert source/version/current pointer/event/audit/intent state is unchanged.
7. Delete only the exact corrupt test key through the test harness.
8. Restore the original bundle object using production conditional-store behavior.
9. Read again and require exact original bytes from the same source version.

The test deliberately preserves size/media metadata so a HEAD-only implementation cannot pass.

### 12.3 Missing object and pre-publication failure

- Exact deletion of a test-owned referenced key must cause canonical read to return the existing missing-object integrity error without state mutation.
- A new publication whose claimed digest/size does not match its supplied bytes must fail before PostgreSQL creates source/version/current pointer/event/intent/audit rows.
- Neither case uses Qdrant, Neo4j, Redis or another provider as recovery input.

## 13. Internal CLI contract

The repository command is:

```text
uv run python tools/canonical_core_operations.py <subcommand>
```

Supported subcommands:

```text
bootstrap-identity
read-current-source
backup-create
backup-verify
restore-empty
phase-one-acceptance
```

CLI parsing happens before environment or secret-file reads. `--help`, `--version` and invalid syntax preserve the existing no-I/O command-shell behavior.

`backup-create`, `restore-empty` and `phase-one-acceptance` refuse staging/production before opening a database, R2 client, subprocess or bundle path. `bootstrap-identity` and `read-current-source` are internal capabilities but Phase 1 acceptance invokes them only against disposable local/test state.

Commands emit one safe JSON result on stdout and safe registered diagnostics on stderr. Raw child output is consumed and mapped, never forwarded. Exit codes follow the established repository classes:

```text
0   success
2   CLI syntax
65  validation/contract/integrity refusal
69  bounded dependency unavailable
70  unexpected internal failure
75  retryable busy/unknown outcome
78  configuration/secret/environment refusal
```

No command prompts interactively. Destructive test steps require exact non-secret confirmation arguments and exact target identities.

## 14. Runtime configuration

Reuse existing database, R2, runtime and Temporal settings. Add only:

```text
KNOWLEDGE_CANONICAL_BACKUP_ROOT
```

The value is an absolute local path. It is not a secret but remains excluded from diagnostics because it may reveal host layout. It joins the closed repository environment-name registry.

Plaintext database/R2 credentials, `.env`, ambient AWS credentials, production test credentials and settings dumps remain prohibited. Recovery code creates no client, pool, filesystem mutation or subprocess on module import.

## 15. Error contract

Add the smallest closed code set needed by the new behavior:

| Code | Category | Retry | Safe details |
|---|---|---:|---|
| `identity_bootstrap_input_invalid` | validation | no | reason |
| `identity_bootstrap_state_conflict` | conflict | no | none |
| `canonical_read_state_invalid` | integrity | no | source_id |
| `canonical_recovery_environment_refused` | authorization | no | operation |
| `canonical_recovery_configuration_invalid` | configuration | no | reason |
| `canonical_recovery_snapshot_busy` | dependency | yes | none |
| `canonical_recovery_bundle_exists` | conflict | no | bundle_id |
| `canonical_recovery_bundle_invalid` | integrity | no | reason |
| `canonical_recovery_target_not_empty` | conflict | no | none |
| `canonical_recovery_dependency_unavailable` | dependency | yes | dependency |
| `canonical_recovery_integrity_failed` | integrity | no | component |
| `canonical_recovery_restore_failed` | integrity | no | component |

Reuse existing database connection/schema, source-not-found and object-storage errors where their meanings already match. Do not wrap an existing precise integrity error in a less precise recovery code.

Safe `reason`, `operation`, `dependency` and `component` values are closed enums/SafeTokens, never provider text or paths. Bundle corruption is non-retryable until the bundle is replaced; dependency unavailability may be retried with the exact same immutable bundle and empty target.

## 16. Diagnostics, audit and metrics

### 16.1 Registered events

```text
identity_bootstrap_succeeded
identity_bootstrap_replayed
identity_bootstrap_rejected
canonical_source_read_succeeded
canonical_source_read_failed
canonical_backup_created
canonical_backup_verified
canonical_backup_failed
canonical_restore_succeeded
canonical_restore_failed
canonical_acceptance_completed
canonical_acceptance_failed
```

Allowed fields are closed safe scalars: operation, outcome, duration, counts, byte totals, bundle ID, user/workspace/device/source/version/event IDs and registered error fields.

### 16.2 Metrics

```text
identity_bootstrap_total{outcome}
canonical_source_read_total{outcome}
canonical_source_read_duration_seconds{outcome}
canonical_backup_total{operation,outcome}
canonical_backup_duration_seconds{operation,outcome}
canonical_backup_objects{operation,outcome}
canonical_backup_bytes{operation,outcome}
canonical_acceptance_total{outcome}
```

No ID, path, hash, key, source type, media type or error message is a metric label.

### 16.3 Forbidden disclosure

Never emit:

- raw object/source bytes;
- username, display names, device name or title;
- bundle root/path or object relative path;
- content hash, object key or request fingerprint;
- database host/name/user, DSN, SQL, parameters or snapshot token;
- secret path/value, ephemeral password-file path or child environment;
- raw `pg_dump`/`pg_restore` stdout/stderr;
- R2 endpoint/bucket/header/request ID/provider exception;
- Temporal input/history or serialized command/receipt.

The private bundle necessarily contains canonical hashes, object keys and bytes; it is never a log or CI artifact.

## 17. Resource, timeout and cancellation bounds

```text
snapshot/table-lock acquisition       15 seconds
pg_dump                               10 minutes
pg_restore                            10 minutes
R2 object operations                   existing 5-minute logical bound
concurrent backup object reads         4
concurrent restore object writes       4
backup-root free-space reserve         2 GiB
complete recovery command             30 minutes
protected CI job                       45 minutes
```

Cancellation:

- stops new object admissions;
- cancels/waits for bounded children;
- terminates then kills an unresponsive child process within a fixed grace;
- closes R2 clients/readers and database connections;
- rolls back snapshot or restore transaction;
- removes exact ephemeral password/staging files;
- leaves a finalized immutable bundle untouched;
- never deletes canonical R2 objects as compensation.

## 18. Test strategy

### 18.1 Unit tests

- Exact identity input grammar, Unicode, control characters and UUIDv7 allocation.
- Empty bootstrap, exact replay, drift classification and fault rollback using store fakes.
- Current-source reference hydration and invalid state shapes.
- Manifest canonical bytes, digest, ordering, duplicate and unknown-field rejection.
- Path traversal, link/reparse, special-file, changed-file and extra-file rejection.
- Atomic staging/finalization and exact cleanup.
- Offline dump/object checksum verification with streaming and bounded memory.
- Restore ordering, existing-exact object reuse and mismatch refusal.
- Subprocess argument/environment construction and secret-file cleanup.
- Timeout/cancellation behavior with injected clocks/processes.
- Closed error/event/metric registries and sentinel leakage.

### 18.2 Contract tests

- Core imports no provider/driver/process package.
- Production R2 adapter still exposes no list/delete/overwrite/copy/presign capability.
- Corruption capability exists only beneath the live-test harness.
- CLI parses before settings and refuses non-local/test recovery before I/O.
- No `DATABASE_URL`, `PGPASSWORD`, password CLI or raw child-output forwarding.
- No public API/MCP/OpenAPI/generated-client change.
- No new Alembic revision or baseline table.
- Workflow type/ID/input/task queue remain the approved source-publication contract.
- CI secrets are absent from fork PRs; artifact allowlist contains JUnit only.

### 18.3 Disposable PostgreSQL/Temporal integration

Against PostgreSQL 18.4 and Temporal Server 1.31.2:

1. Empty bootstrap creates exact graph and audit.
2. Exact bootstrap replay creates no row.
3. Partial/drift/revoked bootstrap fails closed.
4. Full synthetic source create/read/replay succeeds.
5. Publication fault leaves no partial graph.
6. Two projection intents converge on one Temporal execution.
7. Snapshot locks reject/bound observed concurrent DML.
8. Dump and manifest are from the same exported snapshot.
9. Bundle verify detects dump/object/manifest mutation.
10. Empty-target restore is single-transaction and exact.
11. Restore failure leaves target database empty.
12. Post-restore canonical read returns exact bytes.
13. Cancellation leaves no locks, checked-out connections, processes or staging files.

Every run owns a unique `knowledge-ci-*` project and performs exact-label resource cleanup in `finally`.

### 18.4 Protected live R2 acceptance

The protected workflow runs on trusted `master` pushes, a schedule and manual dispatch. It never runs with secrets on fork pull requests.

It proves:

1. Full R2 → PostgreSQL → verified-read path.
2. Idempotent replay bypasses R2.
3. Same-size/same-media object corruption fails SHA-256 verification before byte exposure.
4. Missing referenced object fails closed.
5. Pre-publication claim/byte mismatch creates no canonical pointer.
6. Backup contains every referenced object and exact bytes.
7. Exact-key deletion plus restore returns the original immutable version to readability.
8. Existing exact object is reused; mismatched existing object is never overwritten.
9. PostgreSQL restore and post-restore read match the source bundle.

The workflow uses a per-bucket concurrency group with `cancel-in-progress: false`, unique per-run keys and an exact cleanup manifest. Cleanup failure fails the job. The job uploads only scrubbed JUnit; it never uploads a bundle, dump, service log, environment dump or Temporal history.

Missing live credentials fail with an explicit blocked/failure status; the acceptance case is never silently skipped.

## 19. Phase 1 completion criteria

Phase 1 is complete only when all criteria pass on one final commit:

1. All seven Phase 1 design specs are implemented.
2. Empty workspace and all pinned packages build from lockfiles.
3. Runtime settings, secret files, typed errors and privacy tests pass.
4. Disposable local stack health/persistence/recovery smoke passes.
5. PostgreSQL empty upgrade, constraint, downgrade and re-upgrade gates pass with sole head `20260818_01`.
6. Offline and live Cloudflare R2 object-storage gates pass.
7. Identity bootstrap creates one active user/workspace/device atomically.
8. Exact bootstrap replay returns original UUIDs and creates no row.
9. Full canonical source write/read returns exact bytes.
10. Source publication replay is exact and bypasses R2.
11. Changed publication has two durable intents that converge on one Temporal execution.
12. Corrupt/missing object bytes are never exposed and cause no canonical-state mutation.
13. Verified backup bundle restores PostgreSQL plus every referenced object into an empty disposable target.
14. Restored source/version/current pointer/bytes match the source snapshot.
15. No Qdrant, Neo4j, Redis or Temporal history is used as canonical recovery input.
16. No public source API or Phase 3 ingestion workflow implementation is introduced.
17. Sentinel leakage tests cover application errors, diagnostics, subprocesses, JUnit and CI artifact manifests.
18. Ruff, mypy strict, import boundaries, Python/TypeScript tests and all builds pass.
19. Disposable PostgreSQL/Temporal and protected live-R2 acceptance pass on the final commit.
20. Canonical docs and `docs/operations/canonical-core-recovery.md` match the implemented commands and evidence.
21. Exactly one implementation handoff exists at `docs/handoff/2026-08-15-canonical-core-acceptance-and-recovery.md`.
22. Every accepted deferred item has exactly one index line in `docs/handoff/BACKLOG.md`; blocking acceptance gaps are not deferred.

No milestone status is inferred from an earlier commit, a skipped live case or upload success without restore evidence.

## 20. Expected deliverables

- Provider-neutral identity bootstrap contracts/service.
- Provider-neutral canonical current-source read service.
- Provider-neutral backup manifest and recovery orchestration contracts.
- PostgreSQL bootstrap, current-reference and exported-snapshot adapters.
- Private immutable filesystem bundle writer/verifier.
- Bounded `pg_dump`/`pg_restore` process adapter.
- Repository-internal canonical operations CLI and Poe commands.
- Unit, contract, disposable PostgreSQL/Temporal and protected live-R2 tests.
- Protected `canonical-core-acceptance` workflow.
- Canonical documentation and recovery runbook.
- One final implementation handoff and correctly indexed deferred work.
- No migration, public API, projection implementation, production backup schedule or new production dependency.

## 21. Primary references

- [PostgreSQL 18: SQL dump backup](https://www.postgresql.org/docs/18/backup-dump.html)
- [PostgreSQL 18: `pg_dump`](https://www.postgresql.org/docs/18/app-pgdump.html)
- [PostgreSQL 18: `pg_restore`](https://www.postgresql.org/docs/18/app-pgrestore.html)
- [PostgreSQL 18: snapshot synchronization functions](https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION)
- [PostgreSQL 18: explicit locking](https://www.postgresql.org/docs/18/explicit-locking.html)
- [Cloudflare R2: S3 API compatibility and conditional operations](https://developers.cloudflare.com/r2/api/s3/api/)
- [Cloudflare R2: durability and synchronous writes](https://developers.cloudflare.com/r2/reference/durability/)
- [Cloudflare R2: architecture and strong consistency](https://developers.cloudflare.com/r2/how-r2-works/)
