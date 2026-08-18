"""Draft-service validation, version semantics and semantic hashing.

These tests pin the provider-neutral draft orchestration over an in-memory
port double: the service rejects the closed validation failures before any
store call (rule-count ceiling, duplicate rule IDs, duplicate semantics),
delegates exact-version replacement and status reads, and never inspects or
transforms rule operands itself. The draft semantic hash is pinned as a
deterministic, order-insensitive SHA-256 over the canonical rule rendering so
preview bindings (spec 9/10) share one frozen value.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from tests.unit.exclusion_policy.fakes import extension_rule, rule

from personal_os.diagnostics.context import DiagnosticContext

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.events import SafeToken
from personal_os.diagnostics.trace_context import SpanId, TraceContext, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    MAXIMUM_RULES_PER_REVISION,
    ExclusionRule,
    RuleKind,
)
from personal_os.exclusion_policy.drafts import (
    PolicyDraftService,
    compute_draft_semantic_sha256,
    validate_draft_rules,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.ports import (
    PolicyActor,
    PolicyActorKind,
    PolicyDraft,
    PolicyDraftStore,
    PolicyQueryStore,
    PolicyStatus,
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000d1")
DRAFT_ID = UUID("018f47a0-7b00-7000-8000-0000000000d2")
USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000d3")

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


def _actor() -> PolicyActor:
    return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=USER_ID)


def _draft(version: int, *rules: ExclusionRule) -> PolicyDraft:
    return PolicyDraft(
        draft_id=DRAFT_ID,
        workspace_id=WORKSPACE_ID,
        draft_version=version,
        base_policy_revision_id=None,
        rules=rules,
    )


class RecordingDraftStore:
    """In-memory ``PolicyDraftStore`` double recording every replace call."""

    def __init__(self, draft: PolicyDraft) -> None:
        self.draft = draft
        self.replace_calls: list[tuple[UUID, int, tuple[ExclusionRule, ...], PolicyActor]] = []

    async def load_draft(self, workspace_id: UUID, context: DiagnosticContext) -> PolicyDraft:
        assert workspace_id == WORKSPACE_ID
        return self.draft

    async def replace_rules(
        self,
        draft_id: UUID,
        expected_draft_version: int,
        rules: tuple[ExclusionRule, ...],
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyDraft:
        if expected_draft_version != self.draft.draft_version:
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT,
                safe_details={"current_draft_version": self.draft.draft_version},
            )
        self.replace_calls.append((draft_id, expected_draft_version, rules, actor))
        self.draft = _draft(expected_draft_version + 1, *rules)
        return self.draft


class RecordingQueryStore:
    """In-memory ``PolicyQueryStore`` double recording every status call."""

    def __init__(self, draft: PolicyDraft) -> None:
        self.status = PolicyStatus(
            workspace_id=WORKSPACE_ID,
            active_policy_revision_id=None,
            active_revision_number=0,
            draft=draft,
        )
        self.calls: list[UUID] = []

    async def get_policy_status(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyStatus:
        self.calls.append(workspace_id)
        return self.status


def _service(store: RecordingDraftStore) -> PolicyDraftService:
    draft_store: PolicyDraftStore = store
    query_store: PolicyQueryStore = RecordingQueryStore(store.draft)
    return PolicyDraftService(draft_store=draft_store, query_store=query_store)


# --- validate_draft_rules closed rejections -------------------------------------


def test_validate_draft_rules_rejects_more_than_maximum_rules() -> None:
    rules = tuple(extension_rule(f".ext{index}") for index in range(MAXIMUM_RULES_PER_REVISION + 1))
    with pytest.raises(ExclusionPolicyError) as raised:
        validate_draft_rules(rules)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert raised.value.safe_details["reason"] == SafeToken.parse("rule_count_invalid")


def test_validate_draft_rules_rejects_duplicate_rule_ids() -> None:
    duplicate_id = uuid4()
    rules = (
        extension_rule(".tmp"),
        rule(RuleKind.EXTENSION, rule_id=duplicate_id, text_operand=".bak"),
        rule(RuleKind.MAXIMUM_SIZE, rule_id=duplicate_id, size_bytes_operand=1024),
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        validate_draft_rules(rules)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert raised.value.safe_details["reason"] == SafeToken.parse("rule_id_invalid")
    assert raised.value.safe_details["rule_index"] == 2


def test_validate_draft_rules_rejects_duplicate_semantic_fingerprints() -> None:
    rules = (extension_rule(".tmp"), extension_rule(".TMP"))
    with pytest.raises(ExclusionPolicyError) as raised:
        validate_draft_rules(rules)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert raised.value.safe_details["reason"] == SafeToken.parse("rule_fingerprint_duplicate")
    assert raised.value.safe_details["rule_index"] == 1


def test_validate_draft_rules_accepts_empty_and_unique_rule_lists() -> None:
    validate_draft_rules(())
    validate_draft_rules(
        (extension_rule(".tmp"), rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=1))
    )


# --- service orchestration -------------------------------------------------------


@pytest.mark.asyncio
async def test_service_load_draft_delegates_to_store() -> None:
    loaded_rule = extension_rule(".tmp")
    store = RecordingDraftStore(_draft(3, loaded_rule))
    service = _service(store)
    loaded = await service.load_draft(WORKSPACE_ID, _context())
    assert loaded == _draft(3, loaded_rule)


@pytest.mark.asyncio
async def test_service_replace_draft_rules_validates_before_store_call() -> None:
    store = RecordingDraftStore(_draft(1))
    service = _service(store)
    rules = tuple(extension_rule(f".ext{index}") for index in range(MAXIMUM_RULES_PER_REVISION + 1))
    with pytest.raises(ExclusionPolicyError):
        await service.replace_draft_rules(DRAFT_ID, 1, rules, _actor(), _context())
    assert store.replace_calls == []


@pytest.mark.asyncio
async def test_service_replace_draft_rules_passes_exact_version_and_actor() -> None:
    store = RecordingDraftStore(_draft(1))
    service = _service(store)
    rules = (extension_rule(".tmp"),)
    replaced = await service.replace_draft_rules(DRAFT_ID, 1, rules, _actor(), _context())
    assert store.replace_calls == [(DRAFT_ID, 1, rules, _actor())]
    assert replaced.draft_version == 2
    assert replaced.rules == rules


@pytest.mark.asyncio
async def test_service_replace_draft_rules_propagates_store_conflict() -> None:
    store = RecordingDraftStore(_draft(5))
    service = _service(store)
    with pytest.raises(ExclusionPolicyError) as raised:
        await service.replace_draft_rules(DRAFT_ID, 1, (), _actor(), _context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT
    assert raised.value.safe_details == {"current_draft_version": 5}
    assert store.replace_calls == []


@pytest.mark.asyncio
async def test_service_get_policy_status_delegates_to_query_store() -> None:
    query_store = RecordingQueryStore(_draft(2))
    service = PolicyDraftService(
        draft_store=RecordingDraftStore(_draft(2)), query_store=query_store
    )
    status = await service.get_policy_status(WORKSPACE_ID, _context())
    assert query_store.calls == [WORKSPACE_ID]
    assert status.active_revision_number == 0
    assert status.active_policy_revision_id is None
    assert status.draft.draft_id == DRAFT_ID


# --- policy actor shape ----------------------------------------------------------


def test_policy_actor_user_kind_requires_non_nil_user_id() -> None:
    with pytest.raises(ValueError):
        PolicyActor(actor_kind=PolicyActorKind.USER, user_id=None)
    with pytest.raises(ValueError):
        PolicyActor(actor_kind=PolicyActorKind.USER, user_id=UUID(int=0))


def test_policy_actor_system_kind_forbids_user_id() -> None:
    with pytest.raises(ValueError):
        PolicyActor(actor_kind=PolicyActorKind.SYSTEM, user_id=USER_ID)
    system = PolicyActor(actor_kind=PolicyActorKind.SYSTEM, user_id=None)
    assert system.actor_kind is PolicyActorKind.SYSTEM


# --- draft semantic hash ---------------------------------------------------------


def test_compute_draft_semantic_sha256_is_deterministic_and_order_insensitive() -> None:
    first = extension_rule(".tmp")
    second = rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=4096)
    assert compute_draft_semantic_sha256((first, second)) == compute_draft_semantic_sha256(
        (second, first)
    )
    assert compute_draft_semantic_sha256((first, second)) == compute_draft_semantic_sha256(
        (first, second)
    )


def test_compute_draft_semantic_sha256_is_sixty_four_lowercase_hex_characters() -> None:
    digest = compute_draft_semantic_sha256(())
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(character in "0123456789abcdef" for character in digest)


def test_compute_draft_semantic_sha256_distinguishes_rule_changes() -> None:
    first = extension_rule(".tmp")
    second = extension_rule(".bak")
    assert compute_draft_semantic_sha256(()) != compute_draft_semantic_sha256((first,))
    assert compute_draft_semantic_sha256((first,)) != compute_draft_semantic_sha256((second,))
    assert compute_draft_semantic_sha256((first,)) != compute_draft_semantic_sha256((first, second))
