# Child Nine Hygiene Retirement — interruption handoff (Tasks 1–12 of 20)

Branch `child-nine-hygiene-retirement` (from `master` @ `85fb784`). Plan:
`docs/superpowers/specs/backlog/2026-08-30-child-nine-and-phase-two-closure-hygiene-retirement-design.md`
implementation plan `docs/superpowers/plans/2026-08-30-child-nine-hygiene-retirement.md`.
Session stopped by user after Task 12; Tasks 13–20 plus the SDD final whole-branch
review remain. This file is the interruption snapshot; the completing session's Task 20
rewrites it as the final handoff.

## Trạng thái

- Final SHA at interruption: `10703ff` (Task 12), working tree clean.
- Checkpoint 1 (Tasks 1–13, "before Child 9 acceptance"): Tasks 1–12 complete;
  **Task 13 (plugin session hygiene) is the resume point.**
- Checkpoint 2 (Tasks 14–20): not started.
- Resume map (authoritative, survives compaction): SDD ledger at
  `.superpowers/sdd/2026-08-30-child-nine-hygiene-retirement/progress.md` — per-task
  commits, fix rounds, parked minors. Per-task briefs/reports/review packages sit in
  the same directory.

## Gate evidence (per completed task; full output in task reports)

- Task 2 (only OpenAPI delta): `api-contract-check` exit 0; snapshot diff = exactly one
  new ErrorCode enum member (`canonical_recovery_admission_refused`).
- Python unit/contract suites green at each task commit (e.g. recovery+tools 369
  passed after T5 fix; full default suite 4236 passed after T5).
- Integration on disposable CI stacks, all green and torn down: canonical_core 19
  passed (T3), password transactions 10 passed (T7), credential commands 7 passed
  (T8), `poe authentication-test` 1759 passed (T9) then 1767 passed (T10).
- Web gates green after T11/T12: `@workspace/web-runtime` test/type-check/lint/build
  exit 0 (163 tests at T12).
- KNOWN PRE-EXISTING (not this branch's defect): `bash .local/serve-live-ci.sh up`
  exits 1 at its API-readiness sub-gate with `exclusion_policy_not_initialized` on any
  fresh CI project (log evidence from 2026-08-27, `.local/runtime-logs/live-ci-api-restart.log`).
  Containers come up healthy; DB-level suites run and pass against them. Same for the
  Git Bash PATH gap: prepend `/c/Program Files/PostgreSQL/18/bin` before integration runs.

## Quyết định diễn giải spec (kèm lý do; chi tiết trong task reports)

- T2: `recovery/contracts.py` `RecoveryError.allowed_codes` gained the new member
  (structurally forced — `ApplicationError` rejects unregistered codes); service tests
  live in `test_service_restore.py` (no `test_service.py` exists); runbook exit table
  split preserves a residual config-refusal row (3 rows, information-preserving).
- T3: brief's predicted REDs for deterministic empty-dir/extra-object tests were
  inaccurate (existing probes already rejected those shapes); genuine REDs were the
  probe-to-publish race, move-rollback, and fake-store media-conflict tests — outcomes
  still pinned.
- T4 (plan-mandated ruling): alembic version table stays in `public` (env.py sets no
  `version_table_schema`); constant renamed `_ALEMBIC_VERSION_TABLE_REFERENCE` with
  provenance comment, pinned by test.
- T5: fallback copy buffers verified chunks then one off-loop write (≤100 MiB per
  object, matches the documented ruling; streaming-to-file via to_thread impossible —
  source reader is an async port). Fix round 1 closed a NEW dir-create thread race
  (`_create_directories_private` FileExistsError tolerance for owned non-symlink dirs).
- T7: credential transaction port lives in `sessions.py`, not `ports.py` (verified —
  that is what LoginService depends on); all three implementers extended.
- T8: `status` never fails for existing usernames (deliberate; no workspaces join), so
  the archived-workspace pin landed on the enroll/reset path where exit 78 exists;
  reset-before-enrollment pins the truthful closed refusal (not zero counts).
- T9: `.rowcount == 0` does not work on this stack (SQLAlchemy 2.0.51 + psycopg 3.3.4
  returns -1 for guarded inserts) — win/loss signal is `.returning()` presence
  (server-guaranteed); fix deduped into shared `apply_throttle_bucket_failure`; the
  five TOTP outcome dataclasses also gained `limited_at` (needed by `_rate_limited_json`).
- T10: accepted ordering change — a locked source with an INVALID request now gets the
  validation rejection instead of 429 (lock rides the inserting transaction; invalid
  requests can never create grants either way; valid requests still 429).
- T11: `closeAsTerminal`'s `setChallengePassword("")` is React-unobservable (subtree
  unmounts in the same commit); accepted an end-to-end MSW replacement test.
- T12: focus trap intentionally minimal (Tab wrap + opener restore, no `inert` — no
  new dependency per brief); Retry control gated to the rate-limited path via
  `isLookupRetryable` (retrying expired/denied codes is a dead end).

## Item chờ phán quyết (awaiting the plan's final whole-branch review — not BACKLOG rows)

Parked minors live in the SDD ledger (link above). Highest-signal ones:

- T1: sibling policy workers share the dispatcher's dispose/close gap — plan Task 20
  handoff item (c): observed, out of scope, NOT re-indexed (2026-08-24 policy-workers
  rows own that domain).
- T5: `write_object` digest pre-check hashes up to 100 MiB on the loop (pre-existing,
  outside the brief's filesystem-work scope).
- T8 handoff rulings: EOF at interactive credential password prompts →
  `internal_error:eoferror`/70 (pre-existing; surfaces a closed class token; ruling:
  code stands). Status-on-archived truthful behavior (exit 0) left unpinned.
- T11: `DeviceApproval.loginFields.password` not cleared after successful inline
  login; `SecurityPanel` re-auth password persists pre-filled into the change-form
  current-password field (both out of T11's change list, same hygiene class).
- T12: `exclusion-policy-client.ts` keeps a second copy of
  `REQUEST_UNAVAILABLE_ERROR`/`unwrapEnvelope` (out of the brief's two-file list) —
  triage at final review: fix in-session if trivial, else one BACKLOG row with a
  concrete `Implement by`.
- BACKLOG reconciliation: Tasks 9 and 12 already removed rows 2026-08-16 §7 and §11
  in-commit (AGENTS-compliant immediate removal) — Task 20 removes the remaining 20
  of the plan's 22 rows (2026-08-14 ×5, 2026-08-15 ×9, 2026-08-16 ×5, acceptance ×1).

## Next actions

1. Resume SDD loop at Task 13 (plugin session hygiene) — dispatch per the plan; the
   ledger's "Session: stopped by user" line marks the boundary.
2. Task 14 must also fold in the carry-to-Task-14 ledger item: add
   `authentication.login_locked_out` to the closed action set paragraph in
   `docs/operations/web-authentication-and-device-authorization.md` (~237-239).
3. Tasks 15–19 per plan; Task 20: full verification block (plan Step 1), remove the
   remaining 20 rows, rewrite this handoff with final SHA + gate evidence + the
   plan-mandated interpretive decisions (a)–(e).
4. SDD final whole-branch review over `85fb784..HEAD` (most capable reviewer), point
   it at the ledger's parked-minor list, one fix wave max, then
   superpowers:finishing-a-development-branch.
