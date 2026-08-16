# `apps/obsidian-plugin` — Obsidian plugin shell

This package is the Obsidian plugin composition root. It is a `pnpm` workspace
member (`@workspace/obsidian-plugin`) and is independent of the Python
workspace and of `apps/web`: it imports nothing from either.

**Composition role:** Obsidian plugin shell. It compiles, via esbuild, into
the minimum artifacts the Obsidian loader requires (`dist/main.js` plus the
manifest). `src/plugin.ts` only wires real adapters — Obsidian `requestUrl`,
`Platform`, the app `SecretStorage`, plugin data persistence and `window.open`;
every behavior lives in the tested modules under `src/authentication/`.

## API transport

`src/api/request-url-transport.ts` adapts the standard transport to Obsidian
`requestUrl` (importing Obsidian types only), preserving method, URL, headers,
body, response status and bytes and never logging request or response content;
in-flight requests cannot be cancelled by `requestUrl`, so later feature code
must bound concurrency and discard late results. The runtime binding
`src/api/obsidian-api-transport.ts` is the only layer importing the
`obsidian` module for that adapter. The full API operator contract lives in
`docs/operations/api-runtime-contract.md`.

## Device authentication (spec 19)

`src/authentication/` implements browser device authorization and the crash-safe
token session against the generated API schema. Because the generated client
package cannot load inside Obsidian's module graph, `contracts.ts` hand-writes
only the request/response wire shapes the plugin consumes — mirroring
`packages/api-client/src/generated/schema.ts`, which the Python
bundle-boundary contract test keeps honest.

- **`contracts.ts`** — the closed `ConnectionState` set of spec 19, the
  envelope/error mapping transport over an injected HTTP adapter, exact server
  origin validation (HTTPS with no path/query/fragment/credentials; loopback
  HTTP only behind the explicit development-build constant, which production
  pins to `false`), device-name validation and the injected adapter types
  (SecretStorage, clock, UUID, URL opener, delay).
- **`secret-storage-record.ts`** — the one versioned JSON record
  (`knowledge-workspace-device-credential`, lowercase letters/digits/dashes
  only): the pending-grant polling secret, the active refresh record with its
  pending rotation identity, or the credential-free tombstone
  (`{record_version: 1, state: "cleared", cleared_reason}`). Every write is
  verified by reading it back; Obsidian's SecretStorage has no delete, so
  clearing never claims to remove the key.
- **`device-authorization.ts`** — bounded onboarding: create the grant, persist
  the polling secret BEFORE opening `verification_uri_complete`, poll no faster
  than the server interval (adopting every `retry_after_seconds`/slow-down hint
  exactly), tombstone deny/expiry/cancel, preserve records offline, resume
  pending grants before expiry, reopen the browser on demand.
- **`token-session.ts`** — the crash-safe refresh protocol of spec 13.3
  (persist + verify the pending rotation identity before the network call, one
  verified successor write after the response, reuse a stored pending identity
  after a crash), terminal reuse/revocation tombstones, offline preservation,
  and the revoke-first self-disconnect of spec 14.2. The access credential is a
  private in-memory field, never persisted.
- **`settings-tab.ts`** — the one settings tab of spec 19: exact server origin,
  editable device name, closed connection status, Login, Open browser again,
  Cancel pending login, Disconnect.

At startup the plugin performs at most ONE bounded resume-or-refresh action
and never starts a background sync loop. Plugin data holds only non-secret
material: server origin, device name, non-secret client instance UUID,
SecretStorage record name and the non-secret pending-grant fields (grant ID,
user code, verification URI, expiry, poll interval).

The Python bundle-boundary contract
(`tests/contract/api/test_plugin_authentication_bundle.py`) builds the plugin
and fails the gate on any Electron/Node built-in/`FileSystemAdapter` usage,
credential sentinels or source-map leakage in the emitted bundle.

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

- any Obsidian command, ribbon icon, status bar item or event listener;
- any view, markdown post-processor or workspace hook;
- **Vault** access, file system reads/writes and metadata indexing;
- sync and every background job (later children own them);
- product UI beyond the authentication settings tab.

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
