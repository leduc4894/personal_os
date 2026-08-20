# Child Five Readiness Remediation Design

## Purpose and scope

Close every deferred item whose delivery gate is **Before Child 5** before
starting the source-locator and tombstone lifecycle child. The scope is exactly
the eleven rows in `docs/handoff/BACKLOG.md` with that gate on 2026-08-20:

1. Five source-publication items from the 2026-08-14 handoff.
2. Four canonical-core items from the 2026-08-15 handoff.
3. One API-auth hygiene item from the 2026-08-16 handoff.
4. One small-file-sync sensitive value-object redaction item.

Rows gated for Child 7, Child 9, production activation, a later dependency
pin, or another explicit condition are out of scope and remain indexed.

## Compatibility contract

This remediation must preserve all public API, OpenAPI, database schema, and
closed error-code contracts. It adds no migration and no externally observable
error token. When an existing label is insufficiently precise, implementation
must distinguish the condition in internal diagnostics, event payloads, or
metrics only, using the already-approved safe-token vocabulary.

## Source-publication remediation

### Composition and telemetry

The source-publication composition root must bind the registered
`source_version_publish_*` diagnostics to the real service invocation. Events
must carry only IDs, safe tokens, and approved metadata; source content,
titles, idempotency keys, and credentials remain excluded.

`publish_total` must no longer report a retryable busy or ambiguous-commit
outcome as `rejected`. The implementation preserves the closed public metric
schema by using an existing compatible non-rejection outcome or a separate
internal metric/event, rather than introducing a new public label.

### Projection dispatcher

After a bounded retryable database failure, the dispatcher must remain alive
and retry its polling loop with bounded delay. It must still stop promptly on
explicit shutdown and must not spin on a persistent dependency failure. Leased
outbox rows remain crash-safe through their existing expiry/fencing contract.

Stale-lease diagnostics must be emitted only after the guarded transaction has
committed. A wrong fence token on an active lease must be diagnosed distinctly
from an expired lease; `stale_lease` remains diagnostic-only and never becomes
a metric label.

### Adapter boundaries

Source-publication adapter checks must reject the complete prohibited web and
provider import families already prohibited by the architecture, including
FastAPI, aiohttp, and boto3. The table-field contract test must compare field
maps by value, and validation code may move only when the resulting module has
a single domain purpose and leaves public imports unchanged.

## Canonical-core remediation

### Identity and canonical reads

Identity free-text validation must reject a non-string input with a typed
application error. Non-string username and workspace-key inputs must retain
their distinct existing reason semantics without adding a public error code.

Canonical reads must distinguish an application error raised by the consumer
body from a failed canonical read. Only reader/object verification failures
emit the read-failed event and increment its failure metric. Missing and
corrupt-object tests must assert both the metric and the event; consumer-error
tests must assert they are not emitted.

### Recovery contracts and process adapter

`CanonicalBackupSnapshot` and `ProcessRunResult` representations must redact
their sensitive token/output fields. A decoded JSON manifest whose root is not
an object must follow the existing canonical JSON rejection behavior, without
adding an error code.

The dump/restore adapter must drain bounded child output without a false
timeout after process exit, encode `.pgpass` fields according to PostgreSQL's
escaping rules, and map a restore timeout through the existing timeout error
mapping. Tests must cover each behavior without exposing process output or
password material.

## API-auth and small-file-sync remediation

API lifespan configuration failures must enter the structured diagnostic path
before FastAPI/Uvicorn reports failure. Keyring coverage validation must use
the authoritative database clock. The offline test store must keep username
and source throttle buckets independent, and malformed-JSON 400 responses
must be pinned to `Cache-Control: no-store`.

`SmallFileIdempotencyKey`, `NormalizedLocator`, and `UploadOperationToken`
must each redact their value in `repr`, following the established sensitive
value-object pattern. Their equality, serialization, and runtime values do not
change.

## Failure handling and observability

All external calls retain their existing timeout, bounded retry, error mapping,
and metrics boundaries. Diagnostic changes may report only safe tokens and
opaque identifiers. A remediation must fail closed if a canonical object,
database operation, or configuration is unavailable; no compensating object
deletion or best-effort publish path is introduced.

## Test and acceptance criteria

Each behavior starts with a focused failing test. The resulting suite must
prove that:

- source publication emits its registered diagnostics without sensitive data;
- retryable dispatcher database loss does not terminate the dispatcher loop;
- retryable publication outcomes are not counted as rejections;
- stale-lease diagnostics reflect committed fenced state;
- adapter import and table-metadata contracts reject forbidden/corrupt forms;
- identity, canonical-read, recovery, and subprocess edge cases follow the
  contracts above;
- API-auth diagnostics, clock/bucket behavior, and malformed-JSON cache header
  are pinned; and
- all three small-file value objects redact `repr` while preserving value
  behavior.

Focused unit, contract, and relevant integration tests, plus repository lint
and strict type checks for affected Python packages, must pass. After the
evidence is recorded, remove exactly these eleven rows from `BACKLOG.md`, leave
later-gated rows intact, and write one handoff at
`docs/handoff/2026-08-20-child-five-readiness-remediation.md`. Any newly
deferred work must have one concrete backlog row and an actionable delivery
gate.
