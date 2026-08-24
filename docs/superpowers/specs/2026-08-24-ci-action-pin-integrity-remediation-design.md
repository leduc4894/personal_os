# CI Action Pin Integrity Remediation Design

**Status:** Proposed
**Date:** 2026-08-24
**Scope owner:** GitHub Actions workflow integrity
**Depends on:** `docs/superpowers/specs/phase 1/phase-one-workspace-bootstrap-design.md`

## 1. Objective

Restore GitHub Actions job admission for workflows that install Node.js and
make an invalid full-SHA action reference impossible to merge unnoticed.

## 2. Evidence and root cause

The 2026-08-24 protected-master runs fail before checkout or any repository
test executes because GitHub cannot resolve this reference:

```text
actions/setup-node@820762786026740c76336085b0efc47a31fe5020
```

The valid pinned revision already used by the Ubuntu quality job is:

```text
actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0
```

The invalid value differs by one hexadecimal character. It appears in the
Windows quality, authentication acceptance and exclusion-policy acceptance
jobs. This is a workflow-source defect, not an application, dependency or
secret failure.

## 3. Scope

In scope:

- Correct only the three invalid `actions/setup-node` references.
- Add an offline workflow-source contract that checks every `uses:` reference
  to `actions/setup-node` in `.github/workflows/` is the approved full SHA.
- Preserve the existing action version comment and frozen Node/pnpm install
  behavior.

Out of scope:

- Changing Node.js, pnpm, uv, action versions, permissions, workflow triggers
  or cache settings.
- Adding a third-party action scanner, a production dependency or network
  lookups to ordinary tests.
- Generalizing this remediation into an unpinned-action migration.

## 4. Contract

The repository-wide approved `actions/setup-node` pin is exactly
`820762786026740c76f36085b0efc47a31fe5020`, annotated `# v7.0.0`.

Every matching `uses:` line in every tracked workflow must contain that exact
SHA. A missing, abbreviated, malformed or other valid-looking SHA is a
contract failure. The offline test reports workflow-relative paths and line
numbers only; it never prints environment, secret or credential material.

The test is intentionally source-based rather than a GitHub API validation:
it must run on developer machines and before an Actions runner tries to
resolve the action. The existing protected CI runs remain the integration
evidence that GitHub can resolve the pinned revision.

## 5. Required changes

1. Replace the incorrect pin in:
   - `.github/workflows/quality.yml` (Windows portability job);
   - `.github/workflows/authentication-acceptance.yml`;
   - `.github/workflows/exclusion-policy-acceptance.yml`.
2. Add one focused Python contract test under `tests/contract/` that discovers
   tracked `.yml` and `.yaml` files beneath `.github/workflows/`, extracts
   `actions/setup-node` references, and asserts a non-empty set of references
   all equal the approved constant.
3. Cover a negative fixture or temporary workflow text containing the prior
   typo, proving that the test fails with the relative path and offending
   reference.

No application runtime code, schema, migration or public API changes.

## 6. Acceptance criteria

- The focused contract test passes with the three corrected workflow lines.
- The negative case fails before implementation and identifies the bad
  reference without a network call.
- `uv run poe verify` passes on the implementation commit.
- The affected protected-master workflows start their Node setup step rather
  than failing during GitHub's action-resolution phase.
- `git diff --check` passes and the change set contains no action-version
  upgrade or unrelated workflow reformat.

