# Local Service Stack Design

**Status:** Approved design
**Date:** 2026-08-13
**Phase:** Phase 1 — Bootstrap and canonical core
**Canonical plan:** `docs/20-IMPLEMENTATION_PLAN.md`
**Depends on:** `phase-one-workspace-bootstrap-design.md`, `runtime-configuration-and-diagnostics-design.md`

## 1. Objective

Provide one reproducible Docker Compose stack for the stateful services required by the Phase 1 local and integration environment:

- PostgreSQL for canonical application state and Temporal persistence.
- Qdrant for the rebuildable search projection.
- Neo4j Community Edition for the rebuildable graph projection.
- Redis for ephemeral cache and coordination state.
- Temporal Server, UI, schema tools and CLI administration.

An empty development machine with the pinned toolchain must be able to create local service credentials, start the complete stack, wait for authenticated semantic readiness and preserve selected service state across a normal stop/start cycle without committed secrets or LAN exposure.

Cloudflare R2 is the only canonical object store, but it is a managed dependency outside this Compose topology. This design neither receives R2 credentials nor contacts an R2 bucket. The later content-addressable object-storage design owns R2 configuration, credentials, upload/read behavior and live test-bucket integration.

## 2. Scope

This design owns:

- One canonical Compose topology under `infra/compose/`.
- Exact service versions and immutable registry-image digests.
- Loopback-only host publishing and one private container network.
- Named volumes and safe local reset behavior.
- Generated file-backed credentials and service authentication.
- PostgreSQL role/database provisioning without application tables.
- Temporal schema initialization and namespace provisioning.
- Container health checks and authenticated semantic readiness probes.
- Conservative CPU and memory guardrails for a 16 GB development host.
- Cross-platform lifecycle commands for Linux and Windows Docker Desktop.
- Static, unit, integration and CI acceptance gates.

This design does not own:

- Cloudflare R2 endpoint, bucket names, credentials or live tests.
- API, MCP, worker, Web App or Obsidian containers.
- Alembic application migrations or canonical PostgreSQL tables.
- Python database, Redis, Qdrant, Neo4j, Temporal or R2 adapters.
- Qdrant collections, payload indexes or production projection routes.
- Neo4j constraints, indexes, nodes or relationships.
- Redis business keys or durable business state in Redis.
- Content-addressable object keys, streaming upload or corruption handling.
- Object-store fallback, dual-write or provider cutover.
- Reverse proxy, public TLS or production two-host deployment.
- Backup/PITR/restore automation.
- Prometheus, Grafana, Loki, Tempo, Alloy, Alertmanager or Sentry.
- Production resource sizing or trace sampling.

## 3. Selected approach

Use one canonical Compose file with explicit long-running services and idempotent one-shot initialization jobs. A typed Python lifecycle tool validates preconditions and supplies the same commands on Linux and Windows.

The default stack starts every selected dependency, including Temporal UI and the Temporal CLI toolbox. There are no optional profiles in this baseline.

Rejected alternatives:

1. Splitting the stack across several composable files would multiply supported topology combinations and allow networks, credentials, health semantics and versions to drift.
2. Replacing Temporal with its development server would not exercise the PostgreSQL-backed server, schema initialization or recovery topology required by the canonical architecture.
3. Using disposable Qdrant, Neo4j or Redis storage would fail to prove the agreed local persistence lifecycle.
4. Running MinIO locally would add an archived source dependency, reproducible image build, patching, backup and security maintenance to a one-person system. Cloudflare R2 is now the sole canonical object store.
5. Adding another local S3 emulator would create a second behavior contract without providing production recovery. Offline adapter tests use a test double; live compatibility tests use the dedicated R2 test bucket.

## 4. Topology

### 4.1 Long-running services

```text
postgresql
qdrant
neo4j
redis
temporal
temporal-ui
temporal-cli
```

### 4.2 One-shot services

```text
postgres-provision
temporal-schema-setup
temporal-namespace-bootstrap
```

`temporal-schema-setup` uses the Temporal administrative image and exits after applying the Temporal and visibility schemas. `temporal-cli` is a separate long-running toolbox based on the approved CLI image. The toolbox stays inert between operator commands and proves its connection through its health check.

### 4.3 Dependency graph

```text
postgresql healthy
  -> postgres-provision completed
     -> temporal-schema-setup completed
        -> temporal healthy
           -> temporal-namespace-bootstrap completed
              -> temporal-ui ready
              -> temporal-cli ready

qdrant healthy
neo4j healthy
redis healthy
```

Qdrant, Neo4j and Redis do not depend on PostgreSQL or Temporal. Application processes run on the host during this phase and reach dependencies through loopback-published ports.

### 4.4 Compose identity

- Compose services do not set `container_name`.
- The lifecycle tool uses project name `knowledge-local` locally.
- CI project names match `knowledge-ci-<bounded-lowercase-token>`.
- Compose applies its project prefix to containers, network and volumes.
- Parallel CI jobs use different validated project names.
- No project name is accepted unless it matches `^[a-z0-9][a-z0-9_-]{0,62}$`.

## 5. Repository artifacts

```text
infra/
└── compose/
    ├── compose.yaml
    ├── images.lock.yaml
    ├── README.md
    ├── config/
    │   └── temporal/
    │       └── dynamicconfig.yaml
    └── scripts/
        ├── postgres-provision.sh
        ├── temporal-schema-setup.sh
        ├── temporal-namespace-bootstrap.sh
        └── temporal-secret-entrypoint.sh

tools/
└── local_service_stack.py

tests/
├── unit/tools/test_local_service_stack.py
├── contract/test_local_service_stack_contract.py
└── integration/test_local_service_stack.py

.github/workflows/
└── local-service-stack.yml
```

Generated local artifacts live only under:

```text
.local/
├── stack-secrets/
└── stack-state/
```

The repository ignores `.local/` as a whole. No generated credential, rendered secret configuration, environment dump or state file is committed.

`stack-state` may store only non-sensitive schema versions and project identity. It must not contain a credential, rendered Compose document, environment dump or absolute secret path.

## 6. Version and image contract

### 6.1 Approved versions

| Component | Approved reference | Role |
|---|---|---|
| PostgreSQL | `postgres:18.4-bookworm` | Canonical and Temporal databases |
| Qdrant | `qdrant/qdrant:v1.18.2` | Search projection service |
| Neo4j Community | `neo4j:5.26.28-community` | Single-instance graph projection |
| Redis Open Source | `redis:8.6.4` | Ephemeral cache/coordination |
| Temporal Server | `temporalio/server:1.31.2` | Durable workflow server |
| Temporal schema tools | `temporalio/admin-tools:1.31.2` | PostgreSQL schema setup/update |
| Temporal UI | `temporalio/ui:2.53.0` | Local workflow administration UI |
| Temporal CLI | `temporalio/temporal:1.8.0` | Operator CLI toolbox |

The official `redis:8.6.4-bookworm` tag does not exist. The approved exact Redis tag is `redis:8.6.4`; its manifest is pinned like every other registry image.

Neo4j uses the explicit Community tag and installs no APOC, Graph Data Science or downloaded plugin. Redis uses the official Open Source image. Temporal does not use `auto-setup` or `server start-dev`.

### 6.2 Registry pinning

Every upstream registry image reference in `compose.yaml` contains both:

```text
exact human-readable tag
+ immutable multi-platform manifest digest
```

The tag documents intent; the digest determines bytes. Major-only, minor-only, `latest`, date aliases and environment-selected image names are forbidden.

`images.lock.yaml` records for each logical component:

```text
component
upstream_repository
version
tagged_reference
manifest_digest
supported_platforms
verified_at
```

A contract test checks that every registry image in Compose matches exactly one lock entry and every lock entry is consumed. The lock contains no registry credential.

`linux/amd64` is the required platform for Linux hosts, Ubuntu CI and Windows Docker Desktop in Linux-container mode. `linux/arm64` may be added only after every manifest and full smoke contract pass on that platform.

### 6.3 Upgrade policy

An upgrade is a dedicated pull request that changes version and digest together and includes:

- Upstream release-note and security review.
- Database or on-disk compatibility review.
- Compose configuration validation.
- Empty bootstrap and persisted-volume restart smoke.
- Temporal Server/UI/CLI compatibility evidence where applicable.
- Rollback or forward-only recovery note.

No automated dependency update may merge an image change without these gates.

## 7. Network and ports

### 7.1 Network contract

Compose creates one project-scoped bridge network named logically `service-backplane`.

- Containers discover one another only by Compose service name.
- No service uses host networking.
- No service publishes a port without an explicit loopback host address.
- No container mounts the Docker socket.
- No container mounts the repository root, host root or user home.
- Static config mounts are read-only.
- Writable named volumes are limited to the four service data directories and the
  rebuildable Temporal health-tool bridge populated by its one-shot initializer.

Local service traffic is plaintext because it stays inside the project bridge or host loopback boundary. Authentication remains enabled. This local/test exception is not a production TLS decision.

### 7.2 Default host ports

| Variable | Default | Container port | Purpose |
|---|---:|---:|---|
| `POSTGRES_PORT` | `5432` | `5432` | PostgreSQL |
| `QDRANT_HTTP_PORT` | `6333` | `6333` | Qdrant REST/health UI |
| `QDRANT_GRPC_PORT` | `6334` | `6334` | Qdrant gRPC |
| `NEO4J_HTTP_PORT` | `7474` | `7474` | Neo4j Browser/HTTP |
| `NEO4J_BOLT_PORT` | `7687` | `7687` | Neo4j Bolt |
| `REDIS_PORT` | `6379` | `6379` | Redis |
| `TEMPORAL_GRPC_PORT` | `7233` | `7233` | Temporal frontend |
| `TEMPORAL_UI_PORT` | `8080` | `8080` | Temporal UI |

Each publication renders as:

```text
127.0.0.1:${VARIABLE:-default}:container_port
```

These eight names are the complete port-override allowlist. Credentials, image references, volume names, service hostnames and security switches cannot be overridden through environment variables.

The lifecycle tool validates every effective port before Compose execution:

- ASCII decimal integer only.
- Range `1024..65535`.
- No duplicate effective host port.
- Not already bound on loopback by a process outside this Compose project.

The tool never chooses another port automatically. Direct `docker compose` invocation is not the supported lifecycle because it bypasses validation.

## 8. Secrets and authentication

### 8.1 Secret set

The lifecycle tool creates one atomic secret set under `.local/stack-secrets/`:

```text
postgres_admin_password
postgres_application_password
postgres_temporal_password
qdrant_api_key
neo4j_auth
redis_acl
redis_application_password
```

Fixed non-secret principals are:

```text
PostgreSQL administrator  stack_admin
PostgreSQL application    knowledge_app
PostgreSQL Temporal       temporal_service
Neo4j user                neo4j
Redis application user    knowledge
Temporal namespace        knowledge
```

Passwords and the Qdrant API key use a cryptographically secure generator and at least 192 bits of entropy. Values use an ASCII alphabet accepted without shell escaping by every owning service. Files contain no terminal newline except `redis_acl`, whose native multi-line grammar requires line delimiters.

`neo4j_auth` contains the exact native Docker secret shape `neo4j/<generated-password>`. `redis_acl` disables the default user and enables only the named `knowledge` user. Qdrant receives a generated secret configuration file derived atomically from its API-key secret; the rendered secret configuration remains in `.local/stack-secrets/` and is mounted read-only.

R2 credentials are deliberately absent. They belong to the object-storage adapter secret contract and must never be copied into Compose or stack lifecycle state.

### 8.2 Creation behavior

- The tool resolves the repository root and proves the secret directory remains beneath `<workspace>/.local/stack-secrets`.
- Generation occurs in a sibling staging directory followed by atomic rename.
- An already complete set is reused byte-for-byte.
- Existing files are never overwritten by `bootstrap` or `up`.
- A partial set is terminal; the tool does not guess whether regeneration is safe.
- A missing set while project volumes exist is terminal.
- Secret filenames, absolute paths and values are absent from normal/error output.
- On POSIX, directories use `0700` and files use `0600`.
- On Windows, the design relies on the user-owned directory and Docker Desktop mount boundary; it does not claim complete NTFS ACL validation.

### 8.3 Consumption behavior

Compose declares credentials through top-level `secrets:` entries.

- Native `*_FILE` support is used for PostgreSQL and Neo4j.
- Redis reads a mounted ACL file.
- Qdrant reads a mounted generated configuration file.
- Temporal adapters read the PostgreSQL password file inside the container immediately before `exec` of the fixed command.

If an upstream image lacks a file-based option, a repository-owned fixed entrypoint may export the value only inside the target container process. The secret must not appear in the Compose model, image metadata, host process arguments or `docker inspect` environment configuration.

No credential is accepted from a host environment variable, `.env`, CLI argument, committed YAML or JSON file.

### 8.4 Rotation

Changing a file alone is not credential rotation because persisted PostgreSQL, Neo4j and Redis state may retain the old credential.

- `bootstrap` never rotates.
- `stack reset` keeps secrets by default.
- `stack reset --rotate-secrets` first deletes the exact project volumes successfully, then removes the exact validated local-stack secret set.
- The next `bootstrap` creates a new atomic set.
- In-place rotation of a populated local stack is deferred until an owning credential-rotation spec exists.

## 9. Service-specific contracts

### 9.1 PostgreSQL

The PostgreSQL 18 image stores version-specific `PGDATA` beneath `/var/lib/postgresql`; the named volume mounts at `/var/lib/postgresql`, not the PostgreSQL 17-and-earlier `/var/lib/postgresql/data` path.

The primary container initializes only the `stack_admin` superuser. After authenticated readiness, `postgres-provision` idempotently ensures:

| Database | Owner | Purpose |
|---|---|---|
| `knowledge` | `knowledge_app` | Empty application database for the next Alembic spec |
| `temporal` | `temporal_service` | Temporal primary persistence |
| `temporal_visibility` | `temporal_service` | Temporal visibility persistence |

Rules:

- Application and Temporal roles cannot be superusers.
- The application role has no rights on either Temporal database.
- The Temporal role has no rights on `knowledge`.
- Provisioning reconciles ownership and connection permission without dropping a database or role.
- Provisioning never creates an application table, extension or Alembic revision.
- PostgreSQL logs do not log statements, bind parameters or passwords.
- `shared_buffers` is explicitly bounded beneath the container memory cap.

### 9.2 Temporal

`temporal-schema-setup` runs the official `temporal-sql-tool` workflow against both Temporal databases using the PostgreSQL 12+ plugin supported by Temporal. The plugin name identifies the compatible Temporal SQL dialect family; it does not downgrade PostgreSQL.

The job:

1. Reads the Temporal database password from its mounted secret.
2. Detects whether each schema is absent or already versioned.
3. Creates an absent schema from exact Server `1.31.2` schema assets.
4. Applies supported updates only when the existing version is behind.
5. Fails when the version is ahead, corrupt or incompatible.
6. Verifies both schema-version tables before exiting `0`.

Temporal Server starts only after this job succeeds. It uses the same PostgreSQL server but separate database and role boundaries from application state.

`temporal-namespace-bootstrap` waits for the frontend API, then idempotently ensures:

```text
namespace  knowledge
retention  7 days
```

An existing namespace with another retention is configuration drift and fails instead of being silently modified.

Temporal UI connects only to `temporal:7233`. Temporal CLI uses the same internal address and defaults operator commands to namespace `knowledge`. Neither component receives PostgreSQL credentials.

Temporal local transport has no TLS/auth layer because it is loopback-published and private-network-only. This exception cannot be copied to production.

### 9.3 Qdrant

Qdrant runs single-node with:

- Persistent `/qdrant/storage` named volume.
- REST on `6333` and gRPC on `6334`.
- Cluster peer port `6335` unpublished.
- Admin API key enabled from the mounted generated configuration.
- CORS disabled unless a later UI requirement changes the contract.
- No collection, alias, payload index or vector schema at bootstrap.

Qdrant `/livez`, `/healthz` and `/readyz` are intentionally public health endpoints even when API-key authentication is enabled. Container health uses `/readyz`; semantic readiness separately performs an authenticated API operation. A negative probe to a protected endpoint without `api-key` must fail.

### 9.4 Neo4j Community

Neo4j runs one Community Edition instance with:

- Persistent `/data` named volume.
- Native authentication enabled through `NEO4J_AUTH_FILE`.
- HTTP Browser on `7474` and Bolt on `7687`, both loopback-only.
- HTTPS connector disabled for local use.
- Telemetry disabled when the image supports the setting.
- No downloaded plugin and no `NEO4J_PLUGINS` setting.
- No application constraint, index, label, node or relationship.

Community Edition has one native administrative user and no Enterprise RBAC. This local baseline does not imply production least-privilege parity.

Heap initial/max size and page cache are explicitly configured so their sum leaves native-memory headroom below the container cap.

### 9.5 Redis Open Source

Redis runs standalone with:

- Persistent `/data` named volume for selected local lifecycle continuity.
- An ACL file that disables the default user.
- One authenticated `knowledge` user.
- Protected mode enabled.
- No module loaded.
- No public bind beyond the Compose/loopback boundary.
- Bounded memory consistent with its container cap.
- Persistence enabled sufficiently to prove local stop/start continuity.

The Redis volume does not make Redis canonical. Application correctness must tolerate Redis loss, flush or eviction.

## 10. Persistent volumes and restart behavior

| Logical volume | Mount | Authority classification |
|---|---|---|
| `postgres-data` | `/var/lib/postgresql` | Canonical application state plus Temporal history |
| `qdrant-data` | `/qdrant/storage` | Rebuildable projection |
| `neo4j-data` | `/data` | Rebuildable projection |
| `redis-data` | `/data` | Ephemeral/non-canonical state |
| `temporal-health-tools` | `/opt/knowledge/health` | Rebuildable pinned health binary; non-data and non-canonical |

These are the exact five project volumes. `temporal-schema-setup` populates
`temporal-health-tools`; Temporal Server mounts it read-only and can rebuild it from the
pinned admin-tools image. `docker compose down` does not delete these volumes. Long-running
services use `restart: unless-stopped`. One-shot initialization jobs use `restart: "no"`;
retry happens only through a later explicit lifecycle invocation.

No bind mount is used for database data. This avoids host-filesystem ownership and Windows/WSL consistency problems.

## 11. Resource guardrails

These limits protect a 16 GB development host. They are not production sizing evidence.

| Service | Memory cap | CPU cap |
|---|---:|---:|
| PostgreSQL | 1.5 GB | 1.5 |
| Qdrant | 3 GB | 2.0 |
| Neo4j | 2.5 GB | 2.0 |
| Redis | 256 MB | 0.5 |
| Temporal Server | 1 GB | 1.5 |
| Temporal UI | 256 MB | 0.5 |
| Temporal CLI | 128 MB | 0.25 |
| Each one-shot job | 512 MB | 0.5 |

Compose uses non-Swarm limits supported by local Docker Compose. It does not rely only on `deploy.resources` semantics that a local engine might ignore.

Neo4j heap/page-cache configuration, PostgreSQL shared buffers and Redis maximum memory stay below their caps with explicit native-memory headroom. A process killed for exceeding its limit is unhealthy and makes stack readiness fail.

The production operations spec may replace these guardrails only after measured capacity evidence.

## 12. Health and semantic readiness

### 12.1 Two levels

Container health answers whether the service process accepts a basic local request. Stack readiness answers whether the authenticated contract needed by future application code works.

Health alone never makes the stack ready.

### 12.2 Container health checks

| Service | Container health |
|---|---|
| PostgreSQL | Authenticated `SELECT 1` through local socket/TCP |
| Qdrant | HTTP `/readyz` |
| Neo4j | Authenticated `RETURN 1` through `cypher-shell` |
| Redis | Authenticated `PING` through `redis-cli` |
| Temporal Server | gRPC cluster health using pinned CLI/admin tooling bridged read-only from `temporal-health-tools` |
| Temporal UI | Local HTTP readiness |
| Temporal CLI | `temporal operator cluster health` against internal Server |

Secrets used by a health check are read from mounted files at execution time and never embedded in the health command stored in image/Compose configuration.

Checks use bounded intervals, timeouts, retries and start periods. Neo4j and Temporal receive longer start periods than Redis. The lifecycle command enforces an overall finite startup deadline and identifies the failed logical service without dumping raw logs.

### 12.3 Semantic verification

`stack verify` performs:

| Component | Required verification |
|---|---|
| PostgreSQL | Authenticate intended principals in all three databases; confirm cross-database privilege denial |
| Qdrant | Authenticated protected API request; equivalent request without key is denied |
| Neo4j | Authenticated `RETURN 1`; invalid/no credential is denied |
| Redis | Authenticated `PING`; default/unauthenticated client is denied |
| Temporal | Cluster health and exact `knowledge` namespace/7-day retention describe |
| Temporal UI | HTTP success through the effective loopback port |

Verification is read-only except for bounded integration-test markers in the dedicated smoke workflow. It prints no settings dump, credential, secret path or raw vendor exception.

R2 readiness is not part of `stack verify`. The owning object-storage adapter provides a separate authenticated `check-runtime`/live contract against the selected R2 environment.

## 13. Lifecycle interface

The supported entrypoint is:

```text
uv run python tools/local_service_stack.py <command>
```

Starting containers requires a reachable Linux-container Docker Engine and Docker Compose CLI `>=2.30.0`. Static `config` validation requires the pinned Compose CLI but not a running engine. The lifecycle tool checks capabilities and version before mutation.

Poe exposes matching tasks:

```text
stack-bootstrap
stack-config
stack-up
stack-status
stack-verify
stack-down
stack-reset
stack-smoke
```

### 13.1 Commands

| Command | Contract |
|---|---|
| `bootstrap` | Create or validate the atomic local-service secret set without starting Docker services |
| `config` | Validate tools, ports, secrets, image-lock agreement and rendered Compose syntax |
| `up` | Run preflight, start the dependency graph, wait and run semantic verification |
| `status` | Return sanitized service/init/health/readiness state |
| `verify` | Re-run every local-service semantic readiness probe |
| `down` | Stop/remove project containers and network while retaining volumes and secrets |
| `reset` | Delete exact project containers/network/volumes after confirmation; retain secrets by default |
| `smoke` | Execute the disposable full-stack integration contract in a CI-scoped project |

`up` executes in this order:

```text
Docker and Compose prerequisite check
-> port validation and collision detection
-> secret-set validation
-> registry tag/digest validation
-> docker compose config --quiet
-> docker compose up
-> wait for init jobs and health
-> semantic verify
-> ready
```

It does not contact R2, change a port, regenerate a secret, choose another image, delete a resource or weaken authentication.

### 13.2 Process safety

- Subprocesses are invoked with argument arrays and `shell=False`.
- User-controlled values never become executable command fragments.
- Environment passed to a subprocess is an explicit allowlist plus required inherited platform values.
- Output capture is bounded.
- Every wait has a deadline.
- Ctrl+C cancels waiting without deleting data.
- A failed `up` leaves evidence for inspection but performs no automatic destructive rollback.

### 13.3 Exit codes

| Code | Meaning |
|---:|---|
| `0` | Requested lifecycle operation succeeded |
| `2` | CLI syntax or argument error |
| `64` | Docker/Compose prerequisite, invalid port or port collision |
| `65` | Secret, configuration or image-lock contract failure |
| `69` | Container startup or initialization job failure |
| `70` | Unexpected lifecycle-tool failure |
| `75` | Semantic readiness failed or timed out |

The lifecycle tool maps raw subprocess failures to this closed result set. It never returns success because a failure was merely logged.

## 14. Failure behavior

- Missing Docker or unsupported Compose is terminal before mutation.
- Invalid or occupied ports are terminal before Compose starts a container.
- A missing/partial secret set is terminal; no default credential is substituted.
- An image tag/digest mismatch is terminal; no floating pull occurs.
- PostgreSQL role/database drift that cannot be reconciled non-destructively is terminal.
- Temporal schema ahead/corrupt/incompatible is terminal.
- Temporal namespace retention drift is terminal.
- A one-shot job failure prevents dependent services from satisfying readiness.
- A container marked healthy while its authenticated semantic probe fails leaves the stack `not ready` and returns `75`.
- Dependency failure never triggers deletion, recreation or object-store behavior.
- Vendor exceptions and command output are not copied unfiltered into lifecycle diagnostics.

Failure output may contain only bounded stable metadata such as:

```text
service
stage
result_code
container_state
health_state
exit_code
attempt_count
```

It must not contain credentials, secret filenames/paths, rendered configuration, connection URLs with user information, command arguments containing credentials or raw database statements.

## 15. Reset safety

Reset is the only volume-deletion interface owned by this spec.

Before deletion, the lifecycle tool:

1. Resolves the absolute workspace and canonical Compose path.
2. Validates the exact project name.
3. Enumerates expected logical volumes from the validated Compose model.
4. Resolves actual resources only through exact Compose project labels.
5. Rejects any resource whose label/project identity differs.
6. Shows a count and logical resource types without absolute storage paths.
7. Requires the operator to type the exact project name.

The only accepted logical volume labels are:

```text
postgres-data
qdrant-data
neo4j-data
redis-data
temporal-health-tools
```

After non-destructive Compose down detaches them, reset deletes every resolved volume in
that exact set, including the rebuildable health-tool volume, and verifies each is absent.
Any other project-labeled volumes are unknown labeled volumes and make reset refuse all
volume deletion.

Non-interactive reset is allowed only when all are true:

```text
CI=true
project name begins knowledge-ci-
explicit non-interactive confirmation flag is present
resolved resources all carry that exact project label
```

Reset never calls Docker system/volume/image prune, uses no wildcard and does not delete an image. If volume deletion partially fails, secret rotation does not run. Reset has no access to an R2 bucket and cannot delete R2 objects.

## 16. Test strategy

### 16.1 Static Compose contracts

Tests parse the canonical/rendered Compose model and prove:

- Exact seven long-running and three one-shot services exist.
- Every registry image has exact tag and digest.
- Every image lock entry is consumed exactly once.
- No `latest`, floating tag or host-selected image exists.
- No explicit `container_name` exists.
- No privileged mode, host networking, Docker socket or broad host bind mount exists.
- Every published port binds `127.0.0.1`.
- Only the eight approved port variables occur.
- Every persistent directory maps to the exact named volume.
- Exactly five named volumes exist: four service-data volumes plus the rebuildable,
  non-canonical `temporal-health-tools` bridge.
- Credential consumers declare the required Compose secret.
- No plaintext credential-like field exists in Compose/config.
- Every long-running service has a health check, restart policy and resource cap.
- Every one-shot job has `restart: "no"` and correct completion dependency.
- PostgreSQL 18 volume path is `/var/lib/postgresql`.
- Qdrant peer port `6335` is not published.
- Static mounts are read-only.
- No MinIO image, service, volume, secret, script, port or profile exists.

### 16.2 Lifecycle unit tests

- Atomic first secret generation and complete-set reuse.
- Partial secret-set rejection.
- Existing-volume plus missing-secret rejection.
- POSIX permission enforcement and explicit Windows boundary behavior.
- Default, valid custom, malformed, duplicate and occupied ports.
- Project-name validation and CI namespace restrictions.
- Argument-array subprocess invocation.
- Bounded timeout and cancellation.
- Exit-code mapping for every failure class.
- Sentinel-bearing subprocess errors absent from output.
- Reset exact-label resolution and refusal on foreign resources.
- Secret rotation does not occur after failed volume deletion.

### 16.3 Full-stack smoke

The Ubuntu smoke starts from an empty CI-scoped project:

1. Generate local service credentials.
2. Start the complete Compose graph.
3. Wait for all init and health contracts.
4. Run all semantic readiness and unauthenticated-denial probes.
5. Confirm the three PostgreSQL database/role boundaries.
6. Confirm both Temporal schemas and exact namespace retention.
7. Create exact disposable markers in PostgreSQL, Qdrant, Neo4j and Redis.
8. Stop with `down`, start again and prove every selected marker persists.
9. Re-run all initialization jobs and prove idempotency.
10. Stop one dependency, prove `verify` returns `75`, restart it and prove recovery.
11. Remove exact disposable markers.
12. Reset only the CI project and prove no labeled resource remains.

Marker names contain a unique CI run token. Cleanup uses exact names and runs in `finally`. It never touches an application table, production-shaped Qdrant collection or production Neo4j label.

Redis marker persistence proves the selected local lifecycle only; it is not a correctness or durability guarantee.

The local-stack smoke never contacts R2. Live R2 behavior belongs to the content-addressable object-storage pipeline and uses the dedicated test bucket with exact run-prefix cleanup.

### 16.4 Leakage and security tests

- Repository scan finds no generated secret or `.env`.
- `docker compose config` contains secret references but no secret values.
- `docker inspect` environment/arguments contain no host-supplied secret value.
- Lifecycle stdout/stderr contains no sentinel secret, path or credential-bearing URL.
- Protected endpoints reject missing/invalid credentials.
- Health endpoints deliberately public by upstream design are limited to health metadata.
- Host port inspection shows only `127.0.0.1` publication.
- Containers cannot access the Docker socket.
- Compose and lifecycle artifacts contain no R2 credential, bucket or endpoint.

## 17. CI contract

### 17.1 Cross-platform configuration job

Ubuntu and Windows jobs install an exact pinned Docker Compose CLI and run:

- Lifecycle unit tests.
- Static Compose/security contracts.
- `stack config` with default ports.
- `stack config` with every port overridden to a distinct valid value.
- Invalid/duplicate-port negative cases.

Windows validates the same Compose model but does not start Linux containers. Windows local runtime support means Windows Docker Desktop in Linux-container mode uses the same committed Compose file and lifecycle tool.

### 17.2 Ubuntu smoke job

The full-stack workflow runs:

- On a pull request when `infra/**`, the lifecycle tool, stack tests, relevant dependency pins or workflow itself changes.
- Nightly on the default branch.
- On manual dispatch.

The workflow uses a unique CI project name and finite job timeout. It uploads only sanitized test reports. Raw service logs and environment dumps are not uploaded automatically.

Before this design is considered implemented, a full smoke must pass on the same final commit. A nightly result from another commit is insufficient.

### 17.3 Existing quality gate

Static/unit contracts join the existing `uv run --all-packages --frozen poe verify` graph. The heavy Docker smoke remains a separate workflow. Neither job may collect zero required tests or replace a failed assertion with a warning.

The local-stack workflow receives no Cloudflare secret. The later live R2 workflow runs only on trusted contexts with test-bucket credentials and is not part of this Compose job.

## 18. Acceptance criteria

Implementation is complete only when all criteria pass on the same final commit:

1. The repository contains one canonical Compose topology with exact approved services and initialization jobs.
2. Every registry image has an exact version tag and immutable manifest digest matching the image lock.
3. No `.env`, plaintext credential, floating tag, MinIO artifact or R2 credential is used by the stack.
4. Secret bootstrap is atomic, idempotent, non-overwriting and safe on Linux and the documented Windows boundary.
5. PostgreSQL, Qdrant, Neo4j and Redis require authentication for protected operations.
6. Every published port binds only `127.0.0.1`; Qdrant peer and all unneeded ports remain unpublished.
7. The eight approved host-port overrides validate correctly without becoming Python `KNOWLEDGE_*` runtime settings.
8. PostgreSQL contains separate `knowledge`, `temporal` and `temporal_visibility` databases with approved non-superuser ownership boundaries and no application tables.
9. Temporal Server uses PostgreSQL schemas managed by one-shot tools, not `auto-setup` or the development server.
10. The `knowledge` namespace exists with exactly seven-day retention and bootstrap is idempotent.
11. Qdrant and Neo4j start empty; Redis contains no application-owned bootstrap data.
12. Every service has bounded health checks, semantic readiness and a resource cap consistent with the 16 GB host guardrail.
13. `down/up` preserves selected service markers and repeated initialization does not destroy or duplicate state.
14. A dependency outage makes `verify` fail with code `75` and never triggers destructive recovery or object-store behavior.
15. Reset deletes only the exact five project-labeled volumes (including
    `temporal-health-tools`) after required confirmation, refuses unknown labeled volumes,
    never prunes global Docker state and cannot access R2.
16. Lifecycle diagnostics and CI artifacts contain no sentinel credential, secret path or raw vendor exception text.
17. Static/config validation passes on Ubuntu and Windows; full authenticated integration smoke passes on Ubuntu.
18. Root and infrastructure READMEs document prerequisites, ports, commands, persistence, safe reset and the explicit R2-outside-Compose boundary.

## 19. Expected deliverables

```text
infra/compose/compose.yaml
infra/compose/images.lock.yaml
infra/compose/config/temporal/dynamicconfig.yaml
infra/compose/scripts/postgres-provision.sh
infra/compose/scripts/temporal-schema-setup.sh
infra/compose/scripts/temporal-namespace-bootstrap.sh
infra/compose/scripts/temporal-secret-entrypoint.sh
infra/compose/README.md
tools/local_service_stack.py
tests/unit/tools/test_local_service_stack.py
tests/contract/test_local_service_stack_contract.py
tests/integration/test_local_service_stack.py
.github/workflows/local-service-stack.yml
```

The implementation also updates `.gitignore`, `pyproject.toml` Poe tasks and the root README. It does not modify Python runtime settings with database/service/R2 credentials; settings are introduced only by their owning adapter specs.

## 20. Primary references

- [PostgreSQL 18.4 release](https://www.postgresql.org/docs/release/18.4/)
- [PostgreSQL official image: PostgreSQL 18 data-volume change and `_FILE` secrets](https://hub.docker.com/_/postgres)
- [Qdrant 1.18.2 release](https://github.com/qdrant/qdrant/releases/tag/v1.18.2)
- [Qdrant security and API-key configuration](https://qdrant.tech/documentation/security/)
- [Qdrant health endpoints](https://qdrant.tech/documentation/ops-monitoring/monitoring/)
- [Neo4j 5.26.28 release notes](https://neo4j.com/release-notes/database/)
- [Neo4j Docker secrets](https://neo4j.com/docs/operations-manual/current/docker/docker-compose-standalone/)
- [Temporal Server 1.31.2 release](https://github.com/temporalio/temporal/releases/tag/v1.31.2)
- [Temporal current PostgreSQL Compose sample](https://github.com/temporalio/samples-server/blob/main/compose/docker-compose-postgres.yml)
- [Temporal UI 2.53.0 release](https://github.com/temporalio/ui/releases/tag/v2.53.0)
- [Temporal CLI 1.8.0 release](https://github.com/temporalio/cli/releases/tag/v1.8.0)
- [Cloudflare R2 S3 credentials](https://developers.cloudflare.com/r2/get-started/s3/)
- [Cloudflare R2 consistency model](https://developers.cloudflare.com/r2/reference/consistency/)
- [Docker Compose trust model](https://docs.docker.com/compose/trust-model/)
