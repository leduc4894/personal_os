# Publication Policy Decision Binding Handoff

## Final commit

The final implementation commit is `cdbac3d`. The final-review completion
commits are `23e1f9e`, `6e4e663`, `d1864a8`, and `cdbac3d`. They follow the
original binding commits through `d6a4b73` and close all four Important
whole-branch review findings.

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
- Obsidian unit suite: 25 files and 368 tests passed. Focused E2E journey unit
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

The live test's database helper is read-only and emits counts only. It reads
the loader-provided password file internally and never prints paths, IDs,
locators, content, credentials, tokens, or secret values.

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

The branch is ready for final review and integration. Keep `knowledge-local`
stopped when no live test is running; use the existing Cloudflare Tunnel only
for required HTTPS live journeys. Do not manually edit receiving upload rows
or their deadlines.
