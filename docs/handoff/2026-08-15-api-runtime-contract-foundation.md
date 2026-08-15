# API Runtime and Contract Foundation Handoff

**Date:** 2026-08-15
**Plan:** `docs/superpowers/plans/2026-08-15-api-runtime-and-contract-foundation.md`
**Spec:** `docs/superpowers/specs/2026-08-15-api-runtime-and-contract-foundation-design.md`
**Parent:** `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md` (child 1)
**Branch:** `api-runtime-contract-foundation`
**Final code commit before this handoff commit:** `9cb5b0e`
**Commit range:** `3b09967..9cb5b0e` (16 commits; `3b09967` is the pre-plan
baseline repair described in decision 1, parent `1646847` = `master`).

Living operational status: `docs/operations/api-runtime-contract.md` (the
operator runbook — commands, bind rules, health semantics, OpenAPI pipeline,
transport boundaries). Canonical status: `docs/20-IMPLEMENTATION_PLAN.md`
(Phase 2 child-spec status).

## What was built

- `src/personal_os/api_contracts/` — framework-neutral strict envelopes
  (`ApiEnvelope` with data/error XOR), warning grammar, closed HTTP
  error-status table, exact health payload models and the
  `CanonicalDatabaseReadinessProbe` protocol; four new `ErrorCode` registry
  members plus `ApiTransportError`.
- `src/personal_os/database_schema.py` — single schema-revision authority
  (`CANONICAL_POSTGRESQL_SCHEMA_REVISION = "20260813_01"`); the recovery
  constant is now an alias.
- `packages/postgresql-source-store/readiness.py` — bounded probe proving
  connectivity and the exact Alembic head, with private exception mapping.
- `apps/api/` — `server_settings` (loopback defaults local/test only),
  `request_context` (pure-ASGI correlation middleware: server UUIDv7,
  `X-Client-Request-ID` validation, W3C traceparent, closed access events),
  `application` (FastAPI factory, health routes, four exception handlers),
  `database_lifecycle` (lazy engine, idempotent disposal), `server`
  (`run_server` with Uvicorn single-process contract and exit codes
  0/70/78), `openapi_export` (deterministic normalized document), and lazy
  `serve`/`export-openapi` CLI dispatch via `BootstrapSubcommand`.
- `packages/api-client/` — `@workspace/api-client`: committed snapshot
  `openapi.json`, `openapi-typescript`-generated `src/generated/schema.ts`,
  transport-injected `createApiClient({baseUrl, transport})`, envelope type
  aliases; no automatic retry.
- `apps/web` + `apps/obsidian-plugin` — native-fetch and `requestUrl`
  transports over the shared client; ESLint `no-restricted-imports` permits
  only `@workspace/api-client` across workspace members.
- `tools/api_contract_artifacts.py` + Poe tasks `api-contract-export`,
  `api-contract-check` (composed into `boundary-check` and `verify`).
- Tests — unit suites per module, ASGI contract suites
  (`tests/contract/api/`), sensitive-HTTP sentinel regression, PostgreSQL
  readiness integration (`-m local_stack`), Uvicorn lifecycle integration,
  and the documentation contract (`tests/contract/api/test_api_documentation.py`).

## Gate status (with evidence)

Run on 2026-08-15 at commit `9cb5b0e` (this handoff's documentation files
were the only uncommitted worktree content; they are markdown/tests only and
cannot affect the code gates).

| Gate | Result |
| --- | --- |
| `uv run poe api-contract-check` | exit 0 — `api_contract_current`; openapi-typescript 7.13.0 `generate:check` clean |
| `uv run pytest tests/unit/api_contracts tests/unit/api_runtime tests/unit/postgresql_source_store/test_readiness.py tests/contract/api tests/integration/api_runtime/test_uvicorn_lifecycle.py -q` | `106 passed in 3.87s` |
| `uv run poe verify` | exit 0 — format-check `216 files already formatted`; ruff `All checks passed!`; mypy strict `Success: no issues found in 93 source files`; import-linter `Contracts: 5 kept, 0 broken.` + architecture tests `9 passed`; API contract check current; Python suite `1560 passed, 19 skipped, 115 deselected in 103.93s`; api-client `1 passed`; web `2 passed`; obsidian-plugin `11 passed`; all wheels + pnpm builds succeeded |
| Disposable PostgreSQL gate (rerun on the final pre-handoff commit, not claimed from Task 11's `0b9b0e2`): `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-api-runtime POSTGRES_PORT=55432 uv run pytest tests/integration/canonical_core/test_api_readiness_integration.py -m local_stack -q` | exit 0 — `3 passed in 77.99s` (exact head, revision drift, refused port); zero leftover Docker resources labelled `knowledge-ci-api-runtime` afterwards |
| `git diff --check` | empty |
| `git status --short` | only this task's documentation files and the new documentation contract test |
| `wc -l AGENTS.md CLAUDE.md` | 110 and 111 (unchanged) |

## Interpretive decisions (with rationale)

1. **Baseline repair `3b09967` (pre-plan).** A pre-existing master failure:
   the user's `1646847` reorganization moved specs to
   `docs/superpowers/specs/phase 1/` but
   `tests/contract/test_local_service_stack_contract.py` still pointed at the
   old path. One-line path fix, required so this plan's own `poe verify`
   exit-0 expectation starts from a green baseline.
2. **Task 10 null-body statuses (commit `7aad18d`).** The plan's verbatim
   adapter snippet `new Response(result.arrayBuffer, {status})` throws
   `TypeError` on null-body statuses 204/205/304 (Obsidian always populates
   `arrayBuffer`, even as zero bytes). Passing a null body for exactly that
   closed status set contradicts no plan requirement and aligns with the
   plan's response-preservation constraints; covered by `it.each` tests.
3. **Workspace-protocol pin-guard exemption (commit `9cb5b0e`).**
   `test_dependency_pins` rejected the plan-mandated
   `"@workspace/api-client": "workspace:*"` specifiers in the web/plugin
   manifests. The workspace protocol never resolves against the npm registry,
   so exempting it preserves the guard's intent (every registry dependency
   stays a bare pinned version) instead of defeating it.
4. **Scoping `tests/contract/source_publication/test_no_public_api.py`
   (Tasks 5 and 7).** That guard predates any sanctioned web framework. The
   API runtime is now the sanctioned FastAPI root, so the structural
   framework-free scan targets the MCP root only
   (`PYTHON_FRAMEWORK_FREE_ROOTS`), and the OpenAPI token scan targets the
   endpoint-declaring `paths` subtree — the `ErrorCode` enum legitimately
   contains `source_version_conflict`, which is an error-condition name, not
   a declared endpoint. All five publication-endpoint tokens remain enforced
   on paths/operation ids/summaries.
5. **Import-side-effect harness isolation (commit `d64c866`, Task 6
   post-review fix).** The laziness harness purged only top-level
   `sys.modules` names, leaving uvicorn submodules cached; a later
   `run_server` in full-suite order re-imported `uvicorn/__init__` (via
   `logging.config.dictConfig`) whose submodule attributes never rebind,
   producing `exit 70` in three tests. Fixed by snapshot/restore of the exact
   purged entries around each laziness proof; the defect was purely harness
   state leakage, and it also resolved Task 7's observed order-dependent
   failures.
6. **Secondary, from task reports:** envelope type aliases follow the real
   generated names (`ApiEnvelope_LivenessData_`/`_ReadinessData_` — the
   plan's `LivenessResponse` sketch exists in no snapshot); the correlation
   middleware wraps the built middleware stack
   (`app.middleware_stack = ...`) so the catch-all 500 keeps the bound
   request id and correlation headers — composition is closed afterwards by
   design; PEP 695 generics replace the plan's `TypeVar` sketch (ruff
   UP046/UP047); `ReadinessChecks.schema` keeps the contractual field name
   with one narrowly matched pydantic-deprecation warning filter.

## Deferred items (verdicts)

Review-flagged minors adjudicated non-blocking. Only §1 gets a BACKLOG line
(a standing toolchain warning that must resurface on any pin bump); the rest
are polish notes owned by this handoff.

- §1 `openapi-typescript@7.13.0` peer-declares `typescript@^5.x` while the
  workspace pins `typescript 6.0.3` — benign standing `pnpm install` warning;
  generation/type-check/build all pass. Verdict: defer, re-evaluate on the
  next `openapi-typescript` or `typescript` pin bump. (BACKLOG line added.)
- §2 pnpm reports `Ignored build scripts: unrs-resolver@1.12.2` (transitive
  of openapi-typescript), matching existing workspace behavior
  (`onlyBuiltDependencies` lists only `esbuild`). Verdict: defer; revisit if
  resolver native artifacts ever matter.
- §3 The catch-all `Exception` handler runs in Starlette's
  `ServerErrorMiddleware`, which re-raises after sending the 500 envelope
  (uvicorn then logs the traceback) — standard FastAPI behavior, tests use
  `raise_app_exceptions=False`. Verdict: accept.
- §4 `ApiServerSettings` adds no `__repr__` redaction (the model carries
  only bind coordinates, unlike the database settings that redact
  host/user). Verdict: defer; add redaction if the fields ever grow.
- §5 One transient, non-reproducible `boundary-check` flake (1 failed/8
  passed, subprocess-spawning `lint-imports` tests — a Windows `.cmd`-shim
  pattern this repo already documents) in one chained gate run during Task 3;
  six subsequent runs green. Verdict: accept as environment flake.
- §6 `packages/api-client` ESLint ignores `src/generated/**` (CLI-owned
  output must never be hand-fixed); the Python boundary scan still scans it
  (zero imports). Verdict: accept.
- §7 `pnpm-lock.yaml` carries incidental peer-resolution churn for
  `eslint-config-next` snapshots (suffix changes only, no version pin
  changed) from Task 10's install. Verdict: accept.
- §8 `ForbiddenEnvironment` test accommodations (pydantic 2.13 plugin loader
  reads `PYDANTIC_DISABLE_PLUGINS`; pytest renders six terminal env keys) are
  documented in `tests/unit/api_runtime/test_openapi_export.py`. Verdict:
  accept, documented.

## Next actions

1. Merge `api-runtime-contract-foundation` into `master`.
2. Start Phase 2 child 2, `web-auth-and-device-authorization-design.md`
   (brainstorm → design → plan per the umbrella's child program; it may
   extend the API surface only on top of this green generated-client gate).
3. Watch the BACKLOG line for §1 on the next toolchain pin bump.
