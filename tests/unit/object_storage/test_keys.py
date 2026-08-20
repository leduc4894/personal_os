"""Tests for the closed canonical object-key grammar."""

from __future__ import annotations

import pytest

from personal_os.object_storage import (
    CanonicalObjectKey,
    ContentDigest,
    derive_canonical_object_key,
)

_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_parses_a_derived_canonical_object_key() -> None:
    derived_key = derive_canonical_object_key(ContentDigest.parse(_DIGEST))

    assert CanonicalObjectKey.parse(derived_key.value) == derived_key


@pytest.mark.parametrize(
    "value",
    [
        "objects/sha256/00/00/not-a-digest",
        f"objects/sha256/00/b0/{_DIGEST}",
        f"objects/sha256/e3/b0/{_DIGEST.upper()}",
        f"objects/sha256/e3/b0/{_DIGEST}/surplus",
    ],
)
def test_rejects_noncanonical_object_key_paths(value: str) -> None:
    with pytest.raises(ValueError):
        CanonicalObjectKey.parse(value)
