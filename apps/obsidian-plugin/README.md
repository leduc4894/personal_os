# `apps/obsidian-plugin` — Obsidian plugin shell

This package is the Obsidian plugin composition root. It is a `pnpm` workspace
member (`@workspace/obsidian-plugin`) and is independent of the Python
workspace and of `apps/web`: it imports nothing from either.

**Composition role:** Obsidian plugin shell. It compiles, via esbuild, into
the minimum artifacts the Obsidian loader requires (`dist/main.js` plus the
manifest). `src/plugin.ts` only wires real adapters — Obsidian `requestUrl`,
`Platform`, the app `SecretStorage`, plugin data persistence and `window.open`;
every behavior lives in the tested modules under `src/authentication/`,
`src/exclusion-policy/` and `src/journal/`.

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

## Portable journal dependency

The sync journal runs SQLite as WebAssembly through one pinned production
dependency, `sql.js` (journal design section 6):

- **Version:** `1.14.2`, pinned exactly (no range) in `dependencies`.
- **License:** MIT (sql.js authors); the bundled SQLite engine itself is
  public-domain SQLite compiled to WebAssembly.
- **WASM asset and bundling behavior:** the package's `exports` browser
  condition resolves the bundled module to `dist/sql-wasm-browser.js`, which
  esbuild inlines like every other dependency for `platform: "browser"`.
  The engine bytes ship as a separate WebAssembly asset
  (`dist/sql-wasm.wasm`) that must be loaded as bytes — never through a
  Node `fs` read; journal persistence loads and stores everything through
  Obsidian `DataAdapter.readBinary`/`writeBinary`. The bundle-boundary
  contract excises exactly sql.js's esbuild module segment before scanning,
  so the library's inert emscripten Node-detection text never widens the
  gate for plugin-authored code.
- **No native runtime requirement:** SQLite executes as WebAssembly inside
  the same Web engine on Desktop and Mobile. The plugin imports no Node
  built-in, no Electron API, no native SQLite driver (`node:sqlite`,
  `better-sqlite3`, `sqlite3`) and no ORM; the boundary contract rejects
  all of them.

`src/journal/` implements the portable sync journal of the journal design:
`contracts.ts` freezes the closed vocabulary (event states, safe error
labels, queue outcomes, recovery states, logical records and the 16 MiB /
10,000-row / 64 MiB / 250 ms limits), `sqlite-database.ts` runs SQLite as
WebAssembly with journal-scoped sessions, `persistence.ts` publishes every
commit as a digest-verified immutable generation with crash-safe recovery
and the synchronous unload flush probe, `repository.ts` owns the durable
records and the redacted status histogram, `capture.ts` turns settled Vault
observations into journal intent (plus the confirmed `Sync existing files`
scan), `sync-api.ts` is the hand-mirrored small-file API client and
`queue-driver.ts` runs the bounded foreground pass. `status.ts` projects the
closed sync status of the minimal plugin UX (spec 11).

## Minimal plugin UX and operations (spec 11)

The plugin shows a small status-bar item with a pending count and exactly
one of six closed values — `Ready`, `Syncing`, `Offline — queued`,
`Login required`, `Policy blocked`, `Reconcile required` — projected from
the redacted journal histogram, credential existence and the active pass.
The settings tab repeats the same closed status plus the fixed blocker
guidance (the 16 MiB/multipart boundary, the authorized-only policy
refresh, the no-overwrite conflict/lifecycle deferrals, the queue-preserving
browser login, and the child-6 repair of a `reconcile_required` journal).
Exactly two sync commands exist: `Sync now` (one bounded foreground pass of
currently eligible events, never bypassing the one-active-request guarantee
or the bounded retry backoff) and `Sync existing files` (a snapshot scan
that queues nothing until the user confirms). A `reconcile_required`
journal is a hard stop: the status refresh stops the driver and no pass
runs until child 6 repairs the journal.

Instrumentation is limited to the redacted status counts and closed labels
above — no path, digest or credential ever reaches the status or its
telemetry shape, and the UI is never an automatic upload control. On unload
the driver and listeners stop first, a final synchronous flush probe
records whether a generation commit is still in flight (an interrupted
commit recovers from the newest verified generation on the next open), and
the memory-only access credential is cleared.

## Source lifecycle controls (spec 6.3, 7.1)

The Child 5 lifecycle extension adds the closed set of source-lifecycle
states and the third safe-source command on top of the spec-11 sync UX.
The status surface folds the redacted `local_files.lifecycle_state`
histogram, the number of pending lifecycle events, the count of failed
attempts and the closed set of lifecycle blocker codes onto the same
projection — never a path, locator, source ID, token, fingerprint or
remote URL.

Lifecycle states (`active | rename_pending | move_pending |
delete_pending | restore_pending | tombstoned | restored |
reconcile_required`) and the closed blocked reason codes
(`idempotency_conflict`, `version_conflict`, `locator_conflict`,
`tombstone_not_found`, `tombstone_closed`, `commit_outcome_unknown`,
`integrity_failed`) are the only strings the surface exposes.

The narrow command surface owns exactly three commands:

- **`Sync now`** — schedules an immediate bounded foreground pass.
  The trigger funnels through the same `#runBoundedQueuePass` wrapper
  the Vault listeners use, so the one-active-request guarantee and the
  bounded retry backoff (one second to five minutes, jittered) are
  preserved.
- **`Sync existing files`** — a confirmed snapshot scan; queues
  nothing before the user confirms.
- **`Restore selected tombstone`** — the explicit user-driven restore.
  The user picks a retained tombstone by its safe plugin-local id (the
  path is never displayed), supplies a target Vault path, confirms, and
  the lifecycle capture verifies the bytes hash against the file's
  last-committed fingerprint before recording the restore event. A
  hash mismatch or a missing retained mapping is rejected with the
  closed `journal_mutation_failed` `JournalStoreErrorReason`; the Sync
  status is refreshed on both branches so the redacted surface always
  reflects the new lifecycle state.

Automatic restore is permitted ONLY when the capture detects a
tombstoned path re-appearing with bytes that hash to the last-committed
fingerprint — never from path reuse alone. The full operator playbook
(state transitions, reconcile handling, exact replay, deletion
semantics, redacted diagnostics) lives at
[`docs/operations/source-locator-tombstone-lifecycle.md`](../../docs/operations/source-locator-tombstone-lifecycle.md);
live launcher / stack secrets stay at [`.local/RESTART.md`](../../.local/RESTART.md).

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

The following are deliberately absent and belong to later children of the
journal design:

- cursor pull, remote apply, offline registration and the repair of a
  `reconcile_required` journal (child 6);
- multipart/resumable uploads and files above 16 MiB (child 7);
- candidate preservation, Conflict Inbox, merge and visible conflict
  resolution (child 8);
- any view, markdown post-processor, workspace hook or ribbon icon;
- any background sync daemon, timer or automatic retry loop — queue work
  is foreground and bounded, triggered only by plugin load, a Vault event
  or `Sync now`;
- any automatic full-Vault upload: no control implies one, and the
  snapshot scan queues nothing before an explicit confirmation;
- any listing of paths or locators on the status surface — the
  redacted telemetry carries only the closed enum states, the closed
  blocked reason codes and counts.

No placeholder implementation of the above is provided. Each concern is added
by a separate, reviewed spec.
