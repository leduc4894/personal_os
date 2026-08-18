# Plugin Journal and Small-File Sync Handoff

**Date:** 2026-08-18
**Plan:** `docs/superpowers/plans/2026-08-18-plugin-journal-and-small-file-sync.md`
**Spec:** `docs/superpowers/specs/2026-08-18-plugin-journal-and-small-file-sync-design.md`
**Parent:** `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md` (child 4)
**Branch:** `plugin-journal-small-file-sync` (worktree). Plan-landing commit
`dd2c41e`; implementation range `47e1c95..f68f2c9` (Tasks 1–11); **last
implementation SHA `f68f2c9`** (`docs: explain transient login state after
policy change`). Task 12 (acceptance, documentation, this handoff) lands as
the single commit `docs: hand off small-file sync completion`; its SHA and
the full re-run evidence are recorded in the task report
`.superpowers/sdd/2026-08-18-plugin-journal-and-small-file-sync/task-12-report.md`.

Living operational status: `docs/operations/plugin-journal-small-file-sync.md`
(startup order, queue state, safe diagnostics, recovery generations,
`reconcile_required`, size/policy blocks, scenario table, operator evidence
procedure, acceptance gates). Wire contract:
`tests/fixtures/small_file_sync/wire-golden.json`. Canonical status:
`docs/20-IMPLEMENTATION_PLAN.md` Phase 2 lists children 1–3 only and tracks
no child-4 status line, so it was left untouched (task-12 rule: update only a
status the document already tracks).

**Status: automated acceptance GREEN from one commit; reference-device
evidence PENDING (deferred by design — §5).** Everything an agent can verify
is green with command evidence below; the child is complete except for the
operator-observed device rows, exactly as the plan's acceptance design
anticipated.

## 1. What was built (Tasks 1–11)

Plugin (`apps/obsidian-plugin/src/journal/`): closed journal contracts and
safe labels; pinned `sql.js` WASM dependency; verified SQLite generations
(`journal.sqlite.g<n>` + manifest, current-plus-one retention, torn-write
fallback, empty rebuild with `reconcile_required`); repository with immutable
fingerprints and pre-preflight-only coalescing; capture with 250 ms settling,
policy gate, born-terminal blocks and explicit `Sync existing files` scan
(never automatic); bounded foreground queue driver (one active request,
jittered backoff 1 s–5 min, same idempotency identity); authenticated
preflight/content transport; closed status projection, `Sync now`, safe
unload flush. Plugin bundle ships exactly `main.js`, `manifest.json`,
`sql-wasm.wasm`.

Server: `src/personal_os/small_file_sync/` (contracts, closed errors, ports,
service, redacted metrics) — preflight re-evaluates the active signed policy
server-side, reserves create UUIDs without inserting `sources`, verifies
streamed bytes through the existing bounded spool/CAS path before canonical
publication, and freezes a replayable terminal receipt; durable
`small_file_upload_operations` store (migration `20260818_01`, single Alembic
head) keyed by workspace/device/event/idempotency; two routes
(`POST /api/sync/journal-events/preflight`, `PUT /api/uploads/{operation_id}/content`)
under the `obsidian_sync` device scope; production serve composition with the
r2-object-storage package as the first production object-store composition;
regenerated OpenAPI snapshot and TypeScript client. No route ever returns a
receipt, object key or provider detail.

## 2. Gate evidence (Task 12, final runs from one commit)

Reference host: Windows 11 10.0.26200, CPython 3.14, the pinned uv/pnpm
toolchain. Default pytest selection deselects `local_stack`, `r2_live` and
`device_records` markers (deselected counts below are those classes).

| Gate | Result |
| --- | --- |
| `uv run pytest tests/unit tests/contract -q` | **2947 passed, 21 skipped, 1 deselected** in 94.79 s. First run failed once on the boundary scan (see §4.5); after the sanctioned-surface amendment the full suite passes. 21 skips are pre-existing Windows platform cases; 1 deselected is the device-records marker. |
| `uv run pytest tests/integration -q` | **22 passed, 312 deselected**, 1 warning in 4.43 s — the offline cross-boundary suites (small-file sync wire journey, 16 MiB boundary, policy/device boundaries) over disposable doubles; deselected are the local-stack/r2-live classes. |
| `uv run pytest tests/unit/migrations -q` | **39 passed** in 1.04 s (also inside the unit run; upgrade/downgrade covered). |
| `uv run poe python-lint` | **All checks passed!** (ruff over src, apps, packages, tests, tools). |
| `uv run poe python-type-check` | **Success: no issues found in 161 source files** (mypy strict scope). |
| `pnpm --filter @workspace/obsidian-plugin test` | **25 test files, 365 passed**. |
| `pnpm --filter @workspace/obsidian-plugin build` | exit 0; `dist/` exactly `main.js`, `manifest.json`, `sql-wasm.wasm`. |
| `pnpm --recursive run lint` | exit 0 — eslint (`--max-warnings=0`) for api-client, web, obsidian-plugin. |
| `pnpm --recursive run type-check` | exit 0 — `tsc --noEmit` Done for all three packages. |
| `uv run poe api-contract-check` | exit 0 — `api_contract_current` snapshot check and openapi-typescript `generate:check` both pass. |
| Reference-device gates | **PENDING — deferred by design** (§5). No automated substitute, no fabrication. |

The operator-facing re-run commands live in
`docs/operations/plugin-journal-small-file-sync.md` ("Acceptance gates").

## 3. Acceptance checklist disposition

1. Journal writes and recovers verified SQLite generations on Desktop and
   Mobile without native APIs — **pass (automated)**: generation
   write/verify/torn-write/prior-fallback/empty-rebuild in
   `persistence.test.ts` and `sqlite-database.test.ts`; native-API ban
   (`sql.js` WASM + `DataAdapter` only; no Node/Electron/
   `FileSystemAdapter`/native SQLite) enforced by
   `tests/contract/api/test_plugin_authentication_bundle.py`
   (`test_plugin_sources_never_reference_forbidden_platform_modules`,
   `test_bundle_boundary_permits_the_vendored_wasm_sqlite_package`). The
   physical-device observation rides the deferred item in §5.
2. No automatic initial scan or background retry exists — **pass**:
   capture tests pin "startup does not scan" and the confirmed explicit scan;
   queue-driver tests pin foreground-only passes, one active request, no
   transport auto-retry, stop on unload/suspend.
3. Queued changes coalesce only before preflight; frozen content gets a
   successor event — **pass**: `repository.test.ts` replacement windows and
   `journal-sync-journey.test.ts` mid-pass freeze/successor scenarios.
4. An allowed file up to 16 MiB publishes exactly once through the API —
   **pass**: `tests/integration/small_file_sync/test_single_part_size_boundary.py`
   (exactly-at-ceiling accepted, one byte over rejected before reservation)
   and `test_wire_journey.py` exactly-once publication.
5. Server verifies bytes before canonical publication and response loss
   exactly replays the original outcome — **pass**:
   `tests/unit/small_file_sync/test_service.py` (verified-receipt-only
   publication, content-mismatch non-publication, concurrent receive) and
   the dropped-response replay scenarios of the integration suite
   (`committed_replay`, frozen result, exactly one publication).
6. Offline/auth/policy/size/conflict/lifecycle outcomes preserve Vault data
   and follow their closed journal state — **pass**:
   `test_policy_and_device_boundaries.py` (denied policy mid-upload,
   revoked device), journey suite (offline retry, stale base
   `blocked_conflict`, vanished file `deferred_lifecycle`, changed-bytes
   `integrity_failed`), `reconcile_required` and recovery scenarios; the
   scenario table in the operations guide is the operator mirror.
7. No sensitive value appears in generated output, logs, test reports or
   sanitized device evidence — **pass**: plugin status/diagnostic surfaces
   are closed labels only (status + journey suites), the server side is
   pinned by the leakage contract suites (amended in Task 11 to cover the
   sync surface), the wire-golden corpus is replayed by both languages, and
   the device-evidence procedure (operations guide) records sanitized
   labels and timestamps only.
8. All focused, migration, integration, API snapshot/generated-client,
   plugin build/lint/type and reference-device gates pass from one commit —
   **pass for every automated sub-gate** (§2 table, one commit);
   **deferred for the reference-device sub-gate** (§5) — not marked passed.

## 4. Spec interpretation decisions (with reasons)

1. **Created `SourceTitle` derived from media type (Task 7).** The spec wire
   carries no title or path (privacy), yet canonical publication requires a
   title and titles are create-immutable. The server therefore derives a
   neutral title from the declared media type (e.g. "Markdown file"). Safe
   assumption inside the documented contract; a child owning title mutation
   revisits it. Adjacent ruling: `derive_source_type` folds arbitrary binary
   media into TEXT — accepted with the title decision; revisit in the same
   future mutation work.
2. **Production serve binding added in scope (Task 8).** Spec section 5 puts
   the API in the serving path, so the serve graph composes the real
   small-file sync runtime; `r2-object-storage` became the first production
   composition of the r2 package (dependency pins and gate amendments
   sanctioned for `server.py` and `small_file_sync_composition.py` only).
   Metrics sinks in that graph remain bounded in-memory placeholders until a
   real sink lands (§6).
3. **Publication-time 403 surfaces as Login required, then self-heals
   (Task 11).** A policy revision published between an accepted preflight
   and the content stream fails closed at the publication guard (403,
   nothing published). The plugin's closed spec-12 failure table maps every
   403 to `login_required`, so that pass first ends **Login required** with
   the whole queue retained — a transient status, not a credential failure;
   the next preflight re-evaluates policy and settles the event terminally
   as `excluded_policy`. Pinned as actual behavior and documented in the
   operations guide (policy block section + scenario table); no re-login is
   ever needed.
4. **`operation_retry_required` retries under the frozen `server_error`
   label (Task 9).** The closed client vocabulary has no dedicated label for
   a retryable server-side operation-state answer, so it travels as
   `server_error` (bounded backoff, same idempotency identity) and the pass
   ends after the first retryable failure — foreground passes never loop.
5. **Boundary-scanner sanctioned-surface amendment (Task 12, this session).**
   The first full `tests/unit tests/contract` run failed
   `test_generated_typescript_clients_declare_no_source_publication_endpoint`:
   the plugin journal files legitimately contain `source_version`/
   `sourceVersion` (the hand-mirrored small-file sync receipt fields) and
   `publication` (journal-manifest and fake-server vocabulary), but the
   scanner's sanctioned-surface map — amended by Task 8 for the server-side
   `small_file_sync*` files with the ruling that the sync surface
   "legitimately names canonical source and version identity" — did not
   enumerate the plugin-side client directory that Tasks 9–10 added (their
   scoped verifies never ran this contract file). No endpoint path, no
   `/sources`, no source-publication endpoint declaration exists in the
   journal sources. Amendment: `apps/obsidian-plugin/src/journal` joined the
   sanctioned surfaces in `tests/contract/source_publication/test_no_public_api.py`
   (exactly that directory — a tightness probe confirmed every non-journal
   path, including a hypothetical web `journal` directory, still scans);
   the OpenAPI document scan keeps full strength. Full suite then green.

## 5. Deferred item — reference-device evidence (operator-owned, BACKLOG-indexed)

Desktop AND Mobile Obsidian verification of the scenario table on physical
devices is operator-observed work an agent cannot perform; the sanitized
recording procedure exists (operations guide, "Operator evidence procedure"),
and the automated half is proven by the task-11 suites. Until the operator
records the rows in
`docs/operations/exclusion-policy-device-verification.md` (child-4 section),
the child's device evidence stays **PENDING**; the automated acceptance
checklist item is NOT passed for this sub-gate and nothing was fabricated.
Indexed as one line in `docs/handoff/BACKLOG.md`.

## 6. Other accepted deferred rulings (handoff-only; one-line dispositions)

None blocks the contract; none received a BACKLOG line (task-12 instruction
fixed the BACKLOG to exactly the §5 item, following the exclusion-policy
handoff §6 precedent).

- Task 1: esbuild marker regex spoofable by plugin-authored `// sql.js/...`
  comment (anchor to node_modules paths); segment derivation duplicated
  between probe and strip helpers; sql.js exports-map parsing assumes dict
  shape (degrades to main fallback).
- Task 2: older `user_version` maps to `journal_image_invalid` instead of a
  migration path (matters on a future schema bump); exported image
  self-reports `lastVerifiedGeneration` n-1 (reorder or comment); orphaned
  generations after chain-reset rebuild never swept (bounded residue);
  `open()` not concurrent-safe (documented await-open-before-commit
  contract); `open()` failure paths can leak the engine; `@types/sql.js`
  caret pin vs repo exact-pin convention; transient probe error in
  `#openVerifiedGeneration` swallowed (fail-open probe — the readBinary
  variant is fail-closed and sanctioned).
- Task 3: row parsers validate state/outcome but not safeError label or
  fingerprint shape; SQLite FK references inert under sql.js (pragma off —
  documented); `readEvent`/`readEventsByLocalFileId` interpolate IDs without
  shape validation (escaped, asymmetric with the fail-closed path lookup);
  ISO-BMFF ftyp sniffs video/mp4 for any mov/heic/avif and pure-ASCII RIFF
  sniffs text/plain.
- Task 4: `#admitNormalizedPath` lacks `#isDisposed` early return
  (disposal race is fail-closed, not airtight); removed `.vault` from the
  forbidden list wholesale in one test (add negative pins); untracked-path
  delete/rename adds the path to the session guard set (later legitimate
  create suppressed — undocumentd edge); build does not ship
  `sql-wasm.wasm` to `dist/` by itself (Task 11's build script owns
  shipping; the final bundle does contain it).
- Task 5: preflight-invalid reason vocabulary conventional, not enforced;
  token grammar admits unhyphenated 32-hex UUID text; `SmallFileRejectionReason`
  duplicates code strings without a cross-equality test; sentinel test does
  not render the traceback form of a poisoned cause.
- Task 6: stale-token window after concurrent preflight surfaces NOT_FOUND;
  `expires_at > created_at` CHECK couples app clock to DB clock (bounded
  900 s); 16 MiB ceiling duplicated in migration `_MAXIMUM_DECLARED_SIZE_BYTES`
  vs domain constant (no pin test); diagnostic_context dropped by the store
  adapter; integration harness reaches into `preflight_harness._engine`.
- Task 7: terminal write is a second transaction after the publication
  commit (crash window answers NOT_FOUND and self-heals via replay; task-6
  seam exists to consolidate); locator policy not re-evaluated at receive
  (bounded by expiry — residual window); availability failures counted as
  REJECTED in the upload metric; one-time flake reported in
  `test_outcome_evidence_never_contains_locators_or_operands` (untouched
  area; did not recur in the final runs).
- Task 8: metrics sinks in the serve graph are bounded in-memory
  placeholders; `object-storage-check-runtime --service api` exits 78
  locally without R2 env (safe config event).
- Task 9: local failures (vault reader throw, missing `local_files` row)
  labeled retryable `server_error` (vocabulary-constrained conflation);
  deadline clamp issues a ~1 ms doomed request instead of skipping the
  network action; `resolveOrigin() ?? ""` yields a relative URL on
  unparseable settings; `coalescableStateList` misnames the pending/eligible
  list and deadline-truncated passes report "completed" without a marker.
- Task 10: `#runBoundedQueuePass` pass-already-running guard unpinned;
  catch fallback fabricates outcome "completed"; `NETWORK_RETRY_SAFE_ERRORS`
  typed `ReadonlySet<string>` not a closed union; unload
  `attemptFinalFlush()` result discarded; forbidden-label pin is a narrow
  substring tripwire.
- Task 11: "child-3 precedent" phrase resolves only via doc structure;
  TS-replay source-mention assertion weak alone (backed by hash pin +
  executed replay).

## 7. Next actions

1. **Operator: record the Desktop and Mobile reference-device evidence
   rows** per the operations-guide procedure into
   `docs/operations/exclusion-policy-device-verification.md` (child-4
   section), then remove the BACKLOG line. Only then is this child fully
   closed.
2. Child 5/6/7 owners consume the seams this child froze: stable-ID
   persistence, durable cursors/reconcile (the `reconcile_required` repair
   action is deliberately theirs), rename/move/delete/restore lifecycle,
   remote apply and conflict resolution. The Task 7 title/TEXT-derivation
   rulings (§4.1) belong to the first mutation-owning child.
3. Pick up §6 minors opportunistically in the owning files; the two with
   contract flavor are the `@types/sql.js` pin convention and a future
   `concurrency_busy`-style label split for retryable server outcomes
   (needs a spec amendment, not a silent rename).
4. A real metrics sink should replace the in-memory serve-graph placeholders
   before production alerting relies on them (§6, Task 8).
