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
4. An exact duplicate observation is a no-op. Return-to-prior handling is
   phase-aware: without a materialized lifecycle prefix it cancels the chain;
   with a materialized prefix it records a compensating target and survives
   until that prefix's receipt. Section 2.1 defines the representation. An
   incompatible observation never overwrites the record: the row becomes
   `reconcile_required` and the closed diagnostic
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

### 2.1 Phase-aware return-to-prior representation

No extra phase column is needed. The intent row plus the row's existing open
rename/move event is the durable state representation:

- **Unmaterialized:** no non-terminal rename/move event exists for the owner.
  For intent `A -> B`, an observation `B -> A` is a real cancellation. The
  repository reparents the row to `A` and clears the intent in one mutation;
  `(A, A)` is never committed in this phase.
- **Prefix materialized:** exactly one non-terminal rename/move event exists
  for the owner and its immutable operands describe a prefix, for example
  `A -> B`. If `B -> A` arrives before receipt, the intent becomes `(A, A)`.
  This is a valid durable **compensation pending** state, not cancellation:
  locally the user is back at `A`, but canonical state may still commit at
  `B`. The open prefix is what disambiguates the equal endpoints.
- **Prefix receipt:** when exact or initial receipt for `A -> B` lands, the
  same transaction rebases `(A, A)` to `(B, A)` and arms one compensating
  `B -> A` event. If later observations instead leave `current_path == B`,
  receipt clears the intent because the committed prefix is already final.

Equal endpoints are valid only in the compensation-pending phase. On restart,
the selector joins intents to open lifecycle operands before classification;
an equal-endpoint row with zero or multiple open prefixes is corrupt and takes
the row-specific reconcile action. A terminally rejected prefix also takes its
section 3 park/reconcile exit; it never converts compensation into a false
success. Neither restart nor exact receipt replay may clear `(A, A)` before
the immutable `A -> B` prefix is durably settled.

## 3. Lifecycle-row exit inventory

The following actions are normative:

- **PRESERVE**: keep both the owner row and its intent unchanged.
- **CLEAR**: atomically delete the intent while leaving an already correctly
  placed owner row unchanged. This is legal only when a durable receipt or
  exact echo proves that the row's path is the final endpoint.
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
| Content `local_file_missing -> deferred_lifecycle` | With an intent this terminal exit is deferred under the bounded rule in section 6.2: **PRESERVE** through attempt 40, then **REPARENT + CLEAR**, close E1, and transfer the row to reconciliation on attempt 41. A counter event-ID mismatch instead takes the same reparent/clear/reconcile exit before any cutoff evaluation. Without an intent, preserve the current terminal behavior. | Single-part and multipart cover no-intent close, retry below the limit, restart-persistent count, recovery before the limit, mismatch returns only the conflict reason, and deterministic exhaustion-only reconcile takeover at the limit. |
| Content rows frozen as `deferred_lifecycle` by a materialized rename/move | **PRESERVE** the intent, but delete any bound missing-file deferral state in the same materialization transaction. The frozen row is the existing lifecycle guard; a retry budget is valid only for an actively retrying content event and must not survive its freeze. Frozen rows are removed only with the lifecycle receipt or an explicit reconcile/disposal path. | Seed a valid bound counter, materialize the lifecycle event, and prove the intent remains while the frozen event has no counter; rollback proves neither freeze nor counter deletion commits, and reopen sees either the intact retry state or no counter, never stale state on a frozen event. |
| Any terminal content park while stable source identity or another identity-establishing content event exists | **PRESERVE**. The row can still materialize the rename now or after the other event's receipt. One failed content generation must not discard the chain. | Every closed class covers an identityful update and an identity-less row with a pending successor. |
| Last terminal content park before identity | **REPARENT + CLEAR** and retain the same row plus its terminal audit. There is no remaining event that can provide identity, so keeping the intent would wedge admission; deleting the row would recreate the R2 split. `blocked_conflict` additionally retains/starts its repair barrier. | Every closed class covers an identity-less last event and proves same row, final path, no intent, no R2; conflict also proves the barrier. |
| Rename/move direct terminal `blocked_conflict` or `integrity_failed` | `LifecycleRepository.resolveIntentAwareLifecycleTerminal` owns the terminalization. With an intent it atomically writes the attempt audit, terminalizes the immutable prefix, clears any bound missing-file deferral state, **REPARENTS + CLEARS** the intent, and marks both the row and journal `reconcile_required`. Neither a rejected canonical move nor an integrity rejection proves the prefix committed, so it must not rebase, clear as success, or retain a resumable prefix/compensation reservation. Without an intent the present terminal behavior remains. | Driver and repository tests cover both verdicts with a direct lifecycle prefix, latest composed target, no retained intent/deferral state, one repair obligation, and one sanitized rejection diagnostic; a rollback-injection test proves no partial terminalization or cleanup. |
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
rebuild. New deletion sites must choose one of the actions above and add
an exit test before landing.

### 3.1 Exhaustive terminal content classes

`JournalRepository.resolveIntentAwareContentTerminal` owns the attempt audit,
event close, and preserve-or-reparent decision in one serialized mutation. If
the event has a bound missing-file deferral row, this generic terminal mutation
deletes that row with the event close, before deciding whether the intent is
preserved or cleared. It then checks the owner's durable identity and remaining
pending content events. The two generic rows above apply to every terminal
class; no queue caller performs a separate intent read or attempt write. A
rollback leaves both the event and its still-valid counter untouched; a
committed close leaves neither a terminal event nor a later restart with a
counter that could be mistaken for an active retry.

Born-terminal `blocked_size`/`excluded_policy` capture remains owned by
`JournalRepository.recordCapture`. Fresh admission cannot have an intent, and
intent-owned admission is suppressed before `recordCapture`; a repository
test and a capture test must assert both halves of that impossibility. Every
terminal transition after preflight is instead owned by the generic resolver.

| Closed class / origin | Intent action | Mandatory transport tests |
| --- | --- | --- |
| `excluded_policy`: preflight `excluded` or multipart `policy_denied` mapping | Generic identity/pending test: **PRESERVE** when identity or a successor exists, otherwise **REPARENT + CLEAR**. Policy state and terminal audit remain unchanged. | Preflight exclusion at both single-part and multipart sizes; multipart mid-transfer policy denial; final-row and successor variants. |
| `blocked_size`: born-terminal capture, single-part reopened bytes above the lane limit, or server/wire `blocked_size` | An already owned intent uses the generic rule. A fresh born-terminal event cannot own an intent because admission is suppressed while an intent exists; this invariant is asserted rather than inferred. | Born-terminal capture invariant plus single-part reopen and multipart/server mapping; final-row and successor variants where reachable. |
| `blocked_conflict`: preflight conflict, captured-candidate park, or terminal transport mapping | Generic rule; the identity-less last event **REPARENT + CLEAR** and the existing repair barrier remains mandatory. | Single-part and multipart preflight/candidate paths, identityful update, identity-less final event, successor, and barrier evidence. |
| `integrity_failed` from single-part fingerprint/size mismatch | Generic rule. A newer pending content event owns the newer fingerprint and therefore preserves the intent; without one the row reparents and releases the chain. | Single-part mismatch with and without successor; direct server integrity mapping. |
| `integrity_failed` / `multipart_local_content_changed` from multipart local change | Same generic rule; multipart progress is cleaned by the existing terminal contract in the same event-close transaction. | Multipart changed bytes with and without successor, including progress cleanup and restart. |
| `integrity_failed` from terminal multipart/server integrity mapping | Same generic rule; no provider/session detail changes the local ownership decision. | Multipart integrity response with identity, successor, and identity-less last-event shapes. |
| Ordinary `deferred_lifecycle` without an intent | Existing close; no intent action exists. | Single-part and multipart missing file with no intent. |
| Intent-owned `deferred_lifecycle` freeze marker | **PRESERVE** until lifecycle receipt; this is not the queue's missing-file close. | Single-part and multipart owner events frozen by materialization, then receipt cleanup. |

Retryable transport mappings (`network_offline`, `network_timeout`,
`network_rate_limited`, `server_error`, `operation_retry_required`) and
credential parks (`login_required`) remain non-terminal `waiting_retry` and
**PRESERVE** the intent. They are not row exits, but single-part and multipart
tests must prove they cannot invoke terminal cleanup. This classification is
the exhaustive proof for all five members of
`JOURNAL_NON_RETRY_EVENT_STATES`: `excluded_policy`, `blocked_size`,
`blocked_conflict`, `deferred_lifecycle`, and `integrity_failed`.

The generic owner is proven by one table-driven repository matrix over all
five terminal states times three owner shapes: stable identity, pending
identity successor, and identity-less last event. Queue integration then
covers every listed single-part and multipart origin. Where a closed origin is
not semantically emitted by one transport (for example multipart-only
`multipart_local_content_changed`), the test asserts that adapter exclusion
and still runs that state through the generic repository matrix; it must not
silently omit the class.

### 3.2 Direct lifecycle-prefix rejection

The content resolver in section 3.1 does not own lifecycle-driver terminal
verdicts. In particular, the current `LifecycleDriverImpl` maps API
`integrity` / `integrity_5xx` directly to `integrity_failed`; that route is a
separate terminalization seam and must not call the content resolver or the
old `recordEventAttempt` then `markEventTerminal` pair.

`LifecycleRepository.resolveIntentAwareLifecycleTerminal` is the only allowed
terminal path for a non-terminal `rename` or `move` event when the driver
receives `blocked_conflict` or `integrity_failed`. In one serialized SQLite
transaction it validates the lifecycle event and operands, re-reads its owner
and any intent, appends the closed attempt audit, sets the terminal state and
safe error, and deletes multipart progress exactly as the ordinary terminal
contract does. If no intent exists, this is the existing terminal transition.
If an intent exists, the same transaction additionally verifies that the
prefix and intent have the same owner, reparents the local row to the intent's
latest `current_path`, explicitly deletes its missing-file deferral state and
intent, changes the row to `reconcile_required`, and sets
`journal_meta.is_reconcile_required = 1`. It returns `intent_reconciled` only
after that full commit; the lifecycle driver uses that result to start or retain
the existing repair barrier.

This is also the rule for a compensation-pending `(A, A)` intent whose immutable
`A -> B` prefix is rejected: reparent to `A`, clear the intent, and let
reconciliation establish canonical truth. It never converts equal endpoints
into a success, reuses the rejected prefix, or arms a compensating successor.
An owner/operand mismatch or a failure to persist the required reconciliation
rolls back the whole mutation; the event and intent remain as they were, the
driver fails closed, and it emits the existing
`lifecycle_reconcile_persist_failed` token. No partial close, reparent, or
intent deletion is legal.

After a successful intent-owned terminalization, the driver appends exactly one
`journal_failure` trail entry with
`pending_rename_intent_lifecycle_rejected`. This token reports that a rejected
lifecycle prefix transferred locator ownership to reconciliation; the durable
event's existing closed `blocked_conflict` or `integrity_failed` safe error
remains the verdict detail. There is no token for a no-intent terminalization,
and a rolled-back mutation must not append the success-path rejection token.
The entry carries no path, endpoint, owner, event, source, fingerprint, or
server detail.

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
    check (length(current_path) > 0)
);

create unique index pending_rename_intents_current_path_uq
    on pending_rename_intents(current_path);

create table pending_rename_intent_missing_file_deferrals (
    local_file_id text primary key
        references pending_rename_intents(local_file_id) on delete cascade,
    event_id text not null unique
        references journal_events(event_id),
    deferred_attempt_count integer not null
        check (deferred_attempt_count between 1 and 40)
);
```

The primary key enforces one intent per row. The current-path index makes
prior-miss composition and fresh-admission reservation unambiguous. There is
deliberately no unique index on `prior_path`: a legal multi-file path swap can
temporarily use a path vacated by another row, and ownership is resolved by
`local_file_id`, not by guessing from an old endpoint. Repository mutation
still rejects a target occupied by another live `local_files` row.

The schema deliberately permits `prior_path == current_path` for the
compensation-pending phase in section 2.1. Repository writes and image
validation enforce the cross-table invariant: equal endpoints require exactly
one open rename/move prefix for the same owner. SQLite `CHECK` cannot express
that join. The invariant is validated after migration/open and before resume;
failure takes sanitized row-specific reconciliation, never silent clearing.

`pending_rename_intent_missing_file_deferrals` is the dedicated durable,
per-intent missing-file budget. Its sole row is absent at count zero and binds
one non-terminal content `event_id` to the owner intent. A row stores only
accepted parks 1 through 40; it deliberately cannot store 41. The 41st call
is evaluated inside the owning mutation and takes the terminal reconciliation
exit instead. Its primary key permits at most one missing-file budget per
intent, its `event_id` uniqueness prevents a content event from being counted
against two owners, and the foreign key is defensive only: every intentional
intent exit explicitly deletes this row with the intent. The migration/open
validator rejects a row whose event belongs to another owner, is not a
non-terminal content event, or whose intent parent is absent; the affected
row fails closed into the section 3 reconciliation exit.

Only normalized Vault-relative paths are stored. No source/version identity,
bytes, fingerprint, timestamps, error text, or provider data belong in either
new table. The counter table stores only opaque durable keys and its bounded
integer; it is not an attempt-history replacement.

The v9 -> v10 migration runs in one `begin immediate` transaction, validates
the complete v9 table surface, creates the two empty intent tables/index, and
changes both version stamps last. All v9 rows survive byte-for-byte.
Persistence accepts v9 as the newest predecessor and composes the existing v6
-> v7 -> v8 -> v9 chain before this migration. Fresh DDL and the
required-table validator include both new tables.

Runtime loading remains forward-only. A test-only v10 -> v9 downgrade contract
drops the index/tables and restamps both versions in one transaction only when
both intent tables are empty. Any non-empty intent or deferral table refuses with
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

interface PendingRenameMissingFileDeferral {
  readonly localFileId: string;
  readonly eventId: string;
  readonly deferredAttemptCount: number; // 1..40 only
}

type PendingRenameIntentMutation =
  | "created"
  | "composed"
  | "compensation_pending"
  | "unchanged"
  | "cancelled";

LifecycleRepository.readPendingRenameIntentForLocalFile(localFileId)
LifecycleRepository.readPendingRenameIntentByCurrentPath(normalizedPath)
LifecycleRepository.readPendingRenameIntentOwningEndpoint(normalizedPath)
LifecycleRepository.readPendingRenameIntents()
LifecycleRepository.recordOrComposePendingRenameIntent(input)
LifecycleRepository.recordPendingRenameLifecycleEvent(localFileId, fingerprint)
LifecycleRepository.reparentAndClearPendingRenameIntent(localFileId)
LifecycleRepository.resolveIntentAwareLifecycleTerminal({
  eventId,
  terminalState, // "blocked_conflict" | "integrity_failed"
  attemptedAtEpochMs,
  requestCorrelationId,
}) // -> "no_intent" | "intent_reconciled"

JournalRepository.resolveIntentAwareLocalFileMissing({
  eventId,
  attemptedAtEpochMs,
  requestCorrelationId,
  nextEligibleRetryEpochMs,
}) // -> IntentAwareLocalFileMissingResolution

JournalRepository.resolveIntentAwareContentTerminal({
  eventId,
  terminalState,
  safeError,
  attemptedAtEpochMs,
  requestCorrelationId,
) // -> "intent_preserved" | "intent_reparented" | "no_intent"
```

```ts
type IntentAwareLocalFileMissingResolution =
  | { readonly outcome: "waiting_for_rename" }
  | {
      readonly outcome: "reconcile_takeover";
      readonly diagnosticReason:
        | "pending_rename_intent_conflict"
        | "pending_rename_intent_exhausted";
    }
  | { readonly outcome: "closed_deferred_lifecycle" };
```

Names may change only if semantics remain one-for-one and tests name the same
contract. There is no public arbitrary endpoint-update or clear-only escape
hatch.

`recordOrComposePendingRenameIntent` validates normalized non-empty paths,
owner existence, non-`restore_pending` state, target availability, the exact
chain link, and the derived phase. Create/compose/cancel/compensation is one
serialized mutation. Return-to-prior cancels only without an open prefix; with
one it persists equal endpoints. Collision or incompatible chain handling
marks the same row `reconcile_required` and clears/reparents under section 3
in that mutation; if that fails, no partial intent change commits.

`recordPendingRenameLifecycleEvent` re-reads the intent and identity in its
transaction. It atomically freezes pending content, inserts/replays one
rename/move event plus operands from the current endpoints, and rebinds the
row. It deletes any missing-file deferral row bound to content it freezes in
that same transaction. A unique/idempotent event lookup prevents a restart
from materializing a second event for the same unresolved prefix. The intent
remains present.

Receipt handling, echo consumption, row-specific reconciliation,
`removeLocalMapping`, and restore phantom cleanup absorb their section 3
intent action into their existing serialized transaction. A read-then-write
pair across transactions is not acceptable for any exit.

Every content queue terminal call, including terminal server/wire mappings,
uses `resolveIntentAwareContentTerminal`; direct `markEventTerminal` is not an
allowed content-lane bypass. Its transaction applies the generic terminal rule
from section 3.1. `resolveIntentAwareLocalFileMissing` is the specialized
bounded branch of the same owner and shares its final close/reparent logic.
Every lifecycle-driver `blocked_conflict` or `integrity_failed` call instead
uses `resolveIntentAwareLifecycleTerminal`; direct lifecycle calls to
`recordEventAttempt` plus `markEventTerminal` are prohibited because they can
close a prefix while retaining its intent.

`resolveIntentAwareLocalFileMissing` owns the serialized durable decision,
including attempt audit, counter mutation or deletion, event park/close,
reparent/clear, and reconciliation flag. It returns its discriminated result
only after that transaction commits. The queue driver is the sole diagnostics
owner: after a committed `reconcile_takeover`, it emits exactly one trail entry
from `diagnosticReason`; no repository method, fallback terminal resolver, or
caller-side counter inspection may emit or infer either token. The mismatch
branch is evaluated before any counter increment or cutoff calculation and
returns only `pending_rename_intent_conflict`. The cutoff branch is reachable
only for the same event with stored count 40 and returns only
`pending_rename_intent_exhausted`. Thus a mismatch can never emit exhaustion,
and an exhausted matching counter can never emit conflict.

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
  identity/multipart progress, and schedules the next check after
  `FILE_SETTLE_DELAY_MS`. It also writes the ordinary redacted
  `journal_attempts` audit outcome, but that ten-row ring is audit only and is
  never read as the cutoff budget.
- `PENDING_RENAME_MISSING_FILE_MAX_DEFERRALS` is a separately named constant
  with value 40. Inside the same serialized mutation that writes the audit and
  parks/closes the event, the resolver reads
  `pending_rename_intent_missing_file_deferrals`: absent means accepted park
  1 and inserts `{ event_id: E1, deferred_attempt_count: 1 }`; a row bound to
  E1 increments exactly once. Only the same event may resume its budget. A
  different event ID while the row exists is an invariant failure, not a reset:
  before reading or changing the count, the mutation takes the row-specific
  reparent/clear/reconcile exit and returns `reconcile_takeover` with
  `pending_rename_intent_conflict`. The queue driver emits that token exactly
  once after the committed result; it must not fall through to exhaustion.
- Calls 1 through 40 commit the increment and `waiting_retry` together and
  preserve the intent. The state row's maximum stored value is 40. The next
  invocation computes 41 inside the transaction rather than attempting to
  write an out-of-range row. It writes the audit, closes E1 as terminal
  `deferred_lifecycle`, clears multipart progress, explicitly deletes the
  deferral state, reparents R1 to `intent.current_path`, clears the intent,
  sets R1 and the device journal to `reconcile_required`, and returns
  `reconcile_takeover`. The caller starts or retains the existing repair
  barrier. Admission is then governed by reconciliation, not by a leaked path
  reservation. The queue
  driver emits that token exactly once after the committed result; this
  matching-event cutoff must not emit the conflict token.
- The counter is reset only by deleting its row, never by decreasing it. A
  content committed/no-change receipt (including exact replay) for its bound
  E1 deletes the row in the same receipt transaction before preserving/rearming
  the intent. Every terminal content resolver, lifecycle freeze that closes
  that content event, direct lifecycle-terminal resolver, reparent/clear,
  delete-with-owner, and reconciliation/rebuild exit explicitly deletes the
  row with the intent or with its bound event. A later eligible content event
  can start a fresh budget only after that durable clear. A delete/reconcile
  transition that takes ownership before attempt 41 cancels further re-arms in
  the same transaction.
- On restart the selector reads this dedicated state, validates its owner/event
  relation, and resumes from its stored `deferred_attempt_count`; it does not
  count `journal_attempts` rows. Concurrent callbacks cannot both escape the
  cutoff because no caller may separately call `recordEventAttempt`, update the
  counter, or park/close this branch outside the resolver.
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
- Crash with compensation pending `(A, A)`: restart proves the phase from the
  open `A -> B` operands. Initial or exact replay receipt rebases to `(B, A)`
  and schedules exactly one compensation; it never misclassifies the row as an
  unmaterialized cancellation.
- Crash during intent-owned missing-file retry: the dedicated deferral row,
  not the pruned attempt audit, preserves the remaining bounded budget. A
  crash exposes either the prior count/event state or the complete parked
  mutation. Attempt 41 atomically closes, reparents, clears, and transfers to
  reconciliation, so neither retry nor admission can wedge indefinitely.
- Crash during an intent-owned lifecycle `blocked_conflict` or
  `integrity_failed` verdict exposes either the intact non-terminal prefix and
  intent or the complete terminal/reparent/clear/reconcile transaction. A
  rejected prefix never survives terminal beside an intent after restart.
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
  or corrupt guarded-state coexistence was sent to reconciliation;
- `pending_rename_intent_exhausted`: the bounded intent-owned missing-file
  window ended and ownership transferred to reconciliation.
- `pending_rename_intent_lifecycle_rejected`: an intent-owned rename/move
  prefix received a terminal canonical rejection and atomically transferred
  locator ownership to reconciliation.

Each swallowed boundary appends exactly one readable trail entry. A successful
intent-owned lifecycle rejection appends exactly the lifecycle-rejected token;
if its required reconciliation mutation fails, the whole mutation rolls back
and the existing `lifecycle_reconcile_persist_failed` token is emitted instead.
No path, locator, note name, row/source/version id, fingerprint, content,
exception text, or timer value may appear in trail, Notice, logs, metrics,
tests, or handoff evidence. Ordinary admission suppression and successful
re-arm emit no failure entry.

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
   prefix receipt rebases it and produces exactly one successor. A materialized
   `A -> B` followed by local `B -> A` persists compensation across restart and
   exact receipt replay, then commits exactly one `B -> A` successor.
6. Intent-aware missing-file handling preserves exact replay and never alters
   the pure-create no-intent branch; if replay and bytes remain unavailable,
   the dedicated durable 41st-call cutoff deterministically reparents, clears,
   and transfers to reconciliation even after restart and attempt-ring pruning.
7. `restore_pending`, explicit restore reservation, delete deferral,
   reconciliation, exact echo, conflict barrier, and journal rebuild guards
   retain their documented behavior.
8. v9 images upgrade losslessly to v10; fresh/reopened v10 images validate;
   the guarded empty downgrade round-trip succeeds and non-empty downgrade
   refuses without mutation.
9. A direct lifecycle `blocked_conflict` or `integrity_failed` closes its
   prefix through the lifecycle resolver: with an intent it leaves one
   reconciled row at the latest local endpoint, no intent/deferral state, and
   one sanitized rejection token; without an intent it preserves present
   terminal behavior.
10. Every new closed failure reason is visible through the sanitized durable
    diagnostics trail, and all plugin unit tests, type check, lint, and build
    pass.

## 10. Task 3 test matrix

Task 3 must add or extend tests at these layers:

| Layer | Mandatory cases |
| --- | --- |
| `sqlite-database.test.ts`, lifecycle schema tests, persistence tests | Fresh v10 DDL includes intent plus deferral state; v9 -> v10 preservation; current/prior generation reopen with an intent and a valid bound counter; invalid counter parent/event/owner fails closed; valid equal-endpoint compensation plus open prefix reopens, while equal endpoints without exactly one prefix reconcile; empty v10 -> v9 -> v10 round-trip; either non-empty intent/deferral downgrade refusal; malformed/torn/foreign image rejection; rebuild yields empty intents/deferral state plus reconcile-first. |
| `lifecycle-repository.test.ts`, `lifecycle-driver.test.ts` | Create, exact duplicate, `A -> B -> C` composition, unmaterialized return-to-prior cancellation, materialized `A -> B` then `B -> A` compensation `(A, A)`, current-path uniqueness/collision reconciliation, guarded restore owner, atomic lifecycle materialization from latest endpoints, exact replay, final clear, prefix rebase, row reconcile, phantom restore refusal, and all delete/cascade exits. Seed a valid content counter, then materialize the lifecycle prefix: the intent persists but the frozen content event and reopened journal have no counter; injected rollback leaves no prefix/freeze and the valid pre-transition counter only. Direct API `blocked_conflict` and `integrity` / `integrity_5xx` on an intent-owned prefix must atomically terminalize, reparent latest target, clear intent/counter, require reconciliation, retain one audit, and emit exactly one rejection token; no-intent parity, compensation rejection, and injected rollback are mandatory. |
| `repository.test.ts`, `queue-driver.test.ts`, multipart tests | Intent-owned bytes resolve at the current path. Exercise the resolver itself 40 times (not an attempt-row count), prove its dedicated counter reaches 40 while the audit ring remains pruned to 10, reopen, then make the 41st call take atomic reconcile ownership with only `pending_rename_intent_exhausted`; a seeded counter whose call uses another event ID must take reconcile ownership with only `pending_rename_intent_conflict`, without incrementing or evaluating the cutoff. Cover restart at an intermediate count, recovery receipt clearing/resetting the bound counter, counter transaction rollback, and no-intent close. Seed a valid counter before a generic `resolveIntentAwareContentTerminal` close and prove the committed close deletes it while preserving or clearing the intent as the owner shape requires; injected rollback keeps both the non-terminal event and valid counter, and reopen after each outcome never resumes a stale counter for a terminal/frozen event. Parameterize every terminal content class in section 3.1 over identity, successor, and last-event shapes and both transports where reachable; prove retryable transport parks preserve. Content commit/no-change/exact replay preserves intent; transaction failure cannot race intent creation or swallow its token. |
| `lifecycle-capture.test.ts` | Rapid prior miss resolves through the current endpoint; only one owner/timer exists; re-arm reads `C`, not frozen `B`; restart enumeration including a valid missing-file counter; late composition after prefix materialization; `A -> B` materialized then `B -> A`, restart, exact prefix receipt, one `B -> A` successor, and final clear; rename versus move derived from final endpoints; unmaterialized return-to-prior cancellation; exact final echo and non-matching prefix echo; delete ladder and `restore_pending` guards. |
| `capture.test.ts`, automatic convergence tests | Rename-tail intermediate admission and final admission are suppressed on the existing tail; snapshot cannot mint R2; after reparent/clear the same row can admit a successor; pure-create transit heal remains unchanged; read failure fails closed with diagnostics. |
| `journal-sync-journey.test.ts` | Convert the Task 1 RED to green without deleting its schedule assertions: E1 server commit precedes client receipt, `A -> B -> C` crosses settle, R1 survives, exact receipt establishes identity, one final lifecycle operation commits, and canonical/local state converge without `blocked_conflict`. Add restart between composition and receipt. |
| diagnostics tests/export tests | Closed token membership including exhaustion and lifecycle rejection; one-entry emission at each swallowed read/persist/conflict/exhaustion/rejection boundary; lifecycle-resolver rollback emits only `lifecycle_reconcile_persist_failed`; stable export/stop-reason derivation; and explicit proof that paths and identities are absent. |

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
