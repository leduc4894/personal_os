"""Extend the closed throttle-bucket kind list with the grant-poll pacing kind.

Revision ID: 20260901_01
Revises: 20260829_01
Create Date: 2026-09-01

The 2026-08-31 multi-worker poll pacing spec amendment moves grant-poll
pacing into the durable ``knowledge.authentication_throttle_buckets`` table
under a new closed ``grant_poll`` kind, so every worker reads one
PostgreSQL-authoritative pacing state instead of a per-process clock. This
revision is the schema half only: it recreates the single
``ck_authentication_throttle_buckets__bucket_kind`` CHECK with the seventh
``grant_poll`` member. Every column, index, key and other constraint of the
buckets table is untouched.

The downgrade first deletes the ``grant_poll`` rows the pacing behavior
writes, then restores the original six-value CHECK exactly as the
``20260816_01`` authentication revision wrote it: PostgreSQL validates a
freshly added CHECK against existing rows, so leftover ``grant_poll`` rows
would abort the constraint re-creation. Rows of the six retained kinds are
untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260901_01"
down_revision: str | None = "20260829_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

TABLE_NAME: Final[str] = "authentication_throttle_buckets"
_CONSTRAINT_NAME: Final[str] = "ck_authentication_throttle_buckets__bucket_kind"

UPGRADE_KIND_LIST: Final[tuple[str, ...]] = (
    "login_username",
    "login_source",
    "grant_creation",
    "user_code_lookup",
    "totp_verification",
    "recovery_verification",
    "grant_poll",
)
DOWNGRADE_KIND_LIST: Final[tuple[str, ...]] = UPGRADE_KIND_LIST[:-1]

_GRANT_POLL_ROW_DELETE: Final[str] = (
    f"DELETE FROM {SCHEMA_NAME}.{TABLE_NAME} WHERE bucket_kind = 'grant_poll'"
)


def _kind_check(kind_list: tuple[str, ...]) -> str:
    return "bucket_kind IN (" + ", ".join(f"'{kind}'" for kind in kind_list) + ")"


def upgrade() -> None:
    """Recreate the bucket-kind CHECK with the seventh ``grant_poll`` member."""

    op.drop_constraint(
        _CONSTRAINT_NAME,
        TABLE_NAME,
        schema=SCHEMA_NAME,
        type_="check",
    )
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        TABLE_NAME,
        _kind_check(UPGRADE_KIND_LIST),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    """Delete the pacing rows, then restore the six-value bucket-kind CHECK."""

    op.execute(sa.text(_GRANT_POLL_ROW_DELETE))
    op.drop_constraint(
        _CONSTRAINT_NAME,
        TABLE_NAME,
        schema=SCHEMA_NAME,
        type_="check",
    )
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        TABLE_NAME,
        _kind_check(DOWNGRADE_KIND_LIST),
        schema=SCHEMA_NAME,
    )
