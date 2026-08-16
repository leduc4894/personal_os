"""Migration lifecycle and reflection tests for the authentication schema.

Exercises the real Alembic revision chain against a disposable PostgreSQL 18.4
stack: empty-database upgrade, Phase 1 fixture upgrade, exact-head schema
reflection against the DML Core metadata, destructive-gated downgrade and the
database-enforced state/timestamp matrix checks. Every assertion observes the
live catalog; nothing is mocked.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import psycopg
import pytest
import sqlalchemy as sa

from postgresql_source_store.tables import SOURCE_STORE_TABLES

pytestmark = pytest.mark.local_stack

BASELINE_REVISION: str = "20260813_01"
AUTH_REVISION: str = "20260816_01"

BASELINE_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "workspaces",
        "devices",
        "content_objects",
        "sources",
        "source_versions",
        "sync_events",
        "projection_intents",
        "audit_events",
    }
)
AUTH_TABLES: frozenset[str] = frozenset(
    {
        "user_credentials",
        "web_sessions",
        "totp_credentials",
        "totp_recovery_codes",
        "device_token_families",
        "device_tokens",
        "device_authorization_grants",
        "authentication_throttle_buckets",
    }
)

#: pg_constraint contype codes: p = primary key, f = foreign key,
#: u = unique constraint, c = check constraint.
CONSTRAINT_MANIFEST: dict[str, dict[str, str]] = {
    "user_credentials": {
        "pk_user_credentials": "p",
        "fk_user_credentials__user": "f",
        "fk_user_credentials__workspace_owner": "f",
        "ck_user_credentials__password_hash": "c",
        "ck_user_credentials__credential_revision": "c",
        "ck_user_credentials__timestamps": "c",
    },
    "web_sessions": {
        "pk_web_sessions": "p",
        "fk_web_sessions__workspace_owner": "f",
        "uq_web_sessions__session_secret_hash": "u",
        "ck_web_sessions__session_secret_hash": "c",
        "ck_web_sessions__csrf_secret_hash": "c",
        "ck_web_sessions__state": "c",
        "ck_web_sessions__credential_revision": "c",
        "ck_web_sessions__authentication_method": "c",
        "ck_web_sessions__revocation_reason": "c",
        "ck_web_sessions__state_timestamps": "c",
        "ck_web_sessions__reauthentication": "c",
        "ck_web_sessions__expiry": "c",
    },
    "totp_credentials": {
        "pk_totp_credentials": "p",
        "fk_totp_credentials__workspace_owner": "f",
        "ck_totp_credentials__state": "c",
        "ck_totp_credentials__secret_ciphertext": "c",
        "ck_totp_credentials__secret_nonce": "c",
        "ck_totp_credentials__key_id": "c",
        "ck_totp_credentials__algorithm": "c",
        "ck_totp_credentials__digits": "c",
        "ck_totp_credentials__period_seconds": "c",
        "ck_totp_credentials__last_accepted_time_step": "c",
        "ck_totp_credentials__revision": "c",
        "ck_totp_credentials__state_timestamps": "c",
        "ck_totp_credentials__timestamps": "c",
    },
    "totp_recovery_codes": {
        "pk_totp_recovery_codes": "p",
        "fk_totp_recovery_codes__credential": "f",
        "fk_totp_recovery_codes__workspace_owner": "f",
        "uq_totp_recovery_codes__credential_revision_hash": "u",
        "ck_totp_recovery_codes__code_hash": "c",
        "ck_totp_recovery_codes__revision": "c",
        "ck_totp_recovery_codes__used_at": "c",
    },
    "device_token_families": {
        "pk_device_token_families": "p",
        "fk_device_token_families__workspace_owner": "f",
        "fk_device_token_families__device": "f",
        "ck_device_token_families__state": "c",
        "ck_device_token_families__current_refresh_generation": "c",
        "ck_device_token_families__timestamps": "c",
        "ck_device_token_families__expiry": "c",
        "ck_device_token_families__revocation": "c",
        "ck_device_token_families__revocation_reason": "c",
    },
    "device_tokens": {
        "pk_device_tokens": "p",
        "fk_device_tokens__family": "f",
        "fk_device_tokens__workspace_owner": "f",
        "fk_device_tokens__device": "f",
        "fk_device_tokens__predecessor": "f",
        "fk_device_tokens__successor": "f",
        "uq_device_tokens__secret_hash": "u",
        "ck_device_tokens__secret_hash": "c",
        "ck_device_tokens__token_kind": "c",
        "ck_device_tokens__generation": "c",
        "ck_device_tokens__state": "c",
        "ck_device_tokens__derivation_key_id": "c",
        "ck_device_tokens__rotation_lineage": "c",
        "ck_device_tokens__state_lineage": "c",
        "ck_device_tokens__timestamps": "c",
    },
    "device_authorization_grants": {
        "pk_device_authorization_grants": "p",
        "fk_device_authorization_grants__approved_by_user": "f",
        "fk_device_authorization_grants__approval_session": "f",
        "fk_device_authorization_grants__device": "f",
        "fk_device_authorization_grants__token_family": "f",
        "fk_device_authorization_grants__initial_access_token": "f",
        "fk_device_authorization_grants__initial_refresh_token": "f",
        "uq_device_authorization_grants__user_code_hash": "u",
        "uq_device_authorization_grants__polling_secret_hash": "u",
        "ck_device_authorization_grants__user_code_hash": "c",
        "ck_device_authorization_grants__polling_secret_hash": "c",
        "ck_device_authorization_grants__device_name": "c",
        "ck_device_authorization_grants__platform_class": "c",
        "ck_device_authorization_grants__platform_name": "c",
        "ck_device_authorization_grants__plugin_version": "c",
        "ck_device_authorization_grants__requested_scope": "c",
        "ck_device_authorization_grants__state": "c",
        "ck_device_authorization_grants__state_matrix": "c",
        "ck_device_authorization_grants__exchange_links": "c",
        "ck_device_authorization_grants__timestamps": "c",
    },
    "authentication_throttle_buckets": {
        "pk_authentication_throttle_buckets": "p",
        "uq_authentication_throttle_buckets__kind_hash": "u",
        "ck_authentication_throttle_buckets__bucket_kind": "c",
        "ck_authentication_throttle_buckets__bucket_hash": "c",
        "ck_authentication_throttle_buckets__failed_attempt_count": "c",
        "ck_authentication_throttle_buckets__timestamps": "c",
    },
}

#: pg_index facts per authentication table: name -> (is_unique, has_predicate).
#: Primary-key and inline-unique constraint indexes appear here too.
INDEX_MANIFEST: dict[str, dict[str, tuple[bool, bool]]] = {
    "user_credentials": {
        "pk_user_credentials": (True, False),
        "ix_user_credentials__workspace_user": (False, False),
    },
    "web_sessions": {
        "pk_web_sessions": (True, False),
        "uq_web_sessions__session_secret_hash": (True, False),
        "ix_web_sessions__workspace_user": (False, False),
        "ix_web_sessions__state_idle_expiry": (False, False),
    },
    "totp_credentials": {
        "pk_totp_credentials": (True, False),
        "uq_totp_credentials__active_user": (True, True),
        "uq_totp_credentials__pending_user": (True, True),
        "ix_totp_credentials__workspace_user": (False, False),
    },
    "totp_recovery_codes": {
        "pk_totp_recovery_codes": (True, False),
        "uq_totp_recovery_codes__credential_revision_hash": (True, False),
        "ix_totp_recovery_codes__workspace_user": (False, False),
        "ix_totp_recovery_codes__credential_revision": (False, False),
    },
    "device_token_families": {
        "pk_device_token_families": (True, False),
        "uq_device_token_families__active_device": (True, True),
        "ix_device_token_families__workspace_user": (False, False),
        "ix_device_token_families__workspace_device": (False, False),
    },
    "device_tokens": {
        "pk_device_tokens": (True, False),
        "uq_device_tokens__secret_hash": (True, False),
        "uq_device_tokens__current_refresh_generation": (True, True),
        "uq_device_tokens__successor_per_predecessor": (True, True),
        "ix_device_tokens__family_kind_generation": (False, False),
        "ix_device_tokens__workspace_user": (False, False),
        "ix_device_tokens__workspace_device": (False, False),
        "ix_device_tokens__successor": (False, False),
    },
    "device_authorization_grants": {
        "pk_device_authorization_grants": (True, False),
        "uq_device_authorization_grants__user_code_hash": (True, False),
        "uq_device_authorization_grants__polling_secret_hash": (True, False),
        "ix_device_authorization_grants__client_state_expiry": (False, False),
        "ix_device_authorization_grants__approved_by_user": (False, False),
        "ix_device_authorization_grants__approval_session": (False, False),
        "ix_device_authorization_grants__device": (False, False),
        "ix_device_authorization_grants__token_family": (False, False),
        "ix_device_authorization_grants__initial_access_token": (False, False),
        "ix_device_authorization_grants__initial_refresh_token": (False, False),
    },
    "authentication_throttle_buckets": {
        "pk_authentication_throttle_buckets": (True, False),
        "uq_authentication_throttle_buckets__kind_hash": (True, False),
        "ix_authentication_throttle_buckets__locked_until": (False, True),
    },
}


class ForeignKeyFacts(NamedTuple):
    """The reflected shape of one foreign key constraint."""

    table: str
    local_columns: tuple[str, ...]
    referent_table: str
    referent_columns: tuple[str, ...]


#: fk constraint name -> expected shape (local table, local columns,
#: referenced table, referenced columns).
EXPECTED_FOREIGN_KEYS: dict[str, ForeignKeyFacts] = {
    "fk_user_credentials__user": ForeignKeyFacts(
        "user_credentials", ("user_id",), "users", ("user_id",)
    ),
    "fk_user_credentials__workspace_owner": ForeignKeyFacts(
        "user_credentials",
        ("workspace_id", "user_id"),
        "workspaces",
        ("workspace_id", "owner_user_id"),
    ),
    "fk_web_sessions__workspace_owner": ForeignKeyFacts(
        "web_sessions", ("workspace_id", "user_id"), "workspaces", ("workspace_id", "owner_user_id")
    ),
    "fk_totp_credentials__workspace_owner": ForeignKeyFacts(
        "totp_credentials",
        ("workspace_id", "user_id"),
        "workspaces",
        ("workspace_id", "owner_user_id"),
    ),
    "fk_totp_recovery_codes__credential": ForeignKeyFacts(
        "totp_recovery_codes",
        ("totp_credential_id",),
        "totp_credentials",
        ("totp_credential_id",),
    ),
    "fk_totp_recovery_codes__workspace_owner": ForeignKeyFacts(
        "totp_recovery_codes",
        ("workspace_id", "user_id"),
        "workspaces",
        ("workspace_id", "owner_user_id"),
    ),
    "fk_device_token_families__workspace_owner": ForeignKeyFacts(
        "device_token_families",
        ("workspace_id", "user_id"),
        "workspaces",
        ("workspace_id", "owner_user_id"),
    ),
    "fk_device_token_families__device": ForeignKeyFacts(
        "device_token_families",
        ("workspace_id", "device_id"),
        "devices",
        ("workspace_id", "device_id"),
    ),
    "fk_device_tokens__family": ForeignKeyFacts(
        "device_tokens", ("token_family_id",), "device_token_families", ("token_family_id",)
    ),
    "fk_device_tokens__workspace_owner": ForeignKeyFacts(
        "device_tokens",
        ("workspace_id", "user_id"),
        "workspaces",
        ("workspace_id", "owner_user_id"),
    ),
    "fk_device_tokens__device": ForeignKeyFacts(
        "device_tokens", ("workspace_id", "device_id"), "devices", ("workspace_id", "device_id")
    ),
    "fk_device_tokens__predecessor": ForeignKeyFacts(
        "device_tokens", ("predecessor_token_id",), "device_tokens", ("device_token_id",)
    ),
    "fk_device_tokens__successor": ForeignKeyFacts(
        "device_tokens", ("successor_token_id",), "device_tokens", ("device_token_id",)
    ),
    "fk_device_authorization_grants__approved_by_user": ForeignKeyFacts(
        "device_authorization_grants", ("approved_by_user_id",), "users", ("user_id",)
    ),
    "fk_device_authorization_grants__approval_session": ForeignKeyFacts(
        "device_authorization_grants",
        ("approved_web_session_id",),
        "web_sessions",
        ("web_session_id",),
    ),
    "fk_device_authorization_grants__device": ForeignKeyFacts(
        "device_authorization_grants", ("device_id",), "devices", ("device_id",)
    ),
    "fk_device_authorization_grants__token_family": ForeignKeyFacts(
        "device_authorization_grants",
        ("token_family_id",),
        "device_token_families",
        ("token_family_id",),
    ),
    "fk_device_authorization_grants__initial_access_token": ForeignKeyFacts(
        "device_authorization_grants",
        ("initial_access_token_id",),
        "device_tokens",
        ("device_token_id",),
    ),
    "fk_device_authorization_grants__initial_refresh_token": ForeignKeyFacts(
        "device_authorization_grants",
        ("initial_refresh_token_id",),
        "device_tokens",
        ("device_token_id",),
    ),
}

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]

# --- Leak-safe Alembic subprocess runner -------------------------------------


def _alembic_failure(description: str, result: subprocess.CompletedProcess[str]) -> str:
    """Build a leak-safe assertion message from captured Alembic output.

    Alembic output is leak-safe by design (no DSN/secret); including it here is
    for diagnosis only and never carries the password or connection URL.
    """
    return f"{description} failed with code {result.returncode}: {result.stdout}{result.stderr}"


def run_alembic(
    arguments: Sequence[str], alembic_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *arguments],
        cwd=str(_WORKTREE_ROOT),
        env=alembic_env,
        capture_output=True,
        text=True,
        check=False,
    )


# --- Catalog inspection helpers ----------------------------------------------


def _rows(conn: psycopg.Connection[Any], sql: str, params: Sequence[Any] = ()) -> list[Any]:
    with conn.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return list(cursor.fetchall())


def _scalar(conn: psycopg.Connection[Any], sql: str, params: Sequence[Any] = ()) -> Any:
    with conn.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        row = cursor.fetchone()
    assert row is not None
    return row[0]


def _schema_exists(conn: psycopg.Connection[Any]) -> bool:
    return bool(
        _scalar(conn, "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'knowledge')")
    )


def _knowledge_tables(conn: psycopg.Connection[Any]) -> frozenset[str]:
    return frozenset(
        row[0]
        for row in _rows(conn, "SELECT tablename FROM pg_tables WHERE schemaname = 'knowledge'")
    )


def _current_revision(conn: psycopg.Connection[Any]) -> str | None:
    exists = _scalar(
        conn,
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'alembic_version')",
    )
    if not exists:
        return None
    return _scalar(conn, "SELECT version_num FROM public.alembic_version LIMIT 1")


def _expected_column_facts(table: sa.Table) -> dict[str, tuple[str, int | None, str]]:
    facts: dict[str, tuple[str, int | None, str]] = {}
    for column in table.columns:
        sqltype = column.type
        if isinstance(sqltype, sa.Uuid):
            data_type, char_length = "uuid", None
        elif isinstance(sqltype, sa.Text):
            data_type, char_length = "text", None
        elif isinstance(sqltype, sa.String):
            data_type, char_length = "character varying", sqltype.length
        elif isinstance(sqltype, sa.TIMESTAMP) and sqltype.timezone:
            data_type, char_length = "timestamp with time zone", None
        elif isinstance(sqltype, sa.BigInteger):
            data_type, char_length = "bigint", None
        elif isinstance(sqltype, sa.Integer):
            data_type, char_length = "integer", None
        else:  # pragma: no cover - the metadata carries only the types above
            raise AssertionError(f"unmapped column type for {table.name}.{column.name}")
        facts[column.name] = (data_type, char_length, "YES" if column.nullable else "NO")
    return facts


def _reflected_column_facts(conn: psycopg.Connection[Any], table_name: str) -> dict[str, Any]:
    return {
        row[0]: (row[1], row[2], row[3])
        for row in _rows(
            conn,
            "SELECT column_name, data_type, character_maximum_length, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'knowledge' AND table_name = %s",
            (table_name,),
        )
    }


def _reflected_constraints(conn: psycopg.Connection[Any], table_name: str) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in _rows(
            conn,
            "SELECT con.conname, con.contype::text "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "WHERE nsp.nspname = 'knowledge' AND rel.relname = %s "
            "AND con.contype IN ('p', 'u', 'c', 'f')",
            (table_name,),
        )
    }


def _reflected_indexes(conn: psycopg.Connection[Any], table_name: str) -> dict[str, Any]:
    return {
        row[0]: (row[1], row[2])
        for row in _rows(
            conn,
            "SELECT idxcls.relname, idx.indisunique, idx.indpred IS NOT NULL "
            "FROM pg_index idx "
            "JOIN pg_class idxcls ON idxcls.oid = idx.indexrelid "
            "JOIN pg_class tbl ON tbl.oid = idx.indrelid "
            "JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace "
            "WHERE nsp.nspname = 'knowledge' AND tbl.relname = %s",
            (table_name,),
        )
    }


def _reflected_foreign_keys(conn: psycopg.Connection[Any]) -> dict[str, ForeignKeyFacts]:
    rows = _rows(
        conn,
        "SELECT con.conname, "
        "  src.relname, "
        "  (SELECT string_agg(attr.attname, ',' ORDER BY key_columns.ord) "
        "     FROM unnest(con.conkey) WITH ORDINALITY AS key_columns(attnum, ord) "
        "     JOIN pg_attribute attr ON attr.attrelid = con.conrelid "
        "          AND attr.attnum = key_columns.attnum), "
        "  tgt.relname, "
        "  (SELECT string_agg(attr.attname, ',' ORDER BY key_columns.ord) "
        "     FROM unnest(con.confkey) WITH ORDINALITY AS key_columns(attnum, ord) "
        "     JOIN pg_attribute attr ON attr.attrelid = con.confrelid "
        "          AND attr.attnum = key_columns.attnum) "
        "FROM pg_constraint con "
        "JOIN pg_class src ON src.oid = con.conrelid "
        "JOIN pg_class tgt ON tgt.oid = con.confrelid "
        "JOIN pg_namespace nsp ON nsp.oid = src.relnamespace "
        "WHERE nsp.nspname = 'knowledge' AND con.contype = 'f' "
        "  AND src.relname = ANY(%s)",
        (sorted(AUTH_TABLES),),
    )
    return {
        row[0]: ForeignKeyFacts(
            table=row[1],
            local_columns=tuple(row[2].split(",")),
            referent_table=row[3],
            referent_columns=tuple(row[4].split(",")),
        )
        for row in rows
    }


def _reflected_authentication_catalog(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """Full normalized reflection fingerprint of the eight authentication tables."""
    fingerprint: dict[str, Any] = {"columns": {}, "constraints": {}, "indexes": {}}
    for table_name in sorted(AUTH_TABLES):
        fingerprint["columns"][table_name] = _reflected_column_facts(conn, table_name)
        fingerprint["constraints"][table_name] = _reflected_constraints(conn, table_name)
        fingerprint["indexes"][table_name] = _reflected_indexes(conn, table_name)
    fingerprint["foreign_keys"] = dict(sorted(_reflected_foreign_keys(conn).items()))
    return fingerprint


def _assert_authentication_catalog_matches_contracts(conn: psycopg.Connection[Any]) -> None:
    """Exact-head reflection: columns, constraints, indexes and foreign keys."""
    for table_name in sorted(AUTH_TABLES):
        metadata_table = SOURCE_STORE_TABLES[table_name]
        assert _reflected_column_facts(conn, table_name) == _expected_column_facts(
            metadata_table
        ), table_name
        assert _reflected_constraints(conn, table_name) == CONSTRAINT_MANIFEST[table_name], (
            table_name
        )
        assert _reflected_indexes(conn, table_name) == INDEX_MANIFEST[table_name], table_name
    assert _reflected_foreign_keys(conn) == EXPECTED_FOREIGN_KEYS


# --- Lifecycle tests ---------------------------------------------------------


def test_authentication_schema_phase1_stack_upgrade_reflection_and_gated_downgrade(
    authentication_schema_stack: Any,
) -> None:
    """Empty database -> Phase 1 head -> authentication head -> gated downgrade.

    Covers every migration gate from spec 15.9 for this child: the Phase 1
    fixture upgrade first, the exact-head reflection against the DML Core
    metadata, the destructive-gated downgrade refusal with an auth row present,
    the gated downgrade back to the Phase 1 head and a deterministic re-upgrade.
    """
    conn = authentication_schema_stack.connection
    alembic_env = authentication_schema_stack.alembic_env
    assert not _schema_exists(conn), "knowledge schema must not exist before upgrade"

    # Phase 1 fixture upgrade: the baseline applies alone first.
    phase1 = run_alembic(["upgrade", BASELINE_REVISION], alembic_env)
    assert phase1.returncode == 0, _alembic_failure("upgrade 20260813_01", phase1)
    assert _current_revision(conn) == BASELINE_REVISION
    assert _knowledge_tables(conn) == BASELINE_TABLES

    # The authentication upgrade stacks on the Phase 1 head.
    upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert upgrade.returncode == 0, _alembic_failure("upgrade head", upgrade)
    assert _current_revision(conn) == AUTH_REVISION
    assert _knowledge_tables(conn) == BASELINE_TABLES | AUTH_TABLES

    # Exact-head reflection against the DML metadata and manifests.
    _assert_authentication_catalog_matches_contracts(conn)
    fingerprint_after_upgrade = _reflected_authentication_catalog(conn)

    # Exact-head application smoke: one auth row commits at the new head.
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO knowledge.authentication_throttle_buckets "
            "(throttle_bucket_id, bucket_kind, bucket_hash, window_started_at, "
            " failed_attempt_count, locked_until, updated_at) "
            "VALUES (%s, 'login_username', %s, CURRENT_TIMESTAMP, 1, NULL, "
            "CURRENT_TIMESTAMP)",
            (uuid4(), "ab" * 32),
        )

    # Downgrade with an auth row present but WITHOUT the destructive gate is
    # refused before any DDL runs.
    refusal = run_alembic(["downgrade", BASELINE_REVISION], alembic_env)
    assert refusal.returncode != 0, "unauthorized downgrade must not return zero"
    captured = refusal.stdout + refusal.stderr
    assert "database_destructive_downgrade_refused" in captured
    assert authentication_schema_stack.password.get_secret_value() not in captured
    assert _current_revision(conn) == AUTH_REVISION
    assert _knowledge_tables(conn) == BASELINE_TABLES | AUTH_TABLES
    assert _scalar(conn, "SELECT count(*) FROM knowledge.authentication_throttle_buckets") == 1, (
        "refused downgrade must not touch committed auth rows"
    )

    # Gated downgrade drops exactly the eight authentication tables and leaves
    # the Phase 1 head fully intact.
    gated = run_alembic(
        ["-x", "allow_destructive=true", "downgrade", BASELINE_REVISION], alembic_env
    )
    assert gated.returncode == 0, _alembic_failure("gated downgrade to phase 1 head", gated)
    assert _current_revision(conn) == BASELINE_REVISION
    assert _knowledge_tables(conn) == BASELINE_TABLES
    assert _scalar(conn, "SELECT count(*) FROM knowledge.users") == 0, (
        "the baseline tables remain queryable at the phase 1 head"
    )

    # Deterministic downgrade/upgrade cycle: identical reflection.
    re_upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert re_upgrade.returncode == 0, _alembic_failure("re-upgrade head", re_upgrade)
    assert _current_revision(conn) == AUTH_REVISION
    assert _reflected_authentication_catalog(conn) == fingerprint_after_upgrade, (
        "the authentication catalog must be identical across the gated "
        "downgrade and re-upgrade cycle"
    )


def test_authentication_schema_upgrade_from_empty_database(
    authentication_schema_stack: Any,
) -> None:
    """An empty database reaches the authentication head in one upgrade run."""
    conn = authentication_schema_stack.connection
    alembic_env = authentication_schema_stack.alembic_env
    teardown = run_alembic(["-x", "allow_destructive=true", "downgrade", "base"], alembic_env)
    assert teardown.returncode == 0, _alembic_failure("gated downgrade base", teardown)
    assert not _schema_exists(conn), "downgrade base must remove the knowledge schema"

    upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert upgrade.returncode == 0, _alembic_failure("empty upgrade head", upgrade)
    assert _current_revision(conn) == AUTH_REVISION
    assert _knowledge_tables(conn) == BASELINE_TABLES | AUTH_TABLES
    for table_name in AUTH_TABLES:
        assert _reflected_column_facts(conn, table_name) == _expected_column_facts(
            SOURCE_STORE_TABLES[table_name]
        ), table_name


# --- Database-enforced matrix checks -----------------------------------------


class _MutationAcceptedButShouldReject(Exception):
    """Control-flow sentinel forcing savepoint rollback for an accepted mutation."""


def _assert_rejected(
    conn: psycopg.Connection[Any],
    sql: str,
    params: Sequence[Any] = (),
    *,
    expected_sqlstate: str,
    expected_constraint: str | None = None,
    setup: Sequence[tuple[str, Sequence[Any]]] = (),
) -> None:
    """Execute one mutation inside an isolated savepoint and assert rejection.

    ``setup`` statements create supporting rows in the same savepoint before the
    mutation. The savepoint rolls back on every path, so the committed graph
    never changes. Constraint names are asserted through the diagnostics
    attribute; no vendor message text is ever matched.
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
    if expected_constraint is not None:
        assert caught.diag.constraint_name == expected_constraint, (
            f"expected constraint {expected_constraint}, got {caught.diag.constraint_name}"
        )


_USER_ID: UUID = UUID("00000000-0000-0000-0000-000000000101")
_WORKSPACE_ID: UUID = UUID("00000000-0000-0000-0000-000000000102")
_DEVICE_ID: UUID = UUID("00000000-0000-0000-0000-000000000103")


def _now() -> datetime:
    return datetime.now(UTC)


_PHC_HASH: str = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$c2VjcmV0aGFzaDEyMzQ1Njc4OTA"


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _seed_baseline_graph(conn: psycopg.Connection[Any]) -> None:
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO knowledge.users (user_id, username, display_name) "
            "VALUES (%s, 'auth-schema-owner', 'Auth Schema Owner')",
            (_USER_ID,),
        )
        cursor.execute(
            "INSERT INTO knowledge.workspaces "
            "(workspace_id, owner_user_id, workspace_key, display_name) "
            "VALUES (%s, %s, 'auth-schema-ws', 'Auth Schema Workspace')",
            (_WORKSPACE_ID, _USER_ID),
        )
        cursor.execute(
            "INSERT INTO knowledge.devices "
            "(device_id, workspace_id, user_id, device_name, device_kind) "
            "VALUES (%s, %s, %s, 'Auth Schema Device', 'obsidian')",
            (_DEVICE_ID, _WORKSPACE_ID, _USER_ID),
        )


_INSERT_USER_CREDENTIALS_SQL = (
    "INSERT INTO knowledge.user_credentials "
    "(user_id, workspace_id, password_hash, credential_revision, "
    " totp_prompt_dismissed_at, password_changed_at) "
    "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)"
)

_INSERT_WEB_SESSION_SQL = (
    "INSERT INTO knowledge.web_sessions "
    "(web_session_id, user_id, workspace_id, session_secret_hash, csrf_secret_hash, "
    " state, credential_revision, authentication_method, authenticated_at, "
    " idle_expires_at, absolute_expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, "
    " CURRENT_TIMESTAMP + interval '12 hours', CURRENT_TIMESTAMP + interval '7 days')"
)


#: Sentinel distinguishing "fill a valid call-time timestamp" from an
#: explicit ``None`` (SQL NULL) in the row-parameter helpers.
_AUTO: Any = object()


def _web_session_params(
    *,
    session_id: UUID,
    secret_hash: str | None = None,
    state: str = "active",
    method: str = "password",
    authenticated_at: Any = _AUTO,
) -> tuple[Any, ...]:
    resolved_authenticated_at: datetime | None
    if authenticated_at is _AUTO:
        resolved_authenticated_at = None if state == "pending_totp" else _now()
    else:
        resolved_authenticated_at = authenticated_at
    return (
        session_id,
        _USER_ID,
        _WORKSPACE_ID,
        secret_hash or _hex64(f"session-{session_id}"),
        _hex64(f"csrf-{session_id}"),
        state,
        method,
        resolved_authenticated_at,
    )


_INSERT_TOTP_CREDENTIAL_SQL = (
    "INSERT INTO knowledge.totp_credentials "
    "(totp_credential_id, user_id, workspace_id, state, secret_ciphertext, "
    " secret_nonce, key_id, algorithm, digits, period_seconds, revision, "
    " created_at, enrollment_expires_at, activated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, 'authkey-1', 'SHA1', %s, %s, 1, "
    " CURRENT_TIMESTAMP - interval '1 minute', %s, %s)"
)


def _totp_params(
    *,
    credential_id: UUID,
    state: str = "active",
    digits: int = 6,
    period_seconds: int = 30,
    enrollment_expires_at: datetime | None = None,
    activated_at: Any = _AUTO,
) -> tuple[Any, ...]:
    resolved_activated_at: datetime | None
    if activated_at is _AUTO:
        resolved_activated_at = _now() if state == "active" else None
    else:
        resolved_activated_at = activated_at
    return (
        credential_id,
        _USER_ID,
        _WORKSPACE_ID,
        state,
        "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWQ",
        "c2FsdHNhbHQ",
        digits,
        period_seconds,
        enrollment_expires_at,
        resolved_activated_at,
    )


_INSERT_FAMILY_SQL = (
    "INSERT INTO knowledge.device_token_families "
    "(token_family_id, user_id, workspace_id, device_id, state, "
    " current_refresh_generation, inactivity_expires_at, absolute_expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, "
    " CURRENT_TIMESTAMP + interval '30 days', CURRENT_TIMESTAMP + interval '90 days')"
)


def _device_token_params(
    *,
    device_token_id: UUID,
    token_family_id: UUID,
    token_kind: str = "refresh",
    generation: int = 1,
    secret_hash: str | None = None,
    state: str = "active",
    predecessor_token_id: UUID | None = None,
    successor_token_id: UUID | None = None,
    rotation_id: UUID | None = None,
    rotated_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> tuple[Any, ...]:
    return (
        device_token_id,
        token_family_id,
        _USER_ID,
        _WORKSPACE_ID,
        _DEVICE_ID,
        token_kind,
        generation,
        secret_hash or _hex64(f"device-token-{device_token_id}"),
        state,
        predecessor_token_id,
        successor_token_id,
        rotation_id,
        rotated_at,
        revoked_at,
    )


_INSERT_RECOVERY_CODE_SQL = (
    "INSERT INTO knowledge.totp_recovery_codes "
    "(recovery_code_id, totp_credential_id, user_id, workspace_id, revision, "
    " code_hash, created_at, used_at) "
    "VALUES (%s, %s, %s, %s, 1, %s, CURRENT_TIMESTAMP - interval '1 minute', %s)"
)

_INSERT_DEVICE_TOKEN_SQL = (
    "INSERT INTO knowledge.device_tokens "
    "(device_token_id, token_family_id, user_id, workspace_id, device_id, "
    " token_kind, generation, secret_hash, state, predecessor_token_id, "
    " successor_token_id, rotation_id, derivation_key_id, issued_at, expires_at, "
    " rotated_at, revoked_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'authkey-1', "
    " CURRENT_TIMESTAMP - interval '1 minute', CURRENT_TIMESTAMP + interval '15 minutes', "
    " %s, %s)"
)

_INSERT_GRANT_SQL = (
    "INSERT INTO knowledge.device_authorization_grants "
    "(grant_id, user_code_hash, polling_secret_hash, client_instance_id, "
    " device_name, platform_class, platform_name, plugin_version, "
    " requested_scope, state, expires_at) "
    "VALUES (%s, %s, %s, %s, 'Auth Plugin Device', %s, 'windows', '1.2.3', "
    " 'obsidian_sync', %s, CURRENT_TIMESTAMP + interval '10 minutes')"
)

_INSERT_DENIED_EXCHANGED_GRANT_SQL = (
    "INSERT INTO knowledge.device_authorization_grants "
    "(grant_id, user_code_hash, polling_secret_hash, client_instance_id, "
    " device_name, platform_class, platform_name, plugin_version, "
    " requested_scope, state, expires_at, denied_at, exchanged_at) "
    "VALUES (%s, %s, %s, %s, 'Auth Plugin Device', 'obsidian_desktop', 'windows', "
    " '1.2.3', 'obsidian_sync', 'denied', CURRENT_TIMESTAMP + interval '10 minutes', "
    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)

_INSERT_PENDING_WITH_APPROVAL_GRANT_SQL = (
    "INSERT INTO knowledge.device_authorization_grants "
    "(grant_id, user_code_hash, polling_secret_hash, client_instance_id, "
    " device_name, platform_class, platform_name, plugin_version, "
    " requested_scope, state, expires_at, approved_at, approved_by_user_id, "
    " approved_web_session_id) "
    "VALUES (%s, %s, %s, %s, 'Auth Plugin Device', 'obsidian_desktop', 'windows', "
    " '1.2.3', 'obsidian_sync', 'pending', CURRENT_TIMESTAMP + interval '10 minutes', "
    " CURRENT_TIMESTAMP, %s, %s)"
)

_INSERT_EXCHANGED_UNLINKED_GRANT_SQL = (
    "INSERT INTO knowledge.device_authorization_grants "
    "(grant_id, user_code_hash, polling_secret_hash, client_instance_id, "
    " device_name, platform_class, platform_name, plugin_version, "
    " requested_scope, state, created_at, expires_at, approved_at, "
    " approved_by_user_id, approved_web_session_id, exchanged_at) "
    "VALUES (%s, %s, %s, %s, 'Auth Plugin Device', 'obsidian_desktop', 'windows', "
    " '1.2.3', 'obsidian_sync', 'exchanged', CURRENT_TIMESTAMP - interval '20 minutes', "
    " CURRENT_TIMESTAMP - interval '10 minutes', CURRENT_TIMESTAMP, %s, %s, "
    " CURRENT_TIMESTAMP)"
)


def test_authentication_matrix_checks_reject_inconsistent_rows(
    authentication_schema_stack: Any,
) -> None:
    """Every closed state/timestamp matrix rule rejects at the database boundary."""
    conn = authentication_schema_stack.connection
    alembic_env = authentication_schema_stack.alembic_env
    upgrade = run_alembic(["upgrade", "head"], alembic_env)
    assert upgrade.returncode == 0, _alembic_failure("upgrade head", upgrade)

    _seed_baseline_graph(conn)

    # -- user_credentials ----------------------------------------------------
    _assert_rejected(
        conn,
        _INSERT_USER_CREDENTIALS_SQL,
        (_USER_ID, _WORKSPACE_ID, "not-a-phc-hash", 1, None),
        expected_sqlstate="23514",
        expected_constraint="ck_user_credentials__password_hash",
    )
    _assert_rejected(
        conn,
        _INSERT_USER_CREDENTIALS_SQL,
        (_USER_ID, _WORKSPACE_ID, _PHC_HASH, 0, None),
        expected_sqlstate="23514",
        expected_constraint="ck_user_credentials__credential_revision",
    )

    # -- web_sessions ----------------------------------------------------------
    _assert_rejected(
        conn,
        _INSERT_WEB_SESSION_SQL,
        _web_session_params(session_id=uuid4(), state="revoked"),
        expected_sqlstate="23514",
        expected_constraint="ck_web_sessions__state_timestamps",
    )
    _assert_rejected(
        conn,
        _INSERT_WEB_SESSION_SQL,
        _web_session_params(session_id=uuid4(), authenticated_at=None),
        expected_sqlstate="23514",
        expected_constraint="ck_web_sessions__state_timestamps",
    )
    _assert_rejected(
        conn,
        _INSERT_WEB_SESSION_SQL.replace(
            "CURRENT_TIMESTAMP + interval '12 hours'", "CURRENT_TIMESTAMP + interval '8 days'"
        ),
        _web_session_params(session_id=uuid4()),
        expected_sqlstate="23514",
        expected_constraint="ck_web_sessions__expiry",
    )
    _assert_rejected(
        conn,
        _INSERT_WEB_SESSION_SQL,
        _web_session_params(session_id=uuid4(), state="expired"),
        expected_sqlstate="23514",
        expected_constraint="ck_web_sessions__state",
    )
    _assert_rejected(
        conn,
        _INSERT_WEB_SESSION_SQL,
        _web_session_params(session_id=uuid4(), method="magic_link"),
        expected_sqlstate="23514",
        expected_constraint="ck_web_sessions__authentication_method",
    )
    shared_session_secret = _hex64("shared-session-secret")
    _assert_rejected(
        conn,
        _INSERT_WEB_SESSION_SQL,
        _web_session_params(session_id=uuid4(), secret_hash=shared_session_secret),
        expected_sqlstate="23505",
        expected_constraint="uq_web_sessions__session_secret_hash",
        setup=(
            (
                _INSERT_WEB_SESSION_SQL,
                _web_session_params(session_id=uuid4(), secret_hash=shared_session_secret),
            ),
        ),
    )

    # -- totp_credentials --------------------------------------------------------
    _assert_rejected(
        conn,
        _INSERT_TOTP_CREDENTIAL_SQL,
        _totp_params(credential_id=uuid4(), state="pending", activated_at=None),
        expected_sqlstate="23514",
        expected_constraint="ck_totp_credentials__state_timestamps",
    )
    _assert_rejected(
        conn,
        _INSERT_TOTP_CREDENTIAL_SQL,
        _totp_params(credential_id=uuid4(), state="active", activated_at=None),
        expected_sqlstate="23514",
        expected_constraint="ck_totp_credentials__state_timestamps",
    )
    _assert_rejected(
        conn,
        _INSERT_TOTP_CREDENTIAL_SQL,
        _totp_params(credential_id=uuid4(), state="replaced", activated_at=None),
        expected_sqlstate="23514",
        expected_constraint="ck_totp_credentials__state_timestamps",
    )
    _assert_rejected(
        conn,
        _INSERT_TOTP_CREDENTIAL_SQL,
        _totp_params(credential_id=uuid4(), state="active"),
        expected_sqlstate="23505",
        expected_constraint="uq_totp_credentials__active_user",
        setup=((_INSERT_TOTP_CREDENTIAL_SQL, _totp_params(credential_id=uuid4(), state="active")),),
    )
    first_pending_params = _totp_params(
        credential_id=uuid4(),
        state="pending",
        enrollment_expires_at=_now() + timedelta(minutes=10),
        activated_at=None,
    )
    second_pending_params = _totp_params(
        credential_id=uuid4(),
        state="pending",
        enrollment_expires_at=_now() + timedelta(minutes=10),
        activated_at=None,
    )
    _assert_rejected(
        conn,
        _INSERT_TOTP_CREDENTIAL_SQL,
        second_pending_params,
        expected_sqlstate="23505",
        expected_constraint="uq_totp_credentials__pending_user",
        setup=((_INSERT_TOTP_CREDENTIAL_SQL, first_pending_params),),
    )
    _assert_rejected(
        conn,
        _INSERT_TOTP_CREDENTIAL_SQL,
        _totp_params(credential_id=uuid4(), digits=8),
        expected_sqlstate="23514",
        expected_constraint="ck_totp_credentials__digits",
    )
    _assert_rejected(
        conn,
        _INSERT_TOTP_CREDENTIAL_SQL,
        _totp_params(credential_id=uuid4(), period_seconds=60),
        expected_sqlstate="23514",
        expected_constraint="ck_totp_credentials__period_seconds",
    )

    # -- totp_recovery_codes -------------------------------------------------------
    active_credential_id = uuid4()
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            _INSERT_TOTP_CREDENTIAL_SQL,
            _totp_params(credential_id=active_credential_id, state="active"),
        )
    _assert_rejected(
        conn,
        _INSERT_RECOVERY_CODE_SQL,
        (
            uuid4(),
            active_credential_id,
            _USER_ID,
            _WORKSPACE_ID,
            _hex64("used-too-early"),
            _now() - timedelta(minutes=2),
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_totp_recovery_codes__used_at",
    )
    shared_code_hash = _hex64("shared-recovery-code")
    _assert_rejected(
        conn,
        _INSERT_RECOVERY_CODE_SQL,
        (uuid4(), active_credential_id, _USER_ID, _WORKSPACE_ID, shared_code_hash, None),
        expected_sqlstate="23505",
        expected_constraint="uq_totp_recovery_codes__credential_revision_hash",
        setup=(
            (
                _INSERT_RECOVERY_CODE_SQL,
                (uuid4(), active_credential_id, _USER_ID, _WORKSPACE_ID, shared_code_hash, None),
            ),
        ),
    )
    # used_at is immutable once set (spec 15.4): enforced by the trigger.
    consumed_code_id = uuid4()
    caught: psycopg.Error | None = None
    try:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    _INSERT_RECOVERY_CODE_SQL,
                    (
                        consumed_code_id,
                        active_credential_id,
                        _USER_ID,
                        _WORKSPACE_ID,
                        _hex64("consumed-code"),
                        _now(),
                    ),
                )
                cursor.execute(
                    "UPDATE knowledge.totp_recovery_codes "
                    "SET used_at = %s WHERE recovery_code_id = %s",
                    (_now() + timedelta(minutes=1), consumed_code_id),
                )
            raise _MutationAcceptedButShouldReject
    except _MutationAcceptedButShouldReject:
        pytest.fail("changing a consumed recovery code used_at must be rejected")
    except psycopg.Error as err:
        caught = err
    assert caught is not None
    assert caught.sqlstate == "55000"
    assert "recovery_code_used_at_immutable" in str(caught)

    # -- device_token_families ------------------------------------------------------
    _assert_rejected(
        conn,
        _INSERT_FAMILY_SQL,
        (uuid4(), _USER_ID, _WORKSPACE_ID, _DEVICE_ID, "revoked", 1),
        expected_sqlstate="23514",
        expected_constraint="ck_device_token_families__revocation",
    )
    _assert_rejected(
        conn,
        _INSERT_FAMILY_SQL.replace(
            "CURRENT_TIMESTAMP + interval '30 days'", "CURRENT_TIMESTAMP + interval '91 days'"
        ),
        (uuid4(), _USER_ID, _WORKSPACE_ID, _DEVICE_ID, "active", 1),
        expected_sqlstate="23514",
        expected_constraint="ck_device_token_families__expiry",
    )
    _assert_rejected(
        conn,
        _INSERT_FAMILY_SQL,
        (uuid4(), _USER_ID, _WORKSPACE_ID, _DEVICE_ID, "active", 1),
        expected_sqlstate="23505",
        expected_constraint="uq_device_token_families__active_device",
        setup=((_INSERT_FAMILY_SQL, (uuid4(), _USER_ID, _WORKSPACE_ID, _DEVICE_ID, "active", 1)),),
    )

    # -- device_tokens ----------------------------------------------------------------
    family_id = uuid4()
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            _INSERT_FAMILY_SQL,
            (family_id, _USER_ID, _WORKSPACE_ID, _DEVICE_ID, "active", 1),
        )
    _assert_rejected(
        conn,
        _INSERT_DEVICE_TOKEN_SQL,
        _device_token_params(
            device_token_id=uuid4(),
            token_family_id=family_id,
            state="rotated",
            rotation_id=uuid4(),
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_tokens__state_lineage",
    )
    _assert_rejected(
        conn,
        _INSERT_DEVICE_TOKEN_SQL,
        _device_token_params(device_token_id=uuid4(), token_family_id=family_id, state="revoked"),
        expected_sqlstate="23514",
        expected_constraint="ck_device_tokens__state_lineage",
    )
    # Rotated AND revoked at once is inconsistent under either transition.
    real_successor_id = uuid4()
    _assert_rejected(
        conn,
        _INSERT_DEVICE_TOKEN_SQL,
        _device_token_params(
            device_token_id=uuid4(),
            token_family_id=family_id,
            generation=2,
            state="revoked",
            successor_token_id=real_successor_id,
            rotated_at=_now(),
            revoked_at=_now(),
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_tokens__state_lineage",
        setup=(
            (
                _INSERT_DEVICE_TOKEN_SQL,
                _device_token_params(device_token_id=real_successor_id, token_family_id=family_id),
            ),
        ),
    )
    _assert_rejected(
        conn,
        _INSERT_DEVICE_TOKEN_SQL,
        _device_token_params(
            device_token_id=uuid4(), token_family_id=family_id, generation=2, state="active"
        ),
        expected_sqlstate="23505",
        expected_constraint="uq_device_tokens__current_refresh_generation",
        setup=(
            (
                _INSERT_DEVICE_TOKEN_SQL,
                _device_token_params(device_token_id=uuid4(), token_family_id=family_id),
            ),
        ),
    )
    predecessor_id = uuid4()
    first_successor_id = uuid4()
    spare_successor_id = uuid4()
    predecessor_rotation_id = uuid4()
    _assert_rejected(
        conn,
        _INSERT_DEVICE_TOKEN_SQL,
        _device_token_params(
            device_token_id=uuid4(),
            token_family_id=family_id,
            generation=3,
            state="rotated",
            predecessor_token_id=predecessor_id,
            successor_token_id=spare_successor_id,
            rotation_id=uuid4(),
            rotated_at=_now(),
        ),
        expected_sqlstate="23505",
        expected_constraint="uq_device_tokens__successor_per_predecessor",
        setup=(
            # The natural rotation order the partial uniques force: the
            # predecessor rotates out of 'active' first, then the successor
            # generation becomes the family's single active refresh.
            (
                _INSERT_DEVICE_TOKEN_SQL,
                _device_token_params(device_token_id=predecessor_id, token_family_id=family_id),
            ),
            (
                "UPDATE knowledge.device_tokens "
                "SET state = 'rotated', rotation_id = %s, rotated_at = %s "
                "WHERE device_token_id = %s",
                (predecessor_rotation_id, _now(), predecessor_id),
            ),
            (
                _INSERT_DEVICE_TOKEN_SQL,
                _device_token_params(
                    device_token_id=first_successor_id,
                    token_family_id=family_id,
                    generation=2,
                    predecessor_token_id=predecessor_id,
                ),
            ),
            (
                _INSERT_DEVICE_TOKEN_SQL,
                _device_token_params(
                    device_token_id=spare_successor_id,
                    token_family_id=family_id,
                    token_kind="access",
                ),
            ),
        ),
    )
    shared_secret_hash = _hex64("shared-device-secret")
    _assert_rejected(
        conn,
        _INSERT_DEVICE_TOKEN_SQL,
        _device_token_params(
            device_token_id=uuid4(),
            token_family_id=family_id,
            token_kind="access",
            secret_hash=shared_secret_hash,
        ),
        expected_sqlstate="23505",
        expected_constraint="uq_device_tokens__secret_hash",
        setup=(
            (
                _INSERT_DEVICE_TOKEN_SQL,
                _device_token_params(
                    device_token_id=uuid4(),
                    token_family_id=family_id,
                    token_kind="access",
                    secret_hash=shared_secret_hash,
                ),
            ),
        ),
    )
    _assert_rejected(
        conn,
        _INSERT_DEVICE_TOKEN_SQL,
        _device_token_params(
            device_token_id=uuid4(),
            token_family_id=family_id,
            token_kind="access",
            predecessor_token_id=uuid4(),
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_tokens__rotation_lineage",
    )
    _assert_rejected(
        conn,
        _INSERT_DEVICE_TOKEN_SQL,
        _device_token_params(
            device_token_id=uuid4(), token_family_id=family_id, token_kind="bearer"
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_tokens__token_kind",
    )

    # -- device_authorization_grants ----------------------------------------------------
    approved_session_id = uuid4()
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(_INSERT_WEB_SESSION_SQL, _web_session_params(session_id=approved_session_id))
    _assert_rejected(
        conn,
        _INSERT_GRANT_SQL,
        (
            uuid4(),
            _hex64("grant-no-approver"),
            _hex64("grant-no-approver-polling"),
            uuid4(),
            "obsidian_desktop",
            "approved",
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_authorization_grants__state_matrix",
    )
    _assert_rejected(
        conn,
        _INSERT_DENIED_EXCHANGED_GRANT_SQL,
        (
            uuid4(),
            _hex64("grant-denied-exchanged"),
            _hex64("grant-denied-exchanged-polling"),
            uuid4(),
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_authorization_grants__state_matrix",
    )
    _assert_rejected(
        conn,
        _INSERT_PENDING_WITH_APPROVAL_GRANT_SQL,
        (
            uuid4(),
            _hex64("grant-pending-approved-at"),
            _hex64("grant-pending-approved-at-polling"),
            uuid4(),
            _USER_ID,
            approved_session_id,
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_authorization_grants__state_matrix",
    )
    _assert_rejected(
        conn,
        _INSERT_EXCHANGED_UNLINKED_GRANT_SQL,
        (
            uuid4(),
            _hex64("grant-exchanged-no-device"),
            _hex64("grant-exchanged-no-device-polling"),
            uuid4(),
            _USER_ID,
            approved_session_id,
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_authorization_grants__state_matrix",
    )
    shared_user_code_hash = _hex64("shared-user-code")
    _assert_rejected(
        conn,
        _INSERT_GRANT_SQL,
        (
            uuid4(),
            shared_user_code_hash,
            _hex64("shared-user-code-polling"),
            uuid4(),
            "obsidian_desktop",
            "pending",
        ),
        expected_sqlstate="23505",
        expected_constraint="uq_device_authorization_grants__user_code_hash",
        setup=(
            (
                _INSERT_GRANT_SQL,
                (
                    uuid4(),
                    shared_user_code_hash,
                    _hex64("shared-user-code-polling-b"),
                    uuid4(),
                    "obsidian_desktop",
                    "pending",
                ),
            ),
        ),
    )
    _assert_rejected(
        conn,
        _INSERT_GRANT_SQL,
        (
            uuid4(),
            _hex64("grant-bad-platform-class"),
            _hex64("grant-bad-platform-class-polling"),
            uuid4(),
            "obsidian_web",
            "pending",
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_authorization_grants__platform_class",
    )
    _assert_rejected(
        conn,
        _INSERT_GRANT_SQL.replace("'obsidian_sync', %s", "'admin_sync', %s"),
        (
            uuid4(),
            _hex64("grant-bad-scope"),
            _hex64("grant-bad-scope-polling"),
            uuid4(),
            "obsidian_desktop",
            "pending",
        ),
        expected_sqlstate="23514",
        expected_constraint="ck_device_authorization_grants__requested_scope",
    )

    # -- authentication_throttle_buckets --------------------------------------------------
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.authentication_throttle_buckets "
        "(throttle_bucket_id, bucket_kind, bucket_hash, failed_attempt_count) "
        "VALUES (%s, 'login_username', %s, -1)",
        (uuid4(), _hex64("negative-attempts")),
        expected_sqlstate="23514",
        expected_constraint="ck_authentication_throttle_buckets__failed_attempt_count",
    )
    _assert_rejected(
        conn,
        "INSERT INTO knowledge.authentication_throttle_buckets "
        "(throttle_bucket_id, bucket_kind, bucket_hash, failed_attempt_count) "
        "VALUES (%s, 'login_username', %s, 1)",
        (uuid4(), _hex64("duplicate-bucket")),
        expected_sqlstate="23505",
        expected_constraint="uq_authentication_throttle_buckets__kind_hash",
        setup=(
            (
                "INSERT INTO knowledge.authentication_throttle_buckets "
                "(throttle_bucket_id, bucket_kind, bucket_hash, failed_attempt_count) "
                "VALUES (%s, 'login_username', %s, 1)",
                (uuid4(), _hex64("duplicate-bucket")),
            ),
        ),
    )
