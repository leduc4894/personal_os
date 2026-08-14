# Source Version Publication Handoff

**Date:** 2026-08-14
**Plan:** `docs/superpowers/plans/2026-08-14-source-version-commit-and-idempotency.md`
**Spec:** `docs/superpowers/specs/source-version-commit-and-idempotency-design.md` (at `2ba4ff1`)
**Branch:** `source-version-commit` (worktree `.worktrees/source-version-commit`)
**Final commit:** `5923f83` (plus handoff/BACKLOG doc commit)

## Gate status (with evidence)

| Gate | Status |
|---|---|
| Task-level reviews (13 tasks) | ✅ all approved; 3 Important findings fixed in review loops (Task 8 invariant-rejection rollback `158a6ef`, Task 9 integrity-retry gating `fc375c7`, Task 11 describe/connect RPC bounds `55f5e87`) |
| Final whole-branch review | ✅ merge-ready — 16/16 completion checklist, no Critical/Important findings, no fix-before-merge deferred items |
| Full quality gate (`poe verify`) | ✅ exit 0 at `2c95652` + docs-only delta (format, Ruff, mypy strict, import-linter, Python+TS tests, builds) — see `docs/superpowers/tasks/`-style evidence in the plan workspace reports before deletion; canonical summary in Task 13 report |
| Disposable PostgreSQL/Temporal gate | ✅ 73 passed in ~13:54 (`knowledge-ci-81413-1`, PG 15443 / Temporal 17243/18083); exact-label cleanup verified (0 leftover containers/volumes/networks) |
| Migration baseline | ✅ Alembic head `20260813_01` only; nine-table baseline unchanged; zero commits touch `migrations/` |
| 10k query plans / 100-replay concurrency | ✅ all EXPLAIN probes indexed (approved set derived by AST from the migration); 100 replays → one canonical event; pool clean |

Living operational status: `docs/operations/source-publication.md`.

## What was built

- `src/personal_os/sources/` — provider-neutral commands, fingerprints, safe diff hashes,
  closed errors/metrics/events, publication service (preflight → R2 dedup/verify → commit),
  projection lease state machine, ports.
- `packages/postgresql-source-store/` — async engine (READ COMMITTED, SET LOCAL bounds),
  nine-table DML metadata (contract-tested against the migration), advisory locks
  (`pg_advisory_xact_lock`, idempotency→source order), idempotency preflight/replay
  hydration, atomic create/update/no-change commits with `_RejectionAbort` rollback
  discipline, bounded retries + ambiguous-commit recovery, fenced projection-intent leases.
- `apps/worker/src/workflow_worker/` — Temporal adapter (workflow `SourceIngestionWorkflow`,
  id `source-ingestion/{workspace_id}/{event_id}`, queue `source-ingestion`, closed input,
  `USE_EXISTING`, 10 s caller bounds on start/describe/connect) and the 8-concurrent
  dispatcher runtime with graceful shutdown.
- CI: `canonical-postgresql-baseline.yml` extended (path triggers, three integration
  suites, Temporal ports, 45-min budget).

## Spec interpretations (with reasons)

1. **Audit trust boundary** = trusted-active-workspace row; `actor_invalid` audited after
   it, never before (Task 7) — only coherent reading of spec §7/§10.3.
2. **Impossible event shapes** audited with `reason_code NULL` (closed set has no token).
3. **Metric rejection vocabulary** keeps registry-code tokens (Task 4 enum); spec §10.3's
   short tokens are the audit-row vocabulary only.
4. **`source_version_publish_*` diagnostic events** registered but emitted by no task in
   this plan — Phase 1 has no composition root invoking the service (unowned; BACKLOG).
5. **Integrity failures never retry**: recovery lookup gated to connection-class
   (`UNAVAILABLE`) failures; server-returned SQLSTATEs prove deterministic rollback (Task 9 fix).
6. **Windows local runs** need `NEO4J_BOLT_PORT=17687` (OS port reservation); Linux CI unaffected.

## Deferred items (rulings)

All deferred items below were adjudicated non-blocking by the final review. Grouped:
cosmetic/hygiene items are indexed in `docs/handoff/BACKLOG.md` with section links.

- §1 Import-cycle workaround: `diagnostics/context.py:15` dead `DiagnosticContextError`
  re-export (pre-existing) — delete the one-liner; the diagnostics-first import in
  `postgresql_source_store/__init__.py` can then be removed.
- §2 `source_version_publish_*` events registered but never emitted (no Phase 1 composition root).
- §3 Dispatcher exits on transient DB outage after bounded retries (crash-safe via lease
  expiry ≤60 s; one PG restart bounces the process) — runbook note + resilience follow-up.
- §4 `IdempotencyKey`/`SourceTitle` raw-value `__repr__` — no production path reprs them
  today; re-redact before any future task that formats commands into logs.
- §5 Retryable busy/unknown outcomes counted under `rejected` in `publish_total` (closed
  3-value enum) — remember when wiring alerts.
- §6 Test-hardening batch: Seq Scan matcher (`endswith`), pool-status string assertion,
  `no_public_api` broad substrings (`publication`, `/sources`), AST scanner evasions,
  100-replay CI-margin watch, tightening `reason == "actor_invalid"`, zero-mutation
  assertions on update replays, positional-index leftovers, unused/dead constants
  (`MAXIMUM_RECEIPT_AGE`, settings timeout literals), private-attribute test accesses.
- §7 Fingerprint fixture provenance note; digest-value-object duplication if a third type appears.
- §8 Stale-lease diagnostics: pre-commit emission; `lease_expired` error code for
  wrong-token-on-active-lease; `stale_lease` is event-only (never a metric label).
- §9 Dispatcher polish: whole-batch shutdown wait; engine not disposed on connect-timeout;
  non-timeout connect failures traceback; `Client.close()` never called.
- §10 Adapter contract tightening: postgres forbidden set omits `fastapi`/`aiohttp`/`boto3`
  family (subset test); field-map value-equality; validation-module extraction.

## Next actions

1. Merge `source-version-commit` into `master` (clean fast-forward; full gate already green).
2. Watch the first CI run of the extended ubuntu-lifecycle job (~14 min local; 45-min budget).
3. Pick up BACKLOG §1 (import-cycle deletion) promptly — small, unblocks removing the
  import-order workaround.

No blockers. Phase 1 queues `SourceIngestionWorkflow` starts but does not register the
Phase 3 workflow implementation (by plan scope).
