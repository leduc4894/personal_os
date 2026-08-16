# Web Authentication and Device Authorization Handoff

**Date:** 2026-08-16
**Plan:** `docs/superpowers/plans/2026-08-16-web-auth-and-device-authorization.md`
**Spec:** `docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md`
**Parent:** `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md` (child 2)
**Branch:** `web-auth-device-authorization` (base `17cdf4a`, Task 1–14 commits through `d8f3405`)
**Final plan commit:** the single Task 15 acceptance commit
`test: prove authentication acceptance` (parent `d8f3405`); its SHA is recorded in
the plan report `.superpowers/sdd/2026-08-16-web-auth-and-device-authorization/task-15-report.md`.

Living operational status: `docs/operations/web-authentication-and-device-authorization.md`
(enrollment/reset commands, keyring rotation, proxy contract, device revoke and
recovery, Argon benchmark evidence, backup implications, safe metrics, incident
runbook). Canonical status: `docs/20-IMPLEMENTATION_PLAN.md` (Phase 2, child 2
complete; deliverables 2–7 — Vault synchronization — belong to later children).

## What was built (plan scope, Tasks 1–15)

Framework-neutral authentication domain (`src/personal_os/authentication/`),
PostgreSQL adapters (`packages/postgresql-source-store/`), the serve/offline
composition and closed HTTP route set (`apps/api/src/api_runtime/`), the
protected credential CLI, Web Admin (login, security, devices, approval), the
Obsidian plugin SecretStorage onboarding, the extended OpenAPI snapshot and
generated client, and this final acceptance layer:

- `tests/contract/api/test_authentication_leakage.py` — the spec 24.7 sentinel
  scan: one full credential journey over the offline composed application with
  per-surface exemptions, diagnostics and offline-state scans, plus static
  scans of the OpenAPI document, generated client, blocklist artifact and the
  real production Web/plugin bundles (built by the gate).
- `tests/contract/api/test_authentication_headers.py` — the spec 16 header
  matrix: `Cache-Control: no-store` on every authentication route outcome and
  `Pragma: no-cache` on exactly the seven provisioning/recovery surfaces,
  across a 27-step success-and-rejection journey.
- `tests/integration/authentication/test_authentication_key_rotation.py` —
  two-key keyring: previous-key TOTP re-encryption, `required_key_ids`
  coverage over TOTP/refresh/grant references, the spec 20.1 startup refusal
  with the fixed safe reason, and device derivations surviving a current-key
  transition.
- `tests/integration/authentication/test_emergency_reset_races.py` — reset
  racing concurrent logins, refresh rotation and grant approval on the real
  stack: no interleaving leaves a usable credential or an approved grant.
- `tests/integration/authentication/test_ambiguous_auth_commits.py` — lost
  exchange/refresh acknowledgements replay byte-identically without duplicate
  rows; a genuinely new rotation identity resolves as confirmed reuse.
- `tests/end_to_end/authentication/full-device-onboarding.spec.ts` — the
  browser-side full journey (fragment consume → inline login → context review
  → re-auth gate → approve/deny → Admin list) with contract-fidelity request
  capture.
- CI gates `authentication-test` and `authentication-e2e` (Poe tasks in
  `pyproject.toml`, `test:e2e:authentication` script in `package.json`; the
  browser-e2e job lives in `.github/workflows/quality.yml` and the
  Docker-stack job in `.github/workflows/authentication-acceptance.yml` —
  the committed CI-security contract forbids stack references in
  quality.yml, so the stack gate follows the canonical-baseline workflow
  pattern, and the prefetch contract in
  `tests/contract/test_ci_security.py` now also covers it; both gates fail,
  never skip).
- `docs/operations/web-authentication-and-device-authorization.md` and the
  Phase 2 status update in `docs/20-IMPLEMENTATION_PLAN.md`.
- Pre-existing format drift fixed in
  `tests/contract/api/test_plugin_authentication_bundle.py` (caught by the
  final `format-check`; ruff-only rewrap, zero behavior change).

## Gate status (final gate, run 2026-08-16 in order from a clean tree)

Worktree content at gate time: the Task 15 files above only. Alembic gates ran
against a disposable `knowledge-ci-t15-gate-*` PostgreSQL 18.4 stack (unique
project, sanitized env, reset afterwards asserting zero labelled resources).

| # | Command | Result |
| --- | --- | --- |
| 1 | `uv run poe format-check` | exit 0 (after fixing the pre-existing plugin-bundle-test drift) |
| 2 | `uv run poe lint` | exit 0 |
| 3 | `uv run poe type-check` | exit 0 |
| 4 | `uv run poe import-boundaries` | exit 0 — `Contracts: 5 kept, 0 broken.` |
| 5 | `uv run poe api-contract-check` | exit 0 — snapshot + `openapi-typescript 7.13.0 --check` |
| 6 | `uv run pytest tests/unit/authentication tests/unit/api_runtime tests/unit/postgresql_source_store tests/contract/api tests/integration/authentication -q` | exit 0 — `792 passed, 81 deselected` (default marker deselects `local_stack`; the stack suite is gates 6b/CI) |
| 6b | `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-t15-focus-* uv run pytest tests/contract/api/test_authentication_leakage.py tests/integration/authentication -m local_stack -q` | exit 0 — `81 passed, 4 deselected` (12 new integration tests included) |
| 7 | `pnpm run test` | exit 0 (workspace vitest suites green) |
| 8 | `pnpm run build` | exit 0 |
| 9 | `pnpm exec playwright test tests/end_to_end/authentication` | exit 0 — `10 passed` |
| 10 | `uv run alembic upgrade head` | exit 0 — `-> 20260813_01 -> 20260816_01` |
| 11 | `uv run alembic -x allow_destructive=true downgrade 20260813_01` | exit 0 — `20260816_01 -> 20260813_01` |
| 12 | `uv run alembic upgrade head` (again) | exit 0 — `20260813_01 -> 20260816_01` |
| 13 | `git diff --check` | exit 0 |
| 14 | `git status --short` | exit 0 — only the intended Task 15 files |

Generated artifacts unchanged by every gate (no `packages/api-client` diff).
Focused acceptance runs during development: leak/headers contract files green
standalone; E2E spec green standalone (2 passed; whole directory 10 passed).

## Spec interpretation decisions and amendments

1. **SecLists blocklist pin (plan/spec amendment).** Release 2026.1 no longer
   ships `Passwords/Common-Credentials/10-million-password-list-top-10000.txt`.
   The blocklist is pinned to **SecLists release 2025.2**, exact path
   `Passwords/Common-Credentials/10-million-password-list-top-10000.txt`,
   10000 verified source lines, source SHA-256 `0279e0e7…`, 9913 digests after
   case-variant collapse. Plan Task 2 text and the spec data-artifact note
   should be amended to name release 2025.2 (this handoff is the amendment
   record; the artifact's provenance JSON in
   `src/personal_os/authentication/data/` already carries the exact source).
2. **17th error code ratified (spec 17 amended in Task 10).**
   `device_revocation_confirmation_invalid` (409, not retryable, no detail) was
   adjudicated and RATIFIED during Task 10's review; spec 17 carries the row
   and the 14.1 note. The registry therefore holds 17 authentication-scope
   codes, not 16 — later tasks and this acceptance layer treat the 17-row
   table as canonical.
3. **In-memory poll pacer (plan-vs-schema conflict, Task 9 adjudication).**
   Plan Task-8 text ("poll rate state remains in PostgreSQL throttle buckets")
   conflicts with the Task-3 closed 6-value `bucket_kind` schema. Ruling: the
   in-memory `GrantPollPacer` stands (spec 11.4 mandates no durable pacing;
   single-process serve). **Multi-worker deployments are out of scope until a
   poll bucket kind is added to the closed schema set (schema + spec
   amendment) or a shared pacing store is introduced** — recorded in the
   operations runbook's reverse-proxy section and indexed in BACKLOG §13.
4. **Argon2id benchmark deviation.** Real 20-run sequential benchmark with the
   pinned parameters (memory_cost_kib=65536, time_cost=3, parallelism=1,
   salt 16B, hash 32B) on the development host (Windows 11 10.0.26200, AMD64,
   CPython 3.14.6, argon2-cffi 25.1.0): min 113.3 / median 117.7 / mean 119.4 /
   p95 127.1 / max 134.9 ms. The host sits **below** the reviewed 150–750 ms
   band floor (faster hardware, not a violation). The band rule stands and is
   recorded in the runbook: the smallest deployment host must be benchmarked
   at install time; only a p95 above 750 ms blocks serving logins.
5. **Acceptance-test scope decisions (Task 15).** The leak gate pins the full
   credential-shape regex (`pg1|at1|rt1` + UUID lookup id) rather than bare
   prefixes in bundle artifacts — bare `rt1.` false-positives on minified
   vendor text (`port1.onmessage`); the plugin source-scan gate keeps the
   stricter bare-prefix pin where it is exact. Cookie secrets are scanned as
   Set-Cookie-only sentinels; the user code and polling secret are allowed in
   exactly the creation payload (plus lookup context for the user code and
   device name), matching spec 16's one-time-rendering contract.

## Deferred items and rulings

Each genuinely deferred item below has exactly one BACKLOG line. Ruled
**not deferred** (polish or deliberate, summarized; no backlog line): the
Task 1 env-name mirror-pinning test and the openapi-typescript peer warning
(the latter already indexed by the 2026-08-15 api-contract handoff §1 — not
duplicated); Task 3 byte-pin sibling checks and the conftest teardown misnomer;
Task 4 pre-raise work ordering and the stdlib-hmac parallel idea; Task 5
zero-count ordering (where reproduced in Task 15's key-rotation file the
ordering dependency is documented in-line); Task 10 revoke-current docstring
overclaim, the unvalidated `cast`, the ~330-line offline hand-mirror (behavior
pinned by tests) and the dual terminal-code presentation on revoked families
(both pinned); Task 11 deliberate module-private test import and the
per-route forwarded-bucket duplication (login route pins the pattern).

| § | Deferred item | Ruling |
| --- | --- | --- |
| 1 | `@types/qrcode-generator@1.0.6` deprecated (upstream ships own types); plan-mandated pin | Keep pin now (plan constraint); drop the dev pin when qrcode-generator's own types cover the renderer — a future session must action this on the next dependency wave |
| 2 | `derive_subkey` accepts any ASCII label (Task 2) | Add membership check against `CRYPTO_DOMAIN_LABELS` when the crypto adapter is next touched; real hardening, small |
| 3 | Blocklist `from_digest_text` hex parsing looser than the artifact regex (Task 2) | Tighten to the artifact's exact grammar when the loader is next touched |
| 4 | Lockout transition not distinct in audit — same `login_rejected` row as other rejections (Task 4) | Decide a dedicated reason token when the audit surface is next opened; operators currently distinguish via throttle state |
| 5 | Reset CLI edges: prompt echoes typed username-confirmation, EOFError maps to `internal_error`, untested reset-on-unenrolled and status-of-archived-workspace (Task 5) | Add the CLI edge tests and the typed EOF mapping when the CLI is next touched |
| 6 | API hygiene batch (Task 6): Uvicorn raw traceback on lifespan `ConfigurationError` bypasses structured diagnostics; `verify_keyring_covers_required_key_ids` reads the API-host clock, not the DB clock; offline store mirrors the username bucket onto the source bucket; malformed-JSON 400 `no-store` unpinned | Batch-fix when `server.py`/composition is next touched; pin the 400 `no-store` in the same pass |
| 7 | Throttle-bucket first-insert unique race (Task 7, inherited store convention), 429 second clock read, double `KeyringTotpSecretCodec` instances | First-insert race is a real (low-rate) concurrent-window defect: fix with an upsert or advisory pattern when the store is next touched; the rest ride along |
| 8 | Grant-path hardening batch (Task 8): cold-source creation lock-check/insert not one transaction; live-grant-cap rejection records no throttle attempt; byte%31 modulo bias in user-code generation; dead `session_policy` attribute; terminal-rejection docstring overstates expiry-wins | One transaction for check+insert when the grant store is next touched; the rest are small aligned fixes |
| 9 | Poll replay digest is single-key — a keyring rotation mid-grant breaks the pending/exchanged poll replay; slow-down hint under-reports after back-off; unknown polling credentials unthrottled; pacer counts only pending polls (Task 9) | Multi-key digest map when the exchange store is next touched; documented limitation until then |
| 10 | Web auth-state hygiene batch (Task 12): recovery-continue path keeps the held password in component state; duplicate Current-password fields in re-auth mode; orphaned bootstrap-copy module; `skip()` swallows dismissal failure; no-op unmount cleanup; unused `x-csp-nonce` request header | Clear the held password and drop the dead module/field when the security panel is next touched |
| 11 | Web a11y/UX batch (Task 13): revoke dialog lacks a focus trap; approval re-auth has no abandon path; rate-limited user-code lookup is terminal without retry affordance; `replaceState` drops the query string; `unwrapEnvelope` duplicated in `device-administration-client.ts` | Focus trap + abandon path first (spec 24.5 a11y scope), the rest ride along |
| 12 | Plugin hygiene batch (Task 14): rate-limited grant creation renders an offline label; offline dead-end without restart; error-as casts; dead `DEVICE_AUTH_ERROR_CODES`/`LOCAL_ERROR_CODES` exports; `normalizeSettings` rewrites record names; `reconcileCrashWindow` uncaught `saveData` rejection; `login()` over existing active record overwrites the credential (mitigated by `canLogin` gate) | Catch the crash-window `saveData` rejection and stop overwriting an active record when the plugin session module is next touched; the rest are label/cleanup fixes |
| 13 | Multi-worker poll pacing: in-memory `GrantPollPacer` is single-process only (Task 9 adjudication, decision 3 above) | Before any multi-worker `serve` deployment: add a poll `bucket_kind` to the closed schema set (schema + spec amendment) or introduce a shared pacing store |

## Next actions

1. Merge the branch per the plan's finishing flow (controller-owned).
2. Amendment follow-ups from decisions 1–3 (SecLists release note in plan
   text; poll-bucket schema+spec amendment when scaling beyond one worker).
3. Deferred batch §6–§12 when the respective modules are next touched.
4. CI: the new jobs (`authentication-test` in authentication-acceptance.yml,
   `authentication-e2e` in quality.yml) run on the next push; first CI run is
   their live validation (locally both gates passed with the same
   invocations).
