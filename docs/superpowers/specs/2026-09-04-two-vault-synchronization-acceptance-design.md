# Two-Vault Synchronization Acceptance Design

**Date:** 2026-09-04
**Status:** Proposed; awaiting review
**Owning phase:** Phase 2, Child 9 — cross-slice acceptance and operations

## 1. Objective

Prove that one user can use two independent Obsidian Vault working copies,
each with its own plugin journal and device credential, against one workspace.
An operation performed through Vault A must converge at Vault B, and vice
versa, without a duplicate source, an echo upload, a silent overwrite, or an
incorrect source/locator/tombstone lineage.

The two Vaults are independent local directories. This design explicitly does
not support, test, or make safe a single Vault directory replicated by iCloud,
Obsidian Sync, Dropbox, or another filesystem synchronizer. The canonical API
and PostgreSQL/R2 state remain the only synchronization channel between A and
B.

## 2. Scope and non-goals

In scope:

- an automated two-plugin-actor acceptance harness backed by one disposable
  `knowledge-ci-*` stack, two separately authorized devices, two isolated
  plugin journals, and two isolated Vault adapters;
- correctness cases for create, update, rename, move, delete, explicit
  restore, offline replay, concurrent operations, reconciliation, restart,
  cursor acknowledgement, and conflict resolution;
- bounded burst and bulk correctness cases, with explicit supported limits;
- a guarded operator-run two-Vault Desktop journey and a physical
  Desktop/Mobile journey, both recording sanitized evidence only;
- a single, visible final-state oracle: canonical source/version/locator state,
  A state, B state, cursor state, and journal diagnostics.

Out of scope:

- changing source, lifecycle, conflict, cursor, manifest, or transport
  contracts unless this acceptance work exposes a defect;
- filesystem-level shared-Vault detection or support;
- push/long-poll delivery, per-device scope, high-volume import architecture,
  delta synchronization, or performance claims beyond the current contract;
- treating a fake HTTP producer as evidence that a second plugin/Vault path
  works.

## 3. Fixed decisions and invariants

1. A and B have distinct device IDs, credentials, journal databases, Vault
   roots, and test fixtures, but use the same user/workspace and policy
   revision.
2. Each actor performs local changes only through the same capture,
   lifecycle-capture, queue-driver, coordinator, remote-event-applier, and
   manifest-reconciler paths used by the plugin. Test-only transport scripting
   cannot substitute for the canonical server in the two-actor integration
   layer.
3. A successful convergence assertion requires all of the following:
   canonical state is correct; both Vault byte/locator/trash states are
   correct; both cursors have no outstanding applicable event; neither actor
   emitted an unintended successor upload; and no duplicate active source or
   locator exists.
4. Content conflicts are successful only when both candidates remain
   preserved, a visible conflict exists, and an explicit resolution produces
   one canonical winner. A test must never accept last-write-wins or a silent
   overwrite.
5. Rename and move preserve one source identity. Delete and restore preserve
   tombstone lineage. A remote delete may trash only a proven-unchanged local
   file; otherwise the local bytes become a visible conflict outcome.
6. Filesystem watchers are a latency optimization. Every test that drops,
   delays, or duplicates a watcher observation must prove eventual repair via
   the existing cursor/manifest mechanism rather than inventing another sync
   channel.
7. Diagnostics and recorded evidence contain only status/outcome, closed
   reason token, count, and timestamp. They never contain paths, content,
   digests, credentials, object keys, request IDs, or screenshots with note
   content.

## 4. Acceptance architecture

The test program has three non-interchangeable layers.

| Layer | Actors and dependencies | Purpose | Does not prove |
|---|---|---|---|
| Focused plugin tests | Fakes at a narrow port | Local state transitions, coalescing, retries, and fail-closed diagnostics | Server semantics or cross-Vault convergence |
| Two-plugin integration | Two isolated plugin actors plus real disposable API/PostgreSQL/R2 stack | End-to-end protocol and durable state across A/B | Real Obsidian UI/mobile lifecycle |
| Operator acceptance | Two real test Vaults on Desktop/Desktop or Desktop/Mobile against one disposable stack | Obsidian watcher, Vault API, foreground/suspension, and visible UX | Load capacity beyond the documented bound |

The two-plugin integration harness must expose an actor interface that is
operation-shaped, not HTTP-shaped:

```text
actor.create(bytes) / update(bytes)
actor.rename(target) / move(target)
actor.delete() / restore(target, bytes)
actor.goOffline() / goOnline() / restart()
actor.requestRepair() / advanceForegroundTime()
actor.readVaultState() / readJournalState() / readSyncStatus()
```

The harness owns deterministic time and delivery gates so it can place a
second operation between capture, canonical commit, pull, local apply, and
acknowledgement. It must use separate persistent journals so restart and
lost-journal cases cannot accidentally share state.

## 5. Required scenario matrix

Every row runs with A as origin and, where symmetric, is rerun with B as
origin. The final oracle in section 3 is mandatory for every green row.

### 5.1 Baseline propagation

| ID | User action | Required result |
|---|---|---|
| TV-01 | A creates a supported non-empty file | B receives one source with byte-identical content; no duplicate source or echo event |
| TV-02 | A updates a committed file, then B updates it after convergence | Each side receives the other committed version; one linear canonical lineage |
| TV-03 | A renames a file | B rebinding preserves source ID and removes no unrelated file |
| TV-04 | A moves a file across directories | B applies the new locator with the same source ID |
| TV-05 | A deletes a proven-unchanged file | B moves the exact file to local trash; canonical tombstone is shared |
| TV-06 | A explicitly restores the tombstone at an allowed target | B receives the restored bytes/locator and the original tombstone lineage closes once |
| TV-07 | A creates then deletes before B first pulls | B reaches the canonical tombstone outcome, never a phantom live file |

### 5.2 Ordering, offline, and duplicate delivery

| ID | Interleaving | Required result |
|---|---|---|
| TV-08 | A performs update → rename → update while B is offline | B observes the final locator and bytes in source order; intermediate delivery cannot create a second source |
| TV-09 | A performs rename → delete → explicit restore before B pulls | B reaches only the final valid state with correct tombstone predecessor/successor linkage |
| TV-10 | Delivery duplicates an event or acknowledgement is lost | Exact replay is idempotent; no second Vault apply, source version, or lifecycle event |
| TV-11 | A is offline for create/update/delete burst, reconnects, and restarts during retry | Pending journal work drains in order or surfaces a durable closed blocker; local work is retained |
| TV-12 | One watcher notification is delayed, duplicated, or absent | Reconciliation repairs to canonical state without trusting watcher order |
| TV-13 | A loses its journal after prior convergence | Reconciliation rebinds identity without duplicate source creation; B remains converged |

### 5.3 Concurrent user operations

| ID | Concurrent operation | Required result |
|---|---|---|
| TV-14 | A and B edit the same Markdown source from one base while offline | One visible stale-content conflict with both verified candidates; no silent overwrite |
| TV-15 | A and B edit a binary source from one base | Conflict offers whole-object choices only; neither candidate is lost |
| TV-16 | A edits while B deletes | `edit_remote_delete` conflict; local bytes are retained and no unexpected resurrection occurs |
| TV-17 | A deletes while B edits | `delete_remote_edit` conflict; no hard delete of changed local bytes |
| TV-18 | A renames/moves while B edits | Locator/content race resolves to the documented conflict or ordered canonical result, never a duplicate locator/source |
| TV-19 | A and B claim the same new locator with different files | One canonical claim and one visible locator conflict; no path overwrite |
| TV-20 | A resolves a conflict while B receives another remote advance | Resolution is stale/superseded when required; B receives the successor outcome and no second winner |

### 5.4 Crash, suspension, and recovery

| ID | Failure point | Required result |
|---|---|---|
| TV-21 | A crashes after canonical commit but before local receipt persistence | Restart replays by identity without duplicate canonical version |
| TV-22 | B crashes after remote Vault mutation but before acknowledgement | Restart verifies/apply-recovers and acknowledges without echo |
| TV-23 | B is suspended mid-pull, remote apply, manifest run, or retry backoff | Resume follows the documented bounded/retry state; no concurrent pass corrupts the journal |
| TV-24 | Cursor gap occurs while A commits new work | B blocks safely, repairs through a checkpoint-bound manifest, then receives post-checkpoint work |
| TV-25 | Policy advances during an A/B reconciliation | Both actors invalidate/restart the affected run and never publish denied content |

### 5.5 Bounded bulk and burst correctness

Bulk tests prove correctness within a published fixture bound; they do not
claim large-vault throughput. The initial gate uses 100 files of supported
small-file size and 20 repeated edits to one file. It must be parameterized so
performance work can raise the bound only after benchmark approval.

| ID | User action | Required result |
|---|---|---|
| TV-26 | A copies one file to 100 distinct locators | B has 100 distinct sources/locators with correct bytes; no coalescing across files |
| TV-27 | A bulk-edits 100 existing files | B converges every source and has no cross-file byte/locator swap |
| TV-28 | A bulk-deletes 100 unchanged files | B trashes each proven-unchanged target and no tombstone is omitted or duplicated |
| TV-29 | A moves/renames a folder containing 100 tracked files | Each source preserves identity and each B locator changes exactly once |
| TV-30 | A saves one file 20 times, including rename/edit/rename burst | B receives the final valid bytes and locator; intermediate event coalescing never crosses a lifecycle boundary |

Any test that exceeds the 10,000 pending-event capture limit, 100 MiB per-file
limit, or documented serial-drain capacity must assert the documented closed
status rather than expect successful bulk synchronization.

## 6. Execution and evidence contract

The automated two-plugin integration layer uses `CI=true` and exactly one
disposable project, started by:

```text
bash .local/serve-live-ci.sh up knowledge-ci-two-vault-<date>
```

It bootstraps both device credentials using the repository's approved HTTP
flow and runs database migrations only through the stack script. It never
reads or emits secret values. Teardown uses the paired stack command after the
test result has been collected.

The operator acceptance uses two dedicated empty test Vaults, never an
everyday Vault. The runbook must list safe fixtures and exact user actions for
TV-01 through TV-06, TV-14, TV-16 through TV-18, TV-21 through TV-24, and one
bounded bulk smoke. The operator records only the sanitized outcomes defined
in section 3. Desktop evidence cannot substitute for the physical Mobile
journey; a missing physical device remains the sole permitted mobile deferment
and is indexed once in `docs/handoff/BACKLOG.md`.

## 7. Failure and diagnostics requirements

Each scenario that deliberately closes an error path must assert both the
state outcome and one readable closed token on the existing sync status/trail
surface. Required families include `blocked_conflict`, `reconcile_required`,
cursor/manifest failure, retryable network failure, integrity failure, and
policy denial. A pass may not swallow an unexpected error merely because the
other actor eventually converges.

Tests assert privacy with a negative sentinel: exported trails, assertion
failures, fixture labels, and recorded operator evidence contain no raw
fixture bytes, Vault paths, full SHA-256 values, credentials, URLs, object
keys, or request IDs.

## 8. Acceptance criteria

This acceptance slice is complete only when:

1. TV-01 through TV-25 pass in the real two-plugin integration harness.
2. TV-26 through TV-30 pass at the documented initial bound and report no
   unsupported throughput claim.
3. Focused regression tests cover any defect exposed by the matrix before its
   integration case is marked fixed.
4. TypeScript strict, plugin lint/build, relevant Python integration/API
   contracts, and privacy sentinels pass on the same final revision.
5. The two-Vault Desktop operator evidence passes its guarded acceptance
   command. The corresponding physical Mobile evidence passes, or its one
   permitted device-only deferment is recorded in the handoff backlog.
6. The runbook documents automatic polling/cadence as eventual delivery, not
   realtime delivery, and names the status/reason-token recovery procedure.

## 9. References

- `docs/00-PRODUCT_VISION_AND_PRD.md`
- `docs/01-CANONICAL_ARCHITECTURE.md`
- `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- `docs/15-OBSERVABILITY_AND_ALERTING.md`
- `docs/16-TESTING_AND_EVALUATION.md`
- `docs/19-ARCHITECTURE_DECISIONS.md`
- `docs/20-IMPLEMENTATION_PLAN.md`
- `docs/superpowers/specs/2026-08-18-plugin-journal-and-small-file-sync-design.md`
- `docs/superpowers/specs/2026-08-20-source-locator-and-tombstone-lifecycle-design.md`
- `docs/superpowers/specs/2026-08-26-device-cursor-and-manifest-reconciliation-design.md`
- `docs/superpowers/specs/2026-09-02-source-conflict-capture-and-resolution-design.md`
