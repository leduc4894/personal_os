"""Ed25519 adapters binding the exclusion-policy signing ports to cryptography.

These tests prove the adapters are real RFC 8032 Ed25519 by replaying the
RFC 8032 section 7.1 test vectors through the ports, then verify the golden
snapshot/keyset fixtures end to end: the canonical payload bytes rebuild
exactly, the detached signature verifies under the pinned public key, and the
key ID equals the SHA-256-derived identifier of that key. Negative vectors
cover modified payload bytes, wrong-workspace payloads, wrong signing
domains, wrong keys, malformed signatures and malformed key IDs — every one
must answer a plain ``False`` without raising.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast
from uuid import UUID

import pytest
from api_runtime.exclusion_policy_crypto import (
    Ed25519PolicySigner,
    Ed25519PolicyVerifier,
)

from personal_os.exclusion_policy.contracts import (
    ExclusionPolicyRevision,
    RuleKind,
)
from personal_os.exclusion_policy.errors import PolicyContractError
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.signatures import (
    KEYSET_SIGNING_DOMAIN,
    SNAPSHOT_SIGNING_DOMAIN,
    PolicyKeysetKey,
    PolicyKeysetState,
    build_keyset_payload,
    build_signed_message,
    build_snapshot_payload,
    compute_payload_sha256_hex,
    decode_base64url_without_padding,
    derive_ed25519_key_id,
)

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "exclusion_policy"
SNAPSHOT_FIXTURE_PATH = FIXTURES_ROOT / "snapshot-golden.json"
KEYSET_FIXTURE_PATH = FIXTURES_ROOT / "keyset-golden.json"

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000101")
OTHER_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000102")
POLICY_REVISION_ID = UUID("018f47a0-7b00-7000-8000-000000000201")
PUBLISHED_AT = datetime(2026, 8, 17, 9, 30, 12, 123456, tzinfo=UTC)
CREATED_AT = datetime(2026, 8, 17, 10, 0, 0, 0, tzinfo=UTC)

#: Fixed synthetic signing seeds (never real secrets): the snapshot signer,
#: the keyset current key that signs the rotation keyset, and the staged key.
SNAPSHOT_SIGNING_SEED: Final[bytes] = bytes(range(32))
KEYSET_CURRENT_SIGNING_SEED: Final[bytes] = bytes(range(32, 64))
KEYSET_STAGED_SIGNING_SEED: Final[bytes] = bytes(range(64, 96))

#: RFC 8032 section 7.1 test vectors: seed, public key, message, signature.
RFC_8032_TEST_VECTORS: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
        "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da0"
        "85ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac1"
        "8ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
)


def load_fixture(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def snapshot_revision(workspace_id: UUID = WORKSPACE_ID) -> ExclusionPolicyRevision:
    """One revision covering every rule kind, deliberately unsorted by ID."""

    rules = (
        normalize_rule(
            UUID("018f47a0-7b00-7000-8000-000000000307"),
            RuleKind.SOURCE_TYPE,
            text_operand="youtube",
        ),
        normalize_rule(
            UUID("018f47a0-7b00-7000-8000-000000000304"),
            RuleKind.PATH_GLOB,
            text_operand="vault/**/draft-*.md",
        ),
        normalize_rule(
            UUID("018f47a0-7b00-7000-8000-000000000301"),
            RuleKind.EXACT_SOURCE_ID,
            source_id_operand=UUID("018f47a0-7b00-7000-8000-000000000401"),
        ),
        normalize_rule(
            UUID("018f47a0-7b00-7000-8000-000000000306"),
            RuleKind.MAXIMUM_SIZE,
            size_bytes_operand=8388608,
        ),
        normalize_rule(
            UUID("018f47a0-7b00-7000-8000-000000000302"),
            RuleKind.FOLDER_PREFIX,
            text_operand="archiv/berichte",
        ),
        normalize_rule(
            UUID("018f47a0-7b00-7000-8000-000000000305"),
            RuleKind.MEDIA_TYPE,
            text_operand="application/pdf",
        ),
        normalize_rule(
            UUID("018f47a0-7b00-7000-8000-000000000303"),
            RuleKind.EXTENSION,
            text_operand=".pdf",
        ),
    )
    return ExclusionPolicyRevision(
        policy_revision_id=POLICY_REVISION_ID,
        workspace_id=workspace_id,
        revision_number=1,
        rules=rules,
    )


def snapshot_payload_bytes() -> bytes:
    return build_snapshot_payload(
        snapshot_revision(),
        parent_policy_revision_id=None,
        published_at=PUBLISHED_AT,
    )


def snapshot_signature_envelope() -> dict[str, object]:
    return cast(dict[str, object], load_fixture(SNAPSHOT_FIXTURE_PATH)["signature"])


def snapshot_key_id() -> str:
    return cast(str, snapshot_signature_envelope()["key_id"])


def snapshot_signature() -> bytes:
    return decode_base64url_without_padding(cast(str, snapshot_signature_envelope()["value"]))


def snapshot_verifier() -> Ed25519PolicyVerifier:
    fixture = load_fixture(SNAPSHOT_FIXTURE_PATH)
    public_key = decode_base64url_without_padding(cast(str, fixture["signing_public_key"]))
    return Ed25519PolicyVerifier({snapshot_key_id(): public_key})


def golden_keyset_keys() -> tuple[PolicyKeysetKey, PolicyKeysetKey]:
    # Public keys of the fixed golden seeds, derived once and pinned here so
    # this vector shares exact key material with the crypto-free domain
    # golden tests and the committed fixtures.
    staged_public_key = bytes.fromhex(
        "2543b92ff1095511476adc8369db6ddc933665a11978dda1404ee1066ca9559d"
    )
    current_public_key = bytes.fromhex(
        "29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7"
    )
    return (
        PolicyKeysetKey(
            key_id=derive_ed25519_key_id(staged_public_key),
            public_key=staged_public_key,
            state=PolicyKeysetState.STAGED,
        ),
        PolicyKeysetKey(
            key_id=derive_ed25519_key_id(current_public_key),
            public_key=current_public_key,
            state=PolicyKeysetState.CURRENT,
        ),
    )


def build_golden_keyset_payload() -> bytes:
    return build_keyset_payload(
        workspace_id=WORKSPACE_ID,
        keyset_revision=2,
        parent_keyset_revision=1,
        created_at=CREATED_AT,
        keys=golden_keyset_keys(),
    )


# --- RFC 8032 conformance ------------------------------------------------------


@pytest.mark.parametrize(
    ("seed_hex", "public_hex", "message_hex", "signature_hex"),
    RFC_8032_TEST_VECTORS,
)
def test_rfc_8032_vectors_replay_through_the_ports(
    seed_hex: str, public_hex: str, message_hex: str, signature_hex: str
) -> None:
    signer = Ed25519PolicySigner.from_seed_bytes(bytes.fromhex(seed_hex))
    public_key = bytes.fromhex(public_hex)
    message = bytes.fromhex(message_hex)
    signature = bytes.fromhex(signature_hex)
    assert signer.sign(message) == signature
    assert signer.key_id == derive_ed25519_key_id(public_key)
    verifier = Ed25519PolicyVerifier({signer.key_id: public_key})
    assert verifier.verify(signer.key_id, signature, message)


def test_from_seed_bytes_rejects_wrong_seed_geometry() -> None:
    for wrong_seed in (b"", bytes(31), bytes(33), bytes(64)):
        with pytest.raises(PolicyContractError):
            Ed25519PolicySigner.from_seed_bytes(wrong_seed)


def test_verifier_construction_rejects_malformed_trust_anchors() -> None:
    key_id = derive_ed25519_key_id(bytes(32))
    malformed_anchors = ({"not-a-key-id": bytes(32)}, {key_id: bytes(31)}, {key_id: bytes(33)})
    for anchors in malformed_anchors:
        with pytest.raises(PolicyContractError):
            Ed25519PolicyVerifier(anchors)


# --- Golden snapshot and keyset vectors ----------------------------------------


def test_snapshot_golden_bytes_and_signature_are_stable() -> None:
    fixture = load_fixture(SNAPSHOT_FIXTURE_PATH)
    payload = snapshot_payload_bytes()
    assert payload == cast(str, fixture["payload"]).encode("utf-8")
    assert compute_payload_sha256_hex(payload) == fixture["payload_sha256"]

    signer = Ed25519PolicySigner.from_seed_bytes(SNAPSHOT_SIGNING_SEED)
    assert snapshot_signature_envelope()["algorithm"] == "Ed25519"
    assert signer.key_id == snapshot_key_id()
    signature = snapshot_signature()
    assert len(signature) == 64

    verifier = snapshot_verifier()
    message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload)
    assert signer.sign(message) == signature
    assert verifier.verify(signer.key_id, signature, message)


def test_keyset_golden_bytes_and_signature_are_stable() -> None:
    fixture = load_fixture(KEYSET_FIXTURE_PATH)
    payload = build_golden_keyset_payload()
    assert payload == cast(str, fixture["payload"]).encode("utf-8")
    assert compute_payload_sha256_hex(payload) == fixture["payload_sha256"]

    signer = Ed25519PolicySigner.from_seed_bytes(KEYSET_CURRENT_SIGNING_SEED)
    signature_envelope = cast(dict[str, object], fixture["signature"])
    assert signature_envelope["algorithm"] == "Ed25519"
    assert signer.key_id == signature_envelope["key_id"]
    signature = decode_base64url_without_padding(cast(str, signature_envelope["value"]))

    public_key = decode_base64url_without_padding(cast(str, fixture["signing_public_key"]))
    verifier = Ed25519PolicyVerifier({signer.key_id: public_key})
    message = build_signed_message(KEYSET_SIGNING_DOMAIN, payload)
    assert signer.sign(message) == signature
    assert verifier.verify(signer.key_id, signature, message)


# --- Negative vectors ----------------------------------------------------------


def test_signature_rejects_modified_payload_bytes() -> None:
    verifier = snapshot_verifier()
    key_id = snapshot_key_id()
    signature = snapshot_signature()
    original = snapshot_payload_bytes()
    message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, original)
    assert verifier.verify(key_id, signature, message)

    modified = original.replace(b'"revision_number":1', b'"revision_number":2')
    assert modified != original
    modified_message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, modified)
    assert not verifier.verify(key_id, signature, modified_message)


def test_signature_rejects_wrong_workspace_payload() -> None:
    verifier = snapshot_verifier()
    other_workspace_payload = build_snapshot_payload(
        snapshot_revision(workspace_id=OTHER_WORKSPACE_ID),
        parent_policy_revision_id=None,
        published_at=PUBLISHED_AT,
    )
    message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, other_workspace_payload)
    assert not verifier.verify(snapshot_key_id(), snapshot_signature(), message)


def test_signature_rejects_wrong_signing_domain() -> None:
    verifier = snapshot_verifier()
    message = build_signed_message(KEYSET_SIGNING_DOMAIN, snapshot_payload_bytes())
    assert not verifier.verify(snapshot_key_id(), snapshot_signature(), message)


def test_signature_rejects_the_wrong_key() -> None:
    staged_signer = Ed25519PolicySigner.from_seed_bytes(KEYSET_STAGED_SIGNING_SEED)
    # The staged key is a real, different trust anchor: the snapshot signature
    # must not verify under it, nor under any unknown key ID.
    staged_keys = golden_keyset_keys()
    staged_public_key = staged_keys[0].public_key
    assert derive_ed25519_key_id(staged_public_key) == staged_signer.key_id
    verifier = Ed25519PolicyVerifier({staged_signer.key_id: staged_public_key})
    message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, snapshot_payload_bytes())
    signature = snapshot_signature()
    assert not verifier.verify(staged_signer.key_id, signature, message)
    assert not verifier.verify("ed25519-sha256-" + "0" * 43, signature, message)


def test_verifier_rejects_malformed_signatures_and_key_ids() -> None:
    verifier = snapshot_verifier()
    key_id = snapshot_key_id()
    message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, snapshot_payload_bytes())
    for malformed_signature in (b"", bytes(63), bytes(65), bytes(64), b"\x00" * 64):
        assert not verifier.verify(key_id, malformed_signature, message)
    for malformed_key_id in ("", "not-a-key-id", key_id[:-1], key_id + "0"):
        assert not verifier.verify(malformed_key_id, bytes(64), message)
