"""Provider-neutral ports and payload values for policy drafts and keysets.

The protocols here are the only surface the draft service and the future
publication/preview services see: loading one workspace draft, exact-version
rule replacement, one policy status read, and the immutable keyset
persistence Task 5's key lifecycle hands to PostgreSQL. The module pins the
port payload values — the closed actor shape, the draft and status snapshots,
and the keyset envelope with its Ed25519 material geometry — as frozen
dataclasses whose constructors enforce the closed invariants. Like the rest
of the package it imports no web framework, database driver, provider SDK or
cryptography library; envelope geometry reuses the pinned constants of
:mod:`personal_os.exclusion_policy.signatures`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.exclusion_policy.contracts import (
    MAXIMUM_RULES_PER_REVISION,
    ExclusionRule,
)
from personal_os.exclusion_policy.signatures import (
    ED25519_PUBLIC_KEY_BYTES,
    ED25519_SIGNATURE_BYTES,
)
from personal_os.sources.actors import reject_nil_uuid

#: Maximum canonical keyset payload bytes persisted in one envelope (spec 13;
#: mirrors the Task 3 database CHECK ``octet_length BETWEEN 1 AND 262144``).
KEYSET_MAXIMUM_PAYLOAD_BYTES: Final[int] = 256 * 1024


class PolicyActorKind(StrEnum):
    """Closed actor vocabulary for policy mutations and audit rows."""

    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class PolicyActor:
    """Who performs one policy mutation.

    ``user`` names the authenticated Admin user (draft edits, publications);
    ``system`` names operator tooling with no acting user (the Task 5
    key-lifecycle CLI). Exactly one shape is legal per kind.
    """

    actor_kind: PolicyActorKind
    user_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.actor_kind is PolicyActorKind.USER:
            if self.user_id is None:
                raise ValueError("user actor requires a user_id")
            reject_nil_uuid("user_id", self.user_id)
        elif self.user_id is not None:
            raise ValueError("system actor carries no user_id")


@dataclass(frozen=True, slots=True)
class PolicyDraft:
    """Immutable snapshot of one workspace draft (spec 8.2/9).

    Construction enforces the closed draft invariants — non-nil identities,
    a positive version, and duplicate-free rule IDs and semantic
    fingerprints — so a hydrated draft can never be ambiguous. The initial
    draft is empty with a null base; creation itself belongs to the
    workspace-bootstrap transaction, not to the draft port.
    """

    draft_id: UUID
    workspace_id: UUID
    draft_version: int
    base_policy_revision_id: UUID | None
    rules: tuple[ExclusionRule, ...] = ()

    def __post_init__(self) -> None:
        reject_nil_uuid("draft_id", self.draft_id)
        reject_nil_uuid("workspace_id", self.workspace_id)
        if self.base_policy_revision_id is not None:
            reject_nil_uuid("base_policy_revision_id", self.base_policy_revision_id)
        if self.draft_version < 1:
            raise ValueError("draft_version must be at least 1")
        if len(self.rules) > MAXIMUM_RULES_PER_REVISION:
            raise ValueError(f"draft must contain at most {MAXIMUM_RULES_PER_REVISION} rules")
        rule_ids = {rule.rule_id for rule in self.rules}
        if len(rule_ids) != len(self.rules):
            raise ValueError("duplicate rule_id in draft")
        fingerprints = {rule.semantic_fingerprint for rule in self.rules}
        if len(fingerprints) != len(self.rules):
            raise ValueError("duplicate semantic fingerprint in draft")


@dataclass(frozen=True, slots=True)
class PolicyStatus:
    """Current published-revision metadata plus the working draft (spec 9).

    The active pointer is null exactly when the active revision number is
    zero, mirroring the ``workspace_policy_state`` serialization row.
    """

    workspace_id: UUID
    active_policy_revision_id: UUID | None
    active_revision_number: int
    draft: PolicyDraft

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        if self.active_policy_revision_id is not None:
            reject_nil_uuid("active_policy_revision_id", self.active_policy_revision_id)
            if self.active_revision_number < 1:
                raise ValueError("active_revision_number must be positive with an active pointer")
        elif self.active_revision_number != 0:
            raise ValueError("active_revision_number must be zero without an active pointer")


@dataclass(frozen=True, slots=True)
class PolicySigningKeyRecord:
    """One public trust-anchor row: database identity plus raw public bytes."""

    signing_key_id: UUID
    public_key_bytes: bytes

    def __post_init__(self) -> None:
        reject_nil_uuid("signing_key_id", self.signing_key_id)
        if len(self.public_key_bytes) != ED25519_PUBLIC_KEY_BYTES:
            raise ValueError("public_key_bytes must be exactly 32 raw Ed25519 bytes")


@dataclass(frozen=True, slots=True)
class PolicyKeysetSignatureRecord:
    """One cross-signature over the canonical keyset payload (spec 13.3)."""

    signing_key_id: UUID
    signature_bytes: bytes

    def __post_init__(self) -> None:
        reject_nil_uuid("signing_key_id", self.signing_key_id)
        if len(self.signature_bytes) != ED25519_SIGNATURE_BYTES:
            raise ValueError("signature_bytes must be exactly 64 raw Ed25519 bytes")


@dataclass(frozen=True, slots=True)
class PolicyKeysetEnvelope:
    """What the key lifecycle hands the store: one immutable keyset revision.

    The envelope carries the RFC 8785 canonical payload bytes — never a
    caller-computed hash, which the store derives itself — plus the public
    signing-key rows the revision introduces and the cross-signatures over
    the domain-separated message. Construction enforces the closed chain and
    geometry invariants as plain ``ValueError`` shapes, mirroring the frozen
    value contracts of :mod:`personal_os.exclusion_policy.contracts`.
    """

    policy_keyset_id: UUID
    workspace_id: UUID
    keyset_revision: int
    parent_keyset_revision: int | None
    canonical_payload_bytes: bytes
    keys: tuple[PolicySigningKeyRecord, ...]
    signatures: tuple[PolicyKeysetSignatureRecord, ...]
    created_by_user_id: UUID | None = None

    def __post_init__(self) -> None:
        reject_nil_uuid("policy_keyset_id", self.policy_keyset_id)
        reject_nil_uuid("workspace_id", self.workspace_id)
        if self.created_by_user_id is not None:
            reject_nil_uuid("created_by_user_id", self.created_by_user_id)
        if self.keyset_revision < 1:
            raise ValueError("keyset_revision must be at least 1")
        if self.keyset_revision == 1:
            if self.parent_keyset_revision is not None:
                raise ValueError("keyset revision 1 has no parent revision")
        elif self.parent_keyset_revision != self.keyset_revision - 1:
            raise ValueError("parent_keyset_revision must be keyset_revision minus one")
        if not self.canonical_payload_bytes:
            raise ValueError("canonical_payload_bytes must not be empty")
        if len(self.canonical_payload_bytes) > KEYSET_MAXIMUM_PAYLOAD_BYTES:
            raise ValueError("canonical_payload_bytes exceeds the 256 KiB keyset ceiling")
        key_ids = {key.signing_key_id for key in self.keys}
        if len(key_ids) != len(self.keys):
            raise ValueError("duplicate signing_key_id in keyset envelope")
        if not self.signatures:
            raise ValueError("keyset envelope requires at least one signature")
        signature_key_ids = {signature.signing_key_id for signature in self.signatures}
        if len(signature_key_ids) != len(self.signatures):
            raise ValueError("duplicate signing_key_id in keyset signatures")
        if not signature_key_ids.issubset(key_ids):
            raise ValueError("every keyset signature must reference a declared signing key")


@dataclass(frozen=True, slots=True)
class PersistedPolicyKeyset:
    """Persistence acknowledgement for one keyset envelope."""

    policy_keyset_id: UUID
    workspace_id: UUID
    keyset_revision: int
    payload_sha256: str
    created_at: datetime
    is_replay: bool


@dataclass(frozen=True, slots=True)
class PolicyKeysetRecord:
    """Immutable read model of one persisted keyset graph (spec 8.6/13).

    Signing-key rows and keyset envelopes are append-only; current/staged/
    retired meaning lives in the canonical payload bytes, never in mutated
    rows, so the record returns exactly what was persisted.
    """

    policy_keyset_id: UUID
    workspace_id: UUID
    keyset_revision: int
    parent_keyset_revision: int | None
    canonical_payload_bytes: bytes
    payload_sha256: str
    keys: tuple[PolicySigningKeyRecord, ...]
    signatures: tuple[PolicyKeysetSignatureRecord, ...]
    created_by_user_id: UUID | None
    created_at: datetime

    def __post_init__(self) -> None:
        reject_nil_uuid("policy_keyset_id", self.policy_keyset_id)
        reject_nil_uuid("workspace_id", self.workspace_id)
        if self.keyset_revision < 1:
            raise ValueError("keyset_revision must be at least 1")
        if self.canonical_payload_bytes and len(self.canonical_payload_bytes) > (
            KEYSET_MAXIMUM_PAYLOAD_BYTES
        ):
            raise ValueError("canonical_payload_bytes exceeds the 256 KiB keyset ceiling")


class PolicyDraftStore(Protocol):
    """Draft read and exact-version mutation port (spec 8.2/9).

    ``replace_rules`` performs the full-list compare-and-swap: it requires
    the exact ``expected_draft_version``, increments the version exactly
    once on success and expires ready previews bound to the prior draft
    version. A stale version raises the typed draft conflict; a missing
    graph raises the typed not-initialized error. No method exposes rows,
    SQL or driver payloads.
    """

    async def load_draft(self, workspace_id: UUID, context: DiagnosticContext) -> PolicyDraft: ...

    async def replace_rules(
        self,
        draft_id: UUID,
        expected_draft_version: int,
        rules: tuple[ExclusionRule, ...],
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyDraft: ...


class PolicyQueryStore(Protocol):
    """Admin policy-status read port (spec 9)."""

    async def get_policy_status(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyStatus: ...


class PolicyKeysetStore(Protocol):
    """Immutable keyset persistence port (spec 8.6/13).

    ``persist_keyset`` appends one keyset revision idempotently: an exact
    replay of the same revision identity and canonical bytes acknowledges
    the existing row, while any divergence is an integrity failure that
    never mutates history. ``load_latest_keyset`` returns the newest
    envelope with its keys and signatures, or ``None`` before the first
    initialization.
    """

    async def persist_keyset(
        self, envelope: PolicyKeysetEnvelope, context: DiagnosticContext
    ) -> PersistedPolicyKeyset: ...

    async def load_latest_keyset(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyKeysetRecord | None: ...
