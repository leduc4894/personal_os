# Exclusion Policy Publication Handoff

**Date:** 2026-08-17
**Plan:** `docs/superpowers/plans/2026-08-17-exclusion-policy-publication.md`
**Spec:** `docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md` (status: implemented; see its header)
**Parent:** `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md` (child 3)
**Branch:** `master` (repo convention). Design commit `1e7f270`; implementation
range `7d9a470..94a8a06` (Tasks 1–13); **last implementation SHA `94a8a06`**
(`test: strengthen acceptance workflow mutation contract`). Task 14
(documentation: runbook, canonical status, this handoff, doc contract tests)
lands as the single commit `docs: complete exclusion policy publication`; its
SHA and the full re-run evidence are recorded in the task report
`.superpowers/sdd/2026-08-17-exclusion-policy-publication/task-14-report.md`.

Living operational status: `docs/operations/exclusion-policy-publication.md`
(initial trust, empty-policy publication, preview/publish, key rotation, lock
order, recovery limits, degraded states, backup/restore, rollback-by-new-
revision, gates). Canonical status: `docs/20-IMPLEMENTATION_PLAN.md` (Phase 2,
child 3 complete), `docs/14-SECURITY_PRIVACY_AND_POLICY.md` §5,
`docs/16-TESTING_AND_EVALUATION.md` §10.

**This child is NOT fully closed:** the reference-device verification records
are absent (§4, blocking). Everything else below is green with command
evidence.

## 1. What was built (Tasks 1–14)

Domain (`src/personal_os/exclusion_policy/`): closed deny-only rule model and
normalization, RFC 8785 canonical JSON, Ed25519 snapshot/keyset signatures,
drafts with CAS, async preview, atomic signed publication, fail-closed
enforcement service, reconciliation domain. PostgreSQL adapters
(`packages/postgresql-source-store/policy_*`), migration `20260817_01_add_exclusion_policy_publication`
(Alembic single head `20260817_01`). Offline key lifecycle CLI
(`personal-api policy-key initialize|stage|activate|retire`), Admin/plugin
routes (`apps/api/src/api_runtime/exclusion_policy_*`), Web Admin
`/admin/policy`, plugin policy acquisition/verification + TS evaluator parity
(`apps/obsidian-plugin`), canonical-core `phase-one-acceptance` seeding the
signed empty policy (`tools/signed_policy_seed.py`), reconciliation dispatcher
(workers), CI workflow `.github/workflows/exclusion-policy-acceptance.yml`,
performance suite, and the Task 14 documentation set (runbook + canonical
status + doc contract tests in `tests/contract/test_bootstrap_documentation.py`
and `tests/unit/tools/test_canonical_core_operations.py`).

## 2. Gate evidence

Reference host for every run: Windows 11 10.0.26200, AMD64, CPython 3.14.6,
PostgreSQL 18.4 / Temporal 1.31.2 via Docker Compose (documented in the
performance suite header). Final Task 13 clean runs; Task 14 re-ran the three
operator gates after the documentation changes (report has both outputs).

| Gate | Evidence |
| --- | --- |
| `uv run poe exclusion-policy-test` | exit 0 — Task 13 final run **1382 passed, 2 skipped**; Task 14 re-run over the documentation tree (2026-08-17) **1383 passed, 2 skipped** in 1201 s (the delta is Task 13's own post-run review-fix tests at `94a8a06`, plus nothing from Task 14's docs-only diff — the two new doc-contract test files are outside this gate's path list). Skips are the pre-existing Windows platform cases (POSIX permission-bits + symlink in `tests/unit/api_runtime/test_exclusion_policy_settings.py`); 1 deselected = device records (§4). A first Task 14 attempt errored 3 backup/restore tests solely because the host had lost the pinned pg 18.4 client tools from PATH; after restoring exactly `pg_dump (PostgreSQL) 18.4` / `pg_restore (PostgreSQL) 18.4` the same suite passed — no code or test changed between the two runs. |
| `pnpm run test:e2e:exclusion-policy` (`CI=true PLAYWRIGHT_WEB_PORT=3200`; host excludes 3100) | exit 0 — 1 passed (11.8 s; Task 13: 11.4 s) |
| `uv run pytest tests/performance/test_exclusion_policy_performance.py -m local_stack -q` | exit 0 — 5 passed, all four budgets: evaluator p95 **0.046 ms** (≤5 ms), snapshot verify p95 **3.65 ms** (≤50 ms), preview 10,000 subjects **3.42 s** (≤30 s), reconciliation 10,000 sources **3.66 s** (≤300 s) |
| `uv run poe verify` | exit 0 (format-check, lint, type-check, boundary-check, test+coverage — full suite 2733 passed, 21 skipped, 305 deselected — Web/plugin builds) |
| `uv run alembic heads` | exactly one head: `20260817_01` |
| Canonical-core deferred gate (`tests/integration/canonical_core -m "local_stack and not r2_live"`) | exit 0 — 18 passed, 6 deselected (r2_live needs CI credentials); fixed the latent `CANONICAL_COUNT_TABLES` 9→19 manifest bug en route (Task 13) |
| Doc contract tests (Task 14) | written red-first (16 failed), green after the runbook/status/handoff landed |

No gate that needs the device records was silenced: the `device_records`
marker is deselected from intermediate runs and **fails — never skips — under
explicit selection** (`uv run poe exclusion-policy-device-verification`).

## 3. Spec interpretation decisions (implementation ledger, reviewer-verified)

1. **Keyset payload / rotation overlap (Tasks 2+5).** The keyset payload layer
   allows "at most one current key" (staged-only builds are legal); the
   signer==current enforcement lives at startup/publication (spec 13.1).
   Activation declares the old current key `staged` (closed
   current/staged/retired vocabulary) until the retire revision — this is the
   spec 13.3 step-6 overlap, not a new state.
2. **Signing inside the locked transaction (Task 7).** `commit_publication`
   takes a `SignedSnapshotBuilder` callable so the canonical payload is built,
   signed and self-verified while the serialization row is locked; the domain
   never sees pre-allocation identity. `resolve_committed` takes the
   DiagnosticContext for audit correlation; the active-parent recheck precedes
   the draft-table guard and surfaces the retryable `snapshot_outdated` with
   the current revision number.
3. **Reconciliation vocabulary (Tasks 3+12).** Prior-decision fallbacks: no
   parent revision ⇒ previous `excluded`; no prior evaluation row ⇒ previous
   `allowed`. Intent states are `pending | leased | dispatched | terminal`
   with **dispatched as the resting state** after the workflow acknowledges;
   idempotent completion replays use `ALLOW_DUPLICATE_FAILED_ONLY`. The
   state vocabulary is implementation-owned (spec 15 left it open).
4. **Plugin transport and failure mapping (Task 10).** The plugin talks to the
   two GET policy routes through a pure adapter in `request-url-transport.ts`
   (the obsidian package has no resolvable entry under Vitest). Non-200
   mapping: 409 `not_initialized`, 429 and 5xx are network-class (retry,
   previous cache preserved); integrity failures (bad bytes/hash/signature/
   revision) are a distinct class that sets `policy_integrity_failed` and
   blocks sync. A completed re-onboarding atomically **replaces** the previous
   trust anchor. The trust-reset flow after integrity failure stays later
   scope per spec 13.3.
5. **Enforcement composition (Task 11).** `tools/canonical_core_operations.py`
   (the CLI) is untouched by enforcement wiring because of a real import
   cycle; the guard composes at the canonical services instead. Sync preflight
   is a non-authoritative hint — every authoritative boundary re-evaluates
   under the policy-state lock.
6. **`X-Idempotency-Key` (Task 8).** No prior HTTP idempotency header
   convention existed in the repository; the header is a route-local constant
   of the policy publication route, to be promoted to `api_contracts` when
   source-publication routes need one.
7. **Preview events (Task 6).** Spec 21 defines no preview-completion event;
   preview outcomes surface only through the two metrics, and
   `preview_requested` is the single preview audit row.
8. **Spec 9 wording gaps (Task 9, recorded not fixed).** The spec-17 "signer
   fingerprint" is rendered from `signing_key_id` (the status contract carries
   only that); the spec-10 "polling guidance" member is absent from
   `PolicyPreviewData` (the server rechecks state server-side).

## 4. BLOCKING deferred item — reference-device verification records

**`docs/operations/exclusion-policy-device-verification.md` does not exist.**
Desktop AND Mobile Obsidian reference-device verification of initial trust,
snapshot verification, rotation, offline cache and Vault preservation have not
been recorded. `uv run poe exclusion-policy-device-verification` fails under
explicit selection — correctly, because the evidence does not exist. No
placeholder was fabricated. Recording both device sections (five labeled
outcomes each plus a dated operator line, per
`tests/contract/exclusion_policy/test_reference_device_records.py`) is a
**precondition for calling this child complete**; the runbook's
"Reference-device verification" section describes the procedure. BACKLOG §
indexes this line.

## 5. Other accepted deferred rulings (BACKLOG-indexed)

1. **Mutation testing** — the spec's testing strategy explicitly defers it;
   this handoff records the ruling and the BACKLOG line (mandated by the Task
   14 brief). No mutation run was performed for this child.
2. **TanStack Query vs plan dependency pin (spec 17 amendment).** Spec 17
   mandates TanStack Query; the plan pins `@noble/ed25519` as the only new
   dependency and AGENTS.md gives the more specific document priority. The
   existing effect pattern stands. The **plan owner must amend spec 17** (or
   schedule the dependency) before the next Web child builds on `/admin/policy`.
3. **CI first real-runner execution.** `.github/workflows/exclusion-policy-acceptance.yml`
   was validated by its pinned contract tests and every gate it orchestrates
   ran locally with the same pinned toolchain, but it has never executed on a
   real GitHub runner — watch its first PR run (also see §6, paths filter).

## 6. Deferred code-level minors (handoff-only; one-line dispositions)

Triaged by the final implementation review; none blocks the contract, none
got a BACKLOG line by instruction:

- Metrics (spec §21 deviation): `exclusion_policy_snapshot_verification_total{client_class,outcome}`
  is unimplemented — no recorder or exporter exists in `src/`, `apps/` or
  `packages/`; a future task/child that owns client-side verification
  observability must implement it (or amend spec 21). The runbook now lists it
  as spec-planned, never as a live metric.
- Evaluator/normalization: `..pdf` (two leading dots) accepted by
  `_normalize_extension`; add rejection + golden error case. Folder-prefix/
  path-glob operands trust normalization by convention; make the grammar
  check structural. Duplicate semantic fingerprints have no DB unique floor
  (domain rejection only).
- Crypto/signatures: PEP 758 unparenthesized multi-except (valid on the 3.14
  pin); revision-1 keyset test vector uses a staged key (prefer current);
  rule/keyset sort comments lack the spec clause reference.
- Key lifecycle CLI: stage-command replay branch untested; initialize/stage
  generate the key file before guards (orphan file on rejection); ~30 lines
  of keyset+audit SQL duplicated from the Task 4 store; `staged_key_exists`
  blocks a second staged key (stricter than spec's four-non-retired — needs a
  README line).
- Drafts/publication store: `_MappedRow`/`_select_now` duplicated across
  policy stores (extract at third module); `PolicyKeysetRecord` permits empty
  payload bytes while the envelope rejects; contention-exhausted maps to
  `commit_outcome_unknown` (over-claim vs known rollback — a future
  `concurrency_busy` code needs a spec 19 amendment); draft uncertain-commit
  recovery covered piecewise by Tasks 7/9.
- Previews: READY metric double-increments on idempotent ready replays;
  `_load_ready_preview_record` misnomer; inline `SafeToken.parse("preview_limit_invalid")`
  bypasses the closed registry; duplicate `_context()` test helper;
  integration tests order-dependent on module-seeded draft rules.
- Publication service: `audit_event_id` allocated but unused; preflight
  idempotency-mismatch path skips the rejected-outcome metric and can mislabel
  server-side corruption; `SafeToken.parse` on corrupt codes raises untyped
  mid-transaction.
- API/routes: `KEYSET_PAGE_MAXIMUM` (spec 13.3 bound) lives in the adapter
  layer — promote on a second consumer; plugin read SQL (keyset join/snapshot/
  reconciliation) covered by offline doubles only, no live-table integration
  test.
- Web Admin: publish dialog sends an empty-string impact digest when the
  ready preview has none (disable instead); dead "terminal" panel variant; no
  dialog focus trap / conflict-notice focus.
- Plugin: settings/policy-cache read-modify-write lost-update window
  (serialize when a background writer lands); `KEYSET_PAGE_MAXIMUM_ENVELOPES`
  declared but unenforced; strict-json accepts `-0` (canonical encoder emits
  0); cache-less startup state reads as tampering (spec-correct; distinct
  reason token for later status UI).
- Enforcement harness: `TRANSACTION_MODULE_NAMES` lacks `policy_enforcement.py`
  + `canonical_read.py`; two services dropped `frozen=True` for the injectable
  guard; preflight metric label (`SINGLE_PART_UPLOAD`) mismatches the recheck
  (`SOURCE_CREATE_UPDATE`); `tools/signed_policy_seed.py` must never enter a
  production composition root.
- Reconciliation: excluded→allowed-from-recorded-prior has no integration
  test; intent replay verifies operation only (`source_version_id` selected
  but unasserted); transition counters re-record on idempotent batch replay
  (pin with a comment).
- Acceptance/CI: failure-recovery test raises bare `Exception` (assert typed
  `DBAPIError`); `test_no_public_api` line masking drops any line mentioning
  exclusion-policy (over-broad); `asyncio.sleep(3)` backoff wall-time needs a
  clock seam; the 90-minute CI job runs on every `pull_request` with no paths
  filter.

## 7. Next actions

1. **Record the Desktop and Mobile reference-device verification evidence**
   (§4) in `docs/operations/exclusion-policy-device-verification.md`, then run
   `uv run poe exclusion-policy-device-verification` to green. Only then may
   the child be called complete.
2. Amend spec 17 (TanStack Query) or schedule the dependency (§5.2).
3. Watch the first real-runner execution of `exclusion-policy-acceptance.yml`
   (§5.3); consider the paths filter from §6.
4. Pick up §6 minors opportunistically in the owning files; the duplicate-
  semantic-fingerprint DB floor and the `concurrency_busy` spec-19 amendment
   are the two that touch contracts and need small specs of their own.
5. Child 4 (Vault sync) and the projection-phase registration of
   `policy-projection-transition` workflows consume the pending policy-origin
   intents; do not claim them here.
