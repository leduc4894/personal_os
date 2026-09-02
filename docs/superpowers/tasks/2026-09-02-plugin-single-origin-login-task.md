# Plugin Single-Origin Login Tasks

## Deliverables

1. Plugin native grant creation does not forge a distinct direct-API Origin;
   it opens only the server-minted browser approval URL.
2. API admits a missing-Origin native grant request while retaining exact
   rejection for a present, unconfigured browser Origin.
3. Plugin settings, live bootstrap, tunnel readiness, WDIO defaults and
   operator documents use one public workspace origin for Web and `/api/*`.
4. The affected BACKLOG row is removed only after focused automation, a
   sanitized Desktop check and the final repository gate pass.

## Order and Completion Gates

| Order | Deliverable | Required evidence |
|---|---|---|
| 1 | Plugin boundary | Focused Vitest tests and plugin type check pass. |
| 2 | Native API grant dependency | Device-authorization/API contract tests and OpenAPI checks pass. |
| 3 | Tooling and docs | Settings/bootstrap contract tests and launcher help pass. |
| 4 | Closure | Disposable-stack Desktop evidence is sanitized, `uv run poe verify` passes, and one handoff records the final SHA. |

## Out of Scope

- Supporting a direct API hostname as a plugin login origin.
- Adding a second plugin URL setting or build-time environment configuration.
- Changing device-authorization response schemas, credentials, polling, or
  browser session/CSRF behavior.
