"""Online-only Alembic environment for the canonical PostgreSQL baseline.

The environment consumes the frozen migration settings runtime
(:mod:`migrations.database_migration_runtime`) so URL, password and connect
argument logic is never duplicated here. Settings and the filesystem secret
are loaded only inside the online migration path: Alembic commands that never
execute ``env.py`` (``heads``, ``history``) require no database configuration
and never touch the secret file.

Failure rendering is leak-safe: only approved ``DatabaseMigrationError``
codes and their registered safe messages are rendered through
:class:`alembic.util.CommandError`. The connection URL, raw driver exceptions,
SQL parameters and vendor message text are never printed.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import SQLAlchemyError

from migrations.database_migration_runtime import (
    build_database_connect_arguments,
    build_database_url,
    load_database_migration_settings,
    read_database_password,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError, SecretFileError

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

_ADVISORY_LOCK_SQL: str = (
    "SELECT pg_advisory_xact_lock(hashtextextended('knowledge-schema-migration', 0))"
)
_CURRENT_DATABASE_SQL: str = "SELECT current_database()"
_SERVER_MAJOR_SQL: str = "SELECT current_setting('server_version_num')::integer / 10000"
_REQUIRED_SERVER_MAJOR: int = 18
_DESTRUCTIVE_X_ARGUMENT: str = "allow_destructive"
_LOCK_TIMEOUT_SQLSTATE: str = "55P03"
_OFFLINE_REJECTED_MESSAGE: str = "offline_sql_migrations_not_supported"


def _is_downgrade_command(config: Config) -> bool:
    """Return whether the active Alembic command is ``downgrade``.

    ``Config.cmd_opts`` is absent when Alembic loads ``env.py`` outside a CLI
    command, and the parsed namespace stores the resolved command function in
    its ``cmd`` attribute as ``(function, positional, keyword)``.
    """
    command_options = getattr(config, "cmd_opts", None)
    if command_options is None:
        return False
    resolved_command = getattr(command_options, "cmd", None)
    if not resolved_command:
        return False
    command_function = resolved_command[0]
    return getattr(command_function, "__name__", "") == "downgrade"


def _require_destructive_authorization(config: Config) -> None:
    """Refuse a downgrade unless ``-x allow_destructive=true`` is present exactly.

    ``config`` is part of the required helper signature; the authoritative
    x-argument dictionary is read from the active Alembic command context.
    """
    _ = config
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get(_DESTRUCTIVE_X_ARGUMENT) != "true":
        raise DatabaseMigrationError(ErrorCode.DATABASE_DESTRUCTIVE_DOWNGRADE_REFUSED)


def _verify_database_prerequisites(connection: Connection, database_name: str) -> None:
    """Fail closed on a wrong database or an unsupported PostgreSQL major."""
    current_database = connection.execute(text(_CURRENT_DATABASE_SQL)).scalar_one()
    if current_database != database_name:
        raise DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID)
    server_major = connection.execute(text(_SERVER_MAJOR_SQL)).scalar_one()
    if server_major != _REQUIRED_SERVER_MAJOR:
        raise DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID)


def _run_online_migrations() -> None:
    """Run the baseline in one transaction behind the migration advisory lock."""
    if context.is_offline_mode():
        raise CommandError(_OFFLINE_REJECTED_MESSAGE)
    settings = load_database_migration_settings()
    password = read_database_password(settings)
    if _is_downgrade_command(config):
        _require_destructive_authorization(config)
    engine = create_engine(
        build_database_url(settings, password),
        poolclass=pool.NullPool,
        connect_args=dict(build_database_connect_arguments(settings)),
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                transactional_ddl=True,
                transaction_per_migration=True,
                compare_type=True,
                include_schemas=True,
            )
            with context.begin_transaction():
                connection.execute(text(_ADVISORY_LOCK_SQL))
                _verify_database_prerequisites(connection, settings.database_name)
                context.run_migrations()
    finally:
        engine.dispose()


def _classify_operational_failure(
    error: SQLAlchemyOperationalError,
) -> DatabaseMigrationError:
    """Classify by SQLSTATE only; vendor message text is never inspected."""
    original: object = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate == _LOCK_TIMEOUT_SQLSTATE:
        return DatabaseMigrationError(ErrorCode.DATABASE_MIGRATION_BUSY)
    return DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE)


def _raise_command_error(error: DatabaseMigrationError) -> None:
    raise CommandError(f"{error.error_code.value}: {error.safe_message}") from None


def run_migrations_online() -> None:
    """CLI boundary: map every failure to an approved code and safe message."""
    try:
        _run_online_migrations()
    except DatabaseMigrationError as error:
        _raise_command_error(error)
    except SecretFileError:
        _raise_command_error(
            DatabaseMigrationError(ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID)
        )
    except SQLAlchemyOperationalError as error:
        _raise_command_error(_classify_operational_failure(error))
    except SQLAlchemyError:
        _raise_command_error(DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID))
    except Exception:
        # Any unexpected failure (including the sanctioned in-process
        # ``canonical_baseline_before_verify`` test seam, which raises a plain
        # exception carrying a sentinel) is mapped to a safe code. ``from None``
        # suppresses the original so a sentinel, vendor text or other unexpected
        # content never reaches an operator or CI log.
        _raise_command_error(DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID))


if context.is_offline_mode():
    raise CommandError(_OFFLINE_REJECTED_MESSAGE)
else:
    run_migrations_online()
