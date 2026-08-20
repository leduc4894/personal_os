"""Composition threading of the configured trusted-proxy CIDRs (spec 20.3).

These tests pin the settings flow the serve process depends on:
``AuthenticationSettings.trusted_proxy_cidrs`` (validated from
``KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS``) must reach the client-address resolver
of the composed runtime, so the exact configured trust governs every throttle
bucket — and the offline composition must keep its deterministic fail-closed
default (no trusted proxies: the socket peer always wins) while still
accepting explicit CIDRs for tests that exercise trusted forwarding.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Final, cast

import api_runtime.authentication_composition as authentication_composition
import pytest
from api_runtime.authentication_composition import (
    OfflineAuthenticationState,
    WebAuthenticationRuntime,
    compose_offline_web_authentication,
    compose_web_authentication,
    verify_keyring_covers_required_key_ids,
)
from api_runtime.authentication_crypto import AuthenticationKeyring
from api_runtime.authentication_dependencies import create_client_address_resolver
from api_runtime.authentication_settings import AuthenticationSettings
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.runtime_configuration.models import RuntimeEnvironment

_TRUSTED_PROXY_CIDRS: Final[tuple[str, ...]] = ("192.0.2.0/24",)
_TRUSTED_PEER: Final[tuple[str, int]] = ("192.0.2.10", 443)
_UNTRUSTED_PEER: Final[tuple[str, int]] = ("203.0.113.8", 443)
_FORWARDED_CLIENT: Final[str] = "198.51.100.7"


def build_request(*, client: tuple[str, int], forwarded_for: str) -> Request:
    """Build one login request scope from the given socket peer and chain."""
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/auth/login",
        "raw_path": b"/api/auth/login",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"x-forwarded-for", forwarded_for.encode("ascii"))],
        "client": client,
        "server": ("web-admin.example", 443),
    }
    return Request(scope)


def build_serve_settings(trusted_proxy_cidrs: tuple[str, ...]) -> AuthenticationSettings:
    """Build one valid serve-process settings snapshot with the given trust."""
    return AuthenticationSettings(
        environment=RuntimeEnvironment.STAGING,
        secret_root=Path.cwd(),
        allowed_origin="https://web-admin.example",
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        current_key_id="auth-key-v1",
        current_key_file="auth/auth-key-v1.hex",
        minimum_plugin_version="1.0.0",
        maximum_plugin_version="2.0.0",
    )


def build_serve_keyring() -> AuthenticationKeyring:
    """Build the minimal covering keyring of the serve composition."""
    return AuthenticationKeyring(
        current_key_id="auth-key-v1",
        keys_by_id=MappingProxyType({"auth-key-v1": bytes(range(32))}),
    )


def compose_serve_runtime(trusted_proxy_cidrs: tuple[str, ...]) -> WebAuthenticationRuntime:
    """Compose the real serve graph; the engine is only stored, never used."""
    return compose_web_authentication(
        settings=build_serve_settings(trusted_proxy_cidrs),
        keyring=build_serve_keyring(),
        engine=cast("AsyncEngine", SimpleNamespace()),
    )


def test_serve_settings_trusted_proxy_cidrs_reach_the_runtime_resolver() -> None:
    runtime = compose_serve_runtime(_TRUSTED_PROXY_CIDRS)
    behind_proxy = build_request(client=_TRUSTED_PEER, forwarded_for=_FORWARDED_CLIENT)
    direct_client = build_request(client=_UNTRUSTED_PEER, forwarded_for=_FORWARDED_CLIENT)
    assert runtime.resolve_client_address(behind_proxy) == _FORWARDED_CLIENT
    assert runtime.resolve_client_address(direct_client) == _UNTRUSTED_PEER[0]


def test_serve_runtime_without_configured_trust_stays_fail_closed() -> None:
    runtime = compose_serve_runtime(())
    behind_proxy = build_request(client=_TRUSTED_PEER, forwarded_for=_FORWARDED_CLIENT)
    assert runtime.resolve_client_address(behind_proxy) == _TRUSTED_PEER[0]


def test_offline_composition_defaults_to_the_fail_closed_empty_trust() -> None:
    runtime = compose_offline_web_authentication()
    behind_proxy = build_request(client=_TRUSTED_PEER, forwarded_for=_FORWARDED_CLIENT)
    assert runtime.resolve_client_address(behind_proxy) == _TRUSTED_PEER[0]


def test_offline_composition_accepts_explicit_trust_for_tests() -> None:
    runtime = compose_offline_web_authentication(trusted_proxy_cidrs=_TRUSTED_PROXY_CIDRS)
    behind_proxy = build_request(client=_TRUSTED_PEER, forwarded_for=_FORWARDED_CLIENT)
    assert runtime.resolve_client_address(behind_proxy) == _FORWARDED_CLIENT


def test_resolver_binds_the_configuration_at_composition_time() -> None:
    # The bound trust is snapshotted: later mutation of a sequence passed at
    # bind time must not change the trust of an already-composed runtime.
    mutable_cidrs: list[str] = ["192.0.2.0/24"]
    resolver = create_client_address_resolver(mutable_cidrs)
    mutable_cidrs.append("0.0.0.0/0")
    spoofed = build_request(client=_UNTRUSTED_PEER, forwarded_for=_FORWARDED_CLIENT)
    assert resolver(spoofed) == _UNTRUSTED_PEER[0]


def test_keyring_coverage_uses_the_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage must classify expiring credentials at the database time."""
    database_now = datetime(2030, 1, 1, tzinfo=UTC)
    observed_database_times: list[datetime] = []

    class DatabaseClock:
        async def database_now(self) -> datetime:
            return database_now

    class CredentialStoreWithObservedClock:
        def __init__(self, engine: AsyncEngine) -> None:
            del engine

        async def required_key_ids(self, *, database_now: datetime) -> frozenset[str]:
            observed_database_times.append(database_now)
            return frozenset()

    monkeypatch.setattr(
        authentication_composition,
        "CredentialStore",
        CredentialStoreWithObservedClock,
    )

    asyncio.run(
        verify_keyring_covers_required_key_ids(
            engine=cast("AsyncEngine", SimpleNamespace()),
            keyring=build_serve_keyring(),
            clock=DatabaseClock(),
        )
    )

    assert observed_database_times == [database_now]


def test_offline_authentication_state_keeps_throttle_buckets_independent() -> None:
    """A username lockout must not mutate the distinct source-bucket map."""
    offline_state = OfflineAuthenticationState(totp_active=False)
    assert offline_state.login_buckets is not offline_state.source_buckets
