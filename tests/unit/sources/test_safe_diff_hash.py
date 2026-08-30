"""Golden and exclusion tests for canonical safe diff hashes.

Fixture digests derive from the design spec's worked examples in
``tests/fixtures/source_publication/fingerprint_golden.json`` (pinned UUIDs,
64x'a'/64x'b' content digests); regenerate expectations only from the spec,
never from the implementation.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID

from personal_os.object_storage import ContentDigest
from personal_os.sources.fingerprint import SafeDiffHash, compute_safe_diff_hash

FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "source_publication" / "fingerprint_golden.json"
)


def _load_safe_diff_fixture() -> Mapping[str, object]:
    with FIXTURE_PATH.open("r", encoding="utf-8") as fixture_file:
        return cast(Mapping[str, object], json.load(fixture_file)["safe_diff"])


def test_safe_diff_fixture_matches_golden_hash() -> None:
    fixture = _load_safe_diff_fixture()

    safe_diff_hash = compute_safe_diff_hash(
        source_id=UUID(cast(str, fixture["source_id"])),
        base_version_id=UUID(cast(str, fixture["base_version_id"])),
        base_content_sha256=ContentDigest.parse(cast(str, fixture["base_content_sha256"])),
        new_content_sha256=ContentDigest.parse(cast(str, fixture["new_content_sha256"])),
    )

    assert safe_diff_hash == SafeDiffHash.parse(cast(str, fixture["expected_safe_diff_hash"]))


def test_safe_diff_signature_carries_only_safe_members() -> None:
    parameters = list(inspect.signature(compute_safe_diff_hash).parameters)

    assert parameters == [
        "source_id",
        "base_version_id",
        "base_content_sha256",
        "new_content_sha256",
    ]


def test_null_base_members_are_kept_for_create_style_diff() -> None:
    fixture = _load_safe_diff_fixture()
    base_present = compute_safe_diff_hash(
        source_id=UUID(cast(str, fixture["source_id"])),
        base_version_id=UUID(cast(str, fixture["base_version_id"])),
        base_content_sha256=ContentDigest.parse(cast(str, fixture["base_content_sha256"])),
        new_content_sha256=ContentDigest.parse(cast(str, fixture["new_content_sha256"])),
    )

    base_absent = compute_safe_diff_hash(
        source_id=UUID(cast(str, fixture["source_id"])),
        base_version_id=None,
        base_content_sha256=None,
        new_content_sha256=ContentDigest.parse(cast(str, fixture["new_content_sha256"])),
    )

    assert base_absent != base_present
    assert base_absent == compute_safe_diff_hash(
        source_id=UUID(cast(str, fixture["source_id"])),
        base_version_id=None,
        base_content_sha256=None,
        new_content_sha256=ContentDigest.parse(cast(str, fixture["new_content_sha256"])),
    )


def test_changed_content_digest_changes_safe_diff_hash() -> None:
    fixture = _load_safe_diff_fixture()
    original = compute_safe_diff_hash(
        source_id=UUID(cast(str, fixture["source_id"])),
        base_version_id=UUID(cast(str, fixture["base_version_id"])),
        base_content_sha256=ContentDigest.parse(cast(str, fixture["base_content_sha256"])),
        new_content_sha256=ContentDigest.parse(cast(str, fixture["new_content_sha256"])),
    )

    changed = compute_safe_diff_hash(
        source_id=UUID(cast(str, fixture["source_id"])),
        base_version_id=UUID(cast(str, fixture["base_version_id"])),
        base_content_sha256=ContentDigest.parse(cast(str, fixture["base_content_sha256"])),
        new_content_sha256=ContentDigest.parse("c" * 64),
    )

    assert changed != original
