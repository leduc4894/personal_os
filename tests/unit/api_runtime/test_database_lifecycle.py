"""API process database lifecycle: lazy engine ownership and exactly-once disposal.

These tests prove the :class:`DatabaseRuntimeLifecycle` contract: the engine is
created only inside ``start`` (never at construction, and no connection is
opened there), ``check`` fails with the registered safe
``database_connection_unavailable`` error before ``start`` and delegates to the
internally constructed PostgreSQL readiness probe afterwards, and ``stop``
disposes exactly once while staying idempotent — including when disposal is
cancelled, where the single disposal still happens and the cancellation
propagates.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from api_runtime.database_lifecycle import DatabaseRuntimeLifecycle
from pydantic import SecretStr

from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError
from personal_os.runtime_configuration.models import RuntimeEnvironment
from postgresql_source_store.settings import DatabaseRuntimeSettings


def _settings(tmp_path: Path) -> DatabaseRuntimeSettings:
    return DatabaseRuntimeSettings(
        environment=RuntimeEnvironment.LOCAL,
        secret_root=tmp_path / "secrets",
    )


def _password() -> SecretStr:
    return SecretStr("lifecycle-password-value")


@dataclass
class _FakeScalarResult:
    revisions: tuple[str, ...]

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self.revisions)


@dataclass
class _FakeScalars:
    revisions: tuple[str, ...]

    def all(self) -> tuple[str, ...]:
        return self.revisions


@dataclass
class _FakeConnection:
    revisions: tuple[str, ...]

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None

    async def execute(self, _statement: object) -> _FakeScalarResult:
        return _FakeScalarResult(self.revisions)


class _FakeAsyncEngine:
    """Minimal structural stand-in recording connect and dispose attempts."""

    def __init__(self, revisions: Sequence[str]) -> None:
        self.connect_calls = 0
        self.dispose_calls = 0
        self._revisions = tuple(revisions)

    def connect(self) -> _FakeConnection:
        self.connect_calls += 1
        return _FakeConnection(self._revisions)

    async def dispose(self) -> None:
        self.dispose_calls += 1


@dataclass
class _RecordingEngineFactory:
    """Engine factory recording every construction with its exact inputs."""

    engines: list[_FakeAsyncEngine] = field(default_factory=list)
    calls: list[tuple[DatabaseRuntimeSettings, SecretStr]] = field(default_factory=list)

    def __call__(self, settings: DatabaseRuntimeSettings, password: SecretStr) -> _FakeAsyncEngine:
        self.calls.append((settings, password))
        engine = _FakeAsyncEngine((CANONICAL_POSTGRESQL_SCHEMA_REVISION,))
        self.engines.append(engine)
        return engine


async def _default_disposer(engine: _FakeAsyncEngine) -> None:
    await engine.dispose()


@pytest.mark.asyncio
async def test_start_creates_engine_lazily_without_connecting(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    factory = _RecordingEngineFactory()
    lifecycle = DatabaseRuntimeLifecycle(
        settings,
        _password(),
        engine_factory=factory,
        engine_disposer=_default_disposer,
    )

    assert factory.engines == []
    await lifecycle.start()

    assert len(factory.engines) == 1
    assert factory.calls[0] == (settings, _password())
    assert factory.engines[0].connect_calls == 0

    await lifecycle.start()
    assert len(factory.engines) == 1, "start must not create a second engine"


@pytest.mark.asyncio
async def test_check_fails_safely_before_start(tmp_path: Path) -> None:
    factory = _RecordingEngineFactory()
    lifecycle = DatabaseRuntimeLifecycle(
        _settings(tmp_path),
        _password(),
        engine_factory=factory,
        engine_disposer=_default_disposer,
    )

    with pytest.raises(DatabaseMigrationError) as raised:
        await lifecycle.check()

    assert raised.value.error_code is ErrorCode.DATABASE_CONNECTION_UNAVAILABLE
    assert factory.engines == [], "check must not create the engine"


@pytest.mark.asyncio
async def test_check_delegates_to_readiness_probe_after_start(tmp_path: Path) -> None:
    engine = _FakeAsyncEngine((CANONICAL_POSTGRESQL_SCHEMA_REVISION,))
    lifecycle = DatabaseRuntimeLifecycle(
        _settings(tmp_path),
        _password(),
        engine_factory=lambda _settings, _password: engine,
        engine_disposer=_default_disposer,
    )
    await lifecycle.start()

    await lifecycle.check()

    assert engine.connect_calls == 1


@pytest.mark.asyncio
async def test_check_maps_probe_schema_mismatch_after_start(tmp_path: Path) -> None:
    engine = _FakeAsyncEngine(("stale-revision", CANONICAL_POSTGRESQL_SCHEMA_REVISION))
    lifecycle = DatabaseRuntimeLifecycle(
        _settings(tmp_path),
        _password(),
        engine_factory=lambda _settings, _password: engine,
        engine_disposer=_default_disposer,
    )
    await lifecycle.start()

    with pytest.raises(DatabaseMigrationError) as raised:
        await lifecycle.check()

    assert raised.value.error_code is ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID


@pytest.mark.asyncio
async def test_stop_disposes_exactly_once_and_is_idempotent(tmp_path: Path) -> None:
    engine = _FakeAsyncEngine((CANONICAL_POSTGRESQL_SCHEMA_REVISION,))
    lifecycle = DatabaseRuntimeLifecycle(
        _settings(tmp_path),
        _password(),
        engine_factory=lambda _settings, _password: engine,
        engine_disposer=_default_disposer,
    )
    await lifecycle.start()

    await lifecycle.stop()
    assert engine.dispose_calls == 1

    await lifecycle.stop()
    assert engine.dispose_calls == 1, "stop must dispose exactly once"

    with pytest.raises(DatabaseMigrationError) as raised:
        await lifecycle.check()
    assert raised.value.error_code is ErrorCode.DATABASE_CONNECTION_UNAVAILABLE


@pytest.mark.asyncio
async def test_stop_before_start_is_a_safe_no_op(tmp_path: Path) -> None:
    factory = _RecordingEngineFactory()
    lifecycle = DatabaseRuntimeLifecycle(
        _settings(tmp_path),
        _password(),
        engine_factory=factory,
        engine_disposer=_default_disposer,
    )

    await lifecycle.stop()

    assert factory.engines == []


@pytest.mark.asyncio
async def test_stop_disposes_once_under_cancellation_and_reraises(
    tmp_path: Path,
) -> None:
    engine = _FakeAsyncEngine((CANONICAL_POSTGRESQL_SCHEMA_REVISION,))
    dispose_attempts = 0

    async def cancelling_disposer(_engine: _FakeAsyncEngine) -> None:
        nonlocal dispose_attempts
        dispose_attempts += 1
        raise asyncio.CancelledError

    lifecycle = DatabaseRuntimeLifecycle(
        _settings(tmp_path),
        _password(),
        engine_factory=lambda _settings, _password: engine,
        engine_disposer=cancelling_disposer,
    )
    await lifecycle.start()

    with pytest.raises(asyncio.CancelledError):
        await lifecycle.stop()

    assert dispose_attempts == 1

    await lifecycle.stop()
    assert dispose_attempts == 1, "a cancelled stop must not dispose a second time"
