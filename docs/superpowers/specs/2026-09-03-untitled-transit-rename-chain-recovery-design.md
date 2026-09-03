# Untitled-transit rename-chain recovery design

**Date:** 2026-09-03

**Status:** Binding implementation contract for Task 3

**Domain:** `apps/obsidian-plugin` journal capture and lifecycle

**Source plan:**
`docs/superpowers/plans/2026-09-03-untitled-transit-rename-chain-recovery-plan.md`

## 1. Purpose and boundary

This change preserves one tracked `local_files` identity while a newly created
note is renamed more than once before its first content receipt is durable.
The Task 1 RED journey proves the loss schedule:

1. create event E1 begins at `A`;
2. the server commits E1, but the client has not stored the receipt;
3. Vault observations move `A -> B -> C`;
4. the first lifecycle settle defers on E1, while the second observation can
   no longer find a row at `B`;
5. the content runner reads the stale row path `A`, closes E1 as
   `deferred_lifecycle`, the uncommitted-transit heal deletes R1, and admission
   creates R2 at `C`;
6. R2 conflicts with the canonical source created by E1.

The fix is plugin-local. It adds no server, wire, OpenAPI, policy, or canonical
database contract. PostgreSQL remains canonical; plugin SQLite records only a
durable observation intent that is safe to discard during a proven journal
rebuild followed by reconciliation.

Inherited behavior remains binding: `restore_pending` owns its reservation;
the delete-deferral ladder is bounded and unchanged; a pure create with no
rename intent retains the existing uncommitted-transit heal; rename-tail
admission remains on `#admissionTail`; and echo suppression remains exact.

## 2. Binding decisions

1. There is at most one pending rename intent per `local_file_id`. Its state is
   `(prior_path, current_path)`. `prior_path` is the next canonical locator
   from which a rename can be issued; `current_path` is the latest observed
   Vault target.
2. A rename/move observation first resolves its owner by the local row at the
   observed prior path, by an existing intent whose `current_path` equals that
   prior path, or by an already owner-bound predecessor observation whose
   target equals that prior path. The last case is the only permitted
   prior-miss creation proof: the lane carries the predecessor's resolved
   `local_file_id`, then verifies that the row still owns its earlier path. A
   bare prior miss is not proof. The capture lane carries the resolved owner
   through settle; it never tries to rediscover ownership from a filesystem or
   unrelated-row scan.
3. When an owned observation encounters an in-flight identity-establishing
   create, capture durably creates the intent before returning
   `SETTLE_DEFERRED` or arming the delay. If settle discovers the in-flight
   create only after the delay, it creates the intent in that settle. An
   owner-bound prior miss creates the record directly from the row's still
   owned earlier path to the latest observed target. A later observation with
   `observed_prior_path == intent.current_path` updates only `current_path`;
   it does not create another intent or change the owner.
4. An exact duplicate observation is a no-op. A composition that returns to
   `prior_path` cancels the chain by reparenting the row to that path and
   clearing the intent. An incompatible observation never overwrites the
   record: the row becomes `reconcile_required` and the closed diagnostic
   `pending_rename_intent_conflict` is surfaced.
5. Every delayed re-arm, byte read for the owning content event, lifecycle
   operation derivation, exact echo probe, and final rename/move materialization
   re-reads the durable intent. A timer's captured endpoints are scheduling
   hints only and must never become lifecycle operands.
6. A rename or move is derived from the intent's endpoints at materialization
   time: equal parent means `rename`, changed parent means `move`. The
   lifecycle event and its operands are inserted, content is frozen, and the
   row is rebound to `current_path` in one SQLite transaction. The intent is
   preserved until the corresponding canonical lifecycle prefix is committed
   or otherwise disposed by an exit in section 3.
7. If a second observation composes after a lifecycle prefix has already been
   materialized, that event remains immutable. On its receipt the same intent
   is rebased from the committed target to the latest `current_path`, and a
   successor lifecycle event is armed. Idempotency identity or frozen
   lifecycle operands are never mutated after dispatch may have begun.
8. A rename-tail admission carries the resolved owner. While that owner has an
   intent, delayed admissions for its now-intermediate paths are suppressed.
   Generic settle and snapshot admission also suppress any path equal to an
   intent's `current_path`. Thus no R2 is minted at an intent-owned path.
9. Endpoint re-derivation from the filesystem, fingerprints, event timing, or
   a scan of unrelated rows is rejected. It cannot prove that the prior-miss
   observation belongs to R1, which is precisely the missing link in the RED
   schedule; it could also bind a different note after path reuse.

## 3. Lifecycle-row exit inventory

The following actions are normative:

- **PRESERVE**: keep both the owner row and its intent unchanged.
- **REPARENT + CLEAR**: atomically set the owner row's `normalized_path` to
  `intent.current_path`, then delete the intent.
- **REBASE + PRESERVE**: atomically advance `prior_path` to a committed
  lifecycle target while retaining the later `current_path`.
- **DELETE WITH OWNER**: delete the intent in the same transaction as its
  `local_files` owner (the foreign key is also a defensive cascade).

Every production path that can delete, park, reset, rebuild, or finish a
lifecycle row is classified below before any repository method is introduced.

| Row exit | Required intent action and rationale | Required Task 3 test |
| --- | --- | --- |
| Uncommitted-transit heal, no intent | Existing pure-create behavior: delete R1. There is no observed rename ownership to retain. | Terminal identity-less create without an intent is still pruned. |
| Uncommitted-transit heal, intent present | **REPARENT + CLEAR**, never delete R1. The latest Vault path remains owned by R1; admission may add a successor content event on that row. | Terminal identity-less create followed by heal keeps the same `local_file_id`, moves it to the final path, and leaves no intent. |
| Content `local_file_missing -> deferred_lifecycle` | With an intent this terminal exit is forbidden: **PRESERVE** and park the same event for exact replay. Without an intent, preserve the current terminal behavior. | Single-part and multipart missing-file outcomes take the two different branches atomically. |
| Content rows frozen as `deferred_lifecycle` by a materialized rename/move | **PRESERVE**. These rows are the existing lifecycle guard and are removed only with the lifecycle receipt or an explicit reconcile/disposal path. | Freeze leaves the intent; lifecycle receipt removes the frozen rows and resolves the intent atomically. |
| Content `blocked_conflict` parking before identity | **REPARENT + CLEAR** and retain the same row plus conflict/repair evidence. A terminal conflict cannot drive the intent, but deleting the row would recreate the RED R2 split. | Conflict parks R1 at the latest target, starts the existing repair barrier, and creates neither R2 nor an orphan intent. |
| Rename/move `blocked_conflict` parking | **REPARENT + CLEAR** and mark/retain reconciliation ownership. The rejected canonical move cannot be treated as a committed prefix and a terminal event cannot resume the intent. | Lifecycle conflict leaves one row at the latest Vault path, one repair obligation, and no intent. |
| Row-specific `reconcile_required` transition | **REPARENT + CLEAR** in the same transaction. Manifest reconciliation now owns locator truth; a stale intent reservation must not wedge planning or admission. | Both direct reconcile marking and failure recovery reparent once and clear once. |
| In-place repair/manifest progress reset | **PRESERVE** while the owning row remains valid. Resetting progress is not proof that a local observation vanished. Before repair completion, any intent invalidated by manifest evidence must pass through the row-specific rule above. | Reset/reopen preserves a valid intent and resume can still converge it. |
| Confirmed SQLite journal rebuild | The rebuilt image contains neither local rows nor intents and starts reconcile-first, so intents are **deleted with all owners**. No intent may be copied without its proof-bearing row. | Rebuild has an empty intent table and `reconcile_required`; no local create dispatch occurs before reconciliation. |
| `removeLocalMapping(local_file_id)` | **DELETE WITH OWNER** before/with attempts, multipart state, operands, events, and the row. Repeated removal is a no-op. | Removal and repeated removal leave no owner, endpoint index, or intent. |
| Explicit-restore phantom-occupant cleanup | A phantom owning an intent is not disposable: return existing `restore_target_busy` and **PRESERVE** it. A phantom without an intent keeps the existing queued/waiting cleanup. Any eventual explicit row deletion uses **DELETE WITH OWNER**. | Restore cleanup deletes an ordinary phantom, refuses an intent owner, and never steals an endpoint. |
| Content committed/no-change receipt that establishes identity | **PRESERVE**. The receipt supplies `source_id`/`base_version_id`; the re-arm then materializes the rename from current intent endpoints. | Receipt and exact replay retain the intent, then schedule exactly one lifecycle event with stable row/source identity. |
| Rename/move committed receipt, event target equals `intent.current_path` | **CLEAR** in the same receipt transaction after activating the row and releasing frozen content rows. | Initial receipt and exact receipt replay leave the row at the final path and no intent. |
| Rename/move committed receipt, intent has a later target | **REBASE + PRESERVE** from the committed event target to the later target, then arm exactly one successor. | A chain extended after prefix materialization produces ordered prefixes with one intent and no duplicate event. |
| Exact echo consumed for the composed endpoints | **REPARENT + CLEAR** with the echo-marker consumption. The remote event already proves canonical locator state; no outbound lifecycle row is created. A marker for only an earlier prefix must not consume a later target. | Exact final echo clears; prefix-only or fingerprint-mismatched markers do not suppress the composed rename. |
| Delete observation while rename intent is pending | **PRESERVE** through the existing bounded delete-deferral ladder. Resolve the rename prefix first, then record delete against the same row; a terminal delete/tombstone transition clears any remaining intent atomically. | Existing delete retry bounds remain unchanged and rename-then-delete preserves row/source order. |
| `restore_pending` owner or target reservation | A `restore_pending` row cannot create or compose an intent. A restore target matching another row's intent endpoint is `restore_target_busy`; that other intent is **PRESERVED**. A corrupt coexistence fails closed into row-specific reconciliation. | Rename/delete guards remain quiet; restore reservation refuses both intent-owned endpoints without leaking a path. |

These rules are exhaustive for current `local_files` deletion sites:
`removeLocalMapping`, explicit-restore phantom cleanup, and whole-journal
rebuild. New deletion sites must choose one of the four actions above and add
an exit test before landing.

## 4. Durable schema and migration

Journal schema advances from v9 to v10. `LIFECYCLE_SCHEMA_VERSION` continues
to mirror `JOURNAL_SCHEMA_VERSION`; both SQLite bookkeeping values
(`journal_meta.schema_version` and `pragma user_version`) must equal 10.

```sql
create table pending_rename_intents (
    local_file_id text primary key
        references local_files(local_file_id) on delete cascade,
    prior_path text not null,
    current_path text not null,
    check (length(prior_path) > 0),
    check (length(current_path) > 0),
    check (prior_path <> current_path)
);

create unique index pending_rename_intents_current_path_uq
    on pending_rename_intents(current_path);
```

The primary key enforces one intent per row. The current-path index makes
prior-miss composition and fresh-admission reservation unambiguous. There is
deliberately no unique index on `prior_path`: a legal multi-file path swap can
temporarily use a path vacated by another row, and ownership is resolved by
`local_file_id`, not by guessing from an old endpoint. Repository mutation
still rejects a target occupied by another live `local_files` row.

Only normalized Vault-relative paths are stored. No source/version identity,
bytes, fingerprint, timestamps, error text, or provider data belong in this
table.

The v9 -> v10 migration runs in one `begin immediate` transaction, validates
the complete v9 table surface, creates the empty table/index, and changes both
version stamps last. All v9 rows survive byte-for-byte. Persistence accepts v9
as the newest predecessor and composes the existing v6 -> v7 -> v8 -> v9 chain
before this migration. Fresh DDL and the required-table validator include the
new table.

Runtime loading remains forward-only. A test-only v10 -> v9 downgrade contract
drops the index/table and restamps both versions in one transaction only when
the intent table is empty. A non-empty table refuses with
`journal_mutation_failed` before mutation because v9 cannot represent the
pending observation safely. Upgrade/downgrade tests cover empty round-trip,
non-empty refusal, malformed predecessor, torn migration, and foreign/future
versions.

## 5. Repository surface and transaction ownership

`LifecycleRepository`, exposed through `JournalRepository.lifecycle`, owns
intent reads and lifecycle mutations. `JournalRepository` owns the content
runner's event-state decision. The binding surface is:

```ts
interface PendingRenameIntent {
  readonly localFileId: string;
  readonly priorPath: string;
  readonly currentPath: string;
}

type PendingRenameIntentMutation =
  | "created"
  | "composed"
  | "unchanged"
  | "cancelled";

LifecycleRepository.readPendingRenameIntentForLocalFile(localFileId)
LifecycleRepository.readPendingRenameIntentByCurrentPath(normalizedPath)
LifecycleRepository.readPendingRenameIntentOwningEndpoint(normalizedPath)
LifecycleRepository.readPendingRenameIntents()
LifecycleRepository.recordOrComposePendingRenameIntent(input)
LifecycleRepository.recordPendingRenameLifecycleEvent(localFileId, fingerprint)
LifecycleRepository.reparentAndClearPendingRenameIntent(localFileId)

JournalRepository.resolveIntentAwareLocalFileMissing(
  eventId,
  nextEligibleRetryEpochMs,
) // -> "waiting_for_rename" | "closed_deferred_lifecycle"
```

Names may change only if semantics remain one-for-one and tests name the same
contract. There is no public arbitrary endpoint-update or clear-only escape
hatch.

`recordOrComposePendingRenameIntent` validates normalized non-empty paths,
owner existence, non-`restore_pending` state, target availability, and the
exact chain link. Create/compose/cancel is one serialized mutation. Collision
or incompatible chain handling marks the same row `reconcile_required` and
clears/reparents under section 3 in that mutation; if that fails, no partial
intent change commits.

`recordPendingRenameLifecycleEvent` re-reads the intent and identity in its
transaction. It atomically freezes pending content, inserts/replays one
rename/move event plus operands from the current endpoints, and rebinds the
row. A unique/idempotent event lookup prevents a restart from materializing a
second event for the same unresolved prefix. The intent remains present.

Receipt handling, echo consumption, row-specific reconciliation,
`removeLocalMapping`, and restore phantom cleanup absorb their section 3
intent action into their existing serialized transaction. A read-then-write
pair across transactions is not acceptable for any exit.

## 6. Capture, queue, admission, and restart behavior

### 6.1 Capture and composition

At watcher ingress, lifecycle capture resolves the owner before the settle
delay. An in-flight create causes immediate durable intent creation, ensuring
that a rapid `B -> C` callback can resolve R1 through `current_path == B`. If
the second callback wins before that commit, the owner-bound `A -> B` capture
entry is the explicit predecessor proof: the same serialized lane creates
`A -> C` directly after revalidating R1 at `A`. Every timer callback then
re-reads by `local_file_id`; disposal cancels only the timer, never the durable
intent.

The re-arm selector enumerates intents whose owner has either an in-flight
content event, newly durable identity, a materialized lifecycle prefix, or a
terminal state requiring an exit rule. It coalesces to at most one timer per
`local_file_id` and uses the latest stored endpoints when the timer fires.

### 6.2 Intent-aware content dispatch

The RED schedule requires an exception to
`local_file_missing -> deferred_lifecycle`.

- Preflight continues to use the owner's row path (`prior_path` before the
  first identity receipt), preserving E1's original idempotency request.
- Single-part and multipart byte reads use `intent.current_path` when an
  intent exists. The event fingerprint is still the frozen authority; a byte
  mismatch takes the existing integrity path.
- If the current endpoint is also missing, the atomic
  `resolveIntentAwareLocalFileMissing` transition leaves E1 non-terminal in
  `waiting_retry` with safe label `deferred_lifecycle`, retains its operation
  identity/multipart progress, and schedules a bounded-delay pass without
  consuming the ordinary network retry budget. This gives exact preflight
  replay a future chance to return the already committed receipt.
- Without an intent the same method performs today's terminal
  `deferred_lifecycle` close. The decision and transition share one
  transaction, so intent creation cannot race a stale terminal close.

Terminalizing the intent-owned E1 is unsound: after the server-side commit in
the RED schedule, only exact replay can recover `source_id` and
`base_version_id`; no local endpoint derivation can reconstruct that receipt.

### 6.3 Admission and restart

`#admitNormalizedPath` keeps its position on the existing `#admissionTail`.
It adds the durable intent checks before fresh-row creation. A rename-tail
work item is suppressed while its owner has any intent; a generic settle or
snapshot path is suppressed when it matches `current_path`. Suppression is a
normal lifecycle deferral, not an error and not a new row.

After a verified journal image is opened and migrations finish, the
composition root enumerates pending intents and arms the selector before the
first automatic snapshot admission or outbound drain. No network request is
required to restore the timers. A failed enumeration/resume fails closed:
normal fresh admission does not run until the read succeeds or reconciliation
owns the affected row.

## 7. Crash safety and idempotency

- Intent create/compose/cancel is acknowledged only after the normal journal
  generation commit. A failed or torn mutation exposes the prior valid
  generation; the observation remains retryable and no half-row exists.
- Crash after intent commit but before timer fire: restart enumeration resumes
  it and admission remains suppressed.
- Crash after lifecycle event materialization but before dispatch: event and
  intent coexist; restart finds the existing prefix and dispatches it once.
- Crash after server commit but before content receipt: exact E1 replay stores
  the same identity; the intent survives and materializes the rename.
- Crash after server commit but before lifecycle receipt: exact lifecycle
  replay runs the same clear-or-rebase receipt transaction.
- Repeating create, composition, re-arm, materialization, receipt, reparent,
  clear, and removal is either an exact no-op or returns the same durable
  event. It never creates a second owner or lifecycle event.
- Foreign-key cascade is defensive only. Production delete methods still
  perform their explicit intent cleanup so tests can prove the chosen exit.

## 8. Diagnostics and privacy

Add these closed tokens to the v2 `journal_failure` vocabulary and the
composition-read allowlist where applicable:

- `pending_rename_intent_read_failed`: intent lookup/enumeration could not
  complete; admission, dispatch, or startup resume fails closed;
- `pending_rename_intent_persist_failed`: watcher/re-arm mutation failed and
  the old generation remains authoritative;
- `pending_rename_intent_conflict`: an incompatible chain, target collision,
  or corrupt guarded-state coexistence was sent to reconciliation.

Each swallowed boundary appends exactly one readable trail entry. If marking
reconciliation also fails, the existing `lifecycle_reconcile_persist_failed`
token is additionally emitted. No path, locator, note name, row/source/version
id, fingerprint, content, exception text, or timer value may appear in trail,
Notice, logs, metrics, tests, or handoff evidence. Ordinary admission
suppression and successful re-arm emit no failure entry.

## 9. Acceptance criteria

1. The Task 1 watcher journey converges `A -> B -> C` with one
   `local_file_id`, one canonical source identity, and no R2 conflict; it
   asserts the RED ordering through the fixed branch.
2. One intent composes every linked observation, persists across restart, and
   supplies the latest endpoints to byte read, re-arm, echo, and lifecycle
   materialization.
3. Intermediate and current-path admissions never create a second row while
   the intent owns the chain; admission-tail ordering is unchanged.
4. Every exit in section 3 performs its clear/preserve/reparent action in the
   owner transaction. No deleted row leaves an intent, and no terminal path
   leaves a reservation that wedges a later rename.
5. Content receipt preserves the intent; final lifecycle receipt clears it;
   prefix receipt rebases it and produces exactly one successor.
6. Intent-aware missing-file handling preserves exact replay and never alters
   the pure-create no-intent branch.
7. `restore_pending`, explicit restore reservation, delete deferral,
   reconciliation, exact echo, conflict barrier, and journal rebuild guards
   retain their documented behavior.
8. v9 images upgrade losslessly to v10; fresh/reopened v10 images validate;
   the guarded empty downgrade round-trip succeeds and non-empty downgrade
   refuses without mutation.
9. Every new closed failure reason is visible through the sanitized durable
   diagnostics trail, and all plugin unit tests, type check, lint, and build
   pass.

## 10. Task 3 test matrix

Task 3 must add or extend tests at these layers:

| Layer | Mandatory cases |
| --- | --- |
| `sqlite-database.test.ts`, lifecycle schema tests, persistence tests | Fresh v10 DDL; v9 -> v10 preservation; current/prior generation reopen with an intent; empty v10 -> v9 -> v10 round-trip; non-empty downgrade refusal; malformed/torn/foreign image rejection; rebuild yields empty intents plus reconcile-first. |
| `lifecycle-repository.test.ts` | Create, exact duplicate, `A -> B -> C` composition, return-to-prior cancellation, current-path uniqueness/collision reconciliation, guarded restore owner, atomic lifecycle materialization from latest endpoints, exact replay, final clear, prefix rebase, row reconcile, phantom restore refusal, and all delete/cascade exits. |
| `repository.test.ts`, `queue-driver.test.ts`, multipart tests | Intent-owned bytes resolve at the current path; single/multipart missing current bytes park E1 non-terminal; no-intent missing bytes still close; content commit/no-change/exact replay preserve intent; conflict parks/reparents; transaction failure cannot race intent creation or swallow its token. |
| `lifecycle-capture.test.ts` | Rapid prior miss resolves through the current endpoint; only one owner/timer exists; re-arm reads `C`, not frozen `B`; restart enumeration; late composition after prefix materialization; rename versus move derived from final endpoints; return-to-prior; exact final echo and non-matching prefix echo; delete ladder and `restore_pending` guards. |
| `capture.test.ts`, automatic convergence tests | Rename-tail intermediate admission and final admission are suppressed on the existing tail; snapshot cannot mint R2; after reparent/clear the same row can admit a successor; pure-create transit heal remains unchanged; read failure fails closed with diagnostics. |
| `journal-sync-journey.test.ts` | Convert the Task 1 RED to green without deleting its schedule assertions: E1 server commit precedes client receipt, `A -> B -> C` crosses settle, R1 survives, exact receipt establishes identity, one final lifecycle operation commits, and canonical/local state converge without `blocked_conflict`. Add restart between composition and receipt. |
| diagnostics tests/export tests | Closed token membership, one-entry emission at each swallowed read/persist/conflict boundary, stable export/stop-reason derivation, and explicit proof that paths and identities are absent. |

Task 3 verification is, at minimum:

```text
pnpm --filter @workspace/obsidian-plugin test
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
pnpm --filter @workspace/obsidian-plugin build
```

No canonical document changes are required unless implementation discovers a
server/API contract change; that discovery stops Task 3 for a new design
decision rather than silently expanding this contract.
