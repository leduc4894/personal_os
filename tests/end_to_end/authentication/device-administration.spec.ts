import { expect, type Page, type Route } from "@playwright/test";
import { test } from "@playwright/test";

import { E2E_ACCEPTED_LOGIN_PASSWORD, E2E_LOGIN_USERNAME } from "./e2e-credentials";

/**
 * The device administration journeys (Task 13 scope): the approval page that
 * consumes the user-code fragment exactly once and signs in inline, the
 * recent-re-authentication gate around approval, and the Admin devices page
 * with its guarded revocation dialog. The API is intercepted with page.route:
 * this spec proves the browser flow, not the backend.
 */

const REQUEST_ID = "e2e-00000000-0000-4000-8000-000000000002";

const SESSION_COOKIES = ["admin_session_local=e2e-session-value; Path=/; SameSite=Lax"];
const CSRF_COOKIE = ["admin_csrf_local=e2e-csrf-value; Path=/; SameSite=Lax"];

const GRANT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const USER_CODE = "BCDF-GHJK";
const DEVICE_ID = "11111111-2222-4333-8444-555555555556";

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

function fulfill(body: string, status = 200, headers: Record<string, string> = {}): RouteFulfillment {
  return async (route) => {
    await route.fulfill({ status, headers: jsonResponseHeaders(headers), body });
  };
}

type RouteFulfillment = (route: Route) => Promise<void>;

function activeSession(): Record<string, unknown> {
  return {
    absolute_expires_at: "2026-08-17T00:00:00Z",
    authenticated: true,
    idle_expires_at: "2026-08-16T12:00:00Z",
    scopes: ["device_administration_manage", "web_security_manage"],
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

function adminDevices(laptopRevoked: boolean): Record<string, unknown>[] {
  return [
    {
      device_id: DEVICE_ID,
      device_name: "Family laptop",
      family_absolute_expires_at: "2027-02-16T09:00:00Z",
      last_seen_at: "2026-08-15T18:30:00Z",
      platform_class: "obsidian_desktop",
      platform_name: "windows",
      plugin_version: "1.4.0",
      registered_at: "2026-07-01T10:00:00Z",
      revoked_at: laptopRevoked ? "2026-08-16T09:05:00Z" : null,
      status: laptopRevoked ? "revoked" : "active",
    },
    {
      device_id: "22222222-3333-4444-8555-666666666666",
      device_name: "Old phone",
      family_absolute_expires_at: null,
      last_seen_at: null,
      platform_class: "obsidian_mobile",
      platform_name: "android",
      plugin_version: "1.3.0",
      registered_at: "2026-06-01T08:00:00Z",
      revoked_at: "2026-07-15T12:00:00Z",
      status: "revoked",
    },
  ];
}

async function stubUnauthenticatedSession(page: Page): Promise<void> {
  await page.route("**/api/auth/session", fulfill(errorEnvelopeBody("authentication_required"), 401));
}

async function stubActiveSession(page: Page): Promise<void> {
  await page.route("**/api/auth/session", fulfill(envelopeBody(activeSession())));
}

async function stubInlineLogin(page: Page): Promise<void> {
  await page.route(
    "**/api/auth/login",
    fulfill(envelopeBody(activeSession()), 200, {
      "set-cookie": [...SESSION_COOKIES, ...CSRF_COOKIE].join("\n"),
    }),
  );
}

async function expectStorageEmpty(page: Page): Promise<void> {
  const storageState = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storageState).toEqual({ local: 0, session: 0 });
}

test("the approval page consumes the fragment once, signs in inline, re-authenticates and approves", async ({ page }) => {
  await stubUnauthenticatedSession(page);
  await stubInlineLogin(page);
  const lookupBodies: string[] = [];
  await page.route("**/api/auth/device-authorizations/lookup", async (route) => {
    lookupBodies.push(route.request().postData() ?? "");
    await route.fulfill({ status: 200, headers: jsonResponseHeaders(), body: envelopeBody(grantContext()) });
  });
  let approveCalls = 0;
  const approveCsrfHeaders: (string | undefined)[] = [];
  await page.route(`**/api/auth/device-authorizations/${GRANT_ID}/approve`, async (route) => {
    approveCalls += 1;
    approveCsrfHeaders.push(route.request().headers()["x-csrf-token"]);
    if (approveCalls === 1) {
      await route.fulfill({ status: 403, headers: jsonResponseHeaders(), body: errorEnvelopeBody("recent_authentication_required") });
      return;
    }
    await route.fulfill({
      status: 200,
      headers: jsonResponseHeaders(),
      body: envelopeBody({ decided_at: "2026-08-16T09:05:00Z", grant_id: GRANT_ID, state: "approved" }),
    });
  });
  await page.route(
    "**/api/auth/reauthenticate",
    fulfill(envelopeBody(activeSession()), 200, { "set-cookie": SESSION_COOKIES.join("\n") }),
  );

  await page.goto(`/device/approve#${USER_CODE}`);
  await expect(page.getByRole("heading", { name: "Sign in to approve the device" })).toBeVisible();
  // The fragment was consumed exactly once and stripped from the address bar.
  expect(await page.evaluate(() => window.location.hash)).toBe("");

  await page.getByLabel("Username").fill(E2E_LOGIN_USERNAME);
  await page.getByLabel("Password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // The grant resolved with the in-memory code; every metadata value renders as text.
  await expect(page.getByText("Personal desktop")).toBeVisible();
  await expect(page.getByText(USER_CODE)).toBeVisible();
  await expect(page.getByText("Desktop", { exact: true })).toBeVisible();
  await expect(page.getByText("windows")).toBeVisible();
  await expect(page.getByText("1.4.0")).toBeVisible();
  await expect(page.getByText("obsidian_sync")).toBeVisible();
  await expect(page.getByText(/\(in \d+ minutes\)/)).toBeVisible();

  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Confirm your password again to approve this device.")).toBeVisible();
  await page.getByLabel("Current password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Confirm password" }).click();

  await expect(page.getByRole("heading", { name: "Device approved" })).toBeVisible();
  expect(approveCalls).toBe(2);
  expect(lookupBodies).toEqual([`{"user_code":"${USER_CODE}"}`]);
  expect(approveCsrfHeaders.every((value) => value === "e2e-csrf-value")).toBe(true);
  await expectStorageEmpty(page);
});

test("a signed-in browser can deny the pending request without recent re-authentication", async ({ page }) => {
  await stubActiveSession(page);
  await page.route(
    "**/api/auth/device-authorizations/lookup",
    fulfill(envelopeBody(grantContext())),
  );
  const denyUrls: string[] = [];
  await page.route(`**/api/auth/device-authorizations/${GRANT_ID}/deny`, async (route) => {
    denyUrls.push(new URL(route.request().url()).pathname);
    await route.fulfill({
      status: 200,
      headers: jsonResponseHeaders(),
      body: envelopeBody({ decided_at: "2026-08-16T09:05:00Z", grant_id: GRANT_ID, state: "denied" }),
    });
  });

  await page.goto(`/device/approve#${USER_CODE}`);
  await expect(page.getByText("Personal desktop")).toBeVisible();
  expect(await page.evaluate(() => window.location.hash)).toBe("");

  await page.getByRole("button", { name: "Deny" }).click();
  await expect(page.getByRole("heading", { name: "Device denied" })).toBeVisible();
  expect(denyUrls).toEqual([`/api/auth/device-authorizations/${GRANT_ID}/deny`]);
  await expectStorageEmpty(page);
});

test("the devices page lists spec fields, keeps revoked rows read-only and revokes behind both guards", async ({ page }) => {
  await stubActiveSession(page);
  let laptopRevoked = false;
  let listCalls = 0;
  await page.route("**/api/admin/devices", async (route) => {
    listCalls += 1;
    await route.fulfill({
      status: 200,
      headers: jsonResponseHeaders(),
      body: envelopeBody({ devices: adminDevices(laptopRevoked) }),
    });
  });
  const revokeBodies: string[] = [];
  let revokeCalls = 0;
  await page.route(`**/api/admin/devices/${DEVICE_ID}/revoke`, async (route) => {
    revokeCalls += 1;
    revokeBodies.push(route.request().postData() ?? "");
    if (revokeCalls === 1) {
      // A server-side rename race: the exact client-side name no longer matches.
      await route.fulfill({ status: 409, headers: jsonResponseHeaders(), body: errorEnvelopeBody("device_revocation_confirmation_invalid") });
      return;
    }
    if (revokeCalls === 2) {
      await route.fulfill({ status: 403, headers: jsonResponseHeaders(), body: errorEnvelopeBody("recent_authentication_required") });
      return;
    }
    laptopRevoked = true;
    await route.fulfill({
      status: 200,
      headers: jsonResponseHeaders(),
      body: envelopeBody({ device_id: DEVICE_ID, revoked_at: "2026-08-16T09:05:00Z" }),
    });
  });
  await page.route(
    "**/api/auth/reauthenticate",
    fulfill(envelopeBody(activeSession()), 200, { "set-cookie": SESSION_COOKIES.join("\n") }),
  );
  await page.context().addCookies([
    { name: "admin_csrf_local", value: "e2e-csrf-value", url: "http://127.0.0.1:3100" },
  ]);

  await page.goto("/admin/devices");
  await expect(page.getByRole("heading", { name: "Devices" })).toBeVisible();
  const laptopRow = page.getByRole("row", { name: /Family laptop/ });
  await expect(laptopRow).toBeVisible();
  await expect(page.getByRole("row", { name: /Old phone/ })).toBeVisible();
  await expect(page.getByText("Mobile")).toBeVisible();
  await expect(page.getByText("android")).toBeVisible();
  await expect(page.getByText("1.3.0")).toBeVisible();
  await expect(page.getByText("Not seen yet")).toBeVisible();
  // The revoked row stays read-only.
  await expect(page.getByRole("button", { name: "Revoke Old phone" })).toHaveCount(0);

  await page.getByRole("button", { name: "Revoke Family laptop" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // The exact-name confirmation gates the request client-side.
  await page.getByLabel("Type the device name to confirm").fill("Family lapto");
  await expect(page.getByRole("button", { name: "Revoke device" })).toBeDisabled();
  await page.getByLabel("Type the device name to confirm").fill("Family laptop");
  await expect(page.getByRole("button", { name: "Revoke device" })).toBeEnabled();
  await page.getByRole("button", { name: "Revoke device" }).click();
  await expect(page.getByText("The device name did not match. Check the exact name and try again.")).toBeVisible();

  await page.getByRole("button", { name: "Revoke device" }).click();
  await expect(page.getByText("Confirm your password again to revoke this device.")).toBeVisible();
  await page.getByLabel("Current password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Confirm password" }).click();

  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(laptopRow.getByText("Revoked")).toBeVisible();
  await expect(laptopRow.getByRole("button")).toHaveCount(0);
  expect(listCalls).toBe(2);
  expect(revokeBodies).toEqual([
    '{"device_name_confirmation":"Family laptop"}',
    '{"device_name_confirmation":"Family laptop"}',
    '{"device_name_confirmation":"Family laptop"}',
  ]);
  await expectStorageEmpty(page);
});
