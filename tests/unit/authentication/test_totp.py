"""Pure TOTP computation, replay-window and recovery-value contracts (spec 10).

The domain unit tests pin the interoperable RFC 6238 HMAC-SHA-1 contract
against the RFC's own test vectors (six and eight digits at unix second 59),
the ±1 time-step acceptance window with the newer-than-marker replay rule,
the 160-bit secret generation bounds, the provisioning URI shape (fixed
issuer, canonical username, Base32 secret, pinned parameters), and the
recovery-code vocabulary: ten codes of twelve Base32 characters grouped for
readability, normalization of pasted spellings and the domain-separated
keyed hash.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

import pytest

from personal_os.authentication.crypto import RECOVERY_CODE_HASH_LABEL
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.totp import (
    RECOVERY_CODE_ALPHABET,
    RECOVERY_CODE_COUNT,
    RECOVERY_CODE_GROUP_SIZE,
    RECOVERY_CODE_LENGTH_CHARACTERS,
    TOTP_ACCEPTED_STEP_WINDOW,
    TOTP_ENROLLMENT_EXPIRY,
    TOTP_PERIOD_SECONDS,
    TOTP_PROVISIONING_ISSUER,
    TOTP_SECRET_ENTROPY_BYTES,
    TotpEnrollmentAction,
    generate_recovery_codes,
    generate_totp_secret,
    normalize_recovery_code,
    recovery_code_hash,
    resolve_totp_step,
    time_step_of,
    totp_code,
    totp_provisioning_uri,
)
from personal_os.error_contracts.codes import ErrorCode

#: The RFC 6238 Appendix B seed shared by every vector.
_RFC_SECRET: Final[bytes] = b"12345678901234567890"

#: A fixed twenty-byte secret for window tests.
_WINDOW_SECRET: Final[bytes] = bytes(range(20))

#: One pinned verification moment for window tests.
_WINDOW_UNIX_SECONDS: Final[int] = 1_800_000_059


# --- RFC 6238 vectors -----------------------------------------------------------------


def test_rfc6238_sha1_vector_at_59_seconds() -> None:
    assert totp_code(secret=b"12345678901234567890", unix_time_seconds=59, digits=8) == "94287082"


def test_rfc6238_sha1_six_digit_vector_at_59_seconds() -> None:
    assert totp_code(secret=_RFC_SECRET, unix_time_seconds=59, digits=6) == "287082"


def test_runtime_contract_is_six_digits_thirty_seconds() -> None:
    code = totp_code(secret=_RFC_SECRET, unix_time_seconds=59)
    assert code == "287082"
    assert len(code) == 6


def test_time_step_of_partitions_thirty_second_periods() -> None:
    assert time_step_of(unix_time_seconds=0) == 0
    assert time_step_of(unix_time_seconds=29) == 0
    assert time_step_of(unix_time_seconds=30) == 1
    assert time_step_of(unix_time_seconds=59) == 1


# --- replay window ----------------------------------------------------------------------


def _code_at(step: int) -> str:
    return totp_code(
        secret=_WINDOW_SECRET, unix_time_seconds=step * TOTP_PERIOD_SECONDS
    )


def test_window_accepts_previous_current_and_next_step() -> None:
    current_step = time_step_of(unix_time_seconds=_WINDOW_UNIX_SECONDS)
    for offset in range(-TOTP_ACCEPTED_STEP_WINDOW, TOTP_ACCEPTED_STEP_WINDOW + 1):
        assert (
            resolve_totp_step(
                submitted_code=_code_at(current_step + offset),
                secret=_WINDOW_SECRET,
                last_accepted_time_step=None,
                unix_time_seconds=_WINDOW_UNIX_SECONDS,
            )
            == current_step + offset
        )


def test_window_rejects_drift_beyond_one_step() -> None:
    current_step = time_step_of(unix_time_seconds=_WINDOW_UNIX_SECONDS)
    for drift in (-2, 2, 5):
        assert (
            resolve_totp_step(
                submitted_code=_code_at(current_step + drift),
                secret=_WINDOW_SECRET,
                last_accepted_time_step=None,
                unix_time_seconds=_WINDOW_UNIX_SECONDS,
            )
            is None
        )


def test_step_not_newer_than_marker_is_a_replay() -> None:
    current_step = time_step_of(unix_time_seconds=_WINDOW_UNIX_SECONDS)
    assert (
        resolve_totp_step(
            submitted_code=_code_at(current_step),
            secret=_WINDOW_SECRET,
            last_accepted_time_step=current_step,
            unix_time_seconds=_WINDOW_UNIX_SECONDS,
        )
        is None
    )


def test_marker_at_previous_step_still_accepts_current_step() -> None:
    current_step = time_step_of(unix_time_seconds=_WINDOW_UNIX_SECONDS)
    assert (
        resolve_totp_step(
            submitted_code=_code_at(current_step),
            secret=_WINDOW_SECRET,
            last_accepted_time_step=current_step - 1,
            unix_time_seconds=_WINDOW_UNIX_SECONDS,
        )
        == current_step
    )


def test_wrong_code_for_known_secret_is_rejected() -> None:
    assert (
        resolve_totp_step(
            submitted_code="000000",
            secret=_WINDOW_SECRET,
            last_accepted_time_step=None,
            unix_time_seconds=_WINDOW_UNIX_SECONDS,
        )
        is None
    )


# --- secret generation and provisioning URI ----------------------------------------------


def test_generated_secret_is_160_bits_of_fresh_entropy() -> None:
    first = generate_totp_secret()
    second = generate_totp_secret()
    assert len(first) == TOTP_SECRET_ENTROPY_BYTES == 20
    assert len(second) == 20
    assert first != second


def test_provisioning_uri_carries_issuer_label_secret_and_parameters() -> None:
    uri = totp_provisioning_uri(secret=_WINDOW_SECRET, username="admin")
    secret_base32 = base64.b32encode(_WINDOW_SECRET).decode("ascii")
    label = f"{quote(TOTP_PROVISIONING_ISSUER, safe='')}:{quote('admin', safe='')}"
    assert uri.startswith(f"otpauth://totp/{label}?")
    assert f"secret={secret_base32}" in uri
    assert f"issuer={quote(TOTP_PROVISIONING_ISSUER, safe='')}" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_enrollment_expiry_is_ten_minutes() -> None:
    assert TOTP_ENROLLMENT_EXPIRY.total_seconds() == 600


def test_enrollment_action_vocabulary_is_closed() -> None:
    assert {action.value for action in TotpEnrollmentAction} == {
        "start",
        "dismiss_initial_offer",
    }


# --- recovery values ----------------------------------------------------------------------


def test_recovery_codes_are_ten_grouped_twelve_character_base32_values() -> None:
    codes = generate_recovery_codes()
    assert len(codes) == RECOVERY_CODE_COUNT == 10
    for code in codes:
        groups = code.split("-")
        assert len(groups) == RECOVERY_CODE_LENGTH_CHARACTERS // RECOVERY_CODE_GROUP_SIZE
        assert all(len(group) == RECOVERY_CODE_GROUP_SIZE for group in groups)
        assert all(character in RECOVERY_CODE_ALPHABET for group in groups for character in group)
    assert len(set(codes)) == len(codes)


def test_normalize_recovery_code_strips_grouping_and_case() -> None:
    assert normalize_recovery_code("ABCD-EFGH-IJKL") == "ABCDEFGHIJKL"
    assert normalize_recovery_code("abcd efgh ijkl") == "ABCDEFGHIJKL"
    assert normalize_recovery_code("abcdefghijk l") == "ABCDEFGHIJKL"


@pytest.mark.parametrize(
    "value",
    [
        "ABCD-EFGH-IJK",
        "ABCD-EFGH-IJKLM",
        "ABCD-EFGH-IJK1",
        "ABCD-EFGH-IJK0",
        "",
        "ABCD-EFGH-IJKL-EXTRA-VALUE",
    ],
)
def test_normalize_recovery_code_rejects_foreign_grammar(value: str) -> None:
    with pytest.raises(AuthenticationError) as rejected:
        normalize_recovery_code(value)
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED


def test_recovery_code_hash_is_deterministic_and_code_distinct() -> None:
    first = recovery_code_hash(hmac_key=b"k" * 32, normalized_code="ABCDEFGHIJKL")
    second = recovery_code_hash(hmac_key=b"k" * 32, normalized_code="ABCDEFGHIJKL")
    other = recovery_code_hash(hmac_key=b"k" * 32, normalized_code="AAAAAAAAAAAA")
    assert first == second
    assert len(first) == 64
    assert first != other


def test_recovery_hashing_domain_is_separate_from_other_labels() -> None:
    assert RECOVERY_CODE_HASH_LABEL == "auth/recovery/v1"


# --- a pinned end-to-end moment -----------------------------------------------------------


def test_pinned_clock_moment_resolves_through_service_helper() -> None:
    # Guards the unix-second derivation the services share: the fixed offline
    # clock timestamp must map deterministically onto exactly one step.
    pinned = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    unix_seconds = int(pinned.timestamp())
    assert unix_seconds == 1_767_225_600
    assert time_step_of(unix_time_seconds=unix_seconds) == 58_907_520
