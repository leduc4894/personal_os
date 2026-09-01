"""Upgrade/downgrade contract for the widened operation declared-size bound.

These tests replay the ``20260828_02`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the multipart session head, the
single-CHECK upgrade that replaces the stale 16 MiB single-part ceiling
with the closed 100 MiB product maximum, the downgrade that restores the
original ceiling only after no recorded operation row exceeds it (and the
closed refusal token otherwise), and the migration hygiene rules that keep
the revision free of any domain import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from personal_os.small_file_sync.contracts import MAX_UPLOAD_FILE_SIZE_BYTES

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = (
    REPO_ROOT
    / "migrations"
    / "versions"
    / "20260828_02_widen_small_file_operation_declared_size_bound.py"
)

SIZE_BOUND_REVISION: str = "20260828_02"
MULTIPART_SESSION_REVISION: str = "20260828_01"

DECLARED_SIZE_CHECK_NAME: str = "ck_small_file_upload_operations__declared_size_bytes"

_DOWNGRADE_REFUSAL_MESSAGE: str = "small_file_operation_size_downgrade_has_oversized_rows"


class _ScriptedBindResult:
    """Minimal bind result facade carrying one scalar answer."""

    def __init__(self, scalar_answer: int) -> None:
        self._scalar_answer = scalar_answer

    def fetchall(self) -> list[Any]:
        return []

    def scalar_one(self) -> int:
        return self._scalar_answer


class _ScriptedBind:
    """Bind double recording executed statements and answering one count."""

    def __init__(self, oversized_row_count: int) -> None:
        self.executed: list[str] = []
        self._oversized_row_count = oversized_row_count

    def execute(self, statement: object, *args: Any) -> _ScriptedBindResult:
        self.executed.append(str(statement))
        return _ScriptedBindResult(self._oversized_row_count)


class _EventRecordingAlembicOp:
    """Stub of ``alembic.op`` recording every operation as an ordered event."""

    def __init__(self, oversized_row_count: int) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.bind = _ScriptedBind(oversized_row_count)

    def drop_constraint(self, constraint_name: str, table_name: str, **kwargs: Any) -> None:
        self.events.append(("drop_constraint", constraint_name, table_name))

    def create_check_constraint(
        self, constraint_name: str, table_name: str, condition: str, **kwargs: Any
    ) -> None:
        self.events.append(("create_check_constraint", constraint_name, condition))

    def get_bind(self) -> _ScriptedBind:
        return self.bind


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("operation_size_bound_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str, *, oversized_row_count: int = 0) -> _EventRecordingAlembicOp:
    module = _load_module()
    recorder = _EventRecordingAlembicOp(oversized_row_count)
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, function_name)()
    return recorder


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_revision_extends_the_multipart_session_head() -> None:
    module = _load_module()
    assert module.revision == SIZE_BOUND_REVISION
    assert module.down_revision == MULTIPART_SESSION_REVISION


def test_revision_is_chained_below_the_deferred_identity_head() -> None:
    scripts = _script_directory()
    assert scripts.get_heads() == ["20260901_02"]
    revision = scripts.get_revision(SIZE_BOUND_REVISION)
    assert revision is not None
    assert revision.down_revision == MULTIPART_SESSION_REVISION


def test_upgrade_replaces_exactly_the_declared_size_check() -> None:
    recorder = _replay("upgrade")
    assert recorder.events == [
        ("drop_constraint", DECLARED_SIZE_CHECK_NAME, "small_file_upload_operations"),
        (
            "create_check_constraint",
            DECLARED_SIZE_CHECK_NAME,
            "declared_size_bytes BETWEEN 0 AND 104857600",
        ),
    ]


def test_widened_bound_equals_the_domain_product_maximum() -> None:
    module = _load_module()
    assert int(module._MAXIMUM_DECLARED_SIZE_BYTES) == MAX_UPLOAD_FILE_SIZE_BYTES


def test_downgrade_restores_the_single_part_ceiling_without_oversized_rows() -> None:
    recorder = _replay("downgrade", oversized_row_count=0)
    assert recorder.events == [
        ("drop_constraint", DECLARED_SIZE_CHECK_NAME, "small_file_upload_operations"),
        (
            "create_check_constraint",
            DECLARED_SIZE_CHECK_NAME,
            "declared_size_bytes BETWEEN 0 AND 16777216",
        ),
    ]
    assert any(
        "count(*)" in statement and "declared_size_bytes" in statement
        for statement in recorder.bind.executed
    )


def test_downgrade_refuses_while_oversized_operation_rows_remain() -> None:
    module = _load_module()
    recorder = _EventRecordingAlembicOp(oversized_row_count=3)
    module.op = recorder  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match=module._DOWNGRADE_REFUSAL_MESSAGE):
        module.downgrade()
    assert recorder.events == []


def test_downgrade_refusal_uses_the_closed_module_token() -> None:
    module = _load_module()
    assert module._DOWNGRADE_REFUSAL_MESSAGE == _DOWNGRADE_REFUSAL_MESSAGE


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
