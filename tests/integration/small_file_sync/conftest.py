"""In-process cross-boundary fixtures for the small-file sync wire surface.

The harnesses compose the real application factory over deterministic
offline doubles only — no database, no key file, no object store, no
environment read and no network socket — so every scenario runs against
disposable, guarded test infrastructure and never a personal stack. Two
graphs are offered. The offline graph binds the tested offline small-file
runtime exactly as the route suites do. The real-policy graph upgrades the
policy seam to the production shape of the serve composition: the real
:class:`PolicyEnforcementService` behind the locator-aware
:class:`PolicyEnforcementSmallFileGuard` at preflight and as the publication
gateway's invocation-local guard at receive, driven by a mutable in-memory active-snapshot
source whose revisions carry real Ed25519 signatures over the canonical
snapshot payload. Device credentials are minted through the real
device-authorization routes, and revocations run through the real Admin
revoke route, so every scenario crosses the real HTTP route stack.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID, uuid4

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationClock,
    OfflineAuthenticationState,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from api_runtime.exclusion_policy_crypto import (
    Ed25519PolicySigner,
    TrustAnchorEd25519Verifier,
)
from api_runtime.small_file_sync_composition import (
    BoundPolicySmallFilePublicationGateway,
    OfflineCanonicalObjectStore,
    OfflineCurrentSourceStore,
    OfflineSmallFileClock,
    OfflineSmallFileSyncState,
    OfflineSmallFileUploadOperationStore,
    OfflineSourcePublicationStore,
    PolicyEnforcementSmallFileGuard,
    SmallFileSyncRuntime,
    compose_offline_small_file_sync,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.exclusion_policy.contracts import (
    ExclusionPolicyRevision,
    ExclusionRule,
    RuleKind,
)
from personal_os.exclusion_policy.enforcement import (
    ActivePolicySnapshotMaterial,
    AllowedPolicyRevisionBinding,
    PolicyEnforcementService,
    PublicationPolicyEvidence,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.signatures import (
    SNAPSHOT_SIGNING_DOMAIN,
    build_signed_message,
    build_snapshot_payload,
    compute_payload_sha256_hex,
)
from personal_os.object_storage import VerifiedObjectReceipt
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.small_file_sync.metrics import InMemorySmallFileSyncMetrics
from personal_os.small_file_sync.service import SmallFileSyncService
from personal_os.sources.fingerprint import RequestFingerprint, SourceVersionCommand
from personal_os.sources.metrics import InMemorySourcePublicationMetrics
from personal_os.sources.results import SourceVersionPublicationResult

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}

#: Fixed aware publication moment of every harness-signed policy revision.
_POLICY_PUBLISHED_AT: Final[datetime] = datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC)


class _ReadyProbe:
    """Readiness probe stub: the sync routes never consult dependencies."""

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ExchangeDevice:
    """One device credential minted through the real authorization routes."""

    device_name: str
    access_credential: str
    device_id: UUID


def exchange_device_credential(client: TestClient, *, device_name: str) -> ExchangeDevice:
    """Create, approve and exchange one ``obsidian_sync`` device grant."""

    created = client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json={
            "client_instance_id": str(uuid4()),
            "device_name": device_name,
            "platform_class": "obsidian_desktop",
            "platform_name": "windows",
            "plugin_version": "1.4.0",
            "requested_scope": "obsidian_sync",
        },
    )
    assert created.status_code == 200, created.text
    grant = dict(created.json()["data"])
    login = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert login.status_code == 200, login.text
    cookies = login.cookies
    approved = client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/approve",
        headers={
            "Origin": ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={cookies[SESSION_COOKIE_NAME]}; "
                f"{CSRF_COOKIE_NAME}={cookies[CSRF_COOKIE_NAME]}"
            ),
            "X-CSRF-Token": cookies[CSRF_COOKIE_NAME],
        },
    )
    assert approved.status_code == 200, approved.text
    exchanged = client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/poll",
        headers={"Authorization": f"Bearer {grant['polling_secret']}"},
    )
    assert exchanged.status_code == 200, exchanged.text
    data = dict(exchanged.json()["data"])
    return ExchangeDevice(
        device_name=device_name,
        access_credential=str(data["access_credential"]),
        device_id=UUID(str(data["device_id"])),
    )


def revoke_device_through_admin_route(client: TestClient, device: ExchangeDevice) -> None:
    """Revoke one device through the real Admin route behind a fresh login."""

    login = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert login.status_code == 200, login.text
    cookies = login.cookies
    revoked = client.post(
        f"/api/admin/devices/{device.device_id}/revoke",
        headers={
            "Origin": ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={cookies[SESSION_COOKIE_NAME]}; "
                f"{CSRF_COOKIE_NAME}={cookies[CSRF_COOKIE_NAME]}"
            ),
            "X-CSRF-Token": cookies[CSRF_COOKIE_NAME],
        },
        json={"device_name_confirmation": device.device_name},
    )
    assert revoked.status_code == 200, revoked.text


@dataclass(frozen=True, slots=True)
class SmallFileWireHarness:
    """One test client bound to the composed graph, its state and one device."""

    client: TestClient
    sync_state: OfflineSmallFileSyncState
    device: ExchangeDevice
    snapshot_source: MutableActivePolicySnapshotSource | None = None
    publication_store: PolicyAwareInMemorySourcePublicationStore | None = None

    def preflight(self, body: dict[str, object], *, credential: str | None = None) -> Response:
        return cast(
            Response,
            self.client.post(
                "/api/sync/journal-events/preflight",
                headers={"Authorization": f"Bearer {credential or self.device.access_credential}"},
                json=body,
            ),
        )

    def upload(self, token: str, content: bytes, *, credential: str | None = None) -> Response:
        return cast(
            Response,
            self.client.put(
                f"/api/uploads/{token}/content",
                headers={
                    "Authorization": f"Bearer {credential or self.device.access_credential}",
                    "Content-Type": "application/octet-stream",
                },
                content=content,
            ),
        )


def _build_application(small_file_sync: SmallFileSyncRuntime) -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(
            clock=OfflineAuthenticationClock(),
            state=OfflineAuthenticationState(totp_active=False),
        ),
        small_file_sync=small_file_sync,
    )


@pytest.fixture
def offline_harness() -> Iterator[SmallFileWireHarness]:
    """The offline small-file graph over the real application factory."""

    with offline_wire_harness() as harness:
        yield harness


@contextmanager
def offline_wire_harness() -> Iterator[SmallFileWireHarness]:
    """Compose the offline graph and mint one live device credential."""

    sync_state = OfflineSmallFileSyncState()
    application = _build_application(compose_offline_small_file_sync(state=sync_state))
    with TestClient(application, base_url=ORIGIN) as client:
        yield SmallFileWireHarness(
            client=client,
            sync_state=sync_state,
            device=exchange_device_credential(client, device_name="Journey desktop"),
        )


class MutableActivePolicySnapshotSource:
    """In-memory active-snapshot source publishing real signed revisions.

    Every workspace is answered with the currently published rules as one
    genuinely signed snapshot: the canonical payload bytes, their SHA-256 and
    a real Ed25519 signature over the domain-separated message, verified by
    the real trust-anchor adapter. Publishing new rules advances the revision
    number exactly like an Admin publication would.
    """

    def __init__(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        self._signer = Ed25519PolicySigner(private_key)
        self._public_key_bytes: bytes = private_key.public_key().public_bytes_raw()
        self._rules: tuple[ExclusionRule, ...] = ()
        self._revision_number = 1
        self._materials: dict[UUID, ActivePolicySnapshotMaterial] = {}
        self._is_dirty = True
        self._load_count = 0
        self._load_failures: dict[int, Exception] = {}
        self._corrupt_signature_loads: set[int] = set()
        self._load_barrier: asyncio.Barrier | None = None
        self._load_barrier_delays_seconds: tuple[float, ...] = ()
        self._load_barrier_arrivals = 0

    @property
    def revision_number(self) -> int:
        return self._revision_number

    def publish_rules(self, rules: tuple[ExclusionRule, ...]) -> None:
        self._rules = rules
        self._revision_number += 1
        self._is_dirty = True

    @property
    def load_count(self) -> int:
        return self._load_count

    def fail_after_loads(self, loads_until_failure: int, error: Exception) -> None:
        """Raise ``error`` on one selected future active-snapshot load."""

        if loads_until_failure < 1:
            raise ValueError("loads_until_failure must be positive")
        self._load_failures[self._load_count + loads_until_failure] = error

    def corrupt_signature_after_loads(self, loads_until_corruption: int) -> None:
        """Return invalid signed material on one selected future load."""

        if loads_until_corruption < 1:
            raise ValueError("loads_until_corruption must be positive")
        self._corrupt_signature_loads.add(self._load_count + loads_until_corruption)

    def synchronize_next_loads(self, *, delays_seconds: tuple[float, ...]) -> None:
        """Barrier the next loads, then apply deterministic release delays."""

        if len(delays_seconds) < 2:
            raise ValueError("at least two snapshot loads are required")
        self._load_barrier = asyncio.Barrier(len(delays_seconds))
        self._load_barrier_delays_seconds = delays_seconds
        self._load_barrier_arrivals = 0

    async def load_active_snapshot(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> ActivePolicySnapshotMaterial:
        del context
        self._load_count += 1
        load_number = self._load_count
        failure = self._load_failures.pop(load_number, None)
        if failure is not None:
            raise failure
        if self._is_dirty or workspace_id not in self._materials:
            revision = ExclusionPolicyRevision(
                policy_revision_id=uuid4(),
                workspace_id=workspace_id,
                revision_number=self._revision_number,
                rules=self._rules,
            )
            payload_bytes = build_snapshot_payload(
                revision,
                parent_policy_revision_id=None,
                published_at=_POLICY_PUBLISHED_AT,
            )
            signature = self._signer.sign(
                build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload_bytes)
            )
            self._materials[workspace_id] = ActivePolicySnapshotMaterial(
                workspace_id=workspace_id,
                policy_revision_id=revision.policy_revision_id,
                revision_number=revision.revision_number,
                payload_bytes=payload_bytes,
                payload_sha256=compute_payload_sha256_hex(payload_bytes),
                signature_bytes=signature,
                public_key_bytes=self._public_key_bytes,
            )
            self._is_dirty = False
        material = self._materials[workspace_id]
        if load_number in self._corrupt_signature_loads:
            self._corrupt_signature_loads.remove(load_number)
            material = replace(material, signature_bytes=b"x" * 64)
        barrier = self._load_barrier
        if barrier is not None:
            arrival = self._load_barrier_arrivals
            self._load_barrier_arrivals += 1
            if self._load_barrier_arrivals == barrier.parties:
                self._load_barrier = None
            await barrier.wait()
            await asyncio.sleep(self._load_barrier_delays_seconds[arrival])
        return material


class _NoEvidenceSource:
    """Subject-evidence double answering no stored evidence."""

    async def load_subject_evidence(
        self, workspace_id: UUID, source_id: UUID, context: DiagnosticContext
    ) -> None:
        del workspace_id, source_id, context
        return None


def excluding_folder_rule(folder_prefix: str) -> ExclusionRule:
    """One folder-prefix exclusion rule through the sanctioned constructor."""

    return normalize_rule(uuid4(), RuleKind.FOLDER_PREFIX, text_operand=folder_prefix)


def excluding_extension_rule(extension: str) -> ExclusionRule:
    """One extension exclusion rule through the canonical normalizer."""

    return normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=extension)


def maximum_size_rule(maximum_size_bytes: int) -> ExclusionRule:
    """One inclusive maximum-size exclusion rule (spec 6.2)."""

    return normalize_rule(uuid4(), RuleKind.MAXIMUM_SIZE, size_bytes_operand=maximum_size_bytes)


class PolicyAwareInMemorySourcePublicationStore(OfflineSourcePublicationStore):
    """In-memory transaction-final policy seam matching the durable store table.

    Matching bound evidence skips only locator-free evaluation after a fresh
    signed-snapshot verification. Changed bindings, ordinary decisions and
    absent evidence re-run the real publication guard, just as the PostgreSQL
    store does under its policy-state lock.
    """

    def __init__(
        self,
        state: OfflineSmallFileSyncState,
        clock: OfflineSmallFileClock,
        enforcement: PolicyEnforcementService,
    ) -> None:
        super().__init__(state, clock)
        self._enforcement = enforcement
        self.policy_evidence_by_event_id: dict[UUID, PublicationPolicyEvidence | None] = {}

    async def commit_create(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: PublicationPolicyEvidence | None = None,
    ) -> SourceVersionPublicationResult:
        await self._authorize_locked(command, preflight_decision, diagnostic_context)
        return await super().commit_create(
            command,
            request_fingerprint,
            receipt,
            diagnostic_context,
            preflight_decision=preflight_decision,
        )

    async def commit_update(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: PublicationPolicyEvidence | None = None,
    ) -> SourceVersionPublicationResult:
        await self._authorize_locked(command, preflight_decision, diagnostic_context)
        return await super().commit_update(
            command,
            request_fingerprint,
            receipt,
            diagnostic_context,
            preflight_decision=preflight_decision,
        )

    async def _authorize_locked(
        self,
        command: SourceVersionCommand,
        policy_evidence: PublicationPolicyEvidence | None,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        self.policy_evidence_by_event_id[command.event_id] = policy_evidence
        if isinstance(policy_evidence, AllowedPolicyRevisionBinding):
            await self._enforcement.authorize_bound_publication(
                command,
                policy_evidence,
                diagnostic_context,
            )
            return
        await self._enforcement.authorize_publication(command, diagnostic_context)


@pytest.fixture
def policy_harness() -> Iterator[SmallFileWireHarness]:
    """The serve-shaped graph: the real enforcement at both guarded seams."""

    with policy_wire_harness() as harness:
        yield harness


@contextmanager
def policy_wire_harness() -> Iterator[SmallFileWireHarness]:
    """Compose the serve-shaped policy seam and mint one live credential.

    Mirrors :func:`compose_small_file_sync` exactly at the policy seam — the
    real enforcement service stands behind the locator-aware small-file guard
    and behind the publication gateway's invocation-local guard — while every durable and
    remote adapter stays the offline double, so policy denials at preflight
    and at publication time are observed end-to-end over the real routes.
    """

    snapshot_source = MutableActivePolicySnapshotSource()
    enforcement = PolicyEnforcementService(
        snapshot_source=snapshot_source,
        evidence_source=_NoEvidenceSource(),
        verifier=TrustAnchorEd25519Verifier(),
    )
    sync_state = OfflineSmallFileSyncState()
    clock = OfflineSmallFileClock(sync_state)
    object_store = OfflineCanonicalObjectStore(sync_state, clock)
    publication_store = PolicyAwareInMemorySourcePublicationStore(
        sync_state,
        clock,
        enforcement,
    )
    publication_gateway = BoundPolicySmallFilePublicationGateway(
        store=publication_store,
        object_store=object_store,
        metrics=InMemorySourcePublicationMetrics(),
        clock=clock,
        enforcement=enforcement,
    )
    sync_metrics = InMemorySmallFileSyncMetrics()
    service = SmallFileSyncService(
        operation_store=OfflineSmallFileUploadOperationStore(sync_state, clock),
        policy_guard=PolicyEnforcementSmallFileGuard(enforcement=enforcement),
        publication_gateway=publication_gateway,
        object_store=object_store,
        current_sources=OfflineCurrentSourceStore(sync_state),
        metrics=sync_metrics,
        clock=clock,
    )
    application = _build_application(
        SmallFileSyncRuntime(service=service, rejection_diagnostics=sync_metrics)
    )
    with TestClient(
        application,
        base_url=ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        yield SmallFileWireHarness(
            client=client,
            sync_state=sync_state,
            device=exchange_device_credential(client, device_name="Policy desktop"),
            snapshot_source=snapshot_source,
            publication_store=publication_store,
        )


__all__ = [
    "ExchangeDevice",
    "SmallFileWireHarness",
    "exchange_device_credential",
    "excluding_extension_rule",
    "excluding_folder_rule",
    "maximum_size_rule",
    "offline_harness",
    "offline_wire_harness",
    "policy_harness",
    "policy_wire_harness",
    "revoke_device_through_admin_route",
]
