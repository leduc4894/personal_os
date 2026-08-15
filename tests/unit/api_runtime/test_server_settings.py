"""Frozen API server settings loaded from the approved environment fragment.

These tests prove the API settings composition: loopback defaults apply only
to the local and test environments, staging and production refuse to start
without an explicit host and port (reported through registered safe field
names), and a port outside the bind range is rejected.
"""

from __future__ import annotations

import pytest
from api_runtime.server_settings import load_api_server_settings

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.models import RuntimeEnvironment


def test_local_api_settings_default_to_loopback() -> None:
    settings = load_api_server_settings(environ={"KNOWLEDGE_ENVIRONMENT": "local"})
    assert settings.environment is RuntimeEnvironment.LOCAL
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_remote_environment_requires_explicit_host_and_port(environment: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_api_server_settings(environ={"KNOWLEDGE_ENVIRONMENT": environment})
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID
    assert set(raised.value.safe_details["field_names"]) == {
        SafeToken.parse("host"),
        SafeToken.parse("port"),
    }


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_api_port_outside_bind_range_is_rejected(port: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_api_server_settings(
            environ={"KNOWLEDGE_ENVIRONMENT": "test", "KNOWLEDGE_API_PORT": port}
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID
