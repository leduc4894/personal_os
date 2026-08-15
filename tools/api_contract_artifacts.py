"""API contract artifact gate: compare the committed OpenAPI snapshot, never rewrite it.

The tool accepts exactly one command, ``check``. It renders the deterministic
contract document offline through :func:`api_runtime.openapi_export.render_openapi_json`,
reads the fixed committed snapshot (or an injected ``Path``), compares the two
byte strings with :func:`hmac.compare_digest` and emits one fixed
``api_contract_current`` or ``api_contract_stale`` token on stdout — never
schema content and never filesystem paths. The check is non-mutating by
construction: stale bytes are reported, never rewritten, and a snapshot that
cannot be read is reported as stale with the same fixed token instead of a
traceback. The heavy FastAPI import happens inside the check so argument
errors never load the application stack.
"""

from __future__ import annotations

import hmac
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

#: Fixed committed snapshot location inside the repository worktree.
DEFAULT_SNAPSHOT: Final[Path] = (
    Path(__file__).resolve().parents[1] / "packages" / "api-client" / "openapi.json"
)

#: The only tokens the check ever prints; one line, no schema content or paths.
TOKEN_CONTRACT_CURRENT: Final[str] = "api_contract_current"
TOKEN_CONTRACT_STALE: Final[str] = "api_contract_stale"

_EXIT_CURRENT: Final[int] = 0
_EXIT_STALE: Final[int] = 1
_EXIT_USAGE: Final[int] = 2

_USAGE_ERROR: Final[str] = "api_contract_artifacts accepts exactly one command: check"


def check_snapshot(snapshot_path: Path = DEFAULT_SNAPSHOT) -> int:
    """Compare the snapshot bytes with the deterministic render, without writing.

    Returns ``0`` and prints ``api_contract_current`` when the snapshot bytes
    equal the render; returns ``1`` and prints ``api_contract_stale``
    otherwise, including when the snapshot cannot be read. The snapshot file
    is never modified.
    """
    from api_runtime.openapi_export import render_openapi_json

    try:
        committed = snapshot_path.read_bytes()
    except OSError:
        print(TOKEN_CONTRACT_STALE)
        return _EXIT_STALE
    expected = render_openapi_json()
    if hmac.compare_digest(committed, expected):
        print(TOKEN_CONTRACT_CURRENT)
        return _EXIT_CURRENT
    print(TOKEN_CONTRACT_STALE)
    return _EXIT_STALE


def main(argv: Sequence[str] | None = None) -> int:
    """Run the single supported command against the fixed committed snapshot."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments != ["check"]:
        print(_USAGE_ERROR, file=sys.stderr)
        return _EXIT_USAGE
    return check_snapshot()


if __name__ == "__main__":
    raise SystemExit(main())
