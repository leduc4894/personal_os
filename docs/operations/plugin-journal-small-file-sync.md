# Plugin Journal and Small-File Sync Operations Guide

Operator contract for the Obsidian plugin journal and the authenticated
small-file sync flow (`apps/obsidian-plugin/src/journal`,
`src/personal_os/small_file_sync`, the two `/api/sync` and `/api/uploads`
routes in `apps/api/src/api_runtime`, and the durable
`small_file_upload_operations` store). This guide covers startup, queue
state, safe diagnostics, recovery, the two born-terminal blocks and the
operator evidence procedure. The wire contract lives in
`tests/fixtures/small_file_sync/wire-golden.json` (one corpus, replayed by
both languages); the HTTP posture and error registry live in
`docs/operations/api-runtime-contract.md`; publication semantics live in
`docs/operations/source-publication.md`; the policy side lives in
`docs/operations/exclusion-policy-publication.md`. This guide links to them
and never duplicates their content.

## Startup

Plugin load runs strictly in this order (journal design 7.1, 8):

1. Settings restore and the bounded credential resume-or-refresh action
   (at most ONE network action; no background loop ever starts).
2. Journal recovery (`JournalPersistence.open`) — see recovery below. A
   failed recovery fails closed: no capture listener, no queue driver, and
   Vault editing is never touched.
3. Only after recovery succeeds: Vault listeners, the bounded foreground
   queue driver, the `Sync now` / `Sync existing files` commands, and one
   fire-and-forget first pass.

Nothing syncs before a credential exists: with no device credential the
status shows Login required and the journal keeps capturing locally.

## Queue state

The journal event lifecycle (`queued -> preflight -> uploading ->
committed | no_change`, `waiting_retry` re-entering `queued`) is driven by
one bounded foreground pass per trigger (plugin load, a Vault event,
`Sync now`). One pass holds at most one active content request, ends at its
deadline, before plugin unload or mobile suspension, and never recurses.
Retryable conditions (offline, timeout, rate limited, server error) return
the event to bounded jittered backoff (1 s initial, 5 min ceiling) with the
SAME event and idempotency identity, so the server's exact replay either
returns the original receipt or reopens the flow. The five terminal states
below never retry automatically. An interrupted pass needs no operator
action: the journal is the durable truth and the next trigger resumes
through ordinary eligibility.

## Safe diagnostics

The only diagnostic surfaces are the plugin status bar text and the
settings snapshot: the six closed status values (counts and closed labels
only) plus the per-blocker guidance lines. Journal records, thrown errors
and attempts carry closed safe labels and opaque correlation IDs — never a
path, digest, token, credential, provider detail or library exception. The
server side mirrors this: responses never carry a receipt, object key or
provider detail, and diagnostics follow
`docs/operations/api-runtime-contract.md`. If an operator needs more than
the status bar shows, the answer is the scenario table below — never a
log of Vault content.

## Recovery generation selection

Every committed journal transaction becomes an immutable verified
generation (`journal.sqlite.g<n>` + `journal.manifest.json`, retained
window: current plus one prior). Startup accepts only a manifest whose
named current generation verifies byte-exactly (`verified_generation_loaded`);
a torn, missing or invalid newest write falls back to the newest prior
verified generation (`prior_generation_recovered`); when nothing verifies
but artifacts exist, the journal rebuilds empty with
`reconcile_required` (`empty_journal_rebuilt`) while every Vault file stays
untouched; a first run creates `fresh_journal_created`. Operators never
repair generation files: delete nothing, edit nothing — a rebuild is
safe (Vault content is the source of truth for reconciliation) and a torn
newest generation loses at most the last transaction, which the next pass
re-derives from Vault bytes.

## `reconcile_required`

The journal durably flags `reconcile_required` when (a) the pending-event
soft cap (10,000) or journal-size soft cap (64 MiB) is reached — only NEW
per-change rows are refused; in-flight evidence is retained — (b) the
recovery path buffer overflows, or (c) nothing verified at startup. The
flag is sticky across every later verified generation. While it is set the
composition stops the queue driver (nothing syncs) and capture refuses new
rows; the status bar shows the reconcile guidance. Clearing it is a
deliberate repair action owned by the reconciliation child: it is never
auto-cleared by a successful pass.

## Size block

One regular uploaded file is capped at exactly 16 MiB (single-part
ceiling, spec 3.1). The plugin blocks at capture time with a born-terminal
`blocked_size` event (never retried), the server re-checks the declared
size at preflight (one byte over is the closed `small_file_size_limit_exceeded`
rejection before any reservation) and enforces the ceiling again over the
streamed bytes plus the exact declared size through bounded verification
before anything can publish. Oversize files are an operator-visible block
per file, not an error state of the queue.

## Policy block

Every preflight re-evaluates the active signed policy server-side with the
locator-aware subject (the plugin's local gate is a filter, never the
authority) and persists the server-returned allowed revision on the upload
operation. At publication, a matching active revision reuses that immutable
allowed binding after verifying the signed active snapshot; it does not
re-evaluate a locator-free subject. A changed revision is re-evaluated
fail-closed using the authoritative subject. A denied or indeterminate subject answers the terminal
`excluded` outcome — a born-terminal `excluded_policy` event on the
plugin, never retried, no automatic re-upload. A policy revision published
between an accepted preflight and the content stream is caught by the
publication guard: the upload fails closed (403, nothing published).
Because the plugin's closed failure table maps every 403 to
`login_required`, that pass first ends as **Login required** with the whole
queue retained — a transient status, not a credential failure (the
credential is untouched and still valid). The next preflight of the same
identity re-evaluates policy, answers `excluded`, and the event settles
terminally as `excluded_policy`; no re-login is ever needed. Operators
seeing Login required here with valid credentials should run one more
pass (or `Sync now`) and expect the `excluded_policy` settlement.
Publishing and rotating policies is covered by
`docs/operations/exclusion-policy-publication.md`; revoking a device is
covered by `docs/operations/web-authentication-and-device-authorization.md`
(a revoked device's credential answers `device_revoked` on both sync
surfaces and its reserved operations can never be continued).

## Scenario quick reference

| Scenario | Plugin outcome | Server behavior |
| --- | --- | --- |
| Offline create/update, then reconnect | events retry with bounded backoff, same identity | exact replay returns the frozen receipt or reopens the flow |
| Response lost after commit | next pass re-preflights the same identity | `committed_replay` with the frozen result, exactly one publication |
| File at exactly 16 MiB / one byte over | `blocked_size` at capture when over | accepted at the ceiling; over is rejected before reservation |
| Denied policy | born-terminal `excluded_policy` | `excluded` outcome, no reservation |
| Policy changed during upload | pass first ends Login required (transient 403 mapping, queue retained); next preflight settles `excluded_policy` | changed revision is re-evaluated fail-closed at the publication guard; nothing published |
| Stale update base | born-terminal `blocked_conflict` | `conflict` outcome, no upload |
| Local bytes changed after freeze | frozen event closes `integrity_failed`; successor syncs | digest verification sees only the successor bytes |
| Torn newest generation | `prior_generation_recovered` | none (local recovery) |
| Queue cap reached | `reconcile_required`, capture refuses new rows | none (local limit) |
| File vanished before upload | born-terminal `deferred_lifecycle` | none (no upload attempted) |
| Revoked device | pass ends login-required on 401 | `device_revoked` on both surfaces |

## Operator evidence procedure (reference devices)

Reference-device verification of this child is OPERATOR-observed work on
physical Desktop and Mobile Obsidian test Vaults, following the precedent
of `docs/operations/exclusion-policy-device-verification.md`. The tests in
this repository prove the automated half; the evidence below can only be
recorded by the operator on real devices. Until it is recorded, child-4
device evidence is PENDING (see the handoff backlog).

Procedure, per device (one Desktop and one Mobile test Vault against the
disposable local stack — never a personal Vault):

1. Prepare: fresh test Vault with the plugin build installed, a device
   credential approved for `obsidian_sync`, and an active signed policy
   revision. Use synthetic content only.
2. Run each scenario of the table above once: edit offline and reconnect,
   force a dropped response (kill network mid-upload), add a file at and
   over 16 MiB, publish a denying revision before an upload lands, edit a
   file whose base went stale from another device, corrupt nothing (skip
   recovery on device — recovery is covered by automated fixtures), fill
   no queue (covered by automated fixtures), delete a file mid-sync,
   revoke the test device.
3. Record, per scenario, ONLY: the device class (Desktop/Mobile), the
   scenario name, the observed status-bar value, the observed terminal
   state label from the settings snapshot, the UTC date, and the server
   log timestamp if one was captured.
4. Never record: file names, paths, content, digests, operation tokens,
   credentials, request IDs, or any Vault-identifying detail. Evidence is
   sanitized labels and timestamps only.
5. Store the outcome rows in the living device-verification record
   (`docs/operations/exclusion-policy-device-verification.md`, child-4
   section — replacing its pending note), following the child-3 precedent;
   the session handoff links to that record and never copies the rows.

Deferred item (operator): the child-4 reference-device evidence rows are
not yet recorded; the automated scenarios and this procedure exist and are
verified by the task-11 suites.

## Acceptance gates

The automated half of this child's acceptance re-runs with these commands
(the offline default test selection deselects `local_stack`, `r2_live` and
`device_records` markers; every count below is from the final verification
run of the implementing plan, on the single acceptance commit):

```bash
# Python domain, API runtime, contract and migration suites in one run.
uv run pytest tests/unit tests/contract -q

# Cross-boundary integration suite (offline doubles only — disposable,
# guarded infrastructure, never a personal stack).
uv run pytest tests/integration -q

# Lint and type gates (Python).
uv run poe python-lint && uv run poe python-type-check

# Plugin unit suites and the production bundle build
# (dist/ ships exactly main.js, manifest.json and sql-wasm.wasm).
pnpm --filter @workspace/obsidian-plugin test && pnpm --filter @workspace/obsidian-plugin build

# Lint and type gates (every TypeScript workspace).
pnpm --recursive run lint && pnpm --recursive run type-check

# Deterministic OpenAPI snapshot + generated-client drift check.
uv run poe api-contract-check
```

The reference-device half of acceptance is the operator evidence procedure
above; it fails — never skips — while the device rows are absent, and no
automated gate substitutes for it.
