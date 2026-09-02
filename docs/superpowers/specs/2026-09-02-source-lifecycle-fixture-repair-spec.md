# Source Lifecycle Fixture Repair Spec

**Status:** approved for planning-only work on 2026-09-02

## Goal

Restore the two disposable-stack source-lifecycle integration suites as trustworthy
tests without changing canonical lifecycle, policy, backup, or projection behavior.

## Scope

This spec owns the two reproducible BACKLOG rows dated 2026-09-02:

1. Lifecycle policy-expectation drift in `test_lifecycle_transactions.py` and
   `test_projection_dispatch.py`.
2. Invalid `projection_intents` fixture rows in `test_backup_restore.py` and
   `test_query_plans.py`.

Desktop/Mobile acceptance, device-sync, plugin onboarding, large-vault import,
backup-manifest v5 work, refactor-only rows, tool-pin warnings, and all
conditional “next change” rows are explicitly out of scope.

## Contract

- `PostgresqlSourceLifecycleStore.commit()` re-evaluates the transaction-locked,
  signed active policy. A caller-provided `LifecyclePolicyDecision` is not the
  authoritative verdict for projection intent selection.
- A lifecycle test that expects `delete` intents must seed an active signed rule
  that makes the locked re-evaluation deny.
  An empty signed policy is allow-all and therefore produces `upsert` intents.
- Every `projection_intents.event_id` fixture value must name an existing
  `sync_events.event_id`; a source version UUID is never an event UUID.
- The repair is test/harness-only. It must not change migrations, table schema,
  lifecycle store production code, backup implementation, or the snapshot
  manifest version.

## Acceptance criteria

1. The lifecycle transaction and projection-dispatch cases prove the intended
   locked-policy denial rather than relying on an untrusted passed decision.
2. Backup and query-plan fixture insertion satisfies the event foreign key and
   keeps all existing snapshot-count and EXPLAIN assertions meaningful.
3. The four integration files pass on one disposable `knowledge-ci-*` stack
   created with `.local/serve-live-ci.sh`; no Desktop/Mobile evidence is used.
4. Only the two resolved source-lifecycle/lifecycle-fixture rows are removed
   from `docs/handoff/BACKLOG.md` after fresh verification.
5. One final source-lifecycle handoff records the exact CI project (redacted if
   required), commands, exit statuses, decisions, and remaining backlog scope.

## Failure handling

If a focused failure shows a production contract defect instead of fixture drift,
stop this plan, leave its backlog row intact, and create a new spec/plan for the
owning production domain. If the disposable stack cannot be started, retain the
rows and record the sanitized blocking reason in the single handoff.
