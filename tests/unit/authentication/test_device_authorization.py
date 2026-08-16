"""Browser device-authorization grant domain contracts (spec 11.1-11.3).

These tests pin the pure grant vocabulary: the closed user-code grammar with
its checksum character (human-readable, typo-detecting), the opaque versioned
polling credential with a 256-bit secret, fragment-only browser continuity of
the complete verification URL, HMAC storage digests under the exact-replay
domain label, the settings-bounded plugin version gate, the closed grant
lifetime and poll interval, and the pure lookup/terminal rejection decisions
that keep expiry a property of the pending state only.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

import pytest

from personal_os.authentication.contracts import DeviceAuthorizationGrantState
from personal_os.authentication.crypto import GRANT_REPLAY_DERIVATION_LABEL
from personal_os.authentication.device_authorization import (
    DEVICE_GRANT_EXPIRES_IN_SECONDS,
    DEVICE_GRANT_LIFETIME,
    DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS,
    DEVICE_NAME_MINIMUM_LENGTH_CHARACTERS,
    POLL_INTERVAL_SECONDS,
    POLLING_CREDENTIAL_PREFIX,
    USER_CODE_PATTERN,
    DeviceAuthorizationService,
    PluginVersionBounds,
    StoredDeviceAuthorizationGrant,
    build_verification_uris,
    generate_polling_credential,
    generate_user_code,
    is_valid_user_code,
    parse_plugin_version,
    polling_credential_hash_of,
    resolve_lookup_rejection_code,
    resolve_terminal_rejection_code,
    user_code_hash_of,
    validate_plugin_version_bounds,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.error_contracts.codes import ErrorCode

_DATABASE_NOW: Final[datetime] = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_VERIFICATION_BASE_URL: Final[str] = "https://web-admin.example"
_PLUGIN_BOUNDS: Final[PluginVersionBounds] = PluginVersionBounds(
    minimum=(1, 0, 0), maximum=(2, 0, 0)
)


class StdlibHmacCrypto:
    """Deterministic crypto double mirroring the port with stdlib only."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        return hmac.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("grant hashing never seals")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("grant hashing never opens")


def _grant_hmac_key() -> bytes:
    return StdlibHmacCrypto().derive_subkey(
        master_key=bytes(range(32)), label=GRANT_REPLAY_DERIVATION_LABEL
    )


def _stored_grant(
    *,
    state: DeviceAuthorizationGrantState,
    expires_at: datetime,
) -> StoredDeviceAuthorizationGrant:
    return StoredDeviceAuthorizationGrant(
        grant_id=UUID("00000000-0000-7000-8000-0000000000aa"),
        client_instance_id=UUID("00000000-0000-7000-8000-0000000000bb"),
        claimed_device_id=None,
        device_name="Personal desktop",
        platform_class="obsidian_desktop",
        platform_name="windows",
        plugin_version="1.4.0",
        requested_scope="obsidian_sync",
        state=state,
        created_at=_DATABASE_NOW,
        expires_at=expires_at,
        approved_at=None,
        denied_at=None,
        exchanged_at=None,
        approved_by_user_id=None,
        approved_web_session_id=None,
    )


# --- user code ------------------------------------------------------------------------


def test_generated_user_code_matches_the_closed_human_readable_grammar() -> None:
    user_code = generate_user_code()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", user_code) is not None
    assert USER_CODE_PATTERN.fullmatch(user_code) is not None


def test_user_code_generation_is_deterministic_under_fixed_randomness() -> None:
    fixed_bytes = bytes(31 + index for index in range(31))
    first = generate_user_code(random_bytes=lambda count: fixed_bytes[:count])
    second = generate_user_code(random_bytes=lambda count: fixed_bytes[:count])
    assert first == second == "ABCD-EFGW"
    assert is_valid_user_code(first)


def test_user_code_checksum_detects_single_character_typos() -> None:
    user_code = generate_user_code()
    tampered = ("B" if user_code[0] != "B" else "C") + user_code[1:]
    assert is_valid_user_code(user_code)
    assert not is_valid_user_code(tampered)


@pytest.mark.parametrize(
    "rejected_value",
    [
        "abcd-efgh",  # lowercase
        "ABCDEFGH",  # missing hyphen
        "ABCD-EFG",  # wrong block size
        "ABCD-EFGHI",  # wrong block size
        "ABCD-EFG0",  # zero outside the alphabet
        "ABCD-EFG1",  # one outside the alphabet
        "ABCD-EFGI",  # ambiguous I outside the alphabet
        "ABCD EFGH",  # space instead of the hyphen
        "",  # empty
    ],
)
def test_user_code_validation_rejects_non_grammar_values(rejected_value: str) -> None:
    assert not is_valid_user_code(rejected_value)


def test_user_code_grammar_documents_the_display_bounds() -> None:
    assert DEVICE_NAME_MINIMUM_LENGTH_CHARACTERS == 1
    assert DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS == 80


# --- polling credential ---------------------------------------------------------------


def test_polling_credential_is_versioned_with_a_256_bit_secret() -> None:
    grant_id = UUID("00000000-0000-7000-8000-0000000000cc")
    credential = generate_polling_credential(grant_id=grant_id)
    prefix, lookup_segment, secret_segment = credential.split(".")
    assert prefix == POLLING_CREDENTIAL_PREFIX
    assert lookup_segment == str(grant_id)
    # 43 base64url characters encode exactly 32 random bytes (spec 11.1).
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", secret_segment) is not None


def test_polling_credential_generation_is_deterministic_under_fixed_randomness() -> None:
    grant_id = UUID("00000000-0000-7000-8000-0000000000cc")
    entropy = bytes(range(32))
    first = generate_polling_credential(
        grant_id=grant_id, random_bytes=lambda count: entropy[:count]
    )
    second = generate_polling_credential(
        grant_id=grant_id, random_bytes=lambda count: entropy[:count]
    )
    assert first == second


# --- browser continuity ---------------------------------------------------------------


def test_verification_complete_contains_only_the_user_code_fragment() -> None:
    user_code = generate_user_code()
    polling_credential = generate_polling_credential(
        grant_id=UUID("00000000-0000-7000-8000-0000000000cc")
    )
    verification_uri, verification_uri_complete = build_verification_uris(
        verification_base_url=_VERIFICATION_BASE_URL, user_code=user_code
    )
    parsed = urlsplit(verification_uri_complete)
    assert parsed.query == ""
    assert parsed.fragment == user_code
    assert verification_uri_complete.startswith(verification_uri)
    assert polling_credential not in verification_uri_complete
    assert urlsplit(verification_uri).fragment == ""
    assert verification_uri == f"{_VERIFICATION_BASE_URL}/device/approve"


# --- storage digests ------------------------------------------------------------------


def test_user_code_and_polling_credential_hash_under_the_replay_label() -> None:
    hmac_key = _grant_hmac_key()
    user_code = generate_user_code()
    credential = generate_polling_credential(grant_id=UUID("00000000-0000-7000-8000-0000000000cc"))
    user_code_digest = user_code_hash_of(hmac_key=hmac_key, user_code=user_code)
    credential_digest = polling_credential_hash_of(hmac_key=hmac_key, polling_credential=credential)
    assert re.fullmatch(r"[0-9a-f]{64}", user_code_digest) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", credential_digest) is not None
    assert user_code_digest != credential_digest
    assert user_code not in user_code_digest
    assert credential not in credential_digest


def test_user_code_hash_separates_one_character_differences() -> None:
    hmac_key = _grant_hmac_key()
    user_code = generate_user_code()
    tampered = ("B" if user_code[0] != "B" else "C") + user_code[1:]
    assert user_code_hash_of(hmac_key=hmac_key, user_code=user_code) != user_code_hash_of(
        hmac_key=hmac_key, user_code=tampered
    )


# --- plugin version gate --------------------------------------------------------------


def test_plugin_version_parsing_accepts_dotted_numeric_triples() -> None:
    assert parse_plugin_version("1.13.1") == (1, 13, 1)
    assert parse_plugin_version("0.0.1") == (0, 0, 1)


@pytest.mark.parametrize("rejected_version", ["1.13", "v1.13.1", "1.13.1-beta", ""])
def test_plugin_version_parsing_rejects_non_semantic_values(rejected_version: str) -> None:
    with pytest.raises(AuthenticationError) as raised:
        parse_plugin_version(rejected_version)
    assert raised.value.error_code is ErrorCode.PLUGIN_VERSION_UNSUPPORTED


def test_plugin_version_bounds_reject_versions_outside_the_approved_window() -> None:
    with pytest.raises(AuthenticationError) as below:
        validate_plugin_version_bounds("0.9.9", _PLUGIN_BOUNDS)
    assert below.value.error_code is ErrorCode.PLUGIN_VERSION_UNSUPPORTED
    assert [str(bound) for bound in below.value.safe_details["approved_version_bounds"]] == [
        "1.0.0",
        "2.0.0",
    ]
    with pytest.raises(AuthenticationError) as above:
        validate_plugin_version_bounds("2.0.1", _PLUGIN_BOUNDS)
    assert above.value.error_code is ErrorCode.PLUGIN_VERSION_UNSUPPORTED
    # The bounds are inclusive on both ends.
    validate_plugin_version_bounds("1.0.0", _PLUGIN_BOUNDS)
    validate_plugin_version_bounds("2.0.0", _PLUGIN_BOUNDS)
    validate_plugin_version_bounds("1.13.1", _PLUGIN_BOUNDS)


# --- lifetime constants ---------------------------------------------------------------


def test_grant_lifetime_and_poll_interval_are_exact() -> None:
    assert DEVICE_GRANT_EXPIRES_IN_SECONDS == 600
    assert timedelta(seconds=600) == DEVICE_GRANT_LIFETIME
    assert POLL_INTERVAL_SECONDS == 5


# --- pure decisions -------------------------------------------------------------------


def test_lookup_decision_accepts_only_fresh_pending_grants() -> None:
    fresh = _stored_grant(
        state=DeviceAuthorizationGrantState.PENDING,
        expires_at=_DATABASE_NOW + DEVICE_GRANT_LIFETIME,
    )
    assert resolve_lookup_rejection_code(fresh, database_now=_DATABASE_NOW) is None


def test_lookup_decision_rejects_expired_pending_with_the_expired_code() -> None:
    expired = _stored_grant(state=DeviceAuthorizationGrantState.PENDING, expires_at=_DATABASE_NOW)
    assert (
        resolve_lookup_rejection_code(expired, database_now=_DATABASE_NOW)
        is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED
    )


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        (DeviceAuthorizationGrantState.DENIED, ErrorCode.DEVICE_AUTHORIZATION_DENIED),
        (
            DeviceAuthorizationGrantState.APPROVED,
            ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID,
        ),
        (
            DeviceAuthorizationGrantState.EXCHANGED,
            ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID,
        ),
    ],
)
def test_lookup_decision_maps_closed_states(
    state: DeviceAuthorizationGrantState, expected_code: ErrorCode
) -> None:
    grant = _stored_grant(state=state, expires_at=_DATABASE_NOW + DEVICE_GRANT_LIFETIME)
    assert resolve_lookup_rejection_code(grant, database_now=_DATABASE_NOW) is (expected_code)


def test_lookup_decision_rejects_unknown_grants_closed() -> None:
    assert (
        resolve_lookup_rejection_code(None, database_now=_DATABASE_NOW)
        is ErrorCode.DEVICE_CREDENTIAL_INVALID
    )


def test_terminal_decision_maps_expired_pending_and_terminal_states() -> None:
    pending = _stored_grant(
        state=DeviceAuthorizationGrantState.PENDING,
        expires_at=_DATABASE_NOW + DEVICE_GRANT_LIFETIME,
    )
    expired = _stored_grant(state=DeviceAuthorizationGrantState.PENDING, expires_at=_DATABASE_NOW)
    denied = _stored_grant(
        state=DeviceAuthorizationGrantState.DENIED,
        expires_at=_DATABASE_NOW + DEVICE_GRANT_LIFETIME,
    )
    exchanged = _stored_grant(
        state=DeviceAuthorizationGrantState.EXCHANGED,
        expires_at=_DATABASE_NOW + DEVICE_GRANT_LIFETIME,
    )
    assert resolve_terminal_rejection_code(pending, database_now=_DATABASE_NOW) is None
    assert (
        resolve_terminal_rejection_code(expired, database_now=_DATABASE_NOW)
        is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED
    )
    assert (
        resolve_terminal_rejection_code(denied, database_now=_DATABASE_NOW)
        is ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID
    )
    assert (
        resolve_terminal_rejection_code(exchanged, database_now=_DATABASE_NOW)
        is ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID
    )
    assert (
        resolve_terminal_rejection_code(None, database_now=_DATABASE_NOW)
        is ErrorCode.DEVICE_CREDENTIAL_INVALID
    )


# --- service surface ------------------------------------------------------------------


def test_device_authorization_service_exposes_the_transaction_surface() -> None:
    # The service type exists with the three choreographies of this task; the
    # offline/route tests drive the behavior end to end.
    assert hasattr(DeviceAuthorizationService, "create_grant")
    assert hasattr(DeviceAuthorizationService, "lookup_grant")
    assert hasattr(DeviceAuthorizationService, "approve_grant")
    assert hasattr(DeviceAuthorizationService, "deny_grant")


def test_plugin_version_bounds_construct_from_configured_strings() -> None:
    bounds = PluginVersionBounds.from_strings(
        minimum_plugin_version="1.2.3", maximum_plugin_version="4.5.6"
    )
    assert bounds.minimum == (1, 2, 3)
    assert bounds.maximum == (4, 5, 6)
