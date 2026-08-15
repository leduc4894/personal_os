"""Grammar and validation tests for the bootstrap identity command."""

from uuid import UUID

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.identity.contracts import (
    BOOTSTRAP_INPUT_REASONS,
    BootstrapDeviceKind,
    IdentityBootstrapError,
    validate_bootstrap_identity_command,
)


def build_raw_command(**overrides: str) -> dict[str, str]:
    raw = {
        "username": "duc",
        "user_display_name": " Duc ",
        "workspace_key": "main",
        "workspace_display_name": "Main knowledge",
        "device_name": " Desktop Obsidian ",
        "device_kind": "obsidian",
    }
    raw.update(overrides)
    return raw


def test_valid_command_exact_trims_display_and_device_names() -> None:
    command = validate_bootstrap_identity_command(**build_raw_command())
    assert command.user_display_name == "Duc"
    assert command.device_name == "Desktop Obsidian"
    assert command.username == "duc"
    assert command.workspace_key == "main"
    assert command.device_kind is BootstrapDeviceKind.OBSIDIAN


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("username", "Duc"),  # uppercase rejected
        ("username", "-duc"),  # leading punctuation rejected
        ("username", "d" * 65),  # length 65 rejected
        ("username", "duc!"),  # disallowed character
        ("workspace_key", "Main"),
        ("workspace_key", "_main"),
        ("workspace_key", "k" * 65),
        ("user_display_name", ""),  # empty after trim
        ("user_display_name", "  "),
        ("user_display_name", "x" * 201),
        ("workspace_display_name", "x" * 201),
        ("device_name", ""),
        ("device_name", "x" * 201),
    ],
)
def test_invalid_values_fail_closed_with_reason(field: str, value: str) -> None:
    with pytest.raises(IdentityBootstrapError) as raised:
        validate_bootstrap_identity_command(**build_raw_command(**{field: value}))
    assert raised.value.error_code is ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID
    reason = raised.value.safe_details["reason"]
    assert isinstance(reason, str)
    assert reason.endswith("_invalid")


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("username", "Duc", "username_invalid"),
        ("workspace_key", "_main", "workspace_key_invalid"),
        ("user_display_name", "   ", "display_name_invalid"),
        ("workspace_display_name", "\t", "display_name_invalid"),
        ("device_name", "", "device_name_invalid"),
        ("device_kind", "phone", "device_kind_invalid"),
    ],
)
def test_rejection_reasons_are_closed_members(field: str, value: str, expected_reason: str) -> None:
    with pytest.raises(IdentityBootstrapError) as raised:
        validate_bootstrap_identity_command(**build_raw_command(**{field: value}))
    reason = raised.value.safe_details["reason"]
    assert reason == expected_reason
    assert reason in BOOTSTRAP_INPUT_REASONS


def test_control_characters_rejected_in_free_text_fields() -> None:
    with pytest.raises(IdentityBootstrapError):
        validate_bootstrap_identity_command(**build_raw_command(user_display_name="a\u0000b"))
    with pytest.raises(IdentityBootstrapError):
        validate_bootstrap_identity_command(**build_raw_command(device_name="a\u0007b"))


def test_unicode_is_not_normalized_or_case_folded() -> None:
    # Fullwidth Latin capitals via escapes so lint cannot flag ambiguous text;
    # the assertion string is identical to the literal "\uff21\uff22\uff23 caf\u00e9".
    raw_display_name = "\uff21\uff22\uff23 caf\u00e9"
    command = validate_bootstrap_identity_command(
        **build_raw_command(workspace_display_name=raw_display_name)
    )
    assert command.workspace_display_name == raw_display_name


def test_device_kind_is_closed() -> None:
    with pytest.raises(IdentityBootstrapError):
        validate_bootstrap_identity_command(**build_raw_command(device_kind="phone"))
    assert {kind.value for kind in BootstrapDeviceKind} == {"obsidian", "web", "system"}


def test_no_uuid_is_accepted_by_validation() -> None:
    # Validation has no UUID inputs at all; the surface refuses extra keys.
    with pytest.raises(TypeError):
        validate_bootstrap_identity_command(**build_raw_command(), user_id=UUID(int=1))
