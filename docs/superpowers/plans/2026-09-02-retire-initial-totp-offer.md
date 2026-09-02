# Retire Initial TOTP Offer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the obsolete initial TOTP offer/dismissal contract while preserving Security opt-in enrollment and mandatory recovery replacement.

**Architecture:** This is a subtractive API/schema change. `start` remains the sole enrollment action; Web Admin presents the offer only from Security or recovery replacement. The database migration drops the dismissal timestamp and rebuilds its timestamp check constraint without that column.

**Tech Stack:** Python 3.14, FastAPI/Pydantic, SQLAlchemy/Alembic, PostgreSQL, Next.js/React TypeScript strict, Vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-retire-initial-totp-offer-spec.md`

## Global Constraints

- Do not reinstate post-password TOTP prompting or a Skip button.
- Keep recovery replacement mandatory and Security enrollment explicit.
- Preserve Origin/CSRF, no-store, redaction, secret-in-memory and generated-client checks.
- The migration must support upgrade and downgrade; it may never copy or log a TOTP secret.
- Remove the deferred BACKLOG row only after the implementation and final gate succeed.

---

### Task 1: Remove the Web dismissal caller and Skip UI

**Files:**
- Modify: `apps/web/src/api/authentication-client.ts:20-40,170-186`
- Modify: `apps/web/src/api/authentication-client.test.ts:247-266`
- Modify: `apps/web/src/testing/api-mock-builders.ts:57-61`
- Modify: `apps/web/src/features/authentication/TotpChallenge.tsx:170-297`
- Modify: `apps/web/src/features/authentication/TotpChallenge.test.tsx:178-296`
- Modify: `apps/web/src/features/authentication/LoginForm.tsx:173-179`
- Modify: `apps/web/src/features/authentication/LoginForm.test.tsx:78-98`

**Interfaces:**
- Consumes: `AuthenticationClient.startTotpEnrollment()` and
  `TotpEnrollmentOfferProps`.
- Produces: `TotpEnrollmentOffer` with `client`, `enrollment`,
  `requireCompletion`, and `onCompleted` only; no client dismissal method.

- [ ] **Step 1: Write failing UI tests**

  Replace the standalone Skip success/failure tests with a test that renders
  an ordinary Security-style offer (`requireCompletion` omitted) and asserts:

  ```ts
  expect(screen.queryByRole("button", { name: "Skip for now" })).not.toBeInTheDocument();
  ```

  Retain the recovery case asserting the same absence. Remove
  `dismissInitialTotpOffer` from the LoginForm spy client so TypeScript fails
  until the client interface is reduced.

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  pnpm --filter @workspace/web-runtime test -- TotpChallenge.test.tsx LoginForm.test.tsx authentication-client.test.ts
  ```

  Expected: FAIL because Skip remains rendered/callable and the client still
  declares dismissal.

- [ ] **Step 3: Delete only the obsolete client/UI path**

  Delete `dismissInitialTotpOffer`, the dismissed mock response, `onSkipped`,
  `skip()`, its error copy, and the conditional Skip button. Pass no
  `onSkipped={undefined}` from LoginForm. Do not change QR rendering,
  activation, recovery-code display, or `requireCompletion`.

- [ ] **Step 4: Verify Web behavior**

  Run:

  ```powershell
  pnpm --filter @workspace/web-runtime test -- TotpChallenge.test.tsx LoginForm.test.tsx authentication-client.test.ts
  pnpm --filter @workspace/web-runtime type-check
  ```

  Expected: pass; Security still starts enrollment only when entered and
  recovery replacement offers no bypass.

- [ ] **Step 5: Commit**

  ```powershell
  git add apps/web/src/api/authentication-client.ts apps/web/src/api/authentication-client.test.ts apps/web/src/testing/api-mock-builders.ts apps/web/src/features/authentication/TotpChallenge.tsx apps/web/src/features/authentication/TotpChallenge.test.tsx apps/web/src/features/authentication/LoginForm.tsx apps/web/src/features/authentication/LoginForm.test.tsx
  git commit -m "refactor: remove obsolete totp skip UI"
  ```

### Task 2: Reduce the domain and HTTP enrollment contract

**Files:**
- Modify: `src/personal_os/authentication/totp.py:108-112,448-453,551-556,733-783`
- Modify: `apps/api/src/api_runtime/totp_routes.py:220-251`
- Modify: `apps/api/src/api_runtime/authentication_models.py:183-194`
- Modify: `apps/api/src/api_runtime/authentication_composition.py:719,1091-1097`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/totp_store.py:195-214`
- Modify: `tests/unit/authentication/test_totp.py`
- Modify: `tests/unit/api_runtime/test_totp_routes.py:356-374`
- Modify: `tests/unit/postgresql_source_store/test_totp_store.py:354-368`

**Interfaces:**
- Consumes: `POST /api/auth/totp/enrollments` with body `{"action":"start"}`.
- Produces: `TotpEnrollmentAction.START` and `TotpEnrollmentActionOutcome(public_error, started)`; the transaction port has no `record_prompt_dismissal` method.

- [ ] **Step 1: Write failing contract tests**

  Change the closed vocabulary assertion to:

  ```python
  assert {action.value for action in TotpEnrollmentAction} == {"start"}
  ```

  Replace the route dismissal-success test with a request containing
  `{"action": "dismiss_initial_offer"}` that asserts status `422`,
  `api_request_validation_failed`, no-store headers, and no new TOTP rows.
  Delete the store unit test that expects a timestamp update.

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  uv run pytest tests/unit/authentication/test_totp.py tests/unit/api_runtime/test_totp_routes.py tests/unit/postgresql_source_store/test_totp_store.py -q
  ```

  Expected: FAIL because the enum and route still accept dismissal.

- [ ] **Step 3: Implement the subtractive domain change**

  Remove `DISMISS_INITIAL_OFFER`, `record_prompt_dismissal`,
  `dismissed_at`, the service branch, offline-state field/method, PostgreSQL
  update, and `TotpEnrollmentData.dismissed_at`. Make the route serialize
  only a successful `start` result. Retain all current recent-auth and
  recovery-limited checks for `start`.

- [ ] **Step 4: Regenerate and verify API artifacts**

  Run:

  ```powershell
  uv run pytest tests/unit/authentication/test_totp.py tests/unit/api_runtime/test_totp_routes.py tests/unit/postgresql_source_store/test_totp_store.py -q
  uv run python tools/api_contract_artifacts.py generate
  pnpm --filter @workspace/api-client run generate:check
  ```

  Expected: tests pass; generated schema contains only `"start"` and no
  `dismissed_at`/`dismiss_initial_offer` token.

- [ ] **Step 5: Commit**

  ```powershell
  git add src/personal_os/authentication/totp.py apps/api/src/api_runtime/totp_routes.py apps/api/src/api_runtime/authentication_models.py apps/api/src/api_runtime/authentication_composition.py packages/postgresql-source-store/src/postgresql_source_store/totp_store.py tests/unit/authentication/test_totp.py tests/unit/api_runtime/test_totp_routes.py tests/unit/postgresql_source_store/test_totp_store.py packages/api-client/openapi.json packages/api-client/src/generated/schema.ts
  git commit -m "refactor: retire initial totp dismissal contract"
  ```

### Task 3: Remove the obsolete database state

**Files:**
- Create: `migrations/versions/20260902_02_drop_totp_prompt_dismissal.py`
- Create: `tests/unit/migrations/test_drop_totp_prompt_dismissal_migration.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/tables.py:214-221`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/authentication_credentials.py:843-849`
- Modify: `tests/integration/authentication/test_authentication_migration.py:707-712`

**Interfaces:**
- Consumes: `knowledge.user_credentials` at revision `20260902_01` with
  `ck_user_credentials__timestamps` and nullable
  `totp_prompt_dismissed_at`.
- Produces: the same table without that column; timestamp constraint is
  `updated_at >= created_at AND password_changed_at >= created_at`.

- [ ] **Step 1: Write failing upgrade/downgrade migration tests**

  Copy the repository's migration-test harness pattern. Assert upgrade drops
  `totp_prompt_dismissed_at` and recreates
  `ck_user_credentials__timestamps` without its old clause. Assert downgrade
  restores nullable `TIMESTAMP WITH TIME ZONE` column and the exact old
  constraint clause, permitting a null restored value.

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  uv run pytest tests/unit/migrations/test_drop_totp_prompt_dismissal_migration.py -q
  ```

  Expected: FAIL because revision `20260902_02` does not exist.

- [ ] **Step 3: Add reversible Alembic migration and align table metadata**

  In upgrade, drop `ck_user_credentials__timestamps`, drop
  `totp_prompt_dismissed_at`, then add the reduced named check constraint. In
  downgrade, reverse that exact order: drop the reduced constraint, add the
  nullable column, then recreate the old check. Remove the metadata column
  and the password-change reset assignment.

- [ ] **Step 4: Run migration and authentication regression gates**

  Run:

  ```powershell
  uv run pytest tests/unit/migrations/test_drop_totp_prompt_dismissal_migration.py tests/integration/authentication/test_authentication_migration.py -q
  uv run ruff check migrations packages/postgresql-source-store/src tests/unit/migrations tests/integration/authentication
  uv run mypy --strict packages/postgresql-source-store/src
  ```

  Expected: upgrade/downgrade and strict checks pass.

- [ ] **Step 5: Commit**

  ```powershell
  git add migrations/versions/20260902_02_drop_totp_prompt_dismissal.py tests/unit/migrations/test_drop_totp_prompt_dismissal_migration.py packages/postgresql-source-store/src/postgresql_source_store/tables.py packages/postgresql-source-store/src/postgresql_source_store/authentication_credentials.py tests/integration/authentication/test_authentication_migration.py
  git commit -m "refactor: drop obsolete totp prompt state"
  ```

### Task 4: Update canonical docs and close the backlog item

**Files:**
- Modify: `docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md:391-395,713-716,875-877`
- Modify: `docs/handoff/BACKLOG.md`
- Create: `docs/handoff/2026-09-02-retire-initial-totp-offer.md`

- [ ] **Step 1: Update the canonical auth design**

  Replace the two-action/dismissal description with `start` only, remove the
  credential-table timestamp line, and update the route-list action comment.
  State explicitly that optional enrollment begins from Security, while
  recovery replacement remains mandatory.

- [ ] **Step 2: Run documentation/API consistency checks**

  Run:

  ```powershell
  rg -n "dismiss_initial_offer|dismissInitialTotpOffer|totp_prompt_dismissed_at|Skip for now" apps src packages migrations tests docs --glob '!docs/handoff/*'
  uv run python tools/api_contract_artifacts.py check
  ```

  Expected: the search has no source or canonical-doc matches; generated API
  artifacts are current.

- [ ] **Step 3: Run final verification and record closure**

  Run:

  ```powershell
  uv run poe verify
  git diff --check
  git status --short
  ```

  Delete only the `2026-08-31 | web-admin (pre-existing) | Dead
  dismissInitialTotpOffer path ...` BACKLOG row. Write one handoff with final
  SHA, migration/API/Web evidence, the opt-in decision, and no sensitive data.

- [ ] **Step 4: Commit closure**

  ```powershell
  git add docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md docs/handoff/BACKLOG.md docs/handoff/2026-09-02-retire-initial-totp-offer.md
  git commit -m "docs: close initial totp offer backlog"
  ```
