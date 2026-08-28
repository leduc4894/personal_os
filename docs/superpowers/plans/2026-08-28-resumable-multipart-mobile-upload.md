# Resumable Multipart Mobile Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver resumable, policy-checked multipart staging upload for files above 16 MiB through 100 MiB, with server-side verification/promotion and exact cleanup on Desktop and Mobile.

**Architecture:** Keep multipart orchestration in a framework-neutral `personal_os.multipart_upload` domain. PostgreSQL owns session, provider and cleanup state; the R2 adapter owns provider SDK calls; FastAPI and the plugin expose only safe opaque state. The completion service full-verifies staging and calls the existing `SmallFilePublicationGateway`, so only Phase 1 CAS creates/reuses canonical bytes.

**Tech Stack:** Python 3.14 / mypy strict, FastAPI / Pydantic, SQLAlchemy / Alembic / PostgreSQL, aiobotocore Cloudflare R2, Temporal, TypeScript strict / Obsidian `requestUrl`, sql.js, Vitest, pytest, OpenAPI TypeScript generation, WDIO.

**Spec:** `docs/superpowers/specs/2026-08-28-resumable-multipart-mobile-upload-design.md`

## Global Constraints

- Route only `16 MiB < size_bytes <= 100 MiB`; the ordinary part is exactly 8 MiB and the final part is positive and at most 8 MiB.
- Permit at most 3 active Desktop parts and 2 active Mobile parts; the server returns plan geometry and clients do not duplicate the routing threshold.
- Presigned URLs live 10 minutes and multipart sessions expire 24 hours after creation.
- PostgreSQL retains staging key, provider upload ID and ETags; plugin SQLite persists only opaque session ID, safe geometry/progress/expiry/state and closed reason tokens.
- Never expose or log raw content, path/locator, full digest, URL/query signature, staging key, provider ID/ETag, credential or provider exception text.
- Session ownership derives from existing device Bearer context; no request selects workspace/device and no URL targets a canonical key.
- Promotion full-verifies staging SHA-256/size/media and streams it into the existing Phase 1 conditional CAS writer. Never use R2 copy/rename to promote.
- Cleanup acts only on one persisted exact staging key/upload ID. Production and test code must never list a bucket, delete a prefix/wildcard, or delete a canonical object.
- Every external call has an explicit timeout, bounded retry, typed mapping and closed diagnostic/metric reason.
- Keep domain modules free of FastAPI, SQLAlchemy, aiobotocore and Temporal imports. Do not add a production dependency.
- Every migration has empty upgrade, fixture upgrade and downgrade tests; all public API changes regenerate OpenAPI and the generated client.
- Live work uses one disposable `knowledge-ci-*` project through `.local/serve-live-ci.sh`; the Desktop WDIO and physical Mobile gates cannot be substituted with mocks.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `src/personal_os/multipart_upload/contracts.py` | Closed session/part geometry, opaque public session ID, states and safe results. |
| `src/personal_os/multipart_upload/ports.py` | Domain ports for session store, policy guard, staging provider, publication and cleanup scheduler. |
| `src/personal_os/multipart_upload/service.py` | Create/status/part URL/complete/abort orchestration, verification and idempotency. |
| `src/personal_os/multipart_upload/cleanup.py` | Exact cleanup claim/run state transitions and deterministic workflow input. |
| `src/personal_os/multipart_upload/errors.py`, `metrics.py` | Typed closed errors and low-cardinality metrics. |
| `packages/postgresql-source-store/src/postgresql_source_store/multipart_upload_store.py` | SQLAlchemy transaction/lease implementation; no provider I/O inside a transaction. |
| `packages/postgresql-source-store/src/postgresql_source_store/tables.py` | Metadata for `multipart_uploads` and `multipart_parts`. |
| `migrations/versions/20260828_01_add_multipart_upload_sessions.py` | Reversible Phase 2 schema. |
| `packages/r2-object-storage/src/r2_object_storage/multipart.py` | R2 multipart staging adapter and all SDK request/response mapping. |
| `apps/api/src/api_runtime/multipart_upload_{models,routes,composition}.py` | Strict wire schema, safe endpoint closure and composition root. |
| `apps/worker/src/workflow_worker/multipart_cleanup_workflow.py` | Bounded Temporal cleanup workflow and activity registration. |
| `apps/obsidian-plugin/src/journal/multipart-upload.ts` | Mobile-safe client scheduler/resume protocol; no persistence implementation. |
| `apps/obsidian-plugin/src/journal/{sqlite-database,repository,sync-api,queue-driver}.ts` | SQLite v8 progress, safe API client and dispatch integration. |
| `tests/{unit,contract,integration}/multipart_upload/` | Domain/property, privacy, database/R2/Temporal test coverage. |
| `apps/obsidian-plugin/src/journal/multipart-upload.test.ts` | Client resume, geometry, URL secrecy and concurrency tests. |
| `apps/obsidian-plugin/test/specs/multipart-upload.e2e.ts` | Desktop WDIO journey; paired physical-Mobile procedure is documented. |
| `docs/operations/resumable-multipart-upload.md` | Operator recovery, status/reason interpretation and safe live procedure. |

## Task 1: Establish the framework-neutral multipart contract

**Files:**

- Create: `src/personal_os/multipart_upload/__init__.py`
- Create: `src/personal_os/multipart_upload/contracts.py`
- Create: `src/personal_os/multipart_upload/errors.py`
- Create: `src/personal_os/multipart_upload/ports.py`
- Modify: `src/personal_os/small_file_sync/contracts.py`
- Modify: `src/personal_os/small_file_sync/service.py`
- Test: `tests/unit/multipart_upload/test_contracts.py`
- Test: `tests/unit/multipart_upload/test_errors.py`
- Test: `tests/unit/small_file_sync/test_contracts.py`
- Modify: `src/personal_os/error_contracts/codes.py`

**Interfaces:**

- Consumes: `ContentDigest`, `CanonicalMediaType`, `SmallFileDeviceContext`, `SmallFilePreflight`, `DiagnosticContext`.
- Produces: `MultipartUploadSessionId`, `MultipartUploadPlan`, `MultipartSessionState`, `MultipartPartRange`, `MultipartSessionStatus`, `MultipartUploadApplicationService`, the `MULTIPART_UPLOAD` preflight outcome, and registered `MULTIPART_*` error codes.

- [ ] **Step 1: Write failing geometry, redaction and state-transition tests**

```python
def test_plan_for_maximum_file_has_thirteen_exact_parts() -> None:
    plan = MultipartUploadPlan.from_size_bytes(100 * 1024 * 1024)
    assert plan.part_count == 13
    assert plan.part_range(13).size_bytes == 4 * 1024 * 1024

def test_session_id_and_provider_values_redact_repr() -> None:
    assert "session-value" not in repr(MultipartUploadSessionId("session-value" * 4))

def test_completed_session_cannot_transition_back_to_uploading() -> None:
    with pytest.raises(ValueError):
        MultipartSessionState.COMMITTED.require_transition_to(MultipartSessionState.UPLOADING)

async def test_preflight_routes_one_byte_over_single_part_to_multipart(service) -> None:
    result = await service.preflight(preflight_with_size((16 * 1024 * 1024) + 1), device, context)
    assert result.outcome is SmallFilePreflightOutcome.MULTIPART_UPLOAD
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `uv run pytest tests/unit/multipart_upload/test_contracts.py tests/unit/multipart_upload/test_errors.py -q`

Expected: FAIL because the package, value objects and error registry entries do not exist.

- [ ] **Step 3: Implement minimal immutable contracts and ports**

```python
@dataclass(frozen=True, slots=True)
class MultipartUploadPlan:
    session_id: MultipartUploadSessionId
    part_size_bytes: int
    part_count: int
    expires_at: datetime

    @classmethod
    def from_size_bytes(cls, size_bytes: int) -> MultipartPartGeometry:
        raise NotImplementedError

class MultipartUploadApplicationService(Protocol):
    async def create_or_resume(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartUploadPlan:
        raise NotImplementedError

    async def issue_part_url(
        self,
        *,
        session_id: MultipartUploadSessionId,
        part_number: int,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartPartUrl:
        raise NotImplementedError

    async def complete(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartCompletionResult:
        raise NotImplementedError
```

Register one `ErrorDefinition` each for the eleven closed codes in spec §7,
with retryability matching the spec. Keep provider ID/ETag value objects
private to the store/provider ports and give every sensitive value a redacted
`__repr__`. Replace the small-file upper-bound rejection with the closed
100 MiB product maximum, add `SmallFilePreflightOutcome.MULTIPART_UPLOAD`, and
make the existing preflight service select that outcome only when size is
strictly greater than its unchanged 16 MiB routing constant.

- [ ] **Step 4: Run unit, import and strict typing gates**

Run: `uv run pytest tests/unit/multipart_upload/test_contracts.py tests/unit/multipart_upload/test_errors.py tests/unit/small_file_sync/test_contracts.py -q; uv run mypy src/personal_os/multipart_upload src/personal_os/small_file_sync src/personal_os/error_contracts`

Expected: PASS with zero mypy errors.

- [ ] **Step 5: Commit the isolated contract**

```bash
git add src/personal_os/multipart_upload src/personal_os/small_file_sync tests/unit/multipart_upload tests/unit/small_file_sync src/personal_os/error_contracts/codes.py
git commit -m "feat: define multipart upload contract"
```

## Task 2: Add canonical session/part schema and migration gates

**Files:**

- Create: `migrations/versions/20260828_01_add_multipart_upload_sessions.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/tables.py`
- Test: `tests/unit/migrations/test_multipart_upload_migration.py`
- Test: `tests/contract/multipart_upload/test_table_metadata.py`

**Interfaces:**

- Consumes: Task 1 states/geometry and `small_file_upload_operations.operation_id`.
- Produces: schema-owned `multipart_uploads` and `multipart_parts` relations with exact uniqueness, ownership and lifecycle constraints.

- [ ] **Step 1: Write failing migration and metadata tests**

```python
def test_upgrade_creates_only_private_provider_columns() -> None:
    columns = inspect_schema("multipart_uploads")
    assert {"workspace_id", "device_id", "operation_id", "staging_key", "provider_upload_id"} <= columns
    assert "presigned_url" not in columns

def test_part_number_is_unique_per_session() -> None:
    assert unique_constraint("multipart_parts") == ("multipart_upload_id", "part_number")
```

- [ ] **Step 2: Run migration tests and confirm RED**

Run: `uv run pytest tests/unit/migrations/test_multipart_upload_migration.py tests/contract/multipart_upload/test_table_metadata.py -q`

Expected: FAIL because revision `20260828_01` and the tables are absent.

- [ ] **Step 3: Implement upgrade/downgrade and metadata**

Create `multipart_uploads` with UUID PK, foreign keys to workspace/device and
`small_file_upload_operations`, private text columns for staging/provider
identity, declared fingerprint/geometry, state/expiry/claim lease/terminal
result and cleanup state/retry columns. Create `multipart_parts` with UUID PK,
session FK, validated part number/range, private ETag and provider byte count.
Add an active-session uniqueness constraint on `(operation_id)` and indexes for
owner/status lookup plus expiry cleanup claim. The downgrade drops indexes,
parts, then sessions. Set `down_revision` to the current Phase-2 head rather
than guessing it; the implementer verifies this via `alembic heads` first.

- [ ] **Step 4: Run empty/fixture upgrade and downgrade tests**

Run: `uv run pytest tests/unit/migrations/test_multipart_upload_migration.py tests/contract/multipart_upload/test_table_metadata.py tests/unit/migrations/test_database_migration_runtime.py -q`

Expected: PASS, including downgrade back to the prior head.

- [ ] **Step 5: Commit schema independently**

```bash
git add migrations/versions/20260828_01_add_multipart_upload_sessions.py packages/postgresql-source-store/src/postgresql_source_store/tables.py tests/unit/migrations tests/contract/multipart_upload
git commit -m "feat: persist multipart upload sessions"
```

## Task 3: Implement the PostgreSQL session store and state fencing

**Files:**

- Create: `packages/postgresql-source-store/src/postgresql_source_store/multipart_upload_store.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Test: `tests/unit/postgresql_source_store/test_multipart_upload_store.py`
- Test: `tests/integration/multipart_upload/test_session_store_transactions.py`

**Interfaces:**

- Consumes: Task 1 `MultipartSessionStore` port and Task 2 tables.
- Produces: `PostgreSqlMultipartUploadStore` methods `reserve_session`, `load_owned_session`, `record_provider_part`, `claim_completion`, `record_terminal_result`, `claim_cleanup_batch`, `record_cleanup_result`.

- [ ] **Step 1: Write failing replay/fencing tests**

```python
async def test_same_operation_replays_one_session_without_new_provider_work(store) -> None:
    first = await store.reserve_session(bound_operation, device, now)
    replay = await store.reserve_session(bound_operation, device, now)
    assert replay.session_id == first.session_id

async def test_old_completion_lease_cannot_record_terminal_result(store) -> None:
    old, replacement = await claim_then_expire_and_reclaim(store)
    with pytest.raises(MultipartUploadError, match="multipart_completion_in_progress"):
        await store.record_terminal_result(old, terminal_result)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest tests/unit/postgresql_source_store/test_multipart_upload_store.py tests/integration/multipart_upload/test_session_store_transactions.py -q`

Expected: FAIL because the PostgreSQL store does not exist.

- [ ] **Step 3: Implement transactions without provider I/O**

Use `SELECT * FROM multipart_uploads WHERE operation_id = :operation_id FOR UPDATE`
for session mutation, an explicit finite completion
lease and compare-and-set state/claim token on every terminal write. Store
provider part facts only after `ListParts` confirms them. Return typed closed
errors for owner mismatch, expiry, invalid state and stale claimant. Commit the
lease/state before crossing to R2; provider calls must never occur while a
database transaction is open.

- [ ] **Step 4: Run race and query-plan gates**

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-multipart-store uv run pytest tests/unit/postgresql_source_store/test_multipart_upload_store.py tests/integration/multipart_upload/test_session_store_transactions.py -q`

Expected: PASS, including concurrent replay/claim and explain-plan assertions.

- [ ] **Step 5: Commit the durable store**

```bash
git add packages/postgresql-source-store/src/postgresql_source_store tests/unit/postgresql_source_store tests/integration/multipart_upload
git commit -m "feat: fence multipart session state"
```

## Task 4: Extend the R2 adapter only for private multipart staging

**Files:**

- Create: `packages/r2-object-storage/src/r2_object_storage/multipart.py`
- Modify: `packages/r2-object-storage/src/r2_object_storage/client.py`
- Modify: `packages/r2-object-storage/src/r2_object_storage/error_mapping.py`
- Modify: `tests/contract/object_storage/scripted_s3.py`
- Test: `tests/contract/object_storage/test_r2_multipart_staging_contract.py`
- Test: `tests/unit/object_storage/test_r2_multipart_error_mapping.py`

**Interfaces:**

- Consumes: Task 1 staging provider port.
- Produces: `R2MultipartStagingProvider.create_upload`, `presign_part`, `list_parts`, `complete_upload`, `abort_upload`, `delete_staging_object`.

- [ ] **Step 1: Write failing capability-boundary tests**

```python
async def test_presigned_part_cannot_target_canonical_key(provider) -> None:
    with pytest.raises(MultipartUploadError):
        await provider.presign_part(canonical_key, provider_upload_id, part_range)

async def test_cleanup_uses_exact_upload_or_key_without_list(provider) -> None:
    await provider.abort_upload(staging_key, upload_id)
    assert provider.calls == ["abort_multipart_upload"]
```

- [ ] **Step 2: Run contract tests and confirm RED**

Run: `uv run pytest tests/contract/object_storage/test_r2_multipart_staging_contract.py tests/unit/object_storage/test_r2_multipart_error_mapping.py -q`

Expected: FAIL because multipart staging methods and scripted S3 doubles are absent.

- [ ] **Step 3: Implement the provider boundary**

Keep all aiobotocore SDK keywords in `multipart.py`. Require a validated private
staging-key value before create/presign/list/complete/abort/delete. Presign a
single `upload_part` request with fixed `PartNumber`, `UploadId`, content length
and ten-minute `ExpiresIn`. Map provider errors to the Task 1 codes and never
include exception text or provider identity in errors/logs. Add no `list_objects`,
copy or canonical delete method.

- [ ] **Step 4: Run object-storage regression gates**

Run: `uv run pytest tests/contract/object_storage/test_r2_multipart_staging_contract.py tests/contract/object_storage/test_r2_adapter_contract.py tests/unit/object_storage/test_r2_multipart_error_mapping.py -q; uv run mypy packages/r2-object-storage/src`

Expected: PASS with no forbidden broad-cleanup call in the adapter AST contract.

- [ ] **Step 5: Commit adapter capability**

```bash
git add packages/r2-object-storage tests/contract/object_storage tests/unit/object_storage
git commit -m "feat: add private R2 multipart staging"
```

## Task 5: Build the framework-neutral multipart orchestration service

**Files:**

- Create: `src/personal_os/multipart_upload/service.py`
- Create: `src/personal_os/multipart_upload/metrics.py`
- Test: `tests/unit/multipart_upload/test_service.py`
- Test: `tests/unit/multipart_upload/fakes.py`

**Interfaces:**

- Consumes: Tasks 1–4 ports; existing `SmallFilePolicyGuard`, `SmallFilePublicationGateway`, and bounded `CanonicalObjectStore` reader/writer.
- Produces: `MultipartUploadApplicationService.create_or_resume`, `status`, `issue_part_url`, `complete`, `abort` and `run_exact_cleanup`.

- [ ] **Step 1: Write failing service tests for the critical path**

```python
async def test_complete_full_verifies_then_publishes_once(harness) -> None:
    result = await harness.service.complete(harness.session_id, harness.device, harness.context)
    assert result.terminal_result.result_kind is SmallFileTerminalResultKind.COMMITTED
    assert harness.publisher.calls == ["publish_update"]

async def test_digest_mismatch_never_calls_publisher_and_schedules_cleanup(harness) -> None:
    harness.staging_reader.digest = different_digest
    with pytest.raises(MultipartUploadError, match="multipart_integrity_failed"):
        await harness.service.complete(harness.session_id, harness.device, harness.context)
    assert harness.publisher.calls == []
```

- [ ] **Step 2: Run service tests and confirm RED**

Run: `uv run pytest tests/unit/multipart_upload/test_service.py -q`

Expected: FAIL because orchestration and fakes are absent.

- [ ] **Step 3: Implement the exact ordered service flow**

`create_or_resume` reuses the preflight/idempotency binding, policy guard and
reserved operation; `issue_part_url` rechecks ownership/state/expiry/policy;
`status` reconciles `ListParts`; `complete` claims a lease then executes
`ListParts → CompleteMultipartUpload → bounded full verification spool →
SmallFilePublicationGateway → frozen terminal write → cleanup request`.
Treat provider response loss as status/replay, recheck base/policy before
publication, and record a terminal no-candidate conflict outcome when the
existing publication path reports stale base. Ensure every `BaseException`
after provider work releases or persists the exact cleanup obligation.

- [ ] **Step 4: Run focused service/diagnostic/type gates**

Run: `uv run pytest tests/unit/multipart_upload/test_service.py tests/unit/multipart_upload/test_errors.py -q; uv run mypy src/personal_os/multipart_upload`

Expected: PASS, including completion race, policy advance, local error mapping and no swallowed cleanup reason.

- [ ] **Step 5: Commit the domain service**

```bash
git add src/personal_os/multipart_upload tests/unit/multipart_upload
git commit -m "feat: orchestrate multipart verification and promotion"
```

## Task 6: Add bounded Temporal expiry and exact-cleanup execution

**Files:**

- Create: `apps/worker/src/workflow_worker/multipart_cleanup_workflow.py`
- Modify: `apps/worker/src/workflow_worker/command.py`
- Modify: `apps/worker/src/workflow_worker/__init__.py`
- Test: `tests/unit/workflow_worker/test_multipart_cleanup_workflow.py`
- Test: `tests/integration/multipart_upload/test_cleanup_workflow.py`

**Interfaces:**

- Consumes: Task 3 cleanup claims and Task 5 `run_exact_cleanup`.
- Produces: deterministic `multipart_cleanup/<opaque-session-batch>` workflow input containing only opaque IDs/state/reason; bounded activity retry and claim release.

- [ ] **Step 1: Write failing workflow and cancellation tests**

```python
async def test_expired_session_aborts_only_its_provider_upload(worker_harness) -> None:
    await worker_harness.run_cleanup_for(expired_session)
    assert worker_harness.provider.aborted == [expired_session.private_identity]
    assert worker_harness.provider.deleted == []

async def test_cleanup_failure_persists_closed_reason_and_next_retry(worker_harness) -> None:
    worker_harness.provider.fail_abort = True
    await worker_harness.run_cleanup_for(expired_session)
    assert await worker_harness.store.cleanup_state(expired_session) == "failed"
```

- [ ] **Step 2: Run workflow tests and confirm RED**

Run: `uv run pytest tests/unit/workflow_worker/test_multipart_cleanup_workflow.py tests/integration/multipart_upload/test_cleanup_workflow.py -q`

Expected: FAIL because no workflow/activity is registered.

- [ ] **Step 3: Implement deterministic workflow scheduling**

Mirror the existing worker's durable workflow registration pattern. Claim a
bounded batch of overdue exact session rows, run one finite activity per row
with explicit timeout/retry policy, heartbeat only opaque count/progress, and
record success/failure through the store's lease token. On cancellation, finish
only currently safe exact cleanup or persist `cleanup_pending`; never convert a
cancelled workflow to an untracked resource.

- [ ] **Step 4: Run worker/integration gates**

Run: `uv run pytest tests/unit/workflow_worker/test_multipart_cleanup_workflow.py tests/integration/multipart_upload/test_cleanup_workflow.py tests/unit/workflow_worker/test_command.py -q`

Expected: PASS with workflow history assertions excluding private values.

- [ ] **Step 5: Commit exact cleanup workflow**

```bash
git add apps/worker/src/workflow_worker tests/unit/workflow_worker tests/integration/multipart_upload
git commit -m "feat: clean multipart staging exactly"
```

## Task 7: Expose strict authenticated multipart API routes

**Files:**

- Create: `apps/api/src/api_runtime/multipart_upload_models.py`
- Create: `apps/api/src/api_runtime/multipart_upload_routes.py`
- Create: `apps/api/src/api_runtime/multipart_upload_composition.py`
- Modify: `apps/api/src/api_runtime/application.py`
- Modify: `src/personal_os/api_contracts/request_values.py`
- Test: `tests/unit/api_runtime/test_multipart_upload_models.py`
- Test: `tests/unit/api_runtime/test_multipart_upload_routes.py`
- Test: `tests/contract/api/test_multipart_upload_routes.py`

**Interfaces:**

- Consumes: Task 5 service and existing `require_sync_device` dependency.
- Produces: the five spec §5 endpoints and OpenAPI operation IDs `createMultipartUploadSession`, `getMultipartUploadSession`, `issueMultipartPartUrl`, `completeMultipartUploadSession`, `abortMultipartUploadSession`.

- [ ] **Step 1: Write failing endpoint shape/auth tests**

```python
def test_part_url_response_is_no_store_and_not_returned_by_status(client) -> None:
    issued = client.post(part_url_path).json()["data"]
    assert issued["url"].startswith("https://")
    assert client.get(status_path).json()["data"].get("url") is None

def test_foreign_device_cannot_read_or_abort_session(client, foreign_token) -> None:
    response = client.get(status_path, headers=bearer(foreign_token))
    assert response.status_code in {403, 404}
```

- [ ] **Step 2: Run API tests and confirm RED**

Run: `uv run pytest tests/unit/api_runtime/test_multipart_upload_models.py tests/unit/api_runtime/test_multipart_upload_routes.py tests/contract/api/test_multipart_upload_routes.py -q`

Expected: FAIL because models/routes/operation IDs are absent.

- [ ] **Step 3: Implement strict Pydantic models and closures**

Use frozen `extra="forbid"` request/response models. Validate opaque session
grammar at the boundary, set `ApiRouteTemplate` for every route, use canonical
success/error envelopes and `Cache-Control: no-store`. The one URL response
must not be copied into diagnostics or status models. Compose the service with
the existing server lifecycle so its R2 client closes exactly once.

- [ ] **Step 4: Run API contract and OpenAPI checks**

Run: `uv run pytest tests/unit/api_runtime/test_multipart_upload_models.py tests/unit/api_runtime/test_multipart_upload_routes.py tests/contract/api/test_multipart_upload_routes.py tests/contract/api/test_openapi_schema.py -q; uv run poe api-contract-check`

Expected: PASS with exactly five new authenticated route entries.

- [ ] **Step 5: Commit API boundary**

```bash
git add apps/api/src/api_runtime src/personal_os/api_contracts tests/unit/api_runtime tests/contract/api
git commit -m "feat: expose multipart upload sessions"
```

## Task 8: Regenerate API client and wire server composition

**Files:**

- Modify: `packages/api-client/openapi.json`
- Modify: `packages/api-client/src/generated/schema.ts`
- Modify: `packages/api-client/src/generated/client.ts`
- Modify: `apps/api/src/api_runtime/server.py`
- Test: `tests/contract/api/test_multipart_upload_openapi.py`
- Test: `packages/api-client/src/generated/schema.test-d.ts`

**Interfaces:**

- Consumes: Task 7 served OpenAPI document.
- Produces: generated TypeScript multipart route types and application composition of the production service/store/provider.

- [ ] **Step 1: Write failing snapshot/type tests**

```python
def test_openapi_exposes_only_safe_multipart_response_fields(document) -> None:
    text = json.dumps(document)
    assert "provider_upload_id" not in text
    assert "staging_key" not in text
    assert "completeMultipartUploadSession" in text
```

- [ ] **Step 2: Run snapshot tests and confirm RED**

Run: `uv run pytest tests/contract/api/test_multipart_upload_openapi.py -q; pnpm --dir packages/api-client run typecheck`

Expected: FAIL because generated multipart operations are absent.

- [ ] **Step 3: Generate and bind composition**

Run the repository's existing OpenAPI generation command, commit its exact
output, and bind `PostgreSqlMultipartUploadStore`, R2 staging provider,
policy guard, publication gateway and cleanup scheduler in the serve graph.
The offline composition receives a deterministic no-network fake that returns
typed dependency-unavailable behavior, never a permissive fake URL.

- [ ] **Step 4: Run generated-client and composition gates**

Run: `uv run poe api-contract-check; pnpm --dir packages/api-client run typecheck; uv run pytest tests/contract/api/test_multipart_upload_openapi.py tests/unit/api_runtime/test_server.py -q`

Expected: PASS with deterministic generated output.

- [ ] **Step 5: Commit generated boundary**

```bash
git add packages/api-client apps/api/src/api_runtime tests/contract/api tests/unit/api_runtime
git commit -m "feat: generate multipart upload API client"
```

## Task 9: Persist safe multipart progress in plugin SQLite v8

**Files:**

- Modify: `apps/obsidian-plugin/src/journal/sqlite-database.ts`
- Modify: `apps/obsidian-plugin/src/journal/repository.ts`
- Modify: `apps/obsidian-plugin/src/journal/contracts.ts`
- Test: `apps/obsidian-plugin/src/journal/sqlite-database.test.ts`
- Test: `apps/obsidian-plugin/src/journal/repository.test.ts`

**Interfaces:**

- Consumes: Task 1 safe progress shape and existing journal event IDs.
- Produces: `MultipartProgressRecord`, repository methods `saveMultipartProgress`, `readMultipartProgress`, `clearMultipartProgress` and schema version 8 migration.

- [ ] **Step 1: Write failing migration/privacy tests**

```ts
it("migrates v7 journal data and persists only safe multipart progress", async () => {
  const repository = await openV7ThenMigrate();
  await repository.saveMultipartProgress(record);
  expect(await repository.readMultipartProgress(record.eventId)).toEqual(record);
  expect(databaseDump()).not.toContain("X-Amz-Signature");
  expect(databaseDump()).not.toContain("provider-upload-id");
});
```

- [ ] **Step 2: Run plugin tests and confirm RED**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sqlite-database.test.ts src/journal/repository.test.ts`

Expected: FAIL because v8 and multipart repository methods are absent.

- [ ] **Step 3: Implement SQLite v8 atomic progress records**

Add a per-event table keyed by opaque event ID with session ID, fixed geometry,
expiry, completed part-number JSON validated against geometry, closed state and
safe reason. Create/read/update/clear occur in the same repository mutation
that changes event dispatch state. Reject unknown fields and invalid completed
numbers before SQL mutation; migration preserves every v7 row unchanged.

- [ ] **Step 4: Run plugin migration/type gates**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sqlite-database.test.ts src/journal/repository.test.ts; pnpm --dir apps/obsidian-plugin run typecheck`

Expected: PASS and no URL/provider/digest sentinel persists.

- [ ] **Step 5: Commit plugin durable state**

```bash
git add apps/obsidian-plugin/src/journal
git commit -m "feat: persist safe multipart upload progress"
```

## Task 10: Implement the Mobile-safe multipart API client and scheduler

**Files:**

- Create: `apps/obsidian-plugin/src/journal/multipart-upload.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-api.ts`
- Modify: `apps/obsidian-plugin/src/journal/queue-driver.ts`
- Test: `apps/obsidian-plugin/src/journal/multipart-upload.test.ts`
- Test: `apps/obsidian-plugin/src/journal/sync-api.test.ts`
- Test: `apps/obsidian-plugin/src/journal/queue-driver.test.ts`

**Interfaces:**

- Consumes: Task 8 generated/wire shapes and Task 9 progress store.
- Produces: `MultipartUploadRunner.run(event, platform)` and `JournalSyncApi` methods for create/status/part URL/complete/abort.

- [ ] **Step 1: Write failing scheduler/resume tests**

```ts
it("resumes only unfinished Mobile parts with maximum two active PUTs", async () => {
  const result = await harness.runner.run(harness.event, "mobile");
  expect(result.outcome).toBe("committed");
  expect(harness.maxActivePartPuts).toBeLessThanOrEqual(2);
  expect(harness.partPutNumbers).toEqual([2, 3]);
});

it("never stores a presigned URL after a part PUT", async () => {
  await harness.runner.run(harness.event, "desktop");
  expect(harness.repository.serializedState()).not.toContain("X-Amz-Signature");
});
```

- [ ] **Step 2: Run scheduler tests and confirm RED**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/journal/multipart-upload.test.ts src/journal/sync-api.test.ts src/journal/queue-driver.test.ts`

Expected: FAIL because multipart client methods and runner do not exist.

- [ ] **Step 3: Implement foreground resume and queue dispatch**

The runner calls status before any part URL, opens and checks the frozen local
file before each unfinished range, requests one URL, PUTs it and immediately
discards the response object. Use a platform-concurrency semaphore (3 desktop,
2 mobile). URL expiry retries status then one replacement URL. On suspend,
timeout or offline, persist safe progress and throw the existing retryable
closed failure; on local fingerprint change, request abort when online and
terminalize the old event with `multipart_local_content_changed` without
coalescing it with the newer watcher event.

- [ ] **Step 4: Run full plugin checks**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run; pnpm --dir apps/obsidian-plugin run lint; pnpm --dir apps/obsidian-plugin run typecheck; pnpm --dir apps/obsidian-plugin run build`

Expected: PASS with static Mobile boundary tests and no sensitive persistence.

- [ ] **Step 5: Commit client behavior**

```bash
git add apps/obsidian-plugin/src/journal
git commit -m "feat: resume multipart uploads on mobile"
```

## Task 11: Surface closed multipart diagnostics and enforce privacy

**Files:**

- Modify: `src/personal_os/multipart_upload/metrics.py`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts`
- Modify: `apps/obsidian-plugin/src/journal/status.ts`
- Modify: `tests/contract/api/test_openapi_exposure.py`
- Create: `tests/contract/multipart_upload/test_privacy_contract.py`
- Test: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts`

**Interfaces:**

- Consumes: Task 1 closed error/state vocabulary.
- Produces: `multipart_failure` closed trail kind and metrics labels `outcome`, `state`, `platform_class`, `stage`, `error_code` only.

- [ ] **Step 1: Write failing leak and reason-surface tests**

```python
def test_multipart_sensitive_sentinels_are_absent_from_all_safe_surfaces(rendered) -> None:
    for sentinel in SENSITIVE_MULTIPART_SENTINELS:
        assert sentinel not in rendered

def test_cleanup_failure_records_closed_reason(metrics) -> None:
    metrics.record_cleanup_failed(ErrorCode.MULTIPART_CLEANUP_FAILED)
    assert metrics.records[-1].error_code is ErrorCode.MULTIPART_CLEANUP_FAILED
```

- [ ] **Step 2: Run diagnostics/privacy tests and confirm RED**

Run: `uv run pytest tests/contract/multipart_upload/test_privacy_contract.py tests/contract/api/test_openapi_exposure.py -q; pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sync-diagnostics-trail.test.ts`

Expected: FAIL because multipart closed diagnostics and sentinels are absent.

- [ ] **Step 3: Implement closed-only diagnostics**

Add one closed plugin trail kind/stage table and status projection for multipart
resume/verify/cleanup. Wire every catch in server/provider/client paths to one
typed error, trail/status record or structured event. Validate metric-label
sets at runtime/tests and reject any identifier-bearing label. Do not add a
provider ETag, session ID, request ID, path or digest to metrics.

- [ ] **Step 4: Run privacy and diagnostics gates**

Run: `uv run pytest tests/contract/multipart_upload/test_privacy_contract.py tests/contract/api/test_openapi_exposure.py -q; pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sync-diagnostics-trail.test.ts src/journal/status.test.ts`

Expected: PASS with sentinel scans clean and every closed branch readable.

- [ ] **Step 5: Commit observability boundary**

```bash
git add src/personal_os/multipart_upload apps/obsidian-plugin/src/journal tests/contract/multipart_upload tests/contract/api
git commit -m "feat: surface multipart upload diagnostics safely"
```

## Task 12: Prove end-to-end server/R2 behavior and exact cleanup

**Files:**

- Create: `tests/integration/multipart_upload/conftest.py`
- Create: `tests/integration/multipart_upload/test_r2_multipart_journey.py`
- Create: `tests/integration/multipart_upload/test_multipart_races.py`
- Modify: `tests/integration/r2_object_storage/cleanup_manifest.py`
- Modify: `pyproject.toml` only if adding existing-test-suite composition commands; do not add dependencies.

**Interfaces:**

- Consumes: Tasks 3–8 production server composition and Task 6 workflow.
- Produces: disposable integration proof of resume, corruption refusal, lost response recovery and exact R2 cleanup allowlist.

- [ ] **Step 1: Write failing real-R2 journey tests**

```python
async def test_lost_complete_response_replays_one_version_and_cleans_exact_staging(live_harness) -> None:
    await live_harness.upload_all_parts()
    await live_harness.drop_next_complete_response()
    await live_harness.complete_then_replay()
    assert await live_harness.source_version_count() == 1
    assert await live_harness.cleanup_manifest_contains_only_session_resources()

async def test_corrupt_part_cannot_publish(live_harness) -> None:
    await live_harness.upload_corrupt_part()
    await live_harness.complete_expect_integrity_failure()
    assert await live_harness.source_version_count() == 0
```

- [ ] **Step 2: Run integration tests and confirm RED**

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-multipart-int uv run pytest tests/integration/multipart_upload/test_r2_multipart_journey.py tests/integration/multipart_upload/test_multipart_races.py -q`

Expected: FAIL until production composition and R2 journey support exist.

- [ ] **Step 3: Implement only test harness/fixtures needed for the cases**

Use a unique per-run staging identity and append every resource before first
provider mutation to an exact cleanup manifest. Assert cleanup only receives
that manifest's identities. Exercise success, incomplete abort, completed
delete, expiry, failure/retry and cancellation. Do not add a fixture helper
that lists objects or accepts arbitrary keys.

- [ ] **Step 4: Run integration and full relevant suites**

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-multipart-int uv run pytest tests/integration/multipart_upload tests/integration/small_file_sync tests/contract/object_storage tests/contract/api -q`

Expected: PASS with leaked-resource assertion clean.

- [ ] **Step 5: Commit integration proof**

```bash
git add tests/integration/multipart_upload tests/integration/r2_object_storage pyproject.toml
git commit -m "test: cover multipart staging recovery"
```

## Task 13: Document operation and run mandatory live acceptance

**Files:**

- Create: `docs/operations/resumable-multipart-upload.md`
- Create: `apps/obsidian-plugin/test/specs/multipart-upload.e2e.ts`
- Modify: `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- Modify: `docs/07-POSTGRESQL_DATA_MODEL.md`
- Modify: `docs/11-TEMPORAL_WORKFLOWS.md`
- Modify: `docs/12-API_MCP_AND_AGENT_INTEGRATION.md`
- Modify: `docs/14-SECURITY_PRIVACY_AND_POLICY.md`
- Modify: `docs/15-OBSERVABILITY_AND_ALERTING.md`
- Modify: `docs/16-TESTING_AND_EVALUATION.md`
- Modify: `docs/20-IMPLEMENTATION_PLAN.md`
- Test: `tests/contract/multipart_upload/test_operations_document.py`

**Interfaces:**

- Consumes: Tasks 1–12 complete contract and final route names/reason tokens.
- Produces: living recovery runbook, Desktop WDIO journey and sanitized physical-Mobile evidence procedure.

- [ ] **Step 1: Write failing documentation and WDIO contract tests**

```python
def test_operations_runbook_names_exact_cleanup_and_mobile_gate() -> None:
    document = read_text("docs/operations/resumable-multipart-upload.md")
    assert "exact staging key" in document
    assert "physical Mobile" in document
    assert "prefix delete" not in document
```

- [ ] **Step 2: Run document/WDIO static tests and confirm RED**

Run: `uv run pytest tests/contract/multipart_upload/test_operations_document.py -q; pnpm --dir apps/obsidian-plugin exec vitest run test/specs/multipart-upload.e2e.ts`

Expected: FAIL because the runbook and WDIO spec are absent.

- [ ] **Step 3: Write runbook and implement acceptance journey**

Document safe resume, expiry, local-content-change, cleanup-failure and
re-auth recovery using closed tokens only. The WDIO journey runs a >16 MiB
sanitized fixture through interruption/resume, one corruption refusal, policy
advance and lost completion acknowledgement. Record physical Mobile evidence
in the same sanitized format and prove the two-part cap through diagnostics,
never raw transfer data.

- [ ] **Step 4: Run final offline and live gates**

Run offline: `uv run poe verify; pnpm --dir apps/obsidian-plugin exec vitest run; git diff --check`

Run live: `CI=true bash .local/serve-live-ci.sh up knowledge-ci-multipart-live`, then run the guarded Desktop WDIO command and the physical Mobile matrix against that same disposable project; finally run `bash .local/serve-live-ci.sh down`.

Expected: every offline gate passes; Desktop WDIO and every physical Mobile row are PASS. If an external prerequisite prevents a live run, report the exact blocked gate and do not claim Child 7 complete.

- [ ] **Step 5: Commit canonical docs and acceptance artifacts**

```bash
git add docs apps/obsidian-plugin/test/specs tests/contract/multipart_upload
git commit -m "docs: operate resumable multipart uploads"
```

## Task 14: Final review, handoff and deferred-work adjudication

**Files:**

- Create: `docs/handoff/2026-08-28-resumable-multipart-mobile-upload.md`
- Modify: `docs/handoff/BACKLOG.md` only for a genuinely deferred item with one exact trigger.
- Test: no new test file; run final evidence commands from Task 13.

**Interfaces:**

- Consumes: all landed tasks and current `BACKLOG.md`.
- Produces: exactly one final Child 7 handoff with commit SHA, evidence, decisions, deferred verdicts and next actions.

- [ ] **Step 1: Review the full diff and inspect every closed error path**

Run: `git diff --check; rg -n "except |catch \(" src/personal_os/multipart_upload packages/r2-object-storage/src/r2_object_storage/multipart.py apps/api/src/api_runtime/multipart_upload* apps/obsidian-plugin/src/journal/multipart-upload.ts`

Expected: every new catch maps to a typed error, safe state/trail or structured closed event; no raw provider/content field reaches a forbidden surface.

- [ ] **Step 2: Run the final required command set on the final commit**

Run: `uv run poe verify; uv run poe api-contract-check; CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-multipart-final uv run pytest tests/integration/multipart_upload -q; pnpm --dir apps/obsidian-plugin exec vitest run; pnpm --dir apps/obsidian-plugin run lint; pnpm --dir apps/obsidian-plugin run typecheck; pnpm --dir apps/obsidian-plugin run build; git diff --check`

Expected: every command exits 0. Keep exact command/result evidence for the handoff.

- [ ] **Step 3: Write the single required handoff**

Include final SHA, each offline/live gate with commands/results, the decisions
to keep server-owned provider metadata and exact-only cleanup, any failed live
prerequisite with evidence, and Child 8/9 boundaries. Keep the handoff under
400 lines and link rather than duplicate living operations text.

- [ ] **Step 4: Adjudicate deferred work exactly once**

Remove completed rows only when final evidence exists. For every deferred item,
write one `BACKLOG.md` row with date/domain/one-line ruling/details link and
an `Implement by` value such as `Before Child 8 conflict merge`, `Before
production activation`, or `At next <dependency> pin bump`; do not create an
unbounded deferral.

- [ ] **Step 5: Commit the handoff after all gates are evidenced**

```bash
git add docs/handoff
git commit -m "docs: hand off multipart upload child"
```

## Plan self-review

**Spec coverage:** Tasks 1–5 cover geometry, ownership, resume, policy, full
verification and CAS promotion; Tasks 2–3 cover canonical persistence and
leases; Task 4 is the only provider-SDK boundary; Task 6 owns Temporal expiry;
Tasks 7–8 own API/OpenAPI; Tasks 9–10 own SQLite/Mobile; Task 11 owns every
new diagnostic surface; Tasks 12–13 own integration/privacy/live acceptance;
Task 14 owns final evidence and handoff. No Child 8 conflict-candidate or
general-GC work is included.

**Completeness review:** The plan contains no unresolved design decision. The one
dynamic migration input is deliberately a command (`alembic heads`) that reads
the repository's live head before the implementer writes `down_revision`; this
prevents an unsafe guessed migration chain.

**Type consistency:** Public values use `MultipartUploadSessionId`/
`MultipartUploadPlan`; persistence uses `MultipartSessionStore`; orchestration
uses `MultipartUploadApplicationService`; plugin work uses
`MultipartProgressRecord` and `MultipartUploadRunner`. Provider IDs/ETags are
confined to private store/provider interfaces and never cross API/plugin types.
