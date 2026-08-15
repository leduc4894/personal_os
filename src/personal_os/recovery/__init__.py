"""Public canonical recovery contracts, manifest codec and provider-neutral ports.

Typed recovery error with its closed reason/component/dependency tokens, the
canonical manifest value types and strict encode/parse codec, the dump
process, snapshot-store and bundle-store ports, and the closed backup metric
contract with its bounded in-memory sink. The modules import no infrastructure
SDK, driver or subprocess primitive.
"""

from personal_os.recovery.contracts import (
    CANONICAL_BACKUP_METRIC_CONTRACTS,
    CANONICAL_COUNT_TABLES,
    MANIFEST_CONTRACT,
    MAXIMUM_OBJECT_SIZE_BYTES,
    POSTGRESQL_SCHEMA_REVISION,
    POSTGRESQL_SERVER_VERSION,
    RECOVERY_BUNDLE_INVALID_REASONS,
    RECOVERY_COMPONENTS,
    RECOVERY_CONFIGURATION_REASONS,
    RECOVERY_DEPENDENCIES,
    CanonicalBackupMetrics,
    CanonicalBackupRecord,
    InMemoryCanonicalBackupMetrics,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryBundleInvalidReason,
    RecoveryComponent,
    RecoveryConfigurationReason,
    RecoveryDependency,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
    RecoveryMetricOutcome,
    RecoveryOperation,
)
from personal_os.recovery.manifest import (
    encode_manifest,
    format_manifest_timestamp,
    manifest_digest,
    parse_manifest,
)
from personal_os.recovery.ports import (
    CanonicalBackupSnapshot,
    CanonicalBackupSnapshotStore,
    DumpReceipt,
    PostgresqlConnectionTarget,
    PostgresqlDumpProcess,
    RecoveryBundleStore,
    RecoveryBundleWriter,
    RestoreReceipt,
    VerifiedRecoveryBundle,
)

__all__ = [
    "CANONICAL_BACKUP_METRIC_CONTRACTS",
    "CANONICAL_COUNT_TABLES",
    "MANIFEST_CONTRACT",
    "MAXIMUM_OBJECT_SIZE_BYTES",
    "POSTGRESQL_SCHEMA_REVISION",
    "POSTGRESQL_SERVER_VERSION",
    "RECOVERY_BUNDLE_INVALID_REASONS",
    "RECOVERY_COMPONENTS",
    "RECOVERY_CONFIGURATION_REASONS",
    "RECOVERY_DEPENDENCIES",
    "CanonicalBackupMetrics",
    "CanonicalBackupRecord",
    "CanonicalBackupSnapshot",
    "CanonicalBackupSnapshotStore",
    "DumpReceipt",
    "InMemoryCanonicalBackupMetrics",
    "ManifestDumpEntry",
    "ManifestObjectEntry",
    "PostgresqlConnectionTarget",
    "PostgresqlDumpProcess",
    "RecoveryBundleInvalidReason",
    "RecoveryBundleStore",
    "RecoveryBundleWriter",
    "RecoveryComponent",
    "RecoveryConfigurationReason",
    "RecoveryDependency",
    "RecoveryEnvironment",
    "RecoveryError",
    "RecoveryManifest",
    "RecoveryMetricOutcome",
    "RecoveryOperation",
    "RestoreReceipt",
    "VerifiedRecoveryBundle",
    "encode_manifest",
    "format_manifest_timestamp",
    "manifest_digest",
    "parse_manifest",
]
