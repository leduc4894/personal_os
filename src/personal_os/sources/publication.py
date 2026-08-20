"""The provider-neutral source-version publication service.

:class:`SourceVersionPublicationService` orchestrates one publication
deterministically: validate the command (its value-object constructors already
enforce UUIDs, key/title grammar, actor shape and aware client timestamps; the
service re-validates the ``ExpectedObject`` claim through the object-storage
value contracts), compute the request fingerprint, run the idempotent store
preflight, and only on a miss acquire one verified receipt from the object
store before committing through the store port.

A receipt is never accepted as a public method argument: it is obtained inside
a single invocation, validated against the expected object and the five-minute
age rule measured with the injected aware UTC clock, and handed straight to the
commit port. A bounded database retry inside the store adapter reuses that one
receipt; a fresh service invocation obtains another receipt unless the
preflight proves the operation already committed.

Metric labels come only from the closed enums of
:mod:`personal_os.sources.metrics`; forbidden values (title, key, fingerprint,
digest, object key, receipt fields) never become labels, messages or safe
details. Rejection metrics use the registry-code vocabulary of
``PublicationRejectionReason``; the shorter audit-rejection tokens of spec
section 10.3 are written by the store adapter's audit transaction, not by this
service (see :data:`REJECTION_REASON_BY_ERROR_CODE`).
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectStore,
    ContentDigest,
    ExpectedObject,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.sources.commands import CreateSourceVersion, UpdateSourceVersion
from personal_os.sources.errors import (
    EXPECTED_OBJECT_INVALID,
    RECEIPT_STALE_REASONS,
    SourcePublicationError,
)
from personal_os.sources.fingerprint import (
    SourceVersionCommand,
    compute_request_fingerprint,
)
from personal_os.sources.metrics import (
    PublicationMetricOutcome,
    PublicationOperation,
    PublicationRejectionReason,
    SourcePublicationMetrics,
)
from personal_os.sources.ports import (
    AwareUtcClock,
    PolicyEnforcementGuard,
    SourcePublicationStore,
)
from personal_os.sources.results import SourceVersionPublicationResult

if TYPE_CHECKING:
    from personal_os.exclusion_policy.enforcement import PublicationPolicyEvidence

#: Maximum age of an accepted receipt (spec section 5.3: at most five minutes).
MAXIMUM_RECEIPT_AGE: Final[timedelta] = timedelta(minutes=5)

#: The single registered reason token for every receipt age-rule failure. The
#: closed ``RECEIPT_STALE_REASONS`` set admits only ``older_than_allowed_age``,
#: so a future-dated or naïve ``verified_at`` — which fails the same age rule
#: window — is reported with the same registered token rather than extending
#: the closed vocabulary. The recovery path (reverify) is identical.
_RECEIPT_AGE_RULE_REASON: Final = RECEIPT_STALE_REASONS[0]

#: Registry error code -> business-rejection metric label (spec section 14).
#: The nine business rejections of spec section 10.3 in their registry-code
#: form; concurrency/dependency codes are retryable outcomes, not audited
#: business rejections, so they carry no rejection label.
REJECTION_REASON_BY_ERROR_CODE: Final[Mapping[ErrorCode, PublicationRejectionReason]] = (
    MappingProxyType(
        {
            ErrorCode.SOURCE_PUBLISH_INPUT_INVALID: (
                PublicationRejectionReason.SOURCE_PUBLISH_INPUT_INVALID
            ),
            ErrorCode.SOURCE_NOT_FOUND: PublicationRejectionReason.SOURCE_NOT_FOUND,
            ErrorCode.SOURCE_ALREADY_EXISTS: PublicationRejectionReason.SOURCE_ALREADY_EXISTS,
            ErrorCode.SOURCE_STATE_INVALID: PublicationRejectionReason.SOURCE_STATE_INVALID,
            ErrorCode.SOURCE_VERSION_CONFLICT: PublicationRejectionReason.SOURCE_VERSION_CONFLICT,
            ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH: (
                PublicationRejectionReason.SOURCE_IDEMPOTENCY_MISMATCH
            ),
            ErrorCode.SOURCE_EVENT_IDENTITY_MISMATCH: (
                PublicationRejectionReason.SOURCE_EVENT_IDENTITY_MISMATCH
            ),
            ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE: (
                PublicationRejectionReason.SOURCE_VERIFIED_RECEIPT_STALE
            ),
            ErrorCode.SOURCE_CONTENT_OBJECT_CONFLICT: (
                PublicationRejectionReason.SOURCE_CONTENT_OBJECT_CONFLICT
            ),
        }
    )
)


def validate_expected_object(expected: ExpectedObject) -> None:
    """Re-validate the expected-object claim through the object-storage contracts.

    The value-object constructors do not self-validate, so the grammar checks
    run here, before the fingerprint and before any I/O. Any failure raises the
    typed input-invalid error with the closed ``expected_object_invalid``
    reason token; the offending value is never copied into the error.
    """

    try:
        ContentDigest.parse(expected.content_digest.hexadecimal)
        CanonicalMediaType.parse(expected.media_type.value)
    except ValueError as error:
        raise SourcePublicationError(
            ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
            safe_details={"reason": EXPECTED_OBJECT_INVALID},
        ) from error
    if expected.size_bytes < 0:
        raise SourcePublicationError(
            ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
            safe_details={"reason": EXPECTED_OBJECT_INVALID},
        )


def validate_verified_receipt(
    receipt: VerifiedObjectReceipt,
    expected: ExpectedObject,
    now: datetime,
    source_id: UUID,
) -> None:
    """Enforce the verified-receipt boundary before any commit.

    The receipt digest, derived canonical key, size and media type must equal
    the expected object exactly, otherwise the typed content-object conflict is
    raised. ``verified_at`` must be aware (normalized to UTC for the instant
    comparison), not in the future and at most five minutes old, otherwise the
    typed receipt-stale error is raised with the single registered reason
    token. No receipt field is ever copied into an error message or detail.
    """

    if (
        receipt.content_digest != expected.content_digest
        or receipt.object_key != derive_canonical_object_key(expected.content_digest)
        or receipt.size_bytes != expected.size_bytes
        or receipt.media_type != expected.media_type
    ):
        raise SourcePublicationError(
            ErrorCode.SOURCE_CONTENT_OBJECT_CONFLICT,
            safe_details={"source_id": source_id},
        )
    verified_at = receipt.verified_at
    if verified_at.tzinfo is None or verified_at.utcoffset() is None:
        raise SourcePublicationError(
            ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE,
            safe_details={"reason": _RECEIPT_AGE_RULE_REASON},
        )
    utc_verified_at = verified_at.astimezone(UTC)
    if utc_verified_at > now or now - utc_verified_at > MAXIMUM_RECEIPT_AGE:
        raise SourcePublicationError(
            ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE,
            safe_details={"reason": _RECEIPT_AGE_RULE_REASON},
        )


@dataclass(slots=True)
class SourceVersionPublicationService:
    """Orchestrates one idempotent source-version publication over injected ports.

    Depends only on provider-neutral ports and contracts: the durable
    :class:`SourcePublicationStore`, the mandatory
    :class:`~personal_os.sources.ports.PolicyEnforcementGuard`, the
    :class:`~personal_os.object_storage.CanonicalObjectStore`, the closed
    low-cardinality metrics sink and the aware UTC clock seam. The guard
    evaluates the active exclusion policy before the idempotent preflight and
    before any object-store access, so an excluded or indeterminate subject,
    a missing active signed policy or corrupt signature material fails closed
    with the typed denial and zero object-store calls — including an exact
    replay, whose committed data is canonical data the current policy must
    permit before it is returned. The store verifies the active signed revision
    under the policy-state row lock inside the commit transaction; it reuses
    only an allowed binding for that exact revision and re-evaluates every
    other evidence shape.
    """

    store: SourcePublicationStore
    object_store: CanonicalObjectStore
    metrics: SourcePublicationMetrics
    clock: AwareUtcClock
    policy_guard: PolicyEnforcementGuard

    async def publish_create(
        self,
        *,
        command: CreateSourceVersion,
        stream: AsyncIterable[bytes],
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        """Publish the first version of a source, replaying exactly on retry."""

        return await self._publish(
            command=command,
            stream=stream,
            diagnostic_context=diagnostic_context,
            operation=PublicationOperation.CREATE,
        )

    async def publish_update(
        self,
        *,
        command: UpdateSourceVersion,
        stream: AsyncIterable[bytes],
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        """Publish a new version over the command's base, replaying exactly on retry."""

        return await self._publish(
            command=command,
            stream=stream,
            diagnostic_context=diagnostic_context,
            operation=PublicationOperation.UPDATE,
        )

    async def _publish(
        self,
        *,
        command: SourceVersionCommand,
        stream: AsyncIterable[bytes],
        diagnostic_context: DiagnosticContext,
        operation: PublicationOperation,
    ) -> SourceVersionPublicationResult:
        started_at = self.clock()
        try:
            result = await self._publish_once(
                command=command,
                stream=stream,
                diagnostic_context=diagnostic_context,
                operation=operation,
                started_at=started_at,
            )
        except SourcePublicationError as error:
            self._record_failure(operation=operation, error=error, started_at=started_at)
            raise
        return result

    async def _publish_once(
        self,
        *,
        command: SourceVersionCommand,
        stream: AsyncIterable[bytes],
        diagnostic_context: DiagnosticContext,
        operation: PublicationOperation,
        started_at: datetime,
    ) -> SourceVersionPublicationResult:
        # Validate: the command constructors enforce UUIDs, key/title grammar,
        # actor shape and the aware client timestamp; the expected-object claim
        # is re-validated here because its value objects do not self-validate.
        validate_expected_object(command.expected_object)
        # Policy preflight (spec 14): the active signed policy is verified and
        # the candidate evaluated before the idempotent preflight and before
        # any object-store access, so a denied subject never observes or
        # replays canonical data and never touches object storage.
        preflight_decision: PublicationPolicyEvidence = (
            await self.policy_guard.authorize_publication(
                command,
                diagnostic_context,
            )
        )
        # Fingerprint.
        request_fingerprint = compute_request_fingerprint(command)
        # Idempotent preflight: an exact replay never touches the object store.
        committed = await self.store.resolve_committed(
            command, request_fingerprint, diagnostic_context
        )
        if committed is not None:
            duration_seconds = self._elapsed_seconds_since(started_at)
            self.metrics.record_replay(operation=operation)
            self.metrics.record_publication(
                operation=operation,
                outcome=PublicationMetricOutcome.REPLAYED,
                duration_seconds=duration_seconds,
            )
            return committed
        # One verified receipt per invocation, validated before the commit.
        receipt = await self._obtain_verified_receipt(command=command, stream=stream)
        if isinstance(command, CreateSourceVersion):
            committed = await self.store.commit_create(
                command,
                request_fingerprint,
                receipt,
                diagnostic_context,
                preflight_decision=preflight_decision,
            )
        else:
            committed = await self.store.commit_update(
                command,
                request_fingerprint,
                receipt,
                diagnostic_context,
                preflight_decision=preflight_decision,
            )
        self.metrics.record_publication(
            operation=operation,
            outcome=PublicationMetricOutcome.SUCCEEDED,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )
        return committed

    async def _obtain_verified_receipt(
        self,
        *,
        command: SourceVersionCommand,
        stream: AsyncIterable[bytes],
    ) -> VerifiedObjectReceipt:
        expected = command.expected_object
        # Resolve deduplicated canonical bytes before uploading the stream so
        # identical content never reaches storage twice.
        receipt = await self.object_store.resolve_verified_object(expected)
        if receipt is None:
            receipt = await self.object_store.store_stream(
                stream,
                expected.size_bytes,
                expected.media_type.value,
                expected.content_digest.hexadecimal,
            )
        validate_verified_receipt(receipt, expected, self.clock(), command.source_id)
        return receipt

    def _elapsed_seconds_since(self, started_at: datetime) -> float:
        # Clamped at zero so a clock seam that repeats or drifts backwards can
        # never turn a recorded duration negative.
        return max((self.clock() - started_at).total_seconds(), 0.0)

    def _record_failure(
        self,
        *,
        operation: PublicationOperation,
        error: SourcePublicationError,
        started_at: datetime,
    ) -> None:
        rejection_reason = REJECTION_REASON_BY_ERROR_CODE.get(error.error_code)
        if rejection_reason is not None:
            self.metrics.record_rejection(
                operation=operation,
                reason_code=rejection_reason,
            )
        self.metrics.record_publication(
            operation=operation,
            outcome=PublicationMetricOutcome.REJECTED,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )
