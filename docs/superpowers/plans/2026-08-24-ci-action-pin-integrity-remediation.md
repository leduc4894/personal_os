# CI Action Pin Integrity Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore GitHub Actions admission for the affected Node-installing jobs and prevent an incorrect `actions/setup-node` SHA from merging undetected.

**Architecture:** Keep this a source-level CI integrity contract. A focused offline pytest module obtains the tracked workflow files, extracts only `actions/setup-node` `uses:` lines, and rejects every value other than the one approved full SHA while naming only repository-relative location data. The three workflow edits then replace precisely the known one-character typo; no workflow behavior, toolchain version, or dependency changes.

**Tech Stack:** GitHub Actions YAML, Python 3.14, pytest, `uv`, Poe, Git.

**Spec:** `docs/superpowers/specs/2026-08-24-ci-action-pin-integrity-remediation-design.md`

## Global Constraints

- The approved reference is exactly `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0`.
- Inspect tracked `.yml` and `.yaml` files beneath `.github/workflows/`; require a non-empty set of `actions/setup-node` lines.
- A failure lists only workflow-relative path, line number, and the offending action reference. Do not print workflow environment values, secret/credential material, or use a network/API lookup.
- Correct only the three known invalid pins. Do not change Node.js, pnpm, uv, other action pins, permissions, triggers, cache settings, or frozen-install commands.
- Add no production/runtime dependency, schema, public API, migration, or generalized action-scanning behavior.
- Preserve the existing `# v7.0.0` comments and frozen Node/pnpm workflow behavior byte-for-byte except for the one incorrect SHA character.

## File Structure

- `tests/contract/test_setup_node_pin_integrity.py` — isolated offline source contract, tracked-workflow discovery, location-safe failure rendering, and regression mutation.
- `.github/workflows/quality.yml` — correct the Windows portability job's `actions/setup-node` SHA only.
- `.github/workflows/authentication-acceptance.yml` — correct its `actions/setup-node` SHA only.
- `.github/workflows/exclusion-policy-acceptance.yml` — correct its `actions/setup-node` SHA only.

---

### Task 1: Add the offline setup-node pin contract (RED first)

**Files:**

- Create: `tests/contract/test_setup_node_pin_integrity.py`

**Interfaces:**

- Produces `APPROVED_SETUP_NODE_REFERENCE: Final[str]`, whose value is `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020`.
- Produces `_tracked_workflow_paths() -> list[Path]`, returning sorted tracked `.github/workflows/**/*.yml` and `.yaml` paths only.
- Produces `_setup_node_references(workflow_paths: Sequence[Path]) -> list[tuple[str, int, str]]`, where each tuple is `(repository_relative_posix_path, one_based_line_number, reference)`.
- Produces `_assert_approved_setup_node_references(references: Sequence[tuple[str, int, str]]) -> None`, which raises a single assertion carrying only invalid locations and references.

- [ ] **Step 1: Write the failing repository contract and its mutation regression.** Create the test module with standard-library imports only (`re`, `subprocess`, `Path`, `Sequence`, and `Final`). Obtain paths through `git ls-files -z -- .github/workflows` with captured output, retain only `.yml`/`.yaml` entries, and parse workflow text line-by-line with a regex anchored to YAML `uses:` keys that accepts optional single/double quotes around `actions/setup-node@…`. Assert at least one matching reference, then reject every reference not equal to the approved constant. Render each violation as `<relative-path>:<line>: <offending-reference>`.

```python
def _assert_approved_setup_node_references(
    references: Sequence[tuple[str, int, str]],
) -> None:
    assert references, "tracked workflows must contain actions/setup-node"
    invalid = [
        f"{path}:{line_number}: {reference}"
        for path, line_number, reference in references
        if reference != APPROVED_SETUP_NODE_REFERENCE
    ]
    assert not invalid, "invalid actions/setup-node references:\n" + "\n".join(invalid)


def test_every_tracked_setup_node_reference_uses_the_approved_full_sha() -> None:
    _assert_approved_setup_node_references(
        _setup_node_references(_tracked_workflow_paths())
    )
```

Add a `tmp_path` regression workflow containing the historical typo, invoke the same parser/assertion boundary, and use `pytest.raises(AssertionError, match=...)` to require its relative path, `:1:`, and `actions/setup-node@820762786026740c76336085b0efc47a31fe5020` in the failure. Do not require YAML parsing or a network call.

- [ ] **Step 2: Run the RED contract.** Run `uv run pytest tests/contract/test_setup_node_pin_integrity.py -q`. Expected: the repository-wide test fails and reports exactly the currently invalid `quality.yml`, `authentication-acceptance.yml`, and `exclusion-policy-acceptance.yml` locations/references; the temporary-workflow regression passes, proving the failure message is location-safe.

- [ ] **Step 3: Make the contract deterministic and type-safe.** Ensure `subprocess.run` uses `check=True`, `capture_output=True`, `text=True`, `cwd=REPO_ROOT`, and a literal command tuple; sort paths by repository-relative POSIX string. Strip YAML comments and optional quotes only while extracting the action reference. Keep the test module self-contained rather than changing the broad SHA-pin contract in `tests/contract/test_ci_security.py`.

- [ ] **Step 4: Re-run the focused test to preserve the RED evidence.** Run `uv run pytest tests/contract/test_setup_node_pin_integrity.py -q`. Expected: still fails only at the three repository typo assertions until Task 2 changes their source lines.

- [ ] **Step 5: Commit the RED test only.** Commit `tests/contract/test_setup_node_pin_integrity.py` with message `test: enforce setup-node pin integrity`.

### Task 2: Correct the three workflow references

**Files:**

- Modify: `.github/workflows/quality.yml:92`
- Modify: `.github/workflows/authentication-acceptance.yml:45`
- Modify: `.github/workflows/exclusion-policy-acceptance.yml:54`
- Test: `tests/contract/test_setup_node_pin_integrity.py`

**Interfaces:**

- Consumes Task 1's `APPROVED_SETUP_NODE_REFERENCE` and assertion contract.
- Produces three workflow lines whose action reference is exactly the approved 40-character SHA and whose existing `# v7.0.0` comment remains unchanged.

- [ ] **Step 1: Replace only the typo character on each failing line.** In the three specified workflow files, replace `actions/setup-node@820762786026740c76336085b0efc47a31fe5020` with `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020`. Leave indentation, step name, `with:` block, comment, and all surrounding workflow content unchanged.

- [ ] **Step 2: Run the focused contract GREEN.** Run `uv run pytest tests/contract/test_setup_node_pin_integrity.py -q`. Expected: all tests pass, including the temporary historical-typo rejection, with no network activity.

- [ ] **Step 3: Prove the restricted workflow diff.** Run `git diff --check` and `git diff -- .github/workflows/quality.yml .github/workflows/authentication-acceptance.yml .github/workflows/exclusion-policy-acceptance.yml`. Expected: `diff --check` exits 0 and the workflow diff contains exactly three one-line SHA substitutions; `# v7.0.0`, frozen installs, and all other action versions remain unchanged.

- [ ] **Step 4: Run adjacent existing CI-security coverage.** Run `uv run pytest tests/contract/test_setup_node_pin_integrity.py tests/contract/test_ci_security.py -q`. Expected: exit 0, retaining the broader least-privilege/full-SHA controls while the new test enforces this action's exact revision.

- [ ] **Step 5: Commit the remediation.** Commit the three workflow files with message `fix: correct setup-node action pins`.

### Task 3: Run repository gates and observe GitHub admission

**Files:**

- Verify: `tests/contract/test_setup_node_pin_integrity.py`
- Verify: `.github/workflows/quality.yml`, `.github/workflows/authentication-acceptance.yml`, `.github/workflows/exclusion-policy-acceptance.yml`
- Verify: `pyproject.toml:309-311`

**Interfaces:**

- Consumes the passing offline contract and corrected workflow sources from Tasks 1–2.
- Produces local quality evidence and, once a pull request or protected-master run is available, GitHub-side admission evidence that Node setup begins instead of action resolution failing.

- [ ] **Step 1: Run the complete local verification gate.** Run `uv run poe verify`. Expected: exit 0; this includes format, lint, type-check, boundary checks, all tests (including the new contract), and builds. Record the command's actual exit status and concise totals in the handoff.

- [ ] **Step 2: Inspect the final worktree.** Run `git diff --check`, `git status --short`, and `git diff HEAD -- tests/contract/test_setup_node_pin_integrity.py .github/workflows/quality.yml .github/workflows/authentication-acceptance.yml .github/workflows/exclusion-policy-acceptance.yml`. Expected: no whitespace errors and no unrelated file changes. Confirm the test failure text contains no environment/credential tokens by reviewing the literal assertion rendering.

- [ ] **Step 3: Observe the protected GitHub workflow runs after the change is submitted through the repository's normal review path.** For `quality.yml` (Windows portability), `authentication-acceptance.yml`, and `exclusion-policy-acceptance.yml`, confirm the `Setup Node` step is admitted and starts; specifically, there must be no pre-checkout/action-resolution error for `820762786026740c76336085b0efc47a31fe5020`. This is integration evidence, not a replacement for the offline test. If the run cannot be submitted or observed in the current authority scope, record it as an open external gate rather than claiming it passed.

- [ ] **Step 4: Create one implementation handoff.** After all available gates finish, create exactly one `docs/handoff/2026-08-24-ci-action-pin-integrity-remediation.md` containing the final commit SHA, Task 1 RED evidence, focused/adjacent/full verification evidence, the three exact SHA-only edits, GitHub admission status, and any genuinely deferred external observation. Add one `docs/handoff/BACKLOG.md` row only if the GitHub observation remains deferred, with `Implement by: Before production activation`; otherwise add no backlog item.

- [ ] **Step 5: Commit the handoff.** If the handoff is created, commit it with message `docs: hand off ci action pin remediation`.

## Plan Self-Review

- **Spec coverage:** Task 1 implements the non-empty, tracked `.yml`/`.yaml`, exact-SHA, offline, location-safe contract and historical-typo negative case. Task 2 corrects exactly the three named references while retaining comments and installation behavior. Task 3 covers `poe verify`, diff hygiene, and the protected-run admission criterion.
- **Scope guard:** No task changes action versions other than the typo correction, permissions, triggers, caches, Node/pnpm/uv, dependencies, runtime code, schema, API, or network behavior.
- **Interface consistency:** Every later task consumes the exact constant and parser/assertion boundary defined in Task 1; Task 2 is the only source mutation; Task 3 is verification and operational evidence only.
- **Placeholder scan:** No unresolved implementation placeholder appears. The only conditional outcome is the externally authorized GitHub-run observation, with an explicit handoff/backlog decision.
