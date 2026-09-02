# Serialized Workspace jsdom Tests Specification

## Decision

The root workspace `test` script runs package test scripts sequentially with
pnpm workspace concurrency fixed at one. This removes the known busy-machine
race between the Web and Obsidian Vitest/jsdom suites.

## Contract

- `pnpm run test`, `uv run poe typescript-test`, and `uv run poe verify` use
  `pnpm --workspace-concurrency=1 --recursive --if-present run test`.
- Each package retains its own `vitest run --coverage` command and any
  configured Vitest worker parallelism.
- Test timeouts, jsdom setup, package dependencies, lockfiles, CI workflow
  shape, and production code remain unchanged.

## Acceptance Criteria

1. A contract test pins the root script's sequential pnpm command.
2. Each package suite still runs exactly once and passes when invoked through
   the root test command.
3. `uv run poe verify` passes without requiring an ambient
   `npm_config_workspace_concurrency` environment variable.
4. The jsdom BACKLOG row is removed only after repeat verification on the
   affected machine and one handoff records the result.
