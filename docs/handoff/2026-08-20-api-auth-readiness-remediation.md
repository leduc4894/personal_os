# API Authentication Readiness Remediation Handoff

Date: 2026-08-20  
Domain: API authentication readiness  
Branch: `codex/api-auth-readiness-remediation`  
Last implementation commit: `8c2ec97ecd58c632e4ff05482a10ba278e3ecd1c`

## Scope and status

Closed the API-hygiene backlog item previously gated **Before Child 5**:

- Lifespan keyring configuration refusals emit structured safe diagnostics
  before framework failure and still run lifecycle cleanup.
- Keyring coverage uses the authoritative database clock.
- Offline username and source throttles have separate maps and independent
  transitions.
- Malformed login JSON is contract-pinned to `400` and `Cache-Control: no-store`.
- The authentication leakage harness now scans all three offline throttle maps.

The other Before Child 5 backlog items remain indexed because they were not
part of this API-auth plan.

## Gate evidence

| Gate | Status | Evidence |
| --- | --- | --- |
| Task 1 focused unit/type checks | Pass | `ruff`, 54 focused unit tests, authentication key-rotation integration 5 passed, mypy 31 source files. |
| Task 2 contract pin | Pass | `test_authentication_headers.py` 17 passed; no production change because the RED command already passed. |
| Per-task and final reviews | Pass | Task 1 fix round and Task 2 review clean; final Minor leakage-harness finding fixed and scoped re-review approved. |
| Full repository verification | Pass | `uv run poe verify`: 3,094 Python passed, 21 skipped, 331 deselected; 375 plugin tests; 139 web tests; format/lint/type/import/OpenAPI/build gates passed. |

The first full-gate attempt timed out in an unrelated Obsidian journal test;
the exact test and standalone plugin suite passed, then the complete gate
passed on rerun. No code was changed for that external timing event.

## Decisions

- Kept the keyring helper's clock argument optional for existing internal
  callers, but its only default remains `DatabaseAuthenticationClock(engine)`;
  host time cannot be selected.
- Added only the malformed-JSON regression test because existing behavior
  already met the contract before implementation.
- Fixed, rather than deferred, the leakage test-harness omission so future
  authentication throttle-key redaction regressions remain detectable.

## Deferred work

None created by this plan. `docs/handoff/BACKLOG.md` has had only the closed
API-hygiene row removed.

## Next actions

Proceed with the remaining Child 5 readiness work already indexed in
`docs/handoff/BACKLOG.md` before starting Child 5. Integrate this branch by
the repository's normal review/merge flow.
