"""Fail-before-bind exclusion-policy signing settings and signer loading.

This module owns the policy-signing configuration fragment: the derived
``ed25519-sha256-…`` key ID announced by ``KNOWLEDGE_POLICY_SIGNING_KEY_ID``
and the exact PKCS#8 PEM file named by ``KNOWLEDGE_POLICY_SIGNING_KEY_FILE``
beneath the configured secret root. The file is read only through the shared
secret-file boundary, so the existing size, encoding, symlink/root and
permission checks apply unchanged; the material contract — an unencrypted
PKCS#8 PEM holding exactly one Ed25519 private key — is enforced here so
encrypted, malformed, multi-block and wrong-algorithm files fail as typed
configuration errors before any socket exists (spec 13.1).

Private key material never enters a settings value, a database row, a log line
or an error detail: the frozen settings snapshot carries only the key ID and
the resolved file path, and every material failure is a registered safe reason
token. The module also hosts the pure latest-keyset proof helpers the startup
hook composes: resolving the current key of one canonical keyset payload and
asserting the configured signer equals the current key of every initialized
workspace's latest keyset (spec 13.1/22).

It lives in the API composition-root package: it imports the shared core
error and runtime-configuration contracts plus the pinned Ed25519 adapter and
never imports FastAPI, Uvicorn or a database driver, so shell-only command
paths stay free of server machinery.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from api_runtime.exclusion_policy_crypto import Ed25519PolicySigner
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ConfigurationError,
    InternalApplicationError,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.signatures import is_wellformed_ed25519_key_id
from personal_os.runtime_configuration.environment_names import (
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
)
from personal_os.runtime_configuration.secret_files import read_secret_file

_ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"

#: Upper bound for one policy-signing PKCS#8 PEM file: far above the ~120-byte
#: encoding of one Ed25519 key, far below the generic secret ceiling.
POLICY_SIGNING_KEY_FILE_MAXIMUM_BYTES: Final[int] = 8192

#: Key file names are forward-slash-joined segments that each start with an
#: alphanumeric character, exactly like the authentication key file names, so
#: ``..`` segments, absolute paths and backslash escapes have no valid spelling
#: in this grammar.
_POLICY_KEY_FILE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_MAXIMUM_POLICY_KEY_FILE_NAME_LENGTH: Final[int] = 128

#: Closed map of policy-signing environment names to settings inputs. The key
#: set mirrors the core exclusion-policy fragment exactly, so the
#: repository-wide registry stays the single source of truth for the approved
#: names.
POLICY_SIGNING_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = {
    "KNOWLEDGE_ENVIRONMENT": "environment",
    "KNOWLEDGE_SECRET_ROOT": "secret_root",
    "KNOWLEDGE_POLICY_SIGNING_KEY_ID": "signing_key_id",
    "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "signing_key_file_name",
}

_PKCS8_PRIVATE_KEY_BEGIN: Final[str] = "-----BEGIN PRIVATE KEY-----"
_PKCS8_PRIVATE_KEY_END: Final[str] = "-----END PRIVATE KEY-----"


@dataclass(frozen=True, slots=True)
class ExclusionPolicySigningSettings:
    """Frozen snapshot of the policy-signing configuration fragment.

    ``signing_key_file`` is the exact file resolved beneath the configured
    secret root — never a caller-supplied absolute path — and
    ``signing_key_id`` must be the identifier derived from the public key of
    exactly that file. No key material travels with the snapshot.
    """

    signing_key_id: str
    signing_key_file: Path


def _configuration_invalid(field_names: tuple[str, ...]) -> ConfigurationError:
    """Build the fragment's typed invalid-configuration failure."""

    return ConfigurationError(
        ErrorCode.CONFIGURATION_INVALID,
        safe_details={
            "count": len(field_names),
            "field_names": tuple(SafeToken.parse(name) for name in field_names),
        },
    )


def _secret_invalid(reason: str) -> ConfigurationError:
    """Build the key-material failure carrying only a safe ``reason`` token."""

    return ConfigurationError(
        ErrorCode.CONFIGURATION_SECRET_INVALID,
        safe_details={"reason": SafeToken.parse(reason)},
    )


def validate_policy_signing_key_file_name(file_name: str) -> str:
    """Screen one policy-signing file name against the closed relative grammar."""

    if (
        len(file_name) > _MAXIMUM_POLICY_KEY_FILE_NAME_LENGTH
        or _POLICY_KEY_FILE_NAME_PATTERN.fullmatch(file_name) is None
    ):
        raise _configuration_invalid(("signing_key_file",))
    return file_name


def load_exclusion_policy_signing_settings(
    *,
    environ: Mapping[str, str] | None = None,
) -> ExclusionPolicySigningSettings:
    """Load the frozen signing snapshot from an environment mapping.

    ``environ`` defaults to ``os.environ`` read at call time. Any
    ``KNOWLEDGE_*`` key outside the repository-wide registry raises the typed
    unknown-key error without echoing the name or value; names owned by
    another fragment are ignored. The fragment requires the secret root and
    both signing names, the announced key ID must follow the derived
    ``ed25519-sha256-`` grammar, and the file name must be a relative name
    beneath the secret root. Grammar failures carry registered field names
    only; values never enter an error detail.
    """

    source = dict(os.environ) if environ is None else environ
    unknown_count = sum(
        1
        for key in source
        if key.startswith(_ENVIRONMENT_PREFIX) and key not in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    )
    if unknown_count:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_UNKNOWN_KEY,
            safe_details={"count": unknown_count},
        )
    missing = [
        environment_name
        for environment_name in (
            "KNOWLEDGE_SECRET_ROOT",
            "KNOWLEDGE_POLICY_SIGNING_KEY_ID",
            "KNOWLEDGE_POLICY_SIGNING_KEY_FILE",
        )
        if environment_name not in source
    ]
    if missing:
        raise _configuration_invalid(
            tuple(
                POLICY_SIGNING_ENVIRONMENT_FIELDS[environment_name] for environment_name in missing
            )
        )
    signing_key_id = source["KNOWLEDGE_POLICY_SIGNING_KEY_ID"]
    if not is_wellformed_ed25519_key_id(signing_key_id):
        raise _configuration_invalid(("signing_key_id",))
    file_name = validate_policy_signing_key_file_name(source["KNOWLEDGE_POLICY_SIGNING_KEY_FILE"])
    secret_root = Path(source["KNOWLEDGE_SECRET_ROOT"])
    if not secret_root.is_absolute():
        raise _configuration_invalid(("secret_root",))
    return ExclusionPolicySigningSettings(
        signing_key_id=signing_key_id,
        signing_key_file=secret_root / file_name,
    )


def parse_policy_signing_pem(value: str) -> Ed25519PrivateKey:
    """Parse exactly one unencrypted PKCS#8 Ed25519 private-key PEM value.

    The value must contain exactly one PEM block and that block must be the
    unencrypted PKCS#8 ``PRIVATE KEY`` form holding one Ed25519 key. Encrypted
    containers, concatenated blocks, foreign PEM kinds, wrong algorithms and
    malformed text fail as ``configuration_secret_invalid`` with a closed
    reason token; the offending material never enters the error.
    """

    stripped = value.strip()
    if (
        stripped.count("-----BEGIN ") != 1
        or stripped.count("-----END ") != 1
        or not stripped.startswith(_PKCS8_PRIVATE_KEY_BEGIN)
        or not stripped.endswith(_PKCS8_PRIVATE_KEY_END)
    ):
        if "-----BEGIN ENCRYPTED PRIVATE KEY-----" in stripped:
            raise _secret_invalid("encrypted")
        if stripped.count("-----BEGIN ") > 1:
            raise _secret_invalid("multiple_blocks")
        raise _secret_invalid("malformed")
    try:
        private_key = serialization.load_pem_private_key(stripped.encode("ascii"), password=None)
    except UnicodeEncodeError as cause:
        raise _secret_invalid("malformed") from cause
    except TypeError as cause:
        raise _secret_invalid("encrypted") from cause
    except ValueError as cause:
        raise _secret_invalid("malformed") from cause
    except UnsupportedAlgorithm as cause:  # pragma: no cover - pinned library
        raise _secret_invalid("unsupported_algorithm") from cause
    if not isinstance(private_key, Ed25519PrivateKey):
        raise _secret_invalid("wrong_algorithm")
    return private_key


def load_exclusion_policy_signer(
    settings: ExclusionPolicySigningSettings,
    *,
    secret_root: Path,
) -> Ed25519PolicySigner:
    """Load the signer from the configured exact file through the boundary.

    The file is read with the shared secret-file checks (bounded size, strict
    resolved root, regular file, safe permissions), parsed as exactly one
    unencrypted PKCS#8 Ed25519 PEM, and its derived key ID must equal the
    announced :attr:`ExclusionPolicySigningSettings.signing_key_id` — the
    private/public mismatch refusal of spec 13.1. The returned signer is the
    pinned Ed25519 adapter; key bytes never leave it.
    """

    secret = read_secret_file(
        settings.signing_key_file,
        secret_root,
        maximum_size_bytes=POLICY_SIGNING_KEY_FILE_MAXIMUM_BYTES,
    )
    private_key = parse_policy_signing_pem(secret.get_secret_value())
    signer = Ed25519PolicySigner(private_key)
    if signer.key_id != settings.signing_key_id:
        raise _secret_invalid("key_id_mismatch")
    return signer


def resolve_current_key_id_from_keyset_payload(payload_bytes: bytes) -> str | None:
    """Resolve the current key ID declared by one canonical keyset payload.

    The payload bytes come from the append-only database keyset rows, so a
    shape outside the closed contract is corruption and fails closed as the
    safe ``internal_error``. A staged-only keyset — legal at the payload layer
    — resolves to ``None``; the caller decides what that means.
    """

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        keys = payload["keys"]
        current_ids = [str(key["key_id"]) for key in keys if key["state"] == "current"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
    if len(current_ids) > 1:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from None
    return current_ids[0] if current_ids else None


def assert_signer_is_current_in_latest_keysets(
    latest_keyset_payloads: Sequence[bytes],
    *,
    signing_key_id: str,
) -> None:
    """Prove the configured signer is the current key of every latest keyset.

    Spec 13.1 requires the derived key ID of the configured private key to
    equal the current key in the latest canonical keyset. A database with no
    keyset at all fails as the typed policy not-initialized error; a signer
    that is not the current key — unknown, staged or retired — fails as the
    typed configuration error naming only the registered field. Both refusals
    happen before the API binds its socket.
    """

    if not latest_keyset_payloads:
        raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
    for payload_bytes in latest_keyset_payloads:
        if resolve_current_key_id_from_keyset_payload(payload_bytes) != signing_key_id:
            raise _configuration_invalid(("signing_key_id",))
