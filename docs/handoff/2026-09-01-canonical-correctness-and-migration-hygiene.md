# Canonical Correctness and Migration Hygiene — Handoff

- **Plan:** `docs/superpowers/plans/2026-08-31-canonical-correctness-and-migration-hygiene.md`
- **Spec:** `docs/superpowers/specs/backlog/2026-08-31-canonical-correctness-and-migration-hygiene-design.md`
- **Branch:** `canonical-correctness-migration-hygiene` (from `master` @ `9762124`)
- **Final SHA:** `f32138f64152ad0dde1da78f4b6fdb40686ec4c0`
- **Status:** COMPLETE — all 8 plan tasks done, per-task SDD reviews clean, final whole-branch
  review findings fixed and re-verified. Branch awaits merge decision.

## Gate status (evidence)

| Gate | Result | Evidence |
|---|---|---|
| Both committed-RED tests green | PASS | `test_terminal_transition_clears_raw_locator_and_keeps_digest` + `test_gated_downgrade_drops_the_operation_table_and_reapplies_head` verified green by name in the final live run |
| Live local_stack sweep (`tests/integration/source_publication tests/integration/canonical_core -m "local_stack and not r2_live"`) | PASS | 101 passed / 0 failed / 6 deselected at `d254a99` (project `knowledge-ci-plan3-final-verify`); after the final fix wave the small-file file re-verified 26/26 on a fresh stack (`knowledge-ci-plan3-fr1-finalfix`) |
| `uv run poe verify` | PASS (exit 0) | At final HEAD: python 4319 passed / 21 skipped, obsidian-plugin 1245, web 163, builds green. Final green run serialized via `npm_config_workspace_concurrency=1` — see deferred item #2 |
| `uv run poe api-contract-check` | PASS (exit 0) | `api_contract_current`; OpenAPI snapshot unchanged |
| BACKLOG retirement check | PASS | `rg` for all seven row signatures in `docs/handoff/BACKLOG.md` → no hits; 7/7 rows retired |

## What landed (12 commits, `0cf0741..f32138f`)

1. `af026b2` — diagnostics/runtime-configuration import back-edge removed (`TYPE_CHECKING` guard).
2. `ab41e58` — non-integer epoch clock gets closed token `epoch_ms_clock_non_integer` (small-file-sync copy only).
3. `23ba56b` — fresh lifecycle commits record `COMMITTED` (service side); replays stay `REPLAYED`-only.
4. `e1908ed` — spool `_run_shielded_cleanup` mirrors the adapter's cancellation-preserving shield.
5. `50a26dd` — terminal transitions clear the raw locator (both success variants, clear-before-terminal, same transaction, zero-row `_state_invalid` guards).
6. `0fbd3d6` — gated downgrades preflight the small-file evidence gate before any drop (`allow_destructive_requested` shared helper).
7. `79ced91` — device-sync indexes `ix_sync_events__workspace_event_sequence` + partial `ix_source_tombstones__restore_event_id` (revision `20260901_02`, head constant bumped).
8. `f6b52a6` + `d254a99` — verification-gate repairs (diagnostics contract pin; recovery-test mismatch sentinel `19700101_00` + collision guard; one reformat).
9. `a6e9411` — service is the sole `COMMITTED` metrics emitter (store-side recording removed; store `metrics` param removed; composed-runtime regression tests added).
10. `f32138f` — bound terminal **failures** also clear the raw locator (third and last terminal writer; companion integration test).

## Interpretive decisions (spec-level, with reasoning)

- **Task 1 non-repro:** the documented collection failure could not be reproduced (`runtime_configuration/__init__.py` is empty, so the chain's final hop cannot fire). The primary `TYPE_CHECKING` fix still applied on the unambiguous type-only classification; a `sys.modules` probe showed the back-edge is structurally gone post-fix.
- **Task 6 head assertion:** the extended refusing test pins the walk-stop at the refusing revision `20260820_01` (not the pre-downgrade head) — under `transaction_per_migration`, revisions above the refusal commit their downgrades first. Spec C6 explicitly allows "or a state the existing reapply-head recovery handles"; no revision is left half-applied and the small-file evidence is intact. A walk-level (`env.py`) preflight would be a different mechanism than the plan mandated.
- **Task 6 gated-test head pin:** the stale `"20260818_01"` literal (true only when that revision was head) became a dynamic `_chain_head()` pin that hard-fails on multi-head; survives future revisions.
- **Task 7 down_revision:** set to the actual head at task start (`20260901_01`); 15 existing test files pinning head/count/file-list were mechanically updated (verified no assertion weakened); the multi-workspace query-plan pin uses a 400-event second workspace (measured planner flip boundary ~5–9% share; same planner-dependence class as the file's existing pins).
- **Sole-emitter ruling (final review Critical):** the durable store already recorded `COMMITTED` into the same shared recorder as the service in the serve graph (`server.py`), so Task 3's original change double-counted. The service is now the sole emitter — it is the only caller of `store.commit`, and Task 3's tests/docstrings assume it. The 2026-08-24 BACKLOG row's premise ("route can never show a committed row") was false for the serve graph; the landed end-state satisfies the row's intent with a single accurate counter.
- **Terminal-writer enumeration (final review Important):** C5 required enumerating ALL terminal writers, not just the two the plan named. `_apply_bound_terminal_failure` (`failed` path) also cleared nothing; it now mirrors the same reorder. Re-review enumerated all three terminal statements in the module — every one now clears conditionally with a zero-row guard.

## Deferred items (each has exactly one BACKLOG row)

1. **Historical failed rows retain `normalized_locator`** (small-file). All three terminal writers now clear the raw locator going forward, but rows that reached terminal state *before* this fix still carry it; the identical-replay early-return is intentionally non-mutating, so there is no retroactive clear. Needs a one-time data remediation (e.g. a guarded migration nulling `normalized_locator` on terminal rows while keeping fingerprints). *Implement by: Before production activation.*
2. **jsdom 5s timeouts flake under concurrent pnpm workspace runs** (web infra, pre-existing, environmental). `poe verify`'s `typescript-test` step can flake on a busy machine; tests pass in isolation and under `npm_config_workspace_concurrency=1` (scheduling-only, no repo change). *Implement by: At next web tooling pin bump.*

Deferred minors reviewed and ruled non-blocking at final review: Task 1 comment wording; Task 2 docstring pointer; Task 5 comment asymmetry + pre-terminal non-NULL premise; Task 6 order-pin test name + `env.py` local literal; Task 7 grant-poll literal + empirical planner margin. Rulings live in the SDD ledger (deleted with the scratch workspace); the git history carries the code record.

## Environmental notes

- `serve-live-ci.sh up` on a fresh CI project fails the API-readiness sub-gate (`exclusion_policy_not_initialized`) — documented pre-existing since 2026-08-27; DB-level prerequisites (provision + alembic head) are met and all DB-level local_stack tests ran green despite it.
- The recovery integration suite needs `C:\Program Files\PostgreSQL\18\bin` prepended to Bash `PATH` (pg tools installed but off PATH) — already handoff-documented elsewhere.

## Next actions

1. Merge decision for `canonical-correctness-migration-hygiene` (12 commits, all gates green).
2. The sibling plan `2026-08-31-policy-diagnostics-metrics-sink-and-live-smoke` (untracked docs in the working tree) is untouched by this branch and ready for its own SDD run.
3. On merge: nothing further required — BACKLOG rows for this plan are retired; the two new deferred rows above carry their own triggers.
