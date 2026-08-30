"""Frozen R2 object-storage settings loaded from secret files under a secret root.

These tests prove the provider settings composition: the closed environment-name
map, the endpoint/bucket/spool-root/credential-filename grammar, the secret-file
load into a short-lived frozen credentials value, the rejection of plaintext
secret environment values and ambient AWS variables, and that no error renders a
value or path. They live in the provider test tree and import the provider
settings module plus the shared core error/settings helpers.
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from migrations.database_migration_runtime import load_database_migration_settings
from pydantic import SecretStr

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError, SecretFileError
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import RuntimeEnvironment, ServiceName
from r2_object_storage.settings import (
    OBJECT_STORAGE_ENVIRONMENT_FIELDS,
    LoadedR2Credentials,
    ObjectStorageSettings,
    load_object_storage_settings,
)

# A real-shaped R2 account id is 32 lowercase hexadecimal characters.
_VALID_ACCOUNT_ID = "abcdef0123456789abcdef0123456789"
_VALID_ENDPOINT = f"https://{_VALID_ACCOUNT_ID}.r2.cloudflarestorage.com"
_VALID_BUCKET = "knowledge-test"
_ACCESS_KEY_ID_FILE = "r2_access_key_id"
_SECRET_ACCESS_KEY_FILE = "r2_secret_access_key"


def _write_secret(secret_root: Path, name: str, value: str) -> None:
    (secret_root / name).write_text(value, encoding="utf-8")


def _valid_environ(
    secret_root: Path,
    spool_root: Path,
    *,
    endpoint: str = _VALID_ENDPOINT,
    bucket: str = _VALID_BUCKET,
    access_key_id_file: str = _ACCESS_KEY_ID_FILE,
    secret_access_key_file: str = _SECRET_ACCESS_KEY_FILE,
    environment: str | None = None,
) -> dict[str, str]:
    environ: dict[str, str] = {
        "KNOWLEDGE_SECRET_ROOT": str(secret_root),
        "KNOWLEDGE_R2_ENDPOINT": endpoint,
        "KNOWLEDGE_R2_BUCKET_NAME": bucket,
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE": access_key_id_file,
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE": secret_access_key_file,
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT": str(spool_root),
    }
    if environment is not None:
        environ["KNOWLEDGE_ENVIRONMENT"] = environment
    return environ


# --- closed environment-name map -------------------------------------------


def test_object_storage_environment_field_map_is_closed_and_exact() -> None:
    assert set(OBJECT_STORAGE_ENVIRONMENT_FIELDS) == {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_R2_ENDPOINT",
        "KNOWLEDGE_R2_BUCKET_NAME",
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE",
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE",
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT",
    }
    assert OBJECT_STORAGE_ENVIRONMENT_FIELDS["KNOWLEDGE_ENVIRONMENT"] == "environment"
    assert OBJECT_STORAGE_ENVIRONMENT_FIELDS["KNOWLEDGE_SECRET_ROOT"] == "secret_root"
    assert OBJECT_STORAGE_ENVIRONMENT_FIELDS["KNOWLEDGE_R2_ENDPOINT"] == "r2_endpoint"
    assert OBJECT_STORAGE_ENVIRONMENT_FIELDS["KNOWLEDGE_R2_BUCKET_NAME"] == "r2_bucket_name"
    assert (
        OBJECT_STORAGE_ENVIRONMENT_FIELDS["KNOWLEDGE_R2_ACCESS_KEY_ID_FILE"]
        == "r2_access_key_id_file"
    )
    assert (
        OBJECT_STORAGE_ENVIRONMENT_FIELDS["KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE"]
        == "r2_secret_access_key_file"
    )
    assert (
        OBJECT_STORAGE_ENVIRONMENT_FIELDS["KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT"]
        == "object_storage_spool_root"
    )


# --- brief Step 1: unknown-key rejection of a plaintext secret -------------


def test_r2_loader_rejects_plaintext_secret_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_object_storage_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_R2_SECRET_ACCESS_KEY": "do-not-emit-secret",
            }
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_r2_loader_rejects_typo_of_registered_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_object_storage_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_R2_BUKCET_NAME": "knowledge-test",
            }
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_r2_loader_rejects_unregistered_knowledge_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_object_storage_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_TOTALLY_UNKNOWN_FLAG": "noise",
            }
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


# --- happy path: settings + credentials read from secret files -------------


def test_valid_config_loads_settings_and_reads_secrets_from_files(
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "AKIAEXAMPLEKEYID")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "secret-access-value-12345")

    settings, credentials = load_object_storage_settings(
        environ=_valid_environ(secret_root, spool_root)
    )

    assert isinstance(settings, ObjectStorageSettings)
    assert settings.environment is RuntimeEnvironment.LOCAL
    assert settings.secret_root == secret_root
    assert settings.r2_endpoint == _VALID_ENDPOINT
    assert settings.r2_bucket_name == _VALID_BUCKET
    assert settings.r2_access_key_id_file == _ACCESS_KEY_ID_FILE
    assert settings.r2_secret_access_key_file == _SECRET_ACCESS_KEY_FILE
    assert settings.object_storage_spool_root == spool_root

    assert isinstance(credentials, LoadedR2Credentials)
    assert isinstance(credentials.access_key_id, SecretStr)
    assert isinstance(credentials.secret_access_key, SecretStr)
    assert credentials.access_key_id.get_secret_value() == "AKIAEXAMPLEKEYID"
    assert credentials.secret_access_key.get_secret_value() == "secret-access-value-12345"


# --- endpoint grammar ------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://abcdef0123456789abcdef0123456789.r2.cloudflarestorage.com",
        "https://ABCDEF0123456789ABCDEF0123456789.r2.cloudflarestorage.com",
        "https://abcdef0123456789abcdef012345678.r2.cloudflarestorage.com",  # 31 hex
        "https://gabcdef0123456789abcdef0123456789.r2.cloudflarestorage.com",  # non-hex
        "https://abcdef0123456789abcdef0123456789.r2.cloudflarestorage.com:8443",
        "https://abcdef0123456789abcdef0123456789.r2.cloudflarestorage.com/path",
        "https://abcdef0123456789abcdef0123456789.r2.cloudflarestorage.com?q=1",
        "https://user:pass@abcdef0123456789abcdef0123456789.r2.cloudflarestorage.com",
        "https://abcdef0123456789abcdef0123456789.s3.amazonaws.com",
        "https://abcdef0123456789abcdef0123456789.r2.cloudflarestorage.com/",
        "not-a-url",
    ],
)
def test_invalid_endpoint_is_rejected(tmp_path: Path, endpoint: str) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    with pytest.raises(ObjectStorageError) as raised:
        load_object_storage_settings(
            environ=_valid_environ(secret_root, spool_root, endpoint=endpoint)
        )
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID


def test_valid_endpoint_at_32_lowercase_hex_boundary(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "AKIAEXAMPLEKEYID")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "secret-access-value")
    account = "0" * 32
    settings, _credentials = load_object_storage_settings(
        environ=_valid_environ(
            secret_root, spool_root, endpoint=f"https://{account}.r2.cloudflarestorage.com"
        )
    )
    assert settings.r2_endpoint == f"https://{account}.r2.cloudflarestorage.com"


# --- bucket grammar --------------------------------------------------------


@pytest.mark.parametrize(
    "bucket",
    [
        "ab",  # too short (2)
        "a" * 64,  # too long (64)
        "Knowledge-Test",  # uppercase
        "-knowledge-test",  # leading hyphen
        "knowledge-test-",  # trailing hyphen
        "knowledge_test",  # underscore
        "knowledge.test",  # dot
    ],
)
def test_invalid_bucket_name_is_rejected(tmp_path: Path, bucket: str) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    with pytest.raises(ObjectStorageError) as raised:
        load_object_storage_settings(environ=_valid_environ(secret_root, spool_root, bucket=bucket))
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID


@pytest.mark.parametrize("bucket", ["abc", "a" * 63, "knowledge-test", "knowledge-2026"])
def test_valid_bucket_name_boundaries(tmp_path: Path, bucket: str) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "AKIAEXAMPLEKEYID")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "secret-access-value")
    settings, _credentials = load_object_storage_settings(
        environ=_valid_environ(secret_root, spool_root, bucket=bucket)
    )
    assert settings.r2_bucket_name == bucket


# --- credential filename grammar -------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["with/slash", "with\\backslash", ".", "..", "/absolute", "name\x00", ""],
)
def test_invalid_credential_filename_is_rejected(tmp_path: Path, filename: str) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    with pytest.raises(ObjectStorageError) as raised:
        load_object_storage_settings(
            environ=_valid_environ(secret_root, spool_root, access_key_id_file=filename)
        )
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID


# --- per-platform secret-root default contract -----------------------------


def test_secret_root_documented_default_is_the_linux_serve_path() -> None:
    """The ``secret_root`` default is the documented Linux serve contract.

    The POSIX branch is pinned structurally (the field default itself), not by
    running on POSIX: on a POSIX host the default is absolute and the loader
    proceeds beneath it.
    """

    assert ObjectStorageSettings.model_fields["secret_root"].default == Path("/run/secrets")


@pytest.mark.skipif(sys.platform != "win32", reason="win32 is the override-required branch")
def test_win32_loader_requires_secret_root_override(tmp_path: Path) -> None:
    """On win32 the Linux default is not absolute, so the loader fails closed.

    Windows hosts always set ``KNOWLEDGE_SECRET_ROOT``; omitting it leaves the
    documented Linux default, which the absolute-path validator rejects.
    """

    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    environ = _valid_environ(secret_root, spool_root)
    del environ["KNOWLEDGE_SECRET_ROOT"]

    with pytest.raises(ObjectStorageError) as raised:
        load_object_storage_settings(environ=environ)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID


# --- spool root grammar ----------------------------------------------------


def test_relative_spool_root_is_rejected(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    with pytest.raises(ObjectStorageError) as raised:
        load_object_storage_settings(
            environ=_valid_environ(secret_root, spool_root=Path("relative/spool"))
        )
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID


def test_nonexistent_spool_root_is_rejected(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    missing_spool = tmp_path / "does-not-exist"
    with pytest.raises(ObjectStorageError) as raised:
        load_object_storage_settings(environ=_valid_environ(secret_root, spool_root=missing_spool))
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID


# --- secret-file contract --------------------------------------------------


def test_missing_access_key_file_raises_secret_file_error(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    # Only the secret access key file exists; the access key id file is missing.
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "secret-access-value")
    with pytest.raises(SecretFileError) as raised:
        load_object_storage_settings(environ=_valid_environ(secret_root, spool_root))
    assert raised.value.error_code is ErrorCode.SECRET_FILE_MISSING


def test_missing_secret_access_key_file_raises_secret_file_error(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "AKIAEXAMPLEKEYID")
    with pytest.raises(SecretFileError) as raised:
        load_object_storage_settings(environ=_valid_environ(secret_root, spool_root))
    assert raised.value.error_code is ErrorCode.SECRET_FILE_MISSING


# --- frozen models ---------------------------------------------------------


def test_object_storage_settings_is_frozen(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "AKIAEXAMPLEKEYID")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "secret-access-value")
    settings, credentials = load_object_storage_settings(
        environ=_valid_environ(secret_root, spool_root)
    )
    # Pydantic frozen models reject assignment with a "frozen" validation error.
    with pytest.raises(Exception, match="frozen"):
        settings.r2_bucket_name = "other-bucket"  # type: ignore[misc]
    # The frozen credentials dataclass rejects assignment with FrozenInstanceError.
    with pytest.raises(FrozenInstanceError):
        credentials.access_key_id = SecretStr("other")  # type: ignore[misc]


# --- ambient AWS variables and .env have no effect -------------------------


def test_ambient_aws_variables_have_no_effect(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "AKIAEXAMPLEKEYID")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "secret-access-value")
    environ = _valid_environ(secret_root, spool_root)
    environ["AWS_ACCESS_KEY_ID"] = "ambient-aws-key-do-not-use"
    environ["AWS_SECRET_ACCESS_KEY"] = "ambient-aws-secret-do-not-use"
    environ["AWS_ENDPOINT_URL_S3"] = "https://ambient-endpoint.example"

    settings, credentials = load_object_storage_settings(environ=environ)

    assert settings.r2_endpoint == _VALID_ENDPOINT
    # The ambient AWS values must never reach the loaded credentials.
    assert credentials.access_key_id.get_secret_value() == "AKIAEXAMPLEKEYID"
    assert credentials.secret_access_key.get_secret_value() == "secret-access-value"


def test_dotenv_file_has_no_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv_dir = tmp_path / "with-dotenv"
    dotenv_dir.mkdir()
    (dotenv_dir / ".env").write_text("KNOWLEDGE_R2_BUCKET_NAME=dot-env-bucket\n", encoding="utf-8")
    monkeypatch.chdir(dotenv_dir)

    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "AKIAEXAMPLEKEYID")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "secret-access-value")

    settings, _credentials = load_object_storage_settings(
        environ=_valid_environ(secret_root, spool_root)
    )
    assert settings.r2_bucket_name == _VALID_BUCKET


# --- production/test snapshots ---------------------------------------------


def test_production_snapshot_loads(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "prod-key-id")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "prod-secret")
    settings, credentials = load_object_storage_settings(
        environ=_valid_environ(
            secret_root, spool_root, bucket="knowledge-production", environment="production"
        )
    )
    assert settings.environment is RuntimeEnvironment.PRODUCTION
    assert settings.r2_bucket_name == "knowledge-production"
    assert credentials.access_key_id.get_secret_value() == "prod-key-id"
    assert credentials.secret_access_key.get_secret_value() == "prod-secret"


def test_test_snapshot_loads(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "test-key-id")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "test-secret")
    settings, credentials = load_object_storage_settings(
        environ=_valid_environ(secret_root, spool_root, bucket="knowledge-test", environment="test")
    )
    assert settings.environment is RuntimeEnvironment.TEST
    assert credentials.access_key_id.get_secret_value() == "test-key-id"


def test_production_and_test_use_separate_buckets_and_credentials(
    tmp_path: Path,
) -> None:
    """Production and live-test use separate private buckets and secret files."""
    prod_secrets = tmp_path / "prod-secrets"
    test_secrets = tmp_path / "test-secrets"
    spool_root = tmp_path / "spool"
    for path in (prod_secrets, test_secrets, spool_root):
        path.mkdir()
    _write_secret(prod_secrets, _ACCESS_KEY_ID_FILE, "prod-key-id")
    _write_secret(prod_secrets, _SECRET_ACCESS_KEY_FILE, "prod-secret")
    _write_secret(test_secrets, _ACCESS_KEY_ID_FILE, "test-key-id")
    _write_secret(test_secrets, _SECRET_ACCESS_KEY_FILE, "test-secret")

    prod_settings, prod_credentials = load_object_storage_settings(
        environ=_valid_environ(
            prod_secrets, spool_root, bucket="knowledge-production", environment="production"
        )
    )
    test_settings, test_credentials = load_object_storage_settings(
        environ=_valid_environ(
            test_secrets, spool_root, bucket="knowledge-test", environment="test"
        )
    )

    assert prod_settings.r2_bucket_name != test_settings.r2_bucket_name
    assert (
        prod_credentials.access_key_id.get_secret_value()
        != test_credentials.access_key_id.get_secret_value()
    )
    assert (
        prod_credentials.secret_access_key.get_secret_value()
        != test_credentials.secret_access_key.get_secret_value()
    )


# --- errors never render a value or path -----------------------------------


def test_configuration_invalid_error_never_renders_value_or_path(
    tmp_path: Path,
) -> None:
    sentinel_endpoint = "https://do-not-emit-r2-endpoint.example/path"
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    with pytest.raises(ObjectStorageError) as raised:
        load_object_storage_settings(
            environ=_valid_environ(secret_root, spool_root, endpoint=sentinel_endpoint)
        )
    error = raised.value
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert sentinel_endpoint not in rendered
    assert str(secret_root) not in rendered
    assert str(spool_root) not in rendered


def test_unknown_key_error_never_renders_value(tmp_path: Path) -> None:
    sentinel_value = "DO_NOT_LEAK_ENV_VALUE"
    with pytest.raises(ConfigurationError) as raised:
        load_object_storage_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_R2_SECRET_ACCESS_KEY": sentinel_value,
            }
        )
    error = raised.value
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert sentinel_value not in rendered


def test_settings_and_credentials_repr_never_leak_secrets(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    access_value = "AKIA-LEAK-ME-1234567890"
    secret_value = "secret-LEAK-ME-9876543210"
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, access_value)
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, secret_value)

    settings, credentials = load_object_storage_settings(
        environ=_valid_environ(secret_root, spool_root)
    )

    for rendered in (repr(settings), str(settings), repr(credentials), str(credentials)):
        assert access_value not in rendered
        assert secret_value not in rendered


# --- cross-fragment composition: object-storage ignores runtime/db keys ----


def test_r2_loader_ignores_registered_runtime_and_database_keys(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    spool_root = tmp_path / "spool"
    secret_root.mkdir()
    spool_root.mkdir()
    _write_secret(secret_root, _ACCESS_KEY_ID_FILE, "AKIAEXAMPLEKEYID")
    _write_secret(secret_root, _SECRET_ACCESS_KEY_FILE, "secret-access-value")
    environ = _valid_environ(secret_root, spool_root)
    environ["KNOWLEDGE_LOG_LEVEL"] = "warning"
    environ["KNOWLEDGE_DATABASE_HOST"] = "db.internal.example"

    settings, _credentials = load_object_storage_settings(environ=environ)

    assert settings.r2_bucket_name == _VALID_BUCKET


# --- shared registry proven end-to-end across the three fragments ---------


def test_all_three_loaders_share_the_union_registry(tmp_path: Path) -> None:
    """A registered R2 key present in a combined snapshot is ignored by the
    runtime and database loaders while the object-storage loader still rejects a
    plaintext secret name. This pins the repository-wide registry contract."""
    runtime_settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_R2_BUCKET_NAME": "knowledge-test",
        },
    )
    assert runtime_settings.secret_root == tmp_path

    database_settings = load_database_migration_settings(
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_R2_BUCKET_NAME": "knowledge-test",
        }
    )
    assert database_settings.secret_root == tmp_path
