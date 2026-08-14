# Deferred Work Backlog

Single living index of every deferred (accepted-but-not-done) item across all
handoffs. Each item is ONE line pointing to the handoff that holds the full
context and ruling. Remove the line when the item is done — done work lives in
git history, not here.

Scope guard: this file indexes DEFERRED work only. Gates and requirements
belong to `docs/20-IMPLEMENTATION_PLAN.md`; current status of a gate belongs
to the living domain doc (e.g. `docs/operations/`). Do not duplicate those
here — link them at most.

| Added | Domain | Item | Details |
|---|---|---|---|
| 2026-08-14 | object-storage | `PutObjectRequest` dual MD5 name (`content_md5_base64` field + `content_md5` alias); settle on one name together with the plan-pinned test | [handoff §1](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | `InternalApplicationError` bypasses adapter failure-metrics handlers; add explicit internal-failure recording if operator metrics need it | [handoff §2](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Consolidate duplicated shielded-cleanup helpers (`_run_shielded` / `_run_shielded_cleanup`) when a third caller appears | [handoff §3](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | `shutil.disk_usage` runs on the event loop under the admission lock | [handoff §4](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | `asyncio.timeout(600)` real-time receive backstop untested (defense-in-depth) | [handoff §5](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Single-flight: unretrieved-future guard dead code (keep as invariant); `_run_shielded` cancel-swallow edge if cleanup ever raises | [handoff §6](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | `maximum_reserved_size_bytes` metric sampled, not exact | [handoff §7](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Add validating `CanonicalObjectKey.parse()` to core before any future consumer parses key strings (spec §5.2) | [handoff §8](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Waiter `attempt_count` synthetic (`max(tracker.count, 1)`) | [handoff §9](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Single-flight waiters share one exception instance (accumulated tracebacks, N failure records) | [handoff §10](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Test-hygiene batch: root-logger fixture mutation; `run_bounded` failure path abandons tasks; private `_diagnostic_schema_record` read; assert-for-control-flow ×2; redundant `^$` anchors; win32 `/run/secrets` default | [handoff §11](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Runtime-check `duration_ms` includes client construction time | [handoff §12](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Ops-guide "failed/degraded counterpart" phrasing — only a failed probe event exists | [handoff §13](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | object-storage | Live-harness minors: endpoint in raw tracebacks (`--tb=no` candidate); temp-dir leak on loader rejection; `run_nonce` decorative; `cancel-in-progress: true` can orphan live-bucket objects | [handoff §14](2026-08-14-content-addressable-object-storage.md) |
| 2026-08-14 | infra (pre-existing) | Circular import in `tests/unit/runtime_configuration/test_secret_files.py` breaks directory-scoped pytest collection; full-suite gate unaffected | [handoff §15](2026-08-14-content-addressable-object-storage.md) |
