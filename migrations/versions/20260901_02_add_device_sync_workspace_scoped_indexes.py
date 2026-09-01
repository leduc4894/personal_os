"""Add the device-sync workspace-scoped pull and tombstone-restore indexes.

Revision ID: 20260901_02
Revises: 20260901_01
Create Date: 2026-09-01

The device cursor pull pages one workspace's events through the
credential-scoped ``workspace_id`` equality plus the ordered
``event_sequence`` range, and the pull page's restore hydration joins
tombstones by ``restore_event_id``. At one workspace the global
``uq_sync_events__event_sequence`` unique index answered the pull (and the
restore join stayed bounded by the pinned acceptance fixture size), but at
multi-workspace scale the global sequence index drags every workspace's
pull page across every other workspace's rows, and the restore join owns
no index at all (BACKLOG 2026-08-26 device-sync).

This revision adds exactly the two shipped scale indexes the query-plan
acceptance pins: ``ix_sync_events__workspace_event_sequence`` on
``sync_events (workspace_id, event_sequence)`` — the workspace-prefixed
driving access every workspace-scoped pull wants — and the partial
``ix_source_tombstones__restore_event_id`` on
``source_tombstones (restore_event_id) WHERE restore_event_id IS NOT NULL``
covering the restore-side hydration lookup (the delete side keeps its
existing unique index). No column, constraint or row changes.

The downgrade drops both indexes in exact reverse creation order; it
destroys no recorded evidence, so beyond the environment-level downgrade
authorization it needs no destructive gate of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260901_02"
down_revision: str | None = "20260901_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"


def upgrade() -> None:
    """Create the workspace-scoped pull and tombstone-restore indexes."""

    op.create_index(
        "ix_sync_events__workspace_event_sequence",
        "sync_events",
        ["workspace_id", "event_sequence"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_source_tombstones__restore_event_id",
        "source_tombstones",
        ["restore_event_id"],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("restore_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop both scale indexes in exact reverse creation order."""

    op.drop_index(
        "ix_source_tombstones__restore_event_id",
        table_name="source_tombstones",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "ix_sync_events__workspace_event_sequence",
        table_name="sync_events",
        schema=SCHEMA_NAME,
    )
