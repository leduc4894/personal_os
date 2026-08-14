# Object Storage Implementation Handoff

**Date:** 2026-08-14
**Plan:** `docs/superpowers/plans/2026-08-14-content-addressable-object-storage.md` (all 12 tasks complete)
**Design spec:** `docs/superpowers/specs/content-addressable-object-storage-design.md`
**Merged:** `master` at `8e82486` (15 commits, fast-forward), branch and worktree removed.

## Gate status

| Gate | Status |
|---|---|
| Offline `uv run poe verify` | ✅ exit 0, verified by controller on `8e82486` (format, lint, strict mypy, import boundaries, Python + TS tests, builds) |
| Forbidden-capability audit | ✅ no production multipart/list/delete/copy/presign/`upload_fileobj`; delete exists only in the live test harness |
| Privacy audit | ✅ no committed credential/value/signed-URL |
| **Live R2 gate** | ❌ **BLOCKED** — `live activation blocked: dedicated test-bucket credentials not configured` (recorded in `docs/operations/object-storage.md` and `tests/integration/README.md`) |

**Phase 1 production activation is NOT complete** until the live gate passes on the implementation commit.

## Required next actions (in order)

1. **Configure live-test credentials** (GitHub repo → Settings → Secrets and variables → Actions):
   - Variables: `R2_TEST_ENDPOINT`, `R2_TEST_BUCKET_NAME` (dedicated private test bucket — never the production bucket)
   - Secrets: `R2_TEST_ACCESS_KEY_ID`, `R2_TEST_SECRET_ACCESS_KEY` (token scoped Object Read & Write on that bucket only)
2. **Add spec §16.2 live case 8** — repeated/lost-response-equivalent resolution — to `tests/integration/r2_object_storage/test_live_r2_adapter.py`. The plan's case list omitted it; the live gate must not be declared passing without it.
3. **Dispatch the workflow** on master (Actions → *Object storage live* → Run workflow; or wait for schedule). On green, record the run URL/date in `docs/operations/object-storage.md` and flip the blocked status.
4. Optional cleanups before/at activation (see deferred list): set workflow `concurrency.cancel-in-progress: false` (a cancelled mid-run job skips exact cleanup and the live bucket has no janitor).

## Spec interpretations decided during implementation (do not re-litigate)

| Question | Ruling | Where |
|---|---|---|
| Does single-flight cover verify paths? | **Store-path only.** §6.5's precondition ("after each has independently validated its own input stream") applies to stores; §7/§8 mandate a fresh full verification per read call, which shared single-flight would violate. | `adapter.py` |
| Unknown exception in R2 operation? | Maps to `InternalApplicationError(INTERNAL_ERROR)` (the registry's composition-boundary type) — NOT `ObjectStorageError`, whose `allowed_codes` is contract-pinned to the nine §12 codes. Runtime check exits 70. | `error_mapping.py` |
| GET-time changed ETag (412 on If-Match GET)? | `object_storage_integrity_failed` per §7 ("changing ETag … is an integrity failure"). Distinct from PUT-time 412 = dedup/winner-verify signal per §11. | `adapter.py` |
| HEAD-time size vs media mismatch? | Size → `object_storage_integrity_failed`; media → `object_storage_metadata_conflict` per §6.3. | `require_exact_metadata` |
| Runtime-check janitor degradation? | Emits `object_storage_spool_cleanup_degraded` (real `deferred_count`; count 0 only on exception) and **never skips the HeadBucket probe, never changes the exit code** per §9.3/§14.2. | `runtime_check.py` |
| `BadDigest` retry? | Terminal (non-retryable) per §11, despite botocore's own retryable policy. | `error_mapping.py` |
| Real botocore HEAD misses? | Arrive as `Error.Code == "404"` (status synthesized, bodiless response). `head_object("404") → None`; `head_bucket("404") → object_storage_unavailable`. | `client.py` |

## Deferred minors (triaged, accepted for merge)

*Deferred = real but non-blocking; batch into a later cleanup pass.*

1. `PutObjectRequest` dual MD5 name: field `content_md5_base64` + alias property `content_md5` (the plan's Task 8 test pins `put.content_md6`… `content_md5` verbatim; settle both names together).
2. `InternalApplicationError` bypasses the adapter's `except ObjectStorageError` failure-recording handlers — internal bugs decrement in-flight via `finally` but are absent from failure metrics. Add explicit internal-failure recording if operator metrics need it.
3. Duplicated shielded-cleanup helpers: `_run_shielded` (adapter.py) ≈ `_run_shielded_cleanup` (spool.py). Consolidate when a third caller appears.
4. `shutil.disk_usage` runs on the event loop under the admission lock (one statvfs per admission attempt).
5. `asyncio.timeout(600)` real-time receive backstop untested (defense-in-depth; maps to the same typed error as the tested injected-clock path).
6. Single-flight: unretrieved-future guard is dead code under current lock discipline (keep as invariant guard); `_run_shielded` cancel path could swallow `CancelledError` if cleanup itself raised (no current cleanup does).
7. `maximum_reserved_size_bytes` metric is sampled, not exact (enforcement is the spool manager's, so this is observability fidelity only).
8. `CanonicalObjectKey` has no validating `parse()` in core (spec §5.2 grammar); the only parser is the harness-private regex. Add before any future consumer parses key strings.
9. Waiter `attempt_count` is synthetic (`max(tracker.count, 1)`); waiters share one receipt but record attempt 1.
10. Shared single-flight waiters re-raise the owner's exception instance (accumulated tracebacks, N failure records).
11. Test hygiene: resource-suite fixture mutates the root logger; `run_bounded` failure path abandons pending tasks; `capture_diagnostic_events` reads a private `DiagnosticLogger` attribute; two `assert`-for-control-flow spots in `adapter.py`; redundant `^$` anchors in settings regexes; `Path("/run/secrets")` default never valid on win32 (tests always override).
12. `duration_ms` in the runtime check includes client construction time.
13. Ops-guide phrasing: "failed/degraded counterpart" — only a *failed* probe event exists (`object_storage_operation_failed`).
14. Live harness: raw aiobotocore tracebacks could render the endpoint URL in JUnit (endpoint is a non-secret repo variable; consider `--tb=no` for the live command); temp spool dir leaks if the loader rejects after `mkdtemp`; `run_nonce` is decorative; workflow `cancel-in-progress: true` can orphan live-bucket objects.
15. Pre-existing (NOT this branch): circular import in `tests/unit/runtime_configuration/test_secret_files.py` breaks directory-scoped pytest collection; the canonical full-suite gate is unaffected.

## Review history

Every task passed a task-scoped review (spec + quality); six tasks required fix rounds, all re-reviewed clean:

- Task 1: Poe gate paths extended to cover the new member source.
- Task 5: janitor split into a wall/epoch clock for mtime ages (monotonic clock kept for deadlines); fd-close-under-cancellation leak closed.
- Task 6: `BadDigest` → terminal per §11; real-botocore `"404"` HEAD-absence mapping.
- Task 7: changed-ETag → `INTEGRITY_FAILED` per §7; spool.py format drift fixed; true `attempt_count` plumbed via the retry callback; reader-open failure cleanup hardened.
- Task 8: HEAD-time size mismatch → `INTEGRITY_FAILED` per §6.3.
- Task 10: janitor degradation no longer skips the probe or hijacks the exit code per §9.3/§14.2.
- Final whole-branch review: "With fixes" → one fix wave (`8e82486`): deferred-janitor event with real count; unknown exceptions → `INTERNAL_ERROR`; `python-format` gate symmetry; public `OBJECT_MISSING_ERROR_CODES` rename. Scoped re-review clean.

## Key entry points

- Port + contracts: `src/personal_os/object_storage/`
- Adapter: `packages/r2-object-storage/src/r2_object_storage/` (exported: `R2S3ObjectStore`, settings loader)
- Runtime check: `uv run object-storage-check-runtime --service api|mcp|worker` (exits 0/2/69/70/78)
- Live tests: `uv run poe object-storage-test-live` (fails, never skips, without credentials)
- Operator guide: `docs/operations/object-storage.md`
