# Web Authentication and Device Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add protected Web credential enrollment, PostgreSQL-backed browser authentication with optional TOTP, browser-mediated Obsidian device authorization, rotating opaque device credentials, minimal security/device Admin pages and a SecretStorage-backed plugin login flow without implementing Vault synchronization.

**Architecture:** Framework-neutral authentication contracts and state transitions live under `personal_os.authentication`; PostgreSQL adapters own the atomic row-locking transactions; FastAPI owns cookies, Bearer extraction, Origin/CSRF checks and OpenAPI. Next.js and the Obsidian plugin consume only the generated API client, with browser sessions remaining server-side and plugin refresh state stored through an Obsidian SecretStorage adapter.

**Tech Stack:** Python 3.14.6, FastAPI 0.139.2, Pydantic 2.13.4, SQLAlchemy 2.0.51 async Core, psycopg 3.3.4, Alembic 1.18.5, PostgreSQL 18.4, argon2-cffi 25.1.0, cryptography 49.0.0, Next.js 16.3.0, React 19.2.8, TypeScript 6.0.3 strict, qrcode-generator 2.0.4, Obsidian API 1.13.1, Vitest 4.1.10 and Playwright 1.62.1.

**Normative spec:** `docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md` at commit `8c389be`. Section references below refer to that document. The API foundation plan at `docs/superpowers/plans/2026-08-15-api-runtime-and-contract-foundation.md` must already be complete.

## Global Constraints

- Implement child 2 only. Do not add public signup, email recovery, OIDC, WebAuthn, MCP credentials, source/sync/upload/download routes, queue/watcher behavior, R2, Worker, conflict handling, AI writes or physical auth-row pruning.
- PostgreSQL is authoritative for users, sessions, TOTP, grants, device/token state, throttles and audit. Redis is absent from authorization decisions.
- Domain code under `src/personal_os/authentication/` must not import FastAPI, Starlette, SQLAlchemy, psycopg, Obsidian, React or provider SDKs.
- Credentials are opaque and stateful. Do not add JWT, an authentication framework, an external identity provider, remote password reputation, remote QR rendering or third-party analytics.
- Add exactly three production dependency roles: `argon2-cffi==25.1.0` (MIT), `cryptography==49.0.0` (Apache-2.0 OR BSD-3-Clause) and `qrcode-generator==2.0.4` (MIT). `@types/qrcode-generator==1.0.6` is development-only.
- `argon2-cffi` adds `argon2-cffi-bindings`/CFFI through `uv.lock`; `cryptography` uses its CPython 3.14 wheel and bundled native crypto implementation; `qrcode-generator` has no runtime dependency and is bundled only into Web, never the plugin.
- Pin Argon2id to `memory_cost_kib=65536`, `time_cost=3`, `parallelism=1`, `salt_length_bytes=16`, `hash_length_bytes=32`. Benchmark 20 sequential hashes on the smallest deployment host; the reviewed p95 band is 150–750 ms. Changing the parameters or band requires a spec/operations update.
- Use AES-256-GCM with a fresh 12-byte nonce for TOTP secrets; HKDF-SHA-256 and HMAC-SHA-256 with explicit domain labels for CSRF, throttles, recovery-code hashes and exact token derivation. Never reuse a nonce/key pair.
- Passwords are 15–128 Unicode characters, spaces allowed, no composition rule and no periodic expiry. The offline blocklist is a committed sorted SHA-256 digest artifact with version/provenance; no raw password list is shipped or logged.
- Web sessions use an opaque secret with at least 256 bits of entropy, browser-session cookie `__Host-admin_session`, Secure, HttpOnly, SameSite=Lax, Path=/ and no Domain. Only explicit loopback local-development uses `admin_session_local` without Secure.
- Session limits are exact: pending TOTP five minutes, idle 12 hours, absolute seven days and recent re-authentication five minutes.
- TOTP is RFC 6238 SHA-1, six digits, 30 seconds and a ±1 step window; it generates one 160-bit secret and ten one-use recovery codes of twelve Base32 characters.
- Device grants last 600 seconds and start with a five-second poll interval. Access tokens last 15 minutes; refresh families have 30-day inactivity and 90-day absolute limits.
- Device credentials use `at1.<lookup_id>.<secret>` and `rt1.<lookup_id>.<secret>`. Database rows contain only secret hashes and derivation metadata needed for exact replay.
- The fixed device scope is `obsidian_sync`; Web scopes are `web_security_manage`, `device_authorization_approve` and `device_administration_manage`. Request bodies never select a workspace or widen scopes.
- Extend the registry with exactly `authentication_required`, `authentication_failed`, `authentication_rate_limited`, `recent_authentication_required`, `csrf_validation_failed`, `authorization_scope_denied`, `totp_enrollment_state_invalid`, `device_authorization_pending`, `device_authorization_slow_down`, `device_authorization_denied`, `device_authorization_expired`, `device_authorization_state_invalid`, `device_credential_invalid`, `device_revoked`, `device_token_reuse_detected` and `plugin_version_unsupported`, with the HTTP/retry/detail mapping in spec 17.
- Same-origin Web requests require exact configured Origin. Every state-changing session request also requires session cookie, CSRF cookie, equal `X-CSRF-Token` and stored hash match. CORS remains absent.
- Every auth response sets `Cache-Control: no-store`; provisioning and recovery responses also set `Pragma: no-cache`. Never place a credential in a URL, query, redirect, log, trace, metric, audit detail or generated artifact.
- Preserve child 1 envelopes, manually assigned semantic `operationId` values, closed HTTP/error mappings, no trailing-slash redirects and deterministic OpenAPI/generated-client checks.
- The plugin targets Obsidian 1.13.1, uses only `requestUrl`, `Platform`, `SecretStorage`, settings and browser-opening adapters, and imports no Node.js, Electron or `FileSystemAdapter` module at load time.
- SecretStorage record IDs contain only lowercase ASCII letters, digits and dashes. Clearing writes and reads back a credential-free tombstone, clears the settings reference and never claims to delete a SecretStorage key.
- Persisted timestamps and expiry comparisons use one PostgreSQL transaction timestamp. Application monotonic time is only for I/O deadlines and metrics.
- All external I/O has a timeout, bounded retry and typed safe error mapping. Auth database writes commit once and do not perform network I/O inside the transaction.
- Python is fully typed under mypy strict; TypeScript remains strict. Follow semantic naming in `AGENTS.md`, preserve unrelated worktree changes and stage only the active task's files.
- For every behavior: write the named failing test, run it and read the expected failure, add the smallest implementation, rerun the focused tests, then run affected lint/type/contract gates before committing.

---

## Dependency and Failure Decisions

| Role | Exact package | Runtime impact | Failure boundary |
|---|---|---|---|
| Password hashing | `argon2-cffi==25.1.0` | API/CLI only; CPython 3.14 supported | mismatch becomes generic `authentication_failed`; malformed stored PHC becomes safe `internal_error` plus closed diagnostic |
| AEAD/HKDF/HMAC | `cryptography==49.0.0` | API/CLI only; CPython 3.14 wheels required in CI/build | invalid keyring refuses startup; decrypt/tag/invariant failures fail closed as `internal_error` without crypto text |
| Local QR | `qrcode-generator==2.0.4` | Web bundle only; no runtime transitive packages | renderer error keeps the one-time URI in memory and offers accessible manual-key copy; no telemetry value |
| Web tests | `@testing-library/react==16.3.2`, `@testing-library/jest-dom==7.0.1`, `@testing-library/user-event==14.6.4`, `msw==2.15.0`, `jsdom==30.0.1` | development only | test/build gate only |
| Browser E2E | `@playwright/test==1.62.1` | root development only; browser install is explicit CI setup | E2E gate fails rather than skipping |

## File Structure

### Framework-neutral authentication domain

```text
src/personal_os/authentication/
├── __init__.py
├── contracts.py                  Closed states, commands, results and authenticated contexts
├── errors.py                     Authentication-domain errors mapped to the closed registry
├── ports.py                      Password, crypto, clock and transaction repository protocols
├── passwords.py                  Password policy, offline blocklist and rehash decision
├── crypto.py                     Credential parser and domain-separation inputs
├── sessions.py                   Login/session/CSRF/re-auth/password-change transitions
├── totp.py                       TOTP verification, replay marker and recovery values
├── device_authorization.py       Grant state machine and user-code validation
└── device_tokens.py              Access/refresh lineage, exact replay and reuse classifier

src/personal_os/authentication/data/
├── common-password-sha256-v1.txt
└── common-password-sha256-v1.provenance.json
```

### PostgreSQL and crypto adapters

```text
packages/postgresql-source-store/src/postgresql_source_store/
├── tables.py
├── authentication_credentials.py
├── web_session_store.py
├── totp_store.py
├── device_authorization_store.py
└── device_token_store.py

apps/api/src/api_runtime/
├── authentication_settings.py
├── authentication_crypto.py
├── authentication_composition.py
├── authentication_models.py
├── authentication_dependencies.py
├── session_routes.py
├── totp_routes.py
├── device_authorization_routes.py
└── device_admin_routes.py
```

### Web and Obsidian clients

```text
apps/web/src/
├── api/authentication-client.ts
├── features/authentication/{session-store,LoginForm,TotpChallenge,SecurityPanel}.tsx
├── features/devices/{DeviceApproval,DeviceList,DeviceRevokeDialog}.tsx
└── app/{login,device/approve,admin/devices,admin/security}/page.tsx

apps/obsidian-plugin/src/authentication/
├── contracts.ts
├── secret-storage-record.ts
├── device-authorization.ts
├── token-session.ts
└── settings-tab.ts
```

### Verification and operations

```text
migrations/versions/20260816_01_add_web_authentication_and_device_tokens.py
tests/unit/authentication/
tests/unit/postgresql_source_store/
tests/integration/authentication/
tests/contract/api/
tests/end_to_end/authentication/
docs/operations/web-authentication-and-device-authorization.md
docs/handoff/2026-08-16-web-authentication-and-device-authorization.md
```

---

### Task 1: Pin dependencies, authentication settings and keyring loading

**Files:**
- Modify: `pyproject.toml`, `apps/api/pyproject.toml`, `apps/web/package.json`, `package.json`, `uv.lock`, `pnpm-lock.yaml`
- Modify: `src/personal_os/runtime_configuration/environment_names.py`
- Create: `apps/api/src/api_runtime/authentication_settings.py`
- Create: `apps/api/src/api_runtime/authentication_crypto.py`
- Create: `tests/unit/api_runtime/test_authentication_settings.py`
- Create: `tests/unit/api_runtime/test_authentication_crypto.py`

**Interfaces:**
- Produces `AuthenticationSettings`, `AuthenticationKeyring`, `load_authentication_settings()` and `load_authentication_keyring(settings)`.
- `AuthenticationSettings` exposes `allowed_origin`, `trusted_proxy_cidrs`, `current_key_id`, `current_key_file`, `previous_key_files`, `minimum_plugin_version` and `maximum_plugin_version`; durations and limits are frozen typed constants matching Global Constraints.

- [ ] **Step 1: Write failing settings and keyring tests**

```python
def test_authentication_keyring_rejects_short_current_key(tmp_path: Path) -> None:
    (tmp_path / "auth-current.key").write_bytes(b"short")
    settings = authentication_settings(secret_root=tmp_path)
    with pytest.raises(ConfigurationError) as raised:
        load_authentication_keyring(settings)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID


def test_production_origin_must_be_https() -> None:
    with pytest.raises(ConfigurationError):
        load_authentication_settings(
            environ={"KNOWLEDGE_ENVIRONMENT": "production", "KNOWLEDGE_AUTH_ALLOWED_ORIGIN": "http://example.test"}
        )
```

- [ ] **Step 2: Run the focused tests and confirm import/configuration failures**

Run: `uv run pytest tests/unit/api_runtime/test_authentication_settings.py tests/unit/api_runtime/test_authentication_crypto.py -q`

Expected: collection fails because both modules are absent.

- [ ] **Step 3: Add exact pins, strict setting grammar and fail-before-bind keyring loading**

Use `KNOWLEDGE_AUTH_ALLOWED_ORIGIN`, `KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS`, `KNOWLEDGE_AUTH_CURRENT_KEY_ID`, `KNOWLEDGE_AUTH_CURRENT_KEY_FILE`, `KNOWLEDGE_AUTH_PREVIOUS_KEYS`, `KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION` and `KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION`. Parse previous keys as a bounded comma-separated `key-id=file-name` sequence, reject duplicate IDs/files, `..`, absolute paths, malformed CIDRs and more than four keys. Read each exact file through the existing secret-file boundary, require exactly 32 bytes and return immutable mappings.

```python
@dataclass(frozen=True, slots=True)
class AuthenticationKeyring:
    current_key_id: str
    keys_by_id: Mapping[str, bytes]

    def current_key(self) -> bytes:
        return self.keys_by_id[self.current_key_id]
```

Run `uv lock` and `pnpm install --lockfile-only`; inspect lockfile diffs and licenses before continuing.

- [ ] **Step 4: Run settings, import-side-effect and dependency gates**

Run: `uv run pytest tests/unit/api_runtime/test_authentication_settings.py tests/unit/api_runtime/test_authentication_crypto.py tests/contract/test_command_import_side_effects.py tests/contract/test_dependency_pins.py -q`

Run: `uv run poe python-lint && uv run poe python-type-check && pnpm run type-check`

Expected: all commands exit `0`; shell-only commands still read no key file.

- [ ] **Step 5: Commit the dependency/configuration deliverable**

```powershell
git add pyproject.toml apps/api/pyproject.toml apps/web/package.json package.json uv.lock pnpm-lock.yaml src/personal_os/runtime_configuration/environment_names.py apps/api/src/api_runtime/authentication_settings.py apps/api/src/api_runtime/authentication_crypto.py tests/unit/api_runtime/test_authentication_settings.py tests/unit/api_runtime/test_authentication_crypto.py
git commit -m "feat: add authentication runtime configuration"
```

---

### Task 2: Add password, crypto and authentication domain contracts

**Files:**
- Create: `src/personal_os/authentication/__init__.py`, `contracts.py`, `errors.py`, `ports.py`, `passwords.py`, `crypto.py`
- Create: `src/personal_os/authentication/data/common-password-sha256-v1.txt`, `common-password-sha256-v1.provenance.json`
- Modify: `src/personal_os/error_contracts/codes.py`, `exceptions.py`, `__init__.py`
- Create: `tests/unit/authentication/test_passwords.py`, `test_crypto.py`, `test_contracts.py`

**Interfaces:**
- Produces `PasswordHasherPort.hash_password()`, `verify_password()`, `needs_rehash()`, `AuthenticationCryptoPort`, `OpaqueCredential`, `AuthenticatedWebContext`, `AuthenticatedDeviceContext` and every closed enum used by later tasks.
- Credential parser returns typed values or `AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)` without retaining rejected input.

- [ ] **Step 1: Write failing policy, parser and derivation-vector tests**

```python
def test_password_policy_accepts_spaces_and_rejects_common_value(blocklist: PasswordBlocklist) -> None:
    assert validate_new_password("correct horse battery staple!", blocklist) == "correct horse battery staple!"
    with pytest.raises(AuthenticationError) as raised:
        validate_new_password("passwordpassword", blocklist)
    assert raised.value.error_code is ErrorCode.AUTHENTICATION_FAILED


def test_opaque_token_parser_rejects_unknown_version_without_echo() -> None:
    rejected = "rt9.lookup.secret-sentinel"
    with pytest.raises(AuthenticationError) as raised:
        parse_refresh_credential(rejected)
    assert rejected not in repr(raised.value)
```

- [ ] **Step 2: Run the domain tests and confirm missing-module failures**

Run: `uv run pytest tests/unit/authentication/test_passwords.py tests/unit/authentication/test_crypto.py tests/unit/authentication/test_contracts.py -q`

- [ ] **Step 3: Implement exact values, offline blocklist and reviewed adapters**

Generate the committed blocklist from SecLists release `2026.1`, exact source
`Passwords/Common-Credentials/10-million-password-list-top-10000.txt`, as
lowercase SHA-256 digests. Sort it bytewise, reject duplicate/malformed lines
and record the release, raw source URL, computed source SHA-256, generated
timestamp and generator version in provenance JSON. The generator downloads to
an OS temporary file, verifies that exactly 10,000 non-empty source lines were
read, writes only digests through `apply_patch`, and removes the temporary raw
list. Runtime loads only the digest file from package resources and compares a
SHA-256 digest with `hmac.compare_digest`.

```python
class PasswordHasherPort(Protocol):
    def hash_password(self, password: str) -> str: ...
    def verify_password(self, password_hash: str, password: str) -> bool: ...
    def needs_rehash(self, password_hash: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class OpaqueCredential:
    token_kind: DeviceTokenKind
    lookup_id: UUID
    secret: bytes = field(repr=False)
```

Use `argon2.PasswordHasher(type=Type.ID, memory_cost=65536, time_cost=3, parallelism=1, salt_len=16, hash_len=32)`. Use constant-time comparisons for stored hashes and explicit domain labels such as `auth/csrf/v1`, `auth/throttle/v1`, `auth/recovery/v1`, `auth/grant-replay/v1` and `auth/refresh-replay/v1`.

- [ ] **Step 4: Run unit, leak and architecture gates**

Run: `uv run pytest tests/unit/authentication tests/unit/error_contracts -q`

Run: `uv run pytest tests/contract/test_architecture_boundaries.py tests/contract/test_sensitive_diagnostics.py -q`

Expected: all pass; domain imports no framework/provider module and sentinels are absent from exception repr.

- [ ] **Step 5: Commit domain primitives**

```powershell
git add src/personal_os/authentication src/personal_os/error_contracts tests/unit/authentication tests/unit/error_contracts tests/contract/test_architecture_boundaries.py tests/contract/test_sensitive_diagnostics.py
git commit -m "feat: add authentication domain primitives"
```

---

### Task 3: Add the normalized authentication schema migration

**Files:**
- Create: `migrations/versions/20260816_01_add_web_authentication_and_device_tokens.py`
- Modify: `src/personal_os/database_schema.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/tables.py`, `__init__.py`
- Create: `tests/contract/test_authentication_migration_contract.py`
- Create: `tests/integration/authentication/test_authentication_migration.py`

**Interfaces:**
- Produces SQLAlchemy Core table objects for the eight spec tables and advances the canonical revision constant.
- No ORM and no `create_all()` path are introduced.

- [ ] **Step 1: Write failing source/reflection tests for all tables, checks and indexes**

```python
EXPECTED_AUTH_TABLES = {
    "user_credentials", "web_sessions", "totp_credentials", "totp_recovery_codes",
    "device_authorization_grants", "device_token_families", "device_tokens",
    "authentication_throttle_buckets",
}


def test_authentication_tables_are_present_in_dml_metadata() -> None:
    assert EXPECTED_AUTH_TABLES <= set(SOURCE_STORE_TABLES)
```

The integration test must exercise empty upgrade, Phase 1 fixture upgrade, exact-head reflection and destructive-gated downgrade. Insert one auth row and prove downgrade refuses without `-x allow_destructive=true`.

- [ ] **Step 2: Run migration tests and confirm the revision/table failures**

Run: `uv run pytest tests/contract/test_authentication_migration_contract.py tests/integration/authentication/test_authentication_migration.py -q`

- [ ] **Step 3: Implement one Alembic revision with the exact spec columns**

Use named PK/FK/check/unique/index constraints, `knowledge` schema, timezone-aware timestamps and closed state checks. Add partial uniques for active/pending TOTP, one active family per device, current refresh generation and one successor per predecessor. State/timestamp matrix checks must reject inconsistent pending, approved, denied, exchanged, rotated and revoked rows at the database boundary.

- [ ] **Step 4: Run migration and existing schema gates**

Run: `uv run pytest tests/contract/test_authentication_migration_contract.py tests/contract/test_canonical_postgresql_migration_contract.py tests/integration/authentication/test_authentication_migration.py -q`

Run: `uv run poe database-heads && uv run poe python-lint && uv run poe python-type-check`

- [ ] **Step 5: Commit the schema deliverable**

```powershell
git add migrations/versions/20260816_01_add_web_authentication_and_device_tokens.py src/personal_os/database_schema.py packages/postgresql-source-store/src/postgresql_source_store/tables.py packages/postgresql-source-store/src/postgresql_source_store/__init__.py tests/contract/test_authentication_migration_contract.py tests/integration/authentication/test_authentication_migration.py
git commit -m "feat: add authentication database schema"
```

---

### Task 4: Implement PostgreSQL credential, session and throttle transactions

**Files:**
- Create: `src/personal_os/authentication/sessions.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/authentication_credentials.py`, `web_session_store.py`
- Create: `tests/unit/authentication/test_sessions.py`
- Create: `tests/unit/postgresql_source_store/test_authentication_credentials.py`, `test_web_session_store.py`
- Create: `tests/integration/authentication/test_password_session_transactions.py`

**Interfaces:**
- Produces `CredentialStore`, `WebSessionStore`, `LoginService`, `SessionService`, `PasswordChangeService` and typed transaction commands/results.
- Produces `AuthenticationKeyReferenceStore.required_key_ids(database_now) -> frozenset[str]`; startup must fail before bind when the configured keyring omits an ID referenced by active TOTP or replay-eligible grant/token state.
- `LoginService.login(username, password, source_bucket, context)` always invokes a real or dummy hasher and never reveals account existence.

- [ ] **Step 1: Write failing transition and transaction tests**

```python
async def test_unknown_and_wrong_password_both_call_hasher_once() -> None:
    unknown = await harness.login(username="missing", password="sentinel")
    wrong = await harness.login(username="owner", password="sentinel")
    assert unknown.public_error == wrong.public_error == ErrorCode.AUTHENTICATION_FAILED
    assert harness.hasher.verify_calls == 2


async def test_fifth_failure_locks_bucket_for_fifteen_minutes() -> None:
    outcomes = [await harness.reject_login() for _ in range(5)]
    assert outcomes[-1].locked_until == harness.database_now + timedelta(minutes=15)
```

- [ ] **Step 2: Run focused tests and confirm missing services/stores**

Run: `uv run pytest tests/unit/authentication/test_sessions.py tests/unit/postgresql_source_store/test_authentication_credentials.py tests/unit/postgresql_source_store/test_web_session_store.py -q`

- [ ] **Step 3: Implement row-locking login/session/password-change transitions**

Hash passwords outside database locks. Inside one transaction, recheck active user/workspace and credential revision, update HMACed throttle buckets, create `pending_totp` or `active` sessions, rotate session/CSRF hashes, and append trusted-account audit events. Session authentication checks state, credential revision, idle and absolute expiry; conditionally advances `last_seen_at`/idle expiry without passing absolute expiry. Password change increments revision, revokes other sessions and rotates the current session without touching devices.

- [ ] **Step 4: Run unit/integration and no-network-in-transaction gates**

Run: `uv run pytest tests/unit/authentication/test_sessions.py tests/unit/postgresql_source_store/test_authentication_credentials.py tests/unit/postgresql_source_store/test_web_session_store.py tests/integration/authentication/test_password_session_transactions.py -q`

Run: `uv run pytest tests/contract/source_publication/test_no_network_in_transaction.py tests/contract/test_sensitive_diagnostics.py -q`

- [ ] **Step 5: Commit password/session persistence**

```powershell
git add src/personal_os/authentication/sessions.py packages/postgresql-source-store/src/postgresql_source_store/authentication_credentials.py packages/postgresql-source-store/src/postgresql_source_store/web_session_store.py tests/unit/authentication/test_sessions.py tests/unit/postgresql_source_store/test_authentication_credentials.py tests/unit/postgresql_source_store/test_web_session_store.py tests/integration/authentication/test_password_session_transactions.py
git commit -m "feat: add password session transactions"
```

---

### Task 5: Add protected credential enrollment, status and emergency reset CLI

**Files:**
- Modify: `apps/api/src/api_runtime/command.py`
- Create: `apps/api/src/api_runtime/authentication_commands.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/authentication_credentials.py`
- Create: `tests/unit/api_runtime/test_authentication_commands.py`
- Modify: `tests/contract/test_command_import_side_effects.py`, `test_process_commands.py`
- Create: `tests/integration/authentication/test_credential_commands.py`

**Interfaces:**
- Adds `personal-api enroll-web-credential --username <name> [--password-file-name <name>]`, `web-credential-status --username <name>` and `reset-web-authentication --username <name> [--password-file-name <name>]`.
- Password and reset confirmation are read only after full argument validation; reset confirmation must equal the canonical username.

- [ ] **Step 1: Write failing lazy-input and reset atomicity tests**

```python
def test_enrollment_help_does_not_prompt_or_load_settings(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(getpass, "getpass", fail_if_called)
    assert run(["enroll-web-credential", "--help"]) == 0


async def test_emergency_reset_revokes_every_auth_surface(database: AuthFixture) -> None:
    result = await database.reset_web_authentication(username="owner", new_password="new secure password", confirmation="owner")
    assert result.revoked_web_session_count == 2
    assert result.revoked_device_count == 1
    assert result.denied_grant_count == 1
```

- [ ] **Step 2: Run CLI tests and confirm unknown subcommands**

Run: `uv run pytest tests/unit/api_runtime/test_authentication_commands.py tests/contract/test_command_import_side_effects.py tests/integration/authentication/test_credential_commands.py -q`

- [ ] **Step 3: Implement lazy commands and atomic reset**

Read passwords with `getpass.getpass()` or one validated file name under the existing secret root. Never accept password text in argv/environment. Enrollment hashes outside the transaction, locks the canonical identity and refuses any existing credential. Status returns only enrolled/not-enrolled and `credential_revision`. Reset replaces password/TOTP/recovery state, revokes sessions/devices/families/tokens, denies unexchanged grants and writes one audit row with closed counts in one transaction.

- [ ] **Step 4: Run CLI, integration, lint and type gates**

Run: `uv run pytest tests/unit/api_runtime/test_authentication_commands.py tests/contract/test_command_import_side_effects.py tests/contract/test_process_commands.py tests/integration/authentication/test_credential_commands.py -q`

Run: `uv run poe python-lint && uv run poe python-type-check`

- [ ] **Step 5: Commit protected credential operations**

```powershell
git add apps/api/src/api_runtime/command.py apps/api/src/api_runtime/authentication_commands.py packages/postgresql-source-store/src/postgresql_source_store/authentication_credentials.py tests/unit/api_runtime/test_authentication_commands.py tests/contract/test_command_import_side_effects.py tests/contract/test_process_commands.py tests/integration/authentication/test_credential_commands.py
git commit -m "feat: add protected web credential operations"
```

---

### Task 6: Expose password login, session, logout, re-auth and password-change HTTP contracts

**Files:**
- Create: `apps/api/src/api_runtime/authentication_models.py`, `authentication_dependencies.py`, `session_routes.py`, `authentication_composition.py`
- Modify: `apps/api/src/api_runtime/application.py`, `server.py`, `openapi_export.py`
- Modify: `src/personal_os/api_contracts/request_values.py`, `src/personal_os/error_contracts/codes.py`
- Create: `tests/unit/api_runtime/test_session_routes.py`, `test_authentication_dependencies.py`
- Create: `tests/contract/api/test_session_security_contract.py`
- Modify: `packages/api-client/openapi.json`, `packages/api-client/src/generated/schema.ts`

**Interfaces:**
- Adds `POST /api/auth/login`, `GET /api/auth/session`, `POST /api/auth/logout`, `POST /api/auth/reauthenticate`, `PUT /api/auth/password`.
- Produces `SessionData`, `LoginRequest`, `ReauthenticateRequest`, `PasswordChangeRequest` and cookie/CSRF response helpers.

- [ ] **Step 1: Write failing cookie, Origin, CSRF and closed-route tests**

```python
def test_login_sets_host_session_and_csrf_cookies(client: TestClient) -> None:
    response = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=VALID_LOGIN)
    assert response.status_code == 200
    assert "__Host-admin_session=" in response.headers.get_list("set-cookie")[0]
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert response.headers["cache-control"] == "no-store"


def test_password_change_rejects_missing_csrf(client: TestClient) -> None:
    assert client.put("/api/auth/password", json=VALID_CHANGE).json()["error"]["code"] == "csrf_validation_failed"
```

- [ ] **Step 2: Run HTTP tests and confirm route-not-found failures**

Run: `uv run pytest tests/unit/api_runtime/test_session_routes.py tests/unit/api_runtime/test_authentication_dependencies.py tests/contract/api/test_session_security_contract.py -q`

- [ ] **Step 3: Register strict route models and security dependencies**

Extract cookies and CSRF only in FastAPI dependencies, compare Origin exactly, attach typed context to request state and use dedicated response helpers for rotation/clear. Local insecure cookies require loopback origin plus local/test runtime. Offline OpenAPI composition injects deterministic fake ports and reads no environment/key file/database.

- [ ] **Step 4: Run HTTP/error/OpenAPI regression gates**

Run: `uv run pytest tests/unit/api_runtime tests/contract/api -q`

Run: `uv run poe api-contract-export && pnpm --filter @workspace/api-client run generate && uv run poe api-contract-check`

Expected: routes use envelopes and semantic operation IDs; snapshot and generated client contain the five session/password methods and the staleness gate exits `0`.

- [ ] **Step 5: Commit the session HTTP slice**

```powershell
git add apps/api/src/api_runtime src/personal_os/api_contracts/request_values.py src/personal_os/error_contracts/codes.py packages/api-client/openapi.json packages/api-client/src/generated/schema.ts tests/unit/api_runtime/test_session_routes.py tests/unit/api_runtime/test_authentication_dependencies.py tests/contract/api/test_session_security_contract.py
git commit -m "feat: expose web session authentication"
```

---

### Task 7: Implement TOTP enrollment, verification and recovery

**Files:**
- Create: `src/personal_os/authentication/totp.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/totp_store.py`
- Create: `apps/api/src/api_runtime/totp_routes.py`
- Modify: `apps/api/src/api_runtime/authentication_models.py`, `authentication_composition.py`, `application.py`
- Create: `tests/unit/authentication/test_totp.py`, `tests/unit/postgresql_source_store/test_totp_store.py`, `tests/unit/api_runtime/test_totp_routes.py`
- Create: `tests/integration/authentication/test_totp_recovery_transactions.py`
- Modify: `packages/api-client/openapi.json`, `packages/api-client/src/generated/schema.ts`

**Interfaces:**
- Adds `POST /api/auth/totp/verify`, `POST /api/auth/totp/enrollments`, `POST /api/auth/totp/enrollments/{enrollment_id}/verify`, `POST /api/auth/totp/recovery`, `POST /api/auth/totp/recovery-codes/regenerate` and `DELETE /api/auth/totp`.
- `POST /api/auth/totp/enrollments` uses `TotpEnrollmentAction = start | dismiss_initial_offer`; dismissal requires active session/Origin/CSRF, records `totp_prompt_dismissed_at` and creates no secret or pending row.
- Produces `TotpService`, `TotpStore`, `TotpEnrollmentData`, `RecoveryCodesData` and `RecoveryLimitedContext`.

- [ ] **Step 1: Write failing RFC vector, replay and recovery-lock tests**

```python
def test_rfc6238_sha1_vector_at_59_seconds() -> None:
    assert totp_code(secret=b"12345678901234567890", unix_time_seconds=59, digits=8) == "94287082"


async def test_same_totp_step_is_accepted_once_under_race(store: TotpStore) -> None:
    results = await asyncio.gather(store.verify(CODE), store.verify(CODE), return_exceptions=True)
    assert sum(isinstance(result, TotpVerified) for result in results) == 1
```

- [ ] **Step 2: Run TOTP tests and confirm missing implementation**

Run: `uv run pytest tests/unit/authentication/test_totp.py tests/unit/postgresql_source_store/test_totp_store.py tests/unit/api_runtime/test_totp_routes.py -q`

- [ ] **Step 3: Implement encrypted pending enrollment and one-use recovery**

Create the 160-bit secret with `secrets.token_bytes(20)`, encrypt using current AES-GCM key and key ID, expire pending rows after ten minutes, lock the credential row when advancing `last_accepted_time_step`, and activate plus create ten hashed recovery rows in one transaction. When a valid active credential uses a previous key, decrypt and re-encrypt it with the current key under the same row lock before commit. Recovery consumes one unused code under lock and creates only `recovery_limited`; replacement rotates session/CSRF and returns new codes once. Regeneration requires password plus current TOTP and invalidates the old revision. Disable requires the same proofs, revokes every recovery code, increments credential revision, revokes other Web sessions and rotates the current session to password-only.

- [ ] **Step 4: Run TOTP unit/integration/HTTP gates**

Run: `uv run pytest tests/unit/authentication/test_totp.py tests/unit/postgresql_source_store/test_totp_store.py tests/unit/api_runtime/test_totp_routes.py tests/integration/authentication/test_totp_recovery_transactions.py -q`

Run: `uv run poe api-contract-export && pnpm --filter @workspace/api-client run generate && uv run poe api-contract-check`

Run: `uv run poe python-lint && uv run poe python-type-check`

- [ ] **Step 5: Commit TOTP/recovery**

```powershell
git add src/personal_os/authentication/totp.py packages/postgresql-source-store/src/postgresql_source_store/totp_store.py apps/api/src/api_runtime/totp_routes.py apps/api/src/api_runtime/authentication_models.py apps/api/src/api_runtime/authentication_composition.py apps/api/src/api_runtime/application.py packages/api-client/openapi.json packages/api-client/src/generated/schema.ts tests/unit/authentication/test_totp.py tests/unit/postgresql_source_store/test_totp_store.py tests/unit/api_runtime/test_totp_routes.py tests/integration/authentication/test_totp_recovery_transactions.py
git commit -m "feat: add totp and recovery authentication"
```

---

### Task 8: Implement device grant creation, lookup, approval and denial

**Files:**
- Create: `src/personal_os/authentication/device_authorization.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/device_authorization_store.py`
- Create: `apps/api/src/api_runtime/device_authorization_routes.py`
- Modify: `apps/api/src/api_runtime/authentication_models.py`, `authentication_composition.py`, `application.py`
- Create: `tests/unit/authentication/test_device_authorization.py`, `tests/unit/postgresql_source_store/test_device_authorization_store.py`, `tests/unit/api_runtime/test_device_authorization_routes.py`
- Create: `tests/integration/authentication/test_device_authorization_races.py`
- Modify: `packages/api-client/openapi.json`, `packages/api-client/src/generated/schema.ts`

**Interfaces:**
- Adds `POST /api/auth/device-authorizations`, `POST /api/auth/device-authorizations/lookup`, `POST /api/auth/device-authorizations/{grant_id}/approve` and `POST /api/auth/device-authorizations/{grant_id}/deny`.
- Produces `DeviceAuthorizationService`.
- `create_grant()` returns one user code, polling credential and exact verification URLs; only the user code is allowed in the fragment.

- [ ] **Step 1: Write failing grant validation and approve/deny race tests**

```python
def test_verification_complete_contains_only_user_code_fragment() -> None:
    grant = create_grant(FIXED_RANDOMNESS)
    parsed = urlsplit(grant.verification_uri_complete)
    assert parsed.query == ""
    assert parsed.fragment == grant.user_code
    assert grant.polling_secret not in grant.verification_uri_complete


async def test_approve_racing_deny_has_one_terminal_winner(store: DeviceAuthorizationStore) -> None:
    outcomes = await race(store.approve(COMMAND), store.deny(COMMAND))
    assert sorted(outcome.kind for outcome in outcomes) in (["approved", "state_invalid"], ["denied", "state_invalid"])
```

- [ ] **Step 2: Run grant tests and confirm missing state machine/store**

Run: `uv run pytest tests/unit/authentication/test_device_authorization.py tests/unit/postgresql_source_store/test_device_authorization_store.py tests/unit/api_runtime/test_device_authorization_routes.py -q`

- [ ] **Step 3: Implement validated grants and locked state transitions**

Validate UUID/client instance, display bounds, closed platform values, semantic plugin version bounds and fixed scope before generating secrets. HMAC user code and polling secret before persistence. Approval/denial locks the row, rechecks database time/state, requires typed active/recent Web context for approval and writes exactly one audit event. Poll rate state remains in PostgreSQL throttle buckets.

- [ ] **Step 4: Run unit/race/HTTP gates**

Run: `uv run pytest tests/unit/authentication/test_device_authorization.py tests/unit/postgresql_source_store/test_device_authorization_store.py tests/unit/api_runtime/test_device_authorization_routes.py tests/integration/authentication/test_device_authorization_races.py -q`

Run: `uv run poe api-contract-export && pnpm --filter @workspace/api-client run generate && uv run poe api-contract-check`

- [ ] **Step 5: Commit browser device authorization**

```powershell
git add src/personal_os/authentication/device_authorization.py packages/postgresql-source-store/src/postgresql_source_store/device_authorization_store.py apps/api/src/api_runtime/device_authorization_routes.py apps/api/src/api_runtime/authentication_models.py apps/api/src/api_runtime/authentication_composition.py apps/api/src/api_runtime/application.py packages/api-client/openapi.json packages/api-client/src/generated/schema.ts tests/unit/authentication/test_device_authorization.py tests/unit/postgresql_source_store/test_device_authorization_store.py tests/unit/api_runtime/test_device_authorization_routes.py tests/integration/authentication/test_device_authorization_races.py
git commit -m "feat: add browser device authorization"
```

---

### Task 9: Implement exact-replay device registration and refresh rotation

**Files:**
- Create: `src/personal_os/authentication/device_tokens.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/device_token_store.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/device_authorization_store.py`
- Modify: `apps/api/src/api_runtime/device_authorization_routes.py`, `authentication_dependencies.py`, `authentication_models.py`
- Create: `tests/unit/authentication/test_device_tokens.py`, `tests/unit/postgresql_source_store/test_device_token_store.py`
- Create: `tests/integration/authentication/test_device_token_replay.py`, `test_device_token_reuse.py`
- Modify: `packages/api-client/openapi.json`, `packages/api-client/src/generated/schema.ts`

**Interfaces:**
- Produces `DeviceTokenService.exchange_grant()`, `refresh()`, `authenticate_access()` and versioned token parser/formatter.
- Adds `POST /api/auth/device-authorizations/{grant_id}/poll`; the polling Bearer credential is the only authority accepted by this route.
- Same grant secret or predecessor plus same `rotation_id` returns byte-identical material and anchored timestamps while replay remains valid.

- [ ] **Step 1: Write failing golden derivation, duplicate exchange and reuse tests**

```python
def test_refresh_derivation_vector_is_stable() -> None:
    result = derive_refresh_successor(
        master_key=bytes(range(32)),
        predecessor_secret=bytes(range(32, 64)),
        rotation_id=UUID("00000000-0000-0000-0000-000000000001"),
        token_family_id=UUID("00000000-0000-0000-0000-000000000002"),
        successor_generation=2,
    )
    assert result.secret.hex() == "266ad59acb65e0a437eb79891fa1a349fd1a5e90f531ccf3e442b1920d8a5141"


async def test_same_predecessor_different_rotation_revokes_family(harness: TokenHarness) -> None:
    await harness.refresh(rotation_id=ROTATION_A)
    with pytest.raises(AuthenticationError) as raised:
        await harness.refresh(rotation_id=ROTATION_B)
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED
    assert await harness.family_state() == "revoked"
```

- [ ] **Step 2: Run token tests and confirm missing service/store**

Run: `uv run pytest tests/unit/authentication/test_device_tokens.py tests/unit/postgresql_source_store/test_device_token_store.py tests/integration/authentication/test_device_token_replay.py tests/integration/authentication/test_device_token_reuse.py -q`

- [ ] **Step 3: Implement locked exchange/rotation and current access checks**

Grant exchange creates one device, family, access token and refresh token then anchors their IDs/key ID on the grant in one transaction. Re-poll derives the same result only while generation zero is current. Refresh locks predecessor/family, classifies exact replay versus confirmed reuse, creates one successor/access pair, links lineage and never extends absolute expiry. Access authentication checks token/family/device/user/workspace state on every request and updates device last-seen at most once per five minutes.

For the golden refresh vector, derive a 32-byte PRF key with RFC 5869 HKDF-SHA-256 using a 32-byte zero salt, the 32-byte master key as input key material and `auth/refresh-replay/v1` as `info`. HMAC-SHA-256 that key over `predecessor_secret || rotation_id.bytes || token_family_id.bytes || successor_generation.to_bytes(8, "big")`. Access and initial-exchange derivations use different fixed `info` labels.

- [ ] **Step 4: Run replay, ambiguous-commit and concurrency gates**

Run: `uv run pytest tests/unit/authentication/test_device_tokens.py tests/unit/postgresql_source_store/test_device_token_store.py tests/integration/authentication/test_device_token_replay.py tests/integration/authentication/test_device_token_reuse.py -q`

Inject lost commit acknowledgements for exchange and refresh; assert retry creates no new device/family/token/audit row and returns identical credentials/timestamps.

Run: `uv run poe api-contract-export && pnpm --filter @workspace/api-client run generate && uv run poe api-contract-check`

- [ ] **Step 5: Commit exact-replay tokens**

```powershell
git add src/personal_os/authentication/device_tokens.py packages/postgresql-source-store/src/postgresql_source_store/device_token_store.py packages/postgresql-source-store/src/postgresql_source_store/device_authorization_store.py apps/api/src/api_runtime/device_authorization_routes.py apps/api/src/api_runtime/authentication_dependencies.py apps/api/src/api_runtime/authentication_models.py packages/api-client/openapi.json packages/api-client/src/generated/schema.ts tests/unit/authentication/test_device_tokens.py tests/unit/postgresql_source_store/test_device_token_store.py tests/integration/authentication/test_device_token_replay.py tests/integration/authentication/test_device_token_reuse.py
git commit -m "feat: add rotating device credentials"
```

---

### Task 10: Add device refresh, self-revoke and Admin device APIs

**Files:**
- Create: `apps/api/src/api_runtime/device_admin_routes.py`
- Modify: `apps/api/src/api_runtime/device_authorization_routes.py`, `authentication_models.py`, `authentication_composition.py`, `application.py`
- Modify: `src/personal_os/authentication/device_tokens.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/device_token_store.py`
- Create: `tests/unit/api_runtime/test_device_token_routes.py`, `test_device_admin_routes.py`
- Create: `tests/integration/authentication/test_device_revoke_races.py`
- Modify: `packages/api-client/openapi.json`, `packages/api-client/src/generated/schema.ts`

**Interfaces:**
- Adds `POST /api/auth/device-authorizations/{grant_id}/poll`, `POST /api/auth/device-tokens/refresh`, `POST /api/auth/device-tokens/revoke-current`, `GET /api/admin/devices`, `POST /api/admin/devices/{device_id}/revoke`.

- [ ] **Step 1: Write failing Bearer-scheme, confirmation and revoke-race tests**

```python
def test_admin_revoke_requires_exact_device_name(client: TestClient, session: SessionCookies) -> None:
    response = client.post(f"/api/admin/devices/{DEVICE_ID}/revoke", cookies=session.cookies, headers=session.csrf_headers, json={"device_name_confirmation": "wrong"})
    assert response.status_code == 409


async def test_refresh_racing_admin_revoke_leaves_no_usable_token(harness: TokenHarness) -> None:
    await race(harness.refresh(ROTATION_ID), harness.admin_revoke())
    assert await harness.any_usable_token() is False
```

- [ ] **Step 2: Run route/race tests and confirm missing endpoints**

Run: `uv run pytest tests/unit/api_runtime/test_device_token_routes.py tests/unit/api_runtime/test_device_admin_routes.py tests/integration/authentication/test_device_revoke_races.py -q`

- [ ] **Step 3: Implement dedicated Bearer extraction and atomic revocation**

Declare separate OpenAPI schemes for polling, access and refresh. Self-revoke authenticates the current refresh credential and revokes its family/device. Admin list excludes the bootstrap system device, joins the exchanged authorization grant for validated platform/plugin metadata and returns only approved fields. Admin revoke requires active/recent Web context, CSRF and exact stored display-name confirmation, then revokes device/families/tokens and related grant in one locked transaction.

- [ ] **Step 4: Run route, error and race gates**

Run: `uv run pytest tests/unit/api_runtime/test_device_token_routes.py tests/unit/api_runtime/test_device_admin_routes.py tests/integration/authentication/test_device_revoke_races.py tests/contract/api/test_sensitive_http_contract.py -q`

Run: `uv run poe api-contract-export && pnpm --filter @workspace/api-client run generate && uv run poe api-contract-check`

- [ ] **Step 5: Commit device management APIs**

```powershell
git add apps/api/src/api_runtime/device_admin_routes.py apps/api/src/api_runtime/device_authorization_routes.py apps/api/src/api_runtime/authentication_models.py apps/api/src/api_runtime/authentication_composition.py apps/api/src/api_runtime/application.py src/personal_os/authentication/device_tokens.py packages/postgresql-source-store/src/postgresql_source_store/device_token_store.py packages/api-client/openapi.json packages/api-client/src/generated/schema.ts tests/unit/api_runtime/test_device_token_routes.py tests/unit/api_runtime/test_device_admin_routes.py tests/integration/authentication/test_device_revoke_races.py
git commit -m "feat: add device token administration"
```

---

### Task 11: Finalize HTTP security, OpenAPI and generated client

**Files:**
- Modify: `apps/api/src/api_runtime/request_context.py`, `application.py`, `openapi_export.py`
- Create: `apps/api/src/api_runtime/web_security.py`, `trusted_proxy.py`
- Modify: `packages/api-client/openapi.json`, `packages/api-client/src/generated/schema.ts`, `packages/api-client/src/index.ts`
- Modify: `tools/api_contract_artifacts.py`
- Create: `tests/unit/api_runtime/test_web_security.py`, `test_trusted_proxy.py`
- Create: `tests/contract/api/test_authentication_openapi.py`, `test_authentication_route_set.py`

**Interfaces:**
- Produces nonce CSP/security headers, bounded trusted-proxy resolution and all generated auth methods/types.
- The route set is exactly health/OpenAPI plus spec 16.1–16.4; no slash aliases.

- [ ] **Step 1: Write failing CSP/proxy/OpenAPI contract tests**

```python
def test_untrusted_peer_cannot_supply_forwarded_address() -> None:
    assert resolve_client_address(socket_peer="203.0.113.8", forwarded_for="10.0.0.1", trusted_proxy_cidrs=TRUSTED) == "203.0.113.8"


def test_auth_openapi_has_semantic_operations_and_distinct_bearer_schemes(schema: dict[str, object]) -> None:
    assert schema["paths"]["/api/auth/device-tokens/refresh"]["post"]["operationId"] == "refreshDeviceToken"
    assert {"PollingCredential", "AccessCredential", "RefreshCredential"} <= set(schema["components"]["securitySchemes"])
```

- [ ] **Step 2: Run security/OpenAPI tests and observe stale snapshot/client**

Run: `uv run pytest tests/unit/api_runtime/test_web_security.py tests/unit/api_runtime/test_trusted_proxy.py tests/contract/api/test_authentication_openapi.py tests/contract/api/test_authentication_route_set.py -q`

- [ ] **Step 3: Add headers/proxy logic and regenerate artifacts once**

Generate a fresh per-response nonce and set the exact CSP directives, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, and `no-store`/`Pragma` classifications. Bound forwarded chains to eight hops and select the rightmost untrusted hop only when the immediate peer is trusted. Export normalized OpenAPI, generate TypeScript from the local snapshot and never hand-edit `schema.ts`.

Run: `uv run poe api-contract-export && pnpm --filter @workspace/api-client run generate`

- [ ] **Step 4: Run full HTTP/artifact/client gates**

Run: `uv run pytest tests/unit/api_runtime tests/contract/api -q`

Run: `uv run poe api-contract-check && pnpm --filter @workspace/api-client run test && pnpm --filter @workspace/api-client run type-check`

- [ ] **Step 5: Commit the complete API contract atomically**

```powershell
git add apps/api/src/api_runtime packages/api-client/openapi.json packages/api-client/src/generated/schema.ts packages/api-client/src/index.ts tools/api_contract_artifacts.py tests/unit/api_runtime/test_web_security.py tests/unit/api_runtime/test_trusted_proxy.py tests/contract/api/test_authentication_openapi.py tests/contract/api/test_authentication_route_set.py
git commit -m "feat: publish authentication api contract"
```

---

### Task 12: Build Web login and Security pages

**Files:**
- Create: `apps/web/src/api/authentication-client.ts`
- Create: `apps/web/src/proxy.ts`, `apps/web/src/security-headers.ts`, `apps/web/src/security-headers.test.ts`
- Create: `apps/web/src/features/authentication/session-store.ts`, `LoginForm.tsx`, `TotpChallenge.tsx`, `SecurityPanel.tsx`
- Create: `apps/web/src/app/login/page.tsx`, `apps/web/src/app/admin/security/page.tsx`
- Modify: `apps/web/src/app/page.tsx`, `layout.tsx`
- Create corresponding `*.test.tsx` files beside components
- Create: `tests/end_to_end/authentication/web-security.spec.ts`, `playwright.config.ts`

**Interfaces:**
- Produces memory-only `AuthenticationSessionStore`; CSRF is read from its cookie at request time and never persisted in Web storage.
- Login supports password, TOTP, recovery-limited replacement and skippable first-login TOTP offer.

- [ ] **Step 1: Write failing component tests with MSW and storage sentinels**

```typescript
it("does not persist credentials in web storage", async () => {
  render(<LoginForm client={client} sessionStore={store} />);
  await user.type(screen.getByLabelText("Username"), "owner");
  await user.type(screen.getByLabelText("Password"), "correct horse battery staple!");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  expect(localStorage).toHaveLength(0);
  expect(sessionStorage).toHaveLength(0);
});
```

- [ ] **Step 2: Run Web tests and confirm missing routes/components**

Run: `pnpm --filter @workspace/web-runtime run test`

- [ ] **Step 3: Implement accessible login/security state machines**

Use generated methods only. Keep provisioning URI/recovery codes in component memory, render QR locally with `qrcode-generator`, offer manual secret/code copy, clear one-time values on navigation, and return focus to the first actionable error. `/` redirects to `/login` or `/admin/devices` based only on `GET /api/auth/session`; never accept arbitrary return URLs.

Implement Next.js 16 `src/proxy.ts` with a fresh per-request nonce, set the nonce in the request header for App Router rendering and the CSP on the response, and exclude `/api`, `/_next/static`, `/_next/image` and metadata assets from its matcher. Force the four authenticated/authentication pages to dynamic rendering with `await connection()`. Production CSP contains `default-src 'self'`, `script-src 'self' 'nonce-<value>'`, `connect-src 'self'`, `img-src 'self' data:`, `object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'none'` and `form-action 'self'`; only development may add the minimum Next.js debug directive. Also set `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`.

- [ ] **Step 4: Run Web unit, type, build and Playwright security flow**

Run: `pnpm --filter @workspace/web-runtime run test && pnpm --filter @workspace/web-runtime run type-check && pnpm --filter @workspace/web-runtime run build`

Run: `pnpm exec playwright test tests/end_to_end/authentication/web-security.spec.ts`

- [ ] **Step 5: Commit Web authentication UI**

```powershell
git add apps/web tests/end_to_end/authentication/web-security.spec.ts playwright.config.ts package.json pnpm-lock.yaml
git commit -m "feat: add web authentication security ui"
```

---

### Task 13: Build Web device approval and Admin device pages

**Files:**
- Create: `apps/web/src/features/devices/DeviceApproval.tsx`, `DeviceList.tsx`, `DeviceRevokeDialog.tsx`
- Create: `apps/web/src/app/device/approve/page.tsx`, `apps/web/src/app/admin/devices/page.tsx`
- Create corresponding `*.test.tsx` files
- Create: `tests/end_to_end/authentication/device-administration.spec.ts`

**Interfaces:**
- `DeviceApproval` consumes the user code from the fragment exactly once, removes it with `history.replaceState` and holds grant context only in memory.
- `DeviceRevokeDialog` requires exact device-name confirmation and recent re-auth before calling generated revoke.

- [ ] **Step 1: Write failing fragment-removal, escaping and confirmation tests**

```typescript
it("removes the user code fragment before lookup", async () => {
  window.history.replaceState({}, "", "/device/approve#ABCD-EFGH");
  render(<DeviceApproval client={client} />);
  await waitFor(() => expect(window.location.hash).toBe(""));
  expect(localStorage).toHaveLength(0);
});
```

- [ ] **Step 2: Run component tests and confirm missing pages**

Run: `pnpm --filter @workspace/web-runtime run test`

- [ ] **Step 3: Implement approval, list and revoke behavior**

Render all plugin metadata as React text, show fixed scope and expiry, require deliberate Approve/Deny actions, and support inline login without reconstructing the fragment. Device list displays only spec fields, retains revoked rows read-only and excludes the bootstrap device by server contract.

- [ ] **Step 4: Run Web gates and Playwright device journeys**

Run: `pnpm --filter @workspace/web-runtime run test && pnpm --filter @workspace/web-runtime run type-check && pnpm --filter @workspace/web-runtime run build`

Run: `pnpm exec playwright test tests/end_to_end/authentication/device-administration.spec.ts`

- [ ] **Step 5: Commit device Admin UI**

```powershell
git add apps/web/src/features/devices apps/web/src/app/device apps/web/src/app/admin/devices tests/end_to_end/authentication/device-administration.spec.ts
git commit -m "feat: add device approval administration ui"
```

---

### Task 14: Implement the Obsidian SecretStorage onboarding and token session

**Files:**
- Create: `apps/obsidian-plugin/src/authentication/contracts.ts`, `secret-storage-record.ts`, `device-authorization.ts`, `token-session.ts`, `settings-tab.ts`
- Modify: `apps/obsidian-plugin/src/plugin.ts`, `manifest.json`, `README.md`
- Create corresponding `*.test.ts` files
- Modify: `apps/obsidian-plugin/src/plugin.test.ts`
- Create: `tests/contract/api/test_plugin_authentication_bundle.py`

**Interfaces:**
- Produces `SecretStorageRecordAdapter`, `DeviceAuthorizationController`, `DeviceTokenSession` and closed `ConnectionState` values from spec 19.
- Access credential remains a private in-memory field; settings contain only server origin, device/client metadata and SecretStorage record name.

- [ ] **Step 1: Write failing SecretStorage, crash, offline and tombstone tests**

```typescript
it("writes and reads back rotation identity before refresh", async () => {
  await session.refresh();
  expect(secretStorage.setSecret).toHaveBeenCalledBefore(transport.refresh);
  expect(secretStorage.getSecret).toHaveReturnedWith(expect.stringContaining("pending_rotation_id"));
});


it("terminal reuse writes a credential-free tombstone", async () => {
  transport.refresh.mockRejectedValue(deviceTokenReuseError());
  await expect(session.refresh()).rejects.toMatchObject({ code: "device_token_reuse_detected" });
  expect(lastStoredJson()).toEqual({ record_version: 1, state: "cleared", cleared_reason: "token_reuse" });
});
```

- [ ] **Step 2: Run plugin tests and confirm missing controllers**

Run: `pnpm --filter @workspace/obsidian-plugin run test`

- [ ] **Step 3: Implement bounded onboarding and refresh state machines**

Validate production origin as HTTPS with no path/query/fragment/credentials; allow loopback HTTP only in explicit development build. The settings tab exposes exact server origin, editable device name, closed connection status, Login, Open browser again, Cancel pending login and Disconnect. Persist polling secret before opening `verification_uri_complete`, resume pending grants before expiry, poll no faster than server interval, and tombstone deny/expiry/cancel. At startup perform at most one resume or refresh and never start a background sync loop. Refresh writes/readbacks stable `rotation_id` before network and full successor after response. Offline preserves records; self-disconnect revokes server first and does not clear on failure.

- [ ] **Step 4: Run plugin unit/type/build and bundle-boundary gates**

Run: `pnpm --filter @workspace/obsidian-plugin run test && pnpm --filter @workspace/obsidian-plugin run type-check && pnpm --filter @workspace/obsidian-plugin run build`

Run: `uv run pytest tests/contract/api/test_plugin_authentication_bundle.py tests/contract/test_architecture_boundaries.py -q`

Inspect the production bundle for `electron`, Node built-ins, `FileSystemAdapter`, credential sentinels and source maps containing test secrets; all scans must return zero matches.

- [ ] **Step 5: Commit plugin authentication**

```powershell
git add apps/obsidian-plugin tests/contract/api/test_plugin_authentication_bundle.py
git commit -m "feat: add obsidian device authentication"
```

---

### Task 15: Prove cross-boundary security, operations and acceptance

**Files:**
- Create: `tests/contract/api/test_authentication_leakage.py`, `test_authentication_headers.py`
- Create: `tests/integration/authentication/test_authentication_key_rotation.py`, `test_emergency_reset_races.py`, `test_ambiguous_auth_commits.py`
- Create: `tests/end_to_end/authentication/full-device-onboarding.spec.ts`
- Modify: `.github/workflows/quality.yml`, `pyproject.toml`, `package.json`
- Create: `docs/operations/web-authentication-and-device-authorization.md`
- Modify: `docs/20-IMPLEMENTATION_PLAN.md`
- Create once at plan completion: `docs/handoff/2026-08-16-web-authentication-and-device-authorization.md`
- Modify only for deferred items: `docs/handoff/BACKLOG.md`

**Interfaces:**
- Produces the `authentication-test`/`authentication-e2e` quality gates, the operator runbook and exactly one plan handoff.

- [ ] **Step 1: Write failing leak, key-rotation and full-onboarding acceptance tests**

Use unique sentinel values for password, TOTP secret/code, recovery code, user code, device name and every token kind. Scan captured HTTP, diagnostics, audit safe fields, traces, OpenAPI/generated files and production Web/plugin bundles; allow secret values only in the intended in-memory/SecretStorage test double.

- [ ] **Step 2: Run focused acceptance tests and record every real failure**

Run: `uv run pytest tests/contract/api/test_authentication_leakage.py tests/integration/authentication -q`

Run: `pnpm exec playwright test tests/end_to_end/authentication/full-device-onboarding.spec.ts`

- [ ] **Step 3: Close only evidenced cross-boundary gaps and write operations docs**

The runbook must contain exact enroll/status/reset commands, key creation/permissions/rotation/removal checks, reverse-proxy header/log requirements, device revoke/recovery procedures, Argon benchmark evidence, database backup/restore implications, safe metrics and incident steps for confirmed token reuse. Update canonical Phase 2 status without copying the runbook into the handoff.

- [ ] **Step 4: Run the complete final gate from a clean generated state**

Run in order:

```powershell
uv run poe format-check
uv run poe lint
uv run poe type-check
uv run poe import-boundaries
uv run poe api-contract-check
uv run pytest tests/unit/authentication tests/unit/api_runtime tests/unit/postgresql_source_store tests/contract/api tests/integration/authentication -q
pnpm run test
pnpm run build
pnpm exec playwright test tests/end_to_end/authentication
uv run alembic upgrade head
uv run alembic -x allow_destructive=true downgrade 20260813_01
uv run alembic upgrade head
git diff --check
git status --short
```

Expected: every command exits `0`; generated artifacts stay unchanged; final status contains only the intended documentation/handoff changes.

- [ ] **Step 5: Write exactly one handoff and apply deferred rulings**

Record final commit SHA, each gate command with concise evidence, spec interpretation decisions, dependency/benchmark evidence, deferred items with one ruling each and next actions. Add one `BACKLOG.md` index line per genuinely deferred item; do not create additional handoff files for this plan.

- [ ] **Step 6: Commit final acceptance and operations artifacts**

```powershell
git add tests/contract/api tests/integration/authentication tests/end_to_end/authentication .github/workflows/quality.yml pyproject.toml package.json docs/operations/web-authentication-and-device-authorization.md docs/20-IMPLEMENTATION_PLAN.md docs/handoff/2026-08-16-web-authentication-and-device-authorization.md docs/handoff/BACKLOG.md
git commit -m "test: prove authentication acceptance"
```

## Completion Checklist

- [ ] Protected enrollment creates the first Web credential and public signup remains absent.
- [ ] Password login, generic failure, persisted throttling, 12-hour idle and seven-day absolute session expiry pass.
- [ ] Exact Origin, CSRF, cookie rotation, five-minute recent re-auth and password-change semantics pass.
- [ ] Optional TOTP, replay prevention, recovery-limited sessions and emergency reset pass.
- [ ] Device grant approval/exchange creates exactly one device and token family.
- [ ] Lost exchange/refresh responses exact-replay without duplicates or false reuse.
- [ ] Confirmed refresh reuse, Admin revoke and self-revoke invalidate access on the next request.
- [ ] OpenAPI, generated client, Web Admin and Obsidian onboarding agree on one contract.
- [ ] SecretStorage stores refresh/polling values only; terminal cleanup uses verified tombstones and preserves Vault data.
- [ ] Migration upgrade/downgrade/reflection, race, leak, lint, type, test, build and E2E gates pass.
- [ ] Operations runbook and exactly one handoff contain evidence and deferred rulings.
