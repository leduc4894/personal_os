# `apps/web` — Web App shell

This package is the Next.js App Router composition root for the Web App. It is
a `pnpm` workspace member (`@workspace/web-runtime`) and is independent of the
Python workspace: it imports nothing from `apps/obsidian-plugin` or any Python
package.

**Composition role:** Web App shell. Its only page is a static bootstrap page
that identifies the workspace shell. It type-checks under TypeScript strict
mode and produces a production build with no secret, network service or API
endpoint required.

## Build and test

This member is built and tested through the root pnpm scripts, which the
Poe task graph invokes.

```bash
uv run poe build          # pnpm --recursive run build (next build)
uv run poe test           # pnpm --recursive run test (vitest run --coverage)
pnpm --filter @workspace/web-runtime type-check
```

The production build lands in `apps/web/.next/` (gitignored).

## Intentionally absent behavior

The following are deliberately absent and belong to later specs:

- any **API route**, server action, proxy endpoint or route handler;
- authentication, session management and authorization;
- product navigation, layouts beyond the bootstrap shell and product UI;
- data fetching from backend services and dependency health checks;
- Testing Library (no UI behavior exists to test beyond the bootstrap copy).

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
