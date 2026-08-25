"""Exact-version verified content descriptor resolution over PostgreSQL.

:class:`PostgresqlDeviceContentCatalog` resolves the verified content
identity of one exact ``(source_id, source_version_id)`` pair inside the
credential workspace: one ``READ COMMITTED`` transaction reads only the
content evidence — canonical digest, byte size and canonical media type —
never the object key or any provider receipt, so the resolved
:class:`~personal_os.device_sync.contracts.DeviceContentDescriptor` can
project nothing but the canonical verification request. Membership is the
whole predicate: the exact pair must resolve within the workspace that owns
the source, so a foreign workspace's pair, a mismatched pair and an unknown
version are all indistinguishable from missing through the closed
event-unavailable rejection, while stored evidence that violates the
canonical grammar is the closed download integrity failure.

Current-policy authorization runs inside the same resolution and strictly
before any byte is fetched: the workspace's active published revision is
loaded through the shared manifest-store statements and evaluated against
the resolved version's content subject (the source's identity, type,
canonical media type and byte size; no locator operand exists on a download
path, so locator-requiring rules evaluate indeterminate and fail closed). A
denial — and a workspace without an active published revision, where every
content operation fails closed — raises the registry's closed authorization
rejection; the wire route owns surfacing it.

Driver failures cross the boundary only through the closed device sync
registry (the shared event-store retry and mapping policy): lock contention
retries with the shared cancellable jitter and connection-class
unavailability maps to the retryable ``device_sync_dependency_unavailable``.
SQLSTATE, SQL text, parameters, driver messages, object keys and digests
never enter a typed error, statement or log line.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.device_sync.contracts import (
    DeviceContentDescriptor,
    DeviceSyncContext,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.evaluation import evaluate_policy
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import SourceType
from postgresql_source_store.device_event_store import DeviceSyncDatabaseRetryPolicy
from postgresql_source_store.device_manifest_store import (
    bound_policy_revision_statement,
    policy_rules_select_statement,
    workspace_active_policy_revision_statement,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.policy_previews import hydrate_policy_revision_rules
from postgresql_source_store.tables import content_objects, source_versions, sources

__all__ = [
    "PostgresqlDeviceContentCatalog",
    "device_content_descriptor_statement",
    "evaluate_device_content_policy",
    "hydrate_device_content_descriptor",
]

#: One hydration row: a SQLAlchemy row mapping from the catalog's
#: ``.mappings()`` results or an equivalent mapping in tests.
type _MappedRow = Mapping[str, Any]


# --- statement builders ---------------------------------------------------------


def device_content_descriptor_statement(
    workspace_id: UUID,
    source_id: UUID,
    source_version_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the credential-scoped exact-version content evidence read.

    The membership predicate is exact: the version row must name the source,
    the source must belong to the credential workspace, and only the content
    evidence columns are selected — the object key and every other
    provider-addressing column stay unread, so a foreign workspace's pair is
    as invisible as a missing one.
    """

    return (
        sa.select(
            sources.c.source_type,
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
            source_versions.c.source_id == sa.bindparam("source_id", source_id),
            source_versions.c.source_version_id
            == sa.bindparam("source_version_id", source_version_id),
        )
    )


# --- hydration and policy seams ---------------------------------------------------


def hydrate_device_content_descriptor(
    *,
    source_id: UUID,
    source_version_id: UUID,
    row: _MappedRow | None,
) -> DeviceContentDescriptor:
    """Hydrate the exact-version descriptor from one credential-scoped row.

    A missing row — an unknown pair and a cross-workspace pair alike, both
    hydrating from the same empty lookup — is the closed event-unavailable
    rejection. Stored evidence that violates the canonical digest or media
    grammar, or carries a non-natural byte size, is the closed download
    integrity failure: the download's expected content identity is unusable.
    """

    if row is None:
        raise DeviceSyncError(DeviceSyncErrorCode.EVENT_UNAVAILABLE)
    try:
        content_digest = ContentDigest.parse(str(row["content_hash"]))
        media_type = CanonicalMediaType.parse(str(row["media_type"]))
        size_bytes = int(row["byte_size"])
        return DeviceContentDescriptor(
            source_id=source_id,
            source_version_id=source_version_id,
            content_digest=content_digest,
            size_bytes=size_bytes,
            media_type=media_type,
        )
    except KeyError, TypeError, ValueError:
        raise DeviceSyncError(DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED) from None


def evaluate_device_content_policy(
    revision: ExclusionPolicyRevision,
    *,
    workspace_id: UUID,
    source_id: UUID,
    source_type: Any,
    media_type: CanonicalMediaType,
    size_bytes: int,
) -> None:
    """Authorize one download subject under the workspace's active revision.

    The subject is the resolved version's content evidence — the source's
    identity, type, canonical media type and byte size. A download authorizes
    exact bytes, not a place, so no locator operand exists here and
    locator-requiring rules evaluate indeterminate and fail closed, matching
    the planner's exclusion semantics. A definite exclusion, an indeterminate
    outcome or an unusable source-type token denies: the registry's closed
    authorization rejection carries the boundary.
    """

    subject = PolicySubject(
        workspace_id=workspace_id,
        source_id=source_id,
        normalized_locator=None,
        source_type=_source_type(source_type),
        media_type=media_type,
        size_bytes=size_bytes,
    )
    outcome = evaluate_policy(revision=revision, subject=subject)
    if outcome.enforced is not EnforcedPolicyDecision.ALLOWED:
        raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)


def _source_type(raw: Any) -> SourceType | None:
    if raw is None:
        return None
    try:
        return SourceType(str(raw))
    except ValueError:
        # A stored token outside the closed source-type vocabulary becomes
        # absent evidence: source-type rules then fail closed instead of
        # matching a fabricated type.
        return None


# --- catalog ----------------------------------------------------------------------


class PostgresqlDeviceContentCatalog:
    """Exact-version content descriptor and policy resolution over the schema.

    The catalog takes the composition-owned :class:`AsyncEngine` and the
    shared device sync database retry policy, and opens no connection at
    construction. ``resolve_descriptor`` runs one ``READ COMMITTED``
    transaction behind the pinned ``SET LOCAL`` bounds, scoped entirely by
    the credential-derived
    :class:`~personal_os.device_sync.contracts.DeviceSyncContext`, and
    returns only the expected digest/size/media descriptor — never an object
    key, presigned URL, receipt or provider detail.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        retry: DeviceSyncDatabaseRetryPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else DeviceSyncDatabaseRetryPolicy()

    async def resolve_descriptor(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceContentDescriptor:
        """Resolve the verified content identity of one exact version pair.

        Membership first, then current-policy authorization — both before
        any byte is fetched anywhere: the closed event-unavailable rejection
        answers an unknown, mismatched or cross-workspace pair, the closed
        authorization rejection answers a denied subject, and the closed
        download integrity failure answers unusable stored evidence.
        """

        del diagnostic_context  # correlation flows through the runtime layer's events
        reject_nil_uuid("source_id", source_id)
        reject_nil_uuid("source_version_id", source_version_id)
        return await self._retry.run(
            lambda _attempt: self._resolve_once(
                context, source_id=source_id, source_version_id=source_version_id
            )
        )

    async def _resolve_once(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
    ) -> DeviceContentDescriptor:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = (
                (
                    await connection.execute(
                        device_content_descriptor_statement(
                            context.workspace_id, source_id, source_version_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            descriptor = hydrate_device_content_descriptor(
                source_id=source_id,
                source_version_id=source_version_id,
                row=row,
            )
            revision = await self._load_active_policy_revision(connection, context.workspace_id)
            evaluate_device_content_policy(
                revision,
                workspace_id=context.workspace_id,
                source_id=source_id,
                source_type=None if row is None else row["source_type"],
                media_type=descriptor.media_type,
                size_bytes=descriptor.size_bytes,
            )
            return descriptor

    async def _load_active_policy_revision(
        self, connection: AsyncConnection, workspace_id: UUID
    ) -> ExclusionPolicyRevision:
        """Load the workspace's active published revision, failing closed.

        A workspace without an active revision, or whose active pointer names
        published rows that no longer exist, can authorize nothing: both fail
        closed as the registry's policy denial rather than fabricating an
        empty allow-everything revision.
        """

        pointer = (
            await connection.execute(workspace_active_policy_revision_statement(workspace_id))
        ).one_or_none()
        if (
            pointer is None
            or pointer.active_policy_revision_id is None
            or int(pointer.active_revision_number) < 1
        ):
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
        revision_row = (
            await connection.execute(
                bound_policy_revision_statement(workspace_id, int(pointer.active_revision_number))
            )
        ).one_or_none()
        if revision_row is None:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
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
            revision_number=int(pointer.active_revision_number),
            rules=hydrate_policy_revision_rules(rule_rows),
        )
