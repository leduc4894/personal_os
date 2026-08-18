"""Repository-internal signed policy seeding for smoke and test harnesses.

Spec 14 requires the internal smoke fixtures to explicitly publish a signed
empty policy before performing canonical content operations, and every test
harness that publishes or reads sources through the guarded services needs a
workspace whose active pointer names a genuinely signed revision. This module
owns that one deterministic seeding path: a process-local Ed25519 trust
anchor, the domain snapshot builder and signing-domain helpers for the
canonical payload bytes, a real signature over the domain-separated message,
and the schema-qualified inserts of the signing-key row, the immutable
revision row and the guarded active-pointer swap. Nothing here is an Admin
publication: it is the fixed starting state of a disposable environment, and
the key material is generated in-process, never read from or written to a
secret store.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.exclusion_policy.contracts import (
    ExclusionPolicyRevision,
    ExclusionRule,
)
from personal_os.exclusion_policy.signatures import (
    SNAPSHOT_PAYLOAD_CONTRACT,
    SNAPSHOT_SIGNING_DOMAIN,
    build_signed_message,
    build_snapshot_payload,
    compute_payload_sha256_hex,
)
from postgresql_source_store.tables import (
    policy_drafts,
    policy_previews,
    policy_signing_keys,
    source_policies,
    workspace_policy_state,
)

#: The keyset revision the seeded trust-anchor row is introduced by.
_SEEDED_KEYSET_REVISION: Final[int] = 1

#: The pinned signing-key algorithm literal of the seeded row.
_SIGNING_KEY_ALGORITHM: Final[str] = "Ed25519"

#: Seeded ready previews expire after the closed fifteen-minute window.
_PREVIEW_EXPIRY: Final[timedelta] = timedelta(minutes=15)

_KEY_LOCK = threading.Lock()
_KEYPAIRS_BY_WORKSPACE: dict[UUID, tuple[object, bytes]] = {}


def _seed_keypair(workspace_id: UUID | None = None) -> tuple[object, bytes]:
    """Return the seeding Ed25519 keypair, one per workspace.

    ``policy_signing_keys.public_key_bytes`` carries a global unique
    constraint, so each seeded workspace uses its own process-local anchor;
    ``workspace_id=None`` requests the shared default pair for callers that
    only need signing material (snapshot building).
    """

    with _KEY_LOCK:
        if workspace_id is not None:
            existing = _KEYPAIRS_BY_WORKSPACE.get(workspace_id)
            if existing is not None:
                return existing
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        seed = uuid4().bytes + uuid4().bytes
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        public_key_bytes = private_key.public_key().public_bytes_raw()
        pair = (private_key, public_key_bytes)
        if workspace_id is not None:
            _KEYPAIRS_BY_WORKSPACE[workspace_id] = pair
        return pair


@dataclass(frozen=True, slots=True)
class SeededSignedPolicy:
    """The identities of one seeded signed revision."""

    workspace_id: UUID
    signing_key_id: UUID
    policy_revision_id: UUID
    revision_number: int


def build_seeded_snapshot(
    *,
    workspace_id: UUID,
    policy_revision_id: UUID,
    revision_number: int,
    parent_policy_revision_id: UUID | None,
    rules: tuple[ExclusionRule, ...] = (),
    published_at: datetime | None = None,
    signing_key: object | None = None,
) -> tuple[bytes, str, bytes]:
    """Build the canonical payload, its SHA-256 and the real signature.

    Returns ``(payload_bytes, payload_sha256_hex, signature_bytes)`` over the
    domain-separated message signed with the process-local seeding key.
    """

    revision = ExclusionPolicyRevision(
        policy_revision_id=policy_revision_id,
        workspace_id=workspace_id,
        revision_number=revision_number,
        rules=rules,
    )
    payload_bytes = build_snapshot_payload(
        revision,
        parent_policy_revision_id=parent_policy_revision_id,
        published_at=published_at if published_at is not None else datetime.now(UTC),
    )
    message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload_bytes)
    if signing_key is None:
        signing_key, _ = _seed_keypair()
    signature_bytes = signing_key.sign(message)  # type: ignore[attr-defined]
    return payload_bytes, compute_payload_sha256_hex(payload_bytes), signature_bytes


async def _insert_bound_preview_row(
    connection: Any,
    workspace_id: UUID,
    base_revision_id: UUID | None,
    *,
    created_by_user_id: UUID,
) -> UUID:
    """Insert one ready preview row the seeded revision binds to.

    ``source_policies`` carries a unique foreign key into ``policy_previews``
    (one revision per preview), so every seeded revision binds to its own
    ready row over the workspace's existing draft.
    """

    draft_id = (
        (
            await connection.execute(
                sa.select(policy_drafts.c.policy_draft_id).where(
                    policy_drafts.c.workspace_id == workspace_id
                )
            )
        )
        .scalars()
        .first()
    )
    if draft_id is None:
        # Harness-seeded workspaces may carry no draft graph yet; create the
        # minimal empty draft the preview row binds to.
        draft_id = uuid7()
        now = datetime.now(UTC)
        await connection.execute(
            sa.insert(policy_drafts).values(
                policy_draft_id=draft_id,
                workspace_id=workspace_id,
                draft_version=1,
                base_policy_revision_id=base_revision_id,
                created_by_user_id=created_by_user_id,
                created_at=now,
                updated_at=now,
            )
        )
    preview_id = uuid7()
    await connection.execute(
        sa.insert(policy_previews).values(
            policy_preview_id=preview_id,
            workspace_id=workspace_id,
            policy_draft_id=draft_id,
            draft_version=1,
            draft_sha256=_sha256_hex(b"seeded-draft"),
            base_policy_revision_id=base_revision_id,
            source_checkpoint_event_sequence=0,
            state="ready",
            impact_digest=_sha256_hex(b"seeded-impact"),
            created_by_user_id=created_by_user_id,
            created_at=datetime.now(UTC),
            ready_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + _PREVIEW_EXPIRY,
        )
    )
    return preview_id


def _sha256_hex(value: bytes) -> str:
    from hashlib import sha256

    return sha256(value).hexdigest()


async def seed_signed_policy(
    engine: AsyncEngine,
    *,
    workspace_id: UUID,
    published_by_user_id: UUID,
    rules: tuple[ExclusionRule, ...] = (),
) -> SeededSignedPolicy:
    """Seed one signed policy revision and point the workspace at it.

    Allocates the signing-key row and the immutable revision row for the
    workspace's next revision number, then swaps the active pointer through
    the guarded transition. The snapshot is genuinely signed by the
    process-local trust anchor, so backend enforcement verifies it exactly
    like an Admin-published revision.
    """

    signing_private_key, public_key_bytes = _seed_keypair(workspace_id)
    signing_key_id = uuid7()
    policy_revision_id = uuid7()
    async with engine.begin() as connection:
        state_row = (
            await connection.execute(
                sa.select(
                    workspace_policy_state.c.active_policy_revision_id,
                    workspace_policy_state.c.active_revision_number,
                ).where(workspace_policy_state.c.workspace_id == workspace_id)
            )
        ).one_or_none()
        if state_row is None:
            raise RuntimeError("workspace policy state row is missing")
        parent_revision_id = state_row[0]
        active_number = int(state_row[1])
        revision_number = active_number + 1
        occurred_at = datetime.now(UTC)
        payload_bytes, payload_sha256, signature_bytes = build_seeded_snapshot(
            workspace_id=workspace_id,
            policy_revision_id=policy_revision_id,
            revision_number=revision_number,
            parent_policy_revision_id=parent_revision_id,
            rules=rules,
            published_at=occurred_at,
            signing_key=signing_private_key,
        )
        existing_key_id = (
            (
                await connection.execute(
                    sa.select(policy_signing_keys.c.signing_key_id).where(
                        policy_signing_keys.c.workspace_id == workspace_id,
                        policy_signing_keys.c.public_key_bytes == public_key_bytes,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_key_id is None:
            await connection.execute(
                sa.insert(policy_signing_keys).values(
                    signing_key_id=signing_key_id,
                    workspace_id=workspace_id,
                    algorithm=_SIGNING_KEY_ALGORITHM,
                    public_key_bytes=public_key_bytes,
                    introduced_keyset_revision=_SEEDED_KEYSET_REVISION,
                    created_at=occurred_at,
                )
            )
        else:
            signing_key_id = existing_key_id
        preview_id = await _insert_bound_preview_row(
            connection,
            workspace_id,
            parent_revision_id,
            created_by_user_id=published_by_user_id,
        )
        await connection.execute(
            sa.insert(source_policies).values(
                policy_revision_id=policy_revision_id,
                workspace_id=workspace_id,
                revision_number=revision_number,
                parent_policy_revision_id=parent_revision_id,
                source_checkpoint_event_sequence=0,
                policy_preview_id=preview_id,
                publication_idempotency_key=f"seed-{uuid4().hex}",
                request_fingerprint=uuid4().hex + uuid4().hex,
                snapshot_contract=SNAPSHOT_PAYLOAD_CONTRACT,
                snapshot_payload_bytes=payload_bytes,
                snapshot_payload_sha256=payload_sha256,
                signing_key_id=signing_key_id,
                signature_bytes=signature_bytes,
                published_by_user_id=published_by_user_id,
                published_at=occurred_at,
            )
        )
        # The guarded swap: only a workspace whose pointer still names the
        # revision this seed chains onto (or none, for revision 1) moves.
        swapped = await connection.execute(
            sa.update(workspace_policy_state)
            .values(
                active_policy_revision_id=policy_revision_id,
                active_revision_number=revision_number,
                updated_at=occurred_at,
            )
            .where(
                workspace_policy_state.c.workspace_id == workspace_id,
                workspace_policy_state.c.active_policy_revision_id == parent_revision_id,
                workspace_policy_state.c.active_revision_number == active_number,
            )
        )
        if swapped.rowcount != 1:
            raise RuntimeError("guarded active-pointer swap affected no row")
    return SeededSignedPolicy(
        workspace_id=workspace_id,
        signing_key_id=signing_key_id,
        policy_revision_id=policy_revision_id,
        revision_number=revision_number,
    )


__all__ = [
    "SeededSignedPolicy",
    "build_seeded_snapshot",
    "seed_signed_policy",
]
