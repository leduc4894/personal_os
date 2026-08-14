# Source Publication Operations Guide

Operator contract for canonical source-version publication (`src/personal_os/sources`,
`packages/postgresql-source-store`, `apps/worker`). Publication is the only path that
creates a source version: one PostgreSQL transaction commits the version, the
authoritative current pointer, the sync event, the projection intents and the audit
event together, or nothing at all. Cloudflare R2 holds the immutable bytes; the
runbook for the object side is `docs/operations/object-storage.md`.

## Publication semantics

| Scenario | Behavior |
| --- | --- |
| Create | Inserts the source (pending, null pointer), version 1, sets pointer/state active, the create event, one Qdrant and one Neo4j upsert intent, and the succeeded audit — all atomically. |
| Changed update | Requires an exact `base_version_id` equal to `current_version_id`; inserts the next ordinal, advances the pointer with a guarded `WHERE current_version_id = base` update, the update event, two intents and the succeeded audit. |
| No-change | When the verified receipt equals the current object exactly, only an update event (base = committed) and a `content_unchanged` audit are written. No version, pointer, intent or source-timestamp change. Base comparison happens before content comparison, so a stale base conflicts even when bytes are unchanged. |
| Exact replay | The same workspace idempotency key with the same event ID and request fingerprint returns the original version, event sequence, outcome and `committed_at` with no R2 read and no new row of any kind. Preflight answers replays before touching bytes; the commit transaction rechecks under lock. |

A new publication requires an internal `VerifiedObjectReceipt` whose digest, derived
key, size and media type match the expected object exactly and whose `verified_at` is
at most **five minutes** old. A stale receipt is re-created by verifying the existing
object again — never by editing a timestamp. Receipts never cross HTTP, MCP, Worker,
Web App or Obsidian serialization.

## Concurrency and ambiguous commits

- The transaction takes two transaction-scoped advisory locks in fixed order:
  idempotency identity `(workspace_id, idempotency_key)` first, then source identity,
  then `SELECT ... FOR UPDATE` on the existing source row. Locks release on commit,
  rollback, cancellation or connection loss. Session advisory locks are prohibited.
- Transaction retries are bounded at three attempts and apply only to deadlock,
  serialization failure and bounded lock contention. Business conflicts, identity
  misuse, receipt staleness, metadata conflicts and invariant failures never retry.
- If a commit acknowledgement is uncertain, the service discards the connection,
  opens a fresh bounded connection and looks the key/event/fingerprint up. It retries
  only after that lookup proves absence; if PostgreSQL stays unavailable it returns
  the retryable `source_commit_outcome_unknown`. The system never guesses that an
  ambiguous transaction rolled back, and no database failure ever triggers a
  compensating R2 deletion.

## Projection dispatch outbox

Every changed create/update leaves exactly one Qdrant and one Neo4j upsert intent.
A dispatcher process claims them and starts one Temporal workflow per event:

```text
claim batch                        50 intents
concurrent Temporal starts          8
lease                              60 seconds
Temporal start/describe timeout     10 seconds
retry backoff                       min(300, 2 ** prior_attempt_count) seconds
```

- Workflow type `SourceIngestionWorkflow`, workflow ID
  `source-ingestion/{workspace_id}/{event_id}`, task queue `source-ingestion`.
  Both intents of one event derive the same identity and input.
- The input contract `source_ingestion_reference/v1` carries only the contract tag
  and the four UUIDs (workspace, event, source, source version). Raw content,
  titles, object keys, hashes and vectors never enter workflow input or history.
- Running duplicates resolve via conflict policy `USE_EXISTING`; closed executions
  reject the duplicate run and are resolved by a bounded describe into the accepted
  `existing` outcome. An execution is never terminated or replaced. Unexpected
  type/queue or an abnormal closure is the terminal integrity failure.
- **Phase 1 status:** the dispatcher queues `SourceIngestionWorkflow` starts but no
  Phase 1 worker registers the workflow implementation. Starts wait on the
  `source-ingestion` task queue until the Phase 3 ingestion worker polls; this is
  expected, not an outage.
- Dispatcher activation is limited to the local/test loopback exception
  (`KNOWLEDGE_TEMPORAL_TARGET`, default `127.0.0.1:7233`,
  `KNOWLEDGE_TEMPORAL_NAMESPACE` default `knowledge`,
  `KNOWLEDGE_TEMPORAL_TASK_QUEUE` pinned to `source-ingestion`). Staging/production
  activation is refused until a deployment spec supplies tested TLS/auth settings.
- The dispatcher runs as
  `uv run --package workflow-worker personal-worker dispatch-projections`.

Readiness for API/MCP checks PostgreSQL connectivity and schema head; worker
readiness additionally checks the Temporal namespace. Backlog age may degrade
readiness, never liveness, and liveness performs no network call.

## Operator recovery

### Distinguish a retryable unknown outcome from a known rejection

- `source_commit_outcome_unknown` (dependency, retryable) means PostgreSQL could
  not confirm or deny the commit. Retry the **exact original event ID and
  idempotency key**; preflight then returns the committed result or the retry
  proceeds safely. Never assume a rollback and never send a new event/key for the
  same logical write.
- Known rejections are the closed non-retryable conflict/validation codes — for
  example `source_version_conflict`, `source_idempotency_mismatch`,
  `source_event_identity_mismatch`, `source_already_exists`,
  `source_state_invalid`, `source_verified_receipt_stale`. Each is already
  committed as a `source.version_publish_rejected` audit row with a safe reason;
  no operator action can or should "push them through". A stale receipt requires
  the caller to reverify bytes, which produces a new receipt.

### Inspect only safe counts and statuses

Use the registered diagnostics
(`source_version_publish_succeeded` / `_replayed` / `_rejected`,
`projection_intent_dispatched` / `_dispatch_failed` / `_lease_reclaimed`) and the
low-cardinality metrics (`projection_intent_backlog{status,projection_kind}`,
`projection_intent_oldest_pending_seconds{projection_kind}`,
`projection_dispatch_total{...}`, `projection_lease_reclaimed_total{...}`) plus safe
column inspection of intent status/attempt counts. Do not query, dump or export
request fingerprints, idempotency keys, object keys, content hashes, titles,
SQL parameters or Temporal payloads — they must never enter logs, tickets or
dashboards.

### Let leases expire

A crashed or wedged dispatcher attempt holds its lease for at most 60 seconds.
Expired leases return the intent to pending, increment the attempt, record
`projection_dispatch_lease_expired` and apply the capped backoff automatically.
Wait for expiry and reclamation instead of intervening; the fencing token prevents
a stale attempt from overwriting a successor's transition.

### Retry the exact original event/key

Every safe retry replays the original event ID, idempotency key and request
payload. The deterministic workflow ID guarantees a retried dispatch converges on
the one existing `source-ingestion/{workspace_id}/{event_id}` execution instead of
creating a second run.

## Prohibited actions

- **No manual pointer edits.** Never update `sources.current_version_id`, version
  rows or event rows by hand; a pointer not committed by the publication
  transaction breaks the canonical contract.
- **No intent deletion.** Never delete or reset `projection_intents` rows to clear
  a backlog; leases, fencing and backoff own those transitions.
- **No R2 compensation.** Never delete an R2 object to "undo" a publication or a
  failed transaction; orphan objects are handled solely by the GC grace-period
  path.
- **No Temporal termination or replacement.** Never terminate, signal or
  re-run-replace a source-ingestion execution; duplicate dispatches are resolved
  by `USE_EXISTING`/duplicate-run rejection, not by operator intervention.

## Acceptance status (2026-08-14)

- Offline gate on the implementation commits: **green.**
  `uv run --no-sync poe verify` (format, Ruff, mypy strict, import boundaries,
  Python/TypeScript tests, builds) passed on the Phase 1 source-publication
  commits (`6d3b11f..2c95652`).
- Disposable integration gate: **green.** The disposable PostgreSQL 18.4 /
  Temporal 1.31.2 suites under `tests/integration/source_publication` and
  `tests/integration/projection_dispatch` (`-m local_stack`) pass on a unique
  per-run project label with exact-label cleanup.
