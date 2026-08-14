"""Content-object deduplication contracts for the atomic create transaction.

These unit tests pin the exact content-object reuse semantics of design
section 8.4 without touching a database: the upsert statement is a
schema-qualified ``ON CONFLICT (content_hash) DO NOTHING`` insert bound
entirely from the verified receipt, the reuse lookup selects the metadata
columns by the full hash, the exact three-field comparison accepts only an
exact receipt/row match, and the per-invocation identity allocation produces
five distinct UUIDv7 values reused across transaction retries.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from postgresql_source_store.publication_store import (
    SourceCreateIdentities,
    content_object_by_hash_statement,
    content_object_metadata_matches,
    content_object_upsert_statement,
)

_SALT = "content-object-reuse-unit-bytes"
_CONTENT_OBJECT_ID = uuid4()
_VERIFIED_AT = datetime(2026, 8, 14, 9, 0, 0, tzinfo=UTC)


def _receipt(
    *, size_bytes: int | None = None, media_type: str = "text/markdown"
) -> VerifiedObjectReceipt:
    digest = ContentDigest.parse(hashlib.sha256(_SALT.encode("utf-8")).hexdigest())
    return VerifiedObjectReceipt(
        content_digest=digest,
        object_key=derive_canonical_object_key(digest),
        size_bytes=len(_SALT) if size_bytes is None else size_bytes,
        media_type=CanonicalMediaType.parse(media_type),
        verified_at=_VERIFIED_AT,
        verification_method=VerificationMethod.UPLOADED_FULL_READ,
    )


def _matching_row(receipt: VerifiedObjectReceipt) -> dict[str, object]:
    return {
        "object_key": receipt.object_key.value,
        "byte_size": receipt.size_bytes,
        "media_type": receipt.media_type.value,
    }


# --- upsert statement -------------------------------------------------------------


def test_upsert_is_do_nothing_on_content_hash_and_never_updates() -> None:
    statement = content_object_upsert_statement(_CONTENT_OBJECT_ID, _receipt())
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT (content_hash) DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    assert "knowledge.content_objects" in sql


def test_upsert_binds_every_column_from_the_verified_receipt() -> None:
    receipt = _receipt()
    statement = content_object_upsert_statement(_CONTENT_OBJECT_ID, receipt)
    compiled = statement.compile(dialect=postgresql.dialect())

    assert compiled.params == {
        "content_object_id": _CONTENT_OBJECT_ID,
        "content_hash": receipt.content_digest.hexadecimal,
        "object_key": receipt.object_key.value,
        "byte_size": receipt.size_bytes,
        "media_type": receipt.media_type.value,
        "verified_at": receipt.verified_at,
    }


def test_by_hash_lookup_selects_metadata_columns_for_the_exact_hash() -> None:
    receipt = _receipt()
    statement = content_object_by_hash_statement(receipt.content_digest.hexadecimal)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    for column in ("content_object_id", "object_key", "byte_size", "media_type"):
        assert column in sql
    assert "knowledge.content_objects" in sql
    # SQLAlchemy disambiguates the WHERE bind from the selected column label,
    # so assert the single content-hash parameter by value, not by key spelling.
    [bound_hash] = compiled.params.values()
    assert bound_hash == receipt.content_digest.hexadecimal


# --- exact metadata comparison ----------------------------------------------------


def test_metadata_match_accepts_an_exact_receipt_row_pair() -> None:
    receipt = _receipt()

    assert content_object_metadata_matches(receipt, **_matching_row(receipt)) is True


def test_metadata_match_rejects_each_single_dimension_mismatch() -> None:
    receipt = _receipt()
    exact_row = _matching_row(receipt)

    for dimension, divergent_value in (
        ("object_key", "objects/sha256/00/00/other"),
        ("byte_size", receipt.size_bytes + 1),
        ("media_type", "text/plain"),
    ):
        mismatched_row = dict(exact_row)
        mismatched_row[dimension] = divergent_value

        assert (
            content_object_metadata_matches(
                receipt,
                object_key=str(mismatched_row["object_key"]),
                byte_size=int(mismatched_row["byte_size"]),
                media_type=str(mismatched_row["media_type"]),
            )
            is False
        ), dimension


# --- per-invocation identity allocation -------------------------------------------


def test_create_identities_allocation_is_five_distinct_uuid7_values() -> None:
    identities = SourceCreateIdentities.allocate()

    allocated = (
        identities.content_object_id,
        identities.source_version_id,
        identities.qdrant_intent_id,
        identities.neo4j_intent_id,
        identities.audit_event_id,
    )
    assert len(set(allocated)) == 5
    for value in allocated:
        assert isinstance(value, UUID)
        assert value.version == 7


def test_create_identities_are_allocated_fresh_per_invocation() -> None:
    first = SourceCreateIdentities.allocate()
    second = SourceCreateIdentities.allocate()

    assert first.content_object_id != second.content_object_id
    assert first.source_version_id != second.source_version_id
    assert first.qdrant_intent_id != second.qdrant_intent_id
    assert first.neo4j_intent_id != second.neo4j_intent_id
    assert first.audit_event_id != second.audit_event_id
