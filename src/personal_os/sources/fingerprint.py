"""Canonical request fingerprints and safe diff hashes.

A request fingerprint is the lowercase SHA-256 over the sorted, compact,
UTF-8 JSON encoding of a canonical request envelope built only from the
validated command members that define replay identity. The envelope never
contains the idempotency key, request/trace IDs, receipt fields or generated
values, so no transport or database artifact can alter it. The raw envelope
and its canonical bytes are constructed and discarded locally; neither is
exposed, stored or logged.

A safe diff hash summarizes only the content identity transition of a
publication: source, optional base version, optional base digest and the new
digest. No title, path or content enters the summary.

Both ``parse`` classmethods share one lowercase-hex64 parser kept local to
this module; consolidating it with ``object_storage.ContentDigest`` was
refused by the 2026-08-14 §7 ruling (row-51 precedent: repetition over
cross-domain abstraction while domains keep closed vocabularies).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from personal_os.object_storage import ContentDigest, ExpectedObject
from personal_os.sources.actors import SourceActor
from personal_os.sources.commands import CreateSourceVersion, UpdateSourceVersion

REQUEST_CONTRACT: Final[str] = "source_version_publish/v1"
REQUEST_CONTRACT_WITH_INITIAL_LOCATOR: Final[str] = "source_version_publish/v2"
SAFE_DIFF_CONTRACT: Final[str] = "source_version_diff/v1"

type SourceVersionCommand = CreateSourceVersion | UpdateSourceVersion

_DIGEST_HEX_LENGTH: Final[int] = 64
_HEX_LOWER: Final[frozenset[str]] = frozenset("0123456789abcdef")


def _parse_hex64(value: str, *, error_message: str, length: int = _DIGEST_HEX_LENGTH) -> str:
    """Validate ``value`` as exactly ``length`` lowercase hexadecimal characters.

    Raise ``ValueError`` with ``error_message`` on any deviation, so each
    caller keeps its contract-specific error text.
    """
    if len(value) != length or any(char not in _HEX_LOWER for char in value):
        raise ValueError(error_message)
    return value


@dataclass(frozen=True, slots=True)
class RequestFingerprint:
    """Lowercase hexadecimal SHA-256 of a canonical request envelope."""

    hexadecimal: str

    @classmethod
    def parse(cls, value: str) -> RequestFingerprint:
        """Validate ``value`` as exactly 64 lowercase hexadecimal characters."""
        return cls(
            _parse_hex64(
                value,
                error_message="value does not satisfy the canonical fingerprint contract",
            )
        )

    def __str__(self) -> str:
        return self.hexadecimal


@dataclass(frozen=True, slots=True)
class SafeDiffHash:
    """Lowercase hexadecimal SHA-256 of a canonical safe diff summary."""

    hexadecimal: str

    @classmethod
    def parse(cls, value: str) -> SafeDiffHash:
        """Validate ``value`` as exactly 64 lowercase hexadecimal characters."""
        return cls(
            _parse_hex64(
                value,
                error_message="value does not satisfy the canonical safe diff hash contract",
            )
        )

    def __str__(self) -> str:
        return self.hexadecimal


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _format_utc_timestamp(timestamp: datetime) -> str:
    utc_timestamp = timestamp.astimezone(UTC)
    return f"{utc_timestamp:%Y-%m-%dT%H:%M:%S.%f}Z"


def _request_envelope(command: SourceVersionCommand) -> dict[str, object]:
    expected_object: ExpectedObject = command.expected_object
    actor: SourceActor = command.actor
    actor_id: UUID | None = actor.actor_id
    envelope: dict[str, object] = {
        "contract": REQUEST_CONTRACT,
        "workspace_id": str(command.workspace_id),
        "source_id": str(command.source_id),
        "event_id": str(command.event_id),
        "actor_kind": actor.actor_kind.value,
        "actor_id": None if actor_id is None else str(actor_id),
        "content_sha256": expected_object.content_digest.hexadecimal,
        "content_size_bytes": expected_object.size_bytes,
        "media_type": expected_object.media_type.value,
        "client_timestamp": (
            None
            if command.client_timestamp is None
            else _format_utc_timestamp(command.client_timestamp)
        ),
    }
    if isinstance(command, CreateSourceVersion):
        envelope["command_kind"] = "create"
        envelope["base_version_id"] = None
        envelope["source_type"] = command.source_type.value
        envelope["title"] = command.title.value
        if command.initial_locator is not None:
            envelope["contract"] = REQUEST_CONTRACT_WITH_INITIAL_LOCATOR
            envelope["initial_locator"] = command.initial_locator.value
    else:
        envelope["command_kind"] = "update"
        envelope["base_version_id"] = str(command.base_version_id)
        envelope["source_type"] = None
        envelope["title"] = None
    return envelope


def compute_request_fingerprint(command: SourceVersionCommand) -> RequestFingerprint:
    envelope = _request_envelope(command)
    return RequestFingerprint.parse(hashlib.sha256(_canonical_json_bytes(envelope)).hexdigest())


def compute_safe_diff_hash(
    source_id: UUID,
    base_version_id: UUID | None,
    base_content_sha256: ContentDigest | None,
    new_content_sha256: ContentDigest,
) -> SafeDiffHash:
    summary: dict[str, object] = {
        "contract": SAFE_DIFF_CONTRACT,
        "source_id": str(source_id),
        "base_version_id": None if base_version_id is None else str(base_version_id),
        "base_content_sha256": (
            None if base_content_sha256 is None else base_content_sha256.hexadecimal
        ),
        "new_content_sha256": new_content_sha256.hexadecimal,
    }
    return SafeDiffHash.parse(hashlib.sha256(_canonical_json_bytes(summary)).hexdigest())
