from __future__ import annotations

import pytest

from personal_os.diagnostics.events import (
    EventName,
    ObjectDigestPrefix,
    SafeToken,
    ShortDigest,
)


@pytest.mark.parametrize("value", ["api", "runtime_configuration", "provider.model-1"])
def test_safe_token_accepts_registered_ascii_shape(value: str) -> None:
    assert str(SafeToken.parse(value)) == value


@pytest.mark.parametrize("value", ["", "UPPER", "has space", "secret/value", "x" * 65])
def test_safe_token_rejects_unbounded_or_unsafe_text(value: str) -> None:
    with pytest.raises(ValueError, match="safe token"):
        SafeToken.parse(value)


def test_short_digest_requires_sixteen_lowercase_hex_characters() -> None:
    assert str(ShortDigest.parse("0123456789abcdef")) == "0123456789abcdef"
    with pytest.raises(ValueError, match="digest"):
        ShortDigest.parse("0123456789ABCDEf")


def test_object_digest_prefix_is_distinct_from_short_digest_length() -> None:
    # ObjectDigestPrefix accepts exactly 12 lowercase hex characters; it must not
    # widen or alias the existing 16-character ShortDigest contract.
    assert str(ObjectDigestPrefix.parse("0123456789ab")) == "0123456789ab"
    with pytest.raises(ValueError, match="object digest prefix"):
        ObjectDigestPrefix.parse("0123456789abcdef")  # 16 chars accepted by ShortDigest only
    with pytest.raises(ValueError, match="digest"):
        ShortDigest.parse("0123456789ab")  # 12 chars rejected by ShortDigest


def test_event_names_are_closed() -> None:
    assert {event.value for event in EventName} == {
        "runtime_configuration_validated",
        "runtime_configuration_failed",
        "client_request_id_rejected",
        "trace_context_replaced",
        "logging_payload_rejected",
        "dependency_log",
        "internal_error",
        "object_storage_operation_succeeded",
        "object_storage_operation_failed",
        "object_storage_object_deduplicated",
        "object_storage_integrity_failed",
        "object_storage_spool_cleanup_degraded",
        "source_version_publish_succeeded",
        "source_version_publish_replayed",
        "source_version_publish_rejected",
        "source_version_publish_failed",
        "projection_intent_dispatched",
        "projection_intent_dispatch_failed",
        "projection_intent_lease_reclaimed",
        "identity_bootstrap_succeeded",
        "identity_bootstrap_replayed",
        "identity_bootstrap_rejected",
        "canonical_source_read_succeeded",
        "canonical_source_read_failed",
        "canonical_backup_created",
        "canonical_backup_verified",
        "canonical_backup_failed",
        "canonical_restore_succeeded",
        "canonical_restore_failed",
        "canonical_acceptance_completed",
        "canonical_acceptance_failed",
        "api_request_completed",
        "api_request_rejected",
        "api_request_failed",
        "exclusion_policy_evaluation_completed",
        "exclusion_policy_evaluation_rejected",
    }
