# Source conflict resolution operations

This guide is the operator playbook for the Child 8 Conflict Inbox: how
concurrent and stale edits become durable, user-resolved conflicts, which
user actions are safe, how to read the plugin's redacted reason tokens
when a resolution's local apply is stuck, and the exact Desktop live
journey an operator runs against a disposable CI project. It covers the
whole conflict lane across the plugin and the canonical backend.

Related runbooks:

- The journal capture and queue lanes that detect a stale base live in
  [`plugin-journal-small-file-sync.md`](plugin-journal-small-file-sync.md)
  and
  [`source-locator-tombstone-lifecycle.md`](source-locator-tombstone-lifecycle.md).
- The sync-error-tracing trail every conflict token lands in lives in
  [`sync-error-tracing.md`](sync-error-tracing.md).
- Live setup details (launcher, stack secrets, restart sequence) live at
  [`.local/RESTART.md`](../../.local/RESTART.md) — never copy them here.

## What a conflict is and what the server guarantees

A conflict is captured server-side when a local write can no longer be
applied cleanly: a stale base (`stale_content`), a local edit of a source
the server deleted (`edit_remote_delete`), a local deletion racing a
remote edit (`delete_remote_edit`), or a rename/move/restore onto a
contended path (`locator_collision`). The capture retains the losing
side's verified bytes as immutable evidence — it never publishes, never
moves the canonical current pointer, and never silently overwrites
anything.

The guarantees the operator relies on:

- Exactly one winner. Two racing resolutions converge to one published
  version; the loser is closed by a typed verdict, never a second winner.
- Evidence is immutable. Base, observed remote and candidate bytes stay
  downloadable for the conflict's whole life, including after resolution.
- Server commits first. A resolution commits canonically before any Vault
  write; a failed local apply never rolls the canonical result back — it
  becomes a durable local repair (`local_apply_pending`).
- Everything surfaced is a closed token or an opaque identity. No path,
  content, digest, object key, URL or credential is ever rendered on any
  surface named in this runbook.

## The capture lane (what the device does)

A journal event whose preflight answers `conflict` with a capture grant
uploads its still-frozen bytes through the dedicated conflict-candidate
route (`PUT /api/uploads/{operation_id}/conflict-content` — never the
publication route). The event then parks terminal as `blocked_conflict`:
the conflict now lives server-side and the Inbox lists it. A capture
upload failure never terminalizes as a network failure — the event keeps
its retry eligibility and the next preflight answers the conflict again;
the server's capture replays idempotently by event identity. Bytes that
vanished or changed locally cannot become evidence; the event parks the
same terminal way and the newer bytes belong to the successor event the
watcher already recorded.

## Safe user actions

| Action | Safe? | Effect |
| --- | --- | --- |
| Open the Conflict Inbox (`open-conflict-inbox` command) | Always safe | Read-only listing; refreshes from the server. |
| Keep remote | Safe | Accepts the reviewed remote state; publishes nothing. |
| Keep local | Safe | Publishes exactly one version from the retained candidate bytes. |
| Edit the merge draft, then Save merged | Safe | The draft becomes a verified object through the candidate upload; the resolve carries only its opaque reference. |
| Discard the merge draft (Back / Discard / close) | Safe | The draft was ephemeral memory only; nothing was persisted. |
| Retry a pending local apply (restart or focus the app) | Safe | Local application only; it never issues another resolution. |

What the Inbox offers per conflict shape (the server decides; the plugin
renders exactly these):

| Conflict shape | Offered choices |
| --- | --- |
| Markdown candidate (`stale_content`, content-bearing `locator_collision`) | Keep remote, Keep local, Save merged |
| Binary or plain-text candidate | Keep remote, Keep local (no merge editor) |
| `edit_remote_delete` (remote deleted the source) | Keep remote only |
| Byteless `delete_remote_edit` / `locator_collision` | Keep remote only |
| Any resolved/superseded conflict | none (closed) |

Never instructed, never needed: editing files to "fix" a conflict, manual
database writes, deleting journal rows. The Vault is the working copy; the
canonical state and the Inbox own the resolution.

**Deviation ruling — binary safe-info panel (spec 5.2.2).** The spec names
"safe name/media type/size/hash information" for the binary detail. The
shipped detail surface carries the closed kind labels and the media type
the evidence read verifies, but no size or digest members: the Task 6 wire
contract deliberately renders only opaque identifiers and closed labels,
and reopening it for non-identifying metadata did not meet the Task 10
bar. The binary journey shows the two whole-object choices with no editor
(pinned by the plugin E2E); the size and hash of the exact bytes can be
recomputed locally by the operator from the verified evidence download if
ever needed. Recorded as a standing deviation, not a defect.

## Reading `local_apply_pending` and the reason tokens

When a resolution commits canonically but its Vault apply fails, the
plugin parks a durable no-byte repair row and shows the closed status
line `... · Conflict apply pending (N)`. The row keeps its safe reason;
the retry loop re-applies the parked winner at startup and on foreground
resume — local application only, never another resolution.

Read the reason tokens through the existing diagnostics surface
(`Copy sync diagnostics`): every conflict failure lands in the trail as a
`conflict_failure` entry carrying only closed tokens and an ISO timestamp.

| Token | Meaning | Operator path |
| --- | --- | --- |
| `resolution_committed` | The canonical resolution committed; apply still owed. | None — the bounded retry owns it. |
| `winner_download` (`conflict_winner_download_failed`) | The winner bytes could not be fetched or the local mapping is unknown. | Retry surfaces it again; if it persists, re-open the conflict's evidence read and retry once online. |
| `vault_apply` (`conflict_vault_apply_failed`) | The atomic Vault write (stage/verify/replace or trash) failed. | Check free space and file locks; the retry is safe and idempotent. |
| `conflict_apply_retry_exhausted` | The parked row reached the attempt cap. | Human path below. |
| `conflict_apply_retry_failed` | The fire-and-forget retry surface itself rejected (e.g. the repair read threw). | Read the trail's neighboring closed store reason; the next startup/resume retry re-enters. |
| `conflict_repair_store_failed` | A repair-store mutation threw out of a modal-facing command. | The trail entry carries the closed store reason context; reopen the Inbox and repeat the choice once. |
| `conflict_echo_marker_failed` | The best-effort echo-marker cleanup after a failed apply refused. | Benign: the marker cannot suppress a later real observation; no action. |
| `conflict_candidate_upload_failed` | The merged draft's verified upload failed. | Repeat Save merged while online; nothing was resolved yet. |
| `conflict_choice_unavailable` / `conflict_evidence_unavailable` / `conflict_media_unsupported` / `conflict_text_undecodable` / `conflict_merge_bound_exceeded` | The read path closed with its closed verdict. | Refresh the Inbox; for a merge-bound conflict resolve keep remote/keep local instead. |

### The attempt-capped parked row (human path)

When `conflict_apply_retry_exhausted` lands, the owed apply stops retrying
automatically — it needs a human decision, it is never silently dropped:

1. The status line keeps showing the pending count (a capped row projects
   exactly like a due row, so it stays visible).
2. Open `Copy sync diagnostics` and read the last `conflict_failure`
   tokens of that conflict (closed tokens only).
3. The winner is already canonical. The operator applies it by hand the
   ordinary way: re-open the note from the Inbox evidence (the verified
   read delivers the exact winning bytes) and save it locally, or accept
   the tombstone by deleting the local note for a tombstone outcome.
4. Record the outcome as sanitized evidence (outcome, reason token,
   count, timestamp — never content).

### The sourceless `locator_collision` (parked forever)

A `locator_collision` captured before a canonical source was identified
has `sourceId: null`. Its keep_remote winner cannot be applied locally
because no local mapping exists: the applier fails closed at
`winner_download` (`conflict_winner_download_failed`) and the parked row
stays visible indefinitely by design. This is not a defect: the conflict
is already resolved canonically, the Inbox no longer lists it, and the
row exists only so the owed apply is never forgotten. The operator path
is the same human path as the capped row — or leaving the row in place;
it is inert and privacy-clean.

### The benign retry double trail entry

A retry pass that re-parks an already-parked failure can append a second
`conflict_failure` entry with the same token before the row completes.
This duplication is benign (the trail is append-only evidence, not a
counter); the authoritative state is the parked row itself. Do not file
it as a defect.

## Desktop live journey (operator checklist)

The manual Desktop Conflict Inbox journey runs against a disposable
`knowledge-ci-*` project — never the daily personal Vault. Codex prepares
the CI project and fixtures (bootstrap, TOTP, policy publication per the
`.local/` runbook contracts); the operator performs the steps below in a
dedicated test Vault connected to that project, and records only
sanitized evidence: outcome, reason token, count, timestamp.

1. **Fixture (Codex).** One controlled Markdown note is published to the
   CI project from a first device/session. Expected state: one canonical
   source, one version.
2. **Create the race.** While the test Vault is offline (or before it
   pulls), edit the same note remotely (web/second session) AND locally
   in the test Vault. Bring the Vault online and let the pass run.
   Expected state: the journal parks the local event `blocked_conflict`;
   the Inbox lists one `Content conflict`.
3. **Open the Inbox.** Run `open-conflict-inbox`. Expected: one entry,
   three choices for a Markdown candidate (Keep remote / Keep local /
   the merge path).
4. **Build the merge.** Open the conflict and request the proposal.
   Expected: a bounded three-way editor with both sides' intent; the
   draft is ephemeral.
5. **Resolve Save merged.** Edit the draft and save. Expected: the modal
   reports the resolved-and-applied sentence; the note content becomes
   the merged result; the Inbox is empty. Evidence (sanitized): outcome
   sentence, zero open-conflict count, timestamp.
6. **Binary choice check.** With a seeded binary conflict, the Inbox
   shows exactly Keep remote / Keep local and no editor. Expected: the
   chosen winner's bytes land in the Vault; the loser stays downloadable
   as evidence.
7. **Checkpoint verification (Codex).** The API shows the conflict
   resolved, exactly one winning version published per resolved source,
   and no open conflicts. Evidence: counts and outcome only.
8. **Teardown.** `bash .local/serve-live-ci.sh down` after the operator
   evidence and checkpoint verification complete; `knowledge-local`
   stays down.

## Mobile acceptance gate

The physical Mobile/operations acceptance matrix is NOT covered by this
runbook and is not marked passed by any Child 8 gate. It remains the
existing Child 9 backlog gate (see `docs/handoff/BACKLOG.md`, the
mobile-live rows pointing at their source handoffs).

## Excluded scope (unchanged)

Candidate-object garbage collection, a Web Admin conflict editor,
semantic merge, and the indexed delete-and-recreate cursor-gap remediation
remain out of scope for the conflict lane by the plan's explicit
exclusions; no operator action in this runbook depends on them.
