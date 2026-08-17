"""Exclusion-policy signing settings and the fail-before-bind signer boundary.

These tests pin the closed ``KNOWLEDGE_POLICY_SIGNING_KEY_ID`` /
``KNOWLEDGE_POLICY_SIGNING_KEY_FILE`` fragment: the exact-file grammar beneath
the configured secret root (absolute paths, ``..`` segments and backslash
escapes have no valid spelling), the derived ``ed25519-sha256-`` key-ID
grammar, and the private-key material boundary — an unencrypted PKCS#8 PEM
holding exactly one Ed25519 private key, loaded through the shared secret-file
checks. Encrypted, malformed, multi-block and wrong-algorithm material, key-ID
mismatches and unknown active keys are typed configuration failures that never
echo file names, key bytes or provider text.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from api_runtime.exclusion_policy_settings import (
    POLICY_SIGNING_KEY_FILE_MAXIMUM_BYTES,
    ExclusionPolicySigningSettings,
    assert_signer_is_current_in_latest_keysets,
    load_exclusion_policy_signer,
    load_exclusion_policy_signing_settings,
    parse_policy_signing_pem,
    resolve_current_key_id_from_keyset_payload,
)
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ConfigurationError,
    InternalApplicationError,
    SecretFileError,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.signatures import (
    KEY_ID_PREFIX,
    PolicyKeysetKey,
    PolicyKeysetState,
    build_keyset_payload,
    derive_ed25519_key_id,
)

#: Any absolute path works for grammar tests; the signer tests bind a real
#: temporary secret root through the same field.
_SECRET_ROOT: Path = Path.cwd()
_CREATED_AT = datetime(2026, 8, 17, tzinfo=UTC)


def _generate_signing_material() -> tuple[bytes, str]:
    """Generate one fresh Ed25519 key as an unencrypted PKCS#8 PEM plus ID."""

    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    key_id = derive_ed25519_key_id(private_key.public_key().public_bytes_raw())
    return pem, key_id


_PEM, _KEY_ID = _generate_signing_material()


def signing_environ(**overrides: str) -> dict[str, str]:
    """Build one valid exclusion-policy signing environment snapshot."""

    environ: dict[str, str] = {
        "KNOWLEDGE_ENVIRONMENT": "test",
        "KNOWLEDGE_SECRET_ROOT": str(_SECRET_ROOT),
        "KNOWLEDGE_POLICY_SIGNING_KEY_ID": _KEY_ID,
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy_signing_current.pem",
    }
    environ.update(overrides)
    return environ


def load_settings(**overrides: str) -> ExclusionPolicySigningSettings:
    return load_exclusion_policy_signing_settings(environ=signing_environ(**overrides))


def _entry(public_key: bytes, state: PolicyKeysetState) -> PolicyKeysetKey:
    return PolicyKeysetKey(
        key_id=derive_ed25519_key_id(public_key), public_key=public_key, state=state
    )


def _payload_of(*keys: PolicyKeysetKey) -> bytes:
    return build_keyset_payload(
        workspace_id=uuid4(),
        keyset_revision=1,
        parent_keyset_revision=None,
        created_at=_CREATED_AT,
        keys=keys,
    )


# --- the fragment grammar -----------------------------------------------------


def test_valid_fragment_resolves_the_exact_file_beneath_the_secret_root() -> None:
    settings = load_settings()
    assert settings.signing_key_id == _KEY_ID
    assert settings.signing_key_file == _SECRET_ROOT / "policy_signing_current.pem"
    assert settings.signing_key_file.is_absolute()


def test_fragment_names_join_the_repository_wide_registry() -> None:
    from personal_os.runtime_configuration.environment_names import (
        KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
    )

    assert "KNOWLEDGE_POLICY_SIGNING_KEY_ID" in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    assert "KNOWLEDGE_POLICY_SIGNING_KEY_FILE" in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES


@pytest.mark.parametrize(
    "file_name",
    [
        "/etc/policy_signing.pem",
        "C:\\secrets\\policy_signing.pem",
        "../outside_root.pem",
        "nested/../../outside_root.pem",
        "",
        " trailing-segment.pem",
        ".hidden-start.pem",
    ],
)
def test_key_file_name_grammar_rejects_escapes_and_absolute_paths(
    file_name: str,
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_POLICY_SIGNING_KEY_FILE=file_name)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_nested_relative_segments_remain_valid() -> None:
    settings = load_settings(KNOWLEDGE_POLICY_SIGNING_KEY_FILE="policy/rotation-2026_08.pem")
    assert settings.signing_key_file.name == "rotation-2026_08.pem"


@pytest.mark.parametrize(
    "key_id",
    ["", "ed25519-sha256-short", "rsa-sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "x"],
)
def test_key_id_must_follow_the_derived_ed25519_grammar(key_id: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_POLICY_SIGNING_KEY_ID=key_id)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_missing_either_signing_variable_is_invalid() -> None:
    for missing_name in (
        "KNOWLEDGE_POLICY_SIGNING_KEY_ID",
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE",
        "KNOWLEDGE_SECRET_ROOT",
    ):
        environ = signing_environ()
        del environ[missing_name]
        with pytest.raises(ConfigurationError) as raised:
            load_exclusion_policy_signing_settings(environ=environ)
        assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_relative_secret_root_is_invalid() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_SECRET_ROOT="relative-secret-root")
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_unknown_knowledge_variable_is_terminal() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_POLICY_SIGNING_PRIVATE_KEY_PEM="-----BEGIN-----")
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


# --- the private-key material boundary -----------------------------------------


def test_parse_accepts_exactly_one_unencrypted_ed25519_pkcs8_pem() -> None:
    private_key = parse_policy_signing_pem(_PEM.decode("ascii"))
    assert isinstance(private_key, Ed25519PrivateKey)


def test_parse_rejects_encrypted_pkcs8_pem() -> None:
    private_key = Ed25519PrivateKey.generate()
    encrypted = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=BestAvailableEncryption(b"passphrase"),
    )
    with pytest.raises(ConfigurationError) as raised:
        parse_policy_signing_pem(encrypted.decode("ascii"))
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID
    assert str(raised.value.safe_details["reason"]) == "encrypted"


def test_parse_rejects_malformed_pem() -> None:
    with pytest.raises(ConfigurationError) as raised:
        parse_policy_signing_pem("not a pem document at all")
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID
    assert str(raised.value.safe_details["reason"]) == "malformed"


def test_parse_rejects_multiple_pem_blocks() -> None:
    doubled = _PEM + _PEM
    with pytest.raises(ConfigurationError) as raised:
        parse_policy_signing_pem(doubled.decode("ascii"))
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID
    assert str(raised.value.safe_details["reason"]) == "multiple_blocks"


def test_parse_rejects_wrong_algorithm_pkcs8_pem() -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    with pytest.raises(ConfigurationError) as raised:
        parse_policy_signing_pem(pem.decode("ascii"))
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID
    assert str(raised.value.safe_details["reason"]) == "wrong_algorithm"


def test_load_signer_returns_the_derived_key_id(tmp_path: Path) -> None:
    (tmp_path / "signing.pem").write_bytes(_PEM)
    settings = load_settings(
        KNOWLEDGE_SECRET_ROOT=str(tmp_path),
        KNOWLEDGE_POLICY_SIGNING_KEY_FILE="signing.pem",
    )
    signer = load_exclusion_policy_signer(settings, secret_root=tmp_path)
    assert signer.key_id == _KEY_ID
    assert signer.key_id.startswith(KEY_ID_PREFIX)


def test_load_signer_rejects_private_public_key_id_mismatch(tmp_path: Path) -> None:
    (tmp_path / "signing.pem").write_bytes(_PEM)
    other_pem, _other_key_id = _generate_signing_material()
    other_key_id = derive_ed25519_key_id(
        parse_policy_signing_pem(other_pem.decode("ascii")).public_key().public_bytes_raw()
    )
    settings = load_exclusion_policy_signing_settings(
        environ=signing_environ(
            KNOWLEDGE_SECRET_ROOT=str(tmp_path),
            KNOWLEDGE_POLICY_SIGNING_KEY_FILE="signing.pem",
            KNOWLEDGE_POLICY_SIGNING_KEY_ID=other_key_id,
        )
    )
    with pytest.raises(ConfigurationError) as raised:
        load_exclusion_policy_signer(settings, secret_root=tmp_path)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID
    assert str(raised.value.safe_details["reason"]) == "key_id_mismatch"


def test_load_signer_propagates_the_missing_secret_file_failure(tmp_path: Path) -> None:
    settings = load_settings(
        KNOWLEDGE_SECRET_ROOT=str(tmp_path),
        KNOWLEDGE_POLICY_SIGNING_KEY_FILE="absent.pem",
    )
    with pytest.raises(SecretFileError) as raised:
        load_exclusion_policy_signer(settings, secret_root=tmp_path)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_MISSING


def test_load_signer_oversized_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "oversized.pem").write_bytes(b"x" * (POLICY_SIGNING_KEY_FILE_MAXIMUM_BYTES + 1))
    settings = load_settings(
        KNOWLEDGE_SECRET_ROOT=str(tmp_path),
        KNOWLEDGE_POLICY_SIGNING_KEY_FILE="oversized.pem",
    )
    with pytest.raises(SecretFileError) as raised:
        load_exclusion_policy_signer(settings, secret_root=tmp_path)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_TOO_LARGE


@pytest.mark.skipif(os.name != "posix", reason="permission bits are enforced on POSIX only")
def test_load_signer_rejects_insecure_file_permissions(tmp_path: Path) -> None:
    secret_path = tmp_path / "group_writable.pem"
    secret_path.write_bytes(_PEM)
    os.chmod(secret_path, 0o640)
    settings = load_settings(
        KNOWLEDGE_SECRET_ROOT=str(tmp_path),
        KNOWLEDGE_POLICY_SIGNING_KEY_FILE="group_writable.pem",
    )
    try:
        with pytest.raises(SecretFileError) as raised:
            load_exclusion_policy_signer(settings, secret_root=tmp_path)
        assert raised.value.error_code is ErrorCode.SECRET_FILE_INSECURE_PERMISSIONS
    finally:
        os.chmod(secret_path, 0o600)


def test_load_signer_rejects_symlink_escaping_the_secret_root(tmp_path: Path) -> None:
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    outside_key = outside_root / "real.pem"
    outside_key.write_bytes(_PEM)
    link_path = tmp_path / "alias.pem"
    try:
        link_path.symlink_to(outside_key)
    except OSError as cause:  # pragma: no cover - platform without symlink support
        pytest.skip(f"symlinks are unavailable: {cause}")
    settings = load_settings(
        KNOWLEDGE_SECRET_ROOT=str(tmp_path),
        KNOWLEDGE_POLICY_SIGNING_KEY_FILE="alias.pem",
    )
    with pytest.raises(SecretFileError) as raised:
        load_exclusion_policy_signer(settings, secret_root=tmp_path)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_OUTSIDE_ROOT


# --- the latest-keyset current-key proof ---------------------------------------


def test_resolve_current_key_id_reads_the_canonical_payload() -> None:
    current_public = bytes(range(32))
    staged_public = bytes(range(32, 64))
    payload = _payload_of(
        _entry(current_public, PolicyKeysetState.CURRENT),
        _entry(staged_public, PolicyKeysetState.STAGED),
    )
    assert resolve_current_key_id_from_keyset_payload(payload) == derive_ed25519_key_id(
        current_public
    )


def test_resolve_current_key_id_reports_staged_only_keysets() -> None:
    staged_public = bytes(range(64, 96))
    payload = _payload_of(_entry(staged_public, PolicyKeysetState.STAGED))
    assert resolve_current_key_id_from_keyset_payload(payload) is None


def test_resolve_current_key_id_fails_closed_on_corrupt_payloads() -> None:
    for corrupt in (b"", b"not json", b'{"contract":"other/v1"}', b'{"keys": 5}'):
        with pytest.raises(InternalApplicationError):
            resolve_current_key_id_from_keyset_payload(corrupt)


def test_assert_signer_requires_at_least_one_initialized_keyset() -> None:
    with pytest.raises(ExclusionPolicyError) as raised:
        assert_signer_is_current_in_latest_keysets((), signing_key_id=_KEY_ID)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED


def test_assert_signer_rejects_an_unknown_active_key() -> None:
    current_payload = _payload_of(_entry(bytes(range(32)), PolicyKeysetState.CURRENT))
    staged_only_payload = _payload_of(_entry(bytes(range(64, 96)), PolicyKeysetState.STAGED))
    with pytest.raises(ConfigurationError) as raised:
        assert_signer_is_current_in_latest_keysets(
            (current_payload, staged_only_payload), signing_key_id=_KEY_ID
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_assert_signer_accepts_the_current_key_of_every_latest_keyset() -> None:
    current = _entry(bytes(range(32)), PolicyKeysetState.CURRENT)
    payload_one = _payload_of(current)
    payload_two = _payload_of(current)
    assert (
        assert_signer_is_current_in_latest_keysets(
            (payload_one, payload_two), signing_key_id=current.key_id
        )
        is None
    )


# --- the serialized settings value stays non-secret -----------------------------


def test_settings_snapshot_carries_no_key_material() -> None:
    settings = load_settings()
    rendered = repr(settings) + str(settings)
    assert "PRIVATE KEY" not in rendered
    assert _KEY_ID in rendered
