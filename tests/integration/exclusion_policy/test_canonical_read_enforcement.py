"""Mandatory backend enforcement at the canonical-read boundary.

Proves spec 14 for reads over the real baseline: the guarded read service
resolves the active policy and the source state transactionally and re-checks
the resolved reference before any object GET; publishing a denying revision
turns the next read into the typed denial with zero object-store calls; a
workspace without a published policy denies as not-initialized; and tampered
signature material denies as signing-unavailable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness
from tests.integration.exclusion_policy.test_source_publication_enforcement import (
    PAYLOAD,
    EnforcementHarness,
    _context,
    _rule,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.sources.metrics import InMemoryCanonicalReadMetrics
from personal_os.sources.reading import CanonicalSourceReadService, ReadCurrentSourceCommand
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.tables import (
    content_objects,
    source_versions,
    sources,
    users,
    workspace_policy_state,
    workspaces,
)

pytestmark = pytest.mark.local_stack


def _read_service(harness: EnforcementHarness) -> CanonicalSourceReadService:
    return CanonicalSourceReadService(
        store=PostgresqlCanonicalSourceReadStore(
            harness.base.engine, policy_verifier=harness.policy_verifier
        ),
        object_store=harness.object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=compose_policy_enforcement(
            harness.base.engine, verifier=harness.policy_verifier
        ),
    )


@pytest.fixture(scope="module")
def read_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("read-enforcement-secrets")


@pytest_asyncio.fixture
async def read_harness(
    policy_migration_harness: PolicyMigrationHarness, read_secret_root: Path
) -> EnforcementHarness:
    harness = EnforcementHarness(policy_migration_harness, read_secret_root)
    await harness.ensure_keys_initialized()
    return harness


@pytest.mark.asyncio
async def test_read_allowed_under_empty_policy_returns_exact_bytes(
    read_harness: EnforcementHarness,
) -> None:
    await read_harness.publish_revision()
    published = await read_harness.publish_source(PAYLOAD)

    content = await _read_service(read_harness).read_current_source_bytes(
        ReadCurrentSourceCommand(
            workspace_id=read_harness.workspace_id, source_id=published.source_id
        ),
        _context(),
    )

    assert content == PAYLOAD
    assert read_harness.object_store.calls[-1] == "open_reader"


@pytest.mark.asyncio
async def test_read_after_exclusion_denies_before_any_object_get(
    read_harness: EnforcementHarness,
) -> None:
    await read_harness.publish_revision()
    published = await read_harness.publish_source(PAYLOAD)
    await read_harness.publish_revision(_rule(RuleKind.MEDIA_TYPE, "text/markdown"))
    calls_before = list(read_harness.object_store.calls)

    with pytest.raises(ExclusionPolicyError) as raised:
        await _read_service(read_harness).read_current_source_bytes(
            ReadCurrentSourceCommand(
                workspace_id=read_harness.workspace_id,
                source_id=published.source_id,
            ),
            _context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    # No object GET was issued after the exclusion took effect.
    assert read_harness.object_store.calls == calls_before


@pytest.mark.asyncio
async def test_read_without_published_policy_denies_not_initialized(
    read_harness: EnforcementHarness,
) -> None:
    workspace_id = await _seed_unpublished_workspace(read_harness)
    source_id = await _seed_source_with_version(read_harness, workspace_id)

    with pytest.raises(ExclusionPolicyError) as raised:
        await _read_service(read_harness).read_current_source_bytes(
            ReadCurrentSourceCommand(workspace_id=workspace_id, source_id=source_id),
            _context(),
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED


@pytest.mark.asyncio
async def test_read_with_tampered_signature_denies_signing_unavailable(
    read_harness: EnforcementHarness,
) -> None:
    from tests.integration.exclusion_policy.test_source_publication_enforcement import (
        _activate_forged_revision,
    )

    await read_harness.publish_revision()
    published = await read_harness.publish_source(PAYLOAD)
    # History is append-only, so the tamper model is a forged revision the
    # active pointer is moved onto; verification must fail closed.
    prior_pointer = await _activate_forged_revision(read_harness)

    with pytest.raises(ExclusionPolicyError) as raised:
        await _read_service(read_harness).read_current_source_bytes(
            ReadCurrentSourceCommand(
                workspace_id=read_harness.workspace_id,
                source_id=published.source_id,
            ),
            _context(),
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    del prior_pointer  # The forged revision stays; this test runs last.


async def _seed_unpublished_workspace(harness: EnforcementHarness) -> UUID:
    workspace_id = uuid4()
    owner_user_id = uuid4()
    nonce = uuid4().hex
    async with harness.base.engine.begin() as connection:
        await connection.execute(
            sa.insert(users).values(
                user_id=owner_user_id,
                username=f"read-owner-{nonce[:12]}",
                display_name="Read Enforcement Owner",
            )
        )
        await connection.execute(
            sa.insert(workspaces).values(
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
                workspace_key=f"ws-{nonce[:12]}",
                display_name="Read Enforcement Workspace",
            )
        )
        await connection.execute(
            sa.insert(workspace_policy_state).values(
                workspace_id=workspace_id,
                active_policy_revision_id=None,
                active_revision_number=0,
            )
        )
    return workspace_id


async def _seed_source_with_version(harness: EnforcementHarness, workspace_id: UUID) -> UUID:
    source_id = uuid4()
    content_object_id = uuid4()
    source_version_id = uuid4()
    content_hash = hashlib.sha256(f"read-enforcement-{uuid4().hex}".encode()).hexdigest()
    async with harness.base.engine.begin() as connection:
        await connection.execute(
            sa.insert(content_objects).values(
                content_object_id=content_object_id,
                content_hash=content_hash,
                object_key=(
                    f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"
                ),
                byte_size=22,
                media_type="text/markdown",
                # Database-owned time keeps the verification-window CHECK
                # stable against app/container clock skew.
                verified_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
            )
        )
        # The canonical create order mirrors the publication store: the
        # source row lands first with a null pointer, then the version row,
        # then the guarded pointer activation (the two composite foreign keys
        # reference each other).
        await connection.execute(
            sa.insert(sources).values(
                source_id=source_id,
                workspace_id=workspace_id,
                source_type="markdown",
                title="Read Enforcement Source",
            )
        )
        await connection.execute(
            sa.insert(source_versions).values(
                source_version_id=source_version_id,
                workspace_id=workspace_id,
                source_id=source_id,
                content_object_id=content_object_id,
                content_version=1,
                author_kind="user",
                author_id=workspace_id,
            )
        )
        activated = await connection.execute(
            sa.update(sources)
            .values(sync_state="active", current_version_id=source_version_id)
            .where(
                sources.c.source_id == source_id,
                sources.c.current_version_id.is_(None),
            )
        )
        assert activated.rowcount == 1
    return source_id
