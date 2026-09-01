# Policy Diagnostics Metrics Sink and Live Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the policy-observability rows (first production metrics sink, live smoke round, policy-key CLI class token), close the reconciliation-`leased` staleness row with a recorded code-stands ruling, and retire the stale CI consistency-pins row by verification.

**Architecture:** The sink is a dependency-free Prometheus text-format renderer over the already-bound shared `InMemoryExclusionPolicyMetrics` snapshot, exposed through the authenticated admin diagnostics family (`GET /api/admin/metrics`) — the recorder stays the recording source, the route only renders. The `leased` row closes as code-stands (domain grounding: the preview staleness sweep already detects a dead worker for the same sweep class; an age bound on `dispatched` — the healthy resting state — would false-positive). The CLI fix ports the ratified `_exception_class_token` pattern. The live round is ONE round executing both remediation specs' acceptance-criterion-4 checklists on a disposable `knowledge-ci-*` stack.

**Tech Stack:** Python 3.14 (mypy strict, ruff), FastAPI admin route family, pytest; Playwright WDIO Obsidian harness for the wrong-origin readback; GitHub Actions contract tests.

**Spec:** `docs/superpowers/specs/backlog/2026-08-31-policy-diagnostics-metrics-sink-and-live-smoke-design.md`

## Global Constraints

- The sink exposes closed-vocabulary counters and timestamps only; the forbidden-substrate scan scope extends to it. No raw content, queries, vectors, tokens or secrets on any new surface.
- No new dependency (the renderer hand-writes Prometheus text format — counters only).
- OpenAPI snapshot, generated client and contract tests move together with the new route.
- Nothing in the live round is simulated, mocked or substituted; both BACKLOG smoke rows retire only on observed, sanitized evidence (closed tokens, counts, timestamps — no paths, hostnames, credentials, content).
- Each BACKLOG row is removed in the diff that closes it.
- Plan-review ratifications: (a) the sink surface is the session-authenticated admin route family — docs/15 prescribes the Prometheus backend but no exposition endpoint/auth model, and the diagnostics family precedent decides it; full Alloy/Prometheus stack wiring belongs to the observability phase; (b) the `leased` terminus is the code-stands ruling with evidence, not an invented bound; (c) the CI row closes by verification + coverage extension (pins verified present 2026-08-31).

---

### Task 1: Prometheus text exposition route (`GET /api/admin/metrics`)

**Files:**
- Create: `apps/api/src/api_runtime/metrics_exposition_routes.py`
- Create: `apps/api/src/api_runtime/metrics_exposition.py` (pure renderer)
- Modify: `apps/api/src/api_runtime/application.py` (register the route beside `_register_policy_diagnostics_admin_route` L578-599)
- Modify: `apps/api/src/api_runtime/server.py` (~L163-195, pass the shared `policy_metrics` diagnostics source into the route factory)
- Test: `tests/unit/api_runtime/test_metrics_exposition.py`, `tests/contract/api/test_metrics_exposition_routes.py`
- Docs: `docs/operations/exclusion-policy-publication.md` (sink note replaces the boundary TODO wording)

**Interfaces:**
- Consumes: `ExclusionPolicyDiagnostics` snapshot (`policy_diagnostics()` → `evaluation_counters: Mapping[tuple[boundary, decision], int]`-shaped keys, `publication_counters`, `recent_failures` with closed codes) from the shared recorder bound at `server.py:170`.
- Produces: `render_policy_diagnostics_prometheus(snapshot: ExclusionPolicyDiagnostics) -> str` (module `metrics_exposition.py`, domain-only imports); route `GET /api/admin/metrics` → `text/plain; version=0.0.4; charset=utf-8`, `cache-control: no-store`, web-session admin auth (same dependency as the sibling diagnostics routes), device credentials rejected.

- [ ] **Step 1: Write the failing renderer test**

```python
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome, ExclusionPolicyDiagnostics, PublicationMetricOutcome,
)

def test_renderer_emits_closed_prometheus_counters_only() -> None:
    snapshot = ExclusionPolicyDiagnostics(
        evaluation_counters={("source_create_update", "failed"): 2, ("source_create_update", "allowed"): 7},
        publication_counters={(PublicationMetricOutcome.REJECTED,): 1},
        recent_failures=(),
    )
    text = render_policy_diagnostics_prometheus(snapshot)
    lines = text.strip().splitlines()
    assert 'exclusion_policy_evaluation_total{boundary="source_create_update",decision="failed"} 2' in lines
    assert 'exclusion_policy_evaluation_total{boundary="source_create_update",decision="allowed"} 7' in lines
    assert 'exclusion_policy_publication_total{outcome="rejected"} 1' in lines
    assert "# TYPE exclusion_policy_evaluation_total counter" in lines
```

(Build the snapshot with the real key types the dataclass uses — check `metrics.py:395-402` first and adapt the tuple shapes in the test to match; the assertion contract is the emission format.)

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/unit/api_runtime/test_metrics_exposition.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement the renderer**

```python
def render_policy_diagnostics_prometheus(snapshot: ExclusionPolicyDiagnostics) -> str:
    """Render the closed policy counters in Prometheus text format.

    Counters and closed tokens only (docs/15 §3 cardinality rule): label
    values are the registry's closed boundary/decision/outcome members —
    never ids, paths or free text.
    """
    lines = [
        "# TYPE exclusion_policy_evaluation_total counter",
    ]
    for (boundary, decision), count in sorted(snapshot.evaluation_counters.items()):
        lines.append(f'exclusion_policy_evaluation_total{{boundary="{boundary}",decision="{decision}"}} {count}')
    lines.append("# TYPE exclusion_policy_publication_total counter")
    for outcome, count in sorted(snapshot.publication_counters.items()):
        lines.append(f'exclusion_policy_publication_total{{outcome="{outcome}"}} {count}')
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Route + registration + contract tests**

Route factory mirroring `exclusion_policy_diagnostics_routes.py:64-115` (same auth dependency, `no-store` header, `operation_id="getMetricsExposition"`). Contract tests: (a) document carries exactly the one GET operation with the text/plain response; (b) route requires an authenticated web session; (c) device credentials rejected; (d) forbidden-substrate scan over rendered output (closed tokens/counts only); (e) OpenAPI snapshot + generated client regenerated (`uv run poe api-contract-check` drives the check).

- [ ] **Step 5: Run gates + commit + retire the row**

```bash
uv run poe exclusion-policy-test
uv run poe api-contract-check
uv run poe verify
```

```bash
git add apps/api/src/api_runtime/metrics_exposition.py apps/api/src/api_runtime/metrics_exposition_routes.py apps/api/src/api_runtime/application.py apps/api/src/api_runtime/server.py tests/ docs/operations/exclusion-policy-publication.md packages/api-client docs/handoff/BACKLOG.md
git commit -m "feat: expose the policy counters through a prometheus text sink"
```
Remove the `| 2026-08-24 | policy-observability | No production metrics sink exists...` row. Fallback note: sink render failure never blocks evaluation — the route returns the typed dependency-error family; a contract test pins it.

---

### Task 2: Close the reconciliation-`leased` staleness row as code-stands

**Files:**
- Modify: `docs/operations/exclusion-policy-publication.md` (worker-liveness section gains the ruling)
- Modify: `docs/handoff/BACKLOG.md` (row removal)

**Interfaces:**
- Consumes the domain grounding (verified 2026-08-31): `POLICY_RECONCILIATION_LEASE_SECONDS = 60` reclaimed by any live worker's next cycle (`policy_reconciliation.py:309` `reclaim_lease_update_statement`); `ReconciliationState.DISPATCHED` is the documented resting state of a healthy intent (`exclusion_policy/reconciliation.py:74-85`); the preview staleness sweep (`exclusion_policy_composition.py:134-146`, `STALE_RUNNING_THRESHOLD_SECONDS` = the 15-minute preview deadline) already detects a dead worker for the same sweep class.

- [ ] **Step 1: Write the ruling into the operations doc**

Add to the worker-liveness section:

```markdown
### Reconciliation intents and worker death (code-stands, 2026-08-31)

Reconciliation intents carry no age-based staleness verdict by design:
leases cover only the workflow-start call (60 s, reclaimed by any live
worker's next cycle) and `dispatched` is the resting state of a healthy
intent while its Temporal batches run — an age bound on it would
false-positive. A dead worker is detected by the preview staleness sweep
of the same sweep class (`stale_running_previews`, `worker_stale_running`).
An honest `leased`/`dispatched` verdict requires a domain-defined
execution deadline or heartbeat introduced with scheduling hardening;
until that hardening lands, this is completeness, not a blind spot.
```

- [ ] **Step 2: Retire the row with the ruling as its terminal disposition**

Remove `| 2026-08-24 | policy-workers | Reconciliation intent stuck leased has no staleness verdict...` from BACKLOG.

- [ ] **Step 3: Commit**

```bash
git add docs/operations/exclusion-policy-publication.md docs/handoff/BACKLOG.md
git commit -m "docs: record the reconciliation-leased code-stands ruling"
```

---

### Task 3: Policy-key CLI exception-class token

**Files:**
- Modify: `apps/api/src/api_runtime/exclusion_policy_commands.py:1152-1168` (`run_policy_key_command`)
- Test: `tests/unit/api_runtime/test_exclusion_policy_commands.py` (or the module's existing test file — locate with `rg -ln "run_policy_key_command" tests/`)

**Interfaces:**
- Consumes: the ratified G5/C5 pattern — `authentication_commands.py:198-230` (`_exception_class_token`, `_MAXIMUM_EXCEPTION_CLASS_TOKEN_LENGTH = 64`, `_UNKNOWN_EXCEPTION_CLASS_TOKEN = "unknown_error"`) and its tests (`test_authentication_commands.py:251-280`).
- Produces: failure line `personal-api: internal_error: <closed_class_token>` on the policy-key dispatch's unexpected-exception path.

- [ ] **Step 1: Write the failing test**

```python
def test_policy_key_unexpected_exception_carries_the_closed_class_token(capsys, monkeypatch) -> None:
    def _explode() -> int:
        raise TimeoutError("never rendered")
    monkeypatch.setattr(
        "personal_os.api_runtime.exclusion_policy_commands._run_key_subcommand", _explode, raising=False,
    )
    exit_code = run_policy_key_command(arguments=parse_args(["policy-key", "status"]))
    assert exit_code == _EXIT_INTERNAL
    assert capsys.readouterr().err.strip() == "personal-api: internal_error: timeout_error"
```

(Adapt the monkeypatch target to the actual inner call the `except Exception` wraps — read `run_policy_key_command` L1152-1168 first; the assertion contract is the line.)

- [ ] **Step 2: Run to verify it fails** — current output is bare `personal-api: internal_error`.

- [ ] **Step 3: Implement (local duplication of the helper, per repetition-over-abstraction)**

Copy `_exception_class_token` + its three constants into `exclusion_policy_commands.py` verbatim (with the provenance comment "same closed-token contract as the authentication commands"), then:

```python
    except Exception as error:
        print(
            f"personal-api: internal_error: {_exception_class_token(error)}",
            file=sys.stderr,
        )
        return _EXIT_INTERNAL
```

- [ ] **Step 4: Run + commit + retire the row**

Run: `uv run pytest tests/unit/api_runtime -k policy_key -q && uv run poe verify` — PASS.

```bash
git add apps/api/src/api_runtime/exclusion_policy_commands.py tests/ docs/handoff/BACKLOG.md
git commit -m "fix: surface the exception class token in the policy-key cli"
```
Remove the `| 2026-08-24 | policy-cli | run_policy_key_command still prints bare internal_error...` row.

---

### Task 4: CI consistency-pins row — verify and extend coverage

**Files:**
- Modify: `tests/contract/test_ci_security.py:202-207` (prefetch-coverage tuple gains `exclusion-policy-acceptance.yml`)
- Modify: `docs/handoff/BACKLOG.md` (row removal)

**Interfaces:**
- Consumes (verified present 2026-08-31): the full pin set in `canonical-core-acceptance.yml` (env L46, guard L101, length L104, pull L66, teardown L159-164), `canonical-postgresql-baseline.yml` (L96/126/129/139/157/161), `exclusion-policy-acceptance.yml` (L36/93/96/108/177-182), `local-service-stack.yml` (L247/275/278/288/313/317); `object-storage-live.yml` runs no compose project (pin set N/A).

- [ ] **Step 1: Extend the prefetch-coverage tuple**

In `test_stack_workflows_prefetch_images_before_live_gates` (L194-215), add `"exclusion-policy-acceptance.yml"` to the covered tuple (its L103-108 step is named "Prefetch pinned stack images" with `pull --quiet` — verify the literal step name matches before extending; if the name differs, extend the assertion to accept it or rename the step to the canonical name).

- [ ] **Step 2: Run the CI contract suite**

Run: `uv run pytest tests/contract/test_ci_security.py -q`
Expected: PASS with the widened tuple.

- [ ] **Step 3: Commit + retire the row**

Remove `| 2026-08-16 | ci-workflows (pre-existing) | Stack workflows other than authentication-acceptance.yml lack the mutual project-name/guard consistency pins...` from BACKLOG — the row's terminal disposition cites this task's verification (the pins landed in later waves; the 2026-08-30 index simply outlived them).

```bash
git add tests/contract/test_ci_security.py docs/handoff/BACKLOG.md
git commit -m "test: verify the stack workflow pin set and widen prefetch coverage"
```

---

### Task 5: The single live smoke round (both checklists)

**Files:**
- Modify: operator evidence records under `docs/operations/` per each runbook's examples
- Modify: `docs/handoff/BACKLOG.md` (two row removals — only after the round completes)

**Interfaces:**
- Consumes: `bash .local/serve-live-ci.sh up <knowledge-ci-*>` / `down` (mandatory launch path), the readback routes `GET /api/admin/exclusion-policy/diagnostics`, `GET /api/admin/sync/rejections`, `GET /api/admin/source-lifecycle/rejections`, `GET /api/admin/exclusion-policy` (route strings verified), and the Obsidian WDIO harness for the wrong-origin readback (`tools/obsidian_live_acceptance_bootstrap.py` + `.local/e2e-totp-code.py` per AGENTS).

- [ ] **Step 1: Pre-round prerequisite verification**

Confirm the two 2026-08-24 §5.1/§5.2 prerequisites' current state: (a) worker diagnostics sink — is `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` wired per worker in `.local/run-worker.sh`? (b) the Web Admin rendering decision for `worker_stale_running`/lifecycle rejections (UI line vs authenticated endpoint through the session). Either state is acceptable; record which one the round uses (W1 readback is optional unless a sink is enabled; the staleness/rejection readbacks may go through the authenticated routes with a Web Admin session). If either prerequisite turns out unlanded AND the round needs it, land it in this task first — it becomes part of this plan, not a new deferral.

- [ ] **Step 2: Stand the disposable stack**

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-diagnostics-smoke-<date>
```
Expected: services healthy, API ready (the 2026-08-30-documented `exclusion_policy_not_initialized` readiness caveat may fail the sub-gate — if so, proceed with the suites/round only if the documented caveat is the sole failure, and publish the policy per `.local/publish-policy-revision.py` before triggering policy evaluations).

- [ ] **Step 3: Policy-observability checklist (remediation spec criterion 4)**

1. Trigger: point the signer at a broken key (or stop one policy worker process started by the serve script).
2. Drive one content operation (small-file create through the plugin/API).
3. Readback: `GET /api/admin/exclusion-policy/diagnostics` shows a `failed` evaluation row + `recent_failures` with the closed code; `GET /api/admin/sync/rejections` carries the SYSTEM code; the rotating API diagnostics log under `.local/runtime-logs/` holds the typed exchange.
4. Restore the signer/worker; confirm counters converge.

- [ ] **Step 4: Closed-reason-surfacing checklist (remediation spec criterion 4)**

1. A-class wrong-origin: bootstrap the WDIO Obsidian harness (`uv run python tools/obsidian_live_acceptance_bootstrap.py --project knowledge-ci-diagnostics-smoke-<date>`; TOTP via `.local/e2e-totp-code.py`), point the plugin at an origin that rejects the credential, let one refresh/poll fail. Readback: the settings Sync status detail line names the closed token; the terminal case renders `Last cleared reason: <token>`.
2. W3 staleness: with a preview dispatched, stop the preview worker; wait past the 15-minute bound. Readback: `GET /api/admin/exclusion-policy` `stale_running_previews` carries `{"reason": "worker_stale_running", "age_seconds": N}`. Restart the worker; rows converge or fail closed.
3. L1 lifecycle ring: trigger one typed lifecycle 4xx (a locator conflict on restore is the cheap trigger). Readback: `GET /api/admin/source-lifecycle/rejections` names the closed `error_code`; match it against the plugin trail's parked outcome.
4. W1 dispatch events only if Step 1 found a sink enabled.

- [ ] **Step 5: Record sanitized evidence + tear down**

Record each readback exactly like the runbook's examples (closed tokens, counts, timestamps; no paths/hostnames/credentials/content) in the operator record the owning runbook names (`docs/operations/sync-error-tracing.md` and the policy operations doc families).

```bash
bash .local/serve-live-ci.sh down
```

- [ ] **Step 6: Retire both rows**

Remove `| 2026-08-24 | closed-reason-surfacing | Live smoke round of the remediation surfaces...` and `| 2026-08-24 | policy-observability | Live smoke round of the policy diagnostics surfaces...` from BACKLOG — only after every required readback above is observed and recorded. If any readback cannot be produced, the affected row STAYS and the handoff reports the blocking gate with evidence; no partial completion claim.

```bash
git add docs/operations/ docs/handoff/BACKLOG.md
git commit -m "docs: record the diagnostics live smoke evidence"
```

---

### Task 6: Final verification

- [ ] **Step 1: Full offline gates**

```bash
uv run poe verify
uv run poe exclusion-policy-test
uv run poe api-contract-check
```
Expected: all exit 0 (the new route's OpenAPI delta is the only expected snapshot change).

- [ ] **Step 2: BACKLOG check**

Run: `rg -n "2026-08-24 \| (closed-reason-surfacing|policy-workers|policy-observability|policy-cli)|2026-08-16 \| ci-workflows" docs/handoff/BACKLOG.md`
Expected: no hits — all five rows plus the ride-along retired.

- [ ] **Step 3: Handoff readiness**

Confirm `git status --short` shows only intended files; the plan handoff records: the live-round evidence pointers, the code-stands ruling reference, the mutation-run pointer is NOT this plan's (it belongs to the exclusion-policy plan), and the sink route's runbook note.

## Self-review notes

Spec coverage: C1→Task 1, C2→Task 2, C3→Task 3, C4→Task 5, C5→Task 4. Sequencing inside the plan holds (sink, ruling, CLI, CI verification are offline and land first; the live round is last and reuses the Task 1 sink readback surfaces). Error cases in the spec map to: sink-failure fallback (Task 1 pinned contract test), unproducible readback (Task 5 Step 6 keeps the row open), pin-related real-runner failure (owned by the exclusion-policy plan's observation task, cross-referenced). Key-shape caution flagged inline: `evaluation_counters` tuple shapes must be read from `metrics.py:395-402` before finalizing the renderer test.
