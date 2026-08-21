"""Static boundary contract: PostgreSQL transactions touch no object store or Temporal.

Every module of the ``postgresql_source_store`` adapter is parsed (never
imported) and must keep the canonical transaction boundary: no import root may
reach Temporal, the R2 adapter, a composition root, an HTTP client library or a
raw socket API, and no identifier or call inside the transaction modules may
name an object-storage or Temporal operation. The typed
``personal_os.object_storage`` contracts imported by the publication store are
domain value objects only — they perform no I/O — so they stay permitted,
while the ``r2_object_storage`` provider package is rejected outright.

A synthetic violating module is fed through the same scanner to prove the
scanner detects real violations rather than vacuously passing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_SOURCE_ROOT = (
    REPO_ROOT / "packages" / "postgresql-source-store" / "src" / "postgresql_source_store"
)

#: The modules that own the canonical PostgreSQL transaction paths.
TRANSACTION_MODULE_NAMES: Final[tuple[str, ...]] = (
    "engine.py",
    "locks.py",
    "publication_store.py",
    "projection_intents.py",
    "policy_enforcement.py",
    "canonical_read.py",
    "lifecycle_store.py",
)

#: Import roots that would bridge a transaction onto the network or Temporal.
FORBIDDEN_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "temporalio",
        "r2_object_storage",
        "workflow_worker",
        "api_runtime",
        "mcp_runtime",
        "boto3",
        "botocore",
        "aiobotocore",
        "aiohttp",
        "httpx",
        "requests",
        "urllib",
        "urllib3",
        "socket",
        "ssl",
        "http",
        "ftplib",
        "fastapi",
        "smtplib",
    }
)

#: Identifier tokens naming an object store or Temporal surface; no variable,
#: function, parameter or attribute in the transaction modules may use one.
FORBIDDEN_IDENTIFIER_TOKENS: Final[frozenset[str]] = frozenset(
    {"temporal", "temporalio", "s3", "boto", "r2"}
)

#: Call names of object-storage and Temporal client operations.
FORBIDDEN_CALL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "put_object",
        "get_object",
        "delete_object",
        "upload_fileobj",
        "download_fileobj",
        "start_workflow",
        "signal_workflow",
        "query_workflow",
    }
)

_VIOLATING_MODULE_SOURCE: Final[str] = '''
"""Synthetic violating module for the scanner self-check."""
import socket
import aiohttp
import boto3
import fastapi
from temporalio.client import Client
from r2_object_storage import bucket

async def dispatch(client: Client, r2_bucket: object) -> None:
    temporal_handle = client.get_handle("x")
    await temporal_handle.start_workflow("x")
    bucket.put_object(b"bytes")
    socket.create_connection(("127.0.0.1", 1))
'''


def _iter_module_paths() -> list[Path]:
    return sorted(path for path in ADAPTER_SOURCE_ROOT.rglob("*.py") if path.name != "__init__.py")


def _import_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            roots.add(node.module.split(".", maxsplit=1)[0])
    return roots


def _identifiers(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _call_names(tree: ast.Module) -> set[str]:
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name):
                calls.add(function.id)
            elif isinstance(function, ast.Attribute):
                calls.add(function.attr)
    return calls


def _identifiers_matching_tokens(tree: ast.Module, tokens: frozenset[str]) -> set[str]:
    """Return every identifier that contains one of the forbidden tokens.

    Matching is by substring because a violation rarely names the token alone
    (``temporal_client``, ``r2_bucket``); the token set has no benign substring
    inside the adapter's own vocabulary."""

    matching: set[str] = set()
    for identifier in _identifiers(tree):
        if any(token in identifier for token in tokens):
            matching.add(identifier)
    return matching


def test_scanner_detects_a_violating_module() -> None:
    tree = ast.parse(_VIOLATING_MODULE_SOURCE)
    roots = _import_roots(tree)
    identifiers = _identifiers_matching_tokens(tree, FORBIDDEN_IDENTIFIER_TOKENS)
    calls = _call_names(tree)
    assert FORBIDDEN_IMPORT_ROOTS & roots == {
        "aiohttp",
        "boto3",
        "fastapi",
        "socket",
        "temporalio",
        "r2_object_storage",
    }
    assert {identifier.split("_")[0] for identifier in identifiers} == {"temporal", "r2"}
    assert FORBIDDEN_CALL_NAMES & calls == {"start_workflow", "put_object"}


def test_adapter_modules_import_no_object_storage_temporal_or_network_library() -> None:
    offenders: list[str] = []
    scanned_imports: set[str] = set()
    for path in _iter_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanned_imports.update(_import_roots(tree))
        violating = FORBIDDEN_IMPORT_ROOTS & _import_roots(tree)
        if violating:
            offenders.append(f"{path.name}: imports {sorted(violating)}")
    assert {"fastapi", "aiohttp", "boto3"}.isdisjoint(scanned_imports)
    assert not offenders, (
        "adapter modules must not import network or Temporal roots:\n" + "\n".join(offenders)
    )


def test_transaction_modules_never_name_or_call_storage_or_temporal_operations() -> None:
    offenders: list[str] = []
    for path in _iter_module_paths():
        if path.name not in TRANSACTION_MODULE_NAMES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offending_identifiers = _identifiers_matching_tokens(tree, FORBIDDEN_IDENTIFIER_TOKENS)
        if offending_identifiers:
            offenders.append(f"{path.name}: identifiers {sorted(offending_identifiers)}")
        offending_calls = FORBIDDEN_CALL_NAMES & _call_names(tree)
        if offending_calls:
            offenders.append(f"{path.name}: calls {sorted(offending_calls)}")
    assert not offenders, (
        "transaction modules must never name or call object storage or Temporal:\n"
        + "\n".join(offenders)
    )
