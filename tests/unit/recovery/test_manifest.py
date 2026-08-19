"""Canonical recovery manifest bytes, digest and strict parse rejection (spec 8.2).

Every parse failure must be ``canonical_recovery_bundle_invalid`` with the
exact closed ``reason`` token; no provider text, path or raw byte is ever
copied into safe details.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.recovery.contracts import (
    CANONICAL_COUNT_TABLES,
    MANIFEST_CONTRACT,
    MAXIMUM_OBJECT_SIZE_BYTES,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
)
from personal_os.recovery.manifest import encode_manifest, manifest_digest, parse_manifest

# A fixed valid UUIDv7 (version nibble 7, RFC 4122 variant).
_BUNDLE_ID = UUID("018f6b1e-8a2c-7d3e-9f01-2a3b4c5d6e7f")
_CREATED_AT = datetime(2026, 8, 15, 12, 30, 45, 123456)

_FIRST_DIGEST = "0" * 64
_SECOND_DIGEST = "1" * 64
_DUMP_SHA256 = "2" * 64


def _object_entry(digest_hexadecimal: str, size_bytes: int) -> dict[str, object]:
    object_key = (
        f"objects/sha256/{digest_hexadecimal[0:2]}/{digest_hexadecimal[2:4]}/{digest_hexadecimal}"
    )
    return {
        "content_sha256": digest_hexadecimal,
        "media_type": "application/octet-stream",
        "object_key": object_key,
        "relative_path": object_key,
        "size_bytes": size_bytes,
    }


def build_manifest(**overrides: object) -> RecoveryManifest:
    counts = {table: index + 1 for index, table in enumerate(CANONICAL_COUNT_TABLES)}
    fields: dict[str, object] = {
        "bundle_id": _BUNDLE_ID,
        "created_at": _CREATED_AT,
        "source_environment": RecoveryEnvironment.LOCAL,
        "postgresql_server_version": "18.4",
        "postgresql_schema_revision": "20260813_01",
        "postgres_dump": {
            "relative_path": "postgres.dump",
            "size_bytes": 4096,
            "sha256": _DUMP_SHA256,
        },
        "canonical_counts": counts,
        "objects": (_object_entry(_FIRST_DIGEST, 128), _object_entry(_SECOND_DIGEST, 256)),
    }
    fields.update(overrides)
    postgres_dump_value = fields["postgres_dump"]
    if isinstance(postgres_dump_value, dict):
        postgres_dump_value = ManifestDumpEntry(
            relative_path=str(postgres_dump_value["relative_path"]),
            size_bytes=int(postgres_dump_value["size_bytes"]),
            sha256=str(postgres_dump_value["sha256"]),
        )
    objects_value = fields["objects"]
    if isinstance(objects_value, (tuple, list)) and all(
        isinstance(item, dict) for item in objects_value
    ):
        objects_value = tuple(
            ManifestObjectEntry(
                content_sha256=str(item["content_sha256"]),
                object_key=str(item["object_key"]),
                size_bytes=int(item["size_bytes"]),
                media_type=str(item["media_type"]),
                relative_path=str(item["relative_path"]),
            )
            for item in objects_value
        )
    return RecoveryManifest(
        bundle_id=fields["bundle_id"],  # type: ignore[arg-type]
        created_at=fields["created_at"],  # type: ignore[arg-type]
        source_environment=fields["source_environment"],  # type: ignore[arg-type]
        postgresql_server_version=fields["postgresql_server_version"],  # type: ignore[arg-type]
        postgresql_schema_revision=fields["postgresql_schema_revision"],  # type: ignore[arg-type]
        postgres_dump=postgres_dump_value,  # type: ignore[arg-type]
        canonical_counts=fields["canonical_counts"],  # type: ignore[arg-type]
        objects=objects_value,  # type: ignore[arg-type]
    )


def canonical_json(payload: object) -> bytes:
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return (text + "\n").encode("utf-8")


def manifest_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = json.loads(encode_manifest(build_manifest()).decode("utf-8"))
    payload.update(overrides)
    return payload


def assert_bundle_invalid(raw: bytes, reason: str) -> None:
    with pytest.raises(RecoveryError) as exc_info:
        parse_manifest(raw)
    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID
    assert error.safe_details["reason"] == reason


def test_manifest_bytes_are_canonical_sorted_compact_unicode_json() -> None:
    raw = encode_manifest(build_manifest())
    text = raw.decode("utf-8")
    assert text.endswith("}\n")
    parsed_again = json.loads(text)
    assert (
        json.dumps(parsed_again, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        == text
    )


def test_manifest_digest_hashes_bytes_plus_one_newline() -> None:
    raw = encode_manifest(build_manifest())
    assert manifest_digest(raw) == hashlib.sha256(raw + b"\n").hexdigest()


def test_round_trip_preserves_every_field() -> None:
    manifest = build_manifest()
    parsed = parse_manifest(encode_manifest(manifest))
    assert parsed.bundle_id == manifest.bundle_id
    assert parsed.created_at == manifest.created_at
    assert parsed.source_environment == manifest.source_environment
    assert parsed.postgresql_server_version == manifest.postgresql_server_version
    assert parsed.postgresql_schema_revision == manifest.postgresql_schema_revision
    assert parsed.postgres_dump == manifest.postgres_dump
    assert parsed.objects == manifest.objects
    assert parsed.canonical_counts == manifest.canonical_counts
    assert isinstance(parsed.canonical_counts, MappingProxyType)
    with pytest.raises(TypeError):
        parsed.canonical_counts["users"] = 99  # type: ignore[index]


def test_round_trip_accepts_boundary_object_sizes() -> None:
    manifest = build_manifest(
        objects=(
            _object_entry(_FIRST_DIGEST, 0),
            _object_entry(_SECOND_DIGEST, MAXIMUM_OBJECT_SIZE_BYTES),
        )
    )
    assert parse_manifest(encode_manifest(manifest)).objects == manifest.objects


def test_rejects_unsupported_contract_version() -> None:
    unsupported = canonical_json(manifest_payload(contract="canonical_core_backup/v2"))
    assert_bundle_invalid(unsupported, "contract_unsupported")
    assert_bundle_invalid(canonical_json(manifest_payload(contract="")), "contract_unsupported")


def test_rejects_unknown_top_level_field() -> None:
    payload = manifest_payload()
    payload["extra"] = 1
    assert_bundle_invalid(canonical_json(payload), "field_unknown")


def test_rejects_missing_top_level_field() -> None:
    payload = manifest_payload()
    del payload["objects"]
    assert_bundle_invalid(canonical_json(payload), "field_unknown")


def test_rejects_duplicate_json_key() -> None:
    text = encode_manifest(build_manifest()).decode("utf-8").rstrip("\n")
    duplicated = text[:-1] + ',"bundle_id":"018f6b1e-8a2c-7d3e-9f01-2a3b4c5d6e7f"}'
    assert_bundle_invalid((duplicated + "\n").encode("utf-8"), "duplicate_json_key")


def test_rejects_unsorted_object_entries() -> None:
    manifest = build_manifest(
        objects=(_object_entry(_SECOND_DIGEST, 256), _object_entry(_FIRST_DIGEST, 128))
    )
    assert_bundle_invalid(encode_manifest(manifest), "entries_unsorted")


def test_rejects_duplicate_content_sha256() -> None:
    manifest = build_manifest(
        objects=(_object_entry(_FIRST_DIGEST, 1), _object_entry(_FIRST_DIGEST, 2))
    )
    assert_bundle_invalid(encode_manifest(manifest), "digest_duplicate")


def test_rejects_relative_path_object_key_disagreement() -> None:
    entry = _object_entry(_FIRST_DIGEST, 128)
    entry["relative_path"] = "objects/sha256/00/00/elsewhere"
    assert_bundle_invalid(canonical_json(manifest_payload(objects=[entry])), "path_key_mismatch")


def test_rejects_key_not_derived_from_digest() -> None:
    entry = _object_entry(_FIRST_DIGEST, 128)
    entry["object_key"] = f"objects/sha256/ff/ff/{_FIRST_DIGEST}"
    entry["relative_path"] = entry["object_key"]
    assert_bundle_invalid(canonical_json(manifest_payload(objects=[entry])), "path_key_mismatch")


def test_rejects_non_uuidv7_bundle_id() -> None:
    assert_bundle_invalid(
        canonical_json(manifest_payload(bundle_id=str(UUID(int=0)))), "bundle_id_invalid"
    )
    v4_text = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    assert_bundle_invalid(canonical_json(manifest_payload(bundle_id=v4_text)), "bundle_id_invalid")
    assert_bundle_invalid(
        canonical_json(manifest_payload(bundle_id="not-a-uuid")), "bundle_id_invalid"
    )


def test_rejects_noncanonical_timestamp_format() -> None:
    five_digits = canonical_json(manifest_payload(created_at="2026-08-15T12:30:45.12345Z"))
    assert_bundle_invalid(five_digits, "timestamp_invalid")
    offset = canonical_json(manifest_payload(created_at="2026-08-15T12:30:45.123456+00:00"))
    assert_bundle_invalid(offset, "timestamp_invalid")
    no_fraction = canonical_json(manifest_payload(created_at="2026-08-15T12:30:45Z"))
    assert_bundle_invalid(no_fraction, "timestamp_invalid")


def test_rejects_noncanonical_json_bytes() -> None:
    raw = encode_manifest(build_manifest())
    indented = (json.dumps(json.loads(raw), indent=1) + "\n").encode("utf-8")
    assert_bundle_invalid(indented, "json_noncanonical")
    spaced = (
        json.dumps(json.loads(raw), sort_keys=True, separators=(", ", ": "), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    assert_bundle_invalid(spaced, "json_noncanonical")
    assert_bundle_invalid(b"\xff\xfe not json", "json_noncanonical")


def test_rejects_wrong_closed_counts_map() -> None:
    counts = {table: 0 for table in CANONICAL_COUNT_TABLES}
    del counts["audit_events"]
    assert_bundle_invalid(
        canonical_json(manifest_payload(canonical_counts=counts)), "field_invalid"
    )
    extra = dict(counts)
    extra["audit_events"] = 0
    extra["unexpected_table"] = 1
    assert_bundle_invalid(canonical_json(manifest_payload(canonical_counts=extra)), "field_invalid")


def test_rejects_invalid_grammar_fields() -> None:
    assert_bundle_invalid(
        canonical_json(manifest_payload(source_environment="production")), "field_invalid"
    )
    assert_bundle_invalid(
        canonical_json(manifest_payload(canonical_counts="nope")), "field_invalid"
    )
    uppercase_digest = "A" * 64  # canonical digests are lowercase hex only
    entry = _object_entry(uppercase_digest, 128)
    entry["object_key"] = f"objects/sha256/00/00/{uppercase_digest}"
    entry["relative_path"] = entry["object_key"]
    assert_bundle_invalid(canonical_json(manifest_payload(objects=[entry])), "field_invalid")
    assert_bundle_invalid(
        canonical_json(manifest_payload(objects=[{"content_sha256": _FIRST_DIGEST}])),
        "field_invalid",
    )


def test_rejects_out_of_range_object_size() -> None:
    too_large_entry = _object_entry(_FIRST_DIGEST, MAXIMUM_OBJECT_SIZE_BYTES + 1)
    too_large = canonical_json(manifest_payload(objects=[too_large_entry]))
    assert_bundle_invalid(too_large, "field_invalid")
    negative = canonical_json(manifest_payload(objects=[_object_entry(_FIRST_DIGEST, -1)]))
    assert_bundle_invalid(negative, "field_invalid")


def test_manifest_contract_constant_is_pinned() -> None:
    assert MANIFEST_CONTRACT == "canonical_core_backup/v1"
    # Twenty since small-file operations became canonical: the nine baseline
    # tables, ten policy tables, and the durable upload-operation table.
    assert len(CANONICAL_COUNT_TABLES) == 20
    assert CANONICAL_COUNT_TABLES[-1] == "small_file_upload_operations"
