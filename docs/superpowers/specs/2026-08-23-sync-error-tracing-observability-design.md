# Sync error tracing and observability — design spec

Date: 2026-08-23. Domain: Obsidian plugin + local API diagnostics.

## Problem

The 2026-08-22/23 live debugging session proved the system fails silently at
every layer: the plugin swallows journal-failure reasons, wire failure kinds
are recorded nowhere, server rejection metrics are unreachable in-memory, API
logs live only in the operator terminal, the status bar renders `Ready (n)`
while nothing syncs, and every in-memory diagnostic ring dies on reload. Root
causes took half a day of DB forensics instead of one paste.

## Goals

1. Failures are traceable end-to-end with a durable, restart-surviving,
   on-device diagnostic trail of closed tokens.
2. The user can export sanitized diagnostics in one action and run a bounded
   self-check that localizes the failing layer.
3. Each wire failure entry carries the server envelope's `request_id` so one
   identifier joins the client trail with server-side logs.
4. The server exposes its sync rejection evidence (closed codes, counts) to
   the authenticated Web Admin.

## Non-goals

- No third-party egress (no Sentry or similar) — conflicts with the no-egress
  privacy contract; local trails achieve the value.
- No raw content, paths, digests, tokens, hostnames or free-form strings in
  any trail, surface or export — closed vocabularies only.
- No change to sync/journal semantics: the trail observes, never acts.
- No new background daemons; persistence writes are event-driven and bounded.

## Contracts

### Diagnostic trail event (plugin)

A trail entry is `{ kind, atEpochMs, tokens }` where `kind` is one closed
enum `SyncDiagnosticKind` and `tokens` is a bounded list of closed tokens
drawn ONLY from existing closed vocabularies: `QueuePassOutcome`,
`JournalSafeErrorLabel`, `JournalStoreErrorReason`, `SyncApiFailureKind`
labels, status kinds, lifecycle run outcomes, and the fixed self-check
verdicts. Entry count cap: 128 (oldest evicted). The trail persists as one
JSON sidecar (`sync-diagnostics-trail.json`) in the plugin directory through
the existing vault adapter; a corrupt or unreadable sidecar resets to empty
and records a `trail_reset` entry. Writes are serialized and coalesce with
the appender's await.

### Export surface (plugin)

One command (`Copy sync diagnostics`) builds a sanitized text block: current
status snapshot line, the settings diagnostics line, aggregate counts, and
the trail tail (kind + local timestamp + tokens only). The block is placed on
the clipboard. A settings-tab section renders the last 5 entries and the
total count.

### Self-check (plugin)

One command (`Run sync self-check`) executes, in order: a trail
append-and-persist probe (verifies the mutation/publish path), a credential
presence check, and an origin reachability probe (closed network verdict).
Each step yields a closed verdict; the summary is shown in a notice and
appended to the trail as `self_check` entries. It never mutates sync state.
(The Obsidian `Notice` import behind these notices is a deliberate spec-19
closed-surface addition — the UI notice surface for the diagnostics
commands — mirrored in both import-surface contract tests.)

### Correlation (plugin)

`parseEnvelope`-level failures and successes expose the envelope
`request_id` when present; wire-failure trail entries carry it as an opaque
token. No new wire format — the field already exists.

### Server diagnostics (API)

One authenticated read-only admin route returns the small-file sync
rejection counters and a bounded in-memory ring of the last 50 rejection
records (`{error_code, at_epoch_ms, operation}` — closed tokens only;
reconciliation 2026-08-23: the shipped ring carries the closed `operation`
label, not the drafted `route_template` — route templates live only in the
ASGI scope, below the domain boundary, so the ring deliberately carries no
route plumbing). OpenAPI, generated client and contract tests are updated
with it.

## Privacy invariants (acceptance-critical)

- Type-level closed vocabularies; a free-form string cannot enter a trail
  entry without a compile error.
- Source-contract tests forbid path-shaped and credential-shaped substrings
  in the trail module, renderers, export builder and copy command, following
  the existing settings-tab forbidden-substrate test style.
- The sidecar, export block and admin route contain only the closed tokens,
  counts and timestamps defined above.

## Error cases

- Sidecar unreadable/corrupt → trail resets, `trail_reset` recorded, sync
  unaffected (the trail never blocks the sync path; append failures are
  swallowed into a bounded in-memory failure counter surfaced in settings).
- Clipboard unavailable → the command shows the block in a modal instead.
- Self-check origin probe failure → closed network verdict recorded; no
  retry loop.
- Admin route unauthorized → the API's existing admin auth error contract.
