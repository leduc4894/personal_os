"""Upgrade/downgrade contract for the sealed multipart operation token.

These tests replay the ``20260828_04`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the deferred-identity head, the
three-column upgrade that adds exactly the AEAD-sealed raw-token preimage
columns to the canonical session row (nullable: a reserved session before
this revision, and any composition without a codec, carries no seal and the
evidence read fails closed instead of guessing), the downgrade that drops
the sealed columns only after no forward-state session still needs its
seal (and the closed refusal token otherwise), and the migration hygiene
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
    REPO_ROOT / "migrations" / "versions" / "20260828_04_seal_multipart_operation_token.py"
)

SEALED_TOKEN_REVISION: str = "20260828_04"
DEFERRED_IDENTITY_REVISION: str = "20260828_03"

SESSION_TABLE_NAME: str = "multipart_uploads"

SEALED_COLUMNS: tuple[str, ...] = (
    "operation_token_ciphertext",
    "operation_token_nonce",
    "operation_token_key_id",
)

_DOWNGRADE_REFUSAL_MESSAGE: str = "multipart_operation_token_downgrade_has_forward_sealed_rows"


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

    def __init__(self, forward_sealed_row_count: int) -> None:
        self.executed: list[str] = []
        self._forward_sealed_row_count = forward_sealed_row_count

    def execute(self, statement: object, *args: Any) -> _ScriptedBindResult:
        self.executed.append(str(statement))
        return _ScriptedBindResult(self._forward_sealed_row_count)


class _EventRecordingAlembicOp:
    """Stub of ``alembic.op`` recording every operation as an ordered event."""

    def __init__(self, forward_sealed_row_count: int) -> None:
        self.events: list[tuple[str, str, bool]] = []
        self.bind = _ScriptedBind(forward_sealed_row_count)

    def add_column(
        self,
        table_name: str,
        column: Any,
        *,
        schema: str | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self.events.append(("add_column", f"{schema}.{table_name}.{column.name}", True))

    def create_check_constraint(
        self,
        constraint_name: str,
        table_name: str,
        condition: str,
        *,
        schema: str | None = None,
        **kwargs: Any,
    ) -> None:
        del condition, kwargs
        self.events.append(
            ("create_check_constraint", f"{schema}.{table_name}.{constraint_name}", True)
        )

    def drop_column(
        self,
        table_name: str,
        column_name: str,
        *,
        schema: str | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self.events.append(("drop_column", f"{schema}.{table_name}.{column_name}", False))

    def get_bind(self) -> _ScriptedBind:
        return self.bind


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "multipart_operation_token_seal_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str, *, forward_sealed_row_count: int = 0) -> _EventRecordingAlembicOp:
    module = _load_module()
    recorder = _EventRecordingAlembicOp(forward_sealed_row_count)
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, function_name)()
    return recorder


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_revision_extends_the_deferred_identity_head() -> None:
    module = _load_module()
    assert module.revision == SEALED_TOKEN_REVISION
    assert module.down_revision == DEFERRED_IDENTITY_REVISION


def test_revision_is_the_single_alembic_head() -> None:
    scripts = _script_directory()
    # The submitted policy verdict revision ``20260829_01``, the grant-poll
    # bucket kind revision ``20260901_01``, the device-sync scale index
    # revision ``20260901_02`` and the terminal locator remediation
    # revision ``20260901_03`` stack on this head.
    assert scripts.get_heads() == ["20260901_03"]


def test_upgrade_adds_exactly_the_three_sealed_columns() -> None:
    recorder = _replay("upgrade")
    assert recorder.events == [
        ("add_column", f"knowledge.{SESSION_TABLE_NAME}.{column}", True)
        for column in SEALED_COLUMNS
    ] + [
        (
            "create_check_constraint",
            "knowledge.multipart_uploads.ck_multipart_uploads__operation_token_seal_biconditional",
            True,
        ),
        (
            "create_check_constraint",
            "knowledge.multipart_uploads.ck_multipart_uploads__operation_token_key_id",
            True,
        ),
    ]


def test_downgrade_drops_the_sealed_columns_without_forward_sealed_rows() -> None:
    recorder = _replay("downgrade", forward_sealed_row_count=0)
    # The columns drop in the reverse order they were added.
    assert recorder.events == [
        ("drop_column", f"knowledge.{SESSION_TABLE_NAME}.{column}", False)
        for column in reversed(SEALED_COLUMNS)
    ]
    assert any(
        "count(*)" in statement and "operation_token_ciphertext IS NOT NULL" in statement
        for statement in recorder.bind.executed
    )


def test_downgrade_refuses_while_forward_sessions_still_carry_a_seal() -> None:
    module = _load_module()
    recorder = _EventRecordingAlembicOp(forward_sealed_row_count=2)
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
