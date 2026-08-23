# Plugin sync status recovery handoff

## State

- Last commit inspected: `90cd72c`.
- The focused WDIO journey now reproduces the exact recovery sequence:
  policy block observed, allowed policy reauthorization completed, then the
  real `Sync existing files` confirmation clicked. Its retained phase stops
  before journal recovery, so this remains an active defect rather than a
  completed live gate.
- The startup reconciliation now reports `refresh_required`, rather than
  `connected`, while only a durable refresh credential exists. The later
  successful refresh remains the sole path to `connected`.

## Evidence

- Targeted authentication test: 22 passed.
- Focused plugin composition test and bootstrap contract test pass after the
  bounded post-scan drain changes; type check and plugin build pass.
- Live journal aggregate is `43` policy-terminal rows made at policy revision
  `0`, `1` policy-terminal row made at revision `1`, one committed row and one
  preflight row. The policy cache is valid and contains only the `.tmp` rule;
  an in-memory Markdown probe evaluates as allowed.
- The attempt audit contains only three prior `login_required` outcomes. No
  server policy-denial attempt is present.

## Decisions

- Do not delete or rewrite the user's journal rows. They are durable audit
  evidence and a reset would hide the fault rather than validate recovery.
- Do not treat the visible `Policy blocked` label as current server denial:
  it is currently driven by terminal historical rows. A confirmed re-admission
  scan must create successors before the status projection can clear them.
- `Sync existing files` is a complete operator action: it must schedule its
  own bounded drain, including after an already-running onboarding pass yields.

## Next actions

1. Repeat the retained-phase WDIO gate from a clean `knowledge-ci-*` stack
   after the bounded drain implementation settles; require
   `policy_recovery_journey_completed`.
2. Keep the new spec and plan as the source of implementation detail; do not
   mark Desktop live acceptance complete until its final retained phase and
   sanitized server/journal evidence agree.

---

# Close-out addendum (2026-08-23)

This handoff's defect lineage is closed by the automatic vault convergence
plan (`docs/superpowers/plans/2026-08-22-automatic-vault-convergence.md`,
spec `docs/superpowers/specs/2026-08-22-automatic-vault-convergence-design.md`).

- `Sync existing files` and `Sync now` no longer exist: convergence is
  automatic through startup / policy-accepted / revision-advanced snapshot
  triggers, bounded passes, deadline continuation and a one-shot scheduled
  retry trigger. The manual recovery action this handoff prescribes is
  superseded by design; the operations guides
  (`docs/operations/plugin-journal-small-file-sync.md`,
  `docs/operations/source-locator-tombstone-lifecycle.md`) document the
  automatic contract.
- The WDIO gate named in Next actions above was replaced by a user ruling
  (2026-08-22): live verification ran user-assisted on the real Vault with
  DevTools, no WDIO journey.
- Sanitized live evidence is recorded in the close-out addendum of
  `docs/handoff/2026-08-22-automatic-vault-convergence.md`: 52 committed,
  9 `no_change`, 2 events parked `waiting_retry` retrying with a working
  exponential backoff, and four stacked root causes fixed and verified
  live. The stale policy-block projection this handoff chased now settles
  automatically after verified policy acceptance.
- Both Next actions above are resolved or superseded; no further work is
  tracked under this handoff.
