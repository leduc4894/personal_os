"""Upgrade/downgrade contract for the grant-poll pacing bucket kind.

These tests replay the ``20260901_01`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the submitted policy verdict head,
the single-CHECK upgrade that recreates the closed ``bucket_kind`` list with
the seventh ``grant_poll`` member, the downgrade that restores the original
six-value list exactly as ``20260816_01`` wrote it, the equality of the DDL
list with the domain ``ThrottleBucketKind`` enum, and the migration hygiene
rules that keep the revision free of any domain import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

from personal_os.authentication.sessions import ThrottleBucketKind

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260901_01_add_grant_poll_pacing_bucket_kind.py"
)

GRANT_POLL_PACING_REVISION: str = "20260901_01"
SUBMITTED_POLICY_VERDICT_REVISION: str = "20260829_01"

BUCKET_KIND_CHECK_NAME: str = "ck_authentication_throttle_buckets__bucket_kind"
BUCKET_TABLE_NAME: str = "authentication_throttle_buckets"
SCHEMA_NAME: str = "knowledge"

GRANT_POLL_ROW_DELETE: str = (
    "DELETE FROM knowledge.authentication_throttle_buckets WHERE bucket_kind = 'grant_poll'"
)

ORIGINAL_SIX_KIND_CHECK: str = (
    "bucket_kind IN ('login_username', 'login_source', 'grant_creation', "
    "'user_code_lookup', 'totp_verification', 'recovery_verification')"
)
AMENDED_SEVEN_KIND_CHECK: str = (
    "bucket_kind IN ('login_username', 'login_source', 'grant_creation', "
    "'user_code_lookup', 'totp_verification', 'recovery_verification', 'grant_poll')"
)


class _EventRecordingAlembicOp:
    """Stub of ``alembic.op`` recording every operation as an ordered event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def execute(self, sql: object, **kwargs: Any) -> None:
        self.events.append(("execute", str(getattr(sql, "text", sql)), ""))

    def drop_constraint(self, constraint_name: str, table_name: str, **kwargs: Any) -> None:
        self.events.append(("drop_constraint", constraint_name, table_name))

    def create_check_constraint(
        self, constraint_name: str, table_name: str, condition: str, **kwargs: Any
    ) -> None:
        self.events.append(("create_check_constraint", constraint_name, condition))


def _load_revision_module(module_file_stem: str) -> Any:
    migration_path = REPO_ROOT / "migrations" / "versions" / f"{module_file_stem}.py"
    assert migration_path.is_file(), f"missing grant poll pacing migration: {migration_path.name}"
    spec = importlib.util.spec_from_file_location("grant_poll_pacing_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str) -> _EventRecordingAlembicOp:
    module = _load_revision_module("20260901_01_add_grant_poll_pacing_bucket_kind")
    recorder = _EventRecordingAlembicOp()
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, function_name)()
    return recorder


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_grant_poll_pacing_revision_extends_the_closed_bucket_kind_set() -> None:
    module = _load_revision_module("20260901_01_add_grant_poll_pacing_bucket_kind")
    assert module.revision == GRANT_POLL_PACING_REVISION
    assert module.down_revision == SUBMITTED_POLICY_VERDICT_REVISION
    assert "grant_poll" in module.UPGRADE_KIND_LIST
    assert "grant_poll" not in module.DOWNGRADE_KIND_LIST


def test_canonical_schema_revision_points_at_the_new_head() -> None:
    # The device-sync scale index revision ``20260901_02`` is the head now.
    from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION

    assert CANONICAL_POSTGRESQL_SCHEMA_REVISION == "20260901_02"


def test_grant_poll_pacing_revision_is_chained_below_the_device_sync_scale_index_head() -> None:
    # The device-sync scale index revision ``20260901_02`` stacks on the
    # grant-poll bucket kind revision, so the single graph head moved past it.
    scripts = _script_directory()
    assert scripts.get_heads() == ["20260901_02"]


def test_upgrade_recreates_the_bucket_kind_check_with_the_seventh_value() -> None:
    recorder = _replay("upgrade")
    assert recorder.events == [
        ("drop_constraint", BUCKET_KIND_CHECK_NAME, BUCKET_TABLE_NAME),
        ("create_check_constraint", BUCKET_KIND_CHECK_NAME, AMENDED_SEVEN_KIND_CHECK),
    ]


def test_downgrade_deletes_grant_poll_rows_then_restores_the_six_value_check() -> None:
    """PostgreSQL validates a freshly added CHECK against existing rows, so
    the pacing rows the grant-poll behavior writes must be deleted before the
    original six-value constraint is re-created (BACKLOG 2026-08-16 §13)."""
    recorder = _replay("downgrade")
    assert recorder.events == [
        ("execute", GRANT_POLL_ROW_DELETE, ""),
        ("drop_constraint", BUCKET_KIND_CHECK_NAME, BUCKET_TABLE_NAME),
        ("create_check_constraint", BUCKET_KIND_CHECK_NAME, ORIGINAL_SIX_KIND_CHECK),
    ]


def test_amended_kind_list_equals_the_domain_enum_members() -> None:
    module = _load_revision_module("20260901_01_add_grant_poll_pacing_bucket_kind")
    assert frozenset(module.UPGRADE_KIND_LIST) == frozenset(
        member.value for member in ThrottleBucketKind
    )
    assert len(module.UPGRADE_KIND_LIST) == len(set(module.UPGRADE_KIND_LIST))


def test_upgrade_passes_the_knowledge_schema_and_the_check_type() -> None:
    """The drop must target the check constraint inside ``knowledge``."""

    module = _load_revision_module("20260901_01_add_grant_poll_pacing_bucket_kind")
    captured: list[SimpleNamespace] = []

    class _SchemaRecordingAlembicOp(_EventRecordingAlembicOp):
        def drop_constraint(self, constraint_name: str, table_name: str, **kwargs: Any) -> None:
            captured.append(SimpleNamespace(name=constraint_name, table=table_name, kwargs=kwargs))
            super().drop_constraint(constraint_name, table_name, **kwargs)

        def create_check_constraint(
            self, constraint_name: str, table_name: str, condition: str, **kwargs: Any
        ) -> None:
            captured.append(SimpleNamespace(name=constraint_name, table=table_name, kwargs=kwargs))
            super().create_check_constraint(constraint_name, table_name, condition, **kwargs)

    module.op = _SchemaRecordingAlembicOp()  # type: ignore[attr-defined]
    module.upgrade()
    assert captured, "the upgrade must call alembic.op"
    for call in captured:
        assert call.table == BUCKET_TABLE_NAME
        assert call.kwargs.get("schema") == SCHEMA_NAME
    assert captured[0].kwargs.get("type_") == "check"


def test_migration_hygiene_rules_hold() -> None:
    source = _migration_source()
    lowered = source.lower()
    assert "personal_os" not in source
    assert "gen_random_uuid" not in lowered
    assert "uuid_generate" not in lowered
    assert "jsonb" not in lowered
    assert "create extension" not in lowered


def test_migration_records_no_tables_or_indexes() -> None:
    recorder = _replay("upgrade")
    for operation, _, _ in recorder.events:
        assert operation in {"drop_constraint", "create_check_constraint"}
