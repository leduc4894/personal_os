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

## Active deferred work

These items have a current implementation path or an outstanding acceptance
obligation. Their `Implement by` values are delivery gates, not reasons to
wait before starting work.

| Added | Domain | Item | Implement by | Details |
|---|---|---|---|---|
| 2026-08-18 | small-file-sync | Child-4 reference-device evidence (Desktop + Mobile Obsidian) PENDING — operator records sanitized rows per the documented procedure; automated gates cannot substitute | Before Child 9 acceptance closure | [handoff §5](2026-08-18-plugin-journal-small-file-sync.md) |
| 2026-08-24 | closed-reason-surfacing | Live smoke round of the remediation surfaces: wrong-origin token ✓, terminal cleared-reason ✓, staleness ✓ (all observed 2026-09-01) — REMAINING is the lifecycle rejection ring readback (locator conflict on restore); the 2026-09-03 re-fire was blocked BEFORE any journey step by a new device-sync defect (row below): ordinary note create→move→rename hard-stops reconcile and Repair sync cannot restart; stack/readback path/two-vault fixture are proven, so the re-fire after the fix is short | Before Child 9 operations acceptance | [handoff §4](2026-08-24-closed-reason-surfacing-remediation.md); [round handoff](2026-09-01-policy-diagnostics-metrics-sink-and-live-smoke.md); [block handoff](2026-09-03-lifecycle-rejection-ring-live-readback.md) |
| 2026-09-03 | device-sync | Untitled-transit create→move→rename hard-stops the journal on the real stack and repair cannot restart it — capture-side root cause FIXED (rename/delete settle racing an in-flight create now defers bounded instead of flagging global reconcile_required, commit `29f65f5`, full plugin suite green); REMAINING is the reconciler-side repair deadlock: first Repair sync blocks at `actions` with `device_cursor_gap`, the automatic recovery re-mints a run whose checkpoint still cannot fit the apply lattice (`base_ack=1 < checkpoint=2`, run resting `applying`), and a further Repair sync mints-then-refuses at the start binding check (`device_manifest_state_invalid`) leaving an abandoned `collecting` run | Before Child 9 operations acceptance | [defect handoff](2026-09-03-manifest-repair-quiet-checkpoint-deadlock.md); [block handoff](2026-09-03-lifecycle-rejection-ring-live-readback.md) |
| 2026-08-28 | multipart-upload | Physical Mobile matrix PENDING (mobile live test, needs a physical device) — record the four sanitized rows per the runbook; on the same live round, note whether the journey's re-fire recovery path (lost policy-fixture capture) triggered; also on that same round, re-verify the rebuild reconcile-first fix (a fresh journal over a non-empty vault reconciles first — [child-8 handoff](2026-08-29-device-sync-child8-unblock-smoke-prep.md)) | Before Child 9 operations acceptance | [handoff §deferred](2026-08-28-resumable-multipart-mobile-upload.md) |
| 2026-09-02 | source-conflicts | Operator Desktop Conflict Inbox journey PENDING (manual gate deferred by operator decision 2026-09-02): re-stand the CI project (`CI=true bash .local/serve-live-ci.sh up knowledge-ci-source-conflicts-20260902`, plus `tools/obsidian_live_acceptance_bootstrap.py` for the TOTP/policy HTTP half if needed), run the 8-step runbook plus the final review's 6 additional exercises (keep-remote/keep-local live, stale-successor loop, local-apply recovery, capture lane, binary render, policy recheck) on a dedicated Vault test, record sanitized evidence (outcome, reason token, count, timestamp), then `serve-live-ci.sh down` | Before Child 9 operations acceptance | [handoff §deferred](2026-09-02-source-conflict-capture-and-resolution.md); runbook `docs/operations/source-conflict-resolution.md` |

## Feature gaps

These are intentionally unsupported product capabilities, not defects in the current contract. They need a new approved product/design plan before work begins, and must land by their stated customer or operational commitment.

| Added | Domain | Item | Implement by | Details |
|---|---|---|---|---|
| 2026-09-01 | small-file-sync | Large-vault cold sync is unsupported by design today: the outbound drain is serial per event (live-measured ~6–13s/event end-to-end through the durable Temporal commit) and capture fail-closes at 10,000 pending events — a bulk-import path (batch commits, parallel admission, or manifest-driven bulk mode) is the architectural lever; manifest side already pages 500/page | Before any large-vault onboarding is promised | [round handoff](2026-09-01-policy-diagnostics-metrics-sink-and-live-smoke.md) |

## Trigger-based deferred work

These items have no current implementation obligation. Reopen them only when
the stated trigger occurs; then handle them in the same change or dedicated
follow-up required by that trigger.

| Added | Domain | Item | Implement by | Details |
|---|---|---|---|---|
| 2026-08-15 | api-contract | `openapi-typescript@7.13.0` peer-declares `typescript@^5.x` while the workspace pins `6.0.3` (standing install warning; resurface on any pin bump) | At next TypeScript pin bump | [handoff §1](2026-08-15-api-runtime-contract-foundation.md) |
| 2026-08-20 | sources | Structural enforcement of the "sensitive value object must redact `__repr__`" contract is deferred — until a fourth class needs it, prefer repetition over a `RedactedString` mixin/Protocol. Reopen this row when a new sensitive-string value object is added without a redacted repr | When a fourth sensitive value object is added | [handoff §4](2026-08-14-source-version-publication.md) |
| 2026-08-24 | exclusion-policy | Unknown future `exclusion_policy_*` code silently no-ops out of the small-file rejection ring (`_record_policy_rejection` docstring contract, not runtime-enforced) — a new code must choose a SYSTEM/DENIAL side and extend the ring map in the same diff | Before the next exclusion-policy error code is added | [handoff §5.4](2026-08-24-policy-observability-remediation.md) |
| 2026-09-02 | backup/source-conflicts | The new `knowledge.source_conflicts` canonical table is absent from the backup snapshot's fixed `SNAPSHOT_LOCK_ORDER` (`packages/postgresql-source-store/src/postgresql_source_store/backup_snapshot.py`, manifest v4 stays at 35 tables), so conflict rows fall outside the canonical snapshot's quiescing lock order — extend the order when the backup manifest is next versioned | Before the next backup-manifest version bump (v5) | [handoff §deferred](2026-09-02-source-conflict-capture-and-resolution.md) |
| 2026-09-03 | vault-mutation | Follow-up hardening plan for the shared atomic vault mutation primitive: cause-bearing `AtomicVaultMutationFailure` (read-throw vs fingerprint mismatch, target-absent distinction or `requireOccupiedTarget` flag — removes the writer wrapper's positional null-read heuristic and the prove_base read-throw→divergence-token nuance), a formal pre-first-visible-mutation hook retiring the sequencing wrapper's implicit protocol, the `conflict_sibling_cleanup_failed` closed token for ignored cleanup refusals, and dedup of the writer's residual `hashesTo`/`#restoreRollback` | Before the next atomic-vault-mutation contract extension | [handoff §next-actions](2026-09-03-conflict-vault-apply-hardening.md) |
| 2026-09-03 | source-conflicts | A conflict apply replace-stage failure leaves one exact-token rollback sibling un-reclaimed by retries (data-preserving, spec req 3 compliant — the retry takes the created shape over an absent target); reclaiming requires a recovery sweep with the naming-contract proof | When a conflict vault-apply recovery sweep is designed | [handoff §deferred](2026-09-03-conflict-vault-apply-hardening.md) |
