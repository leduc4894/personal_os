# Conflict Vault-Apply Hardening Design

## Goal

Eliminate hidden staging siblings that a conflict canonical-outcome apply can
leave after a crash, while consolidating the duplicate stage/verify/replace
discipline used by device-sync and conflict resolution.

## Scope

- Extract one plugin-internal, Obsidian-agnostic primitive for same-directory
  hidden staging, fingerprint verification, narrow replace, retained rollback
  and safe sibling cleanup.
- Make `AtomicVaultWriterImpl` consume that primitive without changing its
  public port, durable device-sync state machine, or closed device-sync error
  vocabulary.
- Make `createConflictCanonicalOutcomeApplier` consume the same primitive and
  add a bounded sweep of only its own hidden staging siblings after failed or
  resumed conflict apply attempts.

## Required behavior

1. The shared primitive accepts an injected `VaultMutationSeam`, target
   locator, opaque staging token, expected final fingerprint, optional pinned
   base fingerprint, and bytes; it returns only verified mutation evidence or
   a closed caller-mapped failure.
2. It retains the existing safety ordering: stage hidden sibling, verify staged
   bytes, prove target/base condition, narrowly replace while retaining
   verified rollback bytes, verify final bytes, then cleanup only the exact
   opaque-token siblings it owns.
3. A conflict apply that crashes or throws between staging and replace leaves
   at most exact hidden siblings named by its opaque token. A later retry or
   recovery sweep removes those siblings only after proving that they match
   that apply's naming contract; unrelated hidden files are never listed,
   read, renamed or removed.
4. Failure to remove a sibling remains best-effort: it must not claim the
   canonical conflict resolution applied, mask a vault-apply failure, or cause
   data loss. It surfaces one existing closed diagnostic path rather than raw
   exception text.
5. Existing device-sync recovery semantics remain unchanged: ambiguous bytes
   are preserved and block rather than guessed; visible user content is still
   sent through the Vault trash path and never permanently deleted.

## Non-goals

- No conflict API, canonical source-conflict state, policy decision, database
  migration, or Web Conflict Inbox change.
- No broad vault cleanup, filesystem scan, or removal based on a glob/prefix
  without an operation-owned opaque token.
- No Desktop/Mobile Conflict Inbox live journey.

## Design constraints

- The shared module may depend only on plugin-safe primitives and must not
  import Obsidian directly.
- TypeScript remains strict; public `AtomicVaultWriter` and
  `CanonicalOutcomeApplier` interfaces remain stable unless a test proves an
  internal-only extension is unavoidable.
- Diagnostics retain closed tokens and exclude locators, bytes, digests,
  conflict IDs and credentials.

## Acceptance criteria

- New unit tests prove a failed conflict apply cleans its exact staging sibling
  on retry/recovery and never removes an unrelated hidden sibling.
- Existing device-sync atomic-writer and conflict-composition suites pin the
  same success, rollback, ambiguity and trash behavior after extraction.
- Plugin type-check, lint, test and build gates pass.
- Both source-conflicts maintenance rows are removed from BACKLOG only after
  the suites pass; the Desktop Conflict Inbox live-evidence row remains.

