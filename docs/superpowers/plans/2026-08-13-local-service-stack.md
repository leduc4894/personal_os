# Local Service Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the authenticated, persistent, loopback-only Phase 1 Docker Compose stack for PostgreSQL, Qdrant, Neo4j, Redis and Temporal, with safe cross-platform lifecycle commands and a disposable Ubuntu smoke contract.

**Architecture:** Keep one canonical Compose model under `infra/compose/` and one typed Python orchestration boundary at `tools/local_service_stack.py`. Compose owns seven long-running services and three idempotent initialization jobs; Python owns preflight validation, generated local secrets, bounded subprocesses, semantic readiness and exact-label reset safety. Cloudflare R2 remains completely outside this stack and belongs to the later content-addressable object-storage spec.

**Tech Stack:** CPython 3.14.6, Python standard library, PyYAML 6.0.3, pytest 9.1.1, Ruff 0.15.22, mypy 2.3.0 strict, Docker Engine with Linux containers, Docker Compose CLI 2.30.0 or newer, PostgreSQL 18.4, Qdrant 1.18.2, Neo4j Community 5.26.28, Redis Open Source 8.6.4, Temporal Server 1.31.2, Temporal UI 2.53.0 and Temporal CLI 1.8.0.

## Global Constraints

- Implement only `docs/superpowers/specs/local-service-stack-design.md`; do not add application tables, provider adapters, projection schemas, Cloudflare R2 access, backup or observability services.
- Keep exactly seven long-running services and three one-shot initialization services; do not use profiles or explicit `container_name` values.
- Publish only the eight approved ports as `127.0.0.1:${VARIABLE:-default}:container_port`.
- Use project `knowledge-local` locally and `knowledge-ci-<bounded-lowercase-token>` only when `CI=true`.
- Pin each image by exact tag and immutable manifest digest; initially support only `linux/amd64`.
- Use `redis:8.6.4`; `redis:8.6.4-bookworm` does not exist and is forbidden.
- Generate local-service credentials only under `.local/stack-secrets/`; never accept credentials from `.env`, host environment variables, CLI arguments or committed configuration.
- Do not create, load, validate, print or forward R2 configuration in Compose, lifecycle code or local-stack CI.
- Never log raw service logs, raw vendor exceptions, secret values, secret filenames/paths, rendered secret configuration or credential-bearing URLs.
- Invoke subprocesses using argument arrays, `shell=False`, an explicit environment allowlist, bounded capture and finite deadlines.
- Preserve volumes and secrets on `down`; delete volumes only through exact-label `reset`; rotate secrets only after successful volume deletion.
- Require authenticated semantic verification in addition to health checks; dependency outage exits `75` and never triggers reset or object-store fallback.
- Add no Python production dependency. Keep Ruff and mypy strict clean and run `uv run poe verify` before completion.
- Windows validates configuration only. Real-container smoke runs on Ubuntu; Windows local runtime requires Docker Desktop in Linux-container mode.

## Preflight

Before Task 1, use `superpowers:using-git-worktrees` to create an isolated worktree from commit `b3c65db` or its descendant. Do not execute implementation directly on `master`.

```powershell
git status --short
uv --version
uv run python --version
docker version --format '{{.Client.Version}}'
docker compose version --short
uv run poe verify
```

Expected: clean status, uv `0.11.32`, Python `3.14.6`, a reachable Linux-container Docker Engine, Compose `2.30.0` or newer and the existing repository gate passes.

Before every task, reread its `Interfaces` and Global Constraints. After every task, run `git diff --check`, inspect only named files, commit the independently testable deliverable and start the next task from a clean tree.

## File Map

- `tools/local_service_stack.py`: typed lifecycle, secrets, lock/config validation, probes, reset and CLI exit mapping.
- `infra/compose/compose.yaml`: canonical services, dependencies, ports, secrets, volumes, health checks and limits.
- `infra/compose/images.lock.yaml`: immutable registry references consumed by Compose.
- `infra/compose/config/temporal/dynamicconfig.yaml`: Temporal SQL local configuration.
- `infra/compose/scripts/*.sh`: PostgreSQL/Temporal idempotent initialization and secret adapters.
- `tests/unit/tools/test_local_service_stack.py`: deterministic unit tests using injected boundaries.
- `tests/contract/test_local_service_stack_contract.py`: parsed Compose, image lock, scripts and security invariants.
- `tests/integration/test_local_service_stack.py`: disposable persistence, idempotency and recovery smoke.
- `tests/integration/README.md`: replace the bootstrap reservation with the executable local-stack owner/scope contract.
- `.github/workflows/local-service-stack.yml`: Windows config plus Ubuntu config/full smoke.
- `pyproject.toml`, `.gitignore`, `README.md`, `infra/compose/README.md`: command surface and operator documentation.

---

### Task 1: Typed Lifecycle Foundations and Safe Preconditions

**Files:**

- Create: `tools/local_service_stack.py`
- Create: `tests/unit/tools/test_local_service_stack.py`

**Interfaces:**

- Consumes: Python standard library only.
- Produces: `StackExitCode`, `StackFailure`, `StackPaths`, `PortBinding`, `CommandResult`, `CommandRunner`, `PORT_BINDINGS`, `resolve_stack_paths()`, `validate_project_name()`, `resolve_ports()`, `validate_port_availability()`, `sanitize_subprocess_environment()` and `run_command()`.

- [ ] **Step 1: Write failing path, project, port and subprocess tests**

```python
def test_resolve_stack_paths_stays_beneath_repository(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    assert paths.compose_file == tmp_path / "infra" / "compose" / "compose.yaml"
    assert paths.secret_directory == tmp_path / ".local" / "stack-secrets"


@pytest.mark.parametrize("name", ["knowledge-local", "knowledge-ci-a1b2c3"])
def test_accepts_bounded_project_name(name: str) -> None:
    assert validate_project_name(name) == name


@pytest.mark.parametrize("name", ["", "Knowledge", "../escape", "a" * 64])
def test_rejects_unsafe_project_name(name: str) -> None:
    with pytest.raises(StackFailure) as raised:
        validate_project_name(name)
    assert raised.value.exit_code is StackExitCode.CLI


def test_rejects_duplicate_effective_ports() -> None:
    with pytest.raises(StackFailure, match="duplicate_port"):
        resolve_ports({"POSTGRES_PORT": "15432", "REDIS_PORT": "15432"})


def test_subprocess_environment_omits_credentials_and_r2() -> None:
    clean = sanitize_subprocess_environment(
        {"PATH": "safe", "R2_SECRET_ACCESS_KEY": "secret", "POSTGRES_PASSWORD": "secret"}
    )
    assert clean == {"PATH": "safe"}


def test_run_command_maps_timeout_without_raw_exception() -> None:
    with pytest.raises(StackFailure) as raised:
        run_command(["python", "-c", "import time; time.sleep(5)"], timeout_seconds=0.01)
    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "subprocess_timeout"
```

Also inject a socket factory and assert every socket closes on bind failure.

- [ ] **Step 2: Run tests and confirm the module is missing**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py -q
```

Expected: collection fails because `tools.local_service_stack` does not exist.

- [ ] **Step 3: Implement typed foundations**

```python
class StackExitCode(IntEnum):
    OK = 0
    CLI = 2
    PREREQUISITE = 64
    CONTRACT = 65
    STARTUP = 69
    INTERNAL = 70
    READINESS = 75


@dataclass(frozen=True, slots=True)
class StackFailure(Exception):
    exit_code: StackExitCode
    result_code: str

    def __str__(self) -> str:
        return self.result_code


@dataclass(frozen=True, slots=True)
class StackPaths:
    repository_root: Path
    compose_file: Path
    image_lock: Path
    secret_directory: Path
    state_directory: Path


@dataclass(frozen=True, slots=True)
class PortBinding:
    variable: str
    default: int
    container_port: int


@dataclass(frozen=True, slots=True)
class CommandResult:
    return_code: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult: ...
```

Set all eight `PORT_BINDINGS`, validate ASCII decimal overrides in `1024..65535`, reject duplicates, and bind `127.0.0.1` with `SO_EXCLUSIVEADDRUSE` on Windows where available. Always close sockets in `finally`.

Implement `run_command()` with `subprocess.run(list(arguments), shell=False, check=False, capture_output=True, text=True, timeout=timeout_seconds, env=environment)`. Map operation-level readiness timeout to `75`, a missing prerequisite/process launch failure to `64`, and truncate stdout/stderr to 8192 bytes. The allowlist is `PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`, `COMSPEC`, `TMP`, `TEMP`, `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG`, `CI` and the eight port variables; reject every other input before process launch.

- [ ] **Step 4: Verify and commit**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py -q
uv run ruff check tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py
uv run mypy tools/local_service_stack.py
git diff --check
git add tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py
git commit -m "feat: validate local stack preconditions"
```

---

### Task 2: Atomic File-Backed Local Service Secrets

**Files:**

- Modify: `tools/local_service_stack.py`
- Modify: `tests/unit/tools/test_local_service_stack.py`
- Modify: `.gitignore`

**Interfaces:**

- Consumes: `StackPaths`, `StackFailure` and `StackExitCode`.
- Produces: `SecretKind`, `SecretSpec`, `SecretSetState`, `SECRET_SPECS`, `inspect_secret_set()`, `bootstrap_secret_set()`, `validate_secret_set()` and `remove_secret_set_after_reset()`.

- [ ] **Step 1: Add failing atomicity, entropy, reuse and refusal tests**

```python
from tools.local_service_stack import SECRET_SPECS, SecretSetState, bootstrap_secret_set


def test_bootstrap_creates_exact_complete_secret_set(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    assert bootstrap_secret_set(paths, random_bytes=lambda count: bytes(range(count))) \
        is SecretSetState.COMPLETE
    assert {path.name for path in paths.secret_directory.iterdir()} == {
        spec.filename for spec in SECRET_SPECS
    } | {"qdrant_config.yaml"}


def test_bootstrap_reuses_complete_set_byte_for_byte(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)
    before = {path.name: path.read_bytes() for path in paths.secret_directory.iterdir()}
    bootstrap_secret_set(paths)
    assert {path.name: path.read_bytes() for path in paths.secret_directory.iterdir()} == before


def test_partial_secret_set_is_terminal_and_not_rewritten(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    paths.secret_directory.mkdir(parents=True)
    partial = paths.secret_directory / "postgres_admin_password"
    partial.write_text("unchanged", encoding="ascii")
    with pytest.raises(StackFailure, match="partial_secret_set"):
        bootstrap_secret_set(paths)
    assert partial.read_text(encoding="ascii") == "unchanged"


def test_missing_secrets_with_existing_volume_is_terminal(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    with pytest.raises(StackFailure, match="secret_set_missing_with_volumes"):
        validate_secret_set(paths, list_project_volumes=lambda: ("knowledge-local_postgres-data",))


def test_native_shapes_and_failure_redaction(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)
    values = {path.name: path.read_text(encoding="ascii") for path in paths.secret_directory.iterdir()}
    assert values["neo4j_auth"].startswith("neo4j/")
    assert values["redis_acl"].startswith("user default off\nuser knowledge on >")
    assert values["qdrant_config.yaml"].startswith("service:\n  api_key: ")
    assert all("r2" not in name.lower() for name in values)
```

Also test: 32 characters from a fixed 64-character alphabet; no newline in primitive passwords, API key or `neo4j_auth`; exactly one final newline in Redis ACL and generated Qdrant YAML; symlink rejection; POSIX `0700/0600`; Windows regular-file boundary; secret/path/value absent from every `StackFailure`.

- [ ] **Step 2: Run the tests and confirm the interfaces are absent**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py -q
```

Expected: collection fails on the Task 2 imports.

- [ ] **Step 3: Implement exact secret specifications and atomic staging**

```python
class SecretKind(StrEnum):
    PASSWORD = "password"
    NEO4J_AUTH = "neo4j_auth"
    REDIS_ACL = "redis_acl"


class SecretSetState(StrEnum):
    MISSING = "missing"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class SecretSpec:
    filename: str
    kind: SecretKind


SECRET_SPECS: Final = (
    SecretSpec("postgres_admin_password", SecretKind.PASSWORD),
    SecretSpec("postgres_application_password", SecretKind.PASSWORD),
    SecretSpec("postgres_temporal_password", SecretKind.PASSWORD),
    SecretSpec("qdrant_api_key", SecretKind.PASSWORD),
    SecretSpec("neo4j_auth", SecretKind.NEO4J_AUTH),
    SecretSpec("redis_acl", SecretKind.REDIS_ACL),
    SecretSpec("redis_application_password", SecretKind.PASSWORD),
)
```

Generate each primitive password/API key as 32 characters from a fixed 64-character ASCII alphabet (192 bits). Derive `neo4j_auth` as `neo4j/<password>`, Redis ACL as exactly `user default off\nuser knowledge on ><password> ~* +@all\n`, and Qdrant YAML from the API key.

Create a sibling staging directory using `0700`, files with `os.open(..., O_CREAT | O_EXCL, 0o600)`, flush each file and directory, then atomically rename the complete directory. Reject symlinks and prove resolved paths stay beneath `<repository>/.local/stack-secrets`. On Windows validate regular, non-symlink, user-owned paths without claiming POSIX mode equivalence. Public functions return state only, never secret material.

- [ ] **Step 4: Ignore generated local state and verify**

Add exactly `.local/` to `.gitignore`, then run:

```powershell
git check-ignore .local/stack-secrets/postgres_admin_password
uv run pytest tests/unit/tools/test_local_service_stack.py -q
uv run ruff check tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py
uv run mypy tools/local_service_stack.py
git diff --check
```

Expected: the path is ignored; tests and static checks pass without printing any generated value.

- [ ] **Step 5: Commit atomic secrets**

```powershell
git add .gitignore tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py
git commit -m "feat: bootstrap local stack secrets"
```

---

### Task 3: Immutable Image Lock and Static Compose Topology

**Files:**

- Create: `infra/compose/images.lock.yaml`
- Create: `infra/compose/compose.yaml`
- Create: `tests/contract/test_local_service_stack_contract.py`
- Modify: `tools/local_service_stack.py`
- Modify: `tests/unit/tools/test_local_service_stack.py`

**Interfaces:**

- Consumes: paths, `CommandRunner` and exact secret filenames.
- Produces: `ImageLockEntry`, `load_image_lock()`, `validate_image_lock()`, exact service/network/volume sets and the initial parsed Compose contract.

- [ ] **Step 1: Write failing parsed-contract tests**

```python
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_LOCK_PATH = REPO_ROOT / "infra" / "compose" / "images.lock.yaml"
COMPOSE_PATH = REPO_ROOT / "infra" / "compose" / "compose.yaml"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _read_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_compose_has_exact_topology() -> None:
    compose = _read_yaml(COMPOSE_PATH)
    assert set(compose["services"]) == {
        "postgresql", "qdrant", "neo4j", "redis", "temporal", "temporal-ui",
        "temporal-cli", "postgres-provision", "temporal-schema-setup",
        "temporal-namespace-bootstrap",
    }
    assert set(compose["networks"]) == {"service-backplane"}
    assert set(compose["volumes"]) == {
        "postgres-data", "qdrant-data", "neo4j-data", "redis-data",
    }


def test_every_publication_is_loopback_and_approved() -> None:
    allowed = {
        "POSTGRES_PORT", "QDRANT_HTTP_PORT", "QDRANT_GRPC_PORT", "NEO4J_HTTP_PORT",
        "NEO4J_BOLT_PORT", "REDIS_PORT", "TEMPORAL_GRPC_PORT", "TEMPORAL_UI_PORT",
    }
    seen: set[str] = set()
    for service in _read_yaml(COMPOSE_PATH)["services"].values():
        for publication in service.get("ports", []):
            assert publication.startswith("127.0.0.1:${")
            seen.add(publication.split("${", 1)[1].split(":-", 1)[0])
    assert seen == allowed


def test_lock_is_unique_immutable_and_amd64() -> None:
    entries = _read_yaml(IMAGE_LOCK_PATH)["images"]
    assert len({entry["component"] for entry in entries}) == len(entries)
    assert all(DIGEST_PATTERN.fullmatch(entry["manifest_digest"]) for entry in entries)
    assert all(entry["supported_platforms"] == ["linux/amd64"] for entry in entries)
    assert all(entry["verified_at"] == "2026-08-13" for entry in entries)


def test_forbidden_surfaces_are_absent() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "container_name:", "privileged: true", "network_mode: host",
        "/var/run/docker.sock", "latest", "6335:", "NEO4J_PLUGINS",
        "auto-setup", "start-dev", "minio", "cloudflarestorage.com", "R2_",
    ):
        assert forbidden not in text
```

Add a unit test that writes a lock with a wrong digest and expects `StackFailure(CONTRACT, "image_lock_mismatch")` without echoing the bad reference.

- [ ] **Step 2: Run tests and confirm artifacts/interfaces are absent**

```powershell
uv run pytest tests/contract/test_local_service_stack_contract.py tests/unit/tools/test_local_service_stack.py -q
```

- [ ] **Step 3: Create the exact immutable image lock**

Use `version: 1`, `supported_platforms: [linux/amd64]`, `verified_at: "2026-08-13"` and these exact tag/digest pairs:

```text
postgres:18.4-bookworm              sha256:882236b897e39051d2368c5ccc6cda944904723506b2dfc97f2a8f5bc9afa382
qdrant/qdrant:v1.18.2               sha256:75eab8c4ba42096724fdcfde8b4de0b5713d529dde32f285a1f86fdcb2c9e50c
neo4j:5.26.28-community             sha256:ff32db30b2baff97971e441b46bfd9c832c1b62c970398ef579244c06b21d357
redis:8.6.4                         sha256:a051e4f48a5d0ceda6554974f3ad0f5369f4479197f36829332c1325cecad2b7
temporalio/server:1.31.2            sha256:b5ecdb8282bededae2a10c36e8d862e27d0bc2d247fc73c5416025997ab4a1da
temporalio/admin-tools:1.31.2       sha256:dbc5fcd6ee8f0f4d808bf765af9a87dea9d8a283abfdcfbd2fc148496ba66107
temporalio/ui:2.53.0                sha256:810eba47f77a89b0e64e2e751478ca585d037bbd90c0951a2974a92a6c5adeb9
temporalio/temporal:1.8.0           sha256:2c344b4a39b4489fc6944db095f628f0c30659836faf11780a4dc435599e80e3
```

Immediately before the implementation commit, run `docker buildx imagetools inspect <tag> --format '{{.Manifest.Digest}}'` for every entry. Any mismatch stops the task and requires a reviewed lock update.

- [ ] **Step 4: Create the static Compose skeleton**

Set `name: knowledge-local`. Reference every image as `<tag>@<digest>`, set `platform: linux/amd64`, one private bridge network and four volumes. Declare top-level file secrets under `../../.local/stack-secrets/`. Add the exact eight loopback publications and persistent mounts:

```text
postgres-data:/var/lib/postgresql
qdrant-data:/qdrant/storage
neo4j-data:/data
redis-data:/data
```

Long-running resource contracts:

```text
postgresql   1536m / 1.5 CPU     qdrant       3g / 2.0 CPU
neo4j        2560m / 2.0 CPU     redis        256m / 0.5 CPU
temporal     1g / 1.5 CPU        temporal-ui  256m / 0.5 CPU
temporal-cli 128m / 0.25 CPU
```

Use `restart: unless-stopped` for long-running services. Each init service uses `restart: "no"`, `512m`, `0.5` CPU, read-only script mounts and only required secrets. Task 4 adds final commands and dependencies. Qdrant peer port `6335` stays unpublished.

- [ ] **Step 5: Implement strict lock parsing and consumption validation**

Reject unknown keys, duplicate components/references, invalid digest syntax and unsupported platforms. Require every Compose registry reference to consume exactly one lock entry. `temporal-admin-tools` may serve multiple init jobs, but every reference must be byte-identical to its single logical lock entry.

- [ ] **Step 6: Verify and commit**

```powershell
uv run pytest tests/contract/test_local_service_stack_contract.py tests/unit/tools/test_local_service_stack.py -q
uv run ruff check tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py tests/contract/test_local_service_stack_contract.py
uv run mypy tools/local_service_stack.py
git diff --check
git add infra/compose/images.lock.yaml infra/compose/compose.yaml tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py tests/contract/test_local_service_stack_contract.py
git commit -m "feat: define local stack topology"
```

---

### Task 4: Idempotent PostgreSQL and Temporal Initialization

**Files:**

- Create: `infra/compose/config/temporal/dynamicconfig.yaml`
- Create: `infra/compose/scripts/postgres-provision.sh`
- Create: `infra/compose/scripts/temporal-schema-setup.sh`
- Create: `infra/compose/scripts/temporal-namespace-bootstrap.sh`
- Create: `infra/compose/scripts/temporal-secret-entrypoint.sh`
- Modify: `infra/compose/compose.yaml`
- Modify: `tests/contract/test_local_service_stack_contract.py`

**Interfaces:**

- Consumes: immutable images, Docker secrets and static service names from Task 3.
- Produces: exact role/database ownership, version-aware Temporal schemas, exact namespace reconciliation, final health checks and dependency graph.

- [ ] **Step 1: Add failing parsed-script and dependency tests**

```python
def test_initializers_have_exact_dependency_chain() -> None:
    services = _read_yaml(COMPOSE_PATH)["services"]
    assert services["postgres-provision"]["depends_on"] == {
        "postgresql": {"condition": "service_healthy"}
    }
    assert services["temporal-schema-setup"]["depends_on"] == {
        "postgres-provision": {"condition": "service_completed_successfully"}
    }
    assert services["temporal"]["depends_on"] == {
        "temporal-schema-setup": {"condition": "service_completed_successfully"}
    }
    assert services["temporal-namespace-bootstrap"]["depends_on"] == {
        "temporal": {"condition": "service_healthy"}
    }
    assert services["temporal-ui"]["depends_on"] == {
        "temporal-namespace-bootstrap": {"condition": "service_completed_successfully"}
    }
    assert services["temporal-cli"]["depends_on"] == {
        "temporal-namespace-bootstrap": {"condition": "service_completed_successfully"}
    }


def test_postgres_18_uses_new_volume_root_and_non_superuser_roles() -> None:
    compose = _read_yaml(COMPOSE_PATH)
    assert "postgres-data:/var/lib/postgresql" in compose["services"]["postgresql"]["volumes"]
    script = (REPO_ROOT / "infra/compose/scripts/postgres-provision.sh").read_text()
    assert "NOSUPERUSER" in script
    assert "knowledge_app" in script
    assert "temporal_service" in script
    assert "knowledge" in script and "temporal_visibility" in script


def test_temporal_schema_script_is_version_aware_and_fails_ahead() -> None:
    script = (REPO_ROOT / "infra/compose/scripts/temporal-schema-setup.sh").read_text()
    assert "postgres12" in script
    assert "setup-schema" in script
    assert "update-schema" in script
    assert "schema_version_ahead" in script


def test_namespace_is_exactly_knowledge_with_seven_day_retention() -> None:
    script = (REPO_ROOT / "infra/compose/scripts/temporal-namespace-bootstrap.sh").read_text()
    assert "knowledge" in script
    assert "7d" in script
    assert "namespace_contract_mismatch" in script
```

Also assert: scripts begin with `#!/bin/sh` and `set -eu`; no application DDL; init jobs are one-shot and bounded; every service has the specified memory/CPU/restart/health contract; only necessary secrets are mounted; no `*_FILE` value is copied into logs.

- [ ] **Step 2: Run contract tests and confirm scripts/final graph are absent**

```powershell
uv run pytest tests/contract/test_local_service_stack_contract.py -q
```

- [ ] **Step 3: Implement PostgreSQL provisioning**

Configure PostgreSQL with principal `stack_admin`, `POSTGRES_PASSWORD_FILE=/run/secrets/postgres_admin_password`, database `postgres`, `scram-sha-256`, and health via an authenticated `psql -XAtqc 'SELECT 1'` that reads the mounted password at execution time.

`postgres-provision.sh` reads secret files without echoing them, uses `psql -v ON_ERROR_STOP=1`, and reconciles:

```text
knowledge_app     LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
temporal_service  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
knowledge         OWNER knowledge_app
temporal           OWNER temporal_service
temporal_visibility OWNER temporal_service
```

Use `format('%I', ...)` for identifiers and `format('%L', ...)` for passwords inside server-side SQL. Revoke `PUBLIC` connect/create rights, grant each role only its own database, explicitly revoke cross-database connect, and do not create application tables. Re-running reconciles password/ownership/grants and exits zero without dropping data.

- [ ] **Step 4: Implement version-aware Temporal schema and namespace bootstrap**

`temporal-secret-entrypoint.sh` reads the Temporal PostgreSQL secret into process environment only inside the container, unsets temporary shell variables, then `exec`s the fixed Temporal binary and arguments.

For both `temporal` and `temporal_visibility`, `temporal-schema-setup.sh` invokes `temporal-sql-tool` with `--plugin postgres12`. If schema metadata is absent, run `setup-schema -v 0.0` then `update-schema` from the image's fixed schema directory. If current version is behind, update; if equal, exit zero; if ahead, malformed or unreadable, print only `schema_version_ahead`, `schema_version_invalid` or `schema_unavailable` and exit nonzero. Never downgrade automatically.

`temporal-namespace-bootstrap.sh` uses the pinned Temporal CLI to describe `knowledge`. Create it with retention `7d` only when absent. If present, normalize the returned duration and require exactly seven days; drift exits nonzero with `namespace_contract_mismatch`. Re-running an exact namespace exits zero.

- [ ] **Step 5: Finalize service commands, authentication and health checks**

- Qdrant mounts generated `qdrant_config.yaml` read-only and persists `/qdrant/storage`; container health targets the intentionally public `/readyz`; semantic readiness separately requires the API key and proves an equivalent protected request without it is rejected.
- Neo4j mounts `neo4j_auth`, disables plugins, configures heap/page cache within 2560m, persists `/data`, and probes with `cypher-shell` using the secret.
- Redis starts with `--aclfile /run/secrets/redis_acl --appendonly yes --maxmemory 128mb --maxmemory-policy noeviction --protected-mode yes`; health uses authenticated `redis-cli PING` as `knowledge`.
- Temporal server uses SQL persistence for both stores and a fixed mounted dynamic config; health probes gRPC using the pinned CLI/admin tooling.
- Temporal UI points only at `temporal:7233` and health checks its local HTTP readiness endpoint.
- Temporal CLI stays long-running with a fixed sleep/idle command and health checks authenticated connectivity to namespace `knowledge`; no Docker socket is mounted.

Use interval `5s` and timeout `3s` everywhere. PostgreSQL, Qdrant and Redis use 20 retries with `start_period: 10s`; Neo4j and Temporal use 20 retries with `start_period: 60s`; Temporal UI and CLI use 20 retries with `start_period: 30s`. Add a Docker-enabled contract test that pulls every locked reference, enumerates the command/binary named by each committed `healthcheck.test`, and proves that binary exists in the pinned image before starting the stack. A missing binary fails the contract; do not weaken readiness.

- [ ] **Step 6: Validate rendered Compose and idempotent scripts**

```powershell
uv run python tools/local_service_stack.py bootstrap
docker compose -f infra/compose/compose.yaml --project-name knowledge-local config --quiet
uv run pytest tests/contract/test_local_service_stack_contract.py -q
uv run ruff check tests/contract/test_local_service_stack_contract.py
git diff --check
```

Expected: config renders without secret content, contract tests pass, and shell scripts contain no application DDL or unbounded retry loop.

- [ ] **Step 7: Commit initialization contracts**

```powershell
git add infra/compose tests/contract/test_local_service_stack_contract.py
git commit -m "feat: initialize local service databases"
```

---

### Task 5: Bounded Compose Lifecycle and Stable Status

**Files:**

- Modify: `tools/local_service_stack.py`
- Modify: `tests/unit/tools/test_local_service_stack.py`

**Interfaces:**

- Consumes: exact Compose/init graph and all Task 1–3 validation boundaries.
- Produces: `StackContext`, `PrerequisiteVersions`, `compose_arguments()`, `check_prerequisites()`, `validate_compose_config()`, `wait_for_stack()`, `stack_up()`, `stack_status()` and `stack_down()`.

- [ ] **Step 1: Add failing preflight-order, argument, timeout and status tests**

```python
def test_up_preflight_order_precedes_first_mutating_command(context: StackContext) -> None:
    calls: list[tuple[str, ...]] = []
    stack_up(context, runner=recording_runner(calls))
    assert operation_names(calls)[:7] == [
        "validate_project", "check_compose", "check_engine", "validate_ports",
        "validate_secrets", "validate_lock", "compose_config",
    ]
    assert first_mutating_index(calls) > calls.index(next(c for c in calls if "config" in c))


def test_compose_arguments_are_array_based_and_project_scoped(context: StackContext) -> None:
    assert compose_arguments(context) == [
        "docker", "compose", "--file", str(context.paths.compose_file),
        "--project-name", "knowledge-local",
    ]


def test_wait_timeout_returns_temporary_without_down_or_reset(context: StackContext) -> None:
    calls: list[tuple[str, ...]] = []
    with pytest.raises(StackFailure) as raised:
        wait_for_stack(context, runner=never_ready_runner(calls), deadline_seconds=180)
    assert raised.value.exit_code is StackExitCode.READINESS
    assert not any("down" in call or "volume" in call for call in calls)


def test_status_output_has_stable_non_secret_shape(context: StackContext) -> None:
    status = stack_status(context, runner=healthy_runner())
    assert set(status) == {"project", "state", "services", "initializers", "result_code"}
    assert "password" not in json.dumps(status).lower()


def test_down_never_removes_volumes_images_or_secrets(context: StackContext) -> None:
    calls = capture_calls(lambda runner: stack_down(context, runner=runner))
    flat = " ".join(part for call in calls for part in call)
    assert "--volumes" not in flat and "--rmi" not in flat
```

Also cover Compose `2.29.x` rejection, `2.30.0` acceptance, non-Linux engine rejection, non-amd64 rejection, `knowledge-ci-*` rejection without `CI=true`, init exit nonzero mapping to `69`, daemon unavailable mapping to `64`, and no R2 environment forwarding.

- [ ] **Step 2: Run focused tests and confirm lifecycle interfaces are absent**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py -q
```

- [ ] **Step 3: Implement immutable context and prerequisites**

```python
@dataclass(frozen=True, slots=True)
class PrerequisiteVersions:
    compose: tuple[int, int, int]
    engine_os: str
    engine_architecture: str


@dataclass(frozen=True, slots=True)
class StackContext:
    paths: StackPaths
    project_name: str
    ports: Mapping[str, int]
    environment: Mapping[str, str]
```

All lifecycle commands accept optional `--project-name`; default is `knowledge-local`. Permit a nonlocal name only when `CI=true` and it matches `knowledge-ci-[a-z0-9][a-z0-9-]{0,40}`. Pass project and file explicitly to every Compose invocation; do not depend on `COMPOSE_PROJECT_NAME` or current directory.

Parse Compose semver and require `>=2.30.0`; query engine OS/architecture and require `linux/amd64`. `config` does not require a running engine; `up/status/verify/down/reset/smoke` do.

- [ ] **Step 4: Implement fail-fast up and bounded readiness**

Use this exact no-mutation-before-validation order:

```text
project -> Compose version -> engine OS/architecture -> port validation/collision
-> secret-set state -> image-lock agreement -> docker compose config --quiet
-> docker compose up --detach --remove-orphans --wait --wait-timeout 180
-> inspect three init exit codes -> semantic verify
```

The 180-second outer monotonic deadline includes pull/create/start/init/health. Never invoke `down` automatically on failure. Map invalid CLI arguments to `2`, prerequisite/port failures to `64`, local contract drift to `65`, container startup/init failure to `69`, readiness timeout/failure to `75`, and unexpected internal invariant to `70`.

- [ ] **Step 5: Implement stable `status` and non-destructive `down`**

Read `docker compose ps --all --format json` and inspect init jobs. Emit one compact JSON document with sorted service names and only stable fields: project, aggregate state (`absent|starting|ready|degraded|stopped`), service state/health, init state/exit code, and fixed `result_code`. Do not include container IDs, image references, paths, environment, commands or raw error text.

`down` runs only `docker compose --file <resolved-compose-file> --project-name <validated-project> down --remove-orphans --timeout 30`; it must not pass `--volumes`, `--rmi`, remove secrets or call Docker volume APIs.

- [ ] **Step 6: Verify and commit**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py -q
uv run ruff check tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py
uv run mypy tools/local_service_stack.py
git diff --check
git add tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py
git commit -m "feat: orchestrate local stack lifecycle"
```

---

### Task 6: Authenticated Semantic Verification, Exact Reset and CLI

**Files:**

- Modify: `tools/local_service_stack.py`
- Modify: `tests/unit/tools/test_local_service_stack.py`
- Modify: `tests/contract/test_local_service_stack_contract.py`
- Modify: `pyproject.toml`

**Interfaces:**

- Consumes: ready containers, generated secret files, `CommandRunner` and project-scoped Compose arguments.
- Produces: `ProbeResult`, `verify_stack()`, `reset_stack()`, argparse command surface and Poe tasks `stack-bootstrap`, `stack-config`, `stack-up`, `stack-status`, `stack-verify`, `stack-down`, `stack-reset`, `stack-smoke`.

- [ ] **Step 1: Add failing semantic, reset and CLI tests**

```python
@pytest.mark.parametrize(
    ("probe", "expected_code"),
    [
        ("postgresql", "postgresql_contract_failed"),
        ("qdrant", "qdrant_contract_failed"),
        ("neo4j", "neo4j_contract_failed"),
        ("redis", "redis_contract_failed"),
        ("temporal", "temporal_contract_failed"),
        ("temporal-ui", "temporal_ui_contract_failed"),
    ],
)
def test_verify_maps_each_probe_to_fixed_redacted_code(
    context: StackContext, probe: str, expected_code: str
) -> None:
    with pytest.raises(StackFailure, match=expected_code):
        verify_stack(context, runner=failing_probe_runner(probe, "DO_NOT_LEAK"))


def test_reset_requires_exact_double_confirmation(context: StackContext) -> None:
    with pytest.raises(StackFailure) as raised:
        reset_stack(context, confirm_project="knowledge-local-typo", runner=unused_runner)
    assert raised.value.exit_code is StackExitCode.CLI


def test_reset_deletes_only_exact_labeled_project_volumes(context: StackContext) -> None:
    calls = capture_calls(
        lambda runner: reset_stack(context, confirm_project="knowledge-local", runner=runner)
    )
    assert removed_volumes(calls) == {
        "knowledge-local_postgres-data", "knowledge-local_qdrant-data",
        "knowledge-local_neo4j-data", "knowledge-local_redis-data",
    }


def test_secret_rotation_occurs_only_after_all_volume_deletes_succeed(context: StackContext) -> None:
    with pytest.raises(StackFailure):
        reset_stack(
            context,
            confirm_project="knowledge-local",
            rotate_secrets=True,
            runner=fail_second_volume_delete,
        )
    assert context.paths.secret_directory.exists()


def test_cli_never_prints_raw_exception(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify"], runner=failing_probe_runner("redis", "DO_NOT_LEAK")) == 75
    captured = capsys.readouterr()
    assert "DO_NOT_LEAK" not in captured.out + captured.err
```

Also test exact CLI exit values `0,2,64,65,69,70,75`, JSON output schemas, noninteractive behavior, reset refusal on unexpected labels, absence of volumes, and `--rotate-secrets` refusal unless exact deletion succeeds.

- [ ] **Step 2: Run tests and confirm semantic/reset/CLI behavior is absent**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py tests/contract/test_local_service_stack_contract.py -q
```

- [ ] **Step 3: Implement authenticated semantic probes**

Use `docker compose exec --no-TTY` argument arrays and read credentials inside containers from mounted files. Each probe has a 10-second subprocess timeout and 30-second aggregate deadline:

```text
PostgreSQL  stack_admin succeeds; knowledge_app reaches knowledge; knowledge_app cannot
            connect temporal; temporal_service cannot connect knowledge; required DB owners exact.
Qdrant      API-key request to collections/health succeeds; same request without key is rejected.
Neo4j       cypher-shell as neo4j returns scalar 1 and reports no enabled plugin.
Redis       knowledge ACL user PING/SET/GET succeeds; unauthenticated PING is rejected;
            appendonly=yes, maxmemory=134217728, policy=noeviction.
Temporal    namespace knowledge is registered, state REGISTERED and retention exactly seven days.
Temporal UI loopback HTTP readiness returns success through the published port.
```

Return only `ProbeResult(service, is_ready, result_code, latency_ms)`; never retain command output after parsing. An absent/not-started stack returns exit `2`; a failed dependency after startup returns `75`; contract drift returns `65`.

- [ ] **Step 4: Implement exact-label reset**

Require both `--project-name <name>` and `--confirm-project <same-name>`. Query volumes using Docker label filters:

```text
com.docker.compose.project=<exact project>
com.docker.compose.volume in {postgres-data,qdrant-data,neo4j-data,redis-data}
```

Reject unknown labeled volumes. Run non-destructive Compose down first, remove only resolved names returned by both labels, verify deletion, then optionally remove `.local/stack-secrets` through `remove_secret_set_after_reset()`. Never enumerate broad filesystem paths or use globs.

- [ ] **Step 5: Add argparse and Poe command surface**

Commands and intended behavior:

```text
bootstrap  atomic secret creation/reuse; no Docker mutation
config     validations plus docker compose config --quiet; no engine required
up         bounded startup followed by semantic verify
status     stable JSON; absent stack exits 2
verify     authenticated semantic checks only
down       containers/network only; preserves volumes/secrets
reset      explicit exact-project destructive reset
smoke      CI-only disposable full contract implemented in Task 7
```

Every command accepts `--project-name`; only reset also requires `--confirm-project`; only reset accepts `--rotate-secrets`. `main()` catches `StackFailure` and prints one fixed JSON result on stdout, never exception details. Add Poe tasks that call `uv run python tools/local_service_stack.py <command>` without embedding credentials.

- [ ] **Step 6: Verify and commit**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py tests/contract/test_local_service_stack_contract.py -q
uv run ruff check tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py tests/contract/test_local_service_stack_contract.py
uv run mypy tools/local_service_stack.py
uv run python tools/local_service_stack.py --help
uv run poe --help
git diff --check
git add tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py tests/contract/test_local_service_stack_contract.py pyproject.toml
git commit -m "feat: verify and reset local stack safely"
```

---

### Task 7: Disposable Persistence, Idempotency and Recovery Smoke

**Files:**

- Create: `tests/integration/test_local_service_stack.py`
- Modify: `tests/integration/README.md`
- Modify: `tools/local_service_stack.py`
- Modify: `tests/unit/tools/test_local_service_stack.py`
- Modify: `pyproject.toml`

**Interfaces:**

- Consumes: lifecycle CLI, semantic probes and exact reset.
- Produces: `SmokeMarkerSet`, `run_smoke_contract()` and the CI-only `smoke` end-to-end contract.

- [ ] **Step 1: Add failing unit tests for smoke ordering and guaranteed cleanup**

```python
def test_smoke_runs_exact_contract_order(ci_context: StackContext) -> None:
    events: list[str] = []
    run_smoke_contract(ci_context, operations=recording_smoke_operations(events))
    assert events == [
        "reset-before", "bootstrap", "config", "up-first", "verify-first",
        "create-markers", "down-preserve", "up-second", "verify-second",
        "verify-markers", "up-idempotent", "verify-idempotent",
        "stop-redis", "verify-outage", "start-redis", "verify-recovery",
        "remove-markers", "reset-after",
    ]


def test_smoke_finally_resets_after_mid_run_failure(ci_context: StackContext) -> None:
    events: list[str] = []
    with pytest.raises(StackFailure):
        run_smoke_contract(
            ci_context,
            operations=failing_smoke_operations(events, at="verify-markers"),
        )
    assert events[-1] == "reset-after"


def test_smoke_is_ci_only(local_context: StackContext) -> None:
    with pytest.raises(StackFailure) as raised:
        run_smoke_contract(local_context, operations=unused_smoke_operations)
    assert raised.value.exit_code is StackExitCode.CLI
```

- [ ] **Step 2: Run focused tests and confirm smoke orchestration is absent**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py -q
```

- [ ] **Step 3: Implement deterministic marker and outage operations**

Create only test-scoped markers and delete them before final reset:

```text
PostgreSQL  table public.stack_smoke_marker(marker_key text primary key, marker_value text)
            in knowledge, owned by knowledge_app; insert one random CI-run token.
Qdrant      collection stack_smoke_marker_<token>; one point with a 4-value vector.
Neo4j       (:StackSmokeMarker {marker_key: <token>}).
Redis       key stack:smoke:<token> with a fixed non-secret value.
```

`SmokeMarkerSet` contains only names/IDs safe for logs, never credentials or canonical content. Marker create/read/delete calls use authenticated in-container clients with 10-second deadlines.

For recovery, run `docker compose stop --timeout 15 redis`, require `verify_stack()` to exit `75` with `redis_contract_failed`, run `docker compose start redis`, wait at most 30 seconds for health, and require the full verify to pass. This test must not invoke reset/fallback while Redis is stopped.

- [ ] **Step 4: Implement full disposable integration test**

```python
import os

import pytest

from tools.local_service_stack import main

pytestmark = pytest.mark.local_stack


def test_disposable_stack_persists_restarts_and_recovers() -> None:
    project_name = os.environ["LOCAL_STACK_TEST_PROJECT"]
    assert project_name.startswith("knowledge-ci-")
    assert main([
        "smoke", "--project-name", project_name,
        "--confirm-project", project_name,
    ]) == 0
```

Implement `run_smoke_contract()` with the exact order from Step 1 and a `try/finally` final reset. The first reset tolerates an absent project but rejects label drift. Both startups exercise all three initializers; the third `up` proves idempotency on already-running services. The second startup verifies all four markers survived `down` and container recreation. Final success requires zero matching containers, networks and volumes plus a complete unchanged secret set (unless a separate explicit rotation test is running).

Replace the integration README's bootstrap reservation with a narrow ownership statement: this spec adds only `test_local_service_stack.py`; the remaining future integration layers stay reserved; the test requires Linux Docker and uses an exact disposable `knowledge-ci-*` project.

- [ ] **Step 5: Register marker and run a real local smoke**

Add to pytest configuration:

```toml
markers = [
  "local_stack: requires a reachable Linux Docker Engine and may create disposable containers and volumes",
]
```

Then execute with a fresh bounded token:

```powershell
$env:CI = "true"
$env:LOCAL_STACK_TEST_PROJECT = "knowledge-ci-manual0813"
uv run pytest tests/integration/test_local_service_stack.py -m local_stack -q
```

Expected: one test passes; four persistence markers survive `down/up`; Redis outage is detected and recovery passes; no matching project resources remain. Inspect using exact label filters, then clear only the two variables set above.

- [ ] **Step 6: Run static checks and commit**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py tests/contract/test_local_service_stack_contract.py -q
uv run ruff check tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py tests/integration/test_local_service_stack.py
uv run mypy tools/local_service_stack.py
git diff --check
git add tools/local_service_stack.py tests/unit/tools/test_local_service_stack.py tests/integration/test_local_service_stack.py tests/integration/README.md pyproject.toml
git commit -m "test: exercise local stack persistence and recovery"
```

---

### Task 8: Cross-Platform Configuration CI, Full Smoke and Operator Documentation

**Files:**

- Create: `.github/workflows/local-service-stack.yml`
- Create: `infra/compose/README.md`
- Modify: `tests/contract/test_ci_security.py`
- Modify: `tests/contract/test_bootstrap_documentation.py`
- Modify: `README.md`

**Interfaces:**

- Consumes: all lifecycle commands and the `local_stack` integration marker.
- Produces: least-privilege Windows/Ubuntu config jobs, Ubuntu disposable smoke and documented recovery/exit contracts.

- [ ] **Step 1: Write failing CI and documentation contracts**

Extend CI tests to parse every `.github/workflows/*.yml` and require:

```python
def test_all_workflows_are_least_privilege_and_sha_pinned() -> None:
    for path in WORKFLOW_DIRECTORY.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert _top_level_permissions(text) == ["  contents: read"]
        assert "pull_request_target" not in text
        assert _all_jobs_have_positive_timeouts(text)
        assert all(_is_sha_pinned(ref) for ref in _uses_references(text))


def test_local_stack_workflow_never_receives_provider_secrets() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in ("secrets.", "R2_", "cloudflarestorage.com", "minio"):
        assert forbidden not in text
    assert "knowledge-ci-" in text
    assert "windows-latest" in text and "ubuntu-latest" in text
    assert "pytest tests/integration/test_local_service_stack.py -m local_stack" in text
```

Add documentation assertions for all eight `poe stack-*` commands, exit codes `0,2,64,65,69,70,75`, Docker Desktop Linux-container limitation, exact reset confirmation, persistence behavior and explicit R2 exclusion.

- [ ] **Step 2: Run contracts and confirm workflow/docs are absent**

```powershell
uv run pytest tests/contract/test_ci_security.py tests/contract/test_bootstrap_documentation.py -q
```

- [ ] **Step 3: Create config-only jobs with a checksum-pinned Compose 2.30.0**

Create a dedicated workflow with path-filtered pull-request/push triggers for `infra/**`, `tools/local_service_stack.py`, its tests, `pyproject.toml`, lockfiles and the workflow itself; add nightly `schedule` on the default branch and `workflow_dispatch`. Use `permissions: contents: read`, cancellation concurrency and no repository secrets. Pin checkout and setup-uv to the same SHAs used by `quality.yml`.

Install Docker Compose `v2.30.0` from GitHub releases and verify before installation:

```text
Linux x86_64   1cddcb3399cc68c385796a6ab441ab5734d4c6a0cb4713bd2bf3f0d384550a38
Windows x86_64 07ed10572bed0c42e5477bd33f9eb8f1b1c640d83120cc59feb7ce28f0c1bf86
```

The Windows and Ubuntu config jobs each use `timeout-minutes: 10`, run `uv sync --all-packages --frozen`, lifecycle unit/static-security tests, `poe stack-bootstrap`, and `poe stack-config`. Repeat `stack-config` with all eight ports set to distinct non-default values, then run malformed and duplicate override cases and assert exit `64`. The Windows job never contacts the Docker engine. Both jobs assert the working tree remains unchanged outside ignored `.local/`, and scan sanitized output for secret filenames, known test sentinels and R2 tokens.

- [ ] **Step 4: Add Ubuntu full smoke job**

Use `ubuntu-latest`, `timeout-minutes: 20`, `CI=true`, and a bounded project name derived only from `github.run_id` plus `github.run_attempt`, for example `knowledge-ci-${run_id}-${run_attempt}` after regex/length validation.

Run:

```bash
uv sync --all-packages --frozen
uv run python tools/local_service_stack.py bootstrap --project-name "$LOCAL_STACK_TEST_PROJECT"
uv run python tools/local_service_stack.py config --project-name "$LOCAL_STACK_TEST_PROJECT"
uv run pytest tests/integration/test_local_service_stack.py -m local_stack -q \
  --junitxml=.local/test-results/local-service-stack.xml
```

In an `if: always()` cleanup step, invoke exact reset with identical project/confirmation. Then assert no container/network/volume remains with `com.docker.compose.project=<exact>`. Upload only the JUnit XML using `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02` (`v4.6.2`); never upload `.local/stack-secrets`, Compose render, container logs or Docker inspect output.

- [ ] **Step 5: Document commands, safety and recovery**

Root `README.md` documents prerequisites and the eight high-level Poe tasks. `infra/compose/README.md` owns:

- exact service/version table and `linux/amd64` limitation;
- loopback ports and override names;
- first bootstrap, normal `up/status/verify/down`, and exact reset examples;
- `down` preserves volumes/secrets; `reset` deletes only exact labeled volumes; rotation is explicit;
- partial secrets and missing-secrets-with-volumes are terminal and require operator investigation;
- init/schema/namespace drift behavior and exit-code table;
- Windows uses Docker Desktop Linux containers; only Ubuntu CI exercises real containers;
- R2 is the sole future canonical object store but is not configured, contacted or tested by this local-service stack.

Do not document raw secret paths beyond the owning directory, values, `docker compose exec` credential commands or instructions that bypass authentication.

- [ ] **Step 6: Run the full acceptance sequence**

```powershell
uv run pytest tests/unit/tools/test_local_service_stack.py tests/contract/test_local_service_stack_contract.py tests/contract/test_ci_security.py tests/contract/test_bootstrap_documentation.py -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src tools tests
uv run poe verify
git diff --check
git status --short
```

On a Linux Docker host, additionally run:

```bash
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-acceptance0813 \
  uv run pytest tests/integration/test_local_service_stack.py -m local_stack -q
docker container ls -a --filter label=com.docker.compose.project=knowledge-ci-acceptance0813 --format '{{.ID}}'
docker network ls --filter label=com.docker.compose.project=knowledge-ci-acceptance0813 --format '{{.ID}}'
docker volume ls --filter label=com.docker.compose.project=knowledge-ci-acceptance0813 --format '{{.Name}}'
```

Expected: repository gate and smoke pass; all three Docker inventory commands print nothing; no output/artifact contains secret or R2 material.

- [ ] **Step 7: Commit CI and operator handoff**

```powershell
git add .github/workflows/local-service-stack.yml infra/compose/README.md README.md tests/contract/test_ci_security.py tests/contract/test_bootstrap_documentation.py
git commit -m "ci: verify local service stack"
```

## Final Acceptance Checklist

- [ ] Exactly 7 long-running services, 3 init jobs, 4 volumes, 1 private network and 8 loopback publications are present.
- [ ] All eight image lock entries resolve to the reviewed manifest digests on `linux/amd64`.
- [ ] Secret bootstrap is atomic, reuses a complete set and refuses partial/missing-with-volume states.
- [ ] PostgreSQL privileges deny cross-database access; no application schema exists yet.
- [ ] Temporal schemas and namespace bootstrap are rerunnable and fail on forward/drift states.
- [ ] Authenticated semantic verification covers all services and rejects unauthenticated Qdrant/Redis access.
- [ ] `down/up` preserves PostgreSQL, Qdrant, Neo4j and Redis markers.
- [ ] Redis outage exits `75`; recovery restores readiness without reset/fallback.
- [ ] Exact-label reset leaves no project Docker resources and never removes unrelated resources.
- [ ] Windows config and Ubuntu config/full-smoke CI pass without provider credentials.
- [ ] `uv run poe verify`, `git diff --check`, AGENTS/CLAUDE line-count checks and final `git status --short` are clean.
- [ ] Compose, tools, tests, workflows and docs contain no MinIO service and no R2 access path.
