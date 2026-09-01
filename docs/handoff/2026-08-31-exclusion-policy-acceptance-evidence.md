# Exclusion-policy acceptance evidence handoff

Date: 2026-08-31. Branch: `exclusion-policy-acceptance-evidence`.

## Status

The required first real-runner observation is green: PR #2 run
[`33404975850`](https://github.com/leduc4894/personal_os/actions/runs/33404975850)
on commit `7d4974c` completed successfully in 26m16s. It passed the
migration, exclusion-policy feature, performance-budget, Web Admin vitest,
Obsidian-plugin vitest, browser-journey and sanitized-cleanup gates.

## Delivered commits

- `cae2f9f`, `9d334a0`: coverage batch and privacy-scope remediation.
- `fcc2870`: synchronized in-memory recorder.
- `2e4b693`: in-repo mutation runner and first round (403 mutants; 294
  killed including 3 timeouts; 109 survivor verdicts).
- `5ae1efa`: policy admin E2E fixture contract repair.
- `cca29de`: ratified local effect-based fetch pattern in spec 17.
- `36ef06f`, `5eb9a06`: lifecycle migration backfill and its authority test.
- `b4b63f9`, `4e8d768`, `37d5ca1`: fixture compatibility repairs for feature
  and performance gates.
- `f244a68`, `a89e574`: safe, allowlisted local-stack startup diagnostics.
- `e78253a`, `7d4974c`: multipart test-flake remediation and evidence.
- `2f7124e`, `9c89bbe`, `df9d1c8`, `f4e43a9`: final whole-branch review
  fixes — canonical stack docs aligned to the ratified fetch pattern, the
  E2E status fixture pinned to the generated contract, mutation-tool and
  test minors, and the final gate record.

## Gates

- Task 1 focused suite: 442 passed, one existing TestClient deprecation.
- Task 2 diagnostics: 9 passed; broader gate independently blocked at the
  same schema baseline.
- Task 3 tool tests: 3 passed; ruff and mypy strict passed.
- Task 4 browser journey: 1 passed.
- Real runner: run `33404975850` success in 26m16s, including all workflow
  gates and sanitized cleanup.
- Task 7 local gates: `uv run poe build`, `uv run poe api-contract-check`,
  and `pnpm run test` passed (the latter: API client 1/1, Web 163/163,
  Obsidian 1,245/1,245). `uv run poe verify` passed format/lint/type/boundary
  preflight; its test subprocess completed but the Windows launcher did not
  retain its tail, so the independently observed build and full `poe test`
  process are recorded as supplementary evidence rather than an unqualified
  verify-pass claim.
- Final review HEAD: `uv run --all-packages --frozen poe exclusion-policy-test`
  (CI=true, disposable `knowledge-ci-final-review-20260831`) passed in 18:28 —
  2087 passed, 2 skipped, 1 deselected, exit 0 at `df9d1c8`. The first pass
  exited 1 only because the backup/restore suite errors closed when the pinned
  pg_dump/pg_restore 18.4 client tools (installed at
  `C:\Program Files\PostgreSQL\18\bin`) are off PATH; with that directory
  prepended the gate is green, and local reruns must prepend it.
- The mutation report is machine-local at
  `.local/test-results/exclusion-policy-mutation.md`; regenerate with
  `uv run python tools/exclusion_policy_mutation_report.py --source
  src/personal_os/exclusion_policy --tests tests/unit/exclusion_policy
  --output .local/test-results/exclusion-policy-mutation.md`. The adjudication
  table's line enumeration refers to it.

## Rulings

- Work directly on this branch without a worktree — explicitly requested by
  the user. Cost if wrong: concurrent edits in this checkout can collide.
- Include `BACKLOG.md` in Task 1 despite its abbreviated add command, so the
  closed row retires atomically. Cost if wrong: one extra documentation file
  in that commit.
- Keep the 109 mutation survivors as explicit code-stands/equivalent verdicts
  rather than creating in-scope deferrals. Cost if wrong: future tests may
  need to harden an identified boundary.
- Residual TanStack mentions were first ruled outside the Task 5 amendment;
  the final whole-branch review reopened that ruling, and `2f7124e` aligned
  the numbered canonical docs (`02`, `13`, `20`) to the ratified
  effect-based fetch pattern. Remaining mentions live only in historical
  process records (`docs/superpowers/`, `docs/handoff/`). Cost if wrong: a
  later architecture decision may need a coordinated update.
- The paths-filter decision is DECLINED — the workflow also protects master
  pushes and gates the acceptance suite as a whole; a paths filter would let
  master-relevant changes skip it. Cost if wrong: the 90-minute job remains
  broad, but acceptance coverage cannot be silently skipped.
- Repair lifecycle migration evidence from immutable `sync_events` rather
  than mutable `sources.current_version_id`; unprovable historical rows remain
  null and fail the contract closed. Cost if wrong: a historic fixture outside
  the represented event evidence will not upgrade, by design.
- Extend `stack_startup_failed` with only bounded status/health/initializer
  data after a failed startup. Cost if wrong: the diagnostic shape may need
  adjustment, but raw Compose output and secrets remain withheld.
- Replace the two multipart transport-order assertions with concurrency,
  membership/count, durable-progress and outcome invariants. Cost if wrong:
  a future ordering guarantee would need an explicit production contract and
  a new assertion.

## Next actions

1. Final whole-branch review completed 2026-08-31: 3 Important + 4 Minor
   findings fixed in `93c1f83..f4e43a9`; scoped re-review verdict — all
   findings addressed, no new Critical/Important breakage.
2. Push the final evidence commits, then finish the development branch
   workflow.
