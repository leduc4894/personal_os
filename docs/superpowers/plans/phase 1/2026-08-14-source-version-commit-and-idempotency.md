# Source-Version Commit and Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Phase 1 create/update publication boundary so verified R2 objects become immutable PostgreSQL source versions with deterministic replay, optimistic concurrency, atomic audit/outbox writes and crash-safe Temporal dispatch.

**Architecture:** Keep commands, fingerprinting, results, ports and orchestration provider-neutral in `personal_os.sources`. Put SQLAlchemy Core, Psycopg, advisory locks and projection-intent persistence in the isolated `postgresql-source-store` workspace package. Keep Temporal SDK use in the worker composition root. R2 verification precedes the transaction, while Temporal starts consume committed outbox rows afterward; neither network call occurs inside a PostgreSQL transaction.

**Tech Stack:** Python 3.14.6, uv 0.11.32, PostgreSQL 18.4, SQLAlchemy 2.0.51 async Core, psycopg 3.3.4, Temporal Server 1.31.2, Temporal Python SDK 1.30.0, pytest 9.1.1, pytest-asyncio 1.4.0, Ruff 0.15.22 and mypy 2.3.0 strict.

## Global Constraints

- Implement `docs/superpowers/specs/source-version-commit-and-idempotency-design.md` at commit `2ba4ff1`; do not expand it with rename, move, delete, restore, public API routes or projection execution.
- PostgreSQL remains the canonical state; R2 contains immutable canonical bytes; Qdrant and Neo4j are represented only by durable projection intents.
- Accept publication only from an internal `VerifiedObjectReceipt` matching the expected digest, canonical key, size and media type and verified no more than five minutes earlier.
- Run idempotency preflight before R2. Exact committed replay must not read or verify R2 and must not create another event, audit row or intent.
- Recheck idempotency under a transaction-scoped advisory lock before any canonical mutation.
- Acquire transaction advisory locks in the fixed order idempotency then source; session advisory locks are prohibited.
- Compare update base version before comparing content. A stale base conflicts even if proposed bytes equal the current object.
- One PostgreSQL transaction owns source/version/pointer/event/intents/success-audit atomicity. It contains no R2 or Temporal call.
- A no-change update writes only one sync event and one success audit, with equal base and committed version IDs; it does not update the source row.
- Every changed create/update writes exactly one Qdrant and one Neo4j upsert intent. Both map to one deterministic event-scoped Temporal workflow.
- Never log or serialize raw content, title, locator/path, idempotency key, request fingerprint, full content hash, object key, receipt, SQL/parameters/DSN, password data, Temporal payload or provider exception text.
- Add no migration: revision `20260813_01`, the nine-table baseline and the Alembic head must remain unchanged.
- Use TDD for every behavior task. Run the named failing test before implementation, then the focused green test, then the broader affected suite before each commit.
- Preserve unrelated user changes and use exact paths for every cleanup action.

---

## File Structure

### Provider-neutral source application layer

```text
src/personal_os/sources/
├── __init__.py                 Public source publication exports
├── actors.py                   Trusted actor value and vocabulary
├── commands.py                 Create/update commands and validated values
├── errors.py                   Source and projection typed errors
├── fingerprint.py              Canonical request and safe-diff hashing
├── metrics.py                  Closed low-cardinality metric contract
├── ports.py                    Publication/outbox/clock/random protocols
├── publication.py              Preflight, object verification and commit orchestration
├── projection_dispatch.py      Provider-neutral lease dispatch state machine
└── results.py                  Publication and workflow-start outcomes
```

### PostgreSQL infrastructure package

```text
packages/postgresql-source-store/
├── pyproject.toml
└── src/postgresql_source_store/
    ├── __init__.py
    ├── engine.py               Async engine lifecycle and transaction bounds
    ├── error_mapping.py        Psycopg/SQLAlchemy classification without leakage
    ├── locks.py                Stable advisory lock derivation
    ├── projection_intents.py   Claim, reclaim and fenced state transitions
    ├── publication_store.py    Preflight and atomic publication implementation
    ├── settings.py             Database settings and secret-file loading
    ├── tables.py               Schema-qualified SQLAlchemy Core table metadata
    └── py.typed
```

### Worker composition and tests

```text
apps/worker/src/workflow_worker/
├── projection_dispatch_runtime.py
└── projection_workflow_starter.py

tests/unit/sources/
tests/unit/postgresql_source_store/
tests/unit/workflow_worker/
tests/contract/source_publication/
tests/integration/source_publication/
tests/integration/projection_dispatch/
```

The migration remains the DDL authority. `tables.py` is the typed DML representation and must be contract-tested against the migrated catalog.

---

### Task 1: Add the PostgreSQL adapter package, Temporal SDK and architecture boundaries

**Files:**
- Create: `packages/postgresql-source-store/pyproject.toml`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/py.typed`
- Modify: `apps/worker/pyproject.toml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.importlinter`
- Modify: `src/personal_os/runtime_configuration/environment_names.py`
- Modify: `tests/contract/test_dependency_pins.py`
- Modify: `tests/contract/test_architecture_boundaries.py`
- Modify: `tests/contract/test_python_workspace.py`
- Modify: `tests/unit/runtime_configuration/test_environment_names.py`

**Interfaces:**
- Consumes: `knowledge-core==0.1.0`, SQLAlchemy and Psycopg pins already used by the root project.
- Produces: installable `postgresql-source-store==0.1.0`, worker access to `temporalio==1.30.0`, registered Temporal environment names and enforced import boundaries.

- [ ] **Step 1: Write failing dependency, workspace and boundary tests**

Add exact assertions:

```python
def test_postgresql_source_store_has_exact_dependencies() -> None:
    manifest = tomllib.loads(
        (REPO_ROOT / "packages/postgresql-source-store/pyproject.toml").read_text("utf-8")
    )
    assert manifest["project"]["dependencies"] == [
        "knowledge-core==0.1.0",
        "psycopg[binary]==3.3.4",
        "SQLAlchemy==2.0.51",
    ]


def test_worker_has_exact_publication_dependencies() -> None:
    manifest = tomllib.loads((REPO_ROOT / "apps/worker/pyproject.toml").read_text("utf-8"))
    assert manifest["project"]["dependencies"] == [
        "knowledge-core==0.1.0",
        "postgresql-source-store==0.1.0",
        "temporalio==1.30.0",
    ]
```

Extend the source-root scanner with `packages/postgresql-source-store/src`. Assert that core forbids `postgresql_source_store`; the adapter forbids all composition roots, R2, Temporal, Qdrant, Neo4j and Redis; and only `workflow_worker` may import `temporalio`.

Assert the closed environment registry includes exactly:

```python
TEMPORAL_ENVIRONMENT_NAMES = frozenset(
    {
        "KNOWLEDGE_TEMPORAL_TARGET",
        "KNOWLEDGE_TEMPORAL_NAMESPACE",
        "KNOWLEDGE_TEMPORAL_TASK_QUEUE",
    }
)
```

- [ ] **Step 2: Run the focused contracts and observe the missing package**

```powershell
uv run pytest tests/contract/test_dependency_pins.py tests/contract/test_architecture_boundaries.py tests/contract/test_python_workspace.py tests/unit/runtime_configuration/test_environment_names.py -q
```

Expected: FAIL because the package, worker pins, import contract and Temporal name registry do not exist.

- [ ] **Step 3: Create the package and update all quality-tool paths**

Use this manifest:

```toml
[project]
name = "postgresql-source-store"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = [
  "knowledge-core==0.1.0",
  "psycopg[binary]==3.3.4",
  "SQLAlchemy==2.0.51",
]

[build-system]
requires = ["uv_build==0.11.32"]
build-backend = "uv_build"
```

Add the member to uv workspace/source declarations, Ruff, mypy, coverage, Poe formatting/type-check paths and `.importlinter`. Add the adapter and Temporal pins to the worker. Export no unfinished symbols from either package initializer.

- [ ] **Step 4: Lock and prove the workspace**

```powershell
uv lock
uv sync --all-packages --frozen
uv run pytest tests/contract/test_dependency_pins.py tests/contract/test_architecture_boundaries.py tests/contract/test_python_workspace.py tests/unit/runtime_configuration/test_environment_names.py -q
uv run lint-imports
uv run mypy src apps/worker/src packages/postgresql-source-store/src
```

Expected: every command exits `0`; `uv.lock` contains Temporal SDK 1.30.0 and the new workspace distribution.

- [ ] **Step 5: Commit the package boundary**

```powershell
git add pyproject.toml uv.lock .importlinter apps/worker/pyproject.toml packages/postgresql-source-store src/personal_os/runtime_configuration/environment_names.py tests/contract/test_dependency_pins.py tests/contract/test_architecture_boundaries.py tests/contract/test_python_workspace.py tests/unit/runtime_configuration/test_environment_names.py
git commit -m "build: add source publication infrastructure packages"
```

---

### Task 2: Define immutable actors, commands and publication results

**Files:**
- Create: `src/personal_os/sources/__init__.py`
- Create: `src/personal_os/sources/actors.py`
- Create: `src/personal_os/sources/commands.py`
- Create: `src/personal_os/sources/results.py`
- Create: `tests/unit/sources/test_actor_and_commands.py`
- Create: `tests/unit/sources/test_publication_results.py`

**Interfaces:**
- Consumes: `ExpectedObject`, `ContentDigest` and canonical media type validation from `personal_os.object_storage`.
- Produces: `SourceActor`, `CreateSourceVersion`, `UpdateSourceVersion`, `IdempotencyKey`, `SourceTitle`, `SourceType`, `PublicationOutcome` and `SourceVersionPublicationResult`.

- [ ] **Step 1: Write validation tests for every closed value**

Cover all seven source types, user/device/system actor invariants, nil UUID rejection, printable non-whitespace ASCII idempotency keys of length 1–200, exact-trimmed titles of 1–500 Unicode code points, control characters, naïve timestamps and UTC normalization. Pin the update rule that no source type/title field exists.

```python
def test_device_actor_requires_non_nil_actor_id() -> None:
    with pytest.raises(ValueError, match="actor_id"):
        SourceActor(actor_kind=ActorKind.DEVICE, actor_id=None)


def test_update_exposes_no_title_or_source_type() -> None:
    fields = {field.name for field in dataclasses.fields(UpdateSourceVersion)}
    assert "title" not in fields
    assert "source_type" not in fields
```

- [ ] **Step 2: Run the unit tests and observe missing source contracts**

```powershell
uv run pytest tests/unit/sources/test_actor_and_commands.py tests/unit/sources/test_publication_results.py -q
```

Expected: FAIL during collection because `personal_os.sources` does not exist.

- [ ] **Step 3: Implement the immutable contracts**

Use frozen, slotted dataclasses and closed enums. The key public shapes are:

```python
class ActorKind(StrEnum):
    USER = "user"
    DEVICE = "device"
    SYSTEM = "system"


class SourceType(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"
    PDF = "pdf"
    IMAGE = "image"
    AUDIO = "audio"
    WEB = "web"
    YOUTUBE = "youtube"


@dataclass(frozen=True, slots=True)
class UpdateSourceVersion:
    workspace_id: UUID
    source_id: UUID
    event_id: UUID
    idempotency_key: IdempotencyKey
    base_version_id: UUID
    actor: SourceActor
    expected_object: ExpectedObject
    client_timestamp: datetime | None
```

Normalize aware timestamps to UTC in construction and reject every nil UUID. `SourceVersionPublicationResult` must require positive `content_version` and `event_sequence`, aware UTC `committed_at`, and one of `published | no_change`.

- [ ] **Step 4: Run focused tests, strict typing and core architecture checks**

```powershell
uv run pytest tests/unit/sources/test_actor_and_commands.py tests/unit/sources/test_publication_results.py -q
uv run mypy src/personal_os/sources
uv run pytest tests/contract/test_architecture_boundaries.py -q
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit the domain contracts**

```powershell
git add src/personal_os/sources tests/unit/sources/test_actor_and_commands.py tests/unit/sources/test_publication_results.py
git commit -m "feat: define source publication contracts"
```

---

### Task 3: Canonicalize request fingerprints and safe diff hashes

**Files:**
- Create: `src/personal_os/sources/fingerprint.py`
- Create: `tests/unit/sources/test_source_fingerprint.py`
- Create: `tests/unit/sources/test_safe_diff_hash.py`
- Create: `tests/fixtures/source_publication/fingerprint_golden.json`

**Interfaces:**
- Consumes: the validated command types from Task 2 and `ContentDigest` from object storage.
- Produces: `RequestFingerprint`, `SafeDiffHash`, `compute_request_fingerprint()` and `compute_safe_diff_hash()`.

- [ ] **Step 1: Add golden-byte and exclusion tests**

Pin sorted UTF-8 JSON with `ensure_ascii=False`, compact separators and explicit null members. The create fixture must hash to:

```text
a7d604a619ccd19ac638debb20ca3ef11df106b2c64bb273a27400ee1f9c2888
```

for title `Ghi chú`; the equivalent update fixture must hash to:

```text
2cd7344d34cac9cd342cd32b9256da2141bb191893c0b793ba877166d7211cba
```

Pin the safe diff fixture to:

```text
530aebe6bdbef4057ee45b9bfa5ec6a0c7b30a0dad7133b15e925279800b1fee
```

Test that idempotency key, request/trace IDs, receipt fields and generated values cannot change the request fingerprint because they are not parameters to the function.

- [ ] **Step 2: Run the fingerprint tests and observe missing functions**

```powershell
uv run pytest tests/unit/sources/test_source_fingerprint.py tests/unit/sources/test_safe_diff_hash.py -q
```

Expected: FAIL because `personal_os.sources.fingerprint` does not exist.

- [ ] **Step 3: Implement one private canonical JSON encoder**

```python
def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def compute_request_fingerprint(command: SourceVersionCommand) -> RequestFingerprint:
    envelope = _request_envelope(command)
    return RequestFingerprint.parse(hashlib.sha256(_canonical_json_bytes(envelope)).hexdigest())
```

Serialize UUIDs canonically, timestamps as UTC RFC 3339 with six fractional digits and `Z`, and keep nulls. Construct and discard dictionaries locally; expose neither raw envelope nor bytes.

- [ ] **Step 4: Verify deterministic hashing and typing**

```powershell
uv run pytest tests/unit/sources/test_source_fingerprint.py tests/unit/sources/test_safe_diff_hash.py -q
uv run mypy src/personal_os/sources/fingerprint.py
uv run ruff check src/personal_os/sources/fingerprint.py tests/unit/sources/test_source_fingerprint.py tests/unit/sources/test_safe_diff_hash.py
```

Expected: all commands exit `0`, including the Unicode golden fixture on Windows and Ubuntu.

- [ ] **Step 5: Commit canonical hashing**

```powershell
git add src/personal_os/sources/fingerprint.py tests/unit/sources/test_source_fingerprint.py tests/unit/sources/test_safe_diff_hash.py tests/fixtures/source_publication/fingerprint_golden.json
git commit -m "feat: add canonical source request fingerprints"
```

---

### Task 4: Extend closed errors, diagnostics and low-cardinality metrics

**Files:**
- Create: `src/personal_os/sources/errors.py`
- Create: `src/personal_os/sources/metrics.py`
- Modify: `src/personal_os/error_contracts/codes.py`
- Modify: `src/personal_os/error_contracts/exceptions.py`
- Modify: `src/personal_os/error_contracts/__init__.py`
- Modify: `src/personal_os/diagnostics/events.py`
- Create: `tests/unit/sources/test_source_error_contract.py`
- Create: `tests/unit/sources/test_source_metrics.py`
- Modify: `tests/unit/diagnostics/test_event_registry.py`
- Create: `tests/contract/source_publication/test_telemetry_leakage.py`

**Interfaces:**
- Consumes: existing `ApplicationError`, safe diagnostic scalar types and diagnostic event registry.
- Produces: all 14 spec error codes, six event definitions and a metric recorder whose labels are closed enums only.

- [ ] **Step 1: Write completeness, safe-detail and sentinel tests**

Assert the exact error-code set from spec section 13, fixed category/retryability, and allowed detail fields. Reject arbitrary strings as safe details. Assert exact metric names/label dimensions and prove UUIDs, keys and digests cannot be labels.

```python
SOURCE_ERROR_CODES = {
    "source_publish_input_invalid",
    "source_not_found",
    "source_already_exists",
    "source_state_invalid",
    "source_version_conflict",
    "source_idempotency_mismatch",
    "source_event_identity_mismatch",
    "source_verified_receipt_stale",
    "source_content_object_conflict",
    "source_concurrency_busy",
    "source_concurrency_invariant_failed",
    "source_commit_outcome_unknown",
    "projection_dispatch_unavailable",
    "projection_intent_contract_invalid",
}
```

Use unique sentinels for title, key, fingerprint, SQL, password and provider exception text; assert absence from `str(error)`, `repr(error)`, safe serialization and captured logs.

- [ ] **Step 2: Run the tests and observe incomplete registries**

```powershell
uv run pytest tests/unit/sources/test_source_error_contract.py tests/unit/sources/test_source_metrics.py tests/unit/diagnostics/test_event_registry.py tests/contract/source_publication/test_telemetry_leakage.py -q
```

Expected: FAIL because the new codes, events and metric vocabulary are absent.

- [ ] **Step 3: Add closed definitions and typed subclasses**

Add `SourcePublicationError` and `ProjectionDispatchError` with non-overlapping allowed code sets. Add event definitions for publish success/replay/rejection and projection dispatch success/failure/reclaim. Define enums for publication operation/outcome/reason, projection kind/outcome and metric error code; use them in method signatures rather than raw strings.

```text
SourcePublicationMetrics
  record_publication(
    operation: PublicationOperation,
    outcome: PublicationMetricOutcome,
    duration_seconds: float,
  ) -> None
  record_transaction_retry(reason_code: TransactionRetryReason) -> None
```

Implement these as typed Protocol methods in the same form as the repository's existing metric contracts; production recorders must reject negative/non-finite values.

- [ ] **Step 4: Run focused tests and all existing error/diagnostic tests**

```powershell
uv run pytest tests/unit/sources/test_source_error_contract.py tests/unit/sources/test_source_metrics.py tests/unit/diagnostics tests/unit/error_contracts tests/contract/source_publication/test_telemetry_leakage.py -q
uv run mypy src/personal_os/sources src/personal_os/error_contracts src/personal_os/diagnostics
```

Expected: all commands exit `0`; registry completeness guards still pass.

- [ ] **Step 5: Commit observability contracts**

```powershell
git add src/personal_os/sources/errors.py src/personal_os/sources/metrics.py src/personal_os/error_contracts src/personal_os/diagnostics/events.py tests/unit/sources/test_source_error_contract.py tests/unit/sources/test_source_metrics.py tests/unit/diagnostics/test_event_registry.py tests/contract/source_publication/test_telemetry_leakage.py
git commit -m "feat: register source publication diagnostics"
```

---

### Task 5: Build the provider-neutral publication service and ports

**Files:**
- Create: `src/personal_os/sources/ports.py`
- Create: `src/personal_os/sources/publication.py`
- Modify: `src/personal_os/sources/__init__.py`
- Create: `tests/unit/sources/fakes.py`
- Create: `tests/unit/sources/test_publication_service.py`
- Create: `tests/unit/sources/test_verified_receipt_boundary.py`

**Interfaces:**
- Consumes: command/fingerprint/result contracts, `CanonicalObjectStore`, `VerifiedObjectReceipt`, `DiagnosticContext` and source metrics.
- Produces: `SourcePublicationStore` port and `SourceVersionPublicationService.publish_create()` / `publish_update()` orchestration.

- [ ] **Step 1: Test the orchestration order with narrow fakes**

Prove these call sequences:

```text
exact replay: validate -> fingerprint -> resolve_committed -> return
new commit:   validate -> fingerprint -> resolve_committed -> store/verify -> validate receipt -> commit
```

Assert exact replay makes zero object-store calls. Assert mismatch stops before R2. Assert stale/future/mismatched receipt prevents the commit. Assert a bounded database retry can reuse one valid receipt, while a fresh service invocation must obtain another receipt unless preflight hits.

- [ ] **Step 2: Run service tests and observe missing ports/service**

```powershell
uv run pytest tests/unit/sources/test_publication_service.py tests/unit/sources/test_verified_receipt_boundary.py -q
```

Expected: FAIL because publication ports and service do not exist.

- [ ] **Step 3: Implement provider-neutral protocols and service**

```text
SourcePublicationStore
  resolve_committed(
    command: SourceVersionCommand,
    request_fingerprint: RequestFingerprint,
    diagnostic_context: DiagnosticContext,
  ) -> SourceVersionPublicationResult | None
  commit_create(
    command: CreateSourceVersion,
    request_fingerprint: RequestFingerprint,
    receipt: VerifiedObjectReceipt,
    diagnostic_context: DiagnosticContext,
  ) -> SourceVersionPublicationResult
  commit_update(
    command: UpdateSourceVersion,
    request_fingerprint: RequestFingerprint,
    receipt: VerifiedObjectReceipt,
    diagnostic_context: DiagnosticContext,
  ) -> SourceVersionPublicationResult
```

The service receives a caller-owned async byte stream for a miss. It calls `resolve_verified_object(expected)` before `store_stream(stream, expected_size_bytes, media_type, claimed_sha256)` so deduplicated canonical bytes avoid upload, then validates the returned receipt. Use an injected aware UTC clock for the five-minute age rule. Do not accept a receipt as a public method argument.

- [ ] **Step 4: Verify service behavior and architecture isolation**

```powershell
uv run pytest tests/unit/sources/test_publication_service.py tests/unit/sources/test_verified_receipt_boundary.py -q
uv run mypy src/personal_os/sources
uv run pytest tests/contract/test_architecture_boundaries.py -q
```

Expected: all commands exit `0`; the core imports no SQLAlchemy, Psycopg, R2 package or Temporal SDK.

- [ ] **Step 5: Commit the orchestration boundary**

```powershell
git add src/personal_os/sources tests/unit/sources/fakes.py tests/unit/sources/test_publication_service.py tests/unit/sources/test_verified_receipt_boundary.py
git commit -m "feat: orchestrate verified source publication"
```

---

### Task 6: Add database settings, async engine, table metadata and advisory locks

**Files:**
- Create: `packages/postgresql-source-store/src/postgresql_source_store/settings.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/engine.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/tables.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/locks.py`
- Create: `tests/unit/postgresql_source_store/test_settings.py`
- Create: `tests/unit/postgresql_source_store/test_engine.py`
- Create: `tests/unit/postgresql_source_store/test_locks.py`
- Create: `tests/contract/source_publication/test_table_metadata.py`

**Interfaces:**
- Consumes: the approved database environment names, secret-file reader and baseline migration schema.
- Produces: frozen `DatabaseRuntimeSettings`, composition-owned `AsyncEngine`, nine schema-qualified Core tables and stable transaction-lock functions.

- [ ] **Step 1: Write settings, engine and lock tests**

Assert secret-file-only password loading; reject `DATABASE_URL`, plaintext password and unknown `KNOWLEDGE_*` keys. Pin pool size 4, overflow 4, pool timeout 5 seconds, connect timeout 5 seconds and local transaction timeouts 5/15/30 seconds.

Pin namespaces and signed derivation:

```python
assert IDEMPOTENCY_LOCK_NAMESPACE == 0x53564349
assert SOURCE_LOCK_NAMESPACE == 0x53564353
assert source_lock_key(UUID("018f47a0-7b00-7000-8000-000000000002")) == -1788951247
```

Compute the expected integer in the test from the frozen SHA-256 algorithm once, then keep the literal as a compatibility guard. Scan production SQL for `pg_advisory_xact_lock` and reject `pg_advisory_lock`.

- [ ] **Step 2: Run tests and observe missing infrastructure modules**

```powershell
uv run pytest tests/unit/postgresql_source_store tests/contract/source_publication/test_table_metadata.py -q
```

Expected: FAIL during import because the modules do not exist.

- [ ] **Step 3: Implement settings, engine and exact DML metadata**

Use `postgresql+psycopg`, `pool_pre_ping=True`, explicit lifecycle disposal and bound connection kwargs. Define the nine migrated tables in schema `knowledge` with exact column names and types needed for reads/writes; add no `create_all()` path.

```python
def _signed_first_sha256_word(material: bytes) -> int:
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big", signed=True)


def idempotency_lock_key(workspace_id: UUID, key: IdempotencyKey) -> int:
    material = workspace_id.bytes + b"\x00" + key.value.encode("ascii")
    return _signed_first_sha256_word(material)
```

Use a `SET LOCAL` helper immediately after each `READ COMMITTED` begin. Keep connection/session creation out of module import.

- [ ] **Step 4: Run unit/static checks and compare DML metadata with migration source**

```powershell
uv run pytest tests/unit/postgresql_source_store tests/contract/source_publication/test_table_metadata.py -q
uv run mypy packages/postgresql-source-store/src
uv run ruff check packages/postgresql-source-store/src tests/unit/postgresql_source_store tests/contract/source_publication/test_table_metadata.py
uv run alembic heads
```

Expected: all commands exit `0`; Alembic still reports only `20260813_01`.

- [ ] **Step 5: Commit bounded PostgreSQL infrastructure**

```powershell
git add packages/postgresql-source-store/src/postgresql_source_store/settings.py packages/postgresql-source-store/src/postgresql_source_store/engine.py packages/postgresql-source-store/src/postgresql_source_store/tables.py packages/postgresql-source-store/src/postgresql_source_store/locks.py tests/unit/postgresql_source_store tests/contract/source_publication/test_table_metadata.py
git commit -m "feat: add bounded source database runtime"
```

---

### Task 7: Implement authorization-aware idempotency preflight and replay hydration

**Files:**
- Create: `packages/postgresql-source-store/src/postgresql_source_store/error_mapping.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/publication_store.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Create: `tests/unit/postgresql_source_store/test_error_mapping.py`
- Create: `tests/unit/postgresql_source_store/test_replay_hydration.py`
- Create: `tests/integration/source_publication/conftest.py`
- Create: `tests/integration/source_publication/test_idempotency_preflight.py`

**Interfaces:**
- Consumes: Task 5 `SourcePublicationStore`, Task 6 engine/tables and migrated user/workspace/device/event/version/object rows.
- Produces: `PostgresqlSourcePublicationStore.resolve_committed()` with actor revalidation, tenant-safe identity checks and exact result hydration.

- [ ] **Step 1: Write preflight cases against PostgreSQL 18.4**

Cover exact key/event/fingerprint replay, key mismatch, same event under another key, cross-workspace global event collision, invalid/revoked actor and impossible event shapes. Assert mismatch writes one standalone rejection audit only after a trusted workspace/actor is established. Assert cross-tenant failure returns only requested source/event IDs and never existing tenant data.

- [ ] **Step 2: Run focused tests against a unique disposable stack**

```powershell
$env:CI = "true"
$env:LOCAL_STACK_TEST_PROJECT = "knowledge-ci-81407-1"
$env:POSTGRES_PORT = "15442"
uv run pytest tests/unit/postgresql_source_store/test_error_mapping.py tests/unit/postgresql_source_store/test_replay_hydration.py -q
uv run pytest tests/integration/source_publication/test_idempotency_preflight.py -m local_stack -q
```

Expected: FAIL because preflight/hydration are not implemented. The fixture must reset only `knowledge-ci-81407-1` in `finally`.

- [ ] **Step 3: Implement indexed preflight and safe mapping**

Lookup order is workspace actor → `(workspace_id, idempotency_key)` → global `event_id`. Hydrate by joining event, committed version and content object; classify event shape exactly:

```python
def classify_replay(event_type: str, base_version_id: UUID | None, committed_id: UUID | None) -> PublicationOutcome:
    if event_type == "create" and base_version_id is None and committed_id is not None:
        return PublicationOutcome.PUBLISHED
    if event_type == "update" and base_version_id is not None and committed_id == base_version_id:
        return PublicationOutcome.NO_CHANGE
    if event_type == "update" and base_version_id is not None and committed_id is not None:
        return PublicationOutcome.PUBLISHED
    raise SourcePublicationError(ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED)
```

Map SQLSTATE/driver failures without propagating raw messages. Every SQL statement is schema-qualified and parameter-bound.

- [ ] **Step 4: Re-run unit/integration tests and leakage checks**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_error_mapping.py tests/unit/postgresql_source_store/test_replay_hydration.py -q
uv run pytest tests/integration/source_publication/test_idempotency_preflight.py -m local_stack -q
uv run pytest tests/contract/source_publication/test_telemetry_leakage.py -q
uv run mypy packages/postgresql-source-store/src
```

Expected: all commands exit `0`; exact replay returns canonical event sequence/time and performs no mutation.

- [ ] **Step 5: Commit preflight and replay**

```powershell
git add packages/postgresql-source-store/src/postgresql_source_store tests/unit/postgresql_source_store/test_error_mapping.py tests/unit/postgresql_source_store/test_replay_hydration.py tests/integration/source_publication
git commit -m "feat: resolve idempotent source publication replay"
```

---

### Task 8: Commit source creation atomically

**Files:**
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/publication_store.py`
- Create: `tests/unit/postgresql_source_store/test_content_object_reuse.py`
- Create: `tests/integration/source_publication/test_create_transaction.py`
- Create: `tests/integration/source_publication/test_create_rollback.py`

**Interfaces:**
- Consumes: verified receipt, create command, advisory locks, actor authorization and nine-table DML metadata.
- Produces: `commit_create()` that atomically creates/reuses the object, source, version 1, pointer, event, two intents and success audit.

- [ ] **Step 1: Write create graph, deduplication and rollback tests**

Assert exact row counts/values, `content_version=1`, null parent/base, active pointer, two upsert intents and `source.version_published`. Reusing identical bytes across different sources must produce one content object. Existing hash with a different key/size/media type must roll back with `source_content_object_conflict`.

Parameterize a fault hook after content object, source, version, pointer, event, first intent, second intent and audit; each injected exception must leave no partial graph.

- [ ] **Step 2: Run create tests and observe the unimplemented commit**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_content_object_reuse.py -q
uv run pytest tests/integration/source_publication/test_create_transaction.py tests/integration/source_publication/test_create_rollback.py -m local_stack -q
```

Expected: FAIL because `commit_create()` has no canonical write path.

- [ ] **Step 3: Implement the common locked prefix and create transition**

Inside one `READ COMMITTED` transaction:

```text
SET LOCAL bounds
-> pg_advisory_xact_lock(idempotency namespace, derived key)
-> revalidate actor and replay/mismatch
-> pg_advisory_xact_lock(source namespace, derived key)
-> reject existing global source
-> exact content-object upsert/select/compare
-> source pending
-> version 1
-> source active pointer
-> create event
-> qdrant + neo4j intents
-> success audit with safe diff hash
-> commit
```

Allocate UUIDv7 content/version/intent/audit IDs once per service invocation and reuse them through transaction retries. Use PostgreSQL event identity sequence and transaction timestamps.

- [ ] **Step 4: Prove create atomicity and unchanged migration head**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_content_object_reuse.py -q
uv run pytest tests/integration/source_publication/test_create_transaction.py tests/integration/source_publication/test_create_rollback.py -m local_stack -q
uv run alembic heads
uv run mypy packages/postgresql-source-store/src
```

Expected: all commands exit `0`; only `20260813_01` remains head.

- [ ] **Step 5: Commit atomic create**

```powershell
git add packages/postgresql-source-store/src/postgresql_source_store/publication_store.py tests/unit/postgresql_source_store/test_content_object_reuse.py tests/integration/source_publication/test_create_transaction.py tests/integration/source_publication/test_create_rollback.py
git commit -m "feat: commit source creation atomically"
```

---

### Task 9: Commit changed/no-change updates, concurrency and ambiguous outcomes

**Files:**
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/publication_store.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/error_mapping.py`
- Create: `tests/unit/postgresql_source_store/test_transaction_retry.py`
- Create: `tests/integration/source_publication/test_update_transaction.py`
- Create: `tests/integration/source_publication/test_publication_concurrency.py`
- Create: `tests/integration/source_publication/test_ambiguous_commit.py`
- Create: `tests/integration/source_publication/test_cancellation.py`

**Interfaces:**
- Consumes: committed sources/current versions, verified receipts and preflight/replay behavior.
- Produces: `commit_update()`, bounded retriable transaction runner and evidence-based crash-after-commit recovery.

- [ ] **Step 1: Write changed, no-change and stale-base tests**

Assert changed update creates ordinal `n+1`, parent=current, guarded pointer advance, event, two intents and success audit without changing type/title. Assert no-change creates event/audit only, keeps exact source `updated_at`, pointer, version/object/intent counts and persists `base_version_id == committed_version_id`.

Run base comparison first: a stale base with bytes equal to the current object must return `source_version_conflict`.

- [ ] **Step 2: Add deterministic concurrency and ambiguous-commit tests**

Cover:

- 100 exact concurrent replays produce one event and equivalent results;
- two different-key updates from one base produce one publish and one conflict;
- two creates for one source produce one source and one rejection;
- distinct sources with identical bytes produce one content object;
- cancellation releases locks and pool checkout;
- simulated lost commit acknowledgement resolves on a new connection;
- unavailable outcome lookup returns retryable `source_commit_outcome_unknown` and never claims rollback.

- [ ] **Step 3: Run tests and observe missing update/recovery behavior**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_transaction_retry.py -q
uv run pytest tests/integration/source_publication/test_update_transaction.py tests/integration/source_publication/test_publication_concurrency.py tests/integration/source_publication/test_ambiguous_commit.py tests/integration/source_publication/test_cancellation.py -m local_stack -q
```

Expected: FAIL on update, retry and ambiguous-commit cases.

- [ ] **Step 4: Implement locked update and bounded transaction recovery**

Select source/current version/current object `FOR UPDATE`; accept only `active` and `stored_not_indexed`. Guard changed pointer update with both workspace/source and `current_version_id=base_version_id`, requiring exactly one affected row.

Retry at most three total transaction attempts for deadlock, serialization failure and bounded lock contention, with injected cancellable random jitter in `[0.050, 0.250]` seconds. Business errors and integrity failures never retry.

On uncertain commit acknowledgement, invalidate the connection, use a fresh bounded connection for the same key/event/fingerprint lookup, return replay if present, retry only after proven absence, and otherwise raise the retryable unknown-outcome error.

- [ ] **Step 5: Run the complete publication suite**

```powershell
uv run pytest tests/unit/sources tests/unit/postgresql_source_store -q
uv run pytest tests/integration/source_publication -m local_stack -q
uv run mypy src/personal_os/sources packages/postgresql-source-store/src
uv run ruff check src/personal_os/sources packages/postgresql-source-store/src tests/unit/sources tests/unit/postgresql_source_store tests/integration/source_publication
```

Expected: all commands exit `0`; no test has an unbounded wait.

- [ ] **Step 6: Commit update and crash recovery**

```powershell
git add packages/postgresql-source-store/src/postgresql_source_store/publication_store.py packages/postgresql-source-store/src/postgresql_source_store/error_mapping.py tests/unit/postgresql_source_store/test_transaction_retry.py tests/integration/source_publication
git commit -m "feat: make source updates replay and crash safe"
```

---

### Task 10: Implement leased and fenced projection-intent persistence

**Files:**
- Create: `src/personal_os/sources/projection_dispatch.py`
- Modify: `src/personal_os/sources/ports.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/projection_intents.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Create: `tests/unit/sources/test_projection_dispatch.py`
- Create: `tests/unit/postgresql_source_store/test_projection_backoff.py`
- Create: `tests/integration/projection_dispatch/conftest.py`
- Create: `tests/integration/projection_dispatch/test_projection_intent_leases.py`

**Interfaces:**
- Consumes: committed `projection_intents` baseline rows, an injected clock/UUIDv7 generator and projection metrics.
- Produces: `ProjectionIntentStore`, `LeasedProjectionIntent`, bounded backoff and fenced claim/reclaim/ack/retry/terminal transitions.

- [ ] **Step 1: Write lease state-machine tests**

Pin batch limit 50, lease 60 seconds, ordering `(available_at, created_at, projection_intent_id)` and exponential backoff `min(300, 2 ** prior_attempt_count)` seconds. Assert claim uses `FOR UPDATE SKIP LOCKED`; concurrent claimers never own one intent; expired reclaim increments attempt and records `projection_dispatch_lease_expired`.

For acknowledgement, retry and terminal transitions, assert the `WHERE` clause includes intent ID, `status='leased'` and exact lease token. A stale token must affect zero rows and emit a diagnostic without overwriting state.

- [ ] **Step 2: Run focused tests and observe missing outbox store**

```powershell
uv run pytest tests/unit/sources/test_projection_dispatch.py tests/unit/postgresql_source_store/test_projection_backoff.py -q
uv run pytest tests/integration/projection_dispatch/test_projection_intent_leases.py -m local_stack -q
```

Expected: FAIL because intent ports and persistence do not exist.

- [ ] **Step 3: Implement claim/reclaim and fenced transitions**

The port must expose exactly:

```text
ProjectionIntentStore
  reclaim_expired(now: datetime) -> int
  claim_batch(now: datetime, limit: int) -> immutable tuple of LeasedProjectionIntent
  acknowledge_dispatched(intent_id: UUID, lease_token: UUID, now: datetime) -> bool
  release_retry(
    intent_id: UUID,
    lease_token: UUID,
    error_code: SafeToken,
    available_at: datetime,
    now: datetime,
  ) -> bool
  mark_terminal(
    intent_id: UUID,
    lease_token: UUID,
    error_code: SafeToken,
    now: datetime,
  ) -> bool
```

Use database time for state timestamps/expiry and commit claim before any caller performs network I/O. Increment attempt only on known dispatch outcome or lease expiry.

- [ ] **Step 4: Verify competing dispatchers and invariants**

```powershell
uv run pytest tests/unit/sources/test_projection_dispatch.py tests/unit/postgresql_source_store/test_projection_backoff.py -q
uv run pytest tests/integration/projection_dispatch/test_projection_intent_leases.py -m local_stack -q
uv run mypy src/personal_os/sources packages/postgresql-source-store/src
```

Expected: all commands exit `0`; each successful transition preserves baseline check constraints.

- [ ] **Step 5: Commit the durable projection outbox**

```powershell
git add src/personal_os/sources/ports.py src/personal_os/sources/projection_dispatch.py packages/postgresql-source-store/src/postgresql_source_store/projection_intents.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py tests/unit/sources/test_projection_dispatch.py tests/unit/postgresql_source_store/test_projection_backoff.py tests/integration/projection_dispatch
git commit -m "feat: add fenced projection intent leases"
```

---

### Task 11: Start one deterministic Temporal workflow per source event

**Files:**
- Create: `apps/worker/src/workflow_worker/projection_workflow_starter.py`
- Create: `apps/worker/src/workflow_worker/projection_dispatch_runtime.py`
- Modify: `apps/worker/src/workflow_worker/command.py`
- Create: `tests/unit/workflow_worker/test_projection_workflow_starter.py`
- Create: `tests/unit/workflow_worker/test_projection_dispatch_runtime.py`
- Create: `tests/integration/projection_dispatch/test_temporal_dispatch.py`
- Modify: `.github/workflows/canonical-postgresql-baseline.yml`

**Interfaces:**
- Consumes: leased intent references, `ProjectionWorkflowStarter`, Temporal namespace/task queue settings and fenced persistence transitions.
- Produces: `source_ingestion_reference/v1`, deterministic workflow start/get behavior and an eight-concurrent-start dispatcher runtime.

- [ ] **Step 1: Write safe input and duplicate execution tests**

Pin:

```text
workflow type = SourceIngestionWorkflow
workflow id   = source-ingestion/{workspace_id}/{event_id}
task queue    = source-ingestion
```

The Temporal input must contain only contract tag plus workspace/event/source/source-version UUIDs. Assert two projection intents for one event use identical workflow ID/input. Scan serialized input/history for title, object key, hash, path, content and provider exception sentinels.

Test `USE_EXISTING` for a running execution and reject duplicate run for a closed execution; resolve an exact already-closed deterministic execution as accepted, never terminate or replace it.

- [ ] **Step 2: Run unit and Temporal integration tests and observe missing adapter**

```powershell
uv run pytest tests/unit/workflow_worker/test_projection_workflow_starter.py tests/unit/workflow_worker/test_projection_dispatch_runtime.py -q
uv run pytest tests/integration/projection_dispatch/test_temporal_dispatch.py -m local_stack -q
```

Expected: FAIL because the Temporal adapter and dispatcher runtime do not exist.

- [ ] **Step 3: Implement the Temporal adapter and bounded dispatch loop**

Use `Client.start_workflow()` with a 10-second caller timeout, conflict policy `USE_EXISTING`, duplicate-run rejection and fixed task queue. Map SDK exceptions to closed retryable/non-retryable outcomes without provider text.

The dispatcher:

```text
reclaim expired -> claim at most 50 -> start at most 8 concurrently
-> started/existing: fenced dispatched ack
-> retryable: fenced pending release with capped backoff
-> contract error: fenced terminal transition
```

Graceful shutdown stops new claims, waits at most the start-call bound and leaves unknown attempts leased for expiry. Local/test permits loopback unauthenticated Temporal; staging/production refuses dispatcher activation until TLS/auth settings are defined by a deployment spec.

- [ ] **Step 4: Verify crash points against Temporal Server 1.31.2**

Test crash before start, after Temporal accepts and before database acknowledgement. Stop/restart the dispatcher and prove one workflow run, eventual dispatched rows and fenced stale leases. Prove Temporal accepts the start without an active workflow poller.

```powershell
uv run pytest tests/unit/workflow_worker -q
uv run pytest tests/integration/projection_dispatch -m local_stack -q
uv run mypy apps/worker/src src/personal_os/sources packages/postgresql-source-store/src
uv run lint-imports
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit Temporal dispatch**

```powershell
git add apps/worker/src/workflow_worker .github/workflows/canonical-postgresql-baseline.yml tests/unit/workflow_worker tests/integration/projection_dispatch/test_temporal_dispatch.py
git commit -m "feat: dispatch source events to Temporal"
```

---

### Task 12: Prove scale, query plans, rollback and data leakage

**Files:**
- Create: `tests/integration/source_publication/test_query_plans.py`
- Create: `tests/integration/source_publication/test_large_fixture_concurrency.py`
- Create: `tests/contract/source_publication/test_no_network_in_transaction.py`
- Create: `tests/contract/source_publication/test_no_public_api.py`
- Modify: `tests/contract/source_publication/test_telemetry_leakage.py`
- Modify: `.github/workflows/canonical-postgresql-baseline.yml`

**Interfaces:**
- Consumes: completed publication and dispatch paths.
- Produces: executable acceptance evidence for indexed access, concurrency bounds, architectural isolation and non-disclosure.

- [ ] **Step 1: Add 10,000-row and 100-concurrency acceptance tests**

Populate at least 10,000 source versions and 10,000 pending intents in the disposable database. Run `EXPLAIN (FORMAT JSON)` for current pointer, idempotent replay, version history and pending claim queries. Assert approved indexes appear and reject unbounded sequential scans on the populated relation.

Exercise 100 exact replays and independent-source publishes with finite task-group timeouts. Assert one canonical event for replays, no pool leak and no missed deadline; do not introduce a machine-specific latency threshold.

- [ ] **Step 2: Add static transaction and public-surface guards**

Parse the PostgreSQL transaction modules and assert they import/call neither object storage nor Temporal. Assert no source publication route appears in FastAPI/MCP/OpenAPI or generated TypeScript clients. Assert Alembic heads and migration file count remain unchanged.

- [ ] **Step 3: Run the new acceptance tests and fix only bounded defects**

```powershell
uv run pytest tests/contract/source_publication -q
uv run pytest tests/integration/source_publication/test_query_plans.py tests/integration/source_publication/test_large_fixture_concurrency.py -m local_stack -q
```

Expected before final adjustment: any failure identifies a concrete missing index use, timeout bound, leakage mapping or boundary violation. Adjust DML/query shape and test fixtures only; do not add a migration or relax a privacy assertion.

- [ ] **Step 4: Run all source/dispatch tests together**

```powershell
uv run pytest tests/unit/sources tests/unit/postgresql_source_store tests/unit/workflow_worker tests/contract/source_publication -q
uv run pytest tests/integration/source_publication tests/integration/projection_dispatch -m local_stack -q
```

Expected: all commands exit `0`; disposable project resources are absent after fixture cleanup.

- [ ] **Step 5: Commit acceptance coverage**

```powershell
git add tests/contract/source_publication tests/integration/source_publication tests/integration/projection_dispatch .github/workflows/canonical-postgresql-baseline.yml
git commit -m "test: prove source publication recovery contracts"
```

---

### Task 13: Update canonical documentation and run the complete release gate

**Files:**
- Modify: `docs/03-DATA_OWNERSHIP_AND_STORAGE.md`
- Modify: `docs/07-POSTGRESQL_DATA_MODEL.md`
- Modify: `docs/11-TEMPORAL_WORKFLOWS.md`
- Modify: `docs/19-ARCHITECTURE_DECISIONS.md`
- Modify: `docs/20-IMPLEMENTATION_PLAN.md`
- Create: `docs/operations/source-publication.md`

**Interfaces:**
- Consumes: verified implementation behavior and final commands from Tasks 1–12.
- Produces: canonical contract alignment, operator recovery instructions and one clean full verification result.

- [ ] **Step 1: Reconcile canonical docs with implemented behavior**

Document exact create/update/no-change/replay semantics, receipt age, lock order, ambiguous commit handling, outbox lease bounds, deterministic workflow identity, readiness dependencies and safe operator actions. Record that Phase 1 queues `SourceIngestionWorkflow` starts but does not register the Phase 3 workflow implementation.

The runbook must explain how to distinguish retryable unknown outcome from known rejection, inspect only safe counts/statuses, allow lease expiry, and retry the exact original event/key. It must prohibit manual pointer edits, intent deletion, R2 compensation and Temporal termination/replacement.

- [ ] **Step 2: Verify canonical constraints and document hygiene**

```powershell
rg -n "MinIO|DATABASE_URL|pg_advisory_lock\(" docs src packages apps tests
uv run alembic heads
git diff --check
```

Expected: no MinIO/runtime `DATABASE_URL` or session advisory lock is introduced by this work; Alembic reports `20260813_01`; diff check is clean. Existing historical prose matches must be reviewed individually rather than suppressed.

- [ ] **Step 3: Run the complete cross-platform quality gate**

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".local/source-publication-verification-venv"
uv sync --all-packages --frozen
uv run --no-sync poe verify
```

Expected: formatting, Ruff, mypy strict, import boundaries, Python/TypeScript tests and all builds exit `0`.

- [ ] **Step 4: Run the disposable PostgreSQL/Temporal gate one final time**

```powershell
$env:CI = "true"
$env:LOCAL_STACK_TEST_PROJECT = "knowledge-ci-81413-1"
$env:POSTGRES_PORT = "15443"
$env:TEMPORAL_GRPC_PORT = "17243"
$env:TEMPORAL_UI_PORT = "18083"
uv run pytest tests/integration/source_publication tests/integration/projection_dispatch -m local_stack -q
```

Expected: all tests pass and exact-label cleanup removes only `knowledge-ci-81413-1` containers, networks, volumes and generated secrets.

- [ ] **Step 5: Inspect final scope and commit documentation**

```powershell
git status --short
git diff --stat
git diff -- docs/03-DATA_OWNERSHIP_AND_STORAGE.md docs/07-POSTGRESQL_DATA_MODEL.md docs/11-TEMPORAL_WORKFLOWS.md docs/19-ARCHITECTURE_DECISIONS.md docs/20-IMPLEMENTATION_PLAN.md docs/operations/source-publication.md
git add docs/03-DATA_OWNERSHIP_AND_STORAGE.md docs/07-POSTGRESQL_DATA_MODEL.md docs/11-TEMPORAL_WORKFLOWS.md docs/19-ARCHITECTURE_DECISIONS.md docs/20-IMPLEMENTATION_PLAN.md docs/operations/source-publication.md
git commit -m "docs: operationalize source version publication"
```

Expected: the remaining worktree is clean; no public API, migration, projection implementation or unrelated file is present.

---

## Completion Checklist

- [ ] Create/update are the only publication commands.
- [ ] Exact replay completes before R2 and creates no row.
- [ ] New publication uses an exact internal receipt no older than five minutes.
- [ ] Create and changed update commit canonical graph, event, two intents and audit atomically.
- [ ] Base comparison precedes content comparison; stale clients conflict.
- [ ] No-change writes event/audit only and leaves the source row untouched.
- [ ] Idempotency/event misuse rejects with tenant-safe details and a trusted rejection audit.
- [ ] Content-object deduplication reuses only exact metadata.
- [ ] Transaction retries are bounded and ambiguous outcomes are resolved with evidence.
- [ ] Intent claim/ack/retry/terminal transitions are leased, fenced and crash-safe.
- [ ] Two intents for one event start or resolve one deterministic Temporal execution.
- [ ] R2 and Temporal calls never occur inside a PostgreSQL transaction.
- [ ] Telemetry and Temporal history contain none of the forbidden data.
- [ ] 10,000-row query plans remain indexed and 100 concurrent replays remain idempotent.
- [ ] Alembic head and the nine-table baseline remain unchanged.
- [ ] `uv run --no-sync poe verify` and disposable PostgreSQL/Temporal integration gates pass.
