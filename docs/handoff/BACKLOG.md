# Deferred Work Backlog

Single living index of every deferred (accepted-but-not-done) item across all
handoffs. Each item is ONE line pointing to the handoff that holds the full
context and ruling. Remove the line when the item is done — done work lives in
git history, not here.

Scope guard: this file indexes DEFERRED work only. Gates and requirements
belong to `docs/20-IMPLEMENTATION_PLAN.md`; current status of a gate belongs
to the living domain doc (e.g. `docs/operations/`). Do not duplicate those
here — link them at most.

`Implement by` is the latest permitted delivery gate: “Before Child N” blocks
that child from starting; operational and conditional entries name the exact
trigger instead. Non-load-bearing hygiene rows use `Before Phase 2 closure
(after Child 9)` and do not block the next child by default.

| Added | Domain | Item | Implement by | Details |
|---|---|---|---|---|
| 2026-08-15 | api-contract | `openapi-typescript@7.13.0` peer-declares `typescript@^5.x` while the workspace pins `6.0.3` (standing install warning; resurface on any pin bump) | At next TypeScript pin bump | [handoff §1](2026-08-15-api-runtime-contract-foundation.md) |
| 2026-08-16 | ci-workflows (pre-existing) | Stack workflows other than `authentication-acceptance.yml` lack the mutual project-name/guard consistency pins | Before next stack workflow change | [handoff §15](2026-08-16-web-authentication-and-device-authorization.md) |
| 2026-08-18 | small-file-sync | Child-4 reference-device evidence (Desktop + Mobile Obsidian) PENDING — operator records sanitized rows per the documented procedure; automated gates cannot substitute | Before Child 9 acceptance closure | [handoff §5](2026-08-18-plugin-journal-small-file-sync.md) |
| 2026-08-20 | sources | Structural enforcement of the "sensitive value object must redact `__repr__`" contract is deferred — until a fourth class needs it, prefer repetition over a `RedactedString` mixin/Protocol. Reopen this row when a new sensitive-string value object is added without a redacted repr | When a fourth sensitive value object is added | [handoff §4](2026-08-14-source-version-publication.md) |
| 2026-08-24 | closed-reason-surfacing | Live smoke round of the remediation surfaces (wrong-origin auth tokens, stopped-worker staleness line, lifecycle rejection ring) — requires the user's stack; no completion claim until observed | Before Child 9 operations acceptance | [handoff §4](2026-08-24-closed-reason-surfacing-remediation.md) |
| 2026-08-24 | policy-observability | Live smoke round of the policy diagnostics surfaces (broken signer or stopped worker → `failed` evaluation counter + recent-failure ring + rejection-ring SYSTEM code + rotating log readback) — requires the user's participation; stack verified `stack_ready` 2026-08-24, so the gate is the user, not the environment | Before production activation | [handoff §4](2026-08-24-policy-observability-remediation.md) |
| 2026-08-24 | exclusion-policy | Unknown future `exclusion_policy_*` code silently no-ops out of the small-file rejection ring (`_record_policy_rejection` docstring contract, not runtime-enforced) — a new code must choose a SYSTEM/DENIAL side and extend the ring map in the same diff | Before the next exclusion-policy error code is added | [handoff §5.4](2026-08-24-policy-observability-remediation.md) |
| 2026-08-28 | multipart-upload | Physical Mobile matrix PENDING (mobile live test, needs a physical device) — record the four sanitized rows per the runbook; on the same live round, note whether the journey's re-fire recovery path (lost policy-fixture capture) triggered; also on that same round, re-verify the rebuild reconcile-first fix (a fresh journal over a non-empty vault reconciles first — [child-8 handoff](2026-08-29-device-sync-child8-unblock-smoke-prep.md)) | Before Child 9 operations acceptance | [handoff §deferred](2026-08-28-resumable-multipart-mobile-upload.md) |
| 2026-08-31 | web-admin (pre-existing) | Dead `dismissInitialTotpOffer` path browser-unreachable since 99fe1c3: `LoginForm.tsx` passes `onSkipped={undefined}` and no other surface passes it, so `TotpChallenge.skip()`, `AuthenticationClient.dismissInitialTotpOffer()` and its server action have no browser reachability and no e2e coverage (component tests mock `onSkipped`) | Before next web-admin surface change | [handoff](2026-08-31-web-auth-poll-pacing-and-admin-client-hardening.md#deferred-items) |
| 2026-09-01 | web-infra (pre-existing) | `poe verify`'s `typescript-test` step flakes with shifting jsdom 5s timeouts when the two pnpm workspace suites run concurrently on a busy machine; passes in isolation and under `npm_config_workspace_concurrency=1` (scheduling-only mitigation) | At next web tooling pin bump | [handoff §deferred](2026-09-01-canonical-correctness-and-migration-hygiene.md) |
