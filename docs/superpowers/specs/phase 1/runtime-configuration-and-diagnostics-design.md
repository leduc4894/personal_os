# Runtime Configuration and Diagnostics Design

**Status:** Approved design
**Date:** 2026-08-13
**Phase:** Phase 1 — Bootstrap and canonical core
**Canonical plan:** `docs/20-IMPLEMENTATION_PLAN.md`
**Depends on:** `phase-one-workspace-bootstrap-design.md`

## 1. Objective

Establish one fail-closed runtime configuration, typed error, structured logging and correlation contract for the Python API, MCP and worker composition roots.

The contract must make configuration failures operationally diagnosable without exposing content, queries, vectors, credentials, secret paths or exception text. It must also establish request and W3C trace identity before transport frameworks or telemetry exporters are introduced.

## 2. Scope

This design owns:

- Immutable typed settings for the API, MCP and worker runtimes.
- Exact environment-variable naming and source precedence.
- Secret-file loading under a configured filesystem boundary.
- A transport-neutral typed application error contract.
- Safe JSON Lines logging and defense-in-depth redaction.
- Server request IDs, client request ID handling and W3C trace-context primitives.
- Context isolation for synchronous and asynchronous operations.
- A `check-runtime` command on each Python composition root.
- Unit and contract tests for configuration, errors, diagnostics and data leakage.

This design does not own:

- FastAPI middleware or HTTP response mapping.
- MCP request adapters or tool response mapping.
- Temporal workflow/activity headers or worker polling.
- OpenTelemetry SDK instrumentation, OTLP export, Alloy, Tempo, Loki or Sentry setup.
- Metrics exporters or Prometheus counters.
- Database, Redis, object-storage or provider credentials.
- Web App or Obsidian configuration and diagnostics.
- Hot reload, remote configuration, feature flags or an Admin settings UI.
- `.env`, TOML, YAML or JSON configuration files.

## 3. Selected approach

Use Pydantic Settings for typed environment configuration and Python standard-library logging behind a project-owned safe diagnostic interface.

Exact production dependencies:

| Dependency | Version | Purpose |
|---|---:|---|
| Pydantic | `2.13.4` | Immutable typed models, enums and secret values |
| Pydantic Settings | `2.14.2` | Explicit environment settings source and validation |

`pydantic-settings` must be configured so that `.env`, CLI, file-based config and unused decoding sources are disabled. Its transitive `python-dotenv` package is not an authorized configuration source.

Python standard-library `logging` remains the integration boundary for future frameworks and SDKs. Application code does not call it directly; it emits registered events through `DiagnosticLogger`. A root filter and formatter sanitize dependency logs before serialization.

Rejected alternatives:

1. Adding structlog would improve bound-logger ergonomics but would add a second public logging API while still requiring a project-owned allowlist and scrubber.
2. Implementing all settings validation manually would avoid the transitive dotenv dependency but create unnecessary parsing and schema-maintenance code.
3. Full OpenTelemetry integration would pull deployment observability concerns into Phase 1 before transports and exporters exist.

## 4. Module architecture

```text
src/personal_os/
├── runtime_configuration/
│   ├── __init__.py
│   ├── models.py
│   ├── loading.py
│   └── secret_files.py
├── error_contracts/
│   ├── __init__.py
│   ├── codes.py
│   └── exceptions.py
└── diagnostics/
    ├── __init__.py
    ├── context.py
    ├── trace_context.py
    ├── events.py
    ├── redaction.py
    └── logging.py

apps/api/src/api_runtime/
├── command.py
└── runtime_check.py

apps/mcp/src/mcp_runtime/
├── command.py
└── runtime_check.py

apps/worker/src/workflow_worker/
├── command.py
└── runtime_check.py
```

Responsibilities:

- `runtime_configuration.models` defines frozen settings and closed enums.
- `runtime_configuration.loading` snapshots approved environment keys and maps Pydantic failures to typed safe errors.
- `runtime_configuration.secret_files` owns bounded filesystem reads and returns `SecretStr`.
- `error_contracts.codes` is the registry for stable codes, categories, retryability and safe messages.
- `error_contracts.exceptions` defines transport-neutral typed exceptions and safe serialization.
- `diagnostics.context` owns operation-local correlation binding and reset.
- `diagnostics.trace_context` parses and emits W3C trace context without creating telemetry spans.
- `diagnostics.events` defines registered event names, fields and safe value types.
- `diagnostics.redaction` enforces forbidden fields and fallback sanitization.
- `diagnostics.logging` configures standard logging and exposes `DiagnosticLogger`.
- Each `runtime_check` module binds a fixed service identity and composes the shared contracts. It contains no validation or redaction rules.

The domain package does not import a composition root. Composition roots depend on the shared package and do not import one another.

## 5. Runtime settings contract

### 5.1 Schema

```text
RuntimeSettings
  service_name: ServiceName
  environment: RuntimeEnvironment
  log_level: DiagnosticLevel
  secret_root: Path
```

Closed values:

```text
ServiceName        api | mcp | worker
RuntimeEnvironment local | test | staging | production
DiagnosticLevel    debug | info | warning | error
```

`RuntimeSettings` is frozen. It contains no database URL, provider key, token, password, transport address or future service settings.

`service_name` is supplied by the composition root and cannot be overridden through environment variables.

### 5.2 Sources and precedence

```text
typed defaults < approved KNOWLEDGE_* environment variables
```

Defaults:

```text
environment  local
log_level    info
secret_root  /run/secrets
```

Approved variables:

```text
KNOWLEDGE_ENVIRONMENT
KNOWLEDGE_LOG_LEVEL
KNOWLEDGE_SECRET_ROOT
```

Names are uppercase and case-sensitive. Empty strings are invalid rather than equivalent to missing values.

The loader snapshots only the process environment needed for validation. It must never serialize, log or attach the whole environment to an exception.

Every environment variable beginning with `KNOWLEDGE_` must be recognized by the repository-wide known-name registry. Each adopted settings fragment parses only the keys it owns and ignores other registered `KNOWLEDGE_*` keys, so a composition root can combine runtime, database and object-storage settings without false unknown-key failures. A prefixed name absent from the registry remains terminal `configuration_unknown_key`; this catches misspellings instead of silently applying a default. The registry contains names only and never snapshots or exposes values.

Unrelated operating-system variables are ignored. Direct plaintext secret variables remain unauthorized. Later specs declare secret references only as known names ending in `_FILE`; a plaintext counterpart without `_FILE` is unknown and therefore rejected when it uses the `KNOWLEDGE_` prefix.

### 5.3 Disabled sources

The following sources are disabled even if a dependency supports them:

- `.env` files in the current or parent directory.
- Pydantic CLI settings.
- TOML, YAML and JSON settings files.
- Environment values decoded as arbitrary nested JSON.
- Remote secret-manager integrations.
- Import-time module globals populated from the environment.

The presence of a `.env` file must not affect a process or test.

### 5.4 Load lifecycle

A composition root parses the CLI before settings are loaded. `--help`, `--version`, no arguments and invalid CLI syntax retain their bootstrap behavior and do not read environment variables or files.

`check-runtime` performs one explicit load. The returned snapshot remains unchanged for the operation lifetime. A changed environment affects only a new process or a new explicit test load; there is no watcher, signal reload or mutable global singleton.

Validation completes before a listener, poller or external call can start. Configuration failure is terminal for that startup attempt.

The `/run/secrets` default is the production POSIX-container default. Windows contract tests and local Windows use set `KNOWLEDGE_SECRET_ROOT` to an absolute temporary or operator-owned Windows directory. The security rule is identical on both platforms even though the default deployment path is POSIX-specific.

Security controls such as forbidden diagnostic fields, redaction behavior and secret size limits are constants, not settings. Environment variables cannot weaken them.

## 6. Secret-file contract

### 6.1 Interface

```text
read_secret_file(
  secret_file: Path,
  secret_root: Path,
  maximum_size_bytes: int = 65_536,
) -> SecretStr
```

Callers receive `SecretStr`, not a plain string. Plaintext extraction occurs only at the external adapter that needs the credential and is never part of diagnostic serialization.

No concrete secret field is added by this spec. Database, object-storage, provider and alert credentials are introduced by their owning specs using this loader.

### 6.2 Path boundary

- `secret_file` and `secret_root` must be absolute paths under the host operating-system semantics.
- The loader resolves symlinks and verifies that the final target remains beneath the resolved secret root.
- Symlinks are allowed to support projected secret volumes.
- The final opened target must be a regular file, not a directory, device, socket or pipe.
- Missing or dangling paths fail closed.
- Path comparison uses the platform's canonical case behavior and path-component boundaries; string-prefix comparison is forbidden.
- The implementation must verify the opened file descriptor metadata after opening so a path swap cannot bypass the regular-file and size checks.

### 6.3 Size, encoding and normalization

- Maximum raw file size is `65,536` bytes.
- The loader performs a bounded read of at most `maximum_size_bytes + 1` bytes.
- Exactly `65,536` raw bytes may be accepted if every other rule passes; `65,537` is rejected.
- Input must be UTF-8 without a byte-order mark.
- NUL bytes and an empty result are rejected.
- Exactly one terminal `LF` or one terminal `CRLF` is removed.
- No other leading, trailing or internal whitespace is removed or normalized.
- A file containing only the removed line ending is empty and rejected.

### 6.4 Permission behavior

On POSIX, a group-writable or world-writable target is rejected. Read-only mounts such as mode `0444` remain valid. Owner write permission is allowed because some secret rotation mechanisms replace owner-writable files atomically.

This spec does not claim to validate Windows ACL semantics. Windows relies on the configured secret-root/mount boundary; ACL-hardening belongs to deployment configuration and its tests.

### 6.5 Confidential failure behavior

No error, log record, test assertion message or CLI output may contain:

- The secret value or any substring derived from it.
- The configured or resolved secret path.
- The configured or resolved secret root.
- Raw `OSError` or decoder messages.

Failures expose only the registered error code and non-sensitive classifications such as `reason=outside_root`.

## 7. Typed application errors

### 7.1 Base contract

```text
ApplicationError
  error_code: ErrorCode
  category: ErrorCategory
  is_retryable: bool
  safe_message: str
  safe_details: Mapping[str, SafeDiagnosticValue]
```

Closed categories:

```text
configuration
validation
authorization
conflict
integrity
dependency
internal
```

Initial concrete exceptions:

```text
ConfigurationError
SecretFileError
DiagnosticContextError
InternalApplicationError
```

The base contract is transport-neutral and imports no FastAPI, MCP, Temporal or provider type.

### 7.2 Initial error registry

| Error code | Category | Retryable | Safe meaning |
|---|---|---:|---|
| `configuration_invalid` | configuration | false | Runtime configuration is invalid |
| `configuration_unknown_key` | configuration | false | Runtime configuration contains an unsupported key |
| `secret_file_missing` | configuration | false | A required secret file is unavailable |
| `secret_file_outside_root` | configuration | false | A secret file is outside the configured boundary |
| `secret_file_invalid_type` | configuration | false | A secret path does not identify a regular file |
| `secret_file_insecure_permissions` | configuration | false | A secret file has unsafe write permissions |
| `secret_file_too_large` | configuration | false | A secret file exceeds the allowed size |
| `secret_file_invalid_encoding` | configuration | false | A secret file is not valid accepted text |
| `secret_file_empty` | configuration | false | A secret file contains no usable value |
| `diagnostic_context_invalid` | validation | false | Diagnostic context input is invalid |
| `diagnostic_payload_rejected` | validation | false | Diagnostic data violated the safe event contract |
| `internal_error` | internal | false | An unexpected internal error occurred |

Codes are stable public diagnostic identifiers. Renaming, reusing or changing the meaning of a code is a contract change.

### 7.3 Construction and serialization

- Category, retryability and safe message come from the registry, not caller-provided text.
- `safe_details` keys are registered per code.
- Values are limited to booleans, non-negative integers, enums, UUIDs, validated hashes, bounded ASCII tokens and bounded tuples of those values.
- Arbitrary strings, paths, URLs, exception objects and nested dictionaries are forbidden.
- A Pydantic, filesystem or parsing exception is retained only through exception chaining.
- `__cause__`, exception arguments and rejected input are excluded from serialization.
- Pydantic validation mapping may expose registered field names and an error count, but never rejected values.
- `str(error)` returns the registry's safe message and code; it does not delegate to the cause.

Expected failures retain their typed code. Unexpected exceptions are mapped at the composition boundary to `InternalApplicationError` without copying their message.

## 8. Diagnostic event contract

### 8.1 JSON Lines schema

Every emitted record is one UTF-8 JSON object followed by one newline.

Required fields:

```text
diagnostic_schema_version  integer  fixed at 1
timestamp                  string   RFC 3339 UTC with milliseconds and Z suffix
level                      enum     debug | info | warning | error | critical
service                    enum     api | mcp | worker
environment                enum|null
event                      registered bounded ASCII event code
result_code                enum     started | succeeded | failed | degraded | rejected
```

Registered optional fields:

```text
request_id
client_request_id
trace_id
workflow_id
activity
workspace_id_hash
source_id
operation
duration_ms
error_code
error_category
is_retryable
count
size_bytes
provider
model
logger_name
message_fingerprint
exception_type
stack_fingerprint
reason
configured_log_level
```

An absent optional value is serialized as `null` for correlation fields and omitted for all other optional fields. This keeps request/trace presence explicit without filling every event with unused operational fields.

There is no free-form `message` field. Application code emits a registered `event` and typed fields through `DiagnosticLogger`.

Initial registered events:

| Event | Level | Result code | Required purpose |
|---|---|---|---|
| `runtime_configuration_validated` | info | succeeded | A composition-root runtime snapshot passed validation |
| `runtime_configuration_failed` | error | failed | Settings or secret validation failed |
| `client_request_id_rejected` | warning | rejected | An untrusted client request ID failed validation |
| `trace_context_replaced` | warning | degraded | An invalid inbound trace context was replaced safely; an absent header is normal and emits no warning |
| `logging_payload_rejected` | warning | rejected | An unsafe diagnostic payload was blocked |
| `dependency_log` | normalized source level | degraded | A dependency log was reduced to safe metadata |
| `internal_error` | error | failed | An unexpected internal exception crossed a composition boundary |

Adding an event or field requires a reviewed registry change and leakage tests. Callers cannot invent event names at runtime.

### 8.2 Output routing

- `debug`, `info` and `warning` records go to stdout.
- `error` and `critical` records go to stderr.
- A record is emitted to exactly one stream.
- Handlers use the same JSON serializer and redaction pipeline in local, test, staging and production.
- Logging configuration is idempotent and must not duplicate handlers or events.

### 8.3 Safe values

Safe values include:

- Boolean values.
- Non-negative bounded counts, sizes and millisecond durations.
- Closed enums.
- Canonical UUIDs owned by the application.
- Lowercase hexadecimal trace, span and digest identifiers of exact registered lengths.
- Bounded ASCII tokens matching the registered field grammar.

`workspace_id_hash` is the first 16 lowercase hexadecimal characters of SHA-256 over the canonical workspace UUID string. It is a low-cardinality correlation value, not a replacement for authorization or an input for content hashing.

Source IDs may be logged only as stable opaque UUIDs. Filesystem paths, aliases and source titles are not substitutes.

### 8.4 Dependency logging

Standard-library logging is configured at the root so a future dependency cannot bypass the output serializer merely by using `logging.getLogger()`.

Dependency `LogRecord.getMessage()` output is not forwarded. It is converted to:

```text
event                dependency_log
logger_name          validated bounded logger token
message_fingerprint  truncated SHA-256 of the original rendered message
```

The original dependency message, arguments, traceback and exception text are discarded before output. Known libraries may receive explicit safe adapters in their owning specs; this design provides no blanket message allowlist.

## 9. Redaction and sensitive-data rules

### 9.1 Safe-by-construction boundary

Application code constructs registered event types rather than arbitrary dictionaries. Each event declares its allowed keys and value validators. Unknown keys are not serialized.

Forbidden key families are matched after lowercasing and removing punctuation and separators:

```text
content
body
query
excerpt
citation_text
prompt
completion
token
secret
password
credential
authorization
cookie
signed_url
path
vector
embedding
traceback
exception_message
```

This list applies recursively and cannot be changed through runtime settings.

### 9.2 Defense-in-depth scrubber

Before JSON serialization, the scrubber recursively inspects bounded mappings and sequences and removes or replaces values matching known sensitive forms:

- Bearer credentials.
- JSON Web Tokens.
- PEM private-key blocks.
- URLs containing user information.
- Presigned URL signature or credential query parameters.
- Values attached to forbidden normalized keys.

The scrubber is a final defense, not the primary security boundary. It does not claim to identify every possible high-entropy secret.

### 9.3 Rejected diagnostic payloads

When a caller or dependency supplies an unsafe payload:

1. The unsafe event and fields are not emitted.
2. A constant minimal `logging_payload_rejected` event is emitted through a non-recursive fallback serializer.
3. A counter hook is invoked for future metrics integration.
4. No logging exception escapes into the application operation.

The fallback event contains no rejected key, value, type representation or exception message. Failure in logging never replaces the original application error or exit code.

### 9.4 Exception diagnostics

Expected `ApplicationError` diagnostics contain its code, category, retryability and registered safe details. They contain no traceback.

Unexpected exceptions contain only:

- `error_code=internal_error`.
- A normalized exception type identifier.
- A stack fingerprint derived from code locations after path normalization.
- Correlation fields already present in the diagnostic context.

Exception message, exception arguments, local variables, raw stack text and filesystem paths are not emitted to stdout or stderr. A later Sentry integration may send scrubbed stack structure through a separate errors-only boundary defined by its own spec.

## 10. Request and trace identity

### 10.1 Diagnostic context

```text
TraceContext
  trace_id: TraceId
  remote_parent_span_id: SpanId | None
  local_span_id: SpanId
  trace_flags: TraceFlags

DiagnosticContext
  request_id: UUID
  client_request_id: UUID | None
  trace: TraceContext
  workflow_id: SafeToken | None
```

The context is immutable. Request IDs and trace IDs are diagnostic correlation values, never authorization, idempotency or canonical entity identifiers.

### 10.2 Request IDs

- Every public or explicit operation boundary creates a server-owned UUIDv7 request ID.
- A supplied client request ID is retained only in `client_request_id` after strict canonical UUID validation.
- Invalid client IDs are discarded and produce a safe `client_request_id_rejected` event without echoing the input.
- A server request ID is never replaced by a client value.
- Downstream application calls propagate the server request ID.

### 10.3 W3C trace context

This spec supports W3C `traceparent` version `00`:

```text
00-<32 lowercase hexadecimal trace id>-<16 lowercase hexadecimal parent span id>-<2 hexadecimal flags>
```

Validation rejects:

- Incorrect field count or lengths.
- Non-hexadecimal or uppercase identifiers.
- Version `ff` or any unsupported version.
- All-zero trace or parent span IDs.
- Flags outside the version-00 two-character field.

A missing or invalid header creates a cryptographically random 128-bit trace ID and 64-bit local span ID. An absent header is normal and emits no warning. A present but invalid header emits `trace_context_replaced`; invalid input is not copied to an error or log.

A valid inbound header preserves its trace ID and flags, records the remote parent span ID internally and creates a new random local span ID. Outbound propagation emits version `00` using the current trace ID, local span ID and flags.

This module does not create OpenTelemetry spans. When the SDK is introduced, its adapter must adopt this trace context and must not create a competing correlation ID.

### 10.4 Context isolation

The active value is held in `ContextVar[DiagnosticContext | None]`.

`bind_diagnostic_context()` stores the `ContextVar` token and resets it in `finally`. Nested binding restores its parent. Concurrent asyncio tasks created inside different bound operations retain their own copied contexts and cannot observe one another.

Threads and executors do not receive implicit propagation from this API. A caller must explicitly copy an approved context. A background task that may outlive its request starts detached or binds a durable workflow context.

Logs outside an operation serialize `request_id=null` and `trace_id=null`; startup noise does not receive fake identifiers.

HTTP, MCP and Temporal integration is deferred, but their later adapters must use these primitives and test propagation at their boundaries.

## 11. Composition-root command contract

All three Python shells add an explicit subcommand:

```text
personal-api check-runtime
personal-mcp check-runtime
personal-worker check-runtime
```

Existing behavior remains unchanged:

- `--help` exits `0` without reading environment or files.
- `--version` exits `0` without reading environment or files.
- No arguments print help and exit `0` without reading environment or files.
- Invalid syntax exits `2` without reading environment or files.

`check-runtime` executes:

```text
create server request and trace context
→ bind fixed service identity
→ load and validate immutable settings
→ configure safe JSON logging
→ emit runtime_configuration_validated
→ exit 0
```

On configuration or secret failure it emits one `runtime_configuration_failed` event to stderr and exits `78`. On an unexpected internal failure it emits one safe `internal_error` event and exits `70`.

If settings fail before normal logging is available, an emergency serializer uses the same schema with the known service, `environment=null` and the newly created correlation IDs. It does not retry settings loading to construct the error.

Successful output does not display the secret root or a dump of settings. It may contain service, validated environment, log level and correlation fields because those values are registered safe fields.

The command starts no daemon, listener, poller, provider connection or telemetry exporter.

## 12. Failure behavior

- Configuration validation is fail-closed and completes before external activity.
- Unknown prefixed variables are terminal rather than warnings.
- Missing, unreadable, invalid or out-of-boundary secret files are never replaced by defaults.
- Raw Pydantic, filesystem, Unicode and dependency exceptions never cross the safe error boundary.
- Diagnostic context parsing failure creates a fresh trace where allowed; it never trusts malformed external identity.
- A logging or scrubber bug cannot change the original typed error or process exit code.
- Logger configuration failure uses the emergency serializer and exits `70`.
- A required check that only warns, skips a leak assertion or collects zero tests is not passing.

Process exit codes:

| Exit code | Meaning |
|---:|---|
| `0` | Command completed successfully |
| `2` | CLI syntax or argument error |
| `70` | Unexpected internal software error |
| `78` | Runtime configuration or secret validation error |

## 13. Test strategy

### 13.1 Layout

```text
tests/unit/runtime_configuration/
tests/unit/error_contracts/
tests/unit/diagnostics/
tests/contract/test_runtime_check_commands.py
tests/contract/test_sensitive_diagnostics.py
```

Tests use real filesystem semantics through temporary directories and real subprocess boundaries for CLI output. They do not call a network service or require a container.

### 13.2 Settings cases

- Defaults and every exact approved override.
- Case-sensitive names, empty values and invalid enum values.
- Unknown `KNOWLEDGE_*` variables.
- Relative secret roots.
- A nearby `.env` containing sentinel values has no effect.
- TOML/YAML/JSON and Pydantic CLI settings are not discovered.
- Plaintext secret-like `KNOWLEDGE_*` variables are unknown and rejected.
- Importing modules and invoking help/version/no-args does not inspect environment or filesystem settings.
- The returned model is frozen and an existing snapshot does not change when the process environment mutates.

### 13.3 Secret-file cases

- Valid UTF-8 with no newline, one LF and one CRLF.
- Leading, trailing and internal whitespace preservation.
- Empty, newline-only, NUL, BOM and invalid UTF-8 input.
- Exactly `65,536` bytes and `65,537` bytes.
- Missing file, directory, device-like target where the platform supports one and dangling symlink.
- Symlink whose target remains inside the root and symlink escaping the root.
- Path-component boundary cases such as `secrets` versus `secrets-backup`.
- File replacement/descriptor verification test where the platform provides the required primitives.
- POSIX group/world-writable rejection and `0444` acceptance.
- Windows tests explicitly document that ACL validation is outside this loader.
- Every failure is scanned to prove that sentinel secret values and full paths are absent.

### 13.4 Error cases

- Error codes are unique and registry-complete.
- Each code has one category, retryability value and constant safe message.
- Safe-detail keys and value types are enforced.
- Exception chaining retains the cause for debugging without serialization.
- `str(error)`, JSON serialization and repr snapshots contain no cause message or rejected value.
- Unexpected exception mapping produces a stable safe shape without exception text.

### 13.5 Logging and leakage cases

- One JSON object per line and exact schema version.
- UTC millisecond timestamps and correct stdout/stderr level routing.
- Idempotent logging setup does not duplicate events.
- Registered event fields serialize deterministically.
- Dependency messages become fingerprints, not text.
- A malicious corpus covers raw content, queries, excerpts, tokens, JWTs, private keys, URL credentials, presigned URLs, vectors, nested containers and exceptions that echo input.
- Forbidden-key normalization covers case and punctuation variants.
- Rejected payloads produce one safe fallback event without recursion.
- A logging failure does not mask an active `ApplicationError`.
- Captured stdout/stderr and serialized errors contain none of the corpus sentinel values.

### 13.6 Correlation cases

- Server request IDs are UUIDv7 and never equal a supplied client ID by assignment.
- Valid client UUIDs are stored separately; invalid values are discarded without echo.
- Valid version-00 traceparents continue trace IDs and create new local span IDs.
- Missing, malformed, uppercase, zero and unsupported traceparents create fresh contexts.
- Generated trace/span identifiers are nonzero and have exact widths.
- Outbound traceparent round-trips the current trace identity.
- Nested bindings restore the parent context after success and exception.
- Concurrent asyncio operations do not leak request or trace IDs.
- Detached background context and explicit copied-context behavior are tested.

### 13.7 Composition-root cases

- All three `check-runtime` commands have equivalent JSON shapes.
- Success exits `0`; invalid configuration exits `78`; injected unexpected failure exits `70`.
- CLI syntax still exits `2` without a stack trace.
- Help/version/no-args retain the bootstrap no-side-effect contract.
- Success and failure emit no settings dump or secret-root path.
- The full suite runs on Ubuntu and Windows; POSIX-only permission assertions use an explicit platform marker rather than silently passing.

Coverage remains diagnostic only. Mutation testing remains deferred.

## 14. Acceptance criteria

The design is implemented only when all of the following pass on the same final commit:

1. API, MCP and worker use one shared settings, error and diagnostic contract.
2. Settings are immutable snapshots and no runtime module reads configuration during import.
3. Only typed defaults and exact approved `KNOWLEDGE_*` environment variables affect settings.
4. `.env`, CLI settings and TOML/YAML/JSON configuration have no effect.
5. Unknown prefixed variables fail closed with a typed safe error.
6. Secret values are read only from bounded files under the resolved secret root.
7. Secret values, paths and roots never appear in error serialization, stdout or stderr.
8. Every error code has stable category, retryability and safe message metadata.
9. Every application log is schema-versioned JSON Lines and contains no free-form message.
10. Unsafe diagnostic payloads are rejected without failing the application operation.
11. Expected and unexpected exception diagnostics contain neither raw message nor traceback.
12. Every operation boundary owns a new UUIDv7 request ID; client IDs remain separate.
13. Trace context accepts strict W3C version `00`, creates secure fallback identity and propagates outbound context.
14. Diagnostic context does not leak across nested or concurrent operations.
15. `check-runtime` returns `0`, `78`, `70` and `2` according to the documented contract for all three shells.
16. Help, version, no-args and syntax-error paths still do not read settings or secret files.
17. No framework SDK, OpenTelemetry SDK/exporter, provider client or TypeScript runtime configuration is added.
18. Leak corpus, strict typing, lint, unit/contract tests and `uv run poe verify` pass on Ubuntu and Windows.

## 15. Expected deliverables

```text
src/personal_os/runtime_configuration/
src/personal_os/error_contracts/
src/personal_os/diagnostics/
apps/api/src/api_runtime/runtime_check.py
apps/mcp/src/mcp_runtime/runtime_check.py
apps/worker/src/workflow_worker/runtime_check.py
tests/unit/runtime_configuration/
tests/unit/error_contracts/
tests/unit/diagnostics/
tests/contract/test_runtime_check_commands.py
tests/contract/test_sensitive_diagnostics.py
```

The root and composition-root READMEs document approved variables, secret-file rules, safe output and exit codes. Canonical observability documentation remains authoritative for later Loki, Tempo, Alloy and Sentry deployment behavior.
