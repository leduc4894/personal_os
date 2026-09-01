/**
 * The Obsidian plugin composition root (spec 19).
 *
 * This module only wires real adapters: Obsidian `requestUrl`, `Platform`,
 * the app SecretStorage, plugin data persistence and `window.open`. Every
 * behavior lives in the tested `./authentication` modules. At startup it
 * performs at most ONE bounded resume-or-refresh action and never starts a
 * background sync loop.
 */

import { Modal, Notice, Platform, Plugin, requestUrl, Setting, TFile } from "obsidian";
import type { TAbstractFile } from "obsidian";

import { createObsidianDeviceSyncHttpTransport, createObsidianPolicyHttpTransport, createObsidianSyncHttpTransport } from "./api/obsidian-api-transport";
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
import {
  AutomaticSnapshotCoordinator,
  CoalescingQueuePassDispatcher,
  refreshVerifiedPolicyAndRequestSnapshot,
} from "./journal/automatic-snapshot";
import type { AutomaticSnapshotReason } from "./journal/automatic-snapshot";
import { LifecycleCaptureImpl } from "./journal/lifecycle-capture";
import type { RestoreReservationRefusal, RestoreReservationResult } from "./journal/lifecycle-contracts";
import type {
  LifecycleVaultReader,
  VaultRenameTarget,
  VaultTargetFile,
} from "./journal/lifecycle-capture";
import type { MultipartUploadPlatform } from "./journal/multipart-upload";
import { JournalQueueDriver } from "./journal/queue-driver";
import type { QueuePassOutcome, QueuePassSummary } from "./journal/queue-driver";
import { LifecycleDriverImpl } from "./journal/lifecycle-driver";
import { createRequestUrlLifecycleApi } from "./journal/lifecycle-api";
import { createVaultPluginJournalStore, JournalPersistence } from "./journal/persistence";
import type { JournalFileStore } from "./journal/persistence";
import { JournalRepository } from "./journal/repository";
import { ConfirmModal, PreformattedTextModal, SuggestModal, TextPromptModal } from "./restore-modals";
import type { JournalEventStateErrorCount, JournalRepositoryDatabase } from "./journal/repository";
import type { LocalNoteSyncStatus } from "./journal/note-status";
import {
  projectJournalSyncStatus,
  renderJournalSyncStatus,
  syncBlockerGuidanceLines,
  SYNC_STATUS_TEXT,
} from "./journal/status";
import type { JournalSyncStatusSnapshot, LifecycleBlockedReasonCode } from "./journal/status";
import type { LifecycleStateCounts } from "./journal/status";
import type { MultipartSessionStateCounts } from "./journal/status";
import type { MultipartSafeReasonToken } from "./journal/contracts";
import { JournalStoreError, loadVendoredSqliteEngine } from "./journal/sqlite-database";
import { createJournalFailureReporter } from "./journal/diagnostic-reporter";
import type { JournalFailureReporter } from "./journal/diagnostic-reporter";
import { createSyncDiagnosticsTrail } from "./journal/sync-diagnostics-trail";
import type {
  SyncDiagnosticClosedToken,
  SyncDiagnosticsTrail,
  SyncStartupStageToken,
} from "./journal/sync-diagnostics-trail";
import {
  SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT,
  deriveSyncStopReasonTokens,
  renderSyncDiagnosticsExportBlock,
} from "./journal/sync-diagnostics-export";
import { createJournalSyncApi } from "./journal/sync-api";
import {
  renderSyncSelfCheckJournalNotRunningText,
  renderSyncSelfCheckSummaryText,
  runSyncSelfCheck,
} from "./journal/sync-self-check";
import { createUuidv7Factory } from "./journal/uuidv7";
import { PolicySession } from "./exclusion-policy/policy-session";
import type { PolicyCacheAdapter } from "./exclusion-policy/policy-cache";
import type { PolicyIntegrityState } from "./exclusion-policy/contracts";
import { ConflictInboxModal } from "./conflicts/ConflictInboxModal";
import { createConflictApi } from "./conflicts/api";
import type { ConflictController } from "./conflicts/controller";
import { createConflictController } from "./conflicts/controller";
import { ConflictRepository } from "./conflicts/repository";
import {
  createConflictCanonicalOutcomeApplier,
  createConflictDiagnosticsTrailSink,
  createUnavailableVerifiedCandidateUploader,
  deriveConflictApplyStatusFacts,
  observeUnobservedConflictControllerFailures,
} from "./conflicts/composition";
import type { ConflictCompositionDiagnosticsSink } from "./conflicts/composition";
import {
  AtomicVaultWriterImpl,
  createStructuralVaultMutationSeam,
} from "./device-sync/atomic-vault-writer";
import type {
  StructuralVaultAdapterSurface,
  StructuralVaultSurface,
} from "./device-sync/atomic-vault-writer";
import { createDeviceSyncApi } from "./device-sync/api";
import { createDeviceSyncDiagnostics } from "./device-sync/diagnostics";
import { createManifestCapture } from "./device-sync/manifest-capture";
import {
  createManifestReconciler,
  createManifestReconcilerJournal,
} from "./device-sync/manifest-reconciler";
import { createRemoteEventApplier } from "./device-sync/remote-event-applier";
import { DeviceSyncRepository } from "./device-sync/repository";
import { renderDeviceSyncStatusText } from "./device-sync/status";
import type { DeviceSyncStatus } from "./device-sync/status";
import { createSyncCoordinator } from "./device-sync/sync-coordinator";
import type { SyncCoordinator, SyncTrigger } from "./device-sync/sync-coordinator";

/**
 * The explicit development-build flag of spec 19. Production builds accept
 * HTTPS origins only; loopback HTTP requires flipping this constant in an
 * explicit local build.
 */
const ALLOW_LOOPBACK_HTTP_ORIGIN = false;

/**
 * The small safety margin the one-shot scheduled retry trigger adds on top
 * of the earliest pending retry deadline (fix round 2 D4), so the timer
 * never fires while the parked event is still one clock tick shy of
 * eligibility.
 */
const SCHEDULED_RETRY_PASS_SAFETY_MARGIN_MS = 250;

/**
 * The bound of the pre-trail startup-failure buffer (closed-reason
 * surfacing C1 P4): the two fire-and-forget startup chains can reject
 * before the trail sidecar is loaded; the buffer holds at most this many
 * token lists and the oldest are dropped beyond it.
 */
const MAX_BUFFERED_STARTUP_FAILURE_ENTRIES = 8;

const DEFAULT_DEVICE_NAME = "Obsidian vault";

/**
 * The closed, path-free Notice texts of the three explicit-restore
 * reservation refusals. The diagnostics trail carries the same closed
 * token through the failure reporter; no path, locator or identifier
 * ever reaches a Notice.
 */
const RESTORE_RESERVATION_REFUSAL_NOTICES: Record<
  RestoreReservationRefusal,
  string
> = {
  restore_target_occupied:
    "Restore refused: the target path is already occupied. Choose another target.",
  restore_target_busy:
    "Restore postponed: an upload for the target path is in flight. Try again shortly.",
  restore_already_pending:
    "A restore for this tombstone is already in progress. Wait for it to finish.",
};
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

/**
 * The platform class of the multipart transport (child 7 spec 4): the same
 * Desktop/Mobile discrimination the composition already applies — Desktop
 * earns three part-PUT permits, every non-Desktop runtime (phone or tablet)
 * stays under the hard two-permit Mobile cap.
 */
function resolveMultipartPlatformClass(): MultipartUploadPlatform {
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
  // Fix round 1 (blocker A): the server-minted device id the grant
  // exchange delivered. It round-trips behind the same UUID gate so a
  // restart keeps it; null before the first completed exchange (and then
  // the device-sync self-origin check simply never suppresses).
  const loadedServerDeviceId =
    typeof candidate["device_id"] === "string" && UUID_PATTERN.test(candidate["device_id"])
      ? candidate["device_id"]
      : null;
  return {
    server_origin: typeof candidate["server_origin"] === "string" ? candidate["server_origin"] : "",
    device_name:
      typeof candidate["device_name"] === "string"
        ? validateDeviceName(candidate["device_name"]) ?? DEFAULT_DEVICE_NAME
        : DEFAULT_DEVICE_NAME,
    client_instance_id: loadedClientId ?? crypto.randomUUID(),
    device_id: loadedServerDeviceId,
    // A valid stored record name round-trips unchanged (plugin hygiene,
    // 2026-08-16 §12): the earlier rewrite to the build-time constant
    // renamed every stored SecretStorage record on each load.
    secret_record_name: loadedRecordName,
    pending_grant: normalizePendingGrant(candidate["pending_grant"]),
  };
}

export default class KnowledgeWorkspacePlugin extends Plugin {
  #settings: DeviceAuthenticationSettings = {
    server_origin: "",
    device_name: DEFAULT_DEVICE_NAME,
    client_instance_id: "",
    device_id: null,
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
  /**
   * The durable diagnostics trail (sync error tracing task 1): retained on
   * the plugin so the settings snapshot and the copy-sync-diagnostics
   * export can read the tail, the counts and the derived stop reasons.
   */
  #diagnosticTrail: SyncDiagnosticsTrail | null = null;
  #journalFailureReporter: JournalFailureReporter | null = null;
  /**
   * Closed-reason surfacing C1 P1: the closed tokens of the last journal
   * startup failure (the failed stage token plus the closed store reason
   * when the throw was a store error), or null before the first failure —
   * never a fake success token. Feeds the settings snapshot and the
   * self-check's journal-not-running verdict.
   */
  #lastStartupFailureTokens: readonly SyncDiagnosticClosedToken[] | null = null;
  /**
   * Closed-reason surfacing C1 P4: startup-failure token lists recorded
   * before the trail sidecar is loaded; flushed into the trail right after
   * its load (bounded by MAX_BUFFERED_STARTUP_FAILURE_ENTRIES).
   */
  #bufferedStartupFailureTokenLists: (readonly SyncDiagnosticClosedToken[])[] = [];
  /** C1 P5: has the pending-count read swallow already been recorded? */
  #hasRecordedStatusReadFailure = false;
  /** C1 P5: has the note-status read swallow already been recorded? */
  #hasRecordedNoteStatusReadFailure = false;
  #hasReportedRetryScheduleReadFailure = false;
  #hasReportedSyncStatusReadFailure = false;
  #automaticSnapshotCoordinator: AutomaticSnapshotCoordinator | null = null;
  #boundedQueuePassDispatcher: CoalescingQueuePassDispatcher | null = null;
  /**
   * The single device-sync coordinator (task 12): owns every mutating
   * foreground network phase of the device cursor and manifest
   * reconciliation stack. Null before the journal starts or after unload.
   */
  #syncCoordinator: SyncCoordinator | null = null;
  #pendingAutomaticSnapshotReason: AutomaticSnapshotReason | null = null;
  #isQueuePassActive = false;
  #lastQueuePassOutcome: QueuePassOutcome | null = null;
  #syncStatusBarItem: HTMLElement | null = null;
  /** The one-shot scheduled retry trigger's outstanding timer (fix round 2 D4). */
  #scheduledRetryPassTimer: ReturnType<typeof setTimeout> | null = null;
  /** The deadline the outstanding timer fires at, or null when disarmed. */
  #scheduledRetryPassTargetEpochMs: number | null = null;
  /**
   * The composed Conflict Inbox controller (conflict inbox task 9): null
   * before the journal starts, after unload, and on a fail-closed journal
   * startup — the inbox command gates on exactly this fact.
   */
  #conflictController: ConflictController | null = null;
  /** The conflict composition's closed-token diagnostics sink (task 9). */
  #conflictDiagnostics: ConflictCompositionDiagnosticsSink | null = null;
  /** The durable no-byte conflict repair repository (task 9). */
  #conflictRepository: ConflictRepository | null = null;

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
      onExchange: async (exchange) => {
        // Fix round 1 (blocker A): persist the server-minted device id
        // (uuid7) the exchange delivered BEFORE the session adopts the
        // credential — it is the ONLY identity the device-event
        // origin_device_id namespace uses, and persisting it here makes
        // the self-origin evidence survive restarts. The session itself
        // keeps only the memory-only access credential.
        this.#settings.device_id = exchange.device_id;
        await this.#persistSettings();
        session.adoptExchange(exchange);
        // Initial policy trust exists ONLY immediately after the
        // authenticated onboarding exchange (spec 13.2). The controller
        // delays the Connected state until this has completed, preventing a
        // capture from being fail-closed against an uninitialised policy.
        await policySession.adoptOnboardingTrust();
        this.#requestAutomaticSnapshot("policy_accepted");
      },
    });
    this.#session = session;
    this.#controller = controller;
    this.#policySession = policySession;

    this.#settingTab = new DeviceAuthenticationSettingTab(this.app, this, {
      getSnapshot: () => {
        const syncStatus = this.#projectSyncStatus();
        // Fix round 5: the closed-token journal diagnostics of the two
        // swallowed-failure surfaces (pass-loop journal failures and
        // generation publish failures) reach the settings tab here.
        const generationPublishFailures =
          this.#journalPersistence?.readGenerationPublishFailureSummary() ?? null;
        // Sync error tracing task 2: the durable trail feeds the settings
        // snapshot here — the derived closed stop-reason tokens, the tail
        // (last five entries), the total entry count and the bounded
        // append-failure counter. Closed tokens and timestamps only.
        const trailEntries = this.#diagnosticTrail?.readEntries() ?? [];
        // Closed-reason surfacing C2 A3: the durable tombstone reason of the
        // credential record, so "Revoked"/"Not connected" renders its durable
        // cause — null while no tombstone exists, never a fake success token.
        const secretRecord = readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME);
        return {
          connectionState: this.#connectionState,
          statusDetail: this.#statusDetail,
          // C2 A3: the closed ClearedReason of the terminal tombstone.
          clearedReason:
            secretRecord?.state === "cleared" ? secretRecord.cleared_reason : null,
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
          lastJournalFailureReasons: this.#queueDriver?.readJournalFailureReasons() ?? [],
          generationPublishFailureCount: generationPublishFailures?.count ?? 0,
          lastGenerationPublishFailureReasons:
            generationPublishFailures?.lastReasons ?? [],
          syncStopReasonTokens: deriveSyncStopReasonTokens(trailEntries),
          trailTailEntries: trailEntries.slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT),
          trailEntryCount: trailEntries.length,
          trailAppendFailureCount: this.#diagnosticTrail?.readAppendFailureCount() ?? 0,
          // Closed-reason surfacing C1 P3: the closed policy integrity
          // state (including `policy_integrity_failed`) reaches the
          // settings tab, which renders one fixed guidance line per value.
          policyState: this.#policyState,
          // C1 P1: the closed tokens of the last journal startup failure —
          // null before the first failure, never a fake success token.
          lastStartupFailureTokens: this.#lastStartupFailureTokens,
          localNoteSyncStatuses: this.#readLocalNoteSyncStatuses(),
          // Device cursor task 12: the closed device-sync status (or null
          // while no coordinator runs / the read failed closed).
          deviceSyncStatus: this.#readDeviceSyncStatus(),
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
      retryConnection: () => this.#retryConnection(policySession, session),
      openBrowserAgain: () => controller.openBrowserAgain(),
      cancelPendingLogin: () => controller.cancelPendingLogin(),
      disconnect: () => session.disconnect(),
    });
    this.addSettingTab(this.#settingTab);

    // Sync error tracing task 2: the one-action sanitized export. The block
    // is built ONLY from closed tokens, counts and ISO timestamps by the
    // pure renderer; the clipboard carries it out, and a read-only
    // preformatted modal is the fallback when the clipboard is unavailable.
    // Child six deferred remediation: the fire-and-forget call carries a
    // rejection handler — a rejection of the copy pipeline itself reports
    // through the bounded diagnostics trail and never throws into UI
    // processing.
    this.addCommand({
      id: "copy-sync-diagnostics",
      name: "Copy sync diagnostics",
      callback: () => {
        void this.#copySyncDiagnostics().catch(() => {
          this.#recordDiagnosticsCopyFailureTrailEntry();
        });
      },
    });

    // Sync error tracing task 3: the bounded self-check localizes the
    // failing layer — trail persist, credential presence, origin
    // reachability — with closed verdict tokens only. Every step appends a
    // `self_check` trail entry, nothing mutates sync state (no journal
    // event, no preflight, no policy read), and the one origin probe runs
    // under a short bounded timeout with no retry loop.
    this.addCommand({
      id: "run-sync-self-check",
      name: "Run sync self-check",
      callback: () => {
        void this.#runSyncSelfCheck();
      },
    });

    // Plugin hygiene (2026-08-16 §12): the ONE retry affordance for the
    // offline dead-end — offline state with an active credential had no
    // recovery but a plugin reload. The command stays disabled (and hidden
    // from the palette) in every other state through the check callback;
    // the settings tab exposes the same action behind canRetryConnection.
    this.addCommand({
      id: "retry-connection",
      name: "Retry connection",
      checkCallback: (checking) => {
        if (!this.#isRetryConnectionAvailable(secretStore)) {
          return false;
        }
        if (!checking) {
          void this.#retryConnection(policySession, session);
        }
        return true;
      },
    });

    // Conflict inbox task 9: the ONE explicit Conflict Inbox surface. The
    // command is registered in onload proper — the affordance exists even
    // when the journal startup later fails closed — and stays disabled
    // (hidden from the palette) until a conflict controller runs. The
    // inbox never polls conflicts, never auto-opens and runs no background
    // merge loop: every fetch, choice and merge lives inside this modal.
    this.addCommand({
      id: "open-conflict-inbox",
      name: "Open Conflict Inbox",
      checkCallback: (checking) => {
        const controller = this.#conflictController;
        if (controller === null) {
          return false;
        }
        if (!checking) {
          new ConflictInboxModal(this.app, controller).open();
        }
        return true;
      },
    });

    // The settings tab is registered before any bounded startup work so the
    // spec-19 affordances (Cancel pending login, Open browser again) stay
    // reachable while a pending grant resumes. The single bounded startup
    // action then runs fire-and-forget — never awaited in onload — and never
    // starts a background sync loop. The crash-window reconciliation is
    // local-only (one settings persist) and precedes the refresh.
    const startupRecord = readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME);
    const startupAction = resolveStartupAction(startupRecord);
    if (startupAction === "resume_pending_grant") {
      // Closed-reason surfacing C1 P4: an exceptional throw of the startup
      // chain routes into the startup_failure trail path instead of
      // vanishing; the action itself stays fire-and-forget.
      void controller.resumePendingGrant().catch((error: unknown) => {
        this.#recordStartupChainFailure(error);
      });
    } else {
      try {
        await controller.reconcileCrashWindow();
      } catch (error) {
        // Plugin hygiene (2026-08-16 §12): a settings-persist rejection
        // during the crash-window reconciliation (saveData can reject) must
        // never abort onload. The closed reason routes into the same
        // startup-failure trail path the journal failure reporter feeds —
        // buffered until the trail sidecar loads — and the bounded startup
        // chain continues below.
        this.#recordStartupChainFailure(error);
      }
      if (startupAction === "refresh_credential") {
        // Policy keyset/snapshot are fetched only AFTER a successful token
        // refresh (spec 18), still fire-and-forget and never awaited here.
        // C1 P4: the catch routes exceptional throws into the same
        // startup_failure trail path.
        void session.refresh()
          .then(() =>
            refreshVerifiedPolicyAndRequestSnapshot({
              readAcceptedRevisionNumber: () => policySession.acceptedState?.revisionNumber ?? null,
              refresh: () => policySession.refresh(),
              requestSnapshot: (reason) => this.#requestAutomaticSnapshot(reason),
            }),
          )
          .catch((error: unknown) => {
            this.#recordStartupChainFailure(error);
          });
      }
    }

    // Journal recovery runs BEFORE any capture listener exists (journal
    // design 7.1); a failed recovery fails closed — the plugin keeps
    // editing alive and simply captures nothing.
    await this.#startJournalCapture();
  }

  override onunload(): void {
    // Stop and await the coordinators before closing the journal. The
    // device-sync coordinator stops FIRST: its running cycle may still
    // await the queue dispatcher below, so its stop must begin before the
    // dispatcher's. The capture receives the snapshot abort signal, while
    // the queue driver is stopped immediately so its active request exits
    // without mutating late. The one-shot scheduled retry trigger never
    // outlives the plugin.
    this.#clearScheduledRetryPassTrigger();
    const deviceSyncCoordinatorStop = this.#syncCoordinator?.stop() ?? Promise.resolve();
    this.#syncCoordinator = null;
    const automaticSnapshotStop = this.#automaticSnapshotCoordinator?.stop() ?? Promise.resolve();
    this.#automaticSnapshotCoordinator = null;
    const boundedQueuePassStop = this.#boundedQueuePassDispatcher?.stop() ?? Promise.resolve();
    this.#boundedQueuePassDispatcher = null;
    this.#queueDriver?.stop();
    this.#lifecycleCapture?.dispose();
    this.#capture?.dispose();
    const captureQuiescence = this.#capture?.whenIdle() ?? Promise.resolve();
    void Promise.all([deviceSyncCoordinatorStop, automaticSnapshotStop, boundedQueuePassStop, captureQuiescence]).then(() => {
      this.#releaseJournalResources();
    });
    this.#controller?.stop();
    this.#session?.clearMemoryAccess();
  }

  #releaseJournalResources(): void {
    // Safe unload (spec 11): every journal mutation already persisted its
    // own verified generation, so the final flush attempt is synchronous
    // and bounded — it records whether a commit is still in flight and
    // never blocks unload on async generation publishing. An interrupted
    // commit recovers from the newest verified generation on the next open.
    this.#journalPersistence?.attemptFinalFlush();
    this.#journalPersistence?.close();
    this.#journalPersistence = null;
    this.#queueDriver = null;
    this.#lifecycleCapture = null;
    this.#capture = null;
    this.#queueRepository = null;
    this.#diagnosticTrail = null;
    this.#syncCoordinator = null;
    this.#syncStatusBarItem = null;
    // Conflict inbox task 9: the inbox surfaces release with the journal.
    this.#conflictController = null;
    this.#conflictDiagnostics = null;
    this.#conflictRepository = null;
  }

  #resolveHasActiveCredential(secretStore: SecretStorageRecordAdapter): boolean {
    return (
      this.#session !== null &&
      readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME)?.state === "active"
    );
  }

  /**
   * The retry affordance gate (plugin hygiene, 2026-08-16 §12): enabled
   * exactly while offline WITH an active credential — the one recoverable
   * state that previously required a plugin reload. The `canLogin` gating is
   * unchanged.
   */
  #isRetryConnectionAvailable(secretStore: SecretStorageRecordAdapter): boolean {
    return (
      this.#connectionState === "offline" && this.#resolveHasActiveCredential(secretStore)
    );
  }

  /**
   * Re-invoke the bounded session refresh chain on explicit demand (plugin
   * hygiene, 2026-08-16 §12): one token refresh, then the verified-policy
   * refresh and snapshot request — the same bounded chain the startup action
   * runs. An exceptional rejection routes into the closed startup-failure
   * trail path (buffered until the trail loads); the refresh's own failure
   * state and closed code already ride the state seam.
   */
  async #retryConnection(
    policySession: PolicySession,
    tokenSession: DeviceTokenSession,
  ): Promise<void> {
    const retryChain = tokenSession
      .refresh()
      .then(() =>
        refreshVerifiedPolicyAndRequestSnapshot({
          readAcceptedRevisionNumber: () => policySession.acceptedState?.revisionNumber ?? null,
          refresh: () => policySession.refresh(),
          requestSnapshot: (reason) => this.#requestAutomaticSnapshot(reason),
        }),
      );
    void retryChain.catch((error: unknown) => {
      this.#recordStartupChainFailure(error);
    });
  }

  #setConnectionState(state: ConnectionState, detail: string | null): void {
    this.#connectionState = state;
    this.#statusDetail = detail;
    this.#settingTab?.display();
    // A credential arriving or leaving changes the login-required verdict of
    // the sync status projection (spec 11).
    this.#refreshSyncStatus();
  }

  /**
   * A Vault event is actionable only after the plugin has a verified policy
   * snapshot (or its previously verified offline cache). Obsidian emits
   * create/modify notifications while restoring an existing Vault at plugin
   * load; treating those as fresh captures would silently become a full-Vault
   * scan and fail closed at revision 0 before onboarding can establish trust.
   */
  #canCaptureVaultChanges(): boolean {
    return this.#policyState === "policy_ready" || this.#policyState === "policy_offline_cached";
  }

  #requestAutomaticSnapshot(reason: AutomaticSnapshotReason): void {
    const coordinator = this.#automaticSnapshotCoordinator;
    if (coordinator === null) {
      this.#pendingAutomaticSnapshotReason = reason;
      return;
    }
    coordinator.request(reason);
  }

  /**
   * Forward one closed device-sync trigger to the single coordinator
   * (task 12). Null-safe by construction: triggers from Vault events, the
   * visibility surface and the repair command may arrive before the
   * journal starts or after unload, and a missing coordinator simply
   * drops the trigger (the cadence re-requests the work).
   */
  #requestDeviceSyncCycle(trigger: SyncTrigger): void {
    this.#syncCoordinator?.request(trigger);
  }

  /**
   * The ONE conflict recovery trigger (conflict inbox task 9): retry the
   * persisted local applies of committed conflict resolutions — local
   * application only, never another resolution, never a conflict poll.
   * Null-safe by construction (no controller before the journal starts or
   * after unload), fire-and-forget with a closed-token catch: a rejection
   * of the retry surface itself reaches the trail as
   * `conflict_apply_retry_failed`, and the status refresh keeps the
   * parked-apply surface honest on both branches.
   */
  #retryConflictLocalApplies(): void {
    const controller = this.#conflictController;
    if (controller === null) {
      return;
    }
    void controller
      .retryPendingLocalApplies()
      .catch(() => {
        this.#conflictDiagnostics?.observeConflictCompositionFailure("conflict_apply_retry_failed");
      })
      .finally(() => {
        this.#refreshSyncStatus();
      });
  }

  /**
   * The closed device-sync status of the settings snapshot and the
   * diagnostics export (task 12), or null when no coordinator runs. The
   * read is fail-closed: a throwing projection reports the once-per-
   * session closed `composition_read_failure` observation through the
   * existing sync-status read site and never becomes a stop reason — the
   * settings render keeps its "not running" line instead of a partial or
   * wrong status.
   */
  #readDeviceSyncStatus(): DeviceSyncStatus | null {
    const coordinator = this.#syncCoordinator;
    if (coordinator === null) {
      return null;
    }
    try {
      return coordinator.readStatus();
    } catch {
      this.#reportSyncStatusReadFailureOnce();
      return null;
    }
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
   * driver and the restore command. Every behavior lives in the tested
   * `./journal` modules; this method only binds real adapters.
   */
  async #startJournalCapture(): Promise<void> {
    const policySession = this.#policySession;
    const session = this.#session;
    if (policySession === null || session === null) {
      return;
    }
    // Closed-reason surfacing C1 P1: the failed startup stage must be
    // nameable at the catch. The variable advances before each stage;
    // anything at or after the repository composition is `other`.
    let startupStage: SyncStartupStageToken = "other";
    try {
      // Sync error tracing task 1: the durable closed-token diagnostics
      // trail persists one JSON sidecar (`sync-diagnostics-trail.json`)
      // through the SAME Vault plugin-directory store as the journal. It
      // loads (and resets, when corrupt) BEFORE any seam can append, then
      // feeds both the persistence publish-failure tap and the queue
      // driver wire/pass taps. The trail only observes: appends are
      // fire-and-forget and never block the sync path. Closed-reason
      // surfacing C1 P1 moves the creation to the very top of the startup
      // chain so EVERY stage — including the wasm read and the engine
      // load — can append its failure entry, and the pre-trail buffer of
      // C1 P4 flushes into it.
      const diagnosticTrail = createSyncDiagnosticsTrail({
        fileStore: this.createJournalFileStore(),
      });
      await diagnosticTrail.load();
      // Retained so the settings snapshot and the copy-sync-diagnostics
      // export can read the trail without re-creating the sidecar port.
      this.#diagnosticTrail = diagnosticTrail;
      const journalFailureReporter = createJournalFailureReporter(diagnosticTrail);
      this.#journalFailureReporter = journalFailureReporter;
      this.#flushBufferedStartupFailureEntries(diagnosticTrail);
      startupStage = "wasm_read";
      const engineWasmBinary = await this.#readJournalEngineWasmBinary();
      startupStage = "engine_load";
      const engineModule = await loadVendoredSqliteEngine({
        wasmBinary: engineWasmBinary,
      });
      const persistence = new JournalPersistence({
        fileStore: this.createJournalFileStore(),
        engineModule,
        diagnosticTrail,
        // A journal rebuilt over a non-empty Vault must reconcile first
        // (the mobile full-deletion shape); the probe mirrors exactly the
        // files the automatic snapshot would admit.
        hasVaultContent: async () => this.app.vault.getFiles().length > 0,
      });
      startupStage = "journal_recovery";
      await persistence.open();
      startupStage = "other";
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
        // Spec 12.4: completing a device repair must clear the persistence
        // sticky reconcile flag through this callback, or every later
        // generation commit re-clobbers the durable clear and re-arms the
        // reconcile loop (the 2026-09-01 live-round wedge).
        onDeviceSyncRepairComplete: () => persistence.markReconcileComplete(),
      });
      const vaultReader = this.#createCaptureVaultReader();
      const lifecycleVaultReader = this.#createLifecycleVaultReader(vaultReader);
      const lifecycleCapture = new LifecycleCaptureImpl({
        repository,
        lifecycle: repository.lifecycle,
        vaultReader: lifecycleVaultReader,
        createId: createJournalId,
        policyRevision: 1,
        failureReporter: journalFailureReporter,
      });
      const capture = new JournalCapture({
        repository,
        vaultReader,
        policyGate: policySession,
        lifecycleCapture,
        failureReporter: journalFailureReporter,
      });
      const lifecycleDriver = new LifecycleDriverImpl({
        repository,
        lifecycle: repository.lifecycle,
        api: createRequestUrlLifecycleApi({
          // Resolved afresh per commit so a server-origin edit in settings
          // applies without a plugin reload (the sync API's resolveOrigin
          // contract); freezing it at load stranded every lifecycle commit
          // on a fresh install whose origin was entered after loading.
          resolveBaseUrl: () =>
            parseServerOrigin(this.#settings.server_origin, {
              allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
            }) ?? "",
          transport: createRequestUrlTransport((request) => requestUrl(request)),
          resolveAccessToken: () => session.accessCredential,
        }),
        diagnosticTrail,
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
        multipartPlatform: resolveMultipartPlatformClass(),
        refreshAccessToken: () => session.refresh(),
        diagnosticTrail,
      });
      const boundedQueuePassDispatcher = new CoalescingQueuePassDispatcher({
        runPass: async () => {
          return await this.#runBoundedQueuePass();
        },
      }, journalFailureReporter);
      const automaticSnapshotCoordinator = new AutomaticSnapshotCoordinator({
        runSnapshot: async (signal) => {
          if (signal.aborted || !this.#canCaptureVaultChanges()) {
            return { outcome: "skipped", queuedEventCount: 0 };
          }
          const snapshot = this.#projectSyncStatus();
          if (snapshot === null || snapshot.kind === "reconcile_required") {
            return { outcome: "stopped", queuedEventCount: 0 };
          }
          const summary = await capture.runAutomaticSnapshot({ signal });
          if (signal.aborted) {
            return { outcome: "stopped", queuedEventCount: 0 };
          }
          this.#refreshSyncStatus();
          // Pre-existing pending work still owes a pass even when the scan
          // itself recorded nothing: a rename/move changes no bytes, so the
          // scan's own admission count is lifecycle-blind. Surface the
          // repository's post-snapshot pending count (which includes
          // lifecycle rows) whenever it exceeds the scan's own admission
          // count, so a restart with only pending lifecycle work still
          // requests its queue pass. The scan's own count is never lowered.
          let queuedEventCount = summary.queuedEventCount;
          try {
            queuedEventCount = Math.max(
              summary.queuedEventCount,
              repository.countPendingEvents(),
            );
          } catch {
            // An unreadable journal keeps the scan's own count (fail-closed).
            // Closed-reason surfacing C1 P5: the swallowed reason surfaces
            // as one bounded once-per-session closed-token trail entry.
            this.#recordStatusReadFailureOnce();
          }
          return {
            outcome: summary.outcome === "completed" ? "completed" : "stopped",
            queuedEventCount,
          };
        },
        requestQueuePass: async () => {
          await boundedQueuePassDispatcher.request();
        },
      }, journalFailureReporter);
      // Device cursor and manifest reconciliation (task 12): the single
      // coordinator that owns every mutating foreground network phase —
      // recovery, repair, the eligible outbound drain, one inbound page
      // and the cursor acknowledgement. Built only after the journal and
      // the diagnostics trail are ready; every behavior lives in the
      // tested ./device-sync modules, this block only binds real adapters.
      const deviceSyncDiagnostics = createDeviceSyncDiagnostics(diagnosticTrail);
      const deviceSyncRepository = new DeviceSyncRepository({ database: journalDatabase });
      const deviceSyncApi = createDeviceSyncApi({
        transport: createObsidianDeviceSyncHttpTransport(),
        resolveOrigin: () =>
          parseServerOrigin(this.#settings.server_origin, {
            allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
          }) ?? "",
        getAccessToken: () => session.accessCredential,
        // Resident-app self-healing: a foreground session whose access
        // credential expired rotates once and retries instead of silently
        // stopping until an app restart (2026-08-27 physical-matrix finding).
        refreshAccessToken: () => session.refresh(),
        diagnostics: deviceSyncDiagnostics,
      });
      const remoteEventApplier = createRemoteEventApplier({
        repository: deviceSyncRepository,
        writer: new AtomicVaultWriterImpl({
          repository: deviceSyncRepository,
          seam: createStructuralVaultMutationSeam(
            this.#createStructuralVaultSurfaceForDeviceSync(),
            this.#createStructuralVaultAdapterSurfaceForDeviceSync(),
          ),
        }),
        downloader: (input) => deviceSyncApi.downloadSourceVersion(input),
        diagnostics: deviceSyncDiagnostics,
      });
      const manifestReconcilerJournal = createManifestReconcilerJournal({
        repository,
        capture,
      });
      const manifestReconciler = createManifestReconciler({
        repository: deviceSyncRepository,
        api: deviceSyncApi,
        capture: createManifestCapture({
          vaultReader,
          identityReader: repository,
        }),
        journal: manifestReconcilerJournal,
        applier: remoteEventApplier,
        diagnostics: deviceSyncDiagnostics,
        downloader: (input) => deviceSyncApi.downloadSourceVersion(input),
      });
      const syncCoordinator = createSyncCoordinator({
        repository: deviceSyncRepository,
        api: deviceSyncApi,
        applier: remoteEventApplier,
        reconciler: manifestReconciler,
        outbound: boundedQueuePassDispatcher,
        diagnostics: deviceSyncDiagnostics,
        nowEpochMs: () => Date.now(),
        // The journal's sticky reconcile flag joins the repair-if-required
        // decision of every cycle.
        isJournalReconcileRequired: () => persistence.isReconcileRequired,
        // The active manifest run's action progress feeds the pending
        // action count of the closed status projection.
        readManifestActionProgress: () => repository.readManifestActionProgress(),
        // The server-minted device id (uuid7, persisted at grant
        // exchange) is the identity the device-event origin_device_id
        // namespace carries; the client_instance_id is a disjoint
        // client-minted namespace that can never match. Null before the
        // first exchange: the self-origin check then never suppresses and
        // every pulled event walks the full crash-safe apply machine.
        resolveOwnDeviceId: () => this.#settings.device_id,
        outboundEvidence: {
          readCommittedOutboundRowByLocator: (normalizedLocator) =>
            repository.readLocalFileByPath(normalizedLocator),
        },
        // After a suspension of one hour or more, an active manifest run's
        // temporary progress is discarded before the resume starts a
        // fresh checkpoint-bound run under the same barrier.
        discardExpiredManifestRun: () =>
          manifestReconcilerJournal.discardActiveManifestRun(),
      });
      this.#syncCoordinator = syncCoordinator;
      this.#journalPersistence = persistence;
      this.#capture = capture;
      this.#queueDriver = queueDriver;
      this.#queueRepository = repository;
      this.#lifecycleCapture = lifecycleCapture;
      this.#automaticSnapshotCoordinator = automaticSnapshotCoordinator;
      this.#boundedQueuePassDispatcher = boundedQueuePassDispatcher;
      // Conflict inbox task 9: the Conflict Inbox stack binds behind the
      // SAME journal database seam, diagnostics trail, device-credential
      // transport, atomic vault seam and version-download surface the
      // device-sync lane already owns — adapters only, every behavior
      // lives in the tested ./conflicts modules. The controller wears the
      // foreign-throw observer so a repair-store throw that the modal
      // would render as its fixed fallback still reaches the trail as a
      // closed token (the Task 8 M-1 carry).
      const conflictDiagnostics = createConflictDiagnosticsTrailSink(diagnosticTrail);
      const conflictRepository = new ConflictRepository({ database: journalDatabase });
      const conflictController = observeUnobservedConflictControllerFailures(
        createConflictController({
          api: createConflictApi({
            transport: createObsidianDeviceSyncHttpTransport(),
            resolveOrigin: () =>
              parseServerOrigin(this.#settings.server_origin, {
                allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
              }) ?? "",
            getAccessToken: () => session.accessCredential,
          }),
          repairStore: conflictRepository,
          // No verified-candidate server surface exists yet (Task 7
          // report §1; Task 10 wires it): the interim uploader fails
          // closed with the controller's own candidate-upload reason —
          // no HTTP call against a nonexistent route is invented.
          uploader: createUnavailableVerifiedCandidateUploader(),
          applier: createConflictCanonicalOutcomeApplier({
            database: journalDatabase,
            repository: deviceSyncRepository,
            seam: createStructuralVaultMutationSeam(
              this.#createStructuralVaultSurfaceForDeviceSync(),
              this.#createStructuralVaultAdapterSurfaceForDeviceSync(),
            ),
            downloadSourceVersion: (input) => deviceSyncApi.downloadSourceVersion(input),
            diagnostics: conflictDiagnostics,
          }),
          diagnostics: conflictDiagnostics,
        }),
        conflictDiagnostics,
      );
      this.#conflictController = conflictController;
      this.#conflictDiagnostics = conflictDiagnostics;
      this.#conflictRepository = conflictRepository;
      this.app.workspace.onLayoutReady(() => {
        this.registerEvent(
          this.app.vault.on("create", (file) => {
          if (!this.#canCaptureVaultChanges()) {
            return;
          }
          // The pass follows the settled admission (250 ms settle re-reads the
          // bytes): running it at event time would find an empty journal.
          void capture.notifyPathChanged(file.path).then(
            () => {
              void boundedQueuePassDispatcher.request();
              this.#requestDeviceSyncCycle("local_commit");
            },
            () => undefined,
          );
          }),
        );
        this.registerEvent(
          this.app.vault.on("modify", (file) => {
            if (!this.#canCaptureVaultChanges()) {
              return;
            }
            void capture.notifyPathChanged(file.path).then(
              () => {
                void boundedQueuePassDispatcher.request();
                this.#requestDeviceSyncCycle("local_commit");
              },
              () => undefined,
            );
          }),
        );
        this.registerEvent(
          this.app.vault.on("delete", (file) => {
            if (!this.#canCaptureVaultChanges()) {
              return;
            }
            // The pass follows the recorded delete intent exactly like the
            // create/modify listeners: without a trigger of its own the
            // queued lifecycle event would sit undelivered until an
            // unrelated surface happened to run a pass.
            void capture.notifyPathDeleted(this.#toVaultTargetFile(file)).then(
              () => {
                void boundedQueuePassDispatcher.request();
                this.#requestDeviceSyncCycle("local_commit");
              },
              () => undefined,
            );
          }),
        );
        this.registerEvent(
          this.app.vault.on("rename", (file, oldPath) => {
            if (!this.#canCaptureVaultChanges()) {
              return;
            }
            // Same discipline as delete: the rename's settle delay is
            // applied inside the lifecycle capture, and the settled
            // capture is followed by one bounded queue pass.
            void capture.notifyPathRenamed(this.#toVaultRenameTarget(file), oldPath).then(
              () => {
                void boundedQueuePassDispatcher.request();
                this.#requestDeviceSyncCycle("local_commit");
              },
              () => undefined,
            );
          }),
        );
        automaticSnapshotCoordinator.request("startup");
        if (this.#pendingAutomaticSnapshotReason !== null) {
          automaticSnapshotCoordinator.request(this.#pendingAutomaticSnapshotReason);
          this.#pendingAutomaticSnapshotReason = null;
        }
        // Device cursor task 12: the coordinator's startup trigger follows
        // the same layout-ready boundary as capture convergence — the
        // first bounded cycle runs recovery, repair-if-required, one
        // outbound drain and one inbound page.
        this.#requestDeviceSyncCycle("startup");
        // Conflict inbox task 9: the startup trigger retries ONLY the
        // persisted local applies of committed conflict resolutions —
        // never a conflict poll, never a background merge loop.
        void this.#retryConflictLocalApplies();
        // A device returning from suspension (a backgrounded mobile
        // session, a reopened desktop window) re-enters the cadence: the
        // coordinator measures the idle gap itself, so a suspension of one
        // hour or more expires an active manifest run before the resume.
        this.registerDomEvent(document, "visibilitychange", () => {
          if (document.visibilityState === "visible") {
            this.#requestDeviceSyncCycle("resume");
            // The foreground trigger retries the persisted conflict local
            // applies too — the same bounded, resolution-free surface.
            void this.#retryConflictLocalApplies();
          }
        });
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
      // Device cursor task 12: the ONE explicit repair surface. The
      // command only forwards the closed trigger — the coordinator owns
      // the bounded repair cycle, and the settings tab renders its closed
      // status without any repair control of its own.
      this.addCommand({
        id: "repair-sync",
        name: "Repair sync",
        callback: () => {
          this.#requestDeviceSyncCycle("explicit_repair");
        },
      });
    } catch (error) {
      // Engine or recovery failure is fail-closed: no capture surface is
      // registered and Vault editing is never touched. Closed-reason
      // surfacing C1 P1: the closed stage token (plus the closed store
      // reason when the throw is a store error) reaches the trail, the
      // settings snapshot and the self-check instead of being discarded.
      const startupFailureTokens = this.#buildStartupFailureTokens(startupStage, error);
      this.#lastStartupFailureTokens = startupFailureTokens;
      this.#appendStartupFailureTrailEntry(startupFailureTokens);
    }
  }

  // --- closed startup-failure and read-swallow surfacing (C1 P1/P4/P5) ---------------------------

  /**
   * Build the closed token list of one startup failure (C1 P1): the failed
   * stage token plus the closed `JournalStoreErrorReason` when the thrown
   * value is a store error. Closed tokens only — the exception text, any
   * path and any raw detail never enter the list.
   */
  #buildStartupFailureTokens(
    startupStage: SyncStartupStageToken,
    error: unknown,
  ): readonly SyncDiagnosticClosedToken[] {
    const tokens: SyncDiagnosticClosedToken[] = [startupStage];
    if (error instanceof JournalStoreError) {
      tokens.push(error.reason);
    }
    return tokens;
  }

  /**
   * Append one `startup_failure` trail entry (fire-and-forget, the trail's
   * never-blocks guarantee holds) or buffer it when the trail does not
   * exist yet (C1 P4: the startup chains can reject before the sidecar is
   * loaded). The buffer is bounded; the oldest entries drop beyond it.
   */
  #appendStartupFailureTrailEntry(tokens: readonly SyncDiagnosticClosedToken[]): void {
    const trail = this.#diagnosticTrail;
    if (trail === null) {
      if (this.#bufferedStartupFailureTokenLists.length < MAX_BUFFERED_STARTUP_FAILURE_ENTRIES) {
        this.#bufferedStartupFailureTokenLists.push(tokens);
      }
      return;
    }
    void trail.append({ kind: "startup_failure", tokens });
  }

  /**
   * Flush the bounded pre-trail startup-failure buffer into the freshly
   * loaded trail (C1 P4). Each buffered list appends exactly once; entries
   * recorded after this point append directly.
   */
  #flushBufferedStartupFailureEntries(trail: SyncDiagnosticsTrail): void {
    for (const tokens of this.#bufferedStartupFailureTokenLists) {
      void trail.append({ kind: "startup_failure", tokens });
    }
    this.#bufferedStartupFailureTokenLists = [];
  }

  /**
   * Route one exceptional throw of the two fire-and-forget startup chains
   * into the same `startup_failure` trail path (C1 P4): stage token
   * `other` plus the closed store reason when applicable, buffered until
   * the trail exists. The settings snapshot's journal-startup verdict
   * stays untouched — these chains do not stop the journal.
   */
  #recordStartupChainFailure(error: unknown): void {
    this.#appendStartupFailureTrailEntry(this.#buildStartupFailureTokens("other", error));
  }

  /**
   * Record the pending-count read swallow (C1 P5): ONE
   * `composition_read_failure` trail entry carrying the closed
   * `status_read` stage and the `status_read_failed` token, at most once
   * per session — no per-render spam, and never a derived stop reason
   * (trail v2 taxonomy, task 7).
   */
  #recordStatusReadFailureOnce(): void {
    if (this.#hasRecordedStatusReadFailure) {
      return;
    }
    this.#hasRecordedStatusReadFailure = true;
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({
        kind: "composition_read_failure",
        tokens: ["status_read", "status_read_failed"],
      });
    }
  }

  /**
   * Record the note-status read swallow (C1 P5): ONE
   * `composition_read_failure` trail entry carrying the closed
   * `note_status_read` stage and the `note_status_read_failed` token, at
   * most once per session — no per-render spam, and never a derived stop
   * reason (trail v2 taxonomy, task 7).
   */
  #recordNoteStatusReadFailureOnce(): void {
    if (this.#hasRecordedNoteStatusReadFailure) {
      return;
    }
    this.#hasRecordedNoteStatusReadFailure = true;
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({
        kind: "composition_read_failure",
        tokens: ["note_status_read", "note_status_read_failed"],
      });
    }
  }

  #reportRetryScheduleReadFailureOnce(): void {
    if (this.#hasReportedRetryScheduleReadFailure) {
      return;
    }
    this.#hasReportedRetryScheduleReadFailure = true;
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({
        kind: "composition_read_failure",
        tokens: ["retry_schedule_read", "retry_schedule_read_failed"],
      });
    }
  }

  #reportSyncStatusReadFailureOnce(): void {
    if (this.#hasReportedSyncStatusReadFailure) {
      return;
    }
    this.#hasReportedSyncStatusReadFailure = true;
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({
        kind: "composition_read_failure",
        tokens: ["sync_status_read", "sync_status_read_failed"],
      });
    }
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
    // Reservation-first protocol: the target locator is durably claimed
    // BEFORE the operator stages any bytes, so the convergence lane can
    // never ship the staged restore bytes as a fresh source at the
    // target (the convergence/lifecycle lane race). A refused
    // reservation surfaces its closed reason token through the trail and
    // a path-free Notice; the journal stays healthy.
    let reservation: RestoreReservationResult;
    try {
      reservation = await lifecycleCapture.reserveRestoreTarget(
        selection.localFileId,
        targetPath,
      );
    } catch {
      this.#journalFailureReporter?.reportJournalFailure("restore_reservation_persist_failed");
      new Notice("Restore could not be recorded. Check the Sync status.");
      this.#refreshSyncStatus();
      return;
    }
    if (reservation.outcome === "refused") {
      this.#journalFailureReporter?.reportJournalFailure(reservation.reason);
      new Notice(RESTORE_RESERVATION_REFUSAL_NOTICES[reservation.reason]);
      this.#refreshSyncStatus();
      return;
    }
    const confirmation = await this.#confirmRestoreRequest(selection, targetPath);
    if (confirmation !== "confirmed") {
      // Only the explicit Cancel button releases the durable reservation;
      // a passive dismissal keeps it resumable through the picker.
      if (confirmation === "cancelled") {
        await lifecycleCapture.releaseRestoreTarget(selection.localFileId).catch(
          () => undefined,
        );
        this.#refreshSyncStatus();
        return;
      }
      new Notice("Restore target reserved. Re-run the command to resume or cancel.");
      this.#refreshSyncStatus();
      return;
    }
    try {
      await lifecycleCapture.requestRestore(selection.localFileId, targetPath);
    } catch {
      // The lifecycle capture closes the failure as the closed
      // `journal_mutation_failed` `JournalStoreErrorReason`; the
      // rejected bytes hash, missing retained mapping, missing open
      // tombstone or missing delete predecessor stays local. The
      // reservation remains durable and resumable; the sync status
      // refresh is the single source of truth for the user.
    }
    // One bounded queue pass ships the recorded restore event — the same
    // discipline the Vault rename/delete listeners already follow.
    void this.#boundedQueuePassDispatcher?.request();
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
      const localFileIds = repository.readRestorableLocalFileIds();
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
        "Vault path the restored bytes should occupy. The path is reserved when you continue; place the restored bytes there before confirming.",
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
  ): Promise<"confirmed" | "cancelled" | "dismissed"> {
    return new Promise((resolve) => {
      const modal = new ConfirmModal(
        this.app,
        "Confirm restore",
        [
          `Restore ${selection.shortLabel} to the chosen Vault path?`,
          "The bytes hash must match the server-committed content hash or the restore is rejected.",
        ].join("\n"),
        () => resolve("confirmed"),
        () => resolve("cancelled"),
        () => resolve("dismissed"),
      );
      void targetPath;
      modal.open();
    });
  }

  /**
   * The copy-sync-diagnostics command callback (sync error tracing task 2):
   * build the sanitized export block — closed tokens, counts and ISO
   * timestamps only — and place it on the clipboard. When the clipboard is
   * unavailable or rejects the write, the SAME block is shown in a
   * read-only preformatted modal. The block never reaches a console, a
   * log or any other surface.
   */
  async #copySyncDiagnostics(): Promise<void> {
    const block = this.#buildSyncDiagnosticsExportBlock();
    const clipboard = navigator.clipboard;
    if (clipboard !== undefined) {
      try {
        await clipboard.writeText(block);
        new Notice("Sync diagnostics copied to the clipboard.");
        return;
      } catch {
        // The clipboard refused the write: fall through to the read-only
        // modal fallback below with the same sanitized block.
      }
    }
    new PreformattedTextModal(this.app, "Sync diagnostics", block).open();
  }

  /**
   * Record the copy command's own rejection (child six deferred
   * remediation): the clipboard-unavailable/refused branch is already
   * absorbed by the modal fallback inside `#copySyncDiagnostics`, so this
   * handler covers an exceptional rejection of the copy pipeline itself.
   * It reports through the established bounded diagnostics pattern — ONE
   * `self_check` trail entry carrying the closed `trail_persist_failed`
   * verdict token (the diagnostics write-out pipeline failed; the
   * append itself is failure-counted and never rejects). The handler
   * never rethrows into UI processing and never logs the clipboard data
   * or any other value: the closed token carries no detail.
   */
  #recordDiagnosticsCopyFailureTrailEntry(): void {
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({ kind: "self_check", tokens: ["trail_persist_failed"] });
    }
  }

  /**
   * The closed input assembly of the sanitized export block: the current
   * status snapshot line, the settings journal-store diagnostics inputs,
   * the aggregate trail counts and the trail tail. Every source is an
   * already-redacted closed surface; the builder adds no raw value.
   */
  #buildSyncDiagnosticsExportBlock(): string {
    const syncStatus = this.#projectSyncStatus();
    const generationPublishFailures =
      this.#journalPersistence?.readGenerationPublishFailureSummary() ?? null;
    const trailEntries = this.#diagnosticTrail?.readEntries() ?? [];
    // Device cursor task 12: the same closed device-sync status line the
    // settings tab renders joins the sanitized export block.
    const deviceSyncStatus = this.#readDeviceSyncStatus();
    return renderSyncDiagnosticsExportBlock({
      syncStatusLine: syncStatus === null ? null : renderJournalSyncStatus(syncStatus),
      syncBlockerGuidance:
        syncStatus === null ? [] : [...syncBlockerGuidanceLines(syncStatus)],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: this.#queueDriver?.readJournalFailureReasons() ?? [],
        generationPublishFailureCount: generationPublishFailures?.count ?? 0,
        lastGenerationPublishFailureReasons: generationPublishFailures?.lastReasons ?? [],
      },
      trailEntryCount: trailEntries.length,
      trailAppendFailureCount: this.#diagnosticTrail?.readAppendFailureCount() ?? 0,
      trailTail: trailEntries.slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT),
      deviceSyncStatusLine:
        deviceSyncStatus === null ? null : renderDeviceSyncStatusText(deviceSyncStatus),
    });
  }

  /**
   * The run-sync-self-check command callback (sync error tracing task 3):
   * execute the three closed-verdict steps — trail append-and-persist
   * probe, credential presence, origin reachability — and show the
   * one-line summary in a notice. The composition holds no sync-mutating
   * capability: the pure runner receives only the trail port, the boolean
   * credential-presence reader and one liveness GET through the existing
   * requestUrl transport seam toward the SAME resolved origin the sync
   * client uses. Any outcome — including an unreachable or hanging origin —
   * closes as a verdict token; no hostname, status number or response text
   * ever reaches the notice.
   */
  async #runSyncSelfCheck(): Promise<void> {
    const trail = this.#diagnosticTrail;
    const startupFailureTokens = this.#lastStartupFailureTokens;
    if (trail === null || startupFailureTokens !== null) {
      // The journal stack failed closed at load (or never started): there
      // is no trail to probe and no sync surface to diagnose beyond that
      // fact. Closed-reason surfacing C1 P1: the journal-not-running
      // verdict renders the SAME closed startup-failure tokens the
      // settings snapshot carries.
      new Notice(renderSyncSelfCheckJournalNotRunningText(startupFailureTokens), 10_000);
      return;
    }
    const transport = createObsidianPolicyHttpTransport();
    const summary = await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => this.#session?.accessCredential != null,
      probeOrigin: async () => {
        const origin =
          parseServerOrigin(this.#settings.server_origin, {
            allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
          }) ?? "";
        if (origin === "") {
          // No configured (or parseable) origin: the probe cannot be sent,
          // so the origin is simply not reachable. The verdict stays closed.
          throw new Error("origin unconfigured");
        }
        // The side-effect-free liveness route: any settled HTTP answer —
        // whatever its status — proves the origin reachable, and the status
        // and body never enter a verdict.
        await transport({ url: `${origin}/api/health/live`, headers: {} });
      },
    });
    new Notice(renderSyncSelfCheckSummaryText(summary), 10_000);
  }

  /**
   * The single bounded queue-pass wrapper (spec 8, 11): settled Vault events
   * and automatic snapshots funnel through here, so the status projection
   * sees the active pass and every finished pass outcome. Only the automatic
   * snapshot dispatcher awaits it; listeners remain fire-and-forget.
   */
  async #runBoundedQueuePass(): Promise<QueuePassSummary> {
    const driver = this.#queueDriver;
    if (driver === null) {
      return { outcome: "completed", processedEventCount: 0 };
    }
    this.#isQueuePassActive = true;
    this.#refreshSyncStatus();
    let summary: QueuePassSummary;
    try {
      summary = await driver.requestPass();
    } catch {
      // The driver never lets a trigger crash; a local failure still ends
      // this wrapper's view of the pass. Closed-reason surfacing C1 P2:
      // the swallowed throw surfaces as the closed `pass_wrapper_failed`
      // outcome — on the trail AND on the summary — never `completed`.
      summary = { outcome: "pass_wrapper_failed", processedEventCount: 0 };
      const trail = this.#diagnosticTrail;
      if (trail !== null) {
        void trail.append({ kind: "pass_outcome", tokens: ["pass_wrapper_failed"] });
      }
    }
    if (summary.outcome !== "pass_already_running") {
      // Only the invocation that actually ran the pass clears the active
      // flag; a trigger that found a running pass leaves it untouched.
      this.#isQueuePassActive = false;
      this.#lastQueuePassOutcome = summary.outcome;
      if (summary.outcome !== "stopped") {
        // Fix round 3 (extending fix round 2 D4): arm after every pass
        // end that actually ran work, not only retry/login ends. A
        // `completed` pass can still leave parked work behind — a
        // lifecycle-lane retryable failure (for example one 5xx) parks
        // its event in bounded backoff while the content lane drains or
        // idles. The armer no-ops when no pending row carries a retry
        // deadline, so unconditional arming costs nothing otherwise.
        //
        // Fix round 4 (busy-loop closure): a `stopped` pass end is the
        // one exclusion. The dispatcher is not stopped (only unload
        // stops it), so a stopped-pass timer would fire into the stopped
        // driver, produce another stopped pass, re-arm at a possibly-
        // past deadline (`setTimeout(0)`), and self-sustain for as long
        // as the stopping condition persists — for example a
        // reconcile-required journal with a parked retry row.
        this.#armScheduledRetryPassTrigger();
      }
    }
    this.#refreshSyncStatus();
    return summary;
  }

  /**
   * The bounded one-shot scheduled retry trigger (fix round 2 D4, widened
   * in fix round 3, stopped-exclusion added in fix round 4): after any
   * pass that actually ran and did not end `stopped`, arm ONE cancellable
   * timer at the earliest pending retry deadline (plus a small safety
   * margin) whose single firing requests one bounded queue pass through
   * the same dispatcher every other trigger uses. This mirrors the
   * already-reviewed `deadline_reached` serial follow-up: the PASS stays
   * bounded and trigger-driven; only the trigger is scheduled. The armer
   * no-ops when no pending row carries a retry deadline. A `stopped` pass
   * end never arms: the dispatcher is not stopped (only unload stops it),
   * so a stopped-pass timer would fire into the stopped driver and
   * self-sustain at a past deadline. At most one timer is outstanding
   * (an already-earlier target keeps the existing timer, a sooner target
   * re-arms it), unload cancels it, and this is never a repeating daemon
   * loop. No `JournalQueueDriver` failure semantics change — the
   * no-overtake discipline of fix round 1 stays.
   */
  #armScheduledRetryPassTrigger(): void {
    const repository = this.#queueRepository;
    if (repository === null) {
      return;
    }
    let earliestRetryEpochMs: number | null = null;
    try {
      earliestRetryEpochMs = repository.readEarliestPendingRetryEpochMs();
    } catch {
      // An unreadable journal arms nothing (fail-closed).
      // Closed reason: "retry_schedule_read_failed".
      this.#reportRetryScheduleReadFailureOnce();
      return;
    }
    if (earliestRetryEpochMs === null) {
      return;
    }
    const targetEpochMs = earliestRetryEpochMs + SCHEDULED_RETRY_PASS_SAFETY_MARGIN_MS;
    if (
      this.#scheduledRetryPassTargetEpochMs !== null &&
      this.#scheduledRetryPassTargetEpochMs <= targetEpochMs
    ) {
      // The outstanding timer already fires no later than this target.
      return;
    }
    this.#clearScheduledRetryPassTrigger();
    this.#scheduledRetryPassTargetEpochMs = targetEpochMs;
    this.#scheduledRetryPassTimer = setTimeout(() => {
      this.#scheduledRetryPassTimer = null;
      this.#scheduledRetryPassTargetEpochMs = null;
      void this.#boundedQueuePassDispatcher?.request();
    }, Math.max(0, targetEpochMs - Date.now()));
  }

  /** Cancel the outstanding scheduled retry timer (unload / re-arm). */
  #clearScheduledRetryPassTrigger(): void {
    if (this.#scheduledRetryPassTimer !== null) {
      clearTimeout(this.#scheduledRetryPassTimer);
      this.#scheduledRetryPassTimer = null;
    }
    this.#scheduledRetryPassTargetEpochMs = null;
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
    // Conflict inbox task 9: the composed render carries the closed
    // `Conflict apply pending` fragment on top of the exact spec-11
    // status text while parked conflict applies owe their Vault apply.
    statusBarItem.setText(renderJournalSyncStatus(snapshot));
  }

  /**
   * The closed projection input, or null while no journal runs: the
   * composition reads the redacted repository histogram plus the sticky
   * journal reconcile flag, the live credential fact, the pass facts,
   * (Task 10) the redacted source-lifecycle surface (state histogram,
   * pending-event count, failed-attempt count, closed blocker codes) and
   * (multipart task 11) the redacted multipart surface (closed
   * session-state histogram and closed safe-reason tokens of the durable
   * multipart progress). All reads share one `try { … } catch { return
   * null }` boundary so an unreadable journal renders no status rather
   * than a partial one.
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
    let multipartSessionStateCounts: MultipartSessionStateCounts;
    let multipartSafeReasonCodes: readonly MultipartSafeReasonToken[];
    let conflictApplyStatusFacts: ReturnType<typeof deriveConflictApplyStatusFacts>;
    try {
      eventStateErrorCounts = repository.readEventStateErrorCounts();
      lifecycleStateCounts = repository.readLifecycleStateCounts();
      pendingLifecycleEventCount = repository.countPendingLifecycleEvents();
      failedAttemptCount = repository.countFailedAttempts();
      lifecycleBlockedReasonCodes = repository.readLifecycleBlockedReasonCodes() as readonly LifecycleBlockedReasonCode[];
      multipartSessionStateCounts = repository.readMultipartSessionStateCounts();
      multipartSafeReasonCodes = repository.readMultipartSafeReasonCodes();
      // Conflict inbox task 9: the parked-apply facts join the same
      // fail-closed read boundary — every parked row counts, including an
      // attempt-capped one (eligibility gates on the cap, not the
      // timestamp), and only closed safe-reason tokens surface.
      conflictApplyStatusFacts = deriveConflictApplyStatusFacts(
        this.#conflictRepository?.readPendingLocalApplies() ?? [],
      );
    } catch {
      // The journal store is closed or unreadable: render no status rather
      // than a wrong one (the fail-closed rule of the journal design).
      // Closed reason: "sync_status_read_failed".
      this.#reportSyncStatusReadFailureOnce();
      return null;
    }
    return projectJournalSyncStatus({
      isReconcileRequired: this.#journalPersistence?.isReconcileRequired ?? false,
      eventStateErrorCounts,
      lifecycleStateCounts,
      pendingLifecycleEventCount,
      failedAttemptCount,
      lifecycleBlockedReasonCodes,
      multipartSessionStateCounts,
      multipartSafeReasonCodes,
      conflictApplyPendingCount: conflictApplyStatusFacts.pendingLocalApplyCount,
      conflictApplySafeReasonTokens: conflictApplyStatusFacts.localApplySafeReasonTokens,
      hasAccessCredential: this.#session?.accessCredential != null,
      isQueuePassActive: this.#isQueuePassActive,
      lastQueuePassOutcome: this.#lastQueuePassOutcome,
    });
  }

  /**
   * Read note paths exclusively for the local settings tab. This deliberately
   * remains outside the redacted aggregate/status-bar projection.
   */
  #readLocalNoteSyncStatuses(): readonly LocalNoteSyncStatus[] {
    try {
      return this.#queueRepository?.readLocalNoteSyncStatuses() ?? [];
    } catch {
      // Closed-reason surfacing C1 P5: the swallowed reason surfaces as one
      // bounded once-per-session closed-token trail entry (no per-render
      // spam); the settings tab keeps its empty fallback.
      this.#recordNoteStatusReadFailureOnce();
      return [];
    }
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

  /**
   * The narrow structural `Vault` slice the Task 10 atomic writer's seam
   * binds to (device cursor task 12): `app.vault` narrowed member by
   * member, because the Obsidian `Vault.createBinary` returns the created
   * `TFile` where the mobile-loadable structural surface expects `void`.
   * The rename/trash narrowing is safe by construction: the seam only
   * ever passes files it obtained from `getAbstractFileByPath` itself.
   */
  #createStructuralVaultSurfaceForDeviceSync(): StructuralVaultSurface {
    const vault = this.app.vault;
    return {
      getAbstractFileByPath: (path) => vault.getAbstractFileByPath(path),
      createBinary: async (path, data) => {
        await vault.createBinary(path, data);
      },
      readBinary: async (path) => {
        const file = vault.getAbstractFileByPath(path);
        if (!(file instanceof TFile)) {
          throw new Error("device sync read target is not a regular file");
        }
        return vault.readBinary(file);
      },
      rename: (file, newPath) => vault.rename(file as TAbstractFile, newPath),
      trash: (file, system) => vault.trash(file as TAbstractFile, system),
    };
  }

  /**
   * The raw data-adapter slice for the writer's hidden siblings: the live
   * Desktop gate proved the Vault index never lists dot-prefixed paths, so
   * their staging/verify/rename/cleanup must ride the adapter. Structurally
   * typed — no `obsidian` import needed beyond the vault instance itself.
   */
  #createStructuralVaultAdapterSurfaceForDeviceSync(): StructuralVaultAdapterSurface {
    const adapter = this.app.vault.adapter;
    return {
      exists: (path) => adapter.exists(path),
      readBinary: (path) => adapter.readBinary(path),
      writeBinary: (path, data) => adapter.writeBinary(path, data),
      rename: (fromPath, toPath) => adapter.rename(fromPath, toPath),
      remove: (path) => adapter.remove(path),
    };
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
