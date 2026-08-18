# Performance gates

**Status:** Owned by the exclusion-policy publication spec (section 24,
`docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md`).
The layer left the bootstrap reservation when that spec landed its reference
performance gates — the "later spec" this README originally deferred to.

## What runs here

`test_exclusion_policy_performance.py` pins the four reference budgets of
spec 24 against a deterministic 10,000-source fixture seeded into a disposable
PostgreSQL 18.4 stack (`knowledge-ci-*` project, `CI=true`,
`LOCAL_STACK_TEST_PROJECT`):

- one subject against 256 mixed rules — evaluator p95 <= 5 ms
- one maximum-size signed snapshot verification — p95 <= 50 ms
- 10,000 subjects against 256 mixed rules — preview ready <= 30 s
- 10,000-source reconciliation — <= 300 s

Every budget records p50/p95/max, the reference-host evidence (platform, CPU,
RAM, Python/PostgreSQL versions, live capacity settings) prints before any
assertion, warmup iterations are explicit and excluded, and the module fails —
never skips — when the stack is unavailable.

## How to run

```bash
CI=true LOCAL_STACK_TEST_PROJECT="knowledge-ci-exclusion-perf-$$" \
  uv run pytest tests/performance -m local_stack -q
```

Future specs add their own reference workloads here; a budget that regresses
triggers profiling and an explicit capacity decision (spec 24), never a
weakening of fail-closed behavior.
