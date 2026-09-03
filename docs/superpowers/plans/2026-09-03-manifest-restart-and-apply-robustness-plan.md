# Manifest restart and apply robustness — plan

**Date:** 2026-09-03
**Domain:** device-sync
**Findings source:** the 2026-09-03 L1 re-fire's second block —
[`docs/handoff/2026-09-03-lifecycle-rejection-ring-live-readback.md`](../../handoff/2026-09-03-lifecycle-rejection-ring-live-readback.md)
addendum; three BACKLOG rows (`2026-09-03 | device-sync`, untitled
burst / restart asymmetry / apply wedge). Raw evidence:
`.local/live-round-evidence/l1-refire-20260903/` (machine-local).

## The three findings (all reproduced live, none in the harness today)

1. **Untitled-transit burst loses the rename chain** (data-level): the
   journal stays healthy (the `29f65f5` deferral works) but a
   create-Untitled → move-across-folders → rename burst leaves the
   canonical source at the OLD path (re-downloaded everywhere by the
   manifest-restore semantics) while the renamed path parks
   `blocked_conflict` untracked.
2. **Restart asymmetry:** after `device_manifest_page_replay_mismatch`
   the client discards its page progress, but the SAME-generation
   restart RESUMES the server run whose retained pages contradict the
   fresh capture — the run can never finalize
   (`device_manifest_digest_mismatch`), Repair blocks.
3. **Apply-lane vault failure wedge:** a manifest download action whose
   apply fails repeatedly (`apply_failure · verify_temp ·
   device_apply_vault_failed`) has no bounded terminal verdict — the
   run rests `applying` forever, cursor lag grows, `recovery ·
   device_cursor_gap` loops.

## Task 1 — harness fidelity + RED journeys

**Files:** `apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts`
(ScriptedServer), one RED journey per finding.

- [ ] ScriptedServer: model the server's RETAINED PAGES across a
  same-generation restart (today `#servePage`/finalize likely accept
  whatever the client sends — verify), and add a hook to fail a target
  vault write repeatedly (`failVaultWriteAtLocator`) plus one to make
  the page digest diverge mid-run (a vault edit during the run — the
  existing `onFirstActionsRead` seam may serve).
- [ ] RED journey A (restart asymmetry): a run's page capture diverges
  mid-run → replay mismatch → restart resumes the SAME server run →
  the fresh capture's finalize rejects (`digest_mismatch`) → the
  repair wedges (today's outcome) — desired: the contradicted server
  run is invalidated and a truly fresh run converges.
- [ ] RED journey B (apply wedge): one planned download whose vault
  write fails repeatedly (the hook) → today: the action retries
  forever, run rests `applying`, cursor lag grows — desired: bounded
  attempts then a terminal action verdict with the closed reason; the
  run completes; the failed placement surfaces as a durable readable
  blocker.
- [ ] RED journey C (burst loss): the live untitled burst
  (create-empty → move → rename at the plugin-capture level, reusing
  the settle-deferral harness) → today: the rebind is lost, the old
  path restores, the new path parks — desired: the chained rename
  composes to ONE durable rename (old path → final path).

## Task 2 — GREEN: the fixes (design directions, TDD-verified)

- [ ] **Restart asymmetry:** every restart path that DISCARDS local
  page/action progress (page replay mismatch; the digest-mismatch
  finalize) must invalidate the contradicted SERVER run before
  restarting — the sanctioned mechanism is the observation-generation
  bump (`nextObservationGeneration` + re-barrier), which the server's
  different-generation start expires. Never resume a run whose
  retained pages the client just contradicted.
- [ ] **Apply wedge:** a manifest action's apply failures get a
  bounded attempt budget inside the run (e.g. 2-3 per action, riding
  the existing action progress rows); exhausting it terminalizes the
  action with the closed failure reason (no mutation), the run
  completes, and the unapplied placement records a durable readable
  blocker (the operator's human path mirrors the conflict-apply one).
  Retryable transport failures keep today's retry semantics — only the
  CLOSED vault-failure class (`device_apply_vault_failed` family)
  terminalizes.
- [ ] **Burst loss (the research item):** design first — candidate:
  durable pending-rename intents so a superseded observation composes
  (prior-miss + a later observation whose target matches this file ⇒
  chain old→final), or the settle re-deriving the row by the
  observation's target path. Do NOT implement before the RED journey
  pins the exact live loss; this task may split into its own plan if
  the design needs product input (the manifest-restore semantics for
  locally-deleted files interact here).

## Task 3 — gates, rows, redeploy

- [ ] Plugin vitest / tsc / lint / build; `CI=true
  LOCAL_STACK_TEST_PROJECT=knowledge-ci-<token> uv run poe
  device-sync-test`; `uv run poe verify` — all exit 0.
- [ ] Retire the three BACKLOG rows this plan owns (or split out the
  burst-loss row with its own milestone if deferred to a dedicated
  plan); ONE handoff
  `docs/handoff/2026-09-03-manifest-restart-and-apply-robustness.md`.
- [ ] Rebuild + redeploy the plugin dist to both vaults; reset the
  wedged vault journals per the documented fresh-journal procedure;
  teardown the `knowledge-ci-l1-refire-20260903` stack when the fix
  round no longer needs the live wedge; re-fire the L1 round (its own
  plan).

## Risks

- The harness must first PROVE each wedge (the 2026-09-02 lesson): if
  a RED journey cannot reach today's wedged state, fix the harness
  fidelity first — never relax the journey to pass.
- The burst-loss fix touches capture semantics shared with the
  reservation protocol — its RED journey must also pin the reservation
  and delete-deferral behaviors staying intact.
