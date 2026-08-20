# API Authentication Readiness Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close API-auth readiness blockers without changing public HTTP contracts.

**Architecture:** Add structured lifespan reporting, database-clock validation, separate offline throttle maps, and a route-level malformed JSON header pin.

**Tech Stack:** FastAPI, SQLAlchemy async, pytest, mypy strict.

**Spec:** `docs/superpowers/specs/2026-08-20-child-five-readiness-remediation-design.md`

## Global Constraints

- No OpenAPI, error code, envelope, or status changes.
- Diagnostics must contain no raw traceback, setting, or secret.

---

### Task 1: Correct authentication composition behavior

**Files:**
- Modify: `apps/api/src/api_runtime/server.py`, `apps/api/src/api_runtime/authentication_composition.py`
- Test: `tests/unit/api_runtime/test_server.py`, `tests/unit/api_runtime/test_authentication_composition.py`

- [ ] **Step 1: Write failing tests**

```python
assert result == 70
assert emergency_records[-1]["event"] == "internal_error"
assert offline_state.login_buckets is not offline_state.source_buckets
```

Inject a database clock different from the host clock and assert keyring coverage uses it.

- [ ] **Step 2: Run focused tests**

Run: `uv run pytest tests/unit/api_runtime/test_server.py tests/unit/api_runtime/test_authentication_composition.py -q`

Expected: FAIL on lifecycle reporting, host time, or shared offline bucket state.

- [ ] **Step 3: Implement composition-local corrections**

```python
try:
    await verify_keyring_covers_required_key_ids(engine=engine, keyring=keyring)
except ApplicationError as error:
    logger.emit(EventName.APPLICATION_CONFIGURATION_REJECTED, error.safe_details)
    raise
```

Pass `DatabaseAuthenticationClock` into validation and allocate distinct offline maps.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/api_runtime/test_server.py tests/unit/api_runtime/test_authentication_composition.py -q; uv run mypy apps/api/src/api_runtime`

Expected: PASS.

```powershell
git add apps/api/src/api_runtime tests/unit/api_runtime
git commit -m "fix: harden api authentication composition"
```

### Task 2: Pin malformed JSON cache suppression

**Files:**
- Modify: existing authentication error boundary only if a test proves a regression
- Test: `tests/contract/api/test_authentication_headers.py`

- [ ] **Step 1: Add the test**

```python
response = client.post("/api/auth/login", content=b"{", headers={"content-type": "application/json"})
assert response.status_code == 400
assert response.headers["cache-control"] == "no-store"
```

- [ ] **Step 2: Run it before implementation**

Run: `uv run pytest tests/contract/api/test_authentication_headers.py -q`

Expected: PASS if existing middleware protects this path; otherwise FAIL.

- [ ] **Step 3: Correct only a proven regression**

```python
response.headers.setdefault("Cache-Control", "no-store")
```

Place this in the current authentication response boundary, never a global response handler.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/contract/api/test_authentication_headers.py tests/unit/api_runtime/test_server.py -q`

Expected: PASS.

```powershell
git add apps/api tests/contract/api/test_authentication_headers.py
git commit -m "test: pin malformed authentication headers"
```
