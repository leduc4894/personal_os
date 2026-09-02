"""Static contract tests for the authentication schema migration.

These tests are fully static: they inspect the Alembic script graph, the
parsed source of the ``20260816_01`` authentication revision and the DML Core
metadata in ``postgresql_source_store.tables``. They never start PostgreSQL
and never read a secret. The closed state checks are compared member-by-member
against the domain contracts in ``personal_os.authentication.contracts`` so
the database boundary and the closed enums cannot drift apart.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from personal_os.authentication.contracts import (
    FIXED_DEVICE_SCOPE,
    DeviceAuthorizationGrantState,
    DeviceTokenFamilyState,
    DeviceTokenKind,
    DeviceTokenState,
    TotpCredentialState,
    WebSessionState,
)
from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION
from postgresql_source_store.tables import SOURCE_STORE_TABLES

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
REVISION_PATH: Path = (
    REPO_ROOT
    / "migrations"
    / "versions"
    / "20260816_01_add_web_authentication_and_device_tokens.py"
)

AUTH_REVISION: str = "20260816_01"
BASELINE_REVISION: str = "20260813_01"
POLICY_REVISION: str = "20260817_01"
SMALL_FILE_REVISION: str = "20260818_01"
SOURCE_LIFECYCLE_REVISION: str = "20260820_01"
DEVICE_SYNC_REVISION: str = "20260826_01"
DOWNLOAD_ENTRY_ECHO_REVISION: str = "20260826_02"
SCHEMA_NAME: str = "knowledge"

EXPECTED_AUTH_TABLES = {
    "user_credentials",
    "web_sessions",
    "totp_credentials",
    "totp_recovery_codes",
    "device_authorization_grants",
    "device_token_families",
    "device_tokens",
    "authentication_throttle_buckets",
}

EXPECTED_TABLES_IN_CREATION_ORDER: tuple[str, ...] = (
    "user_credentials",
    "web_sessions",
    "totp_credentials",
    "totp_recovery_codes",
    "device_token_families",
    "device_tokens",
    "device_authorization_grants",
    "authentication_throttle_buckets",
)

_SHA256_CHECK: str = r"~ '^[0-9a-f]{64}$'"

#: Check-constraint name -> the hashed column it pins to lowercase hex SHA-256.
EXPECTED_SHA256_CHECKS: dict[str, str] = {
    "ck_web_sessions__session_secret_hash": "session_secret_hash",
    "ck_web_sessions__csrf_secret_hash": "csrf_secret_hash",
    "ck_totp_recovery_codes__code_hash": "code_hash",
    "ck_device_authorization_grants__user_code_hash": "user_code_hash",
    "ck_device_authorization_grants__polling_secret_hash": "polling_secret_hash",
    "ck_device_tokens__secret_hash": "secret_hash",
    "ck_authentication_throttle_buckets__bucket_hash": "bucket_hash",
}

#: Partial unique indexes required by spec 15: one active and at most one
#: pending TOTP credential per user, one active token family per device, one
#: current refresh generation per family and one successor per predecessor.
EXPECTED_PARTIAL_UNIQUE_INDEXES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "uq_totp_credentials__active_user": ("totp_credentials", ("user_id",), "state = 'active'"),
    "uq_totp_credentials__pending_user": ("totp_credentials", ("user_id",), "state = 'pending'"),
    "uq_device_token_families__active_device": (
        "device_token_families",
        ("device_id",),
        "state = 'active'",
    ),
    "uq_device_tokens__current_refresh_generation": (
        "device_tokens",
        ("token_family_id",),
        "token_kind = 'refresh' AND state = 'active'",
    ),
    "uq_device_tokens__successor_per_predecessor": (
        "device_tokens",
        ("predecessor_token_id",),
        "predecessor_token_id IS NOT NULL",
    ),
}

#: Non-unique partial index for the throttle-lock sweep.
EXPECTED_PARTIAL_INDEXES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "ix_authentication_throttle_buckets__locked_until": (
        "authentication_throttle_buckets",
        ("locked_until",),
        "locked_until IS NOT NULL",
    ),
}

EXPECTED_PLAIN_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ix_user_credentials__workspace_user": ("user_credentials", ("workspace_id", "user_id")),
    "ix_web_sessions__workspace_user": ("web_sessions", ("workspace_id", "user_id")),
    "ix_web_sessions__state_idle_expiry": (
        "web_sessions",
        ("state", "idle_expires_at", "web_session_id"),
    ),
    "ix_totp_credentials__workspace_user": ("totp_credentials", ("workspace_id", "user_id")),
    "ix_totp_recovery_codes__workspace_user": ("totp_recovery_codes", ("workspace_id", "user_id")),
    "ix_totp_recovery_codes__credential_revision": (
        "totp_recovery_codes",
        ("totp_credential_id", "revision"),
    ),
    "ix_device_token_families__workspace_user": (
        "device_token_families",
        ("workspace_id", "user_id"),
    ),
    "ix_device_token_families__workspace_device": (
        "device_token_families",
        ("workspace_id", "device_id", "state"),
    ),
    "ix_device_tokens__family_kind_generation": (
        "device_tokens",
        ("token_family_id", "token_kind", "generation"),
    ),
    "ix_device_tokens__workspace_user": ("device_tokens", ("workspace_id", "user_id")),
    "ix_device_tokens__workspace_device": ("device_tokens", ("workspace_id", "device_id")),
    "ix_device_tokens__successor": ("device_tokens", ("successor_token_id",)),
    "ix_device_authorization_grants__client_state_expiry": (
        "device_authorization_grants",
        ("client_instance_id", "state", "expires_at", "grant_id"),
    ),
    "ix_device_authorization_grants__approved_by_user": (
        "device_authorization_grants",
        ("approved_by_user_id",),
    ),
    "ix_device_authorization_grants__approval_session": (
        "device_authorization_grants",
        ("approved_web_session_id",),
    ),
    "ix_device_authorization_grants__device": (
        "device_authorization_grants",
        ("device_id",),
    ),
    "ix_device_authorization_grants__token_family": (
        "device_authorization_grants",
        ("token_family_id",),
    ),
    "ix_device_authorization_grants__initial_access_token": (
        "device_authorization_grants",
        ("initial_access_token_id",),
    ),
    "ix_device_authorization_grants__initial_refresh_token": (
        "device_authorization_grants",
        ("initial_refresh_token_id",),
    ),
}

#: Exact state/timestamp matrix expressions (spec 15.2/15.3/15.5/15.6/15.7):
#: inconsistent pending, approved, denied, exchanged, rotated and revoked rows
#: are rejected at the database boundary.
EXPECTED_MATRIX_CHECKS: dict[str, str] = {
    "ck_web_sessions__state_timestamps": (
        "(state IN ('active', 'recovery_limited')) = (authenticated_at IS NOT NULL) "
        "AND (state = 'revoked') = (revoked_at IS NOT NULL)"
    ),
    "ck_totp_credentials__state_timestamps": (
        "(state = 'pending') = (enrollment_expires_at IS NOT NULL) "
        "AND (state = 'pending') = (activated_at IS NULL AND replaced_at IS NULL) "
        "AND (state = 'active') = (activated_at IS NOT NULL AND replaced_at IS NULL) "
        "AND (state = 'replaced') = (replaced_at IS NOT NULL)"
    ),
    "ck_device_token_families__revocation": "(state = 'revoked') = (revoked_at IS NOT NULL)",
    "ck_device_tokens__state_lineage": (
        "(state = 'rotated') = (rotated_at IS NOT NULL) "
        "AND (state = 'revoked') = (revoked_at IS NOT NULL) "
        "AND (rotated_at IS NULL OR revoked_at IS NULL) "
        "AND (successor_token_id IS NULL OR state = 'rotated')"
    ),
    "ck_device_authorization_grants__state_matrix": (
        "(state = 'pending') = (approved_at IS NULL AND denied_at IS NULL "
        "AND exchanged_at IS NULL) "
        "AND (state = 'approved') = (approved_at IS NOT NULL AND denied_at IS NULL "
        "AND exchanged_at IS NULL) "
        "AND (state = 'denied') = (denied_at IS NOT NULL AND exchanged_at IS NULL) "
        "AND (state = 'exchanged') = (exchanged_at IS NOT NULL AND denied_at IS NULL "
        "AND device_id IS NOT NULL AND token_family_id IS NOT NULL "
        "AND initial_access_token_id IS NOT NULL AND initial_refresh_token_id IS NOT NULL) "
        "AND (approved_at IS NOT NULL) = (approved_by_user_id IS NOT NULL "
        "AND approved_web_session_id IS NOT NULL)"
    ),
}

#: Closed state vocabularies, keyed by check-constraint name; the parsed
#: IN-list must equal the domain enum member set exactly.
EXPECTED_CLOSED_STATE_CHECKS: dict[str, frozenset[str]] = {
    "ck_web_sessions__state": frozenset(member.value for member in WebSessionState),
    "ck_totp_credentials__state": frozenset(member.value for member in TotpCredentialState),
    "ck_device_authorization_grants__state": frozenset(
        member.value for member in DeviceAuthorizationGrantState
    ),
    "ck_device_tokens__token_kind": frozenset(member.value for member in DeviceTokenKind),
    "ck_device_tokens__state": frozenset(member.value for member in DeviceTokenState),
    "ck_device_token_families__state": frozenset(member.value for member in DeviceTokenFamilyState),
}

#: The recovery-code trigger protecting ``used_at`` immutability (spec 15.4).
EXPECTED_TRIGGER_NAME: str = "trg_totp_recovery_codes__reject_used_at_change"
EXPECTED_TRIGGER_FUNCTION_NAME: str = "reject_recovery_code_used_at_change"

#: The complete named-object manifest per table: primary keys, foreign keys,
#: unique constraints, check constraints, (partial) indexes and the trigger.
EXPECTED_MANIFEST: dict[str, frozenset[str]] = {
    "user_credentials": frozenset(
        {
            "pk_user_credentials",
            "fk_user_credentials__user",
            "fk_user_credentials__workspace_owner",
            "ck_user_credentials__password_hash",
            "ck_user_credentials__credential_revision",
            "ck_user_credentials__timestamps",
            "ix_user_credentials__workspace_user",
        }
    ),
    "web_sessions": frozenset(
        {
            "pk_web_sessions",
            "fk_web_sessions__workspace_owner",
            "uq_web_sessions__session_secret_hash",
            "ck_web_sessions__session_secret_hash",
            "ck_web_sessions__csrf_secret_hash",
            "ck_web_sessions__state",
            "ck_web_sessions__credential_revision",
            "ck_web_sessions__authentication_method",
            "ck_web_sessions__revocation_reason",
            "ck_web_sessions__state_timestamps",
            "ck_web_sessions__reauthentication",
            "ck_web_sessions__expiry",
            "ix_web_sessions__workspace_user",
            "ix_web_sessions__state_idle_expiry",
        }
    ),
    "totp_credentials": frozenset(
        {
            "pk_totp_credentials",
            "fk_totp_credentials__workspace_owner",
            "ck_totp_credentials__state",
            "ck_totp_credentials__secret_ciphertext",
            "ck_totp_credentials__secret_nonce",
            "ck_totp_credentials__key_id",
            "ck_totp_credentials__algorithm",
            "ck_totp_credentials__digits",
            "ck_totp_credentials__period_seconds",
            "ck_totp_credentials__last_accepted_time_step",
            "ck_totp_credentials__revision",
            "ck_totp_credentials__state_timestamps",
            "ck_totp_credentials__timestamps",
            "uq_totp_credentials__active_user",
            "uq_totp_credentials__pending_user",
            "ix_totp_credentials__workspace_user",
        }
    ),
    "totp_recovery_codes": frozenset(
        {
            "pk_totp_recovery_codes",
            "fk_totp_recovery_codes__credential",
            "fk_totp_recovery_codes__workspace_owner",
            "uq_totp_recovery_codes__credential_revision_hash",
            "ck_totp_recovery_codes__code_hash",
            "ck_totp_recovery_codes__revision",
            "ck_totp_recovery_codes__used_at",
            "ix_totp_recovery_codes__credential_revision",
            "ix_totp_recovery_codes__workspace_user",
            "trg_totp_recovery_codes__reject_used_at_change",
        }
    ),
    "device_token_families": frozenset(
        {
            "pk_device_token_families",
            "fk_device_token_families__workspace_owner",
            "fk_device_token_families__device",
            "ck_device_token_families__state",
            "ck_device_token_families__current_refresh_generation",
            "ck_device_token_families__timestamps",
            "ck_device_token_families__expiry",
            "ck_device_token_families__revocation",
            "ck_device_token_families__revocation_reason",
            "uq_device_token_families__active_device",
            "ix_device_token_families__workspace_user",
            "ix_device_token_families__workspace_device",
        }
    ),
    "device_tokens": frozenset(
        {
            "pk_device_tokens",
            "fk_device_tokens__family",
            "fk_device_tokens__workspace_owner",
            "fk_device_tokens__device",
            "fk_device_tokens__predecessor",
            "fk_device_tokens__successor",
            "uq_device_tokens__secret_hash",
            "ck_device_tokens__secret_hash",
            "ck_device_tokens__token_kind",
            "ck_device_tokens__generation",
            "ck_device_tokens__state",
            "ck_device_tokens__derivation_key_id",
            "ck_device_tokens__rotation_lineage",
            "ck_device_tokens__state_lineage",
            "ck_device_tokens__timestamps",
            "uq_device_tokens__current_refresh_generation",
            "uq_device_tokens__successor_per_predecessor",
            "ix_device_tokens__family_kind_generation",
            "ix_device_tokens__workspace_user",
            "ix_device_tokens__workspace_device",
            "ix_device_tokens__successor",
        }
    ),
    "device_authorization_grants": frozenset(
        {
            "pk_device_authorization_grants",
            "fk_device_authorization_grants__approved_by_user",
            "fk_device_authorization_grants__approval_session",
            "fk_device_authorization_grants__device",
            "fk_device_authorization_grants__token_family",
            "fk_device_authorization_grants__initial_access_token",
            "fk_device_authorization_grants__initial_refresh_token",
            "uq_device_authorization_grants__user_code_hash",
            "uq_device_authorization_grants__polling_secret_hash",
            "ck_device_authorization_grants__user_code_hash",
            "ck_device_authorization_grants__polling_secret_hash",
            "ck_device_authorization_grants__device_name",
            "ck_device_authorization_grants__platform_class",
            "ck_device_authorization_grants__platform_name",
            "ck_device_authorization_grants__plugin_version",
            "ck_device_authorization_grants__requested_scope",
            "ck_device_authorization_grants__state",
            "ck_device_authorization_grants__state_matrix",
            "ck_device_authorization_grants__exchange_links",
            "ck_device_authorization_grants__timestamps",
            "ix_device_authorization_grants__client_state_expiry",
            "ix_device_authorization_grants__approved_by_user",
            "ix_device_authorization_grants__approval_session",
            "ix_device_authorization_grants__device",
            "ix_device_authorization_grants__token_family",
            "ix_device_authorization_grants__initial_access_token",
            "ix_device_authorization_grants__initial_refresh_token",
        }
    ),
    "authentication_throttle_buckets": frozenset(
        {
            "pk_authentication_throttle_buckets",
            "uq_authentication_throttle_buckets__kind_hash",
            "ck_authentication_throttle_buckets__bucket_kind",
            "ck_authentication_throttle_buckets__bucket_hash",
            "ck_authentication_throttle_buckets__failed_attempt_count",
            "ck_authentication_throttle_buckets__timestamps",
            "ix_authentication_throttle_buckets__locked_until",
        }
    ),
}

EXPECTED_DOWNGRADE_TABLE_ORDER: tuple[str, ...] = tuple(reversed(EXPECTED_TABLES_IN_CREATION_ORDER))


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
    referent_table: str
    referent_columns: tuple[str, ...]
    ondelete: str | None


class RevisionSource:
    """Parsed view over the handwritten authentication revision module."""

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

    def check_constraints(self) -> dict[str, str]:
        """Map every named check constraint to its literal SQL expression."""
        checks: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if not _is_sa_call(node, "CheckConstraint"):
                continue
            name = _keyword_value(node, "name")
            expression = node.args[0] if node.args else None
            if name is None or expression is None:
                continue
            resolved_name = _resolve_name(name)
            resolved_expression = self.resolved_text(expression)
            if resolved_name is not None and resolved_expression is not None:
                checks[resolved_name] = resolved_expression
        return checks

    def resolved_text(self, node: ast.AST) -> str | None:
        """Resolve a literal, module constant or constant concatenation to text."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.constants:
            return self.constants[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.resolved_text(node.left)
            right = self.resolved_text(node.right)
            if left is not None and right is not None:
                return left + right
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


def _load_revision() -> RevisionSource:
    return RevisionSource(REVISION_PATH.read_text(encoding="utf-8"))


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


def _table_column_contracts(revision: RevisionSource) -> dict[str, tuple[ColumnContract, ...]]:
    tables: dict[str, tuple[ColumnContract, ...]] = {}
    for call in revision.calls("create_table"):
        table_name = _string_arguments(call)[0]
        columns = tuple(
            _column_contract(column) for column in ast.walk(call) if _is_sa_call(column, "Column")
        )
        tables[table_name] = columns
    return tables


def _inline_foreign_keys(revision: RevisionSource) -> list[ForeignKeyContract]:
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
            referents = tuple(
                value
                for value in (_resolve_name(item) for item in candidate.args[1].elts)
                if value is not None
            )
            referent_table = referents[0].split(".")[1] if referents else "<unknown>"
            referent_columns = tuple(referent.split(".")[-1] for referent in referents)
            ondelete = _constant(_keyword_value(candidate, "ondelete"))
            foreign_keys.append(
                ForeignKeyContract(
                    constraint_name=_keyword_name(candidate) or "<unnamed>",
                    table=table_name,
                    local_columns=local_columns,
                    referent_table=referent_table,
                    referent_columns=referent_columns,
                    ondelete=ondelete if isinstance(ondelete, str) else None,
                )
            )
    return foreign_keys


def _index_calls(revision: RevisionSource) -> dict[str, ast.Call]:
    return {_string_arguments(call)[0]: call for call in revision.calls("create_index")}


def _index_columns(call: ast.Call) -> tuple[str, ...]:
    column_list = call.args[2]
    assert isinstance(column_list, ast.List)
    return tuple(
        value for value in (_resolve_name(item) for item in column_list.elts) if value is not None
    )


def _named_identifiers(revision: RevisionSource) -> set[str]:
    identifiers: set[str] = set()
    for node in ast.walk(revision.tree):
        if isinstance(node, ast.Call):
            name = _keyword_name(node)
            if name is not None:
                identifiers.add(name)
    for call in revision.calls("create_index"):
        identifiers.add(_string_arguments(call)[0])
    upgrade_sql = "\n".join(
        sql for operation, sql in revision.ordered_events("upgrade") if operation == "execute"
    )
    identifiers.update(re.findall(r"CREATE TRIGGER (\w+)", upgrade_sql))
    identifiers.update(re.findall(r"CREATE FUNCTION knowledge\.(\w+)", upgrade_sql))
    return identifiers


class _RecordingAlembicOp:
    """Stub of ``alembic.op`` that records created tables and ignores DDL."""

    def __init__(self) -> None:
        self.created_tables: list[sa.Table] = []

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> sa.Table:
        schema = kwargs.get("schema")
        table = sa.Table(name, sa.MetaData(), *args, schema=schema)
        self.created_tables.append(table)
        return table

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_context(self) -> None:
        return None


def _replay_upgrade_tables() -> dict[str, sa.Table]:
    spec = importlib.util.spec_from_file_location("authentication_migration", REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = _RecordingAlembicOp()
    module.op = recorder  # type: ignore[attr-defined]
    module.upgrade()
    return {table.name: table for table in recorder.created_tables}


def _column_signature(column: sa.Column) -> tuple[Any, ...]:
    return (
        column.name,
        type(column.type),
        getattr(column.type, "length", None),
        getattr(column.type, "timezone", None),
        column.nullable,
    )


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


def _in_list_values(expression: str) -> frozenset[str]:
    match = re.search(r"IN \(([^)]*)\)", expression)
    assert match is not None, f"expression is not a closed IN check: {expression!r}"
    return frozenset(re.findall(r"'([^']+)'", match.group(1)))


# ---------------------------------------------------------------------------
# Alembic graph contract
# ---------------------------------------------------------------------------


def test_alembic_graph_has_exactly_one_head_beyond_the_authentication_revision() -> None:
    # Subsequent policy, small-file, lifecycle, device sync, multipart,
    # grant-poll bucket kind, device-sync scale index and terminal locator
    # remediation revisions stack on this revision, so the single graph head
    # moved past authentication.
    script_directory = _script_directory()
    assert script_directory.get_heads() == ["20260902_01"]


def test_authentication_revision_stacks_on_the_canonical_baseline_root() -> None:
    script_directory = _script_directory()
    revisions = list(script_directory.walk_revisions())
    assert len(revisions) == 17
    baseline = script_directory.get_revision(BASELINE_REVISION)
    assert baseline is not None
    assert baseline.down_revision is None
    authentication = script_directory.get_revision(AUTH_REVISION)
    assert authentication is not None
    assert authentication.down_revision == BASELINE_REVISION
    assert not authentication.branch_labels
    assert authentication.dependencies is None


def test_canonical_revision_constant_is_the_current_graph_head() -> None:
    # The canonical revision authority always pins the current graph head; the
    # source-conflict migration ``20260902_01`` is that head now.
    assert CANONICAL_POSTGRESQL_SCHEMA_REVISION == "20260902_01"


# ---------------------------------------------------------------------------
# DML metadata contract (brief step 1)
# ---------------------------------------------------------------------------


def test_authentication_tables_are_present_in_dml_metadata() -> None:
    assert set(SOURCE_STORE_TABLES) >= EXPECTED_AUTH_TABLES


def test_dml_metadata_covers_every_authentication_column_and_type() -> None:
    migration_tables = _replay_upgrade_tables()
    assert set(migration_tables) == EXPECTED_AUTH_TABLES

    for table_name in sorted(EXPECTED_AUTH_TABLES):
        dml_table = SOURCE_STORE_TABLES[table_name]
        migration_table = migration_tables[table_name]

        assert dml_table.schema == SCHEMA_NAME, table_name
        dml_columns = {column.name: _column_signature(column) for column in dml_table.columns}
        migration_columns = {
            column.name: _column_signature(column) for column in migration_table.columns
        }
        assert dml_columns == migration_columns, table_name

        dml_primary_keys = {column.name for column in dml_table.primary_key.columns}
        migration_primary_keys = {column.name for column in migration_table.primary_key.columns}
        assert dml_primary_keys == migration_primary_keys, table_name


# ---------------------------------------------------------------------------
# Static schema contract
# ---------------------------------------------------------------------------


def test_revision_creates_exactly_the_eight_authentication_tables_in_order() -> None:
    revision = _load_revision()
    events = revision.ordered_events("upgrade")
    create_table_names = [detail for operation, detail in events if operation == "create_table"]
    assert create_table_names == list(EXPECTED_TABLES_IN_CREATION_ORDER)
    for call in revision.calls("create_table"):
        assert _keyword_value(call, "schema") is not None


def test_closed_state_checks_match_the_domain_contracts() -> None:
    revision = _load_revision()
    checks = revision.check_constraints()
    for check_name, expected_members in EXPECTED_CLOSED_STATE_CHECKS.items():
        assert check_name in checks, check_name
        assert _in_list_values(checks[check_name]) == expected_members, check_name


def test_fixed_device_scope_is_the_only_requested_grant_scope() -> None:
    revision = _load_revision()
    checks = revision.check_constraints()
    assert checks["ck_device_authorization_grants__requested_scope"] == (
        f"requested_scope = '{FIXED_DEVICE_SCOPE.value}'"
    )


def test_hash_columns_use_the_canonical_sha256_grammar() -> None:
    revision = _load_revision()
    checks = revision.check_constraints()
    for check_name, hashed_column in EXPECTED_SHA256_CHECKS.items():
        assert checks.get(check_name) == f"{hashed_column} {_SHA256_CHECK}", check_name


def test_state_matrix_checks_bind_timestamps_and_references() -> None:
    revision = _load_revision()
    checks = revision.check_constraints()
    for check_name, expected_expression in EXPECTED_MATRIX_CHECKS.items():
        assert checks.get(check_name) == expected_expression, check_name


def test_web_sessions_never_outlive_their_absolute_expiry() -> None:
    revision = _load_revision()
    checks = revision.check_constraints()
    assert "idle_expires_at <= absolute_expires_at" in checks["ck_web_sessions__expiry"]


def test_partial_unique_indexes_enforce_the_spec_single_row_rules() -> None:
    revision = _load_revision()
    indexes = _index_calls(revision)
    for name, (table, columns, where_fragment) in EXPECTED_PARTIAL_UNIQUE_INDEXES.items():
        assert name in indexes, name
        call = indexes[name]
        assert _constant(_keyword_value(call, "unique")) is True, name
        assert _string_arguments(call)[1] == table, name
        assert _index_columns(call) == columns, name
        where = _keyword_value(call, "postgresql_where")
        assert where is not None, name
        where_sql = revision.resolved_text(where)
        assert where_sql is not None and where_fragment in where_sql, name


def test_plain_and_partial_indexes_cover_foreign_key_and_query_paths() -> None:
    revision = _load_revision()
    indexes = _index_calls(revision)
    for name, (table, columns) in EXPECTED_PLAIN_INDEXES.items():
        assert name in indexes, name
        call = indexes[name]
        assert _constant(_keyword_value(call, "unique")) is None, name
        assert _string_arguments(call)[1] == table, name
        assert _index_columns(call) == columns, name
    for name, (table, columns, where_fragment) in EXPECTED_PARTIAL_INDEXES.items():
        assert name in indexes, name
        call = indexes[name]
        assert _constant(_keyword_value(call, "unique")) is None, name
        assert _string_arguments(call)[1] == table, name
        assert _index_columns(call) == columns, name
        where = _keyword_value(call, "postgresql_where")
        assert where is not None, name
        where_sql = revision.resolved_text(where)
        assert where_sql is not None and where_fragment in where_sql, name


def test_every_foreign_key_restricts_deletes_and_targets_the_baseline_or_auth_tables() -> None:
    revision = _load_revision()
    foreign_keys = _inline_foreign_keys(revision)
    assert len(foreign_keys) == 19
    allowed_targets = EXPECTED_AUTH_TABLES | {"users", "workspaces", "devices"}
    for foreign_key in foreign_keys:
        assert foreign_key.ondelete == "RESTRICT", foreign_key.constraint_name
        assert foreign_key.referent_table in allowed_targets, foreign_key.constraint_name
        assert foreign_key.constraint_name.startswith(f"fk_{foreign_key.table}__")


def test_every_foreign_key_column_tuple_has_a_leading_index_path() -> None:
    revision = _load_revision()
    indexes = _index_calls(revision)
    index_columns: dict[str, list[tuple[str, ...]]] = {}
    unique_columns: dict[str, list[tuple[str, ...]]] = {}
    primary_keys: dict[str, tuple[str, ...]] = {}
    for call in revision.calls("create_table"):
        table_name = _string_arguments(call)[0]
        unique_columns[table_name] = []
        primary_keys[table_name] = ()
        for candidate in ast.walk(call):
            if not _is_sa_call(candidate, "PrimaryKeyConstraint"):
                if _is_sa_call(candidate, "UniqueConstraint"):
                    unique_columns[table_name].append(_string_arguments(candidate))
                continue
            primary_keys[table_name] = _string_arguments(candidate)
    for index_call in indexes.values():
        table = _string_arguments(index_call)[1]
        index_columns.setdefault(table, []).append(_index_columns(index_call))
    for foreign_key in _inline_foreign_keys(revision):
        table = foreign_key.table
        candidate_sets = [
            *unique_columns[table],
            *index_columns.get(table, []),
        ]
        if primary_keys[table]:
            candidate_sets.append(primary_keys[table])
        assert any(
            columns[: len(foreign_key.local_columns)] == foreign_key.local_columns
            or foreign_key.local_columns[: len(columns)] == columns
            for columns in candidate_sets
        ), (foreign_key.constraint_name, foreign_key.local_columns)


def test_revision_declares_the_exact_name_manifest() -> None:
    revision = _load_revision()
    expected = frozenset().union(*EXPECTED_MANIFEST.values())
    expected = expected | {EXPECTED_TRIGGER_FUNCTION_NAME}
    assert _named_identifiers(revision) == expected


def test_manifest_names_are_distributed_over_their_tables() -> None:
    revision = _load_revision()
    tables = _table_column_contracts(revision)
    index_calls = _index_calls(revision)
    inline_foreign_keys = {
        foreign_key.constraint_name: foreign_key.table
        for foreign_key in _inline_foreign_keys(revision)
    }
    upgrade_sql = "\n".join(
        sql for operation, sql in revision.ordered_events("upgrade") if operation == "execute"
    )
    trigger_tables = dict(
        re.findall(r"CREATE TRIGGER (\w+)\s+BEFORE UPDATE ON knowledge\.(\w+)", upgrade_sql)
    )
    for table_name, expected_names in EXPECTED_MANIFEST.items():
        observed = {
            name
            for name, table in inline_foreign_keys.items()
            if table == table_name and name.startswith("fk_")
        }
        observed.update(
            name for name, call in index_calls.items() if _string_arguments(call)[1] == table_name
        )
        observed.update(name for name, table in trigger_tables.items() if table == table_name)
        # Primary keys, unique constraints and check constraints are named
        # inline in each create_table; harvest them by parsing the call.
        for call in revision.calls("create_table"):
            if _string_arguments(call)[0] != table_name:
                continue
            for candidate in ast.walk(call):
                if (
                    isinstance(candidate, ast.Call)
                    and isinstance(candidate.func, ast.Attribute)
                    and isinstance(candidate.func.value, ast.Name)
                    and candidate.func.value.id == "sa"
                    and candidate.func.attr
                    in {"PrimaryKeyConstraint", "UniqueConstraint", "CheckConstraint"}
                ):
                    name = _keyword_name(candidate)
                    if name is not None:
                        observed.add(name)
        assert observed == set(expected_names), table_name
        assert table_name in tables


def test_all_ddl_identifiers_fit_in_sixty_three_bytes() -> None:
    revision = _load_revision()
    identifiers = _named_identifiers(revision)
    identifiers.update(EXPECTED_TABLES_IN_CREATION_ORDER)
    identifiers.add(SCHEMA_NAME)
    assert len(identifiers) > 60
    for identifier in identifiers:
        assert len(identifier.encode("utf-8")) <= 63, identifier


def test_no_uuid_has_a_server_default_and_no_column_is_identity() -> None:
    revision = _load_revision()
    identity_columns: list[str] = []
    defaulted_uuid_columns: list[str] = []
    for table_name, columns in _table_column_contracts(revision).items():
        for column in columns:
            if column.has_identity:
                identity_columns.append(f"{table_name}.{column.name}")
            if column.type_name == "Uuid" and column.has_server_default:
                defaulted_uuid_columns.append(f"{table_name}.{column.name}")
    assert identity_columns == []
    assert defaulted_uuid_columns == []


def test_recovery_code_used_at_is_protected_by_a_trigger() -> None:
    revision = _load_revision()
    upgrade_sql = "\n".join(
        sql for operation, sql in revision.ordered_events("upgrade") if operation == "execute"
    )
    assert f"CREATE TRIGGER {EXPECTED_TRIGGER_NAME}" in upgrade_sql
    assert (
        "BEFORE UPDATE ON knowledge.totp_recovery_codes "
        "FOR EACH ROW WHEN (OLD.used_at IS NOT NULL) "
        f"EXECUTE FUNCTION knowledge.{EXPECTED_TRIGGER_FUNCTION_NAME}()"
    ) in upgrade_sql
    assert (
        f"REVOKE EXECUTE ON FUNCTION knowledge.{EXPECTED_TRIGGER_FUNCTION_NAME} FROM PUBLIC"
        in upgrade_sql
    )


def test_upgrade_finishes_with_a_final_catalog_assertion() -> None:
    revision = _load_revision()
    upgrade_sql = "\n".join(
        sql for operation, sql in revision.ordered_events("upgrade") if operation == "execute"
    )
    assert "application_table_count <> 17" in upgrade_sql
    assert "trigger_function_count <> 3" in upgrade_sql
    assert "protection_trigger_count <> 5" in upgrade_sql


def test_revision_imports_no_application_code_or_orm_surface() -> None:
    source = REVISION_PATH.read_text(encoding="utf-8")
    assert "personal_os" not in source
    assert "from alembic import op" in source
    lowered = source.lower()
    assert "relationship" not in lowered
    assert "create_all" not in lowered
    assert "cascade" not in lowered
    assert "create extension" not in lowered
    assert "create type" not in lowered
    assert "gen_random_uuid" not in lowered
    assert "jsonb" not in lowered


# ---------------------------------------------------------------------------
# Downgrade contract
# ---------------------------------------------------------------------------


def test_downgrade_drops_trigger_function_and_eight_tables_without_cascade() -> None:
    revision = _load_revision()
    events = revision.ordered_events("downgrade")
    normalized_events = [(operation, " ".join(detail.split())) for operation, detail in events]
    expected_prefix = [
        ("execute", f"DROP TRIGGER {EXPECTED_TRIGGER_NAME} ON knowledge.totp_recovery_codes"),
        ("execute", f"DROP FUNCTION knowledge.{EXPECTED_TRIGGER_FUNCTION_NAME}"),
    ]
    expected_tables = [("drop_table", table_name) for table_name in EXPECTED_DOWNGRADE_TABLE_ORDER]
    assert normalized_events == expected_prefix + expected_tables
    downgrade_sql = "\n".join(sql for operation, sql in events if operation == "execute")
    assert "cascade" not in downgrade_sql.lower()
    assert "DROP SCHEMA" not in downgrade_sql
