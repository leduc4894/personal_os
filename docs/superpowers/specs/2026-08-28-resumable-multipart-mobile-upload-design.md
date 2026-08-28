# Resumable Multipart Mobile Upload Design

**Child:** 7 of 9 — Phase 2 Obsidian sync

**Status:** approved design; implementation has not started

**Depends on:** Child 1 API/runtime contracts; Child 3 exclusion policy;
Child 4 journal/small-file upload. Child 6's two former start gates are
recorded as complete in the current repository history. The remaining
device-sync recovery finding is explicitly owned by Child 8 and does not widen
this child.

## 1. Purpose

This child adds a resumable, direct-to-R2 staging path for a source create or
update whose frozen content is larger than the server-owned single-part limit.
It permits an Obsidian client, including Mobile, to suspend or lose transport
between parts without losing its durable journal event, while preserving the
existing guarantees:

- PostgreSQL is the canonical state and R2 canonical objects remain immutable;
- no client can write a canonical object key or deserialize an internal
  verified receipt;
- a completed multipart object is merely noncanonical staging until the server
  independently verifies and promotes it through the Phase 1 conditional CAS
  writer; and
- cleanup operates only on an exact session's provider upload ID and staging
  key, never by bucket listing, prefix deletion, or garbage collection.

The initial routing threshold is more than 16 MiB through the existing 100 MiB
product maximum. It is a server-owned value: a client obeys the returned plan,
not a locally duplicated threshold.

## 2. Scope and exclusions

### 2.1 Included

- PostgreSQL multipart-session and part-progress state, with an Alembic
  migration and reversible downgrade.
- Authenticated API contracts to create/resume a session, issue one part URL,
  observe safe status, request completion and request user cancellation.
- An R2 adapter capability limited to multipart staging create/upload-part
  presigning, list-parts verification, complete, abort and exact-key delete.
- Plugin journal persistence and queue-driver behavior for Desktop and Mobile
  upload resumption.
- Server-side full-object staging verification, Phase 1 CAS promotion,
  idempotent source-event publication and exact cleanup.
- Durable expiry cleanup orchestration, closed diagnostics and low-cardinality
  metrics.
- Offline, PostgreSQL/R2 integration, privacy and real-device acceptance
  evidence for this slice.

### 2.2 Excluded

- R2 Worker, hybrid-edge routing, public upload endpoint, public object URL,
  temporary credentials, provider fallback and multi-provider storage.
- Content larger than 100 MiB, client-side encryption, arbitrary ranged upload,
  canonical-object mutation, bucket list/prefix cleanup and general orphan GC.
- Candidate retention, stale-base candidate upload, conflict UI, merge and
  resolution (Child 8).
- Changes to cursor reconciliation semantics except dispatching an existing
  allowed outbound event through this transport.
- A background execution assumption on Mobile. Foreground resume is sufficient;
  suspension is a normal nonterminal interruption.

No production dependency is added by this design. The existing reviewed R2
adapter dependency is extended only after its multipart calls are covered by
the contracts below.

## 3. Invariants

1. A multipart session belongs to exactly one workspace, device and frozen
   outbound journal event. It never changes source, event, digest, byte size,
   media type, policy revision or base evidence after creation.
2. The server derives workspace/device scope from the access credential. A
   request body, session ID or URL cannot select another workspace or device.
3. The server creates the opaque staging key and provider upload ID. Neither is
   returned to the plugin or public API response.
4. A presigned URL authorizes exactly one `PUT` of one numbered part, its exact
   byte range and content length, for at most ten minutes. It targets only the
   session's noncanonical staging upload.
5. The plugin never writes presigned URLs, their query parameters, provider
   upload IDs, provider ETags, staging keys, full digests or raw bytes to
   SQLite, settings, trails, logs, errors, traces, JUnit or device evidence.
6. Part completion is proved by the provider. The server obtains provider part
   metadata itself and stores it in PostgreSQL; the client does not echo an
   ETag as trusted completion evidence.
7. Only one completion/promotion claimant may act for a session. Every replay
   observes the already persisted terminal result rather than issuing a second
   complete, canonical write or source event.
8. Completion always performs a full staging read and independently validates
   exact SHA-256, size and media type before publication. A size/hash mismatch
   cannot commit or create a canonical receipt.
9. Promotion streams the verified staging bytes into the existing Phase 1
   conditional CAS writer. A canonical object is never copied, renamed or
   promoted by R2 provider-side operation.
10. Every terminal path records or schedules cleanup of only its exact staging
    resource. Cleanup is idempotent, bounded and observable; a cleanup failure
    is never silently swallowed.
11. Policy is checked at session creation, URL issuance, completion and
    publication. A URL issued before a policy change may still write only
    temporary staging bytes; it cannot publish denied content.
12. Cancellation, suspend, timeout, offline and an ambiguous response retain
    the journal event and durable safe progress. They do not make Mobile count
    a transfer as failed or invent a new source/event identity.

## 4. Transfer shape

The server decides the plan during the existing preflight transaction. The
existing outcomes remain unchanged below or at 16 MiB. Above it, a permitted
create/update returns `multipart_upload` with an opaque `upload_session_id`,
its expiry, `part_size_bytes`, `part_count` and a status URL. It returns no
signed URL, R2 key, provider ID, ETag, receipt or storage identity.

The immutable initial geometry is:

| Property | Value |
| --- | --- |
| File size | `16 MiB < size_bytes <= 100 MiB` |
| Ordinary part | 8 MiB |
| Final part | remaining positive bytes, at most 8 MiB |
| Maximum part count | 13 |
| Desktop concurrency | at most 3 parts |
| Mobile concurrency | at most 2 parts |
| Part URL lifetime | 10 minutes |
| Multipart session lifetime | 24 hours from creation |

The API validates the requested part number against that geometry and derives
the range. A Mobile client must not assume that an HTTP runtime preserves a
file stream while suspended: it reopens the Vault file, rechecks the frozen
fingerprint and transmits only unfinished ranges when foreground work resumes.

### 4.1 Durable state

`multipart_uploads` is canonical PostgreSQL state with at least:

- opaque public session ID; workspace/device/event/source identity;
- expected SHA-256, `size_bytes`, media type, base version and policy revision;
- exact private staging key and provider upload ID;
- part geometry, creation/expiry timestamps, state, completion claimant and
  terminal result reference; and
- cleanup state, attempts, next retry and last closed cleanup reason.

`multipart_parts` has the session ID, bounded part number, exact range,
provider ETag, verified provider size and completion timestamp. Its unique
constraint is `(multipart_upload_id, part_number)`. The provider identifiers
are database-sensitive fields: no ORM repr, normal log or API schema exposes
them.

The plugin extends its existing journal SQLite schema with a session record
bound to the existing event ID and safe progress only: public session ID,
part geometry, session expiry, session state, completed part-number set and
the last closed retry/status token. It persists no URL, provider metadata,
staging key, digest or raw bytes. A crash before this local commit simply
replays preflight; a crash after it resumes the same session through status.

### 4.2 Server session state machine

```text
created -> uploading -> completing -> verifying -> promoting -> committed
   |           |             |              |             |
   +--------> cancelling ----+--------------+-------------+--> cleanup_pending
   |                                                          -> cleaned
   +--------> expired ---------------------------------------> cleanup_pending
   +--------> integrity_failed ------------------------------> cleanup_pending
   +--------> policy_denied ---------------------------------> cleanup_pending
```

`committed` is the frozen successful source-event outcome, not merely a
completed R2 multipart object. `integrity_failed` and `policy_denied` are
terminal for that frozen event and never publish. `cleanup_pending` is a
cleanup obligation, not permission to reuse the session. Exact client replay
of a terminal session receives its frozen safe result; it does not recreate
provider work.

`completing`, `verifying` and `promoting` are fenced by a durable claimant and
lease. A concurrent completion/status request returns the persisted state or a
safe retryable `multipart_completion_in_progress` result. Lease loss never
lets an old claimant mutate the replacement state.

### 4.3 Client state machine

```text
preflight -> session_persisted -> issuing_parts -> uploading_parts
    ^              |                    |                  |
    |              +-- suspend/offline -+------------------+
    |                                     resume -> status
    +-- expired / changed local bytes / terminal integrity -> next journal event

all required parts complete -> completion_requested -> terminal receipt
```

Before each part and before requesting completion, the plugin compares the
current Vault file to its frozen journal fingerprint. If it changed, it stops
the old session, keeps already observed local progress, requests exact abort
when online and leaves the newer watcher event to upload separately. It never
mixes bytes from two file generations. Server full-object verification remains
the authority if a race reaches R2.

## 5. API contract

All routes require the existing device Bearer credential and `obsidian_sync`
scope, use the canonical response envelope, expose stable safe error codes and
set `Cache-Control: no-store`. The OpenAPI snapshot and generated TypeScript
client are part of the same implementation change.

| Endpoint | Purpose | Safe response data |
| --- | --- | --- |
| `POST /api/uploads/multipart-sessions` | Create or exactly replay the session bound to a preflight operation. | session ID, geometry, expiry, state/status URL |
| `GET /api/uploads/multipart-sessions/{session_id}` | Resume after restart/suspend and reconcile provider-observed parts. | state, geometry, expiry, completed part numbers, terminal result if any |
| `POST /api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url` | Recheck authority/policy/state and issue one short-lived part URL. | one bearer URL plus its expiry; response is never persisted or logged |
| `POST /api/uploads/multipart-sessions/{session_id}/complete` | Claim completion; provider-list parts, complete, verify and promote. | pending state or frozen terminal source-event result |
| `POST /api/uploads/multipart-sessions/{session_id}/abort` | User/client abandonment request; does not erase journal evidence. | accepted terminal/cancel state |

The exact operation-token grammar and the preflight request's source/base
semantics remain the Child 4 contract. The implementation may render
`multipart_upload` directly from preflight or require the create endpoint as a
single idempotent follow-up, but both calls must bind the same frozen event and
produce the same single session. The implementation plan chooses one wire
shape, updates the OpenAPI snapshot and proves it by replay tests; it may not
make the plugin calculate routing itself.

The part-URL endpoint is the sole response allowed to contain a signed URL.
It must use an uncacheable response and must not write that field to an
application log. A client uses the URL directly with the exact byte range and
then discards it. The server discovers successful parts with provider
`ListParts`; it never accepts a client-provided completion manifest as proof.

## 6. Server behavior

### 6.1 Creation and resume

Within the existing preflight/idempotency lock order, the service validates
device status, workspace ownership, current policy, event replay, source/base
evidence and geometry. It persists the session before invoking R2 create
multipart, records enough durable recovery state to retry an ambiguous create,
and then stores the provider ID. If the provider call succeeds but the response
is lost, an exact replay resolves the existing session by its private staging
identity; it does not mint a second session.

The status endpoint queries R2 `ListParts` under an explicit deadline and
bounded retry, reconciles only the numbered completed parts that fit the exact
geometry, and returns their numbers. An unexpected number, size or provider
state is `multipart_provider_state_invalid`, stops completion and schedules
exact cleanup.

### 6.2 Part issuance and upload

For each requested part the server rechecks session ownership, nonterminal
state, expiry, policy and part range. It signs a `PUT` for only that part of
the private staging upload. Provider and transport errors map to typed,
retryable or terminal safe errors; no raw provider message reaches the client.

The plugin uses the existing Mobile-safe HTTP transport only for its
authenticated API calls. The presigned upload call has no service credential
and sends no application request body beyond the exact part bytes. It obeys
the platform concurrency cap and appends no bearer URL to trail entries.

An expired URL is a normal retry: status first, then request a replacement URL
for that one unfinished part. A 401/403 from a part URL is never treated as a
source authorization result; it is a closed `multipart_part_url_rejected`
retry/failure reason followed by status reconciliation. An offline/suspend
condition retains the SQLite session record and backs off only while the app is
eligible to run.

### 6.3 Completion, verification and publication

Completion is serialized by the durable claimant. The claimant:

1. Rechecks device, policy, expiry, source/base and frozen preflight evidence.
2. Uses `ListParts` to prove every required exact number/range; any absent,
   duplicate or inconsistent part fails closed.
3. Calls provider `CompleteMultipartUpload` with provider-observed part
   metadata, making one staging object at the exact private key.
4. Full-reads that staging object through a bounded verified stream and checks
   expected SHA-256, exact size and expected media type.
5. Streams the verified bytes through the Phase 1 conditional CAS writer.
   Existing same-digest bytes still receive the Phase 1 independent
   verification/deduplication behavior.
6. Creates the internal receipt and executes the existing source-event
   publication transaction under the established lock and idempotency rules.
7. Persists the frozen terminal result before returning it, then requests exact
   staging delete.

If the response after step 6 is lost, replay returns the stored result. If
publication loses its acknowledgement, existing evidence lookup/idempotency
recovers it; the service never assumes a failed response means it may publish
again. A stale base detected at any required recheck returns the existing safe
conflict/no-upload outcome; this child does not retain a verified candidate.

### 6.4 Expiry, cancellation and cleanup

At 24 hours, requests see `multipart_session_expired` and cannot acquire more
URLs or publish. The cleanup workflow claims expired/cancelled/terminal rows
using a database lease and only their stored exact resource identities:

- an incomplete provider upload receives provider `AbortMultipartUpload` for
  its exact upload ID;
- a completed staging object receives `DeleteObject` for its exact staging key;
- retrying a successful abort/delete or finding an already absent exact
  resource is successful cleanup; and
- no workflow invokes list, wildcard, prefix or canonical-object deletion.

Cleanup has explicit external-call timeouts, bounded retry and a finite
retention of closed failure state. If repeated cleanup cannot finish, the row
remains visible as `multipart_cleanup_failed` with the closed provider-stage
reason and an exact next retry time; it does not silently become a general-GC
task. Cancellation cleanup follows the same rule and only touches an
unreferenced staging resource.

Temporal owns scheduling/retry of expiry cleanup, not Redis. Its history holds
only opaque session IDs, state and closed reason tokens, never bytes, keys,
URLs, provider IDs or ETags.

## 7. Policy, privacy and diagnostics

Policy reevaluation is required at every server boundary. A policy advance
from allow to deny prevents a new URL, completion and publication. A part
already uploaded through an unexpired URL remains noncanonical and enters
exact cleanup. `excluded` preserves local Vault bytes and the journal's
readable terminal state; it never becomes a hidden retry loop.

The child registers a closed error vocabulary, with one status/retryability
mapping per token, including:

```text
multipart_session_not_found
multipart_session_expired
multipart_session_state_invalid
multipart_part_invalid
multipart_part_url_rejected
multipart_provider_state_invalid
multipart_completion_in_progress
multipart_integrity_failed
multipart_policy_denied
multipart_cleanup_failed
multipart_local_content_changed
multipart_dependency_unavailable
```

The exact final token names are registered once in the central error registry.
Input/state/integrity/policy errors are non-retryable except where a fresh
event/session is explicitly permitted. Offline, timeout, 429 and typed
dependency outages are retryable with existing bounded jitter/backoff. Unknown
exceptions fail closed through the existing safe internal error.

Plugin trails and status expose only operation/session state, platform class,
part counts, bounded attempt count, duration and closed token. Server
diagnostics/metrics use only low-cardinality labels: outcome, state, platform
class, stage and safe error code. No metric label contains any identifier,
locator, filename/path, digest, URL, provider metadata or request ID.

Every new closed path must return a typed error, persist a safe status/blocker,
append a closed plugin trail entry or write a closed structured API event. No
catch may discard its causal reason. Leak scanners cover raw content,
locators/paths, full digests, access credentials, signed URLs, staging keys,
provider IDs, ETags and provider exception text.

## 8. Failure matrix

| Condition | Required result |
| --- | --- |
| App suspends/offline after some parts | Persisted event/session remains resumable; status reconciles completed parts before new URLs. |
| Part URL expires | Request status, then issue a new URL for only the unfinished exact part. |
| File changes during transfer | Stop old session, surface `multipart_local_content_changed`, exact-abort when possible; newer watcher event is separate. |
| Provider accepts unexpected part shape | Mark provider state invalid; no complete/publish; exact cleanup. |
| Missing, duplicate or wrong-size part | Integrity failure; no complete/publish; exact cleanup. |
| Complete response lost | Status/replay returns frozen state/result; no duplicate complete or publication. |
| Staging SHA-256/size/media mismatch | Terminal integrity failure; never creates receipt/version; exact cleanup. |
| CAS race/dedup | Reuse existing verified Phase 1 behavior; one canonical identity, no overwrite. |
| Policy changes to deny | No new part URL/complete/publish; preserve local bytes; staging cleanup. |
| Device revoked or token refresh fails | Preserve local event and safe progress; require reauthentication, then recheck policy/session. |
| Session expiry | Never resume/publish that session; clean exact resource; re-preflight same frozen event only if still eligible. |
| Cleanup call timeout/failure | Bounded retry with `multipart_cleanup_failed`; exact identity only and readable reason. |
| Client cancellation | Server terminalizes cancellation and exact-cleans; local event remains auditable/retryable only by explicit normal requeue semantics. |

## 9. Test strategy

Implementation is test-first. It adds the smallest behavior after a focused
failing test, then runs the applicable lint, strict type check, API contract,
generated-client and integration gates.

### 9.1 Unit, property and API tests

- Geometry boundaries: 16 MiB routes single-part; `16 MiB + 1`, 8 MiB edges,
  100 MiB and 13-part max route correctly; over-limit input fails closed.
- Session/event/device/workspace binding, policy rechecks, expiry, unique part
  geometry and exact replay.
- Presign capability proves exact session/part/range/lifetime and proves no
  canonical key or provider identity enters ordinary wire data.
- Client crash/suspend/restart, URL expiry, completed-part reconciliation,
  Desktop/Mobile concurrency caps and changed-file separation.
- Completion races, lease fencing, lost create/complete/publication response,
  provider part mismatch and every terminal outcome.
- Full-object verification, CAS promotion, dedup and stale-base no-candidate
  behavior.
- API envelope/auth/error mapping, OpenAPI snapshot and generated TypeScript
  client compilation.
- Plugin SQLite upgrade/downgrade and preservation of existing journal,
  reservation, cursor and reconciliation data.
- Static Mobile tests prohibit Node/Electron-only imports and prove URL/provider
  material cannot reach SQLite/trails/settings.

### 9.2 PostgreSQL, R2 and Temporal integration

- Empty/fixture migration upgrade, downgrade and schema ownership checks.
- Concurrent create/resume/complete for one event and competing device/session
  authorization attempts.
- Scripted R2 create/list/complete/abort/delete failures with typed mappings,
  timeouts and exact retry bounds.
- Real disposable R2 test-bucket multipart upload, corrupt staging failure,
  conditional promotion/dedup and exact cleanup after success, cancellation,
  expiry and injected failure. The test harness records only its exact
  staging-key/upload-ID allowlist and never lists or prefix-deletes objects.
- Temporal expiry workflow replay/lease/failure recovery without raw identity
  material in history or logs.

### 9.3 Privacy and live acceptance

Leak tests inject sentinels into content, names, locator, digest, token, signed
URL, staging key, provider ID/ETag and provider exception. They scan logs,
trails, status/settings export, OpenAPI examples, JUnit and retained live
artifacts.

The final commit must pass a Desktop WDIO journey on a fresh disposable
`knowledge-ci-*` stack and a physical Mobile journey using the repository
bootstrap/tunnel contract. They prove:

1. an interrupted multipart upload resumes without retransmitting recorded
   completed parts and commits exactly one source event;
2. Mobile suspension/resume retains durable progress and respects the two-part
   concurrency cap;
3. a corrupt/missing/wrong-size part fails integrity and cannot publish;
4. policy denial after a prior part URL prevents promotion and leaves no
   staging resource outside exact cleanup; and
5. completion acknowledgement loss resolves through status/replay without a
   duplicate version or object.

Desktop mock or unit evidence never substitutes for the physical Mobile gate.
The live procedure uses `bash .local/serve-live-ci.sh up knowledge-ci-<name>`
and tears it down with the paired `down` command. Missing required credentials
or an unready external prerequisite is a visible blocked gate, not a skip.

## 10. Acceptance criteria

The child is complete only when one final commit demonstrates all of the
following:

1. Files over 16 MiB through 100 MiB route through a server-owned multipart
   plan with exact geometry and platform concurrency bounds.
2. A Mobile interruption, restart and suspend/resume continue only unfinished
   parts from SQLite-safe progress; no presigned URL/provider metadata persists.
3. Every session/part URL is credential-, workspace-, device-, event-, part-
   and expiry-scoped and cannot target a canonical object.
4. Exact replay, duplicate completion and ambiguous provider/publication
   responses produce one frozen source-event result only.
5. Missing/corrupt/mismatched staging bytes never create a receipt, version or
   canonical object publication.
6. Promotion uses the Phase 1 verified conditional CAS writer and retains its
   same-digest dedup/concurrency guarantees.
7. Policy advance, revocation, local file change, expiry and cancellation
   preserve local data, surface closed reasons and cannot publish staging.
8. Success, abort, integrity failure, expiry and injected cleanup failure use
   only each session's exact staging identity; no list/wildcard/prefix/canonical
   delete is present in production or test cleanup.
9. Every new closed failure path has a readable reason surface and all leak,
   type, migration, contract, generated-client and relevant integration gates
   pass.
10. Desktop WDIO and physical Mobile acceptance both pass with sanitized
   evidence from the documented disposable live stack.

## 11. Documentation, handoff and deferred boundaries

The implementation updates canonical documents `04`, `07`, `11`, `12`, `14`,
`15`, `16` and `20`, plus the plugin README and a living multipart operations
runbook. Any OpenAPI change updates the generated client and contract tests in
the same change.

At plan completion/interruption, write exactly one Child 7 handoff under
`docs/handoff/YYYY-MM-DD-resumable-multipart-mobile-upload.md`, including the
final commit SHA, all gate evidence, spec interpretation decisions, deferred
rulings and next actions. Each still-deferred item gets exactly one BACKLOG row
with a verifiable `Implement by` trigger.

Child 8 alone owns candidate bytes/conflict capture and resolution. Child 9
owns cross-slice Phase 2 acceptance/operations closure. General canonical
object GC, R2 Worker/hybrid routing and provider switching remain outside this
program unless a future canonical decision explicitly adopts them.

## 12. References

- `docs/00-PRODUCT_VISION_AND_PRD.md`
- `docs/01-CANONICAL_ARCHITECTURE.md`
- `docs/02-TECH_STACK.md`
- `docs/03-DATA_OWNERSHIP_AND_STORAGE.md`
- `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- `docs/07-POSTGRESQL_DATA_MODEL.md`
- `docs/11-TEMPORAL_WORKFLOWS.md`
- `docs/12-API_MCP_AND_AGENT_INTEGRATION.md`
- `docs/14-SECURITY_PRIVACY_AND_POLICY.md`
- `docs/15-OBSERVABILITY_AND_ALERTING.md`
- `docs/16-TESTING_AND_EVALUATION.md`
- `docs/19-ARCHITECTURE_DECISIONS.md`
- `docs/20-IMPLEMENTATION_PLAN.md`
- `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`
- `docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md`
- `docs/superpowers/specs/2026-08-18-plugin-journal-and-small-file-sync-design.md`
- `docs/superpowers/specs/2026-08-26-device-cursor-and-manifest-reconciliation-design.md`
