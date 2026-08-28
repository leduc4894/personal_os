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
#: The only multipart staging-key grammar (spec 3.3): exactly the staging
#: prefix followed by 32 to 128 URL-safe base64url characters — a shape no
#: canonical ``objects/sha256/...`` key can satisfy, so a validated staging
#: key can never address canonical content and a canonical key can never pass
#: as staging material.
_STAGING_KEY_PATTERN: Final = re.compile(r"^staging/multipart/[A-Za-z0-9_-]{32,128}$")
#: Bounded length of one provider upload ID recorded with a staging resource.
_MAXIMUM_RECORDED_UPLOAD_ID_LENGTH: Final[int] = 1024
#: Characters a delete request must never carry (wildcard or placeholder forms).
_WILDCARD_CHARACTERS: Final = ("*", "?", "%")

REJECTION_BUCKET_MISMATCH: Final[SafeToken] = SafeToken.parse("cleanup_bucket_mismatch")
REJECTION_NONCANONICAL_KEY: Final[SafeToken] = SafeToken.parse("cleanup_key_noncanonical")
REJECTION_NONSTAGING_KEY: Final[SafeToken] = SafeToken.parse("cleanup_key_not_staging")
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


@dataclass(frozen=True, slots=True)
class CreatedStagingResourceRecord:
    """One exact staging key the current run created, with its upload IDs.

    The provider upload IDs of exactly this staging key that the current run
    observed (a create race may briefly mint more than one before the losers
    abort their own orphans). Every value stays private cleanup material: it
    is never rendered and only ever addresses this one key's uploads.
    """

    staging_key: str
    provider_upload_ids: tuple[str, ...] = ()


class LiveCleanupManifest:
    """In-memory allowlist of exactly the keys the current run created.

    The manifest is created once per test run against one dedicated test bucket.
    Recording is idempotent per key and never invents a key: only callers that
    observed a successful create (a store receipt or a harness-written object)
    record here. ``recorded_keys`` returns insertion order for deterministic
    cleanup.

    The same manifest also carries the run's exact multipart staging
    identities: a staging key is recorded the moment the composed service
    hands it toward its first provider mutation (before any provider byte
    moves), and every observed provider upload ID of that key is attached the
    moment it is known. Staging recording enforces the closed staging grammar,
    so a canonical-shaped value can never be recorded or cleaned as staging
    material.
    """

    def __init__(self, *, bucket_name: str) -> None:
        self._bucket_name = bucket_name
        self._created: dict[str, CreatedObjectRecord] = {}
        self._staging: dict[str, tuple[str, ...]] = {}

    @property
    def bucket_name(self) -> str:
        """The dedicated test bucket every recorded key was created in."""

        return self._bucket_name

    def record_created(self, record: CreatedObjectRecord) -> None:
        """Record one exact canonical key created by the current run."""

        self._created.setdefault(record.key, record)

    def recorded_keys(self) -> tuple[str, ...]:
        """Every recorded canonical key in insertion (creation) order."""

        return tuple(self._created)

    def record_for(self, key: str) -> CreatedObjectRecord | None:
        """The recorded entry for ``key``, or ``None`` when unrecorded."""

        return self._created.get(key)

    def record_staging_key(self, staging_key: str) -> None:
        """Record one exact staging key before its first provider mutation.

        The grammar check fails closed on any non-staging value (including
        every canonical key shape), so the staging allowlist can only ever
        hold genuine private staging identities of the current run.
        """

        if _STAGING_KEY_PATTERN.fullmatch(staging_key) is None:
            raise CleanupRejection(REJECTION_NONSTAGING_KEY)
        self._staging.setdefault(staging_key, ())

    def attach_staging_upload_id(self, staging_key: str, provider_upload_id: str) -> None:
        """Attach one observed provider upload ID to its recorded staging key.

        The staging key must already be recorded; an unknown key is the closed
        unrecorded rejection, and an oversized or empty upload ID is the closed
        non-staging rejection — neither value is ever rendered.
        """

        if staging_key not in self._staging:
            raise CleanupRejection(REJECTION_UNRECORDED_KEY)
        if not 1 <= len(provider_upload_id) <= _MAXIMUM_RECORDED_UPLOAD_ID_LENGTH:
            raise CleanupRejection(REJECTION_NONSTAGING_KEY)
        attached = self._staging[staging_key]
        if provider_upload_id not in attached:
            self._staging[staging_key] = (*attached, provider_upload_id)

    def recorded_staging_resources(self) -> tuple[CreatedStagingResourceRecord, ...]:
        """Every recorded staging resource in insertion (creation) order."""

        return tuple(
            CreatedStagingResourceRecord(staging_key=key, provider_upload_ids=ids)
            for key, ids in self._staging.items()
        )

    def staging_record_for(self, staging_key: str) -> CreatedStagingResourceRecord | None:
        """The recorded staging entry for ``staging_key``, or ``None``."""

        ids = self._staging.get(staging_key)
        if ids is None:
            return None
        return CreatedStagingResourceRecord(staging_key=staging_key, provider_upload_ids=ids)

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


def validate_staging_cleanup(
    manifest: LiveCleanupManifest,
    *,
    bucket_name: str,
    staging_keys: Sequence[str],
) -> tuple[str, ...]:
    """Validate requested staging cleanup against the exact-identity contract.

    The same fail-closed order as :func:`validate_cleanup_deletions`, for the
    staging side: a wrong bucket, any wildcard character, a value outside the
    closed staging grammar (which every canonical key shape fails) and an
    unrecorded staging key each raise :class:`CleanupRejection` before the
    calling pipeline has issued a single provider request. Returns the
    validated staging keys in request order.
    """

    if bucket_name != manifest.bucket_name:
        raise CleanupRejection(REJECTION_BUCKET_MISMATCH)
    validated: list[str] = []
    for staging_key in staging_keys:
        if any(character in staging_key for character in _WILDCARD_CHARACTERS):
            raise CleanupRejection(REJECTION_WILDCARD_KEY)
        if _STAGING_KEY_PATTERN.fullmatch(staging_key) is None:
            raise CleanupRejection(REJECTION_NONSTAGING_KEY)
        if manifest.staging_record_for(staging_key) is None:
            raise CleanupRejection(REJECTION_UNRECORDED_KEY)
        validated.append(staging_key)
    return tuple(validated)


async def run_exact_staging_cleanup(
    manifest: LiveCleanupManifest,
    *,
    bucket_name: str,
    resources: Sequence[CreatedStagingResourceRecord],
    abort_one: Callable[[str, str], Awaitable[None]],
    delete_one: Callable[[str], Awaitable[None]],
) -> tuple[str, ...]:
    """Validate first, then clean exactly the validated staging resources.

    ``abort_one`` and ``delete_one`` are the harness-local exact-identity
    provider calls; they are invoked only after :func:`validate_staging_cleanup`
    accepted every requested key, each upload ID of a validated key is aborted
    for exactly that key, and the staging object itself is removed for exactly
    that key. Both provider operations treat an already-absent resource as
    success (spec 6.4), so a replayed or inline-cleaned session stays a clean
    teardown. A rejection proves no provider request ran at all.
    """

    validated = validate_staging_cleanup(
        manifest, bucket_name=bucket_name, staging_keys=[r.staging_key for r in resources]
    )
    for staging_key in validated:
        record = manifest.staging_record_for(staging_key)
        assert record is not None, "validated staging keys always have a record"
        for provider_upload_id in record.provider_upload_ids:
            await abort_one(staging_key, provider_upload_id)
        await delete_one(staging_key)
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
