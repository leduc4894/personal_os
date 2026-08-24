# Journal Orchestration Observability Tasks

Plan: `docs/superpowers/plans/2026-08-24-journal-orchestration-observability.md`

1. Add typed closed-token reporter and vocabulary tests.
2. Surface retry-scheduler and status-projection read failures once per session.
3. Surface rejected queue and automatic-snapshot drains without changing their
   settled-promise behavior.
4. Surface capture admission, automatic-scan and reconcile-persistence
   failures while retaining fail-closed semantics and bounded emission.
5. Update the sync diagnostics runbook, record one handoff, and pass the full
   verification gate.

Done only when every token has a behavioral/source-contract test, the final
full gate passes, and no deferred item exists without a concrete `BACKLOG.md`
trigger.
