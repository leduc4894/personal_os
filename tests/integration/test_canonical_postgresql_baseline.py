"""Disposable PostgreSQL 18.4 upgrade lifecycle for the canonical baseline.

This is the first live integration test for the canonical application database.
It owns a single disposable ``knowledge-ci-*`` local-stack project, proves the
real Alembic empty-to-head upgrade against PostgreSQL 18.4, fingerprints the
resulting catalog, inserts a valid row graph across all nine baseline tables,
exercises the allowed-behavior cases, asserts ownership/grants/data-minimization,
and proves the full lifecycle (``upgrade -> downgrade -> re-upgrade``) yields a
stable normalized catalog fingerprint.

The baseline test may access ONLY generated local PostgreSQL credentials beneath
``.local/stack-secrets/``. It never reads or renders R2/provider credentials, a
plaintext password, a DSN, ``DATABASE_URL`` or ``.env`` value. The PostgreSQL
application password is read from the bounded secret file and passed to Psycopg
as keyword arguments (never a connection string).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.local_service_stack import main, validate_project_name

pytestmark = pytest.mark.local_stack

# --- Repository-bound paths and connection constants -------------------------

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[2]
_SECRET_ROOT: Path = (_WORKTREE_ROOT / ".local" / "stack-secrets").resolve()
_APPLICATION_DATABASE: str = "knowledge"
_APPLICATION_USER: str = "knowledge_app"
_DATABASE_HOST: str = "127.0.0.1"
_SSL_MODE: str = "disable"
_APPLICATION_PASSWORD_FILENAME: str = "postgres_application_password"
_ALEMBIC_APPLICATION_NAME: str = "knowledge-baseline-test"
_BASELINE_REVISION: str = "20260813_01"

_TABLES_IN_COUNT_ORDER: tuple[str, ...] = (
    "users",
    "workspaces",
    "devices",
    "content_objects",
    "sources",
    "source_versions",
    "sync_events",
    "projection_intents",
    "audit_events",
)

# Exact expected object sets (the migration is the source of truth for names).
_EXPECTED_TABLES: frozenset[str] = frozenset(_TABLES_IN_COUNT_ORDER)
_EXPECTED_FUNCTIONS: frozenset[str] = frozenset(
    {"reject_immutable_update", "reject_audit_mutation"}
)
_EXPECTED_TRIGGERS: frozenset[str] = frozenset(
    {
        "trg_audit_events__reject_mutation",
        "trg_content_objects__reject_update",
        "trg_source_versions__reject_update",
        "trg_sync_events__reject_update",
    }
)
_EXPECTED_INDEXES: frozenset[str] = frozenset(
    {
        "ix_devices__workspace_user",
        "ix_devices__workspace_status_registered",
        "ix_sources__workspace_state_updated",
        "ix_source_versions__content_object",
        "ix_source_versions__parent",
        "ix_sync_events__source_sequence",
        "ix_sync_events__device",
        "ix_sync_events__committed_version",
        "ix_sync_events__base_version",
        "ix_projection_intents__event_source",
        "ix_projection_intents__source_version",
        "ix_projection_intents__pending_dispatch",
        "ix_projection_intents__source_status",
        "ix_audit_events__workspace_occurred",
        "ix_audit_events__target_lineage",
        "ix_audit_events__request",
    }
)
_CONSTRAINT_NAME_PATTERN = re.compile(r"^(?:pk|fk|uq|ck)_[a-z0-9_]+$")
_INDEX_NAME_PATTERN = re.compile(r"^(?:pk|uq|ix)_[a-z0-9_]+$")
# Word-boundary corpus scan: ``lease_token`` does NOT match because ``_`` is a
# word character, so there is no boundary before ``token``.
_FORBIDDEN_COLUMN_PATTERN = re.compile(r"\b(?:body|locator|query|vector|token|secret|provider)\b")

# --- Fixed test UUIDs and approved SHA-256 values ----------------------------

_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000002")
_DEVICE_ID = UUID("00000000-0000-0000-0000-000000000003")
_CONTENT_OBJECT_ID = UUID("00000000-0000-0000-0000-000000000004")
_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000005")
_SOURCE_VERSION_ID = UUID("00000000-0000-0000-0000-000000000006")
_EVENT_ID = UUID("00000000-0000-0000-0000-000000000007")
_QDRANT_INTENT_ID = UUID("00000000-0000-0000-0000-000000000008")
_NEO4J_INTENT_ID = UUID("00000000-0000-0000-0000-000000000009")
_AUDIT_EVENT_ID = UUID("00000000-0000-0000-0000-00000000000a")
_REQUEST_ID = UUID("00000000-0000-0000-0000-00000000000b")

# Fixed, valid 64-char lowercase hex SHA-256 values (computed once for stability).
_CONTENT_HASH: str = hashlib.sha256(b"baseline-canonical-content-bytes").hexdigest()
_CONTENT_OBJECT_KEY: str = (
    f"objects/sha256/{_CONTENT_HASH[:2]}/{_CONTENT_HASH[2:4]}/{_CONTENT_HASH}"
)
_REQUEST_FINGERPRINT: str = hashlib.sha256(b"baseline-canonical-sync-envelope").hexdigest()

# Distinct UUIDs for the allowed-behavior cases (each runs in a rolled-back tx).
_ALT_SOURCE_ID = UUID("11111111-0000-0000-0000-000000000001")
_ALT_SOURCE_VERSION_ID = UUID("11111111-0000-0000-0000-000000000002")
_ALT_EVENT_ID = UUID("11111111-0000-0000-0000-000000000003")
_ALT_VERSION_TWO_ID = UUID("11111111-0000-0000-0000-000000000004")
_WEB_EVENT_ID = UUID("22222222-0000-0000-0000-000000000001")
_DELETE_EVENT_ID = UUID("33333333-0000-0000-0000-000000000001")
_DELETE_INTENT_ID = UUID("33333333-0000-0000-0000-000000000002")
_AUDIT_NULL_TARGET_ID = UUID("44444444-0000-0000-0000-000000000001")


# --- Catalog SQL (pg_catalog / information_schema) ---------------------------

_SCHEMA_CATALOG_SQL = """
SELECT
    n.nspname AS schema_name,
    pg_get_userbyid(n.nspowner) AS owner,
    COALESCE(
        (SELECT array_agg(privilege_type ORDER BY privilege_type)
         FROM aclexplode(n.nspacl)),
        ARRAY[]::text[]
    ) AS public_privileges
FROM pg_namespace n
WHERE n.nspname = 'knowledge'
"""

_COLUMN_CATALOG_SQL = """
SELECT
    c.relname AS table_name,
    a.attnum,
    a.attname AS column_name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS type_text,
    a.attnotnull AS not_null,
    pg_get_expr(d.adbin, d.adrelid) AS default_expr,
    a.attidentity AS identity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = 'knowledge'
  AND c.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

# contype IN ('p','u','c','f') only: NOT NULL ('n') is a PostgreSQL 18 feature
# with autogenerated names; nullability is already captured on the column.
_CONSTRAINT_CATALOG_SQL = """
SELECT
    c.relname AS table_name,
    con.conname,
    con.contype,
    pg_get_constraintdef(con.oid, true) AS definition,
    con.confdeltype AS delete_action,
    con.condeferrable AS deferrable,
    con.condeferred AS initially_deferred
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'knowledge' AND con.contype IN ('p', 'u', 'c', 'f')
ORDER BY c.relname, con.conname
"""

_INDEX_CATALOG_SQL = """
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'knowledge'
ORDER BY indexname
"""

_FUNCTION_CATALOG_SQL = """
SELECT
    p.proname AS function_name,
    l.lanname AS language_name,
    pg_get_functiondef(p.oid) AS definition
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname = 'knowledge'
ORDER BY p.proname
"""

_TRIGGER_CATALOG_SQL = """
SELECT
    c.relname AS table_name,
    t.tgname AS trigger_name,
    pg_get_triggerdef(t.oid, true) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'knowledge' AND NOT t.tgisinternal
ORDER BY c.relname, t.tgname
"""

# Identity-owned sequences: capture owning (table, column) only. The sequence
# name is autogenerated (excluded by design).
_IDENTITY_SEQUENCE_CATALOG_SQL = """
SELECT
    c.relname AS owning_table,
    a.attname AS owning_column
FROM pg_depend d
JOIN pg_class s ON s.oid = d.objid
JOIN pg_class c ON c.oid = d.refobjid
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.refobjsubid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE d.classid = 'pg_class'::regclass
  AND d.objsubid = 0
  AND s.relkind = 'S'
  AND n.nspname = 'knowledge'
ORDER BY c.relname, a.attname
"""


# --- Lifecycle and connection fixture ----------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineStack:
    """Disposable stack handle: catalog connection plus the Alembic child env."""

    project_name: str
    port: int
    alembic_env: Mapping[str, str]
    connection: psycopg.Connection[Any]


def _require_project_name() -> str:
    raw_name = os.environ.get("LOCAL_STACK_TEST_PROJECT")
    if raw_name is None:
        pytest.fail("LOCAL_STACK_TEST_PROJECT must name a unique knowledge-ci-* project")
    validate_project_name(raw_name)
    if not raw_name.startswith("knowledge-ci-"):
        pytest.fail("LOCAL_STACK_TEST_PROJECT must start with 'knowledge-ci-'")
    if raw_name == "knowledge-local":
        pytest.fail("LOCAL_STACK_TEST_PROJECT must not be the operator 'knowledge-local' project")
    if os.environ.get("CI") != "true":
        pytest.fail("CI must be 'true' to operate a disposable knowledge-ci-* stack")
    return raw_name


def _resolved_host_port() -> int:
    return int(os.environ.get("POSTGRES_PORT", "5432"))


def _read_application_password() -> str:
    resolved_root = _SECRET_ROOT.resolve(strict=True)
    secret_path = (resolved_root / _APPLICATION_PASSWORD_FILENAME).resolve(strict=True)
    if not secret_path.is_relative_to(resolved_root):
        pytest.fail("application password must resolve beneath the bounded secret root")
    return secret_path.read_text(encoding="ascii").strip()


def _build_alembic_environment(port: int) -> dict[str, str]:
    env = dict(os.environ)
    for inherited_key in [key for key in env if key.startswith("KNOWLEDGE_")]:
        del env[inherited_key]
    env.update(
        {
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_SECRET_ROOT": str(_SECRET_ROOT),
            "KNOWLEDGE_DATABASE_HOST": _DATABASE_HOST,
            "KNOWLEDGE_DATABASE_PORT": str(port),
            "KNOWLEDGE_DATABASE_NAME": _APPLICATION_DATABASE,
            "KNOWLEDGE_DATABASE_USER": _APPLICATION_USER,
            "KNOWLEDGE_DATABASE_PASSWORD_FILE": _APPLICATION_PASSWORD_FILENAME,
            "KNOWLEDGE_DATABASE_SSL_MODE": _SSL_MODE,
        }
    )
    return env


def _run_stack_steps(project_name: str) -> None:
    steps: tuple[tuple[str, ...], ...] = (
        ("reset", "--project-name", project_name, "--confirm-project", project_name,
         "--non-interactive"),
        ("bootstrap", "--project-name", project_name),
        ("config", "--project-name", project_name),
        ("up", "--project-name", project_name),
    )
    for argv in steps:
        return_code = main(list(argv))
        assert return_code == 0, f"local-stack step '{argv[0]}' failed with code {return_code}"


def _run_gated_downgrade_if_at_head(alembic_env: Mapping[str, str]) -> None:
    """Tolerant teardown: downgrade to base only when the stack is up and at head."""
    with suppress(AssertionError, subprocess.CalledProcessError, OSError):
        run_alembic(["-x", "allow_destructive=true", "downgrade", "base"], alembic_env)


def _count_project_resources(project_name: str) -> dict[str, int]:
    label = f"label=com.docker.compose.project={project_name}"
    commands: dict[str, list[str]] = {
        "container": ["docker", "container", "ls", "--all", "--quiet", "--filter", label],
        "network": ["docker", "network", "ls", "--quiet", "--filter", label],
        "volume": ["docker", "volume", "ls", "--quiet", "--filter", label],
    }
    counts: dict[str, int] = {}
    for resource, command in commands.items():
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        counts[resource] = len(lines)
    return counts


def _assert_project_absent(project_name: str) -> None:
    counts = _count_project_resources(project_name)
    leftover = {resource: count for resource, count in counts.items() if count}
    assert not leftover, f"disposable project left resources behind: {leftover}"


@pytest.fixture()
def baseline_stack() -> Iterator[BaselineStack]:
    project_name = _require_project_name()
    port = _resolved_host_port()
    _run_stack_steps(project_name)
    password = _read_application_password()
    alembic_env = _build_alembic_environment(port)
    connection = psycopg.connect(
        host=_DATABASE_HOST,
        port=port,
        user=_APPLICATION_USER,
        password=password,
        dbname=_APPLICATION_DATABASE,
        sslmode=_SSL_MODE,
        application_name=_ALEMBIC_APPLICATION_NAME,
    )
    connection.autocommit = True
    try:
        yield BaselineStack(project_name, port, alembic_env, connection)
    finally:
        with suppress(psycopg.Error):
            connection.close()
        _run_gated_downgrade_if_at_head(alembic_env)
        with suppress(Exception):
            main(["reset", "--project-name", project_name, "--confirm-project",
                  project_name, "--non-interactive"])
        _assert_project_absent(project_name)


# --- Subprocess and query helpers --------------------------------------------


def run_alembic(
    arguments: Sequence[str], alembic_env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``uv run alembic`` in the worktree with the sanitized child env.

    Output is captured (never printed) and used only for assertions. Alembic
    diagnostics are leak-safe by design; the password/DSN are never rendered.
    """
    command = ["uv", "run", "alembic", *arguments]
    return subprocess.run(
        command,
        cwd=str(_WORKTREE_ROOT),
        env=dict(alembic_env),
        capture_output=True,
        text=True,
        check=False,
    )


def _scalar(conn: psycopg.Connection[Any], sql: str) -> Any:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    return row[0] if row is not None else None


def _rows(conn: psycopg.Connection[Any], sql: str) -> list[tuple[Any, ...]]:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


def _schema_exists(conn: psycopg.Connection[Any]) -> bool:
    return _scalar(conn, "SELECT to_regnamespace('knowledge')") is not None


def _relation_exists(conn: psycopg.Connection[Any], qualified_name: str) -> bool:
    return _scalar(conn, f"SELECT to_regclass('{qualified_name}')") is not None


# --- Catalog normalization and fingerprint -----------------------------------


def _fetch_catalog(conn: psycopg.Connection[Any]) -> dict[str, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(_SCHEMA_CATALOG_SQL)
        schema = list(cursor.fetchall())
        cursor.execute(_COLUMN_CATALOG_SQL)
        columns = list(cursor.fetchall())
        cursor.execute(_CONSTRAINT_CATALOG_SQL)
        constraints = list(cursor.fetchall())
        cursor.execute(_INDEX_CATALOG_SQL)
        indexes = list(cursor.fetchall())
        cursor.execute(_FUNCTION_CATALOG_SQL)
        functions = list(cursor.fetchall())
        cursor.execute(_TRIGGER_CATALOG_SQL)
        triggers = list(cursor.fetchall())
        cursor.execute(_IDENTITY_SEQUENCE_CATALOG_SQL)
        identity_sequences = list(cursor.fetchall())
    return {
        "schema": schema,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "functions": functions,
        "triggers": triggers,
        "identity_sequences": identity_sequences,
    }


def _json_clean(value: object) -> object:
    if isinstance(value, list):
        return [_json_clean(item) for item in sorted(value, key=_sort_key)]
    return value


def _sort_key(value: object) -> tuple[int, str]:
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, str(value))
    if isinstance(value, (int, float)):
        return (1, str(value))
    return (2, str(value))


def _catalog_fingerprint(conn: psycopg.Connection[Any]) -> str:
    """Return a SHA-256 fingerprint of the normalized knowledge catalog.

    Each section is reduced to a deterministically sorted list of JSON rows so
    OIDs, physical relfilenodes, autogenerated identity-sequence names and
    timestamps never affect the digest.
    """
    catalog = _fetch_catalog(conn)
    normalized: dict[str, list[str]] = {}
    for section, rows in catalog.items():
        serialized: list[str] = []
        for row in rows:
            cleaned = {key: _json_clean(value) for key, value in row.items()}
            serialized.append(json.dumps(cleaned, sort_keys=True, separators=(",", ":")))
        normalized[section] = sorted(serialized)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# --- Exact object-set and ownership assertions -------------------------------


def _names(conn: psycopg.Connection[Any], sql: str) -> frozenset[str]:
    return frozenset(row[0] for row in _rows(conn, sql))


def _assert_exact_object_set(conn: psycopg.Connection[Any]) -> None:
    tables = _names(
        conn, "SELECT tablename FROM pg_tables WHERE schemaname = 'knowledge'"
    )
    assert tables == _EXPECTED_TABLES, f"unexpected tables: {tables ^ _EXPECTED_TABLES}"

    functions = _names(
        conn,
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'knowledge'",
    )
    assert functions == _EXPECTED_FUNCTIONS, f"unexpected functions: {functions}"

    triggers = _names(
        conn,
        "SELECT t.tgname FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'knowledge' AND NOT t.tgisinternal",
    )
    assert triggers == _EXPECTED_TRIGGERS, f"unexpected triggers: {triggers}"

    constraint_names = _names(
        conn,
        "SELECT con.conname FROM pg_constraint con "
        "JOIN pg_namespace n ON n.oid = con.connamespace "
        "WHERE n.nspname = 'knowledge' AND con.contype IN ('p', 'u', 'c', 'f')",
    )
    for name in constraint_names:
        assert _CONSTRAINT_NAME_PATTERN.match(name), f"constraint name breaks grammar: {name}"
    for table in _EXPECTED_TABLES:
        assert f"pk_{table}" in constraint_names, f"missing primary key for {table}"

    index_names = _names(
        conn, "SELECT indexname FROM pg_indexes WHERE schemaname = 'knowledge'"
    )
    for name in index_names:
        assert _INDEX_NAME_PATTERN.match(name), f"index name breaks grammar: {name}"
    assert index_names >= _EXPECTED_INDEXES, "missing documented query indexes"

    circular_fk = _rows(
        conn,
        "SELECT condeferrable, condeferred FROM pg_constraint "
        "WHERE conname = 'fk_sources__current_version'",
    )
    assert circular_fk == [(True, False)], (
        "circular current-version pointer must be DEFERRABLE INITIALLY IMMEDIATE"
    )

    identity_sequences = _rows(conn, _IDENTITY_SEQUENCE_CATALOG_SQL)
    assert identity_sequences == [("sync_events", "event_sequence")], (
        f"unexpected identity-owned sequences: {identity_sequences}"
    )


def _assert_ownership_grants_and_data_minimization(conn: psycopg.Connection[Any]) -> None:
    # Owners: schema, every table and every function owned by knowledge_app.
    assert _scalar(
        conn,
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'knowledge'",
    ) == _APPLICATION_USER
    table_owners = {
        row[0] for row in _rows(
            conn, "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'knowledge'"
        )
    }
    assert table_owners == {_APPLICATION_USER}, f"unexpected table owners: {table_owners}"
    function_owners = {
        row[0] for row in _rows(
            conn,
            "SELECT DISTINCT pg_get_userbyid(proowner) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'knowledge'",
        )
    }
    assert function_owners == {_APPLICATION_USER}, f"unexpected function owners: {function_owners}"

    # PUBLIC lacks schema CREATE/USAGE: the authoritative object-access gate.
    public_schema_acl = _scalar(
        conn,
        "SELECT count(*) FROM aclexplode("
        "(SELECT nspacl FROM pg_namespace WHERE nspname = 'knowledge')) "
        "WHERE grantee = 0",
    )
    assert public_schema_acl == 0, "PUBLIC must hold no privilege on the knowledge schema"

    # PUBLIC holds no table-level privilege on application tables.
    public_table_privileges = _rows(
        conn,
        "SELECT table_name, privilege_type FROM information_schema.table_privileges "
        "WHERE table_schema = 'knowledge' AND grantee = 'PUBLIC'",
    )
    assert not public_table_privileges, (
        f"PUBLIC holds table privileges: {public_table_privileges}"
    )

    # PUBLIC holds no routine/function privilege on application objects. The
    # migration revokes the default EXECUTE grant the engine adds at function
    # creation, so the spec requirement ("PUBLIC receives no object privileges
    # on knowledge") is genuinely enforced, not merely tolerated.
    public_routine_privileges = {
        (row[0], row[1])
        for row in _rows(
            conn,
            "SELECT routine_name, privilege_type FROM information_schema.routine_privileges "
            "WHERE routine_schema = 'knowledge' AND grantee = 'PUBLIC'",
        )
    }
    assert not public_routine_privileges, (
        f"PUBLIC holds routine privileges on knowledge: {public_routine_privileges}"
    )

    # Alembic bookkeeping lives in public; the application schema never owns it.
    assert _relation_exists(conn, "public.alembic_version"), "public.alembic_version must exist"
    assert not _relation_exists(conn, "knowledge.alembic_version"), (
        "knowledge.alembic_version must not exist"
    )

    # Data minimization: no column name matches the forbidden corpus.
    forbidden_columns = sorted(
        row[0]
        for row in _rows(
            conn,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'knowledge'",
        )
        if _FORBIDDEN_COLUMN_PATTERN.search(row[0])
    )
    assert not forbidden_columns, f"forbidden corpus column names: {forbidden_columns}"

    # Data minimization: no baseline column uses JSON or JSONB.
    json_columns = _rows(
        conn,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'knowledge' AND data_type IN ('json', 'jsonb')",
    )
    assert not json_columns, f"baseline uses JSON/JSONB columns: {json_columns}"


# --- Valid canonical graph insert --------------------------------------------


_INSERT_USER_SQL = (
    "INSERT INTO knowledge.users (user_id, username, display_name) "
    "VALUES (%s, 'owner', 'Owner')"
)
_INSERT_WORKSPACE_SQL = (
    "INSERT INTO knowledge.workspaces "
    "(workspace_id, owner_user_id, workspace_key, display_name) "
    "VALUES (%s, %s, 'primary', 'Primary')"
)
_INSERT_DEVICE_SQL = (
    "INSERT INTO knowledge.devices "
    "(device_id, workspace_id, user_id, device_name, device_kind) "
    "VALUES (%s, %s, %s, 'Obsidian 1', 'obsidian')"
)
_INSERT_CONTENT_OBJECT_SQL = (
    "INSERT INTO knowledge.content_objects "
    "(content_object_id, content_hash, object_key, byte_size, media_type, verified_at) "
    "VALUES (%s, %s, %s, 42, 'text/markdown', CURRENT_TIMESTAMP - interval '1 second')"
)
_INSERT_SOURCE_PENDING_SQL = (
    "INSERT INTO knowledge.sources "
    "(source_id, workspace_id, source_type, title) "
    "VALUES (%s, %s, 'markdown', 'Canonical baseline note')"
)
_INSERT_SOURCE_VERSION_SQL = (
    "INSERT INTO knowledge.source_versions "
    "(source_version_id, workspace_id, source_id, content_object_id, content_version, "
    "author_kind, author_id) "
    "VALUES (%s, %s, %s, %s, 1, 'user', %s)"
)
_ACTIVATE_SOURCE_SQL = (
    "UPDATE knowledge.sources "
    "SET sync_state = 'active', current_version_id = %s "
    "WHERE source_id = %s"
)
_INSERT_SYNC_EVENT_SQL = (
    "INSERT INTO knowledge.sync_events "
    "(event_id, workspace_id, source_id, device_id, committed_version_id, "
    "idempotency_key, request_fingerprint, event_type) "
    "VALUES (%s, %s, %s, %s, %s, 'create-canonical-1', %s, 'create')"
)
_INSERT_QDRANT_INTENT_SQL = (
    "INSERT INTO knowledge.projection_intents "
    "(projection_intent_id, workspace_id, event_id, source_id, source_version_id, "
    "projection_kind, operation) "
    "VALUES (%s, %s, %s, %s, %s, 'qdrant', 'upsert')"
)
_INSERT_NEO4J_INTENT_SQL = (
    "INSERT INTO knowledge.projection_intents "
    "(projection_intent_id, workspace_id, event_id, source_id, source_version_id, "
    "projection_kind, operation) "
    "VALUES (%s, %s, %s, %s, %s, 'neo4j', 'upsert')"
)
_INSERT_AUDIT_EVENT_SQL = (
    "INSERT INTO knowledge.audit_events "
    "(audit_event_id, workspace_id, actor_kind, actor_id, action, target_kind, "
    "target_id, request_id, result) "
    "VALUES (%s, %s, 'user', %s, 'source.version.commit', 'source_version', %s, %s, 'succeeded')"
)


def _insert_valid_graph(conn: psycopg.Connection[Any]) -> None:
    """Insert the fixed valid graph across all nine tables in one transaction."""
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS knowledge.fk_sources__current_version DEFERRED")
        cursor.execute(_INSERT_USER_SQL, (_USER_ID,))
        cursor.execute(_INSERT_WORKSPACE_SQL, (_WORKSPACE_ID, _USER_ID))
        cursor.execute(_INSERT_DEVICE_SQL, (_DEVICE_ID, _WORKSPACE_ID, _USER_ID))
        cursor.execute(
            _INSERT_CONTENT_OBJECT_SQL,
            (_CONTENT_OBJECT_ID, _CONTENT_HASH, _CONTENT_OBJECT_KEY),
        )
        cursor.execute(_INSERT_SOURCE_PENDING_SQL, (_SOURCE_ID, _WORKSPACE_ID))
        cursor.execute(
            _INSERT_SOURCE_VERSION_SQL,
            (_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID, _USER_ID),
        )
        cursor.execute(_ACTIVATE_SOURCE_SQL, (_SOURCE_VERSION_ID, _SOURCE_ID))
        cursor.execute(
            _INSERT_SYNC_EVENT_SQL,
            (_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID, _SOURCE_VERSION_ID,
             _REQUEST_FINGERPRINT),
        )
        cursor.execute(
            _INSERT_QDRANT_INTENT_SQL,
            (_QDRANT_INTENT_ID, _WORKSPACE_ID, _EVENT_ID, _SOURCE_ID, _SOURCE_VERSION_ID),
        )
        cursor.execute(
            _INSERT_NEO4J_INTENT_SQL,
            (_NEO4J_INTENT_ID, _WORKSPACE_ID, _EVENT_ID, _SOURCE_ID, _SOURCE_VERSION_ID),
        )
        cursor.execute(
            _INSERT_AUDIT_EVENT_SQL,
            (_AUDIT_EVENT_ID, _WORKSPACE_ID, _USER_ID, _SOURCE_VERSION_ID, _REQUEST_ID),
        )


def _row_counts(conn: psycopg.Connection[Any]) -> list[int]:
    counts: list[int] = []
    with conn.cursor() as cursor:
        for table in _TABLES_IN_COUNT_ORDER:
            cursor.execute(f"SELECT count(*) FROM knowledge.{table}")
            counts.append(cursor.fetchone()[0])
    return counts


class _AcceptedAndRolledBack(Exception):
    """Control-flow signal that forces rollback of an accepted allowed-case tx."""


def _assert_accepted_then_rollback(
    conn: psycopg.Connection[Any], statements: Sequence[tuple[str, tuple[Any, ...]]]
) -> None:
    """Run statements in a transaction that is always rolled back.

    Proves the statements are accepted by PostgreSQL without persisting state,
    so the committed valid-graph row counts stay exact.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cursor:
                for sql, params in statements:
                    cursor.execute(sql, params)
            raise _AcceptedAndRolledBack
    except _AcceptedAndRolledBack:
        return


def _assert_allowed_behaviors(conn: psycopg.Connection[Any]) -> None:
    # A second source may begin at content_version=1 referencing the same object.
    _assert_accepted_then_rollback(
        conn,
        [
            (_INSERT_SOURCE_PENDING_SQL, (_ALT_SOURCE_ID, _WORKSPACE_ID)),
            (
                _INSERT_SOURCE_VERSION_SQL,
                (_ALT_SOURCE_VERSION_ID, _WORKSPACE_ID, _ALT_SOURCE_ID, _CONTENT_OBJECT_ID,
                 _USER_ID),
            ),
        ],
    )

    # Two versions of the same source may reference the same global content object.
    _assert_accepted_then_rollback(
        conn,
        [
            (
                "INSERT INTO knowledge.source_versions "
                "(source_version_id, workspace_id, source_id, content_object_id, "
                "content_version, parent_version_id, author_kind, author_id) "
                "VALUES (%s, %s, %s, %s, 2, %s, 'user', %s)",
                (_ALT_VERSION_TWO_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
                 _SOURCE_VERSION_ID, _USER_ID),
            ),
        ],
    )

    # A Web/system event may originate with a null device.
    _assert_accepted_then_rollback(
        conn,
        [
            (
                "INSERT INTO knowledge.sync_events "
                "(event_id, workspace_id, source_id, device_id, idempotency_key, "
                "request_fingerprint, event_type) "
                "VALUES (%s, %s, %s, NULL, 'web-update-1', %s, 'update')",
                (_WEB_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _REQUEST_FINGERPRINT),
            ),
        ],
    )

    # A delete projection intent may retain source_version_id for provenance.
    _assert_accepted_then_rollback(
        conn,
        [
            (
                "INSERT INTO knowledge.sync_events "
                "(event_id, workspace_id, source_id, device_id, idempotency_key, "
                "request_fingerprint, event_type) "
                "VALUES (%s, %s, %s, %s, 'delete-1', %s, 'delete')",
                (_DELETE_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID, _REQUEST_FINGERPRINT),
            ),
            (
                "INSERT INTO knowledge.projection_intents "
                "(projection_intent_id, workspace_id, event_id, source_id, source_version_id, "
                "projection_kind, operation) "
                "VALUES (%s, %s, %s, %s, %s, 'qdrant', 'delete')",
                (_DELETE_INTENT_ID, _WORKSPACE_ID, _DELETE_EVENT_ID, _SOURCE_ID,
                 _SOURCE_VERSION_ID),
            ),
        ],
    )

    # An audit event may carry a null target_id for workspace-wide actions.
    _assert_accepted_then_rollback(
        conn,
        [
            (
                "INSERT INTO knowledge.audit_events "
                "(audit_event_id, workspace_id, actor_kind, actor_id, action, target_kind, "
                "target_id, request_id, result) "
                "VALUES (%s, %s, 'user', %s, 'workspace.export', 'workspace', NULL, %s, "
                "'succeeded')",
                (_AUDIT_NULL_TARGET_ID, _WORKSPACE_ID, _USER_ID, _REQUEST_ID),
            ),
        ],
    )


# --- The lifecycle test ------------------------------------------------------


def test_canonical_postgresql_baseline_upgrade_catalog_and_valid_graph(
    baseline_stack: BaselineStack,
) -> None:
    conn = baseline_stack.connection
    alembic_env = baseline_stack.alembic_env

    # Step 1: before upgrade, the application schema must be absent.
    assert not _schema_exists(conn), "knowledge schema must not exist before upgrade"

    # Step 3: empty -> head, then confirm exactly one head.
    upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert upgrade.returncode == 0, _alembic_failure("upgrade head", upgrade)
    check_heads = run_alembic(["current", "--check-heads"], alembic_env)
    assert check_heads.returncode == 0, _alembic_failure("current --check-heads", check_heads)

    # Step 2 + 4: catalog fingerprint, exact object set and ownership/data-min.
    fingerprint_after_upgrade = _catalog_fingerprint(conn)
    _assert_exact_object_set(conn)
    _assert_ownership_grants_and_data_minimization(conn)

    # Step 3: valid canonical graph across all nine tables.
    _insert_valid_graph(conn)
    assert _row_counts(conn) == [1, 1, 1, 1, 1, 1, 1, 2, 1], "valid graph row counts differ"

    # Step 3: allowed-behavior cases (each accepted, then rolled back).
    _assert_allowed_behaviors(conn)
    assert _row_counts(conn) == [1, 1, 1, 1, 1, 1, 1, 2, 1], (
        "allowed cases must not persist rows"
    )

    # Step 4: catalog fingerprint stable across two reads (data is not catalog).
    assert _catalog_fingerprint(conn) == fingerprint_after_upgrade, (
        "catalog fingerprint must be stable across reads"
    )

    # Lifecycle: gated downgrade removes the schema, data unchanged is irrelevant
    # because every object is gone.
    downgrade = run_alembic(["-x", "allow_destructive=true", "downgrade", "base"], alembic_env)
    assert downgrade.returncode == 0, _alembic_failure("gated downgrade base", downgrade)
    assert not _schema_exists(conn), "knowledge schema must be absent after downgrade"

    # Acceptance #18: empty -> head -> base -> head yields the same fingerprint.
    re_upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert re_upgrade.returncode == 0, _alembic_failure("second upgrade head", re_upgrade)
    fingerprint_after_re_upgrade = _catalog_fingerprint(conn)
    assert fingerprint_after_re_upgrade == fingerprint_after_upgrade, (
        "catalog fingerprint must be identical across the upgrade/downgrade/re-upgrade cycle"
    )


def _alembic_failure(
    description: str, result: subprocess.CompletedProcess[str]
) -> str:
    """Build a leak-safe assertion message from captured Alembic output.

    Alembic output is leak-safe by design (no DSN/secret); including it here is
    for diagnosis only and never carries the password or connection URL.
    """
    return (
        f"alembic {description} failed with code {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
