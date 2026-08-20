# Canonical Recovery and Plugin Journey Fix Report

Date: 2026-08-20
Branch: `2026-08-19-publication-policy-decision-binding`

## Scope and constraints

- Complete the four whole-branch final-review findings without a worktree,
  subagent, push, merge, public HTTP/schema migration, new production
  dependency, or sensitive output.
- Follow systematic debugging and strict TDD: record the root-cause hypothesis,
  observe each new behavioral test fail for the intended reason, then apply the
  smallest production change and observe GREEN.
- Run the real plugin journey through the repository loader, existing local
  stack, policy workers, and existing Cloudflare Tunnel. Evidence is limited to
  fixture-scoped sanitized counts.

## Root-cause hypotheses recorded before production edits

### Recovery completeness and version semantics

The current recovery lock/count contract enumerates twenty of the thirty
migrated PostgreSQL tables. It includes the baseline, exclusion-policy, and
small-file operation tables, but omits all eight canonical authentication
tables: `user_credentials`, `web_sessions`, `totp_credentials`,
`totp_recovery_codes`, `device_token_families`, `device_tokens`,
`device_authorization_grants`, and `authentication_throttle_buckets`.
Consequently the dump contains those rows but the exported-snapshot writer
barrier and post-restore count witness do not cover them.

The v2 introduction commit `587053e` is reachable only from the current local
branch: no tag, local/remote sibling branch, or `origin` head contains it, and
`origin` has no head named for the current branch. Therefore v2 has not escaped
this branch and remains the correct current identifier. New v2 bundles can be
safely strengthened to 28 counts. To retain prior v2 parsing within this branch,
the reader must continue accepting the exact legacy 20-count v2 shape while
new writers emit only the exact 28-count current shape; restore verification
must query the count keys carried by the already validated manifest.

Expected RED: tests requiring the eight authentication tables in the current
count/lock set fail, a current v2 manifest with 28 counts is rejected, and
PostgreSQL recovery proof with authentication rows lacks the expected counts /
writer fence.

### Real claimed-resume plugin journey

The real WDIO journey currently proves an ordinary allowed publication and a
claimed operation followed by a newly denying `.md` revision. It never
interrupts a claimed upload, publishes a locator-dependent but irrelevant
revision, then proves that a same-identity preflight reauthorizes the unchanged
persisted token and that exact-token resume produces one canonical publication
and one terminal receipt.

Expected RED: the tightened live journey cannot observe the required
interrupted `receiving`/journal state and positive resume evidence because that
scenario and its fixture-scoped assertions do not exist yet.

### Windows secret-path identity

Local-stack application-secret parsing compares configured paths with managed
filenames and with other configured paths using case-sensitive Python string
identity. Windows resolves those spellings case-insensitively, so a configured
application path such as a case variant of a managed filename can alias a file
that inspection/reset treats as lifecycle-owned. The same defect permits
current/previous configured path collisions that differ only in case. Key IDs
are identifiers rather than filesystem paths and must keep their existing exact
`SafeToken` comparison semantics.

Expected RED: on a simulated Windows boundary, lifecycle/reset admission and
current/previous collision tests accept case variants instead of returning the
redacted `application_secret_configuration_invalid` refusal.

### Documentation drift

The implementation/spec amendment permits a successful locator-aware
re-preflight to rebind only the policy revision of a matching `receiving` row
while preserving its token and every other field. Earlier plan/runbook wording
still says all `receiving` re-preflights reject without revision rebind and
describes every mid-upload policy change as exclusion. The exclusion-policy
lock-order runbook also omits the claimed-upload operation advisory fence that
must precede publication idempotency, policy-state, and source locks.

Expected correction: qualify the superseded plan claims, distinguish an
irrelevant locator-only allow/rebind/resume path from deny/indeterminate
exclusion, and document the claimed-upload lock prefix.

## RED evidence

- Recovery focused RED command:
  `uv run pytest tests/unit/recovery/test_manifest.py::test_manifest_contract_constant_is_pinned tests/unit/recovery/test_manifest.py::test_legacy_v2_twenty_table_manifest_remains_byte_canonical tests/unit/postgresql_source_store/test_backup_snapshot.py::test_snapshot_lock_order_covers_the_canonical_policy_and_operation_tables tests/unit/postgresql_source_store/test_backup_snapshot.py::test_share_lock_statements_follow_fixed_spec_order tests/unit/recovery/test_service_restore.py::test_legacy_v2_restore_verifies_the_manifest_twenty_table_shape -q`
  exited `1`: three intended failures reported the current count length `20`
  instead of `28`, the auth-free lock tuple, and 20 instead of 28 generated
  share-lock statements. The v1/v2 compatibility controls passed.
- Windows path focused RED command:
  `uv run pytest tests/unit/tools/test_local_service_stack.py::test_windows_secret_path_identity_rejects_case_variant_of_managed_file_before_lifecycle tests/unit/tools/test_local_service_stack.py::test_windows_secret_path_identity_rejects_current_previous_case_variant_collision -q`
  exited `1` with both tests reporting `DID NOT RAISE StackFailure`: lifecycle
  admission accepted a managed-file case alias, and current/previous configured
  paths differing only by case did not collide.
- The matching policy/current and policy/previous Windows case-variant controls
  also exited `1` with `DID NOT RAISE StackFailure`, confirming the defect was
  common filesystem identity rather than one parser branch.
- First tightened real journey through `.local/run-task6-obsidian.ps1` exited
  `1` after observing one fixture operation in `receiving`, zero canonical
  rows, and one durable pending journal event. The test had expected the
  timing-dependent `waiting_retry` substate, but the request was still
  durably `uploading`. This RED established the missing deterministic
  interruption seam; the journey was corrected to disable the plugin after
  the irrelevant policy publication, inspect the stopped durable state, then
  enable it for resume.

## GREEN evidence

- Recovery focused unit gate:
  `uv run pytest tests/unit/recovery tests/unit/postgresql_source_store/test_backup_snapshot.py -q`
  exited `0`: `124 passed, 4 skipped`.
- Disposable PostgreSQL 18.4 recovery proof with pinned 18.4 client tools:
  the auth writer-block, post-backup auth mutation, and exact auth-row restore
  cases exited `0`: `3 passed in 68.31s`. The snapshot exposed all eight auth
  counts, the credential writer blocked and left revision `1`, the post-cutoff
  throttle row stayed out of the restored graph, and every current-v2 table
  matched byte-for-byte after restore.
- Windows lifecycle gate:
  `uv run pytest tests/unit/tools/test_local_service_stack.py -q` exited `0`:
  `172 passed, 3 skipped`; the five path-identity cases passed. Ruff and
  `mypy --strict tools/local_service_stack.py` both exited `0`.
- Real Obsidian gate through the repository loader and existing HTTPS tunnel:
  `.local/run-task6-obsidian.ps1` exited `0`, `3` spec files passed. Sanitized
  interruption evidence was exactly one receiving/unpublished operation and
  zero sources, versions, or sync events. The opaque pre/post token comparison
  was true without emitting the token. Resume evidence was exactly one source,
  version, sync event, operation, committed operation, exact operation join,
  and terminal journal receipt, with zero remaining receiving operations.

## Final exact-HEAD gates

The exact implementation and canonical-documentation head was `7840798`
(`78407986b9e56eb30eaeee77492958f3d0e1eecf`). The handoff/report-only commit
follows it and changes no implementation or gate evidence.

- `uv run pytest tests/unit/recovery
  tests/unit/postgresql_source_store/test_backup_snapshot.py
  tests/unit/tools/test_local_service_stack.py -q`: `297 passed, 7 skipped`.
- The three mandatory auth-aware PostgreSQL 18.4 backup/restore cases passed in
  `68.31s`: concurrent auth writer blocking, post-backup auth mutation, and
  empty-target exact restore.
- `uv run pytest tests/unit/tools/test_local_service_stack.py -q`:
  `172 passed, 3 skipped`; Ruff and strict mypy also passed.
- Plugin test/lint/type/build: `26` files and `375` tests passed; ESLint,
  strict TypeScript, and the production build passed.
- `.local/run-task6-obsidian.ps1`: all `3` mandatory real WDIO specs passed in
  `39s`. At the claimed interruption, fixture-scoped counts were zero sources,
  versions, and sync events; one operation; zero committed/exact publications;
  one `receiving` row; and one pending durable journal event. After resume, the
  counts were exactly one source, version, sync event, operation,
  committed/exact publication, and terminal receipt, with zero `receiving`
  rows. The pre/post opaque-token equality check was true without logging it.
- `uv run poe canonical-core-test`: `1002 passed, 11 skipped`.
- `uv run poe exclusion-policy-test`, with the repository-pinned PostgreSQL
  18.4 clients and isolated project `knowledge-ci-exact-head-exclusion`:
  `1501 passed, 2 skipped, 1 deselected` in `17m44s`.
- `uv run poe verify`: `3041 passed, 21 skipped, 329 deselected`; API client
  `1`, plugin `375`, and Web `139` JavaScript tests passed, together with every
  format, lint, strict-type, boundary, artifact, and production-build gate.
- `uv run poe api-contract-check` and the targeted contract/migration
  selection passed (`32 passed`). Generated-client, migration, and canonical
  table diffs against `2035e3a` were empty.
- Shutdown verification found zero task listeners, the `knowledge-local` stack
  absent with volumes/secrets preserved, and the one pre-existing tunnel
  process still untouched.

Gate admission diagnostics were not product failures: the first exclusion
run omitted `LOCAL_STACK_TEST_PROJECT` (`103` setup errors); the next collided
with the running live-WDIO ports (`103` `port_unavailable` setup errors); and
the first post-shutdown run lacked PostgreSQL clients on `PATH` (`3` recovery
setup errors). The exact same suite passed after using an isolated project,
stopping the live stack, and prepending the repository-pinned clients. The
worker launch also exposed a Bash/Windows tool-path boundary; an extensionless
temporary copy of the existing Windows `uv.exe` allowed the mandatory
`.local/run-worker.sh` contract to run without changing the runbook, secrets,
DNS, or tunnel.

## Commits

- `efebae5` - include all canonical authentication state in recovery.
- `c7764c4` - compare Windows secret paths by filesystem identity.
- `70290f9` - prove the real claimed exact-token plugin resume.
- `57d3686` - reconcile canonical recovery, path, plan, and lock-order docs.
- `7840798` - apply the repository format gate to the changed Python files.
