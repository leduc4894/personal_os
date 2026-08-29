"""Record the append-time policy verdict on each manifest entry resolution.

Revision ID: 20260829_01
Revises: 20260828_04
Create Date: 2026-08-29

The unowned-upload EXCLUDED bug (BACKLOG line 64) proved that recomputing
the exclusion-policy verdict at finalize can diverge from the verdict the
append made: by finalize the raw locator evidence is gone, so a rule the
append had allowed could exclude the entry afterwards.  This revision adds
``manifest_entry_resolutions.submitted_policy_allowed`` (nullable boolean)
so the append records its policy verdict while the raw locator is still in
memory and the finalize reads it back.  No backfill: existing rows keep
NULL, which the finalize reads as "legacy row appended before this
revision" and answers with today's exact finalize-time recomputation.

Downgrade drops the column; no other object references it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_01"
down_revision: str | None = "20260828_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"
TABLE_NAME: Final[str] = "manifest_entry_resolutions"
COLUMN_NAME: Final[str] = "submitted_policy_allowed"


def upgrade() -> None:
    op.add_column(
        TABLE_NAME,
        sa.Column(COLUMN_NAME, sa.Boolean(), nullable=True),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_column(TABLE_NAME, COLUMN_NAME, schema=SCHEMA_NAME)
