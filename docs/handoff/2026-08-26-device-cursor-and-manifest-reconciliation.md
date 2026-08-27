# Handoff — Device Cursor and Manifest Reconciliation (Child 6, Final)

Final handoff of
`docs/superpowers/plans/2026-08-26-device-cursor-and-manifest-reconciliation.md`.
Tasks 1-14 landed the implementation, cross-boundary tests, canonical docs and
the 0.2.0 release candidate. Task 15 ran the mandatory Desktop WDIO live gate:
the first round exposed three real transport defects (all fixed and tested),
and the rerun on 2026-08-27 returned the repository's closed
**`obsidian_live_acceptance_passed`** verdict with all four Child 6 scenarios
PASS. The physical Mobile matrix did not run (operator unavailable) and stays
properly deferred — Child 6 closes only after it.

## Commit accounting

Branch `device-cursor-manifest-reconciliation` (from `master` @ `41ab718`).

- `cd2a301` — Task 14 (release candidate 0.2.0; last pre-Task-15 commit).
- `1544afc` — Task 15 round 1: the WDIO journey
  (`apps/obsidian-plugin/test/specs/device-sync-reconciliation.e2e.ts`), phase
  codes, guarded-bootstrap spec choice, transport fixes for JSON
  `content-type` and `no-transform`, the reference-device records template +
  contract test, runbook status, docs/15 accumulator sentence, this handoff's
  first draft, the Mobile BACKLOG row.
- `c0fbb17` — Task 15 debug round: the three root causes behind the red gate
  (see Live gates) with unit coverage on the real shapes.
- The final Task 15 commit (successor of this list, exact SHA
  `git rev-parse HEAD` at commit time): green-gate evidence, BACKLOG
  retirement, this rewrite, and the operational changes (compose
  `restart: "no"` + its contract-test pin, AGENTS.md live-stack rule).

Earlier task commits are tabulated in git history `4bf367b..cd2a301`; the
per-task review record lives in
`.superpowers/sdd/2026-08-26-device-cursor-and-manifest-reconciliation/progress.md`
(git-ignored scratch).

## Offline gates (green at the final Task 15 commit candidate)

| Gate | Command | Evidence |
|---|---|---|
| Full Python + web | `uv run poe verify` | exit 0 (ruff/mypy strict/eslint/tsc/vitest/build/web build) |
| API contract | `uv run poe api-contract-check` | exit 0 |
| Device-sync suite | `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-* uv run poe device-sync-test` | exit 0 — 1589 passed / 2 skipped / 1 deselected on `knowledge-ci-task15-final-verify` in 767 s (re-run at the final commit candidate) |
| Plugin unit | `pnpm --dir apps/obsidian-plugin exec vitest run` | full suite green (1144 at `c0fbb17`) |
| Plugin types/lint/build | `tsc --noEmit` / `run lint` / `run build` | all exit 0 |
| Records gate | `uv run poe device-sync-device-verification` | exit 1 **by design** until the operator records the physical Desktop/Mobile rows; both sections exist with every required label `pending` |
| Whitespace/tree | `git diff --check` / `git status --short` | clean; only intended files |

## Live gates (Desktop WDIO PASSED 2026-08-27)

Procedure: `.local/RESTART.md` order followed; disposable `knowledge-ci-*`
projects only (`knowledge-local` volumes untouched; it is left DOWN by design
— see Operational changes); Web Admin :38000, both policy workers and the
existing tunnel `knowledge-api-verify` ran via the new one-command helper;
TOTP enrolled/activated through the approved Web HTTP flow by the guarded
bootstrap; policy published through `.local/publish-policy-revision.py`
(machine-local; publishes the `media_type image/*` family deny — a
locator-class rule cannot evaluate for locator-less verified downloads by
design). No secrets printed; only runbook hostnames appear here.

### Green round

- Project `knowledge-ci-task15-final-20260827` (fresh; one project per WDIO
  attempt — a second run on the same workspace distorts the journey via the
  occupied `hello.md` locator).
- Guarded bootstrap verdict: **`obsidian_live_acceptance_passed`**, state
  `complete` (exit 0). All four scenarios PASS inside the journey: remote
  edit + exact no-echo; cursor gap → repair; SQLite loss without duplicate
  canonical source; remote tombstone → Obsidian local trash.
- Sanitized operator observation during the repair scenario (settings export):
  a transient `Repair: Running (device_manifest_state_invalid)` entry that the
  journey cleared — final device state Applied/Acknowledged converged, cursor
  lag 0, blocker cleared. The diagnostics export header
  `obsidian_sync_diagnostics_export/v1` is the designed export contract (only
  the persisted trail went v2 in Task 7).

### The three root causes behind the earlier red gate (all FIXED)

1. **JSON bodies lacked `content-type`** — every cursor acknowledgement was
   422-rejected before any handler ran. Fixed in the plugin client
   (`jsonRequest` sends `content-type: application/json`); live-verified 200.
2. **The verified byte stream was transformable** — the tunnel's edge gzipped
   `text/plain` payloads and dropped `Content-Length` (proven by a sanitized
   credential-scoped probe). Fixed server-side with
   `Cache-Control: no-store, no-transform`.
3. **The Electron wire differed from every offline stub** (fixed in
   `c0fbb17`): (a) Starlette's `media_type` helper appended `; charset=utf-8`
   to every `text/*` Content-Type, failing the client's exact closed check —
   the header is now set verbatim; (b) the Obsidian Vault index never sees
   dot-prefixed siblings (and lags adapter renames), so the writer's hidden
   staging files now ride a structural data-adapter surface; (c) a
   crash-interrupted apply at `prepared` awaited event redelivery that the
   server never performs — proven-clean intents are now abandoned (intent +
   echo marker) with a `device_apply_recovery_abandoned` repair barrier,
   replacing the permanent `device_apply_recovery_ambiguous` loop.

### Mobile matrix

Not run — the operator was unavailable; per AGENTS.md Mobile acceptance may
defer but never be inferred from Desktop. Exactly one BACKLOG row (2026-08-27
`device-sync-live-gates`) holds it with `Implement by: Before Child 7 start`.
No Mobile row in `device-sync-device-verification.md` is marked passed.

## Spec-interpretation decisions (with rationale)

1. **Plugin version window** — the machine-local launcher pinned
   `KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION=0.1.0` while the branch releases 0.2.0;
   raised to `0.2.0` (machine-local file) before the green round.
2. **Local policy rule shape** — see Live gates (media-type deny; a
   locator-class rule denies every verified download by design).
3. **No `sync-now` command exists** — the journey's `triggerSyncNow` rewrites
   the fixture note with identical bytes via the public Vault API so the
   `local_commit` trigger runs one bounded no-op cycle.
4. **Guarded-run contract extensions** — the bootstrap tool gained the
   `--wdio-spec test/specs/device-sync-reconciliation.e2e.ts` choice and six
   per-phase failure codes; the journey records seven phase codes through the
   existing `E2E_LIVE_PHASE_STATUS_FILE` contract.
5. **BACKLOG retirement executed on green evidence** — see verdicts.

## Backlog verdicts

| Row | Verdict |
|---|---|
| 2026-08-23 observability: failed-request `request_id` | **RETIRED** — remediation live-proven across rounds (422/429/403 lines in `api-diagnostics.log` carry the bound `request_id`) and the green gate cites it |
| 2026-08-24 sync-error-tracing: P5 read tokens in Stop reasons | **RETIRED** — the healthy-sync live round showed the derived Stop-reasons surface clean while sync ran (Task 7's `composition_read_failure` exclusion live-observed) |
| 2026-08-24 sync-error-tracing: residual trail hygiene group | **RETIRED** — dead bind removed, taxonomy fixed, newest-first tail observed in the live settings exports (v1 header by design, entries newest-first) |
| 2026-08-23 `_validate_epoch_ms` metrics row | RETAINED unchanged (trigger not reached, per plan) |
| 2026-08-24 source-lifecycle `record_commit(COMMITTED)` metrics row | RETAINED unchanged (trigger not reached, per plan) |
| 2026-08-26 device-sync rows (EXCLUDED uploads; review minors batch; index candidates) | RETAINED unchanged |
| 2026-08-27 device-sync-live-gates: physical Mobile matrix | RETAINED — updated with the green-Desktop fact; blocks Child 7 start |

The two RETAINED 2026-08-26 device-sync rows each index exactly one deferred
group: the per-task review minors batch from Tasks 1-11 (triaged by the final
whole-branch review; `Implement by: Before Child 6 whole-branch review`) and
the per-workspace pull index `(workspace_id, event_sequence)` together with
the `source_tombstones.restore_event_id` index (query-plan gates pass at the
pinned fixture size; `Implement by: Before production activation`).

## Operational changes landed with Task 15 (user-directed)

- `infra/compose/compose.yaml`: every long-running service `restart: "no"`
  (was `unless-stopped`) — `knowledge-local` no longer auto-wakes when Docker
  Desktop starts; the contract test pin updated to match.
- `.local/serve-live-ci.sh` (machine-local): one command for a live round —
  stands `knowledge-local` down, provisions the disposable CI stack (migrations
  included), starts API + Web Admin + both policy workers + the tunnel, waits
  for readiness; `down` stops everything and tears the CI project out.
  `AGENTS.md` and `.local/RESTART.md` now route live rounds through it.

## Next actions (in order)

1. An operator records the sanitized Desktop rows in
   `docs/operations/device-sync-device-verification.md` from the green
   evidence block, then runs the physical Mobile matrix on the reference
   device and records its rows; `uv run poe device-sync-device-verification`
   must exit 0 — only then is Child 6 complete.
2. Final whole-branch review (triage the parked minors in the SDD ledger),
   then `finishing-a-development-branch`.

Operational state at handoff: the live CI project and all helper-started
services are torn down via `bash .local/serve-live-ci.sh down`;
`knowledge-local` is left DOWN (restore with `uv run poe stack-up`);
`.local/` is untracked — the launcher/policy-helper changes persist only on
this machine and are documented above.
