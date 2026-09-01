"""Upgrade/downgrade contract for the deferred multipart provider identity.

These tests replay the ``20260828_03`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the operation-size-bound head, the
two-column upgrade that relaxes exactly the private provider identity
columns to nullable (spec 6.1 persist-before-create), the downgrade that
restores the mandatory-identity shape only after no pending session row
remains (and the closed refusal token otherwise), and the migration hygiene
rules that keep the revision free of any domain import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260828_03_defer_multipart_provider_identity.py"
)

DEFERRED_IDENTITY_REVISION: str = "20260828_03"
SIZE_BOUND_REVISION: str = "20260828_02"

SESSION_TABLE_NAME: str = "multipart_uploads"

_DOWNGRADE_REFUSAL_MESSAGE: str = "multipart_provider_identity_downgrade_has_pending_rows"


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

    def __init__(self, pending_row_count: int) -> None:
        self.executed: list[str] = []
        self._pending_row_count = pending_row_count

    def execute(self, statement: object, *args: Any) -> _ScriptedBindResult:
        self.executed.append(str(statement))
        return _ScriptedBindResult(self._pending_row_count)


class _EventRecordingAlembicOp:
    """Stub of ``alembic.op`` recording every operation as an ordered event."""

    def __init__(self, pending_row_count: int) -> None:
        self.events: list[tuple[str, str, bool]] = []
        self.bind = _ScriptedBind(pending_row_count)

    def alter_column(
        self,
        table_name: str,
        column_name: str,
        *,
        nullable: bool | None = None,
        **kwargs: Any,
    ) -> None:
        assert nullable is not None, "the stub records exactly nullability changes"
        schema = str(kwargs.get("schema", "public"))
        self.events.append(("alter_column", f"{schema}.{table_name}.{column_name}", nullable))

    def get_bind(self) -> _ScriptedBind:
        return self.bind


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "multipart_deferred_identity_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str, *, pending_row_count: int = 0) -> _EventRecordingAlembicOp:
    module = _load_module()
    recorder = _EventRecordingAlembicOp(pending_row_count)
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, function_name)()
    return recorder


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_revision_extends_the_operation_size_bound_head() -> None:
    module = _load_module()
    assert module.revision == DEFERRED_IDENTITY_REVISION
    assert module.down_revision == SIZE_BOUND_REVISION


def test_revision_is_the_single_alembic_head() -> None:
    scripts = _script_directory()
    # The sealed-token revision ``20260828_04``, the submitted policy verdict
    # revision ``20260829_01``, the grant-poll bucket kind revision
    # ``20260901_01``, the device-sync scale index revision ``20260901_02``
    # and the terminal locator remediation revision ``20260901_03`` stack
    # above this head.
    assert scripts.get_heads() == ["20260902_01"]


def test_upgrade_relaxes_exactly_the_two_identity_columns() -> None:
    recorder = _replay("upgrade")
    assert recorder.events == [
        ("alter_column", "knowledge.multipart_uploads.staging_key", True),
        ("alter_column", "knowledge.multipart_uploads.provider_upload_id", True),
    ]


def test_downgrade_restores_mandatory_identity_without_pending_rows() -> None:
    recorder = _replay("downgrade", pending_row_count=0)
    assert recorder.events == [
        ("alter_column", "knowledge.multipart_uploads.staging_key", False),
        ("alter_column", "knowledge.multipart_uploads.provider_upload_id", False),
    ]
    assert any(
        "count(*)" in statement and "staging_key IS NULL" in statement
        for statement in recorder.bind.executed
    )


def test_downgrade_refuses_while_pending_session_rows_remain() -> None:
    module = _load_module()
    recorder = _EventRecordingAlembicOp(pending_row_count=2)
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
