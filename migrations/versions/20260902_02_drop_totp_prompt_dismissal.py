"""Drop the retired initial-TOTP-offer dismissal timestamp.

Revision ID: 20260902_02
Revises: 20260902_01
Create Date: 2026-09-02

The 2026-09-02 initial TOTP offer retirement removes the obsolete
"dismiss the initial offer" contract end to end: enrollment knows only the
explicit ``start`` action, so ``knowledge.user_credentials`` no longer needs
the nullable ``totp_prompt_dismissed_at`` dismissal timestamp. This revision
drops the timestamp and rebuilds ``ck_user_credentials__timestamps`` as the
reduced invariant ``updated_at >= created_at AND password_changed_at >=
created_at``; every other column, key, index and constraint of the
credentials table is untouched.

The downgrade reverses that exact order: it drops the reduced CHECK, adds
the nullable ``TIMESTAMP WITH TIME ZONE`` column back and recreates the
original clause exactly as the ``20260816_01`` authentication revision wrote
it. The restored clause permits a null dismissal timestamp — the only value
the downgrade can reintroduce, because the column returns empty. The
revision issues constraint and column DDL only: it never selects, copies or
writes credential rows, so no credential material (least of all a TOTP
secret) can transit the migration. Destructive authorization is owned by
the shared ``env.py`` downgrade gate, not by this revision.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260902_02"
down_revision: str | None = "20260902_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"
TABLE_NAME: Final[str] = "user_credentials"
RETIRED_COLUMN_NAME: Final[str] = "totp_prompt_dismissed_at"
TIMESTAMPS_CONSTRAINT_NAME: Final[str] = "ck_user_credentials__timestamps"

#: The reduced timestamp invariant once the dismissal timestamp is gone.
REDUCED_TIMESTAMPS_CHECK: Final[str] = (
    "updated_at >= created_at AND password_changed_at >= created_at"
)

#: The original ``20260816_01`` clause restored by the downgrade; it admits a
#: null dismissal timestamp, the only value the restored empty column holds.
RETIRED_TIMESTAMPS_CHECK: Final[str] = (
    "updated_at >= created_at "
    "AND password_changed_at >= created_at "
    "AND (totp_prompt_dismissed_at IS NULL OR totp_prompt_dismissed_at >= created_at)"
)


def upgrade() -> None:
    """Drop the dismissal timestamp and rebuild the reduced timestamp CHECK."""

    op.drop_constraint(
        TIMESTAMPS_CONSTRAINT_NAME,
        TABLE_NAME,
        schema=SCHEMA_NAME,
        type_="check",
    )
    op.drop_column(TABLE_NAME, RETIRED_COLUMN_NAME, schema=SCHEMA_NAME)
    op.create_check_constraint(
        TIMESTAMPS_CONSTRAINT_NAME,
        TABLE_NAME,
        REDUCED_TIMESTAMPS_CHECK,
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    """Restore the nullable dismissal timestamp and the original timestamp CHECK."""

    op.drop_constraint(
        TIMESTAMPS_CONSTRAINT_NAME,
        TABLE_NAME,
        schema=SCHEMA_NAME,
        type_="check",
    )
    op.add_column(
        TABLE_NAME,
        sa.Column(RETIRED_COLUMN_NAME, sa.TIMESTAMP(timezone=True), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.create_check_constraint(
        TIMESTAMPS_CONSTRAINT_NAME,
        TABLE_NAME,
        RETIRED_TIMESTAMPS_CHECK,
        schema=SCHEMA_NAME,
    )
