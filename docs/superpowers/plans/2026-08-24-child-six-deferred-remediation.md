# Child 6 Deferred Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use \`superpowers:subagent-driven-development\` (recommended) or \`superpowers:executing-plans\` to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Resolve the six Child 6-related deferred-work streams without weakening canonical state, privacy, or closed-token observability.

**Architecture:** The server terminalizes a claimed small-file upload operation when a typed business rejection occurs, while the plugin retains its terminal queue landing and durable diagnostics trail. Database classification remains constraint-specific, and a shared JSON golden locks Python/TypeScript parity. A dedicated diagnostics-surface deliverable proves every changed closed failure is readable when it lands.

**Tech Stack:** Python 3.14 through \`uv\`, SQLAlchemy/PostgreSQL, FastAPI contract fixtures, TypeScript strict, Obsidian plugin, Vitest, sql.js, Ruff, mypy.

**Spec:** \`docs/superpowers/specs/2026-08-24-child-six-deferred-remediation-design.md\`

## Global Constraints

- PostgreSQL and R2 remain canonical; plugin SQLite is rebuildable journal state and never creates canonical identity.
- Start every behavior change with a focused failing test, then write the smallest code that makes it pass.
- No new dependency, public route, migration, background daemon, raw diagnostic field, or free-form reason vocabulary.
- Every new or changed closed error path must surface its exact closed reason token in the trail, status/settings, or structured closed log; never swallow it silently.
- The diagnostics-surface task below must ship with behavior changes; it cannot be deferred to Phase 10.
- Diagnostics contain only closed tokens, counts, timestamps and opaque IDs. They never contain paths, content, digests, credentials, SQLSTATE, SQL, constraint names, exception text, hostnames, or URLs.
- Update OpenAPI/generated client/contract tests/living docs in the same task if public contract changes.
- Use only \`uv\` for Python. The pinned Python 3.14/Ruff output \`except A, B:\` is valid repository style.
- Before live work, read \`.local/RESTART.md\`; use only its approved scripts and a disposable \`knowledge-ci-*\` project.

---

## File structure

| File | Responsibility |
|---|---|
| \`src/personal_os/small_file_sync/ports.py\` | Typed rejection terminalization port. |
| \`src/personal_os/small_file_sync/service.py\` | Persist terminal typed receive rejections, re-raise unchanged error. |
| \`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py\` | Guarded \`receiving → failed\` operation transition. |
| \`packages/postgresql-source-store/src/postgresql_source_store/error_mapping.py\` | Closed SQLSTATE-only classification. |
| \`tests/unit/postgresql_source_store/test_small_file_sync_operations.py\` | Terminal-state/replay tests. |
| \`tests/unit/postgresql_source_store/test_error_mapping.py\` | Known and unrelated integrity-failure tests. |
| \`tests/fixtures/small_file_sync/wire-golden.json\` | Cross-language closed wire corpus. |
| \`tests/contract/small_file_sync/test_wire_contract.py\` | Corpus hash/Python replay. |
| \`apps/obsidian-plugin/src/journal/{queue-driver,sync-diagnostics-trail,sync-diagnostics-export,sync-self-check}.ts\` | Terminal landing and closed diagnostic surfaces. |
| matching plugin \`*.test.ts\` | Trail/settings/self-check RED→GREEN tests. |
| \`docs/operations/source-locator-tombstone-lifecycle.md\` | Sanitized physical Mobile evidence. |
| \`docs/handoff/BACKLOG.md\` | Remove only completed deferred rows. |

### Task 1: Persist typed upload rejections as terminal canonical operation state

**Files:**

- Modify: \`src/personal_os/small_file_sync/ports.py\`
- Modify: \`src/personal_os/small_file_sync/service.py\`
- Modify: \`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py\`
- Test: \`tests/unit/postgresql_source_store/test_small_file_sync_operations.py\`
- Test: \`tests/unit/postgresql_source_store/test_publication_store.py\`

**Interfaces:**

- Produce \`SmallFileUploadOperationStore.record_bound_terminal_failure(bound, error_code, diagnostic_context) -> None\`.
- It accepts a bound \`receiving\` operation and an existing closed \`ErrorCode\`, writes \`STATE_FAILED\` plus that safe code, and is idempotent only for the identical bound/code pair.
- \`SmallFileSyncService.receive_content()\` catches only a non-retryable typed \`ApplicationError\` after receive claim, persists it, then re-raises the same error.

- [ ] **Step 1: Write the RED adapter tests.**

\`\`\`python
async def test_typed_rejection_moves_receiving_operation_to_failed() -> None:
    await store.record_bound_terminal_failure(bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, context)
    assert connection.operation_state == STATE_FAILED
    assert connection.safe_error_code == "source_locator_conflict"

async def test_terminal_failure_replay_is_idempotent() -> None:
    await store.record_bound_terminal_failure(bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, context)
    await store.record_bound_terminal_failure(bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, context)
    assert connection.failed_write_count == 1
\`\`\`

- [ ] **Step 2: Run RED.**

Run: \`uv run pytest tests/unit/postgresql_source_store/test_small_file_sync_operations.py -q\`

Expected: FAIL because no typed-failure terminal method exists.

- [ ] **Step 3: Add the port and guarded adapter statement.**

\`\`\`python
async def record_bound_terminal_failure(
    self,
    bound: SmallFileBoundOperation,
    error_code: ErrorCode,
    diagnostic_context: DiagnosticContext,
) -> None: ...
\`\`\`

Bind the existing operation fence. Accept only \`STATE_RECEIVING\` or an identical existing \`STATE_FAILED\`; reject other prior terminal records with the existing closed state error. Store the registry token only.

- [ ] **Step 4: Write the service RED test, then catch minimally.**

\`\`\`python
with pytest.raises(ApplicationError) as raised:
    await service.receive_content(operation_token, byte_stream, context)
assert raised.value.code is ErrorCode.SOURCE_LOCATOR_CONFLICT
assert operation_store.recorded_failure is ErrorCode.SOURCE_LOCATOR_CONFLICT
\`\`\`

Retryable and unknown failures must retain their current retry behavior and must not be terminalized.

- [ ] **Step 5: Run focused GREEN gates.**

Run: \`uv run pytest tests/unit/postgresql_source_store/test_small_file_sync_operations.py tests/unit/postgresql_source_store/test_publication_store.py tests/unit/small_file_sync -q\`

Expected: PASS; typed 409 ends in \`failed\`, replay is idempotent, unrelated failures remain non-terminal.

- [ ] **Step 6: Commit.**

\`\`\`powershell
git add src/personal_os/small_file_sync/ports.py src/personal_os/small_file_sync/service.py packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py tests/unit/postgresql_source_store/test_small_file_sync_operations.py tests/unit/postgresql_source_store/test_publication_store.py tests/unit/small_file_sync
git commit -m "fix: terminalize typed upload rejections"
\`\`\`

### Task 2: Narrow database classification and pin the shared wire landing

**Files:**

- Modify: \`packages/postgresql-source-store/src/postgresql_source_store/error_mapping.py\`
- Modify: \`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py\`
- Test: \`tests/unit/postgresql_source_store/test_error_mapping.py\`
- Modify: \`tests/fixtures/small_file_sync/wire-golden.json\`
- Modify: \`tests/contract/small_file_sync/test_wire_contract.py\`
- Modify: \`apps/obsidian-plugin/src/journal/sync-wire-contract.test.ts\`
- Test: \`apps/obsidian-plugin/src/journal/queue-driver.test.ts\`

**Interfaces:**

- Known \`source_locator_conflict\` maps to HTTP 409 and plugin \`blocked_conflict\`.
- An unrelated class \`23xxx\` failure gets the existing redacted non-locator application outcome and never becomes retryable \`source_commit_outcome_unknown\`.

- [ ] **Step 1: Write RED classification tests.**

\`\`\`python
def test_unrelated_integrity_error_is_not_a_locator_conflict() -> None:
    mapped = map_database_failure(_integrity_error(sqlstate="23505", constraint="other_unique"), source_id=uuid4())
    assert mapped.code is not ErrorCode.SOURCE_LOCATOR_CONFLICT
    assert mapped.is_retryable is False

def test_mapping_never_exposes_database_details() -> None:
    mapped = map_database_failure(_integrity_error(sqlstate="23505", constraint="private_name"), source_id=uuid4())
    assert "23505" not in str(mapped)
    assert "private_name" not in str(mapped)
\`\`\`

- [ ] **Step 2: Run RED.**

Run: \`uv run pytest tests/unit/postgresql_source_store/test_error_mapping.py -q\`

Expected: FAIL at the present generic integrity path.

- [ ] **Step 3: Implement a narrow, message-free classifier.**

Inspect only exception class, SQLSTATE and known adapter context; never exception message. Preserve the existing locator pre-check as the only producer of \`source_locator_conflict\`. Use an existing safe non-retryable code for unrelated integrity failures; do not invent a public error code.

- [ ] **Step 4: Add the exact corpus entry and replay assertions.**

\`\`\`json
{
  "name": "content_source_locator_conflict",
  "response": {"status": 409, "error_code": "source_locator_conflict"},
  "plugin_landing": {"event_state": "blocked_conflict", "retry": false}
}
\`\`\`

Update the expected fixture hash only via the repository corpus-check flow. Assert in the TypeScript replay and queue-driver test that the terminal trail contains the closed token and only a UUID-gated request ID, if supplied.

- [ ] **Step 5: Run GREEN contract gates.**

Run: \`uv run pytest tests/unit/postgresql_source_store/test_error_mapping.py tests/contract/small_file_sync/test_wire_contract.py -q\`

Run: \`pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sync-wire-contract.test.ts src/journal/queue-driver.test.ts\`

Expected: PASS; both runtimes consume identical corpus bytes and expose no database detail.

- [ ] **Step 6: Commit.**

\`\`\`powershell
git add packages/postgresql-source-store/src/postgresql_source_store/error_mapping.py packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py tests/unit/postgresql_source_store/test_error_mapping.py tests/fixtures/small_file_sync/wire-golden.json tests/contract/small_file_sync/test_wire_contract.py apps/obsidian-plugin/src/journal/sync-wire-contract.test.ts apps/obsidian-plugin/src/journal/queue-driver.test.ts
git commit -m "test: pin typed locator conflict wire landing"
\`\`\`

### Task 3: Deliver the mandatory diagnostics-surface hardening

**Files:**

- Modify: \`apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts\`
- Modify: \`apps/obsidian-plugin/src/journal/sync-diagnostics-export.ts\`
- Modify: \`apps/obsidian-plugin/src/journal/sync-self-check.ts\`
- Modify: \`apps/obsidian-plugin/src/journal/queue-driver.ts\`
- Test: \`apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts\`
- Test: \`apps/obsidian-plugin/src/journal/sync-diagnostics-export.test.ts\`
- Test: \`apps/obsidian-plugin/src/journal/sync-self-check.test.ts\`
- Test: \`apps/obsidian-plugin/src/journal/queue-driver.test.ts\`
- Modify when user-visible behavior changes: \`docs/operations/sync-error-tracing.md\`

**Interfaces:**

- Produce \`envelopeRequestId(requestId: string): SyncDiagnosticRequestIdToken | null\`; only canonical UUIDs produce tokens.
- Set \`SyncDiagnosticsTrailSectionInput.stopReasonTokens\` to the existing readonly closed-token union.
- All new/changed terminal wire outcomes remain readably represented in the durable trail; no free-form server value enters settings.

- [ ] **Step 1: Write RED trail/export tests.**

\`\`\`typescript
it("rejects a non-UUID request id before durable trail persistence", () => {
  expect(envelopeRequestId("untrusted-value")).toBeNull();
});

it("renders only the closed stop-reason union", () => {
  const input: SyncDiagnosticsTrailSectionInput = {
    stopReasonTokens: ["blocked_conflict"], totalEntryCount: 1, appendFailureCount: 0, entries: [],
  };
  expect(renderSyncDiagnosticsTrailSection(input)).toContain("blocked_conflict");
});
\`\`\`

- [ ] **Step 2: Run RED.**

Run: \`pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sync-diagnostics-trail.test.ts src/journal/sync-diagnostics-export.test.ts src/journal/sync-self-check.test.ts src/journal/queue-driver.test.ts\`

Expected: FAIL because constructor validation is absent and settings input is \`string[]\`.

- [ ] **Step 3: Implement the smallest closed-vocabulary cleanup.**

Use one unambiguous request-ID token name; update callers to omit \`null\`. Replace duck-typed \`syncFailureKind\` access with one safe narrowing predicate. Correct the queue-driver comment to say which failures actually reach its hook. Preserve per-vocabulary narrowing, document the 999 append-failure saturation, and keep export/settings types closed.

- [ ] **Step 4: Prove diagnostics failure is observable and non-blocking.**

\`\`\`typescript
it("keeps queue behavior and exposes a bounded trail token when diagnostics persistence fails", async () => {
  failingStore.writeThrows = true;
  await driver.requestPass();
  expect(trail.readAppendFailureCount()).toBe(1);
  expect(trail.readEntries()).toContainEqual(
    expect.objectContaining({ tokens: expect.arrayContaining(["trail_persist_failed"]) }),
  );
});
\`\`\`

Attach a rejection handler to the copy command that reports through the existing bounded trail mechanism. It must not throw into UI processing or log clipboard data.

- [ ] **Step 5: Run all diagnostics/static GREEN gates.**

Run: \`pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sync-diagnostics-trail.test.ts src/journal/sync-diagnostics-export.test.ts src/journal/sync-self-check.test.ts src/journal/queue-driver.test.ts\`

Run: \`pnpm --dir apps/obsidian-plugin exec tsc --noEmit\`

Run: \`pnpm --dir apps/obsidian-plugin run lint\`

Expected: PASS; each changed closed path has trail/settings readback evidence and no \`string[]\` escape hatch remains.

- [ ] **Step 6: Commit.**

\`\`\`powershell
git add apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts apps/obsidian-plugin/src/journal/sync-diagnostics-export.ts apps/obsidian-plugin/src/journal/sync-self-check.ts apps/obsidian-plugin/src/journal/queue-driver.ts apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts apps/obsidian-plugin/src/journal/sync-diagnostics-export.test.ts apps/obsidian-plugin/src/journal/sync-self-check.test.ts apps/obsidian-plugin/src/journal/queue-driver.test.ts docs/operations/sync-error-tracing.md
git commit -m "fix: surface closed sync diagnostics tokens"
\`\`\`

### Task 4: Resolve the PEP 758 formatter gate

**Files:**

- Modify: \`docs/handoff/BACKLOG.md\`
- Modify only if needed: \`docs/operations/sync-error-tracing.md\`

**Interfaces:**

- Produce one ruling: \`uv run poe python-format-check\` is authoritative, and pinned Python 3.14/Ruff output \`except A, B:\` is accepted.

- [ ] **Step 1: Capture the RED formatter proof.**

Run: \`uv run poe python-format-check\`

Expected: either PASS on present Ruff style or the documented parenthesis normalization. Do not use Python 3.12 tooling as evidence.

- [ ] **Step 2: Apply only Ruff's required formatting, then prove GREEN.**

Run: \`uv run poe python-format\`

Run: \`uv run poe python-format-check\`

Expected: PASS without configuration change and without semantic changes.

- [ ] **Step 3: Retire the tooling-style row and commit.**

Before staging, run \`git diff --stat\`; if format affected unrelated user work, unstage it and stop for direction.

\`\`\`powershell
git add docs/handoff/BACKLOG.md docs/operations/sync-error-tracing.md src apps tests tools
git commit -m "docs: accept pinned ruff exception style"
\`\`\`

### Task 5: Obtain physical Mobile lifecycle evidence

**Files:**

- Modify: \`docs/operations/source-locator-tombstone-lifecycle.md\`
- Modify: \`docs/handoff/BACKLOG.md\`
- Test: \`tests/contract/source_lifecycle/test_reference_device_records.py\`

**Interfaces:**

- Produce a sanitized Mobile record with PASS/closed-token outcome and evidence reference for tracked rename, tracked move, delete, automatic restore, explicit restore, offline/reconnect, unload/reload, and policy denial.

- [ ] **Step 1: Prepare the approved disposable live environment.**

Read \`.local/RESTART.md\` and \`tools/obsidian_live_acceptance_bootstrap.py\`. Run \`uv run poe stack-status\`; if the runbook requires it, stop \`knowledge-local\` before creating an exact disposable \`knowledge-ci-*\` project. Use only \`.local/serve-local.sh\`, \`.local/run-worker.sh\`, \`.local/e2e-totp-code.py\`, and \`.local/publish-policy-revision.py\` as documented.

- [ ] **Step 2: Bootstrap and preflight.**

Run: \`uv run python tools/obsidian_live_acceptance_bootstrap.py --project knowledge-ci-<generated-safe-suffix>\`

Expected: active device/TOTP prerequisites or the helper's approved enrollment branch. A missing active credential is a bootstrap branch, not a deferral.

- [ ] **Step 3: Execute the physical Mobile matrix.**

Run each of the eight scenarios on the physical device. For a failure, read back only its closed token from plugin trail/status or lifecycle diagnostics. Never record a path, content, token, URL, digest or raw response.

- [ ] **Step 4: Write sanitized record and prove it.**

Run: \`uv run pytest tests/contract/source_lifecycle/test_reference_device_records.py -m device_records -q\`

Expected: PASS only after all record fields/evidence references are valid. If an external prerequisite is unavailable, preserve the DEFERRED row and report that exact prerequisite; never claim Mobile PASS.

- [ ] **Step 5: Remove the Mobile row only after passing, then commit.**

\`\`\`powershell
git add docs/operations/source-locator-tombstone-lifecycle.md docs/handoff/BACKLOG.md tests/contract/source_lifecycle/test_reference_device_records.py
git commit -m "docs: record mobile lifecycle acceptance"
\`\`\`

### Task 6: Retire proven rows, run final gates and hand off

**Files:**

- Modify: \`docs/handoff/BACKLOG.md\`
- Modify if required: \`docs/operations/sync-error-tracing.md\`
- Create: \`docs/handoff/2026-08-24-child-six-deferred-remediation.md\`

**Interfaces:**

- Produce exactly one handoff: final SHA, RED/GREEN evidence, diagnostics evidence, physical-device result, decisions, and any retained deferred row.

- [ ] **Step 1: Retire only evidence-backed rows.**

Remove rows 53–56, 58, 60–62 only when the associated code tests and Mobile evidence passed. Keep incomplete rows exactly once with their current implement-by trigger.

- [ ] **Step 2: Run final gates.**

\`\`\`powershell
uv run pytest tests/unit/postgresql_source_store/test_small_file_sync_operations.py tests/unit/postgresql_source_store/test_error_mapping.py tests/unit/postgresql_source_store/test_publication_store.py tests/contract/small_file_sync/test_wire_contract.py tests/contract/source_lifecycle/test_reference_device_records.py -q
pnpm --dir apps/obsidian-plugin exec vitest run
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run build
pnpm --dir apps/obsidian-plugin run lint
uv run poe python-format-check
uv run ruff check src apps tests packages
uv run mypy src apps/api/src packages/postgresql-source-store/src
uv run poe verify
git diff --check
git status --short
\`\`\`

Expected: each command exits 0. A Mobile acceptance claim additionally requires Task 5 physical evidence.

- [ ] **Step 3: Write handoff and commit closure docs.**

Record final SHA, exact commands/results, the trail/settings/log token evidence for every changed closed path, PEP 758 ruling, Mobile status, and removed/retained rows. Keep it below 400 lines and link living runbooks.

\`\`\`powershell
git add docs/handoff/BACKLOG.md docs/operations/sync-error-tracing.md docs/handoff/2026-08-24-child-six-deferred-remediation.md
git commit -m "docs: hand off child six deferred remediation"
\`\`\`

## Plan self-review

- Spec coverage: D1 Task 1; D2–D3 Task 2; D4 and the mandatory diagnostics surface Task 3; D5 Task 4; D6 Task 5; closure Task 6.
- Every changed closed failure has a mandatory readable closed-token surface in Tasks 1–3 and final evidence in Task 6.
- Privacy is enforced through closed-token-only tests and sanitized physical-device records.
- No placeholder, implicit external permission, or Phase-10 diagnostics deferral remains.

