# Web-auth poll pacing and admin client hardening — handoff

**Date:** 2026-08-31
**Plan:** `docs/superpowers/plans/2026-08-31-web-auth-multi-worker-poll-and-admin-client-hardening.md`
**Spec:** `docs/superpowers/specs/backlog/2026-08-31-web-auth-multi-worker-poll-and-admin-client-hardening-design.md`
**Branch:** `web-auth-poll-pacing-and-admin-client-hardening` (base `85fb784`)
**Content commits, in order:** `68e4c31` (plan + design spec), `f7de2b6`
(multi-key poll replay digest), `e47b0ca` (`grant_poll` bucket kind,
revision `20260901_01`), `225d9c3` (durable poll pacing; `GrantPollPacer`
deleted), `ab7772b` (rewritten post-offer TOTP Playwright journeys),
`e58a5cd` (single envelope-helper source for the admin API clients),
`3797555` (final-review fix wave: shared-budget proof test, spec drift,
deferred index). The handoff commit carrying this file is last.

Scope: retire the four indexed rows — 2026-08-16 web-auth §9 (single-key
replay digest, hint under-report, unthrottled unknown credentials,
pending-only pacer scope), 2026-08-16 web-auth §13 (multi-worker poll
pacing), 2026-08-30 web-auth acceptance (stale journeys), 2026-08-30 web
admin api clients (duplicated envelope helpers). All four are removed from
`docs/handoff/BACKLOG.md` by the commits that closed them (`f7de2b6` +
`225d9c3`, `225d9c3`, `ab7772b`, `e58a5cd`); the rg no-hit sweep is recorded
in the verification evidence below.

Living operational status: `docs/operations/web-authentication-and-device-authorization.md`
(reverse-proxy section now describes the durable grant-poll pacing). The
per-task SDD reports are session artifacts under git-ignored
`.superpowers/sdd/2026-08-31-web-auth-multi-worker-poll-and-admin-client-hardening/`;
the durable evidence is the commit bodies above, the BACKLOG state, and the
gate counts recorded here.

## Gate status (final verification, plus the fix-wave re-verification)

Run at code head `e58a5cd` (task-6 verification round), re-verified where
touched by the fix wave at `3797555`:

- `uv run poe verify` — exit 0. Green on the third attempt: attempts 1–2
  failed only on an `apps/obsidian-plugin` device-sync journey timeout under
  full-gate parallel load (bits identical to the plan's parent; last touched
  by another plan; passed standalone, isolated, and in the final full run).
  Owned by the device-sync domain, not this plan.
- `uv run poe api-contract-check` — exit 0 (`api_contract_current`, generated
  client `generate:check`). Zero OpenAPI/generated-client delta for the whole
  branch; `git status -- packages/api-client` clean after every build.
- `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-webt6-a uv run poe
  authentication-test` — exit 0, **1778 passed, 2 skipped** over the live
  stack migrated to head (including `20260901_01`). The `serve-live-ci.sh up`
  readiness sub-gate exits 1 with `exclusion_policy_not_initialized` — the
  documented pre-existing caveat owned by the exclusion-policy plan; the
  stack itself comes up migrated and the suite runs against it.
- `pnpm run test` / `pnpm run build` — exit 0.
- Fix-wave re-verification at `3797555`: `uv run pytest
  tests/unit/authentication/test_device_tokens.py -q` — 14 passed;
  `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-webt7-a uv run pytest
  tests/integration/authentication/test_device_token_replay.py -m local_stack
  -q` — **12 passed** (including the new two-instance shared-budget test);
  `poe python-lint`, `poe python-type-check`, `poe python-format-check` —
  exit 0. Stack torn down with `stack_down_complete`.
- BACKLOG retirement sweep — `rg` for the four row signatures over
  `docs/handoff/BACKLOG.md` returns no hits at `e58a5cd` and `3797555`
  (the only web-admin row added since is the new dead-path index below).
- CI gate observation (design acceptance criterion 4's tail) — OPEN: see
  Next actions; it cannot be satisfied pre-merge.

## Interpretive decisions (with reasons)

1. **Multi-previous-key residual — current + exactly one retained key.**
   The replay match covers the digest under the current key plus at most the
   single retained previous key, per spec C1's two-key keyring model; a
   keyring retaining more than one previous key matches current-key-only.
   Reason: the deployment keyring is exactly two-key (the same view the TOTP
   re-encryption leg resolves previous-key rows through), and an N-digest
   command would grow the wire contract with no deployment owner. The boundary
   is now stated in canonical spec §12.2 (`3797555`).
2. **Migration downgrade row-guard overrode the plan brief's reasoning.**
   The plan brief reasoned the `20260901_01` downgrade could directly
   re-create the six-value CHECK; live-stack evidence shows `ALTER TABLE ADD
   CONSTRAINT` validates existing rows in PostgreSQL (only `NOT VALID`
   skips), so leftover `grant_poll` rows abort the re-creation. The downgrade
   therefore DELETEs `grant_poll` rows before re-creating the constraint;
   the integration tests prove the failure with a leftover row, success
   after the delete, retention of other-kind rows, and re-admission on
   re-upgrade (`225d9c3` body).
3. **Acceptance criterion 3 discharged by the fix wave.** The design spec's
   "two pacers (or two composed app instances) against one store" maps to two
   separately composed `DeviceTokenService` instances over one store/engine —
   `GrantPollPacer` no longer exists, so the service is the only pacing
   surface. `test_two_service_instances_share_one_durable_poll_budget`
   (`3797555`) proves worker B's immediate re-poll answers
   `device_authorization_slow_down` with zero shared in-process state, and is
   admissible again after the backed-off window elapses.
4. **C4 CI-gate correction at plan time.** The 2026-08-30 handoff's "auth
   journeys wired into no CI gate" claim was stale: `quality.yml`'s
   `authentication-e2e` job has run `poe authentication-e2e` since the
   child-2 acceptance commit (git `0606cf7`). The deliverable therefore
   became the journey rewrite plus a verified green run through the existing
   gate, not new CI wiring.
5. **C2/C3 pacer disposition.** `GrantPollPacer` is removed outright, not
   reduced to a per-process cache: the durable `grant_poll` bucket is the
   single pacing authority, and a cache could only ever under-throttle
   relative to it.

## Deferred items

1. **Dead `dismissInitialTotpOffer` path — indexed, not fixed (pre-existing,
   outside the plan's four rows).** Browser-unreachable since `99fe1c3`:
   `LoginForm.tsx:178` passes `onSkipped={undefined}` and no other surface
   (`SecurityPanel`, `DeviceApproval`) passes it, so `TotpChallenge.skip()`,
   `AuthenticationClient.dismissInitialTotpOffer()`
   (`authentication-client.ts:179`) and its server action have no browser
   reachability and no e2e coverage (component tests mock `onSkipped`).
   Indexed as ONE row in `docs/handoff/BACKLOG.md` (2026-08-31, web-admin,
   *Before next web-admin surface change*) linking here. This handoff is the
   source because the 2026-08-30 child-nine handoff's deferred section covers
   the retired stale-journey row, not this dead path.
2. **Lock-free violation path — PARK (plan-conformant).** Two workers can
   both read an un-anchored bucket and admit two polls before either's
   window write lands. C3's contract is the durable bucket as the pacing
   authority through the established guarded-insert/upsert pattern, not a
   lock-free linearizability guarantee; the bucket backs off on the next
   observed too-fast poll. Final-review triage: PARK, no fix owed here.
3. **Behavior-change socializing — PARK (already documented).** Repeat polls
   with unknown credentials now answer `device_authorization_slow_down`
   before `device_credential_invalid` (pacing runs before verification;
   existence is not leaked). Documented in the `225d9c3` commit body and the
   spec §11.4 amendment; further socializing is out of scope.

## Next actions

1. After push: observe and record the `authentication-e2e` CI job's first
   auth-journeys run on this branch (`quality.yml`) — acceptance criterion
   4's tail. Record the run outcome durably (PR/commit note); this
   obligation cannot be satisfied pre-merge.
2. Merge decision via `superpowers:finishing-a-development-branch`.
3. Remove the new BACKLOG row when the next web-admin surface change either
   retires the dead `dismissInitialTotpOffer` path or wires it with real
   coverage.
