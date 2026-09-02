# Source Lifecycle Fixture Repair Handoff

**Date:** 2026-09-02

## Status

Planning completed; implementation has not started at the user's direction.
The repository remained on `master` at `2cfad43` while these planning artifacts
were written. No stack, test, or live-device gate ran in this planning session.

## Planned gate

The future implementation must use a disposable `knowledge-ci-*` project via
`.local/serve-live-ci.sh`, run the four focused source-lifecycle integration
files, then tear the project down. Desktop and Mobile live acceptance are
explicitly out of scope.

## Decisions

- The plan owns only the two source-lifecycle fixture defects already recorded
  as reproducible on master.
- Locked signed-policy re-evaluation, not a caller-provided decision object,
  determines lifecycle projection intent operation.
- `projection_intents` fixtures must reference canonical `sync_events` parents.
- No BACKLOG entry has been removed: no implementation verification exists yet.

## Deferred items

No new deferred item was created. `docs/handoff/BACKLOG.md` remains the living
index for all existing Desktop/Mobile, device-sync, refactor, and conditional
items.

## Next actions

1. Create an isolated worktree if implementation is authorized.
2. Execute `docs/superpowers/plans/2026-09-02-source-lifecycle-fixture-repair.md`.
3. Remove only the two named BACKLOG rows after the plan's fresh green evidence.
