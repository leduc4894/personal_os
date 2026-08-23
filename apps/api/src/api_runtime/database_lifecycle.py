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

:meth:`verify_exclusion_policy_signer` is the fail-before-bind signing proof
of spec 13.1/22: it reads the latest canonical keyset payload of every
initialized workspace and requires the configured signer's derived key ID to
equal each keyset's current key. The composition root calls it inside the
application lifespan startup — before Uvicorn binds the listening socket —
so a missing, unknown or mismatched signer aborts startup; driver failures
map onto the closed database error codes with the driver text suppressed.

The class satisfies the canonical database readiness probe contract
structurally, so the composed application's readiness endpoint delegates to
the same object that owns the engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncEngine

from api_runtime.exclusion_policy_settings import (
    assert_signer_is_current_in_latest_keysets,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ApplicationError,
    DatabaseMigrationError,
    InternalApplicationError,
)
from postgresql_source_store.engine import (
    apply_transaction_bounds,
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.readiness import PostgresqlReadinessProbe
from postgresql_source_store.settings import DatabaseRuntimeSettings

type EngineFactory = Callable[[DatabaseRuntimeSettings, SecretStr], AsyncEngine]
type EngineDisposer = Callable[[AsyncEngine], Awaitable[None]]

#: One bounded read of the latest keyset revision per initialized workspace:
#: the canonical payload bytes the current-key proof parses.
_LATEST_KEYSET_PAYLOADS: Final[sa.TextClause] = sa.text(
    "SELECT DISTINCT ON (workspace_id) canonical_payload_bytes"
    " FROM knowledge.policy_keysets"
    " ORDER BY workspace_id, keyset_revision DESC"
)


async def fetch_latest_keyset_payloads(engine: AsyncEngine) -> list[bytes]:
    """Read the latest canonical keyset payload of every initialized workspace.

    The single bounded statement keeps the read inside one transaction with
    the shared ``SET LOCAL`` bounds. Connection-class driver failures map to
    the retryable ``database_connection_unavailable``, every other driver
    failure to the non-retryable ``database_schema_contract_invalid``, and a
    non-driver failure fails closed as the safe ``internal_error`` — driver
    text never crosses the boundary.
    """

    try:
        async with (
            engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(_LATEST_KEYSET_PAYLOADS)
            payloads = [bytes(row[0]) for row in result]
    except asyncio.CancelledError:
        raise
    except ApplicationError:
        raise
    except sa_exc.OperationalError, sa_exc.TimeoutError:
        raise DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE) from None
    except sa_exc.SQLAlchemyError:
        raise DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID) from None
    except Exception as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
    return payloads


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

    async def verify_exclusion_policy_signer(self, *, signing_key_id: str) -> None:
        """Prove the configured signer is the current key before socket bind.

        Uvicorn runs the application lifespan startup before binding the
        listening socket, so this refusal (no initialized keyset, an unknown
        active key or a staged/retired signer) aborts startup exactly like
        the keyring-reference verification of spec 20.1.
        """
        engine = self._engine
        if engine is None:
            raise DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE)
        try:
            payloads: Sequence[bytes] = await fetch_latest_keyset_payloads(engine)
        except asyncio.CancelledError:
            raise
        except ApplicationError:
            raise
        except Exception as cause:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
        assert_signer_is_current_in_latest_keysets(payloads, signing_key_id=signing_key_id)
