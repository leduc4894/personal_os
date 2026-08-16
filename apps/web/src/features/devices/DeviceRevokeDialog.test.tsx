import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@workspace/api-client";

import { createNativeFetchTransport } from "../../api/native-fetch-transport";
import { readCsrfTokenFromCookieSource } from "../../api/authentication-client";
import {
  MOCK_API_BASE_URL,
  installMockCsrfCookie,
  mockApi,
  rateLimitedResponse,
  recentAuthenticationRequiredResponse,
  sessionResponse,
} from "../../testing/api-mock-builders";
import { createDeviceAdministrationClient } from "./device-administration-client";
import type { DeviceAdministrationClient } from "./device-administration-client";
import {
  DEVICE_ID,
  adminDeviceData,
  adminDeviceRevokeResponse,
  confirmationInvalidResponse,
} from "./device-api-fixtures";
import { DeviceRevokeDialog } from "./DeviceRevokeDialog";

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

function renderDialog(overrides: { onClosed?: () => void; onRevoked?: () => void } = {}) {
  const onClosed = overrides.onClosed ?? vi.fn();
  const onRevoked = overrides.onRevoked ?? vi.fn();
  const device = adminDeviceData({ device_name: "Family laptop" });
  render(
    <DeviceRevokeDialog
      client={createTestClient()}
      device={device}
      onClosed={onClosed}
      onRevoked={onRevoked}
    />,
  );
  return { onClosed, onRevoked, device };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  installMockCsrfCookie();
});
afterEach(() => {
  server.resetHandlers();
  document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});
afterAll(() => server.close());

describe("DeviceRevokeDialog", () => {
  it("blocks the revoke call until the confirmation matches the exact name", async () => {
    const user = userEvent.setup();
    const seenBodies: string[] = [];
    server.use(
      mockApi("post", `/api/admin/devices/${DEVICE_ID}/revoke`, async ({ request }) => {
        seenBodies.push(await request.text());
        return adminDeviceRevokeResponse();
      }),
    );
    const { onRevoked } = renderDialog();
    const submitButton = screen.getByRole("button", { name: "Revoke device" });
    expect(submitButton).toBeDisabled();
    await user.type(screen.getByLabelText("Type the device name to confirm"), "Family lapto");
    expect(submitButton).toBeDisabled();
    await user.type(screen.getByLabelText("Type the device name to confirm"), "p");
    expect(submitButton).toBeEnabled();
    await user.click(submitButton);
    await screen.findByRole("heading", { name: "Revoke device" });
    expect(seenBodies).toEqual(['{"device_name_confirmation":"Family laptop"}']);
    expect(onRevoked).toHaveBeenCalled();
  });

  it("handles a name-confirmation conflict inline without closing", async () => {
    const user = userEvent.setup();
    server.use(
      mockApi("post", `/api/admin/devices/${DEVICE_ID}/revoke`, () => confirmationInvalidResponse()),
    );
    const { onRevoked } = renderDialog();
    await user.type(screen.getByLabelText("Type the device name to confirm"), "Family laptop");
    await user.click(screen.getByRole("button", { name: "Revoke device" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The device name did not match. Check the exact name and try again.");
    expect(onRevoked).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("offers recent re-authentication on 403 and retries the revoke after it", async () => {
    const user = userEvent.setup();
    let revokeCalls = 0;
    const revokeCsrfHeaders: (string | null)[] = [];
    server.use(
      mockApi("post", `/api/admin/devices/${DEVICE_ID}/revoke`, async ({ request }) => {
        revokeCalls += 1;
        revokeCsrfHeaders.push(request.headers.get("x-csrf-token"));
        return revokeCalls === 1 ? recentAuthenticationRequiredResponse() : adminDeviceRevokeResponse();
      }),
    );
    server.use(mockApi("post", "/api/auth/reauthenticate", () => sessionResponse("active")));
    const { onRevoked } = renderDialog();
    await user.type(screen.getByLabelText("Type the device name to confirm"), "Family laptop");
    await user.click(screen.getByRole("button", { name: "Revoke device" }));
    expect(
      await screen.findByText("Confirm your password again to revoke this device."),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("Current password"), "correct horse battery staple!");
    await user.click(screen.getByRole("button", { name: "Confirm password" }));
    await screen.findByRole("heading", { name: "Revoke device" });
    expect(revokeCalls).toBe(2);
    expect(onRevoked).toHaveBeenCalledTimes(1);
    expect(revokeCsrfHeaders.every((value) => value === "csrf-round-trip-token")).toBe(true);
  });

  it("shows bounded retry guidance when the revoke throttle is active", async () => {
    const user = userEvent.setup();
    server.use(
      mockApi("post", `/api/admin/devices/${DEVICE_ID}/revoke`, () => rateLimitedResponse(540)),
    );
    renderDialog();
    await user.type(screen.getByLabelText("Type the device name to confirm"), "Family laptop");
    await user.click(screen.getByRole("button", { name: "Revoke device" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Too many attempts. Try again in 9 minutes.");
    expect(alert.textContent).not.toContain("540");
  });

  it("closes on cancel and on escape", async () => {
    const user = userEvent.setup();
    const { onClosed } = renderDialog();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClosed).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClosed).toHaveBeenCalledTimes(2);
  });
});
