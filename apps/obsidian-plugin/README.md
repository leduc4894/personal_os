# `apps/obsidian-plugin` — Obsidian plugin shell

This package is the Obsidian plugin composition root. It is a `pnpm` workspace
member (`@workspace/obsidian-plugin`) and is independent of the Python
workspace and of `apps/web`: it imports nothing from either.

**Composition role:** Obsidian plugin shell. It compiles, via esbuild, into
the minimum artifacts the Obsidian loader requires. `onload` and `onunload`
have no product side effects.

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
- product UI and configuration persistence.

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
