# Source Locator and Tombstone Lifecycle Handoff

## Status

BLOCKED. Child 5 implementation and automated verification are present, but
the two mandatory live acceptance gates have not passed. The lifecycle locator
wire mismatch is fixed and covered by API/OpenAPI/generated-client tests. The
bounded Desktop invocation reached the real authorization and login routes but
stopped because its shell omitted required authentication-setting names. A
corrected canonical helper probe then reported that the fresh disposable
identity had no active TOTP credential, so a fully configured rerun could not
begin. The physical Mobile matrix was not run. Child 5 is not closed and Child
6 must not begin.

Final implementation commit before this handoff: `6c79787` (`fix: align source
lifecycle policy and wire contracts`). The documentation snapshot commit
containing this file is recorded in the Task 12 report because a commit cannot
contain its own SHA.

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| Reference-device contract RED | EXPECTED FAIL | Initial run failed because both record sections were absent. After adding honest records, `uv run pytest tests/contract/source_lifecycle/test_reference_device_records.py -m device_records -q` failed with all Desktop scenarios non-PASS; Mobile remains unobserved. |
| Production lifecycle route regression RED/GREEN | PASS | `test_server_serves_the_source_lifecycle_route` first failed because the route was absent, then passed after composition; server/composition slice: 22 passed. |
| Stack prerequisite | PARTIAL | The ordinary local project was stopped and a validated disposable `knowledge-ci-*` project reached `stack_ready`. Schema, identity, web credential and policy signer setup completed, but `.local/e2e-totp-code.py` reported the closed `no active totp credential found` prerequisite and policy publication could not complete. |
| API/Web/workers/tunnel | PASS | API and Web Admin readiness succeeded; both policy workers ran via repository scripts; the existing configured tunnel served both public origins. No new tunnel or DNS change was made. |
| Desktop WDIO | FAIL | Real Obsidian 1.13.7 / plugin 0.1.0 bounded invocation exited 1 in 1m06s. Device authorization and admin login returned HTTP 200, then the TOTP helper failed with typed missing authentication settings because the invoking shell omitted five required names. With those names corrected, a direct canonical helper probe reported `no active totp credential found`; no lifecycle scenario was reached. |
| Locator wire interoperability | PASS | RED proved OpenAPI exported locator wrappers while the runtime accepted strings. GREEN: API/OpenAPI 9 passed, plugin serialization 18 passed, generated-client drift check and both TypeScript builds exited 0. |
| Locked policy race | PASS | The full disposable PostgreSQL race file passed 5 tests; the transaction-locked canonical verdict now selects projection intent operation. |
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
- The observed Desktop 422 was traced to the generated locator wrapper versus
  runtime string mismatch. The OpenAPI contract now publishes nullable strings
  and the generated client and plugin serialize that same representation.
- The review rerun does not prove the wire fix live: the fresh disposable
  identity lacked the active TOTP credential required by the canonical helper,
  so lifecycle assertions were never reached.
- No BACKLOG row was added: Desktop and Mobile acceptance are mandatory, not
  deferrable work.
- Operations detail and mutable reference-device records remain in
  [the living runbook](../operations/source-locator-tombstone-lifecycle.md).

## Deferred items

None. The two incomplete acceptance gates are current blockers, not deferred
items.

## Next actions

1. Enroll an active TOTP credential for the disposable Web identity through the
   canonical Web Admin flow. Confirm `.local/e2e-totp-code.py` succeeds without
   recording its output, then run `.local/publish-policy-revision.py`.
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
6. Resolve the inherited Ruff format drift, then rerun `uv run poe verify` to
   exit 0 before closure.

## Concerns

- Desktop acceptance still does not prove the corrected locator wire contract;
  the latest bounded run stopped at the missing active-TOTP prerequisite.
- Physical Mobile behavior remains entirely unobserved.
- Repository-wide format verification is red on pre-existing lifecycle-area
  formatting drift, despite the Task 12 files themselves being formatted.
