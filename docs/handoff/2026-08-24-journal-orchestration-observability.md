# Journal Orchestration Observability Handoff

**Date:** 2026-08-24  
**Scope:** closed journal-orchestration failure vocabulary and the sync-error
tracing operator surface.  
**Implementation range:** `570b815..169a319`. Tasks 1–4 landed in
`570b815..1a460c2`; final independent review repairs landed in
`0c6d221..169a319`.
**Final implementation commit:** `169a319` (`fix: admit supported sync
envelope diagnostics`). The subsequent handoff commit records verification
evidence only and does not change implementation.

## Gate status

- RED: the focused plugin contract test failed because
  `retry_schedule_read_failed` was absent from the runbook.
- GREEN: focused trail/export tests, plugin type-check, plugin lint and
  `git diff --check` passed after the runbook and vocabulary comment update.
- Final review: independently found and repaired two errors in the original
  closure: lifecycle reconcile persistence now rejects after recording its
  token, and persisted sync-envelope diagnostics accept only the closed,
  supported token vocabulary.
- Full gate: `uv run poe verify` passed end-to-end on `169a319`: 463 Python
  files format checked; lint, mypy (182 files), TypeScript checks, import/API
  contracts, Python coverage (`3451 passed, 21 skipped, 398 deselected`), all
  plugin tests (`707`), web tests (`138`) and all package/web/plugin builds
  passed. `git diff --check` also passed before the evidence-only handoff
  update.

## Decisions

- All nine journal-orchestration tokens remain closed
  `journal_failure` tokens. A new diagnostic kind would add vocabulary
  without adding operator value.
- The runbook specifies a safe meaning and emission bound for every token.
  The durable trail remains capped at 128 entries; diagnostic append failure
  is non-blocking and counted separately.
- The `sync-diagnostics-trail.ts` vocabulary comment now names all nine
  tokens, including the Task 1 review minor, alongside the contract-enforced
  runbook token list.

## Deferred-item ruling

No new deferred item was created. Existing `sync-error-tracing` backlog rows
remain unchanged: this documentation-only scope introduces no new contract
trigger and therefore requires no `BACKLOG.md` row.

## Next actions

1. Use `Copy sync diagnostics` and the updated runbook when a journal
   orchestration token appears; correlate only through existing closed
   surfaces.
2. Address existing diagnostics-trail BACKLOG items only at their recorded
   triggers.

## Living references

- `docs/operations/sync-error-tracing.md`
- `.superpowers/sdd/2026-08-24-journal-orchestration-observability/task-5-report.md`
- `docs/handoff/BACKLOG.md`
