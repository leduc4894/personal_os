# API Runtime and Contract Foundation Design

**Date:** 2026-08-15

**Status:** Approved design; written-spec review pending

**Parent:** `2026-08-15-phase-two-obsidian-sync-design.md` child 1

**Implementation unit:** One child spec and one implementation plan

## 1. Purpose

Phase 1 left `apps/api` as an intentionally framework-free process shell. Phase
2 needs a runnable HTTP boundary before authentication, policy, sync, upload or
conflict behavior can be implemented safely.

This child builds that boundary as a small contract spine:

- a runnable FastAPI composition root;
- framework-neutral response and error models;
- server-owned request and trace correlation;
- liveness and canonical PostgreSQL readiness;
- a deterministic committed OpenAPI snapshot; and
- one shared TypeScript API package consumed by Web and Obsidian through
  different transports.

It deliberately contains no Phase 2 business schema or business route. The
result is independently testable and is the dependency for every later child
spec.

## 2. Existing foundation

The design reuses these Phase 1 contracts instead of replacing them:

- `apps/api` owns the `personal-api` composition root and console entry point;
- shell-only CLI paths avoid configuration, network and framework imports;
- runtime settings use an exact `KNOWLEDGE_*` allowlist and fail on unknown keys;
- every operation boundary owns a new UUIDv7 request ID;
- client request IDs are validated and retained separately;
- W3C `traceparent` version `00` parsing and formatting already exist;
- diagnostics use closed events and redaction-safe structured fields;
- `ErrorCode`, `ErrorCategory`, `ErrorDefinition` and `ApplicationError` form a
  closed error registry;
- PostgreSQL connectivity and Alembic-head checks already have canonical
  semantics; and
- the API process must not use R2 as a normal readiness dependency.

## 3. Scope

### 3.1 Included

- `personal-api serve` and an injectable FastAPI application factory.
- `personal-api export-openapi` for deterministic offline contract export.
- API bind configuration and environment-specific defaults.
- Request-context, safe access-observation and exception middleware.
- Success, warning and error envelopes represented by strict Pydantic models.
- Closed HTTP mappings for every error that this child can expose.
- `/api/health/live` and `/api/health/ready`.
- Local/test `/api/openapi.json`.
- A committed normalized OpenAPI snapshot.
- A new pnpm workspace package at `packages/api-client`.
- Generated TypeScript contracts and a transport-injected typed API client.
- Native-fetch and Obsidian `requestUrl` transport adapters with shared contract
  tests.
- Unit, ASGI contract, PostgreSQL integration, generation and security tests.
- Documentation of the operator-facing runtime and contract commands.

### 3.2 Excluded

- Login, password, TOTP, sessions, device authorization or token rotation.
- Workspace resolution and authorization middleware.
- Admin UI or any other Web page.
- Source, sync, upload, download, event, reconciliation or conflict routes.
- Any Phase 2 business table or Alembic migration.
- R2, Temporal, Redis, Qdrant or Neo4j readiness checks.
- Worker, multipart or Cloudflare Worker behavior.
- Swagger UI, ReDoc, wildcard CORS or a public production OpenAPI endpoint.
- API URL generation markers such as `/v1`.
- Automatic HTTP retries in the shared client.
- Mutation testing.

## 4. Chosen architecture

The selected approach is a runnable contract spine. A contract-only library
would not prove real FastAPI behavior, while a complete Phase 2 route skeleton
would create placeholders and couple later child specs prematurely.

```text
apps/web                              apps/obsidian-plugin
native fetch adapter                  requestUrl adapter
          \                            /
           packages/api-client
           generated paths/components/operations
           typed ApiClient + injected transport
                         |
                  /api/... HTTP
                         |
                   apps/api
          CLI -> app factory -> middleware -> routes
                         |
         framework-neutral personal_os.api_contracts
                         |
          PostgreSQL readiness adapter (only for /ready)
```

### 4.1 Boundary rules

- `personal_os.api_contracts` may import Pydantic and existing core diagnostic
  and error contracts. It must not import FastAPI, Uvicorn, SQLAlchemy, a
  database driver or a provider SDK.
- `apps/api` is the only Python composition root in this child that imports
  FastAPI or Uvicorn.
- PostgreSQL readiness implementation remains in the PostgreSQL adapter package
  and is injected behind a framework-neutral protocol.
- `packages/api-client` imports neither Web nor Obsidian.
- Web and plugin may import `packages/api-client`, but never each other.
- The plugin-specific transport is the only layer that imports Obsidian APIs.

### 4.2 Component responsibilities

| Unit | Responsibility | Dependencies |
|---|---|---|
| API command shell | Parse `serve` and `export-openapi` without weakening existing shell-only paths | shared command shell |
| App factory | Build routes, middleware, exception handlers and lifespan from injected settings/dependencies | FastAPI, API contracts |
| Server runtime | Bind Uvicorn and own graceful startup/shutdown | Uvicorn, app factory |
| API contracts | Strict envelopes, warnings, errors, health data and readiness protocol | Pydantic, core contracts |
| HTTP error mapper | Map approved stable codes to status without leaking exceptions | error registry |
| PostgreSQL readiness adapter | Prove connectivity and exact Alembic head | SQLAlchemy/PostgreSQL adapter |
| OpenAPI exporter | Normalize and serialize the app schema offline | app factory |
| API client package | Generated contract and typed transport-injected client | openapi-typescript, openapi-fetch |
| Web transport | Supply native `fetch` | Web runtime |
| Plugin transport | Adapt Obsidian `requestUrl` to the client transport | Obsidian API |

The implementation plan may refine filenames, but it must preserve these
responsibilities and import directions.

## 5. Command and runtime behavior

### 5.1 CLI isolation

The existing behavior remains a compatibility contract:

- `personal-api --help`;
- `personal-api --version`;
- no argument;
- invalid syntax; and
- `personal-api check-runtime`

must not import FastAPI or Uvicorn and must not open a socket or database
connection. Only `serve` and `export-openapi` may lazy-import the framework.

### 5.2 `serve`

`personal-api serve` performs this ordered flow:

1. Load the exact runtime and API-server configuration snapshots.
2. Reject invalid or unknown configuration before binding a socket.
3. Construct the app and dependencies without connecting during import.
4. Bind Uvicorn to the validated host and port.
5. Create the PostgreSQL pool through the FastAPI lifespan.
6. Serve requests.
7. On cancellation or termination, stop accepting work and close the pool
   through the same lifespan.

Database unavailability after valid configuration does not crash-loop the API.
The process remains live and reports not-ready.

Uvicorn reload, multi-process worker management, TLS termination and trusted
proxy headers are not enabled here. Deployment owns process scaling and TLS.
The server header must not disclose a framework version.

### 5.3 API settings

The repository allowlist adds exactly:

```text
KNOWLEDGE_API_HOST
KNOWLEDGE_API_PORT
```

The settings model is frozen, uses `extra="forbid"`, and validates port range
`1..65535`.

| Environment | Host | Port |
|---|---|---|
| `local` / `test` | default `127.0.0.1`; explicit override allowed | default `8000`; explicit override allowed |
| `staging` / `production` | required explicitly | required explicitly |

There is no implicit public bind. An explicit `0.0.0.0` or `::` is therefore a
deployment decision, not a default. Secrets never travel through these fields.

### 5.4 `export-openapi`

`personal-api export-openapi --output <path>` builds the same route graph but
does not start lifespan, read secrets, open a socket or connect to PostgreSQL.
The exporter uses fixed test-environment app metadata, calls the app's OpenAPI
generator, normalizes the result and writes UTF-8 JSON with a final newline.

Failure reports a safe command error. A raw output path or exception string is
not emitted to diagnostics.

## 6. HTTP route surface

The initial route surface is closed:

| Method | Route | Auth | Production | Purpose |
|---|---|---:|---:|---|
| `GET` | `/api/health/live` | none | yes | process liveness without I/O |
| `GET` | `/api/health/ready` | none | yes | PostgreSQL connectivity and schema-head readiness |
| `GET` | `/api/openapi.json` | none | no | local/test contract inspection |

There is no redirecting slash variant. Unknown routes and unsupported methods
use the standard error envelope.

FastAPI is configured with `docs_url=None` and `redoc_url=None` in every
environment. Production also uses `openapi_url=None`; local/test use
`openapi_url="/api/openapi.json"`.

## 7. Response contract

All public responses use one strict outer shape.

### 7.1 Success

```json
{
  "request_id": "0198...",
  "data": {},
  "warnings": [],
  "error": null
}
```

### 7.2 Failure

```json
{
  "request_id": "0198...",
  "data": null,
  "warnings": [],
  "error": {
    "code": "stable_error_code",
    "message": "Safe fixed message",
    "retryable": false,
    "details": {}
  }
}
```

The models use `extra="forbid"`. `request_id` is a UUID in OpenAPI and canonical
UUID text in JSON. `data` is route-specific. A response never has both non-null
`data` and non-null `error`.

Warnings use `{code, message, details}` with the same safe-detail grammar as
errors but no retryability. This child defines the schema only; all routes in
this child return an empty warning list. Later child specs must register any
public warning vocabulary before using it.

Allowed detail values are JSON scalar values or bounded arrays of safe strings.
Arbitrary nested objects, paths, raw inputs and exception payloads are rejected.

### 7.3 Health payloads

Liveness success data is:

```json
{"status": "live", "service": "api"}
```

Readiness success data is:

```json
{
  "status": "ready",
  "checks": {
    "postgresql": "ready",
    "schema": "ready"
  }
}
```

Failed readiness uses `data: null` and the precise error envelope rather than a
partially successful data object.

## 8. Request and trace correlation

Middleware establishes one existing `DiagnosticContext` before routing:

1. Generate a fresh server-owned UUIDv7 request ID.
2. Validate optional `X-Client-Request-ID` as a separate correlation value.
3. Resolve optional W3C `traceparent` through the existing strict version-`00`
   implementation.
4. Bind the context for route, adapter, error and access diagnostics.
5. Return the server request ID in both `X-Request-ID` and the envelope.
6. Return the current formatted `traceparent` response header.
7. Clear the context in `finally`.

An invalid client request ID or traceparent is discarded and recorded only by
the existing safe diagnostic event. The rejected value is never echoed.

Access observations contain only the HTTP method, matched route template,
status, duration and safe correlation fields. They do not contain the raw path,
query string, headers, cookies, body, response data or exception text. An
unmatched route uses one constant template label rather than its attacker-owned
path.

## 9. Error mapping

Framework-generated errors are normalized before leaving the app. This child
adds these transport error codes to the shared closed registry:

| Error code | Category | Retryable | HTTP | Safe message | Allowed details |
|---|---|---:|---:|---|---|
| `api_request_malformed` | validation | no | 400 | `The API request is malformed` | none |
| `api_request_validation_failed` | validation | no | 422 | `The API request failed validation` | `field_names` |
| `api_route_not_found` | validation | no | 404 | `The requested API route does not exist` | none |
| `api_method_not_allowed` | validation | no | 405 | `The API route does not allow this method` | none |

Existing codes used here map as follows:

| Error code | HTTP | Behavior |
|---|---:|---|
| `database_connection_unavailable` | 503 | retryable not-ready result |
| `database_schema_contract_invalid` | 503 | non-retryable until schema is repaired |
| `internal_error` | 500 | fixed safe message, empty details |

The mapper is a closed per-code table, not a category-to-status heuristic. Every
code that a route declares must have exactly one tested mapping. If an
unexpected or unmapped exception crosses the boundary, the response is
`internal_error`; the original exception remains internal and follows the safe
diagnostic contract.

Later child specs own their auth, policy, upload and sync error codes and must
extend this table explicitly. They may not reinterpret an existing code.

FastAPI validation locations are reduced to bounded safe field names. Rejected
values and framework exception messages are not returned.

## 10. Health semantics

### 10.1 Liveness

`/api/health/live` performs no network, filesystem, database or provider call.
It returns `200` when the app can execute the request handler. Dependency
failure never changes liveness.

### 10.2 Readiness

`/api/health/ready` performs one bounded probe with a two-second total monotonic
deadline and no internal retry:

1. Acquire a PostgreSQL connection.
2. Prove connectivity.
3. Read the current Alembic revision.
4. Require the one canonical repository head.
5. Release the connection on success, failure or cancellation.

Missing revision state, a revision behind or ahead, multiple heads and contract
drift map to `database_schema_contract_invalid`. Connection refusal, timeout or
pool unavailability map to `database_connection_unavailable`.

R2, Temporal, Redis, Qdrant and Neo4j are never called by this readiness route.
The probe dependency is injectable, so ASGI contract tests do not need a real
database; a separate integration suite proves the real adapter.

## 11. OpenAPI governance

The committed source for TypeScript generation is:

```text
packages/api-client/openapi.json
```

The schema uses OpenAPI 3.1 and declares release version in `info.version`.
Version does not appear in route paths. Each operation has a manually assigned,
semantic and stable `operationId`.

Normalization must:

- sort object keys recursively;
- preserve array order where OpenAPI defines order;
- omit environment-specific `servers` values;
- omit timestamps, hostnames, paths and build-machine values;
- use deterministic model names; and
- emit UTF-8, two-space indentation and one final newline.

The same export run twice against one commit must be byte-identical. API changes
must update route/models, snapshot, generated TypeScript and contract tests in
one change.

## 12. Shared TypeScript client

`packages/api-client` is added to `pnpm-workspace.yaml` and exports:

- generated `paths`, `components` and `operations` types;
- the common envelope/error type aliases;
- a typed `createApiClient({baseUrl, transport})` factory; and
- narrow response helpers that preserve error results instead of throwing away
  the envelope.

Generated schema source is committed at
`packages/api-client/src/generated/schema.ts`; hand-written modules must not be
placed beneath `src/generated/`.

`openapi-typescript` generates immutable, runtime-free contract types from the
local committed snapshot. `openapi-fetch` supplies the typed request client and
accepts the injected fetch-compatible transport. Generator input is never a
remote URL.

The package does not retry automatically. Retry policy depends on operation
semantics, idempotency and the server's `retryable` field, so later feature
clients own it.

The Web adapter supplies native `fetch`. The plugin adapter translates Obsidian
`requestUrl` inputs and outputs without importing Obsidian into the shared
package. Both adapters must preserve method, URL, headers, body, response status
and bytes; neither logs token, query, body or response content.

Both generated source and the snapshot are committed. Web/plugin builds never
need Python or a running API server.

## 13. Contract pipeline

One deterministic repository task runs this sequence:

```text
FastAPI route/models
  -> offline OpenAPI export
  -> normalized openapi.json
  -> committed-diff check
  -> openapi-typescript generation/check
  -> TypeScript strict type-check
  -> Web and plugin transport contract tests
```

The check form must fail without rewriting the worktree when either the snapshot
or generated source is stale. The implementation plan will choose task names
consistent with the existing Poe and pnpm scripts.

## 14. Security and privacy invariants

- Health endpoints are unauthenticated but reveal only closed status/error data.
- No endpoint in this child accepts a bearer token or session cookie.
- CORS middleware is absent; wildcard origin is forbidden.
- Production exposes no OpenAPI, Swagger UI or ReDoc route.
- Uvicorn does not disclose its version in the `Server` header.
- Proxy headers are not trusted until a deployment contract names trusted
  proxies.
- Raw path, query, headers, cookies, request/response body, token, secret,
  database error and provider text never enter logs or error envelopes.
- Request IDs remain server-owned and cannot be selected by a client.
- Error details are validated against each registry allowlist.
- OpenAPI generation reads no secret and contacts no network service.

## 15. Dependencies

The child is authorized to add only these production/runtime roles:

- FastAPI for the Python HTTP adapter;
- Uvicorn for the API server; and
- `openapi-fetch` for the shared TypeScript client.

`openapi-typescript` is a development-only generator. Versions are selected for
the repository's pinned Python 3.14, Node 24 and pnpm 10 toolchain, then locked
in `uv.lock`/`pnpm-lock.yaml`. No infrastructure service is added.

Official behavior references:

- FastAPI OpenAPI/docs URL controls:
  <https://fastapi.tiangolo.com/tutorial/metadata/>
- openapi-typescript CLI and stale-output check:
  <https://openapi-ts.dev/cli>
- openapi-fetch custom transport option:
  <https://openapi-ts.dev/openapi-fetch/api>

## 16. Test strategy

### 16.1 Unit

- API settings defaults, required production fields and port/host rejection.
- Envelope invariants and strict extra-field rejection.
- Warning/detail safe-value grammar.
- Exhaustive mappings for every error exposed by this child.
- Request ID, client request ID and traceparent handling.
- OpenAPI normalization and byte-stable serialization.

### 16.2 ASGI contract

- Liveness and ready success envelopes.
- Malformed request, validation failure, unknown route and wrong method.
- Unexpected exception becomes safe `internal_error`.
- Body and `X-Request-ID` contain the same server UUIDv7.
- Response `traceparent` is valid and client IDs cannot replace request IDs.
- Production has no OpenAPI/docs routes; local/test has only OpenAPI JSON.
- No wildcard CORS or framework-version server header.

### 16.3 PostgreSQL and server integration

- Current-head database returns ready.
- Connection failure/timeout returns retryable 503.
- Missing, behind, ahead or divergent revision returns non-retryable 503.
- Liveness stays 200 throughout database failure.
- Uvicorn starts on loopback, serves health and shuts down cleanly.
- Cancellation releases the readiness connection and lifespan closes the pool.

### 16.4 Generated-client contract

- Two clean exports are byte-identical.
- A backend contract change without regenerated artifacts fails the gate.
- `openapi-typescript --check` and TypeScript strict mode pass.
- Web native-fetch and plugin requestUrl adapters satisfy the same transport
  suite.
- Both consumer apps resolve the workspace package without importing each
  other.

### 16.5 Sensitive-data regression

Sentinels are placed in headers, query strings, invalid bodies, client IDs,
database exceptions and transport failures. None may appear in responses,
logs, traces, test reports or the OpenAPI artifact.

Mutation testing remains explicitly deferred by the parent Phase 2 decision.

## 17. Acceptance criteria

1. Existing shell-only API CLI paths retain their no-framework/no-I/O boundary.
2. `personal-api serve` starts a FastAPI app through an injectable factory.
3. Invalid configuration fails before socket bind; DB outage leaves the process
   live but not ready.
4. Liveness performs no external I/O.
5. Readiness checks only PostgreSQL connectivity and exact schema head within
   two seconds and cleans up on cancellation.
6. Every public success and failure has the approved envelope.
7. Every framework-generated error is normalized to a stable safe code.
8. Request correlation follows the existing UUIDv7 and W3C contracts.
9. Production exposes neither OpenAPI nor interactive API documentation.
10. The OpenAPI snapshot is deterministic, committed and stale-diff guarded.
11. Generated TypeScript is strict, committed and generated only from the local
    snapshot.
12. Web and plugin compile against one shared API package through independent
    transports.
13. No sensitive sentinel escapes through HTTP, diagnostics or generated files.
14. No auth/sync/upload route, business migration or additional readiness
    dependency is introduced.
15. Focused unit, contract and integration gates plus repository lint/type/build
    checks are green before completion.

## 18. Deferred ownership

| Deferred behavior | Owning child |
|---|---|
| Web login, sessions, device flow, tokens and Admin | `web-auth-and-device-authorization-design.md` |
| Workspace policy and exclusions | `exclusion-policy-publication-design.md` |
| Small-file sync routes and offline journal | `plugin-journal-and-small-file-sync-design.md` |
| Rename/move/delete/restore schema and APIs | `source-locator-and-tombstone-lifecycle-design.md` |
| Cursor, events and manifest reconciliation | `device-cursor-and-manifest-reconciliation-design.md` |
| Multipart upload and staging cleanup | `resumable-multipart-mobile-upload-design.md` |
| Conflict capture and resolution | `source-conflict-capture-and-resolution-design.md` |
| Phase-wide live-device and recovery acceptance | `obsidian-sync-acceptance-and-operations-design.md` |

The next child may extend the API surface only after this contract foundation
is implemented and its generated-client gate is green.

## 19. Visual archive

- `html/2026-08-15-api-runtime-and-contract-foundation-architecture.html`
- `html/2026-08-15-api-runtime-and-contract-foundation-response-errors.html`
- `html/2026-08-15-api-runtime-and-contract-foundation-runtime-health.html`
- `html/2026-08-15-api-runtime-and-contract-foundation-openapi-client.html`
- `html/2026-08-15-api-runtime-and-contract-foundation-testing-acceptance.html`
