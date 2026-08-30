import { HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { createApiClient } from "@workspace/api-client";

import { createNativeFetchTransport } from "./native-fetch-transport";
import {
  REQUEST_UNAVAILABLE_ERROR,
  createAuthenticationClient,
  readCsrfTokenFromCookieSource,
  unwrapEnvelope,
  type AuthenticationClient,
} from "./authentication-client";
import {
  CSRF_COOKIE_VALUE,
  MOCK_API_BASE_URL,
  authenticationFailedResponse,
  dismissedEnrollmentResponse,
  installMockCsrfCookie,
  mockApi,
  recoveryCodesResponse,
  recoveryLimitedResponse,
  sessionData,
  sessionResponse,
  totpEnrollmentResponse,
} from "../testing/api-mock-builders";

const server = setupServer();

function createTestClient(): AuthenticationClient {
  const apiClient = createApiClient({
    baseUrl: MOCK_API_BASE_URL,
    transport: createNativeFetchTransport(globalThis.fetch),
  });
  return createAuthenticationClient({
    apiClient,
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
}

function clearMockCookies(): void {
  document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  clearMockCookies();
});
afterAll(() => server.close());

describe("readCsrfTokenFromCookieSource", () => {
  it("reads the production csrf cookie", () => {
    expect(readCsrfTokenFromCookieSource("__Host-admin_csrf=prod-token")).toBe("prod-token");
  });

  it("falls back to the loopback csrf cookie", () => {
    expect(readCsrfTokenFromCookieSource("theme=dark; admin_csrf_local=local-token")).toBe("local-token");
  });

  it("prefers the production cookie when both exist", () => {
    expect(
      readCsrfTokenFromCookieSource("admin_csrf_local=local-token; __Host-admin_csrf=prod-token"),
    ).toBe("prod-token");
  });

  it("returns null when no csrf cookie exists", () => {
    expect(readCsrfTokenFromCookieSource("theme=dark")).toBeNull();
  });
});

describe("unwrapEnvelope", () => {
  it("unwraps a route's data payload from its envelope", () => {
    const session = sessionData("active");
    expect(unwrapEnvelope({ data: { data: session, error: null } })).toEqual({
      ok: true,
      data: session,
    });
  });

  it("surfaces the envelope's registered error body", () => {
    const error = { code: "authentication_failed", details: {}, message: "Simulated failure.", retryable: false };
    expect(unwrapEnvelope({ data: { data: null, error } })).toEqual({
      ok: false,
      error,
    });
  });

  it("falls back to the shared unavailable error when no envelope data is present", () => {
    const fallbackResult = unwrapEnvelope({});
    expect(fallbackResult).toEqual({ ok: false, error: REQUEST_UNAVAILABLE_ERROR });
    if (!fallbackResult.ok) {
      expect(fallbackResult.error).toBe(REQUEST_UNAVAILABLE_ERROR);
    }
  });
});

describe("REQUEST_UNAVAILABLE_ERROR", () => {
  it("is the safe generic body every transport-closed call returns", () => {
    expect(REQUEST_UNAVAILABLE_ERROR).toEqual({
      code: "internal_error",
      details: {},
      message: "The request could not be completed. Check your connection and try again.",
      retryable: true,
    });
  });
});

describe("createAuthenticationClient", () => {
  it("logs in with credentials included and no csrf header", async () => {
    document.cookie = "admin_csrf_local=should-not-be-sent; path=/";
    const seenRequests: Request[] = [];
    server.use(
      mockApi("post", "/api/auth/login", async ({ request }) => {
        seenRequests.push(request);
        return sessionResponse("active");
      }),
    );
    const result = await createTestClient().login({
      username: "owner",
      password: "correct horse battery staple!",
    });
    expect(result).toEqual({
      ok: true,
      data: expect.objectContaining({ state: "active", authenticated: true }),
    });
    expect(seenRequests).toHaveLength(1);
    expect(seenRequests[0]?.credentials).toBe("include");
    expect(seenRequests[0]?.headers.get("x-csrf-token")).toBeNull();
  });

  it("reads the session without a csrf header", async () => {
    const seenRequests: Request[] = [];
    server.use(
      mockApi("get", "/api/auth/session", async ({ request }) => {
        seenRequests.push(request);
        return sessionResponse("pending_totp");
      }),
    );
    const result = await createTestClient().getSession();
    expect(result).toMatchObject({ ok: true, data: { state: "pending_totp" } });
    expect(seenRequests[0]?.credentials).toBe("include");
    expect(seenRequests[0]?.headers.get("x-csrf-token")).toBeNull();
  });

  it("attaches the csrf header read from the cookie at request time", async () => {
    installMockCsrfCookie();
    const seenTokens: (string | null)[] = [];
    server.use(
      mockApi("post", "/api/auth/totp/verify", async ({ request }) => {
        seenTokens.push(request.headers.get("x-csrf-token"));
        return sessionResponse("active");
      }),
    );
    const client = createTestClient();
    const first = await client.verifyTotpChallenge({ code: "123456" });
    document.cookie = "admin_csrf_local=rotated-csrf-token; path=/";
    const second = await client.verifyTotpChallenge({ code: "654321" });
    expect(first).toMatchObject({ ok: true });
    expect(second).toMatchObject({ ok: true });
    expect(seenTokens).toEqual([CSRF_COOKIE_VALUE, "rotated-csrf-token"]);
  });

  it("sends the csrf header on every state-changing session operation", async () => {
    installMockCsrfCookie();
    const client = createTestClient();
    const cases: Array<{ label: string; method: string; path: string; run: () => Promise<unknown> }> = [
      { label: "logout", method: "post", path: "/api/auth/logout", run: () => client.logout() },
      {
        label: "reauthenticate",
        method: "post",
        path: "/api/auth/reauthenticate",
        run: () => client.reauthenticate({ password: "correct horse battery staple!" }),
      },
      {
        label: "change password",
        method: "put",
        path: "/api/auth/password",
        run: () => client.changePassword({ newPassword: "replacement stable phrase!" }),
      },
      {
        label: "verify challenge",
        method: "post",
        path: "/api/auth/totp/verify",
        run: () => client.verifyTotpChallenge({ code: "123456" }),
      },
      {
        label: "start enrollment",
        method: "post",
        path: "/api/auth/totp/enrollments",
        run: () => client.startTotpEnrollment(),
      },
      {
        label: "start recovery",
        method: "post",
        path: "/api/auth/totp/recovery",
        run: () =>
          client.startTotpRecovery({ password: "x".repeat(15), recoveryCode: "ABCD-EFGH-IJKL" }),
      },
      {
        label: "regenerate recovery codes",
        method: "post",
        path: "/api/auth/totp/recovery-codes/regenerate",
        run: () =>
          client.regenerateTotpRecoveryCodes({
            password: "correct horse battery staple!",
            totpCode: "123456",
          }),
      },
      {
        label: "disable totp",
        method: "delete",
        path: "/api/auth/totp",
        run: () =>
          client.disableTotp({ password: "correct horse battery staple!", totpCode: "123456" }),
      },
    ];
    for (const testCase of cases) {
      const seen: (string | null)[] = [];
      server.use(
        mockApi(testCase.method as "post", testCase.path, async ({ request }) => {
          seen.push(request.headers.get("x-csrf-token"));
          if (testCase.path === "/api/auth/totp/enrollments") return totpEnrollmentResponse();
          if (testCase.path === "/api/auth/totp/recovery") return recoveryLimitedResponse();
          if (testCase.path === "/api/auth/totp/recovery-codes/regenerate") {
            return recoveryCodesResponse();
          }
          return sessionResponse("active");
        }),
      );
      await testCase.run();
      expect(seen, testCase.label).toEqual([CSRF_COOKIE_VALUE]);
    }
  });

  it("verifies an enrollment against its enrollment id", async () => {
    installMockCsrfCookie();
    const seenRequests: Request[] = [];
    server.use(
      mockApi("post", "/api/auth/totp/enrollments/e26e0f1c-9884-4d84-a2c3-9d64a0b1f001/verify", async ({ request }) => {
        seenRequests.push(request);
        return recoveryCodesResponse();
      }),
    );
    const result = await createTestClient().verifyTotpEnrollment({
      enrollmentId: "e26e0f1c-9884-4d84-a2c3-9d64a0b1f001",
      code: "123456",
    });
    expect(result).toMatchObject({ ok: true, data: { revision: 3 } });
    expect(seenRequests[0]?.headers.get("x-csrf-token")).toBe(CSRF_COOKIE_VALUE);
  });

  it("dismisses the first-login offer through the enrollment action route", async () => {
    installMockCsrfCookie();
    const seenBodies: string[] = [];
    server.use(
      mockApi("post", "/api/auth/totp/enrollments", async ({ request }) => {
        seenBodies.push(await request.text());
        return dismissedEnrollmentResponse();
      }),
    );
    const result = await createTestClient().dismissInitialTotpOffer();
    expect(result).toMatchObject({ ok: true, data: { action: "dismiss_initial_offer" } });
    expect(seenBodies).toEqual(['{"action":"dismiss_initial_offer"}']);
  });

  it("surfaces the registry error body of a failed call", async () => {
    server.use(mockApi("post", "/api/auth/login", () => authenticationFailedResponse()));
    const result = await createTestClient().login({ username: "owner", password: "wrong password value" });
    expect(result).toEqual({
      ok: false,
      error: {
        code: "authentication_failed",
        details: {},
        message: "Simulated authentication_failed failure.",
        retryable: false,
      },
    });
  });

  it("maps a non-envelope failure to a safe generic error", async () => {
    server.use(
      mockApi("get", "/api/auth/session", () => new HttpResponse("gateway timeout", { status: 504 })),
    );
    const result = await createTestClient().getSession();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("internal_error");
      expect(result.error.retryable).toBe(true);
    }
  });
});
