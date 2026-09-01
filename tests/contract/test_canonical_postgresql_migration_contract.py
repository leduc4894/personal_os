"""Static contract tests for the canonical PostgreSQL baseline migration.

These tests are fully static: they inspect the Alembic script graph, the
Alembic configuration file and the parsed source of the baseline revision.
They never start PostgreSQL and never read a secret.
"""

from __future__ import annotations

import ast
import configparser
import os
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
MIGRATIONS_PATH: Path = REPO_ROOT / "migrations"
ENV_PY_PATH: Path = MIGRATIONS_PATH / "env.py"
REVISION_PATH: Path = (
    MIGRATIONS_PATH / "versions" / "20260813_01_create_canonical_postgresql_baseline.py"
)
PYPROJECT_PATH: Path = REPO_ROOT / "pyproject.toml"

BASELINE_REVISION: str = "20260813_01"
AUTHENTICATION_REVISION: str = "20260816_01"
POLICY_REVISION: str = "20260817_01"
SMALL_FILE_REVISION: str = "20260818_01"
SOURCE_LIFECYCLE_REVISION: str = "20260820_01"
DEVICE_SYNC_REVISION: str = "20260826_01"
DOWNLOAD_ENTRY_ECHO_REVISION: str = "20260826_02"
DEVICE_MANIFEST_REVISION: str = "20260827_01"
MULTIPART_SESSION_REVISION: str = "20260828_01"
MULTIPART_SIZE_BOUND_REVISION: str = "20260828_02"
MULTIPART_DEFERRED_IDENTITY_REVISION: str = "20260828_03"
MULTIPART_OPERATION_TOKEN_SEAL_REVISION: str = "20260828_04"
SUBMITTED_POLICY_VERDICT_REVISION: str = "20260829_01"
GRANT_POLL_BUCKET_KIND_REVISION: str = "20260901_01"
DEVICE_SYNC_SCALE_INDEX_REVISION: str = "20260901_02"
TERMINAL_LOCATOR_REMEDIATION_REVISION: str = "20260901_03"
SCHEMA_NAME: str = "knowledge"

EXPECTED_TABLES_IN_CREATION_ORDER: tuple[str, ...] = (
    "users",
    "workspaces",
    "devices",
    "content_objects",
    "sources",
    "source_versions",
    "sync_events",
    "projection_intents",
    "audit_events",
)

EXPECTED_MANIFEST: dict[str, frozenset[str]] = {
    "users": frozenset(
        {
            "pk_users",
            "uq_users__username",
            "ck_users__username_slug",
            "ck_users__display_name",
            "ck_users__status",
            "ck_users__timestamps",
        }
    ),
    "workspaces": frozenset(
        {
            "pk_workspaces",
            "fk_workspaces__owner_user",
            "uq_workspaces__owner_user",
            "uq_workspaces__workspace_key",
            "uq_workspaces__workspace_owner",
            "ck_workspaces__workspace_key_slug",
            "ck_workspaces__display_name",
            "ck_workspaces__status",
            "ck_workspaces__timestamps",
        }
    ),
    "devices": frozenset(
        {
            "pk_devices",
            "uq_devices__workspace_device",
            "fk_devices__workspace_owner",
            "ck_devices__device_name",
            "ck_devices__device_kind",
            "ck_devices__status",
            "ck_devices__last_seen",
            "ck_devices__revocation",
            "ix_devices__workspace_user",
            "ix_devices__workspace_status_registered",
        }
    ),
    "content_objects": frozenset(
        {
            "pk_content_objects",
            "uq_content_objects__content_hash",
            "uq_content_objects__object_key",
            "ck_content_objects__content_hash",
            "ck_content_objects__object_key",
            "ck_content_objects__byte_size",
            "ck_content_objects__media_type",
            "ck_content_objects__verification",
            "trg_content_objects__reject_update",
        }
    ),
    "sources": frozenset(
        {
            "pk_sources",
            "uq_sources__workspace_source",
            "fk_sources__workspace",
            "fk_sources__current_version",
            "ck_sources__source_type",
            "ck_sources__title",
            "ck_sources__sync_state",
            "ck_sources__current_pointer",
            "ck_sources__deletion",
            "ck_sources__timestamps",
            "ix_sources__workspace_state_updated",
        }
    ),
    "source_versions": frozenset(
        {
            "pk_source_versions",
            "uq_source_versions__workspace_source_version",
            "uq_source_versions__source_ordinal",
            "fk_source_versions__source",
            "fk_source_versions__content_object",
            "fk_source_versions__parent",
            "ck_source_versions__content_version",
            "ck_source_versions__parent",
            "ck_source_versions__author",
            "ix_source_versions__content_object",
            "ix_source_versions__parent",
            "trg_source_versions__reject_update",
        }
    ),
    "sync_events": frozenset(
        {
            "pk_sync_events",
            "uq_sync_events__workspace_event",
            "uq_sync_events__source_event",
            "uq_sync_events__event_sequence",
            "uq_sync_events__idempotency_key",
            "fk_sync_events__source",
            "fk_sync_events__device",
            "fk_sync_events__committed_version",
            "fk_sync_events__base_version",
            "ck_sync_events__idempotency_key",
            "ck_sync_events__request_fingerprint",
            "ck_sync_events__event_type",
            "ix_sync_events__source_sequence",
            "ix_sync_events__device",
            "ix_sync_events__committed_version",
            "ix_sync_events__base_version",
            "trg_sync_events__reject_update",
        }
    ),
    "projection_intents": frozenset(
        {
            "pk_projection_intents",
            "uq_projection_intents__workspace_intent",
            "uq_projection_intents__event_kind",
            "fk_projection_intents__event_source",
            "fk_projection_intents__source",
            "fk_projection_intents__source_version",
            "ck_projection_intents__projection_kind",
            "ck_projection_intents__operation",
            "ck_projection_intents__status",
            "ck_projection_intents__attempt_count",
            "ck_projection_intents__timestamps",
            "ck_projection_intents__operation_version",
            "ck_projection_intents__lease",
            "ck_projection_intents__dispatch",
            "ck_projection_intents__terminal_error",
            "ck_projection_intents__error_code",
            "ix_projection_intents__event_source",
            "ix_projection_intents__source_version",
            "ix_projection_intents__pending_dispatch",
            "ix_projection_intents__source_status",
        }
    ),
    "audit_events": frozenset(
        {
            "pk_audit_events",
            "fk_audit_events__workspace",
            "ck_audit_events__actor",
            "ck_audit_events__actor_reference",
            "ck_audit_events__action",
            "ck_audit_events__target_kind",
            "ck_audit_events__trace_id",
            "ck_audit_events__result",
            "ck_audit_events__reason_code",
            "ck_audit_events__safe_diff_hash",
            "ix_audit_events__workspace_occurred",
            "ix_audit_events__target_lineage",
            "ix_audit_events__request",
            "trg_audit_events__reject_mutation",
        }
    ),
}

# The downgrade must remove the known objects in the exact reverse of the
# upgrade dependency order, without any CASCADE, ending in a RESTRICT drop of
# the now-empty application schema.
EXPECTED_DOWNGRADE_EVENTS: tuple[str, ...] = (
    "execute:DROP TRIGGER trg_audit_events__reject_mutation ON knowledge.audit_events",
    "execute:DROP TRIGGER trg_sync_events__reject_update ON knowledge.sync_events",
    "execute:DROP TRIGGER trg_source_versions__reject_update ON knowledge.source_versions",
    "execute:DROP TRIGGER trg_content_objects__reject_update ON knowledge.content_objects",
    "execute:DROP FUNCTION knowledge.reject_audit_mutation",
    "execute:DROP FUNCTION knowledge.reject_immutable_update",
    "drop_table:audit_events",
    "drop_table:projection_intents",
    "drop_table:sync_events",
    "drop_constraint:fk_sources__current_version",
    "drop_table:source_versions",
    "drop_table:sources",
    "drop_table:content_objects",
    "drop_table:devices",
    "drop_table:workspaces",
    "drop_table:users",
    "execute:REVOKE ALL ON SCHEMA knowledge FROM PUBLIC",
    "execute:DROP SCHEMA knowledge RESTRICT",
)

FORBIDDEN_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "boto3",
        "botocore",
        "qdrant_client",
        "neo4j",
        "redis",
        "temporalio",
    }
)


class ColumnContract(NamedTuple):
    """The parsed shape of one ``sa.Column(...)`` call inside a table create."""

    name: str
    type_name: str
    is_nullable: bool | None
    has_server_default: bool
    has_identity: bool


class ForeignKeyContract(NamedTuple):
    """The parsed shape of one foreign key defined in the revision."""

    constraint_name: str
    table: str
    local_columns: tuple[str, ...]
    referent_columns: tuple[str, ...]
    is_deferrable: bool | None
    initially: str | None
    ondelete: str | None
    match_clause: str | None


class TableContract(NamedTuple):
    """The parsed shape of one ``op.create_table(...)`` call."""

    name: str
    schema: str | None
    columns: tuple[ColumnContract, ...]
    constraint_names: tuple[str, ...]
    unique_column_sets: tuple[tuple[str, ...], ...]


class RevisionSource:
    """Parsed view over the handwritten baseline revision module."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.tree = ast.parse(source)
        self.constants: dict[str, str] = {}
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            else:
                continue
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                self.constants[target.id] = value.value

    def function_body(self, name: str) -> ast.FunctionDef:
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"revision is missing function {name!r}")

    def calls(self, function_name: str) -> Iterator[ast.Call]:
        for node in ast.walk(self.tree):
            if _is_op_call(node, function_name):
                yield node

    def ordered_events(self, function_name: str) -> list[tuple[str, str]]:
        """Flatten a migration function into ordered ``(operation, detail)`` events."""

        def visit(node: ast.AST) -> Iterator[tuple[str, str]]:
            for child in ast.iter_child_nodes(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "op"
                ):
                    operation = child.func.attr
                    if operation == "execute":
                        sql = self._execute_sql(child)
                        if sql is not None:
                            yield ("execute", sql)
                    else:
                        arguments = _string_arguments(child)
                        if arguments:
                            yield (operation, arguments[0])
                yield from visit(child)

        return list(visit(self.function_body(function_name)))

    def _execute_sql(self, call: ast.Call) -> str | None:
        argument = call.args[0]
        if (
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Attribute)
            and argument.func.attr == "text"
            and argument.args
        ):
            text_argument = argument.args[0]
            if isinstance(text_argument, ast.Constant):
                return str(text_argument.value)
            if isinstance(text_argument, ast.Name) and text_argument.id in self.constants:
                return self.constants[text_argument.id]
        return None


def _is_op_call(node: ast.AST, function_name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == function_name
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    )


def _is_sa_call(node: ast.AST, function_name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == function_name
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sa"
    )


def _load_revision() -> RevisionSource:
    return RevisionSource(REVISION_PATH.read_text(encoding="utf-8"))


def _resolve_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id == "SCHEMA_NAME":
        return SCHEMA_NAME
    return None


def _string_arguments(node: ast.Call) -> tuple[str, ...]:
    values: list[str] = []
    for argument in node.args:
        resolved = _resolve_name(argument)
        if resolved is not None:
            values.append(resolved)
    return tuple(values)


def _keyword_value(node: ast.Call, keyword_name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == keyword_name:
            return keyword.value
    return None


def _keyword_name(node: ast.Call) -> str | None:
    value = _keyword_value(node, "name")
    if value is None:
        return None
    return _resolve_name(value)


def _constant(value: ast.AST | None) -> object:
    if isinstance(value, ast.Constant):
        return value.value
    return None


def _column_contract(call: ast.Call) -> ColumnContract:
    type_expression = call.args[1] if len(call.args) > 1 else None
    if isinstance(type_expression, ast.Call) and isinstance(
        type_expression.func, (ast.Attribute, ast.Name)
    ):
        type_function = type_expression.func
        type_name = (
            type_function.attr if isinstance(type_function, ast.Attribute) else type_function.id
        )
    else:
        type_name = "<expression>"
    is_nullable = _constant(_keyword_value(call, "nullable"))
    has_identity_argument = any(
        isinstance(argument, ast.Call)
        and isinstance(argument.func, ast.Attribute)
        and argument.func.attr == "Identity"
        for argument in call.args
    )
    return ColumnContract(
        name=_string_arguments(call)[0],
        type_name=type_name,
        is_nullable=is_nullable if isinstance(is_nullable, bool) else None,
        has_server_default=_keyword_value(call, "server_default") is not None,
        has_identity=_keyword_value(call, "identity") is not None or has_identity_argument,
    )


def _table_contracts(revision: RevisionSource) -> dict[str, TableContract]:
    tables: dict[str, TableContract] = {}
    for call in revision.calls("create_table"):
        schema_expression = _keyword_value(call, "schema")
        columns = tuple(
            _column_contract(column) for column in ast.walk(call) if _is_sa_call(column, "Column")
        )
        constraint_names: list[str] = []
        unique_sets: list[tuple[str, ...]] = []
        for candidate in ast.walk(call):
            if not (
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Attribute)
                and isinstance(candidate.func.value, ast.Name)
                and candidate.func.value.id == "sa"
            ):
                continue
            assert isinstance(candidate.func, ast.Attribute)
            constraint_name = _keyword_name(candidate)
            if constraint_name is not None:
                constraint_names.append(constraint_name)
            if candidate.func.attr == "UniqueConstraint":
                unique_sets.append(_string_arguments(candidate))
        table_name = _string_arguments(call)[0]
        tables[table_name] = TableContract(
            name=table_name,
            schema=(_resolve_name(schema_expression) if schema_expression is not None else None),
            columns=columns,
            constraint_names=tuple(constraint_names),
            unique_column_sets=tuple(unique_sets),
        )
    return tables


def _inline_foreign_keys(revision: RevisionSource) -> list[ForeignKeyContract]:
    """Extract ``sa.ForeignKeyConstraint`` calls grouped by their create_table."""
    foreign_keys: list[ForeignKeyContract] = []
    for call in revision.calls("create_table"):
        table_name = _string_arguments(call)[0]
        for candidate in ast.walk(call):
            if not _is_sa_call(candidate, "ForeignKeyConstraint"):
                continue
            assert isinstance(candidate.args[0], ast.List)
            assert isinstance(candidate.args[1], ast.List)
            local_columns = tuple(
                value
                for value in (_resolve_name(item) for item in candidate.args[0].elts)
                if value is not None
            )
            referent_columns = tuple(
                value
                for value in (_resolve_name(item) for item in candidate.args[1].elts)
                if value is not None
            )
            foreign_keys.append(
                ForeignKeyContract(
                    constraint_name=_keyword_name(candidate) or "<unnamed>",
                    table=table_name,
                    local_columns=local_columns,
                    referent_columns=referent_columns,
                    is_deferrable=_constant(_keyword_value(candidate, "deferrable")),
                    initially=_constant(_keyword_value(candidate, "initially")),
                    ondelete=_constant(_keyword_value(candidate, "ondelete")),
                    match_clause=_constant(_keyword_value(candidate, "match")),
                )
            )
    return foreign_keys


def _create_foreign_key_calls(revision: RevisionSource) -> list[ForeignKeyContract]:
    foreign_keys: list[ForeignKeyContract] = []
    for call in revision.calls("create_foreign_key"):
        assert isinstance(call.args[3], ast.List)
        assert isinstance(call.args[4], ast.List)
        foreign_keys.append(
            ForeignKeyContract(
                constraint_name=_string_arguments(call)[0],
                table=_string_arguments(call)[1],
                local_columns=tuple(
                    value
                    for value in (_resolve_name(item) for item in call.args[3].elts)
                    if value is not None
                ),
                referent_columns=tuple(
                    value
                    for value in (_resolve_name(item) for item in call.args[4].elts)
                    if value is not None
                ),
                is_deferrable=_constant(_keyword_value(call, "deferrable")),
                initially=_constant(_keyword_value(call, "initially")),
                ondelete=_constant(_keyword_value(call, "ondelete")),
                match_clause=_constant(_keyword_value(call, "match")),
            )
        )
    return foreign_keys


class IndexContract(NamedTuple):
    """The parsed shape of one ``op.create_index(...)`` call."""

    name: str
    table: str
    columns: tuple[str, ...]


def _index_contracts(revision: RevisionSource) -> dict[str, IndexContract]:
    indexes: dict[str, IndexContract] = {}
    for call in revision.calls("create_index"):
        name = _string_arguments(call)[0]
        columns: list[str] = []
        column_list = call.args[2]
        assert isinstance(column_list, ast.List)
        for item in column_list.elts:
            resolved = _resolve_name(item)
            if resolved is not None:
                columns.append(resolved)
        indexes[name] = IndexContract(
            name=name,
            table=_string_arguments(call)[1],
            columns=tuple(columns),
        )
    return indexes


def _execute_statements(revision: RevisionSource, function_name: str) -> list[str]:
    return [
        sql for operation, sql in revision.ordered_events(function_name) if operation == "execute"
    ]


def _named_identifiers(revision: RevisionSource) -> set[str]:
    identifiers: set[str] = set()
    for node in ast.walk(revision.tree):
        if isinstance(node, ast.Call):
            name = _keyword_name(node)
            if name is not None:
                identifiers.add(name)
    for operation in ("create_index", "create_foreign_key"):
        for call in revision.calls(operation):
            identifiers.add(_string_arguments(call)[0])
    upgrade_sql = "\n".join(_execute_statements(revision, "upgrade"))
    identifiers.update(re.findall(r"CREATE TRIGGER (\w+)", upgrade_sql))
    identifiers.update(re.findall(r"CREATE FUNCTION knowledge\.(\w+)", upgrade_sql))
    return identifiers


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


# ---------------------------------------------------------------------------
# Alembic graph contract (brief step 1a)
# ---------------------------------------------------------------------------


def test_alembic_graph_has_exactly_one_head_revision() -> None:
    script_directory = _script_directory()
    assert script_directory.get_heads() == [TERMINAL_LOCATOR_REMEDIATION_REVISION]


def test_baseline_revision_is_the_single_graph_root() -> None:
    script_directory = _script_directory()
    revisions = list(script_directory.walk_revisions())
    assert len(revisions) == 16
    revision = script_directory.get_revision(BASELINE_REVISION)
    assert revision is not None
    assert revision.down_revision is None
    # Alembic represents absent branch labels as an empty label set.
    assert not revision.branch_labels
    assert revision.dependencies is None
    for revision_id, down_revision_id in (
        (AUTHENTICATION_REVISION, BASELINE_REVISION),
        (POLICY_REVISION, AUTHENTICATION_REVISION),
        (SMALL_FILE_REVISION, POLICY_REVISION),
        (SOURCE_LIFECYCLE_REVISION, SMALL_FILE_REVISION),
        (DEVICE_SYNC_REVISION, SOURCE_LIFECYCLE_REVISION),
        (DOWNLOAD_ENTRY_ECHO_REVISION, DEVICE_SYNC_REVISION),
        (DEVICE_MANIFEST_REVISION, DOWNLOAD_ENTRY_ECHO_REVISION),
        (MULTIPART_SESSION_REVISION, DEVICE_MANIFEST_REVISION),
        (MULTIPART_SIZE_BOUND_REVISION, MULTIPART_SESSION_REVISION),
        (MULTIPART_DEFERRED_IDENTITY_REVISION, MULTIPART_SIZE_BOUND_REVISION),
        (MULTIPART_OPERATION_TOKEN_SEAL_REVISION, MULTIPART_DEFERRED_IDENTITY_REVISION),
        (SUBMITTED_POLICY_VERDICT_REVISION, MULTIPART_OPERATION_TOKEN_SEAL_REVISION),
        (GRANT_POLL_BUCKET_KIND_REVISION, SUBMITTED_POLICY_VERDICT_REVISION),
        (DEVICE_SYNC_SCALE_INDEX_REVISION, GRANT_POLL_BUCKET_KIND_REVISION),
        (TERMINAL_LOCATOR_REMEDIATION_REVISION, DEVICE_SYNC_SCALE_INDEX_REVISION),
    ):
        stacked = script_directory.get_revision(revision_id)
        assert stacked is not None, revision_id
        assert stacked.down_revision == down_revision_id, revision_id
        assert not stacked.branch_labels
        assert stacked.dependencies is None


def test_alembic_graph_loads_without_database_settings_or_secrets() -> None:
    """``alembic heads`` reads only the script directory; no secret is touched."""
    removed: dict[str, str] = {}
    for key in list(os.environ):
        if key.startswith("KNOWLEDGE_"):
            removed[key] = os.environ.pop(key)
    try:
        script_directory = _script_directory()
        assert script_directory.get_heads() == [TERMINAL_LOCATOR_REMEDIATION_REVISION]
    finally:
        os.environ.update(removed)


# ---------------------------------------------------------------------------
# alembic.ini contract (brief step 1b)
# ---------------------------------------------------------------------------


def test_alembic_ini_sets_script_location_and_transaction_per_migration() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    assert parser.read(ALEMBIC_INI_PATH, encoding="utf-8")
    section = parser["alembic"]
    assert section["script_location"].strip() == "%(here)s/migrations"
    assert section["transaction_per_migration"].strip().lower() == "true"


def test_alembic_ini_carries_no_connection_values() -> None:
    content = ALEMBIC_INI_PATH.read_text(encoding="utf-8").lower()
    assert "sqlalchemy.url" not in content
    assert "password" not in content
    parser = configparser.ConfigParser(interpolation=None)
    assert parser.read(ALEMBIC_INI_PATH, encoding="utf-8")
    for section_name in parser.sections():
        for key, value in parser.items(section_name):
            assert "@" not in value, (section_name, key)
            assert key not in {"host", "user", "password", "database", "dsn"}
            assert not value.startswith(("postgres://", "postgresql://"))


# ---------------------------------------------------------------------------
# env.py contract (brief step 1c)
# ---------------------------------------------------------------------------


def test_env_py_rejects_offline_mode() -> None:
    env_source = ENV_PY_PATH.read_text(encoding="utf-8")
    assert "context.is_offline_mode()" in env_source
    assert "offline" in env_source.lower()


def test_env_py_contains_the_exact_destructive_downgrade_gate() -> None:
    env_source = ENV_PY_PATH.read_text(encoding="utf-8")
    assert "context.get_x_argument(as_dictionary=True)" in env_source
    assert "allow_destructive" in env_source
    assert '!= "true"' in env_source
    assert "DATABASE_DESTRUCTIVE_DOWNGRADE_REFUSED" in env_source


def test_env_py_loads_settings_only_inside_the_online_path() -> None:
    """Settings and the secret file are loaded only by the online migration path."""
    tree = ast.parse(ENV_PY_PATH.read_text(encoding="utf-8"))
    online_function: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_run_online_migrations":
            online_function = node
    assert online_function is not None
    online_calls = {
        node.func.id
        for node in ast.walk(online_function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "load_database_migration_settings" in online_calls
    assert "read_database_password" in online_calls
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node is not online_function:
            nested_calls = {
                candidate.func.id
                for candidate in ast.walk(node)
                if isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Name)
            }
            assert "load_database_migration_settings" not in nested_calls
            assert "read_database_password" not in nested_calls


def test_env_py_uses_runtime_helpers_without_reimplementing_them() -> None:
    env_source = ENV_PY_PATH.read_text(encoding="utf-8")
    assert "build_database_url" in env_source
    assert "build_database_connect_arguments" in env_source
    assert "NullPool" in env_source
    assert "render_as_string" not in env_source
    assert "os.environ" not in env_source


# ---------------------------------------------------------------------------
# Forbidden patterns (brief step 1d)
# ---------------------------------------------------------------------------


def test_no_migration_file_imports_provider_sdks_or_reads_dotenv() -> None:
    migration_files = sorted(MIGRATIONS_PATH.rglob("*.py"))
    assert len(migration_files) >= 4
    for path in migration_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        assert not imported_roots & FORBIDDEN_IMPORT_ROOTS, path.name
        source = path.read_text(encoding="utf-8")
        assert "dotenv" not in source.lower(), path.name
        assert '".env"' not in source, path.name
        assert "'.env'" not in source, path.name
        assert "DATABASE_URL" not in source, path.name


def test_revision_does_not_import_application_models() -> None:
    source = REVISION_PATH.read_text(encoding="utf-8")
    assert "personal_os" not in source
    assert "from alembic import op" in source


def test_revision_contains_no_cascade_extension_enum_seed_or_uuid_default() -> None:
    source = REVISION_PATH.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "cascade" not in lowered
    assert "create extension" not in lowered
    assert "create type" not in lowered
    assert "insert" not in lowered
    assert "gen_random_uuid" not in lowered
    assert "uuidv7" not in lowered
    assert "jsonb" not in lowered
    assert "::json" not in lowered


def test_all_ddl_identifiers_fit_in_sixty_three_bytes() -> None:
    revision = _load_revision()
    identifiers = _named_identifiers(revision)
    identifiers.update(EXPECTED_TABLES_IN_CREATION_ORDER)
    identifiers.add(SCHEMA_NAME)
    assert len(identifiers) > 100
    for identifier in identifiers:
        assert len(identifier.encode("utf-8")) <= 63, identifier


# ---------------------------------------------------------------------------
# Static schema contract (brief step 4)
# ---------------------------------------------------------------------------


def test_revision_creates_exactly_nine_knowledge_tables() -> None:
    revision = _load_revision()
    tables = _table_contracts(revision)
    assert tuple(tables) == EXPECTED_TABLES_IN_CREATION_ORDER
    for table in tables.values():
        assert table.schema == SCHEMA_NAME


def test_revision_declares_the_exact_name_manifest() -> None:
    revision = _load_revision()
    expected = frozenset().union(*EXPECTED_MANIFEST.values())
    # The two trigger-function names sit outside the per-table manifest.
    expected = expected | {"reject_immutable_update", "reject_audit_mutation"}
    assert _named_identifiers(revision) == expected


def test_manifest_names_are_distributed_over_their_tables() -> None:
    revision = _load_revision()
    tables = _table_contracts(revision)
    index_columns = _index_contracts(revision)
    later_foreign_keys = {
        foreign_key.constraint_name: foreign_key.table
        for foreign_key in _create_foreign_key_calls(revision)
    }
    upgrade_sql = "\n".join(_execute_statements(revision, "upgrade"))
    trigger_tables = dict(
        re.findall(
            r"CREATE TRIGGER (\w+)\s+BEFORE (?:UPDATE(?: OR DELETE)? )?ON knowledge\.(\w+)",
            upgrade_sql,
        )
    )
    for table_name, expected_names in EXPECTED_MANIFEST.items():
        observed = set(tables[table_name].constraint_names)
        observed.update(index.name for index in index_columns.values() if index.table == table_name)
        observed.update(name for name, table in later_foreign_keys.items() if table == table_name)
        observed.update(name for name, table in trigger_tables.items() if table == table_name)
        assert observed == set(expected_names), table_name


def test_every_foreign_key_column_tuple_has_a_leading_index_path() -> None:
    revision = _load_revision()
    tables = _table_contracts(revision)
    index_columns = _index_contracts(revision)
    foreign_keys = [
        *_inline_foreign_keys(revision),
        *_create_foreign_key_calls(revision),
    ]
    assert len(foreign_keys) == 15
    for foreign_key in foreign_keys:
        table = tables[foreign_key.table]
        candidate_sets = [
            *table.unique_column_sets,
            *(
                index.columns
                for index in index_columns.values()
                if index.table == foreign_key.table
            ),
        ]
        assert any(
            columns[: len(foreign_key.local_columns)] == foreign_key.local_columns
            or foreign_key.local_columns[: len(columns)] == columns
            for columns in candidate_sets
        ), (foreign_key.constraint_name, foreign_key.local_columns)


def test_content_objects_are_global_and_other_tables_are_workspace_contained() -> None:
    revision = _load_revision()
    tables = _table_contracts(revision)
    content_columns = {column.name for column in tables["content_objects"].columns}
    assert "workspace_id" not in content_columns
    assert {column.name for column in tables["users"].columns} == {
        "user_id",
        "username",
        "display_name",
        "status",
        "created_at",
        "updated_at",
    }
    for table_name in (
        "workspaces",
        "devices",
        "sources",
        "source_versions",
        "sync_events",
        "projection_intents",
        "audit_events",
    ):
        table_columns = {column.name for column in tables[table_name].columns}
        assert "workspace_id" in table_columns, table_name


def test_sources_store_no_body_or_locator_columns() -> None:
    revision = _load_revision()
    source_columns = {column.name for column in _table_contracts(revision)["sources"].columns}
    assert source_columns == {
        "source_id",
        "workspace_id",
        "source_type",
        "title",
        "sync_state",
        "current_version_id",
        "created_at",
        "updated_at",
        "deleted_at",
    }


def test_only_event_sequence_is_identity_and_no_uuid_has_a_server_default() -> None:
    revision = _load_revision()
    identity_columns: list[str] = []
    defaulted_uuid_columns: list[str] = []
    for table in _table_contracts(revision).values():
        for column in table.columns:
            if column.has_identity:
                identity_columns.append(f"{table.name}.{column.name}")
            if column.type_name == "Uuid" and column.has_server_default:
                defaulted_uuid_columns.append(f"{table.name}.{column.name}")
    assert identity_columns == ["sync_events.event_sequence"]
    assert defaulted_uuid_columns == []


def test_current_version_pointer_is_deferrable_initially_immediate_match_simple() -> None:
    revision = _load_revision()
    pointers = [
        foreign_key
        for foreign_key in _create_foreign_key_calls(revision)
        if foreign_key.constraint_name == "fk_sources__current_version"
    ]
    assert len(pointers) == 1
    pointer = pointers[0]
    assert pointer.table == "sources"
    assert pointer.local_columns == ("workspace_id", "source_id", "current_version_id")
    assert pointer.referent_columns == (
        "workspace_id",
        "source_id",
        "source_version_id",
    )
    assert pointer.is_deferrable is True
    assert pointer.initially == "IMMEDIATE"
    assert pointer.match_clause is None
    assert pointer.ondelete == "RESTRICT"
    assert "MATCH FULL" not in revision.source


def test_projection_intent_event_source_containment_is_one_composite_foreign_key() -> None:
    revision = _load_revision()
    containment = [
        foreign_key
        for foreign_key in _inline_foreign_keys(revision)
        if foreign_key.constraint_name == "fk_projection_intents__event_source"
    ]
    assert len(containment) == 1
    foreign_key = containment[0]
    assert foreign_key.table == "projection_intents"
    assert foreign_key.local_columns == ("workspace_id", "source_id", "event_id")
    assert foreign_key.referent_columns == (
        "knowledge.sync_events.workspace_id",
        "knowledge.sync_events.source_id",
        "knowledge.sync_events.event_id",
    )


def test_exactly_two_functions_and_four_triggers_protect_immutability() -> None:
    revision = _load_revision()
    upgrade_sql = "\n".join(_execute_statements(revision, "upgrade"))
    trigger_names = re.findall(r"CREATE TRIGGER (\w+)", upgrade_sql)
    assert sorted(trigger_names) == [
        "trg_audit_events__reject_mutation",
        "trg_content_objects__reject_update",
        "trg_source_versions__reject_update",
        "trg_sync_events__reject_update",
    ]
    function_names = re.findall(r"CREATE FUNCTION knowledge\.(\w+)", upgrade_sql)
    assert sorted(function_names) == ["reject_audit_mutation", "reject_immutable_update"]

    immutable_wiring = re.findall(r"BEFORE UPDATE ON knowledge\.(\w+)", upgrade_sql)
    assert sorted(immutable_wiring) == [
        "content_objects",
        "source_versions",
        "sync_events",
    ]
    assert "BEFORE UPDATE OR DELETE ON knowledge.audit_events" in upgrade_sql
    assert upgrade_sql.count("EXECUTE FUNCTION knowledge.reject_immutable_update()") == 3
    assert upgrade_sql.count("EXECUTE FUNCTION knowledge.reject_audit_mutation()") == 1


def test_trigger_functions_use_fixed_safe_messages_and_search_path() -> None:
    revision = _load_revision()
    upgrade_sql = "\n".join(_execute_statements(revision, "upgrade"))
    assert "SET search_path = pg_catalog" in upgrade_sql
    assert (
        "RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'immutable_row_update_rejected'"
        in upgrade_sql
    )
    assert (
        "RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'audit_events_append_only'"
        in upgrade_sql
    )
    assert upgrade_sql.count("immutable_row_update_rejected") == 1
    assert upgrade_sql.count("audit_events_append_only") == 1


def test_upgrade_creates_schema_tables_pointer_indexes_then_triggers() -> None:
    revision = _load_revision()
    events = revision.ordered_events("upgrade")
    create_table_positions = {
        detail: position
        for position, (operation, detail) in enumerate(events)
        if operation == "create_table"
    }
    assert list(create_table_positions) == list(EXPECTED_TABLES_IN_CREATION_ORDER)
    pointer_positions = [
        position
        for position, (operation, detail) in enumerate(events)
        if operation == "create_foreign_key" and detail == "fk_sources__current_version"
    ]
    assert len(pointer_positions) == 1
    pointer_position = pointer_positions[0]
    assert pointer_position > create_table_positions["sources"]
    assert pointer_position > create_table_positions["source_versions"]
    assert pointer_position < create_table_positions["sync_events"]

    last_table_position = create_table_positions["audit_events"]
    index_positions = [
        position for position, (operation, _) in enumerate(events) if operation == "create_index"
    ]
    function_positions = [
        position
        for position, (operation, detail) in enumerate(events)
        if operation == "execute" and "CREATE FUNCTION knowledge." in detail
    ]
    trigger_positions = [
        position
        for position, (operation, detail) in enumerate(events)
        if operation == "execute" and "CREATE TRIGGER " in detail
    ]
    schema_position = next(
        position
        for position, (operation, detail) in enumerate(events)
        if operation == "execute" and "CREATE SCHEMA knowledge" in detail
    )
    assertion_position = next(
        position
        for position, (operation, detail) in enumerate(events)
        if operation == "execute" and "application_table_count" in detail
    )
    assert all(
        schema_position < table_position for table_position in create_table_positions.values()
    )
    assert all(position > last_table_position for position in index_positions)
    assert all(position > max(index_positions) for position in function_positions)
    assert all(position > max(function_positions) for position in trigger_positions)
    assert assertion_position > max(trigger_positions)


def test_downgrade_drops_known_objects_in_exact_reverse_without_cascade() -> None:
    revision = _load_revision()
    events = revision.ordered_events("downgrade")
    normalized_events = [(operation, " ".join(detail.split())) for operation, detail in events]
    normalized_expected = [
        (expected.split(":", 1)[0], " ".join(expected.split(":", 1)[1].split()))
        for expected in EXPECTED_DOWNGRADE_EVENTS
    ]
    assert normalized_events == normalized_expected
    downgrade_sql = "\n".join(_execute_statements(revision, "downgrade"))
    assert "cascade" not in downgrade_sql.lower()
    assert "DROP SCHEMA knowledge RESTRICT" in downgrade_sql


def test_foreign_keys_all_restrict_deletes() -> None:
    revision = _load_revision()
    foreign_keys = [
        *_inline_foreign_keys(revision),
        *_create_foreign_key_calls(revision),
    ]
    for foreign_key in foreign_keys:
        assert foreign_key.ondelete == "RESTRICT", foreign_key.constraint_name


# ---------------------------------------------------------------------------
# Poe database command contract (Task 6)
# ---------------------------------------------------------------------------

EXPECTED_DATABASE_COMMANDS: dict[str, str] = {
    "database-heads": "alembic heads",
    "database-current": "alembic current --check-heads",
    "database-upgrade": "alembic upgrade head",
    "database-downgrade": "alembic -x allow_destructive=true downgrade base",
}

# No Poe task may embed a password, connection URL, machine-specific absolute
# path or production host. The check scans every string in every task body
# (cmd, shell, sequence entries and env keys/values), not just the new commands.
FORBIDDEN_POE_SUBSTRINGS: tuple[str, ...] = (
    "://",
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "database_url",
    "/users/",
    "/home/",
    "c:\\",
    "d:\\",
    "c:/",
    "d:/",
    ".prod",
    "production.",
)


def _load_poe_tasks() -> dict[str, dict[str, Any]]:
    with PYPROJECT_PATH.open("rb") as handle:
        data = tomllib.load(handle)
    return data["tool"]["poe"]["tasks"]


def _iter_task_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _iter_task_strings(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_task_strings(item)


def test_poe_exposes_the_four_canonical_database_commands() -> None:
    tasks = _load_poe_tasks()
    for name, expected_command in EXPECTED_DATABASE_COMMANDS.items():
        assert name in tasks, name
        task = tasks[name]
        assert isinstance(task, dict), name
        # The four commands are leaf ``cmd`` tasks with no env, shell or sequence.
        assert set(task) == {"cmd"}, name
        assert task["cmd"] == expected_command, name


def test_database_commands_are_not_part_of_the_verify_sequence() -> None:
    tasks = _load_poe_tasks()
    sequence = tasks.get("verify", {}).get("sequence", [])
    assert isinstance(sequence, list)
    for name in EXPECTED_DATABASE_COMMANDS:
        assert name not in sequence, name


def test_no_poe_task_embeds_a_secret_url_or_machine_specific_path() -> None:
    tasks = _load_poe_tasks()
    for name, task in tasks.items():
        scanned = list(_iter_task_strings(task))
        assert scanned, name
        for text in scanned:
            lowered = text.lower()
            for forbidden in FORBIDDEN_POE_SUBSTRINGS:
                assert forbidden not in lowered, (name, forbidden)
