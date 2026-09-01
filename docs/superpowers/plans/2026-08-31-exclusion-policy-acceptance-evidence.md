# Exclusion-policy Acceptance Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the six exclusion-policy BACKLOG rows (test-coverage batch, unsynchronized recorder, mutation testing, first real-runner CI observation, `/admin/policy` 500, TanStack Query spec conflict) and restore a green acceptance-evidence chain.

**Architecture:** Four named test gaps close first, then the recorder gains a lock, then a dependency-free in-repo mutation runner (no production or tool dependency — the repo pins Python 3.14 and no mutation tool supports it) produces the first mutation report over the exclusion-policy unit suites. The `/admin/policy` 500 is reproduced on a fresh build, diagnosed to root cause and fixed. Spec 17's TanStack mandate is amended to ratify the shipped effect pattern. The CI workflow's first real-runner run is observed on this program's own PR.

**Tech Stack:** Python 3.14, pytest, mypy strict, ruff; Playwright + MSW; GitHub Actions.

**Spec:** `docs/superpowers/specs/backlog/2026-08-31-exclusion-policy-acceptance-evidence-design.md`

## Global Constraints

- Closed tokens only on every new assertion surface; no paths, operands, snapshot contents or raw bodies beyond what existing suites pin.
- No new production dependency; the mutation runner is in-repo (`tools/`, dev-only execution).
- Each BACKLOG row is removed in the diff that closes it.
- Plan-review ratifications: the TanStack decision is the AMENDMENT option (ratify the effect pattern, no dependency); the mutation round scopes to `tests/unit/exclusion_policy` as the killing suite (documented scope — the 1383-test full gate per mutant is not runnable); the paths-filter decision for the 90-minute CI job is DECLINED for now (the workflow also gates master pushes; filtering PR paths would let master-relevant changes skip it) — recorded as such in the observation task.

---

### Task 1: Test-coverage batch (the four named gaps)

**Files:**
- Test: `tests/unit/exclusion_policy/test_metrics_diagnostics.py` (ValueError branches; indeterminate pin)
- Test: `tests/contract/api/test_exclusion_policy_diagnostics_routes.py` (real fail-closed walk)
- Test: `tests/unit/sources/test_publication_service.py` (four-code parametrize)

**Interfaces:**
- Consumes: `InMemoryExclusionPolicyMetrics.record_evaluation` (`src/personal_os/exclusion_policy/metrics.py:310`), `_validate_evaluation_error_code` (metrics.py:216-225), the SYSTEM/DENIAL split (`exclusion_policy/errors.py:123-139`), the guard builders (`exclusion_policy/enforcement.py:129-159`), `_publish` guard path (`src/personal_os/sources/publication.py:249-282`).
- Produces: tests only — no production change expected in this task (if a gap turns out to be a bug, stop and report before editing).

- [ ] **Step 1: Direct ValueError-branch tests**

In `tests/unit/exclusion_policy/test_metrics_diagnostics.py` (beside `test_failed_decision_without_error_code_is_rejected_by_the_diagnostics_sink` L177-184):

```python
def test_non_failed_decision_rejects_a_carried_error_code() -> None:
    """The inverse branch of the closed-code validator (metrics.py:225)."""
    recorder = InMemoryExclusionPolicyMetrics(epoch_ms_clock=lambda: 1_000)
    with pytest.raises(ValueError, match="recordable only on the failed decision"):
        recorder.record_evaluation(
            boundary=EvaluationBoundary.SOURCE_CREATE_UPDATE,
            decision=EvaluationMetricOutcome.ALLOWED,
            duration_seconds=0.01,
            error_code=ErrorCode.EXCLUSION_POLICY_DENIED,
        )


def test_validate_evaluation_error_code_rejects_both_invalid_shapes_directly() -> None:
    """Direct unit pin of the validator (previously covered only indirectly)."""
    with pytest.raises(ValueError, match="failed decision requires"):
        _validate_evaluation_error_code(EvaluationMetricOutcome.FAILED, None)
    with pytest.raises(ValueError, match="recordable only on the failed decision"):
        _validate_evaluation_error_code(EvaluationMetricOutcome.ALLOWED, ErrorCode.EXCLUSION_POLICY_DENIED)
```

(Import `_validate_evaluation_error_code` from `personal_os.exclusion_policy.metrics`; mirror the file's existing import block.)

- [ ] **Step 2: Indeterminate-not-`failed` pin beside the denial test**

In `tests/unit/sources/test_publication_service.py`, beside `test_policy_denial_records_failed_event_and_rejected_publication_outcome` (L781):

```python
def test_indeterminate_policy_outcome_records_rejected_event_without_failed_metric(...) -> None:
    """An INDETERMINATE guard refusal is a denial-class outcome: the failed
    event/metric shape applies to SYSTEM codes only — pin that combination."""
    # same harness as the L781 test with denying_policy_guard(ErrorCode.EXCLUSION_POLICY_INDETERMINATE)
    # assertions:
    #   - raises EXCLUSION_POLICY_INDETERMINATE (re-raised unchanged)
    #   - SOURCE_VERSION_PUBLISH_FAILED event recorded with the closed code
    #   - PublicationMetricOutcome.REJECTED == 1
    #   - policy evaluation counters show NO `failed` decision row for this boundary
```

- [ ] **Step 3: Real fail-closed walk into the diagnostics route payload**

In `tests/contract/api/test_exclusion_policy_diagnostics_routes.py` (whose `recorder` fixture currently seeds directly, L84-105):

```python
def test_diagnostics_route_reflects_a_real_fail_closed_evaluation(
    client, recorder, composed_app
) -> None:
    """Drive a REAL broken-signer evaluation through the enforcement path
    (not direct recorder seeding) and read it back from the route."""
    # compose with a verifier whose key material is corrupt (the harness the
    # unit suites use for signing_corruption_error), evaluate one candidate,
    # then:
    response = client.get(_ROUTE_PATH, headers=_web_session_headers())
    assert response.status_code == 200
    payload = response.json()["data"]
    failed_rows = [row for row in payload["evaluation_counters"] if row["decision"] == "failed"]
    assert failed_rows and failed_rows[0]["boundary"] == "<the boundary used>"
    assert payload["recent_failures"][0]["error_code"] == "exclusion_policy_signing_unavailable"
```

- [ ] **Step 4: Four-code parametrize through `_publish`**

```python
@pytest.mark.parametrize("error_code", [
    ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
    ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
    ErrorCode.EXCLUSION_POLICY_DENIED,
    ErrorCode.EXCLUSION_POLICY_INDETERMINATE,
])
def test_every_guard_raisable_code_escapes_publish_with_failed_event_and_outcome(
    error_code, ...
) -> None:
    """All four guard-raisable codes traverse _publish identically: typed
    re-raise, SOURCE_VERSION_PUBLISH_FAILED event, REJECTED publication
    outcome; SYSTEM codes additionally land the failed evaluation counter."""
    # denying_policy_guard(error_code) harness; assertions per the L781 test
    # plus, for the two SYSTEM codes, a failed evaluation-counter row.
```

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/unit/exclusion_policy tests/unit/sources tests/contract/api/test_exclusion_policy_diagnostics_routes.py -q
```
Expected: PASS. If any new test exposes a real bug (fails for behavior reasons), STOP and report — the spec's diagnose-then-fix path applies, not a silent implementation change.

```bash
git add tests/unit/exclusion_policy/test_metrics_diagnostics.py tests/unit/sources/test_publication_service.py tests/contract/api/test_exclusion_policy_diagnostics_routes.py
git commit -m "test: close the exclusion-policy diagnostics coverage batch"
```

Remove the BACKLOG row `| 2026-08-24 | exclusion-policy | Test-coverage batch...` in this commit.

---

### Task 2: Synchronize `InMemoryExclusionPolicyMetrics`

**Files:**
- Modify: `src/personal_os/exclusion_policy/metrics.py:287-405`
- Test: `tests/unit/exclusion_policy/test_metrics_diagnostics.py`

**Interfaces:**
- Produces: same public methods; internal `threading.Lock` guards every counter/ring mutation and the `policy_diagnostics()` snapshot.

- [ ] **Step 1: Write the failing concurrency test**

```python
def test_concurrent_increments_never_lose_a_count() -> None:
    recorder = InMemoryExclusionPolicyMetrics(epoch_ms_clock=lambda: 1_000)
    barrier = threading.Barrier(8)

    def _record() -> None:
        barrier.wait()
        for _ in range(50):
            recorder.record_evaluation(
                boundary=EvaluationBoundary.SOURCE_CREATE_UPDATE,
                decision=EvaluationMetricOutcome.ALLOWED,
                duration_seconds=0.01,
            )

    threads = [threading.Thread(target=_record) for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    snapshot = recorder.policy_diagnostics()
    assert snapshot.evaluation_counters[(
        EvaluationBoundary.SOURCE_CREATE_UPDATE, EvaluationMetricOutcome.ALLOWED
    )] == 400
    assert recorder.evaluation_count() == 400
```

Note: on CPython the GIL may make this pass pre-fix intermittently — the deterministic pin is that the class USES a lock (assert via a white-box check that `_InMemory` has a `_lock` attribute documented in the docstring, in addition to the behavioral test).

- [ ] **Step 2: Run to verify**

Run: `uv run pytest tests/unit/exclusion_policy/test_metrics_diagnostics.py -k concurrent -q`
Expected: FAIL (no `_lock` attribute) on the white-box assertion.

- [ ] **Step 3: Implement**

`__init__` gains `self._lock = threading.Lock()`; every `record_*` wraps its mutation block in `with self._lock:`; `policy_diagnostics()`/`evaluation_count()`/`publication_count()` snapshot under the same lock; docstring line: "increments and snapshots are lock-guarded for multi-worker serve (BACKLOG 2026-08-24 §5.5)". No cross-domain abstraction — this lock stays local.

- [ ] **Step 4: Run + commit + retire row**

```bash
uv run poe exclusion-policy-test
git add src/personal_os/exclusion_policy/metrics.py tests/unit/exclusion_policy/test_metrics_diagnostics.py docs/handoff/BACKLOG.md
git commit -m "fix: lock the in-memory policy metrics recorder"
```
Remove the `| 2026-08-24 | exclusion-policy | InMemoryExclusionPolicyMetrics counter increments are unsynchronized...` row.

---

### Task 3: In-repo mutation runner + first round

**Files:**
- Create: `tools/exclusion_policy_mutation_report.py`
- Test: `tests/unit/tools/test_exclusion_policy_mutation_report.py` (smoke: mutation enumeration + one killed mutant on a tiny fixture module)

**Interfaces:**
- Produces: CLI `uv run python tools/exclusion_policy_mutation_report.py --source src/personal_os/exclusion_policy --tests tests/unit/exclusion_policy --output .local/test-results/exclusion-policy-mutation.md --per-mutant-timeout-seconds 180` — enumerates a closed mutation set, runs the killing suite per mutant, writes a markdown report (score, per-survivor file:line + mutation).

- [ ] **Step 1: Write the runner**

```python
"""Closed-set mutation report for the exclusion-policy suites.

No external mutation tool supports the repo's Python 3.14 pin, so this
runner applies a small closed mutation set (comparison-operator swaps,
boolean-operator swaps, integer-constant ±1, comparison negation) to
``--source`` and runs ``--tests`` per mutant. A mutant is KILLED when the
suite exits non-zero. Survivors are reported for hand review; the score
and every survivor verdict land in the plan handoff (BACKLOG 2026-08-17 §5.1).
"""

from __future__ import annotations

import argparse
import ast
import copy
import dataclasses
import subprocess
import sys
from pathlib import Path

_COMPARISON_SWAPS = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt,
                     ast.Gt: ast.LtE, ast.LtE: ast.Gt}
_BOOL_SWAPS = {ast.And: ast.Or, ast.Or: ast.And}


@dataclasses.dataclass(frozen=True)
class Mutation:
    path: Path
    line: int
    description: str
    source: str


def _mutations_of(tree: ast.Module, source: str, path: Path) -> list[Mutation]:
    out: list[Mutation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            replacement = _COMPARISON_SWAPS.get(type(node.ops[0]))
            if replacement is not None:
                mutated = copy.deepcopy(tree)
                for candidate in ast.walk(mutated):
                    if (isinstance(candidate, ast.Compare) and len(candidate.ops) == 1
                            and candidate.lineno == node.lineno
                            and type(candidate.ops[0]) is type(node.ops[0])):
                        candidate.ops[0] = replacement()
                out.append(Mutation(path, node.lineno,
                                    f"{type(node.ops[0]).__name__}->{replacement.__name__}",
                                    ast.unparse(mutated)))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            for delta in (1, -1):
                mutated = copy.deepcopy(tree)
                for candidate in ast.walk(mutated):
                    if (isinstance(candidate, ast.Constant) and candidate.value == node.value
                            and candidate.lineno == node.lineno and candidate.col_offset == node.col_offset):
                        candidate.value = node.value + delta
                        break
                out.append(Mutation(path, node.lineno, f"int {node.value}->{node.value + delta}",
                                    ast.unparse(mutated)))
    return [m for m in out if m.source != source]


def _run_suite(tests: Path, timeout_seconds: int) -> bool:  # True = killed
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tests), "-x", "-q", "--no-header", "-p", "no:cacheprovider"],
        timeout=timeout_seconds, capture_output=True,
    )
    return result.returncode != 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-mutant-timeout-seconds", type=int, default=180)
    args = parser.parse_args()

    mutations: list[Mutation] = []
    for py_file in sorted(args.source.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        mutations.extend(_mutations_of(ast.parse(source), source, py_file))

    killed = 0
    survivors: list[Mutation] = []
    for index, mutation in enumerate(mutations, start=1):
        original = mutation.path.read_text(encoding="utf-8")
        try:
            mutation.path.write_text(mutation.source + "\n", encoding="utf-8")
            if _run_suite(args.tests, args.per_mutant_timeout_seconds):
                killed += 1
            else:
                survivors.append(mutation)
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        print(f"[{index}/{len(mutations)}] {mutation.path.name}:{mutation.line} {mutation.description}",
              flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Exclusion-policy mutation report", "",
             f"- mutants: {len(mutations)}", f"- killed: {killed}",
             f"- survived: {len(survivors)}",
             f"- score: {killed / len(mutations):.3f}" if mutations else "- score: n/a", "",
             "## Survivors", ""]
    lines += [f"- `{m.path.relative_to(m.path.parents[2])}:{m.line}` — {m.description}" for m in survivors]
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

(Boolean-operator swap wiring follows the same pattern as the comparison swap — include `_BOOL_SWAPS` handling for `ast.BoolOp` in `_mutations_of` the same way.)

- [ ] **Step 2: Smoke test the runner**

```python
def test_runner_enumerates_and_kills_a_mutant(tmp_path: Path) -> None:
    (tmp_path / "subject.py").write_text("def is_large(value: int) -> bool:\n    return value > 10\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_subject.py").write_text(
        "from subject import is_large\n\ndef test_large() -> None:\n    assert is_large(11)\n    assert not is_large(10)\n"
    )
    report = tmp_path / "report.md"
    exit_code = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "exclusion_policy_mutation_report.py"),
         "--source", str(tmp_path), "--tests", str(tmp_path / "tests"), "--output", str(report),
         "--per-mutant-timeout-seconds", "60"],
    ).returncode
    assert exit_code == 0
    text = report.read_text()
    assert "survived: 0" in text and "killed: " in text
```

(Import-path detail: the subject import works because pytest's rootdir insertion picks up `tmp_path`; if not, generate a `conftest.py` with `sys.path` insertion in the fixture — keep the test hermetic.)

- [ ] **Step 3: Run the first mutation round**

```bash
uv run python tools/exclusion_policy_mutation_report.py \
  --source src/personal_os/exclusion_policy \
  --tests tests/unit/exclusion_policy \
  --output .local/test-results/exclusion-policy-mutation.md
```
Expected: completes with a score line. Long-running — run in the background and monitor; on timeout-kill of a mutant, treat it as killed-by-timeout and note it in the report reading (the runner counts non-zero exit as killed; a `subprocess.TimeoutExpired` currently aborts — wrap it to count as killed-by-timeout with a report line, before the real run).

- [ ] **Step 4: Triage survivors**

For each survivor: fix the weak test or record the verdict ("equivalent mutant" / "acceptable survivor" with reason) in the task report. No survivor closes silently.

- [ ] **Step 5: Commit + retire the row**

```bash
git add tools/exclusion_policy_mutation_report.py tests/unit/tools/test_exclusion_policy_mutation_report.py docs/handoff/BACKLOG.md
git commit -m "test: first exclusion-policy mutation round via the in-repo runner"
```
Remove the `| 2026-08-17 | exclusion-policy | Mutation testing of the exclusion-policy suites stays deferred...` row (the standing deferral in spec 17 line 1195 is simultaneously updated: mutation testing for the exclusion-policy suites ran 2026-09; it remains no per-child completion gate).

---

### Task 4: `/admin/policy` 500 — reproduce, diagnose, fix

**Files:**
- Modify: (root cause determines — candidates: `apps/web/src/features/exclusion-policy/PolicyEditor.tsx` L172-206 fetch effect; `apps/api/src/api_runtime/exclusion_policy_routes.py:229-268` `get_policy_status` staleness block; `apps/api/src/api_runtime/exclusion_policy_composition.py:553` `_list_stale_running_previews_once`)
- Test: `tests/end_to_end/exclusion_policy/policy-publication.spec.ts` (must end green)

**Interfaces:**
- Consumes: the single journey `the policy publication journey publishes the empty policy then a deny rule` (L339), which 500s at `page.goto("/admin/policy")` (L344/L353).

- [ ] **Step 1: Reproduce on a fresh build**

```bash
pnpm run test:e2e:exclusion-policy
```
Expected: FAIL with a 500 observed on `/admin/policy` (or the API call behind it). Capture the exact failing response (status, route) into the task report. If it does NOT reproduce locally, reproduce through the CI project (`CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan2-t4-*`) before any diagnosis — a non-reproducing 500 is itself the finding.

- [ ] **Step 2: Diagnose to root cause**

Bisect the surface: (a) hit `GET /api/admin/exclusion-policy` with a web session directly — if 500, the API side is owner (`get_policy_status` / `_list_stale_running_previews_once` — check the API diagnostics log under `.local/runtime-logs/` for the typed exchange); (b) if the API is clean, the web side owns it (PolicyEditor fetch/`applyStatus`, or the server component `apps/web/src/app/admin/policy/page.tsx`). Record the root cause with file:line evidence. If the CI-only `database_schema_contract_invalid` failure blocks a green CI baseline, triage only far enough to prove it is not the same root cause; any fix beyond that is surfaced as a finding with its owner named.

- [ ] **Step 3: Fix per root cause**

The fix restores the page to its spec-17 contract (no contract amendment). Add the regression pin the diagnosis suggests (e.g., if the staleness query errors on empty state, a contract test driving `GET /api/admin/exclusion-policy` through that state).

- [ ] **Step 4: Verify green + commit + retire the row**

```bash
pnpm run test:e2e:exclusion-policy
uv run poe exclusion-policy-test
```
Expected: exit 0 both.

```bash
git add <fix files> docs/handoff/BACKLOG.md
git commit -m "fix: restore the policy admin page journey to green"
```
Remove the `| 2026-08-30 | exclusion-policy acceptance | policy-publication.spec.ts /admin/policy page 500...` row.

---

### Task 5: Spec 17 TanStack amendment

**Files:**
- Modify: `docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md:949-950` (§17 Web Admin behavior)

**Interfaces:**
- Consumes: the ratified decision — no TanStack Query anywhere (`apps/web/package.json` carries none; components use the plan-pinned effect pattern).

- [ ] **Step 1: Amend the sentence**

Replace "The page uses generated API contracts and TanStack Query; it does not connect to PostgreSQL or Temporal." with:

```markdown
The page uses generated API contracts and a local effect-based fetch
pattern (amended 2026-08-31 per the 2026-08-17 handoff §5.2 conflict
ruling: the plan's `@noble/ed25519`-only dependency pin governs; no data
library is mandated); it does not connect to PostgreSQL or Temporal.
```

- [ ] **Step 2: Verify no other TanStack references remain**

Run: `rg -n -i "tanstack" docs/ apps/ packages/`
Expected: no hits.

- [ ] **Step 3: Commit + retire the row**

Remove `| 2026-08-17 | exclusion-policy | Spec 17 mandates TanStack Query but the plan pins @noble/ed25519...` from BACKLOG.

```bash
git add docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md docs/handoff/BACKLOG.md
git commit -m "docs: ratify the effect-pattern fetch in the policy admin spec"
```

---

### Task 6: Observe the first real-runner execution

**Files:**
- Modify: `docs/handoff/BACKLOG.md` (row removal) + the observation record goes into this plan's handoff (created at plan close).

**Interfaces:**
- Consumes: `.github/workflows/exclusion-policy-acceptance.yml` (triggers: `pull_request` unfiltered, `push: master`; 90-minute job).

- [ ] **Step 1: Confirm the workflow's current shape**

Run: `rg -n "on:|pull_request|paths|LOCAL_STACK_TEST_PROJECT" .github/workflows/exclusion-policy-acceptance.yml`
Confirm the run-derived project name and guard are present (they are — verified 2026-08-31; the 2026-08-31 policy-diagnostics plan may extend the CI-security prefetch coverage tuple to this workflow; either order is fine).

- [ ] **Step 2: Open/observe the PR run**

Push this program's branch and open its PR (or reuse the first PR that carries this workflow at its final shape). Watch the `exclusion-policy-acceptance` job on a real GitHub runner. Record: run id, outcome, duration. On failure: triage to root cause (workflow-level vs code-level), fix, and observe a subsequent run — the row retires only on an observed run of the final shape.

- [ ] **Step 3: Record the paths-filter decision**

The 90-minute job runs on every `pull_request` with no paths filter (2026-08-17 handoff §6 consideration). Decision (ratified): DECLINED — the workflow also protects master pushes and gates the acceptance suite as a whole; a paths filter would let master-relevant changes skip it. Record this verbatim in the handoff.

- [ ] **Step 4: Retire the row**

Remove `| 2026-08-17 | exclusion-policy | First real-runner execution of .github/workflows/exclusion-policy-acceptance.yml never observed...` from BACKLOG in the commit that carries the observation evidence reference.

---

### Task 7: Final verification

- [ ] **Step 1: Full gates**

```bash
uv run poe verify
uv run poe exclusion-policy-test
uv run poe api-contract-check
pnpm run test
```
Expected: all exit 0.

- [ ] **Step 2: BACKLOG check**

Run: `rg -n "exclusion-policy" docs/handoff/BACKLOG.md`
Expected: only rows owned by other plans remain (the metrics-sink row, the unknown-future-code conditional row, the reference-device mobile row).

## Self-review notes

Spec coverage: C1→Task 1, C2→Task 2, C3→Tasks 3 (+§5.1 standing-deferral closure), C4→Task 6, C5→Task 4, C6→Task 5. The mutation runner's per-mutant timeout must count as killed-by-timeout (called out in Task 3 Step 3 before the real run). Signatures consistent: `InMemoryExclusionPolicyMetrics` public surface unchanged; runner CLI flags as documented in the Interfaces block.
