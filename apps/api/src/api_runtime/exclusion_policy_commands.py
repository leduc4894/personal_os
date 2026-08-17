"""Offline ``policy-key`` lifecycle commands for exclusion-policy signing keys.

This module implements the four internal subcommands declared by
:mod:`api_runtime.command` — ``policy-key initialize``, ``stage``,
``activate`` and ``retire`` (spec 13.2/13.3). It is imported only inside the
``policy-key`` handler, so every shell-only invocation stays free of
database and crypto imports.

The keyset chain semantics are the staged rotation of spec 13.3: revision 1
is self-signed with the initialized key as the one current key; staging
appends one revision declaring the old current plus the new staged public
keys, signed by the old current key and cross-signed as proof-of-possession
by the new key; activation appends a later cross-signed revision making the
staged key current while the old key stays trusted (staged, the operating
overlap) and requires both signatures; retirement appends one final revision
moving the overlap key to retired, signed by the current key alone. Every
append writes the immutable keyset envelope, the public signing-key rows it
introduces and its lifecycle audit row inside exactly one ``READ COMMITTED``
transaction with the shared policy retry policy and the exact-replay
acknowledgement — a replayed CLI invocation re-derives the already committed
transition from the key file plus the latest keyset and acknowledges it
without appending rows, and no signing-key row is ever mutated.

The private-key boundary is closed: key files live only as exact files
beneath the configured secret root (creation writes one newly created file
with restrictive permissions through ``O_CREAT | O_EXCL`` and never
overwrites existing bytes; an existing file is imported after the same PEM
contract the startup path enforces), private material never enters
PostgreSQL, settings values, logs or audit rows, and stdout carries only the
closed status line — action, public key ID, keyset revision and the replay
flag — never key bytes or signatures. Exit codes follow the process-shell
conventions: ``0`` on success, ``2`` for operator-input validation, ``78``
for typed rejections and ``70`` for unexpected internal failures.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from argparse import Namespace
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from api_runtime.exclusion_policy_crypto import Ed25519PolicySigner
from api_runtime.exclusion_policy_settings import (
    POLICY_SIGNING_KEY_FILE_MAXIMUM_BYTES,
    load_exclusion_policy_signing_settings,
    parse_policy_signing_pem,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ApplicationError,
    ConfigurationError,
    InternalApplicationError,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.ports import (
    PersistedPolicyKeyset,
    PolicyKeysetEnvelope,
    PolicyKeysetRecord,
    PolicyKeysetSignatureRecord,
    PolicySigningKeyRecord,
)
from personal_os.exclusion_policy.signatures import (
    KEYSET_SIGNING_DOMAIN,
    PolicyKeysetKey,
    PolicyKeysetState,
    PolicySigner,
    build_keyset_payload,
    build_signed_message,
    compute_payload_sha256_hex,
    decode_base64url_without_padding,
    derive_ed25519_key_id,
    is_wellformed_ed25519_key_id,
)
from personal_os.runtime_configuration.secret_files import read_secret_file
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.policy_drafts import PolicyDatabaseRetryPolicy
from postgresql_source_store.policy_keysets import (
    PostgresqlPolicyKeysetStore,
    build_keyset_signature_values,
    build_keyset_values,
    build_signing_key_values,
    classify_keyset_replay,
)
from postgresql_source_store.tables import (
    audit_events,
    policy_keyset_signatures,
    policy_keysets,
    policy_signing_keys,
)

#: Exit codes mirroring the shared shell and protected-command conventions.
_EXIT_SUCCESS: Final[int] = 0
_EXIT_INPUT_INVALID: Final[int] = 2
_EXIT_APPLICATION_REJECTED: Final[int] = 78
_EXIT_INTERNAL: Final[int] = 70

#: Audit-row literals for the in-transaction key-lifecycle audit (spec 21).
KEY_INITIALIZED_AUDIT_ACTION: Final[str] = "exclusion_policy.key_initialized"
KEY_STAGED_AUDIT_ACTION: Final[str] = "exclusion_policy.key_staged"
KEY_ACTIVATED_AUDIT_ACTION: Final[str] = "exclusion_policy.key_activated"
KEY_RETIRED_AUDIT_ACTION: Final[str] = "exclusion_policy.key_retired"
POLICY_KEYSET_AUDIT_TARGET_KIND: Final[str] = "policy_keyset"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"

#: Closed status verbs of the result line; they print with the key ID, the
#: keyset revision and the replay flag only.
ACTION_INITIALIZED: Final[str] = "initialized"
ACTION_STAGED: Final[str] = "staged"
ACTION_ACTIVATED: Final[str] = "activated"
ACTION_RETIRED: Final[str] = "retired"

#: POSIX owner-only permissions for a newly created private-key file; platforms
#: without POSIX permission bits keep their native defaults.
_CREATED_KEY_FILE_MODE: Final[int] = 0o600

_KEY_FILE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_MAXIMUM_KEY_FILE_NAME_LENGTH: Final[int] = 128

#: One row of a signing-key read: a SQLAlchemy row mapping from the adapter's
#: ``.mappings()`` results or an equivalent mapping in tests.
type _MappedRow = RowMapping | Mapping[str, Any]


class PolicyKeyCommandInputError(Exception):
    """Closed operator-input rejection carrying one fixed safe reason.

    The rejected value never travels with the error, so ``str`` and ``repr``
    can only ever expose the fixed reason text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _rejection(reason: str) -> ExclusionPolicyError:
    """Build the typed lifecycle rejection with one closed reason token."""

    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
        safe_details={"reason": SafeToken.parse(reason)},
    )


# --- operator-input validation -------------------------------------------------


def validate_policy_key_workspace_id(text: str) -> UUID:
    """Screen one workspace identity against the canonical UUID grammar."""

    try:
        workspace_id = UUID(text)
    except ValueError:
        raise PolicyKeyCommandInputError("workspace id must be a canonical UUID") from None
    if workspace_id == UUID(int=0):
        raise PolicyKeyCommandInputError("workspace id must not be nil")
    return workspace_id


def validate_policy_key_file_name(file_name: str) -> str:
    """Screen one key file name against the closed relative grammar."""

    if (
        len(file_name) > _MAXIMUM_KEY_FILE_NAME_LENGTH
        or _KEY_FILE_NAME_PATTERN.fullmatch(file_name) is None
    ):
        raise PolicyKeyCommandInputError(
            "key file name must be a relative name beneath the secret root"
        )
    return file_name


def validate_policy_signing_key_id_text(text: str) -> str:
    """Screen one key ID against the derived Ed25519 key-ID grammar."""

    if not is_wellformed_ed25519_key_id(text):
        raise PolicyKeyCommandInputError("key id must follow the derived Ed25519 grammar")
    return text


# --- the loaded signing key and the closed result rendering ----------------------


@dataclass(frozen=True, slots=True)
class LoadedPolicySigningKey:
    """One signer together with the raw public bytes its key ID derives from.

    The payload builders need the raw public key beside the signer, and the
    pinned adapter exposes only the derived identifier, so this record binds
    both once at the file boundary. It satisfies the domain
    :class:`~personal_os.exclusion_policy.signatures.PolicySigner` port
    structurally through its ``key_id``/``sign`` members.
    """

    signer: Ed25519PolicySigner
    public_key_bytes: bytes

    @property
    def key_id(self) -> str:
        return self.signer.key_id

    def sign(self, message: bytes) -> bytes:
        return self.signer.sign(message)


@dataclass(frozen=True, slots=True)
class PolicyKeyLifecycleOutcome:
    """The closed lifecycle result: action, public identity, revision, replay."""

    action: str
    key_id: str
    keyset_revision: int
    is_replay: bool


def render_policy_key_outcome(outcome: PolicyKeyLifecycleOutcome) -> str:
    """Render the status line: IDs and status only, never key bytes."""

    return (
        f"{outcome.action}=true key_id={outcome.key_id}"
        f" keyset_revision={outcome.keyset_revision} replayed={str(outcome.is_replay).lower()}"
    )


# --- the keyset payload view -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyKeysetKeyView:
    """One trust-anchor entry of a parsed canonical keyset payload."""

    key_id: str
    public_key: bytes
    state: PolicyKeysetState


@dataclass(frozen=True, slots=True)
class PolicyKeysetView:
    """The immutable view of one persisted canonical keyset payload."""

    keyset_revision: int
    keys: tuple[PolicyKeysetKeyView, ...]

    def current_key(self) -> PolicyKeysetKeyView | None:
        """The one current key, or ``None`` for a staged-only keyset."""

        for key in self.keys:
            if key.state is PolicyKeysetState.CURRENT:
                return key
        return None

    def staged_keys(self) -> tuple[PolicyKeysetKeyView, ...]:
        return tuple(key for key in self.keys if key.state is PolicyKeysetState.STAGED)

    def state_of(self, key_id: str) -> PolicyKeysetState | None:
        for key in self.keys:
            if key.key_id == key_id:
                return key.state
        return None


def parse_policy_keyset_payload(payload_bytes: bytes) -> PolicyKeysetView:
    """Parse one canonical keyset payload into the closed lifecycle view.

    The bytes come from the append-only database rows, so a shape outside the
    closed contract is corruption and fails closed as the safe
    ``internal_error``; no payload value is echoed.
    """

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        revision = int(payload["keyset_revision"])
        raw_keys = payload["keys"]
        keys = tuple(
            PolicyKeysetKeyView(
                key_id=str(raw_key["key_id"]),
                public_key=decode_base64url_without_padding(str(raw_key["public_key"])),
                state=PolicyKeysetState(str(raw_key["state"])),
            )
            for raw_key in raw_keys
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        ExclusionPolicyError,
    ) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
    return PolicyKeysetView(keyset_revision=revision, keys=keys)


# --- the pure transition planners --------------------------------------------------


def plan_initialized_keys(key_id: str, public_key: bytes) -> tuple[PolicyKeysetKey, ...]:
    """Revision 1 carries exactly one current key (spec 13.3)."""

    return (PolicyKeysetKey(key_id=key_id, public_key=public_key, state=PolicyKeysetState.CURRENT),)


def plan_staged_keys(
    previous: PolicyKeysetView, key_id: str, public_key: bytes
) -> tuple[PolicyKeysetKey, ...]:
    """Staging keeps every previous state and adds one staged key.

    The old current key stays current; the new key enters as staged. A key
    that is already known — current, staged or retired — can never be staged
    twice, and at most one staged key exists at a time, so the operator must
    activate or retire the previous staged key first.
    """

    if previous.state_of(key_id) is not None:
        raise _rejection("key_already_known")
    if previous.staged_keys():
        raise _rejection("staged_key_exists")
    return (
        *(
            PolicyKeysetKey(key_id=key.key_id, public_key=key.public_key, state=key.state)
            for key in previous.keys
        ),
        PolicyKeysetKey(key_id=key_id, public_key=public_key, state=PolicyKeysetState.STAGED),
    )


def plan_activated_keys(
    previous: PolicyKeysetView, staged_key_id: str
) -> tuple[PolicyKeysetKey, ...]:
    """Activation promotes one staged key; the old current key joins the overlap.

    The newly current key is the activated staged key; the previously current
    key drops to staged — the non-retired operating overlap of spec 13.3 that
    a later retirement revision removes.
    """

    if previous.state_of(staged_key_id) is not PolicyKeysetState.STAGED:
        raise _rejection("key_not_staged")
    keys: list[PolicyKeysetKey] = []
    for key in previous.keys:
        if key.state is PolicyKeysetState.CURRENT:
            keys.append(
                PolicyKeysetKey(
                    key_id=key.key_id,
                    public_key=key.public_key,
                    state=PolicyKeysetState.STAGED,
                )
            )
        elif key.key_id == staged_key_id:
            keys.append(
                PolicyKeysetKey(
                    key_id=key.key_id,
                    public_key=key.public_key,
                    state=PolicyKeysetState.CURRENT,
                )
            )
        else:
            keys.append(
                PolicyKeysetKey(key_id=key.key_id, public_key=key.public_key, state=key.state)
            )
    return tuple(keys)


def plan_retired_keys(
    previous: PolicyKeysetView, retiring_key_id: str
) -> tuple[PolicyKeysetKey, ...]:
    """Retirement moves one non-current trusted key to retired.

    The current key — including the case where it is the only trusted key —
    can never be retired directly; rotation must stage and activate a
    replacement first. Unknown and already-retired keys are refused; the
    replay acknowledgement of an earlier retirement belongs to the caller.
    """

    state = previous.state_of(retiring_key_id)
    if state is None:
        raise _rejection("key_unknown")
    if state is PolicyKeysetState.CURRENT:
        raise _rejection("cannot_retire_current_key")
    if state is PolicyKeysetState.RETIRED:
        raise _rejection("key_already_retired")
    return tuple(
        PolicyKeysetKey(
            key_id=key.key_id,
            public_key=key.public_key,
            state=PolicyKeysetState.RETIRED if key.key_id == retiring_key_id else key.state,
        )
        for key in previous.keys
    )


# --- the cross-signed envelope construction ------------------------------------------


def build_lifecycle_keyset_envelope(
    *,
    workspace_id: UUID,
    keyset_revision: int,
    keys: Sequence[PolicyKeysetKey],
    row_ids_by_key_id: Mapping[str, UUID],
    signers: Sequence[PolicySigner],
    created_at: datetime,
    required_signing_key_ids: Sequence[str] = (),
) -> PolicyKeysetEnvelope:
    """Build one immutable keyset envelope with its cross-signatures.

    The canonical payload bytes come from the domain builder, every signature
    covers the domain-separated message, and each declared signing key is
    bound to one database row identity — an existing row for already-known
    keys, one freshly allocated row for keys this revision introduces. When
    ``required_signing_key_ids`` names the cross-signature set (the staged
    and activation revisions require the old-current plus the new key), a
    missing signer refuses the build before any byte is persisted.
    """

    signer_ids = {signer.key_id for signer in signers}
    for required_key_id in required_signing_key_ids:
        if required_key_id not in signer_ids:
            raise _rejection("cross_signature_missing")

    payload_bytes = build_keyset_payload(
        workspace_id=workspace_id,
        keyset_revision=keyset_revision,
        parent_keyset_revision=None if keyset_revision == 1 else keyset_revision - 1,
        created_at=created_at,
        keys=tuple(keys),
    )
    message = build_signed_message(KEYSET_SIGNING_DOMAIN, payload_bytes)

    resolved_row_ids: dict[str, UUID] = {}
    for key in keys:
        resolved_row_ids[key.key_id] = row_ids_by_key_id.get(key.key_id, uuid7())
    signing_key_records = tuple(
        PolicySigningKeyRecord(
            signing_key_id=resolved_row_ids[key.key_id], public_key_bytes=key.public_key
        )
        for key in keys
    )
    signature_records = []
    for signer in signers:
        row_id = resolved_row_ids.get(signer.key_id)
        if row_id is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from None
        signature_records.append(
            PolicyKeysetSignatureRecord(signing_key_id=row_id, signature_bytes=signer.sign(message))
        )
    return PolicyKeysetEnvelope(
        policy_keyset_id=uuid7(),
        workspace_id=workspace_id,
        keyset_revision=keyset_revision,
        parent_keyset_revision=None if keyset_revision == 1 else keyset_revision - 1,
        canonical_payload_bytes=payload_bytes,
        keys=signing_key_records,
        signatures=tuple(signature_records),
    )


def build_key_lifecycle_audit_values(
    *,
    workspace_id: UUID,
    policy_keyset_id: UUID,
    action: str,
    payload_sha256: str,
    occurred_at: datetime,
    context: DiagnosticContext,
) -> dict[str, Any]:
    """Build one key-lifecycle audit row's insert values.

    The row carries identifiers, the closed action/target/result literals and
    the canonical payload digest only: key bytes, signatures, file names and
    paths never enter the audit table (spec 21).
    """

    return {
        "audit_event_id": uuid7(),
        "workspace_id": workspace_id,
        "actor_kind": "system",
        "actor_id": None,
        "actor_reference": None,
        "action": action,
        "target_kind": POLICY_KEYSET_AUDIT_TARGET_KIND,
        "target_id": policy_keyset_id,
        "request_id": context.request_id,
        "client_request_id": context.client_request_id,
        "trace_id": context.trace.trace_id.value,
        "result": AUDIT_RESULT_SUCCEEDED,
        "reason_code": None,
        "safe_diff_hash": payload_sha256,
        "occurred_at": occurred_at,
    }


# --- the one-transaction append with audit -------------------------------------------


def _signing_key_insert_statement(values: Mapping[str, Any]) -> postgresql.dml.Insert:
    """Build the insert-once public signing-key statement keyed by identity."""

    statement = postgresql.insert(policy_signing_keys).values(**values)
    return statement.on_conflict_do_nothing(index_elements=[policy_signing_keys.c.signing_key_id])


async def _ensure_signing_key_row(
    connection: AsyncConnection,
    key: PolicySigningKeyRecord,
    workspace_id: UUID,
    introduced_keyset_revision: int,
    occurred_at: datetime,
) -> None:
    """Append one public signing-key row, verifying an existing identity.

    Mirrors the Task 4 keyset store's append-only guard: ``ON CONFLICT DO
    NOTHING`` keeps the first row immutable and the follow-up lookup proves
    the existing identity belongs to the same workspace with the same public
    bytes. The lifecycle owns this statement because its transaction also
    carries the audit row, which the store port does not accept.
    """

    await connection.execute(
        _signing_key_insert_statement(
            build_signing_key_values(
                key,
                workspace_id=workspace_id,
                introduced_keyset_revision=introduced_keyset_revision,
                occurred_at=occurred_at,
            )
        )
    )
    result = await connection.execute(
        sa.select(
            policy_signing_keys.c.workspace_id,
            policy_signing_keys.c.public_key_bytes,
        ).where(policy_signing_keys.c.signing_key_id == key.signing_key_id)
    )
    row = result.mappings().first()
    if (
        row is None
        or row["workspace_id"] != workspace_id
        or row["public_key_bytes"] != key.public_key_bytes
    ):
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)


async def _select_locked_keyset_revision(
    connection: AsyncConnection, workspace_id: UUID, keyset_revision: int
) -> _MappedRow | None:
    result = await connection.execute(
        sa.select(
            policy_keysets.c.policy_keyset_id,
            policy_keysets.c.payload_sha256,
            policy_keysets.c.created_at,
        )
        .where(
            policy_keysets.c.workspace_id == workspace_id,
            policy_keysets.c.keyset_revision == keyset_revision,
        )
        .with_for_update()
    )
    return result.mappings().first()


async def _select_now(connection: AsyncConnection) -> datetime:
    """Read the transaction-stable timestamp shared by every written row."""

    result = await connection.execute(sa.text("SELECT now()"))
    occurred_at = result.scalar_one()
    if not isinstance(occurred_at, datetime):  # pragma: no cover - driver contract
        raise TypeError("SELECT now() did not return a datetime")
    return occurred_at


async def append_keyset_revision_with_audit(
    engine: AsyncEngine,
    envelope: PolicyKeysetEnvelope,
    *,
    audit_action: str,
    context: DiagnosticContext,
) -> PersistedPolicyKeyset:
    """Append one keyset revision and its audit row in exactly one transaction.

    The append reuses the shared policy retry policy with an evidence-based
    recovery lookup for the uncertain-commit case: the envelope is built once
    per invocation, so a proven recovery is the exact replay acknowledgement
    and a proven absence retries the same immutable identity. An existing
    revision under a different identity or payload hash is integrity
    corruption and fails closed without mutating history.
    """

    retry = PolicyDatabaseRetryPolicy()
    return await retry.run(
        lambda _attempt: _append_keyset_revision_once(
            engine, envelope, audit_action=audit_action, context=context
        ),
        recover=lambda: _recover_appended_keyset_revision(engine, envelope),
    )


async def _append_keyset_revision_once(
    engine: AsyncEngine,
    envelope: PolicyKeysetEnvelope,
    *,
    audit_action: str,
    context: DiagnosticContext,
) -> PersistedPolicyKeyset:
    payload_sha256 = compute_payload_sha256_hex(envelope.canonical_payload_bytes)
    async with (
        engine.connect() as connection,
        connection.begin(),
    ):
        await apply_transaction_bounds(connection)
        existing = await _select_locked_keyset_revision(
            connection, envelope.workspace_id, envelope.keyset_revision
        )
        if existing is not None:
            if not classify_keyset_replay(existing, envelope.policy_keyset_id, payload_sha256):
                raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
            return PersistedPolicyKeyset(
                policy_keyset_id=envelope.policy_keyset_id,
                workspace_id=envelope.workspace_id,
                keyset_revision=envelope.keyset_revision,
                payload_sha256=payload_sha256,
                created_at=existing["created_at"],
                is_replay=True,
            )
        occurred_at = await _select_now(connection)
        for key in envelope.keys:
            await _ensure_signing_key_row(
                connection,
                key,
                envelope.workspace_id,
                envelope.keyset_revision,
                occurred_at,
            )
        await connection.execute(
            sa.insert(policy_keysets).values(
                **build_keyset_values(envelope, occurred_at=occurred_at)
            )
        )
        await connection.execute(
            sa.insert(policy_keyset_signatures).values(
                [
                    build_keyset_signature_values(envelope.policy_keyset_id, signature)
                    for signature in envelope.signatures
                ]
            )
        )
        await connection.execute(
            sa.insert(audit_events).values(
                **build_key_lifecycle_audit_values(
                    workspace_id=envelope.workspace_id,
                    policy_keyset_id=envelope.policy_keyset_id,
                    action=audit_action,
                    payload_sha256=payload_sha256,
                    occurred_at=occurred_at,
                    context=context,
                )
            )
        )
    return PersistedPolicyKeyset(
        policy_keyset_id=envelope.policy_keyset_id,
        workspace_id=envelope.workspace_id,
        keyset_revision=envelope.keyset_revision,
        payload_sha256=payload_sha256,
        created_at=occurred_at,
        is_replay=False,
    )


async def _recover_appended_keyset_revision(
    engine: AsyncEngine, envelope: PolicyKeysetEnvelope
) -> PersistedPolicyKeyset | None:
    """Prove or disprove that an uncertain keyset append landed."""

    payload_sha256 = compute_payload_sha256_hex(envelope.canonical_payload_bytes)
    async with (
        engine.connect() as connection,
        connection.begin(),
    ):
        await apply_transaction_bounds(connection)
        result = await connection.execute(
            sa.select(
                policy_keysets.c.policy_keyset_id,
                policy_keysets.c.payload_sha256,
                policy_keysets.c.created_at,
            ).where(
                policy_keysets.c.workspace_id == envelope.workspace_id,
                policy_keysets.c.keyset_revision == envelope.keyset_revision,
            )
        )
        row = result.mappings().first()
    if row is None or not classify_keyset_replay(row, envelope.policy_keyset_id, payload_sha256):
        return None
    return PersistedPolicyKeyset(
        policy_keyset_id=envelope.policy_keyset_id,
        workspace_id=envelope.workspace_id,
        keyset_revision=envelope.keyset_revision,
        payload_sha256=payload_sha256,
        created_at=row["created_at"],
        is_replay=True,
    )


# --- the private-key file boundary -----------------------------------------------------


def _resolve_key_file(secret_root: Path, file_name: str) -> tuple[Path, Path]:
    """Resolve one validated key file and its resolved root as exact paths."""

    validate_policy_key_file_name(file_name)
    if not secret_root.is_absolute():
        raise PolicyKeyCommandInputError("secret root must be absolute")
    resolved_root = secret_root.resolve(strict=True)
    candidate = resolved_root / file_name
    if not candidate.is_relative_to(resolved_root):
        raise PolicyKeyCommandInputError(
            "key file name must be a relative name beneath the secret root"
        )
    return candidate, resolved_root


def _loaded_signing_key_from_secret(secret: SecretStr) -> LoadedPolicySigningKey:
    """Build one loaded signing key from a boundary-validated secret value."""

    private_key = parse_policy_signing_pem(secret.get_secret_value())
    return LoadedPolicySigningKey(
        signer=Ed25519PolicySigner(private_key),
        public_key_bytes=private_key.public_key().public_bytes_raw(),
    )


def load_existing_policy_signing_key(secret_root: Path, file_name: str) -> LoadedPolicySigningKey:
    """Load one already present signing key through the secret-file boundary."""

    candidate, resolved_root = _resolve_key_file(secret_root, file_name)
    secret = read_secret_file(
        candidate, resolved_root, maximum_size_bytes=POLICY_SIGNING_KEY_FILE_MAXIMUM_BYTES
    )
    return _loaded_signing_key_from_secret(secret)


def create_or_load_policy_signing_key(secret_root: Path, file_name: str) -> LoadedPolicySigningKey:
    """Generate one signing key into a newly created file, or import existing.

    Generation writes exactly one newly created file with owner-only
    permissions through ``O_CREAT | O_EXCL`` — an existing path is never
    truncated, and a dangling symlink or reparse point fails the exclusive
    create instead of being followed. When the exact file already exists, its
    material is imported through the same unencrypted-PKCS#8-Ed25519 contract
    the startup path enforces; either way the signer derives from material
    that passed the secret-file boundary.
    """

    candidate, _resolved_root = _resolve_key_file(secret_root, file_name)
    pem_bytes = Ed25519PrivateKey.generate().private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(candidate, open_flags, _CREATED_KEY_FILE_MODE)
    except FileExistsError:
        return load_existing_policy_signing_key(secret_root, file_name)
    except OSError as cause:
        raise PolicyKeyCommandInputError("key file could not be created") from cause
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem_bytes)
    return load_existing_policy_signing_key(secret_root, file_name)


# --- the durable lifecycle operations ---------------------------------------------------


async def load_policy_keyset_state(
    engine: AsyncEngine, workspace_id: UUID, context: DiagnosticContext
) -> tuple[PolicyKeysetRecord | None, Mapping[str, UUID]]:
    """Load the latest keyset record and the workspace's row-identity map.

    The map binds every public key derived identifier of the workspace to its
    immutable ``policy_signing_keys`` row identity, so a new revision can
    reference existing rows and allocate fresh identities only for keys it
    introduces. A payload key without a row is corruption and fails closed.
    """

    record = await PostgresqlPolicyKeysetStore(engine).load_latest_keyset(workspace_id, context)
    row_ids: dict[str, UUID] = {}
    async with (
        engine.connect() as connection,
        connection.begin(),
    ):
        await apply_transaction_bounds(connection)
        result = await connection.execute(
            sa.select(
                policy_signing_keys.c.signing_key_id,
                policy_signing_keys.c.public_key_bytes,
            ).where(policy_signing_keys.c.workspace_id == workspace_id)
        )
        for row in result.mappings():
            row_ids[derive_ed25519_key_id(row["public_key_bytes"])] = row["signing_key_id"]
    if record is not None:
        view = parse_policy_keyset_payload(record.canonical_payload_bytes)
        if any(key.key_id not in row_ids for key in view.keys):
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from None
    return record, MappingProxyType(row_ids)


def _require_signer_is_current(view: PolicyKeysetView, signer_key_id: str) -> None:
    """Refuse any transition whose signer is not the latest current key."""

    current = view.current_key()
    if current is None or current.key_id != signer_key_id:
        raise _rejection("signer_not_current")


def _now_utc() -> datetime:
    return datetime.now(UTC)


async def execute_policy_key_initialize(
    *,
    engine: AsyncEngine,
    workspace_id: UUID,
    key_file_name: str,
    secret_root: Path,
    context: DiagnosticContext,
) -> PolicyKeyLifecycleOutcome:
    """Initialize keyset revision 1 with one self-signed current key (spec 13.2).

    A workspace that already carries a keyset is refused — rotation must
    stage and activate instead — except the exact replay: revision 1 whose
    current key equals the key derived from the named file acknowledges the
    already committed initialization without appending rows.
    """

    record, _row_ids = await load_policy_keyset_state(engine, workspace_id, context)
    if record is not None:
        signer = load_existing_policy_signing_key(secret_root, key_file_name)
        view = parse_policy_keyset_payload(record.canonical_payload_bytes)
        current = view.current_key()
        if record.keyset_revision == 1 and current is not None and current.key_id == signer.key_id:
            return PolicyKeyLifecycleOutcome(
                action=ACTION_INITIALIZED,
                key_id=signer.key_id,
                keyset_revision=1,
                is_replay=True,
            )
        raise _rejection("already_initialized")
    signer = create_or_load_policy_signing_key(secret_root, key_file_name)
    envelope = build_lifecycle_keyset_envelope(
        workspace_id=workspace_id,
        keyset_revision=1,
        keys=plan_initialized_keys(signer.key_id, signer.public_key_bytes),
        row_ids_by_key_id={},
        signers=(signer,),
        created_at=_now_utc(),
    )
    persisted = await append_keyset_revision_with_audit(
        engine, envelope, audit_action=KEY_INITIALIZED_AUDIT_ACTION, context=context
    )
    return PolicyKeyLifecycleOutcome(
        action=ACTION_INITIALIZED,
        key_id=signer.key_id,
        keyset_revision=persisted.keyset_revision,
        is_replay=persisted.is_replay,
    )


async def execute_policy_key_stage(
    *,
    engine: AsyncEngine,
    workspace_id: UUID,
    key_file_name: str,
    secret_root: Path,
    signer: LoadedPolicySigningKey,
    context: DiagnosticContext,
) -> PolicyKeyLifecycleOutcome:
    """Stage one new key: cross-signed revision declaring staged trust (13.3).

    The configured signer must be the latest current key and its private key
    signs the revision beside the new key's proof-of-possession signature. A
    key already staged from the named file acknowledges the earlier staging
    without appending rows.
    """

    record, row_ids = await load_policy_keyset_state(engine, workspace_id, context)
    if record is None:
        raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
    view = parse_policy_keyset_payload(record.canonical_payload_bytes)
    new_signer = create_or_load_policy_signing_key(secret_root, key_file_name)
    new_state = view.state_of(new_signer.key_id)
    if new_state is PolicyKeysetState.STAGED:
        return PolicyKeyLifecycleOutcome(
            action=ACTION_STAGED,
            key_id=new_signer.key_id,
            keyset_revision=record.keyset_revision,
            is_replay=True,
        )
    if new_state is not None:
        raise _rejection("key_already_known")
    _require_signer_is_current(view, signer.key_id)
    keys = plan_staged_keys(view, new_signer.key_id, new_signer.public_key_bytes)
    envelope = build_lifecycle_keyset_envelope(
        workspace_id=workspace_id,
        keyset_revision=record.keyset_revision + 1,
        keys=keys,
        row_ids_by_key_id=row_ids,
        signers=(signer, new_signer),
        required_signing_key_ids=(signer.key_id, new_signer.key_id),
        created_at=_now_utc(),
    )
    persisted = await append_keyset_revision_with_audit(
        engine, envelope, audit_action=KEY_STAGED_AUDIT_ACTION, context=context
    )
    return PolicyKeyLifecycleOutcome(
        action=ACTION_STAGED,
        key_id=new_signer.key_id,
        keyset_revision=persisted.keyset_revision,
        is_replay=persisted.is_replay,
    )


async def execute_policy_key_activate(
    *,
    engine: AsyncEngine,
    workspace_id: UUID,
    staged_key_file_name: str,
    secret_root: Path,
    signer: LoadedPolicySigningKey,
    context: DiagnosticContext,
) -> PolicyKeyLifecycleOutcome:
    """Activate the staged key: cross-signed revision making it current (13.3).

    The staged key's private key must still be available at the named exact
    file and the configured signer must still be the old current key — the
    API signer configuration switches only after this revision commits. A
    staged key that is already the current key acknowledges the earlier
    activation without appending rows.
    """

    record, row_ids = await load_policy_keyset_state(engine, workspace_id, context)
    if record is None:
        raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
    view = parse_policy_keyset_payload(record.canonical_payload_bytes)
    staged_signer = load_existing_policy_signing_key(secret_root, staged_key_file_name)
    current = view.current_key()
    if current is not None and current.key_id == staged_signer.key_id:
        return PolicyKeyLifecycleOutcome(
            action=ACTION_ACTIVATED,
            key_id=staged_signer.key_id,
            keyset_revision=record.keyset_revision,
            is_replay=True,
        )
    if view.state_of(staged_signer.key_id) is not PolicyKeysetState.STAGED:
        raise _rejection("key_not_staged")
    _require_signer_is_current(view, signer.key_id)
    keys = plan_activated_keys(view, staged_signer.key_id)
    envelope = build_lifecycle_keyset_envelope(
        workspace_id=workspace_id,
        keyset_revision=record.keyset_revision + 1,
        keys=keys,
        row_ids_by_key_id=row_ids,
        signers=(signer, staged_signer),
        required_signing_key_ids=(signer.key_id, staged_signer.key_id),
        created_at=_now_utc(),
    )
    persisted = await append_keyset_revision_with_audit(
        engine, envelope, audit_action=KEY_ACTIVATED_AUDIT_ACTION, context=context
    )
    return PolicyKeyLifecycleOutcome(
        action=ACTION_ACTIVATED,
        key_id=staged_signer.key_id,
        keyset_revision=persisted.keyset_revision,
        is_replay=persisted.is_replay,
    )


async def execute_policy_key_retire(
    *,
    engine: AsyncEngine,
    workspace_id: UUID,
    retiring_key_id: str,
    signer: LoadedPolicySigningKey,
    context: DiagnosticContext,
) -> PolicyKeyLifecycleOutcome:
    """Retire one non-current trusted key after the operating overlap (13.3).

    The current key can never be retired directly and an already retired key
    acknowledges the earlier retirement without appending rows. The revision
    is signed by the current key alone: retirement removes trust rather than
    extending it, so no proof-of-possession from the retired key exists.
    """

    validate_policy_signing_key_id_text(retiring_key_id)
    record, row_ids = await load_policy_keyset_state(engine, workspace_id, context)
    if record is None:
        raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
    view = parse_policy_keyset_payload(record.canonical_payload_bytes)
    _require_signer_is_current(view, signer.key_id)
    state = view.state_of(retiring_key_id)
    if state is None:
        raise _rejection("key_unknown")
    if state is PolicyKeysetState.RETIRED:
        return PolicyKeyLifecycleOutcome(
            action=ACTION_RETIRED,
            key_id=retiring_key_id,
            keyset_revision=record.keyset_revision,
            is_replay=True,
        )
    keys = plan_retired_keys(view, retiring_key_id)
    envelope = build_lifecycle_keyset_envelope(
        workspace_id=workspace_id,
        keyset_revision=record.keyset_revision + 1,
        keys=keys,
        row_ids_by_key_id=row_ids,
        signers=(signer,),
        created_at=_now_utc(),
    )
    persisted = await append_keyset_revision_with_audit(
        engine, envelope, audit_action=KEY_RETIRED_AUDIT_ACTION, context=context
    )
    return PolicyKeyLifecycleOutcome(
        action=ACTION_RETIRED,
        key_id=retiring_key_id,
        keyset_revision=persisted.keyset_revision,
        is_replay=persisted.is_replay,
    )


# --- the CLI shell wrappers -----------------------------------------------------------


def _run_async[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    """Drive one coroutine on a selector event loop (psycopg requirement)."""

    return asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(coroutine)


def _load_cli_signer(secret_root: Path) -> LoadedPolicySigningKey:
    """Load the configured signer from the environment snapshot, fail closed.

    The settings loader already resolved the exact file beneath the configured
    secret root and validated the announced key ID grammar; this boundary read
    re-loads the same file, enforces the PEM material contract and refuses the
    private/public mismatch exactly like the startup path (spec 13.1).
    """

    settings = load_exclusion_policy_signing_settings()
    resolved_root = secret_root.resolve(strict=True)
    secret = read_secret_file(
        settings.signing_key_file,
        resolved_root,
        maximum_size_bytes=POLICY_SIGNING_KEY_FILE_MAXIMUM_BYTES,
    )
    loaded = _loaded_signing_key_from_secret(secret)
    if loaded.key_id != settings.signing_key_id:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_SECRET_INVALID,
            safe_details={"reason": SafeToken.parse("key_id_mismatch")},
        )
    return loaded


def _dispatch_policy_key_command(arguments: Namespace) -> int:
    from personal_os.diagnostics.context import create_diagnostic_context
    from postgresql_source_store.engine import (
        create_source_store_engine,
        dispose_source_store_engine,
    )
    from postgresql_source_store.settings import (
        load_database_runtime_settings,
        read_database_runtime_password,
    )

    command = arguments.policy_key_command
    workspace_id = validate_policy_key_workspace_id(arguments.workspace_id)
    context = create_diagnostic_context().context
    database_settings = load_database_runtime_settings()
    password = read_database_runtime_password(database_settings)

    async def _run() -> PolicyKeyLifecycleOutcome:
        engine = create_source_store_engine(database_settings, password)
        try:
            if command == "initialize":
                return await execute_policy_key_initialize(
                    engine=engine,
                    workspace_id=workspace_id,
                    key_file_name=validate_policy_key_file_name(arguments.key_file_name),
                    secret_root=database_settings.secret_root,
                    context=context,
                )
            if command == "stage":
                return await execute_policy_key_stage(
                    engine=engine,
                    workspace_id=workspace_id,
                    key_file_name=validate_policy_key_file_name(arguments.key_file_name),
                    secret_root=database_settings.secret_root,
                    signer=_load_cli_signer(database_settings.secret_root),
                    context=context,
                )
            if command == "activate":
                return await execute_policy_key_activate(
                    engine=engine,
                    workspace_id=workspace_id,
                    staged_key_file_name=validate_policy_key_file_name(
                        arguments.staged_key_file_name
                    ),
                    secret_root=database_settings.secret_root,
                    signer=_load_cli_signer(database_settings.secret_root),
                    context=context,
                )
            return await execute_policy_key_retire(
                engine=engine,
                workspace_id=workspace_id,
                retiring_key_id=validate_policy_signing_key_id_text(arguments.key_id),
                signer=_load_cli_signer(database_settings.secret_root),
                context=context,
            )
        finally:
            await dispose_source_store_engine(engine)

    outcome = _run_async(_run())
    print(render_policy_key_outcome(outcome))
    return _EXIT_SUCCESS


def run_policy_key_command(arguments: Namespace) -> int:
    """Map one ``policy-key`` invocation onto the closed exit-code contract."""

    try:
        return _dispatch_policy_key_command(arguments)
    except PolicyKeyCommandInputError as error:
        print(f"personal-api: {error.reason}", file=sys.stderr)
        return _EXIT_INPUT_INVALID
    except ApplicationError as error:
        print(
            f"personal-api: {error.error_code.value}: {error.safe_message}",
            file=sys.stderr,
        )
        return _EXIT_APPLICATION_REJECTED
    except Exception:
        print("personal-api: internal_error", file=sys.stderr)
        return _EXIT_INTERNAL
