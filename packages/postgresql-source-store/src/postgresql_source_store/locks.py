"""Stable transaction-scoped advisory lock derivation.

Lock keys are derived deterministically from the request identity: a signed
big-endian interpretation of the first four SHA-256 bytes of the exact lock
material. The namespaces here keep the idempotency lock family and the source
lock family from colliding; further reserved namespaces (for example the
identity bootstrap family) build on the same shared derivation and statement
helpers. Only transaction-level two-integer advisory locks are produced
(``pg_advisory_xact_lock`` with bound parameters); session-level advisory
locks are prohibited because they survive transaction failure and must be
released manually. Hash collisions may serialise unrelated requests but can
never merge them: every query still compares complete workspace, key, event
and source values.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import TextClause
from sqlalchemy.sql import Select

from personal_os.sources.commands import IdempotencyKey
from postgresql_source_store.tables import workspace_policy_state

#: Idempotency lock namespace (``"SVCI"`` ASCII) for replay-identity locks.
IDEMPOTENCY_LOCK_NAMESPACE: Final[int] = 0x53564349

#: Source lock namespace (``"SVCS"`` ASCII) for source-row locks.
SOURCE_LOCK_NAMESPACE: Final[int] = 0x53564353

_ADVISORY_LOCK_SQL: Final[str] = "SELECT pg_advisory_xact_lock(:namespace, :derived_key)"


def signed_first_sha256_word(material: bytes) -> int:
    """Interpret the first four SHA-256 bytes as signed big-endian 32-bit.

    Shared by every advisory-lock family in this package so that all
    namespaces derive keys through the identical frozen algorithm.
    """
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big", signed=True)


def idempotency_lock_key(workspace_id: UUID, key: IdempotencyKey) -> int:
    """Derive the transaction lock key for a workspace-scoped replay identity.

    The material is the workspace UUID bytes, a NUL separator that cannot
    appear in the ASCII key, and the exact key bytes.
    """
    material = workspace_id.bytes + b"\x00" + key.value.encode("ascii")
    return signed_first_sha256_word(material)


def source_lock_key(source_id: UUID) -> int:
    """Derive the transaction lock key for a source.

    Source UUIDs are globally unique primary keys, so the lock material is
    global rather than workspace-scoped.
    """
    return signed_first_sha256_word(source_id.bytes)


def advisory_xact_lock_statement(namespace: int, derived_key: int) -> TextClause:
    """Build the transaction-scoped two-integer advisory lock statement.

    Both integers are bound parameters; the lock releases automatically on
    commit, rollback, cancellation or connection loss. Shared by every
    advisory-lock family in this package.
    """
    return sa.text(_ADVISORY_LOCK_SQL).bindparams(
        sa.bindparam("namespace", namespace),
        sa.bindparam("derived_key", derived_key),
    )


def idempotency_lock_statement(workspace_id: UUID, key: IdempotencyKey) -> TextClause:
    """Build the idempotency advisory lock statement for a replay identity."""
    return advisory_xact_lock_statement(
        IDEMPOTENCY_LOCK_NAMESPACE,
        idempotency_lock_key(workspace_id, key),
    )


def source_lock_statement(source_id: UUID) -> TextClause:
    """Build the source advisory lock statement for one source."""
    return advisory_xact_lock_statement(
        SOURCE_LOCK_NAMESPACE,
        source_lock_key(source_id),
    )


def policy_state_lock_statement(workspace_id: UUID) -> Select[tuple[Any, ...]]:
    """Build the ``FOR UPDATE`` lock of the workspace policy-state row.

    The frozen global row-lock order is: publication idempotency advisory
    lock, then this ``workspace_policy_state`` row, then the source advisory
    lock / source rows. Policy publication takes its own idempotency advisory
    lock before this row and never acquires source rows, and reconciliation
    never holds this row while acquiring source rows, so no inverse order
    exists anywhere. Source-store enforcement acquires this row between the
    replay recheck and the source advisory lock (spec 14).
    """

    return (
        sa.select(
            workspace_policy_state.c.active_policy_revision_id,
            workspace_policy_state.c.active_revision_number,
        )
        .where(workspace_policy_state.c.workspace_id == workspace_id)
        .with_for_update()
    )
