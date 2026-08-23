# Sync error tracing operations

This guide covers the operator playbook for tracing Obsidian small-file
sync failures end-to-end: the durable closed-token diagnostics trail inside
the plugin, the bounded `Run sync self-check` command, the one-action
`Copy sync diagnostics` export, the settings diagnostics trail section, and
the authenticated Web Admin sync-rejection route of the API. It replaces
the 2026-08-22/23 debugging pattern — half a day of database forensics per
silent failure — with one paste and one lookup.

The operator surface is small and deliberately redacted:

- One **sidecar ring** (`sync-diagnostics-trail.json`, inside the Vault's
  plugin directory) holding at most 128 durable closed-token entries.
- Two **commands**: `Run sync self-check` and `Copy sync diagnostics`
  (alongside the pre-existing `Sync now`, `Sync existing files` and
  `Restore selected tombstone` commands).
- One **settings section**: `Sync diagnostics trail`.
- One **admin route**: `GET /api/admin/sync/rejections` on the API, behind
  the strict Web Admin session gate.

Every one of these surfaces carries ONLY closed tokens, counts and
ISO-8601 UTC timestamps. No path, hostname, origin, credential, digest,
source id, tombstone id, device id, status number, response body or
free-form error text is ever recorded or rendered. The export block and
the admin route are safe to paste into an issue verbatim.

Live setup details (launcher, stack secrets, restart sequence) live at
[`.local/RESTART.md`](../../.local/RESTART.md) — never copy them here.

## The durable diagnostics trail

One trail entry is `{kind, at timestamp, tokens}`: a closed kind, an
ISO-8601 UTC timestamp and at most eight closed tokens. The trail is a
bounded ring — the oldest entries are evicted beyond 128 — and persists
through the Vault's plugin directory, so it survives plugin reloads and
application restarts. A corrupt or unreadable sidecar resets the trail to
empty and records a `trail_reset` entry; sync is never blocked by the
trail, and swallowed append/persist failures accumulate in a bounded
counter (capped at 999) that the settings section surfaces.

| Kind              | Recorded when                                                                                   |
| ----------------- | ----------------------------------------------------------------------------------------------- |
| `wire_failure`    | One sync HTTP request failed; carries the closed `SyncApiFailureKind` label and, when the server answered with an envelope, the opaque `request_id` token. |
| `pass_outcome`    | Every finished queue pass; carries the closed `QueuePassOutcome`. A success that returned a server envelope may sample its `request_id` onto the entry. |
| `journal_failure` | A journal mutation inside the pass loop failed; carries the closed `JournalStoreErrorReason`.   |
| `publish_failure` | A journal generation publish failed; carries the closed `JournalStoreErrorReason`.               |
| `trail_reset`     | The sidecar was unreadable or corrupt and the trail reset to empty.                             |
| `self_check`      | A `Run sync self-check` step closed; carries the fixed self-check verdict tokens.               |

The closed token vocabularies are exactly the existing sync vocabularies:

- `QueuePassOutcome`: `completed`, `deadline_reached`, `stopped`,
  `login_required`, `retry_scheduled`, `pass_already_running`.
- `SyncApiFailureKind`: `network_offline`, `network_timeout`,
  `network_rate_limited`, `server_error`, `access_expired`,
  `login_required`, `blocked_size`, `integrity_failed`,
  `operation_retry_required`.
- `JournalStoreErrorReason`: `journal_schema_unsupported`,
  `journal_image_invalid`, `journal_mutation_failed`,
  `journal_query_failed`, `journal_store_unavailable`,
  `journal_generation_write_failed`, `journal_manifest_invalid`,
  `journal_not_open`.
- `JournalSafeErrorLabel` (pass/event outcomes):
  `excluded_policy`, `blocked_size`, `blocked_conflict`,
  `deferred_lifecycle`, `integrity_failed`, `reconcile_required`,
  `committed`.
- `LifecycleRunOutcome`: `idle`, `committed`, `blocked`, `retry`,
  `login_required`.
- Self-check verdicts: `trail_probe`, `trail_persist_ok`,
  `trail_persist_failed`, `credential_present`, `credential_absent`,
  `origin_reachable`, `origin_unreachable`.

The one opaque value that may ride along is the server envelope's
`request_id` (a UUID), rendered as `request_id=<uuid>`. It is the
correlation token that joins the client trail with server-side logs (see
below); it identifies no content, no account and no device.

## Run sync self-check (localize the failing layer)

The command palette's **Run sync self-check** executes three steps
strictly in order and shows one notice line. It never mutates sync state —
no journal event, no preflight request, no policy read — and the one
origin probe runs under a five-second bound with no retry.

| Step                   | Verdict tokens                                    | What it proves                                                     |
| ---------------------- | ------------------------------------------------- | ------------------------------------------------------------------ |
| Trail persist probe    | `trail_probe` then `trail_persist_ok` / `trail_persist_failed` | The trail's append-and-persist write path through the Vault plugin directory works. |
| Credential presence    | `credential_present` / `credential_absent`         | An access credential exists locally (the token value never enters the check). |
| Origin reachability    | `origin_reachable` / `origin_unreachable` (with `network_offline` or `network_timeout`) | Anything at the configured origin answered the side-effect-free liveness route. |

Sanitized notice examples:

```text
Sync self-check: trail_persist_ok · credential_present · origin_reachable
Sync self-check: trail_persist_ok · credential_present · origin_unreachable · network_timeout
```

Reading the verdicts:

- `trail_persist_failed` → the plugin cannot durably write its own
  directory (the trail, and likely the journal sidecars, are unhealthy).
  Check the settings append-failure counter and the `trail_reset` history.
- `credential_absent` → login first; the pass will end `login_required`
  and queue the work unchanged.
- `origin_unreachable · network_offline` or `network_timeout` → a
  network/origin problem, not a content or credential problem. No
  hostname, status number or response text is ever shown.

When the journal stack failed closed at load (no trail exists), the
command answers with a fixed "journal not running on this device" notice —
that fact alone already localizes the failure to plugin startup.

Every step also appends its verdict to the trail as `self_check` entries,
so a past run can be read back from the settings section or the export.

## Copy sync diagnostics (the one-action sanitized export)

The command palette's **Copy sync diagnostics** builds one sanitized text
block and places it on the clipboard; when the clipboard is unavailable
the same block is shown in a read-only preformatted modal. The block is
assembled ONLY from already-redacted closed surfaces — the current status
line, the blocker guidance, the journal-store diagnostics, the aggregate
trail counts and the last five trail entries — so it is safe to paste
anywhere.

Sanitized example (shape only; tokens and counts vary):

```text
obsidian_sync_diagnostics_export/v1
Status: Ready (3)
Blocker: Login required: open the existing browser login from the plugin settings. Queued work is kept unchanged.
Journal store diagnostics:
  Pass failures: journal_query_failed
  Generation publish failures: 2 (journal_generation_write_failed)
Trail entries: 42
Trail append failures: 1
Trail tail (last 5):
2026-08-23T09:41:18.000Z · wire_failure · server_error · request_id=018f6c2e-8a1f-7b3c-9d2e-4f5a6b7c8d9e
2026-08-23T09:41:19.000Z · pass_outcome · login_required
2026-08-23T09:52:02.000Z · journal_failure · journal_mutation_failed
2026-08-23T09:52:02.000Z · publish_failure · journal_generation_write_failed
2026-08-23T09:55:40.000Z · self_check · origin_unreachable · network_timeout
```

All timestamps are ISO-8601 UTC by design: the block is a shareable
paste, and local-time offsets would leak coarse location.

## The settings "Sync diagnostics trail" section

The plugin settings tab renders one read-only section that folds the
durable trail into three lines:

- **Stop reasons** — the newest closed token of each failure kind, in the
  fixed order `journal_failure`, `publish_failure`, `wire_failure`. This
  answers "why did syncing stop" without opening the export.
- **Trail entries / append failures** — the total durable entry count and
  the bounded swallowed-append-failure counter (a non-zero counter means
  the sidecar write path is failing even though sync continues).
- **The last five entries** — the same closed lines the export renders.

## Correlate a client failure with API access logs (the request_id join)

Every API exchange is minted one server request id (UUIDv7) by the request
correlation middleware. It is returned in the response envelope body
(`request_id`) and the `X-Request-ID` response header — the two are always
equal — and every structured API log line carries it. The plugin threads
the envelope's `request_id` out of both failure and success parses, so a
`wire_failure` (or a success-sampling `pass_outcome`) trail entry carries
exactly that identifier as its opaque token.

The join, start to finish:

1. Run **Copy sync diagnostics** and read the trail tail.
2. Take the `request_id=<uuid>` token of the failing `wire_failure` entry.
3. Search the API's structured logs for that UUID (the operator terminal
   or wherever the JSON log stream is captured; see
   [`api-runtime-contract.md`](api-runtime-contract.md)). Uvicorn's access
   log is disabled — the structured events are the only request-level
   logging.
4. The matching lines carry the closed route template, the HTTP status,
   the duration and any structured error events of that exchange — enough
   to tell a transport failure (no lines at all: the request never
   arrived) from a server rejection (one `api_request_rejected` line with
   the closed route and status).

The success-side sampling rule: there is no success kind in the trail
vocabulary, and per-request success entries would churn the 128-entry cap,
so only the `pass_outcome` entry of the pass that observed the envelope
may carry a success `request_id`.

## Web Admin sync rejection diagnostics

The API exposes its small-file sync rejection evidence to an
authenticated Web Admin session at `GET /api/admin/sync/rejections`
(semantic operation id `getSyncRejectionDiagnostics`). The route is
read-only, resolves behind the strict active-session origin gate exactly
like the Admin device list — a plugin device credential is never a Web
authority and answers the closed 401 authentication error — and every
response carries the canonical envelope plus `Cache-Control: no-store`.

Two shapes come back inside the envelope's `data`:

- `rejection_counters` — rows of `{operation, error_code, count}`,
  deterministically sorted, one per observed (operation, reason) pair.
- `recent_rejections` — the bounded ring of the last 50 rejections, oldest
  first, each `{error_code, at_epoch_ms, operation}`.

`operation` is the closed label `create` or `update`; it stands in for the
design's route-template token (route templates live only in the ASGI
scope, below the domain boundary, so the ring deliberately carries no
route plumbing). `error_code` is the closed rejection registry:

| `error_code`                          | Meaning                                              |
| ------------------------------------- | ---------------------------------------------------- |
| `small_file_preflight_invalid`        | The preflight request body failed closed validation. |
| `small_file_operation_not_found`      | The upload operation token is unknown.               |
| `small_file_operation_expired`        | The upload operation token expired.                  |
| `small_file_operation_identity_mismatch` | The committed identity drifted from the operation. |
| `small_file_size_limit_exceeded`      | The content exceeds the small-file size boundary.    |
| `small_file_content_integrity_failed` | The streamed bytes failed the integrity check.       |
| `small_file_upload_state_invalid`     | The upload state machine rejected the transition.    |

Sanitized example (shape only):

```json
{
  "request_id": "018f6c31-2b4d-7e5f-8a9b-0c1d2e3f4a5b",
  "data": {
    "rejection_counters": [
      { "operation": "update", "error_code": "small_file_preflight_invalid", "count": 3 }
    ],
    "recent_rejections": [
      { "error_code": "small_file_preflight_invalid", "at_epoch_ms": 1784460000000, "operation": "update" }
    ]
  },
  "error": null,
  "warnings": []
}
```

The counters and ring are in-memory: they reset on API restart and are
per-process. The plugin-side trail (above) is the durable half of the
picture — a counter without a matching `wire_failure` entry means the
client never saw (or never reached) the rejecting exchange, while a
`wire_failure · request_id` entry with no server-side line for that UUID
means the request never arrived.

## Privacy invariants

- The sidecar, the export block, the settings section, the notices and
  the admin route carry only closed tokens, counts and timestamps — no
  paths, hostnames, origins, credentials, digests, source ids, device ids
  or free-form strings.
- The closed vocabularies are enforced at the type level in the plugin (a
  free-form string cannot enter a trail entry) and by contract tests on
  both sides, including forbidden-substrate scans for path-shaped and
  credential-shaped text.
- The self-check's origin probe closes as a verdict only; the hostname,
  any status number and any response text never enter a notice or entry.
- The admin route answers the closed Web auth error contract for
  unauthenticated or unauthorized callers; it never echoes the caller's
  material.

## Linked references

- Design contract (`docs/superpowers/specs/2026-08-23-sync-error-tracing-observability-design.md`)
  — the trail, export, self-check, correlation and admin-route contracts
  this guide operates.
- Plugin modules (`apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts`,
  `sync-diagnostics-export.ts`, `sync-self-check.ts`) — the closed
  vocabularies and bounds.
- Metrics ring and wire models (`src/personal_os/small_file_sync/metrics.py`,
  `apps/api/src/api_runtime/small_file_sync_diagnostics_models.py`) — the
  rejection ring, its read-side snapshot and the strict response models.
- Envelope and logging contract ([`api-runtime-contract.md`](api-runtime-contract.md))
  — the envelope `request_id` / `X-Request-ID` equality and the
  structured-events-only logging posture.
- Redacted operator surface of the sync lifecycle
  ([`source-locator-tombstone-lifecycle.md`](source-locator-tombstone-lifecycle.md)).
- Live launcher / secrets — [`.local/RESTART.md`](../../.local/RESTART.md)
  (NEVER copy launcher details or secrets into this guide).

## Live verification procedure

Follow [`.local/RESTART.md`](../../.local/RESTART.md) exactly (stack
status, the repository serve/worker launchers, Web Admin on port 38000,
the existing tunnel). The diagnosis loop this runbook exists for is:

1. Reload the Obsidian plugin (the trail must survive the reload).
2. Run **Run sync self-check** and read the three verdicts.
3. Run **Copy sync diagnostics** and paste the block into the operator
   record.
4. Join any `wire_failure · request_id` token with the API log stream.
5. Read `GET /api/admin/sync/rejections` from the authenticated Web Admin
   session and compare its counters with the trail.

The first supervised execution of this loop (against the convergence
plan's open park diagnosis) is recorded in the plan handoff
(`docs/handoff/2026-08-23-sync-error-tracing-observability.md`); until
that round runs, treat the loop as specified-but-not-yet-observed. Any
recorded evidence must stay sanitized exactly like the examples above.
