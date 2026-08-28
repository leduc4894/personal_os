"""Static upgrade/downgrade contract for the multipart upload session revision.

These tests replay the ``20260828_01`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the manifest client-activity head,
the closed column sets of ``multipart_uploads`` and ``multipart_parts`` (the
staging key, provider upload ID and provider ETag are private text columns
and no presigned URL, URL fragment or signature ever becomes a column), the
per-session part-number uniqueness, the one-session-per-operation ownership
uniqueness, the owner/status, expiry-sweep and cleanup-claim indexes, the
geometry bound equality with the domain constants of
``personal_os.multipart_upload.contracts`` and the gated destructive
downgrade whose drop order is indexes, parts, then sessions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from personal_os.multipart_upload.contracts import (
    MAX_MULTIPART_PART_COUNT,
    MULTIPART_PART_SIZE_BYTES,
)
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    MAX_UPLOAD_FILE_SIZE_BYTES,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
MIGRATION_FILE_NAME: str = "20260828_01_add_multipart_upload_sessions.py"
MIGRATION_PATH: Path = REPO_ROOT / "migrations" / "versions" / MIGRATION_FILE_NAME

MULTIPART_REVISION: str = "20260828_01"
MANIFEST_CLIENT_ACTIVITY_REVISION: str = "20260827_01"

#: The exact closed column set of the session table. Anything else — a byte
#: payload, a presigned URL or URL fragment, a signature, a receipt or an
#: object key — must fail this pin. The staging key, provider upload ID and
#: provider ETag columns are the deliberate private provider identity.
UPLOAD_TABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "multipart_upload_id",
        "session_id",
        "workspace_id",
        "device_id",
        "operation_id",
        "declared_sha256",
        "declared_size_bytes",
        "declared_media_type",
        "base_version_id",
        "policy_revision_number",
        "part_size_bytes",
        "part_count",
        "staging_key",
        "provider_upload_id",
        "state",
        "claim_token",
        "claim_expires_at",
        "result_kind",
        "result_source_id",
        "result_source_version_id",
        "result_content_version",
        "result_committed_at",
        "cleanup_state",
        "cleanup_attempt_count",
        "cleanup_next_retry_at",
        "cleanup_reason_code",
        "expires_at",
        "created_at",
        "updated_at",
    }
)

#: The exact closed column set of the completed-part evidence table.
PART_TABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "multipart_part_id",
        "multipart_upload_id",
        "part_number",
        "offset_bytes",
        "size_bytes",
        "provider_etag",
        "verified_size_bytes",
        "completed_at",
    }
)

_DOWNGRADE_REFUSAL_MESSAGE: str = "multipart_downgrade_requires_explicit_gate"


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
    """Stub of ``alembic.op`` recording every operation as an ordered event."""

    def __init__(self, *, protected_rows: int = 0, x_arguments: list[str] | None = None) -> None:
        self.events: list[tuple[str, str]] = []
        self.tables: dict[str, Any] = {}
        self.indexes: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
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

    def execute(self, statement: Any, **kwargs: Any) -> None:
        self.events.append(("execute", str(statement)))
        self.executes.append(" ".join(str(statement).split()))

    def get_bind(self) -> _Bind:
        return self.bind

    def get_context(self) -> Any:
        return self.context


def load_revision() -> Any:
    migration_path = REPO_ROOT / "migrations" / "versions" / MIGRATION_FILE_NAME
    assert migration_path.is_file(), f"missing multipart upload migration: {migration_path.name}"
    spec = importlib.util.spec_from_file_location("multipart_upload_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str, **kwargs: Any) -> _Op:
    revision = load_revision()
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


def inspect_schema(table_name: str) -> set[str]:
    """Return the exact column names the upgrade DDL declares for a table."""

    recorder = _replay("upgrade")
    return set(recorder.tables[table_name].columns.keys())


def unique_constraint(table_name: str) -> tuple[str, ...]:
    """Return the columns of every unique constraint, in declaration order."""

    recorder = _replay("upgrade")
    columns: list[str] = []
    for constraint in recorder.tables[table_name].constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            columns.extend(constraint.columns.keys())
    return tuple(columns)


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


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


# --- revision chain -----------------------------------------------------------


def test_multipart_revision_extends_the_manifest_client_activity_head() -> None:
    module = load_revision()
    assert module.revision == "20260828_01"
    assert module.down_revision == "20260827_01"


def test_multipart_revision_is_chained_below_the_operation_size_bound_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))
    assert scripts.get_heads() == ["20260828_03"]
    revision = scripts.get_revision(MULTIPART_REVISION)
    assert revision is not None
    assert revision.down_revision == MANIFEST_CLIENT_ACTIVITY_REVISION
    assert not revision.branch_labels
    assert revision.dependencies is None


# --- upgrade contract ---------------------------------------------------------


def test_upgrade_creates_exactly_the_session_and_part_tables() -> None:
    recorder = _replay("upgrade")
    create_table_events = [
        detail for operation, detail in recorder.events if operation == "create_table"
    ]
    assert create_table_events == ["multipart_uploads", "multipart_parts"]


def test_upgrade_column_sets_are_the_closed_session_and_part_records() -> None:
    recorder = _replay("upgrade")
    assert set(recorder.tables["multipart_uploads"].columns.keys()) == UPLOAD_TABLE_COLUMNS
    assert set(recorder.tables["multipart_parts"].columns.keys()) == PART_TABLE_COLUMNS


def test_upgrade_creates_only_private_provider_columns() -> None:
    columns = inspect_schema("multipart_uploads")
    assert {
        "workspace_id",
        "device_id",
        "operation_id",
        "staging_key",
        "provider_upload_id",
    } <= columns
    assert "presigned_url" not in columns


def test_migration_stores_no_url_signature_receipt_or_provider_bytes() -> None:
    for forbidden_column_hint in (
        "content_bytes",
        "payload_bytes",
        "locator",
        "object_key",
        "presigned",
        "url",
        "signature",
        "receipt",
        "r2_key",
        "query",
    ):
        assert forbidden_column_hint not in UPLOAD_TABLE_COLUMNS, forbidden_column_hint
        assert forbidden_column_hint not in PART_TABLE_COLUMNS, forbidden_column_hint
    assert "staging_key" in UPLOAD_TABLE_COLUMNS
    assert "provider_upload_id" in UPLOAD_TABLE_COLUMNS
    assert "provider_etag" in PART_TABLE_COLUMNS


def test_part_number_is_unique_per_session() -> None:
    assert unique_constraint("multipart_parts") == ("multipart_upload_id", "part_number")


def test_session_ownership_is_unique_per_operation() -> None:
    """One frozen small-file operation owns at most one session, ever.

    The plain uniqueness (not a partial active-state index) is the replay
    guarantee itself: exact replay of a terminal session must find its frozen
    row, and no second session may ever recreate provider work for the same
    frozen operation — so ``cleanup_pending`` is never permission to reuse.
    """

    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["multipart_uploads"])
    ownership = constraints["uq_multipart_uploads__operation"]
    assert isinstance(ownership, sa.UniqueConstraint)
    assert tuple(ownership.columns.keys()) == ("operation_id",)


def test_public_session_id_is_unique() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["multipart_uploads"])
    session_identity = constraints["uq_multipart_uploads__session_id"]
    assert isinstance(session_identity, sa.UniqueConstraint)
    assert tuple(session_identity.columns.keys()) == ("session_id",)


def test_upgrade_pins_the_closed_session_state_vocabulary() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["multipart_uploads"])
    assert str(constraints["ck_multipart_uploads__state"].sqltext) == (
        "state IN ('created', 'uploading', 'completing', 'verifying', 'promoting', "
        "'committed', 'cancelling', 'expired', 'integrity_failed', 'policy_denied', "
        "'cleanup_pending', 'cleaned')"
    )


def test_upgrade_pins_the_session_id_grammar_checks() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["multipart_uploads"])
    assert str(constraints["ck_multipart_uploads__session_id"].sqltext) == (
        "session_id ~ '^[A-Za-z0-9_-]{32,128}$' "
        "AND session_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        "[0-9a-f]{4}-[0-9a-f]{12}$'"
    )


def test_upgrade_pins_the_part_geometry_checks() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["multipart_uploads"])
    assert str(constraints["ck_multipart_uploads__part_geometry"].sqltext) == (
        "part_size_bytes = 8388608 "
        "AND part_count BETWEEN 1 AND 13 "
        "AND (part_count - 1) * part_size_bytes < declared_size_bytes "
        "AND declared_size_bytes <= part_count * part_size_bytes"
    )


def test_upgrade_pins_the_terminal_result_shape_checks() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["multipart_uploads"])
    assert str(constraints["ck_multipart_uploads__terminal_shape"].sqltext) == (
        "(state = 'committed') = "
        "(result_kind IS NOT NULL AND result_source_id IS NOT NULL "
        "AND result_source_version_id IS NOT NULL AND result_content_version IS NOT NULL "
        "AND result_committed_at IS NOT NULL)"
    )
    assert str(constraints["ck_multipart_uploads__claim_lease"].sqltext) == (
        "(claim_token IS NULL) = (claim_expires_at IS NULL)"
    )


def test_upgrade_pins_the_cleanup_shape_checks() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["multipart_uploads"])
    assert str(constraints["ck_multipart_uploads__cleanup_shape"].sqltext) == (
        "(state = 'cleaned') = (cleanup_state = 'succeeded') "
        "AND (cleanup_state = 'failed') = (cleanup_reason_code IS NOT NULL) "
        "AND (cleanup_state = 'none') = (cleanup_attempt_count = 0) "
        "AND (cleanup_next_retry_at IS NULL) = (cleanup_state IN ('none', 'succeeded'))"
    )


def test_upgrade_pins_the_part_number_and_range_checks() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["multipart_parts"])
    assert str(constraints["ck_multipart_parts__part_number"].sqltext) == (
        "part_number BETWEEN 1 AND 13"
    )
    assert str(constraints["ck_multipart_parts__byte_range"].sqltext) == (
        "offset_bytes = (part_number - 1) * 8388608 "
        "AND size_bytes BETWEEN 1 AND 8388608 "
        "AND verified_size_bytes = size_bytes"
    )


def test_geometry_bounds_equal_the_domain_constants() -> None:
    """The DDL bounds and the multipart domain geometry are one set of limits.

    A revision may not import the domain constants (the hygiene rules forbid
    any ``personal_os`` import), so each geometry bound exists twice in
    Python; this pin fails the suite on any drift between the DDL bounds and
    ``personal_os.multipart_upload.contracts``.
    """

    module = load_revision()
    assert int(module._MULTIPART_PART_SIZE_BYTES) == MULTIPART_PART_SIZE_BYTES
    assert int(module._MAXIMUM_PART_COUNT) == MAX_MULTIPART_PART_COUNT
    assert int(module._MINIMUM_MULTIPART_SIZE_BYTES) == MAX_SINGLE_PART_FILE_SIZE_BYTES
    assert int(module._MAXIMUM_UPLOAD_SIZE_BYTES) == MAX_UPLOAD_FILE_SIZE_BYTES


def test_upgrade_declares_the_containment_foreign_keys() -> None:
    recorder = _replay("upgrade")
    contracts = _foreign_key_contracts(recorder)
    assert contracts["fk_multipart_uploads__workspace"] == (
        ("workspace_id",),
        ("knowledge.workspaces.workspace_id",),
        "RESTRICT",
    )
    assert contracts["fk_multipart_uploads__device"] == (
        ("workspace_id", "device_id"),
        ("knowledge.devices.workspace_id", "knowledge.devices.device_id"),
        "RESTRICT",
    )
    assert contracts["fk_multipart_uploads__operation"] == (
        ("operation_id",),
        ("knowledge.small_file_upload_operations.operation_id",),
        "RESTRICT",
    )
    assert contracts["fk_multipart_parts__session"] == (
        ("multipart_upload_id",),
        ("knowledge.multipart_uploads.multipart_upload_id",),
        "RESTRICT",
    )


def test_upgrade_adds_owner_expiry_and_cleanup_claim_indexes() -> None:
    recorder = _replay("upgrade")
    index_events = [detail for operation, detail in recorder.events if operation == "create_index"]
    assert index_events == [
        "ix_multipart_uploads__workspace_state",
        "ix_multipart_uploads__expiry_sweep",
        "ix_multipart_uploads__cleanup_claim",
    ]

    owner_arguments, owner_keywords = recorder.indexes["ix_multipart_uploads__workspace_state"]
    assert owner_arguments == ("multipart_uploads", ["workspace_id", "state"])
    assert owner_keywords["schema"] == "knowledge"

    expiry_arguments, expiry_keywords = recorder.indexes["ix_multipart_uploads__expiry_sweep"]
    assert expiry_arguments == ("multipart_uploads", ["expires_at"])
    assert str(expiry_keywords["postgresql_where"]) == (
        "state in ('created', 'uploading', 'completing', 'verifying', 'promoting')"
    )

    claim_arguments, claim_keywords = recorder.indexes["ix_multipart_uploads__cleanup_claim"]
    assert claim_arguments == ("multipart_uploads", ["cleanup_next_retry_at"])
    assert str(claim_keywords["postgresql_where"]) == ("cleanup_state in ('pending', 'failed')")


def test_upgrade_finishes_with_the_final_catalog_assertion() -> None:
    recorder = _replay("upgrade")
    executed = [detail for operation, detail in recorder.events if operation == "execute"]
    assert any(
        "application_table_count <> 39" in statement
        and "multipart_schema_table_count_invalid" in statement
        for statement in executed
    )


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


# --- downgrade contract -------------------------------------------------------


def test_downgrade_refuses_to_destroy_multipart_state_without_explicit_gate() -> None:
    with pytest.raises(RuntimeError, match=_DOWNGRADE_REFUSAL_MESSAGE):
        _replay("downgrade", protected_rows=1)


def test_downgrade_drops_indexes_then_parts_then_sessions() -> None:
    recorder = _replay("downgrade", protected_rows=1, x_arguments=["allow_destructive=true"])
    ordered_events = [operation for operation, _detail in recorder.events]
    drop_index_positions = [
        index for index, operation in enumerate(ordered_events) if operation == "drop_index"
    ]
    parts_position = ordered_events.index("drop_table")
    assert ordered_events[parts_position] == "drop_table"
    # Every index drop precedes the first table drop; the part evidence table
    # drops before the session table it references.
    assert ordered_events[drop_index_positions[-1] + 1 : parts_position + 2] == [
        "drop_table",
        "drop_table",
    ]
    table_drops = [detail for operation, detail in recorder.events if operation == "drop_table"]
    assert table_drops == ["multipart_parts", "multipart_uploads"]

    executed = [detail for operation, detail in recorder.events if operation == "execute"]
    assert any(
        "application_table_count <> 37" in statement
        and "multipart_downgrade_table_count_invalid" in statement
        for statement in executed
    )


def test_downgrade_keeps_the_rest_of_the_canonical_schema() -> None:
    recorder = _replay("downgrade", x_arguments=["allow_destructive=true"])
    downgrade_sql = "\n".join(recorder.executes)
    assert "DROP SCHEMA" not in downgrade_sql
    assert "knowledge.users" not in downgrade_sql
    assert "knowledge.workspaces" not in downgrade_sql
    assert "knowledge.sources" not in downgrade_sql
    assert "knowledge.small_file_upload_operations" not in downgrade_sql
