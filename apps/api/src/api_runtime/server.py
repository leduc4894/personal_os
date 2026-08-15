"""API server runner: snapshot loading, diagnostics, lifespan and Uvicorn.

This module is the serve composition root. :func:`run_server` captures the
process environment exactly once at entry, loads the runtime, API and database
settings plus the secret-file password, configures structured diagnostics,
builds the database lifecycle and the FastAPI application (wiring the lifecycle
into the application lifespan so the engine starts on startup and is disposed
on shutdown), then runs Uvicorn in single-process mode with the approved
flags: no server header, no proxy headers, no access log, no reload and
exactly one worker.

Exit codes follow the process contract: configuration and secret failures
exit ``78`` with one safe emergency record, unexpected startup failures exit
``70``, and a clean server shutdown exits ``0``. A ``SystemExit`` raised by
the server run is translated, not propagated: ``0``/``None`` map to the clean
shutdown exit and any other code (Uvicorn signals bind failures through
``sys.exit(3)``) maps to ``70``. Raw exception text is never printed. This
module is imported only inside the lazy serve callback, so shell-only
invocations never load FastAPI or Uvicorn.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Final, Protocol

import uvicorn
from fastapi import FastAPI

from api_runtime.application import create_api_application
from api_runtime.database_lifecycle import DatabaseRuntimeLifecycle
from api_runtime.server_settings import load_api_server_settings
from personal_os.diagnostics.context import bind_diagnostic_context, create_diagnostic_context
from personal_os.diagnostics.logging import (
    configure_diagnostics,
    emit_emergency_application_error,
    emit_emergency_internal_error,
)
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import ServiceName
from postgresql_source_store.settings import (
    load_database_runtime_settings,
    read_database_runtime_password,
)

_EXIT_CONFIGURATION_FAILURE: Final[int] = 78
_EXIT_INTERNAL_FAILURE: Final[int] = 70
_EXIT_CLEAN_SHUTDOWN: Final[int] = 0


class RunnableUvicornServer(Protocol):
    """Structural contract of one runnable Uvicorn server object."""

    @property
    def config(self) -> uvicorn.Config: ...

    def run(self) -> None: ...


class ServerFactory(Protocol):
    """Callable building one runnable server from a prepared Uvicorn config."""

    def __call__(self, config: uvicorn.Config) -> RunnableUvicornServer: ...


def run_server(
    *,
    environ: Mapping[str, str] | None = None,
    server_factory: ServerFactory = uvicorn.Server,
) -> int:
    """Run one API server process and return its exit code."""
    environment_snapshot: Mapping[str, str] = dict(os.environ) if environ is None else environ
    resolution = create_diagnostic_context()
    context = resolution.context
    with bind_diagnostic_context(context):
        try:
            runtime_settings = load_runtime_settings(ServiceName.API, environ=environment_snapshot)
            api_settings = load_api_server_settings(environ=environment_snapshot)
            database_settings = load_database_runtime_settings(environ=environment_snapshot)
            password = read_database_runtime_password(database_settings)
        except ApplicationError as error:
            emit_emergency_application_error(ServiceName.API, context, error)
            return _EXIT_CONFIGURATION_FAILURE
        except Exception as error:
            emit_emergency_internal_error(ServiceName.API, context, error)
            return _EXIT_INTERNAL_FAILURE

        try:
            logger = configure_diagnostics(runtime_settings)
            lifecycle = DatabaseRuntimeLifecycle(database_settings, password)

            @asynccontextmanager
            async def database_lifespan(_app: FastAPI) -> AsyncIterator[None]:
                await lifecycle.start()
                try:
                    yield
                finally:
                    await lifecycle.stop()

            application = create_api_application(
                environment=api_settings.environment,
                readiness_probe=lifecycle,
                event_sink=logger,
                lifespan=database_lifespan,
            )
            server_config = uvicorn.Config(
                application,
                host=api_settings.host,
                port=api_settings.port,
                server_header=False,
                proxy_headers=False,
                access_log=False,
                reload=False,
                workers=1,
            )
            server = server_factory(server_config)
            server.run()
        except SystemExit as exit_request:
            # Uvicorn aborts low-level startup (for example a bind failure)
            # through ``sys.exit``; translate it into the documented exits
            # instead of letting the interpreter surface the raw code.
            if exit_request.code is not None and exit_request.code != 0:
                emit_emergency_internal_error(ServiceName.API, context, exit_request)
                return _EXIT_INTERNAL_FAILURE
        except Exception as error:
            emit_emergency_internal_error(ServiceName.API, context, error)
            return _EXIT_INTERNAL_FAILURE

    return _EXIT_CLEAN_SHUTDOWN
