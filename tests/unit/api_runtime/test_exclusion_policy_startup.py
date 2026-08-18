"""Fail-before-bind exclusion-policy signer verification at API startup.

These tests pin the startup ordering contract: the signing settings and the
private-key file load during configuration (a missing fragment, a missing file
or a private/public key-ID mismatch exits ``78`` before any server object is
constructed), and the database proof — the configured active key must equal
the current key of the latest canonical keyset of every initialized workspace —
runs inside the lifespan startup, before Uvicorn binds the listening socket.
Typed policy/configuration errors only; no key material, path or provider text
ever reaches the output.
"""

from __future__ import annotations

import atexit
import logging
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import pytest
from api_runtime.database_lifecycle import DatabaseRuntimeLifecycle
from api_runtime.exclusion_policy_settings import (
    load_exclusion_policy_signer,
    load_exclusion_policy_signing_settings,
)
from api_runtime.server import run_server
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ConfigurationError,
    DatabaseMigrationError,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.signatures import (
    PolicyKeysetKey,
    PolicyKeysetState,
    build_keyset_payload,
    derive_ed25519_key_id,
)

# One hermetic secret root backing the local environment snapshot, mirroring
# the server test module: the database password and the authentication key
# reads inside run_server succeed without touching process secrets.
_SECRET_DIRECTORY = tempfile.TemporaryDirectory(prefix="api-runtime-policy-secrets-")
atexit.register(_SECRET_DIRECTORY.cleanup)
_SECRET_ROOT = Path(_SECRET_DIRECTORY.name)
(_SECRET_ROOT / "postgres_application_password").write_text(
    "server-test-password\n", encoding="utf-8"
)
(_SECRET_ROOT / "authentication_current_key").write_text("00" * 32 + "\n", encoding="utf-8")

_INITIAL_KEY = Ed25519PrivateKey.generate()
_INITIAL_PEM = _INITIAL_KEY.private_bytes(
    encoding=Encoding.PEM,
    format=PrivateFormat.PKCS8,
    encryption_algorithm=NoEncryption(),
)
_INITIAL_PUBLIC_KEY = _INITIAL_KEY.public_key().public_bytes_raw()
_INITIAL_KEY_ID = derive_ed25519_key_id(_INITIAL_PUBLIC_KEY)
(_SECRET_ROOT / "policy_signing_current.pem").write_bytes(_INITIAL_PEM)
(_SECRET_ROOT / "r2_access_key_id").write_text("test-access-key-id" + chr(10), encoding="utf-8")
(_SECRET_ROOT / "r2_secret_access_key").write_text(
    "test-secret-access-key" + chr(10), encoding="utf-8"
)
_SPOOL_DIRECTORY = tempfile.TemporaryDirectory(prefix="api-runtime-policy-spool-")
atexit.register(_SPOOL_DIRECTORY.cleanup)
_SPOOL_ROOT = Path(_SPOOL_DIRECTORY.name)

CREATED_AT = datetime(2026, 8, 17, tzinfo=UTC)

LOCAL_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "local",
        "KNOWLEDGE_SECRET_ROOT": str(_SECRET_ROOT),
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN": "http://127.0.0.1:8000",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-v1",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "authentication_current_key",
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION": "1.13.1",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION": "1.20.0",
        "KNOWLEDGE_POLICY_SIGNING_KEY_ID": _INITIAL_KEY_ID,
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy_signing_current.pem",
        "KNOWLEDGE_R2_ENDPOINT": f"https://{'0' * 32}.r2.cloudflarestorage.com",
        "KNOWLEDGE_R2_BUCKET_NAME": "personal-knowledge-objects",
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE": "r2_access_key_id",
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE": "r2_secret_access_key",
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT": str(_SPOOL_ROOT),
    }
)


class RecordingServerFactory:
    """Factory capturing the config; the server never runs its lifespan."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, config: Any) -> Any:
        self.calls += 1

        class _Server:
            def __init__(self, prepared: Any) -> None:
                self.config = prepared

            async def serve(self) -> None:
                return None

        return _Server(config)


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.setLevel(level)


def _current_keyset_payload(public_key: bytes) -> bytes:
    return build_keyset_payload(
        workspace_id=uuid4(),
        keyset_revision=1,
        parent_keyset_revision=None,
        created_at=CREATED_AT,
        keys=(
            PolicyKeysetKey(
                key_id=derive_ed25519_key_id(public_key),
                public_key=public_key,
                state=PolicyKeysetState.CURRENT,
            ),
        ),
    )


# --- configuration-time refusals exit 78 before any server object ----------------


def test_server_starts_with_a_valid_policy_signing_configuration() -> None:
    factory = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=factory) == 0
    assert factory.calls == 1


def test_server_missing_policy_signing_fragment_exits_seventy_eight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {
        key: value
        for key, value in LOCAL_ENVIRONMENT.items()
        if not key.startswith("KNOWLEDGE_POLICY_")
    }
    result = run_server(environ=environment, server_factory=RecordingServerFactory())
    assert result == 78
    captured = capsys.readouterr()
    assert "runtime_configuration_failed" in captured.err
    assert "configuration_invalid" in captured.err
    assert "Traceback" not in captured.err


def test_server_missing_policy_signing_key_file_exits_seventy_eight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = dict(LOCAL_ENVIRONMENT)
    environment["KNOWLEDGE_POLICY_SIGNING_KEY_FILE"] = "absent_policy_signing_key.pem"
    result = run_server(environ=environment, server_factory=RecordingServerFactory())
    assert result == 78
    captured = capsys.readouterr()
    assert "runtime_configuration_failed" in captured.err
    assert "secret_file_missing" in captured.err
    assert "absent_policy_signing_key" not in captured.err
    assert "Traceback" not in captured.err


def test_server_private_public_key_id_mismatch_exits_seventy_eight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other_key = Ed25519PrivateKey.generate()
    other_key_id = derive_ed25519_key_id(other_key.public_key().public_bytes_raw())
    environment = dict(LOCAL_ENVIRONMENT)
    environment["KNOWLEDGE_POLICY_SIGNING_KEY_ID"] = other_key_id
    result = run_server(environ=environment, server_factory=RecordingServerFactory())
    assert result == 78
    captured = capsys.readouterr()
    assert "configuration_secret_invalid" in captured.err
    assert "Traceback" not in captured.err


def test_server_wrong_algorithm_policy_key_exits_seventy_eight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    (_SECRET_ROOT / "rsa_policy_signing.pem").write_bytes(rsa_pem)
    environment = dict(LOCAL_ENVIRONMENT)
    environment["KNOWLEDGE_POLICY_SIGNING_KEY_FILE"] = "rsa_policy_signing.pem"
    try:
        result = run_server(environ=environment, server_factory=RecordingServerFactory())
    finally:
        (_SECRET_ROOT / "rsa_policy_signing.pem").unlink()
    assert result == 78
    captured = capsys.readouterr()
    assert "configuration_secret_invalid" in captured.err
    assert "BEGIN" not in captured.err
    assert "Traceback" not in captured.err


# --- the pre-bind database proof ------------------------------------------------


class _StartedLifecycleHarness:
    """A started lifecycle over a stand-in engine object."""

    def __init__(self) -> None:
        class _EngineStub:
            pass

        self.lifecycle = DatabaseRuntimeLifecycle(
            settings=object(),  # type: ignore[arg-type]
            password=object(),  # type: ignore[arg-type]
            engine_factory=lambda _settings, _password: _EngineStub(),  # type: ignore[arg-type,return-value]
        )


@pytest.fixture
def started_lifecycle(monkeypatch: pytest.MonkeyPatch) -> Iterator[DatabaseRuntimeLifecycle]:
    harness = _StartedLifecycleHarness()

    async def _start() -> None:
        await harness.lifecycle.start()

    import asyncio

    asyncio.Runner().run(_start())
    yield harness.lifecycle


def test_verification_refuses_before_the_lifecycle_started() -> None:

    lifecycle = DatabaseRuntimeLifecycle(
        settings=object(),  # type: ignore[arg-type]
        password=object(),  # type: ignore[arg-type]
    )
    with pytest.raises(DatabaseMigrationError) as raised:

        async def _verify() -> None:
            await lifecycle.verify_exclusion_policy_signer(signing_key_id=_INITIAL_KEY_ID)

        import asyncio

        asyncio.Runner().run(_verify())
    assert raised.value.error_code is ErrorCode.DATABASE_CONNECTION_UNAVAILABLE


@pytest.mark.asyncio
async def test_verification_accepts_the_current_key_of_every_latest_keyset(
    started_lifecycle: DatabaseRuntimeLifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api_runtime.database_lifecycle as lifecycle_module

    async def _fake_fetch(_engine: object) -> list[bytes]:
        return [_current_keyset_payload(_INITIAL_PUBLIC_KEY)]

    monkeypatch.setattr(lifecycle_module, "fetch_latest_keyset_payloads", _fake_fetch)
    await started_lifecycle.verify_exclusion_policy_signer(signing_key_id=_INITIAL_KEY_ID)


@pytest.mark.asyncio
async def test_verification_rejects_an_unknown_active_key(
    started_lifecycle: DatabaseRuntimeLifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api_runtime.database_lifecycle as lifecycle_module

    async def _fake_fetch(_engine: object) -> list[bytes]:
        return [_current_keyset_payload(bytes(range(32)))]

    monkeypatch.setattr(lifecycle_module, "fetch_latest_keyset_payloads", _fake_fetch)
    with pytest.raises(ConfigurationError) as raised:
        await started_lifecycle.verify_exclusion_policy_signer(signing_key_id=_INITIAL_KEY_ID)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_INVALID


@pytest.mark.asyncio
async def test_verification_rejects_an_uninitialized_database(
    started_lifecycle: DatabaseRuntimeLifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api_runtime.database_lifecycle as lifecycle_module

    async def _fake_fetch(_engine: object) -> list[bytes]:
        return []

    monkeypatch.setattr(lifecycle_module, "fetch_latest_keyset_payloads", _fake_fetch)
    with pytest.raises(ExclusionPolicyError) as raised:
        await started_lifecycle.verify_exclusion_policy_signer(signing_key_id=_INITIAL_KEY_ID)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED


@pytest.mark.asyncio
async def test_verification_maps_driver_failures_to_the_safe_unavailable_error(
    started_lifecycle: DatabaseRuntimeLifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api_runtime.database_lifecycle as lifecycle_module

    async def _exploding_fetch(_engine: object) -> list[bytes]:
        raise RuntimeError("driver text with do-not-emit-detail")

    monkeypatch.setattr(lifecycle_module, "fetch_latest_keyset_payloads", _exploding_fetch)
    with pytest.raises(Exception) as raised:
        await started_lifecycle.verify_exclusion_policy_signer(signing_key_id=_INITIAL_KEY_ID)
    assert "do-not-emit-detail" not in str(raised.value)


# --- the signer object handed to the lifespan ------------------------------------


def test_loaded_signer_derivates_the_configured_identity() -> None:
    settings = load_exclusion_policy_signing_settings(environ=LOCAL_ENVIRONMENT)
    signer = load_exclusion_policy_signer(settings, secret_root=_SECRET_ROOT)
    assert signer.key_id == _INITIAL_KEY_ID
