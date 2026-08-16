"""Exact-replay device token derivations, classification and pacing (spec 12, 13).

These tests pin the byte-exact keyed derivations of design sections 12.2 and
13.4: the golden refresh-successor vector derived through RFC 5869
HKDF-SHA-256 (32-byte zero salt, ``auth/refresh-replay/v1`` info) plus
HMAC-SHA-256 over the predecessor secret, rotation identity, family identity
and big-endian successor generation; the access/exchange derivations under
their own pinned labels; the opaque ``at1.``/``rt1.``/``pg1.`` credential
formatting and parsing round trips; the pure refresh classification of exact
replay versus confirmed reuse versus a new rotation; the in-memory poll
pacing window; and the lifetime constants of sections 13.1 and 13.2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from personal_os.authentication.contracts import DeviceTokenState
from personal_os.authentication.device_tokens import (
    ACCESS_TOKEN_LIFETIME_SECONDS,
    DEVICE_LAST_SEEN_MAXIMUM_UPDATE_INTERVAL,
    INITIAL_REFRESH_GENERATION,
    REFRESH_ABSOLUTE_LIFETIME,
    REFRESH_INACTIVITY_LIFETIME,
    DerivedTokenSecret,
    GrantPollPacer,
    StoredDeviceToken,
    StoredTokenFamily,
    classify_refresh_presentation,
    derive_exchange_access_credential,
    derive_exchange_refresh_credential,
    derive_refresh_successor,
    derive_rotation_access_credential,
    format_access_credential,
    format_refresh_credential,
    parse_polling_credential,
    refresh_secret_hash_of,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.error_contracts.codes import ErrorCode

_MASTER_KEY = bytes(range(32))
_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_ROTATION_ID = UUID("00000000-0000-0000-0000-000000000001")
_TOKEN_FAMILY_ID = UUID("00000000-0000-0000-0000-000000000002")


# --- golden derivation vectors -----------------------------------------------------


def test_refresh_derivation_vector_is_stable() -> None:
    result = derive_refresh_successor(
        master_key=_MASTER_KEY,
        predecessor_secret=bytes(range(32, 64)),
        rotation_id=UUID("00000000-0000-0000-0000-000000000001"),
        token_family_id=UUID("00000000-0000-0000-0000-000000000002"),
        successor_generation=2,
    )
    assert result.secret.hex() == "266ad59acb65e0a437eb79891fa1a349fd1a5e90f531ccf3e442b1920d8a5141"


def test_refresh_derivation_is_deterministic_and_input_bound() -> None:
    arguments = {
        "master_key": _MASTER_KEY,
        "predecessor_secret": bytes(range(32, 64)),
        "rotation_id": _ROTATION_ID,
        "token_family_id": _TOKEN_FAMILY_ID,
        "successor_generation": 2,
    }
    first = derive_refresh_successor(**arguments)
    second = derive_refresh_successor(**arguments)
    assert first.secret == second.secret
    assert len(first.secret) == 32
    for changed in (
        {**arguments, "predecessor_secret": bytes(range(33, 65))},
        {**arguments, "rotation_id": uuid4()},
        {**arguments, "token_family_id": uuid4()},
        {**arguments, "successor_generation": 3},
        {**arguments, "master_key": bytes(range(1, 33))},
    ):
        assert derive_refresh_successor(**changed).secret != first.secret


def test_derivation_labels_separate_access_refresh_and_exchange_domains() -> None:
    predecessor_secret = bytes(range(32, 64))
    rotation_arguments = {
        "master_key": _MASTER_KEY,
        "predecessor_secret": predecessor_secret,
        "rotation_id": _ROTATION_ID,
        "token_family_id": _TOKEN_FAMILY_ID,
        "successor_generation": 2,
    }
    refresh_secret = derive_refresh_successor(**rotation_arguments).secret
    access_secret = derive_rotation_access_credential(**rotation_arguments).secret
    assert refresh_secret != access_secret

    polling_secret = b"polling-secret-material-0123456789"
    grant_id = uuid4()
    access_token_id = uuid4()
    refresh_token_id = uuid4()
    exchange_access = derive_exchange_access_credential(
        master_key=_MASTER_KEY,
        polling_secret=polling_secret,
        grant_id=grant_id,
        token_lookup_id=access_token_id,
        generation=INITIAL_REFRESH_GENERATION,
    ).secret
    exchange_refresh = derive_exchange_refresh_credential(
        master_key=_MASTER_KEY,
        polling_secret=polling_secret,
        grant_id=grant_id,
        token_lookup_id=refresh_token_id,
        generation=INITIAL_REFRESH_GENERATION,
    ).secret
    assert exchange_access != exchange_refresh
    assert exchange_access not in (refresh_secret, access_secret)
    # The derivation is stable across invocations and bound to every input.
    assert (
        derive_exchange_access_credential(
            master_key=_MASTER_KEY,
            polling_secret=polling_secret,
            grant_id=grant_id,
            token_lookup_id=access_token_id,
            generation=INITIAL_REFRESH_GENERATION,
        ).secret
        == exchange_access
    )
    assert (
        derive_exchange_refresh_credential(
            master_key=_MASTER_KEY,
            polling_secret=polling_secret,
            grant_id=uuid4(),
            token_lookup_id=refresh_token_id,
            generation=INITIAL_REFRESH_GENERATION,
        ).secret
        != exchange_refresh
    )


def test_secret_hashes_are_stable_hex_digests_of_the_presented_secret() -> None:
    secret = bytes(range(64))
    first = refresh_secret_hash_of(master_key=_MASTER_KEY, secret=secret)
    assert first == refresh_secret_hash_of(master_key=_MASTER_KEY, secret=secret)
    assert len(first) == 64
    assert first != refresh_secret_hash_of(master_key=bytes(range(1, 33)), secret=secret)


# --- credential formatting and parsing ----------------------------------------------


def test_formatted_credentials_round_trip_through_the_versioned_parsers() -> None:
    from personal_os.authentication.crypto import (
        parse_access_credential,
        parse_refresh_credential,
    )

    access_lookup_id = uuid4()
    refresh_lookup_id = uuid4()
    access_secret = derive_exchange_access_credential(
        master_key=_MASTER_KEY,
        polling_secret=b"polling-secret-material-0123456789",
        grant_id=uuid4(),
        token_lookup_id=access_lookup_id,
        generation=INITIAL_REFRESH_GENERATION,
    ).secret
    refresh_secret = derive_refresh_successor(
        master_key=_MASTER_KEY,
        predecessor_secret=bytes(range(32, 64)),
        rotation_id=_ROTATION_ID,
        token_family_id=_TOKEN_FAMILY_ID,
        successor_generation=2,
    ).secret
    access_credential = format_access_credential(lookup_id=access_lookup_id, secret=access_secret)
    refresh_credential = format_refresh_credential(
        lookup_id=refresh_lookup_id, secret=refresh_secret
    )
    assert access_credential.startswith(f"at1.{access_lookup_id}.")
    assert refresh_credential.startswith(f"rt1.{refresh_lookup_id}.")
    parsed_access = parse_access_credential(access_credential)
    parsed_refresh = parse_refresh_credential(refresh_credential)
    assert parsed_access.lookup_id == access_lookup_id
    assert parsed_access.secret == access_secret.hex().encode("ascii")
    assert parsed_refresh.lookup_id == refresh_lookup_id
    assert parsed_refresh.secret == refresh_secret.hex().encode("ascii")


def test_polling_credential_parser_accepts_only_the_pg1_grammar() -> None:
    grant_id = uuid4()
    credential = f"pg1.{grant_id}.{bytes(range(32)).hex()}"
    parsed = parse_polling_credential(credential)
    assert parsed.grant_id == grant_id
    assert parsed.secret == bytes(range(32)).hex().encode("ascii")
    for rejected in ("", "pg1", f"pg1.{grant_id}", f"at1.{grant_id}.secretsecret01", "pg1.x.y"):
        with pytest.raises(AuthenticationError) as raised:
            parse_polling_credential(rejected)
        assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID


# --- refresh classification (spec 13.4, 13.5) ---------------------------------------


def _stored_predecessor(
    *,
    state: DeviceTokenState = DeviceTokenState.ACTIVE,
    generation: int = 1,
    expires_at: datetime | None = None,
) -> StoredDeviceToken:
    return StoredDeviceToken(
        device_token_id=uuid4(),
        token_family_id=_TOKEN_FAMILY_ID,
        user_id=uuid4(),
        workspace_id=uuid4(),
        device_id=uuid4(),
        generation=generation,
        state=state,
        successor_token_id=None,
        rotation_id=None,
        derivation_key_id="key-current",
        issued_at=_DATABASE_NOW - timedelta(days=1),
        expires_at=expires_at or _DATABASE_NOW + timedelta(days=30),
        rotated_at=None if state is DeviceTokenState.ACTIVE else _DATABASE_NOW,
        revoked_at=_DATABASE_NOW if state is DeviceTokenState.REVOKED else None,
    )


def _stored_family(
    *,
    current_refresh_generation: int = 1,
    inactivity_expires_at: datetime | None = None,
) -> StoredTokenFamily:
    return StoredTokenFamily(
        token_family_id=_TOKEN_FAMILY_ID,
        user_id=uuid4(),
        workspace_id=uuid4(),
        device_id=uuid4(),
        current_refresh_generation=current_refresh_generation,
        inactivity_expires_at=inactivity_expires_at or _DATABASE_NOW + timedelta(days=29),
        absolute_expires_at=_DATABASE_NOW + timedelta(days=60),
        last_refreshed_at=_DATABASE_NOW - timedelta(days=1),
        created_at=_DATABASE_NOW - timedelta(days=1),
    )


def test_current_predecessor_with_new_rotation_classifies_as_new_rotation() -> None:
    predecessor = _stored_predecessor()
    family = _stored_family()
    assert (
        classify_refresh_presentation(
            predecessor=predecessor,
            successor=None,
            family=family,
            presented_rotation_id=_ROTATION_ID,
            database_now=_DATABASE_NOW,
        )
        == "new_rotation"
    )


def test_same_rotation_against_rotated_current_successor_classifies_as_exact_replay() -> None:
    predecessor = _stored_predecessor(state=DeviceTokenState.ROTATED)
    successor = StoredDeviceToken(
        device_token_id=uuid4(),
        token_family_id=_TOKEN_FAMILY_ID,
        user_id=predecessor.user_id,
        workspace_id=predecessor.workspace_id,
        device_id=predecessor.device_id,
        generation=2,
        state=DeviceTokenState.ACTIVE,
        predecessor_token_id=predecessor.device_token_id,
        rotation_id=_ROTATION_ID,
        derivation_key_id="key-current",
        issued_at=_DATABASE_NOW,
        expires_at=_DATABASE_NOW + timedelta(days=30),
    )
    family = _stored_family(current_refresh_generation=2)
    assert (
        classify_refresh_presentation(
            predecessor=predecessor,
            successor=successor,
            family=family,
            presented_rotation_id=_ROTATION_ID,
            database_now=_DATABASE_NOW,
        )
        == "exact_replay"
    )


def test_different_rotation_against_rotated_predecessor_classifies_as_reuse() -> None:
    predecessor = _stored_predecessor(state=DeviceTokenState.ROTATED)
    successor = StoredDeviceToken(
        device_token_id=uuid4(),
        token_family_id=_TOKEN_FAMILY_ID,
        user_id=predecessor.user_id,
        workspace_id=predecessor.workspace_id,
        device_id=predecessor.device_id,
        generation=2,
        state=DeviceTokenState.ACTIVE,
        predecessor_token_id=predecessor.device_token_id,
        rotation_id=uuid4(),
        derivation_key_id="key-current",
        issued_at=_DATABASE_NOW,
        expires_at=_DATABASE_NOW + timedelta(days=30),
    )
    family = _stored_family(current_refresh_generation=2)
    assert (
        classify_refresh_presentation(
            predecessor=predecessor,
            successor=successor,
            family=family,
            presented_rotation_id=_ROTATION_ID,
            database_now=_DATABASE_NOW,
        )
        == "reuse_detected"
    )


def test_expired_revoked_and_stale_presentations_classify_as_reuse() -> None:
    predecessor = _stored_predecessor(expires_at=_DATABASE_NOW - timedelta(seconds=1))
    assert (
        classify_refresh_presentation(
            predecessor=predecessor,
            successor=None,
            family=_stored_family(),
            presented_rotation_id=_ROTATION_ID,
            database_now=_DATABASE_NOW,
        )
        == "reuse_detected"
    )
    revoked = _stored_predecessor(state=DeviceTokenState.REVOKED)
    assert (
        classify_refresh_presentation(
            predecessor=revoked,
            successor=None,
            family=_stored_family(),
            presented_rotation_id=_ROTATION_ID,
            database_now=_DATABASE_NOW,
        )
        == "reuse_detected"
    )
    # An active predecessor that is no longer the family's current generation
    # is stale lineage evidence: confirmed reuse.
    stale = _stored_predecessor(generation=1)
    assert (
        classify_refresh_presentation(
            predecessor=stale,
            successor=None,
            family=_stored_family(current_refresh_generation=2),
            presented_rotation_id=_ROTATION_ID,
            database_now=_DATABASE_NOW,
        )
        == "reuse_detected"
    )
    # A family past its inactivity window used as current is reuse as well.
    inactive = _stored_predecessor()
    assert (
        classify_refresh_presentation(
            predecessor=inactive,
            successor=None,
            family=_stored_family(inactivity_expires_at=_DATABASE_NOW - timedelta(minutes=1)),
            presented_rotation_id=_ROTATION_ID,
            database_now=_DATABASE_NOW,
        )
        == "reuse_detected"
    )


# --- poll pacing (spec 11.4) --------------------------------------------------------


def test_poll_pacer_starts_at_five_seconds_and_backs_off() -> None:
    pacer = GrantPollPacer()
    assert pacer.register_pending_poll(uuid4(), _DATABASE_NOW) is None
    grant_id = uuid4()
    assert pacer.register_pending_poll(grant_id, _DATABASE_NOW) is None
    fast = pacer.register_pending_poll(grant_id, _DATABASE_NOW + timedelta(seconds=2))
    assert fast is not None and fast >= 1
    # The rejected poll doubled the minimum interval: the next allowed poll
    # stays one minimum interval after the last accepted one.
    still_fast = pacer.register_pending_poll(grant_id, _DATABASE_NOW + timedelta(seconds=5))
    assert still_fast is not None
    assert pacer.register_pending_poll(grant_id, _DATABASE_NOW + timedelta(seconds=20)) is None


# --- lifetime constants (spec 13.1, 13.2) -------------------------------------------


def test_lifetime_constants_match_the_design() -> None:
    assert ACCESS_TOKEN_LIFETIME_SECONDS == 15 * 60
    assert timedelta(days=30) == REFRESH_INACTIVITY_LIFETIME
    assert timedelta(days=90) == REFRESH_ABSOLUTE_LIFETIME
    assert timedelta(minutes=5) == DEVICE_LAST_SEEN_MAXIMUM_UPDATE_INTERVAL
    assert INITIAL_REFRESH_GENERATION == 1


def test_derived_secret_repr_hides_the_material() -> None:
    secret = DerivedTokenSecret(secret=b"raw-secret-material")
    assert "raw-secret-material" not in repr(secret)
