"""Static lock-order contract for source/publication policy enforcement.

The frozen global row-lock order is: publication idempotency advisory lock,
then the ``workspace_policy_state`` row, then the source advisory lock /
source rows. Policy publication takes its own idempotency advisory lock
before the policy-state row and never acquires source rows; reconciliation
paths never hold the policy-state lock while acquiring source rows. These
AST-order proofs pin that order inside the exact functions that own the
transactions, so an inverse-order refactor trips the contract instead of a
deadlock in production. A synthetic violating module fed through the same
scanner proves the scanner detects real reorderings.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = (
    REPO_ROOT / "packages" / "postgresql-source-store" / "src" / "postgresql_source_store"
)

_VIOLATING_MODULE_SOURCE = '''
"""Synthetic violating module for the scanner self-check."""
async def _run_locked_transition(self, command, fingerprint, receipt, context, transition):
    await connection.execute(source_lock_statement(command.source_id))
    await evaluate_locked_policy_decision(
        connection, workspace_id=command.workspace_id
    )
    await connection.execute(
        idempotency_lock_statement(command.workspace_id, command.idempotency_key)
    )
'''


def _parse(module_name: str) -> ast.Module:
    return ast.parse(
        (ADAPTER_ROOT / module_name).read_text(encoding="utf-8"),
        filename=str(ADAPTER_ROOT / module_name),
    )


def _functions(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    functions: dict[str, ast.AsyncFunctionDef | ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)
    return functions


def _ordered_call_names(node: ast.AST) -> list[str]:
    """Every called name in true source order (not ast.walk's BFS)."""

    names: list[str] = []

    def visit(inner: ast.AST) -> None:
        if isinstance(inner, ast.Call):
            function = inner.func
            if isinstance(function, ast.Attribute):
                names.append(function.attr)
            elif isinstance(function, ast.Name):
                names.append(function.id)
        for child in ast.iter_child_nodes(inner):
            visit(child)

    visit(node)
    return names


def _index_of(names: list[str], call_name: str) -> int:
    indexes = [index for index, name in enumerate(names) if name == call_name]
    return indexes[0] if indexes else -1


def _expanded_call_names(
    tree: ast.Module, function: ast.AsyncFunctionDef | ast.FunctionDef, depth: int = 3
) -> list[str]:
    """Ordered called names with same-module helper calls inlined in place.

    Private helpers (``self._helper(...)`` and module-level builders) are
    spliced at their call site, so lock statements executed two or three
    calls deep still appear in the caller's true execution order. Recursion
    stops at ``depth`` zero or on names without a same-module definition.
    """

    functions = _functions(tree)
    names: list[str] = []

    def visit(inner: ast.AST, remaining: int) -> None:
        if isinstance(inner, ast.Call):
            function_node = inner.func
            called: str | None = None
            if isinstance(function_node, ast.Attribute):
                called = function_node.attr
            elif isinstance(function_node, ast.Name):
                called = function_node.id
            if called is not None:
                names.append(called)
                if remaining > 0:
                    helper = functions.get(called)
                    if helper is not None and helper is not function:
                        for child in ast.iter_child_nodes(helper):
                            visit(child, remaining - 1)
        for child in ast.iter_child_nodes(inner):
            visit(child, remaining)

    visit(function, depth)
    return names


def test_scanner_detects_an_inverse_order_module() -> None:
    tree = ast.parse(_VIOLATING_MODULE_SOURCE)
    function = _functions(tree)["_run_locked_transition"]
    names = _ordered_call_names(function)
    assert _index_of(names, "source_lock_statement") < _index_of(
        names, "evaluate_locked_policy_decision"
    )
    assert _index_of(names, "evaluate_locked_policy_decision") < _index_of(
        names, "idempotency_lock_statement"
    )


def test_source_commit_locks_idempotency_policy_then_source_in_order() -> None:
    tree = _parse("publication_store.py")
    transition = _functions(tree)["_run_locked_transition"]
    names = _ordered_call_names(transition)
    idempotency = _index_of(names, "idempotency_lock_statement")
    policy = _index_of(names, "evaluate_locked_policy_decision")
    source = _index_of(names, "source_lock_statement")
    assert -1 not in (idempotency, policy, source), (
        "the locked prefix must keep all three serialization points"
    )
    assert idempotency < policy < source, (
        "lock order must stay idempotency advisory -> policy-state row -> source advisory"
    )


def test_source_commit_subject_rebuild_precedes_the_policy_recheck() -> None:
    tree = _parse("publication_store.py")
    transition = _functions(tree)["_run_locked_transition"]
    names = _ordered_call_names(transition)
    subject = _index_of(names, "_build_authoritative_subject")
    policy = _index_of(names, "evaluate_locked_policy_decision")
    assert subject != -1 and subject < policy, (
        "the authoritative subject must be rebuilt before the locked recheck evaluates it"
    )


def test_policy_publication_locks_idempotency_then_state_and_never_source_rows() -> None:
    tree = _parse("policy_publication.py")
    commit = _functions(tree)["_commit_publication_once"]
    names = _expanded_call_names(tree, commit)
    idempotency = _index_of(names, "policy_idempotency_lock_statement")
    state = _index_of(names, "policy_state_lock_statement")
    assert -1 not in (idempotency, state)
    assert idempotency < state, (
        "policy publication must lock its idempotency advisory lock before the state row"
    )
    assert "source_lock_statement" not in names, (
        "policy publication must never acquire the source lock family"
    )
    assert "idempotency_lock_statement" not in names, (
        "policy publication must never acquire the source idempotency lock family"
    )


def test_enforcement_locks_state_row_before_loading_the_snapshot() -> None:
    tree = _parse("policy_enforcement.py")
    loader = _functions(tree)["load_locked_active_policy_snapshot"]
    names = _ordered_call_names(loader)
    lock = _index_of(names, "policy_state_lock_statement")
    snapshot = _index_of(names, "active_policy_snapshot_select_statement")
    assert -1 not in (lock, snapshot)
    assert lock < snapshot, "the policy-state row lock must precede the snapshot load"


def test_canonical_read_enforces_inside_the_resolving_transaction() -> None:
    tree = _parse("canonical_read.py")
    resolve = _functions(tree)["resolve_current"]
    names = _ordered_call_names(resolve)
    lookup = _index_of(names, "current_reference_lookup_statement")
    policy = _index_of(names, "evaluate_locked_policy_decision")
    assert -1 not in (lookup, policy)
    assert lookup < policy, "the read must resolve source state and then recheck policy in-tx"
    assert "source_lock_statement" not in names, "reads never acquire the source lock family"


def test_projection_paths_never_lock_the_policy_state_row() -> None:
    tree = _parse("projection_intents.py")
    module_names = _ordered_call_names(tree)
    assert "policy_state_lock_statement" not in module_names, (
        "projection lease paths must never hold the policy-state lock"
    )
