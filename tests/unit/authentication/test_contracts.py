"""Closed authentication state enums, scopes and authenticated contexts.

These tests pin every closed vocabulary later tasks consume to exactly the
states the design spec names: session states (spec 9.2), TOTP credential
states (spec 10.1/15.3), device authorization grant states (spec 11.4/15.5),
device token kinds/states and family states (spec 13/15.6/15.7), the Web and
device scopes (spec 6), and the shape of the authenticated contexts: ids,
scopes and revision anchors only, never credentials.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from personal_os.authentication.contracts import (
    AUTHENTICATED_WEB_SCOPES,
    FIXED_DEVICE_SCOPE,
    AuthenticatedDeviceContext,
    AuthenticatedWebContext,
    DeviceAuthorizationGrantState,
    DeviceScope,
    DeviceTokenFamilyState,
    DeviceTokenKind,
    DeviceTokenState,
    OpaqueCredential,
    TotpCredentialState,
    WebScope,
    WebSessionState,
)
from personal_os.authentication.errors import AUTHENTICATION_ERROR_CODES

USER_ID = UUID("123e4567-e89b-42d3-a456-426614174000")
WORKSPACE_ID = UUID("123e4567-e89b-42d3-a456-426614174001")
SESSION_ID = UUID("123e4567-e89b-42d3-a456-426614174002")
DEVICE_ID = UUID("123e4567-e89b-42d3-a456-426614174003")


def test_web_session_states_are_exactly_the_spec_nine_two_set() -> None:
    assert {state.value for state in WebSessionState} == {
        "pending_totp",
        "active",
        "recovery_limited",
        "revoked",
    }


def test_totp_credential_states_are_exactly_pending_active_replaced() -> None:
    assert {state.value for state in TotpCredentialState} == {
        "pending",
        "active",
        "replaced",
    }


def test_grant_states_are_exactly_pending_approved_denied_exchanged() -> None:
    assert {state.value for state in DeviceAuthorizationGrantState} == {
        "pending",
        "approved",
        "denied",
        "exchanged",
    }


def test_device_token_kinds_are_exactly_access_and_refresh() -> None:
    assert {kind.value for kind in DeviceTokenKind} == {"access", "refresh"}


def test_device_token_states_are_exactly_active_rotated_revoked() -> None:
    assert {state.value for state in DeviceTokenState} == {
        "active",
        "rotated",
        "revoked",
    }


def test_device_token_family_states_are_exactly_active_revoked() -> None:
    assert {state.value for state in DeviceTokenFamilyState} == {"active", "revoked"}


def test_web_scopes_are_exactly_the_fixed_administration_surface() -> None:
    assert {scope.value for scope in WebScope} == {
        "web_security_manage",
        "device_authorization_approve",
        "device_administration_manage",
    }
    assert frozenset(WebScope) == AUTHENTICATED_WEB_SCOPES


def test_device_scope_is_fixed_obsidian_sync() -> None:
    assert {scope.value for scope in DeviceScope} == {"obsidian_sync"}
    assert FIXED_DEVICE_SCOPE is DeviceScope.OBSIDIAN_SYNC


def test_authenticated_web_context_carries_ids_scopes_and_revision_only() -> None:
    context = AuthenticatedWebContext(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        web_session_id=SESSION_ID,
        credential_revision=3,
        scopes=AUTHENTICATED_WEB_SCOPES,
    )
    assert context.user_id == USER_ID
    assert context.workspace_id == WORKSPACE_ID
    assert context.web_session_id == SESSION_ID
    assert context.credential_revision == 3
    assert context.scopes <= AUTHENTICATED_WEB_SCOPES
    assert not any(
        field_name in ("password", "secret", "credential", "username")
        for field_name in context.__dataclass_fields__
    )
    with pytest.raises(FrozenInstanceError):
        context.credential_revision = 4  # type: ignore[misc]


def test_authenticated_device_context_carries_ids_and_fixed_scope_only() -> None:
    context = AuthenticatedDeviceContext(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        device_id=DEVICE_ID,
        scope=FIXED_DEVICE_SCOPE,
    )
    assert context.scope is DeviceScope.OBSIDIAN_SYNC
    assert set(context.__dataclass_fields__) == {
        "user_id",
        "workspace_id",
        "device_id",
        "scope",
    }
    with pytest.raises(FrozenInstanceError):
        context.device_id = USER_ID  # type: ignore[misc]


def test_opaque_credential_is_frozen_and_hides_secret_in_repr() -> None:
    credential = OpaqueCredential(
        token_kind=DeviceTokenKind.ACCESS,
        lookup_id=DEVICE_ID,
        secret=b"credential-secret-sentinel",
    )
    assert credential.token_kind is DeviceTokenKind.ACCESS
    assert "credential-secret-sentinel" not in repr(credential)
    with pytest.raises(FrozenInstanceError):
        credential.secret = b"replacement"  # type: ignore[misc]


def test_authentication_error_codes_are_exactly_the_seventeen_auth_codes() -> None:
    assert len(AUTHENTICATION_ERROR_CODES) == 17
    assert {code.value for code in AUTHENTICATION_ERROR_CODES} == {
        "authentication_required",
        "authentication_failed",
        "authentication_rate_limited",
        "recent_authentication_required",
        "csrf_validation_failed",
        "authorization_scope_denied",
        "totp_enrollment_state_invalid",
        "device_authorization_pending",
        "device_authorization_slow_down",
        "device_authorization_denied",
        "device_authorization_expired",
        "device_authorization_state_invalid",
        "device_revocation_confirmation_invalid",
        "device_credential_invalid",
        "device_revoked",
        "device_token_reuse_detected",
        "plugin_version_unsupported",
    }
