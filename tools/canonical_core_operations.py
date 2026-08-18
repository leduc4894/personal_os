"""Repository-internal canonical-core operations CLI (design spec 4.2, 13, 14).

``uv run python tools/canonical_core_operations.py <subcommand>`` is the only
new cross-infrastructure composition root: it binds the merged domain services
(identity bootstrap, canonical read, recovery) to the PostgreSQL source-store
engine, the R2 object store, the bounded ``pg_dump``/``pg_restore`` process
boundary and the operator-owned private backup root.

Binding contract:

- ``--help``/``--version`` exit ``0`` and invalid syntax exits ``2`` with zero
  environment or secret reads (parse happens strictly before any I/O).
- Environment gates for ``backup-create``, ``restore-empty`` and
  ``phase-one-acceptance`` fire before any engine, R2 client, subprocess or
  bundle path is opened: ``KNOWLEDGE_ENVIRONMENT`` must be exactly ``local``
  or ``test``, otherwise the command refuses with exit ``78`` and one safe
  registered diagnostic on stderr.
- ``backup-create`` additionally requires the exact
  ``--confirm-write-admission-disabled`` admission flag; its absence is a
  configuration-class refusal (exit ``78``; spec 9.1 treats the flag as
  admission) carrying the operation token. ``restore-empty`` requires
  ``--confirm-target-database`` to equal ``--target-database`` exactly.
- Every recovery/acceptance command runs inside
  ``asyncio.wait_for(..., RECOVERY_COMMAND_TIMEOUT_SECONDS)`` with
  cancellation cleanup; ``ApplicationError`` values map onto the closed exit
  table (validation/conflict/integrity ``65``; dependency ``69``; internal
  ``70``; busy/retryable ``75``; configuration/authorization ``78``).
- Exactly one safe JSON document goes to stdout
  (``json.dumps(..., sort_keys=True, separators=(",", ":"))``); safe
  registered diagnostics go to stderr; raw child output is consumed and
  mapped by the adapters, never forwarded; no command prompts interactively.
- ``read-current-source`` writes bytes only to ``--output-file`` opened
  exclusively (``open(path, "xb")``) and never prints content.

``phase-one-acceptance`` composes the full design-spec-7 acceptance flow
(:func:`run_phase_one_acceptance` with injectable collaborators): identity
bootstrap and its exact replay, one synthetic device-actor publication with
preflight miss, R2 stream/store/full-verify, atomic PostgreSQL commit, exact
canonical read, exact publication replay (no object-store call, no new row),
the two fenced projection-intent claims converging on ONE deterministic
``source-ingestion/{workspace_id}/{event_id}`` Temporal execution (first start
``STARTED``, second ``EXISTING``; the execution may keep waiting on the
``source-ingestion`` task queue because Phase 1 registers no workflow
implementation), and one safe JSON summary of IDs and safe counts only. The
composition fails closed on an unreachable Temporal target — acceptance
requires the real dispatch and never silently skips it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Never, Protocol, TextIO, cast
from uuid import UUID

from personal_os.diagnostics.context import (
    bind_diagnostic_context,
    create_diagnostic_context,
)
from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.diagnostics.logging import (
    DiagnosticLogger,
    emit_emergency_application_error,
    emit_emergency_internal_error,
)
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.recovery.service import RECOVERY_COMMAND_TIMEOUT_SECONDS
from personal_os.runtime_configuration.models import ServiceName
from personal_os.sources.errors import ProjectionDispatchError

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import AsyncEngine
    from temporalio.client import Client as TemporalClient
    from tools.postgresql_dump_process import PostgresqlDumpProcessAdapter
    from workflow_worker.projection_workflow_starter import ProjectionWorkflowStarter

    from personal_os.diagnostics.context import DiagnosticContext
    from personal_os.exclusion_policy.enforcement import PolicyTrustAnchorVerifier
    from personal_os.identity.bootstrap import IdentityBootstrapService
    from personal_os.identity.contracts import BootstrapIdentityCommand
    from personal_os.object_storage.contracts import (
        ExpectedObject,
        VerifiedObjectReader,
        VerifiedObjectReceipt,
    )
    from personal_os.recovery.contracts import CanonicalAcceptanceMetrics, RecoveryEnvironment
    from personal_os.recovery.ports import (
        CanonicalBackupSnapshot,
        DumpReceipt,
        PostgresqlConnectionTarget,
        RecoveryBundleStore,
        RestoreReceipt,
    )
    from personal_os.runtime_configuration.models import CanonicalRecoverySettings
    from personal_os.sources.ports import AwareUtcClock, ProjectionIntentStore
    from personal_os.sources.publication import SourceVersionPublicationService
    from personal_os.sources.reading import CanonicalSourceReadService
    from postgresql_source_store.settings import DatabaseRuntimeSettings
    from r2_object_storage.adapter import R2S3ObjectStore
    from r2_object_storage.client import R2ClientManager

__all__ = [
    "RECOVERY_COMMAND_TIMEOUT_SECONDS",
    "BackupCreateInvocation",
    "BackupVerifyInvocation",
    "BootstrapIdentityInvocation",
    "CanonicalCoreExitCode",
    "PhaseOneAcceptanceCollaborators",
    "PhaseOneAcceptanceInvocation",
    "ReadCurrentSourceInvocation",
    "RestoreEmptyInvocation",
    "exit_code_for_application_error",
    "main",
    "run_phase_one_acceptance",
]

PROGRAM_NAME: Final[str] = "canonical_core_operations"

#: The only environments in which the gated recovery operations may run.
_ALLOWED_OPERATION_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"local", "test"})

#: ``KNOWLEDGE_ENVIRONMENT`` defaults to the local environment, mirroring the
#: runtime-configuration fragment defaults.
_DEFAULT_ENVIRONMENT: Final[str] = "local"

#: The internal operations run in the worker service context for diagnostics.
_OPERATIONS_SERVICE: Final[ServiceName] = ServiceName.WORKER

_PG_DUMP_TOOL_NAME: Final[str] = "pg_dump"
_PG_RESTORE_TOOL_NAME: Final[str] = "pg_restore"

#: Caller-side bound for the acceptance composition's Temporal client connect
#: (mirroring the worker dispatcher's bounded connect over the same target).
_TEMPORAL_CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0

#: Safe token naming the acceptance operation in failure diagnostics.
_ACCEPTANCE_OPERATION_TOKEN: Final[SafeToken] = SafeToken.parse("phase_one_acceptance")

#: The pinned media type of the synthetic acceptance publication.
_ACCEPTANCE_MEDIA_TYPE: Final[str] = "text/markdown"


class CanonicalCoreExitCode(IntEnum):
    """Stable exit codes for the canonical core operations CLI."""

    OK = 0
    CLI = 2
    CONTRACT = 65
    UNAVAILABLE = 69
    INTERNAL = 70
    BUSY = 75
    CONFIG = 78


_CONTRACT_CATEGORIES: Final[frozenset[ErrorCategory]] = frozenset(
    {ErrorCategory.VALIDATION, ErrorCategory.CONFLICT, ErrorCategory.INTEGRITY}
)
_REFUSAL_CATEGORIES: Final[frozenset[ErrorCategory]] = frozenset(
    {ErrorCategory.CONFIGURATION, ErrorCategory.AUTHORIZATION}
)


def exit_code_for_application_error(error: ApplicationError) -> CanonicalCoreExitCode:
    """Map one typed application error onto the closed CLI exit table."""

    if error.category in _CONTRACT_CATEGORIES:
        return CanonicalCoreExitCode.CONTRACT
    if error.category in _REFUSAL_CATEGORIES:
        return CanonicalCoreExitCode.CONFIG
    if error.category is ErrorCategory.INTERNAL:
        return CanonicalCoreExitCode.INTERNAL
    # Dependency: a retryable dependency failure is the busy class; every
    # other dependency failure is plain unavailability.
    if error.is_retryable:
        return CanonicalCoreExitCode.BUSY
    return CanonicalCoreExitCode.UNAVAILABLE


# --- CLI surface -------------------------------------------------------------


class _HelpRequested(Exception):
    pass


class _CliSyntaxFailure(Exception):
    """A parse-time syntax failure; the carried result code is a safe token."""

    def __init__(self, result_code: str) -> None:
        super().__init__(result_code)
        self.result_code = result_code


class _RecoveryCommandTimeout(Exception):
    """A command exceeded the whole-command recovery bound."""


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise _CliSyntaxFailure("invalid_cli")

    def exit(self, status: int = 0, message: str | None = None) -> Never:
        if status == 0:
            if message:
                self._print_message(message, sys.stdout)
            raise _HelpRequested
        raise _CliSyntaxFailure("invalid_cli")


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        prog=PROGRAM_NAME,
        description=(
            "Repository-internal canonical core operations: identity bootstrap, "
            "canonical read, backup creation, offline bundle verification, "
            "empty-target restore and the phase-one acceptance gate."
        ),
        exit_on_error=False,
    )
    parser.add_argument("--version", dest="should_show_version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    bootstrap_parser = subparsers.add_parser("bootstrap-identity", exit_on_error=False)
    bootstrap_parser.add_argument("--username", required=True)
    bootstrap_parser.add_argument("--user-display-name", required=True)
    bootstrap_parser.add_argument("--workspace-key", required=True)
    bootstrap_parser.add_argument("--workspace-display-name", required=True)
    bootstrap_parser.add_argument("--device-name", required=True)
    bootstrap_parser.add_argument("--device-kind", required=True)

    read_parser = subparsers.add_parser("read-current-source", exit_on_error=False)
    read_parser.add_argument("--workspace-id", required=True, type=uuid.UUID)
    read_parser.add_argument("--source-id", required=True, type=uuid.UUID)
    read_parser.add_argument("--output-file", required=True, type=Path)

    backup_create_parser = subparsers.add_parser("backup-create", exit_on_error=False)
    backup_create_parser.add_argument(
        "--confirm-write-admission-disabled",
        dest="confirm_write_admission_disabled",
        action="store_true",
    )

    backup_verify_parser = subparsers.add_parser("backup-verify", exit_on_error=False)
    backup_verify_parser.add_argument("--bundle-id", required=True, type=uuid.UUID)

    restore_parser = subparsers.add_parser("restore-empty", exit_on_error=False)
    restore_parser.add_argument("--bundle-id", required=True, type=uuid.UUID)
    restore_parser.add_argument("--target-database", required=True)
    restore_parser.add_argument("--confirm-target-database", required=True)

    subparsers.add_parser("phase-one-acceptance", exit_on_error=False)
    return parser


# --- Invocations and composition seams ----------------------------------------


@dataclass(frozen=True, slots=True)
class BootstrapIdentityInvocation:
    """One identity-bootstrap request; the service validates its fields."""

    username: str
    user_display_name: str
    workspace_key: str
    workspace_display_name: str
    device_name: str
    device_kind: str


@dataclass(frozen=True, slots=True)
class ReadCurrentSourceInvocation:
    """One canonical current-source read request with its exclusive output."""

    workspace_id: UUID
    source_id: UUID
    output_file: Path


@dataclass(frozen=True, slots=True)
class BackupCreateInvocation:
    """One backup-creation request; write admission was already confirmed."""


@dataclass(frozen=True, slots=True)
class BackupVerifyInvocation:
    """One offline bundle-verification request."""

    bundle_id: UUID


@dataclass(frozen=True, slots=True)
class RestoreEmptyInvocation:
    """One empty-target restore request; the target was already confirmed."""

    bundle_id: UUID
    target_database: str


@dataclass(frozen=True, slots=True)
class PhaseOneAcceptanceInvocation:
    """One phase-one acceptance request over a disposable canonical database."""


class CommandComposition(Protocol):
    """One composed subcommand owning its full resource lifecycle."""

    async def run(self) -> Mapping[str, object]: ...


type BootstrapIdentityComposer = Callable[
    [BootstrapIdentityInvocation, Mapping[str, str]], CommandComposition
]
type ReadCurrentSourceComposer = Callable[
    [ReadCurrentSourceInvocation, Mapping[str, str]], CommandComposition
]
type BackupCreateComposer = Callable[
    [BackupCreateInvocation, Mapping[str, str]], CommandComposition
]
type BackupVerifyComposer = Callable[
    [BackupVerifyInvocation, Mapping[str, str]], CommandComposition
]
type RestoreEmptyComposer = Callable[
    [RestoreEmptyInvocation, Mapping[str, str]], CommandComposition
]
type PhaseOneAcceptanceComposer = Callable[
    [PhaseOneAcceptanceInvocation, Mapping[str, str]], CommandComposition
]


class _ClosureComposition:
    """Adapts one lifecycle-owning coroutine into the composition protocol."""

    def __init__(self, runner: Callable[[], Awaitable[Mapping[str, object]]]) -> None:
        self._runner = runner

    async def run(self) -> Mapping[str, object]:
        return await self._runner()


# --- Gates, mapping and output helpers ----------------------------------------


def _require_local_or_test_environment(environ: Mapping[str, str], operation: str) -> None:
    """Refuse any environment other than exactly ``local`` or ``test``."""

    environment = environ.get("KNOWLEDGE_ENVIRONMENT", _DEFAULT_ENVIRONMENT)
    if environment not in _ALLOWED_OPERATION_ENVIRONMENTS:
        raise _environment_refused(operation)


def _require_write_admission(is_admitted: bool) -> None:
    """Require the exact write-admission confirmation flag (spec 9.1)."""

    if not is_admitted:
        raise _environment_refused("backup_create")


def _environment_refused(operation: str) -> ApplicationError:
    from personal_os.recovery.contracts import RecoveryError

    return RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED,
        safe_details={"operation": SafeToken.parse(operation)},
    )


def _recovery_environment(value: str, operation: str) -> RecoveryEnvironment:
    """Build the closed recovery environment token or refuse the operation."""

    from personal_os.recovery.contracts import RecoveryEnvironment

    if value not in _ALLOWED_OPERATION_ENVIRONMENTS:
        raise _environment_refused(operation)
    return RecoveryEnvironment(value)


def _print_json(stream: TextIO, payload: Mapping[str, object]) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _print_version(stream: TextIO) -> None:
    from personal_os.package_metadata import distribution_version

    stream.write(f"{PROGRAM_NAME} {distribution_version()}\n")


def _install_null_log_guard() -> None:
    """Keep unconfigured dependency logging from echoing to the console."""

    root_logger = logging.getLogger()
    if not any(isinstance(handler, logging.NullHandler) for handler in root_logger.handlers):
        root_logger.addHandler(logging.NullHandler())


def _write_exclusive_output_file(path: Path, content: bytes) -> None:
    """Create ``path`` exclusively and write the bytes; never print content."""

    try:
        with open(path, "xb") as output_file:
            output_file.write(content)
    except FileExistsError:
        raise _CliSyntaxFailure("output_file_exists") from None


async def _read_current_source_with_exclusive_output(
    composition: CommandComposition,
    output_file: Path,
    timeout_seconds: float,
) -> Mapping[str, object]:
    """Read the canonical bytes and write them only to an exclusive new file."""

    async def read_and_write_exclusively() -> Mapping[str, object]:
        result = await composition.run()
        content = cast(bytes, result.get("content_bytes", b""))
        _write_exclusive_output_file(output_file, content)
        return {
            "result_code": "canonical_source_read_complete",
            "size_bytes": len(content),
        }

    return await asyncio.wait_for(read_and_write_exclusively(), timeout=timeout_seconds)


def _recovery_clock() -> datetime:
    return datetime.now(UTC)


def _operations_diagnostic_logger() -> DiagnosticLogger:
    """The CLI's validating diagnostic sink bound into every composed service.

    Delivery uses the same validating facade the acceptance composition binds:
    events are registry-validated here and routed through the configured
    stdlib logging (guarded silent by default in this CLI process).
    """
    return DiagnosticLogger({"service": _OPERATIONS_SERVICE.value})


def _policy_enforcement_verifier() -> PolicyTrustAnchorVerifier:
    """The trust-anchor Ed25519 verifier shared by the guarded compositions.

    Backend enforcement resolves each revision's trust anchor from canonical
    PostgreSQL state, so one process-wide verifier that verifies under
    exactly the provided anchor bytes serves every workspace.
    """

    from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier

    return TrustAnchorEd25519Verifier()


async def _seed_signed_empty_policy(
    engine: AsyncEngine,
    *,
    workspace_id: UUID,
    owner_user_id: UUID,
) -> None:
    """Seed the workspace's revision-1 signed empty policy (spec 14)."""

    try:
        from tools.signed_policy_seed import seed_signed_policy
    except ImportError:
        seed_signed_policy = _sibling_tool_member("signed_policy_seed", "seed_signed_policy")
    await seed_signed_policy(engine, workspace_id=workspace_id, published_by_user_id=owner_user_id)


def _uuid_text(value: UUID) -> str:
    return str(value)


# --- Offline-verification unused ports ----------------------------------------


class _UnusedSnapshotStore:
    """Structural snapshot-port stand-in an offline verification never touches."""

    def open_quiesced_snapshot(
        self, now: datetime
    ) -> AbstractAsyncContextManager[CanonicalBackupSnapshot]:
        del now
        raise ApplicationError(ErrorCode.INTERNAL_ERROR)

    async def observe_pending_writers(self) -> int:
        raise ApplicationError(ErrorCode.INTERNAL_ERROR)


class _UnusedDumpProcess:
    """Structural dump-port stand-in an offline verification never touches."""

    async def create_dump(
        self,
        snapshot_token: str,
        output_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> DumpReceipt:
        del snapshot_token, output_file, target, timeout_seconds
        raise ApplicationError(ErrorCode.INTERNAL_ERROR)

    async def restore_dump(
        self,
        input_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> RestoreReceipt:
        del input_file, target, timeout_seconds
        raise ApplicationError(ErrorCode.INTERNAL_ERROR)


class _UnusedObjectStore:
    """Structural object-store stand-in an offline verification never touches."""

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        del expected
        raise ApplicationError(ErrorCode.INTERNAL_ERROR)

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        del stream, expected_size_bytes, media_type, claimed_sha256
        raise ApplicationError(ErrorCode.INTERNAL_ERROR)

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        del expected
        raise ApplicationError(ErrorCode.INTERNAL_ERROR)

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[VerifiedObjectReader]:
        del expected
        raise ApplicationError(ErrorCode.INTERNAL_ERROR)


# --- Shared composition helpers ------------------------------------------------


def _load_canonical_recovery_settings(environ: Mapping[str, str]) -> CanonicalRecoverySettings:
    from personal_os.runtime_configuration.loading import load_canonical_recovery_settings

    return load_canonical_recovery_settings(environ=environ)


def _sibling_tool_member(module_name: str, member_name: str) -> Any:
    """Resolve one member of a sibling tools module in direct script mode.

    ``python tools/canonical_core_operations.py`` puts ``tools/`` itself (not
    the repository root) on ``sys.path``; the sibling module is then importable
    only by its bare name. ``importlib.import_module`` keeps that resolution
    dynamic so no ``sys.path`` mutation is ever needed.
    """
    import importlib

    return getattr(importlib.import_module(module_name), member_name)


def _build_bundle_store(recovery_settings: CanonicalRecoverySettings) -> RecoveryBundleStore:
    try:
        from tools.canonical_recovery_bundle import build_bundle_store
    except ImportError:
        build_bundle_store = _sibling_tool_member("canonical_recovery_bundle", "build_bundle_store")
    return build_bundle_store(recovery_settings)


def _load_database_parts(
    environ: Mapping[str, str],
) -> tuple[DatabaseRuntimeSettings, SecretStr]:
    from postgresql_source_store.settings import (
        load_database_runtime_settings,
        read_database_runtime_password,
    )

    database_settings = load_database_runtime_settings(environ=environ)
    return database_settings, read_database_runtime_password(database_settings)


def _create_database_engine(
    database_settings: DatabaseRuntimeSettings, password: SecretStr
) -> AsyncEngine:
    from postgresql_source_store.engine import create_source_store_engine

    return create_source_store_engine(database_settings, password)


def _connection_target(database_settings: DatabaseRuntimeSettings) -> PostgresqlConnectionTarget:
    from personal_os.recovery.ports import PostgresqlConnectionTarget as Target

    return Target(
        host=database_settings.host,
        port=database_settings.port,
        database=database_settings.database_name,
        user=database_settings.database_user,
    )


def _compose_dump_process(password: SecretStr) -> PostgresqlDumpProcessAdapter:
    adapter_factory: type[PostgresqlDumpProcessAdapter]
    try:
        from tools.postgresql_dump_process import (
            PostgresqlDumpProcessAdapter as adapter_factory,
        )
    except ImportError:
        adapter_factory = _sibling_tool_member(
            "postgresql_dump_process", "PostgresqlDumpProcessAdapter"
        )
    return adapter_factory(
        _PG_DUMP_TOOL_NAME,
        _PG_RESTORE_TOOL_NAME,
        password=password,
    )


async def _check_client_tools() -> None:
    client_tools_check: Callable[[str, str], Awaitable[None]]
    try:
        from tools.postgresql_dump_process import (
            check_client_tools as client_tools_check,
        )
    except ImportError:
        client_tools_check = _sibling_tool_member("postgresql_dump_process", "check_client_tools")
    await client_tools_check(_PG_DUMP_TOOL_NAME, _PG_RESTORE_TOOL_NAME)


async def _open_r2_object_store(
    environ: Mapping[str, str],
) -> tuple[R2ClientManager, R2S3ObjectStore]:
    from personal_os.diagnostics.logging import DiagnosticLogger
    from r2_object_storage.adapter import R2S3ObjectStore
    from r2_object_storage.client import R2ClientManager
    from r2_object_storage.error_mapping import RetryPolicy
    from r2_object_storage.metrics import InMemoryObjectStorageMetrics
    from r2_object_storage.settings import load_object_storage_settings
    from r2_object_storage.spool import SpoolManager

    object_settings, credentials = load_object_storage_settings(environ=environ)
    manager = R2ClientManager(object_settings, credentials)
    client = await manager.get_client()
    store = R2S3ObjectStore(
        client,
        spools=SpoolManager(object_settings.object_storage_spool_root),
        retry=RetryPolicy(),
        metrics=InMemoryObjectStorageMetrics(),
        logger=DiagnosticLogger(
            {
                "service": _OPERATIONS_SERVICE.value,
                "environment": object_settings.environment.value,
            }
        ),
    )
    return manager, store


async def _close_r2_object_store(
    manager: R2ClientManager | None, object_store: R2S3ObjectStore | None
) -> None:
    if object_store is not None:
        with contextlib.suppress(Exception):
            await object_store.close()
    if manager is not None:
        with contextlib.suppress(Exception):
            await manager.close()


# --- Default compositions ------------------------------------------------------


def _compose_bootstrap_identity(
    invocation: BootstrapIdentityInvocation, environ: Mapping[str, str]
) -> CommandComposition:
    """Engine -> identity store -> bootstrap service (disposable state only)."""

    from personal_os.identity.bootstrap import IdentityBootstrapService
    from personal_os.identity.contracts import (
        InMemoryIdentityBootstrapMetrics,
        validate_bootstrap_identity_command,
    )
    from postgresql_source_store.engine import dispose_source_store_engine
    from postgresql_source_store.identity_bootstrap import PostgresqlIdentityBootstrapStore

    command = validate_bootstrap_identity_command(
        username=invocation.username,
        user_display_name=invocation.user_display_name,
        workspace_key=invocation.workspace_key,
        workspace_display_name=invocation.workspace_display_name,
        device_name=invocation.device_name,
        device_kind=invocation.device_kind,
    )
    database_settings, password = _load_database_parts(environ)
    engine = _create_database_engine(database_settings, password)
    store = PostgresqlIdentityBootstrapStore(engine)
    service = IdentityBootstrapService(
        store=store,
        metrics=InMemoryIdentityBootstrapMetrics(),
        diagnostics=_operations_diagnostic_logger(),
    )

    async def run() -> Mapping[str, object]:
        resolution = create_diagnostic_context()
        try:
            with bind_diagnostic_context(resolution.context):
                result = await service.bootstrap(command, resolution.context)
            return {
                "result_code": f"identity_bootstrap_{result.outcome.value}",
                "outcome": result.outcome.value,
                "user_id": _uuid_text(result.user_id),
                "workspace_id": _uuid_text(result.workspace_id),
                "device_id": _uuid_text(result.device_id),
                "committed_at": result.committed_at.isoformat(),
            }
        finally:
            await dispose_source_store_engine(engine)

    return _ClosureComposition(run)


def _compose_read_current_source(
    invocation: ReadCurrentSourceInvocation, environ: Mapping[str, str]
) -> CommandComposition:
    """Engine -> guarded read store + verified object reader -> canonical bytes."""

    from personal_os.exclusion_policy.metrics import InMemoryExclusionPolicyMetrics
    from personal_os.sources.metrics import InMemoryCanonicalReadMetrics
    from personal_os.sources.reading import CanonicalSourceReadService, ReadCurrentSourceCommand
    from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
    from postgresql_source_store.engine import dispose_source_store_engine
    from postgresql_source_store.policy_enforcement import compose_policy_enforcement

    database_settings, password = _load_database_parts(environ)
    engine = _create_database_engine(database_settings, password)
    policy_verifier = _policy_enforcement_verifier()
    policy_metrics = InMemoryExclusionPolicyMetrics()
    read_store = PostgresqlCanonicalSourceReadStore(
        engine,
        policy_verifier=policy_verifier,
        policy_metrics=policy_metrics,
    )
    command = ReadCurrentSourceCommand(
        workspace_id=invocation.workspace_id, source_id=invocation.source_id
    )

    async def run() -> Mapping[str, object]:
        resolution = create_diagnostic_context()
        manager: R2ClientManager | None = None
        object_store: R2S3ObjectStore | None = None
        try:
            with bind_diagnostic_context(resolution.context):
                manager, object_store = await _open_r2_object_store(environ)
                service = CanonicalSourceReadService(
                    store=read_store,
                    object_store=object_store,
                    metrics=InMemoryCanonicalReadMetrics(),
                    policy_guard=compose_policy_enforcement(
                        engine,
                        verifier=policy_verifier,
                        metrics=policy_metrics,
                    ),
                    diagnostics=_operations_diagnostic_logger(),
                )
                content = await service.read_current_source_bytes(command, resolution.context)
            return {"content_bytes": content}
        finally:
            await _close_r2_object_store(manager, object_store)
            await dispose_source_store_engine(engine)

    return _ClosureComposition(run)


def _compose_backup_create(
    invocation: BackupCreateInvocation, environ: Mapping[str, str]
) -> CommandComposition:
    """Snapshot store + R2 store + dump adapter + bundle store -> backup."""

    from personal_os.recovery.contracts import InMemoryCanonicalBackupMetrics
    from personal_os.recovery.service import BackupCreateCommand, RecoveryService
    from postgresql_source_store.backup_snapshot import PostgresqlBackupSnapshotStore
    from postgresql_source_store.engine import dispose_source_store_engine

    del invocation
    recovery_settings = _load_canonical_recovery_settings(environ)
    database_settings, password = _load_database_parts(environ)
    engine = _create_database_engine(database_settings, password)
    snapshot_store = PostgresqlBackupSnapshotStore(engine)
    dump_process = _compose_dump_process(password)
    command = BackupCreateCommand(
        environment=_recovery_environment(recovery_settings.environment.value, "backup_create"),
        target=_connection_target(database_settings),
    )

    async def run() -> Mapping[str, object]:
        await _check_client_tools()
        resolution = create_diagnostic_context()
        manager: R2ClientManager | None = None
        object_store: R2S3ObjectStore | None = None
        try:
            with bind_diagnostic_context(resolution.context):
                manager, object_store = await _open_r2_object_store(environ)
                service = RecoveryService(
                    snapshot_store=snapshot_store,
                    bundle_store=_build_bundle_store(recovery_settings),
                    dump_process=dump_process,
                    object_store=object_store,
                    metrics=InMemoryCanonicalBackupMetrics(),
                    clock=_recovery_clock,
                    diagnostics=_operations_diagnostic_logger(),
                )
                result = await service.create_backup(command)
            return {
                "result_code": "canonical_backup_created",
                "bundle_id": _uuid_text(result.bundle_id),
                "object_count": result.object_count,
                "byte_total": result.byte_total,
                "duration_seconds": result.duration_seconds,
            }
        finally:
            await _close_r2_object_store(manager, object_store)
            await dispose_source_store_engine(engine)

    return _ClosureComposition(run)


def _compose_backup_verify(
    invocation: BackupVerifyInvocation, environ: Mapping[str, str]
) -> CommandComposition:
    """Offline bundle verification: bundle store only, no engine or R2 client."""

    from personal_os.recovery.contracts import InMemoryCanonicalBackupMetrics
    from personal_os.recovery.service import RecoveryService, VerifyBundleCommand

    recovery_settings = _load_canonical_recovery_settings(environ)
    command = VerifyBundleCommand(
        environment=_recovery_environment(recovery_settings.environment.value, "backup_verify"),
        bundle_id=invocation.bundle_id,
    )

    async def run() -> Mapping[str, object]:
        resolution = create_diagnostic_context()
        with bind_diagnostic_context(resolution.context):
            service = RecoveryService(
                snapshot_store=_UnusedSnapshotStore(),
                bundle_store=_build_bundle_store(recovery_settings),
                dump_process=_UnusedDumpProcess(),
                object_store=_UnusedObjectStore(),
                metrics=InMemoryCanonicalBackupMetrics(),
                clock=_recovery_clock,
                diagnostics=_operations_diagnostic_logger(),
            )
            result = await service.verify_bundle(command)
        return {
            "result_code": "canonical_backup_verified",
            "bundle_id": _uuid_text(result.bundle_id),
            "contract": result.contract,
            "object_count": result.object_count,
            "byte_total": result.byte_total,
            "table_counts": dict(result.table_counts),
        }

    return _ClosureComposition(run)


def _compose_restore_empty(
    invocation: RestoreEmptyInvocation, environ: Mapping[str, str]
) -> CommandComposition:
    """Restore target + read service + dump adapter + bundle store -> restore."""

    from personal_os.exclusion_policy.metrics import InMemoryExclusionPolicyMetrics
    from personal_os.recovery.contracts import InMemoryCanonicalBackupMetrics
    from personal_os.recovery.service import RecoveryService, RestoreEmptyCommand
    from personal_os.sources.metrics import InMemoryCanonicalReadMetrics
    from personal_os.sources.reading import CanonicalSourceReadService
    from postgresql_source_store.backup_snapshot import PostgresqlRestoreTarget
    from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
    from postgresql_source_store.engine import dispose_source_store_engine
    from postgresql_source_store.policy_enforcement import compose_policy_enforcement

    recovery_settings = _load_canonical_recovery_settings(environ)
    database_settings, password = _load_database_parts(environ)
    engine = _create_database_engine(database_settings, password)
    restore_target = PostgresqlRestoreTarget(engine)
    policy_verifier = _policy_enforcement_verifier()
    policy_metrics = InMemoryExclusionPolicyMetrics()
    read_store = PostgresqlCanonicalSourceReadStore(
        engine,
        policy_verifier=policy_verifier,
        policy_metrics=policy_metrics,
    )
    dump_process = _compose_dump_process(password)
    command = RestoreEmptyCommand(
        environment=_recovery_environment(recovery_settings.environment.value, "restore_empty"),
        bundle_id=invocation.bundle_id,
        target=_connection_target(database_settings),
        target_confirmation=invocation.target_database,
        acceptance_probe=None,
    )

    async def run() -> Mapping[str, object]:
        await _check_client_tools()
        resolution = create_diagnostic_context()
        manager: R2ClientManager | None = None
        object_store: R2S3ObjectStore | None = None
        try:
            with bind_diagnostic_context(resolution.context):
                manager, object_store = await _open_r2_object_store(environ)
                read_service = CanonicalSourceReadService(
                    store=read_store,
                    object_store=object_store,
                    metrics=InMemoryCanonicalReadMetrics(),
                    policy_guard=compose_policy_enforcement(
                        engine,
                        verifier=policy_verifier,
                        metrics=policy_metrics,
                    ),
                    diagnostics=_operations_diagnostic_logger(),
                )
                service = RecoveryService(
                    snapshot_store=_UnusedSnapshotStore(),
                    bundle_store=_build_bundle_store(recovery_settings),
                    dump_process=dump_process,
                    object_store=object_store,
                    metrics=InMemoryCanonicalBackupMetrics(),
                    clock=_recovery_clock,
                    diagnostics=_operations_diagnostic_logger(),
                )
                result = await service.restore_empty(
                    command,
                    read_service=read_service,
                    restore_target=restore_target,
                    diagnostic_context=resolution.context,
                )
            return {
                "result_code": "canonical_restore_succeeded",
                "bundle_id": _uuid_text(result.bundle_id),
                "completed_at": result.completed_at.isoformat(),
                "table_counts": dict(result.table_counts),
                "object_count": result.object_count,
            }
        finally:
            await _close_r2_object_store(manager, object_store)
            await dispose_source_store_engine(engine)

    return _ClosureComposition(run)


# --- Phase one acceptance composition (design spec 7) --------------------------


class AcceptanceDiagnosticSink(Protocol):
    """Structural diagnostic sink the composition satisfies with its logger."""

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None: ...


class SignedEmptyPolicySeeder(Protocol):
    """Publishes the signed empty policy a fresh smoke workspace needs.

    Spec 14: before revision 1 every canonical content operation fails
    closed, so the internal smoke fixture explicitly publishes a signed
    empty policy between identity bootstrap and the first publication.
    """

    async def seed(
        self, workspace_id: UUID, owner_user_id: UUID, diagnostic_context: DiagnosticContext
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PhaseOneAcceptanceCollaborators:
    """Every injectable collaborator of the design-spec-7 acceptance flow.

    Unit tests satisfy each port with fakes; the CLI composition binds the
    production PostgreSQL adapters, the R2 object store, the Temporal
    workflow starter and the signed empty-policy seeder. ``table_counts``
    returns the per-table row counts of the canonical database so the
    no-new-row proofs never trust the services, and the clock seam feeds
    every time-dependent decision.
    """

    identity_service: IdentityBootstrapService
    publication_service: SourceVersionPublicationService
    read_service: CanonicalSourceReadService
    policy_seeder: SignedEmptyPolicySeeder
    intent_store: ProjectionIntentStore
    workflow_starter: ProjectionWorkflowStarter
    table_counts: Callable[[], Awaitable[Mapping[str, int]]]
    diagnostics: AcceptanceDiagnosticSink
    metrics: CanonicalAcceptanceMetrics
    clock: AwareUtcClock


def _acceptance_integrity_failure(component: str) -> ApplicationError:
    """One typed integrity failure naming only the closed component token."""

    from personal_os.recovery.contracts import RecoveryError

    return RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED,
        safe_details={"component": SafeToken.parse(component)},
    )


def build_synthetic_bootstrap_command() -> BootstrapIdentityCommand:
    """Build one unique synthetic identity-bootstrap command for this run.

    The values are non-personal, per-run unique and pass the same closed
    validation the ``bootstrap-identity`` subcommand applies, so the acceptance
    bootstrap starts from a pristine disposable identity graph.
    """

    from personal_os.identity.contracts import validate_bootstrap_identity_command

    nonce = uuid.uuid4().hex[:12]
    return validate_bootstrap_identity_command(
        username=f"acceptance-{nonce}",
        user_display_name="Phase One Acceptance Owner",
        workspace_key=f"acceptance-{nonce}",
        workspace_display_name="Phase One Acceptance Workspace",
        device_name="Phase One Acceptance Device",
        device_kind="system",
    )


async def _single_chunk_stream(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _elapsed_ms_since(started_monotonic: float) -> int:
    return max(0, int((time.monotonic() - started_monotonic) * 1000))


def _acceptance_failure_fields(
    error_code: str, error_category: str, is_retryable: bool, duration_ms: int
) -> dict[str, object]:
    """The registered failure-event payload; closed tokens and counts only."""

    return {
        "operation": _ACCEPTANCE_OPERATION_TOKEN,
        "outcome": SafeToken.parse("failed"),
        "duration_ms": duration_ms,
        "error_code": SafeToken.parse(error_code),
        "error_category": SafeToken.parse(error_category),
        "is_retryable": is_retryable,
    }


async def _run_acceptance_flow(
    collaborators: PhaseOneAcceptanceCollaborators,
    command: BootstrapIdentityCommand,
    diagnostic_context: DiagnosticContext,
) -> tuple[Mapping[str, object], dict[str, object]]:
    """Execute the spec-7 flow and return the safe summary plus event fields."""

    from workflow_worker.projection_workflow_starter import (
        SOURCE_INGESTION_REFERENCE_CONTRACT,
        ProjectionWorkflowStartResult,
        projection_workflow_id,
        source_ingestion_reference_for_intent,
    )

    from personal_os.identity.contracts import BootstrapIdentityOutcome
    from personal_os.object_storage import (
        CanonicalMediaType,
        ContentDigest,
        ExpectedObject,
    )
    from personal_os.sources.actors import ActorKind, SourceActor
    from personal_os.sources.commands import (
        CreateSourceVersion,
        IdempotencyKey,
        SourceTitle,
        SourceType,
    )
    from personal_os.sources.projection_dispatch import PROJECTION_CLAIM_BATCH_LIMIT
    from personal_os.sources.reading import ReadCurrentSourceCommand
    from personal_os.sources.results import PublicationOutcome

    # 1. Identity bootstrap creates the canonical identity graph.
    bootstrapped = await collaborators.identity_service.bootstrap(command, diagnostic_context)
    if bootstrapped.outcome is not BootstrapIdentityOutcome.CREATED:
        raise _acceptance_integrity_failure("identity_bootstrap")
    counts_after_bootstrap = await collaborators.table_counts()

    # 2. Exact bootstrap replay: same ids, original committed timestamp, and
    #    no new row in any canonical table.
    replayed_bootstrap = await collaborators.identity_service.bootstrap(command, diagnostic_context)
    if (
        replayed_bootstrap.outcome is not BootstrapIdentityOutcome.EXISTING
        or replayed_bootstrap.user_id != bootstrapped.user_id
        or replayed_bootstrap.workspace_id != bootstrapped.workspace_id
        or replayed_bootstrap.device_id != bootstrapped.device_id
        or replayed_bootstrap.committed_at != bootstrapped.committed_at
    ):
        raise _acceptance_integrity_failure("identity_bootstrap_replay")
    if await collaborators.table_counts() != counts_after_bootstrap:
        raise _acceptance_integrity_failure("identity_bootstrap_replay_row_count")

    # 2b. Explicitly publish the signed empty policy (spec 14): before
    #     revision 1 every canonical content operation fails closed.
    await collaborators.policy_seeder.seed(
        bootstrapped.workspace_id, bootstrapped.user_id, diagnostic_context
    )

    # 3. The synthetic bootstrap device publishes one unique synthetic source.
    source_id = uuid.uuid4()
    event_id = uuid.uuid4()
    payload = f"# Phase one acceptance\n\nsource {source_id}\nrun {uuid.uuid4()}\n".encode()
    create_command = CreateSourceVersion(
        workspace_id=bootstrapped.workspace_id,
        source_id=source_id,
        event_id=event_id,
        idempotency_key=IdempotencyKey(f"phase-one-acceptance-{uuid.uuid4().hex}"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Phase One Acceptance"),
        actor=SourceActor(ActorKind.DEVICE, bootstrapped.device_id),
        expected_object=ExpectedObject(
            content_digest=ContentDigest.parse(hashlib.sha256(payload).hexdigest()),
            size_bytes=len(payload),
            media_type=CanonicalMediaType.parse(_ACCEPTANCE_MEDIA_TYPE),
        ),
        client_timestamp=collaborators.clock(),
    )

    # 4. Publication: preflight miss, then stream/store/full-verify through the
    #    object store and one atomic PostgreSQL commit.
    published = await collaborators.publication_service.publish_create(
        command=create_command,
        stream=_single_chunk_stream(payload),
        diagnostic_context=diagnostic_context,
    )
    if published.outcome is not PublicationOutcome.PUBLISHED:
        raise _acceptance_integrity_failure("source_publication")
    counts_after_publication = await collaborators.table_counts()

    # 5. Canonical current-source read returns exactly the published bytes.
    read_command = ReadCurrentSourceCommand(
        workspace_id=bootstrapped.workspace_id, source_id=source_id
    )
    content = await collaborators.read_service.read_current_source_bytes(
        read_command, diagnostic_context
    )
    if content != payload:
        raise _acceptance_integrity_failure("canonical_read")

    # 6. Exact publication replay: the original version, sequence, outcome and
    #    committed time return with no object-store call (proven by the closed
    #    service contract: the committed preflight hit returns before any store
    #    interaction) and no new row.
    replayed_publication = await collaborators.publication_service.publish_create(
        command=create_command,
        stream=_single_chunk_stream(payload),
        diagnostic_context=diagnostic_context,
    )
    if replayed_publication != published:
        raise _acceptance_integrity_failure("source_publication_replay")
    if await collaborators.table_counts() != counts_after_publication:
        raise _acceptance_integrity_failure("source_publication_replay_row_count")

    # 7. Claim both projection intents through fenced transitions; both derive
    #    the identical workflow id and the closed four-UUID input, so the two
    #    starts converge on ONE deterministic Temporal execution.
    claimed = await collaborators.intent_store.claim_batch(
        collaborators.clock(), PROJECTION_CLAIM_BATCH_LIMIT
    )
    claimed_intents = sorted(claimed, key=lambda intent: intent.projection_kind.value)
    if len(claimed_intents) != 2 or [
        intent.projection_kind.value for intent in claimed_intents
    ] != ["neo4j", "qdrant"]:
        raise _acceptance_integrity_failure("projection_intent_claim")
    workflow_ids: list[str] = []
    start_outcomes: list[ProjectionWorkflowStartResult] = []
    for intent in claimed_intents:
        reference = source_ingestion_reference_for_intent(intent)
        if (
            reference.contract != SOURCE_INGESTION_REFERENCE_CONTRACT
            or reference.workspace_id != bootstrapped.workspace_id
            or reference.event_id != event_id
            or reference.source_id != source_id
            or reference.source_version_id != published.source_version_id
        ):
            raise _acceptance_integrity_failure("projection_intent_reference")
        workflow_ids.append(projection_workflow_id(reference.workspace_id, reference.event_id))
        start_outcomes.append(
            await collaborators.workflow_starter.start_source_ingestion(reference)
        )
    if len(set(workflow_ids)) != 1:
        raise _acceptance_integrity_failure("projection_workflow_identity")
    if start_outcomes != [
        ProjectionWorkflowStartResult.STARTED,
        ProjectionWorkflowStartResult.EXISTING,
    ]:
        raise _acceptance_integrity_failure("projection_workflow_start")
    for intent in claimed_intents:
        acknowledged = await collaborators.intent_store.acknowledge_dispatched(
            intent.projection_intent_id, intent.lease_token, collaborators.clock()
        )
        if not acknowledged:
            raise _acceptance_integrity_failure("projection_intent_acknowledge")

    # 8. Final canonical state: the fenced acknowledgements changed intent
    #    status, never a row count.
    final_counts = await collaborators.table_counts()
    if final_counts != counts_after_publication:
        raise _acceptance_integrity_failure("canonical_state")

    summary: dict[str, object] = {
        "result_code": "canonical_acceptance_completed",
        "user_id": _uuid_text(bootstrapped.user_id),
        "workspace_id": _uuid_text(bootstrapped.workspace_id),
        "device_id": _uuid_text(bootstrapped.device_id),
        "source_id": _uuid_text(source_id),
        "source_version_id": _uuid_text(published.source_version_id),
        "event_id": _uuid_text(event_id),
        "content_version": published.content_version,
        "event_sequence": published.event_sequence,
        "size_bytes": len(payload),
        "projection_intent_count": len(claimed_intents),
        "workflow_id": workflow_ids[0],
        "table_counts": dict(final_counts),
    }
    completed_fields: dict[str, object] = {
        "workspace_id": bootstrapped.workspace_id,
        "source_version_id": published.source_version_id,
        "event_id": event_id,
        "intent_count": len(claimed_intents),
    }
    return summary, completed_fields


async def run_phase_one_acceptance(
    collaborators: PhaseOneAcceptanceCollaborators,
    *,
    identity_command: BootstrapIdentityCommand | None = None,
) -> Mapping[str, object]:
    """Run the full design-spec-7 acceptance flow and return the safe summary.

    Every claim is proven against the collaborators, never trusted: an exact
    bootstrap replay, a preflight-miss publication with stream/store/full-verify
    and atomic commit, an exact canonical read, an exact publication replay
    with no object-store call and no new row, the two fenced intent claims
    converging on one deterministic Temporal execution (first ``STARTED``,
    second ``EXISTING``), and unchanged final row counts. Exactly one
    registered completion or failure event is emitted through the diagnostics
    sink, one closed metric outcome is recorded, and the summary carries IDs
    and safe counts only.
    """

    from personal_os.recovery.contracts import AcceptanceMetricOutcome

    command = (
        identity_command if identity_command is not None else build_synthetic_bootstrap_command()
    )
    resolution = create_diagnostic_context()
    started_monotonic = time.monotonic()
    try:
        summary, completed_fields = await _run_acceptance_flow(
            collaborators, command, resolution.context
        )
    except ApplicationError as error:
        collaborators.metrics.record_acceptance(outcome=AcceptanceMetricOutcome.FAILED)
        collaborators.diagnostics.emit(
            EventName.CANONICAL_ACCEPTANCE_FAILED,
            _acceptance_failure_fields(
                error.error_code.value,
                error.category.value,
                error.is_retryable,
                _elapsed_ms_since(started_monotonic),
            ),
        )
        raise
    except Exception:
        collaborators.metrics.record_acceptance(outcome=AcceptanceMetricOutcome.FAILED)
        collaborators.diagnostics.emit(
            EventName.CANONICAL_ACCEPTANCE_FAILED,
            _acceptance_failure_fields(
                ErrorCode.INTERNAL_ERROR.value,
                ErrorCategory.INTERNAL.value,
                False,
                _elapsed_ms_since(started_monotonic),
            ),
        )
        raise
    collaborators.metrics.record_acceptance(outcome=AcceptanceMetricOutcome.SUCCEEDED)
    completed_fields["outcome"] = AcceptanceMetricOutcome.SUCCEEDED
    completed_fields["duration_ms"] = _elapsed_ms_since(started_monotonic)
    collaborators.diagnostics.emit(EventName.CANONICAL_ACCEPTANCE_COMPLETED, completed_fields)
    return summary


async def _connect_temporal_client(environ: Mapping[str, str]) -> TemporalClient:
    """Connect the bounded Temporal client or fail closed as unavailable.

    Acceptance requires the real dispatch: an unreachable Temporal target is
    the retryable dispatch-unavailable failure with the provider cause chained
    internally, never a silent skip. The connect carries the same caller-side
    bound the worker dispatcher applies (``Client.connect`` exposes no timeout
    keyword, so the bound is applied with ``asyncio.wait_for``).
    """

    from temporalio.client import Client
    from workflow_worker.projection_dispatch_runtime import load_temporal_dispatch_settings

    settings = load_temporal_dispatch_settings(environ=environ)
    try:
        return await asyncio.wait_for(
            Client.connect(settings.target, namespace=settings.namespace),
            timeout=_TEMPORAL_CONNECT_TIMEOUT_SECONDS,
        )
    except ApplicationError:
        raise
    except Exception as cause:
        raise ProjectionDispatchError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE) from cause


def _compose_phase_one_acceptance(
    invocation: PhaseOneAcceptanceInvocation, environ: Mapping[str, str]
) -> CommandComposition:
    """Engine + R2 + Temporal -> every production service of the spec-7 flow."""

    del invocation
    from workflow_worker.projection_workflow_starter import TemporalProjectionWorkflowStarter

    from personal_os.diagnostics.logging import DiagnosticLogger
    from personal_os.exclusion_policy.metrics import InMemoryExclusionPolicyMetrics
    from personal_os.identity.bootstrap import IdentityBootstrapService
    from personal_os.identity.contracts import InMemoryIdentityBootstrapMetrics
    from personal_os.recovery.contracts import InMemoryCanonicalAcceptanceMetrics
    from personal_os.sources.metrics import (
        InMemoryCanonicalReadMetrics,
        InMemorySourcePublicationMetrics,
    )
    from personal_os.sources.publication import SourceVersionPublicationService
    from personal_os.sources.reading import CanonicalSourceReadService
    from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
    from postgresql_source_store.engine import dispose_source_store_engine
    from postgresql_source_store.identity_bootstrap import PostgresqlIdentityBootstrapStore
    from postgresql_source_store.policy_enforcement import compose_policy_enforcement
    from postgresql_source_store.projection_intents import PostgresqlProjectionIntentStore
    from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore

    recovery_settings = _load_canonical_recovery_settings(environ)
    environment = _recovery_environment(recovery_settings.environment.value, "phase_one_acceptance")
    database_settings, password = _load_database_parts(environ)
    engine = _create_database_engine(database_settings, password)
    # The Task 3 deferred sink wiring: the CLI's validating diagnostic logger
    # is bound into the identity store so a drift rejection without a trusted
    # workspace still emits the registered identity_bootstrap_rejected event.
    diagnostic_logger = DiagnosticLogger(
        {"service": _OPERATIONS_SERVICE.value, "environment": environment.value}
    )

    async def read_table_counts() -> Mapping[str, int]:
        import sqlalchemy as sa

        from postgresql_source_store.tables import SOURCE_STORE_TABLES

        counts: dict[str, int] = {}
        async with engine.connect() as connection:
            for table_name, table in SOURCE_STORE_TABLES.items():
                result = await connection.execute(sa.select(sa.func.count()).select_from(table))
                counts[table_name] = int(result.scalar_one())
        return counts

    policy_verifier = _policy_enforcement_verifier()
    policy_metrics = InMemoryExclusionPolicyMetrics()

    class CliSignedEmptyPolicySeeder:
        # Seeds the workspace's revision-1 signed empty policy on the engine.
        async def seed(
            self, workspace_id: UUID, owner_user_id: UUID, diagnostic_context: DiagnosticContext
        ) -> None:
            del diagnostic_context  # The seeding helper runs on the engine.
            await _seed_signed_empty_policy(
                engine, workspace_id=workspace_id, owner_user_id=owner_user_id
            )

    async def run() -> Mapping[str, object]:
        resolution = create_diagnostic_context()
        manager: R2ClientManager | None = None
        object_store: R2S3ObjectStore | None = None
        temporal_client: TemporalClient | None = None
        try:
            with bind_diagnostic_context(resolution.context):
                manager, object_store = await _open_r2_object_store(environ)
                temporal_client = await _connect_temporal_client(environ)
                collaborators = PhaseOneAcceptanceCollaborators(
                    identity_service=IdentityBootstrapService(
                        store=PostgresqlIdentityBootstrapStore(
                            engine, diagnostics=diagnostic_logger
                        ),
                        metrics=InMemoryIdentityBootstrapMetrics(),
                        diagnostics=diagnostic_logger,
                    ),
                    publication_service=SourceVersionPublicationService(
                        store=PostgresqlSourcePublicationStore(
                            engine,
                            policy_verifier=policy_verifier,
                            policy_metrics=policy_metrics,
                        ),
                        object_store=object_store,
                        metrics=InMemorySourcePublicationMetrics(),
                        clock=_recovery_clock,
                        policy_guard=compose_policy_enforcement(
                            engine,
                            verifier=policy_verifier,
                            metrics=policy_metrics,
                        ),
                    ),
                    read_service=CanonicalSourceReadService(
                        store=PostgresqlCanonicalSourceReadStore(
                            engine,
                            policy_verifier=policy_verifier,
                            policy_metrics=policy_metrics,
                        ),
                        object_store=object_store,
                        metrics=InMemoryCanonicalReadMetrics(),
                        policy_guard=compose_policy_enforcement(
                            engine,
                            verifier=policy_verifier,
                            metrics=policy_metrics,
                        ),
                        diagnostics=diagnostic_logger,
                    ),
                    policy_seeder=CliSignedEmptyPolicySeeder(),
                    intent_store=PostgresqlProjectionIntentStore(engine),
                    workflow_starter=TemporalProjectionWorkflowStarter(temporal_client),
                    table_counts=read_table_counts,
                    diagnostics=diagnostic_logger,
                    metrics=InMemoryCanonicalAcceptanceMetrics(),
                    clock=_recovery_clock,
                )
                return await run_phase_one_acceptance(collaborators)
        finally:
            # The Temporal client exposes no close in the pinned SDK (the
            # worker dispatcher relies on process exit the same way); the
            # database engine and the R2 client are the resources that must
            # never leak across repeated CLI invocations in one process.
            await _close_r2_object_store(manager, object_store)
            await dispose_source_store_engine(engine)

    return _ClosureComposition(run)


# --- Entry point --------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    compose_bootstrap_identity: BootstrapIdentityComposer = _compose_bootstrap_identity,
    compose_read_current_source: ReadCurrentSourceComposer = _compose_read_current_source,
    compose_backup_create: BackupCreateComposer = _compose_backup_create,
    compose_backup_verify: BackupVerifyComposer = _compose_backup_verify,
    compose_restore_empty: RestoreEmptyComposer = _compose_restore_empty,
    compose_phase_one_acceptance: PhaseOneAcceptanceComposer = _compose_phase_one_acceptance,
    command_timeout_seconds: float = RECOVERY_COMMAND_TIMEOUT_SECONDS,
) -> int:
    """Run exactly one canonical core operation and return its exit code."""
    out_stream = sys.stdout if stdout is None else stdout
    err_stream = sys.stderr if stderr is None else stderr
    _install_null_log_guard()

    parser = _build_cli_parser()
    try:
        arguments = parser.parse_args(argv)
        if cast(bool, getattr(arguments, "should_show_version", False)):
            _print_version(out_stream)
            return int(CanonicalCoreExitCode.OK)
        if arguments.command is None:
            raise _CliSyntaxFailure("invalid_cli")
    except _HelpRequested:
        return int(CanonicalCoreExitCode.OK)
    except (argparse.ArgumentError, _CliSyntaxFailure) as failure:
        result_code = (
            failure.result_code if isinstance(failure, _CliSyntaxFailure) else "invalid_cli"
        )
        _print_json(out_stream, {"result_code": result_code, "state": "error"})
        return int(CanonicalCoreExitCode.CLI)

    resolution = create_diagnostic_context()
    try:
        payload = asyncio.Runner(
            # psycopg async refuses the Windows Proactor loop that a bare
            # ``asyncio.run`` selects on win32; the database-touching
            # operations below require a selector-backed loop on every host.
            loop_factory=asyncio.SelectorEventLoop,
        ).run(
            _dispatch(
                arguments,
                environ=os.environ if environ is None else environ,
                compose_bootstrap_identity=compose_bootstrap_identity,
                compose_read_current_source=compose_read_current_source,
                compose_backup_create=compose_backup_create,
                compose_backup_verify=compose_backup_verify,
                compose_restore_empty=compose_restore_empty,
                compose_phase_one_acceptance=compose_phase_one_acceptance,
                command_timeout_seconds=command_timeout_seconds,
            )
        )
    except ApplicationError as error:
        _print_json(out_stream, {"result_code": error.error_code.value, "state": "error"})
        emit_emergency_application_error(
            _OPERATIONS_SERVICE, resolution.context, error, stderr=err_stream
        )
        return int(exit_code_for_application_error(error))
    except TimeoutError:
        _print_json(out_stream, {"result_code": "recovery_command_timeout", "state": "error"})
        emit_emergency_internal_error(
            _OPERATIONS_SERVICE, resolution.context, _RecoveryCommandTimeout(), stderr=err_stream
        )
        return int(CanonicalCoreExitCode.BUSY)
    except _CliSyntaxFailure as failure:
        _print_json(out_stream, {"result_code": failure.result_code, "state": "error"})
        return int(CanonicalCoreExitCode.CLI)
    except Exception as error:
        _print_json(
            out_stream, {"result_code": "canonical_operations_internal_error", "state": "error"}
        )
        emit_emergency_internal_error(
            _OPERATIONS_SERVICE, resolution.context, error, stderr=err_stream
        )
        return int(CanonicalCoreExitCode.INTERNAL)
    _print_json(out_stream, payload)
    return int(CanonicalCoreExitCode.OK)


async def _dispatch(
    arguments: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    compose_bootstrap_identity: BootstrapIdentityComposer,
    compose_read_current_source: ReadCurrentSourceComposer,
    compose_backup_create: BackupCreateComposer,
    compose_backup_verify: BackupVerifyComposer,
    compose_restore_empty: RestoreEmptyComposer,
    compose_phase_one_acceptance: PhaseOneAcceptanceComposer,
    command_timeout_seconds: float,
) -> Mapping[str, object]:
    command = cast(str, arguments.command)

    if command == "phase-one-acceptance":
        _require_local_or_test_environment(environ, "phase_one_acceptance")
        composition = compose_phase_one_acceptance(PhaseOneAcceptanceInvocation(), environ)
        return await asyncio.wait_for(composition.run(), timeout=command_timeout_seconds)

    if command == "backup-create":
        _require_local_or_test_environment(environ, "backup_create")
        _require_write_admission(cast(bool, arguments.confirm_write_admission_disabled))
        composition = compose_backup_create(BackupCreateInvocation(), environ)
        return await asyncio.wait_for(composition.run(), timeout=command_timeout_seconds)

    if command == "restore-empty":
        _require_local_or_test_environment(environ, "restore_empty")
        target_database = cast(str, arguments.target_database)
        if cast(str, arguments.confirm_target_database) != target_database:
            raise _environment_refused("restore_empty")
        composition = compose_restore_empty(
            RestoreEmptyInvocation(
                bundle_id=cast(UUID, arguments.bundle_id),
                target_database=target_database,
            ),
            environ,
        )
        return await asyncio.wait_for(composition.run(), timeout=command_timeout_seconds)

    if command == "backup-verify":
        composition = compose_backup_verify(
            BackupVerifyInvocation(bundle_id=cast(UUID, arguments.bundle_id)), environ
        )
        return await asyncio.wait_for(composition.run(), timeout=command_timeout_seconds)

    if command == "read-current-source":
        read_invocation = ReadCurrentSourceInvocation(
            workspace_id=cast(UUID, arguments.workspace_id),
            source_id=cast(UUID, arguments.source_id),
            output_file=cast(Path, arguments.output_file),
        )
        composition = compose_read_current_source(read_invocation, environ)
        return await _read_current_source_with_exclusive_output(
            composition, read_invocation.output_file, command_timeout_seconds
        )

    if command == "bootstrap-identity":
        bootstrap_invocation = BootstrapIdentityInvocation(
            username=cast(str, arguments.username),
            user_display_name=cast(str, arguments.user_display_name),
            workspace_key=cast(str, arguments.workspace_key),
            workspace_display_name=cast(str, arguments.workspace_display_name),
            device_name=cast(str, arguments.device_name),
            device_kind=cast(str, arguments.device_kind),
        )
        composition = compose_bootstrap_identity(bootstrap_invocation, environ)
        return await asyncio.wait_for(composition.run(), timeout=command_timeout_seconds)

    raise _CliSyntaxFailure("invalid_cli")


if __name__ == "__main__":
    raise SystemExit(main())
