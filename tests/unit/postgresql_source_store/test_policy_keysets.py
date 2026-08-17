"""Exclusion-policy keyset store pure-helper contracts of the PostgreSQL adapter.

These tests pin the keyset persistence helpers without touching a database:
the immutable envelope invariants (chain lineage, payload bounds, Ed25519
geometry, signature references), the row-value builders for signing keys,
keysets and signatures (payload hash derived from the canonical bytes, never
trusted from the caller), the hydration of persisted rows back into the
domain record, and the exact-replay classifier that accepts only an identical
keyset identity with an identical payload hash.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.events import SafeToken  # noqa: F401
from personal_os.exclusion_policy.ports import (
    PolicyKeysetEnvelope,
    PolicyKeysetRecord,
    PolicyKeysetSignatureRecord,
    PolicySigningKeyRecord,
)
from personal_os.exclusion_policy.signatures import (
    ED25519_PUBLIC_KEY_BYTES,
    ED25519_SIGNATURE_BYTES,
    compute_payload_sha256_hex,
)
from postgresql_source_store.policy_keysets import (
    build_keyset_signature_values,
    build_keyset_values,
    build_signing_key_values,
    classify_keyset_replay,
    hydrate_policy_keyset,
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000f1")
POLICY_KEYSET_ID = UUID("018f47a0-7b00-7000-8000-0000000000f2")
SIGNING_KEY_ID = UUID("018f47a0-7b00-7000-8000-0000000000f3")
SECOND_SIGNING_KEY_ID = UUID("018f47a0-7b00-7000-8000-0000000000f4")
USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000f5")
OCCURRED_AT = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)

_CURRENT_PUBLIC_KEY = bytes(range(ED25519_PUBLIC_KEY_BYTES))
_STAGED_PUBLIC_KEY = bytes(range(ED25519_PUBLIC_KEY_BYTES, ED25519_PUBLIC_KEY_BYTES * 2))
_CURRENT_SIGNATURE = bytes(range(ED25519_SIGNATURE_BYTES))
_STAGED_SIGNATURE = bytes(range(ED25519_SIGNATURE_BYTES, ED25519_SIGNATURE_BYTES * 2))
_PAYLOAD_BYTES = b'{"contract":"exclusion_policy_keyset/v1"}'


def _keys() -> tuple[PolicySigningKeyRecord, ...]:
    return (
        PolicySigningKeyRecord(signing_key_id=SIGNING_KEY_ID, public_key_bytes=_CURRENT_PUBLIC_KEY),
        PolicySigningKeyRecord(
            signing_key_id=SECOND_SIGNING_KEY_ID, public_key_bytes=_STAGED_PUBLIC_KEY
        ),
    )


def _signatures() -> tuple[PolicyKeysetSignatureRecord, ...]:
    return (
        PolicyKeysetSignatureRecord(
            signing_key_id=SIGNING_KEY_ID, signature_bytes=_CURRENT_SIGNATURE
        ),
        PolicyKeysetSignatureRecord(
            signing_key_id=SECOND_SIGNING_KEY_ID, signature_bytes=_STAGED_SIGNATURE
        ),
    )


def _envelope(
    *,
    keyset_revision: int = 2,
    parent_keyset_revision: int | None = 1,
    keys: tuple[PolicySigningKeyRecord, ...] | None = None,
    signatures: tuple[PolicyKeysetSignatureRecord, ...] | None = None,
    canonical_payload_bytes: bytes = _PAYLOAD_BYTES,
    policy_keyset_id: UUID = POLICY_KEYSET_ID,
    workspace_id: UUID = WORKSPACE_ID,
) -> PolicyKeysetEnvelope:
    return PolicyKeysetEnvelope(
        policy_keyset_id=policy_keyset_id,
        workspace_id=workspace_id,
        keyset_revision=keyset_revision,
        parent_keyset_revision=parent_keyset_revision,
        canonical_payload_bytes=canonical_payload_bytes,
        keys=_keys() if keys is None else keys,
        signatures=_signatures() if signatures is None else signatures,
        created_by_user_id=USER_ID,
    )


# --- immutable envelope invariants -----------------------------------------------


def test_envelope_accepts_valid_chain_shapes() -> None:
    first = _envelope(keyset_revision=1, parent_keyset_revision=None)
    assert first.keyset_revision == 1
    second = _envelope()
    assert second.parent_keyset_revision == 1


def test_envelope_rejects_broken_revision_lineage() -> None:
    with pytest.raises(ValueError):
        _envelope(keyset_revision=1, parent_keyset_revision=1)
    with pytest.raises(ValueError):
        _envelope(keyset_revision=2, parent_keyset_revision=None)
    with pytest.raises(ValueError):
        _envelope(keyset_revision=3, parent_keyset_revision=1)
    with pytest.raises(ValueError):
        _envelope(keyset_revision=0, parent_keyset_revision=None)


def test_envelope_rejects_oversized_or_empty_payload() -> None:
    with pytest.raises(ValueError):
        _envelope(canonical_payload_bytes=b"")
    with pytest.raises(ValueError):
        _envelope(canonical_payload_bytes=b"x" * (256 * 1024 + 1))


def test_envelope_rejects_wrong_ed25519_geometry() -> None:
    with pytest.raises(ValueError):
        _envelope(
            keys=(
                PolicySigningKeyRecord(
                    signing_key_id=SIGNING_KEY_ID,
                    public_key_bytes=_CURRENT_PUBLIC_KEY + b"\x00",
                ),
            )
        )
    with pytest.raises(ValueError):
        _envelope(
            signatures=(
                PolicyKeysetSignatureRecord(
                    signing_key_id=SIGNING_KEY_ID, signature_bytes=b"\x00"
                ),
            )
        )


def test_envelope_rejects_duplicate_keys_and_unknown_signature_references() -> None:
    duplicated = (
        PolicySigningKeyRecord(
            signing_key_id=SIGNING_KEY_ID, public_key_bytes=_CURRENT_PUBLIC_KEY
        ),
        PolicySigningKeyRecord(
            signing_key_id=SIGNING_KEY_ID, public_key_bytes=_STAGED_PUBLIC_KEY
        ),
    )
    with pytest.raises(ValueError):
        _envelope(keys=duplicated)
    unknown_reference = (
        PolicyKeysetSignatureRecord(
            signing_key_id=UUID("018f47a0-7b00-7000-8000-0000000000ff"),
            signature_bytes=_CURRENT_SIGNATURE,
        ),
    )
    with pytest.raises(ValueError):
        _envelope(signatures=unknown_reference)
    with pytest.raises(ValueError):
        _envelope(signatures=())


def test_envelope_rejects_nil_identities() -> None:
    with pytest.raises(ValueError):
        _envelope(policy_keyset_id=UUID(int=0))
    with pytest.raises(ValueError):
        _envelope(workspace_id=UUID(int=0))


# --- row-value builders -----------------------------------------------------------


def test_build_signing_key_values_derives_hash_free_public_metadata() -> None:
    values = build_signing_key_values(
        PolicySigningKeyRecord(
            signing_key_id=SIGNING_KEY_ID, public_key_bytes=_CURRENT_PUBLIC_KEY
        ),
        workspace_id=WORKSPACE_ID,
        introduced_keyset_revision=2,
        occurred_at=OCCURRED_AT,
    )
    assert values == {
        "signing_key_id": SIGNING_KEY_ID,
        "workspace_id": WORKSPACE_ID,
        "algorithm": "Ed25519",
        "public_key_bytes": _CURRENT_PUBLIC_KEY,
        "introduced_keyset_revision": 2,
        "created_at": OCCURRED_AT,
    }


def test_build_keyset_values_computes_payload_hash_from_canonical_bytes() -> None:
    envelope = _envelope()
    values = build_keyset_values(envelope, occurred_at=OCCURRED_AT)
    assert values["policy_keyset_id"] == POLICY_KEYSET_ID
    assert values["workspace_id"] == WORKSPACE_ID
    assert values["keyset_revision"] == 2
    assert values["parent_keyset_revision"] == 1
    assert values["canonical_payload_bytes"] == _PAYLOAD_BYTES
    assert values["payload_sha256"] == compute_payload_sha256_hex(_PAYLOAD_BYTES)
    assert values["created_by_user_id"] == USER_ID
    assert values["created_at"] == OCCURRED_AT


def test_build_keyset_signature_values_maps_identity_pairs() -> None:
    values = build_keyset_signature_values(
        POLICY_KEYSET_ID, _signatures()[0]
    )
    assert values == {
        "policy_keyset_id": POLICY_KEYSET_ID,
        "signing_key_id": SIGNING_KEY_ID,
        "signature_bytes": _CURRENT_SIGNATURE,
    }


# --- hydration and replay classification ------------------------------------------


def _keyset_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "policy_keyset_id": POLICY_KEYSET_ID,
        "workspace_id": WORKSPACE_ID,
        "keyset_revision": 2,
        "parent_keyset_revision": 1,
        "canonical_payload_bytes": _PAYLOAD_BYTES,
        "payload_sha256": compute_payload_sha256_hex(_PAYLOAD_BYTES),
        "created_by_user_id": USER_ID,
        "created_at": OCCURRED_AT,
    }
    row.update(overrides)
    return row


def _key_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "signing_key_id": SIGNING_KEY_ID,
        "workspace_id": WORKSPACE_ID,
        "algorithm": "Ed25519",
        "public_key_bytes": _CURRENT_PUBLIC_KEY,
        "introduced_keyset_revision": 1,
        "created_at": OCCURRED_AT,
    }
    row.update(overrides)
    return row


def _signature_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "policy_keyset_id": POLICY_KEYSET_ID,
        "signing_key_id": SIGNING_KEY_ID,
        "signature_bytes": _CURRENT_SIGNATURE,
    }
    row.update(overrides)
    return row


def test_hydrate_policy_keyset_round_trips_the_persisted_graph() -> None:
    record = hydrate_policy_keyset(
        _keyset_row(),
        [
            _key_row(),
            _key_row(
                signing_key_id=SECOND_SIGNING_KEY_ID, public_key_bytes=_STAGED_PUBLIC_KEY
            ),
        ],
        [
            _signature_row(),
            _signature_row(
                signing_key_id=SECOND_SIGNING_KEY_ID, signature_bytes=_STAGED_SIGNATURE
            ),
        ],
    )
    assert isinstance(record, PolicyKeysetRecord)
    assert record.policy_keyset_id == POLICY_KEYSET_ID
    assert record.keyset_revision == 2
    assert record.parent_keyset_revision == 1
    assert record.canonical_payload_bytes == _PAYLOAD_BYTES
    assert record.payload_sha256 == compute_payload_sha256_hex(_PAYLOAD_BYTES)
    assert record.keys == _keys()
    assert record.signatures == _signatures()
    assert record.created_at == OCCURRED_AT


def test_hydrate_policy_keyset_rejects_foreign_workspace_key_rows() -> None:
    with pytest.raises(ValueError):
        hydrate_policy_keyset(
            _keyset_row(),
            [_key_row(workspace_id=UUID("018f47a0-7b00-7000-8000-0000000000ee"))],
            [_signature_row()],
        )


def test_classify_keyset_replay_requires_identical_identity_and_hash() -> None:
    payload_hash = compute_payload_sha256_hex(_PAYLOAD_BYTES)
    assert classify_keyset_replay(_keyset_row(), POLICY_KEYSET_ID, payload_hash) is True
    assert (
        classify_keyset_replay(
            _keyset_row(policy_keyset_id=UUID("018f47a0-7b00-7000-8000-0000000000ee")),
            POLICY_KEYSET_ID,
            payload_hash,
        )
        is False
    )
    assert (
        classify_keyset_replay(
            _keyset_row(payload_sha256=compute_payload_sha256_hex(b"other")),
            POLICY_KEYSET_ID,
            payload_hash,
        )
        is False
    )
