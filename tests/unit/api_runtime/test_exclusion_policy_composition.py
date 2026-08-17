"""Exclusion-policy runtime composition: offline determinism and serve wiring.

The offline composition is the deterministic double the OpenAPI export and
the route tests consume: fixed identities, one seeded self-signed keyset
revision and no database, key file or environment read. The serve
composition builds the real service graph over the shared engine — drafts,
previews, publication and the plugin/query reads — and constructs without
opening a connection. The query service owns the keyset page bound: it
fetches one row beyond the page maximum so ``has_more`` is exact, and the
page maximum is the spec value 16.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID

import pytest
from api_runtime.exclusion_policy_composition import (
    KEYSET_PAGE_MAXIMUM,
    OfflineExclusionPolicyState,
    PolicyKeysetPage,
    PolicyQueryService,
    compose_exclusion_policy,
    compose_offline_exclusion_policy,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.exclusion_policy.drafts import PolicyDraftService
from personal_os.exclusion_policy.ports import PolicyKeysetRecord
from personal_os.exclusion_policy.previews import PolicyPreviewService
from personal_os.exclusion_policy.publication import ExclusionPolicyPublicationService
from personal_os.exclusion_policy.signatures import (
    build_keyset_payload,
    compute_payload_sha256_hex,
)

_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")


def _context():
    return create_diagnostic_context().context


def _keyset_record(revision: int) -> PolicyKeysetRecord:
    payload = build_keyset_payload(
        workspace_id=_WORKSPACE_ID,
        keyset_revision=revision,
        parent_keyset_revision=None if revision == 1 else revision - 1,
        created_at=_FIXED_NOW,
        keys=(),
    )
    return PolicyKeysetRecord(
        policy_keyset_id=UUID(int=revision),
        workspace_id=_WORKSPACE_ID,
        keyset_revision=revision,
        parent_keyset_revision=None if revision == 1 else revision - 1,
        canonical_payload_bytes=payload,
        payload_sha256=compute_payload_sha256_hex(payload),
        keys=(),
        signatures=(),
        created_by_user_id=None,
        created_at=_FIXED_NOW,
    )


def test_offline_composition_builds_the_four_services() -> None:
    runtime = compose_offline_exclusion_policy()
    assert isinstance(runtime.drafts, PolicyDraftService)
    assert isinstance(runtime.previews, PolicyPreviewService)
    assert isinstance(runtime.publication, ExclusionPolicyPublicationService)
    assert isinstance(runtime.queries, PolicyQueryService)


@pytest.mark.asyncio
async def test_offline_composition_is_deterministic_across_invocations() -> None:
    left = compose_offline_exclusion_policy()
    right = compose_offline_exclusion_policy()
    left_page = await left.queries.list_keyset_page(_WORKSPACE_ID, 0, _context())
    right_page = await right.queries.list_keyset_page(_WORKSPACE_ID, 0, _context())
    assert left_page.has_more is False
    assert [row.keyset_revision for row in left_page.keysets] == [1]
    assert left_page.keysets[0].canonical_payload_bytes == (
        right_page.keysets[0].canonical_payload_bytes
    )
    assert left_page.keysets[0].payload_sha256 == right_page.keysets[0].payload_sha256


@pytest.mark.asyncio
async def test_query_service_slices_the_bounded_ordered_page() -> None:
    state = OfflineExclusionPolicyState()
    state.keyset_rows.extend(_keyset_record(revision) for revision in range(2, 22))
    runtime = compose_offline_exclusion_policy(state=state)
    context = _context()

    first = await runtime.queries.list_keyset_page(_WORKSPACE_ID, 0, context)
    assert isinstance(first, PolicyKeysetPage)
    assert len(first.keysets) == KEYSET_PAGE_MAXIMUM
    assert [row.keyset_revision for row in first.keysets] == list(range(1, 17))
    assert first.has_more is True

    tail = await runtime.queries.list_keyset_page(_WORKSPACE_ID, 16, context)
    assert [row.keyset_revision for row in tail.keysets] == [17, 18, 19, 20, 21]
    assert tail.has_more is False

    empty = await runtime.queries.list_keyset_page(_WORKSPACE_ID, 21, context)
    assert empty.keysets == ()
    assert empty.has_more is False


@pytest.mark.asyncio
async def test_query_service_status_combines_draft_and_reconciliation() -> None:
    state = OfflineExclusionPolicyState()
    runtime = compose_offline_exclusion_policy(state=state)
    context = _context()
    status = await runtime.queries.get_policy_status(_WORKSPACE_ID, context)
    assert status.active_policy_revision_id is None
    assert status.active_revision_number == 0
    assert status.draft.draft_version == 1
    assert await runtime.queries.get_reconciliation_summary(_WORKSPACE_ID, context) is None
    assert await runtime.queries.load_active_snapshot(_WORKSPACE_ID, context) is None


def test_serve_composition_constructs_over_the_engine_without_io() -> None:
    from api_runtime.exclusion_policy_crypto import Ed25519PolicySigner

    signer = Ed25519PolicySigner.from_seed_bytes(bytes(range(32)))
    runtime = compose_exclusion_policy(engine=cast("AsyncEngine", object()), signer=signer)
    assert isinstance(runtime.drafts, PolicyDraftService)
    assert isinstance(runtime.previews, PolicyPreviewService)
    assert isinstance(runtime.publication, ExclusionPolicyPublicationService)
    assert isinstance(runtime.queries, PolicyQueryService)
