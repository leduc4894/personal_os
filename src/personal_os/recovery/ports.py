"""Provider-neutral recovery ports: dump process, snapshot and bundle stores.

These protocols are the composition seams consumed by the recovery service
(design spec 4.4). They carry no driver, SDK, subprocess or filesystem
implementation: adapters (filesystem bundle store, ``pg_dump``/``pg_restore``
process boundary, PostgreSQL snapshot store) implement them behind the
boundary. ``snapshot_token`` is infrastructure-private and never leaves the
composition call that owns it.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from personal_os.object_storage.contracts import ExpectedObject
from personal_os.recovery.contracts import RecoveryManifest


@dataclass(frozen=True, slots=True)
class PostgresqlConnectionTarget:
    """Connection coordinates for one canonical PostgreSQL database.

    Deliberately excludes any password, DSN, socket or TLS detail; credentials
    travel only through the adapter's private ephemeral password file.
    """

    host: str
    port: int
    database: str
    user: str


@dataclass(frozen=True, slots=True)
class DumpReceipt:
    """Observed size and digest of one completed dump."""

    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    """Completion timestamp of one bounded restore."""

    completed_at: datetime


class PostgresqlDumpProcess(Protocol):
    """Bounded pg_dump/pg_restore subprocess boundary (spec 9.3, 11.2)."""

    async def create_dump(
        self,
        snapshot_token: str,
        output_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> DumpReceipt: ...

    async def restore_dump(
        self,
        input_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> RestoreReceipt: ...


@dataclass(frozen=True, slots=True)
class CanonicalBackupSnapshot:
    """Quiesced exported-snapshot evidence (spec 9.2).

    ``snapshot_token`` is infrastructure-private and never leaves the
    composition call that owns it.
    """

    snapshot_token: str
    server_version: str
    schema_head: str
    table_counts: Mapping[str, int]
    referenced_objects: tuple[ExpectedObject, ...]


class CanonicalBackupSnapshotStore(Protocol):
    """Quiesced exported-snapshot seam with pending-writer observation."""

    def open_quiesced_snapshot(
        self, now: datetime
    ) -> AbstractAsyncContextManager[CanonicalBackupSnapshot]: ...

    async def observe_pending_writers(self) -> int: ...


class RecoveryBundleWriter(Protocol):
    """Staging writer for one bundle: dump sidecar, object sidecars, finalize."""

    dump_path: Path

    def object_path(self, content_sha256: str) -> Path: ...

    async def finalize(self, manifest: RecoveryManifest) -> None: ...

    async def abandon(self) -> None: ...


class VerifiedRecoveryBundle(Protocol):
    """An opened bundle whose manifest and sidecars already verified offline."""

    manifest: RecoveryManifest
    dump_path: Path

    def object_path(self, content_sha256: str) -> Path: ...


class RecoveryBundleStore(Protocol):
    """Immutable bundle storage seam: staging, offline verification and open."""

    def create_staging(
        self, bundle_id: UUID
    ) -> AbstractAsyncContextManager[RecoveryBundleWriter]: ...

    def open_verified(
        self, bundle_id: UUID
    ) -> AbstractAsyncContextManager[VerifiedRecoveryBundle]: ...

    def verify_offline(self, bundle_id: UUID) -> RecoveryManifest: ...

    def bundle_exists(self, bundle_id: UUID) -> bool: ...
