"""Canonical current-source read: fail-closed verified reader.

:class:`CanonicalSourceReadService` resolves the current version of a source
through the read-only store port and exposes its bytes only through the
object store's verified reader, so a consumer can never observe a single byte
before size, media and full digest verification passed. The service never
trusts client-supplied object metadata and never updates source state, the
current pointer, versions, events, audit or intents: it performs zero
mutations on any port. Missing or corrupt bytes surface the existing typed
object-storage errors unchanged; a missing or identity-mismatched current
reference fails closed as the typed read-state integrity error.

Outcome metrics are recorded for both terminal paths and registered diagnostic
events are built and registry-validated always, with durations measured by
:func:`time.monotonic`; the validated events are delivered to the optional
composition-provided diagnostics sink, and event fields carry ids and the
closed error-code enum only — never bytes, titles or digests.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import (
    DiagnosticEventSink,
    EventName,
    RejectedDiagnosticPayload,
    build_registered_event,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.object_storage import (
    CanonicalObjectStore,
    ExpectedObject,
    VerifiedObjectReader,
)
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import SourceType
from personal_os.sources.metrics import CanonicalReadMetrics, ReadOutcome
from personal_os.sources.ports import PolicyEnforcementGuard

__all__ = [
    "CanonicalReadStateError",
    "CanonicalSourceReadService",
    "CanonicalSourceReadStore",
    "CanonicalSourceReference",
    "ReadCurrentSourceCommand",
    "canonical_read_failed_event_fields",
    "canonical_read_succeeded_event_fields",
    "validate_read_current_source_command",
]


class CanonicalReadStateError(ApplicationError):
    """A missing or inconsistent canonical current-source reference."""

    allowed_codes = frozenset({ErrorCode.CANONICAL_READ_STATE_INVALID})

    def __init__(self, *, source_id: UUID) -> None:
        super().__init__(
            ErrorCode.CANONICAL_READ_STATE_INVALID,
            safe_details={"source_id": source_id},
        )


@dataclass(frozen=True, slots=True)
class ReadCurrentSourceCommand:
    """One canonical current-source read request for a workspace-owned source."""

    workspace_id: UUID
    source_id: UUID


@dataclass(frozen=True, slots=True)
class CanonicalSourceReference:
    """The resolved current version of a source with its expected canonical object.

    Produced only by the trusted store port; ``content_version`` is a positive
    integer and ``expected_object`` is the verification claim the object store
    must independently prove before any byte is exposed. ``source_type`` is
    the stored canonical type evidence the policy guard evaluates alongside
    the expected object's media type and size.
    """

    workspace_id: UUID
    source_id: UUID
    source_version_id: UUID
    content_version: int
    source_type: SourceType
    expected_object: ExpectedObject
    committed_at: datetime

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("source_version_id", self.source_version_id)
        if self.content_version < 1:
            raise ValueError("content_version must be a positive integer")
        if not isinstance(self.source_type, SourceType):
            raise ValueError("source_type must be a closed SourceType member")


class CanonicalSourceReadStore(Protocol):
    """Read-only port resolving the current version of a source.

    The adapter owns state filtering (which source states are readable); it
    either resolves the current reference or raises the typed read-state
    integrity error. The port exposes no mutating method.
    """

    async def resolve_current(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> CanonicalSourceReference: ...


@dataclass(slots=True)
class CanonicalSourceReadService:
    """Resolves the current version and exposes only verified bytes.

    Depends only on provider-neutral ports: the read-only
    :class:`CanonicalSourceReadStore` (which resolves the active exclusion
    policy and the source state transactionally before returning), the
    mandatory :class:`~personal_os.sources.ports.PolicyEnforcementGuard`
    re-checked before any object-store request, the
    :class:`~personal_os.object_storage.CanonicalObjectStore`, the closed
    low-cardinality read metrics sink and the optional diagnostics sink the
    composition root satisfies with its configured logger. The service never
    updates source state, the current pointer, versions, events, audit or
    intents, and never trusts client-supplied object metadata.
    """

    store: CanonicalSourceReadStore
    object_store: CanonicalObjectStore
    metrics: CanonicalReadMetrics
    policy_guard: PolicyEnforcementGuard
    diagnostics: DiagnosticEventSink | None = None

    @asynccontextmanager
    async def open_current_source(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> AsyncIterator[tuple[CanonicalSourceReference, VerifiedObjectReader]]:
        """Yield the current reference and a reader over its verified bytes.

        The consumer body is entered only after the store resolved the source
        state transactionally under the active policy, the guard re-authorized
        the resolved reference, and the object store verified the full size,
        media type and digest; any typed failure — including verification
        failures raised before the first byte — surfaces the original error
        unchanged after the failed outcome is recorded. Caller cancellation
        propagates while the ``async with`` teardown closes the reader and
        clears the adapter's spool state.
        """
        validate_read_current_source_command(command)
        started = time.monotonic()
        reference: CanonicalSourceReference | None = None
        try:
            reference = await self.store.resolve_current(command, diagnostic_context)
            _validate_reference_matches_command(reference, command)
            await self.policy_guard.authorize_read(reference, diagnostic_context)
            async with self.object_store.open_verified_reader(reference.expected_object) as reader:
                yield reference, reader
        except ApplicationError as error:
            self.metrics.record_read(
                outcome=ReadOutcome.FAILED,
                duration_seconds=max(time.monotonic() - started, 0.0),
            )
            self._emit_registered_event(*canonical_read_failed_event_fields(command, error))
            raise
        assert reference is not None
        self.metrics.record_read(
            outcome=ReadOutcome.SUCCEEDED,
            duration_seconds=max(time.monotonic() - started, 0.0),
        )
        self._emit_registered_event(*canonical_read_succeeded_event_fields(reference))

    def _emit_registered_event(self, event_name: EventName, fields: Mapping[str, object]) -> None:
        """Validate the registered event; deliver it when a sink is bound.

        Without a composition-provided sink the validated payload is discarded
        (build-and-validate only); a rejected payload is registry drift and
        raises regardless of sink presence.
        """
        built = build_registered_event(event_name, fields)
        if isinstance(built, RejectedDiagnosticPayload):
            # A rejected payload here means registry drift, a programming error
            # rather than untrusted input; raise so it also surfaces in optimized
            # (python -O) runs instead of vanishing with assert.
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        if self.diagnostics is not None:
            self.diagnostics.emit(event_name, dict(fields))

    async def read_current_source_bytes(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> bytes:
        """Read the full canonical bytes of the current version of a source."""

        async with self.open_current_source(command, diagnostic_context) as (_, reader):
            chunks: list[bytes] = []
            async for chunk in reader:
                chunks.append(chunk)
            return b"".join(chunks)


def validate_read_current_source_command(command: ReadCurrentSourceCommand) -> None:
    """Reject nil workspace or source UUIDs before any port call."""

    reject_nil_uuid("workspace_id", command.workspace_id)
    reject_nil_uuid("source_id", command.source_id)


def _validate_reference_matches_command(
    reference: CanonicalSourceReference, command: ReadCurrentSourceCommand
) -> None:
    # Fail closed when the trusted store returns a reference for another
    # workspace or source: the read must never expose another source's bytes.
    if reference.workspace_id != command.workspace_id or reference.source_id != command.source_id:
        raise CanonicalReadStateError(source_id=command.source_id)


def canonical_read_succeeded_event_fields(
    reference: CanonicalSourceReference,
) -> tuple[EventName, dict[str, object]]:
    """The registered success event and its safe field payload.

    Carries only server-assigned ids so no byte, title or digest can ever
    reach a diagnostic line; the field set satisfies the registry definition
    for ``canonical_source_read_succeeded`` exactly.
    """
    return EventName.CANONICAL_SOURCE_READ_SUCCEEDED, {
        "source_id": reference.source_id,
        "workspace_id": reference.workspace_id,
        "source_version_id": reference.source_version_id,
    }


def canonical_read_failed_event_fields(
    command: ReadCurrentSourceCommand, error: ApplicationError
) -> tuple[EventName, dict[str, object]]:
    """The registered failure event and its safe field payload.

    Carries only the command's ids and the closed error-code enum; the
    original exception, its message and any chained provider cause never
    enter the field set.
    """
    return EventName.CANONICAL_SOURCE_READ_FAILED, {
        "source_id": command.source_id,
        "workspace_id": command.workspace_id,
        "error_code": error.error_code,
    }
