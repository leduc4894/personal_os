# `apps/mcp` — MCP process shell

This package is the composition root for the `personal-mcp` process. It is a
`uv` workspace member that depends on the root `knowledge-core` distribution
and exposes the `personal-mcp` console-script entry point
(`mcp_runtime.command:main`).

**Composition role:** MCP process shell. The shell parses `--help`,
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
uv run --package mcp-runtime personal-mcp --version
```

## Intentionally absent behavior

The following are deliberately absent and belong to later specs:

- MCP server lifecycle, transport bindings and the MCP tool registry;
- any **MCP tool**, resource, prompt or capability advertisement;
- request/trace ID generation and structured logging;
- database clients, object storage clients and provider SDKs;
- authentication and dependency health checks.

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
