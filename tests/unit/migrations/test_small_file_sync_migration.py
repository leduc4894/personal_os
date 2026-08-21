"""Static upgrade/downgrade contract for the small-file sync operation migration.

These tests replay the ``20260818_01`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the exclusion policy head, the
closed column set of the single ``small_file_upload_operations`` table (no
bytes, raw path, token, receipt or provider key column ever appears), the
identity/token-hash uniqueness, the non-terminal expiry partial index, the
final catalog assertions (thirty tables after the upgrade, twenty-nine after
the downgrade) and the gated destructive downgrade that refuses to discard
recorded upload-operation evidence outside the explicit operator gate.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from personal_os.small_file_sync.contracts import MAX_SINGLE_PART_FILE_SIZE_BYTES

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260818_01_add_small_file_sync_operations.py"
)

SMALL_FILE_REVISION: str = "20260818_01"
POLICY_HEAD_REVISION: str = "20260817_01"

#: The exact closed column set of the operation table. Anything else — a byte
#: payload, a locator/path, the raw operation token, a provider receipt or an
#: object key — must fail this pin.
OPERATION_TABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "operation_id",
        "operation_token_hash",
        "workspace_id",
        "device_id",
        "event_id",
        "idempotency_key",
        "operation_kind",
        "declared_sha256",
        "declared_size_bytes",
        "declared_media_type",
        "policy_revision_number",
        "reserved_source_id",
        "update_source_id",
        "update_base_version_id",
        "state",
        "safe_error_code",
        "result_kind",
        "result_source_id",
        "result_source_version_id",
        "result_content_version",
        "result_committed_at",
        "expires_at",
        "created_at",
        "updated_at",
    }
)

_DOWNGRADE_REFUSAL_MESSAGE: str = "small_file_sync_downgrade_requires_explicit_gate"
_DESTRUCTIVE_X_ARGUMENT: str = "allow_destructive"


class _ScriptedBindResult:
    """Minimal bind result facade: a zero scalar and no seeded rows."""

    def fetchall(self) -> list[Any]:
        return []

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
        table = SimpleNamespace(name=name, args=args)
        self.tables[name] = table
        return table

    def create_index(self, index_name: str, table_name: str, *args: Any, **kwargs: Any) -> None:
        self._record("create_index", index_name)

    def create_foreign_key(self, constraint_name: str, *args: Any, **kwargs: Any) -> None:
        self._record("create_foreign_key", constraint_name)

    def drop_table(self, table_name: str, *args: Any, **kwargs: Any) -> None:
        self._record("drop_table", table_name)
        self.tables.pop(table_name, None)

    def drop_index(self, index_name: str, table_name: str = "", **kwargs: Any) -> None:
        self._record("drop_index", index_name)

    def drop_constraint(self, constraint_name: str, table_name: str, **kwargs: Any) -> None:
        self._record("drop_constraint", constraint_name)

    def execute(self, sql: Any, **kwargs: Any) -> None:
        sql_text = str(sql)
        self._record("execute", " ".join(sql_text.split()))
        self.executed_sql.append(sql_text)

    def get_context(self) -> None:
        return None

    def get_bind(self) -> _ScriptedBind:
        return self.bind


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("small_file_migration_contract", MIGRATION_PATH)
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


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


# --- Alembic graph contract -----------------------------------------------------


def test_alembic_graph_has_exactly_one_head_at_the_small_file_revision() -> None:
    # The locator-lifecycle revision ``20260820_01`` stacks on the
    # small-file sync revision, so the single graph head moved past it.
    assert _script_directory().get_heads() == ["20260820_01"]


def test_small_file_revision_stacks_on_the_policy_head() -> None:
    script_directory = _script_directory()
    revisions = list(script_directory.walk_revisions())
    assert len(revisions) == 5
    revision = script_directory.get_revision(SMALL_FILE_REVISION)
    assert revision is not None
    assert revision.down_revision == POLICY_HEAD_REVISION
    assert not revision.branch_labels
    assert revision.dependencies is None


# --- upgrade contract ------------------------------------------------------------


def test_upgrade_creates_exactly_one_operation_table() -> None:
    recorder = _replay("upgrade")
    create_table_events = [
        detail for operation, detail in recorder.events if operation == "create_table"
    ]
    assert create_table_events == ["small_file_upload_operations"]


def test_upgrade_column_set_is_the_closed_operation_record() -> None:
    recorder = _replay("upgrade")
    table = recorder.tables["small_file_upload_operations"]
    column_names = {argument.name for argument in table.args if isinstance(argument, sa.Column)}
    assert column_names == OPERATION_TABLE_COLUMNS


def test_upgrade_declares_identity_and_token_hash_uniqueness() -> None:
    source = _migration_source()
    assert "uq_small_file_upload_operations__identity" in source
    assert "uq_small_file_upload_operations__operation_token_hash" in source


def test_upgrade_adds_the_nonterminal_expiry_partial_index() -> None:
    recorder = _replay("upgrade")
    index_events = [detail for operation, detail in recorder.events if operation == "create_index"]
    assert "ix_small_file_upload_operations__nonterminal_expiry" in index_events
    source = _migration_source()
    assert "state IN ('pending', 'receiving')" in source


def test_declared_size_ceiling_equals_the_domain_constant() -> None:
    """The migration's DDL ceiling and the domain ceiling are one 16 MiB limit.

    A revision may not import the domain constant (the hygiene rules forbid
    any ``personal_os`` import), so the single-part ceiling exists twice in
    Python; this pin fails the suite on any drift between the DDL bound and
    ``MAX_SINGLE_PART_FILE_SIZE_BYTES``. The plugin's ``MAX_FILE_SIZE_BYTES``
    mirror carries the same value, pinned by its own contracts test — no
    cross-language harness by design.
    """
    module = _load_module()
    declared_ceiling = int(module._MAXIMUM_DECLARED_SIZE_BYTES)
    assert declared_ceiling == MAX_SINGLE_PART_FILE_SIZE_BYTES


def test_upgrade_finishes_with_the_final_catalog_assertion() -> None:
    upgrade_sql = "\n".join(_replay("upgrade").executed_sql)
    assert "application_table_count <> 30" in upgrade_sql
    assert "trigger_function_count <> 4" in upgrade_sql
    assert "protection_trigger_count <> 11" in upgrade_sql


def test_migration_hygiene_rules_hold() -> None:
    source = _migration_source()
    lowered = source.lower()
    assert "personal_os" not in source
    assert "gen_random_uuid" not in lowered
    assert "uuid_generate" not in lowered
    assert "jsonb" not in lowered
    assert "create extension" not in lowered
    assert "create type" not in lowered
    assert "create_all" not in lowered
    assert "cascade" not in lowered


def test_migration_stores_no_bytes_path_token_receipt_or_provider_key() -> None:
    # The exact column-set pin is the authority: none of the forbidden value
    # classes ever becomes a column. The token is stored only as its one-way
    # hash column and no raw token column exists.
    for forbidden_column_hint in (
        "content_bytes",
        "payload_bytes",
        "locator",
        "object_key",
        "presigned",
        "receipt_url",
        "provider",
        "r2_key",
        "operation_token",
    ):
        assert forbidden_column_hint not in OPERATION_TABLE_COLUMNS, forbidden_column_hint
    assert "operation_token_hash" in OPERATION_TABLE_COLUMNS


# --- downgrade contract -----------------------------------------------------------


def test_downgrade_reads_the_destructive_gate_from_the_x_argument() -> None:
    source = _migration_source()
    assert _DESTRUCTIVE_X_ARGUMENT in source
    assert "cmd_opts" in source
    assert _DOWNGRADE_REFUSAL_MESSAGE in source


def test_downgrade_counts_operation_rows_before_any_destructive_step() -> None:
    recorder = _replay("downgrade")
    bind_executed = recorder.bind.executed
    assert bind_executed, "the downgrade must execute its gate count"
    gate_sql = bind_executed[0]
    assert gate_sql.lstrip().upper().startswith("SELECT")
    assert "knowledge.small_file_upload_operations" in gate_sql


def test_downgrade_returns_exactly_to_the_policy_head() -> None:
    recorder = _replay("downgrade")
    drop_table_events = [
        detail for operation, detail in recorder.events if operation == "drop_table"
    ]
    assert drop_table_events == ["small_file_upload_operations"]

    downgrade_sql = "\n".join(recorder.executed_sql)
    assert "application_table_count <> 29" in downgrade_sql
    assert "trigger_function_count <> 4" in downgrade_sql
    assert "protection_trigger_count <> 11" in downgrade_sql
    assert "DROP SCHEMA" not in downgrade_sql
    assert "knowledge.users" not in downgrade_sql
    assert "knowledge.workspaces" not in downgrade_sql
    assert "knowledge.sources" not in downgrade_sql
