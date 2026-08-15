"""Public source-publication and canonical-read domain contracts.

Immutable actors, commands and publication results, the request-fingerprint
and publication ports, the provider-neutral publication service, and the
fail-closed canonical current-source read service. The modules reuse the
canonical object-storage value objects and import no infrastructure SDK,
composition root or provider package.
"""

from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
    UpdateSourceVersion,
)
from personal_os.sources.fingerprint import (
    RequestFingerprint,
    SourceVersionCommand,
    compute_request_fingerprint,
)
from personal_os.sources.metrics import (
    CanonicalReadMetrics,
    InMemoryCanonicalReadMetrics,
    InMemorySourcePublicationMetrics,
    PublicationMetricOutcome,
    PublicationOperation,
    PublicationRejectionReason,
    ReadOutcome,
    SourcePublicationMetrics,
)
from personal_os.sources.ports import AwareUtcClock, SourcePublicationStore
from personal_os.sources.publication import (
    MAXIMUM_RECEIPT_AGE,
    REJECTION_REASON_BY_ERROR_CODE,
    SourceVersionPublicationService,
)
from personal_os.sources.reading import (
    CanonicalReadStateError,
    CanonicalSourceReadService,
    CanonicalSourceReadStore,
    CanonicalSourceReference,
    ReadCurrentSourceCommand,
    canonical_read_failed_event_fields,
    canonical_read_succeeded_event_fields,
    validate_read_current_source_command,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult

__all__ = [
    "MAXIMUM_RECEIPT_AGE",
    "REJECTION_REASON_BY_ERROR_CODE",
    "ActorKind",
    "AwareUtcClock",
    "CanonicalReadMetrics",
    "CanonicalReadStateError",
    "CanonicalSourceReadService",
    "CanonicalSourceReadStore",
    "CanonicalSourceReference",
    "CreateSourceVersion",
    "IdempotencyKey",
    "InMemoryCanonicalReadMetrics",
    "InMemorySourcePublicationMetrics",
    "PublicationMetricOutcome",
    "PublicationOperation",
    "PublicationOutcome",
    "PublicationRejectionReason",
    "ReadCurrentSourceCommand",
    "ReadOutcome",
    "RequestFingerprint",
    "SourceActor",
    "SourcePublicationMetrics",
    "SourcePublicationStore",
    "SourceTitle",
    "SourceType",
    "SourceVersionCommand",
    "SourceVersionPublicationResult",
    "SourceVersionPublicationService",
    "UpdateSourceVersion",
    "canonical_read_failed_event_fields",
    "canonical_read_succeeded_event_fields",
    "compute_request_fingerprint",
    "validate_read_current_source_command",
]
