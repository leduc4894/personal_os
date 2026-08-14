"""Async engine lifecycle and per-transaction bounds for the source store.

This module owns the composition-owned :class:`AsyncEngine` boundary: the
``postgresql+psycopg`` URL (built but never rendered), the pinned pool bounds
and pre-ping, the bound connect arguments, explicit disposal and the ``SET
LOCAL`` transaction-bounds helper applied immediately after every
``READ COMMITTED`` begin. Importing this module creates no engine, connection
or session; the engine is constructed and disposed only by a composition root
through :func:`create_source_store_engine` and
:func:`dispose_source_store_engine`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from postgresql_source_store.settings import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_POOL_OVERFLOW,
    POOL_SIZE,
    POOL_TIMEOUT_SECONDS,
    DatabaseRuntimeSettings,
)

_DATABASE_DRIVER: Final[str] = "postgresql+psycopg"
_APPLICATION_NAME: Final[str] = "knowledge-source-store"

#: Every transaction runs ``READ COMMITTED``; serialisation comes from the
#: transaction-scoped advisory locks, not from a stronger isolation level.
TRANSACTION_ISOLATION_LEVEL: Final[str] = "READ COMMITTED"

#: Applied immediately after each ``READ COMMITTED`` begin via
#: :func:`apply_transaction_bounds`. ``SET LOCAL`` scopes the timeouts to the
#: current transaction only, so pooled connections carry no residual state.
TRANSACTION_BOUND_STATEMENTS: Final[tuple[str, ...]] = (
    "SET LOCAL lock_timeout = '5000ms'",
    "SET LOCAL statement_timeout = '15000ms'",
    "SET LOCAL idle_in_transaction_session_timeout = '30000ms'",
)


def build_source_database_url(
    settings: DatabaseRuntimeSettings,
    password: SecretStr,
) -> URL:
    """Build the SQLAlchemy source-store URL without ever rendering it.

    The password is placed into the :class:`URL` value only; the URL is never
    converted to a string outside tests, so diagnostics can never carry the
    credential.
    """
    return URL.create(
        drivername=_DATABASE_DRIVER,
        username=settings.database_user,
        password=password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.database_name,
    )


def build_source_store_connect_arguments(
    settings: DatabaseRuntimeSettings,
) -> Mapping[str, str | int]:
    """Build the fixed psycopg connect arguments for every pooled connection."""
    return {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "sslmode": settings.ssl_mode.value,
        "application_name": _APPLICATION_NAME,
    }


def create_source_store_engine(
    settings: DatabaseRuntimeSettings,
    password: SecretStr,
) -> AsyncEngine:
    """Create the composition-owned async engine with the pinned bounds.

    No connection is opened here: the pool connects lazily. ``pool_pre_ping``
    discards stale pooled connections, and the explicit ``READ COMMITTED``
    isolation level pins the transaction contract for every checkout.
    """
    return create_async_engine(
        build_source_database_url(settings, password),
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=MAX_POOL_OVERFLOW,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        isolation_level=TRANSACTION_ISOLATION_LEVEL,
        connect_args=dict(build_source_store_connect_arguments(settings)),
    )


async def dispose_source_store_engine(engine: AsyncEngine) -> None:
    """Dispose the engine explicitly, closing the pool and every connection.

    Disposal is idempotent and requires no network call; a second dispose after
    the pool is already closed is a safe no-op.
    """
    await engine.dispose()


async def apply_transaction_bounds(connection: AsyncConnection) -> None:
    """Apply the ``SET LOCAL`` bounds to the connection's current transaction.

    The first statement begins the ``READ COMMITTED`` transaction on a freshly
    checked-out connection; every bound is transaction-local so a rollback or
    commit clears it completely.
    """
    for statement in TRANSACTION_BOUND_STATEMENTS:
        await connection.execute(sa.text(statement))
