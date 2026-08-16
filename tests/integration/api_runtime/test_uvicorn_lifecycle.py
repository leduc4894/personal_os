"""Uvicorn server lifecycle integration: loopback socket, liveness, stop-once.

One real ``uvicorn.Server.serve(sockets=[...])`` task serves the composed
application on an OS-assigned loopback listener (never ``0.0.0.0``): the
application lifespan starts and stops a recording
:class:`~api_runtime.database_lifecycle.DatabaseRuntimeLifecycle` double, an
HTTPX client proves the liveness envelope over real TCP, ``should_exit``
triggers the graceful shutdown, and the awaited task exit proves the lifecycle
stopped exactly once. The whole case runs under one five-second deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import httpx
import pytest
import uvicorn
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from fastapi import FastAPI

from personal_os.runtime_configuration.models import RuntimeEnvironment

_TEST_DEADLINE_SECONDS: Final[float] = 5.0
_STARTUP_POLL_INTERVAL_SECONDS: Final[float] = 0.02
_LISTENER_BACKLOG: Final[int] = 128
_LOOPBACK_HOST: Final[str] = "127.0.0.1"


@dataclass
class RecordingDatabaseLifecycle:
    """Database lifecycle double recording exactly-once start and stop counts."""

    start_count: int = 0
    stop_count: int = 0

    async def start(self) -> None:
        self.start_count += 1

    async def stop(self) -> None:
        self.stop_count += 1

    async def check(self) -> None:
        return None


@pytest.mark.asyncio
async def test_server_serves_liveness_and_stops_lifecycle_once() -> None:
    lifecycle = RecordingDatabaseLifecycle()

    @asynccontextmanager
    async def database_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await lifecycle.start()
        try:
            yield
        finally:
            await lifecycle.stop()

    application = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=lifecycle,
        web_authentication=compose_offline_web_authentication(),
        lifespan=database_lifespan,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=_LOOPBACK_HOST,
            port=0,
            server_header=False,
            proxy_headers=False,
            reload=False,
            workers=1,
            log_level="warning",
        )
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((_LOOPBACK_HOST, 0))
    listener.listen(_LISTENER_BACKLOG)
    host, port = listener.getsockname()
    assert host == _LOOPBACK_HOST
    serve_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(_TEST_DEADLINE_SECONDS):
            while not server.started:
                if serve_task.done():
                    serve_task.result()
                    pytest.fail("uvicorn server task exited before startup completed")
                await asyncio.sleep(_STARTUP_POLL_INTERVAL_SECONDS)
            async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
                response = await client.get("/api/health/live")
            assert response.status_code == 200
            assert response.json()["data"] == {"status": "live", "service": "api"}
            assert "server" not in response.headers
            server.should_exit = True
            await serve_task
    finally:
        server.should_exit = True
        if not serve_task.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(serve_task, timeout=_TEST_DEADLINE_SECONDS)
        if not serve_task.done():
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task
        listener.close()

    assert serve_task.done()
    assert serve_task.exception() is None
    assert lifecycle.start_count == 1
    assert lifecycle.stop_count == 1
