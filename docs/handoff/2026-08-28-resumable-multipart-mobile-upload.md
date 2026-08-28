# Handoff — Resumable Multipart Mobile Upload (Child 7, mid-plan stop after Task 5)

- **Date:** 2026-08-28
- **Branch:** `resumable-multipart-mobile-upload` (from `master` `7fd6137`)
- **Final commit of this session:** `aba6c56` (feat: orchestrate multipart verification and promotion)
- **Plan:** `docs/superpowers/plans/2026-08-28-resumable-multipart-mobile-upload.md` (14 tasks)
- **Spec:** `docs/superpowers/specs/2026-08-28-resumable-multipart-mobile-upload-design.md`
- **Stop instruction:** user directed the session to stop once Task 5 completed. Tasks 6–14 are **NOT started**; nothing below about them is evidence — they are next actions only.
- **SDD workspace (ledger, briefs, reports, review diffs):** `.superpowers/sdd/2026-08-28-resumable-multipart-mobile-upload/` — git-ignored scratch; the ledger `progress.md` is the authoritative per-task record. Resume the SDD loop there (do not re-dispatch Tasks 1–5).

## Gate status (all offline gates; evidence below)

| Gate | Command | Result |
| --- | --- | --- |
| Multipart + small-file unit/contract | `uv run pytest tests/unit/multipart_upload tests/unit/postgresql_source_store/test_multipart_upload_store.py tests/contract/multipart_upload tests/unit/small_file_sync -q` | 270 passed (run 2026-08-28 on `aba6c56`) |
| Object-storage + composition boundaries | `uv run pytest tests/contract/object_storage tests/contract/canonical_core/test_composition_boundaries.py -q` | 117 passed |
| Strict typing | `uv run mypy src/personal_os/multipart_upload packages/postgresql-source-store/src packages/r2-object-storage/src` | Success, 44 files |
| Tree hygiene | `git status --porcelain`; `git diff --check` | clean / clean |

Per-task evidence (TDD RED→GREEN, live-stack runs, full regressions) lives in the SDD workspace reports `task-{1..5}-report.md`. Live-stack integration for Task 3 ran green under `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-multipart-store` (53 passed) and the stack was torn down. The plan's live gates (Desktop WDIO, physical Mobile, `knowledge-ci-multipart-int`/`-final` integration rounds) are owned by Tasks 12–14 and have **not** run.

## Landed work (Tasks 1–5, each task-review clean)

1. `63d0e8a` — domain contract: geometry (8 MiB part, ≤13 parts, 16–100 MiB routing, 24 h expiry, 10 min URL), 12-state machine, **twelve** `MULTIPART_*` codes (brief's "eleven" was a miscount; spec §7 governs), `MULTIPART_UPLOAD` preflight outcome, OpenAPI/generated-client regenerated (outcome is wire-visible).
2. `32e9dc3` — schema: `multipart_uploads` + `multipart_parts` (migration `20260828_01`); **lifetime UNIQUE on `operation_id`** (spec §4.2/§5: one session per operation ever, replay returns the frozen result).
3. `0a263fb`/`59ba317`/`4c3b11e` — PostgreSQL store + fencing: `PostgreSqlMultipartUploadStore` (7 methods + `record_provider_identity`), FOR-UPDATE mutation, completion lease CAS, typed closed errors. Migration `20260828_02` widens the legacy 16 MiB operations CHECK to 100 MiB (live CheckViolation proved the old CHECK unsatisfiable with multipart FK rows). Migration `20260828_03` defers provider identity (nullable) to enable persist-before-create.
4. `7c4b0ad`/`42b2973` — R2 staging provider: exactly six methods, staging-key validation before every SDK call, kwargs-level scripted-S3 contract tests, closed error mapping. Fix round amended `test_corruption_capability_lives_only_in_test_harness` with a narrowly-scoped `multipart.py`-only exception and inlined `"delete_object"` (string assembly evasion was rejected in review); alembic pin set extended to `20260828_01..03` (+ pre-existing `20260827_01` gap).
5. `aba6c56` — orchestration service: `create_or_resume` (reserve → `create_upload` → `record_provider_identity`), `status` (ListParts reconciliation), `issue_part_url` (ownership/state/expiry/policy recheck), `complete` (claim → ListParts → Complete → bounded verification spool → `SmallFilePublicationGateway` → frozen terminal → inline exact staging delete), `abort`, `run_exact_cleanup`; closed low-cardinality metrics module.

## Spec-interpretation decisions (with rationale)

- **Twelve error codes, not eleven** — spec §7 lists twelve; brief miscounted. Spec governs (more-specific document).
- **Lifetime UNIQUE on `operation_id`** — spec §4.2 ("cleanup_pending is not permission to reuse") + §5 (exact replay must return the same single session) require exactly one session per operation ever; an active-only partial index would make replay ambiguous.
- **Persist-before-create** — spec §6.1 requires the session row durable before the R2 create call; Task 3's original port made this unimplementable (identity required at reserve) and was amended in fix round 1 (`4c3b11e`), including nullable identity via `20260828_03`. Divergent identity on replay → typed closed error; the *service* aborts its own fresh orphan (caller-side, not a session obligation). NULL-identity expiry = trivially successful cleanup.
- **Staging read via `CanonicalObjectStore`** — the provider deliberately has no staging-object read method; the verification spool streams through the existing bounded reader (plan binds it). Promotion never uses R2 copy/rename.
- **Harness-bug fixes in Task 5's staged tests** — the plan's Step-1 snippets contained 4 impossible-to-pass bugs (stale `session_id` comparison, guard swapped off the wired service, stale-base setup contradicting the create-time conflict test); fixed intent-preservingly, reviewer-verified.

## Deferred items (verdicts)

| # | Item | Verdict |
| --- | --- | --- |
| D1 | **Locator stand-in weakens §7 early blocking for UPDATE sessions**: policy rechecks at `issue_part_url`/completion-start use `_RECHECK_UPDATE_LOCATOR` placeholder because `BoundSmallFileOperation` drops update locators; a locator-keyed deny advance does NOT block new part URLs/completion start (it fabricates allow-evidence where absent-locator would be indeterminate→fail-closed). Publication still fails closed via `authorize_bound_publication`. | Real, structural; **must be resolved by Task 7** (bind the recheck guard with a locator-free subject that fails closed, or amend the spec documenting the approximation). BACKLOG row written. |
| D2 | Committed-session inline staging-delete failure surfaces only on the in-memory rejection ring; COMMITTED rows have no cleanup-state exits, so no durable retry exists (storage-only leak; staging keys cannot address canonical content). | Real; durable surface owned by Task 7 composition. BACKLOG row written. |
| D3 | Cosmetic minors parked across Tasks 1–5 (transition-table pin limits; `committed_replay` precedence untested; Plan/Status `part_count` looseness; https-prefix URL check; hasattr protocol pin; unrelated reformat in `test_device_sync_migration.py`; `any()` ordering pins; protocol-conformance pin absent; lease token taxonomy split; `literal_binds=False` tautology; client-manager lock race note; presign expiry skew (safe direction); `MultipartStagingKey` placement seam; composition-test multipart exemption breadth; status provider-state-invalid waits for 24 h sweep; lost-complete-response forces re-upload (safe); `staging/multipart/` prefix duplication; report count nits). | Triaged by per-task reviews as non-blocking. One aggregated BACKLOG row; final adjudication at the plan's Task 14 / whole-branch review. |

## Next actions

1. Resume SDD execution at **Task 6** (Temporal cleanup workflow) using the ledger at `.superpowers/sdd/2026-08-28-resumable-multipart-mobile-upload/progress.md`; briefs regenerate via the skill's `task-brief` script. Tasks 6–14 proceed unchanged from the plan.
2. Task 6 consumes `run_exact_cleanup` (`src/personal_os/multipart_upload/service.py`) and the Task 3 cleanup claims; NULL-identity claims are trivially successful.
3. Task 7 must: (a) move the wire size bound from 16 MiB to 100 MiB in `apps/api` (`small_file_sync_models.py` still caps at the old constant; safe_message golden-pinned); (b) resolve D1 (locator-free recheck subject or spec amendment); (c) provide the durable surface for D2; (d) implement `MultipartSessionEvidenceStore` / `MultipartStagingByteSource` / str-key adapter seam ports introduced by Task 5; (e) compose store+provider+service with the server lifecycle (R2 client closes exactly once).
4. Tasks 12–14 own live gates (`knowledge-ci-multipart-int`/`-final`, Desktop WDIO, physical Mobile) — per AGENTS.md the Mobile matrix may only be deferred with a verified BACKLOG row, never silently.
5. Operational watch item from Task 3: one `serve-live-ci up` run saw `postgres-provision` exit 0 without applying migrations (conftest applied Alembic itself). Not blocking; investigate before relying on the initializer for migration application.

## Living documents

- Operations runbook for this feature lands with plan Task 13 at `docs/operations/resumable-multipart-upload.md` (does not exist yet).
- Local live-stack runbook: `.local/RESTART.md`; CI bootstrap contract: `.local/serve-live-ci.sh`.
