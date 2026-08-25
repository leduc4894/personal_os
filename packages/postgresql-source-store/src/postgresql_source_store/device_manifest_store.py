"""Durable manifest run, page, action and completion state over PostgreSQL.

:class:`PostgresqlDeviceManifestStore` implements the
:class:`~personal_os.device_sync.ports.DeviceManifestStore` port over the
``20260826_01`` manifest schema. ``start_manifest`` binds one unfinished run
per workspace/device to the acknowledged cursor base, one frozen statement
checkpoint and the workspace's active policy revision: the conflict-tolerant
insert resumes the existing unfinished run (a different observation
generation is the closed state rejection) and a run past its one-hour
database deadline is expired — retaining its prior evidence — before a fresh
run may start. ``append_manifest_page`` accepts only the exact next ordered
page: an earlier page replays exactly on equal digest and count, reuse with
different evidence fails the run with the closed replay mismatch, the
cumulative 100,000-entry cap holds, and every entry's identity is resolved
against the canonical locator/tombstone history at the run checkpoint inside
the credential workspace — persisting only canonical IDs plus the internal
locator-evidence digest, never raw locator bytes.
``finalize_manifest`` verifies the canonical-JSON final digest over the
run's ordered pages, then materializes the deterministic action plan through
the pure planner: per-entry resolutions against the canonical source state
at the checkpoint under the run's bound policy revision, plus canonical-only
downloads for allowed active sources absent from the manifest (a deleted
canonical source absent locally needs no file action). The first successful
``read_manifest_actions`` moves ``planned`` to ``applying`` and later reads
are state-preserving replays; every action read and completion rechecks the
active policy revision and invalidates a stale run with the closed
policy-advanced reason persisted on the run row. ``complete_manifest`` is
the completion fence: exactly the transaction that changes the exact
``applying`` run to ``completed`` may advance the device cursor to the run
checkpoint without a delivered watermark — foreign, expired, failed and
already-completed runs grant no new advance, and a lost completion response
replays to the same cursor receipt.

Driver failures cross the boundary only through the closed device sync
registry (the shared event-store retry and mapping policy): lock contention
retries with the shared cancellable jitter and connection-class
unavailability maps to the retryable
``device_sync_dependency_unavailable``. SQLSTATE, SQL text, parameters,
driver messages, locators and digests never enter a typed error, statement
or log line; the closed failure reasons surface through the typed errors and
the ``safe_error_code`` persisted on failed runs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from hashlib import sha256
from typing import Any, Final, cast
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.selectable import LateralFromClause

from personal_os.device_sync.contracts import (
    MAX_MANIFEST_RUN_ENTRIES,
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceCursorReceipt,
    FinalizeManifestCommand,
    ManifestAction,
    ManifestActionKind,
    ManifestActionPage,
    ManifestActionReason,
    ManifestActionsQuery,
    ManifestEntry,
    ManifestEntryResolution,
    ManifestMatchKind,
    ManifestPageReceipt,
    ManifestRunReceipt,
    ManifestRunState,
    NormalizedLocator,
    SourceFingerprint,
    StartManifestCommand,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.planning import (
    CanonicalManifestSource,
    ManifestIdentityEvidence,
    compute_locator_evidence_digest,
    plan_manifest_action,
    resolve_manifest_identity,
)
from personal_os.exclusion_policy.canonical_json import canonicalize_json_value
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
)
from personal_os.exclusion_policy.evaluation import evaluate_policy
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import SourceType
from postgresql_source_store.device_event_store import (
    DeviceSyncDatabaseRetryPolicy,
    device_cursor_select_statement,
    device_event_checkpoint_statement,
    manifest_action_page_statement,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.policy_previews import hydrate_policy_revision_rules
from postgresql_source_store.tables import (
    content_objects,
    device_cursors,
    manifest_actions,
    manifest_entry_resolutions,
    manifest_pages,
    manifest_runs,
    policy_rules,
    source_locators,
    source_policies,
    source_tombstones,
    source_versions,
    sources,
    sync_events,
    workspace_policy_state,
)

__all__ = [
    "MANIFEST_UNFINISHED_STATES",
    "PostgresqlDeviceManifestStore",
    "compute_manifest_final_digest",
    "device_cursor_completion_advance_statement",
    "device_cursor_completion_bootstrap_statement",
    "manifest_canonical_only_downloads_statement",
    "manifest_canonical_source_state_statement",
    "manifest_locator_candidates_statement",
    "manifest_page_select_statement",
    "manifest_run_applying_transition_statement",
    "manifest_run_completion_transition_statement",
    "manifest_run_expire_statement",
    "manifest_run_fail_statement",
    "manifest_run_planned_statement",
    "manifest_run_select_statement",
    "manifest_tombstone_candidates_statement",
    "manifest_unfinished_run_select_statement",
    "workspace_active_policy_revision_statement",
]

#: The manifest run states that still own the per-device unfinished slot
#: (exactly the partial unique index vocabulary of the ``20260826_01``
#: migration).
MANIFEST_UNFINISHED_STATES: Final[frozenset[str]] = frozenset(
    {"collecting", "planned", "applying"}
)

#: One page record of the canonical final-digest grammar: the zero-based
#: page number, its accepted entry count and its page digest hex.
type ManifestPageRecord = tuple[int, int, str]


def _unfinished_state_predicate() -> sa.ColumnElement[bool]:
    """The unfinished-state membership over the closed literal vocabulary."""

    return manifest_runs.c.state.in_(
        [sa.literal_column(f"'{state}'") for state in sorted(MANIFEST_UNFINISHED_STATES)]
    )


def compute_manifest_final_digest(pages: Sequence[ManifestPageRecord]) -> str:
    """Digest the run's ordered pages with the canonical-JSON grammar.

    The final digest is the SHA-256 over the RFC 8785 canonical JSON of
    ``{"pages": [{"digest": ..., "entries": ..., "page": ...}, ...],
    "version": 1}`` with the pages sorted by page number, so the client,
    the finalize verification and any later replay agree byte for byte.
    """

    ordered = sorted(pages, key=lambda record: record[0])
    payload = {
        "version": 1,
        "pages": tuple(
            {"page": page_number, "entries": entry_count, "digest": page_digest}
            for page_number, entry_count, page_digest in ordered
        ),
    }
    return sha256(canonicalize_json_value(payload)).hexdigest()


# --- statement builders -----------------------------------------------------------


def manifest_run_select_statement(
    workspace_id: UUID,
    device_id: UUID,
    manifest_run_id: UUID,
    *,
    for_update: bool = False,
) -> sa.Select[tuple[Any, ...]]:
    """Build the credential-scoped manifest run read, optionally locked.

    The ownership triple (workspace, device, run) is the whole predicate: a
    run outside the credential boundary is indistinguishable from a missing
    one.
    """

    statement = sa.select(
        manifest_runs.c.manifest_run_id,
        manifest_runs.c.workspace_id,
        manifest_runs.c.device_id,
        manifest_runs.c.state,
        manifest_runs.c.base_acknowledged_sequence,
        manifest_runs.c.checkpoint_sequence,
        manifest_runs.c.policy_revision_number,
        manifest_runs.c.client_observation_generation,
        manifest_runs.c.next_page_number,
        manifest_runs.c.entry_count,
        manifest_runs.c.final_digest,
        manifest_runs.c.safe_error_code,
        manifest_runs.c.expires_at,
    ).where(
        manifest_runs.c.workspace_id == workspace_id,
        manifest_runs.c.device_id == device_id,
        manifest_runs.c.manifest_run_id == manifest_run_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return statement


def manifest_unfinished_run_select_statement(
    workspace_id: UUID,
    device_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the unfinished-run resume read for one workspace/device.

    Exactly the partial unique index vocabulary selects the device's one
    unfinished run, newest first.
    """

    return (
        sa.select(
            manifest_runs.c.manifest_run_id,
            manifest_runs.c.workspace_id,
            manifest_runs.c.device_id,
            manifest_runs.c.state,
            manifest_runs.c.base_acknowledged_sequence,
            manifest_runs.c.checkpoint_sequence,
            manifest_runs.c.policy_revision_number,
            manifest_runs.c.client_observation_generation,
            manifest_runs.c.next_page_number,
            manifest_runs.c.entry_count,
            manifest_runs.c.final_digest,
            manifest_runs.c.safe_error_code,
            manifest_runs.c.expires_at,
        )
        .where(
            manifest_runs.c.workspace_id == workspace_id,
            manifest_runs.c.device_id == device_id,
            _unfinished_state_predicate(),
        )
        .order_by(manifest_runs.c.created_at.desc())
        .limit(1)
    )


def manifest_run_fail_statement(manifest_run_id: UUID, safe_error_code: str) -> sa.Update:
    """Fail the unfinished run with its closed safe reason token.

    Only the state and the closed reason change: the run keeps whatever
    finalized evidence it had, and no completion time ever appears on a
    failed run.
    """

    return (
        sa.update(manifest_runs)
        .values(
            state=sa.literal_column("'failed'"),
            safe_error_code=sa.bindparam("safe_error_code", safe_error_code),
        )
        .where(
            manifest_runs.c.manifest_run_id == manifest_run_id,
            _unfinished_state_predicate(),
        )
    )


def manifest_run_expire_statement(manifest_run_id: UUID) -> sa.Update:
    """Expire the unfinished run past its one-hour database deadline.

    Expiry is a deadline, not an error: the state changes alone so the row
    retains its prior finalized evidence and never carries a completion
    time or error code.
    """

    return (
        sa.update(manifest_runs)
        .values(state=sa.literal_column("'expired'"))
        .where(
            manifest_runs.c.manifest_run_id == manifest_run_id,
            _unfinished_state_predicate(),
        )
    )


def manifest_run_planned_statement(manifest_run_id: UUID, final_digest: str) -> sa.Update:
    """Finalize the exact collecting run with its verified final digest."""

    return (
        sa.update(manifest_runs)
        .values(
            state=sa.literal_column("'planned'"),
            final_digest=sa.bindparam("final_digest", final_digest),
            planned_at=sa.func.current_timestamp(),
        )
        .where(
            manifest_runs.c.manifest_run_id == manifest_run_id,
            manifest_runs.c.state == sa.literal_column("'collecting'"),
        )
    )


def manifest_run_applying_transition_statement(manifest_run_id: UUID) -> sa.Update:
    """Move the exact planned run to applying on its first action read."""

    return (
        sa.update(manifest_runs)
        .values(state=sa.literal_column("'applying'"))
        .where(
            manifest_runs.c.manifest_run_id == manifest_run_id,
            manifest_runs.c.state == sa.literal_column("'planned'"),
        )
    )


def manifest_run_completion_transition_statement(manifest_run_id: UUID) -> sa.Update:
    """Complete the exact applying run inside the completion fence.

    The guarded prior state is the fence itself: only a transaction that
    actually changed ``applying`` to ``completed`` may follow with the
    cursor advance, and every other completion path observes the row
    unchanged.
    """

    return (
        sa.update(manifest_runs)
        .values(
            state=sa.literal_column("'completed'"),
            completed_at=sa.func.current_timestamp(),
        )
        .where(
            manifest_runs.c.manifest_run_id == manifest_run_id,
            manifest_runs.c.state == sa.literal_column("'applying'"),
        )
    )


def manifest_page_select_statement(
    manifest_run_id: UUID, page_number: int
) -> sa.Select[tuple[Any, ...]]:
    """Build the exact page replay lookup keyed by run and page."""

    return sa.select(
        manifest_pages.c.page_number,
        manifest_pages.c.entry_count,
        manifest_pages.c.page_digest,
    ).where(
        manifest_pages.c.manifest_run_id == manifest_run_id,
        manifest_pages.c.page_number == sa.bindparam("page_number", page_number),
    )


def device_cursor_completion_bootstrap_statement(
    *,
    device_cursor_id: UUID,
    workspace_id: UUID,
    device_id: UUID,
    checkpoint_sequence: int,
) -> sa.Insert:
    """Build the conflict-tolerant first cursor row at the checkpoint.

    A device that never pulled still completes its run: the bootstrap seeds
    both watermarks at the run checkpoint and a lost race is a no-op for
    the guarded advance that follows.
    """

    return (
        postgresql_insert(device_cursors)
        .values(
            device_cursor_id=device_cursor_id,
            workspace_id=workspace_id,
            device_id=device_id,
            acknowledged_sequence=sa.bindparam(
                "acknowledged_sequence", checkpoint_sequence
            ),
            delivered_through_sequence=sa.bindparam(
                "delivered_through_sequence", checkpoint_sequence
            ),
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "device_id"])
    )


def device_cursor_completion_advance_statement(
    workspace_id: UUID,
    device_id: UUID,
    *,
    checkpoint_sequence: int,
) -> sa.Update:
    """Build the fenced completion advance to the run checkpoint.

    The advance raises the acknowledged watermark only forward and only to
    the checkpoint, and lifts the delivered watermark with ``GREATEST`` so
    the database delivery-order invariant holds even though the completion
    transaction authorizes the acknowledgement without a prior delivered
    watermark.
    """

    checkpoint: sa.BindParameter[int] = sa.bindparam(
        "checkpoint_sequence", checkpoint_sequence
    )
    return (
        sa.update(device_cursors)
        .values(
            acknowledged_sequence=checkpoint,
            delivered_through_sequence=sa.func.greatest(
                device_cursors.c.delivered_through_sequence, checkpoint
            ),
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            device_cursors.c.workspace_id == workspace_id,
            device_cursors.c.device_id == device_id,
            device_cursors.c.acknowledged_sequence < checkpoint,
        )
    )


def workspace_active_policy_revision_statement(
    workspace_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace active policy pointer read."""

    return sa.select(
        workspace_policy_state.c.active_revision_number,
        workspace_policy_state.c.active_policy_revision_id,
    ).where(workspace_policy_state.c.workspace_id == workspace_id)


def bound_policy_revision_statement(
    workspace_id: UUID, revision_number: int
) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace-bound published revision lookup."""

    return sa.select(
        source_policies.c.policy_revision_id,
        source_policies.c.revision_number,
    ).where(
        source_policies.c.workspace_id == workspace_id,
        source_policies.c.revision_number
        == sa.bindparam("revision_number", revision_number),
    )


def policy_rules_select_statement(
    policy_revision_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the immutable published rules of one revision, index-ordered."""

    return (
        sa.select(
            policy_rules.c.rule_id,
            policy_rules.c.rule_kind,
            policy_rules.c.source_id_operand,
            policy_rules.c.text_operand,
            policy_rules.c.size_bytes_operand,
        )
        .where(policy_rules.c.policy_revision_id == policy_revision_id)
        .order_by(policy_rules.c.rule_id.asc())
    )


def manifest_page_records_statement(
    manifest_run_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the run's accepted pages in page order for the final digest."""

    return (
        sa.select(
            manifest_pages.c.page_number,
            manifest_pages.c.entry_count,
            manifest_pages.c.page_digest,
        )
        .where(manifest_pages.c.manifest_run_id == manifest_run_id)
        .order_by(manifest_pages.c.page_number.asc())
    )


def manifest_resolution_rows_statement(
    manifest_run_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the run's persisted entry resolutions in capture order."""

    return (
        sa.select(
            manifest_entry_resolutions.c.page_number,
            manifest_entry_resolutions.c.entry_index,
            manifest_entry_resolutions.c.local_entry_id,
            manifest_entry_resolutions.c.known_source_id,
            manifest_entry_resolutions.c.known_version_id,
            manifest_entry_resolutions.c.submitted_sha256,
            manifest_entry_resolutions.c.submitted_size_bytes,
            manifest_entry_resolutions.c.submitted_media_type,
            manifest_entry_resolutions.c.resolved_source_id,
            manifest_entry_resolutions.c.resolved_source_version_id,
            manifest_entry_resolutions.c.resolved_source_locator_id,
            manifest_entry_resolutions.c.resolved_source_tombstone_id,
            manifest_entry_resolutions.c.match_kind,
        )
        .where(manifest_entry_resolutions.c.manifest_run_id == manifest_run_id)
        .order_by(
            manifest_entry_resolutions.c.page_number.asc(),
            manifest_entry_resolutions.c.entry_index.asc(),
        )
    )


def known_base_fingerprints_statement(
    workspace_id: UUID, version_ids: Sequence[UUID]
) -> sa.Select[tuple[Any, ...]]:
    """Build the in-workspace trusted-base fingerprint lookup by version.

    Only versions whose source belongs to the credential workspace join
    their content evidence, so cross-workspace version evidence can never
    become a trusted local base.
    """

    return (
        sa.select(
            source_versions.c.source_version_id,
            source_versions.c.source_id,
            content_objects.c.content_hash,
            content_objects.c.byte_size,
            content_objects.c.media_type,
        )
        .select_from(source_versions)
        .join(
            sources,
            sa.and_(
                sources.c.workspace_id == workspace_id,
                sources.c.source_id == source_versions.c.source_id,
            ),
        )
        .join(
            content_objects,
            content_objects.c.content_object_id == source_versions.c.content_object_id,
        )
        .where(
            source_versions.c.workspace_id == workspace_id,
            source_versions.c.source_version_id.in_(
                sa.bindparam(
                    "version_ids", list(version_ids), type_=sa.Uuid(), expanding=True
                )
            ),
        )
    )


def _checkpoint_version_lateral(checkpoint_sequence: int) -> LateralFromClause:
    """The per-source version committed by the latest event at the checkpoint."""

    return (
        sa.select(sync_events.c.committed_version_id)
        .where(
            sync_events.c.workspace_id == sources.c.workspace_id,
            sync_events.c.source_id == sources.c.source_id,
            sync_events.c.event_sequence
            <= sa.bindparam("checkpoint_sequence", checkpoint_sequence, type_=sa.BigInteger()),
        )
        .order_by(sync_events.c.event_sequence.desc())
        .limit(1)
        .lateral("checkpoint_version")
    )


def _active_locator_lateral(checkpoint_sequence: int) -> LateralFromClause:
    """The per-source locator open at the checkpoint."""

    return (
        sa.select(
            source_locators.c.source_locator_id,
            source_locators.c.normalized_locator,
        )
        .where(
            source_locators.c.workspace_id == sources.c.workspace_id,
            source_locators.c.source_id == sources.c.source_id,
            source_locators.c.opened_sequence
            <= sa.bindparam("checkpoint_sequence", checkpoint_sequence, type_=sa.BigInteger()),
            sa.or_(
                source_locators.c.closed_event_id.is_(None),
                source_locators.c.closed_sequence
                > sa.bindparam("checkpoint_sequence", checkpoint_sequence, type_=sa.BigInteger()),
            ),
        )
        .order_by(source_locators.c.opened_sequence.desc())
        .limit(1)
        .lateral("active_locator")
    )


def _open_tombstone_lateral(checkpoint_sequence: int) -> LateralFromClause:
    """The per-source tombstone open at the checkpoint, if any."""

    delete_event = sync_events.alias("tombstone_delete_event")
    restore_event = sync_events.alias("tombstone_restore_event")
    return (
        sa.select(source_tombstones.c.source_tombstone_id)
        .select_from(source_tombstones)
        .join(
            delete_event,
            sa.and_(
                delete_event.c.workspace_id == source_tombstones.c.workspace_id,
                delete_event.c.event_id == source_tombstones.c.delete_event_id,
            ),
        )
        .outerjoin(
            restore_event,
            sa.and_(
                restore_event.c.workspace_id == source_tombstones.c.workspace_id,
                restore_event.c.event_id == source_tombstones.c.restore_event_id,
            ),
        )
        .where(
            source_tombstones.c.workspace_id == sources.c.workspace_id,
            source_tombstones.c.source_id == sources.c.source_id,
            delete_event.c.event_sequence
            <= sa.bindparam("checkpoint_sequence", checkpoint_sequence, type_=sa.BigInteger()),
            sa.or_(
                source_tombstones.c.restore_event_id.is_(None),
                restore_event.c.event_sequence
                > sa.bindparam("checkpoint_sequence", checkpoint_sequence, type_=sa.BigInteger()),
            ),
        )
        .order_by(delete_event.c.event_sequence.desc())
        .limit(1)
        .lateral("open_tombstone")
    )


def manifest_canonical_source_state_statement(
    workspace_id: UUID,
    checkpoint_sequence: int,
    source_ids: Sequence[UUID],
) -> sa.Select[tuple[Any, ...]]:
    """Build the canonical source state at the checkpoint for known sources.

    Every operand is derived at the frozen checkpoint: the version
    committed by the latest event at or below it, that version's content
    fingerprint, the locator the source holds open at it, and the open
    tombstone if the source is deleted at it.
    """

    checkpoint_version = _checkpoint_version_lateral(checkpoint_sequence)
    active_locator = _active_locator_lateral(checkpoint_sequence)
    open_tombstone = _open_tombstone_lateral(checkpoint_sequence)
    statement = (
        sa.select(
            sources.c.source_id,
            sources.c.source_type,
            source_versions.c.source_version_id,
            content_objects.c.content_hash,
            content_objects.c.byte_size,
            content_objects.c.media_type,
            active_locator.c.source_locator_id.label("active_locator_id"),
            active_locator.c.normalized_locator.label("active_locator"),
            open_tombstone.c.source_tombstone_id.label("open_tombstone_id"),
        )
        .select_from(sources)
        .outerjoin(checkpoint_version, sa.true())
        .outerjoin(
            source_versions,
            sa.and_(
                source_versions.c.workspace_id == sources.c.workspace_id,
                source_versions.c.source_id == sources.c.source_id,
                source_versions.c.source_version_id
                == checkpoint_version.c.committed_version_id,
            ),
        )
        .outerjoin(
            content_objects,
            content_objects.c.content_object_id == source_versions.c.content_object_id,
        )
        .outerjoin(active_locator, sa.true())
        .outerjoin(open_tombstone, sa.true())
        .where(sources.c.workspace_id == workspace_id)
        .order_by(sources.c.source_id.asc())
    )
    if source_ids:
        statement = statement.where(
            sources.c.source_id.in_(
                sa.bindparam(
                    "source_ids", list(source_ids), type_=sa.Uuid(), expanding=True
                )
            )
        )
    return statement


def manifest_locator_candidates_statement(
    workspace_id: UUID,
    checkpoint_sequence: int,
    locators: Sequence[str],
) -> sa.Select[tuple[Any, ...]]:
    """Build the locator history matching the page's normalized locators.

    Rows carry the open/close evidence at the checkpoint so the caller
    classifies each row as a current or a historical candidate; the
    workspace scope keeps foreign locators out of the evidence entirely.
    """

    return (
        sa.select(
            source_locators.c.source_locator_id,
            source_locators.c.source_id,
            source_locators.c.normalized_locator,
            source_locators.c.closed_event_id,
            source_locators.c.closed_sequence,
        )
        .where(
            source_locators.c.workspace_id == workspace_id,
            source_locators.c.opened_sequence
            <= sa.bindparam("checkpoint_sequence", checkpoint_sequence, type_=sa.BigInteger()),
            source_locators.c.normalized_locator.in_(
                sa.bindparam("locator_texts", list(locators), expanding=True)
            ),
        )
        .order_by(
            source_locators.c.normalized_locator.asc(),
            source_locators.c.source_id.asc(),
        )
    )


def manifest_tombstone_candidates_statement(
    workspace_id: UUID,
    checkpoint_sequence: int,
    locators: Sequence[str],
) -> sa.Select[tuple[Any, ...]]:
    """Build the open tombstones whose retained locator matches the page.

    The retained version's fingerprint joins through its content object so
    rule-3 proof compares the exact retained bytes.
    """

    delete_event = sync_events.alias("candidate_delete_event")
    restore_event = sync_events.alias("candidate_restore_event")
    retained_version = source_versions.alias("retained_version")
    retained_object = content_objects.alias("retained_object")
    return (
        sa.select(
            source_tombstones.c.source_tombstone_id,
            source_tombstones.c.source_id,
            source_tombstones.c.retained_locator,
            source_tombstones.c.retained_version_id,
            retained_object.c.content_hash.label("retained_sha256"),
            retained_object.c.byte_size.label("retained_size_bytes"),
            retained_object.c.media_type.label("retained_media_type"),
        )
        .select_from(source_tombstones)
        .join(
            delete_event,
            sa.and_(
                delete_event.c.workspace_id == source_tombstones.c.workspace_id,
                delete_event.c.event_id == source_tombstones.c.delete_event_id,
            ),
        )
        .outerjoin(
            restore_event,
            sa.and_(
                restore_event.c.workspace_id == source_tombstones.c.workspace_id,
                restore_event.c.event_id == source_tombstones.c.restore_event_id,
            ),
        )
        .join(
            retained_version,
            sa.and_(
                retained_version.c.workspace_id == source_tombstones.c.workspace_id,
                retained_version.c.source_version_id
                == source_tombstones.c.retained_version_id,
            ),
        )
        .join(
            retained_object,
            retained_object.c.content_object_id == retained_version.c.content_object_id,
        )
        .where(
            source_tombstones.c.workspace_id == workspace_id,
            delete_event.c.event_sequence
            <= sa.bindparam("checkpoint_sequence", checkpoint_sequence, type_=sa.BigInteger()),
            sa.or_(
                source_tombstones.c.restore_event_id.is_(None),
                restore_event.c.event_sequence
                > sa.bindparam("checkpoint_sequence", checkpoint_sequence, type_=sa.BigInteger()),
            ),
            source_tombstones.c.retained_locator.in_(
                sa.bindparam("locator_texts", list(locators), expanding=True)
            ),
        )
        .order_by(
            source_tombstones.c.retained_locator.asc(),
            source_tombstones.c.source_id.asc(),
        )
    )


def manifest_canonical_only_downloads_statement(
    workspace_id: UUID,
    checkpoint_sequence: int,
    resolved_source_ids: Sequence[UUID],
) -> sa.Select[tuple[Any, ...]]:
    """Build the active sources at the checkpoint absent from the manifest.

    A source qualifies only when it holds a locator open at the checkpoint
    and carries no tombstone open there (a deleted canonical source absent
    locally needs no file action), carries its checkpoint version and
    content evidence for the policy subject, and is excluded when the run
    already proved some entry resolves to it.
    """

    checkpoint_version = _checkpoint_version_lateral(checkpoint_sequence)
    active_locator = _active_locator_lateral(checkpoint_sequence)
    open_tombstone = _open_tombstone_lateral(checkpoint_sequence)
    statement = (
        sa.select(
            sources.c.source_id,
            sources.c.source_type,
            source_versions.c.source_version_id,
            content_objects.c.media_type,
            content_objects.c.byte_size,
            active_locator.c.source_locator_id.label("active_locator_id"),
            active_locator.c.normalized_locator.label("active_locator"),
        )
        .select_from(sources)
        .outerjoin(checkpoint_version, sa.true())
        .outerjoin(
            source_versions,
            sa.and_(
                source_versions.c.workspace_id == sources.c.workspace_id,
                source_versions.c.source_id == sources.c.source_id,
                source_versions.c.source_version_id
                == checkpoint_version.c.committed_version_id,
            ),
        )
        .outerjoin(
            content_objects,
            content_objects.c.content_object_id == source_versions.c.content_object_id,
        )
        .join(active_locator, sa.true())
        .outerjoin(open_tombstone, sa.true())
        .where(
            sources.c.workspace_id == workspace_id,
            open_tombstone.c.source_tombstone_id.is_(None),
        )
        .order_by(active_locator.c.normalized_locator.asc(), sources.c.source_id.asc())
    )
    if resolved_source_ids:
        statement = statement.where(
            sources.c.source_id.not_in(
                sa.bindparam(
                    "resolved_source_ids",
                    list(resolved_source_ids),
                    type_=sa.Uuid(),
                    expanding=True,
                )
            )
        )
    return statement


# --- store ---------------------------------------------------------------------------


def _state_of(raw_state: Any) -> ManifestRunState:
    try:
        return ManifestRunState(str(raw_state))
    except ValueError:
        raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID) from None


def _fingerprint(sha256_hex: Any, size_bytes: Any, media_type: Any) -> SourceFingerprint | None:
    if sha256_hex is None or size_bytes is None or media_type is None:
        return None
    try:
        return SourceFingerprint(
            sha256=str(sha256_hex),
            size_bytes=int(size_bytes),
            media_type=str(media_type),
        )
    except ValueError:
        return None


class PostgresqlDeviceManifestStore:
    """Manifest run, page, action and completion state over the schema.

    The store takes the composition-owned :class:`AsyncEngine`, the shared
    device sync database retry policy and the UUIDv7 allocator seam, and
    opens no connection at construction. Every method runs one
    ``READ COMMITTED`` transaction behind the pinned ``SET LOCAL`` bounds
    and is scoped entirely by the credential-derived
    :class:`~personal_os.device_sync.contracts.DeviceSyncContext`.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        retry: DeviceSyncDatabaseRetryPolicy | None = None,
        identity_generator: Callable[[], UUID] | None = None,
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else DeviceSyncDatabaseRetryPolicy()
        self._identity_generator = identity_generator if identity_generator is not None else uuid7

    # -- start ---------------------------------------------------------------

    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt:
        """Start or exactly resume the device's one unfinished manifest run."""

        return await self._retry.run(lambda _attempt: self._start_once(command))

    async def _start_once(self, command: StartManifestCommand) -> ManifestRunReceipt:
        context = command.context
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            unfinished = (
                await connection.execute(
                    manifest_unfinished_run_select_statement(
                        context.workspace_id, context.device_id
                    )
                )
            ).one_or_none()
            database_now = await self._database_now(connection)
            if unfinished is not None and unfinished.expires_at <= database_now:
                await connection.execute(
                    manifest_run_expire_statement(unfinished.manifest_run_id)
                )
                unfinished = None
            if unfinished is not None:
                return self._resume_or_reject(unfinished, command)
            policy_revision_number = await self._read_active_policy_revision_number(
                connection, context.workspace_id
            )
            acknowledged = await self._read_acknowledged_sequence(connection, context)
            checkpoint_value = (
                await connection.execute(
                    device_event_checkpoint_statement(context.workspace_id)
                )
            ).one_or_none()
            checkpoint = (
                acknowledged if checkpoint_value is None else int(checkpoint_value[0])
            )
            run_id = self._identity_generator()
            await connection.execute(
                postgresql_insert(manifest_runs)
                .values(
                    manifest_run_id=run_id,
                    workspace_id=context.workspace_id,
                    device_id=context.device_id,
                    base_acknowledged_sequence=acknowledged,
                    checkpoint_sequence=max(checkpoint, acknowledged),
                    policy_revision_number=policy_revision_number,
                    client_observation_generation=command.client_observation_generation,
                    state="collecting",
                )
                .on_conflict_do_nothing(
                    index_elements=["workspace_id", "device_id"],
                    index_where=sa.text(
                        "state in ('collecting', 'planned', 'applying')"
                    ),
                )
            )
            created = (
                await connection.execute(
                    manifest_run_select_statement(
                        context.workspace_id, context.device_id, run_id
                    )
                )
            ).one_or_none()
            if created is not None:
                return self._run_receipt(created)
            # A concurrent start won the unfinished slot: resume it.
            winner = (
                await connection.execute(
                    manifest_unfinished_run_select_statement(
                        context.workspace_id, context.device_id
                    )
                )
            ).one_or_none()
            if winner is None:  # pragma: no cover - only a lost race lands here
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
            return self._resume_or_reject(winner, command)

    def _resume_or_reject(
        self, row: RowMapping, command: StartManifestCommand
    ) -> ManifestRunReceipt:
        if int(row.client_observation_generation) != command.client_observation_generation:
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
        return self._run_receipt(row)

    def _run_receipt(self, row: RowMapping) -> ManifestRunReceipt:
        return ManifestRunReceipt(
            manifest_run_id=row.manifest_run_id,
            state=_state_of(row.state),
            base_acknowledged_sequence=int(row.base_acknowledged_sequence),
            checkpoint_sequence=int(row.checkpoint_sequence),
            policy_revision_number=int(row.policy_revision_number),
            client_observation_generation=int(row.client_observation_generation),
            next_page_number=int(row.next_page_number),
            entry_count=int(row.entry_count),
            expires_at=row.expires_at,
        )

    async def _database_now(self, connection: AsyncConnection) -> Any:
        return (await connection.execute(sa.select(sa.func.now()))).scalar_one()

    async def _read_active_policy_revision_number(
        self, connection: AsyncConnection, workspace_id: UUID
    ) -> int:
        row = (
            await connection.execute(
                workspace_active_policy_revision_statement(workspace_id)
            )
        ).one_or_none()
        if (
            row is None
            or row.active_policy_revision_id is None
            or int(row.active_revision_number) < 1
        ):
            # A manifest run binds one active published revision; a
            # workspace without one can neither start nor keep a run.
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED)
        return int(row.active_revision_number)

    async def _read_acknowledged_sequence(
        self, connection: AsyncConnection, context: Any
    ) -> int:
        row = (
            await connection.execute(
                device_cursor_select_statement(context.workspace_id, context.device_id)
            )
        ).one_or_none()
        return 0 if row is None else int(row.acknowledged_sequence)

    # -- append ---------------------------------------------------------------

    async def append_manifest_page(
        self, command: AppendManifestPageCommand
    ) -> ManifestPageReceipt:
        """Accept the exact next ordered page with checkpoint identity proof."""

        return await self._retry.run(lambda _attempt: self._append_once(command))

    async def _append_once(self, command: AppendManifestPageCommand) -> ManifestPageReceipt:
        context = command.context
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            run = await self._lock_run(connection, context, command.manifest_run_id)
            state = str(run.state)
            if state == "expired":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_EXPIRED)
            if state in MANIFEST_UNFINISHED_STATES:
                await self._reject_expired_run(connection, run)
            if state != "collecting":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
            await self._reject_policy_stale_run(connection, run)
            return await self._accept_page(connection, command, run)

    async def _accept_page(
        self,
        connection: AsyncConnection,
        command: AppendManifestPageCommand,
        run: RowMapping,
    ) -> ManifestPageReceipt:
        page_number = command.page_number
        next_page_number = int(run.next_page_number)
        if page_number > next_page_number:
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_PAGE_INVALID)
        if page_number < next_page_number:
            existing = (
                await connection.execute(
                    manifest_page_select_statement(command.manifest_run_id, page_number)
                )
            ).one_or_none()
            if (
                existing is not None
                and int(existing.entry_count) == len(command.entries)
                and str(existing.page_digest) == command.page_digest.hexadecimal
            ):
                return ManifestPageReceipt(
                    manifest_run_id=command.manifest_run_id,
                    page_number=page_number,
                    accepted_entry_count=int(existing.entry_count),
                    next_page_number=next_page_number,
                )
            await self._fail_run(
                connection,
                command.manifest_run_id,
                DeviceSyncErrorCode.MANIFEST_PAGE_REPLAY_MISMATCH,
            )
            await connection.commit()
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_PAGE_REPLAY_MISMATCH)
        if int(run.entry_count) + len(command.entries) > MAX_MANIFEST_RUN_ENTRIES:
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_PAGE_INVALID)
        resolution_rows = await self._resolve_page_entries(
            connection, command.context.workspace_id, run, command
        )
        await connection.execute(
            sa.insert(manifest_pages).values(
                manifest_run_id=command.manifest_run_id,
                page_number=page_number,
                entry_count=len(command.entries),
                page_digest=command.page_digest.hexadecimal,
            )
        )
        if resolution_rows:
            await connection.execute(sa.insert(manifest_entry_resolutions), resolution_rows)
        await connection.execute(
            sa.update(manifest_runs)
            .values(
                next_page_number=page_number + 1,
                entry_count=int(run.entry_count) + len(command.entries),
            )
            .where(manifest_runs.c.manifest_run_id == command.manifest_run_id)
        )
        return ManifestPageReceipt(
            manifest_run_id=command.manifest_run_id,
            page_number=page_number,
            accepted_entry_count=len(command.entries),
            next_page_number=page_number + 1,
        )

    # -- identity resolution ------------------------------------------------------

    async def _resolve_page_entries(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        run: RowMapping,
        command: AppendManifestPageCommand,
    ) -> list[dict[str, Any]]:
        checkpoint = int(run.checkpoint_sequence)
        entries = command.entries
        locator_texts = list(dict.fromkeys(entry.normalized_locator.value for entry in entries))
        current_by_locator: dict[str, list[dict[str, Any]]] = {}
        historical_by_locator: dict[str, list[dict[str, Any]]] = {}
        tombstones_by_locator: dict[str, list[dict[str, Any]]] = {}
        matched_source_ids: set[UUID] = set()
        if locator_texts:
            for row in (
                await connection.execute(
                    manifest_locator_candidates_statement(
                        workspace_id, checkpoint, locator_texts
                    )
                )
            ).mappings():
                matched_source_ids.add(row.source_id)
                closed_at_or_before_checkpoint = row.closed_event_id is not None and int(
                    row.closed_sequence
                ) <= checkpoint
                bucket = (
                    historical_by_locator if closed_at_or_before_checkpoint else current_by_locator
                )
                bucket.setdefault(str(row.normalized_locator), []).append(dict(row))
            for row in (
                await connection.execute(
                    manifest_tombstone_candidates_statement(
                        workspace_id, checkpoint, locator_texts
                    )
                )
            ).mappings():
                matched_source_ids.add(row.source_id)
                tombstones_by_locator.setdefault(str(row.retained_locator), []).append(dict(row))
        canonical_by_source = await self._canonical_states(
            connection, workspace_id, checkpoint, sorted(matched_source_ids)
        )
        resolution_rows: list[dict[str, Any]] = []
        for entry_index, entry in enumerate(entries):
            evidence_bundle = self._entry_evidence(
                entry,
                current_by_locator=current_by_locator,
                historical_by_locator=historical_by_locator,
                tombstones_by_locator=tombstones_by_locator,
                canonical_by_source=canonical_by_source,
            )
            identity = resolve_manifest_identity(evidence_bundle)
            resolved_locator_id: UUID | None = None
            resolved_tombstone_id: UUID | None = None
            if identity.match_kind is ManifestMatchKind.CURRENT_LOCATOR:
                matched = self._matched_locator_row(
                    current_by_locator[entry.normalized_locator.value], identity.source_id
                )
                resolved_locator_id = matched["source_locator_id"]
            elif identity.match_kind is ManifestMatchKind.HISTORICAL_LOCATOR_FINGERPRINT:
                matched = self._matched_locator_row(
                    historical_by_locator[entry.normalized_locator.value], identity.source_id
                )
                resolved_locator_id = matched["source_locator_id"]
            elif identity.match_kind is ManifestMatchKind.OPEN_TOMBSTONE_FINGERPRINT:
                matched = self._matched_tombstone_row(
                    tombstones_by_locator[entry.normalized_locator.value], identity.source_id
                )
                resolved_tombstone_id = matched["source_tombstone_id"]
            resolution_rows.append(
                {
                    "manifest_run_id": command.manifest_run_id,
                    "page_number": command.page_number,
                    "entry_index": entry_index,
                    "local_entry_id": entry.local_entry_id,
                    "known_source_id": entry.known_source_id,
                    "known_version_id": entry.known_version_id,
                    "submitted_sha256": entry.fingerprint.sha256,
                    "submitted_size_bytes": entry.fingerprint.size_bytes,
                    "submitted_media_type": entry.fingerprint.media_type,
                    "locator_evidence_digest": compute_locator_evidence_digest(
                        entry.normalized_locator, evidence_bundle
                    ),
                    "resolved_source_id": identity.source_id,
                    "resolved_source_version_id": identity.source_version_id,
                    "resolved_source_locator_id": resolved_locator_id,
                    "resolved_source_tombstone_id": resolved_tombstone_id,
                    "match_kind": identity.match_kind.value,
                }
            )
        return resolution_rows

    @staticmethod
    def _matched_locator_row(
        rows: list[dict[str, Any]], source_id: UUID | None
    ) -> dict[str, Any]:
        for row in rows:
            if row["source_id"] == source_id:
                return row
        raise DeviceSyncError(DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED)

    @staticmethod
    def _matched_tombstone_row(
        rows: list[dict[str, Any]], source_id: UUID | None
    ) -> dict[str, Any]:
        for row in rows:
            if row["source_id"] == source_id:
                return row
        raise DeviceSyncError(DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED)

    def _entry_evidence(
        self,
        entry: ManifestEntry,
        *,
        current_by_locator: dict[str, list[dict[str, Any]]],
        historical_by_locator: dict[str, list[dict[str, Any]]],
        tombstones_by_locator: dict[str, list[dict[str, Any]]],
        canonical_by_source: dict[UUID, dict[str, Any]],
    ) -> ManifestIdentityEvidence:
        locator_text = entry.normalized_locator.value

        def candidates(rows: list[dict[str, Any]]) -> tuple[CanonicalManifestSource, ...]:
            resolved: list[CanonicalManifestSource] = []
            for row in rows:
                state = canonical_by_source.get(row["source_id"])
                if state is None:
                    continue
                source = self._canonical_manifest_source(state)
                if source is None:
                    continue
                resolved.append(source)
            return tuple(sorted(resolved, key=lambda candidate: candidate.source_id.hex))

        current = candidates(current_by_locator.get(locator_text, []))
        # A closed locator is an authority only for a source still alive at
        # the checkpoint; a deleted source proves through its open
        # tombstone instead.
        historical_rows = [
            row
            for row in historical_by_locator.get(locator_text, [])
            if canonical_by_source.get(row["source_id"], {}).get("open_tombstone_id") is None
        ]
        historical = candidates(historical_rows)
        tombstone_candidates: list[CanonicalManifestSource] = []
        for row in tombstones_by_locator.get(locator_text, []):
            fingerprint = _fingerprint(
                row["retained_sha256"], row["retained_size_bytes"], row["retained_media_type"]
            )
            if fingerprint is None:
                continue
            try:
                retained_locator = NormalizedLocator(str(row["retained_locator"]))
            except ValueError:
                continue
            tombstone_candidates.append(
                CanonicalManifestSource(
                    source_id=row["source_id"],
                    current_version_id=row["retained_version_id"],
                    current_fingerprint=fingerprint,
                    locator=retained_locator,
                    tombstone_id=row["source_tombstone_id"],
                    is_policy_allowed=True,
                )
            )
        open_tombstones = tuple(
            sorted(tombstone_candidates, key=lambda candidate: candidate.source_id.hex)
        )
        if entry.known_source_id is not None:
            # Client-named identity scopes or empties the evidence: invalid
            # or cross-workspace evidence fails closed with no locator
            # fallback.
            known = entry.known_source_id
            current = tuple(candidate for candidate in current if candidate.source_id == known)
            historical = tuple(
                candidate for candidate in historical if candidate.source_id == known
            )
            open_tombstones = tuple(
                candidate for candidate in open_tombstones if candidate.source_id == known
            )
        return ManifestIdentityEvidence(
            local_entry=entry,
            current_locator_candidates=current,
            historical_locator_candidates=historical,
            open_tombstone_candidates=open_tombstones,
        )

    def _canonical_manifest_source(self, state: dict[str, Any]) -> CanonicalManifestSource | None:
        fingerprint = _fingerprint(
            state.get("content_hash"),
            state.get("byte_size"),
            state.get("media_type"),
        )
        version_id = state.get("source_version_id")
        if fingerprint is None or version_id is None:
            return None
        try:
            locator = NormalizedLocator(str(state["active_locator"]))
        except (KeyError, ValueError):
            return None
        return CanonicalManifestSource(
            source_id=state["source_id"],
            current_version_id=version_id,
            current_fingerprint=fingerprint,
            locator=locator,
            tombstone_id=state.get("open_tombstone_id"),
            is_policy_allowed=True,
        )

    async def _canonical_states(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        checkpoint_sequence: int,
        source_ids: Sequence[UUID],
    ) -> dict[UUID, dict[str, Any]]:
        if not source_ids:
            return {}
        rows = (
            await connection.execute(
                manifest_canonical_source_state_statement(
                    workspace_id, checkpoint_sequence, source_ids
                )
            )
        ).mappings()
        return {row.source_id: dict(row) for row in rows}

    # -- finalize -------------------------------------------------------------------

    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt:
        """Verify the final digest and materialize the deterministic plan."""

        return await self._retry.run(lambda _attempt: self._finalize_once(command))

    async def _finalize_once(self, command: FinalizeManifestCommand) -> ManifestRunReceipt:
        context = command.context
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            run = await self._lock_run(connection, context, command.manifest_run_id)
            state = str(run.state)
            if state == "planned":
                await self._reject_expired_run(connection, run)
                return self._planned_replay_or_reject(run, command)
            if state == "expired":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_EXPIRED)
            if state != "collecting":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
            await self._reject_expired_run(connection, run)
            if command.total_entry_count != int(run.entry_count):
                await self._fail_run(
                    connection,
                    command.manifest_run_id,
                    DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH,
                )
                await connection.commit()
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH)
            page_rows = (
                await connection.execute(
                    manifest_page_records_statement(command.manifest_run_id)
                )
            ).all()
            expected_digest = compute_manifest_final_digest(
                tuple(
                    (int(row.page_number), int(row.entry_count), str(row.page_digest))
                    for row in page_rows
                )
            )
            if expected_digest != command.final_digest.hexadecimal:
                await self._fail_run(
                    connection,
                    command.manifest_run_id,
                    DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH,
                )
                await connection.commit()
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH)
            revision = await self._load_bound_policy_revision(
                connection, context.workspace_id, int(run.policy_revision_number)
            )
            action_rows = await self._plan_actions(
                connection, context.workspace_id, run, revision
            )
            if action_rows:
                await connection.execute(sa.insert(manifest_actions), action_rows)
            await connection.execute(
                manifest_run_planned_statement(
                    command.manifest_run_id, command.final_digest.hexadecimal
                )
            )
            planned = (
                await connection.execute(
                    manifest_run_select_statement(
                        context.workspace_id, context.device_id, command.manifest_run_id
                    )
                )
            ).one()
            return self._run_receipt(planned)

    def _planned_replay_or_reject(
        self, run: RowMapping, command: FinalizeManifestCommand
    ) -> ManifestRunReceipt:
        if (
            command.total_entry_count == int(run.entry_count)
            and run.final_digest is not None
            and str(run.final_digest) == command.final_digest.hexadecimal
        ):
            return self._run_receipt(run)
        raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH)

    async def _load_bound_policy_revision(
        self, connection: AsyncConnection, workspace_id: UUID, revision_number: int
    ) -> ExclusionPolicyRevision:
        revision_row = (
            await connection.execute(
                bound_policy_revision_statement(workspace_id, revision_number)
            )
        ).one_or_none()
        if revision_row is None:
            # The bound revision is immutable published state; its absence
            # means the workspace's policy history no longer supports this
            # run's plan.
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED)
        rule_rows = list(
            (
                await connection.execute(
                    policy_rules_select_statement(revision_row.policy_revision_id)
                )
            ).mappings()
        )
        return ExclusionPolicyRevision(
            policy_revision_id=revision_row.policy_revision_id,
            workspace_id=workspace_id,
            revision_number=revision_number,
            rules=hydrate_policy_revision_rules(rule_rows),
        )

    async def _plan_actions(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        run: RowMapping,
        revision: ExclusionPolicyRevision,
    ) -> list[dict[str, Any]]:
        checkpoint = int(run.checkpoint_sequence)
        resolution_rows = list(
            (
                await connection.execute(
                    manifest_resolution_rows_statement(run.manifest_run_id)
                )
            ).mappings()
        )
        resolved_source_ids = sorted(
            {
                row.resolved_source_id
                for row in resolution_rows
                if row.resolved_source_id is not None
            }
        )
        canonical_by_source = await self._canonical_states(
            connection, workspace_id, checkpoint, resolved_source_ids
        )
        known_version_ids = sorted(
            {
                row.known_version_id
                for row in resolution_rows
                if row.known_version_id is not None
            }
        )
        base_fingerprints = await self._known_base_fingerprints(
            connection, workspace_id, known_version_ids
        )
        action_rows: list[dict[str, Any]] = []
        for ordinal, row in enumerate(resolution_rows):
            submitted = _fingerprint(
                row.submitted_sha256, row.submitted_size_bytes, row.submitted_media_type
            )
            if submitted is None:
                raise DeviceSyncError(DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED)
            base_state = (
                base_fingerprints.get(row.known_version_id)
                if row.known_version_id is not None
                else None
            )
            trusted_base = (
                _fingerprint(
                    base_state["content_hash"], base_state["byte_size"], base_state["media_type"]
                )
                if base_state is not None
                and base_state["source_id"] == row.known_source_id
                else None
            )
            resolution = ManifestEntryResolution(
                local_entry_id=str(row.local_entry_id),
                entry_ordinal=ordinal,
                known_source_id=row.known_source_id,
                known_version_id=row.known_version_id,
                submitted_fingerprint=submitted,
                known_base_fingerprint=trusted_base,
                is_policy_allowed=self._submitted_subject_is_allowed(
                    revision, workspace_id, submitted
                ),
                match_kind=ManifestMatchKind(str(row.match_kind)),
                resolved_source_id=row.resolved_source_id,
                resolved_source_version_id=row.resolved_source_version_id,
                resolved_source_locator_id=row.resolved_source_locator_id,
                resolved_source_tombstone_id=row.resolved_source_tombstone_id,
            )
            canonical_state = (
                canonical_by_source.get(resolution.resolved_source_id)
                if resolution.resolved_source_id is not None
                else None
            )
            canonical: CanonicalManifestSource | None = None
            if canonical_state is not None:
                candidate = self._canonical_manifest_source(canonical_state)
                if candidate is not None:
                    canonical = CanonicalManifestSource(
                        source_id=candidate.source_id,
                        current_version_id=candidate.current_version_id,
                        current_fingerprint=candidate.current_fingerprint,
                        locator=candidate.locator,
                        tombstone_id=candidate.tombstone_id,
                        is_policy_allowed=self._source_is_policy_allowed(
                            revision, workspace_id, canonical_state
                        ),
                    )
            action = plan_manifest_action(resolution, canonical)
            action_rows.append(self._action_row(run.manifest_run_id, action))
        download_index = len(resolution_rows)
        for row in (
            await connection.execute(
                manifest_canonical_only_downloads_statement(
                    workspace_id, checkpoint, resolved_source_ids
                )
            )
        ).mappings():
            state = {
                "source_id": row.source_id,
                "source_type": row.source_type,
                "source_version_id": row.source_version_id,
                "media_type": row.media_type,
                "byte_size": row.byte_size,
                "active_locator": row.active_locator,
            }
            if row.source_version_id is None:
                continue
            if not self._source_is_policy_allowed(revision, workspace_id, state):
                continue
            action = ManifestAction(
                action_index=download_index,
                action_kind=ManifestActionKind.DOWNLOAD,
                local_entry_id=None,
                source_id=row.source_id,
                source_version_id=row.source_version_id,
                source_locator_id=row.active_locator_id,
                source_tombstone_id=None,
                reason=None,
            )
            action_rows.append(self._action_row(run.manifest_run_id, action))
            download_index += 1
        return action_rows

    @staticmethod
    def _action_row(manifest_run_id: UUID, action: ManifestAction) -> dict[str, Any]:
        return {
            "manifest_run_id": manifest_run_id,
            "action_index": action.action_index,
            "action_kind": action.action_kind.value,
            "local_entry_id": action.local_entry_id,
            "source_id": action.source_id,
            "source_version_id": action.source_version_id,
            "source_locator_id": action.source_locator_id,
            "source_tombstone_id": action.source_tombstone_id,
            "safe_reason_code": action.reason.value if action.reason is not None else None,
        }

    def _source_is_policy_allowed(
        self,
        revision: ExclusionPolicyRevision,
        workspace_id: UUID,
        state: dict[str, Any],
    ) -> bool:
        locator = state.get("active_locator")
        subject = PolicySubject(
            workspace_id=workspace_id,
            source_id=state.get("source_id"),
            normalized_locator=str(locator) if locator is not None else None,
            source_type=self._source_type(state.get("source_type")),
            media_type=self._media_type(state.get("media_type")),
            size_bytes=(
                int(state["byte_size"]) if state.get("byte_size") is not None else None
            ),
        )
        outcome = evaluate_policy(revision=revision, subject=subject)
        return outcome.enforced is EnforcedPolicyDecision.ALLOWED

    def _submitted_subject_is_allowed(
        self,
        revision: ExclusionPolicyRevision,
        workspace_id: UUID,
        submitted: SourceFingerprint,
    ) -> bool:
        subject = PolicySubject(
            workspace_id=workspace_id,
            source_id=None,
            normalized_locator=None,
            source_type=None,
            media_type=self._media_type(submitted.media_type),
            size_bytes=submitted.size_bytes,
        )
        outcome = evaluate_policy(revision=revision, subject=subject)
        return outcome.enforced is EnforcedPolicyDecision.ALLOWED

    @staticmethod
    def _source_type(raw: Any) -> SourceType | None:
        if raw is None:
            return None
        try:
            return SourceType(str(raw))
        except ValueError:
            return None

    @staticmethod
    def _media_type(raw: Any) -> CanonicalMediaType | None:
        if raw is None:
            return None
        try:
            return CanonicalMediaType.parse(str(raw))
        except ValueError:
            return None

    async def _known_base_fingerprints(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        version_ids: Sequence[UUID],
    ) -> dict[UUID, dict[str, Any]]:
        if not version_ids:
            return {}
        rows = (
            await connection.execute(
                known_base_fingerprints_statement(workspace_id, version_ids)
            )
        ).mappings()
        return {row.source_version_id: dict(row) for row in rows}

    # -- actions -------------------------------------------------------------------

    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage:
        """Read one stable action page, opening the run on the first read."""

        return await self._retry.run(lambda _attempt: self._read_actions_once(query))

    async def _read_actions_once(self, query: ManifestActionsQuery) -> ManifestActionPage:
        context = query.context
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            run = (
                await connection.execute(
                    manifest_run_select_statement(
                        context.workspace_id,
                        context.device_id,
                        query.manifest_run_id,
                    )
                )
            ).one_or_none()
            if run is None:
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_NOT_FOUND)
            state = str(run.state)
            if state in MANIFEST_UNFINISHED_STATES:
                await self._reject_expired_run(connection, run)
                await self._reject_policy_stale_run(connection, run)
                if state == "collecting":
                    raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
                if state == "planned":
                    await connection.execute(
                        manifest_run_applying_transition_statement(query.manifest_run_id)
                    )
            elif state == "expired":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_EXPIRED)
            elif state == "failed":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
            rows = list(
                (
                    await connection.execute(
                        manifest_action_page_statement(
                            query.manifest_run_id,
                            # The query cursor is inclusive (its floor is
                            # zero), while the shared run-scoped statement
                            # pages on a strictly-greater index: shift by one
                            # so the first page delivers action index zero.
                            after_action_index=query.after_action_index - 1,
                            limit=query.limit + 1,
                        )
                    )
                ).mappings()
            )
            has_more = len(rows) > query.limit
            actions = tuple(
                ManifestAction(
                    action_index=int(row.action_index),
                    action_kind=ManifestActionKind(str(row.action_kind)),
                    local_entry_id=row.local_entry_id,
                    source_id=row.source_id,
                    source_version_id=row.source_version_id,
                    source_locator_id=row.source_locator_id,
                    source_tombstone_id=row.source_tombstone_id,
                    reason=(
                        None
                        if row.safe_reason_code is None
                        else _action_reason(str(row.safe_reason_code))
                    ),
                )
                for row in rows[: query.limit]
            )
            return ManifestActionPage(
                manifest_run_id=query.manifest_run_id,
                actions=actions,
                has_more=has_more,
            )

    # -- completion -----------------------------------------------------------------

    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt:
        """Complete the exact applying run and advance the cursor to C."""

        return await self._retry.run(lambda _attempt: self._complete_once(command))

    async def _complete_once(self, command: CompleteManifestCommand) -> DeviceCursorReceipt:
        context = command.context
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            run = await self._lock_run(connection, context, command.manifest_run_id)
            state = str(run.state)
            if state == "completed":
                if (
                    run.final_digest is None
                    or str(run.final_digest) != command.final_digest.hexadecimal
                ):
                    raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH)
                return await self._completion_receipt(
                    connection, context, int(run.checkpoint_sequence)
                )
            if state == "failed":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
            if state == "expired":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_EXPIRED)
            if state in MANIFEST_UNFINISHED_STATES:
                await self._reject_expired_run(connection, run)
            if state != "applying":
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
            await self._reject_policy_stale_run(connection, run)
            if (
                run.final_digest is None
                or str(run.final_digest) != command.final_digest.hexadecimal
            ):
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH)
            completed = await connection.execute(
                manifest_run_completion_transition_statement(command.manifest_run_id)
            )
            if completed.rowcount != 1:
                raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_STATE_INVALID)
            checkpoint = int(run.checkpoint_sequence)
            await connection.execute(
                device_cursor_completion_bootstrap_statement(
                    device_cursor_id=self._identity_generator(),
                    workspace_id=context.workspace_id,
                    device_id=context.device_id,
                    checkpoint_sequence=checkpoint,
                )
            )
            await connection.execute(
                device_cursor_completion_advance_statement(
                    context.workspace_id,
                    context.device_id,
                    checkpoint_sequence=checkpoint,
                )
            )
            return await self._completion_receipt(connection, context, checkpoint)

    async def _completion_receipt(
        self,
        connection: AsyncConnection,
        context: Any,
        checkpoint_sequence: int,
    ) -> DeviceCursorReceipt:
        row = (
            await connection.execute(
                device_cursor_select_statement(context.workspace_id, context.device_id)
            )
        ).one()
        return DeviceCursorReceipt(
            acknowledged_sequence=int(row.acknowledged_sequence),
            delivered_through_sequence=max(
                int(row.delivered_through_sequence), checkpoint_sequence
            ),
        )

    # -- shared guards ---------------------------------------------------------------

    async def _lock_run(
        self, connection: AsyncConnection, context: Any, manifest_run_id: UUID
    ) -> RowMapping:
        reject_nil_uuid("manifest_run_id", manifest_run_id)
        row = (
            await connection.execute(
                manifest_run_select_statement(
                    context.workspace_id,
                    context.device_id,
                    manifest_run_id,
                    for_update=True,
                )
            )
        ).one_or_none()
        if row is None:
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_NOT_FOUND)
        return cast(RowMapping, row)

    async def _reject_expired_run(self, connection: AsyncConnection, run: RowMapping) -> None:
        state = str(run.state)
        if state not in MANIFEST_UNFINISHED_STATES:
            return
        database_now = await self._database_now(connection)
        if run.expires_at <= database_now:
            # The expiry mark outlives the rejection: it commits on its own
            # so the rollback of the rejecting request cannot resurrect the
            # run (nothing else is pending in these fail-closed paths).
            await connection.execute(manifest_run_expire_statement(run.manifest_run_id))
            await connection.commit()
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_EXPIRED)

    async def _reject_policy_stale_run(
        self, connection: AsyncConnection, run: RowMapping
    ) -> None:
        active = await self._read_active_policy_revision_number(
            connection, run.workspace_id
        )
        if active != int(run.policy_revision_number):
            # The closed reason persists on the run row beyond the rejected
            # request: the failure mark commits before the typed raise.
            await self._fail_run(
                connection, run.manifest_run_id, DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED
            )
            await connection.commit()
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED)

    async def _fail_run(
        self,
        connection: AsyncConnection,
        manifest_run_id: UUID,
        code: DeviceSyncErrorCode,
    ) -> None:
        await connection.execute(manifest_run_fail_statement(manifest_run_id, code.value))


def _action_reason(token: str) -> ManifestActionReason:
    try:
        return ManifestActionReason(token)
    except ValueError:
        raise DeviceSyncError(DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED) from None
