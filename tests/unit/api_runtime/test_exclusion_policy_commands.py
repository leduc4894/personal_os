"""Policy signing-key lifecycle command contracts without a database.

These tests pin the offline surface of the ``policy-key`` lifecycle CLI: the
operator-input validators (workspace UUID, exact relative key-file names,
derived key-ID grammar), the create-or-import secret-file boundary that never
overwrites existing bytes, the pure keyset transition planners (initialize /
stage / activate / retire with their typed refusal reasons), the cross-signed
envelope construction — including the refusal to build an activation envelope
without both old-current and newly-current signatures — and the closed result
rendering that prints IDs and status only, never key bytes or signatures.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from api_runtime.exclusion_policy_commands import (
    PolicyKeyCommandInputError,
    PolicyKeyLifecycleOutcome,
    build_lifecycle_keyset_envelope,
    create_or_load_policy_signing_key,
    load_existing_policy_signing_key,
    parse_policy_keyset_payload,
    plan_activated_keys,
    plan_initialized_keys,
    plan_retired_keys,
    plan_staged_keys,
    render_policy_key_outcome,
    validate_policy_key_file_name,
    validate_policy_key_workspace_id,
    validate_policy_signing_key_id_text,
)
from api_runtime.exclusion_policy_crypto import Ed25519PolicyVerifier
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.ports import PolicyKeysetEnvelope, PolicySigningKeyRecord
from personal_os.exclusion_policy.signatures import (
    KEYSET_SIGNING_DOMAIN,
    PolicyKeysetState,
    build_keyset_payload,
    build_signed_message,
    derive_ed25519_key_id,
)
from personal_os.runtime_configuration.secret_files import read_secret_file

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000d1")
CREATED_AT = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)

_CURRENT_PUBLIC_KEY = bytes(range(32))
_STAGED_PUBLIC_KEY = bytes(range(32, 64))
_THIRD_PUBLIC_KEY = bytes(range(64, 96))
_CURRENT_KEY_ID = derive_ed25519_key_id(_CURRENT_PUBLIC_KEY)
_STAGED_KEY_ID = derive_ed25519_key_id(_STAGED_PUBLIC_KEY)
_THIRD_KEY_ID = derive_ed25519_key_id(_THIRD_PUBLIC_KEY)

_CURRENT_ROW_ID = UUID("018f47a0-7b00-7000-8000-0000000000e1")
_STAGED_ROW_ID = UUID("018f47a0-7b00-7000-8000-0000000000e2")


class SignerDouble:
    """Ed25519-backed signer double exposing the PolicySigner port shape."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self.public_key = private_key.public_key().public_bytes_raw()

    @property
    def key_id(self) -> str:
        return derive_ed25519_key_id(self.public_key)

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message)


def _generate_signer() -> SignerDouble:
    return SignerDouble(Ed25519PrivateKey.generate())


def _payload_bytes(
    *, states: tuple[tuple[bytes, PolicyKeysetState], ...], revision: int = 1
) -> bytes:
    from personal_os.exclusion_policy.signatures import PolicyKeysetKey

    keys = tuple(
        PolicyKeysetKey(key_id=derive_ed25519_key_id(public), public_key=public, state=state)
        for public, state in states
    )
    return build_keyset_payload(
        workspace_id=WORKSPACE_ID,
        keyset_revision=revision,
        parent_keyset_revision=None if revision == 1 else revision - 1,
        created_at=CREATED_AT,
        keys=keys,
    )


# --- operator-input validation ---------------------------------------------------


def test_workspace_id_validator_accepts_canonical_uuids() -> None:
    assert validate_policy_key_workspace_id(str(WORKSPACE_ID)) == WORKSPACE_ID


@pytest.mark.parametrize("value", ["", "not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_workspace_id_validator_rejects_malformed_or_nil_uuids(value: str) -> None:
    with pytest.raises(PolicyKeyCommandInputError):
        validate_policy_key_workspace_id(value)


@pytest.mark.parametrize(
    "file_name",
    ["/abs/key.pem", "../escape.pem", "a/../b.pem", "back\\slash.pem", "", " .pem"],
)
def test_key_file_name_validator_rejects_escapes(file_name: str) -> None:
    with pytest.raises(PolicyKeyCommandInputError):
        validate_policy_key_file_name(file_name)


def test_key_id_validator_rejects_foreign_grammars() -> None:
    for value in ("", "auth-key-v1", "ed25519-sha256-short"):
        with pytest.raises(PolicyKeyCommandInputError):
            validate_policy_signing_key_id_text(value)


# --- the create-or-import secret-file boundary ------------------------------------


def test_creation_writes_a_new_exact_file_with_restrictive_permissions(
    tmp_path: Path,
) -> None:
    signer = create_or_load_policy_signing_key(tmp_path, "policy_signing_a.pem")
    written = tmp_path / "policy_signing_a.pem"
    assert written.is_file()
    if os.name == "posix":
        assert (written.stat().st_mode & 0o777) == 0o600
    reloaded = load_existing_policy_signing_key(tmp_path, "policy_signing_a.pem")
    assert reloaded.key_id == signer.key_id
    assert (
        read_secret_file(written, tmp_path)
        .get_secret_value()
        .startswith("-----BEGIN PRIVATE KEY-----")
    )


def test_creation_never_overwrites_existing_bytes(tmp_path: Path) -> None:
    existing_key = Ed25519PrivateKey.generate()
    existing_pem = existing_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    (tmp_path / "policy_signing_a.pem").write_bytes(existing_pem)
    signer = create_or_load_policy_signing_key(tmp_path, "policy_signing_a.pem")
    assert (tmp_path / "policy_signing_a.pem").read_bytes() == existing_pem
    assert signer.key_id == derive_ed25519_key_id(existing_key.public_key().public_bytes_raw())


def test_loading_an_absent_existing_key_file_fails_closed(tmp_path: Path) -> None:
    from personal_os.error_contracts.exceptions import SecretFileError

    with pytest.raises(SecretFileError):
        load_existing_policy_signing_key(tmp_path, "absent.pem")


# --- keyset payload view parsing ----------------------------------------------------


def test_parse_reports_states_and_revisions() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(
            states=(
                (_CURRENT_PUBLIC_KEY, PolicyKeysetState.CURRENT),
                (_STAGED_PUBLIC_KEY, PolicyKeysetState.STAGED),
            ),
            revision=2,
        )
    )
    assert view.keyset_revision == 2
    current = view.current_key()
    assert current is not None and current.key_id == _CURRENT_KEY_ID
    assert tuple(key.key_id for key in view.staged_keys()) == (_STAGED_KEY_ID,)
    assert view.state_of(_STAGED_KEY_ID) is PolicyKeysetState.STAGED
    assert view.state_of(_THIRD_KEY_ID) is None


def test_parse_fails_closed_on_corrupt_payloads() -> None:
    for corrupt in (b"", b"[]", b'{"contract":"x"}'):
        with pytest.raises(InternalApplicationError):
            parse_policy_keyset_payload(corrupt)


# --- the pure transition planners --------------------------------------------------


def test_initialize_plans_one_self_signed_current_key() -> None:
    keys = plan_initialized_keys(_CURRENT_KEY_ID, _CURRENT_PUBLIC_KEY)
    assert [(key.key_id, key.state) for key in keys] == [
        (_CURRENT_KEY_ID, PolicyKeysetState.CURRENT)
    ]


def test_stage_keeps_the_old_current_and_adds_one_staged_key() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(states=((_CURRENT_PUBLIC_KEY, PolicyKeysetState.CURRENT),))
    )
    keys = plan_staged_keys(view, _STAGED_KEY_ID, _STAGED_PUBLIC_KEY)
    states = {key.key_id: key.state for key in keys}
    assert states == {
        _CURRENT_KEY_ID: PolicyKeysetState.CURRENT,
        _STAGED_KEY_ID: PolicyKeysetState.STAGED,
    }


def test_stage_refuses_when_a_staged_key_already_exists() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(
            states=(
                (_CURRENT_PUBLIC_KEY, PolicyKeysetState.CURRENT),
                (_STAGED_PUBLIC_KEY, PolicyKeysetState.STAGED),
            )
        )
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        plan_staged_keys(view, _THIRD_KEY_ID, _THIRD_PUBLIC_KEY)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert str(raised.value.safe_details["reason"]) == "staged_key_exists"


def test_stage_refuses_to_stage_the_current_key() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(states=((_CURRENT_PUBLIC_KEY, PolicyKeysetState.CURRENT),))
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        plan_staged_keys(view, _CURRENT_KEY_ID, _CURRENT_PUBLIC_KEY)
    assert str(raised.value.safe_details["reason"]) == "key_already_known"


def test_activate_promotes_the_staged_key_and_demotes_the_old_current() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(
            states=(
                (_CURRENT_PUBLIC_KEY, PolicyKeysetState.CURRENT),
                (_STAGED_PUBLIC_KEY, PolicyKeysetState.STAGED),
            ),
            revision=2,
        )
    )
    keys = plan_activated_keys(view, _STAGED_KEY_ID)
    states = {key.key_id: key.state for key in keys}
    assert states == {
        _STAGED_KEY_ID: PolicyKeysetState.CURRENT,
        _CURRENT_KEY_ID: PolicyKeysetState.STAGED,
    }


def test_activate_refuses_a_key_that_is_not_staged() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(states=((_CURRENT_PUBLIC_KEY, PolicyKeysetState.CURRENT),))
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        plan_activated_keys(view, _STAGED_KEY_ID)
    assert str(raised.value.safe_details["reason"]) == "key_not_staged"


def test_retire_moves_one_staged_key_to_retired() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(
            states=(
                (_STAGED_PUBLIC_KEY, PolicyKeysetState.CURRENT),
                (_CURRENT_PUBLIC_KEY, PolicyKeysetState.STAGED),
            ),
            revision=3,
        )
    )
    keys = plan_retired_keys(view, _CURRENT_KEY_ID)
    states = {key.key_id: key.state for key in keys}
    assert states == {
        _STAGED_KEY_ID: PolicyKeysetState.CURRENT,
        _CURRENT_KEY_ID: PolicyKeysetState.RETIRED,
    }


def test_retire_refuses_the_current_key_even_when_it_is_the_last_trusted_key() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(states=((_CURRENT_PUBLIC_KEY, PolicyKeysetState.CURRENT),))
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        plan_retired_keys(view, _CURRENT_KEY_ID)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert str(raised.value.safe_details["reason"]) == "cannot_retire_current_key"


def test_retire_refuses_unknown_keys() -> None:
    view = parse_policy_keyset_payload(
        _payload_bytes(states=((_CURRENT_PUBLIC_KEY, PolicyKeysetState.CURRENT),))
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        plan_retired_keys(view, _THIRD_KEY_ID)
    assert str(raised.value.safe_details["reason"]) == "key_unknown"


# --- the cross-signed envelope construction ----------------------------------------


def test_initialize_envelope_is_self_signed_by_the_new_current_key() -> None:
    signer = _generate_signer()
    envelope = build_lifecycle_keyset_envelope(
        workspace_id=WORKSPACE_ID,
        keyset_revision=1,
        keys=plan_initialized_keys(signer.key_id, signer.public_key),
        row_ids_by_key_id={signer.key_id: _CURRENT_ROW_ID},
        signers=(signer,),
        created_at=CREATED_AT,
    )
    assert isinstance(envelope, PolicyKeysetEnvelope)
    assert envelope.parent_keyset_revision is None
    assert envelope.keys == (
        PolicySigningKeyRecord(signing_key_id=_CURRENT_ROW_ID, public_key_bytes=signer.public_key),
    )
    message = build_signed_message(KEYSET_SIGNING_DOMAIN, envelope.canonical_payload_bytes)
    verifier = Ed25519PolicyVerifier({signer.key_id: signer.public_key})
    assert len(envelope.signatures) == 1
    assert verifier.verify(signer.key_id, envelope.signatures[0].signature_bytes, message)


def test_staged_envelope_carries_old_current_and_possession_signatures() -> None:
    old_signer = _generate_signer()
    new_signer = _generate_signer()
    envelope = build_lifecycle_keyset_envelope(
        workspace_id=WORKSPACE_ID,
        keyset_revision=2,
        keys=plan_staged_keys(
            parse_policy_keyset_payload(
                _payload_bytes(states=((old_signer.public_key, PolicyKeysetState.CURRENT),))
            ),
            new_signer.key_id,
            new_signer.public_key,
        ),
        row_ids_by_key_id={
            old_signer.key_id: _CURRENT_ROW_ID,
            new_signer.key_id: _STAGED_ROW_ID,
        },
        signers=(old_signer, new_signer),
        created_at=CREATED_AT,
    )
    message = build_signed_message(KEYSET_SIGNING_DOMAIN, envelope.canonical_payload_bytes)
    verifier = Ed25519PolicyVerifier(
        {
            old_signer.key_id: old_signer.public_key,
            new_signer.key_id: new_signer.public_key,
        }
    )
    assert len(envelope.signatures) == 2
    row_id_to_key_id = {_CURRENT_ROW_ID: old_signer.key_id, _STAGED_ROW_ID: new_signer.key_id}
    for signature in envelope.signatures:
        signing_key_id = row_id_to_key_id[signature.signing_key_id]
        assert verifier.verify(signing_key_id, signature.signature_bytes, message)


def test_activation_envelope_refuses_to_build_without_both_cross_signatures() -> None:
    old_signer = _generate_signer()
    new_signer = _generate_signer()
    staged_view = parse_policy_keyset_payload(
        _payload_bytes(
            states=(
                (old_signer.public_key, PolicyKeysetState.CURRENT),
                (new_signer.public_key, PolicyKeysetState.STAGED),
            ),
            revision=2,
        )
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        build_lifecycle_keyset_envelope(
            workspace_id=WORKSPACE_ID,
            keyset_revision=3,
            keys=plan_activated_keys(staged_view, new_signer.key_id),
            row_ids_by_key_id={
                old_signer.key_id: _CURRENT_ROW_ID,
                new_signer.key_id: _STAGED_ROW_ID,
            },
            signers=(old_signer,),
            required_signing_key_ids=(old_signer.key_id, new_signer.key_id),
            created_at=CREATED_AT,
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert str(raised.value.safe_details["reason"]) == "cross_signature_missing"


def test_envelope_builder_maps_every_key_row_once() -> None:
    signer = _generate_signer()
    envelope = build_lifecycle_keyset_envelope(
        workspace_id=WORKSPACE_ID,
        keyset_revision=1,
        keys=plan_initialized_keys(signer.key_id, signer.public_key),
        row_ids_by_key_id={},
        signers=(signer,),
        created_at=CREATED_AT,
    )
    assert envelope.keys  # the builder allocated one fresh row identity
    assert len({key.signing_key_id for key in envelope.keys}) == len(envelope.keys)


# --- the closed result rendering ----------------------------------------------------


def test_outcome_lines_print_ids_and_status_only() -> None:
    outcome = PolicyKeyLifecycleOutcome(
        action="activated", key_id=_STAGED_KEY_ID, keyset_revision=3, is_replay=False
    )
    line = render_policy_key_outcome(outcome)
    assert line == f"activated=true key_id={_STAGED_KEY_ID} keyset_revision=3 replayed=false"
    replay = PolicyKeyLifecycleOutcome(
        action="initialized", key_id=_CURRENT_KEY_ID, keyset_revision=1, is_replay=True
    )
    assert render_policy_key_outcome(replay) == (
        f"initialized=true key_id={_CURRENT_KEY_ID} keyset_revision=1 replayed=true"
    )
    assert "PRIVATE KEY" not in line
    assert "signature" not in line
