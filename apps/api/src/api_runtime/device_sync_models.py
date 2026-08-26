"""Strict device sync wire models and the domain boundary conversion.

Every model here is frozen and closed for extra fields and mirrors the wire
grammar of spec 7 exactly: no request body carries a workspace, device or
user selector — those derive from the access Bearer credential — and the
entry evidence carries only the opaque plugin-local identity, the optional
client source/version evidence, the normalized locator, the settled-byte
fingerprint and the local observation generation. Conversion to the frozen
domain values happens only through this boundary: each domain-owned grammar
(locator, media type, nil evidence identifiers, digest) surfaces as the
closed ``device_manifest_page_invalid`` or ``device_manifest_digest_mismatch``
typed error, never an echoed value. The response renderers project the
frozen domain results onto strict payloads carrying only the safe members of
spec 7.1/7.3 — never a receipt, object key, provider detail or fingerprint
beyond the canonical evidence the spec publishes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from personal_os.device_sync.contracts import (
    MAX_MANIFEST_PAGE_ENTRIES,
    MAX_MANIFEST_RUN_ENTRIES,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceEventType,
    DeviceSyncEvent,
    ManifestAction,
    ManifestActionKind,
    ManifestActionPage,
    ManifestActionReason,
    ManifestEntry,
    ManifestPageReceipt,
    ManifestRunReceipt,
    ManifestRunState,
    SourceFingerprint,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.object_storage import ContentDigest
from personal_os.source_locators.values import NormalizedLocator

#: Wire grammar of the exact lowercase content digest (64 hex characters).
_SHA256_PATTERN: Final[str] = r"^[0-9a-f]{64}$"

#: Maximum length of the opaque plugin-local manifest entry ID.
_LOCAL_ENTRY_ID_MAXIMUM_LENGTH: Final[int] = 256


class SourceFingerprintData(BaseModel):
    """The settled-byte hash/size/media identity evidence of one version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    media_type: str


class CursorAcknowledgementRequest(BaseModel):
    """The strict cursor acknowledgement body (spec 7.2).

    Carries only the expected prior sequence and the applied-through
    watermark — never a workspace, device or user selector.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_previous_sequence: int = Field(ge=0)
    applied_through_sequence: int = Field(ge=0)


class ManifestStartRequest(BaseModel):
    """The strict manifest run start body (spec 7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_observation_generation: int = Field(ge=0)


class ManifestEntryRequest(BaseModel):
    """One locally observed manifest entry of one page body (spec 7.3).

    Carries the opaque plugin-local entry ID, the optional client
    source/version evidence, the normalized locator, the settled-byte
    fingerprint and the local observation generation — never a workspace,
    device or user selector.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_entry_id: str = Field(min_length=1, max_length=_LOCAL_ENTRY_ID_MAXIMUM_LENGTH)
    known_source_id: UUID | None = None
    known_version_id: UUID | None = None
    normalized_locator: str
    fingerprint: SourceFingerprintData
    observation_generation: int = Field(ge=0)


class ManifestPageRequest(BaseModel):
    """The exact next ordered page of one manifest run (spec 7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[ManifestEntryRequest] = Field(max_length=MAX_MANIFEST_PAGE_ENTRIES)
    page_digest: str = Field(pattern=_SHA256_PATTERN)


class ManifestFinalizeRequest(BaseModel):
    """The finalize body with its total count and final digest (spec 7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_entry_count: int = Field(ge=0, le=MAX_MANIFEST_RUN_ENTRIES)
    final_digest: str = Field(pattern=_SHA256_PATTERN)


class ManifestCompleteRequest(BaseModel):
    """The completion body with the exact planned run's final digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    final_digest: str = Field(pattern=_SHA256_PATTERN)


class DeviceSyncEventData(BaseModel):
    """One immutable canonical sync event with its operation operands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    event_sequence: int
    event_type: DeviceEventType
    source_id: UUID
    origin_device_id: UUID | None = None
    base_version_id: UUID | None = None
    current_version_id: UUID | None = None
    base_fingerprint: SourceFingerprintData | None = None
    current_fingerprint: SourceFingerprintData | None = None
    prior_locator: str | None = None
    resulting_locator: str | None = None
    tombstone_id: UUID | None = None
    committed_at: datetime


class DeviceEventPageData(BaseModel):
    """One bounded pull page of immutable events (spec 7.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    acknowledged_sequence: int
    page_checkpoint_sequence: int
    delivered_through_sequence: int
    events: tuple[DeviceSyncEventData, ...]
    has_more: bool


class DeviceCursorReceiptData(BaseModel):
    """The frozen cursor watermarks of one device (spec 7.2/7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    acknowledged_sequence: int
    delivered_through_sequence: int


class ManifestRunReceiptData(BaseModel):
    """The frozen state of one manifest run (spec 7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_run_id: UUID
    state: ManifestRunState
    base_acknowledged_sequence: int
    checkpoint_sequence: int
    policy_revision_number: int
    client_observation_generation: int
    next_page_number: int
    entry_count: int
    expires_at: datetime


class ManifestPageReceiptData(BaseModel):
    """The frozen acceptance of one manifest page (spec 7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_run_id: UUID
    page_number: int
    accepted_entry_count: int
    next_page_number: int


class ManifestActionData(BaseModel):
    """One frozen deterministic action of a planned run (spec 7.3).

    A ``download`` action publishes the checkpoint-active locator text the
    device must place its bytes at — hydrated at read time from the
    canonical locator row, never persisted on a manifest table; every other
    kind renders the field closed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_index: int
    action_kind: ManifestActionKind
    local_entry_id: str | None = None
    source_id: UUID | None = None
    source_version_id: UUID | None = None
    source_locator_id: UUID | None = None
    source_tombstone_id: UUID | None = None
    reason: ManifestActionReason | None = None
    checkpoint_locator: str | None = None


class ManifestActionPageData(BaseModel):
    """One stable ordered page of frozen actions (spec 7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_run_id: UUID
    actions: tuple[ManifestActionData, ...]
    has_more: bool


def _page_invalid() -> DeviceSyncError:
    return DeviceSyncError(DeviceSyncErrorCode.MANIFEST_PAGE_INVALID)


def _fingerprint_data(fingerprint: SourceFingerprint) -> SourceFingerprintData:
    return SourceFingerprintData(
        sha256=fingerprint.sha256,
        size_bytes=fingerprint.size_bytes,
        media_type=fingerprint.media_type,
    )


def to_domain_entry(entry: ManifestEntryRequest) -> ManifestEntry:
    """Convert one strict wire entry into the frozen domain entry.

    The locator, media type and evidence-identifier grammars stay owned by
    the domain; every violation surfaces as the closed page-invalid typed
    error without echoing the rejected value.
    """

    try:
        locator = NormalizedLocator(entry.normalized_locator)
    except ValueError:
        raise _page_invalid() from None
    try:
        fingerprint = SourceFingerprint(
            sha256=entry.fingerprint.sha256,
            size_bytes=entry.fingerprint.size_bytes,
            media_type=entry.fingerprint.media_type,
        )
    except ValueError:
        raise _page_invalid() from None
    try:
        return ManifestEntry(
            local_entry_id=entry.local_entry_id,
            known_source_id=entry.known_source_id,
            known_version_id=entry.known_version_id,
            normalized_locator=locator,
            fingerprint=fingerprint,
            observation_generation=entry.observation_generation,
        )
    except ValueError:
        raise _page_invalid() from None


def to_domain_entries(entries: Sequence[ManifestEntryRequest]) -> tuple[ManifestEntry, ...]:
    """Convert the strict page entries into the frozen domain tuple."""

    return tuple(to_domain_entry(entry) for entry in entries)


def parse_page_digest(value: str) -> ContentDigest:
    """Parse one wire page digest, closing a violation as page-invalid."""

    try:
        return ContentDigest.parse(value)
    except ValueError:
        raise _page_invalid() from None


def parse_final_digest(value: str) -> ContentDigest:
    """Parse one wire final digest, closing a violation as digest-mismatch."""

    try:
        return ContentDigest.parse(value)
    except ValueError:
        raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH) from None


def _event_data(event: DeviceSyncEvent) -> DeviceSyncEventData:
    return DeviceSyncEventData(
        event_id=event.event_id,
        event_sequence=event.event_sequence,
        event_type=event.event_type,
        source_id=event.source_id,
        origin_device_id=event.origin_device_id,
        base_version_id=event.base_version_id,
        current_version_id=event.current_version_id,
        base_fingerprint=(
            _fingerprint_data(event.base_fingerprint)
            if event.base_fingerprint is not None
            else None
        ),
        current_fingerprint=(
            _fingerprint_data(event.current_fingerprint)
            if event.current_fingerprint is not None
            else None
        ),
        prior_locator=(event.prior_locator.value if event.prior_locator is not None else None),
        resulting_locator=(
            event.resulting_locator.value if event.resulting_locator is not None else None
        ),
        tombstone_id=event.tombstone_id,
        committed_at=event.committed_at,
    )


def device_event_page_data(page: DeviceEventPage) -> DeviceEventPageData:
    """Render one frozen event page onto its strict wire payload."""

    return DeviceEventPageData(
        acknowledged_sequence=page.acknowledged_sequence,
        page_checkpoint_sequence=page.page_checkpoint_sequence,
        delivered_through_sequence=page.delivered_through_sequence,
        events=tuple(_event_data(event) for event in page.events),
        has_more=page.has_more,
    )


def device_cursor_receipt_data(receipt: DeviceCursorReceipt) -> DeviceCursorReceiptData:
    """Render one frozen cursor receipt onto its strict wire payload."""

    return DeviceCursorReceiptData(
        acknowledged_sequence=receipt.acknowledged_sequence,
        delivered_through_sequence=receipt.delivered_through_sequence,
    )


def manifest_run_receipt_data(receipt: ManifestRunReceipt) -> ManifestRunReceiptData:
    """Render one frozen run receipt onto its strict wire payload."""

    return ManifestRunReceiptData(
        manifest_run_id=receipt.manifest_run_id,
        state=receipt.state,
        base_acknowledged_sequence=receipt.base_acknowledged_sequence,
        checkpoint_sequence=receipt.checkpoint_sequence,
        policy_revision_number=receipt.policy_revision_number,
        client_observation_generation=receipt.client_observation_generation,
        next_page_number=receipt.next_page_number,
        entry_count=receipt.entry_count,
        expires_at=receipt.expires_at,
    )


def manifest_page_receipt_data(receipt: ManifestPageReceipt) -> ManifestPageReceiptData:
    """Render one frozen page receipt onto its strict wire payload."""

    return ManifestPageReceiptData(
        manifest_run_id=receipt.manifest_run_id,
        page_number=receipt.page_number,
        accepted_entry_count=receipt.accepted_entry_count,
        next_page_number=receipt.next_page_number,
    )


def _action_data(action: ManifestAction) -> ManifestActionData:
    return ManifestActionData(
        action_index=action.action_index,
        action_kind=action.action_kind,
        local_entry_id=action.local_entry_id,
        source_id=action.source_id,
        source_version_id=action.source_version_id,
        source_locator_id=action.source_locator_id,
        source_tombstone_id=action.source_tombstone_id,
        reason=action.reason,
        checkpoint_locator=(
            action.checkpoint_locator.value if action.checkpoint_locator is not None else None
        ),
    )


def manifest_action_page_data(page: ManifestActionPage) -> ManifestActionPageData:
    """Render one frozen action page onto its strict wire payload."""

    return ManifestActionPageData(
        manifest_run_id=page.manifest_run_id,
        actions=tuple(_action_data(action) for action in page.actions),
        has_more=page.has_more,
    )


__all__ = [
    "CursorAcknowledgementRequest",
    "DeviceCursorReceiptData",
    "DeviceEventPageData",
    "DeviceSyncEventData",
    "ManifestActionData",
    "ManifestActionPageData",
    "ManifestCompleteRequest",
    "ManifestEntryRequest",
    "ManifestFinalizeRequest",
    "ManifestPageReceiptData",
    "ManifestPageRequest",
    "ManifestRunReceiptData",
    "ManifestStartRequest",
    "SourceFingerprintData",
    "device_cursor_receipt_data",
    "device_event_page_data",
    "manifest_action_page_data",
    "manifest_page_receipt_data",
    "manifest_run_receipt_data",
    "parse_final_digest",
    "parse_page_digest",
    "to_domain_entries",
    "to_domain_entry",
]
