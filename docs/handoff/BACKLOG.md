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
| 2026-08-18 | small-file-sync | Child-4 reference-device evidence (Desktop + Mobile Obsidian) PENDING — operator records sanitized rows per the documented procedure; automated gates cannot substitute | Before Child 9 acceptance closure | [handoff §5](2026-08-18-plugin-journal-small-file-sync.md) |
| 2026-08-20 | sources | Structural enforcement of the "sensitive value object must redact `__repr__`" contract is deferred — until a fourth class needs it, prefer repetition over a `RedactedString` mixin/Protocol. Reopen this row when a new sensitive-string value object is added without a redacted repr | When a fourth sensitive value object is added | [handoff §4](2026-08-14-source-version-publication.md) |
| 2026-08-24 | closed-reason-surfacing | Live smoke round of the remediation surfaces: wrong-origin token ✓, terminal cleared-reason ✓, staleness ✓ (all observed 2026-09-01) — REMAINING is the lifecycle rejection ring readback (locator conflict on restore), blocked by the three device-manifest recovery defects surfaced the same round (see the 2026-09-01 device-sync rows below) | After the device-manifest recovery fixes land | [handoff §4](2026-08-24-closed-reason-surfacing-remediation.md); [round handoff](2026-09-01-policy-diagnostics-metrics-sink-and-live-smoke.md) |
| 2026-08-24 | exclusion-policy | Unknown future `exclusion_policy_*` code silently no-ops out of the small-file rejection ring (`_record_policy_rejection` docstring contract, not runtime-enforced) — a new code must choose a SYSTEM/DENIAL side and extend the ring map in the same diff | Before the next exclusion-policy error code is added | [handoff §5.4](2026-08-24-policy-observability-remediation.md) |
| 2026-09-01 | device-sync | Delete+recreate at one locator leaves the repair barrier Blocked (`device_cursor_gap` after `device_manifest_local_diverged`/`identity_ambiguous` settles) and an explicit "Repair sync" does not clear it — the L1 tombstone-restore conflict could not be set up live (observed 2026-09-01 15:41Z on a healthy post-fix journal) | Before the next device-sync live round | [round handoff](2026-09-01-policy-diagnostics-metrics-sink-and-live-smoke.md) |
| 2026-09-01 | device-sync | Plugin login button derives the browser URL from `server_origin`: with the API-only origin the login flow is unusable, the web origin works (both serve sync) — derive the web origin for the browser flow or document the requirement | Before next plugin onboarding change | [round handoff](2026-09-01-policy-diagnostics-metrics-sink-and-live-smoke.md) |
| 2026-09-01 | small-file-sync | Large-vault cold sync is unsupported by design today: the outbound drain is serial per event (live-measured ~6–13s/event end-to-end through the durable Temporal commit) and capture fail-closes at 10,000 pending events — a bulk-import path (batch commits, parallel admission, or manifest-driven bulk mode) is the architectural lever; manifest side already pages 500/page | Before any large-vault onboarding is promised | [round handoff](2026-09-01-policy-diagnostics-metrics-sink-and-live-smoke.md) |
| 2026-08-28 | multipart-upload | Physical Mobile matrix PENDING (mobile live test, needs a physical device) — record the four sanitized rows per the runbook; on the same live round, note whether the journey's re-fire recovery path (lost policy-fixture capture) triggered; also on that same round, re-verify the rebuild reconcile-first fix (a fresh journal over a non-empty vault reconciles first — [child-8 handoff](2026-08-29-device-sync-child8-unblock-smoke-prep.md)) | Before Child 9 operations acceptance | [handoff §deferred](2026-08-28-resumable-multipart-mobile-upload.md) |
| 2026-08-31 | web-admin (pre-existing) | Dead `dismissInitialTotpOffer` path browser-unreachable since 99fe1c3: `LoginForm.tsx` passes `onSkipped={undefined}` and no other surface passes it, so `TotpChallenge.skip()`, `AuthenticationClient.dismissInitialTotpOffer()` and its server action have no browser reachability and no e2e coverage (component tests mock `onSkipped`) | Before next web-admin surface change | [handoff](2026-08-31-web-auth-poll-pacing-and-admin-client-hardening.md#deferred-items) |
| 2026-09-01 | web-infra (pre-existing) | `poe verify`'s `typescript-test` step flakes with shifting jsdom 5s timeouts when the two pnpm workspace suites run concurrently on a busy machine; passes in isolation and under `npm_config_workspace_concurrency=1` (scheduling-only mitigation) | At next web tooling pin bump | [handoff §deferred](2026-09-01-canonical-correctness-and-migration-hygiene.md) |
