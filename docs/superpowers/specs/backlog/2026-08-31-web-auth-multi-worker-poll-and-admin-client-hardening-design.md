# Web-auth multi-worker poll and admin client hardening — design spec

Date: 2026-08-31. Domains: web authentication (grant polling, throttle
buckets, acceptance journeys), Web Admin API clients. Governing docs:
`docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md`
(sections 11.4, 15.8), its handoff §9/§13 rulings, and the 2026-08-30
child-nine hygiene retirement handoff (deferred items 1 and 6).

## Purpose and scope

Retire four indexed BACKLOG rows that need no Child 8/9 work and no mobile
device, chosen so they share one domain and one review surface:

1. 2026-08-16 web-auth §9 — poll replay digest single-key (plus slow-down
   hint under-report, unknown polling credentials unthrottled, pacer counts
   pending only). Gate: before key rotation or multi-worker serve.
2. 2026-08-16 web-auth §13 — multi-worker poll pacing needs a poll
   `bucket_kind` (schema + spec amendment) or a shared pacing store.
   Gate: before multi-worker serve.
3. 2026-08-30 web-auth acceptance — 2 `web-security.spec.ts` journeys stale
   vs the removed first-login TOTP offer (commit 99fe1c3); auth Playwright
   journeys wired into no CI gate (`authentication-acceptance.yml` runs
   pytest-only `poe authentication-test`; Poe task `authentication-e2e`
   orphaned). Gate: before Child 9 operations acceptance.
4. 2026-08-30 web admin api clients — duplicate
   `REQUEST_UNAVAILABLE_ERROR`/`unwrapEnvelope` in
   `apps/web/src/api/exclusion-policy-client.ts` (re-imported by
   `source-lifecycle-client.ts`) instead of the shared
   `authentication-client.ts` exports. Gate: before Phase 2 closure.

Out of scope: keyring rotation mechanics themselves (already two-key),
plugin-side poll behavior changes, mobile evidence rows, and every other
web-auth deferred batch already ruled "when next touched".

## Problem

The grant poll path is single-process and single-key in three compounding
ways. A keyring rotation mid-grant breaks the pending/exchanged poll replay
because the replay digest map has one key; the slow-down hint under-reports
after back-off; polling attempts with unknown credentials bypass throttling
entirely; and the in-memory `GrantPollPacer` counts only pending polls.
Separately, multi-worker `serve` is impossible because pacing state lives
in process memory — the child-2 adjudication (handoff decision 3) ruled the
in-memory pacer stands only until a poll bucket kind joins the closed
schema set or a shared pacing store exists. Meanwhile the auth browser
journeys rotted invisibly (no CI wiring) and the Web Admin API clients
carry verbatim-duplicated envelope helpers.

## Compatibility contract

- `authentication_throttle_buckets` (spec 15.8): the closed `bucket_kind`
  set gains exactly one new member for grant-poll pacing (proposed token
  `grant_poll`; final token ratified at plan review with the spec 15.8
  amendment). `(bucket_kind, bucket_hash)` uniqueness and the
  no-raw-username/address invariant are unchanged. Wherever the closed set
  is enforced (application vocabulary and/or DB constraint), the extension
  lands together with an Alembic revision carrying upgrade/downgrade
  tests per repo rules.
- Sections 11.4 and 15.8 of the web-auth design spec are amended in the same
  effort for the durable poll pacing and the poll bucket kind, and §12.2
  gains the two-key replay digest boundary (the handoff §13 ruling names
  schema + spec amendment as the required pair).
- Public wire behavior is otherwise preserved: poll outcomes, error codes,
  429 envelope shape and retry hints keep their existing contracts. No new
  production dependency. OpenAPI changes only if a plan-review decision
  makes a hint field honest (see C2) — then snapshot, generated client and
  contract tests move together.

## Contracts

### C1 Multi-key poll replay digest

The pending/exchanged poll replay digests are stored per signing key id
(mirroring the two-key keyring model), so a rotation between two polls of
the same grant still verifies a byte-identical replay against the digest
recorded under the key that signed it. A genuinely new poll under the new
key resolves as new work, not replay. Digest entries follow the existing
retention rules; retirement of the previous key makes its digests
unverifiable through the existing expired/invalid credential codes — no
invented fallback path.

### C2 Poll-path pacing ride-alongs (handoff §9)

- The slow-down hint reflects post-back-off state (no under-report after a
  backed-off window).
- Poll attempts with unknown credentials acquire a throttle bucket through
  the same closed table — existence of a credential is not leaked; the
  response shape for unknown credentials is unchanged.
- The pacer's accounting covers all poll attempts in scope, or its scope is
  renamed to what it counts — plan review picks one; either way the metric
  docstring and the operations runbook line state the truth.

### C3 Durable poll pacing (handoff §13)

Pacing authority moves to `authentication_throttle_buckets` under the new
`bucket_kind`: multi-worker `serve` throttles correctly because every
worker reads and writes the same durable bucket. The in-memory
`GrantPollPacer` is either removed or reduced to a per-process cache that
can never under-throttle relative to the durable bucket (plan decides;
single-worker latency and behavior must not regress). Bucket writes use the
established upsert pattern (the first-insert race fix precedent), and the
single-process no-durable-pacing note in the operations runbook's
reverse-proxy section is rewritten to describe the durable behavior.

### C4 Restore the auth browser journeys under the existing CI gate

The two stale `web-security.spec.ts` journeys are rewritten against the
current TOTP flow (the first-login offer removed at 99fe1c3; enrollment
follows the approved security-page flow). Plan-time verification (git
`0606cf7`): the `quality.yml` `authentication-e2e` job has run
`uv run --all-packages --frozen poe authentication-e2e` since the child-2
acceptance commit — the 2026-08-30 handoff's "wired into no CI gate"
claim is stale. The deliverable is therefore the journey rewrite plus a
verified green run through that existing gate (browser-only, no stack
references, per the committed CI-security contract), so the rot this
class represents fails CI from now on.

### C5 Single envelope-helper source

`REQUEST_UNAVAILABLE_ERROR` and `unwrapEnvelope` are exported once from
`authentication-client.ts`; `exclusion-policy-client.ts` and
`source-lifecycle-client.ts` import them. A workspace-wide search (recorded
in the plan report) proves no other verbatim duplicate remains. No behavior
change; existing client tests stay green untouched.

## Privacy invariants (acceptance-critical)

- No raw username, source address, credential or token in any bucket row,
  digest record, log line or metric label — the 15.8 invariant and the
  authentication leak sentinel scope extend to the new bucket kind.
- New closed paths surface only existing registry codes; no paths, hostnames
  or exception text.

## Acceptance criteria

1. RED→GREEN: a rotation-between-polls exact-replay test fails on the
   single-key map and passes on C1; unknown-credential pacing and the
   slow-down hint each get a RED test first; the two rewritten journeys are
   RED against the removed flow's expectations before the rewrite.
2. Migration gates: alembic upgrade/downgrade over the new revision,
   empty-database upgrade, and the schema-contract tests pass.
3. Multi-worker proof: an integration test driving two pacers (or two
   composed app instances) against one store shows one shared throttle
   budget, not two.
4. Full offline gates green: `uv run poe verify`, `uv run poe
   authentication-test`, `uv run poe api-contract-check`, workspace web
   tests and builds; the wired CI gate runs the auth journeys on the next
   push and its first run is observed and recorded.
5. Each of the four BACKLOG rows is removed in the diff that closes it.

## Error cases

- Previous-key digest miss after key retirement: existing
  expired/invalid-credential codes, typed and closed; no silent acceptance.
- Bucket upsert contention under rotation: resolves through the upsert
   pattern; a concurrent-window test pins it.
- Durable bucket unavailable during a poll: the existing dependency-error
  family applies — pacing state is canonical PostgreSQL state, never
  silently bypassed by falling back to memory-only admission.
