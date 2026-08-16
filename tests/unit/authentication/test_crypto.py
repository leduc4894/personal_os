"""Opaque credential parsing and HKDF domain-separation label contracts.

These tests prove the versioned ``at1``/``rt1`` credential grammar (spec 12.1):
typed results for well-formed values, ``device_credential_invalid`` for unknown
versions, wrong kinds, wrong segment counts, invalid lookup identifiers and
size violations, and that no rejected value is ever retained in the raised
error. The five HKDF domain labels are pinned to their exact bytes (spec 20.1).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from personal_os.authentication.contracts import DeviceTokenKind, OpaqueCredential
from personal_os.authentication.crypto import (
    ACCESS_CREDENTIAL_PREFIX,
    CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS,
    CSRF_HASH_LABEL,
    GRANT_REPLAY_DERIVATION_LABEL,
    RECOVERY_CODE_HASH_LABEL,
    REFRESH_CREDENTIAL_PREFIX,
    REFRESH_REPLAY_DERIVATION_LABEL,
    THROTTLE_HMAC_LABEL,
    parse_access_credential,
    parse_refresh_credential,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.error_contracts.codes import ErrorCode

LOOKUP_ID = UUID("123e4567-e89b-42d3-a456-426614174000")
SECRET_SEGMENT = "s3cret_segment_value_0123456789abcdef"
ACCESS_CREDENTIAL = f"at1.{LOOKUP_ID}.{SECRET_SEGMENT}"
REFRESH_CREDENTIAL = f"rt1.{LOOKUP_ID}.{SECRET_SEGMENT}"


def test_parse_access_credential_returns_typed_value() -> None:
    credential = parse_access_credential(ACCESS_CREDENTIAL)
    assert isinstance(credential, OpaqueCredential)
    assert credential.token_kind is DeviceTokenKind.ACCESS
    assert credential.lookup_id == LOOKUP_ID
    assert credential.secret == SECRET_SEGMENT.encode("ascii")


def test_parse_refresh_credential_returns_typed_value() -> None:
    credential = parse_refresh_credential(REFRESH_CREDENTIAL)
    assert credential.token_kind is DeviceTokenKind.REFRESH
    assert credential.lookup_id == LOOKUP_ID
    assert credential.secret == SECRET_SEGMENT.encode("ascii")


def test_parsed_credential_repr_hides_the_secret() -> None:
    credential = parse_refresh_credential(REFRESH_CREDENTIAL)
    assert SECRET_SEGMENT not in repr(credential)
    assert str(LOOKUP_ID) in repr(credential)


def test_opaque_token_parser_rejects_unknown_version_without_echo() -> None:
    rejected = "rt9.lookup.secret-sentinel"
    with pytest.raises(AuthenticationError) as raised:
        parse_refresh_credential(rejected)
    assert rejected not in repr(raised.value)


@pytest.mark.parametrize(
    "rejected",
    [
        "at1.not-a-uuid.secretsecretsecret01",
        "at1..secretsecretsecret01",
        f"rt1.{LOOKUP_ID}",
        f"rt1.{LOOKUP_ID}.{SECRET_SEGMENT}.extra",
        "at1.123e4567-e89b-42d3-a456-426614174000.ab",
        "at1.123E4567-E89B-42D3-A456-426614174000.secretsecretsecret01",
        "at1.123e4567e89b42d3a456426614174000.secretsecretsecret01",
        "at1." + str(LOOKUP_ID) + ".space inside secret value",
        "at1." + str(LOOKUP_ID) + "." + "x" * (CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS + 1),
    ],
)
def test_parser_rejects_malformed_credentials_without_echo(rejected: str) -> None:
    with pytest.raises(AuthenticationError) as raised:
        parse_refresh_credential(rejected)
    error = raised.value
    assert error.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID
    assert rejected not in repr(error)
    assert rejected not in str(error)
    assert error.safe_details == {}


def test_parser_rejects_empty_input_without_content() -> None:
    with pytest.raises(AuthenticationError) as raised:
        parse_refresh_credential("")
    assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID


def test_parser_rejects_wrong_credential_kind() -> None:
    with pytest.raises(AuthenticationError) as access_rejected:
        parse_access_credential(REFRESH_CREDENTIAL)
    assert access_rejected.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID
    with pytest.raises(AuthenticationError) as refresh_rejected:
        parse_refresh_credential(ACCESS_CREDENTIAL)
    assert refresh_rejected.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID


def test_credential_version_prefixes_are_pinned() -> None:
    assert ACCESS_CREDENTIAL_PREFIX == "at1"
    assert REFRESH_CREDENTIAL_PREFIX == "rt1"
    assert CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS >= 64


def test_hkdf_domain_labels_are_exact_bytes() -> None:
    assert CSRF_HASH_LABEL == "auth/csrf/v1"
    assert THROTTLE_HMAC_LABEL == "auth/throttle/v1"
    assert RECOVERY_CODE_HASH_LABEL == "auth/recovery/v1"
    assert GRANT_REPLAY_DERIVATION_LABEL == "auth/grant-replay/v1"
    assert REFRESH_REPLAY_DERIVATION_LABEL == "auth/refresh-replay/v1"
