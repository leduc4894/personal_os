/**
 * The Obsidian plugin composition root (spec 19).
 *
 * This module only wires real adapters: Obsidian `requestUrl`, `Platform`,
 * the app SecretStorage, plugin data persistence and `window.open`. Every
 * behavior lives in the tested `./authentication` modules. At startup it
 * performs at most ONE bounded resume-or-refresh action and never starts a
 * background sync loop.
 */

import { Platform, Plugin, requestUrl } from "obsidian";

import {
  createDeviceApiTransport,
  parseServerOrigin,
  validateDeviceName,
} from "./authentication/contracts";
import type {
  ConnectionState,
  DeviceAuthenticationSettings,
  DeviceHttpRequest,
  DeviceHttpResponse,
  DeviceHttpTransport,
  PendingGrantSettings,
  SecretStorageRecordAdapter,
} from "./authentication/contracts";
import { DeviceAuthorizationController } from "./authentication/device-authorization";
import {
  DEVICE_CREDENTIAL_RECORD_NAME,
  isSecretRecordNameValid,
  readDeviceSecretRecord,
} from "./authentication/secret-storage-record";
import { DeviceAuthenticationSettingTab } from "./authentication/settings-tab";
import { DeviceTokenSession, resolveStartupAction } from "./authentication/token-session";

/**
 * The explicit development-build flag of spec 19. Production builds accept
 * HTTPS origins only; loopback HTTP requires flipping this constant in an
 * explicit local build.
 */
const ALLOW_LOOPBACK_HTTP_ORIGIN = false;

const DEFAULT_DEVICE_NAME = "Obsidian vault";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function createRequestUrlDeviceHttpTransport(): DeviceHttpTransport {
  return async (request: DeviceHttpRequest): Promise<DeviceHttpResponse> => {
    const result = await requestUrl({
      url: request.url,
      method: request.method,
      headers: { ...request.headers },
      body: request.body,
      throw: false,
    });
    return { status: result.status, bodyText: result.text };
  };
}

function resolvePlatformName(): string {
  if (Platform.isIosApp) {
    return "ios";
  }
  if (Platform.isAndroidApp) {
    return "android";
  }
  if (Platform.isWin) {
    return "windows";
  }
  if (Platform.isMacOS) {
    return "macos";
  }
  if (Platform.isLinux) {
    return "linux";
  }
  return Platform.isDesktop ? "desktop" : "mobile";
}

function normalizePendingGrant(
  value: unknown,
): PendingGrantSettings | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate["grant_id"] !== "string" ||
    typeof candidate["user_code"] !== "string" ||
    typeof candidate["verification_uri"] !== "string" ||
    typeof candidate["expires_at_epoch_seconds"] !== "number" ||
    typeof candidate["poll_interval_seconds"] !== "number"
  ) {
    return null;
  }
  return {
    grant_id: candidate["grant_id"],
    user_code: candidate["user_code"],
    verification_uri: candidate["verification_uri"],
    expires_at_epoch_seconds: candidate["expires_at_epoch_seconds"],
    poll_interval_seconds: candidate["poll_interval_seconds"],
  };
}

function normalizeSettings(loaded: unknown): DeviceAuthenticationSettings {
  const candidate = (typeof loaded === "object" && loaded !== null ? loaded : {}) as Record<
    string,
    unknown
  >;
  const loadedRecordName =
    typeof candidate["secret_record_name"] === "string" &&
    isSecretRecordNameValid(candidate["secret_record_name"])
      ? candidate["secret_record_name"]
      : null;
  const loadedClientId =
    typeof candidate["client_instance_id"] === "string" &&
    UUID_PATTERN.test(candidate["client_instance_id"])
      ? candidate["client_instance_id"]
      : null;
  return {
    server_origin: typeof candidate["server_origin"] === "string" ? candidate["server_origin"] : "",
    device_name:
      typeof candidate["device_name"] === "string"
        ? validateDeviceName(candidate["device_name"]) ?? DEFAULT_DEVICE_NAME
        : DEFAULT_DEVICE_NAME,
    client_instance_id: loadedClientId ?? crypto.randomUUID(),
    secret_record_name: loadedRecordName === null ? null : DEVICE_CREDENTIAL_RECORD_NAME,
    pending_grant: normalizePendingGrant(candidate["pending_grant"]),
  };
}

export default class KnowledgeWorkspacePlugin extends Plugin {
  #settings: DeviceAuthenticationSettings = {
    server_origin: "",
    device_name: DEFAULT_DEVICE_NAME,
    client_instance_id: "",
    secret_record_name: null,
    pending_grant: null,
  };
  #connectionState: ConnectionState = "not_connected";
  #statusDetail: string | null = null;
  #controller: DeviceAuthorizationController | null = null;
  #session: DeviceTokenSession | null = null;
  #settingTab: DeviceAuthenticationSettingTab | null = null;

  override async onload(): Promise<void> {
    this.#settings = normalizeSettings(await this.loadData());
    await this.#persistSettings();

    const secretStore = this.app.secretStorage;
    const transport = createDeviceApiTransport(
      createRequestUrlDeviceHttpTransport(),
      () =>
        parseServerOrigin(this.#settings.server_origin, {
          allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
        }) ?? "",
    );
    const session = new DeviceTokenSession({
      transport,
      secretStore,
      recordName: DEVICE_CREDENTIAL_RECORD_NAME,
      settings: this.#settings,
      persistSettings: () => this.#persistSettings(),
      createRotationId: () => crypto.randomUUID(),
      onStateChange: (state, detail) => this.#setConnectionState(state, detail),
    });
    const controller = new DeviceAuthorizationController({
      transport,
      secretStore,
      recordName: DEVICE_CREDENTIAL_RECORD_NAME,
      settings: this.#settings,
      persistSettings: () => this.#persistSettings(),
      clientIdentity: {
        platformClass: Platform.isDesktop ? "obsidian_desktop" : "obsidian_mobile",
        platformName: resolvePlatformName(),
        pluginVersion: this.manifest.version,
        clientInstanceId: this.#settings.client_instance_id,
      },
      allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
      openUrl: (url) => {
        window.open(url, "_blank");
      },
      delay: (milliseconds) =>
        new Promise<void>((resolve) => {
          window.setTimeout(resolve, milliseconds);
        }),
      nowEpochMs: () => Date.now(),
      onStateChange: (state, detail) => this.#setConnectionState(state, detail),
      onExchange: (exchange) => session.adoptExchange(exchange),
    });
    this.#session = session;
    this.#controller = controller;

    // The single bounded startup action of spec 19 — never a background loop.
    const startupRecord = readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME);
    const startupAction = resolveStartupAction(startupRecord);
    if (startupAction === "resume_pending_grant") {
      await controller.resumePendingGrant().catch(() => undefined);
    } else if (startupAction === "refresh_credential") {
      await session.refresh().catch(() => undefined);
    }

    this.#settingTab = new DeviceAuthenticationSettingTab(this.app, this, {
      getSnapshot: () => ({
        connectionState: this.#connectionState,
        statusDetail: this.#statusDetail,
        serverOrigin: this.#settings.server_origin,
        deviceName: this.#settings.device_name,
        hasPendingGrant: this.#settings.pending_grant !== null,
        hasActiveCredential: this.#resolveHasActiveCredential(secretStore),
      }),
      setServerOrigin: (origin) => {
        this.#settings.server_origin = origin;
        void this.#persistSettings();
      },
      setDeviceName: (name) => {
        this.#settings.device_name = name;
        void this.#persistSettings();
      },
      login: () => controller.login(),
      openBrowserAgain: () => controller.openBrowserAgain(),
      cancelPendingLogin: () => controller.cancelPendingLogin(),
      disconnect: () => session.disconnect(),
    });
    this.addSettingTab(this.#settingTab);
  }

  override onunload(): void {
    this.#controller?.stop();
    this.#session?.clearMemoryAccess();
  }

  #resolveHasActiveCredential(secretStore: SecretStorageRecordAdapter): boolean {
    return (
      this.#session !== null &&
      readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME)?.state === "active"
    );
  }

  #setConnectionState(state: ConnectionState, detail: string | null): void {
    this.#connectionState = state;
    this.#statusDetail = detail;
    this.#settingTab?.display();
  }

  async #persistSettings(): Promise<void> {
    await this.saveData(this.#settings);
  }
}
