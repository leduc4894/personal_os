"""Unit tests for the canonical core operations CLI (parse-before-I/O contract)."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid7

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from tools.canonical_core_operations import (
    CanonicalCoreExitCode,
    main,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

_BUNDLE_ID = uuid7()
_LOCAL_ENVIRONMENT: Mapping[str, str] = {"KNOWLEDGE_ENVIRONMENT": "local"}
_STAGING_ENVIRONMENT: Mapping[str, str] = {"KNOWLEDGE_ENVIRONMENT": "staging"}

_BOOTSTRAP_ARGUMENTS = (
    "bootstrap-identity",
    "--username",
    "ops",
    "--user-display-name",
    "Ops User",
    "--workspace-key",
    "personal",
    "--workspace-display-name",
    "Personal Knowledge",
    "--device-name",
    "ops-laptop",
    "--device-kind",
    "system",
)


class _CountingEnviron(dict[str, str]):
    """Environment mapping that counts every value read."""

    def __init__(self, *args: object, **kwargs: str) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.read_count = 0

    def __getitem__(self, key: str) -> str:
        self.read_count += 1
        return super().__getitem__(key)

    def get(self, key: str, default: str | None = None) -> str | None:
        self.read_count += 1
        return super().get(key, default)


@dataclass
class _FakeComposition:
    """Injected stand-in for one composed subcommand."""

    payload: Mapping[str, object] = field(default_factory=dict)
    error: Exception | None = None
    delay_seconds: float = 0.0
    events: list[str] = field(default_factory=list)

    async def run(self) -> Mapping[str, object]:
        self.events.append("started")
        try:
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
            if self.error is not None:
                raise self.error
            return dict(self.payload)
        finally:
            self.events.append("cleaned-up")


class _CountingComposer:
    """Injected stand-in for one ``_compose_*`` factory, counting calls."""

    def __init__(self, composition: _FakeComposition) -> None:
        self.composition = composition
        self.call_count = 0
        self.seen_environments: list[Mapping[str, str]] = []

    def __call__(self, invocation: object, environ: Mapping[str, str]) -> _FakeComposition:
        self.call_count += 1
        self.seen_environments.append(environ)
        return self.composition


def _fake_composers(composition: _FakeComposition) -> dict[str, _CountingComposer]:
    return {
        "bootstrap_identity": _CountingComposer(composition),
        "read_current_source": _CountingComposer(composition),
        "backup_create": _CountingComposer(composition),
        "backup_verify": _CountingComposer(composition),
        "restore_empty": _CountingComposer(composition),
        "phase_one_acceptance": _CountingComposer(composition),
    }


def _run_main(
    argv: list[str],
    *,
    environ: Mapping[str, str] = _LOCAL_ENVIRONMENT,
    composers: dict[str, _CountingComposer] | None = None,
    command_timeout_seconds: float | None = None,
) -> tuple[int, list[dict[str, object]], list[dict[str, object]]]:
    composers = _fake_composers(_FakeComposition()) if composers is None else composers
    stdout = io.StringIO()
    stderr = io.StringIO()
    optional_timeout = (
        {}
        if command_timeout_seconds is None
        else {"command_timeout_seconds": command_timeout_seconds}
    )
    exit_code = main(
        argv,
        environ=environ,
        stdout=stdout,
        stderr=stderr,
        compose_bootstrap_identity=composers["bootstrap_identity"],
        compose_read_current_source=composers["read_current_source"],
        compose_backup_create=composers["backup_create"],
        compose_backup_verify=composers["backup_verify"],
        compose_restore_empty=composers["restore_empty"],
        compose_phase_one_acceptance=composers["phase_one_acceptance"],
        **optional_timeout,
    )
    stdout_documents = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    stderr_documents = [json.loads(line) for line in stderr.getvalue().splitlines() if line.strip()]
    return exit_code, stdout_documents, stderr_documents


# --- Parse-before-I/O -------------------------------------------------------


def test_invalid_syntax_exits_two_without_reading_environment() -> None:
    environment = _CountingEnviron({"KNOWLEDGE_ENVIRONMENT": "local"})
    composers = _fake_composers(_FakeComposition())

    exit_code, stdout_documents, _ = _run_main(
        ["definitely-not-a-subcommand"], environ=environment, composers=composers
    )

    assert exit_code == int(CanonicalCoreExitCode.CLI)
    assert environment.read_count == 0
    assert all(composer.call_count == 0 for composer in composers.values())
    assert len(stdout_documents) == 1
    assert stdout_documents[0]["state"] == "error"


def test_missing_required_flag_exits_two_without_reading_environment() -> None:
    environment = _CountingEnviron()
    exit_code, _, _ = _run_main(["backup-verify"], environ=environment)
    assert exit_code == int(CanonicalCoreExitCode.CLI)
    assert environment.read_count == 0


def test_malformed_uuid_exits_two_without_reading_environment() -> None:
    environment = _CountingEnviron()
    exit_code, _, _ = _run_main(
        [
            "read-current-source",
            "--workspace-id",
            "not-a-uuid",
            "--source-id",
            str(uuid7()),
            "--output-file",
            "out.bin",
        ],
        environ=environment,
    )
    assert exit_code == int(CanonicalCoreExitCode.CLI)
    assert environment.read_count == 0


def test_help_and_version_exit_zero_without_io() -> None:
    for arguments in (["--help"], ["--version"]):
        environment = _CountingEnviron({"KNOWLEDGE_ENVIRONMENT": "local"})
        composers = _fake_composers(_FakeComposition())
        exit_code = main(
            list(arguments),
            environ=environment,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            compose_bootstrap_identity=composers["bootstrap_identity"],
            compose_read_current_source=composers["read_current_source"],
            compose_backup_create=composers["backup_create"],
            compose_backup_verify=composers["backup_verify"],
            compose_restore_empty=composers["restore_empty"],
        )
        assert exit_code == int(CanonicalCoreExitCode.OK)
        assert environment.read_count == 0
        assert all(composer.call_count == 0 for composer in composers.values())


def test_version_prints_program_and_version_without_environment_read() -> None:
    environment = _CountingEnviron({"KNOWLEDGE_ENVIRONMENT": "local"})
    stdout = io.StringIO()

    exit_code = main(["--version"], environ=environment, stdout=stdout, stderr=io.StringIO())

    assert exit_code == int(CanonicalCoreExitCode.OK)
    assert stdout.getvalue().startswith("canonical_core_operations ")
    assert environment.read_count == 0


# --- Environment gates ------------------------------------------------------


def test_backup_create_refuses_staging_before_any_client_or_path() -> None:
    composers = _fake_composers(_FakeComposition())

    exit_code, stdout_documents, stderr_documents = _run_main(
        ["backup-create", "--confirm-write-admission-disabled"],
        environ=_STAGING_ENVIRONMENT,
        composers=composers,
    )

    assert exit_code == int(CanonicalCoreExitCode.CONFIG)
    assert all(composer.call_count == 0 for composer in composers.values())
    assert stdout_documents == [
        {"result_code": "canonical_recovery_environment_refused", "state": "error"}
    ]
    assert len(stderr_documents) == 1
    assert stderr_documents[0]["error_code"] == "canonical_recovery_environment_refused"


def test_restore_empty_refuses_staging_before_any_client_or_path() -> None:
    composers = _fake_composers(_FakeComposition())

    exit_code, _, _ = _run_main(
        [
            "restore-empty",
            "--bundle-id",
            str(_BUNDLE_ID),
            "--target-database",
            "knowledge",
            "--confirm-target-database",
            "knowledge",
        ],
        environ=_STAGING_ENVIRONMENT,
        composers=composers,
    )

    assert exit_code == int(CanonicalCoreExitCode.CONFIG)
    assert all(composer.call_count == 0 for composer in composers.values())


def test_phase_one_acceptance_refuses_staging_before_any_client_or_path() -> None:
    composers = _fake_composers(_FakeComposition())

    exit_code, _, _ = _run_main(
        ["phase-one-acceptance"], environ=_STAGING_ENVIRONMENT, composers=composers
    )

    assert exit_code == int(CanonicalCoreExitCode.CONFIG)
    assert all(composer.call_count == 0 for composer in composers.values())


# --- Exact confirmation arguments -------------------------------------------


def test_backup_create_requires_exact_write_admission_confirmation() -> None:
    composers = _fake_composers(_FakeComposition())

    exit_code, stdout_documents, stderr_documents = _run_main(
        ["backup-create"], environ=_LOCAL_ENVIRONMENT, composers=composers
    )

    assert exit_code == int(CanonicalCoreExitCode.CONFIG)
    assert composers["backup_create"].call_count == 0
    assert stdout_documents[0]["result_code"] == "canonical_recovery_environment_refused"
    assert len(stderr_documents) == 1
    assert stderr_documents[0]["error_code"] == "canonical_recovery_environment_refused"


def test_restore_empty_requires_exact_target_confirmation() -> None:
    composers = _fake_composers(_FakeComposition())

    exit_code, _, _ = _run_main(
        [
            "restore-empty",
            "--bundle-id",
            str(_BUNDLE_ID),
            "--target-database",
            "knowledge",
            "--confirm-target-database",
            "different-database",
        ],
        environ=_LOCAL_ENVIRONMENT,
        composers=composers,
    )

    assert exit_code == int(CanonicalCoreExitCode.CONFIG)
    assert composers["restore_empty"].call_count == 0

    confirmed_composers = _fake_composers(_FakeComposition())
    exit_code, _, _ = _run_main(
        [
            "restore-empty",
            "--bundle-id",
            str(_BUNDLE_ID),
            "--target-database",
            "knowledge",
            "--confirm-target-database",
            "knowledge",
        ],
        environ=_LOCAL_ENVIRONMENT,
        composers=confirmed_composers,
    )
    assert exit_code == int(CanonicalCoreExitCode.OK)
    assert confirmed_composers["restore_empty"].call_count == 1


# --- Output shape -----------------------------------------------------------


def test_bootstrap_identity_emits_one_safe_json_document() -> None:
    composition = _FakeComposition(
        payload={
            "result_code": "identity_bootstrap_created",
            "user_id": str(uuid7()),
            "workspace_id": str(uuid7()),
            "device_id": str(uuid7()),
        }
    )
    composers = _fake_composers(composition)

    exit_code, stdout_documents, stderr_documents = _run_main(
        list(_BOOTSTRAP_ARGUMENTS), environ=_LOCAL_ENVIRONMENT, composers=composers
    )

    assert exit_code == int(CanonicalCoreExitCode.OK)
    assert composers["bootstrap_identity"].call_count == 1
    assert len(stdout_documents) == 1
    assert stdout_documents[0]["result_code"] == "identity_bootstrap_created"
    assert stderr_documents == []


def test_stdout_payload_is_one_compact_sorted_json_document() -> None:
    composition = _FakeComposition(
        payload={"result_code": "canonical_backup_created", "object_count": 2, "byte_total": 12}
    )
    composers = _fake_composers(composition)
    stdout = io.StringIO()

    exit_code = main(
        ["backup-create", "--confirm-write-admission-disabled"],
        environ=_LOCAL_ENVIRONMENT,
        stdout=stdout,
        stderr=io.StringIO(),
        compose_bootstrap_identity=composers["bootstrap_identity"],
        compose_read_current_source=composers["read_current_source"],
        compose_backup_create=composers["backup_create"],
        compose_backup_verify=composers["backup_verify"],
        compose_restore_empty=composers["restore_empty"],
    )

    assert exit_code == int(CanonicalCoreExitCode.OK)
    rendered = stdout.getvalue().strip()
    assert rendered.count("\n") == 0
    assert ", " not in rendered and '": ' not in rendered
    keys = list(json.loads(rendered))
    assert keys == sorted(keys)


def test_read_current_source_writes_bytes_only_to_exclusive_output_file(
    tmp_path: Path,
) -> None:
    content = b"canonical bytes never printed"
    composition = _FakeComposition(payload={"content_bytes": content})
    composers = _fake_composers(composition)
    output_file = tmp_path / "canonical-source.bin"

    exit_code, stdout_documents, stderr_documents = _run_main(
        [
            "read-current-source",
            "--workspace-id",
            str(uuid7()),
            "--source-id",
            str(uuid7()),
            "--output-file",
            str(output_file),
        ],
        environ=_LOCAL_ENVIRONMENT,
        composers=composers,
    )

    assert exit_code == int(CanonicalCoreExitCode.OK)
    assert output_file.read_bytes() == content
    assert len(stdout_documents) == 1
    assert stdout_documents[0]["size_bytes"] == len(content)
    rendered_stdout = json.dumps(stdout_documents[0])
    assert "canonical bytes never printed" not in rendered_stdout
    assert stderr_documents == []

    second_run_composers = _fake_composers(_FakeComposition(payload={"content_bytes": content}))
    exit_code, stdout_documents, _ = _run_main(
        [
            "read-current-source",
            "--workspace-id",
            str(uuid7()),
            "--source-id",
            str(uuid7()),
            "--output-file",
            str(output_file),
        ],
        environ=_LOCAL_ENVIRONMENT,
        composers=second_run_composers,
    )
    assert exit_code == int(CanonicalCoreExitCode.CLI)
    assert stdout_documents[0]["result_code"] == "output_file_exists"
    assert output_file.read_bytes() == content


# --- Error-code mapping -----------------------------------------------------


def test_error_codes_map_to_exit_classes() -> None:
    cases: list[tuple[ErrorCode, CanonicalCoreExitCode]] = [
        (ErrorCode.OBJECT_STORAGE_INPUT_INVALID, CanonicalCoreExitCode.CONTRACT),
        (ErrorCode.SOURCE_ALREADY_EXISTS, CanonicalCoreExitCode.CONTRACT),
        (ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID, CanonicalCoreExitCode.CONTRACT),
        (ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS, CanonicalCoreExitCode.CONTRACT),
        (ErrorCode.DATABASE_MIGRATION_BUSY, CanonicalCoreExitCode.BUSY),
        (ErrorCode.DATABASE_CONNECTION_UNAVAILABLE, CanonicalCoreExitCode.BUSY),
        (ErrorCode.CONFIGURATION_INVALID, CanonicalCoreExitCode.CONFIG),
        (ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED, CanonicalCoreExitCode.CONFIG),
        (ErrorCode.INTERNAL_ERROR, CanonicalCoreExitCode.INTERNAL),
    ]
    for error_code, expected_exit in cases:
        composition = _FakeComposition(error=ApplicationError(error_code))
        composers = _fake_composers(composition)
        exit_code, stdout_documents, stderr_documents = _run_main(
            list(_BOOTSTRAP_ARGUMENTS), environ=_LOCAL_ENVIRONMENT, composers=composers
        )
        assert exit_code == int(expected_exit), error_code.value
        assert stdout_documents == [{"result_code": error_code.value, "state": "error"}]
        assert len(stderr_documents) == 1, error_code.value
        assert stderr_documents[0]["error_code"] == error_code.value


def test_unexpected_failure_maps_to_internal_with_one_document() -> None:
    composition = _FakeComposition(error=RuntimeError("secret raw failure detail"))
    composers = _fake_composers(composition)

    exit_code, stdout_documents, stderr_documents = _run_main(
        list(_BOOTSTRAP_ARGUMENTS), environ=_LOCAL_ENVIRONMENT, composers=composers
    )

    assert exit_code == int(CanonicalCoreExitCode.INTERNAL)
    assert stdout_documents == [
        {"result_code": "canonical_operations_internal_error", "state": "error"}
    ]
    assert len(stderr_documents) == 1
    assert "secret raw failure detail" not in json.dumps(stderr_documents)


def test_phase_one_acceptance_composes_once_and_returns_payload() -> None:
    composition = _FakeComposition(payload={"result_code": "canonical_acceptance_completed"})
    composer = _CountingComposer(composition)
    composers = _fake_composers(_FakeComposition())
    composers["phase_one_acceptance"] = composer

    exit_code, stdout_documents, _ = _run_main(
        ["phase-one-acceptance"], environ=_LOCAL_ENVIRONMENT, composers=composers
    )

    assert exit_code == int(CanonicalCoreExitCode.OK)
    assert composer.call_count == 1
    assert all(
        other.call_count == 0 for name, other in composers.items() if name != "phase_one_acceptance"
    )
    assert stdout_documents == [{"result_code": "canonical_acceptance_completed"}]


# --- Timeout and interactive safety ------------------------------------------


def test_command_timeout_maps_to_busy_with_cancellation_cleanup() -> None:
    composition = _FakeComposition(delay_seconds=5.0)
    composers = _fake_composers(composition)

    exit_code, stdout_documents, _ = _run_main(
        list(_BOOTSTRAP_ARGUMENTS),
        environ=_LOCAL_ENVIRONMENT,
        composers=composers,
        command_timeout_seconds=0.01,
    )

    assert exit_code == int(CanonicalCoreExitCode.BUSY)
    assert stdout_documents == [{"result_code": "recovery_command_timeout", "state": "error"}]
    assert composition.events == ["started", "cleaned-up"]


def test_no_command_prompts_interactively(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_prompt(*args: object, **kwargs: object) -> None:
        raise AssertionError("canonical operations must never prompt interactively")

    monkeypatch.setattr("builtins.input", _fail_prompt)

    for argv in (
        list(_BOOTSTRAP_ARGUMENTS),
        [
            "backup-verify",
            "--bundle-id",
            str(_BUNDLE_ID),
        ],
        [
            "backup-create",
            "--confirm-write-admission-disabled",
        ],
    ):
        exit_code, _, _ = _run_main(argv)
        assert exit_code == int(CanonicalCoreExitCode.OK)


def test_raw_child_output_never_forwarded() -> None:
    raw_child_output = "RAW-CHILD-STDOUT-SENTINEL"
    composition = _FakeComposition(
        error=ApplicationError(ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE)
    )
    composers = _fake_composers(composition)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["backup-verify", "--bundle-id", str(_BUNDLE_ID)],
        environ=_LOCAL_ENVIRONMENT,
        stdout=stdout,
        stderr=stderr,
        compose_bootstrap_identity=composers["bootstrap_identity"],
        compose_read_current_source=composers["read_current_source"],
        compose_backup_create=composers["backup_create"],
        compose_backup_verify=composers["backup_verify"],
        compose_restore_empty=composers["restore_empty"],
    )

    assert exit_code == int(CanonicalCoreExitCode.BUSY)
    combined = stdout.getvalue() + stderr.getvalue()
    assert raw_child_output not in combined
    for line in combined.splitlines():
        if line.strip():
            json.loads(line)


def test_bootstrap_invocation_passes_exact_arguments() -> None:
    from tools.canonical_core_operations import BootstrapIdentityInvocation

    captured: list[BootstrapIdentityInvocation] = []
    composition = _FakeComposition(payload={"result_code": "identity_bootstrap_created"})

    class _CapturingComposer(_CountingComposer):
        def __init__(self) -> None:
            super().__init__(composition)

        def __call__(self, invocation: object, environ: Mapping[str, str]) -> _FakeComposition:
            assert isinstance(invocation, BootstrapIdentityInvocation)
            captured.append(invocation)
            return super().__call__(invocation, environ)

    capturing_composer = _CapturingComposer()
    composers = _fake_composers(composition)
    composers["bootstrap_identity"] = capturing_composer

    exit_code, _, _ = _run_main(
        list(_BOOTSTRAP_ARGUMENTS), environ=_LOCAL_ENVIRONMENT, composers=composers
    )

    assert exit_code == int(CanonicalCoreExitCode.OK)
    assert captured == [
        BootstrapIdentityInvocation(
            username="ops",
            user_display_name="Ops User",
            workspace_key="personal",
            workspace_display_name="Personal Knowledge",
            device_name="ops-laptop",
            device_kind="system",
        )
    ]


def test_bundle_id_reaches_backup_verify_invocation() -> None:
    from tools.canonical_core_operations import BackupVerifyInvocation

    captured: list[BackupVerifyInvocation] = []
    composition = _FakeComposition(payload={"result_code": "canonical_backup_verified"})

    class _CapturingComposer(_CountingComposer):
        def __init__(self) -> None:
            super().__init__(composition)

        def __call__(self, invocation: object, environ: Mapping[str, str]) -> _FakeComposition:
            assert isinstance(invocation, BackupVerifyInvocation)
            captured.append(invocation)
            return super().__call__(invocation, environ)

    composers = _fake_composers(composition)
    composers["backup_verify"] = _CapturingComposer()

    exit_code, stdout_documents, _ = _run_main(
        ["backup-verify", "--bundle-id", str(_BUNDLE_ID)],
        environ=_LOCAL_ENVIRONMENT,
        composers=composers,
    )

    assert exit_code == int(CanonicalCoreExitCode.OK)
    assert captured == [BackupVerifyInvocation(bundle_id=_BUNDLE_ID)]
    assert stdout_documents[0]["result_code"] == "canonical_backup_verified"


def test_composers_receive_environment_snapshot() -> None:
    composition = _FakeComposition(payload={"result_code": "ok"})
    composers = _fake_composers(composition)

    _run_main(list(_BOOTSTRAP_ARGUMENTS), environ=_LOCAL_ENVIRONMENT, composers=composers)

    assert composers["bootstrap_identity"].seen_environments == [dict(_LOCAL_ENVIRONMENT)]


def test_exit_code_enum_values_are_stable() -> None:
    assert [int(code) for code in CanonicalCoreExitCode] == [0, 2, 65, 69, 70, 75, 78]
