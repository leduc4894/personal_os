# Explicit restore target reservation handoff

**Date:** 2026-08-25
**Spec:** `docs/superpowers/specs/2026-08-25-explicit-restore-target-reservation-design.md`
**Plan:** `docs/superpowers/plans/2026-08-25-explicit-restore-target-reservation-plan.md`
**Demand:** the two mandatory pre-Child-6 items — the 2026-08-25
`small-file-sync` convergence/lifecycle lane race row (`Before next plugin
release`) and the `source-lifecycle-mobile-acceptance` row (`Before Child 6
acceptance closure`).

**Status: the race is FIXED and live-verified.** Implementation commits
(base `e153458`):

- `bad2747` `feat: reserve explicit-restore targets in the journal` (schema v6
  `restore_prior_path` + v5→v6 migration; `reserveRestoreTarget` /
  `releaseRestoreTarget`; restore records as `restore_pending`; the committed
  receipt rebinds `normalized_path` to the target locator and clears the
  prior path; `readTombstonedLocalFileIds` → `readRestorableLocalFileIds`)
- `d1d5b42` `fix: defer convergence and guard lifecycle capture on reserved
  targets` (content admission defers `restore_pending` rows on settle +
  snapshot; strict confirm-on-reserved `requestRestore`;
  `detectAutomaticRestore` refuses reserved rows and no longer consumes the
  tombstone eagerly; delete/rename on a reserved row are quiet no-ops)
- `<docs commit>` `feat: wire the reservation-first restore command with
  closed refusals` (reserve at prompt-accept; stage between prompt and
  confirm; record + one bounded pass on confirm; Cancel releases, dismissal
  keeps; closed refusals `restore_target_occupied` / `restore_target_busy` /
  `restore_already_pending` as Notice + trail token;
  `restore_reservation_persist_failed` joins the composition token list;
  WDIO journey restaged; runbooks + spec/plan)

## Root cause (four plugin-side defects; the server is per-spec correct)

1. **Staging-window convergence** — restore requires staged bytes at the
   target BEFORE the command records the event; an untracked target lets
   convergence (startup snapshot / settle admission) ship the staged bytes
   as a fresh source before the restore event, so the server correctly
   rejects with `source_locator_conflict` (the upstream contract demands an
   "available target locator").
2. **No durable target reservation** — nothing marked the target
   spoken-for; per-`local_file_id` lane ordering cannot see cross-file
   locator contention.
3. **Record-time state lies / double restore** — recording a restore set
   `restored` immediately and `detectAutomaticRestore` consumed the
   tombstone at record time; the admission's auto-restore branch could
   record a second restore into `tombstone_closed`. `restore_pending` was
   in the closed enum but never written.
4. **No post-commit rebind** — a committed restore never rebound
   `local_files.normalized_path` to the target, so post-commit convergence
   of the staged bytes collided with the canonical locator and hard-stopped
   the journal.

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| Focused RED→GREEN per task | PASS | schema/migration 12; lifecycle-repository 27; lifecycle-capture 33; capture 33 (each seen RED first, then GREEN) |
| Full plugin suite | PASS | 42 files, 739 tests passed |
| Plugin type check / lint / build | PASS | `tsc --noEmit` 0; `eslint --max-warnings=0` clean; `build` OK |
| Repository offline verify | PASS | `uv run poe verify` exit 0 |
| Stack prerequisite | PASS | `knowledge-local` stood down; disposable `knowledge-ci-restore-reservation` reached ready (CI env required for the up command) |
| API / Web / workers / tunnel | PASS | serve-local.sh, Web 38000, both workers via run-worker.sh; existing cloudflared tunnel served both origins; ports 8000/38000 released afterwards |
| Guarded Desktop WDIO | PASS | `obsidian_live_acceptance_passed` on the final guarded run (one earlier guarded attempt failed `obsidian_wdio_failed_after_onboarding` — see decisions) |
| Diagnostic journey evidence | PASS | `SANITIZED_SOURCE_LIFECYCLE_EVIDENCE` stable source+version identity, 4 lifecycle events, 4 locators, 0 pending, 0 blocked |
| Clean shutdown | PASS | services stopped, ports clear, CI project `stack_down_complete`, `knowledge-local` back to `stack_ready`, diagnostic wrapper + phase files removed |
| Physical mobile matrix | PENDING | requires the operator + iPhone (next action 1) |

## Decisions and interpretations

- **Reservation-first protocol over server-side supersede.** The server's
  409 closed-conflict family stays untouched (it is the correct guard for
  genuine cross-device contention, and the wire corpus entry was just
  pinned by the child-six remediation). The plugin instead claims the
  target durably at prompt-accept, before any bytes are staged.
- **`restore_pending` overload (no new enum value).** The closed 8-value
  enum is untouched; `restore_pending` now means "reserved or event
  recorded, not yet acknowledged" — the settings histogram already renders
  it and no code ever wrote it before.
- **Cancel-only release; dismissal keeps.** `ConfirmModal` gained a third
  callback separating explicit Cancel (releases, atomic return to the prior
  path) from passive dismissal (keeps the reservation resumable through the
  picker) — mobile app switches close modals without an explicit cancel.
- **Schema v6 via the established pattern.** Nullable
  `local_files.restore_prior_path`, v5→v6 in the migration chain
  (`migrateServerReceiptJournalToRestoreReservationSchema`); release and
  commit clear it; re-reservation preserves the original prior (never
  chained).
- **Phantom-row release at reservation.** A target held by a never-committed
  phantom row with only `queued`/`waiting_retry` creates is released inside
  the reservation transaction (the `removeLocalMapping` cleanup shape); a
  phantom with `preflight`/`uploading` refuses `restore_target_busy`.
- **The first guarded attempt's `obsidian_wdio_failed_after_onboarding` was
  NOT the restore fix.** DB forensics showed both fixture-vault creates
  committed; a directly-run identical journey (full serve env) passed
  end-to-end, and the final guarded bootstrap returned
  `obsidian_live_acceptance_passed`. The residual is a pre-existing harness
  timing window in the journey's initial fixture-unique journal read racing
  the second vault file's commit — outside this plan's scope, not observed
  in the official PASS, and noted below as a deferred hygiene item.
- **The guarded bootstrap does not stand up the stack.** It requires the
  disposable project ready (CI env on the up command) plus API, Web, both
  workers and the tunnel running — the same lesson as previous sessions,
  now costing one extra attempt.

## Deferred items and verdicts

- **Journey fixture-unique timing window** (the initial journal read races
  the second fixture file's commit; both guarded runs that failed after
  onboarding across sessions line up with it): DEFER — pre-existing harness
  behaviour, outside the race fix's scope; the official guarded PASS is the
  acceptance evidence. Index it only if it recurs on the next guarded run
  (then scope the wait to the fixture row or filter the uniqueness check).
- **Mobile physical matrix**: PENDING the operator round (below). The
  `source-lifecycle-mobile-acceptance` BACKLOG row stays until sanitized
  physical evidence exists; Mobile must never be reported PASS without it.
- No other rows were created or retired by this plan except the 2026-08-25
  race row (retired — evidence: the guarded Desktop PASS and the diagnostic
  journey evidence above).

## Next actions

1. **Re-run the physical mobile matrix with the operator** (iPhone) under
   the new staging procedure documented in
   `docs/operations/source-locator-tombstone-lifecycle.md`: pick tombstone →
   enter target path (the reservation lands) → stage the exact restored
   bytes at the reserved target → confirm. Record sanitized evidence in the
   living doc, flip the Mobile record to PASS, and retire the
   `source-lifecycle-mobile-acceptance` BACKLOG row. Use a fresh disposable
   `knowledge-ci-*` project with the rebuilt plugin dist and the standard
   launcher chain; stand it down and restore `knowledge-local` afterwards.
2. With both rows retired, Child 6 (`device-cursor-and-manifest-reconciliation`)
   may start; its acceptance closure no longer depends on the mobile row
   once action 1 lands.
