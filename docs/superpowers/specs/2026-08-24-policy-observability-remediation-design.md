# Policy observability remediation — design spec

Date: 2026-08-24. Domains: exclusion policy (enforcement, metrics,
publication), small-file sync preflight, R2 object-storage spool, API CLI.
Governing rule: `AGENTS.md` (no silent swallowing) and
`docs/15-OBSERVABILITY_AND_ALERTING.md` §2/§3/§7.

## Problem

The 2026-08-24 expanded audit found five gaps (G1–G5) after the
closed-reason-surfacing remediation landed. G1–G3 form one cluster: the
policy subsystem's own failures are invisible in production — its metrics
are never bound, its fail-closed raises carry no event, and its errors are
actively collapsed into a success-shaped outcome at the preflight boundary.
G4/G5 are minor reason-token losses.

Note for future audits: the expanded audit's "P0 SyntaxError" finding on
`except A, B:` tuples was FALSE — the repo pins Python 3.14 (PEP 758 makes
the form valid and AST-identical; empirically verified and the API has been
serving with those files). Do not re-flag it; tooling running Python 3.12
mis-parses it.

### Findings (file:line at HEAD `17791c2`)

- G1 **Policy fail-closed failures are swallowed into `excluded` and record
  nothing.** `policy_not_initialized_error()` (enforcement.py:503-504,
  546-547) and `signing_corruption_error()` (enforcement.py:294-324) raise
  before any metric or event; `EvaluationMetricOutcome` has only
  allowed/excluded/indeterminate (metrics.py:49-54). The small-file
  preflight catch (small_file_sync/service.py:331-339 and :420-427) catches
  ALL `ExclusionPolicyError` and returns `outcome=EXCLUDED` — the device
  sees "excluded", and an operator cannot distinguish "policy denies this
  file" from "policy system broken".
- G2 **Policy metrics are dead in the serve graph.** server.py:158 and
  small_file_sync_composition.py:345 omit the metrics parameter, so the
  spec-21 counters (`exclusion_policy_evaluation_total{boundary,decision}`,
  `exclusion_policy_publication_total{outcome}`) record nothing in
  production; the publication service records no outcomes.
- G3 **Policy-guard failures bypass publication surfaces.**
  `SourceVersionPublicationService._publish` catches only
  `SourcePublicationError` (publication.py:262); an `ExclusionPolicyError`
  from `authorize_publication`/`authorize_bound_publication`
  (publication.py:289-294) produces no publication metric and no
  `SOURCE_VERSION_PUBLISH_FAILED/REJECTED` event (builders exist at
  publication.py:412-429, 537-552). A signing-unavailable outage during
  publish leaves no trail.
- G4 **`object_storage_busy` conflates three causes.** Free-space reserve
  (spool.py:392), admission-window expiry (:433), permit/budget exhaustion
  (:419-428) all carry the same closed code with no distinguishing reason
  token, unlike the `stream_invalid` pattern (:277-295).
- G5 **CLI swallows the unexpected-exception class.**
  `_run_protected_command` prints only `internal_error`
  (authentication_commands.py:194-196) — the exception class reaches
  nowhere.

## Goals

1. Policy SYSTEM failures (not-initialized, signing-unavailable) become
   distinguishable from policy DENIALS at every boundary: preflight
   outcome, metrics, log events, and the client envelope.
2. The spec-21 policy metrics actually record in the production serve
   graph and are readable from the Web Admin diagnostics family.
3. Policy-guard failures during publication emit the existing failed
   event/metric shapes with the closed code.
4. G4/G5: one closed reason token each, following established patterns.

## Non-goals

- No change to policy decision semantics: allowed/denied/indeterminate
  evaluation and their existing outcomes stay identical.
- No new dependencies; no Phase-10 exporters (Prometheus sink stays a
  documented boundary TODO).
- No retry/backoff changes anywhere — surfacing only (plus the corrected
  outcome routing in C1, which changes WHAT the client sees for system
  failures, not when/how the system retries).

## Contracts

### C1 Distinguish policy system failures at the preflight boundary (G1)

- Classification: `ExclusionPolicyError` codes split into SYSTEM codes
  (`exclusion_policy_not_initialized`, `exclusion_policy_signing_unavailable`)
  vs DENIAL codes (`exclusion_policy_denied`, `exclusion_policy_indeterminate`).
  The split lives in one place (exclusion_policy errors module) as a closed
  set so future codes must choose a side.
- Behavior at both preflight catch sites (service.py:331-339, :420-427):
  DENIAL codes keep today's behavior (outcome `EXCLUDED`); SYSTEM codes
  PROPAGATE as the typed error so the API's existing status mapping
  applies (409 / 503 envelopes). Plugin-side consequence (verify, do not
  re-implement): 503 maps to the retryable server_error family and parks
  with backoff; 409 maps per its code — check the wire table and record
  the mapping in tests.
- Both catch sites record the closed `error_code` into the small-file
  rejection diagnostics source (the established ring from 87c7941) in all
  cases — denial or system — so the operator surface always carries the
  why.
- `EvaluationMetricOutcome` gains a closed `failed` member; the
  fail-closed raises record it (with the closed code in the event fields)
  instead of recording nothing.

### C2 Bind and expose policy metrics (G2)

- server.py binds `InMemoryExclusionPolicyMetrics` at both composition
  sites (engine composition and small-file composition) — one shared
  instance.
- A read-only authenticated admin route (same family as the sync/lifecycle
  diagnostics routes) returns the evaluation counters (by boundary and
  decision, including `failed`), the publication outcome counters, and a
  bounded recent-failure ring carrying closed codes and timestamps.
- OpenAPI, generated client and contract tests updated per repo rules.

### C3 Emit policy-guard publication failures (G3)

- `_publish` catches `ExclusionPolicyError` and routes it through the
  EXISTING failure shapes: `SOURCE_VERSION_PUBLISH_FAILED` event with the
  closed `error_code` + a REJECTED/FAILED publication metric outcome (per
  the not-retryable semantics of the underlying code; system failures are
  not retried by the publication service today — preserve that), then
  re-raises unchanged for envelope rendering.

### C4 Distinguish spool busy causes (G4)

- The three `object_storage_busy` sites carry a closed `reason` token in
  `safe_details` (`spool_free_space`, `spool_admission_window_expired`,
  `spool_permits_exhausted`) mirroring the `stream_invalid` pattern; no
  code/status change.

### C5 Capture the CLI exception class (G5)

- `_run_protected_command` includes the exception CLASS NAME as a closed
  snake_case token in its failure line (e.g. `timeout_error`) via the
  emergency/internal-error path; no traceback, no message text.

## Privacy invariants (acceptance-critical)

- Closed tokens only (existing registry codes + the new tokens named
  above); no paths, digests, credentials, raw bodies, exception messages
  or provider details on any new surface.
- The admin route returns counters/rings with closed codes and timestamps
  only; forbidden-substrate scans extended to it.
- safe_details additions carry only the closed reason tokens above.

## Acceptance criteria

1. Each gap G1–G5 has a RED test proving the current invisibility (or
   wrong-collapse) then GREEN with the surface; C1 additionally pins the
   plugin-visible outcome mapping for 409/503 system codes end-to-end.
2. Denial semantics unchanged: existing allowed/denied/indeterminate tests
   pass verbatim.
3. Full offline gates green (Python unit+contract+mypy+ruff,
   `uv run poe verify`; plugin suites untouched unless the C1 mapping
   verification requires a wire-table test).
4. One live smoke check with the user when the stack is up: temporarily
   point the signer at a broken key (or stop the policy worker) and read
   the system-failure trail from the admin route + rotating log.

## Error cases

- Metrics sink unavailable: compositions fall back to a no-op sink
  (documented), never block evaluation.
- Admin route unauthorized: existing closed auth errors, mirroring the
  sibling diagnostics routes.
- safe_details validation rejects unknown reason tokens at construction —
  the three G4 tokens are added to the registry before use.
