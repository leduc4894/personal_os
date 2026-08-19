# Publication Policy Decision Binding Handoff

## Final commit

The final implementation commit is `d67d1d6`. The final documentation/evidence
commit and branch HEAD reviewed for this handoff snapshot is `8bbe12c`; it
follows the prior runtime-context documentation commit `6858ce2`. Together,
`d67d1d6` and `8bbe12c` close the claimed-upload resume, live
receiving-observer, offline terminal-fence, plan-completion, and evidence
findings without a public wire/schema/fingerprint change.

The earlier runtime-secret implementation commit is `efe10a6`, following
allowlist commits `104ff9a` and `27ac14e`. They follow final-review completion
commits `23e1f9e`, `6e4e663`, `d1864a8`, and `cdbac3d`, and the original binding
commits through `d6a4b73`. The runtime-context follow-up closes the
`StackContext` propagation and authentication key-ID collision findings
without weakening the subprocess environment boundary.

## Gate evidence

| Command | Exit | Result |
| --- | ---: | --- |
| Focused claimed-resume Vitest (RED) | 1 | durable event stayed `waiting_retry` |
| Unknown-token resume Vitest (RED) | 1 | one forbidden content request observed |
| Focused plugin Vitest (final) | 0 | 4 files, 75 passed |
| `pnpm --filter @workspace/obsidian-plugin test` | 0 | 26 files, 374 passed |
| Offline revision-drift pytest (RED) | 1 | expected identity mismatch was not raised |
| `uv run pytest tests/unit/api_runtime/test_small_file_sync_composition.py -q` | 0 | 11 passed |
| Small-file unit/adapter/integration selection | 0 | 158 passed |
| Disposable PostgreSQL small-file operation integration | 0 | 17 passed |
| Real WDIO receiving assertion (RED) | 1 | operation existed but was not yet observed receiving |
| Real WDIO receiving-race journey (GREEN) | 0 | 1 passed in 1m26s |
| Later final-build real WDIO reruns | 1 | allowed upload HTTP 500 after successful R2 resolve; race not reached |
| `pnpm --recursive run lint` | 0 | all workspace projects passed |
| `pnpm --recursive run type-check` | 0 | strict TypeScript passed |
| `pnpm --recursive run build` | 0 | API client, plugin, and Web passed |
| `uv run poe exclusion-policy-test` | 1 | 1,497 passed; 3 prerequisite-only errors from PostgreSQL clients absent on PATH |
| Pinned PostgreSQL 18.4 backup/restore rerun | 0 | exact 3 cases passed |
| `uv run poe canonical-core-test` | 0 | 989 passed, 11 skipped |
| Initial `uv run poe verify` | 1 | cross-language gate rejected widened internal failure vocabulary |
| Final `uv run poe verify` | 0 | all format/lint/type/boundary/test/build gates passed |
| OpenAPI/generated-client and migration/table diffs | 0 | empty against `2035e3a` |
| `git diff --check` | 0 | clean |

- Claimed-resume RED: the durable journey's second pass remained
  `waiting_retry` after the server had claimed the first content request. GREEN:
  the next pass re-preflights, recognizes only the claimed-state retry response,
  reuses the unchanged persisted token, performs two content attempts total,
  and records one publication/digest/terminal receipt. A supplemental RED
  proved that collapsing unknown and claimed operation failures would resume an
  unknown token; GREEN keeps the shared `operation_retry_required` contract but
  marks only claimed state as internally resumable. Token drift and policy
  exclusion never resume content.
- Offline-fence RED: revision drift terminalized instead of raising. GREEN: the
  offline store compares workspace, device, event, idempotency, operation,
  digest, size, media type, reserved/update source geometry, update base, and
  policy revision before the terminal transition; the focused file passed 11
  tests.
- Live-observer RED: a real WDIO run failed the tightened assertion because the
  observer returned when the fixture operation merely existed. GREEN: its
  digest-scoped read-only query waited for exactly one `receiving` row with no
  result before publishing the denying revision. The real journey then passed
  (1 spec, 1m26s): allowed evidence was exactly one joined publication; the
  race evidence was zero canonical publication/commit, one receiving operation,
  and recovery to `excluded_policy` after reload.
- Later live reruns did not supersede that positive race evidence: three
  attempts failed earlier on the allowed fixture with sanitized HTTP 500 after
  successful R2 resolve, leaving the durable event nonterminal. The observer
  race was not reached. No secret, content, digest, locator, token, or identifier
  was printed; the live-runtime concern is indexed below.
- Focused plugin selection: 4 files and 75 tests passed. Complete plugin suite:
  26 files and 374 tests passed; ESLint, strict TypeScript, and production build
  passed. The cross-language wire corpus passed without widening its failure
  vocabulary.
- Offline/API focused unit file: 11 passed. Small-file unit/adapter/integration
  regression: 158 passed. Real disposable PostgreSQL small-file operation
  integration: 17 passed after one transient host/container timestamp-skew
  setup attempt; no operation test had run in the failed setup.
- `uv run poe exclusion-policy-test` ran 1,497 passing cases, 2 skips, and 1
  deselection; its only three setup errors were the installed PostgreSQL 18.4
  clients missing from that shell's PATH. After prepending the pinned client
  directory and verifying both versions as 18.4, the exact three mandatory
  backup/restore cases passed. Aggregate: 1,500 passed, 2 skipped, 1 deselected.
- `uv run poe canonical-core-test`: 989 passed, 11 skipped.
- Fresh `uv run poe verify`: exit 0 after 3,026 Python tests passed, 21 skipped,
  324 deselected; all workspace JavaScript tests passed (plugin 374, Web 139,
  API client 1), plus format, lint, strict typing, import boundaries, API
  artifacts, Python packages, plugin, client, and Web production builds.
- OpenAPI/generated-client and canonical migration/table diffs against
  `2035e3a` were empty. Final `git diff --check` passed; the operator stack was
  stopped with volumes preserved and the existing tunnel was untouched.

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

All claimed-upload implementation findings are complete. Repeated later live
runs exposed a separate allowed-fixture HTTP 500 after successful R2 resolve;
because the successful receiving-race run and all deterministic gates predate
and survive it, this is a non-blocking live-runtime investigation rather
than an unverified implementation claim. It is indexed once in
`docs/handoff/BACKLOG.md`. The pre-existing signing-key verifier-chain item
remains indexed and was not expanded by this work.

## Canonical documentation links

- `docs/superpowers/specs/2026-08-19-publication-policy-decision-binding-design.md`
- `docs/superpowers/plans/2026-08-19-publication-policy-decision-binding.md`
- `docs/operations/plugin-journal-small-file-sync.md`
- `docs/operations/exclusion-policy-publication.md`
- `.local/RESTART.md` (ignored local operator runbook)

## Next actions

The branch is ready for scoped re-review and integration. Investigate the
sanitized post-R2-resolve live 500 separately without weakening durable retry
or fixture scoping. `knowledge-local` is
stopped with volumes/secrets preserved and ports 8000/38000 have no listeners;
the existing tunnel remains untouched. Keep the stack stopped when no live test
is running. Do not manually edit receiving upload rows or their deadlines.
