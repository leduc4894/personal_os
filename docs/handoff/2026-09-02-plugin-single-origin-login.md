# Plugin single-origin login — handoff

Branch: `plugin-single-origin-login`. Final code commit: `6b60df0`
(`docs: standardize plugin public origin`); branch block `f87b1d3` (plugin
login transport opens the server-minted verification URL, no client-side
derivation), `742e89c` (API admits native device-grant requests without an
`Origin` header), `6b60df0` (one-origin tooling/docs). The closure commit
(this handoff plus the BACKLOG edit) is the branch tip.

Resolved deferred finding (BACKLOG row retired by this round): the 2026-09-01
device-sync row "Plugin login button derives the browser URL from
`server_origin`" — the plugin now consumes the server-returned verification
URL on the one configured public origin, closed live below.

All evidence below is sanitized: outcomes, closed reason tokens, booleans,
counts, timestamps. No origin, hostname, URL, path, user code, or credential.

## Gate status (with evidence)

- **Offline gates (Tasks 1–3 + focused rerun, Step 2a).** Focused python
  files (`tests/unit/api_runtime/test_device_authorization_routes.py`,
  `tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py`): 53
  passed (1 unrelated deprecation warning). Plugin vitest: 64 test files /
  1444 tests passed, exit 0 (2026-09-02T08:0xZ).
- **Final repository verification (Step 5, 2026-09-02 after teardown).**
  `uv run poe verify` exit 0 — python suite 4610 passed / 21 skipped /
  550 deselected, api-client 1 passed, obsidian-plugin 64 files / 1444
  tests passed, web 21 files / 163 tests passed (the documented jsdom
  typescript-test flake did not occur this round). `git diff --check`
  clean; `git status --short` empty before the closure docs edits.
- **Live one-origin proof (Step 2b, against the public origin, sanitized).**
  Native-shape grant creation with NO `Origin` header → HTTP 200,
  `verification_uri_complete` host equals the public origin host: true,
  user code fragment-only: true, one-time grant payload fully rendered:
  true (2026-09-02T08:11:41Z). Control with a foreign `Origin` → HTTP 403
  `csrf_validation_failed` (2026-09-02T08:11:41Z). Verdict PASS: native
  grant creation succeeds on the one public origin and any foreign Origin
  is still rejected by the exact-origin gate.
- **Desktop operator journey (Step 3, manual, dedicated test vault).**
  Verdict **PASS**. Operator set/confirmed `Public workspace origin` equal
  to the configured public origin, selected Login → browser opened the
  server-minted verification page; Web Admin login + TOTP succeeded (1
  stale TOTP attempt rejected first, fresh code via the repo contract
  helper succeeded); device approved in browser; plugin status
  **Connected** (operator-confirmed, 08:21–08:22Z).
  Controller API/DB verification at checkpoints (counts + reason tokens):
  grants 1 pending (controller's own live-proof grant, intentionally never
  approved) + 1 exchanged (the journey's grant: browser approval followed
  by plugin token exchange); request trace 08:20:20Z native grant POST →
  200, 10× poll → 409 `authorization_pending` while the operator approved
  (expected), login 422/401 then 200, TOTP verify 401 (stale code) then
  200, grant lookup 200, approve 200, final poll 200 (token exchange
  complete); post-connection allowed sync requests all 200
  (exclusion-policy keysets/snapshot, session, admin devices) plus
  **4× `/api/sync/events` → 200 `succeeded`** (08:22:17–08:23:14Z).
  sync_events committed in DB during the journey window: 0 (no note
  content changed; the gate's sync-request evidence is the 4 successful
  sync requests, which is what the acceptance requires).
- **Teardown (Step 4, 2026-09-02).** `bash .local/serve-live-ci.sh down`
  stopped the four services (api, web-admin, both workers), returned
  `stack_down_complete` with the disposable project state `absent`;
  `CI=true uv run python tools/local_service_stack.py status
  --project-name knowledge-ci-plugin-origin-20260902` → `stack_absent`
  (no services, no initializers); `uv run poe stack-status` →
  `knowledge-local` state `absent` (remains down, restorable with
  `uv run poe stack-up`).

## Decisions (interpretation of spec, with rationale)

1. **`.local/` edits stay working-tree only.** Task 3 adjusted
   `.local/RESTART.md` and `.local/serve-live-ci.sh` to export the
   fixture's public-origin bounds and project naming. `.local/` is
   gitignored by repo policy, so these edits are deliberately untracked
   (not force-added). The launcher contract is documented in the
   `docs/operations/` runbooks, which reference the scripts by name.
2. **Stale-process port-hijack incident and recovery (Step 1).** The first
   `up` silently hit the exact hazard `.local/RESTART.md` warns about:
   stale API and web-admin processes from an earlier session still held
   their ports, so the freshly launched API failed at startup while the
   readiness probe answered from the OLD pre-branch process (which
   rejected native grant creation with `csrf_validation_failed`), and the
   fresh web-admin failed with EADDRINUSE. Recovery per RESTART.md order:
   only the two stale process trees were killed (command lines verified
   first), all four services were relaunched exactly as `serve-live-ci.sh`
   does under a persistent parent with PIDs recorded in the live-ci state
   file, and readiness was re-verified (api 200, web-admin 200,
   public-origin API 200, 2 workers alive, 2026-09-02T08:12:31Z). The
   authoritative live proof and the operator journey both ran against the
   fresh branch-code process afterwards; the pre-recovery 403 is
   attributable to the stale process, not the branch code (fresh-process
   rerun is the evidence of record). Bootstrap state needed no re-run: it
   is database state on the same CI project database, and the sealed TOTP
   credential is code-independent.
3. **Operator journey performed manually per the operator contract.**
   AGENTS.md forbids WDIO for Live Obsidian Desktop journeys; the human
   operator executed the GUI journey on a dedicated test Vault (separate
   from the daily vault) while the controller verified API/DB state at the
   checkpoints recorded above. Manual evidence was the mandatory gate; no
   mock or self-marked pass substituted for it.

## Deferred items (verdicts)

Both deferred items are tooling observations from the live round, out of
this plan's code scope; each got exactly one BACKLOG row:

- **`serve-live-ci.sh` does not fail on its own bind failure** (owner:
  local launcher tooling maintenance). The launcher's readiness probe can
  be answered by any process on the API port, so the first `up` reported
  all-ready while the fresh API had died — the incident above. Verdict:
  defer; recovery was complete and verified, but the launcher should fail
  closed. BACKLOG row (local-launcher tooling), implement by: at next
  `.local` launcher maintenance, before the next disposable live journey.
- **`tools/obsidian_live_acceptance_bootstrap.py` has no WDIO-free /
  bootstrap-only mode** (owner: live-acceptance tooling). Journey prep had
  to drive the helper's phases programmatically (same module, same
  commands, same closed failure codes, mirroring
  `_execute_live_acceptance` up to and including policy publication,
  minus the WDIO tail). Verdict: defer; the phases are the helper's own
  and outcomes were verified, but a `--bootstrap-only` flag removes the
  seam. BACKLOG row (live-acceptance tooling), implement by: before the
  next manual Desktop live journey.

No in-scope finding was deferred; the previously deferred plugin-login
finding is resolved and its row deleted (done work lives in git history).

## Next actions

1. Branch review/merge per the SDD controller's plan (whole-branch review
   still owns the workspace ledger).
2. If the launcher/bootstrap tooling rows are picked up, land them before
   the next disposable live journey (see the two BACKLOG rows above).
3. The dedicated test vault's plugin copy remains pointed at the now-torn
   -down CI project — expected post-round state; any future journey
   re-runs bootstrap per its runbook.

## Workspace

SDD workspace:
`.superpowers/sdd/2026-09-02-plugin-single-origin-login/` retained until
the whole-branch review; sanitized step evidence lives in
`task-4-live-report.md` and `task-4-journey-evidence.md` there.
