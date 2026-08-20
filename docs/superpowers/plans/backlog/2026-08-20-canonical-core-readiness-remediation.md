# Canonical Core Readiness Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close identity, canonical-read, recovery contract, and dump-process blockers for Child 5.

**Architecture:** Apply typed guards at value boundaries, record read failures only before consumer entry, redact dataclass fields, and concurrently drain bounded child pipes. No error token, migration, or CLI change is allowed.

**Tech Stack:** Python 3.13, asyncio, pytest, mypy strict.

**Spec:** `docs/superpowers/specs/2026-08-20-child-five-readiness-remediation-design.md`

## Global Constraints

- Never render snapshot tokens, stdout, passwords, or passfile fields.
- Recovery remains fail-closed and uses its existing error codes.

---

### Task 1: Harden identity and canonical reads

**Files:**
- Modify: `src/personal_os/identity/contracts.py`, `src/personal_os/sources/reading.py`
- Test: `tests/unit/identity/test_contracts.py`, `tests/unit/sources/test_canonical_read.py`

**Interfaces:** Produces typed non-string validation and reader-only FAILED metrics/events from `CanonicalSourceReadService.open_current_source()`.

- [ ] **Step 1: Write failing tests**

```python
with pytest.raises(IdentityBootstrapError):
    WorkspaceKey(cast(str, 42))
with pytest.raises(InternalApplicationError):
    async with service.open_current_source(command, context):
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
assert metrics.records == []
```

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/unit/identity/test_contracts.py tests/unit/sources/test_canonical_read.py -q`

Expected: FAIL on `AttributeError` or consumer-body FAILED telemetry.

- [ ] **Step 3: Implement narrow checks**

```python
if not isinstance(value, str):
    raise IdentityBootstrapError(ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID, safe_details={"reason": reason})
```

Move the context-manager `yield` outside the reader failure handler and add missing/corrupt FAILED event assertions.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/identity/test_contracts.py tests/unit/sources/test_canonical_read.py -q; uv run mypy src/personal_os/identity src/personal_os/sources`

Expected: PASS.

```powershell
git add src/personal_os/identity src/personal_os/sources tests
git commit -m "fix: harden canonical input and read telemetry"
```

### Task 2: Redact recovery values and normalize JSON rejection

**Files:**
- Modify: `src/personal_os/recovery/contracts.py`, `src/personal_os/recovery/manifest.py`
- Test: `tests/unit/recovery/test_contracts.py`, `tests/unit/recovery/test_manifest.py`

- [ ] **Step 1: Write failing tests**

```python
assert "snapshot-token" not in repr(snapshot)
with pytest.raises(RecoveryError) as raised:
    parse_manifest(b"[]\n")
assert raised.value.safe_details["reason"] is RecoveryBundleInvalidReason.JSON_NONCANONICAL
```

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/unit/recovery/test_contracts.py tests/unit/recovery/test_manifest.py -q`

Expected: FAIL on raw repr or `CONTRACT_UNSUPPORTED`.

- [ ] **Step 3: Implement minimal behavior**

```python
snapshot_token: str = field(repr=False)
if not isinstance(payload, dict):
    _reject(RecoveryBundleInvalidReason.JSON_NONCANONICAL)
```

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/recovery/test_contracts.py tests/unit/recovery/test_manifest.py -q; uv run mypy src/personal_os/recovery`

Expected: PASS.

```powershell
git add src/personal_os/recovery tests
git commit -m "fix: harden recovery value boundaries"
```

### Task 3: Harden dump/restore subprocess edges

**Files:**
- Modify: `tools/postgresql_dump_process.py`
- Test: `tests/unit/tools/test_postgresql_dump_process.py`

- [ ] **Step 1: Write failing tests**

```python
assert "captured-output" not in repr(ProcessRunResult(returncode=0, stdout="captured-output"))
assert passfile.read_text(encoding="utf-8") == "host\\:name:*:*:user:pass\\\\word\n"
assert restore_timeout_error.code is ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
```

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/unit/tools/test_postgresql_dump_process.py -q`

Expected: FAIL on repr, sequential drain, passfile escaping, or timeout mapping.

- [ ] **Step 3: Implement bounded drains and escaping**

```python
stdout_task = asyncio.create_task(_read_capped_stdout(process.stdout))
stderr_task = asyncio.create_task(_discard_stream(process.stderr))
await asyncio.gather(stdout_task, stderr_task)
```

Set `stdout: str = field(default="", repr=False)` and escape backslash, colon, newline, and carriage return in every passfile field.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/tools/test_postgresql_dump_process.py -q; uv run mypy tools/postgresql_dump_process.py`

Expected: PASS.

```powershell
git add tools/postgresql_dump_process.py tests/unit/tools/test_postgresql_dump_process.py
git commit -m "fix: harden recovery subprocess boundary"
```
