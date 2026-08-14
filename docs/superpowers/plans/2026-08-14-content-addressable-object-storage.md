# Content-Addressable Object Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 1 provider-neutral canonical-object contract and a bounded, fail-closed Cloudflare R2 adapter that returns a verified receipt only after exact-byte SHA-256, size and media-type verification.

**Architecture:** Keep content identity, keys, receipts and the async port in `personal_os.object_storage`; put every provider SDK import in a new `r2-object-storage` uv workspace member. The adapter spools input and reads to disk under fixed resource limits, uses conditional single-part R2 writes, independently full-reads stored bytes before trust, and exposes only a verified local reader. PostgreSQL publication, HTTP upload APIs, Workers, multipart and deletion remain absent.

**Tech Stack:** Python 3.14.6, uv 0.11.32, Pydantic 2.13.4, aiobotocore 3.9.0, types-aiobotocore[s3] 3.9.0, pytest 9.1.1, pytest-asyncio 1.4.0, Ruff 0.15.22, mypy 2.3.0, Cloudflare R2 S3 API.

## Global Constraints

- Implement `docs/superpowers/specs/content-addressable-object-storage-design.md` at commit `b53a4e5`; do not expand its scope.
- The only canonical key grammar is `objects/sha256/{first_2}/{next_2}/{sha256}`.
- SHA-256 is the sole content identity; MD5 is transport checking only and ETag is an opaque conditional token.
- Accept exact sizes `0..104_857_600` bytes; use 1 MiB chunks, four in-flight operations, 512 MiB process spool budget and a 2 GiB filesystem reserve.
- Input receive deadline is 10 minutes; one logical R2 operation deadline is 5 minutes; retry has at most three total attempts.
- Upload is one conditional `PutObject`; no transfer manager, multipart, overwrite, list, copy, delete, public URL or presigned operation may enter production code.
- A client digest is only a claim. Only backend hashing plus a full R2 read can create `VerifiedObjectReceipt`.
- A consumer receives no byte until a complete R2 read has passed digest, size and media verification.
- `personal_os` must not import `r2_object_storage`, `aiobotocore`, `botocore`, `aiohttp` or another infrastructure SDK.
- Production and live-test R2 configuration use separate private buckets and secret files; no `.env`, ambient AWS credential chain, plaintext secret environment value or production secret in CI.
- Never log raw content, full digest/key, bucket, endpoint, media type, source path, secret path/value, signed URL, provider headers/body/request ID or exception text.
- Use TDD for every behavior task. Run the named failing test before implementation, then the focused green test, then the broader affected suite before each commit.
- Preserve all unrelated user changes. Use exact-path cleanup only in the live test harness.

---

## File Structure

### Core contracts

```text
src/personal_os/object_storage/
├── __init__.py       Public provider-neutral exports only
├── contracts.py      Immutable values, reader protocol and CanonicalObjectStore port
├── errors.py         ObjectStorageError and safe input reason tokens
└── keys.py           SHA-256, media-type and canonical-key parsing/derivation
```

### Concrete R2 workspace member

```text
packages/r2-object-storage/
├── pyproject.toml    Exact dependencies and uv-build configuration
└── src/r2_object_storage/
    ├── __init__.py       Export R2S3ObjectStore, settings loader and runtime check
    ├── adapter.py        Store/resolve/verify/read orchestration and single-flight
    ├── client.py         aiobotocore lifecycle and narrow typed S3 boundary
    ├── error_mapping.py  R2/S3 classification, typed mapping and retry policy
    ├── metrics.py        Required low-cardinality metric sink contract/recorder
    ├── runtime_check.py  Read-only HeadBucket diagnostic command
    ├── settings.py       Owned settings fragment and secret composition
    └── spool.py          Secure files, hashing, reservation and stale janitor
```

### Tests and operations

```text
tests/unit/object_storage/
├── test_contract_values.py
├── test_error_diagnostics_contract.py
├── test_r2_error_mapping.py
├── test_r2_settings.py
└── test_spool_manager.py
tests/contract/object_storage/
├── scripted_s3.py
├── test_r2_adapter_contract.py
├── test_r2_adapter_resource_contract.py
└── test_r2_runtime_contract.py
tests/integration/r2_object_storage/
├── conftest.py
└── test_live_r2_adapter.py
docs/operations/object-storage.md
.github/workflows/object-storage-live.yml
```

`scripted_s3.py` is a deterministic narrow fake of the adapter's own `S3ClientProtocol`, not an R2/S3 behavioral emulator. The live harness is the only code allowed to call test-only exact-key deletion.

---

### Task 1: Add the R2 workspace member and enforce infrastructure boundaries

**Files:**
- Create: `packages/r2-object-storage/pyproject.toml`
- Create: `packages/r2-object-storage/src/r2_object_storage/__init__.py`
- Create: `packages/r2-object-storage/src/r2_object_storage/py.typed`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.importlinter`
- Modify: `tests/contract/test_dependency_pins.py`
- Modify: `tests/contract/test_architecture_boundaries.py`
- Modify: `tests/contract/test_python_workspace.py`

**Interfaces:**
- Consumes: existing `knowledge-core==0.1.0` workspace distribution.
- Produces: installable `r2-object-storage==0.1.0` / `r2_object_storage` package with `aiobotocore==3.9.0`; root dev tooling includes `pytest-asyncio==1.4.0`; root quality gates include `packages/r2-object-storage/src`.

- [ ] **Step 1: Write failing workspace and boundary tests**

Add assertions that the manifest is discovered, dependencies are exact, the installed import works outside the repository, and core cannot import the provider packages:

```python
def test_r2_workspace_member_has_exact_sdk_pins() -> None:
    manifest = tomllib.loads(
        (REPO_ROOT / "packages/r2-object-storage/pyproject.toml").read_text("utf-8")
    )
    assert manifest["project"]["dependencies"] == [
        "aiobotocore==3.9.0",
        "knowledge-core==0.1.0",
    ]
    assert manifest["dependency-groups"]["dev"] == [
        "types-aiobotocore[s3]==3.9.0"
    ]


def test_r2_package_imports_outside_repository(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import r2_object_storage"],
        cwd=tmp_path,
        check=False,
    )
    assert completed.returncode == 0
```

Extend `PYTHON_SOURCE_ROOTS` with `packages/r2-object-storage/src` and assert `.importlinter` names `r2_object_storage`, `aiobotocore`, `botocore` and `aiohttp` in the core-forbidden contract. Add a second forbidden contract whose source is `r2_object_storage` and whose forbidden modules include the three composition roots, FastAPI, SQLAlchemy, Psycopg, Temporal, Qdrant, Neo4j and Redis; the adapter may import only core contracts plus its approved R2 SDK graph.
Extend `_iter_python_manifests()` to discover `packages/*/pyproject.toml`, and assert the root dev group contains the exact stable Python-3.14-compatible pin `pytest-asyncio==1.4.0`.

- [ ] **Step 2: Run tests and verify the package is absent**

Run:

```powershell
uv run pytest tests/contract/test_dependency_pins.py tests/contract/test_architecture_boundaries.py tests/contract/test_python_workspace.py -q
```

Expected: FAIL because `packages/r2-object-storage/pyproject.toml` and the installed package do not exist.

- [ ] **Step 3: Create the exact package and wire all root tool paths**

Create:

```toml
[project]
name = "r2-object-storage"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = [
  "aiobotocore==3.9.0",
  "knowledge-core==0.1.0",
]

[dependency-groups]
dev = ["types-aiobotocore[s3]==3.9.0"]

[build-system]
requires = ["uv_build==0.11.32"]
build-backend = "uv_build"
```

Add `packages/r2-object-storage` to `[tool.uv.workspace].members`, add `pytest-asyncio==1.4.0` to the root dev group, and include the member source in Ruff, mypy, coverage and architecture scans. The existing root `knowledge-core = { workspace = true }` source declaration resolves the member's core dependency; do not add an unused source entry for the R2 distribution. Keep `__init__.py` empty until Task 2 provides real exports.

- [ ] **Step 4: Lock, install and run the focused contracts**

Run:

```powershell
uv lock
uv sync --all-packages --frozen
uv run pytest tests/contract/test_dependency_pins.py tests/contract/test_architecture_boundaries.py tests/contract/test_python_workspace.py -q
uv run lint-imports
uv run mypy src packages/r2-object-storage/src
```

Expected: all commands exit `0`; `uv.lock` contains aiobotocore/types-aiobotocore 3.9.0 and one compatible locked botocore range resolution.

- [ ] **Step 5: Commit the workspace boundary**

```powershell
git add pyproject.toml uv.lock .importlinter packages/r2-object-storage tests/contract/test_dependency_pins.py tests/contract/test_architecture_boundaries.py tests/contract/test_python_workspace.py
git commit -m "build: add isolated R2 adapter workspace"
```

### Task 2: Implement canonical digest, key, media and verified-receipt contracts

**Files:**
- Create: `src/personal_os/object_storage/__init__.py`
- Create: `src/personal_os/object_storage/keys.py`
- Create: `src/personal_os/object_storage/contracts.py`
- Create: `tests/unit/object_storage/test_contract_values.py`

**Interfaces:**
- Consumes: Python standard-library `hashlib`, `datetime`, `Protocol`, `AsyncIterable` and `AsyncContextManager` only.
- Produces: `ContentDigest`, `CanonicalObjectKey`, `CanonicalMediaType`, `ExpectedObject`, `VerifiedObjectReceipt`, `VerificationMethod`, `VerifiedObjectReader`, `CanonicalObjectStore`, `derive_canonical_object_key()`.

- [ ] **Step 1: Write failing value-object tests**

Cover exact lowercase digest grammar, derived segments, invalid paths, MIME parameters and frozen values:

```python
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_derives_only_canonical_key() -> None:
    digest = ContentDigest.parse(EMPTY_SHA256)
    assert derive_canonical_object_key(digest).value == (
        "objects/sha256/e3/b0/" + EMPTY_SHA256
    )


@pytest.mark.parametrize(
    "value",
    ["", "SHA256:" + EMPTY_SHA256, EMPTY_SHA256.upper(), "f" * 63, "g" * 64],
)
def test_rejects_noncanonical_digest(value: str) -> None:
    with pytest.raises(ValueError, match="digest"):
        ContentDigest.parse(value)


@pytest.mark.parametrize("value", ["text/plain; charset=utf-8", "TEXT/PLAIN", "*/json"])
def test_rejects_noncanonical_media_type(value: str) -> None:
    with pytest.raises(ValueError, match="media type"):
        CanonicalMediaType.parse(value)
```

Add an async structural test with a small in-test `CanonicalObjectStore` implementation so every protocol signature is type-checkable.

- [ ] **Step 2: Run the value tests and verify missing imports**

Run:

```powershell
uv run pytest tests/unit/object_storage/test_contract_values.py -q
```

Expected: collection FAIL with `ModuleNotFoundError: personal_os.object_storage`.

- [ ] **Step 3: Implement the immutable provider-neutral types**

Use frozen slotted dataclasses and closed enums. Field names are exact: `ContentDigest.hexadecimal`, `CanonicalObjectKey.value`, `CanonicalMediaType.value`, `ExpectedObject.content_digest/size_bytes/media_type`, and the receipt fields from the design. The reader signature is `async read(size_bytes: int = 1_048_576) -> bytes` plus async iteration. The port signatures must be exactly:

```python
class CanonicalObjectStore(Protocol):
    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None: ...

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt: ...

    async def verify_existing_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt: ...

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AsyncContextManager[VerifiedObjectReader]: ...
```

`VerifiedObjectReader.read(size_bytes)` rejects negative or greater-than-1-MiB reads at the contract boundary. Export only these provider-neutral names from `personal_os.object_storage`.

- [ ] **Step 4: Run focused unit, Ruff and mypy checks**

```powershell
uv run pytest tests/unit/object_storage/test_contract_values.py -q
uv run ruff check src/personal_os/object_storage tests/unit/object_storage/test_contract_values.py
uv run mypy src/personal_os/object_storage
```

Expected: all exit `0`.

- [ ] **Step 5: Commit the canonical contracts**

```powershell
git add src/personal_os/object_storage tests/unit/object_storage/test_contract_values.py
git commit -m "feat: define canonical object storage contracts"
```

### Task 3: Extend the closed error and diagnostic registries

**Files:**
- Create: `src/personal_os/object_storage/errors.py`
- Create: `tests/unit/object_storage/test_error_diagnostics_contract.py`
- Modify: `src/personal_os/error_contracts/codes.py`
- Modify: `src/personal_os/error_contracts/exceptions.py`
- Modify: `src/personal_os/diagnostics/events.py`
- Modify: `tests/unit/error_contracts/test_application_errors.py`
- Modify: `tests/unit/diagnostics/test_event_values.py`
- Modify: `tests/contract/test_sensitive_diagnostics.py`

**Interfaces:**
- Consumes: `ApplicationError`, `ErrorCode`, `EventName`, `SafeToken` and the closed safe-diagnostic scalar union.
- Produces: all nine object-storage error codes, `ObjectStorageError`, `ObjectDigestPrefix`, five registered events and exact safe field allowlists.

- [ ] **Step 1: Write failing registry completeness and leakage tests**

Assert exact category/retryability and event fields:

```python
def test_object_storage_error_registry_is_exact() -> None:
    expected = {
        ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID: (ErrorCategory.CONFIGURATION, False),
        ErrorCode.OBJECT_STORAGE_INPUT_INVALID: (ErrorCategory.VALIDATION, False),
        ErrorCode.OBJECT_STORAGE_BUSY: (ErrorCategory.DEPENDENCY, True),
        ErrorCode.OBJECT_STORAGE_UNAVAILABLE: (ErrorCategory.DEPENDENCY, True),
        ErrorCode.OBJECT_STORAGE_ACCESS_DENIED: (ErrorCategory.AUTHORIZATION, False),
        ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID: (ErrorCategory.INTEGRITY, False),
        ErrorCode.OBJECT_STORAGE_OBJECT_MISSING: (ErrorCategory.INTEGRITY, False),
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED: (ErrorCategory.INTEGRITY, False),
        ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT: (ErrorCategory.CONFLICT, False),
    }
    for code, (category, retryable) in expected.items():
        error = ObjectStorageError(code)
        assert (error.category, error.is_retryable) == (category, retryable)
```

Feed sentinels through a chained provider exception and object-storage event fields; assert no full hash, key, bucket, endpoint, filename, body, header or exception text reaches JSON output.

- [ ] **Step 2: Run tests and confirm registry members are missing**

```powershell
uv run pytest tests/unit/object_storage/test_error_diagnostics_contract.py tests/unit/error_contracts/test_application_errors.py tests/unit/diagnostics/test_event_values.py tests/contract/test_sensitive_diagnostics.py -q
```

Expected: FAIL because the new enum members and exception do not exist.

- [ ] **Step 3: Add exact errors, reasons and events**

Add the nine codes exactly as named in the design. `ObjectStorageError.allowed_codes` contains only those codes. Define safe input reasons as closed `SafeToken` constants:

```python
SIZE_OUT_OF_RANGE = SafeToken.parse("size_out_of_range")
SIZE_MISMATCH = SafeToken.parse("size_mismatch")
DIGEST_MISMATCH = SafeToken.parse("digest_mismatch")
MEDIA_TYPE_INVALID = SafeToken.parse("media_type_invalid")
STREAM_INVALID = SafeToken.parse("stream_invalid")
```

Register events `object_storage_operation_succeeded`, `object_storage_operation_failed`, `object_storage_object_deduplicated`, `object_storage_integrity_failed` and `object_storage_spool_cleanup_degraded`. Add a dedicated `ObjectDigestPrefix` safe value that accepts exactly 12 lowercase hexadecimal characters, and include it in `SafeDiagnosticScalar`; do not weaken or reuse the existing 16-character `ShortDigest` contract. Only the registered `object_digest_prefix` log field accepts the new type, and metrics never accept it.

Use these exact registry field contracts:

```text
operation_succeeded: required operation,duration_ms,size_bytes,attempt_count,provider
operation_failed: required operation,duration_ms,attempt_count,provider,error_code,error_category,is_retryable; optional size_bytes,object_digest_prefix
object_deduplicated: required operation,duration_ms,size_bytes,attempt_count,provider; optional object_digest_prefix
integrity_failed: required operation,duration_ms,attempt_count,provider,error_code,error_category,is_retryable; optional size_bytes,object_digest_prefix
spool_cleanup_degraded: required operation,count; optional error_code,error_category,is_retryable
```

Error safe-detail allowlists are exact: configuration accepts `count,field_names`; invalid input accepts `reason`; every other object-storage code, including busy, accepts no detail field. Adapter code supplies `provider=SafeToken.parse("r2")` and a registered operation token, never caller text.

- [ ] **Step 4: Run all error/diagnostic and leakage tests**

```powershell
uv run pytest tests/unit/error_contracts tests/unit/diagnostics tests/unit/object_storage/test_error_diagnostics_contract.py tests/contract/test_sensitive_diagnostics.py -q
uv run mypy src/personal_os/error_contracts src/personal_os/diagnostics src/personal_os/object_storage
```

Expected: all exit `0`; no sentinel appears in captured diagnostics.

- [ ] **Step 5: Commit the closed registries**

```powershell
git add src/personal_os/error_contracts src/personal_os/diagnostics src/personal_os/object_storage/errors.py tests/unit/error_contracts tests/unit/diagnostics tests/unit/object_storage/test_error_diagnostics_contract.py tests/contract/test_sensitive_diagnostics.py
git commit -m "feat: register object storage errors and diagnostics"
```

### Task 4: Compose strict environment names and R2 secret-file settings

**Files:**
- Create: `src/personal_os/runtime_configuration/environment_names.py`
- Create: `packages/r2-object-storage/src/r2_object_storage/settings.py`
- Create: `tests/unit/object_storage/test_r2_settings.py`
- Modify: `src/personal_os/runtime_configuration/loading.py`
- Modify: `migrations/database_migration_runtime.py`
- Modify: `tests/unit/runtime_configuration/test_settings_loading.py`
- Modify: `tests/unit/migrations/test_database_migration_runtime.py`

**Interfaces:**
- Consumes: `RuntimeEnvironment`, `read_secret_file()`, `ConfigurationError`, `ObjectStorageError`.
- Produces: repository-wide `KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES`, frozen `ObjectStorageSettings`, `LoadedR2Credentials`, `load_object_storage_settings()`.

- [ ] **Step 1: Write failing cross-fragment and R2 settings tests**

Test that registered foreign keys are ignored by a fragment while typos/plaintext secrets still fail:

```python
def test_runtime_loader_ignores_registered_r2_keys(tmp_path: Path) -> None:
    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
            "KNOWLEDGE_R2_BUCKET_NAME": "knowledge-test",
        },
    )
    assert settings.secret_root == tmp_path


def test_r2_loader_rejects_plaintext_secret_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_object_storage_settings(
            environ={
                "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
                "KNOWLEDGE_R2_SECRET_ACCESS_KEY": "do-not-emit-secret",
            }
        )
    assert raised.value.error_code is ErrorCode.CONFIGURATION_UNKNOWN_KEY
```

Also cover exact endpoint, bucket, relative secret filename, missing/insecure secret file, production/test snapshots, frozen models, `.env`/ambient AWS variables having no effect, and errors never rendering a value/path.

- [ ] **Step 2: Run settings tests and verify cross-fragment failure**

```powershell
uv run pytest tests/unit/runtime_configuration/test_settings_loading.py tests/unit/migrations/test_database_migration_runtime.py tests/unit/object_storage/test_r2_settings.py -q
```

Expected: FAIL because runtime rejects the registered R2 name and R2 settings do not exist.

- [ ] **Step 3: Implement name registry and settings composition**

Define fixed sets for runtime, database and object storage, then their union:

```python
KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    RUNTIME_ENVIRONMENT_NAMES
    | DATABASE_ENVIRONMENT_NAMES
    | OBJECT_STORAGE_ENVIRONMENT_NAMES
)
```

Both current loaders count unknown keys against the union but build model values only from their owned mapping. `ObjectStorageSettings` validates `https://<32 lowercase hex>.r2.cloudflarestorage.com`, a 3–63-character lowercase/hyphen bucket, absolute existing spool root and single relative credential filenames. `load_object_storage_settings()` maps Pydantic failures to `object_storage_configuration_invalid`, then reads both secrets with `read_secret_file()` into a separate short-lived frozen credentials value.

- [ ] **Step 4: Run settings, migration and leakage suites**

```powershell
uv run pytest tests/unit/runtime_configuration tests/unit/migrations/test_database_migration_runtime.py tests/unit/object_storage/test_r2_settings.py tests/contract/test_sensitive_diagnostics.py -q
uv run mypy src/personal_os/runtime_configuration migrations/database_migration_runtime.py packages/r2-object-storage/src/r2_object_storage/settings.py
```

Expected: all exit `0`; existing plaintext `KNOWLEDGE_DATABASE_PASSWORD` rejection remains green.

- [ ] **Step 5: Commit configuration composition**

```powershell
git add src/personal_os/runtime_configuration migrations/database_migration_runtime.py packages/r2-object-storage/src/r2_object_storage/settings.py tests/unit/runtime_configuration tests/unit/migrations/test_database_migration_runtime.py tests/unit/object_storage/test_r2_settings.py
git commit -m "feat: load isolated R2 settings from secret files"
```

### Task 5: Build the secure bounded spool manager

**Files:**
- Create: `packages/r2-object-storage/src/r2_object_storage/spool.py`
- Create: `tests/unit/object_storage/test_spool_manager.py`

**Interfaces:**
- Consumes: async byte streams, `ContentDigest`, `ObjectStorageError`.
- Produces: `SpoolLimits`, `SpoolManager`, `HashedSpool`, `VerificationSpool`, `SpoolCleanupSummary`; async context-managed reservations and exact cleanup.

- [ ] **Step 1: Write failing spool behavior tests**

Use `tmp_path`, injected disk-usage/clock functions and async generators. Cover zero bytes, multi-chunk SHA-256/MD5, declared-size mismatch, 100 MiB boundary with repeated fixed chunks, overflow, four permits, 512 MiB reservation, 2 GiB reserve, the exact 10-minute receive deadline, exception/cancellation cleanup, file mode, symlink/non-regular rejection and janitor exactness.

```python
@pytest.mark.asyncio
async def test_spools_and_hashes_without_buffering_complete_body(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    async with manager.receive_stream(chunks(b"abc", b"def"), 6) as spool:
        assert spool.content_digest.hexadecimal == hashlib.sha256(b"abcdef").hexdigest()
        assert spool.size_bytes == 6
        assert spool.path.read_bytes() == b"abcdef"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cancellation_releases_reservation_and_file(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)

    async def receive() -> None:
        async with manager.receive_stream(blocking_stream(), 100):
            pass

    task = asyncio.create_task(receive())
    await stream_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.reserved_size_bytes == 0
    assert list(tmp_path.iterdir()) == []
```

Define `chunks()`, `blocking_stream()` and `build_spool_manager()` as test-local helpers in this file. The builder injects a fixed clock and disk-usage function; production `SpoolManager` has no `for_testing` method.

- [ ] **Step 2: Run tests and verify spool types are missing**

```powershell
uv run pytest tests/unit/object_storage/test_spool_manager.py -q
```

Expected: collection FAIL because `r2_object_storage.spool` does not exist.

- [ ] **Step 3: Implement fixed limits, exclusive files and bounded janitor**

Use these immutable defaults:

```python
SpoolLimits(
    maximum_object_size_bytes=104_857_600,
    chunk_size_bytes=1_048_576,
    maximum_in_flight_operations=4,
    maximum_reserved_size_bytes=536_870_912,
    free_space_reserve_bytes=2_147_483_648,
    stale_after_seconds=86_400,
    maximum_cleanup_candidates=1_000,
)
```

Create `cas-spool-<uuid>.part` with exclusive `os.open`, mode `0o600`, descriptor regular-file checks and resolved-root containment. Perform file reads/writes/unlinks through `asyncio.to_thread`; keep an `asyncio.Condition` for reservation/backpressure. Janitor is direct-child, non-recursive, grammar/age/type checked and returns counts without paths.

Wrap input consumption in an injected monotonic 600-second deadline. Timeout maps to retryable `object_storage_busy` only when waiting for local admission; a stream that fails to complete inside its admitted receive window maps to non-retryable `object_storage_input_invalid` with `reason=stream_invalid`. Both paths close the generator when supported and remove the spool.

- [ ] **Step 4: Run spool tests plus leak/static checks**

```powershell
uv run pytest tests/unit/object_storage/test_spool_manager.py tests/contract/test_sensitive_diagnostics.py -q
uv run ruff check packages/r2-object-storage/src/r2_object_storage/spool.py tests/unit/object_storage/test_spool_manager.py
uv run mypy packages/r2-object-storage/src/r2_object_storage/spool.py
```

Expected: all exit `0` on Windows; POSIX permission cases use explicit platform marks while path/type/cleanup cases remain mandatory everywhere.

- [ ] **Step 5: Commit the spool boundary**

```powershell
git add packages/r2-object-storage/src/r2_object_storage/spool.py tests/unit/object_storage/test_spool_manager.py
git commit -m "feat: add bounded secure object spooling"
```

### Task 6: Implement the narrow aiobotocore client, retry classification and metrics sink

**Files:**
- Create: `packages/r2-object-storage/src/r2_object_storage/client.py`
- Create: `packages/r2-object-storage/src/r2_object_storage/error_mapping.py`
- Create: `packages/r2-object-storage/src/r2_object_storage/metrics.py`
- Create: `tests/contract/object_storage/scripted_s3.py`
- Create: `tests/unit/object_storage/test_r2_error_mapping.py`

**Interfaces:**
- Consumes: `ObjectStorageSettings`, `LoadedR2Credentials`, aiobotocore 3.9.0.
- Produces: `S3ClientProtocol`, `StreamingBodyProtocol`, `PutObjectRequest`, `HeadObjectResult`, `GetObjectResult`, `R2ClientManager`, `RetryDecision`, `classify_r2_failure()`, `map_r2_failure()`, `ObjectStorageMetrics`, `InMemoryObjectStorageMetrics`.

- [ ] **Step 1: Write failing client configuration and error-matrix tests**

Assert the client factory receives exact config and no ambient discovery:

```python
@pytest.mark.asyncio
async def test_client_configuration_is_bounded() -> None:
    session = RecordingAioSession()
    manager = build_client_manager(session=session)
    await manager.get_client()
    call = session.only_create_client_call
    assert call["region_name"] == "auto"
    config = cast(AioConfig, call["config"])
    assert config.max_pool_connections == 4
    assert config.connect_timeout == 5
    assert config.read_timeout == 60
    assert config.retries["total_max_attempts"] == 1
```

Parameterize retry decisions for connection reset, `408/429/500/502/503/504`, `SlowDown`, `400 BadDigest`, `401/403`, `404 NoSuchBucket`, ordinary missing object, malformed response and unsupported `4xx`. `RetryDecision` is the exact closed enum `retry | conditional_conflict | terminal`; only conditional PUT `412` maps to `conditional_conflict`. The client wrapper maps only object-level `NoSuchKey`/`NoSuchObject` HEAD responses to `None`; it never hides `NoSuchBucket`. Assert provider messages and request IDs never enter mapped errors.

- [ ] **Step 2: Run tests and verify missing boundaries**

```powershell
uv run pytest tests/unit/object_storage/test_r2_error_mapping.py -q
```

Expected: collection FAIL because client/error mapping modules are absent.

- [ ] **Step 3: Implement lazy client ownership and deterministic retry policy**

`R2ClientManager.get_client()` uses one lock and publishes only a complete client; `close()` is idempotent. Construct `AioConfig` with SigV4, region `auto`, TLS verification, pool 4, connect 5, read 60 and SDK retries disabled. Pass explicit access/secret values and disable metadata/credential-chain behavior rather than depending on ambient AWS variables.

Keep retry pure and injectable:

```python
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    maximum_attempts: int = 3
    operation_deadline_seconds: float = 300.0

    async def run[T](
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> T:
        started = monotonic()
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                return await operation(attempt)
            except asyncio.CancelledError:
                raise
            except Exception as cause:
                decision = classify_r2_failure(cause)
                if decision is RetryDecision.CONDITIONAL_CONFLICT:
                    raise ConditionalCreateConflict() from cause
                elapsed = monotonic() - started
                remaining = self.operation_deadline_seconds - elapsed
                if (
                    decision is RetryDecision.TERMINAL
                    or attempt == self.maximum_attempts
                    or remaining <= 0
                ):
                    raise map_r2_failure(cause, exhausted=remaining <= 0) from cause
                maximum_delay = min(2.0 ** (attempt - 1), 30.0, remaining)
                await sleep(jitter(0.0, maximum_delay))
        raise AssertionError("retry loop exhausted without a result")
```

Keep raw SDK kwargs inside `AiobotocoreS3Client`. The narrow protocol used by the adapter and scripted test is:

```python
class S3ClientProtocol(Protocol):
    async def head_object(self, object_key: CanonicalObjectKey) -> HeadObjectResult | None: ...
    async def put_object(self, request: PutObjectRequest) -> None: ...
    async def get_object(
        self, object_key: CanonicalObjectKey, *, if_match: str
    ) -> GetObjectResult: ...
    async def head_bucket(self) -> None: ...
    async def close(self) -> None: ...
```

`PutObjectRequest` contains `object_key`, internal spool path, `size_bytes`, canonical media type, base64 MD5 and fixed `if_none_match="*"`. `HeadObjectResult` contains exact size/media/ETag. `GetObjectResult` contains only the streaming body. None of these provider-package values are exported from `personal_os`.

Backoff uses bounded full jitter. Cancellation is re-raised. `InMemoryObjectStorageMetrics` records only operation/result/error/bytes/duration/retry/in-flight/reserved values and is sufficient for runtime-check and tests without introducing Prometheus.

- [ ] **Step 4: Run client/error/typing tests**

```powershell
uv run pytest tests/unit/object_storage/test_r2_error_mapping.py -q
uv run mypy packages/r2-object-storage/src/r2_object_storage/client.py packages/r2-object-storage/src/r2_object_storage/error_mapping.py packages/r2-object-storage/src/r2_object_storage/metrics.py tests/contract/object_storage/scripted_s3.py
uv run ruff check packages/r2-object-storage/src/r2_object_storage tests/unit/object_storage/test_r2_error_mapping.py tests/contract/object_storage/scripted_s3.py
```

Expected: all exit `0`.

- [ ] **Step 5: Commit the provider boundary**

```powershell
git add packages/r2-object-storage/src/r2_object_storage/client.py packages/r2-object-storage/src/r2_object_storage/error_mapping.py packages/r2-object-storage/src/r2_object_storage/metrics.py tests/contract/object_storage/scripted_s3.py tests/unit/object_storage/test_r2_error_mapping.py
git commit -m "feat: add bounded R2 client and error mapping"
```

### Task 7: Implement full verification and the fail-closed reader first

**Files:**
- Create: `packages/r2-object-storage/src/r2_object_storage/adapter.py`
- Create: `tests/contract/object_storage/test_r2_adapter_contract.py`
- Modify: `packages/r2-object-storage/src/r2_object_storage/__init__.py`

**Interfaces:**
- Consumes: core `ExpectedObject`/receipt/reader contracts, `S3ClientProtocol`, `SpoolManager`, retry policy, diagnostic logger and metrics sink.
- Produces: `R2S3ObjectStore.resolve_verified_object()`, `verify_existing_object()`, `open_verified_reader()`, explicit `close()`.

- [ ] **Step 1: Write failing verification/read contract tests**

Use `ScriptedS3Client` with exact expected calls. Cover matching HEAD+If-Match GET, missing resolve returning `None`, required missing error, size/media conflicts, malformed/missing ETag, changed ETag, short/excess/corrupt body, zero bytes, retry with a clean spool, no long verification cache and reader cleanup.

Define a test-local `build_store(client, tmp_path)` that supplies fixed settings, `SpoolManager`, deterministic clock/sleep, `InMemoryObjectStorageMetrics` and a captured `DiagnosticLogger`; it must not read process environment.

Prove fail-closed consumption explicitly:

```python
@pytest.mark.asyncio
async def test_reader_yields_nothing_before_full_verification(tmp_path: Path) -> None:
    client = ScriptedS3Client.corrupt_after_prefix(b"valid-prefix", b"wrong-tail")
    store = build_store(client, tmp_path)
    consumed = bytearray()
    with pytest.raises(ObjectStorageError) as raised:
        async with store.open_verified_reader(EXPECTED) as reader:
            async for chunk in reader:
                consumed.extend(chunk)
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert consumed == b""
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run contract tests and verify adapter is absent**

```powershell
uv run pytest tests/contract/object_storage/test_r2_adapter_contract.py -q
```

Expected: collection FAIL because `R2S3ObjectStore` does not exist.

- [ ] **Step 3: Implement HEAD + conditional full GET verification**

The private `_verify_to_spool(expected)` must execute:

```python
object_key = derive_canonical_object_key(expected.content_digest)
head = await self._head_exact(object_key)
require_exact_metadata(head, expected)
verification = await self._spools.reserve_verification(expected.size_bytes)
response = await client.get_object(object_key, if_match=head.etag)
await verification.copy_and_hash(response.body, expected)
return verification
```

Close response bodies in `finally`. Only after `copy_and_hash()` reaches EOF with exact hash/size does the context yield `VerifiedObjectReader`. `resolve_verified_object()` catches only the typed ordinary-absence signal and returns `None`; corrupt/conflicting cases raise.

- [ ] **Step 4: Run adapter verification plus spool/error suites**

```powershell
uv run pytest tests/contract/object_storage/test_r2_adapter_contract.py tests/unit/object_storage/test_spool_manager.py tests/unit/object_storage/test_r2_error_mapping.py -q
uv run mypy src/personal_os/object_storage packages/r2-object-storage/src/r2_object_storage
```

Expected: all exit `0` and scripted calls show `IfMatch` on every GET.

- [ ] **Step 5: Commit fail-closed reads**

```powershell
git add packages/r2-object-storage/src/r2_object_storage/adapter.py packages/r2-object-storage/src/r2_object_storage/__init__.py tests/contract/object_storage/test_r2_adapter_contract.py
git commit -m "feat: verify R2 objects before exposing bytes"
```

### Task 8: Add streaming store, conditional deduplication and lost-response recovery

**Files:**
- Modify: `packages/r2-object-storage/src/r2_object_storage/adapter.py`
- Modify: `tests/contract/object_storage/test_r2_adapter_contract.py`

**Interfaces:**
- Consumes: `SpoolManager.receive_stream()`, verification path from Task 7, retry classification.
- Produces: complete `store_stream()` with `ContentLength`, canonical `ContentType`, base64 `ContentMD5`, `IfNoneMatch="*"`, receipt methods `uploaded_full_read`/`existing_full_read`.

- [ ] **Step 1: Write failing upload/dedup/race tests**

Add cases for no R2 call on invalid claim/size/media, exact request fields, successful upload followed by HEAD+GET, existing dedup, `412` race, ambiguous PUT followed by HEAD, terminal BadDigest/auth, no overwrite/self-repair and independent hashing of each same-hash input.

Reuse the test-local `chunks()` async generator and `build_store()` fixture defined in this contract module; every scripted response lists the exact expected call order.

```python
@pytest.mark.asyncio
async def test_store_uses_conditional_single_put_then_full_read(tmp_path: Path) -> None:
    client = ScriptedS3Client.missing_then_put_then_exact_get(b"payload", "text/plain")
    store = build_store(client, tmp_path)
    receipt = await store.store_stream(chunks(b"pay", b"load"), 7, "text/plain")
    put = client.only_put
    assert put.object_key == receipt.object_key
    assert put.size_bytes == 7
    assert put.media_type.value == "text/plain"
    md5 = hashlib.md5(b"payload", usedforsecurity=False).digest()
    assert put.content_md5 == base64.b64encode(md5).decode()
    assert put.if_none_match == "*"
    assert client.calls_after_put == ["head_object", "get_object"]
```

- [ ] **Step 2: Run focused tests and verify missing store behavior**

```powershell
uv run pytest tests/contract/object_storage/test_r2_adapter_contract.py -k "store or deduplic or conditional or lost" -q
```

Expected: FAIL because `store_stream()` has not implemented the cases.

- [ ] **Step 3: Implement the exact store state machine**

Use this order and no alternative branch:

```text
validate admission
-> receive/hash complete input spool
-> compare claimed digest
-> derive expected object/key
-> HEAD
   -> exists: full verify, existing_full_read receipt
   -> missing: conditional PutObject
      -> success: full verify, uploaded_full_read receipt
      -> 412: full verify winner, existing_full_read receipt
      -> ambiguous: retry begins HEAD
```

Rewind the file before each PUT attempt. Never use `upload_fileobj`. Preserve the input spool until stored verification completes, then remove it in a shielded bounded cleanup.

- [ ] **Step 4: Run the complete adapter contract and diagnostics leakage suite**

```powershell
uv run pytest tests/contract/object_storage/test_r2_adapter_contract.py tests/contract/test_sensitive_diagnostics.py -q
uv run ruff check packages/r2-object-storage/src/r2_object_storage/adapter.py tests/contract/object_storage/test_r2_adapter_contract.py
uv run mypy packages/r2-object-storage/src/r2_object_storage/adapter.py
```

Expected: all exit `0`; no scripted call contains multipart/list/delete/copy/presigned behavior.

- [ ] **Step 5: Commit upload and deduplication**

```powershell
git add packages/r2-object-storage/src/r2_object_storage/adapter.py tests/contract/object_storage/test_r2_adapter_contract.py
git commit -m "feat: store immutable R2 objects with deduplication"
```

### Task 9: Prove concurrency, cancellation and 10,000-item bounded behavior

**Files:**
- Create: `tests/contract/object_storage/test_r2_adapter_resource_contract.py`
- Modify: `packages/r2-object-storage/src/r2_object_storage/adapter.py`
- Modify: `packages/r2-object-storage/src/r2_object_storage/spool.py`
- Modify: `packages/r2-object-storage/src/r2_object_storage/metrics.py`

**Interfaces:**
- Consumes: complete store/verify/read behavior.
- Produces: bounded per-digest single-flight table, cancellation-safe waiters, resource/metric snapshots with no lifetime growth.

- [ ] **Step 1: Write failing race and resource tests**

Test four active permits, a fifth waiting, aggregate reservation never above 512 MiB, at most one maximum verification spool alongside four retained input spools, same-key shared R2 work, one waiter cancellation not cancelling other waiters, owner cancellation cleanup, client shutdown and 10,000 completed small items leaving zero table/reservation entries.

Define `run_bounded()` in this test file as an async producer that keeps at most four submitted tasks, and `build_repeating_store()` as a deterministic scripted client/store/metrics fixture; neither helper reads environment or contacts the network.

```python
@pytest.mark.asyncio
async def test_ten_thousand_items_leave_constant_state(tmp_path: Path) -> None:
    store, client, metrics = build_repeating_store(tmp_path)
    await run_bounded(
        store.store_stream(chunks(index.to_bytes(4, "big")), 4, "application/octet-stream")
        for index in range(10_000)
    )
    assert store.single_flight_entry_count == 0
    assert store.spool_manager.reserved_size_bytes == 0
    assert store.spool_manager.in_flight_count == 0
    assert metrics.maximum_in_flight <= 4
    assert metrics.maximum_reserved_size_bytes <= 536_870_912
```

Use generated bytes only; do not allocate a list of 10,000 full objects.

- [ ] **Step 2: Run resource tests and observe unbounded/missing behavior**

```powershell
uv run pytest tests/contract/object_storage/test_r2_adapter_resource_contract.py -q
```

Expected: FAIL on missing single-flight/resource inspection behavior.

- [ ] **Step 3: Implement bounded single flight and shielded cleanup**

Single-flight keys are `ContentDigest`; entries hold one owner task plus waiter count and are removed in a lock-protected `finally`. Each caller hashes its own input before joining. Cancel a waiter without cancelling the shared owner while another waiter exists. All cleanup uses a short shielded local task; `CancelledError` is re-raised after cleanup.

Metrics record only bounded enums/counts; expose test snapshots without digest/key labels.

- [ ] **Step 4: Run resource, adapter and spool suites three times**

```powershell
1..3 | ForEach-Object { uv run pytest tests/contract/object_storage tests/unit/object_storage/test_spool_manager.py -q }
```

Expected: all three runs exit `0`, proving tests do not rely on order/timing leakage.

- [ ] **Step 5: Commit concurrency hardening**

```powershell
git add packages/r2-object-storage/src/r2_object_storage tests/contract/object_storage/test_r2_adapter_resource_contract.py
git commit -m "feat: bound R2 adapter concurrency and cancellation"
```

### Task 10: Add the read-only R2 runtime check and operator contract

**Files:**
- Create: `packages/r2-object-storage/src/r2_object_storage/runtime_check.py`
- Create: `tests/contract/object_storage/test_r2_runtime_contract.py`
- Create: `docs/operations/object-storage.md`
- Modify: `packages/r2-object-storage/src/r2_object_storage/__init__.py`
- Modify: `packages/r2-object-storage/pyproject.toml`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `tests/contract/test_bootstrap_documentation.py`

**Interfaces:**
- Consumes: R2 settings/client manager, diagnostic context/logger, read-only `head_bucket()`.
- Produces: `object-storage-check-runtime --service api|mcp|worker` console command and Poe `object-storage-check-runtime` bound to `--service worker`; stable exits `0`, `2`, `69`, `70`, `78` without extending the closed `ServiceName` enum.

- [ ] **Step 1: Write failing command and documentation tests**

Cover help/import without environment access, invalid/missing `--service` syntax `2`, config failure `78`, access denied `69`, unavailable after bounded retry `69`, unexpected internal `70`, success `0`, one safe JSON event and exact client close. Assert the scripted runtime check never invokes put/get/list/delete.

Add README assertions for all seven approved environment names, secret-file-only credentials, private bucket, no fallback/delete/list, command syntax and exit meanings. The operations guide must state that startup validation is offline, liveness never calls R2, normal readiness does not call R2, only the explicit command performs `HeadBucket`, credential rotation requires process restart, spool storage must be encrypted/ephemeral, and production/test buckets plus credentials never cross.

- [ ] **Step 2: Run runtime/documentation tests and verify command absence**

```powershell
uv run pytest tests/contract/object_storage/test_r2_runtime_contract.py tests/contract/test_bootstrap_documentation.py tests/contract/test_command_import_side_effects.py -q
```

Expected: FAIL because the command and operator documentation do not exist.

- [ ] **Step 3: Implement one-shot HeadBucket diagnostics**

The CLI parses one required `--service` argument into the existing `ServiceName` values before reading environment or secret files. Add this entry point without adding a fourth diagnostics service:

```toml
[project.scripts]
object-storage-check-runtime = "r2_object_storage.runtime_check:main"
```

The command sequence is exact:

```text
create/bind correlation context
-> load runtime + object-storage settings
-> configure safe diagnostics
-> run bounded read-only HeadBucket
-> emit succeeded/degraded typed event
-> close client
```

Map syntax to `2`, configuration to `78`, dependency/access to `69`, unexpected internal to `70`, success to `0`; never render settings or causes. Startup janitor runs before the probe and emits only safe counts.

- [ ] **Step 4: Run command, docs, leak and process-contract suites**

```powershell
uv run pytest tests/contract/object_storage/test_r2_runtime_contract.py tests/contract/test_bootstrap_documentation.py tests/contract/test_command_import_side_effects.py tests/contract/test_sensitive_diagnostics.py -q
uv run ruff check packages/r2-object-storage/src/r2_object_storage/runtime_check.py
uv run mypy packages/r2-object-storage/src/r2_object_storage/runtime_check.py
```

Expected: all exit `0`.

- [ ] **Step 5: Commit runtime diagnostics and operator docs**

```powershell
git add packages/r2-object-storage pyproject.toml README.md docs/operations/object-storage.md tests/contract/object_storage/test_r2_runtime_contract.py tests/contract/test_bootstrap_documentation.py tests/contract/test_command_import_side_effects.py
git commit -m "feat: add read-only R2 runtime diagnostics"
```

### Task 11: Add the dedicated live R2 harness and trusted workflow

**Files:**
- Create: `tests/integration/r2_object_storage/conftest.py`
- Create: `tests/integration/r2_object_storage/test_live_r2_adapter.py`
- Create: `.github/workflows/object-storage-live.yml`
- Modify: `pyproject.toml`
- Modify: `tests/integration/README.md`
- Modify: `tests/contract/test_ci_security.py`

**Interfaces:**
- Consumes: real `R2S3ObjectStore`; GitHub variables `R2_TEST_ENDPOINT`, `R2_TEST_BUCKET_NAME`; GitHub secrets `R2_TEST_ACCESS_KEY_ID`, `R2_TEST_SECRET_ACCESS_KEY`.
- Produces: `r2_live` marker, `poe object-storage-test-live`, protected live workflow and exact-key cleanup manifest.

- [ ] **Step 1: Write failing workflow-security and cleanup-contract tests**

Extend CI security tests to assert:

```python
def test_r2_live_workflow_is_trusted_and_exact_cleanup_only() -> None:
    text = R2_LIVE_WORKFLOW.read_text("utf-8")
    assert "pull_request:" not in text
    assert "branches: [master]" in text
    assert "schedule:" in text and "workflow_dispatch:" in text
    assert "R2_TEST_ACCESS_KEY_ID" in text
    assert "R2_TEST_SECRET_ACCESS_KEY" in text
    assert "R2_PRODUCTION" not in text
    assert "--junitxml=.local/test-results/object-storage-live.xml" in text
    assert "ListObjects" not in text and "prefix-delete" not in text
```

In the live harness, unit-test the cleanup manifest validator separately from network execution: wrong bucket, noncanonical key, unrecorded key and wildcard are rejected before any delete call.

- [ ] **Step 2: Run static CI tests and verify workflow absence**

```powershell
uv run pytest tests/contract/test_ci_security.py tests/contract/object_storage/test_r2_runtime_contract.py -q
```

Expected: FAIL because `.github/workflows/object-storage-live.yml` and live command are absent.

- [ ] **Step 3: Implement live cases and exact-key cleanup**

The `r2_live` test module must fail at fixture setup if any required variable/secret file is missing. Generate per-run random non-personal payloads, record only keys successfully created by the current run, and in a `finally` fixture use a harness-local low-level delete call for those validated keys. Do not export the delete helper from `r2_object_storage`.

Use `pytest_asyncio.fixture` for asynchronous adapter/cleanup fixtures. Change default pytest selection to `not local_stack and not r2_live`, register `r2_live`, and define `object-storage-test-live` as the explicit test path plus `-m r2_live`; the command must override the default marker expression and must not convert missing credentials into a skip.

Live cases are zero-byte round trip, multi-chunk round trip, dedup, concurrent conditional create, missing object, size/media conflict, deliberately corrupt object and exact cleanup after a forced test exception.

The workflow uses pinned checkout/setup-uv/upload-artifact actions, Python 3.14.6, uv 0.11.32, `uv sync --all-packages --frozen`, a finite 20-minute timeout, secret files mode `0600`, JUnit-only artifact and `if: always()` cleanup inside pytest fixtures.

- [ ] **Step 4: Run offline workflow contracts and collect live tests without secrets**

```powershell
uv run pytest tests/contract/test_ci_security.py tests/contract/object_storage/test_r2_runtime_contract.py -q
uv run pytest tests/integration/r2_object_storage/test_live_r2_adapter.py --collect-only -q
uv run poe object-storage-test-live
```

Expected: first two commands exit `0`; the explicitly invoked live command exits nonzero with a safe typed missing-configuration diagnostic when local test credentials are absent. Do not claim the live gate passes until the protected workflow runs with its dedicated bucket.

- [ ] **Step 5: Commit the live pipeline**

```powershell
git add .github/workflows/object-storage-live.yml pyproject.toml tests/integration/r2_object_storage tests/integration/README.md tests/contract/test_ci_security.py
git commit -m "test: add trusted live R2 contract pipeline"
```

### Task 12: Run final acceptance, update realized decisions and commit evidence

**Files:**
- Modify only if verification reveals documented contract drift: `docs/superpowers/specs/content-addressable-object-storage-design.md`
- Modify: `docs/operations/object-storage.md`
- Modify: `tests/integration/README.md`

**Interfaces:**
- Consumes: all prior tasks and the approved design acceptance list.
- Produces: one clean implementation commit sequence with offline gates passing and an explicit live-gate status.

- [ ] **Step 1: Run the complete offline repository gate from a frozen all-package install**

```powershell
uv sync --all-packages --frozen
uv run poe verify
```

Expected: exit `0`; format, lint, strict typing, boundaries, Python/TypeScript tests and all builds pass.

- [ ] **Step 2: Run the focused acceptance suites with no skip hiding**

```powershell
uv run pytest tests/unit/object_storage tests/contract/object_storage tests/contract/test_sensitive_diagnostics.py tests/contract/test_ci_security.py -q
uv run pytest tests/integration/r2_object_storage/test_live_r2_adapter.py --collect-only -q
uv run lint-imports
git diff --check
```

Expected: all exit `0`; the live module collects every required case even when credentials are unavailable for execution.

- [ ] **Step 3: Audit the implementation against forbidden capabilities**

```powershell
rg -n "upload_fileobj|create_multipart|upload_part|presign|ListObjects|list_objects|DeleteObject|delete_object|CopyObject|copy_object|MinIO|server_only|hybrid_edge|8 MiB" src packages/r2-object-storage tests/contract/object_storage
```

Expected: no production match. Test-only assertions may contain forbidden method names only to prove absence; inspect each such match manually.

Run the privacy scan:

```powershell
rg -n "R2_SECRET_ACCESS_KEY=|aws_secret_access_key\s*=\s*['\"]|cloudflarestorage\.com/|x-amz-signature" src packages/r2-object-storage .github tests docs/operations
```

Expected: no committed credential/value/signed-URL match. Endpoint grammar documentation without an account value is allowed after manual inspection.

- [ ] **Step 4: Record live evidence without weakening the gate**

If protected R2 credentials are available, dispatch `.github/workflows/object-storage-live.yml` on the exact implementation commit and record its run URL/date in `docs/operations/object-storage.md`. If they are unavailable, document `live activation blocked: dedicated test-bucket credentials not configured`; do not mark Phase 1 production activation complete and do not skip/xfail the live cases.

- [ ] **Step 5: Inspect final diff, guidance-file sizes and worktree state**

```powershell
git diff --stat
git diff --check
(Get-Content AGENTS.md).Count
(Get-Content CLAUDE.md).Count
git status --short
```

Expected: only intended implementation/test/docs files differ; instruction files retain their reviewed sizes unless intentionally changed; no `.local`, spool, credential, JUnit, coverage or build artifact is tracked.

- [ ] **Step 6: Commit final acceptance documentation if it changed**

```powershell
git add docs/operations/object-storage.md tests/integration/README.md docs/superpowers/specs/content-addressable-object-storage-design.md
git diff --cached --check
git commit -m "docs: record R2 object storage acceptance"
```

If those files have no final changes, skip this commit rather than creating an empty commit.

---

## Completion Checklist

- [ ] Every accepted byte stream is hashed by the backend before key selection.
- [ ] Every receipt follows a full conditional R2 GET and exact SHA-256/size/media check.
- [ ] Missing/corrupt/conflicting objects expose no bytes and create no receipt.
- [ ] Four-operation, 512 MiB spool and 2 GiB reserve limits hold under 10,000-item tests.
- [ ] Conditional create, `412` winner verification and ambiguous-response recovery pass.
- [ ] Cancellation leaves no response, spool, reservation, single-flight entry or client leak.
- [ ] Core/provider imports remain one-way and strict mypy passes for both packages.
- [ ] Settings compose registered fragments but still reject typos and plaintext secret names.
- [ ] Logs, metrics and artifacts pass the extended sentinel leakage corpus.
- [ ] Production code has no multipart, delete, list, copy, public/presigned or Worker route.
- [ ] Offline `poe verify` passes on the exact implementation commit.
- [ ] Live R2 job either passes on that commit or remains explicitly blocking production activation.
