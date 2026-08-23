# Automatic vault convergence handoff

## Current state

- Branch: `codex/automatic-vault-convergence`.
- Last committed SHA: `cfbfced` (`feat: surface closed journal failure diagnostics`).
- Plan and design remain untracked working artifacts at `docs/superpowers/plans/2026-08-22-automatic-vault-convergence.md` and `docs/superpowers/specs/2026-08-22-automatic-vault-convergence-design.md`.
- Tasks 1–5 are implemented and reviewed. Task 6's queue-boundary fix is implemented and re-reviewed; the remaining Task 6 gate is live Desktop evidence. Task 7 has not started.
- User ruling (2026-08-22): the plan's Desktop WDIO gate for Task 6 is replaced by a user-assisted live check in the already-open Obsidian Vault plus DevTools; no WDIO run.

## Confirmed production-like observation

The open Obsidian vault had 44 notes. The server had exactly 34 committed operations and the plugin rendered `Ready (10)`.

Runtime state established that the connection and policy were ready; the queue driver and both coalescing coordinators were idle and not stopped. The previous queue pass reported `completed` after reaching its 60-second bound, leaving 10 eligible events with no future trigger. This is a scheduler defect, not a backend delay or a UI-only defect.

The two red DevTools syntax messages visible during investigation came from the diagnostic console acknowledgement/input, not from the plugin.

## Fix commit and verification evidence

`afdcf01` adds a real-driver regression test and changes the queue pass to surface `deadline_reached`; the automatic dispatcher then starts a serial follow-up pass. It does not add an artificial wait or extend the 60-second pass.

Reported GREEN evidence before review:

- targeted deadline behavior: 1 passing;
- queue-driver, automatic-snapshot, and plugin tests: 66 passing;
- full plugin Vitest suite: 36 files / 537 tests passing;
- `pnpm exec tsc --noEmit` and `pnpm run build` passing.

No local stack, database, authentication, Vault data, or WDIO run was changed during this debugging/fix round.

## Boundary-failure review finding — fixed and re-reviewed

Independent review had returned **Spec failed / Request changes** for `afdcf01`: a network/retry/login failure coinciding with the 60-second deadline was relabelled `deadline_reached`, so the dispatcher started a follow-up pass that could send a later queued note while the failed note was intentionally in retry backoff.

`52161d6` resolves all four required corrections:

1. The pass now ends with the closed outcome `retry_scheduled` for retryable failures (login stays `login_required`); the event-level `QUEUE_OUTCOMES` vocabulary is untouched.
2. `deadline_reached` is returned only for a natural deadline exit where `readOldestEligibleEvent(now)` still returns an event; the re-probe fails closed and cannot throw out of `runPass()`.
3. Failure, retry, login-required, and journal-failure exits never schedule an automatic follow-up; the dispatcher still follows up only on `deadline_reached`.
4. Real-driver RED/GREEN regression added: event 1 fails at the deadline, event 2 stays `queued` and is never sent; the genuine deadline-stranding continuation test is unchanged and green. Two one-line assertions in `journal-sync-journey.test.ts` were updated because they pinned the old conflated `completed` label.

Scoped re-review verdict: all findings addressed, no new Critical/Important breakage; reviewer independently re-ran 69 targeted plus 9 journey tests green. Offline gates: full Vitest suite 36 files / 540 tests, `tsc --noEmit`, `pnpm run build` all passing.

## Live follow-up defects (rename/edit stranding) — fixed and re-reviewed

Live verification after `52161d6` showed the 10 stranded notes converged (deadline continuation works), but 2 renamed notes and 1 edited note stayed pending (`Ready (1)` → `Ready (3)` after restart). An offline reproduction with real components (8 scenarios) confirmed seven defects D1–D7 and their file:line traces in `.superpowers/sdd/2026-08-22-automatic-vault-convergence/repro-rename-edit-report.md`:

- D1 rename/delete listeners never requested a queue pass.
- D2 the startup snapshot counted only new admissions, so lifecycle-only pending work never triggered a pass after restart.
- D3 the content lane could select lifecycle events (no operation filter) and terminally destroy renames via the zeros fingerprint.
- D4 no trigger ever followed a retryable/login pass end.
- D5 lifecycle pre-HTTP `login_required` terminally closed renames as `blocked_conflict` with zero server contact (restart credential race).
- D6 `waiting_retry(login_required)` rendered `Ready (n)`.
- D7 the rename freeze permanently deferred same-note pending edits; no path ever un-deferred.

Fix commits `e320e9c..44b1db6` (one per defect, TDD per cluster, rulings in `task-6-fix-round-2-brief.md`): review Approved all seven rulings with the reviewer re-running the suites. Fix round 3 (`eee8815`, `b6e0aa1`) closed the residual D4 arming gap (arm the one-shot retry trigger after every pass end that actually ran), pinned the terminal-rename fail-closed marker, aligned listener rejection handlers, and excluded `stopped` passes from arming after re-review found a reconcile-required busy loop.

Fix round 4 (`855fe37`, report `repro-lifecycle-credential-stall.md`): live follow-up showed NOTHING synced after the round-2/3 build. Confirmed cause: a stale (401) or null credential parks a pending lifecycle event `login_required` and ended every pass in the lifecycle lane BEFORE the content lane's one-per-pass refresh seam could run — starving the refresh entirely (infinite stall of all sync with one pending lifecycle event). The lifecycle drain now consumes the shared refresh budget, calls `refreshAccessToken()`, and retries `runOne` once before parking; the second `login_required` verdict or a refresh failure still parks and ends `login_required` (spec-8 second-401 discipline). Re-review: ADDRESSED, no new breakage.

Fix round 5 (`507cb91`, `cfbfced`; forensics in `repro-real-journal-file.md`, `repro-park-not-landing.md`): direct read of the user's live journal (sanitized) showed seven pending events starving behind one stuck event whose preflight gets HTTP 403 through the **Cloudflare tunnel** origin; the old `mapWireFailure` mapped every 403 to `login_required` regardless of body, so edge block pages (non-API HTML) killed passes. Now a 403 with a non-API body maps to the retryable `server_error` (queue survives with backoff); only a genuine API-envelope 403 stays `login_required`. Separately, the live `markEventWaitingRetry` commit has never landed across 21 attempts while sibling mutations publish — code and data proven innocent offline with the user's real journal file; the environmental failure reason was swallowed by design, so closed-token diagnostics were added: a bounded journal-failure reason ring on the driver, a generation-publish failure counter/ring on the persistence, both surfaced as a closed-vocabulary "Journal store diagnostics" settings line. Re-review: PASS, no new breakage; full suite 37→40 files / 593 tests green; `dist/main.js` rebuilt at `cfbfced`. One live diagnostic round remains to capture the park-failure reason.

Accepted tradeoffs (controller rulings): D7 deletes `deferred_lifecycle` rows and their attempt audits in the same transaction as the committed rename receipt to release re-admission (frozen events never reached the server; precedent `removeLocalMapping`); D4's one-shot scheduled trigger is plugin-level wiring, not a driver daemon loop — passes stay bounded and trigger-driven.

Fix artifacts:

- `.superpowers/sdd/2026-08-22-automatic-vault-convergence/task-6-fix-round-2-brief.md`
- `.superpowers/sdd/2026-08-22-automatic-vault-convergence/repro-rename-edit-report.md`
- `.superpowers/sdd/2026-08-22-automatic-vault-convergence/task-6-report.md` (appended fix reports)
- `.superpowers/sdd/2026-08-22-automatic-vault-convergence/review-52161d6..44b1db6.diff`, `review-eee8815..b6e0aa1.diff`

## Workspace safety

Do not reset, delete, down, recreate, or change `knowledge-local`, `knowledge-ci-*`, the current database, authentication, Cloudflare tunnel, or the user's Vault. Do not run WDIO until the review finding above is fixed and independently re-reviewed.

These pre-existing Task 6 WIP files are uncommitted and must be preserved:

- `apps/obsidian-plugin/src/authentication/live-device-onboarding-reuse.test.ts`
- `apps/obsidian-plugin/test/specs/device-login-sync.e2e.ts`
- `apps/obsidian-plugin/test/support/live-acceptance-phase-status.ts`
- `apps/obsidian-plugin/test/support/live-device-onboarding.ts`
- `tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py`
- `tools/obsidian_live_acceptance_bootstrap.py`

## Next actions

1. Live Desktop verification, user-assisted (no WDIO): reload the rebuilt plugin in the already-open Vault (`apps/obsidian-plugin/dist/main.js`, rebuilt with the fix), then confirm with sanitized counts only — 44 local notes, 44 committed server-side, zero pending local queue events, and the status bar converging from `Ready (10)` to `Ready (0)`. The no-overtake discipline for retry-delayed events is covered by the offline real-driver regression in `queue-driver.test.ts`.
2. On passing live evidence: finalize Task 6 (decide the fate of the six pre-existing WIP journey/bootstrap files under the no-WDIO ruling), update the SDD ledger, then start Task 7 according to the plan.


---

# Session close-out addendum (2026-08-23 ~23:50 local)

Branch head at closure: `f2e1f6e`. Task 6's live evidence is now SUBSTANTIALLY complete — read this together with the monitoring plan's handoff (`2026-08-23-sync-error-tracing-observability.md` close-out addendum), whose observability stack diagnosed and closed the stall this plan's Task 6 was blocked on.

## Live evidence status (replaces the "Next actions" above)

- The two-day-stuck event committed (2026-08-23 15:59 UTC, request-id joined in API logs); its duplicate resolved `no_change`. Live journal: 52 committed, 9 no_change, 1 integrity_failed (superseded edit — correct), 2 events parked `waiting_retry` retrying with a working exponential backoff (verified to attempt 16).
- Four stacked root causes were fixed and live-verified: fractional retry backoff (`792cbe8`/`580e20d`), update-preflight policy evidence (`c065ddc`), update receive binding (`c7894b4`), plus the `f2e1f6e` style normalization (PEP 758 — see corrections).
- The no-overtake discipline, deadline continuation, park/backoff/armer, lifecycle triggers, and 403 discrimination all behaved correctly under real failure load — effectively the Task 6 automatic-convergence journey, observed organically on the user's live vault (user-assisted, per the no-WDIO ruling).

## Remaining before Task 6 can be marked complete

1. One open server item (single next code action): typed `SOURCE_LOCATOR_CONFLICT` for create-publication locator conflicts — currently the last two parked events retry-fail via a misclassified retryable `source_commit_outcome_unknown`. Brief + repro in the monitoring plan's artifacts. Once it ships and the stack returns, those events converge or park `blocked_conflict` honestly.
2. Final sanitized confirmation from the user's vault (one "Copy sync diagnostics" paste showing Ready (0) or only honest terminal states).
3. Then Task 7 (operational documentation) and the plan's own closure.

## Corrections to this handoff's earlier narrative

- The "corruption / compromised toolchain" concerns recorded mid-session were WRONG: PEP 758 under the pinned Python 3.14 makes `except A, B:` valid (AST-identical); the pinned ruff writes that form by design. Nothing was compromised; the WIP bootstrap tool needs no repair.
- The plugin README's stale command list (noted in the monitoring plan's review) remains a deferred item.

## Environment

Local stack was shut down at session end to relieve RAM; bring it back per `.local/RESTART.md`. Hyper-V port reservations are now persistent (`.local/reserve-stack-ports.ps1`).
