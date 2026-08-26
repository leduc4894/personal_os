"""Allow the per-entry catch-up download to echo its manifest entry.

Revision ID: 20260826_02
Revises: 20260826_01
Create Date: 2026-08-26

The Task 11b manifest action wire amendment: a ``download`` action is the
only kind that MAY lack a local entry (the canonical-only download of a
source absent from the device manifest), and the per-entry catch-up
download — whose manifest entry the device holds — keeps that entry's
``local_entry_id`` echo, exactly as spec 6.5 reads after the amendment
(the nullable column exists for canonical-only downloads only).  The
``20260826_01`` action shape pinned the stricter reading (every download
entry-less); this revision rewrites that one clause and restates every
other clause of the shape verbatim.

Downgrade restores the strict shape only under the standard explicit
destructive gate: echoed catch-up download actions cannot exist under it,
so they are discarded first (the deeper ``20260826_01`` downgrade drops
every manifest table under the same gate).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_02"
down_revision: str | None = "20260826_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

_ACTION_SHAPE_CONSTRAINT: Final[str] = "ck_manifest_actions__shape"

#: The amended per-kind action shape: a download is the only kind that may
#: lack a local entry (the canonical-only download) while the per-entry
#: catch-up download keeps its entry echo; every other clause of the
#: ``20260826_01`` shape is restated verbatim.
_AMENDED_ACTION_SHAPE_CHECK: Final[str] = (
    "(local_entry_id IS NOT NULL OR action_kind = 'download') "
    "AND (action_kind IN ('conflict', 'excluded')) = (safe_reason_code IS NOT NULL) "
    "AND (action_kind NOT IN ('download', 'no_change') "
    "OR (source_id IS NOT NULL AND source_version_id IS NOT NULL)) "
    "AND (action_kind <> 'apply_tombstone' OR source_tombstone_id IS NOT NULL) "
    "AND (action_kind <> 'apply_tombstone' OR source_id IS NOT NULL) "
    "AND (source_version_id IS NULL OR source_id IS NOT NULL) "
    "AND (source_locator_id IS NULL OR source_id IS NOT NULL) "
    "AND (source_tombstone_id IS NULL OR source_id IS NOT NULL)"
)

#: The strict ``20260826_01`` shape the downgrade restores.
_STRICT_ACTION_SHAPE_CHECK: Final[str] = (
    "(action_kind = 'download') = (local_entry_id IS NULL) "
    "AND (action_kind IN ('conflict', 'excluded')) = (safe_reason_code IS NOT NULL) "
    "AND (action_kind NOT IN ('download', 'no_change') "
    "OR (source_id IS NOT NULL AND source_version_id IS NOT NULL)) "
    "AND (action_kind <> 'apply_tombstone' OR source_tombstone_id IS NOT NULL) "
    "AND (action_kind <> 'apply_tombstone' OR source_id IS NOT NULL) "
    "AND (source_version_id IS NULL OR source_id IS NOT NULL) "
    "AND (source_locator_id IS NULL OR source_id IS NOT NULL) "
    "AND (source_tombstone_id IS NULL OR source_id IS NOT NULL)"
)

_ECHO_ROW_COUNT_SQL: Final[str] = (
    "SELECT count(*) FROM knowledge.manifest_actions"
    " WHERE action_kind = 'download' AND local_entry_id IS NOT NULL"
)

_DELETE_ECHO_ROWS_SQL: Final[str] = (
    "DELETE FROM knowledge.manifest_actions"
    " WHERE action_kind = 'download' AND local_entry_id IS NOT NULL"
)

_DESTRUCTIVE_X_ARGUMENT: Final[str] = "allow_destructive"
_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = (
    "manifest_download_entry_echo_downgrade_requires_explicit_gate"
)


def _downgrade_gate_open() -> bool:
    """Return whether the explicit destructive Alembic x-argument is present."""

    migration_context = op.get_context()
    config = getattr(migration_context, "config", None)
    command_options = getattr(config, "cmd_opts", None)
    for argument in getattr(command_options, "x", None) or []:
        key, _, value = str(argument).partition("=")
        if key == _DESTRUCTIVE_X_ARGUMENT and value == "true":
            return True
    return False


def upgrade() -> None:
    """Rewrite the action shape so a download may echo its manifest entry."""

    op.drop_constraint(_ACTION_SHAPE_CONSTRAINT, "manifest_actions", schema=SCHEMA_NAME)
    op.create_check_constraint(
        _ACTION_SHAPE_CONSTRAINT,
        "manifest_actions",
        _AMENDED_ACTION_SHAPE_CHECK,
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    """Restore the strict entry-less download shape under the destructive gate."""

    echoed_row_count = int(op.get_bind().execute(sa.text(_ECHO_ROW_COUNT_SQL)).scalar_one())
    if echoed_row_count > 0 and not _downgrade_gate_open():
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)
    if echoed_row_count > 0:
        op.execute(sa.text(_DELETE_ECHO_ROWS_SQL))
    op.drop_constraint(_ACTION_SHAPE_CONSTRAINT, "manifest_actions", schema=SCHEMA_NAME)
    op.create_check_constraint(
        _ACTION_SHAPE_CONSTRAINT,
        "manifest_actions",
        _STRICT_ACTION_SHAPE_CHECK,
        schema=SCHEMA_NAME,
    )
