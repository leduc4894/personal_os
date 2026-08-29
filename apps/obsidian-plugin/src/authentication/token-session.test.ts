import { describe, expect, it, vi } from "vitest";

import { DeviceAuthError } from "./contracts";
import type {
  DeviceAuthenticationSettings,
  SecretStorageRecordAdapter,
} from "./contracts";
import { DEVICE_CREDENTIAL_RECORD_NAME } from "./secret-storage-record";
import { DeviceTokenSession, resolveStartupAction } from "./token-session";

const REFRESH_CREDENTIAL = "rt1.33333333-3333-4333-8333-333333333333.refresh-secret";
const NEXT_REFRESH_CREDENTIAL = "rt1.44444444-4444-4444-8444-444444444444.refresh-secret";
const ACCESS_CREDENTIAL = "at1.22222222-2222-4222-8222-222222222222.access-secret";

const SUCCESSOR = {
  token_family_id: "88888888-8888-4888-8888-888888888888",
  refresh_generation: 2,
  access_credential: ACCESS_CREDENTIAL,
  refresh_credential: NEXT_REFRESH_CREDENTIAL,
  access_expires_at: "2026-08-16T10:30:00Z",
  refresh_expires_at: "2026-09-15T10:10:00Z",
  family_absolute_expires_at: "2026-11-14T10:10:00Z",
};

function deviceTokenReuseError(): DeviceAuthError {
  return new DeviceAuthError("device_token_reuse_detected", {
    status: 401,
    message: "reuse detected",
  });
}

interface SessionHarness {
  session: DeviceTokenSession;
  transport: {
    createGrant: ReturnType<typeof vi.fn>;
    pollGrant: ReturnType<typeof vi.fn>;
    refresh: ReturnType<typeof vi.fn>;
    revokeCurrent: ReturnType<typeof vi.fn>;
  };
  secretStorage: SecretStorageRecordAdapter;
  stored: Map<string, string>;
  settings: DeviceAuthenticationSettings;
  persistSettings: ReturnType<typeof vi.fn>;
  states: string[];
  stateDetails: (string | null)[];
}

function createSessionHarness(
  initialRecord: Record<string, unknown> | null = {
    record_version: 1,
    state: "active",
    refresh_credential: REFRESH_CREDENTIAL,
    refresh_generation: 1,
    pending_rotation_id: null,
  },
): SessionHarness {
  const stored = new Map<string, string>();
  if (initialRecord !== null) {
    stored.set(DEVICE_CREDENTIAL_RECORD_NAME, JSON.stringify(initialRecord));
  }
  const secretStorage: SecretStorageRecordAdapter = {
    setSecret: vi.fn((recordName: string, value: string) => {
      stored.set(recordName, value);
    }),
    getSecret: vi.fn((recordName: string) => stored.get(recordName) ?? null),
  };
  const transport = {
    createGrant: vi.fn(),
    pollGrant: vi.fn(),
    refresh: vi.fn(),
    revokeCurrent: vi.fn(),
  };
  const settings: DeviceAuthenticationSettings = {
    server_origin: "https://vault.example.com",
    device_name: "Personal vault",
    client_instance_id: "11111111-1111-4111-8111-111111111111",
    device_id: null,
    secret_record_name: initialRecord === null ? null : DEVICE_CREDENTIAL_RECORD_NAME,
    pending_grant: null,
  };
  const persistSettings = vi.fn(async () => undefined);
  const states: string[] = [];
  const stateDetails: (string | null)[] = [];
  const session = new DeviceTokenSession({
    transport,
    secretStore: secretStorage,
    recordName: DEVICE_CREDENTIAL_RECORD_NAME,
    settings,
    persistSettings,
    createRotationId: () => "22222222-2222-4222-8222-222222222222",
    onStateChange: (state, detail) => {
      states.push(state);
      stateDetails.push(detail);
    },
  });
  return { session, transport, secretStorage, stored, settings, persistSettings, states, stateDetails };
}

function lastStoredJson(harness: SessionHarness): Record<string, unknown> {
  const calls = (harness.secretStorage.setSecret as ReturnType<typeof vi.fn>).mock.calls;
  const lastCall = calls[calls.length - 1];
  if (lastCall === undefined) {
    throw new Error("setSecret was never called");
  }
  return JSON.parse(String(lastCall[1])) as Record<string, unknown>;
}

describe("DeviceTokenSession crash-safe refresh (spec 13.3)", () => {
  it("writes and reads back rotation identity before refresh", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockResolvedValue(SUCCESSOR);

    await harness.session.refresh();

    expect(harness.secretStorage.setSecret).toHaveBeenCalledBefore(harness.transport.refresh);
    expect(harness.secretStorage.getSecret).toHaveReturnedWith(
      expect.stringContaining("pending_rotation_id"),
    );
  });

  it("terminal reuse writes a credential-free tombstone", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockRejectedValue(deviceTokenReuseError());

    await expect(harness.session.refresh()).rejects.toMatchObject({
      code: "device_token_reuse_detected",
    });
    expect(lastStoredJson(harness)).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "token_reuse",
    });
  });

  it("persists the full successor record after the response", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockResolvedValue(SUCCESSOR);

    await harness.session.refresh();

    expect(lastStoredJson(harness)).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: NEXT_REFRESH_CREDENTIAL,
      refresh_generation: 2,
      pending_rotation_id: null,
    });
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: NEXT_REFRESH_CREDENTIAL,
      refresh_generation: 2,
      pending_rotation_id: null,
    });
    expect(harness.session.accessCredential).toBe(ACCESS_CREDENTIAL);
    expect(harness.states).toEqual(["connected"]);
  });

  it("never persists the access credential in SecretStorage", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockResolvedValue(SUCCESSOR);

    await harness.session.refresh();

    for (const value of harness.stored.values()) {
      expect(value).not.toContain("at1.");
      expect(value).not.toContain(ACCESS_CREDENTIAL);
    }
  });

  it("reuses the stored pending rotation identity after a crash", async () => {
    const harness = createSessionHarness({
      record_version: 1,
      state: "active",
      refresh_credential: REFRESH_CREDENTIAL,
      refresh_generation: 1,
      pending_rotation_id: "99999999-9999-4999-8999-999999999999",
    });
    harness.transport.refresh.mockResolvedValue(SUCCESSOR);

    await harness.session.refresh();

    expect(harness.transport.refresh).toHaveBeenCalledWith(
      REFRESH_CREDENTIAL,
      "99999999-9999-4999-8999-999999999999",
    );
    expect(lastStoredJson(harness)).toMatchObject({ pending_rotation_id: null });
  });

  it("preserves the record on a network failure and reports offline", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockRejectedValue(
      new DeviceAuthError("network_unavailable", {
        status: 0,
        message: "offline",
        isLocal: true,
      }),
    );

    await expect(harness.session.refresh()).rejects.toMatchObject({
      code: "network_unavailable",
    });
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: REFRESH_CREDENTIAL,
      refresh_generation: 1,
      pending_rotation_id: "22222222-2222-4222-8222-222222222222",
    });
    expect(harness.states).toEqual(["offline"]);
  });

  it("tombstones a server-side revocation", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockRejectedValue(
      new DeviceAuthError("device_revoked", { status: 401, message: "revoked" }),
    );

    await expect(harness.session.refresh()).rejects.toMatchObject({ code: "device_revoked" });
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "device_revoked",
    });
    expect(harness.settings.secret_record_name).toBeNull();
    expect(harness.states).toEqual(["revoked"]);
  });

  it("reports refresh_required for transient server failures while preserving records", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockRejectedValue(
      new DeviceAuthError("database_connection_unavailable", {
        status: 503,
        message: "unavailable",
      }),
    );

    await expect(harness.session.refresh()).rejects.toMatchObject({
      code: "database_connection_unavailable",
    });
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: REFRESH_CREDENTIAL,
      refresh_generation: 1,
      pending_rotation_id: "22222222-2222-4222-8222-222222222222",
    });
    expect(harness.states).toEqual(["refresh_required"]);
  });
});

describe("DeviceTokenSession self-disconnect (spec 14.2)", () => {
  it("revokes on the server before writing the local tombstone", async () => {
    const harness = createSessionHarness();
    harness.transport.revokeCurrent.mockResolvedValue({
      device_id: "77777777-7777-4777-8777-777777777777",
      token_family_id: "88888888-8888-4888-8888-888888888888",
      revoked_at: "2026-08-16T10:10:00Z",
    });
    await harness.session.refresh().catch(() => undefined);
    harness.transport.refresh.mockClear();
    (harness.secretStorage.setSecret as ReturnType<typeof vi.fn>).mockClear();

    await harness.session.disconnect();

    expect(harness.transport.revokeCurrent).toHaveBeenCalledWith(REFRESH_CREDENTIAL);
    expect(harness.secretStorage.setSecret).toHaveBeenCalledAfter(
      harness.transport.revokeCurrent,
    );
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "self_disconnect",
    });
    expect(harness.settings.secret_record_name).toBeNull();
    expect(harness.session.accessCredential).toBeNull();
    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
  });

  it("does not clear local state when the secure revoke cannot complete", async () => {
    const harness = createSessionHarness();
    harness.transport.revokeCurrent.mockRejectedValue(
      new DeviceAuthError("network_unavailable", {
        status: 0,
        message: "offline",
        isLocal: true,
      }),
    );

    await expect(harness.session.disconnect()).rejects.toMatchObject({
      code: "network_unavailable",
    });
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: REFRESH_CREDENTIAL,
      refresh_generation: 1,
      pending_rotation_id: null,
    });
    expect(harness.settings.secret_record_name).toBe(DEVICE_CREDENTIAL_RECORD_NAME);
    expect(harness.states[harness.states.length - 1]).toBe("offline");
  });

  it("clears an already-invalid credential when self-revoke reports it", async () => {
    const harness = createSessionHarness();
    harness.transport.revokeCurrent.mockRejectedValue(
      new DeviceAuthError("device_credential_invalid", {
        status: 401,
        message: "credential is no longer valid",
      }),
    );

    await harness.session.disconnect();

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "self_disconnect",
    });
    expect(harness.settings.secret_record_name).toBeNull();
    expect(harness.session.accessCredential).toBeNull();
    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
  });
});

describe("DeviceTokenSession concurrent refresh single-flight (bare-reload race)", () => {
  it("joins an in-flight refresh instead of rotating twice", async () => {
    // A bare plugin reload fires the startup refresh fire-and-forget while
    // the queue pass's own login-verdict refresh (fix round 4) may arrive
    // concurrently. Two independent rotations on one refresh credential
    // can trip server-side reuse detection and tombstone a healthy
    // credential; the session must join the in-flight promise instead.
    const harness = createSessionHarness();
    let releaseFirstRefresh: ((value: typeof SUCCESSOR) => void) | null = null;
    harness.transport.refresh.mockImplementationOnce(
      () =>
        new Promise<typeof SUCCESSOR>((resolve) => {
          releaseFirstRefresh = resolve;
        }),
    );
    harness.transport.refresh.mockImplementationOnce(async () => ({
      ...SUCCESSOR,
      refresh_credential: NEXT_REFRESH_CREDENTIAL,
    }));

    const startupRefresh = harness.session.refresh();
    const queueRefresh = harness.session.refresh();
    expect(harness.transport.refresh).toHaveBeenCalledTimes(1);
    releaseFirstRefresh?.(SUCCESSOR);
    await Promise.all([startupRefresh, queueRefresh]);

    expect(harness.transport.refresh).toHaveBeenCalledTimes(1);
    expect(harness.transport.refresh).toHaveBeenCalledWith(
      REFRESH_CREDENTIAL,
      "22222222-2222-4222-8222-222222222222",
    );
    expect(harness.session.accessCredential).toBe(ACCESS_CREDENTIAL);
    const record = lastStoredJson(harness);
    expect(record.state).toBe("active");
    expect(record.pending_rotation_id).toBeNull();
    // No reuse tombstone, no cleared record, exactly one connected state.
    expect(harness.settings.secret_record_name).not.toBeNull();
    expect(harness.states.filter((state) => state === "connected")).toHaveLength(1);
  });

  it("starts a fresh rotation after the in-flight refresh settles", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockImplementation(async () => SUCCESSOR);

    await harness.session.refresh();
    await harness.session.refresh();

    expect(harness.transport.refresh).toHaveBeenCalledTimes(2);
  });
});

describe("resolveStartupAction", () => {
  it("resumes a pending grant, refreshes an active record and skips the rest", () => {
    expect(
      resolveStartupAction({
        record_version: 1,
        state: "pending_grant",
        polling_secret: "pg1.polling.secret",
      }),
    ).toBe("resume_pending_grant");

    expect(
      resolveStartupAction({
        record_version: 1,
        state: "active",
        refresh_credential: REFRESH_CREDENTIAL,
        refresh_generation: 1,
        pending_rotation_id: null,
      }),
    ).toBe("refresh_credential");

    expect(
      resolveStartupAction({
        record_version: 1,
        state: "cleared",
        cleared_reason: "token_reuse",
      }),
    ).toBe("none");

    expect(resolveStartupAction(null)).toBe("none");
  });
});

describe("closed failure-reason detail surfacing (closed-reason surfacing C2)", () => {
  it("surfaces network_unavailable on a refresh network failure (A2)", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockRejectedValue(
      new DeviceAuthError("network_unavailable", {
        status: 0,
        message: "offline",
        isLocal: true,
      }),
    );

    await expect(harness.session.refresh()).rejects.toMatchObject({
      code: "network_unavailable",
    });

    expect(harness.states).toEqual(["offline"]);
    expect(harness.stateDetails).toEqual(["network_unavailable"]);
  });

  it("surfaces the closed server code of an unmapped refresh failure (A2)", async () => {
    const harness = createSessionHarness();
    harness.transport.refresh.mockRejectedValue(
      new DeviceAuthError("database_connection_unavailable", {
        status: 503,
        message: "unavailable",
      }),
    );

    await expect(harness.session.refresh()).rejects.toMatchObject({
      code: "database_connection_unavailable",
    });

    expect(harness.states).toEqual(["refresh_required"]);
    expect(harness.stateDetails).toEqual(["database_connection_unavailable"]);
  });

  it("surfaces the terminal ClearedReason when a refresh failure tombstones the record (A3)", async () => {
    const reuseHarness = createSessionHarness();
    reuseHarness.transport.refresh.mockRejectedValue(deviceTokenReuseError());
    await expect(reuseHarness.session.refresh()).rejects.toMatchObject({
      code: "device_token_reuse_detected",
    });
    expect(reuseHarness.states).toEqual(["revoked"]);
    expect(reuseHarness.stateDetails).toEqual(["token_reuse"]);

    const revokedHarness = createSessionHarness();
    revokedHarness.transport.refresh.mockRejectedValue(
      new DeviceAuthError("device_revoked", { status: 401, message: "revoked" }),
    );
    await expect(revokedHarness.session.refresh()).rejects.toMatchObject({ code: "device_revoked" });
    expect(revokedHarness.states).toEqual(["revoked"]);
    expect(revokedHarness.stateDetails).toEqual(["device_revoked"]);

    const invalidHarness = createSessionHarness();
    invalidHarness.transport.refresh.mockRejectedValue(
      new DeviceAuthError("device_credential_invalid", {
        status: 401,
        message: "credential is no longer valid",
      }),
    );
    await expect(invalidHarness.session.refresh()).rejects.toMatchObject({
      code: "device_credential_invalid",
    });
    expect(invalidHarness.states).toEqual(["revoked"]);
    expect(invalidHarness.stateDetails).toEqual(["credential_invalid"]);
  });

  it("surfaces network_unavailable and the self-disconnect ClearedReason on disconnect (A2/A3)", async () => {
    const offlineHarness = createSessionHarness();
    offlineHarness.transport.revokeCurrent.mockRejectedValue(
      new DeviceAuthError("network_unavailable", {
        status: 0,
        message: "offline",
        isLocal: true,
      }),
    );
    await expect(offlineHarness.session.disconnect()).rejects.toMatchObject({
      code: "network_unavailable",
    });
    expect(offlineHarness.states[offlineHarness.states.length - 1]).toBe("offline");
    expect(offlineHarness.stateDetails[offlineHarness.stateDetails.length - 1]).toBe(
      "network_unavailable",
    );

    const disconnectedHarness = createSessionHarness();
    disconnectedHarness.transport.revokeCurrent.mockResolvedValue({
      device_id: "77777777-7777-4777-8777-777777777777",
      token_family_id: "88888888-8888-4888-8888-888888888888",
      revoked_at: "2026-08-16T10:10:00Z",
    });
    await disconnectedHarness.session.disconnect();
    expect(disconnectedHarness.states[disconnectedHarness.states.length - 1]).toBe("not_connected");
    expect(
      disconnectedHarness.stateDetails[disconnectedHarness.stateDetails.length - 1],
    ).toBe("self_disconnect");
  });
});
