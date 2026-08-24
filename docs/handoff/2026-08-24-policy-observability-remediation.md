# Policy Observability Remediation Handoff

**Date:** 2026-08-24
**Plan:** `docs/superpowers/plans/2026-08-24-policy-observability-remediation.md`
**Spec:** `docs/superpowers/specs/2026-08-24-policy-observability-remediation-design.md`
**Branch:** `policy-observability-remediation` (user ruling 2026-08-24: new
branch, no worktree — overrides the plan's branch note). Base `17791c2`;
branch cut from `024c236` (the docs commit adding plan+spec).
Implementation: Tasks 1–4 in `66bbe0a` (policy SYSTEM/DENIAL split),
`98ef293` (diagnostics binding + admin route), `15c562a` (policy-guard
publication failures), `da24abe` (spool busy tokens + CLI class token) —
each reviewed clean. **Final commit of the plan: the docs commit that
carries this handoff** (Task 5, documentation and closure) — same
convention as the 2026-08-23/24 handoffs. Per-task RED/GREEN evidence
lives in the SDD reports under
`.superpowers/sdd/2026-08-24-policy-observability-remediation/`.

Living operator surfaces: `docs/operations/sync-error-tracing.md` (extended
by Task 5: policy diagnostics route, SYSTEM/DENIAL rejection-ring split,
busy reasons, CLI token, smoke-round class 4) and
`docs/operations/exclusion-policy-publication.md` (evaluation-metric
`decision` vocabulary now includes `failed`; diagnostics route listed).

**Status: all offline gates GREEN (Tasks 1–4, review clean). The plan's
live smoke round (spec acceptance criterion 4, "with the user") is
PENDING — deferred to user participation; the read-only stack evidence of
§4 shows the local stack READY, so the blocker is the user's
participation, not stack availability. No live completion claim may be
made until it runs.**

## 1. What was built (one paragraph per task)

- **Task 1 (`66bbe0a`) — classify and surface policy system failures
  (C1/G1).** Closed SYSTEM (`exclusion_policy_not_initialized`,
  `exclusion_policy_signing_unavailable`) vs DENIAL (`exclusion_policy_denied`,
  `exclusion_policy_indeterminate`) split in one place
  (`exclusion_policy/errors.py`, `is_policy_system_failure`); both
  small-file preflight catch sites re-routed — DENIALS keep the 200
  `excluded` outcome, SYSTEM codes PROPAGATE as the typed 409/503 errors;
  the rejection ring records the closed code in ALL four cases; the
  `failed` evaluation outcome added with `error_code` on the record,
  recorded at every fail-closed raise (both boundaries, both shapes);
  plugin wire-table rows pin 409/503 → retryable `server_error` with
  bounded backoff; OpenAPI/client regenerated (enum widening).
- **Task 2 (`98ef293`) — bind and expose policy metrics (C2/G2).** One
  shared `InMemoryExclusionPolicyMetrics` bound at both composition sites
  in the serve graph (documented no-op fallback when absent);
  `ExclusionPolicyDiagnostics` read side (exact evaluation counters incl.
  `failed`, publication counters, bounded 50-record failure ring of closed
  `{boundary, error_code, at_epoch_ms}`); new authenticated read-only
  `GET /api/admin/exclusion-policy/diagnostics` in the diagnostics route
  family (session gate, canonical envelope, `no-store`,
  `extra="forbid"` models); route vocabulary, OpenAPI snapshot, generated
  client, route-set pins and forbidden-substrate leak journey extended.
- **Task 3 (`15c562a`) — emit policy-guard publication failures
  (C3/G3).** `_publish` gains an `except ExclusionPolicyError` clause that
  routes through the EXISTING surfaces — one `SOURCE_VERSION_PUBLISH_FAILED`
  event carrying the closed registry code/category/retryability, plus the
  terminal `REJECTED` publication metric for not-retryable codes — then
  bare-`raise`s the same instance; builder parameter types widened to
  `ApplicationError`; no retry/backoff change; spec-10.3 business-rejection
  vocabulary deliberately untouched.
- **Task 4 (`da24abe`) — spool busy reason tokens + CLI class token
  (C4/G5).** `object_storage_busy` accepts the `reason` detail
  (registry-widened); three closed tokens at the three spool sites —
  `spool_free_space` (reserve check), `spool_permits_exhausted`
  (`_AdmissionWindowExpired` after a capacity wait), `spool_admission_window_expired`
  (outer `wait_for` timeout); `_run_protected_command` prints the
  exception CLASS as a closed snake_case token
  (`personal-api: internal_error: timeout_error`, exit 70) via a bounded
  alphabet/length reduction of `type(error).__name__` — no message, no
  traceback.
- **Task 5 (this docs commit)** — runbook extension (sanitized), canonical
  `failed`-vocabulary update, this handoff, six BACKLOG rows (§5). The
  plan-mandated live smoke round is NOT executed (requires the user);
  read-only stack evidence recorded in §4 instead.

## 2. Gate evidence (final runs, per task reports)

Task 1 (`66bbe0a`):

- RED: classification ImportError; enforcement `AttributeError: no
  attribute 'FAILED'` (6 failed); small-file `DID NOT RAISE
  ExclusionPolicyError` collapse proof at both catch sites (6 failed).
- GREEN focused: `75 passed in 0.36s`; focused suites `612 passed,
  1 deselected` and `785 passed, 2 skipped`; mypy strict `Success … 180
  source files`; ruff + format (459 files) clean; `poe api-contract-check`
  exit 0; plugin `tsc --noEmit` exit 0, wire-table rows `44 passed`.

Task 2 (`98ef293`):

- RED: `12 failed, 11 passed, 10 errors` (missing kwargs, unbound sink,
  `api_route_not_found` on the new route) → GREEN direct set
  `33 passed, 1 warning`.
- Full python suite: `3422 passed, 21 skipped, 398 deselected` (integration
  gated behind the disposable `knowledge-ci-*` project by design); mypy
  strict `182 source files`; ruff/format (463 files) clean;
  `api-contract-check` current; `import-boundaries`: `Contracts: 5 kept,
  0 broken`; TS gates Done; plugin suite `691 passed` (one pre-existing
  timing flake passed on re-run, no plugin/journal files touched by T2's
  diff — the T1 plugin-test rows are behavior pins).

Task 3 (`15c562a`):

- RED: `3 failed` on the empty diagnostics sink (guard error bypassed
  every publication surface — the G3 proof) → GREEN `3 passed` (file run
  `22 passed`); `tests/unit/sources` `179 passed`; cross-domain
  `375 passed, 1 deselected`; `362 passed`; mypy strict 182 files; ruff
  clean; the one `StarletteDeprecationWarning` verified pre-existing on
  the stashed clean tree.

Task 4 (`da24abe`):

- RED: `8 failed, 65 passed, 3 skipped` (three `assert None ==
  '<token>'` busy proofs, the allowlist param, three CLI token asserts) →
  GREEN `75 passed, 3 skipped`; focused suites `220 passed, 3 skipped`;
  full offline suite `3431 passed, 21 skipped, 398 deselected`; mypy
  strict 182 files; ruff clean; format 463 files. No OpenAPI regeneration
  needed (`safe_details` values are not enumerated on the wire).

Task 5 (this commit):

- `uv run poe stack-status` → **exit 0, `result_code: "stack_ready"`**
  (evidence in §4). Documentation-only diff: no Python/TS source touched,
  so no test gates were re-run for this commit; the markdown was
  lint-checked by inspection against the runbook's existing style.

## 3. Interpretive decisions (with reasons)

1. **`error_code` rides the record, not the metric label (Task 1).**
   Spec C1's "record it (with the closed code in the event fields)" became
   `EvaluationRecord.error_code`; the spec-21 label contract stays exactly
   `{boundary, decision}` so label cardinality stays closed, and Task 2's
   admin ring reads the records. A separate structured-log event does not
   exist in the package and would have been new surface beyond the spec.
2. **A propagated SYSTEM failure records no preflight outcome (Task 1).**
   A raise is not a completed preflight, so `small_file_preflight_total`
   gains no row; the rejection-ring entry (with the closed code) is the
   small-file trace. Pinned by `preflight_count(..., EXCLUDED) == 0`
   assertions at both catch sites.
3. **Publication outcome branches on the registry's `is_retryable`
   (Task 3).** Not-retryable (every guard-raisable code today: denied,
   indeterminate, not_initialized, signing_unavailable) → terminal
   `REJECTED` metric; a retryable registry code → the existing metric-free
   failed shape so a terminal outcome can never double-count a later
   retry. The sources `PublicationMetricOutcome` has no `failed` member —
   the "FAILED family" is the metric-free retryable shape, hence the
   branch is metric vs no-metric.
4. **The flipped `REJECTED == 0 → == 1` assertion is C3-mandated
   (Task 3).** `test_excluded_source_never_calls_object_store`'s old
   counter assertion pinned the G3 gap itself; spec C3 requires the
   terminal outcome for policy verdicts. "Denial semantics unchanged"
   binds the evaluation decision, not the publication trail — every
   denial-semantics assertion in that test (typed error, zero port calls,
   stream unread) passed verbatim.
5. **Busy-token mapping of the two admission timeouts (Task 4).**
   `_AdmissionWindowExpired` (the loop's deadline check, reachable only
   after a capacity wait — the permit/budget-exhaustion path) →
   `spool_permits_exhausted`; the outer `asyncio.wait_for` `TimeoutError`
   (the audit's admission-window-expiry site) →
   `spool_admission_window_expired`. The paren-free `except A, B:` form
   binds no exception object to branch on, so the single handler was split
   into two — not a style fix, a token-routing requirement.
6. **CLI class token by bounded reduction, not registry membership
   (Task 4).** The token derives from `type(error).__name__` through an
   alphabet/length bound with an `unknown_error` fallback — closed by
   construction (SafeToken-grammar-safe), deterministic, and sufficient
   for a print-only line; a registry membership for arbitrary exception
   classes would be new vocabulary surface the spec did not ask for.
7. **Live smoke round deferred with stack evidence (Task 5).** The plan
   mandates the round runs "with the user"; that gate requires user
   participation and is deferred (BACKLOG row, §5.1). What was executed:
   the read-only `uv run poe stack-status` (first step of the
   `.local/RESTART.md` order — no service started/stopped/mutated). The
   result (§4) shows the stack READY, so the deferral's cause is user
   availability, not environment.

## 4. PENDING: the live smoke round (spec acceptance criterion 4)

Not run — it requires the user's participation. **The plan stays open and
no live claim of completion may be made until it runs.** Nothing in the
round may be simulated, mocked or substituted (AGENTS.md live-test rules).

Read-only stack evidence captured 2026-08-24 (Task 5), command
`uv run poe stack-status`, exit 0:

```json
{"initializers":{"postgres-provision":{"exit_code":0,"state":"exited"},
"temporal-namespace-bootstrap":{"exit_code":0,"state":"exited"},
"temporal-schema-setup":{"exit_code":0,"state":"exited"}},
"project":"knowledge-local","result_code":"stack_ready",
"services":{"neo4j":{"health":"healthy","state":"running"},
"postgresql":{"health":"healthy","state":"running"},
"qdrant":{"health":"healthy","state":"running"},
"redis":{"health":"healthy","state":"running"},
"temporal":{"health":"healthy","state":"running"},
"temporal-cli":{"health":"healthy","state":"running"},
"temporal-ui":{"health":"healthy","state":"running"}},
"state":"ready"}
```

Scope note: `stack-status` reports the compose services of
`knowledge-local` only; the API serve and the two policy workers are
launched separately per `.local/RESTART.md` (`.local/serve-local.sh`,
`.local/run-worker.sh`) and are NOT covered by this read-only diagnostic —
starting them is the smoke round's own first step, with the user.

The round itself (runbook class 4, `docs/operations/sync-error-tracing.md`):

1. Follow `.local/RESTART.md` exactly (`stack-status` → serve-local → Web
   Admin 38000 → workers → existing tunnel). No other launch path.
2. Trigger: temporarily point the signer at a broken key (or stop the
   policy worker), then drive one content operation.
3. Readback: `GET /api/admin/exclusion-policy/diagnostics` shows a
   `failed` evaluation counter row and `recent_failures` entries with the
   closed code; `GET /api/admin/sync/rejections` carries the SYSTEM code;
   the rotating API diagnostics log (`.local/runtime-logs/`) holds the
   typed exchange. Restore the signer/worker afterwards.
4. Record the evidence sanitized exactly like the runbook's examples
   (closed tokens, counts, timestamps — no paths/digests/content).

## 5. Deferred items (verdicts; one BACKLOG row each)

1. **The live smoke round** (§4). Verdict: defer — user gate.
   **Implement by: before production activation** (the milestone where
   these operator surfaces get their acceptance pass; the stack is ready,
   so the trigger is the user's participation, not environment).
2. **No production metrics sink** — the in-memory recorder bound in the
   serve graph is the only spec-21-compliant sink; the spec's non-goals
   keep the Prometheus sink a documented boundary TODO. Verdict: defer per
   spec. **Implement by: before the first metrics exporter/sink lands**
   (any PR introducing an exporter resolves or explicitly re-anchors this
   row first).
3. **Exclusion-policy test-coverage batch**: direct tests for the
   `_validate_evaluation_error_code` ValueError branches; the
   indeterminate-not-recorded-as-`failed` combination pinned beside the
   definite-denial test; one test walking a real fail-closed evaluation
   end-to-end into the diagnostics route payload (route tests seed the
   recorder directly today); a parametrize over the four guard-raisable
   codes through `_publish` (only denial is directly tested).
   **Implement by: at next exclusion-policy diagnostics change.**
4. **Unknown future `exclusion_policy_*` code silently no-ops out of the
   small-file rejection ring** — `_record_policy_rejection` ignores codes
   outside its four-code map (docstring contract, not runtime-enforced;
   practically unreachable at today's boundaries). Verdict: defer —
   forward-compat hazard only. **Implement by: before the next
   exclusion-policy error code is added** (that change must choose a side
   per C1 and extend the ring map in the same diff).
5. **`InMemoryExclusionPolicyMetrics` increments are unsynchronized** —
   same shape as the sibling in-memory recorders; single-loop synchronous
   use today. Verdict: defer. **Implement by: before multi-worker serve**
   (mirrors the existing web-auth multi-worker row).
6. **`run_policy_key_command` still prints bare `internal_error`** in its
   own `except Exception` (`exclusion_policy_commands.py`, the
   `personal-api policy-key` dispatch) — same G5 defect class, outside
   C5's named scope (the authentication commands). Verdict: defer.
   **Implement by: at next policy-key CLI change.**

Accept-as-is (ledger observations, no row): the failure ring's closed
`boundary` label beyond the spec's literal "codes and timestamps" (ruled
acceptable in the Task 2 review — mirrors the counters' boundary key,
stays within the closed vocabulary); a recorder failure inside
`_record_policy_guard_failure` would mask the original policy error
(pre-existing pattern parity with `_record_failure`); the retryable leg of
`_record_policy_guard_failure` is unreachable today and pinned via
synthetic `SNAPSHOT_OUTDATED` injection through the fake guard (becomes
reachable only under row 4's trigger domain); busy-reason value closedness
via module constants rather than a registry value-enum (the
`stream_invalid` convention C4 mandates mirroring).

## 6. Next actions

1. Run the live smoke round (§4) with the user; record sanitized evidence
   in the operator record; retire BACKLOG row §5.1. Until then the plan's
   acceptance criterion 4 stays open.
2. Merge `policy-observability-remediation` after whole-branch review; the
   operator surfaces are already documented in the runbook and the policy
   operations doc (nothing lands undocumented).
3. The remaining BACKLOG rows (§5.2–5.6) wait on their own triggers;
   nothing in this plan blocks on them.

## 7. Linked living documents

- Operations runbook: `docs/operations/sync-error-tracing.md`
- Policy domain operations: `docs/operations/exclusion-policy-publication.md`
- Local restart runbook: `.local/RESTART.md` (never copy its details)
- Deferred-work index: `docs/handoff/BACKLOG.md`
- Spec: `docs/superpowers/specs/2026-08-24-policy-observability-remediation-design.md`
