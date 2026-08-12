# `apps/api` — API process shell

This package is the composition root for the `personal-api` process. It is a
`uv` workspace member that depends on the root `knowledge-core` distribution
and exposes the `personal-api` console-script entry point
(`api_runtime.command:main`).

**Composition role:** API process shell. The shell parses `--help`,
`--version`, no-argument and invalid-argument input and delegates bootstrap
behavior to the shared `personal_os.command_shell` helper. It imports no
framework SDK and reads no environment variable, secret file or network
resource.

## Build and test

This package is built and tested as part of the root workspace; it has no
standalone build command.

```bash
uv run poe build          # uv build --all-packages (builds this member wheel)
uv run poe test           # pytest exercises --help/--version/no-arg/invalid-arg
uv run --package api-runtime personal-api --help
```

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
