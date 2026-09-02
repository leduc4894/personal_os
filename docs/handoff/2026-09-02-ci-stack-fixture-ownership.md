# CI Stack Fixture Ownership Handoff

**Date:** 2026-09-02

## Status

Implementation is ready to commit on branch `ci-stack-fixture-ownership`.
The prior commit was `220ea5b`; the implementation commit SHA is assigned at
commit time.

## Decision

`source_publication_stack` consumes a ready disposable CI stack without stack
lifecycle mutation. When the stack is absent it retains self-managed startup
and cleanup. Any intermediate state fails closed.

## Gate evidence

- `pytest tests/unit/integration_fixtures/test_source_publication_stack_ownership.py -q`: `2 passed`.
- Ruff format/check on the changed files: pass.
- `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-fixture-ownership-20260902`
  over the four source-lifecycle files: `35 passed in 177.90s`.
- `serve-live-ci.sh down`: disposable project absent afterwards.

## Deferred items

None. BACKLOG was not changed.

## Next actions

Commit the implementation, then reuse this external-owned fixture contract for
future full-stack integration gates.
