from __future__ import annotations

import ast
from pathlib import Path

import pytest

import personal_os.runtime_configuration.loading as loading_module
import personal_os.runtime_configuration.models as models_module
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import (
    ConfiguredLogLevel,
    RuntimeEnvironment,
    ServiceName,
)


def test_loads_exact_environment_overrides(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_LOG_LEVEL": "warning",
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
        },
    )
    assert settings.service_name is ServiceName.API
    assert settings.environment is RuntimeEnvironment.TEST
    assert settings.log_level is ConfiguredLogLevel.WARNING
    assert settings.secret_root == tmp_path


def test_rejects_unknown_prefixed_key_without_echoing_name(tmp_path: Path) -> None:
    unknown_name = "KNOWLEDGE_DO_NOT_LEAK_SECRET_VALUE"
    with pytest.raises(ConfigurationError) as raised:
        load_runtime_settings(
            ServiceName.API,
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                unknown_name: "DO_NOT_LEAK_ENV_VALUE",
            },
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY
    rendered = str(raised.value.to_safe_dict())
    assert unknown_name not in rendered
    assert "DO_NOT_LEAK_ENV_VALUE" not in rendered


def test_settings_are_frozen(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.WORKER,
        environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)},
    )
    with pytest.raises(Exception, match="frozen"):
        settings.log_level = ConfiguredLogLevel.DEBUG


def test_empty_environment_value_is_rejected_as_invalid(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_runtime_settings(
            ServiceName.API,
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_LOG_LEVEL": "",
            },
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID
    details = raised.value.to_safe_dict()["safe_details"]
    assert details == {"count": 1, "field_names": ["log_level"]}


def test_invalid_environment_enum_is_rejected_as_invalid(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_runtime_settings(
            ServiceName.API,
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_ENVIRONMENT": "space",
            },
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_relative_secret_root_is_rejected_as_invalid() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_runtime_settings(
            ServiceName.API,
            environ={"KNOWLEDGE_SECRET_ROOT": "relative/secrets"},
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


def test_lowercase_prefixed_key_has_no_effect(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "knowledge_log_level": "debug",
        },
    )
    assert settings.log_level is ConfiguredLogLevel.INFO


def test_unrelated_environment_variables_are_ignored(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "UNRELATED_NOISE": "loud",
        },
    )
    assert settings.environment is RuntimeEnvironment.LOCAL
    assert settings.log_level is ConfiguredLogLevel.INFO
    assert settings.secret_root == tmp_path


def test_dotenv_file_has_no_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv_dir = tmp_path / "with-dotenv"
    dotenv_dir.mkdir()
    (dotenv_dir / ".env").write_text("KNOWLEDGE_LOG_LEVEL=debug\n", encoding="utf-8")
    monkeypatch.chdir(dotenv_dir)

    settings = load_runtime_settings(
        ServiceName.API,
        environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)},
    )
    assert settings.log_level is ConfiguredLogLevel.INFO


def _module_scope_environment_accesses(source: str, filename: str) -> list[str]:
    """Return descriptions of any os.environ/os.getenv reads outside function bodies.

    Reads inside a function/method body execute only when called, so they do not
    count as import-time reads. Reads at module or class-body scope execute during
    import and are flagged. This is a source-level check; it deliberately ignores
    environment access performed by third-party frameworks (e.g. pydantic's plugin
    loader) that is outside the runtime_configuration modules.
    """

    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []

    def _access_name(node: ast.AST) -> str | None:
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            return "os.environ"
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr in {"getenv", "getenvb"}
            ):
                return f"os.{func.attr}"
        return None

    def _visit(node: ast.AST, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_is_function = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            access = _access_name(child)
            if access and not inside_function:
                offenders.append(f"{filename}: {access}")
            _visit(child, inside_function or child_is_function)

    _visit(tree, inside_function=False)
    return offenders


def test_modules_do_not_read_environment_at_module_scope() -> None:
    models_path = Path(models_module.__file__)
    loading_path = Path(loading_module.__file__)
    offenders = [
        *_module_scope_environment_accesses(
            models_path.read_text(encoding="utf-8"),
            str(models_path),
        ),
        *_module_scope_environment_accesses(
            loading_path.read_text(encoding="utf-8"),
            str(loading_path),
        ),
    ]
    assert offenders == [], (
        "runtime_configuration modules must not read the environment at module scope: "
        + ", ".join(offenders)
    )


def test_mutating_source_after_load_leaves_settings_unchanged(tmp_path: Path) -> None:
    source = {
        "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
        "KNOWLEDGE_LOG_LEVEL": "warning",
    }
    settings = load_runtime_settings(ServiceName.API, environ=source)
    assert settings.log_level is ConfiguredLogLevel.WARNING

    source["KNOWLEDGE_LOG_LEVEL"] = "debug"
    source["KNOWLEDGE_ENVIRONMENT"] = "production"
    source["BOGUS"] = "noise"

    assert settings.log_level is ConfiguredLogLevel.WARNING
    assert settings.environment is RuntimeEnvironment.LOCAL


def test_database_password_key_is_rejected_as_unknown() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_runtime_settings(
            ServiceName.API,
            environ={"KNOWLEDGE_DATABASE_PASSWORD": "hunter2"},
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_runtime_loader_ignores_registered_r2_keys(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_R2_BUCKET_NAME": "knowledge-test",
        },
    )
    assert settings.secret_root == tmp_path
    assert settings.environment is RuntimeEnvironment.LOCAL


def test_runtime_loader_ignores_registered_database_keys(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_DATABASE_HOST": "db.internal.example",
            "KNOWLEDGE_DATABASE_PASSWORD_FILE": "postgres_application_password",
            "KNOWLEDGE_DATABASE_SSL_MODE": "verify-full",
        },
    )
    assert settings.secret_root == tmp_path
    assert settings.environment is RuntimeEnvironment.LOCAL


def test_runtime_loader_still_rejects_typo_of_registered_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_runtime_settings(
            ServiceName.API,
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_R2_BUKCET_NAME": "knowledge-test",
            },
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY


def test_diagnostics_log_dir_is_loaded_from_the_registered_env_name(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_DIAGNOSTICS_LOG_DIR": str(tmp_path / "runtime-logs"),
        },
    )
    assert settings.diagnostics_log_dir == tmp_path / "runtime-logs"


def test_diagnostics_log_dir_defaults_to_disabled(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)},
    )
    assert settings.diagnostics_log_dir is None


@pytest.mark.parametrize("blank_value", ["", "   "])
def test_blank_diagnostics_log_dir_env_value_means_disabled(
    tmp_path: Path,
    blank_value: str,
) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_DIAGNOSTICS_LOG_DIR": blank_value,
        },
    )
    assert settings.diagnostics_log_dir is None
