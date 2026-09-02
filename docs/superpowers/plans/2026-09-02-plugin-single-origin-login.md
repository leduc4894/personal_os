# Plugin Single-Origin Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align plugin onboarding and live-test configuration on one public workspace origin so browser device login and `/api/*` sync work without a separate API hostname.

**Architecture:** The deployed API reads `KNOWLEDGE_AUTH_ALLOWED_ORIGIN` as the exact public workspace origin and already uses it to mint device-approval URLs. The plugin retains one `server_origin` setting, whose contract becomes that same public origin; no `web_origin`, URL derivation, API contract change, or hardcoded hostname is introduced. Live tooling writes the public origin to both browser and plugin inputs while retaining a loopback endpoint only for server-side fixture/evidence work.

**Tech Stack:** Python 3.14, FastAPI authentication composition, TypeScript strict, Obsidian API, Vitest, pytest, Bash/PowerShell local tooling.

**Spec:** `docs/superpowers/specs/2026-09-02-plugin-single-origin-login-spec.md`

## Global Constraints

- `KNOWLEDGE_AUTH_ALLOWED_ORIGIN` remains the deployment-owned, exact public origin; do not add a production dependency or a plugin build-time environment variable.
- The Web Admin and `/api/*` must be reachable from that same origin; do not support or document a direct API hostname for plugin onboarding.
- Preserve exact Origin/CSRF enforcement, fragment-only user-code URLs, no-store responses, and the rule that credentials never enter plugin data, URL, logs, diagnostics, or test output.
- Keep API/OpenAPI/generated-client wire shapes unchanged; no new endpoint is required.
- Use only sanitized origins such as `https://workspace.example` in tests and documentation examples.

---

## File Map

| File | Responsibility |
|---|---|
| `apps/obsidian-plugin/src/authentication/contracts.test.ts` | Pin that native grant requests use the configured public workspace API base without forging a separate direct-API identity. |
| `apps/obsidian-plugin/src/authentication/device-authorization.test.ts` | Pin Login and Open browser again to the server-provided verification URL. |
| `apps/obsidian-plugin/src/authentication/contracts.ts` | Update the transport comment/implementation only as necessary to match the single-origin contract. |
| `apps/obsidian-plugin/src/authentication/settings-tab.ts` and its test | Make the settings label/help text say that Server origin is the public workspace origin serving Web Admin and `/api`. |
| `.local/RESTART.md`, `.local/serve-live-ci.sh` | State and check the one-origin tunnel topology. |
| `tools/obsidian_live_acceptance_bootstrap.py` and tests | Emit the same public origin for browser and plugin fixture variables. |
| `apps/obsidian-plugin/wdio.conf.mts` | Use the public workspace origin as the default fixture value. |
| `docs/operations/exclusion-policy-device-verification.md` | Replace contradictory two-hostname history/current guidance with the supported one-origin contract. |
| `docs/handoff/BACKLOG.md` and one new handoff | Remove the resolved deferred row and record gate evidence after implementation. |

### Task 1: Pin the single-origin plugin boundary

**Files:**
- Modify: `apps/obsidian-plugin/src/authentication/contracts.test.ts:282-309`
- Modify: `apps/obsidian-plugin/src/authentication/device-authorization.test.ts`
- Modify: `apps/obsidian-plugin/src/authentication/contracts.ts:232-273`

**Interfaces:**
- Consumes: `createDeviceApiTransport(http, resolveOrigin)` and `DeviceAuthorizationController`.
- Produces: a transport that targets `${server_origin}/api/auth/device-authorizations` without placing a different hostname in the `Origin` header; controller browser actions continue to consume `DeviceGrantWireData.verification_uri_complete`.

- [ ] **Step 1: Write failing plugin transport tests**

  Replace the current test that expects a configured `Origin` header with a
  test asserting the grant request has the configured public origin in `url`
  and no `origin` header:

  ```ts
  expect(calls).toEqual([
    expect.objectContaining({
      url: "https://workspace.example/api/auth/device-authorizations",
      headers: expect.not.objectContaining({ origin: expect.anything() }),
    }),
  ]);
  ```

  Add controller cases whose API fixture returns
  `verification_uri_complete: "https://workspace.example/device/approve#ABCD-EFGH"`
  while its configured API base is the same `https://workspace.example`.
  Assert `openUrl` receives that complete URL on Login and
  `verification_uri + "#" + user_code` on Open browser again.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run:

  ```powershell
  pnpm --filter @workspace/obsidian-plugin test -- contracts.test.ts device-authorization.test.ts
  ```

  Expected: FAIL because grant creation still injects
  `origin: resolveOrigin()`.

- [ ] **Step 3: Make the minimal transport change**

  In `createDeviceApiTransport().createGrant`, remove the manually supplied
  `origin` header and retain only the fixed JSON headers created by `post`.
  Update its comment to state that this is a native Obsidian request and that
  the browser security boundary begins at the server-minted verification URL.
  Do not alter `DeviceGrantWireData`, pending-grant persistence, URL fragment
  construction, or any poll/refresh/revoke header.

- [ ] **Step 4: Run focused plugin verification**

  Run:

  ```powershell
  pnpm --filter @workspace/obsidian-plugin test -- contracts.test.ts device-authorization.test.ts
  pnpm --filter @workspace/obsidian-plugin type-check
  ```

  Expected: both commands pass; browser URLs remain server-provided and the
  polling secret is absent from the URLs.

- [ ] **Step 5: Commit the plugin boundary**

  ```powershell
  git add apps/obsidian-plugin/src/authentication/contracts.ts apps/obsidian-plugin/src/authentication/contracts.test.ts apps/obsidian-plugin/src/authentication/device-authorization.test.ts
  git commit -m "fix: align plugin login with public origin"
  ```

### Task 2: Preserve browser Origin security while admitting native grants

**Files:**
- Modify: `apps/api/src/api_runtime/authentication_dependencies.py`
- Modify: `apps/api/src/api_runtime/device_authorization_routes.py:125-140`
- Modify: `tests/unit/api_runtime/test_device_authorization_routes.py:129-220`
- Modify: `tests/contract/api/test_authentication_route_set.py` only if route dependency behavior is structurally pinned there

**Interfaces:**
- Consumes: `SessionRouteDependencies.require_allowed_origin` and the existing `DeviceGrantRequest` route.
- Produces: `require_native_or_allowed_origin(request: Request) -> Awaitable[None]`, accepted only by unauthenticated native device grant creation.

- [ ] **Step 1: Write failing API route tests**

  Add these cases around the existing grant-creation tests:

  ```python
  def test_create_grant_accepts_a_native_request_without_origin(harness: DeviceRouteHarness) -> None:
      response = harness.client.post("/api/auth/device-authorizations", json=grant_request_body())
      assert response.status_code == 200

  def test_create_grant_accepts_the_configured_browser_origin(harness: DeviceRouteHarness) -> None:
      response = harness.client.post(
          "/api/auth/device-authorizations", headers={"Origin": ORIGIN}, json=grant_request_body()
      )
      assert response.status_code == 200

  def test_create_grant_rejects_a_present_unconfigured_origin(harness: DeviceRouteHarness) -> None:
      response = harness.client.post(
          "/api/auth/device-authorizations",
          headers={"Origin": "https://attacker.example"},
          json=grant_request_body(),
      )
      assert response.status_code == 403
      assert response.json()["error"]["code"] == "csrf_validation_failed"
  ```

  Retain the existing no-store assertion for every outcome.

- [ ] **Step 2: Run focused API tests and verify RED**

  Run:

  ```powershell
  uv run pytest tests/unit/api_runtime/test_device_authorization_routes.py -q
  ```

  Expected: the native no-Origin case fails with `csrf_validation_failed`.

- [ ] **Step 3: Add the narrow dependency and bind it only to creation**

  Add `require_native_or_allowed_origin` to `SessionRouteDependencies`:

  ```python
  async def require_native_or_allowed_origin(request: Request) -> None:
      if request.headers.get("origin") is None:
          return
      await require_allowed_origin(request)
  ```

  Bind it only to `POST /api/auth/device-authorizations`. Leave login,
  approval, lookup, TOTP, session, password, refresh, revoke, and all
  browser-visible state-changing routes on their present exact Origin/CSRF
  dependencies. Keep the existing source-address throttle on grant creation.

- [ ] **Step 4: Run API regression gates**

  Run:

  ```powershell
  uv run pytest tests/unit/api_runtime/test_device_authorization_routes.py tests/unit/api_runtime/test_authentication_dependencies.py tests/contract/api/test_authentication_route_set.py -q
  uv run python tools/api_contract_artifacts.py check
  pnpm --filter @workspace/api-client run generate:check
  ```

  Expected: all pass with no OpenAPI artifact diff.

- [ ] **Step 5: Commit the native-grant dependency**

  ```powershell
  git add apps/api/src/api_runtime/authentication_dependencies.py apps/api/src/api_runtime/device_authorization_routes.py tests/unit/api_runtime/test_device_authorization_routes.py tests/contract/api/test_authentication_route_set.py
  git commit -m "fix: admit native device grant requests"
  ```

### Task 3: Make local/live configuration and Settings guidance unambiguous

**Files:**
- Modify: `apps/obsidian-plugin/src/authentication/settings-tab.ts`
- Modify: `apps/obsidian-plugin/src/authentication/settings-tab.test.ts`
- Modify: `apps/obsidian-plugin/wdio.conf.mts:54-57`
- Modify: `.local/RESTART.md:52-78`
- Modify: `.local/serve-live-ci.sh:16-18,95-112`
- Modify: `tools/obsidian_live_acceptance_bootstrap.py`
- Test: `tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py`
- Modify: `docs/operations/exclusion-policy-device-verification.md:7-21`

**Interfaces:**
- Consumes: `KNOWLEDGE_AUTH_ALLOWED_ORIGIN`, `E2E_ALLOWED_ORIGIN`,
  `E2E_PLUGIN_ORIGIN`, and the settings-tab view model.
- Produces: one public-origin value for browser and plugin fixtures; a local
  `E2E_SERVER_ORIGIN` is never a plugin setting.

- [ ] **Step 1: Write failing configuration and Settings tests**

  Add a settings-tab assertion for visible copy equivalent to:

  ```ts
  expect(screen.getByText(/Public workspace origin.*Web Admin.*\/api/i)).toBeVisible();
  ```

  Update `build_live_acceptance_config` test fixtures so they no longer pass a
  `plugin_origin` argument. Assert generated environment values set both
  `E2E_ALLOWED_ORIGIN` and `E2E_PLUGIN_ORIGIN` to the one
  `KNOWLEDGE_AUTH_ALLOWED_ORIGIN` literal read from `.local/serve-local.sh`.

- [ ] **Step 2: Run focused tests and verify RED**

  Run:

  ```powershell
  pnpm --filter @workspace/obsidian-plugin test -- settings-tab.test.ts
  uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q
  ```

  Expected: the new topology/copy expectations fail before implementation.

- [ ] **Step 3: Apply the smallest configuration/documentation changes**

  - Label the plugin field `Public workspace origin` and explain that it must
    serve the Web Admin and proxy `/api/*`; retain the stored field name
    `server_origin` for compatibility.
  - Set the WDIO fixture default from the public-origin environment input;
    do not default it to a direct API hostname.
  - Remove `plugin_origin` from `LiveAcceptanceConfig`,
    `build_live_acceptance_config`, and the `--plugin-origin` CLI argument;
    derive `E2E_PLUGIN_ORIGIN` from the validated `allowed_origin` at the one
    `_live_child_environment` construction point.
  - Change tunnel readiness to the public origin's `/api/health/ready` route.
  - Replace the two-hostname topology in restart and operations docs with one
    public origin and state that changing it requires updating
    `KNOWLEDGE_AUTH_ALLOWED_ORIGIN` then restarting API/Web/tunnel routing.

- [ ] **Step 4: Run focused configuration verification**

  Run:

  ```powershell
  pnpm --filter @workspace/obsidian-plugin test -- settings-tab.test.ts
  pnpm --filter @workspace/obsidian-plugin type-check
  uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q
  bash .local/serve-live-ci.sh --help
  ```

  Expected: all test/type commands pass and the launcher still exposes its
  documented `up <knowledge-ci-*>` and `down` commands.

- [ ] **Step 5: Commit the one-origin tooling contract**

  ```powershell
  git add apps/obsidian-plugin/src/authentication/settings-tab.ts apps/obsidian-plugin/src/authentication/settings-tab.test.ts apps/obsidian-plugin/wdio.conf.mts .local/RESTART.md .local/serve-live-ci.sh tools/obsidian_live_acceptance_bootstrap.py tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py docs/operations/exclusion-policy-device-verification.md
  git commit -m "docs: standardize plugin public origin"
  ```

### Task 4: Verify deployment behavior and close the deferred finding

**Files:**
- Modify: `docs/handoff/BACKLOG.md`
- Create: `docs/handoff/2026-09-02-plugin-single-origin-login.md`

**Interfaces:**
- Consumes: the public-origin API configuration and Task 1–3 test results.
- Produces: one handoff with final SHA, exact command results, sanitized
  configuration evidence, decisions, and no remaining plugin-login BACKLOG
  row.

- [ ] **Step 1: Prepare a disposable local stack**

  Read `.local/RESTART.md`, then run exactly:

  ```bash
  CI=true bash .local/serve-live-ci.sh up knowledge-ci-plugin-origin-20260902
  ```

  Use the repository bootstrap helper against that exact project. Do not
  print credentials, origins, URLs, raw paths, or tokens; record only
  readiness outcome, closed reason token, count, and timestamp.

- [ ] **Step 2: Run the focused automation against the one-origin fixture**

  Run the device-authorization route/plugin tests from Tasks 1–3 with the
  fixture plugin origin equal to allowed origin. Confirm the created grant
  returns a verification URL on the configured public origin and that the
  plugin opens that server-returned URL.

- [ ] **Step 3: Perform the required Desktop operator check**

  In a dedicated test Vault, set `Public workspace origin` to the configured
  public origin, select Login, and approve the device in the browser page.
  Verify the plugin reaches Connected and one allowed sync request succeeds.
  Capture only sanitized outcome/reason/count/timestamp evidence; no
  screenshot, hostname, path, user code, or credential enters the handoff.

- [ ] **Step 4: Shut down the disposable stack**

  Run:

  ```bash
  bash .local/serve-live-ci.sh down
  ```

  Expected: the `knowledge-ci-plugin-origin-20260902` project is absent and
  `knowledge-local` remains down.

- [ ] **Step 5: Run final repository verification**

  Run:

  ```powershell
  uv run poe verify
  git diff --check
  git status --short
  ```

  Expected: all gates pass and only intended handoff/backlog edits remain.

- [ ] **Step 6: Remove the resolved backlog row and write the handoff**

  Delete only the `2026-09-01 | device-sync | Plugin login button ...` row
  from `docs/handoff/BACKLOG.md`. Write one handoff containing the final
  commit SHA, test/live gate evidence, the single-origin decision, and no
  copied sensitive configuration. Do not defer the completed item.

- [ ] **Step 7: Commit closure artifacts**

  ```powershell
  git add docs/handoff/BACKLOG.md docs/handoff/2026-09-02-plugin-single-origin-login.md
  git commit -m "docs: close plugin login origin backlog"
  ```
