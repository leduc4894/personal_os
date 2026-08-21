# Source Locator and Tombstone Lifecycle Handoff

## Status

BLOCKED. Child 5 implementation and automated verification are present, but
the mandatory Desktop gate has not passed. The guarded bootstrap created the
fresh Web identity, enrolled and activated TOTP through the approved HTTP flow,
proved the helper preflight, published policy, and launched real WDIO without
printing secret or code material. The latest bounded Desktop run committed
tracked rename and move through the live route, then waited on a delete that
the API rejected with safe HTTP 422. Static TDD subsequently fixed that delete
wire mismatch, but the bounded live window had closed, so the fix has no live
PASS evidence. Mobile is explicitly DEFERRED under the later AGENTS ruling and
its single backlog row. Child 5 is not closed and Child 6 must not begin.

Final implementation commit before this handoff: `e6ccddf` (`fix: omit delete
tombstone wire identity`). The documentation snapshot commit
containing this file is recorded in the Task 12 report because a commit cannot
contain its own SHA.

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| Reference-device contract RED | EXPECTED FAIL | `uv run pytest tests/contract/source_lifecycle/test_reference_device_records.py -m device_records -q`: 1 failed, 4 passed. Mobile is a contract-valid closed deferral with exactly one backlog row; Desktop remains RED because delete failed and the remaining scenarios were not reached. |
| Production lifecycle route regression RED/GREEN | PASS | `test_server_serves_the_source_lifecycle_route` first failed because the route was absent, then passed after composition; server/composition slice: 22 passed. |
| Stack prerequisite | PASS | The ordinary local project was absent and exact project `knowledge-ci-source-lifecycle` reached ready. The guarded bootstrap completed fresh identity, Web credential, TOTP enrollment/activation, helper preflight and policy publication. |
| API/Web/workers/tunnel | PASS | API and Web Admin readiness succeeded; both policy workers ran via repository scripts; the existing configured tunnel served both public origins. No new tunnel or DNS change was made. |
| Desktop WDIO | FAIL | Real Obsidian 1.13.7 / plugin 0.1.0 completed onboarding and initial publication. Live lifecycle routes returned 200 for tracked rename and 200 for tracked move. Delete returned safe HTTP 422; the final bounded rerun was stopped while its delete convergence wait remained open. Commit `e6ccddf` fixes the identified request-shape mismatch but was not rerun live. |
| Locator wire interoperability | PASS | RED proved OpenAPI exported locator wrappers while the runtime accepted strings. GREEN: API/OpenAPI 9 passed, plugin serialization 18 passed, generated-client drift check and both TypeScript builds exited 0. |
| Locked policy race | PASS | The full disposable PostgreSQL race file passed 5 tests; the transaction-locked canonical verdict now selects projection intent operation. |
| Migration selection | PASS | `uv run pytest tests/unit/migrations/test_source_lifecycle_migration.py tests/integration/source_lifecycle/test_lifecycle_migration.py -q`: 7 passed, 1 deselected. |
| Lifecycle feature selection | PASS | `uv run pytest tests/unit/source_locators tests/unit/source_lifecycle tests/contract/source_lifecycle tests/integration/source_lifecycle -q`: 70 passed, 53 deselected. Device records require their explicit marker. |
| Plugin tests | PASS | `pnpm --filter @workspace/obsidian-plugin test`: 32 files, 491 tests passed. |
| Plugin type check | PASS | `pnpm --filter @workspace/obsidian-plugin type-check`: exit 0. |
| Plugin lint | PASS | `pnpm --filter @workspace/obsidian-plugin lint`: exit 0. |
| Plugin build | PASS | `pnpm --filter @workspace/obsidian-plugin build`: exit 0. |
| Lifecycle boundary registries | PASS | The four focused failures were reproduced RED, then the exact Obsidian type import, exact lifecycle API files/route, exact internal-policy identifier grammar and seven lifecycle HTTP status codes were registered without broad scanner exemptions. The four focused nodes passed; all four affected contract files passed 28 tests. |
| Repository verify | PASS | `uv run poe verify` exited 0 after formatting the new classifier: format, lint, strict typing over 176 Python files, import/architecture boundaries, OpenAPI/generated client, Python (3,317 passed, 21 skipped, 398 deselected), plugin (491 passed), web (139 passed), package builds and production web build all passed. |
| Clean shutdown | PASS | API, Web Admin and both workers were stopped; the exact disposable project was verified and removed. The ordinary local project remained absent. |

## Rulings and interpretations

- The brief's environment note does not waive the live gates. Actual Desktop
  and Mobile outcomes are recorded as failures/pending, never inferred from
  unit, integration or mock evidence.
- The production serve graph omitted the lifecycle runtime even though the
  application and OpenAPI export supported it. The smallest fix composes the
  PostgreSQL store, canonical signed-policy evaluator, lifecycle metrics and
  authenticated route in the real server graph.
- The observed Desktop 422 was traced to the generated locator wrapper versus
  runtime string mismatch. The OpenAPI contract now publishes nullable strings
  and the generated client and plugin serialize that same representation.
- Missing active TOTP was handled as the bootstrap branch required by AGENTS,
  not as a deferred prerequisite. Fresh activation, helper preflight and policy
  publication all completed before WDIO.
- Live route evidence proved the UUIDv7 ownership and root-level rename fixes:
  tracked rename and tracked move both committed. Delete then failed validation
  because the plugin sent a provisional tombstone identity even though the
  server contract allocates it. TDD now forces delete to send null while restore
  still sends the server-confirmed predecessor identity.
- The four contract-registry failures predated Task 12. Their registrations are
  closed: `TAbstractFile` remains a type-only Obsidian import, the publication
  scanner sanctions only the three lifecycle API files and exact lifecycle
  route line, the policy scanner matches only exact internal decision
  identifiers, and the HTTP registry enumerates the seven lifecycle codes.
- The later user ruling in `AGENTS.md` keeps Desktop mandatory and permits the
  physical Mobile matrix to defer only through one closed BACKLOG/handoff
  record. That ruling supersedes the historical Task 12 no-deferral sentence.
- Operations detail and mutable reference-device records remain in
  [the living runbook](../operations/source-locator-tombstone-lifecycle.md).

## Deferred items

- `source-lifecycle-mobile-acceptance` /
  `handoff:source-lifecycle-mobile-deferral`: no physical Mobile device was
  available for the eight-scenario matrix. The record remains `DEFERRED`, not
  PASS, and is indexed exactly once in `BACKLOG.md`. Implement by: Before Child
  6 acceptance closure.

## Next actions

1. Bring the disposable live environment up in the authoritative order:
   `uv run poe stack-status`, `bash .local/serve-local.sh`,
   `pnpm --filter @workspace/web-runtime exec next start --port 38000`,
   `bash .local/run-worker.sh run-policy-previews`,
   `bash .local/run-worker.sh run-policy-reconciliations`, then
   `"/c/Program Files (x86)/cloudflared/cloudflared.exe" tunnel run knowledge-api-verify`.
2. Run only the guarded entrypoint
   `uv run python tools/obsidian_live_acceptance_bootstrap.py --project-name knowledge-ci-<bounded-token>`
   with `CI=true`. It owns TOTP bootstrap, policy publication and focused WDIO.
   Record a complete Desktop PASS only if its closed result says so.
3. On a physical Mobile device, run tracked rename, tracked move, delete,
   proven automatic restore, explicit restore, offline capture/reconnect,
   unload/reload and policy-denied transition. Record sanitized metadata and
   evidence references in the living runbook.
4. Run
   `uv run pytest tests/contract/source_lifecycle/test_reference_device_records.py -m device_records -q`.
   Only a PASS for both records permits Child 5 closure.

## Concerns

- Desktop acceptance proves tracked rename and move only. The delete fix in
  `e6ccddf` still requires a complete guarded live rerun through explicit
  restore, stable identity and pending-drain assertions.
- Physical Mobile behavior remains entirely unobserved.
- Repository-wide verification is green. The remaining blocker is exclusively
  the incomplete mandatory Desktop live acceptance journey.
