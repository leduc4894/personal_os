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
import time
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


# --- Task 4: negative invariants (savepoint-isolated rejection evidence) -----
#
# Every baseline invariant is proven by executing ONE mutation inside its own
# savepoint, asserting the expected SQLSTATE (and, for the two approved trigger
# messages only, the fixed message), then rolling the savepoint back. The
# committed valid graph is therefore never mutated, so the row counts stay exact.

_UNIQUE_VIOLATION: str = "23505"
_CHECK_VIOLATION: str = "23514"
_FOREIGN_KEY_VIOLATION: str = "23503"
# PostgreSQL 18 now emits 23001 (restrict_violation) when a DELETE/UPDATE of a
# parent row is blocked by a RESTRICT foreign-key action, distinct from 23503
# (foreign_key_violation), which covers an INSERT/UPDATE of a child row that
# references a missing parent. Every baseline foreign key is ON DELETE RESTRICT,
# so every parent-delete rejection lands here.
_RESTRICT_VIOLATION: str = "23001"
_TRIGGER_PROTECTION: str = "55000"
_IMMUTABLE_MESSAGE: str = "immutable_row_update_rejected"
_AUDIT_MESSAGE: str = "audit_events_append_only"

# Distinct UUIDs for negative-case rows. Each case runs in a rolled-back
# savepoint, so a single id is reused safely across cases; setup ids are kept
# apart from the "bad" id only within the same savepoint.
_BAD_USER_ID = UUID("a0000000-0000-0000-0000-000000000001")
_BAD_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000002")
_BAD_SOURCE_ID = UUID("a0000000-0000-0000-0000-000000000003")
_BAD_SOURCE_VERSION_ID = UUID("a0000000-0000-0000-0000-000000000004")
_BAD_CONTENT_OBJECT_ID = UUID("a0000000-0000-0000-0000-000000000005")
_BAD_EVENT_ID = UUID("a0000000-0000-0000-0000-000000000006")
_BAD_INTENT_ID = UUID("a0000000-0000-0000-0000-000000000007")
_BAD_AUDIT_EVENT_ID = UUID("a0000000-0000-0000-0000-000000000008")
_BAD_DEVICE_ID = UUID("a0000000-0000-0000-0000-000000000009")
_SETUP_USER_ID = UUID("a0000000-0000-0000-0000-000000000010")
_SETUP_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000011")
_SETUP_SOURCE_ID = UUID("a0000000-0000-0000-0000-000000000012")
_SETUP_SOURCE_VERSION_ID = UUID("a0000000-0000-0000-0000-000000000013")
_GEN_SEQUENCE_EVENT_ID = UUID("a0000000-0000-0000-0000-000000000014")
_RANDOM_UUID = UUID("a0000000-0000-0000-0000-000000000015")
_VALID_AUDIT_USER_ID = UUID("a0000000-0000-0000-0000-000000000016")
_VALID_AUDIT_DEVICE_ID = UUID("a0000000-0000-0000-0000-000000000017")
_VALID_AUDIT_SYSTEM_ID = UUID("a0000000-0000-0000-0000-000000000018")
_VALID_AUDIT_WORKFLOW_ID = UUID("a0000000-0000-0000-0000-000000000019")
_SETUP_EVENT_ID = UUID("a0000000-0000-0000-0000-00000000001a")

_NEG_CONTENT_HASH: str = hashlib.sha256(b"task4-negative-canonical-bytes").hexdigest()
_NEG_CONTENT_OBJECT_KEY: str = (
    f"objects/sha256/{_NEG_CONTENT_HASH[:2]}/{_NEG_CONTENT_HASH[2:4]}/{_NEG_CONTENT_HASH}"
)
_UPPER_HEX_HASH: str = "A" + "0" * 63
_UPPER_HEX_KEY: str = (
    f"objects/sha256/{_UPPER_HEX_HASH[:2]}/{_UPPER_HEX_HASH[2:4]}/{_UPPER_HEX_HASH}"
)
_SHORT_HASH: str = "abc123"
_SHORT_KEY: str = f"objects/sha256/{_SHORT_HASH[:2]}/{_SHORT_HASH[2:4]}/{_SHORT_HASH}"
_MISMATCH_OBJECT_KEY: str = f"objects/sha256/zz/zz/{_NEG_CONTENT_HASH}"

_INSERT_SETUP_USER_SQL = (
    "INSERT INTO knowledge.users (user_id, username, display_name) "
    "VALUES (%s, %s, 'Setup User')"
)
_INSERT_SETUP_WORKSPACE_SQL = (
    "INSERT INTO knowledge.workspaces "
    "(workspace_id, owner_user_id, workspace_key, display_name) "
    "VALUES (%s, %s, %s, 'Setup Workspace')"
)
_INSERT_SETUP_SOURCE_PENDING_SQL = (
    "INSERT INTO knowledge.sources (source_id, workspace_id, source_type, title) "
    "VALUES (%s, %s, 'markdown', 'Setup source')"
)
_INSERT_SETUP_SOURCE_VERSION_SQL = (
    "INSERT INTO knowledge.source_versions "
    "(source_version_id, workspace_id, source_id, content_object_id, content_version, "
    "author_kind, author_id) VALUES (%s, %s, %s, %s, 1, 'user', %s)"
)
# A fresh event under the committed source, used as the projection-intent anchor
# for intent negative cases. The committed graph already owns both a qdrant and
# a neo4j intent for the main event, so any new intent must anchor on a different
# event to avoid colliding on (workspace_id, event_id, projection_kind) before the
# targeted check can fire.
_INSERT_SETUP_EVENT_SQL = (
    "INSERT INTO knowledge.sync_events "
    "(event_id, workspace_id, source_id, device_id, idempotency_key, "
    "request_fingerprint, event_type) "
    "VALUES (%s, %s, %s, %s, 'setup-event', %s, 'create')"
)
_SETUP_EVENT_SETUP: tuple[str, tuple[Any, ...]] = (
    _INSERT_SETUP_EVENT_SQL,
    (_SETUP_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID, _REQUEST_FINGERPRINT),
)


class _MutationAcceptedButShouldReject(Exception):
    """Control-flow sentinel forcing savepoint rollback for an accepted mutation."""


def _assert_rejected(
    conn: psycopg.Connection[Any],
    sql: str,
    params: Sequence[Any] = (),
    *,
    expected_sqlstate: str,
    expected_message: str | None = None,
    setup: Sequence[tuple[str, Sequence[Any]]] = (),
) -> None:
    """Execute one mutation inside an isolated savepoint and assert rejection.

    ``setup`` statements create supporting rows in the same savepoint before the
    mutation. The savepoint rolls back on every path, so the committed valid
    graph never changes. Only the two approved trigger messages may be asserted
    via ``expected_message``; no other vendor text is ever matched.
    """
    caught: psycopg.Error | None = None
    try:
        with conn.transaction():
            with conn.cursor() as cursor:
                for setup_sql, setup_params in setup:
                    cursor.execute(setup_sql, tuple(setup_params))
                cursor.execute(sql, tuple(params))
            raise _MutationAcceptedButShouldReject
    except _MutationAcceptedButShouldReject:
        pytest.fail(f"expected SQLSTATE {expected_sqlstate} but the mutation was accepted")
    except psycopg.Error as err:
        caught = err
    assert caught is not None, "psycopg.Error was not raised by the mutation"
    assert caught.sqlstate == expected_sqlstate, (
        f"expected SQLSTATE {expected_sqlstate}, got {caught.sqlstate}"
    )
    if expected_message is not None:
        assert expected_message in str(caught), (
            f"expected trigger message {expected_message!r} not in error: {str(caught)!r}"
        )


def _audit_insert_sql(
    *,
    actor_kind: str = "user",
    actor_id: UUID | None = _USER_ID,
    actor_reference: str | None = None,
    action: str = "source.version.commit",
    target_kind: str = "source_version",
    result: str = "succeeded",
    reason_code: str | None = None,
    trace_id: str | None = None,
    safe_diff_hash: str | None = None,
    audit_event_id: UUID = _BAD_AUDIT_EVENT_ID,
) -> tuple[str, tuple[Any, ...]]:
    """Build a parameterized audit_events INSERT from the given field overrides."""
    columns: list[str] = [
        "audit_event_id", "workspace_id", "actor_kind", "actor_id",
        "actor_reference", "action", "target_kind", "target_id",
        "request_id", "result",
    ]
    values: list[Any] = [
        audit_event_id, _WORKSPACE_ID, actor_kind, actor_id, actor_reference,
        action, target_kind, _SOURCE_VERSION_ID, _REQUEST_ID, result,
    ]
    for column_name, value in (
        ("reason_code", reason_code),
        ("trace_id", trace_id),
        ("safe_diff_hash", safe_diff_hash),
    ):
        if value is not None:
            columns.append(column_name)
            values.append(value)
    placeholders = ", ".join(["%s"] * len(values))
    statement = (
        f"INSERT INTO knowledge.audit_events ({', '.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    return statement, tuple(values)


def _intent_insert_sql(
    *,
    projection_kind: str = "qdrant",
    operation: str = "upsert",
    status: str = "pending",
    attempt_count: int = 0,
    source_version_id: UUID | None = _SOURCE_VERSION_ID,
    source_id: UUID = _SOURCE_ID,
    event_id: UUID = _EVENT_ID,
    projection_intent_id: UUID = _BAD_INTENT_ID,
    lease_token: UUID | None = None,
    last_error_code: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Build a parameterized projection_intents INSERT from field overrides."""
    columns: list[str] = [
        "projection_intent_id", "workspace_id", "event_id", "source_id",
        "source_version_id", "projection_kind", "operation", "status",
        "attempt_count",
    ]
    values: list[Any] = [
        projection_intent_id, _WORKSPACE_ID, event_id, source_id,
        source_version_id, projection_kind, operation, status, attempt_count,
    ]
    for column_name, value in (
        ("lease_token", lease_token),
        ("last_error_code", last_error_code),
    ):
        if value is not None:
            columns.append(column_name)
            values.append(value)
    placeholders = ", ".join(["%s"] * len(values))
    statement = (
        f"INSERT INTO knowledge.projection_intents ({', '.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    return statement, tuple(values)


def _assert_intent_rejected(
    conn: psycopg.Connection[Any],
    *,
    expected_sqlstate: str,
    expected_message: str | None = None,
    extra_setup: Sequence[tuple[str, Sequence[Any]]] = (),
    **overrides: Any,
) -> None:
    """Assert a projection-intent mutation is rejected.

    The intent is anchored on the fresh ``_SETUP_EVENT_ID`` (created in the same
    savepoint), so it never collides with the committed qdrant/neo4j intents on
    ``_EVENT_ID``. ``extra_setup`` adds further supporting rows when needed.
    """
    sql, params = _intent_insert_sql(event_id=_SETUP_EVENT_ID, **overrides)
    setup: list[tuple[str, Sequence[Any]]] = [_SETUP_EVENT_SETUP, *extra_setup]
    _assert_rejected(
        conn,
        sql,
        params,
        expected_sqlstate=expected_sqlstate,
        expected_message=expected_message,
        setup=setup,
    )


def _assert_identity_and_ownership_invariants(conn: psycopg.Connection[Any]) -> None:
    # Duplicate username / workspace owner / workspace key.
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.users (user_id, username, display_name) "
        "VALUES (%s, 'owner', 'Dup')",
        (_BAD_USER_ID,),
        expected_sqlstate=_UNIQUE_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.workspaces "
        "(workspace_id, owner_user_id, workspace_key, display_name) "
        "VALUES (%s, %s, 'dup-owner-key', 'Dup')",
        (_BAD_WORKSPACE_ID, _USER_ID),
        expected_sqlstate=_UNIQUE_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.workspaces "
        "(workspace_id, owner_user_id, workspace_key, display_name) "
        "VALUES (%s, %s, 'primary', 'Dup')",
        (_BAD_WORKSPACE_ID, _BAD_USER_ID),
        expected_sqlstate=_UNIQUE_VIOLATION,
        setup=[
            (
                "INSERT INTO knowledge.users (user_id, username, display_name) "
                "VALUES (%s, 'second-owner', 'Second')",
                (_BAD_USER_ID,),
            ),
        ],
    )

    # Device whose (workspace_id, user_id) does not identify the workspace owner.
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.devices "
        "(device_id, workspace_id, user_id, device_name, device_kind) "
        "VALUES (%s, %s, %s, 'Rogue', 'obsidian')",
        (_BAD_DEVICE_ID, _WORKSPACE_ID, _BAD_USER_ID),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
    )

    # Invalid device kind / status / last-seen / revocation combinations.
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.devices "
        "(device_id, workspace_id, user_id, device_name, device_kind) "
        "VALUES (%s, %s, %s, 'Phone', 'mobile')",
        (_BAD_DEVICE_ID, _WORKSPACE_ID, _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.devices "
        "(device_id, workspace_id, user_id, device_name, device_kind, status) "
        "VALUES (%s, %s, %s, 'Phone', 'obsidian', 'paused')",
        (_BAD_DEVICE_ID, _WORKSPACE_ID, _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.devices "
        "(device_id, workspace_id, user_id, device_name, device_kind, last_seen_at) "
        "VALUES (%s, %s, %s, 'Phone', 'obsidian', CURRENT_TIMESTAMP - interval '1 hour')",
        (_BAD_DEVICE_ID, _WORKSPACE_ID, _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.devices "
        "(device_id, workspace_id, user_id, device_name, device_kind, status) "
        "VALUES (%s, %s, %s, 'Phone', 'obsidian', 'revoked')",
        (_BAD_DEVICE_ID, _WORKSPACE_ID, _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.devices "
        "(device_id, workspace_id, user_id, device_name, device_kind, status, revoked_at) "
        "VALUES (%s, %s, %s, 'Phone', 'obsidian', 'active', CURRENT_TIMESTAMP)",
        (_BAD_DEVICE_ID, _WORKSPACE_ID, _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.devices "
        "(device_id, workspace_id, user_id, device_name, device_kind, status, revoked_at) "
        "VALUES (%s, %s, %s, 'Phone', 'obsidian', 'revoked', "
        "CURRENT_TIMESTAMP - interval '1 hour')",
        (_BAD_DEVICE_ID, _WORKSPACE_ID, _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Physical parent deletes blocked by lineage foreign keys (RESTRICT).
    _assert_rejected(
        conn,
        "DELETE FROM knowledge.users WHERE user_id = %s",
        (_USER_ID,),
        expected_sqlstate=_RESTRICT_VIOLATION,
    )
    _assert_rejected(
        conn,
        "DELETE FROM knowledge.workspaces WHERE workspace_id = %s",
        (_WORKSPACE_ID,),
        expected_sqlstate=_RESTRICT_VIOLATION,
    )


def _assert_content_and_source_version_invariants(conn: psycopg.Connection[Any]) -> None:
    content_insert = (
        "INSERT INTO knowledge.content_objects "
        "(content_object_id, content_hash, object_key, byte_size, media_type, verified_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)"
    )

    # Uppercase / short SHA-256 and a mismatched object key.
    _assert_rejected(
        conn,
        content_insert,
        (_BAD_CONTENT_OBJECT_ID, _UPPER_HEX_HASH, _UPPER_HEX_KEY, 42,
         "text/markdown"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        content_insert,
        (_BAD_CONTENT_OBJECT_ID, _SHORT_HASH, _SHORT_KEY, 42,
         "text/markdown"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        content_insert,
        (_BAD_CONTENT_OBJECT_ID, _NEG_CONTENT_HASH, _MISMATCH_OBJECT_KEY, 42,
         "text/markdown"),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Negative byte size and parameterized / uppercase / invalid media type.
    _assert_rejected(
        conn,
        content_insert,
        (_BAD_CONTENT_OBJECT_ID, _NEG_CONTENT_HASH, _NEG_CONTENT_OBJECT_KEY, -1,
         "text/markdown"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        content_insert,
        (_BAD_CONTENT_OBJECT_ID, _NEG_CONTENT_HASH, _NEG_CONTENT_OBJECT_KEY, 42,
         "text/markdown; charset=utf-8"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        content_insert,
        (_BAD_CONTENT_OBJECT_ID, _NEG_CONTENT_HASH, _NEG_CONTENT_OBJECT_KEY, 42,
         "Text/Markdown"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        content_insert,
        (_BAD_CONTENT_OBJECT_ID, _NEG_CONTENT_HASH, _NEG_CONTENT_OBJECT_KEY, 42,
         "notamimetype"),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Verification timestamp after creation.
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.content_objects "
        "(content_object_id, content_hash, object_key, byte_size, media_type, "
        "verified_at, created_at) "
        "VALUES (%s, %s, %s, 42, 'text/markdown', CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP - interval '1 hour')",
        (_BAD_CONTENT_OBJECT_ID, _NEG_CONTENT_HASH, _NEG_CONTENT_OBJECT_KEY),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Duplicate global content hash (and its derived object key).
    _assert_rejected(
        conn,
        content_insert,
        (_BAD_CONTENT_OBJECT_ID, _CONTENT_HASH, _CONTENT_OBJECT_KEY, 42,
         "text/markdown"),
        expected_sqlstate=_UNIQUE_VIOLATION,
    )

    # Invalid source type / state / deleted / current-pointer combinations.
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.sources (source_id, workspace_id, source_type, title) "
        "VALUES (%s, %s, 'docx', 'Bad type')",
        (_BAD_SOURCE_ID, _WORKSPACE_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.sources "
        "(source_id, workspace_id, source_type, title, sync_state) "
        "VALUES (%s, %s, 'markdown', 'Bad state', 'frozen')",
        (_BAD_SOURCE_ID, _WORKSPACE_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.sources "
        "(source_id, workspace_id, source_type, title, sync_state) "
        "VALUES (%s, %s, 'markdown', 'Bad', 'deleted')",
        (_BAD_SOURCE_ID, _WORKSPACE_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.sources "
        "(source_id, workspace_id, source_type, title, sync_state, deleted_at) "
        "VALUES (%s, %s, 'markdown', 'Bad', 'pending', CURRENT_TIMESTAMP)",
        (_BAD_SOURCE_ID, _WORKSPACE_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.sources "
        "(source_id, workspace_id, source_type, title, sync_state) "
        "VALUES (%s, %s, 'markdown', 'Bad', 'active')",
        (_BAD_SOURCE_ID, _WORKSPACE_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "UPDATE knowledge.sources SET current_version_id = %s WHERE source_id = %s",
        (_SETUP_SOURCE_VERSION_ID, _SETUP_SOURCE_ID),
        expected_sqlstate=_CHECK_VIOLATION,
        setup=[
            (_INSERT_SETUP_SOURCE_PENDING_SQL, (_SETUP_SOURCE_ID, _WORKSPACE_ID)),
            (
                _INSERT_SETUP_SOURCE_VERSION_SQL,
                (_SETUP_SOURCE_VERSION_ID, _WORKSPACE_ID, _SETUP_SOURCE_ID,
                 _CONTENT_OBJECT_ID, _USER_ID),
            ),
        ],
    )

    # Current pointer from another source / workspace.
    _assert_rejected(
        conn,
        "UPDATE knowledge.sources "
        "SET sync_state = 'active', current_version_id = %s WHERE source_id = %s",
        (_SOURCE_VERSION_ID, _SETUP_SOURCE_ID),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
        setup=[
            (_INSERT_SETUP_SOURCE_PENDING_SQL, (_SETUP_SOURCE_ID, _WORKSPACE_ID)),
            (
                _INSERT_SETUP_SOURCE_VERSION_SQL,
                (_SETUP_SOURCE_VERSION_ID, _WORKSPACE_ID, _SETUP_SOURCE_ID,
                 _CONTENT_OBJECT_ID, _USER_ID),
            ),
        ],
    )
    _assert_rejected(
        conn,
        "UPDATE knowledge.sources "
        "SET sync_state = 'active', current_version_id = %s WHERE source_id = %s",
        (_SOURCE_VERSION_ID, _SETUP_SOURCE_ID),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
        setup=[
            (_INSERT_SETUP_USER_SQL, (_SETUP_USER_ID, "setup-user")),
            (
                _INSERT_SETUP_WORKSPACE_SQL,
                (_SETUP_WORKSPACE_ID, _SETUP_USER_ID, "setup-workspace"),
            ),
            (_INSERT_SETUP_SOURCE_PENDING_SQL, (_SETUP_SOURCE_ID, _SETUP_WORKSPACE_ID)),
            (
                _INSERT_SETUP_SOURCE_VERSION_SQL,
                (_SETUP_SOURCE_VERSION_ID, _SETUP_WORKSPACE_ID, _SETUP_SOURCE_ID,
                 _CONTENT_OBJECT_ID, _SETUP_USER_ID),
            ),
        ],
    )

    source_version_insert = (
        "INSERT INTO knowledge.source_versions "
        "(source_version_id, workspace_id, source_id, content_object_id, content_version, "
        "parent_version_id, author_kind, author_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    )

    # Duplicate / nonpositive per-source content version.
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
         1, None, "user", _USER_ID),
        expected_sqlstate=_UNIQUE_VIOLATION,
    )
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
         0, None, "user", _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Parent version from another source / workspace and self-parent.
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
         2, _SETUP_SOURCE_VERSION_ID, "user", _USER_ID),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
        setup=[
            (_INSERT_SETUP_SOURCE_PENDING_SQL, (_SETUP_SOURCE_ID, _WORKSPACE_ID)),
            (
                _INSERT_SETUP_SOURCE_VERSION_SQL,
                (_SETUP_SOURCE_VERSION_ID, _WORKSPACE_ID, _SETUP_SOURCE_ID,
                 _CONTENT_OBJECT_ID, _USER_ID),
            ),
        ],
    )
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
         2, _SETUP_SOURCE_VERSION_ID, "user", _USER_ID),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
        setup=[
            (_INSERT_SETUP_USER_SQL, (_SETUP_USER_ID, "setup-user")),
            (
                _INSERT_SETUP_WORKSPACE_SQL,
                (_SETUP_WORKSPACE_ID, _SETUP_USER_ID, "setup-workspace"),
            ),
            (_INSERT_SETUP_SOURCE_PENDING_SQL, (_SETUP_SOURCE_ID, _SETUP_WORKSPACE_ID)),
            (
                _INSERT_SETUP_SOURCE_VERSION_SQL,
                (_SETUP_SOURCE_VERSION_ID, _SETUP_WORKSPACE_ID, _SETUP_SOURCE_ID,
                 _CONTENT_OBJECT_ID, _SETUP_USER_ID),
            ),
        ],
    )
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
         2, _BAD_SOURCE_VERSION_ID, "user", _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Nonexistent content object (content objects are global CAS metadata).
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _RANDOM_UUID,
         2, None, "user", _USER_ID),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
    )

    # Invalid author-kind / author-id combinations.
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
         2, None, "admin", _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
         2, None, "system", _USER_ID),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        source_version_insert,
        (_BAD_SOURCE_VERSION_ID, _WORKSPACE_ID, _SOURCE_ID, _CONTENT_OBJECT_ID,
         2, None, "user", None),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Immutability: UPDATE of content_objects and source_versions.
    _assert_rejected(
        conn,
        "UPDATE knowledge.content_objects SET byte_size = byte_size "
        "WHERE content_object_id = %s",
        (_CONTENT_OBJECT_ID,),
        expected_sqlstate=_TRIGGER_PROTECTION,
        expected_message=_IMMUTABLE_MESSAGE,
    )
    _assert_rejected(
        conn,
        "UPDATE knowledge.source_versions SET content_version = content_version "
        "WHERE source_version_id = %s",
        (_SOURCE_VERSION_ID,),
        expected_sqlstate=_TRIGGER_PROTECTION,
        expected_message=_IMMUTABLE_MESSAGE,
    )

    # Referenced content object / source version / source cannot be deleted.
    _assert_rejected(
        conn,
        "DELETE FROM knowledge.content_objects WHERE content_object_id = %s",
        (_CONTENT_OBJECT_ID,),
        expected_sqlstate=_RESTRICT_VIOLATION,
    )
    _assert_rejected(
        conn,
        "DELETE FROM knowledge.source_versions WHERE source_version_id = %s",
        (_SOURCE_VERSION_ID,),
        expected_sqlstate=_RESTRICT_VIOLATION,
    )
    _assert_rejected(
        conn,
        "DELETE FROM knowledge.sources WHERE source_id = %s",
        (_SOURCE_ID,),
        expected_sqlstate=_RESTRICT_VIOLATION,
    )


def _assert_event_and_intent_invariants(conn: psycopg.Connection[Any]) -> None:
    event_insert = (
        "INSERT INTO knowledge.sync_events "
        "(event_id, workspace_id, source_id, device_id, idempotency_key, "
        "request_fingerprint, event_type) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )

    # Duplicate event ID.
    _assert_rejected(
        conn,
        event_insert,
        (_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID, "dup-event-id",
         _REQUEST_FINGERPRINT, "create"),
        expected_sqlstate=_UNIQUE_VIOLATION,
    )

    # Generated event sequence: an insert WITHOUT event_sequence succeeds and
    # yields a server-generated positive bigint (then rolls back).
    generated_sequence: object = None
    try:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    event_insert,
                    (_GEN_SEQUENCE_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID,
                     "generated-sequence", _REQUEST_FINGERPRINT, "create"),
                )
                cursor.execute(
                    "SELECT event_sequence FROM knowledge.sync_events "
                    "WHERE event_id = %s",
                    (_GEN_SEQUENCE_EVENT_ID,),
                )
                row = cursor.fetchone()
            assert row is not None, "generated event row must exist"
            generated_sequence = row[0]
            assert isinstance(generated_sequence, int) and generated_sequence > 0, (
                f"event_sequence must be a generated positive bigint, got {generated_sequence}"
            )
            raise _AcceptedAndRolledBack
    except _AcceptedAndRolledBack:
        pass

    # Duplicate workspace idempotency key.
    _assert_rejected(
        conn,
        event_insert,
        (_BAD_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID, "create-canonical-1",
         _REQUEST_FINGERPRINT, "create"),
        expected_sqlstate=_UNIQUE_VIOLATION,
    )

    # Event device / committed version / base version from another workspace/source.
    _assert_rejected(
        conn,
        event_insert,
        (_BAD_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _BAD_DEVICE_ID, "bad-device",
         _REQUEST_FINGERPRINT, "create"),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.sync_events "
        "(event_id, workspace_id, source_id, device_id, committed_version_id, "
        "idempotency_key, request_fingerprint, event_type) "
        "VALUES (%s, %s, %s, %s, %s, 'bad-committed', %s, 'create')",
        (_BAD_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID,
         _SETUP_SOURCE_VERSION_ID, _REQUEST_FINGERPRINT),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
        setup=[
            (_INSERT_SETUP_SOURCE_PENDING_SQL, (_SETUP_SOURCE_ID, _WORKSPACE_ID)),
            (
                _INSERT_SETUP_SOURCE_VERSION_SQL,
                (_SETUP_SOURCE_VERSION_ID, _WORKSPACE_ID, _SETUP_SOURCE_ID,
                 _CONTENT_OBJECT_ID, _USER_ID),
            ),
        ],
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.sync_events "
        "(event_id, workspace_id, source_id, device_id, base_version_id, "
        "idempotency_key, request_fingerprint, event_type) "
        "VALUES (%s, %s, %s, %s, %s, 'bad-base', %s, 'create')",
        (_BAD_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID,
         _SETUP_SOURCE_VERSION_ID, _REQUEST_FINGERPRINT),
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
        setup=[
            (_INSERT_SETUP_SOURCE_PENDING_SQL, (_SETUP_SOURCE_ID, _WORKSPACE_ID)),
            (
                _INSERT_SETUP_SOURCE_VERSION_SQL,
                (_SETUP_SOURCE_VERSION_ID, _WORKSPACE_ID, _SETUP_SOURCE_ID,
                 _CONTENT_OBJECT_ID, _USER_ID),
            ),
        ],
    )

    # Malformed idempotency key / request fingerprint / event type.
    _assert_rejected(
        conn,
        event_insert,
        (_BAD_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID, "has space",
         _REQUEST_FINGERPRINT, "create"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        event_insert,
        (_BAD_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID, "bad-fingerprint",
         "ABC123", "create"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        event_insert,
        (_BAD_EVENT_ID, _WORKSPACE_ID, _SOURCE_ID, _DEVICE_ID, "bad-type",
         _REQUEST_FINGERPRINT, "purge"),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Immutability: UPDATE of sync_events.
    _assert_rejected(
        conn,
        "UPDATE knowledge.sync_events SET event_type = event_type WHERE event_id = %s",
        (_EVENT_ID,),
        expected_sqlstate=_TRIGGER_PROTECTION,
        expected_message=_IMMUTABLE_MESSAGE,
    )

    # Duplicate (workspace_id, event_id, projection_kind): collides with the
    # committed qdrant intent on the main event.
    _assert_rejected(
        conn,
        *_intent_insert_sql(),
        expected_sqlstate=_UNIQUE_VIOLATION,
    )

    # Intent whose event belongs to another source. The fresh setup event lives
    # under the committed source, but the intent claims the second source.
    _assert_intent_rejected(
        conn,
        source_id=_SETUP_SOURCE_ID,
        source_version_id=None,
        operation="delete",
        expected_sqlstate=_FOREIGN_KEY_VIOLATION,
        extra_setup=[
            (_INSERT_SETUP_SOURCE_PENDING_SQL, (_SETUP_SOURCE_ID, _WORKSPACE_ID)),
        ],
    )

    # Invalid projection kind / operation / status.
    _assert_intent_rejected(
        conn, projection_kind="weaviate", expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_intent_rejected(
        conn, operation="merge", expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_intent_rejected(
        conn, status="running", expected_sqlstate=_CHECK_VIOLATION,
    )

    # Negative attempt count and timestamp order.
    _assert_intent_rejected(
        conn, attempt_count=-1, expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.projection_intents "
        "(projection_intent_id, workspace_id, event_id, source_id, source_version_id, "
        "projection_kind, operation, status, available_at) "
        "VALUES (%s, %s, %s, %s, %s, 'qdrant', 'upsert', 'pending', "
        "CURRENT_TIMESTAMP - interval '1 hour')",
        (_BAD_INTENT_ID, _WORKSPACE_ID, _SETUP_EVENT_ID, _SOURCE_ID, _SOURCE_VERSION_ID),
        expected_sqlstate=_CHECK_VIOLATION,
        setup=[_SETUP_EVENT_SETUP],
    )

    # Upsert without version.
    _assert_intent_rejected(
        conn, source_version_id=None, expected_sqlstate=_CHECK_VIOLATION,
    )

    # Lease fields inconsistent with status.
    _assert_intent_rejected(
        conn, status="leased", expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_intent_rejected(
        conn, status="pending", lease_token=_BAD_DEVICE_ID,
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.projection_intents "
        "(projection_intent_id, workspace_id, event_id, source_id, source_version_id, "
        "projection_kind, operation, status, lease_token, leased_until) "
        "VALUES (%s, %s, %s, %s, %s, 'qdrant', 'upsert', 'leased', %s, CURRENT_TIMESTAMP)",
        (_BAD_INTENT_ID, _WORKSPACE_ID, _SETUP_EVENT_ID, _SOURCE_ID, _SOURCE_VERSION_ID,
         _BAD_DEVICE_ID),
        expected_sqlstate=_CHECK_VIOLATION,
        setup=[_SETUP_EVENT_SETUP],
    )

    # Invalid dispatched fields.
    _assert_intent_rejected(
        conn, status="dispatched", expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.projection_intents "
        "(projection_intent_id, workspace_id, event_id, source_id, source_version_id, "
        "projection_kind, operation, status, dispatched_at) "
        "VALUES (%s, %s, %s, %s, %s, 'qdrant', 'upsert', 'pending', CURRENT_TIMESTAMP)",
        (_BAD_INTENT_ID, _WORKSPACE_ID, _SETUP_EVENT_ID, _SOURCE_ID, _SOURCE_VERSION_ID),
        expected_sqlstate=_CHECK_VIOLATION,
        setup=[_SETUP_EVENT_SETUP],
    )

    # Terminal without error and unsafe error token.
    _assert_intent_rejected(
        conn, status="terminal", expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_intent_rejected(
        conn, status="terminal", last_error_code="Bad Code!",
        expected_sqlstate=_CHECK_VIOLATION,
    )


def _assert_audit_invariants(conn: psycopg.Connection[Any]) -> None:
    # Every valid actor shape is accepted (then rolled back).
    _assert_accepted_then_rollback(
        conn,
        [
            _audit_insert_sql(
                actor_kind="user", actor_id=_USER_ID, actor_reference=None,
                audit_event_id=_VALID_AUDIT_USER_ID,
            ),
            _audit_insert_sql(
                actor_kind="device", actor_id=_DEVICE_ID, actor_reference=None,
                audit_event_id=_VALID_AUDIT_DEVICE_ID,
            ),
            _audit_insert_sql(
                actor_kind="system", actor_id=None, actor_reference=None,
                audit_event_id=_VALID_AUDIT_SYSTEM_ID,
            ),
            _audit_insert_sql(
                actor_kind="workflow", actor_id=None, actor_reference="github-actions",
                audit_event_id=_VALID_AUDIT_WORKFLOW_ID,
            ),
        ],
    )

    # Invalid actor combinations.
    _assert_rejected(
        conn, *_audit_insert_sql(actor_kind="user", actor_id=None, actor_reference=None),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        *_audit_insert_sql(
            actor_kind="user", actor_id=_USER_ID, actor_reference="github-actions"
        ),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(actor_kind="device", actor_id=None, actor_reference=None),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(actor_kind="system", actor_id=_USER_ID, actor_reference=None),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        *_audit_insert_sql(actor_kind="system", actor_id=None, actor_reference="github-actions"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        *_audit_insert_sql(
            actor_kind="workflow", actor_id=_USER_ID, actor_reference="github-actions"
        ),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(actor_kind="workflow", actor_id=None, actor_reference=None),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(actor_kind="service", actor_id=None, actor_reference=None),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Unsafe / empty action, target, reference and reason tokens.
    _assert_rejected(
        conn, *_audit_insert_sql(action="Bad Action!"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(action=""),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(target_kind="Bad Target"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(target_kind=""),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn,
        *_audit_insert_sql(
            actor_kind="workflow", actor_id=None, actor_reference="Bad Ref"
        ),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(reason_code="Bad Reason"),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Zero / uppercase / short trace ID.
    _assert_rejected(
        conn, *_audit_insert_sql(trace_id="0" * 32),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(trace_id="A" + "0" * 31),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(trace_id="abc123"),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Invalid diff hash and result.
    _assert_rejected(
        conn, *_audit_insert_sql(safe_diff_hash="abc123"),
        expected_sqlstate=_CHECK_VIOLATION,
    )
    _assert_rejected(
        conn, *_audit_insert_sql(result="success"),
        expected_sqlstate=_CHECK_VIOLATION,
    )

    # Append-only: UPDATE and DELETE both rejected by the trigger.
    _assert_rejected(
        conn,
        "UPDATE knowledge.audit_events SET result = result WHERE audit_event_id = %s",
        (_AUDIT_EVENT_ID,),
        expected_sqlstate=_TRIGGER_PROTECTION,
        expected_message=_AUDIT_MESSAGE,
    )
    _assert_rejected(
        conn,
        "DELETE FROM knowledge.audit_events WHERE audit_event_id = %s",
        (_AUDIT_EVENT_ID,),
        expected_sqlstate=_TRIGGER_PROTECTION,
        expected_message=_AUDIT_MESSAGE,
    )


def _assert_negative_invariants(conn: psycopg.Connection[Any]) -> None:
    """Run every baseline negative case inside one transaction.

    Each ``_assert_rejected`` call opens its own nested savepoint, so the four
    groups are fully isolated from one another and from the committed valid
    graph. The outer transaction commits nothing.
    """
    with conn.transaction():
        _assert_identity_and_ownership_invariants(conn)
        _assert_content_and_source_version_invariants(conn)
        _assert_event_and_intent_invariants(conn)
        _assert_audit_invariants(conn)


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

    # Task 4: every baseline invariant is database-enforced. Each mutation runs
    # in its own savepoint and is rolled back, so the committed valid graph is
    # never mutated.
    _assert_negative_invariants(conn)
    assert _row_counts(conn) == [1, 1, 1, 1, 1, 1, 1, 2, 1], (
        "negative cases must not persist rows"
    )
    # The current-version pointer is restored after the lineage/pointer cases.
    with conn.cursor() as _pointer_cursor:
        _pointer_cursor.execute(
            "SELECT current_version_id FROM knowledge.sources WHERE source_id = %s",
            (_SOURCE_ID,),
        )
        _restored_pointer = _pointer_cursor.fetchone()[0]
    assert _restored_pointer == _SOURCE_VERSION_ID, (
        "source current-version pointer must remain intact after negative cases"
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


# ---------------------------------------------------------------------------
# Task 5: recovery behavior
#
# Each test owns a disposable ``baseline_stack`` fixture instance and proves one
# recovery guarantee: destructive-gate refusal, no-op upgrade, bounded advisory
# lock, two-process first-upgrade race, late-failure rollback through the
# sanctioned in-process seam, restrictive downgrade + dependent-view rollback,
# and interruption atomicity.
# ---------------------------------------------------------------------------


def _current_revision(conn: psycopg.Connection[Any]) -> str | None:
    """Return the current alembic_version row, or None when absent/empty."""
    if not _relation_exists(conn, "public.alembic_version"):
        return None
    return _scalar(conn, "SELECT version_num FROM public.alembic_version LIMIT 1")


def _knowledge_table_count(conn: psycopg.Connection[Any]) -> int:
    return int(
        _scalar(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'knowledge'")
    )


def _baseline_revision_applied(conn: psycopg.Connection[Any]) -> bool:
    if not _relation_exists(conn, "public.alembic_version"):
        return False
    count = _scalar(
        conn,
        "SELECT count(*) FROM public.alembic_version "
        f"WHERE version_num = '{_BASELINE_REVISION}'",
    )
    return bool(count)


def _venv_alembic_command() -> list[str]:
    """Return the venv-local alembic executable path.

    Used for the concurrency test where ``uv run`` would serialize two
    subprocesses on its project lock; the venv's own alembic entry point has no
    such lock so both subprocesses truly race on the advisory lock.
    """
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / ("alembic.exe" if sys.platform == "win32" else "alembic")
    if not candidate.exists():
        candidate = bin_dir / "alembic"
    return [str(candidate)]


def _assert_captured_output_is_leak_safe(
    captured: str, *, password: str, sentinel: str | None = None
) -> None:
    """Assert no password, URL, driver text, SQLSTATE or sentinel leaks."""
    assert password not in captured, "captured output leaked the application password"
    assert "://" not in captured, "captured output leaked a URL scheme"
    lowered = captured.lower()
    assert "psycopg" not in lowered, "captured output leaked raw driver text"
    assert "sqlstate" not in lowered, "captured output leaked a SQLSTATE token"
    assert password not in lowered
    if sentinel is not None:
        assert sentinel not in captured, "captured output leaked the test sentinel"


def test_upgrade_head_is_a_noop_when_already_at_head(
    baseline_stack: BaselineStack,
) -> None:
    """Step 1: ``upgrade head`` at head is a no-op; row counts/fingerprint unchanged."""
    conn = baseline_stack.connection
    alembic_env = baseline_stack.alembic_env

    upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert upgrade.returncode == 0, _alembic_failure("first upgrade head", upgrade)
    _insert_valid_graph(conn)

    fingerprint_before = _catalog_fingerprint(conn)
    counts_before = _row_counts(conn)
    assert _current_revision(conn) == _BASELINE_REVISION

    noop = run_alembic(["upgrade", "head"], alembic_env)
    assert noop.returncode == 0, _alembic_failure("no-op upgrade head", noop)

    assert _current_revision(conn) == _BASELINE_REVISION
    assert _row_counts(conn) == counts_before, "no-op upgrade changed row counts"
    assert _catalog_fingerprint(conn) == fingerprint_before, (
        "no-op upgrade changed the catalog fingerprint"
    )


def test_downgrade_without_destructive_authorization_is_refused(
    baseline_stack: BaselineStack,
) -> None:
    """Step 1: ``downgrade base`` without ``-x`` is refused before any DDL, no leak."""
    conn = baseline_stack.connection
    alembic_env = baseline_stack.alembic_env
    password = _read_application_password()

    upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert upgrade.returncode == 0, _alembic_failure("upgrade head", upgrade)
    _insert_valid_graph(conn)

    fingerprint_before = _catalog_fingerprint(conn)
    counts_before = _row_counts(conn)
    assert _current_revision(conn) == _BASELINE_REVISION

    refusal = run_alembic(["downgrade", "base"], alembic_env)
    assert refusal.returncode != 0, "unauthorized downgrade must not return zero"

    captured = refusal.stdout + refusal.stderr
    assert "database_destructive_downgrade_refused" in captured, (
        "captured output must carry the registered refusal code"
    )
    assert "Destructive database downgrade is not authorized" in captured, (
        "captured output must carry the registered refusal safe message"
    )
    _assert_captured_output_is_leak_safe(captured, password=password)

    assert _current_revision(conn) == _BASELINE_REVISION
    assert _row_counts(conn) == counts_before, "refused downgrade changed row counts"
    assert _catalog_fingerprint(conn) == fingerprint_before


def test_concurrent_upgrade_blocks_on_advisory_lock_then_succeeds(
    baseline_stack: BaselineStack,
) -> None:
    """Step 2: a held advisory lock bounds the upgrade to a safe busy result."""
    conn = baseline_stack.connection
    alembic_env = baseline_stack.alembic_env
    password = _read_application_password()
    port = baseline_stack.port

    assert not _schema_exists(conn), "must start at base"

    holder = psycopg.connect(
        host=_DATABASE_HOST,
        port=port,
        user=_APPLICATION_USER,
        password=password,
        dbname=_APPLICATION_DATABASE,
        sslmode=_SSL_MODE,
        application_name=f"{_ALEMBIC_APPLICATION_NAME}-lock-holder",
    )
    try:
        with holder.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('knowledge-schema-migration', 0))"
            )
        start = time.monotonic()
        blocked = run_alembic(["upgrade", "head"], alembic_env)
        elapsed = time.monotonic() - start

        assert blocked.returncode != 0, (
            "upgrade must not succeed while the advisory lock is held"
        )
        assert elapsed >= 4.0, f"upgrade gave up after {elapsed:.1f}s (< 4s lower bound)"
        assert elapsed <= 15.0, f"upgrade hung for {elapsed:.1f}s (> 15s upper bound)"

        captured = blocked.stdout + blocked.stderr
        assert "database_migration_busy" in captured, (
            f"expected database_migration_busy in output: {captured!r}"
        )
        _assert_captured_output_is_leak_safe(captured, password=password)

        # No object was created or dropped while blocked.
        assert not _schema_exists(conn), "blocked upgrade must not create any object"
    finally:
        with suppress(psycopg.Error):
            holder.rollback()
            holder.close()

    # After the holder releases, the same command succeeds.
    succeeding = run_alembic(["upgrade", "head"], alembic_env)
    assert succeeding.returncode == 0, _alembic_failure("upgrade after lock release", succeeding)
    assert _schema_exists(conn)
    _assert_exact_object_set(conn)


def test_two_first_upgrade_processes_leave_exactly_one_head(
    baseline_stack: BaselineStack,
) -> None:
    """Step 2: two concurrent first-upgrade processes leave one head, no duplicates."""
    conn = baseline_stack.connection
    alembic_env = baseline_stack.alembic_env

    assert not _schema_exists(conn), "must start at base"

    command_base = [*_venv_alembic_command(), "upgrade", "head"]
    processes = [
        subprocess.Popen(
            command_base,
            cwd=str(_WORKTREE_ROOT),
            env=dict(alembic_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for proc in processes:
            proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
        raise

    success_count = sum(1 for proc in processes if proc.returncode == 0)
    assert success_count >= 1, (
        "at least one first-upgrade must succeed; codes="
        f"{tuple(proc.returncode for proc in processes)}"
    )

    # Final state: exactly one head, exact catalog, no duplicate objects.
    assert _current_revision(conn) == _BASELINE_REVISION
    assert _knowledge_table_count(conn) == 9, (
        "two first-upgrade attempts must leave exactly nine tables, no duplicates"
    )
    _assert_exact_object_set(conn)


def test_late_upgrade_failure_rolls_back_the_whole_schema(
    baseline_stack: BaselineStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3: a late failure via the sanctioned seam rolls back the whole schema."""
    conn = baseline_stack.connection
    alembic_env = baseline_stack.alembic_env

    assert not _schema_exists(conn), "must start at base"

    # The Python API reads KNOWLEDGE_* from os.environ inside the test process.
    for key in list(os.environ):
        if key.startswith("KNOWLEDGE_"):
            monkeypatch.delenv(key)
    for key, value in alembic_env.items():
        monkeypatch.setenv(key, value)

    from alembic import command
    from alembic.config import Config
    from alembic.util import CommandError

    sentinel = "LATE_FAILURE_SENTINEL_a8f3b2c1"

    def failing_before_verify_hook() -> None:
        raise RuntimeError(sentinel)

    failing_config = Config(str(_WORKTREE_ROOT / "alembic.ini"))
    failing_config.attributes["canonical_baseline_before_verify"] = failing_before_verify_hook

    with pytest.raises(CommandError) as exc_info:
        command.upgrade(failing_config, "head")

    rendered = f"{exc_info.value}"
    assert sentinel not in rendered, "mapped error leaked the injected exception sentinel"
    assert "database_schema_contract_invalid" in rendered, (
        "late failure must map to the safe schema-contract code"
    )

    # Transaction rolled back completely: schema absent, no applied revision.
    assert not _schema_exists(conn), "knowledge schema must be absent after rollback"
    assert not _baseline_revision_applied(conn), (
        "alembic_version must carry no applied revision after rollback"
    )

    # Remove the hook: normal upgrade succeeds.
    clean_config = Config(str(_WORKTREE_ROOT / "alembic.ini"))
    command.upgrade(clean_config, "head")

    assert _schema_exists(conn)
    assert _baseline_revision_applied(conn)
    _assert_exact_object_set(conn)


def test_restrictive_downgrade_re_upgrade_and_dependent_view_rollback(
    baseline_stack: BaselineStack,
) -> None:
    """Step 4: RESTRICT-only downgrade, fingerprint A==B, dependent-view rollback."""
    conn = baseline_stack.connection
    alembic_env = baseline_stack.alembic_env

    # 1-4: fingerprint A, gated downgrade (fully absent), re-upgrade B == A.
    upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert upgrade.returncode == 0, _alembic_failure("first upgrade head", upgrade)
    fingerprint_a = _catalog_fingerprint(conn)
    _assert_exact_object_set(conn)

    downgrade = run_alembic(["-x", "allow_destructive=true", "downgrade", "base"], alembic_env)
    assert downgrade.returncode == 0, _alembic_failure("first gated downgrade", downgrade)
    assert not _schema_exists(conn), "knowledge schema must be absent after gated downgrade"
    assert _knowledge_table_count(conn) == 0

    re_upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert re_upgrade.returncode == 0, _alembic_failure("re-upgrade head", re_upgrade)
    fingerprint_b = _catalog_fingerprint(conn)
    assert fingerprint_b == fingerprint_a, (
        "re-upgrade fingerprint must match the first upgrade fingerprint (A == B)"
    )

    # 5: create an unexpected dependent view on an application table.
    with conn.cursor() as cursor:
        cursor.execute(
            "CREATE VIEW knowledge.ci_dependent_audit_view AS "
            "SELECT workspace_id, action FROM knowledge.audit_events"
        )
    assert _relation_exists(conn, "knowledge.ci_dependent_audit_view")

    # 6: gated downgrade MUST fail (RESTRICT) and roll back completely.
    blocked_downgrade = run_alembic(
        ["-x", "allow_destructive=true", "downgrade", "base"], alembic_env
    )
    assert blocked_downgrade.returncode != 0, (
        "gated downgrade must fail when a dependent object exists"
    )
    # Exact head + view intact: the entire downgrade transaction rolled back.
    assert _knowledge_table_count(conn) == 9, (
        "blocked downgrade must leave all nine tables in place"
    )
    _assert_exact_object_set(conn)
    assert _relation_exists(conn, "knowledge.ci_dependent_audit_view"), (
        "the dependent view must survive the rolled-back downgrade"
    )

    # 7: explicitly drop only the test view.
    with conn.cursor() as cursor:
        cursor.execute("DROP VIEW knowledge.ci_dependent_audit_view")
    assert not _relation_exists(conn, "knowledge.ci_dependent_audit_view")

    # 8: gated downgrade now succeeds.
    clean_downgrade = run_alembic(
        ["-x", "allow_destructive=true", "downgrade", "base"], alembic_env
    )
    assert clean_downgrade.returncode == 0, _alembic_failure(
        "gated downgrade after dropping the view", clean_downgrade
    )
    assert not _schema_exists(conn)

    # 9: final re-upgrade: exact head + fingerprint.
    final_upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert final_upgrade.returncode == 0, _alembic_failure("final re-upgrade", final_upgrade)
    fingerprint_c = _catalog_fingerprint(conn)
    assert fingerprint_c == fingerprint_a, (
        "final re-upgrade fingerprint must match the original (C == A)"
    )
    _assert_exact_object_set(conn)


# --- Step 5: interruption atomicity ------------------------------------------
#
# Module-level slot for the ready event and the hook/target functions. spawn
# re-imports this module in the child; the target sets the slot before calling
# command.upgrade so the hook can fire the event from inside the open migration
# transaction, then block until the parent terminates the child.

_interruption_ready_event: Any = None


def _interruption_before_verify_hook() -> None:
    """Signal readiness then block so the parent can terminate mid-transaction."""
    event = _interruption_ready_event
    if event is not None:
        event.set()
    # Block indefinitely; the parent terminates us while the migration
    # transaction is still open so the server must either roll back (abandon) or
    # have already committed (race). Either outcome is acceptable; a subset is not.
    while True:
        time.sleep(0.2)


def _run_first_upgrade_under_interruption(
    ready_event: Any,
    worktree_root: str,
    alembic_env: Mapping[str, str],
) -> None:
    """Child target: run upgrade with a blocking before-verify hook."""
    global _interruption_ready_event
    _interruption_ready_event = ready_event

    # Replace KNOWLEDGE_* env in the spawned child with the sanitized fixture env.
    for key in list(os.environ):
        if key.startswith("KNOWLEDGE_"):
            os.environ.pop(key, None)
    os.environ.update(dict(alembic_env))

    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(worktree_root) / "alembic.ini"))
    config.attributes["canonical_baseline_before_verify"] = _interruption_before_verify_hook
    with suppress(BaseException):
        # The parent terminates us mid-transaction; any propagated exception
        # (including SystemExit from a forceful terminate) is expected.
        command.upgrade(config, "head")


def test_first_upgrade_interrupted_after_ddl_leaves_base_or_head(
    baseline_stack: BaselineStack,
) -> None:
    """Step 5: interrupt the client after DDL begins; the DB is base or head, never partial."""
    import multiprocessing

    conn = baseline_stack.connection
    alembic_env = baseline_stack.alembic_env

    assert not _schema_exists(conn), "must start at base for the interruption test"

    spawn_context = multiprocessing.get_context("spawn")
    ready_event: Any = spawn_context.Event()
    child: Any = spawn_context.Process(
        target=_run_first_upgrade_under_interruption,
        args=(ready_event, str(_WORKTREE_ROOT), dict(alembic_env)),
        daemon=True,
    )
    child.start()
    try:
        signaled = ready_event.wait(timeout=60)
        if not signaled:
            child.terminate()
            child.join(timeout=10)
            pytest.fail(
                "upgrade child did not reach the before-verify hook within 60s; "
                f"child alive={child.is_alive()} exitcode={child.exitcode}"
            )
        # The hook has fired: every CREATE TABLE/FUNCTION/TRIGGER has executed
        # inside the open migration transaction. Terminate the client now so the
        # server must either commit (race) or roll back (abandon).
        child.terminate()
    finally:
        if child.is_alive():
            child.terminate()
        child.join(timeout=30)

    assert not child.is_alive(), "interrupted upgrade child did not exit after terminate"

    # Give the PostgreSQL backend a beat to detect the dead socket and abort the
    # abandoned transaction.
    time.sleep(1.0)

    table_set = _names(
        conn, "SELECT tablename FROM pg_tables WHERE schemaname = 'knowledge'"
    )
    if table_set:
        # Head case: the server committed the migration transaction before the
        # terminate landed. The catalog must be the exact head, never a subset.
        assert table_set == _EXPECTED_TABLES, (
            f"interrupted upgrade exposed a partial baseline: {sorted(table_set)}"
        )
        assert _current_revision(conn) == _BASELINE_REVISION
    else:
        # Base case: the server aborted the transaction. No knowledge object
        # may remain; in particular, no subset of the nine tables.
        assert not _schema_exists(conn), (
            "interrupted upgrade left the knowledge schema with no tables"
        )
