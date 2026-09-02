# Serialized Workspace jsdom Tests — Handoff

- **Plan:** `docs/superpowers/plans/2026-09-02-serialized-workspace-jsdom-tests.md`
- **Spec:** `docs/superpowers/specs/2026-09-02-serialized-workspace-jsdom-tests-spec.md`
- **Branch:** `serialized-workspace-jsdom-tests` (from `master` @ `ce70854`)
- **Final code SHA:** `4c95209` (the docs-only closure commit carrying this
  handoff follows it; a handoff cannot contain its own carrying commit's hash)
- **Status:** COMPLETE — both plan tasks landed. The root test command pins
  `--workspace-concurrency=1` behind a contract test, repeat runs prove no
  ambient `npm_config_workspace_concurrency` setting is needed, the full
  repository gate passes through the committed command, and the deferred jsdom
  flake BACKLOG row is deleted.

## Gate status (evidence)

All evidence sanitized to command outcome, suite counts, and durations; no
fixture content copied. Timestamps local (UTC+7), 2026-09-02.

| Gate | Result | Evidence |
|---|---|---|
| `npm_config_workspace_concurrency` unset check | PASS | `printenv npm_config_workspace_concurrency` exit 1 (not set); `test -z "${npm_config_workspace_concurrency:-}"` confirms unset/empty in the shell; sanity probe (`npm_config_workspace_concurrency=MISSING printenv …`) shows the check detects the variable when set |
| `pnpm run test` — repeat run 1 (env var unset) | PASS (exit 0) | 80s wall clock; scope 3 of 4 workspace projects; `@workspace/api-client` 1 file / 1 test; `@workspace/obsidian-plugin` 64 files / 1444 tests (47.25s); `@workspace/web-runtime` 21 files / 161 tests (26.61s); zero failures |
| `pnpm run test` — repeat run 2 (env var unset) | PASS (exit 0) | 68s wall clock; identical counts: 1/1, 64 files / 1444 tests (37.81s), 21 files / 161 tests (25.47s); zero failures |
| `uv run poe verify` (at `4c95209`, clean tree, no env workaround) | PASS (exit 0) | 478s total; 14 sub-gates green in order: ruff format-check, pnpm format:check, ruff lint ("All checks passed!"), pnpm lint, mypy strict, pnpm type-check, lint-imports, architecture-boundary contract (10 passed in 2.86s), api-contract artifacts check (`api_contract_current`), api-client `generate:check`, pytest `4624 passed, 21 skipped, 550 deselected, 1 warning` in 329.47s, `pnpm run test` (all three suites green, counts as above), `uv build --all-packages --clear`, `pnpm run build` (api-client, obsidian-plugin, web all Done) |
| `git diff --check` (pre-docs) | PASS | exit 0, no whitespace errors |
| `git status --short` (pre-docs) | clean | no entries before the two closure files |

## What landed

1. `4c95209` — Task 1: root `scripts.test` pinned to
   `pnpm --workspace-concurrency=1 --recursive --if-present run test`
   (`package.json`), plus the pinning contract test
   `test_root_test_script_serializes_workspace_suites` in
   `tests/contract/test_quality_orchestration.py` (RED before the one-line
   change, GREEN after; 4 passed in the focused run). Exactly those two files.
2. Docs closure commit (this handoff): deleted the single
   `2026-08-31 | web-infra (pre-existing)` jsdom-flake row from
   `docs/handoff/BACKLOG.md` (14 → 13 data rows, one-line deletion) and added
   this handoff. Docs-only.

## Decisions and interpretations

1. **Scheduling-only mitigation at the single orchestration seam.** The flake
   was cross-suite CPU contention: pnpm's default parallel workspace fan-out
   raced the two jsdom-heavy suites (web, obsidian-plugin) on a busy machine,
   producing shifting 5s jsdom timeouts. Pinning `--workspace-concurrency=1`
   in the root `scripts.test` serializes the fan-out so package suites never
   contend with each other, while everything inside each suite — test-worker
   behavior, Vitest configs, timeouts, assertions — is untouched. No
   package-local `test` script, Vitest config, production code, dependency,
   lockfile, or CI workflow command changed; CI inherits the fix through the
   unchanged `uv run poe verify` → `typescript-test` → `pnpm run test` chain.
   The flake is closed by removing the contention, not by loosening the gates.
2. **No ambient environment setting required.** The shell was proven free of
   `npm_config_workspace_concurrency` (gate table, row 1), and both repeat
   runs additionally stripped it via `env -u` (the Git Bash equivalent of the
   brief's PowerShell `Remove-Item Env:…`). The committed root command carries
   the flag itself; nothing about a developer's or CI's environment must
   change.
3. **Repeat stability proven before closure.** Per the plan's constraint, the
   BACKLOG row was deleted only after two consecutive full-workspace green
   runs with identical per-suite counts and no ambient override. The row's
   `Implement by: At next web tooling pin bump` trigger never fired because
   the finding itself is resolved before any pin bump.

## Remaining unrelated warnings observed (no action, out of scope)

- One pytest warning during `poe verify`: `StarletteDeprecationWarning` from
  `fastapi`'s testclient import (third-party, pre-existing).
- Two MSW "unhandled exception during the handler lookup" log lines during
  web negative-path tests; identical across all three observed runs
  (deterministic) and those tests pass. Pre-existing, unrelated to suite
  scheduling.
- The standing `openapi-typescript@7.13.0` peer-dependency install warning
  stays indexed in BACKLOG (2026-08-15, api-contract) — it is an install-time
  warning in a different domain and did not appear in the verify output.

## Deferred items (verdicts)

1. **Contract-test KeyError on a vanished `scripts.test` key — code stands**
   (Task 1 review Minor). If `package.json` ever lost the `scripts.test` key
   entirely, the pinning test would fail with `KeyError` instead of a
   formatted assertion message. Verdict: fail-closed either way — a missing
   key still fails the gate loudly — and a vanished root test entry point is
   not a plausible drift mode. Ruled not worth a re-review cycle; unchanged
   from Task 1's review ruling.

No other findings were deferred; the plan had no in-scope findings left open.
No secrets, tokens, raw content or user data appear in this handoff.

## Next actions

1. Merge `serialized-workspace-jsdom-tests` per the controller's process; the
   docs closure commit is the branch tip.
2. None owed by this plan. If jsdom 5s timeouts ever recur, they would now
   occur inside a single serialized suite rather than from cross-suite
   contention — treat that as a new finding with a new handoff, since this
   plan's cause (parallel workspace fan-out) is eliminated.
