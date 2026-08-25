"""Deterministic manifest identity proof and action planning (spec 12.2/12.3).

Pure framework-neutral planning over one local manifest entry: the
identity-recovery proof evaluates the approved evidence priority exactly —
an exact current active locator binds source identity even when the local
bytes differ; a unique historical locator binds only together with the
exact current canonical fingerprint; an open tombstone binds only through
its retained locator and retained version fingerprint; hash-only,
multiple-candidate and closed evidence prove nothing. The action planner
maps one entry resolution plus the canonical source state at the run
checkpoint onto exactly one closed action kind with its operand shape, so
the same inputs always plan the same action. The internal locator-evidence
digest commits to the locator and the evidence tuples actually used for
resolution through the repository's RFC 8785 canonical-JSON grammar, so
persisted planning state never carries raw locator bytes. The module
imports no FastAPI, SQLAlchemy, database driver, R2 SDK or Obsidian type;
locators and fingerprints never render outside a redacted ``repr``.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Final
from uuid import UUID

from personal_os.device_sync.contracts import (
    ManifestAction,
    ManifestActionKind,
    ManifestActionReason,
    ManifestEntry,
    ManifestEntryResolution,
    ManifestMatchKind,
    SourceFingerprint,
)
from personal_os.exclusion_policy.canonical_json import canonicalize_json_value
from personal_os.source_locators import NormalizedLocator

__all__ = [
    "CanonicalManifestSource",
    "ManifestIdentityEvidence",
    "ManifestIdentityResolution",
    "compute_locator_evidence_digest",
    "plan_manifest_action",
    "resolve_manifest_identity",
]


@dataclass(frozen=True, slots=True)
class CanonicalManifestSource:
    """One canonical source's checkpoint state offered as identity evidence.

    Carries the exact version identity and fingerprint current at the run
    checkpoint, the source's normalized locator at that checkpoint, the
    open tombstone when the source is deleted at the checkpoint, and the
    closed policy decision for the source subject under the run's bound
    policy revision. Locator and fingerprint are private values and never
    render outside a redacted ``repr``.
    """

    source_id: UUID
    current_version_id: UUID
    current_fingerprint: SourceFingerprint
    locator: NormalizedLocator
    tombstone_id: UUID | None
    is_policy_allowed: bool

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


@dataclass(frozen=True, slots=True)
class ManifestIdentityEvidence:
    """The checkpoint-scoped locator-evidence bundle of one local entry.

    The adapter builds each candidate tuple from the canonical lifecycle
    rows at the run checkpoint inside the credential workspace: an entry
    that supplies a source ID must have had its evidence validated (or
    emptied) against workspace membership first, because invalid or
    cross-workspace evidence fails closed and is never retried through
    locator fallback. Candidates are private values and never render
    outside a redacted ``repr``.
    """

    local_entry: ManifestEntry
    current_locator_candidates: tuple[CanonicalManifestSource, ...]
    historical_locator_candidates: tuple[CanonicalManifestSource, ...]
    open_tombstone_candidates: tuple[CanonicalManifestSource, ...]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


@dataclass(frozen=True, slots=True)
class ManifestIdentityResolution:
    """The closed identity outcome of one manifest entry."""

    source_id: UUID | None
    source_version_id: UUID | None
    match_kind: ManifestMatchKind
    reason: ManifestActionReason | None


#: The closed ambiguous outcome: nothing was proven, and only a conflict
#: action may carry the entry forward.
_UNPROVEN: Final[ManifestIdentityResolution] = ManifestIdentityResolution(
    source_id=None,
    source_version_id=None,
    match_kind=ManifestMatchKind.UNPROVEN,
    reason=ManifestActionReason.IDENTITY_AMBIGUOUS,
)


def resolve_manifest_identity(evidence: ManifestIdentityEvidence) -> ManifestIdentityResolution:
    """Prove one entry's canonical source identity under the proof priority.

    Rule 1 binds through a single current active locator even when the
    local bytes differ (the divergence is planner business, not identity
    business). Rule 2 binds a unique historical locator only together with
    the exact current canonical fingerprint, because a closed locator is
    no longer an active authority. Rule 3 binds an open tombstone through
    its retained locator plus the exact retained version fingerprint.
    Hash-only evidence, more than one candidate in the matched class, or a
    fingerprint mismatch proves nothing: the closed ambiguous outcome.
    """

    current = evidence.current_locator_candidates
    if len(current) > 1:
        return _UNPROVEN
    if len(current) == 1:
        candidate = current[0]
        return ManifestIdentityResolution(
            source_id=candidate.source_id,
            source_version_id=candidate.current_version_id,
            match_kind=ManifestMatchKind.CURRENT_LOCATOR,
            reason=None,
        )
    historical = evidence.historical_locator_candidates
    if len(historical) > 1:
        return _UNPROVEN
    if len(historical) == 1:
        candidate = historical[0]
        if candidate.current_fingerprint != evidence.local_entry.fingerprint:
            return _UNPROVEN
        return ManifestIdentityResolution(
            source_id=candidate.source_id,
            source_version_id=candidate.current_version_id,
            match_kind=ManifestMatchKind.HISTORICAL_LOCATOR_FINGERPRINT,
            reason=None,
        )
    tombstones = evidence.open_tombstone_candidates
    if len(tombstones) == 1:
        candidate = tombstones[0]
        if candidate.current_fingerprint == evidence.local_entry.fingerprint:
            return ManifestIdentityResolution(
                source_id=candidate.source_id,
                source_version_id=candidate.current_version_id,
                match_kind=ManifestMatchKind.OPEN_TOMBSTONE_FINGERPRINT,
                reason=None,
            )
    return _UNPROVEN


def plan_manifest_action(
    resolution: ManifestEntryResolution,
    canonical: CanonicalManifestSource | None,
) -> ManifestAction:
    """Plan exactly one deterministic action for one entry resolution.

    An unproven resolution fails closed: known evidence the checkpoint
    could not prove is the identity-ambiguous conflict and never an upload
    or download, while genuinely unowned locator evidence plans an upload
    the bound policy allows or an exclusion it forbids. A proven resolution
    plans against the canonical state at the checkpoint: a canonically
    deleted source applies its open tombstone; a policy-forbidden source
    is excluded with its local bytes preserved; matching current bytes are
    no-change; a trusted base that is still current with changed bytes
    uploads; local bytes still equal to a stale trusted base download the
    canonical advance (the only action without a local entry); every other
    divergence is the local-diverged conflict — untrusted bytes are never
    automatically uploaded or downloaded.
    """

    if resolution.match_kind is ManifestMatchKind.UNPROVEN:
        if canonical is not None:
            return _conflict(resolution, ManifestActionReason.IDENTITY_AMBIGUOUS)
        if resolution.known_source_id is not None or resolution.known_version_id is not None:
            return _conflict(resolution, ManifestActionReason.IDENTITY_AMBIGUOUS)
        if not resolution.is_policy_allowed:
            return _excluded(resolution)
        return ManifestAction(
            action_index=resolution.entry_ordinal,
            action_kind=ManifestActionKind.UPLOAD,
            local_entry_id=resolution.local_entry_id,
            source_id=None,
            source_version_id=None,
            source_locator_id=None,
            source_tombstone_id=None,
            reason=None,
        )
    if canonical is None:
        # A proven identity whose canonical checkpoint state vanished is
        # inconsistent lifecycle evidence, never an implicit mutation.
        return _conflict(resolution, ManifestActionReason.IDENTITY_AMBIGUOUS)
    if canonical.tombstone_id is not None:
        return ManifestAction(
            action_index=resolution.entry_ordinal,
            action_kind=ManifestActionKind.APPLY_TOMBSTONE,
            local_entry_id=resolution.local_entry_id,
            source_id=resolution.resolved_source_id,
            source_version_id=None,
            source_locator_id=resolution.resolved_source_locator_id,
            source_tombstone_id=(
                resolution.resolved_source_tombstone_id
                if resolution.resolved_source_tombstone_id is not None
                else canonical.tombstone_id
            ),
            reason=None,
        )
    if not canonical.is_policy_allowed:
        return _excluded(resolution)
    bytes_match = resolution.submitted_fingerprint == canonical.current_fingerprint
    trusted_base = (
        resolution.known_source_id == canonical.source_id
        and resolution.known_version_id is not None
        and resolution.known_base_fingerprint is not None
    )
    if bytes_match:
        return ManifestAction(
            action_index=resolution.entry_ordinal,
            action_kind=ManifestActionKind.NO_CHANGE,
            local_entry_id=resolution.local_entry_id,
            source_id=resolution.resolved_source_id,
            source_version_id=canonical.current_version_id,
            source_locator_id=resolution.resolved_source_locator_id,
            source_tombstone_id=None,
            reason=None,
        )
    if trusted_base and resolution.known_version_id == canonical.current_version_id:
        # The trusted local base is still the canonical current version and
        # the device changed bytes on top of it: an upload based on it.
        return ManifestAction(
            action_index=resolution.entry_ordinal,
            action_kind=ManifestActionKind.UPLOAD,
            local_entry_id=resolution.local_entry_id,
            source_id=canonical.source_id,
            source_version_id=canonical.current_version_id,
            source_locator_id=resolution.resolved_source_locator_id,
            source_tombstone_id=None,
            reason=None,
        )
    if trusted_base and resolution.submitted_fingerprint == resolution.known_base_fingerprint:
        # Local bytes still equal a stale trusted base while the canonical
        # source advanced: the canonical catch-up download.
        return ManifestAction(
            action_index=resolution.entry_ordinal,
            action_kind=ManifestActionKind.DOWNLOAD,
            local_entry_id=None,
            source_id=canonical.source_id,
            source_version_id=canonical.current_version_id,
            source_locator_id=resolution.resolved_source_locator_id,
            source_tombstone_id=None,
            reason=None,
        )
    return _conflict(resolution, ManifestActionReason.LOCAL_DIVERGED)


def _conflict(
    resolution: ManifestEntryResolution, reason: ManifestActionReason
) -> ManifestAction:
    return ManifestAction(
        action_index=resolution.entry_ordinal,
        action_kind=ManifestActionKind.CONFLICT,
        local_entry_id=resolution.local_entry_id,
        source_id=None,
        source_version_id=None,
        source_locator_id=None,
        source_tombstone_id=None,
        reason=reason,
    )


def _excluded(resolution: ManifestEntryResolution) -> ManifestAction:
    return ManifestAction(
        action_index=resolution.entry_ordinal,
        action_kind=ManifestActionKind.EXCLUDED,
        local_entry_id=resolution.local_entry_id,
        source_id=None,
        source_version_id=None,
        source_locator_id=None,
        source_tombstone_id=None,
        reason=ManifestActionReason.POLICY_EXCLUDED,
    )


def compute_locator_evidence_digest(
    locator: NormalizedLocator, evidence: ManifestIdentityEvidence
) -> str:
    """Commit to the locator and the evidence tuples used for resolution.

    The digest is the SHA-256 over the RFC 8785 canonical JSON of the
    closed payload ``{"evidence": ..., "locator": ...}`` where every tuple
    names its class and canonical identities only — candidate source IDs,
    and the tombstone identity for open-tombstone evidence. The caller
    must supply each candidate tuple in a deterministic order (the store
    orders by source identity), so equal evidence always digests equally.
    """

    payload = {
        "locator": locator.value,
        "evidence": (
            tuple(
                {"class": "current_locator", "source_id": candidate.source_id.hex}
                for candidate in evidence.current_locator_candidates
            ),
            tuple(
                {"class": "historical_locator", "source_id": candidate.source_id.hex}
                for candidate in evidence.historical_locator_candidates
            ),
            tuple(
                {
                    "class": "open_tombstone",
                    "source_id": candidate.source_id.hex,
                    "source_tombstone_id": (
                        candidate.tombstone_id.hex if candidate.tombstone_id else ""
                    ),
                }
                for candidate in evidence.open_tombstone_candidates
            ),
        ),
    }
    return sha256(canonicalize_json_value(payload)).hexdigest()
