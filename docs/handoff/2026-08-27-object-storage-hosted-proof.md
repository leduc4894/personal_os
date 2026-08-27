# Object Storage Hosted Proof and Run-Nonce Retirement Handoff

**Date:** 2026-08-27
**Retired backlog row:** 2026-08-14 object-storage — "Hosted R2 run covering
the sanitized JUnit harness is not yet recorded; `run_nonce` remains
decorative" (gate: Before Child 7 and production activation)
**Source ruling:** `docs/handoff/2026-08-20-object-storage-backlog-retirement.md`
§Hosted-live prerequisite and next external action

## Final application commit

| Change | SHA | Subject |
| --- | --- | --- |
| `run_nonce` removal | `49ba212a05ae789a377cf3027661d2d8dee9e08e` | `fix: remove decorative live manifest run nonce` |

The documentation commit (this handoff, the operations-doc proof section and
the BACKLOG row removal) lands after this application commit.

## Gate status: hosted R2 proof of the sanitized JUnit harness

**Recorded and green.** The protected workflow
`.github/workflows/object-storage-live.yml` executed on the final hardened
application commit `49ba212a05ae789a377cf3027661d2d8dee9e08e`, which contains
the complete harness-hardening chain: JUnit sanitization (`1ec3a01`), the
concurrency-race correction (`da52777`), the zero-byte live-diagnostics
hardening (`bd2447e` → `37d1def`) and the `run_nonce` removal itself.

Sanitized evidence only (no credentials, endpoint, bucket name, raw JUnit
output or configuration is recorded):

- **Workflow run reference:** `leduc4894/personal_os` actions run
  [33069347334](https://github.com/leduc4894/personal_os/actions/runs/33069347334)
  — "object storage live R2", job "Ubuntu dedicated-bucket R2 contract".
- **Trigger/event:** push of the final application commit to `master` (the
  workflow's trusted-surface trigger; identical workflow file and ref as a
  manual dispatch would select).
- **Commit SHA:** `49ba212a05ae789a377cf3027661d2d8dee9e08e`.
- **Date:** 2026-08-27, started 11:53:16 UTC, completed 11:54:01 UTC.
- **Result:** conclusion **success**; every job step green, including
  "Run live R2 contract cases with exact-key cleanup", "Remove unsanitized
  JUnit staging file", "Remove dedicated test secret files" and
  "Upload sanitized JUnit report".
- **Case count/outcome:** **9 passed** (log summary `9 passed in 26.31s`) —
  the full design 16.2 live set: zero-byte round trip, multi-chunk round
  trip, duplicate store, concurrent conditional create, missing object,
  size/media conflict, corrupted object, lost-response-equivalent resolution,
  exact cleanup after forced test exception.
- **Artifact confirmation:** the run published exactly **one** artifact —
  `object-storage-live-junit-33069347334-1` (615 bytes zipped, 1556 bytes
  XML), the sanitizer's upload step output. The downloaded report was
  verified to contain only suite/case identity and statuses: nine passed
  testcases, no `failure`/`error` nodes, no
  `system-out`/`system-err`/`properties` elements, and no case text.

The historical 2026-08-14 hosted run (`22dccca`, run 31791535221) remains
what it was: proof of the pre-sanitization harness. The record above is the
first hosted proof that exercises and publishes the sanitized JUnit path.

The living status now lives in
`docs/operations/object-storage.md` §Hosted proof of the sanitized JUnit
harness (2026-08-27); this handoff is the point-in-time snapshot.

## Decision: `run_nonce` disposition

**Ruled: removed, not made load-bearing.**

Rationale:

1. The manifest nonce never bound anything. Payloads are per-run random via
   `secrets.token_bytes`, which is strictly stronger per-run uniqueness than
   deriving bytes from a nonce that is generated once and then never read.
2. The nonce was never persisted, logged or compared, so it could not serve
   reproducibility or audit either — a nonce-derived payload would be just as
   unrecoverable as a random one.
3. The exact-key cleanup contract binds to the dedicated bucket plus the
   recorded canonical keys of the current run; a run identity adds no
   safety to that validation.
4. Keeping it made the harness docstrings overclaim ("payloads … bound to the
   manifest nonce") — removal restores an honest description.

The constructor surface is now `LiveCleanupManifest(bucket_name=...)`; the
`run_nonce` property, the fixture's `uuid.uuid4().hex` argument and the
module docstring's nonce claim are gone. No design document (16-TESTING,
operations runbook) ever specified the nonce, so no canonical contract
changed.

## RED/GREEN evidence

- RED: changed `_manifest_with` in
  `tests/integration/r2_object_storage/test_live_cleanup_manifest.py` to
  construct without `run_nonce` — `uv run pytest
  tests/integration/r2_object_storage/test_live_cleanup_manifest.py -q`
  exited 1 with **9 failed** (missing required keyword `run_nonce`).
- GREEN: `uv run pytest
  tests/integration/r2_object_storage/test_live_cleanup_manifest.py -q`
  exited 0 (10 passed) after the removal.
- Focused suites: `uv run pytest tests/integration/r2_object_storage/
  test_live_cleanup_manifest.py tests/integration/r2_object_storage/
  test_live_junit_sanitization.py tests/contract/object_storage
  tests/unit/object_storage tests/contract/test_ci_security.py -q`
  — exit 0, **335 passed, 3 skipped**.
- Live-module collection: `uv run pytest
  tests/integration/r2_object_storage/test_live_r2_adapter.py -m r2_live
  --collect-only -q` — exit 0, **9 tests collected**.
- `rg -n "run_nonce"` over non-markdown sources: no remaining references.
- `uv run ruff check`, `uv run ruff format --check`,
  `uv run mypy` over the three changed files: exit 0.
- `git diff --check`: exit 0.

## Deferred items

None from this slice. The two remaining object-storage BACKLOG rows
(`_run_shielded` cancellation edge; test-hygiene batch) predate this row,
were explicitly left indexed by the 2026-08-20 retirement handoff, and keep
their existing `Before Phase 2 closure (after Child 9)` gates.

## Next actions

1. None for this row — the "Before Child 7 and production activation" gate
   is cleared for its object-storage half. Child 7's other blocker (the
   physical Mobile reference-device matrix, 2026-08-27 row) is untouched.
2. Production activation itself remains the documented deliberate deployment
   decision in `docs/operations/object-storage.md`, not a test status.
