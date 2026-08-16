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
  dismissedEnrollmentResponse,
  enrollmentStateInvalidResponse,
  installMockCsrfCookie,
  mockApi,
  recoveryCodesResponse,
  recoveryLimitedResponse,
  sessionResponse,
  totpEnrollmentResponse,
  unauthenticatedResponse,
} from "../../testing/api-mock-builders";
import { createAuthenticationSessionStore } from "./session-store";
import { LoginForm } from "./LoginForm";

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

/** Session probe every mounted LoginForm performs before its first paint settles. */
function stubSessionProbe(): void {
  server.use(mockApi("get", "/api/auth/session", () => unauthenticatedResponse()));
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  replaceMock.mockReset();
  installMockCsrfCookie();
  stubSessionProbe();
});
afterEach(() => {
  server.resetHandlers();
  document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});
afterAll(() => server.close());

async function submitPassword(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.type(screen.getByLabelText("Username"), "owner");
  await user.type(screen.getByLabelText("Password"), "correct horse battery staple!");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("LoginForm", () => {
  it("does not persist credentials in web storage", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("active")));
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => enrollmentStateInvalidResponse()));
    const client = createTestClient();
    const store = createAuthenticationSessionStore();
    render(<LoginForm client={client} sessionStore={store} />);
    await user.type(screen.getByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple!");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/admin/devices"));
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });

  it("shows a generic failure for wrong credentials and focuses the error", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/login", () => authenticationFailedResponse()));
    render(<LoginForm client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await submitPassword(user);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Sign-in failed. Check your username and password.");
    expect(alert).toHaveFocus();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("focuses the first missing field instead of sending an incomplete request", async () => {
    const user = userEvent.setup();
    render(<LoginForm client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Enter your username and password.");
    expect(screen.getByLabelText("Username")).toHaveFocus();
  });

  it("records the active session in the memory store on success", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("active")));
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => enrollmentStateInvalidResponse()));
    const store = createAuthenticationSessionStore();
    render(<LoginForm client={createTestClient()} sessionStore={store} />);
    await submitPassword(user);
    await waitFor(() => expect(store.getSession()).toMatchObject({ state: "active" }));
    expect(replaceMock).toHaveBeenCalledWith("/admin/devices");
  });

  it("moves to the TOTP challenge after a pending_totp login", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("pending_totp")));
    render(<LoginForm client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await submitPassword(user);
    expect(await screen.findByLabelText("Authentication code")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("completes the TOTP step and redirects to the admin devices page", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("pending_totp")));
    server.use(mockApi("post", "/api/auth/totp/verify", () => sessionResponse("active")));
    render(<LoginForm client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await submitPassword(user);
    await user.type(await screen.findByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/admin/devices"));
  });

  it("offers the skippable first-login TOTP enrollment after an active login", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("active")));
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => totpEnrollmentResponse()));
    const { container } = render(
      <LoginForm client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />,
    );
    await submitPassword(user);
    expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("dismisses the first-login offer and redirects without enrollment", async () => {
    const user = userEvent.setup();
    const seenBodies: string[] = [];
    let enrollmentsCallCount = 0;
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("active")));
    server.use(
      mockApi("post", "/api/auth/totp/enrollments", async ({ request }) => {
        enrollmentsCallCount += 1;
        seenBodies.push(await request.text());
        // The first call probes the offer; the skip click submits the dismissal.
        return enrollmentsCallCount === 1 ? totpEnrollmentResponse() : dismissedEnrollmentResponse();
      }),
    );
    render(<LoginForm client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await submitPassword(user);
    await user.click(await screen.findByRole("button", { name: "Skip for now" }));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/admin/devices"));
    expect(seenBodies).toEqual(['{"action":"start"}', '{"action":"dismiss_initial_offer"}']);
  });

  it("requires TOTP replacement after recovery-code login without offering a skip", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("pending_totp")));
    server.use(mockApi("post", "/api/auth/totp/recovery", () => recoveryLimitedResponse()));
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => totpEnrollmentResponse()));
    render(<LoginForm client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await submitPassword(user);
    await user.click(await screen.findByRole("button", { name: "Use a recovery code instead" }));
    await user.type(await screen.findByLabelText("Recovery code"), "ABCD-EFGH-IJKL");
    await user.click(screen.getByRole("button", { name: "Continue with recovery code" }));
    expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
    expect(
      screen.getByText("Recovery mode: set up a new authenticator before continuing."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Skip for now" })).not.toBeInTheDocument();
  });

  it("clears one-time enrollment values when navigation continues", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("active")));
    server.use(mockApi("post", "/api/auth/totp/enrollments", () => totpEnrollmentResponse()));
    server.use(
      mockApi("post", "/api/auth/totp/enrollments/e26e0f1c-9884-4d84-a2c3-9d64a0b1f001/verify", () =>
        recoveryCodesResponse(),
      ),
    );
    render(<LoginForm client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await submitPassword(user);
    await user.type(await screen.findByLabelText("Verification code"), "123456");
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByText("ABCD-EFGH-IJKL")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue to devices" }));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/admin/devices"));
    expect(screen.queryByText("ABCD-EFGH-IJKL")).not.toBeInTheDocument();
    expect(screen.queryByText("JBSWY3DPEHPK3PXP")).not.toBeInTheDocument();
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });
});
