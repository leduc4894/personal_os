from __future__ import annotations

import os
from pathlib import Path

import pytest
from migrations.database_migration_runtime import (
    DATABASE_ENVIRONMENT_FIELDS,
    DatabaseMigrationSettings,
    DatabaseSslMode,
    build_database_connect_arguments,
    build_database_url,
    load_database_migration_settings,
    read_database_password,
)
from pydantic import SecretStr
from sqlalchemy.engine import URL

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    DatabaseMigrationError,
    SecretFileError,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment

PASSWORD_FILE_NAME = "postgres_application_password"


def _write_password_file(secret_root: Path, *, value: str = "hunter2") -> None:
    (secret_root / PASSWORD_FILE_NAME).write_text(value, encoding="utf-8")


def test_local_defaults_match_the_canonical_baseline(tmp_path: Path) -> None:
    settings = load_database_migration_settings(environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)})

    assert settings.environment is RuntimeEnvironment.LOCAL
    assert settings.secret_root == tmp_path
    assert settings.host == "127.0.0.1"
    assert settings.port == 5432
    assert settings.database_name == "knowledge"
    assert settings.database_user == "knowledge_app"
    assert settings.password_file_name == PASSWORD_FILE_NAME
    assert settings.ssl_mode is DatabaseSslMode.DISABLE
    # The ``/run/secrets`` default is the POSIX/container baseline. Assert it via
    # field metadata so the assertion stays stable on Windows, where the loaded
    # value is always an absolute operator-supplied path.
    assert DatabaseMigrationSettings.model_fields["secret_root"].default == Path("/run/secrets")


def test_database_environment_field_map_is_closed_and_exact() -> None:
    assert set(DATABASE_ENVIRONMENT_FIELDS) == {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_DATABASE_HOST",
        "KNOWLEDGE_DATABASE_PORT",
        "KNOWLEDGE_DATABASE_NAME",
        "KNOWLEDGE_DATABASE_USER",
        "KNOWLEDGE_DATABASE_PASSWORD_FILE",
        "KNOWLEDGE_DATABASE_SSL_MODE",
    }
    assert DATABASE_ENVIRONMENT_FIELDS["KNOWLEDGE_ENVIRONMENT"] == "environment"
    assert DATABASE_ENVIRONMENT_FIELDS["KNOWLEDGE_SECRET_ROOT"] == "secret_root"
    assert DATABASE_ENVIRONMENT_FIELDS["KNOWLEDGE_DATABASE_HOST"] == "host"
    assert DATABASE_ENVIRONMENT_FIELDS["KNOWLEDGE_DATABASE_PORT"] == "port"
    assert DATABASE_ENVIRONMENT_FIELDS["KNOWLEDGE_DATABASE_NAME"] == "database_name"
    assert DATABASE_ENVIRONMENT_FIELDS["KNOWLEDGE_DATABASE_USER"] == "database_user"
    assert DATABASE_ENVIRONMENT_FIELDS["KNOWLEDGE_DATABASE_PASSWORD_FILE"] == "password_file_name"
    assert DATABASE_ENVIRONMENT_FIELDS["KNOWLEDGE_DATABASE_SSL_MODE"] == "ssl_mode"


def test_all_eight_canonical_fields_load_into_the_model(tmp_path: Path) -> None:
    settings = load_database_migration_settings(
        environ={
            "KNOWLEDGE_ENVIRONMENT": "staging",
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_DATABASE_HOST": "db.internal.example",
            "KNOWLEDGE_DATABASE_PORT": "6543",
            "KNOWLEDGE_DATABASE_NAME": "knowledge",
            "KNOWLEDGE_DATABASE_USER": "knowledge_app",
            "KNOWLEDGE_DATABASE_PASSWORD_FILE": PASSWORD_FILE_NAME,
            "KNOWLEDGE_DATABASE_SSL_MODE": "verify-full",
        }
    )
    assert settings.environment is RuntimeEnvironment.STAGING
    assert settings.host == "db.internal.example"
    assert settings.port == 6543
    assert settings.database_name == "knowledge"
    assert settings.database_user == "knowledge_app"
    assert settings.password_file_name == PASSWORD_FILE_NAME
    assert settings.ssl_mode is DatabaseSslMode.VERIFY_FULL
    assert settings.secret_root == tmp_path


def test_unknown_knowledge_key_maps_to_configuration_invalid_without_echo(
    tmp_path: Path,
) -> None:
    sentinel_key = "KNOWLEDGE_TOTALLY_UNKNOWN_FLAG"
    sentinel_value = "LEAK_VALUE_424242"
    with pytest.raises(DatabaseMigrationError) as exc_info:
        load_database_migration_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                sentinel_key: sentinel_value,
            }
        )
    error = exc_info.value
    assert error.error_code is ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert sentinel_key not in rendered
    assert sentinel_value not in rendered


def test_port_must_be_inside_the_postgres_bind_range(tmp_path: Path) -> None:
    for bad_port in ("0", "65536", "-1", "not-a-port"):
        with pytest.raises(DatabaseMigrationError) as exc_info:
            load_database_migration_settings(
                environ={
                    "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                    "KNOWLEDGE_DATABASE_PORT": bad_port,
                }
            )
        assert exc_info.value.error_code is ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID


def test_port_accepts_the_postgres_bind_range_bounds(tmp_path: Path) -> None:
    for good_port in ("1", "5432", "65535"):
        settings = load_database_migration_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_DATABASE_PORT": good_port,
            }
        )
        assert settings.port == int(good_port)


def test_connection_fields_are_trimmed(tmp_path: Path) -> None:
    settings = load_database_migration_settings(
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_DATABASE_HOST": "  db.internal.example  ",
            "KNOWLEDGE_DATABASE_NAME": "  knowledge  ",
            "KNOWLEDGE_DATABASE_USER": "  knowledge_app  ",
        }
    )
    assert settings.host == "db.internal.example"
    assert settings.database_name == "knowledge"
    assert settings.database_user == "knowledge_app"


def test_connection_fields_reject_blank_or_unsafe_values(tmp_path: Path) -> None:
    invalid_cases = [
        ("KNOWLEDGE_DATABASE_HOST", "   "),
        ("KNOWLEDGE_DATABASE_HOST", "host\nwith\nnewline"),
        ("KNOWLEDGE_DATABASE_HOST", "host\twith\ttab"),
        ("KNOWLEDGE_DATABASE_NAME", ""),
        ("KNOWLEDGE_DATABASE_USER", "   "),
    ]
    for env_name, bad_value in invalid_cases:
        with pytest.raises(DatabaseMigrationError) as exc_info:
            load_database_migration_settings(
                environ={
                    "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                    env_name: bad_value,
                }
            )
        assert exc_info.value.error_code is ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID


def test_password_file_name_must_be_a_single_relative_component(tmp_path: Path) -> None:
    invalid_names = [
        "with/slash",
        "with\\backslash",
        ".",
        "..",
        "/absolute",
        "name\x00",
        "",
    ]
    for bad_name in invalid_names:
        with pytest.raises(DatabaseMigrationError) as exc_info:
            load_database_migration_settings(
                environ={
                    "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                    "KNOWLEDGE_DATABASE_PASSWORD_FILE": bad_name,
                }
            )
        assert exc_info.value.error_code is ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID


def test_local_and_test_accept_only_disable(tmp_path: Path) -> None:
    for environment in ("local", "test"):
        accepted = load_database_migration_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_ENVIRONMENT": environment,
                "KNOWLEDGE_DATABASE_SSL_MODE": "disable",
            }
        )
        assert accepted.ssl_mode is DatabaseSslMode.DISABLE

        with pytest.raises(DatabaseMigrationError):
            load_database_migration_settings(
                environ={
                    "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                    "KNOWLEDGE_ENVIRONMENT": environment,
                    "KNOWLEDGE_DATABASE_SSL_MODE": "verify-full",
                }
            )


def test_staging_and_production_require_verify_full(tmp_path: Path) -> None:
    for environment in ("staging", "production"):
        with pytest.raises(DatabaseMigrationError):
            load_database_migration_settings(
                environ={
                    "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                    "KNOWLEDGE_ENVIRONMENT": environment,
                    "KNOWLEDGE_DATABASE_SSL_MODE": "disable",
                }
            )

        accepted = load_database_migration_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_ENVIRONMENT": environment,
                "KNOWLEDGE_DATABASE_SSL_MODE": "verify-full",
            }
        )
        assert accepted.ssl_mode is DatabaseSslMode.VERIFY_FULL


def test_unsupported_ssl_mode_token_is_rejected(tmp_path: Path) -> None:
    for bad_mode in ("allow", "prefer", "require", "bogus"):
        with pytest.raises(DatabaseMigrationError):
            load_database_migration_settings(
                environ={
                    "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                    "KNOWLEDGE_DATABASE_SSL_MODE": bad_mode,
                }
            )


def test_loader_defaults_to_process_environ_at_call_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in list(os.environ):
        if key.startswith("KNOWLEDGE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KNOWLEDGE_SECRET_ROOT", str(tmp_path))

    settings = load_database_migration_settings()

    assert settings.secret_root == tmp_path
    assert settings.host == "127.0.0.1"


def test_read_database_password_reads_bounded_secret_file(tmp_path: Path) -> None:
    _write_password_file(tmp_path, value="hunter2")
    settings = load_database_migration_settings(environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)})

    password = read_database_password(settings)

    assert isinstance(password, SecretStr)
    assert password.get_secret_value() == "hunter2"


def test_missing_password_file_preserves_secret_file_error(tmp_path: Path) -> None:
    settings = load_database_migration_settings(environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)})

    with pytest.raises(SecretFileError) as exc_info:
        read_database_password(settings)

    # read_database_password must NOT catch or wrap SecretFileError into a
    # DatabaseMigrationError; the existing secret-file contract is preserved.
    assert exc_info.value.error_code is ErrorCode.SECRET_FILE_MISSING
    assert not isinstance(exc_info.value, DatabaseMigrationError)


def test_build_database_url_keeps_password_without_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_password = "SENTINEL_PASSWORD_98765"
    _write_password_file(tmp_path, value=sentinel_password)
    settings = load_database_migration_settings(environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)})
    password = read_database_password(settings)

    def _forbid_cleartext_render(*args: object, **kwargs: object) -> str:
        # repr() masks via hide_password=True; only an unhidden render leaks.
        if kwargs.get("hide_password", False) is False:
            raise AssertionError("URL.render_as_string must not expose the password")
        return "REDACTED"

    monkeypatch.setattr(URL, "render_as_string", _forbid_cleartext_render)

    url = build_database_url(settings, password)

    assert url.drivername == "postgresql+psycopg"
    assert url.username == settings.database_user
    assert url.password == sentinel_password
    assert url.host == settings.host
    assert url.port == settings.port
    assert url.database == settings.database_name


def test_build_database_connect_arguments_are_exact_and_fixed(tmp_path: Path) -> None:
    settings = load_database_migration_settings(environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)})

    arguments = build_database_connect_arguments(settings)

    assert dict(arguments) == {
        "connect_timeout": 5,
        "sslmode": "disable",
        "application_name": "knowledge-migration",
        "options": (
            "-c timezone=UTC -c lock_timeout=5000 "
            "-c statement_timeout=60000 "
            "-c idle_in_transaction_session_timeout=60000"
        ),
    }

    staging = load_database_migration_settings(
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_ENVIRONMENT": "staging",
            "KNOWLEDGE_DATABASE_SSL_MODE": "verify-full",
        }
    )
    assert build_database_connect_arguments(staging)["sslmode"] == "verify-full"


def test_settings_repr_and_str_never_leak_sensitive_fields(tmp_path: Path) -> None:
    sentinel_host = "SENTINEL_HOST_NAME"
    sentinel_filename = "sentinel_password_file_name"
    settings = load_database_migration_settings(
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_DATABASE_HOST": sentinel_host,
            "KNOWLEDGE_DATABASE_PASSWORD_FILE": sentinel_filename,
        }
    )

    rendered = f"{settings!r} {settings!s}"

    assert sentinel_host not in rendered
    assert sentinel_filename not in rendered
    assert str(tmp_path) not in rendered


def test_mapped_validation_failure_never_leaks_input_values(tmp_path: Path) -> None:
    sentinel_value = "SENTINEL_BAD_PORT_VALUE_XYZ"
    with pytest.raises(DatabaseMigrationError) as exc_info:
        load_database_migration_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_DATABASE_PORT": sentinel_value,
            }
        )

    error = exc_info.value
    assert error.error_code is ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert sentinel_value not in rendered
