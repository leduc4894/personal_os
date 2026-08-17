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

# The diagnostics import below precedes every error-contracts import: the core
# packages have a module-level re-export cycle (``error_contracts.exceptions``
# initializes ``personal_os.diagnostics``, whose ``context`` module imports
# exceptions back), which only resolves when the diagnostics package loads
# first. The R2 adapter's import graph relies on the same ordering.
from personal_os.diagnostics.events import SafeToken  # noqa: F401
from postgresql_source_store.backup_snapshot import (
    PostgresqlBackupSnapshotStore,
    PostgresqlRestoreTarget,
)
from postgresql_source_store.canonical_read import (
    ACCEPTED_READ_SOURCE_STATES,
    PostgresqlCanonicalSourceReadStore,
    hydrate_canonical_source_reference,
)
from postgresql_source_store.error_mapping import (
    DatabaseRetryPolicy,
    map_database_failure,
)
from postgresql_source_store.identity_bootstrap import PostgresqlIdentityBootstrapStore
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
from postgresql_source_store.tables import SOURCE_STORE_TABLES

__all__ = [
    "ACCEPTED_READ_SOURCE_STATES",
    "SOURCE_STORE_TABLES",
    "DatabaseRetryPolicy",
    "PostgresqlBackupSnapshotStore",
    "PostgresqlCanonicalSourceReadStore",
    "PostgresqlIdentityBootstrapStore",
    "PostgresqlPolicyDraftStore",
    "PostgresqlPolicyKeysetStore",
    "PostgresqlProjectionIntentStore",
    "PostgresqlReadinessProbe",
    "PostgresqlRestoreTarget",
    "PostgresqlSourcePublicationStore",
    "ProjectionDiagnosticSink",
    "ProjectionIntentStatus",
    "ProjectionRetryPolicy",
    "classify_replay",
    "hydrate_canonical_source_reference",
    "map_database_failure",
]
