"""Public source-publication domain contracts.

Immutable actors, commands and publication results. The module reuses the
canonical object-storage value objects and imports no infrastructure SDK,
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
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult

__all__ = [
    "ActorKind",
    "CreateSourceVersion",
    "IdempotencyKey",
    "PublicationOutcome",
    "SourceActor",
    "SourceTitle",
    "SourceType",
    "SourceVersionPublicationResult",
    "UpdateSourceVersion",
]
