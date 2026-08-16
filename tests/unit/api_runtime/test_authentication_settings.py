"""Authentication settings grammar loaded from the approved environment fragment.

These tests prove the strict non-secret authentication configuration grammar:
the allowed origin is an exact normalized scheme/host pair that must be HTTPS
outside local development, trusted-proxy entries are validated CIDRs, the
current and previous key references follow a closed file-name grammar without
duplicates, escapes or more than four previous keys, and the plugin version
bounds form an ordered dotted triple. Session limits stay frozen typed
constants matching the Global Constraints.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from api_runtime.authentication_settings import (
    AuthenticationSettings,
    load_authentication_settings,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError

#: Any absolute path works for grammar tests; the keyring tests bind a real
#: temporary secret root through the same field.
_SECRET_ROOT: Path = Path.cwd()


def authentication_environ(**overrides: str) -> dict[str, str]:
    """Build one valid authentication environment snapshot with overrides."""
    environ: dict[str, str] = {
        "KNOWLEDGE_ENVIRONMENT": "test",
        "KNOWLEDGE_SECRET_ROOT": str(_SECRET_ROOT),
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN": "https://admin.example.test",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-1",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "auth-current.key",
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION": "1.13.0",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION": "1.13.1",
    }
    environ.update(overrides)
    return environ


def load_settings(**overrides: str) -> AuthenticationSettings:
    return load_authentication_settings(environ=authentication_environ(**overrides))


def test_valid_settings_parse_every_field() -> None:
    settings = load_settings(
        KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS="10.0.0.0/8",
        KNOWLEDGE_AUTH_PREVIOUS_KEYS="auth-key-0=auth-0.key",
    )
    assert settings.allowed_origin == "https://admin.example.test"
    assert settings.trusted_proxy_cidrs == ("10.0.0.0/8",)
    assert settings.current_key_id == "auth-key-1"
    assert settings.current_key_file == "auth-current.key"
    assert settings.previous_key_files == (("auth-key-0", "auth-0.key"),)
    assert settings.minimum_plugin_version == "1.13.0"
    assert settings.maximum_plugin_version == "1.13.1"


def test_production_origin_must_be_https() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(
            KNOWLEDGE_ENVIRONMENT="production",
            KNOWLEDGE_AUTH_ALLOWED_ORIGIN="http://example.test",
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_remote_environments_reject_http_origin(environment: str) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(
            KNOWLEDGE_ENVIRONMENT=environment,
            KNOWLEDGE_AUTH_ALLOWED_ORIGIN="http://admin.example.test",
        )


def test_local_http_origin_is_accepted_for_loopback_development() -> None:
    settings = load_settings(
        KNOWLEDGE_ENVIRONMENT="local",
        KNOWLEDGE_AUTH_ALLOWED_ORIGIN="http://localhost:3000",
    )
    assert settings.allowed_origin == "http://localhost:3000"


@pytest.mark.parametrize(
    "allowed_origin",
    [
        "https://admin.example.test/login",
        "https://admin.example.test/?query=1",
        "https://admin.example.test#fragment",
        "https://user:pass@admin.example.test",
        "ftp://admin.example.test",
        "https://",
        "not-an-origin",
        "https://admin.example.test:0",
        "https://admin.example.test:65536",
        " https://admin.example.test",
    ],
)
def test_origin_outside_closed_grammar_is_rejected(allowed_origin: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_ALLOWED_ORIGIN=allowed_origin)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_origin_normalizes_scheme_and_host_to_lowercase() -> None:
    settings = load_settings(
        KNOWLEDGE_AUTH_ALLOWED_ORIGIN="HTTPS://Admin.Example.Test:8443",
    )
    assert settings.allowed_origin == "https://admin.example.test:8443"


def test_trusted_proxy_cidrs_parse_in_order_and_normalize() -> None:
    settings = load_settings(
        KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS="10.0.0.0/8,fd00::/8,192.168.0.0/16",
    )
    assert settings.trusted_proxy_cidrs == ("10.0.0.0/8", "fd00::/8", "192.168.0.0/16")


def test_trusted_proxy_cidrs_default_to_empty_sequence() -> None:
    assert load_settings().trusted_proxy_cidrs == ()


@pytest.mark.parametrize(
    "trusted_proxy_cidrs",
    [
        "not-a-cidr",
        "10.0.0.0/33",
        "10.0.0.1/8",
        "10.0.0.0/8,,192.168.0.0/16",
        "10.0.0.0/8,",
        " 10.0.0.0/8",
    ],
)
def test_malformed_trusted_proxy_cidr_is_rejected(trusted_proxy_cidrs: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS=trusted_proxy_cidrs)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_previous_keys_parse_in_declaration_order() -> None:
    settings = load_settings(
        KNOWLEDGE_AUTH_PREVIOUS_KEYS="auth-key-0=auth-0.key,auth-key-2=auth-2.key",
    )
    assert settings.previous_key_files == (
        ("auth-key-0", "auth-0.key"),
        ("auth-key-2", "auth-2.key"),
    )


def test_previous_keys_default_to_empty_sequence() -> None:
    assert load_settings().previous_key_files == ()


@pytest.mark.parametrize(
    "previous_keys",
    [
        "auth-key-0",
        "auth-key-0=",
        "=auth-0.key",
        "auth-key-0=auth-0.key=auth-1.key",
    ],
)
def test_previous_key_entry_outside_pair_grammar_is_rejected(previous_keys: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_PREVIOUS_KEYS=previous_keys)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


@pytest.mark.parametrize(
    "key_file",
    ["../escape.key", "auth/../escape.key", "..", "/etc/auth-current.key", "C:\\secrets\\key"],
)
def test_current_key_file_outside_secret_root_grammar_is_rejected(key_file: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_CURRENT_KEY_FILE=key_file)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


@pytest.mark.parametrize(
    "previous_keys",
    ["auth-key-0=../escape.key", "auth-key-0=..", "auth-key-0=/etc/auth-0.key"],
)
def test_previous_key_file_outside_secret_root_grammar_is_rejected(
    previous_keys: str,
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_PREVIOUS_KEYS=previous_keys)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_duplicate_previous_key_ids_are_rejected() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(
            KNOWLEDGE_AUTH_PREVIOUS_KEYS="auth-key-0=auth-0.key,auth-key-0=auth-2.key",
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_duplicate_previous_key_files_are_rejected() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(
            KNOWLEDGE_AUTH_PREVIOUS_KEYS="auth-key-0=auth-0.key,auth-key-2=auth-0.key",
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_previous_key_id_colliding_with_current_key_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_PREVIOUS_KEYS="auth-key-1=auth-0.key")
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_previous_key_file_colliding_with_current_key_file_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_PREVIOUS_KEYS="auth-key-0=auth-current.key")
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_more_than_four_previous_keys_are_rejected() -> None:
    previous_keys = ",".join(f"auth-key-{index}=auth-{index}.key" for index in range(5))
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_PREVIOUS_KEYS=previous_keys)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


@pytest.mark.parametrize("key_id", ["", "-leading-dash", "UPPER-KEY", "key with space"])
def test_key_id_outside_identifier_grammar_is_rejected(key_id: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_CURRENT_KEY_ID=key_id)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


@pytest.mark.parametrize(
    "plugin_version",
    ["1.13", "v1.13.1", "1.13.1-beta", "", "1.13.1.0"],
)
def test_plugin_version_outside_dotted_triple_grammar_is_rejected(
    plugin_version: str,
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION=plugin_version)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_inverted_plugin_version_bounds_are_rejected() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(
            KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION="1.14.0",
            KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION="1.13.1",
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_session_limits_are_frozen_constants_matching_global_constraints() -> None:
    assert AuthenticationSettings.SESSION_PENDING_TOTP_TTL_SECONDS == 300
    assert AuthenticationSettings.SESSION_IDLE_TTL_HOURS == 12
    assert AuthenticationSettings.SESSION_ABSOLUTE_TTL_DAYS == 7
    assert AuthenticationSettings.RECENT_REAUTHENTICATION_WINDOW_SECONDS == 300
    assert AuthenticationSettings.AUTHENTICATION_KEY_SIZE_BYTES == 32
    assert AuthenticationSettings.MAXIMUM_PREVIOUS_KEY_COUNT == 4


def test_unknown_knowledge_key_is_terminal() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(KNOWLEDGE_AUTH_ALLOWED_ORIGN="https://admin.example.test")
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY
