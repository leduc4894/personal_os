"""Canonical PostgreSQL migration runtime and Alembic baseline.

This package owns the migration connection boundary (typed settings, the bounded
password reader and the SQLAlchemy ``URL`` / psycopg connect-argument builders).
It deliberately imports SQLAlchemy, psycopg and Alembic, which are approved
migration-only dependencies that must remain outside ``src/personal_os/``.
"""
