# Object Storage Live Activation Handoff

**Date:** 2026-08-14
**Follows:** `2026-08-14-content-addressable-object-storage.md` (its "Required next actions" are now complete)
**Final commit:** `22dccca` (pushed to `origin/master`)

## Gate status (with evidence)

| Gate | Status |
|---|---|
| Local live run (`poe object-storage-test-live`) | ✅ 9/9 passed in ~21s (Windows, developer secret files) |
| Protected workflow live gate | ✅ run [31791535221](https://github.com/leduc4894/personal_os/actions/runs/31791535221) — 9/9 passed in 17.6s on `master` @ `22dccca`, JUnit artifact uploaded |
| Spec §16.2 case set | ✅ complete — all nine cases, including case 8 added this session |

Phase 1 object-storage live gate is satisfied; production activation is now a
deployment decision, not a test status (see
`docs/operations/object-storage.md` → "Acceptance status").

## What this session changed

1. **Live case 8** (`test_repeated_lost_response_equivalent_resolution`) added
   to `tests/integration/r2_object_storage/test_live_r2_adapter.py`: an
   out-of-band writer lands the canonical object (the live equivalent of a
   lost PUT response); adapter resolves HEAD → exists → full verify
   (`EXISTING_FULL_READ`), never overwrites, reads back exact bytes.
2. **Workflow fix** (`.github/workflows/object-storage-live.yml`): `runner.temp`
   is a step-level context — `R2_TEST_SECRET_ROOT` moved from job-level `env`
   to each step's `env` (GitHub rejected the workflow with HTTP 422 otherwise).
3. **Credential-shape guard** in the workflow write step: R2 access key ids are
   exactly 32 chars, secret access keys exactly 64; a mismatch fails with a
   safe lengths-only error. This immediately caught a malformed repository
   secret (52 chars) that would otherwise surface as a cryptic SigV4 failure.
4. **Secret reader hardening** (`secret_files.py`): strips *every* trailing
   CR/LF (a lone `\r` from a Windows clipboard corrupts single-line headers
   such as SigV4 `Credential=`); interior whitespace preserved; tests updated
   to the new contract.

## Configuration state (GitHub repository)

- Variables: `R2_TEST_ENDPOINT`, `R2_TEST_BUCKET_NAME` ✅
- Secrets: `R2_TEST_ACCESS_KEY_ID` (32), `R2_TEST_SECRET_ACCESS_KEY` (64) ✅
  — re-entered cleanly after the guard caught the malformed value
- A stray environment named `R2_TEST_ENDPOINT` exists from initial setup;
  harmless (workflow uses no `environment:`), delete when convenient:
  `gh api -X DELETE repos/leduc4894/personal_os/environments/R2_TEST_ENDPOINT`

## Observations for the record

- The concurrency group (`cancel-in-progress: true`) cancelled the
  push-triggered run in favor of the manual dispatch — the deferred BACKLOG
  note about orphaned objects on cancellation remains relevant.
- The pre-existing circular import (BACKLOG item 15) hit again when running
  `tests/unit/runtime_configuration/test_secret_files.py` alone; priming with
  `tests/unit/error_contracts/test_application_errors.py` works around it.
- The `Node.js 20 deprecated` annotation on `upload-artifact` is upstream
  noise; ignore until the action ships a Node 24 build.

## Next actions

None blocking. Optional cleanups live in `docs/handoff/BACKLOG.md` (the
shielded-cleanup consolidation, disk-usage offload, and the stray-environment
deletion above).
