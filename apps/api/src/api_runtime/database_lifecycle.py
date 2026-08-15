"""API process database lifecycle: lazily created engine and readiness probe.

This module owns the process-lifetime database boundary for the API
composition root. The engine is constructed only inside :meth:`start` (and no
connection is opened there — the pool connects lazily), the readiness probe is
constructed from that engine and reached through :meth:`check`, which fails
with the registered safe ``database_connection_unavailable`` error before the
lifecycle has started. :meth:`stop` disposes exactly once and stays idempotent,
including under cancellation: the engine reference is cleared before disposal
is awaited, so a cancelled disposal still re-raises without ever disposing a
second time.

The class satisfies the canonical database readiness probe contract
structurally, so the composed application's readiness endpoint delegates to
the same object that owns the engine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.readiness import PostgresqlReadinessProbe
from postgresql_source_store.settings import DatabaseRuntimeSettings

type EngineFactory = Callable[[DatabaseRuntimeSettings, SecretStr], AsyncEngine]
type EngineDisposer = Callable[[AsyncEngine], Awaitable[None]]


class DatabaseRuntimeLifecycle:
    """Owns the API process engine, its probe and their exactly-once disposal."""

    def __init__(
        self,
        settings: DatabaseRuntimeSettings,
        password: SecretStr,
        *,
        engine_factory: EngineFactory = create_source_store_engine,
        engine_disposer: EngineDisposer = dispose_source_store_engine,
    ) -> None:
        self._settings = settings
        self._password = password
        self._engine_factory = engine_factory
        self._engine_disposer = engine_disposer
        self._engine: AsyncEngine | None = None
        self._probe: PostgresqlReadinessProbe | None = None

    async def start(self) -> None:
        """Create the engine and its probe; open no connection.

        Calling ``start`` again after a successful start is a no-op, so an
        aborted startup sequence can safely re-run.
        """
        if self._engine is not None:
            return
        self._engine = self._engine_factory(self._settings, self._password)
        self._probe = PostgresqlReadinessProbe(self._engine)

    async def stop(self) -> None:
        """Dispose the engine exactly once; stay idempotent under cancellation.

        The engine reference is cleared before disposal is awaited, so even a
        cancelled disposal (which propagates :class:`asyncio.CancelledError`)
        never triggers a second disposal attempt.
        """
        engine = self._engine
        self._engine = None
        self._probe = None
        if engine is None:
            return
        await self._engine_disposer(engine)

    async def check(self) -> None:
        """Run the readiness probe, or fail safely before the lifecycle starts."""
        if self._probe is None:
            raise DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE)
        await self._probe.check()
