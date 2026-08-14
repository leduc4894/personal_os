"""Pure R2 failure classification, bounded retry loop and error mapping.

The adapter owns retry so attempt count, the five-minute deadline, error mapping
and diagnostics stay deterministic. This module is intentionally pure and
injectable: :class:`RetryPolicy` takes a monotonic clock, an awaitable sleep and
a jitter function so the retry loop, backoff and deadline are fully testable
without real time.

The closed :class:`RetryDecision` enum is the complete set of outcomes for any
S3/R2 failure: ``RETRY`` for transient transport or availability conditions,
``CONDITIONAL_CONFLICT`` for a conditional-create ``412 PreconditionFailed``
(that transitions the adapter to winner verification, not a normal retry), and
``TERMINAL`` for access denial, a missing bucket, a missing object, malformed
provider responses and unsupported non-transient ``4xx`` responses.

Provider exception classes, response bodies, request ids, headers and messages
remain chained only as the cause; they are never copied into a mapped
:class:`ObjectStorageError`, its safe details or diagnostics.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ReadTimeoutError,
    ResponseStreamingError,
)
from botocore.exceptions import ConnectionError as BotoCoreConnectionError

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage.errors import ObjectStorageError


class RetryDecision(StrEnum):
    """The closed set of outcomes for a classified R2 failure."""

    RETRY = "retry"
    CONDITIONAL_CONFLICT = "conditional_conflict"
    TERMINAL = "terminal"


class ConditionalCreateConflict(Exception):
    """Signal that a conditional PUT lost the immutable-key race.

    Raised by :meth:`RetryPolicy.run` when the cause is a conditional-create
    ``412 PreconditionFailed`` so the adapter can transition directly to winner
    verification instead of mapping the failure to a typed dependency error. It
    is internal to the provider boundary and carries no provider value.
    """


# S3/R2 ``Error.Code`` strings, confirmed against the design spec §11 retry
# matrix and the documented S3 error set. ``BadDigest``/``InvalidDigest`` are
# deliberately absent: the design pins both as NON-RETRYABLE terminal failures.
_RETRYABLE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "SlowDown",  # registered R2 throttling code (HTTP 503).
    }
)
_ACCESS_DENIED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "Unauthorized",
        "UnauthorizedAccess",
    }
)
_OBJECT_MISSING_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "NoSuchKey",
        "NoSuchObject",
        "NotFound",  # the code synthesised for a HEAD object absence.
    }
)
_TRANSIENT_HTTP_STATUSES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})

# Transient transport failures raised by botocore before/while reading a
# response. ``BotoCoreConnectionError`` covers ``EndpointConnectionError`` and
# ``ConnectTimeoutError``; the remaining three are ``HTTPClientError`` subtypes.
_TRANSIENT_TRANSPORT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    BotoCoreConnectionError,
    ConnectionClosedError,
    ReadTimeoutError,
    ResponseStreamingError,
)


def _extract_client_error_info(cause: ClientError) -> tuple[str | None, int | None]:
    """Return the ``(error_code, http_status)`` pair safe to read from a cause.

    Any missing, malformed or wrongly-typed field collapses to ``None`` so a
    malformed response is classified deterministically as terminal rather than
    raising while mapping. No provider value leaves this helper except the code
    token and the integer status, which are the only values used for routing.
    """

    response = cause.response
    if not isinstance(response, dict):
        return None, None
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, dict) else None
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return (
        code if isinstance(code, str) else None,
        status if isinstance(status, int) and not isinstance(status, bool) else None,
    )


def classify_r2_failure(cause: BaseException) -> RetryDecision:
    """Classify an R2 failure into the closed :class:`RetryDecision` set.

    Pure and side-effect free: it inspects only the exception type and the safe
    code/status fields, never the provider message, request id or endpoint. An
    unrecognized exception fails closed as ``TERMINAL``.
    """

    if isinstance(cause, ClientError):
        code, status = _extract_client_error_info(cause)
        if code == "PreconditionFailed":
            return RetryDecision.CONDITIONAL_CONFLICT
        if code in _RETRYABLE_ERROR_CODES:
            return RetryDecision.RETRY
        if code == "NoSuchBucket":
            return RetryDecision.TERMINAL
        if code in _OBJECT_MISSING_ERROR_CODES:
            return RetryDecision.TERMINAL
        if code in _ACCESS_DENIED_ERROR_CODES or status in (401, 403):
            return RetryDecision.TERMINAL
        if status in _TRANSIENT_HTTP_STATUSES:
            return RetryDecision.RETRY
        return RetryDecision.TERMINAL
    if isinstance(cause, _TRANSIENT_TRANSPORT_ERRORS):
        return RetryDecision.RETRY
    return RetryDecision.TERMINAL


def map_r2_failure(cause: BaseException, *, exhausted: bool) -> ObjectStorageError:
    """Map a classified R2 failure to a typed :class:`ObjectStorageError`.

    ``exhausted`` records whether the retry deadline elapsed (as opposed to the
    attempt budget); both give-up modes for a transient cause yield the same
    ``object_storage_unavailable`` code, and it does not alter the code selected
    for a terminal cause. The parameter is retained for signature fidelity with
    :meth:`RetryPolicy.run` and for future diagnostic differentiation.

    The provider exception remains only as the chained ``__cause__``; its
    message, request id, headers and body never enter the returned error.
    """

    decision = classify_r2_failure(cause)
    if decision is RetryDecision.RETRY:
        return ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE)
    if decision is RetryDecision.CONDITIONAL_CONFLICT:
        # The retry loop raises ConditionalCreateConflict before reaching here;
        # this branch is defensive and never executed on the documented path.
        return ObjectStorageError(ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT)
    if isinstance(cause, ClientError):
        code, status = _extract_client_error_info(cause)
        if code == "NoSuchBucket":
            return ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE)
        if code in _OBJECT_MISSING_ERROR_CODES:
            return ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
        if code in _ACCESS_DENIED_ERROR_CODES or status in (401, 403):
            return ObjectStorageError(ErrorCode.OBJECT_STORAGE_ACCESS_DENIED)
        return ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID)
    return ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    maximum_attempts: int = 3
    operation_deadline_seconds: float = 300.0

    async def run[T](
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> T:
        started = monotonic()
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                return await operation(attempt)
            except asyncio.CancelledError:
                raise
            except Exception as cause:
                decision = classify_r2_failure(cause)
                if decision is RetryDecision.CONDITIONAL_CONFLICT:
                    raise ConditionalCreateConflict() from cause
                elapsed = monotonic() - started
                remaining = self.operation_deadline_seconds - elapsed
                if (
                    decision is RetryDecision.TERMINAL
                    or attempt == self.maximum_attempts
                    or remaining <= 0
                ):
                    raise map_r2_failure(cause, exhausted=remaining <= 0) from cause
                maximum_delay = min(2.0 ** (attempt - 1), 30.0, remaining)
                await sleep(jitter(0.0, maximum_delay))
        raise AssertionError("retry loop exhausted without a result")
