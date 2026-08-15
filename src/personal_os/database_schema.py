"""Canonical PostgreSQL schema revision authority shared by core and adapters.

This module is the single source of truth for the pinned Alembic head every
runtime must observe before serving. It imports nothing and carries only the
closed revision constant, so provider-neutral core packages and SQL adapters
share one authority without bridging architectural boundaries.
"""

from __future__ import annotations

from typing import Final

#: Canonical PostgreSQL baseline pinned by the acceptance/recovery contract;
#: readiness accepts exactly this head and nothing else.
CANONICAL_POSTGRESQL_SCHEMA_REVISION: Final[str] = "20260813_01"
