"""Downgrade of the grant-poll pacing bucket kind over real pacing rows.

The disposable stack is upgraded to the pacing head, one ``grant_poll``
bucket row is written through the real :meth:`DeviceAuthorizationStore.pace_grant_poll`
and one retained-kind row through SQL, and the gated downgrade then proves
the row guard: PostgreSQL validates a freshly added CHECK against existing
rows, so deleting the ``grant_poll`` rows BEFORE the six-value constraint is
re-created is what keeps the downgrade succeeding — retained-kind rows
survive, and a re-upgrade admits the pacing kind again with the behavior
still writing it (BACKLOG 2026-08-16 §13).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from postgresql_source_store.device_authorization_store import DeviceAuthorizationStore
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.settings import (
    DatabaseRuntimeSettings,
    load_database_runtime_settings,
)

pytestmark = pytest.mark.local_stack

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]
_DATABASE_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
_PACING_DIGEST = "a" * 64
_RETAINED_KIND_DIGEST = "b" * 64
_PRE_PACING_REVISION = "20260829_01"


def _run_alembic(
    arguments: list[str], alembic_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *arguments],
        cwd=str(_WORKTREE_ROOT),
        env=alembic_env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def pacing_head_stack(authentication_schema_stack: Any) -> Any:
    """Upgrade the disposable stack to the pacing head once per module."""
    upgrade = _run_alembic(["upgrade", "head"], authentication_schema_stack.alembic_env)
    assert upgrade.returncode == 0, f"alembic upgrade head failed: {upgrade.stdout}{upgrade.stderr}"
    return authentication_schema_stack


@pytest_asyncio.fixture
async def pacing_store_engine(pacing_head_stack: Any) -> AsyncEngine:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=pacing_head_stack.alembic_env
    )
    password = SecretStr(pacing_head_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    try:
        yield engine
    finally:
        await dispose_source_store_engine(engine)


async def _seed_one_grant_poll_row(engine: AsyncEngine) -> None:
    """Write exactly one grant_poll bucket row through the real pacing."""
    store = DeviceAuthorizationStore(engine)
    assert (
        await store.pace_grant_poll(
            polling_credential_hash=_PACING_DIGEST, database_now=_DATABASE_NOW
        )
        is None
    )
    too_fast = await store.pace_grant_poll(
        polling_credential_hash=_PACING_DIGEST, database_now=_DATABASE_NOW
    )
    assert too_fast is not None and too_fast >= 1


def _bucket_count(pacing_head_stack: Any, bucket_kind: str) -> int:
    with pacing_head_stack.connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM knowledge.authentication_throttle_buckets WHERE bucket_kind = %s",
            (bucket_kind,),
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_downgrade_deletes_grant_poll_rows_and_keeps_retained_kinds(
    pacing_head_stack: Any, pacing_store_engine: AsyncEngine
) -> None:
    await _seed_one_grant_poll_row(pacing_store_engine)
    with pacing_head_stack.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO knowledge.authentication_throttle_buckets "
            "(throttle_bucket_id, bucket_kind, bucket_hash, window_started_at, "
            "failed_attempt_count, locked_until, updated_at) "
            "VALUES (%s, 'login_source', %s, %s, 1, NULL, %s)",
            (
                uuid4(),
                _RETAINED_KIND_DIGEST,
                _DATABASE_NOW,
                _DATABASE_NOW + timedelta(seconds=1),
            ),
        )
    assert _bucket_count(pacing_head_stack, "grant_poll") == 1
    assert _bucket_count(pacing_head_stack, "login_source") == 1

    downgrade = _run_alembic(
        ["-x", "allow_destructive=true", "downgrade", _PRE_PACING_REVISION],
        pacing_head_stack.alembic_env,
    )
    assert downgrade.returncode == 0, (
        f"downgrade with a leftover grant_poll row failed: {downgrade.stdout}{downgrade.stderr}"
    )
    assert _bucket_count(pacing_head_stack, "grant_poll") == 0
    assert _bucket_count(pacing_head_stack, "login_source") == 1

    re_upgrade = _run_alembic(["upgrade", "head"], pacing_head_stack.alembic_env)
    assert re_upgrade.returncode == 0, (
        f"re-upgrade after the guarded downgrade failed: {re_upgrade.stdout}{re_upgrade.stderr}"
    )
    await _seed_one_grant_poll_row(pacing_store_engine)
    assert _bucket_count(pacing_head_stack, "grant_poll") == 1
