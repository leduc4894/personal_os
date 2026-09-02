import { describe, expect, it, vi } from "vitest";

import {
  CONNECTION_STATES,
  CONNECTION_STATUS_TEXT,
  createDeviceApiTransport,
  DeviceAuthError,
  parseServerOrigin,
  resolveAuthenticationControls,
  validateDeviceName,
} from "./contracts";
import type { DeviceHttpResponse, DeviceHttpTransport } from "./contracts";

function httpResponse(status: number, body: unknown): DeviceHttpResponse {
  return { status, bodyText: typeof body === "string" ? body : JSON.stringify(body) };
}

function successEnvelope(data: unknown): DeviceHttpResponse {
  return httpResponse(200, { data, error: null, request_id: "0d9f6c0e-0000-4000-8000-000000000001", warnings: [] });
}

function errorEnvelope(
  code: string,
  status: number,
  details: Record<string, unknown> = {},
): DeviceHttpResponse {
  return httpResponse(status, {
    data: null,
    error: { code, message: "safe registered message", details, retryable: status === 429 },
    request_id: "0d9f6c0e-0000-4000-8000-000000000002",
    warnings: [],
  });
}

function createGrantData(): Record<string, unknown> {
  return {
    grant_id: "6e5cb1a2-0000-4000-8000-00000000000a",
    user_code: "ABCD-EFGH",
    polling_secret: "pg1.6e5cb1a2-0000-4000-8000-00000000000a.polling-secret",
    verification_uri: "https://vault.example.com/device/approve",
    verification_uri_complete: "https://vault.example.com/device/approve#ABCD-EFGH",
    expires_in_seconds: 600,
    poll_interval_seconds: 5,
  };
}

describe("ConnectionState closed set (spec 19)", () => {
  it("is exactly the eight spec-19 states in no leaking extra form", () => {
    expect([...CONNECTION_STATES]).toEqual([
      "not_connected",
      "requesting_authorization",
      "waiting_for_approval",
      "connected",
      "offline",
      "refresh_required",
      "revoked",
      "configuration_invalid",
    ]);
  });

  it("renders one closed status text per state", () => {
    for (const state of CONNECTION_STATES) {
      expect(typeof CONNECTION_STATUS_TEXT[state]).toBe("string");
      expect(CONNECTION_STATUS_TEXT[state].length).toBeGreaterThan(0);
    }
  });

  it("derives the settings-tab controls from credential facts", () => {
    expect(
      resolveAuthenticationControls("not_connected", {
        hasPendingGrant: false,
        hasActiveCredential: false,
      }),
    ).toEqual({
      canLogin: true,
      canRetryConnection: false,
      canOpenBrowser: false,
      canCancel: false,
      canDisconnect: false,
    });

    expect(
      resolveAuthenticationControls("waiting_for_approval", {
        hasPendingGrant: true,
        hasActiveCredential: false,
      }),
    ).toEqual({
      canLogin: false,
      canRetryConnection: false,
      canOpenBrowser: true,
      canCancel: true,
      canDisconnect: false,
    });

    expect(
      resolveAuthenticationControls("connected", {
        hasPendingGrant: false,
        hasActiveCredential: true,
      }),
    ).toEqual({
      canLogin: false,
      canRetryConnection: false,
      canOpenBrowser: false,
      canCancel: false,
      canDisconnect: true,
    });

    expect(
      resolveAuthenticationControls("offline", {
        hasPendingGrant: true,
        hasActiveCredential: true,
      }),
    ).toEqual({
      canLogin: false,
      canRetryConnection: true,
      canOpenBrowser: true,
      canCancel: true,
      canDisconnect: true,
    });

    expect(
      resolveAuthenticationControls("revoked", {
        hasPendingGrant: false,
        hasActiveCredential: false,
      }),
    ).toEqual({
      canLogin: true,
      canRetryConnection: false,
      canOpenBrowser: false,
      canCancel: false,
      canDisconnect: false,
    });

    expect(
      resolveAuthenticationControls("refresh_required", {
        hasPendingGrant: false,
        hasActiveCredential: false,
      }),
    ).toEqual({
      canLogin: true,
      canRetryConnection: false,
      canOpenBrowser: false,
      canCancel: false,
      canDisconnect: false,
    });
  });

  it("enables the retry affordance only while offline with an active credential", () => {
    // Plugin hygiene (2026-08-16 §12): the offline dead-end (offline state
    // with a live credential) is the one state that gains the retry
    // affordance; `canLogin` keeps its own unchanged gating.
    expect(
      resolveAuthenticationControls("offline", {
        hasPendingGrant: false,
        hasActiveCredential: true,
      }).canRetryConnection,
    ).toBe(true);

    expect(
      resolveAuthenticationControls("offline", {
        hasPendingGrant: false,
        hasActiveCredential: false,
      }).canRetryConnection,
    ).toBe(false);

    expect(
      resolveAuthenticationControls("refresh_required", {
        hasPendingGrant: false,
        hasActiveCredential: true,
      }).canRetryConnection,
    ).toBe(false);

    expect(
      resolveAuthenticationControls("connected", {
        hasPendingGrant: false,
        hasActiveCredential: true,
      }).canRetryConnection,
    ).toBe(false);
  });
});

describe("parseServerOrigin", () => {
  it("accepts an exact HTTPS origin and normalizes it", () => {
    expect(parseServerOrigin("https://vault.example.com", { allowLoopbackHttp: false })).toBe(
      "https://vault.example.com",
    );
    expect(parseServerOrigin(" https://vault.example.com/ ", { allowLoopbackHttp: false })).toBe(
      "https://vault.example.com",
    );
    expect(parseServerOrigin("https://vault.example.com:8443", { allowLoopbackHttp: false })).toBe(
      "https://vault.example.com:8443",
    );
  });

  it("rejects origins with a path, query, fragment or embedded credential", () => {
    for (const invalidOrigin of [
      "https://vault.example.com/device",
      "https://vault.example.com?x=1",
      "https://vault.example.com#fragment",
      "https://user:pass@vault.example.com",
      "https://user@vault.example.com",
      "ftp://vault.example.com",
      "not a url",
      "",
    ]) {
      expect(parseServerOrigin(invalidOrigin, { allowLoopbackHttp: false })).toBeNull();
    }
  });

  it("rejects plain HTTP in production builds", () => {
    expect(parseServerOrigin("http://vault.example.com", { allowLoopbackHttp: false })).toBeNull();
    expect(parseServerOrigin("http://127.0.0.1:8000", { allowLoopbackHttp: false })).toBeNull();
  });

  it("allows loopback HTTP only in explicit development builds", () => {
    expect(parseServerOrigin("http://127.0.0.1:8000", { allowLoopbackHttp: true })).toBe(
      "http://127.0.0.1:8000",
    );
    expect(parseServerOrigin("http://localhost:3000", { allowLoopbackHttp: true })).toBe(
      "http://localhost:3000",
    );
    expect(parseServerOrigin("http://[::1]:9000", { allowLoopbackHttp: true })).toBe(
      "http://[::1]:9000",
    );
    expect(parseServerOrigin("http://127.66.0.9:8000", { allowLoopbackHttp: true })).toBe(
      "http://127.66.0.9:8000",
    );
    expect(parseServerOrigin("http://vault.example.com", { allowLoopbackHttp: true })).toBeNull();
    expect(parseServerOrigin("http://192.168.1.4:8000", { allowLoopbackHttp: true })).toBeNull();
  });
});

describe("validateDeviceName", () => {
  it("trims and accepts 1-80 display characters", () => {
    expect(validateDeviceName("  Personal vault  ")).toBe("Personal vault");
    expect(validateDeviceName("x")).toBe("x");
    expect(validateDeviceName("a".repeat(80))).toBe("a".repeat(80));
  });

  it("rejects empty and oversized names", () => {
    expect(validateDeviceName("   ")).toBeNull();
    expect(validateDeviceName("a".repeat(81))).toBeNull();
  });
});

describe("createDeviceApiTransport", () => {
  it("posts the exact grant-creation body and unwraps the envelope data", async () => {
    const calls: unknown[] = [];
    const http: DeviceHttpTransport = async (request) => {
      calls.push(request);
      return successEnvelope(createGrantData());
    };
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    const grant = await transport.createGrant({
      client_instance_id: "11111111-1111-4111-8111-111111111111",
      device_name: "Personal vault",
      platform_class: "obsidian_desktop",
      platform_name: "windows",
      plugin_version: "0.1.0",
      requested_scope: "obsidian_sync",
    });
    expect(grant.user_code).toBe("ABCD-EFGH");
    expect(calls[0]).toEqual({
      url: "https://vault.example.com/api/auth/device-authorizations",
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({
        client_instance_id: "11111111-1111-4111-8111-111111111111",
        device_name: "Personal vault",
        platform_class: "obsidian_desktop",
        platform_name: "windows",
        plugin_version: "0.1.0",
        requested_scope: "obsidian_sync",
      }),
    });
  });

  it("targets the configured public origin in the URL and sends no origin header", async () => {
    // Grant creation is a native Obsidian request, not a browser fetch: the
    // request URL itself targets the configured public origin and no Origin
    // header is forged — the browser security boundary begins at the
    // server-minted verification URL.
    const calls: unknown[] = [];
    const http: DeviceHttpTransport = async (request) => {
      calls.push(request);
      return successEnvelope(createGrantData());
    };
    const transport = createDeviceApiTransport(http, () => "https://workspace.example");

    await transport.createGrant({
      client_instance_id: "11111111-1111-4111-8111-111111111111",
      device_name: "Personal vault",
      platform_class: "obsidian_desktop",
      platform_name: "windows",
      plugin_version: "0.1.0",
      requested_scope: "obsidian_sync",
    });

    expect(calls).toEqual([
      expect.objectContaining({
        url: "https://workspace.example/api/auth/device-authorizations",
        headers: expect.not.objectContaining({ origin: expect.anything() }),
      }),
    ]);
  });

  it("presents the polling secret as the dedicated Bearer credential", async () => {
    const calls: unknown[] = [];
    const http: DeviceHttpTransport = async (request) => {
      calls.push(request);
      return successEnvelope({
        grant_id: "6e5cb1a2-0000-4000-8000-00000000000a",
        device_id: "77777777-7777-4777-8777-777777777777",
        token_family_id: "88888888-8888-4888-8888-888888888888",
        refresh_generation: 1,
        access_credential: "at1.access.secret",
        refresh_credential: "rt1.refresh.secret",
        access_expires_at: "2026-08-16T10:15:00Z",
        refresh_expires_at: "2026-09-15T10:10:00Z",
      });
    };
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    await transport.pollGrant(
      "6e5cb1a2-0000-4000-8000-00000000000a",
      "pg1.polling.secret",
    );
    expect(calls[0]).toEqual({
      url: "https://vault.example.com/api/auth/device-authorizations/6e5cb1a2-0000-4000-8000-00000000000a/poll",
      method: "POST",
      headers: {
        authorization: "Bearer pg1.polling.secret",
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({}),
    });
  });

  it("sends the rotation identity body with the refresh Bearer credential", async () => {
    const calls: unknown[] = [];
    const http: DeviceHttpTransport = async (request) => {
      calls.push(request);
      return successEnvelope({
        token_family_id: "88888888-8888-4888-8888-888888888888",
        refresh_generation: 2,
        access_credential: "at1.next.access",
        refresh_credential: "rt1.next.refresh",
        access_expires_at: "2026-08-16T10:30:00Z",
        refresh_expires_at: "2026-09-15T10:10:00Z",
        family_absolute_expires_at: "2026-11-14T10:10:00Z",
      });
    };
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    await transport.refresh("rt1.refresh.secret", "22222222-2222-4222-8222-222222222222");
    expect(calls[0]).toEqual({
      url: "https://vault.example.com/api/auth/device-tokens/refresh",
      method: "POST",
      headers: {
        authorization: "Bearer rt1.refresh.secret",
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({ rotation_id: "22222222-2222-4222-8222-222222222222" }),
    });
  });

  it("revokes with the refresh credential and no request body", async () => {
    const calls: unknown[] = [];
    const http: DeviceHttpTransport = async (request) => {
      calls.push(request);
      return successEnvelope({
        device_id: "77777777-7777-4777-8777-777777777777",
        token_family_id: "88888888-8888-4888-8888-888888888888",
        revoked_at: "2026-08-16T10:10:00Z",
      });
    };
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    await transport.revokeCurrent("rt1.refresh.secret");
    expect(calls[0]).toEqual({
      url: "https://vault.example.com/api/auth/device-tokens/revoke-current",
      method: "POST",
      headers: {
        authorization: "Bearer rt1.refresh.secret",
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({}),
    });
  });

  it("maps the pending envelope to a retryable error with the server hint", async () => {
    const http: DeviceHttpTransport = async () =>
      errorEnvelope("device_authorization_pending", 409, { retry_after_seconds: 7 });
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    const failure = await transport.pollGrant("grant-id", "pg1.polling.secret").catch((error) => error);
    expect(failure).toBeInstanceOf(DeviceAuthError);
    expect(failure).toMatchObject({
      code: "device_authorization_pending",
      status: 409,
      retryAfterSeconds: 7,
    });
  });

  it("maps the slow-down envelope to its new interval", async () => {
    const http: DeviceHttpTransport = async () =>
      errorEnvelope("device_authorization_slow_down", 429, { retry_after_seconds: 9 });
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    const failure = await transport.pollGrant("grant-id", "pg1.polling.secret").catch((error) => error);
    expect(failure).toMatchObject({
      code: "device_authorization_slow_down",
      status: 429,
      retryAfterSeconds: 9,
    });
  });

  it("maps the version-bounds detail of the 426 unsupported response", async () => {
    const http: DeviceHttpTransport = async () =>
      errorEnvelope("plugin_version_unsupported", 426, {
        approved_version_bounds: ["0.1.0", "2.0.0"],
      });
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    const failure = await transport.createGrant({
      client_instance_id: "11111111-1111-4111-8111-111111111111",
      device_name: "Personal vault",
      platform_class: "obsidian_desktop",
      platform_name: "windows",
      plugin_version: "9.9.9",
      requested_scope: "obsidian_sync",
    }).catch((error) => error);
    expect(failure).toMatchObject({
      code: "plugin_version_unsupported",
      status: 426,
      approvedVersionBounds: { minimum: "0.1.0", maximum: "2.0.0" },
    });
  });

  it("classifies unparseable bodies as malformed without echoing them", async () => {
    const http: DeviceHttpTransport = async () => httpResponse(502, "<html>gateway</html>");
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    const failure = await transport.revokeCurrent("rt1.refresh.secret").catch((error) => error);
    expect(failure).toMatchObject({ code: "api_request_malformed", status: 502 });
    expect(String(failure.message)).not.toContain("gateway");
  });

  it("classifies transport failures as local network unavailability", async () => {
    const http: DeviceHttpTransport = async () => {
      throw new TypeError("fetch failed");
    };
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    const failure = await transport.refresh("rt1.refresh.secret", "rotation").catch((error) => error);
    expect(failure).toMatchObject({ code: "network_unavailable", status: 0, isLocal: true });
  });

  it("never logs request or response content on any path", async () => {
    const consoleSpies = [
      vi.spyOn(console, "log"),
      vi.spyOn(console, "info"),
      vi.spyOn(console, "debug"),
      vi.spyOn(console, "warn"),
      vi.spyOn(console, "error"),
    ];
    const http: DeviceHttpTransport = async () =>
      errorEnvelope("device_token_reuse_detected", 401);
    const transport = createDeviceApiTransport(http, () => "https://vault.example.com");
    await transport.refresh("rt1.refresh.secret", "rotation").catch(() => undefined);
    for (const spy of consoleSpies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });
});
