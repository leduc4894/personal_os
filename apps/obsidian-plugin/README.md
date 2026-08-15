# `apps/obsidian-plugin` — Obsidian plugin shell

This package is the Obsidian plugin composition root. It is a `pnpm` workspace
member (`@workspace/obsidian-plugin`) and is independent of the Python
workspace and of `apps/web`: it imports nothing from either.

**Composition role:** Obsidian plugin shell. It compiles, via esbuild, into
the minimum artifacts the Obsidian loader requires. `onload` and `onunload`
have no product side effects.

## API transport

The plugin compiles against the shared generated client
`@workspace/api-client` (alone among workspace members — ESLint rejects every
other `@workspace/*` import, including Web). `src/api/request-url-transport.ts`
adapts the standard transport to Obsidian `requestUrl` (importing Obsidian
types only), preserving method, URL, headers, body, response status and bytes
and never logging request or response content; in-flight requests cannot be
cancelled by `requestUrl`, so later feature code must bound concurrency and
discard late results. The runtime binding
`src/api/obsidian-api-transport.ts` is the only layer importing the
`obsidian` module. The full API operator contract lives in
`docs/operations/api-runtime-contract.md`.

## Build and test

This member is built and tested through the root pnpm scripts, which the
Poe task graph invokes.

```bash
uv run poe build          # pnpm --recursive run build (esbuild → dist/)
uv run poe test           # pnpm --recursive run test (vitest run --coverage)
pnpm --filter @workspace/obsidian-plugin type-check
```

## Build output

The plugin distribution is exactly two files in `apps/obsidian-plugin/dist/`
(gitignored, rebuilt on every install):

- `main.js` — the esbuild production bundle;
- `manifest.json` — the Obsidian plugin manifest.

No source maps, test fixtures or secrets are emitted.

## Intentionally absent behavior

The following are deliberately absent and belong to later specs:

- any Obsidian command, ribbon icon, status bar item or settings tab;
- any event listener, view, markdown post-processor or workspace hook;
- **Vault** access, file system reads/writes and metadata indexing;
- authentication, onboarding and the secure token store (later Phase 2
  children own them);
- product UI and configuration persistence.

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
