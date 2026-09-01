# Exclusion Policy Publication Operations Guide

Operator contract for the server-owned exclusion policy (`src/personal_os/exclusion_policy`,
the adapters in `packages/postgresql-source-store`, the Admin/plugin routes in
`apps/api/src/api_runtime`, the Web Admin policy page in `apps/web` and the policy
acquisition/verification in `apps/obsidian-plugin`). Design:
`docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md`
(status: implemented 2026-08-17).

The policy is deny-only with default allow. PostgreSQL holds the canonical drafts,
immutable revisions, signing-key history, evaluations and reconciliation intents;
nothing about policy lives in R2. Every backend content boundary re-evaluates the
active revision fail-closed; the plugin's signed snapshot only optimizes bandwidth
and is never an authorization capability.

Exclusion changes are published as a **new revision**. Published revisions,
rules, signatures, keysets, evaluations and audit rows are immutable history:
they are **never edited** and they are **never deleted**. There is no in-place
rollback, no deletion and no re-signing of an old revision — the only way to
change what the system enforces is to edit the draft and publish the next
revision (a rollback is simply a new revision that restores the previous rule
set).

## Initial trust (onboarding)

One workspace must hold exactly one initialized signing keyset before any
policy publication or content operation can succeed. On a trusted host with
the secret root mounted:

```bash
# Generate (or import) the first Ed25519 signer and publish the self-signed
# keyset revision 1. Never overwrites an existing key file; prints only the
# public key ID. Private key material is never an argument, env value or log.
uv run --package api-runtime personal-api policy-key initialize --workspace-id <uuid> --key-file-name policy_signing_a.pem
```

Then point the API signer fragment at that key and restart `serve`. Startup
derives the public key and its `ed25519-sha256-…` ID and refuses to bind a
socket unless it equals the current key of the latest canonical keyset.

| Environment | Meaning |
| --- | --- |
| `KNOWLEDGE_POLICY_SIGNING_KEY_ID` | Derived public key ID of the signer the API uses. |
| `KNOWLEDGE_POLICY_SIGNING_KEY_FILE` | Exact file name of the unencrypted PKCS#8 Ed25519 private key under `KNOWLEDGE_SECRET_ROOT`. |

The Obsidian plugin trusts its first keyset only immediately after
authenticated device onboarding over the configured HTTPS origin. A random
unauthenticated keyset endpoint, a snapshot-embedded public key or a URL
parameter can never create trust. Re-onboarding after a completed trust reset
atomically replaces the previous trust anchor (the old anchor is replaced, not
merged).

The Phase 1 canonical-core acceptance path seeds the same starting state
automatically in disposable environments — it publishes the signed empty policy
before any content operation:

```bash
uv run python tools/canonical_core_operations.py phase-one-acceptance
```

## Explicit empty-policy publication (first revision)

Until revision 1 exists the system is fail-closed: the Admin policy page is
reachable but every content operation answers
`exclusion_policy_not_initialized` and denies. Do not work around this; publish
the initial empty policy explicitly so first-publication impact is visible:

1. Web Admin → `/admin/policy` → confirm the initialization-state banner shows
   no active revision.
2. Leave the draft rule list empty (default allow for everything) and save the
   draft.
3. Create a preview; existing valid sources appear as `newly_allowed`.
4. Complete the typed publication confirmation (below). Revision 1 becomes
   active and content operations open.

An initial policy that is deliberately non-empty follows the same flow with
draft rules filled in; the empty-policy path is just the explicit minimum.

## Preview and publish flow

```text
PUT  /api/admin/exclusion-policy/draft          replace draft (CSRF + expected_draft_version)
POST /api/admin/exclusion-policy/previews       202, starts the async preview
GET  /api/admin/exclusion-policy/previews/{id}  202 while pending/running, 200 ready
POST /api/admin/exclusion-policy/publications   201 new publication, 200 exact replay
GET  /api/admin/exclusion-policy/diagnostics    200 evaluation/publication counters + recent-failure ring
GET  /api/admin/metrics                        200 Prometheus text exposition of the same counters
```

The diagnostics read is the observability surface of this domain: evaluation
counters by `(boundary, decision)` — the closed `failed` decision included —,
publication outcome counters, and the bounded ring of recent policy system
failures (closed registry code, closed boundary, epoch-ms timestamp). It is
read-only, in-memory per process (resets on restart) and sits behind the same
strict Web Admin session gate as the routes above; the operator procedure and
the sanitized payload shape live in
[`sync-error-tracing.md`](sync-error-tracing.md).

The preview runs as one deterministic Temporal workflow per preview:

```text
exclusion-policy-preview/{workspace_id}/{policy_preview_id}
```

Publication requires Admin scope, CSRF, password re-authentication within the
last five minutes, the exact typed confirmation phrase
`PUBLISH EXCLUSION POLICY`, a ready unexpired preview, the expected draft
version and active
revision, and a printable idempotency key (1–200 characters) sent in the
`X-Idempotency-Key` request header — this is a new header introduced by this
child; there is no prior HTTP idempotency convention in the repository.

Publication is one READ COMMITTED transaction: replay resolution, ownership /
confirmation / expiry / version / parent / checkpoint rechecks, canonical
payload build, local Ed25519 signing and self-verification, immutable insert,
active-pointer swap, one reconciliation intent, one audit row, preview
consumption and draft rebase — one commit or none. No R2, Temporal, HTTP or
provider call happens inside the transaction. If a commit acknowledgement is
ambiguous, the service performs a fresh-connection evidence lookup by workspace,
idempotency key and fingerprint: proven committed evidence returns the exact
original result; proven absence permits one normal retry; PostgreSQL
unavailability stays the retryable `exclusion_policy_commit_outcome_unknown`
and never assumes rollback.

## Key rotation

Rotation is staged so already-trusted devices can verify every link. Replace
`<uuid>` and file names with real values; private keys live only as exact
files under the secret root.

```bash
# 1. Stage the new key beside the old current key (old-current signature plus
#    proof-of-possession from the new key) while the old key stays usable.
uv run --package api-runtime personal-api policy-key stage --workspace-id <uuid> --key-file-name policy_signing_b.pem

# 2. Make the staged key current in a cross-signed keyset revision. The old
#    current key becomes `staged` (overlap) until the retire step below.
uv run --package api-runtime personal-api policy-key activate --workspace-id <uuid> --staged-key-file-name policy_signing_b.pem

# 3. Only after switching KNOWLEDGE_POLICY_SIGNING_KEY_ID/_FILE to the new key
#    and restarting serve, retire the old key after the operating overlap.
uv run --package api-runtime personal-api policy-key retire --workspace-id <uuid> --key-id <ed25519-sha256-…>
```

At most one key is current and at most four keys are non-retired in the latest
keyset. Historical keysets and signatures stay append-only. Loss of the old
private key before a valid bridge is published requires an explicit device
trust reset and re-onboarding; loss of the current private key after a bridge
prevents new publication but never invalidates already persisted snapshots.
Recovery never invents a replacement identity under an old key ID.

Long-offline devices page the chain with
`GET /api/sync/exclusion-policy/keysets?after_keyset_revision=<n>` (16
envelopes per keyset page) and then refresh
`GET /api/sync/exclusion-policy/snapshot` (conditional GET by payload digest;
ETag is the quoted payload SHA-256). Unknown revision gaps stop rotation and
network sync; trust is never reset silently.

## Concurrency: the frozen lock order

```text
ordinary source/enforcement path: publication idempotency advisory lock → workspace_policy_state row → source row
claimed upload path: operation identity advisory lock → publication idempotency advisory lock → workspace_policy_state row → source row
policy publication path: policy idempotency advisory lock → workspace_policy_state row
```

Policy publication locks the workspace policy-state row and never touches
source rows. The claimed-upload path first fences reauthorization and terminal
publication with the operation identity lock, then enters the ordinary
publication order. The source path re-evaluates the locked active policy
between the idempotency recheck and the source advisory lock. Reconciliation
never holds the policy-state lock while acquiring source rows. AST-order and
race contract tests pin these orders; an inverse-order refactor fails the gate
instead of deadlocking production.

## Reconciliation

Publication commits one durable intent; a leased dispatcher starts exactly one
workflow per revision:

```text
exclusion-policy-reconciliation/{workspace_id}/{policy_revision_id}
```

Batches of 500 sources re-evaluate the active immutable revision and record
immutable `policy_evaluations` rows; decision changes emit deterministic
Qdrant/Neo4j projection intents (workflow
`policy-projection-transition/{workspace_id}/{policy_revision_id}/{source_id}`,
started with `USE_EXISTING`, registered by the owning projection phase — until
then the intents stay durable and pending and never reach the source workflow).
The workflow continues as new after 20 batches or 10,000 sources. Cancellation
does not reverse the active revision; policy activation is never rolled back
automatically because backend enforcement is already safe.

## Recovery limits

| Limit | Value |
| --- | --- |
| Preview ready expiry / execution deadline | 15 minutes (a failed or expired preview cannot publish; retry with a new preview) |
| Preview/reconciliation scan batch | 500 rows per batch (keyset pages) |
| Preview result page size | 200 items per page, stable `(impact_class, source_id)` cursor |
| Keyset chain page | 16 envelopes per keyset page |
| Rules per revision | 256 rules maximum |
| Signed snapshot envelope | 256 KiB maximum (Ed25519 signing is bounded local CPU work) |

## Degraded states: detection and recovery

| Degraded state | Detection | Recovery |
| --- | --- | --- |
| Invalid signer | `serve` refuses to bind its socket at startup (`configuration_secret_invalid` / keyset mismatch; publication answers `exclusion_policy_signing_unavailable`). | Fix the key file / env pair so the derived key ID equals the latest keyset's current key, then restart. The offline `policy-key` CLI stays available without the API. |
| PostgreSQL unavailable | No policy claim is made; every content boundary fails closed (`deny`); publication may answer retryable `exclusion_policy_commit_outcome_unknown`. | Restore PostgreSQL; retry the exact original idempotency key — never a new one — and let replay resolution return the committed result or permit one retry. |
| Temporal unavailable during preview | Preview stays pending/retryable (202 polling); nothing publishes. | Restore Temporal; the dispatcher claim/backoff converges. A preview stuck past the 15-minute deadline fails closed; create a new preview. |
| Temporal unavailable after publish | Active policy remains enforced; the reconciliation intent stays durable; `exclusion_policy_reconciliation_lag_seconds` grows. | Restore Temporal; reconciliation replays exact batch/evaluation/intent identity with no duplicate effect. |
| Stale preview | Preview reads return `exclusion_policy_preview_stale` (or 410 expired) when the workspace source checkpoint moved, the draft changed or the active revision advanced after capture. | Create a fresh preview from the current draft; a stale preview must never publish. |
| Plugin integrity failure | Plugin state `policy_integrity_failed`; network sync blocks. Snapshot tamper, keyset gap, lower revision or same-revision/different-payload mismatch all land here. | The previous valid cached snapshot is preserved. Refresh keyset/snapshot while authenticated; if the trust anchor itself is lost, complete a device re-onboarding which replaces the anchor. Never hand-edit the plugin cache. |
| Reconciliation lag | `exclusion_policy_reconciliation_lag_seconds` metric and the Admin reconciliation summary (state pending/leased/dispatched, dispatched is the resting state after the workflow acknowledges). | Intents are durable; verify the dispatcher and Temporal, then let the bounded retry/backoff converge. A superseded revision's workflow stops without later effects. |
| Restore without the private key | Restore readiness check fails: the database's latest current key ID has no matching secret file; `serve` refuses startup; publication and content operations stay closed. | Inspect with offline tools if needed, then restore the matching private key file from the separate secret backup (or, if it is truly lost, stage a trust reset and re-onboard devices). Canonical policy state is intact — only signing/serving is blocked. |

## Backup and restore

Database backup includes drafts, revisions, rules, public key history, snapshot
bytes, evaluations and intents. The database backup does not include secret files,
and the policy signing key is one of those secret files:
**the private key is never part of the database backup — back up the current
policy private key separately** under the same protected secret-backup
procedure as the authentication keys.
A restore with valid canonical policy but no private key may be inspected
offline but cannot publish or serve content operations (see the degraded-states
row above).

## Rollback is a new revision

Immutable history is never edited or deleted — not by operators, not by
recovery, not automatically. To undo a published change, restore the previous
rule set into the draft and publish it as the next revision. Superseded
revisions remain queryable for provenance; enforcement always reads the active
pointer.

## Reference-device verification

Completion of this child additionally requires recorded Desktop and Mobile
Obsidian reference-device verification (initial trust, snapshot verification,
rotation, offline cache, Vault preservation) in
`docs/operations/exclusion-policy-device-verification.md`. Until both device
sections with dated operator lines exist, the gate fails by design under
explicit selection:

```bash
uv run poe exclusion-policy-device-verification
```

Do not fabricate or placeholder this evidence; absence blocks final completion.

## Acceptance gates

```bash
# Full feature gate: unit, contract, API and disposable-stack integration
# suites (1383 passed at the final verification run; the two Windows-only
# platform skips in the settings suite are pre-existing).
uv run poe exclusion-policy-test

# Browser journey (login → fail-closed status → empty-policy publish →
# revision 1 → deny rule → impact preview → revision 2).
pnpm run test:e2e:exclusion-policy

# Performance budgets: evaluator p95 ≤ 5 ms, snapshot verify p95 ≤ 50 ms,
# preview (10,000 subjects) ≤ 30 s, reconciliation (10,000 sources) ≤ 300 s.
uv run pytest tests/performance/test_exclusion_policy_performance.py -m local_stack -q

# Repository-wide verify (format, lint, types, boundaries, tests, builds).
uv run poe verify
```

The CI workflow `.github/workflows/exclusion-policy-acceptance.yml` runs the
same gates on a disposable stack. Alembic single head for this child:
`20260817_01` (`add_exclusion_policy_publication`).

## Safe metrics and audit

Audit actions are the closed set `exclusion_policy.draft_replaced`,
`preview_requested`, `published`, `publish_rejected`, `key_initialized`,
`key_staged`, `key_activated`, `key_retired`, `reconciliation_completed`,
`reconciliation_failed` — actor, workspace, opaque IDs, request ID, result,
counts, safe reason and impact digest only. Metrics are the closed
low-cardinality implemented set:
`exclusion_policy_evaluation_total{boundary,decision}` and
`exclusion_policy_evaluation_duration_seconds{boundary,decision}`,
`exclusion_policy_preview_total{outcome}` and
`exclusion_policy_preview_duration_seconds{outcome}`,
`exclusion_policy_publication_total{outcome}`,
`exclusion_policy_reconciliation_sources_total{transition}` and
`exclusion_policy_reconciliation_lag_seconds`. Planned by spec §21 but not
implemented: `exclusion_policy_snapshot_verification_total{client_class,outcome}`
(no recorder or exporter emits it today; the plugin's local verification
outcomes surface through its own diagnostics, not this backend metric set).
Workspace, source, rule, preview, revision, path, media type and key ID are
prohibited labels. Nothing logs rule operands, paths, titles, signatures, key
bytes or secret-file paths.

The `decision` label of the evaluation metrics is the closed set
`allowed | excluded | indeterminate | failed`: the first three are the raw
evaluation decision (indeterminacy stays observable; enforcement maps
`indeterminate` to deny), and `failed` records that the policy SYSTEM itself
failed before it could decide — no active signed policy
(`exclusion_policy_not_initialized`) or signing unavailable/corrupt
(`exclusion_policy_signing_unavailable`) — so a fail-closed boundary is
counted instead of invisible (policy-observability remediation 2026-08-24 C1).
The registry code of a `failed` evaluation never becomes a metric label; it
rides the record and surfaces through the Admin diagnostics route
(`GET /api/admin/exclusion-policy/diagnostics`, operator procedure in
[`sync-error-tracing.md`](sync-error-tracing.md)).

The production metrics sink is `GET /api/admin/metrics` (operation id
`getMetricsExposition`, sink plan 2026-08-31): it renders the shared
recorder's evaluation and publication counter families — the same snapshot
the diagnostics route reads — in Prometheus text format (`text/plain;
version=0.0.4; charset=utf-8`, `Cache-Control: no-store`) behind the same
strict Web Admin session gate as the routes above; a plugin device
credential is never accepted. Counters and closed label tokens only: the
recent-failure ring, durations and every id, path or free-text shape never
render, and the sink only reads — it never records. If the sink cannot read
or render the counter snapshot, the scrape closes with the retryable
`exclusion_policy_metrics_unavailable` (503) while evaluation keeps
recording: the sink is read-side only and can never block an evaluation
path. A fresh or fallback sink scrapes as the two `# TYPE` header lines
with zero samples.
