# Local service stack integration

**Status:** One executable disposable-stack test is owned here.

## Owner

The local service stack spec owns only `test_local_service_stack.py`. It proves
authenticated startup, persistence across `down`/`up`, repeated initializer
idempotence, Redis outage detection and recovery, and exact final cleanup.

## Future acceptance source

All other future integration layers remain reserved for their owning specs.
They must add executable behavior tests only when the corresponding production
contract exists.

## Runtime boundary

- The test requires a reachable Linux `amd64` Docker Engine.
- `CI=true` and an exact disposable `knowledge-ci-*` project name are required.
- The project is reset in `finally`; no local or production project is accepted.
- Cloudflare R2 and provider credentials are outside this test's scope.

Placeholder tests, zero-assertion fixtures and unrelated cross-module coverage
remain forbidden.
