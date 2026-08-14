"""Immutable publication actors and the closed actor-kind vocabulary.

An actor value object pins only the structural invariants: ``user`` and
``device`` actors carry a non-nil actor UUID and a ``system`` actor carries
none. Workspace ownership, device status and approval are policy rechecked by
the PostgreSQL adapter as defense in depth; no external payload ever
deserializes directly into a trusted actor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

NIL_UUID: Final[UUID] = UUID(int=0)


class ActorKind(StrEnum):
    """Closed vocabulary of actor kinds that may author a source version."""

    USER = "user"
    DEVICE = "device"
    SYSTEM = "system"


def reject_nil_uuid(field_name: str, value: UUID) -> None:
    """Reject the nil UUID, naming ``field_name`` in the error message."""
    if value == NIL_UUID:
        raise ValueError(f"{field_name} must be a non-nil UUID")


@dataclass(frozen=True, slots=True)
class SourceActor:
    """Immutable actor behind a source-publication command.

    ``user`` requires the active workspace owner and ``device`` an active
    same-workspace device, both identified by a non-nil UUID; ``system`` has no
    actor ID and is constructible only by an internal composition boundary.
    """

    actor_kind: ActorKind
    actor_id: UUID | None

    def __post_init__(self) -> None:
        if self.actor_kind is ActorKind.SYSTEM:
            if self.actor_id is not None:
                raise ValueError("actor_id must be None for a system actor")
            return
        if self.actor_id is None:
            raise ValueError(f"actor_id is required for a {self.actor_kind.value} actor")
        reject_nil_uuid("actor_id", self.actor_id)
