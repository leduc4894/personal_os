"""Upgrade/downgrade contract for the terminal locator data remediation.

These tests replay the ``20260901_03`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the device-sync scale index head,
the single guarded UPDATE that clears the raw ``normalized_locator`` on
every already-terminal operation row while leaving the retained
``locator_fingerprint`` and ``updated_at`` untouched, the downgrade that is
a documented privacy no-op, and the migration hygiene rules that keep the
revision free of any domain import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260901_03_clear_terminal_small_file_locators.py"
)

TERMINAL_LOCATOR_REMEDIATION_REVISION: str = "20260901_03"
DEVICE_SYNC_SCALE_INDEX_REVISION: str = "20260901_02"

#: The exact guarded UPDATE the upgrade emits, restated as the assertion's
#: expected shape (table, SET and WHERE) so any drift fails loudly here
#: before it can reach a database.
EXPECTED_TERMINAL_LOCATOR_CLEAR_SQL: str = (
    "UPDATE knowledge.small_file_upload_operations "
    "SET normalized_locator = NULL "
    "WHERE state IN ('committed', 'failed') "
    "AND normalized_locator IS NOT NULL"
)


class _EventRecordingAlembicOp:
    """Stub of ``alembic.op`` recording every operation as an ordered event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.calls: dict[str, SimpleNamespace] = {}

    def execute(self, sql: object, **kwargs: Any) -> None:
        self.events.append(("execute", str(getattr(sql, "text", sql))))
        self.calls.setdefault("execute", SimpleNamespace(sql=sql, kwargs=kwargs))


def _load_revision_module() -> Any:
    assert MIGRATION_PATH.is_file(), (
        f"missing terminal locator remediation migration: {MIGRATION_PATH.name}"
    )
    spec = importlib.util.spec_from_file_location(
        "terminal_locator_remediation_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str) -> _EventRecordingAlembicOp:
    module = _load_revision_module()
    recorder = _EventRecordingAlembicOp()
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, function_name)()
    return recorder


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_terminal_locator_remediation_revision_stacks_on_the_device_sync_head() -> None:
    module = _load_revision_module()
    assert module.revision == TERMINAL_LOCATOR_REMEDIATION_REVISION
    assert module.down_revision == DEVICE_SYNC_SCALE_INDEX_REVISION


def test_canonical_schema_revision_points_at_the_new_head() -> None:
    from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION

    assert CANONICAL_POSTGRESQL_SCHEMA_REVISION == TERMINAL_LOCATOR_REMEDIATION_REVISION


def test_terminal_locator_remediation_revision_is_the_single_alembic_head() -> None:
    scripts = _script_directory()
    assert scripts.get_heads() == [TERMINAL_LOCATOR_REMEDIATION_REVISION]


def test_upgrade_emits_exactly_one_guarded_terminal_locator_clear() -> None:
    recorder = _replay("upgrade")
    assert recorder.events == [("execute", EXPECTED_TERMINAL_LOCATOR_CLEAR_SQL)]


def test_upgrade_clears_only_the_raw_locator_on_terminal_states() -> None:
    """The SET touches one column; the guard admits only terminal-with-locator rows."""

    module = _load_revision_module()
    sql: str = " ".join(module.TERMINAL_LOCATOR_CLEAR_SQL.split())
    lowered = sql.lower()
    assert "set normalized_locator = null" in lowered
    assert "state in ('committed', 'failed')" in lowered
    assert "normalized_locator is not null" in lowered
    # The retained digest keeps exact-replay identity intact: the UPDATE must
    # never touch the fingerprint column.
    assert "locator_fingerprint" not in lowered
    # Schema-authority backfill, not a domain write: no updated_at churn.
    assert "updated_at" not in lowered


def test_downgrade_is_a_documented_privacy_no_op() -> None:
    recorder = _replay("downgrade")
    assert recorder.events == []


def test_downgrade_docstring_records_the_irrecoverable_provenance() -> None:
    source = _migration_source()
    assert "irrecoverable" in source.lower() or "not recover" in source.lower()
    assert "allow_destructive" in source


def test_migration_hygiene_rules_hold() -> None:
    source = _migration_source()
    lowered = source.lower()
    assert "personal_os" not in source
    assert "gen_random_uuid" not in lowered
    assert "uuid_generate" not in lowered
    assert "jsonb" not in lowered
    assert "create extension" not in lowered
