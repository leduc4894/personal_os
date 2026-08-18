"""Documentation-as-code contract for the authentication key material.

The web authentication runbook documents the one command operators use to
generate the current master key file. The keyring loader only accepts
hex-encoded key material (64 hex characters decoding to exactly 32 bytes),
so the documented pipeline must produce exactly that format — an earlier
revision of the runbook showed a raw-random pipeline whose output the
loader rejects with ``secret_file_invalid_encoding``.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
from api_runtime.authentication_crypto import load_authentication_keyring
from api_runtime.authentication_settings import AuthenticationSettings

from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.models import RuntimeEnvironment

RUNBOOK = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "operations"
    / "web-authentication-and-device-authorization.md"
)

#: The exact pipeline the runbook must document: 32 random bytes, hex-encoded
#: as one 64-character line (a trailing newline is stripped by the reader).
DOCUMENTED_PIPELINE = re.compile(
    r"head -c 32 /dev/urandom \| xxd -p -c 64 > \"\$KNOWLEDGE_SECRET_ROOT/auth-key-2026-08\.key\""
)


def test_runbook_documents_the_hex_key_generation_pipeline() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")

    assert DOCUMENTED_PIPELINE.search(content) is not None, (
        "the web authentication runbook must document the hex-encoding key "
        "generation pipeline; raw-random bytes are rejected by the keyring "
        "loader with secret_file_invalid_encoding"
    )
    assert "head -c 32 /dev/urandom >" not in content, (
        "the runbook must not suggest writing raw random bytes directly to "
        "the key file; the keyring loader only accepts hex-encoded material"
    )


def test_documented_pipeline_output_loads_through_the_keyring() -> None:
    """What `xxd -p -c 64` produces must load; raw bytes must not."""
    with tempfile.TemporaryDirectory() as secret_root_name:
        secret_root = Path(secret_root_name)
        hex_material = "ab" * 32 + "\n"  # exactly what xxd -p -c 64 emits
        (secret_root / "auth-key-2026-08.key").write_text(hex_material, encoding="utf-8")
        settings = AuthenticationSettings(
            environment=RuntimeEnvironment.LOCAL,
            secret_root=secret_root,
            allowed_origin="http://localhost:38000",
            current_key_id="auth-key-2026-08",
            current_key_file="auth-key-2026-08.key",
            minimum_plugin_version="0.1.0",
            maximum_plugin_version="0.1.0",
        )

        keyring = load_authentication_keyring(settings)

        assert "auth-key-2026-08" in keyring.keys_by_id

        from personal_os.error_contracts.exceptions import SecretFileError

        # Valid text that is not hex grammar is rejected by the keyring
        # decoder with the typed configuration error.
        (secret_root / "auth-key-2026-08.key").write_text("z" * 64 + "\n", encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_authentication_keyring(settings)

        # Raw binary bytes are rejected earlier, at the secret-file reader's
        # text boundary.
        (secret_root / "auth-key-2026-08.key").write_bytes(b"\xab" * 32)
        with pytest.raises(SecretFileError):
            load_authentication_keyring(settings)
