"""Seal the raw multipart operation token on the canonical session row.

Revision ID: 20260828_04
Revises: 20260828_03
Create Date: 2026-08-28

The durable bound-operation evidence of a multipart session must rebuild
the exact :class:`BoundSmallFileOperation` a completion claimant hands to
the small-file publication fence — including its raw ``operation_token``,
because the fence resolves the operation row by the one-way hash of that
preimage. ``small_file_upload_operations`` deliberately persists only the
hash, so the preimage was unrecoverable and no durable serve graph could
exist.

This revision adds exactly three nullable sealed-text columns to
``multipart_uploads`` — ``operation_token_ciphertext``,
``operation_token_nonce`` and ``operation_token_key_id`` — carrying the
AEAD-sealed raw token (never plaintext) together with the versioned
keyring key that sealed it. The session reservation writes the seal inside
its own transaction and refreshes it whenever a replayed reservation
arrives with a rotated token, so the seal always names the operation row's
current token hash; the durable evidence read verifies that hash and fails
closed on any drift. Nullability admits compositions without a codec (the
cleanup worker) and rows reserved before this revision: those carry no
seal and the evidence read fails closed instead of guessing. A biconditional
CHECK keeps the three columns present or absent together, and the key ID
keeps the closed safe-token shape.

The downgrade drops the sealed columns only after no forward-state session
row still carries a seal — such a row's completion evidence would become
unrecoverable — refusing with the closed
``multipart_operation_token_downgrade_has_forward_sealed_rows`` token
otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260828_04"
down_revision: str | None = "20260828_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

SESSION_TABLE_NAME: Final[str] = "multipart_uploads"

#: The closed safe-token shape the sealed key ID satisfies (mirrors the
#: TOTP key ID column of migration 20260816_01).
_SAFE_TOKEN_CHECK: Final[str] = r"~ '^[a-z][a-z0-9_.:-]*$'"

#: The five forward states whose completion still needs the sealed token.
_FORWARD_STATES_SQL: Final[str] = (
    "('created', 'uploading', 'completing', 'verifying', 'promoting')"
)

_SEALED_COLUMNS: Final[tuple[tuple[str, sa.String], ...]] = (
    ("operation_token_ciphertext", sa.String(length=255)),
    ("operation_token_nonce", sa.String(length=64)),
    ("operation_token_key_id", sa.String(length=100)),
)

_DOWNGRADE_FORWARD_SEALED_COUNT_SQL: Final[str] = (
    f"SELECT count(*) FROM {SCHEMA_NAME}.{SESSION_TABLE_NAME} "
    "WHERE state IN " + _FORWARD_STATES_SQL
    + " AND operation_token_ciphertext IS NOT NULL"
)

_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = (
    "multipart_operation_token_downgrade_has_forward_sealed_rows"
)


def upgrade() -> None:
    """Add the three nullable sealed-token columns and their closed shapes."""

    for column_name, column_type in _SEALED_COLUMNS:
        op.add_column(
            SESSION_TABLE_NAME,
            sa.Column(column_name, column_type, nullable=True),
            schema=SCHEMA_NAME,
        )
    op.create_check_constraint(
        "ck_multipart_uploads__operation_token_seal_biconditional",
        SESSION_TABLE_NAME,
        "("
        "operation_token_ciphertext IS NULL AND operation_token_nonce IS NULL "
        "AND operation_token_key_id IS NULL"
        ") OR ("
        "operation_token_ciphertext IS NOT NULL AND operation_token_nonce IS NOT NULL "
        "AND operation_token_key_id IS NOT NULL"
        ")",
        schema=SCHEMA_NAME,
    )
    op.create_check_constraint(
        "ck_multipart_uploads__operation_token_key_id",
        SESSION_TABLE_NAME,
        "operation_token_key_id IS NULL "
        "OR (char_length(operation_token_key_id) <= 100 "
        f"AND operation_token_key_id {_SAFE_TOKEN_CHECK})",
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    """Drop the sealed columns once no forward session still needs its seal."""

    bind = op.get_bind()
    forward_sealed_row_count = int(
        bind.execute(sa.text(_DOWNGRADE_FORWARD_SEALED_COUNT_SQL)).scalar_one()
    )
    if forward_sealed_row_count > 0:
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)
    for column_name, _column_type in reversed(_SEALED_COLUMNS):
        op.drop_column(SESSION_TABLE_NAME, column_name, schema=SCHEMA_NAME)
