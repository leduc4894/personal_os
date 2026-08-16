import { render, screen, waitFor } from "@testing-library/react";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@workspace/api-client";

const routerMock = vi.hoisted(() => ({
  replace: vi.fn(),
  push: vi.fn(),
  refresh: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));
const replaceMock = routerMock.replace;

import { createNativeFetchTransport } from "../api/native-fetch-transport";
import {
  createAuthenticationClient,
  type AuthenticationClient,
} from "../api/authentication-client";
import { MOCK_API_BASE_URL, mockApi, sessionResponse } from "../testing/api-mock-builders";
import { SessionRedirect } from "./session-redirect";

const server = setupServer();

function createTestClient(): AuthenticationClient {
  return createAuthenticationClient({
    apiClient: createApiClient({
      baseUrl: MOCK_API_BASE_URL,
      transport: createNativeFetchTransport(globalThis.fetch),
    }),
    readCsrfToken: () => document.cookie.match(/__Host-admin_csrf=([^;]+)/)?.[1] ?? null,
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  replaceMock.mockReset();
});
afterAll(() => server.close());

describe("SessionRedirect", () => {
  it("redirects an active session to the admin devices page", async () => {
    server.use(mockApi("get", "/api/auth/session", () => sessionResponse("active")));
    render(<SessionRedirect client={createTestClient()} />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/admin/devices"));
  });

  it("redirects an unauthenticated visitor to the login page", async () => {
    server.use(
      mockApi("get", "/api/auth/session", () => {
        return Response.json({
          data: null,
          error: { code: "authentication_required", details: {}, message: "Sign in first.", retryable: false },
          request_id: "5b34a3ca-8a30-4f6f-9b1e-1d2a1a1b9c10",
          warnings: [],
        }, { status: 401 });
      }),
    );
    render(<SessionRedirect client={createTestClient()} />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
  });

  it("sends challenge sessions to the login page, never to an attacker-controlled return url", async () => {
    server.use(mockApi("get", "/api/auth/session", () => sessionResponse("pending_totp")));
    window.history.replaceState({}, "Test", "/?returnTo=https://attacker.example/admin");
    render(<SessionRedirect client={createTestClient()} />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
    expect(replaceMock).not.toHaveBeenCalledWith(expect.stringContaining("attacker.example"));
  });

  it("waits for the session probe without flashing content", () => {
    server.use(mockApi("get", "/api/auth/session", () => sessionResponse("active")));
    render(<SessionRedirect client={createTestClient()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking your session");
  });
});
