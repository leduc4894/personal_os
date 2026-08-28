"""Widen the small-file upload-operation declared size bound to the product maximum.

Revision ID: 20260828_02
Revises: 20260828_01
Create Date: 2026-08-28

The multipart upload child routes any declared size strictly above the
16 MiB single-part routing constant (and at or below the 100 MiB product
maximum) into the resumable multipart transport, and its canonical
``knowledge.multipart_uploads`` rows bind — by RESTRICT foreign key and a
lifetime-unique operation key — to the frozen ``small_file_upload_operations``
row of exactly that preflight. The ``20260818_01`` CHECK still capped the
operation row's ``declared_size_bytes`` at the single-part routing constant,
so no multipart-routed preflight could ever reserve the operation its
session requires: the two landed bounds were mutually unsatisfiable. This
revision replaces only that one CHECK with the closed product maximum
(``BETWEEN 0 AND 100 MiB``), the same bound the domain contracts and the
multipart session geometry already enforce; every column, index, key and
other constraint of the operations table is untouched.

The downgrade restores the original 16 MiB CHECK and refuses — with the
closed ``small_file_operation_size_downgrade_has_oversized_rows`` token —
while any recorded operation row still declares more than 16 MiB, because
recreating the old CHECK over such rows would fail and silently discarding
recorded upload evidence is never an implicit downgrade behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260828_02"
down_revision: str | None = "20260828_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

OPERATION_TABLE_NAME: Final[str] = "small_file_upload_operations"
DECLARED_SIZE_CHECK_NAME: Final[str] = "ck_small_file_upload_operations__declared_size_bytes"

#: The closed product maximum for any declared upload (spec: multipart
#: routing range upper bound). The migration cannot import the domain
#: constant, so the bound is stated here as the DDL authority's own value;
#: the migration tests pin the equality with the domain contract constant.
_MAXIMUM_DECLARED_SIZE_BYTES: Final[int] = 100 * 1024 * 1024

#: The original single-part routing ceiling of ``20260818_01`` that the
#: downgrade restores.
_SINGLE_PART_ROUTING_CEILING_BYTES: Final[int] = 16 * 1024 * 1024

_WIDENED_DECLARED_SIZE_CHECK: Final[str] = (
    f"declared_size_bytes BETWEEN 0 AND {_MAXIMUM_DECLARED_SIZE_BYTES}"
)

_ORIGINAL_DECLARED_SIZE_CHECK: Final[str] = (
    f"declared_size_bytes BETWEEN 0 AND {_SINGLE_PART_ROUTING_CEILING_BYTES}"
)

_DOWNGRADE_OVERSIZED_COUNT_SQL: Final[str] = (
    f"SELECT count(*) FROM {SCHEMA_NAME}.{OPERATION_TABLE_NAME} "
    f"WHERE declared_size_bytes > {_SINGLE_PART_ROUTING_CEILING_BYTES}"
)

_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = (
    "small_file_operation_size_downgrade_has_oversized_rows"
)


def _drop_declared_size_check() -> None:
    op.drop_constraint(
        DECLARED_SIZE_CHECK_NAME,
        table_name=OPERATION_TABLE_NAME,
        schema=SCHEMA_NAME,
        type_="check",
    )


def _create_declared_size_check(check_text: str) -> None:
    op.create_check_constraint(
        DECLARED_SIZE_CHECK_NAME,
        OPERATION_TABLE_NAME,
        check_text,
        schema=SCHEMA_NAME,
    )


def upgrade() -> None:
    """Replace the stale single-part ceiling with the product maximum."""

    _drop_declared_size_check()
    _create_declared_size_check(_WIDENED_DECLARED_SIZE_CHECK)


def downgrade() -> None:
    """Restore the single-part ceiling once no oversized rows remain."""

    bind = op.get_bind()
    oversized_row_count = int(
        bind.execute(sa.text(_DOWNGRADE_OVERSIZED_COUNT_SQL)).scalar_one()
    )
    if oversized_row_count > 0:
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)
    _drop_declared_size_check()
    _create_declared_size_check(_ORIGINAL_DECLARED_SIZE_CHECK)
