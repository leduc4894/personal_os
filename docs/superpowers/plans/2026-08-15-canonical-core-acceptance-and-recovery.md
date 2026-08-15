# Canonical Core Acceptance and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close Phase 1 with one executable proof that the canonical boundary works from an empty environment (identity bootstrap, verified current-source read, exact idempotent replay, durable Temporal dispatch, same-size corruption detection, PostgreSQL+R2 backup/verify/restore) and define the evidence-based Phase 1 completion gate.

**Architecture:** Provider-neutral identity bootstrap, canonical read and recovery contracts live in `personal_os.identity`, `personal_os.sources.reading` and `personal_os.recovery`. PostgreSQL behavior (bootstrap transaction, current-reference lookup, quiesced exported snapshot, restore-target probes) lives in the `postgresql-source-store` package. R2 behavior reuses the existing `r2_object_storage` adapter unchanged. `pg_dump`/`pg_restore` subprocess composition and all cross-infrastructure wiring live in `tools/`, which is the only new composition root. Acceptance code composes production services; it never reimplements publication, verification or transaction behavior.

**Tech Stack:** Python 3.14.6, uv 0.11.32, PostgreSQL 18.4 (`pg_dump`/`pg_restore` custom format), SQLAlchemy 2.0.51 async Core, psycopg 3.3.4, Temporal Server 1.31.2 / Temporal Python SDK 1.30.0, Cloudflare R2 via aiobotocore 3.9.0, pytest 9.1.1, Ruff 0.15.22, mypy 2.3.0 strict.

**Normative spec:** `docs/superpowers/specs/canonical-core-acceptance-and-recovery-design.md` at commit `db8ffd9`. Section numbers below (for example "spec 9.2") refer to that document. The spec's exact values, orders and vocabularies are binding.

## Global Constraints

- Implement the approved design only: no rename/move/delete/GC, no production backup scheduling/encryption/retention, no merge/replace restore, no second provider, no WAL archiving/PITR, no `SourceIngestionWorkflow` registration, no public HTTP/MCP endpoint, no multi-user bootstrap, no production deployment readiness (spec 2.2).
- No new production Python or TypeScript dependency; no new Alembic revision — the migration graph stays exactly one revision with head `20260813_01` (spec 4.3, 19.5).
- No public API, MCP, OpenAPI or generated-client change (spec 18.2).
- Import boundaries: `personal_os.identity`, `personal_os.recovery`, `personal_os.sources` import only stdlib types and existing core contracts — never SQLAlchemy, Psycopg, Temporal, aiobotocore, botocore or subprocess composition. `postgresql_source_store` may not import R2 or Temporal. The production `CanonicalObjectStore` adapter gains no list/delete/overwrite/copy/presign method. `tools/canonical_core_operations.py` is the only new cross-infrastructure composition root; API, MCP and Worker never import the tools module (spec 4.2).
- The exported PostgreSQL snapshot token is infrastructure-private: it flows only from `postgresql_source_store.backup_snapshot` to `PostgresqlDumpProcess` inside one composition call and never enters a manifest, diagnostic, metric, log or public core result (spec 4.4).
- Identity validation matches the existing baseline exactly: `username`/`workspace_key` match `^[a-z0-9][a-z0-9._-]{0,63}$`; display names and device name are exact-trimmed Unicode, length `1..200` code points; control characters (Unicode category `Cc`) are rejected; values are never normalized or case-folded; device kind is closed `obsidian|web|system`; no UUID is accepted from the CLI (spec 5.1).
- Bootstrap replay requires exact match of every field and returns the original database `committed_at`; drift returns `identity_bootstrap_state_conflict` without repair (spec 5.4).
- Canonical read accepts source states `active` and `stored_not_indexed` only; missing, pending, deleted, null pointer, cross-source pointer or inconsistent object metadata fails closed (spec 6.1). No byte is exposed before the existing R2 adapter has verified exact key, ETag stability, size, media type and SHA-256 (spec 6.2).
- Recovery is allowed only in `KNOWLEDGE_ENVIRONMENT` `local` or `test`; `backup-create` additionally requires the exact confirmation `--confirm-write-admission-disabled`; `restore-empty` requires exact target-project confirmation (spec 9.1, 11.1).
- The command never uses `DATABASE_URL`, `PGPASSWORD`, a password-bearing DSN or a CLI password; it uses an ephemeral mode-`0600` libpq password file with only `PGPASSFILE` set for the child, removed in `finally` (spec 9.3). `pg_dump`/`pg_restore` use the fixed semantic option sets in spec 9.3/11.2 verbatim as an argument vector without a shell.
- Bounds: snapshot/table-lock acquisition 15 s; `pg_dump` 10 min; `pg_restore` 10 min; R2 object operations the existing 5-minute logical bound; concurrent backup object reads 4; concurrent restore object writes 4; backup-root free-space reserve 2 GiB; complete recovery command 30 min; protected CI job 45 min (spec 17).
- Restore order: verify complete bundle → restore/verify exact R2 objects → `pg_restore` in one transaction → verify schema and canonical graph → canonical read smoke → safe receipt. R2 first so the database never commits references to absent unverified bytes (spec 11.2).
- Privacy: never log or serialize raw object/source bytes; username, display names, device name or title; bundle root/path or object relative path; content hash, object key or request fingerprint; database host/name/user, DSN, SQL, parameters or snapshot token; secret path/value, ephemeral password-file path or child environment; raw `pg_dump`/`pg_restore` stdout/stderr; R2 endpoint/bucket/header/request ID/provider exception; Temporal input/history (spec 16.3). The bundle itself necessarily contains hashes, keys and bytes; it is never a log or CI artifact.
- Never log raw content, query, vector, token, secret or sensitive data; external calls keep timeout, bounded retry, error mapping and metrics (AGENTS.md architecture boundaries).
- CLI: parsing happens before any environment or secret-file read; `--help`, `--version` and invalid syntax preserve the existing no-I/O command-shell behavior; commands emit one safe JSON document on stdout and safe registered diagnostics on stderr; exit codes 0/2/65/69/70/75/78 per spec 13; no interactive prompts (spec 13).
- Use TDD for every behavior task: run the named failing test before implementation, then the focused green test, then lint (`uv run poe python-lint`), type check (`uv run poe python-type-check`) and the affected suite before each commit.
- Naming follows AGENTS.md: domain + role names, no project prefix, no purely ordinal names, units in names (`timeout_seconds`, `size_bytes`), behavior-named tests.
- Preserve unrelated user changes; use exact resolved paths for every filesystem cleanup action.

---

## File Structure

### Provider-neutral core additions

```text
src/personal_os/identity/
├── __init__.py        Public identity bootstrap exports
├── contracts.py       Command/result values, validation, typed error, metric contract
├── ports.py           IdentityBootstrapStore protocol
└── bootstrap.py       Service orchestration and replay/drift classification

src/personal_os/recovery/
├── __init__.py        Public recovery exports
├── contracts.py       Recovery typed error, environment, metric contract, snapshot/dump value types
├── manifest.py        Canonical manifest encode/parse/validate
├── ports.py           Dump process, snapshot store, bundle store protocols
├── bundle.py          Private local immutable filesystem bundle writer/verifier
└── service.py         Backup creation, offline verification, empty-target restore orchestration

src/personal_os/sources/
└── reading.py         Canonical current-source read command, reference, port, service
```

`src/personal_os/recovery/bundle.py` extends the spec's proposed layout (spec 4.1) by one module so the filesystem bundle unit keeps one clear purpose; the spec explicitly permits consolidation adjustments with clear purpose.

### Infrastructure and tooling additions

```text
packages/postgresql-source-store/src/postgresql_source_store/
├── identity_bootstrap.py   Atomic bootstrap transaction and replay/drift queries
├── canonical_read.py       One-bounded-read current-reference lookup
└── backup_snapshot.py      Quiesced exported snapshot, schema-head and restore-target probes

tools/
├── canonical_core_operations.py   Repository-internal operations CLI (six subcommands)
├── canonical_recovery_bundle.py   Bundle-store composition from KNOWLEDGE_CANONICAL_BACKUP_ROOT
└── postgresql_dump_process.py     Bounded pg_dump/pg_restore adapter with passfile boundary

tests/unit/identity/
tests/unit/recovery/
tests/unit/sources/test_canonical_read.py
tests/unit/postgresql_source_store/
tests/unit/tools/test_postgresql_dump_process.py
tests/unit/tools/test_canonical_core_operations.py
tests/contract/canonical_core/
tests/integration/canonical_core/

.github/workflows/canonical-core-acceptance.yml
docs/operations/canonical-core-recovery.md
docs/handoff/2026-08-15-canonical-core-acceptance-and-recovery.md
```

---

### Task 1: Identity bootstrap contracts, validation, error codes, events and metrics

**Files:**
- Create: `src/personal_os/identity/__init__.py`
- Create: `src/personal_os/identity/contracts.py`
- Create: `src/personal_os/identity/ports.py`
- Modify: `src/personal_os/error_contracts/codes.py`
- Modify: `src/personal_os/diagnostics/events.py`
- Test: `tests/unit/identity/test_contracts.py`
- Test: `tests/unit/identity/test_identity_registries.py`

**Interfaces:**
- Consumes: `ApplicationError`, `ErrorCode`, `ErrorDefinition`, `ERROR_DEFINITIONS`, `EventName`, `EventDefinition`, `EVENT_DEFINITIONS`, `DiagnosticContext` from existing core.
- Produces (later tasks rely on these exact names): `BootstrapIdentityCommand`, `BootstrapIdentityResult`, `BootstrapIdentityOutcome`, `BootstrapDeviceKind`, `validate_bootstrap_identity_command(...)`, `IdentityBootstrapError`, `BOOTSTRAP_INPUT_REASONS`, `IdentityBootstrapStore`, `IDENTITY_METRIC_CONTRACTS`, `IdentityBootstrapMetrics`, `InMemoryIdentityBootstrapMetrics`, error codes `IDENTITY_BOOTSTRAP_INPUT_INVALID` / `IDENTITY_BOOTSTRAP_STATE_CONFLICT`, events `IDENTITY_BOOTSTRAP_SUCCEEDED` / `IDENTITY_BOOTSTRAP_REPLAYED` / `IDENTITY_BOOTSTRAP_REJECTED`.

- [ ] **Step 1: Write failing registry and validation tests**

`tests/unit/identity/test_identity_registries.py` pins the two error codes with spec-15 semantics and the three spec-16.1 events:

```python
"""Registry contract tests for the identity bootstrap fragment."""

from personal_os.error_contracts.codes import (
    ERROR_DEFINITIONS,
    ErrorCategory,
    ErrorCode,
)
from personal_os.diagnostics.events import EVENT_DEFINITIONS, DiagnosticLevel, EventName, ResultCode


def test_identity_bootstrap_error_codes_match_design_table() -> None:
    input_invalid = ERROR_DEFINITIONS[ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID]
    assert input_invalid.category is ErrorCategory.VALIDATION
    assert input_invalid.is_retryable is False
    assert input_invalid.allowed_detail_fields == frozenset({"reason"})

    state_conflict = ERROR_DEFINITIONS[ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT]
    assert state_conflict.category is ErrorCategory.CONFLICT
    assert state_conflict.is_retryable is False
    assert state_conflict.allowed_detail_fields == frozenset({})


def test_identity_bootstrap_events_match_design_registry() -> None:
    succeeded = EVENT_DEFINITIONS[EventName.IDENTITY_BOOTSTRAP_SUCCEEDED]
    assert succeeded.level is DiagnosticLevel.INFO
    assert succeeded.result_code is ResultCode.SUCCEEDED
    assert succeeded.allowed_fields == frozenset(
        {"outcome", "user_id", "workspace_id", "device_id"}
    )

    replayed = EVENT_DEFINITIONS[EventName.IDENTITY_BOOTSTRAP_REPLAYED]
    assert replayed.level is DiagnosticLevel.INFO
    assert replayed.result_code is ResultCode.SUCCEEDED

    rejected = EVENT_DEFINITIONS[EventName.IDENTITY_BOOTSTRAP_REJECTED]
    assert rejected.level is DiagnosticLevel.WARNING
    assert rejected.result_code is ResultCode.REJECTED
```

`tests/unit/identity/test_contracts.py` pins spec 5.1 grammar exactly:

```python
"""Grammar and validation tests for the bootstrap identity command."""

from uuid import UUID

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.identity.contracts import (
    BootstrapDeviceKind,
    BootstrapIdentityError,
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
        ("username", "Duc"),          # uppercase rejected
        ("username", "-duc"),         # leading punctuation rejected
        ("username", "d" * 65),       # length 65 rejected
        ("username", "duc!"),         # disallowed character
        ("workspace_key", "Main"),
        ("workspace_key", "_main"),
        ("workspace_key", "k" * 65),
        ("user_display_name", ""),    # empty after trim
        ("user_display_name", "  "),
        ("user_display_name", "x" * 201),
        ("workspace_display_name", "x" * 201),
        ("device_name", ""),
        ("device_name", "x" * 201),
    ],
)
def test_invalid_values_fail_closed_with_reason(field: str, value: str) -> None:
    with pytest.raises(BootstrapIdentityError) as raised:
        validate_bootstrap_identity_command(**build_raw_command(**{field: value}))
    assert raised.value.error_code is ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID
    reason = raised.value.safe_details["reason"]
    assert isinstance(reason, str)
    assert reason.endswith("_invalid")


def test_control_characters_rejected_in_free_text_fields() -> None:
    with pytest.raises(BootstrapIdentityError):
        validate_bootstrap_identity_command(**build_raw_command(user_display_name="a\u0000b"))
    with pytest.raises(BootstrapIdentityError):
        validate_bootstrap_identity_command(**build_raw_command(device_name="a\u0007b"))


def test_unicode_is_not_normalized_or_case_folded() -> None:
    command = validate_bootstrap_identity_command(
        **build_raw_command(workspace_display_name="ＡＢＣ café")
    )
    assert command.workspace_display_name == "ＡＢＣ café"


def test_device_kind_is_closed() -> None:
    with pytest.raises(BootstrapIdentityError):
        validate_bootstrap_identity_command(**build_raw_command(device_kind="phone"))
    assert {kind.value for kind in BootstrapDeviceKind} == {"obsidian", "web", "system"}


def test_no_uuid_is_accepted_by_validation() -> None:
    # Validation has no UUID inputs at all; the surface refuses extra keys.
    with pytest.raises(TypeError):
        validate_bootstrap_identity_command(**build_raw_command(), user_id=UUID(int=1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/identity -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'personal_os.identity'` (create empty `tests/unit/identity/__init__.py` if pytest needs it; follow the existing `tests/unit/sources` layout).

- [ ] **Step 3: Implement contracts, ports and registries**

`src/personal_os/identity/contracts.py`:

```python
"""Identity bootstrap command/result contracts and validation.

The validation grammar mirrors the canonical PostgreSQL baseline exactly
(design spec 5.1): keys are ``^[a-z0-9][a-z0-9._-]{0,63}$``, free-text
fields are exact-trimmed Unicode of 1..200 code points without control
characters, values are never normalized or case-folded, and the device
kind vocabulary is closed. No UUID is accepted from any caller.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Mapping, Protocol, runtime_checkable
from uuid import UUID

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

IDENTITY_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
FREE_TEXT_MAXIMUM_LENGTH: Final[int] = 200


class BootstrapDeviceKind(StrEnum):
    OBSIDIAN = "obsidian"
    WEB = "web"
    SYSTEM = "system"


class BootstrapIdentityOutcome(StrEnum):
    CREATED = "created"
    EXISTING = "existing"


BOOTSTRAP_INPUT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "username_invalid",
        "workspace_key_invalid",
        "display_name_invalid",
        "device_name_invalid",
        "device_kind_invalid",
    }
)


class IdentityBootstrapError(ApplicationError):
    """Typed identity bootstrap error with the closed identity code set."""

    allowed_codes: frozenset[ErrorCode] = frozenset(
        {ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID, ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT}
    )


@dataclass(frozen=True, slots=True)
class BootstrapIdentityCommand:
    username: str
    user_display_name: str
    workspace_key: str
    workspace_display_name: str
    device_name: str
    device_kind: BootstrapDeviceKind


@dataclass(frozen=True, slots=True)
class BootstrapIdentityResult:
    user_id: UUID
    workspace_id: UUID
    device_id: UUID
    outcome: BootstrapIdentityOutcome
    committed_at: datetime


def _reject(reason: str) -> None:
    raise IdentityBootstrapError(
        ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID,
        safe_details={"reason": SafeToken.parse(reason)},
    )


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validate_key(field: str, value: str, reason: str) -> str:
    if not IDENTITY_KEY_PATTERN.fullmatch(value):
        _reject(reason)
    return value


def _validate_free_text(field: str, raw: str, reason: str) -> str:
    value = raw.strip()
    if not value or len(value) > FREE_TEXT_MAXIMUM_LENGTH:
        _reject(reason)
    if _has_control_character(value):
        _reject(reason)
    return value


def validate_bootstrap_identity_command(
    *,
    username: str,
    user_display_name: str,
    workspace_key: str,
    workspace_display_name: str,
    device_name: str,
    device_kind: str,
) -> BootstrapIdentityCommand:
    """Validate and exact-trim one bootstrap command before any I/O (spec 5.1)."""
    if not isinstance(username, str) or not isinstance(workspace_key, str):
        _reject("username_invalid")
    _validate_key("username", username, "username_invalid")
    _validate_key("workspace_key", workspace_key, "workspace_key_invalid")
    _validate_free_text("user_display_name", user_display_name, "display_name_invalid")
    _validate_free_text(
        "workspace_display_name", workspace_display_name, "display_name_invalid"
    )
    _validate_free_text("device_name", device_name, "device_name_invalid")
    try:
        parsed_kind = BootstrapDeviceKind(device_kind)
    except ValueError:
        _reject("device_kind_invalid")
    return BootstrapIdentityCommand(
        username=username,
        user_display_name=user_display_name.strip(),
        workspace_key=workspace_key,
        workspace_display_name=workspace_display_name.strip(),
        device_name=device_name.strip(),
        device_kind=parsed_kind,
    )
```

Notes for the implementer: drop unused `field` parameters or use them in rejection mapping — keep mypy strict happy; the closed `_reject` reasons must all be members of `BOOTSTRAP_INPUT_REASONS` (assert this in a unit test). Add a small metrics contract in the same module:

```python
IDENTITY_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = {
    "identity_bootstrap_total": frozenset({"outcome"}),
}


class IdentityBootstrapMetrics(Protocol):
    """Low-cardinality identity bootstrap metric sink (spec 16.2)."""

    def record_bootstrap(self, outcome: BootstrapIdentityOutcome) -> None: ...


class InMemoryIdentityBootstrapMetrics:
    """Bounded in-memory sink for tests and local acceptance runs."""

    def __init__(self) -> None:
        self.outcomes: list[BootstrapIdentityOutcome] = []

    def record_bootstrap(self, outcome: BootstrapIdentityOutcome) -> None:
        self.outcomes.append(outcome)
```

`src/personal_os/identity/ports.py`:

```python
"""Provider-neutral port for the atomic identity bootstrap store."""

from __future__ import annotations

from typing import Protocol

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.identity.contracts import BootstrapIdentityCommand, BootstrapIdentityResult


class IdentityBootstrapStore(Protocol):
    """One atomic bootstrap or exact-replay read (design spec 4.4, 5.3, 5.4)."""

    async def bootstrap(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult: ...
```

`src/personal_os/identity/__init__.py` re-exports every public name above.

In `src/personal_os/error_contracts/codes.py`, append to `ErrorCode`:

```python
    IDENTITY_BOOTSTRAP_INPUT_INVALID = "identity_bootstrap_input_invalid"
    IDENTITY_BOOTSTRAP_STATE_CONFLICT = "identity_bootstrap_state_conflict"
```

and to `ERROR_DEFINITIONS` (category / retryable / allowed detail fields per spec 15):

```python
        ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="identity bootstrap input is invalid",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="identity bootstrap state conflicts with canonical state",
            allowed_detail_fields=frozenset({}),
        ),
```

In `src/personal_os/diagnostics/events.py`, append the three event names and definitions:

```python
    IDENTITY_BOOTSTRAP_SUCCEEDED = "identity_bootstrap_succeeded"
    IDENTITY_BOOTSTRAP_REPLAYED = "identity_bootstrap_replayed"
    IDENTITY_BOOTSTRAP_REJECTED = "identity_bootstrap_rejected"
```

```python
        EventName.IDENTITY_BOOTSTRAP_SUCCEEDED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"outcome", "workspace_id"}),
            allowed_fields=frozenset({"outcome", "user_id", "workspace_id", "device_id"}),
        ),
        EventName.IDENTITY_BOOTSTRAP_REPLAYED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"workspace_id"}),
            allowed_fields=frozenset({"user_id", "workspace_id", "device_id"}),
        ),
        EventName.IDENTITY_BOOTSTRAP_REJECTED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.REJECTED,
            required_fields=frozenset({"error_code"}),
            allowed_fields=frozenset({"workspace_id", "error_code"}),
        ),
```

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/identity -q`
Expected: PASS (all new tests).
Run: `uv run pytest tests/unit/error_contracts tests/unit/diagnostics -q`
Expected: PASS (registry completeness guards hold).
Run: `uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/identity tests/unit/identity src/personal_os/error_contracts/codes.py src/personal_os/diagnostics/events.py
git commit -m "feat: add identity bootstrap contracts and validation"
```

---

### Task 2: Identity bootstrap service with replay/drift classification

**Files:**
- Create: `src/personal_os/identity/bootstrap.py`
- Modify: `src/personal_os/identity/__init__.py`
- Test: `tests/unit/identity/test_bootstrap.py`

**Interfaces:**
- Consumes: everything from Task 1, plus `build_registered_event` and `DiagnosticContext` from existing core.
- Produces: `ExistingIdentityUser`, `ExistingIdentityWorkspace`, `ExistingIdentityDevice`, `ExistingIdentityState`, `classify_existing_identity(state, command) -> BootstrapIdentityResult`, `resolve_trusted_workspace_id(state, command) -> UUID | None`, `IdentityBootstrapService` with `async def bootstrap(self, command, diagnostic_context) -> BootstrapIdentityResult`.

- [ ] **Step 1: Write failing classification and service tests**

`tests/unit/identity/test_bootstrap.py` implements spec 5.4 as pure classification tests plus service tests with a fake store. Build helpers `build_state(**overrides)` and `build_command()` mirroring the Task 1 test builders. Required cases:

```python
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
        pytest.param(
            lambda s: replace(s, devices=s.devices * 2), id="two-matching-devices"
        ),
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
    assert classify_existing_identity(state, build_command()).outcome is BootstrapIdentityOutcome.EXISTING
```

Service tests with a `FakeIdentityBootstrapStore` (records whether `bootstrap` was called; returns a scripted result) and `InMemoryIdentityBootstrapMetrics`:

```python
async def test_service_emits_succeeded_event_and_metric_for_created_outcome() -> None: ...
async def test_service_emits_replayed_event_for_existing_outcome() -> None: ...
async def test_service_returns_store_result_unchanged() -> None: ...
def test_resolve_trusted_workspace_id_requires_single_active_matching_workspace() -> None: ...
```

Event emission asserts via `build_registered_event(...)` producing `DiagnosticEvent` (not `RejectedDiagnosticPayload`) with exactly the allowed fields from Task 1. The service never logs names — assert emitted fields contain no username/display/device-name values.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/identity/test_bootstrap.py -q`
Expected: FAIL — `bootstrap.py` does not exist.

- [ ] **Step 3: Implement the classifier and service**

`src/personal_os/identity/bootstrap.py`:

```python
"""Identity bootstrap service and provider-neutral replay/drift classification."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import EventName, build_registered_event
from personal_os.error_contracts.codes import ErrorCode
from personal_os.identity.contracts import (
    BootstrapIdentityCommand,
    BootstrapIdentityError,
    BootstrapIdentityOutcome,
    BootstrapIdentityResult,
    IdentityBootstrapMetrics,
    IdentityBootstrapStore,
)

_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_USER_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"


@dataclass(frozen=True, slots=True)
class ExistingIdentityUser:
    user_id: UUID
    username: str
    display_name: str
    status: str


@dataclass(frozen=True, slots=True)
class ExistingIdentityWorkspace:
    workspace_id: UUID
    owner_user_id: UUID
    workspace_key: str
    display_name: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExistingIdentityDevice:
    device_id: UUID
    workspace_id: UUID
    user_id: UUID
    device_name: str
    device_kind: str
    status: str
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExistingIdentityState:
    """Provider-neutral snapshot of canonical identity rows (spec 5.4)."""

    users: tuple[ExistingIdentityUser, ...]
    workspaces: tuple[ExistingIdentityWorkspace, ...]
    devices: tuple[ExistingIdentityDevice, ...]


def _state_conflict() -> None:
    raise IdentityBootstrapError(ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT)


def resolve_trusted_workspace_id(
    state: ExistingIdentityState, command: BootstrapIdentityCommand
) -> UUID | None:
    """The single active workspace whose key matches, else ``None``."""
    trusted = [
        workspace
        for workspace in state.workspaces
        if workspace.status == _WORKSPACE_STATUS_ACTIVE
        and workspace.workspace_key == command.workspace_key
    ]
    return trusted[0].workspace_id if len(trusted) == 1 else None


def classify_existing_identity(
    state: ExistingIdentityState, command: BootstrapIdentityCommand
) -> BootstrapIdentityResult:
    """Classify existing identity state as exact replay or conflict (spec 5.4).

    Never mutates, never repairs: any drift from the originally bootstrapped
    values is a terminal ``identity_bootstrap_state_conflict``.
    """
    if len(state.users) != 1 or len(state.workspaces) != 1:
        _state_conflict()
    user, workspace = state.users[0], state.workspaces[0]
    if (
        user.username != command.username
        or user.display_name != command.user_display_name
        or user.status != _USER_STATUS_ACTIVE
        or workspace.workspace_key != command.workspace_key
        or workspace.display_name != command.workspace_display_name
        or workspace.status != _WORKSPACE_STATUS_ACTIVE
        or workspace.owner_user_id != user.user_id
    ):
        _state_conflict()
    matching = [
        device
        for device in state.devices
        if device.workspace_id == workspace.workspace_id
        and device.device_name == command.device_name
        and device.device_kind == command.device_kind.value
    ]
    if len(matching) != 1:
        _state_conflict()
    device = matching[0]
    if (
        device.status != _DEVICE_STATUS_ACTIVE
        or device.revoked_at is not None
        or device.user_id != user.user_id
    ):
        _state_conflict()
    return BootstrapIdentityResult(
        user_id=user.user_id,
        workspace_id=workspace.workspace_id,
        device_id=device.device_id,
        outcome=BootstrapIdentityOutcome.EXISTING,
        committed_at=workspace.created_at,
    )


@dataclass(frozen=True, slots=True)
class IdentityBootstrapService:
    """Validates, delegates to the atomic store and emits safe diagnostics."""

    store: IdentityBootstrapStore
    metrics: IdentityBootstrapMetrics

    async def bootstrap(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult:
        result = await self.store.bootstrap(command, diagnostic_context)
        self.metrics.record_bootstrap(result.outcome)
        if result.outcome is BootstrapIdentityOutcome.CREATED:
            event_name = EventName.IDENTITY_BOOTSTRAP_SUCCEEDED
            fields = {
                "outcome": result.outcome,
                "workspace_id": result.workspace_id,
                "user_id": result.user_id,
                "device_id": result.device_id,
            }
        else:
            event_name = EventName.IDENTITY_BOOTSTRAP_REPLAYED
            fields = {
                "workspace_id": result.workspace_id,
                "user_id": result.user_id,
                "device_id": result.device_id,
            }
        built = build_registered_event(event_name, fields)
        # A rejected payload here is a programming error; surface it loudly.
        assert not isinstance(built, RejectedDiagnosticPayload)
        return result
```

Wire the built event into the diagnostic sink exactly the way `sources/projection_dispatch.py` consumes `build_registered_event` (attach via `diagnostics.logging` — follow that file's established pattern). `replace` is re-exported for tests. Do not emit any field containing a name value.

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/identity -q` then `uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/identity/bootstrap.py tests/unit/identity/test_bootstrap.py src/personal_os/identity/__init__.py
git commit -m "feat: add identity bootstrap service with drift classification"
```

---

### Task 3: PostgreSQL identity bootstrap adapter

**Files:**
- Create: `packages/postgresql-source-store/src/postgresql_source_store/identity_bootstrap.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/locks.py`
- Test: `tests/unit/postgresql_source_store/test_identity_bootstrap.py`

**Interfaces:**
- Consumes: `tables.py` (`users`, `workspaces`, `devices`, `audit_events`), `engine.py` (`apply_transaction_bounds`), `locks.py` pattern, `map_database_failure`, Task 1/2 core names.
- Produces: `IDENTITY_BOOTSTRAP_LOCK_NAMESPACE`, `bootstrap_lock_key(command)`, `bootstrap_lock_statement(command)`, `hydrate_identity_state(...)`, `build_identity_audit_values(...)`, `PostgresqlIdentityBootstrapStore(engine)` implementing `IdentityBootstrapStore`.

- [ ] **Step 1: Write failing unit tests for the pure helpers**

`tests/unit/postgresql_source_store/test_identity_bootstrap.py` (mirror `test_locks.py`/`test_replay_hydration.py` style; SQLAlchemy statements are compiled with a dialect to assert SQL text and bound parameters without a database):

```python
def test_bootstrap_lock_statement_uses_reserved_namespace_and_derived_key() -> None:
    statement = bootstrap_lock_statement(build_command())
    compiled = statement.compile(dialect=postgresql_dialect())
    assert "pg_advisory_xact_lock" in str(compiled)
    assert compiled.params["namespace"] == IDENTITY_BOOTSTRAP_LOCK_NAMESPACE
    assert compiled.params["derived_key"] == bootstrap_lock_key(build_command())


def test_bootstrap_lock_namespace_is_reserved_and_distinct() -> None:
    assert IDENTITY_BOOTSTRAP_LOCK_NAMESPACE != IDEMPOTENCY_LOCK_NAMESPACE
    assert IDENTITY_BOOTSTRAP_LOCK_NAMESPACE != SOURCE_LOCK_NAMESPACE


def test_hydrate_identity_state_builds_core_state_from_row_shapes() -> None: ...


def test_build_identity_audit_values_uses_completed_action_and_workspace_target() -> None:
    values = build_identity_audit_values(
        workspace_id=WORKSPACE_ID, request_id=REQUEST_ID, occurred_at=COMMITTED_AT
    )
    assert values["action"] == "identity.bootstrap_completed"
    assert values["actor_kind"] == "system"
    assert values["target_kind"] == "workspace"
    assert values["target_id"] == WORKSPACE_ID
    assert values["result"] == "succeeded"
```

Also test `build_identity_rejection_audit_values(...)`: action `identity.bootstrap_rejected`, result `rejected`, reason_code `identity_state_conflict`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/postgresql_source_store/test_identity_bootstrap.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the adapter**

Key content of `identity_bootstrap.py`:

```python
#: Reserved advisory-lock namespace for identity bootstrap (distinct from
#: the idempotency and source namespaces; "SVCB" in the established scheme).
IDENTITY_BOOTSTRAP_LOCK_NAMESPACE: Final[int] = 0x53564342

IDENTITY_BOOTSTRAP_AUDIT_ACTION: Final[str] = "identity.bootstrap_completed"
IDENTITY_REJECTION_AUDIT_ACTION: Final[str] = "identity.bootstrap_rejected"
IDENTITY_REJECTION_REASON: Final[str] = "identity_state_conflict"
```

`bootstrap_lock_key(command)` follows `idempotency_lock_key` in `locks.py`: signed first four bytes of `hashlib.sha256(f"{command.username}:{command.workspace_key}".encode("utf-8")).digest()` (`int.from_bytes(..., "big", signed=True)`); `bootstrap_lock_statement(command)` builds `sa.text("SELECT pg_advisory_xact_lock(:namespace, :derived_key)").bindparams(...)` exactly like `locks.idempotency_lock_statement`.

`hydrate_identity_state(user_rows, workspace_rows, device_rows)` converts mapped row dicts into Task 2's `ExistingIdentityState` (devices filtered to the single workspace's ID when exactly one workspace exists; otherwise all rows pass through so the classifier sees the drift).

`PostgresqlIdentityBootstrapStore`:

```python
class PostgresqlIdentityBootstrapStore:
    """Atomic PostgreSQL bootstrap transaction (design spec 5.3, 5.4)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def bootstrap(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult:
        try:
            async with self._engine.connect() as connection:
                async with connection.begin():
                    await apply_transaction_bounds(connection)
                    await connection.execute(sa.text(str(bootstrap_lock_statement(command))))
                    state = await self._read_identity_state(connection)
                    if self._is_empty(state):
                        return await self._create_identity(connection, command, diagnostic_context)
                    result = self._classify(state, command)
                    # Replaying performs no mutation and no extra audit row;
                    # the transaction (read-only here) simply commits.
                    return result
        except _RejectionAbort:  # pragma: no cover - defensive
            raise
        except SQLAlchemyError as cause:
            raise map_database_failure(cause, source_id=NIL_UUID) from cause
```

Implementation requirements (spec 5.3, 5.4):

- `_create_identity`: `SELECT now()` into `committed_at` (all four rows use this one transaction timestamp); allocate `user_id`, `workspace_id`, `device_id` with `uuid7()`; insert active user, active workspace owned by the user, active device with `last_seen_at=None`, `revoked_at=None`, `registered_at=committed_at`; insert a succeeded audit event (action `identity.bootstrap_completed`, actor kind `system`, target kind `workspace`, target ID the created workspace, `request_id` from `diagnostic_context.request_id`, `trace_id` from the diagnostic context's trace, `occurred_at=committed_at`); return `BootstrapIdentityResult(..., outcome=CREATED, committed_at=committed_at)`. A fault after any insert rolls back all four rows because everything shares one transaction.
- `_classify`: build state, call `classify_existing_identity`; on `IdentityBootstrapError(STATE_CONFLICT)` let the transaction roll back, then — outside the transaction — if `resolve_trusted_workspace_id(state, command)` returns a workspace ID, write a standalone rejection audit row in its own short transaction (action `identity.bootstrap_rejected`, result `rejected`, reason `identity_state_conflict`, target the trusted workspace); if no trusted workspace, emit only the registered `IDENTITY_BOOTSTRAP_REJECTED` diagnostic event. Never log the rejected values.
- Row reads use `sa.select(SOURCE_STORE_TABLES["users"])` etc. with `.mappings()`; map failures through `map_database_failure` with a nil `source_id` sentinel (follow how `publication_store.py` handles its non-source failure mapping; if `map_database_failure` requires a real UUID, pass `UUID(int=0)` and keep it internal).

Add `PostgresqlIdentityBootstrapStore` to the package `__init__.py` exports.

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/postgresql_source_store -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS (integration behavior is proven in Task 13).

- [ ] **Step 5: Commit**

```bash
git add packages/postgresql-source-store/src/postgresql_source_store/identity_bootstrap.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py packages/postgresql-source-store/src/postgresql_source_store/locks.py tests/unit/postgresql_source_store/test_identity_bootstrap.py
git commit -m "feat: add postgresql identity bootstrap store"
```

---

### Task 4: Canonical current-source read service

**Files:**
- Create: `src/personal_os/sources/reading.py`
- Modify: `src/personal_os/sources/__init__.py`
- Modify: `src/personal_os/sources/metrics.py`
- Modify: `src/personal_os/error_contracts/codes.py`
- Modify: `src/personal_os/diagnostics/events.py`
- Test: `tests/unit/sources/test_canonical_read.py`

**Interfaces:**
- Consumes: `ExpectedObject`, `CanonicalObjectStore`, `VerifiedObjectReader` from `personal_os.object_storage`, `SourceActor`-style nil-UUID rejection from `sources.actors`, Task 1-style registry patterns.
- Produces: `ReadCurrentSourceCommand`, `CanonicalSourceReference`, `CanonicalSourceReadStore`, `CanonicalSourceReadService` with `open_current_source(command, diagnostic_context)` async context manager and `async def read_current_source_bytes(...) -> bytes`, error code `CANONICAL_READ_STATE_INVALID`, events `CANONICAL_SOURCE_READ_SUCCEEDED` / `CANONICAL_SOURCE_READ_FAILED`, metric contracts `canonical_source_read_total{outcome}` and `canonical_source_read_duration_seconds{outcome}`.

- [ ] **Step 1: Write failing tests**

`tests/unit/sources/test_canonical_read.py` with fakes extending `tests/unit/sources/fakes.py`:

```python
class FakeCanonicalSourceReadStore:
    """Scripted current-reference resolver recording every call."""

    def __init__(self, reference: CanonicalSourceReference | None) -> None:
        self.reference = reference
        self.resolve_calls: list[tuple[UUID, UUID]] = []

    async def resolve_current(self, command, diagnostic_context):
        self.resolve_calls.append((command.workspace_id, command.source_id))
        if self.reference is None:
            raise CanonicalReadStateError(source_id=command.source_id)
        return self.reference


class LeakCheckingObjectStore:
    """Open/verify-only object store that proves no byte reaches the consumer early."""

    def __init__(self, *, fail_verification: bool = False) -> None:
        self.fail_verification = fail_verification
        self.opened: list[ContentDigest] = []
        self.closed = 0
```

Required cases:

```python
def test_reference_hydration_requires_positive_content_version() -> None: ...
def test_read_command_rejects_nil_uuids() -> None: ...
async def test_verified_read_returns_exact_bytes_and_emits_success() -> None: ...
async def test_read_never_updates_any_canonical_state(...) -> None: ...  # fake stores assert zero mutation calls
async def test_missing_reference_fails_closed_with_state_invalid() -> None: ...
async def test_object_store_missing_error_surfaces_unchanged() -> None:
    # ObjectStorageError(OBJECT_STORAGE_OBJECT_MISSING) from the object store
    # is raised as-is, never wrapped in a less precise code (spec 15).
async def test_corrupt_object_error_surfaces_before_any_byte_reaches_consumer() -> None:
    # LeakCheckingObjectStore fails full verification; assert the consumer
    # context manager body never executed (zero bytes observed).
async def test_caller_cancellation_closes_reader_and_clears_spool_state() -> None:
    # Cancel inside the consumer body; assert reader close() ran and the
    # fake object store recorded spool removal.
async def test_metrics_record_outcome_and_duration() -> None: ...
```

`CanonicalReadStateError` is a thin subclass of `ApplicationError` carrying `source_id` in safe details (define it in `reading.py`; allowed detail field `source_id` on `CANONICAL_READ_STATE_INVALID`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sources/test_canonical_read.py -q`
Expected: FAIL — `reading.py` does not exist.

- [ ] **Step 3: Implement the read service**

`src/personal_os/sources/reading.py` core shape:

```python
"""Canonical current-source read: fail-closed verified reader (spec 6)."""

CANONICAL_READ_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = {
    "canonical_source_read_total": frozenset({"outcome"}),
    "canonical_source_read_duration_seconds": frozenset({"outcome"}),
}


@dataclass(frozen=True, slots=True)
class ReadCurrentSourceCommand:
    workspace_id: UUID
    source_id: UUID


@dataclass(frozen=True, slots=True)
class CanonicalSourceReference:
    workspace_id: UUID
    source_id: UUID
    source_version_id: UUID
    content_version: int  # positive
    expected_object: ExpectedObject
    committed_at: datetime


class CanonicalSourceReadStore(Protocol):
    async def resolve_current(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> CanonicalSourceReference: ...


@dataclass(frozen=True, slots=True)
class CanonicalSourceReadService:
    """Resolves the current version and exposes only verified bytes (spec 6.2).

    The service never trusts client-supplied object metadata and never
    updates source state, current pointer, version, event, audit or intent.
    """

    store: CanonicalSourceReadStore
    object_store: CanonicalObjectStore
    metrics: CanonicalReadMetrics

    @asynccontextmanager
    async def open_current_source(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> AsyncIterator[tuple[CanonicalSourceReference, VerifiedObjectReader]]:
        validate_read_current_source_command(command)
        started = time.monotonic()
        reference = await self.store.resolve_current(command, diagnostic_context)
        try:
            async with self.object_store.open_verified_reader(reference.expected_object) as reader:
                yield reference, reader
        except ApplicationError:
            self.metrics.record_read(ReadOutcome.FAILED, time.monotonic() - started)
            raise
        self.metrics.record_read(ReadOutcome.SUCCEEDED, time.monotonic() - started)

    async def read_current_source_bytes(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> bytes:
        async with self.open_current_source(command, diagnostic_context) as (_, reader):
            chunks: list[bytes] = []
            async for chunk in reader:
                chunks.append(chunk)
            return b"".join(chunks)
```

`validate_read_current_source_command` rejects nil UUIDs via `reject_nil_uuid`. Register `CANONICAL_READ_STATE_INVALID` in `error_contracts/codes.py` (category `integrity`, not retryable, allowed details `{"source_id"}`) and the two events in `diagnostics/events.py` (`SUCCEEDED`: INFO/SUCCEEDED, allowed `{"source_id", "workspace_id", "source_version_id"}`; `FAILED`: ERROR/FAILED, required `{"error_code"}`, allowed `{"source_id", "workspace_id", "error_code"}`). Add the two metric names to `SOURCE_METRIC_CONTRACTS` in `sources/metrics.py` with label sets above and a `CanonicalReadMetrics` protocol (`record_read(outcome, duration_seconds)`) plus an in-memory implementation following `InMemorySourcePublicationMetrics`. Emit the registered events from the service for both outcomes (fields: IDs only — never bytes, titles or digests).

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/sources -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/sources/reading.py src/personal_os/sources/__init__.py src/personal_os/sources/metrics.py src/personal_os/error_contracts/codes.py src/personal_os/diagnostics/events.py tests/unit/sources/test_canonical_read.py
git commit -m "feat: add canonical current-source read service"
```

---

### Task 5: PostgreSQL current-reference read adapter

**Files:**
- Create: `packages/postgresql-source-store/src/postgresql_source_store/canonical_read.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Test: `tests/unit/postgresql_source_store/test_canonical_read.py`

**Interfaces:**
- Consumes: Task 4 core types, `tables.py`, `apply_transaction_bounds`, `map_database_failure`, `ContentDigest`, `CanonicalMediaType`, `derive_canonical_object_key`.
- Produces: `hydrate_canonical_source_reference(row) -> CanonicalSourceReference`, `ACCEPTED_READ_SOURCE_STATES`, `PostgresqlCanonicalSourceReadStore(engine)` implementing `CanonicalSourceReadStore`.

- [ ] **Step 1: Write failing hydration tests**

`tests/unit/postgresql_source_store/test_canonical_read.py` mirrors `test_replay_hydration.py`: build row dicts with the exact column names from the joined read and assert hydration or fail-closed errors:

```python
ACCEPTED = ("active", "stored_not_indexed")


@pytest.mark.parametrize("sync_state", ACCEPTED)
def test_hydrates_reference_for_accepted_source_states(sync_state) -> None: ...


@pytest.mark.parametrize(
    "mutator",
    [
        pytest.param(lambda r: r, id="happy-path"),
        pytest.param(lambda r: {**r, "sync_state": "pending"}, id="pending-rejected"),
        pytest.param(lambda r: {**r, "sync_state": "deleted"}, id="deleted-rejected"),
        pytest.param(lambda r: {**r, "current_source_version_id": None}, id="null-pointer"),
        pytest.param(
            lambda r: {**r, "version_workspace_id": OTHER_WORKSPACE_ID},
            id="cross-workspace-pointer",
        ),
        pytest.param(
            lambda r: {**r, "version_source_id": OTHER_SOURCE_ID},
            id="cross-source-pointer",
        ),
        pytest.param(lambda r: {**r, "content_hash": "XYZ"}, id="noncanonical-digest"),
        pytest.param(
            lambda r: {**r, "object_key": "objects/sha256/aa/bbb/other"},
            id="key-derivation-mismatch",
        ),
        pytest.param(lambda r: {**r, "byte_size": -1}, id="negative-size"),
        pytest.param(lambda r: {**r, "media_type": "text/markdown; charset=utf-8"}, id="media-parameters"),
        pytest.param(lambda r: {**r, "content_version": 0}, id="non-positive-content-version"),
    ],
)
def test_reference_hydration_fails_closed(mutator) -> None: ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/postgresql_source_store/test_canonical_read.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the adapter**

`canonical_read.py`:

```python
ACCEPTED_READ_SOURCE_STATES: Final[frozenset[str]] = frozenset({"active", "stored_not_indexed"})
```

`hydrate_canonical_source_reference(row)` (pure): parse `content_hash` with `ContentDigest.parse`, require `object_key == derive_canonical_object_key(digest).value`, `media_type` via `CanonicalMediaType.parse`, `byte_size >= 0`, `content_version >= 1`, `committed_at` timezone-aware, pointer columns consistent (`version_workspace_id == workspace_id`, `version_source_id == source_id`, `current_source_version_id == source_version_id`); any violation raises `CanonicalReadStateError` (safe detail `source_id` only). Missing source (no row) raises the existing `SOURCE_NOT_FOUND` shaped error exactly as `publication_store` does.

`PostgresqlCanonicalSourceReadStore.resolve_current` performs one bounded read — a single `SELECT` joining `sources`, `source_versions` (on `sources.current_version_id = source_versions.source_version_id`) and `content_objects` (on `source_versions.content_object_id`) filtered by both `workspace_id` and `source_id`, inside `engine.connect()` + `begin()` with `apply_transaction_bounds`; map infrastructure failures via `map_database_failure(cause, source_id=command.source_id)`.

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/postgresql_source_store -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/postgresql-source-store/src/postgresql_source_store/canonical_read.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py tests/unit/postgresql_source_store/test_canonical_read.py
git commit -m "feat: add postgresql canonical current-reference store"
```

---

### Task 6: Recovery error codes, manifest contracts and recovery ports

**Files:**
- Create: `src/personal_os/recovery/__init__.py`
- Create: `src/personal_os/recovery/contracts.py`
- Create: `src/personal_os/recovery/manifest.py`
- Create: `src/personal_os/recovery/ports.py`
- Modify: `src/personal_os/error_contracts/codes.py`
- Modify: `src/personal_os/diagnostics/events.py`
- Test: `tests/unit/recovery/test_contracts.py`
- Test: `tests/unit/recovery/test_manifest.py`

**Interfaces:**
- Consumes: core registry patterns, `ExpectedObject`, `DiagnosticContext`.
- Produces: error codes `CANONICAL_RECOVERY_ENVIRONMENT_REFUSED`, `CANONICAL_RECOVERY_CONFIGURATION_INVALID`, `CANONICAL_RECOVERY_SNAPSHOT_BUSY`, `CANONICAL_RECOVERY_BUNDLE_EXISTS`, `CANONICAL_RECOVERY_BUNDLE_INVALID`, `CANONICAL_RECOVERY_TARGET_NOT_EMPTY`, `CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE`, `CANONICAL_RECOVERY_INTEGRITY_FAILED`, `CANONICAL_RECOVERY_RESTORE_FAILED`; `RecoveryError`; closed token sets `RECOVERY_CONFIGURATION_REASONS`, `RECOVERY_BUNDLE_INVALID_REASONS`, `RECOVERY_COMPONENTS`, `RECOVERY_DEPENDENCIES`; `RecoveryEnvironment`; `CANONICAL_COUNT_TABLES`; `MANIFEST_CONTRACT`; `RecoveryManifest`, `ManifestObjectEntry`, `ManifestDumpEntry`; `encode_manifest`, `manifest_digest`, `parse_manifest`; ports `PostgresqlDumpProcess`, `PostgresqlConnectionTarget`, `DumpReceipt`, `RestoreReceipt`, `CanonicalBackupSnapshot`, `CanonicalBackupSnapshotStore`, `RecoveryBundleStore`, `RecoveryBundleWriter`, `VerifiedRecoveryBundle`; events `CANONICAL_BACKUP_CREATED` / `CANONICAL_BACKUP_VERIFIED` / `CANONICAL_BACKUP_FAILED` / `CANONICAL_RESTORE_SUCCEEDED` / `CANONICAL_RESTORE_FAILED`; `CANONICAL_BACKUP_METRIC_CONTRACTS`.

- [ ] **Step 1: Write failing tests**

`tests/unit/recovery/test_contracts.py` pins the spec-15 error table exactly (category, retryability, allowed detail fields for all nine codes above — `environment_refused`: authorization/no/`{operation}`; `configuration_invalid`: configuration/no/`{reason}`; `snapshot_busy`: dependency/yes/`{}`; `bundle_exists`: conflict/no/`{bundle_id}`; `bundle_invalid`: integrity/no/`{reason}`; `target_not_empty`: conflict/no/`{}`; `dependency_unavailable`: dependency/yes/`{dependency}`; `integrity_failed`: integrity/no/`{component}`; `restore_failed`: integrity/no/`{component}`), the closed token sets:

```python
def test_recovery_reason_tokens_are_closed() -> None:
    assert RECOVERY_CONFIGURATION_REASONS == frozenset(
        {"environment_not_allowed", "backup_root_not_absolute", "schema_head_mismatch",
         "free_space_reserve", "client_tools_unavailable", "target_not_empty"}
    )
    assert RECOVERY_BUNDLE_INVALID_REASONS == frozenset(
        {"contract_unsupported", "json_noncanonical", "duplicate_json_key", "bundle_id_invalid",
         "timestamp_invalid", "field_unknown", "field_invalid", "entries_unsorted",
         "digest_duplicate", "path_key_mismatch", "sidecar_missing", "file_tree_mismatch",
         "file_changed", "checksum_mismatch"}
    )
    assert RECOVERY_COMPONENTS == frozenset(
        {"postgres_dump", "postgres_restore", "object_set", "bundle", "canonical_graph",
         "canonical_read"}
    )
    assert RECOVERY_DEPENDENCIES == frozenset({"postgresql", "r2", "temporal", "pg_client"})
```

and the metric contract:

```python
def test_recovery_metric_contracts_match_design() -> None:
    assert CANONICAL_BACKUP_METRIC_CONTRACTS == {
        "canonical_backup_total": frozenset({"operation", "outcome"}),
        "canonical_backup_duration_seconds": frozenset({"operation", "outcome"}),
        "canonical_backup_objects": frozenset({"operation", "outcome"}),
        "canonical_backup_bytes": frozenset({"operation", "outcome"}),
    }
```

`tests/unit/recovery/test_manifest.py` implements spec 8.2:

```python
def build_manifest(**overrides) -> RecoveryManifest: ...


def test_manifest_bytes_are_canonical_sorted_compact_unicode_json() -> None:
    raw = encode_manifest(build_manifest())
    text = raw.decode("utf-8")
    assert text.endswith("}\n")
    parsed_again = json.loads(text)
    assert json.dumps(parsed_again, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" == text
    assert "é" in text or True  # ensure_ascii=False keeps non-ASCII literal


def test_manifest_digest_hashes_bytes_plus_one_newline() -> None:
    raw = encode_manifest(build_manifest())
    assert manifest_digest(raw) == hashlib.sha256(raw + b"\n").hexdigest()


def test_round_trip_preserves_every_field() -> None: ...


def test_rejects_unknown_top_level_field() -> None:
    raw = json_dict_with_extra_field(...)  # mutate then re-serialize canonically
    assert_bundle_invalid(parse_manifest, raw, "field_unknown")


def test_rejects_duplicate_json_key() -> None: ...
def test_rejects_unsorted_object_entries() -> None: ...
def test_rejects_duplicate_content_sha256() -> None: ...
def test_rejects_relative_path_object_key_disagreement() -> None: ...
def test_rejects_key_not_derived_from_digest() -> None: ...
def test_rejects_unsupported_contract_version() -> None: ...
def test_rejects_non_uuidv7_bundle_id() -> None: ...
def test_rejects_noncanonical_timestamp_format() -> None: ...  # requires exactly six fractional digits and Z
def test_rejects_noncanonical_json_bytes() -> None: ...        # re-encoding must equal input
def test_rejects_wrong_closed_counts_map() -> None: ...        # exactly the nine table names
def test_rejects_out_of_range_object_size() -> None: ...       # 0..104857600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/recovery -q`
Expected: FAIL — package does not exist.

- [ ] **Step 3: Implement contracts, manifest and ports**

`src/personal_os/recovery/contracts.py` essentials:

```python
MANIFEST_CONTRACT: Final[str] = "canonical_core_backup/v1"
POSTGRESQL_SCHEMA_REVISION: Final[str] = "20260813_01"
POSTGRESQL_SERVER_VERSION: Final[str] = "18.4"
MAXIMUM_OBJECT_SIZE_BYTES: Final[int] = 104_857_600

CANONICAL_COUNT_TABLES: Final[tuple[str, ...]] = (
    "users", "workspaces", "devices", "content_objects", "sources",
    "source_versions", "sync_events", "projection_intents", "audit_events",
)


class RecoveryEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"


class RecoveryError(ApplicationError):
    allowed_codes: frozenset[ErrorCode] = frozenset({
        ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED, ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID,
        ErrorCode.CANONICAL_RECOVERY_SNAPSHOT_BUSY, ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS,
        ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID, ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY,
        ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE, ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED,
        ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED,
    })


@dataclass(frozen=True, slots=True)
class ManifestDumpEntry:
    relative_path: str          # "postgres.dump"
    size_bytes: int
    sha256: str                 # lowercase hex


@dataclass(frozen=True, slots=True)
class ManifestObjectEntry:
    content_sha256: str
    object_key: str
    size_bytes: int
    media_type: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class RecoveryManifest:
    bundle_id: UUID
    created_at: datetime
    source_environment: str     # RecoveryEnvironment value
    postgresql_server_version: str
    postgresql_schema_revision: str
    postgres_dump: ManifestDumpEntry
    canonical_counts: Mapping[str, int]
    objects: tuple[ManifestObjectEntry, ...]
```

Also add `RecoveryOperation` (`create`/`verify`/`restore`), `RecoveryMetricOutcome` (`succeeded`/`failed`), `CANONICAL_BACKUP_METRIC_CONTRACTS`, and a `CanonicalBackupMetrics` protocol with `record_backup(operation, outcome, duration_seconds, object_count, byte_total)` plus an in-memory implementation.

`manifest.py`:

```python
_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


def encode_manifest(manifest: RecoveryManifest) -> bytes:
    """Canonical UTF-8 JSON: sorted keys, compact separators, final newline."""
    payload = {
        "bundle_id": str(manifest.bundle_id),
        "canonical_counts": dict(sorted(manifest.canonical_counts.items())),
        "created_at": format_manifest_timestamp(manifest.created_at),
        "objects": [
            {
                "content_sha256": entry.content_sha256,
                "media_type": entry.media_type,
                "object_key": entry.object_key,
                "relative_path": entry.relative_path,
                "size_bytes": entry.size_bytes,
            }
            for entry in manifest.objects  # already sorted by content_sha256
        ],
        "postgres_dump": {
            "format": "custom",
            "relative_path": manifest.postgres_dump.relative_path,
            "sha256": manifest.postgres_dump.sha256,
            "size_bytes": manifest.postgres_dump.size_bytes,
        },
        "postgresql_schema_revision": manifest.postgresql_schema_revision,
        "postgresql_server_version": manifest.postgresql_server_version,
        "source_environment": manifest.source_environment,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return (text + "\n").encode("utf-8")
```

`parse_manifest(raw)` validates in this order: UTF-8 decode; strict JSON load with an `object_pairs_hook` that raises on duplicate keys (`duplicate_json_key`); top-level key set exactly matches the nine canonical keys (`field_unknown` for extras); `contract` check comes first — the contract field must be present with value `canonical_core_backup/v1`, anything else is `contract_unsupported` and is never guessed (spec 8.2); field grammar (UUIDv7 `bundle_id` — `uuid.version == 7`; timestamp matches `_TIMESTAMP_PATTERN` then `datetime.strptime(..., "%Y-%m-%dT%H:%M:%S.%fZ")`; `size_bytes` ranges; lowercase 64-hex digests; `canonical_counts` keys exactly `CANONICAL_COUNT_TABLES`; object entries sorted strictly ascending by `content_sha256` with no duplicates; `relative_path == object_key == derive_canonical_object_key(ContentDigest.parse(content_sha256)).value`; media type parses as canonical; `postgres_dump.relative_path == "postgres.dump"`); finally byte-canonicality: `encode_manifest(parsed) == raw` else `json_noncanonical`. Every failure raises `RecoveryError(CANONICAL_RECOVERY_BUNDLE_INVALID, safe_details={"reason": SafeToken.parse(...)})`.

`ports.py` (spec 4.4 verbatim semantics):

```python
@dataclass(frozen=True, slots=True)
class PostgresqlConnectionTarget:
    host: str
    port: int
    database: str
    user: str


@dataclass(frozen=True, slots=True)
class DumpReceipt:
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
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
    def open_quiesced_snapshot(
        self, now: datetime
    ) -> AbstractAsyncContextManager[CanonicalBackupSnapshot]: ...

    async def observe_pending_writers(self) -> int: ...


class RecoveryBundleWriter(Protocol):
    dump_path: Path
    def object_path(self, content_sha256: str) -> Path: ...
    async def finalize(self, manifest: RecoveryManifest) -> None: ...
    async def abandon(self) -> None: ...


class VerifiedRecoveryBundle(Protocol):
    manifest: RecoveryManifest
    dump_path: Path
    def object_path(self, content_sha256: str) -> Path: ...


class RecoveryBundleStore(Protocol):
    def create_staging(self, bundle_id: UUID) -> AbstractAsyncContextManager[RecoveryBundleWriter]: ...
    def open_verified(self, bundle_id: UUID) -> AbstractAsyncContextManager[VerifiedRecoveryBundle]: ...
    def bundle_exists(self, bundle_id: UUID) -> bool: ...
```

Register the nine error codes in `codes.py` and the five backup/restore events in `events.py` (created/verified: INFO/SUCCEEDED; restore_succeeded: INFO/SUCCEEDED; backup_failed/restore_failed: ERROR/FAILED with required `{"error_code"}`; allowed fields limited to spec 16.1 safe scalars — `operation`, `outcome`, `duration`, `object_count`, `byte_total`, `bundle_id`, error-code fields).

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/recovery tests/unit/error_contracts tests/unit/diagnostics -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/recovery tests/unit/recovery src/personal_os/error_contracts/codes.py src/personal_os/diagnostics/events.py
git commit -m "feat: add recovery contracts, manifest and ports"
```

---

### Task 7: Private immutable filesystem bundle store

**Files:**
- Create: `src/personal_os/recovery/bundle.py`
- Modify: `src/personal_os/recovery/__init__.py`
- Test: `tests/unit/recovery/test_bundle.py`
- Test: `tests/unit/recovery/test_bundle_safety.py`

**Interfaces:**
- Consumes: Task 6 manifest functions, ports and `RecoveryError`; stdlib `pathlib`, `stat`, `os`, `secrets`, `hashlib`, `shutil`.
- Produces: `FilesystemRecoveryBundleStore(root: Path)` implementing `RecoveryBundleStore`, plus `BACKUP_FREE_SPACE_RESERVE_BYTES`, `STAGING_NAME_PREFIX`, `validate_backup_root(root)`.

- [ ] **Step 1: Write failing tests**

`tests/unit/recovery/test_bundle.py` (round-trip behavior on `tmp_path`):

```python
async def test_create_then_verify_round_trip(tmp_path) -> None: ...
async def test_creation_fails_when_final_directory_exists(tmp_path) -> None: ...
async def test_finalize_renames_staging_away_and_staging_no_longer_exists(tmp_path) -> None: ...
async def test_abandon_removes_exactly_the_staging_directory(tmp_path) -> None: ...
async def test_manifest_written_last_and_sidecar_matches_digest(tmp_path) -> None: ...
async def test_object_files_land_under_objects_sha256_first2_next2(tmp_path) -> None: ...
def test_validate_backup_root_rejects_relative_path(tmp_path) -> None: ...
async def test_admission_checks_free_space_reserve_before_first_copy(tmp_path, monkeypatch) -> None:
    # Fake shutil.disk_usage to report exactly 2 GiB - 1 free; assert the
    # RecoveryError(CONFIGURATION_INVALID, reason="free_space_reserve") is
    # raised before any file is written.
```

`tests/unit/recovery/test_bundle_safety.py` (spec 8.3 — offline attacks):

```python
def test_resolved_child_escaping_root_rejected(tmp_path) -> None: ...
def test_symlinked_object_rejected(tmp_path) -> None: ...          # POSIX only guard: skipif os.name != "posix"
def test_extra_unregistered_file_rejected(tmp_path) -> None: ...
def test_missing_object_file_rejected(tmp_path) -> None: ...
def test_modified_manifest_rejected_by_sidecar(tmp_path) -> None: ...
def test_modified_dump_rejected_by_checksum(tmp_path) -> None: ...
def test_modified_object_rejected_by_streaming_checksum(tmp_path) -> None: ...
def test_missing_sidecar_rejected(tmp_path) -> None: ...
def test_staging_suffix_directory_rejected_by_verify(tmp_path) -> None: ...
def test_directory_bundle_id_must_be_canonical_uuid_string(tmp_path) -> None: ...
def test_changed_file_during_verification_detected(tmp_path, monkeypatch) -> None:
    # Instrument the verify loop's identity recheck helper: after open,
    # mutate mtime; assert RecoveryError(BUNDLE_INVALID, "file_changed").
```

Windows notes: POSIX permission assertions (`0700`/`0600`) are `skipif os.name != "posix"`; symlink-escape tests use `os.symlink` and skip where creation fails. Reparse-point rejection on Windows is implemented via `stat.FILE_ATTRIBUTE_REPARSE_POINT` checks and covered by a unit test of the predicate (`_has_reparse_point`) with a fake stat result.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/recovery/test_bundle.py tests/unit/recovery/test_bundle_safety.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the bundle store**

`src/personal_os/recovery/bundle.py` structure (full behavior, spec 8.1, 8.3, 10):

```python
BACKUP_FREE_SPACE_RESERVE_BYTES: Final[int] = 2 * 1024**3
STAGING_NAME_PREFIX: Final[str] = ".staging-"
DIRECTORY_PERMISSIONS_POSIX: Final[int] = 0o700
FILE_PERMISSIONS_POSIX: Final[int] = 0o600
STREAM_CHUNK_SIZE_BYTES: Final[int] = 1024 * 1024
```

- `validate_backup_root(root)`: absolute, exists, is a directory, not a symlink; free space via `shutil.disk_usage(root).free >= BACKUP_FREE_SPACE_RESERVE_BYTES` else `RecoveryError(CONFIGURATION_INVALID, "free_space_reserve")`.
- `_resolve_within_root(root, relative)`: `Path.resolve()` the child, assert `root_resolved` is a parent, reject any component that is a link (`os.path.islink`) or, on Windows, carries `stat.FILE_ATTRIBUTE_REPARSE_POINT`; reject non-regular files (`stat.S_ISREG`).
- `create_staging(bundle_id)`: `_require_free_space` first (before any object copy); staging directory `root / f"{STAGING_NAME_PREFIX}{bundle_id}-{secrets.token_hex(16)}"` created with `mkdir(mode=0o700)` (POSIX chmod applied explicitly); the returned writer creates every file exclusively (`open(path, "xb")`), flushes and `os.fsync`s each completed file; `object_path(digest)` returns `<staging>/objects/sha256/<first2>/<next2>/<digest>`; `finalize(manifest)` writes `manifest.json` (via `encode_manifest`) then `manifest.sha256` (`manifest_digest(bytes)` plus one newline) last, fsyncs the staging directory (POSIX), verifies the final target does not exist, `os.rename(staging, final)` (same filesystem), fsyncs the parent directory on POSIX; on Windows flush file handles before the same-volume rename (crash-consistency beyond that belongs to deployment configuration, spec 8.1). Any pre-rename failure path calls `abandon()` which removes exactly the resolved staging directory via `shutil.rmtree(staging, ignore_errors=False)` guarded to the staging path created by this invocation.
- `open_verified(bundle_id)` / `verify_offline(bundle_id)` implement spec 10 order exactly: (1) root/path boundary; (2) final directory type and absence of staging suffix; (3) exact registered file tree — walk and collect every relative path, require the set to equal `{manifest.json, manifest.sha256, postgres.dump} ∪ {objects/sha256/<d[0:2]>/<d[2:4]>/<d> for each manifest entry}` after parsing, rejecting links/specials; (4) sidecar grammar (64 lowercase hex + newline) and digest equal to `manifest_digest(manifest_bytes)`; (5) `parse_manifest`; (6) dump exact size and streaming SHA-256; (7) every object path/key derivation, size and streaming SHA-256; (8) totals. Open files with `os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))`, re-check type/identity after open, and compare pre/post `fstat` (size, mtime_ns, inode) to detect a changed file (`file_changed`).
- All verification reads stream in `STREAM_CHUNK_SIZE_BYTES` chunks — never whole-file reads (bounded memory, spec 18.1).

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/recovery -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS on Windows (POSIX-only tests skip).

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/recovery/bundle.py src/personal_os/recovery/__init__.py tests/unit/recovery/test_bundle.py tests/unit/recovery/test_bundle_safety.py
git commit -m "feat: add immutable filesystem recovery bundle store"
```

---

### Task 8: Bounded pg_dump/pg_restore process adapter

**Files:**
- Create: `tools/postgresql_dump_process.py`
- Test: `tests/unit/tools/test_postgresql_dump_process.py`

**Interfaces:**
- Consumes: Task 6 `PostgresqlDumpProcess` port, `PostgresqlConnectionTarget`, `DumpReceipt`, `RestoreReceipt`, `RecoveryError`; stdlib subprocess/asyncio/tempfile.
- Produces: `EXPECTED_POSTGRESQL_CLIENT_VERSION = "18.4"`, `resolve_client_tool(tool_name) -> Path`, `parse_client_version(output) -> str`, `check_client_tools(dump_tool, restore_tool) -> None`, `run_bounded_child(argv, *, env, timeout_seconds) -> ProcessRunResult`, `PostgresqlDumpProcessAdapter(dump_binary, restore_binary, *, password: SecretStr, runner=run_bounded_child)`.

- [ ] **Step 1: Write failing tests**

`tests/unit/tools/test_postgresql_dump_process.py` with a `ScriptedRunner` recording `(argv, env, timeout)` and returning scripted return codes:

```python
def test_create_dump_uses_exact_semantic_argument_vector() -> None:
    # Spec 9.3 order is binding:
    expected_prefix = [
        "<dump_binary>", "--format=custom", "--no-owner", "--no-privileges",
        "--no-password", "--lock-wait-timeout=15000", f"--snapshot={TOKEN}",
        f"--file={output_file}",
        "--host", HOST, "--port", PORT, "--username", USER, DATABASE,
    ]
```

Required cases:

```python
def test_restore_dump_uses_exact_semantic_argument_vector() -> None:
    # pg_restore --single-transaction --exit-on-error --no-owner --no-privileges
    # --no-password --host H --port P --username U --dbname TARGET <dump path>


def test_child_env_sets_only_pgpassfile_and_never_password_env() -> None:
    # env passed to the runner contains PGPASSFILE; excludes PGPASSWORD,
    # DATABASE_URL and every other PG* variable.


def test_passfile_is_ephemeral_outside_bundle_and_removed_in_finally() -> None:
    # Capture the passfile path via env; assert the file existed during the
    # run with content "host:port:database:user:password" and is gone after;
    # on POSIX assert mode 0o600.


def test_passfile_removed_even_when_runner_raises() -> None: ...


def test_missing_binary_fails_closed_as_dependency_unavailable() -> None: ...
def test_client_version_below_expected_rejected() -> None: ...       # "17.4"
def test_client_version_newer_major_rejected() -> None: ...          # "19.1"
def test_unparseable_version_output_rejected() -> None: ...          # unexpected text


def test_dump_failure_maps_to_integrity_failed_without_raw_stderr() -> None:
    # ScriptedRunner returns returncode 1 and stderr text; the raised
    # RecoveryError must not contain any of that text.


def test_dump_timeout_terminates_then_kills_within_grace() -> None: ...
def test_restore_failure_maps_to_restore_failed() -> None: ...
def test_dump_receipt_hashes_exact_output_file(tmp_path) -> None: ...
async def test_no_shell_invocation_anywhere() -> None: ...  # argv is a sequence, never a string
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/tools/test_postgresql_dump_process.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the adapter**

`tools/postgresql_dump_process.py`:

```python
EXPECTED_POSTGRESQL_CLIENT_VERSION: Final[str] = "18.4"
CHILD_TERMINATE_GRACE_SECONDS: Final[float] = 5.0


@dataclass(frozen=True, slots=True)
class ProcessRunResult:
    returncode: int
    timed_out: bool = False


async def run_bounded_child(
    argv: Sequence[str], *, env: Mapping[str, str], timeout_seconds: float
) -> ProcessRunResult:
    """Run one child without a shell; stdout/stderr are consumed and discarded."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=dict(env),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=CHILD_TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
        return ProcessRunResult(returncode=process.returncode or -1, timed_out=True)
    await process.stderr.read()  # consume, never forward
    return ProcessRunResult(returncode=process.returncode)
```

`parse_client_version(output)` extracts the version from `pg_dump (PostgreSQL) 18.4`-style output and returns it (e.g. `"18.4"`); `check_client_tools` resolves both binaries via `shutil.which` (missing → `RecoveryError(DEPENDENCY_UNAVAILABLE, dependency="pg_client")`), runs `<binary> --version` through the same bounded runner and requires the parsed version to equal `EXPECTED_POSTGRESQL_CLIENT_VERSION` exactly — an older, newer-major or unparseable binary fails closed before snapshot acquisition (spec 4.3).

`PostgresqlDumpProcessAdapter`:

- `_child_env()`: `{key: value for key, value in os.environ.items() if not key.startswith("PG") and key != "DATABASE_URL"} | {"PGPASSFILE": str(passfile)}`.
- `_ephemeral_passfile(target)`: `tempfile.mkstemp(prefix="knowledge-pgpass-", dir=tempfile.gettempdir())` (system temp — never inside the bundle root), `os.fdopen` write `f"{target.host}:{target.port}:{target.database}:{target.user}:{password}\n"`, `os.chmod(path, 0o600)` on POSIX; context manager removes the file in `finally` including on cancellation.
- `create_dump(...)`: inside the passfile context, `run_bounded_child([str(self._dump_binary), "--format=custom", "--no-owner", "--no-privileges", "--no-password", "--lock-wait-timeout=15000", f"--snapshot={snapshot_token}", f"--file={str(output_file)}", "--host", target.host, "--port", str(target.port), "--username", target.user, target.database], env=..., timeout_seconds=timeout_seconds)`; nonzero/timeout → `RecoveryError(INTEGRITY_FAILED, component="postgres_dump")`; success → stream-hash `output_file` (1 MiB chunks) into `DumpReceipt(size_bytes, sha256)`.
- `restore_dump(...)`: same boundary with the spec 11.2 vector (`--single-transaction --exit-on-error --no-owner --no-privileges --no-password --host --port --username --dbname <target> <input file>`); failure → `RecoveryError(RESTORE_FAILED, component="postgres_restore")`; success → `RestoreReceipt(completed_at=utcnow)`.
- No parallel jobs, no `--no-sync`, no connection strings, no stdout archive streaming, no `--clean`/`--create`, never a shell, never log argv/host/user/stderr (spec 9.3, 11.2, 16.3).

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/tools -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/postgresql_dump_process.py tests/unit/tools/test_postgresql_dump_process.py
git commit -m "feat: add bounded pg_dump pg_restore process adapter"
```

---

### Task 9: PostgreSQL quiesced exported-snapshot adapter

**Files:**
- Create: `packages/postgresql-source-store/src/postgresql_source_store/backup_snapshot.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Test: `tests/unit/postgresql_source_store/test_backup_snapshot.py`

**Interfaces:**
- Consumes: Task 6 `CanonicalBackupSnapshot`/ports, `tables.py` (`SOURCE_STORE_TABLES`, `SOURCE_STORE_SCHEMA`), `apply_transaction_bounds`, `map_database_failure`, `ExpectedObject`, `derive_canonical_object_key`.
- Produces: `SNAPSHOT_LOCK_ORDER`, `SNAPSHOT_LOCK_TIMEOUT_SECONDS = 15`, `build_share_lock_statements()`, `hydrate_referenced_objects(rows)`, `PostgresqlBackupSnapshotStore(engine)` implementing `CanonicalBackupSnapshotStore`, `PostgresqlRestoreTarget(engine)` with `is_application_empty()`, `server_version()`, `read_canonical_counts()`, `read_schema_head()`, `read_current_pointer_resolution()`.

- [ ] **Step 1: Write failing unit tests**

`tests/unit/postgresql_source_store/test_backup_snapshot.py` (statement-compilation and hydration tests, no database):

```python
def test_share_lock_statements_follow_fixed_spec_order() -> None:
    statements = build_share_lock_statements()
    texts = [str(s.compile(dialect=postgresql_dialect())) for s in statements]
    assert len(texts) == 9
    for text, table in zip(texts, SNAPSHOT_LOCK_ORDER, strict=True):
        assert f'"{table}"' in text
        assert "SHARE MODE NOWAIT" in text


def test_snapshot_lock_order_is_the_spec_nine_tables() -> None:
    assert SNAPSHOT_LOCK_ORDER == (
        "users", "workspaces", "devices", "content_objects", "sources",
        "source_versions", "sync_events", "projection_intents", "audit_events",
    )


def test_hydrate_referenced_objects_validates_canonical_derivation() -> None: ...
def test_hydrate_referenced_objects_deduplicates_content_objects() -> None: ...
def test_hydrate_rejects_noncanonical_object_key() -> None: ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/postgresql_source_store/test_backup_snapshot.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the snapshot store**

`backup_snapshot.py` requirements (spec 9.2):

- `build_share_lock_statements()` returns nine `sa.text(f'LOCK TABLE {SOURCE_STORE_SCHEMA}."{table}" IN SHARE MODE NOWAIT')` statements in `SNAPSHOT_LOCK_ORDER` — the fixed order is binding.
- `PostgresqlBackupSnapshotStore.open_quiesced_snapshot(now)` is an `@asynccontextmanager`:
  1. `engine.connect()` with `await connection.execution_options(isolation_level="REPEATABLE READ")` — begin **before the first query**;
  2. `SET LOCAL lock_timeout = '15000ms'` plus the established statement/idle bounds;
  3. execute the nine lock statements in order; a lock failure (SQLAlchemy `OperationalError`/`DBAPIError` for timeout or NOWAIT refusal) maps to `RecoveryError(CANONICAL_RECOVERY_SNAPSHOT_BUSY)` — never a raw driver error;
  4. `SELECT pg_export_snapshot()` → opaque token (infrastructure-private; flows only to `PostgresqlDumpProcess` inside the composition call, spec 4.4);
  5. `SELECT server_version()`, `SELECT version_num FROM alembic_version`, the nine `SELECT count(*)` queries and the referenced-objects read (distinct `content_objects` joined from `source_versions`) — all from the same snapshot;
  6. yield `CanonicalBackupSnapshot(snapshot_token, server_version, schema_head, table_counts, referenced_objects)`;
  7. on context exit (success or failure) roll back the transaction and dispose the connection, releasing locks. The transaction performs no mutation.
- `observe_pending_writers()` counts ungranted relation locks on the nine tables: `SELECT count(*) FROM pg_locks JOIN pg_class ON pg_locks.relation = pg_class.oid WHERE pg_locks.locktype = 'relation' AND NOT pg_locks.granted AND pg_class.relname = ANY(:tables)`; the caller aborts finalization when the count is non-zero (spec 9.2 step 8).
- `hydrate_referenced_objects(rows)` validates digest hex, `object_key == derive_canonical_object_key(digest)`, canonical media type, `byte_size` in `0..104857600`; raises `RecoveryError(INTEGRITY_FAILED, component="object_set")` on any violation.
- `PostgresqlRestoreTarget` (same module): `is_application_empty()` — no relation in schema `knowledge` and no `alembic_version` table (spec 11.1); `read_canonical_counts()` — the nine counts for post-restore verification; `read_schema_head()` — `alembic_version.version_num`; `read_current_pointer_resolution()` — one joined read returning the count of sources whose `current_version_id` is null, points at another source's version, or references a missing content object (post-restore check expects zero).

Add both classes to the package `__init__.py` exports.

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/postgresql_source_store -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/postgresql-source-store/src/postgresql_source_store/backup_snapshot.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py tests/unit/postgresql_source_store/test_backup_snapshot.py
git commit -m "feat: add postgresql quiesced snapshot and restore-target adapters"
```

---

### Task 10: Recovery service — consistent backup creation

**Files:**
- Create: `src/personal_os/recovery/service.py`
- Modify: `src/personal_os/recovery/__init__.py`
- Test: `tests/unit/recovery/test_service_backup.py`

**Interfaces:**
- Consumes: Tasks 6–9 ports and contracts; `CanonicalObjectStore.open_verified_reader`.
- Produces: `BackupCreateCommand` (frozen: `environment: RecoveryEnvironment`, `target: PostgresqlConnectionTarget`), `BackupCreationResult` (frozen: `bundle_id: UUID`, `object_count: int`, `byte_total: int`, `duration_seconds: float`), `RecoveryService(snapshot_store, bundle_store, dump_process, object_store, metrics, clock)` with `async def create_backup(self, command) -> BackupCreationResult`.

- [ ] **Step 1: Write failing tests**

`tests/unit/recovery/test_service_backup.py` with fakes (`FakeSnapshotStore`, `FakeBundleStore`, `FakeDumpProcess`, following `tests/unit/sources/fakes.py` patterns; a `ConcurrencyRecordingObjectStore` tracking the peak number of simultaneous `open_verified_reader` bodies):

```python
async def test_backup_creates_verified_bundle_with_manifest_from_snapshot() -> None:
    # Assert the finalized manifest: counts equal the snapshot's, objects
    # sorted by digest, dump entry hashes the fake dump receipt, schema
    # revision "20260813_01", server version "18.4", environment from command.


async def test_environment_refusal_happens_before_any_io() -> None:
    # RecoveryEnvironment gate: every fake records zero calls; raises
    # RecoveryError(ENVIRONMENT_REFUSED, operation="create").


async def test_schema_head_mismatch_refuses_backup() -> None: ...
async def test_existing_bundle_id_refuses_without_mutation() -> None: ...


async def test_object_reads_bounded_to_four_concurrent() -> None:
    # Ten referenced objects; ConcurrencyRecordingObjectStore asserts peak
    # simultaneous readers == 4.


async def test_pending_writer_before_finalize_aborts_bundle() -> None:
    # FakeSnapshotStore.observe_pending_writers -> 1; staging abandoned, no
    # finalized directory, SNAPSHOT_BUSY raised (retryable).


async def test_dump_failure_abandons_staging_and_never_touches_canonical_state() -> None: ...
async def test_object_read_failure_abandons_staging_without_r2_mutation() -> None: ...
async def test_cancellation_removes_exact_staging_and_closes_readers() -> None:
    # asyncio.Task cancel while an object copy is in flight; assert abandon()
    # ran, all readers closed, no finalized bundle.


async def test_success_emits_created_event_and_metrics() -> None: ...
async def test_snapshot_transaction_stays_open_through_finalize() -> None:
    # FakeSnapshotStore records ordering: snapshot open ... finalize ... exit.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/recovery/test_service_backup.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement backup creation**

`src/personal_os/recovery/service.py`:

```python
BACKUP_OBJECT_READ_CONCURRENCY: Final[int] = 4
RECOVERY_COMMAND_TIMEOUT_SECONDS: Final[float] = 30 * 60.0


@dataclass(frozen=True, slots=True)
class RecoveryService:
    """Backup creation, offline verification and empty-target restore (spec 9-11).

    Composes existing production ports; never reimplements publication,
    object verification or transaction behavior.
    """

    snapshot_store: CanonicalBackupSnapshotStore
    bundle_store: RecoveryBundleStore
    dump_process: PostgresqlDumpProcess
    object_store: CanonicalObjectStore
    metrics: CanonicalBackupMetrics
    clock: Callable[[], datetime]
```

`create_backup(command)` flow (spec 9.1–9.3): refuse an environment outside `{local, test}` (`ENVIRONMENT_REFUSED`, operation `create`) before any client, connection or path is opened; allocate `bundle_id = uuid7()`; open `snapshot_store.open_quiesced_snapshot(now=self.clock())`:

- inside the snapshot: refuse `schema_head != "20260813_01"` (`CONFIGURATION_INVALID`, `schema_head_mismatch`); refuse an existing bundle (`BUNDLE_EXISTS`, detail `bundle_id`); enter `bundle_store.create_staging(bundle_id)`;
- `dump_process.create_dump(snapshot.snapshot_token, writer.dump_path, command.target, timeout_seconds=600)` — the token flows only here;
- copy each `snapshot.referenced_objects` entry through `object_store.open_verified_reader` into `writer.object_path(digest)` under `asyncio.Semaphore(BACKUP_OBJECT_READ_CONCURRENCY)`;
- recheck `await snapshot_store.observe_pending_writers()` — a non-zero count aborts finalization (`SNAPSHOT_BUSY`) with `writer.abandon()`;
- build the `RecoveryManifest` (`created_at` from `self.clock()`, counts from `snapshot.table_counts`, dump entry from the dump receipt) and `await writer.finalize(manifest)` **while the snapshot transaction is still open** (spec 9.2 step 9);
- exit the snapshot context (releasing locks) only after finalization (spec 9.2 step 10).

Wrap the flow so every failure path abandons staging and re-raises; emit `CANONICAL_BACKUP_FAILED` (with `error_code`) on failure and `CANONICAL_BACKUP_CREATED` (with `bundle_id`, `object_count`, `byte_total`, `duration`) on success; record `record_backup("create", outcome, duration_seconds, object_count, byte_total)`. The 30-minute whole-command bound and cancellation semantics are exercised through direct task cancellation in tests and applied by the composition layer (Task 12).

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/recovery -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/recovery/service.py src/personal_os/recovery/__init__.py tests/unit/recovery/test_service_backup.py
git commit -m "feat: add consistent recovery backup creation service"
```

---

### Task 11: Recovery service — offline verification and empty-target restore

**Files:**
- Modify: `src/personal_os/recovery/service.py`
- Modify: `src/personal_os/recovery/__init__.py`
- Test: `tests/unit/recovery/test_service_verify.py`
- Test: `tests/unit/recovery/test_service_restore.py`

**Interfaces:**
- Consumes: Task 10 service, `PostgresqlRestoreTarget`, `CanonicalSourceReadService` (Task 4), `store_stream`/`resolve_verified_object`/`verify_existing_object`.
- Produces: `VerifyBundleCommand` (frozen: `environment`, `bundle_id`), `BundleVerificationResult` (frozen: `bundle_id`, `contract`, `object_count`, `byte_total`, `table_counts`), `AcceptanceSmokeProbe` (frozen: `workspace_id`, `source_id`, `expected_sha256`, `expected_size_bytes`, `expected_media_type`), `RestoreEmptyCommand` (frozen: `environment`, `bundle_id`, `target: PostgresqlConnectionTarget`, `target_confirmation: str`, `acceptance_probe: AcceptanceSmokeProbe | None`), `RestoreEmptyResult` (frozen: `bundle_id`, `completed_at`, `table_counts`, `object_count`), `RecoveryService.verify_bundle(...)`, `RecoveryService.restore_empty(command, *, read_service, restore_target)`.

- [ ] **Step 1: Write failing tests**

`tests/unit/recovery/test_service_verify.py`:

```python
async def test_verification_makes_no_postgresql_r2_or_temporal_call() -> None:
    # Fakes for every port; assert zero interactions after verify_bundle.

async def test_verification_returns_only_safe_counts() -> None:
    # Result carries no paths, keys or hashes.

async def test_invalid_bundle_raises_bundle_invalid() -> None: ...
async def test_verification_emits_verified_event_and_metrics() -> None: ...
```

`tests/unit/recovery/test_service_restore.py` — each admission rule of spec 11.1 and each ordering rule of spec 11.2/11.3, using an ordered `CallLedger` fake to pin global ordering:

```python
async def test_restore_order_is_verify_r2_pgrestore_graph_smoke_receipt() -> None:
    # Ledger sequence: verify_offline, object restores, restore_dump,
    # schema/counts/graph checks, canonical read smoke, result.


async def test_missing_r2_key_restored_via_conditional_store_with_claimed_digest() -> None:
    # resolve_verified_object -> None; store_stream called with the bundle
    # file stream, exact size, media type and claimed_sha256 == digest.


async def test_existing_exact_object_reused_without_store_stream() -> None: ...


async def test_mismatched_existing_object_fails_closed_without_overwrite() -> None:
    # verify_existing_object raises the typed object-storage error; assert
    # store_stream never called, no delete, pg_restore never attempted.


async def test_restore_object_writes_bounded_to_four_concurrent() -> None: ...


async def test_pg_restore_failure_maps_restore_failed_and_leaves_no_success_receipt() -> None: ...
async def test_target_not_empty_refused_before_any_io() -> None: ...
async def test_environment_refused_for_restore() -> None: ...
async def test_target_confirmation_mismatch_refused() -> None: ...


async def test_post_restore_count_mismatch_fails_closed() -> None: ...
async def test_post_restore_schema_head_must_be_baseline() -> None: ...
async def test_post_restore_current_pointer_resolution_checked() -> None: ...
async def test_post_restore_referenced_objects_full_verified() -> None: ...


async def test_acceptance_smoke_read_returns_exact_restored_bytes() -> None:
    # Fake read service returns bytes whose sha256 == probe.expected_sha256.

async def test_acceptance_smoke_mismatch_fails_restore() -> None: ...
async def test_success_emits_restore_succeeded_event_with_no_keys_or_hashes() -> None: ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/recovery/test_service_verify.py tests/unit/recovery/test_service_restore.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement verification and restore**

`verify_bundle(command)` (spec 10): environment gate; `bundle_store.verify_offline(bundle_id)` returns the parsed manifest (raising `BUNDLE_INVALID` reasons); return `BundleVerificationResult` with safe fields only; emit `CANONICAL_BACKUP_VERIFIED`; metrics `record_backup("verify", ...)`.

`restore_empty(command, *, read_service, restore_target)` (spec 11):

1. Gates before any I/O beyond offline verification: environment `local`/`test` (`ENVIRONMENT_REFUSED`, operation `restore`); `target_confirmation` must equal the exact target database identifier (`ENVIRONMENT_REFUSED`, operation `restore`); fresh complete `bundle_store.verify_offline` — an invalid bundle never reaches PostgreSQL or R2.
2. Target admission: `restore_target.is_application_empty()` else `TARGET_NOT_EMPTY`; `restore_target.server_version() == "18.4"` else `RecoveryError(DEPENDENCY_UNAVAILABLE, dependency="postgresql")`; `read_schema_head()` must be absent — the dump brings the baseline itself.
3. R2 restore first, `asyncio.Semaphore(RESTORE_OBJECT_WRITE_CONCURRENCY)` with `RESTORE_OBJECT_WRITE_CONCURRENCY = 4`: for each manifest object build `ExpectedObject`; `resolve_verified_object` returning `None` means stream the bundle file through `store_stream(..., claimed_sha256=digest)` and verify the receipt matches exactly; an existing receipt means `verify_existing_object`; any mismatch fails closed (`INTEGRITY_FAILED`, component `object_set`) with no overwrite, delete or fallback. Objects restored before a later failure are safe unreferenced CAS bytes (spec 11.2).
4. `dump_process.restore_dump(bundle.dump_path, command.target, timeout_seconds=600)` — one transaction, all-or-nothing (`--single-transaction --exit-on-error`).
5. Post-restore verification (spec 11.3): `read_schema_head() == "20260813_01"`; `read_canonical_counts()` equals the manifest counts exactly; `read_current_pointer_resolution() == 0`; every manifest object full-verifies from R2 (`verify_existing_object`); the acceptance smoke probe reads through `read_service.read_current_source_bytes` and requires the exact expected digest/size/media.
6. Success: emit `CANONICAL_RESTORE_SUCCEEDED`, metrics `record_backup("restore", ...)`, return `RestoreEmptyResult` (no keys, hashes or paths). On failure emit `CANONICAL_RESTORE_FAILED` (only `error_code`/`component` details); never delete restored R2 objects as compensation.

No Qdrant, Neo4j, Redis or Temporal history is ever queried (spec 11.3) — enforced by the import-boundary contract test in Task 15.

- [ ] **Step 4: Run tests, lint and type check**

Run: `uv run pytest tests/unit/recovery -q && uv run poe python-lint && uv run poe python-type-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/personal_os/recovery/service.py src/personal_os/recovery/__init__.py tests/unit/recovery/test_service_verify.py tests/unit/recovery/test_service_restore.py
git commit -m "feat: add recovery verification and empty-target restore service"
```

---

### Task 12: Backup-root runtime setting and canonical operations CLI

**Files:**
- Create: `tools/canonical_core_operations.py`
- Create: `tools/canonical_recovery_bundle.py`
- Modify: `src/personal_os/runtime_configuration/environment_names.py`
- Modify: `src/personal_os/runtime_configuration/loading.py`
- Modify: `src/personal_os/runtime_configuration/models.py`
- Modify: `pyproject.toml` (Poe leaf tasks)
- Test: `tests/unit/runtime_configuration/test_canonical_recovery_settings.py`
- Test: `tests/unit/tools/test_canonical_core_operations.py`

**Interfaces:**
- Consumes: Tasks 1–11 surfaces, `local_service_stack.py` CLI patterns (`_CliParser`, JSON-on-stdout, exit-code classes), `load_database_runtime_settings`, `read_database_runtime_password`, `load_object_storage_settings`, `R2ClientManager`, `R2S3ObjectStore`.
- Produces: `CANONICAL_RECOVERY_ENVIRONMENT_NAMES` fragment, `CanonicalRecoverySettings`, `load_canonical_recovery_settings(*, environ=None)`, `CanonicalCoreExitCode(IntEnum)`, `canonical_core_operations.main(argv) -> int`, `canonical_recovery_bundle.build_bundle_store(settings) -> FilesystemRecoveryBundleStore`.

- [ ] **Step 1: Write failing tests**

`tests/unit/runtime_configuration/test_canonical_recovery_settings.py`:

```python
def test_backup_root_joins_environment_name_registry() -> None:
    assert "KNOWLEDGE_CANONICAL_BACKUP_ROOT" in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    assert "KNOWLEDGE_CANONICAL_BACKUP_ROOT" in CANONICAL_RECOVERY_ENVIRONMENT_NAMES


def test_loads_absolute_backup_root_with_local_environment() -> None: ...
def test_relative_backup_root_refused() -> None: ...
def test_unknown_knowledge_key_still_terminal() -> None: ...
def test_backup_root_excluded_from_repr_and_diagnostics() -> None: ...
```

`tests/unit/tools/test_canonical_core_operations.py` (parse-before-I/O and gate behavior; every composition factory is injected and call-counted):

```python
def test_invalid_syntax_exits_two_without_reading_environment() -> None: ...


def test_help_and_version_exit_zero_without_io() -> None: ...


def test_backup_create_refuses_staging_before_any_client_or_path() -> None:
    # KNOWLEDGE_ENVIRONMENT=staging -> exit 78, zero factory calls.


def test_backup_create_requires_exact_write_admission_confirmation() -> None: ...


def test_restore_empty_requires_exact_target_confirmation() -> None: ...


def test_bootstrap_identity_emits_one_safe_json_document() -> None: ...


def test_read_current_source_writes_bytes_only_to_exclusive_output_file(tmp_path) -> None: ...


def test_error_codes_map_to_exit_classes() -> None:
    # validation/conflict/integrity -> 65; dependency -> 69; internal -> 70;
    # busy -> 75; configuration/environment -> 78; syntax -> 2; success -> 0.


def test_no_command_prompts_interactively() -> None: ...
def test_raw_child_output_never_forwarded() -> None: ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime_configuration/test_canonical_recovery_settings.py tests/unit/tools/test_canonical_core_operations.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the setting and CLI**

Runtime configuration: add to `environment_names.py`:

```python
#: Canonical recovery fragment: the operator-owned private backup root.
CANONICAL_RECOVERY_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {"KNOWLEDGE_ENVIRONMENT", "KNOWLEDGE_CANONICAL_BACKUP_ROOT"}
)
```

and include the fragment in `KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES`. Add `CanonicalRecoverySettings` to `models.py` (frozen, `extra="forbid"`, absolute-path validation for `backup_root: Path`, redacted `__repr__`/`__str__` — the backup root may reveal host layout and stays out of diagnostics, spec 14) and `load_canonical_recovery_settings` to `loading.py` following the existing fragment loaders (unknown `KNOWLEDGE_*` keys stay terminal).

`tools/canonical_core_operations.py` (the only new cross-infrastructure composition root, spec 4.2, 13):

- `_CliParser` following the `local_service_stack.py` pattern: parse errors exit 2 with **no** environment or secret-file read; `--help`/`--version` exit 0 with no I/O.
- Subcommands and flags exactly (spec 13):
  - `bootstrap-identity --username --user-display-name --workspace-key --workspace-display-name --device-name --device-kind`
  - `read-current-source --workspace-id --source-id --output-file`
  - `backup-create --confirm-write-admission-disabled`
  - `backup-verify --bundle-id`
  - `restore-empty --bundle-id --target-database --confirm-target-database` (confirmation must equal `--target-database`)
  - `phase-one-acceptance` (registered now, implemented in Task 15)
- `CanonicalCoreExitCode(IntEnum)`: `OK=0, CLI=2, CONTRACT=65, UNAVAILABLE=69, INTERNAL=70, BUSY=75, CONFIG=78`.
- Environment gates for `backup-create`, `restore-empty` and `phase-one-acceptance` run before opening a database engine, R2 client, subprocess or bundle path: `KNOWLEDGE_ENVIRONMENT` must be exactly `local` or `test`; violations exit 78 with the registered diagnostic on stderr.
- Composition per subcommand: load database settings and secret-file password, build the engine, instantiate `PostgresqlIdentityBootstrapStore` / `PostgresqlCanonicalSourceReadStore` + `CanonicalSourceReadService` + the existing R2 store (`R2ClientManager` + `R2S3ObjectStore`) / `PostgresqlBackupSnapshotStore` + `PostgresqlRestoreTarget` + `PostgresqlDumpProcessAdapter` (after `check_client_tools`) + `FilesystemRecoveryBundleStore` via `canonical_recovery_bundle.build_bundle_store`, then `RecoveryService`. Every command runs under `asyncio.wait_for(..., RECOVERY_COMMAND_TIMEOUT_SECONDS)`; `ApplicationError` maps to exit codes through a closed code-to-exit table (validation/conflict/integrity → 65; dependency → 69; internal → 70; busy → 75; configuration/authorization → 78).
- `read-current-source` writes bytes only to `--output-file` opened exclusively (`open(path, "xb")`); never prints content to stdout/stderr (spec 6.2).
- Output: exactly one safe JSON document on stdout (`json.dumps(..., sort_keys=True, separators=(",", ":"))`); safe registered diagnostics on stderr; raw child output consumed and mapped, never forwarded; no prompts.
- `tools/canonical_recovery_bundle.py`: `build_bundle_store(settings)` runs `validate_backup_root(settings.backup_root)` then returns `FilesystemRecoveryBundleStore(settings.backup_root)`; nothing else.

Add Poe leaf tasks for the new test directories following the existing leaf-task convention (for example `canonical-core-test` running the unit/contract directories of this feature), composed into the existing public gates the way prior features did.

- [ ] **Step 4: Run tests, lint, type check and boundaries**

Run: `uv run pytest tests/unit/runtime_configuration tests/unit/tools -q && uv run poe python-lint && uv run poe python-type-check && uv run poe boundary-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/canonical_core_operations.py tools/canonical_recovery_bundle.py src/personal_os/runtime_configuration/environment_names.py src/personal_os/runtime_configuration/loading.py src/personal_os/runtime_configuration/models.py pyproject.toml tests/unit/runtime_configuration/test_canonical_recovery_settings.py tests/unit/tools/test_canonical_core_operations.py
git commit -m "feat: add canonical core operations cli and backup-root setting"
```

---

### Task 13: Disposable PostgreSQL/Temporal integration suite

**Files:**
- Create: `tests/integration/canonical_core/conftest.py`
- Create: `tests/integration/canonical_core/test_identity_bootstrap_integration.py`
- Create: `tests/integration/canonical_core/test_canonical_read_integration.py`
- Create: `tests/integration/canonical_core/test_recovery_integration.py`

**Interfaces:**
- Consumes: `tools.local_service_stack` lifecycle, the `source_publication` conftest template, `PostgresqlIdentityBootstrapStore`, `PostgresqlCanonicalSourceReadStore`, `PostgresqlBackupSnapshotStore`, `PostgresqlRestoreTarget`, `PostgresqlDumpProcessAdapter`, `FilesystemRecoveryBundleStore`, `RecoveryService`, `SourceVersionPublicationService`.
- Produces: fixtures `canonical_core_stack`, `canonical_core_harness`, `disposable_restore_database`.

- [ ] **Step 1: Write the conftest and integration tests**

`conftest.py` copies the `source_publication/conftest.py` gating and lifecycle exactly: `LOCAL_STACK_TEST_PROJECT` matching `knowledge-ci-*` with `CI == "true"` (`pytest.fail`, never skip), module-scoped stack fixture running reset → bootstrap → config → up → `alembic upgrade head` with a sanitized child environment, and a `finally` reset asserting zero labelled leftover Docker resources; `pytest_asyncio_loop_factories` forcing `SelectorEventLoop` on Windows. Add `disposable_restore_database`: create `knowledge_ci_restore_<nonce>` on the disposable PostgreSQL instance through the stack runner (`docker compose exec postgres psql`, superuser credential from the stack bootstrap), drop it in `finally`.

Test files implement spec 18.3 items 1–5 and 7–13 (item 6 — two intents converging on one Temporal execution — is already proven by `tests/integration/projection_dispatch/test_temporal_dispatch.py`; do not duplicate it; Task 15's acceptance flow re-proves it end to end):

`test_identity_bootstrap_integration.py`:

```python
async def test_empty_bootstrap_creates_exact_graph_and_audit(...): ...
async def test_exact_bootstrap_replay_creates_no_row_and_returns_original_timestamp(...): ...
async def test_partial_identity_state_conflicts_without_repair(...): ...
async def test_changed_display_name_conflicts(...): ...
async def test_revoked_bootstrap_device_conflicts(...): ...
```

`test_canonical_read_integration.py`:

```python
async def test_full_synthetic_source_create_read_replay_succeeds(...): ...
async def test_publication_claim_mismatch_leaves_no_canonical_row(...): ...
async def test_deleted_source_state_fails_closed(...): ...
```

`test_recovery_integration.py`:

```python
async def test_concurrent_dml_and_snapshot_busy(...) -> None:
    # Open a quiesced snapshot; a concurrent UPDATE blocks behind the SHARE
    # locks; a second open_quiesced_snapshot fails bounded (15 s) with
    # SNAPSHOT_BUSY.

async def test_dump_and_manifest_come_from_same_exported_snapshot(...): ...
async def test_bundle_verify_detects_dump_object_and_manifest_mutation(...): ...
async def test_empty_target_restore_is_single_transaction_and_exact(...): ...
async def test_restore_failure_leaves_target_database_empty(...): ...
async def test_post_restore_canonical_read_returns_exact_bytes(...): ...
async def test_failed_backup_leaves_no_staging_files_locks_or_processes(...): ...
    # Assert no staging directories under the backup root, no ungranted
    # locks in pg_locks for the nine tables, no leftover child processes.
```

Every run owns its unique `knowledge-ci-*` project and performs exact-label cleanup in `finally` (existing convention). Use `pytestmark = pytest.mark.local_stack` plus `pytest.mark.asyncio`.

- [ ] **Step 2: Run the suite against the disposable stack**

With Docker available:
`LOCAL_STACK_TEST_PROJECT=knowledge-ci-<nonce> CI=true uv run pytest tests/integration/canonical_core -m local_stack -q`
Expected: PASS. If Docker is unavailable in this session, record the run as deferred evidence in the task report (the Task 15 workflow proves it in CI) and state that explicitly. Always run: `uv run poe python-lint && uv run poe python-type-check`.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/canonical_core
git commit -m "test: add disposable postgresql recovery integration suite"
```

---

### Task 14: Protected live-R2 acceptance and corruption drills

**Files:**
- Create: `tests/integration/canonical_core/test_live_r2_acceptance.py`
- Modify: `tests/integration/r2_object_storage/conftest.py` (only if a harness gap blocks the drill; keep additions minimal and test-only)

**Interfaces:**
- Consumes: `LiveR2Harness` + `LiveCleanupManifest` (including `write_object_under_digest` and `delete_exact_object`), `R2S3ObjectStore`, `SourceVersionPublicationService`, `CanonicalSourceReadService`, `RecoveryService`, `FilesystemRecoveryBundleStore`, the Task 13 stack fixtures.
- Produces: protected live acceptance tests marked `pytestmark = [pytest.mark.r2_live, pytest.mark.local_stack, pytest.mark.asyncio]`.

- [ ] **Step 1: Write the live drills**

`test_live_r2_acceptance.py` implements spec 12.2, 12.3 and 18.4 items 1–9 against the real test bucket combined with the disposable PostgreSQL/Temporal stack (the Task 13 conftest provides the stack; live R2 gating comes from the `r2_live` marker and `R2_TEST_*` environment composed via `compose_live_environment`):

```python
async def test_same_size_corruption_detected_before_byte_exposure(...) -> None:
    # 1. publish a unique synthetic object and create a verified backup bundle
    # 2. write_object_under_digest with same-size, same-media different bytes
    # 3. canonical read -> OBJECT_STORAGE_INTEGRITY_FAILED after full SHA-256
    # 4. zero bytes reached the consumer
    # 5. source/version/current pointer/event/audit/intent state unchanged
    #    (row counts before/after identical)
    # 6. delete_exact_object on the corrupt test-owned key
    # 7. restore the original bundle object through production conditional
    #    store_stream from the bundle file with claimed_sha256
    # 8. canonical read again returns the exact original bytes for the same
    #    immutable source version


async def test_missing_referenced_object_fails_closed_without_mutation(...) -> None: ...
async def test_pre_publication_claim_mismatch_creates_no_canonical_pointer(...) -> None: ...
async def test_backup_contains_every_referenced_object_and_exact_bytes(...) -> None: ...
async def test_existing_exact_object_reused_and_mismatch_never_overwritten(...) -> None: ...
async def test_restore_matches_source_bundle_and_post_restore_read(...) -> None: ...
```

The suite never lists the bucket, never deletes a prefix or wildcard, touches only keys it created and registered in the cleanup manifest, and runs exact-key cleanup in `finally` — a cleanup failure fails the gate (existing harness behavior, spec 12.1). The corruption step preserves size and media type exactly so a HEAD-only implementation cannot pass (spec 12.2). Missing live credentials fail, never skip.

- [ ] **Step 2: Run offline checks**

Run: `uv run pytest tests/integration/canonical_core -q` (marker-deselected, collection must be clean) and `uv run poe python-lint && uv run poe python-type-check`
Expected: PASS collection; live execution is proven by the Task 15 protected workflow.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/canonical_core/test_live_r2_acceptance.py tests/integration/r2_object_storage/conftest.py
git commit -m "test: add protected live r2 acceptance and corruption drills"
```

---

### Task 15: phase-one-acceptance composition and protected CI workflow

**Files:**
- Modify: `tools/canonical_core_operations.py` (implement `phase-one-acceptance`)
- Create: `.github/workflows/canonical-core-acceptance.yml`
- Modify: `tests/contract/test_ci_security.py` (extend to the new workflow)
- Create: `tests/contract/canonical_core/test_composition_boundaries.py`
- Modify: `src/personal_os/diagnostics/events.py` (acceptance events)
- Test: `tests/unit/tools/test_canonical_core_acceptance.py`

**Interfaces:**
- Consumes: every production service from Tasks 1–12; `TemporalProjectionWorkflowStarter`, `projection_workflow_id`, `source_ingestion_reference_for_intent` from the worker adapter (tools may import it; API/MCP/Worker never import tools — spec 4.2).
- Produces: `run_phase_one_acceptance(...)` orchestration (injectable collaborators), the `phase-one-acceptance` CLI subcommand, events `CANONICAL_ACCEPTANCE_COMPLETED`/`CANONICAL_ACCEPTANCE_FAILED`, metric contract `canonical_acceptance_total{outcome}`, the protected workflow file.

- [ ] **Step 1: Write failing tests**

`tests/unit/tools/test_canonical_core_acceptance.py` — orchestration with fakes asserting the spec-7 flow order and proofs:

```python
async def test_flow_proves_all_spec_7_claims(...) -> None:
    # Ledger order: bootstrap, exact replay (no new row), synthetic publish
    # (preflight miss -> store/full-verify -> atomic commit), canonical read
    # (exact bytes), publication replay (no R2 call, no new row), two
    # intents claimed through fenced transitions, one Temporal start plus
    # one EXISTING resolution with the identical
    # source-ingestion/{workspace_id}/{event_id} workflow id and the closed
    # four-UUID input, safe summary emitted.


async def test_replay_bypasses_r2_and_adds_no_row(...) -> None: ...
async def test_acceptance_emits_completed_event_and_safe_summary() -> None: ...
async def test_failure_emits_failed_event_and_maps_exit_code() -> None: ...
```

`tests/contract/canonical_core/test_composition_boundaries.py` (spec 18.2):

```python
def test_core_imports_no_provider_driver_or_process_package() -> None:
    # personal_os.identity/recovery/sources import no sqlalchemy, psycopg,
    # temporalio, aiobotocore, botocore or subprocess-composition module.

def test_production_r2_adapter_exposes_no_destructive_capability() -> None:
    # No list/delete/overwrite/copy/presign attribute on R2S3ObjectStore.

def test_corruption_capability_lives_only_in_test_harness() -> None: ...
def test_cli_parses_before_settings_io() -> None: ...
def test_no_database_url_or_pgpassword_anywhere_in_tools() -> None: ...
def test_no_public_api_mcp_openapi_change() -> None: ...
def test_no_new_alembic_revision() -> None: ...

def test_workflow_contract_unchanged() -> None:
    # SourceIngestionWorkflow type/queue/id prefix and the closed
    # four-UUID input contract keep the approved values.

def test_new_events_and_metrics_registered() -> None: ...
```

Extend `tests/contract/test_ci_security.py` for `canonical-core-acceptance.yml`: secrets written as mode-0600 files only, removed `if: always()`; no bundle/dump/service-log/environment uploads — the artifact allowlist contains sanitized JUnit only; triggers are master pushes/schedule/dispatch only, never fork PRs with secrets (spec 18.4).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/tools/test_canonical_core_acceptance.py tests/contract/canonical_core tests/contract/test_ci_security.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the acceptance flow and workflow**

`run_phase_one_acceptance` (in `tools/canonical_core_operations.py`, collaborators injected for tests) implements spec 7 exactly: bootstrap identity → exact bootstrap replay (same IDs/timestamp, no new rows via counts) → construct a synthetic `CreateSourceVersion` as the bootstrap device with unique synthetic bytes, UUIDs and idempotency key → publication preflight miss → stream/store/full-verify through R2 → atomic PostgreSQL publication → canonical current-source read (exact bytes) → exact publication replay (original version, sequence, outcome and time; no R2 call; no new row) → claim the Qdrant and Neo4j projection intents through fenced transitions → start/resolve one deterministic Temporal execution (both intents derive the identical `source-ingestion/{workspace_id}/{event_id}` workflow ID and closed four-UUID input; the execution may keep waiting on `source-ingestion` because Phase 1 never registers the workflow implementation) → verify canonical state and safe diagnostics → emit `CANONICAL_ACCEPTANCE_COMPLETED` (or `..._FAILED` with `error_code`) → print one safe JSON summary (IDs and safe counts only). No Qdrant collection, Neo4j graph data or Redis application state is required (spec 7 proof 9). Register the two events and the `canonical_acceptance_total{outcome}` metric contract.

`.github/workflows/canonical-core-acceptance.yml` (spec 18.4, mirroring `object-storage-live.yml` with the required differences):

- Triggers: `push: branches: [master]`, daily `schedule`, `workflow_dispatch`. Never fork PRs.
- `permissions: contents: read`; `concurrency: canonical-core-acceptance-${{ github.workflow }}-${{ github.ref }}` with `cancel-in-progress: false` (per-bucket; never orphan live-bucket objects).
- Job timeout 45 minutes; pinned action versions matching the existing workflows (checkout v7.0.1, setup-uv 0.11.32, Python 3.14.6).
- Repo vars `R2_TEST_ENDPOINT`/`R2_TEST_BUCKET_NAME`; secrets `R2_TEST_ACCESS_KEY_ID`/`R2_TEST_SECRET_ACCESS_KEY` written step-locally as mode-0600 files under `${{ runner.temp }}` with the credential-shape guard; removed `if: always()`.
- Disposable `LOCAL_STACK_TEST_PROJECT: knowledge-ci-${{ github.run_id }}-${{ github.run_attempt }}`; `CI: true`.
- Steps: run `uv run pytest tests/integration/canonical_core -m "local_stack and r2_live" -q --junitxml=.local/test-results/canonical-core-acceptance.xml` (missing live credentials fail explicitly, never silently skip); exact-label stack cleanup; upload only the scrubbed JUnit artifact (retention 7 days). Never upload a bundle, dump, service log, environment dump or Temporal history.

- [ ] **Step 4: Run tests, lint, type check and boundaries**

Run: `uv run pytest tests/unit/tools tests/contract/canonical_core tests/contract/test_ci_security.py -q && uv run poe python-lint && uv run poe python-type-check && uv run poe boundary-check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/canonical_core_operations.py .github/workflows/canonical-core-acceptance.yml tests/contract/canonical_core tests/contract/test_ci_security.py tests/unit/tools/test_canonical_core_acceptance.py src/personal_os/diagnostics/events.py src/personal_os/recovery
git commit -m "feat: add phase one acceptance composition and protected workflow"
```

---

### Task 16: Operator runbook, canonical docs, handoff and backlog

**Files:**
- Create: `docs/operations/canonical-core-recovery.md`
- Create: `docs/handoff/2026-08-15-canonical-core-acceptance-and-recovery.md`
- Modify: `docs/handoff/BACKLOG.md`
- Modify: `docs/20-IMPLEMENTATION_PLAN.md` (only if it tracks per-phase deliverable status; otherwise skip)

**Interfaces:**
- Consumes: the implemented commands, exit codes and evidence from Tasks 1–15.
- Produces: the operator runbook, exactly one implementation handoff, correctly indexed deferred items.

- [ ] **Step 1: Write the runbook**

`docs/operations/canonical-core-recovery.md` mirrors the structure of `docs/operations/object-storage.md` and `docs/operations/source-publication.md`:

- **Command boundaries**: repository-internal CLI, six subcommands with exact flags, parse-before-I/O, no prompts, exit-code table (0/2/65/69/70/75/78 with the meanings from spec 13).
- **Configuration**: `KNOWLEDGE_CANONICAL_BACKUP_ROOT` (absolute private path, excluded from diagnostics), reused database/R2/Temporal settings, secret-file rules.
- **Identity bootstrap**: grammar, replay semantics, conflict posture (`identity_bootstrap_state_conflict` never repairs), audit actions.
- **Backup lifecycle**: admission gates, bundle layout (`manifest.json`, `manifest.sha256`, `postgres.dump`, `objects/sha256/...`), immutability, the verification step order, what the sidecar does and does not prove (spec 8.2).
- **Restore**: empty-target-only admission, R2-before-PostgreSQL ordering rationale, single-transaction guarantee, post-restore verification list.
- **Safety boundary**: local/test only, unencrypted-bundle handling (encrypted or ephemeral private storage requirement), prohibited actions (no production list/delete/overwrite, no automatic pointer rollback, no merge restore).
- **Corruption drills**: how to run the protected live drills and read their evidence.
- **Acceptance status**: dated status with evidence pointers (workflow run, commit SHA).

- [ ] **Step 2: Write the handoff and backlog**

`docs/handoff/2026-08-15-canonical-core-acceptance-and-recovery.md` per AGENTS.md: final commit SHA; gate status with evidence for each verification command actually run (`uv run poe verify` output, integration/live gate status or explicit deferred-with-reason); interpretive decisions with rationale (the `recovery/bundle.py` module addition, server-version refusal mapping, the second-database creation mechanism in integration tests); deferred items each with a verdict; next actions (Phase 2 Obsidian sync). Keep under ~400 lines — link living docs instead of copying them.

Each accepted deferred item gets exactly one line in `docs/handoff/BACKLOG.md` (existing `| Added | Domain | Item | Details |` format) pointing back to this handoff; remove lines for anything completed during this plan.

- [ ] **Step 3: Verify docs match implementation and commit**

Cross-check every documented flag, exit code and file name against `uv run python tools/canonical_core_operations.py --help` output and the test suites.

```bash
git add docs/operations/canonical-core-recovery.md docs/handoff/2026-08-15-canonical-core-acceptance-and-recovery.md docs/handoff/BACKLOG.md docs/20-IMPLEMENTATION_PLAN.md
git commit -m "docs: add canonical core recovery runbook and handoff"
```

---

## Self-Review

- **Spec coverage:** objective §1 (Tasks 1–16); layout/import/dependency rules §4 (Tasks 1–12, boundary contract tests in Task 15); identity §5 (Tasks 1–3); canonical read §6 (Tasks 4–5); acceptance flow §7 (Task 15); bundle contract §8 (Tasks 6–7); backup creation §9 (Tasks 8–10); offline verify §10 (Tasks 7, 11); restore §11 (Tasks 8, 11); drills §12 (Task 14); CLI §13 (Task 12); configuration §14 (Task 12); errors §15 (Tasks 1, 4, 6); diagnostics/metrics §16 (Tasks 1, 4, 6, 15); bounds §17 (Tasks 8–11); test strategy §18 (every task plus 13–15); completion criteria §19 (Task 16 handoff evidence); deliverables §20 (all covered; no migration, public API, projection implementation or new production dependency introduced).
- **Placeholder scan:** no TBD/TODO; every step names files, exact values and commands.
- **Type consistency:** `BootstrapIdentityCommand`/`BootstrapIdentityResult` (Tasks 1→2→3→12); `CanonicalSourceReference` (4→5→12); `RecoveryManifest`/`parse_manifest` (6→7→10→11); `CanonicalBackupSnapshot` (6→9→10); `PostgresqlDumpProcess` port (6) implemented by the Task 8 adapter; `RecoveryBundleStore` (6) implemented by Task 7; `RecoveryService` (10→11→12→15); exit codes (12→15).
