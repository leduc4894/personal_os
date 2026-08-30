import {
  createApiClient,
  type ApiClient,
  type components,
} from "@workspace/api-client";

import { createNativeFetchTransport } from "../../api/native-fetch-transport";
import {
  CSRF_HEADER_NAME,
  REQUEST_UNAVAILABLE_ERROR,
  createAuthenticationClient,
  readCsrfTokenFromCookieSource,
  unwrapEnvelope,
  type AuthenticationCallResult,
  type AuthenticationClient,
} from "../../api/authentication-client";

export type DeviceGrantContextData = components["schemas"]["DeviceGrantContextData"];
export type DeviceGrantDecisionData = components["schemas"]["DeviceGrantDecisionData"];
export type AdminDeviceData = components["schemas"]["AdminDeviceData"];
export type AdminDeviceListData = components["schemas"]["AdminDeviceListData"];
export type AdminDeviceRevokeData = components["schemas"]["AdminDeviceRevokeData"];

/**
 * The device surfaces' client: every interactive-login method of
 * {@link AuthenticationClient} plus the browser device-authorization routes
 * (spec 11.2/11.3) and the Admin device routes (spec 14.1, 18.3). Results
 * carry the route's data payload or its registered error, never a raw status.
 */
export interface DeviceAdministrationClient extends AuthenticationClient {
  lookupDeviceAuthorization(input: {
    userCode: string;
  }): Promise<AuthenticationCallResult<DeviceGrantContextData>>;
  approveDeviceAuthorization(input: {
    grantId: string;
  }): Promise<AuthenticationCallResult<DeviceGrantDecisionData>>;
  denyDeviceAuthorization(input: {
    grantId: string;
  }): Promise<AuthenticationCallResult<DeviceGrantDecisionData>>;
  listAdminDevices(): Promise<AuthenticationCallResult<AdminDeviceListData>>;
  revokeAdminDevice(input: {
    deviceId: string;
    deviceNameConfirmation: string;
  }): Promise<AuthenticationCallResult<AdminDeviceRevokeData>>;
}

export function createDeviceAdministrationClient(options: {
  apiClient: ApiClient;
  /** Reads the CSRF cookie at request time; never persisted between requests. */
  readCsrfToken: () => string | null;
}): DeviceAdministrationClient {
  const { apiClient, readCsrfToken } = options;
  const authentication = createAuthenticationClient(options);

  function csrfHeaders(): Record<string, string> {
    const token = readCsrfToken();
    return token === null ? {} : { [CSRF_HEADER_NAME]: token };
  }

  async function call<T>(
    request: () => Promise<{ data?: unknown; error?: unknown }>,
  ): Promise<AuthenticationCallResult<T>> {
    try {
      return unwrapEnvelope<T>(await request());
    } catch {
      return { ok: false, error: REQUEST_UNAVAILABLE_ERROR };
    }
  }

  return {
    ...authentication,
    lookupDeviceAuthorization({ userCode }) {
      return call(() =>
        apiClient.POST("/api/auth/device-authorizations/lookup", {
          body: { user_code: userCode },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    approveDeviceAuthorization({ grantId }) {
      return call(() =>
        apiClient.POST("/api/auth/device-authorizations/{grant_id}/approve", {
          params: { path: { grant_id: grantId } },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    denyDeviceAuthorization({ grantId }) {
      return call(() =>
        apiClient.POST("/api/auth/device-authorizations/{grant_id}/deny", {
          params: { path: { grant_id: grantId } },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    listAdminDevices() {
      return call(() => apiClient.GET("/api/admin/devices", { credentials: "include" }));
    },
    revokeAdminDevice({ deviceId, deviceNameConfirmation }) {
      return call(() =>
        apiClient.POST("/api/admin/devices/{device_id}/revoke", {
          params: { path: { device_id: deviceId } },
          body: { device_name_confirmation: deviceNameConfirmation },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
  };
}

const BROWSER_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

let cachedBrowserDeviceClient: DeviceAdministrationClient | null = null;

/**
 * Builds the client the browser device pages use: same-origin by default and
 * memoized so React default props keep one stable identity across renders.
 */
export function createBrowserDeviceAdministrationClient(): DeviceAdministrationClient {
  cachedBrowserDeviceClient ??= createDeviceAdministrationClient({
    apiClient: createApiClient({
      baseUrl: BROWSER_API_BASE_URL,
      transport: createNativeFetchTransport(),
    }),
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
  return cachedBrowserDeviceClient;
}
