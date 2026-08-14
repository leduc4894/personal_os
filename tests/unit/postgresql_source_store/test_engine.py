"""Async engine lifecycle and per-transaction bounds for the source store.

These tests prove the engine contract: the ``postgresql+psycopg`` driver, the
pinned pool bounds and pre-ping, the bound connect arguments, the explicit
lifecycle disposal, the ``READ COMMITTED`` isolation level, the ``SET LOCAL``
transaction-bounds statements (5/15/30 seconds) and that importing the module
creates no engine, connection or session. The URL is never rendered and never
carries the password into a string.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool, Pool

from postgresql_source_store.engine import (
    TRANSACTION_BOUND_STATEMENTS,
    TRANSACTION_ISOLATION_LEVEL,
    apply_transaction_bounds,
    build_source_database_url,
    build_source_store_connect_arguments,
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.settings import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_POOL_OVERFLOW,
    POOL_SIZE,
    POOL_TIMEOUT_SECONDS,
    DatabaseRuntimeSettings,
)


def _settings(tmp_path: Path) -> DatabaseRuntimeSettings:
    secret_root = tmp_path / "secrets"
    if not secret_root.exists():
        secret_root.mkdir()
    return DatabaseRuntimeSettings(secret_root=secret_root)


def _password() -> SecretStr:
    return SecretStr("engine-password-value")


# --- URL and connect arguments ---------------------------------------------


def test_url_uses_postgresql_psycopg_driver(tmp_path: Path) -> None:
    url = build_source_database_url(_settings(tmp_path), _password())
    assert isinstance(url, URL)
    assert url.drivername == "postgresql+psycopg"
    assert url.username == "knowledge_app"
    assert url.host == "127.0.0.1"
    assert url.port == 5432
    assert url.database == "knowledge"
    # The password exists only inside the URL object and is masked in renders.
    assert url.password == "engine-password-value"
    assert "engine-password-value" not in str(url)
    assert repr(url).count("***") == 1


def test_connect_arguments_are_bound_and_carry_no_secret(tmp_path: Path) -> None:
    connect_arguments = build_source_store_connect_arguments(_settings(tmp_path))
    assert dict(connect_arguments) == {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "sslmode": "disable",
        "application_name": "knowledge-source-store",
    }
    assert "password" not in connect_arguments


# --- engine construction and pinned pool bounds ----------------------------


def test_engine_pins_pool_bounds_and_pre_ping(tmp_path: Path) -> None:
    engine = create_source_store_engine(_settings(tmp_path), _password())
    try:
        assert isinstance(engine, AsyncEngine)
        pool = engine.sync_engine.pool
        assert isinstance(pool, AsyncAdaptedQueuePool)
        assert pool.size() == POOL_SIZE
        assert pool._max_overflow == MAX_POOL_OVERFLOW
        assert pool._timeout == POOL_TIMEOUT_SECONDS
        assert pool._pre_ping is True
    finally:
        engine.sync_engine.dispose()


def test_engine_uses_read_committed_isolation(tmp_path: Path) -> None:
    engine = create_source_store_engine(_settings(tmp_path), _password())
    try:
        # ``create_async_engine(isolation_level=...)`` stores the level the
        # dialect applies on every new connection.
        assert engine.sync_engine.dialect._on_connect_isolation_level == (
            TRANSACTION_ISOLATION_LEVEL
        )
        assert TRANSACTION_ISOLATION_LEVEL == "READ COMMITTED"
    finally:
        engine.sync_engine.dispose()


def test_engine_repr_never_renders_password(tmp_path: Path) -> None:
    engine = create_source_store_engine(_settings(tmp_path), _password())
    try:
        assert "engine-password-value" not in repr(engine)
        assert "engine-password-value" not in str(engine)
    finally:
        engine.sync_engine.dispose()


# --- explicit lifecycle disposal -------------------------------------------


@pytest.mark.asyncio
async def test_dispose_closes_the_pool_and_replaces_it(tmp_path: Path) -> None:
    engine = create_source_store_engine(_settings(tmp_path), _password())
    pool_before = engine.sync_engine.pool
    await dispose_source_store_engine(engine)
    assert engine.sync_engine.pool is not pool_before
    # A second dispose is a safe no-op (idempotent lifecycle boundary).
    await dispose_source_store_engine(engine)


# --- SET LOCAL transaction bounds ------------------------------------------


class _RecordingConnection:
    """Minimal async-connection double capturing executed statement text."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, clause: Any, *parameters: Any, **kwargs: Any) -> None:
        self.statements.append(str(clause))


def test_transaction_bound_statements_are_exact_and_ordered() -> None:
    assert TRANSACTION_BOUND_STATEMENTS == (
        "SET LOCAL lock_timeout = '5000ms'",
        "SET LOCAL statement_timeout = '15000ms'",
        "SET LOCAL idle_in_transaction_session_timeout = '30000ms'",
    )


@pytest.mark.asyncio
async def test_apply_transaction_bounds_executes_each_statement_once() -> None:
    connection = _RecordingConnection()
    await apply_transaction_bounds(cast(AsyncConnection, connection))
    assert connection.statements == list(TRANSACTION_BOUND_STATEMENTS)


# --- no engine, connection or session is created at import -----------------


def test_module_import_creates_no_engine_or_pool() -> None:
    import postgresql_source_store.engine as engine_module

    leaked = [
        name
        for name, value in vars(engine_module).items()
        if isinstance(value, (AsyncEngine, Pool))
    ]
    assert leaked == []
