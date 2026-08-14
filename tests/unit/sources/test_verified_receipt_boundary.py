"""The verified-receipt boundary of the publication service.

A receipt accepted by the service must match the expected object exactly
(digest, derived canonical key, size, media type) and satisfy the five-minute
age rule against the injected aware UTC clock: not in the future and at most
five minutes old. An invalid receipt prevents the commit in every acquisition
path (deduplicated resolve and stored upload alike), and a receipt is never a
public method argument of the service.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from tests.unit.sources.fakes import (
    CallLedger,
    FakeCanonicalObjectStore,
    FakeSourcePublicationStore,
    ProbedByteStream,
    SequencedUtcClock,
    build_committed_result,
    build_create_command,
    build_diagnostic_context,
    build_expected_object,
    build_verified_receipt,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectKey,
    ContentDigest,
    ExpectedObject,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.sources import SourceVersionPublicationService
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import compute_request_fingerprint
from personal_os.sources.metrics import InMemorySourcePublicationMetrics

_NOW: Final[datetime] = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _other_digest() -> ContentDigest:
    return ContentDigest.parse("f" * 64)


def _build_boundary_service(
    *,
    resolve_receipts: list[VerifiedObjectReceipt | None],
    store_receipt: VerifiedObjectReceipt | None = None,
) -> tuple[SourceVersionPublicationService, FakeSourcePublicationStore, CallLedger]:
    ledger = CallLedger()
    store = FakeSourcePublicationStore(
        ledger=ledger,
        commit_result=build_committed_result(build_create_command()),
    )
    object_store = FakeCanonicalObjectStore(
        ledger=ledger,
        resolve_receipts=list(resolve_receipts),
        store_receipt=store_receipt,
    )
    service = SourceVersionPublicationService(
        store=store,
        object_store=object_store,
        metrics=InMemorySourcePublicationMetrics(),
        clock=SequencedUtcClock(moments=[_NOW, _NOW]),
    )
    return service, store, ledger


async def _publish(service: SourceVersionPublicationService) -> None:
    await service.publish_create(
        command=build_create_command(),
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=build_diagnostic_context(),
    )


@pytest.mark.asyncio
async def test_receipt_at_exactly_five_minutes_old_is_accepted() -> None:
    expected = build_expected_object()
    receipt = build_verified_receipt(expected, _NOW - timedelta(minutes=5))
    service, store, _ = _build_boundary_service(resolve_receipts=[receipt])

    await _publish(service)

    assert store.commit_receipt_identities == [[id(receipt)]]


@pytest.mark.asyncio
async def test_receipt_just_older_than_five_minutes_prevents_commit() -> None:
    expected = build_expected_object()
    stale_receipt = build_verified_receipt(expected, _NOW - timedelta(minutes=5, seconds=1))
    service, store, _ = _build_boundary_service(resolve_receipts=[stale_receipt])

    with pytest.raises(SourcePublicationError) as exc_info:
        await _publish(service)

    assert exc_info.value.error_code is ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE
    assert store.commit_receipt_identities == []


@pytest.mark.asyncio
async def test_future_dated_receipt_prevents_commit() -> None:
    expected = build_expected_object()
    future_receipt = build_verified_receipt(expected, _NOW + timedelta(seconds=1))
    service, store, _ = _build_boundary_service(resolve_receipts=[future_receipt])

    with pytest.raises(SourcePublicationError) as exc_info:
        await _publish(service)

    assert exc_info.value.error_code is ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE
    assert store.commit_receipt_identities == []


@pytest.mark.asyncio
async def test_naive_verified_at_timestamp_prevents_commit() -> None:
    expected = build_expected_object()
    naive_receipt = build_verified_receipt(expected, _NOW.replace(tzinfo=None))
    service, store, _ = _build_boundary_service(resolve_receipts=[naive_receipt])

    with pytest.raises(SourcePublicationError) as exc_info:
        await _publish(service)

    assert exc_info.value.error_code is ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE
    assert store.commit_receipt_identities == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_builder",
    [
        pytest.param(
            lambda expected: build_verified_receipt(expected, _NOW, content_digest=_other_digest()),
            id="digest_mismatch",
        ),
        pytest.param(
            lambda expected: build_verified_receipt(
                expected,
                _NOW,
                object_key=CanonicalObjectKey("objects/sha256/ff/ff/" + "f" * 64),
            ),
            id="object_key_mismatch",
        ),
        pytest.param(
            lambda expected: build_verified_receipt(expected, _NOW, size_bytes=1),
            id="size_mismatch",
        ),
        pytest.param(
            lambda expected: build_verified_receipt(
                expected, _NOW, media_type=CanonicalMediaType.parse("text/plain")
            ),
            id="media_type_mismatch",
        ),
    ],
)
async def test_receipt_field_mismatch_prevents_commit(
    receipt_builder: Callable[[ExpectedObject], VerifiedObjectReceipt],
) -> None:
    expected = build_expected_object()
    mismatched_receipt = receipt_builder(expected)
    service, store, _ = _build_boundary_service(resolve_receipts=[mismatched_receipt])

    with pytest.raises(SourcePublicationError) as exc_info:
        await _publish(service)

    assert exc_info.value.error_code is ErrorCode.SOURCE_CONTENT_OBJECT_CONFLICT
    assert store.commit_receipt_identities == []


@pytest.mark.asyncio
async def test_stored_upload_receipt_is_validated_like_a_deduplicated_receipt() -> None:
    expected = build_expected_object()
    stale_store_receipt = build_verified_receipt(expected, _NOW - timedelta(minutes=6))
    service, store, _ = _build_boundary_service(
        resolve_receipts=[None],
        store_receipt=stale_store_receipt,
    )

    with pytest.raises(SourcePublicationError) as exc_info:
        await _publish(service)

    assert exc_info.value.error_code is ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE
    assert store.commit_receipt_identities == []


@pytest.mark.asyncio
async def test_receipt_boundary_errors_carry_no_forbidden_values() -> None:
    expected = build_expected_object()
    stale_receipt = build_verified_receipt(expected, _NOW - timedelta(hours=1))
    service, _, _ = _build_boundary_service(resolve_receipts=[stale_receipt])

    with pytest.raises(SourcePublicationError) as exc_info:
        await _publish(service)

    error = exc_info.value
    forbidden_values = [
        expected.content_digest.hexadecimal,
        str(compute_request_fingerprint(build_create_command())),
        str(derive_canonical_object_key(expected.content_digest)),
        stale_receipt.verified_at.isoformat(),
        "Publication service fake title",
        "publish-once-001",
    ]
    rendered = f"{error!r} {error!s} {error.to_safe_dict()!r}"
    for forbidden in forbidden_values:
        assert forbidden not in rendered


def test_verified_receipt_is_never_a_public_service_method_argument() -> None:
    public_methods = [
        member
        for name, member in inspect.getmembers(SourceVersionPublicationService, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods, "the service must expose public methods to inspect"
    for method in public_methods:
        for parameter in inspect.signature(method).parameters.values():
            annotation = str(parameter.annotation)
            assert "VerifiedObjectReceipt" not in annotation, (
                f"{method.__name__} must not accept a receipt as a public argument"
            )
