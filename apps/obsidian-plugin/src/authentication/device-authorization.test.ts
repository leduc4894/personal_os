import { describe, expect, it, vi } from "vitest";

import { DeviceAuthorizationController } from "./device-authorization";
import { DeviceAuthError } from "./contracts";
import type {
  DeviceAuthenticationSettings,
  SecretStorageRecordAdapter,
} from "./contracts";
import { policyVerificationError } from "../exclusion-policy/contracts";
import { DEVICE_CREDENTIAL_RECORD_NAME } from "./secret-storage-record";

const SERVER_ORIGIN = "https://vault.example.com";
const POLLING_SECRET = "pg1.6e5cb1a2-0000-4000-8000-00000000000a.polling-secret";
const NEXT_REFRESH_CREDENTIAL = "rt1.33333333-3333-4333-8333-333333333333.refresh-secret";

const GRANT_DATA = {
  grant_id: "6e5cb1a2-0000-4000-8000-00000000000a",
  user_code: "ABCD-EFGH",
  polling_secret: POLLING_SECRET,
  verification_uri: `${SERVER_ORIGIN}/device/approve`,
  verification_uri_complete: `${SERVER_ORIGIN}/device/approve#ABCD-EFGH`,
  expires_in_seconds: 600,
  poll_interval_seconds: 5,
};

/**
 * The single-public-origin fixture: the API base and every verification URL
 * the server mints share one origin, so browser actions must consume the
 * server-provided URLs verbatim.
 */
const SINGLE_PUBLIC_ORIGIN = "https://workspace.example";
const SINGLE_ORIGIN_GRANT_DATA = {
  ...GRANT_DATA,
  verification_uri: `${SINGLE_PUBLIC_ORIGIN}/device/approve`,
  verification_uri_complete: `${SINGLE_PUBLIC_ORIGIN}/device/approve#ABCD-EFGH`,
};

const EXCHANGE_DATA = {
  grant_id: "6e5cb1a2-0000-4000-8000-00000000000a",
  device_id: "77777777-7777-4777-8777-777777777777",
  token_family_id: "88888888-8888-4888-8888-888888888888",
  refresh_generation: 1,
  access_credential: "at1.22222222-2222-4222-8222-222222222222.access-secret",
  refresh_credential: NEXT_REFRESH_CREDENTIAL,
  access_expires_at: "2026-08-16T10:15:00Z",
  refresh_expires_at: "2026-09-15T10:10:00Z",
};

function pendingError(retryAfterSeconds: number): DeviceAuthError {
  return new DeviceAuthError("device_authorization_pending", {
    status: 409,
    message: "pending",
    retryAfterSeconds,
  });
}

function slowDownError(retryAfterSeconds: number): DeviceAuthError {
  return new DeviceAuthError("device_authorization_slow_down", {
    status: 429,
    message: "slow down",
    retryAfterSeconds,
  });
}

class ManualTimeline {
  nowMs = 1_000_000;
  requestedDelayMs: number[] = [];
  private delayResolvers: Array<() => void> = [];

  readonly delay = (milliseconds: number): Promise<void> => {
    this.requestedDelayMs.push(milliseconds);
    this.nowMs += milliseconds;
    return new Promise<void>((resolve) => {
      this.delayResolvers.push(resolve);
    });
  };

  async settle(): Promise<void> {
    for (let index = 0; index < 50; index += 1) {
      await Promise.resolve();
    }
  }

  async releaseOneDelay(): Promise<void> {
    await this.settle();
    const resolve = this.delayResolvers.shift();
    if (resolve === undefined) {
      throw new Error("no pending delay to release");
    }
    resolve();
    await this.settle();
  }
}

interface TestHarness {
  controller: DeviceAuthorizationController;
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
  openUrl: ReturnType<typeof vi.fn>;
  timeline: ManualTimeline;
  states: string[];
  stateDetails: (string | null)[];
  onExchange: ReturnType<typeof vi.fn>;
  pendingGrantsAtOpenUrl: unknown[];
}

function createHarness(overrides: Partial<DeviceAuthenticationSettings> = {}): TestHarness {
  const stored = new Map<string, string>();
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
    server_origin: SERVER_ORIGIN,
    device_name: "Personal vault",
    client_instance_id: "11111111-1111-4111-8111-111111111111",
    device_id: null,
    secret_record_name: null,
    pending_grant: null,
    ...overrides,
  };
  const timeline = new ManualTimeline();
  const states: string[] = [];
  const stateDetails: (string | null)[] = [];
  const onExchange = vi.fn();
  const persistSettings = vi.fn(async () => undefined);
  const pendingGrantsAtOpenUrl: unknown[] = [];
  const openUrl = vi.fn(() => {
    pendingGrantsAtOpenUrl.push(
      settings.pending_grant === null ? null : { ...settings.pending_grant },
    );
  });
  const controller = new DeviceAuthorizationController({
    transport,
    secretStore: secretStorage,
    recordName: DEVICE_CREDENTIAL_RECORD_NAME,
    settings,
    persistSettings,
    clientIdentity: {
      platformClass: "obsidian_desktop",
      platformName: "windows",
      pluginVersion: "0.1.0",
      clientInstanceId: settings.client_instance_id,
    },
    allowLoopbackHttp: false,
    openUrl,
    delay: timeline.delay,
    nowEpochMs: () => timeline.nowMs,
    onStateChange: (state, detail) => {
      states.push(state);
      stateDetails.push(detail);
    },
    onExchange,
  });
  return {
    controller,
    transport,
    secretStorage,
    stored,
    settings,
    persistSettings,
    openUrl,
    timeline,
    states,
    stateDetails,
    onExchange,
    pendingGrantsAtOpenUrl,
  };
}

describe("DeviceAuthorizationController login", () => {
  it("persists the polling secret before opening the verification URL", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.secretStorage.setSecret).toHaveBeenCalledBefore(harness.openUrl);
    const firstWrite = (harness.secretStorage.setSecret as ReturnType<typeof vi.fn>).mock
      .calls[0]?.[1];
    expect(JSON.parse(String(firstWrite))).toEqual({
      record_version: 1,
      state: "pending_grant",
      polling_secret: POLLING_SECRET,
    });
    expect(harness.openUrl).toHaveBeenCalledWith(GRANT_DATA.verification_uri_complete);
    expect(harness.pendingGrantsAtOpenUrl).toEqual([
      {
        grant_id: GRANT_DATA.grant_id,
        user_code: GRANT_DATA.user_code,
        verification_uri: GRANT_DATA.verification_uri,
        expires_at_epoch_seconds: (1_000_000 + 600_000) / 1000,
        poll_interval_seconds: 5,
      },
    ]);
    expect(harness.settings.secret_record_name).toBe(DEVICE_CREDENTIAL_RECORD_NAME);
  });

  it("sends the closed creation body with the device identity", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.transport.createGrant).toHaveBeenCalledWith({
      client_instance_id: "11111111-1111-4111-8111-111111111111",
      device_name: "Personal vault",
      platform_class: "obsidian_desktop",
      platform_name: "windows",
      plugin_version: "0.1.0",
      requested_scope: "obsidian_sync",
    });
  });

  it("surfaces an invalid server origin as the closed configuration_invalid state", async () => {
    const harness = createHarness({ server_origin: "http://vault.example.com" });
    await harness.controller.login();
    expect(harness.states).toEqual(["configuration_invalid"]);
    expect(harness.transport.createGrant).not.toHaveBeenCalled();
    expect(harness.secretStorage.setSecret).not.toHaveBeenCalled();
    expect(harness.openUrl).not.toHaveBeenCalled();
  });

  it("surfaces an invalid device name as configuration_invalid without a request", async () => {
    const harness = createHarness({ device_name: "   " });
    await harness.controller.login();
    expect(harness.states).toEqual(["configuration_invalid"]);
    expect(harness.transport.createGrant).not.toHaveBeenCalled();
  });

  it("closes plugin_version_unsupported without any retry loop", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockRejectedValue(
      new DeviceAuthError("plugin_version_unsupported", {
        status: 426,
        message: "unsupported",
        approvedVersionBounds: { minimum: "0.1.0", maximum: "2.0.0" },
      }),
    );
    await harness.controller.login();
    expect(harness.transport.createGrant).toHaveBeenCalledTimes(1);
    expect(harness.states).toEqual(["requesting_authorization", "configuration_invalid"]);
    expect(harness.secretStorage.setSecret).not.toHaveBeenCalled();
    expect(harness.openUrl).not.toHaveBeenCalled();
  });

  it("reports offline on a creation network failure while preserving storage", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockRejectedValue(
      new DeviceAuthError("network_unavailable", { status: 0, message: "offline", isLocal: true }),
    );
    await harness.controller.login();
    expect(harness.states).toEqual(["requesting_authorization", "offline"]);
    expect(harness.secretStorage.setSecret).not.toHaveBeenCalled();
    expect(harness.settings.pending_grant).toBeNull();
  });

  it("surfaces rate-limited creation with retry guidance", async () => {
    // Plugin hygiene (2026-08-16 §12): a 429 grant-creation refusal is NOT
    // an offline state — the server answered and asked to wait, so the
    // state change carries the closed rate-limited code plus the server's
    // retry window instead of the offline label.
    const harness = createHarness();
    harness.transport.createGrant.mockRejectedValue(
      new DeviceAuthError("authentication_rate_limited", {
        status: 429,
        message: "rate limited",
        retryAfterSeconds: 30,
      }),
    );

    await harness.controller.login();

    const lastState = harness.states[harness.states.length - 1];
    expect(lastState).not.toBe("offline");
    expect(lastState).toBe("not_connected");
    const lastDetail = harness.stateDetails[harness.stateDetails.length - 1];
    expect(lastDetail).toContain("authentication_rate_limited");
    expect(lastDetail).toContain("30");
    // No record is written and no pending reference survives the refusal.
    expect(harness.secretStorage.setSecret).not.toHaveBeenCalled();
    expect(harness.settings.pending_grant).toBeNull();
  });

  it("surfaces a rate-limited refusal without a retry hint as the closed code only", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockRejectedValue(
      new DeviceAuthError("authentication_rate_limited", {
        status: 429,
        message: "rate limited",
        retryAfterSeconds: null,
      }),
    );

    await harness.controller.login();

    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
    expect(harness.stateDetails[harness.stateDetails.length - 1]).toBe(
      "authentication_rate_limited",
    );
  });

  it("login refuses to overwrite an active record", async () => {
    // Plugin hygiene (2026-08-16 §12): `canLogin` is only the UI gate. The
    // controller reads the durable record FIRST and refuses with a typed
    // error when an active credential exists, so a stale render racing the
    // record can never overwrite the live credential with a new grant.
    const harness = createHarness();
    harness.stored.set(
      DEVICE_CREDENTIAL_RECORD_NAME,
      JSON.stringify({
        record_version: 1,
        state: "active",
        refresh_credential: NEXT_REFRESH_CREDENTIAL,
        refresh_generation: 1,
        pending_rotation_id: null,
      }),
    );

    await expect(harness.controller.login()).rejects.toMatchObject({
      code: "device_credential_invalid",
      isLocal: true,
    });

    expect(harness.transport.createGrant).not.toHaveBeenCalled();
    expect(harness.secretStorage.setSecret).not.toHaveBeenCalled();
    expect(harness.openUrl).not.toHaveBeenCalled();
    expect(harness.settings.pending_grant).toBeNull();
    // The closed refusal code rides the state seam, so the settings render
    // shows why Login refused instead of swallowing the rejection.
    expect(harness.states).toEqual(["refresh_required"]);
    expect(harness.stateDetails).toEqual(["device_credential_invalid"]);
  });
});

describe("DeviceAuthorizationController polling", () => {
  it("paces every poll by the server interval and adopts slow-down hints", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant
      .mockRejectedValueOnce(pendingError(7))
      .mockRejectedValueOnce(slowDownError(9))
      .mockResolvedValueOnce(EXCHANGE_DATA);

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await harness.timeline.releaseOneDelay();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.timeline.requestedDelayMs).toEqual([5_000, 7_000, 9_000]);
    expect(harness.transport.pollGrant).toHaveBeenCalledTimes(3);
    expect(harness.transport.pollGrant).toHaveBeenCalledWith(
      GRANT_DATA.grant_id,
      POLLING_SECRET,
    );
  });

  it("writes the active successor record and reaches connected on exchange", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: NEXT_REFRESH_CREDENTIAL,
      refresh_generation: 1,
      pending_rotation_id: null,
    });
    expect(harness.settings.pending_grant).toBeNull();
    expect(harness.settings.secret_record_name).toBe(DEVICE_CREDENTIAL_RECORD_NAME);
    expect(harness.onExchange).toHaveBeenCalledWith(EXCHANGE_DATA);
    expect(harness.states).toEqual([
      "requesting_authorization",
      "waiting_for_approval",
      "connected",
    ]);
  });

  it("does not report connected until asynchronous exchange work has completed", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);
    let completeExchange!: () => void;
    const exchangeWork = new Promise<void>((resolve) => {
      completeExchange = resolve;
    });
    harness.onExchange.mockImplementation(
      () => exchangeWork,
    );

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await Promise.resolve();

    expect(harness.states).toEqual(["requesting_authorization", "waiting_for_approval"]);

    completeExchange();
    await loginPromise;

    expect(harness.states).toEqual([
      "requesting_authorization",
      "waiting_for_approval",
      "connected",
    ]);
  });

  it("reports offline and retains the credential when exchange work fails", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);
    harness.onExchange.mockRejectedValue(new Error("policy bootstrap failed"));

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.states).toEqual([
      "requesting_authorization",
      "waiting_for_approval",
      "offline",
    ]);
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toMatchObject({
      state: "active",
      refresh_credential: NEXT_REFRESH_CREDENTIAL,
    });
  });

  it("tombstones a denied grant and clears the settings reference", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockRejectedValue(
      new DeviceAuthError("device_authorization_denied", { status: 403, message: "denied" }),
    );

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "grant_denied",
    });
    expect(harness.settings.pending_grant).toBeNull();
    expect(harness.settings.secret_record_name).toBeNull();
    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
  });

  it("tombstones an expired grant", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockRejectedValue(
      new DeviceAuthError("device_authorization_expired", { status: 410, message: "expired" }),
    );

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "grant_expired",
    });
    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
  });

  it("stops polling at the grant expiry deadline even while pending", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue({
      ...GRANT_DATA,
      expires_in_seconds: 1,
      poll_interval_seconds: 5,
    });
    harness.transport.pollGrant.mockRejectedValue(pendingError(5));

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "grant_expired",
    });
    expect(harness.transport.pollGrant).not.toHaveBeenCalled();
    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
  });

  it("preserves the pending record and reports offline on a poll network failure", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockRejectedValue(
      new DeviceAuthError("network_unavailable", { status: 0, message: "offline", isLocal: true }),
    );

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "pending_grant",
      polling_secret: POLLING_SECRET,
    });
    expect(harness.settings.pending_grant).not.toBeNull();
    expect(harness.states[harness.states.length - 1]).toBe("offline");
  });

  it("discards a late poll result that lands after cancel", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    let resolveLatePoll: (value: unknown) => void = () => undefined;
    const latePoll = new Promise<unknown>((resolve) => {
      resolveLatePoll = resolve;
    });
    harness.transport.pollGrant.mockReturnValue(latePoll);

    const loginPromise = harness.controller.login();
    // Release the pacing delay so the loop is inside the in-flight poll.
    await harness.timeline.releaseOneDelay();
    await harness.controller.cancelPendingLogin();
    resolveLatePoll(EXCHANGE_DATA);
    await harness.timeline.settle();
    await loginPromise;

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "login_cancelled",
    });
    expect(harness.onExchange).not.toHaveBeenCalled();
    expect(harness.settings.pending_grant).toBeNull();
    expect(harness.settings.secret_record_name).toBeNull();
    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
  });
});

describe("cancel and resume", () => {
  it("cancels a pending login with a tombstone and no further polls", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);

    const loginPromise = harness.controller.login();
    await harness.timeline.settle();
    await harness.controller.cancelPendingLogin();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "login_cancelled",
    });
    expect(harness.transport.pollGrant).not.toHaveBeenCalled();
    expect(harness.settings.pending_grant).toBeNull();
    expect(harness.settings.secret_record_name).toBeNull();
    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
  });

  it("reopens the exact verification URL with the user-code fragment", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);

    const loginPromise = harness.controller.login();
    await harness.timeline.settle();
    harness.controller.openBrowserAgain();
    expect(harness.openUrl).toHaveBeenLastCalledWith(`${GRANT_DATA.verification_uri}#ABCD-EFGH`);
    await harness.timeline.releaseOneDelay();
    await loginPromise;
  });

  it("resumes an unexpired pending grant before expiry", async () => {
    const harness = createHarness({
      pending_grant: {
        grant_id: GRANT_DATA.grant_id,
        user_code: GRANT_DATA.user_code,
        verification_uri: GRANT_DATA.verification_uri,
        expires_at_epoch_seconds: (1_000_000 + 600_000) / 1000,
        poll_interval_seconds: 5,
      },
      secret_record_name: DEVICE_CREDENTIAL_RECORD_NAME,
    });
    harness.stored.set(
      DEVICE_CREDENTIAL_RECORD_NAME,
      JSON.stringify({
        record_version: 1,
        state: "pending_grant",
        polling_secret: POLLING_SECRET,
      }),
    );
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);

    const resumePromise = harness.controller.resumePendingGrant();
    await harness.timeline.releaseOneDelay();
    await resumePromise;

    expect(harness.transport.pollGrant).toHaveBeenCalledWith(GRANT_DATA.grant_id, POLLING_SECRET);
    expect(harness.states).toEqual(["waiting_for_approval", "connected"]);
  });

  it("tombstones an expired pending grant at resume without polling", async () => {
    const harness = createHarness({
      pending_grant: {
        grant_id: GRANT_DATA.grant_id,
        user_code: GRANT_DATA.user_code,
        verification_uri: GRANT_DATA.verification_uri,
        expires_at_epoch_seconds: 500,
        poll_interval_seconds: 5,
      },
      secret_record_name: DEVICE_CREDENTIAL_RECORD_NAME,
    });
    harness.stored.set(
      DEVICE_CREDENTIAL_RECORD_NAME,
      JSON.stringify({
        record_version: 1,
        state: "pending_grant",
        polling_secret: POLLING_SECRET,
      }),
    );

    await harness.controller.resumePendingGrant();

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "grant_expired",
    });
    expect(harness.transport.pollGrant).not.toHaveBeenCalled();
    expect(harness.settings.pending_grant).toBeNull();
  });
});

describe("browser actions stay on the single public origin", () => {
  it("opens the server-minted verification_uri_complete on login", async () => {
    const harness = createHarness({ server_origin: SINGLE_PUBLIC_ORIGIN });
    harness.transport.createGrant.mockResolvedValue(SINGLE_ORIGIN_GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.openUrl).toHaveBeenCalledTimes(1);
    expect(harness.openUrl).toHaveBeenCalledWith(
      SINGLE_ORIGIN_GRANT_DATA.verification_uri_complete,
    );
  });

  it("reopens the single-origin verification_uri with the user-code fragment", async () => {
    const harness = createHarness({ server_origin: SINGLE_PUBLIC_ORIGIN });
    harness.transport.createGrant.mockResolvedValue(SINGLE_ORIGIN_GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);

    const loginPromise = harness.controller.login();
    await harness.timeline.settle();
    harness.controller.openBrowserAgain();
    expect(harness.openUrl).toHaveBeenLastCalledWith(
      `${SINGLE_ORIGIN_GRANT_DATA.verification_uri}#${SINGLE_ORIGIN_GRANT_DATA.user_code}`,
    );
    await harness.timeline.releaseOneDelay();
    await loginPromise;
  });
});

describe("crash-window reconciliation of the pending reference", () => {
  function createActiveRecordHarness(): TestHarness {
    const harness = createHarness({
      pending_grant: {
        grant_id: GRANT_DATA.grant_id,
        user_code: GRANT_DATA.user_code,
        verification_uri: GRANT_DATA.verification_uri,
        expires_at_epoch_seconds: (1_000_000 + 600_000) / 1000,
        poll_interval_seconds: 5,
      },
      secret_record_name: DEVICE_CREDENTIAL_RECORD_NAME,
    });
    harness.stored.set(
      DEVICE_CREDENTIAL_RECORD_NAME,
      JSON.stringify({
        record_version: 1,
        state: "active",
        refresh_credential: NEXT_REFRESH_CREDENTIAL,
        refresh_generation: 1,
        pending_rotation_id: null,
      }),
    );
    return harness;
  }

  it("keeps the active record and drops the stale pending reference", async () => {
    const harness = createActiveRecordHarness();

    await harness.controller.reconcileCrashWindow();

    expect(harness.settings.pending_grant).toBeNull();
    expect(harness.settings.secret_record_name).toBe(DEVICE_CREDENTIAL_RECORD_NAME);
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: NEXT_REFRESH_CREDENTIAL,
      refresh_generation: 1,
      pending_rotation_id: null,
    });
    expect(harness.transport.pollGrant).not.toHaveBeenCalled();
    // An active refresh credential is not an active access credential.  The
    // startup refresh in the plugin owns the transition to `connected` once
    // it has obtained an in-memory access credential.
    expect(harness.states).toEqual(["refresh_required"]);
  });

  it("drops a stale pending reference over a cleared record and reports not_connected", async () => {
    const harness = createHarness({
      pending_grant: {
        grant_id: GRANT_DATA.grant_id,
        user_code: GRANT_DATA.user_code,
        verification_uri: GRANT_DATA.verification_uri,
        expires_at_epoch_seconds: (1_000_000 + 600_000) / 1000,
        poll_interval_seconds: 5,
      },
      secret_record_name: DEVICE_CREDENTIAL_RECORD_NAME,
    });
    harness.stored.set(
      DEVICE_CREDENTIAL_RECORD_NAME,
      JSON.stringify({ record_version: 1, state: "cleared", cleared_reason: "token_reuse" }),
    );

    await harness.controller.reconcileCrashWindow();

    expect(harness.settings.pending_grant).toBeNull();
    expect(harness.settings.secret_record_name).toBeNull();
    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "token_reuse",
    });
    expect(harness.states).toEqual(["not_connected"]);
  });

  it("cancel keeps a live credential when the pending reference is stale", async () => {
    const harness = createActiveRecordHarness();

    await harness.controller.cancelPendingLogin();

    expect(JSON.parse(harness.stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: NEXT_REFRESH_CREDENTIAL,
      refresh_generation: 1,
      pending_rotation_id: null,
    });
    expect(harness.settings.pending_grant).toBeNull();
    expect(harness.settings.secret_record_name).toBe(DEVICE_CREDENTIAL_RECORD_NAME);
    expect(harness.states[harness.states.length - 1]).toBe("connected");
  });
});

describe("closed failure-reason detail surfacing (closed-reason surfacing C2)", () => {
  it("surfaces the closed policy token of a failed onboarding exchange (A1)", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);
    harness.onExchange.mockRejectedValue(
      policyVerificationError("policy_signature_untrusted_key"),
    );

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.states[harness.states.length - 1]).toBe("offline");
    expect(harness.stateDetails[harness.stateDetails.length - 1]).toBe(
      "policy_signature_untrusted_key",
    );
  });

  it("keeps a null detail when the exchange bootstrap throws a foreign error (A1)", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockResolvedValue(EXCHANGE_DATA);
    harness.onExchange.mockRejectedValue(new Error("unmapped exchange failure"));

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.states[harness.states.length - 1]).toBe("offline");
    expect(harness.stateDetails[harness.stateDetails.length - 1]).toBeNull();
  });

  it("surfaces network_unavailable on a creation network failure (A4)", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockRejectedValue(
      new DeviceAuthError("network_unavailable", { status: 0, message: "offline", isLocal: true }),
    );

    await harness.controller.login();

    expect(harness.states[harness.states.length - 1]).toBe("offline");
    expect(harness.stateDetails[harness.stateDetails.length - 1]).toBe("network_unavailable");
  });

  it("surfaces the closed server code of an unmapped grant-creation failure (A4)", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockRejectedValue(
      new DeviceAuthError("database_connection_unavailable", {
        status: 503,
        message: "unavailable",
      }),
    );

    await harness.controller.login();

    expect(harness.states[harness.states.length - 1]).toBe("offline");
    expect(harness.stateDetails[harness.stateDetails.length - 1]).toBe(
      "database_connection_unavailable",
    );
  });

  it("surfaces network_unavailable on a poll network failure (A5)", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockRejectedValue(
      new DeviceAuthError("network_unavailable", { status: 0, message: "offline", isLocal: true }),
    );

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.states[harness.states.length - 1]).toBe("offline");
    expect(harness.stateDetails[harness.stateDetails.length - 1]).toBe("network_unavailable");
  });

  it("surfaces the closed server code of an unmapped poll failure (A5)", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockRejectedValue(
      new DeviceAuthError("database_connection_unavailable", {
        status: 503,
        message: "unavailable",
      }),
    );

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.states[harness.states.length - 1]).toBe("offline");
    expect(harness.stateDetails[harness.stateDetails.length - 1]).toBe(
      "database_connection_unavailable",
    );
  });

  it("surfaces the terminal ClearedReason beside a denied grant (A3)", async () => {
    const harness = createHarness();
    harness.transport.createGrant.mockResolvedValue(GRANT_DATA);
    harness.transport.pollGrant.mockRejectedValue(
      new DeviceAuthError("device_authorization_denied", { status: 403, message: "denied" }),
    );

    const loginPromise = harness.controller.login();
    await harness.timeline.releaseOneDelay();
    await loginPromise;

    expect(harness.states[harness.states.length - 1]).toBe("not_connected");
    expect(harness.stateDetails[harness.stateDetails.length - 1]).toBe("grant_denied");
  });
});
