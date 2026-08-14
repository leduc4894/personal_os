"""Stable advisory-lock derivation and the transaction-only lock contract.

These tests pin the two namespaces, reproduce the signed SHA-256 first-word
derivation from the frozen algorithm (keeping the pinned literal as a
compatibility guard) and scan every production SQL string in the adapter for
transaction-scoped ``pg_advisory_xact_lock`` usage, rejecting session-level
advisory locks outright.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa

from personal_os.sources.commands import IdempotencyKey
from postgresql_source_store.locks import (
    IDEMPOTENCY_LOCK_NAMESPACE,
    SOURCE_LOCK_NAMESPACE,
    idempotency_lock_key,
    idempotency_lock_statement,
    source_lock_key,
    source_lock_statement,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_SOURCE_ROOT = (
    REPO_ROOT / "packages" / "postgresql-source-store" / "src" / "postgresql_source_store"
)

PINNED_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000002")
PINNED_SOURCE_LOCK_KEY = -1788951247

_SIGNED_INT32_MIN = -(2**31)
_SIGNED_INT32_MAX = 2**31 - 1


def _frozen_signed_first_sha256_word(material: bytes) -> int:
    """The frozen derivation algorithm, re-implemented for the test."""
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big", signed=True)


# --- brief Step 1: pinned namespaces and derivation ------------------------


def test_lock_namespaces_are_pinned() -> None:
    assert IDEMPOTENCY_LOCK_NAMESPACE == 0x53564349
    assert SOURCE_LOCK_NAMESPACE == 0x53564353


def test_source_lock_key_matches_frozen_algorithm_and_pinned_literal() -> None:
    # Recompute once from the frozen algorithm, then keep the literal as a
    # compatibility guard against a change in either side.
    expected = _frozen_signed_first_sha256_word(PINNED_SOURCE_ID.bytes)
    assert expected == PINNED_SOURCE_LOCK_KEY
    assert source_lock_key(PINNED_SOURCE_ID) == expected
    assert source_lock_key(PINNED_SOURCE_ID) == PINNED_SOURCE_LOCK_KEY


def test_idempotency_lock_key_uses_workspace_bytes_nul_separator_and_ascii_key() -> None:
    workspace_id = UUID("018f47a0-7b00-7000-8000-000000000001")
    key = IdempotencyKey("client-generated-opaque-key")
    expected = _frozen_signed_first_sha256_word(
        workspace_id.bytes + b"\x00" + key.value.encode("ascii")
    )
    assert idempotency_lock_key(workspace_id, key) == expected


def test_lock_keys_are_deterministic_and_signed_int32() -> None:
    workspace_id = uuid4()
    key = IdempotencyKey("stable-request-1")
    for derived_key in (
        source_lock_key(PINNED_SOURCE_ID),
        idempotency_lock_key(workspace_id, key),
    ):
        assert _SIGNED_INT32_MIN <= derived_key <= _SIGNED_INT32_MAX
    assert source_lock_key(PINNED_SOURCE_ID) == source_lock_key(PINNED_SOURCE_ID)
    assert idempotency_lock_key(workspace_id, key) == idempotency_lock_key(workspace_id, key)


def test_distinct_material_derives_distinct_keys() -> None:
    workspace_id = uuid4()
    assert source_lock_key(uuid4()) != source_lock_key(uuid4())
    assert idempotency_lock_key(workspace_id, IdempotencyKey("request-a")) != (
        idempotency_lock_key(workspace_id, IdempotencyKey("request-b"))
    )
    assert idempotency_lock_key(uuid4(), IdempotencyKey("request-a")) != (
        idempotency_lock_key(uuid4(), IdempotencyKey("request-a"))
    )
    # Namespaces differ between the two lock families for identical material.
    assert IDEMPOTENCY_LOCK_NAMESPACE != SOURCE_LOCK_NAMESPACE


# --- transaction-only advisory lock statements -----------------------------


def test_lock_statements_bind_namespace_and_key_as_parameters() -> None:
    statement = idempotency_lock_statement(uuid4(), IdempotencyKey("request-1"))
    assert isinstance(statement, sa.TextClause)
    compiled = str(statement)
    assert compiled == "SELECT pg_advisory_xact_lock(:namespace, :derived_key)"

    source_statement = source_lock_statement(PINNED_SOURCE_ID)
    assert str(source_statement) == "SELECT pg_advisory_xact_lock(:namespace, :derived_key)"
    params = source_statement.compile().params
    assert params == {
        "namespace": SOURCE_LOCK_NAMESPACE,
        "derived_key": source_lock_key(PINNED_SOURCE_ID),
    }


# --- brief Step 1: production SQL scan --------------------------------------


def test_production_sql_uses_only_transaction_scoped_advisory_locks() -> None:
    # Any ``pg_advisory_*`` call that is not transaction-scoped (session lock,
    # session unlock, shared variants) is prohibited.
    advisory_call_pattern = re.compile(r"\bpg_advisory_\w+\b")
    xact_lock_pattern = re.compile(r"\bpg_advisory_xact_lock\b")
    scanned_files = sorted(PACKAGE_SOURCE_ROOT.glob("*.py"))
    assert scanned_files, "expected the adapter package source tree to exist"

    xact_occurrences = 0
    for source_path in scanned_files:
        source_text = source_path.read_text(encoding="utf-8")
        offenders = [
            call
            for call in advisory_call_pattern.findall(source_text)
            if not xact_lock_pattern.fullmatch(call)
        ]
        assert not offenders, (
            f"session-level advisory locks are prohibited, found in {source_path.name}"
        )
        xact_occurrences += len(xact_lock_pattern.findall(source_text))
    assert xact_occurrences > 0, "expected transaction-scoped advisory lock SQL in production"
