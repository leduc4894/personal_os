"""Recovery value contracts: closed tokens, typed error and manifest dataclasses.

This module is provider-neutral: it imports no database driver, cloud SDK,
subprocess or filesystem primitive. Safe ``reason``, ``component`` and
``dependency`` values are closed :class:`enum.StrEnum` members, never provider
text, paths or raw bytes. :data:`CANONICAL_BACKUP_METRIC_CONTRACTS` pins the
exact metric names and label dimensions; no ID, path, hash, key, source type,
media type or error message is ever a metric label.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

#: Historical and current manifest contract identifiers; never guessed.
MANIFEST_CONTRACT_V1: Final[str] = "canonical_core_backup/v1"
MANIFEST_CONTRACT_V2: Final[str] = "canonical_core_backup/v2"
MANIFEST_CONTRACT: Final[str] = MANIFEST_CONTRACT_V2

#: Compatibility alias: revision authority lives in
#: :mod:`personal_os.database_schema`; existing recovery imports keep
#: resolving to the identical constant.
POSTGRESQL_SCHEMA_REVISION: Final[str] = CANONICAL_POSTGRESQL_SCHEMA_REVISION
POSTGRESQL_SERVER_VERSION: Final[str] = "18.4"

#: Upper bound for one canonical object admitted into a bundle (100 MiB).
MAXIMUM_OBJECT_SIZE_BYTES: Final[int] = 104_857_600

#: The exact historical v1 table-count set. This public shape is immutable.
V1_CANONICAL_COUNT_TABLES: Final[tuple[str, ...]] = (
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

#: The exact branch-local v2 shape emitted before authentication completeness.
#: Readers retain this closed shape so already-created v2 bundles remain
#: verifiable and restorable; writers never emit it after the completeness fix.
LEGACY_V2_CANONICAL_COUNT_TABLES: Final[tuple[str, ...]] = (
    "users",
    "workspaces",
    "devices",
    "content_objects",
    "sources",
    "source_versions",
    "sync_events",
    "projection_intents",
    "audit_events",
    "workspace_policy_state",
    "policy_signing_keys",
    "policy_keysets",
    "policy_keyset_signatures",
    "source_policies",
    "policy_rules",
    "policy_drafts",
    "policy_draft_rules",
    "policy_evaluations",
    "policy_reconciliation_intents",
    "small_file_upload_operations",
)

#: The exact closed set of canonical tables counted in every new v2 manifest.
#: Mirrors the snapshot lock order. Authentication rows are PostgreSQL
#: canonical state just like baseline, policy and durable upload-operation
#: rows; omitting any of them would leave the restored graph unwitnessed.
CANONICAL_COUNT_TABLES: Final[tuple[str, ...]] = (
    "users",
    "workspaces",
    "devices",
    "content_objects",
    "sources",
    "source_versions",
    "sync_events",
    "projection_intents",
    "audit_events",
    "user_credentials",
    "web_sessions",
    "totp_credentials",
    "totp_recovery_codes",
    "device_token_families",
    "device_tokens",
    "device_authorization_grants",
    "authentication_throttle_buckets",
    "workspace_policy_state",
    "policy_signing_keys",
    "policy_keysets",
    "policy_keyset_signatures",
    "source_policies",
    "policy_rules",
    "policy_drafts",
    "policy_draft_rules",
    "policy_evaluations",
    "policy_reconciliation_intents",
    "small_file_upload_operations",
)

#: Maximum number of retained backup records. The recorder is a bounded ring
#: buffer for tests and standalone runs, never an unbounded audit log.
_MAXIMUM_BACKUP_RECORDS: Final[int] = 4096


class RecoveryEnvironment(StrEnum):
    """The closed environments a recovery command may target."""

    LOCAL = "local"
    TEST = "test"


class RecoveryConfigurationReason(StrEnum):
    """Closed ``reason`` tokens for ``canonical_recovery_configuration_invalid``."""

    ENVIRONMENT_NOT_ALLOWED = "environment_not_allowed"
    BACKUP_ROOT_NOT_ABSOLUTE = "backup_root_not_absolute"
    SCHEMA_HEAD_MISMATCH = "schema_head_mismatch"
    FREE_SPACE_RESERVE = "free_space_reserve"
    CLIENT_TOOLS_UNAVAILABLE = "client_tools_unavailable"
    TARGET_NOT_EMPTY = "target_not_empty"


class RecoveryBundleInvalidReason(StrEnum):
    """Closed ``reason`` tokens for ``canonical_recovery_bundle_invalid``."""

    CONTRACT_UNSUPPORTED = "contract_unsupported"
    JSON_NONCANONICAL = "json_noncanonical"
    DUPLICATE_JSON_KEY = "duplicate_json_key"
    BUNDLE_ID_INVALID = "bundle_id_invalid"
    TIMESTAMP_INVALID = "timestamp_invalid"
    FIELD_UNKNOWN = "field_unknown"
    FIELD_INVALID = "field_invalid"
    ENTRIES_UNSORTED = "entries_unsorted"
    DIGEST_DUPLICATE = "digest_duplicate"
    PATH_KEY_MISMATCH = "path_key_mismatch"
    SIDECAR_MISSING = "sidecar_missing"
    FILE_TREE_MISMATCH = "file_tree_mismatch"
    FILE_CHANGED = "file_changed"
    CHECKSUM_MISMATCH = "checksum_mismatch"


class RecoveryComponent(StrEnum):
    """Closed ``component`` tokens for integrity and restore failures."""

    POSTGRES_DUMP = "postgres_dump"
    POSTGRES_RESTORE = "postgres_restore"
    OBJECT_SET = "object_set"
    BUNDLE = "bundle"
    CANONICAL_GRAPH = "canonical_graph"
    CANONICAL_READ = "canonical_read"


class RecoveryDependency(StrEnum):
    """Closed ``dependency`` tokens for retryable dependency failures."""

    POSTGRESQL = "postgresql"
    R2 = "r2"
    TEMPORAL = "temporal"
    PG_CLIENT = "pg_client"


#: Closed reason tokens accepted by ``canonical_recovery_configuration_invalid``.
RECOVERY_CONFIGURATION_REASONS: Final[frozenset[str]] = frozenset(
    member.value for member in RecoveryConfigurationReason
)

#: Closed reason tokens accepted by ``canonical_recovery_bundle_invalid``.
RECOVERY_BUNDLE_INVALID_REASONS: Final[frozenset[str]] = frozenset(
    member.value for member in RecoveryBundleInvalidReason
)

#: Closed component tokens accepted by the recovery integrity codes.
RECOVERY_COMPONENTS: Final[frozenset[str]] = frozenset(member.value for member in RecoveryComponent)

#: Closed dependency tokens accepted by ``canonical_recovery_dependency_unavailable``.
RECOVERY_DEPENDENCIES: Final[frozenset[str]] = frozenset(
    member.value for member in RecoveryDependency
)

#: Safe-token grammar guards: every closed token parses as a registered
#: diagnostic token, so no reason, component or dependency can smuggle caller
#: text into safe details.
RECOVERY_CONFIGURATION_REASON_TOKENS: Final[tuple[SafeToken, ...]] = tuple(
    SafeToken.parse(member.value) for member in RecoveryConfigurationReason
)
RECOVERY_BUNDLE_INVALID_REASON_TOKENS: Final[tuple[SafeToken, ...]] = tuple(
    SafeToken.parse(member.value) for member in RecoveryBundleInvalidReason
)
RECOVERY_COMPONENT_TOKENS: Final[tuple[SafeToken, ...]] = tuple(
    SafeToken.parse(member.value) for member in RecoveryComponent
)
RECOVERY_DEPENDENCY_TOKENS: Final[tuple[SafeToken, ...]] = tuple(
    SafeToken.parse(member.value) for member in RecoveryDependency
)


class RecoveryError(ApplicationError):
    """Typed recovery error bound to the closed nine-code recovery set."""

    allowed_codes: frozenset[ErrorCode] = frozenset(
        {
            ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED,
            ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID,
            ErrorCode.CANONICAL_RECOVERY_SNAPSHOT_BUSY,
            ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS,
            ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID,
            ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY,
            ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE,
            ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED,
            ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED,
        }
    )


@dataclass(frozen=True, slots=True)
class ManifestDumpEntry:
    """The single ``postgres.dump`` sidecar described by a manifest."""

    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ManifestObjectEntry:
    """One content-addressed canonical object admitted into a bundle."""

    content_sha256: str
    object_key: str
    size_bytes: int
    media_type: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class RecoveryManifest:
    """Validated bundle manifest; ``canonical_counts`` is a frozen mapping."""

    bundle_id: UUID
    created_at: datetime
    source_environment: str
    postgresql_server_version: str
    postgresql_schema_revision: str
    postgres_dump: ManifestDumpEntry
    canonical_counts: Mapping[str, int]
    objects: tuple[ManifestObjectEntry, ...]
    contract: str = MANIFEST_CONTRACT


class RecoveryOperation(StrEnum):
    """The closed recovery operations used as event and metric labels."""

    CREATE = "create"
    VERIFY = "verify"
    RESTORE = "restore"


class RecoveryMetricOutcome(StrEnum):
    """The closed recovery outcomes used as metric labels."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: The exact required metric names and their label dimensions (spec 16.2).
CANONICAL_BACKUP_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "canonical_backup_total": frozenset({"operation", "outcome"}),
        "canonical_backup_duration_seconds": frozenset({"operation", "outcome"}),
        "canonical_backup_objects": frozenset({"operation", "outcome"}),
        "canonical_backup_bytes": frozenset({"operation", "outcome"}),
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalBackupRecord:
    """One recorded backup/verify/restore outcome.

    Carries only the closed operation/outcome enums, a finite non-negative
    duration and non-negative totals; never a UUID, path, hash or key.
    """

    operation: RecoveryOperation
    outcome: RecoveryMetricOutcome
    duration_seconds: float
    object_count: int
    byte_total: int


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


@runtime_checkable
class CanonicalBackupMetrics(Protocol):
    """Low-cardinality canonical backup/restore metrics sink (spec 16.2)."""

    def record_backup(
        self,
        *,
        operation: RecoveryOperation,
        outcome: RecoveryMetricOutcome,
        duration_seconds: float,
        object_count: int,
        byte_total: int,
    ) -> None:
        """Record one completed recovery operation outcome."""
        ...


class InMemoryCanonicalBackupMetrics:
    """Bounded in-memory sink implementing :class:`CanonicalBackupMetrics`.

    Sufficient for tests and standalone acceptance runs without introducing
    Prometheus. It keeps at most :data:`_MAXIMUM_BACKUP_RECORDS` records in a
    ring buffer keyed only by the closed enums, and rejects non-finite or
    negative durations, negative totals and any non-enum label so a UUID, path,
    hash or key can never become a label.
    """

    def __init__(self) -> None:
        self._records: deque[CanonicalBackupRecord] = deque(maxlen=_MAXIMUM_BACKUP_RECORDS)

    def record_backup(
        self,
        *,
        operation: RecoveryOperation,
        outcome: RecoveryMetricOutcome,
        duration_seconds: float,
        object_count: int,
        byte_total: int,
    ) -> None:
        _validate_label("operation", RecoveryOperation, operation)
        _validate_label("outcome", RecoveryMetricOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        if object_count < 0:
            raise ValueError("object_count must be non-negative")
        if byte_total < 0:
            raise ValueError("byte_total must be non-negative")
        self._records.append(
            CanonicalBackupRecord(
                operation=operation,
                outcome=outcome,
                duration_seconds=duration_seconds,
                object_count=object_count,
                byte_total=byte_total,
            )
        )

    def backup_records(self) -> list[CanonicalBackupRecord]:
        """A snapshot list of recorded outcomes (oldest first)."""

        return list(self._records)

    def backup_count(self, operation: RecoveryOperation, outcome: RecoveryMetricOutcome) -> int:
        return sum(
            1
            for record in self._records
            if record.operation is operation and record.outcome is outcome
        )

    def __repr__(self) -> str:
        return "InMemoryCanonicalBackupMetrics(redacted)"


class AcceptanceMetricOutcome(StrEnum):
    """The closed phase-one acceptance outcomes used as metric labels."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: The exact required acceptance metric name and its label dimensions (spec 18):
#: only the closed outcome label, never an ID, key, digest or error message.
CANONICAL_ACCEPTANCE_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {"canonical_acceptance_total": frozenset({"outcome"})}
)


@runtime_checkable
class CanonicalAcceptanceMetrics(Protocol):
    """Low-cardinality phase-one acceptance metrics sink (spec 18)."""

    def record_acceptance(self, *, outcome: AcceptanceMetricOutcome) -> None:
        """Record one acceptance-run outcome under ``canonical_acceptance_total``."""
        ...


class InMemoryCanonicalAcceptanceMetrics:
    """Bounded in-memory sink implementing :class:`CanonicalAcceptanceMetrics`.

    Sufficient for the repository-internal acceptance CLI without introducing
    Prometheus. It counts only the closed outcome enum, so no UUID, key,
    digest, title or error message can ever become a label.
    """

    def __init__(self) -> None:
        self._counts: dict[AcceptanceMetricOutcome, int] = {}

    def record_acceptance(self, *, outcome: AcceptanceMetricOutcome) -> None:
        _validate_label("outcome", AcceptanceMetricOutcome, outcome)
        self._counts[outcome] = self._counts.get(outcome, 0) + 1

    def acceptance_count(self, outcome: AcceptanceMetricOutcome) -> int:
        return self._counts.get(outcome, 0)

    def __repr__(self) -> str:
        return "InMemoryCanonicalAcceptanceMetrics(redacted)"
