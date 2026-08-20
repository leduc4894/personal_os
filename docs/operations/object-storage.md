# Object Storage Operations Guide

Operator contract for the content-addressable Cloudflare R2 object storage.
The provider package (`packages/r2-object-storage`) owns the only code that
contacts R2; the canonical bytes it stores are immutable and content-addressed,
and PostgreSQL holds the canonical state that references them.

## Runtime check boundaries

- **Startup validation is offline.** Process startup validates settings, secret
  files and spool safety **without calling R2**. A misconfigured process fails
  fast locally; a reachable-but-unhealthy R2 never blocks a process from
  starting.
- **Liveness never calls R2.** Liveness probes must stay purely local.
- **Normal readiness does not call R2.** Readiness must not perform a network
  call that would cause an orchestrator to restart healthy processes during an
  R2 incident.
- **Only the explicit runtime check performs HeadBucket.** The one-shot
  `object-storage-check-runtime` command is the single `HeadBucket` probe in
  the entire system, and it is strictly read-only: it never writes, lists or
  deletes an object (there is no delete, list, copy, public-URL or presigned
  operation anywhere in the production port or adapter).

## The runtime check command

```bash
# direct console command (any of the three existing services):
uv run --package r2-object-storage object-storage-check-runtime --service api
uv run --package r2-object-storage object-storage-check-runtime --service mcp
uv run --package r2-object-storage object-storage-check-runtime --service worker

# repository-root Poe task, bound to the worker service:
uv run poe object-storage-check-runtime        # --service worker
```

The command parses `--service` (required, one of `api`, `mcp`, `worker`) before
reading any environment variable or secret file, runs the startup spool janitor,
performs one bounded read-only `HeadBucket` probe, emits the safe JSON
diagnostic event(s) and closes the R2 client exactly once. It never renders
settings values, secret paths or exception causes.

Janitor degradation is a warning, never a probe skip and never an exit-code
change: a failed cleanup run emits one `object_storage_spool_cleanup_degraded`
event carrying only safe counts, the `HeadBucket` probe still runs, and the
exit code reflects only the probe outcome. Stale candidates the janitor could
not handle are picked up by a later run. Every completed probe emits exactly
one probe-outcome event — `object_storage_operation_succeeded` on success or
`object_storage_operation_failed` on dependency/access failure; a degraded
janitor adds one separate cleanup warning event.

| Exit | Meaning |
| --- | --- |
| `0` | Success — the read-only probe completed. |
| `2` | CLI syntax error (missing/unknown `--service`), decided before any environment or secret read. |
| `69` | Dependency/access unavailable — access denied, or unavailable after the bounded retry. |
| `70` | Unexpected internal error. |
| `78` | Configuration or secret error. |

Retry behavior: `access_denied` is terminal (no retry); transient unavailability
retries a bounded three attempts inside the command, then exits `69`.

## Configuration

The object-storage fragment reads exactly these seven approved environment
names; any other `KNOWLEDGE_*` key is a terminal configuration error:

| Variable | Meaning |
| --- | --- |
| `KNOWLEDGE_ENVIRONMENT` | `local`, `test`, `staging`, `production` |
| `KNOWLEDGE_SECRET_ROOT` | absolute secret root (production default `/run/secrets`) |
| `KNOWLEDGE_R2_ENDPOINT` | canonical `https://<account-id>.r2.cloudflarestorage.com` |
| `KNOWLEDGE_R2_BUCKET_NAME` | lowercase R2-compatible bucket name |
| `KNOWLEDGE_R2_ACCESS_KEY_ID_FILE` | access-key-id secret file name beneath the secret root |
| `KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE` | secret-access-key secret file name beneath the secret root |
| `KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT` | absolute existing spool directory |

### Credentials

R2 credentials are **secret-file-only**: the two `_FILE` variables name bounded
regular files beneath `KNOWLEDGE_SECRET_ROOT`; plaintext secret environment
variables, `.env` files, shared AWS credentials files and ambient AWS credential
discovery are prohibited and ineffective. Explicit credentials disable any
ambient chain; TLS verification cannot be disabled. Each R2 token has Object
Read & Write permission scoped only to its exact bucket, and **credential
rotation takes effect on process restart** — rotated secret files are picked up
by the next process, never by a running one.

### Bucket isolation

- Production and test/CI use **different private buckets and different
  credentials**; production and test buckets and credentials **never cross**.
- Each bucket is a **private bucket**: public development URLs and custom
  public domains are disabled.
- Canonical objects have no automatic expiration rule.
- Production credentials never enter CI; test credentials never enter
  production.

## Spool storage

The adapter spools streaming bytes into private local files under
`KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT` while hashing them (mode `0600`, exclusive
create, bounded size/admission limits). Spool storage must sit on **encrypted or
ephemeral** storage: spool files hold object bytes that have not yet been
verified and published, so the underlying volume must be encrypted at rest or
ephemeral (tmpfs/scratch that never survives the host). The startup janitor
removes stale spool files (older than 24 hours) before the runtime-check probe
and reports counts only.

## Failure posture

An R2 outage degrades operations that require canonical bytes; it never
triggers provider switching (there is **no fallback** provider), Worker routing
changes or process restart loops, and it does not prevent unrelated local
diagnostics from running. Reads fail closed: every stored-object read verifies
size, digest and media type before a single byte reaches a consumer.

## Acceptance status (2026-08-14)

- Offline gates on the implementation commit: **green.** `uv run poe verify`
  (format, lint, strict typing, import boundaries, Python/TypeScript tests,
  builds), the focused object-storage acceptance suites, the live-module
  collection check, `lint-imports` and `git diff --check` all passed.
- Live gate: **green.** The protected `object-storage-live` workflow
  (`.github/workflows/object-storage-live.yml`) passed all nine live cases
  (the full design 16.2 set, including repeated/lost-response-equivalent
  resolution) against the dedicated private test bucket on `master` at commit
  `22dccca` — run
  [31791535221](https://github.com/leduc4894/personal_os/actions/runs/31791535221),
  2026-08-14. The workflow re-runs on every push to `master`, daily at 02:23 UTC
  and on manual dispatch.
- Live activation history: the first dispatch attempts surfaced configuration
  defects outside the adapter — a workflow file bug (`runner.temp` used at job
  level), a missing repository secret and a malformed secret value — each
  caught fail-closed by the harness or the credential-shape guard before any
  test claimed a pass. Local developer runs use the same contract via
  `uv run poe object-storage-test-live` with the three `R2_TEST_*` variables
  and the two mode-0600 credential files.
- Phase 1 object-storage activation: **live gate satisfied** on the recorded
  commit. Production activation remains a deliberate deployment decision
  (secret files, spool volume and the runtime check), not a test status.
