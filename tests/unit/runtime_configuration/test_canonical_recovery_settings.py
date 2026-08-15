"""Unit tests for the canonical recovery runtime settings fragment."""

from __future__ import annotations

from pathlib import Path

import pytest

import personal_os.runtime_configuration.loading as loading_module
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.environment_names import (
    CANONICAL_RECOVERY_ENVIRONMENT_NAMES,
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
)
from personal_os.runtime_configuration.loading import load_canonical_recovery_settings
from personal_os.runtime_configuration.models import (
    CanonicalRecoverySettings,
    RuntimeEnvironment,
)


def test_backup_root_joins_environment_name_registry() -> None:
    assert "KNOWLEDGE_CANONICAL_BACKUP_ROOT" in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    assert "KNOWLEDGE_CANONICAL_BACKUP_ROOT" in CANONICAL_RECOVERY_ENVIRONMENT_NAMES
    assert "KNOWLEDGE_ENVIRONMENT" in CANONICAL_RECOVERY_ENVIRONMENT_NAMES


def test_loader_owns_exactly_the_fragment_field_map() -> None:
    assert dict(loading_module.CANONICAL_RECOVERY_ENVIRONMENT_FIELDS) == {
        "KNOWLEDGE_ENVIRONMENT": "environment",
        "KNOWLEDGE_CANONICAL_BACKUP_ROOT": "backup_root",
    }


def test_loads_absolute_backup_root_with_local_environment(tmp_path: Path) -> None:
    backup_root = tmp_path / "canonical-backups"
    settings = load_canonical_recovery_settings(
        environ={
            "KNOWLEDGE_ENVIRONMENT": "local",
            "KNOWLEDGE_CANONICAL_BACKUP_ROOT": str(backup_root),
        }
    )
    assert settings.environment is RuntimeEnvironment.LOCAL
    assert settings.backup_root == backup_root


def test_backup_root_defaults_environment_to_local(tmp_path: Path) -> None:
    settings = load_canonical_recovery_settings(
        environ={"KNOWLEDGE_CANONICAL_BACKUP_ROOT": str(tmp_path)}
    )
    assert settings.environment is RuntimeEnvironment.LOCAL


def test_relative_backup_root_refused() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_canonical_recovery_settings(
            environ={"KNOWLEDGE_CANONICAL_BACKUP_ROOT": "relative/backups"}
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID
    details = raised.value.to_safe_dict()["safe_details"]
    assert details == {"count": 1, "field_names": ["backup_root"]}


def test_missing_backup_root_refused() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_canonical_recovery_settings(environ={"KNOWLEDGE_ENVIRONMENT": "local"})
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_unknown_knowledge_key_still_terminal(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_canonical_recovery_settings(
            environ={
                "KNOWLEDGE_CANONICAL_BACKUP_ROOT": str(tmp_path),
                "KNOWLEDGE_NOT_A_REGISTERED_NAME": "value",
            }
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_loader_ignores_registered_keys_of_other_fragments(tmp_path: Path) -> None:
    settings = load_canonical_recovery_settings(
        environ={
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_CANONICAL_BACKUP_ROOT": str(tmp_path),
            "KNOWLEDGE_DATABASE_HOST": "127.0.0.1",
            "KNOWLEDGE_R2_BUCKET_NAME": "knowledge-test",
            "UNRELATED_NOISE": "loud",
        }
    )
    assert settings.environment is RuntimeEnvironment.TEST
    assert settings.backup_root == tmp_path


def test_backup_root_excluded_from_repr_and_diagnostics(tmp_path: Path) -> None:
    settings = load_canonical_recovery_settings(
        environ={"KNOWLEDGE_CANONICAL_BACKUP_ROOT": str(tmp_path)}
    )
    assert repr(settings) == "CanonicalRecoverySettings(redacted)"
    assert str(settings) == "CanonicalRecoverySettings(redacted)"
    assert str(tmp_path) not in repr(settings)
    assert str(tmp_path) not in str(settings)


def test_canonical_recovery_settings_are_frozen(tmp_path: Path) -> None:
    settings = load_canonical_recovery_settings(
        environ={"KNOWLEDGE_CANONICAL_BACKUP_ROOT": str(tmp_path)}
    )
    with pytest.raises(Exception, match="frozen"):
        settings.backup_root = tmp_path / "other"

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRecoverySettings(
            environment=RuntimeEnvironment.LOCAL,
            backup_root=tmp_path,
            unregistered_field="forbidden",
        )
