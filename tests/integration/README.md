# Local service stack integration

**Status:** Two executable disposable-stack tests plus the dedicated live R2
harness are owned here.

## Owners

The local service stack spec owns only `test_local_service_stack.py`. It proves
authenticated startup, persistence across `down`/`up`, repeated initializer
idempotence, Redis outage detection and recovery, and exact final cleanup.

The canonical PostgreSQL baseline spec owns only
`test_canonical_postgresql_baseline.py`. It proves the real Alembic
empty-to-head upgrade on disposable PostgreSQL 18.4, the normalized catalog
fingerprint, a valid row graph across all nine baseline tables, the
allowed-behavior cases, ownership/grants/data-minimization, and the full
`upgrade -> downgrade -> re-upgrade` lifecycle with an identical catalog
fingerprint. The disposable project is reset in `finally` and the project label
inventory is asserted empty.

The content-addressable object-storage design owns
`r2_object_storage/`: the offline exact-key cleanup contract in
`test_live_cleanup_manifest.py` and the `r2_live`-marked live adapter cases in
`test_live_r2_adapter.py`.

## Future acceptance source

All other future integration layers remain reserved for their owning specs.
They must add executable behavior tests only when the corresponding production
contract exists.

## Runtime boundary

- The tests require a reachable Linux `amd64` Docker Engine.
- `CI=true` and an exact disposable `knowledge-ci-*` project name are required.
- Each project is reset in `finally`; no local or production project is accepted.
- Cloudflare R2 and provider credentials are outside these tests' scope.

## Credential scope

The canonical PostgreSQL baseline test may access ONLY generated local
PostgreSQL credentials beneath the worktree's ignored `.local/stack-secrets/`
directory. It connects with Psycopg keyword arguments and never renders a
plaintext password, DSN, `DATABASE_URL` or `.env` value. It must never read,
render or depend on Cloudflare R2 or any provider credentials.

Placeholder tests, zero-assertion fixtures and unrelated cross-module coverage
remain forbidden.

## Live R2 object-storage harness (`r2_live`)

The live R2 harness exercises the real `R2S3ObjectStore` against ONE dedicated
private TEST bucket. It never runs in the default suite: the default pytest
selection is `not local_stack and not r2_live`, and the dedicated command and
protected workflow select it explicitly with `-m r2_live`.

### Required configuration (names only; values are never rendered)

| Name                  | Kind     | Meaning                                            |
| --------------------- | -------- | -------------------------------------------------- |
| `R2_TEST_ENDPOINT`    | variable | Canonical `https://<account>.r2.cloudflarestorage.com` URL of the dedicated test bucket |
| `R2_TEST_BUCKET_NAME` | variable | Dedicated private TEST bucket (never production)   |
| `R2_TEST_SECRET_ROOT` | variable | Absolute directory holding the two credential files |
| `r2_test_access_key_id` under the secret root | secret file (mode 0600) | Dedicated test access key |
| `r2_test_secret_access_key` under the secret root | secret file (mode 0600) | Dedicated test secret key |

The harness composes these onto the frozen settings loader's exact
`KNOWLEDGE_*` names (secret FILES; there is no plaintext secret environment
value, `.env` or ambient AWS credential path). Production R2 bucket
information and credentials must never be provided to this harness.

### No-skip contract

Invoking the live suite without the required configuration is an ERROR, not a
skip: fixture setup fails with a safe diagnostic listing the missing variable
or secret-file NAMES only. `uv run poe object-storage-test-live` therefore
exits nonzero without local test credentials.

### Activation status (2026-08-14)

The live gate is **green**: the protected `object-storage-live` workflow passed
all nine live cases (the full design 16.2 set) against the dedicated private
test bucket on `master` at commit `22dccca` —
[run 31791535221](https://github.com/leduc4894/personal_os/actions/runs/31791535221),
2026-08-14. A local developer run had passed the same nine cases first via
`uv run poe object-storage-test-live` with the `R2_TEST_*` variables and the
two mode-0600 credential files. No live case is skipped or xfailed.

### Exact-key cleanup contract

CAS test objects keep the production key grammar, so cleanup can never use a
run prefix or wildcard. Each run records an allowlist of every exact canonical
key it created; teardown validates — before any delete call — that the bucket
is the dedicated test bucket and every key is a recorded canonical
`objects/sha256/{2}/{2}/{64 hex}` key, deletes exactly those keys through the
harness-local low-level delete (the only deletion code in the repository,
never exported from `r2_object_storage`), and then proves absence. Cleanup
failure fails the run and reports only shortened digest prefixes.

### How to run

```text
# Protected trusted pipeline (GitHub): schedule, manual dispatch, master push —
# .github/workflows/object-storage-live.yml writes the R2_TEST_* secrets to
# mode-0600 files and runs the suite with a JUnit-only artifact.

# Locally: provide the same files yourself, then
R2_TEST_ENDPOINT=https://<account>.r2.cloudflarestorage.com \
R2_TEST_BUCKET_NAME=<dedicated-test-bucket> \
R2_TEST_SECRET_ROOT=<absolute-dir-with-0600-secret-files> \
uv run poe object-storage-test-live
```
