"""Password policy, offline blocklist loading and Argon2id parameter pins.

These tests prove the 15-128 Unicode-character policy with spaces and no
composition rule (spec 8.1), rejection of values present in the committed
SecLists-derived SHA-256 blocklist through a constant-time digest probe, the
artifact/provenance contract of the committed digest file, and the pinned
Argon2id work parameters later adapters must consume.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
from dataclasses import FrozenInstanceError

import pytest

from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.passwords import (
    ARGON2ID_HASH_LENGTH_BYTES,
    ARGON2ID_MEMORY_COST_KIB,
    ARGON2ID_PARALLELISM_LANES,
    ARGON2ID_SALT_LENGTH_BYTES,
    ARGON2ID_TIME_COST_ITERATIONS,
    COMMON_PASSWORD_BLOCKLIST_DIGEST_COUNT,
    PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    PASSWORD_MINIMUM_LENGTH_CHARACTERS,
    PasswordBlocklist,
    load_common_password_blocklist,
    validate_new_password,
)
from personal_os.error_contracts.codes import ErrorCode

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Verified present in the SecLists top-10000 source list (one of only two
#: entries with at least fifteen characters), so its digest is in the artifact.
COMMON_BLOCKED_PASSWORD = "Mailcreated5240"

#: Verified absent from the SecLists top-10000 in any spelling; the policy
#: therefore accepts it and this test documents that verified fact.
UNLISTED_PASSPHRASE = "passwordpassword"


@pytest.fixture
def blocklist() -> PasswordBlocklist:
    return load_common_password_blocklist()


def test_password_policy_accepts_spaces_and_rejects_common_value(
    blocklist: PasswordBlocklist,
) -> None:
    assert (
        validate_new_password("correct horse battery staple!", blocklist)
        == "correct horse battery staple!"
    )
    with pytest.raises(AuthenticationError) as raised:
        validate_new_password(COMMON_BLOCKED_PASSWORD, blocklist)
    assert raised.value.error_code is ErrorCode.AUTHENTICATION_FAILED


def test_rejected_password_value_never_appears_in_error_rendering(
    blocklist: PasswordBlocklist,
) -> None:
    with pytest.raises(AuthenticationError) as raised:
        validate_new_password(COMMON_BLOCKED_PASSWORD, blocklist)
    rendered = f"{raised.value!r} {raised.value} {raised.value.to_safe_dict()}"
    assert COMMON_BLOCKED_PASSWORD not in rendered


def test_policy_bounds_are_fifteen_through_one_hundred_twenty_eight_characters(
    blocklist: PasswordBlocklist,
) -> None:
    assert PASSWORD_MINIMUM_LENGTH_CHARACTERS == 15
    assert PASSWORD_MAXIMUM_LENGTH_CHARACTERS == 128
    assert validate_new_password("a" * 15, blocklist) == "a" * 15
    assert validate_new_password("a" * 128, blocklist) == "a" * 128
    with pytest.raises(AuthenticationError) as too_short:
        validate_new_password("a" * 14, blocklist)
    assert too_short.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    with pytest.raises(AuthenticationError) as too_long:
        validate_new_password("a" * 129, blocklist)
    assert too_long.value.error_code is ErrorCode.AUTHENTICATION_FAILED


def test_policy_counts_unicode_code_points_and_has_no_composition_rule(
    blocklist: PasswordBlocklist,
) -> None:
    unicode_password = "mật khẩu cá nhân đủ dài an toàn"
    assert len(unicode_password) >= PASSWORD_MINIMUM_LENGTH_CHARACTERS
    assert validate_new_password(unicode_password, blocklist) == unicode_password


def test_policy_accepts_verified_unlisted_common_shape(blocklist: PasswordBlocklist) -> None:
    """The brief's original sentinel is verified absent from the source list."""
    assert validate_new_password(UNLISTED_PASSPHRASE, blocklist) == UNLISTED_PASSPHRASE


def test_blocklist_probe_is_case_insensitive_over_lowercased_digests(
    blocklist: PasswordBlocklist,
) -> None:
    assert blocklist.is_blocked(COMMON_BLOCKED_PASSWORD) is True
    assert blocklist.is_blocked(COMMON_BLOCKED_PASSWORD.lower()) is True
    assert blocklist.is_blocked("correct horse battery staple!") is False
    digest = hashlib.sha256(COMMON_BLOCKED_PASSWORD.lower().encode("utf-8")).hexdigest()
    assert digest in blocklist.digests


def test_committed_blocklist_artifact_is_sorted_and_complete(
    blocklist: PasswordBlocklist,
) -> None:
    assert len(blocklist.digests) == COMMON_PASSWORD_BLOCKLIST_DIGEST_COUNT == 9_913
    assert all(_DIGEST_PATTERN.fullmatch(digest) for digest in blocklist.digests)
    assert list(blocklist.digests) == sorted(blocklist.digests)
    assert len(set(blocklist.digests)) == len(blocklist.digests)


def test_blocklist_is_frozen_with_bounded_repr(blocklist: PasswordBlocklist) -> None:
    with pytest.raises(FrozenInstanceError):
        blocklist.digests = ()  # type: ignore[misc]
    assert "digest_count=9913" in repr(blocklist)
    assert blocklist.digests[0] not in repr(blocklist)


def test_blocklist_loader_rejects_malformed_digest_lines() -> None:
    with pytest.raises(ValueError, match="blocklist digest lines"):
        PasswordBlocklist.from_digest_text("not-a-digest\n" * 3)
    with pytest.raises(ValueError, match="blocklist digest lines"):
        PasswordBlocklist.from_digest_text(
            hashlib.sha256(b"a").hexdigest() + "\n" + hashlib.sha256(b"b").hexdigest()
        )


def test_blocklist_loader_rejects_unsorted_and_duplicate_digests() -> None:
    ascending_first = "0" * 64
    ascending_last = "f" * 64
    with pytest.raises(ValueError, match="blocklist digest lines"):
        PasswordBlocklist.from_digest_text(f"{ascending_last}\n{ascending_first}\n")
    with pytest.raises(ValueError, match="blocklist digest lines"):
        PasswordBlocklist.from_digest_text(f"{ascending_first}\n{ascending_first}\n")


def test_provenance_records_source_release_url_and_counts() -> None:
    provenance = json.loads(_read_provenance_text())
    assert provenance["source_release"] == "2025.2"
    assert (
        provenance["source_url"]
        == "https://raw.githubusercontent.com/danielmiessler/SecLists/2025.2/"
        "Passwords/Common-Credentials/10-million-password-list-top-10000.txt"
    )
    assert provenance["source_line_count"] == 10_000
    assert provenance["lowercased_duplicate_line_count"] == 87
    assert provenance["digest_count"] == COMMON_PASSWORD_BLOCKLIST_DIGEST_COUNT
    assert re.fullmatch(r"[0-9a-f]{64}", provenance["source_sha256"])
    assert provenance["generator_version"]
    assert "T" in provenance["generated_at"]


def _read_provenance_text() -> str:
    resource = importlib.resources.files("personal_os.authentication").joinpath(
        "data/common-password-sha256-v1.provenance.json"
    )
    return resource.read_text(encoding="utf-8")


def test_argon2id_parameters_are_pinned_with_units() -> None:
    assert ARGON2ID_MEMORY_COST_KIB == 65_536
    assert ARGON2ID_TIME_COST_ITERATIONS == 3
    assert ARGON2ID_PARALLELISM_LANES == 1
    assert ARGON2ID_SALT_LENGTH_BYTES == 16
    assert ARGON2ID_HASH_LENGTH_BYTES == 32
