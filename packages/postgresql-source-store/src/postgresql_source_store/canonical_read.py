"""Canonical current-source read adapter over one bounded joined lookup.

:class:`PostgresqlCanonicalSourceReadStore` implements the read-only
:class:`~personal_os.sources.reading.CanonicalSourceReadStore` port.
``resolve_current`` runs one ``READ COMMITTED`` transaction with the pinned
``SET LOCAL`` bounds: a single schema-qualified, parameter-bound ``SELECT``
from ``sources`` left-joined through ``source_versions`` (on
``sources.current_version_id``), ``content_objects`` (on
``source_versions.content_object_id``) and the source's one open
``source_locators`` row (the locator evidence of the read-boundary policy
subject), filtered by both ``workspace_id`` and ``source_id`` — so a source
owned by another workspace is indistinguishable
from a missing one and nothing about the owning tenant is disclosed.

The pure :func:`hydrate_canonical_source_reference` fails closed on every
inconsistency: unreadable source states, a null current pointer, a version row
owned by another workspace or source, a pointer that names a foreign version, a
noncanonical digest, a derived-key mismatch, a negative byte size, a
parameterized media type, a non-positive content version and a naive committed
timestamp all raise the typed
:class:`~personal_os.sources.reading.CanonicalReadStateError` carrying only the
requested ``source_id``. A missing row raises the existing ``SOURCE_NOT_FOUND``
error shape used by :mod:`postgresql_source_store.publication_store`. Driver
failures are routed through :func:`postgresql_source_store.error_mapping.map_database_failure`
so SQLSTATE, SQL, parameters and driver text never leave the adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import PolicyTrustAnchorVerifier
from personal_os.exclusion_policy.metrics import ExclusionPolicyMetrics, PolicyBoundary
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    derive_canonical_object_key,
)
from personal_os.sources.commands import SourceType
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.reading import (
    CanonicalReadStateError,
    CanonicalSourceReference,
    ReadCurrentSourceCommand,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import map_database_failure
from postgresql_source_store.policy_enforcement import evaluate_locked_policy_decision
from postgresql_source_store.tables import (
    content_objects,
    source_locators,
    source_versions,
    sources,
)

#: The only source states whose current reference may be read.
ACCEPTED_READ_SOURCE_STATES: Final[frozenset[str]] = frozenset({"active", "stored_not_indexed"})


def current_reference_lookup_statement(
    workspace_id: UUID, source_id: UUID
) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified, parameter-bound current-reference lookup.

    The left joins keep the ``sources`` row visible when no current version or
    content object is attached, so a pending or dangling-pointer source reaches
    the fail-closed hydration instead of masquerading as a missing source.
    The source's one open ``source_locators`` row (at most one exists per
    source by the partial unique index on the open history) joins the same way,
    so the read-boundary policy subject carries the current locator evidence
    that locator-dependent rules require. Every selected column is labeled
    with the exact hydration row key.
    """
    return (
        sa.select(
            sources.c.workspace_id.label("workspace_id"),
            sources.c.source_id.label("source_id"),
            sources.c.sync_state.label("sync_state"),
            sources.c.source_type.label("source_type"),
            sources.c.current_version_id.label("current_source_version_id"),
            source_versions.c.workspace_id.label("version_workspace_id"),
            source_versions.c.source_id.label("version_source_id"),
            source_versions.c.source_version_id.label("source_version_id"),
            source_versions.c.content_version.label("content_version"),
            source_versions.c.committed_at.label("committed_at"),
            content_objects.c.content_hash.label("content_hash"),
            content_objects.c.object_key.label("object_key"),
            content_objects.c.byte_size.label("byte_size"),
            content_objects.c.media_type.label("media_type"),
            source_locators.c.normalized_locator.label("normalized_locator"),
        )
        .select_from(sources)
        .outerjoin(
            source_versions,
            source_versions.c.source_version_id == sources.c.current_version_id,
        )
        .outerjoin(
            content_objects,
            content_objects.c.content_object_id == source_versions.c.content_object_id,
        )
        .outerjoin(
            source_locators,
            sa.and_(
                source_locators.c.workspace_id == sources.c.workspace_id,
                source_locators.c.source_id == sources.c.source_id,
                source_locators.c.closed_at.is_(None),
            ),
        )
        .where(
            sources.c.workspace_id == workspace_id,
            sources.c.source_id == source_id,
        )
    )


def hydrate_canonical_source_reference(row: Mapping[str, Any]) -> CanonicalSourceReference:
    """Hydrate one joined lookup row into the canonical current reference.

    Pure and fail-closed: every pointer, state, digest, key, size, media and
    time violation raises the typed read-state integrity error with only the
    row's ``source_id`` as its safe detail. The object key must equal the only
    derivable canonical key of the stored digest, so a key that disagrees with
    the content identity never reaches the object store.
    """
    workspace_id = row["workspace_id"]
    source_id = row["source_id"]
    if not isinstance(workspace_id, UUID) or not isinstance(source_id, UUID):
        # Impossible against the migrated constraints; a row without typed
        # identities is adapter or schema drift, never untrusted input.
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)

    def invalid() -> CanonicalReadStateError:
        return CanonicalReadStateError(source_id=source_id)

    if row["sync_state"] not in ACCEPTED_READ_SOURCE_STATES:
        raise invalid()
    current_source_version_id = row["current_source_version_id"]
    source_version_id = row["source_version_id"]
    if (
        current_source_version_id is None
        or not isinstance(source_version_id, UUID)
        or row["version_workspace_id"] != workspace_id
        or row["version_source_id"] != source_id
        or current_source_version_id != source_version_id
    ):
        raise invalid()
    content_version = row["content_version"]
    byte_size = row["byte_size"]
    committed_at = row["committed_at"]
    content_hash = row["content_hash"]
    object_key = row["object_key"]
    media_type_value = row["media_type"]
    if (
        not isinstance(content_version, int)
        or content_version < 1
        or not isinstance(byte_size, int)
        or byte_size < 0
        or not isinstance(committed_at, datetime)
        or committed_at.tzinfo is None
        or not isinstance(content_hash, str)
        or not isinstance(object_key, str)
        or not isinstance(media_type_value, str)
    ):
        raise invalid()
    try:
        digest = ContentDigest.parse(content_hash)
        media_type = CanonicalMediaType.parse(media_type_value)
    except ValueError as cause:
        raise invalid() from cause
    source_type_value = row["source_type"]
    try:
        source_type = SourceType(str(source_type_value))
    except ValueError as cause:
        # Impossible against the CHECK constraint; fail closed as drift.
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
    if object_key != derive_canonical_object_key(digest).value:
        raise invalid()
    return CanonicalSourceReference(
        workspace_id=workspace_id,
        source_id=source_id,
        source_version_id=source_version_id,
        content_version=content_version,
        source_type=source_type,
        expected_object=ExpectedObject(
            content_digest=digest,
            size_bytes=byte_size,
            media_type=media_type,
        ),
        committed_at=committed_at,
    )


def build_canonical_read_policy_subject(
    reference: CanonicalSourceReference, normalized_locator: str | None
) -> PolicySubject:
    """Build the read-boundary policy subject with the current locator evidence.

    The hydrated reference supplies the pointer-consistent type, media and
    size evidence; the joined open locator supplies the locator evidence that
    locator-dependent rules (extension, folder-prefix, path-glob) require, so
    they evaluate definitively at this boundary exactly as they do at the
    authorize boundary. A source with no open locator row keeps genuinely
    absent locator evidence instead of a fabricated value.
    """

    return PolicySubject(
        workspace_id=reference.workspace_id,
        source_id=reference.source_id,
        normalized_locator=normalized_locator,
        source_type=reference.source_type,
        media_type=reference.expected_object.media_type,
        size_bytes=reference.expected_object.size_bytes,
    )


class PostgresqlCanonicalSourceReadStore:
    """Read-only canonical current-reference store over the PostgreSQL baseline.

    The store takes the composition-owned :class:`AsyncEngine` and the
    mandatory policy trust-anchor verifier; it opens no connection at
    construction. ``resolve_current`` performs exactly one bounded joined
    read, hydrates the reference and — inside the same transaction — locks the
    ``workspace_policy_state`` row and re-evaluates the active signed policy
    against the hydrated subject, so no object-store request can be issued
    before the current policy permits the source. It never mutates state,
    pointer, versions, events, audit or intents.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        policy_verifier: PolicyTrustAnchorVerifier,
        policy_metrics: ExclusionPolicyMetrics | None = None,
    ) -> None:
        self._engine = engine
        self._policy_verifier = policy_verifier
        self._policy_metrics = policy_metrics

    async def resolve_current(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> CanonicalSourceReference:
        try:
            async with (
                self._engine.connect() as connection,
                connection.begin(),
            ):
                await apply_transaction_bounds(connection)
                result = await connection.execute(
                    current_reference_lookup_statement(command.workspace_id, command.source_id)
                )
                row = result.one_or_none()
                if row is None:
                    raise SourcePublicationError(
                        ErrorCode.SOURCE_NOT_FOUND,
                        safe_details={"source_id": command.source_id},
                    )
                row_mapping = row._mapping
                reference = hydrate_canonical_source_reference(row_mapping)
                # Spec 14: the policy recheck shares the read transaction that
                # resolves the source state, so the pointer cannot move and a
                # policy revision cannot activate between resolution and the
                # authorization decision. The subject carries the joined open
                # locator so locator-dependent rules evaluate definitively
                # here instead of failing as indeterminate.
                subject = build_canonical_read_policy_subject(
                    reference, row_mapping["normalized_locator"]
                )
                await evaluate_locked_policy_decision(
                    connection,
                    workspace_id=command.workspace_id,
                    subject=subject,
                    verifier=self._policy_verifier,
                    metrics=self._policy_metrics,
                    boundary=PolicyBoundary.CANONICAL_READ,
                )
        except ApplicationError:
            raise
        except Exception as cause:
            raise map_database_failure(cause, source_id=command.source_id) from cause
        return reference
