# Source Lifecycle Fixture Repair Tasks

## Deliverables

- Seed locked policy outcomes that match lifecycle projection assertions.
- Seed valid `sync_events` parents before every fixture `projection_intents` row.
- Remove only the two verified BACKLOG rows and write one closure handoff.

## Completion conditions

- The focused local-stack integration command in the implementation plan exits
  zero on a disposable `knowledge-ci-*` project.
- No production module, migration, API contract, generated client, or live
  Desktop/Mobile artifact changes.
- `git diff --check`, Python format/lint, and the focused integration suite pass.

## Out of scope

All device/Desktop/Mobile work and every conditional, refactor-only, or
future-pin-bump backlog row remain indexed without edits.
