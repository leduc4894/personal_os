"""Frozen source-database runtime settings loaded from secret files only.

These tests prove the database settings composition for the source store
adapter: the closed environment-name map mirrors the core database fragment,
the connection-field grammar, the secret-file-only password boundary, the
rejection of ``DATABASE_URL``, plaintext passwords and unknown ``KNOWLEDGE_*``
keys, the pinned runtime bounds (pool size/overflow/timeouts) and that no
error or representation renders a value or path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError, SecretFileError
from personal_os.runtime_configuration.environment_names import DATABASE_ENVIRONMENT_NAMES
from personal_os.runtime_configuration.models import RuntimeEnvironment
from postgresql_source_store.engine import TRANSACTION_BOUND_STATEMENTS
from postgresql_source_store.settings import (
    CONNECT_TIMEOUT_SECONDS,
    DATABASE_RUNTIME_ENVIRONMENT_FIELDS,
    IDLE_IN_TRANSACTION_SESSION_TIMEOUT_SECONDS,
    LOCK_TIMEOUT_SECONDS,
    MAX_POOL_OVERFLOW,
    POOL_SIZE,
    POOL_TIMEOUT_SECONDS,
    STATEMENT_TIMEOUT_SECONDS,
    DatabaseRuntimeSettings,
    load_database_runtime_settings,
    read_database_runtime_password,
)

_PASSWORD_FILE = "postgres_application_password"


def _write_password_file(secret_root: Path, value: str) -> None:
    (secret_root / _PASSWORD_FILE).write_text(value, encoding="utf-8")


def _valid_environ(secret_root: Path, **overrides: str) -> dict[str, str]:
    environ: dict[str, str] = {"KNOWLEDGE_SECRET_ROOT": str(secret_root)}
    environ.update(overrides)
    return environ


# --- closed environment-name map mirrors the core database fragment --------


def test_database_runtime_environment_field_map_is_closed_and_exact() -> None:
    assert set(DATABASE_RUNTIME_ENVIRONMENT_FIELDS) == {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_DATABASE_HOST",
        "KNOWLEDGE_DATABASE_PORT",
        "KNOWLEDGE_DATABASE_NAME",
        "KNOWLEDGE_DATABASE_USER",
        "KNOWLEDGE_DATABASE_PASSWORD_FILE",
        "KNOWLEDGE_DATABASE_SSL_MODE",
    }
    # The owned fragment must match the core registry exactly: one source of
    # truth for the approved database environment names.
    assert set(DATABASE_RUNTIME_ENVIRONMENT_FIELDS) == set(DATABASE_ENVIRONMENT_NAMES)


# --- brief Step 1: reject DATABASE_URL, plaintext password, unknown keys ---


def test_database_loader_rejects_database_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_database_runtime_settings(
            environ={**_valid_environ(tmp_path), "DATABASE_URL": "postgresql://u:p@h/db"}
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_database_loader_rejects_plaintext_password_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_database_runtime_settings(
            environ={
                **_valid_environ(tmp_path),
                "KNOWLEDGE_DATABASE_PASSWORD": "do-not-emit-password",
            }
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_database_loader_rejects_typo_of_registered_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_database_runtime_settings(
            environ={**_valid_environ(tmp_path), "KNOWLEDGE_DATABASE_HOSTNAME": "db.internal"}
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_database_loader_rejects_unregistered_knowledge_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_database_runtime_settings(
            environ={**_valid_environ(tmp_path), "KNOWLEDGE_TOTALLY_UNKNOWN_FLAG": "noise"}
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_unknown_key_error_never_renders_value(tmp_path: Path) -> None:
    sentinel_value = "DO_NOT_LEAK_ENV_VALUE"
    with pytest.raises(ConfigurationError) as raised:
        load_database_runtime_settings(
            environ={
                **_valid_environ(tmp_path),
                "KNOWLEDGE_DATABASE_PASSWORD": sentinel_value,
            }
        )
    error = raised.value
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert sentinel_value not in rendered


# --- happy path: settings snapshot plus secret-file password ---------------


def test_valid_config_loads_settings_with_defaults(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()

    settings = load_database_runtime_settings(environ=_valid_environ(secret_root))

    assert isinstance(settings, DatabaseRuntimeSettings)
    assert settings.environment is RuntimeEnvironment.LOCAL
    assert settings.secret_root == secret_root
    assert settings.host == "127.0.0.1"
    assert settings.port == 5432
    assert settings.database_name == "knowledge"
    assert settings.database_user == "knowledge_app"
    assert settings.password_file_name == _PASSWORD_FILE
    assert settings.ssl_mode.value == "disable"


def test_valid_config_reads_password_from_secret_file_only(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _write_password_file(secret_root, "secret-password-value\n")

    settings = load_database_runtime_settings(environ=_valid_environ(secret_root))
    password = read_database_runtime_password(settings)

    assert isinstance(password, SecretStr)
    # Trailing newline is stripped by the shared secret-file contract.
    assert password.get_secret_value() == "secret-password-value"


def test_missing_password_file_raises_secret_file_error(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    settings = load_database_runtime_settings(environ=_valid_environ(secret_root))
    with pytest.raises(SecretFileError) as raised:
        read_database_runtime_password(settings)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_MISSING


# --- connection-field grammar ----------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("KNOWLEDGE_DATABASE_HOST", "db .internal"),
        ("KNOWLEDGE_DATABASE_NAME", "spaced name"),
        ("KNOWLEDGE_DATABASE_USER", "user\x00name"),
        ("KNOWLEDGE_DATABASE_PORT", "0"),
        ("KNOWLEDGE_DATABASE_PORT", "not-a-port"),
        ("KNOWLEDGE_DATABASE_PASSWORD_FILE", "nested/path"),
        ("KNOWLEDGE_DATABASE_SSL_MODE", "require"),
    ],
)
def test_invalid_connection_field_is_rejected(tmp_path: Path, field: str, value: str) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    with pytest.raises(ConfigurationError) as raised:
        load_database_runtime_settings(environ=_valid_environ(secret_root, **{field: value}))
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_configuration_invalid_error_never_renders_value_or_path(tmp_path: Path) -> None:
    sentinel = "do-not-emit host"
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    with pytest.raises(ConfigurationError) as raised:
        load_database_runtime_settings(
            environ=_valid_environ(secret_root, **{"KNOWLEDGE_DATABASE_HOST": sentinel})
        )
    error = raised.value
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert sentinel not in rendered
    assert str(secret_root) not in rendered


def test_ssl_environment_pairing_is_enforced(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    with pytest.raises(ConfigurationError):
        load_database_runtime_settings(
            environ=_valid_environ(
                secret_root,
                KNOWLEDGE_ENVIRONMENT="production",
                KNOWLEDGE_DATABASE_SSL_MODE="disable",
            )
        )


# --- brief Step 1: pinned runtime bounds -----------------------------------


def test_pool_and_timeout_bounds_are_pinned() -> None:
    assert POOL_SIZE == 4
    assert MAX_POOL_OVERFLOW == 4
    assert POOL_TIMEOUT_SECONDS == 5
    assert CONNECT_TIMEOUT_SECONDS == 5
    assert LOCK_TIMEOUT_SECONDS == 5
    assert STATEMENT_TIMEOUT_SECONDS == 15
    assert IDLE_IN_TRANSACTION_SESSION_TIMEOUT_SECONDS == 30


def test_transaction_bound_statements_derive_from_the_pinned_timeouts() -> None:
    """The engine owns no timeout literals of its own.

    ``TRANSACTION_BOUND_STATEMENTS`` must be computed from exactly the pinned
    settings constants imported above, so no second, drifting copy of the
    5/15/30-second bounds can exist.
    """

    assert (
        f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_SECONDS * 1000}ms'",
        f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_SECONDS * 1000}ms'",
        (
            "SET LOCAL idle_in_transaction_session_timeout = "
            f"'{IDLE_IN_TRANSACTION_SESSION_TIMEOUT_SECONDS * 1000}ms'"
        ),
    ) == TRANSACTION_BOUND_STATEMENTS


# --- cross-fragment composition and frozen snapshot ------------------------


def test_database_loader_ignores_registered_runtime_and_object_storage_keys(
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    environ = _valid_environ(
        secret_root,
        KNOWLEDGE_LOG_LEVEL="warning",
        KNOWLEDGE_R2_BUCKET_NAME="knowledge-test",
        KNOWLEDGE_TEMPORAL_TARGET="temporal.internal:7233",
    )
    settings = load_database_runtime_settings(environ=environ)
    assert settings.database_name == "knowledge"


def test_database_runtime_settings_is_frozen_and_redacted(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _write_password_file(secret_root, "LEAK-ME-998877")
    settings = load_database_runtime_settings(environ=_valid_environ(secret_root))

    with pytest.raises(Exception, match="frozen"):
        settings.database_name = "other"  # type: ignore[misc]
    for rendered in (repr(settings), str(settings)):
        assert "LEAK-ME" not in rendered
        assert "redacted" in rendered


def test_ambient_postgres_variables_have_no_effect(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _write_password_file(secret_root, "file-password-value")
    environ = _valid_environ(
        secret_root,
        PGHOST="ambient-host-do-not-use",
        PGPASSWORD="ambient-password-do-not-use",
    )
    settings = load_database_runtime_settings(environ=environ)
    assert settings.host == "127.0.0.1"
    assert read_database_runtime_password(settings).get_secret_value() == "file-password-value"
