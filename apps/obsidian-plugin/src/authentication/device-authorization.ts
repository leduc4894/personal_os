/**
 * The bounded browser device-authorization onboarding state machine
 * (spec 11, 12, 19).
 *
 * The controller creates one grant, persists the polling secret BEFORE any
 * browser is opened, resumes pending grants before expiry, polls no faster
 * than the server interval (adopting every slow-down hint exactly), stops on
 * every terminal outcome with a credential-free tombstone, and never starts a
 * background sync loop — the poll loop is bounded by the grant expiry.
 */

import {
  parseServerOrigin,
  resolveDeviceAuthClosedCode,
  validateDeviceName,
} from "./contracts";
import type {
  ConnectionState,
  DeviceApiTransport,
  DeviceAuthError,
  DeviceAuthenticationSettings,
  DeviceGrantExchangeWireData,
  DeviceGrantWireRequest,
  EpochMsClock,
  SecretStorageRecordAdapter,
  Delay,
  UrlOpener,
} from "./contracts";
import { PolicyVerificationError } from "../exclusion-policy/contracts";
import {
  readDeviceSecretRecord,
  writeActiveDeviceRecord,
  writeClearedTombstone,
  writePendingGrantRecord,
} from "./secret-storage-record";
import type { ClearedReason } from "./secret-storage-record";

export interface DeviceAuthorizationClientIdentity {
  readonly platformClass: DeviceGrantWireRequest["platform_class"];
  readonly platformName: string;
  readonly pluginVersion: string;
  readonly clientInstanceId: string;
}

export interface DeviceAuthorizationControllerDeps {
  readonly transport: DeviceApiTransport;
  readonly secretStore: SecretStorageRecordAdapter;
  readonly recordName: string;
  readonly settings: DeviceAuthenticationSettings;
  readonly persistSettings: () => Promise<void>;
  readonly clientIdentity: DeviceAuthorizationClientIdentity;
  readonly allowLoopbackHttp: boolean;
  readonly openUrl: UrlOpener;
  readonly delay: Delay;
  readonly nowEpochMs: EpochMsClock;
  readonly onStateChange: (state: ConnectionState, detail: string | null) => void;
  /**
   * Completes any authenticated-session work that must finish before this
   * device is usable. The policy trust bootstrap uses this boundary so a
   * connected device never captures files against an uninitialised policy.
   */
  readonly onExchange: (exchange: DeviceGrantExchangeWireData) => void | Promise<void>;
}

export class DeviceAuthorizationController {
  readonly #deps: DeviceAuthorizationControllerDeps;
  #stopRequested = false;

  constructor(deps: DeviceAuthorizationControllerDeps) {
    this.#deps = deps;
  }

  /** Stop the poll loop without touching any record (plugin unload). */
  stop(): void {
    this.#stopRequested = true;
  }

  /**
   * Start one bounded onboarding: create the grant, persist the polling
   * secret, open the verification URL, then poll until a terminal outcome,
   * expiry or a recoverable offline state.
   */
  async login(): Promise<void> {
    this.#stopRequested = false;
    const origin = parseServerOrigin(this.#deps.settings.server_origin, {
      allowLoopbackHttp: this.#deps.allowLoopbackHttp,
    });
    const deviceName = validateDeviceName(this.#deps.settings.device_name);
    if (origin === null || deviceName === null) {
      this.#deps.onStateChange(
        "configuration_invalid",
        origin === null ? "the server origin must be an exact HTTPS origin" : null,
      );
      return;
    }

    this.#deps.onStateChange("requesting_authorization", null);
    let grant;
    try {
      grant = await this.#deps.transport.createGrant({
        client_instance_id: this.#deps.clientIdentity.clientInstanceId,
        device_name: deviceName,
        platform_class: this.#deps.clientIdentity.platformClass,
        platform_name: this.#deps.clientIdentity.platformName,
        plugin_version: this.#deps.clientIdentity.pluginVersion,
        requested_scope: "obsidian_sync",
      });
    } catch (error) {
      this.#surfaceCreationFailure(error);
      return;
    }

    // The polling secret is durable before any browser opens (spec 11.1).
    writePendingGrantRecord(this.#deps.secretStore, this.#deps.recordName, grant.polling_secret);
    this.#deps.settings.pending_grant = {
      grant_id: grant.grant_id,
      user_code: grant.user_code,
      verification_uri: grant.verification_uri,
      expires_at_epoch_seconds: Math.floor(this.#deps.nowEpochMs() / 1000) + grant.expires_in_seconds,
      poll_interval_seconds: grant.poll_interval_seconds,
    };
    this.#deps.settings.secret_record_name = this.#deps.recordName;
    await this.#deps.persistSettings();
    this.#deps.onStateChange("waiting_for_approval", grant.user_code);
    this.#deps.openUrl(grant.verification_uri_complete);
    await this.#pollUntilTerminal();
  }

  /** Re-open the approval page for a still-pending grant (spec 11.2). */
  openBrowserAgain(): void {
    const pendingGrant = this.#deps.settings.pending_grant;
    if (pendingGrant === null) {
      return;
    }
    this.#deps.openUrl(`${pendingGrant.verification_uri}#${pendingGrant.user_code}`);
  }

  /**
   * Cancel a pending login: stop polling, tombstone the record locally and
   * clear the non-secret settings reference. No network call is made. A
   * stale reference over an already-exchanged record is dropped without
   * touching the live credential.
   */
  async cancelPendingLogin(): Promise<void> {
    this.#stopRequested = true;
    if (this.#deps.settings.pending_grant === null) {
      return;
    }
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (record?.state === "active") {
      // Crash window: the grant already exchanged before the pending
      // reference was cleared, so cancelling must keep the credential.
      this.#deps.settings.pending_grant = null;
      this.#deps.settings.secret_record_name = this.#deps.recordName;
      await this.#deps.persistSettings();
      this.#deps.onStateChange("connected", null);
      return;
    }
    await this.#terminatePendingGrant("login_cancelled", "not_connected");
  }

  /**
   * Reconcile the crash-window pairings between the record and the pending
   * grant reference (spec 19 bounded startup), locally and without network:
   * a poll exchange commits the active record before the pending reference
   * is cleared, and a tombstone commits before the reference is cleared, so
   * a crash in either gap leaves a stale reference that must never destroy
   * the committed record. An active record keeps its credential and reports
   * requires a refresh before it can report connected; a cleared (or absent)
   * record drops the stale reference and
   * reports not_connected. Pending records belong to the resume path.
   */
  async reconcileCrashWindow(): Promise<void> {
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (record?.state === "pending_grant") {
      return;
    }
    if (record?.state === "active") {
      const referenceIsStale =
        this.#deps.settings.pending_grant !== null ||
        this.#deps.settings.secret_record_name !== this.#deps.recordName;
      if (referenceIsStale) {
        this.#deps.settings.pending_grant = null;
        this.#deps.settings.secret_record_name = this.#deps.recordName;
        await this.#deps.persistSettings();
      }
      // The durable record holds only a refresh credential.  The startup
      // refresh owns the later transition to Connected after it has obtained
      // the memory-only access credential needed for sync.
      this.#deps.onStateChange("refresh_required", null);
      return;
    }
    const referenceIsStale =
      this.#deps.settings.pending_grant !== null ||
      this.#deps.settings.secret_record_name !== null;
    if (referenceIsStale) {
      this.#deps.settings.pending_grant = null;
      this.#deps.settings.secret_record_name = null;
      await this.#deps.persistSettings();
      this.#deps.onStateChange("not_connected", null);
    }
  }

  /**
   * Resume one still-pending grant after a restart (spec 19): an expired
   * grant is tombstoned locally without polling; an unexpired grant resumes
   * the bounded poll loop with its persisted interval.
   */
  async resumePendingGrant(): Promise<void> {
    const pendingGrant = this.#deps.settings.pending_grant;
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (pendingGrant === null || record?.state !== "pending_grant") {
      if (record?.state === "pending_grant") {
        await this.#terminatePendingGrant("grant_invalid", "not_connected");
      }
      return;
    }
    if (this.#deps.nowEpochMs() >= pendingGrant.expires_at_epoch_seconds * 1000) {
      await this.#terminatePendingGrant("grant_expired", "not_connected");
      return;
    }
    this.#stopRequested = false;
    this.#deps.onStateChange("waiting_for_approval", pendingGrant.user_code);
    await this.#pollUntilTerminal();
  }

  async #terminatePendingGrant(
    clearedReason: ClearedReason,
    nextState: ConnectionState,
  ): Promise<void> {
    writeClearedTombstone(this.#deps.secretStore, this.#deps.recordName, clearedReason);
    this.#deps.settings.pending_grant = null;
    this.#deps.settings.secret_record_name = null;
    await this.#deps.persistSettings();
    // Closed-reason surfacing C2 A3: the terminal ClearedReason is durable
    // in the tombstone and rides the state seam as the detail.
    this.#deps.onStateChange(nextState, clearedReason);
  }

  #surfaceCreationFailure(error: unknown): void {
    const authError = error as DeviceAuthError;
    if (
      authError?.code === "plugin_version_unsupported" ||
      authError?.code === "api_request_validation_failed" ||
      authError?.code === "api_request_malformed"
    ) {
      const detail =
        authError.approvedVersionBounds === null
          ? null
          : `approved plugin versions ${authError.approvedVersionBounds.minimum} – ${authError.approvedVersionBounds.maximum}`;
      this.#deps.onStateChange("configuration_invalid", detail);
      return;
    }
    // Closed-reason surfacing C2 A4: the closed code the transport already
    // produced (transport code or server registry code) reaches the seam
    // instead of collapsing every non-mapped failure to a null detail.
    this.#deps.onStateChange("offline", resolveDeviceAuthClosedCode(error));
  }

  async #pollUntilTerminal(): Promise<void> {
    const pendingGrant = this.#deps.settings.pending_grant;
    if (pendingGrant === null) {
      return;
    }
    let intervalSeconds = pendingGrant.poll_interval_seconds;
    for (;;) {
      await this.#deps.delay(intervalSeconds * 1000);
      if (this.#stopRequested) {
        return;
      }
      const currentGrant = this.#deps.settings.pending_grant;
      if (currentGrant === null) {
        return;
      }
      if (this.#deps.nowEpochMs() >= currentGrant.expires_at_epoch_seconds * 1000) {
        await this.#terminatePendingGrant("grant_expired", "not_connected");
        return;
      }
      const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
      if (record?.state !== "pending_grant") {
        return;
      }
      let exchange: DeviceGrantExchangeWireData;
      try {
        exchange = await this.#deps.transport.pollGrant(
          currentGrant.grant_id,
          record.polling_secret,
        );
      } catch (error) {
        const outcome = this.#classifyPollFailure(error);
        if (outcome.kind === "continue") {
          if (outcome.intervalSeconds !== undefined) {
            intervalSeconds = outcome.intervalSeconds;
          }
          continue;
        }
        if (outcome.kind === "terminal") {
          await this.#terminatePendingGrant(outcome.clearedReason, "not_connected");
          return;
        }
        this.#deps.onStateChange("offline", outcome.code);
        return;
      }
      if (this.#stopRequested || this.#deps.settings.pending_grant === null) {
        // A cancel or unload raced the in-flight poll. The terminating path
        // already owns the record and the state, so the late result is
        // discarded — nothing is written, no credential is adopted — and the
        // truthful cancel state is re-asserted.
        if (this.#deps.settings.pending_grant === null) {
          this.#deps.onStateChange("not_connected", null);
        }
        return;
      }
      writeActiveDeviceRecord(this.#deps.secretStore, this.#deps.recordName, {
        refresh_credential: exchange.refresh_credential,
        refresh_generation: exchange.refresh_generation,
        pending_rotation_id: null,
      });
      this.#deps.settings.pending_grant = null;
      this.#deps.settings.secret_record_name = this.#deps.recordName;
      await this.#deps.persistSettings();
      try {
        await this.#deps.onExchange(exchange);
      } catch (error) {
        // The active refresh credential remains safely stored for a later
        // retry, but a device without a verified policy snapshot must not be
        // presented as ready to capture or sync content. Closed-reason
        // surfacing C2 A1: the policy-trust bootstrap closes with a closed
        // `policy_*` reason token that now rides the seam instead of being
        // discarded; a foreign throw keeps the null detail (no raw text).
        const policyReason =
          error instanceof PolicyVerificationError ? error.reason : null;
        this.#deps.onStateChange("offline", policyReason);
        return;
      }
      this.#deps.onStateChange("connected", null);
      return;
    }
  }

  /**
   * Classify one poll failure without side effects: pending and slow-down
   * carry the server's exact retry hint, terminal outcomes name their
   * tombstone reason, and everything recoverable is an offline finish that
   * preserves the record while carrying the closed code it closed on
   * (closed-reason surfacing C2 A5).
   */
  #classifyPollFailure(
    error: unknown,
  ):
    | { kind: "continue"; intervalSeconds?: number }
    | { kind: "terminal"; clearedReason: ClearedReason }
    | { kind: "offline"; code: string | null } {
    const authError = error as DeviceAuthError;
    if (
      authError?.code === "device_authorization_pending" ||
      authError?.code === "device_authorization_slow_down"
    ) {
      // Obey the server hint exactly and never poll faster than it allows.
      const retryHint = authError.retryAfterSeconds;
      if (retryHint === null || retryHint < 1) {
        return { kind: "continue" };
      }
      return { kind: "continue", intervalSeconds: retryHint };
    }
    if (authError?.code === "device_authorization_denied") {
      return { kind: "terminal", clearedReason: "grant_denied" };
    }
    if (authError?.code === "device_authorization_expired") {
      return { kind: "terminal", clearedReason: "grant_expired" };
    }
    if (
      authError?.code === "device_credential_invalid" ||
      authError?.code === "device_authorization_state_invalid"
    ) {
      return { kind: "terminal", clearedReason: "grant_invalid" };
    }
    return { kind: "offline", code: resolveDeviceAuthClosedCode(error) };
  }
}
