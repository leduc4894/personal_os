# Source Locator and Tombstone Lifecycle Handoff

## Status

COMPLETE. Child 5 implementation, automated verification and the mandatory
Desktop live gate are complete. After the explicit-restore modal settlement
fixes `ce59fee` and `b981c71`, exactly one final guarded retry prepared the
exact disposable stack and runbook services, used the existing tunnel, and
invoked only the approved bootstrap with `CI=true`. It returned
`obsidian_live_acceptance_passed`; child diagnostics remained suppressed and
no secret, code, raw locator or content was recorded. Mobile remains explicitly
DEFERRED under the later AGENTS ruling and its single backlog row, so Child 5
is closed and Child 6 may begin but cannot close before that physical evidence.

Final implementation commit before this handoff: `b981c71` (`test: prove
explicit restore modal settlement`). The documentation snapshot commit
containing this file is recorded in the Task 12 report because a commit cannot
contain its own SHA.

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| Reference-device contract | PASS | The living Desktop record now carries six observed PASS outcomes from the final guarded journey. Mobile remains a contract-valid closed deferral with exactly one backlog row. |
| Production lifecycle route regression RED/GREEN | PASS | `test_server_serves_the_source_lifecycle_route` first failed because the route was absent, then passed after composition; server/composition slice: 22 passed. |
| Stack prerequisite | PASS | The ordinary local project was absent and exact project `knowledge-ci-source-lifecycle` reached ready. The guarded bootstrap completed fresh identity, Web credential, TOTP enrollment/activation, helper preflight and policy publication. |
| API/Web/workers/tunnel | PASS | API and Web Admin readiness succeeded; both policy workers ran via repository scripts; the existing configured tunnel served both public origins. No new tunnel or DNS change was made. |
| Desktop WDIO | PASS | Exactly one final guarded retry after `ce59fee` and `b981c71` returned `obsidian_live_acceptance_passed`, proving rename, move, delete, explicit restore, stable identity and final journal drain through the approved closed boundary. |
| Locator wire interoperability | PASS | RED proved OpenAPI exported locator wrappers while the runtime accepted strings. GREEN: API/OpenAPI 9 passed, plugin serialization 18 passed, generated-client drift check and both TypeScript builds exited 0. |
| Locked policy race | PASS | The full disposable PostgreSQL race file passed 5 tests; the transaction-locked canonical verdict now selects projection intent operation. |
| Migration selection | PASS | `uv run pytest tests/unit/migrations/test_source_lifecycle_migration.py tests/integration/source_lifecycle/test_lifecycle_migration.py -q`: 7 passed, 1 deselected. |
| Lifecycle feature selection | PASS | `uv run pytest tests/unit/source_locators tests/unit/source_lifecycle tests/contract/source_lifecycle tests/integration/source_lifecycle -q`: 70 passed, 57 deselected. Device records require their explicit marker. |
| Plugin tests | PASS | `pnpm --filter @workspace/obsidian-plugin test`: 32 files, 491 tests passed. |
| Plugin type check | PASS | `pnpm --filter @workspace/obsidian-plugin type-check`: exit 0. |
| Plugin lint | PASS | `pnpm --filter @workspace/obsidian-plugin lint`: exit 0. |
| Plugin build | PASS | `pnpm --filter @workspace/obsidian-plugin build`: exit 0. |
| Lifecycle boundary registries | PASS | The four focused failures were reproduced RED, then the exact Obsidian type import, exact lifecycle API files/route, exact internal-policy identifier grammar and seven lifecycle HTTP status codes were registered without broad scanner exemptions. The four focused nodes passed; all four affected contract files passed 28 tests. |
| Repository verify | PASS | `uv run poe verify` exited 0 after formatting the new classifier: format, lint, strict typing over 176 Python files, import/architecture boundaries, OpenAPI/generated client, Python (3,317 passed, 21 skipped, 398 deselected), plugin (491 passed), web (139 passed), package builds and production web build all passed. |
| Clean shutdown | PASS | All API, Web Admin and worker processes owned by the guarded retries were stopped; ports 8000/38000 were clear. Exact project `knowledge-ci-source-lifecycle` and ordinary `knowledge-local` both returned `stack_absent`; the pre-existing tunnel remained running. No phase marker remained and no child diagnostics were retained. |

## Post-fix retry audit trail

| Step | Repository command | Sanitized outcome |
| --- | --- | --- |
| Ordinary stack preflight | `uv run poe stack-status` | `knowledge-local`: `stack_absent`. |
| Ordinary stack lowering | `uv run poe stack-down` | `stack_down_complete`. |
| Disposable stack bootstrap | `uv run python tools/local_service_stack.py bootstrap --project-name knowledge-ci-source-lifecycle` | `secret_set_ready`; only contract presence was observed. |
| Disposable stack validation | `uv run python tools/local_service_stack.py config --project-name knowledge-ci-source-lifecycle` | `stack_config_valid`. |
| Disposable stack start | `uv run python tools/local_service_stack.py up --project-name knowledge-ci-source-lifecycle` | `stack_ready`; declared services healthy. |
| Disposable stack probe | `uv run python tools/local_service_stack.py verify --project-name knowledge-ci-source-lifecycle` | `stack_verified`; every closed service probe was ready. |
| API | `bash .local/serve-local.sh` | The controller corrected a local argument-quoting error, then API readiness returned HTTP 200. No runtime or secret contract was missing. |
| Web Admin | `pnpm --filter @workspace/web-runtime exec next start --port 38000` | Local readiness returned HTTP 200. |
| Preview worker | `bash .local/run-worker.sh run-policy-previews` | The controller corrected the same local argument-quoting error, then the worker stayed alive for the guarded run. |
| Reconciliation worker | `bash .local/run-worker.sh run-policy-reconciliations` | The controller corrected the same local argument-quoting error, then the worker stayed alive for the guarded run. |
| Existing tunnel | `Get-Process cloudflared` plus bounded public readiness probes | Existing process remained alive; both configured origins returned HTTP 200. No tunnel or DNS mutation occurred. |
| Guarded Desktop run | `CI=true uv run python tools/obsidian_live_acceptance_bootstrap.py --project-name knowledge-ci-source-lifecycle` | `{"result_code":"obsidian_wdio_failed","state":"error"}`; no child diagnostics crossed the boundary. |
| Owned process cleanup | `Stop-Process` for the resolved owned process trees | Zero owned roots remained; ports 8000/38000 were clear. The existing tunnel remained alive. |
| Disposable stack stop | `uv run python tools/local_service_stack.py down --project-name knowledge-ci-source-lifecycle` | `stack_down_complete`. |
| Final absence checks | `uv run python tools/local_service_stack.py status --project-name knowledge-ci-source-lifecycle` and `uv run poe stack-status` | Both exact CI and ordinary projects returned `stack_absent`; owned temporary logs were absent. |

## Instrumented Desktop retry audit trail

Exactly one retry was run after the closed phase-marker contract landed. The
ordinary stack was lowered first; the exact disposable project then returned
`stack_ready` and `stack_verified`. API and Web Admin returned HTTP 200, both
repository-launched policy workers stayed alive, and the pre-existing tunnel
served both configured origins without any tunnel or DNS mutation.

The sole guarded invocation returned
`{"result_code":"obsidian_wdio_failed_after_delete","state":"error"}`. No
child output crossed the bootstrap boundary. No second invocation, code change
or success claim followed.

Cleanup stopped every owned API, Web and worker process, left zero owned roots,
cleared ports 8000 and 38000, and removed the owned temporary logs and phase
marker. Exact `knowledge-ci-source-lifecycle` and ordinary `knowledge-local`
both ended at `stack_absent`; the pre-existing tunnel remained alive.

## Final guarded Desktop retry audit trail

Exactly one retry was run after the explicit-restore modal settlement fixes
`ce59fee` and `b981c71`. The ordinary project was absent and was lowered through
the repository contract. Exact `knowledge-ci-source-lifecycle` then returned
`secret_set_ready`, `stack_config_valid`, `stack_ready` and `stack_verified`.
API and Web Admin readiness returned HTTP 200, both policy workers stayed alive
through the repository launcher, and the pre-existing tunnel served both
configured origins without any tunnel or DNS mutation.

The sole approved invocation was
`CI=true uv run python tools/obsidian_live_acceptance_bootstrap.py --project-name knowledge-ci-source-lifecycle`.
It returned
`{"result_code":"obsidian_live_acceptance_passed","state":"complete"}`.
This closed result proves the complete focused journey, including explicit
restore, stable source/version identity and final lifecycle-journal drain. No
second retry or code change followed it, and no child output was copied into
the acceptance evidence.

Cleanup stopped the four owned process sessions, cleared ports 8000 and 38000,
and left no project-bound phase marker. Exact `knowledge-ci-source-lifecycle`
and ordinary `knowledge-local` both ended at `stack_absent`; the pre-existing
tunnel remained running.

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

1. On a physical Mobile device, run tracked rename, tracked move, delete,
   proven automatic restore, explicit restore, offline capture/reconnect,
   unload/reload and policy-denied transition. Record sanitized metadata and
   evidence references in the living runbook before Child 6 acceptance
   closure, then remove the single resolved backlog row.
2. Run
   `uv run pytest tests/contract/source_lifecycle/test_reference_device_records.py -m device_records -q`.
   Mobile must either PASS or continue to satisfy the exact closed deferral
   contract until its implement-by trigger.

## Concerns

- Physical Mobile behavior remains entirely unobserved.
- Repository-wide verification and the mandatory Desktop journey are green.
  The only remaining item is the explicitly bounded Mobile deferral.
