"""Health data values and the canonical database readiness probe protocol."""

from __future__ import annotations

import warnings
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LivenessData(BaseModel):
    """Process-liveness success data; it implies no I/O by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["live"] = "live"
    service: Literal["api"] = "api"


with warnings.catch_warnings():
    # ``schema`` is the contractual readiness payload key, and pydantic's
    # BaseModel still carries the deprecated v1 ``schema`` method; silence only
    # this pinned field's shadow warning so no other warning is hidden.
    warnings.filterwarnings(
        "ignore",
        message='Field name "schema" in "ReadinessChecks" shadows an attribute',
        category=UserWarning,
    )

    class ReadinessChecks(BaseModel):
        """Canonical dependency check outcomes; this child tracks PostgreSQL only."""

        model_config = ConfigDict(frozen=True, extra="forbid")
        postgresql: Literal["ready"] = "ready"
        # "schema" is the contractual readiness payload key; it shadows the
        # deprecated v1 ``BaseModel.schema`` method, hence the targeted ignore.
        schema: Literal["ready"] = "ready"  # type: ignore[assignment]


class ReadinessData(BaseModel):
    """Readiness success data; failed readiness uses an error envelope instead."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ready"] = "ready"
    checks: ReadinessChecks = Field(default_factory=ReadinessChecks)


@runtime_checkable
class CanonicalDatabaseReadinessProbe(Protocol):
    """Connectivity plus exact-schema-head probe owned by the PostgreSQL adapter."""

    async def check(self) -> None: ...
