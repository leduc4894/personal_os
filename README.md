# Knowledge Workspace

Reproducible Python and TypeScript monorepo for the personal knowledge system.
Phase One establishes the locked toolchains, strict quality gates and thin
composition-root shells for five deployable application boundaries. No product
behavior from later specs is implemented here.

## Operator prerequisites

Install these exact versions before cloning. CI verifies the same versions on
every push and pull request; a mismatched major, minor or patch fails the build.

| Tool | Pinned version |
| --- | --- |
| Python | `Python 3.14.6` |
| uv | `uv 0.11.32` |
| Node.js | `Node.js 24.18.0` |
| pnpm | `pnpm 10.34.0` |

These are the exact language and package-manager prerequisites. Internal
development dependencies (formatters, linters, type checkers, test runners)
are pinned in `pyproject.toml`, `package.json` and the committed lockfiles; they
are not installed manually by operators.

The local service stack additionally requires a Linux `linux/amd64` Docker
Engine and Docker Compose 2.30.0 (the reviewed CI pin; the lifecycle tool
accepts Compose `>=2.30.0`). On Windows, use Docker Desktop with Linux containers;
Windows containers are not supported by this stack.

## Fresh install (frozen lockfiles)

From the repository root, after cloning:

```bash
uv sync --all-packages --frozen
pnpm install --frozen-lockfile
```

Both commands are reproducible: they consume `uv.lock` and `pnpm-lock.yaml`
verbatim and never rewrite them. Re-running either command on a clean clone
must leave both lockfiles byte-for-byte unchanged:

```bash
git diff --exit-code -- uv.lock pnpm-lock.yaml
```

## Local service stack

The authenticated Docker Compose stack provides PostgreSQL, Qdrant, Neo4j,
Redis and Temporal for local development. It publishes eight endpoints only on
`127.0.0.1`, keeps generated credentials outside Git and does not configure or
contact Cloudflare R2. Detailed ports, service versions, failure recovery and
safe reset rules live in [`infra/compose/README.md`](infra/compose/README.md).

Bootstrap once, then use the normal lifecycle commands from the repository
root:

```bash
uv run poe stack-bootstrap  # create or validate the atomic local credential set
uv run poe stack-config     # validate Compose without contacting the Docker Engine
uv run poe stack-up         # start, wait for health, then verify authenticated readiness
uv run poe stack-status     # return sanitized project state
uv run poe stack-verify     # repeat all semantic readiness probes
uv run poe stack-down       # remove containers/network; preserve volumes and credentials
uv run poe stack-reset      # destructive; requires exact project confirmation arguments
uv run poe stack-smoke      # CI-only disposable persistence/recovery contract
```

`stack-reset` and `stack-smoke` need the explicit arguments documented in the
infrastructure runbook. Do not invoke Docker Compose directly: doing so skips
port, image-lock, credential-state and project-identity validation.

## Quality commands

Eight public Poe the Poet gates are exposed. They run identically on Ubuntu and
Windows; Poe only orchestrates the underlying `uv` and `pnpm` tooling and
contains no business logic.

```bash
uv run poe format          # apply Python (ruff) and TypeScript (eslint) formatting
uv run poe format-check    # verify formatting without writing changes
uv run poe lint            # ruff + eslint, warnings are fatal
uv run poe type-check      # mypy strict + tsc strict
uv run poe test            # pytest with diagnostic coverage + member vitest runs
uv run poe boundary-check  # import-linter + architecture contract tests
uv run poe build           # uv build --all-packages + pnpm recursive build
uv run poe verify          # the full pipeline, in canonical order (see below)
```

### `poe verify` order

`uv run poe verify` runs the six public gates in this exact order. A failure in
any gate stops the pipeline and surfaces the failing stage.

```text
format-check → lint → type-check → boundary-check → test → build
```

## Composition-root CLIs

After a frozen install, the three Python process shells respond to the
documented CLI contracts (`--help` exits 0, `--version` reports the shared
distribution version, no argument prints concise help, an invalid argument
exits 2 without a stack trace).

```bash
uv run --package api-runtime personal-api --help
uv run --package mcp-runtime personal-mcp --version
uv run --package workflow-worker personal-worker
```

## Runtime configuration & diagnostics

Each Python process shell exposes a `check-runtime` subcommand that loads and
validates the runtime configuration snapshot and emits exactly one structured
diagnostic line. It is the operator-facing health check for the composition
root.

### Approved environment variables

The runtime reads exactly three environment variables. Any other `KNOWLEDGE_`
prefixed key is rejected as a configuration error.

| Variable | Default | Allowed values |
| --- | --- | --- |
| `KNOWLEDGE_ENVIRONMENT` | `local` | `local`, `test`, `staging`, `production` |
| `KNOWLEDGE_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `KNOWLEDGE_SECRET_ROOT` | `/run/secrets` | absolute path |

`KNOWLEDGE_SECRET_ROOT` defaults to the production POSIX secret root
`/run/secrets`. That path is not absolute on Windows, so Windows and local-test
runs must set `KNOWLEDGE_SECRET_ROOT` to an explicit absolute directory.

### Secret files

Secrets are file-only and load only from beneath `KNOWLEDGE_SECRET_ROOT`. Each
secret file must be a regular file bounded beneath the root, at most 64 KiB,
UTF-8 encoded with no byte-order mark, with at most one optional trailing
newline and a non-empty value. Plaintext secret environment variables, `.env`
files, TOML/YAML/JSON settings files and command-line secret values are not
supported and must not be used.

### Structured diagnostics

Every process emits one JSON object per line. Records at `debug`, `info` and
`warning` route to stdout; `error` and `critical` route to stderr. Each record
carries a server-generated `request_id` (UUIDv7) and `trace_id` (32-character
lowercase hexadecimal W3C trace id). Raw content, queries, vectors, tokens,
file paths, exception text and settings dumps are never emitted.

### Commands and exit codes

```bash
personal-api check-runtime
personal-mcp check-runtime
personal-worker check-runtime
```

| Exit | Meaning |
| --- | --- |
| `0` | Success — runtime configuration validated. |
| `2` | CLI syntax error. |
| `70` | Unexpected internal error. |
| `78` | Configuration or secret error. |

## Build outputs

| Artifact | Location |
| --- | --- |
| Web App production build | `apps/web/.next/` (produced by `next build`) |
| Obsidian plugin loader artifacts | `apps/obsidian-plugin/dist/` (`main.js` + `manifest.json`) |
| Python wheels and sdists | `dist/` (produced by `uv build --all-packages`) |

Web App and Obsidian plugin build outputs are gitignored; they are rebuilt from
source on every install. The Obsidian plugin ships exactly two artifacts:
`main.js` and `manifest.json`.

## Intentionally out of scope

Runtime configuration loading, secret-file loading and structured diagnostics
are provided by this workspace (see *Runtime configuration & diagnostics*
above). The following remain deliberately absent and belong to later specs:

- application database schemas/adapters and Cloudflare R2 object storage behavior;
- provider SDKs, concrete provider credentials and remote secret managers;
- framework adapters and HTTP/MCP/Temporal request/trace propagation, plus
  OpenTelemetry exporters and log-shipping deployment;
- API, MCP or workflow behavior (no FastAPI routes, MCP tools, or Temporal
  workflows/activities);
- product UI beyond the static bootstrap page, including the Obsidian plugin's
  product commands.

Each of these concerns belongs to a later spec and is documented as absent in
the relevant `apps/*/README.md`.

## Repository layout

```text
pyproject.toml            # root knowledge-core distribution + Poe task graph
uv.lock                   # frozen Python workspace
package.json              # pnpm workspace root
pnpm-workspace.yaml       # web + obsidian-plugin members
pnpm-lock.yaml            # frozen TypeScript workspace
src/personal_os/          # shared Python distribution (no product behavior)
apps/api/                 # personal-api composition shell
apps/mcp/                 # personal-mcp composition shell
apps/worker/              # personal-worker composition shell
apps/web/                 # Next.js Web App shell
apps/obsidian-plugin/     # Obsidian plugin shell
tests/                    # canonical test hierarchy (unit, contract, reserved layers)
infra/compose/            # authenticated local dependency stack and operator runbook
.github/workflows/quality.yml   # Ubuntu + Windows quality matrix
.github/workflows/local-service-stack.yml  # config portability + Ubuntu Docker smoke
```
