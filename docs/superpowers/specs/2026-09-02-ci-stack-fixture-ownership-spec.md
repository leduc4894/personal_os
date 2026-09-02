# CI Stack Fixture Ownership Spec

## Goal

Allow source-publication integration fixtures to consume a ready disposable CI
stack without resetting the stack owned by `.local/serve-live-ci.sh`.

## Contract

- A `ready` `knowledge-ci-*` stack is externally owned: the fixture runs
  migrations and tests against it but never resets it.
- An `absent` stack is fixture-owned: the historical reset/bootstrap/config/up
  sequence and final reset remain unchanged.
- Any other status fails closed; the fixture must not mutate that project.

## Acceptance

- Unit tests pin ready and absent ownership branches.
- The four source-lifecycle integration files pass against a clean stack
  created by `serve-live-ci.sh`.
- The live CI project is down after verification.
