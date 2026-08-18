# Exclusion Policy Publication Design

**Date:** 2026-08-17

**Status:** Implemented (2026-08-17); runtime contract in force — implementation record: `docs/handoff/2026-08-17-exclusion-policy-publication.md`, operator runbook: `docs/operations/exclusion-policy-publication.md`. Reference-device verification records (spec 23.5/25) remain outstanding and block final completion.

**Owning program:** Phase 2 — Obsidian sync, child 3

**Depends on:** API runtime/contract foundation and Web authentication/device authorization

## 1. Objective

Create one server-owned exclusion-policy boundary that can answer whether a
source may cross a backend content boundary, preview the exact impact of a
draft before publication, publish immutable revisions safely under races and
lost acknowledgements, distribute a tamper-evident snapshot to Obsidian
Desktop/Mobile, and force every backend path to enforce the active revision.

This child is complete when an authenticated administrator can explicitly
publish revision 1, backend and plugin evaluate the same bounded rule set, a
stale or forged plugin snapshot cannot authorize work, and an allow-to-deny
change blocks content immediately while durable projection cleanup converges.

The policy decision in this child is deliberately binary at the product level:

```text
allowed | excluded
```

The evaluator also exposes `indeterminate` as an integrity outcome. Enforcement
maps it to deny. `local_only`, `cloud_ok`, provider selection, retention and
metadata-property predicates remain later-phase contracts.

## 2. Canonical context

The design preserves these existing decisions:

- PostgreSQL is the correctness authority for policy, source identity, audit
  and durable intent.
- Cloudflare R2 remains the only canonical byte store; policy state and signed
  snapshots do not move content bytes into PostgreSQL.
- Admin Dashboard owns exclusions. Plugin filtering only avoids unnecessary
  work and bandwidth.
- Unknown policy, missing evidence or evaluation failure denies by default.
- Qdrant and Neo4j are rebuildable projections and may contain no retrievable
  residue for a denied source.
- Temporal owns durable preview/reconciliation orchestration and retries.
- Redis is neither policy authority nor a correctness cache.
- API, MCP, Web and worker adapters call shared domain services rather than
  implementing rule behavior.
- Raw content, path, locator, title, rule operand, secret and crypto exception
  text never enter telemetry.

Children 1 and 2 already provide the API envelope, closed error registry,
request/trace context, OpenAPI/generated-client gates, Web sessions, CSRF,
five-minute recent re-authentication, device scopes, plugin authentication and
exact secret-file loading. This child extends those contracts; it does not
replace them.

## 3. Approved decisions

1. Scope Phase 2 to exclusion only; defer AI access classes.
2. Use deny-only rules with default allow. Any definite rule match excludes.
3. Require an explicit initial publication. There is no unsigned or implicit
   revision zero.
4. Give plugin snapshots no TTL. Protect them with monotonic revision checks,
   exact hash/signature verification and backend re-evaluation.
5. Use a dedicated Ed25519 signing key lifecycle, never authentication key
   material.
6. Let preview report `matched`, `not_matched` and `indeterminate` honestly for
   the canonical evidence available at the preview checkpoint.
7. Keep policy state separate from `sources.sync_state`.
8. Store immutable relational revisions and normalized rules; do not use one
   opaque JSONB policy document or an external policy engine.
9. Activate a new revision synchronously in PostgreSQL and reconcile existing
   sources asynchronously in bounded Temporal batches.
10. Use one pure evaluator contract with shared Python/TypeScript golden
    fixtures.

## 4. Scope

### 4.1 Included

- One mutable working draft per workspace with optimistic concurrency.
- Exact source ID, folder prefix, bounded path glob, extension, media type,
  maximum size and source-type exclusion rules.
- Source-subject and rule normalization shared by backend and plugin.
- Asynchronous impact preview over a stable canonical source checkpoint.
- Immutable, idempotent publication and active-revision pointer swap.
- Ed25519-signed RFC 8785 canonical JSON snapshots.
- Authenticated, cross-signed public-key rotation chain.
- Server-side policy decisions and internal decision evidence.
- Enforcement hooks for sync, publication, read, manifest, ingestion,
  projection, rebuild and retrieval paths.
- Durable policy reconciliation and projection transition intents.
- Minimal `/admin/policy` Web flow and plugin snapshot cache/verification.
- Migrations, OpenAPI/generated client, tests, diagnostics and operational key
  commands needed by this child.

### 4.2 Excluded

- `local_only`, `cloud_ok`, provider routing or provider fallback.
- Metadata/property predicates before Phase 3 canonical metadata exists.
- Retention, legal hold or physical canonical-object garbage collection.
- Regex, ordered rules, priority, explicit allow overrides, negation, arbitrary
  expressions, CEL or Rego.
- Cloudflare Worker or client-authoritative admission.
- KMS/HSM integration, client-side encryption or use of authentication master
  keys for signatures.
- Web source editor, sync journal, multipart, locator lifecycle and conflict UI
  owned by later Phase 2 children.
- Mutation testing as a Phase 2 gate.

## 5. System boundary

```text
Web Admin                         Obsidian plugin
  draft / preview / publish        trusted keyset + snapshot cache
              \                    /
               authenticated FastAPI
                         |
            policy application services
        draft | preview | publish | snapshot
                         |
                pure PolicyEvaluator
                         |
       PostgreSQL canonical policy + durable intents
                         |
               Temporal reconciliation
                         |
             rebuildable projection intents
```

The domain is divided into focused units:

- `PolicyEvaluator` validates normalized contracts and returns deterministic
  decision evidence without I/O.
- `PolicyDraftService` owns one working draft and compare-and-swap edits.
- `PolicyPreviewService` creates and reads bounded preview jobs.
- `PolicyPublicationService` owns exact replay, concurrency, signing and the
  active-pointer transaction.
- `PolicySnapshotService` returns persisted immutable snapshot/keyset envelopes.
- `PolicyEnforcementService` loads the active revision, evaluates a subject and
  returns internal evidence with enforced deny semantics.
- `PolicyReconciliationWorkflow` evaluates existing sources and emits durable
  projection transitions in bounded batches.

PostgreSQL, Temporal, FastAPI and cryptography libraries implement ports around
these services. The domain package imports none of them.

## 6. Closed rule model

### 6.1 Common rule fields

Every rule has exactly:

```text
rule_id                UUID, stable within draft and published revision
rule_kind              closed enum
normalized_operand     represented by exactly one typed operand column
semantic_fingerprint   lowercase SHA-256 over kind + normalized operand
```

There is no rule name, arbitrary description, priority, enabled flag or action.
Draft editing adds/removes a rule. Published rules are immutable. Duplicate
semantic fingerprints inside one draft/revision are rejected.

A revision contains zero through 256 rules. An empty published revision has
default decision `allowed`.

### 6.2 Rule kinds

| Rule kind | Operand | Excludes when |
|---|---|---|
| `exact_source_id` | non-nil UUID | subject source ID equals operand |
| `folder_prefix` | normalized relative folder | locator begins at that exact segment boundary |
| `path_glob` | bounded normalized glob | normalized locator matches the closed glob grammar |
| `extension` | lowercase ASCII suffix beginning `.` | final filename ends with suffix, ASCII case-insensitive |
| `media_type` | exact canonical MIME or one top-level family | media type equals exact value or family |
| `maximum_size` | integer bytes | `size_bytes > maximum_size_bytes` |
| `source_type` | current closed source type | source type equals operand |

The Phase 2 maximum object size remains 100 MiB. `maximum_size_bytes` is in the
inclusive range `0..104857600`; zero excludes every non-empty subject but does
not exclude a zero-byte subject.

An extension is 2–64 ASCII characters, begins with one dot and may contain
lowercase letters, digits, dots, hyphens or underscores. Multi-suffix values
such as `.tar.gz` are valid. Media type follows the existing canonical MIME
grammar; a family operand has exactly `type/*` and no parameters.

### 6.3 Locator normalization

A Vault locator is normalized before evaluation:

1. Input is valid Unicode and normalized to NFC.
2. Separator is `/`; backslash input is rejected rather than silently changed.
3. Locator is relative: no leading/trailing slash, URI scheme, drive letter or
   authority.
4. Empty, `.`, `..`, NUL and control-character segments are rejected.
5. Maximum encoded locator is 4,096 UTF-8 bytes, 256 segments and 255 UTF-8
   bytes per segment.
6. Matching is case-sensitive except the explicitly ASCII-insensitive
   extension rule.
7. Percent signs and Unicode characters are literal; there is no URL decode,
   locale collation or platform-dependent case folding.

A folder prefix contains at least one normalized segment and no trailing slash.
`private` matches `private/a.md` and not `private-notes/a.md`.

### 6.4 Bounded glob grammar

Glob operands are normalized relative paths with these additions:

- `*` is the only wildcard inside a segment and matches zero or more Unicode
  code points other than `/`.
- `**` is special only when it is the complete segment and matches zero or more
  whole path segments.
- Regex syntax, `?`, character classes, braces, negation and escape processing
  are unsupported and rejected.
- A glob is at most 1,024 UTF-8 bytes, 64 segments and 16 wildcard tokens.

The evaluator compiles this grammar into bounded segment matching; it does not
translate untrusted patterns into a backtracking regular expression.

## 7. Policy subject and evaluation

```text
PolicySubject
  workspace_id
  source_id?             required only by exact-source rules
  normalized_locator?    required by folder/glob/extension rules
  source_type?           required by source-type rules
  media_type?            required by media-type rules
  size_bytes?            required by maximum-size rules
```

`workspace_id` is always required and server-derived at public boundaries. A
public request containing invalid evidence fails input validation. A canonical
legacy source that genuinely lacks a field produces missing evidence instead
of inventing a value.

The pure evaluator uses this precedence:

```text
one or more definite matches                  raw = excluded
no match and a required subject field missing raw = indeterminate
no match and no required field missing         raw = allowed

enforced(excluded)      = denied
enforced(indeterminate) = denied
enforced(allowed)       = allowed
```

A definite match wins over unrelated missing evidence. With no rules, every
otherwise valid subject is `allowed` even if optional fields are absent.

Decision evidence contains sorted matching rule IDs, sorted missing field names,
the revision identity and a subject fingerprint. It never contains rule
operands. The subject fingerprint is SHA-256 over a closed canonical structure;
it remains internal and is never logged because path-derived hashes may be
guessable.

Python and TypeScript must consume the same JSON golden corpus. A contract hash
identifies normalization, glob and evaluation semantics. Changing those
semantics requires a new snapshot contract version, not an in-place behavior
change.

## 8. PostgreSQL schema

All tables live in schema `knowledge`, carry `workspace_id` where applicable
and use named constraints/indexes. Published policy artifacts use append-only
mutation-rejection triggers.

### 8.1 `workspace_policy_state`

```text
workspace_id PK/FK
active_policy_revision_id nullable
active_revision_number >= 0
created_at / updated_at
```

There is exactly one row per workspace. `active_revision_number = 0` iff the
active pointer is null. The active pointer references a revision from the same
workspace. The row is the serialization point for publication.

Migration/bootstrap creates this row and an empty draft for every existing and
future workspace, but does not publish or sign a policy implicitly.

### 8.2 `policy_drafts` and `policy_draft_rules`

```text
policy_drafts
  policy_draft_id PK
  workspace_id unique
  draft_version >= 1
  base_policy_revision_id nullable
  created_by_user_id / updated_by_user_id
  created_at / updated_at

policy_draft_rules
  policy_draft_id / rule_id PK
  rule_kind
  source_id_operand nullable
  text_operand nullable
  size_bytes_operand nullable
  semantic_fingerprint
```

A check constraint maps every kind to exactly one populated typed operand.
Draft replacement locks the draft row and requires exact
`expected_draft_version`; success increments it once.

### 8.3 `source_policies` and `policy_rules`

```text
source_policies
  policy_revision_id PK
  workspace_id / revision_number unique
  parent_policy_revision_id nullable
  default_decision = allowed
  source_checkpoint_event_sequence
  policy_preview_id unique
  publication_idempotency_key
  request_fingerprint
  snapshot_contract
  snapshot_payload_bytes
  snapshot_payload_sha256
  signing_key_id
  signature_bytes
  published_by_user_id / published_at

policy_rules
  policy_revision_id / rule_id PK
  rule_kind
  typed operand columns
  semantic_fingerprint
```

`(workspace_id, publication_idempotency_key)` and
`(workspace_id, revision_number)` are unique. Parent is null only for revision
1; every later parent is revision number minus one in the same workspace.
Payload hash is lowercase SHA-256 and signature is exactly 64 bytes.

### 8.4 Preview tables

```text
policy_previews
  policy_preview_id PK
  workspace_id / policy_draft_id
  draft_version / draft_sha256
  base_policy_revision_id nullable
  source_checkpoint_event_sequence
  state = pending | leased | running | ready | failed | expired | consumed
  impact counters
  impact_digest nullable until ready
  attempt_count / available_at
  lease_token / leased_until nullable
  safe_error_code nullable
  created_by_user_id
  created_at / ready_at / expires_at / consumed_at nullable

policy_preview_results
  policy_preview_id / source_id PK
  previous_raw_decision / previous_enforced_decision
  proposed_raw_decision / proposed_enforced_decision
  proposed_match_state
  impact_class
  matched_rule_ids
  missing_fields
  subject_fingerprint
```

Preview rows double as a leased outbox for deterministic Temporal start. Raw
path, title and rule operands are not copied into result rows. The Admin read
service joins current display fields only after proving the source still
belongs to the workspace.

### 8.5 Evaluation and reconciliation

```text
policy_evaluations
  policy_evaluation_id PK
  policy_revision_id / source_id
  subject_event_sequence
  raw_decision / enforced_decision
  matched_rule_ids / missing_fields
  subject_fingerprint
  evaluated_at

policy_reconciliation_intents
  policy_reconciliation_intent_id PK
  workspace_id / policy_revision_id unique
  workflow_id unique
  state / attempt_count / available_at
  lease_token / leased_until / dispatched_at nullable
  safe_error_code nullable
  created_at / updated_at
```

Evaluations are insert-once evidence for one source state under one revision.
`(policy_revision_id, source_id, subject_event_sequence)` is unique, so a later
locator/version change under the same policy creates a new evaluation instead
of overwriting history. An idempotent replay verifies exact equality; it never
overwrites a different result.

The existing `projection_intents` table gains an origin discriminator so policy
transitions do not fabricate source-edit events:

```text
origin_kind = source_event | policy_transition
event_id nullable
policy_revision_id nullable
```

Exactly one origin reference is populated. Existing rows backfill
`source_event`. Policy-transition uniqueness is
`(policy_revision_id, source_id, projection_kind)`; the existing source-event
uniqueness remains intact.

### 8.6 Public-key history

```text
policy_signing_keys
  signing_key_id PK
  workspace_id
  algorithm = Ed25519
  public_key_bytes unique
  introduced_keyset_revision
  created_at

policy_keysets
  policy_keyset_id PK
  workspace_id / keyset_revision unique
  parent_keyset_revision nullable
  canonical_payload_bytes / payload_sha256
  created_by_user_id nullable
  created_at

policy_keyset_signatures
  policy_keyset_id / signing_key_id PK
  signature_bytes
```

Signing-key rows and keyset envelopes are immutable. Current/staged/retired
meaning is declared by the latest keyset payload rather than mutating key rows.
Private keys never enter PostgreSQL.

### 8.7 Migration gates

The migration must pass:

- empty-database upgrade;
- Phase 1 and Child 2 fixture upgrade;
- exact-head application smoke;
- reflection of columns, FKs, checks, uniques, partial indexes and triggers;
- preservation/backfill of existing projection intents; and
- deterministic downgrade to the Child 2 head.

Downgrade refuses outside an explicit destructive test/operator gate when a
published policy, preview, evaluation, keyset or policy-origin intent exists.

## 9. Draft lifecycle

The Admin read returns current published revision metadata, the working draft
and exact `draft_version`. A draft update sends the complete desired rule list,
not patch operations. The backend validates and normalizes the entire list,
computes its semantic hash and atomically replaces child rows under draft
compare-and-swap.

Full replacement keeps conflict semantics simple across two browser tabs:

- exact expected version succeeds and increments once;
- stale expected version returns a conflict;
- the server never merges or drops rules silently;
- identical replacement remains an explicit successful edit and increments the
  draft version, so any existing preview becomes stale.

The initial draft is empty with null base. After publication, the same draft ID
is rebased to the new policy revision, retains the just-published rule set and
increments its version in the publication transaction.

Rule values may be shown in the authenticated Admin UI but never in error
details, diagnostics or audit payloads.

## 10. Asynchronous preview

`POST /api/admin/exclusion-policy/previews` creates one pending preview bound to:

```text
workspace_id
policy_draft_id
draft_version
draft_sha256
base_policy_revision_id
source_checkpoint_event_sequence
actor_user_id
```

It returns HTTP 202 with `policy_preview_id`, state and polling guidance. A
leased dispatcher starts deterministic workflow:

```text
exclusion-policy-preview/{workspace_id}/{policy_preview_id}
```

Workflow input contains only contract tag and opaque IDs/checkpoint. Activities
load rules and source subjects from PostgreSQL in pages of 500. Raw locator,
title and rule values never enter Temporal history.

The source checkpoint is the workspace's last assigned canonical source-event
sequence. Every future mutation of a field used by policy must emit such an
event. One preview activity opens one repeatable-read PostgreSQL transaction,
verifies the current sequence still equals the captured checkpoint, streams
subjects in pages of 500, heartbeats between pages and writes the complete
result set in that same transaction. A crash rolls back the whole result and
the activity retries from the captured inputs; it never composes results from
different database snapshots.

Impact classes are:

```text
newly_excluded
still_excluded
newly_allowed
still_allowed
indeterminate
```

`proposed_match_state` is the closed preview vocabulary
`matched | not_matched | indeterminate`, mapping respectively to proposed raw
decision `excluded | allowed | indeterminate`.

`indeterminate` is reported separately even though its effective decision is
deny. `impact_digest` is SHA-256 over sorted tuples of opaque source ID,
previous/proposed effective decision and impact class. It contains no title,
path or operand.

When no active revision exists, the previous raw state is `indeterminate` and
its enforced state is deny. An initial empty policy therefore previews existing
valid sources as `newly_allowed`, making first-publication impact explicit.

Preview result reads first verify that the current workspace source checkpoint
still equals the preview checkpoint. Only then may the service join current
title/locator for Admin display. A mismatch returns stale preview rather than
showing display data from a source state that was not evaluated.

A ready preview expires 15 minutes after `ready_at`. Pending/running previews
have a bounded 15-minute execution deadline. Failure is retryable only through
a new preview; a failed/expired row cannot publish. Preview result pages use a
stable `(impact_class, source_id)` cursor and at most 200 items.

## 11. Publication command

Publication requires:

- authenticated Admin scope;
- valid CSRF proof;
- password recent re-authentication no older than five minutes;
- exact phrase `PUBLISH EXCLUSION POLICY`;
- ready, unexpired preview ID;
- expected draft version and active policy revision;
- printable opaque idempotency key of 1–200 characters.

The request fingerprint covers contract tag, workspace/actor identity, preview
ID and digest, draft identity/version/hash, expected active revision and exact
confirmation semantics. It excludes request/trace IDs and the idempotency key
itself. Serialization follows the repository's closed canonical fingerprint
rules.

### 11.1 Transaction

One `READ COMMITTED` transaction:

1. Acquires transaction advisory lock for publication idempotency identity.
2. Resolves exact replay or rejects identity reuse with a different fingerprint.
3. Locks `workspace_policy_state`, then the draft and preview rows.
4. Rechecks actor/workspace ownership, confirmation, preview state/expiry,
   draft version/hash, active parent and source checkpoint.
5. Allocates `active_revision_number + 1` and a UUIDv7 revision ID.
6. Builds canonical snapshot payload from the already-normalized draft.
7. Signs locally with the configured current Ed25519 key and verifies the
   produced signature with its derived public key.
8. Inserts immutable revision, rules and signature bytes.
9. Swaps the active pointer and increments the active revision number.
10. Inserts one reconciliation intent and one append-only publication audit.
11. Marks the preview consumed and rebases/increments the draft.
12. Commits once.

No R2, Temporal, HTTP or provider call occurs in the transaction. Ed25519
signing is bounded local CPU work over at most 256 KiB and is permitted while
the policy-state row is locked.

### 11.2 Replay and ambiguous acknowledgement

Exact replay returns the original revision number, IDs, hash, key ID,
publication time and reconciliation status without signing or inserting again.
A different fingerprint under the same idempotency key is terminal misuse.

After an ambiguous commit acknowledgement, the service discards the connection
and performs bounded evidence lookup on a new connection by workspace,
idempotency key and fingerprint. Proven committed evidence returns exact replay;
proven absence permits one normal retry. PostgreSQL unavailability returns
retryable `exclusion_policy_commit_outcome_unknown`; it never assumes rollback.

Rejected publication after trusted actor/workspace resolution appends
`exclusion_policy.publish_rejected` with a closed reason and safe digest only.

## 12. Signed snapshot contract

The persisted envelope shape is:

```json
{
  "payload": {
    "contract": "exclusion_policy_snapshot/v1",
    "workspace_id": "UUID",
    "policy_revision_id": "UUID",
    "revision_number": 1,
    "parent_policy_revision_id": null,
    "published_at": "2026-08-17T00:00:00.000000Z",
    "default_decision": "allowed",
    "evaluator_contract_sha256": "LOWERCASE_SHA256",
    "rules": []
  },
  "payload_sha256": "LOWERCASE_SHA256",
  "signature": {
    "algorithm": "Ed25519",
    "key_id": "ed25519-sha256-BASE64URL",
    "value": "BASE64URL_WITHOUT_PADDING"
  }
}
```

Rules are sorted by lowercase textual `rule_id`; object properties follow RFC
8785 JCS. All strings have already passed domain normalization. No float,
negative zero, NaN, Infinity, duplicate property or lone surrogate can enter
the payload. Timestamp is UTC with exactly six fractional digits and `Z`.

Each rule object contains `rule_id`, `rule_kind` and exactly one named typed
operand: `source_id`, `folder_prefix`, `path_glob`, `extension`, `media_type`,
`maximum_size_bytes` or `source_type`. It contains no semantic fingerprint,
database ID, label or display string. Snapshot validation enforces the same
closed kind-to-operand mapping as PostgreSQL.

The signed message is:

```text
ASCII("exclusion-policy-snapshot/v1") || 0x00 || JCS_UTF8(payload)
```

`payload_sha256` hashes only `JCS_UTF8(payload)`. The detached signature is 64
bytes encoded base64url without padding. `key_id` is
`ed25519-sha256-` plus base64url-without-padding SHA-256 of the raw 32-byte
public key. The complete encoded response is at most 256 KiB.

The server returns persisted bytes/signature; it does not regenerate or resign
a published revision on read.

## 13. Signing keys and trust rotation

### 13.1 Private-key boundary

Policy signing uses a dedicated Ed25519 key. It is never derived from, wrapped
by or stored beside values produced from the authentication master key.

The current signer is an unencrypted PKCS#8 PEM containing exactly one Ed25519
private key in an exact file beneath the configured secret root. Existing
secret-file size, encoding, symlink/root and permission checks apply. Startup
derives the public key and `key_id`, then proves it equals the current key in
the latest canonical keyset. Missing, malformed, wrong-algorithm, duplicate or
mismatched material prevents the API from binding a socket.

Python uses the already pinned `cryptography==49.0.0`. The plugin must use one
reviewed, pinned, pure TypeScript/mobile-compatible Ed25519 implementation; the
implementation plan owns exact version, license, audit history, transitive
dependency and production-bundle review. Hand-written curve arithmetic is
prohibited.

### 13.2 Initial trust

An offline internal CLI initializes keyset revision 1 before policy-enabled API
startup. It generates or imports a key, refuses to overwrite an existing secret
file, writes restrictive permissions where the platform supports them, stores
only public metadata in PostgreSQL and prints only the public fingerprint.

The first plugin keyset is trusted only immediately after authenticated device
onboarding over the configured HTTPS origin. It is bound to the credential's
workspace and stored as a non-secret trust anchor. A random unauthenticated
keyset endpoint, snapshot-embedded public key or URL parameter can never create
trust.

### 13.3 Rotation chain

Rotation is staged:

1. Generate/import a new private key while the old current private key remains
   available.
2. Publish an immutable keyset revision containing old current plus new staged
   public keys. Require an old-current signature and proof-of-possession
   signature from the new key.
3. Let authenticated clients fetch and verify the chain.
4. Publish a later cross-signed keyset making the new key current.
5. Switch API signer configuration only after that keyset commits.
6. Publish a later keyset retiring the old key after the operating overlap.

At most one key is current and at most four keys are non-retired in the latest
keyset. Historical keysets/signatures remain append-only. The keyset API returns
at most 16 ordered envelopes per page after a client-supplied known revision,
so a long-offline device can verify every link without an unbounded response.

The canonical keyset payload is workspace-bound:

```json
{
  "contract": "exclusion_policy_keyset/v1",
  "workspace_id": "UUID",
  "keyset_revision": 2,
  "parent_keyset_revision": 1,
  "created_at": "2026-08-17T00:00:00.000000Z",
  "keys": [
    {
      "algorithm": "Ed25519",
      "key_id": "ed25519-sha256-BASE64URL",
      "public_key": "BASE64URL_WITHOUT_PADDING",
      "state": "current"
    }
  ]
}
```

Keys sort by `key_id`. Keyset bytes use RFC 8785 and signatures cover
`ASCII("exclusion-policy-keyset/v1") || 0x00 || JCS_UTF8(payload)`.
Revision 1 is self-signed and trusted only through authenticated first
onboarding; later revisions follow the chain rules below.

A plugin accepts a later keyset only when its parent equals the highest trusted
revision, its canonical bytes/hash are valid, and at least one signature comes
from an already trusted non-retired key. Activation also requires a valid
signature from the newly current key. Unknown gaps stop rotation and network
sync; they never reset trust silently.

Loss of the old private key before publishing a valid bridge requires explicit
device trust reset/re-onboarding. Loss of the current private key after a
bridge prevents new publication but does not invalidate already persisted
snapshots. Recovery never invents a replacement identity under the old key ID.

### 13.4 Plugin snapshot acceptance

The plugin validates in this order:

1. Parse bounded JSON with no duplicate property names.
2. Validate closed envelope/payload schema and workspace binding.
3. Recreate RFC 8785 canonical payload bytes.
4. Verify exact SHA-256.
5. Resolve `key_id` from trusted key history.
6. Verify the Ed25519 signature and evaluator contract hash.
7. Apply monotonic revision rules.

A revision greater than the highest accepted revision is accepted and persisted
atomically. The same revision is accepted only when revision ID and payload hash
are identical. A lower revision or same number with different identity/hash is
an integrity failure.

Snapshot has no time expiry. Offline classification may use the last valid
snapshot. Before any network sync, the plugin checks the server's current
revision; stale state refreshes keyset/snapshot and replays the same pending
event identity. The backend remains authoritative even when the plugin reports
a current revision.

## 14. Backend enforcement

### 14.1 Internal decision evidence

`PolicyEnforcementService` returns an internal-only immutable value:

```text
PolicyDecision
  workspace_id
  policy_revision_id
  revision_number
  subject_fingerprint
  raw_decision
  enforced_decision
  matched_rule_ids
  missing_fields
  evaluated_at
```

This type is not part of OpenAPI, MCP, Temporal history or plugin contracts.
Infrastructure receipts and public clients cannot deserialize or manufacture
it. A guarded canonical operation checks workspace, active revision and subject
fingerprint again at its transaction/read boundary.

Immutable revision contents may be cached in-process by revision ID. The
active pointer is always resolved from canonical PostgreSQL state at the
correct operation checkpoint. Redis loss, stale process cache or client claims
cannot change the decision.

### 14.2 Mandatory boundaries

| Boundary | Required check |
|---|---|
| Sync preflight | Before accepting a body, verifying object reuse or issuing an upload plan |
| Single-part upload | Before body read and again before canonical publication |
| Multipart | At session create, completion/promotion and publication; already issued short-lived staging part URLs remain noncanonical |
| Source create/update | Inside guarded publication before current pointer commit |
| Canonical read/download | Before any R2 request and again when resolving current source state |
| Manifest/reconcile | For every proposed action at the bound policy revision |
| Conflict capture/resolution | Before candidate admission and again before resolution publication |
| Ingestion | Before canonical byte read, every provider request and projection write |
| Rebuild/repair | At enumeration and immediately before target write |
| Retrieval/context | Before candidate return and before canonical hydration |
| MCP/action | Before read, proposal construction and approved commit |

Missing active policy, missing required evidence, policy-state corruption,
evaluation exception or PostgreSQL unavailability maps to deny. The service
never falls back to a plugin decision or projection payload.

A policy change after preflight invalidates the old decision. Publication
rechecks under the active policy-state lock; the event is replanned under the
new revision or denied. Bytes already written to a noncanonical staging key may
become an exact orphan for later bounded cleanup, but cannot become a source
version or current pointer.

Static import/composition tests prohibit API, MCP and worker entrypoints from
calling raw publication/read infrastructure outside the guarded composition.
Phase 1 internal smoke fixtures explicitly publish a signed empty policy before
performing canonical content operations.

## 15. Publication reconciliation

Publication commits one durable reconciliation intent. A leased dispatcher
starts exactly one workflow:

```text
exclusion-policy-reconciliation/{workspace_id}/{policy_revision_id}
```

Workflow input is a closed contract containing only workspace ID, policy
revision ID and source checkpoint. Activities page at most 500 sources and:

1. Confirm the revision is still active. A superseded revision stops without
   emitting later projection effects.
2. Build the current canonical subject at or after the publication checkpoint.
3. Evaluate the active immutable revision.
4. Insert/verify one immutable `policy_evaluations` row for the subject's exact
   source-event sequence.
5. Compare prior and proposed enforced decisions.
6. Emit deterministic projection intents when the effective decision changes.
7. Commit batch progress, counters and safe status.

Transitions are:

| Previous | Proposed | Projection intent |
|---|---|---|
| allowed | denied or indeterminate | Qdrant delete + Neo4j delete |
| denied or indeterminate | allowed | Qdrant upsert + Neo4j upsert when source has a valid current version and lifecycle permits it |
| same enforced decision | any raw reason change | none |

Policy-origin intent identity derives deterministically from policy revision,
source and projection kind. Exact replay verifies existing operation and source
version instead of inserting a second intent. A projection consumer rechecks
the then-active policy before every write, so an old pending upsert cannot
reintroduce denied content.

Only a source with a non-null current version can receive a policy-origin
projection intent. Both Qdrant and Neo4j intents for one source derive workflow
ID:

```text
policy-projection-transition/{workspace_id}/{policy_revision_id}/{source_id}
```

The closed workflow input contains contract tag plus workspace, policy revision,
source and current source-version UUIDs. Dispatcher start uses `USE_EXISTING`;
lost acknowledgement converges to the same execution. The transition workflow
is registered by the owning projection phase. Until then, durable intents stay
pending rather than being misrouted to `SourceIngestionWorkflow`.

The workflow continues as new after 20 batches or 10,000 sources. Cancellation
does not reverse the active revision. Dependency failure leaves reconciliation
pending with bounded retry/backoff and alertable lag. Policy activation is
never rolled back automatically because backend enforcement is already safe;
the administrator publishes a new revision to change policy.

## 16. HTTP API

All routes use canonical success/error envelopes, semantic operation IDs,
strict Pydantic models, exact response sets and no trailing-slash redirect.
Workspace and actor always derive from credential/session.

### 16.1 Admin routes

```text
GET  /api/admin/exclusion-policy
PUT  /api/admin/exclusion-policy/draft
POST /api/admin/exclusion-policy/previews
GET  /api/admin/exclusion-policy/previews/{policy_preview_id}
POST /api/admin/exclusion-policy/publications
```

The read route returns current revision metadata, draft/rules, exact draft
version and current reconciliation summary. Draft replacement requires CSRF
and `expected_draft_version`. Preview creation returns 202. Preview reads return
202 while pending/running, 200 when ready, and the closed error envelope when
failed/expired.

Publication requires Admin scope, CSRF and recent re-auth. A successful new
publication returns 201; exact replay returns 200 with the original result.

### 16.2 Plugin routes

```text
GET /api/sync/exclusion-policy/keysets
GET /api/sync/exclusion-policy/snapshot
```

Both require an active device access token with sync policy-read scope. Keysets
accept an optional nonnegative `after_keyset_revision` and return the next
bounded chain page. Snapshot supports conditional GET by current payload digest;
its ETag is the quoted payload SHA-256 and contains no source information.

Admin policy, preview, keyset and snapshot responses set
`Cache-Control: no-store`; policy routes never include R2 data, secret-file
paths or private keys.

The OpenAPI change, deterministic snapshot, generated TypeScript client and
contract tests land in one deliverable.

## 17. Web Admin behavior

Child 3 adds one real route, `/admin/policy`, to the existing authenticated
shell. It contains:

- initialization state and explicit empty-policy publication guidance;
- current revision, signer fingerprint and reconciliation status;
- draft rule editor with closed controls per rule kind;
- validation without echoing rejected values into error telemetry;
- current-versus-draft structural diff;
- asynchronous preview progress and paginated impact counts/results;
- a prominent `indeterminate` warning with missing field names;
- recent re-auth prompt; and
- exact typed publication confirmation.

Preview results may join and display title/locator to the authenticated owner.
They are escaped text, excluded from browser analytics/Sentry/breadcrumbs and
never persisted in localStorage/sessionStorage. The page uses generated API
contracts and TanStack Query; it does not connect to PostgreSQL or Temporal.

Two-tab draft conflict offers reload and manual reapply. It never last-write-
wins. Publish success replaces current metadata with the exact committed result
and continues showing reconciliation progress; it does not imply projections
are already clean.

## 18. Obsidian plugin behavior

This child adds policy acquisition/verification and a small policy status area,
not the later file journal or upload implementation.

The plugin stores these non-secret values through a narrow settings adapter:

```text
workspace binding
trusted keyset chain
last valid signed snapshot
highest accepted policy revision number / ID / digest
policy integrity state
```

Later Child 4 may move operational policy metadata to SQLite without changing
the snapshot contract. Loss of this cache requires authenticated keyset/
snapshot acquisition; it never changes canonical state.

Closed states are:

```text
policy_not_initialized
policy_ready
policy_refresh_required
policy_offline_cached
policy_integrity_failed
```

When connected, the plugin fetches keyset/snapshot after token refresh. It
verifies into temporary memory and atomically replaces cache only after all
checks pass. Invalid new material leaves the previous valid cache untouched,
sets `policy_integrity_failed` and blocks network sync.

The pure TypeScript evaluator classifies Vault candidates locally. `excluded`
avoids queue/upload; `indeterminate` is withheld with a repair explanation;
`allowed` may proceed only after the later sync service confirms current server
revision. Policy changes from deny to allow are discovered by later manifest
reconciliation, so skipped local files are not lost.

Offline mode preserves every Vault file and edit. It may display classification
from the last trusted snapshot but performs no network authorization claim.
Static tests continue prohibiting Node.js, Electron and `FileSystemAdapter`
imports at module load time.

## 19. Error contract

The closed registry gains:

| Error code | HTTP | Retryable | Safe details |
|---|---:|---:|---|
| `exclusion_policy_input_invalid` | 422 | no | closed `reason`, optional `rule_index` |
| `exclusion_policy_not_initialized` | 409 | no | none |
| `exclusion_policy_draft_conflict` | 409 | no | `current_draft_version` |
| `exclusion_policy_preview_pending` | 409 | yes | `retry_after_seconds` |
| `exclusion_policy_preview_failed` | 409 | no | closed `reason` |
| `exclusion_policy_preview_expired` | 410 | no | none |
| `exclusion_policy_preview_stale` | 409 | no | closed `reason` |
| `exclusion_policy_confirmation_invalid` | 409 | no | none |
| `exclusion_policy_denied` | 403 | no | `policy_revision_number` |
| `exclusion_policy_indeterminate` | 403 | no | closed `reason` |
| `exclusion_policy_snapshot_outdated` | 409 | yes | `current_policy_revision_number` |
| `exclusion_policy_signing_unavailable` | 503 | no | none |
| `exclusion_policy_commit_outcome_unknown` | 503 | yes | none |

The 202 preview polling responses are normal success envelopes, not
`exclusion_policy_preview_pending`; the error code is reserved for an operation
that requires a ready preview.

Internal signature/hash/keyset/state corruption maps to the safe public
`internal_error` unless one of the explicit operator-facing codes applies. No
error contains rule operand, path, title, draft body, rejected value, signature,
public key bytes, secret-file path, SQL/driver text or crypto exception.

Plugin-local verification failures use closed non-HTTP reason tokens and never
serialize library exceptions.

## 20. Security and privacy

- Backend authorization is independent from signature acceptance. A valid
  snapshot does not grant upload/read capability.
- Rule and subject bounds are checked before allocation/compilation.
- Glob evaluation has no untrusted backtracking regex.
- All IDs are workspace-bound; cross-workspace source IDs never match or leak.
- Preview joins recheck workspace ownership at response time.
- Publication requires CSRF, five-minute recent re-authentication and exact
  confirmation even when impact count is zero.
- Key generation/import refuses overwrite and never prints private material.
- Private key objects have process-local lifetime and no repr/serialization;
  the design does not claim impossible guaranteed memory zeroization in Python.
- Keyset/snapshot endpoints are authenticated and `no-store`.
- Policy bytes are small canonical PostgreSQL state, not R2 content objects.
- Signed payloads contain rule operands because the plugin must evaluate them;
  transport and local cache therefore receive the same protection as other
  sensitive plugin settings and never enter telemetry.
- A path-derived subject fingerprint is treated as sensitive despite being a
  SHA-256 value.

## 21. Audit and observability

Append-only audit actions include:

```text
exclusion_policy.draft_replaced
exclusion_policy.preview_requested
exclusion_policy.published
exclusion_policy.publish_rejected
exclusion_policy.key_initialized
exclusion_policy.key_staged
exclusion_policy.key_activated
exclusion_policy.key_retired
exclusion_policy.reconciliation_completed
exclusion_policy.reconciliation_failed
```

Audit stores actor, workspace, opaque target IDs, request ID, result, counts,
safe reason and diff/impact digest. It never stores a rule list, operand,
snapshot, source display value or private/public key bytes. Normal snapshot
fetches and per-source evaluations use metrics rather than one audit row each.

Structured diagnostic events use the same closed vocabulary and correlation
context. Allowed fields are revision number, rule count, decision, boundary,
impact counts, batch count, duration and safe error code. Source ID may appear
only where existing diagnostic policy permits an opaque ID; it is never a
metric label.

Metrics:

```text
exclusion_policy_evaluation_total{boundary,decision}
exclusion_policy_evaluation_duration_seconds{boundary,decision}
exclusion_policy_preview_total{outcome}
exclusion_policy_preview_duration_seconds{outcome}
exclusion_policy_publication_total{outcome}
exclusion_policy_reconciliation_sources_total{transition}
exclusion_policy_reconciliation_lag_seconds
exclusion_policy_snapshot_verification_total{client_class,outcome}
```

Labels are closed and low-cardinality. Workspace, source, rule, preview,
revision, path, media type and key ID are prohibited metric labels.

## 22. Failure and recovery

| Failure | Required behavior |
|---|---|
| Policy never published | Admin remains available; all content operations deny |
| PostgreSQL unavailable | No policy claim; content boundary fails closed |
| Private key missing/mismatched | API refuses startup; offline key CLI remains available |
| Temporal unavailable during preview | Preview remains pending/retryable, no publication |
| Temporal unavailable after publish | Active policy remains enforced; reconciliation intent remains durable |
| Reconciliation batch crash | Exact batch/evaluation/intent replay, no duplicate effect |
| Revision superseded mid-reconcile | Old workflow stops before later projection effects |
| Snapshot/keyset tamper | Plugin preserves prior trusted cache and blocks network sync |
| Long-offline device | Fetches and verifies paginated keyset chain, then current snapshot |
| Signing key lost without bridge | Explicit trust-reset/re-onboarding recovery; no silent key replacement |
| Ambiguous publication commit | New-connection evidence lookup or retryable unknown outcome |

Database backup includes drafts, revisions, public key history, snapshot bytes,
evaluations and intents. It does not include secret files. Operations must back
up the current policy private key separately with the same protected secret
backup procedure as authentication keys.

Restore readiness checks that the database's latest current key ID matches an
available private key before exposing the API. A restore with valid canonical
policy but no private key may be inspected by offline tools but cannot publish
or serve content operations.

Preview/results are bounded operational data and may be pruned after 24 hours
when not referenced by a published revision. Published policy/keyset/snapshot,
audit and evaluations follow canonical backup/retention rules. Reconciliation
and projection intents remain until safely acknowledged/compacted by their
own contracts.

## 23. Testing strategy

### 23.1 Domain and cross-language

- Every rule kind at boundary values.
- Default allow, any-match deny and definite-match-over-missing precedence.
- Missing field combinations and invalid public subject distinction.
- NFC, Unicode, slash, case and UTF-8 byte limits.
- Folder segment boundaries and complete bounded glob grammar.
- Duplicate semantic rule rejection and deterministic rule ordering.
- Shared Python/TypeScript evaluator golden fixtures and contract hash.
- Property/fuzz cases proving bounded completion for hostile patterns/paths.

### 23.2 Cryptographic contract

- RFC 8785 canonicalization fixtures, including property ordering and rejected
  non-I-JSON inputs.
- RFC 8032/library Ed25519 test vectors.
- Golden payload bytes, SHA-256, key ID and signature in Python/TypeScript.
- Tamper every signed field, hash, signature, workspace and evaluator hash.
- Unknown key, keyset gap, invalid cross-signature and activation without proof
  of possession.
- Lower-revision rollback and same-revision/different-payload equivocation.
- Atomic cache replacement and preservation of previous valid snapshot.

### 23.3 PostgreSQL and races

- Empty/fixture upgrade, reflection, backfill and downgrade refusal/gate.
- Append-only trigger rejection for revision/rule/evaluation/keyset tables.
- One active policy/current key and exact operand checks.
- Draft two-writer compare-and-swap.
- Draft edit, source event or active-policy change after preview.
- Two publications from one preview: one commit and one stale/exact replay.
- Same/different idempotency fingerprint races.
- Injected ambiguous commit acknowledgement and evidence lookup.
- Cancellation at every transaction await point leaves no partial revision.

### 23.4 Workflow and enforcement

- Preview start lost acknowledgement, lease expiry and exact workflow reuse.
- Preview page replay and stable impact digest.
- Reconciliation retry, crash after batch commit, continue-as-new and
  superseded revision stop.
- Exactly one policy-origin projection intent per revision/source/kind.
- Allow-to-deny blocks preflight/read before cleanup completes.
- Deny-to-allow emits upsert only for lifecycle-valid current source.
- Pending source ingestion re-evaluates and cannot write after deny.
- Every current/future boundary in section 14 has a contract test proving guard
  invocation; static tests reject raw adapter bypass.

### 23.5 API, Web and plugin

- Exact route/status/error/header/OpenAPI contract and generated-client compile.
- CSRF, scope, recent re-auth, typed confirmation and no trailing redirects.
- Web initialization, rule editing, async preview, indeterminate warning,
  two-tab conflict, publication replay and reconciliation progress.
- Plugin first trust, offline cache, conditional fetch, refresh, rotation,
  rollback, integrity failure and Vault preservation.
- Desktop and Mobile evaluator/signature verification on reference devices.
- Mobile static imports and production bundle inspection.
- Leak sentinels for paths, titles, operands, snapshot contents, private keys,
  signatures and crypto exception text across HTTP diagnostics, audit, traces,
  Sentry, JUnit and production builds.

Mutation testing remains deferred. It is not a completion gate for this child.

## 24. Performance and capacity gates

The required reference benchmarks are:

```text
one subject against 256 rules             p95 <= 5 ms
10,000 subjects against 256 mixed rules   preview ready <= 30 seconds
one maximum-size snapshot verification    p95 <= 50 ms
10,000-source reconciliation              <= 5 minutes without dependency outage
```

Preview and reconciliation page at 500 subjects. Preview result API pages at
200. Workflow continues as new after 10,000 subjects. Snapshot is at most 256
KiB. Benchmarks record reference hardware, runtime, rule distribution, database
state, wall time and peak memory; an unrecorded local observation cannot satisfy
the gate.

Failure to meet a budget triggers profiling and an explicit capacity decision;
it does not authorize weakening fail-closed behavior, skipping rules, trusting
the plugin or caching the active pointer in Redis.

## 25. Acceptance criteria

Child 3 is complete only when one final commit proves:

1. Existing/future workspaces have an empty draft and no implicit published
   policy.
2. Before revision 1, canonical content operations fail closed while Admin can
   preview/publish.
3. Explicit empty revision 1 is immutable, signed and allows valid subjects.
4. All seven rule kinds produce identical backend/plugin outcomes.
5. Any definite match excludes; missing required evidence is visible as
   `indeterminate` and enforced deny.
6. Preview is bound to exact draft, active revision and source checkpoint; a
   stale preview cannot publish.
7. Publication exact replay returns the original outcome and never duplicates
   revision, audit, key signature or reconciliation intent.
8. Lost commit acknowledgement converges through evidence lookup.
9. Snapshot/keyset tamper, unknown key, gap, equivocation and rollback are
   rejected without destroying the last trusted plugin state.
10. Rotation from old to new signing key works through a verifiable immutable
    chain on Desktop and Mobile.
11. Backend rejects stale client policy and enforces the active revision at
    every available content boundary.
12. Allow-to-deny takes effect before projection cleanup; reconciliation later
    leaves no active denied Qdrant/Neo4j residue when those projections exist.
13. Deny-to-allow schedules exactly one eligible reprojection per target.
14. Workflow crash/retry/cancellation creates no duplicate evaluation or intent.
15. Migration, OpenAPI, generated client, Web/plugin tests, leak scans and
    reference performance gates pass.
16. Operations documentation covers key initialization, rotation, backup,
    restore, lost-key recovery, stuck preview and reconciliation repair.

## 26. Implementation boundary for the next plan

The implementation plan should preserve this dependency order:

1. Pure rule/subject contracts, normalization, evaluator and shared fixtures.
2. Error/diagnostic registries and Ed25519/JCS/keyset domain contracts.
3. Alembic schema, append-only triggers and PostgreSQL repositories.
4. Offline policy-key CLI, startup signer checks and keyset rotation.
5. Draft, async preview and publication services with race tests.
6. Snapshot/keyset HTTP routes and plugin verification/cache.
7. Admin policy page and generated-client integration.
8. Enforcement guard integration into currently available canonical paths.
9. Reconciliation workflow, generic policy-origin projection intents and
   dispatcher evolution.
10. Cross-boundary, leak, performance, recovery and operations gates.

This order is guidance for the future written implementation plan. This design
does not authorize implementation before the user reviews the written spec and
approves transition to `writing-plans`.

## 27. Canonical documentation effects

This child clarifies, without changing the parent architecture:

- `sources.sync_state` is lifecycle/projection availability, not policy allow.
  Effective policy lives in `policy_evaluations` and active revision state.
- Phase 2 exclusion is binary; future AI access classes extend policy in their
  owning Phase 3/4 design.
- Preview may be incomplete for canonical sources lacking locator evidence;
  it reports `indeterminate` and publication remains an explicit confirmed
  action.
- Plugin signed snapshots optimize bandwidth only. Backend re-evaluation is the
  authorization/correctness boundary.

## 28. References

- `docs/00-PRODUCT_VISION_AND_PRD.md`
- `docs/01-CANONICAL_ARCHITECTURE.md`
- `docs/02-TECH_STACK.md`
- `docs/03-DATA_OWNERSHIP_AND_STORAGE.md`
- `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- `docs/05-INGESTION_CHUNKING_AND_INDEXING.md`
- `docs/07-POSTGRESQL_DATA_MODEL.md`
- `docs/11-TEMPORAL_WORKFLOWS.md`
- `docs/13-WEB_APP_AND_ADMIN_DASHBOARD.md`
- `docs/14-SECURITY_PRIVACY_AND_POLICY.md`
- `docs/15-OBSERVABILITY_AND_ALERTING.md`
- `docs/16-TESTING_AND_EVALUATION.md`
- `docs/19-ARCHITECTURE_DECISIONS.md`
- `docs/20-IMPLEMENTATION_PLAN.md`
- `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`
- `docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md`
- [RFC 8032 — Edwards-Curve Digital Signature Algorithm](https://www.rfc-editor.org/rfc/rfc8032)
- [RFC 8785 — JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)
- [Cryptography Ed25519](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/)
- [Cryptography key serialization](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/)
