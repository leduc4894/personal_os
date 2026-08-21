# Obsidian Live Acceptance Bootstrap Handoff

## Status

COMPLETE. The guardrail sub-plan is implemented. This status covers the
bootstrap orchestration only; it does not claim that the source-lifecycle
Desktop or Mobile live acceptance gates have passed.

Final implementation commit: `86e8f00` (`feat: guard obsidian live acceptance bootstrap`).
The documentation snapshot containing this handoff is the enclosing commit and
cannot record its own SHA.

## Gate evidence

| Gate | Status | Evidence |
| --- | --- | --- |
| Initial contract RED | EXPECTED FAIL | `uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q` failed during collection with `ModuleNotFoundError` before the production module existed. |
| Preflight edge RED | EXPECTED FAIL | The expanded focused run reported two intended failures: malformed helper output bypassed enrollment and an already-active rerun performed two helper probes. |
| Bootstrap contract GREEN | PASS | `uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q`: 6 passed. |
| Related command/docs contracts | PASS | `uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py tests/contract/test_bootstrap_documentation.py tests/contract/test_process_commands.py -q`: 70 passed. |
| Security and architecture contracts | PASS | `uv run pytest tests/contract/test_ci_security.py tests/contract/test_architecture_boundaries.py tests/contract/api/test_authentication_leakage.py -q`: 59 passed, one upstream deprecation warning. |
| Python formatting | PASS | `uv run poe python-format-check`: 450 files already formatted. |
| Python lint | PASS | `uv run poe python-lint`: all checks passed. |
| Python strict typing | PASS | `uv run poe python-type-check`: no issues in 176 source files. |
| Closed CLI refusal | PASS | Invoking the real entrypoint with `CI=true` and `knowledge-local` returned only `disposable_project_required` and exited nonzero before external work. |

## Decisions

- The entrypoint begins after services are started in `.local/RESTART.md`
  order. It does not duplicate or replace the local API, Web, worker, or tunnel
  launchers.
- Non-secret runtime setting names are loaded from `.local/serve-local.sh`, so
  the helper, policy publisher, and WDIO receive one consistent environment and
  cannot repeat the prior missing-authentication-settings invocation.
- A helper exit is accepted only when stdout is exactly one six-digit code.
  Missing or malformed output selects the HTTP enrollment branch; after
  enrollment a second failed probe is a closed bootstrap failure.
- TOTP start and activation use the real Web HTTP routes. Identity, password
  credential, policy-key initialization, policy publication, and WDIO reuse
  their existing repository-owned boundaries.
- Every child stream is captured or discarded. The entrypoint emits one closed
  JSON status document and never forwards provisioning material, credentials,
  cookies, codes, provider errors, paths, or locators.

## Deferred items

None. The original source-lifecycle Desktop and Mobile live gates remain active
Task 12 work rather than deferred items, so no BACKLOG row was added.

## Next actions

1. Start the disposable stack and live services in `.local/RESTART.md` order.
2. Set `CI=true` and run
   `uv run python tools/obsidian_live_acceptance_bootstrap.py --project-name knowledge-ci-<bounded-token>`.
3. Record the actual Desktop result in the source-lifecycle operations guide.
4. Complete the mandatory physical Mobile matrix and rerun the reference-device
   contract before closing Child 5.
