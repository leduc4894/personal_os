import { HttpResponse, type DefaultBodyType } from "msw";

import type { components } from "@workspace/api-client";

import { errorResponse } from "../../testing/api-mock-builders";

/**
 * Device-route MSW fixtures shared by the device feature component tests.
 * Shapes mirror the published API contract exactly: the approval-page grant
 * context (spec 11.3), the Admin device rows (spec 18.3) and the Admin
 * revoke decision (spec 14.1). No credential or polling identity appears.
 */

type DeviceGrantContextData = components["schemas"]["DeviceGrantContextData"];
type DeviceGrantDecisionData = components["schemas"]["DeviceGrantDecisionData"];
type AdminDeviceData = components["schemas"]["AdminDeviceData"];
type AdminDeviceRevokeData = components["schemas"]["AdminDeviceRevokeData"];

const REQUEST_ID = "5b34a3ca-8a30-4f6f-9b1e-1d2a1a1b9c11";

export const DEVICE_GRANT_ID = "7f2b3d4e-5f60-4a71-8a82-93a4b5c6d7e8";
export const DEVICE_USER_CODE = "BCDF-GHJK";
export const DEVICE_ID = "0d1e2f3a-4b5c-4d6e-8f90-a1b2c3d4e5f6";

function dataEnvelope(data: unknown): HttpResponse<DefaultBodyType> {
  return HttpResponse.json({ data, error: null, request_id: REQUEST_ID, warnings: [] });
}

/** One pending grant's display context with a ~10-minute remaining lifetime. */
export function deviceGrantContextData(
  overrides: Partial<DeviceGrantContextData> = {},
): DeviceGrantContextData {
  return {
    device_name: "Personal desktop",
    expires_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
    grant_id: DEVICE_GRANT_ID,
    platform_class: "obsidian_desktop",
    platform_name: "windows",
    plugin_version: "1.4.0",
    requested_scope: "obsidian_sync",
    user_code: DEVICE_USER_CODE,
    ...overrides,
  };
}

export function deviceGrantContextResponse(
  overrides: Partial<DeviceGrantContextData> = {},
): HttpResponse<DefaultBodyType> {
  return dataEnvelope(deviceGrantContextData(overrides));
}

export function deviceDecisionResponse(state: "approved" | "denied"): HttpResponse<DefaultBodyType> {
  const decision: DeviceGrantDecisionData = {
    decided_at: new Date().toISOString(),
    grant_id: DEVICE_GRANT_ID,
    state,
  };
  return dataEnvelope(decision);
}

export function adminDeviceData(overrides: Partial<AdminDeviceData> = {}): AdminDeviceData {
  return {
    device_id: DEVICE_ID,
    device_name: "Personal desktop",
    family_absolute_expires_at: "2027-02-16T09:00:00Z",
    last_seen_at: "2026-08-15T18:30:00Z",
    platform_class: "obsidian_desktop",
    platform_name: "windows",
    plugin_version: "1.4.0",
    registered_at: "2026-07-01T10:00:00Z",
    revoked_at: null,
    status: "active",
    ...overrides,
  };
}

export function adminDeviceListResponse(
  devices: readonly AdminDeviceData[],
): HttpResponse<DefaultBodyType> {
  return dataEnvelope({ devices });
}

export function adminDeviceRevokeResponse(deviceId: string = DEVICE_ID): HttpResponse<DefaultBodyType> {
  const revoked: AdminDeviceRevokeData = { device_id: deviceId, revoked_at: "2026-08-16T09:05:00Z" };
  return dataEnvelope(revoked);
}

/** The exact-name confirmation mismatch the Admin revoke route renders (14.1). */
export function confirmationInvalidResponse(): HttpResponse<DefaultBodyType> {
  return errorResponse("device_revocation_confirmation_invalid", 409);
}

/** The closed rejection of a checksum-invalid user code on lookup (11.3). */
export function deviceCredentialInvalidResponse(): HttpResponse<DefaultBodyType> {
  return errorResponse("device_credential_invalid", 401);
}
