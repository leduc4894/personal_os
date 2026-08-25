# Device Cursor and Manifest Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Child 6 server-to-device cursor pull, crash-safe remote apply, exact echo suppression and checkpoint-bound manifest repair, including deterministic recovery after loss of the plugin SQLite journal.

**Architecture:** A framework-neutral `device_sync` domain owns the closed contracts, planning rules, errors and metrics. PostgreSQL adapters own cursor/event hydration and frozen manifest state, FastAPI exposes authenticated JSON and verified-binary routes, and a single Obsidian `SyncCoordinator` serializes outbound, inbound and repair mutations over portable sql.js state. PostgreSQL remains canonical; the Vault remains the user-controlled working copy; the plugin journal remains rebuildable.

**Tech Stack:** Python 3.14, Pydantic 2, SQLAlchemy 2, psycopg 3, Alembic, PostgreSQL, FastAPI, Cloudflare R2 through the existing verified object-store port, TypeScript 6 strict, Obsidian 1.13.1, sql.js, Vitest, WebdriverIO 9 and wdio-obsidian-service.

**Spec:** `docs/superpowers/specs/2026-08-26-device-cursor-and-manifest-reconciliation-design.md`

## Global Constraints

- Implement only Child 6. Conflict candidate retention/merge remains Child 8; multipart/resumable upload and files above the single-part limit remain Child 7.
- PostgreSQL is canonical for source identity, event order, current version, locator and tombstone state. Plugin SQLite is durable but rebuildable and cannot mint or guess a canonical source identity.
- One plugin `SyncCoordinator` owns all mutating foreground network phases. Watcher capture continues while a reconciliation barrier is active.
- The local cursor advances only with a durable terminal-safe local outcome. The server cursor advances only after the local cursor is durable, except for the exact manifest-completion transaction described by the spec.
- Remote content apply must retain verified old or new bytes across every crash point. Never use an unverified in-place overwrite.
- Echo suppression requires exact event, source, operation, locator operands and final fingerprint evidence. Never add a time-window wildcard.
- Remote delete must call `Vault.trash(file, false)`. There is no hard-delete fallback.
- Manifest runs bind barrier generation `G`, checkpoint sequence `C`, one policy revision, ordered pages, a canonical digest and a one-hour database-time expiry.
- Identity recovery after SQLite loss follows the approved proof priority: current active locator; unique historical locator plus exact current fingerprint; open tombstone retained locator plus exact retained fingerprint. Hash-only or ambiguous evidence never binds.
- Pull pages contain at most 200 events. Manifest pages contain at most 500 entries. One manifest contains at most 100,000 entries. Existing pending-event capacity remains 10,000.
- Cursor pull cadence is startup/resume/local commit/every 30 seconds while foreground-active. Full reconciliation is correctness-triggered and every six accumulated foreground-active hours. Retry backoff is cancellable jittered exponential from one second through five minutes.
- Every new closed failure path must surface its reason token immediately through a plugin trail/status/settings surface or a structured server diagnostic event. No task may land a new caught failure that is readable only in a test or swallowed until a later phase.
- The mandatory diagnostics-surface Task 7 must land before Tasks 8-12. Every later plugin task must assert the exact trail kind, stage and reason at each new catch site.
- `api_request_failed`, including exceptional 5xx responses, must retain the bound server-generated `request_id` and closed result code in the structured diagnostic line.
- The diagnostics trail changes to `obsidian_sync_diagnostics_trail/v2`; v1 remains readable and is losslessly rewritten. Composition read failures never become derived sync stop reasons, and exported tail order is newest-first.
- Do not change `personal_os.small_file_sync.metrics` or source-lifecycle write metrics in this child. Retain their two conditional BACKLOG rows.
- Domain code imports no FastAPI, SQLAlchemy, database driver, R2 SDK or Obsidian type. Mobile-loadable plugin modules import no Node.js, Electron or `FileSystemAdapter` at module load time.
- Raw content, locator/path, full digest, credential, object key, temporary name, provider detail, response body and exception text never enter logs, metrics, traces, settings exports, JUnit or handoffs.
- Add no production dependency. Python remains mypy-strict and TypeScript remains strict.
- Follow TDD in every task: observe the named failure before writing implementation; keep commits small and semantic.
- API changes update OpenAPI, the generated workspace client, the hand-mirrored plugin wire client, contract tests and docs together.
- Before local/live services, read `.local/RESTART.md` and use the repository bootstrap scripts. Use only disposable `knowledge-ci-*` projects; do not print secrets or create a tunnel.
- Desktop WDIO and the physical Mobile matrix are mandatory completion gates. Mock or unit evidence cannot substitute for either.

## Deliverable and File Map

```text
src/personal_os/device_sync/
├── __init__.py                 Closed public domain exports
├── contracts.py                Cursor, event, manifest and content values
├── errors.py                   Registered public errors and action reasons
├── metrics.py                  Low-cardinality device-sync metrics
├── ports.py                    Event, manifest, policy and content ports
├── planning.py                 Identity proof and deterministic actions
└── service.py                  Framework-neutral operation orchestration

packages/postgresql-source-store/src/postgresql_source_store/
├── tables.py                   Five Child 6 table metadata objects
├── device_event_store.py       Pull hydration and cursor acknowledgement
├── device_manifest_store.py    Run/page/action state and completion fence
├── device_content_catalog.py   Exact source-version/object descriptor lookup
└── backup_snapshot.py          Canonical backup manifest coverage

migrations/versions/
└── 20260826_01_add_device_sync_reconciliation.py

apps/api/src/api_runtime/
├── device_sync_models.py       Strict JSON wire models
├── device_sync_routes.py       Authenticated JSON and binary endpoints
├── device_sync_composition.py  PostgreSQL + policy + verified R2 graph
├── request_context.py          Failed-request correlation remediation
└── application.py              Route registration and OpenAPI models

apps/obsidian-plugin/src/device-sync/
├── contracts.ts                Hand-mirrored closed plugin contracts
├── diagnostics.ts              Trail facade for every Child 6 failure
├── schema.ts                   v6-to-v7 migration and table grammar
├── repository.ts               Local cursor/barrier/action/apply persistence
├── api.ts                      Authenticated JSON/binary client
├── echo-suppression.ts         Exact watcher marker matching
├── atomic-vault-writer.ts      Verified staging/replace/trash adapter
├── remote-event-applier.ts     Crash-safe incremental state machine
├── manifest-capture.ts         Stable ordered page enumeration
├── manifest-reconciler.ts      Action recheck/apply and barrier release
├── sync-coordinator.ts         Coalesced cadence and single mutating phase
└── status.ts                   Readable cursor/repair projection

apps/obsidian-plugin/test/specs/
└── device-sync-reconciliation.e2e.ts

tests/integration/device_sync/
├── conftest.py
├── test_device_sync_migration.py
├── test_cursor_and_event_transactions.py
├── test_cursor_and_manifest_transactions.py
├── test_device_sync_query_plans.py
├── test_two_device_reconciliation.py
└── test_verified_content_download.py

tests/contract/device_sync/
├── test_sensitive_device_sync_contract.py
└── test_reference_device_records.py

docs/operations/
└── device-cursor-manifest-reconciliation.md
```

### Task 1: Establish the Device Sync Domain, Error Registry and Server Diagnostic Contract

**Files:**

- Create: `src/personal_os/device_sync/__init__.py`
- Create: `src/personal_os/device_sync/contracts.py`
- Create: `src/personal_os/device_sync/errors.py`
- Create: `src/personal_os/device_sync/metrics.py`
- Create: `src/personal_os/device_sync/ports.py`
- Create: `src/personal_os/device_sync/service.py`
- Modify: `src/personal_os/error_contracts/codes.py`
- Modify: `src/personal_os/api_contracts/errors.py`
- Modify: `src/personal_os/diagnostics/events.py`
- Create: `tests/unit/device_sync/fakes.py`
- Create: `tests/unit/device_sync/test_contracts.py`
- Create: `tests/unit/device_sync/test_errors.py`
- Create: `tests/unit/device_sync/test_metrics.py`
- Create: `tests/unit/device_sync/test_service.py`
- Modify: `tests/unit/api_contracts/test_http_errors.py`
- Modify: `tests/unit/diagnostics/test_event_registry.py`
- Modify: `tests/unit/diagnostics/test_event_values.py`

**Interfaces:**

- Consumes: `DiagnosticContext`, `DiagnosticEventSink`, `NormalizedLocator`, `ContentDigest`, `CanonicalMediaType` and the central `ErrorCode`/HTTP envelope registry.
- Produces:

```python
class DeviceSyncOperation(StrEnum):
    PULL = "pull"
    ACKNOWLEDGE = "acknowledge"
    MANIFEST_START = "manifest_start"
    MANIFEST_PAGE = "manifest_page"
    MANIFEST_FINALIZE = "manifest_finalize"
    MANIFEST_ACTIONS = "manifest_actions"
    MANIFEST_COMPLETE = "manifest_complete"
    DOWNLOAD = "download"

class DeviceSyncOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    REPLAYED = "replayed"

class DeviceEventType(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    RENAMED = "renamed"
    MOVED = "moved"
    DELETED = "deleted"
    RESTORED = "restored"

class DeviceSyncErrorCode(StrEnum):
    CURSOR_GAP = "device_cursor_gap"
    CURSOR_REGRESSION = "device_cursor_regression"
    CURSOR_ACK_AHEAD = "device_cursor_ack_ahead"
    EVENT_UNAVAILABLE = "device_event_unavailable"
    EVENT_INTEGRITY_FAILED = "device_event_integrity_failed"
    MANIFEST_NOT_FOUND = "device_manifest_not_found"
    MANIFEST_EXPIRED = "device_manifest_expired"
    MANIFEST_STATE_INVALID = "device_manifest_state_invalid"
    MANIFEST_PAGE_INVALID = "device_manifest_page_invalid"
    MANIFEST_PAGE_REPLAY_MISMATCH = "device_manifest_page_replay_mismatch"
    MANIFEST_DIGEST_MISMATCH = "device_manifest_digest_mismatch"
    MANIFEST_POLICY_ADVANCED = "device_manifest_policy_advanced"
    DOWNLOAD_INTEGRITY_FAILED = "device_download_integrity_failed"
    DEPENDENCY_UNAVAILABLE = "device_sync_dependency_unavailable"

class ManifestRunState(StrEnum):
    COLLECTING = "collecting"
    PLANNED = "planned"
    APPLYING = "applying"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"

class ManifestActionKind(StrEnum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    APPLY_TOMBSTONE = "apply_tombstone"
    CONFLICT = "conflict"
    NO_CHANGE = "no_change"
    EXCLUDED = "excluded"

class ManifestActionReason(StrEnum):
    IDENTITY_AMBIGUOUS = "device_manifest_identity_ambiguous"
    LOCAL_DIVERGED = "device_manifest_local_diverged"
    TARGET_OCCUPIED = "device_manifest_target_occupied"
    ACTION_STALE = "device_manifest_action_stale"
    POLICY_EXCLUDED = "device_manifest_policy_excluded"

@dataclass(frozen=True, slots=True)
class DeviceSyncContext:
    workspace_id: UUID
    device_id: UUID
    user_id: UUID

@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    sha256: str
    size_bytes: int
    media_type: str

@dataclass(frozen=True, slots=True)
class DeviceSyncEvent:
    event_id: UUID
    event_sequence: int
    event_type: DeviceEventType
    source_id: UUID
    origin_device_id: UUID | None
    base_version_id: UUID | None
    current_version_id: UUID | None
    base_fingerprint: SourceFingerprint | None
    current_fingerprint: SourceFingerprint | None
    prior_locator: NormalizedLocator | None
    resulting_locator: NormalizedLocator | None
    tombstone_id: UUID | None
    committed_at: datetime

@dataclass(frozen=True, slots=True)
class DeviceCursorReceipt:
    acknowledged_sequence: int
    delivered_through_sequence: int

@dataclass(frozen=True, slots=True)
class DeviceEventPage:
    acknowledged_sequence: int
    page_checkpoint_sequence: int
    delivered_through_sequence: int
    events: tuple[DeviceSyncEvent, ...]
    has_more: bool

@dataclass(frozen=True, slots=True)
class DeviceContentDescriptor:
    source_id: UUID
    source_version_id: UUID
    content_digest: ContentDigest
    size_bytes: int
    media_type: CanonicalMediaType

    def expected_object(self) -> ExpectedObject: ...

@dataclass(frozen=True, slots=True)
class ManifestEntry:
    local_entry_id: str
    known_source_id: UUID | None
    known_version_id: UUID | None
    normalized_locator: NormalizedLocator
    fingerprint: SourceFingerprint
    observation_generation: int

@dataclass(frozen=True, slots=True)
class ManifestAction:
    action_index: int
    action_kind: ManifestActionKind
    local_entry_id: str | None
    source_id: UUID | None
    source_version_id: UUID | None
    source_locator_id: UUID | None
    source_tombstone_id: UUID | None
    reason: ManifestActionReason | None

class DeviceSyncMetrics(Protocol):
    def record_operation(
        self, *, operation: DeviceSyncOperation, outcome: DeviceSyncOutcome,
        reason: DeviceSyncErrorCode | None, duration_ms: int,
    ) -> None: ...

class DeviceEventStore(Protocol):
    async def pull_events(
        self, context: DeviceSyncContext, *, limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage: ...

    async def acknowledge_cursor(
        self, context: DeviceSyncContext, *, expected_previous_sequence: int,
        applied_through_sequence: int, diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt: ...

class DeviceManifestStore(Protocol):
    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt: ...
    async def append_manifest_page(self, command: AppendManifestPageCommand) -> ManifestPageReceipt: ...
    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt: ...
    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage: ...
    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt: ...

class DeviceSyncService:
    def __init__(
        self, *, events: DeviceEventStore, manifests: DeviceManifestStore,
        metrics: DeviceSyncMetrics, diagnostics: DiagnosticEventSink | None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None: ...
```

- [ ] **Step 1: Write failing closed-contract tests**

Pin all enum members, UUID/non-negative bounds, event-shape operands, page/action limits, one-hour expiry inputs and redacted `repr`. Include tests proving a create/update event cannot carry tombstone operands, delete/restore must carry their exact tombstone/locator shapes, and unknown enum strings fail before a store call.

```python
def test_delete_event_requires_prior_locator_and_tombstone() -> None:
    with pytest.raises(ValueError, match="delete event shape invalid"):
        DeviceSyncEvent(
            event_id=uuid7(), event_sequence=4, event_type=DeviceEventType.DELETED,
            source_id=uuid4(), origin_device_id=uuid4(), base_version_id=uuid4(),
            current_version_id=uuid4(), base_fingerprint=FINGERPRINT,
            current_fingerprint=None, prior_locator=None, resulting_locator=None,
            tombstone_id=None, committed_at=NOW,
        )
```

- [ ] **Step 2: Write failing registry, metric and diagnostic-surface tests**

Require the fourteen public errors from spec section 13, the five action-reason tokens, the mapping below, bounded metric labels, and structured events `device_sync_operation_completed`, `device_sync_operation_rejected` and `device_sync_operation_failed`. Rejected/failed events carry only `operation`, `reason` and `duration_ms`; successful events carry `operation` and `duration_ms`.

| Error code | HTTP | Retryable |
|---|---:|---|
| `device_cursor_gap` | 409 | no |
| `device_cursor_regression` | 409 | no |
| `device_cursor_ack_ahead` | 409 | no |
| `device_event_unavailable` | 404 | no |
| `device_event_integrity_failed` | 409 | no |
| `device_manifest_not_found` | 404 | no |
| `device_manifest_expired` | 410 | no |
| `device_manifest_state_invalid` | 409 | no |
| `device_manifest_page_invalid` | 422 | no |
| `device_manifest_page_replay_mismatch` | 409 | no |
| `device_manifest_digest_mismatch` | 422 | no |
| `device_manifest_policy_advanced` | 409 | no |
| `device_download_integrity_failed` | 422 | no |
| `device_sync_dependency_unavailable` | 503 | yes |

```python
@pytest.mark.asyncio
async def test_pull_gap_records_closed_reason_before_reraising() -> None:
    sink = RecordingEventSink()
    service = DeviceSyncService(events=GapStore(), manifests=UnusedManifestStore(), metrics=metrics, diagnostics=sink)
    with pytest.raises(DeviceSyncError) as raised:
        await service.pull_events(context=DEVICE, diagnostic_context=DIAGNOSTIC)
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_GAP
    fields = sink.last_fields()
    assert fields["operation"] is DeviceSyncOperation.PULL
    assert fields["reason"] is DeviceSyncErrorCode.CURSOR_GAP
    assert isinstance(fields["duration_ms"], int)
```

- [ ] **Step 3: Run the focused tests and confirm the missing package/registry failures**

```powershell
uv run pytest tests/unit/device_sync tests/unit/api_contracts/test_http_errors.py tests/unit/diagnostics/test_event_registry.py tests/unit/diagnostics/test_event_values.py -q
```

Expected: collection/import failures for `personal_os.device_sync` and missing error/event registry members; no environment failure.

- [ ] **Step 4: Implement immutable contracts, closed errors, metrics and service instrumentation**

Use one mapping from `DeviceSyncErrorCode` to central `ErrorCode`; service methods record the operation outcome and emit the closed reason before re-raising. Do not catch `CancelledError` and do not put identifiers into metric labels.

```python
async def pull_events(
    self, *, context: DeviceSyncContext, diagnostic_context: DiagnosticContext,
) -> DeviceEventPage:
    started = self._monotonic()
    try:
        page = await self._events.pull_events(
            context, limit=MAX_PULL_EVENTS, diagnostic_context=diagnostic_context
        )
    except DeviceSyncError as error:
        self._record_failure(DeviceSyncOperation.PULL, error.code, started, diagnostic_context)
        raise
    self._record_success(DeviceSyncOperation.PULL, started, diagnostic_context)
    return page
```

- [ ] **Step 5: Run strict focused gates**

```powershell
uv run pytest tests/unit/device_sync tests/unit/api_contracts/test_http_errors.py tests/unit/diagnostics/test_event_registry.py tests/unit/diagnostics/test_event_values.py -q
uv run poe python-lint
uv run poe python-type-check
```

Expected: all commands exit 0; coverage includes one assertion that every `DeviceSyncErrorCode` has one registry definition, HTTP mapping and structured diagnostic reason.

- [ ] **Step 6: Commit the domain contract**

```powershell
git add src/personal_os/device_sync src/personal_os/error_contracts/codes.py src/personal_os/api_contracts/errors.py src/personal_os/diagnostics/events.py tests/unit/device_sync tests/unit/api_contracts/test_http_errors.py tests/unit/diagnostics/test_event_registry.py tests/unit/diagnostics/test_event_values.py
git commit -m "feat: define device sync contracts"
```

### Task 2: Add Canonical Cursor and Manifest Schema

**Files:**

- Create: `migrations/versions/20260826_01_add_device_sync_reconciliation.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/tables.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/backup_snapshot.py`
- Modify: `src/personal_os/recovery/contracts.py`
- Create: `tests/unit/migrations/test_device_sync_migration.py`
- Create: `tests/integration/device_sync/conftest.py`
- Create: `tests/integration/device_sync/test_device_sync_migration.py`
- Modify: `tests/contract/source_publication/test_table_metadata.py`
- Modify: `tests/contract/test_canonical_postgresql_migration_contract.py`
- Modify: `tests/unit/postgresql_source_store/test_backup_snapshot.py`

**Interfaces:**

- Consumes: migration head `20260820_01`, existing `workspaces`, `devices`, `sync_events`, `sources`, `source_versions`, `source_locators` and `source_tombstones`.
- Produces: SQLAlchemy metadata objects `device_cursors`, `manifest_runs`, `manifest_pages`, `manifest_entry_resolutions`, `manifest_actions`.

```sql
create table knowledge.device_cursors (
  device_cursor_id uuid primary key,
  workspace_id uuid not null,
  device_id uuid not null,
  acknowledged_sequence bigint not null check (acknowledged_sequence >= 0),
  delivered_through_sequence bigint not null,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  constraint ck_device_cursors_delivery check (
    delivered_through_sequence >= acknowledged_sequence
  ),
  constraint uq_device_cursors_workspace_device unique (workspace_id, device_id)
);

create table knowledge.manifest_runs (
  manifest_run_id uuid primary key,
  workspace_id uuid not null,
  device_id uuid not null,
  base_acknowledged_sequence bigint not null,
  checkpoint_sequence bigint not null,
  policy_revision_number bigint not null,
  client_observation_generation bigint not null,
  state text not null,
  next_page_number integer not null default 0,
  entry_count integer not null default 0,
  final_digest varchar(64),
  safe_error_code varchar(100),
  created_at timestamptz not null,
  expires_at timestamptz not null,
  planned_at timestamptz,
  completed_at timestamptz
);
```

- [ ] **Step 1: Write failing migration/metadata/backup tests**

Assert the exact revision/down-revision, named checks/FKs/indexes, one unfinished manifest per device, run state vocabulary, page count `0..500`, run count `0..100000`, action-shape checks, restricting cursor FKs, temporary manifest cascades only, backup manifest coverage and guarded downgrade.

```python
def test_device_sync_revision_extends_source_lifecycle_head() -> None:
    module = load_revision("20260826_01_add_device_sync_reconciliation")
    assert module.revision == "20260826_01"
    assert module.down_revision == "20260820_01"
```

- [ ] **Step 2: Run RED migration tests**

```powershell
uv run pytest tests/unit/migrations/test_device_sync_migration.py tests/contract/source_publication/test_table_metadata.py tests/contract/test_canonical_postgresql_migration_contract.py tests/unit/postgresql_source_store/test_backup_snapshot.py -q
```

Expected: missing migration/tables/backup members only.

- [ ] **Step 3: Implement the migration and matching metadata**

Create all five tables in one forward revision. Use database time for run timestamps, partial uniqueness for `collecting|planned|applying`, stable `(manifest_run_id, page_number)` and `(manifest_run_id, action_index)` primary keys, and `on delete cascade` only from `manifest_runs` to its pages/resolutions/actions.

```python
op.create_index(
    "uq_manifest_runs_unfinished_device",
    "manifest_runs",
    ["workspace_id", "device_id"],
    unique=True,
    schema="knowledge",
    postgresql_where=sa.text("state in ('collecting', 'planned', 'applying')"),
)
```

- [ ] **Step 4: Prove live PostgreSQL upgrade/downgrade behavior**

Use the disposable integration fixture to upgrade from `20260820_01`, reject duplicate cursor/unfinished-run rows, enforce action shapes, downgrade to the prior head, then upgrade again.

```powershell
uv run pytest tests/integration/device_sync/test_device_sync_migration.py -m local_stack -q
```

Expected: PASS against a unique `knowledge-ci-*` project; no test targets `knowledge-local` data.

- [ ] **Step 5: Run schema gates**

```powershell
uv run pytest tests/unit/migrations/test_device_sync_migration.py tests/contract/source_publication/test_table_metadata.py tests/contract/test_canonical_postgresql_migration_contract.py tests/unit/postgresql_source_store/test_backup_snapshot.py -q
uv run poe database-heads
uv run poe python-lint
uv run poe python-type-check
```

- [ ] **Step 6: Commit the schema**

```powershell
git add migrations/versions/20260826_01_add_device_sync_reconciliation.py packages/postgresql-source-store/src/postgresql_source_store/tables.py packages/postgresql-source-store/src/postgresql_source_store/backup_snapshot.py src/personal_os/recovery/contracts.py tests/unit/migrations/test_device_sync_migration.py tests/integration/device_sync tests/contract/source_publication/test_table_metadata.py tests/contract/test_canonical_postgresql_migration_contract.py tests/unit/postgresql_source_store/test_backup_snapshot.py
git commit -m "feat: add device sync persistence schema"
```

### Task 3: Implement Event Pull Hydration and Monotonic Cursor Acknowledgement

**Files:**

- Create: `packages/postgresql-source-store/src/postgresql_source_store/device_event_store.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Create: `tests/unit/postgresql_source_store/test_device_event_store.py`
- Create: `tests/integration/device_sync/test_cursor_and_event_transactions.py`
- Create: `tests/integration/device_sync/test_device_sync_query_plans.py`

**Interfaces:**

- Consumes: `DeviceEventStore`, canonical lifecycle tables and credential-derived `DeviceSyncContext` from Task 1; schema from Task 2.
- Produces:

```python
class PostgresqlDeviceEventStore:
    async def pull_events(
        self, context: DeviceSyncContext, *, limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage: ...

    async def acknowledge_cursor(
        self, context: DeviceSyncContext, *, expected_previous_sequence: int,
        applied_through_sequence: int, diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt: ...

    async def minimum_acknowledged_sequence(self, workspace_id: UUID) -> int: ...
```

- [ ] **Step 1: Write failing statement and hydration tests**

Pin credential scope, `limit <= 200`, statement checkpoint behavior, the workspace compaction floor from the minimum active-device acknowledgement, and the exact operands for create/update/rename/move/delete/restore. Require a gap on a missing predecessor and an integrity error on an impossible shape; no query may skip an unhydratable event. Actual event-compaction execution remains the deferred retention owner from the spec.

```python
@pytest.mark.parametrize("event_type", tuple(DeviceEventType))
def test_hydrates_operation_shaped_event(event_type: DeviceEventType) -> None:
    hydrated = hydrate_device_event(row_for(event_type))
    assert hydrated.event_type is event_type
    assert_event_shape(hydrated)
```

- [ ] **Step 2: Write failing cursor race/replay tests**

Require fresh cursor zero, pull-only delivery watermark updates, expected-prior locking, regression rejection, ack-ahead rejection and exact acknowledgement replay.

```python
@pytest.mark.asyncio
async def test_ack_above_delivered_watermark_fails_closed(store: PostgresqlDeviceEventStore) -> None:
    await store.pull_events(DEVICE, limit=1, diagnostic_context=DIAGNOSTIC)
    with pytest.raises(DeviceSyncError) as raised:
        await store.acknowledge_cursor(
            DEVICE, expected_previous_sequence=0, applied_through_sequence=2,
            diagnostic_context=DIAGNOSTIC,
        )
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_ACK_AHEAD
```

- [ ] **Step 3: Run RED store tests**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_device_event_store.py tests/integration/device_sync/test_cursor_and_event_transactions.py -q
```

Expected: import/missing-adapter failures.

- [ ] **Step 4: Implement bounded hydration and cursor fencing**

The first page statement reads one `max(event_sequence)` checkpoint and pages only through it. Acknowledge locks the exact device cursor row and returns the frozen receipt on exact replay.

```python
if applied_through_sequence < row.acknowledged_sequence:
    raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_REGRESSION)
if applied_through_sequence > row.delivered_through_sequence:
    raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_ACK_AHEAD)
```

- [ ] **Step 5: Run transaction and query-plan gates**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_device_event_store.py -q
uv run pytest tests/integration/device_sync/test_cursor_and_event_transactions.py tests/integration/device_sync/test_device_sync_query_plans.py -m local_stack -q
uv run poe python-lint
uv run poe python-type-check
```

Expected: concurrent acks serialize without cursor regression; pull/action indexes avoid unbounded sequential scans at the pinned fixture size.

- [ ] **Step 6: Commit event pull and cursors**

```powershell
git add packages/postgresql-source-store/src/postgresql_source_store/device_event_store.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py tests/unit/postgresql_source_store/test_device_event_store.py tests/integration/device_sync/test_cursor_and_event_transactions.py tests/integration/device_sync/test_device_sync_query_plans.py
git commit -m "feat: pull device events with monotonic cursors"
```

### Task 4: Implement Deterministic Manifest Identity Proof, Planning and Completion

**Files:**

- Create: `src/personal_os/device_sync/planning.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/device_manifest_store.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Create: `tests/unit/device_sync/test_planning.py`
- Create: `tests/unit/postgresql_source_store/test_device_manifest_store.py`
- Create: `tests/integration/device_sync/test_cursor_and_manifest_transactions.py`
- Modify: `tests/integration/device_sync/test_device_sync_query_plans.py`

**Interfaces:**

- Consumes: Task 1 `DeviceManifestStore`, `ManifestEntryResolution`, `ManifestAction`, `ManifestActionReason`; Task 2 schema; active policy state and canonical lifecycle rows.
- Produces:

```python
class ManifestMatchKind(StrEnum):
    CURRENT_LOCATOR = "current_locator"
    HISTORICAL_LOCATOR_FINGERPRINT = "historical_locator_fingerprint"
    OPEN_TOMBSTONE_FINGERPRINT = "open_tombstone_fingerprint"
    UNPROVEN = "unproven"

@dataclass(frozen=True, slots=True)
class CanonicalManifestSource:
    source_id: UUID
    current_version_id: UUID
    current_fingerprint: SourceFingerprint
    locator: NormalizedLocator
    tombstone_id: UUID | None
    is_policy_allowed: bool

@dataclass(frozen=True, slots=True)
class ManifestIdentityEvidence:
    local_entry: ManifestEntry
    current_locator_candidates: tuple[CanonicalManifestSource, ...]
    historical_locator_candidates: tuple[CanonicalManifestSource, ...]
    open_tombstone_candidates: tuple[CanonicalManifestSource, ...]

@dataclass(frozen=True, slots=True)
class ManifestIdentityResolution:
    source_id: UUID | None
    source_version_id: UUID | None
    match_kind: ManifestMatchKind
    reason: ManifestActionReason | None

def resolve_manifest_identity(evidence: ManifestIdentityEvidence) -> ManifestIdentityResolution: ...

def plan_manifest_action(
    resolution: ManifestEntryResolution,
    canonical: CanonicalManifestSource | None,
) -> ManifestAction: ...

class PostgresqlDeviceManifestStore(DeviceManifestStore):
    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt: ...
    async def append_manifest_page(self, command: AppendManifestPageCommand) -> ManifestPageReceipt: ...
    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt: ...
    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage: ...
    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt: ...
```

- [ ] **Step 1: Write failing pure identity-proof and planner matrices**

Cover priority and ambiguity exactly: current locator can bind but divergent untrusted bytes conflict; historical locator requires one candidate plus exact current fingerprint; open tombstone requires retained locator plus retained fingerprint; hash-only/multiple/closed evidence is ambiguous. Cover all six action kinds and canonical-only missing-source actions.

```python
def test_hash_only_identity_never_binds() -> None:
    result = resolve_manifest_identity(
        ManifestIdentityEvidence(
            local_entry=UNKNOWN_ENTRY,
            current_locator_candidates=(),
            historical_locator_candidates=(),
            open_tombstone_candidates=(),
        )
    )
    assert result.match_kind is ManifestMatchKind.UNPROVEN
    assert result.reason is ManifestActionReason.IDENTITY_AMBIGUOUS
```

- [ ] **Step 2: Write failing page/replay/expiry/policy tests**

Require ordered pages from zero, exact digest/count replay, mismatched replay failure, cumulative 100,000 cap, canonical-JSON final digest, one unfinished run, one-hour database expiry, policy-revision invalidation and immutable action pagination. The first successful action-page read must transition `planned -> applying`; later reads preserve `applying` and return the same rows.

```python
@pytest.mark.asyncio
async def test_same_page_number_with_different_digest_fails_run(store: PostgresqlDeviceManifestStore) -> None:
    await store.append_manifest_page(PAGE_ZERO)
    with pytest.raises(DeviceSyncError) as raised:
        await store.append_manifest_page(replace(PAGE_ZERO, page_digest=OTHER_DIGEST))
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_REPLAY_MISMATCH
```

- [ ] **Step 3: Write failing completion/cursor race tests**

Prove only the same transaction changing exact `applying -> completed` may advance the cursor to `C` without delivered watermark; foreign/expired/failed/already-completed runs grant no new advance; lost response exact replay returns the same cursor.

- [ ] **Step 4: Run RED planner/store tests**

```powershell
uv run pytest tests/unit/device_sync/test_planning.py tests/unit/postgresql_source_store/test_device_manifest_store.py tests/integration/device_sync/test_cursor_and_manifest_transactions.py -q
```

Expected: missing planner/store behavior.

- [ ] **Step 5: Implement frozen planning and transactional completion**

Resolve locator evidence inside the workspace and checkpoint, persist only canonical IDs plus the internal locator-evidence digest, and materialize ordered immutable action rows. Recheck the active policy revision on first action read and completion.

```python
if active_policy_revision != run.policy_revision_number:
    await self._fail_run(run.manifest_run_id, DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED)
    raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED)
```

- [ ] **Step 6: Run deterministic and concurrent gates**

```powershell
uv run pytest tests/unit/device_sync/test_planning.py tests/unit/postgresql_source_store/test_device_manifest_store.py -q
uv run pytest tests/integration/device_sync/test_cursor_and_manifest_transactions.py tests/integration/device_sync/test_device_sync_query_plans.py -m local_stack -q
uv run poe python-lint
uv run poe python-type-check
```

- [ ] **Step 7: Commit manifest planning**

```powershell
git add src/personal_os/device_sync/planning.py packages/postgresql-source-store/src/postgresql_source_store/device_manifest_store.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py tests/unit/device_sync/test_planning.py tests/unit/postgresql_source_store/test_device_manifest_store.py tests/integration/device_sync/test_cursor_and_manifest_transactions.py tests/integration/device_sync/test_device_sync_query_plans.py
git commit -m "feat: reconcile checkpointed device manifests"
```

### Task 5: Add Exact-Version Verified Content Reading

**Files:**

- Create: `packages/postgresql-source-store/src/postgresql_source_store/device_content_catalog.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Create: `apps/api/src/api_runtime/device_sync_content.py`
- Create: `tests/unit/postgresql_source_store/test_device_content_catalog.py`
- Create: `tests/unit/api_runtime/test_device_sync_content.py`
- Create: `tests/integration/device_sync/test_verified_content_download.py`
- Modify: `tests/contract/object_storage/test_r2_adapter_contract.py`

**Interfaces:**

- Consumes: Task 1 `DeviceContentDescriptor`, `CanonicalObjectStore.open_verified_reader(ExpectedObject)`, credential-derived `DeviceSyncContext` and canonical source/version/object tables.
- Produces:

```python
class PostgresqlDeviceContentCatalog:
    async def resolve_descriptor(
        self, context: DeviceSyncContext, *, source_id: UUID,
        source_version_id: UUID, diagnostic_context: DiagnosticContext,
    ) -> DeviceContentDescriptor: ...

class VerifiedDeviceContentService:
    def open_content(
        self, context: DeviceSyncContext, *, source_id: UUID,
        source_version_id: UUID, diagnostic_context: DiagnosticContext,
    ) -> AbstractAsyncContextManager[VerifiedDeviceContent]: ...

@dataclass(frozen=True, slots=True)
class VerifiedDeviceContent:
    descriptor: DeviceContentDescriptor
    reader: VerifiedObjectReader
```

- [ ] **Step 1: Write failing scope, membership, policy and integrity tests**

Require exact source/version membership, workspace scope, current-policy authorization before bytes, missing/corrupt object mapping, and a descriptor that never exposes `object_key` or provider receipt.

```python
@pytest.mark.asyncio
async def test_cross_workspace_version_is_indistinguishable_from_missing(catalog) -> None:
    with pytest.raises(DeviceSyncError) as raised:
        await catalog.resolve_descriptor(
            DEVICE_A, source_id=SOURCE_B, source_version_id=VERSION_B,
            diagnostic_context=DIAGNOSTIC,
        )
    assert raised.value.code is DeviceSyncErrorCode.EVENT_UNAVAILABLE
```

- [ ] **Step 2: Run RED content tests**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_device_content_catalog.py tests/unit/api_runtime/test_device_sync_content.py tests/contract/object_storage/test_r2_adapter_contract.py -q
```

- [ ] **Step 3: Implement catalog plus verified reader composition**

The PostgreSQL catalog returns only the expected digest/size/media descriptor. The runtime service enters the existing fully verified spool-backed R2 reader before exposing an iterator; object absence/corruption maps to the closed device download integrity error and records the Task 1 diagnostic reason.

```python
@asynccontextmanager
async def open_content(...):
    descriptor = await self._catalog.resolve_descriptor(...)
    async with self._objects.open_verified_reader(descriptor.expected_object()) as reader:
        yield VerifiedDeviceContent(descriptor=descriptor, reader=reader)
```

- [ ] **Step 4: Run unit and live-adapter integration gates**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_device_content_catalog.py tests/unit/api_runtime/test_device_sync_content.py tests/contract/object_storage/test_r2_adapter_contract.py -q
uv run pytest tests/integration/device_sync/test_verified_content_download.py -m local_stack -q
uv run poe python-lint
uv run poe python-type-check
```

Expected: exact bytes are yielded only after full R2 verification; missing/corrupt fixtures return closed reason tokens without provider strings.

- [ ] **Step 5: Commit verified content reading**

```powershell
git add packages/postgresql-source-store/src/postgresql_source_store/device_content_catalog.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py apps/api/src/api_runtime/device_sync_content.py tests/unit/postgresql_source_store/test_device_content_catalog.py tests/unit/api_runtime/test_device_sync_content.py tests/integration/device_sync/test_verified_content_download.py tests/contract/object_storage/test_r2_adapter_contract.py
git commit -m "feat: read verified device source versions"
```

### Task 6: Expose Authenticated Device Sync APIs and Repair Failed-Request Correlation

**Files:**

- Create: `apps/api/src/api_runtime/device_sync_models.py`
- Create: `apps/api/src/api_runtime/device_sync_routes.py`
- Create: `apps/api/src/api_runtime/device_sync_composition.py`
- Modify: `apps/api/src/api_runtime/application.py`
- Modify: `apps/api/src/api_runtime/server.py`
- Modify: `apps/api/src/api_runtime/openapi_export.py`
- Modify: `apps/api/src/api_runtime/request_context.py`
- Modify: `src/personal_os/api_contracts/request_values.py`
- Modify: `src/personal_os/api_contracts/__init__.py`
- Modify: `src/personal_os/diagnostics/events.py`
- Modify: `tests/unit/api_runtime/test_request_context.py`
- Create: `tests/unit/api_runtime/test_device_sync_models.py`
- Create: `tests/unit/api_runtime/test_device_sync_routes.py`
- Create: `tests/unit/api_runtime/test_device_sync_composition.py`
- Create: `tests/contract/api/test_device_sync_routes.py`
- Create: `tests/contract/api/test_device_sync_openapi.py`
- Modify: `tests/contract/api/test_sensitive_http_contract.py`
- Modify: `tests/contract/api/test_openapi_schema.py`
- Modify: `packages/api-client/openapi.json`
- Modify: `packages/api-client/src/generated/schema.ts`

**Interfaces:**

- Consumes: Task 1 service/errors, Tasks 3-5 adapters, existing access-bearer `obsidian_sync` dependency and canonical envelope helpers.
- Produces these operation IDs:

```text
GET  /api/sync/events                                                   pullDeviceSyncEvents
POST /api/sync/cursor-acknowledgements                                 acknowledgeDeviceSyncCursor
POST /api/sync/manifests                                               startDeviceManifest
PUT  /api/sync/manifests/{manifest_run_id}/pages/{page_number}         appendDeviceManifestPage
POST /api/sync/manifests/{manifest_run_id}/finalize                    finalizeDeviceManifest
GET  /api/sync/manifests/{manifest_run_id}/actions                     listDeviceManifestActions
POST /api/sync/manifests/{manifest_run_id}/complete                    completeDeviceManifest
GET  /api/sources/{source_id}/versions/{source_version_id}/content     downloadDeviceSourceVersion
```

```python
@dataclass(frozen=True, slots=True)
class DeviceSyncRouteEndpoints:
    pull_events: Callable[..., Awaitable[JSONResponse]]
    acknowledge_cursor: Callable[..., Awaitable[JSONResponse]]
    start_manifest: Callable[..., Awaitable[JSONResponse]]
    append_manifest_page: Callable[..., Awaitable[JSONResponse]]
    finalize_manifest: Callable[..., Awaitable[JSONResponse]]
    list_manifest_actions: Callable[..., Awaitable[JSONResponse]]
    complete_manifest: Callable[..., Awaitable[JSONResponse]]
    download_source_version: Callable[..., Awaitable[StreamingResponse]]
```

- [ ] **Step 1: Write failing model, auth-scope and route tests**

Pin strict bodies, absent workspace/device/user fields, max page sizes, `Cache-Control: no-store`, canonical envelopes, semantic route templates and typed error mappings. Binary success must carry exact `Content-Length`, `Content-Type`, `X-Content-SHA256`, `X-Request-ID`; pre-stream failures remain JSON envelopes.

```python
@pytest.mark.asyncio
async def test_pull_derives_device_scope_and_caps_page(client, access_token) -> None:
    response = await client.get("/api/sync/events", headers=bearer(access_token))
    assert response.status_code == 200
    assert len(response.json()["data"]["events"]) <= 200
    assert response.headers["cache-control"] == "no-store"
```

- [ ] **Step 2: Write failing structured-diagnostics correlation tests**

Require 500 and 503 `api_request_failed` lines to carry the same bound request UUID as `X-Request-ID`/envelope plus `result_code="failed"`; assert raw path, body, locator and exception sentinels are absent. Keep completed/rejected behavior unchanged.

```python
@pytest.mark.asyncio
async def test_failed_access_observation_keeps_bound_request_id() -> None:
    sink = RecordingEventSink()
    response = await request(create_crashing_app(sink), "GET", "/api/health/ready")
    failed = sink.only(EventName.API_REQUEST_FAILED)
    assert failed.context.request_id == UUID(response.headers["x-request-id"])
    assert failed.result_code is ResultCode.FAILED
```

- [ ] **Step 3: Run RED API tests**

```powershell
uv run pytest tests/unit/api_runtime/test_device_sync_models.py tests/unit/api_runtime/test_device_sync_routes.py tests/unit/api_runtime/test_device_sync_composition.py tests/unit/api_runtime/test_request_context.py tests/contract/api/test_device_sync_routes.py tests/contract/api/test_device_sync_openapi.py tests/contract/api/test_sensitive_http_contract.py -q
```

- [ ] **Step 4: Implement routes, runtime composition and streaming cleanup**

Authenticate every route through the existing access bearer and derive `DeviceSyncContext` from the resolved token. For binary success, keep the verified-reader context open until the streaming generator closes; a mid-stream exception terminates transport and never attempts to emit JSON.

```python
async def verified_chunks(opened: AsyncContextManager[VerifiedDeviceContent]) -> AsyncIterator[bytes]:
    async with opened as content:
        async for chunk in content.reader:
            yield chunk
```

- [ ] **Step 5: Implement failed-request correlation and route vocabularies**

Bind the diagnostic context for the entire exchange, including exception-generated 5xx response start, and test `API_REQUEST_FAILED` with the closed route template rather than a raw path. Retire only the 2026-08-23 failed-request `request_id` row after final verification.

```python
with bind_diagnostic_context(resolution.context):
    try:
        await self._app(scope, receive, send_with_correlation_headers)
    finally:
        if status_code is not None:
            self._emit_access_observation(scope, status_code, started_ns)
```

- [ ] **Step 6: Export OpenAPI and regenerate the workspace client**

```powershell
uv run poe api-contract-export
pnpm --filter @workspace/api-client run generate
uv run poe api-contract-check
```

Expected: the committed snapshot and generated `components["schemas"]` contain every device-sync error/model and all eight operation IDs.

- [ ] **Step 7: Run API, strict and leakage gates**

```powershell
uv run pytest tests/unit/api_runtime tests/unit/device_sync tests/contract/api tests/contract/test_sensitive_diagnostics.py -q
uv run poe python-lint
uv run poe python-type-check
uv run poe api-contract-check
pnpm --filter @workspace/api-client run type-check
```

- [ ] **Step 8: Commit the public API**

```powershell
git add apps/api/src/api_runtime/device_sync_models.py apps/api/src/api_runtime/device_sync_routes.py apps/api/src/api_runtime/device_sync_composition.py apps/api/src/api_runtime/application.py apps/api/src/api_runtime/server.py apps/api/src/api_runtime/openapi_export.py apps/api/src/api_runtime/request_context.py src/personal_os/api_contracts/request_values.py src/personal_os/api_contracts/__init__.py src/personal_os/diagnostics/events.py tests/unit/api_runtime/test_request_context.py tests/unit/api_runtime/test_device_sync_models.py tests/unit/api_runtime/test_device_sync_routes.py tests/unit/api_runtime/test_device_sync_composition.py tests/contract/api/test_device_sync_routes.py tests/contract/api/test_device_sync_openapi.py tests/contract/api/test_sensitive_http_contract.py tests/contract/api/test_openapi_schema.py packages/api-client/openapi.json packages/api-client/src/generated/schema.ts
git commit -m "feat: expose device reconciliation api"
```

### Task 7: Upgrade the Plugin Diagnostics Trail and Land the Mandatory Diagnostics Surface

**Files:**

- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-export.ts`
- Modify: `apps/obsidian-plugin/src/journal/diagnostic-reporter.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-api.ts`
- Modify: `apps/obsidian-plugin/src/journal/queue-driver.ts`
- Modify: `apps/obsidian-plugin/src/journal/lifecycle-api.ts`
- Modify: `apps/obsidian-plugin/src/journal/lifecycle-driver.ts`
- Modify: `apps/obsidian-plugin/src/journal/status.ts`
- Modify: `apps/obsidian-plugin/src/plugin.ts`
- Create: `apps/obsidian-plugin/src/device-sync/contracts.ts`
- Create: `apps/obsidian-plugin/src/device-sync/contracts.test.ts`
- Create: `apps/obsidian-plugin/src/device-sync/diagnostics.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-export.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/queue-driver.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/lifecycle-api.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/lifecycle-driver.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/status.test.ts`
- Create: `apps/obsidian-plugin/src/device-sync/diagnostics.test.ts`
- Modify: `apps/obsidian-plugin/src/plugin.test.ts`

**Interfaces:**

- Consumes: existing fire-and-forget `SyncDiagnosticsTrail`, UUID-gated `envelopeRequestId`, Task 6 generated client/error registry and the current plugin sync error conventions.
- Produces:

```typescript
export const SYNC_DIAGNOSTICS_TRAIL_CONTRACT = "obsidian_sync_diagnostics_trail/v2";

export const DEVICE_SYNC_SERVER_REASONS = [
  "device_cursor_gap",
  "device_cursor_regression",
  "device_cursor_ack_ahead",
  "device_event_unavailable",
  "device_event_integrity_failed",
  "device_manifest_not_found",
  "device_manifest_expired",
  "device_manifest_state_invalid",
  "device_manifest_page_invalid",
  "device_manifest_page_replay_mismatch",
  "device_manifest_digest_mismatch",
  "device_manifest_policy_advanced",
  "device_download_integrity_failed",
  "device_sync_dependency_unavailable",
] as const satisfies readonly components["schemas"]["ErrorCode"][];

export const DEVICE_SYNC_ACTION_REASONS = [
  "device_manifest_identity_ambiguous",
  "device_manifest_local_diverged",
  "device_manifest_target_occupied",
  "device_manifest_action_stale",
  "device_manifest_policy_excluded",
] as const;

export const DEVICE_SYNC_TRANSPORT_REASONS = [
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "access_expired",
  "login_required",
] as const;

export const DEVICE_SYNC_LOCAL_REASONS = [
  "device_apply_trash_failed",
  "device_apply_vault_failed",
  "device_apply_recovery_ambiguous",
  "device_manifest_capture_failed",
] as const;

export type DeviceSyncReason =
  | (typeof DEVICE_SYNC_SERVER_REASONS)[number]
  | (typeof DEVICE_SYNC_ACTION_REASONS)[number]
  | (typeof DEVICE_SYNC_TRANSPORT_REASONS)[number]
  | (typeof DEVICE_SYNC_LOCAL_REASONS)[number]
  | JournalStoreErrorReason;

export type ApplyFailureStage =
  | "prepare" | "download" | "verify_temp" | "vault_mutation"
  | "verify_final" | "local_commit" | "recovery" | "trash";

export type ReconcileFailureStage =
  | "start" | "page" | "finalize" | "actions" | "complete";

export type SyncDiagnosticKind =
  | "wire_failure"
  | "pass_outcome"
  | "journal_failure"
  | "publish_failure"
  | "trail_reset"
  | "self_check"
  | "startup_failure"
  | "credential_failure"
  | "cursor_failure"
  | "apply_failure"
  | "reconcile_failure"
  | "composition_read_failure";

export interface DeviceSyncDiagnostics {
  cursorFailure(stage: "pull" | "acknowledge", reason: DeviceSyncReason, correlation?: DeviceSyncFailureCorrelation): void;
  applyFailure(stage: ApplyFailureStage, reason: DeviceSyncReason, correlation?: DeviceSyncFailureCorrelation): void;
  reconcileFailure(stage: ReconcileFailureStage, reason: DeviceSyncReason, correlation?: DeviceSyncFailureCorrelation): void;
  credentialFailure(stage: "access_missing" | "refresh_failed", reason: DeviceSyncReason): void;
}

export interface DeviceSyncFailureCorrelation {
  readonly requestId: string | null;
  readonly wireErrorCode: string | null;
}
```

- [ ] **Step 1: Write failing v1-to-v2 and closed-vocabulary tests**

Require v1 load and lossless rewrite, rejection of foreign kinds/stages/reasons, all new kinds/stages from spec section 14.1, UUID-only request correlation and the complete Task 6 device-sync error-code set. Define the four `DEVICE_SYNC_*_REASONS` arrays, `DeviceSyncReason`, `ApplyFailureStage`, `ReconcileFailureStage` and `DeviceSyncFailureCorrelation` in `contracts.ts` before the diagnostics facade consumes them.

```typescript
it("loads v1 and rewrites known entries as v2", async () => {
  const store = seededTrailStore(V1_DOCUMENT);
  const trail = createSyncDiagnosticsTrail({ fileStore: store });
  await trail.load();
  await trail.append({ kind: "cursor_failure", tokens: ["pull", "device_cursor_gap"] });
  expect(parsePersisted(store).contract).toBe("obsidian_sync_diagnostics_trail/v2");
  expect(trail.readEntries()[0]).toEqual(V1_FIRST_ENTRY);
});
```

- [ ] **Step 2: Write failing backlog-remediation tests**

Pin the four exact outcomes: `status_read_failed`/`note_status_read_failed` become `composition_read_failure` and never a stop reason; no-access/refresh-before-contact becomes `credential_failure`, not `wire_failure`; exported trail tail is newest-first by entry element; remove the dead bind from the trail test while preserving the assertion.

```typescript
it("does not derive composition reads as sync stop reasons", () => {
  expect(deriveSyncStopReasonTokens([
    entry("composition_read_failure", ["status_read", "status_read_failed"]),
  ])).toEqual([]);
});
```

- [ ] **Step 3: Write failing facade coverage for every Child 6 closed reason**

Use a table test over cursor/apply/reconcile/credential stage combinations. Require the primary reason plus optional UUID-gated request ID and registered server code, and require that a failed trail append cannot alter the sync outcome.

```typescript
it("surfaces the exact cursor stage and reason", async () => {
  diagnostics.cursorFailure("pull", "device_cursor_gap");
  await settleTrailPersist();
  expect(trail.readEntries().at(-1)).toMatchObject({
    kind: "cursor_failure",
    tokens: ["pull", "device_cursor_gap"],
  });
});
```

- [ ] **Step 4: Run RED diagnostics tests**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sync-diagnostics-trail.test.ts src/journal/sync-diagnostics-export.test.ts src/journal/queue-driver.test.ts src/journal/lifecycle-api.test.ts src/journal/lifecycle-driver.test.ts src/journal/status.test.ts src/device-sync/diagnostics.test.ts src/plugin.test.ts
```

Expected: failures identify the v1 contract, old `journal_failure` composition classification, wire/contact taxonomy and oldest-first tail.

- [ ] **Step 5: Implement trail v2, the diagnostics facade and residual hygiene**

Keep `append()` observe-only and never rejecting. Convert existing P5 call sites to `composition_read_failure`; distinguish credential absence before transport; render newest-first with `slice(-5).reverse()`; keep all token unions closed.

```typescript
const newestFirstTail = input.entries
  .slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT)
  .reverse();
```

- [ ] **Step 6: Run full plugin diagnostics gates**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
```

Expected: all exit 0. Do not remove the three triggered BACKLOG rows yet; retirement waits for Task 15 live evidence.

- [ ] **Step 7: Commit the mandatory diagnostics surface**

```powershell
git add apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts apps/obsidian-plugin/src/journal/sync-diagnostics-export.ts apps/obsidian-plugin/src/journal/diagnostic-reporter.ts apps/obsidian-plugin/src/journal/sync-api.ts apps/obsidian-plugin/src/journal/queue-driver.ts apps/obsidian-plugin/src/journal/lifecycle-api.ts apps/obsidian-plugin/src/journal/lifecycle-driver.ts apps/obsidian-plugin/src/journal/status.ts apps/obsidian-plugin/src/device-sync/contracts.ts apps/obsidian-plugin/src/device-sync/contracts.test.ts apps/obsidian-plugin/src/device-sync/diagnostics.ts apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts apps/obsidian-plugin/src/journal/sync-diagnostics-export.test.ts apps/obsidian-plugin/src/journal/queue-driver.test.ts apps/obsidian-plugin/src/journal/lifecycle-api.test.ts apps/obsidian-plugin/src/journal/lifecycle-driver.test.ts apps/obsidian-plugin/src/journal/status.test.ts apps/obsidian-plugin/src/device-sync/diagnostics.test.ts apps/obsidian-plugin/src/plugin.ts apps/obsidian-plugin/src/plugin.test.ts
git commit -m "feat: surface device sync failure reasons"
```

### Task 8: Upgrade the Portable Journal to Schema v7 and Persist Reconciliation State

**Files:**

- Modify: `apps/obsidian-plugin/src/device-sync/contracts.ts`
- Create: `apps/obsidian-plugin/src/device-sync/schema.ts`
- Create: `apps/obsidian-plugin/src/device-sync/repository.ts`
- Modify: `apps/obsidian-plugin/src/journal/contracts.ts`
- Modify: `apps/obsidian-plugin/src/journal/sqlite-database.ts`
- Modify: `apps/obsidian-plugin/src/journal/persistence.ts`
- Modify: `apps/obsidian-plugin/src/journal/repository.ts`
- Modify: `apps/obsidian-plugin/src/device-sync/contracts.test.ts`
- Create: `apps/obsidian-plugin/src/device-sync/schema.test.ts`
- Create: `apps/obsidian-plugin/src/device-sync/repository.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/sqlite-database.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/persistence.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/repository.test.ts`

**Interfaces:**

- Consumes: journal schema v6 and Task 7 `DeviceSyncDiagnostics`.
- Produces:

```typescript
export interface DeviceSyncState {
  readonly appliedSequence: number;
  readonly acknowledgedSequence: number;
  readonly observationGeneration: number;
  readonly barrierGeneration: number | null;
  readonly barrierReason: DeviceSyncReason | null;
  readonly activeManifestRunId: string | null;
  readonly manifestCheckpointSequence: number | null;
  readonly manifestFinalDigest: string | null;
}

export interface DeviceSyncRepository {
  readState(): DeviceSyncState;
  nextObservationGeneration(): Promise<number>;
  startRepairBarrier(input: RepairBarrierInput): Promise<void>;
  recordManifestPage(input: LocalManifestPageReceipt): Promise<void>;
  recordManifestAction(input: LocalManifestActionProgress): Promise<void>;
  prepareRemoteApply(input: PreparedRemoteApply): Promise<void>;
  transitionRemoteApply(input: RemoteApplyTransition): Promise<void>;
  terminalizeEvent(input: TerminalDeviceEvent): Promise<void>;
  recordServerAcknowledgement(sequence: number): Promise<void>;
  completeRepair(input: CompleteLocalRepair): Promise<void>;
  readUnfinishedApply(): RemoteApplyOperation | null;
  recordEchoMarker(input: EchoMarker): Promise<void>;
  readEchoMarker(eventSequence: number): EchoMarker | null;
  matchAndConsumeEcho(input: VaultObservation): Promise<boolean>;
}
```

```sql
create table device_sync_state (... singleton_key integer primary key check (singleton_key = 1) ...);
create table manifest_page_progress (... primary key (manifest_run_id, page_number) ...);
create table manifest_action_progress (... primary key (manifest_run_id, action_index) ...);
create table remote_apply_operations (... event_sequence integer primary key ...);
create table echo_markers (... event_sequence integer primary key ...);
```

- [ ] **Step 1: Write failing v6-to-v7 migration tests**

Require every v6 local file, journal event, attempt, lifecycle operand, tombstone and restore reservation to survive. New cursor values are zero, no barrier/apply/echo row exists, and a pre-existing `is_reconcile_required=1` remains set.

```typescript
it("migrates v6 to v7 without clearing reconcile_required", async () => {
  const v7 = migrateRestoreReservationJournalToDeviceSyncSchema(engine, V6_IMAGE);
  const db = SqliteDatabase.openFromImage(engine, v7);
  expect(db.schemaVersion).toBe(7);
  expect(readDeviceSyncState(db)).toMatchObject({ appliedSequence: 0, acknowledgedSequence: 0 });
  expect(db.readJournalMeta().isReconcileRequired).toBe(true);
});
```

- [ ] **Step 2: Write failing repository invariant tests**

Pin cursor monotonicity/contiguity, observation-generation increments, one active barrier, exact manifest page/action replay, legal remote-apply transitions, terminal event plus cursor in one serialized generation, acknowledgement debt, exact echo marker matching and local status reasons.

- [ ] **Step 3: Write failing persistence-loader migration tests**

Require `JournalPersistence` to accept a verified v6 generation/manifest, migrate the image in memory, persist a new verified v7 generation before exposing the repository and retain the v6 generation as the prior recovery image. Foreign/newer versions still fail closed.

- [ ] **Step 4: Run RED schema/repository tests**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/contracts.test.ts src/device-sync/schema.test.ts src/device-sync/repository.test.ts src/journal/sqlite-database.test.ts src/journal/persistence.test.ts src/journal/repository.test.ts
```

- [ ] **Step 5: Implement v7 DDL, migration and repository**

All mutations use `runSerializedMutation`. Repository invariant blockers persist a closed `barrierReason` readable through status. Ordinary sql.js/store errors propagate as their existing closed `JournalStoreErrorReason`; the repository never catches them merely to continue. The production v6-to-v7 migration/recovery catch records the existing `startup_failure/journal_recovery` surface before rebuilding or rethrowing. Tasks 10 and 11 add the operation-specific `apply_failure` or `reconcile_failure` entry at their own call sites.

```typescript
await this.#database.runSerializedMutation(async (session) => {
  assertContiguousAppliedSequence(session, input.eventSequence);
  writeTerminalOutcome(session, input);
  writeAppliedCursor(session, input.eventSequence);
});
```

- [ ] **Step 6: Run plugin persistence gates**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync src/journal/sqlite-database.test.ts src/journal/persistence.test.ts src/journal/repository.test.ts
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
```

- [ ] **Step 7: Commit journal v7**

```powershell
git add apps/obsidian-plugin/src/device-sync/contracts.ts apps/obsidian-plugin/src/device-sync/contracts.test.ts apps/obsidian-plugin/src/device-sync/schema.ts apps/obsidian-plugin/src/device-sync/schema.test.ts apps/obsidian-plugin/src/device-sync/repository.ts apps/obsidian-plugin/src/device-sync/repository.test.ts apps/obsidian-plugin/src/journal/contracts.ts apps/obsidian-plugin/src/journal/sqlite-database.ts apps/obsidian-plugin/src/journal/sqlite-database.test.ts apps/obsidian-plugin/src/journal/persistence.ts apps/obsidian-plugin/src/journal/persistence.test.ts apps/obsidian-plugin/src/journal/repository.ts apps/obsidian-plugin/src/journal/repository.test.ts
git commit -m "feat: persist device reconciliation journal state"
```

### Task 9: Add the Hand-Mirrored Device Sync Client and Binary Transport

**Files:**

- Create: `apps/obsidian-plugin/src/device-sync/api.ts`
- Create: `apps/obsidian-plugin/src/device-sync/api.test.ts`
- Modify: `apps/obsidian-plugin/src/api/request-url-transport.ts`
- Modify: `apps/obsidian-plugin/src/api/request-url-transport.test.ts`
- Modify: `apps/obsidian-plugin/src/api/obsidian-api-transport.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-api.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-wire-contract.test.ts`
- Modify: `tests/fixtures/small_file_sync/wire-golden.json`
- Modify: `tests/contract/small_file_sync/test_wire_contract.py`

**Interfaces:**

- Consumes: Task 6 generated client types and routes; Task 7 diagnostics; Task 8 plugin contracts.
- Produces:

```typescript
export interface DeviceSyncApi {
  pullEvents(): Promise<DeviceEventPage>;
  acknowledgeCursor(input: CursorAcknowledgementInput): Promise<DeviceCursorReceipt>;
  startManifest(input: StartManifestInput): Promise<ManifestRunReceipt>;
  appendManifestPage(input: AppendManifestPageInput): Promise<ManifestPageReceipt>;
  finalizeManifest(input: FinalizeManifestInput): Promise<ManifestRunReceipt>;
  listManifestActions(input: ManifestActionsInput): Promise<ManifestActionPage>;
  completeManifest(input: CompleteManifestInput): Promise<DeviceCursorReceipt>;
  downloadSourceVersion(input: DownloadSourceVersionInput): Promise<VerifiedDownload>;
}

export interface VerifiedDownload {
  readonly bytes: Uint8Array;
  readonly declaredSha256: string;
  readonly sizeBytes: number;
  readonly mediaType: string;
}

export interface DeviceSyncFailure {
  readonly reason: DeviceSyncReason;
  readonly retryable: boolean;
  readonly correlation: DeviceSyncFailureCorrelation | undefined;
}

export class DeviceSyncApiError extends Error implements DeviceSyncFailureCorrelation {
  readonly reason: DeviceSyncReason;
  readonly retryable: boolean;
  readonly requestId: string | null;
  readonly wireErrorCode: string | null;
}

export function classifyDeviceSyncFailure(error: unknown): DeviceSyncFailure;
```

- [ ] **Step 1: Write failing JSON and binary parsing tests**

Pin all server shapes, UUIDs, integer bounds, action unions, canonical envelope failures, response header names, byte length and SHA-256. A partial/truncated binary response must be `device_download_integrity_failed`, never success.

```typescript
it("rejects a truncated verified download", async () => {
  const api = createDeviceSyncApi({ transport: binaryResponse({ contentLength: 8, bytes: bytes(7) }), ...deps });
  await expect(api.downloadSourceVersion(DOWNLOAD)).rejects.toMatchObject({
    reason: "device_download_integrity_failed",
  });
});
```

- [ ] **Step 2: Write failing authentication and diagnostics tests**

No access token must avoid transport and append `credential_failure/access_missing`. A reached timeout/429/5xx must append the operation's cursor/reconcile/apply kind and reason. Parsed envelope failures append only UUID-gated request ID and registered code.

- [ ] **Step 3: Extend the shared wire corpus**

Add representative cursor gap, manifest policy advance and download integrity envelopes with exact Python and TypeScript expectations. Recompute the existing corpus digest through its repository test helper; do not hand-edit the digest.

- [ ] **Step 4: Run RED client/transport/corpus tests**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/api.test.ts src/api/request-url-transport.test.ts src/journal/sync-wire-contract.test.ts
uv run pytest tests/contract/small_file_sync/test_wire_contract.py -q
```

- [ ] **Step 5: Implement strict client and `requestUrl` binary transport**

Resolve origin/token afresh per request. Keep messages static; do not retain URL/status/body in thrown errors. Verify binary length/hash before returning the byte buffer.

```typescript
if (response.bodyBytes.byteLength !== declaredSize || await sha256Hex(response.bodyBytes) !== declaredSha256) {
  diagnostics.applyFailure("download", "device_download_integrity_failed");
  throw new DeviceSyncApiError("device_download_integrity_failed", false, requestId, wireCode);
}
```

- [ ] **Step 6: Run client and strict plugin gates**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/api.test.ts src/api/request-url-transport.test.ts src/journal/sync-wire-contract.test.ts
uv run pytest tests/contract/small_file_sync/test_wire_contract.py -q
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
```

- [ ] **Step 7: Commit the device sync client**

```powershell
git add apps/obsidian-plugin/src/device-sync/api.ts apps/obsidian-plugin/src/device-sync/api.test.ts apps/obsidian-plugin/src/api/request-url-transport.ts apps/obsidian-plugin/src/api/request-url-transport.test.ts apps/obsidian-plugin/src/api/obsidian-api-transport.ts apps/obsidian-plugin/src/journal/sync-api.ts apps/obsidian-plugin/src/journal/sync-wire-contract.test.ts tests/fixtures/small_file_sync/wire-golden.json tests/contract/small_file_sync/test_wire_contract.py
git commit -m "feat: add device sync wire client"
```

### Task 10: Implement Crash-Safe Remote Apply and Exact Echo Suppression

**Files:**

- Create: `apps/obsidian-plugin/src/device-sync/echo-suppression.ts`
- Create: `apps/obsidian-plugin/src/device-sync/echo-suppression.test.ts`
- Create: `apps/obsidian-plugin/src/device-sync/atomic-vault-writer.ts`
- Create: `apps/obsidian-plugin/src/device-sync/atomic-vault-writer.test.ts`
- Create: `apps/obsidian-plugin/src/device-sync/remote-event-applier.ts`
- Create: `apps/obsidian-plugin/src/device-sync/remote-event-applier.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/capture.ts`
- Modify: `apps/obsidian-plugin/src/journal/capture.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/lifecycle-capture.ts`
- Modify: `apps/obsidian-plugin/src/journal/lifecycle-capture.test.ts`

**Interfaces:**

- Consumes: Tasks 7-9 diagnostics/repository/API and existing journal/lifecycle mapping operations.
- Produces:

```typescript
export interface AtomicVaultWriter {
  stageAndReplace(input: ContentApplyInput): Promise<VerifiedVaultMutation>;
  renameOrMove(input: LocatorApplyInput): Promise<VerifiedVaultMutation>;
  trash(input: TombstoneApplyInput): Promise<VerifiedVaultMutation>;
  recover(input: RemoteApplyOperation): Promise<RemoteApplyRecovery>;
}

export interface RemoteEventApplier {
  recoverUnfinishedApply(): Promise<void>;
  apply(event: DeviceSyncEvent): Promise<TerminalDeviceEvent>;
}
```

- [ ] **Step 1: Write failing exact echo tests**

Require equality across event sequence, source, operation, applicable prior/target locators and final fingerprint. A mismatch remains a real watcher event. Restart snapshot proof may consume an exact marker; elapsed time may not.

```typescript
it("does not suppress the same path with different bytes", async () => {
  await repository.recordEchoMarker(EXPECTED_MARKER);
  expect(await suppressor.matchAndConsume({ ...OBSERVATION, fingerprint: OTHER })).toBe(false);
  expect(repository.readEchoMarker(EVENT_SEQUENCE)).not.toBeNull();
});
```

- [ ] **Step 2: Write failing crash-injection matrix tests**

Inject a crash after `prepared`, `temp_verified`, `vault_mutated`, `locally_applied` and before/after server acknowledgement for create/update/rename/move/delete/restore. Require exact-temp cleanup/resume, rollback restoration, preservation of ambiguous bytes and no cursor advancement on retryable failure.

- [ ] **Step 3: Write failing Vault safety tests**

Pin same-directory temporary siblings, final hash verification, occupied-target conflict, base-fingerprint conflict and `Vault.trash(file, false)`. Assert no permanent-delete method is called.

- [ ] **Step 4: Run RED apply tests**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/echo-suppression.test.ts src/device-sync/atomic-vault-writer.test.ts src/device-sync/remote-event-applier.test.ts src/journal/capture.test.ts src/journal/lifecycle-capture.test.ts
```

- [ ] **Step 5: Implement the durable state machine**

Persist `prepared` and the echo marker before Vault mutation. Content applies verify staging bytes, persist `temp_verified`, perform the narrow replace with rollback evidence, verify final bytes, persist `vault_mutated`, then terminalize local mapping/cursor in one journal generation.

```typescript
await repository.prepareRemoteApply(operation);
const mutation = await writer.stageAndReplace(contentInput);
await repository.transitionRemoteApply({ eventSequence, state: "vault_mutated", mutation });
await repository.terminalizeEvent(terminalOutcome);
```

- [ ] **Step 6: Surface every apply catch immediately**

At each catch, append one `apply_failure` entry with the exact stage and reason before returning conflict/repair or rethrowing. Tests must assert `prepare`, `download`, `verify_temp`, `vault_mutation`, `verify_final`, `local_commit`, `recovery` and `trash` at their own throw sites.

```typescript
try {
  await writer.trash(input);
} catch {
  diagnostics.applyFailure("trash", "device_apply_trash_failed");
  return { kind: "blocked", reason: "device_apply_trash_failed" };
}
```

- [ ] **Step 7: Run apply and mobile-boundary gates**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync src/journal/capture.test.ts src/journal/lifecycle-capture.test.ts
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
```

Expected: static tests prove no Node/Electron/`FileSystemAdapter` imports in `src/device-sync`.

- [ ] **Step 8: Commit remote apply**

```powershell
git add apps/obsidian-plugin/src/device-sync/echo-suppression.ts apps/obsidian-plugin/src/device-sync/echo-suppression.test.ts apps/obsidian-plugin/src/device-sync/atomic-vault-writer.ts apps/obsidian-plugin/src/device-sync/atomic-vault-writer.test.ts apps/obsidian-plugin/src/device-sync/remote-event-applier.ts apps/obsidian-plugin/src/device-sync/remote-event-applier.test.ts apps/obsidian-plugin/src/journal/capture.ts apps/obsidian-plugin/src/journal/capture.test.ts apps/obsidian-plugin/src/journal/lifecycle-capture.ts apps/obsidian-plugin/src/journal/lifecycle-capture.test.ts
git commit -m "feat: apply remote events crash safely"
```

### Task 11: Implement Manifest Capture, Barrier Semantics and Action Reconciliation

**Files:**

- Create: `apps/obsidian-plugin/src/device-sync/manifest-capture.ts`
- Create: `apps/obsidian-plugin/src/device-sync/manifest-capture.test.ts`
- Create: `apps/obsidian-plugin/src/device-sync/manifest-reconciler.ts`
- Create: `apps/obsidian-plugin/src/device-sync/manifest-reconciler.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/capture.ts`
- Modify: `apps/obsidian-plugin/src/journal/capture.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/repository.ts`
- Modify: `apps/obsidian-plugin/src/journal/repository.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/queue-driver.ts`
- Modify: `apps/obsidian-plugin/src/journal/queue-driver.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/automatic-snapshot.ts`
- Modify: `apps/obsidian-plugin/src/journal/automatic-snapshot.test.ts`

**Interfaces:**

- Consumes: existing canonical JSON/hash/fingerprint helpers, Tasks 8-10 repository/API/applier and the existing outbound queue/lifecycle drivers.
- Produces:

```typescript
export interface ManifestCapture {
  capturePages(barrierGeneration: number): AsyncIterable<ManifestEntryPage>;
}

export type ReconcileReason =
  | "onboarding"
  | "sqlite_rebuilt"
  | "cursor_gap"
  | "history_compacted"
  | "unknown_event"
  | "local_invariant"
  | "explicit_repair"
  | "periodic";

export interface ManifestReconciler {
  reconcile(reason: ReconcileReason): Promise<ManifestReconcileOutcome>;
  resume(): Promise<ManifestReconcileOutcome>;
}

export type ManifestReconcileOutcome =
  | { readonly kind: "completed"; readonly checkpointSequence: number }
  | { readonly kind: "retry"; readonly reason: DeviceSyncReason }
  | { readonly kind: "blocked"; readonly reason: DeviceSyncReason };
```

- [ ] **Step 1: Write failing ordered capture/digest tests**

Require normalized-locator order, settled fingerprint reads, 500-entry pages, canonical-JSON page/final digest, 100,000 total cap, opaque local IDs and no raw locator in diagnostics. A file changing during enumeration remains represented by a generation greater than `G`.

- [ ] **Step 2: Write failing action-recheck tests**

Before every action, recheck current path/fingerprint, occupied target, policy revision, restore reservation and newer local journal event. A stale action becomes a durable conflict/repair blocker without invalidating unrelated safe actions.

```typescript
it("preserves an edit observed after the barrier", async () => {
  await repository.startRepairBarrier({ generation: 14, reason: "device_cursor_gap" });
  await capture.recordObservation(EDIT_AFTER_BARRIER);
  fakeApi.queueManifestAction(STALE_DOWNLOAD);
  await reconciler.resume();
  expect(repository.readEvent(EDIT_AFTER_BARRIER.eventId)?.observationGeneration).toBe(15);
  expect(vault.writeCount).toBe(0);
});
```

- [ ] **Step 3: Write failing upload/barrier-release tests**

An upload action terminalizes when an outbound event is durably created or reauthorized under the barrier. After all actions are safe, exact server completion is recorded, local cursor becomes `C`, repair/barrier clears, and rows with generation `> G` plus planner uploads become dispatchable.

- [ ] **Step 4: Run RED reconciliation tests**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/manifest-capture.test.ts src/device-sync/manifest-reconciler.test.ts src/journal/capture.test.ts src/journal/repository.test.ts src/journal/queue-driver.test.ts src/journal/automatic-snapshot.test.ts
```

- [ ] **Step 5: Implement capture, action application and exact resume**

Persist server run/page/action progress after every accepted response. On one-hour expiry or policy advance, record the closed reason, retain all local edits, discard only the exact temporary run progress and start a new checkpoint-bound run.

```typescript
await api.completeManifest({ manifestRunId, finalDigest });
await repository.completeRepair({
  manifestRunId,
  checkpointSequence,
  barrierGeneration,
});
```

- [ ] **Step 6: Assert every reconcile catch uses the Task 7 surface**

Tests cover `start`, `page`, `finalize`, `actions`, `complete` with the exact reason token. No `catch { return retry; }` is permitted without the preceding diagnostics call or a persisted readable blocker.

```typescript
try {
  return await api.finalizeManifest(input);
} catch (error) {
  const failure = classifyDeviceSyncFailure(error);
  diagnostics.reconcileFailure("finalize", failure.reason, failure.correlation);
  return { kind: "retry", reason: failure.reason };
}
```

- [ ] **Step 7: Run reconciliation gates**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync src/journal/capture.test.ts src/journal/repository.test.ts src/journal/queue-driver.test.ts src/journal/automatic-snapshot.test.ts
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
```

- [ ] **Step 8: Commit manifest reconciliation**

```powershell
git add apps/obsidian-plugin/src/device-sync/manifest-capture.ts apps/obsidian-plugin/src/device-sync/manifest-capture.test.ts apps/obsidian-plugin/src/device-sync/manifest-reconciler.ts apps/obsidian-plugin/src/device-sync/manifest-reconciler.test.ts apps/obsidian-plugin/src/journal/capture.ts apps/obsidian-plugin/src/journal/capture.test.ts apps/obsidian-plugin/src/journal/repository.ts apps/obsidian-plugin/src/journal/repository.test.ts apps/obsidian-plugin/src/journal/queue-driver.ts apps/obsidian-plugin/src/journal/queue-driver.test.ts apps/obsidian-plugin/src/journal/automatic-snapshot.ts apps/obsidian-plugin/src/journal/automatic-snapshot.test.ts
git commit -m "feat: reconcile device manifests without losing edits"
```

### Task 12: Compose the Single Sync Coordinator, Cadence, Repair Command and Status

**Files:**

- Create: `apps/obsidian-plugin/src/device-sync/sync-coordinator.ts`
- Create: `apps/obsidian-plugin/src/device-sync/sync-coordinator.test.ts`
- Create: `apps/obsidian-plugin/src/device-sync/status.ts`
- Create: `apps/obsidian-plugin/src/device-sync/status.test.ts`
- Modify: `apps/obsidian-plugin/src/plugin.ts`
- Modify: `apps/obsidian-plugin/src/plugin.test.ts`
- Modify: `apps/obsidian-plugin/src/authentication/settings-tab.ts`
- Modify: `apps/obsidian-plugin/src/authentication/settings-tab.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/status.ts`
- Modify: `apps/obsidian-plugin/src/journal/status.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-export.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-export.test.ts`
- Modify: `apps/obsidian-plugin/src/live-acceptance-phase-status.test.ts`

**Interfaces:**

- Consumes: queue/lifecycle drivers, policy session and Tasks 7-11 plugin services.
- Produces:

```typescript
export interface SyncCoordinator {
  request(trigger: SyncTrigger): void;
  stop(): Promise<void>;
  readStatus(): DeviceSyncStatus;
}

export type SyncTrigger =
  | "startup"
  | "resume"
  | "local_commit"
  | "pull_interval"
  | "periodic_reconcile"
  | "explicit_repair";

export interface DeviceSyncStatus {
  readonly appliedSequence: number;
  readonly acknowledgedSequence: number;
  readonly cursorLag: number;
  readonly repairState: "ready" | "required" | "running" | "blocked";
  readonly reason: DeviceSyncReason | null;
  readonly pendingActionCount: number;
}
```

- [ ] **Step 1: Write failing single-phase and ordering tests**

Coalesce simultaneous triggers and prove no outbound/inbound/reconcile mutation overlaps. Each bounded cycle must run recovery, repair-if-required, eligible outbound drain, one inbound page, local ack and at most one follow-up request.

```typescript
it("serializes all mutating phases", async () => {
  coordinator.request("startup");
  coordinator.request("local_commit");
  coordinator.request("explicit_repair");
  await settleCoordinator();
  expect(phaseProbe.maximumConcurrentMutations).toBe(1);
});
```

- [ ] **Step 2: Write failing fake-clock cadence/backoff/suspend tests**

Pin foreground pull at 30 seconds, reconciliation after six accumulated active hours, startup/resume/local-commit triggers, jittered exponential retry `1s..5m`, coalescing, unload cancellation and one-hour manifest expiry after suspend.

- [ ] **Step 3: Write failing self-origin/cursor acknowledgement tests**

Origin device ID alone cannot suppress. Exact event/source/version/fingerprint evidence closes the matching outbound row before cursor advancement. Lost acknowledgement remains owed and is retried before another pull.

- [ ] **Step 4: Write failing settings/status/repair-command tests**

Add `Repair sync`; settings and Copy diagnostics show only closed state/reason, counts, cursor lag and five newest trail entries. A status/settings read failure emits `composition_read_failure`, not a stop reason.

- [ ] **Step 5: Run RED coordinator/composition tests**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/sync-coordinator.test.ts src/device-sync/status.test.ts src/plugin.test.ts src/authentication/settings-tab.test.ts src/journal/status.test.ts src/journal/sync-diagnostics-export.test.ts src/live-acceptance-phase-status.test.ts
```

- [ ] **Step 6: Implement coordinator and plugin composition**

Build adapters after the journal and diagnostic trail are ready; register Vault events/cadence/repair command; stop cleanly on unload. The coordinator catches only typed failures, records the exact diagnostics stage/reason and schedules retry or readable repair state.

```typescript
const coordinator = createSyncCoordinator({
  repository: deviceSyncRepository,
  api: deviceSyncApi,
  applier: remoteEventApplier,
  reconciler: manifestReconciler,
  outbound: boundedOutboundDriver,
  diagnostics: deviceSyncDiagnostics,
  nowEpochMs: () => Date.now(),
});
```

- [ ] **Step 7: Run full plugin gates**

```powershell
pnpm --dir apps/obsidian-plugin exec vitest run
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
```

- [ ] **Step 8: Commit coordinator and user surfaces**

```powershell
git add apps/obsidian-plugin/src/device-sync/sync-coordinator.ts apps/obsidian-plugin/src/device-sync/sync-coordinator.test.ts apps/obsidian-plugin/src/device-sync/status.ts apps/obsidian-plugin/src/device-sync/status.test.ts apps/obsidian-plugin/src/plugin.ts apps/obsidian-plugin/src/plugin.test.ts apps/obsidian-plugin/src/authentication/settings-tab.ts apps/obsidian-plugin/src/authentication/settings-tab.test.ts apps/obsidian-plugin/src/journal/status.ts apps/obsidian-plugin/src/journal/status.test.ts apps/obsidian-plugin/src/journal/sync-diagnostics-export.ts apps/obsidian-plugin/src/journal/sync-diagnostics-export.test.ts apps/obsidian-plugin/src/live-acceptance-phase-status.test.ts
git commit -m "feat: coordinate foreground device synchronization"
```

### Task 13: Prove Cross-Boundary Races, Two-Device Repair, Privacy and Performance

**Files:**

- Create: `tests/integration/device_sync/test_two_device_reconciliation.py`
- Modify: `tests/integration/device_sync/test_cursor_and_manifest_transactions.py`
- Modify: `tests/integration/device_sync/test_device_sync_query_plans.py`
- Modify: `tests/integration/device_sync/test_verified_content_download.py`
- Create: `tests/contract/device_sync/test_sensitive_device_sync_contract.py`
- Create: `tests/contract/device_sync/test_reference_device_records.py`
- Create: `apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts`
- Modify: `tests/contract/test_sensitive_diagnostics.py`
- Modify: `pyproject.toml`

**Interfaces:**

- Consumes: complete backend/plugin implementation from Tasks 1-12.
- Produces Poe tasks:

```toml
[tool.poe.tasks.device-sync-test]
cmd = "pytest tests/unit/device_sync tests/unit/postgresql_source_store tests/unit/api_runtime tests/contract/device_sync tests/contract/api tests/integration/device_sync -m 'not r2_live' -q"

[tool.poe.tasks.device-sync-device-verification]
cmd = "pytest tests/contract/device_sync/test_reference_device_records.py -m device_records -q"
```

- [ ] **Step 1: Write failing two-device and race journeys**

Cover remote edit/no-echo, lifecycle events, concurrent canonical commit after checkpoint, lost ack, cursor gap, policy advance, SQLite loss and edit-during-reconcile. Assert one canonical source after repair and all cursors/actions converge.

```python
@pytest.mark.asyncio
async def test_sqlite_loss_rebinds_without_duplicate_source(two_device_stack) -> None:
    source = await two_device_stack.device_a.publish(FILE)
    await two_device_stack.device_b.drop_local_journal()
    result = await two_device_stack.device_b.reconcile()
    assert result.source_ids == {source.source_id}
    assert await two_device_stack.count_sources_at(FILE.locator) == 1
```

- [ ] **Step 2: Write failing privacy and cardinality tests**

Inject unique sentinels into content, locator, path, digest, temp name, object key, credential, response body and provider exception. Scan logs, trails, settings export, JUnit and handoff-shaped result records. Require device-sync metric label products remain within the closed set.

- [ ] **Step 3: Write failing query-plan and bounded-load tests**

Use 10,000 events and 100,000 manifest entries to pin indexed cursor/action pagination and bounded response pages. Do not require all entries to become outbound rows.

- [ ] **Step 4: Run RED cross-boundary tests**

```powershell
uv run pytest tests/integration/device_sync tests/contract/device_sync tests/contract/test_sensitive_diagnostics.py -q
pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/device-sync-journey.test.ts
```

- [ ] **Step 5: Add only the missing test seams and deterministic fixtures**

Use production service/adapters for the journey; doubles may supply clocks, transport loss and Vault bytes but cannot bypass cursor, manifest, policy or identity proof. Every deliberately injected failure must assert its readable reason token.

- [ ] **Step 6: Run disposable-stack and live-R2 gates**

Follow `.local/RESTART.md` and the repository secret loader. Run R2 only with the existing dedicated live fixture and exact cleanup allowlist.

```powershell
uv run poe device-sync-test
uv run poe object-storage-test-live
pnpm --dir apps/obsidian-plugin exec vitest run
```

Expected: all exit 0; the R2 harness removes only exact run keys and emits no provider value.

- [ ] **Step 7: Run strict global checks for the touched surfaces**

```powershell
uv run poe format-check
uv run poe lint
uv run poe type-check
uv run poe boundary-check
uv run poe api-contract-check
pnpm --dir apps/obsidian-plugin run build
```

- [ ] **Step 8: Commit cross-boundary acceptance**

```powershell
git add tests/integration/device_sync tests/contract/device_sync tests/contract/test_sensitive_diagnostics.py apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts pyproject.toml
git commit -m "test: prove device reconciliation boundaries"
```

### Task 14: Update Canonical Contracts, Operations and Run the Offline Release Gate

**Files:**

- Create: `docs/operations/device-cursor-manifest-reconciliation.md`
- Modify: `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- Modify: `docs/07-POSTGRESQL_DATA_MODEL.md`
- Modify: `docs/12-API_MCP_AND_AGENT_INTEGRATION.md`
- Modify: `docs/15-OBSERVABILITY_AND_ALERTING.md`
- Modify: `docs/16-TESTING_AND_EVALUATION.md`
- Modify: `docs/20-IMPLEMENTATION_PLAN.md`
- Modify: `docs/operations/sync-error-tracing.md`
- Modify: `docs/operations/plugin-journal-small-file-sync.md`
- Modify: `apps/obsidian-plugin/manifest.json`
- Modify: `apps/obsidian-plugin/package.json`
- Modify: `apps/obsidian-plugin/README.md`
- Modify: `tests/contract/api/test_api_documentation.py`
- Modify: `tests/contract/device_sync/test_reference_device_records.py`

**Interfaces:**

- Consumes: implemented and tested contracts from Tasks 1-13.
- Produces: the living operator runbook, accurate canonical Child 5/6 status and one plugin release candidate carrying diagnostics trail v2.

- [ ] **Step 1: Write documentation contract checks before prose changes**

Extend the existing documentation/API/device-record tests to require route names, table names, cadence, reason vocabularies, local-trash semantics, mandatory live gates and an accurate Child 5 closed/Child 6 in-progress status.

```python
def test_device_sync_runbook_names_every_failure_surface() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for token in ("cursor_failure", "apply_failure", "reconcile_failure", "composition_read_failure"):
        assert token in text
```

- [ ] **Step 2: Run RED documentation/contract tests**

```powershell
uv run pytest tests/contract/device_sync tests/contract/api/test_api_documentation.py -q
```

- [ ] **Step 3: Update canonical and living documents**

Document cursor/repair status, exact repair command semantics, lost SQLite recovery, manifest expiry/resume, remote apply recovery, diagnostics readback, Desktop/Mobile procedures and operator interpretation. Correct the stale Child 5 status in `docs/20-IMPLEMENTATION_PLAN.md` using its passed Desktop and physical Mobile evidence.

- [ ] **Step 4: Bump the plugin release metadata once**

Bump `apps/obsidian-plugin/manifest.json` and `apps/obsidian-plugin/package.json` together from `0.1.0` to the Child 6 feature release `0.2.0`. Add the compatibility/recovery note to the plugin README: trail v1 is migrated to v2, journal v6 is migrated to v7, and losing SQLite starts manifest repair. Do not introduce a `versions.json` file because this repository has no such release artifact.

```json
{
  "version": "0.2.0"
}
```

- [ ] **Step 5: Run the full offline verification at one commit candidate**

```powershell
uv run poe verify
uv run poe api-contract-check
uv run poe device-sync-test
pnpm --dir apps/obsidian-plugin exec vitest run
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
git diff --check
```

Expected: every command exits 0. Record the exact counts and commit SHA for the final handoff; do not claim Child 6 complete yet.

- [ ] **Step 6: Commit canonical docs and release candidate**

```powershell
git add docs/04-OBSIDIAN_SYNC_AND_SOURCES.md docs/07-POSTGRESQL_DATA_MODEL.md docs/12-API_MCP_AND_AGENT_INTEGRATION.md docs/15-OBSERVABILITY_AND_ALERTING.md docs/16-TESTING_AND_EVALUATION.md docs/20-IMPLEMENTATION_PLAN.md docs/operations/device-cursor-manifest-reconciliation.md docs/operations/sync-error-tracing.md docs/operations/plugin-journal-small-file-sync.md apps/obsidian-plugin/manifest.json apps/obsidian-plugin/package.json apps/obsidian-plugin/README.md tests/contract/api/test_api_documentation.py tests/contract/device_sync/test_reference_device_records.py
git commit -m "docs: publish device reconciliation operations"
```

### Task 15: Run Mandatory Desktop and Mobile Gates, Retire Triggered Backlog Rows and Write One Handoff

**Files:**

- Create: `apps/obsidian-plugin/test/specs/device-sync-reconciliation.e2e.ts`
- Modify: `apps/obsidian-plugin/wdio.conf.mts`
- Modify: `apps/obsidian-plugin/test/support/live-acceptance-phase-status.ts`
- Modify: `docs/operations/device-cursor-manifest-reconciliation.md`
- Modify: `tests/contract/device_sync/test_reference_device_records.py`
- Modify: `docs/handoff/BACKLOG.md`
- Create: `docs/handoff/2026-08-26-device-cursor-and-manifest-reconciliation.md`

**Interfaces:**

- Consumes: the release candidate and offline evidence from Task 14; `.local/RESTART.md`, `.local/serve-local.sh`, `.local/run-worker.sh`, `.local/e2e-totp-code.py`, `.local/publish-policy-revision.py` and `tools/obsidian_live_acceptance_bootstrap.py`.
- Produces: sanitized Desktop/Mobile evidence, final device-record contract, exactly one handoff and evidence-backed retirement of only the triggered backlog rows.

- [ ] **Step 1: Write the failing Desktop WDIO journey**

The journey must prove remote edit plus exact no-echo, cursor gap to repair, SQLite loss without duplicate source and remote tombstone to Obsidian local trash. It must fail if the source count, cursor, trail reason or trash evidence is absent.

```typescript
it("repairs a lost journal without duplicating canonical identity", async () => {
  await seedCanonicalSource();
  await removePluginJournalGenerations();
  await restartPluginAndRunRepair();
  await expect($(syncStatusSelector)).toHaveText(expect.stringContaining("Ready"));
  expect(await canonicalSourceCountForFixture()).toBe(1);
});
```

- [ ] **Step 2: Run the guarded Desktop live gate**

Read `.local/RESTART.md` first. Check `uv run poe stack-status`; stand down `knowledge-local` only as the runbook directs; bootstrap one exact disposable `knowledge-ci-*` project; enroll TOTP through the approved Web flow if preflight says no active credential; publish policy; use the existing redacted Cloudflare tunnel; then launch WDIO.

```powershell
uv run python tools/obsidian_live_acceptance_bootstrap.py --help
pnpm --dir apps/obsidian-plugin exec wdio run wdio.conf.mts --spec test/specs/device-sync-reconciliation.e2e.ts
```

Expected final token: the repository's closed `obsidian_live_acceptance_passed` verdict and all four Child 6 scenarios PASS. If not, Child 6 remains BLOCKED and the handoff records the exact closed prerequisite/failure token.

- [ ] **Step 3: Run the physical Mobile matrix with the operator**

On the physical reference device, prove manifest suspend/resume, remote apply/no-echo, SQLite-loss repair, tombstone-to-local-trash and edit-during-reconciliation preservation. Record only sanitized closed evidence in the living runbook/device record.

```powershell
uv run poe device-sync-device-verification
```

Expected: PASS only after the physical record contains all five scenarios. Absence or partial evidence is BLOCKED, never inferred from Desktop.

- [ ] **Step 4: Retire exactly the triggered BACKLOG rows**

Delete these rows only after corresponding green evidence is cited in the handoff:

```text
2026-08-23 observability: failed API request_id
2026-08-24 sync-error-tracing: P5 read tokens in Stop reasons
2026-08-24 sync-error-tracing: residual trail hygiene group
```

Retain exactly once, unchanged, the `_validate_epoch_ms` small-file-sync metrics row and missing `record_commit(COMMITTED)` source-lifecycle metrics row because their triggers were not reached. Do not create duplicate deferred rows.

- [ ] **Step 5: Write exactly one final handoff**

Create `docs/handoff/2026-08-26-device-cursor-and-manifest-reconciliation.md` with final commit SHA, every offline/live gate and exact evidence, design interpretations, backlog verdicts and next actions. Link living runbooks rather than copying them. Keep it below approximately 400 lines.

- [ ] **Step 6: Run final evidence checks at the handoff commit candidate**

```powershell
uv run poe verify
uv run poe api-contract-check
uv run poe device-sync-test
uv run poe device-sync-device-verification
pnpm --dir apps/obsidian-plugin exec vitest run
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
git diff --check
git status --short
```

Expected: all commands exit 0 and `git status --short` lists only the intended handoff/backlog/evidence files before commit.

- [ ] **Step 7: Commit final acceptance and verify clean state**

```powershell
git add apps/obsidian-plugin/test/specs/device-sync-reconciliation.e2e.ts apps/obsidian-plugin/wdio.conf.mts apps/obsidian-plugin/test/support/live-acceptance-phase-status.ts docs/operations/device-cursor-manifest-reconciliation.md tests/contract/device_sync/test_reference_device_records.py docs/handoff/BACKLOG.md docs/handoff/2026-08-26-device-cursor-and-manifest-reconciliation.md
git commit -m "docs: close device reconciliation acceptance"
git status --short
git rev-parse HEAD
```

Expected: clean working tree and the printed SHA matches the handoff's final commit accounting. If the handoff necessarily records its own commit predecessor, state that convention explicitly rather than inventing a future SHA.

## Spec Coverage Matrix

| Spec requirement | Implementing task(s) |
|---|---|
| Dedicated framework-neutral `device_sync` domain | 1 |
| Closed errors, low-cardinality metrics and server reason surfaces | 1, 6, 13 |
| PostgreSQL cursor/manifest schema and backup coverage | 2 |
| Event pull, hydration, checkpoint and monotonic acknowledgement | 3 |
| Identity proof, deterministic actions, expiry/policy and completion cursor exception | 4 |
| Exact verified binary content | 5, 6, 9 |
| Auth-scoped APIs, OpenAPI and generated client | 6 |
| Failed API request `request_id` | 6, 15 |
| Trail v2, P5 classification and residual diagnostics hygiene | 7, 15 |
| SQLite v7 cursor/barrier/manifest/apply/echo state | 8 |
| Hand-mirrored plugin JSON/binary client | 9 |
| Crash-safe apply, local trash and exact no-echo | 10 |
| Barrier `G`, checkpoint `C`, action rechecks and lost-SQLite repair | 11 |
| Single coordinator, cadence, retry, suspend and status/repair command | 12 |
| Two-device races, privacy, query plans and live R2 | 13 |
| Canonical docs, operations and plugin release | 14 |
| Mandatory Desktop WDIO, physical Mobile, backlog and handoff | 15 |

## Execution Discipline

1. Execute tasks in order. Task 7 is a hard prerequisite for every new plugin catch path in Tasks 8-12.
2. At the beginning of implementation, create an isolated worktree with `superpowers:using-git-worktrees`; do not reuse a dirty workspace.
3. For each task, run the named RED command and read the failure before implementation.
4. Use the smallest implementation that satisfies the task and the approved spec. Do not begin Child 7 or Child 8 work.
5. Before each commit, run the focused green command, strict checks proportional to the files changed, `git diff --check` and `git status --short`.
6. Never swallow a caught failure. The same task must prove its closed reason is readable through structured server diagnostics, plugin trail, settings/status or a persisted action blocker.
7. Do not retire a BACKLOG row from unit evidence when its ruling requires live evidence.
8. Do not claim Child 6 complete until both Desktop WDIO and the physical Mobile matrix pass at the final implementation commit.
