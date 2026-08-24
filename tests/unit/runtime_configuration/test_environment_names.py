from __future__ import annotations

from personal_os.runtime_configuration.environment_names import (
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
    RUNTIME_ENVIRONMENT_NAMES,
    TEMPORAL_ENVIRONMENT_NAMES,
)


def test_temporal_environment_names_are_a_closed_registry() -> None:
    assert set(TEMPORAL_ENVIRONMENT_NAMES) == {
        "KNOWLEDGE_TEMPORAL_TARGET",
        "KNOWLEDGE_TEMPORAL_NAMESPACE",
        "KNOWLEDGE_TEMPORAL_TASK_QUEUE",
    }


def test_temporal_environment_names_join_the_repository_wide_registry() -> None:
    assert TEMPORAL_ENVIRONMENT_NAMES <= KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES


def test_api_environment_names_are_registered() -> None:
    assert {"KNOWLEDGE_API_HOST", "KNOWLEDGE_API_PORT"} <= (KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES)


def test_diagnostics_log_dir_name_is_registered_in_the_runtime_fragment() -> None:
    assert "KNOWLEDGE_DIAGNOSTICS_LOG_DIR" in RUNTIME_ENVIRONMENT_NAMES
    assert "KNOWLEDGE_DIAGNOSTICS_LOG_DIR" in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
