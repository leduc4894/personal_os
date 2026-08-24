"""Offline contracts for the closed zero-byte live-test diagnostic boundary."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from tests.integration.r2_object_storage import conftest as live_conftest
from tests.integration.r2_object_storage import test_live_r2_adapter as live_adapter

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.object_storage import VerificationMethod

_PROVIDER_MESSAGE_SENTINEL = "provider-message-sentinel.invalid"


def _provider_client_error() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": _PROVIDER_MESSAGE_SENTINEL},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "PutObject",
    )


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (ApplicationError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE), "object_storage_unavailable"),
        (_provider_client_error(), "provider_client_error"),
        (
            ReadTimeoutError(endpoint_url=_PROVIDER_MESSAGE_SENTINEL, proxy_url=None),
            "provider_timeout",
        ),
        (ConnectTimeoutError(endpoint_url=_PROVIDER_MESSAGE_SENTINEL), "provider_timeout"),
        (
            EndpointConnectionError(endpoint_url=_PROVIDER_MESSAGE_SENTINEL),
            "provider_transport_error",
        ),
        (RuntimeError(_PROVIDER_MESSAGE_SENTINEL), "provider_unclassified_error"),
    ],
)
def test_classifier_returns_only_closed_reason_tokens(
    failure: BaseException, expected_reason: str
) -> None:
    """A classifier change that reads exception text must fail this closed-token contract."""

    diagnostic = live_conftest.ZeroByteLiveDiagnostic(
        stage="store", reason=live_conftest.classify_zero_byte_live_failure(failure)
    )

    serialized = diagnostic.to_json()
    assert json.loads(serialized) == {
        "event": "r2_live_zero_byte_failed",
        "stage": "store",
        "reason": expected_reason,
    }
    assert _PROVIDER_MESSAGE_SENTINEL not in serialized


def test_diagnostic_serializer_rejects_reason_outside_fixed_allowlist() -> None:
    """Allowing an arbitrary reason would let unsafe text enter the record."""

    with pytest.raises(ValueError, match="reason is not allowed"):
        live_conftest.ZeroByteLiveDiagnostic(
            stage="store", reason=_PROVIDER_MESSAGE_SENTINEL
        ).to_json()


class _PrimaryFailure(Exception):
    pass


class _FakeManifest:
    def __len__(self) -> int:
        return 1


class _FakeStore:
    def __init__(
        self, failure_stage: str | None, failure: BaseException, content_digest: object
    ) -> None:
        self._failure_stage = failure_stage
        self._failure = failure
        self._content_digest = content_digest

    async def resolve_verified_object(self, _expected: object) -> object:
        if self._failure_stage == "resolve":
            raise self._failure
        return SimpleNamespace(content_digest=self._content_digest)

    @asynccontextmanager
    async def open_verified_reader(self, _expected: object) -> Any:
        if self._failure_stage == "read":
            raise self._failure
        yield _EmptyReader()


class _EmptyReader:
    def __aiter__(self) -> _EmptyReader:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


class _FakeHarness:
    def __init__(self, failure_stage: str, failure: BaseException) -> None:
        self._failure_stage = failure_stage
        self._failure = failure
        self.manifest = _FakeManifest()
        self._receipt = SimpleNamespace(
            size_bytes=0,
            verification_method=VerificationMethod.UPLOADED_FULL_READ,
            content_digest=object(),
            media_type=object(),
        )
        self.store = _FakeStore(failure_stage, failure, self._receipt.content_digest)

    async def store_payload(self, _payload: bytes, *, media_type: str) -> object:
        assert media_type == "application/octet-stream"
        if self._failure_stage == "store":
            raise self._failure
        return self._receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["store", "resolve", "read"])
async def test_zero_byte_wrapper_reports_the_exact_failing_stage(failure_stage: str) -> None:
    """Moving a body operation to the wrong stage must fail this stage contract."""

    primary = _PrimaryFailure(_PROVIDER_MESSAGE_SENTINEL)
    emitted: list[str] = []

    with pytest.raises(_PrimaryFailure) as raised:
        await live_adapter._run_zero_byte_round_trip(
            _FakeHarness(failure_stage, primary), emit_diagnostic=emitted.append
        )

    assert raised.value is primary
    assert [json.loads(record) for record in emitted] == [
        {
            "event": "r2_live_zero_byte_failed",
            "stage": failure_stage,
            "reason": "provider_unclassified_error",
        }
    ]
    assert _PROVIDER_MESSAGE_SENTINEL not in emitted[0]


@pytest.mark.asyncio
async def test_zero_byte_wrapper_preserves_primary_failure_when_diagnostic_emission_fails() -> None:
    """Replacing the primary body error with an emission error must fail this contract."""

    primary = _PrimaryFailure(_PROVIDER_MESSAGE_SENTINEL)
    emitted: list[str] = []
    emission_attempts = 0

    def fail_once_then_capture(record: str) -> None:
        nonlocal emission_attempts
        emission_attempts += 1
        if emission_attempts == 1:
            raise RuntimeError(_PROVIDER_MESSAGE_SENTINEL)
        emitted.append(record)

    with pytest.raises(_PrimaryFailure) as raised:
        await live_adapter._run_zero_byte_round_trip(
            _FakeHarness("store", primary), emit_diagnostic=fail_once_then_capture
        )

    assert raised.value is primary
    assert [json.loads(record) for record in emitted] == [
        {
            "event": "r2_live_zero_byte_failed",
            "stage": "store",
            "reason": "diagnostic_emission_failed",
        }
    ]
    assert _PROVIDER_MESSAGE_SENTINEL not in emitted[0]
