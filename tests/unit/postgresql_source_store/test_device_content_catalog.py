"""Device content catalog statement scope, hydration and policy seams.

These tests pin the pure pieces of the exact-version content catalog without
a database: the descriptor read is credential-scoped and parameter-bound (no
literal workspace, source or version value appears in compiled SQL, and the
object key or any other provider-addressing column is never selected), the
membership hydration maps a missing row — an unknown pair and a
cross-workspace pair alike — onto the closed event-unavailable rejection
while stored evidence that violates the canonical digest/media grammar maps
onto the closed download integrity failure, and the download policy seam
authorizes the resolved version's content subject under the workspace's
active revision, failing closed when a rule requires evidence the download
path cannot supply. Durable transaction behavior is integration territory
(the disposable stack suite).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import ExclusionPolicyRevision, RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from postgresql_source_store.device_content_catalog import (
    device_content_descriptor_statement,
    evaluate_device_content_policy,
    hydrate_device_content_descriptor,
)

_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000001")
_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000002")
_SOURCE_VERSION_ID = UUID("018f47a0-7b00-7000-8000-000000000003")

_CONTENT_HASH = "a" * 64
_MEDIA_TYPE = "text/markdown"


def _bind_marker(text: str, parameter: str) -> bool:
    """Check whether a parameter-bound marker is in the SQL text."""

    if f"%({parameter})s" in text:
        return True
    return any(
        marker in text
        for marker in (
            f"%({parameter}_1)s",
            f"%({parameter}_2)s",
            f"%({parameter}_3)s",
        )
    )


def _row(
    *,
    content_hash: str = _CONTENT_HASH,
    byte_size: Any = 21,
    media_type: str = _MEDIA_TYPE,
    source_type: str = "markdown",
) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "content_hash": content_hash,
        "byte_size": byte_size,
        "media_type": media_type,
    }


# --- statement scope ---------------------------------------------------------


def test_descriptor_statement_scopes_membership_to_the_credential_workspace() -> None:
    text = str(
        device_content_descriptor_statement(_WORKSPACE_ID, _SOURCE_ID, _SOURCE_VERSION_ID).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "FROM knowledge.source_versions" in text
    assert "JOIN knowledge.sources" in text
    assert "JOIN knowledge.content_objects" in text
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "source_id")
    assert _bind_marker(text, "source_version_id")
    assert str(_WORKSPACE_ID) not in text
    assert str(_SOURCE_ID) not in text
    assert str(_SOURCE_VERSION_ID) not in text


def test_descriptor_statement_never_selects_the_object_key() -> None:
    text = str(
        device_content_descriptor_statement(_WORKSPACE_ID, _SOURCE_ID, _SOURCE_VERSION_ID).compile(
            dialect=postgresql.dialect()
        )
    )
    selected = text.split("FROM", 1)[0]
    assert "object_key" not in text
    assert "content_hash" in selected
    assert "byte_size" in selected
    assert "media_type" in selected


# --- membership and evidence hydration ---------------------------------------


def test_missing_row_is_the_closed_event_unavailable_rejection() -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_content_descriptor(
            source_id=_SOURCE_ID, source_version_id=_SOURCE_VERSION_ID, row=None
        )
    # An unknown pair and a cross-workspace pair hydrate from the same
    # missing row: foreign content is indistinguishable from absent content.
    assert raised.value.code is DeviceSyncErrorCode.EVENT_UNAVAILABLE


def test_valid_row_hydrates_a_descriptor_without_any_key_surface() -> None:
    descriptor = hydrate_device_content_descriptor(
        source_id=_SOURCE_ID,
        source_version_id=_SOURCE_VERSION_ID,
        row=_row(byte_size=21),
    )
    assert descriptor.source_id == _SOURCE_ID
    assert descriptor.source_version_id == _SOURCE_VERSION_ID
    assert descriptor.content_digest == ContentDigest.parse(_CONTENT_HASH)
    assert descriptor.size_bytes == 21
    assert descriptor.media_type == CanonicalMediaType.parse(_MEDIA_TYPE)
    assert not hasattr(descriptor, "object_key")
    assert "<redacted>" in repr(descriptor)


@pytest.mark.parametrize(
    ("content_hash", "media_type", "byte_size"),
    (
        ("A" * 64, _MEDIA_TYPE, 21),  # uppercase digest violates the grammar
        (_CONTENT_HASH, "text", 21),  # media type without a subtype
        (_CONTENT_HASH, _MEDIA_TYPE, -1),  # negative byte size
        (_CONTENT_HASH, _MEDIA_TYPE, "many"),  # non-integer size evidence
    ),
)
def test_corrupt_stored_evidence_is_the_closed_download_integrity_failure(
    content_hash: str, media_type: str, byte_size: Any
) -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_content_descriptor(
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            row=_row(content_hash=content_hash, media_type=media_type, byte_size=byte_size),
        )
    assert raised.value.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED


def test_missing_evidence_column_is_the_closed_download_integrity_failure() -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_content_descriptor(
            source_id=_SOURCE_ID, source_version_id=_SOURCE_VERSION_ID, row={}
        )
    assert raised.value.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED


# --- download policy seam ----------------------------------------------------


def _revision(*rules: Any) -> ExclusionPolicyRevision:
    return ExclusionPolicyRevision(
        policy_revision_id=uuid4(),
        workspace_id=_WORKSPACE_ID,
        revision_number=1,
        rules=tuple(rules),
    )


def test_empty_active_revision_allows_the_content_subject() -> None:
    evaluate_device_content_policy(
        _revision(),
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        source_type="markdown",
        media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
        size_bytes=21,
    )


def test_matching_media_rule_denies_the_download_subject() -> None:
    rule = normalize_rule(uuid4(), RuleKind.MEDIA_TYPE, text_operand=_MEDIA_TYPE)
    with pytest.raises(ExclusionPolicyError) as raised:
        evaluate_device_content_policy(
            _revision(rule),
            workspace_id=_WORKSPACE_ID,
            source_id=_SOURCE_ID,
            source_type="markdown",
            media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
            size_bytes=21,
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED


def test_other_source_id_rule_does_not_deny_this_download() -> None:
    rule = normalize_rule(uuid4(), RuleKind.EXACT_SOURCE_ID, source_id_operand=uuid4())
    evaluate_device_content_policy(
        _revision(rule),
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        source_type="markdown",
        media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
        size_bytes=21,
    )


def test_locator_rule_fails_closed_without_locator_evidence() -> None:
    rule = normalize_rule(uuid4(), RuleKind.FOLDER_PREFIX, text_operand="notes")
    with pytest.raises(ExclusionPolicyError) as raised:
        evaluate_device_content_policy(
            _revision(rule),
            workspace_id=_WORKSPACE_ID,
            source_id=_SOURCE_ID,
            source_type="markdown",
            media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
            size_bytes=21,
        )
    # A download authorizes exact bytes, not a place: locator-requiring rules
    # evaluate indeterminate and fail closed as the registry denial.
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED


def test_media_subject_decides_when_the_media_rule_misses() -> None:
    rule = normalize_rule(uuid4(), RuleKind.MEDIA_TYPE, text_operand="application/pdf")
    evaluate_device_content_policy(
        _revision(rule),
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        source_type="markdown",
        media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
        size_bytes=21,
    )


def test_unknown_source_type_token_fails_closed_for_source_type_rules() -> None:
    rule = normalize_rule(uuid4(), RuleKind.SOURCE_TYPE, text_operand="pdf")
    # An unrecognized stored source type becomes absent evidence, so a
    # source-type rule evaluates indeterminate and fails closed.
    with pytest.raises(ExclusionPolicyError) as raised:
        evaluate_device_content_policy(
            _revision(rule),
            workspace_id=_WORKSPACE_ID,
            source_id=_SOURCE_ID,
            source_type="alien-type",
            media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
            size_bytes=21,
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
