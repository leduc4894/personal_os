# Phase One Workspace Bootstrap Design

**Status:** Approved design
**Date:** 2026-08-12
**Phase:** Phase 1 — Bootstrap and canonical core
**Canonical plan:** `docs/20-IMPLEMENTATION_PLAN.md`

## 1. Objective

Establish reproducible Python and TypeScript workspaces, strict quality gates, executable composition-root shells and cross-platform CI without implementing product behavior from later Phase 1 specs.

The bootstrap must prove that all five deployable application boundaries can install, type-check, test and build independently while sharing locked toolchains and respecting the modular-monolith dependency rules.

## 2. Scope

This design owns:

- The Python `uv` workspace and shared `personal_os` distribution.
- The TypeScript `pnpm` workspace.
- Thin shells for API, MCP, worker, Web App and Obsidian plugin composition roots.
- Formatting, linting, strict type checking, unit/contract test entry points, import-boundary checks and builds.
- GitHub Actions quality gates on Ubuntu and Windows.
- Exact runtime, package-manager and direct-dependency pins with committed lockfiles.

This design does not own:

- Settings, secret-file loading, structured errors/logging or request/trace IDs.
- Database schemas, migrations, object storage or Docker Compose.
- FastAPI routes, MCP tools, Temporal workflows, authentication or dependency health checks.
- Product UI, Obsidian commands, event listeners or Vault access.
- Generated API clients, published packages, container images or release artifacts.
- Mutation testing or global coverage thresholds.

## 3. Selected approach

Use a monorepo with isolated workspace members:

- One root Python distribution containing `src/personal_os/`.
- Three Python application members for API, MCP and worker processes.
- Two TypeScript members for the Web App and Obsidian plugin.
- One `uv.lock` and one `pnpm-lock.yaml` for the repository.
- One cross-platform command surface that delegates to member-specific tooling.

This approach preserves deployable process boundaries without duplicating dependency graphs or weakening the canonical modular-monolith design.

Rejected alternatives:

1. A single undifferentiated Python and TypeScript project would reduce manifest count but allow process-specific dependencies and imports to leak across boundaries.
2. Fully independent projects and lockfiles would maximize isolation but create unnecessary dependency drift, duplicated configuration and CI overhead for a personal modular monolith.

## 4. Repository topology

```text
repository-root/
├── pyproject.toml
├── uv.lock
├── .python-version
├── package.json
├── pnpm-workspace.yaml
├── pnpm-lock.yaml
├── .node-version
│
├── src/
│   └── personal_os/
│       ├── __init__.py
│       └── py.typed
│
├── apps/
│   ├── api/
│   │   ├── pyproject.toml
│   │   └── src/api_runtime/
│   ├── mcp/
│   │   ├── pyproject.toml
│   │   └── src/mcp_runtime/
│   ├── worker/
│   │   ├── pyproject.toml
│   │   └── src/workflow_worker/
│   ├── web/
│   │   ├── package.json
│   │   └── src/
│   └── obsidian-plugin/
│       ├── package.json
│       └── src/
│
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── end_to_end/
│   ├── golden/
│   └── performance/
│
└── .github/
    └── workflows/
        └── quality.yml
```

The root Python project owns the `personal_os` distribution. The API, MCP and worker projects are `uv` workspace members that depend on that root distribution through a workspace dependency. They do not depend on one another.

The Web App and Obsidian plugin are the only `pnpm` members. No TypeScript shared package is created until a later spec defines a concrete shared contract.

Only packages required by this spec are created. Empty future-domain packages such as retrieval, graph or actions are forbidden.

## 5. Toolchain baseline

| Tool | Pinned version | Declaration |
|---|---:|---|
| CPython | `3.14.6` | `.python-version`; `requires-python = ">=3.14,<3.15"` |
| Node.js | `24.18.0` LTS | `.node-version`; root engine `>=24.18.0 <25` |
| uv | `0.11.32` | CI setup and documented prerequisite |
| pnpm | `10.34.0` | Root `packageManager` and CI setup |

CPython uses the standard build. The free-threaded build is outside scope.

CI must validate the actual runtime and package-manager versions before installing dependencies. A mismatched major/minor/patch is a failure, not a warning.

## 6. Dependency policy

### 6.1 Python development dependencies

The root Python project initially has no production dependency. It exact-pins:

| Dependency | Version | Purpose |
|---|---:|---|
| Ruff | `0.15.22` | Formatting and linting |
| mypy | `2.3.0` | Strict static typing |
| pytest | `9.1.1` | Unit and contract tests |
| pytest-cov | `7.1.0` | Diagnostic coverage reports |
| Import Linter | `2.13` | Python architecture boundaries |
| Poe the Poet | `0.48.0` | Cross-platform task orchestration |

`pytest-asyncio`, FastAPI, Temporal SDK, MCP SDK, database drivers and provider clients are not installed until a behavior in a later spec requires them.

### 6.2 TypeScript dependencies

Repository quality tooling exact-pins:

| Dependency | Version |
|---|---:|
| TypeScript | `6.0.3` |
| ESLint | `9.39.5` |
| typescript-eslint | `8.67.0` |
| Vitest | `4.1.10` |
| `@vitest/coverage-v8` | `4.1.10` |

TypeScript `7.0.2` is not selected because the pinned typescript-eslint release declares TypeScript support below `6.1.0`.

The Web App exact-pins:

| Dependency | Version |
|---|---:|
| Next.js | `16.3.0` |
| `eslint-config-next` | `16.3.0` |
| React | `19.2.8` |
| React DOM | `19.2.8` |
| `@types/node` | `24.13.3` |
| `@types/react` | `19.2.18` |
| `@types/react-dom` | `19.2.4` |

The Obsidian plugin exact-pins `obsidian` `1.13.1` and `esbuild` `0.28.2`.

Testing Library is not installed because this spec introduces no UI behavior.

### 6.3 Locking and upgrades

- All direct dependencies use exact versions.
- Transitive dependencies are fixed by `uv.lock` and `pnpm-lock.yaml`.
- CI installs with `uv sync --frozen` and `pnpm install --frozen-lockfile`.
- Wildcards, floating tags, prereleases and Git branch dependencies are forbidden.
- Lockfile integrity or checksum mismatch is terminal; automated lockfile rewriting in CI is forbidden.
- Dependency upgrades use a separately reviewed pull request with release-note and compatibility evidence.
- Automated dependency-upgrade tooling is not configured by this spec.
- Unreviewed lifecycle or postinstall scripts are forbidden. pnpm build-script allowlists may include only the exact native build packages required by the selected Next.js/esbuild dependency graph, as resolved in the lockfile; every allowlist change requires review.

### 6.4 Version revisions realized during implementation

ESLint was revised from the originally specified `10.8.1` to `9.39.5` (latest 9.x, exact pin) after implementation proved ESLint 10 runtime-incompatible with the pinned `eslint-config-next@16.3.0`: the Web lint gate crashed with `TypeError: scopeManager.addGlobals is not a function`, and the plugins bundled in `eslint-config-next@16.3.0` (`eslint-plugin-react@7.37.5`, `eslint-plugin-jsx-a11y@6.10.2`, `eslint-plugin-import@2.32.0`) all declare peer dependency ranges capped at ESLint `^9`. No gate or strictness was weakened; `9.39.5` is the minimal exact pin that makes the mandated lint gate functional. Evidence: `pnpm-lock.yaml` pins `eslint@9.39.5` for both TypeScript members, and the Ubuntu and Windows quality matrix both exit `0` on the final commit. This satisfies the section 12 requirement that a baseline version decision be revised explicitly with release-note, lockfile and compatibility evidence.

## 7. Composition-root contracts

### 7.1 Python processes

The workspace exposes:

```text
personal-api --help
personal-api --version
personal-mcp --help
personal-mcp --version
personal-worker --help
personal-worker --version
```

Each shell must satisfy:

- `--help` exits `0` and identifies the process.
- `--version` exits `0` and reads one shared distribution version from package metadata.
- No argument prints concise help and exits `0`; it must not start a daemon.
- An invalid argument exits `2` and writes a concise message to `stderr`.
- Importing or invoking the shell does not read environment variables, secret files or network resources.
- No shell imports FastAPI, MCP SDK or Temporal SDK.
- Entry modules use role-bearing names such as `command.py`; a generic `main.py` is forbidden.

The import packages are `api_runtime`, `mcp_runtime` and `workflow_worker`. These names describe composition roles and do not contain domain behavior.

### 7.2 Web App shell

- The Next.js App Router application must type-check and produce a production build.
- Its only page is a static bootstrap page that identifies the workspace shell.
- The build requires no secret, network service or API endpoint.
- API routes, server actions, proxy endpoints, authentication and product navigation are forbidden.

### 7.3 Obsidian plugin shell

- A valid plugin manifest and lifecycle entry point must compile into the minimum artifacts the Obsidian loader requires.
- `onload` and `onunload` have no product side effects.
- The plugin registers no command, ribbon, event listener or Vault access.
- Production artifacts exclude secrets, test fixtures and source maps by default.

### 7.4 Error boundary

CLI input errors must not display stack traces. Programming, import and build failures must remain visible and fail tests or CI.

This spec does not create an application error hierarchy or structured logging abstraction. Those contracts belong to `runtime-configuration-and-diagnostics-design.md`.

## 8. Architecture boundaries

```text
apps/api ────────┐
apps/mcp ────────┼──→ personal_os
apps/worker ─────┘

apps/web                 independent
apps/obsidian-plugin     independent
```

The following invariants are executable CI gates:

- `src/personal_os/**` never imports from `apps/**`.
- `personal_os` never imports FastAPI, SQLAlchemy or a database driver, Temporal SDK, MCP SDK, Qdrant client, Neo4j driver or provider SDK.
- Python composition roots never import one another.
- Web and Obsidian plugin code never import one another.
- Relative imports cannot cross a package boundary.
- `sys.path` mutation and TypeScript path aliases that bypass the declared dependency graph are forbidden.
- Every imported external package must be declared by the workspace member that consumes it.

Import Linter enforces Python boundaries. ESLint `no-restricted-imports` and pnpm workspace isolation enforce TypeScript boundaries.

A boundary exception requires a documented reason, owner and narrow path. Blanket ignores are forbidden.

## 9. Quality command contract

Poe the Poet exposes the same command surface on Windows and Linux:

```text
uv run poe format
uv run poe format-check
uv run poe lint
uv run poe type-check
uv run poe test
uv run poe boundary-check
uv run poe build
uv run poe verify
```

Poe only orchestrates underlying `uv` and `pnpm` commands; it contains no business logic.

`verify` runs in this order:

```text
format-check
→ lint
→ type-check
→ boundary-check
→ unit and bootstrap contract tests
→ build all five composition roots
```

Python uses Ruff format/check and mypy strict. TypeScript enables strict mode plus:

- `noUncheckedIndexedAccess`
- `exactOptionalPropertyTypes`
- `noImplicitOverride`
- `useUnknownInCatchVariables`

ESLint uses flat configuration. Required checks fail on warnings; local exceptions cannot reduce strictness globally. Generated-code exclusions are not created before generated code exists.

## 10. Test strategy

Python tests use the canonical root layers. This spec adds real tests for package and command behavior under `tests/unit/` and `tests/contract/`.

TypeScript unit tests colocate beside source because the Web App and Obsidian plugin have different runtimes and Vitest configurations. Cross-system tests remain in the root test hierarchy when later specs introduce them.

Required bootstrap cases:

- Import `personal_os` from the installed workspace rather than relying on the repository working directory.
- Exercise `--help`, `--version`, no-argument and invalid-argument behavior for all three Python CLIs.
- Prove CLI import and invocation do not read environment variables, filesystem secrets or the network.
- Type-check and production-build the Web shell.
- Compile a valid Obsidian plugin artifact.
- Prove architecture-boundary enforcement with negative fixtures or an equivalent isolated failing-contract test.
- Prove a failing member build propagates through `poe verify`.

Tests must be independent of execution order, internet access, timezone and service containers. pytest and each TypeScript member's Vitest invocation must collect at least one required test; a zero-test run is a failure.

Python and TypeScript coverage reports are diagnostics only. No global percentage threshold is set because thin framework shells do not provide a meaningful coverage baseline. Mutation tooling and thresholds are deferred until deterministic domain behavior exists.

The `integration`, `end_to_end`, `golden` and `performance` directories remain part of the canonical hierarchy but contain no placeholder tests that automatically pass.

## 11. GitHub Actions design

The repository uses GitHub, so `.github/workflows/quality.yml` runs for pull requests and pushes to `master`.

### 11.1 Ubuntu quality job

```text
checkout
→ verify pinned toolchain
→ uv sync --frozen
→ pnpm install --frozen-lockfile
→ uv run poe verify
→ retain diagnostic coverage/build output when configured
```

### 11.2 Windows portability job

```text
checkout
→ verify pinned toolchain
→ frozen installs
→ format/lint/type/boundary checks
→ unit and bootstrap contract tests
→ build all five composition roots
```

### 11.3 Workflow security and determinism

- Workflow permissions are `contents: read` unless a narrower permission is possible.
- Every third-party GitHub Action is pinned by full commit SHA; a human-readable tag may appear only in a comment.
- Pull-request jobs receive no repository secrets.
- `pull_request_target` is forbidden.
- Jobs do not download unpinned or unchecked executables through remote shell scripts.
- Cache keys bind operating system, runtime versions and lockfile hashes.
- Only package-manager download stores are cached; `.venv` and `node_modules` are not cached.
- Cache misses cannot alter correctness.
- A concurrency group cancels superseded runs for the same branch or pull request.
- Every job has a finite timeout.
- CI does not run Docker services, live providers, deployment or publishing.
- Pull-request jobs do not create release artifacts.

## 12. Failure behavior

- Toolchain or package incompatibility blocks completion. Strictness and required gates are not weakened as a workaround.
- Windows/Linux differences are fixed in shared commands or configuration. Platform-specific quality contracts are forbidden.
- If a framework shell cannot build with the approved baseline, the version decision must be revised explicitly with release-note, lockfile and compatibility evidence.
- A required check that is skipped, warns without failing or collects zero tests is not a passing gate.
- Lockfile drift, undeclared imports and architecture-boundary violations are terminal CI failures.

## 13. Acceptance criteria

The spec is complete only when all of the following pass on the same final commit:

1. A fresh clone installs on Ubuntu and Windows using frozen lockfiles.
2. `uv run poe verify` executes every required gate and exits `0`.
3. `personal_os` imports from the installed workspace.
4. All three Python CLIs satisfy help, version, no-argument and invalid-argument contracts.
5. The Web App produces a production build without secrets or external services.
6. The Obsidian plugin compiles the minimum valid loader artifacts without product behavior.
7. Python mypy strict and TypeScript strict checks pass.
8. Architecture-boundary checks include negative proof that a prohibited import fails.
9. pytest and Vitest cannot silently pass with zero tests.
10. No dependency is floating, undeclared or sourced from an unpinned Git branch.
11. Every third-party GitHub Action is pinned by full commit SHA.
12. No behavior owned by a later spec is represented by a placeholder implementation.
13. Lockfiles are committed, reproducible and unchanged after a second frozen install.
14. `git diff --check` passes and only intended files are changed.

## 14. Expected deliverables

```text
pyproject.toml
uv.lock
.python-version
package.json
pnpm-workspace.yaml
pnpm-lock.yaml
.node-version
src/personal_os/
apps/api/
apps/mcp/
apps/worker/
apps/web/
apps/obsidian-plugin/
tests/
.github/workflows/quality.yml
```

The root README documents prerequisites, frozen installation and quality commands. Each composition root documents the shell it provides and the explicitly absent product behavior. Configuration and secret templates are deferred to the next design spec.
