"""Defer the multipart provider identity columns to the post-create write.

Revision ID: 20260828_03
Revises: 20260828_02
Create Date: 2026-08-28

Spec 6.1 (Creation and resume) fixes the creation order: the service
"persists the session before invoking R2 create multipart, records enough
durable recovery state to retry an ambiguous create, and then stores the
provider ID". The ``20260828_01`` schema required ``staging_key`` and
``provider_upload_id`` at insert time, which forced every caller to mint
the provider upload before any durable trace existed — leaving a crash
between the provider create and the session insert with an orphaned
staging upload and no recovery state at all.

This revision makes exactly those two columns nullable so the canonical
session row can be reserved first and the provider identity — minted by
the staging provider and ambiguous on a lost response — can land
afterwards through the session store's fenced compare-and-set write: the
write stores the identity once, replays the identical identity
idempotently, and rejects a divergent identity with the closed
provider-state-invalid token. No constraint, index or other column
changes; the columns remain private provider identity text.

The downgrade restores the original NOT NULL shapes and refuses — with
the closed ``multipart_provider_identity_downgrade_has_pending_rows``
token — while any session row still carries a NULL identity, because
recreating the NOT NULL constraints over such rows would fail and
discarding reserved sessions is never an implicit downgrade behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260828_03"
down_revision: str | None = "20260828_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

SESSION_TABLE_NAME: Final[str] = "multipart_uploads"

#: The two private provider identity columns whose values the service can
#: only obtain after the provider create call.
DEFERRED_IDENTITY_COLUMNS: Final[tuple[str, ...]] = ("staging_key", "provider_upload_id")

_DOWNGRADE_PENDING_COUNT_SQL: Final[str] = (
    f"SELECT count(*) FROM {SCHEMA_NAME}.{SESSION_TABLE_NAME} "
    "WHERE staging_key IS NULL OR provider_upload_id IS NULL"
)

_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = (
    "multipart_provider_identity_downgrade_has_pending_rows"
)


def _set_identity_nullability(nullable: bool) -> None:
    for column_name in DEFERRED_IDENTITY_COLUMNS:
        op.alter_column(
            SESSION_TABLE_NAME,
            column_name,
            existing_type=sa.Text(),
            nullable=nullable,
            schema=SCHEMA_NAME,
        )


def upgrade() -> None:
    """Allow the canonical session row to precede the provider create."""

    _set_identity_nullability(nullable=True)


def downgrade() -> None:
    """Restore the immediate-identity shape once no pending rows remain."""

    bind = op.get_bind()
    pending_row_count = int(
        bind.execute(sa.text(_DOWNGRADE_PENDING_COUNT_SQL)).scalar_one()
    )
    if pending_row_count > 0:
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)
    _set_identity_nullability(nullable=False)
