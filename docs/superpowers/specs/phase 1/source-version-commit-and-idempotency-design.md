# Source-Version Commit and Idempotency Design

**Status:** Approved design target for Phase 1 implementation planning

**Scope owner:** Canonical source publication, optimistic concurrency, durable replay and projection-intent dispatch

**Depends on:** `phase-one-workspace-bootstrap-design.md`, `runtime-configuration-and-diagnostics-design.md`, `canonical-postgresql-baseline-design.md`, `content-addressable-object-storage-design.md`

**Followed by:** `canonical-core-acceptance-and-recovery-design.md`

## 1. Objective

Publish one immutable source version only after its canonical Cloudflare R2 object has been independently verified, and keep publication correct under retries, concurrent writers, process crashes and ambiguous database acknowledgements.

The publication boundary is one PostgreSQL transaction containing the source version, authoritative current pointer, sync event, projection intents and audit event. It consumes an internal `VerifiedObjectReceipt`; client-supplied hash, key, size and media type are never proof of storage.

Once a request commits, every exact replay returns the same canonical outcome without creating another version, event, intent or audit row. A stale base never silently overwrites a newer current version.

## 2. Scope

### 2.1 In scope

- Separate typed `create` and `update` commands.
- Read-before-upload idempotency preflight.
- Canonical request fingerprinting.
- Publication only from a current `VerifiedObjectReceipt`.
- PostgreSQL advisory locks and source-row locks.
- Atomic source/version/current-pointer/event/audit/intent writes.
- Optimistic concurrency using `base_version_id`.
- No-change acceptance without another version.
- Replay hydration from committed baseline rows.
- Bounded transaction retry and ambiguous-commit recovery.
- Projection-intent lease/dispatch to a deterministic Temporal workflow.
- Closed errors, diagnostics, metrics and leakage tests.
- PostgreSQL 18.4 and Temporal Server 1.31.2 integration tests.

### 2.2 Out of scope

- `rename`, `move`, `delete` and `restore`.
- Source locators, tombstones, conflict records and three-way merge.
- Obsidian SQLite/onboarding/manifest/cursor behavior.
- Public FastAPI or MCP contracts.
- R2 adapter implementation, multipart and optional Worker upload.
- Parsing, chunking, Qdrant, Neo4j or projection completion.
- AI proposal/approval writes and `approved_action` authorship.
- Content-object deletion, retention and garbage collection.
- A new application table or Alembic revision.

## 3. Selected approach

Use a transport-neutral application service in `personal_os.sources`, a separate PostgreSQL adapter package and a Temporal start adapter in the worker composition root.

Publication uses two serialization layers in one PostgreSQL `READ COMMITTED` transaction:

1. A transaction advisory lock derived from `(workspace_id, idempotency_key)` serializes replay identity.
2. A transaction advisory lock derived from `(workspace_id, source_id)`, followed by `SELECT ... FOR UPDATE` for an existing source, serializes source creation and pointer changes.

R2 verification and Temporal calls occur outside the database transaction. Changed publication writes a durable outbox pair; a dispatcher later starts one event-scoped workflow.

Hash collisions in advisory-lock keys may serialize unrelated requests but cannot merge them: all queries and constraints still compare complete workspace, key, event and source values.

### 3.1 Alternatives rejected

1. **Unique-constraint recovery alone.** Concurrent replay would use expected exceptions, rollback and a second transaction as its normal path.
2. **A new idempotency-ledger table.** It duplicates `sync_events` and expands the approved baseline for no Phase 1 correctness gain.
3. **`SERIALIZABLE` for every publish.** Source-local locking expresses the real contention boundary with fewer retries during initial import.
4. **Timestamp last-write-wins.** Client time is informational and can be skewed.
5. **A new version for unchanged bytes.** It creates false history and unnecessary projections.
6. **Content hash as idempotency identity.** Identical bytes may belong to distinct business events.
7. **Temporal start inside the transaction.** It holds locks across a network call without creating cross-system atomicity.
8. **One workflow per projection kind.** It would duplicate later extraction and chunk planning.

## 4. Packages and dependencies

### 4.1 Repository layout

```text
src/personal_os/sources/
  actors.py
  commands.py
  errors.py
  fingerprint.py
  ports.py
  publication.py
  projection_dispatch.py
  results.py

packages/postgresql-source-store/
  pyproject.toml
  src/postgresql_source_store/
    settings.py
    tables.py
    locks.py
    publication_store.py
    projection_intents.py
    error_mapping.py
    py.typed

apps/worker/src/workflow_worker/
  projection_workflow_starter.py
  projection_dispatch_runtime.py
```

`publication.py` orchestrates preflight, object verification and commit through ports. `publication_store.py` owns the transaction. `projection_intents.py` owns lease SQL but imports no Temporal type. The worker owns the Temporal SDK adapter and no source rules.

### 4.2 Import boundaries

- `personal_os.sources` may import provider-neutral core contracts, including `personal_os.object_storage`.
- Core must not import SQLAlchemy, psycopg, Temporal, FastAPI, MCP or a provider SDK.
- `postgresql_source_store` may import core, SQLAlchemy and psycopg only; it must not import R2, Temporal, Qdrant, Neo4j, Redis or composition roots.
- Only `workflow_worker` imports `temporalio`.
- API and MCP roots gain no public route in this phase.

Import-linter and architecture fixtures enforce these boundaries.

### 4.3 Exact dependencies

```text
postgresql-source-store
  knowledge-core==0.1.0
  SQLAlchemy==2.0.51
  psycopg[binary]==3.3.4

apps/worker
  knowledge-core==0.1.0
  postgresql-source-store==0.1.0
  temporalio==1.30.0
```

`temporalio==1.30.0` was the current stable PyPI release on 2026-08-14 and publishes CPython 3.14-compatible wheels. No second retry, outbox or UUID library is added.

## 5. Domain contracts

All values are immutable, strictly typed and provider-neutral. External payloads never deserialize into a trusted receipt or authorized actor.

### 5.1 Actor

```text
SourceActor
  actor_kind: user | device | system
  actor_id: UUID | None
```

- `user` requires the active workspace owner.
- `device` requires an active same-workspace device; the UUID populates version author, sync-event device and audit actor.
- `system` has no actor ID and is constructible only by an internal composition boundary.
- `approved_action` is rejected until the safe-actions phase.
- The PostgreSQL adapter rechecks active ownership/status as defense in depth.

### 5.2 Commands

```text
CreateSourceVersion
  workspace_id: UUID
  source_id: UUID
  event_id: UUID
  idempotency_key: IdempotencyKey
  source_type: SourceType
  title: SourceTitle
  actor: SourceActor
  expected_object: ExpectedObject
  client_timestamp: aware UTC datetime | None

UpdateSourceVersion
  workspace_id: UUID
  source_id: UUID
  event_id: UUID
  idempotency_key: IdempotencyKey
  base_version_id: UUID
  actor: SourceActor
  expected_object: ExpectedObject
  client_timestamp: aware UTC datetime | None
```

`source_id` is backend-issued before the transaction and retained for retry. An update cannot mutate source type, title or locator.

`IdempotencyKey` enforces printable ASCII `!` through `~`, length `1..200`, with no whitespace, control character or normalization. It is workspace-scoped, opaque and never logged.

### 5.3 Verified-object boundary

`ExpectedObject` is an untrusted semantic claim used for fingerprinting and verification. A new publication additionally requires `VerifiedObjectReceipt` from `CanonicalObjectStore`.

Before commit:

- receipt digest, derived key, size and media type equal `ExpectedObject` exactly;
- `verified_at` is aware UTC, not in the future and at most five minutes old;
- the receipt never crosses HTTP, MCP, Worker, Web App or Obsidian serialization;
- a stale receipt is re-created by `verify_existing_object()`, never by timestamp mutation.

A bounded database retry in the same service call may reuse the receipt. A new call obtains a fresh receipt unless preflight proves the operation already committed.

### 5.4 Result

```text
PublicationOutcome = published | no_change

SourceVersionPublicationResult
  source_id: UUID
  source_version_id: UUID
  content_version: positive int
  event_id: UUID
  event_sequence: positive int
  content_digest: ContentDigest
  outcome: PublicationOutcome
  committed_at: aware UTC datetime
```

`committed_at` is the sync event's database transaction time. Replay returns exactly these canonical fields; replay status is diagnostic-only.

### 5.5 Ports

```text
SourcePublicationStore
  resolve_committed(command, fingerprint, diagnostic_context)
    -> SourceVersionPublicationResult | None
  commit_create(command, fingerprint, receipt, diagnostic_context)
    -> SourceVersionPublicationResult
  commit_update(command, fingerprint, receipt, diagnostic_context)
    -> SourceVersionPublicationResult

ProjectionIntentStore
  reclaim_expired(now) -> int
  claim_batch(now, limit) -> tuple[LeasedProjectionIntent, ...]
  acknowledge_dispatched(intent_id, lease_token, now) -> bool
  release_retry(intent_id, lease_token, error_code, available_at, now) -> bool
  mark_terminal(intent_id, lease_token, error_code, now) -> bool

ProjectionWorkflowStarter
  start_or_get(request) -> WorkflowStartOutcome
```

Ports expose no SQLAlchemy row, database exception, Temporal handle or provider payload.

## 6. Request fingerprint

### 6.1 Validation order

Before I/O:

1. Parse non-nil canonical UUIDs.
2. Validate idempotency key, command and actor vocabulary.
3. Validate create title as exact trimmed Unicode of length `1..500` without control characters.
4. Normalize an aware client timestamp to UTC.
5. Validate `ExpectedObject` through object-storage value contracts.
6. Construct the fingerprint.

Title is not Unicode-normalized or case-folded. Stored title and fingerprint use the same exact code-point sequence after the trim check.

### 6.2 Canonical envelope

```text
contract                 source_version_publish/v1
command_kind             create | update
workspace_id             canonical lowercase UUID
source_id                canonical lowercase UUID
event_id                 canonical lowercase UUID
base_version_id          canonical lowercase UUID | null
source_type              closed token | null
title                    exact create title | null
actor_kind               user | device | system
actor_id                 canonical lowercase UUID | null
content_sha256           64 lowercase hex
content_size_bytes       integer
media_type               canonical media type
client_timestamp         UTC RFC 3339, six fractional digits, Z | null
```

Create uses null base and includes source type/title. Update includes base and explicit null source type/title. Null members remain present.

Excluded fields are idempotency key; request/client-request/trace IDs; receipt verification time/method; generated sequence/timestamps; and generated database UUIDs.

The validated map is UTF-8 JSON with sorted keys, separators `,` and `:`, no insignificant whitespace, `ensure_ascii=false` and `allow_nan=false`. `request_fingerprint` is lowercase SHA-256 over those bytes. The raw envelope is discarded and never logged. Golden fixtures pin every included/excluded field.

## 7. Idempotency preflight

```text
validated request and expected-object claim
  -> compute candidate fingerprint
  -> PostgreSQL resolve_committed()
     -> exact committed match: return result without R2 read
     -> mismatch: audit rejected and return typed error
     -> miss: store/verify R2 bytes
  -> commit transaction rechecks idempotency under lock
```

Preflight performs indexed lookups without a source lock:

1. Revalidate the active workspace/actor context before disclosing an outcome or writing rejection audit.
2. Search `(workspace_id, idempotency_key)`.
3. If found, require event ID and fingerprint to match, then hydrate the result.
4. If absent, search the globally unique `event_id`.
5. If that event belongs to another workspace or key, reject event-identity reuse without disclosing the existing tenant.
6. Otherwise return `None`.

A match joins the event's committed version and content object. This is not a byte read and creates no reference, so no R2 verification is needed. Later canonical byte reads still fail closed. Untrusted claims can only select one already committed outcome with the full same fingerprint; they cannot publish.

## 8. PostgreSQL transaction

### 8.1 Runtime bounds

Use SQLAlchemy 2 async Core with psycopg 3, no ORM identity map.

```text
connect timeout                         5 seconds
pool size                              4 per process
max overflow                           4 per process
pool checkout timeout                   5 seconds
lock_timeout                            5 seconds
statement_timeout                      15 seconds
idle_in_transaction_session_timeout   30 seconds
transaction retry attempts              3
```

SQL is schema-qualified and client values are bound parameters. The transaction performs no R2 or Temporal call.

### 8.2 Advisory locks

Use transaction-level two-integer advisory locks:

```text
IDEMPOTENCY_LOCK_NAMESPACE = 0x53564349
SOURCE_LOCK_NAMESPACE      = 0x53564353

idempotency material = workspace UUID + NUL + exact idempotency key
source material      = canonical source UUID
```

The second integer is the first four SHA-256 bytes interpreted as signed big-endian 32-bit. Source UUIDs are globally unique primary keys, so their lock material is global rather than workspace-scoped. Locks are always idempotency then source. Session advisory locks are prohibited; transaction locks release on commit, rollback, cancellation or connection loss.

### 8.3 Common prefix

1. Begin `READ COMMITTED` and set local timeouts.
2. Acquire idempotency lock.
3. Revalidate workspace and actor before returning any existing result.
4. Repeat key lookup: return exact replay or audit/reject mismatch.
5. Repeat global event-ID lookup and audit/reject cross-key or cross-workspace identity reuse without tenant disclosure.
6. Acquire source advisory lock.
7. Check `source_id` globally and enforce the requested workspace boundary before entering command-specific logic.
8. Execute create/update state transition.

Backend UUIDv7 values are allocated once per service invocation and reused through bounded transaction attempts. PostgreSQL supplies event sequence and transaction timestamps.

### 8.4 Content-object reuse

For a new content reference:

1. `INSERT ... ON CONFLICT (content_hash) DO NOTHING` using receipt metadata.
   On the first insert, both `verified_at` and `created_at` are the receipt's
   verified instant; PostgreSQL transaction time is not mixed with the
   application/R2 receipt clock.
2. Select the row by full hash.
3. Compare object key, size and media type exactly.
4. Reuse only an exact match; otherwise roll back with `source_content_object_conflict`.

The first `verified_at` and `created_at` remain unchanged on later
deduplication. Reference count is derived, never stored.

### 8.5 Create

Create requires that the source not exist after its advisory lock:

```text
reuse/insert content object
-> insert pending source with null pointer
-> insert version 1, parent null
-> set pointer and state active
-> insert create event: base null, committed new version
-> insert qdrant upsert intent
-> insert neo4j upsert intent
-> insert succeeded audit
-> commit
```

The version author derives from actor. Audit action is `source.version_published`. An existing source under a new event is `source_already_exists`; create never becomes update.

### 8.6 Update preconditions

After source advisory lock, select source/current version/current object `FOR UPDATE`. Accept only `active` and `stored_not_indexed`. Reject missing, pending, deleted, null/inconsistent current data, or a base unequal to `current_version_id`.

Base comparison occurs before content comparison. A stale client conflicts even when proposed bytes equal current. The safe error may include source ID, current version ID and ordinal, never content/title/path.

### 8.7 No-change

When receipt digest/key/size/media type equal the current object:

```text
insert update event
  base_version_id = current
  committed_version_id = current
-> insert succeeded audit, reason content_unchanged
-> commit
```

Do not insert content object/version/intent or update source pointer/state/`updated_at`. Equality of base and committed IDs is the persisted no-change marker.

### 8.8 Changed update

```text
reuse/insert content object
-> insert next ordinal, parent current
-> guarded pointer update WHERE current_version_id = base
-> insert update event: base old, committed new
-> insert two upsert intents
-> insert succeeded audit
-> commit
```

The guarded update must affect exactly one row or roll back with `source_concurrency_invariant_failed`. Source type/title remain unchanged. Existing sync state is preserved; even `stored_not_indexed` receives intents so later ingestion re-evaluates the new version.

### 8.9 Replay classification

| Event shape | Outcome |
|---|---|
| create, base null, committed non-null | published |
| update, base non-null and different from committed | published |
| update, base equals committed | no_change |

Any other create/update shape is an integrity error. Hydration rechecks event/version/object workspace and source containment.

## 9. Concurrency and crash recovery

### 9.1 Replay rules

- Same workspace key plus same event/fingerprint returns the committed result without mutation.
- Same key with another event/fingerprint is `source_idempotency_mismatch`.
- Same event under another key is `source_event_identity_mismatch`.
- Same content under a new event/key is a distinct attempt and follows normal rules.

Only a committed `sync_events` row consumes a new idempotency key. Rejected attempts are audited but do not reserve an absent key because the baseline has no rejected-outcome ledger.

### 9.2 Required concurrent outcomes

- Same-key requests: one commit, all others exact replay.
- Different-key updates from one base: one changed publish, later contender conflicts.
- Concurrent creates of one source: one create, later contender source-already-exists.
- Different sources with identical bytes: one global content-object row.
- No-change racing changed update: lock winner commits first; loser rechecks current and either no-changes against unchanged base or conflicts after pointer advance.

### 9.3 Database retry

Retry at most three times for deadlock, serialization failure and bounded lock contention, using cancellable random jitter from 50 to 250 milliseconds. Do not retry business conflict, identity misuse, receipt failure, metadata conflict or invariant failure.

Raw SQLSTATE, statement, parameters and driver message never leave the adapter.

### 9.4 Ambiguous commit

If commit acknowledgement is uncertain:

1. Discard the connection.
2. Open a new bounded connection.
3. Resolve key/event/fingerprint.
4. Return the committed result if found.
5. Retry only after a successful lookup proves absence.
6. If PostgreSQL remains unavailable, return retryable `source_commit_outcome_unknown`.

Never state that an ambiguous transaction rolled back without evidence. The caller retries the exact event and key.

### 9.5 Crash matrix

| Failure point | Required behavior |
|---|---|
| Before R2 verification | no receipt or database mutation |
| R2 verified, before DB | orphan object may remain; retry verifies again unless preflight hits |
| Before DB commit | entire transaction rolls back |
| Commit succeeds, response lost | exact replay hydrates result |
| Commit outcome unknown | new lookup resolves or returns retryable unknown |
| Commit before dispatcher | intents remain pending |
| Claim before Temporal | lease expires and is reclaimed |
| Temporal accepts, response lost | deterministic ID resolves existing execution |
| Temporal accepts, DB ack lost | lease retry resolves existing execution then marks dispatched |

No database failure triggers compensating R2 deletion.

## 10. Audit

### 10.1 Successful actions

| Outcome | Action | Result | Reason |
|---|---|---|---|
| create/update changed | `source.version_published` | succeeded | null |
| update unchanged | `source.version_no_change` | succeeded | `content_unchanged` |

Successful audit is in the canonical transaction. Actor and request/trace fields come from trusted contexts. Replay creates no new audit row.

### 10.2 Safe diff hash

`safe_diff_hash` is SHA-256 over sorted canonical JSON:

```text
contract = source_version_diff/v1
source_id
base_version_id | null
base_content_sha256 | null
new_content_sha256
```

Only the final digest is stored. The summary is discarded and never logged. No title, path or content enters audit. Rejected attempts leave it null.

### 10.3 Rejections

After a valid workspace/actor is established, business rejection commits standalone audit action `source.version_publish_rejected`, result `rejected`, with one closed reason:

```text
source_not_found
source_already_exists
source_state_invalid
version_conflict
idempotency_mismatch
event_identity_mismatch
actor_invalid
verified_receipt_stale
content_object_metadata_conflict
source_locator_conflict
```

`source_locator_conflict` rejects a small-file create whose bound initial locator collides with a foreign ACTIVE locator: the guarded pre-check inside the create's locked transition (2026-08-23) raises the typed, non-retryable `source_locator_conflict` (HTTP 409) before the locator insert, so the partial unique active-locator index violation never surfaces as a retryable outcome-unknown loop. The index remains the race-only final arbiter.

Malformed input before a trusted actor boundary produces only registered diagnostics. If PostgreSQL cannot write audit, the service reports database failure and never claims audit exists.

## 11. Projection dispatcher

### 11.1 Intent rows

Every changed create/update inserts exactly one Qdrant and one Neo4j `upsert` intent for the event/version. No-change inserts none. Intent means durable dispatch required, not projection completed.

### 11.2 Bounds and claim

```text
batch                              50 intents
concurrent Temporal starts          8
lease                              60 seconds
Temporal start timeout             10 seconds
backoff initial                     1 second
backoff multiplier                  2
backoff cap                          5 minutes
```

Claim available pending rows ordered by `(available_at, created_at, projection_intent_id)` with `FOR UPDATE SKIP LOCKED`; set leased status, UUIDv7 token and database-time expiry; commit before Temporal. Attempt count changes only when outcome is known or lease expires.

### 11.3 One workflow per event

```text
workflow type = SourceIngestionWorkflow
workflow id   = source-ingestion/{workspace_id}/{event_id}
task queue    = source-ingestion
```

Input contract `source_ingestion_reference/v1` contains only workspace, event, source and source-version UUIDs. Both intents derive identical workflow identity/input, avoiding duplicate future extraction. Raw content, title, object key, hash and vector never enter Temporal history.

Temporal can accept a start before an ingestion worker polls. Phase 1 therefore dispatches durable starts; they wait on the fixed task queue until Phase 3 registers the workflow.

### 11.4 Duplicate and fencing behavior

For running workflows use conflict policy `USE_EXISTING`; for closed workflows reject duplicate run. If the exact deterministic execution already closed, resolve it as accepted rather than starting another. Never terminate or replace an existing execution. Unexpected type/contract is terminal integrity failure.

All acknowledgement SQL includes exact intent ID, `status=leased` and lease token:

- started/existing: dispatched, attempt +1, clear lease/error, set dispatch/update time;
- retryable Temporal error: pending, attempt +1, clear lease, safe error and bounded availability;
- non-retryable contract error: terminal, attempt +1, clear lease, safe error;
- zero rows: stale lease, emit diagnostic and do not overwrite.

Temporal outage stays pending with capped backoff regardless of attempt count. Terminal is only for an unchanged request that cannot succeed.

Expired leases return to pending, increment attempt, clear lease, record `projection_dispatch_lease_expired` and apply backoff. Graceful shutdown stops claims, waits at most the start-call bound and leaves unknown attempts for lease expiry.

## 12. Runtime configuration and lifecycle

Publication reuses only:

```text
KNOWLEDGE_ENVIRONMENT
KNOWLEDGE_SECRET_ROOT
KNOWLEDGE_DATABASE_HOST
KNOWLEDGE_DATABASE_PORT
KNOWLEDGE_DATABASE_NAME
KNOWLEDGE_DATABASE_USER
KNOWLEDGE_DATABASE_PASSWORD_FILE
KNOWLEDGE_DATABASE_SSL_MODE
```

The worker adds:

```text
KNOWLEDGE_TEMPORAL_TARGET       default 127.0.0.1:7233
KNOWLEDGE_TEMPORAL_NAMESPACE    default knowledge
KNOWLEDGE_TEMPORAL_TASK_QUEUE   default source-ingestion
```

Settings are frozen and secret-file based. `DATABASE_URL`, `.env`, plaintext password and CLI password are unsupported. Local/test Temporal is the loopback-only unauthenticated exception already approved. Staging/production dispatcher activation is refused until the deployment spec supplies tested TLS/auth settings.

Pools and clients are composition-owned and never created on import. Liveness makes no network call. Readiness checks PostgreSQL connectivity/schema head; worker readiness also checks Temporal namespace. Backlog age may degrade readiness, not liveness. Schema/intent/namespace drift fails closed. Cancellation rolls back transactions and returns pool connections.

## 13. Error contract

Add closed registry codes:

| Code | Category | Retry | Safe details |
|---|---|---:|---|
| `source_publish_input_invalid` | validation | no | reason |
| `source_not_found` | conflict | no | source_id |
| `source_already_exists` | conflict | no | source_id |
| `source_state_invalid` | conflict | no | source_id, source_state |
| `source_version_conflict` | conflict | no | source_id, current_version_id, content_version |
| `source_idempotency_mismatch` | conflict | no | source_id |
| `source_event_identity_mismatch` | conflict | no | source_id, event_id |
| `source_verified_receipt_stale` | validation | no | reason |
| `source_content_object_conflict` | integrity | no | source_id |
| `source_concurrency_busy` | dependency | yes | source_id |
| `source_concurrency_invariant_failed` | integrity | no | source_id |
| `source_commit_outcome_unknown` | dependency | yes | source_id |
| `source_locator_conflict` | conflict | no | (none) |
| `projection_dispatch_unavailable` | dependency | yes | projection_kind |
| `projection_intent_contract_invalid` | integrity | no | projection_kind |

`source_locator_conflict` is the pre-registered lifecycle locator-conflict code, also carried by the publication exception since 2026-08-23: a small-file create whose bound initial locator collides with a foreign ACTIVE locator rejects with it (HTTP 409, non-retryable) from the guarded pre-check inside the create's locked transition, before the locator insert. Its registry definition admits no safe detail field; the rejected source identity rides the diagnostic event fields and the rejection audit row.

Safe messages are fixed. Receipt-stale is not generally retryable because retrying the same stale value cannot succeed; the application must reverify.

## 14. Diagnostics and metrics

Registered events:

```text
source_version_publish_succeeded
source_version_publish_replayed
source_version_publish_rejected
projection_intent_dispatched
projection_intent_dispatch_failed
projection_intent_lease_reclaimed
```

Allowed fields are closed UUID/token/integer values such as operation, outcome, duration, attempt count, content version, source/event/intent ID, projection kind and registered error fields.

Never emit raw content, title, path, locator, idempotency key, request fingerprint, full content hash, object key, receipt, client timestamp, SQL/parameters/DSN, password path/value, Temporal payload or provider exception message.

Required low-cardinality metrics:

```text
source_version_publish_total{operation,outcome}
source_version_publish_duration_seconds{operation,outcome}
source_version_replay_total{operation}
source_version_rejection_total{operation,reason_code}
source_version_transaction_retry_total{reason_code}
projection_intent_backlog{status,projection_kind}
projection_intent_oldest_pending_seconds{projection_kind}
projection_dispatch_total{projection_kind,outcome,error_code}
projection_dispatch_duration_seconds{projection_kind,outcome}
projection_lease_reclaimed_total{projection_kind}
```

IDs, keys and digests are never metric labels.

## 15. Test strategy

### 15.1 Unit and static contracts

- Validate every command, actor, key and receipt boundary.
- Golden fingerprint bytes/hashes for create/update, Unicode, UTC, nulls and exclusions.
- Hydrate published/no-change and reject impossible event shapes.
- Golden safe-diff hashes.
- Lock namespaces, derivation, order and signed conversion.
- Retry/backoff with injected clock/randomness.
- Dispatcher lease, expiry, stale ack and workflow input.
- Closed error/diagnostic/metric registries and sentinel leakage.
- Import rules; no session advisory lock; no R2/Temporal call in transaction.
- No migration/head change and no public API.

### 15.2 PostgreSQL 18.4 integration

On a unique disposable local-stack project prove:

1. Create produces one source/version/event/two intents/audit and correct pointer.
2. Changed update produces next ordinal, parent and pointer.
3. No-change produces event/audit only and leaves source timestamp unchanged.
4. Replay returns equivalent serialized result and unchanged row counts.
5. Key/event misuse and stale base reject with safe audit.
6. Missing/pending/deleted sources and invalid actors reject.
7. Same bytes across concurrent sources create one content object.
8. Metadata mismatch fails closed.
9. Two updates from one base yield one publish and one conflict.
10. Concurrent exact replay yields one canonical event.
11. Concurrent create yields one source and one rejection.
12. Faults after each insert/update prove whole rollback.
13. Post-commit connection loss resolves through a new lookup.
14. Unavailable resolution returns unknown outcome, never false rollback.
15. Cancellation releases locks and pool checkout.

Populate at least 10,000 versions and 10,000 pending intents; `EXPLAIN` must show approved indexed current/replay/history/claim paths. Exercise 100 concurrent exact replays and distinct-source concurrency. No hard CI latency target is set before production benchmarking; unbounded query shape, leaked connection or missed deadline fails.

### 15.3 Temporal dispatch integration

Against Temporal Server 1.31.2:

- two dispatchers never own one lease;
- two event intents resolve one workflow run;
- start succeeds without an active poller;
- running and closed duplicates create no second run;
- transient outage returns pending with bounded backoff;
- invalid contract becomes terminal;
- crashes before start, after accept and before DB ack recover;
- expired lease increments attempt and stale token cannot acknowledge;
- history/input contains only the approved version tag and four UUID references;
- cancellation leaves no corrupt state.

### 15.4 CI and leakage

Cross-platform verify runs unit/static tests. Ubuntu runs disposable PostgreSQL/Temporal integration with finite timeout and exact-label cleanup. Windows does not require Docker. No R2 credential is needed; the final canonical acceptance spec owns live R2-to-PostgreSQL coverage.

Sentinels in content, title, key, fingerprint, SQL parameters, password data and simulated exceptions must not appear in logs, traces, exception serialization, stdout/stderr, JUnit or artifact manifests. CI uploads no database dump, Temporal history, raw service log or environment dump.

## 16. Acceptance criteria

1. Only create/update are implemented.
2. A new publication requires an exact internal receipt no older than five minutes.
3. Preflight returns committed replay without R2, while commit rechecks under lock.
4. Create atomically commits version 1, pointer, event, two intents and audit.
5. Changed update requires exact base and atomically advances ordinal/pointer.
6. No-change is checked after base and creates no version/pointer/intent work.
7. Exact replay returns original version/event sequence/outcome/time without duplicate rows.
8. Reused key/event with another semantic request rejects and audits.
9. Concurrent races produce the deterministic outcomes in this spec.
10. Content-object reuse requires exact verified metadata.
11. Rollback leaves no partial canonical graph.
12. Ambiguous commit is resolved or reported retryable unknown, never guessed.
13. Changed events create one Qdrant and one Neo4j intent; no-change creates none.
14. Both intents map to one deterministic `SourceIngestionWorkflow` execution.
15. Intent lease/retry/terminal/ack transitions are fenced and crash-safe.
16. Temporal outage loses no intent and triggers no provider fallback.
17. Audit/telemetry pass sentinel leakage tests.
18. Query plans remain indexed at 10,000-row fixtures.
19. Alembic head and nine-table schema remain unchanged.
20. Lint, strict typing, architecture, unit, PostgreSQL, Temporal and build gates pass on one commit.

## 17. Expected deliverables

- Core source command, actor, fingerprint, result, error, port and service modules.
- `postgresql-source-store` workspace package.
- Preflight and atomic publication store.
- Projection intent store, dispatcher and Temporal start adapter.
- Exact uv lock update with `temporalio==1.30.0`.
- Architecture/error/diagnostic/metric registry updates.
- Unit, contract, PostgreSQL 18.4 and Temporal 1.31.2 integration tests.
- Canonical documentation updates.
- No migration, public API or projection implementation.

## 18. Primary references

- [PostgreSQL 18 advisory lock functions](https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)
- [PostgreSQL 18 explicit locking](https://www.postgresql.org/docs/18/explicit-locking.html)
- [PostgreSQL 18 `SELECT`, `FOR UPDATE` and `SKIP LOCKED`](https://www.postgresql.org/docs/18/sql-select.html)
- [PostgreSQL 18 transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html)
- [SQLAlchemy 2.0 asyncio extension](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [psycopg 3 async operations](https://www.psycopg.org/psycopg3/docs/advanced/async.html)
- [Temporal Python SDK 1.30.0](https://pypi.org/project/temporalio/1.30.0/)
- [Temporal Python SDK repository](https://github.com/temporalio/sdk-python)
- [Temporal Workflow ID conflict/reuse protocol](https://api-docs.temporal.io/)
