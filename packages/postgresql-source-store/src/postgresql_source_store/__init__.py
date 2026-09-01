"""PostgreSQL source-version store adapter package.

This package implements the core source publication contracts (idempotent
preflight and replay hydration now; version commits and citation lookups in
later tasks) and the atomic identity bootstrap transaction over the canonical
PostgreSQL baseline, plus the canonical database readiness probe
(:class:`PostgresqlReadinessProbe`). The composition root builds the engine
through :mod:`postgresql_source_store.engine` and constructs
:class:`PostgresqlSourcePublicationStore` and
:class:`PostgresqlIdentityBootstrapStore` directly.
"""

from postgresql_source_store.backup_snapshot import (
    PostgresqlBackupSnapshotStore,
    PostgresqlRestoreTarget,
)
from postgresql_source_store.canonical_read import (
    ACCEPTED_READ_SOURCE_STATES,
    PostgresqlCanonicalSourceReadStore,
    hydrate_canonical_source_reference,
)
from postgresql_source_store.conflict_store import (
    PostgresqlSourceConflictStore,
    SourceConflictDatabaseRetryPolicy,
    hydrate_source_conflict,
)
from postgresql_source_store.device_content_catalog import (
    PostgresqlDeviceContentCatalog,
)
from postgresql_source_store.device_event_store import (
    PostgresqlDeviceEventStore,
    hydrate_device_event,
)
from postgresql_source_store.device_manifest_store import (
    PostgresqlDeviceManifestStore,
    compute_manifest_final_digest,
)
from postgresql_source_store.error_mapping import (
    DatabaseRetryPolicy,
    map_database_failure,
)
from postgresql_source_store.identity_bootstrap import PostgresqlIdentityBootstrapStore
from postgresql_source_store.multipart_upload_store import (
    MULTIPART_COMPLETION_LEASE_SECONDS,
    MultipartDatabaseRetryPolicy,
    PostgresqlMultipartSessionEvidenceStore,
    PostgresqlMultipartUploadStore,
)
from postgresql_source_store.policy_drafts import PostgresqlPolicyDraftStore
from postgresql_source_store.policy_keysets import PostgresqlPolicyKeysetStore
from postgresql_source_store.projection_intents import (
    PostgresqlProjectionIntentStore,
    ProjectionDiagnosticSink,
    ProjectionIntentStatus,
    ProjectionRetryPolicy,
)
from postgresql_source_store.publication_store import (
    PostgresqlSourcePublicationStore,
    classify_replay,
)
from postgresql_source_store.readiness import PostgresqlReadinessProbe
from postgresql_source_store.small_file_sync_operations import (
    UPLOAD_OPERATION_EXPIRY_SECONDS,
    PostgresqlSmallFileUploadOperationStore,
)
from postgresql_source_store.tables import SOURCE_STORE_TABLES

__all__ = [
    "ACCEPTED_READ_SOURCE_STATES",
    "MULTIPART_COMPLETION_LEASE_SECONDS",
    "SOURCE_STORE_TABLES",
    "UPLOAD_OPERATION_EXPIRY_SECONDS",
    "DatabaseRetryPolicy",
    "MultipartDatabaseRetryPolicy",
    "PostgresqlBackupSnapshotStore",
    "PostgresqlCanonicalSourceReadStore",
    "PostgresqlDeviceContentCatalog",
    "PostgresqlDeviceEventStore",
    "PostgresqlDeviceManifestStore",
    "PostgresqlIdentityBootstrapStore",
    "PostgresqlMultipartSessionEvidenceStore",
    "PostgresqlMultipartUploadStore",
    "PostgresqlPolicyDraftStore",
    "PostgresqlPolicyKeysetStore",
    "PostgresqlProjectionIntentStore",
    "PostgresqlReadinessProbe",
    "PostgresqlRestoreTarget",
    "PostgresqlSmallFileUploadOperationStore",
    "PostgresqlSourceConflictStore",
    "PostgresqlSourcePublicationStore",
    "ProjectionDiagnosticSink",
    "ProjectionIntentStatus",
    "ProjectionRetryPolicy",
    "SourceConflictDatabaseRetryPolicy",
    "classify_replay",
    "compute_manifest_final_digest",
    "hydrate_canonical_source_reference",
    "hydrate_device_event",
    "hydrate_source_conflict",
    "map_database_failure",
]
