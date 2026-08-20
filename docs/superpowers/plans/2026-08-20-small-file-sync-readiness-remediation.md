# Small File Sync Readiness Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redact small-file sensitive value-object representations and close the Child 5 gate after the four domain plans are green.

**Architecture:** Add explicit local repr controls without changing constructors, equality, hashes, serialization, or wire behavior. Documentation closure removes only the eleven completed gate rows.

**Tech Stack:** Python dataclasses, pytest, repository documentation.

**Spec:** `docs/superpowers/specs/2026-08-20-child-five-readiness-remediation-design.md`

## Global Constraints

- Do not introduce a shared redaction abstraction.
- Leave every later-gated backlog row unchanged.

---

### Task 1: Redact sensitive value-object representations

**Files:**
- Modify: `src/personal_os/small_file_sync/contracts.py`
- Test: `tests/unit/small_file_sync/test_contracts.py`

- [ ] **Step 1: Write failing tests**

```python
for value_object, raw_value in cases:
    instance = value_object(raw_value)
    assert raw_value not in repr(instance)
    assert instance.value == raw_value
    assert instance == value_object(raw_value)
```

- [ ] **Step 2: Run the failing test**

Run: `uv run pytest tests/unit/small_file_sync/test_contracts.py -q`

Expected: FAIL because dataclass repr includes `value=` and the raw sensitive value.

- [ ] **Step 3: Implement explicit redaction**

```python
def __repr__(self) -> str:
    return f"{type(self).__name__}(value=<redacted>)"
```

Add this method independently to `SmallFileIdempotencyKey`, `NormalizedLocator`, and `UploadOperationToken`.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/small_file_sync/test_contracts.py tests/unit/small_file_sync/test_service.py -q; uv run mypy src/personal_os/small_file_sync`

Expected: PASS.

```powershell
git add src/personal_os/small_file_sync/contracts.py tests/unit/small_file_sync/test_contracts.py
git commit -m "fix: redact small file value representations"
```

### Task 2: Close the completed readiness backlog gates

**Files:**
- Modify: `docs/handoff/BACKLOG.md`
- Create: `docs/handoff/2026-08-20-child-five-readiness-remediation.md`

- [ ] **Step 1: Confirm exact documentation scope**

Run: `rg -n "Before Child 5" docs/handoff/BACKLOG.md`

Expected: exactly eleven rows matching the approved remediation spec.

- [ ] **Step 2: Run final gates**

Run: `uv run poe python-lint; uv run poe python-type-check; uv run pytest tests/unit/sources tests/unit/identity tests/unit/recovery tests/unit/tools/test_postgresql_dump_process.py tests/unit/api_runtime tests/unit/small_file_sync tests/contract/source_publication tests/contract/api -q`

Expected: all commands exit 0; additionally run projection integration if source dispatcher transactions changed.

- [ ] **Step 3: Write the exact handoff and remove only completed rows**

```markdown
## Gate status

| Gate | Evidence |
| --- | --- |
| Focused remediation suite | exact command and exit 0 |
```

Include final commit SHA, gate evidence, interpretations, remaining linked deferred work, and next action: begin Child 5.

- [ ] **Step 4: Verify documentation and commit**

Run: `git diff --check; rg -n "Before Child 5" docs/handoff/BACKLOG.md`

Expected: no `Before Child 5` line remains and later gates are untouched.

```powershell
git add docs/handoff/BACKLOG.md docs/handoff/2026-08-20-child-five-readiness-remediation.md
git commit -m "docs: close child five readiness backlog"
```
