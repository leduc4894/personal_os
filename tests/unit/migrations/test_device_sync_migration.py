"""Static DDL contract for the canonical device-cursor and manifest revision."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260826_01_add_device_sync_reconciliation.py"
)

DEVICE_SYNC_REVISION = "20260826_01"
DOWNLOAD_ENTRY_ECHO_REVISION = "20260826_02"
RUN_CLIENT_ACTIVITY_REVISION = "20260827_01"
MULTIPART_UPLOAD_REVISION = "20260828_01"
SOURCE_LIFECYCLE_REVISION = "20260820_01"

CURSOR_COLUMNS = frozenset(
    {
        "device_cursor_id",
        "workspace_id",
        "device_id",
        "acknowledged_sequence",
        "delivered_through_sequence",
        "created_at",
        "updated_at",
    }
)

RUN_COLUMNS = frozenset(
    {
        "manifest_run_id",
        "workspace_id",
        "device_id",
        "base_acknowledged_sequence",
        "checkpoint_sequence",
        "policy_revision_number",
        "client_observation_generation",
        "state",
        "next_page_number",
        "entry_count",
        "final_digest",
        "safe_error_code",
        "created_at",
        "expires_at",
        "planned_at",
        "completed_at",
    }
)

PAGE_COLUMNS = frozenset(
    {
        "manifest_run_id",
        "page_number",
        "entry_count",
        "page_digest",
        "received_at",
    }
)

RESOLUTION_COLUMNS = frozenset(
    {
        "manifest_run_id",
        "page_number",
        "entry_index",
        "local_entry_id",
        "known_source_id",
        "known_version_id",
        "submitted_sha256",
        "submitted_size_bytes",
        "submitted_media_type",
        "locator_evidence_digest",
        "resolved_source_id",
        "resolved_source_version_id",
        "resolved_source_locator_id",
        "resolved_source_tombstone_id",
        "match_kind",
    }
)

ACTION_COLUMNS = frozenset(
    {
        "manifest_run_id",
        "action_index",
        "action_kind",
        "local_entry_id",
        "source_id",
        "source_version_id",
        "source_locator_id",
        "source_tombstone_id",
        "safe_reason_code",
    }
)

DEVICE_SYNC_TABLE_COLUMNS = {
    "device_cursors": CURSOR_COLUMNS,
    "manifest_runs": RUN_COLUMNS,
    "manifest_pages": PAGE_COLUMNS,
    "manifest_entry_resolutions": RESOLUTION_COLUMNS,
    "manifest_actions": ACTION_COLUMNS,
}


class _Result:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def scalar_one(self) -> int:
        return self._count


class _Bind:
    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.executed: list[str] = []

    def execute(self, statement: object, *args: Any) -> _Result:
        self.executed.append(str(statement))
        return _Result(self.count)


class _Op:
    def __init__(self, *, protected_rows: int = 0, x_arguments: list[str] | None = None) -> None:
        self.events: list[tuple[str, str]] = []
        self.tables: dict[str, Any] = {}
        self.indexes: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self.checks: dict[str, str] = {}
        self.adds: dict[tuple[str, str], Any] = {}
        self.alterations: list[tuple[str, str, Any]] = []
        self.drops: list[tuple[str, str]] = []
        self.executes: list[str] = []
        self.bind = _Bind(protected_rows)
        self.context = SimpleNamespace(
            config=SimpleNamespace(cmd_opts=SimpleNamespace(x=x_arguments or []))
        )

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.events.append(("create_table", name))
        table = sa.Table(name, sa.MetaData(), *args, schema=kwargs.get("schema"))
        self.tables[name] = table
        return table

    def create_index(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("create_index", name))
        self.indexes[name] = (args, kwargs)

    def drop_index(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("drop_index", name))

    def drop_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("drop_table", name))

    def drop_constraint(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("drop_constraint", name))

    def create_check_constraint(
        self, name: str, table_name: str, condition: str, **kwargs: Any
    ) -> None:
        self.events.append(("create_check_constraint", name))
        self.checks[name] = condition

    def execute(self, statement: Any, **kwargs: Any) -> None:
        self.events.append(("execute", str(statement)))
        self.executes.append(" ".join(str(statement).split()))

    def add_column(self, table_name: str, column: Any, **kwargs: Any) -> None:
        self.events.append(("add_column", table_name))
        self.adds[(table_name, column.name)] = column

    def alter_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        self.events.append(("alter_column", table_name))
        self.alterations.append((table_name, column_name, kwargs.get("nullable")))

    def drop_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        self.events.append(("drop_column", table_name))
        self.drops.append((table_name, column_name))

    def get_bind(self) -> _Bind:
        return self.bind

    def get_context(self) -> Any:
        return self.context


def load_revision(module_file_name: str) -> Any:
    migration_path = REPO_ROOT / "migrations" / "versions" / module_file_name
    assert migration_path.is_file(), f"missing device sync migration: {migration_path.name}"
    spec = importlib.util.spec_from_file_location("device_sync_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str, **kwargs: Any) -> _Op:
    revision = load_revision("20260826_01_add_device_sync_reconciliation.py")
    recorder = _Op(**kwargs)
    revision.op = recorder
    getattr(revision, function_name)()
    return recorder


def _named_constraints(table: sa.Table) -> dict[str, Any]:
    return {
        constraint.name: constraint
        for constraint in table.constraints
        if constraint.name is not None
    }


def _foreign_key_contracts(recorder: _Op) -> dict[str, tuple[Any, ...]]:
    """Map every named foreign key to its columns, targets and delete rule."""

    contracts: dict[str, tuple[Any, ...]] = {}
    for table in recorder.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, sa.ForeignKeyConstraint):
                continue
            assert constraint.name is not None
            contracts[constraint.name] = (
                tuple(constraint.column_keys),
                tuple(element.target_fullname for element in constraint.elements),
                constraint.ondelete,
            )
    return contracts


# --- revision chain -----------------------------------------------------------


def test_device_sync_revision_extends_source_lifecycle_head() -> None:
    module = load_revision("20260826_01_add_device_sync_reconciliation.py")
    assert module.revision == "20260826_01"
    assert module.down_revision == "20260820_01"


def test_device_sync_revision_is_the_single_alembic_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))
    # The multipart upload, operation-size-bound and deferred-identity
    # revisions stack on the run client-activity revision, so the single graph
    # head moved past it.
    assert scripts.get_heads() == ["20260828_03"]
    revision = scripts.get_revision(RUN_CLIENT_ACTIVITY_REVISION)
    assert revision is not None
    assert revision.down_revision == DOWNLOAD_ENTRY_ECHO_REVISION


def test_run_client_activity_revision_pins_its_chain_link() -> None:
    module = load_revision("20260827_01_add_manifest_run_client_activity.py")
    assert module.revision == "20260827_01"
    assert module.down_revision == "20260826_02"


def test_run_client_activity_upgrade_adds_the_backfilled_not_null_column() -> None:
    module = load_revision("20260827_01_add_manifest_run_client_activity.py")
    recorder = _Op()
    module.op = recorder
    module.upgrade()
    added = recorder.adds[("manifest_runs", "last_client_activity_at")]
    assert added.nullable is True
    # The backfill copies each run's only known activity point before the
    # NOT NULL clamp, so idle-expiry never sees a synthetic zero anchor.
    assert recorder.executes == [
        "UPDATE knowledge.manifest_runs "
        "SET last_client_activity_at = created_at "
        "WHERE last_client_activity_at IS NULL"
    ]
    assert recorder.alterations == [("manifest_runs", "last_client_activity_at", False)]


def test_run_client_activity_downgrade_drops_the_column() -> None:
    module = load_revision("20260827_01_add_manifest_run_client_activity.py")
    recorder = _Op()
    module.op = recorder
    module.downgrade()
    assert recorder.drops == [("manifest_runs", "last_client_activity_at")]


def test_download_entry_echo_revision_pins_its_chain_link() -> None:
    module = load_revision("20260826_02_allow_manifest_download_entry_echo.py")
    assert module.revision == "20260826_02"
    assert module.down_revision == "20260826_01"


def test_download_entry_echo_upgrade_rewrites_only_the_action_shape() -> None:
    module = load_revision("20260826_02_allow_manifest_download_entry_echo.py")
    recorder = _Op()
    module.op = recorder
    module.upgrade()
    assert recorder.events == [
        ("drop_constraint", "ck_manifest_actions__shape"),
        ("create_check_constraint", "ck_manifest_actions__shape"),
    ]
    assert recorder.checks["ck_manifest_actions__shape"] == (
        "(local_entry_id IS NOT NULL OR action_kind = 'download') "
        "AND (action_kind IN ('conflict', 'excluded')) = (safe_reason_code IS NOT NULL) "
        "AND (action_kind NOT IN ('download', 'no_change') "
        "OR (source_id IS NOT NULL AND source_version_id IS NOT NULL)) "
        "AND (action_kind <> 'apply_tombstone' OR source_tombstone_id IS NOT NULL) "
        "AND (action_kind <> 'apply_tombstone' OR source_id IS NOT NULL) "
        "AND (source_version_id IS NULL OR source_id IS NOT NULL) "
        "AND (source_locator_id IS NULL OR source_id IS NOT NULL) "
        "AND (source_tombstone_id IS NULL OR source_id IS NOT NULL)"
    )


def test_download_entry_echo_downgrade_restores_the_strict_shape_under_the_gate() -> None:
    module = load_revision("20260826_02_allow_manifest_download_entry_echo.py")
    # No echoed rows: the strict shape returns with no destructive delete.
    quiet = _Op()
    module.op = quiet
    module.downgrade()
    assert quiet.events == [
        ("drop_constraint", "ck_manifest_actions__shape"),
        ("create_check_constraint", "ck_manifest_actions__shape"),
    ]
    assert quiet.checks["ck_manifest_actions__shape"] == (
        "(action_kind = 'download') = (local_entry_id IS NULL) "
        "AND (action_kind IN ('conflict', 'excluded')) = (safe_reason_code IS NOT NULL) "
        "AND (action_kind NOT IN ('download', 'no_change') "
        "OR (source_id IS NOT NULL AND source_version_id IS NOT NULL)) "
        "AND (action_kind <> 'apply_tombstone' OR source_tombstone_id IS NOT NULL) "
        "AND (action_kind <> 'apply_tombstone' OR source_id IS NOT NULL) "
        "AND (source_version_id IS NULL OR source_id IS NOT NULL) "
        "AND (source_locator_id IS NULL OR source_id IS NOT NULL) "
        "AND (source_tombstone_id IS NULL OR source_id IS NOT NULL)"
    )
    # Echoed rows without the explicit gate refuse the downgrade outright.
    gated = _Op(protected_rows=1)
    module.op = gated
    with pytest.raises(RuntimeError, match="explicit_gate"):
        module.downgrade()
    assert gated.events == []
    # The explicit destructive gate discards the echoed rows first.
    destructive = _Op(protected_rows=1, x_arguments=["allow_destructive=true"])
    module.op = destructive
    module.downgrade()
    assert destructive.events == [
        (
            "execute",
            "DELETE FROM knowledge.manifest_actions"
            " WHERE action_kind = 'download' AND local_entry_id IS NOT NULL",
        ),
        ("drop_constraint", "ck_manifest_actions__shape"),
        ("create_check_constraint", "ck_manifest_actions__shape"),
    ]


def test_amended_action_shape_admits_the_catch_up_echo_and_rejects_entry_less_kinds() -> None:
    """The amended shape truth table, evaluated like the DDL will enforce it.

    A download may carry its manifest entry (the Task 11b catch-up echo) or
    none (the canonical-only download); every other kind still requires its
    entry. The constraint SQL itself is evaluated row by row against a
    throwaway in-memory SQLite database.
    """

    module = load_revision("20260826_02_allow_manifest_download_entry_echo.py")
    shape_sql = module._AMENDED_ACTION_SHAPE_CHECK
    shape_query = sa.text(
        f"SELECT ({shape_sql}) FROM (SELECT :action_kind AS action_kind,"
        " :local_entry_id AS local_entry_id, :safe_reason_code AS safe_reason_code,"
        " :source_id AS source_id, :source_version_id AS source_version_id,"
        " :source_locator_id AS source_locator_id,"
        " :source_tombstone_id AS source_tombstone_id)"
    )
    engine = sa.create_engine("sqlite://")

    def _evaluates_true(action_kind: str, local_entry_id: str | None) -> bool:
        # Every operand outside the entry clause is shaped honestly for the
        # kind, so each verdict below isolates the entry clause alone.
        with engine.connect() as connection:
            return bool(
                connection.execute(
                    shape_query,
                    {
                        "action_kind": action_kind,
                        "local_entry_id": local_entry_id,
                        "safe_reason_code": (
                            "device_manifest_policy_excluded"
                            if action_kind in ("conflict", "excluded")
                            else None
                        ),
                        "source_id": (
                            None
                            if action_kind in ("conflict", "excluded")
                            else "4960b7a8-283b-4d92-8186-ca0d8f894f8c"
                        ),
                        "source_locator_id": None,
                        "source_version_id": (
                            None
                            if action_kind in ("conflict", "excluded", "apply_tombstone")
                            else "f88d9045-28d1-41ba-90b2-503ec1f4984d"
                        ),
                        "source_tombstone_id": (
                            "018f47a0-7b00-7000-8000-000000000009"
                            if action_kind == "apply_tombstone"
                            else None
                        ),
                    },
                ).scalar_one()
            )

    # Every honest entry shape must satisfy the constraint.
    assert _evaluates_true("download", None)  # canonical-only download
    assert _evaluates_true("download", "entry-stale")  # catch-up echo (task 11b)
    assert _evaluates_true("upload", "entry-upload")
    assert _evaluates_true("no_change", "entry-match")
    assert _evaluates_true("conflict", "entry-diverged")
    assert _evaluates_true("apply_tombstone", "entry-gone")
    # Every entry-less non-download kind must violate it.
    assert not _evaluates_true("upload", None)
    assert not _evaluates_true("no_change", None)
    assert not _evaluates_true("apply_tombstone", None)
    assert not _evaluates_true("conflict", None)
    engine.dispose()


# --- upgrade shape ------------------------------------------------------------


def test_upgrade_creates_the_five_device_sync_tables() -> None:
    recorder = _replay("upgrade")
    assert [detail for operation, detail in recorder.events if operation == "create_table"] == [
        "device_cursors",
        "manifest_runs",
        "manifest_pages",
        "manifest_entry_resolutions",
        "manifest_actions",
    ]
    for table_name, expected_columns in DEVICE_SYNC_TABLE_COLUMNS.items():
        assert set(recorder.tables[table_name].columns.keys()) == expected_columns, table_name


def test_upgrade_declares_the_exact_cursor_checks_and_unique_key() -> None:
    recorder = _replay("upgrade")
    cursor_table = recorder.tables["device_cursors"]
    constraints = _named_constraints(cursor_table)
    assert str(constraints["ck_device_cursors_delivery"].sqltext) == (
        "delivered_through_sequence >= acknowledged_sequence"
    )
    assert str(constraints["ck_device_cursors__acknowledged"].sqltext) == (
        "acknowledged_sequence >= 0"
    )
    unique = constraints["uq_device_cursors_workspace_device"]
    assert isinstance(unique, sa.UniqueConstraint)
    assert tuple(unique.columns.keys()) == ("workspace_id", "device_id")


def test_upgrade_restricts_cursor_foreign_keys_to_workspace_and_device() -> None:
    recorder = _replay("upgrade")
    assert _foreign_key_contracts(recorder) == {
        "fk_device_cursors__workspace": (
            ("workspace_id",),
            ("knowledge.workspaces.workspace_id",),
            "RESTRICT",
        ),
        "fk_device_cursors__device": (
            ("workspace_id", "device_id"),
            ("knowledge.devices.workspace_id", "knowledge.devices.device_id"),
            "RESTRICT",
        ),
        "fk_manifest_runs__workspace": (
            ("workspace_id",),
            ("knowledge.workspaces.workspace_id",),
            "RESTRICT",
        ),
        "fk_manifest_runs__device": (
            ("workspace_id", "device_id"),
            ("knowledge.devices.workspace_id", "knowledge.devices.device_id"),
            "RESTRICT",
        ),
        "fk_manifest_pages__run": (
            ("manifest_run_id",),
            ("knowledge.manifest_runs.manifest_run_id",),
            "CASCADE",
        ),
        "fk_manifest_entry_resolutions__run": (
            ("manifest_run_id",),
            ("knowledge.manifest_runs.manifest_run_id",),
            "CASCADE",
        ),
        "fk_manifest_actions__run": (
            ("manifest_run_id",),
            ("knowledge.manifest_runs.manifest_run_id",),
            "CASCADE",
        ),
    }


def test_upgrade_pins_the_manifest_run_state_vocabulary_and_bounds() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["manifest_runs"])
    assert str(constraints["ck_manifest_runs__state"].sqltext) == (
        "state IN ('collecting', 'planned', 'applying', 'completed', 'expired', 'failed')"
    )
    assert str(constraints["ck_manifest_runs__sequences"].sqltext) == (
        "base_acknowledged_sequence >= 0 AND checkpoint_sequence >= base_acknowledged_sequence"
    )
    assert str(constraints["ck_manifest_runs__policy_revision"].sqltext) == (
        "policy_revision_number >= 1"
    )
    assert str(constraints["ck_manifest_runs__entry_count"].sqltext) == (
        "entry_count BETWEEN 0 AND 100000"
    )
    assert str(constraints["ck_manifest_runs__page_number"].sqltext) == "next_page_number >= 0"
    assert (
        str(constraints["ck_manifest_runs__final_digest"].sqltext)
        == "final_digest IS NULL OR final_digest ~ '^[0-9a-f]{64}$'"
    )
    assert str(constraints["ck_manifest_runs__state_shape"].sqltext) == (
        "(final_digest IS NULL) = (planned_at IS NULL) "
        "AND (state = 'completed') = (completed_at IS NOT NULL) "
        "AND (state = 'failed') = (safe_error_code IS NOT NULL) "
        "AND (state <> 'collecting' "
        "OR (final_digest IS NULL AND completed_at IS NULL AND safe_error_code IS NULL)) "
        "AND (state NOT IN ('planned', 'applying', 'completed') "
        "OR (final_digest IS NOT NULL AND safe_error_code IS NULL))"
    )
    assert str(constraints["ck_manifest_runs__lifetime"].sqltext) == "expires_at > created_at"


def test_state_shape_admits_every_honest_terminal_evidence_combination() -> None:
    """Failed and expired runs stay writable in every honest shape.

    The terminal shape must admit a run failing during collection (no digest
    yet), a run failing or expiring after planning (digest and planning time
    retained as evidence), and an expired run that never reached planning —
    while rejecting every dishonest combination (a completion time or an
    error code on a state that does not own it, half-present finalized
    evidence, or a collecting run carrying any terminal evidence).  The
    constraint SQL itself is evaluated row by row against a throwaway
    in-memory SQLite database, so the truth table pins exactly what the DDL
    will enforce.
    """

    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["manifest_runs"])
    shape_sql = str(constraints["ck_manifest_runs__state_shape"].sqltext)
    shape_query = sa.text(
        f"SELECT ({shape_sql}) FROM (SELECT :state AS state, :final_digest AS final_digest,"
        " :planned_at AS planned_at, :completed_at AS completed_at,"
        " :safe_error_code AS safe_error_code)"
    )
    engine = sa.create_engine("sqlite://")

    def _evaluates_true(
        state: str,
        final_digest: str | None,
        planned_at: str | None,
        completed_at: str | None,
        safe_error_code: str | None,
    ) -> bool:
        with engine.connect() as connection:
            return bool(
                connection.execute(
                    shape_query,
                    {
                        "state": state,
                        "final_digest": final_digest,
                        "planned_at": planned_at,
                        "completed_at": completed_at,
                        "safe_error_code": safe_error_code,
                    },
                ).scalar_one()
            )

    digest = "a" * 64
    timestamp = "2026-08-26 12:00:00+00:00"
    error_token = "device_manifest_replay_mismatch"
    # Every honest shape must satisfy the constraint.
    for state, has_digest, has_error, has_completed in (
        ("collecting", False, None, False),
        ("planned", True, None, False),
        ("applying", True, None, False),
        ("completed", True, None, True),
        ("failed", False, error_token, False),
        ("failed", True, error_token, False),
        ("expired", False, None, False),
        ("expired", True, None, False),
    ):
        assert _evaluates_true(
            state,
            digest if has_digest else None,
            timestamp if has_digest else None,
            timestamp if has_completed else None,
            has_error,
        ), (state, has_digest, has_error, has_completed)
    # Every dishonest shape must violate it.
    for state, has_digest, has_error, has_completed in (
        ("collecting", True, None, False),
        ("collecting", False, error_token, False),
        ("planned", False, None, False),
        ("planned", True, error_token, False),
        ("completed", True, None, False),
        ("completed", False, None, True),
        ("failed", False, None, False),
        ("expired", False, "device_manifest_run_expired", False),
        ("expired", True, None, True),
    ):
        assert not _evaluates_true(
            state,
            digest if has_digest else None,
            timestamp if has_digest else None,
            timestamp if has_completed else None,
            has_error,
        ), (state, has_digest, has_error, has_completed)
    engine.dispose()


def test_upgrade_enforces_one_unfinished_manifest_run_per_device() -> None:
    recorder = _replay("upgrade")
    arguments, keyword_arguments = recorder.indexes["uq_manifest_runs_unfinished_device"]
    assert arguments == ("manifest_runs", ["workspace_id", "device_id"])
    assert keyword_arguments["unique"] is True
    assert keyword_arguments["schema"] == "knowledge"
    assert str(keyword_arguments["postgresql_where"]) == (
        "state in ('collecting', 'planned', 'applying')"
    )


def test_upgrade_pins_the_manifest_page_primary_key_and_bounds() -> None:
    recorder = _replay("upgrade")
    page_table = recorder.tables["manifest_pages"]
    constraints = _named_constraints(page_table)
    primary_key = constraints["pk_manifest_pages"]
    assert isinstance(primary_key, sa.PrimaryKeyConstraint)
    assert tuple(primary_key.columns.keys()) == ("manifest_run_id", "page_number")
    assert str(constraints["ck_manifest_pages__entry_count"].sqltext) == (
        "entry_count BETWEEN 0 AND 500"
    )
    assert str(constraints["ck_manifest_pages__page_number"].sqltext) == "page_number >= 0"
    assert (
        str(constraints["ck_manifest_pages__page_digest"].sqltext)
        == "page_digest ~ '^[0-9a-f]{64}$'"
    )


def test_upgrade_pins_the_entry_resolution_key_and_match_vocabulary() -> None:
    recorder = _replay("upgrade")
    resolution_table = recorder.tables["manifest_entry_resolutions"]
    constraints = _named_constraints(resolution_table)
    primary_key = constraints["pk_manifest_entry_resolutions"]
    assert isinstance(primary_key, sa.PrimaryKeyConstraint)
    assert tuple(primary_key.columns.keys()) == (
        "manifest_run_id",
        "page_number",
        "entry_index",
    )
    assert str(constraints["ck_manifest_entry_resolutions__match_kind"].sqltext) == (
        "match_kind IN ('current_locator', 'historical_locator_fingerprint', "
        "'open_tombstone_fingerprint', 'unproven')"
    )
    assert str(constraints["ck_manifest_entry_resolutions__identity_shape"].sqltext) == (
        "(match_kind = 'unproven') = "
        "(resolved_source_id IS NULL AND resolved_source_version_id IS NULL "
        "AND resolved_source_locator_id IS NULL AND resolved_source_tombstone_id IS NULL) "
        "AND (resolved_source_id IS NULL) = (resolved_source_version_id IS NULL)"
    )


def test_upgrade_pins_the_action_key_and_action_shape_checks() -> None:
    recorder = _replay("upgrade")
    action_table = recorder.tables["manifest_actions"]
    constraints = _named_constraints(action_table)
    primary_key = constraints["pk_manifest_actions"]
    assert isinstance(primary_key, sa.PrimaryKeyConstraint)
    assert tuple(primary_key.columns.keys()) == ("manifest_run_id", "action_index")
    assert str(constraints["ck_manifest_actions__action_kind"].sqltext) == (
        "action_kind IN ('upload', 'download', 'apply_tombstone', 'conflict', "
        "'no_change', 'excluded')"
    )
    assert str(constraints["ck_manifest_actions__shape"].sqltext) == (
        "(action_kind = 'download') = (local_entry_id IS NULL) "
        "AND (action_kind IN ('conflict', 'excluded')) = (safe_reason_code IS NOT NULL) "
        "AND (action_kind NOT IN ('download', 'no_change') "
        "OR (source_id IS NOT NULL AND source_version_id IS NOT NULL)) "
        "AND (action_kind <> 'apply_tombstone' OR source_tombstone_id IS NOT NULL) "
        "AND (action_kind <> 'apply_tombstone' OR source_id IS NOT NULL) "
        "AND (source_version_id IS NULL OR source_id IS NOT NULL) "
        "AND (source_locator_id IS NULL OR source_id IS NOT NULL) "
        "AND (source_tombstone_id IS NULL OR source_id IS NOT NULL)"
    )


def test_upgrade_uses_database_time_for_run_and_cursor_timestamps() -> None:
    recorder = _replay("upgrade")
    server_defaulted: set[str] = set()
    for table_name in DEVICE_SYNC_TABLE_COLUMNS:
        for column in recorder.tables[table_name].columns:
            if column.server_default is not None:
                server_defaulted.add(f"{table_name}.{column.name}")
    assert {
        "device_cursors.created_at",
        "device_cursors.updated_at",
        "manifest_runs.created_at",
        "manifest_runs.expires_at",
        "manifest_pages.received_at",
    } <= server_defaulted
    for qualified_name in (
        "device_cursors.created_at",
        "device_cursors.updated_at",
        "manifest_runs.created_at",
        "manifest_runs.expires_at",
        "manifest_pages.received_at",
    ):
        table_name, column_name = qualified_name.split(".")
        column = recorder.tables[table_name].columns[column_name]
        assert column.server_default is not None
        assert column.server_default.arg.text in (
            "CURRENT_TIMESTAMP",
            "CURRENT_TIMESTAMP + interval '1 hour'",
        ), qualified_name


def test_upgrade_asserts_the_final_catalog_table_count() -> None:
    recorder = _replay("upgrade")
    executed = [detail for operation, detail in recorder.events if operation == "execute"]
    assert any(
        "application_table_count <> 37" in statement
        and "device_sync_schema_table_count_invalid" in statement
        for statement in executed
    )


# --- downgrade gate -----------------------------------------------------------


def test_downgrade_refuses_to_destroy_device_sync_state_without_explicit_gate() -> None:
    with pytest.raises(RuntimeError, match="device_sync_downgrade_requires_explicit_gate"):
        _replay("downgrade", protected_rows=1)


def test_downgrade_drops_the_five_tables_after_the_explicit_gate() -> None:
    recorder = _replay("downgrade", protected_rows=1, x_arguments=["allow_destructive=true"])
    assert [detail for operation, detail in recorder.events if operation == "drop_table"] == [
        "manifest_actions",
        "manifest_entry_resolutions",
        "manifest_pages",
        "manifest_runs",
        "device_cursors",
    ]
    executed = [detail for operation, detail in recorder.events if operation == "execute"]
    assert any(
        "application_table_count <> 32" in statement
        and "device_sync_downgrade_table_count_invalid" in statement
        for statement in executed
    )
