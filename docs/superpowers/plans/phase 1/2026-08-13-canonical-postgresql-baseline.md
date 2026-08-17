# Canonical PostgreSQL Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the first canonical PostgreSQL 18.4 schema as one reversible Alembic baseline with nine exact application tables, tenant-containment constraints, immutable history, safe migration configuration and a disposable upgrade/downgrade/re-upgrade acceptance lifecycle.

**Architecture:** Keep SQLAlchemy, Psycopg and Alembic outside `src/personal_os`: `migrations/database_migration_runtime.py` composes the existing safe secret-file and typed-error contracts, `migrations/env.py` owns connection policy and the migration transaction, and one handwritten revision owns all PostgreSQL DDL. PostgreSQL is canonical; the migration creates no repositories, ORM models, bootstrap data, R2 behavior or projection implementation. Static contracts run on every platform, while a unique Linux disposable local-stack project proves the live PostgreSQL lifecycle.

**Tech Stack:** CPython 3.14.6, PostgreSQL 18.4, Alembic 1.18.5, SQLAlchemy 2.0.51, Psycopg 3.3.4, Pydantic 2.13.4, pytest 9.1.1, Ruff 0.15.22, mypy 2.3.0 strict, uv 0.11.32 and Docker Compose 2.30.0.

## Global Constraints

- Implement only the approved contract in `docs/superpowers/specs/canonical-postgresql-baseline-design.md`.
- Create exactly one application schema, `knowledge`, and exactly nine application tables: `users`, `workspaces`, `devices`, `content_objects`, `sources`, `source_versions`, `sync_events`, `projection_intents` and `audit_events`.
- Keep `public.alembic_version` outside `knowledge`; create no other application table, seed row, extension, PostgreSQL enum, ORM model, repository, Unit of Work or service.
- Pin `alembic==1.18.5`, `SQLAlchemy==2.0.51` and `psycopg[binary]==3.3.4` in root production dependencies and commit `uv.lock`.
- Do not import Alembic, SQLAlchemy or Psycopg anywhere under `src/personal_os/`; preserve the existing import-linter prohibition.
- Generate no UUID in PostgreSQL. Every UUID is supplied by the caller; only `sync_events.event_sequence` is `bigint GENERATED ALWAYS AS IDENTITY`.
- Treat `content_objects` as global verified CAS metadata. It has no `workspace_id`; all device/source/version/event/intent/audit references remain workspace-contained.
- Store no raw source body, locator, path, URL, query, vector, token, credential, provider response, free-form audit message or generic JSONB payload.
- Use explicit schema qualification and explicit semantic constraint/index/trigger names no longer than 63 bytes.
- Use `ON DELETE RESTRICT` or the equivalent `NO ACTION`; never use `CASCADE`.
- Reject update of content objects, source versions and sync events; reject update or delete of audit events with fixed SQLSTATE `55000` messages.
- Read the database password only from one bounded file under `KNOWLEDGE_SECRET_ROOT`; never accept or render a plaintext password, DSN, `DATABASE_URL` or `.env` value.
- Support PostgreSQL server major 18 only, use one transactional migration, acquire the fixed advisory transaction lock and bound connection/lock/statement/idle-transaction timeouts.
- Refuse downgrade before mutation unless the exact Alembic x-argument `allow_destructive=true` is present.
- Test every behavior first. Run focused tests after every red/green cycle and commit only green, independently reviewable deliverables.
- Keep the existing local stack and its secrets intact. Integration cleanup may reset only the exact `knowledge-ci-*` project created by the test.

---

## Preflight

Before Task 1, use `superpowers:using-git-worktrees` and create an isolated worktree from the commit containing this plan. Do not implement directly on `master`.

```powershell
git status --short
uv --version
$env:UV_PROJECT_ENVIRONMENT='.venv-canonical-postgresql'
uv sync --all-packages --frozen
uv run --all-packages --frozen poe verify
```

Expected: clean status, uv `0.11.32`, Python `3.14.6` and the existing suite passes. On Windows, do not reuse the repository's Linux `.venv`; keep the task-specific environment name shown above or another ignored task-specific name.

## File Map

### Migration runtime and schema

- `pyproject.toml`: exact migration dependencies, Ruff/mypy coverage and public Poe commands.
- `uv.lock`: frozen dependency resolution.
- `migrations/__init__.py`: migration infrastructure package marker only.
- `migrations/database_migration_runtime.py`: frozen settings, exact environment loader, safe SQLAlchemy URL/connect arguments and typed error mapping.
- `migrations/env.py`: online-only Alembic environment, destructive gate, safe connection handling, preflight checks, transaction timeouts and advisory lock.
- `migrations/script.py.mako`: typed future-revision template.
- `migrations/versions/20260813_01_create_canonical_postgresql_baseline.py`: the only revision and all approved DDL.
- `alembic.ini`: script path and logging shape; deliberately no connection URL.

### Shared typed errors

- `src/personal_os/error_contracts/codes.py`: registered database migration error definitions.
- `src/personal_os/error_contracts/exceptions.py`: `DatabaseMigrationError` restricted to those codes.
- `tests/unit/error_contracts/test_application_errors.py`: registry category, retryability and leak-safe rendering tests.

### Tests, CI and operator documentation

- `tests/unit/migrations/test_database_migration_runtime.py`: settings, secret filename, URL and error mapping behavior.
- `tests/contract/test_canonical_postgresql_migration_contract.py`: one-head graph, exact artifact/DDL and forbidden-pattern contracts.
- `tests/integration/test_canonical_postgresql_baseline.py`: disposable PostgreSQL catalog, valid graph, negative invariants, immutability, rollback, lock and recovery lifecycle.
- `tests/integration/README.md`: ownership and safety boundary for the new integration test.
- `.github/workflows/canonical-postgresql-baseline.yml`: Windows static job and Ubuntu disposable lifecycle job.
- `tests/contract/test_ci_security.py`: path/permission/action-pin/secret/artifact/cleanup contract for the new workflow.
- `infra/compose/README.md`: local migration commands and safe configuration procedure.

---

### Task 1: Typed Migration Settings and Safe Failure Contract

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/personal_os/error_contracts/codes.py`
- Modify: `src/personal_os/error_contracts/exceptions.py`
- Modify: `tests/unit/error_contracts/test_application_errors.py`
- Create: `migrations/__init__.py`
- Create: `migrations/database_migration_runtime.py`
- Create: `tests/unit/migrations/test_database_migration_runtime.py`

**Interfaces:**

- Consumes: an explicit `Mapping[str, str]`, `RuntimeEnvironment`, `read_secret_file()`, Pydantic `SecretStr` and SQLAlchemy `URL`.
- Produces: `DatabaseSslMode`, frozen `DatabaseMigrationSettings`, `load_database_migration_settings()`, `read_database_password()`, `build_database_url()`, `build_database_connect_arguments()`, `DatabaseMigrationError` and five closed database error codes.

- [ ] **Step 1: Write failing dependency and import-boundary contracts**

Add exact production pins:

```toml
[project]
dependencies = [
  "alembic==1.18.5",
  "pydantic==2.13.4",
  "pydantic-settings==2.14.2",
  "psycopg[binary]==3.3.4",
  "SQLAlchemy==2.0.51",
]
```

Extend Ruff and mypy inputs to include `migrations`:

```toml
[tool.ruff]
src = ["src", "apps", "migrations", "tests", "tools"]

[tool.mypy]
files = ["src", "apps/api/src", "apps/mcp/src", "apps/worker/src", "migrations", "tools"]
```

Do not remove `sqlalchemy` from `.importlinter`; the core prohibition is intentional.

Run before implementation:

```powershell
$env:UV_PROJECT_ENVIRONMENT='.venv-canonical-postgresql'
uv lock
uv run pytest tests/contract/test_architecture_boundaries.py -q
uv run mypy migrations
```

Expected: the dependency lock updates; mypy fails because the package/runtime files do not exist yet.

- [ ] **Step 2: Write failing typed error tests**

Add tests that require this exact registry behavior:

```python
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError


def test_database_migration_errors_use_closed_safe_metadata() -> None:
    cases = {
        ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID: (
            ErrorCategory.CONFIGURATION,
            False,
        ),
        ErrorCode.DATABASE_CONNECTION_UNAVAILABLE: (ErrorCategory.DEPENDENCY, True),
        ErrorCode.DATABASE_MIGRATION_BUSY: (ErrorCategory.DEPENDENCY, True),
        ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID: (ErrorCategory.INTEGRITY, False),
        ErrorCode.DATABASE_DESTRUCTIVE_DOWNGRADE_REFUSED: (
            ErrorCategory.AUTHORIZATION,
            False,
        ),
    }
    for error_code, (category, is_retryable) in cases.items():
        error = DatabaseMigrationError(error_code)
        assert error.category is category
        assert error.is_retryable is is_retryable
        assert error.safe_details == {}


def test_database_migration_error_never_renders_driver_cause() -> None:
    error = DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE)
    error.__cause__ = RuntimeError("DO_NOT_LEAK_DATABASE_DRIVER")
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert "DO_NOT_LEAK_DATABASE_DRIVER" not in rendered
```

Run:

```powershell
uv run pytest tests/unit/error_contracts/test_application_errors.py -q
```

Expected: FAIL because codes and subclass are missing.

- [ ] **Step 3: Register the database error codes**

Add five values to `ErrorCode` and exact definitions to `ERROR_DEFINITIONS`:

| Code | Category | Retryable | Safe message |
|---|---|---:|---|
| `database_migration_configuration_invalid` | configuration | no | `Database migration configuration is invalid` |
| `database_connection_unavailable` | dependency | yes | `The canonical database is unavailable` |
| `database_migration_busy` | dependency | yes | `Another database migration is in progress` |
| `database_schema_contract_invalid` | integrity | no | `The canonical database schema contract is invalid` |
| `database_destructive_downgrade_refused` | authorization | no | `Destructive database downgrade is not authorized` |

Every definition has `allowed_detail_fields=frozenset()`. Add:

```python
class DatabaseMigrationError(ApplicationError):
    """Safe migration configuration, dependency and schema failures."""

    allowed_codes = frozenset(
        {
            ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID,
            ErrorCode.DATABASE_CONNECTION_UNAVAILABLE,
            ErrorCode.DATABASE_MIGRATION_BUSY,
            ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID,
            ErrorCode.DATABASE_DESTRUCTIVE_DOWNGRADE_REFUSED,
        }
    )
```

Run the focused error tests; expected PASS.

- [ ] **Step 4: Write failing migration settings tests**

Create tests for the public signatures below:

```python
class DatabaseSslMode(StrEnum):
    DISABLE = "disable"
    VERIFY_FULL = "verify-full"


class DatabaseMigrationSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    environment: RuntimeEnvironment = RuntimeEnvironment.LOCAL
    secret_root: Path = Path("/run/secrets")
    host: str = "127.0.0.1"
    port: int = 5432
    database_name: str = "knowledge"
    database_user: str = "knowledge_app"
    password_file_name: str = "postgres_application_password"
    ssl_mode: DatabaseSslMode = DatabaseSslMode.DISABLE
```

The exact function signatures are:

- `load_database_migration_settings(*, environ: Mapping[str, str] | None = None) -> DatabaseMigrationSettings`
- `read_database_password(settings: DatabaseMigrationSettings) -> SecretStr`
- `build_database_url(settings: DatabaseMigrationSettings, password: SecretStr) -> URL`
- `build_database_connect_arguments(settings: DatabaseMigrationSettings) -> Mapping[str, str | int]`

Required test cases:

- local defaults are secret root `/run/secrets`, host `127.0.0.1`, port `5432`, database `knowledge`, user `knowledge_app`, password file `postgres_application_password`, SSL `disable`; Windows tests pass an absolute temporary secret root because `/run/secrets` is the container/POSIX default;
- only the eight exact `KNOWLEDGE_*` names from the spec are accepted;
- an unknown `KNOWLEDGE_*` key maps to `DATABASE_MIGRATION_CONFIGURATION_INVALID` without echoing key/value;
- port is in `1..65535`; host/database/user are bounded trimmed non-empty safe connection fields;
- password filename is one relative filename and rejects separators, `.`, `..`, absolute paths and NUL;
- `local`/`test` accept only `disable`; `staging`/`production` accept only `verify-full`;
- missing/insecure/out-of-root password file preserves the existing `SecretFileError` contract;
- the SQLAlchemy URL has driver `postgresql+psycopg` and retains the password as a value without ever calling `render_as_string()`;
- connect arguments are exactly `connect_timeout=5`, `sslmode`, `application_name=knowledge-migration` and one PostgreSQL `options` string setting UTC and the three millisecond timeouts;
- repr, str and mapped failures never contain sentinel host, secret root, filename or password.

Run; expected FAIL because the runtime module is absent.

- [ ] **Step 5: Implement the frozen loader and URL boundary**

Use the exact environment map:

```python
DATABASE_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "environment",
        "KNOWLEDGE_SECRET_ROOT": "secret_root",
        "KNOWLEDGE_DATABASE_HOST": "host",
        "KNOWLEDGE_DATABASE_PORT": "port",
        "KNOWLEDGE_DATABASE_NAME": "database_name",
        "KNOWLEDGE_DATABASE_USER": "database_user",
        "KNOWLEDGE_DATABASE_PASSWORD_FILE": "password_file_name",
        "KNOWLEDGE_DATABASE_SSL_MODE": "ssl_mode",
    }
)
```

The settings loader reads `os.environ` only at call time, validates with Pydantic and maps only validation errors to `DatabaseMigrationError`. `read_database_password()` resolves `secret_root / password_file_name` and calls `read_secret_file()`. Do not catch `SecretFileError` and do not include Pydantic input values in mapped details. Require an absolute secret root before reading; on Windows local operators must override the POSIX/container default with an absolute Windows path as shown in Task 6.

Build the URL without string rendering:

```python
URL.create(
    drivername="postgresql+psycopg",
    username=settings.database_user,
    password=password.get_secret_value(),
    host=settings.host,
    port=settings.port,
    database=settings.database_name,
)
```

Build connect args with fixed values:

```python
{
    "connect_timeout": 5,
    "sslmode": settings.ssl_mode.value,
    "application_name": "knowledge-migration",
    "options": (
        "-c timezone=UTC -c lock_timeout=5000 "
        "-c statement_timeout=60000 "
        "-c idle_in_transaction_session_timeout=60000"
    ),
}
```

Run:

```powershell
uv run pytest tests/unit/migrations/test_database_migration_runtime.py tests/unit/error_contracts/test_application_errors.py -q
uv run ruff check migrations src/personal_os/error_contracts tests/unit/migrations
uv run mypy migrations src/personal_os/error_contracts
uv run lint-imports
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```powershell
git add pyproject.toml uv.lock migrations/__init__.py migrations/database_migration_runtime.py src/personal_os/error_contracts tests/unit/error_contracts/test_application_errors.py tests/unit/migrations/test_database_migration_runtime.py
git commit -m "feat: add safe database migration runtime"
```

---

### Task 2: One-Head Alembic Environment and Complete Baseline DDL

**Files:**

- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20260813_01_create_canonical_postgresql_baseline.py`
- Create: `tests/contract/test_canonical_postgresql_migration_contract.py`

**Interfaces:**

- Consumes: Task 1 settings/error runtime and the Alembic online command context.
- Produces: one revision `20260813_01`, one head, transactional online migration execution and the exact `knowledge` catalog contract.

- [ ] **Step 1: Write failing Alembic graph and configuration tests**

Contract tests must load `ScriptDirectory.from_config(Config("alembic.ini"))` and assert:

```python
assert script_directory.get_heads() == ["20260813_01"]
revision = script_directory.get_revision("20260813_01")
assert revision is not None
assert revision.down_revision is None
assert revision.branch_labels is None
assert revision.dependencies is None
```

Also assert:

- `alembic.ini` contains `script_location = %(here)s/migrations` and `transaction_per_migration = true`;
- it contains neither `sqlalchemy.url` nor any host/user/password/database value;
- `env.py` rejects offline mode and contains the exact destructive x-argument gate;
- no migration file imports application models, R2/Qdrant/Neo4j/Redis/Temporal SDKs or accesses `.env`/`DATABASE_URL`;
- the revision contains no `CASCADE`, `CREATE EXTENSION`, PostgreSQL enum, `INSERT`, seed data or UUID default;
- all DDL names fit in 63 encoded bytes.

Run; expected FAIL because Alembic artifacts do not exist.

- [ ] **Step 2: Create the online-only Alembic environment**

`alembic.ini` must keep logging bounded and set:

```ini
[alembic]
script_location = %(here)s/migrations
prepend_sys_path = .
transaction_per_migration = true
```

`migrations/env.py` must expose small typed helpers with these exact signatures: `_is_downgrade_command(config: Config) -> bool`, `_require_destructive_authorization(config: Config) -> None`, `_verify_database_prerequisites(connection: Connection, database_name: str) -> None` and `_run_online_migrations() -> None`.

Required execution order:

1. reject offline mode;
2. load frozen settings and password;
3. reject downgrade unless `context.get_x_argument(as_dictionary=True)` is exactly authorized;
4. create an engine from the in-memory `URL`, `NullPool` and fixed connect args;
5. configure Alembic with the connection, `transactional_ddl=True`, `transaction_per_migration=True`, `compare_type=True`, `include_schemas=True` and no target ORM metadata;
6. enter the Alembic transaction;
7. acquire `pg_advisory_xact_lock(hashtextextended('knowledge-schema-migration', 0))`;
8. verify `current_database()` equals the configured database and `current_setting('server_version_num')::integer / 10000 = 18`;
9. run migrations;
10. dispose the engine.

Catch configuration, SQLAlchemy and Psycopg failures only at this CLI boundary. Render only an approved `DatabaseMigrationError` code/message via `alembic.util.CommandError`; use `raise ... from None`. Classify a PostgreSQL lock timeout by SQLSTATE `55P03`, never by vendor message text, and map it to `DATABASE_MIGRATION_BUSY`; other connection/operational failures map to `DATABASE_CONNECTION_UNAVAILABLE`; wrong server/database and unmanaged schema/object failures map to `DATABASE_SCHEMA_CONTRACT_INVALID`. Never print the URL or raw driver exception.

- [ ] **Step 3: Write the complete handwritten revision**

Use these exact module identifiers:

```python
revision: str = "20260813_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
SCHEMA_NAME: Final[str] = "knowledge"
```

Upgrade order is schema, nine tables, circular source pointer FK, indexes, functions, triggers and final catalog assertion. Downgrade is the exact reverse, explicit and restrictive.

The following object-name manifest is normative:

| Table/object | Required named constraints/indexes/triggers |
|---|---|
| `users` | `pk_users`, `uq_users__username`, `ck_users__username_slug`, `ck_users__display_name`, `ck_users__status`, `ck_users__timestamps` |
| `workspaces` | `pk_workspaces`, `fk_workspaces__owner_user`, `uq_workspaces__owner_user`, `uq_workspaces__workspace_key`, `uq_workspaces__workspace_owner`, `ck_workspaces__workspace_key_slug`, `ck_workspaces__display_name`, `ck_workspaces__status`, `ck_workspaces__timestamps` |
| `devices` | `pk_devices`, `uq_devices__workspace_device`, `fk_devices__workspace_owner`, `ck_devices__device_name`, `ck_devices__device_kind`, `ck_devices__status`, `ck_devices__last_seen`, `ck_devices__revocation`, `ix_devices__workspace_user`, `ix_devices__workspace_status_registered` |
| `content_objects` | `pk_content_objects`, `uq_content_objects__content_hash`, `uq_content_objects__object_key`, `ck_content_objects__content_hash`, `ck_content_objects__object_key`, `ck_content_objects__byte_size`, `ck_content_objects__media_type`, `ck_content_objects__verification`, `trg_content_objects__reject_update` |
| `sources` | `pk_sources`, `uq_sources__workspace_source`, `fk_sources__workspace`, `fk_sources__current_version`, `ck_sources__source_type`, `ck_sources__title`, `ck_sources__sync_state`, `ck_sources__current_pointer`, `ck_sources__deletion`, `ck_sources__timestamps`, `ix_sources__workspace_state_updated` |
| `source_versions` | `pk_source_versions`, `uq_source_versions__workspace_source_version`, `uq_source_versions__source_ordinal`, `fk_source_versions__source`, `fk_source_versions__content_object`, `fk_source_versions__parent`, `ck_source_versions__content_version`, `ck_source_versions__parent`, `ck_source_versions__author`, `ix_source_versions__content_object`, `ix_source_versions__parent`, `trg_source_versions__reject_update` |
| `sync_events` | `pk_sync_events`, `uq_sync_events__workspace_event`, `uq_sync_events__source_event`, `uq_sync_events__event_sequence`, `uq_sync_events__idempotency_key`, `fk_sync_events__source`, `fk_sync_events__device`, `fk_sync_events__committed_version`, `fk_sync_events__base_version`, `ck_sync_events__idempotency_key`, `ck_sync_events__request_fingerprint`, `ck_sync_events__event_type`, `ix_sync_events__source_sequence`, `ix_sync_events__device`, `ix_sync_events__committed_version`, `ix_sync_events__base_version`, `trg_sync_events__reject_update` |
| `projection_intents` | `pk_projection_intents`, `uq_projection_intents__workspace_intent`, `uq_projection_intents__event_kind`, `fk_projection_intents__event_source`, `fk_projection_intents__source`, `fk_projection_intents__source_version`, `ck_projection_intents__projection_kind`, `ck_projection_intents__operation`, `ck_projection_intents__status`, `ck_projection_intents__attempt_count`, `ck_projection_intents__timestamps`, `ck_projection_intents__operation_version`, `ck_projection_intents__lease`, `ck_projection_intents__dispatch`, `ck_projection_intents__terminal_error`, `ck_projection_intents__error_code`, `ix_projection_intents__event_source`, `ix_projection_intents__source_version`, `ix_projection_intents__pending_dispatch`, `ix_projection_intents__source_status` |
| `audit_events` | `pk_audit_events`, `fk_audit_events__workspace`, `ck_audit_events__actor`, `ck_audit_events__actor_reference`, `ck_audit_events__action`, `ck_audit_events__target_kind`, `ck_audit_events__trace_id`, `ck_audit_events__result`, `ck_audit_events__reason_code`, `ck_audit_events__safe_diff_hash`, `ix_audit_events__workspace_occurred`, `ix_audit_events__target_lineage`, `ix_audit_events__request`, `trg_audit_events__reject_mutation` |

Use SQLAlchemy types with `schema="knowledge"`, explicit `server_default=sa.text("CURRENT_TIMESTAMP")` only where approved, and explicit `ondelete="RESTRICT"`. The source current-pointer FK is:

```python
op.create_foreign_key(
    "fk_sources__current_version",
    "sources",
    "source_versions",
    ["workspace_id", "source_id", "current_version_id"],
    ["workspace_id", "source_id", "source_version_id"],
    source_schema="knowledge",
    referent_schema="knowledge",
    ondelete="RESTRICT",
    deferrable=True,
    initially="IMMEDIATE",
)
```

Use default `MATCH SIMPLE`; do not specify `MATCH FULL`.

The event/source containment FK is one relation, not two independent checks:

```python
sa.ForeignKeyConstraint(
    ["workspace_id", "source_id", "event_id"],
    [
        "knowledge.sync_events.workspace_id",
        "knowledge.sync_events.source_id",
        "knowledge.sync_events.event_id",
    ],
    name="fk_projection_intents__event_source",
    ondelete="RESTRICT",
)
```

Implement exact check semantics from the approved spec. In particular:

- slug: `^[a-z0-9][a-z0-9._-]{0,63}$`;
- SHA-256: `^[0-9a-f]{64}$`;
- object key equals `objects/sha256/` plus hash characters `1..2`, `/`, `3..4`, `/`, full hash;
- idempotency key: printable ASCII `^[!-~]{1,200}$`;
- safe action/reason/reference tokens: lowercase bounded `^[a-z][a-z0-9_.:-]*$`;
- trace ID: exactly 32 lowercase hex and not `00000000000000000000000000000000`;
- media type: lowercase trimmed two non-empty RFC token components separated by exactly one `/`, no parameters;
- every closed vocabulary and timestamp/nullability rule listed in sections 8.1 through 8.9 of the approved spec.

Create trigger functions with fixed bodies and explicit safe search paths:

```sql
CREATE FUNCTION knowledge.reject_immutable_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'immutable_row_update_rejected';
END;
$$
```

`knowledge.reject_audit_mutation()` uses fixed message `audit_events_append_only`. Neither function interpolates table name, key or row content.

- [ ] **Step 4: Complete static contract assertions**

Parse revision source and Alembic graph to assert:

- exact nine table calls and no extra table;
- exact name manifest above and every identifier length;
- all foreign-key columns have a suitable leading unique/index path;
- `content_objects` lacks `workspace_id` and every relevant other table has it;
- `sources` has no content/object key/locator/path/url/metadata body column;
- only `event_sequence` has identity and no UUID has a server default;
- current pointer is deferrable/initially immediate and not `MATCH FULL`;
- exactly two functions and four triggers have fixed safe messages;
- downgrade lists known objects in reverse and contains no cascade;
- only one revision/head exists.

Run:

```powershell
uv run alembic heads
uv run pytest tests/contract/test_canonical_postgresql_migration_contract.py -q
uv run ruff check migrations tests/contract/test_canonical_postgresql_migration_contract.py
uv run mypy migrations
```

Expected: one head `20260813_01` and all static tests PASS. `alembic heads` must not require database settings or touch the filesystem secret.

- [ ] **Step 5: Commit Task 2**

```powershell
git add alembic.ini migrations/env.py migrations/script.py.mako migrations/versions/20260813_01_create_canonical_postgresql_baseline.py tests/contract/test_canonical_postgresql_migration_contract.py
git commit -m "feat: create canonical PostgreSQL baseline migration"
```

---

### Task 3: Disposable Upgrade, Catalog Fingerprint and Valid Canonical Graph

**Files:**

- Create: `tests/integration/test_canonical_postgresql_baseline.py`
- Modify: `tests/integration/README.md`

**Interfaces:**

- Consumes: exact `knowledge-ci-*` project name, local-stack lifecycle, ignored generated PostgreSQL application password, Alembic CLI and Psycopg catalog queries.
- Produces: normalized catalog fingerprint and a valid row graph covering all nine tables.

- [ ] **Step 1: Write the failing disposable lifecycle fixture**

Mark the entire module:

```python
pytestmark = pytest.mark.local_stack
```

The fixture must:

1. read `LOCAL_STACK_TEST_PROJECT`, pass it through the existing `validate_project_name()` contract, require the `knowledge-ci-` prefix and reject `knowledge-local`;
2. call local-stack `reset`, `bootstrap`, `config` and `up` for that exact project;
3. resolve `.local/stack-secrets/postgres_application_password`, remove every inherited `KNOWLEDGE_*` key from the child environment and add only the eight approved migration environment keys for Alembic subprocesses;
4. connect with Psycopg keyword arguments, never a DSN;
5. in `finally`, run gated downgrade when possible, then reset the exact project and inspect Docker project labels for zero remaining container/network/volume resources.

First assertion: before upgrade, `to_regnamespace('knowledge') IS NULL`.

Run:

```powershell
$env:LOCAL_STACK_TEST_PROJECT='knowledge-ci-manual-baseline'
$env:CI='true'
uv run pytest tests/integration/test_canonical_postgresql_baseline.py -m local_stack -q
```

Expected: FAIL because lifecycle helpers/assertions are incomplete; cleanup still succeeds.

- [ ] **Step 2: Add exact catalog normalization**

Create typed helpers that query `pg_catalog`/`information_schema` for:

- schema owner and privileges;
- tables and columns including type, nullability, default and identity;
- primary/unique/check/foreign constraints, referenced columns, delete action, deferrability and initial mode;
- indexes, column order, sort direction and predicates;
- functions, language, configuration and body digest;
- triggers, timing/events/function;
- sequences owned by identity columns.

Normalize rows into sorted tuples and hash canonical JSON with SHA-256. Do not include OIDs, physical relfilenodes, autogenerated identity sequence names or timestamps. Assert the exact object set and store the fingerprint only in memory.

- [ ] **Step 3: Prove empty-to-head and valid data behavior**

Run real subprocess commands with captured output:

```text
uv run alembic upgrade head
uv run alembic current --check-heads
```

Insert, in one explicit transaction, fixed test UUIDs for:

- one user and owned workspace;
- one Obsidian device;
- one global verified content object;
- one pending source, then version 1, then update source to `active` with that current pointer;
- one sync event committing version 1;
- one Qdrant upsert intent and one Neo4j upsert intent for the same event;
- one succeeded audit event.

The source-pointer FK is initially immediate, so execute `SET CONSTRAINTS knowledge.fk_sources__current_version DEFERRED` only in the transaction that must form the circular source/version graph. Assert row counts are exactly `1,1,1,1,1,1,1,2,1` in table order.

Add allowed behavior cases:

- a second source may begin at `content_version=1`;
- two versions may reference the same global content object;
- a Web/system event may use null `device_id`;
- a delete projection intent may retain `source_version_id`;
- an audit event may have null `target_id`.

- [ ] **Step 4: Assert ownership, grants and data minimization**

Assert:

- schema/table/function owner is `knowledge_app`;
- `PUBLIC` lacks schema create/usage and application object privileges;
- `public.alembic_version` exists and `knowledge.alembic_version` does not;
- no column name matches the forbidden body/locator/query/vector/token/secret/provider corpus;
- no baseline table uses JSON/JSONB;
- the exact catalog fingerprint is stable across two reads.

Update `tests/integration/README.md` from “one executable test” to two owned tests and document that the baseline test may access only generated local PostgreSQL credentials, never R2/provider credentials.

Run:

```powershell
uv run pytest tests/integration/test_canonical_postgresql_baseline.py -m local_stack -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```powershell
git add tests/integration/test_canonical_postgresql_baseline.py tests/integration/README.md
git commit -m "test: prove canonical PostgreSQL upgrade and catalog"
```

---

### Task 4: Negative Constraints, Referential Lineage and Immutability

**Files:**

- Modify: `tests/integration/test_canonical_postgresql_baseline.py`
- Modify: `migrations/versions/20260813_01_create_canonical_postgresql_baseline.py` only when a failing test proves the DDL is incomplete.

**Interfaces:**

- Consumes: the valid graph and direct SQL transactions from Task 3.
- Produces: database-enforced rejection evidence for every baseline invariant.

- [ ] **Step 1: Add one isolated failing transaction per identity/ownership invariant**

Use a helper that opens a savepoint, executes one mutation, asserts expected SQLSTATE (`23505`, `23514`, `23503` or `55000`) and rolls back the savepoint. Never assert vendor message text except the two approved trigger messages.

Cover:

- duplicate username, owner user and workspace key;
- device whose `(workspace_id, user_id)` is not a workspace owner;
- invalid device kind/status/last-seen/revocation combinations;
- physical parent deletes blocked by lineage FKs.

Run the focused parametrized group; expected FAIL until all names/checks are correct.

- [ ] **Step 2: Add CAS and source/version invariant tests**

Cover:

- uppercase/short hash, mismatched object-key segments, negative size, parameterized/uppercase/invalid media type and verification after creation;
- duplicate global hash and object key;
- invalid source type/state/deleted/current-pointer combinations;
- current pointer from another source or workspace;
- duplicate/nonpositive content version, cross-source/workspace parent, self-parent and nonexistent content object;
- invalid source-version author kind/author ID combination;
- update of `content_objects` and `source_versions` returns SQLSTATE `55000` and fixed message `immutable_row_update_rejected`.

Also prove a referenced content object, source version and source cannot be physically deleted while lineage exists.

- [ ] **Step 3: Add event/intent invariant tests**

Cover:

- duplicate event ID, generated event sequence and workspace idempotency key;
- event device/committed/base version from another workspace/source;
- malformed idempotency key, request fingerprint and event type;
- update of sync event returns the fixed immutable message;
- duplicate `(workspace_id,event_id,projection_kind)`;
- intent whose event belongs to another source, invalid projection kind/operation/status, negative attempts and timestamp order;
- upsert without version, lease fields inconsistent with status, invalid dispatched fields, terminal without error and unsafe error token.

- [ ] **Step 4: Add audit invariant and append-only tests**

Cover every actor shape (`user`, `device`, `system`, `workflow`) and reject invalid combinations. Reject unsafe/empty action, target, reference and reason tokens, zero/uppercase/short trace ID, invalid diff hash and invalid result. Assert update and delete both return SQLSTATE `55000` and fixed message `audit_events_append_only`.

- [ ] **Step 5: Run the complete constraint suite and static contracts**

```powershell
uv run pytest tests/integration/test_canonical_postgresql_baseline.py -m local_stack -q
uv run pytest tests/contract/test_canonical_postgresql_migration_contract.py -q
uv run ruff check migrations tests/integration/test_canonical_postgresql_baseline.py
uv run mypy migrations
```

Expected: PASS with every negative transaction isolated and the valid graph still present afterward.

- [ ] **Step 6: Commit Task 4**

```powershell
git add migrations/versions/20260813_01_create_canonical_postgresql_baseline.py tests/integration/test_canonical_postgresql_baseline.py
git commit -m "test: enforce canonical PostgreSQL invariants"
```

---

### Task 5: Transaction Rollback, Migration Lock and Destructive Recovery

**Files:**

- Modify: `migrations/env.py`
- Modify: `migrations/versions/20260813_01_create_canonical_postgresql_baseline.py`
- Modify: `tests/unit/migrations/test_database_migration_runtime.py`
- Modify: `tests/integration/test_canonical_postgresql_baseline.py`

**Interfaces:**

- Consumes: Alembic subprocesses, a second Psycopg connection and controlled disposable catalog mutations.
- Produces: bounded concurrent execution, fail-closed downgrade and fingerprint-equivalent recovery.

- [ ] **Step 1: Write failing destructive-gate and no-op tests**

With valid rows present:

1. run `upgrade head` again and assert success, unchanged row counts and unchanged fingerprint;
2. run `alembic downgrade base` without x-argument and assert a safe nonzero result, unchanged head/data/fingerprint;
3. prove captured stdout/stderr contains only the registered refusal code/message and no host, filename, secret root, password or URL.

Expected: FAIL until the pre-DDL gate and safe CLI mapping are correct.

- [ ] **Step 2: Prove the advisory lock is bounded and retryable**

Open a second connection and begin a transaction holding:

```sql
SELECT pg_advisory_xact_lock(hashtextextended('knowledge-schema-migration', 0));
```

Run an Alembic command against the same database. Assert it returns nonzero after at least 4 seconds and within 15 seconds, maps to `database_migration_busy`, leaks no raw driver text and creates/drops no object. Release the holder transaction and assert the same command succeeds.

Then start two first-upgrade subprocesses against a freshly downgraded database and assert one exact final head, one exact catalog and no duplicate object. Either subprocess may wait/succeed; neither may leave partial DDL.

- [ ] **Step 3: Prove late upgrade failure rolls back the whole schema**

Inject a test-only failure through a callable stored under the exact Alembic `Config.attributes` key `canonical_baseline_before_verify`; the revision invokes it immediately before final catalog verification. Do not add an environment variable, CLI argument or provider-controlled branch for this seam. Run the command through the Alembic Python API against an empty disposable database and assert:

- the transaction fails;
- `knowledge` is absent;
- `public.alembic_version` has no applied revision;
- the mapped error contains no injected exception sentinel.

Remove the hook and prove normal upgrade succeeds. The hook must be unreachable from CLI/environment input.

- [ ] **Step 4: Prove restrictive downgrade and re-upgrade recovery**

Sequence:

1. capture fingerprint A at populated head;
2. run `uv run alembic -x allow_destructive=true downgrade base`;
3. assert `knowledge` and every owned table/index/sequence/function/trigger are absent, while `public.alembic_version` may remain empty;
4. upgrade head and capture fingerprint B; assert B equals A;
5. create an unexpected dependent view on an application table;
6. gated downgrade and assert failure rolls back completely, leaving exact head and view intact;
7. explicitly drop only the test view;
8. gated downgrade succeeds;
9. upgrade once more and assert the exact head/fingerprint;
10. gated downgrade in fixture cleanup.

Downgrade implementation drops four triggers, two functions, tables and schema in exact reverse dependency order. It uses `DROP SCHEMA knowledge RESTRICT`; never `CASCADE`.

- [ ] **Step 5: Prove interruption atomicity**

Start first upgrade in a child process with the same `canonical_baseline_before_verify` callable signaling a multiprocessing event, terminate the client only after DDL begins, reconnect and assert the database is either exact base or exact head. PostgreSQL must never expose a subset of the nine tables. If the server completes the transaction before termination, accept exact head; otherwise require exact base.

- [ ] **Step 6: Run and commit recovery behavior**

```powershell
uv run pytest tests/unit/migrations/test_database_migration_runtime.py -q
uv run pytest tests/integration/test_canonical_postgresql_baseline.py -m local_stack -q
uv run pytest tests/contract/test_canonical_postgresql_migration_contract.py -q
```

Expected: PASS.

```powershell
git add migrations/env.py migrations/versions/20260813_01_create_canonical_postgresql_baseline.py tests/unit/migrations/test_database_migration_runtime.py tests/integration/test_canonical_postgresql_baseline.py
git commit -m "test: prove PostgreSQL migration recovery"
```

---

### Task 6: Operator Commands and Local Documentation

**Files:**

- Modify: `pyproject.toml`
- Modify: `infra/compose/README.md`
- Modify: `tests/contract/test_canonical_postgresql_migration_contract.py`

**Interfaces:**

- Consumes: approved database environment and existing `stack-bootstrap`, `stack-up` and `stack-reset` commands.
- Produces: cross-platform Poe migration commands and a safe local operator runbook.

- [ ] **Step 1: Write failing command/documentation contracts**

Require these exact public tasks:

```toml
[tool.poe.tasks.database-heads]
cmd = "alembic heads"

[tool.poe.tasks.database-current]
cmd = "alembic current --check-heads"

[tool.poe.tasks.database-upgrade]
cmd = "alembic upgrade head"

[tool.poe.tasks.database-downgrade]
cmd = "alembic -x allow_destructive=true downgrade base"
```

Tests assert no task embeds a password, URL, project-specific path or production host.

- [ ] **Step 2: Add the safe local runbook**

Document, in order:

```powershell
uv run poe stack-bootstrap
uv run poe stack-up
$env:KNOWLEDGE_ENVIRONMENT='local'
$env:KNOWLEDGE_SECRET_ROOT=(Resolve-Path '.local/stack-secrets').Path
uv run poe database-heads
uv run poe database-upgrade
uv run poe database-current
```

Explain that downgrade destroys all canonical rows and show it only in a clearly marked disposable-development reset section. Include Linux equivalents using an absolute `realpath`. State that staging/production require `verify-full`, verified backup and deployment authorization; do not invent the later CA-file contract.

Document safe failures by code only. Never show a connection URL or secret content.

- [ ] **Step 3: Verify and commit Task 6**

```powershell
uv run pytest tests/contract/test_canonical_postgresql_migration_contract.py -q
uv run poe database-heads
uv run ruff check migrations tests/contract/test_canonical_postgresql_migration_contract.py
```

Expected: PASS and exactly one head.

```powershell
git add pyproject.toml infra/compose/README.md tests/contract/test_canonical_postgresql_migration_contract.py
git commit -m "docs: add canonical database migration runbook"
```

---

### Task 7: Cross-Platform Static CI and Ubuntu PostgreSQL Lifecycle

**Files:**

- Create: `.github/workflows/canonical-postgresql-baseline.yml`
- Modify: `tests/contract/test_ci_security.py`

**Interfaces:**

- Consumes: frozen workspace, pinned Compose installer, generated local-stack credentials and baseline test commands.
- Produces: Windows static assurance and Ubuntu PostgreSQL 18.4 lifecycle assurance with exact cleanup and sanitized JUnit-only artifact.

- [ ] **Step 1: Write failing workflow security contracts**

Add `CANONICAL_POSTGRESQL_WORKFLOW_PATH` and require:

- top-level `permissions: contents: read`, no job override and every action pinned to a 40-hex SHA;
- `pull_request`, push to `master`, schedule and manual triggers with path filters for migrations, spec, tests, `pyproject.toml`, `uv.lock`, Compose PostgreSQL files and this workflow;
- concurrency cancellation and positive finite timeout per job;
- one Windows static job and one Ubuntu lifecycle job;
- no `secrets.`, R2/Cloudflare/provider token, deployment, publish, database dump, Docker/server log or environment upload;
- only `.local/test-results/canonical-postgresql-baseline.xml` may be uploaded;
- exact-project cleanup runs under `if: always()` and validates container/network/volume labels are empty.

Run; expected FAIL because workflow is absent.

- [ ] **Step 2: Implement the Windows static job**

Reuse the exact SHA-pinned checkout/setup-uv actions and Python versions from `quality.yml`. The job runs:

```text
uv sync --all-packages --frozen
uv run alembic heads
uv run pytest tests/unit/migrations/test_database_migration_runtime.py tests/contract/test_canonical_postgresql_migration_contract.py tests/contract/test_ci_security.py -q
uv run ruff check migrations tests/unit/migrations tests/contract/test_canonical_postgresql_migration_contract.py
uv run mypy migrations
```

It does not install/start Docker or read a database secret.

- [ ] **Step 3: Implement the Ubuntu lifecycle job**

Reuse the verified Docker Compose `v2.30.0` Linux binary and SHA-256 from `local-service-stack.yml`. Set:

```yaml
env:
  CI: "true"
  LOCAL_STACK_TEST_PROJECT: knowledge-ci-${{ github.run_id }}-${{ github.run_attempt }}
```

Validate the project pattern and length, frozen-sync Python, then run:

```bash
mkdir -p .local/test-results
uv run pytest tests/integration/test_canonical_postgresql_baseline.py -m local_stack -q \
  --junitxml=.local/test-results/canonical-postgresql-baseline.xml
```

Under `if: always()`, run exact gated cleanup for the known disposable project and assert no matching resource label remains. Upload only the JUnit file with `include-hidden-files: true`, `if-no-files-found: ignore` and retention 7 days. Never upload `.local/stack-secrets`, Compose config, environment, database dump or logs.

- [ ] **Step 4: Mutation-check the CI contract**

Add in-memory mutations that prove tests fail if a job gets write permission, an action loses SHA pinning, the project becomes `knowledge-local`, cleanup loses `if: always()`, a provider secret appears or artifact scope widens.

- [ ] **Step 5: Verify and commit Task 7**

```powershell
uv run pytest tests/contract/test_ci_security.py tests/contract/test_canonical_postgresql_migration_contract.py -q
uv run ruff check tests/contract/test_ci_security.py
```

Expected: PASS.

```powershell
git add .github/workflows/canonical-postgresql-baseline.yml tests/contract/test_ci_security.py
git commit -m "ci: verify canonical PostgreSQL migration lifecycle"
```

---

### Task 8: Final Acceptance, Leakage Audit and Review

**Files:**

- Modify only files proven incomplete by the acceptance run.

**Interfaces:**

- Consumes: all Task 1-7 deliverables.
- Produces: one reviewable, verified implementation satisfying every approved acceptance criterion.

- [ ] **Step 1: Run static and graph acceptance on the host**

```powershell
$env:UV_PROJECT_ENVIRONMENT='.venv-canonical-postgresql'
uv sync --all-packages --frozen
uv run alembic heads
uv run pytest tests/unit/migrations/test_database_migration_runtime.py tests/contract/test_canonical_postgresql_migration_contract.py tests/contract/test_ci_security.py -q
uv run ruff format --check migrations tests
uv run ruff check migrations tests
uv run mypy migrations src/personal_os/error_contracts
uv run lint-imports
```

Expected: one head `20260813_01`; all commands PASS.

- [ ] **Step 2: Run the final disposable PostgreSQL acceptance from the same commit**

On a Linux amd64 Docker host:

```bash
export LOCAL_STACK_TEST_PROJECT="knowledge-ci-final-$(git rev-parse --short HEAD)"
export CI=true
uv run pytest tests/integration/test_canonical_postgresql_baseline.py -m local_stack -q
```

Expected: empty → head → populated/negative checks → refused downgrade → base → head fingerprint equality → final cleanup all PASS.

- [ ] **Step 3: Run the complete repository gate**

```powershell
$env:UV_PROJECT_ENVIRONMENT='.venv-canonical-postgresql'
uv run --all-packages --frozen poe verify
```

Expected: formatting, lint, strict typing, boundaries, Python/TypeScript tests and builds all PASS. The normal Python suite continues to deselect `local_stack` tests.

- [ ] **Step 4: Perform an explicit leakage scan**

Capture migration success and every failure class using sentinels for host, secret root, filename, password and driver error. Assert none appears in stdout, stderr, exception repr, JUnit or diagnostic event output. Scan tracked artifacts:

```powershell
rg -n "DATABASE_URL|sqlalchemy\.url|DO_NOT_LEAK|cloudflarestorage|R2_|postgresql\+psycopg://" alembic.ini migrations tests .github infra pyproject.toml
```

Expected: only deliberate forbidden-pattern assertions in tests; no URL, credential or provider configuration in migration/runtime/workflow artifacts.

- [ ] **Step 5: Review diff and canonical coverage**

Use `superpowers:requesting-code-review`. Reviewer must check all 20 acceptance criteria, exact nine-table catalog, global content-object boundary, Obsidian/backend ID authority, current-pointer `MATCH SIMPLE`, event/source intent containment, trigger safety, rollback, destructive gate, CI cleanup and absence of out-of-scope code.

Then inspect:

```powershell
git status --short
git diff --check
git diff --stat master...HEAD
git log --oneline --decorate -8
(Get-Content AGENTS.md).Count
(Get-Content CLAUDE.md).Count
```

Expected: no unintended changes, no whitespace errors, instruction-file counts unchanged unless explicitly justified, and only approved artifacts differ.

- [ ] **Step 6: Commit any review-only correction and stop**

If review required a correction, rerun the affected focused test and full gate, then commit with a semantic message. Do not merge, push or implement bootstrap/storage/commit services as part of this plan. Use `superpowers:finishing-a-development-branch` to present integration options.
