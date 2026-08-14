"""Public source-publication domain contracts.

Immutable actors, commands and publication results, the request-fingerprint
and publication ports, and the provider-neutral publication service. The
modules reuse the canonical object-storage value objects and import no
infrastructure SDK, composition root or provider package.
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
    InMemorySourcePublicationMetrics,
    PublicationMetricOutcome,
    PublicationOperation,
    PublicationRejectionReason,
    SourcePublicationMetrics,
)
from personal_os.sources.ports import AwareUtcClock, SourcePublicationStore
from personal_os.sources.publication import (
    MAXIMUM_RECEIPT_AGE,
    REJECTION_REASON_BY_ERROR_CODE,
    SourceVersionPublicationService,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult

__all__ = [
    "MAXIMUM_RECEIPT_AGE",
    "REJECTION_REASON_BY_ERROR_CODE",
    "ActorKind",
    "AwareUtcClock",
    "CreateSourceVersion",
    "IdempotencyKey",
    "InMemorySourcePublicationMetrics",
    "PublicationMetricOutcome",
    "PublicationOperation",
    "PublicationOutcome",
    "PublicationRejectionReason",
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
    "compute_request_fingerprint",
]
