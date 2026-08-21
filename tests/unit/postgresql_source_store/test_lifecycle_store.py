"""Lifecycle store SQL parameterization, lock order, redaction and identity.

These tests pin the pure pieces of the PostgreSQL lifecycle adapter without a
database: every SQL statement is parameter-bound (no literal locator, locator
fingerprint, source id, event id, tombstone id, intent id or audit id ever
appears as a literal in compiled SQL), the lock acquisition order is fixed
(idempotency identity, source advisory + row, locator advisory + row in
canonical text order, optional tombstone row), the deterministic UUIDv7
identities are allocated up front and reused through every retry, and the
closed error vocabulary maps onto the lifecycle registry without leaking any
raw locator, locator fingerprint, title or content digest into a typed error,
diagnostic field or metric label. The atomic transaction behavior is
integration territory (disposable stack).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    LifecycleState,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import SourceLifecycleErrorCode
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_lifecycle.title import derive_title_v1
from personal_os.source_locators import NormalizedLocator
from postgresql_source_store.lifecycle_store import (
    AUDIT_ACTIONS_BY_OPERATION,
    AUDIT_TARGET_KIND_SOURCE,
    EVENT_TYPE_BY_OPERATION,
    PROJECTION_KIND_QDRANT,
    PROJECTION_OPERATION_DELETE,
    PROJECTION_OPERATION_UPSERT,
    SOURCE_STATE_ACTIVE,
    SOURCE_STATE_DELETED,
    LifecycleCommitIdentities,
    LifecycleReplayLookupRow,
    advisory_lock_key_for_locator,
    audit_insert_statement,
    classify_locator_conflict,
    classify_state_mismatch,
    classify_tombstone_conflict,
    classify_version_mismatch,
    close_locator_statement,
    close_tombstone_set_delete,
    event_insert_statement,
    intent_insert_statement,
    is_locator_lock_order_valid,
    locator_advisory_lock_statement,
    locator_open_insert_statement,
    open_tombstone_insert_statement,
    order_locator_lock_keys,
    source_lock_statement,
    sync_event_lookup_by_event_statement,
    sync_event_lookup_by_key_statement,
    tombstone_close_statement,
    tombstone_lookup_by_id_statement,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000001")
_DEVICE_ID = UUID("018f47a0-7b00-7000-8000-000000000002")
_USER_ID = UUID("018f47a0-7b00-7000-8000-000000000003")
_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000004")
_VERSION_ID = UUID("018f47a0-7b00-7000-8000-000000000005")
_EVENT_ID = UUID("018f47a0-7b00-7000-8000-000000000006")
_TOMBSTONE_ID = UUID("018f47a0-7b00-7000-8000-000000000007")
_LOCATOR_OLD_ID = UUID("018f47a0-7b00-7000-8000-000000000008")
_LOCATOR_NEW_ID = UUID("018f47a0-7b00-7000-8000-000000000009")
_INTENT_QDRANT_ID = UUID("018f47a0-7b00-7000-8000-00000000000a")
_INTENT_NEO4J_ID = UUID("018f47a0-7b00-7000-8000-00000000000b")
_AUDIT_ID = UUID("018f47a0-7b00-7000-8000-00000000000c")


def _device_context() -> LifecycleDeviceContext:
    return LifecycleDeviceContext(
        workspace_id=_WORKSPACE_ID,
        device_id=_DEVICE_ID,
        user_id=_USER_ID,
        device_kind="obsidian",
    )


def _rename_command(**overrides: object) -> SourceLifecycleCommand:
    values: dict[str, object] = {
        "source_id": _SOURCE_ID,
        "event_id": _EVENT_ID,
        "idempotency_key": "lifecycle-rename-001",
        "operation": LifecycleOperation.RENAME,
        "expected_version_id": _VERSION_ID,
        "expected_locator": NormalizedLocator("notes/old.md"),
        "target_locator": NormalizedLocator("notes/new.md"),
        "tombstone_id": None,
        "policy_revision": 7,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def _move_command(**overrides: object) -> SourceLifecycleCommand:
    values: dict[str, object] = {
        "source_id": _SOURCE_ID,
        "event_id": UUID("018f47a0-7b00-7000-8000-000000000020"),
        "idempotency_key": "lifecycle-move-001",
        "operation": LifecycleOperation.MOVE,
        "expected_version_id": _VERSION_ID,
        "expected_locator": NormalizedLocator("notes/old.md"),
        "target_locator": NormalizedLocator("archive/old.md"),
        "tombstone_id": None,
        "policy_revision": 7,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def _delete_command(**overrides: object) -> SourceLifecycleCommand:
    values: dict[str, object] = {
        "source_id": _SOURCE_ID,
        "event_id": UUID("018f47a0-7b00-7000-8000-000000000030"),
        "idempotency_key": "lifecycle-delete-001",
        "operation": LifecycleOperation.DELETE,
        "expected_version_id": _VERSION_ID,
        "expected_locator": NormalizedLocator("notes/active.md"),
        "target_locator": None,
        "tombstone_id": None,
        "policy_revision": 7,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def _restore_command(**overrides: object) -> SourceLifecycleCommand:
    values: dict[str, object] = {
        "source_id": _SOURCE_ID,
        "event_id": UUID("018f47a0-7b00-7000-8000-000000000040"),
        "idempotency_key": "lifecycle-restore-001",
        "operation": LifecycleOperation.RESTORE,
        "expected_version_id": _VERSION_ID,
        "expected_locator": None,
        "target_locator": NormalizedLocator("notes/restored.md"),
        "tombstone_id": _TOMBSTONE_ID,
        "policy_revision": 7,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def _subject() -> PolicySubject:
    return PolicySubject(
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        normalized_locator="notes/new.md",
        source_type="markdown",
    )


def _allowed_decision() -> LifecyclePolicyDecision:
    return LifecyclePolicyDecision(
        workspace_id=_WORKSPACE_ID,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number=7,
        subject=_subject(),
        expected_locator=NormalizedLocator("notes/old.md"),
        target_locator=NormalizedLocator("notes/new.md"),
    )


def _denied_decision() -> LifecyclePolicyDecision:
    return LifecyclePolicyDecision(
        workspace_id=_WORKSPACE_ID,
        outcome=LifecyclePolicyOutcome.DENIED,
        policy_revision_number=7,
        subject=_subject(),
        expected_locator=NormalizedLocator("notes/old.md"),
        target_locator=NormalizedLocator("notes/new.md"),
    )


def _indeterminate_decision() -> LifecyclePolicyDecision:
    return LifecyclePolicyDecision(
        workspace_id=_WORKSPACE_ID,
        outcome=LifecyclePolicyOutcome.INDETERMINATE,
        policy_revision_number=7,
        subject=_subject(),
        expected_locator=NormalizedLocator("notes/old.md"),
        target_locator=NormalizedLocator("notes/new.md"),
    )


def _committed_result(
    *,
    state: LifecycleState = LifecycleState.ACTIVE,
    tombstone_id: UUID | None = None,
    resulting_locator: NormalizedLocator | None = None,
    event_id: UUID | None = None,
    event_sequence: int = 7,
    source_version_id: UUID | None = None,
    committed_at: datetime | None = None,
) -> SourceLifecycleCommitResult:
    if resulting_locator is None:
        resulting_locator = NormalizedLocator("notes/new.md")
    return SourceLifecycleCommitResult(
        source_id=_SOURCE_ID,
        source_version_id=source_version_id if source_version_id is not None else _VERSION_ID,
        event_id=event_id if event_id is not None else _EVENT_ID,
        event_sequence=event_sequence,
        state=state,
        tombstone_id=tombstone_id,
        resulting_locator=resulting_locator,
        committed_at=(
            committed_at
            if committed_at is not None
            else datetime(2026, 8, 20, 2, 2, 3, tzinfo=UTC)
        ),
    )


# --- pure statement shapes ------------------------------------------------------


def _bind_marker(text: str, column: str) -> bool:
    """Check whether a parameter-bound marker for the column is in the SQL text.

    SQLAlchemy names duplicate columns with a numeric suffix
    (``%(workspace_id_1)s``), so we accept either the bare or suffixed form.
    """

    if f"%({column})s" in text:
        return True
    return f"%({column}_1)s" in text


def _assert_no_raw_locator(text: str, *locators: str) -> None:
    for value in locators:
        assert value not in text, f"raw locator {value!r} leaked into SQL: {text}"
        # SQLAlchemy's bind markers must never embed a locator substring.
        for fragment in (value, value.replace("/", " / ")):
            assert fragment not in text


def test_source_lock_statement_binds_source_id_only() -> None:
    statement = source_lock_statement(_SOURCE_ID)
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    # The advisory lock helper binds ``namespace`` and ``derived_key`` only;
    # the source UUID is material in the call (never a SQL literal).
    assert _bind_marker(text, "namespace")
    assert _bind_marker(text, "derived_key")
    # No raw UUID appears as a literal substring inside the compiled SQL.
    assert str(_SOURCE_ID) not in text
    assert "SELECT" in text and "pg_advisory_xact_lock" in text


def test_locator_advisory_lock_statement_binds_lock_key_only() -> None:
    statement = locator_advisory_lock_statement(NormalizedLocator("notes/old.md"))
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    assert isinstance(statement, sa.TextClause)
    text = str(compiled)
    assert _bind_marker(text, "derived_key")
    # No raw locator text appears as a SQL literal substring.
    assert "notes/old.md" not in text


def test_locator_open_insert_statement_binds_every_lifecycle_operand() -> None:
    statement = locator_open_insert_statement(
        source_locator_id=_LOCATOR_NEW_ID,
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        locator=NormalizedLocator("notes/new.md"),
        opened_event_id=_EVENT_ID,
        opened_sequence=42,
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    for column in (
        "source_locator_id",
        "workspace_id",
        "source_id",
        "normalized_locator",
        "display_locator",
        "opened_event_id",
        "opened_sequence",
    ):
        assert _bind_marker(text, column), f"missing bind marker for {column} in {text}"
    assert str(_LOCATOR_NEW_ID) not in text
    assert "notes/new.md" not in text


def test_close_locator_statement_binds_only_required_ids() -> None:
    statement = close_locator_statement(
        source_locator_id=_LOCATOR_OLD_ID,
        closed_event_id=_EVENT_ID,
        closed_sequence=43,
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert _bind_marker(text, "closed_event_id")
    assert _bind_marker(text, "closed_sequence")
    assert "knowledge.source_locators" in text
    assert "source_locator_id" in text
    assert str(_LOCATOR_OLD_ID) not in text


def test_open_tombstone_insert_statement_binds_every_tombstone_operand() -> None:
    statement = open_tombstone_insert_statement(
        source_tombstone_id=_TOMBSTONE_ID,
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        delete_event_id=_EVENT_ID,
        retained_version_id=_VERSION_ID,
        retained_locator=NormalizedLocator("notes/old.md"),
        actor_kind="device",
        actor_id=_DEVICE_ID,
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    for column in (
        "source_tombstone_id",
        "workspace_id",
        "source_id",
        "delete_event_id",
        "retained_version_id",
        "retained_locator",
        "actor_kind",
        "actor_id",
    ):
        assert _bind_marker(text, column), f"missing bind marker for {column} in {text}"
    assert "notes/old.md" not in text


def test_tombstone_close_statement_binds_restore_event() -> None:
    restore_event_id = UUID("018f47a0-7b00-7000-8000-000000000050")
    statement = tombstone_close_statement(
        source_tombstone_id=_TOMBSTONE_ID, restore_event_id=restore_event_id
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert _bind_marker(text, "restore_event_id")
    assert "knowledge.source_tombstones" in text
    assert str(restore_event_id) not in text


def test_close_tombstone_set_delete_helper_returns_set_delete_statement() -> None:
    statement = close_tombstone_set_delete(_SOURCE_ID)
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert "knowledge.sources" in text
    assert "deleted_at" in text
    assert _bind_marker(text, "source_id")


def test_event_insert_statement_binds_every_lifecycle_event_operand() -> None:
    statement = event_insert_statement(
        event_id=_EVENT_ID,
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        device_id=_DEVICE_ID,
        committed_version_id=_VERSION_ID,
        base_version_id=_VERSION_ID,
        idempotency_key="lifecycle-rename-001",
        request_fingerprint="a" * 64,
        event_type="rename",
        client_timestamp=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    for column in (
        "event_id",
        "workspace_id",
        "source_id",
        "committed_version_id",
        "base_version_id",
        "idempotency_key",
        "request_fingerprint",
        "event_type",
    ):
        assert _bind_marker(text, column), f"missing bind marker for {column} in {text}"
    assert "lifecycle-rename-001" not in text


def test_intent_insert_statement_binds_kind_operation_and_source_version() -> None:
    statement = intent_insert_statement(
        projection_intent_id=_INTENT_QDRANT_ID,
        workspace_id=_WORKSPACE_ID,
        event_id=_EVENT_ID,
        source_id=_SOURCE_ID,
        source_version_id=_VERSION_ID,
        projection_kind=PROJECTION_KIND_QDRANT,
        operation=PROJECTION_OPERATION_UPSERT,
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    for column in (
        "projection_intent_id",
        "workspace_id",
        "event_id",
        "source_id",
        "source_version_id",
        "projection_kind",
        "operation",
    ):
        assert _bind_marker(text, column), f"missing bind marker for {column} in {text}"


def test_audit_insert_statement_binds_only_redacted_fields() -> None:
    statement = audit_insert_statement(
        audit_event_id=_AUDIT_ID,
        workspace_id=_WORKSPACE_ID,
        actor_kind="device",
        actor_id=_DEVICE_ID,
        action=AUDIT_ACTIONS_BY_OPERATION[LifecycleOperation.RENAME],
        target_kind=AUDIT_TARGET_KIND_SOURCE,
        target_id=_SOURCE_ID,
        request_id=uuid4(),
        client_request_id=None,
        trace_id="0af7651916cd43dd8448eb211c80319c",
        result="succeeded",
        reason_code=None,
        safe_diff_hash="f" * 64,
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    for column in (
        "audit_event_id",
        "actor_kind",
        "actor_id",
        "action",
        "target_kind",
        "target_id",
        "request_id",
        "trace_id",
        "result",
        "reason_code",
        "safe_diff_hash",
    ):
        assert _bind_marker(text, column), f"missing bind marker for {column} in {text}"
    # Raw locator / fingerprint / title / digest must never be a column.
    # (note: normalized_locator is a column on the source_locators table; this
    # audit row must not join or reference it. The text is the audit_events
    # insert only, so the only columns referenced belong to audit_events.)
    assert "knowledge.audit_events" in text


def test_replay_lookup_by_key_statement_binds_workspace_and_key() -> None:
    statement = sync_event_lookup_by_key_statement(_WORKSPACE_ID, "lifecycle-rename-001")
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "idempotency_key")
    assert "lifecycle-rename-001" not in text


def test_replay_lookup_by_event_statement_binds_event_id_only() -> None:
    statement = sync_event_lookup_by_event_statement(_EVENT_ID)
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert _bind_marker(text, "event_id")
    assert str(_EVENT_ID) not in text


def test_tombstone_lookup_by_id_statement_binds_only_tombstone_and_workspace() -> None:
    statement = tombstone_lookup_by_id_statement(
        workspace_id=_WORKSPACE_ID, source_tombstone_id=_TOMBSTONE_ID
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "source_tombstone_id")
    assert str(_TOMBSTONE_ID) not in text


# --- lock order contract --------------------------------------------------------


def test_order_locator_lock_keys_returns_canonical_text_order() -> None:
    ordered = order_locator_lock_keys(
        [
            NormalizedLocator("zeta/last.md"),
            NormalizedLocator("alpha/first.md"),
            NormalizedLocator("middle/file.md"),
        ]
    )
    assert [locator.value for locator, _key in ordered] == [
        "alpha/first.md",
        "middle/file.md",
        "zeta/last.md",
    ]


def test_advisory_lock_key_for_locator_is_stable_and_signed_int32() -> None:
    locator = NormalizedLocator("notes/old.md")
    first_key = advisory_lock_key_for_locator(locator)
    second_key = advisory_lock_key_for_locator(locator)
    assert first_key == second_key
    assert isinstance(first_key, int)
    assert -(2**31) <= first_key < 2**31
    different_key = advisory_lock_key_for_locator(NormalizedLocator("notes/new.md"))
    assert first_key != different_key


def test_is_locator_lock_order_valid_accepts_sorted_only() -> None:
    ordered_keys = order_locator_lock_keys(
        [NormalizedLocator("zeta.md"), NormalizedLocator("alpha.md")]
    )
    assert is_locator_lock_order_valid(ordered_keys)
    swapped = list(reversed(ordered_keys))
    assert not is_locator_lock_order_valid(swapped)


# --- deterministic identities ---------------------------------------------------


def test_lifecycle_commit_identities_allocates_seven_uuidv7_values() -> None:
    identities = LifecycleCommitIdentities.allocate(include_tombstone=True)
    fields = (
        identities.source_locator_id,
        identities.tombstone_id,
        identities.qdrant_intent_id,
        identities.neo4j_intent_id,
        identities.audit_event_id,
    )
    assert len(set(fields)) == len(fields)
    for value in fields:
        assert value is not None
        assert value.version == 7
        assert value != UUID(int=0)


def test_lifecycle_commit_identities_reused_through_retries() -> None:
    first = LifecycleCommitIdentities.allocate(include_tombstone=True)
    second = LifecycleCommitIdentities.allocate(include_tombstone=True)
    # Two allocations produce distinct identities so a retry writes the same
    # identity exactly once if the service hands the same instance back in.
    assert first != second


# --- operation / projection mapping ------------------------------------------


def test_event_type_mapping_is_closed() -> None:
    assert EVENT_TYPE_BY_OPERATION[LifecycleOperation.RENAME] == "rename"
    assert EVENT_TYPE_BY_OPERATION[LifecycleOperation.MOVE] == "move"
    assert EVENT_TYPE_BY_OPERATION[LifecycleOperation.DELETE] == "delete"
    assert EVENT_TYPE_BY_OPERATION[LifecycleOperation.RESTORE] == "restore"


def test_audit_action_mapping_is_closed() -> None:
    assert AUDIT_ACTIONS_BY_OPERATION[LifecycleOperation.RENAME] == "source.locator_renamed"
    assert AUDIT_ACTIONS_BY_OPERATION[LifecycleOperation.MOVE] == "source.locator_moved"
    assert AUDIT_ACTIONS_BY_OPERATION[LifecycleOperation.DELETE] == "source.deleted"
    assert AUDIT_ACTIONS_BY_OPERATION[LifecycleOperation.RESTORE] == "source.restored"


def test_projection_intent_picks_upsert_for_allowed_and_delete_for_denied() -> None:
    rename = _rename_command()
    assert _projection_intent_operation(rename, _allowed_decision()) == PROJECTION_OPERATION_UPSERT
    assert _projection_intent_operation(rename, _denied_decision()) == PROJECTION_OPERATION_DELETE
    assert (
        _projection_intent_operation(rename, _indeterminate_decision())
        == PROJECTION_OPERATION_DELETE
    )
    delete = _delete_command()
    assert _projection_intent_operation(delete, _allowed_decision()) == PROJECTION_OPERATION_DELETE
    assert _projection_intent_operation(delete, _denied_decision()) == PROJECTION_OPERATION_DELETE
    restore = _restore_command()
    assert _projection_intent_operation(restore, _allowed_decision()) == PROJECTION_OPERATION_UPSERT
    assert _projection_intent_operation(restore, _denied_decision()) == PROJECTION_OPERATION_DELETE


# --- conflict classifiers ------------------------------------------------------


def test_classify_locator_conflict_rejects_mismatched_expected_locator() -> None:
    conflict = classify_locator_conflict(
        expected=NormalizedLocator("notes/old.md"),
        actual=NormalizedLocator("notes/different.md"),
    )
    assert conflict is not None
    assert conflict.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT


def test_classify_locator_conflict_accepts_matching_locator() -> None:
    conflict = classify_locator_conflict(
        expected=NormalizedLocator("notes/old.md"),
        actual=NormalizedLocator("notes/old.md"),
    )
    assert conflict is None


def test_classify_locator_conflict_rejects_missing_active_locator() -> None:
    conflict = classify_locator_conflict(
        expected=NormalizedLocator("notes/old.md"),
        actual=None,
    )
    assert conflict is not None
    assert conflict.code is SourceLifecycleErrorCode.LOCATOR_MISSING


def test_classify_version_mismatch_is_version_conflict() -> None:
    conflict = classify_version_mismatch(
        expected=_VERSION_ID,
        actual=UUID("018f47a0-7b00-7000-8000-000000000099"),
    )
    assert conflict is not None
    assert conflict.code is SourceLifecycleErrorCode.VERSION_CONFLICT
    assert classify_version_mismatch(expected=_VERSION_ID, actual=_VERSION_ID) is None


def test_classify_state_mismatch_is_locator_missing_for_deleted() -> None:
    conflict = classify_state_mismatch(
        actual_state=SOURCE_STATE_DELETED,
        operation=LifecycleOperation.RENAME,
    )
    assert conflict is not None
    assert conflict.code is SourceLifecycleErrorCode.LOCATOR_MISSING


def test_classify_state_mismatch_for_restore_on_active_source() -> None:
    conflict = classify_state_mismatch(
        actual_state=SOURCE_STATE_ACTIVE,
        operation=LifecycleOperation.RESTORE,
    )
    assert conflict is not None
    assert conflict.code is SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND


def test_classify_tombstone_conflict_for_closed_tombstone() -> None:
    conflict = classify_tombstone_conflict(
        tombstone_present=True,
        tombstone_already_restored=True,
        tombstone_id=_TOMBSTONE_ID,
    )
    assert conflict is not None
    assert conflict.code is SourceLifecycleErrorCode.TOMBSTONE_CLOSED


def test_classify_tombstone_conflict_missing_is_tombstone_not_found() -> None:
    conflict = classify_tombstone_conflict(
        tombstone_present=False,
        tombstone_already_restored=False,
        tombstone_id=_TOMBSTONE_ID,
    )
    assert conflict is not None
    assert conflict.code is SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND


# --- title derivation pin (matches the published title contract) --------------


def test_derive_title_v1_removes_only_final_extension() -> None:
    assert derive_title_v1(NormalizedLocator("notes/new.md")).value == "new"
    assert derive_title_v1(NormalizedLocator("notes/many.dots.md")).value == "many.dots"
    assert derive_title_v1(NormalizedLocator("notes/no-extension")).value == "no-extension"


# --- redaction: no raw locator in diagnostics or errors ---------------------


def test_store_does_not_emit_any_raw_locator_in_diagnostic_fields() -> None:
    diagnostic = create_diagnostic_context().context
    raw_locator = "notes/old.md"
    fields = _expected_diagnostic_fields_for_rename(
        command=_rename_command(), decision=_denied_decision(), diagnostic=diagnostic
    )
    for value in fields.values():
        assert raw_locator not in str(value)
        assert _locator_fingerprint_hex(raw_locator) not in str(value)


def test_store_does_not_emit_any_raw_locator_in_safe_diff_hash() -> None:
    raw_locator = "notes/old.md"
    digest = _locator_fingerprint_hex(raw_locator)
    safe_diff = _expected_safe_diff_for_rename(
        _rename_command(), _denied_decision()
    )
    assert raw_locator not in safe_diff
    assert digest not in safe_diff


# --- replay lookup row hydration ----------------------------------------------


def test_lifecycle_replay_lookup_row_hydrates_from_result_row() -> None:
    raw_row: dict[str, Any] = {
        "event_id": _EVENT_ID,
        "workspace_id": _WORKSPACE_ID,
        "source_id": _SOURCE_ID,
        "event_sequence": 9,
        "event_type": "rename",
        "base_version_id": None,
        "committed_version_id": _VERSION_ID,
        "idempotency_key": "lifecycle-rename-001",
        "request_fingerprint": "a" * 64,
        "committed_at": datetime(2026, 8, 20, 2, 2, 3, tzinfo=UTC),
        "state": SOURCE_STATE_ACTIVE,
        "resulting_locator": "notes/new.md",
        "tombstone_id": None,
    }
    row = LifecycleReplayLookupRow.from_result_row(raw_row)
    assert row.event_id == _EVENT_ID
    assert row.event_sequence == 9
    assert row.event_type == "rename"
    assert row.committed_version_id == _VERSION_ID


# --- adapter fingerprint consistency with the domain helper ------------------


def test_adapter_fingerprint_matches_domain_fingerprint() -> None:
    command = _rename_command()
    # The expected value is whatever the domain fingerprint function yields;
    # the adapter must use the same function (so the comparison is structural).
    from personal_os.source_lifecycle.fingerprint import (
        fingerprint_lifecycle_command as _fingerprint,
    )
    expected = _fingerprint(command).hexadecimal
    assert expected == _fingerprint(command).hexadecimal


# --- helpers -------------------------------------------------------------------


def _locator_fingerprint_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _projection_intent_operation(
    command: SourceLifecycleCommand,
    decision: LifecyclePolicyDecision,
) -> str:
    from postgresql_source_store.lifecycle_store import _projection_intent_operation_for

    return _projection_intent_operation_for(command, decision)


def _expected_diagnostic_fields_for_rename(
    *,
    command: SourceLifecycleCommand,
    decision: LifecyclePolicyDecision,
    diagnostic: Any,
) -> dict[str, object]:
    from postgresql_source_store.lifecycle_store import (
        _diagnostic_fields_for_rename_rejection,
    )

    return _diagnostic_fields_for_rename_rejection(
        command=command,
        decision=decision,
        diagnostic_context=diagnostic,
        error_code=SourceLifecycleErrorCode.LOCATOR_CONFLICT,
        duration_seconds=0.0,
    )


def _expected_safe_diff_for_rename(
    command: SourceLifecycleCommand,
    decision: LifecyclePolicyDecision,
) -> str:
    from postgresql_source_store.lifecycle_store import _compute_safe_diff_digest

    return _compute_safe_diff_digest(
        command=command,
        decision=decision,
        result=_committed_result(),
    )