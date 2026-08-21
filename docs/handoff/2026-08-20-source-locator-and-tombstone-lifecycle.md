# Source Locator and Tombstone Lifecycle Handoff

## Status

BLOCKED. Child 5 implementation and automated verification are present, but
the two mandatory live acceptance gates have not passed. The real Desktop WDIO
journey reached the authenticated canonical API and received a safe HTTP 422 on
the rename lifecycle request; move, delete, explicit restore, stable-identity
and drain assertions were therefore not reached. The physical Mobile matrix
was not run. Child 5 is not closed and Child 6 must not begin.

Final implementation commit before this handoff: `89e1aef` (`fix: bind source
lifecycle serve runtime`). The documentation snapshot commit containing this
file is recorded in the Task 12 report because a commit cannot contain its own
SHA.

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| Reference-device contract RED | EXPECTED FAIL | Initial run failed because both record sections were absent. After adding honest records, `uv run pytest tests/contract/source_lifecycle/test_reference_device_records.py -m device_records -q` failed with all Desktop scenarios non-PASS; Mobile remains unobserved. |
| Production lifecycle route regression RED/GREEN | PASS | `test_server_serves_the_source_lifecycle_route` first failed because the route was absent, then passed after composition; server/composition slice: 22 passed. |
| Stack prerequisite | PASS | `uv run poe stack-status` reported the ordinary local project stopped; a validated disposable `knowledge-ci-*` project reached `stack_ready`. Schema, identity, web credential, TOTP and policy revision setup completed through repository contracts without recording values. |
| API/Web/workers/tunnel | PASS | API and Web Admin readiness were HTTP 200; both policy workers ran via repository scripts; the existing configured tunnel served both public origins. No new tunnel or DNS change was made. |
| Desktop WDIO | FAIL | Real Obsidian 1.13.7 / plugin 0.1.0 run exited 1 in 1m38s. Authentication, policy acquisition and initial canonical create passed; rename POST reached `/api/sources/lifecycle-events` and returned safe HTTP 422, leaving lifecycle event count zero. |
| Migration selection | PASS | `uv run pytest tests/unit/migrations/test_source_lifecycle_migration.py tests/integration/source_lifecycle/test_lifecycle_migration.py -q`: 7 passed, 1 deselected. |
| Lifecycle feature selection | PASS | `uv run pytest tests/unit/source_locators tests/unit/source_lifecycle tests/contract/source_lifecycle tests/integration/source_lifecycle -q`: 70 passed, 53 deselected. Device records require their explicit marker. |
| Plugin tests | PASS | `pnpm --filter @workspace/obsidian-plugin test`: 31 files, 488 tests passed. |
| Plugin type check | PASS | `pnpm --filter @workspace/obsidian-plugin type-check`: exit 0. |
| Plugin lint | PASS | `pnpm --filter @workspace/obsidian-plugin lint`: exit 0. |
| Plugin build | PASS | `pnpm --filter @workspace/obsidian-plugin build`: exit 0. |
| Repository verify | FAIL | `uv run poe verify` stopped at format-check: 28 existing source-lifecycle-area files would be reformatted. The Task 12 Python files were then formatted; unrelated files were not rewritten. |
| Clean shutdown | PASS | API, Web Admin and both workers were stopped; the exact disposable project was verified and removed. The ordinary local project remained absent. |

## Rulings and interpretations

- The brief's environment note does not waive the live gates. Actual Desktop
  and Mobile outcomes are recorded as failures/pending, never inferred from
  unit, integration or mock evidence.
- The production serve graph omitted the lifecycle runtime even though the
  application and OpenAPI export supported it. The smallest fix composes the
  PostgreSQL store, canonical signed-policy evaluator, lifecycle metrics and
  authenticated route in the real server graph.
- The post-fix Desktop 422 is a distinct wire-validation blocker. The generated
  client currently sends locator wrappers while the backend validator accepts
  normalized locator strings. That contract mismatch requires its own RED
  OpenAPI/generated-client regression before correction; it was not guessed
  around during acceptance.
- No BACKLOG row was added: Desktop and Mobile acceptance are mandatory, not
  deferrable work.
- Operations detail and mutable reference-device records remain in
  [the living runbook](../operations/source-locator-tombstone-lifecycle.md).

## Deferred items

None. The two incomplete acceptance gates are current blockers, not deferred
items.

## Next actions

1. Write a failing API/OpenAPI/generated-client contract proving lifecycle
   locator fields have one shared wire representation; implement and regenerate
   the client, then rerun all affected contract and plugin tests.
2. Bring the disposable live environment up in the authoritative order:
   `uv run poe stack-status`, `bash .local/serve-local.sh`,
   `pnpm --filter @workspace/web-runtime exec next start --port 38000`,
   `bash .local/run-worker.sh run-policy-previews`,
   `bash .local/run-worker.sh run-policy-reconciliations`, then
   `"/c/Program Files (x86)/cloudflared/cloudflared.exe" tunnel run knowledge-api-verify`.
3. Run
   `pnpm --filter @workspace/obsidian-plugin exec wdio run wdio.conf.mts --spec test/specs/source-lifecycle.e2e.ts`
   with the canonical loader contract and record a complete PASS.
4. On a physical Mobile device, run tracked rename, tracked move, delete,
   proven automatic restore, explicit restore, offline capture/reconnect,
   unload/reload and policy-denied transition. Record sanitized metadata and
   evidence references in the living runbook.
5. Run
   `uv run pytest tests/contract/source_lifecycle/test_reference_device_records.py -m device_records -q`.
   Only a PASS for both records permits Child 5 closure.

## Concerns

- Desktop acceptance currently demonstrates an authenticated wire-contract
  mismatch, not lifecycle success.
- Physical Mobile behavior remains entirely unobserved.
- Repository-wide format verification is red on pre-existing lifecycle-area
  formatting drift, despite the Task 12 files themselves being formatted.
