# Runtime Configuration and Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add fail-closed typed runtime settings, bounded secret-file loading, transport-neutral errors, safe JSON diagnostics and request/trace correlation to the Python API, MCP and worker shells.

**Architecture:** Build small shared modules under `personal_os` with one-way dependencies: safe diagnostic values support typed errors; runtime configuration maps external failures into those errors; correlation and redaction feed a standard-library logging boundary; composition roots lazily invoke a shared runtime checker. Existing help/version/no-argument behavior remains side-effect free.

**Tech Stack:** CPython 3.14.6, Pydantic 2.13.4, Pydantic Settings 2.14.2, Python standard-library logging/contextvars/secrets/uuid, pytest 9.1.1, Ruff 0.15.22 and mypy 2.3.0 strict.

## Global Constraints

- Apply runtime configuration and diagnostics only to Python API, MCP and worker; do not modify Web or Obsidian runtime behavior.
- Exact-pin production dependencies `pydantic==2.13.4` and `pydantic-settings==2.14.2`; commit the updated `uv.lock`.
- Do not add structlog, OpenTelemetry SDK/exporter, FastAPI, MCP SDK, Temporal SDK, provider clients or metrics exporters.
- Load settings only from typed defaults and exact uppercase `KNOWLEDGE_*` environment variables; disable `.env`, CLI settings, TOML, YAML, JSON and remote sources.
- Keep settings immutable and perform no environment or filesystem read at module import, help, version, no-argument or syntax-error paths.
- Accept secrets only through `_FILE` references under an absolute resolved secret root; never log a secret value, path or root.
- Use registered error codes, registered safe details and registered diagnostic events; do not add free-form error or log messages.
- Emit schema-versioned JSON Lines through Python standard-library logging; `debug/info/warning` go to stdout and `error/critical` go to stderr exactly once.
- Do not emit exception messages, arguments, raw traceback, local variables or filesystem paths.
- Server request IDs are UUIDv7 and never reuse client IDs; trace context supports strict W3C version `00` without telemetry export.
- Logging/redaction failures never replace the original application error or exit code.
- Preserve process exits `0` success, `2` CLI syntax, `70` unexpected internal error and `78` configuration/secret error.
- Run mypy strict, Ruff, focused tests and `uv run poe verify`; coverage remains diagnostic and mutation testing remains deferred.

---

## Preflight

Before Task 1, use `superpowers:using-git-worktrees` to create an isolated worktree from commit `a1dc058` or its descendant. Then run:

```powershell
git status --short
uv --version
uv run python --version
uv run poe verify
```

Expected: clean status, uv `0.11.32`, Python `3.14.6` and the existing bootstrap suite passes. Do not execute this plan directly on `master`.

## File Map

### Shared contracts

- `src/personal_os/diagnostics/events.py`: safe scalar wrappers, diagnostic enums, event definitions and field registry.
- `src/personal_os/error_contracts/codes.py`: stable error categories, codes and metadata registry.
- `src/personal_os/error_contracts/exceptions.py`: safe typed exception hierarchy and serialization.
- `src/personal_os/runtime_configuration/models.py`: frozen runtime settings and service/environment/configured-level enums.
- `src/personal_os/runtime_configuration/loading.py`: exact environment snapshot, unknown-key rejection and Pydantic error mapping.
- `src/personal_os/runtime_configuration/secret_files.py`: bounded filesystem secret loader.
- `src/personal_os/diagnostics/trace_context.py`: strict W3C traceparent parse/create/format primitives.
- `src/personal_os/diagnostics/context.py`: UUIDv7 request context and `ContextVar` lifecycle.
- `src/personal_os/diagnostics/redaction.py`: forbidden-key normalization, sensitive-pattern inspection and fingerprints.
- `src/personal_os/diagnostics/logging.py`: JSON serializer, root handlers, safe application logger, dependency-log adapter and emergency output.
- `src/personal_os/diagnostics/runtime_check.py`: shared `check-runtime` orchestration and exit-code mapping.

### Composition roots

- `src/personal_os/command_shell.py`: optional `check-runtime` dispatch while preserving existing CLI behavior.
- `apps/api/src/api_runtime/runtime_check.py`: bind `ServiceName.API`.
- `apps/mcp/src/mcp_runtime/runtime_check.py`: bind `ServiceName.MCP`.
- `apps/worker/src/workflow_worker/runtime_check.py`: bind `ServiceName.WORKER`.
- The three existing `command.py` files lazily import their own runtime checker only when the subcommand is selected.

### Tests and documentation

- `tests/unit/error_contracts/`: registry, safe details, chaining and serialization.
- `tests/unit/runtime_configuration/`: settings sources and secret filesystem behavior.
- `tests/unit/diagnostics/`: event values, trace context, context isolation, redaction and logging.
- `tests/contract/test_runtime_check_commands.py`: three real subprocess command contracts.
- `tests/contract/test_sensitive_diagnostics.py`: end-to-end sentinel leak corpus.
- `README.md` and Python app READMEs: approved variables, command, output and exit codes.

---

### Task 1: Safe Diagnostic Values and Typed Error Registry

**Files:**

- Create: `src/personal_os/diagnostics/__init__.py`
- Create: `src/personal_os/diagnostics/events.py`
- Create: `src/personal_os/error_contracts/__init__.py`
- Create: `src/personal_os/error_contracts/codes.py`
- Create: `src/personal_os/error_contracts/exceptions.py`
- Create: `tests/unit/diagnostics/test_event_values.py`
- Create: `tests/unit/error_contracts/test_application_errors.py`

**Interfaces:**

- Consumes: standard library only.
- Produces: `SafeToken`, `ShortDigest`, `SafeDiagnosticValue`, `DiagnosticLevel`, `ResultCode`, `EventName`, `EVENT_DEFINITIONS`, `ErrorCategory`, `ErrorCode`, `ERROR_DEFINITIONS`, `ApplicationError` and four typed subclasses.

- [ ] **Step 1: Write failing safe-value and registry tests**

```python
# tests/unit/diagnostics/test_event_values.py
from __future__ import annotations

import pytest

from personal_os.diagnostics.events import EventName, SafeToken, ShortDigest


@pytest.mark.parametrize("value", ["api", "runtime_configuration", "provider.model-1"])
def test_safe_token_accepts_registered_ascii_shape(value: str) -> None:
    assert str(SafeToken.parse(value)) == value


@pytest.mark.parametrize("value", ["", "UPPER", "has space", "secret/value", "x" * 65])
def test_safe_token_rejects_unbounded_or_unsafe_text(value: str) -> None:
    with pytest.raises(ValueError, match="safe token"):
        SafeToken.parse(value)


def test_short_digest_requires_sixteen_lowercase_hex_characters() -> None:
    assert str(ShortDigest.parse("0123456789abcdef")) == "0123456789abcdef"
    with pytest.raises(ValueError, match="digest"):
        ShortDigest.parse("0123456789ABCDEf")


def test_event_names_are_closed() -> None:
    assert {event.value for event in EventName} == {
        "runtime_configuration_validated",
        "runtime_configuration_failed",
        "client_request_id_rejected",
        "trace_context_replaced",
        "logging_payload_rejected",
        "dependency_log",
        "internal_error",
    }
```

```python
# tests/unit/error_contracts/test_application_errors.py
from __future__ import annotations

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError


def test_error_uses_registry_metadata_and_safe_details() -> None:
    error = ConfigurationError(
        ErrorCode.CONFIGURATION_INVALID,
        safe_details={"count": 1, "field_names": (SafeToken.parse("log_level"),)},
    )
    assert error.category is ErrorCategory.CONFIGURATION
    assert error.is_retryable is False
    assert error.to_safe_dict() == {
        "error_code": "configuration_invalid",
        "category": "configuration",
        "is_retryable": False,
        "safe_message": "Runtime configuration is invalid",
        "safe_details": {"count": 1, "field_names": ["log_level"]},
    }


def test_error_never_serializes_cause_text() -> None:
    sentinel = "DO_NOT_LEAK_ERROR_CAUSE"
    try:
        raise ValueError(sentinel)
    except ValueError as cause:
        error = ConfigurationError(ErrorCode.CONFIGURATION_INVALID)
        error.__cause__ = cause
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert sentinel not in rendered
```

- [ ] **Step 2: Run the focused tests and confirm missing modules**

Run:

```powershell
uv run pytest tests/unit/diagnostics/test_event_values.py tests/unit/error_contracts/test_application_errors.py -q
```

Expected: collection fails because `personal_os.diagnostics.events` and `personal_os.error_contracts` do not exist.

- [ ] **Step 3: Implement safe values and the event registry**

Create empty package `__init__.py` files that contain only role-specific docstrings. In `events.py`, implement these exact public shapes:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, TypeAlias
from uuid import UUID

_SAFE_TOKEN_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_SHORT_DIGEST_PATTERN: Final = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True, slots=True)
class SafeToken:
    value: str

    @classmethod
    def parse(cls, value: str) -> SafeToken:
        if _SAFE_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("value does not satisfy the safe token contract")
        return cls(value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ShortDigest:
    value: str

    @classmethod
    def parse(cls, value: str) -> ShortDigest:
        if _SHORT_DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("value does not satisfy the short digest contract")
        return cls(value)

    def __str__(self) -> str:
        return self.value


class DiagnosticLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ResultCode(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class EventName(StrEnum):
    RUNTIME_CONFIGURATION_VALIDATED = "runtime_configuration_validated"
    RUNTIME_CONFIGURATION_FAILED = "runtime_configuration_failed"
    CLIENT_REQUEST_ID_REJECTED = "client_request_id_rejected"
    TRACE_CONTEXT_REPLACED = "trace_context_replaced"
    LOGGING_PAYLOAD_REJECTED = "logging_payload_rejected"
    DEPENDENCY_LOG = "dependency_log"
    INTERNAL_ERROR = "internal_error"


SafeDiagnosticScalar: TypeAlias = bool | int | StrEnum | UUID | SafeToken | ShortDigest
SafeDiagnosticValue: TypeAlias = SafeDiagnosticScalar | tuple[SafeDiagnosticScalar, ...]


@dataclass(frozen=True, slots=True)
class EventDefinition:
    level: DiagnosticLevel | None
    result_code: ResultCode
    required_fields: frozenset[str]
    allowed_fields: frozenset[str]
```

Build `EVENT_DEFINITIONS` as a `MappingProxyType` keyed by every `EventName`. Use the exact fixed level/result pairs from the approved spec. Give each event only these caller fields:

```text
runtime_configuration_validated: configured_log_level
runtime_configuration_failed: error_code, error_category, is_retryable, reason, count
client_request_id_rejected: reason
trace_context_replaced: reason
logging_payload_rejected: reason, count
dependency_log: logger_name, message_fingerprint
internal_error: error_code, error_category, is_retryable, exception_type, stack_fingerprint
```

Required fields are `configured_log_level` for validation; `error_code`, `error_category` and `is_retryable` for configuration failure; `reason` for client-ID and trace replacement; `reason` and `count` for payload rejection; both dependency fields; and all five internal-error fields. `reason` and `count` remain optional for configuration failure because not every typed configuration error supplies both. `dependency_log.level` is `None`, meaning only the dependency adapter may supply a normalized source level. All other event levels are fixed.

- [ ] **Step 4: Implement the closed error registry**

In `codes.py`, define `ErrorCategory`, all 12 `ErrorCode` values from the design, and:

```python
@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    category: ErrorCategory
    is_retryable: bool
    safe_message: str
    allowed_detail_fields: frozenset[str]
```

Create immutable `ERROR_DEFINITIONS`. Use only these detail fields:

```text
configuration_invalid: count, field_names
configuration_unknown_key: count
each secret_file_* code: reason
diagnostic_context_invalid: reason
diagnostic_payload_rejected: reason, count
internal_error: no safe details
```

Assert registry completeness at module definition time:

```python
if set(ERROR_DEFINITIONS) != set(ErrorCode):
    raise RuntimeError("error definition registry is incomplete")
```

- [ ] **Step 5: Implement safe typed exceptions**

In `exceptions.py`, implement:

```python
class ApplicationError(Exception):
    allowed_codes: frozenset[ErrorCode] = frozenset(ErrorCode)

    def __init__(
        self,
        error_code: ErrorCode,
        *,
        safe_details: Mapping[str, SafeDiagnosticValue] | None = None,
    ) -> None:
        if error_code not in self.allowed_codes:
            raise ValueError("error code is not valid for this exception type")
        definition = ERROR_DEFINITIONS[error_code]
        details = _validate_safe_details(definition, safe_details or {})
        self.error_code = error_code
        self.category = definition.category
        self.is_retryable = definition.is_retryable
        self.safe_message = definition.safe_message
        self.safe_details = MappingProxyType(details)
        super().__init__(f"{error_code.value}: {definition.safe_message}")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code.value,
            "category": self.category.value,
            "is_retryable": self.is_retryable,
            "safe_message": self.safe_message,
            "safe_details": _serialize_safe_details(self.safe_details),
        }
```

Validate detail keys against `allowed_detail_fields`; validate every scalar against the declared safe union without calling arbitrary `repr` or `str`. Convert enums, UUIDs, `SafeToken` and `ShortDigest` explicitly in `_serialize_safe_details`.

The validator accepts integers only in `0..2**63 - 1`, rejects `bool` through the integer branch so booleans remain their own explicit type, and limits tuples to 64 elements. It accepts no arbitrary string, float, list, mapping, path or user-defined object.

Define subclasses with exact code sets:

```text
ConfigurationError: configuration_invalid, configuration_unknown_key
SecretFileError: all secret_file_* codes
DiagnosticContextError: diagnostic_context_invalid, diagnostic_payload_rejected
InternalApplicationError: internal_error
```

- [ ] **Step 6: Run strict focused verification**

Run:

```powershell
uv run pytest tests/unit/diagnostics/test_event_values.py tests/unit/error_contracts/test_application_errors.py -q
uv run ruff check src/personal_os/diagnostics src/personal_os/error_contracts tests/unit/diagnostics tests/unit/error_contracts
uv run mypy src/personal_os/diagnostics src/personal_os/error_contracts
```

Expected: all commands pass; no exception test output contains `DO_NOT_LEAK_ERROR_CAUSE`.

- [ ] **Step 7: Commit the error foundation**

```powershell
git add src/personal_os/diagnostics src/personal_os/error_contracts tests/unit/diagnostics tests/unit/error_contracts
git commit -m "feat: add typed diagnostic error contracts"
```

---

### Task 2: Immutable Runtime Settings and Exact Environment Source

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/personal_os/runtime_configuration/__init__.py`
- Create: `src/personal_os/runtime_configuration/models.py`
- Create: `src/personal_os/runtime_configuration/loading.py`
- Create: `tests/unit/runtime_configuration/test_settings_loading.py`

**Interfaces:**

- Consumes: `SafeToken`, `ConfigurationError`, `ErrorCode` from Task 1.
- Produces: `ServiceName`, `RuntimeEnvironment`, `ConfiguredLogLevel`, frozen `RuntimeSettings`, `load_runtime_settings(service_name, *, environ=None) -> RuntimeSettings`.

- [ ] **Step 1: Write the failing source-precedence tests**

```python
# tests/unit/runtime_configuration/test_settings_loading.py
from __future__ import annotations

from pathlib import Path

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import (
    ConfiguredLogLevel,
    RuntimeEnvironment,
    ServiceName,
)


def test_loads_exact_environment_overrides(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_LOG_LEVEL": "warning",
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
        },
    )
    assert settings.service_name is ServiceName.API
    assert settings.environment is RuntimeEnvironment.TEST
    assert settings.log_level is ConfiguredLogLevel.WARNING
    assert settings.secret_root == tmp_path


def test_rejects_unknown_prefixed_key_without_echoing_name(tmp_path: Path) -> None:
    unknown_name = "KNOWLEDGE_DO_NOT_LEAK_SECRET_VALUE"
    with pytest.raises(ConfigurationError) as raised:
        load_runtime_settings(
            ServiceName.API,
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                unknown_name: "DO_NOT_LEAK_ENV_VALUE",
            },
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY
    rendered = str(raised.value.to_safe_dict())
    assert unknown_name not in rendered
    assert "DO_NOT_LEAK_ENV_VALUE" not in rendered


def test_settings_are_frozen(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.WORKER,
        environ={"KNOWLEDGE_SECRET_ROOT": str(tmp_path)},
    )
    with pytest.raises(Exception, match="frozen"):
        settings.log_level = ConfiguredLogLevel.DEBUG  # type: ignore[misc]
```

Also add exact tests for empty values, invalid enums, relative roots, lowercase `knowledge_log_level` having no effect, unrelated environment variables being ignored, and an existing `.env` sentinel having no effect.

- [ ] **Step 2: Run the settings tests and verify missing implementation**

Run:

```powershell
uv run pytest tests/unit/runtime_configuration/test_settings_loading.py -q
```

Expected: collection fails because `personal_os.runtime_configuration` does not exist.

- [ ] **Step 3: Add exact production dependencies and lock them**

Modify root project dependencies:

```toml
[project]
dependencies = [
  "pydantic==2.13.4",
  "pydantic-settings==2.14.2",
]
```

Run:

```powershell
uv lock
uv sync --all-packages --frozen
```

Expected: `uv.lock` resolves both direct pins; do not add an extra or direct `python-dotenv` dependency.

- [ ] **Step 4: Implement the frozen settings model**

Create a docstring-only package `__init__.py`. In `models.py`, define:

```python
from enum import StrEnum
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class ServiceName(StrEnum):
    API = "api"
    MCP = "mcp"
    WORKER = "worker"


class RuntimeEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ConfiguredLogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        env_file=None,
        enable_decoding=False,
    )

    service_name: ServiceName
    environment: RuntimeEnvironment = RuntimeEnvironment.LOCAL
    log_level: ConfiguredLogLevel = ConfiguredLogLevel.INFO
    secret_root: Path = Path("/run/secrets")

    @field_validator("secret_root")
    @classmethod
    def require_absolute_secret_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("secret root must be absolute")
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls, env_settings, dotenv_settings, file_secret_settings
        return (init_settings,)
```

Using init source only is deliberate: `loading.py` owns the exact environment snapshot and unknown-key check. It also prevents BaseSettings from auto-reading `.env` or file secrets.

- [ ] **Step 5: Implement exact environment loading and safe error mapping**

In `loading.py`, define constants:

```python
ENVIRONMENT_PREFIX = "KNOWLEDGE_"
ENVIRONMENT_FIELDS = {
    "KNOWLEDGE_ENVIRONMENT": "environment",
    "KNOWLEDGE_LOG_LEVEL": "log_level",
    "KNOWLEDGE_SECRET_ROOT": "secret_root",
}
```

Implement:

```python
def load_runtime_settings(
    service_name: ServiceName,
    *,
    environ: Mapping[str, str] | None = None,
) -> RuntimeSettings:
    source = dict(os.environ if environ is None else environ)
    unknown_count = sum(
        key.startswith(ENVIRONMENT_PREFIX) and key not in ENVIRONMENT_FIELDS
        for key in source
    )
    if unknown_count:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_UNKNOWN_KEY,
            safe_details={"count": unknown_count},
        )
    values = {
        field_name: source[environment_name]
        for environment_name, field_name in ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    try:
        return RuntimeSettings(service_name=service_name, **values)
    except ValidationError as cause:
        fields = tuple(
            SafeToken.parse(str(error["loc"][0]))
            for error in cause.errors(include_input=False, include_url=False)
            if error["loc"]
        )
        mapped = ConfigurationError(
            ErrorCode.CONFIGURATION_INVALID,
            safe_details={"count": len(cause.errors()), "field_names": fields},
        )
        raise mapped from cause
```

Do not include unknown key names, values, Pydantic messages or input in the mapped error.

- [ ] **Step 6: Prove disabled sources and no import-time read**

Add tests that:

- change process cwd to a directory containing `.env` with `KNOWLEDGE_LOG_LEVEL=debug` and verify an explicit empty `environ` still returns `info`;
- monkeypatch `os.environ` access only during module import and verify importing `models` and `loading` performs no read;
- mutate the source mapping after load and verify the existing settings object is unchanged;
- verify a direct `KNOWLEDGE_DATABASE_PASSWORD` variable is rejected as unknown.

Run:

```powershell
uv run pytest tests/unit/runtime_configuration/test_settings_loading.py -q
uv run ruff check src/personal_os/runtime_configuration tests/unit/runtime_configuration
uv run mypy src/personal_os/runtime_configuration
uv lock --check
```

Expected: all checks pass and the lockfile is current.

- [ ] **Step 7: Commit immutable settings**

```powershell
git add pyproject.toml uv.lock src/personal_os/runtime_configuration tests/unit/runtime_configuration/test_settings_loading.py
git commit -m "feat: add immutable runtime settings"
```

---

### Task 3: Bounded Secret-File Loading

**Files:**

- Create: `src/personal_os/runtime_configuration/secret_files.py`
- Create: `tests/unit/runtime_configuration/test_secret_files.py`

**Interfaces:**

- Consumes: `SecretFileError`, `ErrorCode`, `SafeToken` and Pydantic `SecretStr`.
- Produces: `read_secret_file(secret_file, secret_root, maximum_size_bytes=65_536) -> SecretStr`.

- [ ] **Step 1: Write failing content-normalization and boundary tests**

```python
# tests/unit/runtime_configuration/test_secret_files.py
from __future__ import annotations

import os
from pathlib import Path

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import SecretFileError
from personal_os.runtime_configuration.secret_files import read_secret_file


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(b"value", "value"), (b"value\n", "value"), (b"value\r\n", "value")],
)
def test_reads_utf8_and_removes_exactly_one_line_ending(
    tmp_path: Path, raw: bytes, expected: str
) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_bytes(raw)
    secret = read_secret_file(secret_file, tmp_path)
    assert secret.get_secret_value() == expected


def test_preserves_other_whitespace(tmp_path: Path) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_bytes(b"  value  \n\n")
    assert read_secret_file(secret_file, tmp_path).get_secret_value() == "  value  \n"


def test_rejects_symlink_escape_without_disclosing_paths(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    outside = tmp_path / "DO_NOT_LEAK_SECRET_PATH"
    outside.write_text("DO_NOT_LEAK_SECRET_VALUE", encoding="utf-8")
    link = secret_root / "credential"
    link.symlink_to(outside)
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(link, secret_root)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_OUTSIDE_ROOT
    rendered = f"{raised.value} {raised.value.to_safe_dict()}"
    assert str(outside) not in rendered
    assert "DO_NOT_LEAK_SECRET_VALUE" not in rendered
```

Add parameterized failures for empty, newline-only, UTF-8 BOM, NUL, invalid UTF-8, missing path, directory, `65,537` bytes and `secrets-backup` prefix escape. Add success cases for `65,536` bytes and an in-root symlink.

- [ ] **Step 2: Run tests and confirm the loader is missing**

Run:

```powershell
uv run pytest tests/unit/runtime_configuration/test_secret_files.py -q
```

Expected: collection fails because `runtime_configuration.secret_files` does not exist.

- [ ] **Step 3: Implement confidential error mapping helpers**

Define `DEFAULT_MAXIMUM_SECRET_SIZE_BYTES = 65_536` and private `_secret_error(code, reason)`. `reason` is selected only from these safe tokens:

```text
missing
outside_root
invalid_type
insecure_permissions
too_large
invalid_encoding
empty
```

Map raw filesystem and decoder exceptions with `raise mapped from cause`; never place the exception string or path into `safe_details`.

- [ ] **Step 4: Implement resolved-root, file-descriptor and size checks**

Implement this operation order:

```text
validate positive maximum
→ require absolute input paths
→ resolve root and candidate strictly
→ candidate.is_relative_to(root)
→ pre-open stat rejects non-regular target
→ open the resolved candidate binary read-only, adding O_NOFOLLOW where available
→ fstat opened descriptor and recheck regular type/size/permissions
→ bounded read maximum + 1
→ decode and normalize
```

Use `stat.S_ISREG`, `stat.S_IWGRP` and `stat.S_IWOTH`. Apply permission rejection only when `os.name == "posix"`. Use `Path.is_relative_to`, never string prefix comparison.

The normalization implementation is exact:

```python
if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
    raise _secret_error(ErrorCode.SECRET_FILE_INVALID_ENCODING, "invalid_encoding")
try:
    value = raw.decode("utf-8")
except UnicodeDecodeError as cause:
    raise _secret_error(
        ErrorCode.SECRET_FILE_INVALID_ENCODING,
        "invalid_encoding",
    ) from cause
if value.endswith("\r\n"):
    value = value[:-2]
elif value.endswith("\n"):
    value = value[:-1]
if value == "":
    raise _secret_error(ErrorCode.SECRET_FILE_EMPTY, "empty")
return SecretStr(value)
```

- [ ] **Step 5: Add platform permission and descriptor tests**

On POSIX, create mode `0o666` and assert `secret_file_insecure_permissions`; create `0o444` and assert success. Mark only this test with `@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")`.

Monkeypatch or use a controlled file replacement to prove the post-open `fstat` check rejects a non-regular or oversized opened target even if the pre-open stat passed. The test must not print the path on failure.

- [ ] **Step 6: Run focused verification**

Run:

```powershell
uv run pytest tests/unit/runtime_configuration/test_secret_files.py -q
uv run ruff check src/personal_os/runtime_configuration/secret_files.py tests/unit/runtime_configuration/test_secret_files.py
uv run mypy src/personal_os/runtime_configuration/secret_files.py
```

Expected: all applicable tests pass; POSIX permission test is explicitly skipped on Windows with the documented reason.

- [ ] **Step 7: Commit the secret loader**

```powershell
git add src/personal_os/runtime_configuration/secret_files.py tests/unit/runtime_configuration/test_secret_files.py
git commit -m "feat: add bounded secret file loading"
```

---

### Task 4: Request Identity and W3C Trace Context

**Files:**

- Create: `src/personal_os/diagnostics/trace_context.py`
- Create: `src/personal_os/diagnostics/context.py`
- Create: `tests/unit/diagnostics/test_trace_context.py`
- Create: `tests/unit/diagnostics/test_context_binding.py`

**Interfaces:**

- Consumes: `SafeToken`, `DiagnosticContextError`, `ErrorCode`.
- Produces: `TraceId`, `SpanId`, `TraceContext`, `TraceContextResolution`, `resolve_trace_context()`, `format_traceparent()`, `DiagnosticContext`, `DiagnosticContextResolution`, `create_diagnostic_context()`, `bind_diagnostic_context()`, `current_diagnostic_context()`, `detached_diagnostic_context()` and `copy_diagnostic_context()`.

- [ ] **Step 1: Write failing strict traceparent tests**

```python
# tests/unit/diagnostics/test_trace_context.py
from __future__ import annotations

import pytest

from personal_os.diagnostics.trace_context import format_traceparent, resolve_trace_context

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_valid_version_zero_keeps_trace_and_creates_local_span() -> None:
    resolved = resolve_trace_context(VALID)
    assert resolved.was_replaced is False
    assert str(resolved.context.trace_id) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert str(resolved.context.remote_parent_span_id) == "00f067aa0ba902b7"
    assert len(str(resolved.context.local_span_id)) == 16
    assert format_traceparent(resolved.context).startswith(
        "00-4bf92f3577b34da6a3ce929d0e0e4736-"
    )


@pytest.mark.parametrize(
    "value",
    [
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
        "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "malformed",
    ],
)
def test_invalid_present_header_is_replaced_without_echo(value: str) -> None:
    resolved = resolve_trace_context(value)
    assert resolved.was_replaced is True
    assert value not in repr(resolved)


def test_absent_header_creates_context_without_warning() -> None:
    resolved = resolve_trace_context(None)
    assert resolved.was_replaced is False
```

- [ ] **Step 2: Write failing UUIDv7 and ContextVar isolation tests**

```python
# tests/unit/diagnostics/test_context_binding.py
from __future__ import annotations

import asyncio
from uuid import UUID

from personal_os.diagnostics.context import (
    bind_diagnostic_context,
    create_diagnostic_context,
    current_diagnostic_context,
)


def test_server_request_id_is_uuid7_and_client_id_is_separate() -> None:
    client_id = "123e4567-e89b-12d3-a456-426614174000"
    resolved = create_diagnostic_context(client_request_id=client_id)
    assert resolved.context.request_id.version == 7
    assert resolved.context.client_request_id == UUID(client_id)
    assert resolved.context.request_id != resolved.context.client_request_id


def test_nested_binding_restores_parent() -> None:
    parent = create_diagnostic_context().context
    child = create_diagnostic_context().context
    with bind_diagnostic_context(parent):
        with bind_diagnostic_context(child):
            assert current_diagnostic_context() is child
        assert current_diagnostic_context() is parent
    assert current_diagnostic_context() is None


def test_concurrent_operations_do_not_leak_context() -> None:
    async def observe() -> UUID:
        context = create_diagnostic_context().context
        with bind_diagnostic_context(context):
            await asyncio.sleep(0)
            assert current_diagnostic_context() is context
            return context.request_id

    async def run() -> tuple[UUID, UUID]:
        first, second = await asyncio.gather(observe(), observe())
        return first, second

    first, second = asyncio.run(run())
    assert first != second
```

- [ ] **Step 3: Run tests and verify the context modules are absent**

Run:

```powershell
uv run pytest tests/unit/diagnostics/test_trace_context.py tests/unit/diagnostics/test_context_binding.py -q
```

Expected: collection fails because `trace_context` and `context` do not exist.

- [ ] **Step 4: Implement strict trace types and resolution**

In `trace_context.py`, use frozen wrappers that validate exact lowercase nonzero hex lengths. Define:

```python
@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: TraceId
    remote_parent_span_id: SpanId | None
    local_span_id: SpanId
    trace_flags: int


@dataclass(frozen=True, slots=True)
class TraceContextResolution:
    context: TraceContext
    was_replaced: bool
```

Use `re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})", value)`, reject zero IDs, parse flags with base 16, and generate IDs with `secrets.token_hex(16)` and `secrets.token_hex(8)` until nonzero. An absent value returns a fresh context with `was_replaced=False`; a present invalid value returns fresh context with `was_replaced=True`.

`format_traceparent(context)` returns:

```python
f"00-{context.trace_id}-{context.local_span_id}-{context.trace_flags:02x}"
```

- [ ] **Step 5: Implement request context resolution and binding**

In `context.py`, define:

```python
@dataclass(frozen=True, slots=True)
class DiagnosticContext:
    request_id: UUID
    client_request_id: UUID | None
    trace: TraceContext
    workflow_id: SafeToken | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticContextResolution:
    context: DiagnosticContext
    was_client_request_id_rejected: bool
    was_traceparent_replaced: bool
```

`create_diagnostic_context()` always calls `uuid.uuid7()`. A client ID is valid only when `str(UUID(value)) == value`; invalid or noncanonical input becomes `None` and sets the rejection flag without storing the raw value.

Use this exact boundary signature:

```python
def create_diagnostic_context(
    *,
    client_request_id: str | None = None,
    traceparent: str | None = None,
    workflow_id: SafeToken | None = None,
) -> DiagnosticContextResolution: ...
```

Use one private `ContextVar[DiagnosticContext | None]` with default `None`. Implement context managers with token reset in `finally`. `detached_diagnostic_context()` temporarily binds `None`. `copy_diagnostic_context()` returns `contextvars.copy_context()` and performs no thread submission itself.

- [ ] **Step 6: Add exception and detached-context cases**

Test that parent context is restored when a nested body raises, a task created under `detached_diagnostic_context()` sees `None`, and an explicitly copied context sees the approved request ID when invoked. Also assert rejected client/trace sentinel text appears in no dataclass repr.

- [ ] **Step 7: Run strict focused verification**

Run:

```powershell
uv run pytest tests/unit/diagnostics/test_trace_context.py tests/unit/diagnostics/test_context_binding.py -q
uv run ruff check src/personal_os/diagnostics tests/unit/diagnostics
uv run mypy src/personal_os/diagnostics
```

Expected: all tests and static checks pass.

- [ ] **Step 8: Commit correlation primitives**

```powershell
git add src/personal_os/diagnostics/trace_context.py src/personal_os/diagnostics/context.py tests/unit/diagnostics
git commit -m "feat: add diagnostic correlation context"
```

---

### Task 5: Safe Diagnostic Payload Validation and Fingerprinting

**Files:**

- Modify: `src/personal_os/diagnostics/events.py`
- Create: `src/personal_os/diagnostics/redaction.py`
- Create: `tests/unit/diagnostics/test_redaction.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    definition: EventDefinition
    fields: Mapping[str, SafeDiagnosticValue]


@dataclass(frozen=True, slots=True)
class RejectedDiagnosticPayload:
    reason: SafeToken
    count: int


def build_registered_event(
    event_name: EventName,
    fields: Mapping[str, object],
) -> DiagnosticEvent | RejectedDiagnosticPayload: ...


def fingerprint_text(value: str) -> ShortDigest: ...
def fingerprint_stack(exception: BaseException) -> ShortDigest: ...
def normalize_exception_type(exception: BaseException) -> SafeToken: ...
```

- [ ] **Step 1: Write failing event-boundary tests**

Create `tests/unit/diagnostics/test_redaction.py` with a sentinel that must never survive into an accepted or rejected result:

```python
from personal_os.diagnostics.events import (
    EventName,
    RejectedDiagnosticPayload,
    SafeToken,
    build_registered_event,
)


def test_rejects_unknown_field_without_retaining_value() -> None:
    sentinel = "do-not-emit-unknown-field"

    result = build_registered_event(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": "info", "invented_field": sentinel},
    )

    assert result == RejectedDiagnosticPayload(
        reason=SafeToken.parse("unknown_field"),
        count=1,
    )
    assert sentinel not in repr(result)


def test_rejects_forbidden_normalized_key_recursively() -> None:
    sentinel = "do-not-emit-nested-query"

    result = build_registered_event(
        EventName.RUNTIME_CONFIGURATION_FAILED,
        {
            "error_code": "configuration_invalid",
            "error_category": "configuration",
            "is_retryable": False,
            "reason": "validation_failed",
            "count": 1,
            "metadata": {"citation-text": sentinel},
        },
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert sentinel not in repr(result)
```

The second test deliberately combines an unknown field and a forbidden nested key. The implementation must return a constant rejection summary and must not copy either the rejected key or value into it.

- [ ] **Step 2: Write the sensitive-form corpus**

Parameterize values covering every required defense-in-depth family:

```python
@pytest.mark.parametrize(
    "unsafe_value",
    [
        "Bearer do-not-emit-bearer",
        "eyJhbGciOiJIUzI1NiJ9.ZG8tbm90LWVtaXQtand0.c2lnbmF0dXJl",
        "-----BEGIN PRIVATE KEY-----\ndo-not-emit-pem",
        "https://user:do-not-emit-password@example.test/resource",
        "https://example.test/object?X-Amz-Credential=do-not-emit-credential",
        "https://example.test/object?X-Amz-Signature=do-not-emit-signature",
        "https://example.test/object?X-Goog-Credential=do-not-emit-google",
        "https://example.test/object?sig=do-not-emit-signature",
        "https://example.test/object?token=do-not-emit-token",
    ],
)
def test_rejects_sensitive_value_patterns(unsafe_value: str) -> None:
    result = build_registered_event(
        EventName.CLIENT_REQUEST_ID_REJECTED,
        {"reason": unsafe_value},
    )

    assert isinstance(result, RejectedDiagnosticPayload)
    assert unsafe_value not in repr(result)
```

Add cases for all forbidden normalized key families after lowercase and punctuation removal:

```text
content, body, query, excerpt, citation_text, prompt, completion,
token, secret, password, credential, authorization, cookie,
signed_url, path, vector, embedding, traceback, exception_message
```

Also cover mappings nested eight levels deep, sequences longer than 64 items, recursive-looking containers constructed without an actual reference cycle, non-string keys, objects whose `__str__` and `__repr__` raise, negative integers, integers above `2**63 - 1`, noncanonical UUID strings, and non-finite floats. No rejected object representation may be evaluated.

- [ ] **Step 3: Confirm the payload tests fail**

Run:

```powershell
uv run pytest tests/unit/diagnostics/test_redaction.py -q
```

Expected: import or collection fails because `redaction.py` and the event builder do not exist.

- [ ] **Step 4: Implement bounded recursive inspection**

In `redaction.py`, define constants, not settings:

```python
MAX_DIAGNOSTIC_DEPTH = 8
MAX_DIAGNOSTIC_ITEMS = 64
MAX_SAFE_INTEGER = 2**63 - 1

FORBIDDEN_NORMALIZED_KEYS = frozenset(
    {
        "content",
        "body",
        "query",
        "excerpt",
        "citationtext",
        "prompt",
        "completion",
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "cookie",
        "signedurl",
        "path",
        "vector",
        "embedding",
        "traceback",
        "exceptionmessage",
    }
)
```

Normalize keys with `"".join(character for character in key.lower() if character.isalnum())`. Reject rather than replace any payload when:

- A forbidden normalized key occurs at any inspected level.
- The traversal exceeds eight mapping/sequence levels or 64 total items.
- A key is not a string.
- A string matches Bearer, JWT, PEM private-key, URL user-info or signed-URL patterns.
- A value is outside the closed safe-value types registered in Task 1.

The inspector returns only an internal boolean/reason code/count. It must never put an offending key, value, type representation or exception message into a return value or thrown exception.

- [ ] **Step 5: Implement registered event construction**

`build_registered_event()` looks up `EVENT_DEFINITIONS[event_name]`, validates that the exact required and allowed fields match the registry, then validates each value using the safe wrappers from Task 1. Freeze accepted fields with `MappingProxyType`; never retain the caller's mutable mapping. It returns:

```python
RejectedDiagnosticPayload(reason=SafeToken("unknown_field"), count=unknown_count)
RejectedDiagnosticPayload(reason=SafeToken("missing_field"), count=missing_count)
RejectedDiagnosticPayload(reason=SafeToken("unsafe_value"), count=unsafe_count)
```

It never raises for untrusted diagnostic data. Do not accept an event name as a free string; callers must supply `EventName`.

- [ ] **Step 6: Implement deterministic fingerprints**

`fingerprint_text()` returns the first 16 lowercase hexadecimal characters of SHA-256 over UTF-8 text. This function is used only after a dependency message has been rendered inside the logging boundary; the original text is immediately discarded.

`fingerprint_stack()` walks `traceback.extract_tb(exception.__traceback__)`, builds an internal sequence from `frame.name` and `frame.lineno` only, and hashes that sequence. It must exclude filenames, source lines, local values and exception arguments. If no traceback exists, hash the constant `"no_stack"`.

`normalize_exception_type()` uses only `type(exception).__module__` and `type(exception).__qualname__`, lowercases ASCII alphanumerics, replaces each run of other characters with `.`, strips separators and returns at most 64 characters. If normalization is empty or too long, return `exception.<16-character-type-name-digest>`. Do not call `str(exception)` or `repr(exception)`.

- [ ] **Step 7: Add fingerprint and hostile-object tests**

Assert the same dependency text gives the same digest, different text changes it, the digest is exactly 16 lowercase hexadecimal characters, two exceptions from the same code location share a stack fingerprint even when their messages contain different sentinels, and neither sentinel appears in any result.

Create a hostile exception whose `__str__` raises and prove exception-type and stack fingerprinting still work. Create a hostile payload object whose `__repr__` raises and prove event construction returns a constant rejection.

- [ ] **Step 8: Run focused verification**

Run:

```powershell
uv run pytest tests/unit/diagnostics/test_event_values.py tests/unit/diagnostics/test_redaction.py -q
uv run ruff check src/personal_os/diagnostics tests/unit/diagnostics
uv run mypy src/personal_os/diagnostics
```

Expected: all checks pass.

- [ ] **Step 9: Commit the safe payload boundary**

```powershell
git add src/personal_os/diagnostics tests/unit/diagnostics
git commit -m "feat: reject unsafe diagnostic payloads"
```

---

### Task 6: Structured JSON Logging and Emergency Serialization

**Files:**

- Create: `src/personal_os/diagnostics/logging.py`
- Modify: `src/personal_os/diagnostics/__init__.py`
- Create: `tests/unit/diagnostics/test_logging.py`
- Create: `tests/contract/test_sensitive_diagnostics.py`

**Interfaces:**

```python
type RejectionCounterHook = Callable[[EventName], None]


class DiagnosticLogger:
    def emit(
        self,
        event_name: EventName,
        fields: Mapping[str, object] | None = None,
    ) -> None: ...

    def emit_application_error(self, error: ApplicationError) -> None: ...
    def emit_internal_error(self, exception: BaseException) -> None: ...


def configure_diagnostics(
    settings: RuntimeSettings,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    rejection_counter_hook: RejectionCounterHook | None = None,
) -> DiagnosticLogger: ...


def emit_emergency_application_error(
    service: ServiceName,
    context: DiagnosticContext,
    error: ApplicationError,
    *,
    stderr: TextIO | None = None,
) -> None: ...


def emit_emergency_internal_error(
    service: ServiceName,
    context: DiagnosticContext,
    exception: BaseException,
    *,
    stderr: TextIO | None = None,
) -> None: ...


def reset_diagnostics_for_testing() -> None: ...
```

- [ ] **Step 1: Write failing JSON schema and stream-routing tests**

In `tests/unit/diagnostics/test_logging.py`, inject `StringIO` streams and a fixed UTC clock through a private test seam. Assert exact keys rather than substring-only output:

```python
def test_emits_one_schema_v1_json_line_to_stdout(runtime_settings: RuntimeSettings) -> None:
    stdout = StringIO()
    stderr = StringIO()
    logger = configure_diagnostics(runtime_settings, stdout=stdout, stderr=stderr)

    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": "info"},
    )

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert records == [
        {
            "diagnostic_schema_version": 1,
            "timestamp": "2026-08-13T00:00:00.000Z",
            "level": "info",
            "service": "api",
            "environment": "test",
            "event": "runtime_configuration_validated",
            "result_code": "succeeded",
            "request_id": None,
            "trace_id": None,
            "configured_log_level": "info",
        }
    ]
    assert stderr.getvalue() == ""
```

The test fixture must restore the real clock and call `reset_diagnostics_for_testing()` after every case. Add routing assertions that `debug`, `info` and `warning` go only to stdout while `error` and `critical` go only to stderr.

- [ ] **Step 2: Write correlation and idempotency tests**

Bind a known `DiagnosticContext` and assert canonical strings for `request_id`, `client_request_id`, `trace_id` and `workflow_id`. Outside a binding, assert `request_id` and `trace_id` are present as JSON `null`, while absent `client_request_id` and `workflow_id` are omitted.

Call `configure_diagnostics()` twice, emit once, and assert exactly one line across the two streams. Assert the root logger has exactly the two owned handlers and `propagate` behavior cannot duplicate an application event.

- [ ] **Step 3: Write dependency normalization tests**

After configuration, use `logging.getLogger("httpx.transport")` with `%s` arguments containing a sentinel. Assert the output contains:

```json
{
  "event": "dependency_log",
  "logger_name": "httpx.transport",
  "message_fingerprint": "<16 lowercase hex>",
  "result_code": "degraded"
}
```

Assert it contains no `message`, `args`, `exc_info`, traceback, sentinel or filesystem path. Add a `LogRecord` argument whose `__str__` raises; logging must produce one safe `logging_payload_rejected` line and must not raise into the caller.

- [ ] **Step 4: Write rejection and exception tests**

Cover these behaviors:

- An application event with an unknown or unsafe field emits only `logging_payload_rejected` with `reason="unsafe_payload"` and `count=1`.
- The rejection counter hook is called once; a hook that raises cannot escape or cause recursive logging.
- `emit_application_error()` emits the registered code, category, retryability and safe details with no traceback.
- `emit_internal_error()` emits `error_code="internal_error"`, normalized `exception_type` and `stack_fingerprint` without message, arguments, source paths or raw stack.
- A JSON serializer failure falls back to one constant rejection line and preserves the caller's control flow.
- Failure inside the non-recursive fallback writes at most one minimal emergency line and never calls the normal logger again.

- [ ] **Step 5: Confirm logging tests fail**

Run:

```powershell
uv run pytest tests/unit/diagnostics/test_logging.py -q
```

Expected: collection fails because the logging implementation does not exist.

- [ ] **Step 6: Implement the canonical serializer**

Use stdlib `json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)` and append exactly one `"\n"`. Format timestamps as RFC 3339 UTC with millisecond precision and a literal `Z`.

The serializer always writes these required fields in the record object:

```text
diagnostic_schema_version, timestamp, level, service, environment,
event, result_code, request_id, trace_id
```

It includes `client_request_id`, `workflow_id` and other registered optional fields only when present. It must never add a `message` key.

Define one maximum-level filter for stdout and one minimum-level filter for stderr. Mark owned handlers with a private attribute. On the first configuration, retain the prior root handlers for test restoration, replace them with exactly the two owned handlers and set the root level to `DEBUG`. On reconfiguration, close and replace only owned handlers without multiplying them. `reset_diagnostics_for_testing()` removes owned handlers and restores the captured prior handlers and root level.

- [ ] **Step 7: Implement application and dependency record paths**

`DiagnosticLogger.emit()` constructs a `DiagnosticEvent`, builds the schema record from the frozen settings snapshot plus the current context, and sends only validated scalar values to stdlib logging by using a private record marker. The handler formatter recognizes the marker and never uses a free-form message.

For an unmarked dependency `LogRecord`:

1. Normalize the source level into the closed diagnostic level enum.
2. Validate the logger name as a bounded ASCII token; if invalid, use `unknown.dependency`.
3. Call `record.getMessage()` once inside `try`, immediately hash it and discard it.
4. Discard `record.args`, `record.exc_info`, `record.exc_text`, stack text and pathname from serialization.
5. Emit only the registered `dependency_log` fields.

Any validation, rendering, hook or serialization failure enters a non-recursive constant fallback path. The fallback must not call `logging` again.

- [ ] **Step 8: Implement expected and unexpected error helpers**

`emit_application_error()` maps the `ApplicationError` registry fields to `runtime_configuration_failed`. Include only registered safe details such as validation `reason` and `count`; do not serialize `error.args`, `__cause__` or `__context__`.

`emit_internal_error()` constructs `internal_error` using only the fixed error code/category/retryability plus `normalize_exception_type()` and `fingerprint_stack()`.

Emergency helpers construct the same schema directly and write to the supplied stderr or `sys.stderr`. Configuration errors use `environment=null`; internal errors use the known environment only when a validated snapshot already exists. Each helper catches its own final stream-write error and returns `None`.

- [ ] **Step 9: Build the cross-layer leak corpus**

In `tests/contract/test_sensitive_diagnostics.py`, pass distinct `do-not-emit-*` sentinels through:

- Unknown settings variables and invalid settings values.
- Secret values, secret filenames, resolved secret roots and sibling-root paths.
- Invalid client request IDs and `traceparent` values.
- Every forbidden field family and sensitive value pattern.
- Dependency messages and arguments.
- Expected error causes and unexpected exception messages/arguments.
- Hostile objects whose `__str__` or `__repr__` raises.

Capture stdout, stderr, serialized return values, `str(error)` and `repr(error)`. Assert no full sentinel or registered path appears anywhere. Assert at least one test is collected by running the file directly; no conditional skip may bypass sentinel scanning.

- [ ] **Step 10: Run focused verification**

Run:

```powershell
uv run pytest tests/unit/diagnostics/test_logging.py tests/contract/test_sensitive_diagnostics.py -q
uv run ruff check src/personal_os/diagnostics tests/unit/diagnostics tests/contract/test_sensitive_diagnostics.py
uv run mypy src/personal_os/diagnostics
```

Expected: all checks pass, every captured line parses as one JSON object, and the leak corpus contains no sentinel.

- [ ] **Step 11: Commit structured diagnostics**

```powershell
git add src/personal_os/diagnostics tests/unit/diagnostics tests/contract/test_sensitive_diagnostics.py
git commit -m "feat: add safe structured diagnostics"
```

---

### Task 7: `check-runtime` Composition-Root Commands

**Files:**

- Modify: `src/personal_os/command_shell.py`
- Create: `src/personal_os/diagnostics/runtime_check.py`
- Modify: `apps/api/src/api_runtime/command.py`
- Create: `apps/api/src/api_runtime/runtime_check.py`
- Modify: `apps/mcp/src/mcp_runtime/command.py`
- Create: `apps/mcp/src/mcp_runtime/runtime_check.py`
- Modify: `apps/worker/src/workflow_worker/command.py`
- Create: `apps/worker/src/workflow_worker/runtime_check.py`
- Modify: `tests/unit/test_command_shell.py`
- Modify: `tests/contract/test_process_commands.py`
- Modify: `tests/contract/test_command_import_side_effects.py`
- Create: `tests/contract/test_runtime_check_commands.py`

**Interfaces:**

```python
type RuntimeCheck = Callable[[], int]


def run_bootstrap_command(
    identity: CommandIdentity,
    argv: Sequence[str] | None = None,
    *,
    runtime_check: RuntimeCheck | None = None,
) -> int: ...


def run_runtime_check(service: ServiceName) -> int: ...
```

- [ ] **Step 1: Write failing parser behavior tests**

Extend `tests/unit/test_command_shell.py`:

```python
def test_check_runtime_dispatches_explicit_callback() -> None:
    calls = 0

    def check_runtime() -> int:
        nonlocal calls
        calls += 1
        return 78

    result = run_bootstrap_command(
        CommandIdentity("personal-api", "API process"),
        ["check-runtime"],
        runtime_check=check_runtime,
    )

    assert result == 78
    assert calls == 1
```

Add tests proving `[]`, `--help`, `--version` and invalid syntax never call the callback. Invalid syntax must retain argparse exit code `2` and no Python traceback.

- [ ] **Step 2: Confirm command-shell tests fail**

Run:

```powershell
uv run pytest tests/unit/test_command_shell.py -q
```

Expected: the new keyword argument or subcommand is unsupported.

- [ ] **Step 3: Add lazy CLI dispatch**

Add a `check-runtime` subparser in `run_bootstrap_command()`. Invoke the callback only after successful parsing selects that subcommand. When `runtime_check is None`, call `parser.error()` so the path remains a syntax failure rather than silently succeeding.

Each composition wrapper supplies a private lazy callback:

```python
def _check_runtime() -> int:
    from api_runtime.runtime_check import run

    return run()


def run(argv: Sequence[str] | None = None) -> int:
    return run_bootstrap_command(IDENTITY, argv, runtime_check=_check_runtime)
```

Use the matching package name for MCP and worker. The import occurs only after `check-runtime` is selected, preserving the no-environment/no-filesystem behavior of all shell-only paths.

- [ ] **Step 4: Update import-side-effect contracts**

Extend `tests/contract/test_command_import_side_effects.py` so it still blocks environment access, file reads and network calls during module import and during `--help`, `--version`, no-argument and invalid-syntax invocations. Allow the wrapper AST to import only:

```text
__future__, collections.abc, typing, personal_os.command_shell
```

The new runtime module import must exist only inside `_check_runtime()` and the test must assert that this lazy import is not evaluated on shell-only paths.

- [ ] **Step 5: Write process-level runtime checks**

Create a subprocess parameterization for `personal-api`, `personal-mcp` and `personal-worker`. Build the environment from an allowlisted minimum, explicitly remove every inherited key starting with `KNOWLEDGE_`, and set an absolute temporary secret root:

```python
@pytest.mark.parametrize(
    ("command", "service"),
    [
        ("personal-api", "api"),
        ("personal-mcp", "mcp"),
        ("personal-worker", "worker"),
    ],
)
def test_check_runtime_emits_equivalent_success_shape(
    command: str,
    service: str,
    clean_runtime_environment: dict[str, str],
) -> None:
    completed = run_command(command, "check-runtime", env=clean_runtime_environment)

    assert completed.returncode == 0
    record = json.loads(completed.stdout)
    assert record["service"] == service
    assert record["event"] == "runtime_configuration_validated"
    assert record["result_code"] == "succeeded"
    assert UUID(record["request_id"]).version == 7
    assert len(record["trace_id"]) == 32
    assert completed.stderr == ""
```

Add invalid configuration with `KNOWLEDGE_LOG_LEVEL=do-not-emit-invalid-level`: exit `78`, one `runtime_configuration_failed` JSON line on stderr, no stdout and no sentinel. Add `KNOWLEDGE_UNKNOWN=do-not-emit-unknown`: the same exit contract with a safe unknown-count detail.

For exit `70`, monkeypatch `load_runtime_settings()` to raise a hostile exception in a unit test of `run_runtime_check()`; assert the emergency `internal_error` record and no exception text. Do not add a production environment switch that injects failures.

- [ ] **Step 6: Confirm process contracts fail**

Run:

```powershell
uv run pytest tests/contract/test_runtime_check_commands.py -q
```

Expected: all three executables reject `check-runtime` as unknown syntax.

- [ ] **Step 7: Implement shared orchestration**

`run_runtime_check(service)` performs one linear composition-boundary sequence:

```text
create_diagnostic_context()
bind_diagnostic_context(context)
load_runtime_settings(service=service)
configure_diagnostics(settings)
emit runtime_configuration_validated(configured_log_level)
return 0
```

Before a validated settings snapshot exists:

- Catch `ApplicationError`, emit one emergency `runtime_configuration_failed` line with `environment=null`, return `78`.
- Catch any other `BaseException` subclass derived from `Exception`, emit one emergency `internal_error` line, return `70`.

After settings exist, catch logger configuration or emission failures, use the emergency internal serializer with the validated environment and return `70`. Do not reload settings while handling an error. Do not catch `KeyboardInterrupt`, `SystemExit` or `GeneratorExit`.

Each app `runtime_check.py` contains only its fixed service binding:

```python
from personal_os.diagnostics.runtime_check import run_runtime_check
from personal_os.runtime_configuration.models import ServiceName


def run() -> int:
    return run_runtime_check(ServiceName.API)
```

Use `ServiceName.MCP` and `ServiceName.WORKER` in the other roots. These modules start no listener, daemon, poller, provider connection or telemetry exporter.

- [ ] **Step 8: Preserve all existing process behavior**

Extend `tests/contract/test_process_commands.py` for every executable:

- No args: help and exit `0`.
- `--help`: help and exit `0`.
- `--version`: version and exit `0`.
- Invalid syntax: exit `2`, usage on stderr and no traceback.
- None of those cases read an intentionally unreadable secret-file path supplied in an inherited-looking environment fixture.

The last assertion demonstrates that parsing paths still do not load settings; it must not rely only on mocking the loader.

- [ ] **Step 9: Run command and boundary verification**

Run:

```powershell
uv run pytest tests/unit/test_command_shell.py tests/contract/test_command_import_side_effects.py tests/contract/test_process_commands.py tests/contract/test_runtime_check_commands.py tests/contract/test_sensitive_diagnostics.py -q
uv run ruff check src apps tests
uv run mypy src apps
uv run lint-imports
```

Expected: all command, leak, lint, strict typing and architecture-boundary checks pass.

- [ ] **Step 10: Commit composition-root integration**

```powershell
git add src/personal_os/command_shell.py src/personal_os/diagnostics/runtime_check.py apps tests/unit/test_command_shell.py tests/contract
git commit -m "feat: add runtime diagnostic checks"
```

---

### Task 8: Operator Documentation and Full Acceptance

**Files:**

- Modify: `README.md`
- Modify: `apps/api/README.md`
- Modify: `apps/mcp/README.md`
- Modify: `apps/worker/README.md`
- Modify: `tests/contract/test_bootstrap_documentation.py`

- [ ] **Step 1: Write failing documentation contract tests**

Extend `tests/contract/test_bootstrap_documentation.py` to require the root README to contain:

```text
KNOWLEDGE_ENVIRONMENT
KNOWLEDGE_LOG_LEVEL
KNOWLEDGE_SECRET_ROOT
/run/secrets
check-runtime
0
2
70
78
```

Require each composition-root README to contain its exact command, safe JSON output statement, no-settings-dump statement and the four exit codes. Add a root assertion that `.env`, TOML, YAML, JSON settings, plaintext secret environment variables and secret values on a command line are unsupported.

- [ ] **Step 2: Confirm documentation tests fail**

Run:

```powershell
uv run pytest tests/contract/test_bootstrap_documentation.py -q
```

Expected: assertions fail because runtime configuration and diagnostic operation are not documented.

- [ ] **Step 3: Document the approved runtime contract**

In the root README, add a concise operator section with:

- The three approved variables and defaults.
- The production POSIX secret root `/run/secrets` and requirement for an explicit absolute Windows/local test root.
- File-only secrets bounded beneath that root, 64 KiB maximum, UTF-8, exactly one optional terminal newline and no empty value.
- The prohibition on plaintext secret environment variables, `.env`, TOML, YAML, JSON settings and command-line secret values.
- One JSON object per line, stdout/stderr level routing, correlation fields and the rule that raw content, queries, vectors, tokens, paths, exception text and settings dumps are never emitted.
- Commands `personal-api check-runtime`, `personal-mcp check-runtime`, `personal-worker check-runtime` and exit meanings `0`, `2`, `70`, `78`.

In each app README, show only that app's command and link back to the root contract rather than duplicating security rules incompletely.

- [ ] **Step 4: Verify dependency lock determinism**

Run:

```powershell
uv lock --check
git diff --exit-code -- uv.lock
uv sync --all-packages --all-groups --frozen
```

Expected: the lock is current, `uv.lock` is unchanged by checking, and the exact workspace environment installs from the frozen lock.

- [ ] **Step 5: Run the complete repository gate**

Run:

```powershell
uv run poe verify
```

Expected: formatting check, Ruff, mypy strict, import-linter, unit tests, integration tests and contract tests all pass according to the repository's existing `verify` task. If `poe verify` does not currently include one of the new test directories, update the Poe task in `pyproject.toml`, add that file to this task's commit and rerun the full gate.

Inspect `.github/workflows/quality.yml` and confirm the unchanged workflow runs `uv run --all-packages --frozen poe verify` in both `ubuntu-latest` and `windows-latest`. Before accepting the implementation branch, require successful `Ubuntu quality` and `Windows portability` jobs; a local run alone does not satisfy the cross-platform acceptance criterion.

- [ ] **Step 6: Smoke-test all three installed commands**

From a PowerShell session with inherited `KNOWLEDGE_*` variables removed and `KNOWLEDGE_SECRET_ROOT` set to a fresh absolute temporary directory, run:

```powershell
uv run personal-api check-runtime
uv run personal-mcp check-runtime
uv run personal-worker check-runtime
```

Expected for each: exit `0`, exactly one JSON object on stdout, no stderr, correct fixed service, `environment="local"`, UUIDv7 `request_id`, 32-character lowercase hex `trace_id`, and no settings dump or secret-root path.

Then set `KNOWLEDGE_LOG_LEVEL` to a unique sentinel and run one command. Expected: exit `78`, exactly one safe JSON object on stderr, no stdout and no sentinel text.

- [ ] **Step 7: Inspect change scope and instruction files**

Run:

```powershell
git status --short
git diff --check
git diff --stat
git diff -- src/personal_os apps tests README.md pyproject.toml uv.lock
(Get-Content AGENTS.md).Count
(Get-Content CLAUDE.md).Count
```

Expected: only files named by this plan are changed, no whitespace errors exist, no unrelated user edits were reformatted, and the two instruction-file line counts have been read as required by `AGENTS.md`.

- [ ] **Step 8: Commit operator documentation**

```powershell
git add README.md apps/api/README.md apps/mcp/README.md apps/worker/README.md tests/contract/test_bootstrap_documentation.py pyproject.toml
git commit -m "docs: document runtime diagnostics"
```

Include `pyproject.toml` only if Step 5 proved the existing gate omitted a required test suite and it was changed intentionally.

- [ ] **Step 9: Run post-commit verification**

Run:

```powershell
git status --short
uv run poe verify
```

Expected: the working tree is clean and the full gate still passes from committed content.

---

## Completion Boundary

This plan completes only the approved runtime configuration and diagnostics spec. It does not add framework adapters, HTTP/MCP/Temporal propagation, OpenTelemetry exporters, Loki/Tempo/Alloy/Sentry deployment, reloadable settings, concrete provider credentials or remote secret managers. Those capabilities require their owning specs.

Completion requires all of the following evidence:

1. Exact approved dependencies are locked and frozen installation succeeds.
2. Typed error, settings, secret-file, correlation, redaction and logging unit suites pass.
3. Cross-layer sentinel leak tests pass without skips and collect at least one test.
4. All three process commands satisfy the `0`, `2`, `70` and `78` contracts.
5. Help, version, no-argument and syntax-error paths perform no configuration or secret-file access.
6. Ruff, mypy strict, import-linter and the repository-wide `poe verify` gate pass locally and in the existing Ubuntu and Windows CI jobs.
7. Root and app READMEs document the operator contract without exposing or encouraging unsafe secret handling.
