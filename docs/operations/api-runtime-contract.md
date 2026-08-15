# API Runtime and Contract Operations Guide

Operator contract for the API HTTP boundary (`apps/api`, the framework-neutral
`src/personal_os/api_contracts`, and the shared TypeScript client
`packages/api-client`). This child deliberately implements only the contract
spine — liveness, PostgreSQL readiness, the deterministic OpenAPI snapshot and
the generated-client pipeline. Design:
`docs/superpowers/specs/2026-08-15-api-runtime-and-contract-foundation-design.md`.

## Commands

```bash
uv run --package api-runtime personal-api serve               # run the API server
uv run --package api-runtime personal-api export-openapi --output <path>
uv run --package api-runtime personal-api check-runtime       # configuration-only health check
```

- `serve` starts Uvicorn against the composed FastAPI application. It is the
  only command (with `export-openapi`) that imports FastAPI/Uvicorn; every
  shell-only path (`--help`, `--version`, no argument, invalid syntax,
  `check-runtime`) imports neither framework and performs no I/O.
- `export-openapi` renders the deterministic OpenAPI 3.1 document offline: no
  lifespan, no secret read, no socket, no PostgreSQL connection. It never
  creates parent directories and writes bytes only after the full document is
  rendered.

Exit codes (both commands): `0` success, `2` CLI syntax error, `70` unexpected
internal error, `78` configuration or secret error.

## Bind configuration

| Environment | `KNOWLEDGE_API_HOST` | `KNOWLEDGE_API_PORT` |
| --- | --- | --- |
| `local` / `test` | optional, default `127.0.0.1` | optional, default `8000` |
| `staging` / `production` | **required explicitly** | **required explicitly** |

- There is no implicit public bind: staging/production refuse to start with a
  `configuration_invalid` error naming exactly the missing fields until both
  values are supplied. An explicit `0.0.0.0` or `::` is a deployment decision,
  never a default.
- The port is validated in `1..65535`; a blank, whitespace-bearing or
  over-long host value is rejected before any socket is bound.
- Any other `KNOWLEDGE_*` environment key is a terminal
  `configuration_unknown_key` error (the API fragment adds only these two
  names to the repository allowlist).

## Route surface

| Method | Route | Production | Purpose |
| --- | --- | ---: | --- |
| `GET` | `/api/health/live` | yes | process liveness, no I/O |
| `GET` | `/api/health/ready` | yes | PostgreSQL connectivity and exact schema head |
| `GET` | `/api/openapi.json` | no | local/test raw contract document |

There is no redirecting-slash variant (`/api/health/live/` is a 404, not a
307). Unknown routes and unsupported methods return the standard error
envelope (`api_route_not_found` 404, `api_method_not_allowed` 405).

## Response, error and request-ID contract

Every application and health response uses one strict envelope
(`extra="forbid"`): `{request_id, data, warnings, error}` with exactly one of
`data`/`error` non-null. The raw local/test `/api/openapi.json` document is
the single documented exception.

- `request_id` is a server-minted UUIDv7. It is returned in the envelope body
  and in the `X-Request-ID` response header; the two are always equal.
- A `traceparent` response header carries the current W3C version-`00` trace
  context. A client-supplied `X-Client-Request-ID` is validated as a separate
  correlation value and can never replace the server request ID; an invalid
  correlation value is discarded and recorded only as a rejection event — the
  rejected value is never echoed.
- The HTTP error mapping is a closed per-code table:

| Code | HTTP | Retryable |
| --- | ---: | ---: |
| `api_request_malformed` | 400 | no |
| `api_request_validation_failed` | 422 | no |
| `api_route_not_found` | 404 | no |
| `api_method_not_allowed` | 405 | no |
| `database_connection_unavailable` | 503 | yes |
| `database_schema_contract_invalid` | 503 | no |
| `internal_error` | 500 | no |

  Validation failures expose only bounded safe `field_names`; rejected values,
  raw paths, queries, headers, cookies and exception text never enter a
  response, a log record or the OpenAPI artifact.

## Startup, liveness, readiness and schema drift

Startup order: capture the environment once, load runtime + API + database
settings and the secret-file password, configure diagnostics, build the
application and database lifecycle, then bind Uvicorn. The PostgreSQL engine
is created by the application lifespan at startup (no connection is opened
during import; readiness before the engine exists reports the safe
`database_connection_unavailable` 503).

- **Liveness** (`/api/health/live`) performs no network, filesystem, database
  or provider call and returns `200` with `{"status": "live", "service":
  "api"}` whenever the process can execute a handler. Dependency failure never
  changes liveness: during a database outage the process stays up and serves
  not-ready.
- **Readiness** (`/api/health/ready`) performs one bounded probe — acquire a
  connection, prove connectivity, read `alembic_version`, require exactly the
  one canonical head, release the connection — inside a two-second monotonic
  deadline with no internal retry. Success returns `{"status": "ready",
  "checks": {"postgresql": "ready", "schema": "ready"}}`.
- **Schema drift diagnosis**: connection refusal/timeout maps to the
  retryable `database_connection_unavailable` 503 (start the stack, check
  credentials — the process needs no restart once the database answers). A
  missing, behind, ahead or multi-head revision maps to the non-retryable
  `database_schema_contract_invalid` 503: repair the schema to the exact
  canonical head (`20260813_01`) with the repository's Alembic migrations
  before expecting readiness; restarting the process alone cannot clear it.

R2, Temporal, Redis, Qdrant and Neo4j are **not** API readiness dependencies
and are never called by the readiness route. The API's canonical state is
PostgreSQL; R2 holds immutable object bytes (a read-only one-shot
`object-storage-check-runtime` probe exists for diagnosis, not readiness);
Temporal workflow coordination is owned by the worker; Qdrant and Neo4j are
rebuildable projections. Wiring any of them into API readiness is a contract
change that requires a spec.

## OpenAPI governance

- Local/test serve the raw OpenAPI 3.1 document at `/api/openapi.json`.
  Swagger UI and ReDoc are absent in **every** environment (`docs_url=None`,
  `redoc_url=None`). Beyond that, production OpenAPI is disabled
  (`openapi_url=None`): staging and production serve no document route — the
  absent route returns the normal enveloped 404. There is no wildcard CORS and
  no `Server` version header.
- The committed generation source is `packages/api-client/openapi.json`. The
  pipeline:

```bash
uv run poe api-contract-export   # personal-api export-openapi -> committed snapshot
uv run poe api-contract-check    # stale-check + generated TypeScript check
pnpm --filter @workspace/api-client run generate      # regenerate src/generated/schema.ts
```

  `api-contract-check` (also part of `poe boundary-check` and `poe verify`)
  compares a fresh render against the committed snapshot byte-for-byte and
  verifies the generated TypeScript is current — it fails without rewriting
  the worktree. Two exports against one commit are byte-identical; a contract
  change must update routes/models, the snapshot, the generated TypeScript
  and the contract tests in one change.

## Web and plugin transport boundaries

Web and the Obsidian plugin both compile against the shared
`@workspace/api-client` package (generated types plus the transport-injected
`createApiClient({baseUrl, transport})`) and never import each other — the
boundary is enforced by ESLint `no-restricted-imports` in all three packages.
The shared client performs no automatic retry; retry policy belongs to later
feature clients. The Web adapter supplies native `fetch`; the plugin adapter
translates Obsidian `requestUrl` (pure adapter imports Obsidian types only,
so tests never load the Obsidian runtime; the runtime binding
`createObsidianApiTransport` is the only layer importing the `obsidian`
module). Neither transport logs tokens, queries, bodies or response content.
Obsidian `requestUrl` cannot be cancelled in flight — later feature code must
bound concurrency and discard late results after deadlines; an
already-aborted request is rejected before dispatch.

## Safe shutdown

On termination or cancellation Uvicorn stops accepting work, the application
lifespan closes the database engine exactly once (idempotent disposal), and
the process exits `0`. `KeyboardInterrupt`/`SystemExit` are never swallowed.
Uvicorn runs single-process (`workers=1`, `reload` off), does not trust proxy
headers and sends no framework version header.

## Prohibited logging and actions

- Never log or emit raw paths, query strings, headers, cookies, request or
  response bodies, tokens, secrets, database statements or exception text.
  Access diagnostics carry only the closed set `http_method`, `route`
  (template, or the constant `unmatched` label), `status_code`, `duration_ms`
  plus the safe correlation fields.
- Do not add auth, sync, upload or business routes, CORS, proxy trust or new
  readiness dependencies to this surface — they belong to later child specs.
- Do not hand-edit `packages/api-client/openapi.json` or
  `packages/api-client/src/generated/`; regenerate them with the commands
  above.

## Scope and next child

Implemented here: the runnable API composition root, envelopes, error
mapping, correlation, liveness/readiness, deterministic OpenAPI and the shared
generated client. Deliberately excluded: login, sessions, device
authorization, workspace resolution, sync/upload/conflict routes, business
migrations and any additional readiness dependency. The next Phase 2 child is
`web-auth-and-device-authorization-design.md` (see
`docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`,
section 17).
