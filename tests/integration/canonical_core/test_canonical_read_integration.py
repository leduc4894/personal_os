"""Canonical current-source read against a disposable PostgreSQL 18.4.

Every case runs against the real migrated baseline: the synthetic source is
created through the real publication service over the fake local-filesystem
object store, then read back through the real
:class:`PostgresqlCanonicalSourceReadStore` and
:class:`CanonicalSourceReadService` — the full create/read/replay cycle returns
the exact canonical bytes; a publication whose claimed digest disagrees with
the streamed bytes fails inside the object store and leaves no canonical row;
and a deleted source state fails closed as the typed read-state error.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from tests.integration.canonical_core.conftest import (
    CanonicalCoreHarness,
    single_chunk_stream,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.sources.reading import CanonicalReadStateError, ReadCurrentSourceCommand
from postgresql_source_store.tables import sources

pytestmark = pytest.mark.local_stack


@pytest.mark.asyncio
async def test_full_synthetic_source_create_read_replay_succeeds(
    canonical_core_harness: CanonicalCoreHarness,
) -> None:
    workspace = await canonical_core_harness.seed_workspace()
    payload = f"# Canonical core note\n\nrun-{uuid4()}\n".encode()
    published = await canonical_core_harness.publish_markdown_source(workspace, payload)
    command = published.command
    first = published.result

    assert first.outcome.value == "published"
    assert first.content_version == 1
    assert first.source_id == command.source_id

    reference = await canonical_core_harness.read_service.store.resolve_current(
        ReadCurrentSourceCommand(workspace_id=workspace.workspace_id, source_id=command.source_id),
        canonical_core_harness.diagnostic_context(),
    )
    assert reference.workspace_id == workspace.workspace_id
    assert reference.source_id == command.source_id
    assert reference.source_version_id == first.source_version_id
    assert reference.content_version == 1
    assert reference.expected_object.content_digest.hexadecimal == (
        first.content_digest.hexadecimal
    )
    assert reference.expected_object.size_bytes == len(payload)
    assert reference.expected_object.media_type.value == "text/markdown"
    assert reference.committed_at.tzinfo is not None
    assert reference.committed_at == first.committed_at

    read_command = ReadCurrentSourceCommand(
        workspace_id=workspace.workspace_id, source_id=command.source_id
    )
    served_bytes = await canonical_core_harness.read_service.read_current_source_bytes(
        read_command, canonical_core_harness.diagnostic_context()
    )
    assert served_bytes == payload

    replayed = await canonical_core_harness.publication_service.publish_create(
        command=command,
        stream=single_chunk_stream(payload),
        diagnostic_context=canonical_core_harness.diagnostic_context(),
    )
    assert replayed == first

    served_again = await canonical_core_harness.read_service.read_current_source_bytes(
        read_command, canonical_core_harness.diagnostic_context()
    )
    assert served_again == payload


@pytest.mark.asyncio
async def test_publication_claim_mismatch_leaves_no_canonical_row(
    canonical_core_harness: CanonicalCoreHarness,
) -> None:
    workspace = await canonical_core_harness.seed_workspace()
    claimed_payload = b"a" * 64
    streamed_payload = b"b" * 64
    command = canonical_core_harness.build_markdown_create_command(
        workspace, claimed_payload, title="Claim Mismatch Note"
    )
    counts_before = await canonical_core_harness.table_counts()

    with pytest.raises(ObjectStorageError) as captured:
        await canonical_core_harness.publication_service.publish_create(
            command=command,
            stream=single_chunk_stream(streamed_payload),
            diagnostic_context=canonical_core_harness.diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert await canonical_core_harness.table_counts() == counts_before
    async with canonical_core_harness.engine.connect() as connection:
        source_row = (
            await connection.execute(
                sa.select(sources.c.source_id).where(sources.c.source_id == command.source_id)
            )
        ).one_or_none()
    assert source_row is None


@pytest.mark.asyncio
async def test_deleted_source_state_fails_closed(
    canonical_core_harness: CanonicalCoreHarness,
) -> None:
    workspace = await canonical_core_harness.seed_workspace()
    published = await canonical_core_harness.publish_markdown_source(
        workspace, b"# Deleted source\n", title="Deleted Source Note"
    )
    async with canonical_core_harness.engine.begin() as connection:
        await connection.execute(
            sa.update(sources)
            .values(sync_state="deleted", deleted_at=sa.text("CURRENT_TIMESTAMP"))
            .where(sources.c.source_id == published.command.source_id)
        )

    read_command = ReadCurrentSourceCommand(
        workspace_id=workspace.workspace_id, source_id=published.command.source_id
    )
    with pytest.raises(CanonicalReadStateError) as captured:
        await canonical_core_harness.read_service.store.resolve_current(
            read_command, canonical_core_harness.diagnostic_context()
        )
    assert captured.value.error_code is ErrorCode.CANONICAL_READ_STATE_INVALID
    assert dict(captured.value.safe_details) == {"source_id": published.command.source_id}

    with pytest.raises(CanonicalReadStateError):
        await canonical_core_harness.read_service.read_current_source_bytes(
            read_command, canonical_core_harness.diagnostic_context()
        )
