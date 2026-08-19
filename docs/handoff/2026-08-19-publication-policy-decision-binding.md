# Publication Policy Decision Binding Handoff

## Final commit

The latest implementation commit is `efe10a6`, following runtime-secret
allowlist commits `104ff9a` and `27ac14e`. They follow final-review completion
commits `23e1f9e`, `6e4e663`, `d1864a8`, and `cdbac3d`, and the original binding
commits through `d6a4b73`. The final runtime-context follow-up closes the
StackContext propagation and authentication key-ID collision findings without
weakening the subprocess environment boundary.

## Gate evidence

- Claim/expiry RED: the new unit and deterministic PostgreSQL tests both
  failed because same-identity preflight reclaimed an expired `receiving` row.
  GREEN: the focused unit passed, the real PostgreSQL regression passed, all
  17 source-publication operation integrations passed, and all 66 small-file
  service unit tests passed.
- Secret-rotation RED: both reset/rebootstrap tests failed with
  `secret_set_removal_failed` when an allowlisted application file shared the
  secret directory. GREEN: both focused tests passed, and the local-stack
  unit/contract/integration selection passed with 176 passed, 8 skipped, and
  1 deselected.
- Merge-review secret RED: 6 focused cases failed because inspection/reset/
  rebootstrap did not accept the runtime environment contract. GREEN: current,
  multiple previous, and policy signing paths (including safe nested/versioned
  paths) survive the complete lifecycle; unsafe paths fail with a redacted
  closed code. A follow-up RED proved a previous-key filename could still
  collide with the current key; GREEN now rejects that cross-field collision
  exactly as the runtime loader does while keeping distinct names valid. The
  final local-stack selection passed with 183 passed, 8
  skipped, and 1 deselected; Ruff, format, and mypy passed.
- Runtime-context RED: 5 lifecycle/CLI cases failed while the unchanged
  sanitizer control passed. `StackContext` discarded the configured reference
  metadata, reset/bootstrap classified legitimate versioned files as partial,
  and status reached its runner for an invalid or current/previous-colliding
  key ID. GREEN: all 6 focused cases passed after retaining a typed immutable
  snapshot containing only SafeToken-validated key IDs and validated relative
  filenames. The full local-stack unit suite passed with 168 passed and 3
  skipped; the combined runtime-loader/unit/contract selection passed with 243
  passed and 8 skipped; Ruff, format, mypy, CLI help, and diff checks passed.
- The real disposable local-stack smoke ran under the isolated project
  `knowledge-ci-runtime-context-0819` and passed 1 test in 195.13 seconds.
  Exact-label container, network, and volume inventories were empty afterward;
  the operator `knowledge-local` stack remained absent.
- Portable-subprocess RED: the behavioral clone-layout test failed because the
  subprocess searched the fixed checkout. GREEN: the real subprocess found the
  arbitrary repository-root marker through the E2E spec URL. The full Obsidian
  unit suite now passes 26 files and 369 tests; ESLint, strict TypeScript, and
  plugin build pass.
- Focused small-file unit/composition selection: 150 passed. Small-file sync
  integration: 18 passed. Combined source-publication and policy enforcement
  on disposable PostgreSQL: 27 passed.
- Live Obsidian through `wdio-obsidian-service`, the loader-provided local
  secrets, real device grant/admin TOTP, policy workers, and the existing
  Cloudflare Tunnel: all 3 specs passed. Sanitized server evidence proved
  exactly one source, source version, sync event, committed operation, and
  exact operation/publication join for the allowed upload.
- In the same live journey, a denying `.md` revision was published after
  preflight while the real content request was active. Before recovery the
  deltas were zero canonical sources/commits/exact publications and one
  `receiving` operation with no result. A real plugin unload/reload caused the
  next preflight to settle the one durable nonterminal event as
  `excluded_policy`; final source/version/event/commit deltas remained zero.
- The merge-review live rerun scoped PostgreSQL evidence to each fixture's
  per-run content identity and journal evidence to its normalized path. All 3
  real WDIO specs passed through the existing loader and Cloudflare Tunnel.
  The allowed fixture alone counted exactly 1 source/version/event/operation/
  commit/exact join and 0 receiving-unpublished rows. The race fixture alone
  counted 0 canonical source/version/event/commit/exact join, 1 operation, and
  1 receiving-unpublished row before and after reload recovery. Only sanitized
  counts were emitted.
- Obsidian unit suite: 26 files and 369 tests passed. Focused E2E journey unit
  coverage: 8 passed. ESLint, strict TypeScript, and plugin build passed.
- `uv run poe exclusion-policy-test` plus the three mandatory backup/restore
  cases rerun with repository PostgreSQL clients verified at exactly 18.4:
  aggregate 1,499 passed, 2 skipped, 1 deselected.
- `uv run poe canonical-core-test`: 977 passed, 11 skipped.
- `uv run poe verify`: passed after 3,034 selected Python tests plus all
  formatting, lint, strict typing, import-boundary, API artifact, JavaScript,
  and build gates.
- Migration/API contract selection: 32 passed. OpenAPI/generated-client,
  migration, and canonical-table diffs against `2035e3a` were empty.
- Final `git diff --check` passed. Both disposable integration projects were
  removed; `knowledge-local` was stopped with volumes preserved and ports
  8000/38000 had no listeners.

## Spec interpretations and rationale

The server owns the allowed policy revision. Preflight stores an immutable
`AllowedPolicyRevisionBinding`; plugin revision claims are not authority. At
the transaction-final policy lock, a verified unchanged revision reuses the
binding, while a revision change forces a fail-closed authoritative
evaluation. The binding remains invocation-local through the small-file
publication gateway; no request-global policy state was introduced.

`pending -> receiving` is an ownership claim. Only an expired `pending` row
may be reclaimed and atomically rebound to a new token, deadline, and policy
revision. A `receiving` row keeps its token and revision fence across expiry:
same-identity preflight rejects, the exact token may resume, and guarded
terminalization may complete after expiry when state, token, operation
identity, declared content fields, and bound revision still match. This
prevents canonical publication followed by a lost terminal write after a
competing rebind.

`stack reset --rotate-secrets` owns only its declared managed filenames. It
preserves allowlisted application files byte-for-byte, removes the managed
subset, and reboots that subset through the existing staging/rollback path so
a fresh smoke fingerprint is created without a partial-directory dead end.
Application allowlisting is the union of the documented application files and
the validated runtime-selected current authentication key, bounded parsed
previous keys, and policy signing key. Safe nested relative paths follow the
runtime grammar; absolute/traversal/backslash paths and managed-name collisions
fail closed without reading or echoing a secret value.

`StackContext.environment` remains the subprocess-only allowlisted mapping.
Before sanitizing it, context construction now parses a separate immutable
application-secret reference snapshot containing only validated key IDs and
relative filenames. Status validates this snapshot but never forwards it;
bootstrap, secret validation, smoke fingerprinting, reset, and rotation consume
the snapshot directly. Current and previous authentication IDs use the same
`SafeToken.parse` boundary as the authoritative API runtime loader, including a
fail-closed current/previous collision check with one fixed redacted code.

The live test's database helper is read-only and emits counts only. It reads
the loader-provided password file internally and never prints paths, IDs,
locators, content, credentials, tokens, or secret values.
All server queries and waits are constrained to a unique per-run fixture digest;
all journal queries use a bound normalized-path parameter. TOTP and observer
subprocesses derive cwd from the E2E spec module URL rather than a machine path.

## Deferred items and verdicts

All final-review findings are complete; this wave adds no deferred item. The
pre-existing signing-key verifier-chain item remains indexed in
`docs/handoff/BACKLOG.md` and was not expanded by this work.

## Canonical documentation links

- `docs/superpowers/specs/2026-08-19-publication-policy-decision-binding-design.md`
- `docs/superpowers/plans/2026-08-19-publication-policy-decision-binding.md`
- `docs/operations/plugin-journal-small-file-sync.md`
- `docs/operations/exclusion-policy-publication.md`
- `.local/RESTART.md` (ignored local operator runbook)

## Next actions

The branch is ready for scoped re-review and integration. `knowledge-local` is
stopped with volumes/secrets preserved and ports 8000/38000 have no listeners;
the existing tunnel remains untouched. Keep the stack stopped when no live test
is running. Do not manually edit receiving upload rows or their deadlines.
