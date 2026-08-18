"""Draft orchestration: closed validation, semantic hashing and the service.

``validate_draft_rules`` is the provider-neutral gate every draft update
passes before any storage call: the rule-count ceiling, duplicate rule IDs
and duplicate semantic fingerprints (spec 6.1) each reject with exactly one
closed reason token plus the zero-based ``rule_index`` where known, and no
rule operand ever enters the typed error. ``compute_draft_semantic_sha256``
hashes the complete desired rule list over its canonical RFC 8785 rendering
into the frozen draft digest that preview bindings (Task 6) and audit
``safe_diff_hash`` values share.

:class:`PolicyDraftService` composes the draft and query ports: it validates
the entire list, then delegates the atomic full-list replacement — the
compare-and-swap, single version increment and ready-preview expiry live in
the store transaction, never here.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Final
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.exclusion_policy.canonical_json import (
    CanonicalJsonValue,
    canonicalize_json_value,
)
from personal_os.exclusion_policy.contracts import (
    MAXIMUM_RULES_PER_REVISION,
    ExactSourceIdOperand,
    ExclusionRule,
    ExtensionOperand,
    FolderPrefixOperand,
    MaximumSizeOperand,
    MediaTypeOperand,
    PathGlobOperand,
    SourceTypeOperand,
)
from personal_os.exclusion_policy.errors import (
    RULE_COUNT_INVALID,
    RULE_FINGERPRINT_DUPLICATE,
    RULE_ID_INVALID,
    input_invalid,
)
from personal_os.exclusion_policy.ports import (
    PolicyActor,
    PolicyDraft,
    PolicyDraftStore,
    PolicyQueryStore,
    PolicyStatus,
)
from personal_os.sources.actors import reject_nil_uuid

#: Contract tag hashed into every draft semantic digest.
DRAFT_HASH_CONTRACT: Final[str] = "exclusion_policy_draft/v1"


def _render_rule(rule: ExclusionRule) -> dict[str, CanonicalJsonValue]:
    """Render one rule as ``rule_id``, ``rule_kind`` and its single operand.

    Field names and rendering mirror the signed-snapshot rule objects of
    :mod:`personal_os.exclusion_policy.signatures` so the draft digest and
    the signed payload can never disagree about a rule's canonical form.
    """

    rendered: dict[str, CanonicalJsonValue] = {
        "rule_id": str(rule.rule_id),
        "rule_kind": rule.rule_kind.value,
    }
    operand = rule.operand
    if isinstance(operand, ExactSourceIdOperand):
        rendered["source_id"] = str(operand.source_id)
    elif isinstance(operand, FolderPrefixOperand):
        rendered["folder_prefix"] = operand.folder_prefix
    elif isinstance(operand, PathGlobOperand):
        rendered["path_glob"] = operand.normalized_pattern
    elif isinstance(operand, ExtensionOperand):
        rendered["extension"] = operand.extension
    elif isinstance(operand, MediaTypeOperand):
        exact = operand.exact_media_type
        rendered["media_type"] = exact.value if exact is not None else f"{operand.family_type}/*"
    elif isinstance(operand, MaximumSizeOperand):
        rendered["maximum_size_bytes"] = operand.maximum_size_bytes
    else:
        assert isinstance(operand, SourceTypeOperand)
        rendered["source_type"] = operand.source_type.value
    return rendered


def compute_draft_semantic_sha256(rules: tuple[ExclusionRule, ...]) -> str:
    """Hash the complete rule list into the frozen draft semantic digest.

    The digest is the lowercase SHA-256 over the RFC 8785 canonical JSON of
    the contract tag plus the rules sorted by textual ``rule_id``, so it is
    deterministic and independent of the caller's list order. It carries no
    workspace, draft identity or version: an unchanged rule list keeps its
    digest across edits, which is exactly what preview staleness compares
    alongside the version.
    """

    ordered_rules = sorted(rules, key=lambda rule: str(rule.rule_id))
    payload: dict[str, CanonicalJsonValue] = {
        "contract": DRAFT_HASH_CONTRACT,
        "rules": tuple(_render_rule(rule) for rule in ordered_rules),
    }
    return sha256(canonicalize_json_value(payload)).hexdigest()


def validate_draft_rules(rules: tuple[ExclusionRule, ...]) -> None:
    """Enforce the closed draft-list invariants (spec 6.1/9) or reject typed.

    The list may hold zero through 256 rules; a larger list, a duplicate
    ``rule_id`` or a duplicate semantic fingerprint (two rules excluding the
    same semantics under different IDs) rejects with the closed
    ``exclusion_policy_input_invalid`` reason token and, for duplicates, the
    zero-based index of the offending rule.
    """

    if len(rules) > MAXIMUM_RULES_PER_REVISION:
        raise input_invalid(RULE_COUNT_INVALID)
    rule_id_first_index: dict[UUID, int] = {}
    fingerprint_first_index: dict[str, int] = {}
    for index, rule in enumerate(rules):
        first_rule_index = rule_id_first_index.get(rule.rule_id)
        if first_rule_index is not None:
            raise input_invalid(RULE_ID_INVALID, index)
        rule_id_first_index[rule.rule_id] = index
        first_fingerprint_index = fingerprint_first_index.get(rule.semantic_fingerprint)
        if first_fingerprint_index is not None:
            raise input_invalid(RULE_FINGERPRINT_DUPLICATE, index)
        fingerprint_first_index[rule.semantic_fingerprint] = index


class PolicyDraftService:
    """Provider-neutral draft application service (spec 9).

    The service owns validation and orchestration only: every read and the
    atomic full-list replacement belong to the injected ports. A successful
    replacement returns the store's post-increment draft; a stale expected
    version crosses the boundary as the store's typed draft conflict without
    transformation.
    """

    def __init__(self, *, draft_store: PolicyDraftStore, query_store: PolicyQueryStore) -> None:
        self._draft_store = draft_store
        self._query_store = query_store

    async def load_draft(self, workspace_id: UUID, context: DiagnosticContext) -> PolicyDraft:
        """Return the workspace's working draft with its exact version."""

        reject_nil_uuid("workspace_id", workspace_id)
        return await self._draft_store.load_draft(workspace_id, context)

    async def replace_draft_rules(
        self,
        draft_id: UUID,
        expected_draft_version: int,
        rules: tuple[ExclusionRule, ...],
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyDraft:
        """Validate the complete desired list, then replace it atomically.

        The server never merges or silently drops rules: validation happens
        before any store call, and an identical replacement remains an
        explicit successful edit that increments the draft version once.
        """

        reject_nil_uuid("draft_id", draft_id)
        validate_draft_rules(rules)
        return await self._draft_store.replace_rules(
            draft_id, expected_draft_version, rules, actor, context
        )

    async def get_policy_status(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyStatus:
        """Return current published-revision metadata plus the working draft."""

        reject_nil_uuid("workspace_id", workspace_id)
        return await self._query_store.get_policy_status(workspace_id, context)
