# Child Nine and Phase Two Closure Hygiene Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the 22 BACKLOG rows gated "Before Child 9 operations/recovery acceptance" (15 rows, checkpoint 1) and "Before Phase 2 closure (after Child 9)" (7 rows, checkpoint 2) exactly as specified in `docs/superpowers/specs/backlog/2026-08-30-child-nine-and-phase-two-closure-hygiene-retirement-design.md`.

**Architecture:** Twenty small domain tasks, each an independent TDD cycle with its own commit, ordered checkpoint-first. Two closed-vocabulary extensions (canonical-recovery admission code — OpenAPI-visible; lockout audit action — store-internal), one dispatcher lifecycle fix, and the rest are precision/hygiene fixes inside existing contracts.

**Tech Stack:** Python 3.14 (mypy strict, ruff, pytest, SQLAlchemy async, Alembic), TypeScript strict (Vitest, React 19 web admin, Obsidian plugin vitest, Playwright E2E specs).

## Global Constraints

- `uv run poe verify` and `uv run poe api-contract-check` exit 0 at every commit (Task 2 additionally runs `uv run poe api-contract-export` then `pnpm --filter @workspace/api-client run generate` BEFORE the check).
- Web gates: `pnpm --filter @workspace/web-runtime test` / `run type-check` / `run lint` / `run build`. Plugin gates: `pnpm --dir apps/obsidian-plugin exec vitest run` / `run type-check` / `run lint` / `run build`.
- Integration tests only on disposable stacks: `CI=true bash .local/serve-live-ci.sh up knowledge-ci-<slug>` before, `bash .local/serve-live-ci.sh down` after. Never touch `knowledge-local` data. Authentication integration needs `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-<slug>` (conftest refuses otherwise, fail never skip).
- No new production dependencies. No migrations. The ONLY OpenAPI change is Task 2's new ErrorCode enum member.
- Do not "fix" `except A, B:` tuple syntax anywhere — valid Python 3.14 (PEP 758), including `tests/contract/api/test_authentication_leakage.py:667`.
- Every newly closed error path surfaces its closed reason token at a readable surface.
- Naming per AGENTS: behavior-named tests (`test_rejects_…`), units in names (`timeout_seconds`), no vague names.
- Commits small and conventional; one task = one commit (plus its test files).

---

# CHECKPOINT 1 — before Child 9 acceptance runs

### Task 1: Projection dispatcher lifecycle (row 2026-08-14 §9)

**Files:**
- Modify: `apps/worker/src/workflow_worker/projection_dispatch_runtime.py:522-545` (`run_projection_dispatcher_process`), `:227-250` (shutdown loop)
- Modify: `apps/worker/src/workflow_worker/command.py:49-58`
- Test: `tests/unit/workflow_worker/test_projection_dispatch_runtime.py` (extend), `tests/unit/workflow_worker/test_command.py`

**Interfaces:**
- Consumes: `create_source_store_engine`, `dispose_source_store_engine`, `TemporalClient.connect`, `ProjectionDispatchError`, `ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE`.
- Produces: `run_projection_dispatcher_process` with engine disposed and client closed on EVERY exit path; non-timeout connect failures typed `projection_dispatch_unavailable`.

- [ ] **Step 1: Write the failing tests**

New test class in `tests/unit/workflow_worker/test_projection_dispatch_runtime.py` (module-level monkeypatching of `projection_dispatch_runtime.create_source_store_engine` / `dispose_source_store_engine` / `TemporalClient`, following the file's existing fake/doubles style):

```python
async def test_connect_timeout_disposes_the_engine_and_closes_the_client() -> None:
    # engine factory records dispose; client factory records close;
    # connect raises TimeoutError -> ProjectionDispatchError
    # assert dispose called exactly once and client.close() awaited once

async def test_non_timeout_connect_failure_is_typed_not_a_raw_traceback() -> None:
    # connect raises RuntimeError("connection refused")
    # -> pytest.raises(ProjectionDispatchError) with error_code
    #    PROJECTION_DISPATCH_UNAVAILABLE (no raw propagation)

async def test_clean_shutdown_closes_the_temporal_client_and_disposes_the_engine() -> None:
    # shutdown set after one idle cycle -> both disposals observed
```

Extend the existing shutdown test (`test_shutdown_stops_new_claims_after_the_current_batch`, lines 430-441) with the whole-batch-drain pin:

```python
    # after shutdown fires mid-claim, every already-claimed intent completed:
    assert fake_store.completed == fake_store.claimed  # drain, not abort
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/workflow_worker -q`
Expected: FAIL — engine not disposed on connect failure; no client close; non-timeout connect propagates raw.

- [ ] **Step 3: Implement**

In `run_projection_dispatcher_process` (lines 522-545): create the engine first, then open a `try:` IMMEDIATELY (before Temporal connect) whose `finally` disposes the engine AND closes the client; wrap the connect in both exception arms:

```python
    engine = create_source_store_engine(...)
    temporal_client: TemporalClient | None = None
    try:
        try:
            temporal_client = await asyncio.wait_for(
                TemporalClient.connect(...), timeout=PROJECTION_WORKFLOW_START_TIMEOUT.total_seconds()
            )
        except TimeoutError as cause:
            raise ProjectionDispatchError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE) from cause
        except Exception as cause:
            # Any other connect failure (refused endpoint, TLS, DNS) is the
            # same closed dependency outcome — never a raw traceback.
            raise ProjectionDispatchError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE) from cause
        ...  # existing runtime startup, unchanged
    finally:
        if temporal_client is not None:
            await temporal_client.close()
        await dispose_source_store_engine(engine)
```

(If `TemporalClient.close()` is not awaitable in the pinned temporalio version, close via `await temporal_client.service_client…` — check the sibling usage first; if truly absent, keep a `client.close()`-shaped helper with a comment and prove it in the test.)

The whole-batch drain already exists (`run_until_shutdown` awaits the TaskGroup over every claimed intent, lines 268-271) — Step 1's drain pin proves it; if RED, fix the loop so shutdown cannot cancel the in-flight TaskGroup.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/workflow_worker tests/unit/workflow_worker/test_command.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/worker/src/workflow_worker/projection_dispatch_runtime.py apps/worker/src/workflow_worker/command.py tests/unit/workflow_worker/
git commit -m "fix: dispose dispatcher engine, close temporal client, type connect failures"
```

---

### Task 2: Canonical-recovery admission token (row 2026-08-15 §10)

**Files:**
- Modify: `src/personal_os/error_contracts/codes.py:67-75` (StrEnum) and `:430-483` (recovery `ErrorDefinition` block)
- Modify: `tools/canonical_core_operations.py:369-382` (`_environment_refused`, `_require_write_admission`), `:1579-1581` (target-database mismatch)
- Modify: `src/personal_os/recovery/service.py:546-550` (service-level admission re-check)
- Modify: `docs/operations/canonical-core-recovery.md:42-55` (exit table line 50)
- Regenerate: `packages/api-client/openapi.json` + `packages/api-client/src/generated/schema.ts`
- Test: `tests/unit/tools/test_canonical_core_operations.py` (admission refusals), `tests/unit/recovery/test_service.py` (service re-check), registry completeness auto-guard (`codes.py:959-960`)

**Interfaces:**
- Produces: `ErrorCode.CANONICAL_RECOVERY_ADMISSION_REFUSED = "canonical_recovery_admission_refused"` — category AUTHORIZATION, `is_retryable=False`, `allowed_detail_fields={"operation"}`, safe message "the write-admission gate refused the operation". Exit stays 78 (`_REFUSAL_CATEGORIES` already contains AUTHORIZATION, tools lines 175-177).

- [ ] **Step 1: Write the failing tests**

```python
def test_missing_write_admission_flag_refuses_with_the_admission_code() -> None:
    # backup-create without --confirm-write-admission-disabled
    # -> error.error_code is CANONICAL_RECOVERY_ADMISSION_REFUSED,
    #    exit code 78, safe_details["operation"] == "backup_create"

def test_target_database_mismatch_refuses_with_the_admission_code() -> None:
    # restore-empty with a wrong --confirm-target-database
    # -> same code, safe_details["operation"] == "restore_empty"
```

And the service twin: `restore_empty` on a mismatched target raises `CANONICAL_RECOVERY_ADMISSION_REFUSED` (was ENVIRONMENT_REFUSED). Keep one test asserting a TRUE environment refusal (e.g. non-local environment) still yields ENVIRONMENT_REFUSED — the split must be observable both ways.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/tools tests/unit/recovery -q`
Expected: FAIL (current code reuses ENVIRONMENT_REFUSED).

- [ ] **Step 3: Implement**

1. New StrEnum member + `ErrorDefinition` in `codes.py` (recovery block); the completeness guard at `codes.py:959-960` forces the pair.
2. New helper in `tools/canonical_core_operations.py`:

```python
def _admission_refused(operation: str) -> RecoveryError:
    return RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_ADMISSION_REFUSED,
        safe_details={"operation": SafeToken.parse(operation)},
    )
```

Switch `_require_write_admission` (369-373) and the mismatch site (1579-1581) to it; `_environment_refused` remains for the true environment gate.
3. `service.py:546-550` switches to the new code with the same detail.
4. Runbook exit table: line 50 splits into two rows (environment gate → `canonical_recovery_environment_refused`; admission gate/mismatch → `canonical_recovery_admission_refused`), both exit 78.
5. Regenerate artifacts:

```bash
uv run poe api-contract-export
pnpm --filter @workspace/api-client run generate
```

(Recovery codes are NOT in `_APPROVED_HTTP_STATUS_CODES` — no status entry needed; the enum member is the only wire delta.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/tools tests/unit/recovery tests/contract -q && uv run poe api-contract-check`
Expected: PASS; snapshot diff shows exactly the one added enum member.

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/error_contracts/codes.py tools/canonical_core_operations.py src/personal_os/recovery/service.py docs/operations/canonical-core-recovery.md packages/api-client/openapi.json packages/api-client/src/generated/schema.ts tests/unit/tools tests/unit/recovery
git commit -m "feat: split the canonical-recovery admission refusal token"
```

---

### Task 3: Bundle finalize collision, verify totals, harness cleanup (rows 2026-08-15 §6, §12, §13)

**Files:**
- Modify: `src/personal_os/recovery/bundle.py:394-419` (`finalize`), `:566-583` (`verify_offline` totals)
- Modify: `tests/integration/canonical_core/conftest.py:418-532` (fake store), `:538-562` (mkdtemp prefixes + POSIX leak), `:568-583` (Any types), `:614-651` (`disposable_identity_database`)
- Modify: `tests/integration/canonical_core/test_live_r2_acceptance.py:257-288` (`live_acceptance_context`), `:284` (cast)
- Test: `tests/unit/recovery/test_bundle.py` (or the file holding finalize tests), integration suite

**Interfaces:**
- Produces: `finalize` fails closed on ANY pre-existing final path (file, empty dir, non-empty dir) with `CANONICAL_RECOVERY_BUNDLE_EXISTS`; `verify_offline` counts discovered object files independently of `manifest.objects`.

- [ ] **Step 1: Write the failing tests** (unit, real filesystem via the existing bundle fixtures)

```python
async def test_finalize_refuses_an_empty_preexisting_final_directory(tmp_path) -> None:
    # build a valid staging writer, mkdir the final path (empty) first
    # -> pytest.raises(RecoveryError) with CANONICAL_RECOVERY_BUNDLE_EXISTS
    #    AND the empty directory still exists afterwards (nothing clobbered)

async def test_finalize_refuses_a_nonempty_preexisting_final_directory(tmp_path) -> None: ...

def test_verify_offline_rejects_an_unreferenced_extra_object(tmp_path) -> None:
    # valid bundle + one extra unreferenced object file added under objects/
    # -> verify_offline raises (currently the tautology lets it pass)
```

Harness tests (unit-level where possible): fake store re-raise test —

```python
async def test_fake_store_rejects_same_digest_different_media_restore(...) -> None:
    # store once with media_type "text/markdown", re-store same digest with
    # "application/octet-stream" -> raises OBJECT_STORAGE_METADATA_CONFLICT
```

- [ ] **Step 2: Run to verify failure** — Run: `uv run pytest tests/unit/recovery -q`; Expected: FAIL (empty-dir rename succeeds today; extra object passes; fake accepts re-store).

- [ ] **Step 3: Implement**

1. `finalize` — replace lexists-check→rename with an atomic directory claim then per-entry moves:

```python
    final_path = self._root / _canonical_bundle_id_text(self._bundle_id)
    try:
        # mkdir is the atomic claim: EEXIST for ANY pre-existing final path
        # (file, empty dir, or bundle) on both POSIX and Windows — closing
        # the POSIX rename-into-empty-directory TOCTOU.
        final_path.mkdir(mode=0o750)
    except FileExistsError as cause:
        raise RecoveryError(
            ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS,
            safe_details={"bundle_id": self._bundle_id},
        ) from cause
    try:
        for entry in sorted(self._staging_path.iterdir()):
            os.rename(self._staging_path / entry.name, final_path / entry.name)
        self._staging_path.rmdir()
        _fsync_directory(self._root)   # plus the manifest/sidecar writes as today
    except BaseException:
        shutil.rmtree(final_path, ignore_errors=True)
        raise
```

(Preserve the existing manifest/sidecar exclusive writes, fsync ordering, `_is_finalized` flag, and `_remove_staging` on failure; only the claim+rename strategy changes. Update `create_staging`'s lexists probe at 479-483 to tolerate the new claim semantics — it stays as the early friendly check.)
2. `verify_offline` totals (566-583): collect the object files discovered by the directory walk (the existing `os.walk`) and compare independently:

```python
    if discovered_object_count != len(manifest.objects) or discovered_bytes_total != sum(
        entry.size_bytes for entry in manifest.objects
    ):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.CHECKSUM_MISMATCH)
```

3. Fake store (`conftest.py:471-487`): on `already_present`, read the persisted `.media` sidecar and raise `OBJECT_STORAGE_METADATA_CONFLICT` on mismatch (mirror `verify_existing_object`, lines 489-508).
4. Prefixes: unit conftest `rk7` → `recovery-bundle-` (`tests/unit/recovery/conftest.py:26`); integration `rk13`/`ob13` → `recovery-bundle-`/`object-store-` (`tests/integration/canonical_core/conftest.py:540,546,558`). POSIX leak fix (543-552): wrap the POSIX `yield` in the same `try/finally shutil.rmtree` the Windows branch uses.
5. `run_bounded_child_on_worker_loop` (568-583): `argv: Any` → `argv: Sequence[str]`; type the recording diagnostics events list concretely (drop `dict[str, Any]` at 661, 666).
6. `disposable_identity_database` (614-651): `live_acceptance_context` (test_live_r2_acceptance.py:257-288) consumes the fixture's existing harness/engine instead of building a second one; with the real `LocalFilesystemObjectStore` type flowing, delete the `cast(LocalFilesystemObjectStore, …)` at line 284. `test_identity_bootstrap_integration.py:59` keeps working unchanged.

- [ ] **Step 4: Run to verify pass**

```bash
uv run pytest tests/unit/recovery tests/unit/tools -q
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan-bundle-<date>
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-bundle-<date> uv run pytest tests/integration/canonical_core -m "local_stack and not r2_live" -q
bash .local/serve-live-ci.sh down
```

Expected: PASS everywhere.

- [ ] **Step 5: Commit** — `git add …` + `git commit -m "fix: close bundle finalize races and harness fidelity gaps"`

---

### Task 4: Snapshot-adapter precision (row 2026-08-15 §8)

**Files:**
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/backup_snapshot.py:126-130` (comment), `:133-151` (alembic constants), `:183-196` (`pending_writer_count_statement`)
- Modify: `docs/operations/canonical-core-recovery.md` (one lock_timeout sentence)
- Test: `tests/unit/postgresql_source_store/test_backup_snapshot.py` (new pins)

**Interfaces:** Produces: pending-writer count qualified to the `knowledge` schema; documented (not changed) NOWAIT/lock_timeout interplay and alembic location.

- [ ] **Step 1: Write the failing tests**

```python
def test_pending_writer_statement_qualifies_the_store_schema() -> None:
    sql, params = compile_statement(pending_writer_count_statement())
    assert "pg_namespace" in sql and "nspname" in sql
    assert params["schema"] == SOURCE_STORE_SCHEMA  # "knowledge"

def test_alembic_head_statement_names_its_location() -> None:
    # pins the version-table location constant text (see Step 3 ruling)
```

- [ ] **Step 2: Run** — `uv run pytest tests/unit/postgresql_source_store -q` → FAIL.

- [ ] **Step 3: Implement**

1. Namespace-qualify (spec-interpretation note for the handoff: the join stays spurious-abort-safe only):

```python
return sa.text(
    "SELECT count(*) FROM pg_locks"
    " JOIN pg_class ON pg_locks.relation = pg_class.oid"
    " JOIN pg_namespace ON pg_class.relnamespace = pg_namespace.oid"
    " WHERE pg_locks.locktype = 'relation'"
    " AND NOT pg_locks.granted"
    " AND pg_namespace.nspname = :schema"
    " AND pg_class.relname = ANY(:tables)"
)
```

Bind `{"tables": ..., "schema": SOURCE_STORE_SCHEMA}` at `observe_pending_writers` (395).
2. Alembic location RULING (differs from the spec's phrasing, record in handoff): `env.py` sets no `version_table_schema`, so Alembic's default-schema placement (`public`) is the live contract the acceptance gates prove; switching the read to `knowledge` would break. Fix = eliminate the undocumented magic: rename the constant to `_ALEMBIC_VERSION_TABLE_REFERENCE` with a provenance comment citing `migrations/env.py` default-schema behavior, and pin the statement text by test.
3. `SNAPSHOT_TRANSACTION_BOUND_STATEMENTS` (126-130): add the inline comment + one runbook sentence — `lock_timeout` guards only blocking-lock paths; the `NOWAIT` share locks fail immediately regardless (spec keeps both).

- [ ] **Step 4: Run + Commit** — `uv run pytest tests/unit/postgresql_source_store -q`; `git commit -m "fix: namespace-qualify pending-writer detection and document snapshot bounds"`

---

### Task 5: Recovery event-loop hygiene + documented rulings (row 2026-08-15 §9)

**Files:**
- Modify: `src/personal_os/recovery/bundle.py` (`_write_file_exclusively` 157-170, `_fsync_directory` 147-154, `_remove_staging` 424-434, `_staging_writer_context` 486-498, async callers 386-419, 507+), `verify_offline` call sites
- Modify: `src/personal_os/recovery/service.py:481` (`verify_bundle`), `:556-570` (0/0 comment), `:291-297`, `:484-490`
- Modify: `docs/operations/canonical-core-recovery.md` (object-cap bound + 0/0 convention)
- Test: `tests/unit/recovery/test_bundle.py`, `tests/unit/recovery/test_service.py`

**Interfaces:** Produces: all blocking filesystem work on bundle create/verify/restore coroutine paths runs via `asyncio.to_thread`; the ≤100 MiB buffered-copy bound (`MAXIMUM_OBJECT_SIZE_BYTES`, contracts.py:42) and the failed-restore 0/0 closed-sink convention are documented rulings (no behavior change).

- [ ] **Step 1: Write the failing test**

```python
async def test_offline_verify_does_not_block_the_event_loop(tmp_path, monkeypatch) -> None:
    # Build one valid bundle; monkeypatch bundle._stream_file_digest with a
    # wrapper that time.sleep(0.05)s (simulated slow fs) then delegates.
    verify_task = asyncio.create_task(service.verify_bundle(...))
    side_task = asyncio.create_task(asyncio.sleep(0))
    await asyncio.wait_for(side_task, timeout=2.0)
    assert verify_task.done() is False   # loop stayed responsive
    await verify_task
```

- [ ] **Step 2: Run** — `uv run pytest tests/unit/recovery -q` → FAIL (sync verify blocks the loop; `side_task` cannot complete first).

- [ ] **Step 3: Implement**

1. Route the sync helpers off the loop at their async call sites: `await asyncio.to_thread(_write_file_exclusively, …)`, `await asyncio.to_thread(_fsync_directory, …)`, `await asyncio.to_thread(_remove_staging, …)`; `service.verify_bundle` awaits `asyncio.to_thread(store.verify_offline, …)` (keeps `verify_offline`'s sync signature); same for the service copy/read calls at `service.py:441,681` (`to_thread` around the blocking open/read/write blocks — restructure `_copy_referenced_object`'s fallback write and `_stream_bundle_object`'s reads into small sync helpers executed via `to_thread`).
2. Keep the buffered whole-object copy (bounded by `MAXIMUM_OBJECT_SIZE_BYTES`): add the runbook sentence naming the 100 MiB per-object bound and the chunked-restore path.
3. 0/0 ruling: comment at `service.py:562-563` ("failed restores deliberately report 0/0: mid-failure totals are not trustworthy; the closed sink convention") + one runbook line; same comment at 291-297 and 484-490.

- [ ] **Step 4: Run + Commit** — `uv run pytest tests/unit/recovery tests/unit/tools -q`; `git commit -m "fix: move recovery file io off the event loop and document bounds"`

---

### Task 6: CLI composition documented rulings (row 2026-08-15 §11)

**Files:**
- Modify: `tools/canonical_core_operations.py` (comment at the compose helpers ~805, ~901, ~1365)
- Modify: `pyproject.toml:184-188` (extend the `canonical-core-test` comment)
- Modify: `docs/operations/canonical-core-recovery.md` (one sentence)

- [ ] **Step 1: Add the rulings** — at each compose helper's engine line: "The engine opens no connection at compose time (lazy pool); disposal in run()'s finally covers every executed path — compose-time disposal is deliberately absent (2026-08-15 §11 ruling)." In pyproject: "…deliberately standalone: composing the local-stack suite into `verify` would slow every gate run (2026-08-15 §11 ruling)." Runbook names both rulings. No code change, no test.
- [ ] **Step 2: Verify + Commit** — `uv run poe format-check && uv run pytest tests/unit/tools -q`; `git commit -m "docs: record the canonical-core CLI composition rulings"`

---

### Task 7: Lockout audit action (row 2026-08-16 §4)

**Files:**
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/authentication_credentials.py:94-106` (constant), new `record_locked_login_rejection` near `record_login_failure` (325-368)
- Modify: `src/personal_os/authentication/sessions.py:742-748` (locked branch calls it)
- Modify: `src/personal_os/authentication/ports.py` (port method) + offline fake if one exists
- Test: `tests/unit/postgresql_source_store/test_authentication_credentials.py`, `tests/integration/authentication/test_password_session_transactions.py`

**Interfaces:**
- Produces: `LOGIN_LOCKED_OUT_AUDIT_ACTION: Final[str] = "authentication.login_locked_out"`; `CredentialStore.record_locked_login_rejection(command) -> None` — writes ONLY the audit row (action `login_locked_out`, result `rejected`, reason_code `None`, same trusted-identity gating as `_append_audit_event` callers).

- [ ] **Step 1: Write the failing tests**

Unit (ScriptedEngine pattern; the file's audit filter is at 326-335):

```python
async def test_locked_login_rejection_audits_the_dedicated_action() -> None:
    # locked credential row scripted; call record_locked_login_rejection
    # -> exactly one audit_events insert with action == LOGIN_LOCKED_OUT_AUDIT_ACTION
    #    and result == AUDIT_RESULT_REJECTED

async def test_locked_rejection_writes_no_throttle_row() -> None:
    # no authentication_throttle_buckets insert
```

Integration: `harness.audit_rows("authentication.login_locked_out")` after a login attempt against a locked account returns one row; a wrong-password attempt on an UNlocked account still yields exactly `authentication.login_rejected`. Error-case trio per spec: locked+correct, locked+wrong (both → `login_locked_out`), unlocked+wrong (→ `login_rejected`).

- [ ] **Step 2: Run** — `uv run pytest tests/unit/postgresql_source_store -q` → FAIL.

- [ ] **Step 3: Implement** — constant in the 94-106 block; store method reusing `_append_audit_event` (990-1020) with the credential row already fetched by the caller; `LoginService.login` locked branch (sessions.py:742-748) calls it before returning `LoginOutcome(public_error=AUTHENTICATION_RATE_LIMITED, …)` (best-effort: wrap in the existing transaction runner; a store failure must not mask the rate-limited outcome). Extend the store port + any offline fake.

- [ ] **Step 4: Run + Commit** — unit + `poe authentication-test` on a CI project; `git commit -m "feat: audit locked logins with a dedicated action token"`

---

### Task 8: Reset CLI edges (row 2026-08-16 §5)

**Files:**
- Modify: `apps/api/src/api_runtime/authentication_commands.py:131-141` (`read_emergency_reset_confirmation`), `:277-319` (`_reset_web_authentication`)
- Test: `tests/unit/api_runtime/test_authentication_commands.py`, `tests/integration/authentication/test_credential_commands.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_confirmation_prompt_does_not_echo(monkeypatch) -> None:
    # monkeypatch getpass.getpass; input() must NOT be called
def test_stdin_eof_maps_to_a_typed_abort(monkeypatch) -> None:
    # getpass raises EOFError -> CredentialCommandInputError("reset
    # confirmation input closed") -> exit 2, message names input closed
    # (never internal_error / 70)
```

Integration: `test_reset_on_unenrolled_workspace_…` (reset before any enrollment closes counts at zero — extend the existing `test_reset_before_any_surfaces_exist_closes_every_count_at_zero`, line 436) and `test_status_of_archived_workspace_is_authentication_failed` (archive a workspace via the existing harness seam; assert the closed `AUTHENTICATION_FAILED` outcome and exit 78).

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** — swap `input(` (line 138) for `getpass.getpass(` (prompt text keeps "type the username to confirm"); wrap the two prompt reads in `try/except EOFError: raise CredentialCommandInputError("reset confirmation input closed") from None` (exit 2 via the existing arm at 207-225).

- [ ] **Step 4: Run + Commit** — `uv run pytest tests/unit/api_runtime/test_authentication_commands.py -q` + integration on CI project; `git commit -m "fix: close reset CLI echo and eof edges"`

---

### Task 9: Throttle-bucket insert race + riders (row 2026-08-16 §7)

**Files:**
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/authentication_credentials.py:827-896` (insert 871-882), `device_authorization_store.py:683-742` (insert 717-728), `totp_store.py:754-796` (insert ~790)
- Modify: `apps/api/src/api_runtime/session_routes.py:153-166`, `totp_routes.py:130-145`; `src/personal_os/authentication/sessions.py:573-579` (`LoginOutcome`) and the login decision site `:716`
- Modify: `apps/api/src/api_runtime/authentication_composition.py:334,344`
- Test: `tests/unit/postgresql_source_store/` (three stores), `tests/integration/authentication/test_password_session_transactions.py` (race), `tests/unit/api_runtime/` (route clock)

**Interfaces:**
- Produces: first-insert uses `INSERT … ON CONFLICT ON CONSTRAINT uq_authentication_throttle_buckets__kind_hash DO NOTHING` with a re-`SELECT … FOR UPDATE` fallback when rowcount is 0; `LoginOutcome.limited_at: datetime | None = None` carrying the decision clock; one shared `KeyringTotpSecretCodec` instance.

- [ ] **Step 1: Write the failing tests**

Integration race (the decisive RED):

```python
async def test_concurrent_cold_bucket_first_failures_settle_one_row(harness) -> None:
    # two record_login_failure transactions on the same cold bucket via
    # asyncio.gather on separate connections -> BOTH succeed, exactly one
    # throttle row, strike count == 2, no internal_error
```

Unit riders: route test asserting `_rate_limited_login` computes retry-after from `outcome.limited_at` (no second `database_now` call — count calls on a scripted service); composition test asserting `KeyringTotpSecretCodec` constructed once (monkeypatch counting).

- [ ] **Step 2: Run** → FAIL (loser escapes as 23505→internal_error today). **Step 3: Implement** — upsert pattern in all three stores:

```python
    inserted = await connection.execute(
        sa.insert(authentication_throttle_buckets)
        .values(...)
        .on_conflict_do_nothing(
            constraint="uq_authentication_throttle_buckets__kind_hash"
        )
    )
    if inserted.rowcount == 0:
        # lost the cold-insert race: re-select under the lock and continue
        # through the existing-row update path
```

`LoginOutcome` gains `limited_at`; login sets it from the single `database_now` (sessions.py:716); `_rate_limited_login` (153-166) and `_rate_limited_json` (totp 130-145) use it instead of a fresh `database_now()`. Composition: `secret_codec = KeyringTotpSecretCodec(crypto, keyring)` once, passed to both `TotpStore` (334) and `TotpService` (344).

- [ ] **Step 4: Run + Commit** — `uv run poe authentication-test` (with CI project per Global Constraints); `git commit -m "fix: close the throttle cold-insert race and 429 clock seams"`

---

### Task 10: Grant-path hardening batch (row 2026-08-16 §8)

**Files:**
- Modify: `src/personal_os/authentication/device_authorization.py:191-197` (user code), `:310-325` (docstring), `:530-539` (dead attr), `:573-606` (cap + cold path)
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/device_authorization_store.py:243-274` (single-transaction cold path)
- Modify: `apps/api/src/api_runtime/authentication_composition.py:~2150` (drop `session_policy` threading)
- Test: `tests/unit/authentication/test_device_authorization.py`, `tests/unit/postgresql_source_store/test_device_authorization_store.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_user_code_generation_rejects_biased_bytes() -> None:
    # feed random_bytes yielding [248, 255, 5, ...] -> first two rejected,
    # code built from the unbiased remainder; charset pin unchanged
def test_live_grant_cap_rejection_records_a_throttle_attempt() -> None:
    # window at MAXIMUM_LIVE_GRANTS_PER_CLIENT_INSTANCE -> record_throttle_attempt called
async def test_cold_bucket_and_grant_insert_share_one_transaction() -> None:
    # scripted store: bucket resolve-or-insert and grant insert in ONE
    # connection/transaction (adapter-level pin)
```

Plus: `login_surface_refuses…` n/a — remove-attr test = import surface stays green; docstring correction is prose (pinned by an existing behavior test if one pins state-wins — add one if absent: terminal-state grant past `expires_at` resolves `DEVICE_AUTHORIZATION_STATE_INVALID`, matching code not docstring).

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** — unbiased mapping (rejection-sample bytes `>= 248`; alphabet length 31, `31*8=248`); `record_throttle_attempt` in the cap branch (599-606) mirroring 591-598; extend `insert_pending_grant` to resolve-or-insert the cold bucket inside its transaction (reusing Task 9's upsert), with `create_grant` no longer pre-resolving lock-free (573-576); delete `self.session_policy` (537-539) and the composition threading; rewrite the `resolve_terminal_rejection_code` docstring (313-318) to state-check-wins.

- [ ] **Step 4: Run + Commit** — `uv run poe authentication-test` (CI project); `git commit -m "fix: harden grant cap throttling, cold insert, and user-code bias"`

---

### Task 11: Web auth-state hygiene (row 2026-08-16 §10)

**Files:**
- Modify: `apps/web/src/features/authentication/LoginForm.tsx:39,83-87,125-133,157-159`; `TotpChallenge.tsx:84-101,210-217,245-250`; `SecurityPanel.tsx:206-234,314-359`; `DeviceApproval.tsx:68,137,304`
- Delete: `apps/web/src/app/bootstrap-copy.ts` + `apps/web/src/app/bootstrap-copy.test.ts`
- Modify: `apps/web/src/proxy.ts:9,23`
- Test: sibling `.test.tsx` files per component

- [ ] **Step 1: Write the failing tests** — `LoginForm.test.tsx`: after `recovery_limited`/`onRecoveryLimited` transitions, a rerender asserting the password state is cleared (assert the TotpChallenge `password` prop is `""` via a spy client); `TotpChallenge.test.tsx`: dismissal failure (client returns `{ok:false}`) renders the closed error and does NOT call `onSkipped`; the unmount effect is gone (no assertion fires — covered by removing dead code, existing tests stay green); `SecurityPanel.test.tsx`: exactly one current-password input rendered in re-auth mode; `DeviceApproval.test.tsx`: terminal close clears `challengePassword`. Proxy: no `x-csp-nonce` header set (unit test over the proxy helper or drop the constant + dead line and rely on type-check).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** — `setPassword("")` on the two recovery transitions (and after `submitRecovery` resolves); `skip()` handles the dismissal result (error message + no `onSkipped` on failure); delete the no-op unmount `useEffect` (210-217); SecurityPanel renders the re-auth password input OR the change-form field, never both; clear `challengePassword` in `closeAsTerminal`; delete `bootstrap-copy.ts` + test; remove `x-csp-nonce` constant and the `requestHeaders.set` line (proxy.ts:9,23).
- [ ] **Step 4: Run + Commit** — `pnpm --filter @workspace/web-runtime test && … run type-check && … run lint`; `git commit -m "fix: clear held passwords and close auth-state hygiene gaps"`

---

### Task 12: Web a11y/UX batch (row 2026-08-16 §11)

**Files:**
- Modify: `apps/web/src/features/devices/DeviceRevokeDialog.tsx:114-125`; `DeviceApproval.tsx:98-102,127-129,222-252,309-355`
- Modify: `apps/web/src/api/authentication-client.ts:83-94` (export `unwrapEnvelope` + `REQUEST_UNAVAILABLE_ERROR`); `apps/web/src/features/devices/device-administration-client.ts:50-71,86-94` (import instead)
- Test: `DeviceRevokeDialog.test.tsx`, `DeviceApproval.test.tsx`, client tests

- [ ] **Step 1: Write the failing tests** — focus trap: Tab from the last focusable child cycles to the first, Shift+Tab inverts, close restores focus to the opener; abandon path: re-auth step renders a Cancel that returns to the context step without approving; rate-limited lookup: terminal view renders a Retry control that re-runs the lookup; query preserved: after the hash-clearing `replaceState`, `window.location.search` survives. Client: `device-administration-client` imports the shared helper (type-level + one behavior test each side).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** — minimal focus trap (`useEffect` keydown handler over the dialog's focusable elements + restore on unmount, no new dependency); Cancel button in the re-auth render (318-355) wired to a `setStep("context")`-returning abandon handler; Retry affordance in the terminal-error render (309-316) calling the existing lookup; `replaceState(null, "", window.location.pathname + window.location.search)` (127-129); export-and-import the envelope helpers.
- [ ] **Step 4: Run + Commit** — web gates; `git commit -m "fix: revoke focus trap, re-auth abandon, lookup retry, query and envelope dedupe"`

---

### Task 13: Plugin session hygiene (row 2026-08-16 §12)

**Files:**
- Modify: `apps/obsidian-plugin/src/authentication/device-authorization.ts:83-127,173-202,240-258,354`; `token-session.ts:139,190`; `contracts.ts:35,53-91`; `plugin.ts:245-279,548-565,687-692`; `settings-tab.ts:237-270`
- Test: `device-authorization.test.ts`, `token-session.test.ts`, `contracts.test.ts:74-109`, `plugin.test.ts:1041`

- [ ] **Step 1: Write the failing tests**

```typescript
it("surfaces rate-limited creation with retry guidance", …)   // state change carries the closed rate-limited code + retryAfterSeconds, not "offline"
it("offers a retry affordance while offline with an active credential", …)  // new command enabled
it("login refuses to overwrite an active record", …)          // typed refusal when the record is active
it("reconcileCrashWindow saveData rejection is caught", …)    // closed reason recorded, onload continues
it("normalizeSettings preserves a valid stored record name", …)  // UPDATES the plugin.test.ts:1041 pin
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** — `#surfaceCreationFailure` branches `authentication_rate_limited` to its own state detail (retry seconds rendered); new "Retry connection" command re-invoking the session refresh (offline + active credential → enabled; `canLogin` gating unchanged); type guards (`isDeviceAuthError(error)` helper) replacing the four `as` casts; delete `DEVICE_AUTH_ERROR_CODES`/`LOCAL_ERROR_CODES` (contracts.ts:74-91); `normalizeSettings` keeps `loadedRecordName` when valid (plugin.ts:276); `plugin.ts:548` wraps `reconcileCrashWindow()` in `try/catch` routing to the journal failure reporter with a closed reason; `login()` reads the record state first and refuses overwrite with a typed error (secret-storage-record read + closed code, `canLogin` stays as UI gate).
- [ ] **Step 4: Run + Commit** — plugin vitest/tsc/lint/build; `git commit -m "fix: plugin session label, recovery, overwrite guard, and cleanup"`

---

# CHECKPOINT 2 — before the final Phase 2 handoff

### Task 14: Authentication acceptance-test polish (row 2026-08-16 §14)

**Files:**
- Modify: `tests/end_to_end/authentication/full-device-onboarding.spec.ts:241-242,268-269` (+ new shared credential fixture module, e.g. `tests/end_to_end/authentication/e2e-credentials.ts`)
- Modify: `tests/end_to_end/authentication/device-administration.spec.ts`, `web-security.spec.ts`, `tests/end_to_end/exclusion_policy/policy-publication.spec.ts` (13 password literals)
- Modify: `tests/contract/api/test_authentication_leakage.py:224-232,281,284-291,480-492`; `tests/integration/authentication/test_authentication_key_rotation.py:100-101,386-395,505`; `tests/contract/test_ci_security.py:420-422`
- Modify: `docs/operations/web-authentication-and-device-authorization.md:181-199`

- [ ] **Step 1: Write the failing/adjusted tests and polish**
  1. E2E: replace the two mock-vs-mock assertions with behavior assertions against the captured request log (the spec's contract-fidelity capture) — e.g. the browser actually navigated the `verification_uri_complete` of the grant the capture recorded, and the exchange used that grant id.
  2. New `e2e-credentials.ts` exporting the single accepted-login password + user fixtures; all 13 literals across the four spec files import it (spelling stays per-suite value; ONE definition per value).
  3. Leakage: fix the `rendered_offline_state` docstring (491-492) to state the real per-table column policy; register cookie sentinels for `reauthenticate-rejected` (284-291) and `password-change` (480-487) surfaces by calling `capture_cookie_sentinels` after each (the journey observes both setting cookies).
  4. Key-rotation: delete the dead `_RETIRED_MASTER_KEY` first assignment (100-101, keep the `bytes(range(64,96))` value with the existing name); add the account predicate to `grant_derivation_key_ids` (386-395) so assertion 505 is order-independent; drop the in-line ordering caveat at 497-498.
  5. Runbook reproduce script (181-199): print all five stats (min/median/mean/p95/max) matching the gate's evidence format.
  6. `test_ci_security.py:420-422`: remove the inert `re.MULTILINE` flag.
- [ ] **Step 2: Run** — `pnpm exec playwright test tests/end_to_end/authentication` + `uv run pytest tests/contract/test_ci_security.py -q` (+ leakage contract on CI project). Expected: PASS.
- [ ] **Step 3: Commit** — `git commit -m "test: tighten authentication acceptance assertions and sentinels"`

---

### Task 15: Object-storage shielded cancellation (row 2026-08-14, retirement ruling 2)

**Files:**
- Modify: `packages/r2-object-storage/src/r2_object_storage/adapter.py:109-124` (`_run_shielded`)
- Test: `tests/contract/object_storage/test_r2_adapter_resource_contract.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_raising_cleanup_does_not_swallow_caller_cancellation() -> None:
    async def raising_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    task = asyncio.ensure_future(_run_shielded_for_test(raising_cleanup, sink))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sink.failures  # received the cleanup failure (closed token path)
```

- [ ] **Step 2: Run** → FAIL (RuntimeError replaces CancelledError today). **Step 3: Implement** — add an optional failure sink parameter (default `None`; the three call sites at 828/848/850 pass their existing failure-recording hook where reachable, else `None`):

```python
async def _run_shielded(
    cleanup: Coroutine[object, object, None],
    *,
    on_cleanup_failure: Callable[[BaseException], None] | None = None,
) -> None:
    task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException as cleanup_error:
            # A failing cleanup must never mask the caller's cancellation.
            if on_cleanup_failure is not None:
                on_cleanup_failure(cleanup_error)
        raise
```

- [ ] **Step 4: Run + Commit** — `uv run pytest tests/contract/object_storage tests/unit/object_storage -q`; `git commit -m "fix: preserve cancellation when shielded cleanup raises"`

---

### Task 16: Object-storage test-hygiene batch (row 2026-08-14 §11)

**Files:**
- Modify: `packages/r2-object-storage/src/r2_object_storage/adapter.py:558,719,831,897` (asserts); `settings.py:51,56,105,112-117`
- Modify: `src/personal_os/diagnostics/logging.py:59` (public accessor beside the marker)
- Modify: `tests/contract/object_storage/test_r2_adapter_resource_contract.py:248-250,302-319`; `test_r2_adapter_contract.py:135-137,167-196`; `test_r2_multipart_staging_contract.py:113-115,139-160`; `tests/integration/r2_object_storage/conftest.py:84-86`; `tests/unit/object_storage/test_r2_settings.py`

- [ ] **Step 1: Adjust tests first (they drive each fix)**
  1. Root logger: each fixture/harness site saves handlers+level before adding the `NullHandler` and restores in `finally` (4 sites above).
  2. `run_bounded` (302-319): wrap the result-collection in `try/except BaseException: cancel-and-gather pending (return_exceptions=True); raise`.
  3. Public accessor: `diagnostics/logging.py` gains `def diagnostic_schema_record(record: logging.LogRecord) -> Mapping[str, object] | None` reading the marker; both `_DiagnosticRecordCapture` copies use it (delete the `getattr(record, "_diagnostic_schema_record")` reads).
  4. Settings tests: pin per-platform default behavior — on POSIX the documented default is `Path("/run/secrets")`; on win32 the loader requires an override (tests assert the documented contract; field docstring states "Linux serve contract — Windows hosts always set KNOWLEDGE_SECRET_ROOT").
  5. Adapter asserts → explicit invariant raises (`if hashed is None: raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from None`-shaped, matching the module's existing internal-error mapping; four sites).
  6. Drop the redundant `^$` anchors (settings.py:51,56 — `.fullmatch` at 122,129 makes them no-ops).
- [ ] **Step 2: Run** — `uv run pytest tests/contract/object_storage tests/unit/object_storage -q` → PASS after implementing 3-6 (RED first for the accessor and invariant-raise tests, which you write in Step 1).
- [ ] **Step 3: Commit** — `git commit -m "test: close object-storage hygiene batch"`

---

### Task 17: Source-publication test hardening (row 2026-08-14 §6)

**Files:**
- Modify: `tests/integration/source_publication/test_query_plans.py:78-102,339-344`; `test_large_fixture_concurrency.py:60,282-283`; `test_cancellation.py:125-135,216`; `test_publication_concurrency.py:58`; `test_idempotency_preflight.py:399-429`
- Modify: `tests/contract/source_publication/test_no_public_api.py:85-91,275-294,364-369`
- Modify: `tests/unit/sources/fakes.py:76`; `packages/postgresql-source-store/src/postgresql_source_store/settings.py:58-60` + `engine.py:23-29,41-45`; `tests/unit/postgresql_source_store/test_settings.py:25-26,30,220-222`
- Modify: `tests/integration/source_publication/test_small_file_operations.py:207`; `test_ambiguous_commit.py:218`

- [ ] **Step 1: Adjust/write the tests and fixes (one bullet = one commit-able unit, all in this task)**
  1. Seq matcher (339-344): `str(node["Node Type"]).endswith("Seq Scan")`; add a unit pin with a `Parallel Seq Scan` node flagged and `Index Scan` not.
  2. Remove `test_large_fixture_concurrency.py:283`; convert `test_cancellation.py:216` + `_await_pool_checked_in` (125-135) to the numeric `pool.checkedout() == 0`.
  3. `no_public_api` tokens (85-91): replace `"publication"` and `"/sources"` with route-shaped fragments (`"/api/publications"`, `"/api/sources"`); update the near-miss pin (364-369) to the new expected trip set; masking helpers unchanged.
  4. AST scanner (78-102): fail closed on unsupported name shapes — a `create_index`/constraint whose name is not an `ast.Constant` raises `ValueError("unsupported index-name shape in baseline migration")`; pin with tests for variable, f-string, and `name=` keyword forms.
  5. Margins: `CONCURRENCY_TIMEOUT_SECONDS = 180.0` (test_publication_concurrency.py:58); `GATHER_DEADLINE_SECONDS = 360.0` (test_large_fixture_concurrency.py:60) — assertions unchanged.
  6. `test_idempotency_preflight.py:425-429`: pin `safe_details["reason"]` value and the audit row's `workspace_id`/`target_id` (mirror 388-390); add the zero-mutation pin to the update-replay test (row/version counts unchanged across an exact replay).
  7. Delete `MAXIMUM_RECEIPT_AGE` in `tests/unit/sources/fakes.py:76` (keep the live `publication.py:85` one); wire `engine.py:41-45` to import `LOCK_TIMEOUT_SECONDS`/`STATEMENT_TIMEOUT_SECONDS`/`IDLE_IN_TRANSACTION_SESSION_TIMEOUT_SECONDS` from `settings.py:58-60` (same values — removes the dead-constant/literal duplication); `test_settings.py` pins updated to the import.
  8. Cross-object private reaches: expose `preflight_harness.engine` property (fixes `test_small_file_operations.py:207`); replace `store._acknowledgement_lost_once` (test_ambiguous_commit.py:218) with a behavioral assertion (the replay-visible acknowledgement state); add the conftest comment that subclass `super()._insert_*` harness hooks are the house pattern.
- [ ] **Step 2: Run** — `uv run pytest tests/integration/source_publication tests/contract/source_publication tests/unit/sources tests/unit/postgresql_source_store -q` (integration on a CI project). Expected: PASS.
- [ ] **Step 3: Commit** — `git commit -m "test: harden source-publication plans, scans, and fixtures"`

---

### Task 18: Fingerprint provenance note + hex64 ruling (row 2026-08-14 §7)

**Files:**
- Modify: `tests/unit/sources/test_source_fingerprint.py` + `test_safe_diff_hash.py` (module docstrings)
- Modify: `src/personal_os/sources/fingerprint.py:40-68` (intra-module dedupe)

**Spec-interpretation note (record in handoff):** research found a THIRD digest value object already exists (`ContentDigest`, object_storage/keys.py:24-39) beside `RequestFingerprint` and `SafeDiffHash` — the row's condition has fired. Ruling implemented: dedupe ONLY within `sources/fingerprint.py` (one `_parse_hex64` helper shared by the two classes there); cross-domain consolidation with `object_storage` is refused by the row-51 precedent (repetition over cross-domain abstraction while domains keep closed vocabularies; also avoids a new cross-domain import boundary).

- [ ] **Step 1:** Docstrings on both test modules: "Fixture digests derive from the design spec's worked examples in `tests/fixtures/source_publication/fingerprint_golden.json` (pinned UUIDs, 64×'a'/64×'b' content digests); regenerate expectations only from the spec, never from the implementation."
- [ ] **Step 2:** Extract `def _parse_hex64(value: str, *, length: int = 64) -> str` in `fingerprint.py`; both `parse` classmethods delegate (identical validation semantics; existing golden tests prove behavior unchanged).
- [ ] **Step 3: Run + Commit** — `uv run pytest tests/unit/sources -q`; `git commit -m "refactor: share hex64 parsing in sources fingerprints and note fixture provenance"`

---

### Task 19: Canonical-core acceptance polish (rows 2026-08-15 §4, §14)

**Files:**
- Modify: `tests/unit/postgresql_source_store/test_canonical_read.py:207-209` (rename only: `test_lookup_statement_joins_three_relations`)
- Modify: `tests/contract/canonical_core/test_composition_boundaries.py:173-182` (rename: `test_no_database_url_or_pgpassword_in_canonical_core_tools`)
- Modify: `tools/canonical_core_operations.py:1049-1050,1142,1275,1288,1300,1306`
- Test: `tests/unit/tools/` (clock seam)

- [ ] **Step 1: Write the failing test** — `run_phase_one_acceptance` accepts an injectable `monotonic_clock: Callable[[], float] = time.monotonic`; the test passes a fake clock advancing deterministically and asserts `duration_ms` fields equal the fake deltas (today they bypass the seam → RED).
- [ ] **Step 2: Implement** — thread the seam through `_elapsed_ms_since` (1049-1050) and the three `completed_fields["duration_ms"]` sites (1288, 1300, 1306); default behavior unchanged.
- [ ] **Step 3: Run + Commit** — `uv run pytest tests/unit/tools tests/contract/canonical_core -q` + the two renames; `git commit -m "test: honest canonical-core test names and clock-seamed durations"`

---

### Task 20: Docs, BACKLOG retirement, handoff

**Files:**
- Modify: `docs/handoff/BACKLOG.md` (remove exactly the 22 rows: 2026-08-14 object-storage ×2 + source-publication ×3; 2026-08-15 canonical-core ×9; 2026-08-16 web-auth ×7 + acceptance-tests ×1)
- Modify: `docs/20-IMPLEMENTATION_PLAN.md` (only if it names any of these gates)
- Create: `docs/handoff/2026-08-30-child-nine-hygiene-retirement.md`

- [ ] **Step 1: Final verification**

```bash
uv run poe verify && uv run poe api-contract-check
pnpm --filter @workspace/web-runtime test && pnpm --dir apps/obsidian-plugin exec vitest run && pnpm --dir apps/obsidian-plugin run type-check && pnpm --dir apps/obsidian-plugin run build
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan-final-<date>
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-final-<date> uv run poe authentication-test
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-final-<date> uv run pytest tests/integration/source_publication tests/integration/canonical_core -m "local_stack and not r2_live" -q
bash .local/serve-live-ci.sh down
git diff --check && git status --short
```

- [ ] **Step 2: Handoff content** — final SHA per checkpoint; gate evidence; interpretive decisions: (a) alembic-version location documented-not-switched (Task 4); (b) whole-batch drain already existed — pinned, not changed (Task 1); (c) policy-worker runtimes share the dispatcher's dispose/close gap — observed, out of this row's scope, NOT re-indexed (same domain as the 2026-08-24 policy-workers rows); (d) third-digest-type condition fired — intra-module dedupe + row-51-precedent refusal of cross-domain extraction (Task 18); (e) the two documented code-stands rulings (Task 6) and the 0/0 closed-sink ruling (Task 5). Remove the 22 rows; nothing newly deferred.
- [ ] **Step 3: Commit** — `git commit -m "docs: retire the child-nine hygiene backlog rows"`

---

## Self-Review (completed)

- **Spec coverage:** 22/22 rows mapped — cp1: T1 (dispatcher), T2 (admission), T3 (§6+§12+§13), T4 (§8), T5 (§9), T6 (§11), T7-T10 (web-auth §4/§5/§7/§8), T11-T13 (§10/§11/§12); cp2: T14 (§14), T15-T16 (object-storage), T17-T18 (§6/§7), T19 (§4+§14), T20 (retirement). The spec's three documented code-stands rulings land in T5/T6/T18.
- **Placeholders:** none — every step names exact files/lines from the 2026-08-30 research pass and shows the change shape.
- **Type consistency:** `CANONICAL_RECOVERY_ADMISSION_REFUSED` (T2 definition = T2 tests); `LOGIN_LOCKED_OUT_AUDIT_ACTION`/`record_locked_login_rejection` (T7 both sides); `LoginOutcome.limited_at` (T9); `_run_shielded(..., on_cleanup_failure=…)` (T15); upsert constraint id shared by T9/T10 device-store paths.
