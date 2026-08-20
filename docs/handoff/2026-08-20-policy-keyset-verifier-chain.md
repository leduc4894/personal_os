# Policy Keyset Verifier Chain Handoff

**Date:** 2026-08-20
**Spec:** `docs/superpowers/specs/2026-08-20-policy-keyset-verifier-chain-design.md`
**Plan:** `docs/superpowers/plans/2026-08-20-policy-keyset-verifier-chain.md`

## Final commit

`b7fdc16 fix: verify small-file policies with snapshot anchors`

The runtime interface remains unchanged. The documentation commit that carries
this handoff records the accepted code result.

## Gate evidence

The regression was observed failing before the composition change because the
configured current signer could not verify a valid active snapshot signed by a
previous canonical key. The following sanitized final outcomes were recorded
from the code commit:

| Command | Outcome |
| --- | --- |
| `uv run pytest tests/unit/api_runtime/test_small_file_sync_composition.py::test_serve_composition_verifies_an_active_snapshot_signed_before_rotation -q` | 1 passed |
| `uv run pytest tests/unit/api_runtime/test_small_file_sync_composition.py -q` | 12 passed |
| `uv run pytest tests/unit/api_runtime/test_exclusion_policy_crypto.py tests/unit/exclusion_policy/test_enforcement.py -q` | 35 passed |
| `uv run mypy apps/api/src/api_runtime/small_file_sync_composition.py` | Success: no issues found in 1 source file |
| `uv run ruff check apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/api_runtime/test_small_file_sync_composition.py` | All checks passed |
| `git diff --check` before the code commit | no whitespace errors |

## Decision and rationale

`compose_small_file_sync()` now uses one `TrustAnchorEd25519Verifier` for its
enforcement, publication, and canonical-read paths. It verifies each active
snapshot against the public key attached through the canonical
snapshot-to-signing-key join, so a valid snapshot signed before a completed
key rotation remains enforceable. New publication continues to use the
configured current signer and its existing startup validation.

This preserves the fail-closed policy boundary in
`docs/01-CANONICAL_ARCHITECTURE.md` and the administration ownership decision
in ADR-012 of `docs/19-ARCHITECTURE_DECISIONS.md`. No schema, migration, HTTP,
or generated-client contract changed.

## Deferred items

None. The completed 2026-08-19 exclusion-policy verifier-chain/signing-key
rotation item was removed from `docs/handoff/BACKLOG.md` after the code gates
passed.

## Next actions

Continue the Phase 2 work tracked in `docs/20-IMPLEMENTATION_PLAN.md`. Future
policy changes must retain canonical-anchor verification and focused
post-rotation regression coverage.
