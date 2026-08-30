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

import { createNativeFetchTransport } from "../../api/native-fetch-transport";
import { readCsrfTokenFromCookieSource } from "../../api/authentication-client";
import {
  MOCK_API_BASE_URL,
  installMockCsrfCookie,
  mockApi,
  rateLimitedResponse,
  recentAuthenticationRequiredResponse,
  recoveryLimitedResponse,
  sessionResponse,
  unauthenticatedResponse,
} from "../../testing/api-mock-builders";
import { createAuthenticationSessionStore } from "../authentication/session-store";
import { createDeviceAdministrationClient } from "./device-administration-client";
import type { DeviceAdministrationClient } from "./device-administration-client";
import {
  DEVICE_GRANT_ID,
  DEVICE_USER_CODE,
  deviceCredentialInvalidResponse,
  deviceDecisionResponse,
  deviceGrantContextResponse,
} from "./device-api-fixtures";
import { DeviceApproval } from "./DeviceApproval";

const server = setupServer();

function createTestClient(): DeviceAdministrationClient {
  return createDeviceAdministrationClient({
    apiClient: createApiClient({
      baseUrl: MOCK_API_BASE_URL,
      transport: createNativeFetchTransport(globalThis.fetch),
    }),
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
}

function stubActiveSession(): void {
  server.use(mockApi("get", "/api/auth/session", () => sessionResponse("active")));
}

function stubLookup(): void {
  server.use(
    mockApi("post", "/api/auth/device-authorizations/lookup", () => deviceGrantContextResponse()),
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  window.history.replaceState({}, "", "/device/approve");
  installMockCsrfCookie();
  stubActiveSession();
});
afterEach(() => {
  server.resetHandlers();
  window.history.replaceState({}, "", "/device/approve");
  document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});
afterAll(() => server.close());

describe("DeviceApproval", () => {
  it("removes the user code fragment before lookup", async () => {
    stubLookup();
    window.history.replaceState({}, "", `/device/approve#ABCD-EFGH`);
    render(<DeviceApproval client={createTestClient()} />);
    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(localStorage).toHaveLength(0);
  });

  it("renders plugin metadata as escaped text only", async () => {
    const hostileDeviceName = '<img src=x onerror="alert(1)"> Sync <script>alert(2)</script>';
    window.history.replaceState({}, "", `/device/approve#${DEVICE_USER_CODE}`);
    server.use(
      mockApi(
        "post",
        "/api/auth/device-authorizations/lookup",
        () => deviceGrantContextResponse({ device_name: hostileDeviceName }),
      ),
    );
    const { container } = render(
      <DeviceApproval client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />,
    );
    expect(await screen.findByText(hostileDeviceName)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.innerHTML).toContain("&lt;img");
    expect(container.innerHTML).not.toContain("<img");
  });

  it("shows the full grant context with the fixed scope and remaining expiry", async () => {
    window.history.replaceState({}, "", `/device/approve#${DEVICE_USER_CODE}`);
    stubLookup();
    render(<DeviceApproval client={createTestClient()} />);
    expect(await screen.findByText("Personal desktop")).toBeInTheDocument();
    expect(screen.getByText(DEVICE_USER_CODE)).toBeInTheDocument();
    expect(screen.getByText("Desktop")).toBeInTheDocument();
    expect(screen.getByText("windows")).toBeInTheDocument();
    expect(screen.getByText("1.4.0")).toBeInTheDocument();
    expect(screen.getByText("obsidian_sync")).toBeInTheDocument();
    expect(screen.getByText(/\(in \d+ minutes\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deny" })).toBeInTheDocument();
  });

  it("approves after recent re-authentication and keeps the grant context out of storage", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/device/approve#${DEVICE_USER_CODE}`);
    stubLookup();
    let approveCalls = 0;
    const approveCsrfHeaders: (string | null)[] = [];
    server.use(
      mockApi("post", `/api/auth/device-authorizations/${DEVICE_GRANT_ID}/approve`, async ({ request }) => {
        approveCalls += 1;
        approveCsrfHeaders.push(request.headers.get("x-csrf-token"));
        return approveCalls === 1
          ? recentAuthenticationRequiredResponse()
          : deviceDecisionResponse("approved");
      }),
    );
    server.use(
      mockApi("post", "/api/auth/reauthenticate", () => sessionResponse("active")),
    );
    render(<DeviceApproval client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await screen.findByText("Personal desktop");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(
      await screen.findByText("Confirm your password again to approve this device."),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("Current password"), "correct horse battery staple!");
    await user.click(screen.getByRole("button", { name: "Confirm password" }));
    expect(await screen.findByRole("heading", { name: "Device approved" })).toBeInTheDocument();
    expect(approveCalls).toBe(2);
    expect(approveCsrfHeaders.every((value) => value === "csrf-round-trip-token")).toBe(true);
    expect(screen.queryByText("Personal desktop")).not.toBeInTheDocument();
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });

  it("denies the request without recent re-authentication", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/device/approve#${DEVICE_USER_CODE}`);
    stubLookup();
    const denyUrls: string[] = [];
    server.use(
      mockApi("post", `/api/auth/device-authorizations/${DEVICE_GRANT_ID}/deny`, ({ request }) => {
        denyUrls.push(new URL(request.url).pathname);
        return deviceDecisionResponse("denied");
      }),
    );
    render(<DeviceApproval client={createTestClient()} />);
    await screen.findByText("Personal desktop");
    await user.click(screen.getByRole("button", { name: "Deny" }));
    expect(await screen.findByRole("heading", { name: "Device denied" })).toBeInTheDocument();
    expect(denyUrls).toEqual([
      `/api/auth/device-authorizations/${DEVICE_GRANT_ID}/deny`,
    ]);
    expect(localStorage).toHaveLength(0);
  });

  it("logs in inline and resolves the in-memory code after the fragment is gone", async () => {
    const user = userEvent.setup();
    server.use(mockApi("get", "/api/auth/session", () => unauthenticatedResponse()));
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("active")));
    const lookupBodies: string[] = [];
    server.use(
      mockApi("post", "/api/auth/device-authorizations/lookup", async ({ request }) => {
        lookupBodies.push(await request.text());
        return deviceGrantContextResponse();
      }),
    );
    window.history.replaceState({}, "", `/device/approve#${DEVICE_USER_CODE}`);
    render(<DeviceApproval client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    expect(await screen.findByRole("heading", { name: "Sign in to approve the device" })).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    await user.type(screen.getByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple!");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Personal desktop")).toBeInTheDocument();
    expect(lookupBodies).toEqual([`{"user_code":"${DEVICE_USER_CODE}"}`]);
    expect(window.location.hash).toBe("");
  });

  it("completes a pending_totp inline login before resolving the grant", async () => {
    const user = userEvent.setup();
    server.use(mockApi("get", "/api/auth/session", () => unauthenticatedResponse()));
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("pending_totp")));
    server.use(mockApi("post", "/api/auth/totp/verify", () => sessionResponse("active")));
    stubLookup();
    window.history.replaceState({}, "", `/device/approve#${DEVICE_USER_CODE}`);
    render(<DeviceApproval client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await user.type(await screen.findByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple!");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await user.type(await screen.findByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("Personal desktop")).toBeInTheDocument();
  });

  it("closes recovery mode as terminal and releases the inline challenge", async () => {
    const user = userEvent.setup();
    const recoveryBodies: string[] = [];
    server.use(mockApi("get", "/api/auth/session", () => unauthenticatedResponse()));
    server.use(mockApi("post", "/api/auth/login", () => sessionResponse("pending_totp")));
    server.use(
      mockApi("post", "/api/auth/totp/recovery", async ({ request }) => {
        recoveryBodies.push(await request.text());
        return recoveryLimitedResponse();
      }),
    );
    window.history.replaceState({}, "", `/device/approve#${DEVICE_USER_CODE}`);
    render(<DeviceApproval client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await user.type(await screen.findByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple!");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    // The challenge carried the inline-login password into the recovery call.
    await user.click(await screen.findByRole("button", { name: "Use a recovery code instead" }));
    await user.type(await screen.findByLabelText("Recovery code"), "ABCD-EFGH-IJKL");
    await user.click(screen.getByRole("button", { name: "Continue with recovery code" }));
    expect(
      await screen.findByText(/Recovery-mode sign-in must be completed on the sign-in page/),
    ).toBeInTheDocument();
    expect(recoveryBodies).toEqual([
      '{"password":"correct horse battery staple!","recovery_code":"ABCD-EFGH-IJKL"}',
    ]);
    // The terminal close drops the challenge together with its one-time state.
    expect(screen.queryByLabelText("Recovery code")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Authentication code")).not.toBeInTheDocument();
  });

  it("closes an unrecognized code with plugin guidance", async () => {
    server.use(
      mockApi("post", "/api/auth/device-authorizations/lookup", () => deviceCredentialInvalidResponse()),
    );
    window.history.replaceState({}, "", `/device/approve#ZZZZ-ZZZZ`);
    render(<DeviceApproval client={createTestClient()} />);
    expect(await screen.findByText(/not recognized/)).toBeInTheDocument();
    expect(screen.getByText(/Open browser again/)).toBeInTheDocument();
  });

  it("asks the user to reopen the browser link when no code is present", async () => {
    render(<DeviceApproval client={createTestClient()} />);
    expect(await screen.findByText(/opened without a device code/)).toBeInTheDocument();
    expect(screen.getByText(/Open browser again/)).toBeInTheDocument();
  });

  it("shows bounded retry guidance when the lookup throttle is active", async () => {
    server.use(
      mockApi("post", "/api/auth/device-authorizations/lookup", () => rateLimitedResponse(540)),
    );
    window.history.replaceState({}, "", `/device/approve#${DEVICE_USER_CODE}`);
    render(<DeviceApproval client={createTestClient()} />);
    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent("Too many attempts. Try again in 9 minutes.");
    expect(note.textContent).not.toContain("540");
    expect(note.textContent).not.toContain("Simulated");
  });
});
