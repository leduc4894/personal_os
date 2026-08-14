"""PostgreSQL source-version store adapter package.

This package implements the core source publication contracts (idempotent
preflight and replay hydration now; version commits and citation lookups in
later tasks) over the canonical PostgreSQL baseline. The composition root
builds the engine through :mod:`postgresql_source_store.engine` and constructs
:class:`PostgresqlSourcePublicationStore` directly.
"""

# The diagnostics import below precedes every error-contracts import: the core
# packages have a module-level re-export cycle (``error_contracts.exceptions``
# initializes ``personal_os.diagnostics``, whose ``context`` module imports
# exceptions back), which only resolves when the diagnostics package loads
# first. The R2 adapter's import graph relies on the same ordering.
from personal_os.diagnostics.events import SafeToken  # noqa: F401
from postgresql_source_store.error_mapping import (
    DatabaseRetryPolicy,
    map_database_failure,
)
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

__all__ = [
    "DatabaseRetryPolicy",
    "PostgresqlProjectionIntentStore",
    "PostgresqlSourcePublicationStore",
    "ProjectionDiagnosticSink",
    "ProjectionIntentStatus",
    "ProjectionRetryPolicy",
    "classify_replay",
    "map_database_failure",
]
