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
