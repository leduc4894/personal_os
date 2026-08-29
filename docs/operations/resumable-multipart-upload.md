# Resumable multipart upload operations

This guide is the operator playbook for the resumable multipart upload
transport (Child 7): files strictly above 16 MiB and at most 100 MiB move
through a server-owned multipart session (8 MiB ordinary parts, at most 13
parts, 10-minute part URLs, 24-hour session lifetime) with server-side
full-object verification before anything publishes. It covers how to read
the closed reason surfaces, the five recovery procedures, and the safe
live acceptance procedure — including the physical Mobile gate.

Boundaries worth restating for every procedure below:

- Cleanup is always the **exact staging key** of one session plus its
  exact provider upload identity. There is no listing, no wildcard and no
  prefix-based deletion anywhere in production or test cleanup, and no
  cleanup path may address a canonical object.
- No presigned URL, URL signature, provider upload ID, provider ETag,
  staging key, full digest or Vault path ever appears on a trail, in
  settings, in a log line, in a metric label or in an exported block.
- Local content is never modified by any failure path below; the journal
  keeps the frozen event and the operator's next save is a new event.

## Where the closed reasons surface

- **Plugin trail** (`sync-diagnostics-trail.json` sidecar, 128-entry ring,
  rendered in settings and by `Copy sync diagnostics`): entries of kind
  `multipart_failure` carry exactly one stage token (`multipart_resume`,
  `multipart_verify`, `multipart_cleanup`) plus the closed reason token of
  the failure.
- **Plugin settings**: the journal status histogram carries the terminal
  event states (`excluded_policy` for policy denials, `integrity_failed`
  under label `multipart_local_content_changed` for changed local files);
  the composition projection carries the closed multipart session-state
  counts and safe-reason codes.
- **Server rejection ring and structured log**: `multipart_upload_rejected`
  events on the rotating structured log carry the closed stage and
  `error_code` tokens; committed-session cleanup failures surface here.
- **PostgreSQL** (`knowledge.multipart_uploads`): `state`, `cleanup_state`,
  `cleanup_reason_code` and `result_kind` columns hold the same closed
  vocabulary (`created`, `uploading`, `completing`, `verifying`,
  `promoting`, `committed`, `integrity_failed`, `policy_denied`,
  `expired`, `cancelling`, `cleanup_pending`, `cleaned`;
  `cleanup_state` in `none`/`pending`/`running`/`failed`/`succeeded`).

The twelve closed reason tokens and their meaning:

| Token | Meaning | Terminal for the session |
| --- | --- | --- |
| `multipart_session_not_found` | The session ID is unknown or foreign to this device. | Yes (client clears progress and re-preflights) |
| `multipart_session_expired` | The 24-hour session lifetime passed. | Yes (same recovery as not-found) |
| `multipart_session_state_invalid` | The session can no longer accept this operation (wrong state or fenced claim). | Yes, except pending-completion replays |
| `multipart_part_invalid` | Part number or declared geometry outside the frozen plan. | No (client bug; correct the caller) |
| `multipart_part_url_rejected` | A part URL was refused twice (stale signature or revoked authorization). | No (retryable after status reconciliation) |
| `multipart_provider_state_invalid` | The provider upload no longer matches the recorded geometry (absent parts at completion). | Yes (`integrity_failed`) |
| `multipart_completion_in_progress` | Another claimant holds the completion lease. | No (replay through status) |
| `multipart_integrity_failed` | Full-object verification rejected the staging bytes (size, digest or media type). | Yes (`integrity_failed`, nothing published) |
| `multipart_policy_denied` | The active exclusion policy denies the locator at creation, part-URL issuance, completion or publication. | Yes (`policy_denied`) |
| `multipart_cleanup_failed` | The exact staging cleanup could not finish and will be retried. | No (bounded retry, see below) |
| `multipart_local_content_changed` | The local file changed under the frozen fingerprint (client-side verdict). | Yes for the old event; the newer save is its own event |
| `multipart_dependency_unavailable` | The object storage dependency is unavailable; the durable obligation stays. | No (bounded retry, see below) |

## Recovery procedures

### Safe resume

Symptoms: Desktop/Mobile restart, suspend, offline or timeout mid-upload;
the journal shows one pending event and the durable multipart progress
holds completed part numbers. Recovery is automatic and needs no operator
action: on the next foreground pass the client calls session **status
first** — never a part URL — reconciles the provider-observed completed
parts with its durable set, and transmits only unfinished ranges. The
provider-observed set wins; recorded completed parts are never
retransmitted. Do not delete the journal or the plugin data; a resume that
seems stuck is a credential or policy question (see Re-auth below), not a
progress question.

### Expiry

Symptoms: `multipart_session_expired` (or status answering expired/cleaned
after more than 24 hours). The client clears its durable progress and
re-preflights the same frozen event; the server's exact-replay creates one
fresh session, because one journal operation can only ever bind a new
session after the old one is terminal. The expiry sweep (Temporal
`MultipartCleanupWorkflow`, worker command `run-multipart-cleanup`) aborts
the provider upload and deletes the exact staging key of each expired
session — nothing else. No operator action is required; if expired rows
linger in `cleanup_state = 'pending'`, check that the cleanup worker is
running before anything else.

### Local content change

Symptoms: trail entry `multipart_failure` with stage `multipart_verify`
and reason `multipart_local_content_changed`; the old event terminalizes
as `integrity_failed` under that closed label while the newer save becomes
its own journal event. The client keeps the already observed progress,
requests the exact abort when online (best effort — an offline abort never
blocks the verdict) and never mixes bytes of two file generations. The
operator does nothing: the newer event uploads separately under its own
fingerprint.

### Cleanup failure

Symptoms: `multipart_cleanup_failed` or `multipart_dependency_unavailable`
on the server rejection ring or the session row (`cleanup_state =
'failed'`, `cleanup_reason_code` set, `cleanup_next_retry_at` bounded).
The obligation is durable: each retry uses only that session's exact
staging key and provider upload identity, with a bounded backoff. Never
"help" a failed cleanup by deleting anything but that exact staging key —
in particular, no bucket listing and no prefix-based deletion exists as an
operator procedure. If retries stay failed, the dependency outage runbook
(`docs/operations/object-storage.md`) applies; the staging key stays
addressable from the session row.

### Re-auth

Symptoms: `Login required` status, credential-failure trail entries
(`access_missing` / `refresh_failed`), or 401s during a multipart pass.
The queue consumes its one-per-pass credential refresh automatically; when
the refresh credential itself is spent, the plugin parks at `Login
required` and the operator re-authorizes through the browser device grant
(the same Web flow as onboarding). Progress is untouched by
re-authorization: the durable multipart progress and the server session
survive, and the next foreground pass resumes through status.

## Live acceptance procedure

The mandatory live acceptance round runs the Desktop journey and the
physical Mobile matrix against ONE disposable project, in this order
(AGENTS.md contract; live setup details live at
[`.local/RESTART.md`](../../.local/RESTART.md)):

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-multipart-live
CI=true uv run python tools/obsidian_live_acceptance_bootstrap.py \
  --project-name knowledge-ci-multipart-live \
  --wdio-spec test/specs/multipart-upload.e2e.ts
bash .local/serve-live-ci.sh down
```

The bootstrap owns migrations, identity, TOTP activation (enrolling through
the approved Web HTTP flow when no active credential exists) and policy
publication before it launches the guarded WDIO run; its terminal status
document is the closed verdict (`obsidian_live_acceptance_passed`). The
Desktop journey (`test/specs/multipart-upload.e2e.ts`) proves, against the
real server and real R2 staging: an interrupted >16 MiB upload resumes
from durable progress and commits exactly one publication; a corrupt final
part is refused with `multipart_integrity_failed` and nothing publishes; a
lost completion acknowledgement resolves through status and an exact
replay that returns the same frozen result with exactly one version; and a
mid-transfer policy advance terminalizes under `multipart_policy_denied`
with zero publications and a durably recorded exact cleanup obligation.

### The physical Mobile gate

Desktop evidence — mock, unit or WDIO — never substitutes for the
physical Mobile matrix. A real phone (Obsidian Mobile on the physical
device, the same disposable project's tunnel origin) runs these rows and
the operator records them in the sanitized evidence format below:

| Row | Procedure | Pass condition |
| --- | --- | --- |
| M1 Mobile upload >16 MiB | Onboard the device; add a >16 MiB sanitized fixture (deterministic seed pattern, never personal content) | Journal commits one receipt; server shows exactly one publication for the declared digest |
| M2 Suspension/resume | Background the app mid-upload (screen lock), return to foreground | The same session resumes through status; recorded completed parts are not retransmitted; one publication |
| M3 Two-part concurrency cap | During M1/M2, export `Copy sync diagnostics` before completion | Trail and settings carry no failure tokens; the transfer respects the Mobile class (at most two part PUTs in flight — proven through the diagnostics surfaces, never raw transfer captures) |
| M4 Durable progress privacy | After M2, export `Copy sync diagnostics` | Export carries only closed tokens; no URL, signature, staging key, digest or path |

Evidence for every row is recorded in the same sanitized format the
Desktop journey uses: closed tokens, booleans and fixture-scoped counts
only (committed counts, session counts, part-row counts, trail token
counts). Never record the fixture bytes, a presigned URL, a signature, a
staging key, a full digest, a Vault path or any tunnel hostname. The
physical Mobile matrix is PENDING until a physical device run records all
four rows; a pending row is reported as pending, never as passed.
