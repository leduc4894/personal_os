"""Canonical recovery manifest encoding, digest and strict parsing (spec 8.2).

The manifest is the single integrity witness of a recovery bundle: canonical
UTF-8 JSON with sorted keys, compact separators, literal non-ASCII, no NaN and
a final newline. Parsing is strict and total: the contract identifier is never
guessed, duplicate JSON keys, unknown fields, non-canonical bytes and every
grammar violation are rejected with the closed
:class:`~personal_os.recovery.contracts.RecoveryBundleInvalidReason` tokens,
never with provider text, paths or raw bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from types import MappingProxyType
from typing import Final, NoReturn
from uuid import UUID

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage.keys import (
    CanonicalMediaType,
    ContentDigest,
    derive_canonical_object_key,
)
from personal_os.recovery.contracts import (
    CANONICAL_COUNT_TABLES,
    LEGACY_V2_CANONICAL_COUNT_TABLES,
    MANIFEST_CONTRACT_V1,
    MANIFEST_CONTRACT_V2,
    MANIFEST_CONTRACT_V3,
    MAXIMUM_OBJECT_SIZE_BYTES,
    POSTGRESQL_SCHEMA_REVISION,
    V1_CANONICAL_COUNT_TABLES,
    V2_CANONICAL_COUNT_TABLES,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryBundleInvalidReason,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
)

_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
_POSTGRES_DUMP_RELATIVE_PATH: Final[str] = "postgres.dump"
_POSTGRES_DUMP_FORMAT: Final[str] = "custom"
_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {
        "bundle_id",
        "canonical_counts",
        "contract",
        "created_at",
        "objects",
        "postgres_dump",
        "postgresql_schema_revision",
        "postgresql_server_version",
        "source_environment",
    }
)
_DUMP_KEYS: Final[frozenset[str]] = frozenset({"format", "relative_path", "sha256", "size_bytes"})
_OBJECT_KEYS: Final[frozenset[str]] = frozenset(
    {"content_sha256", "media_type", "object_key", "relative_path", "size_bytes"}
)
_HEX_LOWER: Final[frozenset[str]] = frozenset("0123456789abcdef")
_CONTRACT_SCHEMA_REVISIONS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        MANIFEST_CONTRACT_V1: "20260813_01",
        MANIFEST_CONTRACT_V2: "20260818_01",
        MANIFEST_CONTRACT_V3: POSTGRESQL_SCHEMA_REVISION,
    }
)


def _reject(reason: RecoveryBundleInvalidReason) -> NoReturn:
    raise RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID,
        safe_details={"reason": reason},
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            _reject(RecoveryBundleInvalidReason.DUPLICATE_JSON_KEY)
        seen.add(key)
    return dict(pairs)


def _is_lowercase_hex64(value: str) -> bool:
    return len(value) == 64 and all(character in _HEX_LOWER for character in value)


def format_manifest_timestamp(created_at: datetime) -> str:
    """Render ``created_at`` as canonical UTC text with exactly six fraction digits."""

    return f"{created_at:%Y-%m-%dT%H:%M:%S.%f}Z"


def encode_manifest(manifest: RecoveryManifest) -> bytes:
    """Encode the canonical UTF-8 JSON bytes: sorted keys, compact, final newline."""

    payload = {
        "bundle_id": str(manifest.bundle_id),
        "canonical_counts": dict(sorted(manifest.canonical_counts.items())),
        "contract": manifest.contract,
        "created_at": format_manifest_timestamp(manifest.created_at),
        "objects": [
            {
                "content_sha256": entry.content_sha256,
                "media_type": entry.media_type,
                "object_key": entry.object_key,
                "relative_path": entry.relative_path,
                "size_bytes": entry.size_bytes,
            }
            for entry in manifest.objects  # already sorted by content_sha256
        ],
        "postgres_dump": {
            "format": _POSTGRES_DUMP_FORMAT,
            "relative_path": manifest.postgres_dump.relative_path,
            "sha256": manifest.postgres_dump.sha256,
            "size_bytes": manifest.postgres_dump.size_bytes,
        },
        "postgresql_schema_revision": manifest.postgresql_schema_revision,
        "postgresql_server_version": manifest.postgresql_server_version,
        "source_environment": manifest.source_environment,
    }
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return (text + "\n").encode("utf-8")


def manifest_digest(raw_manifest_bytes: bytes) -> str:
    """Lowercase SHA-256 of the canonical bytes plus exactly one newline."""

    return hashlib.sha256(raw_manifest_bytes + b"\n").hexdigest()


def parse_manifest(raw: bytes) -> RecoveryManifest:
    """Strictly parse and validate canonical manifest bytes (spec 8.2 order).

    Validation order: UTF-8 decode; strict JSON load rejecting duplicate keys;
    the ``contract`` field first and never guessed; the exact top-level key
    set; field grammar (UUIDv7 bundle id, six-digit-Z timestamp, closed counts
    map, sorted strictly ascending unique object entries, derived object keys,
    bounded sizes); finally byte-canonicality — re-encoding must equal the
    input. Every failure raises ``RecoveryError`` with
    ``canonical_recovery_bundle_invalid`` and the exact closed reason token.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _reject(RecoveryBundleInvalidReason.JSON_NONCANONICAL)
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError:
        _reject(RecoveryBundleInvalidReason.JSON_NONCANONICAL)
    if not isinstance(payload, dict):
        _reject(RecoveryBundleInvalidReason.JSON_NONCANONICAL)
    contract_value = payload.get("contract")
    if not isinstance(contract_value, str) or contract_value not in _CONTRACT_SCHEMA_REVISIONS:
        _reject(RecoveryBundleInvalidReason.CONTRACT_UNSUPPORTED)
    if frozenset(payload) != _MANIFEST_KEYS:
        _reject(RecoveryBundleInvalidReason.FIELD_UNKNOWN)

    bundle_id_value = payload["bundle_id"]
    if not isinstance(bundle_id_value, str):
        _reject(RecoveryBundleInvalidReason.BUNDLE_ID_INVALID)
    try:
        bundle_id = UUID(bundle_id_value)
    except ValueError:
        _reject(RecoveryBundleInvalidReason.BUNDLE_ID_INVALID)
    if bundle_id.version != 7:
        _reject(RecoveryBundleInvalidReason.BUNDLE_ID_INVALID)

    created_at_value = payload["created_at"]
    if (
        not isinstance(created_at_value, str)
        or _TIMESTAMP_PATTERN.fullmatch(created_at_value) is None
    ):
        _reject(RecoveryBundleInvalidReason.TIMESTAMP_INVALID)
    try:
        created_at = datetime.strptime(created_at_value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        _reject(RecoveryBundleInvalidReason.TIMESTAMP_INVALID)

    source_environment_value = payload["source_environment"]
    if not isinstance(source_environment_value, str):
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
    try:
        source_environment = RecoveryEnvironment(source_environment_value)
    except ValueError:
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)

    server_version_value = payload["postgresql_server_version"]
    schema_revision_value = payload["postgresql_schema_revision"]
    if not isinstance(server_version_value, str) or not isinstance(schema_revision_value, str):
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
    if schema_revision_value != _CONTRACT_SCHEMA_REVISIONS[contract_value]:
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)

    dump_value = payload["postgres_dump"]
    if not isinstance(dump_value, dict) or frozenset(dump_value) != _DUMP_KEYS:
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
    dump_format = dump_value["format"]
    dump_relative_path = dump_value["relative_path"]
    dump_sha256 = dump_value["sha256"]
    dump_size_bytes = dump_value["size_bytes"]
    if dump_format != _POSTGRES_DUMP_FORMAT or dump_relative_path != _POSTGRES_DUMP_RELATIVE_PATH:
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
    if not isinstance(dump_sha256, str) or not _is_lowercase_hex64(dump_sha256):
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
    if (
        not isinstance(dump_size_bytes, int)
        or isinstance(dump_size_bytes, bool)
        or dump_size_bytes < 0
    ):
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)

    counts_value = payload["canonical_counts"]
    expected_count_table_sets = (
        (frozenset(V1_CANONICAL_COUNT_TABLES),)
        if contract_value == MANIFEST_CONTRACT_V1
        else (
            (
                frozenset(LEGACY_V2_CANONICAL_COUNT_TABLES),
                frozenset(V2_CANONICAL_COUNT_TABLES),
            )
            if contract_value == MANIFEST_CONTRACT_V2
            else (frozenset(CANONICAL_COUNT_TABLES),)
        )
    )
    if not isinstance(counts_value, dict) or frozenset(counts_value) not in (
        expected_count_table_sets
    ):
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
    counts: dict[str, int] = {}
    for table, table_count in counts_value.items():
        if not isinstance(table_count, int) or isinstance(table_count, bool) or table_count < 0:
            _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
        counts[str(table)] = table_count

    objects_value = payload["objects"]
    if not isinstance(objects_value, list):
        _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
    entries: list[ManifestObjectEntry] = []
    previous_sha256: str | None = None
    for item in objects_value:
        if not isinstance(item, dict) or frozenset(item) != _OBJECT_KEYS:
            _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
        content_sha256 = item["content_sha256"]
        media_type_value = item["media_type"]
        object_key = item["object_key"]
        relative_path = item["relative_path"]
        size_bytes = item["size_bytes"]
        if not isinstance(content_sha256, str):
            _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
        try:
            digest = ContentDigest.parse(content_sha256)
        except ValueError:
            _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
        if not isinstance(object_key, str) or not isinstance(relative_path, str):
            _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
        derived_key = derive_canonical_object_key(digest).value
        if relative_path != object_key or object_key != derived_key:
            _reject(RecoveryBundleInvalidReason.PATH_KEY_MISMATCH)
        if not isinstance(media_type_value, str):
            _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
        try:
            CanonicalMediaType.parse(media_type_value)
        except ValueError:
            _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or size_bytes > MAXIMUM_OBJECT_SIZE_BYTES
        ):
            _reject(RecoveryBundleInvalidReason.FIELD_INVALID)
        if previous_sha256 is not None:
            if content_sha256 == previous_sha256:
                _reject(RecoveryBundleInvalidReason.DIGEST_DUPLICATE)
            if content_sha256 < previous_sha256:
                _reject(RecoveryBundleInvalidReason.ENTRIES_UNSORTED)
        previous_sha256 = content_sha256
        entries.append(
            ManifestObjectEntry(
                content_sha256=content_sha256,
                object_key=object_key,
                size_bytes=size_bytes,
                media_type=media_type_value,
                relative_path=relative_path,
            )
        )

    manifest = RecoveryManifest(
        bundle_id=bundle_id,
        created_at=created_at,
        source_environment=source_environment,
        postgresql_server_version=server_version_value,
        postgresql_schema_revision=schema_revision_value,
        postgres_dump=ManifestDumpEntry(
            relative_path=_POSTGRES_DUMP_RELATIVE_PATH,
            size_bytes=dump_size_bytes,
            sha256=dump_sha256,
        ),
        canonical_counts=MappingProxyType(counts),
        objects=tuple(entries),
        contract=contract_value,
    )
    if encode_manifest(manifest) != raw:
        _reject(RecoveryBundleInvalidReason.JSON_NONCANONICAL)
    return manifest
