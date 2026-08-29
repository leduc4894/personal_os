# Device-Sync Child-8 Unblock and Smoke-Round Prep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the five deferred BACKLOG rows that block the Child 8 conflict merge (unowned-upload `EXCLUDED` durable fix, device-sync review minors, mobile rebuild reconcile-first) and prepare the closed-reason live smoke round (per-worker diagnostics wiring, Web Admin rendering of `worker_stale_running` and lifecycle rejections).

**Architecture:** Three server-side changes land in the `device_sync`/API runtime (append-time policy decision persisted in a new nullable column; honest mid-stream outcome classification; deterministic generator `aclose`), two plugin changes land in `apps/obsidian-plugin` (rebuild reconcile-first decided by vault content; per-action download reuse + outbound-conflict barrier parity), and two smoke-prep changes land outside git (`.local/run-worker.sh`) and in `apps/web` (Web Admin surfaces). No wire/OpenAPI contract changes; one Alembic migration.

**Tech Stack:** Python 3.14 (mypy strict, ruff, pytest, SQLAlchemy async, Alembic), TypeScript strict (Vitest, React 19/Next.js App Router for `apps/web`, Obsidian plugin vitest).

## Global Constraints

- `uv run poe verify` (format-check, lint, type-check, boundary-check, test, build) and `uv run poe api-contract-check` must exit 0 at every commit.
- Plugin gates: `pnpm --dir apps/obsidian-plugin exec vitest run`, `... run type-check`, `... run lint`, `... run build` — all exit 0.
- No new production dependencies. No API/wire contract change (the migration is internal; OpenAPI snapshot must stay byte-identical).
- The raw locator never persists: the append-time decision is stored as a boolean, never the locator text.
- Every schema change needs an Alembic migration with upgrade/downgrade tests, a `CANONICAL_POSTGRESQL_SCHEMA_REVISION` bump, and both test pins updated (`tests/contract/test_authentication_migration_contract.py:754`, `tests/unit/recovery/test_contracts.py:112`).
- Integration tests run ONLY on a disposable `knowledge-ci-*` project: `CI=true bash .local/serve-live-ci.sh up knowledge-ci-<slug>` before, `bash .local/serve-live-ci.sh down` after. Never touch `knowledge-local` data.
- No completion claim for any live/mobile behavior: the rebuild fix is verified by offline suite evidence only; physical mobile re-verification is NOT in this plan.
- `.local/` is untracked machine-local state (Task 8 edits it; it is never committed).
- Commits are small and conventional (`fix:`, `test:`, `docs:`), one logical change each.
- BACKLOG rows retired by this plan: `docs/handoff/BACKLOG.md` lines 64, 65, 66 (device-sync / device-sync-recovery, gate "Before Child 8 conflict merge") and lines 54, 55 (policy-workers, web-admin, smoke-round prep gates).

---

### Task 1: Device-sync contract and route-test hygiene batch

The four smallest review minors (BACKLOG line 65): duplicate `__all__` entry, dead fakes helpers, unbounded `ManifestAction.local_entry_id`, auth-gate parametrize covering only 5 of 8 routes.

**Files:**
- Modify: `src/personal_os/device_sync/__init__.py:68-69`
- Modify: `tests/unit/device_sync/fakes.py:482-491`
- Modify: `src/personal_os/device_sync/contracts.py:456-473`
- Modify: `tests/unit/device_sync/test_contracts.py` (new tests beside the existing `ManifestAction` tests at lines 412-469)
- Modify: `tests/unit/api_runtime/test_device_sync_routes.py:188-227`

**Interfaces:**
- Consumes: `ManifestAction` (contracts.py:433), `_LOCAL_ENTRY_ID_MAXIMUM_LENGTH = 256` (contracts.py:47), `ManifestEntry`'s local-entry-id validation at contracts.py:355-359.
- Produces: nothing downstream; standalone hygiene.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/device_sync/test_contracts.py`, add beside the existing ManifestAction tests:

```python
def test_manifest_action_local_entry_id_is_bounded() -> None:
    with pytest.raises(ValueError):
        ManifestAction(
            action_index=0,
            action_kind=ManifestActionKind.UPLOAD,
            local_entry_id="",
            source_id=None,
            source_version_id=None,
            source_locator_id=None,
            source_tombstone_id=None,
            reason=None,
        )
    with pytest.raises(ValueError):
        ManifestAction(
            action_index=0,
            action_kind=ManifestActionKind.UPLOAD,
            local_entry_id="a" * 257,
            source_id=None,
            source_version_id=None,
            source_locator_id=None,
            source_tombstone_id=None,
            reason=None,
        )
    # 256 characters remain valid, mirroring ManifestEntry's bound.
    action = ManifestAction(
        action_index=0,
        action_kind=ManifestActionKind.UPLOAD,
        local_entry_id="a" * 256,
        source_id=None,
        source_version_id=None,
        source_locator_id=None,
        source_tombstone_id=None,
        reason=None,
    )
    assert action.local_entry_id == "a" * 256


def test_package_all_entries_are_unique() -> None:
    from personal_os import device_sync

    assert len(device_sync.__all__) == len(set(device_sync.__all__))
```

(Copy the exact import/pytest style from the neighboring tests in the file.)

In `tests/unit/api_runtime/test_device_sync_routes.py`, extend the parametrize list at lines 188-197 with the three missing routes:

```python
    ("PUT", f"/api/sync/manifests/{uuid4()}/pages/0"),
    ("POST", f"/api/sync/manifests/{uuid4()}/finalize"),
    ("POST", f"/api/sync/manifests/{uuid4()}/complete"),
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/device_sync/test_contracts.py tests/unit/api_runtime/test_device_sync_routes.py -q`
Expected: FAIL — the two new contract tests fail (no bound on `local_entry_id`; duplicate `"DeviceEventType"` in `__all__`). The auth-gate additions already pass if the dependency is shared; they are coverage completion, not RED (keep them regardless).

- [ ] **Step 3: Implement**

- `src/personal_os/device_sync/__init__.py`: delete the duplicate `"DeviceEventType",` at line 69.
- `tests/unit/device_sync/fakes.py`: delete `build_metrics_protocol_fake` (lines 482-485) and `manifest_run_id_of` (lines 488-491). Remove imports that become unused (check `ManifestRunReceipt` — still used by `build_run_receipt`, keep it; `InMemoryDeviceSyncMetrics` still used by `build_service_harness`, keep it).
- `src/personal_os/device_sync/contracts.py` in `ManifestAction.__post_init__`, after the `action_index` check, mirror `ManifestEntry`'s validation:

```python
        if self.local_entry_id is not None and not (
            1 <= len(self.local_entry_id.encode("utf-8")) <= _LOCAL_ENTRY_ID_MAXIMUM_LENGTH
        ):
            raise ValueError("local_entry_id must be 1..256 UTF-8 bytes when present")
```

(Match `ManifestEntry`'s exact bound semantics at contracts.py:355-359; if that check uses character length rather than bytes, mirror characters instead so the two stay consistent with the DDL's `octet_length` check.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/device_sync tests/unit/api_runtime -q`
Expected: PASS (all, including the existing suites that construct `ManifestAction`).

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/device_sync/__init__.py src/personal_os/device_sync/contracts.py tests/unit/device_sync/fakes.py tests/unit/device_sync/test_contracts.py tests/unit/api_runtime/test_device_sync_routes.py
git commit -m "fix: close device-sync contract and route-test hygiene minors"
```

---

### Task 2: Honest mid-stream download outcome and deterministic generator close

Two review minors (BACKLOG line 65) that live server-side: a download that starts 200 and dies mid-stream is logged `API_REQUEST_COMPLETED`; the streaming wrapper never `aclose`s the inner verified-chunks generator on client disconnect.

**Files:**
- Modify: `apps/api/src/api_runtime/request_context.py:151-178, 209-229`
- Modify: `apps/api/src/api_runtime/device_sync_routes.py:339-345`
- Test: `tests/unit/api_runtime/test_request_context.py`, `tests/unit/api_runtime/test_device_sync_routes.py:760-778`

**Interfaces:**
- Consumes: `EventName.API_REQUEST_COMPLETED/_REJECTED/_FAILED` (src/personal_os/diagnostics/events.py), the raw-ASGI middleware in `request_context.py`, `verified_chunks` (device_sync_routes.py:96-116).
- Produces: access observations may now carry `"reason": "response_body_incomplete"` on `API_REQUEST_FAILED` — a new closed reason token surfaced in the diagnostics stream (AGENTS closed-path rule).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/api_runtime/test_request_context.py`, following the file's existing raw-ASGI app + recording-sink pattern:

```python
@pytest.mark.asyncio
async def test_mid_stream_failure_after_200_emits_failed_with_closed_reason() -> None:
    events: list[tuple[EventName, dict[str, object]]] = []
    # ... build the middleware over an app that sends
    # http.response.start (status 200), then one
    # http.response.body with more_body=True, then raises RuntimeError.
    assert events == [
        (EventName.API_REQUEST_FAILED, ANY)
    ] and events[0][1]["reason"] == "response_body_incomplete" \
        and events[0][1]["status_code"] == 200
```

Write it with the file's real sink double and exact assertion style (dict equality including `http_method`/`route`/`duration_ms` keys as the neighboring tests do). Add the twin positive case: an app that sends start 200 plus a final `http.response.body` with `more_body=False` still emits `API_REQUEST_COMPLETED` with no `reason` key.

In `tests/unit/api_runtime/test_device_sync_routes.py`, extend the existing raw-ASGI mid-stream test (lines 760-778): after the response iteration is abandoned mid-body, assert the fake content service's opened verified context was closed (the fake `content_service.open_content` context records its own `closed` flag; assert it is `True`). This is the `aclose` proof.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/api_runtime/test_request_context.py tests/unit/api_runtime/test_device_sync_routes.py -q`
Expected: FAIL — the mid-stream case records `API_REQUEST_COMPLETED`; the context-closed assertion fails.

- [ ] **Step 3: Implement**

`request_context.py`:

1. Add a module constant: `_RESPONSE_BODY_INCOMPLETE_REASON: Final[str] = "response_body_incomplete"`.
2. In `__call__`, track body completion beside `status_code`:

```python
        status_code: int | None = None
        response_body_completed = False

        async def send_with_correlation_headers(message: Mapping[str, Any]) -> None:
            nonlocal status_code, response_body_completed
            if message["type"] == "http.response.start":
                status_code = message["status"]
                await send(_amend_response_start(message, resolution.context))
                return
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_body_completed = True
            await send(message)
```

3. Pass the flag through the `finally` block into `_emit_access_observation(scope, status_code, started_ns, response_body_completed)` and classify: a `status_code < 400` exchange whose final body chunk was never sent emits `API_REQUEST_FAILED` with `"reason": _RESPONSE_BODY_INCOMPLETE_REASON` in the payload (keep `status_code` as the already-sent 2xx — that pairing is the honest mid-stream record). Everything else keeps today's classification.

`device_sync_routes.py` `_continued` (lines 339-345):

```python
    async def _continued(primed: bytes, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Yield the primed first chunk, then the remainder of the stream."""

        try:
            if primed:
                yield primed
            async for chunk in stream:
                yield chunk
        finally:
            # Client disconnect closes only this outer generator; the inner
            # verified-chunks generator owns the opened reader context and
            # must close deterministically instead of by GC.
            await stream.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/api_runtime -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/api_runtime/request_context.py apps/api/src/api_runtime/device_sync_routes.py tests/unit/api_runtime/test_request_context.py tests/unit/api_runtime/test_device_sync_routes.py
git commit -m "fix: classify mid-stream download failures and close the verified stream"
```

---

### Task 3: Migration — append-time submitted policy decision column

BACKLOG line 64 durable fix, schema half. `manifest_entry_resolutions` gains `submitted_policy_allowed BOOLEAN NULL`: the policy verdict recorded at append time (when the raw locator is still in memory), read at finalize. NULL means "legacy row appended before this migration" and keeps today's finalize-time recomputation.

**Files:**
- Create: `migrations/versions/20260829_01_add_manifest_entry_submitted_policy_allowed.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/tables.py:638-659`
- Modify: `src/personal_os/database_schema.py:22`
- Modify: `tests/contract/test_authentication_migration_contract.py:754`, `tests/unit/recovery/test_contracts.py:112`
- Test: `tests/integration/device_sync/test_device_sync_migration.py`

**Interfaces:**
- Consumes: migration chain head `20260828_04`; template `migrations/versions/20260827_01_add_manifest_run_client_activity.py`.
- Produces: column `knowledge.manifest_entry_resolutions.submitted_policy_allowed` (nullable boolean) for Task 4.

- [ ] **Step 1: Write the failing migration test**

In `tests/integration/device_sync/test_device_sync_migration.py`, following the file's existing column-assertion pattern (how it pinned `manifest_runs.last_client_activity_at` for 20260827_01 — information_schema lookup), add:

```python
async def test_submitted_policy_allowed_column_survives_upgrade_and_downgrade(...) -> None:
    # After upgrade to head: the column exists and is nullable.
    # After downgrade to 20260828_04: the column is absent.
    # Table counts stay 37 (upgrade) / 32 (downgrade) — a column add
    # changes no catalog counts.
```

Use the file's existing engine/upgrade/downgrade fixtures verbatim (copy the 20260827_01 column test and adapt names).

- [ ] **Step 2: Run test to verify it fails**

Run (disposable stack first):

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan-col-<date>
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-col-<date> uv run pytest tests/integration/device_sync/test_device_sync_migration.py -q
bash .local/serve-live-ci.sh down
```

Expected: FAIL — column does not exist after upgrade.

- [ ] **Step 3: Implement the migration**

Create `migrations/versions/20260829_01_add_manifest_entry_submitted_policy_allowed.py` mirroring 20260827_01's structure (header docstring, `Final` constants, `SCHEMA_NAME = "knowledge"`):

```python
def upgrade() -> None:
    op.add_column(
        "manifest_entry_resolutions",
        sa.Column("submitted_policy_allowed", sa.Boolean(), nullable=True),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_column("manifest_entry_resolutions", "submitted_policy_allowed", schema=SCHEMA_NAME)
```

No backfill: existing rows keep NULL, which Task 4 reads as "recompute at finalize" (today's exact behavior). Downgrade mirrors 20260827_01's plain column drop.

Then:
- `tables.py`: add `sa.Column("submitted_policy_allowed", sa.Boolean(), nullable=True)` to the `manifest_entry_resolutions` mirror.
- `src/personal_os/database_schema.py:22`: `CANONICAL_POSTGRESQL_SCHEMA_REVISION = "20260829_01"`.
- Update both pins (`tests/contract/test_authentication_migration_contract.py:754`, `tests/unit/recovery/test_contracts.py:112`) to `"20260829_01"`.

- [ ] **Step 4: Run tests to verify they pass**

Same command as Step 2, plus the two pin tests:
`uv run pytest tests/contract/test_authentication_migration_contract.py tests/unit/recovery/test_contracts.py -q`
Expected: PASS everywhere; `uv run alembic heads` shows single head `20260829_01`.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/20260829_01_add_manifest_entry_submitted_policy_allowed.py packages/postgresql-source-store/src/postgresql_source_store/tables.py src/personal_os/database_schema.py tests/contract/test_authentication_migration_contract.py tests/unit/recovery/test_contracts.py tests/integration/device_sync/test_device_sync_migration.py
git commit -m "feat: add manifest append-time policy decision column"
```

---

### Task 4: Store logic — evaluate the submitted subject at append, read the persisted decision at finalize

BACKLOG line 64 durable fix, behavior half. Today `_submitted_subject_is_allowed` (device_manifest_store.py:1738-1753) builds the `PolicySubject` with `normalized_locator=None, source_id=None, source_type=None`, so any locator-class / `exact_source_id` / `source_type` rule forces an indeterminate → enforced `EXCLUDED` verdict for every unowned upload. The run's bound revision is immutable for the run's lifetime (append re-checks staleness via `_reject_policy_stale_run`), so a decision recorded at append stays valid at finalize.

**Files:**
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/device_manifest_store.py` (`_accept_page` 1123-1188, `_resolve_page_entries` 1192-1278, `_plan_actions` 1553-1681, helper beside `_submitted_subject_is_allowed` 1738-1753)
- Test: `tests/integration/device_sync/test_cursor_and_manifest_transactions.py`

**Interfaces:**
- Consumes: `evaluate_policy`, `PolicySubject`, `EnforcedPolicyDecision` (already imported); `_load_bound_policy_revision` (1528-1551); Task 3's column.
- Produces: resolution rows carrying `submitted_policy_allowed: bool | None`; `ManifestEntryResolution.is_policy_allowed` hydration from the persisted value.

- [ ] **Step 1: Write the failing integration test**

In `tests/integration/device_sync/test_cursor_and_manifest_transactions.py` (reusing `seed_device_sync_workspace`, `publish_workspace_policy`, `SeededPolicyRule`, `manifest_entry`, `fingerprint`, `_digest`, `compute_manifest_final_digest`, and the `ManifestStoreHarness`):

```python
@pytest.mark.asyncio
async def test_locator_class_rule_uses_append_time_decision_for_unowned_uploads(
    manifest_store: ManifestStoreHarness,
) -> None:
    workspace = await seed_device_sync_workspace(manifest_store.engine)
    await publish_workspace_policy(
        manifest_store.engine,
        workspace,
        rules=(SeededPolicyRule(rule_kind="folder_prefix", text_operand="private/"),),
    )
    context = workspace.context()
    run = await manifest_store.start(context)
    entries = (
        manifest_entry("entry-allowed", locator="journal/note.md",
                       observed=fingerprint("append-time-allowed")),
        manifest_entry("entry-denied", locator="private/secret.md",
                       observed=fingerprint("append-time-denied")),
    )
    page_digest = _digest("append-time-policy-page-0")
    await manifest_store.append_page(
        context, run.manifest_run_id, page_number=0, entries=entries, page_digest=page_digest
    )
    final_digest = compute_manifest_final_digest((page_digest,))
    await manifest_store.finalize(
        context, run.manifest_run_id, total_entry_count=len(entries), final_digest=final_digest
    )
    page = await manifest_store.read_actions(context, run.manifest_run_id, limit=200)
    kinds = {action.local_entry_id: action.action_kind for action in page.actions}
    assert kinds["entry-allowed"] is ManifestActionKind.UPLOAD
    assert kinds["entry-denied"] is ManifestActionKind.EXCLUDED
```

Also extend the harness `resolution_rows` reader (device_manifest_store test harness at test file line 882) if it enumerates columns, and assert `submitted_policy_allowed` is `True` / `False` for the two rows.

- [ ] **Step 2: Run test to verify it fails**

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan-append-<date>
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-append-<date> uv run pytest tests/integration/device_sync/test_cursor_and_manifest_transactions.py -q
bash .local/serve-live-ci.sh down
```

Expected: FAIL — `entry-allowed` plans `EXCLUDED` today (the `folder_prefix` rule goes indeterminate for the unowned subject).

- [ ] **Step 3: Implement**

In `device_manifest_store.py`:

1. Add the append-time evaluator beside `_submitted_subject_is_allowed`:

```python
    def _submitted_entry_is_allowed(
        self,
        revision: ExclusionPolicyRevision,
        workspace_id: UUID,
        entry: ManifestEntry,
    ) -> bool:
        """The append-time decision: the entry's raw locator is still in
        memory here, so locator-class rules evaluate against the submitted
        path. Only the boolean verdict persists — never the locator."""
        subject = PolicySubject(
            workspace_id=workspace_id,
            source_id=entry.known_source_id,
            normalized_locator=entry.normalized_locator.value,
            source_type=None,
            media_type=self._media_type(entry.fingerprint.media_type),
            size_bytes=entry.fingerprint.size_bytes,
        )
        outcome = evaluate_policy(revision=revision, subject=subject)
        return outcome.enforced is EnforcedPolicyDecision.ALLOWED
```

2. In `_accept_page`, load the run's bound revision once inside the same transaction and pass it down:
   `revision = await self._load_bound_policy_revision(connection, workspace_id, int(run.policy_revision_number))` → forward `revision=revision` into `_resolve_page_entries`.
3. In `_resolve_page_entries`, accept the `revision` parameter and add to each resolution row dict:
   `"submitted_policy_allowed": self._submitted_entry_is_allowed(revision, workspace_id, entry),`
4. In `_plan_actions` (line 1610-1612), hydrate from the persisted decision with the legacy fallback:

```python
                is_policy_allowed=(
                    row.submitted_policy_allowed
                    if row.submitted_policy_allowed is not None
                    else self._submitted_subject_is_allowed(revision, workspace_id, submitted)
                ),
```

- [ ] **Step 4: Run tests to verify they pass**

Same command as Step 2, then the full device-sync suite:
`CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-append-<date> uv run poe device-sync-test`
Expected: PASS — the new test, and every existing test (the fixture's `media_type` exclusion at line 662 is unaffected: media_type is available at finalize too, and fresh runs now record the same verdict at append).

- [ ] **Step 5: Commit**

```bash
git add packages/postgresql-source-store/src/postgresql_source_store/device_manifest_store.py tests/integration/device_sync/test_cursor_and_manifest_transactions.py
git commit -m "fix: evaluate unowned manifest uploads against the append-time policy decision"
```

---

### Task 5: Plugin — rebuild reconcile-first decided by vault content

BACKLOG line 66. `JournalPersistence.#rebuildEmptyJournal` (persistence.ts:729-749) decides reconcile-first only from journal artifacts (manifest present OR `journal.sqlite.g1` exists). A full journal deletion (the mobile shape) yields `fresh_journal_created` with no reconcile flag, so the startup snapshot mints creates (repository.ts:900-907) and the drain uploads them before any reconcile — the blocked-conflict create-storm. Two fixes: (a) a fresh journal over a non-empty vault must reconcile first; (b) the artifact probe must see ANY generation file, not only generation 1 (retention deletes g1 once the third generation publishes).

**Files:**
- Modify: `apps/obsidian-plugin/src/journal/contracts.ts:261-268`
- Modify: `apps/obsidian-plugin/src/journal/persistence.ts` (options ~322, `#rebuildEmptyJournal` 729-749, `#hasFirstGenerationFile` 751-759, `JournalFileStore` 95-100, `VaultAdapterSurface` 108-115, `createVaultPluginJournalStore` 133-149)
- Modify: `apps/obsidian-plugin/src/plugin.ts:750-756`
- Test: `apps/obsidian-plugin/src/journal/persistence.test.ts`, `apps/obsidian-plugin/src/journal/contracts.test.ts`

**Interfaces:**
- Consumes: `JournalPersistence` options `{fileStore, engineModule, diagnosticTrail}`; `generationFileName(n)`.
- Produces: `JournalPersistenceOptions.hasVaultContent?: (() => Promise<boolean>) | null`; `JournalFileStore.list(): Promise<readonly string[]>`; new closed recovery state `"fresh_journal_reconcile_required"`.

- [ ] **Step 1: Write the failing tests**

In `contracts.test.ts`, extend the `JOURNAL_RECOVERY_STATES` pin with `"fresh_journal_reconcile_required"`.

In `persistence.test.ts` (using the file's existing fake file store and engine harness):

```typescript
it("rebuilds fresh-journal-reconcile-required when the vault has content", async () => {
  const persistence = buildPersistence({ hasVaultContent: async () => true }); // empty store
  await persistence.open();
  expect(persistence.recoveryState).toBe("fresh_journal_reconcile_required");
  expect(persistence.isReconcileRequired).toBe(true);
});

it("keeps fresh_journal_created for an empty vault", async () => {
  const persistence = buildPersistence({ hasVaultContent: async () => false });
  await persistence.open();
  expect(persistence.recoveryState).toBe("fresh_journal_created");
  expect(persistence.isReconcileRequired).toBe(false);
});

it("treats any generation file as a rebuild artifact", async () => {
  // store has journal.sqlite.g2 (and no manifest, no g1) — today this
  // misclassifies as fresh_journal_created
  const persistence = buildPersistence({
    generationFiles: [2],
    hasVaultContent: async () => false,
  });
  await persistence.open();
  expect(persistence.recoveryState).toBe("empty_journal_rebuilt");
  expect(persistence.isReconcileRequired).toBe(true);
});

it("fails closed when the vault probe errors", async () => {
  const persistence = buildPersistence({
    hasVaultContent: async () => { throw new Error("vault unavailable"); },
  });
  await expect(persistence.open()).rejects.toMatchObject({ reason: "journal_store_unavailable" });
});
```

(Adapt to the file's actual harness/builder names and error-shape assertions; follow how existing tests assert `journalStoreError` reasons.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/journal/persistence.test.ts src/journal/contracts.test.ts`
Expected: FAIL — no `hasVaultContent` option, no `list()`, no new state, g2-only probe false.

- [ ] **Step 3: Implement**

1. `contracts.ts` — extend the closed vocabulary and its docstring:

```typescript
export const JOURNAL_RECOVERY_STATES = [
  "fresh_journal_created",
  "fresh_journal_reconcile_required",
  "verified_generation_loaded",
  "prior_generation_recovered",
  "empty_journal_rebuilt",
] as const;
```

Update the block comment: `fresh_journal_reconcile_required` accompanies `isReconcileRequired: true` because the journal knows nothing about a non-empty Vault (the full-deletion rebuild shape); `empty_journal_rebuilt` keeps its existing meaning.

2. `persistence.ts`:
   - `JournalFileStore` and `VaultAdapterSurface.adapter` gain `list(): Promise<readonly string[]>`; `createVaultPluginJournalStore` implements it via the Obsidian DataAdapter's directory listing, narrowed to the journal directory's file names. Extend the in-memory fake store used by tests.
   - Beside `generationFileName`, add and export `isGenerationFileName(fileName: string): boolean` (matches the same `journal.sqlite.gN` naming exactly) and use it in the new probe:

```typescript
  async #hasAnyGenerationFile(): Promise<boolean> {
    try {
      const names = await this.#fileStore.list();
      return names.some(isGenerationFileName);
    } catch {
      // Fail closed exactly like the previous single-generation probe.
      throw journalStoreError("journal_store_unavailable");
    }
  }
```

   - `JournalPersistenceOptions` gains `readonly hasVaultContent?: (() => Promise<boolean>) | null;` (stored as `#hasVaultContent`, default `null`).
   - `#rebuildEmptyJournal`:

```typescript
  async #rebuildEmptyJournal(isManifestPresent: boolean): Promise<void> {
    const hasJournalArtifacts = isManifestPresent || (await this.#hasAnyGenerationFile());
    const hasVaultContent =
      this.#hasVaultContent === null ? false : await this.#probeVaultContent();
    const reconcileRequired = hasJournalArtifacts || hasVaultContent;
    if (reconcileRequired) {
      // Nothing verified (or nothing known about a non-empty Vault):
      // preserve every Vault file and reconcile before any outbound upload.
      this.#isReconcileRequired = true;
    }
    const recoveryState: JournalRecoveryState = hasJournalArtifacts
      ? "empty_journal_rebuilt"
      : hasVaultContent
        ? "fresh_journal_reconcile_required"
        : "fresh_journal_created";
    // ... rest unchanged (createEmpty with isReconcileRequired/recoveryState)
  }

  async #probeVaultContent(): Promise<boolean> {
    try {
      return await this.#hasVaultContent!();
    } catch {
      // A unreadable Vault must never masquerade as an empty one.
      throw journalStoreError("journal_store_unavailable");
    }
  }
```

3. `plugin.ts` (~line 750), wire the probe at construction:

```typescript
      const persistence = new JournalPersistence({
        fileStore: this.createJournalFileStore(),
        engineModule,
        diagnosticTrail,
        // A journal rebuilt over a non-empty Vault must reconcile first
        // (the mobile full-deletion shape); the probe mirrors exactly the
        // files the automatic snapshot would admit.
        hasVaultContent: async () => this.app.vault.getFiles().length > 0,
      });
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run` then `... run type-check`
Expected: PASS (full plugin suite — the status projection and any recovery-state exhaustive switches surface here if the new token needs another mapping; fix those in the same commit).

- [ ] **Step 5: Commit**

```bash
git add apps/obsidian-plugin/src/journal/contracts.ts apps/obsidian-plugin/src/journal/persistence.ts apps/obsidian-plugin/src/journal/contracts.test.ts apps/obsidian-plugin/src/journal/persistence.test.ts apps/obsidian-plugin/src/plugin.ts
git commit -m "fix: reconcile first when a rebuilt journal meets vault content"
```

---

### Task 6: Plugin minors — single verified download per action and outbound-conflict barrier parity

The remaining three review minors (BACKLOG line 65). "Double download per action" and "digest-after-validation ordering" are one defect: `applyAction` downloads once to prove the fingerprint (manifest-reconciler.ts:526-539, bytes discarded) and `applier.apply` downloads the same version again (remote-event-applier.ts:238-261) — two downloads whose bytes could theoretically diverge; one verified download closes both. "Echo-conflict barrier parity": the queue driver's `blocked_conflict` park (queue-driver.ts:1134-1140) raises no repair barrier while every inbound conflict lane does (remote-event-applier.ts:412-418, 459-472; manifest-reconciler.ts:863-872).

**Files:**
- Modify: `apps/obsidian-plugin/src/device-sync/remote-event-applier.ts` (apply signature + download stage 238-261)
- Modify: `apps/obsidian-plugin/src/device-sync/manifest-reconciler.ts:526-592`
- Modify: `apps/obsidian-plugin/src/journal/queue-driver.ts:1134-1140`
- Test: `apps/obsidian-plugin/src/device-sync/remote-event-applier.test.ts`, `apps/obsidian-plugin/src/device-sync/manifest-reconciler.test.ts`, `apps/obsidian-plugin/src/journal/queue-driver.test.ts`

**Interfaces:**
- Consumes: `VerifiedDownload` (api.ts), `repository.deviceSync.nextObservationGeneration()` / `startRepairBarrier({generation, reason})` (repository.ts:349), closed reason `device_manifest_target_occupied` (already a `DeviceSyncReason`, contracts.ts:57-63).
- Produces: `apply(event, options?: { verifiedDownload?: VerifiedDownload | null })`.

- [ ] **Step 1: Write the failing tests**

`manifest-reconciler.test.ts` — for a download action, assert the shared downloader seam is called exactly once and the applier receives the verified bytes:

```typescript
it("downloads each download action exactly once and reuses the verified bytes", async () => {
  // drive one applyAction-equivalent path with a counting downloader fake
  // and a recording applier fake; today downloader.count === 2
  expect(downloader.calls).toHaveLength(1);
  expect(applier.lastOptions?.verifiedDownload?.declaredSha256).toEqual(
    downloader.calls[0]?.declaredSha256,
  );
});
```

`remote-event-applier.test.ts`:

```typescript
it("skips the download when a matching verified download is provided", async () => {
  // apply(event, { verifiedDownload }) — downloader fake must not be called;
  // the writer receives verifiedDownload.bytes
});
it("falls back to the downloader when the verified download does not match", async () => {
  // verifiedDownload.declaredSha256 !== event.currentFingerprint.sha256
  // → downloader called once
});
```

`queue-driver.test.ts` — extend/beside the existing `blocked_conflict` test:

```typescript
it("raises a repair barrier when parking a blocked_conflict upload", async () => {
  // drive one event whose upload settles source_locator_conflict
  // today: no barrier; after: barrier_reason "device_manifest_target_occupied"
  const state = await harness.repository.deviceSync.readState();
  expect(state.barrierGeneration).not.toBeNull();
});
it("tolerates an already-owed repair barrier when parking blocked_conflict", async () => {
  // pre-existing barrier → the park still completes, no throw
});
```

(Adapt to each file's existing harness builders; the barrier assertions mirror queue-driver.test.ts:2381.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/remote-event-applier.test.ts src/device-sync/manifest-reconciler.test.ts src/journal/queue-driver.test.ts`
Expected: FAIL — downloader called twice, no `verifiedDownload` option, no barrier after `blocked_conflict`.

- [ ] **Step 3: Implement**

1. `remote-event-applier.ts` — extend the apply entry with the optional options object; in the download stage:

```typescript
    let bytes: Uint8Array | null = null;
    if (needsDownload && event.currentVersionId !== null) {
      const predownloaded =
        options?.verifiedDownload != null &&
        options.verifiedDownload.declaredSha256 === event.currentFingerprint?.sha256
          ? options.verifiedDownload
          : null;
      if (predownloaded !== null) {
        // The reconciler proved this exact download already; reusing the
        // same bytes keeps the digest proof and the applied bytes one
        // object instead of two downloads.
        bytes = predownloaded.bytes;
      } else {
        // ...existing downloader call and error mapping unchanged
      }
    }
```

Thread `options` through every internal call path `apply` uses (the recovery/resume lanes pass none).

2. `manifest-reconciler.ts` — in the download branch of `applyAction`, after building `verifiedFingerprint`:

```typescript
    try {
      await applier.apply(event, { verifiedDownload: verified });
    } catch (error) {
      return runFailure("actions", error);
    }
```

(Only the two download branches have `verified` in scope; the tombstone branch keeps `applier.apply(event)`.)

3. `queue-driver.ts` — the `blocked_conflict` case:

```typescript
      case "blocked_conflict": {
        // The server's typed, non-retryable business-conflict verdict (for
        // example the create-time `source_locator_conflict`): park the event
        // terminally so the queue moves on instead of retrying a verdict
        // that can never succeed.
        await this.#closeTerminal(eventId, "blocked_conflict", "blocked_conflict", correlationId);
        // Barrier parity with the inbound apply lanes: the same conflict on
        // the inbound path freezes observation and requires reconciliation,
        // so the outbound lane must not keep uploading into a claim it
        // cannot see.
        try {
          const generation = await this.#repository.deviceSync.nextObservationGeneration();
          await this.#repository.deviceSync.startRepairBarrier({
            generation,
            reason: "device_manifest_target_occupied",
          });
        } catch {
          // A barrier or active manifest run already exists: a repair is
          // already owed — nothing to raise.
        }
        return "continue";
      }
```

(`this.#repository.deviceSync` is the driver's established access — queue-driver.ts:364.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run` then `... run type-check && ... run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/obsidian-plugin/src/device-sync/remote-event-applier.ts apps/obsidian-plugin/src/device-sync/manifest-reconciler.ts apps/obsidian-plugin/src/journal/queue-driver.ts apps/obsidian-plugin/src/device-sync/remote-event-applier.test.ts apps/obsidian-plugin/src/device-sync/manifest-reconciler.test.ts apps/obsidian-plugin/src/journal/queue-driver.test.ts
git commit -m "fix: reuse the verified download and barrier outbound conflicts"
```

---

### Task 7: Web Admin — stale-preview health line and lifecycle rejections page

BACKLOG line 55. Decision (recorded in the handoff, Task 9): the spec's "read back from Web Admin" means the operator can SEE it in the Web Admin UI — render both surfaces. No API or generated-client change; the types already exist in `packages/api-client/src/generated/schema.ts` (line 1960, 3215-3281).

**Files:**
- Modify: `apps/web/src/features/exclusion-policy/PolicyStatus.tsx` (+ `PolicyStatus.test.tsx`)
- Modify: `apps/web/src/api/exclusion-policy-client.ts` (export `unwrapEnvelope`)
- Create: `apps/web/src/api/source-lifecycle-client.ts` (+ `.test.ts`)
- Create: `apps/web/src/features/source-lifecycle/LifecycleRejections.tsx` (+ `.test.tsx`)
- Create: `apps/web/src/app/admin/lifecycle/page.tsx`

**Interfaces:**
- Consumes: `createApiClient`, `AuthenticationCallResult`, `createNativeFetchTransport` (existing web api layer); `PolicyStatusData.stale_running_previews` (`StaleRunningPreviewData: { policy_preview_id, reason: "worker_stale_running", age_seconds }`).
- Produces: `createBrowserSourceLifecycleClient()` with `getRejectionDiagnostics(): Promise<AuthenticationCallResult<SourceLifecycleDiagnosticsData>>`; `SourceLifecycleDiagnosticsData` (`commit_counters: {operation, outcome, count}[]`, `recent_rejections: {error_code, at_epoch_ms, operation}[]`).

- [ ] **Step 1: Write the failing tests**

`PolicyStatus.test.tsx` — extend the `statusData()` fixture factory (line 17-36) with an override:

```tsx
it("renders one health row per stale running preview", () => {
  render(
    <PolicyStatus
      status={statusData({
        stale_running_previews: [
          { policy_preview_id: "b7c1...", reason: "worker_stale_running", age_seconds: 1140 },
        ],
      })}
    />,
  );
  expect(screen.getByText(/worker stale running/i)).toBeInTheDocument();
  expect(screen.getByText(/19 min/i)).toBeInTheDocument();
});

it("renders no worker-health block while nothing is stale", () => {
  render(<PolicyStatus status={statusData()} />);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
```

`LifecycleRejections.test.tsx` — component takes an injectable reader (fake client, no msw needed):

```tsx
it("renders counters and the recent rejection ring", async () => {
  const client = {
    getRejectionDiagnostics: async () => ({
      ok: true as const,
      data: {
        commit_counters: [
          { operation: "rename", outcome: "committed", count: 3 },
          { operation: "restore", outcome: "rejected", count: 1 },
        ],
        recent_rejections: [
          { error_code: "source_locator_conflict", at_epoch_ms: 1_750_000_000_000, operation: "restore" },
        ],
      },
    }),
  };
  render(<LifecycleRejections client={client} />);
  expect(await screen.findByText("rename · committed")).toBeInTheDocument();
  expect(screen.getByText("source_locator_conflict")).toBeInTheDocument();
});

it("renders the closed error code when the read fails", async () => {
  const client = { getRejectionDiagnostics: async () => ({ ok: false as const, error: { code: "forbidden", ... } }) };
  render(<LifecycleRejections client={client} />);
  expect(await screen.findByText(/forbidden/i)).toBeInTheDocument();
});
```

`source-lifecycle-client.test.ts` — mirror `exclusion-policy-client.test.ts`: envelope unwrapping (ok data / error / transport throw).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm --filter @workspace/web-runtime test`
Expected: FAIL — no stale block, components/modules missing.

- [ ] **Step 3: Implement**

1. `exclusion-policy-client.ts`: change `function unwrapEnvelope` to `export function unwrapEnvelope` (the web-auth review explicitly flagged duplicating it as a smell; export, don't copy).

2. `source-lifecycle-client.ts`:

```typescript
import { createApiClient, type ApiClient, type components } from "@workspace/api-client";
import { createNativeFetchTransport } from "./native-fetch-transport";
import { unwrapEnvelope, type AuthenticationCallResult } from "./exclusion-policy-client";

export type SourceLifecycleDiagnosticsData =
  components["schemas"]["SourceLifecycleDiagnosticsData"];

export interface SourceLifecycleReader {
  getRejectionDiagnostics(): Promise<AuthenticationCallResult<SourceLifecycleDiagnosticsData>>;
}

export function createSourceLifecycleClient(options: {
  apiClient: ApiClient;
}): SourceLifecycleReader {
  const { apiClient } = options;
  return {
    async getRejectionDiagnostics() {
      try {
        return unwrapEnvelope<SourceLifecycleDiagnosticsData>(
          await apiClient.GET("/api/admin/source-lifecycle/rejections", {
            credentials: "include",
          }),
        );
      } catch {
        return { ok: false, error: REQUEST_UNAVAILABLE_ERROR }; // import or mirror the shared body
      }
    },
  };
}

let cachedBrowserClient: SourceLifecycleReader | null = null;

export function createBrowserSourceLifecycleClient(): SourceLifecycleReader {
  cachedBrowserClient ??= createSourceLifecycleClient({
    apiClient: createApiClient({
      baseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "",
      transport: createNativeFetchTransport(),
    }),
  });
  return cachedBrowserClient;
}
```

(If `REQUEST_UNAVAILABLE_ERROR` is not exported, export it from `exclusion-policy-client.ts` the same way as `unwrapEnvelope` — do not duplicate it.)

3. `PolicyStatus.tsx` — inside the section, after the reconciliation paragraph:

```tsx
      {status.stale_running_previews !== null && (
        <div role="alert">
          <h3>Preview worker health</h3>
          <ul>
            {status.stale_running_previews.map((preview) => (
              <li key={preview.policy_preview_id}>
                Preview <code>{preview.policy_preview_id}</code> — worker stale running for{" "}
                {Math.round(preview.age_seconds / 60)} min
              </li>
            ))}
          </ul>
          <p>No live policy worker has swept these previews. Restart the policy workers.</p>
        </div>
      )}
```

(Keep the component's existing prose style and class naming; match how `policy-status` styles sub-blocks.)

4. `LifecycleRejections.tsx` — `"use client"` component: `useEffect` + `useState` over `props.client.getRejectionDiagnostics()`; renders an `<h2>Lifecycle operations</h2>` card with a `<dl>` of `commit_counters` rows (`{operation} · {outcome}` → `{count}`) and the bounded `recent_rejections` list (`{error_code}` + `{operation}` + formatted time); explicit empty states ("No lifecycle operations recorded yet." / "No rejections in the recent ring."); the error branch renders only the closed `error.code`.

5. `apps/web/src/app/admin/lifecycle/page.tsx` — mirror `admin/policy/page.tsx` exactly (same exports/directives), rendering `<LifecycleRejections client={createBrowserSourceLifecycleClient()} />`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm --filter @workspace/web-runtime test && pnpm --filter @workspace/web-runtime run type-check && pnpm --filter @workspace/web-runtime run lint && pnpm --filter @workspace/web-runtime run build`
Expected: PASS; `uv run poe api-contract-check` still exit 0 (no API change).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/api/exclusion-policy-client.ts apps/web/src/api/source-lifecycle-client.ts apps/web/src/api/source-lifecycle-client.test.ts apps/web/src/features/exclusion-policy/PolicyStatus.tsx apps/web/src/features/exclusion-policy/PolicyStatus.test.tsx apps/web/src/features/source-lifecycle/LifecycleRejections.tsx apps/web/src/features/source-lifecycle/LifecycleRejections.test.tsx apps/web/src/app/admin/lifecycle/page.tsx
git commit -m "feat: render worker staleness and lifecycle rejections in Web Admin"
```

---

### Task 8: Machine-local — per-worker diagnostics directories in run-worker.sh

BACKLOG line 54. The rotating file sink activates per process via `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` (runtime_configuration/loading.py:28; diagnostics/logging.py:529-541 — blank/unset = disabled). `run-worker.sh` never sets it, and a shared directory would trip Windows rotation-rename contention between the two workers, so each worker process gets its own directory.

**Files (all machine-local, untracked — never committed):**
- Modify: `.local/run-worker.sh`
- Modify: `.local/RESTART.md` (step 4b, lines 45-48)

**Interfaces:**
- Consumes: worker role argument (`run-policy-previews` / `run-policy-reconciliations`; apps/worker/src/workflow_worker/command.py:30-35).
- Produces: `.local/runtime-logs/worker-previews/api-diagnostics.log` and `.local/runtime-logs/worker-reconciliations/api-diagnostics.log` whenever a worker starts.

- [ ] **Step 1: Edit run-worker.sh**

After the existing env exports, before the `exec`:

```bash
# Durable worker diagnostics capture (closed-reason remediation §5.1): each
# worker process owns one diagnostics directory — two processes sharing one
# directory contend on the Windows rotation rename (exclusive open).
worker_role="${1:-}"
case "$worker_role" in
  run-policy-previews) worker_diagnostics_subdir="worker-previews" ;;
  run-policy-reconciliations) worker_diagnostics_subdir="worker-reconciliations" ;;
  *) worker_diagnostics_subdir="" ;;
esac
if [[ -n "$worker_diagnostics_subdir" ]]; then
  export KNOWLEDGE_DIAGNOSTICS_LOG_DIR="$(pwd -W 2>/dev/null || pwd)/.local/runtime-logs/$worker_diagnostics_subdir"
  mkdir -p "$KNOWLEDGE_DIAGNOSTICS_LOG_DIR"
fi
```

- [ ] **Step 2: Update RESTART.md step 4b**

Append one sentence to the existing block (lines 45-48): each worker now also writes its rotating diagnostics log to `.local/runtime-logs/worker-<role>/api-diagnostics.log` (set automatically by the script).

- [ ] **Step 3: Verify against the disposable CI stack**

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-diag-verify-<date>
ls -la .local/runtime-logs/worker-previews/ .local/runtime-logs/worker-reconciliations/
# both directories contain api-diagnostics.log (the sink attaches at process
# start); do NOT print file contents — only names/sizes.
bash .local/serve-live-ci.sh down
```

Expected: both `api-diagnostics.log` files exist and are non-empty. (serve-live-ci.sh invokes run-worker.sh, so the export applies there too.)

---

### Task 9: Canonical docs, BACKLOG retirement, handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-08-26-device-cursor-and-manifest-reconciliation-design.md` (dated amendment section)
- Modify: `docs/operations/device-cursor-manifest-reconciliation.md`, `docs/operations/sync-error-tracing.md`
- Modify: `docs/handoff/BACKLOG.md` (remove lines 64, 65, 66, 54, 55)
- Create: `docs/handoff/2026-08-29-device-sync-child8-unblock-smoke-prep.md`

- [ ] **Step 1: Spec amendment**

Add one dated amendment section to the Child 6 design spec covering the four contract changes: (a) `manifest_entry_resolutions.submitted_policy_allowed` — the append-time submitted-policy decision under the run's bound revision, NULL = legacy recompute, raw locator still never persists; (b) the new closed recovery state `fresh_journal_reconcile_required` (fresh journal + non-empty vault ⇒ reconcile-first; any-generation artifact probe); (c) `apply(event, { verifiedDownload })` — one verified download per action, digest proof and applied bytes are the same object; (d) the outbound `blocked_conflict` repair barrier (`device_manifest_target_occupied`).

- [ ] **Step 2: Runbooks**

- `device-cursor-manifest-reconciliation.md`: unowned uploads are policy-evaluated at append (locator-class rules now work for them); the rebuild/reconcile-first rule including the mobile full-deletion shape.
- `sync-error-tracing.md`: the W1 wiring decision is executed (per-worker `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` in run-worker.sh, per-process directories); the W3/L1 "wire-only" caveat is replaced by the real surfaces (PolicyStatus health block; `/admin/lifecycle` page).

- [ ] **Step 3: Final verification**

```bash
uv run poe verify
uv run poe api-contract-check
pnpm --dir apps/obsidian-plugin exec vitest run && pnpm --dir apps/obsidian-plugin run type-check && pnpm --dir apps/obsidian-plugin run lint && pnpm --dir apps/obsidian-plugin run build
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan-final-<date>
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-final-<date> uv run poe device-sync-test
bash .local/serve-live-ci.sh down
git diff --check && git status --short
```

Expected: every command exit 0; working tree clean of unintended files.

- [ ] **Step 4: BACKLOG retirement and handoff**

Remove exactly the five rows (2026-08-26 device-sync ×2, 2026-08-27 device-sync-recovery, 2026-08-24 policy-workers, 2026-08-24 web-admin). Write `docs/handoff/2026-08-29-device-sync-child8-unblock-smoke-prep.md`: final commit SHA, gate evidence, interpretive decisions (the "UI line" ruling; the "digest-after-validation ordering = the download-reuse defect" reading, since the original SDD ledger was cleaned), deferred items (physical mobile re-verification of the rebuild fix rides the next mobile matrix — BACKLOG row NOT re-added; it is covered by the standing Child 9 mobile gates), and next actions.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-08-26-device-cursor-and-manifest-reconciliation-design.md docs/operations/device-cursor-manifest-reconciliation.md docs/operations/sync-error-tracing.md docs/handoff/BACKLOG.md docs/handoff/2026-08-29-device-sync-child8-unblock-smoke-prep.md
git commit -m "docs: amend child 6 contracts and retire unblock backlog rows"
```

---

## Self-Review (completed)

- **Coverage:** all five BACKLOG rows map to tasks — line 64 → Tasks 3+4; line 65 → Tasks 1+2+6; line 66 → Task 5; line 54 → Task 8; line 55 → Task 7. Docs/retirement → Task 9.
- **Placeholders:** none; every step carries concrete code or exact file/line targets. Test skeletons reference verified harness names (`ManifestStoreHarness`, `publish_workspace_policy`, `SeededPolicyRule`, `_digest`, `statusData()`, queue-driver barrier assertions at test line 2381).
- **Type consistency:** `submitted_policy_allowed` (Task 3 column = Task 4 row key = Task 4 hydration); `hasVaultContent`/`list()`/`fresh_journal_reconcile_required` (Task 5 only); `apply(event, { verifiedDownload })` (Task 6 both sides); `SourceLifecycleDiagnosticsData`/`getRejectionDiagnostics` (Task 7 both sides).
