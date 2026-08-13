"""Boundary tests for safe diagnostic payload validation and fingerprinting."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import pytest

from personal_os.diagnostics.events import (
    EVENT_DEFINITIONS,
    DiagnosticEvent,
    EventName,
    RejectedDiagnosticPayload,
    SafeToken,
    ShortDigest,
    build_registered_event,
)
from personal_os.diagnostics.redaction import (
    fingerprint_stack,
    fingerprint_text,
    normalize_exception_type,
)

SENTINEL = "do-not-emit"
_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{16}")


def _valid_internal_error_payload() -> dict[str, object]:
    return {
        "error_code": SafeToken.parse("configuration_invalid"),
        "error_category": SafeToken.parse("configuration"),
        "is_retryable": False,
        "exception_type": SafeToken.parse("valueerror"),
        "stack_fingerprint": ShortDigest.parse("0123456789abcdef"),
    }


# --- Step 1: event boundary tests -------------------------------------------------


def test_rejects_unknown_field_without_retaining_value() -> None:
    sentinel = "do-not-emit-unknown-field"

    result = build_registered_event(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": "info", "invented_field": sentinel},
    )

    assert result == RejectedDiagnosticPayload(
        reason=SafeToken.parse("unknown_field"),
        count=1,
    )
    assert sentinel not in repr(result)


def test_rejects_forbidden_normalized_key_recursively() -> None:
    sentinel = "do-not-emit-nested-query"

    result = build_registered_event(
        EventName.RUNTIME_CONFIGURATION_FAILED,
        {
            "error_code": "configuration_invalid",
            "error_category": "configuration",
            "is_retryable": False,
            "reason": "validation_failed",
            "count": 1,
            "metadata": {"citation-text": sentinel},
        },
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert sentinel not in repr(result)


def test_rejects_missing_required_field() -> None:
    result = build_registered_event(
        EventName.RUNTIME_CONFIGURATION_FAILED,
        {
            "error_code": SafeToken.parse("configuration_invalid"),
            "error_category": SafeToken.parse("configuration"),
        },
    )

    assert result == RejectedDiagnosticPayload(
        reason=SafeToken.parse("missing_field"),
        count=1,
    )


# --- Step 2: sensitive value patterns --------------------------------------------


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "Bearer do-not-emit-bearer",
        "eyJhbGciOiJIUzI1NiJ9.ZG8tbm90LWVtaXQtand0.c2lnbmF0dXJl",
        "-----BEGIN PRIVATE KEY-----\ndo-not-emit-pem",
        "https://user:do-not-emit-password@example.test/resource",
        "https://example.test/object?X-Amz-Credential=do-not-emit-credential",
        "https://example.test/object?X-Amz-Signature=do-not-emit-signature",
        "https://example.test/object?X-Goog-Credential=do-not-emit-google",
        "https://example.test/object?sig=do-not-emit-signature",
        "https://example.test/object?token=do-not-emit-token",
    ],
)
def test_rejects_sensitive_value_patterns(unsafe_value: str) -> None:
    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": unsafe_value},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"
    assert unsafe_value not in repr(result)


# --- Step 2: forbidden normalized key families -----------------------------------


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "content",
        "body",
        "query",
        "excerpt",
        "citation_text",
        "prompt",
        "completion",
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "cookie",
        "signed_url",
        "path",
        "vector",
        "embedding",
        "traceback",
        "exception_message",
    ],
)
def test_rejects_forbidden_normalized_key_families(forbidden_key: str) -> None:
    payload_value = f"{SENTINEL}-{forbidden_key}"

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": {forbidden_key: payload_value}},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"
    assert payload_value not in repr(result)
    assert forbidden_key not in repr(result)


# --- Step 2: structural edge cases ------------------------------------------------


def test_rejects_mapping_nested_eight_levels_deep() -> None:
    nested: object = f"{SENTINEL}-deep"
    for _ in range(8):
        nested = {"level": nested}

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": nested},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"
    assert f"{SENTINEL}-deep" not in repr(result)


def test_rejects_sequence_longer_than_sixty_four_items() -> None:
    items = tuple(SafeToken.parse(f"v{i:03d}") for i in range(65))

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": items},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"


def test_rejects_recursive_looking_container_without_cycle() -> None:
    nested: object = f"{SENTINEL}-recursive"
    for _ in range(30):
        nested = {"a": [nested]}

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": nested},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"
    assert f"{SENTINEL}-recursive" not in repr(result)


def test_rejects_non_string_key() -> None:
    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": {123: f"{SENTINEL}-nonstring-key"}},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"
    assert f"{SENTINEL}-nonstring-key" not in repr(result)


def test_rejects_object_whose_str_and_repr_raise() -> None:
    class HostileScalar:
        def __str__(self) -> str:
            raise RuntimeError(f"{SENTINEL}-str")

        def __repr__(self) -> str:
            raise RuntimeError(f"{SENTINEL}-repr")

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": HostileScalar()},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"
    assert SENTINEL not in repr(result)


def test_rejects_negative_integer() -> None:
    payload = _valid_internal_error_payload()
    payload["is_retryable"] = -1

    result = build_registered_event(EventName.INTERNAL_ERROR, payload)

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"


def test_rejects_integer_above_max_safe_integer() -> None:
    payload = _valid_internal_error_payload()
    payload["is_retryable"] = 2**63

    result = build_registered_event(EventName.INTERNAL_ERROR, payload)

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_rejects_non_finite_float(value: float) -> None:
    payload = _valid_internal_error_payload()
    payload["is_retryable"] = value

    result = build_registered_event(EventName.INTERNAL_ERROR, payload)

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"


def test_rejects_noncanonical_uuid_string() -> None:
    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": "550E8400-E29B-41D4-A716-446655440000"},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"


# --- Acceptance tests -------------------------------------------------------------


def test_accepts_valid_scalar_payload() -> None:
    result = build_registered_event(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": SafeToken.parse("info")},
    )

    assert isinstance(result, DiagnosticEvent)
    assert result.definition is EVENT_DEFINITIONS[EventName.RUNTIME_CONFIGURATION_VALIDATED]
    assert set(result.fields) == {"configured_log_level"}
    assert result.fields["configured_log_level"] == SafeToken.parse("info")


def test_accepts_internal_error_with_mixed_scalar_types() -> None:
    result = build_registered_event(EventName.INTERNAL_ERROR, _valid_internal_error_payload())

    assert isinstance(result, DiagnosticEvent)
    assert result.fields["is_retryable"] is False
    assert result.fields["exception_type"] == SafeToken.parse("valueerror")
    assert result.fields["stack_fingerprint"] == ShortDigest.parse("0123456789abcdef")


def test_accepts_uuid_scalar() -> None:
    request_id = uuid4()

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": request_id},
    )

    assert isinstance(result, DiagnosticEvent)
    assert result.fields["reason"] == request_id


def test_accepts_flat_tuple_of_scalars() -> None:
    tokens = (SafeToken.parse("alpha"), SafeToken.parse("beta"), UUID(int=0))

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": tokens},
    )

    assert isinstance(result, DiagnosticEvent)
    assert result.fields["reason"] == tokens


def test_accepts_tuple_at_sixty_four_item_limit() -> None:
    tokens = tuple(SafeToken.parse(f"v{i:03d}") for i in range(64))

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": tokens},
    )

    assert isinstance(result, DiagnosticEvent)


def test_does_not_retain_caller_mapping() -> None:
    fields: dict[str, object] = {"configured_log_level": SafeToken.parse("info")}

    result = build_registered_event(EventName.RUNTIME_CONFIGURATION_VALIDATED, fields)

    assert isinstance(result, DiagnosticEvent)
    fields["configured_log_level"] = SafeToken.parse("debug")
    fields["extra"] = SafeToken.parse("x")
    assert result.fields["configured_log_level"] == SafeToken.parse("info")
    assert "extra" not in result.fields


def test_accepted_fields_are_immutable() -> None:
    result = build_registered_event(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": SafeToken.parse("info")},
    )

    assert isinstance(result, DiagnosticEvent)
    with pytest.raises(TypeError):
        result.fields["configured_log_level"] = SafeToken.parse("debug")  # type: ignore[index]


# --- Step 7: deterministic fingerprints ------------------------------------------


def test_fingerprint_text_is_stable_distinct_and_hex() -> None:
    digest_a1 = fingerprint_text("dependency-message-a")
    digest_a2 = fingerprint_text("dependency-message-a")
    digest_b = fingerprint_text("dependency-message-b")

    assert digest_a1 == digest_a2
    assert digest_a1 != digest_b
    assert _DIGEST_PATTERN.fullmatch(str(digest_a1)) is not None
    assert isinstance(digest_a1, ShortDigest)


def test_fingerprint_stack_matches_for_same_code_location() -> None:
    def _capture(message: str) -> ValueError:
        try:
            raise ValueError(message)
        except ValueError as exc:
            return exc

    exc_a = _capture(f"{SENTINEL}-stack-a")
    exc_b = _capture(f"{SENTINEL}-stack-b")

    fingerprint_a = fingerprint_stack(exc_a)
    fingerprint_b = fingerprint_stack(exc_b)

    assert fingerprint_a == fingerprint_b
    assert _DIGEST_PATTERN.fullmatch(str(fingerprint_a)) is not None
    assert f"{SENTINEL}-stack-a" not in repr(fingerprint_a)
    assert f"{SENTINEL}-stack-b" not in repr(fingerprint_b)


def test_fingerprint_stack_without_traceback_is_stable() -> None:
    fingerprint_a = fingerprint_stack(ValueError(f"{SENTINEL}-unraised-a"))
    fingerprint_b = fingerprint_stack(key_error_factory(f"{SENTINEL}-unraised-b"))

    assert fingerprint_a == fingerprint_b
    assert _DIGEST_PATTERN.fullmatch(str(fingerprint_a)) is not None
    assert f"{SENTINEL}-unraised-a" not in repr(fingerprint_a)


def key_error_factory(message: str) -> KeyError:
    return KeyError(message)


def test_normalize_exception_type_returns_safe_token() -> None:
    token = normalize_exception_type(ValueError("ignored"))

    assert isinstance(token, SafeToken)
    assert _TOKEN_PATTERN.fullmatch(str(token)) is not None


def test_normalize_exception_type_survives_hostile_str() -> None:
    class HostileError(Exception):
        def __str__(self) -> str:
            raise RuntimeError(f"{SENTINEL}-str")

        def __repr__(self) -> str:
            raise RuntimeError(f"{SENTINEL}-repr")

    token = normalize_exception_type(HostileError())
    stack = fingerprint_stack(HostileError())

    assert isinstance(token, SafeToken)
    assert _TOKEN_PATTERN.fullmatch(str(token)) is not None
    assert SENTINEL not in str(token)
    assert isinstance(stack, ShortDigest)
    assert _DIGEST_PATTERN.fullmatch(str(stack)) is not None


def test_build_registered_event_survives_hostile_repr_payload() -> None:
    class HostilePayload:
        def __repr__(self) -> str:
            raise RuntimeError(f"{SENTINEL}-repr")

    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": HostilePayload()},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert str(result.reason) == "unsafe_value"
    assert SENTINEL not in repr(result)
