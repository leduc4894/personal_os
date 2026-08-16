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
import { readCsrfTokenFromCookieSource } from "../../api/authentication-client";
import {
  MOCK_API_BASE_URL,
  errorResponse,
  installMockCsrfCookie,
  mockApi,
  sessionResponse,
  unauthenticatedResponse,
} from "../../testing/api-mock-builders";
import { createAuthenticationSessionStore } from "../authentication/session-store";
import { createDeviceAdministrationClient } from "./device-administration-client";
import type { DeviceAdministrationClient } from "./device-administration-client";
import {
  adminDeviceData,
  adminDeviceListResponse,
  adminDeviceRevokeResponse,
} from "./device-api-fixtures";
import { DeviceList } from "./DeviceList";

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

describe("DeviceList", () => {
  it("redirects to the login page when the session is not authenticated", async () => {
    server.use(mockApi("get", "/api/auth/session", () => unauthenticatedResponse()));
    render(<DeviceList client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
  });

  it("renders exactly the spec field set for each device", async () => {
    server.use(
      mockApi("get", "/api/admin/devices", () =>
        adminDeviceListResponse([
          adminDeviceData({ device_name: "Family laptop" }),
          adminDeviceData({
            device_id: "11111111-2222-4333-8444-555555555557",
            device_name: "Old phone",
            family_absolute_expires_at: null,
            last_seen_at: null,
            platform_class: "obsidian_mobile",
            platform_name: "android",
            plugin_version: "1.3.0",
            revoked_at: "2026-07-15T12:00:00Z",
            status: "revoked",
          }),
        ]),
      ),
    );
    const { container } = render(
      <DeviceList client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />,
    );
    const headers = await screen.findAllByRole("columnheader");
    expect(headers.map((header) => header.textContent)).toEqual([
      "Device name",
      "Type",
      "Platform",
      "Plugin version",
      "Status",
      "Registered",
      "Last seen",
      "Revoked",
      "Family expires",
      "Actions",
    ]);
    expect(screen.getByRole("row", { name: /Family laptop/ })).toBeInTheDocument();
    expect(screen.getByText("Desktop")).toBeInTheDocument();
    expect(screen.getByText("Mobile")).toBeInTheDocument();
    expect(screen.getByText("android")).toBeInTheDocument();
    expect(screen.getByText("1.3.0")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    // "Revoked" names both a column and a status cell; both must render.
    expect(screen.getAllByText("Revoked").length).toBe(2);
    expect(screen.getByText("Not seen yet")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(container.querySelector('time[dateTime="2026-07-01T10:00:00Z"]')).not.toBeNull();
    expect(container.querySelector('time[dateTime="2026-07-15T12:00:00Z"]')).not.toBeNull();
    expect(screen.queryByText(/device_id/i)).toBeNull();
    expect(screen.queryByText(/credential/i)).toBeNull();
  });

  it("keeps revoked rows read-only and offers revoke only on active rows", async () => {
    server.use(
      mockApi("get", "/api/admin/devices", () =>
        adminDeviceListResponse([
          adminDeviceData({ device_name: "Family laptop" }),
          adminDeviceData({
            device_id: "11111111-2222-4333-8444-555555555557",
            device_name: "Old phone",
            status: "revoked",
            revoked_at: "2026-07-15T12:00:00Z",
          }),
        ]),
      ),
    );
    render(<DeviceList client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    expect(await screen.findByRole("button", { name: "Revoke Family laptop" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke Old phone" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
  });

  it("refreshes the list after a completed revocation", async () => {
    const user = userEvent.setup();
    const laptop = adminDeviceData({ device_name: "Family laptop" });
    let laptopRevoked = false;
    server.use(
      mockApi("get", "/api/admin/devices", () =>
        adminDeviceListResponse([
          laptopRevoked
            ? { ...laptop, status: "revoked", revoked_at: "2026-08-16T09:05:00Z" }
            : laptop,
        ]),
      ),
    );
    server.use(
      mockApi("post", `/api/admin/devices/${laptop.device_id}/revoke`, () => {
        laptopRevoked = true;
        return adminDeviceRevokeResponse(laptop.device_id);
      }),
    );
    render(<DeviceList client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    await user.click(await screen.findByRole("button", { name: "Revoke Family laptop" }));
    await user.type(
      await screen.findByLabelText("Type the device name to confirm"),
      "Family laptop",
    );
    await user.click(screen.getByRole("button", { name: "Revoke device" }));
    await waitFor(() =>
      expect(screen.getByRole("row", { name: /Family laptop/ }).textContent).toContain("Revoked"),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke Family laptop" })).not.toBeInTheDocument();
  });

  it("surfaces a load failure with an actionable message", async () => {
    server.use(mockApi("get", "/api/admin/devices", () => errorResponse("internal_error", 500)));
    render(<DeviceList client={createTestClient()} sessionStore={createAuthenticationSessionStore()} />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The device list could not be loaded. Try again.");
  });
});
