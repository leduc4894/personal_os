"""Provider-neutral bootstrap service orchestration and replay/drift classification.

Pins spec 5.4 as pure classification tests: an exact replay returns the
originally committed ids and the stored workspace creation timestamp, every
drift shape fails closed as ``identity_bootstrap_state_conflict`` without
repair, and additional valid devices never invalidate a replay. The service
tests pin the store delegation, the closed metric label, and the registered
completion-event payload built only from ids and the outcome enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

import pytest

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.events import (
    DiagnosticEvent,
    EventName,
    RejectedDiagnosticPayload,
    build_registered_event,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.identity.bootstrap import (
    ExistingIdentityDevice,
    ExistingIdentityState,
    ExistingIdentityUser,
    ExistingIdentityWorkspace,
    IdentityBootstrapService,
    bootstrap_completion_event,
    classify_existing_identity,
    resolve_trusted_workspace_id,
)
from personal_os.identity.contracts import (
    BootstrapIdentityCommand,
    BootstrapIdentityOutcome,
    BootstrapIdentityResult,
    IdentityBootstrapError,
    InMemoryIdentityBootstrapMetrics,
    validate_bootstrap_identity_command,
)

CLOCK_NOW: Final[datetime] = datetime(2026, 8, 15, 9, 0, 0, tzinfo=UTC)
USER_ID: Final[UUID] = uuid4()
WORKSPACE_ID: Final[UUID] = uuid4()
DEVICE_ID: Final[UUID] = uuid4()
OTHER_USER_ID: Final[UUID] = uuid4()
OTHER_WORKSPACE_ID: Final[UUID] = uuid4()
OTHER_SOURCE_ID: Final[UUID] = uuid4()


def build_command() -> BootstrapIdentityCommand:
    """The exact-trimmed baseline command every replay state was built from."""

    return validate_bootstrap_identity_command(
        username="duc",
        user_display_name="Duc",
        workspace_key="main",
        workspace_display_name="Main knowledge",
        device_name="Desktop Obsidian",
        device_kind="obsidian",
    )


def build_device(
    *,
    device_id: UUID = DEVICE_ID,
    workspace_id: UUID = WORKSPACE_ID,
    user_id: UUID = USER_ID,
    device_name: str = "Desktop Obsidian",
    device_kind: str = "obsidian",
    status: str = "active",
    revoked_at: datetime | None = None,
) -> ExistingIdentityDevice:
    """One canonical device row view; defaults mirror the baseline command."""

    return ExistingIdentityDevice(
        device_id=device_id,
        workspace_id=workspace_id,
        user_id=user_id,
        device_name=device_name,
        device_kind=device_kind,
        status=status,
        revoked_at=revoked_at,
    )


def build_state(
    *,
    users: tuple[ExistingIdentityUser, ...] | None = None,
    workspaces: tuple[ExistingIdentityWorkspace, ...] | None = None,
    devices: tuple[ExistingIdentityDevice, ...] | None = None,
) -> ExistingIdentityState:
    """One active user/workspace/device snapshot exactly replaying the command."""

    command = build_command()
    return ExistingIdentityState(
        users=users
        if users is not None
        else (
            ExistingIdentityUser(
                user_id=USER_ID,
                username=command.username,
                display_name=command.user_display_name,
                status="active",
            ),
        ),
        workspaces=workspaces
        if workspaces is not None
        else (
            ExistingIdentityWorkspace(
                workspace_id=WORKSPACE_ID,
                owner_user_id=USER_ID,
                workspace_key=command.workspace_key,
                display_name=command.workspace_display_name,
                status="active",
                created_at=CLOCK_NOW,
            ),
        ),
        devices=devices if devices is not None else (build_device(),),
    )


def build_result(outcome: BootstrapIdentityOutcome) -> BootstrapIdentityResult:
    """A scripted store result carrying the baseline identity ids."""

    return BootstrapIdentityResult(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        device_id=DEVICE_ID,
        outcome=outcome,
        committed_at=CLOCK_NOW,
    )


def build_diagnostic_context() -> DiagnosticContext:
    """A fresh server-owned diagnostic context for one request-bound unit of work."""

    return create_diagnostic_context().context


@dataclass
class FakeIdentityBootstrapStore:
    """Store fake recording each bootstrap call and returning one scripted result."""

    result: BootstrapIdentityResult
    commands: list[BootstrapIdentityCommand] = field(default_factory=list)
    contexts: list[DiagnosticContext] = field(default_factory=list)

    async def bootstrap(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult:
        self.commands.append(command)
        self.contexts.append(diagnostic_context)
        return self.result


def test_exact_replay_returns_original_ids_and_timestamp() -> None:
    state = build_state()  # one active user/workspace/device matching the command
    result = classify_existing_identity(state, build_command())
    assert result.outcome is BootstrapIdentityOutcome.EXISTING
    assert result.user_id == state.users[0].user_id
    assert result.workspace_id == state.workspaces[0].workspace_id
    assert result.device_id == state.devices[0].device_id
    assert result.committed_at == state.workspaces[0].created_at


@pytest.mark.parametrize(
    "mutator",
    [
        pytest.param(lambda s: replace(s, users=()), id="zero-users"),
        pytest.param(lambda s: replace(s, users=s.users * 2), id="two-users"),
        pytest.param(lambda s: replace(s, workspaces=()), id="zero-workspaces"),
        pytest.param(lambda s: replace(s, workspaces=s.workspaces * 2), id="two-workspaces"),
        pytest.param(lambda s: replace(s, devices=()), id="zero-matching-devices"),
        pytest.param(lambda s: replace(s, devices=s.devices * 2), id="two-matching-devices"),
        pytest.param(
            lambda s: replace(s, devices=(replace(s.devices[0], revoked_at=CLOCK_NOW),)),
            id="revoked-bootstrap-device",
        ),
        pytest.param(
            lambda s: replace(s, devices=(replace(s.devices[0], status="disabled"),)),
            id="disabled-bootstrap-device",
        ),
        pytest.param(
            lambda s: replace(s, devices=(replace(s.devices[0], user_id=OTHER_USER_ID),)),
            id="device-owned-by-other-user",
        ),
        pytest.param(
            lambda s: replace(s, users=(replace(s.users[0], username="other"),)),
            id="username-drift",
        ),
        pytest.param(
            lambda s: replace(s, users=(replace(s.users[0], display_name="Other"),)),
            id="user-display-name-drift",
        ),
        pytest.param(
            lambda s: replace(s, workspaces=(replace(s.workspaces[0], workspace_key="other"),)),
            id="workspace-key-drift",
        ),
        pytest.param(
            lambda s: replace(s, workspaces=(replace(s.workspaces[0], status="archived"),)),
            id="workspace-archived",
        ),
        pytest.param(
            lambda s: replace(s, users=(replace(s.users[0], status="disabled"),)),
            id="user-disabled",
        ),
    ],
)
def test_drift_fails_closed_without_repair(mutator) -> None:
    with pytest.raises(IdentityBootstrapError) as raised:
        classify_existing_identity(mutator(build_state()), build_command())
    assert raised.value.error_code is ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT


def test_additional_valid_devices_do_not_invalidate_replay() -> None:
    state = build_state(
        devices=(build_device(), build_device(device_name="Phone", device_kind="web"))
    )
    assert (
        classify_existing_identity(state, build_command()).outcome
        is BootstrapIdentityOutcome.EXISTING
    )


@pytest.mark.parametrize(
    "mutator",
    [
        pytest.param(
            lambda s: replace(s, devices=(replace(s.devices[0], device_name="Phone"),)),
            id="bootstrap-device-renamed",
        ),
        pytest.param(
            lambda s: replace(s, devices=(replace(s.devices[0], device_kind="web"),)),
            id="bootstrap-device-kind-changed",
        ),
        pytest.param(
            lambda s: replace(s, devices=(replace(s.devices[0], workspace_id=OTHER_WORKSPACE_ID),)),
            id="bootstrap-device-in-other-workspace",
        ),
    ],
)
def test_changed_bootstrap_device_identity_fails_closed(mutator) -> None:
    with pytest.raises(IdentityBootstrapError) as raised:
        classify_existing_identity(mutator(build_state()), build_command())
    assert raised.value.error_code is ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(build_state(), WORKSPACE_ID, id="single-active-matching-workspace"),
        pytest.param(build_state(workspaces=()), None, id="zero-workspaces"),
        pytest.param(
            build_state(
                workspaces=(
                    ExistingIdentityWorkspace(
                        workspace_id=WORKSPACE_ID,
                        owner_user_id=USER_ID,
                        workspace_key="main",
                        display_name="Main knowledge",
                        status="active",
                        created_at=CLOCK_NOW,
                    ),
                    ExistingIdentityWorkspace(
                        workspace_id=OTHER_WORKSPACE_ID,
                        owner_user_id=USER_ID,
                        workspace_key="main",
                        display_name="Other",
                        status="active",
                        created_at=CLOCK_NOW,
                    ),
                )
            ),
            None,
            id="two-active-matching-workspaces",
        ),
        pytest.param(
            build_state(
                workspaces=(
                    ExistingIdentityWorkspace(
                        workspace_id=WORKSPACE_ID,
                        owner_user_id=USER_ID,
                        workspace_key="main",
                        display_name="Main knowledge",
                        status="archived",
                        created_at=CLOCK_NOW,
                    ),
                )
            ),
            None,
            id="matching-workspace-archived",
        ),
        pytest.param(
            build_state(
                workspaces=(
                    ExistingIdentityWorkspace(
                        workspace_id=WORKSPACE_ID,
                        owner_user_id=USER_ID,
                        workspace_key="other",
                        display_name="Main knowledge",
                        status="active",
                        created_at=CLOCK_NOW,
                    ),
                )
            ),
            None,
            id="workspace-key-mismatch",
        ),
    ],
)
def test_resolve_trusted_workspace_id_requires_single_active_matching_workspace(
    state: ExistingIdentityState, expected: UUID | None
) -> None:
    assert resolve_trusted_workspace_id(state, build_command()) == expected


@pytest.mark.asyncio
async def test_service_emits_succeeded_event_and_metric_for_created_outcome() -> None:
    result = build_result(BootstrapIdentityOutcome.CREATED)
    store = FakeIdentityBootstrapStore(result=result)
    metrics = InMemoryIdentityBootstrapMetrics()
    service = IdentityBootstrapService(store=store, metrics=metrics)

    await service.bootstrap(build_command(), build_diagnostic_context())

    event_name, event_fields = bootstrap_completion_event(result)
    assert event_name is EventName.IDENTITY_BOOTSTRAP_SUCCEEDED
    built = build_registered_event(event_name, event_fields)
    assert isinstance(built, DiagnosticEvent)
    assert set(built.fields) == {"outcome", "user_id", "workspace_id", "device_id"}
    assert built.fields["outcome"] is BootstrapIdentityOutcome.CREATED
    assert metrics.bootstrap_count(BootstrapIdentityOutcome.CREATED) == 1


@pytest.mark.asyncio
async def test_service_emits_replayed_event_for_existing_outcome() -> None:
    result = build_result(BootstrapIdentityOutcome.EXISTING)
    store = FakeIdentityBootstrapStore(result=result)
    metrics = InMemoryIdentityBootstrapMetrics()
    service = IdentityBootstrapService(store=store, metrics=metrics)

    await service.bootstrap(build_command(), build_diagnostic_context())

    event_name, event_fields = bootstrap_completion_event(result)
    assert event_name is EventName.IDENTITY_BOOTSTRAP_REPLAYED
    built = build_registered_event(event_name, event_fields)
    assert isinstance(built, DiagnosticEvent)
    assert set(built.fields) == {"user_id", "workspace_id", "device_id"}
    assert metrics.bootstrap_count(BootstrapIdentityOutcome.EXISTING) == 1


def test_completion_event_fields_carry_only_ids_and_the_outcome() -> None:
    for outcome in (BootstrapIdentityOutcome.CREATED, BootstrapIdentityOutcome.EXISTING):
        event_name, event_fields = bootstrap_completion_event(build_result(outcome))
        for value in event_fields.values():
            assert isinstance(value, UUID | BootstrapIdentityOutcome)
        built = build_registered_event(event_name, event_fields)
        assert not isinstance(built, RejectedDiagnosticPayload)


@pytest.mark.asyncio
async def test_service_returns_store_result_unchanged() -> None:
    result = build_result(BootstrapIdentityOutcome.CREATED)
    store = FakeIdentityBootstrapStore(result=result)
    service = IdentityBootstrapService(store=store, metrics=InMemoryIdentityBootstrapMetrics())
    command = build_command()
    diagnostic_context = build_diagnostic_context()

    returned = await service.bootstrap(command, diagnostic_context)

    assert returned is result
    assert store.commands == [command]
    assert store.contexts == [diagnostic_context]
