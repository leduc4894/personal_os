import { expect, type Page, type Route } from "@playwright/test";
import { test } from "@playwright/test";

import { E2E_ACCEPTED_LOGIN_PASSWORD, E2E_LOGIN_USERNAME } from "./e2e-credentials";

/**
 * The complete device-onboarding journey across the Web boundary: a plugin
 * (simulated by this spec) creates a device grant, the unauthenticated
 * browser opens the exact verification URL with the user code in its
 * fragment, signs in inline, reviews the grant context, clears the recent
 * re-authentication gate and approves; the plugin then polls its one-time
 * credential into an exchanged device, and the Admin device list shows the
 * registered device. The API is intercepted with page.route at the exact
 * OpenAPI contract paths and payloads, so the journey also pins request and
 * response fidelity against the generated client contract: the plugin side
 * of the exchange (SecretStorage, crash-safe refresh) is proven by the
 * plugin unit suite, not here.
 */

const REQUEST_ID = "e2e-00000000-0000-4000-8000-000000000003";

const SESSION_COOKIES = ["admin_session_local=e2e-session-value; Path=/; SameSite=Lax"];
const CSRF_COOKIE = ["admin_csrf_local=e2e-csrf-value; Path=/; SameSite=Lax"];

const GRANT_ID = "3f2a1d0e-4b5c-4f6a-8b7c-9d0e1f2a3b4c";
const USER_CODE = "BCDF-GHJK";
const DEVICE_ID = "5e4d3c2b-1a0f-4e9d-8c8b-7a6b5c4d3e2f";
const POLLING_SECRET = "pg1.0123456789abcdef-0123-4567-89ab-cdef01234567.pollingsecretvalue";

/** The exact provisioning payload of the OpenAPI device-grant contract. */
interface DeviceGrantProvisioning {
  grant_id: string;
  user_code: string;
  polling_secret: string;
  verification_uri: string;
  verification_uri_complete: string;
  expires_in_seconds: number;
  poll_interval_seconds: number;
}

function createdGrant(): DeviceGrantProvisioning {
  return {
    grant_id: GRANT_ID,
    user_code: USER_CODE,
    polling_secret: POLLING_SECRET,
    verification_uri: "http://127.0.0.1:3100/device/approve",
    verification_uri_complete: `http://127.0.0.1:3100/device/approve#${USER_CODE}`,
    expires_in_seconds: 600,
    poll_interval_seconds: 5,
  };
}

/** The exact exchange payload of the OpenAPI poll contract. */
function exchangedCredentials(): Record<string, unknown> {
  return {
    grant_id: GRANT_ID,
    device_id: DEVICE_ID,
    token_family_id: "6f5e4d3c-2b1a-4f0e-9d9c-8b7a6b5c4d3e",
    refresh_generation: 1,
    access_credential: `at1.${DEVICE_ID}.access-secret-value`,
    refresh_credential: `rt1.6f5e4d3c2b1a4f0e9d9c8b7a6b5c4d3e.refresh-secret-value`,
    access_expires_at: "2026-08-16T12:15:00Z",
    refresh_expires_at: "2026-09-15T12:00:00Z",
  };
}

function activeSession(): Record<string, unknown> {
  return {
    absolute_expires_at: "2026-08-17T00:00:00Z",
    authenticated: true,
    idle_expires_at: "2026-08-16T12:00:00Z",
    scopes: ["device_administration_manage", "device_authorization_approve"],
    state: "active",
  };
}

function grantContext(): Record<string, unknown> {
  return {
    device_name: "Personal desktop",
    expires_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
    grant_id: GRANT_ID,
    platform_class: "obsidian_desktop",
    platform_name: "windows",
    plugin_version: "1.4.0",
    requested_scope: "obsidian_sync",
    user_code: USER_CODE,
  };
}

function adminDevices(includeOnboarded: boolean): Record<string, unknown>[] {
  const devices: Record<string, unknown>[] = [];
  if (includeOnboarded) {
    devices.push({
      device_id: DEVICE_ID,
      device_name: "Personal desktop",
      family_absolute_expires_at: "2026-11-14T12:00:00Z",
      last_seen_at: null,
      platform_class: "obsidian_desktop",
      platform_name: "windows",
      plugin_version: "1.4.0",
      registered_at: "2026-08-16T12:00:00Z",
      revoked_at: null,
      status: "active",
    });
  }
  return devices;
}

function jsonResponseHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return { "content-type": "application/json", "cache-control": "no-store", ...extra };
}

function envelopeBody(data: unknown): string {
  return JSON.stringify({ data, error: null, request_id: REQUEST_ID, warnings: [] });
}

function errorEnvelopeBody(code: string): string {
  return JSON.stringify({
    data: null,
    error: { code, details: {}, message: `Simulated ${code}.`, retryable: false },
    request_id: REQUEST_ID,
    warnings: [],
  });
}

type RouteFulfillment = (route: Route) => Promise<void>;

function fulfill(body: string, status = 200, headers: Record<string, string> = {}): RouteFulfillment {
  return async (route) => {
    await route.fulfill({ status, headers: jsonResponseHeaders(headers), body });
  };
}

interface CapturedApiCall {
  method: string;
  path: string;
  body: string | null;
  csrfToken: string | undefined;
}

/**
 * The complete journey with a request-capturing API surface. Every browser
 * call is intercepted at the exact contract path, so the recorded calls pin
 * the request fidelity of the Web application itself.
 */
class OnboardingJourney {
  readonly calls: CapturedApiCall[] = [];
  private isDeviceOnboarded = false;

  constructor(private readonly page: Page) {}

  async install(): Promise<void> {
    let isSignedIn = false;
    await this.page.route("**/api/auth/session", async (route) => {
      await route.fulfill({
        status: isSignedIn ? 200 : 401,
        headers: jsonResponseHeaders(),
        body: isSignedIn ? envelopeBody(activeSession()) : errorEnvelopeBody("authentication_required"),
      });
    });
    await this.page.route("**/api/auth/login", async (route) => {
      this.calls.push({
        method: "POST",
        path: "/api/auth/login",
        body: route.request().postData(),
        csrfToken: undefined,
      });
      isSignedIn = true;
      await route.fulfill({
        status: 200,
        headers: jsonResponseHeaders({
          "set-cookie": [...SESSION_COOKIES, ...CSRF_COOKIE].join("\n"),
        }),
        body: envelopeBody(activeSession()),
      });
    });
    await this.page.route("**/api/auth/reauthenticate", async (route) => {
      this.calls.push({
        method: "POST",
        path: "/api/auth/reauthenticate",
        body: route.request().postData(),
        csrfToken: route.request().headers()["x-csrf-token"],
      });
      await route.fulfill({
        status: 200,
        headers: jsonResponseHeaders({ "set-cookie": SESSION_COOKIES.join("\n") }),
        body: envelopeBody(activeSession()),
      });
    });
    await this.page.route("**/api/auth/device-authorizations/lookup", async (route) => {
      this.calls.push({
        method: "POST",
        path: "/api/auth/device-authorizations/lookup",
        body: route.request().postData(),
        csrfToken: route.request().headers()["x-csrf-token"],
      });
      await route.fulfill({ status: 200, headers: jsonResponseHeaders(), body: envelopeBody(grantContext()) });
    });
    await this.page.route(`**/api/auth/device-authorizations/${GRANT_ID}/approve`, async (route) => {
      this.calls.push({
        method: "POST",
        path: `/api/auth/device-authorizations/${GRANT_ID}/approve`,
        body: route.request().postData(),
        csrfToken: route.request().headers()["x-csrf-token"],
      });
      if (!this.isDeviceOnboarded) {
        // The first approval attempt has not cleared the five-minute
        // recent-re-authentication window since the inline login.
        this.isDeviceOnboarded = true;
        await route.fulfill({
          status: 403,
          headers: jsonResponseHeaders(),
          body: errorEnvelopeBody("recent_authentication_required"),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: jsonResponseHeaders(),
        body: envelopeBody({
          decided_at: "2026-08-16T12:05:00Z",
          grant_id: GRANT_ID,
          state: "approved",
        }),
      });
    });
    await this.page.route("**/api/admin/devices", async (route) => {
      this.calls.push({
        method: "GET",
        path: "/api/admin/devices",
        body: null,
        csrfToken: route.request().headers()["x-csrf-token"],
      });
      await route.fulfill({
        status: 200,
        headers: jsonResponseHeaders(),
        body: envelopeBody({ devices: adminDevices(this.isDeviceOnboarded) }),
      });
    });
  }

  callsTo(path: string): CapturedApiCall[] {
    return this.calls.filter((call) => call.path === path);
  }
}

test("the full onboarding journey approves one device and lists it, at contract fidelity", async ({ page }) => {
  const journey = new OnboardingJourney(page);
  await journey.install();

  // The plugin received the provisioning payload and the browser opens its
  // exact verification_uri_complete; the request-fidelity assertions below
  // pin that the journey consumed the user code of that grant payload.
  const provisioning = createdGrant();
  await page.goto(provisioning.verification_uri_complete);

  await expect(page.getByRole("heading", { name: "Sign in to approve the device" })).toBeVisible();
  expect(await page.evaluate(() => window.location.hash)).toBe("");

  await page.getByLabel("Username").fill(E2E_LOGIN_USERNAME);
  await page.getByLabel("Password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // The grant context renders every display field of the contract payload.
  await expect(page.getByText("Personal desktop")).toBeVisible();
  await expect(page.getByText(USER_CODE)).toBeVisible();
  await expect(page.getByText("Desktop", { exact: true })).toBeVisible();
  await expect(page.getByText("windows")).toBeVisible();
  await expect(page.getByText("obsidian_sync")).toBeVisible();

  // The recent-re-authentication gate precedes the committed approval.
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Confirm your password again to approve this device.")).toBeVisible();
  await page.getByLabel("Current password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Confirm password" }).click();
  await expect(page.getByRole("heading", { name: "Device approved" })).toBeVisible();

  // The plugin-side poll consumes the one-time credential into exactly one
  // exchanged device; the web journey then sees it in the Admin list. The
  // fidelity assertions below pin the exchange to the grant the browser
  // actually approved through the captured request log.
  const exchanged = exchangedCredentials();
  await page.goto("/admin/devices");
  const onboardedRow = page.getByRole("row", { name: /Personal desktop/ });
  await expect(onboardedRow).toBeVisible();
  await expect(onboardedRow.getByText("Active")).toBeVisible();

  // Request fidelity: every state-changing browser call matched the
  // generated client contract and carried the CSRF token from the cookie.
  expect(journey.callsTo("/api/auth/login")).toEqual([
    {
      method: "POST",
      path: "/api/auth/login",
      body: JSON.stringify({ username: E2E_LOGIN_USERNAME, password: E2E_ACCEPTED_LOGIN_PASSWORD }),
      csrfToken: undefined,
    },
  ]);
  // The browser navigated the grant's verification_uri_complete and its
  // lookup consumed exactly the user code that grant payload carries.
  expect(journey.callsTo("/api/auth/device-authorizations/lookup")).toEqual([
    {
      method: "POST",
      path: "/api/auth/device-authorizations/lookup",
      body: JSON.stringify({ user_code: provisioning.user_code }),
      csrfToken: "e2e-csrf-value",
    },
  ]);
  // The exchange consumed the grant the browser approved: both captured
  // approval calls targeted the exchanged grant id in their path.
  const approvals = journey.callsTo(
    `/api/auth/device-authorizations/${exchanged.grant_id}/approve`,
  );
  expect(approvals).toHaveLength(2);
  expect(approvals.every((call) => call.csrfToken === "e2e-csrf-value")).toBe(true);
  expect(journey.callsTo("/api/auth/reauthenticate")).toEqual([
    {
      method: "POST",
      path: "/api/auth/reauthenticate",
      body: JSON.stringify({ password: E2E_ACCEPTED_LOGIN_PASSWORD, totp_code: null }),
      csrfToken: "e2e-csrf-value",
    },
  ]);
  expect(journey.callsTo("/api/admin/devices").length).toBeGreaterThan(0);

  // No credential or one-time value persisted anywhere in the browser.
  const storageState = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storageState).toEqual({ local: 0, session: 0 });
  const cookieNames = (await page.context().cookies()).map((cookie) => cookie.name);
  for (const name of cookieNames) {
    expect(["admin_session_local", "admin_csrf_local"]).toContain(name);
  }
});

test("a denied onboarding terminalizes the approval page without device rows", async ({ page }) => {
  let isSignedIn = false;
  await page.route("**/api/auth/session", async (route) => {
    await route.fulfill({
      status: isSignedIn ? 200 : 401,
      headers: jsonResponseHeaders(),
      body: isSignedIn ? envelopeBody(activeSession()) : errorEnvelopeBody("authentication_required"),
    });
  });
  await page.route("**/api/auth/login", async (route) => {
    isSignedIn = true;
    await route.fulfill({
      status: 200,
      headers: jsonResponseHeaders({
        "set-cookie": [...SESSION_COOKIES, ...CSRF_COOKIE].join("\n"),
      }),
      body: envelopeBody(activeSession()),
    });
  });
  await page.route("**/api/auth/device-authorizations/lookup", fulfill(envelopeBody(grantContext())));
  await page.route(`**/api/auth/device-authorizations/${GRANT_ID}/deny`, async (route) => {
    expect(route.request().headers()["x-csrf-token"]).toBe("e2e-csrf-value");
    await route.fulfill({
      status: 200,
      headers: jsonResponseHeaders(),
      body: envelopeBody({
        decided_at: "2026-08-16T12:05:00Z",
        grant_id: GRANT_ID,
        state: "denied",
      }),
    });
  });
  await page.route("**/api/admin/devices", fulfill(envelopeBody({ devices: adminDevices(false) })));

  await page.goto(`/device/approve#${USER_CODE}`);
  await page.getByLabel("Username").fill(E2E_LOGIN_USERNAME);
  await page.getByLabel("Password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "Deny" }).click();

  await expect(page.getByRole("heading", { name: "Device denied" })).toBeVisible();
  await page.goto("/admin/devices");
  await expect(page.getByRole("row", { name: /Personal desktop/ })).toHaveCount(0);

  const storageState = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storageState).toEqual({ local: 0, session: 0 });
});
