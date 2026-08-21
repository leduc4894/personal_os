# Obsidian Live Acceptance Bootstrap Tasks

## Deliverable 1: Executable fresh-state recovery

Done when a contract test starts with an unavailable TOTP helper, observes the
real enrollment/verify HTTP route sequence, observes a successful second helper
preflight, and proves policy publication and WDIO occur only afterward.

Verification:

```powershell
uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q
```

## Deliverable 2: Closed privacy and failure boundary

Done when invalid disposable input and every failed external step return a
closed result code, while child/HTTP secret sentinels never appear in stdout or
stderr.

Verification:

```powershell
uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q
uv run ruff check tools/obsidian_live_acceptance_bootstrap.py tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py
uv run mypy --strict tools/obsidian_live_acceptance_bootstrap.py
```

## Deliverable 3: Durable operator instruction

Done when AGENTS.md and the lifecycle operations guide name the bootstrap
entrypoint and explicitly forbid treating a missing active TOTP credential as a
blocker or deferred gate before the bootstrap branch has run.

Verification:

```powershell
git diff --check
(Get-Content AGENTS.md).Count
(Get-Content docs/operations/source-locator-tombstone-lifecycle.md).Count
```
