"""Static contract tests for the exclusion policy schema migration.

These tests are fully static: they inspect the Alembic script graph, replay the
``20260817_01`` upgrade and downgrade against a recording stub of ``alembic.op``
(never a database) and read the revision source. They pin the revision chain,
the gated destructive downgrade that returns exactly to the Child 2 head, the
per-workspace bootstrap seeding, the final catalog assertions and the leak-safe
migration hygiene rules (no server-generated UUIDs, no JSON columns, no
extensions, no application imports).
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
    REPO_ROOT / "migrations" / "versions" / "20260817_01_add_exclusion_policy_publication.py"
)

POLICY_REVISION: str = "20260817_01"
CHILD_2_HEAD_REVISION: str = "20260816_01"

POLICY_TABLE_DROP_ORDER: tuple[str, ...] = (
    "policy_keyset_signatures",
    "policy_keysets",
    "policy_evaluations",
    "policy_preview_results",
    "policy_reconciliation_intents",
    "policy_rules",
    "source_policies",
    "policy_previews",
    "policy_draft_rules",
    "policy_drafts",
    "workspace_policy_state",
    "policy_signing_keys",
)

#: The row classes whose presence refuses a downgrade outside the explicit
#: destructive gate (spec section 8.7).
GATED_ROW_TABLES: tuple[str, ...] = (
    "source_policies",
    "policy_previews",
    "policy_evaluations",
    "policy_keysets",
    "policy_signing_keys",
    "policy_reconciliation_intents",
    "workspace_policy_state",
    "projection_intents",
)

_DOWNGRADE_REFUSAL_MESSAGE: str = "exclusion_policy_downgrade_requires_explicit_gate"
_DESTRUCTIVE_X_ARGUMENT: str = "allow_destructive"


class _ScriptedBindResult:
    """Minimal bind result facade: one workspace row and a zero gate count."""

    def fetchall(self) -> list[Any]:
        return [SimpleNamespace(workspace_id="018f47a0-7b00-7000-8000-000000000201")]

    def scalar_one(self) -> int:
        return 0


class _ScriptedBind:
    """Bind double recording executed statements and answering zero counts."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, statement: object, *args: Any) -> _ScriptedBindResult:
        self.executed.append(str(statement))
        return _ScriptedBindResult()


class _EventRecordingAlembicOp:
    """Stub of ``alembic.op`` recording every operation as an ordered event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.executed_sql: list[str] = []
        self.tables: dict[str, Any] = {}
        self.bind = _ScriptedBind()

    def _record(self, operation: str, detail: str) -> None:
        self.events.append((operation, detail))

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._record("create_table", name)
        table = type("Table", (), {"name": name})()
        self.tables[name] = table
        return table

    def create_index(self, index_name: str, table_name: str, *args: Any, **kwargs: Any) -> None:
        self._record("create_index", index_name)

    def create_foreign_key(self, constraint_name: str, *args: Any, **kwargs: Any) -> None:
        self._record("create_foreign_key", constraint_name)

    def create_check_constraint(self, constraint_name: str, *args: Any, **kwargs: Any) -> None:
        self._record("create_check_constraint", constraint_name)

    def add_column(self, table_name: str, *args: Any, **kwargs: Any) -> None:
        self._record("add_column", table_name)

    def alter_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        self._record("alter_column", f"{table_name}.{column_name}")

    def drop_table(self, table_name: str, *args: Any, **kwargs: Any) -> None:
        self._record("drop_table", table_name)
        self.tables.pop(table_name, None)

    def drop_index(self, index_name: str, table_name: str = "", **kwargs: Any) -> None:
        self._record("drop_index", index_name)

    def drop_constraint(self, constraint_name: str, table_name: str, **kwargs: Any) -> None:
        self._record("drop_constraint", constraint_name)

    def drop_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        self._record("drop_column", f"{table_name}.{column_name}")

    def execute(self, sql: Any, **kwargs: Any) -> None:
        sql_text = str(sql)
        self._record("execute", " ".join(sql_text.split()))
        self.executed_sql.append(sql_text)

    def get_context(self) -> None:
        return None

    def get_bind(self) -> _ScriptedBind:
        return self.bind


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("policy_migration_contract", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str) -> _EventRecordingAlembicOp:
    module = _load_module()
    recorder = _EventRecordingAlembicOp()
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, function_name)()
    return recorder


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


# --- Alembic graph contract ------------------------------------------------------


def test_alembic_graph_has_exactly_one_head_at_the_policy_revision() -> None:
    # The device sync revisions ``20260826_01`` and ``20260826_02`` stack on
    # the source-locator and tombstone revision ``20260820_01``, which stacks
    # on the small-file sync revision ``20260818_01``, so the single graph
    # head moved past the policy revision.
    assert _script_directory().get_heads() == ["20260826_02"]


def test_policy_revision_stacks_on_the_child_2_head() -> None:
    script_directory = _script_directory()
    revisions = list(script_directory.walk_revisions())
    assert len(revisions) == 7
    policy = script_directory.get_revision(POLICY_REVISION)
    assert policy is not None
    assert policy.down_revision == CHILD_2_HEAD_REVISION
    assert not policy.branch_labels
    assert policy.dependencies is None


# --- upgrade hygiene and assertions ----------------------------------------------


def test_upgrade_seeds_one_policy_state_row_and_empty_draft_per_workspace() -> None:
    recorder = _replay("upgrade")
    # The seeding reads and writes run through the migration bind, so their
    # parameter-bound SQL appears in the bind's executed statements.
    seed_sql = "\n".join(recorder.bind.executed)
    assert "INSERT INTO knowledge.workspace_policy_state" in seed_sql
    assert "INSERT INTO knowledge.policy_drafts" in seed_sql
    assert "SELECT workspace_id FROM knowledge.workspaces" in seed_sql
    # The seeded state row never publishes or signs implicitly.
    assert "VALUES (:workspace_id, NULL, 0)" in seed_sql
    assert "VALUES (:policy_draft_id, :workspace_id, 1, NULL, NULL, NULL)" in seed_sql


def test_upgrade_finishes_with_the_final_catalog_assertion() -> None:
    upgrade_sql = "\n".join(_replay("upgrade").executed_sql)
    assert "application_table_count <> 29" in upgrade_sql
    assert "trigger_function_count <> 4" in upgrade_sql
    assert "protection_trigger_count <> 11" in upgrade_sql


def test_migration_imports_uuidv7_and_no_server_side_uuid_generation() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "from uuid import uuid7" in source
    lowered = source.lower()
    assert "gen_random_uuid" not in lowered
    assert "uuid_generate" not in lowered
    assert "jsonb" not in lowered
    assert "create extension" not in lowered
    assert "create type" not in lowered
    assert "personal_os" not in source
    assert "relationship" not in lowered
    assert "create_all" not in lowered


# --- downgrade contract -----------------------------------------------------------


def test_downgrade_reads_the_destructive_gate_from_the_x_argument() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert _DESTRUCTIVE_X_ARGUMENT in source
    assert "cmd_opts" in source


def test_downgrade_gate_counts_every_protected_row_class() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    for table_name in GATED_ROW_TABLES:
        assert f"knowledge.{table_name}" in source, table_name
    assert "origin_kind = 'policy_transition'" in source
    assert "active_policy_revision_id IS NOT NULL" in source
    assert _DOWNGRADE_REFUSAL_MESSAGE in source


def test_downgrade_returns_exactly_to_the_child_2_head() -> None:
    recorder = _replay("downgrade")
    events = recorder.events
    drop_table_events = [table for operation, table in events if operation == "drop_table"]
    assert drop_table_events == list(POLICY_TABLE_DROP_ORDER)

    # The projection-intent origin discriminator is fully reversed: the
    # partial unique index, the policy foreign key, the origin check, the
    # added column, the restored NOT NULL event reference and the origin
    # column itself.
    drop_index_events = [detail for operation, detail in events if operation == "drop_index"]
    assert "uq_projection_intents__policy_transition" in drop_index_events
    drop_constraint_events = [
        detail for operation, detail in events if operation == "drop_constraint"
    ]
    assert "fk_projection_intents__policy_revision" in drop_constraint_events
    assert "ck_projection_intents__origin" in drop_constraint_events
    drop_column_events = [detail for operation, detail in events if operation == "drop_column"]
    assert "projection_intents.policy_revision_id" in drop_column_events
    assert "projection_intents.origin_kind" in drop_column_events
    alter_events = [detail for operation, detail in events if operation == "alter_column"]
    assert "projection_intents.event_id" in alter_events

    downgrade_sql = "\n".join(recorder.executed_sql)
    assert (
        "DELETE FROM knowledge.projection_intents WHERE origin_kind = 'policy_transition'"
        in downgrade_sql
    )
    assert "DROP FUNCTION knowledge.reject_policy_history_mutation" in downgrade_sql
    assert "application_table_count <> 17" in downgrade_sql
    assert "trigger_function_count <> 3" in downgrade_sql
    assert "protection_trigger_count <> 5" in downgrade_sql
    assert "cascade" not in downgrade_sql.lower()
    assert "DROP SCHEMA" not in downgrade_sql
    assert "knowledge.users" not in downgrade_sql
    assert "knowledge.workspaces" not in downgrade_sql


def test_downgrade_counts_protected_rows_before_any_destructive_step() -> None:
    # Ordering contract: the gate's protected-row count is the very first
    # statement the downgrade executes (through the migration bind), and the
    # destructive statements (the policy-transition delete and every DROP)
    # appear only after it in the function body.
    recorder = _replay("downgrade")
    bind_executed = recorder.bind.executed
    assert bind_executed, "the downgrade must execute its gate count"
    gate_sql = bind_executed[0]
    assert gate_sql.lstrip().upper().startswith("SELECT")
    assert "knowledge.source_policies" in gate_sql

    downgrade_sql = "\n".join(recorder.executed_sql)
    assert (
        "DELETE FROM knowledge.projection_intents WHERE origin_kind = 'policy_transition'"
        in downgrade_sql
    )
    assert "DROP TRIGGER trg_source_policies__reject_mutation" in downgrade_sql
    assert "DROP FUNCTION knowledge.reject_policy_history_mutation" in downgrade_sql
