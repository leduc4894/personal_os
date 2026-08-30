import { HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { createApiClient } from "@workspace/api-client";

import { createNativeFetchTransport } from "../../api/native-fetch-transport";
import {
  REQUEST_UNAVAILABLE_ERROR,
  readCsrfTokenFromCookieSource,
} from "../../api/authentication-client";
import { MOCK_API_BASE_URL, mockApi } from "../../testing/api-mock-builders";
import { createDeviceAdministrationClient } from "./device-administration-client";
import { deviceGrantContextResponse } from "./device-api-fixtures";

const server = setupServer();

function createTestClient() {
  return createDeviceAdministrationClient({
    apiClient: createApiClient({
      baseUrl: MOCK_API_BASE_URL,
      transport: createNativeFetchTransport(globalThis.fetch),
    }),
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("createDeviceAdministrationClient envelope handling", () => {
  it("unwraps a route's data payload through the shared envelope helper", async () => {
    server.use(
      mockApi("post", "/api/auth/device-authorizations/lookup", () => deviceGrantContextResponse()),
    );
    const result = await createTestClient().lookupDeviceAuthorization({ userCode: "BCDF-GHJK" });
    expect(result).toMatchObject({ ok: true, data: { device_name: "Personal desktop" } });
  });

  it("closes transport failures with the shared unavailable error body", async () => {
    server.use(
      mockApi("post", "/api/auth/device-authorizations/lookup", () =>
        new HttpResponse("gateway timeout", { status: 504 }),
      ),
    );
    const result = await createTestClient().lookupDeviceAuthorization({ userCode: "BCDF-GHJK" });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      // Identity with the authentication client's exported constant proves the
      // device surface reuses the shared transport-closure body.
      expect(result.error).toBe(REQUEST_UNAVAILABLE_ERROR);
    }
  });
});
