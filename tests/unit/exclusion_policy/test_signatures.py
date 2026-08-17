"""Signed snapshot/keyset payload builders, message construction and crypto ports.

The builders are the only way canonical signed-policy bytes are produced: they
accept typed domain values (never route or database dictionaries), emit exact
RFC 8785 bytes with the field sets from spec sections 12 and 13, and enforce
the 256 KiB signed-snapshot envelope limit. Signatures here run through a
deterministic fake signer/verifier pair; the Ed25519 adapter vectors live in
``tests/unit/api_runtime/test_exclusion_policy_crypto.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Final, cast
from uuid import UUID

import pytest

from personal_os.exclusion_policy.contracts import (
    ExclusionPolicyRevision,
    RuleKind,
)
from personal_os.exclusion_policy.errors import PolicyContractError
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.signatures import (
    KEYSET_SIGNING_DOMAIN,
    SIGNED_SNAPSHOT_MAXIMUM_BYTES,
    SNAPSHOT_SIGNING_DOMAIN,
    PolicyKeysetKey,
    PolicyKeysetState,
    PolicySignatureVerifier,
    PolicySigner,
    build_keyset_payload,
    build_signed_message,
    build_snapshot_payload,
    compute_payload_sha256_hex,
    compute_signed_snapshot_envelope_size,
    decode_base64url_without_padding,
    derive_ed25519_key_id,
    encode_base64url_without_padding,
    is_wellformed_ed25519_key_id,
)

SNAPSHOT_FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "exclusion_policy" / "snapshot-golden.json"
)
KEYSET_FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "exclusion_policy" / "keyset-golden.json"
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000101")
POLICY_REVISION_ID = UUID("018f47a0-7b00-7000-8000-000000000201")

#: sha256("exclusion_policy_evaluator/v1"), pinned independently of the module.
EVALUATOR_CONTRACT_SHA256 = "8f174f9aa9a7a1580b377fa469a65c6e76801db66421404703b7aab38f50fbe1"

#: Domain separators are ASCII text joined to the payload by one 0x00 byte.
SNAPSHOT_DOMAIN_BYTES = b"exclusion-policy-snapshot/v1"
KEYSET_DOMAIN_BYTES = b"exclusion-policy-keyset/v1"

BASE64URL_BYTES_0_TO_63 = (
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0-Pw"
)


class RecordingFakeSigner(PolicySigner):
    """Deterministic hash-based signer double; records signed messages."""

    def __init__(self) -> None:
        self.signed_messages: list[bytes] = []

    @property
    def key_id(self) -> str:
        return "ed25519-sha256-" + "0" * 43

    def sign(self, message: bytes) -> bytes:
        self.signed_messages.append(message)
        return sha256(b"fake-signer" + message).digest()


class RecordingFakeVerifier(PolicySignatureVerifier):
    """Verifier double accepting exactly the messages the fake signer signed."""

    def __init__(self) -> None:
        self.verified: list[tuple[str, bytes, bytes]] = []

    def verify(self, key_id: str, signature: bytes, message: bytes) -> bool:
        self.verified.append((key_id, signature, message))
        return signature == sha256(b"fake-signer" + message).digest()


def one_rule_per_kind_revision() -> ExclusionPolicyRevision:
    """Revision whose rule IDs are deliberately not in sorted order."""

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
        workspace_id=WORKSPACE_ID,
        revision_number=1,
        rules=rules,
    )


PUBLISHED_AT = datetime(2026, 8, 17, 9, 30, 12, 123456, tzinfo=UTC)
CREATED_AT = datetime(2026, 8, 17, 10, 0, 0, 0, tzinfo=UTC)


def build_test_snapshot_payload() -> bytes:
    return build_snapshot_payload(
        one_rule_per_kind_revision(),
        parent_policy_revision_id=None,
        published_at=PUBLISHED_AT,
    )


# --- Message construction and key identifiers --------------------------------


def test_signed_message_is_domain_separator_then_nul_then_payload() -> None:
    payload = b'{"a":1}'
    assert build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload) == (
        SNAPSHOT_DOMAIN_BYTES + b"\x00" + payload
    )
    assert build_signed_message(KEYSET_SIGNING_DOMAIN, payload) == (
        KEYSET_DOMAIN_BYTES + b"\x00" + payload
    )


def test_key_id_is_prefixed_base64url_sha256_of_the_raw_public_key() -> None:
    public_key = bytes(32)
    assert derive_ed25519_key_id(public_key) == (
        "ed25519-sha256-Zmh6rfhivXdsj8GLjp-OIAiXFIVu4jOzkCpZHQ1fKSU"
    )
    assert is_wellformed_ed25519_key_id(derive_ed25519_key_id(public_key))
    assert len(derive_ed25519_key_id(public_key)) == len("ed25519-sha256-") + 43


def test_malformed_key_ids_fail_the_wellformedness_check() -> None:
    for malformed in (
        "",
        "ed25519-sha256-" + "0" * 42,
        "ed25519-sha256-" + "0" * 44,
        "ed25519-sha256-" + "0" * 42 + "+",
        "ed25519-sha256-" + "0" * 42 + "/",
        "ed25519-sha256-" + "0" * 43 + "=",
        "some-other-algorithm-" + "0" * 43,
    ):
        assert not is_wellformed_ed25519_key_id(malformed)


def test_payload_hash_is_lowercase_sha256_hex() -> None:
    payload = b'{"contract":"x"}'
    assert compute_payload_sha256_hex(payload) == sha256(payload).hexdigest()


def test_base64url_round_trip_rejects_standard_alphabet_and_padding() -> None:
    data = bytes(range(64))
    text = encode_base64url_without_padding(data)
    assert text == BASE64URL_BYTES_0_TO_63
    assert decode_base64url_without_padding(text) == data
    for malformed in (
        text + "=",
        text.replace("-", "+"),
        text[:-1] + "/",
        text + " ",
        "not base64url!",
        "",
    ):
        with pytest.raises(PolicyContractError):
            decode_base64url_without_padding(malformed)


# --- Snapshot payload ---------------------------------------------------------


def test_snapshot_payload_has_exactly_the_spec_12_field_set_in_jcs_order() -> None:
    payload = cast(dict[str, object], json.loads(build_test_snapshot_payload()))
    assert list(payload) == [
        "contract",
        "default_decision",
        "evaluator_contract_sha256",
        "parent_policy_revision_id",
        "policy_revision_id",
        "published_at",
        "revision_number",
        "rules",
        "workspace_id",
    ]
    assert payload["contract"] == "exclusion_policy_snapshot/v1"
    assert payload["default_decision"] == "allowed"
    assert payload["evaluator_contract_sha256"] == EVALUATOR_CONTRACT_SHA256
    assert payload["workspace_id"] == str(WORKSPACE_ID)
    assert payload["policy_revision_id"] == str(POLICY_REVISION_ID)
    assert payload["revision_number"] == 1
    assert payload["parent_policy_revision_id"] is None
    assert payload["published_at"] == "2026-08-17T09:30:12.123456Z"


def test_snapshot_rules_sort_by_rule_id_and_carry_one_named_operand_each() -> None:
    payload = cast(dict[str, object], json.loads(build_test_snapshot_payload()))
    rules = cast(Sequence[Mapping[str, object]], payload["rules"])
    assert [rule["rule_id"] for rule in rules] == sorted(rule["rule_id"] for rule in rules)
    by_kind = {rule["rule_kind"]: rule for rule in rules}
    assert set(by_kind) == {kind.value for kind in RuleKind}
    assert by_kind["exact_source_id"] == {
        "rule_id": "018f47a0-7b00-7000-8000-000000000301",
        "rule_kind": "exact_source_id",
        "source_id": "018f47a0-7b00-7000-8000-000000000401",
    }
    assert by_kind["folder_prefix"] == {
        "rule_id": "018f47a0-7b00-7000-8000-000000000302",
        "rule_kind": "folder_prefix",
        "folder_prefix": "archiv/berichte",
    }
    assert by_kind["path_glob"]["path_glob"] == "vault/**/draft-*.md"
    assert by_kind["extension"]["extension"] == ".pdf"
    assert by_kind["media_type"]["media_type"] == "application/pdf"
    assert by_kind["maximum_size"]["maximum_size_bytes"] == 8388608
    assert by_kind["source_type"]["source_type"] == "youtube"
    for rule in rules:
        assert len(rule) == 3


def test_snapshot_parent_revision_and_media_type_family_render_canonically() -> None:
    rules = (
        normalize_rule(
            UUID("018f47a0-7b00-7000-8000-000000000305"),
            RuleKind.MEDIA_TYPE,
            text_operand="image/*",
        ),
    )
    revision = ExclusionPolicyRevision(
        policy_revision_id=POLICY_REVISION_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=2,
        rules=rules,
    )
    payload = cast(
        dict[str, object],
        json.loads(
            build_snapshot_payload(
                revision,
                parent_policy_revision_id=POLICY_REVISION_ID,
                published_at=PUBLISHED_AT,
            )
        ),
    )
    assert payload["parent_policy_revision_id"] == str(POLICY_REVISION_ID)
    rule = cast(Mapping[str, object], cast(Sequence[object], payload["rules"])[0])
    assert rule["media_type"] == "image/*"


@pytest.mark.parametrize(
    "published_at",
    [
        datetime(2026, 8, 17, 9, 30, 12, 123456),
        datetime(2026, 8, 17, 9, 30, 12, 123456, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_snapshot_rejects_naive_and_non_utc_timestamps(published_at: datetime) -> None:
    revision = ExclusionPolicyRevision(
        policy_revision_id=POLICY_REVISION_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=1,
    )
    with pytest.raises(PolicyContractError):
        build_snapshot_payload(
            revision,
            parent_policy_revision_id=None,
            published_at=published_at,
        )


def test_snapshot_envelope_size_counts_payload_plus_signature_envelope() -> None:
    payload = build_test_snapshot_payload()
    envelope_size = compute_signed_snapshot_envelope_size(payload)
    assert envelope_size == 11 + len(payload) + len(
        ',"payload_sha256":"' + "0" * 64 + '","signature":{"algorithm":"Ed25519","key_id":"'
        "ed25519-sha256-" + "0" * 43 + '","value":"' + "0" * 86 + '"}}'
    )


def test_typical_snapshot_envelope_fits_the_256_kib_limit() -> None:
    assert compute_signed_snapshot_envelope_size(build_test_snapshot_payload()) <= (
        SIGNED_SNAPSHOT_MAXIMUM_BYTES
    )


def test_oversized_snapshot_is_rejected_before_signing() -> None:
    # 16 segments of 250 ASCII bytes = 4,015 bytes: a valid locator whose
    # repeated rules push the canonical payload far past the 256 KiB cap.
    segment = "a" * 250
    folder_prefix = "/".join([segment] * 16)
    rules = tuple(
        normalize_rule(
            UUID(int=0x018F47A07B0070008000000000000000 + index),
            RuleKind.FOLDER_PREFIX,
            text_operand=f"{folder_prefix}/{index:04d}",
        )
        for index in range(200)
    )
    revision = ExclusionPolicyRevision(
        policy_revision_id=POLICY_REVISION_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=1,
        rules=rules,
    )
    with pytest.raises(PolicyContractError):
        build_snapshot_payload(
            revision,
            parent_policy_revision_id=None,
            published_at=PUBLISHED_AT,
        )


# --- Keyset payload -----------------------------------------------------------


def keyset_keys() -> tuple[PolicyKeysetKey, PolicyKeysetKey]:
    current_public_key = bytes(32)
    staged_public_key = bytes(range(32, 64))
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


def build_test_keyset_payload() -> bytes:
    return build_keyset_payload(
        workspace_id=WORKSPACE_ID,
        keyset_revision=2,
        parent_keyset_revision=1,
        created_at=CREATED_AT,
        keys=keyset_keys(),
    )


def test_keyset_payload_has_exactly_the_spec_13_field_set_in_jcs_order() -> None:
    payload = cast(dict[str, object], json.loads(build_test_keyset_payload()))
    assert list(payload) == [
        "contract",
        "created_at",
        "keys",
        "keyset_revision",
        "parent_keyset_revision",
        "workspace_id",
    ]
    assert payload["contract"] == "exclusion_policy_keyset/v1"
    assert payload["workspace_id"] == str(WORKSPACE_ID)
    assert payload["keyset_revision"] == 2
    assert payload["parent_keyset_revision"] == 1
    assert payload["created_at"] == "2026-08-17T10:00:00.000000Z"


def test_keyset_keys_sort_by_key_id_and_pin_algorithm_and_state() -> None:
    payload = cast(dict[str, object], json.loads(build_test_keyset_payload()))
    keys = cast(Sequence[Mapping[str, object]], payload["keys"])
    assert [key["key_id"] for key in keys] == sorted(key["key_id"] for key in keys)
    for key in keys:
        assert list(key) == ["algorithm", "key_id", "public_key", "state"]
        assert key["algorithm"] == "Ed25519"
        assert is_wellformed_ed25519_key_id(cast(str, key["key_id"]))
        assert key["state"] in ("current", "staged", "retired")
    by_state = {key["state"]: key for key in keys}
    assert by_state["current"]["public_key"] == encode_base64url_without_padding(bytes(32))
    assert by_state["staged"]["public_key"] == encode_base64url_without_padding(
        bytes(range(32, 64))
    )


def test_keyset_revision_one_has_null_parent() -> None:
    payload = build_keyset_payload(
        workspace_id=WORKSPACE_ID,
        keyset_revision=1,
        parent_keyset_revision=None,
        created_at=CREATED_AT,
        keys=keyset_keys()[:1],
    )
    assert cast(dict[str, object], json.loads(payload))["parent_keyset_revision"] is None


@pytest.mark.parametrize(
    ("revision_number", "parent"),
    [(0, None), (1, 1), (2, 2), (3, 5)],
)
def test_keyset_rejects_invalid_revision_chain(revision_number: int, parent: int | None) -> None:
    with pytest.raises(PolicyContractError):
        build_keyset_payload(
            workspace_id=WORKSPACE_ID,
            keyset_revision=revision_number,
            parent_keyset_revision=parent,
            created_at=CREATED_AT,
            keys=keyset_keys()[:1],
        )


def test_keyset_rejects_duplicate_key_ids() -> None:
    key, _ = keyset_keys()
    with pytest.raises(PolicyContractError):
        build_keyset_payload(
            workspace_id=WORKSPACE_ID,
            keyset_revision=2,
            parent_keyset_revision=1,
            created_at=CREATED_AT,
            keys=(key, key),
        )


def test_keyset_rejects_more_than_one_current_key() -> None:
    staged, current = keyset_keys()
    second_current = replace(staged, state=PolicyKeysetState.CURRENT)
    with pytest.raises(PolicyContractError):
        build_keyset_payload(
            workspace_id=WORKSPACE_ID,
            keyset_revision=2,
            parent_keyset_revision=1,
            created_at=CREATED_AT,
            keys=(current, second_current),
        )


def test_keyset_rejects_more_than_four_non_retired_keys() -> None:
    keys = tuple(
        PolicyKeysetKey(
            key_id=derive_ed25519_key_id(bytes([index]) + bytes(31)),
            public_key=bytes([index]) + bytes(31),
            state=PolicyKeysetState.STAGED,
        )
        for index in range(5)
    )
    with pytest.raises(PolicyContractError):
        build_keyset_payload(
            workspace_id=WORKSPACE_ID,
            keyset_revision=2,
            parent_keyset_revision=1,
            created_at=CREATED_AT,
            keys=keys,
        )


def test_keyset_allows_five_keys_when_only_four_are_non_retired() -> None:
    keys = tuple(
        PolicyKeysetKey(
            key_id=derive_ed25519_key_id(bytes([index]) + bytes(31)),
            public_key=bytes([index]) + bytes(31),
            state=(
                PolicyKeysetState.RETIRED
                if index == 0
                else (PolicyKeysetState.CURRENT if index == 1 else PolicyKeysetState.STAGED)
            ),
        )
        for index in range(5)
    )
    payload = build_keyset_payload(
        workspace_id=WORKSPACE_ID,
        keyset_revision=2,
        parent_keyset_revision=1,
        created_at=CREATED_AT,
        keys=keys,
    )
    assert len(cast(Sequence[object], cast(dict[str, object], json.loads(payload))["keys"])) == 5


@pytest.mark.parametrize(
    "key",
    [
        PolicyKeysetKey(
            key_id="not-a-key-id",
            public_key=bytes(32),
            state=PolicyKeysetState.CURRENT,
        ),
        PolicyKeysetKey(
            key_id="ed25519-sha256-" + "0" * 43,
            public_key=bytes(31),
            state=PolicyKeysetState.CURRENT,
        ),
    ],
)
def test_keyset_rejects_malformed_key_material(key: PolicyKeysetKey) -> None:
    with pytest.raises(PolicyContractError):
        build_keyset_payload(
            workspace_id=WORKSPACE_ID,
            keyset_revision=1,
            parent_keyset_revision=None,
            created_at=CREATED_AT,
            keys=(key,),
        )


def test_keyset_rejects_naive_or_non_utc_creation_time() -> None:
    for created_at in (
        datetime(2026, 8, 17, 10, 0, 0, 0),
        datetime(2026, 8, 17, 10, 0, 0, 0, tzinfo=timezone(timedelta(hours=3))),
    ):
        with pytest.raises(PolicyContractError):
            build_keyset_payload(
                workspace_id=WORKSPACE_ID,
                keyset_revision=1,
                parent_keyset_revision=None,
                created_at=created_at,
                keys=keyset_keys()[:1],
            )


# --- Ports and signing flow ---------------------------------------------------


def test_signer_port_signs_the_domain_separated_snapshot_message() -> None:
    signer = RecordingFakeSigner()
    verifier = RecordingFakeVerifier()
    payload = build_test_snapshot_payload()
    signature = signer.sign(build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload))
    assert verifier.verify(
        signer.key_id,
        signature,
        build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload),
    )
    assert not verifier.verify(
        signer.key_id,
        signature,
        build_signed_message(KEYSET_SIGNING_DOMAIN, payload),
    )


def test_canonical_payload_bytes_are_stable_across_calls() -> None:
    assert build_test_snapshot_payload() == build_test_snapshot_payload()
    assert build_test_keyset_payload() == build_test_keyset_payload()
    assert compute_payload_sha256_hex(build_test_snapshot_payload()) == (
        compute_payload_sha256_hex(build_test_snapshot_payload())
    )


# --- Golden fixtures -----------------------------------------------------------

#: Public keys of the fixed synthetic golden signing seeds, derived once and
#: pinned as raw bytes so these crypto-free tests share exact key material
#: with the Ed25519 adapter vectors in ``tests/unit/api_runtime``.
GOLDEN_SNAPSHOT_PUBLIC_KEY: Final[bytes] = bytes.fromhex(
    "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
)
GOLDEN_KEYSET_CURRENT_PUBLIC_KEY: Final[bytes] = bytes.fromhex(
    "29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7"
)
GOLDEN_KEYSET_STAGED_PUBLIC_KEY: Final[bytes] = bytes.fromhex(
    "2543b92ff1095511476adc8369db6ddc933665a11978dda1404ee1066ca9559d"
)


def build_golden_keyset_payload() -> bytes:
    keys = (
        PolicyKeysetKey(
            key_id=derive_ed25519_key_id(GOLDEN_KEYSET_STAGED_PUBLIC_KEY),
            public_key=GOLDEN_KEYSET_STAGED_PUBLIC_KEY,
            state=PolicyKeysetState.STAGED,
        ),
        PolicyKeysetKey(
            key_id=derive_ed25519_key_id(GOLDEN_KEYSET_CURRENT_PUBLIC_KEY),
            public_key=GOLDEN_KEYSET_CURRENT_PUBLIC_KEY,
            state=PolicyKeysetState.CURRENT,
        ),
    )
    return build_keyset_payload(
        workspace_id=WORKSPACE_ID,
        keyset_revision=2,
        parent_keyset_revision=1,
        created_at=CREATED_AT,
        keys=keys,
    )


def test_snapshot_golden_fixture_pins_canonical_bytes_and_hash() -> None:
    fixture = cast(dict[str, object], json.loads(SNAPSHOT_FIXTURE_PATH.read_text("utf-8")))
    payload = build_test_snapshot_payload()
    assert payload == cast(str, fixture["payload"]).encode("utf-8")
    assert compute_payload_sha256_hex(payload) == fixture["payload_sha256"]
    signature = cast(dict[str, object], fixture["signature"])
    assert signature["algorithm"] == "Ed25519"
    assert signature["key_id"] == derive_ed25519_key_id(GOLDEN_SNAPSHOT_PUBLIC_KEY)


def test_keyset_golden_fixture_pins_canonical_bytes_and_hash() -> None:
    fixture = cast(dict[str, object], json.loads(KEYSET_FIXTURE_PATH.read_text("utf-8")))
    payload = build_golden_keyset_payload()
    assert payload == cast(str, fixture["payload"]).encode("utf-8")
    assert compute_payload_sha256_hex(payload) == fixture["payload_sha256"]
    signature = cast(dict[str, object], fixture["signature"])
    assert signature["algorithm"] == "Ed25519"
    assert signature["key_id"] == derive_ed25519_key_id(GOLDEN_KEYSET_CURRENT_PUBLIC_KEY)
