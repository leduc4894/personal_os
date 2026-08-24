# Closed-reason surfacing remediation — design spec

Date: 2026-08-24. Domains: Obsidian plugin (auth/session, composition root),
local API (source lifecycle), policy workers. Governing rule:
`AGENTS.md` (no silent swallowing) and `docs/15-OBSERVABILITY_AND_ALERTING.md`
§2/§7.

## Problem

A full audit of the landed domains (Phase 1 core + Phase 2 sync through
child 6) against the new observability rule found 14 closed-error paths whose
reason tokens reach nowhere readable. The sync/journal domain is the
reference-good pattern (durable trail + admin rejection route); every gap
below is a deviation from that pattern in a neighboring domain.

### Audit findings (file:line verified 2026-08-24)

**Plugin composition root (`apps/obsidian-plugin/src/plugin.ts`)**

- P1 Journal startup failure is fully silent: `#startJournalCapture`'s
  closing `catch {}` (plugin.ts:746-749) discards whether the engine load,
  wasm read, or journal recovery failed. Largest single black hole: no
  capture surface + no reason anywhere.
- P2 Queue-pass wrapper misreports success: an unexpected `requestPass()`
  throw is swallowed and replaced with
  `{outcome: "completed", processedEventCount: 0}` (plugin.ts:971-977).
- P3 `policy_integrity_failed` is invisible: the state is stored and gates
  capture (plugin.ts:215, 267-270, 487-489) but never reaches the settings
  snapshot (plugin.ts:312-351) — policy failures stop sync silently.
- P4 (minor) fire-and-forget startup chains can discard exceptional throws
  (plugin.ts:402, 408-416).
- P5 (minor) pending-count and note-status reads swallow to `0`/`[]`
  (plugin.ts:642-644, 1133-1139).

**Plugin auth/session (`apps/obsidian-plugin/src/authentication/`)**

The `onStateChange(state, detail)` seam exists and the settings tab renders
`detail` (settings-tab.ts:111-117), but every failure transition passes
`detail: null`, discarding closed tokens the transport already produced:

- A1 Onboarding exchange failure: bare `catch {}` around the policy-trust
  bootstrap drops `policy_*` tokens (device-authorization.ts:315-323).
- A2 Refresh failure: `network_unavailable` and unknown fallback emit
  `offline|refresh_required` with `null` while holding the closed code
  (token-session.ts:118-137).
- A3 Terminal cleared-reasons are durable in the tombstone
  (`token_reuse`, `device_revoked`, `credential_invalid`, `grant_denied`,
  `grant_expired`, `grant_invalid`, `login_cancelled`, `self_disconnect`)
  but never rendered — the user sees only "Revoked"/"Not connected"
  (token-session.ts:139-148, device-authorization.ts:225-234).
- A4 Grant-creation failure collapses all non-mapped codes to `offline/null`
  (device-authorization.ts:236-251).
- A5 Poll failure classification discards the underlying code
  (device-authorization.ts:283-296, 335-363).

**Server lifecycle API**

- L1 `InMemorySourceLifecycleMetrics` is write-only in production: recorded
  at source_lifecycle/service.py:122-154, wired at
  api_runtime/server.py:166, read by nothing — no admin route parity with
  `GET /api/admin/sync/rejections`.
- L2 Typed rejections carry their closed code only in the client envelope;
  server-side operators see the 4xx status alone
  (application.py:752-756; access observation carries status only).

**Policy workers (`apps/worker`)**

- W1 Preview/reconciliation dispatch loops swallow unexpected failures with
  `except Exception: return` and no diagnostic sink is injected at all
  (policy_workflow_runtime.py:244-247, 392-395) — the root of the
  "preview kẹt running mãi" failure mode documented in `.local/RESTART.md`.
- W2 Reconciliation failure reasons are durable (`safe_error_code` in the
  store, policy_reconciliation.py:358-391, 1512-1560) but the admin summary
  selects only state/updated_at — "failed" with no why
  (exclusion_policy_composition.py:428-462).
- W3 No liveness/staleness surface detects a dead worker: rows sit
  `running` forever (observation; needs a heartbeat or stale-running
  detection, not a swallow fix).

Verified clean (excluded): `src/journal/**` (the reference pattern), the
diagnostics logging boundary, Web Admin server-side, all Python broad
catches (each maps to a closed code and emits it).

## Goals

1. Every audited path surfaces its closed reason token at a readable place,
   using the established surfaces: the plugin diagnostics trail, the
   settings snapshot `detail`, admin diagnostics routes, closed-code log
   events.
2. No new surface types: reuse the trail kinds/vocabulary, the settings
   snapshot fields, the admin-route pattern, and the structured log events —
   extensions only where a new closed token is unavoidable.
3. Honest summaries: no swallowed failure may render as a success state
   (P2), and no silent stop (P1, P3, W3 detection).

## Non-goals

- No change to sync/journal semantics, auth state machines, or worker
  scheduling behavior — surfacing only, except W3's detection which is
  read-only alerting surface (no auto-restart).
- No Phase-10 infrastructure (Prometheus/Grafana/Tempo/Alertmanager).
- No per-request free-form logging anywhere; closed vocabularies only.

## Contracts

### C1 Plugin composition surfacing (P1, P2, P3; P4/P5 folded)

- P1: `#startJournalCapture`'s catch records a trail entry (new closed kind
  `startup_failure` with tokens naming the failed stage: engine_load /
  wasm_read / journal_recovery / other, plus the closed
  `JournalStoreErrorReason` when the throw is a store error) and exposes a
  `lastStartupFailureTokens` field in the settings snapshot. The
  self-check's "journal not running" verdict must render the same tokens.
- P2: the queue-pass wrapper's catch records the outcome as a trail
  `pass_outcome` entry carrying a new closed token `pass_wrapper_failed`
  (never `completed`) and keeps the pass summary honest (a wrapper-level
  closed outcome or the existing `completed`-with-zero only when genuinely
  idle).
- P3: `policyState` (closed enum incl. `policy_integrity_failed`) joins the
  settings snapshot and the status-bar/settings guidance maps each closed
  value to one fixed guidance line.
- P4: the two fire-and-forget startup chains route exceptional throws into
  the same `startup_failure` trail path.
- P5: the two read-swallows record one bounded closed token trail entry
  (`status_read_failed` / `note_status_read_failed`) at most once per
  session per site (no per-render spam).

### C2 Plugin auth detail tokens (A1–A5)

- Every `onStateChange` failure transition passes the closed reason token
  it already holds as `detail` (snake_case closed tokens: transport codes,
  `policy_*` tokens, 5xx server codes, `ClearedReason` values). The tokens
  must come from existing closed enums — no new vocabulary except where the
  audit shows a code exists but is dropped.
- A3: the terminal tombstone reason joins the settings snapshot so
  "Revoked"/"Not connected" renders its durable cause.
- The settings tab's existing detail line renders them unchanged; no UI
  redesign.

### C3 Lifecycle admin route parity (L1, and L2 satisfied by it)

- One authenticated read-only admin route (mirroring
  `small_file_sync_diagnostics_routes.py`) returns the lifecycle metrics:
  commit counters + a bounded recent-rejection ring with closed
  `error_code`/`operation`/timestamp tokens. OpenAPI, generated client and
  contract tests updated per repo rules.
- L2 needs no separate change: the readable ring satisfies the operator
  surface; the envelope already carries the code to the client/trail.

### C4 Worker dispatch sinks and reconciliation reasons (W1, W2)

- W1: both worker runtimes accept an injected diagnostic sink and emit one
  closed-code event (`preview_dispatch_unavailable` /
  `reconciliation_dispatch_unavailable`) at the two swallowed catches; the
  events ride the existing structured logging boundary (and thus the
  rotating file sink).
- W2: the admin reconciliation summary selects and renders `safe_error_code`
  (closed token, null-safe) — parity with the preview surface.

### C5 Worker liveness detection (W3)

- A staleness surface only: the admin policy summary (or a sibling admin
  read) reports rows whose `running` state exceeds a fixed staleness bound
  (closed token like `worker_stale_running` with the age); no auto-restart,
  no background daemon — computed on read.

## Privacy invariants (acceptance-critical)

- Only closed tokens (existing enums + the minimal new tokens named above),
  counts, timestamps and opaque ids on every new surface; type-level
  enforcement where the surface is in TypeScript (trail vocabulary
  pattern), closed StrEnum/validation in Python.
- No paths, hostnames, digests, credentials, raw bodies or exception text
  anywhere; source-contract/forbidden-substrate tests extended to the new
  snapshot fields and routes.
- All trail appends stay fire-and-forget with the proven never-blocks
  guarantee.

## Acceptance criteria

1. Each audit item P1–P5, A1–A5, L1–L2, W1–W3 maps to at least one failing
   RED test that proves the reason is currently invisible, then GREEN with
   the surface (behavioral where possible; source-contract where the house
   style requires).
2. The audit's excluded-clean set remains clean (no regression in the
   reference pattern; no new swallow introduced).
3. Full offline gates green (plugin vitest/tsc/build/eslint; Python unit +
   contract + mypy + ruff; `uv run poe verify`).
4. One live smoke round on the user's vault after implementation: trigger
   one visible failure of each class where cheap (e.g. wrong server origin
   → A-class tokens; stop worker → W3 staleness) and read them back from
   Copy sync diagnostics / Web Admin.

## Error cases

- Trail/sink unavailable: surfaces degrade exactly like the existing
  pattern (bounded failure counter, never blocks the failing path being
  surfaced).
- Admin routes unauthorized: existing closed auth errors, mirroring the
  sync-rejections route.
- W3 staleness read fails: renders the existing closed dependency error; no
  partial data.
- Snapshot fields null before first failure: rendered as absent, never as a
  fake success token.
