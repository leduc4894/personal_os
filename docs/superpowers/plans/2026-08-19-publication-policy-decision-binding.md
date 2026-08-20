# Publication Policy Decision Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a small-file upload publish under the server-verified, locator-aware
policy revision that allowed its preflight, while preserving fail-closed
re-evaluation whenever the active revision changes.

**Architecture:** Add an immutable allowed-revision binding to the exclusion-policy
domain, persist only its revision number on the existing upload-operation row,
and pass the reconstructed binding explicitly through an invocation-local
small-file publication gateway. The shared publication service remains the
orchestrator. Both its outer policy guard and PostgreSQL's transaction-final
policy check verify the active signed snapshot; they skip locator-free evaluation
only when that verified active revision equals the bound revision.

**Tech Stack:** Python 3.14, dataclasses and Protocol ports, pytest/pytest-asyncio,
SQLAlchemy 2, PostgreSQL, FastAPI composition, Alembic contract snapshots,
TypeScript 6/Vitest/WebdriverIO for the unchanged Obsidian client journey.

**Spec:**
`docs/superpowers/specs/2026-08-19-publication-policy-decision-binding-design.md`

## Global Constraints

- Implement only the approved small-file publication binding. Do not alter
  policy signing/key rotation, locator persistence, the wire request/response,
  OpenAPI, generated clients, database schema, or the operation fingerprint.
- The plugin's `policy_revision` remains a wire claim. Only the revision returned
  by the server's successful locator-aware evaluation may be persisted as
  publication authority.
- Persist no locator, policy payload, subject fingerprint, rule ID, signing
  material, token, receipt, raw content, or sensitive operand.
- Keep `SourceVersionPublicationService` as the only source-version publication
  orchestrator. Regular callers using `PolicyDecision` retain unconditional
  transaction-final re-evaluation.
- Pass binding state explicitly by immutable value. Do not use `ContextVar`,
  mutable guard setters, request-global state, or a shared “current binding.”
- Missing policy state, invalid signatures, database failures, revision changes
  requiring absent locator evidence, and workspace mismatches all fail closed
  through existing error codes.
- Every implementation task starts with the named failing test, reads the
  expected failure, applies the smallest passing change, and commits only after
  its focused gates pass.
- Do not update `docs/handoff/BACKLOG.md` until all acceptance and regression
  gates in Task 6 pass. Create exactly one implementation handoff in Task 7.

## Deliverable Structure

```text
src/personal_os/exclusion_policy/
├── enforcement.py              Immutable binding and bound authorization
└── __init__.py                 Public domain exports

src/personal_os/small_file_sync/
├── ports.py                    Guard/store/publication gateway protocols
└── service.py                  Preflight bind and receive-side gateway call

src/personal_os/sources/
├── ports.py                    Policy evidence union at publication seams
└── publication.py              Existing orchestration, widened evidence type

packages/postgresql-source-store/src/postgresql_source_store/
├── small_file_sync_operations.py  Atomic server-revision insert/rebind
├── policy_enforcement.py          Locked active-revision authorization helper
└── publication_store.py           Transaction-final binding consumption

apps/api/src/api_runtime/
└── small_file_sync_composition.py Locator guard and invocation-local gateway

tests/
├── unit/                         Contract, service, adapter, composition tests
├── integration/small_file_sync/  Wire-level policy/concurrency acceptance
└── integration/source_publication/ PostgreSQL row and locked-policy proofs

docs/
├── operations/plugin-journal-small-file-sync.md
├── handoff/BACKLOG.md
└── handoff/2026-08-19-publication-policy-decision-binding.md
```

## Responsibility and Dependency Map

| Responsibility | Owner | Must not own |
|---|---|---|
| Prove a locator-aware preflight was allowed under revision N | `PolicyEnforcementSmallFileGuard` + `AllowedPolicyRevisionBinding` | Plugin revision claims or policy payload persistence |
| Persist and rotate the authorized revision | `SmallFileUploadOperationStore` implementations | Locator or raw policy decision |
| Reconstruct receive authorization | `SmallFileSyncService` from `SmallFileBoundOperation` | Mutable request-local state |
| Authorize before object/publication work | `PolicyEnforcementService.authorize_bound_publication` | Transaction serialization |
| Preserve publication orchestration | `SourceVersionPublicationService` | Special-case small-file state |
| Serialize policy and canonical source mutation | PostgreSQL publication store under `workspace_policy_state FOR UPDATE` | Trust in outer guard alone |
| Bind evidence per invocation | `BoundPolicySmallFilePublicationGateway` | Shared mutable guard |

Dependency order is intentional: Task 1 defines the value, Task 2 makes the
operation row authoritative, Task 3 adds outer semantics, Task 4 adds the locked
semantics, Task 5 composes them without cross-request state, Task 6 proves the
user journey, and Task 7 records the verified result.

---

## Task 1: Introduce the Server-Owned Allowed Revision Binding

**Files:**

- Modify: `src/personal_os/exclusion_policy/enforcement.py`
- Modify: `src/personal_os/exclusion_policy/__init__.py`
- Modify: `src/personal_os/small_file_sync/ports.py`
- Modify: `apps/api/src/api_runtime/small_file_sync_composition.py`
- Modify: `tests/unit/exclusion_policy/test_enforcement.py`
- Modify: `tests/unit/api_runtime/test_small_file_sync_composition.py`
- Modify: `tests/unit/small_file_sync/fakes.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class AllowedPolicyRevisionBinding:
    workspace_id: UUID
    policy_revision_number: int

    def __post_init__(self) -> None:
        if self.workspace_id.int == 0:
            raise ValueError("workspace_id must be non-nil")
        if self.policy_revision_number < 1:
            raise ValueError("policy_revision_number must be positive")


type PublicationPolicyEvidence = PolicyDecision | AllowedPolicyRevisionBinding
```

```python
class SmallFilePolicyGuard(Protocol):
    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        raise NotImplementedError
```

- [x] Add `test_allowed_policy_revision_binding_rejects_nil_workspace_and_non_positive_revision`
  to `tests/unit/exclusion_policy/test_enforcement.py`. Construct one valid value,
  then assert `UUID(int=0)`, revision `0`, and revision `-1` raise `ValueError`.

- [x] Add `test_locator_guard_returns_the_server_verified_revision_not_the_plugin_claim`
  to `tests/unit/api_runtime/test_small_file_sync_composition.py`. Use a preflight
  whose `policy_revision_number` differs from the signed active snapshot, call
  `PolicyEnforcementSmallFileGuard.authorize_small_file`, and assert:

  ```python
  assert binding == AllowedPolicyRevisionBinding(
      workspace_id=device_context.workspace_id,
      policy_revision_number=active_revision_number,
  )
  assert binding.policy_revision_number != preflight.policy_revision_number
  ```

- [x] Run the two new tests and confirm they fail because the value does not exist
  and the current guard returns `None`:

  ```powershell
  uv run pytest tests/unit/exclusion_policy/test_enforcement.py tests/unit/api_runtime/test_small_file_sync_composition.py -q
  ```

- [x] Implement `AllowedPolicyRevisionBinding` beside `PolicyDecision` and export
  both it and `PublicationPolicyEvidence` from
  `personal_os.exclusion_policy.__init__`. The value must contain exactly the two
  fields shown above.

- [x] Change `PolicyEnforcementSmallFileGuard.authorize_small_file` to convert only
  the successful server decision:

  ```python
  decision = await self.enforcement.authorize_preflight(
      subject=subject,
      boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
      context=diagnostic_context,
  )
  return AllowedPolicyRevisionBinding(
      workspace_id=decision.workspace_id,
      policy_revision_number=decision.revision_number,
  )
  ```

- [x] Update the protocol docstring and all guard test doubles to return a valid
  binding. Give `OfflineSmallFileSyncState` an internal
  `active_policy_revision_number: int = 1`; the offline guard returns that
  server-owned state value and never copies `preflight.policy_revision_number`.
  Keep its existing denial knob behavior unchanged.

- [x] Run the focused tests and then the complete affected unit suites:

  ```powershell
  uv run pytest tests/unit/exclusion_policy tests/unit/api_runtime/test_small_file_sync_composition.py tests/unit/small_file_sync -q
  uv run mypy src/personal_os/exclusion_policy src/personal_os/small_file_sync apps/api/src/api_runtime/small_file_sync_composition.py
  uv run ruff check src/personal_os/exclusion_policy src/personal_os/small_file_sync apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/exclusion_policy tests/unit/api_runtime/test_small_file_sync_composition.py tests/unit/small_file_sync
  ```

- [x] Commit:

  ```powershell
  git add src/personal_os/exclusion_policy/enforcement.py src/personal_os/exclusion_policy/__init__.py src/personal_os/small_file_sync/ports.py apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/exclusion_policy/test_enforcement.py tests/unit/api_runtime/test_small_file_sync_composition.py tests/unit/small_file_sync/fakes.py
  git commit -m "feat: define allowed policy revision binding"
  ```

---

## Task 2: Persist and Atomically Rebind the Server Revision

**Files:**

- Modify: `src/personal_os/small_file_sync/ports.py`
- Modify: `src/personal_os/small_file_sync/service.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py`
- Modify: `apps/api/src/api_runtime/small_file_sync_composition.py`
- Modify: `tests/unit/small_file_sync/fakes.py`
- Modify: `tests/unit/small_file_sync/test_service.py`
- Modify: `tests/unit/postgresql_source_store/test_small_file_sync_operations.py`
- Modify: `tests/integration/source_publication/test_small_file_operations.py`

**Interfaces:**

```python
class SmallFileUploadOperationStore(Protocol):
    async def reserve_operation(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileUploadOperation:
        raise NotImplementedError
```

The SQL builders receive the primitive `policy_revision_number: int`; the port
and adapter boundary receives the typed binding and validates its workspace
before opening a transaction.

- [x] In `tests/unit/small_file_sync/test_service.py`, add
  `test_preflight_reserves_with_the_guard_binding_not_the_plugin_revision`.
  Configure the fake guard to return revision `7`, send plugin revision `2`, and
  assert the fake operation store recorded revision `7`.

- [x] In `tests/unit/postgresql_source_store/test_small_file_sync_operations.py`,
  add three focused tests:

  - `test_insert_binds_the_server_policy_revision`
  - `test_token_rotation_rebinds_policy_revision_without_changing_fingerprint`
  - `test_bound_row_comparison_includes_policy_revision`

  Compile each SQLAlchemy statement and assert the bound parameters contain the
  passed server revision. Assert `operation_fingerprint_matches` still returns
  true when only the client claim differs.

- [x] In `tests/integration/source_publication/test_small_file_operations.py`, add
  `test_successful_repreflight_rotates_token_and_rebinds_server_revision`.
  Reserve once with binding revision `4`, reserve the same pending identity
  again with binding revision `5` before expiry, and assert one row, a new token,
  unchanged reserved source ID, and row revision `5`.

- [x] Add `test_reservation_rejects_a_foreign_workspace_binding_before_sql` to the
  adapter unit suite. Instrument the fake engine/connection and assert it was not
  entered when binding and credential-derived workspaces differ.

- [x] Run the focused unit tests and confirm failures identify the old signature,
  plugin-owned insert value, rotation omission, and row-comparison omission:

  ```powershell
  uv run pytest tests/unit/small_file_sync/test_service.py tests/unit/postgresql_source_store/test_small_file_sync_operations.py -q
  ```

- [x] Capture the returned binding once in `_preflight_once` and pass it by keyword
  to reservation after replay/base/no-change checks:

  ```python
  policy_binding = await self.policy_guard.authorize_small_file(
      preflight,
      device_context,
      diagnostic_context,
  )
  operation = await self.operation_store.reserve_operation(
      preflight=preflight,
      device_context=device_context,
      policy_binding=policy_binding,
      diagnostic_context=diagnostic_context,
  )
  ```

- [x] Change every store implementation and fake to the new signature. Validate:

  ```python
  if policy_binding.workspace_id != device_context.workspace_id:
      raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
  ```

  Store the binding revision in an explicit offline/fake row field so
  `resolve_bound_operation` never reconstructs authority from the preflight
  request.

- [x] Extend the PostgreSQL insert and rotation statements so both paths use the
  server revision:

  ```python
  .values(policy_revision_number=policy_revision_number)
  ```

  The rotation update must set token hash, expiry, policy revision, and
  `updated_at` in the same statement for every pending re-preflight, expired or
  not. A `receiving` row is already claimed and is never reclaimed because of
  expiry: token and expiry do not rotate. This original no-revision-rebind
  wording is superseded only by the final-blocker addendum below: a successful
  locator-aware reauthorization of the same claimed identity may update its
  policy revision under the operation lock while preserving the exact token
  and every other bound field. Do not add policy revision to
  `operation_fingerprint_matches`.

- [x] Extend `_bound_matches_row` with:

  ```python
  and int(row.policy_revision_number) == bound.policy_revision_number
  ```

- [x] Update all existing reserve call sites with a test/server binding helper,
  then run the unit and disposable-PostgreSQL gates:

  ```powershell
  uv run pytest tests/unit/small_file_sync tests/unit/postgresql_source_store/test_small_file_sync_operations.py -q
  uv run pytest tests/integration/source_publication/test_small_file_operations.py -m local_stack -q
  uv run mypy src/personal_os/small_file_sync packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py apps/api/src/api_runtime/small_file_sync_composition.py
  ```

- [x] Commit:

  ```powershell
  git add src/personal_os/small_file_sync/ports.py src/personal_os/small_file_sync/service.py packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/small_file_sync/fakes.py tests/unit/small_file_sync/test_service.py tests/unit/postgresql_source_store/test_small_file_sync_operations.py tests/integration/source_publication/test_small_file_operations.py
  git commit -m "feat: bind upload operations to server policy revisions"
  ```

---

## Task 3: Add Bound Outer Publication Authorization

**Files:**

- Modify: `src/personal_os/exclusion_policy/enforcement.py`
- Modify: `tests/unit/exclusion_policy/test_enforcement.py`

**Interfaces:**

```python
async def authorize_bound_publication(
    self,
    command: SourceVersionCommand,
    binding: AllowedPolicyRevisionBinding,
    diagnostic_context: DiagnosticContext,
) -> PublicationPolicyEvidence:
    raise NotImplementedError
```

- [x] Add these tests to `tests/unit/exclusion_policy/test_enforcement.py`:

  - `test_bound_publication_returns_binding_without_evaluation_when_revision_matches`
  - `test_bound_publication_evaluates_the_current_revision_when_revision_changed`
  - `test_bound_publication_denies_changed_locator_rule_as_indeterminate`
  - `test_bound_publication_rejects_foreign_workspace_binding`
  - `test_bound_publication_fails_closed_when_active_snapshot_is_missing_or_invalid`

  In the equal-revision test, inject an evaluator spy or monkeypatch the module's
  evaluator and assert it was never called. Assert one low-cardinality `allowed`
  publication metric and no revision/locator labels. In the changed-revision
  evaluation test, use a fully decidable non-matching media-type or size rule and
  assert the returned current `PolicyDecision` allows publication; revision
  mismatch by itself is not an unconditional denial.

- [x] Run the new tests and confirm failure because the method does not exist:

  ```powershell
  uv run pytest tests/unit/exclusion_policy/test_enforcement.py -q
  ```

- [x] Extract the existing create/update publication-subject construction into a
  private async method used by both authorization paths. Do not change
  `authorize_publication`: it still loads the current snapshot and evaluates it
  unconditionally.

- [x] Implement the bound method in this order: workspace check, one active
  snapshot load, signed-material verification, revision comparison, then either
  binding return or current evaluation. Its core branch must have this shape:

  ```python
  started_monotonic = time.monotonic()
  material = await self._snapshot_source.load_active_snapshot(
      command.workspace_id,
      diagnostic_context,
  )
  if material is None:
      raise policy_not_initialized_error()
  revision = parse_verified_policy_revision(material, verifier=self._verifier)
  if revision.revision_number == binding.policy_revision_number:
      if self._metrics is not None:
          self._metrics.record_evaluation(
              boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
              decision=EvaluationMetricOutcome.ALLOWED,
              duration_seconds=max(time.monotonic() - started_monotonic, 0.0),
          )
      return binding

  subject = await self._publication_subject(command, diagnostic_context)
  decision = self._evaluate_material(
      material,
      subject,
      PolicyBoundary.SINGLE_PART_UPLOAD,
      started_monotonic,
  )
  enforce_policy_decision(decision)
  return decision
  ```

  Adapt the duration expression to the existing metrics clock contract; do not
  introduce revision, workspace, locator, or rule identifiers as labels.

- [x] Map foreign-workspace binding to an existing closed internal invariant
  error. The small-file gateway will normally reject it earlier; this method is
  a defense-in-depth boundary and must never silently substitute the command
  workspace.

- [x] Run the full exclusion-policy and source-publication unit suites:

  ```powershell
  uv run pytest tests/unit/exclusion_policy tests/unit/sources -q
  uv run mypy src/personal_os/exclusion_policy
  uv run ruff check src/personal_os/exclusion_policy tests/unit/exclusion_policy
  ```

- [x] Commit:

  ```powershell
  git add src/personal_os/exclusion_policy/enforcement.py tests/unit/exclusion_policy/test_enforcement.py
  git commit -m "feat: authorize bound publication revisions"
  ```

---

## Task 4: Preserve the Binding at the Transaction-Final Policy Lock

**Files:**

- Modify: `src/personal_os/sources/ports.py`
- Modify: `src/personal_os/sources/publication.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/policy_enforcement.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/publication_store.py`
- Modify: `apps/api/src/api_runtime/small_file_sync_composition.py`
- Modify: `tests/unit/sources/fakes.py`
- Modify: `tests/unit/sources/test_publication_service.py`
- Create: `tests/unit/postgresql_source_store/test_policy_enforcement.py`
- Create: `tests/unit/postgresql_source_store/test_publication_store.py`
- Modify: `tests/integration/exclusion_policy/test_source_publication_enforcement.py`

**Interfaces:**

```python
class PolicyEnforcementGuard(Protocol):
    async def authorize_publication(
        self,
        command: SourceVersionCommand,
        diagnostic_context: DiagnosticContext,
    ) -> PublicationPolicyEvidence:
        raise NotImplementedError
```

Both `SourcePublicationStore.commit_create` and `commit_update` change only the
type of `preflight_decision` to `PublicationPolicyEvidence | None`; the keyword
name remains stable to avoid unrelated churn.

```python
async def authorize_locked_publication_policy(
    connection: AsyncConnection,
    command: SourceVersionCommand,
    subject: PolicySubject,
    policy_evidence: PublicationPolicyEvidence | None,
    verifier: PolicySignatureVerifier,
    metrics: ExclusionPolicyMetrics | None,
) -> PublicationPolicyEvidence:
    raise NotImplementedError
```

- [x] Add a source-service unit test
  `test_bound_policy_evidence_flows_to_the_commit_unchanged`. Configure the fake
  guard to return `AllowedPolicyRevisionBinding`, publish, and assert the fake
  store received the identical object.

- [x] Add locked-helper/store unit tests proving:

  - matching binding + verified locked revision returns the binding and never
    invokes `evaluate_policy`;
  - changed binding revision evaluates the supplied authoritative subject;
  - ordinary `PolicyDecision` still evaluates even when revision numbers match;
  - foreign-workspace binding fails before source mutation;
  - no active snapshot, invalid signature, and connection failure fail closed.

- [x] Add two real-PostgreSQL tests to
  `tests/integration/exclusion_policy/test_source_publication_enforcement.py`:

  - `test_matching_bound_revision_commits_despite_locator_only_rule`
  - `test_changed_bound_revision_rechecks_and_rolls_back_locator_only_rule`

  The first publishes a signed active locator rule, passes a matching binding,
  and proves the commit succeeds although a locator-free evaluator would be
  indeterminate. The second advances the active revision after creating the
  binding and proves `exclusion_policy_indeterminate` plus zero source/version
  mutation. Keep the existing ordinary-decision policy-change test unchanged.

- [x] Run the new unit tests and confirm they fail on the current
  `PolicyDecision`-only types and unconditional evaluator:

  ```powershell
  uv run pytest tests/unit/sources/test_publication_service.py tests/unit/postgresql_source_store/test_policy_enforcement.py tests/unit/postgresql_source_store/test_publication_store.py -q
  ```

- [x] Widen the source ports, publication-service local evidence type, store
  signatures, and fakes to `PublicationPolicyEvidence`. Do not change the
  publication service's ordering: policy guard, committed lookup, object
  resolution/store, commit.

- [x] Implement `authorize_locked_publication_policy` by reusing
  `load_locked_active_policy_snapshot` and `parse_verified_policy_revision`.
  The decision table must be explicit:

  ```python
  started_monotonic = time.monotonic()
  material = await load_locked_active_policy_snapshot(
      connection,
      command.workspace_id,
  )
  revision = parse_verified_policy_revision(material, verifier=verifier)
  if isinstance(policy_evidence, AllowedPolicyRevisionBinding):
      if policy_evidence.workspace_id != command.workspace_id:
          raise SourcePublicationError(
              ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
              safe_details={"source_id": command.source_id},
          )
      if revision.revision_number == policy_evidence.policy_revision_number:
          if metrics is not None:
              metrics.record_evaluation(
                  boundary=PolicyBoundary.SOURCE_CREATE_UPDATE,
                  decision=EvaluationMetricOutcome.ALLOWED,
                  duration_seconds=max(time.monotonic() - started_monotonic, 0.0),
              )
          return policy_evidence

  decision = evaluate_policy_decision(
      revision=revision,
      subject=subject,
      evaluated_at=datetime.now(UTC),
  )
  if metrics is not None:
      record_evaluation_metric(
          metrics,
          boundary=PolicyBoundary.SOURCE_CREATE_UPDATE,
          decision=decision,
          duration_seconds=max(time.monotonic() - started_monotonic, 0.0),
      )
  enforce_policy_decision(decision)
  return decision
  ```

  Use the repository's exact error constructor and existing metric helper
  signatures. A `None` evidence value and ordinary `PolicyDecision` both follow
  the unconditional evaluation branch.

- [x] Replace the publication store's inline unconditional policy block with the
  helper only after idempotency locks, workspace/actor checks, and locked replay
  resolution. Keep `workspace_policy_state FOR UPDATE` in the existing global
  lock order and build the authoritative subject from command plus verified
  receipt before calling the helper.

- [x] Run all affected unit and integration suites:

  ```powershell
  uv run pytest tests/unit/sources tests/unit/postgresql_source_store/test_policy_enforcement.py tests/unit/postgresql_source_store/test_publication_store.py -q
  uv run pytest tests/integration/exclusion_policy/test_source_publication_enforcement.py -m local_stack -q
  uv run mypy src/personal_os/sources packages/postgresql-source-store/src/postgresql_source_store
  ```

- [x] Commit:

  ```powershell
  git add src/personal_os/sources/ports.py src/personal_os/sources/publication.py packages/postgresql-source-store/src/postgresql_source_store/policy_enforcement.py packages/postgresql-source-store/src/postgresql_source_store/publication_store.py apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/sources/fakes.py tests/unit/sources/test_publication_service.py tests/unit/postgresql_source_store/test_policy_enforcement.py tests/unit/postgresql_source_store/test_publication_store.py tests/integration/exclusion_policy/test_source_publication_enforcement.py
  git commit -m "feat: honor bound policy at publication commit"
  ```

---

## Task 5: Compose an Invocation-Local Small-File Publication Gateway

**Files:**

- Modify: `src/personal_os/small_file_sync/ports.py`
- Modify: `src/personal_os/small_file_sync/service.py`
- Modify: `apps/api/src/api_runtime/small_file_sync_composition.py`
- Modify: `tests/unit/small_file_sync/fakes.py`
- Modify: `tests/unit/small_file_sync/test_service.py`
- Modify: `tests/unit/api_runtime/test_small_file_sync_composition.py`
- Modify: `tests/integration/small_file_sync/conftest.py`

**Interfaces:**

```python
class SmallFilePublicationGateway(Protocol):
    async def publish_create(
        self,
        *,
        command: CreateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        raise NotImplementedError

    async def publish_update(
        self,
        *,
        command: UpdateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        raise NotImplementedError
```

```python
@dataclass(frozen=True, slots=True)
class BoundPolicySmallFilePublicationGateway:
    store: SourcePublicationStore
    object_store: CanonicalObjectStore
    metrics: SourcePublicationMetrics
    clock: SourceAwareUtcClock
    enforcement: PolicyEnforcementService
```

- [x] Replace the fake concrete publication service with a fake
  `SmallFilePublicationGateway` and add service tests:

  - `test_receive_reconstructs_binding_only_from_the_bound_operation`
  - `test_receive_rejects_binding_workspace_mismatch_before_gateway`
  - `test_concurrent_receives_keep_their_policy_bindings_isolated`

  For the concurrency test, use two `asyncio.Event` barriers inside the fake
  gateway. Start receives bound to revisions `11` and `12`, release them in
  reverse order, and assert each call retained its own revision and produced one
  terminal result.

- [x] Add composition tests:

  - `test_serve_composition_binds_the_bound_policy_publication_gateway`
  - `test_gateway_builds_a_fresh_immutable_guard_for_each_invocation`
  - `test_concurrent_gateway_calls_do_not_share_bound_evidence`

  Inspect the service graph without relying on private mutable state. Prove two
  simultaneous calls reach the enforcement fake with their respective bindings.

- [x] Run the focused tests and confirm they fail because
  `SmallFileSyncService` still owns a concrete `SourceVersionPublicationService`:

  ```powershell
  uv run pytest tests/unit/small_file_sync/test_service.py tests/unit/api_runtime/test_small_file_sync_composition.py -q
  ```

- [x] Add `SmallFilePublicationGateway` to `small_file_sync.ports`. Change the
  service field from `publication_service` to `publication_gateway`. In
  `_publish`, reconstruct the immutable binding only from the resolved row:

  ```python
  policy_binding = AllowedPolicyRevisionBinding(
      workspace_id=bound.workspace_id,
      policy_revision_number=bound.policy_revision_number,
  )
  if command.workspace_id != device_context.workspace_id:
      raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
  if policy_binding.workspace_id != device_context.workspace_id:
      raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
  ```

  Pass the binding by keyword to the gateway create/update method. Do not read
  the receive request, plugin revision, or preflight object for authorization.

- [x] Add a private frozen invocation guard in the composition root:

  ```python
  @dataclass(frozen=True, slots=True)
  class _BoundPolicyPublicationGuard:
      enforcement: PolicyEnforcementService
      binding: AllowedPolicyRevisionBinding

      async def authorize_publication(
          self,
          command: SourceVersionCommand,
          diagnostic_context: DiagnosticContext,
      ) -> PublicationPolicyEvidence:
          return await self.enforcement.authorize_bound_publication(
              command,
              self.binding,
              diagnostic_context,
          )
  ```

- [x] Implement each gateway method by validating workspace equality, creating a
  fresh `_BoundPolicyPublicationGuard`, creating a fresh lightweight
  `SourceVersionPublicationService` over the shared adapters, and immediately
  invoking its matching publish method. There must be no setter and no stored
  “current” binding:

  ```python
  publication_service = SourceVersionPublicationService(
      store=self.store,
      object_store=self.object_store,
      metrics=self.metrics,
      clock=self.clock,
      policy_guard=_BoundPolicyPublicationGuard(
          enforcement=self.enforcement,
          binding=policy_binding,
      ),
  )
  return await publication_service.publish_create(
      command=command,
      stream=stream,
      diagnostic_context=diagnostic_context,
  )
  ```

- [x] Update real, offline, and policy-test compositions to inject a gateway.
  The offline gateway may use a deterministic allowing publication guard, but it
  must still accept and forward the invocation's binding and must not derive it
  from the plugin request. Export `BoundPolicySmallFilePublicationGateway` from
  the composition module for the integration harness; keep the invocation guard
  private.

- [x] Run focused suites plus boundary and strict-type checks:

  ```powershell
  uv run pytest tests/unit/small_file_sync tests/unit/api_runtime/test_small_file_sync_composition.py tests/unit/sources -q
  uv run poe boundary-check
  uv run mypy src/personal_os/small_file_sync apps/api/src/api_runtime/small_file_sync_composition.py
  uv run ruff check src/personal_os/small_file_sync apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/small_file_sync tests/unit/api_runtime/test_small_file_sync_composition.py
  ```

- [x] Commit:

  ```powershell
  git add src/personal_os/small_file_sync/ports.py src/personal_os/small_file_sync/service.py apps/api/src/api_runtime/small_file_sync_composition.py tests/unit/small_file_sync/fakes.py tests/unit/small_file_sync/test_service.py tests/unit/api_runtime/test_small_file_sync_composition.py tests/integration/small_file_sync/conftest.py
  git commit -m "refactor: add bound small file publication gateway"
  ```

---

## Task 6: Prove the Wire Journey, Policy Change, Isolation, and Fail-Closed Paths

**Files:**

- Modify: `tests/integration/small_file_sync/conftest.py`
- Modify: `tests/integration/small_file_sync/test_policy_and_device_boundaries.py`
- Modify: `tests/integration/source_publication/test_small_file_operations.py`
- Modify: `tests/integration/exclusion_policy/test_source_publication_enforcement.py`
- Modify only if assertion coverage is missing:
  `apps/obsidian-plugin/src/journal/journal-sync-journey.test.ts`
- Modify only if assertion coverage is missing:
  `apps/obsidian-plugin/test/specs/device-login-sync.e2e.ts`

**Interfaces:** No new production interface is introduced in this task. The
integration harness must implement the Task 1-5 interfaces exactly and must not
add test-only branches to production services.

**Acceptance cases:**

1. Active locator rule excludes `.tmp` but allows `.md`; an `.md` journal event
   preflights and uploads exactly once, then the journal is committed.
2. Plugin revision claim differs from the server revision; the operation row and
   receive binding use the server revision.
3. A new locator-dependent revision between preflight and receive denies or is
   indeterminate, publishes nothing, and the next same-identity preflight returns
   `excluded`.
4. Two concurrent receives with different bound revisions cannot contaminate one
   another.
5. Active-policy load/verification/database failure at outer or locked guard
   publishes no canonical row and returns an existing closed error.
6. Exact replay, no-change, size mismatch, media mismatch, expired token, revoked
   device, and ordinary source-publication behavior remain unchanged.

- [x] First update `policy_wire_harness` to mirror production exactly: real
  `PolicyEnforcementSmallFileGuard`, real
  `BoundPolicySmallFilePublicationGateway`, mutable signed snapshot source, and
  an in-memory publication-store double that consumes
  `PublicationPolicyEvidence` with the same matching-binding/changed-revision
  decision table. Do not make the harness a shortcut around either guard.

- [x] Add `excluding_extension_rule(extension: str)` beside the existing folder
  and size helpers. Construct it through
  `normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=extension)` so the
  acceptance test exercises the canonical normalization contract.

- [x] Replace the current locator-rule regression with
  `test_matching_preflight_revision_publishes_locator_allowed_markdown_once`.
  Publish a `.tmp` exclusion rule, send a `.md` path, assert upload `200`, one
  publication commit, one source ID, and exact replay without a second commit.

- [x] Keep and tighten
  `test_locator_rule_published_during_the_upload_fails_closed_at_publication`:
  assert changed revision, `exclusion_policy_indeterminate`, zero publication,
  and same-identity next preflight `excluded`.

- [x] Add the plugin-claim disagreement assertion at the PostgreSQL operation
  integration boundary, not only in a fake: query
  `small_file_upload_operations.policy_revision_number` and prove it equals the
  server binding.

- [x] Add a deterministic concurrent wire/service test with barriers around the
  active-snapshot load or commit seam. Assert each receive uses its own immutable
  binding and that scheduling order does not change the result.

- [x] Add outer and locked policy-source failure tests. Use one load failure and
  one invalid signature/database error representative; assert existing error
  codes, `Cache-Control: no-store` at HTTP, and zero canonical source/version
  rows. Do not assert raw driver/signature text.

- [x] Run the focused Python acceptance suites:

  ```powershell
  uv run pytest tests/unit/small_file_sync tests/unit/exclusion_policy tests/unit/sources tests/unit/postgresql_source_store -q
  uv run pytest tests/integration/small_file_sync -q
  uv run pytest tests/integration/source_publication/test_small_file_operations.py tests/integration/exclusion_policy/test_source_publication_enforcement.py -m local_stack -q
  ```

- [x] Run the real plugin unit journey. Change the TypeScript test only if it
  does not already assert one upload followed by a committed journal state:

  ```powershell
  pnpm --filter @workspace/obsidian-plugin exec vitest run src/journal/journal-sync-journey.test.ts
  ```

- [x] Run the real Obsidian device-login/sync journey in its isolated WebdriverIO
  environment. If the environment lacks the Obsidian binary/display prerequisite,
  record the exact external prerequisite in the handoff; do not replace this gate
  with a mock:

  ```powershell
  pnpm --filter @workspace/obsidian-plugin exec wdio run ./wdio.conf.mts --spec ./test/specs/device-login-sync.e2e.ts
  ```

- [x] Prove public artifacts and schema stayed unchanged:

  ```powershell
  uv run poe api-contract-check
  uv run pytest tests/unit/migrations/test_small_file_sync_migration.py tests/contract/small_file_sync tests/contract/api/test_small_file_sync_routes.py tests/contract/api/test_small_file_sync_openapi.py -q
  git diff --exit-code 2035e3a..HEAD -- packages/api-client/openapi.json packages/api-client/src/generated
  git diff --exit-code 2035e3a..HEAD -- migrations/versions/20260818_01_add_small_file_sync_operations.py packages/postgresql-source-store/src/postgresql_source_store/tables.py
  ```

- [x] Run the repo regression gates and read every result before marking this task
  complete:

  ```powershell
  uv run poe exclusion-policy-test
  uv run poe canonical-core-test
  uv run poe verify
  ```

- [x] Commit acceptance-test changes only after every available gate passes:

  ```powershell
  git add tests/integration/small_file_sync/conftest.py tests/integration/small_file_sync/test_policy_and_device_boundaries.py tests/integration/source_publication/test_small_file_operations.py tests/integration/exclusion_policy/test_source_publication_enforcement.py apps/obsidian-plugin/src/journal/journal-sync-journey.test.ts apps/obsidian-plugin/test/specs/device-login-sync.e2e.ts
  git commit -m "test: cover bound small file publication policy"
  ```

---

## Task 7: Update Canonical Operations Documentation and Write One Handoff

**Files:**

- Modify: `docs/operations/plugin-journal-small-file-sync.md`
- Modify: `docs/handoff/BACKLOG.md`
- Create: `docs/handoff/2026-08-19-publication-policy-decision-binding.md`

**Interfaces:** No code interface changes. This task updates the living operator
contract, the deferred-work index, and the single required handoff snapshot.

- [x] Update the runbook's policy flow to state that preflight persists the
  server-returned allowed revision, same-revision publication reuses that
  invariant after verifying the signed active snapshot, and changed revisions
  re-evaluate fail-closed. Keep the next-preflight self-healing procedure.

- [x] Remove exactly the publication locator-gap line from
  `docs/handoff/BACKLOG.md`. Preserve the separate verifier-chain/signing-key
  rotation item and every unrelated deferred item.

- [x] Create exactly one handoff snapshot with these sections:

  ```markdown
  # Publication Policy Decision Binding Handoff

  ## Final commit
  ## Gate evidence
  ## Spec interpretations and rationale
  ## Deferred items and verdicts
  ## Canonical documentation links
  ## Next actions
  ```

  Record the final implementation commit SHA; command, exit code, and concise
  result for every Task 6 gate; the immutable-binding and transaction-lock
  decisions; any unavailable physical/E2E prerequisite; and links to the living
  spec/runbook. Keep it below roughly 400 lines.

- [x] Verify documentation references, naming, sensitive-token absence, and the
  one-handoff rule:

  ```powershell
  rg -n "publication|policy_revision|AllowedPolicyRevisionBinding" docs/operations/plugin-journal-small-file-sync.md docs/handoff/2026-08-19-publication-policy-decision-binding.md
  rg -n "publication.*locator|locator.*publication" docs/handoff/BACKLOG.md
  rg -n "TODO|TBD|PLACEHOLDER|raw content|access token|refresh token" docs/operations/plugin-journal-small-file-sync.md docs/handoff/2026-08-19-publication-policy-decision-binding.md
  (Get-Content docs/handoff/2026-08-19-publication-policy-decision-binding.md).Count
  git status --short
  git diff --check
  ```

- [x] Inspect the complete implementation diff and confirm no public schema,
  migration, dependency, architecture, or unrelated user file changed:

  ```powershell
  git diff --stat 2035e3a..HEAD
  git diff 2035e3a..HEAD -- src apps packages tests docs
  git status --short
  ```

- [x] Commit the documentation and handoff:

  ```powershell
  git add docs/operations/plugin-journal-small-file-sync.md docs/handoff/BACKLOG.md docs/handoff/2026-08-19-publication-policy-decision-binding.md
  git commit -m "docs: hand off publication policy binding"
  ```

- [x] Re-run `git status --short` and require an empty worktree before branch
  integration or cleanup.

## Final-review completion addendum

The whole-branch review adds four completion gates without changing the public
wire, schema, fingerprint, or invocation-local binding architecture:

- [x] Prove in a unit seam and real PostgreSQL that `pending -> receiving` is an
  ownership claim. Advance time beyond `expires_at`, prove expiry alone cannot
  reclaim or rotate token/expiry, allow successful locator-aware
  reauthorization to update only the policy revision, allow exact-token resume,
  and allow the guarded `receiving -> committed` transition after expiry.
- [x] Change `stack reset --rotate-secrets` to delete and rebootstrap only the
  managed stack-secret filenames. Preserve every allowlisted application file
  byte-for-byte and prove a complete reset, rebootstrap, and changed smoke
  fingerprint with the fake runner.
- [x] Extend the real Obsidian journey with sanitized PostgreSQL evidence that
  one operation joins exactly one canonical source version and sync event. Then
  open a read-only operation observer, publish a locator-dependent denying
  revision after preflight and during the real content request, prove no
  terminal operation result, preserve one durable nonterminal journal event,
  and prove it reaches `excluded_policy` on the next preflight after a real
  plugin reload.
- [x] Re-run focused unit/integration/contract/static gates, the real live WDIO
  journey through the existing Cloudflare Tunnel, `exclusion-policy-test`,
  `canonical-core-test`, `verify`, artifact/schema diffs, `git diff --check`,
  and final status inspection. Record fresh evidence in the existing single
  handoff; do not create another handoff.

## Claimed-upload resume final-review addendum

- [x] Observe interrupted-after-claim behavior with one durable nonterminal
  event. The final live proof makes interruption deterministic by disabling the
  plugin after the revision changes (so it does not depend on whether the row
  has advanced from `uploading` to `waiting_retry`), then proves the next pass
  resumes exactly the unchanged persisted token and produces one terminal
  publication.
- [x] Prove that an unknown operation, a token replaced during preflight, and a
  successful policy-change exclusion cannot enter claimed-token resume.
- [x] Make the live database observer wait for the fixture-scoped
  `receiving`/unpublished row before publishing the policy-race revision, while
  emitting sanitized counts only.
- [x] Extend the mandatory real journey with a deterministic plugin
  interruption, an irrelevant `folder_prefix` revision, internal equality of
  the pre/post opaque operation identity (never logged), and fixture-scoped
  proof of exactly one canonical publication and one terminal receipt.
- [x] Reject offline terminalization when any bound field differs, including
  the allowed policy revision, matching the PostgreSQL fence.
- [x] Run focused and regression unit/integration/contract/static/build gates,
  the real WDIO journey, artifact/schema diffs, and final diff checks; record
  both successful evidence and later external live-run concerns honestly.

## Coverage Matrix

| Spec requirement | Primary task | Proof |
|---|---:|---|
| Immutable allowed binding, no sensitive payload | 1 | Value geometry and export tests |
| Plugin claim is not authority | 1, 2, 6 | Guard, row, and wire disagreement tests |
| New insert and every pending re-preflight use server revision | 2 | SQL unit + PostgreSQL row tests |
| Claimed receive retains token/revision and may terminalize after expiry | Final-review addendum | Unit + deterministic PostgreSQL expiry race |
| Fingerprint remains revision-free | 2 | Existing/new fingerprint regression |
| Bound terminal transition checks revision | 2 | `_bound_matches_row` unit test |
| Same verified active revision skips locator-free evaluator | 3, 4 | Outer spy + real locked commit test |
| Changed revision evaluates current policy fail-closed | 3, 4, 6 | Unit, PostgreSQL, wire tests |
| Ordinary publication callers retain unconditional recheck | 4 | Ordinary `PolicyDecision` regression |
| Explicit invocation-local gateway | 5 | Composition and concurrency tests |
| No cross-request binding contamination | 5, 6 | Barrier-controlled concurrent receives |
| No migration/API/client change | 6 | Artifact, migration, contract gates |
| One successful `.md` publish under locator rule | 6 | Wire + real plugin journeys |
| Backlog item closed only after evidence | 7 | Gate table and targeted backlog diff |

## Final-blocker completion addendum

- [x] Prove RED for locator-allowed re-preflight followed by an unchanged
  exact-token PUT that remains indeterminate under the row's old revision.
- [x] Synchronously reauthorize only a matching `receiving` row under the
  operation lock, preserving its exact token and every non-policy bound field.
- [x] Fence claimed source publication with that same operation lock and write
  canonical publication plus the matching terminal operation result in one
  PostgreSQL transaction; prove both race orders and exactly-once replay.
- [x] Reproduce application/database clock skew through the real publication
  service, remove receipt-time test fudging, and bind the first content-object
  row's creation/verification timestamps deterministically.
- [x] Preserve the historical nine-count `canonical_core_backup/v1` reader and
  the exact branch-local twenty-count v2 shape, emit the complete 28-count
  graph as current v2, and prove v1/legacy-v2 restore compatibility plus the
  required current-v2 rebackup path. V2 is strengthened in place because it
  never escaped this branch.
- [x] Document managed versus preserved local secrets, dynamic reference
  grammar/collisions, and exact reset/bootstrap results in canonical docs.
- [x] Bound the live evidence subprocess, PostgreSQL connect, statement, lock,
  and receiving-observer waits without exposing sensitive operands.
- [x] Run the complete real WDIO final artifact through the existing runbook
  and tunnel with no HTTP 500, then run focused, regression, strict static,
  build, artifact, migration, and diff gates on the final commit.

## Completion Criteria

- [x] Every task commit exists in order and contains only its named scope.
- [x] All focused tests were observed failing before their implementation and
  passing afterward.
- [x] Unit, integration, contract, type, lint, boundary, build, and available
  real-client gates have fresh recorded evidence.
- [x] `SourceVersionPublicationService` remains the publication orchestrator;
  PostgreSQL remains the canonical transaction-final authority.
- [x] No public API, generated client, table, migration, production dependency,
  error code, or telemetry label was added.
- [x] Exactly one implementation handoff exists and the publication-gap backlog
  line is removed only after all required gates pass.
- [x] `git diff --check` passes and the final worktree is clean.
