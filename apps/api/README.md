# `apps/api` — API process shell

This package is the composition root for the `personal-api` process. It is a
`uv` workspace member that depends on the root `knowledge-core` distribution
and exposes the `personal-api` console-script entry point
(`api_runtime.command:main`).

**Composition role:** API process shell. The shell-only paths (`--help`,
`--version`, no argument and any invalid syntax) parse arguments and exit
without reading any environment variable, secret file or network resource, and
without importing any framework SDK. The `check-runtime` subcommand loads the
approved runtime configuration described in the root README.

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

## Intentionally absent behavior

The following are deliberately absent and belong to later specs:

- **FastAPI** application, routes, dependency injection and middleware;
- request/trace ID generation and structured logging;
- database clients (PostgreSQL/SQLAlchemy), object storage clients and provider
  SDKs;
- authentication, authorization and dependency health checks;
- generated API clients and OpenAPI schemas.

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
