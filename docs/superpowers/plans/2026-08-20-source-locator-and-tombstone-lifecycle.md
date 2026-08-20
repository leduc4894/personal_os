# Source Locator and Tombstone Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Implement Child 5 rename, move, delete, explicit restore, stable source identity, canonical locator/tombstone state and rebuildable projection intents without changing canonical content bytes or source-version identity.

**Architecture:** PostgreSQL remains the sole owner of locator, tombstone, replay and lifecycle-event state. A framework-neutral lifecycle service validates closed commands, normalizes locator values, performs policy checks and delegates one atomic transition to the PostgreSQL adapter. The authenticated API exposes one lifecycle-event endpoint. The Obsidian plugin records immutable lifecycle events and dependencies in its portable SQLite journal, then dispatches them through a bounded foreground driver. Qdrant and Neo4j receive only durable projection intents referencing the retained current source version; they never become lifecycle authorities.

**Tech Stack:** Python 3.14, Pydantic 2, SQLAlchemy 2, psycopg 3, Alembic, PostgreSQL, FastAPI, Temporal projection dispatch, TypeScript 6 strict, Obsidian 1.13, sql.js, Vitest, WebdriverIO 9 and wdio-obsidian-service.

**Spec:** docs/superpowers/specs/2026-08-20-source-locator-and-tombstone-lifecycle-design.md

## Global Constraints

- Implement only Child 5. Do not add pull cursors, remote apply, conflict-file generation, multipart uploads, client-side encryption or automatic restore.
- Preserve stable source_id and current_version_id for rename, move, delete and restore. Lifecycle operations do not create source versions or rewrite immutable object bytes.
- PostgreSQL is canonical. Qdrant and Neo4j remain disposable projections driven by durable intents.
- Every lifecycle source-event projection intent, including delete, references the retained current source_version_id. Deletion semantics are carried by operation = delete.
- Reuse the existing source-publication lock order. Acquire the small-file operation fence when present, then source/idempotency/workspace locks in the documented order. Do not perform network I/O inside a database transaction.
- Normalize locators once through a framework-neutral value object. Do not introduce a sources-to-small_file_sync dependency.
- Raw locators may be stored only in canonical/local state that requires them. Never log, metric-label, audit-detail or error-envelope a raw locator, title, content digest, query, token or secret.
- Exact replay returns the original committed result. Same identity with a different immutable fingerprint fails closed with idempotency_conflict.
- Delete always writes a tombstone. Restore is explicit, checks tombstone/source/locator identity and never infers a remote resurrection from local file creation.
- Policy-denied or indeterminate rename/move still commits the truthful canonical locator transition but emits delete projection intents. Policy-denied or indeterminate restore closes the tombstone and restores canonical active state while also emitting delete intents.
- Preserve v1 source-publication fingerprints for requests without an initial locator. Use a locator-aware v2 fingerprint only when initial_locator is present.
- All database changes require Alembic upgrade/downgrade tests and backup manifest coverage. All API changes require OpenAPI, generated client and contract-test updates.
- Python must pass mypy strict; TypeScript must pass strict type checking. Add no production dependency.
- Follow TDD in every task: run the named failing test and read the expected failure before implementation.
- Preserve unrelated user changes. Use small semantic commits and do not begin Child 6 work.
- Before any live journey, follow .local/RESTART.md and the repository bootstrap scripts exactly. Do not print secrets or create a new tunnel.

## Deliverable and File Map

~~~text
src/personal_os/source_locators/
├── __init__.py
└── values.py                         Shared normalized sensitive locator value

src/personal_os/source_lifecycle/
├── __init__.py
├── commands.py                       Closed lifecycle commands and results
├── errors.py                         Safe stable error vocabulary
├── fingerprint.py                    Immutable lifecycle request fingerprints
├── metrics.py                        Redacted bounded metric labels
├── ports.py                          Store, policy and clock protocols
├── service.py                        Framework-neutral lifecycle orchestration
└── title.py                          Versioned filename-to-title derivation

packages/postgresql-source-store/src/postgresql_source_store/
├── tables.py                         Locator/tombstone/canonical column metadata
├── lifecycle_store.py                Atomic transition and exact replay adapter
├── publication_store.py              Atomic initial locator on source create
├── small_file_sync_operations.py     Transient locator evidence for publication
└── backup_snapshot.py                New canonical tables in backup/restore

migrations/versions/
└── 20260820_01_add_source_locator_lifecycle.py

apps/api/src/api_runtime/
├── source_lifecycle_models.py
├── source_lifecycle_routes.py
├── source_lifecycle_composition.py
└── application.py

apps/obsidian-plugin/src/journal/
├── lifecycle-contracts.ts
├── lifecycle-repository.ts
├── lifecycle-capture.ts
├── lifecycle-api.ts
├── lifecycle-driver.ts
└── status.ts

apps/obsidian-plugin/test/specs/
└── source-lifecycle.e2e.ts

docs/operations/
└── source-locator-tombstone-lifecycle.md
~~~

## Task 1: Establish the Shared Locator Value and Lifecycle Domain Contracts

**Files:**

- Create: src/personal_os/source_locators/__init__.py
- Create: src/personal_os/source_locators/values.py
- Create: src/personal_os/source_lifecycle/__init__.py
- Create: src/personal_os/source_lifecycle/commands.py
- Create: src/personal_os/source_lifecycle/errors.py
- Create: src/personal_os/source_lifecycle/fingerprint.py
- Create: src/personal_os/source_lifecycle/metrics.py
- Create: src/personal_os/source_lifecycle/ports.py
- Create: src/personal_os/source_lifecycle/title.py
- Modify: src/personal_os/small_file_sync/contracts.py
- Modify: src/personal_os/sources/commands.py
- Modify: src/personal_os/sources/fingerprint.py
- Create: tests/unit/source_locators/test_values.py
- Create: tests/unit/source_lifecycle/test_commands.py
- Create: tests/unit/source_lifecycle/test_fingerprint.py
- Create: tests/unit/source_lifecycle/test_title.py
- Modify: tests/unit/small_file_sync/test_contracts.py
- Modify: tests/fixtures/source_publication/fingerprint_golden.json
- Modify: tests/unit/sources/test_source_fingerprint.py

**Interfaces:**

~~~python
@dataclass(frozen=True, slots=True)
class NormalizedLocator:
    value: str

    def __repr__(self) -> str:
        return "NormalizedLocator(value=<redacted>)"

class LifecycleOperation(StrEnum):
    RENAME = "rename"
    MOVE = "move"
    DELETE = "delete"
    RESTORE = "restore"

@dataclass(frozen=True, slots=True)
class SourceLifecycleCommand:
    source_id: UUID
    event_id: UUID
    idempotency_key: str
    operation: LifecycleOperation
    expected_version_id: UUID
    expected_locator: NormalizedLocator | None
    target_locator: NormalizedLocator | None
    tombstone_id: UUID | None
    policy_revision: int
    client_timestamp: datetime | None
~~~

- [ ] Add failing tests proving the existing canonical NFC/slash grammar: non-NFC input, empty/dot segments, absolute paths, backslashes, controls, scheme/drive prefixes and overlong values are rejected; canonical Unicode/case is retained and repr is redacted.
- [ ] Add failing tests proving UUIDv7 event identity and each operation shape: rename/move require expected_locator and target_locator, delete requires expected_locator and forbids target/tombstone, restore requires target_locator and tombstone_id but forbids expected_locator, expected and target must differ, policy_revision is positive, and unknown operations fail closed.
- [ ] Add golden fingerprint tests proving deterministic field ordering across operation, source, expected version/locator, target, tombstone, policy revision, event and idempotency identity; preserve every existing v1 publication fingerprint and add a v2 golden only for CreateSourceVersion.initial_locator.
- [ ] Add failing derive_title_v1 tests proving only the final extension is removed, normalized Unicode is preserved, multiple-dot filenames retain earlier dots, and empty or more-than-500-character results are rejected.
- [ ] Run the focused tests and confirm failures are imports/missing behavior, not fixture or environment failures:

~~~powershell
uv run pytest tests/unit/source_locators tests/unit/source_lifecycle tests/unit/small_file_sync/test_contracts.py tests/unit/sources/test_source_fingerprint.py -q
~~~

- [ ] Implement the shared value object, closed commands/results/errors/ports and redacted metrics. Make small_file_sync import and re-export NormalizedLocator so existing callers remain compatible.
- [ ] Add initial_locator: NormalizedLocator | None = None to CreateSourceVersion. Keep source_version_publish/v1 byte-for-byte stable when it is None and select source_version_publish/v2 only when it is present.
- [ ] Rerun the focused tests, then strict checks:

~~~powershell
uv run pytest tests/unit/source_locators tests/unit/source_lifecycle tests/unit/small_file_sync/test_contracts.py tests/unit/sources/test_source_fingerprint.py -q
uv run poe python-lint
uv run poe python-type-check
~~~

- [ ] Commit: feat: define source lifecycle contracts

## Task 2: Add Canonical Locator and Tombstone Schema

**Files:**

- Create: migrations/versions/20260820_01_add_source_locator_lifecycle.py
- Modify: packages/postgresql-source-store/src/postgresql_source_store/tables.py
- Modify: packages/postgresql-source-store/src/postgresql_source_store/backup_snapshot.py
- Modify: src/personal_os/recovery/contracts.py
- Create: tests/unit/migrations/test_source_lifecycle_migration.py
- Create: tests/integration/source_lifecycle/test_lifecycle_migration.py
- Modify: tests/contract/source_publication/test_table_metadata.py
- Modify: tests/contract/test_canonical_postgresql_migration_contract.py

**Schema contract:**

~~~sql
CREATE TABLE source_locators (
    source_locator_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(workspace_id),
    source_id uuid NOT NULL REFERENCES sources(source_id),
    normalized_locator text NOT NULL,
    display_locator text NOT NULL,
    opened_event_id uuid NOT NULL REFERENCES sync_events(event_id),
    opened_sequence bigint NOT NULL,
    closed_event_id uuid NULL REFERENCES sync_events(event_id),
    closed_sequence bigint NULL,
    opened_at timestamptz NOT NULL,
    closed_at timestamptz NULL
);
CREATE UNIQUE INDEX uq_source_locators_active_workspace_path
    ON source_locators (workspace_id, normalized_locator)
    WHERE closed_event_id IS NULL;
CREATE UNIQUE INDEX uq_source_locators_active_source
    ON source_locators (source_id)
    WHERE closed_event_id IS NULL;

CREATE TABLE source_tombstones (
    source_tombstone_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(workspace_id),
    source_id uuid NOT NULL REFERENCES sources(source_id),
    delete_event_id uuid NOT NULL UNIQUE REFERENCES sync_events(event_id),
    retained_version_id uuid NOT NULL REFERENCES source_versions(source_version_id),
    retained_locator text NOT NULL,
    actor_kind text NOT NULL,
    actor_id uuid NOT NULL,
    deleted_at timestamptz NOT NULL,
    restore_event_id uuid NULL REFERENCES sync_events(event_id),
    restored_at timestamptz NULL
);
CREATE UNIQUE INDEX uq_source_tombstones_open_source
    ON source_tombstones (source_id)
    WHERE restore_event_id IS NULL;
~~~

- [ ] Write failing migration tests for the exact revision/down_revision pair, named FKs/checks/indexes, partial active-locator uniqueness by workspace/path and by source, one-open-tombstone uniqueness, lifecycle event/intent check values, source sync_state values and guarded destructive downgrade.
- [ ] Write a failing live PostgreSQL migration test that upgrades from 20260818_01, exercises duplicate-active-source and duplicate-active-path rejection, downgrades, then upgrades again.
- [ ] Add failing metadata/backup tests requiring source_locators and source_tombstones in the frozen SQLAlchemy table set and canonical v3 backup manifest.
- [ ] Run:

~~~powershell
uv run pytest tests/unit/migrations/test_source_lifecycle_migration.py tests/contract/source_publication/test_table_metadata.py tests/contract/test_canonical_postgresql_migration_contract.py -q
~~~

  Confirm the failures identify the missing revision/tables/manifest.

- [ ] Implement the Alembic migration with explicit checks and indexes. Extend sync_events.event_type with rename, move, delete and restore; preserve sources.sync_state as active, stored_not_indexed, pending or deleted and add the documented deleted timestamps/transition support.
- [ ] Update typed table metadata and backup/restore contracts. Do not silently backfill a guessed locator for legacy sources; preserve them as locator-unknown until a later explicit reconciliation.
- [ ] Verify upgrade/downgrade and query constraints:

~~~powershell
uv run pytest tests/unit/migrations/test_source_lifecycle_migration.py tests/integration/source_lifecycle/test_lifecycle_migration.py tests/contract/source_publication/test_table_metadata.py tests/contract/test_canonical_postgresql_migration_contract.py -q
uv run alembic upgrade head
uv run alembic downgrade 20260818_01
uv run alembic upgrade head
~~~

- [ ] Commit: feat: add canonical source lifecycle schema

## Task 3: Bind Initial Locator Evidence to Small-File Publication

**Files:**

- Modify: migrations/versions/20260820_01_add_source_locator_lifecycle.py
- Modify: packages/postgresql-source-store/src/postgresql_source_store/tables.py
- Modify: packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py
- Modify: packages/postgresql-source-store/src/postgresql_source_store/publication_store.py
- Modify: src/personal_os/small_file_sync/contracts.py
- Modify: src/personal_os/small_file_sync/ports.py
- Modify: src/personal_os/small_file_sync/service.py
- Modify: tests/unit/postgresql_source_store/test_small_file_sync_operations.py
- Modify: tests/unit/small_file_sync/test_service.py
- Modify: tests/integration/source_publication/test_create_transaction.py
- Modify: tests/integration/source_publication/test_small_file_operations.py
- Modify: tests/integration/small_file_sync/test_policy_and_device_boundaries.py

**Interfaces:**

~~~python
@dataclass(frozen=True, slots=True)
class BoundSmallFileOperation:
    operation_id: UUID
    normalized_locator: NormalizedLocator | None
    locator_fingerprint: str | None
    # Existing immutable operation fields remain unchanged.

async def commit_create(
    self,
    command: CreateSourceVersion,
    request_fingerprint: str,
    receipt: VerifiedObjectReceipt,
    diagnostic_context: DiagnosticContext,
    *,
    preflight_decision: PublicationPolicyDecision | None = None,
) -> PublishedSourceVersion:
    raise NotImplementedError
~~~

- [ ] Add failing tests proving new reservations persist normalized_locator plus locator_fingerprint, replays compare the locator fingerprint, terminal transitions clear the raw locator but retain its digest, and pre-migration rows with null locator remain readable.
- [ ] Add failing transaction tests proving create inserts the initial source_locators row, including display locator and opening event sequence, after sync_events and before projection intents/audit in the same commit; rollback leaves no source, version, locator, event, intent or audit row.
- [ ] Add a failing policy-race test proving publication commit reevaluates the bound locator under the locked current policy, including when policy changes between preflight and publication.
- [ ] Run:

~~~powershell
uv run pytest tests/unit/postgresql_source_store/test_small_file_sync_operations.py tests/unit/small_file_sync/test_service.py tests/integration/source_publication/test_create_transaction.py tests/integration/source_publication/test_small_file_operations.py tests/integration/small_file_sync/test_policy_and_device_boundaries.py -q
~~~

- [ ] Extend small_file_upload_operations with nullable transient normalized_locator and retained locator_fingerprint. Clear the raw locator on terminal success/failure without weakening exact replay.
- [ ] Pass the bound locator to CreateSourceVersion.initial_locator. Reserve a deterministic source_locator_id with the create identities, insert the locator atomically and evaluate policy against the bound locator under the transaction lock.
- [ ] Re-run the tests plus leakage contracts:

~~~powershell
uv run pytest tests/unit/postgresql_source_store/test_small_file_sync_operations.py tests/unit/small_file_sync/test_service.py tests/integration/source_publication tests/integration/small_file_sync/test_policy_and_device_boundaries.py tests/contract/source_publication/test_telemetry_leakage.py -q
~~~

- [ ] Commit: feat: persist initial source locator atomically

## Task 4: Implement Atomic PostgreSQL Lifecycle Transitions

**Files:**

- Create: packages/postgresql-source-store/src/postgresql_source_store/lifecycle_store.py
- Create: tests/unit/postgresql_source_store/test_lifecycle_store.py
- Create: tests/integration/source_lifecycle/conftest.py
- Create: tests/integration/source_lifecycle/test_lifecycle_transactions.py
- Create: tests/integration/source_lifecycle/test_lifecycle_concurrency.py
- Create: tests/integration/source_lifecycle/test_lifecycle_ambiguous_commit.py
- Modify: packages/postgresql-source-store/src/postgresql_source_store/projection_intents.py

**Adapter contract:**

~~~python
class PostgresqlSourceLifecycleStore:
    async def resolve_committed(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: str,
        diagnostic_context: DiagnosticContext,
    ) -> LifecycleCommitResult | None:
        raise NotImplementedError

    async def commit(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: str,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
    ) -> LifecycleCommitResult:
        raise NotImplementedError
~~~

- [ ] Add failing unit tests for exact SQL parameterization, fixed lock order, no raw locator in diagnostics and deterministic IDs for locator, tombstone, two projection intents and audit event.
- [ ] Add failing transaction tests for allowed rename/move, denied/indeterminate rename/move, delete, allowed restore and denied/indeterminate restore. Assert exact active/history locator rows and event sequences, truthful tombstone/source state, sync event, two intents and one redacted audit record.
- [ ] Add failing title tests proving rename removes only the final extension from the target filename, preserves normalized Unicode, rejects empty/over-500 titles and updates sources.title; move leaves title unchanged.
- [ ] Assert every intent has source_version_id equal to sources.current_version_id; delete intent operation remains delete. Assert denied rename/move/restore emit delete intents.
- [ ] Add failing rollback tests for locator collision, expected-locator mismatch, stale expected_version_id, restore of a non-deleted source and injected failures after each write boundary.
- [ ] Add failing concurrency/replay tests for same-event replay, same idempotency key with changed fingerprint and one rejection audit, two devices racing for one target locator, delete versus update, restore versus move and ambiguous commit resolution through a fresh bounded evidence lookup.
- [ ] Run:

~~~powershell
uv run pytest tests/unit/postgresql_source_store/test_lifecycle_store.py tests/integration/source_lifecycle -q
~~~

- [ ] Implement one transaction with exact order: replay lookup, idempotency identity lock, source advisory lock and row lock, old/target locator advisory locks in canonical text order and locator row locks, then open tombstone row lock for restore. Validate state/version/locator/classification/availability/policy before writing the transition, event, intents and audit. Reuse the at-most-three cancellable deadlock/serialization/lock-contention retry policy; never retry a business or integrity conflict.
- [ ] On ambiguous acknowledgement, discard the connection and perform one bounded lookup on a fresh connection. Return committed replay when evidence exists; otherwise return source_lifecycle_commit_outcome_unknown without guessing rollback.
- [ ] Verify no network calls occur in transaction and inspect query plans for active locator and replay lookups:

~~~powershell
uv run pytest tests/unit/postgresql_source_store/test_lifecycle_store.py tests/integration/source_lifecycle tests/contract/source_publication/test_no_network_in_transaction.py -q
~~~

- [ ] Commit: feat: commit atomic source lifecycle transitions

## Task 5: Orchestrate Policy, Replay, Errors and Metrics

**Files:**

- Modify: src/personal_os/source_lifecycle/ports.py
- Create: src/personal_os/source_lifecycle/service.py
- Modify: src/personal_os/source_lifecycle/errors.py
- Modify: src/personal_os/source_lifecycle/metrics.py
- Create: tests/unit/source_lifecycle/fakes.py
- Create: tests/unit/source_lifecycle/test_service.py
- Create: tests/contract/source_lifecycle/test_telemetry_leakage.py

**Service contract:**

~~~python
class SourceLifecycleService:
    async def commit(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> LifecycleCommitResult:
        request_fingerprint = fingerprint_lifecycle_command(command)
        replay = await self._store.resolve_committed(
            command, device_context, request_fingerprint, diagnostic_context
        )
        if replay is not None:
            return replay
        decision = await self._policy.evaluate_lifecycle(command, device_context)
        return await self._store.commit(
            command, device_context, request_fingerprint, decision, diagnostic_context
        )
~~~

- [ ] Add failing tests for exact replay before policy/network work, allowed and denied/indeterminate rename/move, unconditional delete, allowed and denied/indeterminate restore, cancellation and safe error mapping. Denied/indeterminate target policy must still commit truthful lifecycle state and select projection deletes.
- [ ] Add failing metrics/leakage tests proving only operation/outcome/error_code labels are accepted and raw locators, titles, fingerprints, tokens and content never appear in logs, spans, metrics or exception strings.
- [ ] Run:

~~~powershell
uv run pytest tests/unit/source_lifecycle/test_service.py tests/contract/source_lifecycle/test_telemetry_leakage.py -q
~~~

- [ ] Implement the service with bounded policy evaluation outside the transaction, store-side locked-policy verification and no retries around non-idempotent external work. Reuse canonical diagnostic context.
- [ ] Publish the spec's lifecycle errors exactly: source_lifecycle_input_invalid, source_locator_missing, source_locator_conflict, source_tombstone_not_found, source_tombstone_closed, source_lifecycle_version_conflict and source_lifecycle_commit_outcome_unknown, plus the existing canonical authentication/authorization/idempotency envelopes where applicable.
- [ ] Rerun:

~~~powershell
uv run pytest tests/unit/source_lifecycle tests/contract/source_lifecycle -q
uv run poe python-lint
uv run poe python-type-check
~~~

- [ ] Commit: feat: orchestrate source lifecycle events

## Task 6: Expose the Authenticated Lifecycle API and Generated Client

**Files:**

- Create: apps/api/src/api_runtime/source_lifecycle_models.py
- Create: apps/api/src/api_runtime/source_lifecycle_routes.py
- Create: apps/api/src/api_runtime/source_lifecycle_composition.py
- Modify: apps/api/src/api_runtime/application.py
- Modify: apps/api/src/api_runtime/server.py
- Create: tests/unit/api_runtime/test_source_lifecycle_models.py
- Create: tests/unit/api_runtime/test_source_lifecycle_routes.py
- Create: tests/unit/api_runtime/test_source_lifecycle_composition.py
- Create: tests/contract/api/test_source_lifecycle_routes.py
- Create: tests/contract/api/test_source_lifecycle_openapi.py
- Modify: packages/api-client/openapi.json
- Modify: generated files under packages/api-client/src/

**Wire contract:**

~~~http
POST /api/sources/lifecycle-events
Authorization: Bearer <opaque-device-token>
Content-Type: application/json

{
  "event_id": "uuid",
  "idempotency_key": "uuid",
  "source_id": "uuid",
  "operation": "rename|move|delete|restore",
  "expected_version_id": "uuid",
  "expected_locator": "folder/note.md",
  "target_locator": "folder/renamed.md",
  "tombstone_id": null,
  "policy_revision": 7,
  "client_timestamp": "RFC3339 or null"
}
~~~

- [ ] Add failing model tests for strict extra-forbid parsing, operation-dependent expected/target/tombstone fields, positive policy_revision, UUID/timestamp validation and safe response/error envelopes.
- [ ] Add failing route tests proving bearer authentication derives workspace/device/user, OBSIDIAN_SYNC scope is required, body workspace/device fields are rejected and domain errors map to stable HTTP responses.
- [ ] Add a failing OpenAPI golden test requiring operationId commitSourceLifecycleEvent, the closed enums, security scheme and deterministic generated path/schema types.
- [ ] Run:

~~~powershell
uv run pytest tests/unit/api_runtime/test_source_lifecycle_models.py tests/unit/api_runtime/test_source_lifecycle_routes.py tests/unit/api_runtime/test_source_lifecycle_composition.py tests/contract/api/test_source_lifecycle_routes.py tests/contract/api/test_source_lifecycle_openapi.py -q
~~~

- [ ] Implement thin Pydantic models/routes/composition. Reuse ACCESS_BEARER_SCHEME, extract_bearer_credential, authenticate_access and DeviceScope.OBSIDIAN_SYNC. Derive LifecycleDeviceContext, including workspace identity, exclusively from the bearer token.
- [ ] Export OpenAPI with uv run poe api-contract-export, regenerate with pnpm --filter @workspace/api-client run generate, and review the diff for unrelated schema churn.
- [ ] Verify:

~~~powershell
uv run pytest tests/unit/api_runtime/test_source_lifecycle_models.py tests/unit/api_runtime/test_source_lifecycle_routes.py tests/unit/api_runtime/test_source_lifecycle_composition.py tests/contract/api/test_source_lifecycle_routes.py tests/contract/api/test_source_lifecycle_openapi.py -q
uv run poe api-contract-check
~~~

- [ ] Commit: feat: expose authenticated source lifecycle api

## Task 7: Extend the Plugin Journal with Durable Lifecycle Records

**Files:**

- Create: apps/obsidian-plugin/src/journal/lifecycle-contracts.ts
- Create: apps/obsidian-plugin/src/journal/lifecycle-repository.ts
- Create: apps/obsidian-plugin/src/journal/lifecycle-contracts.test.ts
- Create: apps/obsidian-plugin/src/journal/lifecycle-repository.test.ts
- Modify: apps/obsidian-plugin/src/journal/contracts.ts
- Modify: apps/obsidian-plugin/src/journal/sqlite-database.ts
- Modify: apps/obsidian-plugin/src/journal/persistence.ts
- Modify: apps/obsidian-plugin/src/journal/repository.ts

**Local schema extension:**

~~~sql
CREATE TABLE lifecycle_event_operands (
    event_id TEXT PRIMARY KEY REFERENCES journal_events(event_id),
    source_id TEXT NOT NULL,
    expected_version_id TEXT NOT NULL,
    expected_locator TEXT NULL,
    target_locator TEXT NULL,
    tombstone_id TEXT NULL,
    policy_revision INTEGER NOT NULL,
    predecessor_event_id TEXT NULL,
    CHECK (policy_revision >= 1)
);

ALTER TABLE local_files ADD COLUMN last_locator TEXT NULL;
ALTER TABLE local_files ADD COLUMN open_tombstone_id TEXT NULL;
ALTER TABLE local_files ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'active';
~~~

- [ ] Add failing strict-type tests extending JournalOperation and closed lifecycle states with rename, move, delete and restore while preserving create/update behavior and immutable event identity. Add a schema migration test from the Child 4 journal without losing local_files, journal_events, attempts or manifest generation.
- [ ] Add failing repository tests for rename/move event insertion, delete plus retained local mapping in one SQLite transaction, restore successor, ordered predecessor dependencies, exact replay, terminal retention, prohibition on lifecycle coalescing and reconcile_required on corrupt/missing dependency evidence.
- [ ] Add failing leakage tests proving status/attempt projections expose only safe codes and counts, never expected_locator, target_locator or source IDs.
- [ ] Run:

~~~powershell
pnpm --filter @workspace/obsidian-plugin test -- lifecycle-contracts lifecycle-repository
~~~

- [ ] Implement a focused lifecycle repository over the existing serialized sql.js writer. Extend the shared journal operation vocabulary and state machine without changing create/update fingerprints or content-upload columns; keep lifecycle-only operands in the keyed extension table.
- [ ] Preserve two verified SQLite generations and include lifecycle tables in image verification/recovery. Make migrations transactional and deterministic.
- [ ] Verify:

~~~powershell
pnpm --filter @workspace/obsidian-plugin test -- lifecycle-contracts lifecycle-repository persistence sqlite-database
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
~~~

- [ ] Commit: feat: persist plugin lifecycle journal

## Task 8: Capture Rename, Move, Delete and Explicit Restore

**Files:**

- Create: apps/obsidian-plugin/src/journal/lifecycle-capture.ts
- Create: apps/obsidian-plugin/src/journal/lifecycle-capture.test.ts
- Modify: apps/obsidian-plugin/src/journal/capture.ts
- Modify: apps/obsidian-plugin/src/plugin.ts

**Capture boundary:**

~~~typescript
export interface LifecycleCapture {
  captureRename(file: TFile, priorPath: string): Promise<void>;
  captureDelete(file: TAbstractFile): Promise<void>;
  requestRestore(localFileId: string, targetPath: string): Promise<void>;
}
~~~

- [ ] Add failing tests proving a Vault rename on the same parent captures rename, a changed parent captures move, delete captures a tombstone event before local mapping removal and an untracked file does not mint a lifecycle event.
- [ ] Add failing tests for settled/bursty rename notifications, delete while create/update is pending, rename after a frozen content preflight, predecessor ordering and plugin unload during capture.
- [ ] Add failing tests proving automatic restore is allowed only when the retained local_file_id/source_id mapping and last committed hash both match unchanged bytes. Any unproven path reuse remains a new create or visible blocker; an explicit restore action may select a retained tombstone and a target path but still rejects changed bytes.
- [ ] Run:

~~~powershell
pnpm --filter @workspace/obsidian-plugin test -- lifecycle-capture capture
~~~

- [ ] Implement thin Vault event adapters that normalize paths, query stable local/source identity and persist lifecycle evidence before scheduling dispatch. Apply the existing 250 ms per-path settle before fingerprinting. Keep plugin.ts composition-only.
- [ ] Replace Child 4 deferred_lifecycle handling only for supported rename/move/delete. Freeze affected pending create/update work before recording lifecycle order. Preserve fail-closed behavior for missing identity or corrupt dependency evidence by setting reconcile_required.
- [ ] Verify:

~~~powershell
pnpm --filter @workspace/obsidian-plugin test -- lifecycle-capture capture repository
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
~~~

- [ ] Commit: feat: capture obsidian source lifecycle events

## Task 9: Dispatch Lifecycle Events through the Generated API Client

**Files:**

- Create: apps/obsidian-plugin/src/journal/lifecycle-api.ts
- Create: apps/obsidian-plugin/src/journal/lifecycle-api.test.ts
- Create: apps/obsidian-plugin/src/journal/lifecycle-driver.ts
- Create: apps/obsidian-plugin/src/journal/lifecycle-driver.test.ts
- Modify: apps/obsidian-plugin/src/journal/queue-driver.ts
- Modify: apps/obsidian-plugin/src/plugin.ts

**Driver contract:**

~~~typescript
export interface LifecycleApi {
  commit(event: FrozenLifecycleEvent, signal: AbortSignal): Promise<LifecycleResult>;
}

export interface LifecycleDriver {
  runOne(signal: AbortSignal): Promise<"idle" | "committed" | "blocked" | "retry">;
}
~~~

- [ ] Add failing API tests proving the generated POST /api/sources/lifecycle-events path type is used with bearer authentication, no workspace/device body fields, one AbortSignal and safe response mapping.
- [ ] Add failing driver tests for predecessor ordering, one active lifecycle request, exact replay, one-second-to-five-minute jittered retry with persisted next_attempt_at, non-retryable conflict/integrity errors, cancellation and unload.
- [ ] Add race tests for rename followed by content update, delete while update is awaiting retry, and restore followed by edit. The successor must not dispatch until the lifecycle predecessor is terminal-success.
- [ ] Run:

~~~powershell
pnpm --filter @workspace/obsidian-plugin test -- lifecycle-api lifecycle-driver queue-driver
~~~

- [ ] Implement generated-client transport and the bounded foreground driver. Share scheduling with the content queue so only one mutating request is active and dependency order is deterministic.
- [ ] Map retry only for offline, timeout, 429 and 5xx classes already allowed by the journal contract. Persist the server result before acknowledging local completion, and populate a restore successor's tombstone_id only from its committed delete predecessor.
- [ ] Verify:

~~~powershell
pnpm --filter @workspace/obsidian-plugin test -- lifecycle-api lifecycle-driver queue-driver
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
pnpm --filter @workspace/obsidian-plugin build
~~~

- [ ] Commit: feat: dispatch plugin lifecycle events

## Task 10: Add Safe User Controls, Status and Recovery Guidance

**Files:**

- Modify: apps/obsidian-plugin/src/journal/status.ts
- Modify: apps/obsidian-plugin/src/journal/status.test.ts
- Modify: apps/obsidian-plugin/src/plugin.ts
- Modify: apps/obsidian-plugin/README.md
- Create: docs/operations/source-locator-tombstone-lifecycle.md

- [ ] Add failing tests for redacted lifecycle counts, blocked reason codes, Sync now scheduling and Restore selected tombstone requiring explicit confirmation and a valid mapping.
- [ ] Add failing tests proving status notices and command errors never include paths, locators, source IDs, tokens, fingerprints or remote details.
- [ ] Run:

~~~powershell
pnpm --filter @workspace/obsidian-plugin test -- status plugin
~~~

- [ ] Wire lifecycle capture/driver/status through plugin composition. Add a narrowly named explicit restore command and permit automatic restore only for the proven retained mapping plus unchanged hash case; never infer restore from path reuse alone.
- [ ] Document state transitions, safe operator actions, reconcile_required handling, exact replay, deletion semantics and redacted diagnostics. Link to .local/RESTART.md for live setup instead of copying secrets or launcher details.
- [ ] Verify:

~~~powershell
pnpm --filter @workspace/obsidian-plugin test
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
pnpm --filter @workspace/obsidian-plugin build
~~~

- [ ] Commit: feat: add source lifecycle controls

## Task 11: Prove Cross-Boundary Races, Projection Compatibility and Recovery

**Files:**

- Create: tests/contract/source_lifecycle/test_projection_intent_contract.py
- Create: tests/integration/source_lifecycle/test_projection_dispatch.py
- Create: tests/integration/source_lifecycle/test_policy_races.py
- Create: tests/integration/source_lifecycle/test_query_plans.py
- Modify: tests/integration/projection_dispatch/test_temporal_dispatch.py
- Modify: tests/unit/workflow_worker/test_projection_dispatch_runtime.py
- Modify: tests/unit/postgresql_source_store/test_backup_snapshot.py
- Create: tests/integration/source_lifecycle/test_backup_restore.py
- Modify: tests/contract/source_publication/test_telemetry_leakage.py

- [ ] Add failing contract tests proving qdrant/neo4j lifecycle intents retain a non-null source_version_id, closed operation and lifecycle event identity. Demonstrate current dispatcher/Temporal ingestion references accept delete intents unchanged.
- [ ] Add failing policy-race tests for policy changes between API evaluation and store lock: denied rename/move commits the locator transition with delete intents; denied restore closes the tombstone, restores active canonical state and emits delete intents.
- [ ] Add failing concurrency tests for lifecycle versus publication lock ordering and prove there is no deadlock under bounded parallel create/update/rename/delete/restore traffic.
- [ ] Add failing backup/restore tests proving locator history, active uniqueness, tombstones, replay events and pending projection intents survive a canonical snapshot round trip.
- [ ] Add failing EXPLAIN assertions for replay by event/idempotency, active locator by source, active locator by workspace/path and pending projection intent claim.
- [ ] Run:

~~~powershell
uv run pytest tests/contract/source_lifecycle tests/integration/source_lifecycle tests/integration/projection_dispatch tests/unit/workflow_worker/test_projection_dispatch_runtime.py tests/unit/postgresql_source_store/test_backup_snapshot.py -q
~~~

- [ ] Implement only the adapter/dispatcher/recovery changes required by the failing contracts. Do not teach Qdrant or Neo4j to own canonical lifecycle state.
- [ ] Run focused and full Python gates:

~~~powershell
uv run pytest tests/unit/source_locators tests/unit/source_lifecycle tests/unit/postgresql_source_store tests/contract/source_lifecycle tests/integration/source_lifecycle -q
uv run poe python-lint
uv run poe python-type-check
uv run poe api-contract-check
~~~

- [ ] Commit: test: prove source lifecycle boundaries

## Task 12: Run Live Acceptance, Update Canonical Docs and Write One Handoff

**Files:**

- Create: apps/obsidian-plugin/test/specs/source-lifecycle.e2e.ts
- Modify: docs/04-OBSIDIAN_SYNC_AND_SOURCES.md
- Modify: docs/07-POSTGRESQL_DATA_MODEL.md
- Modify: docs/14-SECURITY_PRIVACY_AND_POLICY.md
- Modify: docs/16-TESTING_AND_EVALUATION.md
- Modify: docs/20-IMPLEMENTATION_PLAN.md
- Modify: docs/operations/source-locator-tombstone-lifecycle.md
- Create: docs/handoff/2026-08-20-source-locator-tombstone-lifecycle.md
- Create: tests/contract/source_lifecycle/test_reference_device_records.py
- Modify only if needed: docs/handoff/BACKLOG.md

**Live acceptance cases:**

~~~typescript
it("preserves source identity across rename move delete and explicit restore", async () => {
  const before = await readAuthenticatedSourceEvidence();
  await renameFixtureNote();
  await moveFixtureNote();
  await deleteFixtureNote();
  await explicitlyRestoreFixtureNote();
  const after = await readAuthenticatedSourceEvidence();
  expect(after.sourceId).toBe(before.sourceId);
  expect(after.currentVersionId).toBe(before.currentVersionId);
  expect((await readSafeLifecycleStatus()).pendingLifecycleCount).toBe(0);
});
~~~

- [ ] Write the failing WDIO journey before changing live behavior. It must exercise real Vault rename/move/delete and explicit restore through wdio-obsidian-service, the authenticated public HTTPS origin and the canonical backend; no mocks may replace this gate.
- [ ] Read .local/RESTART.md, then bring prerequisites up in its exact order with uv run poe stack-status, .local/serve-local.sh, Web Admin on port 38000, both .local/run-worker.sh policy workers and the existing knowledge-api-verify tunnel. Report only redacted presence/status, never secret values.
- [ ] Run the focused live Desktop journey:

~~~powershell
pnpm --filter @workspace/obsidian-plugin exec wdio run wdio.conf.mts --spec test/specs/source-lifecycle.e2e.ts
~~~

- [ ] Execute the physical Mobile acceptance matrix from the spec: tracked rename, move, delete, proven automatic restore, explicit restore, offline capture/reconnect, unload/reload and policy-denied transition. Record sanitized device/app/plugin version, UTC time, outcome and evidence reference in the operations guide; validate it with tests/contract/source_lifecycle/test_reference_device_records.py.
- [ ] Run migration, feature, plugin and full repository verification from a clean service state:

~~~powershell
uv run pytest tests/unit/migrations/test_source_lifecycle_migration.py tests/integration/source_lifecycle/test_lifecycle_migration.py -q
uv run pytest tests/unit/source_locators tests/unit/source_lifecycle tests/contract/source_lifecycle tests/integration/source_lifecycle -q
pnpm --filter @workspace/obsidian-plugin test
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
pnpm --filter @workspace/obsidian-plugin build
uv run poe verify
~~~

- [ ] Update canonical docs with the implemented contracts and Child 5 status. Keep operations detail in the living operations guide; do not duplicate it in the handoff.
- [ ] Inspect git diff, re-read the applicable AGENTS.md, and ensure no unrelated files, raw locators, credentials or generated noise entered the diff.
- [ ] Create exactly one handoff at docs/handoff/2026-08-20-source-locator-tombstone-lifecycle.md containing final commit SHA, every gate with command/evidence, interpretation decisions, deferred items and next actions.
- [ ] Add a BACKLOG row only for a genuinely deferred non-acceptance item. Give it one verifiable Implement by trigger such as Before Child 6 or Before production activation. Mobile acceptance and the required WDIO journey are not deferrable.
- [ ] Commit: docs: close source lifecycle child

## Spec Coverage Matrix

| Spec area | Plan coverage |
|---|---|
| Scope, approved decisions and boundaries | Global Constraints; Tasks 1, 4, 8 |
| Stable source/version identity and operation shapes | Tasks 1, 4, 8, 12 |
| source_locators, source_tombstones and legacy state | Tasks 2, 3, 4 |
| Command, endpoint and exact replay | Tasks 1, 4, 5, 6 |
| Lock order, atomic effects and policy races | Tasks 3, 4, 5, 11 |
| Audit, errors, telemetry and privacy | Tasks 1, 4, 5, 6, 7, 10, 11 |
| Plugin durable records, capture and dispatch | Tasks 7, 8, 9 |
| Recovery boundary | Tasks 7, 10, 11 |
| Failure matrix and concurrency | Tasks 4, 5, 9, 11 |
| Automated, Desktop and Mobile acceptance | Tasks 11, 12 |
| Deferred Child 6+ boundaries | Global Constraints; Task 12 |

## Execution Discipline

1. Before implementation, use superpowers:using-git-worktrees to create an isolated codex/ worktree unless the user explicitly chooses the current workspace.
2. Execute tasks in order. Tasks 1-6 establish canonical server contracts; Tasks 7-10 build the plugin against the generated API; Tasks 11-12 close cross-boundary and live gates.
3. For each checkbox group, preserve the red test output in the task notes before implementing. A test that passes before implementation does not satisfy the TDD gate; strengthen it until it proves the missing behavior.
4. Stop on an architectural contradiction, missing required live prerequisite or unrelated dirty-file overlap. Do not weaken, skip or mark a mandatory gate deferred.
5. After every task commit, record the SHA and exact verification commands in the single final handoff working section; consolidate it before closure.
