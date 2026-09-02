# CI Stack Fixture Ownership Implementation Plan

**Goal:** Remove conflicting stack lifecycle ownership between the
source-publication integration fixture and `serve-live-ci.sh`.

**Spec:** `docs/superpowers/specs/2026-09-02-ci-stack-fixture-ownership-spec.md`

## Completed tasks

1. Added red ownership tests for a ready external project and an absent
   fixture-owned project.
2. Added a safe status reader and ownership selector to
   `tests/integration/source_publication/conftest.py`; only fixture-owned
   projects are reset in teardown.
3. Verified `2 passed` for the ownership unit suite and `35 passed` for the
   source-lifecycle CI integration gate.
