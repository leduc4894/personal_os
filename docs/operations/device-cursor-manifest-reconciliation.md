# Device Cursor and Manifest Reconciliation Operations

Living operator runbook for server-to-device synchronization and device
repair: the device sync route set, cursor and manifest state, the `Repair
sync` command, recovery from an expired manifest run, a crash-interrupted
remote apply and the loss of the plugin SQLite journal. Design contract:
`docs/superpowers/specs/2026-08-26-device-cursor-and-manifest-reconciliation-design.md`.
Plugin release 0.2.0 is the feature release of this child (see the plugin
README compatibility note). The outbound small-file lane stays owned by
[`plugin-journal-small-file-sync.md`](plugin-journal-small-file-sync.md);
lifecycle writes by
[`source-locator-tombstone-lifecycle.md`](source-locator-tombstone-lifecycle.md);
the closed diagnostics vocabulary by
[`sync-error-tracing.md`](sync-error-tracing.md).

Status (2026-08-27): implementation complete and offline-verified; the Desktop
WDIO live gate **PASSED** on the disposable local stack (bootstrap verdict
`obsidian_live_acceptance_passed`, all four scenarios: remote edit + exact
no-echo, cursor gap → repair, SQLite loss without duplicate source, remote
tombstone → Obsidian local trash). The **physical Mobile matrix also RAN and
PASSED** (2026-08-27 evening session on a physical iPhone against the
disposable stack; operator-confirmed sanitized rows recorded in
[`device-sync-device-verification.md`](device-sync-device-verification.md);
`uv run poe device-sync-device-verification` exits 0). The matrix surfaced
five real physical-device findings, all recorded in the handoff with staged
or deferred fixes: the finalize-transition suspension deadlock (supersede
fix staged), the missing access-token refresh while the app stays resident,
the rebuild-without-reconcile-first create-poisoning cascade, the snapshot
echo race at initial reconcile, and the sequence-burn of retried
poisoned creates. No byte of operator data was lost or overwritten in any
of them — every failure mode failed closed. The standing no completion claim
rule held until both records existed; with both gates now passed, the
Child 6 completion claim is unlocked pending the final whole-branch review.

## The route set (operator reference)

Eight authenticated routes serve the device (every one requires the active
`obsidian_sync` device Bearer credential and derives workspace/device from
it; JSON responses carry the canonical envelope and `Cache-Control:
no-store`):

| Route | Operation id |
| --- | --- |
| `GET /api/sync/events` | `pullDeviceSyncEvents` |
| `POST /api/sync/cursor-acknowledgements` | `acknowledgeDeviceSyncCursor` |
| `POST /api/sync/manifests` | `startDeviceManifest` |
| `PUT /api/sync/manifests/{manifest_run_id}/pages/{page_number}` | `appendDeviceManifestPage` |
| `POST /api/sync/manifests/{manifest_run_id}/finalize` | `finalizeDeviceManifest` |
| `GET /api/sync/manifests/{manifest_run_id}/actions` | `listDeviceManifestActions` |
| `POST /api/sync/manifests/{manifest_run_id}/complete` | `completeDeviceManifest` |
| `GET /api/sources/{source_id}/versions/{source_version_id}/content` | `downloadDeviceSourceVersion` |

The binary download is the one documented exception to the JSON envelope: a
success streams the exact verified bytes with `Content-Length`,
`Content-Type` and `X-Content-SHA256`. Every error detected before the
stream starts is a normal JSON envelope; a mid-stream failure terminates
the transport (the plugin trusts no partial response and re-verifies digest
and size while staging and after final placement).

The closed `device_*` error registry (each code maps to exactly one tested
HTTP status; every code is terminal for the triggering request except the
dependency outage):

| Code | HTTP | Operator meaning |
| --- | --- | --- |
| `device_cursor_gap` | 409 | Retained event history cannot satisfy the cursor — the device must reconcile, not pull. |
| `device_cursor_regression` | 409 | An acknowledgement tried to move the cursor backwards; rejected, nothing changed. |
| `device_cursor_ack_ahead` | 409 | The acknowledgement exceeded the delivered watermark without the manifest exception. |
| `device_event_unavailable` | 404 | A requested event is unavailable. |
| `device_event_integrity_failed` | 409 | An event failed hydration integrity (e.g. an update whose active locator cannot be resolved at its sequence). |
| `device_manifest_not_found` | 404 | Unknown or foreign manifest run. |
| `device_manifest_expired` | 410 | The run passed its one-hour database deadline; start a new run. |
| `device_manifest_state_invalid` | 409 | The run state does not accept this action (also the fail-closed settle when a download action's locator cannot be hydrated). |
| `device_manifest_page_invalid` | 422 | Page number, order or bounds violated. |
| `device_manifest_page_replay_mismatch` | 409 | A replayed page did not match recorded evidence; the run fails closed. |
| `device_manifest_digest_mismatch` | 422 | Final digest did not match the recorded pages. |
| `device_manifest_policy_advanced` | 409 | The active policy advanced past the checkpoint; the run is invalid and a fresh run replaces it. |
| `device_download_integrity_failed` | 422 | The verified download failed digest/size verification. |
| `device_sync_dependency_unavailable` | 503 | Retryable dependency outage — the only retryable code; the plugin backs off and retries with the same identity. |

Planner/apply blockers are not route exceptions: the closed action-reason
vocabulary (`device_manifest_identity_ambiguous`,
`device_manifest_local_diverged`, `device_manifest_target_occupied`,
`device_manifest_action_stale`, `device_manifest_policy_excluded`) settles
individual actions as conflict/excluded evidence and never fails the whole
manifest request.

## Cadence, cursors and the terminal-safe rule

The plugin coordinator runs one mutating phase at a time (outbound, inbound,
repair never overlap) with this cadence:

- Cursor pull: after safe startup/recovery, onboarding or app resume, after
  a canonical local commit, and every 30 seconds while the plugin is
  foreground-active.
- Full reconciliation: after onboarding without a trusted mapping, after
  SQLite rebuild/fallback, cursor gap, compacted history, unknown event
  shape or local invariant mismatch, by the explicit `Repair sync` command,
  and every six hours of accumulated foreground-active time (suspended time
  accumulates nothing).
- Offline/timeout/429/temporary outage: cancellable jittered exponential
  backoff from one second to five minutes; the backoff pauses the pull tick
  and the first success re-anchors the cadence.
- A manifest run expires after one hour of database time OR five minutes
  without client activity (every start, page append, finalize and action
  read refreshes the activity anchor), whichever comes first — a device
  whose app was suspended or killed mid-run waits minutes, not the full
  hour; an idle gap of one hour or more with an active run discards
  its local progress before the resume cycle, and resume then starts a new
  run — never fake success. A replayed finalize (planned or applying)
  whose digest matches the recorded evidence replays idempotently.

Documented behavior note (accepted): after a long suspension the
catch-up-anchored cadence fires a bounded catch-up burst — each stale tick
credits one 30-second interval toward the six-hour accumulator. The result
is at worst a spurious periodic-reconcile opportunity that no-ops when
nothing is owed; fake-clock tests pin the exact behavior.

Cursor advancement is the correctness spine:

- The local cursor advances only in the same SQLite generation commit that
  records a terminal-safe outcome — applied, proven self-origin no-op,
  durable conflict, handled tombstone or excluded. It never advances for a
  retryable failure, a prepared operation or a swallowed exception.
- The server cursor advances only through `acknowledgeDeviceSyncCursor`
  after that local generation is verified (a lost acknowledgement is
  retried before the next pull). The manifest-completion transaction is the
  sole exception: the same transaction that moves one exact `applying` run
  to `completed` may advance the cursor to that run's checkpoint without a
  prior delivered watermark. No other path — completed, failed, expired or
  foreign run — grants an advance.
- An event whose evidence does not prove self-origin is never suppressed:
  origin device id alone suppresses nothing; the full apply machine runs
  and an idempotent byte-identical rewrite is the worst outcome.

## Status readback (what the operator sees)

- Settings → "Device sync" section: the closed `repairState`
  (`ready | required | running | blocked`), cursor lag, pending action
  count and reason tokens. Cursor lag is `max(applied, checkpoint) -
  acknowledged`, floored at zero: a persistently growing lag with no
  `blocked` reason means acknowledgements are not landing — read the trail.
- The `Copy sync diagnostics` export carries one `Device sync: …` line with
  the same closed projection.
- `reconcile_required` guidance in the journal status names the
  `Repair sync` command.

## `Repair sync` (the explicit repair command)

The command palette's **Repair sync** triggers an explicit reconciliation
cycle through the same coordinator (never a parallel path). Starting a run
persists a local barrier generation `G`, pauses outbound dispatch (watcher
capture continues; newer observations get generations greater than `G`),
enumerates the manifest in normalized-locator order and ships it as
contiguous pages (at most 500 entries each, 100,000 entries per run). The
server freezes one event checkpoint and one policy revision; a policy
advance mid-run invalidates the run (`device_manifest_policy_advanced`)
and a fresh run replaces it. A start carrying a newer client observation
generation (an explicit repair after an interrupted run, or a rebuilt
journal) likewise supersedes the abandoned unfinished run — expired with
its evidence retained — instead of dead-locking the device until the
one-hour deadline.

After every action is terminal-safe locally, completion marks the run
completed, advances the server cursor to the checkpoint (the sole
exception above), durably records local cursor `C`, clears
`reconcile_required` (`markReconcileComplete`) and the barrier, then
releases the outbound rows captured after `G`. A lost completion response
is resolved by exact replay. No Vault edit observed during the run is
discarded or rewritten — a stale action becomes a durable conflict, never
an overwrite.

Operators never repair generation files, edit manifest rows or extend
deadlines; every recovery below is automatic or command-driven.

## Identity proof after SQLite loss

Losing the SQLite journal (or starting a fresh device) rebuilds mappings
through manifest reconciliation — the plugin never mints or guesses a
source identity, and local bytes never silently overwrite canonical bytes.
For a manifest entry without a source ID the backend evaluates, in order:

1. an exact **current locator** match proves identity (bytes may still
   differ → `conflict`, never auto-upload/download);
2. one unique **historical locator** plus the exact current canonical
   fingerprint proves a remotely renamed/moved source;
3. one **open tombstone** whose retained locator and retained version
   fingerprint both match proves the deleted source;
4. **hash-only** matching, multiple candidates, a historical locator
   without exact fingerprint, a closed tombstone or missing evidence
   proves nothing — hash-only evidence never binds identity.

An entry that supplies a source ID must prove it belongs to the
credential workspace; invalid or cross-workspace evidence fails closed and
does not fall back to locator matching.

## Remote apply, crash recovery and local trash

Remote apply is a durable state machine
(`prepared → temp_verified → vault_mutated → locally_applied →
server_acknowledged`) holding only local correctness evidence — no bytes,
credential, object key or provider response. The writer stages a
same-directory sibling, verifies size/hash, then performs the narrow
atomic replacement and verifies the final hash; where the adapter cannot
replace in one rename it retains an exact rollback sibling first. A crash
always recovers verified old or new bytes — never a partially trusted
file. Recovery runs before any network phase; the design's recovery table
(exact temp removal, resume of a verified temp, finalize without reapply,
rollback restore, preserve-and-require-repair, retry of a lost
acknowledgement) is implemented and integration-tested.

Remote delete moves a proven unchanged file to Obsidian local trash via
`Vault.trash(file, false)` — Desktop and Mobile both use local trash, and
there is no hard-delete fallback anywhere. Only after the trash operation
is durable does the mapping become tombstoned and the cursor advance. A
changed file, missing proof or trash failure preserves bytes and surfaces
a closed apply reason.

Remote restore requires the exact tombstone/source lineage and an
available target; it can rebind proven retained bytes or download the
retained current version through the verified apply path, and never
consumes a different/closed tombstone or an existing `restore_pending`
reservation.

Every inbound action is rechecked against the currently verified policy
before bytes move; excluded/indeterminate content is not downloaded, local
bytes are preserved, and the closed excluded state still permits cursor
progress.

## Closed diagnostics (trail v2 vocabulary)

The diagnostics sidecar writes `obsidian_sync_diagnostics_trail/v2`; a v1
sidecar is migrated losslessly (a foreign token still resets through the
closed `trail_reset` behavior). The device-sync failure kinds and their
closed stage vocabularies are exactly:

```text
cursor_failure              pull | acknowledge
apply_failure               prepare | download | verify_temp | vault_mutation |
                            verify_final | local_commit | recovery | trash
reconcile_failure           start | page | finalize | actions | complete
credential_failure          access_missing | refresh_failed
composition_read_failure    status_read | note_status_read | retry_schedule_read |
                            sync_status_read
```

`wire_failure` now means an HTTP attempt actually reached the transport and
failed; a missing credential or a failed refresh before contact records
`credential_failure` instead. The once-per-session settings reads record
`composition_read_failure`, which is deliberately excluded from the derived
stop-reason line (it never stops sync). Every cursor/apply/reconcile closed
path appends a stage token plus a closed reason; a parsed server envelope
also appends its UUID-gated `request_id` and registered server error code.
The full vocabulary, the settings/export surfaces and the `request_id` join
live in [`sync-error-tracing.md`](sync-error-tracing.md).

## Acceptance gates and the mandatory live round

Offline (this repository, run at the release candidate commit):

```bash
uv run poe verify
uv run poe api-contract-check
uv run poe device-sync-test
pnpm --dir apps/obsidian-plugin exec vitest run
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
```

The mandatory live gates (design 18.1) are operator work on the disposable
local stack (`.local/RESTART.md`; a disposable `knowledge-ci-*` project —
never a personal Vault):

- **Desktop WDIO** (`wdio-obsidian-service`): remote edit plus exact
  no-echo; cursor gap to manifest repair; lost-SQLite identity/cursor
  recovery without duplicate sources; remote tombstone to local trash.
  Run it guarded (the closed verdict token
  `obsidian_live_acceptance_passed` comes only from the bootstrap):
  `CI=true uv run python tools/obsidian_live_acceptance_bootstrap.py
  --project-name knowledge-ci-<slug> --wdio-spec
  test/specs/device-sync-reconciliation.e2e.ts`. One fresh disposable
  project per attempt — every WDIO run re-uploads the fixture vault's
  `hello.md`, and a second run on the same workspace hits its occupied
  locator (`source_locator_conflict`), which flags the journal
  `reconcile_required` and distorts the journey.
- **Physical Mobile matrix**: manifest suspend/resume; remote apply plus
  no-echo; lost-SQLite repair; tombstone to local trash;
  edit-during-reconciliation preservation.

Mock, unit inference or Desktop evidence cannot replace the physical
Mobile matrix. Until both gates pass and the sanitized rows are recorded in
[`device-sync-device-verification.md`](device-sync-device-verification.md),
Child 6 stays open — do not claim Child 6 complete. The recorded-evidence
gate runs as `uv run poe device-sync-device-verification` and fails (never
skips) while the records are absent.

Recorded evidence must stay sanitized exactly like the trail: device class,
scenario name, observed closed status/state tokens and UTC dates only —
never file names, paths, content, digests, tokens, credentials or request
IDs.
