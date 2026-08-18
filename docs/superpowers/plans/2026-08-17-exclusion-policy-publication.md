# Exclusion Policy Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver child 3's deny-only exclusion-policy control plane: immutable PostgreSQL revisions, asynchronous exact-draft preview, signed Ed25519 snapshots, backend fail-closed enforcement, optional Obsidian cache/verification, reconciliation and acceptance evidence without implementing synchronization or projection consumers.

**Architecture:** Framework-neutral rule normalization, evaluation, signed-payload contracts and orchestration live under `personal_os.exclusion_policy`. PostgreSQL owns drafts, immutable revisions, active state, preview/evaluation evidence, key history, idempotency, audit and reconciliation intents. FastAPI exposes authenticated Admin and device-policy routes; Temporal executes preview and reconciliation; Web provides the Admin editor; the Obsidian plugin independently verifies the keyset/snapshot and evaluates local candidates. Every canonical source boundary re-evaluates against the active revision, treating missing subject fields or unverifiable policy material as deny.

**Tech Stack:** Python 3.14.6, FastAPI 0.139.2, Pydantic 2.13.4, SQLAlchemy 2.0.51 async Core, psycopg 3.3.4, Alembic 1.18.5, PostgreSQL 18.4, cryptography 49.0.0, Temporal Python SDK 1.30.0, Next.js 16.3.0, React 19.2.8, TypeScript 6.0.3 strict, Obsidian API 1.13.1, `@noble/ed25519` 3.1.0, Vitest 4.1.10 and Playwright 1.62.1.

**Normative spec:** `docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md` at commit `1e7f270`. Section references below refer to that document. The Phase 1 canonical-core implementation and child 2 authentication plan must already be complete.

## Global Constraints

- Implement child 3 only. Do not add Vault synchronization, source discovery/watchers, upload APIs, conflict resolution, metadata/AI rules, projection consumers, Qdrant/Neo4j writes, Cloudflare Worker routing or retention/pruning.
- PostgreSQL is authoritative. R2 keeps immutable canonical bytes; Redis never decides policy; Temporal may retry work but cannot publish or activate a revision outside the PostgreSQL transaction.
- Domain code under `src/personal_os/exclusion_policy/` must not import FastAPI, Starlette, SQLAlchemy, psycopg, Temporal, cryptography, Obsidian, React or provider SDKs.
- Rules are deny-only with default allow. Raw evaluation is exactly `allowed | excluded | indeterminate`; enforcement maps `indeterminate` to deny. Never silently ignore malformed, unsupported or missing operands.
- Support exactly `exact_source_id`, `folder_prefix`, `path_glob`, `extension`, `media_type`, `maximum_size` and `source_type`. Preserve normalized Unicode locator semantics and the bounded glob grammar from spec section 6.
- An empty policy is valid but must be explicitly published and signed. There is no implicit revision zero, unsigned fallback, permissive startup fallback or snapshot TTL.
- Pin `@noble/ed25519==3.1.0` exactly. It is the only new production dependency. Use `verifyAsync` with WebCrypto SHA-512; do not add a generic JCS or hashing dependency unless a reviewed compatibility test proves the platform requires one.
- Implement a closed RFC 8785-compatible canonical encoder for the fixed snapshot/keyset schemas only: ASCII member names, integers, booleans, null, arrays and normalized valid Unicode strings; reject floats, non-normalized strings, duplicate semantic fields and unknown payload types.
- Snapshot and keyset signatures use Ed25519 RFC 8032 over the exact domain-separated bytes in spec sections 12 and 13. Private keys are exact secret files, never database values, settings values, API payloads, logs, traces, metrics, audit details or backup content.
- Initial plugin trust is keyset revision 1 fetched only immediately after authenticated device onboarding over the configured HTTPS origin. Later rotation is accepted only through a valid cross-signed keyset chain; rollback, unknown key, invalid signature, workspace mismatch and malformed canonical bytes preserve the prior valid plugin cache and deny the candidate update.
- Persisted times and ordering use PostgreSQL transaction time and monotonic database sequences/checkpoints. Application clocks are only for deadlines, metrics and expiry input validation.
- Source publication uses the global row-lock order `publication idempotency advisory lock -> workspace_policy_state row -> source row`. Policy publication uses `policy idempotency advisory lock -> workspace_policy_state row`. Reconciliation never holds a policy-state lock while acquiring source rows. Do not introduce any inverse order.
- Source upload preflight evaluates before R2 I/O; the final PostgreSQL publication transaction locks policy state and evaluates again before changing `current_version_id`. A policy change during upload therefore fails closed without publishing the source version; immutable deduplicated R2 bytes may remain unreferenced for later cleanup.
- `projection_intents` gains exactly one origin: `source_event` with non-null `event_id`, or `policy_transition` with non-null `policy_revision_id`. The existing source dispatcher must claim only `source_event`; pending policy-transition intents must never start `SourceIngestionWorkflow`.
- Only sources with a non-null current version receive policy-transition projection intents. The later projection child owns the actual transition workflow; this child persists and safely isolates those intents.
- Preview executes one `REPEATABLE READ` PostgreSQL transaction in one Temporal activity, verifies the draft/revision/checkpoint at start, streams 500-row batches, heartbeats progress and atomically writes the complete result. It never merges batches from different snapshots.
- A ready preview expires after 15 minutes and publish requires exact draft version, active revision and source-event checkpoint equality. Preview API pages are at most 200 rows.
- Hard limits are 256 rules and 256 KiB signed snapshot bytes. Gates are evaluator p95 <= 5 ms per subject, signature verification p95 <= 50 ms, 10,000-source preview <= 30 seconds and 10,000-source reconciliation <= 5 minutes on the documented reference host.
- Admin routes require the existing Web session, exact Origin, CSRF and recent authentication where specified. Plugin routes require the existing `obsidian_sync` access credential and derive workspace/device from authentication context, never request input.
- Publication confirmation is exactly `PUBLISH EXCLUSION POLICY`. Its printable opaque idempotency key is 1–200 characters and remains outside the request fingerprint.
- Extend the registry with exactly `exclusion_policy_input_invalid`, `exclusion_policy_not_initialized`, `exclusion_policy_draft_conflict`, `exclusion_policy_preview_pending`, `exclusion_policy_preview_failed`, `exclusion_policy_preview_expired`, `exclusion_policy_preview_stale`, `exclusion_policy_confirmation_invalid`, `exclusion_policy_denied`, `exclusion_policy_indeterminate`, `exclusion_policy_snapshot_outdated`, `exclusion_policy_signing_unavailable` and `exclusion_policy_commit_outcome_unknown`, using the HTTP/retry/safe-detail mapping in spec section 19.
- Every external I/O has timeout, bounded retry, typed error mapping and structural diagnostics. Never log raw locator, path, filename, content, rule operand, snapshot/keyset bytes, signature, public/private key material, query, vector, token or secret.
- Python remains fully typed under mypy strict; TypeScript remains strict. Preserve envelopes, semantic `operationId`, closed error mappings, no trailing-slash redirects and deterministic OpenAPI/generated-client checks.
- For every behavior: write the named failing test, run it and inspect the expected failure, add the smallest implementation, rerun focused tests, then run affected lint/type/contract gates before committing.

---

## Dependency and Failure Decisions

| Role | Exact package or implementation | Runtime impact | Failure boundary |
|---|---|---|---|
| Python Ed25519 | Existing `cryptography==49.0.0` | API/key CLI only | invalid key size, signature or key-file access fails closed with a typed policy/configuration error; provider text never crosses the boundary |
| Plugin Ed25519 | `@noble/ed25519==3.1.0` | bundled into Obsidian plugin; zero runtime dependencies | `verifyAsync` rejection preserves the previous valid cache and reports only a closed reason |
| Canonical JSON | repository-owned closed encoder | Python and plugin, fixed signed schemas only | unsupported value, non-NFC string, float or unknown field is rejected before signing/acceptance |
| Durable orchestration | Existing `temporalio==1.30.0` | worker only | activity retry is idempotent; database transaction rollback prevents partial preview/publication evidence |
| Browser E2E | Existing `@playwright/test==1.62.1` | development only | acceptance gate fails rather than skipping when browser/runtime is absent |

Before any `-m local_stack`, integration or feature-gate command in a PowerShell implementation session, bind one disposable guarded identity for that session:

```powershell
$env:CI = "true"
$env:LOCAL_STACK_TEST_PROJECT = "knowledge-ci-exclusion-$PID"
```

The test fixture must validate this exact bounded project name, reset only its labeled resources and clean it in `finally`. Never point these commands at the operator project `knowledge-local`.

## File Structure

### Framework-neutral policy domain

```text
src/personal_os/exclusion_policy/
├── __init__.py
├── contracts.py                 Closed rule, subject, decision, snapshot and keyset values
├── normalization.py             NFC locator/rule normalization and bounded glob compilation
├── evaluation.py                Pure deny-only evaluator and safe decision evidence
├── canonical_json.py            Closed RFC 8785-compatible canonical encoder
├── signatures.py                Domain-separated message construction and crypto ports
├── ports.py                     Draft, preview, publication, query and reconciliation protocols
├── drafts.py                    Draft mutation and exact-version validation
├── previews.py                  Preview orchestration and stable result contracts
├── publication.py               Publish/idempotency orchestration and ambiguous-commit recovery
├── enforcement.py               Preflight/final/read fail-closed policy guard
├── reconciliation.py            Active-revision evaluation and transition intent planning
├── errors.py                    Policy-domain typed errors
└── metrics.py                   Closed low-cardinality metrics
```

### PostgreSQL, API and worker adapters

```text
migrations/versions/20260817_01_add_exclusion_policy_publication.py

packages/postgresql-source-store/src/postgresql_source_store/
├── tables.py
├── policy_drafts.py
├── policy_keysets.py
├── policy_previews.py
├── policy_publication.py
├── policy_enforcement.py
├── policy_reconciliation.py
├── publication_store.py
├── projection_intents.py
└── backup_snapshot.py

apps/api/src/api_runtime/
├── exclusion_policy_settings.py
├── exclusion_policy_crypto.py
├── exclusion_policy_commands.py
├── exclusion_policy_composition.py
├── exclusion_policy_models.py
├── exclusion_policy_routes.py
├── application.py
├── server.py
└── openapi_export.py

apps/worker/src/workflow_worker/
├── policy_preview_workflow.py
├── policy_reconciliation_workflow.py
├── policy_workflow_runtime.py
├── projection_dispatch_runtime.py
└── command.py
```

### Web, plugin and verification

```text
apps/web/src/
├── api/exclusion-policy-client.ts
├── features/exclusion-policy/policy-models.ts
├── features/exclusion-policy/PolicyEditor.tsx
├── features/exclusion-policy/PolicyPreview.tsx
├── features/exclusion-policy/PolicyPublishDialog.tsx
├── features/exclusion-policy/PolicyStatus.tsx
└── app/admin/policy/page.tsx

apps/obsidian-plugin/src/exclusion-policy/
├── contracts.ts
├── strict-json.ts
├── canonical-json.ts
├── evaluator.ts
├── keyset.ts
├── snapshot.ts
├── policy-cache.ts
└── policy-session.ts

tests/fixtures/exclusion_policy/
├── evaluator-golden.json
├── snapshot-golden.json
└── keyset-golden.json

tests/unit/exclusion_policy/
tests/unit/api_runtime/
tests/unit/postgresql_source_store/
tests/unit/workflow_worker/
tests/contract/api/
tests/contract/exclusion_policy/
tests/integration/exclusion_policy/
tests/end_to_end/exclusion_policy/
tests/performance/test_exclusion_policy_performance.py
docs/operations/exclusion-policy-publication.md
docs/handoff/2026-08-17-exclusion-policy-publication.md
```

---

### Task 1: Add closed rule contracts, normalization and the pure evaluator

**Files:**
- Create: `src/personal_os/exclusion_policy/__init__.py`, `contracts.py`, `normalization.py`, `evaluation.py`, `errors.py`, `metrics.py`
- Create: `tests/fixtures/exclusion_policy/evaluator-golden.json`
- Create: `tests/unit/exclusion_policy/test_contracts.py`, `test_normalization.py`, `test_evaluation.py`, `test_evaluator_golden.py`
- Modify: `src/personal_os/error_contracts/codes.py`, `src/personal_os/error_contracts/__init__.py`
- Modify: `src/personal_os/diagnostics/events.py`
- Modify: `tests/unit/diagnostics/test_event_registry.py`, `test_event_values.py`

**Interfaces:**
- Produces `RuleKind`, `RawPolicyDecision`, `EnforcedPolicyDecision`, `PreviewMatchState`, `ExclusionRule`, `PolicySubject`, `ExclusionPolicyRevision`, `PolicyEvaluationOutcome`, `normalize_locator()`, `normalize_rule()` and `evaluate_policy()`.
- Reuse `SourceType`, `CanonicalMediaType`, UUID and byte-size value semantics from existing canonical contracts; do not create parallel provider-specific types.

- [ ] **Step 1: Write failing closed-enum, normalization, glob and truth-table tests**

```python
def test_missing_size_is_indeterminate_for_maximum_size() -> None:
    decision = evaluate_policy(
        revision=revision(rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=8 * 1024 * 1024)),
        subject=subject(size_bytes=None),
    )
    assert decision.raw is RawPolicyDecision.INDETERMINATE
    assert decision.enforced is EnforcedPolicyDecision.EXCLUDED


def test_any_definite_match_excludes_even_when_another_rule_is_indeterminate() -> None:
    decision = evaluate_policy(
        revision=revision(extension_rule(".pdf"), maximum_size_rule(1024)),
        subject=subject(normalized_locator="vault/a.pdf", size_bytes=None),
    )
    assert decision.raw is RawPolicyDecision.EXCLUDED
```

Golden cases must cover NFC equivalence, slash normalization, case rules, folder boundaries, every supported glob token, empty policy, missing fields, invalid operands, maximum-size equality and multiple-rule precedence. Add property/fuzz-style bounded generators for hostile locator/pattern lengths without adding a production dependency.

- [ ] **Step 2: Run focused tests and confirm missing-domain failures**

Run: `uv run pytest tests/unit/exclusion_policy/test_contracts.py tests/unit/exclusion_policy/test_normalization.py tests/unit/exclusion_policy/test_evaluation.py tests/unit/exclusion_policy/test_evaluator_golden.py -q`

Expected: collection fails because `personal_os.exclusion_policy` does not exist.

- [ ] **Step 3: Implement immutable values, bounded normalization and evaluation**

```python
class RuleKind(StrEnum):
    EXACT_SOURCE_ID = "exact_source_id"
    FOLDER_PREFIX = "folder_prefix"
    PATH_GLOB = "path_glob"
    EXTENSION = "extension"
    MEDIA_TYPE = "media_type"
    MAXIMUM_SIZE = "maximum_size"
    SOURCE_TYPE = "source_type"


@dataclass(frozen=True, slots=True)
class PolicyEvaluationOutcome:
    raw: RawPolicyDecision
    enforced: EnforcedPolicyDecision
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[PolicySubjectField, ...]
```

Compile glob patterns into a bounded internal token sequence, never regular expressions supplied by users. Reject more than 256 rules before evaluation. Evidence contains only IDs and closed decisions, not locators or operands.

- [ ] **Step 4: Run domain, registry, lint and strict-type gates**

Run: `uv run pytest tests/unit/exclusion_policy tests/unit/error_contracts tests/unit/diagnostics/test_event_registry.py tests/unit/diagnostics/test_event_values.py -q`

Run: `uv run ruff check src/personal_os/exclusion_policy tests/unit/exclusion_policy && uv run mypy src/personal_os/exclusion_policy`

Expected: all commands exit `0`; the golden fixture is deterministic and contains no raw content.

- [ ] **Step 5: Commit the evaluator deliverable**

```powershell
git add src/personal_os/exclusion_policy src/personal_os/error_contracts src/personal_os/diagnostics/events.py tests/fixtures/exclusion_policy/evaluator-golden.json tests/unit/exclusion_policy tests/unit/diagnostics/test_event_registry.py tests/unit/diagnostics/test_event_values.py
git commit -m "feat: add exclusion policy evaluator"
```

---

### Task 2: Add canonical signed payloads and Ed25519 adapters

**Files:**
- Create: `src/personal_os/exclusion_policy/canonical_json.py`, `signatures.py`
- Create: `apps/api/src/api_runtime/exclusion_policy_crypto.py`
- Create: `tests/fixtures/exclusion_policy/snapshot-golden.json`, `keyset-golden.json`
- Create: `tests/unit/exclusion_policy/test_canonical_json.py`, `test_signatures.py`
- Create: `tests/unit/api_runtime/test_exclusion_policy_crypto.py`
- Modify: `apps/obsidian-plugin/package.json`, `pnpm-lock.yaml`

**Interfaces:**
- Produces `canonicalize_json_value()`, `build_snapshot_payload()`, `build_keyset_payload()`, `build_signed_message()`, `PolicySigner`, `PolicySignatureVerifier`, `Ed25519PolicySigner` and `Ed25519PolicyVerifier`.
- Signed payload builders accept typed values and emit exact bytes; they never accept arbitrary dictionaries from a route or database row.

- [ ] **Step 1: Write failing canonicalization, payload-vector and crypto-negative tests**

```python
def test_snapshot_golden_bytes_and_signature_are_stable(snapshot_vector: SnapshotVector) -> None:
    payload = build_snapshot_payload(snapshot_vector.snapshot)
    assert payload == snapshot_vector.canonical_payload
    assert verifier.verify(snapshot_vector.key_id, snapshot_vector.signature, payload)


@pytest.mark.parametrize("value", [1.5, float("nan"), "e\u0301"])
def test_closed_canonicalizer_rejects_unsupported_or_non_nfc_values(value: object) -> None:
    with pytest.raises(PolicyContractError):
        canonicalize_json_value(value)
```

Include modified-byte, wrong-workspace, wrong-domain-separator, wrong-key, malformed-base64 and oversized-snapshot cases.

- [ ] **Step 2: Run focused tests and confirm missing canonical/signature implementations**

Run: `uv run pytest tests/unit/exclusion_policy/test_canonical_json.py tests/unit/exclusion_policy/test_signatures.py tests/unit/api_runtime/test_exclusion_policy_crypto.py -q`

- [ ] **Step 3: Implement the closed encoder, typed payload builders and crypto adapters**

```python
type CanonicalJsonValue = None | bool | int | str | tuple["CanonicalJsonValue", ...] | Mapping[str, "CanonicalJsonValue"]


class PolicySigner(Protocol):
    @property
    def key_id(self) -> str: ...
    def sign(self, message: bytes) -> bytes: ...
```

Reject keys outside the fixed payload schema before encoding. Enforce the 256 KiB limit on canonical payload plus signature envelope. Pin `@noble/ed25519` exactly with `pnpm add --filter @workspace/obsidian-plugin --save-exact @noble/ed25519@3.1.0`; inspect the lockfile for zero runtime transitives.

- [ ] **Step 4: Run crypto vectors, dependency and leakage gates**

Run: `uv run pytest tests/unit/exclusion_policy/test_canonical_json.py tests/unit/exclusion_policy/test_signatures.py tests/unit/api_runtime/test_exclusion_policy_crypto.py tests/contract/test_dependency_pins.py tests/contract/test_sensitive_diagnostics.py -q`

Run: `uv run poe python-lint && uv run poe python-type-check && pnpm --filter @workspace/obsidian-plugin run type-check`

- [ ] **Step 5: Commit signed-contract foundations**

```powershell
git add src/personal_os/exclusion_policy apps/api/src/api_runtime/exclusion_policy_crypto.py apps/obsidian-plugin/package.json pnpm-lock.yaml tests/fixtures/exclusion_policy tests/unit/exclusion_policy tests/unit/api_runtime/test_exclusion_policy_crypto.py
git commit -m "feat: add signed policy contracts"
```

---

### Task 3: Add the PostgreSQL policy baseline and projection-intent origin migration

**Files:**
- Create: `migrations/versions/20260817_01_add_exclusion_policy_publication.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/tables.py`, `backup_snapshot.py`, `projection_intents.py`, `identity_bootstrap.py`
- Modify: `src/personal_os/sources/projection_dispatch.py`, `ports.py`
- Create: `tests/contract/exclusion_policy/test_policy_table_metadata.py`, `test_policy_migration_contract.py`
- Modify: `tests/contract/source_publication/test_table_metadata.py`
- Create: `tests/integration/exclusion_policy/test_policy_migration.py`, `conftest.py`
- Modify: `tests/unit/postgresql_source_store/test_backup_snapshot.py`, `test_projection_backoff.py`, `test_identity_bootstrap.py`

**Interfaces:**
- Adds the exact tables and constraints from spec section 8: `workspace_policy_state`, `policy_drafts`, `policy_draft_rules`, `source_policies`, `policy_rules`, `policy_previews`, `policy_preview_results`, `policy_evaluations`, `policy_reconciliation_intents`, `policy_signing_keys`, `policy_keysets` and `policy_keyset_signatures`.
- Evolves `LeasedProjectionIntent` with `origin_kind`, nullable `event_id` and nullable `policy_revision_id`, enforcing exactly one origin.

- [ ] **Step 1: Write failing metadata, upgrade/downgrade and origin-isolation tests**

Assert immutable revision/rule tables have no update path, revision numbers are workspace-unique, evaluation identity is `(policy_revision_id, source_id, subject_event_sequence)`, preview-result identity includes the exact preview, and both projection-intent origin shapes satisfy/violate the database CHECK as expected.

- [ ] **Step 2: Run migration tests and confirm missing revision/schema failures**

Run: `uv run pytest tests/contract/exclusion_policy/test_policy_table_metadata.py tests/contract/exclusion_policy/test_policy_migration_contract.py tests/integration/exclusion_policy/test_policy_migration.py -m local_stack -q`

- [ ] **Step 3: Implement one reversible migration and matching SQLAlchemy metadata**

Use UUIDv7 IDs, database timestamps, bounded varchar/check constraints, partial indexes for active/ready/pending lookups, append-only mutation-rejection triggers and foreign-key delete behavior from spec section 8. `downgrade()` returns exactly to the Child 2 head and must refuse outside the explicit destructive gate when any policy/keyset/preview/evaluation or policy-origin row exists. Initialize one `workspace_policy_state` row and one empty draft for every existing workspace, but leave `active_policy_revision_id` null; extend future workspace bootstrap through the same adapter transaction.

```python
class ProjectionIntentOriginKind(StrEnum):
    SOURCE_EVENT = "source_event"
    POLICY_TRANSITION = "policy_transition"
```

Update backup/restore table order so policy state, immutable revisions/rules, key history and durable intents are canonical; ephemeral preview results may be reconstructed only where the spec explicitly permits it.

- [ ] **Step 4: Prove upgrade, downgrade, restore order and legacy dispatch compatibility**

Run: `uv run pytest tests/contract/exclusion_policy tests/contract/source_publication/test_table_metadata.py tests/unit/postgresql_source_store/test_backup_snapshot.py tests/unit/postgresql_source_store/test_projection_backoff.py -q`

Run: `uv run pytest tests/integration/exclusion_policy/test_policy_migration.py tests/integration/projection_dispatch/test_projection_intent_leases.py -m local_stack -q`

Run: `uv run alembic heads`

- [ ] **Step 5: Commit the schema deliverable**

```powershell
git add migrations/versions/20260817_01_add_exclusion_policy_publication.py packages/postgresql-source-store/src/postgresql_source_store src/personal_os/sources tests/contract/exclusion_policy tests/contract/source_publication/test_table_metadata.py tests/integration/exclusion_policy tests/unit/postgresql_source_store
git commit -m "feat: add exclusion policy schema"
```

---

### Task 4: Implement policy repositories and exact-version draft mutation

**Files:**
- Create: `src/personal_os/exclusion_policy/ports.py`, `drafts.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/policy_drafts.py`, `policy_keysets.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/__init__.py`
- Create: `tests/unit/exclusion_policy/test_drafts.py`
- Create: `tests/unit/postgresql_source_store/test_policy_drafts.py`, `test_policy_keysets.py`
- Create: `tests/integration/exclusion_policy/test_policy_draft_transactions.py`

**Interfaces:**
- Produces `PolicyDraftStore`, `PolicyQueryStore`, `PolicyKeysetStore`, `PolicyDraftService`, `load_draft()`, `replace_draft_rules()`, `get_policy_status()` and immutable row-to-domain mapping. Draft creation itself remains part of the workspace-bootstrap transaction from Task 3.
- Every mutation requires `expected_draft_version`; a successful mutation increments the version exactly once and invalidates ready previews for the prior draft version.

- [ ] **Step 1: Write failing draft validation, stale-version and concurrent-writer tests**

```python
async def test_replace_rules_rejects_stale_draft_version(store: PolicyDraftStore) -> None:
    current = await store.load_draft(workspace_id, context)
    await store.replace_rules(current.draft_id, current.version, (extension_rule(".tmp"),), actor, context)
    with pytest.raises(ExclusionPolicyError) as raised:
        await store.replace_rules(current.draft_id, current.version, (), actor, context)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT
```

Cover duplicate rule IDs, invalid typed operand columns, 257 rules, unsupported kind, cross-workspace lookup, immutable published rows and audit fields containing IDs/closed counts only.

- [ ] **Step 2: Run focused domain/adapter tests and inspect missing-port failures**

Run: `uv run pytest tests/unit/exclusion_policy/test_drafts.py tests/unit/postgresql_source_store/test_policy_drafts.py tests/unit/postgresql_source_store/test_policy_keysets.py -q`

- [ ] **Step 3: Implement strict ports, retry mapping and PostgreSQL transactions**

```python
class PolicyDraftStore(Protocol):
    async def load_draft(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyDraft: ...

    async def replace_rules(
        self,
        draft_id: UUID,
        expected_draft_version: int,
        rules: tuple[ExclusionRule, ...],
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyDraft: ...
```

Use `SELECT ... FOR UPDATE` on the draft row, database time, bounded contention retry and the existing safe database error classifier. Store one typed operand column per rule kind and database CHECKs that exactly one legal operand shape is populated.

- [ ] **Step 4: Run focused integration, lint and type gates**

Run: `uv run pytest tests/unit/exclusion_policy/test_drafts.py tests/unit/postgresql_source_store/test_policy_drafts.py tests/unit/postgresql_source_store/test_policy_keysets.py -q`

Run: `uv run pytest tests/integration/exclusion_policy/test_policy_draft_transactions.py -m local_stack -q`

Run: `uv run ruff check src/personal_os/exclusion_policy packages/postgresql-source-store/src/postgresql_source_store tests/unit/exclusion_policy tests/unit/postgresql_source_store && uv run mypy src/personal_os/exclusion_policy packages/postgresql-source-store/src/postgresql_source_store`

- [ ] **Step 5: Commit draft persistence**

```powershell
git add src/personal_os/exclusion_policy packages/postgresql-source-store/src/postgresql_source_store tests/unit/exclusion_policy tests/unit/postgresql_source_store tests/integration/exclusion_policy
git commit -m "feat: add exclusion policy drafts"
```

---

### Task 5: Add signing configuration and the dedicated key lifecycle CLI

**Files:**
- Create: `apps/api/src/api_runtime/exclusion_policy_settings.py`, `exclusion_policy_commands.py`
- Modify: `apps/api/src/api_runtime/command.py`, `server.py`, `database_lifecycle.py`
- Modify: `src/personal_os/runtime_configuration/environment_names.py`
- Create: `tests/unit/api_runtime/test_exclusion_policy_settings.py`, `test_exclusion_policy_commands.py`, `test_exclusion_policy_startup.py`
- Create: `tests/integration/exclusion_policy/test_policy_key_rotation.py`
- Modify: `apps/api/README.md`

**Interfaces:**
- Produces `ExclusionPolicySigningSettings`, `load_exclusion_policy_signer()`, and internal commands `policy-key initialize`, `policy-key stage`, `policy-key activate`, `policy-key retire`.
- Startup verifies exact private-key file permissions/length, derives its public key, and proves the configured active key ID exists in PostgreSQL before binding a socket.

- [ ] **Step 1: Write failing secret-file, startup coverage and rotation-chain tests**

Test absolute paths, `..`, symlink/reparse ambiguity, encrypted/malformed/multi-key/wrong-algorithm PEM, file permission mismatch, unknown active key, private/public mismatch, retiring the last trusted key, activating without cross-signature, valid staged activation and replayed CLI invocation.

- [ ] **Step 2: Run focused tests and confirm missing settings/command failures**

Run: `uv run pytest tests/unit/api_runtime/test_exclusion_policy_settings.py tests/unit/api_runtime/test_exclusion_policy_commands.py tests/unit/api_runtime/test_exclusion_policy_startup.py -q`

- [ ] **Step 3: Implement fail-before-bind loading and cross-signed lifecycle commands**

Use `KNOWLEDGE_POLICY_SIGNING_KEY_ID` and `KNOWLEDGE_POLICY_SIGNING_KEY_FILE`. Reuse the exact secret-root loader; require an unencrypted PKCS#8 PEM containing exactly one Ed25519 private key. Derive the raw 32-byte public key and key ID, then prove they equal the current key in the latest canonical database keyset. Commands print IDs/status only, never key bytes or signatures.

```python
@dataclass(frozen=True, slots=True)
class ExclusionPolicySigningSettings:
    signing_key_id: str
    signing_key_file: Path
```

Key activation writes the cross-signed immutable keyset whose payload declares current/staged/retired meaning and appends audit in one transaction; it never mutates an existing signing-key row. Private-key generation writes only a newly created exact file with restrictive permissions and refuses overwrite.

- [ ] **Step 4: Run startup/import, rotation and secret-leak gates**

Run: `uv run pytest tests/unit/api_runtime/test_exclusion_policy_settings.py tests/unit/api_runtime/test_exclusion_policy_commands.py tests/unit/api_runtime/test_exclusion_policy_startup.py tests/contract/test_command_import_side_effects.py tests/contract/test_sensitive_diagnostics.py -q`

Run: `uv run pytest tests/integration/exclusion_policy/test_policy_key_rotation.py -m local_stack -q`

- [ ] **Step 5: Commit signing lifecycle support**

```powershell
git add apps/api/src/api_runtime apps/api/README.md src/personal_os/runtime_configuration/environment_names.py tests/unit/api_runtime tests/integration/exclusion_policy
git commit -m "feat: add policy signing key lifecycle"
```

---

### Task 6: Implement exact-snapshot asynchronous preview

**Files:**
- Create: `src/personal_os/exclusion_policy/previews.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/policy_previews.py`
- Create: `apps/worker/src/workflow_worker/policy_preview_workflow.py`, `policy_workflow_runtime.py`
- Modify: `apps/worker/src/workflow_worker/command.py`
- Create: `tests/unit/exclusion_policy/test_previews.py`
- Create: `tests/unit/postgresql_source_store/test_policy_previews.py`
- Create: `tests/unit/workflow_worker/test_policy_preview_workflow.py`
- Create: `tests/integration/exclusion_policy/test_policy_preview_transactions.py`, `test_policy_preview_temporal.py`

**Interfaces:**
- Produces `PolicyPreviewStore.request_preview()`, `run_preview_activity()`, `get_preview()`, `list_preview_results()` and Temporal workflow ID `exclusion-policy-preview/{workspace_id}/{policy_preview_id}`.
- A preview binds exact `draft_id`, `draft_version`, prior active revision (nullable) and `source_event_checkpoint`; results expose only IDs, closed match states and safe display metadata already authorized to Admin.

- [ ] **Step 1: Write failing snapshot-isolation, crash-rollback and expiry tests**

```python
async def test_preview_activity_rolls_back_every_result_after_midstream_failure(
    preview_store: PolicyPreviewStore,
) -> None:
    with pytest.raises(InjectedPreviewFailure):
        await preview_store.run_preview_activity(preview_id, fail_after_subjects=501)
    assert await preview_store.count_results(preview_id) == 0
    assert (await preview_store.get_preview(preview_id)).status is PreviewStatus.PENDING
```

Cover no-active-policy semantics, 500-row server cursor batches, heartbeat counts, source mutations during execution, stale checkpoint at publish-read time, 200-row API pagination and ready-at plus 15-minute expiry.

- [ ] **Step 2: Run focused tests and confirm missing workflow/store failures**

Run: `uv run pytest tests/unit/exclusion_policy/test_previews.py tests/unit/postgresql_source_store/test_policy_previews.py tests/unit/workflow_worker/test_policy_preview_workflow.py -q`

- [ ] **Step 3: Implement the single-activity repeatable-read preview**

```python
@dataclass(frozen=True, slots=True)
class PolicyPreviewBinding:
    preview_id: UUID
    draft_id: UUID
    draft_version: int
    active_policy_revision_id: UUID | None
    source_event_checkpoint: int
```

The request transaction captures the binding and durable pending row. Its leased dispatcher uses the deterministic workflow ID and converges after lost start acknowledgement. The activity opens one `REPEATABLE READ` transaction, validates the binding/checkpoint before scanning, fetches sources in stable `(source_id)` order in 500-row pages, evaluates old/new policy, writes rows, heartbeats after each page and marks ready in that same transaction. Cancellation or failure rolls back every result. Emit only the closed preview diagnostic events/metrics from spec section 21 after the durable outcome is known.

- [ ] **Step 4: Run unit, PostgreSQL and Temporal integration gates**

Run: `uv run pytest tests/unit/exclusion_policy/test_previews.py tests/unit/postgresql_source_store/test_policy_previews.py tests/unit/workflow_worker/test_policy_preview_workflow.py -q`

Run: `uv run pytest tests/integration/exclusion_policy/test_policy_preview_transactions.py tests/integration/exclusion_policy/test_policy_preview_temporal.py -m local_stack -q`

Run: `uv run ruff check src/personal_os/exclusion_policy apps/worker/src/workflow_worker packages/postgresql-source-store/src/postgresql_source_store && uv run mypy src/personal_os/exclusion_policy apps/worker/src/workflow_worker packages/postgresql-source-store/src/postgresql_source_store`

- [ ] **Step 5: Commit preview orchestration**

```powershell
git add src/personal_os/exclusion_policy/previews.py packages/postgresql-source-store/src/postgresql_source_store/policy_previews.py apps/worker/src/workflow_worker tests/unit/exclusion_policy tests/unit/postgresql_source_store tests/unit/workflow_worker tests/integration/exclusion_policy
git commit -m "feat: add exclusion policy preview"
```

---

### Task 7: Implement atomic publication, signatures and ambiguous-commit replay

**Files:**
- Create: `src/personal_os/exclusion_policy/publication.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/policy_publication.py`
- Create: `tests/unit/exclusion_policy/test_publication.py`
- Create: `tests/unit/postgresql_source_store/test_policy_publication.py`
- Create: `tests/integration/exclusion_policy/test_policy_publication_transaction.py`, `test_policy_publication_races.py`, `test_policy_ambiguous_commit.py`

**Interfaces:**
- Produces `PublishPolicyCommand`, `PublishedPolicyResult`, `PolicyPublicationStore.resolve_committed()`, `commit_publication()` and `ExclusionPolicyPublicationService.publish()`.
- Request fingerprint includes contract tag, workspace/actor, preview ID/digest, draft ID/version/hash, expected active revision and exact confirmation semantics; it excludes request/trace IDs and the idempotency key itself. Signature bytes are deterministic for the committed canonical payload.

- [ ] **Step 1: Write failing initial-publish, stale-preview, replay and race tests**

Cover explicit empty initial policy, valid non-empty policy, expired preview, draft mutation after preview, source checkpoint advance, active revision advance, idempotency mismatch, identical replay, two concurrent publishers, signing failure rollback and connection loss after commit.

- [ ] **Step 2: Run focused tests and confirm absent publication behavior**

Run: `uv run pytest tests/unit/exclusion_policy/test_publication.py tests/unit/postgresql_source_store/test_policy_publication.py -q`

- [ ] **Step 3: Implement one signed publication transaction with deterministic recovery**

```python
class PolicyPublicationStore(Protocol):
    async def resolve_committed(
        self, command: PublishPolicyCommand, fingerprint: PolicyRequestFingerprint
    ) -> PublishedPolicyResult | None: ...

    async def commit_publication(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        signed_snapshot: SignedPolicySnapshot,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult: ...
```

The service validates input before opening the transaction. The store obtains the policy-idempotency advisory lock, locks `workspace_policy_state`, then the draft and preview rows, rechecks exact binding/expiry/checkpoint, builds/canonicalizes/signs/verifies the snapshot while the serialization row is locked, inserts immutable revision/rules/signature, swaps the active pointer, persists replay evidence, writes audit, creates reconciliation work, marks the preview consumed and rebases/increments the same draft in one commit. On ambiguous acknowledgement, reconnect and resolve by the same key/fingerprint; never sign or insert a second revision. Publication diagnostics/metrics are emitted only after a known commit/replay/rejection outcome and carry safe IDs, hashes, counts and closed labels only.

- [ ] **Step 4: Run publication, race and rollback integration gates**

Run: `uv run pytest tests/unit/exclusion_policy/test_publication.py tests/unit/postgresql_source_store/test_policy_publication.py -q`

Run: `uv run pytest tests/integration/exclusion_policy/test_policy_publication_transaction.py tests/integration/exclusion_policy/test_policy_publication_races.py tests/integration/exclusion_policy/test_policy_ambiguous_commit.py -m local_stack -q`

- [ ] **Step 5: Commit atomic policy publication**

```powershell
git add src/personal_os/exclusion_policy/publication.py packages/postgresql-source-store/src/postgresql_source_store/policy_publication.py tests/unit/exclusion_policy tests/unit/postgresql_source_store tests/integration/exclusion_policy
git commit -m "feat: publish signed exclusion policies"
```

---

### Task 8: Expose authenticated Admin and plugin policy APIs

**Files:**
- Create: `apps/api/src/api_runtime/exclusion_policy_models.py`, `exclusion_policy_routes.py`, `exclusion_policy_composition.py`
- Modify: `apps/api/src/api_runtime/application.py`, `server.py`, `openapi_export.py`
- Modify: `src/personal_os/api_contracts/request_values.py`, `src/personal_os/error_contracts/codes.py`
- Create: `tests/unit/api_runtime/test_exclusion_policy_models.py`, `test_exclusion_policy_routes.py`, `test_exclusion_policy_composition.py`
- Create: `tests/contract/api/test_exclusion_policy_routes.py`, `test_exclusion_policy_openapi.py`, `test_exclusion_policy_leakage.py`
- Modify: `packages/api-client/openapi.json`, generated files under `packages/api-client/src/`

**Interfaces:**
- Adds Admin routes for status/draft read, full draft replacement, preview request/status/results and publish; plugin routes return the current cross-signed keyset and active signed snapshot.
- Admin workspace/actor and plugin workspace/device always come from authenticated context. Publish accepts the existing idempotency header and exact expected binding, not a client-supplied signature or revision number.

```text
GET  /api/admin/exclusion-policy
PUT  /api/admin/exclusion-policy/draft
POST /api/admin/exclusion-policy/previews
GET  /api/admin/exclusion-policy/previews/{policy_preview_id}
POST /api/admin/exclusion-policy/publications
GET  /api/sync/exclusion-policy/keysets
GET  /api/sync/exclusion-policy/snapshot
```

- [ ] **Step 1: Write failing route-set, authorization, envelope and OpenAPI tests**

Assert exact semantic operation IDs, strict body models (`extra="forbid"`), 200-row preview page cap, no trailing slash variants, no workspace selector, Web session/CSRF/recent-auth requirements, `obsidian_sync` Bearer requirement, `Cache-Control: no-store`, ETag behavior for keyset/snapshot and closed error envelope mappings.

- [ ] **Step 2: Run API unit/contract tests and inspect missing-route failures**

Run: `uv run pytest tests/unit/api_runtime/test_exclusion_policy_models.py tests/unit/api_runtime/test_exclusion_policy_routes.py tests/unit/api_runtime/test_exclusion_policy_composition.py tests/contract/api/test_exclusion_policy_routes.py tests/contract/api/test_exclusion_policy_openapi.py -q`

- [ ] **Step 3: Implement route composition over domain services**

Use endpoint factories like the existing authentication routes. Pydantic models convert to domain values at the boundary and never leak database/provider objects. Snapshot/keyset endpoints return their exact persisted signed envelopes as typed JSON, with snapshot ETag equal to the quoted payload SHA-256 and `304` only after authenticating the caller. Keyset pagination accepts a nonnegative known revision and returns at most 16 ordered envelopes.

```python
@dataclass(frozen=True, slots=True)
class ExclusionPolicyRuntime:
    drafts: PolicyDraftService
    previews: PolicyPreviewService
    publication: ExclusionPolicyPublicationService
    queries: PolicyQueryService
```

Register the routes explicitly in `create_api_application`; do not use router auto-discovery. Extend closed route-template and error/status registries with only spec section 19 values.

- [ ] **Step 4: Export OpenAPI, regenerate the client and run API boundary gates**

Run: `uv run pytest tests/unit/api_runtime/test_exclusion_policy_models.py tests/unit/api_runtime/test_exclusion_policy_routes.py tests/unit/api_runtime/test_exclusion_policy_composition.py tests/contract/api/test_exclusion_policy_routes.py tests/contract/api/test_exclusion_policy_openapi.py tests/contract/api/test_exclusion_policy_leakage.py -q`

Run: `uv run poe api-contract-export`

Run: `pnpm --filter @workspace/api-client run generate`

Run: `uv run poe api-contract-check && uv run poe python-type-check && pnpm --filter @workspace/api-client run type-check`

- [ ] **Step 5: Commit the API contract**

```powershell
git add apps/api/src/api_runtime src/personal_os/api_contracts src/personal_os/error_contracts tests/unit/api_runtime tests/contract/api packages/api-client
git commit -m "feat: expose exclusion policy api"
```

---

### Task 9: Build the Web Admin policy editor, preview and publish confirmation

**Files:**
- Create: `apps/web/src/api/exclusion-policy-client.ts`, `exclusion-policy-client.test.ts`
- Create: `apps/web/src/features/exclusion-policy/policy-models.ts`
- Create: `apps/web/src/features/exclusion-policy/PolicyEditor.tsx`, `PolicyEditor.test.tsx`
- Create: `apps/web/src/features/exclusion-policy/PolicyPreview.tsx`, `PolicyPreview.test.tsx`
- Create: `apps/web/src/features/exclusion-policy/PolicyPublishDialog.tsx`, `PolicyPublishDialog.test.tsx`
- Create: `apps/web/src/features/exclusion-policy/PolicyStatus.tsx`, `PolicyStatus.test.tsx`
- Create: `apps/web/src/app/admin/policy/page.tsx`

**Interfaces:**
- The client wraps only the generated API client and existing authenticated fetch/CSRF behavior.
- The editor shows all seven rule kinds, normalized validation feedback, counts for newly excluded/newly allowed/unchanged/indeterminate, paged result details and an explicit publish confirmation bound to the preview ID/version/checkpoint.

- [ ] **Step 1: Write failing accessible UI and stale-state tests**

Test keyboard/label semantics, adding/removing/reordering rules without changing IDs, operand-specific validation, preview polling, expired/stale preview messaging, indeterminate warning, exact publish confirmation, double-submit protection, replayed response and safe generic failure copy that never renders secret/provider details.

- [ ] **Step 2: Run Web tests and confirm missing components**

Run: `pnpm --filter @workspace/web exec vitest run src/api/exclusion-policy-client.test.ts src/features/exclusion-policy/PolicyEditor.test.tsx src/features/exclusion-policy/PolicyPreview.test.tsx src/features/exclusion-policy/PolicyPublishDialog.test.tsx src/features/exclusion-policy/PolicyStatus.test.tsx`

- [ ] **Step 3: Implement the minimal Admin page and state transitions**

Keep draft state local until explicit save; server draft version is the concurrency token. Poll only pending/running previews with bounded interval and stop on ready/failed/expired/unmount. Disable publish unless the current ready preview exactly matches the saved draft and current active revision/checkpoint returned by status.

```ts
export type PolicyAdminState =
  | { kind: "loading" }
  | { kind: "editing"; draft: PolicyDraft; status: PolicyStatus }
  | { kind: "previewing"; draft: PolicyDraft; previewId: string }
  | { kind: "publishable"; draft: PolicyDraft; preview: ReadyPolicyPreview }
  | { kind: "failed"; errorCode: PolicySafeErrorCode };
```

- [ ] **Step 4: Run Web unit, lint, type and production-build gates**

Run: `pnpm --filter @workspace/web run test`

Run: `pnpm --filter @workspace/web run lint && pnpm --filter @workspace/web run type-check && pnpm --filter @workspace/web run build`

- [ ] **Step 5: Commit the Admin UI**

```powershell
git add apps/web/src/api apps/web/src/features/exclusion-policy apps/web/src/app/admin/policy
git commit -m "feat: add exclusion policy admin ui"
```

---

### Task 10: Verify, cache and evaluate policy in the Obsidian plugin

**Files:**
- Create: `apps/obsidian-plugin/src/exclusion-policy/contracts.ts`, `strict-json.ts`, `canonical-json.ts`, `evaluator.ts`, `keyset.ts`, `snapshot.ts`, `policy-cache.ts`, `policy-session.ts`
- Create: matching `*.test.ts` files under `apps/obsidian-plugin/src/exclusion-policy/`
- Modify: `apps/obsidian-plugin/src/plugin.ts`, `plugin.test.ts`
- Modify: `apps/obsidian-plugin/src/api/obsidian-api-transport.ts`

**Interfaces:**
- Produces `verifyKeysetChain()`, `verifyPolicySnapshot()`, `evaluatePolicy()`, `PolicyCacheAdapter` and `PolicySession.refresh()`.
- Cache contains only the last accepted signed keyset/snapshot envelope and monotonic revision metadata. No private key, Vault content or raw excluded path is persisted for policy diagnostics.

- [ ] **Step 1: Write failing cross-language vectors, rollback and cache-preservation tests**

Import all three shared fixtures. Test authenticated-onboarding acceptance of self-signed keyset revision 1, rejection of the same bytes outside that onboarding boundary, one and multiple rotations, wrong workspace, unknown key, modified byte, malformed canonical value, snapshot/keyset downgrade, same-revision different bytes, offline startup with previous valid cache, first-run offline deny and invalid refresh preserving the previous valid cache.

- [ ] **Step 2: Run plugin tests and confirm missing verifier/evaluator modules**

Run: `pnpm --filter @workspace/obsidian-plugin exec vitest run src/exclusion-policy`

- [ ] **Step 3: Implement WebCrypto-backed verification and fail-closed local evaluation**

```ts
export interface AcceptedPolicyState {
  readonly workspaceId: string;
  readonly revisionNumber: number;
  readonly keysetSequence: number;
  readonly keysetEnvelope: SignedPolicyKeyset;
  readonly snapshotEnvelope: SignedPolicySnapshot;
}

export type LocalPolicyDecision =
  | { readonly raw: "allowed"; readonly enforced: "allowed" }
  | { readonly raw: "excluded" | "indeterminate"; readonly enforced: "excluded" };
```

Use `ed25519.verifyAsync`; configure SHA-512 through platform WebCrypto exactly once. Parse the bounded response text with a repository-owned closed JSON parser that rejects duplicate properties, lone surrogates, floats/non-I-JSON values and unknown schema fields before canonicalization; plain `JSON.parse()` alone is insufficient. Compare immutable revision/keyset sequence before replacing cache. Fetch keyset before a snapshot signed by an unknown key, and never clear a good cache because the network or verification failed. Persist the accepted keyset/snapshot as one versioned plugin-data record through an adapter, read it back before switching the in-memory pointer, and retain the prior in-memory/persisted record on any write/readback failure.

- [ ] **Step 4: Run plugin test, bundle, forbidden-import and leakage gates**

Run: `pnpm --filter @workspace/obsidian-plugin run test`

Run: `pnpm --filter @workspace/obsidian-plugin run lint && pnpm --filter @workspace/obsidian-plugin run type-check && pnpm --filter @workspace/obsidian-plugin run build`

Run: `uv run pytest tests/contract/api/test_plugin_authentication_bundle.py tests/contract/api/test_exclusion_policy_leakage.py -q`

- [ ] **Step 5: Commit plugin policy verification**

```powershell
git add apps/obsidian-plugin/src apps/obsidian-plugin/package.json pnpm-lock.yaml tests/fixtures/exclusion_policy
git commit -m "feat: verify exclusion policy in obsidian"
```

---

### Task 11: Enforce active policy at source publication and canonical read boundaries

**Files:**
- Create: `src/personal_os/exclusion_policy/enforcement.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/policy_enforcement.py`
- Modify: `src/personal_os/sources/ports.py`, `publication.py`, `reading.py`, `commands.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/publication_store.py`, `canonical_read.py`, `locks.py`
- Modify: canonical-core CLI/test harness call sites that publish or read sources
- Create: `tests/unit/exclusion_policy/test_enforcement.py`
- Modify: `tests/unit/sources/test_publication_service.py`, `test_canonical_read.py`, `fakes.py`
- Create: `tests/contract/exclusion_policy/test_enforcement_boundaries.py`, `test_policy_lock_order.py`
- Create: `tests/integration/exclusion_policy/test_source_publication_enforcement.py`, `test_canonical_read_enforcement.py`, `test_policy_publication_race.py`

**Interfaces:**
- Produces `PolicyEnforcementService.authorize_preflight()`, internal-only `PolicyDecision` and PostgreSQL final recheck methods used inside publication/read state resolution.
- `SourceVersionPublicationService` evaluates before object-store access; `SourcePublicationStore.commit_create/update` receive preflight evidence but independently lock/re-evaluate the active policy before source mutation.

- [ ] **Step 1: Write failing no-R2-on-deny, final-recheck and read-denial tests**

```python
async def test_excluded_source_never_calls_object_store(
    service: SourceVersionPublicationService, object_store: RecordingObjectStore
) -> None:
    service.policy_guard = denying_policy_guard()
    with pytest.raises(ExclusionPolicyError) as raised:
        await service.publish_create(command=command, stream=stream(), diagnostic_context=context)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert object_store.calls == []
```

Cover indeterminate deny, missing active signed policy, policy changes during upload, exact replay now excluded, canonical read after exclusion, corrupt signature material, concurrent source/policy publication with lock timeouts and unchanged current pointer after rejection.

- [ ] **Step 2: Run focused source/policy tests and inspect expected failures**

Run: `uv run pytest tests/unit/exclusion_policy/test_enforcement.py tests/unit/sources/test_publication_service.py tests/unit/sources/test_canonical_read.py tests/contract/exclusion_policy/test_enforcement_boundaries.py tests/contract/exclusion_policy/test_policy_lock_order.py -q`

- [ ] **Step 3: Implement preflight plus transaction-final enforcement**

```python
@dataclass(frozen=True, slots=True)
class PolicyDecision:
    workspace_id: UUID
    policy_revision_id: UUID
    revision_number: int
    subject_fingerprint: bytes
    raw_decision: RawPolicyDecision
    enforced_decision: EnforcedPolicyDecision
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[PolicySubjectField, ...]
    evaluated_at: datetime
```

Preflight loads/verifies active signed policy and evaluates the candidate before `resolve_verified_object()` or `store_stream()`. Commit acquires the policy-state row between idempotency and source locks, rebuilds the authoritative subject, treats the preflight decision only as a non-authoritative hint, and evaluates the currently active revision again. A replay lookup may avoid R2 but must not return canonical data until the current policy permits the subject. Reads resolve policy and source state transactionally before issuing any R2 GET. Evaluation metrics use only closed `boundary` and `decision` labels.

- [ ] **Step 4: Run race, no-network-in-transaction and canonical-core regression gates**

Run: `uv run pytest tests/unit/exclusion_policy/test_enforcement.py tests/unit/sources tests/contract/exclusion_policy tests/contract/source_publication/test_no_network_in_transaction.py -q`

Run: `uv run pytest tests/integration/exclusion_policy/test_source_publication_enforcement.py tests/integration/exclusion_policy/test_canonical_read_enforcement.py tests/integration/exclusion_policy/test_policy_publication_race.py tests/integration/source_publication -m local_stack -q`

Run: `uv run poe canonical-core-test`

- [ ] **Step 5: Commit mandatory backend enforcement**

```powershell
git add src/personal_os/exclusion_policy/enforcement.py src/personal_os/sources packages/postgresql-source-store/src/postgresql_source_store tests/unit/exclusion_policy tests/unit/sources tests/contract/exclusion_policy tests/integration/exclusion_policy
git commit -m "feat: enforce active exclusion policy"
```

---

### Task 12: Reconcile the active revision and isolate policy-transition intents

**Files:**
- Create: `src/personal_os/exclusion_policy/reconciliation.py`
- Create: `packages/postgresql-source-store/src/postgresql_source_store/policy_reconciliation.py`
- Create: `apps/worker/src/workflow_worker/policy_reconciliation_workflow.py`
- Modify: `apps/worker/src/workflow_worker/policy_workflow_runtime.py`, `command.py`, `projection_dispatch_runtime.py`, `projection_workflow_starter.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/projection_intents.py`
- Modify: `src/personal_os/sources/projection_dispatch.py`
- Create: `tests/unit/exclusion_policy/test_reconciliation.py`
- Create: `tests/unit/postgresql_source_store/test_policy_reconciliation.py`
- Create: `tests/unit/workflow_worker/test_policy_reconciliation_workflow.py`
- Modify: `tests/unit/workflow_worker/test_projection_dispatch_runtime.py`, `test_projection_workflow_starter.py`
- Create: `tests/integration/exclusion_policy/test_policy_reconciliation.py`, `test_policy_transition_intent_isolation.py`

**Interfaces:**
- Produces workflow ID `exclusion-policy-reconciliation/{workspace_id}/{policy_revision_id}`, immutable `PolicyEvaluation` rows and deterministic policy-origin projection intents.
- Existing dispatch claims source-event origins only. Policy-origin intents remain pending and visible to operations until the later projection child installs `policy-projection-transition/{workspace_id}/{policy_revision_id}/{source_id}`.

- [ ] **Step 1: Write failing retry, stale-revision and exactly-once intent tests**

Cover allowed→excluded, excluded→allowed, unchanged, indeterminate→denied, source with null current version, source changed during scan, publication of a newer policy during reconciliation, retry after partial batches, duplicate workflow start and two workers. Assert one evaluation per `(revision, source, subject_event_sequence)` and one intent per `(revision, source, projection_kind)` where a transition requires it.

- [ ] **Step 2: Run focused tests and confirm missing reconciliation/isolation behavior**

Run: `uv run pytest tests/unit/exclusion_policy/test_reconciliation.py tests/unit/postgresql_source_store/test_policy_reconciliation.py tests/unit/workflow_worker/test_policy_reconciliation_workflow.py tests/unit/workflow_worker/test_projection_dispatch_runtime.py tests/unit/workflow_worker/test_projection_workflow_starter.py -q`

- [ ] **Step 3: Implement bounded idempotent reconciliation**

```python
@dataclass(frozen=True, slots=True)
class ReconciliationInput:
    contract: Literal["exclusion_policy_reconciliation/v1"]
    workspace_id: UUID
    policy_revision_id: UUID
    source_checkpoint_event_sequence: int


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    policy_revision_id: UUID
    source_id: UUID
    subject_event_sequence: int
    raw_decision: RawPolicyDecision
    enforced_decision: EnforcedPolicyDecision
```

Each activity batch reads the active revision and a stable source range, evaluates, inserts or verifies only the exact immutable identity, derives transitions from the prior evaluation and inserts deterministic Qdrant/Neo4j intents with `ON CONFLICT DO NOTHING`. Before every write, confirm the target revision is still active and the subject event sequence still matches; otherwise re-read/replan. Heartbeat after every committed batch and continue as new after 20 batches or 10,000 sources. Record only closed transition counters/reconciliation lag and emit completion/failure diagnostics after durable state transitions.

Change claim SQL to require `origin_kind='source_event'`. A contract test must prove policy-transition rows cannot reach `SourceIngestionWorkflow`; do not mark them terminal merely because their later consumer is absent.

- [ ] **Step 4: Run workflow, PostgreSQL and dispatcher regression gates**

Run: `uv run pytest tests/unit/exclusion_policy/test_reconciliation.py tests/unit/postgresql_source_store/test_policy_reconciliation.py tests/unit/workflow_worker -q`

Run: `uv run pytest tests/integration/exclusion_policy/test_policy_reconciliation.py tests/integration/exclusion_policy/test_policy_transition_intent_isolation.py tests/integration/projection_dispatch -m local_stack -q`

- [ ] **Step 5: Commit reconciliation and intent isolation**

```powershell
git add src/personal_os/exclusion_policy/reconciliation.py src/personal_os/sources/projection_dispatch.py packages/postgresql-source-store/src/postgresql_source_store apps/worker/src/workflow_worker tests/unit/exclusion_policy tests/unit/postgresql_source_store tests/unit/workflow_worker tests/integration/exclusion_policy
git commit -m "feat: reconcile exclusion policy transitions"
```

---

### Task 13: Add cross-surface acceptance, security and performance gates

**Files:**
- Create: `tests/end_to_end/exclusion_policy/policy-publication.spec.ts`
- Create: `tests/performance/test_exclusion_policy_performance.py`
- Create: `tests/contract/exclusion_policy/test_cross_language_vectors.py`, `test_sensitive_policy_contract.py`, `test_no_policy_bypass.py`
- Create: `tests/integration/exclusion_policy/test_policy_backup_restore.py`, `test_policy_failure_recovery.py`
- Modify: canonical-core acceptance/bootstrap fixtures and operations command under `tools/`
- Modify: `package.json`, `playwright.config.ts`, `pyproject.toml`
- Create: `.github/workflows/exclusion-policy-acceptance.yml`

**Interfaces:**
- Adds `poe exclusion-policy-test`, root `test:e2e:exclusion-policy` and a CI workflow using a unique guarded Compose project.
- Phase 1 bootstrap/smoke explicitly initializes trust and publishes a signed empty policy before any canonical source write/read acceptance path.

- [ ] **Step 1: Write failing full-journey, recovery, leakage and budget tests**

The browser journey logs in, creates a draft, previews an empty and a deny rule, publishes with confirmation and sees active status. Integration tests restore policy/keyset/evaluation state, prove a restore without the private signing file is inspectable only through offline tooling and cannot start the API, publish or serve content operations, recover ambiguous commit, tolerate Temporal outage after publication and deny when PostgreSQL/signature state is unavailable.

Static and dynamic leakage tests scan Python/TypeScript logs, generated artifacts, built Web/plugin bundles and HTTP errors for sentinel locator, operand, signature and key values. Bypass tests reject direct R2/canonical-source access from API, MCP and worker composition outside the approved guarded adapter.

- [ ] **Step 2: Run new gates and confirm they fail before orchestration is wired**

Run: `uv run pytest tests/contract/exclusion_policy/test_cross_language_vectors.py tests/contract/exclusion_policy/test_sensitive_policy_contract.py tests/contract/exclusion_policy/test_no_policy_bypass.py tests/integration/exclusion_policy/test_policy_backup_restore.py tests/integration/exclusion_policy/test_policy_failure_recovery.py -m local_stack -q`

Run: `pnpm run test:e2e:exclusion-policy`

- [ ] **Step 3: Implement acceptance fixtures, exact budgets and CI orchestration**

Performance tests use a deterministic 10,000-source fixture and document CPU/RAM/PostgreSQL settings. Record p50/p95/max and assert the spec limits: evaluator p95 <= 5 ms, verify p95 <= 50 ms, preview <= 30 seconds and reconciliation <= 300 seconds. Warmup is explicit and excluded from measurement; tests fail, never skip, when their required local stack/browser is unavailable.

Add:

```toml
[tool.poe.tasks.exclusion-policy-test]
cmd = "pytest tests/unit/exclusion_policy tests/unit/api_runtime tests/unit/postgresql_source_store tests/unit/workflow_worker tests/contract/exclusion_policy tests/contract/api tests/integration/exclusion_policy -m \"not r2_live\" -q"
```

The acceptance workflow generates disposable test signing keys inside the job, never echoes them, starts PostgreSQL/Temporal with a unique `knowledge-ci-*` project name, runs migration/unit/contract/integration/performance/Web/plugin/E2E gates, uploads only redacted test reports and destroys the exact guarded project in `always()` cleanup. Completion also requires recorded Desktop and Mobile Obsidian reference-device verification of initial trust, snapshot verification, rotation, offline cache and Vault preservation; absence of either record blocks the final handoff.

- [ ] **Step 4: Run the feature and full repository verification gates**

Run: `uv run poe exclusion-policy-test`

Run: `pnpm run test:e2e:exclusion-policy`

Run: `uv run pytest tests/performance/test_exclusion_policy_performance.py -m local_stack -q`

Run: `uv run poe verify`

Expected: every command exits `0`; performance output records the reference-host evidence and no test is silently skipped.

- [ ] **Step 5: Commit acceptance and CI gates**

```powershell
git add tests/end_to_end/exclusion_policy tests/performance/test_exclusion_policy_performance.py tests/contract/exclusion_policy tests/integration/exclusion_policy tools package.json playwright.config.ts pyproject.toml .github/workflows/exclusion-policy-acceptance.yml
git commit -m "test: gate exclusion policy publication"
```

---

### Task 14: Publish operations, canonical status and the single implementation handoff

**Files:**
- Create: `docs/operations/exclusion-policy-publication.md`
- Modify: `docs/07-SECURITY_PRIVACY_AND_GOVERNANCE.md`, `docs/14-OBSERVABILITY_EVALUATION_AND_TEST_STRATEGY.md`, `docs/20-IMPLEMENTATION_PLAN.md`
- Modify: `docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md`
- Create: `docs/handoff/2026-08-17-exclusion-policy-publication.md`
- Modify only if implementation review defers an item: `docs/handoff/BACKLOG.md`

**Interfaces:**
- The operations guide is the living runbook for initial trust, explicit empty-policy publication, preview/publish, key rotation, degraded states, reconciliation inspection, backup/restore and rollback-by-new-revision.
- The handoff is the one snapshot required by `AGENTS.md`, containing last commit SHA, gate evidence, spec interpretation decisions, reviewed deferred rulings and next actions.

- [ ] **Step 1: Write failing documentation contract tests**

Add or extend documentation tests that require exact commands, environment names, recovery limits, workflow IDs, lock order, no-private-key-in-backup warning, rollback-as-new-revision semantics and all acceptance gate names. Assert canonical docs no longer describe child 3 as merely planned after implementation is complete.

- [ ] **Step 2: Run documentation tests and confirm the missing runbook/status failures**

Run: `uv run pytest tests/contract/test_bootstrap_documentation.py tests/unit/tools/test_canonical_core_operations.py tests/contract/exclusion_policy -q`

- [ ] **Step 3: Write the living runbook and update canonical status**

Document copy-paste-safe commands without real secret values. Include detection and recovery for invalid signer, unavailable PostgreSQL/Temporal, stale preview, plugin integrity failure, reconciliation lag and restore without private key. State clearly that exclusion changes are published as a new revision; immutable history is not edited or deleted.

Update the spec status to implemented only after all acceptance gates pass. Update `docs/20-IMPLEMENTATION_PLAN.md` child 3 evidence with exact migration head, feature-gate commands and implementation commit range; do not claim child 4 or projection consumers.

- [ ] **Step 4: Perform final review, write exactly one handoff and verify repository state**

Run: `git diff --check`

Run: `(Get-Content AGENTS.md).Count; (Get-Content CLAUDE.md).Count`

Run: `uv run poe exclusion-policy-test && pnpm run test:e2e:exclusion-policy && uv run poe verify`

Run: `git status --short; git diff --stat; git diff -- docs/07-SECURITY_PRIVACY_AND_GOVERNANCE.md docs/14-OBSERVABILITY_EVALUATION_AND_TEST_STRATEGY.md docs/20-IMPLEMENTATION_PLAN.md docs/operations/exclusion-policy-publication.md`

Record exact command output summaries and the last implementation SHA in `docs/handoff/2026-08-17-exclusion-policy-publication.md`. The spec already defers mutation testing, so record its ruling in the handoff and add exactly one `2026-08-17 | exclusion-policy | mutation testing` index line to `BACKLOG.md`. If implementation review accepts another deferred item, add exactly one corresponding index line per item. Keep the handoff under roughly 400 lines and link to the living runbook rather than copying it.

- [ ] **Step 5: Commit documentation and handoff**

```powershell
git add docs/07-SECURITY_PRIVACY_AND_GOVERNANCE.md docs/14-OBSERVABILITY_EVALUATION_AND_TEST_STRATEGY.md docs/20-IMPLEMENTATION_PLAN.md docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md docs/operations/exclusion-policy-publication.md docs/handoff/2026-08-17-exclusion-policy-publication.md docs/handoff/BACKLOG.md tests/contract/test_bootstrap_documentation.py tests/unit/tools/test_canonical_core_operations.py
git commit -m "docs: complete exclusion policy publication"
```

---

## Final Verification Checklist

- [ ] `git status --short` shows only intentional files before the final documentation commit and is clean afterward.
- [ ] `git diff --check` exits `0`.
- [ ] `uv run alembic heads` reports exactly the expected single head.
- [ ] `uv run poe exclusion-policy-test` exits `0` with no silent skips.
- [ ] `pnpm run test:e2e:exclusion-policy` exits `0`.
- [ ] `uv run pytest tests/performance/test_exclusion_policy_performance.py -m local_stack -q` exits `0` and records all four budgets.
- [ ] `uv run poe verify` exits `0`.
- [ ] Python/TypeScript evaluate every shared golden case to identical raw/enforced decisions and canonical signed bytes.
- [ ] Invalid/missing policy, signature, subject evidence or dependency state denies before canonical bytes are read or published.
- [ ] Source/policy concurrency tests prove the fixed lock order and no stale revision commit.
- [ ] Preview and reconciliation crash tests prove no partial correctness state is exposed.
- [ ] Existing source-event projection intents still dispatch; policy-transition intents remain pending and cannot reach the source workflow.
- [ ] Backup/restore preserves canonical policy state and explicitly excludes private signing keys.
- [ ] Web/plugin production bundles and diagnostics contain none of the sentinel sensitive values.
- [ ] Desktop and Mobile reference-device evidence covers first trust, rotation, offline cache, integrity failure and Vault preservation.
- [ ] `docs/handoff/2026-08-17-exclusion-policy-publication.md` is the only handoff for this implementation plan and every accepted deferred item has exactly one `BACKLOG.md` line.
