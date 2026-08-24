# Journal orchestration observability — design spec

Date: 2026-08-24. Domain: Obsidian plugin journal orchestration. Governing
rule: `AGENTS.md` and `docs/15-OBSERVABILITY_AND_ALERTING.md`: every closed
error path must surface a closed reason token in a readable trail, settings
surface, admin diagnostics route, or structured log.

## Problem

The final repository-wide observability sweep found six groups of closed paths
in the plugin journal orchestration that discard their reason after choosing a
safe outcome. Existing queue and lifecycle behavior is intentionally
fail-closed, but the durable Sync diagnostics trail cannot explain why a retry
was not armed, an admission was skipped, or reconcile state was not persisted.

Verified gaps:

1. `apps/obsidian-plugin/src/plugin.ts:1213` drops an exception while reading
   the earliest retry deadline, so no retry timer is armed without a reason.
2. `apps/obsidian-plugin/src/plugin.ts:1292` drops failures of the aggregate
   status projection and returns no snapshot without a reason.
3. `apps/obsidian-plugin/src/journal/automatic-snapshot.ts:40,106` converts
   rejected queue and automatic-snapshot drains to `undefined`.
4. `apps/obsidian-plugin/src/journal/capture.ts:237` converts a settled Vault
   admission rejection to `undefined` before releasing its waiters.
5. `apps/obsidian-plugin/src/journal/capture.ts:380` counts a failed automatic
   snapshot admission as skipped but retains no failure reason.
6. `apps/obsidian-plugin/src/journal/lifecycle-capture.ts:336,585,592` drops
   failures while persisting the required reconciliation state.

## Goals

1. Surface a closed, site-specific token for every listed path in the existing
   Sync diagnostics trail.
2. Preserve all current scheduling, fail-closed, retry, and Vault admission
   semantics; this is observability-only behavior.
3. Bound trail growth: no unbounded per-file or per-trigger append storm.
4. Keep all surfaced fields safe: closed tokens only, never exception text,
   paths, vault bytes, ids, hashes, credentials, or provider data.

## Non-goals

- No new browser UI, HTTP route, logging sink, metric, or external dependency.
- No retry policy, queue ordering, lifecycle state-machine, or error mapping
  change.
- No attempt to persist a trail record when the trail itself is unavailable;
  existing bounded append-failure accounting remains the fallback.

## Design

### Closed vocabulary

Extend `SyncCompositionReadFailureToken` and therefore
`SyncDiagnosticClosedToken` with these exact snake-case tokens:

| Token | Closed path |
| --- | --- |
| `retry_schedule_read_failed` | earliest retry deadline read failed; timer is not armed |
| `sync_status_read_failed` | aggregate sync-status read failed; no partial snapshot is rendered |
| `queue_drain_failed` | coalesced queue-pass drain rejected |
| `snapshot_drain_failed` | automatic-snapshot drain rejected |
| `settled_admission_failed` | debounced Vault admission rejected before its waiters released |
| `automatic_snapshot_admission_failed` | one automatic scan item failed and was counted skipped |
| `lifecycle_reconcile_persist_failed` | required reconcile state could not be persisted |

Every token rides the existing `journal_failure` trail kind. The diagnostic
export and settings Copy diagnostics therefore acquire the vocabulary without a
new rendering surface.

### Reporter seam

Introduce a narrow plugin-local reporter interface whose only operation accepts
one closed `SyncDiagnosticClosedToken`. The composition root implements it by
appending `{ kind: "journal_failure", tokens: [token] }` to the existing
`SyncDiagnosticsTrail`.

`AutomaticSnapshotCoordinator`, `CoalescingQueuePassDispatcher`, vault capture,
and lifecycle capture receive that reporter at composition time. They do not
receive the trail itself and cannot append arbitrary strings. This preserves the
trail's closed vocabulary type boundary and keeps journal modules independent
of plugin UI composition.

### Bounded emission rules

- The two composition-root reads emit once per plugin session per site, matching
  the existing `status_read_failed`/`note_status_read_failed` pattern.
- Queue drain, snapshot drain, and settled-admission failure emit once for the
  failed drain/admission execution. A later independently started execution can
  emit again.
- Automatic snapshot admissions coalesce to one
  `automatic_snapshot_admission_failed` entry per scan, irrespective of the
  number of skipped files caused by errors. `skippedFileCount` remains exact.
- Each failed reconcile-persist attempt emits one
  `lifecycle_reconcile_persist_failed` entry. The operation stays rejected; it
  must not masquerade as a successfully durable reconcile requirement.

### Required behavior by gap

1. Retry-deadline read failure leaves no timer armed and appends
   `retry_schedule_read_failed` once.
2. Aggregate status read failure returns `null` exactly as today and appends
   `sync_status_read_failed` once.
3. Each coordinator preserves its current settled promise and cleanup behavior;
   a queue drain append uses `queue_drain_failed`, while a snapshot drain uses
   `snapshot_drain_failed`.
4. A rejected debounced admission releases waiters exactly as today after
   appending `settled_admission_failed`.
5. Per-item automatic scan rejections still increment `skippedFileCount`; the
   scan appends the one coalesced token before it returns its summary.
6. Reconcile-persist failure remains a fail-closed rejected lifecycle action.
   It appends `lifecycle_reconcile_persist_failed`; callers never treat it as a
   durable reconcile state.

## Acceptance criteria

1. Every listed catch has a RED test proving no trail entry before the change
   and a GREEN behavioral or source-contract test proving its exact token.
2. TypeScript rejects a reporter call with an arbitrary string.
3. The automatic scan test proves multiple failed paths add their skipped count
   but exactly one failure token for the scan.
4. The retry and status read paths append at most once per session/site.
5. Existing coordinator coalescing, shutdown, lifecycle, and queue ordering
   tests remain green unchanged in outcome.
6. Sync trail parse/export tests accept all seven new tokens and reject an
   arbitrary token.
7. The runbook lists the new tokens and their safe operator meaning.
8. `pnpm --filter @workspace/obsidian-plugin test`, TypeScript type checking,
   lint/format checks, and `uv run poe verify` pass on the final commit.

## Error and privacy cases

- A reporter append remains fire-and-forget. If it cannot persist, the existing
  trail append-failure counter is the observable degradation; it never blocks
  the original safe outcome.
- Reporter code catches no exception text. The exception determines only the
  fixed token selected by its static call site.
- A missing reporter is permitted only for isolated unit construction; it is a
  no-op. Production composition always supplies the reporter.
- Cancellation remains propagated wherever the current code propagates it;
  token surfacing must not convert cancellation into a success or an extra retry.
