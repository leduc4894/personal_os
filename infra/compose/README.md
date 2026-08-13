# Local Service Stack Operator Guide

This directory owns the authenticated Docker Compose dependencies used by local
development and the disposable integration smoke. The supported interface is
`tools/local_service_stack.py`, normally through the Poe commands below. Direct
`docker compose` lifecycle calls are unsupported because they bypass project,
port, image-lock and credential-state validation.

## Prerequisites and platform boundary

- Python and uv must match the versions in the root README, followed by
  `uv sync --all-packages --frozen`.
- Docker Compose must be version `2.30.0` or newer. CI checksum-pins `2.30.0`.
- The Docker Engine must run Linux containers on `linux/amd64`.
- Linux hosts use a native Linux Docker Engine. Windows uses Docker Desktop in
  Linux containers mode; Windows containers are unsupported.
- Configuration validation does not contact the Docker Engine. Only Ubuntu CI
  exercises real containers; Windows CI validates the same static model and
  lifecycle security contracts.

## Exact service versions

Compose pins the exact tag and immutable reviewed manifest digest for every
image. The topology has seven long-running services and three one-shot init
jobs.

| Component | Version | Lifecycle role |
| --- | --- | --- |
| PostgreSQL | `18.4` (`18.4-bookworm`) | Canonical application state and Temporal persistence |
| Qdrant | `1.18.2` | Rebuildable search projection |
| Neo4j Community LTS | `5.26.28` | Rebuildable graph projection |
| Redis Open Source | `8.6.4` | Ephemeral cache and coordination |
| Temporal Server | `1.31.2` | Durable workflow server |
| Temporal schema tools | `1.31.2` | One-shot schema initializer |
| Temporal UI | `2.53.0` | Loopback administration UI |
| Temporal CLI | `1.8.0` | Inert operator toolbox and health client |

Every service uses `linux/amd64`. Qdrant, Neo4j and Redis remain projections or
ephemeral state; their local persistence does not make them canonical.

## Loopback ports

All eight publications bind to `127.0.0.1`. Override values must be unique
ASCII decimal ports in `1024..65535`; the lifecycle tool never chooses a
fallback port.

| Override | Default | Purpose |
| --- | ---: | --- |
| `POSTGRES_PORT` | `5432` | PostgreSQL |
| `QDRANT_HTTP_PORT` | `6333` | Qdrant HTTP |
| `QDRANT_GRPC_PORT` | `6334` | Qdrant gRPC |
| `NEO4J_HTTP_PORT` | `7474` | Neo4j Browser/HTTP |
| `NEO4J_BOLT_PORT` | `7687` | Neo4j Bolt |
| `REDIS_PORT` | `6379` | Redis |
| `TEMPORAL_GRPC_PORT` | `7233` | Temporal frontend |
| `TEMPORAL_UI_PORT` | `8080` | Temporal UI |

Set overrides only for the command that needs them. For example:

```bash
POSTGRES_PORT=15432 QDRANT_HTTP_PORT=16333 uv run poe stack-config
```

PowerShell operators set and later remove the corresponding `$env:` values.
Credentials, image references, volume names, service hostnames and security
switches are not environment overrides.

## First bootstrap and normal operation

From the repository root, create or validate the atomic credential set, then
validate the static model:

```bash
uv run poe stack-bootstrap
uv run poe stack-config
```

Generated credentials remain only in the owning `.local/stack-secrets/`
directory, which Git ignores. Bootstrap reuses a complete set byte-for-byte and
never rotates or overwrites it.

Start and inspect the stack with:

```bash
uv run poe stack-up
uv run poe stack-status
uv run poe stack-verify
```

`stack-up` validates prerequisites, ports, credentials, image locks and Compose
syntax before mutation. It then starts the dependency graph, waits within a
finite deadline and performs authenticated semantic verification.

Stop normal local operation with:

```bash
uv run poe stack-down
```

`stack-down` preserves all volumes and secrets; it removes only project
containers and the project network. A later `uv run poe stack-up` re-runs the
idempotent initializers and preserves the selected PostgreSQL, Qdrant, Neo4j
and Redis markers.

The remaining high-level task is CI-only:

```bash
uv run poe stack-smoke
```

The workflow supplies its validated `knowledge-ci-*` project and exact
confirmation arguments. Operators do not use this command against a normal
local project.

## Safe reset and credential rotation

Reset is destructive. Supply the exact project name twice; `--confirm-project`
must equal `--project-name` and the operator must type the exact project name:

```bash
uv run python tools/local_service_stack.py reset --project-name knowledge-local --confirm-project knowledge-local
```

The corresponding high-level task is `uv run poe stack-reset`, but the direct
example above makes the required arguments explicit. Reset first performs a
non-destructive down, then deletes only resources with the exact Compose
project label and the exact five allowed logical volume labels:

- `postgres-data`
- `qdrant-data`
- `neo4j-data`
- `redis-data`
- `temporal-health-tools` (rebuildable and non-canonical)

Reset refuses unknown labeled volumes, an incomplete five-volume set or an
ambiguous label before deleting any volume. It never prunes global Docker
state, removes images or uses a wildcard. Credentials are preserved by
default.

Credential rotation is explicit and is allowed only after every exact project
volume is deleted successfully:

```bash
uv run python tools/local_service_stack.py reset --project-name knowledge-local --confirm-project knowledge-local --rotate-secrets
uv run poe stack-bootstrap
```

If any deletion fails, rotation does not occur. In-place rotation of a
populated stack is unsupported.

## Terminal states and recovery

A partial secret set is terminal. A missing secret set with existing project volumes
is also terminal. Both require operator investigation: do not delete
individual credential files, generate replacements over persisted data or
weaken authentication. Establish which exact project owns the volumes and
choose deliberate recovery or the confirmed reset path.

Initialization is rerunnable but fail-closed:

- PostgreSQL role and database provisioning reconciles only safe ownership and
  connection state; it does not create application schemas.
- Temporal schema setup creates an absent schema and applies supported forward
  updates. A schema that is ahead, corrupt or incompatible is terminal drift.
- Temporal namespace bootstrap reuses `knowledge` only with the exact seven-day
  retention. Any other retention is namespace drift and fails.
- Init, schema or namespace drift never triggers automatic deletion, fallback
  credentials or object-store behavior. Keep the safe JSON result code for
  operator investigation; inspect vendor state without copying credentials
  into tickets or logs.

After a dependency outage, restore that exact dependency and run
`uv run poe stack-verify`. A readiness failure does not authorize reset.

## Lifecycle exit codes

Lifecycle commands emit one sanitized JSON result and use this closed exit-code
set:

| Exit | Meaning |
| ---: | --- |
| `0` | Success: the requested lifecycle operation completed. |
| `2` | CLI syntax or argument error; `status` also uses it for an absent stack. |
| `64` | Docker/Compose prerequisite, invalid port or port collision. |
| `65` | Secret, configuration, image-lock or semantic contract failure. |
| `69` | Container startup or initialization job failure. |
| `70` | Unexpected internal lifecycle-tool failure. |
| `75` | Semantic readiness failure or timeout. |

Output never includes credential values, credential filenames, absolute
credential paths, rendered Compose configuration, raw vendor errors or
credential-bearing URLs.

## Cloudflare R2 boundary

Cloudflare R2 is the sole future canonical object store, but it is outside this
local-service stack. R2 is not configured, not contacted and not tested by any
command or CI job described here. The later object-storage adapter owns its
credentials, test bucket, live compatibility tests and exact-prefix cleanup.
