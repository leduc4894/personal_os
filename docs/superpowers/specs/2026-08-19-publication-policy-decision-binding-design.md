# Publication Policy Decision Binding Design

**Date:** 2026-08-19

**Status:** Implemented and verified (2026-08-19); implementation record: `docs/handoff/2026-08-19-publication-policy-decision-binding.md`

**Scope:** Small-file sync publication only

## 1. Purpose

Fix the publication-time locator gap in the small-file sync path. A successful
server preflight already evaluates the active signed exclusion policy against a
subject that includes `normalized_locator`, but the later source-publication
guards reconstruct a subject without locator evidence. Any active
`extension`, `folder_prefix`, or `path_glob` rule therefore turns that later
evaluation into `indeterminate`, which is enforced as deny.

This design binds the server's allowed preflight decision to the durable upload
operation and reuses that evidence while the same signed policy revision remains
active. Publication re-evaluates only after the active revision changes. The
transaction-final policy check remains authoritative and fail-closed.

This document specifies behavior and contracts only. It does not implement or
modify product code.

## 2. Canonical context

### 2.1 Product and architecture constraints

- The product is fail-closed when policy or canonical bytes are ambiguous
  (`docs/00-PRODUCT_VISION_AND_PRD.md:53-61`).
- PostgreSQL owns policy and source identity while R2 owns immutable canonical
  bytes (`docs/01-CANONICAL_ARCHITECTURE.md:15-22`,
  `docs/01-CANONICAL_ARCHITECTURE.md:71-79`).
- A source pointer must not publish until its object is written and verified
  (`docs/03-DATA_OWNERSHIP_AND_STORAGE.md:40-48`).
- Backend policy enforcement is authoritative; missing required evidence yields
  `indeterminate` and is enforced as deny
  (`docs/14-SECURITY_PRIVACY_AND_POLICY.md:20-38`).
- Telemetry must not contain locator, raw content, token, secret, or other
  sensitive operands (`docs/14-SECURITY_PRIVACY_AND_POLICY.md:81-92`).
- Schema changes require Alembic upgrade/downgrade coverage and API changes
  require OpenAPI, generated-client, contract-test, and documentation updates
  (`docs/20-IMPLEMENTATION_PLAN.md:42-49`). This design requires neither a
  schema change nor a public API change.

### 2.2 Existing small-file contract

The canonical child-4 design requires the operation record to bind device,
workspace, event identity, idempotency key, declared fingerprint, **policy
decision**, and expiry. It also says the record is implementation state rather
than a source locator or provider receipt
(`docs/superpowers/specs/2026-08-18-plugin-journal-and-small-file-sync-design.md:294-332`).

The current wire request includes the plugin's accepted `policy_revision`
(`apps/api/src/api_runtime/small_file_sync_models.py:69-91`) and converts it to
`SmallFilePreflight.policy_revision_number`
(`apps/api/src/api_runtime/small_file_sync_models.py:136-193`). That value is a
client claim. The server's locator-aware policy decision remains the authority.

The operation schema already has a non-null `policy_revision_number` and no
locator, raw token, receipt, object key, or byte payload
(`packages/postgresql-source-store/src/postgresql_source_store/tables.py:525-553`).
The migration contract pins that privacy-preserving column set
(`tests/unit/migrations/test_small_file_sync_migration.py:36-65`). No migration
is needed.

### 2.3 Current implementation gap

`SmallFileSyncService._preflight_once` invokes the locator-aware guard before
replay, update-base checking, and reservation, but discards the guard's decision
(`src/personal_os/small_file_sync/service.py:318-365`). The guard protocol
currently returns `None` (`src/personal_os/small_file_sync/ports.py:69-89`), and
the composition adapter also discards the `PolicyDecision`
(`apps/api/src/api_runtime/small_file_sync_composition.py:132-165`).

Reservation writes the plugin-declared revision directly from `SmallFilePreflight`
(`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py:261-288`).
The receive-side view does expose that row value
(`src/personal_os/small_file_sync/ports.py:37-66`,
`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py:528-561`),
but `SmallFileSyncService` never consults it before calling the shared
publication service (`src/personal_os/small_file_sync/service.py:438-557`).

The shared publication service always calls `authorize_publication` before
idempotency lookup and object resolution
(`src/personal_os/sources/publication.py:253-305`). That method constructs a
subject with source type, media type, and size, but no locator
(`src/personal_os/exclusion_policy/enforcement.py:492-531`). The evaluator then
raises the closed indeterminate error for missing required evidence
(`src/personal_os/exclusion_policy/enforcement.py:421-427`).

The production composition injects the same enforcement service into both the
locator-aware small-file guard and the locator-free publication service
(`apps/api/src/api_runtime/small_file_sync_composition.py:196-241`). The
PostgreSQL publication store also performs an independent locator-free policy
evaluation under `workspace_policy_state FOR UPDATE`
(`packages/postgresql-source-store/src/postgresql_source_store/publication_store.py:600-698`,
`packages/postgresql-source-store/src/postgresql_source_store/publication_store.py:700-732`).
Therefore bypassing only the outer publication guard would not fix the real
transaction path.

The known gap is indexed separately from the signing-key rotation problem in
`docs/handoff/BACKLOG.md:74-75`. This design fixes only the small-file entry.

## 3. Behavior and contract

### 3.1 Bound evidence value

Add an internal immutable value named `AllowedPolicyRevisionBinding`:

```text
AllowedPolicyRevisionBinding
  workspace_id               UUID, non-nil
  policy_revision_number     integer >= 1
```

The type name is the allowed-outcome invariant. It has no separate outcome
field and carries no locator, subject fingerprint, matched rule IDs, missing
fields, payload bytes, digest token, signing material, or timestamps.

Only a successful server-side evaluation may create this binding. The
locator-aware small-file guard converts the returned allowed `PolicyDecision`
to `AllowedPolicyRevisionBinding`; it never derives the revision from the
plugin request. `PolicyDecision` already carries server-verified workspace and
revision identities plus the evaluated outcome
(`src/personal_os/exclusion_policy/enforcement.py:158-190`).

Persisting only `(workspace_id already on row, policy_revision_number)` is
sufficient because:

1. reservation is unreachable after a denied or indeterminate preflight;
2. `AllowedPolicyRevisionBinding` is required by the reservation port;
3. signed policy revisions are immutable and revision numbers are
   workspace-scoped monotonic identities—the publication transaction inserts
   an immutable revision and advances the number under the serialization lock
   (`packages/postgresql-source-store/src/postgresql_source_store/policy_publication.py:8-18`,
   `packages/postgresql-source-store/src/postgresql_source_store/policy_publication.py:800-830`);
4. publication verifies the active signed snapshot before trusting revision
   equality; and
5. transaction-final comparison repeats under the policy-state lock.

No decision payload or locator is persisted.

### 3.2 Preflight contract

Change `SmallFilePolicyGuard.authorize_small_file(...)` to return
`AllowedPolicyRevisionBinding` rather than `None`.

Change `SmallFileUploadOperationStore.reserve_operation(...)` to require that
binding in addition to the preflight and credential-derived device context. The
store must reject a binding whose `workspace_id` differs from the device
context before issuing SQL.

The preflight order remains:

```text
validate request
  -> locator-aware active-policy evaluation
  -> exact terminal replay lookup
  -> update-base/no-change check
  -> reserve or rebind operation using server decision revision
```

For a new operation, insert `policy_revision_number` from the binding. For an
existing `pending` operation reached by a new successful preflight, rotate the
token hash and update `policy_revision_number` to the new server binding in the
same transaction. This applies whether or not the pending reservation has
expired. The current rotation statement updates only token hash, expiry, and
timestamp
(`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py:291-309`);
implementation must extend that statement with the bound revision.

`receiving` is a claimed ownership state, not an expired reservation that a
second preflight may reclaim. The `pending -> receiving` claim must land before
the content stream is consumed. Once claimed before the reservation deadline,
the exact token and bound revision remain the receive's fence across that
deadline: an exact-token retry may resume, but a same-identity preflight must
fail closed and may not rotate either value. Only an expired `pending` row is
reclaimable. This prevents a paused receive from publishing canonical state and
then losing its terminal write to a later token/revision rotation.

The request fingerprint continues to exclude policy revision so a later
locator-aware preflight can reauthorize and rebind the same journal identity;
the existing comparison deliberately follows that rule
(`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py:177-202`).

The receive-side terminal transition must compare the claimed token, operation
state, bound revision, identity, and declared content fields as one guarded
fence. Its eligibility does not depend on the original reservation deadline:
expiry prevents a pending claim or enables a pending reclaim, but cannot revoke
an already claimed receive. The current comparison covers identity and declared
content fields but omits `policy_revision_number`
(`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py:564-583`).

The plugin-provided `policy_revision` remains required on the existing request
and remains available for plugin/session consistency. It is not the persisted
authorization authority. No request or response shape changes.

### 3.3 Explicit small-file publication port

Replace the concrete `SourceVersionPublicationService` dependency of
`SmallFileSyncService` with a provider-neutral `SmallFilePublicationGateway`
port. It exposes create and update publication operations equivalent to the
current calls, with one additional required
`AllowedPolicyRevisionBinding` argument. The current service field is concrete
(`src/personal_os/small_file_sync/service.py:230-252`).

The domain service reconstructs the binding only from the bound operation row:

```text
workspace_id               = bound.workspace_id
policy_revision_number     = bound.policy_revision_number
```

It validates that the command workspace, credential-derived workspace, bound
workspace, and binding workspace are identical before crossing the gateway.

The production composition provides
`BoundPolicySmallFilePublicationGateway`. For each invocation it creates an
immutable guard bound to that invocation's evidence and invokes the existing
`SourceVersionPublicationService` over the shared store, object store, metrics,
and clock. Creating an invocation-local guard is a pure object construction; it
does not open a database or object-store connection.

The existing `SourceVersionPublicationService` orchestration order remains
unchanged. Its policy-guard return type is widened internally to
`PolicyDecision | AllowedPolicyRevisionBinding`; regular guards continue to
return `PolicyDecision`.

### 3.4 Outer publication authorization

Add a bound-publication authorization operation to
`PolicyEnforcementService`; do not change the semantics of its existing
`authorize_publication` method.

For `(command, binding)` the bound operation:

1. rejects a workspace mismatch;
2. loads the active snapshot through `ActivePolicySnapshotSource`, whose
   production adapter is `PostgresqlActivePolicySnapshotSource`
   (`src/personal_os/exclusion_policy/enforcement.py:225-236`,
   `packages/postgresql-source-store/src/postgresql_source_store/policy_enforcement.py:190-225`);
3. fails closed if no active revision exists;
4. verifies and parses the signed material through
   `parse_verified_policy_revision`, including payload hash, signature,
   canonical payload, and row/payload identity checks
   (`src/personal_os/exclusion_policy/enforcement.py:277-350`);
5. compares the verified active revision number with the bound number;
6. when equal, returns the binding without running `evaluate_policy`; and
7. when different, constructs the same locator-free publication subject and
   runs the existing publication evaluation path against the newly loaded
   revision.

The changed-revision path may allow a revision whose rules are fully decidable
from publication evidence. It denies a definite match and denies indeterminate
when the new revision needs locator evidence. This preserves the existing
deny-only evaluator semantics rather than treating every policy publication as
an unconditional upload failure.

The same closed low-cardinality boundary/decision metrics remain. A verified
equal-revision binding records an `allowed` authorization outcome without
recording a locator, subject fingerprint, revision, rule, or operand as a
metric label.

### 3.5 Transaction-final authorization

The PostgreSQL publication store remains the authoritative serialization
boundary. Extend its policy helper to accept either ordinary `PolicyDecision`
or `AllowedPolicyRevisionBinding`.

After idempotency locking, trusted workspace/actor checks, and locked replay
resolution, the store must:

1. reject evidence from another workspace;
2. lock `workspace_policy_state` using the existing global lock order;
3. load and verify the locked active signed snapshot;
4. when evidence is `AllowedPolicyRevisionBinding` and active revision equals
   the bound revision, record an allowed authorization and skip the evaluator;
5. when the revision differs, build the existing authoritative locator-free
   subject from command plus verified receipt and evaluate the locked active
   revision; and
6. on any denial, indeterminate outcome, missing policy, signature failure, or
   database failure, roll back without source/version/current-pointer mutation.

For ordinary `PolicyDecision`, the store keeps its current unconditional
locked re-evaluation. Thus all non-small-file callers retain current behavior.

The policy row lock linearizes source publication against policy publication.
If a new policy waits behind a source transaction that already owns the lock,
the source commit is ordered before the new revision. If the new revision owns
the lock first, the source transaction sees it and re-evaluates.

### 3.6 Guard state machine

```text
LOAD_ACTIVE
├─ database/load failure ───────────────> FAIL_CLOSED
├─ no active revision ──────────────────> NOT_INITIALIZED
├─ invalid signed material ─────────────> SIGNING_UNAVAILABLE
└─ verified active revision
   ├─ active == bound ──────────────────> ALLOW_BOUND_WITHOUT_EVALUATION
   └─ active != bound
      ├─ current evaluation allowed ────> ALLOW_CURRENT_DECISION
      ├─ current evaluation excluded ───> DENY
      └─ evidence incomplete ───────────> INDETERMINATE_DENY
```

Transaction-final state machine:

```text
LOCK_ACTIVE_POLICY
├─ binding workspace mismatch ──────────> ABORT_INVARIANT
├─ load/verification failure ───────────> ROLLBACK_FAIL_CLOSED
├─ active == bound ─────────────────────> SKIP_EVALUATOR_AND_COMMIT
└─ active != bound
   ├─ authoritative evaluation allowed ─> COMMIT
   └─ excluded/indeterminate ───────────> ROLLBACK_DENY
```

### 3.7 Text sequence diagram

```text
Plugin
  -> SmallFileSyncService.preflight(locator-aware subject)
  -> PolicyEnforcementSmallFileGuard
  -> load + verify active revision N
  -> evaluate locator-aware subject
  <- AllowedPolicyRevisionBinding(workspace, N)
  -> operation_store.reserve_operation(binding N)
  -> insert or atomically rebind operation to N
  <- opaque operation token

Plugin PUT
  -> SmallFileSyncService.receive(token)
  -> operation_store.resolve_bound_operation()
  <- operation carrying workspace + bound revision N
  -> spool, hash, size, and media-type verification in R2
  -> SmallFilePublicationGateway(command, binding N)
  -> load + verify active revision

  [active still N]
    -> return bound allowed evidence without locator-free evaluation

  [active is M]
    -> run current publication evaluation under M
    -> allow, definite deny, or indeterminate-deny

  -> SourceVersionPublicationService
  -> PostgreSQL publication transaction
  -> idempotency advisory lock
  -> workspace and actor checks
  -> workspace_policy_state FOR UPDATE
  -> load + verify locked active revision

  [locked active == evidence-bound revision]
    -> skip evaluator

  [locked active changed]
    -> evaluate authoritative locator-free publication subject

  -> canonical source/version commit
  -> record operation terminal result
  <- committed receipt
```

If a new locator-dependent revision is published after preflight, the outer or
locked mismatch path produces `exclusion_policy_indeterminate` and publishes
nothing. The next plugin preflight evaluates the same event with locator
evidence under the new revision and settles to `excluded`. This preserves the
documented self-healing scenario
(`docs/operations/plugin-journal-small-file-sync.md:100-116`,
`docs/operations/plugin-journal-small-file-sync.md:123-133`).

Verified CAS bytes may already exist when the policy denial occurs. They remain
unreferenced by canonical source state; this design does not add a deletion or
cleanup path.

## 4. Concurrency and isolation

The binding is passed explicitly by value through every layer. No singleton
stores a current revision, no setter mutates a shared guard, and no
`ContextVar` carries authorization state.

Each receive invocation owns:

```text
bound operation -> immutable policy binding -> invocation-local guard
                -> per-call policy evidence -> one store commit call
```

Two receives may therefore carry different bound revisions through the same
composition graph without interference. Shared services are stateless or
receive evidence as method arguments. Retry closures capture only their own
immutable evidence, and every transaction attempt reloads the locked active
revision. Cancellation leaves no authorization state to reset.

Operation rebind is protected by the existing operation-identity advisory lock
and row lock (`packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py:803-866`).
Token rotation and revision update must be one SQL update in that transaction,
and only a `pending` row may enter that update. A `receiving` row retains its
claim until guarded terminalization even when wall-clock expiry passes.

## 5. Decisions and rejected alternatives

### 5.1 Persist revision plus an allowed invariant; do not persist a payload

**Decision:** persist only the existing server-owned policy revision number.
Represent `allowed` in the type and reservation call graph, not as another
column.

**Reason:** matched rules, missing fields, fingerprints, and locator-derived
material add sensitive or unnecessary state. The immutable revision plus the
fact that only an allowed binding can reserve is sufficient when both
publication checks verify active-revision equality.

**Rejected:** store a serialized `PolicyDecision`, subject fingerprint, locator,
or decision JSON. It expands privacy and migration scope without strengthening
the equality rule.

### 5.2 Use an explicit small-file publication port

**Decision:** pass binding through `SmallFilePublicationGateway` and an
invocation-local bound guard.

**Reason:** authorization provenance is visible in signatures, type checking,
fakes, and tests. There is no cross-task mutable state and no cleanup obligation.

**Rejected:** begin/end hooks backed by `ContextVar`. A correctly reset
per-task context can avoid ordinary asyncio races, but it creates ambient
coupling between `SmallFileSyncService`, the publication guard, retries, and
the transaction store. Nested calls and missing reset paths are harder to
audit than a value passed explicitly.

**Rejected:** a shared wrapper guard with a setter. The production composition
is a singleton graph; two receives can interleave at any `await` and overwrite
the shared revision. The current composition builds one service graph containing
one publication service and one policy service
(`apps/api/src/api_runtime/small_file_sync_composition.py:196-241`). Serializing
the setter would unnecessarily serialize all publication and still leave
cancellation cleanup risk.

### 5.3 Re-evaluate changed revisions through the existing evaluator

**Decision:** when active revision differs, run the current publication
evaluation rather than returning a new unconditional revision-changed error.

**Reason:** a revision containing only fully available operands can safely
allow the publication. Locator-dependent revisions already fail closed as
`exclusion_policy_indeterminate`; definite matches already use
`exclusion_policy_denied`. Reusing these codes preserves the public contract
and plugin recovery flow.

**Rejected:** add `small_file_policy_revision_changed`. It would deny benign
policy changes, add registry/OpenAPI/client work, and duplicate evaluator
semantics.

### 5.4 Keep transaction-final comparison

**Decision:** the equal-revision optimization applies at both the outer guard
and the locked store check.

**Reason:** changing only the outer guard leaves the existing locator-free
transaction evaluator in place. Removing the locked check would open a race
between the outer read and canonical commit.

## 6. Error cases

No new error code is introduced. Existing codes are registered in
`src/personal_os/error_contracts/codes.py:97-116` and their policy definitions
are in `src/personal_os/error_contracts/codes.py:586-656`.

| Condition | Required result | Canonical public code |
|---|---|---|
| Active revision changed and a rule definitely matches | deny; no publication | `exclusion_policy_denied` |
| Active revision changed and locator evidence is required | deny; no publication | `exclusion_policy_indeterminate` |
| No active policy row/revision | deny; no publication | `exclusion_policy_not_initialized` |
| Active snapshot hash/signature/payload is invalid | deny; no publication | `exclusion_policy_signing_unavailable` |
| Outer active-revision database read fails | fail closed before publication | `internal_error` through the existing safe exception boundary |
| Database/commit failure under the publication transaction | roll back or resolve through existing retry/evidence lookup; never guess success | existing source-store database mapping (`packages/postgresql-source-store/src/postgresql_source_store/error_mapping.py:95-114`) |
| Binding workspace differs from bound operation, device context, or command | reject before source publication | `small_file_upload_state_invalid` |
| Store receives bound evidence for another workspace | abort transaction as a concurrency invariant | existing `source_concurrency_invariant_failed` defense |
| Operation row revision differs from the receive binding during terminal write | reject terminal transition | `small_file_operation_identity_mismatch` |

The HTTP mappings of policy denial, indeterminate, not-initialized, signing
unavailable, and internal error are already closed
(`src/personal_os/api_contracts/errors.py:92-132`). An unclassified outer-load
exception is collapsed to the internal envelope without copying exception text
(`apps/api/src/api_runtime/application.py:697-705`). Errors and diagnostics must
not copy locator, digest, token, payload, rule operand, signature, public key,
or driver text.

## 7. Acceptance criteria

1. With active revision `N` containing exactly one deny rule
   `extension=".tmp"`, uploading `notes/example.md`:
   - passes locator-aware preflight;
   - stores revision `N` from the server decision on the operation;
   - skips locator-free evaluation while `N` remains active;
   - publishes exactly one canonical source version;
   - returns a committed terminal receipt; and
   - leaves the plugin journal event in `committed`, not `integrity_failed`.
2. Repeating the same operation after a lost response returns the frozen result
   and does not create a second source version, event, projection intent, or
   object upload.
3. If revision `M` is published after preflight and denies the candidate:
   - the outer or locked comparison detects `M != N`;
   - publication is denied with no canonical mutation;
   - the plugin retains the journal event according to the existing retry
     contract; and
   - the next locator-aware preflight under `M` settles to `excluded`.
4. If a changed revision is fully decidable from publication evidence and
   allows the subject, publication may proceed after the normal evaluation and
   locked recheck.
5. Two concurrent receives with different bound revisions never observe or
   use each other's binding, regardless of scheduling order.
6. Loss of database access during either active-revision read fails closed and
   creates no source, source version, current pointer, sync event, projection
   intent, or terminal committed operation result.
7. Re-preflight of a `pending` operation under a newly allowed revision updates
   token hash and policy revision atomically.
8. A receive claimed before expiry retains its token and revision fence after
   expiry. Same-identity preflight cannot reclaim its `receiving` row, the exact
   token may resume, and guarded terminalization can commit once without an
   expiry-only rejection.
9. Existing callers outside small-file sync retain current
   `authorize_publication` and locked re-evaluation behavior.
10. `tests/unit/small_file_sync`, small-file integration tests, and
   `tests/contract/small_file_sync` pass, along with relevant API route and
   OpenAPI snapshot tests.
11. Ruff and mypy-strict pass for every affected Python package.

## 8. Testing strategy

Implementation follows TDD: each behavior below begins with a failing test.

### 8.1 Small-file domain unit tests

Update `tests/unit/small_file_sync/fakes.py`:

- allowing guard returns a configurable server-owned
  `AllowedPolicyRevisionBinding`;
- operation store records the binding separately from the plugin request;
- re-reservation updates the fake row's revision;
- publication gateway records the exact per-call binding; and
- active-policy fakes support equal, changed, absent, corrupt, and load-failure
  states without retaining locator or content.

Add tests to `tests/unit/small_file_sync/test_service.py` for:

- server decision revision overriding a different client-declared revision;
- reservation receiving the exact allowed binding;
- receive reconstructing binding only from `SmallFileBoundOperation`;
- workspace mismatch failing before publication;
- committed replay remaining single-publication; and
- two receives interleaved with `asyncio.gather` and barriers. Reverse the
  barrier release order in a parameterized case to prove schedule independence.

### 8.2 PostgreSQL operation adapter tests

Extend `tests/unit/postgresql_source_store/test_small_file_sync_operations.py`
to prove:

- insert uses server binding revision;
- token rotation and revision rebind are one parameter-bound update;
- operation fingerprint still excludes policy revision;
- receive-side row comparison includes policy revision; and
- expired `pending` rows rebind, while claimed `receiving` rows cannot be
  reclaimed and can terminalize after expiry; and
- no locator, raw token, receipt, decision payload, or provider field is added.

Keep `tests/unit/migrations/test_small_file_sync_migration.py` unchanged and
green as evidence that no migration occurred.

### 8.3 Policy and publication unit tests

Add focused tests around bound-publication authorization:

- equal verified revision returns bound evidence without invoking evaluator;
- snapshot verification still runs on the equal path;
- changed revision invokes the current publication evaluator;
- changed locator-dependent revision returns indeterminate-deny;
- changed definite rule returns definite deny;
- absent or corrupt active snapshot fails closed; and
- database load failure does not invoke the publication service/store.

Extend source-publication store tests to prove:

- bound evidence plus equal locked revision skips evaluator and commits;
- bound evidence plus changed locked revision evaluates;
- a regular `PolicyDecision` still follows unconditional locked evaluation;
- revision change between outer check and lock is detected;
- workspace-mismatched evidence aborts; and
- database failure rolls back all canonical writes.

### 8.4 Integration and journey tests

Extend `tests/integration/small_file_sync/test_policy_and_device_boundaries.py`.
Its current scenarios already cover a policy published during upload and the
locator-rule indeterminate result
(`tests/integration/small_file_sync/test_policy_and_device_boundaries.py:83-149`).
Update its harness so both the outer guard and transaction-final comparison
honor bound evidence; a fake commit that ignores policy evidence is
insufficient.

Add the life-or-death regression:

```text
active rules = [extension ".tmp"]
preflight locator = "notes/example.md"
upload exact declared bytes
expect HTTP success + committed result
expect publication_commits == 1
repeat same identity
expect frozen replay + publication_commits == 1
```

Add a deterministic PostgreSQL expiry race: reserve, claim, advance the store
clock past `expires_at`, pause before terminalization, and attempt a successful
same-identity preflight under a later allowed revision. The preflight must be
rejected without rotating token or revision; the claimed token must still
resolve and its bound terminal write must commit exactly once.

Keep and adapt the changed-policy scenarios so they prove zero publication and
the next preflight's locator-aware self-heal.

Add or extend a PostgreSQL-backed integration in
`tests/integration/exclusion_policy/test_source_publication_enforcement.py` to
exercise the real locked comparison; the in-memory route harness alone cannot
prove `FOR UPDATE` behavior.

Extend the real Obsidian journey in
`apps/obsidian-plugin/test/specs/device-login-sync.e2e.ts` to publish the
`.tmp` rule, upload a `.md` fixture, and inspect sanitized journal evidence for
terminal `committed` plus exactly one server publication. The policy-change
journey must also demonstrate the next-preflight recovery. No diagnostic may
contain the fixture locator, digest, operation token, or payload.

### 8.5 Regression gates

At minimum, implementation runs:

```text
uv run pytest tests/unit/small_file_sync -q
uv run pytest tests/unit/postgresql_source_store/test_small_file_sync_operations.py -q
uv run pytest tests/unit/api_runtime/test_small_file_sync_composition.py -q
uv run pytest tests/integration/small_file_sync -q
uv run pytest tests/integration/exclusion_policy/test_source_publication_enforcement.py -q
uv run pytest tests/contract/small_file_sync tests/contract/api/test_small_file_sync_routes.py tests/contract/api/test_small_file_sync_openapi.py -q
```

Run the repository's relevant Ruff, mypy-strict, plugin unit, and real-Obsidian
journey gates on the same final commit. Because the wire contract is unchanged,
the committed OpenAPI and generated client should remain byte-equivalent; any
diff is a design violation unless this spec is amended first.

## 9. Deferred boundaries

The following are explicitly outside this design:

- the separate serve-side one-key verifier/keyset-chain rotation defect indexed
  at `docs/handoff/BACKLOG.md:74`;
- locator persistence or canonical locator lifecycle planned for child 5;
- storing locator, subject fingerprint, complete `PolicyDecision`, digest token,
  receipt, provider key, or payload on an operation;
- multipart or large-file upload;
- changing the plugin's general HTTP 403 mapping;
- deleting unreferenced CAS bytes after a denied publication;
- changing `authorize_publication` semantics for any caller outside the
  explicit small-file gateway;
- changing canonical-read, query, projection, or AI-provider policy guards; and
- redesigning source-publication idempotency, operation terminal atomicity, or
  trust-anchor rotation.

The backlog entry for the publication-time locator gap is removed only after
implementation and all acceptance gates pass. The separate verifier-chain
entry remains.

## 10. Open questions

None. The design choices required for implementation are closed by this spec.
