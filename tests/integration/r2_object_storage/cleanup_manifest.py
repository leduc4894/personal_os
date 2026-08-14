"""Per-run exact-key cleanup manifest and validation for the live R2 harness.

Design section 16.3: content-addressed test objects keep the production key
grammar, so cleanup can never use a run prefix or a wildcard. The manifest
records every exact canonical key the CURRENT run created, and
:func:`validate_cleanup_deletions` proves — before any network call — that a
requested deletion targets exactly this run's recorded keys in exactly the
dedicated test bucket, under the only canonical SHA-256 key grammar
(``objects/sha256/{2}/{2}/{64 hex}`` with shards matching the digest).

This module is harness-local: it lives in the test package, never in
``r2_object_storage``, and together with the harness fixture's low-level
``delete_object`` call it is the only deletion code in the repository.
Rejection reasons are closed safe tokens; bucket names, endpoints and secret
values never enter any failure path here.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from personal_os.diagnostics.events import SafeToken

#: The only canonical object-key grammar (design section 5): the shards must
#: match the first four characters of the trailing 64-hex digest.
_CANONICAL_KEY_PATTERN: Final = re.compile(
    r"^objects/sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})$"
)
#: Characters a delete request must never carry (wildcard or placeholder forms).
_WILDCARD_CHARACTERS: Final = ("*", "?", "%")

REJECTION_BUCKET_MISMATCH: Final[SafeToken] = SafeToken.parse("cleanup_bucket_mismatch")
REJECTION_NONCANONICAL_KEY: Final[SafeToken] = SafeToken.parse("cleanup_key_noncanonical")
REJECTION_UNRECORDED_KEY: Final[SafeToken] = SafeToken.parse("cleanup_key_unrecorded")
REJECTION_WILDCARD_KEY: Final[SafeToken] = SafeToken.parse("cleanup_key_wildcard")


class CleanupRejection(ValueError):
    """A cleanup request violated the exact-key contract before any delete call.

    ``reason`` is one of the closed safe tokens above; no bucket name, endpoint,
    secret or full key value is ever attached.
    """

    def __init__(self, reason: SafeToken) -> None:
        super().__init__(f"live cleanup rejected: {reason.value}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CreatedObjectRecord:
    """One exact canonical key the current run created, with reverify metadata."""

    key: str
    digest_hexadecimal: str
    size_bytes: int
    media_type: str


class LiveCleanupManifest:
    """In-memory allowlist of exactly the keys the current run created.

    The manifest is created once per test run against one dedicated test bucket.
    Recording is idempotent per key and never invents a key: only callers that
    observed a successful create (a store receipt or a harness-written object)
    record here. ``recorded_keys`` returns insertion order for deterministic
    cleanup.
    """

    def __init__(self, *, bucket_name: str, run_nonce: str) -> None:
        self._bucket_name = bucket_name
        self._run_nonce = run_nonce
        self._created: dict[str, CreatedObjectRecord] = {}

    @property
    def bucket_name(self) -> str:
        """The dedicated test bucket every recorded key was created in."""

        return self._bucket_name

    @property
    def run_nonce(self) -> str:
        """Per-run random identity binding recorded payloads to this run."""

        return self._run_nonce

    def record_created(self, record: CreatedObjectRecord) -> None:
        """Record one exact canonical key created by the current run."""

        self._created.setdefault(record.key, record)

    def recorded_keys(self) -> tuple[str, ...]:
        """Every recorded key in insertion (creation) order."""

        return tuple(self._created)

    def record_for(self, key: str) -> CreatedObjectRecord | None:
        """The recorded entry for ``key``, or ``None`` when unrecorded."""

        return self._created.get(key)

    def __len__(self) -> int:
        return len(self._created)


def _is_canonical_key(key: str) -> bool:
    """Require the exact canonical grammar with shards matching the digest."""

    match = _CANONICAL_KEY_PATTERN.fullmatch(key)
    if match is None:
        return False
    digest = match.group(3)
    return match.group(1) == digest[:2] and match.group(2) == digest[2:4]


def validate_cleanup_deletions(
    manifest: LiveCleanupManifest,
    *,
    bucket_name: str,
    keys: Sequence[str],
) -> tuple[str, ...]:
    """Validate every requested deletion against the exact-key contract.

    All four rejections happen BEFORE any delete call exists in the calling
    pipeline: a wrong (non-test) bucket, a noncanonical key, an unrecorded key
    and any wildcard character each raise :class:`CleanupRejection` without the
    caller having issued a single network request. Returns the validated keys
    in request order.
    """

    if bucket_name != manifest.bucket_name:
        raise CleanupRejection(REJECTION_BUCKET_MISMATCH)
    validated: list[str] = []
    for key in keys:
        if any(character in key for character in _WILDCARD_CHARACTERS):
            raise CleanupRejection(REJECTION_WILDCARD_KEY)
        if not _is_canonical_key(key):
            raise CleanupRejection(REJECTION_NONCANONICAL_KEY)
        if manifest.record_for(key) is None:
            raise CleanupRejection(REJECTION_UNRECORDED_KEY)
        validated.append(key)
    return tuple(validated)


async def run_exact_key_cleanup(
    manifest: LiveCleanupManifest,
    *,
    bucket_name: str,
    keys: Sequence[str],
    delete_one: Callable[[str], Awaitable[None]],
) -> tuple[str, ...]:
    """Validate first, then delete exactly the validated keys via ``delete_one``.

    ``delete_one`` is the harness-local low-level delete call; it is invoked
    once per validated key only after :func:`validate_cleanup_deletions`
    accepted every requested key. A rejection therefore proves no delete call
    ran at all.
    """

    validated = validate_cleanup_deletions(manifest, bucket_name=bucket_name, keys=keys)
    for key in validated:
        await delete_one(key)
    return validated


def short_key_prefix(key: str) -> str:
    """Render only the first 12 hex characters of a key's digest prefix.

    Cleanup diagnostics (design section 16.3) may report shortened digest
    prefixes only — never a full key, bucket name or endpoint.
    """

    return key.rsplit("/", 1)[-1][:12]


def compose_live_environment(
    environment: Mapping[str, str],
    *,
    secret_root: str,
    access_key_file_name: str,
    secret_access_key_file_name: str,
    spool_root: str,
) -> dict[str, str]:
    """Compose the loader's environment from the dedicated test variables.

    Maps the harness surface (``R2_TEST_ENDPOINT``, ``R2_TEST_BUCKET_NAME``,
    the secret root and the two secret file names) onto the exact
    ``KNOWLEDGE_*`` names :func:`r2_object_storage.settings.load_object_storage_settings`
    reads. Only these names are passed, so ambient ``KNOWLEDGE_*`` or AWS
    variables have no effect on the live harness.
    """

    return {
        "KNOWLEDGE_R2_ENDPOINT": environment["R2_TEST_ENDPOINT"],
        "KNOWLEDGE_R2_BUCKET_NAME": environment["R2_TEST_BUCKET_NAME"],
        "KNOWLEDGE_SECRET_ROOT": secret_root,
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE": access_key_file_name,
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE": secret_access_key_file_name,
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT": spool_root,
    }
