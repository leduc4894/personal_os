# Policy Keyset Verifier Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to implement this plan task-by-task.

**Goal:** Preserve server-side policy enforcement after a valid signing-key
rotation when the active snapshot remains signed by its canonical previous key.

**Architecture:** The small-file production composition will replace its
current-signer-only `KeyedTrustAnchorVerifier` with the existing stateless
`TrustAnchorEd25519Verifier`. The PostgreSQL snapshot loader already joins the
active snapshot to its immutable signing-key anchor, so the verifier can check
the exact persisted public key without a process-frozen key map. New policy
publication remains self-verified by the configured current signer in the
separate exclusion-policy composition.

**Tech Stack:** Python 3.14, pytest, SQLAlchemy, cryptography Ed25519 adapter.

**Spec:**
`docs/superpowers/specs/2026-08-20-policy-keyset-verifier-chain-design.md`

## Global Constraints

- Do not change the keyset chain format, signing/rotation lifecycle, database
  schema, migration, HTTP/OpenAPI contract, generated clients, or dependencies.
- The configured private signer remains required for new policy publication and
  its existing startup validation against the latest current key is unchanged.
- Server enforcement must verify active snapshot material only through the
  persisted snapshot-to-signing-key anchor and fail closed on invalid material.
- Tests must demonstrate that a snapshot signed by an older trusted key remains
  enforceable after the runtime uses a newer current signer.
- Start each code task by adding and observing its named failing test; apply the
  smallest implementation change; run focused lint/type/test gates.
- Do not remove the P0 backlog item until the final regression gates pass.
- Preserve unrelated untracked files `verify.log` and `verify2.log`.

---

### Task 1: Compose enforcement with canonical snapshot anchors

**Files:**

- Modify: `apps/api/src/api_runtime/small_file_sync_composition.py`
- Modify: `tests/unit/api_runtime/test_small_file_sync_composition.py`

**Interfaces:**

`compose_small_file_sync(...) -> SmallFileSyncRuntime` remains unchanged. Its
`PolicyEnforcementService` must receive `TrustAnchorEd25519Verifier()`; the
same verifier instance continues to be passed to the publication and canonical
read stores composed by this runtime.

- [ ] **Step 1: Write the failing regression test**

  In `tests/unit/api_runtime/test_small_file_sync_composition.py`, add
  `test_serve_composition_verifies_an_active_snapshot_signed_before_rotation`.
  Build an old `Ed25519PolicySigner` and a distinct current signer. Compose the
  serve runtime with the current signer. Build an allowed
  `ActivePolicySnapshotMaterial` with a valid snapshot payload and signature
  from the old signer, including the old signer public key as the material's
  anchor. Call the composed policy guard's enforcement `evaluate_material` for
  an allowed `PolicySubject` and assert `decision.is_allowed`.

- [ ] **Step 2: Observe RED**

  Run:

  ```powershell
  uv run pytest tests/unit/api_runtime/test_small_file_sync_composition.py::test_serve_composition_verifies_an_active_snapshot_signed_before_rotation -q
  ```

  Expected: FAIL because the current-signer-only verifier cannot verify the
  otherwise valid old-key snapshot.

- [ ] **Step 3: Implement the minimum composition change**

  Replace the `KeyedTrustAnchorVerifier(Ed25519PolicyVerifier({...}))` creation
  in `compose_small_file_sync` with `TrustAnchorEd25519Verifier()`. Update only
  imports and the composition docstring needed to describe canonical snapshot
  anchors. Do not alter `compose_exclusion_policy`, which still uses the
  current signer for new-publication self-verification.

- [ ] **Step 4: Verify GREEN and adjacent safety coverage**

  Run:

  ```powershell
  uv run pytest tests/unit/api_runtime/test_small_file_sync_composition.py -q
  uv run pytest tests/unit/api_runtime/test_exclusion_policy_crypto.py tests/unit/exclusion_policy/test_enforcement.py -q
  uv run mypy apps/api/src/api_runtime/small_file_sync_composition.py
  uv run ruff check apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/api_runtime/test_small_file_sync_composition.py
  ```

- [ ] **Step 5: Commit**

  ```powershell
  git add apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/api_runtime/test_small_file_sync_composition.py
  git commit -m "fix: verify small-file policies with snapshot anchors"
  ```

### Task 2: Record the accepted fix and close the P0 backlog entry

**Files:**

- Add: `docs/superpowers/specs/2026-08-20-policy-keyset-verifier-chain-design.md`
- Add: `docs/superpowers/plans/2026-08-20-policy-keyset-verifier-chain.md`
- Modify: `docs/handoff/BACKLOG.md`
- Add: `docs/handoff/2026-08-20-policy-keyset-verifier-chain.md`

**Interfaces:** No runtime interface changes. This task records the completed
contract and evidence after Task 1 passes.

- [ ] **Step 1: Write the handoff evidence**

  Create exactly one handoff with sections `Final commit`, `Gate evidence`,
  `Decision and rationale`, `Deferred items`, and `Next actions`. Include only
  sanitized command outcomes, relevant canonical links, and the final commit
  SHA. Keep it under 400 lines.

- [ ] **Step 2: Remove only the P0 line**

  Remove the 2026-08-19 `exclusion-policy` verifier-chain/signing-key rotation
  row from `docs/handoff/BACKLOG.md`. Keep every other deferred item unchanged.

- [ ] **Step 3: Verify documentation and diff scope**

  Run:

  ```powershell
  rg -n "verifier-chain|signing-key rotation" docs/handoff/BACKLOG.md
  rg -n "TODO|TBD|PLACEHOLDER|secret|token" docs/handoff/2026-08-20-policy-keyset-verifier-chain.md
  git diff --check
  git diff --stat HEAD~1..HEAD
  ```

- [ ] **Step 4: Commit**

  ```powershell
  git add docs/superpowers/specs/2026-08-20-policy-keyset-verifier-chain-design.md docs/superpowers/plans/2026-08-20-policy-keyset-verifier-chain.md docs/handoff/BACKLOG.md docs/handoff/2026-08-20-policy-keyset-verifier-chain.md
  git commit -m "docs: hand off policy keyset verifier fix"
  ```

## Completion criteria

- The regression test is observed RED before the production composition change
  and GREEN afterward.
- Focused composition, crypto, and enforcement tests plus strict type/lint
  checks pass from the final code commit.
- The P0 backlog line is removed only after the code gates pass.
- Exactly one P0 handoff exists and unrelated workspace files are preserved.
