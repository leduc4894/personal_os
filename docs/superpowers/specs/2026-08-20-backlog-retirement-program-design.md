# Backlog Retirement Program Design

## Objective

Retire every row in `docs/handoff/BACKLOG.md` without treating deferred work
as a harmless parking lot. Before a downstream Phase 2 child or later phase
depends on a domain, its load-bearing correctness, privacy, idempotency,
availability and acceptance obligations must be either implemented or resolved
by an explicit canonical decision.

## Non-negotiable disposition rule

Every backlog row has exactly one terminal disposition:

1. **Implemented:** regression tests and relevant gates prove the behaviour;
   the row is removed and one handoff records evidence.
2. **Superseded or unnecessary by an explicit ruling:** a canonical spec/ADR
   explains why no code is correct (for example, an abstraction promised only
   after a third caller when only two callers exist). The row is removed; no
   placeholder code is added solely to empty the list.
3. **External acceptance completed:** an operator records sanitized
   device/CI evidence through the existing procedure. The row is then removed.

“Later”, “when convenient”, and unowned observation are not valid terminal
states. Rows are removed only after their disposition is committed.

## Program order

The program is a sequence of bounded waves, not one cross-domain mega-change.
Each wave gets its own approved spec, implementation plan, branch, tests,
review and one handoff. No later wave may use a known unretired invariant from
an earlier wave as an assumption.

| Wave | Domain boundary | Required outcome before the next wave |
| --- | --- | --- |
| 1 | Canonical safety and privacy | canonical bytes/read/recovery and sensitive values have explicit fail-closed and redaction contracts |
| 2 | Source publication durability | retry, lease, dispatcher and diagnostic semantics survive database/process disruption |
| 3 | Authentication concurrency | auth/throttle/grant/poll state remains correct under races, rotation and multiple workers |
| 4 | Phase 2 acceptance closure | real-device evidence, CI observation, and spec/dependency contradictions have final dispositions |
| 5 | Domain hygiene and precision | remaining metrics, test-hygiene, documentation and conditional-abstraction rows are implemented or ruled unnecessary in their owners |

## Wave 1 — Canonical safety and privacy

### Scope

Wave 1 owns backlog rows that can make canonical reads/writes unreliable,
block strict collection, expose a sensitive value through `repr`, or leave a
future core consumer without a validating boundary:

- object-storage admission (`disk_usage` off the event loop), exact object-key
  parser before string parsing gains a consumer, internal failure metrics
  disposition, real-time receive backstop proof, and the single-flight
  exception/cancellation contract;
- canonical-read distinction between a consumer-body `ApplicationError` and a
  canonical read failure, including event/metric assertions for missing and
  corrupt bytes;
- recovery representation and process-output redaction, JSON-manifest error
  classification, bounded-memory/event-loop hygiene, and restore metric
  accuracy;
- explicit redaction for the small-file value objects whose documentation says
  they never enter diagnostics;
- the diagnostics/error-contract circular-import structural fix.

### Required invariants

1. A canonical read failure emits the failure event/metric exactly once; an
   error raised by a caller’s consumer body is propagated without being counted
   as a storage read failure.
2. No sensitive typed value can reveal its raw string, token or process output
   via the default `repr` path. Logs, diagnostics and tests keep using opaque
   IDs, safe codes, counts and shortened hashes only.
3. Synchronous filesystem work on a potentially blocking canonical path is
   moved out of the event loop, and the revised resource/admission behaviour is
   proved by deterministic tests.
4. Core object-key parsing is closed, validating and round-trips only the
   canonical key grammar. No consumer accepts arbitrary key strings.
5. The diagnostics and error-contract dependency graph is acyclic by module
   structure, not import order.
6. Recovery transfer and restore accounting stay bounded and truthful under
   error paths; invalid manifest shapes use their documented typed mapping.

### Explicit non-goals

- No change to PostgreSQL/R2 source-of-truth roles, policy fail-closed rules,
  public API/OpenAPI, database schema or provider topology.
- No premature shared `RedactedString` abstraction: individual redacted value
  objects remain explicit until a real additional consumer demonstrates a
  common protocol. The existing “fourth class” structural-enforcement row is
  retired through this documented ruling, not code manufactured for reuse.
- No bulk cleanup of unrelated naming, test-style or documentation rows in a
  canonical-safety change.

### Acceptance

- Each behavioural change has a test observed RED before its implementation
  and GREEN afterward.
- `uv run poe canonical-core-test`, relevant object-storage suites, strict
  mypy, Ruff and architecture-boundary checks pass from one final commit.
- Leak-contract tests cover every newly redacted value type.
- `docs/handoff/BACKLOG.md` loses only Wave 1 rows whose terminal disposition
  is evidenced in the Wave 1 handoff.

## Dependency gates for later waves

Wave 2 may begin only after Wave 1 acceptance is green. Wave 3 may be planned
in parallel but may not merge code that depends on unresolved canonical
diagnostic/redaction semantics. Phase 2 child 5 (locator/tombstone lifecycle)
does not start until Waves 1–4 have retired their respective load-bearing rows
or the user explicitly accepts a documented exception.

## Decision log and audit trail

Every wave handoff must list:

- removed backlog rows and their disposition;
- any ruling that retires a conditional abstraction without code;
- exact verification commands/results;
- remaining rows by next owning wave; and
- external evidence still needed, with the named existing procedure rather
  than a substitute mock.

`BACKLOG.md` remains an index of genuinely unresolved work until the terminal
disposition is committed; it must not be emptied by relabeling open work.
