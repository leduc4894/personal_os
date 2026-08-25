"""Bounded PostgreSQL readiness probe: connectivity plus exact schema head.

These tests prove the probe contract: the two exact SQL statements in order
(``SELECT 1`` then the ordered ``alembic_version`` head query), acceptance
only when the materialized revision set is exactly the canonical head,
rejection of missing/behind/ahead/multiple revisions, connection-class driver
failures mapped to ``database_connection_unavailable``, cancellation
propagated while the connection still closes, and conformance to the
runtime-checkable :class:`CanonicalDatabaseReadinessProbe` protocol. The
two-second overall deadline is owned by the composition root, not here.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from personal_os.api_contracts import CanonicalDatabaseReadinessProbe
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError
from postgresql_source_store.readiness import PostgresqlReadinessProbe

CONNECTIVITY_SQL = "SELECT 1"
REVISIONS_SQL = "SELECT version_num FROM public.alembic_version ORDER BY version_num"


class ScriptedResult:
    """Result double exposing ``scalars().all()`` over the scripted rows."""

    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> ScriptedResult:
        return self

    def all(self) -> list[object]:
        return list(self._values)


class ScriptedConnection:
    """Async connection double scripting the readiness statement sequence."""

    def __init__(self, engine: ScriptedEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> ScriptedConnection:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self._engine.connection_was_closed = True
        return False

    async def execute(self, statement: object) -> ScriptedResult:
        sql = str(statement)
        self._engine.executed_sql.append(sql)
        if sql == CONNECTIVITY_SQL:
            if self._engine.connectivity_failure is not None:
                raise self._engine.connectivity_failure
            return ScriptedResult([self._engine.connectivity])
        if sql == REVISIONS_SQL:
            if self._engine.cancel_during_revision:
                raise asyncio.CancelledError
            return ScriptedResult(list(self._engine.revisions))
        raise AssertionError(f"unexpected statement: {sql}")


class ScriptedEngine:
    """Engine double handing out one scripted async-connection context."""

    def __init__(
        self,
        *,
        connectivity: int = 1,
        revisions: tuple[str, ...] = (),
        cancel_during_revision: bool = False,
        connectivity_failure: BaseException | None = None,
    ) -> None:
        self.connectivity = connectivity
        self.revisions = revisions
        self.cancel_during_revision = cancel_during_revision
        self.connectivity_failure = connectivity_failure
        self.executed_sql: list[str] = []
        self.connection_was_closed = False

    def connect(self) -> ScriptedConnection:
        return ScriptedConnection(self)


def test_probe_satisfies_canonical_database_readiness_probe_protocol() -> None:
    probe = PostgresqlReadinessProbe(ScriptedEngine())
    assert isinstance(probe, CanonicalDatabaseReadinessProbe)


@pytest.mark.asyncio
async def test_readiness_accepts_connectivity_and_exact_single_head() -> None:
    engine = ScriptedEngine(connectivity=1, revisions=("20260826_01",))
    await PostgresqlReadinessProbe(engine).check()
    assert engine.executed_sql == [
        "SELECT 1",
        "SELECT version_num FROM public.alembic_version ORDER BY version_num",
    ]
    assert engine.connection_was_closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("revisions", [(), ("older",), ("future",), ("a", "b")])
async def test_readiness_rejects_every_non_exact_revision_set(
    revisions: tuple[str, ...],
) -> None:
    with pytest.raises(DatabaseMigrationError) as raised:
        await PostgresqlReadinessProbe(ScriptedEngine(connectivity=1, revisions=revisions)).check()
    assert raised.value.error_code is ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            SQLAlchemyOperationalError("SELECT 1", {}, Exception("connection refused")),
            id="operational_error",
        ),
        pytest.param(
            SQLAlchemyTimeoutError("pool checkout timed out"),
            id="timeout_error",
        ),
    ],
)
async def test_readiness_maps_connection_failure_to_unavailable(
    failure: Exception,
) -> None:
    engine = ScriptedEngine(connectivity_failure=failure)
    with pytest.raises(DatabaseMigrationError) as raised:
        await PostgresqlReadinessProbe(engine).check()
    assert raised.value.error_code is ErrorCode.DATABASE_CONNECTION_UNAVAILABLE
    assert raised.value.__cause__ is None
    assert engine.executed_sql == [CONNECTIVITY_SQL]
    assert engine.connection_was_closed is True


@pytest.mark.asyncio
async def test_readiness_propagates_cancellation_and_closes_connection() -> None:
    engine = ScriptedEngine(cancel_during_revision=True)
    with pytest.raises(asyncio.CancelledError):
        await PostgresqlReadinessProbe(engine).check()
    assert engine.connection_was_closed is True
