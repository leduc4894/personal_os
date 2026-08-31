import assert from "node:assert/strict";

import { expect, type Page, type Route } from "@playwright/test";
import { test } from "@playwright/test";

import { E2E_ACCEPTED_LOGIN_PASSWORD, E2E_LOGIN_USERNAME } from "./e2e-credentials";

/**
 * The web security journeys: a password-only login lands on the devices page
 * with no first-login TOTP interstitial, TOTP enrollment is driven from the
 * Security surface with a locally rendered QR and one-time recovery codes,
 * plus logout, response security headers and web-storage hygiene. The API is
 * intercepted with page.route: this spec proves the browser flow, not the
 * backend (full-fidelity E2E belongs to the later end-to-end task).
 */

const REQUEST_ID = "e2e-00000000-0000-4000-8000-000000000001";

const SESSION_COOKIES = ["admin_session_local=e2e-session-value; Path=/; SameSite=Lax"];
const CSRF_COOKIE = ["admin_csrf_local=e2e-csrf-value; Path=/; SameSite=Lax"];

function envelope(data: unknown, headers: Record<string, string> = {}): Record<string, string> {
  return {
    "content-type": "application/json",
    "cache-control": "no-store",
    ...headers,
  };
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

function activeSession(): Record<string, unknown> {
  return {
    absolute_expires_at: "2026-08-17T00:00:00Z",
    authenticated: true,
    idle_expires_at: "2026-08-16T12:00:00Z",
    scopes: ["device_administration_manage", "web_security_manage"],
    state: "active",
  };
}

function pendingTotpSession(): Record<string, unknown> {
  return {
    absolute_expires_at: "2026-08-17T00:00:00Z",
    authenticated: false,
    idle_expires_at: "2026-08-16T12:00:00Z",
    scopes: [],
    state: "pending_totp",
  };
}

function enrollmentOffer(): Record<string, unknown> {
  return {
    action: "start",
    dismissed_at: null,
    enrollment: {
      enrollment_id: "11111111-2222-4333-8444-555555555555",
      expires_at: "2026-08-16T09:10:00Z",
      provisioning_uri: "otpauth://totp/personal:owner?issuer=personal&secret=E2EESUPERSECRET2345",
      secret: "E2EESUPERSECRET2345",
    },
  };
}

function recoveryCodes(): Record<string, unknown> {
  return {
    codes: ["AAAA-BBBB-CCCC", "DDDD-EEEE-FFFF", "GGGG-HHHH-IIII"],
    revision: 1,
  };
}

type RouteFulfillment = (route: Route) => Promise<void>;

function jsonResponse(body: string, status = 200, headers: Record<string, string> = {}): RouteFulfillment {
  return async (route) => {
    await route.fulfill({ status, headers: { "content-type": "application/json", ...headers }, body });
  };
}

/**
 * A session route that answers unauthenticated until the login journey
 * succeeds, then active — the /admin/devices landing page re-probes the
 * session on mount, so its mock must reflect the signed-in state.
 */
async function stubSessionActivatingOnSignIn(page: Page): Promise<() => void> {
  let isSignedIn = false;
  await page.route("**/api/auth/session", async (route) => {
    await route.fulfill({
      status: isSignedIn ? 200 : 401,
      headers: { "content-type": "application/json" },
      body: isSignedIn ? envelopeBody(activeSession()) : errorEnvelopeBody("authentication_required"),
    });
  });
  return () => {
    isSignedIn = true;
  };
}

async function stubActiveSession(page: Page): Promise<void> {
  await page.route("**/api/auth/session", jsonResponse(envelopeBody(activeSession())));
}

test("TOTP enrollment completes through the security page with a local QR and one-time codes", async ({ page }) => {
  const csrfHeaders: (string | undefined)[] = [];
  let enrollmentActionCalls = 0;

  const markSignedIn = await stubSessionActivatingOnSignIn(page);
  await page.route("**/api/auth/login", async (route) => {
    markSignedIn();
    await route.fulfill({
      status: 200,
      headers: envelope(null, { "set-cookie": [...SESSION_COOKIES, ...CSRF_COOKIE].join("\n") }),
      body: envelopeBody(activeSession()),
    });
  });
  await page.route("**/api/auth/totp/enrollments", async (route) => {
    enrollmentActionCalls += 1;
    csrfHeaders.push(route.request().headers()["x-csrf-token"]);
    await route.fulfill({
      status: 200,
      headers: envelope(null),
      body: envelopeBody(enrollmentOffer()),
    });
  });
  await page.route("**/api/auth/totp/enrollments/*/verify", async (route) => {
    csrfHeaders.push(route.request().headers()["x-csrf-token"]);
    expect(route.request().url()).toContain("11111111-2222-4333-8444-555555555555");
    await route.fulfill({
      status: 200,
      headers: envelope(null, { "set-cookie": SESSION_COOKIES.join("\n") }),
      body: envelopeBody(recoveryCodes()),
    });
  });

  const response = await page.goto("/login");
  const contentSecurityPolicy = response?.headers()["content-security-policy"] ?? "";
  expect(contentSecurityPolicy).toContain("default-src 'self'");
  expect(contentSecurityPolicy).toContain("script-src 'self' 'nonce-");
  expect(contentSecurityPolicy).toContain("object-src 'none'");
  expect(contentSecurityPolicy).toContain("frame-ancestors 'none'");
  expect(response?.headers()["referrer-policy"]).toBe("no-referrer");
  expect(response?.headers()["x-content-type-options"]).toBe("nosniff");

  await page.getByLabel("Username").fill(E2E_LOGIN_USERNAME);
  await page.getByLabel("Password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/admin\/devices$/);

  // The Security surface probes enrollment on mount and renders the offer
  // inline: there is no enable button and no skip path on this page.
  await page.goto("/admin/security");
  await expect(page.getByRole("heading", { name: "Set up two-factor authentication" })).toBeVisible();
  await expect(page.getByText("E2EESUPERSECRET2345")).toBeVisible();
  await expect(page.locator(".qr-code svg")).toBeVisible();
  await expect(page.getByRole("button", { name: "Skip for now" })).toHaveCount(0);

  await page.getByLabel("Verification code").fill("123456");
  await page.getByRole("button", { name: "Activate" }).click();

  await expect(page.getByText("Two-factor authentication is active.")).toBeVisible();
  await expect(page.getByText("AAAA-BBBB-CCCC")).toBeVisible();
  await expect(page.getByText("These codes are shown only once. Store them somewhere safe.")).toBeVisible();

  // The state-changing calls carried the CSRF token read from the cookie jar.
  expect(csrfHeaders.every((value) => value === "e2e-csrf-value")).toBe(true);
  expect(enrollmentActionCalls).toBe(1);

  // One-time values disappear once the codes are acknowledged.
  await page.getByRole("button", { name: "I saved the codes" }).click();
  await expect(page.getByText("AAAA-BBBB-CCCC")).toHaveCount(0);
  await expect(page.getByText("E2EESUPERSECRET2345")).toHaveCount(0);

  const storageState = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storageState).toEqual({ local: 0, session: 0 });

  const cookieNames = (await page.context().cookies()).map((cookie) => cookie.name);
  for (const name of cookieNames) {
    expect(["admin_session_local", "admin_csrf_local", "__Host-admin_session", "__Host-admin_csrf"]).toContain(name);
  }
});

test("login lands on the devices page with no first-login TOTP interstitial", async ({ page }) => {
  const requested: string[] = [];
  page.on("request", (request) => requested.push(request.url()));

  const markSignedIn = await stubSessionActivatingOnSignIn(page);
  await page.route("**/api/auth/login", async (route) => {
    markSignedIn();
    await route.fulfill({
      status: 200,
      headers: envelope(null, { "set-cookie": [...SESSION_COOKIES, ...CSRF_COOKIE].join("\n") }),
      body: envelopeBody(activeSession()),
    });
  });
  // If the removed offer ever crept back into the login path, this stub would
  // let it render — the journey then fails on the dialog, the ledger or both.
  await page.route("**/api/auth/totp/enrollments", async (route) => {
    await route.fulfill({ status: 200, headers: envelope(null), body: envelopeBody(enrollmentOffer()) });
  });

  await page.goto("/login");
  await page.getByLabel("Username").fill(E2E_LOGIN_USERNAME);
  await page.getByLabel("Password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/admin\/devices$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  assert(
    !requested.some((url) => url.includes("/api/auth/totp/enrollments")),
    "the removed first-login offer must not reappear in the login path",
  );

  const storageState = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storageState).toEqual({ local: 0, session: 0 });
});

test("a pending_totp login completes through the TOTP challenge", async ({ page }) => {
  const markSignedIn = await stubSessionActivatingOnSignIn(page);
  await page.route("**/api/auth/login", async (route) => {
    await route.fulfill({
      status: 200,
      headers: envelope(null, { "set-cookie": CSRF_COOKIE.join("\n") }),
      body: envelopeBody(pendingTotpSession()),
    });
  });
  await page.route("**/api/auth/totp/verify", async (route) => {
    expect(route.request().postData()).toBe('{"code":"654321"}');
    markSignedIn();
    await route.fulfill({
      status: 200,
      headers: envelope(null, { "set-cookie": SESSION_COOKIES.join("\n") }),
      body: envelopeBody(activeSession()),
    });
  });
  await page.route("**/api/auth/totp/enrollments", async (route) => {
    // The account already holds an active TOTP credential after this login.
    await route.fulfill({ status: 409, headers: envelope(null), body: errorEnvelopeBody("totp_enrollment_state_invalid") });
  });

  await page.goto("/login");
  await page.getByLabel("Username").fill(E2E_LOGIN_USERNAME);
  await page.getByLabel("Password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByLabel("Authentication code")).toBeVisible();
  await page.getByLabel("Authentication code").fill("654321");
  await page.getByRole("button", { name: "Verify" }).click();

  await expect(page).toHaveURL(/\/admin\/devices$/);
});

test("the security page regenerates one-time codes, signs out and keeps storage empty", async ({ page }) => {
  let isSignedOut = false;
  await page.route("**/api/auth/session", async (route) => {
    await route.fulfill({
      status: isSignedOut ? 401 : 200,
      headers: { "content-type": "application/json" },
      body: isSignedOut ? errorEnvelopeBody("authentication_required") : envelopeBody(activeSession()),
    });
  });
  await page.route("**/api/auth/totp/enrollments", async (route) => {
    await route.fulfill({ status: 409, headers: envelope(null), body: errorEnvelopeBody("totp_enrollment_state_invalid") });
  });
  await page.route("**/api/auth/totp/recovery-codes/regenerate", async (route) => {
    await route.fulfill({ status: 200, headers: envelope(null), body: envelopeBody(recoveryCodes()) });
  });
  await page.route("**/api/auth/logout", async (route) => {
    expect(route.request().headers()["x-csrf-token"]).toBe("e2e-csrf-value");
    isSignedOut = true;
    await route.fulfill({
      status: 200,
      headers: envelope(null),
      body: envelopeBody({ ...activeSession(), authenticated: false, scopes: [], state: "revoked" }),
    });
  });
  // The logout call reads the CSRF cookie at request time; install it first.
  await page.context().addCookies([
    { name: "admin_csrf_local", value: "e2e-csrf-value", url: "http://127.0.0.1:3100" },
  ]);

  await page.goto("/admin/security");
  await expect(page.getByText("Two-factor authentication is active.")).toBeVisible();

  await page.getByLabel("Regenerate password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByLabel("Regenerate TOTP code").fill("123456");
  await page.getByRole("button", { name: "Regenerate recovery codes" }).click();

  await expect(page.getByText("AAAA-BBBB-CCCC")).toBeVisible();
  await page.getByRole("button", { name: "I saved the codes" }).click();
  await expect(page.getByText("AAAA-BBBB-CCCC")).toHaveCount(0);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);

  const storageState = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storageState).toEqual({ local: 0, session: 0 });
});

test("the root redirect bases its decision only on the session endpoint", async ({ page }) => {
  await stubActiveSession(page);
  await page.goto("/?returnTo=https://attacker.example/phish");
  await expect(page).toHaveURL(/\/admin\/devices$/);
});
