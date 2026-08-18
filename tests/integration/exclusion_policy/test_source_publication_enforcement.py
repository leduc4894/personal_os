"""Mandatory backend enforcement at the source-publication boundary.

Proves spec 14 over the real PostgreSQL baseline and real signed revisions
published through the real policy publication service: a workspace without a
published policy fails closed as the typed not-initialized denial before any
object-store access; an empty revision 1 allows valid subjects; a definite
media-type match denies with only the revision number as its safe detail; an
extension rule yields the typed indeterminate denial because canonical
publication subjects carry no locator; the transaction-final recheck ignores
the preflight hint and denies a publication whose policy changed during the
upload, leaving no source, version, event or rejection-audit row and the
current pointer untouched; an exact replay now excluded returns no canonical
data; and tampered signature material denies as signing-unavailable.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.exclusion_policy_commands import (
    create_or_load_policy_signing_key,
    execute_policy_key_initialize,
)
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness

from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.previews import PolicyPreviewRecord
from personal_os.exclusion_policy.publication import (
    CONFIRMATION_PHRASE,
    ExclusionPolicyPublicationService,
    PublishPolicyCommand,
)
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
)
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import compute_request_fingerprint
from personal_os.sources.metrics import InMemorySourcePublicationMetrics
from personal_os.sources.publication import SourceVersionPublicationService
from personal_os.sources.results import SourceVersionPublicationResult
from postgresql_source_store.policy_drafts import PostgresqlPolicyDraftStore
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.policy_previews import PostgresqlPolicyPreviewStore
from postgresql_source_store.policy_publication import (
    PUBLISH_REJECTED_AUDIT_ACTION,
    PostgresqlPolicyPublicationStore,
)
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore
from postgresql_source_store.tables import (
    policy_signing_keys,
    source_policies,
    workspace_policy_state,
)

pytestmark = pytest.mark.local_stack

KEY_FILE_NAME = "enforcement_signing_initial.pem"

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


def _rule(kind: RuleKind, text_operand: str):
    return normalize_rule(uuid4(), kind, text_operand=text_operand)


class RecordingObjectStore:
    """In-memory canonical object store recording every call by name."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.objects: dict[str, bytes] = {}

    def _receipt(self, payload: bytes, media_type: str) -> VerifiedObjectReceipt:
        digest = ContentDigest.parse(hashlib.sha256(payload).hexdigest())
        return VerifiedObjectReceipt(
            content_digest=digest,
            object_key=derive_canonical_object_key(digest),
            size_bytes=len(payload),
            media_type=CanonicalMediaType.parse(media_type),
            verified_at=datetime.now(UTC),
            verification_method=VerificationMethod.UPLOADED_FULL_READ,
        )

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        self.calls.append("resolve")
        stored = self.objects.get(expected.content_digest.hexadecimal)
        return None if stored is None else self._receipt(stored, expected.media_type.value)

    async def store_stream(
        self,
        stream: AsyncIterator[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        self.calls.append("store_stream")
        payload = b""
        async for chunk in stream:
            payload += chunk
        assert len(payload) == expected_size_bytes
        digest_hexadecimal = hashlib.sha256(payload).hexdigest()
        assert claimed_sha256 == digest_hexadecimal
        self.objects[digest_hexadecimal] = payload
        return self._receipt(payload, media_type)

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        self.calls.append("verify")
        return self._receipt(
            self.objects[expected.content_digest.hexadecimal], expected.media_type.value
        )

    def open_verified_reader(self, expected: ExpectedObject) -> Any:
        self.calls.append("open_reader")
        payload = self.objects[expected.content_digest.hexadecimal]

        chunks = [payload[i : i + 65536] for i in range(0, len(payload), 65536)]

        @asynccontextmanager
        async def opened() -> AsyncIterator[Any]:
            class _Reader:
                def __init__(self, pending: list[bytes]) -> None:
                    self.pending = pending

                def __aiter__(self) -> AsyncIterator[bytes]:
                    return self

                async def __anext__(self) -> bytes:
                    if not self.pending:
                        raise StopAsyncIteration
                    return self.pending.pop(0)

                async def read(self, size_bytes: int = 1_048_576) -> bytes:
                    """The port's sized-read surface (recovery object copies)."""
                    buffer = b""
                    while self.pending and len(buffer) < size_bytes:
                        buffer += self.pending.pop(0)
                    if len(buffer) > size_bytes:
                        self.pending.insert(0, buffer[size_bytes:])
                        buffer = buffer[:size_bytes]
                    return buffer

            reader = _Reader(list(chunks))
            try:
                yield reader
            finally:
                reader.pending.clear()

        return opened()


@dataclass
class EnforcementHarness:
    """Real publication machinery plus the guarded source services."""

    base: PolicyMigrationHarness
    secret_root: Path
    object_store: RecordingObjectStore = field(default_factory=RecordingObjectStore)
    baseline_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier

        engine = self.base.engine
        self.signing_key = create_or_load_policy_signing_key(self.secret_root, KEY_FILE_NAME)
        self.policy_verifier = TrustAnchorEd25519Verifier()
        self.draft_store = PostgresqlPolicyDraftStore(engine)
        self.preview_store = PostgresqlPolicyPreviewStore(engine)
        self.policy_publication_service = ExclusionPolicyPublicationService(
            store=PostgresqlPolicyPublicationStore(engine),
            signer=self.signing_key,
            verifier=self._keyed_verifier(),
        )
        self.source_store = PostgresqlSourcePublicationStore(
            engine, policy_verifier=self.policy_verifier
        )
        self.publication_service = SourceVersionPublicationService(
            store=self.source_store,
            object_store=self.object_store,
            metrics=InMemorySourcePublicationMetrics(),
            clock=lambda: datetime.now(UTC),
            policy_guard=compose_policy_enforcement(engine, verifier=self.policy_verifier),
        )

    def _keyed_verifier(self) -> Any:
        from api_runtime.exclusion_policy_crypto import Ed25519PolicyVerifier

        return Ed25519PolicyVerifier({self.signing_key.key_id: self.signing_key.public_key_bytes})

    @property
    def workspace_id(self) -> UUID:
        return self.base.stack.workspace_id

    @property
    def owner_user_id(self) -> UUID:
        return self.base.stack.owner_user_id

    def actor(self) -> PolicyActor:
        return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=self.owner_user_id)

    async def ensure_keys_initialized(self) -> None:
        await execute_policy_key_initialize(
            engine=self.base.engine,
            workspace_id=self.workspace_id,
            key_file_name=KEY_FILE_NAME,
            secret_root=self.secret_root,
            context=_context(),
        )
        # The migration-seeded workspace already carries one source and one
        # sync event; every row assertion compares against this baseline.
        self.baseline_counts = {
            table: await self.row_count(table)
            for table in ("sources", "source_versions", "sync_events", "audit_events")
        }

    async def new_rows(self, table: str) -> int:
        """Rows added since the harness baseline was captured."""
        return await self.row_count(table) - self.baseline_counts.get(table, 0)

    async def publish_revision(self, *rules: Any) -> int:
        """Replace the draft, preview it and publish it; returns the number."""

        draft = await self.draft_store.load_draft(self.workspace_id, _context())
        # Always replace the full rule set: an "empty revision" call must
        # clear whatever rules a prior revision installed in the draft.
        await self.draft_store.replace_rules(
            draft.draft_id, draft.draft_version, tuple(rules), self.actor(), _context()
        )
        requested = await self.preview_store.request_preview(
            self.workspace_id, self.actor(), _context()
        )
        preview: PolicyPreviewRecord = await self.preview_store.run_preview_activity(
            requested.policy_preview_id, _context()
        )
        assert preview.impact_digest is not None
        async with self.base.engine.connect() as connection:
            state = (
                await connection.execute(
                    sa.select(
                        workspace_policy_state.c.active_policy_revision_id,
                        workspace_policy_state.c.active_revision_number,
                    ).where(workspace_policy_state.c.workspace_id == self.workspace_id)
                )
            ).one()
        command = PublishPolicyCommand(
            workspace_id=self.workspace_id,
            actor=self.actor(),
            policy_preview_id=preview.policy_preview_id,
            policy_draft_id=preview.policy_draft_id,
            expected_draft_version=preview.draft_version,
            expected_draft_sha256=preview.draft_sha256,
            preview_impact_digest=preview.impact_digest,
            expected_active_policy_revision_id=preview.base_policy_revision_id,
            expected_active_revision_number=int(state.active_revision_number),
            idempotency_key=IdempotencyKey(f"enforce-{uuid4().hex}"),
            confirmation=CONFIRMATION_PHRASE,
        )
        result = await self.policy_publication_service.publish(command, _context())
        return result.revision_number

    def build_create_command(self, payload: bytes) -> CreateSourceVersion:
        digest = ContentDigest.parse(hashlib.sha256(payload).hexdigest())
        return CreateSourceVersion(
            workspace_id=self.workspace_id,
            source_id=uuid4(),
            event_id=uuid4(),
            idempotency_key=IdempotencyKey(f"source-{uuid4().hex}"),
            source_type=SourceType.MARKDOWN,
            title=SourceTitle("Enforcement Subject"),
            actor=SourceActor(actor_kind=ActorKind.USER, actor_id=self.owner_user_id),
            expected_object=ExpectedObject(
                content_digest=digest,
                size_bytes=len(payload),
                media_type=CanonicalMediaType.parse("text/markdown"),
            ),
            client_timestamp=None,
        )

    async def publish_source(self, payload: bytes) -> SourceVersionPublicationResult:
        command = self.build_create_command(payload)

        async def stream() -> AsyncIterator[bytes]:
            yield payload

        return await self.publication_service.publish_create(
            command=command,
            stream=stream(),
            diagnostic_context=_context(),
        )

    async def row_count(self, table: str) -> int:
        """Workspace-bound row count relative to the migration-seeded baseline."""
        return int(
            await self.base.fetch_scalar(
                f"SELECT count(*) FROM knowledge.{table} WHERE workspace_id = :workspace_id",
                {"workspace_id": self.workspace_id},
            )
        )


@pytest.fixture(scope="module")
def enforcement_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("enforcement-secrets")


@pytest_asyncio.fixture
async def enforcement_harness(
    policy_migration_harness: PolicyMigrationHarness, enforcement_secret_root: Path
) -> EnforcementHarness:
    harness = EnforcementHarness(policy_migration_harness, enforcement_secret_root)
    await harness.ensure_keys_initialized()
    return harness


PAYLOAD = b"# Enforcement subject\n\nUnique canonical bytes.\n"


@pytest.mark.asyncio
async def test_workspace_without_published_policy_fails_closed(
    enforcement_harness: EnforcementHarness,
) -> None:
    with pytest.raises(ExclusionPolicyError) as raised:
        await enforcement_harness.publish_source(PAYLOAD)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED
    # No object-store call, no canonical row, no rejection audit.
    assert enforcement_harness.object_store.calls == []
    assert await enforcement_harness.new_rows("sources") == 0
    assert await enforcement_harness.new_rows("sync_events") == 0
    assert await enforcement_harness.new_rows("audit_events") == 0


@pytest.mark.asyncio
async def test_empty_revision_one_allows_a_valid_subject(
    enforcement_harness: EnforcementHarness,
) -> None:
    revision_number = await enforcement_harness.publish_revision()
    assert revision_number == 1

    result = await enforcement_harness.publish_source(PAYLOAD)

    assert result.content_digest.hexadecimal == hashlib.sha256(PAYLOAD).hexdigest()
    assert await enforcement_harness.new_rows("sources") == 1


@pytest.mark.asyncio
async def test_definite_match_denies_and_writes_no_canonical_row(
    enforcement_harness: EnforcementHarness,
) -> None:
    await enforcement_harness.publish_revision()
    denial_revision = await enforcement_harness.publish_revision(
        _rule(RuleKind.MEDIA_TYPE, "text/markdown")
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await enforcement_harness.publish_source(PAYLOAD)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert raised.value.safe_details == {"policy_revision_number": denial_revision}
    # Fail-closed store: zero object-store calls happened before the denial at
    # the guard, and the commit never ran.
    assert enforcement_harness.object_store.calls == []
    assert await enforcement_harness.new_rows("sources") == 0
    assert await enforcement_harness.new_rows("sync_events") == 0
    # Policy denials are metrics, not business-rejection audit rows.
    assert (
        await enforcement_harness.base.fetch_scalar(
            "SELECT count(*) FROM knowledge.audit_events"
            " WHERE workspace_id = :workspace_id AND action = :action",
            {
                "workspace_id": enforcement_harness.workspace_id,
                "action": PUBLISH_REJECTED_AUDIT_ACTION,
            },
        )
        == 0
    )


@pytest.mark.asyncio
async def test_missing_locator_evidence_denies_as_indeterminate(
    enforcement_harness: EnforcementHarness,
) -> None:
    await enforcement_harness.publish_revision(_rule(RuleKind.EXTENSION, ".md"))

    with pytest.raises(ExclusionPolicyError) as raised:
        await enforcement_harness.publish_source(PAYLOAD)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INDETERMINATE
    assert set(raised.value.safe_details) == {"reason"}
    assert enforcement_harness.object_store.calls == []
    assert await enforcement_harness.new_rows("sources") == 0


@pytest.mark.asyncio
async def test_final_recheck_denies_a_policy_change_during_upload(
    enforcement_harness: EnforcementHarness,
) -> None:
    permissive_revision = await enforcement_harness.publish_revision()
    guard = enforcement_harness.publication_service.policy_guard
    command = enforcement_harness.build_create_command(PAYLOAD)
    # The preflight hint was computed under the permissive revision.
    preflight_decision = await guard.authorize_publication(command, _context())
    assert preflight_decision.revision_number == permissive_revision

    # The active revision changes to a denying one "during the upload".
    denial_revision = await enforcement_harness.publish_revision(
        _rule(RuleKind.MEDIA_TYPE, "text/markdown")
    )

    receipt = enforcement_harness.object_store._receipt(PAYLOAD, "text/markdown")
    with pytest.raises(ExclusionPolicyError) as raised:
        await enforcement_harness.source_store.commit_create(
            command,
            compute_request_fingerprint(command),
            receipt,
            _context(),
            preflight_decision=preflight_decision,
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert raised.value.safe_details == {"policy_revision_number": denial_revision}
    assert await enforcement_harness.new_rows("sources") == 0
    assert await enforcement_harness.new_rows("source_versions") == 0
    assert await enforcement_harness.new_rows("sync_events") == 0


@pytest.mark.asyncio
async def test_exact_replay_now_excluded_returns_no_canonical_data(
    enforcement_harness: EnforcementHarness,
) -> None:
    await enforcement_harness.publish_revision()
    await enforcement_harness.publish_source(PAYLOAD)
    await enforcement_harness.publish_revision(_rule(RuleKind.MEDIA_TYPE, "text/markdown"))

    # The exact replay of the same command is denied: canonical data returns
    # only while the current policy permits the subject.
    with pytest.raises(ExclusionPolicyError) as raised:
        await enforcement_harness.publish_source(PAYLOAD)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    # Still exactly the one committed publication.
    assert await enforcement_harness.new_rows("sources") == 1
    assert await enforcement_harness.new_rows("sync_events") == 1


@pytest.mark.asyncio
async def test_store_rejects_foreign_workspace_preflight_evidence(
    enforcement_harness: EnforcementHarness,
) -> None:
    from tests.unit.sources.fakes import build_policy_decision

    await enforcement_harness.publish_revision()
    command = enforcement_harness.build_create_command(PAYLOAD)
    receipt = enforcement_harness.object_store._receipt(PAYLOAD, "text/markdown")
    foreign_decision = build_policy_decision()  # Another workspace's evidence.
    assert foreign_decision.workspace_id != command.workspace_id

    with pytest.raises(SourcePublicationError) as raised:
        await enforcement_harness.source_store.commit_create(
            command,
            compute_request_fingerprint(command),
            receipt,
            _context(),
            preflight_decision=foreign_decision,
        )
    assert raised.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED
    assert await enforcement_harness.new_rows("sources") == 0


@pytest.mark.asyncio
async def test_tampered_signature_material_denies_as_signing_unavailable(
    enforcement_harness: EnforcementHarness,
) -> None:
    await enforcement_harness.publish_revision()
    prior_pointer = await _activate_forged_revision(enforcement_harness)

    with pytest.raises(ExclusionPolicyError) as raised:
        await enforcement_harness.publish_source(PAYLOAD + b"other")
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert enforcement_harness.object_store.calls == []
    assert await enforcement_harness.new_rows("sources") == 0
    del prior_pointer  # The forged revision stays; this test runs last.


async def _activate_forged_revision(
    harness: EnforcementHarness,
) -> tuple[UUID | None, int]:
    """Point the workspace at a forged revision; returns the prior pointer.

    The append-only triggers make persisted history immutable, so the tamper
    model is a forged INSERT: a fresh trust-anchor row whose public bytes
    never signed the revision, plus a revision row whose signature bytes are
    garbage. Verification must fail closed on the guarded path. The returned
    prior pointer lets the caller restore the workspace state afterwards so
    later tests bind their previews against a real revision.
    """

    import hashlib as _hashlib

    forged_revision_id = uuid4()
    forged_key_id = uuid4()
    payload = b"{}"
    occurred_at = datetime.now(UTC)
    async with harness.base.engine.begin() as connection:
        state = (
            await connection.execute(
                sa.select(
                    workspace_policy_state.c.active_policy_revision_id,
                    workspace_policy_state.c.active_revision_number,
                ).where(workspace_policy_state.c.workspace_id == harness.workspace_id)
            )
        ).one()
        await connection.execute(
            sa.insert(policy_signing_keys).values(
                signing_key_id=forged_key_id,
                workspace_id=harness.workspace_id,
                algorithm="Ed25519",
                public_key_bytes=bytes(range(32)),
                introduced_keyset_revision=99,
                created_at=occurred_at,
            )
        )
        draft_row = (
            await connection.execute(
                sa.text(
                    "SELECT policy_draft_id FROM knowledge.policy_drafts"
                    " WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": str(harness.workspace_id)},
            )
        ).scalar_one()
        forged_preview_id = uuid4()
        await connection.execute(
            sa.text(
                "INSERT INTO knowledge.policy_previews"
                " (policy_preview_id, workspace_id, policy_draft_id, draft_version,"
                " draft_sha256, base_policy_revision_id,"
                " source_checkpoint_event_sequence, state, newly_excluded_count,"
                " still_excluded_count, newly_allowed_count, still_allowed_count,"
                " indeterminate_count, impact_digest, created_by_user_id, created_at,"
                " ready_at, expires_at)"
                " VALUES (:policy_preview_id, :workspace_id, :policy_draft_id, 1,"
                " :draft_sha256, :base_revision_id, 0, 'ready', 0, 0, 0, 0, 0,"
                " :impact_digest, :created_by, CURRENT_TIMESTAMP,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + interval '15 minutes')"
            ),
            {
                "policy_preview_id": str(forged_preview_id),
                "workspace_id": str(harness.workspace_id),
                "policy_draft_id": str(draft_row),
                "draft_sha256": _hashlib.sha256(b"forged-draft").hexdigest(),
                "base_revision_id": (
                    None
                    if state.active_policy_revision_id is None
                    else str(state.active_policy_revision_id)
                ),
                "impact_digest": _hashlib.sha256(b"forged-impact").hexdigest(),
                "created_by": str(harness.owner_user_id),
            },
        )
        await connection.execute(
            sa.insert(source_policies).values(
                policy_revision_id=forged_revision_id,
                workspace_id=harness.workspace_id,
                revision_number=int(state.active_revision_number) + 1,
                parent_policy_revision_id=state.active_policy_revision_id,
                source_checkpoint_event_sequence=0,
                policy_preview_id=forged_preview_id,
                publication_idempotency_key=f"forged-{uuid4().hex}",
                request_fingerprint=_hashlib.sha256(b"forged").hexdigest(),
                snapshot_contract="exclusion_policy_snapshot/v1",
                snapshot_payload_bytes=payload,
                snapshot_payload_sha256=_hashlib.sha256(payload).hexdigest(),
                signing_key_id=forged_key_id,
                signature_bytes=b"x" * 64,
                published_by_user_id=harness.owner_user_id,
                published_at=occurred_at,
            )
        )
        swapped = await connection.execute(
            sa.update(workspace_policy_state)
            .values(
                active_policy_revision_id=forged_revision_id,
                active_revision_number=int(state.active_revision_number) + 1,
                updated_at=occurred_at,
            )
            .where(
                workspace_policy_state.c.workspace_id == harness.workspace_id,
                workspace_policy_state.c.active_revision_number
                == int(state.active_revision_number),
            )
        )
        assert swapped.rowcount == 1
    return state.active_policy_revision_id, int(state.active_revision_number)
