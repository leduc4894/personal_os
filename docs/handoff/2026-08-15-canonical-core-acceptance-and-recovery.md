# Canonical Core Acceptance and Recovery Handoff

**Date:** 2026-08-15
**Plan:** `docs/superpowers/plans/2026-08-15-canonical-core-acceptance-and-recovery.md`
**Spec:** `docs/superpowers/specs/canonical-core-acceptance-and-recovery-design.md`
**Branch:** `canonical-core-acceptance-recovery`
**Final code commit:** `76202b1` — Tasks 1-15 implemented on branch
`canonical-core-acceptance-recovery` in the range `dc3f3de..HEAD` (19 commits:
`efe9dc6` identity contracts → `76202b1` phase-one acceptance composition and
protected workflow). This documentation commit (runbook + handoff + BACKLOG)
follows it.

Living operational status: `docs/operations/canonical-core-recovery.md`.

## What was built

- `src/personal_os/identity/` — bootstrap contracts/validation (closed grammar,
  reason tokens), drift-classifying service, PostgreSQL store with the exact
  replay and `identity.bootstrap_completed` / `identity.bootstrap_rejected`
  audit semantics.
- `src/personal_os/sources/reading/` + `postgresql_source_store/canonical_read.py`
  — canonical current-source read (fail-closed state checks, verified R2
  reader, no content in diagnostics).
- `src/personal_os/recovery/` — contracts/manifest/ports, the immutable
  filesystem bundle store (`bundle.py`), the backup/verify/restore service
  with quiesced-snapshot consistency and single-transaction restore.
- `packages/postgresql-source-store/` — quiesced snapshot store and restore
  target adapters.
- `tools/` — `canonical_core_operations.py` (six-subcommand CLI and the
  `run_phase_one_acceptance` composition), `canonical_recovery_bundle.py`,
  `postgresql_dump_process.py` (bounded pg_dump/pg_restore boundary).
- Tests — unit suites per module, the disposable PostgreSQL 18.4 integration
  suite (`tests/integration/canonical_core`, `-m local_stack`), the protected
  live-R2 drills (`-m "local_stack and r2_live"`), and the composition/CI
  contract suites (`tests/contract/canonical_core`,
  `tests/contract/test_ci_security.py`).
- CI — `.github/workflows/canonical-core-acceptance.yml` (protected
  trusted-surface-only live gate).

## Gate status (with evidence)

All evidence below was produced on the final code commit `76202b1` on this
host (Windows, Git Bash), 2026-08-15.

| Gate | Status and evidence |
|---|---|
| `uv run poe python-lint` | green — `All checks passed!` |
| `uv run poe python-type-check` | green — `Success: no issues found in 78 source files` |
| `uv run poe format-check` | green — `183 files already formatted`; obsidian-plugin and web eslint `Done` |
| `uv run poe boundary-check` | green — `Contracts: 5 kept, 0 broken.`; architecture tests `8 passed in 0.73s` |
| `uv run pytest -q` (full default suite, `-m "not local_stack and not r2_live"`) | green — `1411 passed, 19 skipped, 112 deselected in 96.44s` |
| Disposable-stack integration gate (Task 13, live local run) | green — `uv run pytest tests/integration/canonical_core -m local_stack -q` → `15 passed in 216.05s`, including the controller's independent 5/5 re-run of the live identity module and zero leftover labelled Docker resources; surfaced and fixed three live-blocking production bugs (commit `b78aa15`) |
| Phase-one acceptance composition (Task 15) | green — `pytest tests/unit/tools tests/contract/canonical_core tests/contract/test_ci_security.py -q` → `256 passed, 3 skipped`; shared-module regression `406 passed, 4 skipped` |
| Protected live-R2 CI workflow | **deferred, by design** — the workflow triggers only on protected `master` pushes (never this branch, never forks); its first execution is pending merge. No CI live-R2 evidence is claimed. Local fail-closed smoke of the CLI exits 78 with one safe JSON document, as pinned. |
| CLI/exit-code cross-check for this handoff | green — every flag, subcommand and exit code in `docs/operations/canonical-core-recovery.md` verified against `uv run python tools/canonical_core_operations.py --help` (and per-subcommand help) on 2026-08-15 |

## Interpretive decisions (with rationale)

1. **`recovery/bundle.py` module addition.** The spec's proposed layout (spec
   4.1) had no dedicated bundle-store module; the plan added
   `src/personal_os/recovery/bundle.py` so the private immutable filesystem
   bundle writer/verifier keeps one clear purpose. The spec explicitly permits
   consolidation adjustments with clear purpose; contract/manifest/ports stay
   in their own modules.
2. **Server-version refusal via `dependency_unavailable`/`postgresql`.** A
   restore target whose PostgreSQL does not report exactly the pinned `18.4`
   refuses with `CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE` (dependency
   `postgresql`), not a configuration code: the server is a dependency the
   operator can stand up correctly, and the registry marks the code retryable
   — hence CLI exit `75`. The Debian `18.4 (Debian 18.4-1.pgdg12+1)` build is
   normalized by comparing only the first `server_version` token.
3. **Exit 75 vs 69 for retryable dependencies (incl. Temporal unreachable).**
   Spec 13 was ambiguous; the closed error registry marks every dependency
   code retryable, and the CLI's closed table maps retryable dependency
   failures to the busy class (`75`). Temporal-unreachable in
   `phase-one-acceptance` (`projection_dispatch_unavailable`) therefore exits
   `75`; exit `69` stays reserved for a future non-retryable dependency code
   and is currently unreachable via the registry. Adjudicated at Task 15 and
   documented in the runbook's exit table.
4. **Admission-refusal `result_code` reuse.** A missing
   `--confirm-write-admission-disabled` or a `--confirm-target-database`
   mismatch refuses via `CANONICAL_RECOVERY_ENVIRONMENT_REFUSED` (exit `78`,
   correct class per spec 9.1 "the flag is admission"); only the token label
   is imprecise. A dedicated admission token was deferred (BACKLOG).
5. **Per-test pristine identity databases.** Identity bootstrap classifies the
   whole users/workspaces/devices graph, but `audit_events` is append-only and
   FK-RESTRICTs the workspaces it names, so the graph cannot be reset by
   deletion. Each identity integration test instead gets its own
   `knowledge_ci_identity_<nonce>` database, migrated with the real Alembic
   baseline and dropped `WITH (FORCE)` afterwards.
6. **Restore's fresh offline verification via `open_verified`.** The restore
   flow verifies the bundle through `bundle_store.open_verified`, which
   internally runs the complete spec-10 `verify_offline`, instead of calling
   `verify_offline` separately and hashing every bundle file twice. The port
   gained the `verify_offline` declaration so the service stays
   protocol-typed.
7. **Second-database creation via `docker compose exec`.** The disposable
   restore/identity databases are created by
   `docker compose ... exec postgresql psql` with the stack-bootstrap
   superuser credential read from the mounted secret — the password never
   leaves the container shell and never enters the test process environment.
8. **Worker-loop runner harness deviation.** Windows cannot host psycopg async
   (needs `SelectorEventLoop`) and asyncio subprocesses (need the proactor
   loop) on one event loop; the integration conftest therefore runs the
   unmodified production `run_bounded_child` on a loop owned by a worker
   thread. On Linux CI this is a harmless transport detail.
9. **Manifest carries nine JSON keys** (the brief's encode sketch listed
   eight, its own parse contract mandates the `contract` key) and event
   durations use `duration_ms` (AGENTS naming rule) — both pinned by tests.
10. **`byte_total` counts content objects only** (dump excluded), matching the
    manifest totals the offline verifier re-checks; documented on the result
    types.
11. **`KNOWLEDGE_ENVIRONMENT` missing defaults to `local`** for the CLI gate,
    mirroring the runtime-configuration fragment default; the gate refuses
    only explicit non-local/test values.
12. **Task 3's unwired `IDENTITY_BOOTSTRAP_REJECTED` sink** was resolved in the
    Task 15 composition by binding the CLI's validating `DiagnosticLogger`
    into `PostgresqlIdentityBootstrapStore`.

## Deferred items (verdicts)

All items below were adjudicated non-blocking by task reviews. Grouped
minors share one BACKLOG line per group; each group's details live here.

- §1 Identity input-validation hardening (Task 1): non-string free-text inputs
  raise `AttributeError` instead of a typed error; non-string
  username/workspace_key both report `username_invalid`. Verdict: deferred —
  no CLI path passes non-strings (argparse values are always `str`); typed
  guards worth adding with the next identity contract change.
- §2 Latent circular import `diagnostics` <-> `error_contracts.exceptions`
  (Task 1, pre-existing): currently dodged by import ordering in every
  domain. Verdict: deferred — deserves a dedicated structural fix (see also
  the related source-publication BACKLOG item).
- §3 Canonical-read service hardening (Task 4): an `ApplicationError` raised
  by the consumer body is conflated with the read-failure metric/event
  (yield inside try); missing/corrupt tests assert the FAILED metric but not
  the FAILED event. Verdict: deferred — same shape as the plan sketch;
  harden before adding read alerting.
- §4 Test precision (Task 5): the lookup-statement filter test name
  overpromises (join count asserted, not the WHERE predicate). Verdict:
  deferred — cosmetic test-name tightening.
- §5 Recovery contract edges (Task 6): `CanonicalBackupSnapshot`'s default
  dataclass repr exposes `snapshot_token`; a non-dict JSON manifest reports
  `contract_unsupported` instead of `json_noncanonical`. Verdict: deferred —
  add the redacted repr in the next adapter touch; the non-dict edge is
  unpinned by the closed token set.
- §6 Bundle-store minors (Task 7): finalize-rename TOCTOU vs an empty final
  directory on POSIX (stdlib lacks `RENAME_NOREPLACE`); the verify-totals
  `object_count` check is tautological; conftest mkdtemp prefix `rk7` is
  cryptic. Verdict: deferred — all defense-in-depth or naming cosmetics.
- §7 Dump-process adapter hardening (Task 8): `ProcessRunResult.stdout`
  appears in the dataclass repr (harden `repr=False`); a chatty child drained
  after exit could false-timeout; passfile escaping not applied; restore-time
  error mapping untested. Verdict: deferred — no production path reprs the
  result today.
- §8 Snapshot-adapter precision (Task 9): pending-writer query joins on
  relname only (no `pg_namespace` schema constraint — spurious-abort
  direction only); `alembic_version` hardcoded to the `public` schema;
  `SET LOCAL lock_timeout` is inert for `NOWAIT` locks (spec mandated both).
  Verdict: deferred — conservative failure direction only.
- §9 Bounded-memory/event-loop hygiene (Tasks 10-11): the buffered object-copy
  path materializes up to 100 MiB per object (the writer API's own bound) and
  uses sync file I/O inside coroutines (create and restore paths); failed
  restore metrics hardcode 0/0 totals (deliberate closed-sink convention).
  Verdict: deferred — bounded, documented; revisit if bundle sizes grow.
- §10 CLI admission-refusal label (Task 12): admission refusals reuse the
  `ENVIRONMENT_REFUSED` result code (exit 78 correct, label misleading).
  Verdict: deferred — split the token only with a registry change.
- §11 CLI composition hygiene (Task 12): no engine disposal at compose time
  (lazy engine, no connection opened); the `canonical-core-test` Poe task is
  standalone, not composed into `poe verify`. Verdict: deferred — deliberate
  local-only composition; composing the focused task into `verify` would slow
  every gate run.
- §12 Integration-harness hygiene (Task 13): conftest `bundle_root` leaks
  temp dirs on the POSIX branch; `Any`-typed runner/shim signatures;
  `LocalFilesystemObjectStore` silently passes a same-digest-different-media
  re-store. Verdict: deferred — test-only surfaces.
- §13 Live-harness type precision (Task 14): `cast(LocalFilesystemObjectStore)`
  type-lie bridging a Task 13 harness annotation; an unused discarded harness
  in `live_acceptance_context`. Verdict: deferred — test-only.
- §14 Acceptance polish (Task 15): the boundary test name
  `test_no_database_url_or_pgpassword_anywhere_in_tools` overstates scope
  (scans the two canonical-core tools only); `duration_ms` bypasses the clock
  seam. (An earlier draft of this item also called the workflow's
  `TEMPORAL_GRPC_PORT`/`TEMPORAL_UI_PORT` env vars dead — corrected at the
  final-branch review: `tools/local_service_stack.py` consumes both.)
  Verdict: deferred — rename/tighten with the next workflow touch.

Resolved during the plan (no BACKLOG line): the Task 3 rejection-sink wiring
(decision 12); the Task 10 formatting drift (style commit `1c4ec05`); the
Task 7 ops-doc Windows MAX_PATH requirement (now in the runbook's
Configuration section); the Task 12 runbook mandate to document the 69/75
mapping (now in the runbook's exit table); the Task 14 concurrent-CI-legs
bucket note (the workflow is single-leg and documents per-run digest
uniqueness).

## Next actions

1. Merge `canonical-core-acceptance-recovery` into `master`; the first
   protected `canonical-core-acceptance` workflow run then executes the live
   R2 drills — record its run link in
   `docs/operations/canonical-core-recovery.md` (Acceptance status) and act
   on any failure before relying on the gate.
2. Phase 2 Obsidian sync (per `docs/20-IMPLEMENTATION_PLAN.md`), building on
   the identity graph and canonical publication/read paths this plan landed.
