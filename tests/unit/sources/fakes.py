"""Narrow in-memory fakes proving the publication service orchestration order.

Every fake records the exact port call sequence into one shared ledger so a
test can assert the full cross-port order (store preflight, object-store
resolve/store, receipt validation, commit) with string entries only. The fakes
never retain, echo or log command payloads: titles, idempotency keys and
fingerprints are compared by identity/equality in the assertions, not recorded.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid4

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectKey,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
    UpdateSourceVersion,
)
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import RequestFingerprint, SourceVersionCommand
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult

#: Shared ledger entry constants: one string per observed port call.
STORE_RESOLVE_COMMITTED: Final[str] = "store.resolve_committed"
STORE_COMMIT_CREATE: Final[str] = "store.commit_create"
STORE_COMMIT_UPDATE: Final[str] = "store.commit_update"
OBJECT_STORE_RESOLVE: Final[str] = "object_store.resolve_verified_object"
OBJECT_STORE_STORE_STREAM: Final[str] = "object_store.store_stream"

#: The maximum allowed receipt age from spec section 5.3 (five minutes).
MAXIMUM_RECEIPT_AGE: Final[timedelta] = timedelta(minutes=5)

_CANONICAL_BYTES: Final[bytes] = b"canonical publication bytes"


@dataclass
class CallLedger:
    """Append-only shared record of observed port calls across all fakes."""

    entries: list[str] = field(default_factory=list)

    def record(self, entry: str) -> None:
        self.entries.append(entry)


@dataclass
class SequencedUtcClock:
    """Injectable aware UTC clock returning queued moments, then the last one.

    The service calls the clock at publication start and again whenever a
    terminal metric duration is measured; tests queue one moment per expected
    read. Once a single moment remains it is returned forever.
    """

    moments: list[datetime]

    def __call__(self) -> datetime:
        if len(self.moments) > 1:
            return self.moments.pop(0)
        return self.moments[0]


class ProbedByteStream:
    """Caller-owned async byte stream that reports whether it was consumed."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._remaining = list(chunks)
        self.was_consumed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if not self._remaining:
            raise StopAsyncIteration
        self.was_consumed = True
        return self._remaining.pop(0)


def build_expected_object() -> ExpectedObject:
    """A structurally valid expected-object claim over fixed canonical bytes."""

    return ExpectedObject(
        content_digest=ContentDigest.parse(hashlib.sha256(_CANONICAL_BYTES).hexdigest()),
        size_bytes=len(_CANONICAL_BYTES),
        media_type=CanonicalMediaType.parse("text/markdown"),
    )


def build_create_command(expected_object: ExpectedObject | None = None) -> CreateSourceVersion:
    """A valid create command; ``expected_object`` overrides the claim under test."""

    return CreateSourceVersion(
        workspace_id=uuid4(),
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("publish-once-001"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Publication service fake title"),
        actor=SourceActor(actor_kind=ActorKind.USER, actor_id=uuid4()),
        expected_object=expected_object if expected_object is not None else build_expected_object(),
        client_timestamp=None,
    )


def build_update_command(expected_object: ExpectedObject | None = None) -> UpdateSourceVersion:
    """A valid update command; ``expected_object`` overrides the claim under test."""

    return UpdateSourceVersion(
        workspace_id=uuid4(),
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("publish-once-002"),
        base_version_id=uuid4(),
        actor=SourceActor(actor_kind=ActorKind.DEVICE, actor_id=uuid4()),
        expected_object=expected_object if expected_object is not None else build_expected_object(),
        client_timestamp=None,
    )


def build_committed_result(command: SourceVersionCommand) -> SourceVersionPublicationResult:
    """The canonical committed result a store returns for ``command``."""

    return SourceVersionPublicationResult(
        source_id=command.source_id,
        source_version_id=uuid4(),
        content_version=1,
        event_id=command.event_id,
        event_sequence=1,
        content_digest=command.expected_object.content_digest,
        outcome=PublicationOutcome.PUBLISHED,
        committed_at=datetime.now(UTC),
    )


def build_verified_receipt(
    expected: ExpectedObject,
    verified_at: datetime,
    *,
    content_digest: ContentDigest | None = None,
    object_key: CanonicalObjectKey | None = None,
    size_bytes: int | None = None,
    media_type: CanonicalMediaType | None = None,
) -> VerifiedObjectReceipt:
    """A receipt for ``expected`` stamped at ``verified_at`` with overridable fields."""

    return VerifiedObjectReceipt(
        content_digest=content_digest if content_digest is not None else expected.content_digest,
        object_key=(
            object_key
            if object_key is not None
            else derive_canonical_object_key(expected.content_digest)
        ),
        size_bytes=size_bytes if size_bytes is not None else expected.size_bytes,
        media_type=media_type if media_type is not None else expected.media_type,
        verified_at=verified_at,
        verification_method=VerificationMethod.UPLOADED_FULL_READ,
    )


def build_diagnostic_context() -> DiagnosticContext:
    """A fresh server-owned diagnostic context for one request-bound unit of work."""

    return create_diagnostic_context().context


@dataclass
class FakeCanonicalObjectStore:
    """Object-store fake issuing receipts while recording the exact call order.

    ``resolve_receipts`` is a queue consumed one entry per
    ``resolve_verified_object`` call (an exhausted or ``None`` entry models "no
    existing object"). ``store_stream`` fully consumes the caller's stream and
    returns ``store_receipt``. Receipts issued to the service are retained by
    identity so tests can prove which invocation's receipt reached commit.
    """

    ledger: CallLedger
    resolve_receipts: list[VerifiedObjectReceipt | None] = field(default_factory=list)
    store_receipt: VerifiedObjectReceipt | None = None
    issued_resolve_receipts: list[VerifiedObjectReceipt] = field(default_factory=list)
    store_stream_calls: list[tuple[int, str, str | None, int]] = field(default_factory=list)

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        self.ledger.record(OBJECT_STORE_RESOLVE)
        receipt = self.resolve_receipts.pop(0) if self.resolve_receipts else None
        if receipt is not None:
            self.issued_resolve_receipts.append(receipt)
        return receipt

    async def store_stream(
        self,
        stream: AsyncIterator[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        self.ledger.record(OBJECT_STORE_STORE_STREAM)
        consumed_bytes = 0
        async for chunk in stream:
            consumed_bytes += len(chunk)
        self.store_stream_calls.append(
            (expected_size_bytes, media_type, claimed_sha256, consumed_bytes)
        )
        receipt = self.store_receipt
        if receipt is None:
            raise AssertionError("store_stream called without a configured store receipt")
        return receipt

    def resolve_call_count(self) -> int:
        return self.ledger.entries.count(OBJECT_STORE_RESOLVE)


@dataclass
class FakeSourcePublicationStore:
    """Publication-store fake modelling preflight, bounded retry and commit.

    ``resolve_committed`` returns ``committed_result`` when set, or raises
    ``resolve_error`` to model a mismatch found by preflight. Each commit
    performs ``1 + internal_retry_attempts`` attempts, recording the receipt
    passed by the service for every attempt (the bounded database retry reuses
    exactly the receipt the service obtained). ``committed_result`` is never
    mutated by a commit: a test that wants a later preflight to hit sets it
    explicitly, so a second invocation misses by default.
    """

    ledger: CallLedger
    commit_result: SourceVersionPublicationResult
    committed_result: SourceVersionPublicationResult | None = None
    resolve_error: SourcePublicationError | None = None
    internal_retry_attempts: int = 0
    resolve_committed_fingerprints: list[RequestFingerprint] = field(default_factory=list)
    commit_receipt_identities: list[list[int]] = field(default_factory=list)
    commit_fingerprints: list[RequestFingerprint] = field(default_factory=list)

    async def resolve_committed(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult | None:
        self.ledger.record(STORE_RESOLVE_COMMITTED)
        self.resolve_committed_fingerprints.append(request_fingerprint)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.committed_result

    async def commit_create(
        self,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        self.ledger.record(STORE_COMMIT_CREATE)
        return self._commit(receipt, request_fingerprint)

    async def commit_update(
        self,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        self.ledger.record(STORE_COMMIT_UPDATE)
        return self._commit(receipt, request_fingerprint)

    def _commit(
        self, receipt: VerifiedObjectReceipt, request_fingerprint: RequestFingerprint
    ) -> SourceVersionPublicationResult:
        attempt_receipt_identities: list[int] = []
        for _ in range(1 + self.internal_retry_attempts):
            # The bounded database retry inside the adapter reuses the same
            # receipt the service obtained; record it once per attempt.
            attempt_receipt_identities.append(id(receipt))
        self.commit_receipt_identities.append(attempt_receipt_identities)
        self.commit_fingerprints.append(request_fingerprint)
        return self.commit_result


def build_idempotency_mismatch_error() -> SourcePublicationError:
    """The typed error a preflight mismatch raises before any object-store call."""

    return SourcePublicationError(ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH)
