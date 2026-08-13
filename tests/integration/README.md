# Local service stack integration

**Status:** Two executable disposable-stack tests are owned here.

## Owners

The local service stack spec owns only `test_local_service_stack.py`. It proves
authenticated startup, persistence across `down`/`up`, repeated initializer
idempotence, Redis outage detection and recovery, and exact final cleanup.

The canonical PostgreSQL baseline spec owns only
`test_canonical_postgresql_baseline.py`. It proves the real Alembic
empty-to-head upgrade on disposable PostgreSQL 18.4, the normalized catalog
fingerprint, a valid row graph across all nine baseline tables, the
allowed-behavior cases, ownership/grants/data-minimization, and the full
`upgrade -> downgrade -> re-upgrade` lifecycle with an identical catalog
fingerprint. The disposable project is reset in `finally` and the project label
inventory is asserted empty.

## Future acceptance source

All other future integration layers remain reserved for their owning specs.
They must add executable behavior tests only when the corresponding production
contract exists.

## Runtime boundary

- The tests require a reachable Linux `amd64` Docker Engine.
- `CI=true` and an exact disposable `knowledge-ci-*` project name are required.
- Each project is reset in `finally`; no local or production project is accepted.
- Cloudflare R2 and provider credentials are outside these tests' scope.

## Credential scope

The canonical PostgreSQL baseline test may access ONLY generated local
PostgreSQL credentials beneath the worktree's ignored `.local/stack-secrets/`
directory. It connects with Psycopg keyword arguments and never renders a
plaintext password, DSN, `DATABASE_URL` or `.env` value. It must never read,
render or depend on Cloudflare R2 or any provider credentials.

Placeholder tests, zero-assertion fixtures and unrelated cross-module coverage
remain forbidden.
