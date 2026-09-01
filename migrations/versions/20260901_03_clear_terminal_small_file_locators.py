"""Clear the raw locator on already-terminal small-file operation rows.

Revision ID: 20260901_03
Revises: 20260901_02
Create Date: 2026-09-01

One-shot privacy data remediation (BACKLOG 2026-09-01, small-file): rows
that reached a terminal ``committed`` or ``failed`` state before the
clear-on-terminal runtime writers landed still carry the raw note path in
``normalized_locator``, because the identical-replay early-return is
intentionally non-mutating and so never re-clears them. This revision
brings those historical rows to the shape the runtime now produces: the
transient raw locator is nulled while the retained ``locator_fingerprint``
digest stays, so an exact replay can still confirm locator identity.

The upgrade is exactly one guarded UPDATE — ``SET normalized_locator =
NULL`` on terminal rows that still carry a locator — so a second run
matches zero rows (naturally idempotent). It touches no other column: the
fingerprint carries the replay evidence, and ``updated_at`` stays
untouched because this is a schema-authority backfill, not a domain
write (the locator-lifecycle revision's projection-intent backfill set
that precedent). No public contract, wire behavior or query semantics
change.

The downgrade is an intentional NO-OP: the cleared raw paths are
irrecoverable by design — restoring them would re-expose the very note
paths this privacy remediation removes — and the retained fingerprints
keep replay identity intact, so there is nothing faithful to restore. It
is deliberately NOT gated on ``allow_destructive``: the runtime
clear-on-terminal transition is ungated and shares this same privacy
intent, and a no-op needs no destructive authorization.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260901_03"
down_revision: str | None = "20260901_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

#: The one guarded UPDATE this revision ships. The terminal state set is
#: restated from the owning table's closed CHECK constraints in
#: ``20260818_01`` — ``ck_small_file_upload_operations__state`` admits
#: exactly ``pending``, ``receiving``, ``committed`` and ``failed``, and
#: the terminal-shape companion treats only ``committed`` and ``failed``
#: as closed — because this module cannot import the domain constants.
#: The ``normalized_locator IS NOT NULL`` guard makes the UPDATE a no-op
#: on every replay, so the remediation is idempotent by construction.
TERMINAL_LOCATOR_CLEAR_SQL: Final[str] = (
    f"UPDATE {SCHEMA_NAME}.small_file_upload_operations "
    "SET normalized_locator = NULL "
    "WHERE state IN ('committed', 'failed') "
    "AND normalized_locator IS NOT NULL"
)


def upgrade() -> None:
    """Clear the raw locator on every already-terminal operation row."""

    op.execute(sa.text(TERMINAL_LOCATOR_CLEAR_SQL))


def downgrade() -> None:
    """Intentional no-op: the cleared raw locators are irrecoverable by design."""

    # Privacy remediation, not restorable schema work: the raw note paths
    # this upgrade removed must not come back, and the retained locator
    # fingerprints keep replay identity intact, so there is nothing
    # faithful to restore. The runtime clear-on-terminal transition is
    # ungated and shares this privacy intent, so this no-op is not gated
    # on ``allow_destructive`` either.
