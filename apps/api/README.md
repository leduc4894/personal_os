# `apps/api` — API process shell

This package is the composition root for the `personal-api` process. It is a
`uv` workspace member that depends on the root `knowledge-core` distribution
and exposes the `personal-api` console-script entry point
(`api_runtime.command:main`).

**Composition role:** API process shell and the runnable HTTP contract spine.
The shell-only paths (`--help`, `--version`, no argument and any invalid
syntax) parse arguments and exit without reading any environment variable,
secret file or network resource, and without importing any framework SDK.
`serve` lazy-imports FastAPI/Uvicorn and runs the application;
`export-openapi` renders the deterministic OpenAPI 3.1 document offline. The
`check-runtime` subcommand loads the approved runtime configuration described
in the root README.

## API runtime contract

- `personal-api serve` binds Uvicorn single-process (loopback defaults in
  local/test; `KNOWLEDGE_API_HOST`/`KNOWLEDGE_API_PORT` required explicitly in
  staging/production) and serves `GET /api/health/live` (I/O-free liveness)
  and `GET /api/health/ready` (one bounded PostgreSQL connectivity and
  exact-schema-head probe, two-second deadline, no retry).
- Every application/health response uses the strict envelope
  `{request_id, data, warnings, error}` with a server-owned UUIDv7 request ID
  returned in body and `X-Request-ID`; framework errors map through a closed
  code-to-status table. Local/test additionally serve the raw
  `/api/openapi.json`; production OpenAPI is disabled along with Swagger UI
  and ReDoc.
- The full operator contract (startup/shutdown, readiness and schema-drift
  diagnosis, OpenAPI governance) lives in
  [`docs/operations/api-runtime-contract.md`](../../docs/operations/api-runtime-contract.md).

## Build and test

This package is built and tested as part of the root workspace; it has no
standalone build command.

```bash
uv run poe build          # uv build --all-packages (builds this member wheel)
uv run poe test           # pytest exercises --help/--version/no-arg/invalid-arg
uv run --package api-runtime personal-api --help
```

## `check-runtime` health check

```bash
personal-api check-runtime
```

The command loads and validates the runtime configuration snapshot and emits
exactly one safe JSON object (one JSON object per line). It never performs a
settings dump and never emits secret values, file paths, environment variables
or exception text. Stream routing, correlation fields, the approved
`KNOWLEDGE_*` variables and the full operator contract are defined in the root
README (*Runtime configuration & diagnostics*); this section lists only the
exit codes:

| Exit | Meaning |
| --- | --- |
| `0` | Success — runtime configuration validated. |
| `2` | CLI syntax error. |
| `70` | Unexpected internal error. |
| `78` | Configuration or secret error. |

## Intentionally absent behavior

The following are deliberately absent and belong to later child specs:

- authentication, sessions, device authorization and workspace resolution
  middleware;
- source, sync, upload, download, event, reconciliation or conflict routes;
- business tables, Alembic migrations and non-PostgreSQL readiness
  dependencies (R2, Temporal, Redis, Qdrant, Neo4j);
- object storage clients and provider SDKs in the API process.

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
