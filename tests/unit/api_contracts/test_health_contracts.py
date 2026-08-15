"""Health data model and readiness probe protocol contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_os.api_contracts import (
    CanonicalDatabaseReadinessProbe,
    LivenessData,
    ReadinessChecks,
    ReadinessData,
)


def test_liveness_data_has_exact_live_api_values() -> None:
    assert LivenessData().model_dump(mode="json") == {"status": "live", "service": "api"}
    with pytest.raises(ValidationError):
        LivenessData(status="dead")
    with pytest.raises(ValidationError):
        LivenessData(service="worker")


def test_readiness_data_defaults_to_ready_checks() -> None:
    assert ReadinessData().model_dump(mode="json") == {
        "status": "ready",
        "checks": {"postgresql": "ready", "schema": "ready"},
    }
    assert ReadinessData().checks == ReadinessChecks()


def test_readiness_models_reject_unknown_state() -> None:
    with pytest.raises(ValidationError):
        ReadinessChecks(postgresql="unavailable")
    with pytest.raises(ValidationError):
        ReadinessData(status="degraded")
    with pytest.raises(ValidationError):
        ReadinessChecks(postgresql="ready", schema="ready", extra_check="value")  # type: ignore[call-arg]


def test_readiness_probe_protocol_matches_async_check_implementations() -> None:
    class ReachableProbe:
        async def check(self) -> None: ...

    class UnrelatedType:
        async def verify(self) -> None: ...

    assert isinstance(ReachableProbe(), CanonicalDatabaseReadinessProbe)
    assert not isinstance(UnrelatedType(), CanonicalDatabaseReadinessProbe)
