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
change. A cleanup scan failure emits one
`object_storage_spool_cleanup_degraded` event with safe cleanup counts and the
closed reason `spool_cleanup_scan_failed`. Stale candidates the janitor could
not handle are picked up by a later run; an individual stale-entry failure
uses `spool_cleanup_entry_failed` as its closed reason. The `HeadBucket` probe
still runs and its result remains authoritative: emit exactly one
`object_storage_operation_succeeded` or `object_storage_operation_failed`
probe-outcome event, and use that outcome alone for the exit code.

Client close is a separate lifecycle diagnostic. If closing the client
degrades, emit `object_storage_client_close_degraded` with the closed client
close reason `object_storage_client_close_failed` after the probe outcome.
This event never replaces the probe
event and never changes its already-determined exit code. Thus the probe
outcome has precedence for command status; cleanup and close diagnostics are
additional safe events only.

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

## Hosted proof of the sanitized JUnit harness (2026-08-27)

The protected workflow's sanitized-JUnit path has now been exercised and
recorded on the fully hardened harness (the JUnit sanitization chain through
`1ec3a01` and the zero-byte diagnostics hardening through `37d1def`, plus the
`run_nonce` removal at `49ba212`):

- Workflow run
  [33069347334](https://github.com/leduc4894/personal_os/actions/runs/33069347334)
  ("object storage live R2", job "Ubuntu dedicated-bucket R2 contract"),
  triggered on `master` at commit
  `49ba212a05ae789a377cf3027661d2d8dee9e08e`, completed 2026-08-27 11:54 UTC
  with conclusion **success** — every step green, including the
  unsanitized-staging removal and secret-file removal steps.
- Case count/outcome: **9 passed** (`9 passed in 26.31s`) — the full design
  16.2 live set, zero failures.
- Published artifacts: exactly **one** — the sanitizer upload
  `object-storage-live-junit-33069347334-1`. The downloaded report contains
  only suite/case identity and statuses: nine passed testcases, no
  failure/error nodes, no `system-out`/`system-err`/`properties`, and no case
  text. No credentials, endpoint, bucket name or raw JUnit output was
  recorded anywhere.

Sanitized evidence and the `run_nonce` disposition live in
`docs/handoff/2026-08-27-object-storage-hosted-proof.md`.
