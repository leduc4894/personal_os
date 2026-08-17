"""Signed snapshot/keyset payload builders, message construction and crypto ports.

This module is the only producer of canonical signed-policy bytes. The typed
builders accept domain values — never route payloads or database rows — and
emit the exact RFC 8785 field sets of spec sections 12 and 13. Signatures run
Ed25519 (RFC 8032) over the domain-separated message
``ASCII(domain) || 0x00 || JCS_UTF8(payload)``; the domain separators, key-ID
derivation, base64url grammar and the 256 KiB signed-snapshot ceiling are
pinned here so every implementation shares one byte-level contract.

The module pins ports only: :class:`PolicySigner` and
:class:`PolicySignatureVerifier` are protocols the API composition root binds
to Ed25519 adapters. No cryptography library, provider SDK or secret value is
imported or stored here; private keys never enter this package.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol
from uuid import UUID

from personal_os.exclusion_policy.canonical_json import (
    CanonicalJsonValue,
    canonicalize_json_value,
)
from personal_os.exclusion_policy.contracts import (
    EVALUATOR_CONTRACT,
    ExactSourceIdOperand,
    ExclusionPolicyRevision,
    ExclusionRule,
    ExtensionOperand,
    FolderPrefixOperand,
    MaximumSizeOperand,
    MediaTypeOperand,
    PathGlobOperand,
    SourceTypeOperand,
)
from personal_os.exclusion_policy.errors import (
    KEYSET_CURRENT_COUNT_INVALID,
    KEYSET_KEY_DUPLICATE,
    KEYSET_KEY_INVALID,
    KEYSET_NON_RETIRED_COUNT_INVALID,
    KEYSET_REVISION_INVALID,
    PARENT_REVISION_INVALID,
    PAYLOAD_BASE64URL_INVALID,
    PAYLOAD_OVERSIZED,
    PAYLOAD_SIGNING_DOMAIN_INVALID,
    PAYLOAD_TIMESTAMP_INVALID,
    PAYLOAD_VALUE_UNSUPPORTED,
    PAYLOAD_WORKSPACE_INVALID,
    SIGNING_KEY_ID_INVALID,
    SIGNING_KEY_SIZE_INVALID,
    payload_contract_error,
)
from personal_os.sources.actors import reject_nil_uuid

#: Payload contract tags (spec 12 and 13).
SNAPSHOT_PAYLOAD_CONTRACT: Final[str] = "exclusion_policy_snapshot/v1"
KEYSET_PAYLOAD_CONTRACT: Final[str] = "exclusion_policy_keyset/v1"

#: ASCII domain separators prefixed to every signed message, joined to the
#: payload bytes by one 0x00 byte.
SNAPSHOT_SIGNING_DOMAIN: Final[str] = "exclusion-policy-snapshot/v1"
KEYSET_SIGNING_DOMAIN: Final[str] = "exclusion-policy-keyset/v1"
SIGNING_DOMAINS: Final[frozenset[str]] = frozenset({SNAPSHOT_SIGNING_DOMAIN, KEYSET_SIGNING_DOMAIN})

#: Hard ceiling on the complete encoded signed-snapshot response (spec 12).
SIGNED_SNAPSHOT_MAXIMUM_BYTES: Final[int] = 256 * 1024

#: Ed25519 material geometry (RFC 8032): 32-byte public keys and seeds,
#: 64-byte signatures; the base64url-without-padding lengths follow.
ED25519_PUBLIC_KEY_BYTES: Final[int] = 32
ED25519_SEED_BYTES: Final[int] = 32
ED25519_SIGNATURE_BYTES: Final[int] = 64
SIGNATURE_ALGORITHM: Final[str] = "Ed25519"
KEY_ID_PREFIX: Final[str] = "ed25519-sha256-"

#: Keyset chain ceilings (spec 13.3): one current key, at most four
#: non-retired keys in the latest keyset.
KEYSET_MAXIMUM_NON_RETIRED_KEYS: Final[int] = 4

_BASE64URL_SHA256_LENGTH: Final[int] = 43
_BASE64URL_SIGNATURE_LENGTH: Final[int] = 86
_HEX_SHA256_LENGTH: Final[int] = 64
_ZERO_UTC_OFFSET: Final[timedelta] = timedelta(0)

_BASE64URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
_KEY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^{KEY_ID_PREFIX}[A-Za-z0-9_-]{{{_BASE64URL_SHA256_LENGTH}}}$"
)

# Fixed segments of the persisted signed-snapshot envelope; the size check
# composes them so the ceiling covers the complete encoded response.
_ENVELOPE_PAYLOAD_PREFIX_BYTES: Final[bytes] = b'{"payload":'
_ENVELOPE_HASH_SEGMENT_BYTES: Final[bytes] = b',"payload_sha256":"'
_ENVELOPE_SIGNATURE_SEGMENT_BYTES: Final[bytes] = (
    b'","signature":{"algorithm":"' + SIGNATURE_ALGORITHM.encode("ascii") + b'","key_id":"'
)
_ENVELOPE_VALUE_SEGMENT_BYTES: Final[bytes] = b'","value":"'
_ENVELOPE_SUFFIX_BYTES: Final[bytes] = b'"}}'


class PolicyKeysetState(StrEnum):
    """Closed key lifecycle states inside one keyset revision (spec 13.3)."""

    CURRENT = "current"
    STAGED = "staged"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class PolicyKeysetKey:
    """One public trust-anchor entry: derived key ID, raw key, lifecycle state.

    The record is deliberately inert; :func:`build_keyset_payload` enforces
    the closed key material contract — a well-formed ``key_id`` equal to the
    SHA-256-derived identifier of exactly 32 raw public-key bytes — before any
    byte is emitted.
    """

    key_id: str
    public_key: bytes
    state: PolicyKeysetState


class PolicySigner(Protocol):
    """Port signing one domain-separated message with the current policy key."""

    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class PolicySignatureVerifier(Protocol):
    """Port verifying one signature over a message under a known key ID."""

    def verify(self, key_id: str, signature: bytes, message: bytes) -> bool: ...


def build_signed_message(domain: str, payload: bytes) -> bytes:
    """Join the ASCII domain separator, one 0x00 byte and the payload bytes.

    The separator is the domain-separation prefix every signature and every
    cross-signature covers; only the two closed policy domains are accepted so
    no caller can mint a new signing context.
    """

    if domain not in SIGNING_DOMAINS:
        raise payload_contract_error(PAYLOAD_SIGNING_DOMAIN_INVALID)
    return domain.encode("ascii") + b"\x00" + payload


def encode_base64url_without_padding(data: bytes) -> str:
    """Encode raw bytes as base64url with the padding characters stripped."""

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_base64url_without_padding(text: str) -> bytes:
    """Decode strict base64url text: URL alphabet only, no padding, no gaps.

    Standard-alphabet ``+``/``/``, ``=`` padding, whitespace,
    padding-impossible lengths and the empty string are all rejected.
    """

    if not text or len(text) % 4 == 1 or _BASE64URL_PATTERN.fullmatch(text) is None:
        raise payload_contract_error(PAYLOAD_BASE64URL_INVALID)
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def derive_ed25519_key_id(public_key: bytes) -> str:
    """Derive ``ed25519-sha256-BASE64URL`` from one raw 32-byte public key."""

    if len(public_key) != ED25519_PUBLIC_KEY_BYTES:
        raise payload_contract_error(SIGNING_KEY_SIZE_INVALID)
    digest = sha256(public_key).digest()
    return KEY_ID_PREFIX + encode_base64url_without_padding(digest)


def is_wellformed_ed25519_key_id(key_id: str) -> bool:
    """Check the closed key-ID grammar: prefix plus 43 base64url characters."""

    return _KEY_ID_PATTERN.fullmatch(key_id) is not None


def compute_payload_sha256_hex(payload: bytes) -> str:
    """Hash exactly the canonical payload bytes to lowercase hex (spec 12)."""

    return sha256(payload).hexdigest()


def compute_signed_snapshot_envelope_size(payload_bytes: bytes) -> int:
    """Size of the complete encoded signed-snapshot response for a payload.

    The persisted envelope wraps the payload with the fixed-length members
    ``payload_sha256`` (64 hex characters) and ``signature`` (the algorithm
    tag, the 58-character key ID and the 86-character base64url signature
    value), so the response size is the payload length plus one fixed
    overhead of 300 bytes.
    """

    return (
        len(_ENVELOPE_PAYLOAD_PREFIX_BYTES)
        + len(payload_bytes)
        + len(_ENVELOPE_HASH_SEGMENT_BYTES)
        + _HEX_SHA256_LENGTH
        + len(_ENVELOPE_SIGNATURE_SEGMENT_BYTES)
        + len(KEY_ID_PREFIX)
        + _BASE64URL_SHA256_LENGTH
        + len(_ENVELOPE_VALUE_SEGMENT_BYTES)
        + _BASE64URL_SIGNATURE_LENGTH
        + len(_ENVELOPE_SUFFIX_BYTES)
    )


def _format_utc_timestamp(moment: datetime) -> str:
    """Render one UTC instant with exactly six fractional digits and ``Z``."""

    if moment.tzinfo is None or moment.utcoffset() != _ZERO_UTC_OFFSET:
        raise payload_contract_error(PAYLOAD_TIMESTAMP_INVALID)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _render_rule(rule: ExclusionRule) -> dict[str, CanonicalJsonValue]:
    """Render one rule as ``rule_id``, ``rule_kind`` and its single operand."""

    rendered: dict[str, CanonicalJsonValue] = {
        "rule_id": str(rule.rule_id),
        "rule_kind": rule.rule_kind.value,
    }
    operand = rule.operand
    if isinstance(operand, ExactSourceIdOperand):
        rendered["source_id"] = str(operand.source_id)
    elif isinstance(operand, FolderPrefixOperand):
        rendered["folder_prefix"] = operand.folder_prefix
    elif isinstance(operand, PathGlobOperand):
        rendered["path_glob"] = operand.normalized_pattern
    elif isinstance(operand, ExtensionOperand):
        rendered["extension"] = operand.extension
    elif isinstance(operand, MediaTypeOperand):
        exact = operand.exact_media_type
        rendered["media_type"] = exact.value if exact is not None else f"{operand.family_type}/*"
    elif isinstance(operand, MaximumSizeOperand):
        rendered["maximum_size_bytes"] = operand.maximum_size_bytes
    elif isinstance(operand, SourceTypeOperand):
        rendered["source_type"] = operand.source_type.value
    else:
        raise payload_contract_error(PAYLOAD_VALUE_UNSUPPORTED)
    return rendered


def build_snapshot_payload(
    revision: ExclusionPolicyRevision,
    *,
    parent_policy_revision_id: UUID | None,
    published_at: datetime,
) -> bytes:
    """Build the canonical signed-snapshot payload bytes (spec 12).

    The payload carries exactly the closed field set — contract tag, workspace
    and revision identities, revision number, parent identity or ``null``,
    publication instant, the constant allow-by-default decision, the evaluator
    contract hash and the rules sorted by lowercase textual ``rule_id``, each
    with ``rule_id``, ``rule_kind`` and exactly one named typed operand. The
    complete signed envelope must fit the 256 KiB ceiling or the build fails
    before any signature exists.
    """

    if parent_policy_revision_id is not None:
        try:
            reject_nil_uuid("parent_policy_revision_id", parent_policy_revision_id)
        except ValueError:
            raise payload_contract_error(PARENT_REVISION_INVALID) from None
    ordered_rules = sorted(revision.rules, key=lambda rule: str(rule.rule_id))
    payload: dict[str, CanonicalJsonValue] = {
        "contract": SNAPSHOT_PAYLOAD_CONTRACT,
        "workspace_id": str(revision.workspace_id),
        "policy_revision_id": str(revision.policy_revision_id),
        "revision_number": revision.revision_number,
        "parent_policy_revision_id": (
            None if parent_policy_revision_id is None else str(parent_policy_revision_id)
        ),
        "published_at": _format_utc_timestamp(published_at),
        "default_decision": "allowed",
        "evaluator_contract_sha256": sha256(EVALUATOR_CONTRACT.encode("ascii")).hexdigest(),
        "rules": tuple(_render_rule(rule) for rule in ordered_rules),
    }
    payload_bytes = canonicalize_json_value(payload)
    envelope_size = compute_signed_snapshot_envelope_size(payload_bytes)
    if envelope_size > SIGNED_SNAPSHOT_MAXIMUM_BYTES:
        raise payload_contract_error(PAYLOAD_OVERSIZED)
    return payload_bytes


def _validate_keyset_key(key: PolicyKeysetKey) -> None:
    """Enforce the closed key material contract before rendering."""

    if not is_wellformed_ed25519_key_id(key.key_id):
        raise payload_contract_error(SIGNING_KEY_ID_INVALID)
    if len(key.public_key) != ED25519_PUBLIC_KEY_BYTES:
        raise payload_contract_error(SIGNING_KEY_SIZE_INVALID)
    if key.key_id != derive_ed25519_key_id(key.public_key):
        raise payload_contract_error(KEYSET_KEY_INVALID)


def build_keyset_payload(
    *,
    workspace_id: UUID,
    keyset_revision: int,
    parent_keyset_revision: int | None,
    created_at: datetime,
    keys: tuple[PolicyKeysetKey, ...],
) -> bytes:
    """Build the canonical keyset payload bytes (spec 13.3).

    The payload is workspace-bound and carries exactly the closed field set:
    contract tag, workspace identity, keyset revision, immediate-parent
    revision (``null`` only for the self-signed revision 1), creation instant
    and the keys sorted by ``key_id``, each pinning the algorithm, its derived
    key ID, the raw public key as base64url and its lifecycle state. The
    rotation ceilings hold: at most one current key, at most four non-retired
    keys and no duplicate key IDs.
    """

    try:
        reject_nil_uuid("workspace_id", workspace_id)
    except ValueError:
        raise payload_contract_error(PAYLOAD_WORKSPACE_INVALID) from None
    if keyset_revision < 1:
        raise payload_contract_error(KEYSET_REVISION_INVALID)
    if keyset_revision == 1:
        if parent_keyset_revision is not None:
            raise payload_contract_error(KEYSET_REVISION_INVALID)
    elif parent_keyset_revision != keyset_revision - 1:
        raise payload_contract_error(KEYSET_REVISION_INVALID)

    seen_key_ids: set[str] = set()
    current_count = 0
    non_retired_count = 0
    for key in keys:
        _validate_keyset_key(key)
        if key.key_id in seen_key_ids:
            raise payload_contract_error(KEYSET_KEY_DUPLICATE)
        seen_key_ids.add(key.key_id)
        if key.state is PolicyKeysetState.CURRENT:
            current_count += 1
        if key.state is not PolicyKeysetState.RETIRED:
            non_retired_count += 1
    if current_count > 1:
        raise payload_contract_error(KEYSET_CURRENT_COUNT_INVALID)
    if non_retired_count > KEYSET_MAXIMUM_NON_RETIRED_KEYS:
        raise payload_contract_error(KEYSET_NON_RETIRED_COUNT_INVALID)

    def render_key(key: PolicyKeysetKey) -> dict[str, CanonicalJsonValue]:
        return {
            "algorithm": SIGNATURE_ALGORITHM,
            "key_id": key.key_id,
            "public_key": encode_base64url_without_padding(key.public_key),
            "state": key.state.value,
        }

    ordered_keys = sorted(keys, key=lambda key: key.key_id)
    payload: dict[str, CanonicalJsonValue] = {
        "contract": KEYSET_PAYLOAD_CONTRACT,
        "workspace_id": str(workspace_id),
        "keyset_revision": keyset_revision,
        "parent_keyset_revision": parent_keyset_revision,
        "created_at": _format_utc_timestamp(created_at),
        "keys": tuple(render_key(key) for key in ordered_keys),
    }
    return canonicalize_json_value(payload)
