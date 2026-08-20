# Policy Keyset Verifier Chain Design

## Goal

Keep server-side exclusion-policy enforcement available after a valid signing-key
rotation when the active policy snapshot was signed by a previous key in the
canonical keyset chain.

## Problem

`compose_small_file_sync()` currently wraps an `Ed25519PolicyVerifier` whose
mapping contains only the configured current signer.  The policy snapshot
loader already joins the snapshot to its immutable canonical public-key anchor.
After a valid key rotation, an unchanged active snapshot signed by the prior
key therefore fails verification in the small-file serve graph, even though its
persisted key is canonical and its policy remains active.

## Decision

The small-file serve graph will use the existing `TrustAnchorEd25519Verifier`
directly for every policy enforcement, source-publication, and canonical-read
path it composes.  That verifier checks the signature only against the exact
public key joined from the active snapshot's persisted `policy_signing_keys`
row.  It does not receive or trust a process-frozen key map.

The configured signer remains the exclusive private key for new policy
publication.  Its existing startup check must still prove that it matches the
current key in the latest canonical keyset.  This change does not modify the
keyset chain format, rotation lifecycle, database schema, HTTP wire contract,
or generated client.

## Required behavior

1. A valid active policy snapshot signed by a prior canonical key remains
   enforceable after the configured signer switches to a newer current key.
2. The snapshot public key is resolved only through the existing canonical
   snapshot-to-signing-key join for the same workspace.
3. Missing/corrupt snapshot material, an unknown or malformed trust anchor,
   invalid signature, and database errors continue to fail closed through the
   existing typed errors.
4. Newly published policy revisions still use only the configured current
   signer and its self-verification mapping.

## Testing

Add a regression test around the production `compose_small_file_sync()` graph:
seed an active snapshot signed by an older key, rotate the configured signer to
a valid newer key without publishing another policy revision, then verify that
an allowed small-file policy evaluation succeeds.  The test must fail with the
old current-signer-only composition and pass using the trust-anchor verifier.
Keep focused negative verification coverage for invalid signatures/anchors.

## Acceptance criteria

- The regression test demonstrates the post-rotation unchanged-snapshot case.
- The focused small-file composition and policy enforcement suites pass.
- No public API, migration, schema, policy signing, or rotation contract
  changes are introduced.
- The completed P0 line is removed from `docs/handoff/BACKLOG.md` and one
  handoff records the verification evidence and any deferred items.
