"""Strict source lifecycle wire models and the domain boundary conversion.

Every model here is frozen and closed for extra fields and mirrors the
canonical :class:`SourceLifecycleCommand` operation-dependent field
grammar (spec 19.2): ``rename`` / ``move`` carry ``expected_locator`` and
``target_locator`` and reject ``tombstone_id``; ``delete`` carries only
``expected_locator`` and rejects ``target_locator`` / ``tombstone_id``;
``restore`` carries ``target_locator`` and ``tombstone_id`` and rejects
``expected_locator``. ``policy_revision`` is a closed positive integer and
the opaque idempotency key travels through the same printable non-whitespace
ASCII grammar of the sources contract; ``event_id`` must be a UUIDv7 and
``client_timestamp`` is an optional RFC3339 UTC instant. Conversion to the
frozen :class:`SourceLifecycleCommand` happens only through the shared
domain validator, so every wire violation surfaces as the typed
``source_lifecycle_input_invalid`` with its closed ``reason`` token or as
the framework's closed validation envelope — locator, fingerprint,
idempotency key and content stay out of the rendered safe detail. The
response renderer projects the commit result onto a strict payload of
exactly the eight safe receipt members of the canonical
:class:`SourceLifecycleCommitResult`, never a fingerprint, locator text,
snapshot bytes or canonical envelope.
"""

from __future__ import annotations

from datetime import datetime
from re import compile as _re_compile
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.functional_validators import BeforeValidator

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    LifecycleState,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import SourceLifecycleApplicationError
from personal_os.source_locators import NormalizedLocator

#: Wire grammar of the opaque idempotency key: the printable non-whitespace
#: ASCII grammar of the sources contract (1-200 characters).
_IDEMPOTENCY_KEY_PATTERN: Final[str] = r"^[!-~]{1,200}$"

#: Wire grammar of an RFC3339 UTC instant with optional fractional seconds.
_RFC3339_UTC_PATTERN: Final[str] = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)
_RFC3339_UTC_RE = _re_compile(_RFC3339_UTC_PATTERN)


#: Public aliases for the OpenAPI document; the request stores
#: :class:`LifecycleOperation` and the response stores
#: :class:`LifecycleState` directly so the wire schema serializes both as
#: their closed enum members.
LifecycleOperationValue = Literal["rename", "move", "delete", "restore"]
LifecycleStateValue = Literal["active", "deleted"]


def _coerce_locator(value: object) -> object:
    """Coerce a wire string into :class:`NormalizedLocator` before Pydantic validation.

    The dataclass has no Pydantic-friendly ``__init__`` ergonomics, so the
    pre-validator runs first and surfaces a closed validation error when the
    raw wire value violates the canonical locator grammar.
    """

    if value is None or isinstance(value, NormalizedLocator):
        return value
    if isinstance(value, str):
        try:
            return NormalizedLocator(value)
        except ValueError as cause:
            raise ValueError(f"normalized locator rejected: {cause}") from cause
    raise ValueError("expected a normalized locator string")


_LocatorField = Annotated[
    NormalizedLocator | None,
    BeforeValidator(_coerce_locator),
]


def _coerce_client_timestamp(value: object) -> object:
    """Coerce an RFC3339 UTC string into an aware ``datetime`` before validation.

    The wire grammar is enforced through the
    :data:`_RFC3339_UTC_PATTERN` regex; the parsed value carries an explicit
    UTC tzinfo so the domain command never receives a naive timestamp.
    """

    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        if not _RFC3339_UTC_RE.fullmatch(value):
            raise ValueError("client_timestamp must be RFC3339 UTC (ending in Z)")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as cause:
            raise ValueError(f"client_timestamp rejected: {cause}") from cause
    raise ValueError("expected an RFC3339 UTC timestamp string")


_ClientTimestampField = Annotated[
    datetime | None,
    BeforeValidator(_coerce_client_timestamp),
]


#: Closed safe reason tokens of the route-bound wire validation rejections.
_REASON_OPERATION_INVALID: Final[SafeToken] = SafeToken.parse("operation_invalid")
_REASON_EVENT_ID_INVALID: Final[SafeToken] = SafeToken.parse("event_id_invalid")
_REASON_POLICY_REVISION_INVALID: Final[SafeToken] = SafeToken.parse("policy_revision_invalid")
_REASON_IDEMPOTENCY_KEY_INVALID: Final[SafeToken] = SafeToken.parse("idempotency_key_invalid")
_REASON_CLIENT_TIMESTAMP_INVALID: Final[SafeToken] = SafeToken.parse("client_timestamp_invalid")
_REASON_LOCATOR_INVALID: Final[SafeToken] = SafeToken.parse("locator_invalid")


def _wire_invalid(reason: SafeToken) -> SourceLifecycleApplicationError:
    return SourceLifecycleApplicationError(
        ErrorCode.SOURCE_LIFECYCLE_INPUT_INVALID, safe_details={"reason": reason}
    )


class SourceLifecycleEventRequest(BaseModel):
    """The strict lifecycle event commit body (spec 19.2).

    Carries the stable UUIDv7 ``event_id``, the opaque idempotency key, the
    closed ``operation`` token, the source identity, the expected version
    UUID, the operation-dependent locator evidence and tombstone identity,
    the closed positive ``policy_revision`` and an optional RFC3339
    ``client_timestamp``. Workspace, device and user identities are
    deliberately absent — they derive from the resolved bearer context.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    source_id: UUID
    operation: LifecycleOperation
    expected_version_id: UUID
    expected_locator: _LocatorField = None
    target_locator: _LocatorField = None
    tombstone_id: UUID | None = None
    policy_revision: int = Field(ge=1)
    client_timestamp: _ClientTimestampField = None

    @model_validator(mode="after")
    def _validate_request_shape(self) -> SourceLifecycleEventRequest:
        if self.event_id.version != 7:
            raise ValueError("event_id must be a UUIDv7") from None
        if self.event_id.int == 0:
            raise ValueError("event_id must be non-nil") from None
        if self.source_id.int == 0 or self.expected_version_id.int == 0:
            raise ValueError("source_id and expected_version_id must be non-nil") from None
        if self.tombstone_id is not None and self.operation is not LifecycleOperation.RESTORE:
            raise ValueError(
                f"operation {self.operation.value} must not carry a tombstone_id"
            ) from None
        if (
            self.operation is LifecycleOperation.RENAME
            or self.operation is LifecycleOperation.MOVE
        ):
            if self.expected_locator is None:
                raise ValueError(
                    f"operation {self.operation.value} requires expected_locator"
                ) from None
            if self.target_locator is None:
                raise ValueError(
                    f"operation {self.operation.value} requires target_locator"
                ) from None
            if self.expected_locator == self.target_locator:
                raise ValueError(
                    "expected_locator and target_locator must differ"
                ) from None
        elif self.operation is LifecycleOperation.DELETE:
            if self.expected_locator is None:
                raise ValueError("operation delete requires expected_locator") from None
            if self.target_locator is not None:
                raise ValueError(
                    "operation delete must not carry target_locator"
                ) from None
        else:
            if self.expected_locator is not None:
                raise ValueError(
                    "operation restore must not carry expected_locator"
                ) from None
            if self.target_locator is None:
                raise ValueError("operation restore requires target_locator") from None
            if self.tombstone_id is None:
                raise ValueError("operation restore requires tombstone_id") from None
        return self


class SourceLifecycleCommitData(BaseModel):
    """The safe receipt of one closed lifecycle commit (spec 19.2).

    Exactly the eight members of :class:`SourceLifecycleCommitResult` —
    never a fingerprint, locator text, snapshot bytes, canonical envelope
    or signed payload. The response renders with ``exclude_unset`` so the
    ``deleted`` outcome emits ``tombstone_id`` and omits
    ``resulting_locator``, mirroring the domain invariant that the
    resulting locator is the canonical locator of the active state only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    source_version_id: UUID
    event_id: UUID
    event_sequence: int = Field(ge=1)
    state: LifecycleState
    tombstone_id: UUID | None = None
    resulting_locator: str | None = None
    committed_at: datetime


def to_domain_command(request: SourceLifecycleEventRequest) -> SourceLifecycleCommand:
    """Convert the strict wire body into the frozen domain command.

    Every semantic grammar the domain owns — UUIDv7 event identity,
    locator NFC normalization, tombstone identity, policy revision, RFC3339
    client timestamp and the operation-dependent locator shape — surfaces
    as the closed ``source_lifecycle_input_invalid`` rejection with its
    single safe ``reason`` token. The request workspace, device and user
    identities are absent; they always derive from the bearer context, so
    this conversion never picks them up.
    """

    client_timestamp = request.client_timestamp
    try:
        return SourceLifecycleCommand(
            source_id=request.source_id,
            event_id=request.event_id,
            idempotency_key=request.idempotency_key,
            operation=request.operation,
            expected_version_id=request.expected_version_id,
            expected_locator=request.expected_locator,
            target_locator=request.target_locator,
            tombstone_id=request.tombstone_id,
            policy_revision=request.policy_revision,
            client_timestamp=client_timestamp,
        )
    except ValueError as cause:
        raise _wire_invalid(_REASON_OPERATION_INVALID) from cause


def source_lifecycle_commit_data(
    result: SourceLifecycleCommitResult,
) -> SourceLifecycleCommitData:
    """Render one canonical commit result onto the strict wire payload."""

    return SourceLifecycleCommitData(
        source_id=result.source_id,
        source_version_id=result.source_version_id,
        event_id=result.event_id,
        event_sequence=result.event_sequence,
        state=result.state,
        tombstone_id=result.tombstone_id,
        resulting_locator=(
            None if result.resulting_locator is None else result.resulting_locator.value
        ),
        committed_at=result.committed_at,
    )


__all__ = [
    "LifecycleOperationValue",
    "LifecycleStateValue",
    "SourceLifecycleCommitData",
    "SourceLifecycleEventRequest",
    "source_lifecycle_commit_data",
    "to_domain_command",
]