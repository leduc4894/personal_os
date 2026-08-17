"""Publication command, fingerprint, snapshot signing and service contracts.

These tests pin the provider-neutral publication half of spec section 11:
the frozen command whose construction enforces the closed binding invariants,
the canonical request fingerprint that covers contract tag, workspace/actor,
preview identity/digest, draft identity/version/hash, expected active
revision and the exact confirmation semantics while excluding request/trace
IDs and the idempotency key itself, the in-transaction snapshot signing
helper that builds, signs and verifies the spec 12 envelope (deterministic
signature bytes, fail-closed signing-unavailable mapping) and the application
service that validates before any transaction, resolves exact replay without
re-committing and records the closed publication metric only after a known
durable outcome.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from tests.unit.exclusion_policy.fakes import rule  # noqa: F401

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.events import SafeToken  # noqa: F401
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import (
    InMemoryExclusionPolicyMetrics,
    PublicationMetricOutcome,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.publication import (
    CONFIRMATION_PHRASE,
    ExclusionPolicyPublicationService,
    PolicyPublicationStore,
    PolicyRequestFingerprint,
    PublicationSnapshotMaterial,
    PublishedPolicyResult,
    PublishPolicyCommand,
    SignedPolicySnapshot,
    compute_publication_request_fingerprint,
    sign_policy_snapshot,
)
from personal_os.exclusion_policy.signatures import SIGNATURE_ALGORITHM
from personal_os.sources.commands import IdempotencyKey

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000f1")
OTHER_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000f2")
PREVIEW_ID = UUID("018f47a0-7b00-7000-8000-0000000000f3")
OTHER_PREVIEW_ID = UUID("018f47a0-7b00-7000-8000-0000000000f4")
DRAFT_ID = UUID("018f47a0-7b00-7000-8000-0000000000f5")
USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000f6")
OTHER_USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000f7")
ACTIVE_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000f8")
NEW_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000f9")
RULE_ID = UUID("018f47a0-7b00-7000-8000-0000000000fa")
PUBLISHED_AT = datetime(2026, 8, 17, 9, 30, 0, 123456, tzinfo=UTC)

DRAFT_SHA256 = "a" * 64
OTHER_DIGEST = "b" * 64
PREVIEW_IMPACT_DIGEST = "c" * 64

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)

_SIGNING_SEED = b"\x11" * 32
_FAKE_KEY_ID = "ed25519-sha256-" + sha256(_SIGNING_SEED).hexdigest()[:43]


def _context() -> DiagnosticContext:
    return DiagnosticContext(
        request_id=UUID("018f47a0-7b00-7000-8000-0000000000fd"),
        client_request_id=None,
        trace=_TRACE,
    )


def _actor(user_id: UUID = USER_ID) -> PolicyActor:
    return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=user_id)


def _command(**overrides: Any) -> PublishPolicyCommand:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "actor": _actor(),
        "policy_preview_id": PREVIEW_ID,
        "policy_draft_id": DRAFT_ID,
        "expected_draft_version": 3,
        "expected_draft_sha256": DRAFT_SHA256,
        "preview_impact_digest": PREVIEW_IMPACT_DIGEST,
        "expected_active_policy_revision_id": None,
        "expected_active_revision_number": 0,
        "idempotency_key": IdempotencyKey("publish-once-001"),
        "confirmation": CONFIRMATION_PHRASE,
    }
    values.update(overrides)
    return PublishPolicyCommand(**values)


def _material(**overrides: Any) -> PublicationSnapshotMaterial:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "policy_revision_id": NEW_REVISION_ID,
        "revision_number": 1,
        "parent_policy_revision_id": None,
        "published_at": PUBLISHED_AT,
        "rules": (
            normalize_rule(
                RULE_ID,
                RuleKind.FOLDER_PREFIX,
                text_operand="private/notes",
                rule_index=0,
            ),
        ),
    }
    values.update(overrides)
    return PublicationSnapshotMaterial(**values)


def _result(**overrides: Any) -> PublishedPolicyResult:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "policy_revision_id": NEW_REVISION_ID,
        "revision_number": 1,
        "parent_policy_revision_id": None,
        "payload_sha256": "d" * 64,
        "signing_key_id": _FAKE_KEY_ID,
        "published_at": PUBLISHED_AT,
        "rule_count": 1,
        "reconciliation_status": "pending",
        "is_replay": False,
    }
    values.update(overrides)
    return PublishedPolicyResult(**values)


class _FakeSigner:
    """Deterministic test signer implementing the domain signing port."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[bytes] = []

    @property
    def key_id(self) -> str:
        return _FAKE_KEY_ID

    def sign(self, message: bytes) -> bytes:
        if self._fail:
            raise RuntimeError("signer exploded")
        self.calls.append(message)
        return sha256(_SIGNING_SEED + message).digest() + b"\x00" * 32


class _FakeVerifier:
    """Deterministic test verifier implementing the domain verification port."""

    def __init__(self, *, accept: bool = True) -> None:
        self._accept = accept

    def verify(self, key_id: str, signature: bytes, message: bytes) -> bool:
        expected = sha256(_SIGNING_SEED + message).digest() + b"\x00" * 32
        return self._accept and key_id == _FAKE_KEY_ID and signature == expected


class _RecordingStore:
    """In-memory policy publication store double."""

    def __init__(
        self,
        *,
        resolved: PublishedPolicyResult | None = None,
        committed: PublishedPolicyResult | None = None,
        commit_error: ApplicationError | None = None,
    ) -> None:
        self.resolved = resolved
        self.committed = committed if committed is not None else _result()
        self.commit_error = commit_error
        self.resolve_calls = 0
        self.commit_calls = 0
        self.seen_fingerprints: list[str] = []
        self.builder_results: list[SignedPolicySnapshot] = []

    async def resolve_committed(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult | None:
        del command, context
        self.resolve_calls += 1
        self.seen_fingerprints.append(fingerprint.hexadecimal)
        return self.resolved

    async def commit_publication(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        build_signed_snapshot: Any,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult:
        del context
        self.commit_calls += 1
        self.seen_fingerprints.append(fingerprint.hexadecimal)
        if self.commit_error is not None:
            raise self.commit_error
        # The store invokes the builder under its locked transaction; the
        # double mirrors that by materializing the snapshot exactly once.
        self.builder_results.append(build_signed_snapshot(_material()))
        return self.committed


def _assert_is_input_invalid(error: ExclusionPolicyError, reason: str) -> None:
    assert error.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert error.safe_details["reason"].value == reason


# --- command invariants --------------------------------------------------------------


def test_confirmation_phrase_is_exact() -> None:
    assert CONFIRMATION_PHRASE == "PUBLISH EXCLUSION POLICY"


def test_command_construction_accepts_initial_empty_publication() -> None:
    command = _command()
    assert command.expected_active_policy_revision_id is None
    assert command.expected_active_revision_number == 0


def test_command_rejects_nil_identities() -> None:
    with pytest.raises(ValueError):
        _command(workspace_id=UUID(int=0))
    with pytest.raises(ValueError):
        _command(policy_preview_id=UUID(int=0))
    with pytest.raises(ValueError):
        _command(policy_draft_id=UUID(int=0))
    with pytest.raises(ValueError):
        _command(expected_active_policy_revision_id=UUID(int=0))


def test_command_rejects_invalid_versions_and_digests() -> None:
    with pytest.raises(ValueError):
        _command(expected_draft_version=0)
    with pytest.raises(ValueError):
        _command(expected_active_revision_number=-1)
    with pytest.raises(ValueError):
        _command(expected_draft_sha256="XYZ")
    with pytest.raises(ValueError):
        _command(preview_impact_digest="c" * 63)
    with pytest.raises(ValueError):
        _command(expected_draft_sha256=DRAFT_SHA256.upper())


def test_command_rejects_invalid_idempotency_keys() -> None:
    with pytest.raises(ValueError):
        _command(idempotency_key=IdempotencyKey(""))
    with pytest.raises(ValueError):
        _command(idempotency_key=IdempotencyKey("k" * 201))
    with pytest.raises(ValueError):
        _command(idempotency_key=IdempotencyKey("has space"))
    with pytest.raises(ValueError):
        _command(idempotency_key=IdempotencyKey("ünïcode"))


# --- fingerprint ---------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_wellformed() -> None:
    fingerprint = compute_publication_request_fingerprint(_command())
    assert isinstance(fingerprint, PolicyRequestFingerprint)
    assert len(fingerprint.hexadecimal) == 64
    assert fingerprint.hexadecimal == compute_publication_request_fingerprint(
        _command()
    ).hexadecimal
    assert str(fingerprint) == fingerprint.hexadecimal


def test_fingerprint_parse_rejects_malformed_hex() -> None:
    with pytest.raises(ValueError):
        PolicyRequestFingerprint.parse("nothex")
    with pytest.raises(ValueError):
        PolicyRequestFingerprint.parse("A" * 64)


def test_fingerprint_excludes_the_idempotency_key() -> None:
    first = compute_publication_request_fingerprint(
        _command(idempotency_key=IdempotencyKey("key-one"))
    )
    second = compute_publication_request_fingerprint(
        _command(idempotency_key=IdempotencyKey("key-two"))
    )
    assert first.hexadecimal == second.hexadecimal


@pytest.mark.parametrize(
    "override",
    [
        {"workspace_id": OTHER_WORKSPACE_ID},
        {"actor": _actor(OTHER_USER_ID)},
        {"policy_preview_id": OTHER_PREVIEW_ID},
        {"preview_impact_digest": OTHER_DIGEST},
        {"policy_draft_id": UUID("018f47a0-7b00-7000-8000-0000000000fc")},
        {"expected_draft_version": 4},
        {"expected_draft_sha256": OTHER_DIGEST},
        {"expected_active_policy_revision_id": ACTIVE_REVISION_ID},
        {"expected_active_revision_number": 1},
    ],
)
def test_fingerprint_covers_every_binding_member(override: dict[str, Any]) -> None:
    baseline = compute_publication_request_fingerprint(_command())
    changed = compute_publication_request_fingerprint(_command(**override))
    assert changed.hexadecimal != baseline.hexadecimal


# --- snapshot signing ----------------------------------------------------------------


def test_sign_policy_snapshot_is_deterministic_and_verifiable() -> None:
    first = sign_policy_snapshot(_material(), signer=_FakeSigner(), verifier=_FakeVerifier())
    second = sign_policy_snapshot(_material(), signer=_FakeSigner(), verifier=_FakeVerifier())
    assert isinstance(first, SignedPolicySnapshot)
    assert first.signature_bytes == second.signature_bytes
    assert first.key_id == _FAKE_KEY_ID
    assert len(first.signature_bytes) == 64
    assert first.payload_sha256 == sha256(first.payload_bytes).hexdigest()
    assert first.payload_bytes.startswith(b'{"contract":"exclusion_policy_snapshot/v1"')
    assert b'"revision_number":1' in first.payload_bytes


def test_sign_policy_snapshot_covers_every_rule_kind_operand_name() -> None:
    rules = (
        normalize_rule(
            UUID(int=1), RuleKind.EXACT_SOURCE_ID, source_id_operand=UUID(int=2), rule_index=0
        ),
        normalize_rule(
            UUID(int=3), RuleKind.PATH_GLOB, text_operand="attachments/**/*.tmp", rule_index=1
        ),
        normalize_rule(UUID(int=4), RuleKind.MAXIMUM_SIZE, size_bytes_operand=1024, rule_index=2),
    )
    snapshot = sign_policy_snapshot(
        _material(rules=rules), signer=_FakeSigner(), verifier=_FakeVerifier()
    )
    assert b'"source_id"' in snapshot.payload_bytes
    assert b'"path_glob"' in snapshot.payload_bytes
    assert b'"maximum_size_bytes"' in snapshot.payload_bytes


def test_sign_policy_snapshot_signature_covers_domain_separated_payload() -> None:
    signer = _FakeSigner()
    snapshot = sign_policy_snapshot(_material(), signer=signer, verifier=_FakeVerifier())
    message = b"exclusion-policy-snapshot/v1\x00" + snapshot.payload_bytes
    assert signer.calls == [message]


def test_sign_policy_snapshot_rejects_failed_verification() -> None:
    with pytest.raises(ExclusionPolicyError) as raised:
        sign_policy_snapshot(
            _material(), signer=_FakeSigner(), verifier=_FakeVerifier(accept=False)
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert not raised.value.safe_details


def test_sign_policy_snapshot_maps_signer_crash_to_signing_unavailable() -> None:
    with pytest.raises(ExclusionPolicyError) as raised:
        sign_policy_snapshot(
            _material(), signer=_FakeSigner(fail=True), verifier=_FakeVerifier()
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert raised.value.__cause__ is not None


def test_signed_snapshot_rejects_wrong_geometry() -> None:
    with pytest.raises(ValueError):
        SignedPolicySnapshot(
            payload_bytes=b"{}",
            payload_sha256="nothex",
            key_id=_FAKE_KEY_ID,
            signature_bytes=b"\x00" * 64,
        )
    with pytest.raises(ValueError):
        SignedPolicySnapshot(
            payload_bytes=b"{}",
            payload_sha256="d" * 64,
            key_id="wrong",
            signature_bytes=b"\x00" * 64,
        )
    with pytest.raises(ValueError):
        SignedPolicySnapshot(
            payload_bytes=b"",
            payload_sha256="d" * 64,
            key_id=_FAKE_KEY_ID,
            signature_bytes=b"\x00" * 64,
        )
    with pytest.raises(ValueError):
        SignedPolicySnapshot(
            payload_bytes=b"{}",
            payload_sha256="d" * 64,
            key_id=_FAKE_KEY_ID,
            signature_bytes=b"\x00" * 63,
        )


# --- service -------------------------------------------------------------------------


def _service(
    store: _RecordingStore, *, metrics: InMemoryExclusionPolicyMetrics | None = None
) -> ExclusionPolicyPublicationService:
    return ExclusionPolicyPublicationService(
        store=store,
        signer=_FakeSigner(),
        verifier=_FakeVerifier(),
        metrics=metrics,
    )


def test_publish_validates_confirmation_before_any_store_call() -> None:
    store = _RecordingStore()
    with pytest.raises(ExclusionPolicyError) as raised:
        asyncio.run(
            _service(store).publish(
                _command(confirmation="publish exclusion policy"), _context()
            )
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID
    assert store.resolve_calls == 0
    assert store.commit_calls == 0


def test_publish_requires_a_user_actor() -> None:
    store = _RecordingStore()
    with pytest.raises(ExclusionPolicyError) as raised:
        asyncio.run(
            _service(store).publish(
                _command(actor=PolicyActor(actor_kind=PolicyActorKind.SYSTEM)), _context()
            )
        )
    _assert_is_input_invalid(raised.value, "actor_invalid")
    assert store.resolve_calls == 0


def test_publish_returns_exact_replay_without_committing() -> None:
    replay = _result(is_replay=True)
    store = _RecordingStore(resolved=replay)
    metrics = InMemoryExclusionPolicyMetrics()
    result = asyncio.run(_service(store, metrics=metrics).publish(_command(), _context()))
    assert result is replay
    assert store.commit_calls == 0
    assert metrics.publication_count(PublicationMetricOutcome.REPLAYED) == 1
    assert metrics.publication_count(PublicationMetricOutcome.PUBLISHED) == 0


def test_publish_commits_with_canonical_fingerprint_and_builder() -> None:
    committed = _result()
    store = _RecordingStore(committed=committed)
    metrics = InMemoryExclusionPolicyMetrics()
    command = _command()
    result = asyncio.run(_service(store, metrics=metrics).publish(command, _context()))
    assert result is committed
    assert metrics.publication_count(PublicationMetricOutcome.PUBLISHED) == 1
    assert store.seen_fingerprints == [
        compute_publication_request_fingerprint(command).hexadecimal
    ] * 2
    assert len(store.builder_results) == 1


def test_publish_records_replay_metric_for_recovered_commit() -> None:
    store = _RecordingStore(committed=_result(is_replay=True))
    metrics = InMemoryExclusionPolicyMetrics()
    asyncio.run(_service(store, metrics=metrics).publish(_command(), _context()))
    assert metrics.publication_count(PublicationMetricOutcome.REPLAYED) == 1
    assert metrics.publication_count(PublicationMetricOutcome.PUBLISHED) == 0


def test_publish_records_rejected_metric_for_business_rejection() -> None:
    store = _RecordingStore(
        commit_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED)
    )
    metrics = InMemoryExclusionPolicyMetrics()
    with pytest.raises(ExclusionPolicyError):
        asyncio.run(_service(store, metrics=metrics).publish(_command(), _context()))
    assert metrics.publication_count(PublicationMetricOutcome.REJECTED) == 1


def test_publish_records_no_metric_for_unknown_outcome() -> None:
    store = _RecordingStore(
        commit_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)
    )
    metrics = InMemoryExclusionPolicyMetrics()
    with pytest.raises(ExclusionPolicyError):
        asyncio.run(_service(store, metrics=metrics).publish(_command(), _context()))
    assert metrics.publication_records() == []


def test_publication_store_protocol_accepts_the_double() -> None:
    store: PolicyPublicationStore = _RecordingStore()
    assert store is not None


def test_signature_algorithm_constant_untouched() -> None:
    assert SIGNATURE_ALGORITHM == "Ed25519"
