# API Runtime and Contract Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Phase 1 API process shell into a runnable, privacy-safe FastAPI boundary with deterministic OpenAPI and one generated TypeScript client shared by Web and Obsidian, without adding any authentication or sync business behavior.

**Architecture:** Framework-neutral envelopes, health values and readiness protocols live under `personal_os.api_contracts`; FastAPI/Uvicorn composition remains under `apps/api`; PostgreSQL readiness remains in `postgresql-source-store`. A normalized committed OpenAPI snapshot under `packages/api-client` generates immutable TypeScript types, while `openapi-fetch` receives either the Web native-fetch adapter or the Obsidian `requestUrl` adapter.

**Tech Stack:** Python 3.14.6, uv 0.11.32, FastAPI 0.139.2, Uvicorn 0.51.0, HTTPX 0.28.1 (test only), Pydantic 2.13.4, SQLAlchemy 2.0.51 async Core, psycopg 3.3.4, PostgreSQL 18.4, Node 24.18.x, pnpm 10.34.0, TypeScript 6.0.3 strict, openapi-typescript 7.13.0, openapi-fetch 0.17.0, Vitest 4.1.10.

**Normative spec:** `docs/superpowers/specs/2026-08-15-api-runtime-and-contract-foundation-design.md` at commit `b4c6b1c`. Section references below refer to that document. The umbrella remains `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`.

## Global Constraints

- Implement this child only: no password/TOTP/session/device flow, Admin UI, workspace authorization, source/sync/upload/download/event/conflict route, Phase 2 table, Alembic revision, Worker, multipart or Cloudflare Worker behavior (spec 3.2).
- The public route set is exactly `GET /api/health/live`, `GET /api/health/ready`, plus local/test-only `GET /api/openapi.json`; no slash-redirect alias and no `/v1` path (spec 6).
- `personal_os.api_contracts` may import Pydantic and existing core contracts only; it must not import FastAPI, Uvicorn, SQLAlchemy, Psycopg or provider packages. FastAPI/Uvicorn remain inside `api_runtime`; SQLAlchemy/Psycopg readiness remains inside `postgresql_source_store` (spec 4.1).
- Existing API shell paths (`--help`, `--version`, no argument, invalid syntax and `check-runtime`) must not import FastAPI/Uvicorn, read configuration/secrets or open network/database resources (spec 5.1).
- Add exact Python pins `fastapi==0.139.2`, `uvicorn==0.51.0`, `httpx==0.28.1` (dev only) and exact TypeScript pins `openapi-typescript==7.13.0`, `openapi-fetch==0.17.0`; preserve every existing exact pin and commit both lockfiles.
- API settings add only `KNOWLEDGE_API_HOST` and `KNOWLEDGE_API_PORT`; local/test default to `127.0.0.1:8000`, while staging/production require both explicitly. Port range is `1..65535` and no environment gets an implicit public bind (spec 5.3).
- Liveness performs zero network/filesystem/database/provider I/O. Readiness performs one PostgreSQL connectivity plus exact-schema-head probe inside one 2-second monotonic deadline with no retry; cancellation releases the connection (spec 10).
- All application and health responses use the exact envelope `{request_id,data,warnings,error}`. The raw local/test `/api/openapi.json` document is the sole generator-required exception; it still receives correlation headers. Request ID is fresh server-owned UUIDv7 in every envelope and `X-Request-ID`; response `traceparent` is formatted from the bound diagnostic context (spec 7-8).
- HTTP status is selected by a closed per-code table. Do not infer status from category. Framework validation, unknown route, unsupported method and unexpected exception must use the same safe envelope (spec 9).
- CORS middleware is absent. Swagger and ReDoc are disabled in every environment. Production sets `openapi_url=None`; trusted proxy headers and framework version server headers remain disabled (spec 6, 14).
- Do not log or serialize raw path, query, header, cookie, body, response data, token, secret, database/provider exception or rejected client correlation input. Access observations contain only closed method/route values, status, duration and correlation (spec 8, 14).
- `personal-api export-openapi` must read no environment or secret, open no socket/database, and produce recursively key-sorted UTF-8 JSON with two-space indentation and one final newline. Two exports from one commit must be byte-identical (spec 5.4, 11).
- `packages/api-client/openapi.json` and `packages/api-client/src/generated/schema.ts` are committed. TypeScript generation uses only the local snapshot and the check path must detect staleness without rewriting the worktree (spec 11-13).
- The shared client performs no automatic retry. Web and plugin import `@workspace/api-client`, never each other; the shared package imports neither app nor Obsidian (spec 12).
- Use TDD for every behavior: write the named failing test, run it and read the expected failure, add the minimum implementation, rerun the focused test, then run affected lint/type/contract gates before each task commit.
- Naming follows `AGENTS.md`: semantic domain/role names, no purely ordinal names, booleans start with `is`/`has`/`can`/`should`, quantities include units, Python is fully typed for mypy strict and TypeScript stays strict.
- Preserve unrelated user changes, especially the unstaged Phase 1 spec moves. Stage only files named by the active task.

---

## File Structure

### Framework-neutral Python contracts

```text
src/personal_os/
├── api_contracts/
│   ├── __init__.py              Public API-contract exports
│   ├── envelopes.py             Strict generic response, warning and error models
│   ├── errors.py                API transport error type and closed HTTP status map
│   ├── health.py                Health data values and readiness probe protocol
│   └── request_values.py        Closed HTTP method/route values used by diagnostics
└── database_schema.py           Canonical PostgreSQL revision constant shared safely
```

### API composition root

```text
apps/api/src/api_runtime/
├── command.py                   Lazy CLI dispatch for check-runtime/serve/export
├── server_settings.py           Exact API host/port settings loader
├── application.py               FastAPI factory, route registration and handlers
├── request_context.py           Pure-ASGI correlation and safe access middleware
├── health_routes.py             Liveness/readiness handlers only
├── database_lifecycle.py        Engine/readiness lifecycle composition
├── server.py                    Uvicorn configuration and process runner
└── openapi_export.py            Offline schema normalization and file export
```

### PostgreSQL adapter

```text
packages/postgresql-source-store/src/postgresql_source_store/
└── readiness.py                 Connectivity/revision probe and safe SQL error mapping
```

### Shared TypeScript client and consumer transports

```text
packages/api-client/
├── package.json
├── tsconfig.json
├── eslint.config.mjs
├── vitest.config.ts
├── openapi.json
└── src/
    ├── generated/schema.ts      Generated only; never hand edited
    ├── client.ts                Typed client factory and transport type
    ├── envelopes.ts             Stable aliases/helpers over generated components
    ├── client.test.ts
    └── index.ts

apps/web/src/api/
├── native-fetch-transport.ts
└── native-fetch-transport.test.ts

apps/obsidian-plugin/src/api/
├── request-url-transport.ts
├── request-url-transport.test.ts
└── obsidian-api-transport.ts
```

### Verification and documentation

```text
tests/unit/api_contracts/
tests/unit/api_runtime/
tests/unit/postgresql_source_store/test_readiness.py
tests/contract/api/
tests/integration/canonical_core/test_api_readiness_integration.py
tools/api_contract_artifacts.py
docs/operations/api-runtime-contract.md
docs/handoff/2026-08-15-api-runtime-contract-foundation.md
```

`tests/contract/test_architecture_boundaries.py` must add `packages/api-client/src` to the scanned TypeScript roots and prove that the shared package imports neither consumer.

---

### Task 1: Pin API dependencies and add exact server settings

**Files:**
- Modify: `apps/api/pyproject.toml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/personal_os/runtime_configuration/environment_names.py`
- Create: `apps/api/src/api_runtime/server_settings.py`
- Modify: `tests/unit/runtime_configuration/test_environment_names.py`
- Create: `tests/unit/api_runtime/test_server_settings.py`

**Interfaces:**
- Consumes: `RuntimeEnvironment`, `ConfigurationError`, `ErrorCode.CONFIGURATION_INVALID`, `ErrorCode.CONFIGURATION_UNKNOWN_KEY`, `KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES`.
- Produces: `API_SERVER_ENVIRONMENT_NAMES`, `ApiServerSettings(environment: RuntimeEnvironment, host: str, port: int)`, `load_api_server_settings(*, environ: Mapping[str, str] | None = None) -> ApiServerSettings`.

- [ ] **Step 1: Write the failing allowlist and settings tests**

Add exact assertions to `tests/unit/runtime_configuration/test_environment_names.py` and create `tests/unit/api_runtime/test_server_settings.py`:

```python
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.models import RuntimeEnvironment
from api_runtime.server_settings import load_api_server_settings


def test_api_environment_names_are_registered() -> None:
    assert {"KNOWLEDGE_API_HOST", "KNOWLEDGE_API_PORT"} <= (
        KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    )


def test_local_api_settings_default_to_loopback() -> None:
    settings = load_api_server_settings(environ={"KNOWLEDGE_ENVIRONMENT": "local"})
    assert settings.environment is RuntimeEnvironment.LOCAL
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_remote_environment_requires_explicit_host_and_port(environment: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_api_server_settings(environ={"KNOWLEDGE_ENVIRONMENT": environment})
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID
    assert set(raised.value.safe_details["field_names"]) == {
        SafeToken.parse("host"),
        SafeToken.parse("port"),
    }


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_api_port_outside_bind_range_is_rejected(port: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_api_server_settings(
            environ={"KNOWLEDGE_ENVIRONMENT": "test", "KNOWLEDGE_API_PORT": port}
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID
```

- [ ] **Step 2: Run the tests and confirm the red state**

Run:

```powershell
uv run pytest tests/unit/runtime_configuration/test_environment_names.py tests/unit/api_runtime/test_server_settings.py -q
```

Expected: collection fails because `api_runtime.server_settings` and the two environment names do not exist.

- [ ] **Step 3: Add the settings fragment and exact dependency pins**

Use the loader pattern from `postgresql_source_store.settings`: capture the environment at call time, reject unknown `KNOWLEDGE_*` names, supply loopback defaults only for local/test, and convert Pydantic errors to registered safe field names.

Core implementation shape:

```python
API_SERVER_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "environment",
        "KNOWLEDGE_API_HOST": "host",
        "KNOWLEDGE_API_PORT": "port",
    }
)


class ApiServerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)
    environment: RuntimeEnvironment
    host: str
    port: int


def load_api_server_settings(
    *, environ: Mapping[str, str] | None = None
) -> ApiServerSettings:
    source = dict(os.environ if environ is None else environ)
    # Apply 127.0.0.1:8000 only when environment is local/test; staging and
    # production add missing host/port to the safe field_names tuple.
```

Set `apps/api/pyproject.toml` dependencies exactly to:

```toml
dependencies = [
  "fastapi==0.139.2",
  "knowledge-core==0.1.0",
  "postgresql-source-store==0.1.0",
  "uvicorn==0.51.0",
]
```

Add `httpx==0.28.1` to the root dev group, then run `uv lock` once. Do not add `fastapi[standard]`, reload extras or another HTTP server.

- [ ] **Step 4: Run focused settings and regression gates**

Run:

```powershell
uv run pytest tests/unit/runtime_configuration tests/unit/api_runtime/test_server_settings.py -q
uv run poe python-lint
uv run poe python-type-check
uv run pytest tests/contract/test_command_import_side_effects.py -q
```

Expected: all commands exit `0`; shell import tests remain green even though FastAPI/Uvicorn are installed.

- [ ] **Step 5: Commit the settings deliverable**

```powershell
git add apps/api/pyproject.toml pyproject.toml uv.lock src/personal_os/runtime_configuration/environment_names.py apps/api/src/api_runtime/server_settings.py tests/unit/runtime_configuration/test_environment_names.py tests/unit/api_runtime/test_server_settings.py
git commit -m "feat: add api server settings"
```

---

### Task 2: Add framework-neutral envelopes and HTTP error vocabulary

**Files:**
- Create: `src/personal_os/api_contracts/__init__.py`
- Create: `src/personal_os/api_contracts/envelopes.py`
- Create: `src/personal_os/api_contracts/errors.py`
- Create: `src/personal_os/api_contracts/health.py`
- Create: `src/personal_os/api_contracts/request_values.py`
- Modify: `src/personal_os/error_contracts/codes.py`
- Modify: `src/personal_os/error_contracts/exceptions.py`
- Modify: `src/personal_os/error_contracts/__init__.py`
- Create: `tests/unit/api_contracts/test_envelopes.py`
- Create: `tests/unit/api_contracts/test_http_errors.py`
- Create: `tests/unit/api_contracts/test_health_contracts.py`
- Modify: `tests/contract/test_architecture_boundaries.py`

**Interfaces:**
- Consumes: `ApplicationError`, `ERROR_DEFINITIONS`, `ErrorCategory`, `ErrorCode`, `SafeToken`, existing safe-detail serialization.
- Produces: `ApiWarning`, `ApiErrorBody`, `ApiEnvelope[DataT]`, `success_envelope(...)`, `error_envelope(...)`, `ApiTransportError`, `HTTP_ERROR_STATUSES`, `LivenessData`, `ReadinessChecks`, `ReadinessData`, `CanonicalDatabaseReadinessProbe`, `ApiHttpMethod`, `ApiRouteTemplate`.

- [ ] **Step 1: Write failing registry, envelope and import-boundary tests**

Pin the four new codes exactly:

```python
EXPECTED_API_ERRORS = {
    "api_request_malformed": (ErrorCategory.VALIDATION, False, "The API request is malformed", frozenset()),
    "api_request_validation_failed": (
        ErrorCategory.VALIDATION,
        False,
        "The API request failed validation",
        frozenset({"field_names"}),
    ),
    "api_route_not_found": (ErrorCategory.VALIDATION, False, "The requested API route does not exist", frozenset()),
    "api_method_not_allowed": (ErrorCategory.VALIDATION, False, "The API route does not allow this method", frozenset()),
}
```

Pin the status table:

```python
def test_http_status_map_is_closed_for_child_one() -> None:
    assert HTTP_ERROR_STATUSES == {
        ErrorCode.API_REQUEST_MALFORMED: 400,
        ErrorCode.API_REQUEST_VALIDATION_FAILED: 422,
        ErrorCode.API_ROUTE_NOT_FOUND: 404,
        ErrorCode.API_METHOD_NOT_ALLOWED: 405,
        ErrorCode.DATABASE_CONNECTION_UNAVAILABLE: 503,
        ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID: 503,
        ErrorCode.INTERNAL_ERROR: 500,
    }
```

Pin the envelope XOR invariant and safe serialization:

```python
def test_success_and_error_envelopes_are_mutually_exclusive() -> None:
    request_id = uuid7()
    success = success_envelope(request_id=request_id, data=LivenessData())
    assert success.model_dump(mode="json") == {
        "request_id": str(request_id),
        "data": {"status": "live", "service": "api"},
        "warnings": [],
        "error": None,
    }
    with pytest.raises(ValidationError):
        ApiEnvelope[LivenessData](
            request_id=request_id,
            data=LivenessData(),
            warnings=(),
            error=ApiErrorBody(
                code=ErrorCode.INTERNAL_ERROR,
                message="An unexpected internal error occurred",
                retryable=False,
                details={},
            ),
        )
```

Add an AST/import-linter assertion that every module under `personal_os.api_contracts` rejects imports of `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg` and provider packages.

- [ ] **Step 2: Run tests and confirm missing contracts**

```powershell
uv run pytest tests/unit/api_contracts tests/contract/test_architecture_boundaries.py -q
```

Expected: collection fails on the absent `personal_os.api_contracts` package and API error enum members.

- [ ] **Step 3: Implement the strict models, protocol and error mapper**

Use concrete response models in route annotations later; keep the generic envelope framework-neutral:

```python
DataT = TypeVar("DataT")
type ApiDetailValue = bool | int | str | tuple[bool | int | str, ...]


class ApiErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: ErrorCode
    message: str
    retryable: bool
    details: Mapping[str, ApiDetailValue]


class ApiEnvelope(BaseModel, Generic[DataT]):
    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: UUID
    data: DataT | None
    warnings: tuple[ApiWarning, ...] = ()
    error: ApiErrorBody | None

    @model_validator(mode="after")
    def require_one_outcome(self) -> ApiEnvelope[DataT]:
        if (self.data is None) == (self.error is None):
            raise ValueError("exactly one of data or error must be present")
        return self
```

Define the unused-but-stable warning schema now without inventing warning
values: `code` matches `^[a-z0-9][a-z0-9._:-]{0,63}$`, `message` is `1..160`
characters, and `details` uses the same `ApiDetailValue` grammar. Health values
are exact frozen models:

```python
class LivenessData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["live"] = "live"
    service: Literal["api"] = "api"


class ReadinessChecks(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    postgresql: Literal["ready"] = "ready"
    schema: Literal["ready"] = "ready"


class ReadinessData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ready"] = "ready"
    checks: ReadinessChecks = Field(default_factory=ReadinessChecks)
```

Add `ApiTransportError(ApplicationError)` with exactly the four new API codes
in `allowed_codes`. Store `HTTP_ERROR_STATUSES` in a `MappingProxyType` and
reject attempts to map a code not listed in the approved seven-code table.

`error_envelope` must call `ApplicationError.to_safe_dict()` and copy only its registered values; it never accepts an arbitrary message/details mapping. Define:

```python
@runtime_checkable
class CanonicalDatabaseReadinessProbe(Protocol):
    async def check(self) -> None: ...
```

`ApiRouteTemplate` is a closed `StrEnum` with exact values `/api/health/live`, `/api/health/ready`, `/api/openapi.json`, and `unmatched`; `ApiHttpMethod` initially contains `GET` and `OTHER`. These closed enum values are safe diagnostic scalars even though route values contain `/`.

- [ ] **Step 4: Run unit, boundary, lint and type gates**

```powershell
uv run pytest tests/unit/api_contracts tests/unit/error_contracts -q
uv run poe python-lint
uv run poe python-type-check
uv run poe boundary-check
```

Expected: all commands exit `0`, and core still has no FastAPI or SQLAlchemy import.

- [ ] **Step 5: Commit the core contract deliverable**

```powershell
git add src/personal_os/api_contracts src/personal_os/error_contracts tests/unit/api_contracts tests/contract/test_architecture_boundaries.py
git commit -m "feat: define api response contracts"
```

---

### Task 3: Implement the bounded PostgreSQL readiness adapter

**Files:**
- Create: `src/personal_os/database_schema.py`
- Modify: `src/personal_os/recovery/contracts.py`
- Modify: `src/personal_os/recovery/__init__.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/readiness.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Create: `tests/unit/postgresql_source_store/test_readiness.py`
- Modify: `tests/unit/recovery/test_contracts.py`

**Interfaces:**
- Consumes: `CanonicalDatabaseReadinessProbe`, `DatabaseMigrationError`, `ErrorCode.DATABASE_CONNECTION_UNAVAILABLE`, `ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID`, `AsyncEngine`.
- Produces: `CANONICAL_POSTGRESQL_SCHEMA_REVISION = "20260813_01"`, compatibility alias `POSTGRESQL_SCHEMA_REVISION`, `PostgresqlReadinessProbe(engine: AsyncEngine, expected_revision: str = CANONICAL_POSTGRESQL_SCHEMA_REVISION)`, `await PostgresqlReadinessProbe.check() -> None`.

- [ ] **Step 1: Write failing adapter tests with a scripted async engine**

Cover exact head, missing/behind/ahead/multiple revisions, connection failure and cancellation. The core assertions are:

```python
@pytest.mark.asyncio
async def test_readiness_accepts_connectivity_and_exact_single_head() -> None:
    engine = ScriptedEngine(connectivity=1, revisions=("20260813_01",))
    await PostgresqlReadinessProbe(engine).check()
    assert engine.executed_sql == [
        "SELECT 1",
        "SELECT version_num FROM public.alembic_version ORDER BY version_num",
    ]
    assert engine.connection_was_closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("revisions", [(), ("older",), ("future",), ("a", "b")])
async def test_readiness_rejects_every_non_exact_revision_set(
    revisions: tuple[str, ...],
) -> None:
    with pytest.raises(DatabaseMigrationError) as raised:
        await PostgresqlReadinessProbe(
            ScriptedEngine(connectivity=1, revisions=revisions)
        ).check()
    assert raised.value.error_code is ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID


@pytest.mark.asyncio
async def test_readiness_propagates_cancellation_and_closes_connection() -> None:
    engine = ScriptedEngine(cancel_during_revision=True)
    with pytest.raises(asyncio.CancelledError):
        await PostgresqlReadinessProbe(engine).check()
    assert engine.connection_was_closed is True
```

Also assert `personal_os.recovery.POSTGRESQL_SCHEMA_REVISION` remains the same value after moving authority to `personal_os.database_schema`.

- [ ] **Step 2: Run the focused tests and confirm the missing adapter**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_readiness.py tests/unit/recovery/test_contracts.py -q
```

Expected: collection fails because `database_schema.py` and `postgresql_source_store.readiness` do not exist.

- [ ] **Step 3: Implement exact queries and safe exception classification**

Use two constant SQL statements and materialize every revision so multiple rows cannot be mistaken for one:

```python
_CONNECTIVITY = sa.text("SELECT 1")
_SCHEMA_REVISIONS = sa.text(
    "SELECT version_num FROM public.alembic_version ORDER BY version_num"
)


class PostgresqlReadinessProbe:
    def __init__(self, engine: AsyncEngine, expected_revision: str = CANONICAL_POSTGRESQL_SCHEMA_REVISION) -> None:
        self._engine = engine
        self._expected_revision = expected_revision

    async def check(self) -> None:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(_CONNECTIVITY)
                result = await connection.execute(_SCHEMA_REVISIONS)
                revisions = tuple(str(value) for value in result.scalars().all())
                if revisions != (self._expected_revision,):
                    raise DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID)
        except asyncio.CancelledError:
            raise
        except DatabaseMigrationError:
            raise
        except SQLAlchemyOperationalError, SQLAlchemyTimeoutError:
            raise DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE) from None
        except SQLAlchemyError:
            raise DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID) from None
```

Keep the two-second overall deadline out of this adapter; Task 5 owns it around the complete readiness operation.

- [ ] **Step 4: Run adapter, recovery, boundary, lint and type gates**

```powershell
uv run pytest tests/unit/postgresql_source_store/test_readiness.py tests/unit/recovery/test_contracts.py -q
uv run poe python-lint
uv run poe python-type-check
uv run poe boundary-check
```

Expected: all commands exit `0`; the PostgreSQL package remains isolated from FastAPI and composition roots.

- [ ] **Step 5: Commit the readiness adapter**

```powershell
git add src/personal_os/database_schema.py src/personal_os/recovery/contracts.py src/personal_os/recovery/__init__.py packages/postgresql-source-store/src/postgresql_source_store/readiness.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py tests/unit/postgresql_source_store/test_readiness.py tests/unit/recovery/test_contracts.py
git commit -m "feat: add postgresql readiness probe"
```

---

### Task 4: Add correlation middleware and safe HTTP diagnostic events

**Files:**
- Modify: `src/personal_os/diagnostics/events.py`
- Create: `apps/api/src/api_runtime/request_context.py`
- Modify: `tests/unit/diagnostics/test_event_registry.py`
- Create: `tests/unit/api_runtime/test_request_context.py`
- Modify: `tests/contract/test_sensitive_diagnostics.py`

**Interfaces:**
- Consumes: `create_diagnostic_context`, `bind_diagnostic_context`, `format_traceparent`, `DiagnosticEventSink`, `ApiHttpMethod`, `ApiRouteTemplate`.
- Produces: events `API_REQUEST_COMPLETED`, `API_REQUEST_REJECTED`, `API_REQUEST_FAILED`; `RequestContextMiddleware(app: ASGIApp, event_sink: DiagnosticEventSink | None = None, monotonic_ns: Callable[[], int] = time.monotonic_ns)`.

- [ ] **Step 1: Write failing event and pure-ASGI middleware tests**

Pin the event schemas:

```python
EXPECTED_FIELDS = frozenset({"http_method", "route", "status_code", "duration_ms"})


def test_api_request_events_have_closed_low_cardinality_fields() -> None:
    for name, result in (
        (EventName.API_REQUEST_COMPLETED, ResultCode.SUCCEEDED),
        (EventName.API_REQUEST_REJECTED, ResultCode.REJECTED),
        (EventName.API_REQUEST_FAILED, ResultCode.FAILED),
    ):
        definition = EVENT_DEFINITIONS[name]
        assert definition.result_code is result
        assert definition.required_fields == EXPECTED_FIELDS
        assert definition.allowed_fields == EXPECTED_FIELDS
```

Test middleware with a minimal ASGI app and captured `http.response.start`:

```python
@pytest.mark.asyncio
async def test_middleware_owns_request_id_and_returns_trace_headers() -> None:
    observed_contexts: list[DiagnosticContext | None] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        observed_contexts.append(current_diagnostic_context())
        scope["route_template"] = ApiRouteTemplate.HEALTH_LIVE
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    response = await invoke_asgi(
        RequestContextMiddleware(app),
        headers={
            "x-client-request-id": str(uuid4()),
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        },
    )
    context = observed_contexts[0]
    assert context is not None and context.request_id.version == 7
    assert response.headers["x-request-id"] == str(context.request_id)
    assert response.headers["traceparent"].startswith(
        "00-4bf92f3577b34da6a3ce929d0e0e4736-"
    )
    assert current_diagnostic_context() is None
```

Add sentinels in raw path, query, headers and malformed correlation inputs; assert captured events contain only closed enum values and never any sentinel.

- [ ] **Step 2: Run the tests and verify the red state**

```powershell
uv run pytest tests/unit/api_runtime/test_request_context.py tests/unit/diagnostics/test_event_registry.py tests/contract/test_sensitive_diagnostics.py -q
```

Expected: collection fails on missing events and `api_runtime.request_context`.

- [ ] **Step 3: Implement pure-ASGI correlation and observation**

Do not use `BaseHTTPMiddleware`. For HTTP scopes:

```python
resolution = create_diagnostic_context(
    client_request_id=_header(scope, b"x-client-request-id"),
    traceparent=_header(scope, b"traceparent"),
)
with bind_diagnostic_context(resolution.context):
    await self._app(scope, receive, send_with_correlation_headers)
```

The wrapped `send` stores status and adds `X-Request-ID` plus formatted
`traceparent` exactly once. In `finally`, `_resolve_route_template(scope)`
accepts an already assigned `ApiRouteTemplate`, maps FastAPI's matched route
path only when it equals one of the three closed enum values, and otherwise
returns `UNMATCHED`; it never retains an attacker-owned raw path. Clamp duration
to a non-negative integer millisecond, and emit:

```python
event_name = (
    EventName.API_REQUEST_COMPLETED
    if status_code < 400
    else EventName.API_REQUEST_REJECTED
    if status_code < 500
    else EventName.API_REQUEST_FAILED
)
```

Emit existing `CLIENT_REQUEST_ID_REJECTED` and `TRACE_CONTEXT_REPLACED` with the fixed reason tokens `invalid_format`; never include the rejected value. Non-HTTP ASGI scopes pass through without creating an HTTP context.

- [ ] **Step 4: Run focused tests and diagnostics regressions**

```powershell
uv run pytest tests/unit/api_runtime/test_request_context.py tests/unit/diagnostics tests/contract/test_sensitive_diagnostics.py -q
uv run poe python-lint
uv run poe python-type-check
```

Expected: all commands exit `0`; every sentinel assertion remains green.

- [ ] **Step 5: Commit the middleware deliverable**

```powershell
git add src/personal_os/diagnostics/events.py apps/api/src/api_runtime/request_context.py tests/unit/diagnostics/test_event_registry.py tests/unit/api_runtime/test_request_context.py tests/contract/test_sensitive_diagnostics.py
git commit -m "feat: add api request correlation"
```

---

### Task 5: Build the FastAPI app, health routes and envelope handlers

**Files:**
- Create: `apps/api/src/api_runtime/health_routes.py`
- Create: `apps/api/src/api_runtime/application.py`
- Create: `tests/contract/api/test_health_routes.py`
- Create: `tests/contract/api/test_error_envelopes.py`
- Create: `tests/contract/api/test_openapi_exposure.py`

**Interfaces:**
- Consumes: `CanonicalDatabaseReadinessProbe`, `ApiEnvelope`, health data models, `HTTP_ERROR_STATUSES`, `RequestContextMiddleware`, `RuntimeEnvironment`, `DiagnosticEventSink`.
- Produces: `register_api_exception_handlers(app: FastAPI) -> None`; `create_api_application(*, environment: RuntimeEnvironment, readiness_probe: CanonicalDatabaseReadinessProbe, event_sink: DiagnosticEventSink | None = None, lifespan: Lifespan[FastAPI] | None = None) -> FastAPI`; operation IDs `getApiLiveness` and `getApiReadiness`.

- [ ] **Step 1: Write failing ASGI contract tests**

Use `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")` and an injected probe:

```python
@pytest.mark.asyncio
async def test_liveness_never_calls_readiness_probe() -> None:
    probe = RecordingProbe()
    response = await request(create_test_app(probe), "GET", "/api/health/live")
    assert response.status_code == 200
    assert response.json()["data"] == {"status": "live", "service": "api"}
    assert probe.call_count == 0


@pytest.mark.asyncio
async def test_readiness_has_one_two_second_deadline_and_no_retry() -> None:
    probe = BlockingProbe()
    response = await request(create_test_app(probe), "GET", "/api/health/ready")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "database_connection_unavailable",
        "message": "The canonical database is unavailable",
        "retryable": True,
        "details": {},
    }
    assert probe.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "status", "code"),
    [
        ("/api/not-real?sentinel=do-not-emit", "GET", 404, "api_route_not_found"),
        ("/api/health/live", "POST", 405, "api_method_not_allowed"),
    ],
)
async def test_framework_errors_use_safe_envelope(
    path: str, method: str, status: int, code: str
) -> None:
    response = await request(create_test_app(ReadyProbe()), method, path)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "do-not-emit" not in response.text
```

Add tests for malformed JSON (`400 api_request_malformed`), schema failure (`422 api_request_validation_failed` with only safe `field_names`), `ApplicationError`, unexpected sentinel exception (`500 internal_error`), `redirect_slashes=False`, body/header request ID equality, and response traceparent validity. Malformed/schema tests build a test-only FastAPI app, call `register_api_exception_handlers`, and add `/test/body`; that route never enters `create_api_application` or its OpenAPI snapshot.

- [ ] **Step 2: Run the contract tests and verify they fail**

```powershell
uv run pytest tests/contract/api -q
```

Expected: collection fails because `create_api_application` and the health modules do not exist.

- [ ] **Step 3: Implement the app factory and exact route/error behavior**

Construct FastAPI with:

```python
app = FastAPI(
    title="Personal Knowledge API",
    version=distribution_version(),
    openapi_version="3.1.0",
    openapi_url="/api/openapi.json" if environment in LOCAL_ENVIRONMENTS else None,
    docs_url=None,
    redoc_url=None,
    redirect_slashes=False,
    lifespan=lifespan,
)
```

Register `RequestContextMiddleware` and only the two health routes. Every application route sets `request.scope["route_template"]` to its closed `ApiRouteTemplate` before returning. FastAPI's local/test OpenAPI handler returns the raw standard document and middleware classifies it as `ApiRouteTemplate.OPENAPI`; it is the only non-envelope success response.

Readiness wraps the complete probe once:

```python
try:
    async with asyncio.timeout(2.0):
        await readiness_probe.check()
except TimeoutError:
    error = DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE)
    return api_error_response(request_id(), error)
```

Register handlers for `RequestValidationError`, Starlette `HTTPException`, `ApplicationError` and `Exception`. Classify only `json_invalid` as malformed; validation exposes bounded unique top-level field names as `SafeToken`s and no rejected values. Unknown status/code combinations fall back to `InternalApplicationError`.

Do not add CORS, GZip, session or authentication middleware.

- [ ] **Step 4: Run ASGI contracts, lint, type and boundary gates**

```powershell
uv run pytest tests/contract/api tests/unit/api_contracts tests/unit/api_runtime/test_request_context.py -q
uv run poe python-lint
uv run poe python-type-check
uv run poe boundary-check
```

Expected: all commands exit `0`; the route list remains closed and every response is enveloped.

- [ ] **Step 5: Commit the runnable app factory**

```powershell
git add apps/api/src/api_runtime/application.py apps/api/src/api_runtime/health_routes.py tests/contract/api
git commit -m "feat: add api health contract"
```

---

### Task 6: Add lazy CLI dispatch, database lifespan and Uvicorn runner

**Files:**
- Modify: `src/personal_os/command_shell.py`
- Modify: `apps/api/src/api_runtime/command.py`
- Create: `apps/api/src/api_runtime/database_lifecycle.py`
- Create: `apps/api/src/api_runtime/server.py`
- Modify: `tests/unit/test_command_shell.py`
- Create: `tests/unit/api_runtime/test_database_lifecycle.py`
- Create: `tests/unit/api_runtime/test_server.py`
- Modify: `tests/contract/test_process_commands.py`
- Modify: `tests/contract/test_command_import_side_effects.py`

**Interfaces:**
- Consumes: `ApiServerSettings`, database settings/password loader, `create_source_store_engine`, `dispose_source_store_engine`, `PostgresqlReadinessProbe`, `create_api_application`, `configure_diagnostics`.
- Produces: `BootstrapSubcommand`, generic `run_bootstrap_command(..., subcommands: Sequence[BootstrapSubcommand] = ())`; `DatabaseRuntimeLifecycle.start()`, `.stop()`, `.check()`; `run_server(*, environ: Mapping[str, str] | None = None, server_factory: ServerFactory = uvicorn.Server) -> int`.

- [ ] **Step 1: Write failing CLI, lifecycle and server configuration tests**

Pin generic command dispatch without importing the selected implementation:

```python
def test_bootstrap_subcommand_parses_owned_arguments_and_calls_handler() -> None:
    calls: list[Namespace] = []
    command = BootstrapSubcommand(
        name="export-openapi",
        help="export contract",
        configure=lambda parser: parser.add_argument("--output", required=True),
        handler=lambda arguments: calls.append(arguments) or 0,
    )
    assert run_bootstrap_command(IDENTITY, ["export-openapi", "--output", "schema.json"], subcommands=(command,)) == 0
    assert calls[0].output == "schema.json"
```

Extend import-side-effect expectations so `api_runtime.server`, `api_runtime.application`, `fastapi` and `uvicorn` remain absent after every shell-only invocation. Add stdlib `argparse` to the wrapper import allowlist because typed `ArgumentParser`/`Namespace` callbacks are now part of the lazy shell contract; do not allow any new external module.

Pin runtime behavior:

```python
def test_server_disables_version_and_proxy_headers() -> None:
    captured = RecordingServerFactory()
    result = run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured)
    assert result == 0
    assert captured.config.host == "127.0.0.1"
    assert captured.config.port == 8000
    assert captured.config.server_header is False
    assert captured.config.proxy_headers is False
    assert captured.config.reload is False
    assert captured.config.workers == 1
```

Lifecycle tests must prove engine creation occurs in `start`, no connection is opened there, `check` fails safely before start, and `stop` disposes exactly once on normal exit and cancellation.

- [ ] **Step 2: Run tests and confirm lazy dispatch is absent**

```powershell
uv run pytest tests/unit/test_command_shell.py tests/unit/api_runtime/test_database_lifecycle.py tests/unit/api_runtime/test_server.py tests/contract/test_process_commands.py tests/contract/test_command_import_side_effects.py -q
```

Expected: failures show `BootstrapSubcommand`, `serve`, lifecycle and server runner are missing.

- [ ] **Step 3: Implement generic subcommands and API-only lazy callbacks**

Add this immutable shared shell value:

```python
@dataclass(frozen=True, slots=True)
class BootstrapSubcommand:
    name: str
    help: str
    configure: Callable[[ArgumentParser], None]
    handler: Callable[[Namespace], int]
```

`run_bootstrap_command` creates each declared subparser, invokes `configure`, stores the selected handler with `set_defaults`, and calls it only after successful parsing. Existing callers pass no extra subcommands and retain byte-equivalent behavior.

`api_runtime.command` defines `_serve` and `_export_openapi` functions whose imports stay inside the function bodies:

```python
def _serve(_arguments: Namespace) -> int:
    from api_runtime.server import run_server
    return run_server()


def _export_openapi(arguments: Namespace) -> int:
    from api_runtime.openapi_export import export_openapi
    return export_openapi(arguments.output)
```

Task 7 supplies `openapi_export`; until then the callback is never invoked by focused shell tests. `DatabaseRuntimeLifecycle` owns the lazily created engine and readiness probe and makes `stop()` idempotent. `run_server` captures `os.environ` once, loads runtime/API/database settings and password, configures diagnostics, builds the lifecycle/app, and runs Uvicorn with the approved flags.

Map configuration/secret failures to existing safe exit `78`, unexpected startup failure to `70`, and normal server shutdown to `0`; never print raw exceptions.

- [ ] **Step 4: Run CLI, lifecycle, server and full shell regression gates**

```powershell
uv run pytest tests/unit/test_command_shell.py tests/unit/api_runtime tests/contract/test_process_commands.py tests/contract/test_command_import_side_effects.py tests/contract/test_runtime_check_commands.py -q
uv run poe python-lint
uv run poe python-type-check
uv run poe boundary-check
```

Expected: all commands exit `0`; MCP and Worker shell behavior remains unchanged.

- [ ] **Step 5: Commit the process runtime**

```powershell
git add src/personal_os/command_shell.py apps/api/src/api_runtime/command.py apps/api/src/api_runtime/database_lifecycle.py apps/api/src/api_runtime/server.py tests/unit/test_command_shell.py tests/unit/api_runtime/test_database_lifecycle.py tests/unit/api_runtime/test_server.py tests/contract/test_process_commands.py tests/contract/test_command_import_side_effects.py
git commit -m "feat: run api server lazily"
```

---

### Task 7: Export and snapshot deterministic OpenAPI

**Files:**
- Create: `apps/api/src/api_runtime/openapi_export.py`
- Modify: `apps/api/src/api_runtime/command.py`
- Create: `tests/unit/api_runtime/test_openapi_export.py`
- Create: `tests/contract/api/test_openapi_schema.py`
- Create: `packages/api-client/openapi.json`

**Interfaces:**
- Consumes: `create_api_application`, fixed test environment, a no-I/O `ReadyProbe`, CLI `export-openapi --output` callback from Task 6.
- Produces: `normalize_openapi(document: Mapping[str, object]) -> dict[str, object]`, `render_openapi_json() -> bytes`, `export_openapi(output_path: str) -> int`; committed `packages/api-client/openapi.json`.

- [ ] **Step 1: Write failing determinism and no-I/O tests**

Pin normalization and exporter behavior:

```python
def test_openapi_render_is_byte_identical_and_has_no_machine_values() -> None:
    first = render_openapi_json()
    second = render_openapi_json()
    assert first == second
    assert first.endswith(b"\n")
    document = json.loads(first)
    assert document["openapi"] == "3.1.0"
    assert "servers" not in document
    assert set(document["paths"]) == {
        "/api/health/live",
        "/api/health/ready",
    }
    assert document["paths"]["/api/health/live"]["get"]["operationId"] == "getApiLiveness"
    assert document["paths"]["/api/health/ready"]["get"]["operationId"] == "getApiReadiness"


def test_export_never_reads_environment_secret_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(os, "environ", ForbiddenEnvironment())
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(AsyncEngine, "connect", forbid)
    output = tmp_path / "openapi.json"
    assert export_openapi(str(output)) == 0
    assert output.read_bytes() == render_openapi_json()
```

Contract-test every response against a named component schema, require explicit operation IDs, forbid timestamps/hostnames/filesystem paths, and assert schemas set `additionalProperties: false` where the strict Pydantic models require it.

- [ ] **Step 2: Run exporter/schema tests and confirm the red state**

```powershell
uv run pytest tests/unit/api_runtime/test_openapi_export.py tests/contract/api/test_openapi_schema.py -q
```

Expected: collection fails because `api_runtime.openapi_export` and the snapshot are absent.

- [ ] **Step 3: Implement recursive normalization and perform the first export**

Implement deterministic traversal without rewriting arrays:

```python
def _normalize_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _normalize_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def render_openapi_json() -> bytes:
    app = create_contract_application()
    document = normalize_openapi(app.openapi())
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
```

`create_contract_application()` is private to `openapi_export.py`; it injects a
`_ReadyProbe` whose `check()` returns `None`, passes `RuntimeEnvironment.TEST`
to `create_api_application`, and never enters application lifespan. It is not a
second route graph.

`normalize_openapi` removes `servers` and rejects any unsupported non-JSON value. Write using `Path(output_path).write_bytes` only after full bytes are rendered; create no parent directory implicitly. Then run:

```powershell
uv run --package api-runtime personal-api export-openapi --output packages/api-client/openapi.json
```

Read the output and confirm it contains only the two health operations and named envelope/error components.

- [ ] **Step 4: Run schema, CLI isolation, lint and type gates**

```powershell
uv run pytest tests/unit/api_runtime/test_openapi_export.py tests/contract/api tests/contract/test_command_import_side_effects.py -q
uv run poe python-lint
uv run poe python-type-check
```

Expected: all commands exit `0`; a second export produces no diff.

- [ ] **Step 5: Commit the canonical OpenAPI snapshot**

```powershell
git add apps/api/src/api_runtime/openapi_export.py apps/api/src/api_runtime/command.py tests/unit/api_runtime/test_openapi_export.py tests/contract/api/test_openapi_schema.py packages/api-client/openapi.json
git commit -m "feat: export deterministic openapi"
```

---

### Task 8: Create the generated shared TypeScript API package

**Files:**
- Modify: `pnpm-workspace.yaml`
- Modify: `pnpm-lock.yaml`
- Create: `packages/api-client/package.json`
- Create: `packages/api-client/tsconfig.json`
- Create: `packages/api-client/eslint.config.mjs`
- Create: `packages/api-client/vitest.config.ts`
- Create: `packages/api-client/src/generated/schema.ts`
- Create: `packages/api-client/src/client.ts`
- Create: `packages/api-client/src/envelopes.ts`
- Create: `packages/api-client/src/client.test.ts`
- Create: `packages/api-client/src/index.ts`
- Modify: `tests/contract/test_architecture_boundaries.py`

**Interfaces:**
- Consumes: local `packages/api-client/openapi.json`, generated `paths`/`components`/`operations`, `openapi-fetch` custom `fetch` option.
- Produces: private workspace package `@workspace/api-client`; `type ApiTransport = typeof globalThis.fetch`; `type ApiClient = Client<paths>`; `createApiClient({baseUrl, transport}) -> ApiClient`; exported generated types and envelope aliases.

- [ ] **Step 1: Add package scaffolding, then write the failing client contract**

Add `packages/api-client` to `pnpm-workspace.yaml`. Create `package.json` with exact dependencies:

```json
{
  "name": "@workspace/api-client",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "exports": {".": "./src/index.ts"},
  "scripts": {
    "build": "tsc --noEmit",
    "format": "eslint . --fix",
    "format:check": "eslint . --max-warnings=0",
    "generate": "openapi-typescript openapi.json -o src/generated/schema.ts --immutable --export-type",
    "generate:check": "openapi-typescript openapi.json -o src/generated/schema.ts --immutable --export-type --check",
    "lint": "eslint . --max-warnings=0",
    "test": "vitest run --coverage",
    "type-check": "tsc --noEmit"
  },
  "dependencies": {"openapi-fetch": "0.17.0"},
  "devDependencies": {
    "@types/node": "24.13.3",
    "@vitest/coverage-v8": "4.1.10",
    "eslint": "9.39.5",
    "openapi-typescript": "7.13.0",
    "typescript": "6.0.3",
    "typescript-eslint": "8.67.0",
    "vitest": "4.1.10"
  }
}
```

Use the root strict `tsconfig.base.json`, include `src/**/*.ts`, and exclude `coverage`. The package ESLint config uses `typescript-eslint` strict and forbids imports matching `@workspace/web-runtime`, `@workspace/obsidian-plugin`, `**/apps/web/**`, and `**/obsidian-plugin/**`.

Write `client.test.ts` first:

```typescript
it("passes request and response through the injected transport", async () => {
  const requests: Request[] = [];
  const transport: ApiTransport = async (input, init) => {
    requests.push(new Request(input, init));
    return Response.json({
      request_id: REQUEST_ID,
      data: { status: "live", service: "api" },
      warnings: [],
      error: null,
    }, {
      status: 200,
      headers: { "x-request-id": REQUEST_ID },
    });
  };
  const client = createApiClient({ baseUrl: "https://api.invalid", transport });
  const { data, error, response } = await client.GET("/api/health/live");
  expect(error).toBeUndefined();
  expect(data?.data).toEqual({ status: "live", service: "api" });
  expect(response.headers.get("x-request-id")).toBe(REQUEST_ID);
  expect(requests[0]?.url).toBe("https://api.invalid/api/health/live");
});
```

- [ ] **Step 2: Install, generate and prove the client test fails before the factory exists**

```powershell
pnpm install --frozen-lockfile=false
pnpm --filter @workspace/api-client run generate
pnpm --filter @workspace/api-client run test
```

Expected: install/generation succeed; test compilation fails because `ApiTransport` and `createApiClient` are not exported.

- [ ] **Step 3: Implement the narrow typed client and envelope aliases**

`client.ts`:

```typescript
import createClient, { type Client } from "openapi-fetch";
import type { paths } from "./generated/schema";

export type ApiTransport = typeof globalThis.fetch;
export type ApiClient = Client<paths>;

export function createApiClient(options: {
  baseUrl: string;
  transport: ApiTransport;
}): ApiClient {
  return createClient<paths>({ baseUrl: options.baseUrl, fetch: options.transport });
}
```

`envelopes.ts` aliases concrete generated component schemas rather than duplicating fields:

```typescript
import type { components } from "./generated/schema";

export type ApiErrorBody = components["schemas"]["ApiErrorBody"];
export type LivenessResponse = components["schemas"]["LivenessResponse"];
export type ReadinessResponse = components["schemas"]["ReadinessResponse"];
```

`index.ts` exports those values/types and generated `paths`, `components`, `operations` as type-only exports. Do not hand edit `src/generated/schema.ts`.

Extend the architecture contract's `TS_SOURCE_ROOTS` with `packages/api-client/src`, classify it as `api-client`, and reject any consumer import.

- [ ] **Step 4: Run generation, package and architecture gates**

```powershell
pnpm --filter @workspace/api-client run generate:check
pnpm --filter @workspace/api-client run format:check
pnpm --filter @workspace/api-client run lint
pnpm --filter @workspace/api-client run type-check
pnpm --filter @workspace/api-client run test
uv run pytest tests/contract/test_architecture_boundaries.py -q
```

Expected: all commands exit `0`; the generated file has no diff after `generate:check`.

- [ ] **Step 5: Commit the shared client package**

```powershell
git add pnpm-workspace.yaml pnpm-lock.yaml packages/api-client tests/contract/test_architecture_boundaries.py
git commit -m "feat: generate shared api client"
```

---

### Task 9: Add the Web native-fetch transport

**Files:**
- Modify: `apps/web/package.json`
- Modify: `pnpm-lock.yaml`
- Create: `apps/web/src/api/native-fetch-transport.ts`
- Create: `apps/web/src/api/native-fetch-transport.test.ts`
- Modify: `apps/web/eslint.config.mjs`

**Interfaces:**
- Consumes: `ApiTransport` from `@workspace/api-client`, caller-supplied/native `fetch`.
- Produces: `createNativeFetchTransport(fetchImplementation: typeof globalThis.fetch = globalThis.fetch) -> ApiTransport`.

- [ ] **Step 1: Add the workspace dependency and write the failing transport test**

Add `"@workspace/api-client": "workspace:*"` to Web dependencies, then write:

```typescript
it("preserves method headers body status and response bytes", async () => {
  const calls: Request[] = [];
  const nativeFetch: typeof fetch = async (input, init) => {
    calls.push(new Request(input, init));
    return new Response(new Uint8Array([1, 2, 3]), {
      status: 202,
      headers: { "content-type": "application/octet-stream" },
    });
  };
  const transport = createNativeFetchTransport(nativeFetch);
  const response = await transport("https://api.invalid/api/test", {
    method: "PUT",
    headers: { "x-contract": "safe-value" },
    body: "payload",
  });
  expect(calls[0]?.method).toBe("PUT");
  expect(calls[0]?.headers.get("x-contract")).toBe("safe-value");
  expect(await calls[0]?.text()).toBe("payload");
  expect(response.status).toBe(202);
  expect([...new Uint8Array(await response.arrayBuffer())]).toEqual([1, 2, 3]);
});
```

- [ ] **Step 2: Run the Web test and confirm the missing transport**

```powershell
pnpm install --frozen-lockfile=false
pnpm --filter @workspace/web-runtime run test
```

Expected: test compilation fails because `native-fetch-transport.ts` does not exist.

- [ ] **Step 3: Implement the one-purpose adapter**

```typescript
import type { ApiTransport } from "@workspace/api-client";

export function createNativeFetchTransport(
  fetchImplementation: typeof globalThis.fetch = globalThis.fetch,
): ApiTransport {
  return (input, init) => fetchImplementation(input, init);
}
```

Update Web ESLint restrictions so Web still rejects Obsidian but explicitly permits `@workspace/api-client`. Do not import this transport into UI components and do not issue a network request in this child.

- [ ] **Step 4: Run Web and shared-package gates**

```powershell
pnpm --filter @workspace/web-runtime run format:check
pnpm --filter @workspace/web-runtime run lint
pnpm --filter @workspace/web-runtime run type-check
pnpm --filter @workspace/web-runtime run test
pnpm --filter @workspace/web-runtime run build
uv run pytest tests/contract/test_architecture_boundaries.py -q
```

Expected: all commands exit `0`; the existing bootstrap page is unchanged.

- [ ] **Step 5: Commit the Web adapter**

```powershell
git add apps/web/package.json apps/web/eslint.config.mjs apps/web/src/api/native-fetch-transport.ts apps/web/src/api/native-fetch-transport.test.ts pnpm-lock.yaml
git commit -m "feat: add web api transport"
```

---

### Task 10: Add the Obsidian `requestUrl` transport

**Files:**
- Modify: `apps/obsidian-plugin/package.json`
- Modify: `pnpm-lock.yaml`
- Create: `apps/obsidian-plugin/src/api/request-url-transport.ts`
- Create: `apps/obsidian-plugin/src/api/request-url-transport.test.ts`
- Create: `apps/obsidian-plugin/src/api/obsidian-api-transport.ts`
- Modify: `apps/obsidian-plugin/eslint.config.mjs`

**Interfaces:**
- Consumes: `ApiTransport` from `@workspace/api-client`; Obsidian `requestUrl`, `RequestUrlParam`, `RequestUrlResponse`.
- Produces: `type RequestUrlFunction = (request: RequestUrlParam) => Promise<RequestUrlResponse>`; pure `createRequestUrlTransport(requestUrlFunction: RequestUrlFunction) -> ApiTransport`; provider binding `createObsidianApiTransport() -> ApiTransport`.

- [ ] **Step 1: Add the workspace dependency and write failing byte-preservation tests**

Add `"@workspace/api-client": "workspace:*"` to plugin dependencies; keep `obsidian` in dev dependencies. Write tests against an injected function so Vitest never loads the real Obsidian module:

```typescript
it("adapts Request to requestUrl and preserves response bytes", async () => {
  const calls: RequestUrlParam[] = [];
  const requestUrlFunction: RequestUrlFunction = async (request) => {
    calls.push(request);
    return {
      status: 206,
      headers: { "content-type": "application/octet-stream" },
      arrayBuffer: new Uint8Array([4, 5, 6]).buffer,
      json: undefined,
      text: "",
    };
  };
  const transport = createRequestUrlTransport(requestUrlFunction);
  const response = await transport("https://api.invalid/api/object", {
    method: "PUT",
    headers: { "content-type": "application/octet-stream", "x-contract": "safe" },
    body: new Uint8Array([1, 2, 3]),
  });
  expect(calls[0]).toMatchObject({
    url: "https://api.invalid/api/object",
    method: "PUT",
    throw: false,
  });
  expect(new Uint8Array(calls[0]?.body as ArrayBuffer)).toEqual(new Uint8Array([1, 2, 3]));
  expect(response.status).toBe(206);
  expect([...new Uint8Array(await response.arrayBuffer())]).toEqual([4, 5, 6]);
});


it("rejects an already-aborted request before requestUrl dispatch", async () => {
  const controller = new AbortController();
  controller.abort();
  const requestUrlFunction = vi.fn<RequestUrlFunction>();
  const transport = createRequestUrlTransport(requestUrlFunction);
  await expect(
    transport("https://api.invalid/api/health/live", { signal: controller.signal }),
  ).rejects.toMatchObject({ name: "AbortError" });
  expect(requestUrlFunction).not.toHaveBeenCalled();
});
```

Also test GET/HEAD omit the body, duplicate headers are deterministically joined, status/headers are preserved, and neither request nor response content is logged.

- [ ] **Step 2: Run plugin tests and confirm the missing adapter**

```powershell
pnpm install --frozen-lockfile=false
pnpm --filter @workspace/obsidian-plugin run test
```

Expected: test compilation fails because `request-url-transport.ts` is absent.

- [ ] **Step 3: Implement the isolated requestUrl adapter**

The adapter imports Obsidian only in this file:

```typescript
import type { RequestUrlParam, RequestUrlResponse } from "obsidian";
import type { ApiTransport } from "@workspace/api-client";

export type RequestUrlFunction = (
  request: RequestUrlParam,
) => Promise<RequestUrlResponse>;

export function createRequestUrlTransport(
  requestUrlFunction: RequestUrlFunction,
): ApiTransport {
  return async (input, init) => {
    const request = new Request(input, init);
    if (request.signal.aborted) {
      throw new DOMException("The request was aborted", "AbortError");
    }
    const body =
      request.method === "GET" || request.method === "HEAD"
        ? undefined
        : await request.arrayBuffer();
    const result = await requestUrlFunction({
      url: request.url,
      method: request.method,
      headers: Object.fromEntries(request.headers.entries()),
      body,
      throw: false,
    });
    return new Response(result.arrayBuffer, {
      status: result.status,
      headers: result.headers,
    });
  };
}
```

Bind the real provider in the separate tiny module so Vitest can exercise the
pure adapter without loading Obsidian runtime:

```typescript
import { requestUrl } from "obsidian";
import type { ApiTransport } from "@workspace/api-client";
import { createRequestUrlTransport } from "./request-url-transport";

export function createObsidianApiTransport(): ApiTransport {
  return createRequestUrlTransport(requestUrl);
}
```

Do not import the adapter from `plugin.ts`; the bootstrap plugin remains behavior-empty. In-flight abort cannot cancel Obsidian's underlying `requestUrl`; later operation code must bound concurrency and discard a late result after its deadline. This child adds no automatic retry.

- [ ] **Step 4: Run plugin, package, build and architecture gates**

```powershell
pnpm --filter @workspace/obsidian-plugin run format:check
pnpm --filter @workspace/obsidian-plugin run lint
pnpm --filter @workspace/obsidian-plugin run type-check
pnpm --filter @workspace/obsidian-plugin run test
pnpm --filter @workspace/obsidian-plugin run build
pnpm --filter @workspace/api-client run test
uv run pytest tests/contract/test_architecture_boundaries.py -q
```

Expected: all commands exit `0`; the existing test still proves `plugin.ts` contains no `requestUrl` or product behavior.

- [ ] **Step 5: Commit the plugin adapter**

```powershell
git add apps/obsidian-plugin/package.json apps/obsidian-plugin/eslint.config.mjs apps/obsidian-plugin/src/api/request-url-transport.ts apps/obsidian-plugin/src/api/request-url-transport.test.ts apps/obsidian-plugin/src/api/obsidian-api-transport.ts pnpm-lock.yaml
git commit -m "feat: add obsidian api transport"
```

---

### Task 11: Add stale-contract, security, real-readiness and server lifecycle gates

**Files:**
- Create: `tools/api_contract_artifacts.py`
- Create: `tests/unit/tools/test_api_contract_artifacts.py`
- Modify: `pyproject.toml`
- Create: `tests/contract/api/test_sensitive_http_contract.py`
- Create: `tests/integration/canonical_core/test_api_readiness_integration.py`
- Create: `tests/integration/api_runtime/test_uvicorn_lifecycle.py`

**Interfaces:**
- Consumes: `render_openapi_json`, committed snapshot, API client `generate:check`, disposable canonical-core PostgreSQL fixture, `run_server`/app factory.
- Produces: `check_snapshot(snapshot_path: Path = DEFAULT_SNAPSHOT) -> int`; Poe tasks `api-contract-export`, `api-contract-snapshot-check`, `api-client-generate-check`, `api-contract-check`; full sensitive-data and live integration evidence.

- [ ] **Step 1: Write failing stale-artifact and sensitive HTTP tests**

Pin non-mutating comparison:

```python
def test_snapshot_check_detects_stale_bytes_without_rewriting(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    stale = b'{"openapi":"stale"}\n'
    snapshot.write_bytes(stale)
    assert check_snapshot(snapshot) == 1
    assert snapshot.read_bytes() == stale


def test_snapshot_check_accepts_exact_render(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    snapshot.write_bytes(render_openapi_json())
    assert check_snapshot(snapshot) == 0
```

`test_sensitive_http_contract.py` injects unique sentinels in raw path, query, headers, cookies, malformed JSON, validation values, database exception text and an unexpected exception. Capture HTTP and structured diagnostics; assert every sentinel is absent from both. Also assert no `server` version header, CORS header, Swagger/ReDoc HTML or production OpenAPI body.

- [ ] **Step 2: Run the new tests and prove the gates are absent**

```powershell
uv run pytest tests/unit/tools/test_api_contract_artifacts.py tests/contract/api/test_sensitive_http_contract.py -q
```

Expected: collection fails because `tools.api_contract_artifacts` and the security test seams are missing.

- [ ] **Step 3: Implement contract checks and add real integration cases**

`tools/api_contract_artifacts.py` accepts only `check`, reads the fixed default snapshot or an injected `Path`, compares bytes with `hmac.compare_digest`, emits one fixed `api_contract_current` or `api_contract_stale` token, and never prints schema content or paths.

Add Poe tasks:

```toml
[tool.poe.tasks.api-contract-export]
cmd = "uv run --package api-runtime personal-api export-openapi --output packages/api-client/openapi.json"

[tool.poe.tasks.api-contract-snapshot-check]
cmd = "uv run python tools/api_contract_artifacts.py check"

[tool.poe.tasks.api-client-generate-check]
shell = "pnpm --filter @workspace/api-client run generate:check"

[tool.poe.tasks.api-contract-check]
sequence = ["api-contract-snapshot-check", "api-client-generate-check"]
default_item_type = "ref"
```

Add `api-contract-check` to `boundary-check` after existing boundary tests.

In `tests/integration/canonical_core/test_api_readiness_integration.py`, reuse `canonical_core_stack`:

```python
pytestmark = pytest.mark.local_stack


@pytest.mark.asyncio
async def test_real_postgresql_reports_current_head(canonical_core_stack: CanonicalCoreStack) -> None:
    engine = create_source_store_engine(
        canonical_core_stack.settings, canonical_core_stack.password
    )
    try:
        await PostgresqlReadinessProbe(engine).check()
    finally:
        await dispose_source_store_engine(engine)
```

Add a disposable committed revision-drift case that sets `public.alembic_version.version_num` to `stale_revision`, proves `DATABASE_SCHEMA_CONTRACT_INVALID`, and restores `20260813_01` in `finally`. Add a refused-port case for `DATABASE_CONNECTION_UNAVAILABLE`; never use the operator's non-disposable project.

`test_uvicorn_lifecycle.py` binds an OS-assigned loopback socket, starts `uvicorn.Server.serve(sockets=[socket])` with a fake ready probe, requests liveness with HTTPX, sets `server.should_exit = True`, and proves the task exits and lifecycle stop runs once. Use a 5-second test deadline and never bind `0.0.0.0`.

- [ ] **Step 4: Run contract, default and explicit local-stack gates**

Run default gates first:

```powershell
uv run pytest tests/unit/tools/test_api_contract_artifacts.py tests/contract/api tests/integration/api_runtime/test_uvicorn_lifecycle.py -q
uv run poe api-contract-check
uv run poe boundary-check
```

Then run the disposable PostgreSQL evidence with a free dedicated port:

```powershell
$env:CI='true'
$env:LOCAL_STACK_TEST_PROJECT='knowledge-ci-api-runtime'
$env:POSTGRES_PORT='55432'
uv run pytest tests/integration/canonical_core/test_api_readiness_integration.py -m local_stack -q
Remove-Item Env:CI,Env:LOCAL_STACK_TEST_PROJECT,Env:POSTGRES_PORT
```

Expected: every command exits `0`; the integration fixture resets its exact disposable Compose project in `finally`.

- [ ] **Step 5: Commit the cross-boundary gates**

```powershell
git add tools/api_contract_artifacts.py tests/unit/tools/test_api_contract_artifacts.py pyproject.toml tests/contract/api/test_sensitive_http_contract.py tests/integration/canonical_core/test_api_readiness_integration.py tests/integration/api_runtime/test_uvicorn_lifecycle.py
git commit -m "test: gate api runtime contracts"
```

---

### Task 12: Update canonical docs, run full verification and write the required handoff

**Files:**
- Modify: `README.md`
- Modify: `apps/api/README.md`
- Modify: `apps/web/README.md`
- Modify: `apps/obsidian-plugin/README.md`
- Modify: `docs/12-API_MCP_AND_AGENT_INTEGRATION.md`
- Modify: `docs/15-OBSERVABILITY_AND_ALERTING.md`
- Modify: `docs/20-IMPLEMENTATION_PLAN.md`
- Create: `docs/operations/api-runtime-contract.md`
- Create: `docs/handoff/2026-08-15-api-runtime-contract-foundation.md`
- Modify only if a genuinely new deferred item exists: `docs/handoff/BACKLOG.md`

**Interfaces:**
- Consumes: every implemented command, route, status/error contract, OpenAPI/client task and verification result from Tasks 1-11.
- Produces: living operator guide, canonical Phase 2 child status, one exact plan handoff with commit/gate evidence and next child pointer.

- [ ] **Step 1: Write failing documentation contract assertions**

Add focused assertions to the most relevant existing contract tests or create `tests/contract/api/test_api_documentation.py`:

```python
def test_api_docs_name_exact_commands_routes_and_production_policy() -> None:
    root = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    api = (REPO_ROOT / "apps/api/README.md").read_text(encoding="utf-8")
    operations = (REPO_ROOT / "docs/operations/api-runtime-contract.md").read_text(
        encoding="utf-8"
    )
    combined = root + api + operations
    for required in (
        "personal-api serve",
        "personal-api export-openapi",
        "/api/health/live",
        "/api/health/ready",
        "KNOWLEDGE_API_HOST",
        "KNOWLEDGE_API_PORT",
        "api-contract-check",
    ):
        assert required in combined
    assert "production OpenAPI is disabled" in combined
```

Add an assertion that `docs/20-IMPLEMENTATION_PLAN.md` names the completed child by semantic slug and points to the design/plan instead of claiming all Phase 2 sync is complete.

- [ ] **Step 2: Run the documentation contract and confirm the red state**

```powershell
uv run pytest tests/contract/api/test_api_documentation.py -q
```

Expected: failure because the operations guide and final documentation text do not yet exist.

- [ ] **Step 3: Write living documentation and a point-in-time handoff**

Document:

- exact local/test and staging/production bind requirements;
- safe startup, liveness, readiness and schema-drift diagnosis;
- why R2/Temporal/Qdrant/Neo4j are not API readiness dependencies;
- response/error/request-ID contract;
- local-only OpenAPI and production docs shutdown;
- snapshot export/generation/check commands;
- Web and plugin transport boundaries;
- safe shutdown and prohibited logging;
- scope exclusions and next child `web-auth-and-device-authorization-design`.

The handoff contains the final commit SHA available before the handoff commit, exact gate commands/results, interpretation decisions, no copied runbook body, and the next actions. Record no backlog line when the only remaining items are already owned by the umbrella child sequence; add one `BACKLOG.md` line only for a genuinely new deferred defect with a verdict.

- [ ] **Step 4: Run all focused and repository gates from a clean index**

```powershell
uv run poe api-contract-check
uv run pytest tests/unit/api_contracts tests/unit/api_runtime tests/unit/postgresql_source_store/test_readiness.py tests/contract/api tests/integration/api_runtime/test_uvicorn_lifecycle.py -q
uv run poe verify
git diff --check
git status --short
(Get-Content -LiteralPath AGENTS.md).Count
(Get-Content -LiteralPath CLAUDE.md).Count
```

Expected:

- API contract check reports current artifacts and exits `0`;
- focused Python tests pass with zero failures;
- `poe verify` completes format, lint, type, boundary, default tests and build with exit `0`;
- `git diff --check` emits nothing;
- `git status --short` shows only this task's intended documentation plus the user's pre-existing Phase 1 moves;
- `AGENTS.md` and `CLAUDE.md` remain 110 and 111 lines respectively unless the user changed them during execution.

If Task 11's local-stack gate was not run in the same final commit, rerun it now with the exact disposable project command from Task 11 and record its output in the handoff. Do not claim the readiness integration gate from an older commit.

- [ ] **Step 5: Commit documentation and handoff, then request final review**

```powershell
git add README.md apps/api/README.md apps/web/README.md apps/obsidian-plugin/README.md docs/12-API_MCP_AND_AGENT_INTEGRATION.md docs/15-OBSERVABILITY_AND_ALERTING.md docs/20-IMPLEMENTATION_PLAN.md docs/operations/api-runtime-contract.md docs/handoff/2026-08-15-api-runtime-contract-foundation.md tests/contract/api/test_api_documentation.py
git add docs/handoff/BACKLOG.md  # only when Step 3 created one justified new backlog line
git commit -m "docs: operate api contract foundation"
```

Run `git show --check --stat HEAD` and read the final `git status --short`. Request code review with `superpowers:requesting-code-review`; resolve findings through the required review workflow before using `superpowers:finishing-a-development-branch`.

---

## Spec Coverage Map

| Spec sections | Implemented and proved by |
|---|---|
| 1-4 purpose, scope and boundaries | Tasks 1-3, 8-10; architecture contract in Tasks 2 and 8 |
| 5 command/runtime behavior | Tasks 1, 6 and 7 |
| 6 closed route surface | Tasks 5, 7 and 11 |
| 7 response contract | Tasks 2, 5 and 8 |
| 8 request/trace correlation | Tasks 4, 5 and 11 |
| 9 HTTP error mapping | Tasks 2, 5 and 11 |
| 10 liveness/readiness | Tasks 3, 5, 6 and 11 |
| 11 OpenAPI governance | Tasks 7, 8 and 11 |
| 12 shared TypeScript client | Tasks 8-10 |
| 13 deterministic contract pipeline | Tasks 7, 8 and 11 |
| 14 security/privacy | Tasks 4-7 and 11 |
| 15 dependencies | Tasks 1 and 8-10 |
| 16 tests and 17 acceptance | Every task's red/green gate plus Tasks 11-12 |
| 18 deferred ownership | Task 12 documentation/handoff; no excluded feature is implemented |

## Completion Checklist

- [ ] Exactly the approved three-route environment-dependent surface exists; no auth/sync/business schema slipped in.
- [ ] Shell-only API paths still avoid framework imports, settings, secrets and I/O.
- [ ] Liveness is I/O-free; readiness is one bounded PostgreSQL/exact-head probe.
- [ ] Every application/health success and framework/application/unexpected failure uses the exact envelope and server UUIDv7; raw local/test OpenAPI is the only documented exception.
- [ ] HTTP error mapping is closed, safe and exhaustive for every code exposed by this child.
- [ ] Correlation/access diagnostics contain only closed fields and no sensitive sentinel.
- [ ] Uvicorn uses loopback defaults, no version header, no proxy trust, no reload and clean lifecycle disposal.
- [ ] Production has no OpenAPI, Swagger, ReDoc or wildcard CORS.
- [ ] Snapshot and generated TypeScript are committed, deterministic and non-mutating stale-checked.
- [ ] Web and plugin compile against `@workspace/api-client` through independent tested transports.
- [ ] Default repository gates and disposable PostgreSQL readiness integration are green on the final commit.
- [ ] Canonical docs, living operations guide and exactly one plan handoff are current.
- [ ] User-owned Phase 1 file moves remain untouched and outside every task commit.
