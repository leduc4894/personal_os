"""PostgreSQL quiesced exported-snapshot adapter and restore-target probes.

:class:`PostgresqlBackupSnapshotStore` implements the provider-neutral
:class:`~personal_os.recovery.ports.CanonicalBackupSnapshotStore` port over the
canonical baseline (spec 9.2): a ``REPEATABLE READ`` transaction begun before
its first query, twenty ``LOCK TABLE ... IN SHARE MODE NOWAIT`` statements in
the fixed order, the ``pg_export_snapshot()`` token, the server version, the
Alembic head, the twenty table counts and the referenced content objects —
all read from the same snapshot, with no mutation, and a rollback on context
exit that releases every lock. The canonical policy tables (state, immutable
revisions/rules, key history and durable intents) join the snapshot; the
ephemeral preview tables stay reconstructible and excluded. The snapshot token
is infrastructure-private: it is carried on the frozen snapshot value and
flows only to the dump process inside the composition call; this module never
logs, prints or embeds it in an error.

:class:`PostgresqlRestoreTarget` provides the pre/post-restore probes of
spec 11.1: application emptiness, the twenty canonical counts, the schema head
and the current-pointer resolution count expected to be zero after a restore.

Every driver failure crosses the boundary as a closed-token
:class:`~personal_os.recovery.contracts.RecoveryError`: lock contention and
``NOWAIT`` refusal as the retryable ``canonical_recovery_snapshot_busy``, and
any other database unavailability as ``canonical_recovery_dependency_unavailable``
with the ``postgresql`` dependency. SQLSTATE values, SQL text, parameters and
driver messages stay chained as causes only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    derive_canonical_object_key,
)
from personal_os.recovery.contracts import (
    MAXIMUM_OBJECT_SIZE_BYTES,
    RecoveryComponent,
    RecoveryDependency,
    RecoveryError,
)
from personal_os.recovery.ports import CanonicalBackupSnapshot
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    DatabaseFailureKind,
    classify_database_failure,
)
from postgresql_source_store.tables import (
    SOURCE_STORE_SCHEMA,
    SOURCE_STORE_TABLES,
    content_objects,
    source_versions,
    sources,
)

#: The binding fixed order of the twenty quiescing share locks. The
#: canonical baseline tables are followed by the canonical policy tables in
#: foreign-key dependency order (publication locks workspace_policy_state
#: before drafts and revisions), then the durable small-file upload-operation
#: table (its workspace/device containment references baseline tables only);
#: the reconstructible preview tables stay out of the canonical snapshot by
#: design.
SNAPSHOT_LOCK_ORDER: Final[tuple[str, ...]] = (
    "users",
    "workspaces",
    "devices",
    "content_objects",
    "sources",
    "source_versions",
    "sync_events",
    "projection_intents",
    "audit_events",
    "workspace_policy_state",
    "policy_signing_keys",
    "policy_keysets",
    "policy_keyset_signatures",
    "source_policies",
    "policy_rules",
    "policy_drafts",
    "policy_draft_rules",
    "policy_evaluations",
    "policy_reconciliation_intents",
    "small_file_upload_operations",
)

#: The snapshot transaction waits at most this long for each share lock.
SNAPSHOT_LOCK_TIMEOUT_SECONDS: Final[int] = 15

#: The snapshot transaction's isolation level: stronger than the engine's
#: ``READ COMMITTED`` default so every read observes one stable snapshot.
SNAPSHOT_ISOLATION_LEVEL: Final[str] = "REPEATABLE READ"

#: Applied immediately after the snapshot begin: the widened snapshot lock
#: timeout plus the established statement and idle-in-transaction bounds.
#: ``SET LOCAL`` scopes every bound to the snapshot transaction only.
SNAPSHOT_TRANSACTION_BOUND_STATEMENTS: Final[tuple[str, ...]] = (
    f"SET LOCAL lock_timeout = '{SNAPSHOT_LOCK_TIMEOUT_SECONDS * 1000}ms'",
    "SET LOCAL statement_timeout = '15000ms'",
    "SET LOCAL idle_in_transaction_session_timeout = '30000ms'",
)

#: Alembic's version table lives in the default schema.
_ALEMBIC_SCHEMA: Final[str] = "public"
_ALEMBIC_TABLE: Final[str] = "alembic_version"

_EXPORT_SNAPSHOT_STATEMENT: Final[sa.TextClause] = sa.text("SELECT pg_export_snapshot()")
_SERVER_VERSION_STATEMENT: Final[sa.TextClause] = sa.text(
    # Distribution builds append a packaging suffix to the ``server_version``
    # setting; the pinned contract compares the bare upstream version token.
    "SELECT split_part(current_setting('server_version'), ' ', 1)"
)
_SCHEMA_HEAD_STATEMENT: Final[sa.TextClause] = sa.text(
    f"SELECT version_num FROM {_ALEMBIC_SCHEMA}.{_ALEMBIC_TABLE}"
)
_SCHEMA_RELATION_COUNT_STATEMENT: Final[sa.TextClause] = sa.text(
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = :schema"
)
_ALEMBIC_PRESENCE_COUNT_STATEMENT: Final[sa.TextClause] = sa.text(
    "SELECT count(*) FROM information_schema.tables"
    " WHERE table_schema = :schema AND table_name = :table_name"
)


def _map_snapshot_failure(cause: BaseException) -> RecoveryError:
    """Map a snapshot or probe failure onto the closed recovery error set.

    Lock contention (lock timeout, ``NOWAIT`` refusal, deadlock) maps to the
    retryable snapshot-busy token; every other database or driver failure maps
    to the retryable dependency-unavailable token with the ``postgresql``
    dependency. The cause stays chained only; its text never enters the error.
    """
    if classify_database_failure(cause) is DatabaseFailureKind.CONTENTION:
        return RecoveryError(ErrorCode.CANONICAL_RECOVERY_SNAPSHOT_BUSY)
    return RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE,
        safe_details={"dependency": RecoveryDependency.POSTGRESQL},
    )


def build_share_lock_statements() -> tuple[sa.TextClause, ...]:
    """Build the twenty quiescing share locks in the binding fixed order.

    Each statement is a schema-qualified, fully quoted
    ``LOCK TABLE knowledge."<table>" IN SHARE MODE NOWAIT``; the order is the
    single fixed order of :data:`SNAPSHOT_LOCK_ORDER`, never caller-chosen.
    """
    return tuple(
        sa.text(f'LOCK TABLE {SOURCE_STORE_SCHEMA}."{table_name}" IN SHARE MODE NOWAIT')
        for table_name in SNAPSHOT_LOCK_ORDER
    )


def pending_writer_count_statement() -> sa.TextClause:
    """Build the parameter-bound count of ungranted relation locks.

    Counts lock requests on the twenty canonical tables that PostgreSQL has not
    granted (blocked writers waiting on the quiescing share locks); the table
    names travel only as the bound ``:tables`` array parameter.
    """
    return sa.text(
        "SELECT count(*) FROM pg_locks"
        " JOIN pg_class ON pg_locks.relation = pg_class.oid"
        " WHERE pg_locks.locktype = 'relation'"
        " AND NOT pg_locks.granted"
        " AND pg_class.relname = ANY(:tables)"
    )


def referenced_objects_statement() -> sa.Select[tuple[Any, ...]]:
    """Build the distinct referenced-content-objects read of the snapshot.

    Joins ``content_objects`` from ``source_versions`` and keeps one row per
    distinct referenced object identity with exactly the four hydration
    columns; unreferenced content objects are deliberately not part of the
    snapshot evidence.
    """
    return (
        sa.select(
            content_objects.c.content_hash.label("content_hash"),
            content_objects.c.object_key.label("object_key"),
            content_objects.c.byte_size.label("byte_size"),
            content_objects.c.media_type.label("media_type"),
        )
        .select_from(content_objects)
        .join(
            source_versions,
            source_versions.c.content_object_id == content_objects.c.content_object_id,
        )
        .distinct()
    )


def current_pointer_resolution_statement() -> sa.Select[tuple[Any]]:
    """Build the post-restore current-pointer resolution probe.

    One joined read counting the sources whose ``current_version_id`` is null,
    names a version owned by another source (the version join matches the
    pointer within the same source, so a foreign version never resolves), or
    resolves to a version whose content object is missing. A consistent
    post-restore state expects zero.
    """
    return (
        sa.select(sa.func.count())
        .select_from(sources)
        .outerjoin(
            source_versions,
            sa.and_(
                source_versions.c.source_version_id == sources.c.current_version_id,
                source_versions.c.source_id == sources.c.source_id,
            ),
        )
        .outerjoin(
            content_objects,
            content_objects.c.content_object_id == source_versions.c.content_object_id,
        )
        .where(
            sources.c.current_version_id.is_(None)
            | source_versions.c.source_version_id.is_(None)
            | content_objects.c.content_object_id.is_(None),
        )
    )


def table_count_statement(table: sa.Table) -> sa.Select[tuple[Any]]:
    """Build the exact count read for one canonical table."""
    return sa.select(sa.func.count()).select_from(table)


def _reject_object_set() -> RecoveryError:
    return RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED,
        safe_details={"component": RecoveryComponent.OBJECT_SET},
    )


def _hydrate_expected_object(row: Mapping[str, Any]) -> ExpectedObject:
    """Hydrate one referenced-object row, raising ``ValueError`` on violation."""
    content_hash = row["content_hash"]
    object_key = row["object_key"]
    byte_size = row["byte_size"]
    media_type_value = row["media_type"]
    if (
        not isinstance(content_hash, str)
        or not isinstance(object_key, str)
        or not isinstance(media_type_value, str)
        or not isinstance(byte_size, int)
        or isinstance(byte_size, bool)
        or not 0 <= byte_size <= MAXIMUM_OBJECT_SIZE_BYTES
    ):
        raise ValueError("referenced object row violates the snapshot object contract")
    digest = ContentDigest.parse(content_hash)
    media_type = CanonicalMediaType.parse(media_type_value)
    if object_key != derive_canonical_object_key(digest).value:
        raise ValueError("object key does not match the canonical derivation")
    return ExpectedObject(content_digest=digest, size_bytes=byte_size, media_type=media_type)


def hydrate_referenced_objects(rows: Iterable[Mapping[str, Any]]) -> tuple[ExpectedObject, ...]:
    """Hydrate and validate the snapshot's referenced content objects.

    Pure and fail-closed: every digest, derived-key, media-type or size
    violation raises the typed integrity failure with the closed ``object_set``
    component only — never the offending value. Repeated rows for the same
    content identity collapse to one expected object, but a duplicate that
    disagrees on size or media type is an integrity failure, not a dedup.
    """
    hydrated: dict[str, ExpectedObject] = {}
    for row in rows:
        try:
            expected = _hydrate_expected_object(row)
        except ValueError as cause:
            raise _reject_object_set() from cause
        seen = hydrated.get(expected.content_digest.hexadecimal)
        if seen is None:
            hydrated[expected.content_digest.hexadecimal] = expected
        elif (seen.size_bytes, seen.media_type.value) != (
            expected.size_bytes,
            expected.media_type.value,
        ):
            raise _reject_object_set()
    return tuple(hydrated.values())


async def _read_table_counts(
    connection: AsyncConnection,
    table_names: tuple[str, ...] = SNAPSHOT_LOCK_ORDER,
) -> Mapping[str, int]:
    """Read the requested closed canonical table counts over one connection."""
    counts: dict[str, int] = {}
    for table_name in table_names:
        count = (
            await connection.execute(table_count_statement(SOURCE_STORE_TABLES[table_name]))
        ).scalar_one()
        counts[table_name] = int(count)
    return MappingProxyType(counts)


class PostgresqlBackupSnapshotStore:
    """Quiesced exported-snapshot store over the canonical PostgreSQL baseline.

    Takes the composition-owned :class:`AsyncEngine` and opens no connection at
    construction. ``open_quiesced_snapshot`` mutates nothing: it takes the twenty
    share locks, exports the snapshot token, reads every piece of evidence from
    that one snapshot and rolls back on exit, success or failure, releasing the
    locks.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def open_quiesced_snapshot(self, now: datetime) -> AsyncIterator[CanonicalBackupSnapshot]:
        async with self._engine.connect() as connection:
            # The isolation override must precede the first query so the
            # snapshot transaction begins life as REPEATABLE READ.
            await connection.execution_options(isolation_level=SNAPSHOT_ISOLATION_LEVEL)
            transaction = await connection.begin()
            try:
                yield await self._read_quiesced_snapshot(connection)
            finally:
                # Rollback on success and failure alike: nothing mutated, and
                # the rollback is what releases the twenty share locks.
                await transaction.rollback()

    async def _read_quiesced_snapshot(self, connection: AsyncConnection) -> CanonicalBackupSnapshot:
        try:
            for bound in SNAPSHOT_TRANSACTION_BOUND_STATEMENTS:
                await connection.execute(sa.text(bound))
            for lock_statement in build_share_lock_statements():
                await connection.execute(lock_statement)
            snapshot_token = str(
                (await connection.execute(_EXPORT_SNAPSHOT_STATEMENT)).scalar_one()
            )
            server_version = str((await connection.execute(_SERVER_VERSION_STATEMENT)).scalar_one())
            schema_head = str((await connection.execute(_SCHEMA_HEAD_STATEMENT)).scalar_one())
            table_counts = await _read_table_counts(connection)
            referenced_rows = (
                (await connection.execute(referenced_objects_statement())).mappings().all()
            )
            referenced_objects = hydrate_referenced_objects(referenced_rows)
        except ApplicationError:
            raise
        except Exception as cause:
            raise _map_snapshot_failure(cause) from cause
        return CanonicalBackupSnapshot(
            snapshot_token=snapshot_token,
            server_version=server_version,
            schema_head=schema_head,
            table_counts=table_counts,
            referenced_objects=referenced_objects,
        )

    async def observe_pending_writers(self) -> int:
        """Count writers still waiting for a lock on the twenty canonical tables.

        A non-zero count means a writer was blocked by the quiescing share
        locks at observation time; the caller aborts finalization (spec 9.2).
        """
        try:
            async with self._engine.connect() as connection, connection.begin():
                await apply_transaction_bounds(connection)
                count = (
                    await connection.execute(
                        pending_writer_count_statement(),
                        {"tables": list(SNAPSHOT_LOCK_ORDER)},
                    )
                ).scalar_one()
        except ApplicationError:
            raise
        except Exception as cause:
            raise _map_snapshot_failure(cause) from cause
        return int(count)


class PostgresqlRestoreTarget:
    """Pre/post-restore probes against one canonical restore target.

    Every probe is a bounded read-only transaction using the established
    ``SET LOCAL`` bounds; failures cross the boundary as the retryable
    ``canonical_recovery_dependency_unavailable`` recovery error.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def _bounded_probe(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.connect() as connection, connection.begin():
            await apply_transaction_bounds(connection)
            yield connection

    async def server_version(self) -> str:
        """Read the PostgreSQL server version of the target."""
        try:
            async with self._bounded_probe() as connection:
                version = (await connection.execute(_SERVER_VERSION_STATEMENT)).scalar_one()
        except ApplicationError:
            raise
        except Exception as cause:
            raise _map_snapshot_failure(cause) from cause
        return str(version)

    async def is_application_empty(self) -> bool:
        """True when no relation exists in the application schema and no ``alembic_version``.

        Both probes are parameter-bound catalog reads; the target is empty only
        when both counts are zero (spec 11.1).
        """
        try:
            async with self._bounded_probe() as connection:
                relation_count = (
                    await connection.execute(
                        _SCHEMA_RELATION_COUNT_STATEMENT, {"schema": SOURCE_STORE_SCHEMA}
                    )
                ).scalar_one()
                alembic_count = (
                    await connection.execute(
                        _ALEMBIC_PRESENCE_COUNT_STATEMENT,
                        {"schema": _ALEMBIC_SCHEMA, "table_name": _ALEMBIC_TABLE},
                    )
                ).scalar_one()
        except ApplicationError:
            raise
        except Exception as cause:
            raise _map_snapshot_failure(cause) from cause
        return int(relation_count) == 0 and int(alembic_count) == 0

    async def read_canonical_counts(
        self, table_names: tuple[str, ...] = SNAPSHOT_LOCK_ORDER
    ) -> Mapping[str, int]:
        """Read the requested manifest-version count set after restore."""
        try:
            async with self._bounded_probe() as connection:
                return await _read_table_counts(connection, table_names)
        except ApplicationError:
            raise
        except Exception as cause:
            raise _map_snapshot_failure(cause) from cause

    async def read_schema_head(self) -> str | None:
        """Read the Alembic head revision, or ``None`` when it cannot exist.

        ``None`` covers both an absent ``alembic_version`` table — an admitted
        empty restore target carries no baseline at all — and a present table
        with no row. The presence check is a parameter-bound catalog read so a
        missing table never surfaces as a driver failure.
        """
        try:
            async with self._bounded_probe() as connection:
                alembic_count = (
                    await connection.execute(
                        _ALEMBIC_PRESENCE_COUNT_STATEMENT,
                        {"schema": _ALEMBIC_SCHEMA, "table_name": _ALEMBIC_TABLE},
                    )
                ).scalar_one()
                if int(alembic_count) == 0:
                    return None
                head = (await connection.execute(_SCHEMA_HEAD_STATEMENT)).scalar_one_or_none()
        except ApplicationError:
            raise
        except Exception as cause:
            raise _map_snapshot_failure(cause) from cause
        return None if head is None else str(head)

    async def read_current_pointer_resolution(self) -> int:
        """Count unresolved current pointers; a consistent restore expects zero."""
        try:
            async with self._bounded_probe() as connection:
                unresolved = (
                    await connection.execute(current_pointer_resolution_statement())
                ).scalar_one()
        except ApplicationError:
            raise
        except Exception as cause:
            raise _map_snapshot_failure(cause) from cause
        return int(unresolved)
