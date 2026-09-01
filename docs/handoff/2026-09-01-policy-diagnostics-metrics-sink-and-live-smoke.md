# Policy diagnostics metrics sink and live smoke — handoff

Branch: `policy-diagnostics-metrics-sink-and-live-smoke` (from `master` `e11190d`).
Plan: `docs/superpowers/plans/2026-08-31-policy-diagnostics-metrics-sink-and-live-smoke.md`.
Final commit of the round: `8956a45` (docs evidence); code head `460064f`..`8956a45` block below.

## Gate status (with evidence)

- **Task 1 — Prometheus sink route `GET /api/admin/metrics`**: DONE. Commits
  `38697ce` (route + renderer + tests + OpenAPI/client regen + docs), reviewed
  clean. Gates: `poe exclusion-policy-test` exit 0 (2098 passed), `poe
  api-contract-check` exit 0, `poe verify` exit 0 (2026-09-01 early session).
  **Live-scraped twice during the round** behind the Web Admin session
  (correct content-type/no-store, sanitized counters).
- **Task 2 — reconciliation-`leased` code-stands ruling**: DONE. Commit
  `7253b79`, review clean (all grounding claims independently re-verified in
  code).
- **Task 3 — policy-key CLI exception-class token**: DONE. Commit `d3b4e02`,
  review clean; `poe verify` exit 0 at that point.
- **Task 4 — CI consistency pins**: DONE. Commit `bbd21ef`, review clean
  (suite re-run 44/44 by reviewer).
- **Task 5 — live smoke round**: PARTIAL BY DESIGN RULE. Executed
  2026-09-01 as an operator-driven round on the user's REAL vault against
  `knowledge-ci-diagnostics-smoke-20260901` (user directive: no WDIO for the
  round; WDIO attempts documented below). Evidence record:
  `docs/operations/sync-error-tracing.md` §"Live smoke round of 2026-09-01".
  - policy-observability row: **RETIRED** — all four readbacks observed
    (failed counter + closed-code recent_failures, SYSTEM code in the
    rejection ring, typed 409 exchange in the rotating log with exact
    request_id join, convergence after restore with fresh `allowed`
    evaluations).
  - closed-reason-surfacing row: **STAYS** — wrong-origin token ✓, terminal
    `Last cleared reason: token_reuse` ✓ (post-revoke nuance recorded),
    staleness `worker_stale_running`+convergence ✓; the L1 locator-conflict
    readback could not be produced (blocked by the device-sync findings
    below). No partial completion claim, per the plan's own rule.
- **Task 6 — final verification**: `poe exclusion-policy-test` / `poe
  api-contract-check` exit 0 (Task 1 run). `poe verify` at round end is
  blocked at `typescript-test` by the **documented pre-existing jsdom 5s
  flake** (`PolicyEditor` operand-grammar test; BACKLOG row 2026-09-01
  web-infra) — reproducible only under machine load with `--coverage`; the
  web suite passes standalone (163/163, verified twice); this branch's diff
  touches NO web code (`git diff master --stat -- apps/web` empty). All
  changed areas green: plugin suite 1246/1246 + `tsc --noEmit` + eslint
  clean; python gates green earlier on the branch.

## Fixes landed during the round (out-of-plan, user-directed)

1. **`460064f` — journal composition wiring bug (FIXED, TDD).** The
   composition root never wired `JournalRepository.onDeviceSyncRepairComplete`
   → `JournalPersistence.markReconcileComplete()` (plugin.ts:842). The sticky
   in-memory reconcile flag then never cleared and every later generation
   commit re-clobbered the durable clear: a rebuilt journal over a non-empty
   vault looped reconcile forever (server-side manifest runs completing every
   ~15s, barrier generation climbing, queue lane stopped, uploads never
   dispatched). Failing composition test first (`plugin.test.ts`), one-line
   wire, full plugin suite 1246/1246, deployed to the real vault and
   **verified live**: flag cleared durably, dispatch resumed (11 allowed
   evaluations), device cursor 2→15→20.
2. **`c9dae80` — bootstrap accepts the diagnostics journey spec** (the
   authored WDIO spec `diagnostics-surface-live-smoke.e2e.ts` rides along,
   unrun: both WDIO attempts failed in the harness environment —
   settings-DOM timing and `target window already closed` while the user's
   real Obsidian ran; the user then directed the operator-driven round).

## Findings routed to the owning domain (BACKLOG rows added)

- device-sync: queue-pass stall (hanging await wedges `#isPassRunning`;
  reload recovers; needs bounded watchdog, TDD-first).
- device-sync: delete+recreate leaves repair Blocked (`device_cursor_gap`)
  that "Repair sync" does not clear — blocked the L1 setup.
- device-sync: login button unusable with the API-only origin (derives
  browser URL from `server_origin`).
- small-file-sync: large-vault cold sync unsupported by design (serial
  drain ~6–13s/event live-measured; 10k pending cap) — bulk-import path is
  the lever.

## Deferred items (verdicts)

- L1 lifecycle readback: deferred to the closed-reason row's new
  "Implement by" (after device-manifest recovery fixes) — verdict: blocked
  by out-of-plan defects, evidence recorded, no partial claim.
- W1 dispatch event live induction: code-stands-style verdict recorded in
  the runbook evidence section (dependency outage correctly yields the
  typed retryable release; the event class is unreachable live without
  simulating a fault, which the plan forbids; emission is test-pinned).
- `poe verify` typescript-test flake: pre-existing row (2026-09-01
  web-infra) owns it; not this plan's.

## Next actions

1. Device-sync recovery plan (TDD): the two BACKLOG device-sync defects —
   pass-stall watchdog first (has live repro evidence), then the
   cursor-gap-blocked repair.
2. Re-run the L1 readback on the round after those fixes; retire the
   closed-reason row then.
3. User's vault: reconnect against `knowledge-local` (restored `ready`);
   if the wedged journal shape reappears, a fresh journal (move
   `journal.sqlite.g*` + `journal.manifest.json` out) reconciles
   locator-proven against the real stack.

## Workspace

SDD workspace `.superpowers/sdd/2026-08-31-policy-diagnostics-metrics-sink-and-live-smoke/`
retained until the final whole-branch review; ledger inside.
