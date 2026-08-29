"""Track manifest-run client activity for the idle-expiry deadline.

Revision ID: 20260827_01
Revises: 20260826_02
Create Date: 2026-08-27

The physical Mobile matrix (2026-08-27) proved the fixed one-hour run
deadline dead-locks a device whose client went quiet mid-run: every
recovery path waits out the full hour.  This revision adds
``manifest_runs.last_client_activity_at`` (touched on every client
operation — start, page append, finalize, action read) so the store can
expire a run after five minutes without client activity, whichever comes
first against the retained one-hour hard cap.  Existing rows backfill
from ``created_at`` (their only known activity point).

Downgrade drops the column; no other object references it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_01"
down_revision: str | None = "20260826_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"
TABLE_NAME: Final[str] = "manifest_runs"
COLUMN_NAME: Final[str] = "last_client_activity_at"


def upgrade() -> None:
    op.add_column(
        TABLE_NAME,
        sa.Column(COLUMN_NAME, sa.TIMESTAMP(timezone=True), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.execute(
        f"UPDATE {SCHEMA_NAME}.{TABLE_NAME} "
        f"SET {COLUMN_NAME} = created_at "
        f"WHERE {COLUMN_NAME} IS NULL"
    )
    op.alter_column(
        TABLE_NAME,
        COLUMN_NAME,
        nullable=False,
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_column(TABLE_NAME, COLUMN_NAME, schema=SCHEMA_NAME)
