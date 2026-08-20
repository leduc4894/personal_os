# Object Storage Backlog Retirement Handoff

**Date:** 2026-08-20
**Plan:** `docs/superpowers/plans/2026-08-20-object-storage-backlog-retirement.md`
**Binding spec:** `docs/superpowers/specs/2026-08-20-object-storage-backlog-retirement-design.md`

## Final implementation SHAs

| Task | Final SHA | Subject |
| --- | --- | --- |
| 1 | `b3e5ae5a39174430191c06323bd6cc6c03f435dc` | `feat: validate canonical object keys` |
| 2 | `3cc9b766f88b95adba891876d25fd0ed195a133c` | `test: harden spool admission coordination` |
| 3 | `0c8c0ceead6aac1f9e1ba9d55d561325ee070be6` | `fix: detach single-flight waiter error chains` |
| 4 | `1ec3a01d3126e25645e7e0bf4fab8097a69553a4` | `fix: sanitize object storage live junit` |
| Final P1 correction | `da52777cb8733cdeac043c249bf10399cbf7a75e` | `fix: close object storage concurrency races` |

The Task 2 and Task 3 SHAs are review-round commits on top of their initial
implementation commits (`00d5cc8` and `8facfe4` respectively). Task 4's
runtime/harness implementation began at `dfff3b7` and is finalized by
`1ec3a01`. The final correction commit closes the two concurrency races found
in whole-wave review without changing a public storage port or typed error.

## Retired backlog rows

| Prior row | Terminal disposition and evidence |
| --- | --- |
| Dual MD5 name | **Ruled implemented.** `PutObjectRequest` retains compatible `content_md5_base64` and `content_md5`; Task 1's adapter-contract GREEN suite passed 50 tests. |
| Internal application failure metrics | **Implemented.** `InternalApplicationError` is counted as a failed operation; Task 3 RED showed zero records and GREEN proved the recorded failure. |
| Duplicate shielded-cleanup helpers | **Expressly ruled.** The two helpers have distinct ownership and only two callers; retain them until a real third caller establishes a shared abstraction. |
| Event-loop `disk_usage` | **Implemented.** Admission probes via `asyncio.to_thread` while retaining the admission lock; Task 2 proved independent async work can proceed. |
| Real-time receive backstop | **Implemented.** The stalled-stream test exercises the receive-timeout backstop and verifies the existing typed error plus zero spool, permit, and reservation state. |
| Reserved-size gauge fidelity | **Implemented and concurrency-hardened.** Every adapter-owned acquire/release emits its exact post-mutation value while the `SpoolManager` condition lock is held. The final RED deterministically reduced a true 69-byte peak to 46 through an interleaved release; GREEN retained the 69-byte peak. |
| Core object-key parser | **Implemented.** `CanonicalObjectKey.parse` enforces the exact lowercase, sharded SHA-256 grammar and byte-for-byte canonical round trip. |
| Synthetic waiter attempts | **Implemented.** Waiters report `attempt_count == 0`; owner retry counts are unchanged. |
| Shared waiter exception instance | **Implemented and generation-fenced.** Waiters receive a fresh equivalent typed error with no owner/provider exception chain, and detach identity-checks the exact entry generation. The final cross-generation regression proves an old failed-flight waiter cannot consume a replacement flight's waiter and turn its typed owner-cancellation outcome into bare `CancelledError`. |
| Runtime probe duration | **Implemented.** `duration_ms` begins immediately before the probe, excluding client composition. |
| Inaccurate probe-event wording | **Implemented.** The operator guide now names the emitted success and failure probe events only. |
| Composite single-flight predecessor row | **Narrowed and replaced, not wholly retired.** The defensive unretrieved-future guard is expressly ruled to remain because owner-failure retrieval is tested before registry removal. The theoretical `_run_shielded` cleanup-raises cancellation edge is unresolved and remains in the replacement [ruling backlog row](BACKLOG.md). |
| Composite live-harness predecessor row | **Narrowed and replaced, not wholly retired.** JUnit failure/error sanitization, loader-rejection spool-root cleanup, and `cancel-in-progress: false` are implemented by Task 4. The external hosted proof of the sanitized path and the decorative `run_nonce` remain in the replacement [external-prerequisite backlog row](BACKLOG.md). |

Eleven whole object-storage rows were retired. Two composite predecessor rows
were narrowed into the replacement rows above; therefore all 13 former
object-storage lines were removed, but only their terminal sub-items left the
backlog. No schema, public API, provider, dependency, or canonical architecture
contract changed.

## RED/GREEN evidence

### Task 1

- RED: `uv run pytest tests/unit/object_storage/test_keys.py -q` — exit 1,
  5 failures because `CanonicalObjectKey.parse` was absent.
- GREEN: `uv run pytest tests/unit/object_storage/test_keys.py tests/contract/object_storage/test_r2_adapter_contract.py -q`
  — exit 0, 50 passed.

### Task 2

- RED: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py -q`
  — exit 1; the synchronous free-space probe blocked independent async work.
- GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py -q`
  — exit 0, 9 passed.
- GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py tests/unit/object_storage/test_spool_manager.py -q`
  — exit 0, 39 passed, 3 skipped.
- Review GREEN reruns: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py -q`
  — exit 0, 9 passed; `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py tests/unit/object_storage/test_spool_manager.py -q`
  — exit 0, 39 passed, 3 skipped.

### Task 3

- RED: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_same_digest_failure_is_fresh_for_waiter_with_zero_attempts tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_internal_application_error_records_failed_operation tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_reservation_gauge_emits_after_failed_verification_mutations -q`
  — exit 1, 3 failures.
- GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_same_digest_failure_is_fresh_for_waiter_with_zero_attempts tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_internal_application_error_records_failed_operation tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_reservation_gauge_emits_after_failed_verification_mutations -q`
  — exit 0, 3 passed.
- GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py tests/unit/object_storage/test_r2_error_mapping.py -q`
  — exit 0, 71 passed.
- GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_contract.py tests/contract/object_storage/test_r2_adapter_resource_contract.py tests/unit/object_storage/test_r2_error_mapping.py -q`
  — exit 0, 116 passed.
- Review RED: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_same_digest_failure_is_fresh_for_waiter_with_zero_attempts -q`
  — exit 1, 1 failure because the clone retained the owner context.
- Review GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_same_digest_failure_is_fresh_for_waiter_with_zero_attempts -q`
  — exit 0, 1 passed; `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py tests/unit/object_storage/test_r2_error_mapping.py -q`
  — exit 0, 71 passed.

### Final P1 correction

- Single-flight RED: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_old_waiter_detach_cannot_cancel_new_generation_waiter -q`
  — exit 1; the replacement generation's real waiter received bare
  `CancelledError` after the delayed old-generation detach decremented its
  entry.
- Single-flight GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_old_waiter_detach_cannot_cancel_new_generation_waiter tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_same_digest_failure_is_fresh_for_waiter_with_zero_attempts tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_waiter_cancellation_keeps_owner_and_other_waiters_running tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_owner_cancellation_cleans_entry_and_fails_waiter_closed -q`
  — exit 0, 4 passed.
- Reservation RED: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_reservation_peak_survives_interleaved_release -q`
  — exit 1; the true 69-byte reservation peak was reported as 46 bytes.
- Reservation GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_reservation_peak_survives_interleaved_release tests/contract/object_storage/test_r2_adapter_resource_contract.py::test_reservation_gauge_emits_after_failed_verification_mutations -q`
  — exit 0, 2 passed.
- Targeted GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py tests/unit/object_storage/test_spool_manager.py tests/unit/object_storage/test_r2_error_mapping.py -q`
  — exit 0, 103 passed, 3 skipped.
- Expanded GREEN: `uv run pytest tests/contract/object_storage/test_r2_adapter_contract.py tests/contract/object_storage/test_r2_adapter_resource_contract.py tests/unit/object_storage/test_r2_error_mapping.py -q`
  — exit 0, 118 passed.

### Task 4

- RED: `uv run pytest tests/unit/object_storage/test_runtime_check.py tests/contract/test_ci_security.py -q`
  — exit 1, 2 failed/42 passed.
- RED: `uv run pytest tests/integration/r2_object_storage/test_live_cleanup_manifest.py -q`
  — exit 1, 1 failed/9 passed.
- GREEN: `uv run pytest tests/unit/object_storage/test_runtime_check.py tests/contract/test_ci_security.py -q`
  — exit 0, 44 passed.
- GREEN: `uv run pytest tests/integration/r2_object_storage/test_live_cleanup_manifest.py -q`
  — exit 0, 10 passed.
- GREEN: `uv run pytest tests/unit/object_storage/test_runtime_check.py tests/contract/object_storage/test_r2_runtime_contract.py tests/integration/r2_object_storage/test_live_cleanup_manifest.py tests/contract/test_ci_security.py -q`
  — exit 0, 71 passed.
- GREEN: `uv run pytest tests/integration/r2_object_storage/test_live_r2_adapter.py -m r2_live --collect-only -q`
  — exit 0, 9 tests collected.
- Review RED: `uv run pytest tests/integration/r2_object_storage/test_live_junit_sanitization.py tests/contract/test_ci_security.py -q`
  — exit 1, 2 failed/43 passed.
- Review GREEN: `uv run pytest tests/integration/r2_object_storage/test_live_junit_sanitization.py tests/contract/test_ci_security.py -q`
  — exit 0, 45 passed.
- Review GREEN: `uv run pytest tests/integration/r2_object_storage/test_live_cleanup_manifest.py tests/integration/r2_object_storage/test_live_junit_sanitization.py tests/contract/object_storage tests/contract/test_ci_security.py tests/unit/object_storage -q`
  — exit 0, 306 passed, 3 skipped.

Focused Ruff, mypy, and `git diff --check` gates in Tasks 1–4 all exited 0 as
recorded in their task reports. Task 4's initial `uv run poe verify` exited 0
at `dfff3b7` (Python 3066 passed/21 skipped/329 deselected; API 1, plugin 375,
web 139). After the JUnit review change, two fresh full verifies stopped at the
same unrelated concurrent Obsidian journey timeout; they did **not** exit 0.
The timed-out case passed alone in 416 ms and the full plugin package later
passed 375/375, but this is contention evidence, not a successful full-verify
claim for that review round.

## Rulings and remaining indexed work

1. Keep the defensive unretrieved-future guard. Tests prove the owner failure
   is retrieved before registry removal; speculative refactoring is not
   warranted.
2. Keep the remaining `_run_shielded` cancellation row indexed. Its theoretical
   cleanup-raises edge was neither implemented nor ruled by this plan.
3. Keep the test-hygiene batch indexed. It is outside the binding behavior.
4. Keep the pre-existing `infra` circular-import row unchanged. It belongs to
   the separate diagnostics/error-contract slice and is not object-storage
   work.
5. Do not restore a waiter-generation or reservation-metric row to
   `BACKLOG.md`: commit `da52777` resolves both whole-wave findings with
   deterministic RED/GREEN evidence. The three remaining object-storage rows
   in the living index are unchanged and cover separate unresolved work.

## Hosted-live prerequisite and next external action

No hosted R2 workflow was dispatched for Task 4, and no hosted result is
claimed here. The historical 2026-08-14 hosted result does not establish that
the new JUnit-sanitization path was exercised. The remaining hosted-live row
therefore stays indexed.

An authorized operator must dispatch the existing protected object-storage
live workflow on the final hardened application commit, then record sanitized
evidence only: workflow run reference, commit SHA, date, case count/outcome,
and confirmation that only the sanitizer's upload artifact was published. Do
not record credentials, endpoint, bucket, raw JUnit output, or configuration.
The same remaining row also retains the nonfunctional `run_nonce` cleanup for
a future separately scoped decision.

## Task 5 gate record

- `git diff --check` and `git diff --cached --check`: exit 0 after the
  handoff and backlog index were staged.
- First `uv run poe verify`: exit 1 only at the known concurrent Obsidian
  journal journey timeout (374/375 plugin tests passed). Python still passed
  3068 tests with 21 skipped and 329 deselected; all preceding format, lint,
  type, import-boundary, architecture, and artifact gates passed.
- Diagnosis: the timed-out case passed alone in 750 ms, and the full plugin
  suite passed 375/375 in 2.52 s. The staged diff contains only this handoff
  and `BACKLOG.md`; no plugin source or test changed.
- Retry `uv run poe verify`: exit 0. Python: 3068 passed, 21 skipped, 329
  deselected; API client: 1 passed; Obsidian plugin: 375 passed; web: 139
  passed. Formatting, lint, strict typing, import boundaries, API artifact
  checks, package builds, and web/plugin builds also completed.
- Final correction `uv run poe verify`: exit 0. Python: 3070 passed, 21
  skipped, 329 deselected; API client: 1 passed; Obsidian plugin: 375 passed;
  web: 139 passed. Formatting, lint, strict typing, import boundaries, API
  artifact checks, all package builds, and web/plugin production builds also
  completed on commit `da52777` plus this handoff update.

The documentation commit SHA is recorded in
`.superpowers/sdd/2026-08-20-object-storage-backlog-retirement/task-5-report.md`.
