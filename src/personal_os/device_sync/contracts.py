"""Framework-neutral device sync domain contracts (spec 5.1, 6, 7 and 12).

Immutable value objects and closed vocabularies for server-to-device
synchronization: the credential-derived sync context, the operation-shaped
canonical event operands, the cursor receipts and bounded event pages, the
verified content descriptor, the manifest entry/action evidence and the
manifest run commands, receipts, pages and queries. The module imports no
FastAPI, SQLAlchemy, database driver, R2 SDK or Obsidian type; device and
workspace identity arrive only through the credential-derived
:class:`DeviceSyncContext`, never from request data. Locators, digests and
fingerprints are private values: they never enter diagnostics, metric labels
or a non-redacted ``repr``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.source_locators.values import NormalizedLocator as NormalizedLocator
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import normalize_utc_timestamp

#: Maximum number of immutable events one pull page carries (spec 7.1).
MAX_PULL_EVENTS: Final[int] = 200

#: Maximum number of entries one manifest page carries (spec 6.3/7.3). The
#: same ceiling bounds one read page of manifest actions.
MAX_MANIFEST_PAGE_ENTRIES: Final[int] = 500

#: Maximum cumulative entries one manifest run accepts (spec 6.2).
MAX_MANIFEST_RUN_ENTRIES: Final[int] = 100_000

#: One manifest run expires after exactly one hour of database wall time
#: (spec 6.2), even across mobile suspend.
MANIFEST_RUN_LIFETIME: Final[timedelta] = timedelta(hours=1)

#: Maximum length of the opaque plugin-local manifest entry ID.
_LOCAL_ENTRY_ID_MAXIMUM_LENGTH: Final[int] = 256

#: Fingerprint identity grammar: exactly the canonical SHA-256 digest shape.
_FINGERPRINT_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class DeviceEventType(StrEnum):
    """Closed vocabulary of canonical sync event types (spec 7.1)."""

    CREATED = "created"
    UPDATED = "updated"
    RENAMED = "renamed"
    MOVED = "moved"
    DELETED = "deleted"
    RESTORED = "restored"


class ManifestRunState(StrEnum):
    """Closed lifecycle states of one manifest run (spec 6.2/7.3)."""

    COLLECTING = "collecting"
    PLANNED = "planned"
    APPLYING = "applying"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"


class ManifestActionKind(StrEnum):
    """Closed deterministic action kinds of manifest planning (spec 12.3)."""

    UPLOAD = "upload"
    DOWNLOAD = "download"
    APPLY_TOMBSTONE = "apply_tombstone"
    CONFLICT = "conflict"
    NO_CHANGE = "no_change"
    EXCLUDED = "excluded"


class ManifestActionReason(StrEnum):
    """Closed planner/apply blocker tokens (spec 13).

    These tokens are not route exceptions: they appear only in a conflict,
    excluded or local repair action/trail surface and never turn one
    ambiguous entry into a failed whole-manifest request.
    """

    IDENTITY_AMBIGUOUS = "device_manifest_identity_ambiguous"
    LOCAL_DIVERGED = "device_manifest_local_diverged"
    TARGET_OCCUPIED = "device_manifest_target_occupied"
    ACTION_STALE = "device_manifest_action_stale"
    POLICY_EXCLUDED = "device_manifest_policy_excluded"


class ManifestMatchKind(StrEnum):
    """Closed identity-proof vocabulary of one entry resolution (spec 12.2)."""

    CURRENT_LOCATOR = "current_locator"
    HISTORICAL_LOCATOR_FINGERPRINT = "historical_locator_fingerprint"
    OPEN_TOMBSTONE_FINGERPRINT = "open_tombstone_fingerprint"
    UNPROVEN = "unproven"


#: The event types whose operation shape requires the resulting locator
#: (spec 7.1: create, rename, move and restore).
_EVENT_TYPES_WITH_RESULTING_LOCATOR: Final[frozenset[DeviceEventType]] = frozenset(
    {
        DeviceEventType.CREATED,
        DeviceEventType.RENAMED,
        DeviceEventType.MOVED,
        DeviceEventType.RESTORED,
    }
)

#: The event types whose operation shape requires the prior locator
#: (spec 7.1: rename, move and delete).
_EVENT_TYPES_WITH_PRIOR_LOCATOR: Final[frozenset[DeviceEventType]] = frozenset(
    {DeviceEventType.RENAMED, DeviceEventType.MOVED, DeviceEventType.DELETED}
)

#: The event types whose operation shape requires the exact tombstone
#: (spec 7.1: delete and restore).
_EVENT_TYPES_WITH_TOMBSTONE: Final[frozenset[DeviceEventType]] = frozenset(
    {DeviceEventType.DELETED, DeviceEventType.RESTORED}
)

#: The phrase each event type contributes to its shape-error messages.
_EVENT_SHAPE_PHRASES: Final[Mapping[DeviceEventType, str]] = MappingProxyType(
    {
        DeviceEventType.CREATED: "create",
        DeviceEventType.UPDATED: "update",
        DeviceEventType.RENAMED: "rename",
        DeviceEventType.MOVED: "move",
        DeviceEventType.DELETED: "delete",
        DeviceEventType.RESTORED: "restore",
    }
)


@dataclass(frozen=True, slots=True)
class DeviceSyncContext:
    """Credential-derived identity of one authenticated device sync unit of work.

    Composed by the API adapter from the opaque bearer token; no request
    field can select any of these identities.
    """

    workspace_id: UUID
    device_id: UUID
    user_id: UUID

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("device_id", self.device_id)
        reject_nil_uuid("user_id", self.user_id)


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    """Submitted hash/size/media identity evidence of one observed version.

    The SHA-256 is exactly 64 lowercase hexadecimal characters, the byte size
    is non-negative and the media type satisfies the canonical grammar. The
    digest is a private value and never renders outside a redacted ``repr``.
    """

    sha256: str
    size_bytes: int
    media_type: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(sha256=<redacted>)"

    def __post_init__(self) -> None:
        if _FINGERPRINT_SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("fingerprint sha256 must be 64 lowercase hexadecimal characters")
        if self.size_bytes < 0:
            raise ValueError("fingerprint size_bytes must be a non-negative byte size")
        CanonicalMediaType.parse(self.media_type)


@dataclass(frozen=True, slots=True)
class DeviceSyncEvent:
    """One immutable canonical sync event with operation-shaped operands.

    Field requirements follow spec 7.1 exactly: create, rename, move and
    restore carry the resulting locator; rename, move and delete carry the
    prior locator; delete and restore carry the exact tombstone ID; no other
    type carries a tombstone operand. Locators and fingerprints are private
    values and never render outside a redacted ``repr``.
    """

    event_id: UUID
    event_sequence: int
    event_type: DeviceEventType
    source_id: UUID
    origin_device_id: UUID | None
    base_version_id: UUID | None
    current_version_id: UUID | None
    base_fingerprint: SourceFingerprint | None
    current_fingerprint: SourceFingerprint | None
    prior_locator: NormalizedLocator | None
    resulting_locator: NormalizedLocator | None
    tombstone_id: UUID | None
    committed_at: datetime

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def __post_init__(self) -> None:
        reject_nil_uuid("event_id", self.event_id)
        reject_nil_uuid("source_id", self.source_id)
        if self.origin_device_id is not None:
            reject_nil_uuid("origin_device_id", self.origin_device_id)
        if self.base_version_id is not None:
            reject_nil_uuid("base_version_id", self.base_version_id)
        if self.current_version_id is not None:
            reject_nil_uuid("current_version_id", self.current_version_id)
        if self.tombstone_id is not None:
            reject_nil_uuid("tombstone_id", self.tombstone_id)
        if self.event_sequence < 0:
            raise ValueError("event_sequence must be a non-negative sequence number")
        phrase = _EVENT_SHAPE_PHRASES[self.event_type]
        if self.event_type in _EVENT_TYPES_WITH_RESULTING_LOCATOR:
            if self.resulting_locator is None:
                raise ValueError(f"{phrase} event shape invalid: resulting locator is required")
        elif self.resulting_locator is not None:
            raise ValueError(f"{phrase} event shape invalid: resulting locator is forbidden")
        if self.event_type in _EVENT_TYPES_WITH_PRIOR_LOCATOR:
            if self.prior_locator is None:
                raise ValueError(f"{phrase} event shape invalid: prior locator is required")
        elif self.prior_locator is not None:
            raise ValueError(f"{phrase} event shape invalid: prior locator is forbidden")
        if self.event_type in _EVENT_TYPES_WITH_TOMBSTONE:
            if self.tombstone_id is None:
                raise ValueError(f"{phrase} event shape invalid: tombstone operand is required")
        elif self.tombstone_id is not None:
            raise ValueError(f"{phrase} event shape invalid: tombstone operand is forbidden")
        object.__setattr__(
            self,
            "committed_at",
            normalize_utc_timestamp("committed_at", self.committed_at),
        )


@dataclass(frozen=True, slots=True)
class DeviceCursorReceipt:
    """Frozen cursor watermarks of one device (spec 6.1)."""

    acknowledged_sequence: int
    delivered_through_sequence: int

    def __post_init__(self) -> None:
        if self.acknowledged_sequence < 0 or self.delivered_through_sequence < 0:
            raise ValueError("cursor sequences must be non-negative")
        if self.delivered_through_sequence < self.acknowledged_sequence:
            raise ValueError("delivered watermark must not precede the acknowledged cursor")


@dataclass(frozen=True, slots=True)
class DeviceEventPage:
    """One bounded pull page of immutable events (spec 7.1)."""

    acknowledged_sequence: int
    page_checkpoint_sequence: int
    delivered_through_sequence: int
    events: tuple[DeviceSyncEvent, ...]
    has_more: bool

    def __post_init__(self) -> None:
        if self.acknowledged_sequence < 0 or self.delivered_through_sequence < 0:
            raise ValueError("cursor sequences must be non-negative")
        if self.delivered_through_sequence < self.acknowledged_sequence:
            raise ValueError("delivered watermark must not precede the acknowledged cursor")
        if self.page_checkpoint_sequence < self.delivered_through_sequence:
            raise ValueError("page checkpoint must not precede the delivered watermark")
        if len(self.events) > MAX_PULL_EVENTS:
            raise ValueError(f"event page must contain at most {MAX_PULL_EVENTS} events")


@dataclass(frozen=True, slots=True)
class DeviceContentDescriptor:
    """Verified content identity of one exact source version (spec 7.4).

    Carries only the expected digest, byte size and canonical media type —
    never an object key, presigned URL, receipt or provider detail.
    """

    source_id: UUID
    source_version_id: UUID
    content_digest: ContentDigest
    size_bytes: int
    media_type: CanonicalMediaType

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def expected_object(self) -> ExpectedObject:
        """Project the canonical verification request for these exact bytes."""

        return ExpectedObject(
            content_digest=self.content_digest,
            size_bytes=self.size_bytes,
            media_type=self.media_type,
        )

    def __post_init__(self) -> None:
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("source_version_id", self.source_version_id)
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative byte size")


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One locally observed manifest entry (spec 12.1).

    Carries the opaque plugin-local entry ID, optional client source/version
    evidence, the normalized locator, the settled-byte fingerprint and the
    local observation generation. Locator and fingerprint are private values
    and never render outside a redacted ``repr``.
    """

    local_entry_id: str
    known_source_id: UUID | None
    known_version_id: UUID | None
    normalized_locator: NormalizedLocator
    fingerprint: SourceFingerprint
    observation_generation: int

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def __post_init__(self) -> None:
        if not self.local_entry_id or len(self.local_entry_id) > _LOCAL_ENTRY_ID_MAXIMUM_LENGTH:
            raise ValueError(
                f"local_entry_id must be 1 to {_LOCAL_ENTRY_ID_MAXIMUM_LENGTH} characters long"
            )
        if self.known_source_id is not None:
            reject_nil_uuid("known_source_id", self.known_source_id)
        if self.known_version_id is not None:
            reject_nil_uuid("known_version_id", self.known_version_id)
        if self.observation_generation < 0:
            raise ValueError("observation_generation must be non-negative")


@dataclass(frozen=True, slots=True)
class ManifestEntryResolution:
    """One frozen manifest entry identity resolution (spec 6.4/12.2).

    Carries the submitted entry evidence the deterministic planner needs
    plus the canonical identity proven against the canonical locator
    history at the run checkpoint: the optional client source/version
    evidence, the settled-byte fingerprint, the fingerprint of the trusted
    local base when the client's known version is provable in the
    workspace, the closed policy decision for the submitted entry subject,
    and the proven canonical identity with its match kind. The planner
    consumes it together with the canonical source state at the same
    checkpoint; ``entry_ordinal`` is the entry's zero-based position in the
    run's ordered resolutions and becomes the materialized action's index.
    Fingerprints are private values and never render outside a redacted
    ``repr``.
    """

    local_entry_id: str
    entry_ordinal: int
    known_source_id: UUID | None
    known_version_id: UUID | None
    submitted_fingerprint: SourceFingerprint
    known_base_fingerprint: SourceFingerprint | None
    is_policy_allowed: bool
    match_kind: ManifestMatchKind
    resolved_source_id: UUID | None
    resolved_source_version_id: UUID | None
    resolved_source_locator_id: UUID | None
    resolved_source_tombstone_id: UUID | None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def __post_init__(self) -> None:
        if not self.local_entry_id or len(self.local_entry_id) > _LOCAL_ENTRY_ID_MAXIMUM_LENGTH:
            raise ValueError(
                f"local_entry_id must be 1 to {_LOCAL_ENTRY_ID_MAXIMUM_LENGTH} characters long"
            )
        if self.entry_ordinal < 0:
            raise ValueError("entry_ordinal must be a non-negative run entry order")
        if self.known_source_id is not None:
            reject_nil_uuid("known_source_id", self.known_source_id)
        if self.known_version_id is not None:
            reject_nil_uuid("known_version_id", self.known_version_id)
        if self.resolved_source_id is not None:
            reject_nil_uuid("resolved_source_id", self.resolved_source_id)
        if self.resolved_source_version_id is not None:
            reject_nil_uuid("resolved_source_version_id", self.resolved_source_version_id)
        if self.resolved_source_locator_id is not None:
            reject_nil_uuid("resolved_source_locator_id", self.resolved_source_locator_id)
        if self.resolved_source_tombstone_id is not None:
            reject_nil_uuid("resolved_source_tombstone_id", self.resolved_source_tombstone_id)
        if self.match_kind is ManifestMatchKind.UNPROVEN:
            if (
                self.resolved_source_id is not None
                or self.resolved_source_version_id is not None
                or self.resolved_source_locator_id is not None
                or self.resolved_source_tombstone_id is not None
            ):
                raise ValueError("unproven resolution carries no canonical identity")
        elif self.resolved_source_id is None or self.resolved_source_version_id is None:
            raise ValueError("proven resolution names its source and version")


@dataclass(frozen=True, slots=True)
class ManifestAction:
    """One frozen deterministic action of a planned manifest run (spec 6.5).

    A ``download`` action carries the checkpoint-active locator text the
    device must place its bytes at; every other kind carries none. The
    locator is a private value that travels on the operational action wire
    only and never renders outside a redacted ``repr``.
    """

    action_index: int
    action_kind: ManifestActionKind
    local_entry_id: str | None
    source_id: UUID | None
    source_version_id: UUID | None
    source_locator_id: UUID | None
    source_tombstone_id: UUID | None
    reason: ManifestActionReason | None
    checkpoint_locator: NormalizedLocator | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def __post_init__(self) -> None:
        if self.action_index < 0:
            raise ValueError("action_index must be a non-negative index")
        if self.source_id is not None:
            reject_nil_uuid("source_id", self.source_id)
        if self.source_version_id is not None:
            reject_nil_uuid("source_version_id", self.source_version_id)
        if self.source_locator_id is not None:
            reject_nil_uuid("source_locator_id", self.source_locator_id)
        if self.source_tombstone_id is not None:
            reject_nil_uuid("source_tombstone_id", self.source_tombstone_id)
        if self.action_kind is ManifestActionKind.DOWNLOAD:
            if self.checkpoint_locator is None:
                raise ValueError("download action shape invalid: checkpoint locator is required")
        elif self.checkpoint_locator is not None:
            raise ValueError(
                f"{self.action_kind.value} action shape invalid: checkpoint locator is forbidden"
            )


def compute_manifest_run_expiry(created_at: datetime) -> datetime:
    """Return the one-hour manifest-run expiry deadline for ``created_at``.

    The deadline normalizes to UTC and adds exactly
    :data:`MANIFEST_RUN_LIFETIME`; database-time enforcement is the store's
    own authority.
    """

    normalized = normalize_utc_timestamp("created_at", created_at)
    if normalized is None:
        # Unreachable for the non-optional parameter; keeps the path total.
        raise ValueError("created_at must be timezone-aware")
    return normalized + MANIFEST_RUN_LIFETIME


@dataclass(frozen=True, slots=True)
class StartManifestCommand:
    """Start or exactly resume one manifest run (spec 7.3)."""

    context: DeviceSyncContext
    client_observation_generation: int
    diagnostic_context: DiagnosticContext

    def __post_init__(self) -> None:
        if self.client_observation_generation < 0:
            raise ValueError("client_observation_generation must be non-negative")


@dataclass(frozen=True, slots=True)
class ManifestRunReceipt:
    """The frozen state of one manifest run (spec 6.2)."""

    manifest_run_id: UUID
    state: ManifestRunState
    base_acknowledged_sequence: int
    checkpoint_sequence: int
    policy_revision_number: int
    client_observation_generation: int
    next_page_number: int
    entry_count: int
    expires_at: datetime

    def __post_init__(self) -> None:
        reject_nil_uuid("manifest_run_id", self.manifest_run_id)
        if self.base_acknowledged_sequence < 0 or self.checkpoint_sequence < 0:
            raise ValueError("run sequences must be non-negative")
        if self.checkpoint_sequence < self.base_acknowledged_sequence:
            raise ValueError("checkpoint must not precede the base acknowledged cursor")
        if self.policy_revision_number < 1:
            raise ValueError("policy_revision_number must be a positive integer")
        if self.client_observation_generation < 0:
            raise ValueError("client_observation_generation must be non-negative")
        if self.next_page_number < 0:
            raise ValueError("next_page_number must be a non-negative page number")
        if not 0 <= self.entry_count <= MAX_MANIFEST_RUN_ENTRIES:
            raise ValueError(
                f"run entry_count must be 0 to at most {MAX_MANIFEST_RUN_ENTRIES} entries"
            )
        object.__setattr__(
            self,
            "expires_at",
            normalize_utc_timestamp("expires_at", self.expires_at),
        )


@dataclass(frozen=True, slots=True)
class AppendManifestPageCommand:
    """Put the exact next ordered page of one manifest run (spec 6.3/7.3)."""

    context: DeviceSyncContext
    manifest_run_id: UUID
    page_number: int
    entries: tuple[ManifestEntry, ...]
    page_digest: ContentDigest
    diagnostic_context: DiagnosticContext

    def __post_init__(self) -> None:
        reject_nil_uuid("manifest_run_id", self.manifest_run_id)
        if self.page_number < 0:
            raise ValueError("page_number must be a non-negative page number")
        if len(self.entries) > MAX_MANIFEST_PAGE_ENTRIES:
            raise ValueError(
                f"manifest page must carry at most {MAX_MANIFEST_PAGE_ENTRIES} entries"
            )


@dataclass(frozen=True, slots=True)
class ManifestPageReceipt:
    """The frozen acceptance of one manifest page (spec 6.3)."""

    manifest_run_id: UUID
    page_number: int
    accepted_entry_count: int
    next_page_number: int

    def __post_init__(self) -> None:
        reject_nil_uuid("manifest_run_id", self.manifest_run_id)
        if self.page_number < 0 or self.next_page_number < 0:
            raise ValueError("page numbers must be non-negative")
        if not 0 <= self.accepted_entry_count <= MAX_MANIFEST_PAGE_ENTRIES:
            raise ValueError(
                f"accepted_entry_count must be 0 to at most {MAX_MANIFEST_PAGE_ENTRIES} entries"
            )


@dataclass(frozen=True, slots=True)
class FinalizeManifestCommand:
    """Finalize one run with its total count and final digest (spec 7.3)."""

    context: DeviceSyncContext
    manifest_run_id: UUID
    total_entry_count: int
    final_digest: ContentDigest
    diagnostic_context: DiagnosticContext

    def __post_init__(self) -> None:
        reject_nil_uuid("manifest_run_id", self.manifest_run_id)
        if not 0 <= self.total_entry_count <= MAX_MANIFEST_RUN_ENTRIES:
            raise ValueError(
                f"total_entry_count must be 0 to at most {MAX_MANIFEST_RUN_ENTRIES} entries"
            )


@dataclass(frozen=True, slots=True)
class ManifestActionsQuery:
    """Read one deterministic action page after ``after_action_index`` (spec 7.3)."""

    context: DeviceSyncContext
    manifest_run_id: UUID
    after_action_index: int
    limit: int
    diagnostic_context: DiagnosticContext

    def __post_init__(self) -> None:
        reject_nil_uuid("manifest_run_id", self.manifest_run_id)
        if self.after_action_index < 0:
            raise ValueError("after_action_index must be a non-negative index")
        if not 1 <= self.limit <= MAX_MANIFEST_PAGE_ENTRIES:
            raise ValueError(f"action page limit must be 1 to {MAX_MANIFEST_PAGE_ENTRIES} actions")


@dataclass(frozen=True, slots=True)
class ManifestActionPage:
    """One stable ordered page of frozen actions (spec 6.5)."""

    manifest_run_id: UUID
    actions: tuple[ManifestAction, ...]
    has_more: bool

    def __post_init__(self) -> None:
        reject_nil_uuid("manifest_run_id", self.manifest_run_id)
        if len(self.actions) > MAX_MANIFEST_PAGE_ENTRIES:
            raise ValueError(f"action page must carry at most {MAX_MANIFEST_PAGE_ENTRIES} actions")
        for previous, current in zip(self.actions, self.actions[1:], strict=False):
            if current.action_index <= previous.action_index:
                raise ValueError("action page must carry strictly increasing action indices")


@dataclass(frozen=True, slots=True)
class CompleteManifestCommand:
    """Complete the exact planned run and advance the cursor (spec 7.3)."""

    context: DeviceSyncContext
    manifest_run_id: UUID
    final_digest: ContentDigest
    diagnostic_context: DiagnosticContext

    def __post_init__(self) -> None:
        reject_nil_uuid("manifest_run_id", self.manifest_run_id)


__all__ = [
    "MANIFEST_RUN_LIFETIME",
    "MAX_MANIFEST_PAGE_ENTRIES",
    "MAX_MANIFEST_RUN_ENTRIES",
    "MAX_PULL_EVENTS",
    "AppendManifestPageCommand",
    "CompleteManifestCommand",
    "DeviceContentDescriptor",
    "DeviceCursorReceipt",
    "DeviceEventPage",
    "DeviceEventType",
    "DeviceSyncContext",
    "DeviceSyncEvent",
    "FinalizeManifestCommand",
    "ManifestAction",
    "ManifestActionKind",
    "ManifestActionPage",
    "ManifestActionReason",
    "ManifestActionsQuery",
    "ManifestEntry",
    "ManifestEntryResolution",
    "ManifestMatchKind",
    "ManifestPageReceipt",
    "ManifestRunReceipt",
    "ManifestRunState",
    "NormalizedLocator",
    "SourceFingerprint",
    "StartManifestCommand",
    "compute_manifest_run_expiry",
]
