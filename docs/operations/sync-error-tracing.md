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
  plugin directory) holding at most 128 durable closed-token entries under
  the versioned contract `obsidian_sync_diagnostics_trail/v2`; a legacy v1
  sidecar is migrated losslessly (its known entries parse under the same
  closed gates) and a foreign token still resets through `trail_reset`.
- Two **commands**: `Run sync self-check` and `Copy sync diagnostics`,
  alongside the `Restore selected tombstone` command and the Child 6
  `Repair sync` command — exactly these four commands are registered in the
  plugin's command palette (`Repair sync` is owned by
  [`device-cursor-manifest-reconciliation.md`](device-cursor-manifest-reconciliation.md)).
- One **settings section**: `Sync diagnostics trail`, plus the settings
  **detail lines** that render the closed failure reasons of the
  composition and auth layers (journal startup failure, policy state,
  connection detail, last cleared reason).
- Three **admin routes**: `GET /api/admin/sync/rejections` (small-file sync),
  `GET /api/admin/source-lifecycle/rejections` (source lifecycle), and
  `GET /api/admin/exclusion-policy/diagnostics` (policy evaluation counters
  and the recent policy-system-failure ring), plus the Admin policy status
  read `GET /api/admin/exclusion-policy` whose summary carries the
  reconciliation reason and the stale-running staleness block — all behind
  the strict Web Admin session gate.
- Two **Web Admin UI surfaces** rendering those reads (2026-08-29): the
  policy page's `PolicyStatus` preview-worker health block (one row per
  stale preview with its age and restart guidance) and the
  `/admin/lifecycle` page's lifecycle-operations card (commit counters
  plus the recent rejection ring).
- The worker **dispatch events** (`preview_dispatch_unavailable`,
  `reconciliation_dispatch_unavailable`) riding the structured logging
  boundary.

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

Persist-failure observability (child six remediation): every swallowed
persist failure also records ONE bounded `self_check · trail_persist_failed`
marker entry per failure episode — the marker re-arms only after the next
successful persist, rides that persist into the sidecar as an honest
durable record, and needs no `Run sync self-check` invocation. The counter
saturates at 999 and then stops moving, so a saturated counter cannot by
itself flag a NEW failure; the marker entry and the visible `999` reading
together still localize the failure to the trail write path (inside that
saturation window the self-check's trail-persist probe conservatively
passes for the same reason — a failed write cannot move an already-full
counter).

| Kind              | Recorded when                                                                                   |
| ----------------- | ----------------------------------------------------------------------------------------------- |
| `wire_failure`    | One sync HTTP attempt actually reached the transport and failed; carries the closed `SyncApiFailureKind` label and, when the server answered with an envelope, the opaque `request_id` token. A missing credential or a failed refresh BEFORE contact records `credential_failure` instead (Child 6 taxonomy split). |
| `pass_outcome`    | Every finished queue pass; carries the closed `QueuePassOutcome`. A success that returned a server envelope may sample its `request_id` onto the entry. |
| `journal_failure` | A journal mutation inside the pass loop failed; carries the closed `JournalStoreErrorReason`. It also carries the closed journal-orchestration failure tokens listed below when composition, scheduling, drain, capture or reconcile work fails closed. |
| `publish_failure` | A journal generation publish failed; carries the closed `JournalStoreErrorReason`.               |
| `trail_reset`     | The sidecar was unreadable or corrupt and the trail reset to empty.                             |
| `self_check`      | A `Run sync self-check` step closed; carries the fixed self-check verdict tokens. The trail itself also records one `trail_persist_failed` entry per persist-failure episode, and the copy command records one on its own exceptional rejection (below). |
| `startup_failure` | The journal startup chain (engine load, wasm read, journal recovery, or a fire-and-forget startup action) threw and capture failed closed; carries exactly one startup stage token, plus the closed `JournalStoreErrorReason` when the throw is a store error. The same tokens persist in the settings snapshot as the `lastStartupFailureTokens` field. |
| `credential_failure` | Child 6: the device-sync lane could not even contact the wire because no access credential exists (`access_missing`) or a refresh failed (`refresh_failed`). |
| `cursor_failure`  | Child 6: a device cursor phase closed failed; carries one stage `pull` or `acknowledge` plus the closed reason. |
| `apply_failure`   | Child 6: a remote apply closed failed; carries one stage of `prepare`, `download`, `verify_temp`, `vault_mutation`, `verify_final`, `local_commit`, `recovery`, `trash` plus the closed reason. |
| `reconcile_failure` | Child 6: a manifest reconciliation phase closed failed; carries one stage of `start`, `page`, `finalize`, `actions`, `complete` plus the closed reason. |
| `composition_read_failure` | Child 6: a once-per-session settings/status projection read failed and used its fail-closed fallback; carries one stage of `status_read`, `note_status_read`, `retry_schedule_read`, `sync_status_read`. Deliberately excluded from the derived settings stop-reason line — these reads never stop sync. |

The Child 6 failure kinds append, in order: the stage token, the closed
reason, then the gated correlation facts — a registered server error code
(the `device_*` family or the journal-lane envelope subset) and the
UUID-gated `request_id` of a parsed server envelope; an untrusted value of
either records nothing. Reading the device-sync surfaces (cursor lag,
repair state, action counts) is owned by
[`device-cursor-manifest-reconciliation.md`](device-cursor-manifest-reconciliation.md).

The closed token vocabularies are exactly the existing sync vocabularies:

- `QueuePassOutcome`: `completed`, `deadline_reached`, `stopped`,
  `login_required`, `retry_scheduled`, `pass_already_running`,
  `pass_wrapper_failed` (the pass wrapper itself threw — an honest
  failure, never rendered as `completed`).
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
  `network_offline`, `network_timeout`, `network_rate_limited`,
  `server_error`, `login_required`, `excluded_policy`, `blocked_size`,
  `blocked_conflict`, `deferred_lifecycle`, `integrity_failed`,
  `reconcile_required`, `committed`.
- `LifecycleRunOutcome`: `idle`, `committed`, `blocked`, `retry`,
  `login_required`.
- Self-check verdicts: `trail_probe`, `trail_persist_ok`,
  `trail_persist_failed`, `credential_present`, `credential_absent`,
  `origin_reachable`, `origin_unreachable`.
- Startup stage tokens (`startup_failure` kind): `engine_load`,
  `wasm_read`, `journal_recovery`, `other`.
- Journal orchestration failure tokens (ride the `journal_failure` kind):
  `status_read_failed`, `note_status_read_failed`,
  `retry_schedule_read_failed`, `sync_status_read_failed`,
  `queue_drain_failed`, `snapshot_drain_failed`,
  `settled_admission_failed`, `automatic_snapshot_admission_failed`,
  `lifecycle_reconcile_persist_failed`,
  `restore_reservation_persist_failed`,
  `pending_rename_intent_read_failed`, `pending_rename_intent_persist_failed`,
  `pending_rename_intent_conflict`, `pending_rename_intent_exhausted`,
  `pending_rename_intent_lifecycle_rejected`. Their safe meanings and emission
  bounds are fixed below.

### Journal orchestration failure tokens

All ten tokens below name only a failed internal operation. They never
carry the thrown error, a note path, content, identifier, or credential.
They are appended as a `journal_failure` trail entry through the same bounded
128-entry ring; a failed trail append remains non-blocking and is counted by
the existing append-failure counter.

| Token | Safe operator meaning | Bounded emission behavior |
| --- | --- | --- |
| `status_read_failed` | The automatic snapshot could not read its pending-event status, so it used its existing fail-closed fallback. | At most once per plugin session for this read site. |
| `note_status_read_failed` | The settings snapshot could not read local note status, so it used its existing fail-closed fallback. | At most once per plugin session for this read site. |
| `retry_schedule_read_failed` | Retry scheduling could not read journal state and did not arm a retry timer. | At most once per plugin session for this read site. |
| `sync_status_read_failed` | Sync-status projection could not read journal state and returns no partial status. | At most once per plugin session for this read site. |
| `queue_drain_failed` | The queue coordinator drain rejected; its public wait still settles with the existing closed fallback. | One token for each rejected queue drain. |
| `snapshot_drain_failed` | The snapshot coordinator drain rejected; its public wait still settles with the existing closed fallback. | One token for each rejected snapshot drain. |
| `settled_admission_failed` | A settled content-admission operation rejected before its waiters were released. | One token for each rejected settled admission. |
| `automatic_snapshot_admission_failed` | One or more automatic-snapshot admissions rejected; affected files remain counted as skipped. | At most one token per automatic snapshot scan. |
| `lifecycle_reconcile_persist_failed` | Persisting the lifecycle `reconcile_required` marker failed; the lifecycle result stays fail-closed. | One token for each failed reconcile-marker persistence attempt. |
| `restore_reservation_persist_failed` | Persisting an explicit-restore target reservation failed; the restore command refused closed and the tombstone stays open. | One token for each failed reservation persistence attempt. |
| `pending_rename_intent_read_failed` | Reading a durable pending rename intent failed, so admission, dispatch, or startup resume failed closed. | One token for each swallowed read boundary. |
| `pending_rename_intent_persist_failed` | Persisting a pending rename observation or re-arm failed; the prior verified generation remains authoritative. | One token for each swallowed persistence boundary. |
| `pending_rename_intent_conflict` | An incompatible rename chain, endpoint collision, or corrupt guarded state transferred the owner to reconciliation. | One token for each committed conflict boundary. |
| `pending_rename_intent_exhausted` | The bounded intent-owned missing-file window ended and locator ownership transferred to reconciliation. | One token for each committed exhaustion boundary. |
| `pending_rename_intent_lifecycle_rejected` | A canonical rejection of an intent-owned rename/move prefix transferred its locator to reconciliation. | One token for each committed lifecycle rejection. |

The one opaque value that may ride along is the server envelope's
`request_id` (a UUID), rendered as `request_id=<uuid>`. It is the
correlation token that joins the client trail with server-side logs (see
below); it identifies no content, no account and no device. The trail
admits the token only through a constructor UUID gate — a non-canonical
value (free-form text, a path fragment, an uppercase/braced UUID variant)
is rejected before any entry exists and is never echoed, rendered or
logged — so even a compromised envelope shape cannot smuggle a value into
the trail.

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

- `trail_persist_failed` → either the plugin cannot durably write its own
  directory (the trail, and likely the journal sidecars, are unhealthy —
  check the settings append-failure counter and the `trail_reset` history)
  or the copy command hit its own exceptional rejection, which emits the
  same token with the counter possibly at 0.
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
trail counts and the five newest trail entries rendered newest first — so
it is safe to paste anywhere.

Sanitized example (shape only; tokens and counts vary; the tail renders
newest first — the Child 6 pinned order):

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
2026-08-23T09:55:40.000Z · self_check · origin_unreachable · network_timeout
2026-08-23T09:52:02.000Z · publish_failure · journal_generation_write_failed
2026-08-23T09:52:02.000Z · journal_failure · journal_mutation_failed
2026-08-23T09:41:19.000Z · pass_outcome · login_required
2026-08-23T09:41:18.000Z · wire_failure · server_error · request_id=018f6c2e-8a1f-7b3c-9d2e-4f5a6b7c8d9e
```

All timestamps are ISO-8601 UTC by design: the block is a shareable
paste, and local-time offsets would leak coarse location.

Failure surfacing: a clipboard that is unavailable or refuses the write is
absorbed by the read-only modal fallback above. An exceptional rejection
of the copy pipeline itself (child six remediation) can never throw into
UI processing — the command carries a rejection handler that records ONE
`self_check · trail_persist_failed` trail entry through the same bounded
mechanism, and nothing is ever logged: no console, no clipboard data, no
failure detail beyond the closed token.

## The settings "Sync diagnostics trail" section

The plugin settings tab renders one read-only section that folds the
durable trail into three lines:

- **Stop reasons** — the newest closed token of each failure kind, in the
  fixed order `journal_failure`, `publish_failure`, `wire_failure`. This
  answers "why did syncing stop" without opening the export. The input is
  typed as the existing closed-token union, so no free-form server value
  can enter the line.
- **Trail entries / append failures** — the total durable entry count and
  the bounded swallowed-append-failure counter (a non-zero counter means
  the sidecar write path is failing even though sync continues).
- **The last five entries** — the same closed lines the export renders,
  newest first.

## Settings detail lines (startup, policy state, auth reasons)

The settings tab renders four more closed-token lines beside the trail
section. Each is fixed English keyed by a closed enum value — no path,
hostname, credential or free-form text can enter any of them, and every
field is null-safe (absent before the first failure, never a fake success
token):

- **Journal startup failure** — `Journal startup failed: <stage token>[, <store reason>]`,
  rendered inside the Sync status description whenever the snapshot's
  `lastStartupFailureTokens` field is non-null. The same tokens ride the
  trail's `startup_failure` kind and the self-check's "journal not
  running" notice, so one startup failure is readable from three places.
- **Policy state** — one fixed guidance line per closed policy integrity
  state (`policy_not_initialized`, `policy_ready`,
  `policy_refresh_required`, `policy_offline_cached`,
  `policy_integrity_failed`). `policy_integrity_failed` gates capture, so
  this line is the only settings-side answer to "why did syncing silently
  stop" when policy trust broke.
- **Connection detail** — the auth state seam's closed reason token
  (transport codes, `policy_*` tokens, closed server codes) appended to
  the fixed connection status text when a failure transition carries one.
- **Last cleared reason** — `Last cleared reason: <token>` beside a
  terminal connection state, from the durable credential tombstone's
  closed `ClearedReason` (`token_reuse`, `device_revoked`,
  `credential_invalid`, `grant_denied`, `grant_expired`, `grant_invalid`,
  `login_cancelled`, `self_disconnect`).

Sanitized example (shape only):

```text
Sync status: Offline — network_unavailable
Policy state: Policy integrity failed: capture is stopped until policy trust is re-established through the authorized login flow.
Journal startup failed: journal_recovery, journal_store_unavailable
Last cleared reason: device_revoked
```

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
   logging. The local launcher additionally keeps the same redacted lines in
   a durable rotating sink at `.local/runtime-logs/api-diagnostics.log`
   (10 MB per file, 5 files), activated by the
   `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` runtime setting: blank/unset disables
   it, and an invalid directory fails closed to disabled after one closed
   `logging_payload_rejected` line — the sink never changes the emitted
   vocabulary or the stdout stream.
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
| `exclusion_policy_denied`             | The active policy excluded the content (a policy DENIAL verdict). |
| `exclusion_policy_indeterminate`      | The evaluation could not decide (integrity); enforced as deny. |
| `exclusion_policy_not_initialized`    | Policy SYSTEM failure: no active signed policy.      |
| `exclusion_policy_signing_unavailable` | Policy SYSTEM failure: signing unavailable or corrupt. |

The four policy codes split into two classes (policy-observability
remediation 2026-08-24 C1). A **DENIAL** (`exclusion_policy_denied`,
`exclusion_policy_indeterminate`) is a completed policy decision: the
preflight answers 200 `excluded` as before and the ring entry names the
verdict. A **SYSTEM failure** (`exclusion_policy_not_initialized`,
`exclusion_policy_signing_unavailable`) means the policy itself could not
run: the preflight PROPAGATES the typed error (409 / 503 envelope per the
closed status table) instead of answering a wrong 200 `excluded`, and the
ring entry is the only small-file trace — a raise is not a completed
preflight, so no `small_file_preflight_total` outcome row exists for it.
Plugin-side, both system codes map to the retryable `server_error` wire
family with bounded backoff (the event is kept, never dropped), so a
`wire_failure · server_error` trail entry joined with a ring SYSTEM code is
the signature of a policy outage, not a content denial.

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

## Web Admin source lifecycle rejection diagnostics

The source lifecycle domain exposes the same evidence shape at
`GET /api/admin/source-lifecycle/rejections`, behind the identical strict
Web Admin session gate and envelope contract. Two shapes come back inside
the envelope's `data`:

- `commit_counters` — rows of `{operation, outcome, count}`, one per
  observed (operation, outcome) pair. `operation` is the closed lifecycle
  label `rename`, `move`, `delete` or `restore`; `outcome` is `committed`,
  `rejected` or `replayed`.
- `recent_rejections` — the bounded ring of the last 50 rejections, oldest
  first, each `{error_code, at_epoch_ms, operation}`.

`error_code` is the closed source lifecycle error registry:

| `error_code`                              | Meaning                                              |
| ----------------------------------------- | ---------------------------------------------------- |
| `source_lifecycle_input_invalid`          | The lifecycle command failed closed validation.     |
| `source_locator_missing`                  | The expected locator evidence did not match.         |
| `source_locator_conflict`                 | Another active locator occupies the target path.     |
| `source_tombstone_not_found`              | The referenced tombstone is unknown.                 |
| `source_tombstone_closed`                 | The referenced tombstone no longer accepts restores. |
| `source_lifecycle_version_conflict`       | The expected version did not match the current row.  |
| `source_lifecycle_commit_outcome_unknown` | The commit's outcome stayed unknown (retryable).     |

Sanitized example (shape only):

```json
{
  "request_id": "018f6d02-4c61-7b0e-9c1d-2e3f4a5b6c7d",
  "data": {
    "commit_counters": [
      { "operation": "delete", "outcome": "committed", "count": 2 }
    ],
    "recent_rejections": [
      { "error_code": "source_locator_conflict", "at_epoch_ms": 1784546400000, "operation": "restore" }
    ]
  },
  "error": null,
  "warnings": []
}
```

The same in-memory caveats as the sync ring apply. One write-side
caveat: the write side currently records only `replayed` outcomes and
rejections — a fresh successful commit does not yet produce a
`committed` counter row, so missing `committed` rows do not mean no
commit ever succeeded (deferred:
[`BACKLOG.md`](../handoff/BACKLOG.md), source-lifecycle row). This route
answers the server half of a typed 4xx that the plugin parks as
`blocked_conflict`/`deferred_lifecycle`: the plugin trail names the
outcome, this ring names the closed rejection reason. The Web Admin UI
renders it (2026-08-29): the `/admin/lifecycle` page shows the
lifecycle-operations card — the commit counters and the recent
rejection ring with formatted times, explicit empty states, and only the
closed `error.code` on a failed read — through the authenticated Web
Admin session, so the L1 readback no longer requires a raw endpoint
call.

## Web Admin policy evaluation diagnostics

The exclusion-policy domain exposes its evaluation evidence at
`GET /api/admin/exclusion-policy/diagnostics` (operation id
`getExclusionPolicyDiagnostics`), behind the same strict Web Admin session
gate, envelope and `Cache-Control: no-store` contract as the two rejection
routes above. Three shapes come back inside the envelope's `data`:

- `evaluation_counters` — rows of `{boundary, decision, count}`, sorted by
  boundary then decision. `decision` is the closed set
  `allowed | excluded | indeterminate | failed`: the first three are raw
  evaluation decisions, `failed` records that the policy SYSTEM failed
  before it could decide (no active signed policy, signing
  unavailable/corrupt) — the fail-closed outcome that was previously
  invisible.
- `publication_counters` — rows of `{outcome, count}`; `outcome` is the
  closed set `published | replayed | rejected`.
- `recent_failures` — the bounded ring of the last 50 policy system
  failures, oldest first, each `{boundary, error_code, at_epoch_ms}`.
  `error_code` is the closed registry enum (`exclusion_policy_not_initialized`,
  `exclusion_policy_signing_unavailable` are the codes the enforcement
  boundary records today); `boundary` is the closed boundary label
  (`sync_preflight`, `canonical_read`, …) standing in for the design's
  route-template token.

Sanitized example (shape only):

```json
{
  "request_id": "018f6d10-3c7a-7e1f-9b2c-8d4e5f6a7b8c",
  "data": {
    "evaluation_counters": [
      { "boundary": "sync_preflight", "decision": "allowed", "count": 412 },
      { "boundary": "sync_preflight", "decision": "excluded", "count": 3 },
      { "boundary": "sync_preflight", "decision": "failed", "count": 2 }
    ],
    "publication_counters": [
      { "outcome": "published", "count": 1 }
    ],
    "recent_failures": [
      { "boundary": "sync_preflight", "error_code": "exclusion_policy_signing_unavailable", "at_epoch_ms": 1784550000000 },
      { "boundary": "canonical_read", "error_code": "exclusion_policy_not_initialized", "at_epoch_ms": 1784550060000 }
    ]
  },
  "error": null,
  "warnings": []
}
```

Reading it during an incident:

- A `failed` counter row plus matching `recent_failures` entries is the
  server-side proof of a policy outage class (broken signer, missing
  policy) — join it with the small-file rejection ring's SYSTEM codes
  (above) and the plugin's `wire_failure · server_error` entries.
- Non-zero `failed` while the stack "looks idle": check the degraded-state
  table of the policy domain
  ([`exclusion-policy-publication.md`](exclusion-policy-publication.md)) —
  an invalid signer refuses startup; a missing policy answers 409 on every
  content boundary.
- `publication_counters` rows are POLICY revision publications (the
  preview/publish flow of [`exclusion-policy-publication.md`](exclusion-policy-publication.md)):
  `published` fresh committed revision, `replayed` exact replay,
  `rejected` terminal business rejection. Do not confuse them with the
  source-version publication guard failures of the 2026-08-24 remediation
  — those emit the existing `SOURCE_VERSION_PUBLISH_FAILED` event with the
  closed policy code and a terminal `rejected` outcome in the sources
  domain's own metrics, read from the structured event stream, not this
  route.

The same in-memory caveats as the other rings apply: the counters and ring
are per-process and reset on API restart; they are evidence of what this
process saw, not a durable audit trail (the audit table is the durable
half). The production metrics sink is the Prometheus text exposition route
`GET /api/admin/metrics` (sink plan 2026-08-31, operation id
`getMetricsExposition`) behind the same strict Web Admin session gate —
it renders exactly these counter families from the shared recorder
snapshot, scraped live and sanitized during the 2026-09-01 diagnostics
smoke round.

## Policy worker diagnostics (dispatch events, reconciliation reason, stale running)

Three surfaces cover the policy worker failure modes — the "preview stuck
running forever" class included:

- **Worker dispatch events.** When the preview or reconciliation dispatch
  loop swallows an unexpected start failure (the lease outcome stays
  unknown and lease expiry reclaims it), the worker emits one closed
  structured event: `preview_dispatch_unavailable` or
  `reconciliation_dispatch_unavailable`. The event carries exactly the
  opaque row id (`policy_preview_id` / `policy_reconciliation_intent_id`),
  the `attempt_count`, and the closed `exception_type` /
  `stack_fingerprint` reductions — never provider text, workflow identity
  or exception arguments. The events ride the structured logging boundary,
  so they appear in the worker's log stream and — when the worker process
  runs with its own `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` — in its rotating file
  sink. Sanitized example line (shape only):
  `preview_dispatch_unavailable · policy_preview_id=… · attempt_count=2 · exception_type=temporalio.client.workflowstartfailure · stack_fingerprint=…`.
- **Reconciliation reason.** The Admin policy status read
  (`GET /api/admin/exclusion-policy`) renders the latest reconciliation
  intent with its durable `safe_error_code` — the closed failure reason
  recorded on the row, `null` while no failure is recorded. A "failed"
  reconciliation now names its closed code instead of only its state.
- **Stale running staleness block.** The same status read carries
  `stale_running_previews`: `null` while nothing is stale, else one row
  per preview still in an executable state (`pending`, `leased`,
  `running`) whose age exceeds the domain's execution deadline (15
  minutes) — each `{policy_preview_id, reason, age_seconds}` with the
  fixed reason token `worker_stale_running`. The verdict is computed on
  read against the database clock: a row older than the bound proves no
  worker is sweeping it. The read pages at the 16 oldest rows — more
  than 16 stuck rows render as the 16 oldest, with no truncation
  marker. Nothing is restarted or scheduled by this
  surface; the operator decides. The Web Admin UI renders it (2026-08-29):
  the policy page's `PolicyStatus` component carries a **Preview worker
  health** alert block — one row per stale preview with its age in
  minutes and the fixed restart guidance — whenever
  `stale_running_previews` is non-null, so the operator reads the
  staleness verdict directly from the Admin UI instead of a raw endpoint
  call.

Sanitized example (shape only):

```json
"stale_running_previews": [
  { "policy_preview_id": "…", "reason": "worker_stale_running", "age_seconds": 4073 }
]
```

Worker file-sink note: the rotating sink is activated per process by the
`KNOWLEDGE_DIAGNOSTICS_LOG_DIR` runtime setting — and the repository
worker launcher (`.local/run-worker.sh`) now sets it automatically
(2026-08-29; machine-local launcher: `.local/` is untracked, so on a fresh
clone the operator sets the variable themselves): each worker role gets
its own diagnostics directory
(`.local/runtime-logs/worker-previews/`,
`.local/runtime-logs/worker-reconciliations/`), so the W1 dispatch
events land in durable rotating files by default on the local stack. One
directory per process remains mandatory (never a shared directory; the
sink rotates files under an exclusive lock).

## Object-storage busy reasons and CLI failure tokens

Two more closed surfaces close the remaining "silent reason" classes of the
2026-08-24 remediation round:

- **Spool busy reason tokens.** Every `object_storage_busy` envelope from
  the local upload spool now carries `safe_details.reason` with exactly one
  of three closed tokens (the key was already registry-validated; the value
  vocabulary is closed by module constants):

  | `reason` token                    | Meaning                                                       |
  | --------------------------------- | ------------------------------------------------------------- |
  | `spool_free_space`                | The spool directory's free-space reserve check failed.        |
  | `spool_permits_exhausted`         | The admission wait ended with permits/budget exhausted.       |
  | `spool_admission_window_expired`  | The admission window (the outer wait bound) elapsed without a release wake. |

  One retryable code, three distinguishable causes — read the token from
  the error envelope's `safe_details`, never from any path or message. The
  spool mechanics live in [`object-storage.md`](object-storage.md).

- **CLI exception class token.** When a `personal-api` CLI command dies on
  an unexpected internal exception, the emergency failure line prints the
  exception CLASS as a closed snake_case token after the code —
  `personal-api: internal_error: timeout_error` (exit code stays 70). The
  token is derived from the class name alone through a bounded
  alphabet/length reduction (arbitrary class names cannot smuggle content;
  an unrepresentable name collapses to `unknown_error`) — no traceback and
  no message text ever reach the line. The fix covers the authentication
  commands and the `policy-key` CLI dispatch alike (2026-08-31 sink plan,
  Task 3): every CLI failure line of this class now carries the closed
  token.

## Privacy invariants

- The sidecar, the export block, the settings section, the settings
  detail lines, the notices and the admin routes carry only closed
  tokens, counts and timestamps — no paths, hostnames, origins,
  credentials, digests, source ids, device ids or free-form strings.
- The policy diagnostics route adds only closed boundary/decision/outcome
  labels, closed registry codes and epoch-ms integers; the busy reason
  tokens and the CLI class token are closed vocabularies by construction
  (module constants; bounded alphabet reduction).
- The worker dispatch events carry only the opaque row id, the attempt
  count and the closed exception-type/stack-fingerprint reductions —
  never provider text, a workflow identity or any exception argument.
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
- Remediation contract (`docs/superpowers/specs/2026-08-24-closed-reason-surfacing-remediation-design.md`)
  — the startup/pass/policy-state/auth detail tokens, the lifecycle admin
  route, the worker dispatch events and the stale-running surface.
- Policy observability contract (`docs/superpowers/specs/2026-08-24-policy-observability-remediation-design.md`)
  — the policy SYSTEM/DENIAL split, the `failed` evaluation decision, the
  policy diagnostics admin route, the publication guard events and the
  spool busy reason tokens this guide operates.
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
- Device cursor and manifest reconciliation surfaces — the Child 6
  cursor/apply/reconcile/credential/composition kinds and stages above
  ([`device-cursor-manifest-reconciliation.md`](device-cursor-manifest-reconciliation.md)).
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

The remediation surfaces add one failure class each to the loop:

1. **Wrong-origin auth failure (settings detail tokens).** Point the
   plugin at an origin that rejects the credential, let one refresh or
   grant poll fail, then read the settings Connection detail line — it
   must name the closed transport/server token, not a bare state.
2. **Stopped policy worker (staleness line).** With the stack up and a
   preview dispatched, stop the preview worker and wait past the 15
   minute bound, then read the Admin policy status: the
   `stale_running_previews` block must carry `worker_stale_running` with
   the row's age — visible directly in the Web Admin policy page's
   Preview worker health block. Restart the worker afterwards; the rows
   converge or fail closed on their own.
3. **Lifecycle rejections (admin surface).** After any typed lifecycle 4xx
   (a locator conflict is the cheap trigger), read the lifecycle
   rejections surface — the Web Admin `/admin/lifecycle` page, or
   `GET /api/admin/source-lifecycle/rejections` behind the same session —
   and match the ring's `error_code` against the plugin trail's parked
   outcome.
4. **Policy system failure (diagnostics route + rejection ring).**
   Temporarily point the signer at a broken key (or stop the policy
   worker), drive one content operation, then read
   `GET /api/admin/exclusion-policy/diagnostics`: a `failed` evaluation
   counter row and `recent_failures` entries with the closed code must
   appear, the small-file rejection ring must carry the SYSTEM code, and
   the rotating API diagnostics log holds the typed exchange. Restore the
   signer/worker afterwards.

## Live smoke round of 2026-09-01 (operator-recorded evidence)

The first diagnostics live smoke round ran 2026-09-01 against a
disposable `knowledge-ci-*` stack with the operator's real vault as the
device under test (closed tokens, counts and timestamps only, exactly
like the examples above; the round's plan and handoff carry the full
narrative).

**Class 4 — policy system failure (all four readbacks observed).** With
the workspace active-policy pointer fault-injected to the never-published
state for a bounded window (13:45:39Z–13:49:18Z), one content create
answered the typed fail-closed denial and every surface recorded it:
`GET /api/admin/exclusion-policy/diagnostics` carried
`{boundary: single_part_upload, decision: failed, count: 1}` plus one
`recent_failures` entry `{error_code: exclusion_policy_not_initialized,
at_epoch_ms: 1788270400421}`; `GET /api/admin/sync/rejections` carried
the same closed code on `operation: create`; the rotating API diagnostics
log held the typed exchange — `api_request_rejected` with status 409 at
13:46:40.422Z whose `request_id` joins the ring's epoch exactly. The
plugin parked the probe note pending, named the reconcile stage in closed
tokens, and after the pointer restore the note converged to committed
with the `failed` counter frozen and fresh `allowed` evaluations
appearing (count 11 by round end). The sink route
`GET /api/admin/metrics` rendered the same counters in Prometheus text
format behind the session gate (scraped twice, sanitized). The
policy-observability row retired on this evidence.

**Class 2 — stopped-worker staleness (observed and converged).** With
every preview worker process dead and one preview row resting in an
executable state past the 15-minute bound, the Admin policy status read
carried exactly
`stale_running_previews: [{policy_preview_id: …, reason:
"worker_stale_running", age_seconds: 1437}]`. After the worker restart
the row converged fail-closed: the worker's first sweep failed it with
the closed code `preview_execution_deadline`, and the staleness block
returned to null. The W1 dispatch-event class was exercised live with a
stopped Temporal: the system answered with the typed retryable release
(bounded-backoff claim/release cycle, attempts visible on the row) — the
correct designed outcome for a dependency outage; the
`preview_dispatch_unavailable` event itself fires only on the
unexpected-exception path, which cannot be induced live without
simulating a fault (forbidden), so its live induction stays unobserved
with the emission pinned by the remediation's test suite.

**Class 1 — wrong-origin and terminal tokens (observed on the real
vault).** Baseline `Connection status: Connected`; pointing the plugin at
a non-resolving HTTPS origin rendered the closed transport token
(`network_unavailable`) in the settings detail line; restoring the origin
converged back to `Connected`. After an admin device revoke the terminal
case rendered `Last cleared reason: token_reuse` — a closed token of the
credential-failure vocabulary (the dead family's next refresh classifies
as reuse; the vocabulary surfaces it, never a bare state).

**Class 3 — lifecycle rejection ring (not observed; row stays open).**
The tombstone-restore locator conflict could not be produced: the round's
three device-manifest recovery-path defects (see the 2026-09-01 handoff —
the composition wiring bug fixed during the round, the queue-pass stall,
and the cursor-gap-blocked repair) each intercepted the setup sequence.
The closed-reason-surfacing row stays open with this evidence.
