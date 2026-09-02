# Initial TOTP Offer Retirement — Handoff

- **Plan:** `docs/superpowers/plans/2026-09-02-retire-initial-totp-offer.md`
- **Spec:** `docs/superpowers/specs/2026-09-02-retire-initial-totp-offer-spec.md`
- **Branch:** `retire-initial-totp-offer` (from `master` @ `7995e9c`)
- **Final code SHA:** `64dd65d`; docs closure commit `5695126` carries this
  handoff (a handoff cannot contain its own carrying commit's hash, so the
  closure SHA is recorded here by this follow-up note)
- **Status:** COMPLETE — all four plan tasks landed, every automated gate
  PASS at the final tip. The obsolete "initial TOTP offer/dismissal"
  contract is gone end to end: Web Skip UI and client method, the
  `dismiss_initial_offer` action (domain, HTTP, generated artifacts), and the
  `user_credentials.totp_prompt_dismissed_at` column. Optional enrollment now
  begins only from Security; recovery replacement stays mandatory. No manual
  or live gate was owed by this plan.

## Gate status (evidence)

| Gate | Result | Evidence |
|---|---|---|
| `uv run poe verify` (Task 4, at `64dd65d` + docs edits) | PASS (exit 0) | 14 sub-gates green: ruff format-check/lint, pnpm format:check/lint, mypy strict (223 files), pnpm type-check, import boundaries, architecture contract tests, `api_contract_artifacts.py check` (`api_contract_current`), api-client `generate:check`, pytest `4623 passed, 21 skipped, 550 deselected`, pnpm test (web 21 files / plugin 64 files / api-client 1 file), uv build + pnpm build. Log: `.superpowers/sdd/2026-09-02-retire-initial-totp-offer/verify-log.txt` |
| Retired-token `rg` consistency gate (Task 4 Step 2) | PASS per controller ruling | `rg -n "dismiss_initial_offer|dismissInitialTotpOffer|totp_prompt_dismissed_at|Skip for now" apps src packages migrations tests docs --glob '!docs/handoff/*'` — every match classified acceptable (Decisions #1); zero matches in canonical docs (`docs/` minus `handoff/`, `superpowers/`) and zero live-contract usage |
| `uv run python tools/api_contract_artifacts.py check` | PASS | prints `api_contract_current` |
| `git diff --check` / `git status --short` (Task 4 Step 3, pre-BACKLOG) | PASS / clean | exit 0; only the intended design-doc modification pending |
| Real-DDL migration upgrade AND downgrade (Task 3) | PASS | 19 passed in 271s on disposable `knowledge-ci-task3-totpretire`: gated downgrade walks back through `20260902_02` downgrade; credential/password transactions write `user_credentials` at head. See Task 3 report §tests |
| Migration unit tests (Task 3, TDD) | PASS | `tests/unit/migrations/test_drop_totp_prompt_dismissal_migration.py` 10 passed (RED first: 10 failed while the revision was absent) |
| Auth domain/API/store suites (Task 2, TDD) | PASS | 1582 passed; RED first: closed-vocabulary `{'start'}` mismatch and `422 != 200` |
| Web tests + type-check (Task 1, TDD) | PASS | web suite 21 files / 161 tests; RED: TS2741 missing `dismissInitialTotpOffer` in spy client |

## What landed

1. `d249f3c` — Task 1: Web Skip UI and dismissal caller removed (7 files,
   +2/−92): `AuthenticationClient.dismissInitialTotpOffer()`, `onSkipped`
   prop, `skip()` and the `Skip for now` button; Skip absence now asserted in
   component and e2e tests.
2. `a5e1411` — Task 2: domain/HTTP contract reduced to `start` only (12
   files, +23/−130): `TotpEnrollmentAction = {start}`, dismissal branch/port/
   store method/`dismissed_at` response field removed; OpenAPI + generated
   client regenerated (`TotpEnrollmentAction` renders as `"start"` only);
   intentional 422 rejection test for `{"action": "dismiss_initial_offer"}`.
3. `59f4ca6` — Task 3: DB state dropped (25 files, +479/−99): reversible
   migration `20260902_02` (drop column, rebuild reduced
   `ck_user_credentials__timestamps`; downgrade restores the exact
   `20260816_01` clause), metadata/reset-path cleanup, sanctioned 20-file
   head-pin ripple (`src/personal_os/database_schema.py` head bump to
   `20260902_02` + all revision-count/head pins).
4. `64dd65d` — Task 4 pre-closure fix: format-only join of the
   `MIGRATION_PATH` constant in the Task 3 migration test; `poe verify`'s
   `python-format-check` had caught the drift (Decision #5).
5. Docs closure commit (this handoff): canonical auth design doc updated to
   `start`-only (§10.1 states optional enrollment begins from Security while
   recovery replacement remains mandatory; §15.1 credential table without the
   dismissal timestamp; §16.2 route comment `# action = start`), retired
   BACKLOG row deleted, baseline-suite BACKLOG row added.

## Decisions and interpretations

1. **Retired-token `rg` gate adjudication (controller ruling applied).** The
   plan expected "no source or canonical-doc matches", but the searched paths
   legitimately retain the tokens. Every match of the brief's exact command
   falls in these acceptable classes (full list in the Task 4 report):
   (a) `migrations/versions/` — immutable history `20260816_01` (original
   column + clause) and `20260902_02` (the retirement revision's own
   definitions and mandated downgrade restore); (b)
   `tests/unit/migrations/test_drop_totp_prompt_dismissal_migration.py` —
   the retirement migration's own tests; (c)
   `tests/unit/api_runtime/test_totp_routes.py` — the intentional 422
   rejection test sending `{"action": "dismiss_initial_offer"}`; (d)
   documented test-exclusion pins — `RETIREMENT_DROPPED_COLUMNS` in
   `tests/contract/test_authentication_migration_contract.py` and the
   `_RETIRED_DISMISSAL_COLUMN_FACTS` revision-pin docstring block in
   `tests/integration/authentication/test_authentication_migration.py`.
   Three judgment calls inside that ruling, reported for transparency:
   - `src/personal_os/database_schema.py` mentions the column once in the
     `#:` chain note above `CANONICAL_POSTGRESQL_SCHEMA_REVISION` — that note
     documents every revision's action (provenance, same nature as class (a))
     and was part of the coordinator-approved Task 3 ripple; the schema
     metadata itself is clean. Rewording it is possible but was out of Task
     4's three-file scope.
   - Five `"Skip for now"` matches are negative assertions
     (`not.toBeInTheDocument()` / `toHaveCount(0)`) in colocated Web test
     files (`*.test.tsx` under `apps/web/src`) and
     `tests/end_to_end/authentication/web-security.spec.ts` — prescribed by
     this plan's own Task 1; they guard against Skip-button reintroduction.
     They are tests, not shipped source.
   - `docs/superpowers/` matches are workflow artifacts, not canonical docs:
     the retirement's own plan/spec (they must name what they remove) and the
     historical `2026-08-16-web-auth-and-device-authorization.md`
     implementation plan (point-in-time record that freezes its normative
     references to a commit). Canonical docs (`docs/` minus `handoff/` and
     `superpowers/`, incl. `docs/operations/`) have ZERO matches — verified.
     Ratified by the controller on 2026-09-02: this boundary (canonical docs =
     docs/ minus handoff/ and superpowers/) is the standing convention for
     future retirement-token consistency gates.
2. **Task 2 deviation (two files beyond the Step 5 list, both required by the
   regenerated types):** `apps/web/src/testing/api-mock-builders.ts` (stale
   `dismissed_at: null` broke web type-check with TS2353) and
   `tests/end_to_end/authentication/web-security.spec.ts` (stale mock field
   of the exact route whose contract changed). One-line removals each;
   leaving them would keep dangling references to the retired tokens.
3. **Task 2 deviation (generation command):** the plan's
   `tools/api_contract_artifacts.py generate` subcommand does not exist (the
   tool is check-only). Regeneration used the repository's canonical
   commands: `uv run poe api-contract-export` +
   `pnpm --filter @workspace/api-client run generate`. Task 4 used the
   existing `check` subcommand per the controller ruling.
4. **Task 3 sanctioned head-pin ripple (20 files beyond the 5-file list).**
   A new Alembic head mechanically breaks every head/revision-count pin, and
   without bumping `CANONICAL_POSTGRESQL_SCHEMA_REVISION` runtime readiness
   would reject the new head (`poe verify` would fail 29 tests). Approved by
   the coordinator as "Brief + head-pin ripple"; the same update every prior
   head migration made.
5. **Task 4 format-only followup (`64dd65d`).** Task 3's new migration test
   shipped with a formatting drift that the first `poe verify` run caught at
   `python-format-check`. Per the AGENTS.md rule that simple in-scope items
   must be fixed in-session, the one-line `ruff format` fix was committed
   separately before the docs closure, and the full `poe verify` was re-run
   green. Diff is formatting-only (joined a parenthesized assignment).
6. **Opt-in semantics now explicit in the design doc.** §10.1 keeps the
   existing statement that a password-only login continues straight into the
   app, and the rewritten action paragraph now states: `start` is the only
   enrollment action, optional enrollment begins from Security, recovery
   replacement remains mandatory, and no dismissal action or endpoint exists.
   Nothing in any touched text reintroduces post-password prompting or a Skip
   control.

## Deferred items (verdicts)

1. **Canonical PostgreSQL baseline suite (BACKLOG row added, category (a) —
   out of scope).** `tests/integration/test_canonical_postgresql_baseline.py`
   lifecycle tests fail because the suite pins a `20260818_01`-era
   `_EXPECTED_TABLES`/`_HEAD_REVISION` set while running `alembic upgrade
   head`; verified identical at the branch base `a5e1411` (pre-existing, not
   worsened here). `local_stack`-marked, so local `poe verify` and the
   default pytest run deselect it; only the dedicated CI workflow selects it.
   Real-DDL coverage for `20260902_02` was proven by the authentication
   integration suites instead. BACKLOG row added with `Implement by: Before
   next schema migration lands after 20260902_02`; that owner must re-pin or
   fix the suite and add the reduced `ck_user_credentials__timestamps`
   clause to the baseline manifest.
2. **No real-DDL head assertion of the reduced timestamp clause — code
   stands.** The reduced clause text is pinned exactly by the migration unit
   suite and by chain-replay metadata contracts, and the real-DDL gate
   proved the constraint rebuild applies and rolls back; no test reads the
   live catalog at head to compare the clause string. The re-pinned baseline
   manifest (item 1) is the natural owner of that assertion.
3. **SQLAlchemy private API in the chain-replay recorder — code stands.**
   `tests/contract/source_publication/test_table_metadata.py`
   `drop_column()` removes through `table._columns` because SQLAlchemy Core
   tables expose no public `drop_column`; the use is documented in an inline
   comment and fixed a latent dead-code bug (the method had never been
   exercised before this revision). If SQLAlchemy ever offers a public path,
   switch then.
4. **Stacking-comment drift — code stands.** The hand-maintained "which
   revisions stack on which" prose in head-pin comments (e.g.
   `tests/unit/migrations/test_small_file_sync_migration.py`,
   `tests/contract/exclusion_policy/test_policy_migration_contract.py`) does
   not enumerate every intermediate revision (the source-conflicts revision
   `20260902_01` is absent from the enumerations). Behavior is pinned by the
   `get_heads()` assertions; the next head migration should either enumerate
   fully or reduce the prose to the assertion.

No other findings were deferred; every in-scope finding was fixed in-branch.
No secrets, tokens, raw content or user data appear in this handoff.

## Next actions

1. Merge `retire-initial-totp-offer` per the controller's process; the docs
   closure commit is the branch tip.
2. Next schema migration owner (per the BACKLOG row): re-pin or fix
   `tests/integration/test_canonical_postgresql_baseline.py` to the current
   head and add the reduced `ck_user_credentials__timestamps` clause to its
   manifest.
3. Optional, for that same owner: if the one chain-note mention of
   `totp_prompt_dismissed_at` in `src/personal_os/database_schema.py` is
   ruled undesirable under the retirement token policy (Decision #1), reword
   it in the next head-bump diff, where that comment is edited anyway.
