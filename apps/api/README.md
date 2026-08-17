# `apps/api` — API process shell

This package is the composition root for the `personal-api` process. It is a
`uv` workspace member that depends on the root `knowledge-core` distribution
and exposes the `personal-api` console-script entry point
(`api_runtime.command:main`).

**Composition role:** API process shell and the runnable HTTP contract spine.
The shell-only paths (`--help`, `--version`, no argument and any invalid
syntax) parse arguments and exit without reading any environment variable,
secret file or network resource, and without importing any framework SDK.
`serve` lazy-imports FastAPI/Uvicorn and runs the application;
`export-openapi` renders the deterministic OpenAPI 3.1 document offline. The
`check-runtime` subcommand loads the approved runtime configuration described
in the root README.

## API runtime contract

- `personal-api serve` binds Uvicorn single-process (loopback defaults in
  local/test; `KNOWLEDGE_API_HOST`/`KNOWLEDGE_API_PORT` required explicitly in
  staging/production) and serves `GET /api/health/live` (I/O-free liveness)
  and `GET /api/health/ready` (one bounded PostgreSQL connectivity and
  exact-schema-head probe, two-second deadline, no retry). Uvicorn's access
  log is disabled entirely — request-level observations come only from the
  structured diagnostics events — and low-level Uvicorn-internal startup
  failures (for example bind errors) surface as the safe exit code `70`.
- Every application/health response uses the strict envelope
  `{request_id, data, warnings, error}` with a server-owned UUIDv7 request ID
  returned in body and `X-Request-ID`; framework errors map through a closed
  code-to-status table. Local/test additionally serve the raw
  `/api/openapi.json`; production OpenAPI is disabled along with Swagger UI
  and ReDoc.
- The full operator contract (startup/shutdown, readiness and schema-drift
  diagnosis, OpenAPI governance) lives in
  [`docs/operations/api-runtime-contract.md`](../../docs/operations/api-runtime-contract.md).

## Build and test

This package is built and tested as part of the root workspace; it has no
standalone build command.

```bash
uv run poe build          # uv build --all-packages (builds this member wheel)
uv run poe test           # pytest exercises --help/--version/no-arg/invalid-arg
uv run --package api-runtime personal-api --help
```

## `check-runtime` health check

```bash
personal-api check-runtime
```

The command loads and validates the runtime configuration snapshot and emits
exactly one safe JSON object (one JSON object per line). It never performs a
settings dump and never emits secret values, file paths, environment variables
or exception text. Stream routing, correlation fields, the approved
`KNOWLEDGE_*` variables and the full operator contract are defined in the root
README (*Runtime configuration & diagnostics*); this section lists only the
exit codes:

| Exit | Meaning |
| --- | --- |
| `0` | Success — runtime configuration validated. |
| `2` | CLI syntax error. |
| `70` | Unexpected internal error. |
| `78` | Configuration or secret error. |

## `policy-key` signing-key lifecycle

```bash
personal-api policy-key initialize --workspace-id <uuid> --key-file-name policy_signing_a.pem
personal-api policy-key stage      --workspace-id <uuid> --key-file-name policy_signing_b.pem
personal-api policy-key activate   --workspace-id <uuid> --staged-key-file-name policy_signing_b.pem
personal-api policy-key retire     --workspace-id <uuid> --key-id <ed25519-sha256-…>
```

These are the offline operator commands of the exclusion-policy signing-key
rotation (spec sections 13.2/13.3). `initialize` generates or imports one
Ed25519 key into a newly created exact file beneath `KNOWLEDGE_SECRET_ROOT`
(owner-only permissions, never overwriting existing bytes) and publishes the
self-signed keyset revision 1 with that key as the one current key. `stage`
publishes a cross-signed revision adding the new key as staged beside the old
current key (old-current signature plus proof-of-possession from the new
key); `activate` publishes the cross-signed revision making the staged key
current — the API signer configuration
(`KNOWLEDGE_POLICY_SIGNING_KEY_ID`/`KNOWLEDGE_POLICY_SIGNING_KEY_FILE`)
switches only after it commits; `retire` publishes the retirement revision
for the old key after the operating overlap, signed by the current key alone.
Every revision appends the immutable keyset envelope, the public signing-key
rows it introduces and its `exclusion_policy.key_*` audit row in exactly one
transaction; replayed invocations acknowledge the already committed
transition without appending rows.

Key material stays inside the secret-file boundary: private keys are never
arguments, database rows, settings values or logs, and the commands print
only the closed status line — action, public key ID, keyset revision and the
replay flag. `serve` loads the configured signer through the same boundary
and refuses to bind its socket unless the derived key ID equals the current
key of the latest canonical keyset of every initialized workspace. Exit codes
follow the table above with `2` additionally covering operator-input
validation and `78` typed lifecycle rejections.

## Intentionally absent behavior

The following are deliberately absent and belong to later child specs:

- authentication, sessions, device authorization and workspace resolution
  middleware;
- source, sync, upload, download, event, reconciliation or conflict routes;
- business tables, Alembic migrations and non-PostgreSQL readiness
  dependencies (R2, Temporal, Redis, Qdrant, Neo4j);
- object storage clients and provider SDKs in the API process.

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
