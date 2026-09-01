# Exclusion-policy acceptance evidence — design spec

Date: 2026-08-31. Domain: exclusion policy (tests, metrics recorder, Web
Admin `/admin/policy`, acceptance CI). Governing docs: spec 17
(`2026-08-17-exclusion-policy-publication-design.md`, testing strategy and
section 1195 mutation ruling), the 2026-08-17 handoff §5, the 2026-08-24
policy-observability remediation handoff §5.3/§5.5 and spec C1/C2, and the
2026-08-30 child-nine hygiene retirement handoff (deferred item 2).

## Purpose and scope

Retire six indexed BACKLOG rows that restore or complete the
exclusion-policy acceptance evidence chain, all without Child 8/9 work and
without mobile:

1. 2026-08-17 exclusion-policy §5.1 — mutation testing of the
   exclusion-policy suites, deferred exactly as the spec's testing
   strategy mandates. Gate: before Child 9 acceptance closure.
2. 2026-08-17 exclusion-policy §5.2 — spec 17 mandates TanStack Query but
   the plan pins `@noble/ed25519` as the only new dependency; plan owner
   amends the spec or schedules the dependency. Gate: before next policy
   UI change.
3. 2026-08-17 exclusion-policy §5.3 — first real-runner execution of
   `.github/workflows/exclusion-policy-acceptance.yml` never observed.
   Gate: before Child 9 acceptance closure.
4. 2026-08-24 exclusion-policy (policy-observability §5.3) — test-coverage
   batch: `_validate_evaluation_error_code` ValueError branches;
   indeterminate-not-`failed` combination pin beside the denial test; one
   real fail-closed walk into the diagnostics route payload; parametrize
   the four guard-raisable codes through `_publish`.
5. 2026-08-24 exclusion-policy (policy-observability §5.5) —
   `InMemoryExclusionPolicyMetrics` increments unsynchronized.
   Gate: before multi-worker serve.
6. 2026-08-30 exclusion-policy acceptance — `policy-publication.spec.ts`
   `/admin/policy` page 500, root cause undiagnosed (no green CI baseline
   since ≥2026-08-24). Gate: before Phase 2 closure (after Child 9).

Out of scope: the reference-device verification records row (mobile), the
unknown-future-code ring-map row (conditional trigger), the metrics-sink
row (owned by the 2026-08-31 policy-diagnostics spec), and the §6
code-level minors that carry no BACKLOG line.

## Problem

The child-3 acceptance surface has four evidence holes and one live
defect. Its mutation-testing mandate is standing-deferred; its CI workflow
has never executed on a real GitHub runner; its diagnostics remediation
left a named test-coverage batch and an unsynchronized recorder; and the
Web Admin policy page 500s in `policy-publication.spec.ts` with no
diagnosed root cause and no green CI baseline since at least 2026-08-24
(the earlier-failing `database_schema_contract_invalid` hides it). On top
of these, spec 17 still mandates TanStack Query while the shipped code
uses the plan-pinned effect pattern — a documented conflict the plan owner
must resolve before the next Web child builds on `/admin/policy`.

## Compatibility contract

- No public contract change is required by rows 1, 3, 4, 5: they add
  tests, synchronization and CI observation only.
- Row 6 is diagnose-then-fix: the fix must restore the page to its
  spec-17 contract; if root cause implicates a contract, the fix follows
  the existing contract rather than amending it, and any unavoidable
  amendment lands with OpenAPI/generated-client/contract-test updates per
  repo rules.
- Row 2 is a spec-amendment decision, not a code change by default:
  either spec 17 is amended to ratify the existing effect pattern
  (recommended — no new production dependency, consistent with AGENTS
  dependency rules) or the TanStack dependency is scheduled with
  justification. The decision is recorded in spec 17 itself and in the
  handoff; user ratification happens at plan review.
- Mutation tooling, if any, is a dev-only dependency with justification;
  no production dependency is added.

## Contracts

### C1 Test-coverage batch (policy-observability §5.3, verbatim scope)

1. Direct tests for the `_validate_evaluation_error_code` ValueError
   branches.
2. The indeterminate-not-recorded-as-`failed` combination pinned beside
   the definite-denial test.
3. One test walking a real fail-closed evaluation end-to-end into the
   diagnostics route payload (today's route tests seed the recorder
   directly).
4. A parametrize over the four guard-raisable codes through `_publish`
   (only denial is directly tested today).

### C2 Synchronized in-memory recorder

`InMemoryExclusionPolicyMetrics` increments and snapshot reads are
synchronized (lock or equivalent) so a concurrent read cannot observe a
torn counter. The fix stays inside the class — no cross-domain shared
recorder abstraction (the repetition-over-abstraction ruling stands). A
multi-threaded increment-then-read test pins it.

### C3 Mutation-testing round

A mutation-testing run over the exclusion-policy Python suites executes,
the score is recorded, and every surviving mutant is either fixed or
closed with a written verdict in the handoff. Tool choice, runtime
budget and any sampling are plan decisions; the run must be reproducible
from a committed command. This ends the standing deferral for the
exclusion-policy suites named by the row; plugin TypeScript suites stay
out of scope unless plan review explicitly widens them.

### C4 First real-runner CI observation

A PR (this program's own PR qualifies) triggers
`exclusion-policy-acceptance.yml` on a real GitHub runner; the run is
observed and its outcome recorded in the handoff. Any workflow failure is
triaged to root cause — not re-run blind. The optional paths-filter
decision for the 90-minute job (2026-08-17 handoff §6) is taken or
explicitly declined in the same record. If the
2026-08-31 policy-diagnostics spec lands CI consistency pins on this
workflow first, the observed run must be the pinned version.

### C5 `/admin/policy` 500 diagnosis and fix

Reproduce on a fresh build (the handoff's named first step), diagnose to
root cause, fix, and restore a green `policy-publication.spec.ts` locally.
If the hidden CI baseline (`database_schema_contract_invalid`) still
blocks a green CI run of the suite, that failure is triaged far enough to
prove it is not the same root cause, and any fix beyond that is surfaced
as a finding with its owner named — not silently absorbed.

### C6 TanStack Query spec-amendment decision

Produce the decision record and land it: amend spec 17 to ratify the
existing effect pattern (removing the TanStack mandate) or schedule the
dependency with impact statement. The conflict must not survive this
effort — that is the row's terminal condition.

## Privacy invariants (acceptance-critical)

- New tests and the mutation run assert closed codes/tokens only; no
  paths, operands, snapshot contents or raw bodies enter fixtures beyond
  what existing suites already pin.
- CI observation records carry workflow names, run ids and exit outcomes
  only.

## Acceptance criteria

1. C1's four items land as named tests; C2's concurrency test lands RED
   first where feasible (torn-read reproduction may be probabilistic —
   then the pin is the deterministic post-lock behavior).
2. Mutation round executed with a recorded, reproducible command and a
   survivor verdict list; no surviving mutant closes silently.
3. `policy-publication.spec.ts` green on a fresh local build; root cause
   recorded.
4. Real-runner observation recorded (run URL or id, outcome).
5. Spec 17 no longer contains the unresolved TanStack conflict.
6. Full offline gates green: `uv run poe exclusion-policy-test`, `uv run
   poe verify`, workspace web tests; each of the six BACKLOG rows is
   removed in the diff that closes it.

## Error cases

- Mutation tool incompatible with the pinned Python/build: record the
  incompatibility, pick the alternative tool or scope the run to what the
  tool supports, and amend the row's closure evidence accordingly — never
  claim a score that was not produced.
- Real-runner fails on environment (not code): triage to the workflow
  level, fix the workflow, observe a subsequent run; the row retires only
  on an observed run of the final workflow shape.
- `/admin/policy` root cause reveals an owned-by-another-domain defect:
  fix what this page owns, index the rest per BACKLOG rules with a
  verifiable `Implement by`.
