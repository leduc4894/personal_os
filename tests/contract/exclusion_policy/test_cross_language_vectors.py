"""Cross-language golden-vector contract: one corpus, two replays, one hash.

The evaluator, keyset and snapshot golden corpora under
``tests/fixtures/exclusion_policy/`` are the shared Python/TypeScript
contract surfaces of spec 23.1/23.2: the Python unit suites and the Obsidian
plugin vitest suites both replay the identical bytes. This gate pins that
identity three ways. First, the SHA-256 of every fixture is pinned here —
the cross-language contract hash — so any corpus change must consciously
update this registry instead of silently drifting one language's replay.
Second, the TypeScript replay surfaces are proven to read exactly these
fixture files. Third, the real TypeScript replay suites execute through the
project's pinned vitest, so both languages demonstrably pass the same
vectors in the same run; a missing Node/pnpm toolchain fails the gate
rather than skipping it.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
FIXTURE_DIRECTORY: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "exclusion_policy"

#: The cross-language contract hash registry: fixture file name -> SHA-256.
#: Both language replay suites consume these exact bytes; changing a corpus
#: means changing this registry in the same commit.
CROSS_LANGUAGE_FIXTURE_SHA256: Final[dict[str, str]] = {
    "evaluator-golden.json": ("0bdf565641f623936398e168a6feee2a5e714da086c74a43cbdfa1f8e7afe38e"),
    "keyset-golden.json": ("73e8c62d3aaae3a9a4af7926800934d4c414127425767503168723a9a07111e1"),
    "snapshot-golden.json": ("af7cebb12ad54243ff08e1aea58cbcb4d1bd6cc46d6b649ce3cbc134cff47d74"),
}

#: The TypeScript replay suites ( beneath the plugin source tree) that must
#: read the fixture files, one entry per fixture.
TS_REPLAY_SOURCES: Final[dict[str, str]] = {
    "evaluator-golden.json": "apps/obsidian-plugin/src/exclusion-policy/evaluator.test.ts",
    "keyset-golden.json": "apps/obsidian-plugin/src/exclusion-policy/keyset.test.ts",
    "snapshot-golden.json": "apps/obsidian-plugin/src/exclusion-policy/snapshot.test.ts",
}

#: The plugin vitest suites the gate executes (the golden replays plus the
#: cache-replay suite that re-verifies snapshot fixtures offline).
TS_REPLAY_TEST_FILES: Final[tuple[str, ...]] = (
    "src/exclusion-policy/evaluator.test.ts",
    "src/exclusion-policy/keyset.test.ts",
    "src/exclusion-policy/snapshot.test.ts",
    "src/exclusion-policy/policy-cache.test.ts",
)

_FIXTURE_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[/\\](?P<fixture>evaluator|keyset|snapshot)-golden\.json"
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_golden_fixture_is_registered_and_hash_matches() -> None:
    present = sorted(path.name for path in FIXTURE_DIRECTORY.glob("*-golden.json"))
    assert present == sorted(CROSS_LANGUAGE_FIXTURE_SHA256), (
        "the cross-language registry and the fixture directory must list the same corpora"
    )
    for fixture_name, pinned_hash in CROSS_LANGUAGE_FIXTURE_SHA256.items():
        actual = _file_sha256(FIXTURE_DIRECTORY / fixture_name)
        assert actual == pinned_hash, (
            f"{fixture_name} drifted from the pinned cross-language contract hash; "
            "regenerate both language replays and update this registry together"
        )


def test_typescript_replay_suites_read_exactly_the_registered_fixtures() -> None:
    for fixture_name, relative_source in TS_REPLAY_SOURCES.items():
        source_path = REPO_ROOT / relative_source
        assert source_path.is_file(), f"the TypeScript replay surface {source_path} is missing"
        referenced = set(
            match.group("fixture") + "-golden.json"
            for match in _FIXTURE_REFERENCE_PATTERN.finditer(
                source_path.read_text(encoding="utf-8")
            )
        )
        assert fixture_name in referenced, (
            f"{relative_source} no longer replays {fixture_name}; the corpus has no "
            "TypeScript consumer and the cross-language contract is broken"
        )


def test_typescript_replay_of_the_shared_vectors_passes() -> None:
    """Execute the plugin's vitest replay of the shared corpora.

    The Python suite already replays these fixtures in-process; this gate
    proves the other language passes the identical bytes in the same run.
    Missing pnpm fails the gate — the cross-language contract never skips.
    """
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        pytest.fail("pnpm is required to execute the TypeScript replay of the shared vectors")
    command = [
        "pnpm",
        "--filter",
        "@workspace/obsidian-plugin",
        "exec",
        "vitest",
        "run",
        *TS_REPLAY_TEST_FILES,
    ]
    if sys.platform == "win32":
        command = ["cmd.exe", "/c", pnpm, *command[1:]]
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "the TypeScript replay of the shared golden vectors failed:\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
