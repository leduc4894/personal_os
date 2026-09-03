# Lifecycle rejection ring live readback — handoff (BLOCKED)

**Date:** 2026-09-03
**Plan:** `docs/superpowers/plans/2026-09-03-lifecycle-rejection-ring-live-readback-plan.md`
**Branch:** `master` (docs-only commits; no code change)
**Status: BLOCKED by a new device-sync defect.** The L1 readback did NOT
run and NO partial completion is claimed — BACKLOG row
`2026-08-24 | closed-reason-surfacing` stays open, now blocked by the
defect row added below (the 2026-09-01 pattern repeating at a deeper
layer). The round's operator redirected the session to fixing the
defect first.

## Gate status (with evidence)

| Gate | Result | Evidence |
|---|---|---|
| CI stack up | PASS | `CI=true bash .local/serve-live-ci.sh up knowledge-ci-lifecycle-readback-20260903` — first attempt failed at API readiness with the documented `exclusion_policy_not_initialized` branch (fresh CI DB has no keyset); seeded `canonical_core_operations.py bootstrap-identity` + `personal-api policy-key initialize` (workspace keyset rev 1) per the 2026-09-02 chain, killed the half-up services WITHOUT tearing the project, second `up` converged: api/web-admin/2 workers/tunnel all ready |
| Bootstrap journey-ready | PASS | `obsidian_live_bootstrap_ready` (TOTP active, policy published; `--bootstrap-only`) |
| Plugin build for vaults | PASS | `pnpm --dir apps/obsidian-plugin run build` — v0.2.0 dist (contains the 2026-09-02 cursor-gap fix) |
| Two-vault fixture | PASS | vault B prepared at `.local/vault-fixtures/l1-second-writer-20260903/` (built plugin pre-installed); both vaults logged in as devices — server device list: 2× `obsidian_desktop` windows, plugin 0.2.0, active (registered 06:23:10Z / 06:24:32Z) |
| Baseline ring readback | PASS | `GET /api/admin/source-lifecycle/rejections` behind the Web Admin session: HTTP 200, `commit_counters: []`, `recent_rejections: []` — the L1 readback path itself is proven working against the live stack |
| Journey B (L1 trigger) | **NOT RUN** | blocked during B1 preparation by the defect below |
| BACKLOG retirement | NOT DONE (by design) | row stays open; no partial claim |

## The finding: untitled-transit reconcile hard-stop, repair cannot restart

**Live sequence (vault A, fresh journal, plugin 0.2.0, 2026-09-03):**
the operator's ordinary note creation — create default `Untitled.md`
(empty), drag into a folder (move), rename to the final name — took the
journal to a hard stop, and the repair machinery could not restart it.

**Evidence trail (sanitized, from two `Copy sync diagnostics` exports):**

- 06:27:06.708Z `reconcile_failure · actions · device_manifest_identity_ambiguous`
- 06:27:06.717Z `reconcile_failure · actions · device_cursor_gap`
- First `Repair sync`: `Repair: Blocked (device_cursor_gap)`, Applied 2,
  Acknowledged 2, Cursor lag 0, Pending actions 2, Status
  `Reconcile required (1)`
- 06:45:21.096Z `reconcile_failure · start · device_manifest_state_invalid`
  (the operator's second `Repair sync`)
- Second export: `Repair: Blocked (device_manifest_state_invalid)`,
  same counts, still `Reconcile required (1)`

**Server-side manifest runs (read from the CI project DB, ids redacted;
unredacted machine-local copy at
`.local/live-round-evidence/lifecycle-readback-20260903/`):**

| run | created (UTC) | base_ack | checkpoint | state | actions |
|---|---|---|---|---|---|
| 1 | 06:27:05.398 | 2 | 2 | expired | `conflict·device_manifest_identity_ambiguous`, `download`, `download` |
| 2 | 06:27:29.367 | **1** | **2** | **applying** (poll touched 06:50:05) | `no_change`, `download` (pending) |
| 3 | 06:45:21.002 | 2 | 2 | collecting (abandoned; never planned) | — |

**Reading (diagnosis for the fix plan, not a verdict):**

1. The 2026-09-02 recovery DID fire automatically: run 1 was closed and
   run 2 re-minted 24s later (no operator action in between).
2. The re-minted run 2 still cannot fit the apply lattice:
   `base_acknowledged_sequence=1 < checkpoint_sequence=2` — the fresh
   checkpoint was taken against a server cursor of 1 while the local
   apply lattice already sits at 2. Its pending download action cannot
   apply (`applied+1 > checkpoint`), the two-attempt bound is exhausted,
   and the run rests `applying`, polled but never converging.
3. The manual `Repair sync` mints run 3 but the client rejects it at the
   start-stage binding check (`device_manifest_state_invalid`) — the
   journal still binds run 2 — leaving run 3 abandoned `collecting`
   (server-side idle-expiry will close runs 2/3 at 07:27Z/07:45Z).
4. Net: ordinary vault actions (empty note → move → rename) hard-stop
   sync on the real stack and neither the automatic recovery nor the
   manual repair can restart it. This is the first LIVE observation of
   the "frozen open run" premise (the 2026-09-02 handoff's deferred
   live-parity item) — the premise is confirmed live, and the shipped
   recovery does not cover this shape.

**Fix inputs preserved:** the stuck journal is INTACT in vault A
(operator instructed not to reset); the unredacted server rows above;
API log timestamps (`manifest_start`×13, last 06:45:21Z); both trail
exports. The journal fixture copy is the fix plan's first task.

## Interpretive decisions

1. **The L1 trigger design stands** (plan §Trigger design): the
   two-vault reservation/second-writer blind spot remains the
   deterministic locator-conflict trigger for the re-fire; the defect
   blocked it before any journey step ran, so nothing about the design
   was invalidated.
2. **L1 plugin-side readback reading** (carried for the re-fire):
   today's lifecycle lane parks a 409 on the event row
   (`blocked_conflict`) and writes NO trail entry; the Conflict Inbox
   byteless `locator_collision` capture is the user-visible parked
   outcome. The original "plugin trail's parked outcome" wording
   predates Child 8; record the export verbatim either way.
3. **Operator declined, then ran, the second-repair verification** —
   both exports are recorded above; the recovery does not converge, so
   the "fix already landed" reading is closed as a verdict: the shipped
   fix does not cover this live shape.
4. **Stack intentionally left UP** after the block (recorded in
   `.local/runtime-logs/live-ci-state`): runs 2/3 and the stuck device
   state are live fix evidence and expire server-side at 07:27Z/07:45Z.
   Teardown stays deferred to the fix round: `CI=true bash
   .local/serve-live-ci.sh down` once the fix round no longer needs the
   live state. `knowledge-local` remains down.

## Deferred items (verdicts)

- **The untitled-transit hard-stop defect** — out of scope for this
  plan (device-sync domain owns it): BACKLOG row added
  (`2026-09-03 | device-sync`, `Before Child 9 operations acceptance`),
  pointing here. The immediately following fix plan owns its delivery.
- **L1 readback** — row `2026-08-24 | closed-reason-surfacing` updated:
  blocked by the defect row; re-fires after the fix lands (the stack
  bootstrap recipe, the two-vault fixture and the readback path are all
  proven this round, so the re-fire is short).
- **Journey A (delete-and-recreate repair convergence)** — its
  acceptance evidence is now ENTANGLED with the defect (this round
  observed the opposite: non-convergence); the fix plan's gates own it.

## Next actions

1. Copy the stuck vault A journal as the reproduction fixture (operator
   closes Obsidian first — never move journal files under a live app).
2. Fix plan + spec for the device-sync defect (failing harness journey
   reproducing: untitled-transit entry ambiguity + cursor misfit +
   re-mint misfit `base_ack=1 < checkpoint=2` + start-binding refusal),
   then the fix under `poe device-sync-test` gates.
3. After the fix lands: re-fire this round's Journey B exactly as
   planned, retire the closed-reason row on observed readbacks.
4. Teardown per §Decisions 4.

## Addendum: the 2026-09-03 re-fire (second block, three NEW findings)

After the fix round (`29f65f5` + `f7a92e5`) landed and all gates passed,
the L1 re-fire against `knowledge-ci-l1-refire-20260903` surfaced THREE
new device-sync edges — the first two fixes held exactly where they
aimed (no journal hard-stop on the untitled burst; the stale-binding
shed drove vault B's repair past finalize), but ordinary interleaved
operator usage keeps tripping adjacent edges:

1. **Untitled-transit burst still loses the rename chain (data-level).**
   The operator's ordinary create-Untitled → drag into folder → rename
   left the journal HEALTHY (Ready — the capture fix works) but the
   canonical source stayed at the OLD path (Untitled.md, re-downloaded
   into BOTH vaults by the manifest-restore semantics) while the
   renamed path (journey-b/origin.md) parked `blocked_conflict`
   untracked. Evidence: the 09:48Z export (Ready, 47 clean entries) +
   the operator's observation.
2. **Restart asymmetry — the resumed run cannot finish.** Vault B
   (fresh journal) reconciling while its vault churned hit
   `reconcile_failure · page · device_manifest_page_replay_mismatch`
   then `finalize · device_manifest_digest_mismatch`: the restart
   discarded the CLIENT's page progress but the same-generation start
   RESUMED the server run whose retained pages contradict the fresh
   capture — unfinishable. Evidence: 09:54Z export + server rows (run
   `…c06b4f` 'applying', base_ack 1, checkpoint 5).
3. **An apply-lane vault failure wedges the repair.** After the repair
   re-minted (checkpoint 12 — the shed worked) the run rests 'applying'
   with one pending download whose apply fails REPEATEDLY:
   `apply_failure · verify_temp · device_apply_vault_failed` (×2) +
   `recovery · device_cursor_gap`, cursor lag 11, no bounded terminal
   verdict. Evidence: 10:03Z export + server rows (run `…92ecf09`).

The re-fire is BLOCKED a third time (no L1 readback observed; row
stays open). The operator directed: fix the findings before further
testing. Raw server evidence:
`.local/live-round-evidence/l1-refire-20260903/`; the CI stack stays UP
until the fix round no longer needs the live wedge (it idles out
server-side regardless).
