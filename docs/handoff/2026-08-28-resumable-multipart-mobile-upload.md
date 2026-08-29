# Handoff — Resumable Multipart Mobile Upload (Child 7, complete)

- **Date:** 2026-08-29
- **Branch:** `resumable-multipart-mobile-upload` (from `master` `7fd6137`)
- **Implementation head:** `ef6981d` (fix: close multipart review minors); this handoff commit follows. Closing wave below.
- **Plan:** `docs/superpowers/plans/2026-08-28-resumable-multipart-mobile-upload.md` — all 14 tasks executed.
- **Spec:** `docs/superpowers/specs/2026-08-28-resumable-multipart-mobile-upload-design.md`
- **Living operations doc:** `docs/operations/resumable-multipart-upload.md` (recovery runbook, live procedure) — linked, not duplicated here.
- **Process note:** Tasks 1–12 ran the full SDD loop (implementer subagent + task review + fix rounds). Per explicit user instruction, the Task 13 remainder and all of Task 14 (verification, catch-site sweep, handoff, BACKLOG) were executed directly by the controller session without the final subagent review wave; per-task review evidence lives in `.superpowers/sdd/2026-08-28-resumable-multipart-mobile-upload/`.

## Gate status (final evidence)

| Gate | Command | Result |
| --- | --- | --- |
| Repo verify (deterministic parts) | `uv run poe verify` | ruff clean; mypy strict 209 files clean; Python suites 4207 passed / 21 skipped; api-client 1/1; web 138/138. **Caveat below.** |
| Plugin suite (direct) | `pnpm --dir apps/obsidian-plugin exec vitest run` | 56 files / 1226 tests passed |
| API contract | `uv run poe api-contract-check` | exit 0 |
| Plugin lint / types / build | `pnpm ... run lint` / `type-check` / `build` | exit 0 / 0 / 0 |
| Live integration (final) | `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-multipart-final uv run pytest tests/integration/multipart_upload -q -m "not device_records"` | **39 passed** in 500s (28 local_stack + 11 r2_live) |
| Desktop WDIO live (Task 13) | guarded bootstrap on `knowledge-ci-multipart-live` | `obsidian_live_acceptance_passed`, phase `multipart_journey_completed` |
| Physical Mobile matrix | runbook procedure | **PENDING** — physical device prerequisite, not run |
| Tree hygiene | `git diff --check`; stack teardown | clean; `stack_down_complete`, 0 containers, `knowledge-local` left down |

**`poe verify` caveat (honest):** exit 1 on 4 of 5 runs — each time a different rotating subset of 2–3 *pre-existing* timing-sensitive plugin tests (`repository-subprocess`, `lifecycle-repository` leakage, `echo-suppression` import scan; 5–11 s each) starved under the parallel coverage run on this machine. Evidence it is environment load, not a branch defect: the branch touched none of those three files (`git log master..HEAD -- <files>` empty); the three files pass standalone with coverage 49/49; the direct full suite is green 1226/1226; one full `poe verify` run was entirely green. BACKLOG row added.

**R2 test env note:** the final round composed `R2_TEST_ENDPOINT`/`R2_TEST_BUCKET_NAME` from `.local/serve-local.sh` values and a mode-0600 secret root holding `r2_test_access_key_id`/`r2_test_secret_access_key` copied from `.local/stack-secrets/` (values never rendered), per the `compose_live_environment` contract.

## Task ledger (all reviewed; fix rounds where noted)

| Task | Commits | Note |
| --- | --- | --- |
| 1 contract | `63d0e8a` | twelve `MULTIPART_*` codes (spec §7; brief's "eleven" was a miscount) |
| 2 schema | `32e9dc3` | `20260828_01`; lifetime UNIQUE on `operation_id` (spec §4.2/§5) |
| 3 store+fencing | `0a263fb` `59ba317` `4c3b11e` | fix round: persist-before-create (`20260828_02` widens size CHECK; `20260828_03` defers identity) |
| 4 R2 staging | `7c4b0ad` `42b2973` | fix round: composition-boundary scan amended, literal inlined (string-assembly evasion rejected) |
| 5 service | `aba6c56` | §6.3 chain, exact cleanup, closed metrics |
| 6 Temporal cleanup | `ea971ba` | batch-granularity activity adjudicated sound |
| 7 API routes | `a104f4b` | wire bound → 100 MiB; D1 resolved fail-closed; early OpenAPI/TS regen adjudicated necessary |
| 8 client+composition | `a646519` `d4d1aaa` `7f25383` `46f50ae` | Blocker B staging read; Blocker A sealed token (`20260828_04`); D2 closed log event |
| 9 plugin SQLite v8 | `3d4ede3` | safe progress only; sentinel-scanned |
| 10 runner/scheduler | `167ac56` `6d82b04` | fix round: platform class wired + conservative default |
| 11 diagnostics/privacy | `0710dbf` `0493fc5` | fix round: status counts fed in production |
| 12 integration proof | `1d9ca89` `5a2fe12` | live 11 passed; fix round: redacted assertions |
| 13 docs+live | `3d50040` `75d0921` | WDIO PASS; journey re-fire hardening (controller-applied) |
| 14 final+handoff | this commit | evidence above |

## Spec-interpretation decisions (with rationale)

1. **Twelve error codes** (not eleven) — spec §7 governs over the brief's miscount.
2. **Lifetime UNIQUE on `operation_id`** — replay must return the same single session ever (§4.2 "cleanup_pending is not permission to reuse"; §5 create-or-replay).
3. **Persist-before-create** — §6.1 requires durable session row before the R2 create; identity deferred (nullable) with a fenced post-create write; divergent identity → typed closed error + caller aborts its own fresh orphan; NULL-identity expiry = trivially successful cleanup.
4. **One activity per bounded batch (≤100)** in the cleanup workflow — spec §6.4 prescribes no per-row activity granularity; per-row timeout/retry/closed-failure live inside `run_exact_cleanup`; the store port shape and history-privacy force batch granularity.
5. **24 h session deadline governs evidence reuse** (not the 15-min small-file reservation); the publication fence deliberately skips the small-file expiry check on the bound path.
6. **D1 resolved fail-closed** — recheck guards evaluate a locator-free subject; a locator-keyed deny advance blocks part-URL issuance and completion (route-proven), publication additionally fails closed via `authorize_bound_publication`.
7. **Wire size bound** moved to the 100 MiB product maximum at the API boundary; offline fake keeps `https://multipart-staging.invalid/`.
8. **`multipart_local_content_changed`** is client-originated and intentionally absent from the wire-code map (fail-safe retryable fall-through).

## Deferred items (verdicts → BACKLOG rows)

| Item | Verdict |
| --- | --- |
| Physical Mobile matrix | PENDING — runbook procedure documented; no physical evidence yet, so Child 7's Mobile acceptance is NOT claimed. Row: *Before Child 9 operations acceptance*. |
| `poe verify` plugin-coverage flake (3 pre-existing timing tests, rotating subset) | Environment load, not branch defect (evidence above). Row: *Before the next full `poe verify` dependency bump or CI move*. |
| Bare plugin reload → false `login_required` (credential-refresh race) | Real, prior fixes don't cover the reload corner; harness works around via fresh grant. Row: *Before Child 9 operations acceptance*. |
| `serve-live-ci up` fresh-project quirk: `postgres-provision` intermittently exits 0 without migrations → API readiness 503 (fixtures self-provision; live bootstrap owns `alembic upgrade head`) | Ops fix in the CI bootstrap. Row: *Before the next live acceptance round*. |
| Journey re-fire recovery path (`75d0921`) not yet exercised live (offline-proven; added after the passing round) | Row: *At the next multipart WDIO journey run*. |
| Create-while-`receiving` surfaces `small_file_upload_state_invalid` at the route layer (spec-faithful; resume is status+part-URLs) | Route-layer translation question. Row: *Before production activation*. |
| Cosmetic minors batch from per-task reviews (Tasks 1–12 minors recorded in the SDD ledger; e.g. dual trail entries halving ring history, tautology-shaped sentinel legs, duplicated staging prefix constants, token-taxonomy split, unwired-progress minors) | Non-blocking, triaged per task. Row: *Before Child 8 conflict merge*. |

Resolved during the plan (BACKLOG rows removed): D1 locator stand-in (Task 7), D2 durable rejection surface (Task 8).

## Next actions

1. Record physical Mobile evidence per the runbook (`docs/operations/resumable-multipart-upload.md`) — the only open Child 7 acceptance item.
2. Child 8 conflict-merge sweep should re-check the deferred-minors row and the route-layer `small_file_upload_state_invalid` translation.
3. Merge decision for the branch itself is the user's; SDD workspace kept (per-task review evidence) at `.superpowers/sdd/2026-08-28-resumable-multipart-mobile-upload/`.

## Closing wave (2026-08-29, post-plan, controller-executed per user instruction)

Per the tightened AGENTS.md deferral rule (only out-of-scope items or mobile
live tests may be deferred), the deferred list was closed:

**Fixed and verified (commit `ef6981d` + local script):**
- Live-CI bootstrap quirk FIXED at the source: `.local/serve-live-ci.sh` now
  runs `alembic upgrade head` after stack-up (postgres-provision never
  applied migrations — it only creates roles/databases; the "intermittent"
  appearance was fresh-name vs volume-reuse). Verified on the fresh project
  `knowledge-ci-provision-fix`: full "LIVE STACK UP" incl. `api ready`, then
  clean teardown. Script is git-ignored (local contract); AGENTS.md now
  documents the real behavior.
- Simple review minors: protocol-conformance pin (store↔port, mypy-enforced);
  R2 client-manager raw-client read moved under the lock; presign expiry now
  derived after the SDK call (retried URLs never overstate validity);
  composition-boundary multipart exception re-ban
  `write_object_under_digest`/`delete_exact_object` inside the branch;
  staging-prefix grammar-twin cross-reference; evidence select now DERIVED
  from `_OPERATION_ROW_COLUMNS` (duplication removed, drift impossible);
  cleanup-workflow history scan extended with URL/ETag sentinels; runner
  deadline re-check before completion on fully-resumed sessions; resume no
  longer replays a stale `safeReason`; dead plugin-test assertion reordered;
  plugin composition assertion now pins the shorthand pass-through; journey
  harness byte source passes a manifest-gated recording wrapper (verification
  reads join the exact-identity tripwire); 10-min activity-cap comment states
  the real trade-off. Gates: python 3374 unit + contract suites green, mypy
  strict clean, plugin vitest/lint/type-check green (the two rename/convergence
  tests that flaked under the full parallel run pass 17/17 standalone — the
  documented out-of-scope flake row).
- Route-translation question CLOSED by ruling: keep the spec-faithful
  behavior (create-while-`receiving` answers `small_file_upload_state_invalid`;
  resume is status + part URLs) — documented in the runbook's Safe-resume
  section; BACKLOG row removed.

**Closed by explicit ruling (code stands; no BACKLOG row):** dual
`multipart_failure`+`wire_failure` trail entries (disjoint information, bounded
ring by design); recorder-level sentinel-rejection tests carry the privacy
guarantee where document scans are structural; `part_count` bounds follow the
brief's field list with exact derivation in geometry; URL check is
prefix+length for a server-produced value; lease token taxonomy split is
closed and typed on both families; `literal_binds` parameter-bound tests are
structural; heartbeat cadence and post-start workflow-failure events are not
spec-mandated (lease fencing preserves correctness, per-row failure state is
durable); status `multipart_provider_state_invalid` rides the 24 h expiry
sweep by spec §6.4 design; lost-complete-response re-upload is the safe
direction; `MultipartStagingKey` placement respects the dependency direction
(fail-closed at parse); lazy facade kwargs trade compile-time checks for a
closed seven-method surface; keyring seal-key startup verification is bounded
by the 24 h session lifetime (TOTP precedent); the 12th wire code is
client-originated by design; pre-completion `local_file_missing` is defensive
(driver maps to `deferred_lifecycle`); cancellation/abort share observables
per §6.4; injected cleanup failure exercises the durable retry path with the
retry running the real delete; typed loser paths make race `isinstance`
renders moot.

**BACKLOG state after adjudication:** one mobile-live row (Mobile matrix +
re-fire live exercise, same round) and two out-of-scope rows (plugin timing
flake, web-auth reload race), each naming its owning domain.

**Post-merge follow-up (same day, on master):** the web-auth reload race was
root-caused and fixed — a bare reload runs the startup refresh fire-and-forget
while a queue pass's login-verdict refresh (fix round 4) could rotate the same
credential concurrently; two rotations on one refresh credential risk
server-side reuse detection tombstoning a healthy credential.
`DeviceTokenSession.refresh` now single-flights (concurrent callers join the
in-flight rotation; exactly one transport refresh, TDD-proven: RED double-call
→ GREEN joined). Auth/queue suites 240/240; full suite green modulo the
documented standalone-passing timing flake. BACKLOG row removed.

**`poe verify` flake also fixed (same day):** root causes were threefold, all
real. (1) The plugin files that spawn real subprocesses / wait on real timers
/ scan whole modules exceeded vitest's default 5 s per-test timeout under
parallel coverage on a loaded machine — five files now carry an explicit
`testTimeout: 30_000` (wall-clock headroom only; no assertion weakened).
(2) A genuine Python 3.14 bug in the repo: frozen-dataclass exceptions
(`StackFailure`, `LiveAcceptanceFailure`) crash with `FrozenInstanceError`
instead of propagating whenever they cross a generator context manager, because
contextlib assigns `__traceback__` on 3.14 — reproduced in isolation; both
classes are now hand-rolled immutable exceptions whose `__setattr__` allows
exactly the exception-bookkeeping fields (regression-tested). (3) A transient
Windows `Popen` refusal under load aborted `run_command`; it now retries the
spawn once (bounded; persistent outage still surfaces
`subprocess_unavailable` after exactly two attempts — tested). Evidence:
`uv run poe verify` exit 0 end-to-end (plugin 56 files / 1228 tests green
under coverage, python suites green, contract/type/lint/build gates green).
A latent TS error in the token-session test (missed earlier because the gate
piped through `tail`, swallowing the exit code) was also fixed. BACKLOG row
removed.
