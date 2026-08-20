# Source Publication Readiness Remediation Handoff

**Date:** 2026-08-20  
**Plan:** `docs/superpowers/plans/2026-08-20-source-publication-readiness-remediation.md`  
**Spec:** `docs/superpowers/specs/2026-08-20-child-five-readiness-remediation-design.md`  
**Branch:** `codex/source-publication-readiness-remediation`  
**Code commit:** `b9cb8c8` (handoff/BACKLOG documentation commit follows)

## Gate status

| Gate | Status and evidence |
| --- | --- |
| Source-publication telemetry | ✅ Registered safe failed diagnostic is bound at both production composition roots; retryable busy/ambiguous outcomes do not increment rejection metrics. Task review clean. |
| Dispatcher recovery and lease diagnostics | ✅ Only unavailable dispatch errors retry after the one-second, shutdown-aware bounded delay; stale diagnostics emit after commit and distinguish active wrong fences from expired leases. Task review clean; final-review fixes re-reviewed clean. |
| Adapter structural contracts | ✅ Contract scanner covers FastAPI, aiohttp, and boto3; table field map comparison is exact. Task review clean. |
| Final whole-branch review | ✅ Initial review found two Important gaps; `b9cb8c8` fixed both, and scoped re-review approved with no new breakage. |
| Full repository gate | ✅ `uv run poe verify` exited 0 at `b9cb8c8`: formatting, Ruff, strict mypy, TypeScript checks, import-linter, contract/API checks, Python and TypeScript tests, and builds. |
| Local-stack lease regression | ✅ Task 2 report records the targeted transaction-order regression as 17 passed after the local service was stopped to release its port. |

Living operational status: `docs/operations/source-publication.md`.

## Delivered behavior

- Source publication now reports registered, safe diagnostics from API and canonical-core composition roots; retryable failures remain non-rejections.
- Projection dispatch stays alive through bounded retryable database unavailability and preserves prompt shutdown.
- Fenced stale-lease diagnostics are emitted after the transaction commits, with active wrong-token and expired lease cases separately classified using the closed diagnostic vocabulary.
- Static source-adapter contracts now reject the full required forbidden import families and compare table metadata field maps exactly.

## Rulings and interpretations

1. The plan’s focused Task 1 baseline was already green, so implementers added regression tests and recorded RED before production changes. **Cost if wrong:** a pre-existing test could have obscured a missing acceptance behavior.
2. `SOURCE_VERSION_PUBLISH_FAILED` was absent despite the plan’s literal assertion; it was registered as an internal diagnostic using only existing safe field vocabulary. **Cost if wrong:** the event name could be relied on beyond the stated internal contract.
3. Active wrong-fence stale leases use existing `projection_intent_contract_invalid`; expired leases retain `projection_dispatch_lease_expired`; retry recovery uses the existing one-second polling interval. **Cost if wrong:** consumers may interpret the active-fence code as broader contract invalidity.
4. The planned `.items()` field-map assertion is semantically equivalent to dictionary equality, so it had no genuine RED state. The change preserves the stated exact-value contract without claiming new behavioral coverage. **Cost if wrong:** future test changes could still miss a different metadata mismatch unless their fixtures exercise it.
5. Final review’s suggestion to directly assert the retry delay and non-unavailable rethrow was triaged non-blocking: the implementation and focused tests already cover the behavior sufficiently for this gate. No deferred backlog item was created. **Cost if wrong:** a later refactor could weaken these branches before a direct regression test is added.

## Deferred items

None created by this plan. The five completed source-publication rows were removed from `docs/handoff/BACKLOG.md`; later-gated source-publication and all other domain rows remain indexed.

## Next actions

1. Integrate `codex/source-publication-readiness-remediation` into `master` when ready.
2. Continue the remaining independently scoped Before-Child-5 remediation rows from the canonical-core, web-auth, and small-file-sync handoffs before starting Child 5.
