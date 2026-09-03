# Untitled-transit rename chain recovery — plan

**Date:** 2026-09-03
**Domain:** device-sync / small-file capture
**Finding source:** BACKLOG row `2026-09-03 | device-sync` (Untitled-transit
burst); the live symptom is pinned in the
[lifecycle-rejection-ring addendum](../../handoff/2026-09-03-lifecycle-rejection-ring-live-readback.md)
§"the burst, observation by observation"; the capture-lane territory map
(lifecycle-capture, capture, queue-driver, repository) was surveyed
2026-09-03 from the current tree. Raw evidence:
`.local/live-round-evidence/lifecycle-readback-20260903/` (machine-local).

## The loss, precisely (from the live round)

Create `Untitled.md` → drag into `journey-b/` → rename to `origin.md`:

1. The create's event E1 mints row R1 at `Untitled.md` (`source_id = null`)
   and uploads; the queue sees the local target vanish mid-flight (the
   file already moved) and closes E1 `local_file_missing` →
   `deferred_lifecycle` — **after** the server created the canonical
   source, so the canonical lives at the OLD path with no local mapping.
2. The move observation settles into the rename deferral (`29f65f5`):
   R1 has an in-flight event, so the settle re-arms with the FROZEN
   `(Untitled.md → journey-b/Untitled.md)` pair — it never re-resolves
   either endpoint and is invisible to other observations.
3. The rename observation (`journey-b/Untitled.md → journey-b/origin.md`)
   prior-misses — R1 is still bound at `Untitled.md`, nothing links the
   two observations — and quietly no-ops (`lifecycle-capture.ts` prior-miss
   branch).
4. The post-rename fresh admission mints R2 at `journey-b/origin.md`; its
   create upload hits the content/locator conflict and parks
   `blocked_conflict` untracked (`queue-driver.ts`).
5. The deferral's re-arm later finds R1 uncommitted-transit and silently
   heals it away (`removeLocalMapping`) — the journal stays Ready, the
   canonical stays at `Untitled.md`, the manifest re-downloads it into
   every vault, and the renamed path parks untracked.

Net: one user-visible file becomes a stale canonical copy plus a parked
untracked duplicate; no journal error ever surfaces.

## Goal

One burst composes to ONE durable intent: the local file at its FINAL path
is tracked as the SAME canonical source the create committed, through a
single equivalent rename (`Untitled.md → journey-b/origin.md`), with the
intermediate path never minting a second row. The journal stays healthy
(the `29f65f5` deferral keeps working) and the closed reasons stay
readable when composition is impossible.

## Non-goals

- The manifest-restore semantics for locally-DELETED files (the fence /
  restore question the burst-loss row interacts with) — owned elsewhere;
  this plan composes RENAMES only.
- Server-side changes; the wire contract is untouched.
- Re-litigating the `29f65f5` deferral itself (timing stays).

## Design direction (to be pinned by the RED journey before implementing)

**Primary candidate — durable pending-rename intents (chain composition):**

- When a rename settle defers (in-flight create) OR prior-misses while a
  tracked row still sits at an earlier path, record ONE durable
  pending-rename intent `(prior → new)` keyed by the row (a new
  `pending_rename_intents` slice of the journal schema — version bump +
  migration with upgrade/downgrade tests).
- A later observation whose PRIOR equals a pending intent's NEW composes:
  extend the intent to `(intent.prior → observation.new)`, keep exactly
  one intent per row.
- The deferral re-arm and the eventual commit resolve through the INTENT
  (current endpoints), not the frozen observation pair: when the row's
  identity lands, ONE rename commits prior → final; the fresh admission
  of an intermediate path must consult the intents and never mint R2.
- The uncommitted-transit heal and the `blocked_conflict` parking of a
  row that a pending intent owns must re-parent (heal the intent, not the
  chain) — the orphaned-canonical shape of the live loss dies here.

**Fallback candidate — settle-time endpoint re-derivation:** every re-arm
re-reads the vault and re-derives both endpoints (where does the row's
content actually live now?). Cheaper (no schema change) but cannot bridge
the prior-miss (no linkage exists to consult) — only viable combined with
a session-level map; likely insufficient. Decide in the RED round.

**Guarded behaviors the RED journey must pin as intact:** the reservation
(`restore_pending`) guards; the delete-deferral ladder; the pure-create
transit heal (no rename involved); the admission tail's serialization;
echo suppression on the composed rename.

## Files (expected)

- `apps/obsidian-plugin/src/journal/lifecycle-capture.ts` — prior-miss
  branch, deferral re-arm, heal guard.
- `apps/obsidian-plugin/src/journal/capture.ts` — post-rename admission
  consults intents (no R2 mint for an intent-owned path).
- `apps/obsidian-plugin/src/journal/lifecycle-repository.ts` +
  `repository.ts` + `schema.ts` (+ migration) — durable intent rows,
  re-parent on heal/park, commit-and-clear on the composed rename.
- `apps/obsidian-plugin/src/journal/queue-driver.ts` — only if the
  `local_file_missing → deferred_lifecycle` close needs an intent-aware
  exception (decide in the RED round).
- Tests: `device-sync-journey.test.ts` (the burst RED journey),
  lifecycle/capture focused tests, schema migration tests.

## Risks

- The durable intent is a new crash-surface: every path that deletes or
  parks a row (heal, defer, reconcile reset, sqlite rebuild) must clear or
  re-parent its intent — a leaked intent wedges the next rename at that
  path. Enumerate ALL row-lifetime exits in the design round.
- The harness must reproduce the live timing (E1 in-flight when the move
  settles; the rename arriving before E1 resolves; the server commit
  landing before the `local_file_missing` close). If the journey cannot
  reach the live loss shape, fix the harness first — never relax the
  journey.
- Composition changes WHICH locator a `blocked_conflict` names; the
  lifecycle rejection-ring readback expectations must be re-checked.

## Verification

- RED journey reproduces the live loss (stale canonical at the old path +
  parked untracked renamed path), then goes green on the composed chain.
- Focused tests per branch (compose, heal re-parent, admission consult,
  intent lifecycle across restart).
- Full gates: plugin `vitest run` / `tsc --noEmit` / `eslint` / `build`;
  `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-<token> uv run poe
  device-sync-test` (fresh project, run alone — see the robustness
  handoff's flaky note); `uv run poe verify`.
- BACKLOG row retires only after all gates are green; ONE handoff update.

## Tasks

1. **Harness burst fidelity + RED journey** — model the watcher-level
   burst (create → move → rename with the E1 in-flight overlap) on the
   production stack; pin today's loss; also pin the guarded behaviors.
2. **Design pin** — durable pending-rename intent schema + the row-lifetime
   exit enumeration (heal/park/defer/rebuild); decide the fallback.
3. **GREEN** — implement the smallest chain-composition that passes the
   RED journey; focused tests per branch.
4. **Gates, row, handoff** — full gate run, retire the row, update the
   robustness handoff (or its successor) with the interpreted decisions.
