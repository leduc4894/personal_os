# Canonical PostgreSQL Baseline Repin Design

## Goal

Restore the disposable canonical PostgreSQL baseline lifecycle suite so its
catalog oracle matches the current Alembic head `20260902_02`.

## Scope

- Update the integration test's head revision, exact table/order catalog
  constants, expected indexes, triggers and constraints to the migrations
  already present through `20260902_02`.
- Add the reduced `ck_user_credentials__timestamps` expectation required after
  initial-TOTP-offer retirement.
- Preserve the suite's existing empty-to-head, no-op, concurrent upgrade,
  destructive downgrade, re-upgrade, and interruption guarantees.

## Required behavior

1. `alembic upgrade head` reaches the test's exact `_HEAD_REVISION` and the
   catalog assertions include all canonical tables introduced since
   `20260818_01`, including `knowledge.source_conflicts`.
2. Expected counts and seeded row-count assertions include every new table in
   deterministic count order, with zero rows unless this suite's fixed fixture
   explicitly creates a valid row graph for it.
3. The asserted constraint corpus accepts the reduced
   `ck_user_credentials__timestamps` clause produced by migration
   `20260902_02` and continues rejecting unexpected catalog drift.
4. Upgrade/downgrade/re-upgrade produces the same normalized catalog
   fingerprint; destructive authorization and leak-redaction assertions remain
   unchanged.

## Non-goals

- No production schema change, migration creation, migration rewrite, or
  backup-manifest change.
- No changes to local-stack secrets, Docker configuration, or ordinary
  `poe verify` selection.
- No live Obsidian test.

## Design constraints

- The test continues to use only the bounded PostgreSQL secret-file contract
  and must not print a password, DSN, provider credential or raw database
  error.
- Expected catalog values are derived from existing migrations and verified
  against a disposable `knowledge-ci-*` project, never against
  `knowledge-local`.
- The final suite remains marked `local_stack` and is invoked only through its
  dedicated guarded CI/local-stack command.

## Acceptance criteria

- The baseline suite fails before the repin because the old catalog/head is
  stale, then passes with the exact current head and catalog.
- Its full lifecycle assertions pass on a disposable `knowledge-ci-*` stack;
  no existing migration behavior changes.
- The canonical-postgresql-baseline BACKLOG row is removed after that evidence
  is recorded.

