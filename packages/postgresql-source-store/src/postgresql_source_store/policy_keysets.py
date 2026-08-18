"""Exclusion-policy keyset persistence: immutable envelopes over PostgreSQL.

:class:`PostgresqlPolicyKeysetStore` implements the durable
:class:`~personal_os.exclusion_policy.ports.PolicyKeysetStore` port over
the migrated public-key history (spec 8.6/13). ``persist_keyset`` runs one
``READ COMMITTED`` transaction: the ``(workspace_id, keyset_revision)``
row is locked first, an exact replay of the same keyset identity with the
same canonical-payload hash acknowledges the existing row without
mutation, and otherwise the store appends the immutable graph — the public
signing-key rows it introduces (verified for workspace and key bytes when
already present), the keyset envelope whose ``payload_sha256`` is derived
from the canonical bytes rather than trusted from the caller, and its
cross-signature rows. Divergent replays and foreign key material fail
closed as the public ``internal_error``; nothing is ever overwritten, and
the Task 3 append-only triggers remain the final guard.

``load_latest_keyset`` returns the newest envelope with its cross-signature
rows and the signing keys those rows evidence (the full key membership with
its current/staged/retired states is declared by the canonical payload the
Task 5 lifecycle parses), or ``None`` before the first initialization.
Driver failures reuse the shared safe classifier, bounded policy retry and
unknown-outcome mapping of
:mod:`postgresql_source_store.policy_drafts`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.ports import (
    PersistedPolicyKeyset,
    PolicyKeysetEnvelope,
    PolicyKeysetRecord,
    PolicyKeysetSignatureRecord,
    PolicySigningKeyRecord,
)
from personal_os.exclusion_policy.signatures import (
    SIGNATURE_ALGORITHM,
    compute_payload_sha256_hex,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.policy_drafts import (
    PolicyDatabaseRetryPolicy,
)
from postgresql_source_store.tables import (
    policy_keyset_signatures,
    policy_keysets,
    policy_signing_keys,
)

#: One row of a keyset/key/signature read: a SQLAlchemy row mapping from
#: the adapter's ``.mappings()`` results or an equivalent mapping in tests.
type _MappedRow = RowMapping | Mapping[str, Any]


def build_signing_key_values(
    key: PolicySigningKeyRecord,
    *,
    workspace_id: UUID,
    introduced_keyset_revision: int,
    occurred_at: datetime,
) -> dict[str, Any]:
    """Build one public ``policy_signing_keys`` row's insert values.

    Only public metadata is stored: the database identity, the workspace,
    the pinned algorithm, the raw public bytes and the keyset revision that
    introduced the key. Private keys never enter PostgreSQL (spec 13.1).
    """

    return {
        "signing_key_id": key.signing_key_id,
        "workspace_id": workspace_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "public_key_bytes": key.public_key_bytes,
        "introduced_keyset_revision": introduced_keyset_revision,
        "created_at": occurred_at,
    }


def build_keyset_values(envelope: PolicyKeysetEnvelope, *, occurred_at: datetime) -> dict[str, Any]:
    """Build one ``policy_keysets`` envelope row's insert values.

    ``payload_sha256`` is derived from the canonical payload bytes inside
    the adapter; the caller never supplies a hash, so a persisted row can
    never disagree with its own bytes.
    """

    return {
        "policy_keyset_id": envelope.policy_keyset_id,
        "workspace_id": envelope.workspace_id,
        "keyset_revision": envelope.keyset_revision,
        "parent_keyset_revision": envelope.parent_keyset_revision,
        "canonical_payload_bytes": envelope.canonical_payload_bytes,
        "payload_sha256": compute_payload_sha256_hex(envelope.canonical_payload_bytes),
        "created_by_user_id": envelope.created_by_user_id,
        "created_at": occurred_at,
    }


def build_keyset_signature_values(
    policy_keyset_id: UUID, signature: PolicyKeysetSignatureRecord
) -> dict[str, Any]:
    """Build one ``policy_keyset_signatures`` row's insert values."""

    return {
        "policy_keyset_id": policy_keyset_id,
        "signing_key_id": signature.signing_key_id,
        "signature_bytes": signature.signature_bytes,
    }


def hydrate_policy_keyset(
    keyset_row: _MappedRow,
    key_rows: Sequence[_MappedRow],
    signature_rows: Sequence[_MappedRow],
) -> PolicyKeysetRecord:
    """Build the immutable keyset read model from mapped rows.

    A signing-key row belonging to another workspace can never back this
    workspace's signatures, so hydration fails closed on the workspace
    mismatch exactly like the frozen value contracts do.
    """

    workspace_id = keyset_row["workspace_id"]
    keys: list[PolicySigningKeyRecord] = []
    for row in key_rows:
        if row["workspace_id"] != workspace_id:
            raise ValueError("signing key row belongs to a foreign workspace")
        keys.append(
            PolicySigningKeyRecord(
                signing_key_id=row["signing_key_id"],
                public_key_bytes=row["public_key_bytes"],
            )
        )
    signatures = tuple(
        PolicyKeysetSignatureRecord(
            signing_key_id=row["signing_key_id"],
            signature_bytes=row["signature_bytes"],
        )
        for row in signature_rows
    )
    return PolicyKeysetRecord(
        policy_keyset_id=keyset_row["policy_keyset_id"],
        workspace_id=workspace_id,
        keyset_revision=int(keyset_row["keyset_revision"]),
        parent_keyset_revision=keyset_row["parent_keyset_revision"],
        canonical_payload_bytes=keyset_row["canonical_payload_bytes"],
        payload_sha256=keyset_row["payload_sha256"],
        keys=tuple(keys),
        signatures=signatures,
        created_by_user_id=keyset_row["created_by_user_id"],
        created_at=keyset_row["created_at"],
    )


def classify_keyset_replay(
    keyset_row: _MappedRow, policy_keyset_id: UUID, payload_sha256: str
) -> bool:
    """Classify an existing keyset row as an exact replay or a divergence.

    Only the identical keyset identity together with the identical payload
    hash is a replay; the same revision under another identity or bytes is
    an integrity divergence the caller must reject without mutating
    history.
    """

    identity_matches: bool = keyset_row["policy_keyset_id"] == policy_keyset_id
    hash_matches: bool = keyset_row["payload_sha256"] == payload_sha256
    return identity_matches and hash_matches


def _keyset_revision_lock_statement(
    workspace_id: UUID, keyset_revision: int
) -> sa.Select[tuple[Any, ...]]:
    """Build the ``FOR UPDATE`` lock on one workspace keyset revision."""

    return (
        sa.select(
            policy_keysets.c.policy_keyset_id,
            policy_keysets.c.payload_sha256,
            policy_keysets.c.created_at,
        )
        .where(
            policy_keysets.c.workspace_id == workspace_id,
            policy_keysets.c.keyset_revision == keyset_revision,
        )
        .with_for_update()
    )


def _signing_key_insert_statement(values: Mapping[str, Any]) -> postgresql.dml.Insert:
    """Build the insert-once public signing-key statement keyed by identity."""

    statement = postgresql.insert(policy_signing_keys).values(**values)
    return statement.on_conflict_do_nothing(index_elements=[policy_signing_keys.c.signing_key_id])


class PostgresqlPolicyKeysetStore:
    """Durable keyset envelope store over the canonical baseline.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. ``persist_keyset`` appends one immutable
    revision per transaction with the exact-replay acknowledgement; the
    Task 5 key-lifecycle commands own which cross-signed envelope to hand
    over and the lifecycle audit rows that accompany it.
    """

    def __init__(
        self, engine: AsyncEngine, *, retry: PolicyDatabaseRetryPolicy | None = None
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else PolicyDatabaseRetryPolicy()

    async def persist_keyset(
        self, envelope: PolicyKeysetEnvelope, context: DiagnosticContext
    ) -> PersistedPolicyKeyset:
        return await self._retry.run(
            lambda _attempt: self._persist_keyset_once(envelope, context),
            recover=lambda: self._recover_persisted_keyset(envelope),
        )

    async def load_latest_keyset(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyKeysetRecord | None:
        return await self._retry.run(
            lambda _attempt: self._load_latest_keyset_once(workspace_id, context)
        )

    async def _persist_keyset_once(
        self, envelope: PolicyKeysetEnvelope, context: DiagnosticContext
    ) -> PersistedPolicyKeyset:
        payload_sha256 = compute_payload_sha256_hex(envelope.canonical_payload_bytes)
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            existing = await self._select_locked_keyset_revision(
                connection, envelope.workspace_id, envelope.keyset_revision
            )
            if existing is not None:
                if not classify_keyset_replay(existing, envelope.policy_keyset_id, payload_sha256):
                    raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
                return PersistedPolicyKeyset(
                    policy_keyset_id=envelope.policy_keyset_id,
                    workspace_id=envelope.workspace_id,
                    keyset_revision=envelope.keyset_revision,
                    payload_sha256=payload_sha256,
                    created_at=existing["created_at"],
                    is_replay=True,
                )
            occurred_at = await self._select_now(connection)
            for key in envelope.keys:
                await self._ensure_signing_key(
                    connection, key, envelope.workspace_id, envelope.keyset_revision, occurred_at
                )
            await connection.execute(
                sa.insert(policy_keysets).values(
                    **build_keyset_values(envelope, occurred_at=occurred_at)
                )
            )
            if envelope.signatures:
                await connection.execute(
                    sa.insert(policy_keyset_signatures).values(
                        [
                            build_keyset_signature_values(envelope.policy_keyset_id, signature)
                            for signature in envelope.signatures
                        ]
                    )
                )
            return PersistedPolicyKeyset(
                policy_keyset_id=envelope.policy_keyset_id,
                workspace_id=envelope.workspace_id,
                keyset_revision=envelope.keyset_revision,
                payload_sha256=payload_sha256,
                created_at=occurred_at,
                is_replay=False,
            )

    async def _recover_persisted_keyset(
        self, envelope: PolicyKeysetEnvelope
    ) -> PersistedPolicyKeyset | None:
        """Prove or disprove that an uncertain keyset append landed."""

        payload_sha256 = compute_payload_sha256_hex(envelope.canonical_payload_bytes)
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._select_keyset_revision(
                connection, envelope.workspace_id, envelope.keyset_revision
            )
        if row is None or not classify_keyset_replay(
            row, envelope.policy_keyset_id, payload_sha256
        ):
            return None
        return PersistedPolicyKeyset(
            policy_keyset_id=envelope.policy_keyset_id,
            workspace_id=envelope.workspace_id,
            keyset_revision=envelope.keyset_revision,
            payload_sha256=payload_sha256,
            created_at=row["created_at"],
            is_replay=True,
        )

    async def _load_latest_keyset_once(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyKeysetRecord | None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            keyset_result = await connection.execute(
                sa.select(
                    policy_keysets.c.policy_keyset_id,
                    policy_keysets.c.workspace_id,
                    policy_keysets.c.keyset_revision,
                    policy_keysets.c.parent_keyset_revision,
                    policy_keysets.c.canonical_payload_bytes,
                    policy_keysets.c.payload_sha256,
                    policy_keysets.c.created_by_user_id,
                    policy_keysets.c.created_at,
                )
                .where(policy_keysets.c.workspace_id == workspace_id)
                .order_by(policy_keysets.c.keyset_revision.desc())
                .limit(1)
            )
            keyset_row = keyset_result.mappings().first()
            if keyset_row is None:
                return None
            keyset_id = keyset_row["policy_keyset_id"]
            key_rows = await self._select_keyset_keys(connection, keyset_id)
            signature_rows = await self._select_keyset_signatures(connection, keyset_id)
        return hydrate_policy_keyset(keyset_row, key_rows, signature_rows)

    @staticmethod
    async def _ensure_signing_key(
        connection: AsyncConnection,
        key: PolicySigningKeyRecord,
        workspace_id: UUID,
        introduced_keyset_revision: int,
        occurred_at: datetime,
    ) -> None:
        """Append one public signing-key row, verifying an existing identity.

        ``ON CONFLICT DO NOTHING`` keeps the first row immutable; the
        follow-up lookup then proves the existing identity belongs to the
        same workspace with the same public bytes. A different workspace or
        key material under the same identity is corruption.
        """

        await connection.execute(
            _signing_key_insert_statement(
                build_signing_key_values(
                    key,
                    workspace_id=workspace_id,
                    introduced_keyset_revision=introduced_keyset_revision,
                    occurred_at=occurred_at,
                )
            )
        )
        result = await connection.execute(
            sa.select(
                policy_signing_keys.c.workspace_id,
                policy_signing_keys.c.public_key_bytes,
            ).where(policy_signing_keys.c.signing_key_id == key.signing_key_id)
        )
        row = result.mappings().first()
        if (
            row is None
            or row["workspace_id"] != workspace_id
            or row["public_key_bytes"] != key.public_key_bytes
        ):
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)

    @staticmethod
    async def _select_locked_keyset_revision(
        connection: AsyncConnection, workspace_id: UUID, keyset_revision: int
    ) -> _MappedRow | None:
        result = await connection.execute(
            _keyset_revision_lock_statement(workspace_id, keyset_revision)
        )
        row: _MappedRow | None = result.mappings().first()
        return row

    @staticmethod
    async def _select_keyset_revision(
        connection: AsyncConnection, workspace_id: UUID, keyset_revision: int
    ) -> _MappedRow | None:
        result = await connection.execute(
            sa.select(
                policy_keysets.c.policy_keyset_id,
                policy_keysets.c.payload_sha256,
                policy_keysets.c.created_at,
            ).where(
                policy_keysets.c.workspace_id == workspace_id,
                policy_keysets.c.keyset_revision == keyset_revision,
            )
        )
        return result.mappings().first()

    @staticmethod
    async def _select_keyset_keys(
        connection: AsyncConnection, policy_keyset_id: UUID
    ) -> list[_MappedRow]:
        result = await connection.execute(
            sa.select(
                policy_signing_keys.c.signing_key_id,
                policy_signing_keys.c.workspace_id,
                policy_signing_keys.c.public_key_bytes,
            )
            .select_from(policy_signing_keys)
            .join(
                policy_keyset_signatures,
                policy_keyset_signatures.c.signing_key_id == policy_signing_keys.c.signing_key_id,
            )
            .where(policy_keyset_signatures.c.policy_keyset_id == policy_keyset_id)
            .order_by(policy_signing_keys.c.signing_key_id)
        )
        return list(result.mappings().all())

    @staticmethod
    async def _select_keyset_signatures(
        connection: AsyncConnection, policy_keyset_id: UUID
    ) -> list[_MappedRow]:
        result = await connection.execute(
            sa.select(
                policy_keyset_signatures.c.signing_key_id,
                policy_keyset_signatures.c.signature_bytes,
            )
            .where(policy_keyset_signatures.c.policy_keyset_id == policy_keyset_id)
            .order_by(policy_keyset_signatures.c.signing_key_id)
        )
        return list(result.mappings().all())

    @staticmethod
    async def _select_now(connection: AsyncConnection) -> datetime:
        """Read the transaction-stable timestamp shared by every written row."""

        result = await connection.execute(sa.text("SELECT now()"))
        occurred_at = result.scalar_one()
        if not isinstance(occurred_at, datetime):  # pragma: no cover - driver contract
            raise TypeError("SELECT now() did not return a datetime")
        return occurred_at
