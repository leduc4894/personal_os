# Handoff — Device Cursor and Manifest Reconciliation (Child 6, Final)

Final handoff of
`docs/superpowers/plans/2026-08-26-device-cursor-and-manifest-reconciliation.md`.
Tasks 1-14 landed the implementation, cross-boundary tests, canonical docs and
the 0.2.0 release candidate. Task 15 ran the mandatory Desktop WDIO live gate
for the first time and left it **BLOCKED on one remaining plugin-side defect**
(two other real transport defects it exposed are fixed and tested in this
commit). The physical Mobile matrix did not run (operator unavailable). Child 6
is NOT complete; no completion claim is made.

## Commit accounting

Branch `device-cursor-manifest-reconciliation` (from `master` @ `41ab718`).

Last implementation commit before Task 15: `cd2a301` (Task 14, release
candidate 0.2.0). The commit that carries this handoff is Task 15's own
commit and therefore its immediate successor; by the plan's convention (used
by the mid-plan handoff for `05a1c54`) this file records `cd2a301` as the
last pre-Task-15 SHA and the Task 15 commit as the final acceptance commit
whose exact SHA is `git rev-parse HEAD` at commit time — stated here rather
than inventing a future SHA.

Task 15's commit carries: the Desktop WDIO journey
(`apps/obsidian-plugin/test/specs/device-sync-reconciliation.e2e.ts`), its
phase codes (`test/support/live-acceptance-phase-status.ts`), the guarded
bootstrap's new spec choice (`tools/obsidian_live_acceptance_bootstrap.py`),
two live-gate transport fixes with tests
(`apps/obsidian-plugin/src/device-sync/api.ts`,
`apps/api/src/api_runtime/device_sync_routes.py` +
`tests/unit/api_runtime/test_device_sync_routes.py`), the reference-device
records template and its structural contract test
(`docs/operations/device-sync-device-verification.md`,
`tests/contract/device_sync/test_reference_device_records.py`), the runbook
status, the docs/15 §7 accumulator sentence, `wdio.conf.mts`, the BACKLOG row
and this handoff.

Earlier task commits are tabulated in git history `4bf367b..cd2a301` (18
implementation commits + Task 11b/12b dispatches); the per-task review record
lives in `.superpowers/sdd/2026-08-26-device-cursor-and-manifest-reconciliation/progress.md`
(git-ignored scratch).

## Offline gates (all green at this commit)

| Gate | Command | Evidence |
|---|---|---|
| Full Python + web | `uv run poe verify` | exit 0 (505 files formatted, ruff/mypy strict/eslint/tsc/vitest/build/web build) |
| API contract | `uv run poe api-contract-check` | exit 0 (openapi-typescript clean) |
| Device-sync suite | `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-task15-verify uv run poe device-sync-test` | exit 0: 1590 passed, 2 skipped, 1 deselected (`device_records` by design), 775 s |
| Plugin unit | `pnpm --dir apps/obsidian-plugin exec vitest run` | 1140 passed (55 files) |
| Plugin types/lint/build | `tsc --noEmit` / `run lint` / `run build` | all exit 0 |
| Records gate | `uv run poe device-sync-device-verification` | exit 1 **by design**: both sections exist with every required label `pending`; "only observed outcomes satisfy the gate". This is the correct, documented state until a human records physical evidence. |
| Whitespace/tree | `git diff --check` / `git status --short` | clean; only intended files |

The two live-gate fixes are pinned by the suites above: the device-sync
client tests (`src/device-sync/api.test.ts`, 57 tests) and the route header
test (`test_download_streams_verified_bytes_with_exact_headers` now asserts
`cache-control: no-store, no-transform`).

## Live gates (Desktop WDIO BLOCKED)

Procedure: `.local/RESTART.md` order followed throughout; `knowledge-local`
stood down (volumes kept) before each disposable provision and restored
`"state":"ready"` at the end; nine disposable `knowledge-ci-*` projects were
created and fully reset (no containers/volumes remain); Web Admin :38000,
both policy workers, and the existing tunnel `knowledge-api-verify` ran each
round; TOTP enrolled/activated through the approved Web HTTP flow by the
guarded bootstrap on every fresh project; policy published through
`.local/publish-policy-revision.py`. No secrets were printed; only the
runbook's own hostnames appear here.

### What the gate proved before blocking

- The guarded bootstrap chain works end to end for the new spec: stack
  preflight, migrations, identity, web credential, policy key, TOTP
  enrollment + activation, policy publication, WDIO launch, phase-status
  plumbing (`device_sync_scenario_started` → `device_sync_onboarding_completed`).
- Desktop onboarding, the automatic snapshot upload, the fixture create
  upload, the remote second-device actor (grant → approve → poll → update
  preflight/upload), the event pull, self-origin echo suppression and the
  cursor acknowledgement all worked on the real wire (server log: pulls 200,
  acks 200, canonical events 1-3 committed, cursor acked=2/delivered=3).

### Defect 1 (FIXED): device-sync JSON bodies lacked `content-type`

Every `POST /api/sync/cursor-acknowledgements` (and every other JSON body on
the device-sync client) was 422-rejected by request validation before any
handler ran — the client set only `accept`. Fixed in
`apps/obsidian-plugin/src/device-sync/api.ts` (`jsonRequest` now sends
`content-type: application/json` for carrying requests, exactly like the
journal lane). Live-verified: acknowledgements returned 200 afterwards.

### Defect 2 (FIXED): the verified byte stream was transformable

The public HTTPS origin's edge (Cloudflare tunnel) re-encoded `text/plain`
payload responses to `content-encoding: gzip` and DROPPED the explicit
`Content-Length`; the client's exact size verification then correctly failed.
Proven by a sanitized credential-scoped probe (local: length 77, no encoding,
digest match; tunnel before fix: encoding gzip, length header absent, digest
match after decompression; tunnel after fix: identical to local). Fixed in
`apps/api/src/api_runtime/device_sync_routes.py` — the download response now
carries `Cache-Control: no-store, no-transform`.

### Defect 3 (OPEN — the blocker): plugin-side download integrity + recovery dead end

After both fixes, the remote-edit apply still fails identically on every
fresh workspace (three clean reproductions: `task15f`, `task15g`, `task15h`):

- Plugin trail (sanitized, from the journey's failure dump):
  `apply_failure:download` → `apply_failure:device_download_integrity_failed`
  once, then `apply_failure:recovery` →
  `apply_failure:device_apply_recovery_ambiguous` repeating on every cycle.
- Server side: the download itself returns 200 with exact headers (probe
  above); the desktop cursor ends `acknowledged=2, delivered=3` — events 1-2
  (both creates, self-origin suppressed) settled, event 3 (the remote update)
  delivered but never applied.
- Bootstrap closed verdict: `obsidian_wdio_failed_after_device_sync_onboarding`
  (phase `device_sync_onboarding_completed`).

So the failure is inside the plugin's `requestUrl`-based
`DeviceSyncHttpTransport` integrity inputs (`arrayBuffer`/header shape on the
real Electron wire) or just after them — the offline journeys cannot see it
because their fixtures stub the transport. Additionally, the failed download
leaves the apply row `prepared` and every subsequent recovery reports
`device_apply_recovery_ambiguous` forever: the device never self-heals and no
amount of cadence retries or explicit repairs clears it (the whole drain
keeps cycling recovery). Both facets need a fix before the gate can pass.

### Mobile matrix

Not run — the operator (human) was unavailable in this session, and per
AGENTS.md/18.1 Mobile acceptance may defer but never be inferred from
Desktop evidence. Exactly one BACKLOG row added (see verdicts below). No
Mobile row in `device-sync-device-verification.md` is marked passed.

## Spec-interpretation decisions (with rationale)

1. **`plugin_version: "0.1.0"` in the journey's remote actor** — the local
   launcher's auth gate pins the accepted plugin window
   (`KNOWLEDGE_AUTH_MIN/MAX_PLUGIN_VERSION=0.1.0`); the desktop onboarding
   helper sends the same value. Sending 0.2.0 is a closed 426.
2. **Local policy rule shape** — `.local/publish-policy-revision.py` (local,
   never committed) now publishes a `media_type` family deny (`image/*`)
   instead of `extension .tmp`. Rationale: the verified download authorizes
   exact bytes with NO locator operand by design
   (`device_content_catalog.evaluate_device_content_policy`), so any
   locator-class rule makes every download indeterminate → denied
   (`exclusion_policy_denied` 403, reproduced live). A media-type rule keeps
   a real, evaluable deny on the disposable workspace. The next session must
   know this machine's local helper now publishes that shape.
3. **No `sync-now` command exists** — the plugin's command palette exposes
   only copy-diagnostics, self-check, restore and repair-sync. The journey's
   `triggerSyncNow` rewrites the fixture note with its own unchanged bytes
   via the public Vault API: the modify event always fires, the capture
   admits nothing (identical fingerprint) and the `local_commit` trigger runs
   one bounded cycle with zero canonical side effects.
4. **One fresh disposable project per WDIO attempt** — every WDIO run copies
   the fixture vault (which contains `hello.md`); a second run on the same
   workspace creates `hello.md` at an occupied locator
   (`source_locator_conflict` 409), flags the journal `reconcile_required`
   and distorts the journey. Recorded in the runbook.
5. **Guarded-run contract extensions** — the bootstrap tool gained the
   `--wdio-spec test/specs/device-sync-reconciliation.e2e.ts` choice and six
   per-phase failure codes; the journey records seven phase codes through the
   existing `E2E_LIVE_PHASE_STATUS_FILE` contract. Manual (non-guarded) WDIO
   runs need ABSOLUTE paths for the password file, TOTP helper and phase
   file — the bootstrap passes absolute values; relative ones resolve against
   the plugin directory and fail closed.
6. **BACKLOG retirement withheld** — plan Step 4 gated the three triggered
   rows on green Desktop evidence cited in this handoff; the gate is red, so
   none were retired (Execution Discipline #7: no retirement from unit
   evidence when the ruling requires live evidence). Note: live failed
   requests DID carry `request_id` in the API's structured lines throughout
   today's round (422/429/403 lines in `api-diagnostics.log` all carry it) —
   that specific remediation is live-proven and ready to retire on a green
   gate rerun.

## Backlog verdicts

| Row | Verdict |
|---|---|
| 2026-08-23 observability: failed-request `request_id` | RETAINED — remediation live-proven today (structured failed-request lines carry `request_id`); retirement still gated on the green Desktop rerun per plan Step 4 |
| 2026-08-24 sync-error-tracing: P5 read tokens in Stop reasons | RETAINED — no green live run to observe the derived stop-reason surface during healthy sync |
| 2026-08-24 sync-error-tracing: residual trail hygiene group | RETAINED — same gate |
| 2026-08-23 `_validate_epoch_ms` metrics row | RETAINED unchanged (trigger not reached, per plan) |
| 2026-08-24 source-lifecycle `record_commit(COMMITTED)` metrics row | RETAINED unchanged (trigger not reached, per plan) |
| 2026-08-26 device-sync rows (EXCLUDED uploads; review minors batch; index candidates) | RETAINED unchanged |
| NEW 2026-08-27 device-sync-live-gates: physical Mobile matrix | ADDED — one row, `Implement by: Before Child 7 start (physical operator matrix; rerun after the Desktop download-integrity fix)`, pointing here |

## Next actions (in order)

1. Fix defect 3: instrument the plugin's `requestUrl` transport on the real
   wire (probe `arrayBuffer`/`content-length`/`x-content-sha256` as the
   transport sees them inside Obsidian; a focused WDIO probe or a temporary
   trail hook on the integrity inputs localizes it in one run), then make the
   failed-download recovery path terminate (the permanent
   `device_apply_recovery_ambiguous` loop must reach a readable, actionable
   state — repair or blocked verdict — instead of an endless recovery cycle).
2. Rerun the guarded Desktop gate on a FRESH `knowledge-ci-*` project
   (runbook command; expect `obsidian_live_acceptance_passed` with all four
   scenarios in the sanitized evidence block).
3. Record the Desktop rows in
   `docs/operations/device-sync-device-verification.md` from the sanitized
   evidence (an operator observes the run), then run the physical Mobile
   matrix with the operator and record its rows; `uv run poe
   device-sync-device-verification` must exit 0.
4. Retire the three triggered BACKLOG rows above with the green-gate
   citation; update the runbook status block.
5. Final whole-branch review (triage the parked minors in the SDD ledger),
   then `finishing-a-development-branch`.

Operational state at handoff: `knowledge-local` restored and `"ready"`
(all seven services healthy); no `knowledge-ci-*` containers or volumes
remain; the API serve/tunnel background processes started for the round were
stopped (the operator restarts them per `.local/RESTART.md` when needed);
`.local/` is untracked, so the local publish-helper rule change (decision 2)
persists only on this machine and should be read before the next live round.
