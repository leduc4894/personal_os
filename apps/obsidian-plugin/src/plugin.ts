/**
 * The Obsidian plugin composition root (spec 19).
 *
 * This module only wires real adapters: Obsidian `requestUrl`, `Platform`,
 * the app SecretStorage, plugin data persistence and `window.open`. Every
 * behavior lives in the tested `./authentication` modules. At startup it
 * performs at most ONE bounded resume-or-refresh action and never starts a
 * background sync loop.
 */

import { Modal, Platform, Plugin, requestUrl, Setting, TFile } from "obsidian";
import type { TAbstractFile } from "obsidian";

import { createObsidianPolicyHttpTransport, createObsidianSyncHttpTransport } from "./api/obsidian-api-transport";
import { createRequestUrlTransport } from "./api/request-url-transport";
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
import { LifecycleCaptureImpl } from "./journal/lifecycle-capture";
import type {
  LifecycleVaultReader,
  VaultRenameTarget,
  VaultTargetFile,
} from "./journal/lifecycle-capture";
import { JournalQueueDriver } from "./journal/queue-driver";
import type { QueuePassOutcome, QueuePassSummary } from "./journal/queue-driver";
import { LifecycleDriverImpl } from "./journal/lifecycle-driver";
import { createRequestUrlLifecycleApi } from "./journal/lifecycle-api";
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
import type { JournalSyncStatusSnapshot, LifecycleBlockedReasonCode } from "./journal/status";
import type { LifecycleStateCounts } from "./journal/status";
import { loadVendoredSqliteEngine } from "./journal/sqlite-database";
import { createJournalSyncApi } from "./journal/sync-api";
import { createUuidv7Factory } from "./journal/uuidv7";
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
  #lifecycleCapture: LifecycleCaptureImpl | null = null;
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
          // Task 10 / fix round 1 I1: the redacted lifecycle surface
          // reaches the settings tab through the same projection.
          lifecycleStateCounts: syncStatus?.lifecycleStateCounts ?? null,
          pendingLifecycleEventCount: syncStatus?.pendingLifecycleEventCount ?? 0,
          failedAttemptCount: syncStatus?.failedAttemptCount ?? 0,
          lifecycleBlockedReasonCodes: syncStatus?.lifecycleBlockedReasonCodes ?? [],
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
      const createJournalId = createUuidv7Factory();
      const repository = new JournalRepository({
        database: journalDatabase,
        createId: createJournalId,
      });
      const vaultReader = this.#createCaptureVaultReader();
      const lifecycleVaultReader = this.#createLifecycleVaultReader(vaultReader);
      const lifecycleCapture = new LifecycleCaptureImpl({
        repository,
        lifecycle: repository.lifecycle,
        vaultReader: lifecycleVaultReader,
        createId: createJournalId,
        policyRevision: 1,
      });
      const capture = new JournalCapture({
        repository,
        vaultReader,
        policyGate: policySession,
        lifecycleCapture,
      });
      const lifecycleDriver = new LifecycleDriverImpl({
        repository,
        lifecycle: repository.lifecycle,
        api: createRequestUrlLifecycleApi({
          baseUrl:
            parseServerOrigin(this.#settings.server_origin, {
              allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
            }) ?? "",
          transport: createRequestUrlTransport((request) => requestUrl(request)),
          resolveAccessToken: () => session.accessCredential,
        }),
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
        lifecycleDriver,
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
      // Explicit restore surface (Task 10, spec 6.3 + 7.1): the user
      // picks one retained tombstone by its plugin-local id (the only
      // identity the journal actually retains for the row), confirms a
      // target path, and the lifecycle capture verifies the bytes hash
      // against the file's last-committed fingerprint before recording
      // a restore event. The surface never logs paths, locators, source
      // ids, tokens or fingerprints — failures surface as the closed
      // `journal_mutation_failed` `JournalStoreErrorReason` and the Sync
      // status is refreshed so the lifecycle state transitions stay
      // visible.
      this.addCommand({
        id: "restore-selected-tombstone",
        name: "Restore selected tombstone",
        callback: () => {
          void this.#runRestoreSelectedTombstone();
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
   * The explicit-restore command callback (Task 10, spec 6.3 + 7.1):
   * show the picker for retained tombstones, confirm the target path
   * with the user and call the lifecycle capture port to validate the
   * bytes hash and record the restore event. Failures surface as the
   * closed `journal_mutation_failed` `JournalStoreErrorReason` and
   * never reach the console; the sync status is refreshed on both
   * branches so the redacted status surface reflects the new lifecycle
   * state.
   */
  async #runRestoreSelectedTombstone(): Promise<void> {
    const lifecycleCapture = this.#lifecycleCapture;
    const repository = this.#queueRepository;
    if (lifecycleCapture === null || repository === null) {
      return;
    }
    const selection = await this.#pickTombstonedFile(repository);
    if (selection === null) {
      return;
    }
    const targetPath = await this.#promptForRestoreTargetPath();
    if (targetPath === null) {
      return;
    }
    const confirmed = await this.#confirmRestoreRequest(selection, targetPath);
    if (!confirmed) {
      return;
    }
    try {
      await lifecycleCapture.requestRestore(selection.localFileId, targetPath);
    } catch {
      // The lifecycle capture closes the failure as the closed
      // `journal_mutation_failed` `JournalStoreErrorReason`; the
      // rejected bytes hash, missing retained mapping, missing open
      // tombstone or missing delete predecessor stays local. The sync
      // status refresh is the single source of truth for the user.
    }
    this.#refreshSyncStatus();
  }

  /**
   * The narrow picker for retained tombstones. Each candidate carries
   * only its plugin-local `localFileId` and a short safe label — the
   * underlying path never reaches the picker text. The picker closes
   * with `null` when the user dismisses the modal without a choice.
   */
  #pickTombstonedFile(
    repository: JournalRepository,
  ): Promise<{ readonly localFileId: string; readonly shortLabel: string } | null> {
    return new Promise((resolve) => {
      const localFileIds = repository.readTombstonedLocalFileIds();
      const candidates: readonly { readonly localFileId: string; readonly shortLabel: string }[] =
        localFileIds.map((localFileId) => ({
          localFileId,
          shortLabel: `Tombstone #${localFileId.slice(-8)}`,
        }));
      if (candidates.length === 0) {
        new NoticeModal(
          this.app,
          "No retained tombstones",
          "There are no tombstoned files eligible for restore right now.",
        ).open();
        resolve(null);
        return;
      }
      const modal = new SuggestModal<{ readonly localFileId: string; readonly shortLabel: string }>(
        this.app,
        candidates,
        (item) => item.shortLabel,
      );
      modal.setPlaceholder("Pick a tombstone to restore");
      modal.onChooseItem = (item) => resolve(item);
      modal.onClose = () => resolve(null);
      modal.open();
    });
  }

  /** The narrow text prompt for the restore target path. */
  #promptForRestoreTargetPath(): Promise<string | null> {
    return new Promise((resolve) => {
      const modal = new TextPromptModal(
        this.app,
        "Restore target path",
        "Vault path the restored bytes should occupy (no path is recorded yet).",
        (value) => resolve(value),
        () => resolve(null),
      );
      modal.open();
    });
  }

  /** The narrow confirmation modal of an explicit restore request. */
  #confirmRestoreRequest(
    selection: { readonly localFileId: string; readonly shortLabel: string },
    targetPath: string,
  ): Promise<boolean> {
    return new Promise((resolve) => {
      const modal = new ConfirmModal(
        this.app,
        "Confirm restore",
        [
          `Restore ${selection.shortLabel} to the chosen Vault path?`,
          "The bytes hash must match the server-committed content hash or the restore is rejected.",
        ].join("\n"),
        () => resolve(true),
        () => resolve(false),
      );
      void targetPath;
      modal.open();
    });
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
   * journal reconcile flag, the live credential fact, the pass facts and
   * (Task 10) the redacted source-lifecycle surface (state histogram,
   * pending-event count, failed-attempt count, closed blocker codes). All
   * five reads share one `try { … } catch { return null }` boundary so an
   * unreadable journal renders no status rather than a partial one.
   */
  #projectSyncStatus(): JournalSyncStatusSnapshot | null {
    const repository = this.#queueRepository;
    if (repository === null) {
      return null;
    }
    let eventStateErrorCounts: readonly JournalEventStateErrorCount[];
    let lifecycleStateCounts: LifecycleStateCounts;
    let pendingLifecycleEventCount: number;
    let failedAttemptCount: number;
    let lifecycleBlockedReasonCodes: readonly LifecycleBlockedReasonCode[];
    try {
      eventStateErrorCounts = repository.readEventStateErrorCounts();
      lifecycleStateCounts = repository.readLifecycleStateCounts();
      pendingLifecycleEventCount = repository.countPendingLifecycleEvents();
      failedAttemptCount = repository.countFailedAttempts();
      lifecycleBlockedReasonCodes = repository.readLifecycleBlockedReasonCodes() as readonly LifecycleBlockedReasonCode[];
    } catch {
      // The journal store is closed or unreadable: render no status rather
      // than a wrong one (the fail-closed rule of the journal design).
      return null;
    }
    return projectJournalSyncStatus({
      isReconcileRequired: this.#journalPersistence?.isReconcileRequired ?? false,
      eventStateErrorCounts,
      lifecycleStateCounts,
      pendingLifecycleEventCount,
      failedAttemptCount,
      lifecycleBlockedReasonCodes,
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

// --- task-10 modal helpers (composition only, no behaviour) ---------------------------------

/**
 * A minimal Obsidian `SuggestModal<T>` reimplementation that the
 * compose-only plugin layer can carry without depending on the optional
 * `obsidian.d.ts` augmentation. The picker shows the safe label and
 * resolves the typed item through {@link SuggestModal.onChooseItem}.
 */
class SuggestModal<T> extends Modal {
  readonly #items: readonly T[];
  readonly #render: (item: T) => string;
  #placeholder = "Search…";
  onChooseItem: (item: T) => void = () => undefined;

  constructor(app: import("obsidian").App, items: readonly T[], render: (item: T) => string) {
    super(app);
    this.#items = items;
    this.#render = render;
  }

  setPlaceholder(text: string): void {
    this.#placeholder = text;
  }

  override onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("p", { text: this.#placeholder });
    const list = contentEl.createEl("ul");
    for (const item of this.#items) {
      const row = list.createEl("li", { text: this.#render(item) });
      row.style.cursor = "pointer";
      row.addEventListener("click", () => {
        this.close();
        this.onChooseItem(item);
      });
    }
  }
}

/** A minimal Obsidian `Modal` that resolves to a typed text value or null. */
class TextPromptModal extends Modal {
  readonly #title: string;
  readonly #description: string;
  readonly #accept: (value: string) => void;
  readonly #reject: () => void;
  #inputValue = "";

  constructor(
    app: import("obsidian").App,
    title: string,
    description: string,
    accept: (value: string) => void,
    reject: () => void,
  ) {
    super(app);
    this.#title = title;
    this.#description = description;
    this.#accept = accept;
    this.#reject = reject;
  }

  override onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("p", { text: this.#description });
    const input = contentEl.createEl("input");
    input.type = "text";
    input.style.width = "100%";
    input.addEventListener("input", () => {
      this.#inputValue = input.value;
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        this.close();
        this.#accept(this.#inputValue);
      }
    });
    new Setting(contentEl)
      .addButton((button) =>
        button
          .setButtonText("Restore")
          .setCta()
          .onClick(() => {
            this.close();
            this.#accept(this.#inputValue);
          }),
      )
      .addButton((button) =>
        button
          .setButtonText("Cancel")
          .onClick(() => {
            this.close();
            this.#reject();
          }),
      );
    this.onClose = () => this.#reject();
  }
}

/** A minimal two-button confirmation modal that resolves to a boolean. */
class ConfirmModal extends Modal {
  readonly #title: string;
  readonly #body: string;
  readonly #accept: () => void;
  readonly #reject: () => void;

  constructor(
    app: import("obsidian").App,
    title: string,
    body: string,
    accept: () => void,
    reject: () => void,
  ) {
    super(app);
    this.#title = title;
    this.#body = body;
    this.#accept = accept;
    this.#reject = reject;
  }

  override onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("p", { text: this.#body });
    new Setting(contentEl)
      .addButton((button) =>
        button
          .setButtonText("Restore")
          .setCta()
          .onClick(() => {
            this.close();
            this.#accept();
          }),
      )
      .addButton((button) =>
        button
          .setButtonText("Cancel")
          .onClick(() => {
            this.close();
            this.#reject();
          }),
      );
    this.onClose = () => this.#reject();
  }
}

/** A minimal read-only notice modal that closes on its own. */
class NoticeModal extends Modal {
  readonly #title: string;
  readonly #body: string;

  constructor(app: import("obsidian").App, title: string, body: string) {
    super(app);
    this.#title = title;
    this.#body = body;
  }

  override onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("p", { text: this.#body });
    new Setting(contentEl).addButton((button) =>
      button.setButtonText("Close").setCta().onClick(() => this.close()),
    );
  }
}
