"""Composition of the exclusion-policy runtime: serve graph and offline graph.

:func:`compose_exclusion_policy` builds the real service graph the serve
process runs: the PostgreSQL draft/query, preview and publication stores over
the shared engine, the Task 5 Ed25519 signer with a verifier bound to its own
derived public key, and the plugin/query read adapter that pages the
append-only keyset chain, loads the active signed snapshot and reads the
latest reconciliation intent — all in bounded read transactions with the
shared policy retry policy.

:func:`compose_offline_exclusion_policy` builds the deterministic offline
graph used by the OpenAPI export and by unit tests: fixed identities, one
seeded self-signed keyset revision 1, in-memory draft/preview/publication
state and the real domain services over them. It reads no environment value,
no secret file and no database, so the offline contract document stays
byte-deterministic while route tests can pre-seed or restamp rows through the
public containers of :class:`OfflineExclusionPolicyState`.

The runtime's ``queries`` member is :class:`PolicyQueryService`, the Admin and
plugin read service over one :class:`PolicyQueryStore` and one
:class:`PolicyPluginReadPort`; it owns the keyset page bound of spec 13.3 —
at most 16 ordered envelopes per page — by fetching one row beyond the bound
so the continuation flag is exact.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, Protocol
from uuid import UUID, uuid5, uuid7

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from api_runtime.exclusion_policy_crypto import (
    Ed25519PolicySigner,
    Ed25519PolicyVerifier,
)
from api_runtime.exclusion_policy_models import (
    ActivePolicySnapshot,
    PolicyReconciliationSummary,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import ExclusionRule
from personal_os.exclusion_policy.drafts import (
    PolicyDraftService,
    compute_draft_semantic_sha256,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import ExclusionPolicyMetrics
from personal_os.exclusion_policy.ports import (
    PolicyActor,
    PolicyDraft,
    PolicyKeysetRecord,
    PolicyKeysetSignatureRecord,
    PolicyQueryStore,
    PolicySigningKeyRecord,
    PolicyStatus,
)
from personal_os.exclusion_policy.previews import (
    PREVIEW_RESULT_PAGE_MAXIMUM,
    PolicyPreviewRecord,
    PolicyPreviewResultPage,
    PolicyPreviewResultRow,
    PolicyPreviewService,
    PreviewProgress,
    PreviewResultCursor,
    PreviewStatus,
    compute_impact_digest,
)
from personal_os.exclusion_policy.publication import (
    ExclusionPolicyPublicationService,
    PolicyRequestFingerprint,
    PublicationSnapshotMaterial,
    PublishedPolicyResult,
    PublishPolicyCommand,
    SignedPolicySnapshot,
    SignedSnapshotBuilder,
)
from personal_os.exclusion_policy.signatures import (
    KEYSET_SIGNING_DOMAIN,
    PolicyKeysetKey,
    PolicyKeysetState,
    build_keyset_payload,
    build_signed_message,
    compute_payload_sha256_hex,
    derive_ed25519_key_id,
)
from personal_os.sources.actors import reject_nil_uuid
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.policy_drafts import (
    PolicyDatabaseRetryPolicy,
    PostgresqlPolicyDraftStore,
    draft_conflict_error,
    draft_not_initialized_error,
)
from postgresql_source_store.policy_keysets import hydrate_policy_keyset
from postgresql_source_store.policy_previews import (
    PostgresqlPolicyPreviewStore,
    preview_missing_error,
)
from postgresql_source_store.policy_publication import PostgresqlPolicyPublicationStore
from postgresql_source_store.tables import (
    policy_keyset_signatures,
    policy_keysets,
    policy_reconciliation_intents,
    policy_signing_keys,
    source_policies,
    workspace_policy_state,
)

#: The keyset chain page bound of spec 13.3: at most 16 ordered envelopes per
#: response, so a long-offline device verifies every link in bounded pages.
KEYSET_PAGE_MAXIMUM: Final[int] = 16

#: Deterministic offline identities and material.
OFFLINE_POLICY_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")
_OFFLINE_DRAFT_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000003")
_OFFLINE_NOW: Final[datetime] = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_OFFLINE_SIGNER_SEED: Final[bytes] = bytes(range(32))
_OFFLINE_IDENTITY_NAMESPACE: Final[UUID] = UUID("6a0e7a1e-0000-7000-8000-0000000000f1")

#: Closed safe reason tokens of the offline publication rechecks, mirroring
#: the real store's audit vocabulary.
_PREVIEW_BINDING_STALE: Final[SafeToken] = SafeToken.parse("preview_binding_stale")
_PREVIEW_DRAFT_STALE: Final[SafeToken] = SafeToken.parse("preview_draft_stale")
_PREVIEW_MISSING: Final[SafeToken] = SafeToken.parse("preview_missing")
_IDEMPOTENCY_MISMATCH: Final[SafeToken] = SafeToken.parse("idempotency_mismatch")

type _MappedRow = RowMapping


@dataclass(frozen=True, slots=True)
class PolicyKeysetPage:
    """One bounded ordered keyset chain page plus the continuation flag."""

    keysets: tuple[PolicyKeysetRecord, ...]
    has_more: bool


class PolicyPluginReadPort(Protocol):
    """The plugin-facing read surface: keyset chain, snapshot, reconciliation."""

    async def list_keyset_records(
        self,
        workspace_id: UUID,
        after_keyset_revision: int,
        limit: int,
        context: DiagnosticContext,
    ) -> tuple[PolicyKeysetRecord, ...]: ...

    async def load_active_snapshot(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> ActivePolicySnapshot | None: ...

    async def get_reconciliation_summary(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyReconciliationSummary | None: ...


class PolicyQueryService:
    """Admin and plugin policy reads over the query and plugin read ports.

    The service owns the spec 13.3 page bound: it asks the read port for one
    row beyond the bound, trims the page to at most 16 ordered envelopes and
    derives ``has_more`` from the extra row, so the continuation flag is
    exact without a second round trip.
    """

    def __init__(
        self, *, query_store: PolicyQueryStore, plugin_reads: PolicyPluginReadPort
    ) -> None:
        self._query_store = query_store
        self._plugin_reads = plugin_reads

    async def get_policy_status(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyStatus:
        """Return current published-revision metadata plus the working draft."""

        reject_nil_uuid("workspace_id", workspace_id)
        return await self._query_store.get_policy_status(workspace_id, context)

    async def get_reconciliation_summary(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyReconciliationSummary | None:
        """Return the latest durable reconciliation intent, or ``None``."""

        reject_nil_uuid("workspace_id", workspace_id)
        return await self._plugin_reads.get_reconciliation_summary(workspace_id, context)

    async def load_active_snapshot(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> ActivePolicySnapshot | None:
        """Return the active signed snapshot, or ``None`` before revision 1."""

        reject_nil_uuid("workspace_id", workspace_id)
        return await self._plugin_reads.load_active_snapshot(workspace_id, context)

    async def list_keyset_page(
        self, workspace_id: UUID, after_keyset_revision: int, context: DiagnosticContext
    ) -> PolicyKeysetPage:
        """Return the next bounded chain page after a known revision."""

        reject_nil_uuid("workspace_id", workspace_id)
        if after_keyset_revision < 0:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_INPUT_INVALID)
        records = await self._plugin_reads.list_keyset_records(
            workspace_id, after_keyset_revision, KEYSET_PAGE_MAXIMUM + 1, context
        )
        return PolicyKeysetPage(
            keysets=records[:KEYSET_PAGE_MAXIMUM],
            has_more=len(records) > KEYSET_PAGE_MAXIMUM,
        )


@dataclass(frozen=True, slots=True)
class ExclusionPolicyRuntime:
    """One composed exclusion-policy runtime the policy routes consume."""

    drafts: PolicyDraftService
    previews: PolicyPreviewService
    publication: ExclusionPolicyPublicationService
    queries: PolicyQueryService


# --- the serve composition ---------------------------------------------------------------


class PostgresqlPolicyPluginReadStore:
    """Plugin-facing reads over the canonical baseline.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. Every read runs one bounded transaction
    through the shared policy retry policy, hydrates the frozen domain
    values and never returns a row, SQL or driver payload. The keyset page
    preserves the append-only ``keyset_revision`` order; the snapshot read
    joins the active pointer with the immutable revision row and derives the
    signing key identifier from the trust anchor's public bytes.
    """

    def __init__(
        self, engine: AsyncEngine, *, retry: PolicyDatabaseRetryPolicy | None = None
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else PolicyDatabaseRetryPolicy()

    async def list_keyset_records(
        self,
        workspace_id: UUID,
        after_keyset_revision: int,
        limit: int,
        context: DiagnosticContext,
    ) -> tuple[PolicyKeysetRecord, ...]:
        del context
        return await self._retry.run(
            lambda _attempt: self._list_keyset_records_once(
                workspace_id, after_keyset_revision, limit
            )
        )

    async def _list_keyset_records_once(
        self, workspace_id: UUID, after_keyset_revision: int, limit: int
    ) -> tuple[PolicyKeysetRecord, ...]:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            keyset_rows = list(
                (
                    await connection.execute(
                        sa.select(
                            policy_keysets.c.policy_keyset_id,
                            policy_keysets.c.workspace_id,
                            policy_keysets.c.keyset_revision,
                            policy_keysets.c.parent_keyset_revision,
                            policy_keysets.c.canonical_payload_bytes,
                            policy_keysets.c.payload_sha256,
                            policy_keysets.c.created_by_user_id,
                            policy_keysets.c.created_at,
                        )
                        .where(
                            policy_keysets.c.workspace_id == workspace_id,
                            policy_keysets.c.keyset_revision > after_keyset_revision,
                        )
                        .order_by(policy_keysets.c.keyset_revision.asc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            if not keyset_rows:
                return ()
            keyset_ids = [row["policy_keyset_id"] for row in keyset_rows]
            key_rows = list(
                (
                    await connection.execute(
                        sa.select(
                            policy_keyset_signatures.c.policy_keyset_id,
                            policy_signing_keys.c.signing_key_id,
                            policy_signing_keys.c.workspace_id,
                            policy_signing_keys.c.public_key_bytes,
                        )
                        .select_from(policy_signing_keys)
                        .join(
                            policy_keyset_signatures,
                            policy_keyset_signatures.c.signing_key_id
                            == policy_signing_keys.c.signing_key_id,
                        )
                        .where(policy_keyset_signatures.c.policy_keyset_id.in_(keyset_ids))
                        .order_by(policy_signing_keys.c.signing_key_id)
                    )
                )
                .mappings()
                .all()
            )
            signature_rows = list(
                (
                    await connection.execute(
                        sa.select(
                            policy_keyset_signatures.c.policy_keyset_id,
                            policy_keyset_signatures.c.signing_key_id,
                            policy_keyset_signatures.c.signature_bytes,
                        )
                        .where(policy_keyset_signatures.c.policy_keyset_id.in_(keyset_ids))
                        .order_by(policy_keyset_signatures.c.signing_key_id)
                    )
                )
                .mappings()
                .all()
            )
        keys_by_keyset: dict[UUID, list[_MappedRow]] = {keyset_id: [] for keyset_id in keyset_ids}
        for key_row in key_rows:
            keys_by_keyset[key_row["policy_keyset_id"]].append(key_row)
        signatures_by_keyset: dict[UUID, list[_MappedRow]] = {
            keyset_id: [] for keyset_id in keyset_ids
        }
        for signature_row in signature_rows:
            signatures_by_keyset[signature_row["policy_keyset_id"]].append(signature_row)
        return tuple(
            hydrate_policy_keyset(
                row,
                keys_by_keyset[row["policy_keyset_id"]],
                signatures_by_keyset[row["policy_keyset_id"]],
            )
            for row in keyset_rows
        )

    async def load_active_snapshot(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> ActivePolicySnapshot | None:
        del context
        return await self._retry.run(lambda _attempt: self._load_active_snapshot_once(workspace_id))

    async def _load_active_snapshot_once(self, workspace_id: UUID) -> ActivePolicySnapshot | None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            state_row = (
                (
                    await connection.execute(
                        sa.select(workspace_policy_state.c.active_policy_revision_id).where(
                            workspace_policy_state.c.workspace_id == workspace_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if state_row is None:
                raise draft_not_initialized_error()
            active_revision_id = state_row["active_policy_revision_id"]
            if active_revision_id is None:
                return None
            snapshot_row = (
                (
                    await connection.execute(
                        sa.select(
                            source_policies.c.policy_revision_id,
                            source_policies.c.revision_number,
                            source_policies.c.parent_policy_revision_id,
                            source_policies.c.snapshot_payload_bytes,
                            source_policies.c.snapshot_payload_sha256,
                            source_policies.c.signature_bytes,
                            source_policies.c.published_at,
                            policy_signing_keys.c.public_key_bytes,
                        )
                        .select_from(source_policies)
                        .join(
                            policy_signing_keys,
                            policy_signing_keys.c.signing_key_id
                            == source_policies.c.signing_key_id,
                        )
                        .where(source_policies.c.policy_revision_id == active_revision_id)
                    )
                )
                .mappings()
                .first()
            )
        if snapshot_row is None:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
        return ActivePolicySnapshot(
            policy_revision_id=snapshot_row["policy_revision_id"],
            revision_number=int(snapshot_row["revision_number"]),
            parent_policy_revision_id=snapshot_row["parent_policy_revision_id"],
            payload_bytes=snapshot_row["snapshot_payload_bytes"],
            payload_sha256=snapshot_row["snapshot_payload_sha256"],
            signing_key_id=derive_ed25519_key_id(snapshot_row["public_key_bytes"]),
            signature_bytes=snapshot_row["signature_bytes"],
            published_at=snapshot_row["published_at"],
        )

    async def get_reconciliation_summary(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyReconciliationSummary | None:
        del context
        return await self._retry.run(
            lambda _attempt: self._get_reconciliation_summary_once(workspace_id)
        )

    async def _get_reconciliation_summary_once(
        self, workspace_id: UUID
    ) -> PolicyReconciliationSummary | None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = (
                (
                    await connection.execute(
                        sa.select(
                            policy_reconciliation_intents.c.policy_revision_id,
                            policy_reconciliation_intents.c.state,
                            policy_reconciliation_intents.c.updated_at,
                        )
                        .where(policy_reconciliation_intents.c.workspace_id == workspace_id)
                        .order_by(policy_reconciliation_intents.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return PolicyReconciliationSummary(
            policy_revision_id=row["policy_revision_id"],
            state=str(row["state"]),
            updated_at=row["updated_at"],
        )


def compose_exclusion_policy(
    *,
    engine: AsyncEngine,
    signer: Ed25519PolicySigner,
    metrics: ExclusionPolicyMetrics | None = None,
) -> ExclusionPolicyRuntime:
    """Build the real exclusion-policy runtime of one serve process.

    The verifier is bound to the signer's own derived public key — the
    in-transaction self-check of spec 11.1 step 7 — and the read adapter
    shares the engine with the stores without opening a connection here.
    """

    draft_store = PostgresqlPolicyDraftStore(engine)
    plugin_reads: PolicyPluginReadPort = PostgresqlPolicyPluginReadStore(engine)
    verifier = Ed25519PolicyVerifier({signer.key_id: signer.public_key_bytes})
    return ExclusionPolicyRuntime(
        drafts=PolicyDraftService(draft_store=draft_store, query_store=draft_store),
        previews=PolicyPreviewService(preview_store=PostgresqlPolicyPreviewStore(engine)),
        publication=ExclusionPolicyPublicationService(
            store=PostgresqlPolicyPublicationStore(engine),
            signer=signer,
            verifier=verifier,
            metrics=metrics,
        ),
        queries=PolicyQueryService(query_store=draft_store, plugin_reads=plugin_reads),
    )


# --- the offline composition --------------------------------------------------------------


class OfflineExclusionPolicyState:
    """In-memory draft, preview, publication and trust state of the offline graph.

    Tests seed or restamp rows through these public containers exactly like
    the offline authentication state; every stored value is a frozen domain
    value. The seeded keyset revision 1 is the real self-signed envelope of
    the deterministic offline signer.
    """

    def __init__(self) -> None:
        self.workspace_id: UUID = OFFLINE_POLICY_WORKSPACE_ID
        self.signer: Ed25519PolicySigner = Ed25519PolicySigner.from_seed_bytes(_OFFLINE_SIGNER_SEED)
        self.draft: PolicyDraft = PolicyDraft(
            draft_id=_OFFLINE_DRAFT_ID,
            workspace_id=self.workspace_id,
            draft_version=1,
            base_policy_revision_id=None,
        )
        self.is_policy_initialized: bool = True
        self.active_policy_revision_id: UUID | None = None
        self.active_revision_number: int = 0
        self.checkpoint: int = 0
        self.preview_rows: dict[UUID, PolicyPreviewRecord] = {}
        self.preview_result_rows: dict[UUID, list[PolicyPreviewResultRow]] = {}
        self.keyset_rows: list[PolicyKeysetRecord] = [self._seeded_keyset_revision_one()]
        self.active_snapshot: ActivePolicySnapshot | None = None
        self.reconciliation_summary: PolicyReconciliationSummary | None = None
        self.publications: dict[
            tuple[UUID, str], tuple[PolicyRequestFingerprint, PublishedPolicyResult]
        ] = {}

    @property
    def signer_public_key_bytes(self) -> bytes:
        """The raw public bytes of the offline signer's trust anchor."""

        return self.signer.public_key_bytes

    def _seeded_keyset_revision_one(self) -> PolicyKeysetRecord:
        """Build the deterministic self-signed keyset revision 1."""

        public_key_bytes = self.signer.public_key_bytes
        payload = build_keyset_payload(
            workspace_id=self.workspace_id,
            keyset_revision=1,
            parent_keyset_revision=None,
            created_at=_OFFLINE_NOW,
            keys=(
                PolicyKeysetKey(
                    key_id=self.signer.key_id,
                    public_key=public_key_bytes,
                    state=PolicyKeysetState.CURRENT,
                ),
            ),
        )
        signing_key_row_id = uuid5(_OFFLINE_IDENTITY_NAMESPACE, f"keyset-key:{self.signer.key_id}")
        signature = self.signer.sign(build_signed_message(KEYSET_SIGNING_DOMAIN, payload))
        return PolicyKeysetRecord(
            policy_keyset_id=uuid5(_OFFLINE_IDENTITY_NAMESPACE, "keyset:1"),
            workspace_id=self.workspace_id,
            keyset_revision=1,
            parent_keyset_revision=None,
            canonical_payload_bytes=payload,
            payload_sha256=compute_payload_sha256_hex(payload),
            keys=(PolicySigningKeyRecord(signing_key_row_id, public_key_bytes),),
            signatures=(PolicyKeysetSignatureRecord(signing_key_row_id, signature),),
            created_by_user_id=None,
            created_at=_OFFLINE_NOW,
        )


class OfflinePolicyDraftStore:
    """In-memory draft/query double behind the offline draft service."""

    def __init__(self, state: OfflineExclusionPolicyState) -> None:
        self._state = state

    async def load_draft(self, workspace_id: UUID, context: DiagnosticContext) -> PolicyDraft:
        del workspace_id, context
        if not self._state.is_policy_initialized:
            raise draft_not_initialized_error()
        return self._state.draft

    async def get_policy_status(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyStatus:
        del workspace_id, context
        if not self._state.is_policy_initialized:
            raise draft_not_initialized_error()
        return PolicyStatus(
            workspace_id=self._state.workspace_id,
            active_policy_revision_id=self._state.active_policy_revision_id,
            active_revision_number=self._state.active_revision_number,
            draft=self._state.draft,
        )

    async def replace_rules(
        self,
        draft_id: UUID,
        expected_draft_version: int,
        rules: tuple[ExclusionRule, ...],
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyDraft:
        del actor, context
        if not self._state.is_policy_initialized:
            raise draft_not_initialized_error()
        draft = self._state.draft
        if draft.draft_id != draft_id:
            raise draft_not_initialized_error()
        if draft.draft_version != expected_draft_version:
            raise draft_conflict_error(draft.draft_version)
        replaced = replace(draft, draft_version=draft.draft_version + 1, rules=rules)
        self._state.draft = replaced
        for preview_id, row in self._state.preview_rows.items():
            if row.status is PreviewStatus.READY and row.draft_version == expected_draft_version:
                self._state.preview_rows[preview_id] = replace(row, status=PreviewStatus.EXPIRED)
        return replaced


class OfflinePolicyPreviewStore:
    """In-memory preview double mirroring the real read contracts."""

    def __init__(self, state: OfflineExclusionPolicyState) -> None:
        self._state = state

    async def request_preview(
        self, workspace_id: UUID, actor: PolicyActor, context: DiagnosticContext
    ) -> PolicyPreviewRecord:
        del workspace_id, context
        if not self._state.is_policy_initialized:
            raise draft_not_initialized_error()
        draft = self._state.draft
        record = PolicyPreviewRecord(
            policy_preview_id=uuid7(),
            workspace_id=self._state.workspace_id,
            policy_draft_id=draft.draft_id,
            draft_version=draft.draft_version,
            draft_sha256=compute_draft_semantic_sha256(draft.rules),
            base_policy_revision_id=self._state.active_policy_revision_id,
            source_checkpoint_event_sequence=self._state.checkpoint,
            status=PreviewStatus.PENDING,
            impact_digest=None,
            safe_error_code=None,
            created_by_user_id=(
                actor.user_id
                if actor.user_id is not None
                else uuid5(_OFFLINE_IDENTITY_NAMESPACE, "system-actor")
            ),
            created_at=_OFFLINE_NOW,
            ready_at=None,
            expires_at=None,
            consumed_at=None,
        )
        self._state.preview_rows[record.policy_preview_id] = record
        return record

    async def run_preview_activity(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        heartbeat: Callable[[PreviewProgress], Awaitable[None]] | None = None,
    ) -> PolicyPreviewRecord:
        del heartbeat
        record = await self.get_preview(preview_id, context)
        ready = replace(
            record,
            status=PreviewStatus.READY,
            impact_digest=compute_impact_digest(()),
            ready_at=_OFFLINE_NOW,
            expires_at=_OFFLINE_NOW,
        )
        self._state.preview_rows[preview_id] = ready
        return ready

    async def get_preview(
        self, preview_id: UUID, context: DiagnosticContext
    ) -> PolicyPreviewRecord:
        del context
        record = self._state.preview_rows.get(preview_id)
        if record is None:
            raise preview_missing_error()
        return record

    async def list_preview_results(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        cursor: PreviewResultCursor | None = None,
        limit: int = PREVIEW_RESULT_PAGE_MAXIMUM,
    ) -> PolicyPreviewResultPage:
        record = await self.get_preview(preview_id, context)
        if record.status is not PreviewStatus.READY:
            if record.status in (PreviewStatus.EXPIRED, PreviewStatus.CONSUMED):
                raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED)
            if record.status is PreviewStatus.FAILED:
                raise ExclusionPolicyError(
                    ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
                    safe_details={
                        "reason": SafeToken.parse(str(record.safe_error_code or _PREVIEW_MISSING))
                    },
                )
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING)
        rows = sorted(
            self._state.preview_result_rows.get(preview_id, []),
            key=lambda row: (row.impact_class.value, str(row.source_id)),
        )
        if cursor is not None:
            rows = [
                row
                for row in rows
                if (row.impact_class.value, str(row.source_id))
                > (cursor.impact_class.value, str(cursor.source_id))
            ]
        page_rows = rows[:limit]
        next_cursor = (
            PreviewResultCursor(
                impact_class=page_rows[-1].impact_class, source_id=page_rows[-1].source_id
            )
            if len(rows) > limit and page_rows
            else None
        )
        return PolicyPreviewResultPage(rows=tuple(page_rows), next_cursor=next_cursor)


class OfflinePolicyPublicationStore:
    """In-memory publication double mirroring the real recheck order.

    The replay identity is ``(workspace, idempotency key)`` with the real
    request fingerprint; the rechecks under the lock follow spec 11.1 —
    preview existence/state/expiry, the command-versus-preview binding, the
    active parent, then the draft-table consistency guard — and the commit
    path builds, signs and verifies the real snapshot through the domain
    builder before any state mutates.
    """

    def __init__(self, state: OfflineExclusionPolicyState) -> None:
        self._state = state

    async def resolve_committed(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult | None:
        del context
        entry = self._state.publications.get((command.workspace_id, command.idempotency_key.value))
        if entry is None:
            return None
        committed_fingerprint, result = entry
        if committed_fingerprint.hexadecimal != fingerprint.hexadecimal:
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
                safe_details={"reason": _IDEMPOTENCY_MISMATCH},
            )
        return replace(result, is_replay=True)

    async def commit_publication(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        build_signed_snapshot: SignedSnapshotBuilder,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult:
        if not self._state.is_policy_initialized:
            raise draft_not_initialized_error()
        replayed = await self.resolve_committed(command, fingerprint, context)
        if replayed is not None:
            return replayed
        preview = self._state.preview_rows.get(command.policy_preview_id)
        if preview is None:
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
                safe_details={"reason": _PREVIEW_MISSING},
            )
        if preview.status is PreviewStatus.FAILED:
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
                safe_details={
                    "reason": SafeToken.parse(str(preview.safe_error_code or _PREVIEW_MISSING))
                },
            )
        if preview.status in (PreviewStatus.EXPIRED, PreviewStatus.CONSUMED):
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED)
        if preview.status is not PreviewStatus.READY:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING)
        if (
            preview.policy_draft_id != command.policy_draft_id
            or preview.draft_version != command.expected_draft_version
            or preview.draft_sha256 != command.expected_draft_sha256
            or preview.impact_digest != command.preview_impact_digest
            or preview.base_policy_revision_id != command.expected_active_policy_revision_id
        ):
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
                safe_details={"reason": _PREVIEW_BINDING_STALE},
            )
        if (
            command.expected_active_revision_number != self._state.active_revision_number
            or command.expected_active_policy_revision_id != self._state.active_policy_revision_id
        ):
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED,
                safe_details={"current_policy_revision_number": self._state.active_revision_number},
            )
        draft = self._state.draft
        if (
            draft.draft_id != preview.policy_draft_id
            or draft.draft_version != preview.draft_version
            or compute_draft_semantic_sha256(draft.rules) != preview.draft_sha256
        ):
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
                safe_details={"reason": _PREVIEW_DRAFT_STALE},
            )
        if preview.source_checkpoint_event_sequence != self._state.checkpoint:
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
                safe_details={"reason": SafeToken.parse("preview_source_checkpoint_stale")},
            )
        revision_number = self._state.active_revision_number + 1
        policy_revision_id = uuid5(
            _OFFLINE_IDENTITY_NAMESPACE, f"revision:{self._state.workspace_id}:{revision_number}"
        )
        material = PublicationSnapshotMaterial(
            workspace_id=self._state.workspace_id,
            policy_revision_id=policy_revision_id,
            revision_number=revision_number,
            parent_policy_revision_id=self._state.active_policy_revision_id,
            published_at=_OFFLINE_NOW,
            rules=draft.rules,
        )
        signed: SignedPolicySnapshot = build_signed_snapshot(material)
        result = PublishedPolicyResult(
            workspace_id=self._state.workspace_id,
            policy_revision_id=policy_revision_id,
            revision_number=revision_number,
            parent_policy_revision_id=self._state.active_policy_revision_id,
            payload_sha256=signed.payload_sha256,
            signing_key_id=self._state.signer.key_id,
            published_at=_OFFLINE_NOW,
            rule_count=len(draft.rules),
            reconciliation_status="pending",
            is_replay=False,
        )
        self._state.publications[(command.workspace_id, command.idempotency_key.value)] = (
            fingerprint,
            result,
        )
        self._state.active_policy_revision_id = policy_revision_id
        self._state.active_revision_number = revision_number
        self._state.active_snapshot = ActivePolicySnapshot(
            policy_revision_id=policy_revision_id,
            revision_number=revision_number,
            parent_policy_revision_id=result.parent_policy_revision_id,
            payload_bytes=signed.payload_bytes,
            payload_sha256=signed.payload_sha256,
            signing_key_id=signed.key_id,
            signature_bytes=signed.signature_bytes,
            published_at=_OFFLINE_NOW,
        )
        self._state.reconciliation_summary = PolicyReconciliationSummary(
            policy_revision_id=policy_revision_id,
            state="pending",
            updated_at=_OFFLINE_NOW,
        )
        self._state.draft = replace(
            draft,
            draft_version=draft.draft_version + 1,
            base_policy_revision_id=policy_revision_id,
        )
        self._state.preview_rows[preview.policy_preview_id] = replace(
            preview,
            status=PreviewStatus.CONSUMED,
            consumed_at=_OFFLINE_NOW,
        )
        return result


class OfflinePolicyPluginReadStore:
    """In-memory plugin read double over the offline state containers."""

    def __init__(self, state: OfflineExclusionPolicyState) -> None:
        self._state = state

    async def list_keyset_records(
        self,
        workspace_id: UUID,
        after_keyset_revision: int,
        limit: int,
        context: DiagnosticContext,
    ) -> tuple[PolicyKeysetRecord, ...]:
        del workspace_id, context
        ordered = sorted(self._state.keyset_rows, key=lambda row: row.keyset_revision)
        return tuple(row for row in ordered if row.keyset_revision > after_keyset_revision)[:limit]

    async def load_active_snapshot(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> ActivePolicySnapshot | None:
        del workspace_id, context
        return self._state.active_snapshot

    async def get_reconciliation_summary(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyReconciliationSummary | None:
        del workspace_id, context
        return self._state.reconciliation_summary


def compose_offline_exclusion_policy(
    *, state: OfflineExclusionPolicyState | None = None
) -> ExclusionPolicyRuntime:
    """Build the deterministic offline runtime for export and tests."""

    offline_state = state if state is not None else OfflineExclusionPolicyState()
    draft_store = OfflinePolicyDraftStore(offline_state)
    plugin_reads: PolicyPluginReadPort = OfflinePolicyPluginReadStore(offline_state)
    return ExclusionPolicyRuntime(
        drafts=PolicyDraftService(draft_store=draft_store, query_store=draft_store),
        previews=PolicyPreviewService(preview_store=OfflinePolicyPreviewStore(offline_state)),
        publication=ExclusionPolicyPublicationService(
            store=OfflinePolicyPublicationStore(offline_state),
            signer=offline_state.signer,
            verifier=Ed25519PolicyVerifier(
                {offline_state.signer.key_id: offline_state.signer.public_key_bytes}
            ),
        ),
        queries=PolicyQueryService(query_store=draft_store, plugin_reads=plugin_reads),
    )


__all__ = [
    "KEYSET_PAGE_MAXIMUM",
    "OFFLINE_POLICY_WORKSPACE_ID",
    "ExclusionPolicyRuntime",
    "OfflineExclusionPolicyState",
    "OfflinePolicyDraftStore",
    "OfflinePolicyPluginReadStore",
    "OfflinePolicyPreviewStore",
    "OfflinePolicyPublicationStore",
    "PolicyKeysetPage",
    "PolicyPluginReadPort",
    "PolicyQueryService",
    "PostgresqlPolicyPluginReadStore",
    "compose_exclusion_policy",
    "compose_offline_exclusion_policy",
]
