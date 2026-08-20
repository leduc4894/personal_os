# Object Storage Backlog Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire load-bearing object-storage backlog rows with tested contracts and terminal rulings.

**Architecture:** Keep core key validation transport-neutral, admission in `SpoolManager`, and R2 retries/metrics in the adapter. Prepare the existing live harness without simulating external acceptance.

**Tech Stack:** Python 3.13, asyncio, pytest, Ruff, mypy strict, Poe.

**Spec:** `docs/superpowers/specs/2026-08-20-object-storage-backlog-retirement-design.md`

## Global Constraints

- Preserve all public storage ports, R2's immutable-byte role, and typed error codes.
- Record RED before minimal GREEN implementation for every behavior.
- Never log keys, content, credentials, endpoint values, or provider tracebacks.
- Do not remove the pre-existing infra circular-import row.
- Preserve `verify.log` and `verify2.log`.

---

### Task 1: Core key parser and MD5 ruling

**Files:** `src/personal_os/object_storage/keys.py`; `tests/unit/object_storage/test_keys.py`; `tests/contract/object_storage/test_r2_adapter_contract.py`.

**Produces:** `CanonicalObjectKey.parse(value: str) -> CanonicalObjectKey`.

- [ ] Write a failing test that rejects `objects/sha256/00/00/not-a-digest`, wrong digest shards, uppercase digest and surplus paths; also round-trip a derived key.
- [ ] Run `uv run pytest tests/unit/object_storage/test_keys.py -q`; observe failure because `parse` is absent.
- [ ] Implement `parse` by `ContentDigest.parse(value.rsplit("/", 1)[-1])`, deriving the canonical key, comparing it byte-for-byte to `value`, and raising `ValueError` on mismatch.
- [ ] Run `uv run pytest tests/unit/object_storage/test_keys.py tests/contract/object_storage/test_r2_adapter_contract.py -q`; confirm the current `content_md5_base64` and `content_md5` contract remains green.
- [ ] Commit: `feat: validate canonical object keys`.

### Task 2: Non-blocking spool admission and receive backstop

**Files:** `packages/r2-object-storage/src/r2_object_storage/spool.py`; `tests/contract/object_storage/test_r2_adapter_resource_contract.py`.

**Consumes:** The existing typed `SpoolManager._acquire_admission()` errors.

- [ ] Write a failing test whose injected `disk_usage` blocks in a worker gate while an independent asyncio task completes; add a stalled stream test by patching the receive timeout only in the test and asserting typed error, no spool, zero permit/reservation.
- [ ] Run the resource contract test; observe RED while synchronous `disk_usage` blocks the loop.
- [ ] Make free-space validation async with `await asyncio.to_thread(self._disk_usage, self._root)`, retain the condition lock while probing, and retain existing cleanup/error mapping.
- [ ] Re-run the resource suite and confirm GREEN with exact cleanup assertions.
- [ ] Commit: `fix: keep spool admission off event loop`.

### Task 3: Single-flight isolation and exact metrics

**Files:** `packages/r2-object-storage/src/r2_object_storage/adapter.py`; `packages/r2-object-storage/src/r2_object_storage/metrics.py`; `tests/contract/object_storage/test_r2_adapter_resource_contract.py`; `tests/unit/object_storage/test_r2_error_mapping.py`.

**Produces:** Fresh equivalent typed errors for waiters; waiter `attempt_count == 0`.

- [ ] Write RED tests proving owner and waiter failures have equal safe code but distinct identity, waiter attempt count is zero, internal errors count as failures, and reservation gauges update at every mutation.
- [ ] Run the targeted resource/error-mapping tests and observe current shared exception/synthetic attempt behavior.
- [ ] Clone only typed application errors from safe code/details; retain shielded waiter/owner cancellation behavior and the defensive retrieved-future guard. Record failures for application errors and emit the reservation gauge after each acquire/release.
- [ ] Re-run targeted suites and confirm GREEN without changing owner retry counts.
- [ ] Commit: `fix: isolate object storage single-flight failures`.

### Task 4: Runtime evidence and live-harness safety

**Files:** `packages/r2-object-storage/src/r2_object_storage/runtime_check.py`; `tests/unit/object_storage/test_runtime_check.py`; `docs/operations/object-storage.md`; `.github/workflows/object-storage-live.yml`; `tests/integration/r2_object_storage/conftest.py`; `tests/integration/r2_object_storage/test_live_r2_adapter.py`.

**Produces:** Probe-only `duration_ms` and cancellation-safe local live cleanup.

- [ ] Write RED tests for client-composition exclusion from duration and for `cancel-in-progress: false` in this workflow.
- [ ] Run `uv run pytest tests/unit/object_storage/test_runtime_check.py tests/contract/test_ci_security.py -q` and observe RED.
- [ ] Start timing immediately before the probe; correct only inaccurate operator wording; set the workflow cancellation policy false; guard temp-root creation/loading with `try/finally`; add the lost-response-equivalent scenario with no configuration output.
- [ ] Run runtime, integration-harness offline and CI-security tests; confirm GREEN.
- [ ] Commit: `fix: harden object storage runtime gate`.

### Task 5: Backlog terminal record

**Files:** `docs/handoff/BACKLOG.md`; create `docs/handoff/2026-08-20-object-storage-backlog-retirement.md`.

**Consumes:** Evidence/rulings from Tasks 1–4.

- [ ] Write one handoff with final SHA, each RED/GREEN command, terminal disposition for every removed object-storage row, the helper/future rulings, and sanitized hosted-live prerequisite.
- [ ] Remove only rows that are implemented or expressly ruled. Keep the pre-existing infra row and any absent hosted-run evidence indexed.
- [ ] Run `git diff --check` and `uv run poe verify`; require clean diff and exit 0.
- [ ] Commit: `docs: retire object storage backlog`.

## Self-review

- Tasks 1–4 map one-to-one to every binding behavior; Task 5 removes only terminal work.
- The pre-existing circular import is deliberately excluded.
- No schema, public API, provider, or dependency change is introduced.
