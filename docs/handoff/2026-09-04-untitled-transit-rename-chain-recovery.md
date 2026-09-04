# Untitled-transit rename-chain recovery — handoff

**Date:** 2026-09-04

**Status:** COMPLETE for the rename-chain implementation, automated gates, and
release packaging. The manual two-vault L1 operator re-fire is not claimed by
this handoff.

**Final implementation commit before closure:**
`10db8f1013a636a7ae7fd369cd7463226550d8a6`
(`fix(obsidian-plugin): remove orphaned rename deferral counters`).

**Closure/package/backlog-retirement commit:**
`dab579d98f4ad56fec6ec7e71302a7924d65a21c`.

**Plan:**
[`2026-09-03-untitled-transit-rename-chain-recovery-plan.md`](../superpowers/plans/2026-09-03-untitled-transit-rename-chain-recovery-plan.md)

**Binding design:**
[`2026-09-03-untitled-transit-rename-chain-recovery-design.md`](../superpowers/specs/2026-09-03-untitled-transit-rename-chain-recovery-design.md)

## Outcome

The watcher-level create/move/rename burst now retains one durable local owner
and composes the observations into the final canonical lifecycle path. The
intermediate endpoint cannot mint a second local row, the original content
event can recover its server-issued identity by exact replay, and terminal or
reconciliation exits cannot leave an intent or missing-file counter wedging a
later operation.

The exact `2026-09-03 | device-sync | Untitled-transit burst` BACKLOG row was
retired only after every required final gate passed. The user's unrelated
BACKLOG wording changes were preserved. No server, wire, OpenAPI, PostgreSQL,
or policy contract changed.

## Final gate evidence

Evidence below contains only outcomes, counts, closed reason tokens, and the
round timestamp (`2026-09-04`, Asia/Saigon). It contains no Vault content,
locator, credential, secret, or token value.

| Gate | Result |
| --- | --- |
| `pnpm --dir apps/obsidian-plugin run test` | PASS, exit 0 — 65 files and 1,524 tests passed; coverage: 82.58% statements, 79.23% branches, 85.15% functions, 82.69% lines. |
| `pnpm --dir apps/obsidian-plugin run type-check` | PASS, exit 0 — `tsc --noEmit`. |
| `pnpm --dir apps/obsidian-plugin run lint` | PASS, exit 0 — ESLint completed with zero warnings allowed. |
| `pnpm --dir apps/obsidian-plugin run build` | PASS, exit 0 — the release package was rebuilt from source. |
| Final dist inspection | PASS — the build produced exactly `main.js` (877,801 bytes), `manifest.json` (265 bytes), and `sql-wasm.wasm` (658,410 bytes). The manifest and WASM hashes match their declared build inputs; all three artifacts are included for the next deployment to both live-vault fixtures. |
| `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-untitledrename-20260904 uv run poe device-sync-test` | PASS on the fresh unchanged full rerun, exit 0 — 1,863 passed, 2 skipped, 1 deselected, 1 warning in 627.69 seconds. |
| `uv run poe verify` | PASS, exit 0 — format, lint, strict types, import boundaries, API artifact checks, 4,624 Python tests (21 skipped, 550 deselected), API-client 1/1, plugin 1,524/1,524, web 161/161, and all Python/TypeScript production builds completed. |
| Disposable-stack cleanup | PASS — `CI=true bash .local/serve-live-ci.sh down` exited 0; both `knowledge-ci-untitledrename-20260904` and `knowledge-local` then reported `stack_absent`. |

The first full device-sync attempt closed with 13 fixture-seed failures before
the service under test ran. Every failure carried the same PostgreSQL reason,
`ck_content_objects__verification`: the host-generated `verified_at` was a few
microseconds later than the container's transaction-scoped `created_at`.
Task 3 changed no Python fixture or canonical schema. The exact first failing
test then passed unchanged on a fresh project (1/1), followed by the complete
unchanged full rerun above. Ruling: **code stands**; this was transient
host/container clock skew in an old integration fixture, not a rename-chain
failure. The final authoritative gate is green, so no deferred row was added.

The existing Vite CommonJS/ESM notice and Starlette `httpx` deprecation notice
remain warnings only; all owning commands exited 0.

## Interpreted design rulings

1. **The fallback is rejected.** Settle-time endpoint re-derivation from the
   filesystem, fingerprints, timing, or unrelated-row scans cannot prove that
   a prior-miss observation belongs to the original owner and could capture a
   reused path. Ownership therefore comes only from the local row, the durable
   intent's current endpoint, or an owner-bound predecessor observation.
2. **One owner carries one durable chain.** Journal schema v10 stores at most
   one `pending_rename_intents` row per `local_file_id`. Linked observations
   update only its current endpoint; every delayed re-arm, materialization,
   exact-echo probe, and relevant byte read re-reads that durable state.
   Intermediate/current-path admission is suppressed on the existing serialized
   admission tail, so the burst cannot mint R2.
3. **The missing-file exception is intent-aware and bounded.** Preflight keeps
   the original owner locator so E1's idempotency identity remains stable,
   while both single-part and multipart byte reads use the current intent
   endpoint. If it is absent, calls 1–40 atomically retain E1 in retry and
   persist a dedicated counter. Call 41 atomically closes E1, reparents and
   clears the intent/counter, and transfers the row to reconciliation. A
   mismatched event takes the conflict-reconciliation branch before cutoff
   evaluation and never resets the counter.
4. **Reversal is phase-aware.** Before a lifecycle prefix exists, returning to
   the prior endpoint cancels the unmaterialized chain. After an immutable
   prefix exists, equal intent endpoints mean compensation pending, not
   success or cancellation. The prefix receipt rebases the same intent and
   arms exactly one compensating successor; restart and exact replay retain
   that distinction.
5. **Terminal cleanup follows ownership proof.** A pure create without an
   intent keeps the prior heal behavior. An intent-owned heal, last pre-identity
   terminal content event, row-specific reconciliation, or terminal lifecycle
   rejection reparents to the latest durable target and clears all bound
   intent/counter state in the owning transaction. Identityful or
   successor-backed content terminals preserve the chain. Final receipts clear,
   partial-prefix receipts rebase, owner deletion removes dependent state, and
   a confirmed journal rebuild starts reconciliation without copying intents.
   Restore reservations and the existing delete ladder retain their guards.
6. **Diagnostics are closed and readable.** The durable trail exposes only
   `pending_rename_intent_read_failed`,
   `pending_rename_intent_persist_failed`,
   `pending_rename_intent_conflict`,
   `pending_rename_intent_exhausted`, and
   `pending_rename_intent_lifecycle_rejected` at their owning boundaries.
   A rolled-back lifecycle reconciliation emits only the existing
   `lifecycle_reconcile_persist_failed`. Successful suppression/re-arm is
   silent, and no endpoint, identity, fingerprint, content, or exception text
   enters the trail.

## Deferred and closed decisions

- **Closed:** the scope-owned Untitled-transit loss and its dedicated BACKLOG
  row. The Task 1 watcher schedule, restart recovery, direct lifecycle
  rejection, terminal cleanup, and counter cutoff are all pinned by the green
  Task 3 suite and the final gates above.
- **Deferred by this handoff:** none. No scope-owned item was added to
  `BACKLOG.md`.
- **Not claimed:** the manual Desktop L1 lifecycle-rejection-ring journey. It
  remains the next operator action already indexed by the existing
  `2026-08-24 | closed-reason-surfacing` row; automated tests do not substitute
  for that evidence.

## Next actions

1. Prepare and redeploy the committed `main.js`, `manifest.json`, and
   `sql-wasm.wasm` package to **both** dedicated test-vault fixtures described
   by the
   [L1 readback handoff](2026-09-03-lifecycle-rejection-ring-live-readback.md).
2. Re-stand an approved disposable `knowledge-ci-*` project through the
   repository live-CI/bootstrap contracts, then re-fire the L1 operator journey
   directly in Obsidian Desktop on the isolated fixtures (no WDIO).
3. Record only sanitized outcome, reason token, count, and timestamp evidence;
   do not record content, locator, credentials, tokens, or sensitive
   screenshots. Tear the disposable stack down after the round.

## Closure artifacts

- Release package: `apps/obsidian-plugin/dist/main.js`, `manifest.json`, and
  `sql-wasm.wasm`.
- This single handoff.
- Exact retirement of the Untitled-transit BACKLOG row; unrelated user edits
  remain intact.
