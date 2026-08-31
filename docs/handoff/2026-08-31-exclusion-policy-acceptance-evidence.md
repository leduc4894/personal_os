# Exclusion-policy acceptance evidence handoff

Date: 2026-08-31. Branch: `exclusion-policy-acceptance-evidence`.

## Status

Blocked at the required first real-runner observation. PR #2 triggered
`exclusion-policy-acceptance.yml` run `33380395564`; it failed in the
migration gate after 2m26s. Its disposable project validation, dependency
setup and exact cleanup all passed. The failure was
`database_schema_contract_invalid` while the migration fixture upgraded from
the Child 2 revision to head; the acceptance, performance, web, plugin and
browser gates were consequently skipped.

The matching local disposable-stack run produced the same closed reason.
This is not the Task 4 browser fixture defect: the repaired
`policy-publication.spec.ts` journey passed locally (1 passed).

## Delivered commits

- `cae2f9f`, `9d334a0`: coverage batch and privacy-scope remediation.
- `fcc2870`: synchronized in-memory recorder.
- `2e4b693`: in-repo mutation runner and first round (403 mutants; 294
  killed including 3 timeouts; 109 survivor verdicts).
- `5ae1efa`: policy admin E2E fixture contract repair.
- `cca29de`: ratified local effect-based fetch pattern in spec 17.

## Gates

- Task 1 focused suite: 442 passed, one existing TestClient deprecation.
- Task 2 diagnostics: 9 passed; broader gate independently blocked at the
  same schema baseline.
- Task 3 tool tests: 3 passed; ruff and mypy strict passed.
- Task 4 browser journey: 1 passed.
- Real runner: run `33380395564` failure at migration gate only; sanitized
  cleanup succeeded.

## Rulings

- Work directly on this branch without a worktree — explicitly requested by
  the user. Cost if wrong: concurrent edits in this checkout can collide.
- Include `BACKLOG.md` in Task 1 despite its abbreviated add command, so the
  closed row retires atomically. Cost if wrong: one extra documentation file
  in that commit.
- Keep the 109 mutation survivors as explicit code-stands/equivalent verdicts
  rather than creating in-scope deferrals. Cost if wrong: future tests may
  need to harden an identified boundary.
- Treat residual TanStack mentions in historical/global architecture docs as
  outside the Task 5 amendment; spec 17's unresolved mandate is removed.
  Cost if wrong: a later architecture decision may need a coordinated update.
- Do not remove the first-real-runner BACKLOG row: its green-run terminal
  condition is unmet. The schema failure is owned by
  `2026-08-31-canonical-correctness-and-migration-hygiene`, which must repair
  the canonical migration contract before this plan can resume. Cost if wrong:
  the acceptance workflow could remain blocked.

## Next actions

1. Complete the canonical correctness/migration-hygiene plan and obtain a
   green migration gate.
2. Re-run PR #2's final workflow shape; record a green real-runner outcome,
   duration and the declined paths-filter decision.
3. Remove the remaining CI-observation BACKLOG row only in that evidence
   commit, then run Task 7 final gates and final branch review.
