# Source Conflict Capture and Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Child 8’s server-authoritative conflict capture, verified candidate retention, Obsidian Conflict Inbox, explicit resolution, and race-safe two-device behavior.

**Architecture:** A new framework-neutral `source_conflicts` domain owns immutable evidence and transitions; the PostgreSQL adapter atomically captures/replays/resolves conflicts beside existing source publication and lifecycle state. Small-file and lifecycle services delegate to this domain. The API is a device-authenticated adapter, while the Obsidian plugin owns only inbox UI, bounded local merge, journal repair state, and atomic application of the canonical winner.

**Tech Stack:** Python 3.13, SQLAlchemy async, Alembic, FastAPI/Pydantic/OpenAPI, PostgreSQL, Cloudflare R2 verified-read/object contracts, TypeScript strict, Obsidian API, sql.js, Vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-source-conflict-capture-and-resolution-design.md`

## Global Constraints

- PostgreSQL is canonical state and R2 holds immutable canonical bytes; neither the plugin journal nor a projection can become a source of truth.
- Domain modules must not import FastAPI, SQLAlchemy, database drivers, or provider SDKs.
- Candidate bytes must complete the existing verified-object contract before a conflict can reference them; raw bytes never enter a resolve command.
- Capture does not change the source current pointer; exact capture replay returns the original conflict; resolution has a new event identity.
- Every resolve transaction rechecks the reviewed remote version and policy. A stale resolution makes an immutable predecessor plus open successor; it never edits evidence in place.
- Text/Markdown merge is bounded, editable and explicit. Binary permits only keep-remote/keep-local; no automatic merge or last-write-wins.
- Server commit precedes Vault apply. A Vault failure records durable `local_apply_pending` with closed diagnostic tokens and retries local application only.
- Do not log or emit paths, bytes, diff/merge text, full digests, tokens, R2 keys, URLs, or provider details. Closed error paths surface a safe reason token.
- Use a new Alembic revision after `20260901_03`; add both upgrade and downgrade tests. Update `src/personal_os/database_schema.py` in the same task.
- Start all implementation behavior with failing tests. Do not add a production dependency unless a task explicitly changes this plan and spec first.

---

## File Structure

| Area | Files | Responsibility |
|---|---|---|
| Conflict domain | `src/personal_os/source_conflicts/{contracts,commands,errors,metrics,ports,service}.py` | Closed vocabulary, input/output types, policy/read/publication ports and pure orchestration. |
| PostgreSQL persistence | `migrations/versions/20260902_01_add_source_conflicts.py`, `src/postgresql_source_store/conflict_store.py` | Schema, immutable evidence, advisory-lock transactions, replay and resolution commits. |
| Existing domain adapters | `small_file_sync/*`, `source_lifecycle/*` | Turn stale/race outcomes into conflict commands; preserve existing non-conflict results. |
| HTTP adapter | `apps/api/src/api_runtime/{source_conflict_models,source_conflict_routes,source_conflict_composition}.py` | Strict device wire contract, auth, error mapping and composed service. |
| Plugin transport/state | `apps/obsidian-plugin/src/conflicts/{contracts,api,repository,merge,controller}.ts` and journal schema/persistence files | No-byte journal facts, safe API client, merge and repair controller. |
| Plugin UI/composition | `apps/obsidian-plugin/src/conflicts/ConflictInboxModal.ts`, `apps/obsidian-plugin/src/plugin.ts` | Explicit user interaction and composition; no domain logic in `plugin.ts`. |
| Tests and operations | focused unit/contract/integration/plugin/E2E tests plus `docs/operations/` | TDD evidence, privacy checks, diagnostics runbook and live Desktop gate. |

### Task 1: Establish closed conflict domain contracts and diagnostics

**Files:**
- Create: `src/personal_os/source_conflicts/contracts.py`
- Create: `src/personal_os/source_conflicts/commands.py`
- Create: `src/personal_os/source_conflicts/errors.py`
- Create: `src/personal_os/source_conflicts/metrics.py`
- Create: `src/personal_os/source_conflicts/ports.py`
- Create: `src/personal_os/source_conflicts/__init__.py`
- Test: `tests/unit/source_conflicts/test_contracts.py`
- Test: `tests/unit/source_conflicts/test_errors.py`
- Test: `tests/unit/source_conflicts/test_metrics.py`

**Interfaces:**
- Produces `ConflictKind`, `ConflictStatus`, `ConflictResolutionKind`, `ConflictCandidate`, `CaptureConflictCommand`, `ResolveConflictCommand`, `SourceConflict`, and `SourceConflictError`.
- Produces `SourceConflictStore`, `SourceConflictPolicyGuard`, `ConflictEvidenceReader`, and `SourceConflictMetrics` protocols consumed by Tasks 2–6.

- [ ] **Step 1: Write failing value-object and privacy tests.**

```python
def test_content_conflict_requires_verified_candidate_object() -> None:
    with pytest.raises(ValueError, match="verified_candidate_object_id"):
        ConflictCandidate.content(None)

def test_delete_conflict_refuses_content_object() -> None:
    with pytest.raises(ValueError, match="delete candidate"):
        ConflictCandidate.delete(verified_candidate_object_id=uuid4())
```

- [ ] **Step 2: Run the focused tests and verify import failure.**

Run: `uv run pytest tests/unit/source_conflicts/test_contracts.py tests/unit/source_conflicts/test_errors.py tests/unit/source_conflicts/test_metrics.py -q`

Expected: FAIL because `personal_os.source_conflicts` does not yet exist.

- [ ] **Step 3: Implement the closed vocabulary and protocol boundary.**

```python
class ConflictKind(StrEnum):
    STALE_CONTENT = "stale_content"
    EDIT_REMOTE_DELETE = "edit_remote_delete"
    DELETE_REMOTE_EDIT = "delete_remote_edit"
    LOCATOR_COLLISION = "locator_collision"

@dataclass(frozen=True, slots=True)
class ResolveConflictCommand:
    conflict_id: UUID
    reviewed_remote_version_id: UUID | None
    resolution_kind: ConflictResolutionKind
    resolution_event_id: UUID
    idempotency_key: IdempotencyKey
    verified_candidate_object_id: UUID | None
```

Make every type validate the permitted candidate/kind and resolution/kind combinations in `__post_init__`. Use `ApplicationError` plus registered closed `source_conflict_*` codes; metrics may accept only enum labels.

- [ ] **Step 4: Run focused tests and strict type check.**

Run: `uv run pytest tests/unit/source_conflicts -q; uv run mypy src/personal_os/source_conflicts`

Expected: PASS.

- [ ] **Step 5: Commit the domain contract.**

```bash
git add src/personal_os/source_conflicts tests/unit/source_conflicts
git commit -m "feat: define source conflict contracts"
```

### Task 2: Add canonical conflict schema and PostgreSQL store

**Files:**
- Create: `migrations/versions/20260902_01_add_source_conflicts.py`
- Modify: `src/personal_os/database_schema.py`
- Create: `src/postgresql_source_store/conflict_store.py`
- Modify: `src/postgresql_source_store/__init__.py`
- Test: `tests/unit/migrations/test_source_conflicts_migration.py`
- Test: `tests/unit/postgresql_source_store/test_conflict_store.py`
- Test: `tests/integration/source_conflicts/test_conflict_store_transactions.py`

**Interfaces:**
- Consumes Task 1 commands and `SourceConflictStore` protocol.
- Produces `PostgresqlSourceConflictStore(engine, clock)` implementing atomic `capture()`, `list_open()`, `read()`, `resolve()` and replay lookup.

- [ ] **Step 1: Write migration and store tests first.**

```python
async def test_capture_inserts_one_event_conflict_and_candidate_reference_without_current_pointer_change(...) -> None:
    before = await harness.current_version_id(source_id)
    conflict = await harness.store.capture(command)
    assert conflict.status is ConflictStatus.OPEN
    assert await harness.current_version_id(source_id) == before
    assert await harness.count_conflicts() == 1

async def test_same_capture_idempotency_key_returns_original_conflict(...) -> None:
    assert await harness.store.capture(command) == await harness.store.capture(command)
```

- [ ] **Step 2: Run migration/store tests to establish failure.**

Run: `uv run pytest tests/unit/migrations/test_source_conflicts_migration.py tests/unit/postgresql_source_store/test_conflict_store.py tests/integration/source_conflicts/test_conflict_store_transactions.py -q`

Expected: FAIL because the revision and store do not exist.

- [ ] **Step 3: Implement the Alembic revision and adapter.**

Create `knowledge.source_conflicts` with UUID primary key, workspace/source/event/device/version/content-object foreign keys, closed `conflict_kind`/`status`/resolution checks, `successor_conflict_id`, timestamps, and indexes for workspace-open listing, source history and originating-event replay. Use `ON DELETE RESTRICT` for evidence references. Downgrade must reject non-empty conflict state before dropping the table. Advance `CANONICAL_POSTGRESQL_SCHEMA_REVISION`.

```python
class PostgresqlSourceConflictStore(SourceConflictStore):
    async def capture(self, command: CaptureConflictCommand, context: DiagnosticContext) -> SourceConflict: ...
    async def resolve(self, command: ResolveConflictCommand, context: DiagnosticContext) -> ConflictResolutionResult: ...
```

Acquire the existing idempotency lock before source/locator locks. In one transaction, write the accepted `sync_events` row, evidence row and audit. On resolve, lock conflict and source/locator in deterministic order, replay by resolution event identity, recheck policy/current state, then either commit winner or make predecessor `superseded` plus successor.

- [ ] **Step 4: Verify migration and transaction behavior.**

Run: `uv run pytest tests/unit/migrations/test_source_conflicts_migration.py tests/unit/postgresql_source_store/test_conflict_store.py tests/integration/source_conflicts/test_conflict_store_transactions.py -q`

Expected: PASS, including upgrade/downgrade, replay and current-pointer invariants.

- [ ] **Step 5: Commit persistence.**

```bash
git add migrations/versions/20260902_01_add_source_conflicts.py src/personal_os/database_schema.py src/postgresql_source_store tests/unit/migrations tests/unit/postgresql_source_store tests/integration/source_conflicts
git commit -m "feat: persist source conflict evidence"
```

### Task 3: Implement capture and resolution service orchestration

**Files:**
- Create: `src/personal_os/source_conflicts/service.py`
- Test: `tests/unit/source_conflicts/test_service.py`
- Test: `tests/unit/source_conflicts/fakes.py`

**Interfaces:**
- Consumes Task 1 ports and Task 2 store.
- Produces `SourceConflictService.capture_conflict()` and `SourceConflictService.resolve_conflict()` for existing domains and HTTP composition.

- [ ] **Step 1: Write failing service transition tests.**

```python
async def test_stale_resolution_keeps_predecessor_immutable_and_opens_successor() -> None:
    result = await service.resolve_conflict(stale_resolution)
    assert result.kind is ConflictResolutionOutcome.STALE_SUCCESSOR
    assert result.successor.observed_remote_version_id == newer_version_id

async def test_keep_remote_records_no_publication_command() -> None:
    await service.resolve_conflict(keep_remote)
    assert publication_gateway.commands == []
```

- [ ] **Step 2: Run service tests and verify failure.**

Run: `uv run pytest tests/unit/source_conflicts/test_service.py -q`

Expected: FAIL because the service is absent.

- [ ] **Step 3: Implement policy/current recheck orchestration.**

```python
async def resolve_conflict(
    self, command: ResolveConflictCommand, diagnostic_context: DiagnosticContext
) -> ConflictResolutionResult:
    conflict = await self.store.read_for_resolution(command.conflict_id, diagnostic_context)
    await self.policy_guard.authorize_resolution(conflict, diagnostic_context)
    return await self.store.resolve(command, diagnostic_context)
```

Keep verified-object admission outside the service: it receives only references. Record exactly one closed metric/diagnostic outcome per completed or rejected branch; propagate typed errors unchanged.

- [ ] **Step 4: Run focused tests.**

Run: `uv run pytest tests/unit/source_conflicts/test_service.py tests/unit/source_conflicts/test_metrics.py -q`

Expected: PASS.

- [ ] **Step 5: Commit service orchestration.**

```bash
git add src/personal_os/source_conflicts/service.py tests/unit/source_conflicts
git commit -m "feat: orchestrate source conflict resolution"
```

### Task 4: Route small-file stale outcomes into conflict capture

**Files:**
- Modify: `src/personal_os/small_file_sync/contracts.py`
- Modify: `src/personal_os/small_file_sync/ports.py`
- Modify: `src/personal_os/small_file_sync/service.py`
- Modify: `apps/api/src/api_runtime/small_file_sync_composition.py`
- Test: `tests/unit/small_file_sync/test_service.py`
- Test: `tests/integration/small_file_sync/test_wire_journey.py`

**Interfaces:**
- Consumes `SourceConflictService.capture_conflict()` from Task 3.
- Produces a conflict preflight outcome that contains only an opaque conflict ID and never an upload receipt or raw content.

- [ ] **Step 1: Add failing stale-update capture tests.**

```python
async def test_stale_verified_update_is_captured_and_replay_returns_same_conflict() -> None:
    first = await harness.receive_stale_update()
    replay = await harness.replay_same_event()
    assert first.conflict_id == replay.conflict_id
    assert harness.publication_count == 0
```

- [ ] **Step 2: Run targeted tests.**

Run: `uv run pytest tests/unit/small_file_sync/test_service.py tests/integration/small_file_sync/test_wire_journey.py -q`

Expected: FAIL because stale updates only park as `blocked_conflict` today.

- [ ] **Step 3: Add the conflict-capture port and invoke it after verified candidate admission.**

Do not change the preflight policy/base check. For a stale base, reserve/verify candidate bytes through the existing small-file or multipart receive path, then issue `CaptureConflictCommand` in place of publication. Map replay to the same opaque ID and preserve local journal evidence.

- [ ] **Step 4: Run small-file tests.**

Run: `uv run pytest tests/unit/small_file_sync tests/integration/small_file_sync -q`

Expected: PASS.

- [ ] **Step 5: Commit the small-file bridge.**

```bash
git add src/personal_os/small_file_sync apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/small_file_sync tests/integration/small_file_sync
git commit -m "feat: capture stale small file conflicts"
```

### Task 5: Route lifecycle races into the shared conflict service

**Files:**
- Modify: `src/personal_os/source_lifecycle/commands.py`
- Modify: `src/personal_os/source_lifecycle/ports.py`
- Modify: `src/personal_os/source_lifecycle/service.py`
- Modify: `apps/api/src/api_runtime/source_lifecycle_composition.py`
- Test: `tests/unit/source_lifecycle/test_service.py`
- Test: `tests/integration/source_lifecycle/test_lifecycle_concurrency.py`
- Test: `tests/integration/source_conflicts/test_lifecycle_conflicts.py`

**Interfaces:**
- Consumes Task 3 `CaptureConflictCommand` variants `EDIT_REMOTE_DELETE`, `DELETE_REMOTE_EDIT`, and `LOCATOR_COLLISION`.
- Produces deterministic lifecycle conflict receipts without a current-pointer mutation.

- [ ] **Step 1: Write failing lifecycle-race tests.**

```python
async def test_delete_against_remote_edit_creates_no_byte_conflict() -> None:
    receipt = await harness.delete_after_remote_update()
    assert receipt.conflict_kind is ConflictKind.DELETE_REMOTE_EDIT
    assert receipt.verified_candidate_object_id is None

async def test_locator_collision_preserves_locator_snapshot_without_rebinding() -> None:
    receipt = await harness.concurrent_rename()
    assert receipt.conflict_kind is ConflictKind.LOCATOR_COLLISION
```

- [ ] **Step 2: Run lifecycle tests and verify failure.**

Run: `uv run pytest tests/unit/source_lifecycle/test_service.py tests/integration/source_lifecycle/test_lifecycle_concurrency.py tests/integration/source_conflicts/test_lifecycle_conflicts.py -q`

Expected: FAIL because lifecycle collision has no durable conflict receipt.

- [ ] **Step 3: Delegate collision branches, preserving existing locks and audit.**

Replace only branches that cannot commit due to a competing canonical lifecycle transition. Pass the lifecycle command fingerprint, current locator snapshot, base/observed remote versions and deletion/content candidate shape to the shared service. Do not reimplement candidate retention or conflict state in lifecycle tables.

- [ ] **Step 4: Re-run lifecycle and conflict integration tests.**

Run: `uv run pytest tests/unit/source_lifecycle tests/integration/source_lifecycle tests/integration/source_conflicts/test_lifecycle_conflicts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the lifecycle bridge.**

```bash
git add src/personal_os/source_lifecycle apps/api/src/api_runtime/source_lifecycle_composition.py tests/unit/source_lifecycle tests/integration/source_lifecycle tests/integration/source_conflicts
git commit -m "feat: capture lifecycle source conflicts"
```

### Task 6: Expose strict device Conflict API and generated client contract

**Files:**
- Create: `apps/api/src/api_runtime/source_conflict_models.py`
- Create: `apps/api/src/api_runtime/source_conflict_routes.py`
- Create: `apps/api/src/api_runtime/source_conflict_composition.py`
- Modify: `apps/api/src/api_runtime/server.py`
- Modify: `apps/api/src/api_runtime/openapi_export.py`
- Test: `tests/unit/api_runtime/test_source_conflict_models.py`
- Test: `tests/unit/api_runtime/test_source_conflict_routes.py`
- Test: `tests/unit/api_runtime/test_source_conflict_composition.py`
- Test: `tests/contract/api/test_source_conflict_openapi.py`
- Test: `tests/contract/api/test_source_conflict_routes.py`

**Interfaces:**
- Consumes Task 3 service and Task 2 evidence store.
- Produces `GET /api/sync/conflicts`, `GET /api/sync/conflicts/{conflict_id}`, `GET /api/sync/conflicts/{conflict_id}/evidence/{role}`, and `POST /api/sync/conflicts/{conflict_id}/resolve`.

- [ ] **Step 1: Write failing route/auth/privacy tests.**

```python
def test_resolve_requires_device_credential_and_rejects_raw_merged_bytes(client: TestClient) -> None:
    response = client.post("/api/sync/conflicts/" + str(CONFLICT_ID) + "/resolve", json={"raw": "secret"})
    assert response.status_code in {401, 422}

def test_evidence_download_rechecks_policy_before_opening_reader(...) -> None:
    assert guarded_reader.open_count == 0
```

- [ ] **Step 2: Run focused API tests.**

Run: `uv run pytest tests/unit/api_runtime/test_source_conflict_models.py tests/unit/api_runtime/test_source_conflict_routes.py tests/contract/api/test_source_conflict_openapi.py tests/contract/api/test_source_conflict_routes.py -q`

Expected: FAIL because routes and models are absent.

- [ ] **Step 3: Implement models, routes and composition.**

Use canonical envelope/error mapping and `require_access_credential`. Validate UUIDs, closed role enum, closed resolution enum, reviewed remote version and optional verified object UUID. Stream evidence from the existing verified-read adapter only after domain policy authorization. Register routes in the composed server and regenerate the OpenAPI snapshot/client according to repository commands.

- [ ] **Step 4: Run API contract gates.**

Run: `uv run pytest tests/unit/api_runtime/test_source_conflict_models.py tests/unit/api_runtime/test_source_conflict_routes.py tests/unit/api_runtime/test_source_conflict_composition.py tests/contract/api/test_source_conflict_openapi.py tests/contract/api/test_source_conflict_routes.py -q; uv run poe api-contract-check`

Expected: PASS.

- [ ] **Step 5: Commit HTTP contract.**

```bash
git add apps/api/src/api_runtime tests/unit/api_runtime tests/contract/api apps/web/src/api
git commit -m "feat: expose source conflict API"
```

### Task 7: Add plugin conflict wire client and durable no-byte repair state

**Files:**
- Create: `apps/obsidian-plugin/src/conflicts/contracts.ts`
- Create: `apps/obsidian-plugin/src/conflicts/api.ts`
- Create: `apps/obsidian-plugin/src/conflicts/repository.ts`
- Modify: `apps/obsidian-plugin/src/journal/sqlite-database.ts`
- Modify: `apps/obsidian-plugin/src/journal/persistence.ts`
- Test: `apps/obsidian-plugin/src/conflicts/contracts.test.ts`
- Test: `apps/obsidian-plugin/src/conflicts/api.test.ts`
- Test: `apps/obsidian-plugin/src/conflicts/repository.test.ts`
- Test: `apps/obsidian-plugin/src/journal/sqlite-database.test.ts`
- Test: `apps/obsidian-plugin/src/journal/persistence.test.ts`

**Interfaces:**
- Consumes Task 6 JSON contract through the existing authenticated request transport.
- Produces `ConflictApi`, `ConflictRepository`, `PendingLocalApply`, and journal schema v9 migration.

- [ ] **Step 1: Write failing schema/client tests.**

```typescript
it("migrates v8 journal without storing candidate bytes or paths", () => {
  const database = openV8Fixture();
  migrateJournal(database);
  expect(database.readSchemaVersion()).toBe(9);
  expect(database.readAll("select * from conflict_local_repairs;")[0]?.columns).not.toContain("bytes");
});
```

- [ ] **Step 2: Run plugin focused tests.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/conflicts/contracts.test.ts src/conflicts/api.test.ts src/conflicts/repository.test.ts src/journal/sqlite-database.test.ts src/journal/persistence.test.ts`

Expected: FAIL because conflict modules and migration v9 are absent.

- [ ] **Step 3: Implement safe wire and journal records.**

Define strict decoded response types; reject unknown enum values and raw response details. Add only conflict UUID, resolution event identity, target action, safe reason and retry bookkeeping to `conflict_local_repairs`; do not store evidence or draft bytes. Add v8→v9 schema migration and persistence acceptance like earlier journal migrations.

- [ ] **Step 4: Run migration and client tests.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/conflicts src/journal/sqlite-database.test.ts src/journal/persistence.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit plugin state and transport.**

```bash
git add apps/obsidian-plugin/src/conflicts apps/obsidian-plugin/src/journal
git commit -m "feat: add conflict plugin state"
```

### Task 8: Implement bounded merge and explicit Conflict Inbox controller

**Files:**
- Create: `apps/obsidian-plugin/src/conflicts/merge.ts`
- Create: `apps/obsidian-plugin/src/conflicts/controller.ts`
- Create: `apps/obsidian-plugin/src/conflicts/ConflictInboxModal.ts`
- Test: `apps/obsidian-plugin/src/conflicts/merge.test.ts`
- Test: `apps/obsidian-plugin/src/conflicts/controller.test.ts`
- Test: `apps/obsidian-plugin/src/conflicts/ConflictInboxModal.test.ts`

**Interfaces:**
- Consumes Task 7 `ConflictApi`/repository and existing `AtomicVaultWriterImpl` echo-suppression seam.
- Produces `createConflictController()`, `computeBoundedThreeWayMerge()`, and an explicit modal with allowed choices by conflict kind/media type.

- [ ] **Step 1: Write failing merge/controller tests.**

```typescript
it("never auto-resolves conflicting text hunks", () => {
  const merge = computeBoundedThreeWayMerge(base, remote, local);
  expect(merge.requiresUserReview).toBe(true);
});

it("parks a canonical winner as local_apply_pending when atomic Vault write fails", async () => {
  await expect(controller.resolveKeepRemote(conflictId)).resolves.toEqual({ kind: "local_apply_pending" });
});
```

- [ ] **Step 2: Run focused plugin tests.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/conflicts/merge.test.ts src/conflicts/controller.test.ts src/conflicts/ConflictInboxModal.test.ts`

Expected: FAIL because the merge/controller/modal are absent.

- [ ] **Step 3: Implement bounded, user-mediated behavior.**

Set explicit byte and line limits in `merge.ts`; an exceeded bound renders a safe “manual choice required” state and never loads a partial merge. Decode text only for supported text/Markdown media types. The controller fetches evidence on demand, uploads an edited merge through existing verified upload APIs, posts resolve, then invokes atomic Vault apply. Binary UI cannot render a merge editor or `save_merged` action.

- [ ] **Step 4: Run conflict UI tests.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/conflicts`

Expected: PASS, including no raw content in diagnostics fixtures.

- [ ] **Step 5: Commit Conflict Inbox behavior.**

```bash
git add apps/obsidian-plugin/src/conflicts
git commit -m "feat: add conflict inbox resolution"
```

### Task 9: Compose inbox, recovery, status and diagnostics in the plugin

**Files:**
- Modify: `apps/obsidian-plugin/src/plugin.ts`
- Modify: `apps/obsidian-plugin/src/journal/status.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-export.ts`
- Test: `apps/obsidian-plugin/src/plugin.test.ts`
- Test: `apps/obsidian-plugin/src/journal/status.test.ts`
- Test: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts`

**Interfaces:**
- Consumes Tasks 7–8 controller/repository.
- Produces a command to open the Conflict Inbox, a readable pending-apply status, and closed reason tokens for all newly swallowed local paths.

- [ ] **Step 1: Write failing composition/status tests.**

```typescript
it("opens the Conflict Inbox only through an explicit plugin command", async () => {
  await plugin.onload();
  expect(plugin.registeredCommandIds()).toContain("open-conflict-inbox");
});

it("surfaces local_apply_pending with a closed token and no locator", () => {
  expect(renderJournalSyncStatus(snapshot)).toContain("Conflict apply pending");
});
```

- [ ] **Step 2: Run focused tests.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/plugin.test.ts src/journal/status.test.ts src/journal/sync-diagnostics-trail.test.ts`

Expected: FAIL because no conflict composition/status exists.

- [ ] **Step 3: Wire only adapters in `plugin.ts`.**

Construct the API with the existing device credential transport, conflict repository, bounded controller and atomic writer. Register the inbox command and use the existing trail/status refresh facilities. On startup/foreground sync trigger, retry persisted local applies only; do not poll conflicts or run a background merge loop.

- [ ] **Step 4: Run plugin type/lint and relevant tests.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/plugin.test.ts src/journal/status.test.ts src/journal/sync-diagnostics-trail.test.ts; pnpm --dir apps/obsidian-plugin run type-check; pnpm --dir apps/obsidian-plugin run lint`

Expected: PASS.

- [ ] **Step 5: Commit composition.**

```bash
git add apps/obsidian-plugin/src/plugin.ts apps/obsidian-plugin/src/journal
git commit -m "feat: compose conflict inbox recovery"
```

### Task 10: Prove races, privacy, operation documentation and final gates

**Files:**
- Create: `tests/integration/source_conflicts/test_resolution_races.py`
- Create: `tests/contract/source_conflicts/test_privacy_contract.py`
- Create: `tests/contract/source_conflicts/test_table_metadata.py`
- Create: `apps/obsidian-plugin/test/specs/source-conflict-resolution.e2e.ts`
- Modify: `docs/operations/source-locator-tombstone-lifecycle.md`
- Create: `docs/operations/source-conflict-resolution.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes all earlier task contracts.
- Produces end-to-end evidence that concurrent devices never silently overwrite and an operation runbook for Inbox/local-apply repair diagnostics.

- [ ] **Step 1: Write red integration, privacy and E2E cases.**

```python
async def test_two_resolvers_racing_for_one_conflict_create_at_most_one_winning_version(...) -> None:
    results = await asyncio.gather(resolve_local(), resolve_merged(), return_exceptions=True)
    assert await harness.published_version_count_after_capture() == 1
    assert any(result.kind is ConflictResolutionOutcome.STALE_SUCCESSOR for result in results)
```

```typescript
it("shows binary choices without a merge editor and keeps the losing candidate retained", async () => {
  await journey.openBinaryConflict();
  expect(journey.visibleChoices()).toEqual(["keep remote", "keep local"]);
});
```

- [ ] **Step 2: Run new tests and verify they fail against missing coverage/behavior.**

Run: `uv run pytest tests/integration/source_conflicts/test_resolution_races.py tests/contract/source_conflicts -q; pnpm --dir apps/obsidian-plugin exec vitest run test/specs/source-conflict-resolution.e2e.ts`

Expected: FAIL until Tasks 1–9 behavior is complete.

- [ ] **Step 3: Complete only fixes exposed by these tests and write the runbook.**

Cover concurrent capture, two resolve requests, capture-versus-resolve, remote advance, policy advance, text/binary/lifecycle two-device journeys and leakage scan fixtures. The runbook documents safe user actions, `local_apply_pending` reason-token readback, Desktop live steps and the existing Child 9 Mobile gate; it must contain no sensitive content or credentials.

- [ ] **Step 4: Run full required verification.**

Run:

```bash
uv run poe verify
uv run poe api-contract-check
CI=true bash .local/serve-live-ci.sh up knowledge-ci-source-conflicts-20260902
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-source-conflicts-20260902 uv run poe device-sync-test
pnpm --dir apps/obsidian-plugin exec vitest run
pnpm --dir apps/obsidian-plugin run type-check
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
```

Then run the required Desktop WDIO Conflict Inbox journey using the local bootstrap/runbook contracts; tear down with `bash .local/serve-live-ci.sh down`. The physical Mobile matrix is not marked passed here; preserve its existing Child 9 backlog gate.

Expected: all non-mobile gates PASS, the Desktop journey records sanitized success evidence, and teardown leaves `knowledge-local` down.

- [ ] **Step 5: Commit final tests and operations docs.**

```bash
git add tests/integration/source_conflicts tests/contract/source_conflicts apps/obsidian-plugin/test/specs docs/operations docs/README.md
git commit -m "test: verify source conflict resolution"
```

## Plan Self-Review

- Spec coverage: Tasks 1–3 implement the common aggregate, immutable evidence, verified candidates, replay, successor and policy/current rechecks. Tasks 4–5 integrate stale and lifecycle capture. Task 6 covers device API and verified reads. Tasks 7–9 cover no-byte local state, bounded merge, binary choices, canonical-first apply, diagnostics and UI. Task 10 covers races, privacy, operations and required gates.
- Scope: candidate GC, Web conflict UI, cursor-gap remediation and Mobile acceptance remain explicitly excluded; no task silently absorbs them.
- Type consistency: `CaptureConflictCommand` and `ResolveConflictCommand` originate in Task 1 and are used by Tasks 2–6. `SourceConflictService` originates in Task 3 and is the only shared domain surface for Tasks 4–6. `ConflictApi`/`ConflictRepository` originate in Task 7 and are consumed by Tasks 8–9.
- Placeholder scan: no deferred implementation placeholder is present; exact routes, module names, commands, test behavior and gate commands are stated above.
