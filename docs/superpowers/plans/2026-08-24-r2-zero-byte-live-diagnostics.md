# R2 Zero-Byte Live Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit and retain one privacy-safe, fixed-schema diagnostic when the live zero-byte R2 round trip fails.

**Architecture:** Keep the diagnostic harness-local: a small pure classifier produces a closed reason token from typed application errors or provider exception types, and the zero-byte test writes it to pytest's captured output before preserving the original exception. The JUnit sanitizer removes every ordinary captured stream and retains only the exact diagnostic record for the zero-byte test.

**Tech Stack:** Python 3.14, pytest/pytest-asyncio, botocore exception types, XML `ElementTree`.

**Spec:** `docs/superpowers/specs/2026-08-24-r2-zero-byte-live-diagnostics-design.md`

## Global Constraints

- Emit exactly one record with `event` `r2_live_zero_byte_failed`, stage `store`, `resolve` or `read`, and one closed reason token before re-raising the original body failure.
- An `ApplicationError` uses its existing `error_code`; provider failures may only map to `provider_client_error`, `provider_timeout`, `provider_transport_error`, or `provider_unclassified_error`.
- Do not inspect or serialize exception text, chained causes, endpoints, buckets, keys, digests, payloads, request headers, or credentials.
- If emitting the diagnostic fails, preserve the original exception and emit only reason `diagnostic_emission_failed`.
- The sanitized JUnit artifact retains only test identity, pass/fail state, the one permitted fixed-schema diagnostic, and `r2_live_failure_details_redacted`; it removes all properties, traceback text, arbitrary streams, and error/failure content.
- Do not change R2 adapter semantics, exact-key cleanup, the live case selection, or introduce retries/skips.

---

### Task 1: Zero-byte failure classification and emission

**Files:**
- Modify: `tests/integration/r2_object_storage/conftest.py`
- Modify: `tests/integration/r2_object_storage/test_live_r2_adapter.py`
- Create: `tests/integration/r2_object_storage/test_zero_byte_live_diagnostics.py`

**Interfaces:**
- Produces `ZeroByteLiveDiagnostic` (fixed event/stage/reason serialization) and a pure exception-to-reason classifier for Task 2's sanitizer allowlist.
- Consumes `ApplicationError.error_code.value` and only botocore exception types/safe response metadata.

- [ ] **Step 1: Write failing diagnostics tests**

Add tests proving typed application errors preserve only their error-code token, each provider category returns its allowed closed token, exception messages never occur in the serialized JSON, injected `store`/`resolve`/`read` failures report their exact stage, and an output-emission failure re-raises the original exception with reason `diagnostic_emission_failed`.

- [ ] **Step 2: Run the new tests to verify RED**

Run: `uv run pytest tests/integration/r2_object_storage/test_zero_byte_live_diagnostics.py -q`

Expected: FAIL because the diagnostic model/classifier and zero-byte failure wrapper do not exist.

- [ ] **Step 3: Implement the minimal closed diagnostic boundary**

Add a harness-local fixed-schema record and pure classifier in `conftest.py`. Wrap `test_zero_byte_round_trip` so only the three body operations are stage-labelled, emit one JSON record through pytest capture, and bare re-raise the primary error. The diagnostic-emission fallback must not add exception details.

- [ ] **Step 4: Run focused diagnostics tests and the unchanged offline storage contracts**

Run: `uv run pytest tests/integration/r2_object_storage/test_zero_byte_live_diagnostics.py tests/contract/object_storage/test_r2_adapter_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
git add tests/integration/r2_object_storage/conftest.py tests/integration/r2_object_storage/test_live_r2_adapter.py tests/integration/r2_object_storage/test_zero_byte_live_diagnostics.py
git commit -m "test: add closed zero-byte live diagnostics"
```

### Task 2: Permit only the zero-byte diagnostic in sanitized JUnit

**Files:**
- Modify: `tests/integration/r2_object_storage/conftest.py`
- Modify: `tests/integration/r2_object_storage/test_live_junit_sanitization.py`
- Modify: `tests/contract/test_ci_security.py`

**Interfaces:**
- Consumes Task 1's exact JSON schema/event identity.
- Produces a sanitized report that contains a permitted diagnostic only in the failing zero-byte testcase's `system-out`.

- [ ] **Step 1: Write failing sanitizer and workflow-contract tests**

Extend sanitizer fixtures with a valid zero-byte record plus sensitive ordinary `system-out`, `system-err`, properties, traceback and a malformed/impostor record. Assert the valid record is the sole retained stream, all unsafe material is removed, and the existing redacted marker remains. Extend the CI security contract only if the workflow needs an assertion for the unchanged sanitizer invocation.

- [ ] **Step 2: Run the sanitizer tests to verify RED**

Run: `uv run pytest tests/integration/r2_object_storage/test_live_junit_sanitization.py tests/contract/test_ci_security.py -q`

Expected: FAIL because the current sanitizer removes every `system-out` element.

- [ ] **Step 3: Implement allowlisted preservation**

Parse `system-out` only for the exact fixed-schema diagnostic from Task 1 and only for `test_zero_byte_round_trip`; remove every other stream and sanitize failure/error nodes exactly as before. Reject malformed, duplicated, extra-field, wrong-stage, wrong-reason and non-zero-byte candidates rather than retaining them.

- [ ] **Step 4: Run focused artifact tests and repository verification**

Run: `uv run pytest tests/integration/r2_object_storage/test_live_junit_sanitization.py tests/integration/r2_object_storage/test_zero_byte_live_diagnostics.py tests/contract/test_ci_security.py -q`

Then run: `uv run poe verify` and `git diff --check`.

Expected: all commands PASS; the protected live workflow remains intentionally undispatched because it requires protected GitHub secrets.

- [ ] **Step 5: Commit**

```text
git add tests/integration/r2_object_storage/conftest.py tests/integration/r2_object_storage/test_live_junit_sanitization.py tests/contract/test_ci_security.py
git commit -m "test: retain safe R2 zero-byte diagnostics"
```
