"""API server runner: snapshot loading, diagnostics, lifespan and Uvicorn.

This module is the serve composition root. :func:`run_server` captures the
process environment exactly once at entry, loads the runtime, API, database
and authentication settings plus the secret-file password, loads the
versioned authentication keyring from its exact secret files (refusing with
the configuration exit before any socket exists when a key file is missing or
malformed), loads the exclusion-policy signing settings and their Ed25519
private key through the same secret-file boundary, loads the R2
object-storage settings and their two credential files through the same
boundary, configures structured diagnostics, builds the database lifecycle,
the web-authentication runtime, the exclusion-policy runtime, the
small-file sync runtime, the multipart upload runtime and the device sync
runtime over the real PostgreSQL
and R2 adapters (each R2 client opens lazily at the first store call inside
the serving loop), and
builds the FastAPI application (wiring the lifecycle into the application
lifespan so the engine starts on startup, the keyring-reference verification
refuses startup when PostgreSQL references a key ID the keyring omits, the
exclusion-policy signer proof refuses startup when the derived key ID is not
the current key of the latest canonical keyset, the R2 clients close on
shutdown, and the engine is disposed on shutdown), then runs
Uvicorn in single-process mode with the approved flags: no server header, no
proxy headers, no access log, no reload and exactly one worker.

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

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Final, Protocol

import uvicorn
from fastapi import FastAPI

from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    DatabaseAuthenticationClock,
    compose_web_authentication,
    verify_keyring_covers_required_key_ids,
)
from api_runtime.authentication_crypto import load_authentication_keyring
from api_runtime.authentication_settings import load_authentication_settings
from api_runtime.database_lifecycle import DatabaseRuntimeLifecycle
from api_runtime.device_sync_composition import compose_device_sync
from api_runtime.exclusion_policy_composition import compose_exclusion_policy
from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from api_runtime.exclusion_policy_settings import (
    load_exclusion_policy_signer,
    load_exclusion_policy_signing_settings,
)
from api_runtime.multipart_upload_composition import compose_multipart_upload
from api_runtime.server_settings import load_api_server_settings
from api_runtime.small_file_sync_composition import compose_small_file_sync
from api_runtime.source_lifecycle_composition import (
    PostgresqlSourceLifecyclePolicy,
    compose_source_lifecycle_runtime,
)
from personal_os.diagnostics.context import bind_diagnostic_context, create_diagnostic_context
from personal_os.diagnostics.logging import (
    configure_diagnostics,
    emit_emergency_application_error,
    emit_emergency_internal_error,
)
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.metrics import InMemoryExclusionPolicyMetrics
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import ServiceName
from personal_os.source_lifecycle.metrics import InMemorySourceLifecycleMetrics
from postgresql_source_store.engine import create_source_store_engine
from postgresql_source_store.lifecycle_store import PostgresqlSourceLifecycleStore
from postgresql_source_store.policy_enforcement import (
    PostgresqlActivePolicySnapshotSource,
    PostgresqlPolicySubjectEvidenceSource,
)
from postgresql_source_store.settings import (
    load_database_runtime_settings,
    read_database_runtime_password,
)
from r2_object_storage.settings import load_object_storage_settings

_EXIT_CONFIGURATION_FAILURE: Final[int] = 78
_EXIT_INTERNAL_FAILURE: Final[int] = 70
_EXIT_CLEAN_SHUTDOWN: Final[int] = 0


class RunnableUvicornServer(Protocol):
    """Structural contract of one runnable Uvicorn server object."""

    @property
    def config(self) -> uvicorn.Config: ...

    async def serve(self) -> None: ...


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
            authentication_settings = load_authentication_settings(environ=environment_snapshot)
            keyring = load_authentication_keyring(authentication_settings)
            policy_signing_settings = load_exclusion_policy_signing_settings(
                environ=environment_snapshot
            )
            policy_signer = load_exclusion_policy_signer(
                policy_signing_settings, secret_root=database_settings.secret_root
            )
            object_storage_settings, object_storage_credentials = load_object_storage_settings(
                environ=environment_snapshot
            )
        except ApplicationError as error:
            emit_emergency_application_error(ServiceName.API, context, error)
            return _EXIT_CONFIGURATION_FAILURE
        except Exception as error:
            emit_emergency_internal_error(ServiceName.API, context, error)
            return _EXIT_INTERNAL_FAILURE

        try:
            logger = configure_diagnostics(runtime_settings)
            # The engine opens no connection here: pools connect lazily, so the
            # same engine can serve the readiness probe, the authentication
            # transactions and the pre-bind keyring verification while the
            # lifecycle keeps owning its exactly-once disposal.
            engine = create_source_store_engine(database_settings, password)
            lifecycle = DatabaseRuntimeLifecycle(
                database_settings,
                password,
                engine_factory=lambda _settings, _password: engine,
            )
            web_authentication = compose_web_authentication(
                settings=authentication_settings,
                keyring=keyring,
                engine=engine,
            )
            # One shared in-memory policy metrics sink serves both composition
            # sites (spec 2026-08-24 C2): the enforcement evaluations of the
            # small-file graph and the publish outcomes of the policy graph
            # record into the same counters, which the Web Admin diagnostics
            # route then serves. A production sink replacing it implements the
            # same Protocol; the compositions' no-op fallback only applies
            # when no sink is bound at all.
            policy_metrics = InMemoryExclusionPolicyMetrics()
            exclusion_policy = compose_exclusion_policy(
                engine=engine, signer=policy_signer, metrics=policy_metrics
            )
            small_file_sync = compose_small_file_sync(
                engine=engine,
                signer=policy_signer,
                object_storage_settings=object_storage_settings,
                object_storage_credentials=object_storage_credentials,
                logger=logger,
                policy_metrics=policy_metrics,
            )
            # The multipart upload runtime owns its own lazy R2 client
            # manager (the staging provider, the exact staging read and the
            # canonical verification spool share it) so it stays
            # independently disposable through the same lifecycle; the
            # durable session store seals its raw operation tokens with the
            # authentication keyring the serve graph already loaded.
            multipart_upload = compose_multipart_upload(
                engine=engine,
                object_storage_settings=object_storage_settings,
                object_storage_credentials=object_storage_credentials,
                logger=logger,
                keyring=keyring,
                policy_metrics=policy_metrics,
            )
            lifecycle_metrics = InMemorySourceLifecycleMetrics()
            lifecycle_policy_verifier = TrustAnchorEd25519Verifier()
            source_lifecycle = compose_source_lifecycle_runtime(
                store=PostgresqlSourceLifecycleStore(
                    engine,
                    policy_verifier=lifecycle_policy_verifier,
                ),
                policy=PostgresqlSourceLifecyclePolicy(
                    snapshot_source=PostgresqlActivePolicySnapshotSource(engine),
                    evidence_source=PostgresqlPolicySubjectEvidenceSource(engine),
                    verifier=lifecycle_policy_verifier,
                ),
                metrics=lifecycle_metrics,
                web_authentication=web_authentication,
            )
            # The device sync runtime owns its own lazy R2 client manager so
            # the verified download reader stays independently disposable;
            # the client opens only at the first download inside the serving
            # loop and closes with the process on shutdown.
            device_sync = compose_device_sync(
                engine=engine,
                object_storage_settings=object_storage_settings,
                object_storage_credentials=object_storage_credentials,
                logger=logger,
            )

            @asynccontextmanager
            async def database_lifespan(_app: FastAPI) -> AsyncIterator[None]:
                # Uvicorn runs the lifespan startup before binding the
                # listening socket, so the keyring-reference refusal of spec
                # 20.1 and the exclusion-policy signer proof of spec 13.1
                # fail startup without ever exposing the socket.
                await lifecycle.start()
                try:
                    try:
                        await verify_keyring_covers_required_key_ids(
                            engine=engine,
                            keyring=keyring,
                            clock=DatabaseAuthenticationClock(engine),
                        )
                    except ApplicationError as error:
                        logger.emit_application_error(error)
                        raise
                    await lifecycle.verify_exclusion_policy_signer(
                        signing_key_id=policy_signer.key_id
                    )
                    yield
                finally:
                    if small_file_sync.aclose is not None:
                        await small_file_sync.aclose()
                    if multipart_upload.aclose is not None:
                        await multipart_upload.aclose()
                    if device_sync.aclose is not None:
                        await device_sync.aclose()
                    await lifecycle.stop()

            application = create_api_application(
                environment=api_settings.environment,
                readiness_probe=lifecycle,
                web_authentication=web_authentication,
                exclusion_policy=exclusion_policy,
                small_file_sync=small_file_sync,
                multipart_upload=multipart_upload,
                source_lifecycle=source_lifecycle,
                device_sync=device_sync,
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
            # psycopg async refuses the Windows Proactor loop, and Uvicorn's
            # own loop factory selects ProactorEventLoop on win32, so the
            # application (whose lifespan opens PostgreSQL connections) must
            # be served on an explicitly selected SelectorEventLoop instead
            # of through ``Server.run``.
            asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(server.serve())
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
