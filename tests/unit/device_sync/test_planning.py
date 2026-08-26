"""Deterministic manifest identity proof and action planner matrices.

These tests pin the pure planning rules of spec 12.2/12.3 exactly: the
identity-recovery proof priority (current active locator binds even when
bytes differ; a unique historical locator binds only with the exact current
fingerprint; an open tombstone binds only through its retained locator and
retained fingerprint; hash-only, multiple-candidate and closed evidence
prove nothing), and the deterministic action planner over one entry
resolution and the canonical source state at the run checkpoint. Locator
and digest sentinels never appear in a rendered planning value, and the
internal locator-evidence digest is deterministic over the canonical JSON
of the evidence tuples.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from personal_os.device_sync.contracts import (
    ManifestActionKind,
    ManifestActionReason,
    ManifestEntry,
    ManifestMatchKind,
    NormalizedLocator,
    SourceFingerprint,
)
from personal_os.device_sync.planning import (
    CanonicalManifestSource,
    ManifestIdentityEvidence,
    ManifestIdentityResolution,
    compute_locator_evidence_digest,
    plan_manifest_action,
    resolve_manifest_identity,
)

_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000001")
_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000002")
_SECOND_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000003")
_VERSION_ID = UUID("018f47a0-7b00-7000-8000-000000000004")
_SECOND_VERSION_ID = UUID("018f47a0-7b00-7000-8000-000000000005")
_LOCATOR_ID = UUID("018f47a0-7b00-7000-8000-000000000006")
_TOMBSTONE_ID = UUID("018f47a0-7b00-7000-8000-000000000007")
#: The checkpoint-active locator row of a remotely renamed source: the
#: placement operand every file action must carry.
_ACTIVE_LOCATOR_ID = UUID("018f47a0-7b00-7000-8000-000000000010")

_LOCATOR = NormalizedLocator("notes/alpha.md")
_OTHER_LOCATOR = NormalizedLocator("notes/beta.md")

_FINGERPRINT_SHA256 = hashlib.sha256(b"planning fingerprint evidence").hexdigest()
_OTHER_FINGERPRINT_SHA256 = hashlib.sha256(b"divergent fingerprint evidence").hexdigest()
_THIRD_FINGERPRINT_SHA256 = hashlib.sha256(b"advanced fingerprint evidence").hexdigest()
FINGERPRINT = SourceFingerprint(
    sha256=_FINGERPRINT_SHA256, size_bytes=64, media_type="text/markdown"
)
OTHER_FINGERPRINT = SourceFingerprint(
    sha256=_OTHER_FINGERPRINT_SHA256, size_bytes=48, media_type="text/markdown"
)
THIRD_FINGERPRINT = SourceFingerprint(
    sha256=_THIRD_FINGERPRINT_SHA256, size_bytes=32, media_type="text/markdown"
)

#: The entry with no canonical candidate at all: hash-only evidence.
UNKNOWN_ENTRY = ManifestEntry(
    local_entry_id="entry-orphan",
    known_source_id=None,
    known_version_id=None,
    normalized_locator=_LOCATOR,
    fingerprint=FINGERPRINT,
    observation_generation=2,
)


def canonical_source(
    *,
    source_id: UUID = _SOURCE_ID,
    version_id: UUID = _VERSION_ID,
    fingerprint: SourceFingerprint = FINGERPRINT,
    locator: NormalizedLocator = _LOCATOR,
    active_locator_id: UUID | None = _LOCATOR_ID,
    tombstone_id: UUID | None = None,
    is_policy_allowed: bool = True,
) -> CanonicalManifestSource:
    """One canonical source state at the run checkpoint."""

    return CanonicalManifestSource(
        source_id=source_id,
        current_version_id=version_id,
        current_fingerprint=fingerprint,
        locator=locator,
        active_locator_id=active_locator_id,
        tombstone_id=tombstone_id,
        is_policy_allowed=is_policy_allowed,
    )


def evidence(
    *,
    entry: ManifestEntry = UNKNOWN_ENTRY,
    current: tuple[CanonicalManifestSource, ...] = (),
    historical: tuple[CanonicalManifestSource, ...] = (),
    tombstones: tuple[CanonicalManifestSource, ...] = (),
) -> ManifestIdentityEvidence:
    """One identity-evidence bundle for one local entry."""

    return ManifestIdentityEvidence(
        local_entry=entry,
        current_locator_candidates=current,
        historical_locator_candidates=historical,
        open_tombstone_candidates=tombstones,
    )


def unproven() -> ManifestIdentityResolution:
    """The closed ambiguous identity outcome."""

    return ManifestIdentityResolution(
        source_id=None,
        source_version_id=None,
        match_kind=ManifestMatchKind.UNPROVEN,
        reason=ManifestActionReason.IDENTITY_AMBIGUOUS,
    )


# --- identity proof priority and ambiguity (spec 12.2) -------------------------


def test_hash_only_identity_never_binds() -> None:
    result = resolve_manifest_identity(
        ManifestIdentityEvidence(
            local_entry=UNKNOWN_ENTRY,
            current_locator_candidates=(),
            historical_locator_candidates=(),
            open_tombstone_candidates=(),
        )
    )
    assert result.match_kind is ManifestMatchKind.UNPROVEN
    assert result.reason is ManifestActionReason.IDENTITY_AMBIGUOUS


def test_single_current_locator_candidate_binds_even_with_divergent_bytes() -> None:
    result = resolve_manifest_identity(
        evidence(
            current=(
                canonical_source(
                    source_id=_SOURCE_ID,
                    version_id=_VERSION_ID,
                    fingerprint=OTHER_FINGERPRINT,
                ),
            )
        )
    )
    assert result == ManifestIdentityResolution(
        source_id=_SOURCE_ID,
        source_version_id=_VERSION_ID,
        match_kind=ManifestMatchKind.CURRENT_LOCATOR,
        reason=None,
    )


def test_multiple_current_locator_candidates_are_ambiguous() -> None:
    result = resolve_manifest_identity(
        evidence(
            current=(
                canonical_source(source_id=_SOURCE_ID),
                canonical_source(source_id=_SECOND_SOURCE_ID),
            )
        )
    )
    assert result == unproven()


def test_current_locator_outranks_historical_and_tombstone_evidence() -> None:
    result = resolve_manifest_identity(
        evidence(
            current=(canonical_source(source_id=_SOURCE_ID, version_id=_VERSION_ID),),
            historical=(canonical_source(source_id=_SECOND_SOURCE_ID),),
            tombstones=(
                canonical_source(
                    source_id=UUID("018f47a0-7b00-7000-8000-000000000008"),
                    tombstone_id=_TOMBSTONE_ID,
                ),
            ),
        )
    )
    assert result.match_kind is ManifestMatchKind.CURRENT_LOCATOR
    assert result.source_id == _SOURCE_ID
    assert result.source_version_id == _VERSION_ID


def test_unique_historical_locator_with_exact_current_fingerprint_binds() -> None:
    result = resolve_manifest_identity(
        evidence(historical=(canonical_source(source_id=_SOURCE_ID),))
    )
    assert result == ManifestIdentityResolution(
        source_id=_SOURCE_ID,
        source_version_id=_VERSION_ID,
        match_kind=ManifestMatchKind.HISTORICAL_LOCATOR_FINGERPRINT,
        reason=None,
    )


def test_historical_locator_without_exact_fingerprint_never_binds() -> None:
    result = resolve_manifest_identity(
        evidence(historical=(canonical_source(fingerprint=OTHER_FINGERPRINT),))
    )
    assert result == unproven()


def test_multiple_historical_candidates_are_ambiguous() -> None:
    result = resolve_manifest_identity(
        evidence(
            historical=(
                canonical_source(source_id=_SOURCE_ID),
                canonical_source(source_id=_SECOND_SOURCE_ID),
            )
        )
    )
    assert result == unproven()


def test_open_tombstone_with_retained_fingerprint_binds_the_deleted_source() -> None:
    result = resolve_manifest_identity(
        evidence(tombstones=(canonical_source(tombstone_id=_TOMBSTONE_ID, version_id=_VERSION_ID),))
    )
    assert result == ManifestIdentityResolution(
        source_id=_SOURCE_ID,
        source_version_id=_VERSION_ID,
        match_kind=ManifestMatchKind.OPEN_TOMBSTONE_FINGERPRINT,
        reason=None,
    )


def test_open_tombstone_with_divergent_fingerprint_never_binds() -> None:
    result = resolve_manifest_identity(
        evidence(
            tombstones=(
                canonical_source(tombstone_id=_TOMBSTONE_ID, fingerprint=OTHER_FINGERPRINT),
            )
        )
    )
    assert result == unproven()


def test_multiple_tombstone_candidates_are_ambiguous() -> None:
    result = resolve_manifest_identity(
        evidence(
            tombstones=(
                canonical_source(tombstone_id=_TOMBSTONE_ID),
                canonical_source(
                    source_id=_SECOND_SOURCE_ID,
                    tombstone_id=UUID("018f47a0-7b00-7000-8000-000000000009"),
                ),
            )
        )
    )
    assert result == unproven()


# --- deterministic action planning (spec 12.3) ---------------------------------


def planned_entry_resolution(
    *,
    entry_id: str = "entry-1",
    ordinal: int = 0,
    known_source_id: UUID | None = None,
    known_version_id: UUID | None = None,
    submitted_fingerprint: SourceFingerprint = FINGERPRINT,
    known_base_fingerprint: SourceFingerprint | None = None,
    is_policy_allowed: bool = True,
    match_kind: ManifestMatchKind = ManifestMatchKind.CURRENT_LOCATOR,
    resolved_source_id: UUID | None = _SOURCE_ID,
    resolved_source_version_id: UUID | None = _VERSION_ID,
    resolved_source_locator_id: UUID | None = _LOCATOR_ID,
    resolved_source_tombstone_id: UUID | None = None,
):
    """One entry resolution shaped for the planner under test."""

    from personal_os.device_sync.contracts import ManifestEntryResolution

    return ManifestEntryResolution(
        local_entry_id=entry_id,
        entry_ordinal=ordinal,
        known_source_id=known_source_id,
        known_version_id=known_version_id,
        submitted_fingerprint=submitted_fingerprint,
        known_base_fingerprint=known_base_fingerprint,
        is_policy_allowed=is_policy_allowed,
        match_kind=match_kind,
        resolved_source_id=resolved_source_id,
        resolved_source_version_id=resolved_source_version_id,
        resolved_source_locator_id=resolved_source_locator_id,
        resolved_source_tombstone_id=resolved_source_tombstone_id,
    )


def unproven_entry_resolution(
    *,
    entry_id: str = "entry-orphan",
    known_source_id: UUID | None = None,
    known_version_id: UUID | None = None,
    is_policy_allowed: bool = True,
    submitted_fingerprint: SourceFingerprint = FINGERPRINT,
):
    """One unproven entry resolution carrying no canonical identity."""

    return planned_entry_resolution(
        entry_id=entry_id,
        known_source_id=known_source_id,
        known_version_id=known_version_id,
        is_policy_allowed=is_policy_allowed,
        submitted_fingerprint=submitted_fingerprint,
        match_kind=ManifestMatchKind.UNPROVEN,
        resolved_source_id=None,
        resolved_source_version_id=None,
        resolved_source_locator_id=None,
        resolved_source_tombstone_id=None,
    )


def test_matching_current_bytes_plan_no_change() -> None:
    resolution = planned_entry_resolution()
    action = plan_manifest_action(resolution, canonical_source())
    assert action.action_kind is ManifestActionKind.NO_CHANGE
    assert action.local_entry_id == "entry-1"
    assert action.source_id == _SOURCE_ID
    assert action.source_version_id == _VERSION_ID
    assert action.source_locator_id == _LOCATOR_ID
    assert action.source_tombstone_id is None
    assert action.reason is None


def test_remote_rename_no_change_carries_the_checkpoint_active_locator() -> None:
    """The rule-2 rename journey: the entry matched the source's closed
    historical locator, but the placement operand must name the locator
    open at the checkpoint (where the plugin places the file)."""

    resolution = planned_entry_resolution(
        match_kind=ManifestMatchKind.HISTORICAL_LOCATOR_FINGERPRINT,
        # The historical locator the entry proved through; its id is the
        # resolved operand the action must NOT carry.
        resolved_source_locator_id=_LOCATOR_ID,
    )
    canonical = canonical_source(locator=_OTHER_LOCATOR, active_locator_id=_ACTIVE_LOCATOR_ID)
    action = plan_manifest_action(resolution, canonical)
    assert action.action_kind is ManifestActionKind.NO_CHANGE
    assert action.source_locator_id == _ACTIVE_LOCATOR_ID
    assert action.source_locator_id != resolution.resolved_source_locator_id


def test_upload_and_download_carry_the_checkpoint_active_locator() -> None:
    """Every file-placement action names the canonical active locator row,
    never the entry's resolved (possibly historical) locator id."""

    upload_resolution = planned_entry_resolution(
        known_source_id=_SOURCE_ID,
        known_version_id=_VERSION_ID,
        submitted_fingerprint=OTHER_FINGERPRINT,
        known_base_fingerprint=FINGERPRINT,
    )
    upload = plan_manifest_action(
        upload_resolution,
        canonical_source(active_locator_id=_ACTIVE_LOCATOR_ID),
    )
    assert upload.action_kind is ManifestActionKind.UPLOAD
    assert upload.source_locator_id == _ACTIVE_LOCATOR_ID

    download_resolution = planned_entry_resolution(
        known_source_id=_SOURCE_ID,
        known_version_id=_SECOND_VERSION_ID,
        submitted_fingerprint=FINGERPRINT,
        known_base_fingerprint=FINGERPRINT,
    )
    download_canonical = canonical_source(
        version_id=_VERSION_ID,
        fingerprint=OTHER_FINGERPRINT,
        active_locator_id=_ACTIVE_LOCATOR_ID,
    )
    download = plan_manifest_action(download_resolution, download_canonical)
    assert download.action_kind is ManifestActionKind.DOWNLOAD
    assert download.source_locator_id == _ACTIVE_LOCATOR_ID
    assert download.checkpoint_locator == download_canonical.locator


def test_unowned_new_locator_plans_upload() -> None:
    action = plan_manifest_action(unproven_entry_resolution(), None)
    assert action.action_kind is ManifestActionKind.UPLOAD
    assert action.local_entry_id == "entry-orphan"
    assert action.source_id is None
    assert action.source_version_id is None
    assert action.reason is None


def test_trusted_current_base_with_changed_bytes_plans_upload() -> None:
    resolution = planned_entry_resolution(
        known_source_id=_SOURCE_ID,
        known_version_id=_VERSION_ID,
        submitted_fingerprint=OTHER_FINGERPRINT,
        known_base_fingerprint=FINGERPRINT,
    )
    action = plan_manifest_action(resolution, canonical_source())
    assert action.action_kind is ManifestActionKind.UPLOAD
    assert action.local_entry_id == "entry-1"
    assert action.source_id == _SOURCE_ID
    assert action.source_version_id == _VERSION_ID
    assert action.reason is None


def test_stale_trusted_base_with_equal_local_bytes_plans_download() -> None:
    """The per-entry catch-up download keeps its manifest entry's echo
    (spec 6.5: the column is nullable for canonical-only downloads only)
    and names the checkpoint-active locator the device places bytes at."""

    resolution = planned_entry_resolution(
        known_source_id=_SOURCE_ID,
        known_version_id=_SECOND_VERSION_ID,
        submitted_fingerprint=FINGERPRINT,
        known_base_fingerprint=FINGERPRINT,
    )
    canonical = canonical_source(
        version_id=_VERSION_ID,
        fingerprint=OTHER_FINGERPRINT,
        locator=_OTHER_LOCATOR,
        active_locator_id=_ACTIVE_LOCATOR_ID,
    )
    action = plan_manifest_action(resolution, canonical)
    assert action.action_kind is ManifestActionKind.DOWNLOAD
    assert action.local_entry_id == "entry-1"
    assert action.source_id == _SOURCE_ID
    assert action.source_version_id == _VERSION_ID
    assert action.source_locator_id == _ACTIVE_LOCATOR_ID
    assert action.checkpoint_locator == _OTHER_LOCATOR
    assert action.reason is None


def test_both_sides_advanced_plan_local_diverged_conflict() -> None:
    resolution = planned_entry_resolution(
        known_source_id=_SOURCE_ID,
        known_version_id=_SECOND_VERSION_ID,
        submitted_fingerprint=OTHER_FINGERPRINT,
        known_base_fingerprint=FINGERPRINT,
    )
    canonical = canonical_source(fingerprint=THIRD_FINGERPRINT)
    action = plan_manifest_action(resolution, canonical)
    assert action.action_kind is ManifestActionKind.CONFLICT
    assert action.local_entry_id == "entry-1"
    assert action.source_id is None
    assert action.reason is ManifestActionReason.LOCAL_DIVERGED


def test_untrusted_divergent_bytes_plan_local_diverged_conflict() -> None:
    resolution = planned_entry_resolution(submitted_fingerprint=OTHER_FINGERPRINT)
    canonical = canonical_source()
    action = plan_manifest_action(resolution, canonical)
    assert action.action_kind is ManifestActionKind.CONFLICT
    assert action.reason is ManifestActionReason.LOCAL_DIVERGED


def test_unproven_entry_with_known_evidence_fails_closed_as_identity_conflict() -> None:
    action = plan_manifest_action(
        unproven_entry_resolution(known_source_id=_SECOND_SOURCE_ID), None
    )
    assert action.action_kind is ManifestActionKind.CONFLICT
    assert action.local_entry_id == "entry-orphan"
    assert action.reason is ManifestActionReason.IDENTITY_AMBIGUOUS


def test_proven_identity_without_canonical_state_fails_closed() -> None:
    action = plan_manifest_action(planned_entry_resolution(), None)
    assert action.action_kind is ManifestActionKind.CONFLICT
    assert action.reason is ManifestActionReason.IDENTITY_AMBIGUOUS


def test_policy_forbidden_canonical_source_plans_excluded() -> None:
    action = plan_manifest_action(
        planned_entry_resolution(), canonical_source(is_policy_allowed=False)
    )
    assert action.action_kind is ManifestActionKind.EXCLUDED
    assert action.local_entry_id == "entry-1"
    assert action.reason is ManifestActionReason.POLICY_EXCLUDED


def test_policy_forbidden_new_locator_plans_excluded() -> None:
    action = plan_manifest_action(unproven_entry_resolution(is_policy_allowed=False), None)
    assert action.action_kind is ManifestActionKind.EXCLUDED
    assert action.local_entry_id == "entry-orphan"
    assert action.reason is ManifestActionReason.POLICY_EXCLUDED


def test_canonically_deleted_source_plans_apply_tombstone() -> None:
    resolution = planned_entry_resolution(
        match_kind=ManifestMatchKind.OPEN_TOMBSTONE_FINGERPRINT,
        resolved_source_tombstone_id=_TOMBSTONE_ID,
    )
    canonical = canonical_source(tombstone_id=_TOMBSTONE_ID)
    action = plan_manifest_action(resolution, canonical)
    assert action.action_kind is ManifestActionKind.APPLY_TOMBSTONE
    assert action.local_entry_id == "entry-1"
    assert action.source_id == _SOURCE_ID
    assert action.source_tombstone_id == _TOMBSTONE_ID
    assert action.reason is None


def test_planner_matrix_is_total_over_every_action_kind() -> None:
    covered: set[ManifestActionKind] = set()
    scenarios = (
        plan_manifest_action(planned_entry_resolution(), canonical_source()),
        plan_manifest_action(unproven_entry_resolution(), None),
        plan_manifest_action(
            planned_entry_resolution(
                known_source_id=_SOURCE_ID,
                known_version_id=_VERSION_ID,
                submitted_fingerprint=OTHER_FINGERPRINT,
                known_base_fingerprint=FINGERPRINT,
            ),
            canonical_source(),
        ),
        plan_manifest_action(
            planned_entry_resolution(
                known_source_id=_SOURCE_ID,
                known_version_id=_SECOND_VERSION_ID,
                known_base_fingerprint=FINGERPRINT,
            ),
            canonical_source(version_id=_VERSION_ID, fingerprint=OTHER_FINGERPRINT),
        ),
        plan_manifest_action(
            planned_entry_resolution(
                match_kind=ManifestMatchKind.OPEN_TOMBSTONE_FINGERPRINT,
                resolved_source_tombstone_id=_TOMBSTONE_ID,
            ),
            canonical_source(tombstone_id=_TOMBSTONE_ID),
        ),
        plan_manifest_action(
            planned_entry_resolution(submitted_fingerprint=OTHER_FINGERPRINT),
            canonical_source(),
        ),
        plan_manifest_action(unproven_entry_resolution(known_source_id=_SECOND_SOURCE_ID), None),
        plan_manifest_action(planned_entry_resolution(), canonical_source(is_policy_allowed=False)),
        plan_manifest_action(unproven_entry_resolution(is_policy_allowed=False), None),
    )
    for action in scenarios:
        covered.add(action.action_kind)
    assert covered == set(ManifestActionKind)


def test_action_indices_follow_the_entry_ordinal() -> None:
    action = plan_manifest_action(planned_entry_resolution(ordinal=41), canonical_source())
    assert action.action_index == 41


# --- locator-evidence digest ----------------------------------------------------


def test_locator_evidence_digest_is_deterministic_over_equal_evidence() -> None:
    first = compute_locator_evidence_digest(_LOCATOR, evidence(current=(canonical_source(),)))
    second = compute_locator_evidence_digest(_LOCATOR, evidence(current=(canonical_source(),)))
    assert first == second
    assert len(first) == 64
    assert first == first.lower()


def test_locator_evidence_digest_binds_the_locator_and_the_candidates() -> None:
    base = compute_locator_evidence_digest(_LOCATOR, evidence())
    other_locator = compute_locator_evidence_digest(_OTHER_LOCATOR, evidence())
    with_candidate = compute_locator_evidence_digest(
        _LOCATOR, evidence(current=(canonical_source(),))
    )
    other_candidate = compute_locator_evidence_digest(
        _LOCATOR, evidence(current=(canonical_source(source_id=_SECOND_SOURCE_ID),))
    )
    assert len({base, other_locator, with_candidate, other_candidate}) == 4


# --- privacy of the pure planning values ----------------------------------------


def test_planning_values_never_render_locators_or_digests_in_repr() -> None:
    source = canonical_source()
    bundle = evidence(current=(source,))
    resolution = planned_entry_resolution()
    rendered = f"{source!r} {bundle!r} {resolution!r}"
    assert "notes/alpha.md" not in rendered
    assert _FINGERPRINT_SHA256 not in rendered
    assert "<redacted>" in rendered
