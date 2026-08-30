# Child Nine and Phase Two Closure Hygiene Retirement Design

## Purpose and scope

Give every backlog row gated **Before Child 9 operations/recovery acceptance**
or **Before Phase 2 closure (after Child 9)** a terminal disposition, following
the Backlog Retirement Program (`2026-08-20-backlog-retirement-program-design.md`,
program gates 3 and 5). The scope is exactly the twenty-two indexed rows on
2026-08-30, grouped by owning handoff:

**Landing checkpoint 1 — before Child 9 acceptance runs (15 rows):**

1. 2026-08-14 source-publication: dispatcher polish (handoff §9).
2. 2026-08-15 canonical-core: bundle-store minors (§6); snapshot-adapter
   precision (§8); bounded-memory/event-loop hygiene (§9); integration-harness
   hygiene (§12); live-harness type precision (§13); CLI admission-refusal
   label (§10); CLI composition hygiene (§11).
3. 2026-08-16 web-auth: lockout audit transition (§4); reset CLI edges (§5);
   throttle-bucket first-insert race (§7); grant-path hardening batch (§8);
   web auth-state hygiene batch (§10); web a11y/UX batch (§11); plugin hygiene
   batch (§12).

**Landing checkpoint 2 — before the final Phase 2 handoff (7 rows):**

4. 2026-08-14 object-storage: `_run_shielded` cancellation edge (retirement
   ruling 2); test-hygiene batch (handoff §11).
5. 2026-08-14 source-publication: test-hardening batch (§6); fingerprint
   fixture provenance + conditional hex64 extraction (§7).
6. 2026-08-15 canonical-core: lookup-statement filter test name (§4);
   acceptance polish (§14).
7. 2026-08-16 authentication-acceptance-tests: acceptance-test polish batch
   (§14).

Both checkpoints may land in one effort before Child 9 starts; the split only
permits checkpoint-2 rows to trail Child 9. Rows gated by conditional triggers
(TypeScript pin bump, key rotation, multi-worker serve, next-X-change rows),
production activation, live smoke rounds, the physical Mobile matrices, the
exclusion-policy mutation-testing standing deferral (spec-mandated), and the
CI first-run observation row stay indexed and are out of scope.

## Compatibility contract

Public HTTP API, OpenAPI, database schema, and wire behavior are preserved
with exactly two closed-vocabulary extensions, both pre-announced by their
BACKLOG rows:

1. **Canonical-recovery admission token** (CLI admission-refusal label row):
   one new code joins the shared closed error registry (the
   `canonical_recovery_*` family is part of the OpenAPI `ErrorCode` enum), so
   the registry, OpenAPI snapshot, generated client, contract tests, CLI exit
   table, and runbook change together. Exit code 78 and retryability are
   unchanged.
2. **Lockout audit action** (web-auth lockout row): one new internal audit
   action token (`authentication.*` vocabulary in the credential store). It is
   not surfaced through any HTTP route, OpenAPI schema, or generated client.

Everything else distinguishes conditions only through internal diagnostics,
event payloads, metrics labels, audit rows, UI affordances, or tests, using
already-approved safe-token vocabulary. No migration is added. Every newly
closed error path surfaces its reason token at a readable surface
(AGENTS diagnostics rule).

## Object-storage hygiene (checkpoint 2)

### Shielded-cancellation invariant

When a shielded cleanup runs during cancellation handling and itself raises,
the original `CancelledError` must still propagate to the awaiting caller. The
cleanup's exception may be recorded through the existing safe diagnostics
token path first. A deterministic test injects a raising cleanup and proves
the caller observes `CancelledError`; failure metrics are unchanged for that
path. (The 2026-08-20 retirement expressly left this edge neither implemented
nor ruled; this section is the implementation ruling.)

### Test-hygiene batch

- The resource-suite fixture must not permanently mutate the root logger:
  handler/level state is saved and restored (or scoped to a dedicated logger)
  so ordering cannot leak configuration between test modules.
- The `run_bounded` failure path must not abandon pending tasks: on failure it
  cancels and awaits outstanding tasks (no "task was destroyed" /
  unretrieved-exception warnings under `-W error` style assertions).
- `capture_diagnostic_events` must read the diagnostic logger through a
  declared test-safe surface, not a private attribute; the private boundary
  itself is not weakened.
- The two `assert`-for-control-flow spots in `adapter.py` become explicit
  raises of the mapped typed error (asserts vanish under `python -O`).
- Redundant `^$` anchors in settings regexes are dropped where the pattern
  already anchors by construction; matched/unmatched behavior is pinned
  unchanged by the existing settings tests.
- The settings loader's POSIX-only `/run/secrets` default is documented as a
  Linux-serve contract, and the tests assert the documented per-platform
  default instead of always overriding it.

## Source-publication (checkpoint 1: dispatcher; checkpoint 2: tests)

### Dispatcher polish (checkpoint 1)

- **Shutdown drains the batch.** On explicit shutdown the dispatcher stops
  polling, then waits — bounded by the existing shutdown bound — for the whole
  in-flight batch before exiting; leased outbox rows remain crash-safe through
  the existing expiry/fencing contract either way.
- **Engine disposed on connect-timeout.** When startup fails on a connection
  timeout, the async engine is disposed before the process exits (no leaked
  pool/connections).
- **Non-timeout connect failures close cleanly.** A non-timeout startup
  connection failure maps through the structured diagnostics path with its
  closed reason token and a clean process exit — no raw traceback on the
  operator surface.
- **Temporal client closed.** The Temporal `Client.close()` is awaited on
  every shutdown path (graceful and failure) within the shutdown bound.

Acceptance: dispatcher tests prove batch drain, engine disposal, client close,
and the two connect-failure classes (timeout vs other) with their closed
reason tokens; the existing crash-safety lease tests stay green.

### Test-hardening and fixture note (checkpoint 2)

- The query-plan Seq-Scan matcher classifies a plan node as a sequential scan
  iff its node-type label ends with `Seq Scan`; an index-scan-only plan
  provably does not match.
- The redundant pool-status string assertion is removed (pool state already
  asserted structurally).
- The `no_public_api` scan matches exact module/symbol patterns, not broad
  substrings (`publication`, `/sources`); a benign symbol containing those
  substrings provably passes.
- Known AST-scanner evasion shapes (aliasing, attribute indirection) are
  either detected or explicitly recorded as out of contract by pinned tests.
- The 100-replay concurrency test's timing margin is widened for CI without
  weakening the assertion (one canonical event, clean pool).
- Audit assertions tighten to the exact `actor_invalid` row; update-replay
  tests assert zero mutations; positional-index leftovers and the dead
  constants (`MAXIMUM_RECEIPT_AGE`, settings timeout literals) are removed.
- Private-attribute test accesses move to a declared surface or a documented
  module-private import (the house pattern ruled in the web-auth handoff).
- The fingerprint fixture's docstring records its provenance (how each digest
  was derived). The shared hex64 parse extraction stays conditional: it is
  implemented when (and only when) a third digest value object appears; at
  Phase 2 closure with two types this is the terminal ruling, and the
  condition transfers to that future trigger.

## Canonical-core (checkpoint 1: recovery + operations; checkpoint 2: polish)

### Recovery-acceptance minors (checkpoint 1)

- **Bundle finalize is collision-closed.** If the final bundle path already
  exists (the POSIX TOCTOU window, including an empty pre-existing final
  directory), finalize fails closed with the existing typed
  bundle-exists/integrity error on both POSIX and Windows; a test simulates
  the pre-existing final path.
- **Verify-totals recompute independently.** The offline verifier's
  `object_count` check derives its expected count independently (from the
  manifest entries / directory listing), not from the value under test.
- **Harness naming and cleanup.** The conftest `mkdtemp` prefix becomes
  descriptive (e.g. `recovery-bundle-`); the POSIX `bundle_root` branch cleans
  its temp directories on every exit path.
- **Snapshot-adapter precision.** The pending-writer query constrains
  `pg_class.relname` to the schema's own namespace (no cross-schema
  spurious-abort; genuine pending writers in the knowledge schema still
  abort); `alembic_version` is read from the configured schema name, not
  hardcoded `public`; the inert-for-NOWAIT `SET LOCAL lock_timeout` stays only
  with an inline comment and runbook note that it guards blocking-lock paths
  (NOWAIT behavior itself is pinned unchanged by test).
- **Event-loop hygiene.** Blocking file I/O on the bundle create/restore
  coroutine paths moves off the event loop (`to_thread` or equivalent); the
  per-object buffered-copy bound stays at the documented object cap and the
  runbook names that bound; failed-restore metrics keep the deliberate 0/0
  closed-sink convention, now documented in the metrics docstring and runbook.
- **Harness fidelity.** `LocalFilesystemObjectStore` rejects a
  same-digest-different-media-type re-store exactly like the real store;
  runner/shim signatures lose their `Any` types; the
  `cast(LocalFilesystemObjectStore)` type-lie is removed by fixing the
  underlying harness annotation; the unused discarded harness in
  `live_acceptance_context` is deleted.

### Operations-acceptance minors (checkpoint 1)

- **Admission token split.** A missing `--confirm-write-admission-disabled` or
  a `--confirm-target-database` mismatch refuses with a dedicated
  admission-refused registry code (`CANONICAL_RECOVERY_ADMISSION_REFUSED`,
  exit 78, not retryable, no detail) instead
  of reusing `CANONICAL_RECOVERY_ENVIRONMENT_REFUSED`; registry, OpenAPI
  snapshot, generated client, contract tests, CLI table, and the runbook
  change in one diff. Error cases: flag absent; database-name mismatch; both
  together (the admission refusal wins, unchanged precedence).
- **CLI composition rulings (code stands, documented).** The compose-time
  lazy-engine no-dispose decision and the standalone (not `verify`-composed)
  `canonical-core-test` Poe task are recorded as documented rulings where each
  lives (composition comment; Poe task comment), closing the row without code
  change — composing the local-stack task into `verify` would slow every gate
  run and the lazy engine opens no connection at compose time.

### Acceptance polish (checkpoint 2)

- The boundary test name states its actual scope (the two canonical-core
  tools), not "anywhere in tools".
- `duration_ms` in the phase-one acceptance composition flows through the
  injected clock seam so tests control it deterministically.
- The lookup-statement filter test is renamed to what it asserts (join
  structure) — cosmetic name honesty, no assertion change.

## Web authentication and authorization (checkpoint 1)

### Audit and CLI edges

- **Lockout is distinct in audit.** A valid-credential login rejected because
  the account is locked records a dedicated audit action token
  (`authentication.login_locked_out`, internal vocabulary) instead of the
  generic `authentication.login_rejected`; wrong-password and locked-out rows
  are provably distinct, lockout transitions remain unthrottled-read-safe,
  and no public HTTP surface changes. Error cases: locked account with correct
  password; locked account with wrong password (rejection semantics
  unchanged); unlocked account wrong password (generic row, unchanged).
- **Reset CLI edges.** The username-confirmation prompt does not echo the
  typed value; stdin EOF maps to a typed aborted outcome with the correct
  exit code (not `internal_error`); tests cover reset-on-unenrolled and
  status-of-archived-workspace with their closed outcomes.

### Concurrency hardening

- **Throttle-bucket first-insert race.** Two concurrent first strikes on one
  bucket both complete correctly (exactly one row, correct strike count) via
  an upsert or advisory pattern; the integration test proves the window. The
  429 path reads the clock once and reuses it (retry-after and bucket stamp
  agree); one `KeyringTotpSecretCodec` instance is composed and shared.
- **Grant-path batch.** Cold-source creation check+insert runs as one
  transaction (no duplicate cold sources under concurrency); a live-grant-cap
  rejection records its throttle attempt like every other rejection; user-code
  generation replaces the `byte % 31` mapping with an unbiased mapping
  (rejection sampling or uniform alphabet mapping); the dead `session_policy`
  attribute is removed; the terminal-rejection docstring stops overstating
  expiry-wins.

### Web Admin batches

- **Auth-state hygiene.** The recovery-continue path clears the held password
  from component state on transition; the duplicate Current-password field in
  re-auth mode is deduplicated; the orphaned bootstrap-copy module is deleted;
  `skip()` no longer swallows dismissal failure (surfaced or retried with a
  safe reason); the no-op unmount cleanup and the unused `x-csp-nonce` request
  header are removed.
- **A11y/UX.** The revoke dialog traps focus (spec 24.5 scope); the approval
  re-auth step gains an abandon path returning to a safe pre-re-auth state; a
  rate-limited user-code lookup offers a retry affordance instead of a
  terminal dead-end; `replaceState` preserves the query string; the duplicated
  `unwrapEnvelope` in `device-administration-client.ts` is replaced by the
  single shared export (if the Child 8 prep plan's export from
  `exclusion-policy-client.ts` has landed, consume it; otherwise this batch
  creates that shared export).
- **Plugin session hygiene.** A rate-limited grant creation renders the
  rate-limited label, not the offline label; the offline state offers a
  recovery affordance (retry/reconnect) rather than a dead-end;
  error-`as` casts are replaced by honest typing; the dead
  `DEVICE_AUTH_ERROR_CODES`/`LOCAL_ERROR_CODES` exports are removed;
  `normalizeSettings` preserves record names; `reconcileCrashWindow` catches
  the `saveData` rejection and surfaces its closed reason; `login()` refuses
  to overwrite an existing active record at the session-module level
  (defense in depth under the existing `canLogin` gate).

### Acceptance-test polish (checkpoint 2, authentication-acceptance-tests)

Vacuous E2E assertions on mock constants become behavior assertions; the
offline-state whitelist docstring stops overclaiming; Set-Cookie sentinels
cover every surface the journey observes setting cookies (not only the login
pair); the dead `_RETIRED_MASTER_KEY` assignment is removed; the
grant-table ordering dependency becomes order-independent (sorted read or
set-wise assertion); the reproduce script prints the same stat set as the
gate; the accepted-login password constant is derived from the single
existing fixture instead of duplicated; the inert `re.MULTILINE` flag is
removed or made meaningful.

## Failure handling and observability

All external calls keep their existing timeout, bounded-retry, error-mapping,
and metrics boundaries. New failure distinctions (admission refusal, lockout
audit, EOF abort, connect-failure classes, saveData rejection, dismissal
failure) surface only safe closed tokens at their designated readable surface
(audit row, CLI exit table, diagnostics event, settings line, or structured
log) — never raw values, raw tracebacks, or content. Fail-closed behavior is
unchanged everywhere; no compensating deletion or best-effort path is
introduced.

## Test and acceptance criteria

Each behavioral change starts with a focused failing test. The resulting
suites must prove:

- object-storage: cancellation survives a raising shielded cleanup; no root
  logger leakage; no abandoned tasks; public test surface for event capture;
  control flow without `assert`; documented per-platform secret-root default.
- source-publication: dispatcher shutdown drain / engine disposal / client
  close / closed-token connect failures; Seq-Scan matcher exactness;
  `no_public_api` precision; AST-scanner evasion pins; widened CI margin;
  tightened audit and replay assertions; dead code gone; fixture provenance
  documented.
- canonical-core: finalize collision-closure; independent verify totals;
  namespace-qualified pending-writer detection; configured-schema
  `alembic_version`; documented lock_timeout scope; off-loop file I/O;
  documented copy bound and 0/0 convention; faithful fake store; typed
  harness; admission token across registry/OpenAPI/client/CLI/runbook; scope
  -honest test names; seam-routed `duration_ms`.
- web-auth: distinct lockout audit rows; non-echoing reset prompt with typed
  EOF; concurrent first-strike correctness; single clock read; shared codec;
  one-transaction cold-source creation; unbiased user codes; password cleared;
  focus trap, abandon path, retry affordance, query preservation, single
  `unwrapEnvelope`; correct rate-limited label, offline recovery affordance,
  honest casts, no dead exports, name-preserving normalize, caught `saveData`,
  no active-record overwrite; and the eight acceptance-layer polish items.

`uv run poe verify`, `uv run poe api-contract-check`, focused domain suites,
and (for store-level races) the disposable `knowledge-ci-*` integration stacks
must exit 0 from one final commit per checkpoint. After evidence is recorded,
remove exactly the twenty-two rows from `BACKLOG.md` (checkpoint 1 rows at
Child 9 readiness, checkpoint 2 rows before the final Phase 2 handoff), leave
conditional/live/mobile rows intact, and write one handoff per executed wave
listing removed rows, dispositions (including the three documented
code-stands rulings: conditional hex64 extraction, CLI composition pair, and
the 0/0 closed-sink convention), exact verification commands, and any newly
deferred work with a concrete verifiable gate.
