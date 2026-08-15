"""API readiness integration against a disposable PostgreSQL 18.4 stack.

Every case drives the real :class:`PostgresqlReadinessProbe` over a real
engine created by :func:`create_source_store_engine`: the exact-head case
proves the migrated canonical baseline reports current head, the
revision-drift case rewrites ``public.alembic_version.version_num`` to
``stale_revision`` on the disposable committed database and proves the
non-retryable ``database_schema_contract_invalid`` before restoring the exact
head (``20260813_01`` via the canonical revision constant) in ``finally``, and
the refused-port case points the same real settings at a refused loopback port
and proves the retryable ``database_connection_unavailable``.

Marker decision: the refused-port case needs no Docker of its own, but it
derives its settings from the disposable stack fixture (only host and port are
overridden, exactly like a real misconfiguration of the approved settings
model), so it stays inside this ``local_stack`` module for one coherent
readiness suite and runs only in the explicit local-stack gate.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.canonical_core.conftest import CanonicalCoreStack

from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.readiness import PostgresqlReadinessProbe

pytestmark = pytest.mark.local_stack

_DRIFTED_REVISION = "stale_revision"
_REFUSED_LOOPBACK_PORT = 1


async def _set_alembic_revision(engine: AsyncEngine, revision: str) -> None:
    """Rewrite the disposable database's single alembic revision row."""
    async with engine.begin() as connection:
        await connection.execute(
            sa.text("UPDATE public.alembic_version SET version_num = :revision").bindparams(
                revision=revision
            )
        )


@pytest.mark.asyncio
async def test_real_postgresql_reports_current_head(
    canonical_core_stack: CanonicalCoreStack,
) -> None:
    engine = create_source_store_engine(
        canonical_core_stack.settings, canonical_core_stack.password
    )
    try:
        await PostgresqlReadinessProbe(engine).check()
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_stale_revision_reports_schema_contract_invalid_then_restores_head(
    canonical_core_stack: CanonicalCoreStack,
) -> None:
    engine = create_source_store_engine(
        canonical_core_stack.settings, canonical_core_stack.password
    )
    try:
        await _set_alembic_revision(engine, _DRIFTED_REVISION)
        try:
            with pytest.raises(DatabaseMigrationError) as raised:
                await PostgresqlReadinessProbe(engine).check()
        finally:
            await _set_alembic_revision(engine, CANONICAL_POSTGRESQL_SCHEMA_REVISION)
        assert raised.value.error_code == ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID
        # The exact head (20260813_01) is restored: the same probe is current again.
        await PostgresqlReadinessProbe(engine).check()
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_refused_port_reports_connection_unavailable(
    canonical_core_stack: CanonicalCoreStack,
) -> None:
    refused_settings = canonical_core_stack.settings.model_copy(
        update={"host": "127.0.0.1", "port": _REFUSED_LOOPBACK_PORT}
    )
    engine = create_source_store_engine(refused_settings, canonical_core_stack.password)
    try:
        with pytest.raises(DatabaseMigrationError) as raised:
            await PostgresqlReadinessProbe(engine).check()
        assert raised.value.error_code == ErrorCode.DATABASE_CONNECTION_UNAVAILABLE
    finally:
        await dispose_source_store_engine(engine)
