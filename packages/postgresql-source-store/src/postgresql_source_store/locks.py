"""Stable transaction-scoped advisory lock derivation.

Lock keys are derived deterministically from the request identity: a signed
big-endian interpretation of the first four SHA-256 bytes of the exact lock
material. The two namespaces keep the idempotency lock family and the source
lock family from colliding. Only transaction-level two-integer advisory locks
are produced (``pg_advisory_xact_lock`` with bound parameters); session-level
advisory locks are prohibited because they survive transaction failure and
must be released manually. Hash collisions may serialise unrelated requests
but can never merge them: every query still compares complete workspace, key,
event and source values.
"""

from __future__ import annotations

import hashlib
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import TextClause

from personal_os.sources.commands import IdempotencyKey

#: Idempotency lock namespace (``"SVCI"`` ASCII) for replay-identity locks.
IDEMPOTENCY_LOCK_NAMESPACE: Final[int] = 0x53564349

#: Source lock namespace (``"SVCS"`` ASCII) for source-row locks.
SOURCE_LOCK_NAMESPACE: Final[int] = 0x53564353

_ADVISORY_LOCK_SQL: Final[str] = "SELECT pg_advisory_xact_lock(:namespace, :derived_key)"


def _signed_first_sha256_word(material: bytes) -> int:
    """Interpret the first four SHA-256 bytes as signed big-endian 32-bit."""
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big", signed=True)


def idempotency_lock_key(workspace_id: UUID, key: IdempotencyKey) -> int:
    """Derive the transaction lock key for a workspace-scoped replay identity.

    The material is the workspace UUID bytes, a NUL separator that cannot
    appear in the ASCII key, and the exact key bytes.
    """
    material = workspace_id.bytes + b"\x00" + key.value.encode("ascii")
    return _signed_first_sha256_word(material)


def source_lock_key(source_id: UUID) -> int:
    """Derive the transaction lock key for a source.

    Source UUIDs are globally unique primary keys, so the lock material is
    global rather than workspace-scoped.
    """
    return _signed_first_sha256_word(source_id.bytes)


def _advisory_xact_lock_statement(namespace: int, derived_key: int) -> TextClause:
    """Build the transaction-scoped two-integer advisory lock statement.

    Both integers are bound parameters; the lock releases automatically on
    commit, rollback, cancellation or connection loss.
    """
    return sa.text(_ADVISORY_LOCK_SQL).bindparams(
        sa.bindparam("namespace", namespace),
        sa.bindparam("derived_key", derived_key),
    )


def idempotency_lock_statement(workspace_id: UUID, key: IdempotencyKey) -> TextClause:
    """Build the idempotency advisory lock statement for a replay identity."""
    return _advisory_xact_lock_statement(
        IDEMPOTENCY_LOCK_NAMESPACE,
        idempotency_lock_key(workspace_id, key),
    )


def source_lock_statement(source_id: UUID) -> TextClause:
    """Build the source advisory lock statement for one source."""
    return _advisory_xact_lock_statement(
        SOURCE_LOCK_NAMESPACE,
        source_lock_key(source_id),
    )
