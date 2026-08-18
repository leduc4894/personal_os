"""Atomic signed publication: command, fingerprint, snapshot signing, service.

This module is the provider-neutral half of spec section 11. The frozen
:class:`PublishPolicyCommand` carries the exact publication binding — preview
identity/digest, draft identity/version/hash, expected active revision, the
exact confirmation phrase and the opaque idempotency key — and
:func:`compute_publication_request_fingerprint` hashes the closed envelope
(contract tag, workspace/actor identity, preview identity/digest, draft
identity/version/hash, expected active revision, exact confirmation
semantics) with the repository's canonical sorted-compact-JSON rules. The
fingerprint excludes request/trace IDs and the idempotency key itself, so no
transport or retry artifact can alter replay identity.

:func:`sign_policy_snapshot` is the only signing path: it builds the spec 12
canonical payload through the Task 2 builder, signs the domain-separated
message through the injected :class:`PolicySigner` port, verifies the produced
signature with the injected :class:`PolicySignatureVerifier` port and fails
closed to the typed signing-unavailable error on any crash, geometry or
verification failure. The store invokes this helper while the serialization
row is locked, so the persisted signature bytes are always the deterministic
signature of exactly the committed canonical payload.

:class:`ExclusionPolicyPublicationService` validates input before any
transaction, resolves exact replay through the store port and records the
closed publication metric only after a known commit, replay or rejection
outcome — never for an unknown commit outcome. Like the rest of the package
it imports no web framework, database driver, provider SDK or cryptography
library.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    ExclusionPolicyRevision,
    ExclusionRule,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError, input_invalid
from personal_os.exclusion_policy.metrics import (
    ExclusionPolicyMetrics,
    PublicationMetricOutcome,
)
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.signatures import (
    ED25519_SIGNATURE_BYTES,
    SNAPSHOT_PAYLOAD_CONTRACT,
    SNAPSHOT_SIGNING_DOMAIN,
    PolicySignatureVerifier,
    PolicySigner,
    build_signed_message,
    build_snapshot_payload,
    compute_payload_sha256_hex,
    is_wellformed_ed25519_key_id,
)
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import IdempotencyKey

#: The exact confirmation phrase a publication request must carry (spec 11).
CONFIRMATION_PHRASE: Final[str] = "PUBLISH EXCLUSION POLICY"

#: Contract tag hashed into every publication request fingerprint.
PUBLICATION_REQUEST_CONTRACT: Final[str] = "exclusion_policy_publish/v1"

#: Closed reason tokens for service-level publication input rejections.
ACTOR_INVALID: SafeToken = SafeToken.parse("actor_invalid")

_DIGEST_HEX_LENGTH: Final[int] = 64
_HEX_LOWER: Final[frozenset[str]] = frozenset("0123456789abcdef")


def _validate_digest_hex(field_name: str, value: str) -> None:
    if len(value) != _DIGEST_HEX_LENGTH or any(char not in _HEX_LOWER for char in value):
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class PolicyRequestFingerprint:
    """Lowercase hexadecimal SHA-256 of the canonical publication envelope."""

    hexadecimal: str

    @classmethod
    def parse(cls, value: str) -> PolicyRequestFingerprint:
        """Validate ``value`` as exactly 64 lowercase hexadecimal characters."""
        if len(value) != _DIGEST_HEX_LENGTH or any(char not in _HEX_LOWER for char in value):
            raise ValueError("value does not satisfy the canonical fingerprint contract")
        return cls(value)

    def __str__(self) -> str:
        return self.hexadecimal


@dataclass(frozen=True, slots=True)
class PublishPolicyCommand:
    """One Admin publication request bound to its exact replay identity.

    Construction enforces the closed binding invariants — non-nil
    identities, a positive expected draft version, a nonnegative expected
    active revision number, lowercase-hex digests and the printable opaque
    idempotency-key grammar — while the exact confirmation phrase is checked
    by the service (and rechecked under the store's lock) so a wrong phrase
    surfaces as the typed confirmation error rather than a construction
    crash. An initially empty policy publishes revision 1 explicitly: a null
    expected active revision with a zero revision number is the valid
    first-publication shape, and nothing here publishes implicitly.
    """

    workspace_id: UUID
    actor: PolicyActor
    policy_preview_id: UUID
    policy_draft_id: UUID
    expected_draft_version: int
    expected_draft_sha256: str
    preview_impact_digest: str
    expected_active_policy_revision_id: UUID | None
    expected_active_revision_number: int
    idempotency_key: IdempotencyKey
    confirmation: str

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("policy_preview_id", self.policy_preview_id)
        reject_nil_uuid("policy_draft_id", self.policy_draft_id)
        if self.expected_active_policy_revision_id is not None:
            reject_nil_uuid(
                "expected_active_policy_revision_id", self.expected_active_policy_revision_id
            )
        if self.expected_draft_version < 1:
            raise ValueError("expected_draft_version must be at least 1")
        if self.expected_active_revision_number < 0:
            raise ValueError("expected_active_revision_number must not be negative")
        _validate_digest_hex("expected_draft_sha256", self.expected_draft_sha256)
        _validate_digest_hex("preview_impact_digest", self.preview_impact_digest)


def _canonical_json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def compute_publication_request_fingerprint(
    command: PublishPolicyCommand,
) -> PolicyRequestFingerprint:
    """Hash the closed publication request envelope (spec 11).

    The envelope covers exactly the replay-defining members: the contract
    tag, workspace and actor identity, preview identity and impact digest,
    draft identity, version and semantic hash, the expected active revision
    identity and number, and the exact confirmation semantics. The
    idempotency key and every request/trace correlation identity are
    excluded, so a retried request with a different transport identity keeps
    its fingerprint while any binding drift changes it.
    """

    if command.actor.actor_kind is not PolicyActorKind.USER or command.actor.user_id is None:
        raise ValueError("publication fingerprint requires a user actor")
    envelope: dict[str, object] = {
        "contract": PUBLICATION_REQUEST_CONTRACT,
        "workspace_id": str(command.workspace_id),
        "actor_kind": command.actor.actor_kind.value,
        "actor_id": str(command.actor.user_id),
        "policy_preview_id": str(command.policy_preview_id),
        "preview_impact_digest": command.preview_impact_digest,
        "policy_draft_id": str(command.policy_draft_id),
        "draft_version": command.expected_draft_version,
        "draft_sha256": command.expected_draft_sha256,
        "expected_active_policy_revision_id": (
            None
            if command.expected_active_policy_revision_id is None
            else str(command.expected_active_policy_revision_id)
        ),
        "expected_active_revision_number": command.expected_active_revision_number,
        # The command is validated against the exact phrase before hashing,
        # so pinning the constant here fixes the confirmation semantics.
        "confirmation": CONFIRMATION_PHRASE,
    }
    return PolicyRequestFingerprint(sha256(_canonical_json_bytes(envelope)).hexdigest())


@dataclass(frozen=True, slots=True)
class PublicationSnapshotMaterial:
    """The locked-transaction inputs of one snapshot build (spec 11.1 step 6).

    The store assembles this value only after the serialization row, draft
    and preview rows are locked and every binding has been rechecked, so the
    material names exactly the revision identity, parent lineage, transaction
    timestamp and already-normalized rules of the revision being committed.
    """

    workspace_id: UUID
    policy_revision_id: UUID
    revision_number: int
    parent_policy_revision_id: UUID | None
    published_at: datetime
    rules: tuple[ExclusionRule, ...]

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("policy_revision_id", self.policy_revision_id)
        if self.parent_policy_revision_id is not None:
            reject_nil_uuid("parent_policy_revision_id", self.parent_policy_revision_id)
        if self.revision_number < 1:
            raise ValueError("revision_number must be at least 1")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SignedPolicySnapshot:
    """The persisted signed-snapshot members of one committed revision.

    Carries the canonical payload bytes, their lowercase SHA-256, the derived
    Ed25519 key ID and exactly 64 raw signature bytes. The envelope wrapping
    is fixed by spec 12; nothing here re-renders or re-signs persisted bytes.
    """

    payload_bytes: bytes
    payload_sha256: str
    key_id: str
    signature_bytes: bytes

    def __post_init__(self) -> None:
        if not self.payload_bytes:
            raise ValueError("payload_bytes must not be empty")
        _validate_digest_hex("payload_sha256", self.payload_sha256)
        if not is_wellformed_ed25519_key_id(self.key_id):
            raise ValueError("key_id does not satisfy the closed Ed25519 key-id grammar")
        if len(self.signature_bytes) != ED25519_SIGNATURE_BYTES:
            raise ValueError("signature_bytes must be exactly 64 raw Ed25519 bytes")


def signing_unavailable_error() -> ExclusionPolicyError:
    """Build the typed signing-unavailable error carrying no safe details."""

    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE)


def sign_policy_snapshot(
    material: PublicationSnapshotMaterial,
    *,
    signer: PolicySigner,
    verifier: PolicySignatureVerifier,
) -> SignedPolicySnapshot:
    """Build, sign and verify one snapshot inside the locked transaction.

    The payload comes from the Task 2 builder over the material's frozen
    revision values; the signature covers the domain-separated message and
    is verified with the signer's own derived public key before it exists as
    a value. A signer crash, wrong geometry or failed verification maps to
    the typed signing-unavailable error with the cause chained only — never
    message bytes, key material or library text.
    """

    revision = ExclusionPolicyRevision(
        policy_revision_id=material.policy_revision_id,
        workspace_id=material.workspace_id,
        revision_number=material.revision_number,
        rules=material.rules,
    )
    payload_bytes = build_snapshot_payload(
        revision,
        parent_policy_revision_id=material.parent_policy_revision_id,
        published_at=material.published_at,
    )
    message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload_bytes)
    try:
        signature_bytes = signer.sign(message)
    except ExclusionPolicyError:
        raise
    except Exception as cause:
        raise signing_unavailable_error() from cause
    if len(signature_bytes) != ED25519_SIGNATURE_BYTES:
        raise signing_unavailable_error()
    if not verifier.verify(signer.key_id, signature_bytes, message):
        raise signing_unavailable_error()
    return SignedPolicySnapshot(
        payload_bytes=payload_bytes,
        payload_sha256=compute_payload_sha256_hex(payload_bytes),
        key_id=signer.key_id,
        signature_bytes=signature_bytes,
    )


#: The in-transaction snapshot builder the store invokes under its locks.
type SignedSnapshotBuilder = Callable[[PublicationSnapshotMaterial], SignedPolicySnapshot]


@dataclass(frozen=True, slots=True)
class PublishedPolicyResult:
    """The durable outcome of one publication commit or exact replay.

    Carries the revision identities and number, the committed payload hash,
    the derived signing key ID, the publication time, the rule count, the
    reconciliation state of the durable intent and whether this
    acknowledgement was an exact replay rather than a fresh commit. It never
    carries payload or signature bytes: reads return persisted bytes only.
    """

    workspace_id: UUID
    policy_revision_id: UUID
    revision_number: int
    parent_policy_revision_id: UUID | None
    payload_sha256: str
    signing_key_id: str
    published_at: datetime
    rule_count: int
    reconciliation_status: str
    is_replay: bool

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("policy_revision_id", self.policy_revision_id)
        if self.parent_policy_revision_id is not None:
            reject_nil_uuid("parent_policy_revision_id", self.parent_policy_revision_id)
        if self.revision_number < 1:
            raise ValueError("revision_number must be at least 1")
        _validate_digest_hex("payload_sha256", self.payload_sha256)
        if not is_wellformed_ed25519_key_id(self.signing_key_id):
            raise ValueError("signing_key_id does not satisfy the closed key-id grammar")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        if self.rule_count < 0:
            raise ValueError("rule_count must not be negative")
        if not self.reconciliation_status:
            raise ValueError("reconciliation_status must not be empty")


class PolicyPublicationStore(Protocol):
    """Provider-neutral publication persistence port (spec 11.1/11.2).

    ``resolve_committed`` is the lock-free indexed replay preflight by
    workspace and idempotency key: an exact fingerprint match hydrates the
    original result, a different fingerprint under the same key is terminal
    misuse, and no row means the commit path decides. ``commit_publication``
    runs the one locked transaction — advisory lock, serialization row,
    draft and preview locks, binding rechecks, the builder invocation,
    immutable revision/rules/signature insert, pointer swap, reconciliation
    work, audit, preview consumption and draft rebase — and resolves an
    ambiguous acknowledgement through the same key/fingerprint evidence
    lookup. No method exposes rows, SQL or driver payloads.
    """

    async def resolve_committed(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult | None: ...

    async def commit_publication(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        build_signed_snapshot: SignedSnapshotBuilder,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult: ...


class ExclusionPolicyPublicationService:
    """Provider-neutral publication application service (spec 11).

    The service owns validation and orchestration only: input is validated
    before any transaction opens, the exact replay preflight precedes the
    commit, and the signed snapshot is built only inside the store's locked
    transaction through the bound signer and verifier ports. The closed
    publication metric is recorded only after a known durable outcome —
    commit, replay or terminal rejection — and never when the commit outcome
    is unknown.
    """

    def __init__(
        self,
        *,
        store: PolicyPublicationStore,
        signer: PolicySigner,
        verifier: PolicySignatureVerifier,
        metrics: ExclusionPolicyMetrics | None = None,
    ) -> None:
        self._store = store
        self._signer = signer
        self._verifier = verifier
        self._metrics = metrics

    async def publish(
        self, command: PublishPolicyCommand, context: DiagnosticContext
    ) -> PublishedPolicyResult:
        """Validate, replay-resolve and atomically publish one command."""

        self._validate(command)
        fingerprint = compute_publication_request_fingerprint(command)
        started_monotonic = time.monotonic()
        resolved = await self._store.resolve_committed(command, fingerprint, context)
        if resolved is not None:
            self._record(PublicationMetricOutcome.REPLAYED, started_monotonic)
            return resolved
        try:
            result = await self._store.commit_publication(
                command, fingerprint, self._build_signed_snapshot, context
            )
        except ExclusionPolicyError as error:
            if error.error_code is not ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN:
                self._record(PublicationMetricOutcome.REJECTED, started_monotonic)
            raise
        outcome = (
            PublicationMetricOutcome.REPLAYED
            if result.is_replay
            else PublicationMetricOutcome.PUBLISHED
        )
        self._record(outcome, started_monotonic)
        return result

    def _validate(self, command: PublishPolicyCommand) -> None:
        """Enforce the closed pre-transaction validation set (spec 11)."""

        if command.actor.actor_kind is not PolicyActorKind.USER:
            raise input_invalid(ACTOR_INVALID)
        if command.confirmation != CONFIRMATION_PHRASE:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID)

    def _build_signed_snapshot(self, material: PublicationSnapshotMaterial) -> SignedPolicySnapshot:
        """Build, sign and verify the snapshot for the store's locked row."""

        return sign_policy_snapshot(material, signer=self._signer, verifier=self._verifier)

    def _record(self, outcome: PublicationMetricOutcome, started_monotonic: float) -> None:
        if self._metrics is None:
            return
        duration_seconds = max(0.0, time.monotonic() - started_monotonic)
        self._metrics.record_publication(outcome=outcome, duration_seconds=duration_seconds)


__all__ = [
    "ACTOR_INVALID",
    "CONFIRMATION_PHRASE",
    "PUBLICATION_REQUEST_CONTRACT",
    "SNAPSHOT_PAYLOAD_CONTRACT",
    "ExclusionPolicyPublicationService",
    "PolicyPublicationStore",
    "PolicyRequestFingerprint",
    "PublicationSnapshotMaterial",
    "PublishPolicyCommand",
    "PublishedPolicyResult",
    "SignedPolicySnapshot",
    "SignedSnapshotBuilder",
    "compute_publication_request_fingerprint",
    "sign_policy_snapshot",
    "signing_unavailable_error",
]
