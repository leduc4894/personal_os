/**
 * The crash-safe device token session (spec 13.3, 13.4, 13.5, 14.2, 19).
 *
 * The refresh credential lives only in the SecretStorage record; the access
 * credential is a private in-memory field. Every rotation persists the pending
 * rotation identity and verifies the readback BEFORE the network request, and
 * the full successor in one verified write after the response, so either a
 * retained predecessor (exact replay by rotation identity) or the persisted
 * successor recovers a crash. Terminal reuse/revocation replaces the record
 * with a credential-free tombstone; offline preserves everything.
 */

import { DeviceAuthError, resolveDeviceAuthClosedCode } from "./contracts";
import type {
  ConnectionState,
  DeviceApiTransport,
  DeviceAuthenticationSettings,
  DeviceGrantExchangeWireData,
  SecretStorageRecordAdapter,
  UuidFactory,
} from "./contracts";
import {
  readDeviceSecretRecord,
  writeActiveDeviceRecord,
  writeClearedTombstone,
} from "./secret-storage-record";
import type { ClearedReason, DeviceSecretRecord } from "./secret-storage-record";

export interface DeviceTokenSessionDeps {
  readonly transport: DeviceApiTransport;
  readonly secretStore: SecretStorageRecordAdapter;
  readonly recordName: string;
  readonly settings: DeviceAuthenticationSettings;
  readonly persistSettings: () => Promise<void>;
  readonly createRotationId: UuidFactory;
  readonly onStateChange: (state: ConnectionState, detail: string | null) => void;
}

/** The single bounded startup action of spec 19. */
export type DeviceStartupAction = "resume_pending_grant" | "refresh_credential" | "none";

/**
 * Decide the at-most-one startup action from the record alone: resume a
 * pending grant (an expired one is tombstoned locally by the resume path),
 * refresh an active credential, otherwise nothing.
 */
export function resolveStartupAction(record: DeviceSecretRecord | null): DeviceStartupAction {
  if (record?.state === "pending_grant") {
    return "resume_pending_grant";
  }
  if (record?.state === "active") {
    return "refresh_credential";
  }
  return "none";
}

export class DeviceTokenSession {
  readonly #deps: DeviceTokenSessionDeps;
  #accessCredential: string | null = null;
  #refreshInFlight: Promise<void> | null = null;

  constructor(deps: DeviceTokenSessionDeps) {
    this.#deps = deps;
  }

  /** The memory-only access credential (never persisted, spec 13.3). */
  get accessCredential(): string | null {
    return this.#accessCredential;
  }

  /** Adopt the exchange of a completed onboarding as the live session. */
  adoptExchange(exchange: DeviceGrantExchangeWireData): void {
    this.#accessCredential = exchange.access_credential;
  }

  /** Clear the in-memory access credential (plugin unload). */
  clearMemoryAccess(): void {
    this.#accessCredential = null;
  }

  /**
   * Rotate the refresh credential once (spec 13.3, 13.4): persist the pending
   * rotation identity and verify the readback before the network call, then
   * persist the complete successor in one verified write. A stored pending
   * identity from a crashed attempt is reused so the server replays the exact
   * successor instead of detecting reuse.
   */
  async refresh(): Promise<void> {
    // Single-flight (bare-reload race): plugin onload fires the startup
    // refresh fire-and-forget while a queue pass's login-verdict refresh
    // can arrive concurrently. Two independent rotations on one refresh
    // credential can trip server-side reuse detection and tombstone a
    // healthy credential, so a concurrent caller joins the in-flight
    // rotation instead of starting its own.
    if (this.#refreshInFlight !== null) {
      return this.#refreshInFlight;
    }
    const attempt = this.#rotateOnce();
    this.#refreshInFlight = attempt;
    try {
      await attempt;
    } finally {
      this.#refreshInFlight = null;
    }
  }

  async #rotateOnce(): Promise<void> {
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (record?.state !== "active") {
      throw new DeviceAuthError("device_credential_invalid", {
        status: 0,
        message: "no active device credential record is available",
        isLocal: true,
      });
    }
    const rotationId = record.pending_rotation_id ?? this.#deps.createRotationId();
    writeActiveDeviceRecord(this.#deps.secretStore, this.#deps.recordName, {
      refresh_credential: record.refresh_credential,
      refresh_generation: record.refresh_generation,
      pending_rotation_id: rotationId,
    });

    try {
      const successor = await this.#deps.transport.refresh(record.refresh_credential, rotationId);
      writeActiveDeviceRecord(this.#deps.secretStore, this.#deps.recordName, {
        refresh_credential: successor.refresh_credential,
        refresh_generation: successor.refresh_generation,
        pending_rotation_id: null,
      });
      this.#accessCredential = successor.access_credential;
      this.#deps.onStateChange("connected", null);
    } catch (error) {
      await this.#surfaceRefreshFailure(error);
      throw error;
    }
  }

  async #surfaceRefreshFailure(error: unknown): Promise<void> {
    const code = (error as { code?: string } | null)?.code;
    if (code === "device_token_reuse_detected") {
      await this.#clearTerminalRecord("token_reuse", "revoked");
      return;
    }
    if (code === "device_revoked") {
      await this.#clearTerminalRecord("device_revoked", "revoked");
      return;
    }
    if (code === "device_credential_invalid") {
      await this.#clearTerminalRecord("credential_invalid", "revoked");
      return;
    }
    if (code === "network_unavailable") {
      // Closed-reason surfacing C2 A2: the closed code the failure already
      // holds rides the seam instead of a null detail.
      this.#deps.onStateChange("offline", "network_unavailable");
      return;
    }
    // Closed-reason surfacing C2 A2: the unknown fallback still carries the
    // closed server code it closed on; only a foreign throw keeps null.
    this.#deps.onStateChange("refresh_required", resolveDeviceAuthClosedCode(error));
  }

  async #clearTerminalRecord(
    clearedReason: ClearedReason,
    nextState: ConnectionState,
  ): Promise<void> {
    writeClearedTombstone(this.#deps.secretStore, this.#deps.recordName, clearedReason);
    this.#deps.settings.secret_record_name = null;
    this.#accessCredential = null;
    await this.#deps.persistSettings();
    // Closed-reason surfacing C2 A3: the terminal ClearedReason is durable
    // in the tombstone and rides the state seam as the detail.
    this.#deps.onStateChange(nextState, clearedReason);
  }

  /**
   * Self-disconnect (spec 14.2): the server revoke happens FIRST. A confirmed
   * response or a terminal credential response replaces the record with the
   * verified tombstone and clears the settings reference and the in-memory
   * access credential. Transient failures keep the local record for retry.
   */
  async disconnect(): Promise<void> {
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (record?.state !== "active") {
      return;
    }
    try {
      await this.#deps.transport.revokeCurrent(record.refresh_credential);
    } catch (error) {
      const code = (error as { code?: string } | null)?.code;
      if (
        code === "device_credential_invalid" ||
        code === "device_revoked" ||
        code === "device_token_reuse_detected"
      ) {
        await this.#clearTerminalRecord("self_disconnect", "not_connected");
        return;
      }
      if (code === "network_unavailable") {
        // Closed-reason surfacing C2 A2: same closed token as the refresh
        // network failure — the code the transport already produced.
        this.#deps.onStateChange("offline", "network_unavailable");
      }
      throw error;
    }
    await this.#clearTerminalRecord("self_disconnect", "not_connected");
  }
}
