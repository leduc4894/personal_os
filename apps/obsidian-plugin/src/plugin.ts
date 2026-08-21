/**
 * The Obsidian plugin composition root (spec 19).
 *
 * This module only wires real adapters: Obsidian `requestUrl`, `Platform`,
 * the app SecretStorage, plugin data persistence and `window.open`. Every
 * behavior lives in the tested `./authentication` modules. At startup it
 * performs at most ONE bounded resume-or-refresh action and never starts a
 * background sync loop.
 */

import { Modal, Platform, Plugin, requestUrl, Setting, TAbstractFile, TFile } from "obsidian";

import { createObsidianPolicyHttpTransport, createObsidianSyncHttpTransport } from "./api/obsidian-api-transport";
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
import { JournalCapture } from "./journal/capture";
import type { CaptureVaultReader } from "./journal/capture";
import { LifecycleCapture } from "./journal/lifecycle-capture";
import type {
  LifecycleVaultReader,
  VaultRenameTarget,
  VaultTargetFile,
} from "./journal/lifecycle-capture";
import { JournalQueueDriver } from "./journal/queue-driver";
import type { QueuePassOutcome, QueuePassSummary } from "./journal/queue-driver";
import { createVaultPluginJournalStore, JournalPersistence } from "./journal/persistence";
import type { JournalFileStore } from "./journal/persistence";
import { JournalRepository } from "./journal/repository";
import type { JournalEventStateErrorCount, JournalRepositoryDatabase } from "./journal/repository";
import {
  projectJournalSyncStatus,
  renderJournalSyncStatusText,
  syncBlockerGuidanceLines,
  SYNC_STATUS_TEXT,
} from "./journal/status";
import type { JournalSyncStatusSnapshot } from "./journal/status";
import { loadVendoredSqliteEngine } from "./journal/sqlite-database";
import { createJournalSyncApi } from "./journal/sync-api";
import { PolicySession } from "./exclusion-policy/policy-session";
import type { PolicyCacheAdapter } from "./exclusion-policy/policy-cache";
import type { PolicyIntegrityState } from "./exclusion-policy/contracts";

/**
 * The explicit development-build flag of spec 19. Production builds accept
 * HTTPS origins only; loopback HTTP requires flipping this constant in an
 * explicit local build.
 */
const ALLOW_LOOPBACK_HTTP_ORIGIN = false;

const DEFAULT_DEVICE_NAME = "Obsidian vault";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The single plugin-data member holding the versioned exclusion-policy cache
 * record (spec 18). Settings and the policy cache share one document, so
 * every persist path merges instead of replacing.
 */
const POLICY_CACHE_PLUGIN_DATA_KEY = "policy_cache";

/**
 * The vendored sql.js WebAssembly engine file, deployed next to the plugin
 * manifest inside the Vault's configured plugin directory (journal design
 * 6.1: the only permitted database engine, loaded lazily at capture start).
 */
const JOURNAL_ENGINE_WASM_FILE_NAME = "sql-wasm.wasm";

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
  #policySession: PolicySession | null = null;
  #policyState: PolicyIntegrityState = "policy_not_initialized";
  #journalPersistence: JournalPersistence | null = null;
  #capture: JournalCapture | null = null;
  #lifecycleCapture: LifecycleCapture | null = null;
  #queueDriver: JournalQueueDriver | null = null;
  #queueRepository: JournalRepository | null = null;
  #isQueuePassActive = false;
  #lastQueuePassOutcome: QueuePassOutcome | null = null;
  #syncStatusBarItem: HTMLElement | null = null;

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
    const policySession = new PolicySession({
      http: createObsidianPolicyHttpTransport(),
      resolveOrigin: () =>
        parseServerOrigin(this.#settings.server_origin, {
          allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
        }) ?? "",
      getAccessToken: () => session.accessCredential,
      cache: this.#createPolicyCacheAdapter(),
      onStateChange: (state) => {
        this.#policyState = state;
      },
    });
    // Offline startup: restore the last verified policy cache from plugin
    // data (local-only, bounded) before any network work is considered.
    await policySession.restoreFromCache();
    this.#policyState = policySession.state;
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
      onExchange: (exchange) => {
        session.adoptExchange(exchange);
        // Initial policy trust exists ONLY immediately after the
        // authenticated onboarding exchange (spec 13.2).
        void policySession.adoptOnboardingTrust().catch(() => undefined);
      },
    });
    this.#session = session;
    this.#controller = controller;
    this.#policySession = policySession;

    this.#settingTab = new DeviceAuthenticationSettingTab(this.app, this, {
      getSnapshot: () => {
        const syncStatus = this.#projectSyncStatus();
        return {
          connectionState: this.#connectionState,
          statusDetail: this.#statusDetail,
          serverOrigin: this.#settings.server_origin,
          deviceName: this.#settings.device_name,
          hasPendingGrant: this.#settings.pending_grant !== null,
          hasActiveCredential: this.#resolveHasActiveCredential(secretStore),
          syncStatusText:
            syncStatus === null ? null : SYNC_STATUS_TEXT[syncStatus.kind],
          syncBlockerGuidance:
            syncStatus === null ? [] : [...syncBlockerGuidanceLines(syncStatus)],
        };
      },
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

    // The settings tab is registered before any bounded startup work so the
    // spec-19 affordances (Cancel pending login, Open browser again) stay
    // reachable while a pending grant resumes. The single bounded startup
    // action then runs fire-and-forget — never awaited in onload — and never
    // starts a background sync loop. The crash-window reconciliation is
    // local-only (one settings persist) and precedes the refresh.
    const startupRecord = readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME);
    const startupAction = resolveStartupAction(startupRecord);
    if (startupAction === "resume_pending_grant") {
      void controller.resumePendingGrant().catch(() => undefined);
    } else {
      await controller.reconcileCrashWindow();
      if (startupAction === "refresh_credential") {
        // Policy keyset/snapshot are fetched only AFTER a successful token
        // refresh (spec 18), still fire-and-forget and never awaited here.
        void session.refresh()
          .then(() => policySession.refresh())
          .catch(() => undefined);
      }
    }

    // Journal recovery runs BEFORE any capture listener exists (journal
    // design 7.1); a failed recovery fails closed — the plugin keeps
    // editing alive and simply captures nothing.
    await this.#startJournalCapture();
  }

  override onunload(): void {
    // The queue driver stops FIRST (spec 8): no new pass starts and every
    // late in-flight requestUrl result is discarded before the journal and
    // the memory credential go away.
    this.#queueDriver?.stop();
    this.#queueDriver = null;
    this.#lifecycleCapture?.dispose();
    this.#lifecycleCapture = null;
    this.#capture?.dispose();
    this.#capture = null;
    // Safe unload (spec 11): every journal mutation already persisted its
    // own verified generation, so the final flush attempt is synchronous
    // and bounded — it records whether a commit is still in flight and
    // never blocks unload on async generation publishing. An interrupted
    // commit recovers from the newest verified generation on the next open.
    this.#journalPersistence?.attemptFinalFlush();
    this.#journalPersistence?.close();
    this.#journalPersistence = null;
    this.#queueRepository = null;
    this.#syncStatusBarItem = null;
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
    // A credential arriving or leaving changes the login-required verdict of
    // the sync status projection (spec 11).
    this.#refreshSyncStatus();
  }

  async #persistSettings(): Promise<void> {
    // Merge into the single plugin-data document so the versioned policy
    // cache record survives settings persistence (spec 18 storage adapter).
    const loaded = (await this.loadData()) as Record<string, unknown> | null;
    await this.saveData({ ...(loaded ?? {}), ...this.#settings });
  }

  /**
   * The narrow journal binary store of the journal design (6.1): journal
   * generations resolve through the Vault's configured plugin directory
   * (`Vault.configDir` + the manifest id) and the adapter's binary methods —
   * never a hard-coded config-directory name. Composition only; the journal
   * persistence layer itself is injected and tested in `./journal`.
   */
  createJournalFileStore(): JournalFileStore {
    return createVaultPluginJournalStore(this.app, this.manifest.id);
  }

  /**
   * Composition-only journal capture and queue wiring (journal design 7.1,
   * 8): load the vendored engine, run journal recovery, then — and only
   * then — register the Vault listeners, the bounded foreground queue
   * driver and the two sync commands. Every behavior lives in the tested
   * `./journal` modules; this method only binds real adapters.
   */
  async #startJournalCapture(): Promise<void> {
    const policySession = this.#policySession;
    const session = this.#session;
    if (policySession === null || session === null) {
      return;
    }
    try {
      const engineModule = await loadVendoredSqliteEngine({
        wasmBinary: await this.#readJournalEngineWasmBinary(),
      });
      const persistence = new JournalPersistence({
        fileStore: this.createJournalFileStore(),
        engineModule,
      });
      await persistence.open();
      const journalDatabase: JournalRepositoryDatabase = {
        runSerializedMutation(operation) {
          return persistence.commitGeneration(operation);
        },
        readAll(sql) {
          return persistence.readAll(sql);
        },
      };
      const repository = new JournalRepository({ database: journalDatabase });
      const vaultReader = this.#createCaptureVaultReader();
      const lifecycleVaultReader = this.#createLifecycleVaultReader(vaultReader);
      const lifecycleCapture = new LifecycleCapture({
        repository,
        lifecycle: repository.lifecycle,
        vaultReader: lifecycleVaultReader,
        policyRevision: 1,
      });
      const capture = new JournalCapture({
        repository,
        vaultReader,
        policyGate: policySession,
        lifecycleCapture,
      });
      const queueDriver = new JournalQueueDriver({
        repository,
        syncApi: createJournalSyncApi({
          transport: createObsidianSyncHttpTransport(),
          resolveOrigin: () =>
            parseServerOrigin(this.#settings.server_origin, {
              allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
            }) ?? "",
          getAccessToken: () => session.accessCredential,
        }),
        fileBytesReader: vaultReader,
        refreshAccessToken: () => session.refresh(),
      });
      this.registerEvent(
        this.app.vault.on("create", (file) => {
          // The pass follows the settled admission (250 ms settle re-reads the
          // bytes): running it at event time would find an empty journal.
          void capture.notifyPathChanged(file.path).then(() => {
            void this.#runBoundedQueuePass();
          });
        }),
      );
      this.registerEvent(
        this.app.vault.on("modify", (file) => {
          void capture.notifyPathChanged(file.path).then(() => {
            void this.#runBoundedQueuePass();
          });
        }),
      );
      this.registerEvent(
        this.app.vault.on("delete", (file) => {
          void capture.notifyPathDeleted(this.#toVaultTargetFile(file));
        }),
      );
      this.registerEvent(
        this.app.vault.on("rename", (file, oldPath) => {
          void capture.notifyPathRenamed(this.#toVaultRenameTarget(file), oldPath);
        }),
      );
      this.addCommand({
        id: "sync-now",
        name: "Sync now",
        callback: () => {
          void this.#runBoundedQueuePass();
        },
      });
      this.addCommand({
        id: "sync-existing-files",
        name: "Sync existing files",
        callback: () => {
          void this.#runExistingFilesScan();
        },
      });
      this.#journalPersistence = persistence;
      this.#capture = capture;
      this.#queueDriver = queueDriver;
      this.#queueRepository = repository;
      this.#lifecycleCapture = lifecycleCapture;
      // Plugin load after safe recovery is the first bounded foreground
      // trigger (spec 8): fire-and-forget, never awaited in onload. A
      // reconcile-required journal stops the driver inside the status
      // refresh before this pass can start (spec 11).
      void this.#runBoundedQueuePass();
    } catch {
      // Engine or recovery failure is fail-closed: no capture surface is
      // registered and Vault editing is never touched.
    }
  }

  /** The one confirmed command callback; the scan itself is task-4 capture. */
  async #runExistingFilesScan(): Promise<void> {
    const capture = this.#capture;
    if (capture === null) {
      return;
    }
    await capture
      .runExistingFilesScan({ confirm: () => this.#confirmExistingFilesScan() })
      .catch(() => undefined);
    this.#refreshSyncStatus();
  }

  /**
   * The single foreground pass trigger (spec 8, 11): plugin load, a Vault
   * event and `Sync now` all funnel through here, so the status projection
   * sees the active pass and every finished pass outcome. The trigger
   * itself is never awaited — onload, the Vault listeners and the command
   * callbacks stay synchronous and fire-and-forget.
   */
  async #runBoundedQueuePass(): Promise<void> {
    const driver = this.#queueDriver;
    if (driver === null) {
      return;
    }
    this.#isQueuePassActive = true;
    this.#refreshSyncStatus();
    let summary: QueuePassSummary;
    try {
      summary = await driver.requestPass();
    } catch {
      // The driver never lets a trigger crash; a local failure still ends
      // this wrapper's view of the pass.
      summary = { outcome: "completed", processedEventCount: 0 };
    }
    if (summary.outcome !== "pass_already_running") {
      // Only the invocation that actually ran the pass clears the active
      // flag; a trigger that found a running pass leaves it untouched.
      this.#isQueuePassActive = false;
      this.#lastQueuePassOutcome = summary.outcome;
    }
    this.#refreshSyncStatus();
  }

  /**
   * Project and render the closed sync status (spec 11): the journal
   * histogram, credential existence and pass facts in; one of the six
   * closed values with counts out — the small status-bar surface and the
   * settings snapshot. A reconcile-required journal is a hard stop: the
   * driver is stopped here and the child-6 guidance explains why nothing
   * syncs until repair.
   */
  #refreshSyncStatus(): void {
    const snapshot = this.#projectSyncStatus();
    if (snapshot === null) {
      return;
    }
    if (snapshot.kind === "reconcile_required") {
      this.#queueDriver?.stop();
    }
    const statusBarItem = this.#syncStatusBarItem ?? this.addStatusBarItem();
    this.#syncStatusBarItem = statusBarItem;
    statusBarItem.setText(renderJournalSyncStatusText(snapshot));
  }

  /**
   * The closed projection input, or null while no journal runs: the
   * composition reads the redacted repository histogram plus the sticky
   * journal reconcile flag, the live credential fact and the pass facts.
   */
  #projectSyncStatus(): JournalSyncStatusSnapshot | null {
    const repository = this.#queueRepository;
    if (repository === null) {
      return null;
    }
    let eventStateErrorCounts: readonly JournalEventStateErrorCount[];
    try {
      eventStateErrorCounts = repository.readEventStateErrorCounts();
    } catch {
      // The journal store is closed or unreadable: render no status rather
      // than a wrong one (the fail-closed rule of the journal design).
      return null;
    }
    return projectJournalSyncStatus({
      isReconcileRequired: this.#journalPersistence?.isReconcileRequired ?? false,
      eventStateErrorCounts,
      hasAccessCredential: this.#session?.accessCredential != null,
      isQueuePassActive: this.#isQueuePassActive,
      lastQueuePassOutcome: this.#lastQueuePassOutcome,
    });
  }

  /**
   * The confirmation modal of `Sync existing files` (journal design 7.1):
   * nothing is queued until the user confirms. The message stays free of
   * paths and any other Vault detail.
   */
  #confirmExistingFilesScan(): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      const modal = new Modal(this.app);
      modal.titleEl.setText("Sync existing files");
      modal.contentEl.createEl("p", {
        text: "Queue the current regular Vault files (each at most 16 MiB) for sync in bounded batches?",
      });
      new Setting(modal.contentEl)
        .addButton((button) =>
          button
            .setButtonText("Sync")
            .setCta()
            .onClick(() => {
              modal.close();
              resolve(true);
            }),
        )
        .addButton((button) =>
          button
            .setButtonText("Cancel")
            .onClick(() => {
              modal.close();
              resolve(false);
            }),
        );
      modal.onClose = () => resolve(false);
      modal.open();
    });
  }

  /**
   * The narrow read-only Vault slice capture needs (journal design 7.1):
   * regular files only, resolved through the structural Obsidian surface.
   */
  #createCaptureVaultReader(): CaptureVaultReader {
    const vault = this.app.vault;
    return {
      readRegularFileBytes: async (normalizedPath) => {
        const file = vault.getAbstractFileByPath(normalizedPath);
        if (!(file instanceof TFile)) {
          return null;
        }
        return new Uint8Array(await vault.readBinary(file));
      },
      listRegularFilePaths: async () =>
        vault.getFiles().map((file) => file.path).sort(),
    };
  }

  /**
   * The narrow read-only Vault slice the lifecycle capture needs
   * (journal design 6.3, 7.1): just the current bytes of one regular
   * file for tombstone verification on restore, layered on top of the
   * capture reader so plugin composition stays in one place.
   */
  #createLifecycleVaultReader(captureReader: CaptureVaultReader): LifecycleVaultReader {
    return {
      readRegularFileBytes: (normalizedPath) => captureReader.readRegularFileBytes(normalizedPath),
    };
  }

  /** Narrow an Obsidian file into the lifecycle capture's rename target. */
  #toVaultRenameTarget(file: TAbstractFile): VaultRenameTarget {
    return this.#toVaultTargetFile(file) as VaultRenameTarget;
  }

  /** Narrow an Obsidian file into the lifecycle capture's delete target. */
  #toVaultTargetFile(file: TAbstractFile): VaultTargetFile {
    const parentPath = file.parent?.path ?? null;
    return {
      path: file.path,
      parent: parentPath === null ? null : { path: parentPath },
    };
  }

  /** Read the vendored engine bytes from the configured plugin directory. */
  async #readJournalEngineWasmBinary(): Promise<ArrayBuffer> {
    const { configDir, adapter } = this.app.vault;
    const pluginDirectory = [configDir, "plugins", this.manifest.id]
      .filter((segment) => segment.length > 0)
      .join("/");
    return adapter.readBinary(`${pluginDirectory}/${JOURNAL_ENGINE_WASM_FILE_NAME}`);
  }

  /**
   * The narrow settings adapter of spec 18: the accepted policy state lives
   * in ONE versioned plugin-data record under a reserved member. No Vault
   * content, credential or diagnostic path ever enters this record.
   */
  #createPolicyCacheAdapter(): PolicyCacheAdapter {
    return {
      readPolicyCacheRecord: async (): Promise<unknown> => {
        const loaded = (await this.loadData()) as Record<string, unknown> | null;
        return loaded === null ? null : (loaded[POLICY_CACHE_PLUGIN_DATA_KEY] ?? null);
      },
      writePolicyCacheRecord: async (record: unknown): Promise<void> => {
        const loaded = (await this.loadData()) as Record<string, unknown> | null;
        await this.saveData({
          ...(loaded ?? {}),
          [POLICY_CACHE_PLUGIN_DATA_KEY]: record,
        });
      },
    };
  }
}
