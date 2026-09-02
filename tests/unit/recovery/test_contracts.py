"""Closed recovery error contract, token sets and metric contracts.

Asserts the exact ten-code recovery table (category, retryability, safe-detail
allowlist), the closed reason/component/dependency token sets, the backup
metric contract pin and the bounded in-memory backup metric sink behaviour.
"""

from __future__ import annotations

import math

import pytest

from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION
from personal_os.diagnostics.events import EVENT_DEFINITIONS, DiagnosticLevel, EventName, ResultCode
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode
from personal_os.recovery.contracts import (
    CANONICAL_BACKUP_METRIC_CONTRACTS,
    POSTGRESQL_SCHEMA_REVISION,
    RECOVERY_BUNDLE_INVALID_REASONS,
    RECOVERY_COMPONENTS,
    RECOVERY_CONFIGURATION_REASONS,
    RECOVERY_DEPENDENCIES,
    InMemoryCanonicalBackupMetrics,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryMetricOutcome,
    RecoveryOperation,
)
from personal_os.recovery.ports import CanonicalBackupSnapshot

#: The exact spec-15 recovery table: code -> (category, retryable, allowed details).
RECOVERY_ERROR_TABLE = {
    "canonical_recovery_environment_refused": (
        ErrorCategory.AUTHORIZATION,
        False,
        frozenset({"operation"}),
    ),
    "canonical_recovery_admission_refused": (
        ErrorCategory.AUTHORIZATION,
        False,
        frozenset({"operation"}),
    ),
    "canonical_recovery_configuration_invalid": (
        ErrorCategory.CONFIGURATION,
        False,
        frozenset({"reason"}),
    ),
    "canonical_recovery_snapshot_busy": (ErrorCategory.DEPENDENCY, True, frozenset()),
    "canonical_recovery_bundle_exists": (
        ErrorCategory.CONFLICT,
        False,
        frozenset({"bundle_id"}),
    ),
    "canonical_recovery_bundle_invalid": (
        ErrorCategory.INTEGRITY,
        False,
        frozenset({"reason"}),
    ),
    "canonical_recovery_target_not_empty": (ErrorCategory.CONFLICT, False, frozenset()),
    "canonical_recovery_dependency_unavailable": (
        ErrorCategory.DEPENDENCY,
        True,
        frozenset({"dependency"}),
    ),
    "canonical_recovery_integrity_failed": (
        ErrorCategory.INTEGRITY,
        False,
        frozenset({"component"}),
    ),
    "canonical_recovery_restore_failed": (
        ErrorCategory.INTEGRITY,
        False,
        frozenset({"component"}),
    ),
}


def test_recovery_error_code_set_is_closed() -> None:
    assert {code.value for code in RecoveryError.allowed_codes} == set(RECOVERY_ERROR_TABLE)
    assert RecoveryError.allowed_codes <= frozenset(ErrorCode)


def test_recovery_error_registry_category_retryability_and_details_are_fixed() -> None:
    for value, (category, retryable, allowed_details) in RECOVERY_ERROR_TABLE.items():
        definition = ERROR_DEFINITIONS[ErrorCode(value)]
        assert definition.category is category, value
        assert definition.is_retryable is retryable, value
        assert definition.allowed_detail_fields == allowed_details, value


def test_recovery_error_rejects_codes_outside_the_closed_set() -> None:
    with pytest.raises(ValueError, match="not valid for this exception type"):
        RecoveryError(ErrorCode.CONFIGURATION_INVALID)


def test_recovery_environment_is_closed() -> None:
    assert {member.value for member in RecoveryEnvironment} == {"local", "test"}


def test_canonical_backup_snapshot_repr_redacts_snapshot_token() -> None:
    snapshot = CanonicalBackupSnapshot(
        snapshot_token="snapshot-token",
        server_version="18.4",
        schema_head=POSTGRESQL_SCHEMA_REVISION,
        table_counts={},
        referenced_objects=(),
    )

    assert "snapshot-token" not in repr(snapshot)


def test_postgresql_schema_revision_alias_keeps_database_schema_authority() -> None:
    # Authority moved to ``personal_os.database_schema``; the recovery-side
    # name must keep resolving to the identical constant object. The head is
    # the source-conflict revision ``20260902_01``.
    assert CANONICAL_POSTGRESQL_SCHEMA_REVISION == "20260902_01"
    assert POSTGRESQL_SCHEMA_REVISION == "20260902_01"
    assert POSTGRESQL_SCHEMA_REVISION is CANONICAL_POSTGRESQL_SCHEMA_REVISION


def test_recovery_reason_tokens_are_closed() -> None:
    assert (
        frozenset(
            {
                "environment_not_allowed",
                "backup_root_not_absolute",
                "schema_head_mismatch",
                "free_space_reserve",
                "client_tools_unavailable",
                "target_not_empty",
            }
        )
        == RECOVERY_CONFIGURATION_REASONS
    )
    assert (
        frozenset(
            {
                "contract_unsupported",
                "json_noncanonical",
                "duplicate_json_key",
                "bundle_id_invalid",
                "timestamp_invalid",
                "field_unknown",
                "field_invalid",
                "entries_unsorted",
                "digest_duplicate",
                "path_key_mismatch",
                "sidecar_missing",
                "file_tree_mismatch",
                "file_changed",
                "checksum_mismatch",
            }
        )
        == RECOVERY_BUNDLE_INVALID_REASONS
    )
    assert (
        frozenset(
            {
                "postgres_dump",
                "postgres_restore",
                "object_set",
                "bundle",
                "canonical_graph",
                "canonical_read",
            }
        )
        == RECOVERY_COMPONENTS
    )
    assert frozenset({"postgresql", "r2", "temporal", "pg_client"}) == RECOVERY_DEPENDENCIES


def test_recovery_metric_contracts_match_design() -> None:
    assert {
        "canonical_backup_total": frozenset({"operation", "outcome"}),
        "canonical_backup_duration_seconds": frozenset({"operation", "outcome"}),
        "canonical_backup_objects": frozenset({"operation", "outcome"}),
        "canonical_backup_bytes": frozenset({"operation", "outcome"}),
    } == CANONICAL_BACKUP_METRIC_CONTRACTS


def test_recovery_operation_and_outcome_labels_are_closed() -> None:
    assert {member.value for member in RecoveryOperation} == {"create", "verify", "restore"}
    assert {member.value for member in RecoveryMetricOutcome} == {"succeeded", "failed"}


def test_backup_and_restore_events_match_design_registry() -> None:
    created = EVENT_DEFINITIONS[EventName.CANONICAL_BACKUP_CREATED]
    assert created.level is DiagnosticLevel.INFO
    assert created.result_code is ResultCode.SUCCEEDED
    assert created.required_fields == frozenset(
        {"operation", "outcome", "duration_ms", "bundle_id"}
    )
    assert created.allowed_fields == frozenset(
        {"operation", "outcome", "duration_ms", "bundle_id", "object_count", "byte_total"}
    )

    verified = EVENT_DEFINITIONS[EventName.CANONICAL_BACKUP_VERIFIED]
    assert verified.level is DiagnosticLevel.INFO
    assert verified.result_code is ResultCode.SUCCEEDED
    assert verified.required_fields == frozenset(
        {"operation", "outcome", "duration_ms", "bundle_id"}
    )
    assert verified.allowed_fields == frozenset(
        {"operation", "outcome", "duration_ms", "bundle_id", "object_count", "byte_total"}
    )

    restore_succeeded = EVENT_DEFINITIONS[EventName.CANONICAL_RESTORE_SUCCEEDED]
    assert restore_succeeded.level is DiagnosticLevel.INFO
    assert restore_succeeded.result_code is ResultCode.SUCCEEDED
    assert restore_succeeded.required_fields == frozenset(
        {"operation", "outcome", "duration_ms", "bundle_id"}
    )
    assert restore_succeeded.allowed_fields == frozenset(
        {"operation", "outcome", "duration_ms", "bundle_id", "object_count", "byte_total"}
    )

    backup_failed = EVENT_DEFINITIONS[EventName.CANONICAL_BACKUP_FAILED]
    assert backup_failed.level is DiagnosticLevel.ERROR
    assert backup_failed.result_code is ResultCode.FAILED
    assert backup_failed.required_fields == frozenset({"error_code"})
    assert backup_failed.allowed_fields == frozenset(
        {"operation", "outcome", "duration_ms", "bundle_id", "error_code"}
    )

    restore_failed = EVENT_DEFINITIONS[EventName.CANONICAL_RESTORE_FAILED]
    assert restore_failed.level is DiagnosticLevel.ERROR
    assert restore_failed.result_code is ResultCode.FAILED
    assert restore_failed.required_fields == frozenset({"error_code"})
    assert restore_failed.allowed_fields == frozenset(
        {"operation", "outcome", "duration_ms", "bundle_id", "error_code"}
    )


def test_in_memory_backup_metrics_record_and_count_closed_labels() -> None:
    sink = InMemoryCanonicalBackupMetrics()
    sink.record_backup(
        operation=RecoveryOperation.CREATE,
        outcome=RecoveryMetricOutcome.SUCCEEDED,
        duration_seconds=1.5,
        object_count=2,
        byte_total=2048,
    )
    assert sink.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.SUCCEEDED) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"duration_seconds": -0.5},
        {"duration_seconds": math.nan},
        {"duration_seconds": math.inf},
        {"object_count": -1},
        {"byte_total": -1},
    ],
)
def test_in_memory_backup_metrics_reject_invalid_values(kwargs: dict[str, float]) -> None:
    sink = InMemoryCanonicalBackupMetrics()
    base = {
        "operation": RecoveryOperation.CREATE,
        "outcome": RecoveryMetricOutcome.SUCCEEDED,
        "duration_seconds": 1.0,
        "object_count": 0,
        "byte_total": 0,
    }
    with pytest.raises(ValueError):
        sink.record_backup(**{**base, **kwargs})


def test_in_memory_backup_metrics_reject_non_enum_labels() -> None:
    sink = InMemoryCanonicalBackupMetrics()
    with pytest.raises(ValueError, match="closed enum member"):
        sink.record_backup(
            operation="create",  # type: ignore[arg-type]
            outcome=RecoveryMetricOutcome.SUCCEEDED,
            duration_seconds=1.0,
            object_count=0,
            byte_total=0,
        )
