# Handoff — Device-Sync Child-8 Unblock and Smoke-Round Prep

Final handoff of
`docs/superpowers/plans/2026-08-29-device-sync-child8-unblock-and-smoke-round-prep-plan.md`.
Tasks 1-8 landed the five Child-8-unblocking and smoke-round-prep changes
(four server/plugin contract changes, the Web Admin surfaces, the
machine-local worker diagnostics wiring); this Task 9 lands the canonical
docs, retires the five BACKLOG rows and records the final verification
battery. The Child 8 conflict merge is unblocked.

## Commit accounting

Branch `device-sync-child8-unblock-smoke-prep` (from `master`; plan
commit `7f99b57`, merge base `4f77e61`). The last task-wave CODE commit
is **`99663ae`** (`feat: render worker staleness and lifecycle
rejections in Web Admin`); the docs commit (spec amendment, runbooks,
BACKLOG retirement, this handoff) follows `99663ae` immediately. The
final whole-branch review then landed ONE fix commit — `fix: emit the
mid-stream closed reason as a safe diagnostics token`, the "fix commit"
row below, which also carries this handoff's amendment — and `uv run poe
verify` plus `uv run poe api-contract-check` re-ran at that tree (both
exit 0; gate rows refreshed). As before, this handoff cannot contain its
own carrier commit's SHA; the branch tip at land time is the fix commit.

| Commit | Task | Change |
|---|---|---|
| `7f99b57` | — | the plan document |
| `e777f86` | 1 | device-sync contract and route-test hygiene minors (duplicate `__all__` entry, dead fakes helpers, bounded `ManifestAction.local_entry_id`, auth-gate parametrize 8/8 routes) |
| `98b3954` | 2 | mid-stream download failure classified `API_REQUEST_FAILED · response_body_incomplete`; deterministic `aclose` of the verified-chunks generator |
| `a452164` | 3 | migration `20260829_01` — `manifest_entry_resolutions.submitted_policy_allowed` (nullable boolean) |
| `79884c7` | 4 | append-time submitted-policy evaluation for unowned manifest uploads; persisted decision read at finalize |
| `3f2348e` | 5a | `list()` port on the in-memory journal file-store fakes (test seam) |
| `019a297` | 5b | `fresh_journal_reconcile_required`: rebuild reconcile-first decided by vault content; any-generation artifact probe |
| `c2072db` | 6 | one verified download per action (`apply(event, { verifiedDownload })`); outbound `blocked_conflict` repair barrier (`device_manifest_target_occupied`) |
| `99663ae` | 7 | Web Admin renders `worker_stale_running` (PolicyStatus health block) and the lifecycle rejection ring (`/admin/lifecycle` page) |
| (none) | 8 | machine-local: per-worker `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` in `.local/run-worker.sh` + `RESTART.md` step 4b — untracked by design (see Machine-local execution) |
| docs commit | 9 | spec amendment section 21, runbook updates, BACKLOG retirement, this handoff |
| fix commit (branch tip; this row's own carrier) | final review | whole-branch review fix wave: Task 2 mid-stream reason emitted as `SafeToken` (plan-defect fix, decision 4) plus the `build_registered_event`-pinned test; `match="local_entry_id"` on the bounded raises (T1 minor); mobile-round reconcile-first clause on the 2026-08-28 multipart BACKLOG row; machine-local launcher hedge in `sync-error-tracing.md`; this handoff amendment |

## Gate evidence (final battery, 2026-08-29)

First battery ran at `99663ae` + the docs commit; the final-review fix
commit re-ran `uv run poe verify` and `uv run poe api-contract-check`
(both exit 0 — the two rows below refreshed with that run's evidence;
the fix wave changed no web/plugin code, so those counts are unchanged).

| Command | Exit | Evidence |
|---|---|---|
| `uv run poe verify` | 0 | ruff format/check clean; eslint clean; mypy strict — 209 files, no issues; tsc ×3 clean; lint-imports clean; boundary 10 passed; pytest --cov **4220 passed, 21 skipped** (first battery 4219; the fix wave's +1 is the `build_registered_event`-pinned mid-stream test); web vitest 147 passed; plugin vitest 1237 passed; plugin + web builds done — re-ran at the fix commit |
| `uv run poe api-contract-check` | 0 | `api_contract_current`; `openapi-typescript --check` clean (byte-identical snapshot — no wire change, as planned) — re-ran at the fix commit |
| `pnpm --dir apps/obsidian-plugin exec vitest run` | 0 | 56 files / **1237 passed** |
| `pnpm --dir apps/obsidian-plugin run type-check` | 0 | tsc clean |
| `pnpm --dir apps/obsidian-plugin run lint` | 0 | eslint clean |
| `pnpm --dir apps/obsidian-plugin run build` | 0 | build done |
| `CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan-final-20260829` | **1** | known pre-existing fresh-CI readiness behavior — see Known environment behavior; migrations applied through `20260829_01`, all services started, not blocking |
| `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-final-20260829 uv run poe device-sync-test` | 0 | **1708 passed, 2 skipped, 1 deselected** in 634.60 s |
| `bash .local/serve-live-ci.sh down` | 0 | `stack_down_complete`, state `absent`; `knowledge-local` left DOWN (restore: `uv run poe stack-up`) |
| `git diff --check` / `git status --short` | 0 / 0 | no whitespace errors; only the intended docs files |

## Spec-interpretation decisions (with rationale)

1. **The "UI line" ruling (Task 7 / BACKLOG 2026-08-24 web-admin).** The
   closed-reason remediation spec's acceptance readback "from Web Admin"
   means the operator can SEE it in the Web Admin UI — not merely that an
   authenticated endpoint read is possible through the Web Admin session.
   Both surfaces were therefore rendered (no endpoint-read variant kept
   as an alternative): the policy page's `PolicyStatus` component gained
   a Preview worker health alert block (one row per
   `stale_running_previews` entry with its age in minutes and the fixed
   restart guidance, rendered only while the block is non-null), and the
   new `/admin/lifecycle` page renders the lifecycle-operations card
   (commit counters plus the recent rejection ring) through a new
   `source-lifecycle-client`. No API, OpenAPI or generated-client change
   was needed — the types already existed. Rationale: the smoke round's
   readback step is operator work in a browser; an endpoint read would
   keep the surfaces invisible to the operator the spec addresses.
   `docs/operations/sync-error-tracing.md` now names both UI surfaces
   where it previously carried the "wire-only" caveat.
2. **"Digest-after-validation ordering" = the download-reuse defect
   (Task 6).** The retired 2026-08-26 review-minors row listed "double
   download per action" and "digest-after-validation ordering" as two
   findings. They are one defect, and the original SDD ledger carrying
   the split was cleaned, so the reading is recorded here: the reconciler
   downloaded once to prove the fingerprint (bytes discarded) and the
   applier downloaded the same version again — two downloads whose bytes
   could theoretically diverge, with the digest validated on the first
   copy only. Task 6's single fix (`apply(event, { verifiedDownload })`)
   closes both: one verified download per action, the digest proof and
   the applied bytes the same object. The spec amendment section 21
   states this collapse explicitly.
3. **Rebuild fix verified offline-only.** The plan's no-new-gates
   constraint held: the reconcile-first fix is proven by the plugin
   suite (unit seams: `hasVaultContent`, any-generation probe, fail-closed
   probe) and the full battery, not by a physical mobile run. This is a
   plan-acknowledged scope choice, not an oversight — see Deferred items.
4. **The plan's Task 2 `Final[str]` snippet was a plan defect (final
   review, Critical).** The plan mandated
   `_RESPONSE_BODY_INCOMPLETE_REASON: Final[str] = "response_body_incomplete"`
   verbatim, but a raw `str` is outside the diagnostics safe-scalar union:
   the production sink (`DiagnosticLogger.emit` → `build_registered_event`)
   would always reject the payload and drop the event, substituting
   `logging_payload_rejected` — the closed reason token never reached the
   diagnostics stream, violating the plan's own Produces contract (token
   surfaced in the diagnostics stream per the AGENTS closed-path rule).
   Fixed by emitting `SafeToken.parse("response_body_incomplete")`,
   matching the neighboring `_INVALID_FORMAT_REASON` constant's exact
   style; the `events.py` registry allowance (optional `reason` on
   `api_request_failed`) was already correct and is unchanged. Per-task
   review missed it because the unit `RecordingSink` captures payloads
   verbatim without validation; the whole-branch review caught it at the
   middleware↔validation boundary. A new test routes the mid-stream
   failure payload through `build_registered_event` and asserts the
   accepted event keeps the reason intact, so this defect class (unit
   double bypassing validation) cannot recur silently.

## Machine-local execution (Task 8, not in git history)

Task 8 edited only untracked machine-local files — by design there is no
commit to cite. It was EXECUTED and verified, not deferred:
`.local/run-worker.sh` now maps each worker role to its own diagnostics
directory (`.local/runtime-logs/worker-previews/` and
`.local/runtime-logs/worker-reconciliations/`), exports
`KNOWLEDGE_DIAGNOSTICS_LOG_DIR` and creates the directory before `exec`;
`.local/RESTART.md` step 4b notes the automatic sink. Evidence pointer:
the final battery's `serve-live-ci.sh up` run (this task) started
`worker-previews` and `worker-reconciliations` with the new directories —
and Task 8's own verification round proved the sink attaches at process
start (worker-created files in both directories; names/sizes only, never
contents). The wiring retired BACKLOG row 2026-08-24 policy-workers.

## BACKLOG retirement (five rows removed)

| Removed row | Retired by |
|---|---|
| 2026-08-24 policy-workers — worker `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` wiring | Task 8 (machine-local, executed — above) |
| 2026-08-24 web-admin — Web Admin UI rendering decision | Task 7 (`99663ae`) + the UI line ruling |
| 2026-08-26 device-sync — unowned uploads plan `EXCLUDED` | Tasks 3+4 (`a452164`, `79884c7`): migration `20260829_01` + append-time decision |
| 2026-08-26 device-sync — per-task review minors batch | Tasks 1+2+6 (`e777f86`, `98b3954`, `c2072db`); the "digest-after-validation ordering" half rides decision 2 |
| 2026-08-27 device-sync-recovery — mobile rebuild reconcile-first | Task 5 (`019a297` + `3f2348e`); physical re-verification deferred — see below |

The remaining `Before Child 8 conflict merge` blocker set is empty: the
Child 8 conflict merge is unblocked.

## Deferred items and review-minor verdicts

1. **Physical mobile re-verification of the rebuild fix — rides the next
   mobile matrix; NO BACKLOG row re-added.** Verdict: this is the AGENTS
   mobile-live-test deferral class (needs a physical device), and its
   trigger is already indexed — the standing Child 9 mobile gates
   (`docs/20-IMPLEMENTATION_PLAN.md` Child 9 acceptance) plus the next
   physical live round already tracked by the 2026-08-28 multipart-upload
   mobile BACKLOG row. A new row would duplicate an indexed trigger; the
   final-review wave instead appended one clause naming this
   re-verification to that existing row (self-contained index).
   Offline evidence stands in the interim: the mobile full-deletion shape
   is pinned by unit seams (fresh journal + vault content ⇒
   `fresh_journal_reconcile_required`; any-generation artifact probe;
   fail-closed probe error).
2. **Per-task review minors from the SDD ledger — adjudicated "code
   stands" in groups** (each was logged during Tasks 1-8; none reopens a
   code change in this docs-only task):

   | Minor (task) | Ruling |
   |---|---|
   | StarletteDeprecationWarning from fastapi testclient import (T1) | Code stands — third-party deprecation, pre-existing, no repository change can remove it; surfaces as 1 warning in every gate run |
   | Bare `pytest.raises` without `match=` in the new bound test (T1) | Fixed in the final-review fix commit — both raises blocks now carry `match="local_entry_id"` (the superseding "code stands" ruling is retired) |
   | Starlette abandons the body iterator on suspension-at-yield disconnect; inner reader only GC-closed there (T2) | Code stands — framework behavior no wrapper can intercept; the deterministically reachable close/throw/complete paths are covered by tests; the reader's own context still closes it |
   | aclose test drives the endpoint directly, not through the ASGI stack (T2) | Code stands — Starlette abandons the iterator on disconnect, so the ASGI stack cannot reach the aclose path at all; the `_continued` wiring itself stays covered by the existing route tests |
   | Head-revision literals scattered across 13 unit test files (T3) | Code stands — pre-existing style in files this plan does not own; the canonical pin (named constant in the migration contract test) was updated |
   | Legacy `NULL` fallback in `_plan_actions` has no seeded-NULL test (T4) | Code stands — the fallback line is the removed legacy call verbatim (same method and arguments), not new logic; every fresh row records the append-time verdict |
   | `plugin.ts` comment "mirrors exactly the files the automatic snapshot would admit" overstates `getFiles()` (T5) | Code stands — plan-verbatim comment; the practical divergence is the hidden-file set, and `getFiles()` seeing more content errs toward reconcile-first (the safe direction) |
   | `createVaultPluginJournalStore.list` docstring says "own file names" but admits nested descendants (T5) | Code stands — harmless by construction: `isGenerationFileName` rejects any name containing `/`, so nested names cannot pass the probe |
   | Bare catch swallows stale-generation refusal and genuine DB failure alike in the barrier helper (T6) | Code stands — plan-mandated snippet; bounded consequence: a genuinely failed barrier raise degrades to the pre-fix no-barrier behavior (the conflict park itself still completed) |
   | No test shows the held follower resumes after barrier release; test title "moves the queue on" loosely true (T6) | Code stands — release/resume is the pre-existing barrier-pause machinery's own tested behavior; the new tests pin the park + barrier raise |
   | Empty-array `stale_running_previews` would render the alert block with restart guidance (T7) | Code stands — brief-verbatim null gate; the server computes `null` while nothing is stale per the runbook; worst case is an empty list plus guidance |
   | Rejection-row React key `operation-timestamp-errorcode` collision theoretically possible; pre-existing `unwrapEnvelope` copies in two sibling clients (T7) | Code stands — same-operation/same-code/same-millisecond collision at worst triggers a React key warning; the export introduced by Task 7 prevents a third copy |
   | `RESTART.md` `worker-<role>` placeholder could misread as a CLI arg (T8) | Code stands — brief-mandated form matching the file's angle-bracket placeholder convention |

3. **Known environment behavior (recorded, not chased).** On a fresh CI
   project, `serve-live-ci.sh up` may report the API never ready
   (`/api/health/ready` fails on `exclusion_policy_not_initialized`) —
   first observed in Task 3, observed again in this final round (up exit
   1). It does not block the DB-backed suites: the device-sync-test
   fixtures self-provision and passed 1708 on the same stack. The smoke
   round itself will initialize policy through the documented bootstrap.

## Next actions (in order)

1. `finishing-a-development-branch` for this branch (all gates green at
   `99663ae` + this docs commit).
2. Start Child 8 (conflict candidates/merge/resolution) — its merge gate
   blockers are retired.
3. Run the closed-reason live smoke round (BACKLOG row 2026-08-23
   closed-reason-surfacing, `Before Child 9 operations acceptance`): the
   W1 worker dispatch events now land in durable per-worker rotating
   files by default, and the W3/L1 readbacks are visible in the Web Admin
   UI (policy page Preview worker health block; `/admin/lifecycle`
   page).
4. At the next physical mobile matrix (Child 9 gates / the 2026-08-28
   multipart row's live round), re-verify the rebuild reconcile-first
   fix on the device that surfaced the original finding.

Operational state at handoff: the CI project and all helper-started
services are torn down (`stack_down_complete`); `knowledge-local` left
DOWN (restore: `uv run poe stack-up`); `.local/` changes (Task 8) persist
only on this machine and are documented above and in
`.local/RESTART.md`.
