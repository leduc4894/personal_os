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
| 2026-08-14 | source-publication | Delete dead `DiagnosticContextError` re-export (`diagnostics/context.py:15`, pre-existing) then drop the diagnostics-first import workaround in `postgresql_source_store/__init__.py` | [handoff §1](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | `source_version_publish_*` diagnostic events registered but emitted by no task (Phase 1 has no composition root invoking the service) | [handoff §2](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | Dispatcher exits on transient DB outage after bounded retries (crash-safe via lease expiry; one PG restart bounces process) | [handoff §3](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | `IdempotencyKey`/`SourceTitle` raw-value `__repr__` — no production path reprs them; re-redact before any task that formats commands into logs | [handoff §4](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | Retryable busy/unknown outcomes counted under `rejected` in `publish_total` — caveat for alert wiring | [handoff §5](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | Test-hardening batch: Seq Scan matcher should `endswith`; pool-status string assertion redundant; `no_public_api` broad substrings; AST scanner evasions; 100-replay CI margin; misc assertion tightening; dead constants; private-attribute test accesses | [handoff §6](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | Fingerprint fixture provenance note in test docstring; extract shared hex64 parse if a third digest type appears | [handoff §7](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | Stale-lease diagnostics: emitted pre-commit; `lease_expired` mislabels wrong-token-on-active-lease; `stale_lease` event-only token | [handoff §8](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | Dispatcher polish: whole-batch shutdown wait; engine not disposed on connect-timeout; non-timeout connect failure traceback; `Client.close()` never called | [handoff §9](2026-08-14-source-version-publication.md) |
| 2026-08-14 | source-publication | Adapter contract tightening: postgres forbidden set omits fastapi/aiohttp/boto3 family; field-map value-equality test; shared validation module | [handoff §10](2026-08-14-source-version-publication.md) |
| 2026-08-15 | canonical-core | Identity input-validation hardening: typed error for non-string free-text inputs; distinct reason token for non-string username/workspace-key | [handoff §1](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Latent circular import `diagnostics` <-> `error_contracts.exceptions` dodged by import ordering; deserves dedicated structural fix | [handoff §2](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Canonical-read hardening: consumer-body ApplicationError conflated with read-failure metric/event; missing/corrupt tests assert metric but not FAILED event | [handoff §3](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Lookup-statement filter test name overpromises (join count asserted, not WHERE predicate) | [handoff §4](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Recovery contract edges: `CanonicalBackupSnapshot` default repr exposes snapshot_token; non-dict JSON manifest reports contract_unsupported instead of json_noncanonical | [handoff §5](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Bundle-store minors: finalize-rename TOCTOU vs empty final dir on POSIX; verify-totals object_count check tautological; conftest mkdtemp prefix cryptic | [handoff §6](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Dump-process adapter hardening: ProcessRunResult.stdout in repr; chatty-child post-exit drain false-timeout; passfile escaping; restore-timeout mapping untested | [handoff §7](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Snapshot-adapter precision: pending-writer relname-only join (no pg_namespace); alembic_version hardcoded public schema; SET LOCAL lock_timeout inert for NOWAIT | [handoff §8](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Bounded-memory/event-loop hygiene: buffered copy materializes <=100MiB per object; sync file I/O in coroutines; failed-restore metrics hardcode 0/0 totals | [handoff §9](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | CLI admission refusals reuse ENVIRONMENT_REFUSED result_code (exit 78 correct, label misleading); split token with registry change | [handoff §10](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | CLI composition hygiene: no compose-time engine disposal (lazy, no connection); canonical-core-test Poe task not composed into verify | [handoff §11](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Integration-harness hygiene: bundle_root POSIX temp leak; Any-typed runner/shim signatures; fake object store passes same-digest-different-media re-store | [handoff §12](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Live-harness type precision: cast(LocalFilesystemObjectStore) type-lie; unused discarded harness in live_acceptance_context | [handoff §13](2026-08-15-canonical-core-acceptance-and-recovery.md) |
| 2026-08-15 | canonical-core | Acceptance polish: boundary test name overstates DATABASE_URL/PGPASSWORD scope; duration_ms bypasses clock seam; workflow dead TEMPORAL_*_PORT env vars | [handoff §14](2026-08-15-canonical-core-acceptance-and-recovery.md) |
