import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

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

import { createNativeFetchTransport } from "../../api/native-fetch-transport";
import {
  createAuthenticationClient,
  readCsrfTokenFromCookieSource,
  type AuthenticationClient,
} from "../../api/authentication-client";
import {
  MOCK_API_BASE_URL,
  authenticationFailedResponse,
  enrollmentStateInvalidResponse,
  installMockCsrfCookie,
  mockApi,
  recoveryCodesResponse,
  recentAuthenticationRequiredResponse,
  sessionResponse,
  totpEnrollmentResponse,
  unauthenticatedResponse,
} from "../../testing/api-mock-builders";
import { createAuthenticationSessionStore } from "./session-store";
import { SecurityPanel } from "./SecurityPanel";

const server = setupServer();

function createTestClient(): AuthenticationClient {
  return createAuthenticationClient({
    apiClient: createApiClient({
      baseUrl: MOCK_API_BASE_URL,
      transport: createNativeFetchTransport(globalThis.fetch),
    }),
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  replaceMock.mockReset();
  installMockCsrfCookie();
  server.use(mockApi("get", "/api/auth/session", () => sessionResponse("active")));
});
afterEach(() => {
  server.resetHandlers();
  document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});
afterAll(() => server.close());

describe("SecurityPanel", () => {
  it("redirects to the login page when the session is not authenticated", async () => {
    server.use(mockApi("get", "/api/auth/session", () => unauthenticatedResponse()));
    render(<SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
  });

  it("shows the active-TOTP controls when enrollment is refused", async () => {
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => enrollmentStateInvalidResponse()));
    render(<SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    expect(await screen.findByText("Two-factor authentication is active.")).toBeInTheDocument();
    expect(screen.queryByText("JBSWY3DPEHPK3PXP")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("offers enrollment when no active TOTP credential exists", async () => {
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => totpEnrollmentResponse()));
    render(<SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
  });

  it("changes the password after confirming the current credentials", async () => {
    const user = userEvent.setup();
    const seenRequests: string[] = [];
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => enrollmentStateInvalidResponse()));
    server.use(
      mockApi("post", "/api/auth/reauthenticate", async ({ request }) => {
        seenRequests.push(`reauthenticate ${await request.text()}`);
        return sessionResponse("active");
      }),
    );
    server.use(
      mockApi("put", "/api/auth/password", async ({ request }) => {
        seenRequests.push(`password ${await request.text()}`);
        return sessionResponse("active");
      }),
    );
    render(<SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await screen.findByText("Two-factor authentication is active.");
    await user.type(screen.getByLabelText("Current password"), "correct horse battery staple!");
    await user.type(screen.getByLabelText("Current TOTP code"), "123456");
    await user.type(screen.getByLabelText("New password"), "replacement stable phrase!");
    await user.type(screen.getByLabelText("Confirm new password"), "replacement stable phrase!");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("Password changed. Other sessions were signed out.")).toBeInTheDocument();
    expect(seenRequests).toEqual([
      'reauthenticate {"password":"correct horse battery staple!","totp_code":"123456"}',
      'password {"new_password":"replacement stable phrase!"}',
    ]);
  });

  it("regenerates recovery codes exactly once and keeps them out of storage", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => enrollmentStateInvalidResponse()));
    server.use(
      mockApi("post", "/api/auth/totp/recovery-codes/regenerate", () => recoveryCodesResponse()),
    );
    render(<SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await screen.findByText("Two-factor authentication is active.");
    await user.type(screen.getByLabelText("Regenerate password"), "correct horse battery staple!");
    await user.type(screen.getByLabelText("Regenerate TOTP code"), "123456");
    await user.click(screen.getByRole("button", { name: "Regenerate recovery codes" }));
    expect(await screen.findByText("ABCD-EFGH-IJKL")).toBeInTheDocument();
    expect(screen.getByText("MNOP-QRST-UVWX")).toBeInTheDocument();
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });

  it("disables TOTP with the current password and a live code", async () => {
    const user = userEvent.setup();
    const seenRequests: string[] = [];
    server.use(
      mockApi("post", "/api/auth/totp/enrollments", () => enrollmentStateInvalidResponse()),
    );
    server.use(
      mockApi("delete", "/api/auth/totp", async ({ request }) => {
        seenRequests.push(await request.text());
        return sessionResponse("active");
      }),
    );
    const { rerender } = render(
      <SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />,
    );
    await screen.findByText("Two-factor authentication is active.");
    await user.type(screen.getByLabelText("Disable password"), "correct horse battery staple!");
    await user.type(screen.getByLabelText("Disable TOTP code"), "123456");
    await user.click(screen.getByRole("button", { name: "Disable two-factor authentication" }));
    await waitFor(() => expect(seenRequests).toEqual([
      '{"password":"correct horse battery staple!","totp_code":"123456"}',
    ]));
    rerender(<SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
  });

  it("signs out through the csrf-protected logout route", async () => {
    const user = userEvent.setup();
    const seenRequests: Request[] = [];
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => enrollmentStateInvalidResponse()));
    server.use(
      mockApi("post", "/api/auth/logout", async ({ request }) => {
        seenRequests.push(request);
        return sessionResponse("revoked");
      }),
    );
    const store = createAuthenticationSessionStore();
    render(<SecurityPanel client={createTestClient()} sessionStore={store} />);
    await screen.findByText("Two-factor authentication is active.");
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
    expect(seenRequests[0]?.headers.get("x-csrf-token")).toBe("csrf-round-trip-token");
    expect(store.getSession()).toBeNull();
  });

  it("surfaces a recent-authentication requirement as an actionable prompt", async () => {
    const user = userEvent.setup();
    server.use(
      mockApi("post", "/api/auth/totp/enrollments", () => recentAuthenticationRequiredResponse()),
    );
    render(<SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    expect(
      await screen.findByText("Confirm your password again to manage two-factor authentication."),
    ).toBeInTheDocument();
  });

  it("shows a generic failure and focuses it when disable fails", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => enrollmentStateInvalidResponse()));
    server.use(mockApi("delete", "/api/auth/totp", () => authenticationFailedResponse()));
    render(<SecurityPanel client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await screen.findByText("Two-factor authentication is active.");
    await user.type(screen.getByLabelText("Disable password"), "correct horse battery staple!");
    await user.type(screen.getByLabelText("Disable TOTP code"), "000000");
    await user.click(screen.getByRole("button", { name: "Disable two-factor authentication" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Disabling two-factor authentication failed. Try again.");
    expect(alert).toHaveFocus();
  });
});
