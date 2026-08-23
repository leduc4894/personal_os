"""Bounded PostgreSQL readiness probe: connectivity plus exact schema head.

Implements the canonical database readiness probe contract (``async def
check() -> None``) for the canonical PostgreSQL baseline. The probe runs
exactly two constant statements — ``SELECT 1`` then the ordered
``alembic_version`` head query — materializes every revision row so multiple
heads can never be mistaken for one, and maps driver failures onto the closed
database error codes with the driver cause suppressed, so statements,
parameters and driver text never cross the boundary. The two-second overall
deadline is deliberately absent: the composition root owns it around the
complete readiness operation.
"""

from __future__ import annotations

import asyncio
from typing import Final

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError

_CONNECTIVITY: Final[sa.TextClause] = sa.text("SELECT 1")
_SCHEMA_REVISIONS: Final[sa.TextClause] = sa.text(
    "SELECT version_num FROM public.alembic_version ORDER BY version_num"
)


class PostgresqlReadinessProbe:
    """Connectivity plus exact-single-head probe over the canonical baseline."""

    def __init__(
        self,
        engine: AsyncEngine,
        expected_revision: str = CANONICAL_POSTGRESQL_SCHEMA_REVISION,
    ) -> None:
        self._engine = engine
        self._expected_revision = expected_revision

    async def check(self) -> None:
        """Prove connectivity and the exact expected schema head or raise.

        Raises the retryable ``database_connection_unavailable`` for
        connection-class driver failures, the non-retryable
        ``database_schema_contract_invalid`` for any other driver failure or a
        revision set that is not exactly the expected head, and propagates
        cancellation while the connection context still closes.
        """
        try:
            async with self._engine.connect() as connection:
                await connection.execute(_CONNECTIVITY)
                result = await connection.execute(_SCHEMA_REVISIONS)
                revisions = tuple(str(value) for value in result.scalars().all())
                if revisions != (self._expected_revision,):
                    raise DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID)
        except asyncio.CancelledError:
            raise
        except DatabaseMigrationError:
            raise
        except (SQLAlchemyOperationalError, SQLAlchemyTimeoutError):
            raise DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE) from None
        except SQLAlchemyError:
            raise DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID) from None
