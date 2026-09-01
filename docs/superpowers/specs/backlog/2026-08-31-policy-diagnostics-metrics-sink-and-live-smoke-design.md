# Policy diagnostics metrics sink and live smoke — design spec

Date: 2026-08-31. Domains: exclusion-policy observability, policy workers
(reconciliation liveness), policy-key CLI, CI stack workflows. Governing
docs: `docs/15-OBSERVABILITY_AND_ALERTING.md` (sink authority), the
2026-08-24 policy-observability remediation spec/handoff (§4, §5.2, §5.6),
the 2026-08-24 closed-reason-surfacing remediation handoff (§4, §5.3), and
the 2026-08-16 web-auth handoff §15 (CI consistency pins).

## Purpose and scope

Retire five indexed BACKLOG rows plus one ride-along, all sharing one
operations-readiness theme — no Child 8/9 work, no mobile:

1. 2026-08-24 policy-observability §5.2 — no production metrics sink; the
   in-memory recorder bound in the serve graph is the only spec-21-
   compliant sink (Prometheus exporter is a documented boundary TODO).
   Gate: before the first metrics exporter/sink lands — this effort IS
   that landing, so it resolves the row rather than re-anchoring it.
2. 2026-08-24 closed-reason-surfacing §5.3 — reconciliation intent stuck
   `leased` has no staleness verdict; needs a domain-defined bound, not an
   invented one. Gate: before production activation.
3. 2026-08-24 policy-observability §5.6 — `run_policy_key_command` prints
   bare `internal_error` in its own `except Exception`
   (`exclusion_policy_commands.py`).
4. 2026-08-24 closed-reason-surfacing §4 — live smoke round of the
   remediation surfaces (wrong-origin auth tokens, stopped-worker
   staleness line, lifecycle rejection ring). Gate: before Child 9
   operations acceptance.
5. 2026-08-24 policy-observability §4 — live smoke round of the policy
   diagnostics surfaces (broken signer or stopped worker → `failed`
   evaluation counter + recent-failure ring + SYSTEM rejection code +
   rotating log readback). Gate: before production activation.
6. Ride-along — 2026-08-16 ci-workflows (pre-existing): stack workflows
   other than `authentication-acceptance.yml` lack the mutual
   project-name/guard consistency pins. Gate: before next stack workflow
   change.

Out of scope: the exclusion-policy diagnostics test batch and recorder
synchronization (owned by the 2026-08-31 exclusion-policy acceptance
spec), the mobile matrices, and the worker dispose/close observations
already ruled duplicated-domain.

## Problem

Two remediation specs shipped operator surfaces that have never been
observed live: their acceptance criterion 4 (a live smoke round) is the
one open gate on both plans, blocked only on a live round with the user —
the stack verified `stack_ready` on 2026-08-24. The policy subsystem still
has no production metrics sink, so its spec-21 counters are readable only
through the admin diagnostics route. Reconciliation intents have no
honest staleness verdict because no domain execution bound exists (leases
cover only the workflow-start call, 60 s, reclaimed by any live worker's
next cycle; `dispatched` is the healthy resting state — an age bound on it
would false-positive). The policy-key CLI still loses the exception class
the authentication CLI was fixed to surface. And the stack workflows never
received the consistency pins `authentication-acceptance.yml` established.

## Compatibility contract

- The sink follows `docs/15-OBSERVABILITY_AND_ALERTING.md` as authority
  for exposition model, endpoint and auth; the in-memory recorder stays
  the recording source — the sink renders, it does not become a second
  recorder. Counters keep the closed spec-21 vocabulary (boundary,
  decision, outcome labels unchanged).
- Row 2's terminus is one of: (a) a domain-defined execution bound or
  heartbeat for reconciliation intents, introduced with scheduling
  hardening, from which the staleness verdict derives; or (b) an explicit
  code-stands ruling recorded in the handoff with evidence that the
  preview staleness surface already detects a dead worker for the same
  sweep class. No invented constant (handoff grounding stands).
- Row 3 mirrors the ratified G5/C5 pattern: exception class name as a
  closed snake_case token in the failure line; no traceback, no message
  text.
- CI pins replicate the established pattern (run-derived
  `knowledge-ci-*` project name, shape+length validation,
  `--project-name`/`--confirm-project` mutual pin, label-filtered
  teardown) onto the remaining stack workflows; job triggers and gate
  content are otherwise unchanged.
- No wire/OpenAPI change is required by any row; the sink's exposure
  surface follows whatever docs/15 already prescribes (admin-route family
  or metrics endpoint — per docs/15, not invented here).

## Contracts

### C1 First production metrics sink

The spec-21 policy counters (evaluation by boundary/decision including
`failed`, publication outcomes) become readable through a production
sink conforming to docs/15. The boundary TODO notes in the remediation
spec's non-goals and the runbook are replaced by the shipped surface.
Sink unavailability never blocks evaluation (composition falls back to
the in-memory recorder, documented).

### C2 Reconciliation `leased` staleness verdict

Produce the terminus: implement the domain-defined bound/heartbeat and
its verdict surface, or close the row with the recorded code-stands
ruling and evidence. Either way the BACKLOG row leaves the index with a
terminal disposition — the "no honest verdict yet" state does not
survive.

### C3 Policy-key CLI exception-class token

`run_policy_key_command`'s `except Exception` failure line carries the
exception class as a closed snake_case token via the same emergency/
internal-error path the authentication commands use. A CLI test pins the
token for an injected unexpected exception.

### C4 The single live smoke round (both checklists)

One live round executes both remediation specs' acceptance criterion 4
checklists:

- Policy-observability §4: trigger broken signer or stopped policy
  worker; read back the `failed` evaluation counter row and
  `recent_failures` closed codes from
  `GET /api/admin/exclusion-policy/diagnostics`, the SYSTEM code from
  `GET /api/admin/sync/rejections`, and the typed exchange in the
  rotating API diagnostics log; restore afterwards.
- Closed-reason §4: wrong-origin A-class token (settings connection
  detail line + terminal "Last cleared reason"), W3 staleness (stop the
  preview worker past the 15-minute bound; `stale_running_previews`
  carries the reason and age), L1 lifecycle rejection ring (typed 4xx →
  `GET /api/admin/source-lifecycle/rejections` matches the plugin
  trail's parked outcome). W1 dispatch events only if a worker
  diagnostics sink is enabled.

The round follows `.local/RESTART.md` / the `serve-live-ci.sh` contract
exactly (read-only reads may stay on `knowledge-local`; any write journey
moves to a disposable `knowledge-ci-*` project first). Nothing is
simulated, mocked or substituted; both rows retire only on observed,
sanitized evidence (closed tokens, counts, timestamps — no paths,
hostnames, credentials or content). Before the round, verify the current
state of the two §5.1/§5.2 prerequisites the closed-reason handoff named
(worker rotating-file sink; Web Admin UI rendering decision for
`worker_stale_running` and the lifecycle rejections route) — they are not
currently indexed; if either turns out unlanded, land it as part of this
effort's prep, not a new deferral.

### C5 Stack-workflow consistency pins (ride-along; verified present)

Plan-time verification: `canonical-core-acceptance.yml`,
`canonical-postgresql-baseline.yml`, `exclusion-policy-acceptance.yml`
and `local-service-stack.yml` already carry the full pin set (run-derived
`knowledge-ci-*` project name, guard regex, 63-length check,
`--project-name`/`--confirm-project`, label-filtered teardown) — later
waves landed them after the 2026-08-16 row was indexed; the row is stale.
`object-storage-live.yml` runs no compose project, so the pin set does
not apply. The deliverable is therefore: verify each workflow against
the pin contract with evidence, extend `tests/contract/test_ci_security.py`
prefetch coverage to include `exclusion-policy-acceptance.yml` (today's
tuple omits it), and retire the row with the verification record.

## Privacy invariants (acceptance-critical)

- The sink exposes closed-vocabulary counters and timestamps only; the
  forbidden-substrate scan scope extends to it.
- Smoke evidence and any UI/endpoint additions carry closed tokens and
  counts only; no raw content, queries, vectors, tokens or secrets.
- No credential, tunnel hostname or secret value is printed by the round
  or its records.

## Acceptance criteria

1. C1: sink renders the closed counters read-only; a contract test pins
   the exposure shape; fallback-on-sink-failure documented and tested.
2. C2: terminus recorded in the handoff with either the implemented
   bound + its test, or the code-stands evidence.
3. C3: CLI test pins the closed class token.
4. C4: the round runs to completion with sanitized evidence recorded in
   the operator records; BOTH source plans' acceptance criterion 4 close.
   No completion claim before the round runs.
5. C5: the pinned workflows pass their local contract tests
   (`tests/contract/test_ci_security.py` family) and the first observed
   real-runner run (shared with the exclusion-policy spec) uses the
   pinned shape.
6. Full offline gates green before the round: `uv run poe verify`, `uv
   run poe exclusion-policy-test`, `uv run poe api-contract-check`.
   Each BACKLOG row (5 rows + ride-along) is removed in the diff that
   closes it.

## Error cases

- Sink exposition route unavailable/unauthorized: existing closed auth
  errors; evaluation never blocks (C1 fallback).
- Live-round trigger cannot produce a surface (e.g. staleness bound
  unreachable in round time): record the attempt honestly, keep the row
  open, and report the blocking gate — no partial completion claim.
- Round reveals a NEW defect in a surfaced line: fix in-scope surfaces
  in this effort if small; otherwise index per BACKLOG rules with a
  verifiable `Implement by` and owner.
- Pinned workflow fails on real runner for pin reasons: fix the pins and
  re-observe; the exclusion-policy first-run row still retires only on
  the final shape's observed run.
