"""Static boundary contract: policy enforcement stays fail-closed and internal.

The domain enforcement module must stay infrastructure-free (no SQLAlchemy,
psycopg, FastAPI, Uvicorn, cryptography or composition-root import), the
internal ``PolicyDecision`` evidence must never surface in the HTTP API
runtime (OpenAPI models, routes, settings), the publication service must call
the policy guard before any object-store access inside one invocation, and
the read service must re-check the guard after state resolution and before
the verified reader opens. A synthetic violating module fed through the same
scanner proves the scanner detects real violations.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[3]
DOMAIN_ROOT = REPO_ROOT / "src" / "personal_os" / "exclusion_policy"
SOURCES_ROOT = REPO_ROOT / "src" / "personal_os" / "sources"
API_RUNTIME_ROOT = REPO_ROOT / "apps" / "api" / "src" / "api_runtime"

#: Import roots the domain enforcement module must never reach.
FORBIDDEN_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "sqlalchemy",
        "psycopg",
        "alembic",
        "fastapi",
        "uvicorn",
        "temporalio",
        "redis",
        "boto3",
        "botocore",
        "aiobotocore",
        "cryptography",
        "api_runtime",
        "mcp_runtime",
        "workflow_worker",
        "r2_object_storage",
        "postgresql_source_store",
    }
)

#: Object-store member calls the publication flow may make only after the guard.
OBJECT_STORE_CALLS: Final[frozenset[str]] = frozenset(
    {"resolve_verified_object", "store_stream", "verify_existing_object", "open_verified_reader"}
)

_VIOLATING_MODULE_SOURCE: Final[str] = '''
"""Synthetic violating module for the scanner self-check."""
import sqlalchemy
from fastapi import APIRouter

router = APIRouter()

@router.get("/policy-decision")
def leak_decision() -> dict[str, object]:
    return {"policy_decision": "evidence"}
'''

_INTERNAL_POLICY_DECISION_IDENTIFIER: Final[re.Pattern[str]] = re.compile(
    r"\b(?:PolicyDecision|policy_decision)\b"
)


def _import_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            roots.add(node.module.split(".", maxsplit=1)[0])
    return roots


def _functions(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    functions: dict[str, ast.AsyncFunctionDef | ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)
    return functions


def _call_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for walked in ast.walk(node):
        if isinstance(walked, ast.Call):
            function = walked.func
            if isinstance(function, ast.Attribute):
                names.append(function.attr)
            elif isinstance(function, ast.Name):
                names.append(function.id)
    return names


def test_scanner_detects_a_violating_module() -> None:
    tree = ast.parse(_VIOLATING_MODULE_SOURCE)
    assert FORBIDDEN_IMPORT_ROOTS & _import_roots(tree) == {"sqlalchemy", "fastapi"}
    assert _INTERNAL_POLICY_DECISION_IDENTIFIER.search(_VIOLATING_MODULE_SOURCE) is not None


def test_domain_enforcement_imports_no_infrastructure() -> None:
    tree = ast.parse(
        (DOMAIN_ROOT / "enforcement.py").read_text(encoding="utf-8"),
        filename=str(DOMAIN_ROOT / "enforcement.py"),
    )
    violating = FORBIDDEN_IMPORT_ROOTS & _import_roots(tree)
    assert not violating, f"enforcement.py must not import infrastructure: {sorted(violating)}"


def test_policy_decision_never_surfaces_in_the_api_runtime() -> None:
    offenders: list[str] = []
    for path in sorted(API_RUNTIME_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if _INTERNAL_POLICY_DECISION_IDENTIFIER.search(source) is not None:
            offenders.append(path.name)
    assert not offenders, (
        "the internal PolicyDecision evidence must never cross into the API runtime: "
        + ", ".join(offenders)
    )


def test_publication_service_guard_precedes_every_object_store_access() -> None:
    publication_tree = ast.parse(
        (SOURCES_ROOT / "publication.py").read_text(encoding="utf-8"),
        filename=str(SOURCES_ROOT / "publication.py"),
    )
    functions = _functions(publication_tree)
    publish_once = functions.get("_publish_once")
    assert publish_once is not None, "the publication service must keep its one-shot flow"
    statements = ast.unparse(publish_once)
    guard_index = statements.find("self.policy_guard.authorize_publication")
    assert guard_index >= 0, "the publication flow must call the policy guard"
    receipt_index = statements.find("self._obtain_verified_receipt")
    assert receipt_index >= 0, "the publication flow must obtain its receipt through the helper"
    assert guard_index < receipt_index, "the policy guard must run before object-store access"

    receipt_helper = functions.get("_obtain_verified_receipt")
    assert receipt_helper is not None
    assert OBJECT_STORE_CALLS & set(_call_names(receipt_helper)), (
        "the receipt helper must remain the only object-store access point"
    )
    # The one-shot flow itself never touches the object store: every
    # object-store member call lives behind the guarded receipt helper.
    publish_calls = _call_names(publish_once)
    assert not OBJECT_STORE_CALLS & set(publish_calls), (
        "object-store access must stay behind the guarded receipt helper"
    )


def test_read_service_guard_precedes_the_verified_reader() -> None:
    reading_tree = ast.parse(
        (SOURCES_ROOT / "reading.py").read_text(encoding="utf-8"),
        filename=str(SOURCES_ROOT / "reading.py"),
    )
    open_current = _functions(reading_tree).get("open_current_source")
    assert open_current is not None, "the read service must keep its open_current_source flow"
    statements = ast.unparse(open_current)
    guard_index = statements.find("self.policy_guard.authorize_read")
    reader_index = statements.find("self.object_store.open_verified_reader")
    assert guard_index >= 0, "the read flow must call the policy guard"
    assert reader_index >= 0, "the read flow must open the verified reader"
    assert guard_index < reader_index, "the policy guard must run before any object GET"
