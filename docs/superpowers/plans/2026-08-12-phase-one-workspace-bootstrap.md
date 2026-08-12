# Phase One Workspace Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build reproducible Python and TypeScript workspaces with five executable composition-root shells, strict local quality gates and cross-platform GitHub Actions, without introducing product behavior from later Phase 1 specs.

**Architecture:** Keep `src/personal_os/` as the root Python distribution, add three isolated uv application members for API/MCP/worker, and add two isolated pnpm members for Web and Obsidian. Poe the Poet is the single cross-platform command surface; Import Linter, ESLint and contract tests make the dependency boundaries executable.

**Tech Stack:** CPython 3.14.6, uv 0.11.32, Ruff 0.15.22, mypy 2.3.0, pytest 9.1.1, Node.js 24.18.0 LTS, pnpm 10.34.0, TypeScript 6.0.3, ESLint 10.8.1, Vitest 4.1.10, Next.js 16.3.0, React 19.2.8, Obsidian API 1.13.1 and esbuild 0.28.2.

## Global Constraints

- Use the standard CPython `3.14.6` build; do not use the free-threaded build.
- Require uv `0.11.32`, Node.js `24.18.0` and pnpm `10.34.0` exactly.
- Exact-pin every direct Python and TypeScript dependency and commit one `uv.lock` plus one `pnpm-lock.yaml`.
- Use `uv sync --all-packages --frozen` and `pnpm install --frozen-lockfile` after lock generation; CI must never rewrite either lockfile.
- Keep the canonical package root `src/personal_os/`; do not create empty future-domain packages.
- Keep API, MCP and worker as thin Python composition roots that depend on `personal_os` but never on one another.
- Keep Web and Obsidian as separate pnpm members with no shared TypeScript package and no cross-imports.
- Python is fully typed and passes mypy strict; TypeScript enables `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride` and `useUnknownInCatchVariables`.
- Do not add FastAPI, MCP SDK, Temporal SDK, SQLAlchemy, database drivers, provider SDKs, Testing Library, MSW, Playwright or mutation-testing tools.
- Do not read settings, environment secrets, secret files or network resources while importing or invoking bootstrap help.
- Do not add API routes, MCP tools, workflows, authentication, product UI, Obsidian commands/events/Vault access, service containers or deployment behavior.
- Coverage is diagnostic only; do not add a global percentage threshold or `passWithNoTests` behavior.
- Every third-party GitHub Action is pinned to a full 40-character commit SHA; workflows use least privilege, finite timeouts and no secrets.
- Follow failing-test → minimal implementation → focused verification → commit for each task.

---

## Preflight

Before Task 1, use `superpowers:using-git-worktrees` to create an isolated worktree. From that worktree run:

```powershell
git status --short
uv --version
node --version
pnpm --version
```

Expected: clean status, `uv 0.11.32`, `v24.18.0` and `10.34.0`. Install the approved tool versions outside the repository if any value differs; do not weaken the version checks in repository configuration.

## File Map

### Repository and Python workspace

- `.editorconfig`: shared whitespace, newline and indentation rules.
- `.gitattributes`: stable LF text normalization across Windows and Linux.
- `.gitignore`: generated Python, Node, coverage and build outputs only.
- `.python-version`: exact standard CPython version.
- `pyproject.toml`: root distribution, uv workspace, exact Python tools, Ruff/mypy/pytest/coverage/Import Linter/Poe configuration.
- `uv.lock`: one resolved Python workspace graph.
- `src/personal_os/__init__.py`: intentionally side-effect-free package root.
- `src/personal_os/package_metadata.py`: one function for reading the installed distribution version.
- `src/personal_os/command_shell.py`: transport-free implementation of the shared bootstrap CLI contract.
- `src/personal_os/py.typed`: PEP 561 marker.
- `apps/api/pyproject.toml`, `apps/mcp/pyproject.toml`, `apps/worker/pyproject.toml`: isolated member metadata and console entry points.
- `apps/api/src/api_runtime/command.py`: API shell wrapper.
- `apps/mcp/src/mcp_runtime/command.py`: MCP shell wrapper.
- `apps/worker/src/workflow_worker/command.py`: worker shell wrapper.

### TypeScript workspace

- `.node-version`: exact Node.js version.
- `package.json`: private pnpm workspace command surface and exact package-manager declaration.
- `pnpm-workspace.yaml`: Web/Obsidian membership and reviewed build-script allowlist.
- `pnpm-lock.yaml`: one resolved TypeScript workspace graph.
- `tsconfig.base.json`: shared strict compiler defaults; this is configuration, not a shared runtime package.
- `apps/web/*`: Next.js static shell, strict config, lint config and colocated Vitest test.
- `apps/obsidian-plugin/*`: valid plugin manifest, empty lifecycle shell, deterministic esbuild script and colocated Vitest tests.

### Quality, CI and documentation

- `.importlinter`: Python root-package and forbidden/independence contracts.
- `tools/check_toolchain_versions.py`: cross-platform exact runtime/tool version check used by CI.
- `tests/unit/`: deterministic Python unit tests for shared bootstrap code.
- `tests/contract/`: installed-package, CLI, pins, boundaries, CI security and orchestration contracts.
- `tests/fixtures/architecture/`: isolated intentionally-invalid import graph for negative proof.
- `tests/fixtures/quality/`: isolated intentionally-failing Poe sequence for failure propagation proof.
- `.github/workflows/quality.yml`: Ubuntu and Windows quality matrix.
- `README.md` and per-app README files: prerequisites, commands and explicit exclusions.
- `tests/integration/README.md`, `tests/end_to_end/README.md`, `tests/golden/README.md`, `tests/performance/README.md`: reserve canonical test layers without fake passing tests.

---

### Task 1: Reproducible Root Python Distribution

**Files:**

- Create: `.editorconfig`
- Create: `.gitattributes`
- Create: `.gitignore`
- Create: `.python-version`
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `src/personal_os/__init__.py`
- Create: `src/personal_os/package_metadata.py`
- Create: `src/personal_os/py.typed`
- Create: `tests/contract/test_python_workspace.py`

**Interfaces:**

- Consumes: no application code; only the exact global toolchain.
- Produces: import package `personal_os`, distribution `knowledge-core==0.1.0`, and `distribution_version() -> str` for every Python shell.

- [ ] **Step 1: Write the failing installed-workspace contract**

```python
# tests/contract/test_python_workspace.py
from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
import subprocess
import sys

from personal_os.package_metadata import distribution_version


def test_distribution_version_comes_from_installed_metadata() -> None:
    assert distribution_version() == version("knowledge-core") == "0.1.0"


def test_import_succeeds_outside_repository(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import personal_os; print(personal_os.__name__)"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "personal_os"
```

- [ ] **Step 2: Run the test and verify the package is absent**

Run:

```powershell
uvx --from pytest==9.1.1 pytest tests/contract/test_python_workspace.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'personal_os'`.

- [ ] **Step 3: Add root metadata and strict Python configuration**

Create `.python-version` containing exactly `3.14.6`. Configure `pyproject.toml` with this core content:

```toml
[project]
name = "knowledge-core"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = []

[build-system]
requires = ["uv_build==0.11.32"]
build-backend = "uv_build"

[dependency-groups]
dev = [
  "import-linter==2.13",
  "mypy==2.3.0",
  "poethepoet==0.48.0",
  "pytest==9.1.1",
  "pytest-cov==7.1.0",
  "ruff==0.15.22",
]

[tool.uv]
required-version = "==0.11.32"

[tool.pytest.ini_options]
addopts = ["--strict-config", "--strict-markers"]
testpaths = ["tests"]

[tool.ruff]
target-version = "py314"
line-length = 100
src = ["src", "apps", "tests", "tools"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.mypy]
python_version = "3.14"
strict = true
files = ["src", "apps/api/src", "apps/mcp/src", "apps/worker/src", "tools"]

[tool.coverage.run]
branch = true
source = ["personal_os", "api_runtime", "mcp_runtime", "workflow_worker"]

[tool.coverage.report]
show_missing = true
skip_covered = false
```

Create stable repository text rules:

```ini
# .editorconfig
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 2

[*.py]
indent_size = 4
```

```gitattributes
* text=auto eol=lf
```

Ignore only generated state: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage`, `coverage/`, `htmlcov/`, `dist/`, `node_modules/`, `.next/`, and `apps/obsidian-plugin/dist/`.

- [ ] **Step 4: Implement the side-effect-free package and version function**

```python
# src/personal_os/__init__.py
"""Canonical application package."""
```

```python
# src/personal_os/package_metadata.py
"""Installed distribution metadata for composition-root shells."""

from importlib.metadata import version
from typing import Final

DISTRIBUTION_NAME: Final = "knowledge-core"


def distribution_version() -> str:
    return version(DISTRIBUTION_NAME)
```

Create an empty `src/personal_os/py.typed` marker. Do not add domain directories.

- [ ] **Step 5: Lock, install and verify the root package**

Run:

```powershell
uv lock
uv sync --frozen
uv run pytest tests/contract/test_python_workspace.py -q
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv build --clear
```

Expected: all checks pass and `dist/` contains the `knowledge_core-0.1.0` wheel and source distribution.

- [ ] **Step 6: Commit the root Python distribution**

```powershell
git add .editorconfig .gitattributes .gitignore .python-version pyproject.toml uv.lock src tests/contract/test_python_workspace.py
git commit -m "build: bootstrap root python distribution"
```

---

### Task 2: API, MCP and Worker Command Shells

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/personal_os/command_shell.py`
- Create: `apps/api/pyproject.toml`
- Create: `apps/api/src/api_runtime/__init__.py`
- Create: `apps/api/src/api_runtime/command.py`
- Create: `apps/mcp/pyproject.toml`
- Create: `apps/mcp/src/mcp_runtime/__init__.py`
- Create: `apps/mcp/src/mcp_runtime/command.py`
- Create: `apps/worker/pyproject.toml`
- Create: `apps/worker/src/workflow_worker/__init__.py`
- Create: `apps/worker/src/workflow_worker/command.py`
- Create: `tests/unit/test_command_shell.py`
- Create: `tests/contract/test_process_commands.py`
- Create: `tests/contract/test_command_import_side_effects.py`

**Interfaces:**

- Consumes: `personal_os.package_metadata.distribution_version() -> str` from Task 1.
- Produces: `CommandIdentity`, `run_bootstrap_command(identity, argv) -> int`, and console commands `personal-api`, `personal-mcp`, `personal-worker`.

- [ ] **Step 1: Write failing command behavior tests**

```python
# tests/unit/test_command_shell.py
from __future__ import annotations

import pytest

from personal_os.command_shell import CommandIdentity, run_bootstrap_command

IDENTITY = CommandIdentity(program_name="personal-api", process_description="API process shell")


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_bootstrap_command(IDENTITY, []) == 0
    assert "usage: personal-api" in capsys.readouterr().out


def test_version_uses_distribution_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_bootstrap_command(IDENTITY, ["--version"]) == 0
    assert capsys.readouterr().out.strip() == "personal-api 0.1.0"


def test_invalid_argument_exits_two_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        run_bootstrap_command(IDENTITY, ["--invalid"])
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "unrecognized arguments" in captured.err
    assert "Traceback" not in captured.err
```

In `tests/contract/test_process_commands.py`, parameterize these exact triples:

```python
COMMANDS = (
    ("personal-api", "api_runtime.command", "API process shell"),
    ("personal-mcp", "mcp_runtime.command", "MCP process shell"),
    ("personal-worker", "workflow_worker.command", "Temporal worker process shell"),
)
```

For every triple, use `subprocess.run([sys.executable, "-m", module_name, *arguments], cwd=tmp_path, ...)` to assert no args and `--help` exit `0`, `--version` prints `<command> 0.1.0`, and `--invalid` exits `2` without `Traceback`. Also assert the installed `console_scripts` entry point maps to the declared module.

- [ ] **Step 2: Run the focused tests and verify missing shells**

Run:

```powershell
uv run pytest tests/unit/test_command_shell.py tests/contract/test_process_commands.py -q
```

Expected: collection fails because `personal_os.command_shell` and the three runtime packages do not exist.

- [ ] **Step 3: Add the uv members and exact workspace dependencies**

Add to root `pyproject.toml`:

```toml
[tool.uv.workspace]
members = ["apps/api", "apps/mcp", "apps/worker"]

[tool.uv.sources]
knowledge-core = { workspace = true }
```

Create the member manifests with no dependency except the root distribution:

```toml
# apps/api/pyproject.toml
[project]
name = "api-runtime"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = ["knowledge-core==0.1.0"]

[project.scripts]
personal-api = "api_runtime.command:main"

[build-system]
requires = ["uv_build==0.11.32"]
build-backend = "uv_build"
```

```toml
# apps/mcp/pyproject.toml
[project]
name = "mcp-runtime"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = ["knowledge-core==0.1.0"]

[project.scripts]
personal-mcp = "mcp_runtime.command:main"

[build-system]
requires = ["uv_build==0.11.32"]
build-backend = "uv_build"
```

```toml
# apps/worker/pyproject.toml
[project]
name = "workflow-worker"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = ["knowledge-core==0.1.0"]

[project.scripts]
personal-worker = "workflow_worker.command:main"

[build-system]
requires = ["uv_build==0.11.32"]
build-backend = "uv_build"
```

- [ ] **Step 4: Implement the shared command contract**

```python
# src/personal_os/command_shell.py
from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Sequence
from dataclasses import dataclass

from personal_os.package_metadata import distribution_version


@dataclass(frozen=True, slots=True)
class CommandIdentity:
    program_name: str
    process_description: str


def run_bootstrap_command(identity: CommandIdentity, argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(prog=identity.program_name, description=identity.process_description)
    parser.add_argument("--version", dest="should_show_version", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.should_show_version:
        print(f"{identity.program_name} {distribution_version()}")
        return 0
    parser.print_help()
    return 0
```

- [ ] **Step 5: Implement all three role-bearing wrappers**

```python
# apps/api/src/api_runtime/command.py
from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

from personal_os.command_shell import CommandIdentity, run_bootstrap_command

IDENTITY = CommandIdentity("personal-api", "API process shell")


def run(argv: Sequence[str] | None = None) -> int:
    return run_bootstrap_command(IDENTITY, argv)


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
```

```python
# apps/mcp/src/mcp_runtime/command.py
from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

from personal_os.command_shell import CommandIdentity, run_bootstrap_command

IDENTITY = CommandIdentity("personal-mcp", "MCP process shell")


def run(argv: Sequence[str] | None = None) -> int:
    return run_bootstrap_command(IDENTITY, argv)


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
```

```python
# apps/worker/src/workflow_worker/command.py
from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

from personal_os.command_shell import CommandIdentity, run_bootstrap_command

IDENTITY = CommandIdentity("personal-worker", "Temporal worker process shell")


def run(argv: Sequence[str] | None = None) -> int:
    return run_bootstrap_command(IDENTITY, argv)


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
```

Each package `__init__.py` contains only a role-specific module docstring.

- [ ] **Step 6: Prove command imports do not access secrets or the network**

In `tests/contract/test_command_import_side_effects.py`, remove each command module from `sys.modules`, monkeypatch `os.getenv`, `Path.read_text` and `socket.create_connection` to raise `AssertionError`, then import the module and call `run(["--help"])`. Assert exit `0`. Add an AST assertion that each wrapper imports only `collections.abc`, `typing` and `personal_os.command_shell`; this also excludes direct framework/provider imports and environment access.

Run:

```powershell
uv lock
uv sync --all-packages --frozen
uv run pytest tests/unit/test_command_shell.py tests/contract/test_process_commands.py tests/contract/test_command_import_side_effects.py -q
uv run mypy src apps/api/src apps/mcp/src apps/worker/src
uv build --all-packages --clear
```

Expected: all tests and builds pass; no FastAPI, MCP or Temporal package appears in `uv.lock`.

- [ ] **Step 7: Commit the Python composition roots**

```powershell
git add pyproject.toml uv.lock src/personal_os/command_shell.py apps/api apps/mcp apps/worker tests/unit tests/contract
git commit -m "feat: add python composition root shells"
```

---

### Task 3: Strict Next.js Web Shell

**Files:**

- Create: `.node-version`
- Create: `package.json`
- Create: `pnpm-workspace.yaml`
- Create: `pnpm-lock.yaml`
- Create: `tsconfig.base.json`
- Create: `apps/web/package.json`
- Create: `apps/web/next.config.ts`
- Create: `apps/web/next-env.d.ts`
- Create: `apps/web/tsconfig.json`
- Create: `apps/web/eslint.config.mjs`
- Create: `apps/web/vitest.config.ts`
- Create: `apps/web/src/app/bootstrap-copy.ts`
- Create: `apps/web/src/app/bootstrap-copy.test.ts`
- Create: `apps/web/src/app/layout.tsx`
- Create: `apps/web/src/app/page.tsx`

**Interfaces:**

- Consumes: Node.js/pnpm pins only; no Python or backend runtime.
- Produces: pnpm member `@workspace/web-runtime` with `format`, `format:check`, `lint`, `type-check`, `test` and `build` scripts.

- [ ] **Step 1: Create the workspace manifest and failing Web test**

Create `.node-version` with `24.18.0` and this root manifest:

```json
{
  "name": "knowledge-workspace-monorepo",
  "version": "0.1.0",
  "private": true,
  "packageManager": "pnpm@10.34.0",
  "engines": {
    "node": ">=24.18.0 <25",
    "pnpm": "10.34.0"
  }
}
```

Create `pnpm-workspace.yaml`:

```yaml
packages:
  - apps/web
  - apps/obsidian-plugin
```

Create `apps/web/package.json` with exact versions:

```json
{
  "name": "@workspace/web-runtime",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "build": "next build",
    "format": "eslint . --fix",
    "format:check": "eslint . --max-warnings=0",
    "lint": "eslint . --max-warnings=0",
    "test": "vitest run --coverage",
    "type-check": "tsc --noEmit"
  },
  "dependencies": {
    "next": "16.3.0",
    "react": "19.2.8",
    "react-dom": "19.2.8"
  },
  "devDependencies": {
    "@types/node": "24.13.3",
    "@types/react": "19.2.18",
    "@types/react-dom": "19.2.4",
    "@vitest/coverage-v8": "4.1.10",
    "eslint": "9.39.5",
    "eslint-config-next": "16.3.0",
    "typescript": "6.0.3",
    "typescript-eslint": "8.67.0",
    "vitest": "4.1.10"
  }
}
```

Write `bootstrap-copy.test.ts` first:

```typescript
import { describe, expect, it } from "vitest";

import { WORKSPACE_SHELL_HEADING } from "./bootstrap-copy";

describe("Web workspace shell", () => {
  it("identifies the bootstrap shell without product navigation", () => {
    expect(WORKSPACE_SHELL_HEADING).toBe("Workspace bootstrap ready");
  });
});
```

- [ ] **Step 2: Install and verify the missing Web module failure**

Run:

```powershell
pnpm install
pnpm --filter @workspace/web-runtime test
```

Expected: Vitest collects the test and fails because `./bootstrap-copy` does not exist.

- [ ] **Step 3: Add strict shared and Web TypeScript configurations**

Create the root compiler contract:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "useUnknownInCatchVariables": true,
    "forceConsistentCasingInFileNames": true,
    "verbatimModuleSyntax": true,
    "isolatedModules": true,
    "skipLibCheck": true,
    "noEmit": true
  }
}
```

Create the Web compiler config without a path alias:

```json
{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "lib": ["DOM", "DOM.Iterable", "ES2022"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "preserve",
    "plugins": [{ "name": "next" }]
  },
  "include": ["next-env.d.ts", ".next/types/**/*.ts", "src/**/*.ts", "src/**/*.tsx"],
  "exclude": ["node_modules", "coverage"]
}
```

Configure Vitest explicitly:

```typescript
// apps/web/vitest.config.ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    passWithNoTests: false,
    coverage: { provider: "v8", reporter: ["text", "json-summary"] },
  },
});
```

Configure flat ESLint from `eslint-config-next/core-web-vitals`, ignore only generated `.next/`, `coverage/` and `next-env.d.ts`, and add this rule for all TypeScript source:

```javascript
"no-restricted-imports": [
  "error",
  {
    patterns: [
      { group: ["@workspace/obsidian-plugin", "**/obsidian-plugin/**"] },
    ],
  },
]
```

- [ ] **Step 4: Implement the static Web shell**

```typescript
// apps/web/src/app/bootstrap-copy.ts
export const WORKSPACE_SHELL_HEADING = "Workspace bootstrap ready";
```

```tsx
// apps/web/src/app/page.tsx
import { WORKSPACE_SHELL_HEADING } from "./bootstrap-copy";

export default function WorkspaceBootstrapPage() {
  return (
    <main>
      <h1>{WORKSPACE_SHELL_HEADING}</h1>
      <p>Application services are not configured in this bootstrap.</p>
    </main>
  );
}
```

Create a minimal typed root layout with static metadata. Do not add routes, server actions, authentication, API clients, CSS frameworks or product navigation. `next.config.ts` must export an empty typed Next configuration and must not read environment variables.

- [ ] **Step 5: Verify test collection, strictness and production build**

Run:

```powershell
pnpm --filter @workspace/web-runtime test
pnpm --filter @workspace/web-runtime type-check
pnpm --filter @workspace/web-runtime lint
$env:NEXT_TELEMETRY_DISABLED = '1'
pnpm --filter @workspace/web-runtime build
Remove-Item Env:NEXT_TELEMETRY_DISABLED
```

Expected: one or more Vitest tests run, TypeScript and ESLint pass with zero warnings, and `.next/` is produced without a service or secret.

- [ ] **Step 6: Confirm the lockfile is frozen and reproducible**

Run:

```powershell
pnpm install --frozen-lockfile
git diff --exit-code -- pnpm-lock.yaml
```

Expected: no lockfile change.

- [ ] **Step 7: Commit the Web shell**

```powershell
git add .node-version package.json pnpm-workspace.yaml pnpm-lock.yaml tsconfig.base.json apps/web
git commit -m "feat: add strict web composition shell"
```

---

### Task 4: Side-Effect-Free Obsidian Plugin Shell

**Files:**

- Modify: `pnpm-workspace.yaml`
- Modify: `pnpm-lock.yaml`
- Create: `apps/obsidian-plugin/package.json`
- Create: `apps/obsidian-plugin/manifest.json`
- Create: `apps/obsidian-plugin/tsconfig.json`
- Create: `apps/obsidian-plugin/eslint.config.mjs`
- Create: `apps/obsidian-plugin/vitest.config.ts`
- Create: `apps/obsidian-plugin/scripts/build-plugin.mjs`
- Create: `apps/obsidian-plugin/src/plugin.ts`
- Create: `apps/obsidian-plugin/src/plugin.test.ts`

**Interfaces:**

- Consumes: root strict TypeScript configuration and pnpm workspace from Task 3.
- Produces: pnpm member `@workspace/obsidian-plugin` and `dist/main.js` plus `dist/manifest.json` only.

- [ ] **Step 1: Add the member manifest and failing lifecycle contract**

Create the member manifest:

```json
{
  "name": "@workspace/obsidian-plugin",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "build": "node scripts/build-plugin.mjs",
    "format": "eslint . --fix",
    "format:check": "eslint . --max-warnings=0",
    "lint": "eslint . --max-warnings=0",
    "test": "vitest run --coverage",
    "type-check": "tsc --noEmit"
  },
  "devDependencies": {
    "@types/node": "24.13.3",
    "@vitest/coverage-v8": "4.1.10",
    "esbuild": "0.28.2",
    "eslint": "9.39.5",
    "obsidian": "1.13.1",
    "typescript": "6.0.3",
    "typescript-eslint": "8.67.0",
    "vitest": "4.1.10"
  }
}
```

Write the lifecycle test using the TypeScript compiler API so it does not import the Obsidian runtime:

```typescript
// apps/obsidian-plugin/src/plugin.test.ts
import { readFileSync } from "node:fs";
import * as ts from "typescript";
import { describe, expect, it } from "vitest";

const pluginPath = new URL("./plugin.ts", import.meta.url);
const pluginSource = readFileSync(pluginPath, "utf8");
const sourceFile = ts.createSourceFile("plugin.ts", pluginSource, ts.ScriptTarget.Latest, true);

describe("Obsidian bootstrap lifecycle", () => {
  it("contains only empty load and unload methods", () => {
    const pluginClass = sourceFile.statements.find(ts.isClassDeclaration);
    expect(pluginClass).toBeDefined();
    const methods = pluginClass?.members.filter(ts.isMethodDeclaration) ?? [];
    const methodNames = methods.map((method) => method.name.getText(sourceFile));
    expect(methodNames).toEqual(["onload", "onunload"]);
    expect(methods.every((method) => method.body?.statements.length === 0)).toBe(true);
  });

  it("does not register product behavior or access runtime data", () => {
    for (const forbiddenText of [
      "addCommand",
      "addRibbonIcon",
      "registerEvent",
      ".vault",
      "requestUrl",
      "fetch(",
      "process.env",
    ]) {
      expect(pluginSource).not.toContain(forbiddenText);
    }
  });
});
```

- [ ] **Step 2: Run the test and verify the plugin entry is absent**

Run:

```powershell
pnpm install
pnpm --filter @workspace/obsidian-plugin test
```

Expected: the test is collected and fails because `src/plugin.ts` is absent.

- [ ] **Step 3: Add strict plugin configuration and manifest**

Create `manifest.json` with exact values:

```json
{
  "id": "knowledge-workspace",
  "name": "Knowledge Workspace",
  "version": "0.1.0",
  "minAppVersion": "1.13.1",
  "description": "Bootstrap shell for the private knowledge workspace.",
  "author": "Workspace owner",
  "isDesktopOnly": false
}
```

The plugin `tsconfig.json` extends `../../tsconfig.base.json`, uses `module: "ESNext"`, `moduleResolution: "Bundler"`, `lib: ["DOM", "ES2022"]`, and includes only `src/**/*.ts`. Vitest uses Node, includes `src/**/*.test.ts`, enables V8 coverage and keeps `passWithNoTests: false`. Flat ESLint uses `typescript-eslint` strict configuration, sets `@typescript-eslint/no-empty-function` to `error` with only `overrideMethods` allowed, and forbids imports matching `@workspace/web-runtime` or any path containing `apps/web`. This is the sole narrow exception required by the intentional empty lifecycle; do not add a file-level disable.

- [ ] **Step 4: Implement the intentional empty lifecycle**

```typescript
// apps/obsidian-plugin/src/plugin.ts
import { Plugin } from "obsidian";

export default class KnowledgeWorkspacePlugin extends Plugin {
  override async onload(): Promise<void> {}

  override onunload(): void {}
}
```

This is the complete Phase 1 plugin behavior. Do not register commands, events, views, ribbon icons or Vault access.

- [ ] **Step 5: Add deterministic production bundling**

Create the deterministic build script:

```javascript
// apps/obsidian-plugin/scripts/build-plugin.mjs
import { copyFile, mkdir, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";

const packageDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outputDirectory = path.join(packageDirectory, "dist");

await rm(outputDirectory, { recursive: true, force: true });
await mkdir(outputDirectory, { recursive: true });
await build({
  entryPoints: [path.join(packageDirectory, "src", "plugin.ts")],
  bundle: true,
  external: ["obsidian"],
  format: "cjs",
  platform: "browser",
  target: "es2022",
  sourcemap: false,
  outfile: path.join(outputDirectory, "main.js"),
});
await copyFile(
  path.join(packageDirectory, "manifest.json"),
  path.join(outputDirectory, "manifest.json"),
);
```

It removes only the generated plugin `dist` directory and does not load environment variables or emit test/source files.

Add the only approved lifecycle-script allowlist to `pnpm-workspace.yaml`:

```yaml
onlyBuiltDependencies:
  - esbuild
```

- [ ] **Step 6: Verify the plugin contract and artifact inventory**

Run:

```powershell
pnpm install --frozen-lockfile
pnpm --filter @workspace/obsidian-plugin test
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
pnpm --filter @workspace/obsidian-plugin build
Get-ChildItem apps/obsidian-plugin/dist -File | Select-Object -ExpandProperty Name | Sort-Object
```

Expected artifact list, and no other file:

```text
main.js
manifest.json
```

- [ ] **Step 7: Commit the Obsidian shell**

```powershell
git add pnpm-workspace.yaml pnpm-lock.yaml apps/obsidian-plugin
git commit -m "feat: add obsidian composition shell"
```

---

### Task 5: Executable Architecture and Quality Gates

**Files:**

- Modify: `pyproject.toml`
- Modify: `package.json`
- Modify: `apps/web/eslint.config.mjs`
- Modify: `apps/obsidian-plugin/eslint.config.mjs`
- Create: `.importlinter`
- Create: `tests/contract/test_architecture_boundaries.py`
- Create: `tests/contract/test_dependency_pins.py`
- Create: `tests/contract/test_quality_orchestration.py`
- Create: `tests/fixtures/architecture/invalid_python/.importlinter`
- Create: `tests/fixtures/architecture/invalid_python/source_package/__init__.py`
- Create: `tests/fixtures/architecture/invalid_python/forbidden_package/__init__.py`
- Create: `tests/fixtures/quality/failing_pipeline/pyproject.toml`

**Interfaces:**

- Consumes: all five member command surfaces from Tasks 2–4.
- Produces: the eight required Poe commands and executable positive/negative dependency-boundary evidence.

- [ ] **Step 1: Write failing pin and architecture contracts**

`test_dependency_pins.py` must parse every `pyproject.toml` with `tomllib` and every `package.json` with `json`. Assert registry dependencies use only `name==x.y.z` for Python or bare `x.y.z` for npm; reject `*`, ranges, prereleases, Git URLs and branch references. Explicitly allow only `{ workspace = true }` sources and assert the only pnpm `onlyBuiltDependencies` value is `esbuild`.

`test_architecture_boundaries.py` must:

1. run `lint-imports` against the repository and expect exit `0`;
2. run `lint-imports --config <invalid fixture>`, with the fixture directory prepended to `PYTHONPATH`, and expect nonzero plus a broken-contract message;
3. scan Python AST for `sys.path` mutation;
4. scan TypeScript imports and reject Web↔Obsidian paths or path aliases outside each member.

- [ ] **Step 2: Run focused contracts and capture the expected failures**

Run:

```powershell
uv run pytest tests/contract/test_dependency_pins.py tests/contract/test_architecture_boundaries.py -q
```

Expected: failure because `.importlinter`, the negative fixture and the final quality task graph do not yet exist.

- [ ] **Step 3: Define positive Python import contracts**

Create `.importlinter`:

```ini
[importlinter]
root_packages =
    personal_os
    api_runtime
    mcp_runtime
    workflow_worker
include_external_packages = True

[importlinter:contract:domain-does-not-import-composition-or-infrastructure]
name = Core package does not import composition roots or infrastructure SDKs
type = forbidden
source_modules =
    personal_os
forbidden_modules =
    api_runtime
    mcp_runtime
    workflow_worker
    fastapi
    sqlalchemy
    temporalio
    mcp
    qdrant_client
    neo4j
    redis
    boto3

[importlinter:contract:composition-roots-are-independent]
name = Python composition roots do not import one another
type = independence
modules =
    api_runtime
    mcp_runtime
    workflow_worker
```

The invalid fixture declares `source_package` and `forbidden_package` as roots, makes `source_package/__init__.py` import `forbidden_package`, and defines a forbidden contract from source to forbidden. It exists only under `tests/fixtures` and is excluded from normal mypy/import-linter roots.

- [ ] **Step 4: Define root pnpm member commands**

Add these root scripts:

```json
{
  "scripts": {
    "build": "pnpm --recursive --if-present run build",
    "format": "pnpm --recursive --if-present run format",
    "format:check": "pnpm --recursive --if-present run format:check",
    "lint": "pnpm --recursive --if-present run lint",
    "test": "pnpm --recursive --if-present run test",
    "type-check": "pnpm --recursive --if-present run type-check"
  }
}
```

Keep both member ESLint `no-restricted-imports` rules fail-closed and use `--max-warnings=0`. Do not add blanket ignores.

- [ ] **Step 5: Define the complete Poe command graph**

Add private subtasks plus these public task sequences to `pyproject.toml`:

```toml
[tool.poe.tasks.python-format]
cmd = "ruff format src apps tests tools"

[tool.poe.tasks.python-format-check]
cmd = "ruff format --check src apps tests tools"

[tool.poe.tasks.python-lint]
cmd = "ruff check src apps tests tools"

[tool.poe.tasks.typescript-format]
cmd = "pnpm run format"

[tool.poe.tasks.typescript-format-check]
cmd = "pnpm run format:check"

[tool.poe.tasks.typescript-lint]
cmd = "pnpm run lint"

[tool.poe.tasks.python-type-check]
cmd = "mypy src apps/api/src apps/mcp/src apps/worker/src tools"

[tool.poe.tasks.typescript-type-check]
cmd = "pnpm run type-check"

[tool.poe.tasks.python-test]
cmd = "pytest --cov --cov-report=term-missing"

[tool.poe.tasks.typescript-test]
cmd = "pnpm run test"

[tool.poe.tasks.import-boundaries]
cmd = "lint-imports"

[tool.poe.tasks.boundary-contract-tests]
cmd = "pytest tests/contract/test_architecture_boundaries.py -q"

[tool.poe.tasks.python-build]
cmd = "uv build --all-packages --clear"

[tool.poe.tasks.typescript-build]
cmd = "pnpm run build"

[tool.poe.tasks.format]
sequence = ["python-format", "typescript-format"]
default_item_type = "ref"

[tool.poe.tasks.format-check]
sequence = ["python-format-check", "typescript-format-check"]
default_item_type = "ref"

[tool.poe.tasks.lint]
sequence = ["python-lint", "typescript-lint"]
default_item_type = "ref"

[tool.poe.tasks.type-check]
sequence = ["python-type-check", "typescript-type-check"]
default_item_type = "ref"

[tool.poe.tasks.test]
sequence = ["python-test", "typescript-test"]
default_item_type = "ref"

[tool.poe.tasks.boundary-check]
sequence = ["import-boundaries", "boundary-contract-tests"]
default_item_type = "ref"

[tool.poe.tasks.build]
sequence = ["python-build", "typescript-build"]
default_item_type = "ref"

[tool.poe.tasks.verify]
sequence = ["format-check", "lint", "type-check", "boundary-check", "test", "build"]
default_item_type = "ref"
```

Set `NEXT_TELEMETRY_DISABLED = "1"` only on the TypeScript build task using Poe task environment configuration; do not add application settings or secret templates.

- [ ] **Step 6: Add an isolated failure-propagation proof**

The `tests/fixtures/quality/failing_pipeline/pyproject.toml` fixture defines six successful Python one-line tasks followed by a `build` task that exits `23`; its `verify` sequence uses the same six public gate names and order as the real configuration. `test_quality_orchestration.py` parses the real TOML to assert exact order, then runs:

```python
completed = subprocess.run(
    ["poe", "-C", str(fixture_directory), "verify"],
    check=False,
    capture_output=True,
    text=True,
)
assert completed.returncode != 0
assert "build" in completed.stdout
```

Also assert no public Poe task contains `continue_on_error`, `passWithNoTests` or warning-only fallbacks.

- [ ] **Step 7: Run every local quality command**

Run:

```powershell
uv run poe format
uv run poe format-check
uv run poe lint
uv run poe type-check
uv run poe boundary-check
uv run poe test
uv run poe build
uv run poe verify
```

Expected: every command exits `0`; pytest and both Vitest members report at least one collected test; coverage is printed without a threshold.

- [ ] **Step 8: Commit the executable quality gates**

```powershell
git add pyproject.toml package.json .importlinter apps/web/eslint.config.mjs apps/obsidian-plugin/eslint.config.mjs tests
git commit -m "test: enforce workspace quality boundaries"
```

---

### Task 6: Pinned Ubuntu and Windows GitHub Actions

**Files:**

- Create: `tools/check_toolchain_versions.py`
- Create: `tests/unit/test_toolchain_versions.py`
- Create: `tests/contract/test_ci_security.py`
- Create: `.github/workflows/quality.yml`

**Interfaces:**

- Consumes: `uv run poe verify`, both frozen lockfiles and all five builds.
- Produces: one PR/push workflow with Ubuntu full-quality and Windows portability matrix entries.

- [ ] **Step 1: Write failing toolchain and workflow-security tests**

Define a typed `ToolVersionExpectation` dataclass and injectable command runner in the test contract. Test exact success values and a mismatch that returns a nonzero result with only tool name/expected/actual—never environment contents.

`test_ci_security.py` must parse `.github/workflows/quality.yml` as text and assert:

- triggers contain pull requests and pushes to `master`, but not `pull_request_target`;
- top-level permissions are exactly `contents: read`;
- every non-local `uses:` reference matches `@[0-9a-f]{40}`;
- both `ubuntu-latest` and `windows-latest` occur;
- a finite `timeout-minutes` and concurrency cancellation occur;
- frozen uv/pnpm install commands and `uv run --frozen poe verify` occur;
- neither `secrets.` nor Docker/service/deploy/publish commands occur.

- [ ] **Step 2: Run the focused tests and verify missing files**

Run:

```powershell
uv run pytest tests/unit/test_toolchain_versions.py tests/contract/test_ci_security.py -q
```

Expected: collection or assertions fail because the checker and workflow do not exist.

- [ ] **Step 3: Implement exact cross-platform version checking**

`tools/check_toolchain_versions.py` must run these commands without a shell:

```python
EXPECTED_OUTPUTS = {
    ("python", "--version"): "Python 3.14.6",
    ("uv", "--version"): "uv 0.11.32",
    ("node", "--version"): "v24.18.0",
    ("pnpm", "--version"): "10.34.0",
}
```

Normalize surrounding whitespace only. Report each mismatch to `stderr` and return `1`; return `0` only when all four match. Do not print paths, environment variables or unrelated command output.

- [ ] **Step 4: Add a SHA-pinned quality matrix**

Create `.github/workflows/quality.yml` with these reviewed action pins:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
- uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
- uses: pnpm/action-setup@ff378ebe6b225b0680b81c1ad4498ae0d1d3a5e3 # v6.0.10
- uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0
```

The workflow must use:

```yaml
name: quality

on:
  pull_request:
  push:
    branches: [master]

permissions:
  contents: read

concurrency:
  group: quality-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  verify:
    name: ${{ matrix.name }}
    strategy:
      fail-fast: false
      matrix:
        include:
          - name: Ubuntu quality
            operating-system: ubuntu-latest
          - name: Windows portability
            operating-system: windows-latest
    runs-on: ${{ matrix.operating-system }}
    timeout-minutes: 30
    env:
      NEXT_TELEMETRY_DISABLED: "1"
```

After checkout, setup-uv receives `version: "0.11.32"`, `python-version: "3.14.6"`, `enable-cache: true` and `cache-dependency-glob: uv.lock`. pnpm setup receives `version: "10.34.0"` and `run_install: false`. setup-node receives `node-version-file: .node-version`, `cache: pnpm` and `cache-dependency-path: pnpm-lock.yaml`.

Run steps, in order:

```text
python tools/check_toolchain_versions.py
uv sync --all-packages --frozen
pnpm install --frozen-lockfile
uv run --all-packages --frozen poe verify
git diff --exit-code -- uv.lock pnpm-lock.yaml
```

Do not add artifact upload, Docker services, deployment, publishing, repository writes or secret references.

- [ ] **Step 5: Run workflow-security and local checker tests**

Run:

```powershell
uv run pytest tests/unit/test_toolchain_versions.py tests/contract/test_ci_security.py -q
uv run ruff check tools tests/unit/test_toolchain_versions.py tests/contract/test_ci_security.py
uv run mypy tools
```

Expected: pass. Running `python tools/check_toolchain_versions.py` on a machine with a different approved tool not installed must fail clearly; this is also a deliberate local negative check.

- [ ] **Step 6: Validate the workflow syntax and full local gate**

Run:

```powershell
uv run poe verify
git diff --check
```

Push the task branch and confirm both GitHub matrix entries complete successfully on the same commit. A skipped or warning-only job is not success.

- [ ] **Step 7: Commit CI**

```powershell
git add tools tests/unit/test_toolchain_versions.py tests/contract/test_ci_security.py .github/workflows/quality.yml
git commit -m "ci: add pinned cross-platform quality gates"
```

---

### Task 7: Bootstrap Documentation and Final Acceptance

**Files:**

- Create: `README.md`
- Create: `apps/api/README.md`
- Create: `apps/mcp/README.md`
- Create: `apps/worker/README.md`
- Create: `apps/web/README.md`
- Create: `apps/obsidian-plugin/README.md`
- Create: `tests/integration/README.md`
- Create: `tests/end_to_end/README.md`
- Create: `tests/golden/README.md`
- Create: `tests/performance/README.md`

**Interfaces:**

- Consumes: all commands, artifacts and constraints implemented in Tasks 1–6.
- Produces: operator-facing bootstrap instructions and final evidence for every acceptance criterion in the approved design.

- [ ] **Step 1: Write documentation assertions before the documents**

Extend `tests/contract/test_python_workspace.py` or create `tests/contract/test_bootstrap_documentation.py` to assert the root README contains exact prerequisites `Python 3.14.6`, `uv 0.11.32`, `Node.js 24.18.0`, `pnpm 10.34.0`, both frozen install commands and all eight public Poe commands. Assert every app README names its shell and explicitly lists its absent product behavior.

Run:

```powershell
uv run pytest tests/contract/test_bootstrap_documentation.py -q
```

Expected: failure because the README files do not exist.

- [ ] **Step 2: Document bootstrap and deliberate exclusions**

The root README must provide:

1. the exact standard runtime/package-manager prerequisites;
2. `uv sync --all-packages --frozen` and `pnpm install --frozen-lockfile`;
3. all eight `uv run poe ...` commands and the verify order;
4. the three CLI help/version examples;
5. Web and plugin build locations;
6. a statement that configuration/secrets, databases, object storage, API/MCP/workflow behavior and product UI belong to later specs.

Each app README names its composition role, its build/test command and what is intentionally absent. Each reserved test-layer README states the owner and future acceptance source; it contains no executable placeholder test.

- [ ] **Step 3: Run clean-install and lockfile-replay acceptance**

From the repository root:

```powershell
uv sync --all-packages --frozen
pnpm install --frozen-lockfile
git diff --exit-code -- uv.lock pnpm-lock.yaml
uv sync --all-packages --frozen
pnpm install --frozen-lockfile
git diff --exit-code -- uv.lock pnpm-lock.yaml
```

Expected: both install passes succeed and neither lockfile changes.

- [ ] **Step 4: Run final functional and repository acceptance**

```powershell
uv run --all-packages --frozen poe verify
uv run --package api-runtime personal-api --help
uv run --package mcp-runtime personal-mcp --version
uv run --package workflow-worker personal-worker
Get-ChildItem apps/obsidian-plugin/dist -File | Select-Object -ExpandProperty Name | Sort-Object
git diff --check
git status --short
```

Expected: `poe verify` passes in the documented order; the CLIs satisfy their contracts; plugin output is exactly `main.js` and `manifest.json`; diff check passes; status contains only the intended Task 7 documentation/test changes before commit. Confirm the same final commit is green for both GitHub Actions matrix entries.

- [ ] **Step 5: Commit the completed workspace bootstrap**

```powershell
git add README.md apps tests
git commit -m "docs: document workspace bootstrap"
git status --short
git log -7 --oneline
```

Expected: clean working tree and seven focused implementation commits. Record the final GitHub Actions URLs in the handoff; do not claim Ubuntu/Windows acceptance without both job results.

---

## Realized deviations (as-built vs. this plan)

Implementation followed this plan; the following minimal, evidence-backed deviations were necessary. Each is documented in the SDD progress ledger and the pull request, and the Ubuntu + Windows quality matrix is green on the final commit.

- **ESLint `9.39.5`, not `10.8.1`** (web + obsidian `package.json`): ESLint 10 is runtime-incompatible with `eslint-config-next@16.3.0` (lint gate crashed with `TypeError: scopeManager.addGlobals is not a function`; bundled plugins `eslint-plugin-react@7.37.5`, `eslint-plugin-jsx-a11y@6.10.2`, `eslint-plugin-import@2.32.0` peer-cap at `^9`). See spec §6.4.
- **Root `[tool.uv.build-backend] module-name = "personal_os"`** (Task 1): `uv_build` derives the module from the distribution name (`knowledge-core`→`knowledge_core`), which does not match the import package `personal_os`; without it `uv sync`/`uv build` cannot locate the module.
- **Obsidian `tsconfig` `types: ["node"]`** (Task 4): TypeScript 6.0.3 requires it — `tsc --noEmit` fails with TS2591 on the `node:fs` import without it.
- **Toolchain checker** (Task 6): probes resolve via `shutil.which` / `sys.executable`, Windows `.cmd`/`.bat` shims launch through `cmd /c`, and CI runs the checker via `uv run python`. Required because the Windows runner's bare `python` (App Execution Alias stub) and `pnpm.cmd` shim are not directly spawnable by `CreateProcess`. The `uv --version` build-metadata parenthetical is stripped before comparison.
- **TypeScript Poe subtasks use the `shell` task type** (Task 5) so Poe can spawn `pnpm` on Windows; Python subtasks stay `cmd`.
- **`pyyaml==6.0.3`** is declared as a direct dev dependency (the dependency-pin contract test imports it).

## Completion Boundary

This plan is complete only when all 14 acceptance criteria in `docs/superpowers/specs/phase-one-workspace-bootstrap-design.md` pass on the same final commit. Completion does not authorize work from `runtime-configuration-and-diagnostics-design.md`; no settings, secrets, structured logging, request IDs, database clients, Docker services or product behavior may be added while executing this plan.
