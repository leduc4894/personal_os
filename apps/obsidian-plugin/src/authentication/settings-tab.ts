/**
 * The one settings tab of the plugin (spec 19): exact server origin, editable
 * device name, closed connection status, Login, Open browser again, Cancel
 * pending login and Disconnect. The tab renders from a snapshot and delegates
 * every action to the injected view; it holds no state of its own.
 */

import { PluginSettingTab, Setting } from "obsidian";
import type { App, Plugin } from "obsidian";

import { CONNECTION_STATUS_TEXT, resolveAuthenticationControls } from "./contracts";
import type { ConnectionState } from "./contracts";
import type { ClearedReason } from "./secret-storage-record";
import type { PolicyIntegrityState } from "../exclusion-policy/contracts";
import type { DeviceSyncStatus } from "../device-sync/status";
import { renderDeviceSyncStatusText } from "../device-sync/status";
import type { LifecycleStateCounts, LifecycleBlockedReasonCode } from "../journal/status";
import { LIFECYCLE_LOCAL_FILE_STATES } from "../journal/lifecycle-contracts";
import type { LifecycleLocalFileState } from "../journal/lifecycle-contracts";
import type { LocalNoteSyncState, LocalNoteSyncStatus } from "../journal/note-status";
import type { SyncDiagnosticClosedToken, SyncDiagnosticTrailEntry } from "../journal/sync-diagnostics-trail";
import type { JournalStoreErrorReason } from "../journal/sqlite-database";
import {
  renderJournalStoreDiagnosticsLine,
  renderSyncDiagnosticsTrailSection,
} from "../journal/sync-diagnostics-export";

// The journal-store diagnostics line moved to its closed home beside the
// trail renderers (sync error tracing task 2); the re-export keeps this
// module's published surface unchanged.
export { renderJournalStoreDiagnosticsLine } from "../journal/sync-diagnostics-export";

export interface DeviceAuthenticationSnapshot {
  readonly connectionState: ConnectionState;
  readonly statusDetail: string | null;
  /**
   * The durable closed `ClearedReason` of the credential tombstone
   * (closed-reason surfacing C2 A3): why the record was terminally cleared,
   * or null while no tombstone exists — never a fake success token.
   * Rendered beside the terminal connection state so "Revoked"/"Not
   * connected" shows its durable cause. Closed enum value only.
   */
  readonly clearedReason: ClearedReason | null;
  readonly serverOrigin: string;
  readonly deviceName: string;
  readonly hasPendingGrant: boolean;
  readonly hasActiveCredential: boolean;
  /**
   * The closed sync status text of spec 11, or null when no journal runs.
   * Already redacted: a status value and counts only.
   */
  readonly syncStatusText: string | null;
  /** The spec-11 blocker guidance lines, already redacted and closed. */
  readonly syncBlockerGuidance: readonly string[];
  /**
   * Task 10 / fix round 1 I1: the redacted lifecycle state histogram. The
   * map carries ONLY closed enum keys and non-negative integer counts.
   * `null` when no journal runs on the device.
   */
  readonly lifecycleStateCounts: LifecycleStateCounts | null;
  /** Pending lifecycle event count; zero when no journal runs. */
  readonly pendingLifecycleEventCount: number;
  /** Failed-attempt count in the bounded audit ring; zero when no journal runs. */
  readonly failedAttemptCount: number;
  /** Closed blocked reason codes observed on the journal; empty when none. */
  readonly lifecycleBlockedReasonCodes: readonly LifecycleBlockedReasonCode[];
  /**
   * Fix round 5 diagnostics: the closed `JournalStoreErrorReason` tokens of
   * the journal failures the queue pass loop's fail-closed catch swallowed,
   * newest last (bounded, in-memory only). Empty when none were observed.
   */
  readonly lastJournalFailureReasons: readonly JournalStoreErrorReason[];
  /**
   * Fix round 5 diagnostics: the total generation-publish failure count and
   * the last bounded closed reason tokens (the file-store/publish path),
   * newest last. Zero/empty when every publish verified.
   */
  readonly generationPublishFailureCount: number;
  readonly lastGenerationPublishFailureReasons: readonly JournalStoreErrorReason[];
  /**
   * Sync error tracing task 2: the closed stop-reason tokens derived from
   * the durable diagnostics trail (the newest closed token of each failure
   * kind), empty when no failure was recorded.
   */
  readonly syncStopReasonTokens: readonly SyncDiagnosticClosedToken[];
  /** The durable trail's bounded entry view, oldest first (the tail). */
  readonly trailTailEntries: readonly SyncDiagnosticTrailEntry[];
  /** The total durable trail entry count. */
  readonly trailEntryCount: number;
  /** The bounded count of swallowed trail append/persist failures. */
  readonly trailAppendFailureCount: number;
  /**
   * The closed policy integrity state of spec 18 (closed-reason surfacing
   * C1 P3) — including `policy_integrity_failed`, which gates capture but
   * previously never reached this snapshot. A closed enum value only.
   */
  readonly policyState: PolicyIntegrityState;
  /**
   * The closed tokens of the last journal startup failure (closed-reason
   * surfacing C1 P1): the failed startup stage plus the closed store
   * reason when the throw was a store error. Null before the first
   * failure — never a fake success token.
   */
  readonly lastStartupFailureTokens: readonly SyncDiagnosticClosedToken[] | null;
  /** Local-only per-note statuses; paths must never leave this settings tab. */
  readonly localNoteSyncStatuses: readonly LocalNoteSyncStatus[];
  /**
   * The closed device-sync status (device cursor task 12): repair state,
   * closed reason, cursor watermarks, cursor lag and the pending action
   * count — or null when no device-sync coordinator runs on the device.
   */
  readonly deviceSyncStatus: DeviceSyncStatus | null;
}

export interface DeviceAuthenticationTabView {
  getSnapshot(): DeviceAuthenticationSnapshot;
  setServerOrigin(origin: string): void;
  setDeviceName(name: string): void;
  login(): Promise<void>;
  openBrowserAgain(): void;
  cancelPendingLogin(): Promise<void>;
  disconnect(): Promise<void>;
}

export class DeviceAuthenticationSettingTab extends PluginSettingTab {
  readonly #view: DeviceAuthenticationTabView;

  constructor(app: App, plugin: Plugin, view: DeviceAuthenticationTabView) {
    super(app, plugin);
    this.#view = view;
  }

  override display(): void {
    const containerEl = this.containerEl;
    containerEl.empty();

    const snapshot = this.#view.getSnapshot();
    const controls = resolveAuthenticationControls(snapshot.connectionState, {
      hasPendingGrant: snapshot.hasPendingGrant,
      hasActiveCredential: snapshot.hasActiveCredential,
    });

    new Setting(containerEl)
      .setName("Connection status")
      .setDesc(renderConnectionStatusDescription(snapshot));

    // The small sync status of spec 11: one of the six closed values with
    // counts plus the fixed blocker guidance — display only, never an
    // automatic upload control, and never a full-Vault upload affordance.
    new Setting(containerEl)
      .setName("Sync status")
      .setDesc(syncStatusDescription(snapshot));

    // Closed-reason surfacing C1 P3: the closed policy integrity state
    // renders one fixed guidance line per closed value, so a
    // `policy_integrity_failed` state (which silently gates capture) is
    // finally visible with its resolution boundary.
    new Setting(containerEl)
      .setName("Policy state")
      .setDesc(renderPolicyStateGuidanceLine(snapshot.policyState));

    // Task 10 / fix round 1 I1: the redacted lifecycle state histogram
    // reaches the settings tab here. Counts only, closed enum keys, no
    // path, no source ID, no tombstone id, no fingerprint.
    new Setting(containerEl)
      .setName("Lifecycle state")
      .setDesc(lifecycleStateCountsDescription(snapshot));

    new Setting(containerEl)
      .setName("Lifecycle blockers")
      .setDesc(lifecycleBlockedReasonCodesDescription(snapshot));

    // Fix round 5: the closed-token diagnostics of swallowed journal
    // failures and generation-publish failures — the surface that makes
    // environmental commit failures (the live park mystery) diagnosable.
    // Closed vocabulary only: no raw error text, path, digest or content.
    new Setting(containerEl)
      .setName("Journal store diagnostics")
      .setDesc(
        renderJournalStoreDiagnosticsLine({
          lastJournalFailureReasons: snapshot.lastJournalFailureReasons,
          generationPublishFailureCount: snapshot.generationPublishFailureCount,
          lastGenerationPublishFailureReasons: snapshot.lastGenerationPublishFailureReasons,
        }),
      );

    // Sync error tracing task 2: the durable trail surface — the derived
    // closed stop-reason tokens, the total entry count, the bounded
    // append-failure counter and the last five entries. Closed tokens,
    // counts and timestamps only.
    new Setting(containerEl)
      .setName("Sync diagnostics trail")
      .setDesc(
        renderSyncDiagnosticsTrailSection({
          stopReasonTokens: snapshot.syncStopReasonTokens,
          totalEntryCount: snapshot.trailEntryCount,
          appendFailureCount: snapshot.trailAppendFailureCount,
          entries: snapshot.trailTailEntries,
        }),
      );

    // Device cursor task 12: the closed device-sync status — repair state,
    // closed reason, counts and cursor lag — rendered through the SAME
    // closed projection the diagnostics export uses. No repair control
    // lives here: the plugin command `Repair sync` owns the explicit
    // repair trigger.
    new Setting(containerEl)
      .setName("Device sync")
      .setDesc(deviceSyncStatusDescription(snapshot.deviceSyncStatus));

    // Vault paths are intentionally limited to this local settings surface.
    // The aggregate sync status and status bar remain redacted.
    new Setting(containerEl)
      .setName("Sync status by note")
      .setDesc(renderLocalNoteSyncStatusList(snapshot.localNoteSyncStatuses));

    new Setting(containerEl)
      .setName("Server origin")
      .setDesc("Exact HTTPS origin of the personal knowledge API")
      .addText((text) =>
        text
          .setPlaceholder("https://vault.example.com")
          .setValue(snapshot.serverOrigin)
          .onChange((value) => this.#view.setServerOrigin(value.trim())),
      );

    new Setting(containerEl)
      .setName("Device name")
      .setDesc("1–80 display characters shown on the approval page")
      .addText((text) =>
        text
          .setPlaceholder("Personal vault")
          .setValue(snapshot.deviceName)
          .onChange((value) => this.#view.setDeviceName(value)),
      );

    const actionSetting = new Setting(containerEl);
    actionSetting.addButton((button) =>
      button
        .setButtonText("Login")
        .setDisabled(!controls.canLogin)
        .onClick(() => {
          void this.#runAction(this.#view.login());
        }),
    );
    actionSetting.addButton((button) =>
      button
        .setButtonText("Open browser again")
        .setDisabled(!controls.canOpenBrowser)
        .onClick(() => {
          this.#view.openBrowserAgain();
          this.display();
        }),
    );
    actionSetting.addButton((button) =>
      button
        .setButtonText("Cancel pending login")
        .setDisabled(!controls.canCancel)
        .onClick(() => {
          void this.#runAction(this.#view.cancelPendingLogin());
        }),
    );
    actionSetting.addButton((button) =>
      button
        .setButtonText("Disconnect")
        .setDisabled(!controls.canDisconnect)
        .onClick(() => {
          void this.#runAction(this.#view.disconnect());
        }),
    );
  }

  #runAction(action: Promise<void>): void {
    action.then(
      () => this.display(),
      () => this.display(),
    );
  }
}

/**
 * The closed device-sync status description (device cursor task 12): the
 * projection's own renderer — repair state label, closed reason token,
 * cursor watermarks, cursor lag and the pending action count — or one
 * fixed line when no coordinator runs. Closed tokens and counts only.
 */
function deviceSyncStatusDescription(status: DeviceSyncStatus | null): string {
  return status === null
    ? "Device sync is not running on this device"
    : renderDeviceSyncStatusText(status);
}

/** The status line plus each blocker guidance line, joined in closed order. */
function syncStatusDescription(snapshot: DeviceAuthenticationSnapshot): string {
  const lines: string[] = [];
  if (snapshot.syncStatusText !== null) {
    lines.push(snapshot.syncStatusText);
  }
  lines.push(...snapshot.syncBlockerGuidance);
  // Closed-reason surfacing C1 P1: when the journal stack failed closed at
  // load, the closed startup-failure tokens render beside the status so
  // the silent stop is diagnosable from this tab.
  const startupFailureLine = renderJournalStartupFailureLine(
    snapshot.lastStartupFailureTokens,
  );
  if (startupFailureLine !== null) {
    lines.push(startupFailureLine);
  }
  if (lines.length === 0) {
    return "Journal not running on this device";
  }
  return lines.join(" ");
}

/**
 * The closed connection states whose durable tombstone cause renders beside
 * the state text (closed-reason surfacing C2 A3): the two terminal states a
 * cleared credential record leaves behind.
 */
const TERMINAL_CONNECTION_STATES: readonly ConnectionState[] = ["revoked", "not_connected"];

/**
 * Render the connection-status description (closed-reason surfacing C2): the
 * fixed state text, then the live closed-token detail the state seam carried
 * (A1/A2/A4/A5), then — beside a terminal state only — the durable tombstone
 * `ClearedReason` when the live detail does not already show it (A3). Closed
 * tokens and fixed English only; null inputs render nothing (never a fake
 * success token).
 */
export function renderConnectionStatusDescription(
  snapshot: Pick<
    DeviceAuthenticationSnapshot,
    "connectionState" | "statusDetail" | "clearedReason"
  >,
): string {
  const statusText = CONNECTION_STATUS_TEXT[snapshot.connectionState];
  const parts: string[] = [];
  if (snapshot.statusDetail !== null) {
    parts.push(snapshot.statusDetail);
  }
  const isTerminalConnectionState = TERMINAL_CONNECTION_STATES.includes(
    snapshot.connectionState,
  );
  if (
    isTerminalConnectionState &&
    snapshot.clearedReason !== null &&
    snapshot.clearedReason !== snapshot.statusDetail
  ) {
    parts.push(`Last cleared reason: ${snapshot.clearedReason}`);
  }
  return parts.length === 0 ? statusText : `${statusText} — ${parts.join(" · ")}`;
}

/**
 * The fixed guidance line of each closed policy integrity state
 * (closed-reason surfacing C1 P3): one line per closed value, keyed by
 * the closed enum — fixed English only, never a path, credential,
 * hostname or any free-form detail.
 */
const POLICY_STATE_GUIDANCE_TEXT: Readonly<Record<PolicyIntegrityState, string>> = {
  policy_not_initialized:
    "Policy not initialized: complete the browser login to establish policy trust before any capture runs.",
  policy_ready:
    "Policy verified: capture and sync run under the currently accepted policy revision.",
  policy_refresh_required:
    "Policy refresh required: the accepted policy revision is stale; the next successful credential refresh renews it.",
  policy_offline_cached:
    "Policy offline cache in use: capture continues under the last verified policy revision until connectivity returns.",
  policy_integrity_failed:
    "Policy integrity failed: capture is stopped until policy trust is re-established through the authorized login flow.",
};

/**
 * Render the one fixed guidance line of one closed policy integrity state
 * (closed-reason surfacing C1 P3). The closed enum is the only key; the
 * line is fixed text, so no raw value can ever reach the description.
 */
export function renderPolicyStateGuidanceLine(policyState: PolicyIntegrityState): string {
  return POLICY_STATE_GUIDANCE_TEXT[policyState];
}

/**
 * Render the journal-startup-failure line (closed-reason surfacing C1 P1):
 * a fixed English head plus the closed tokens only — or null before the
 * first failure (never a fake success token). The input is the existing
 * readonly closed-token union, so a free-form value cannot type-check in.
 */
export function renderJournalStartupFailureLine(
  startupFailureTokens: readonly SyncDiagnosticClosedToken[] | null,
): string | null {
  if (startupFailureTokens === null || startupFailureTokens.length === 0) {
    return null;
  }
  return `Journal startup failed: ${startupFailureTokens.join(", ")}`;
}

/**
 * The redacted lifecycle state histogram (Task 10, fix round 1 I1): each
 * closed enum state plus its count, in the closed enum's declared order.
 * The rendered text never includes a path, locator, source ID, token,
 * fingerprint or any other raw value. Returns a single blank line when no
 * journal runs on the device.
 */
function lifecycleStateCountsDescription(snapshot: DeviceAuthenticationSnapshot): string {
  const counts = snapshot.lifecycleStateCounts;
  if (counts === null) {
    return "Journal not running on this device";
  }
  const parts: string[] = [];
  for (const state of LIFECYCLE_LOCAL_FILE_STATES) {
    const value = counts[state];
    parts.push(`${LIFECYCLE_STATE_LABEL[state]}: ${value}`);
  }
  parts.push(`Pending lifecycle events: ${snapshot.pendingLifecycleEventCount}`);
  parts.push(`Failed attempts: ${snapshot.failedAttemptCount}`);
  return parts.join(" · ");
}

/**
 * The closed set of lifecycle blocked reason codes (Task 10, fix round 1
 * I1): each closed enum code on its own line, or a blank line when no
 * journal runs. Only the closed enum vocabulary surfaces; no row, locator,
 * source ID, tombstone id or fingerprint ever reaches the description.
 */
function lifecycleBlockedReasonCodesDescription(snapshot: DeviceAuthenticationSnapshot): string {
  const counts = snapshot.lifecycleStateCounts;
  if (counts === null) {
    return "Journal not running on this device";
  }
  if (snapshot.lifecycleBlockedReasonCodes.length === 0) {
    return "No lifecycle blockers observed";
  }
  return [...snapshot.lifecycleBlockedReasonCodes].join(", ");
}

/**
 * Render the local-only current-note list in normalized-path order. Paths are
 * supplied solely by the device journal and this string must not be reused by
 * telemetry, HTTP, logs, or the redacted status bar.
 */
export function renderLocalNoteSyncStatusList(
  statuses: readonly LocalNoteSyncStatus[],
): string {
  if (statuses.length === 0) {
    return "No note sync statuses are available on this device";
  }
  return [...statuses]
    .sort(compareNormalizedPathsByCodeUnit)
    .map(renderLocalNoteSyncStatus)
    .join("\n");
}

/** Match the journal's deterministic normalized-path ordinal ordering. */
function compareNormalizedPathsByCodeUnit(
  left: LocalNoteSyncStatus,
  right: LocalNoteSyncStatus,
): number {
  if (left.normalizedPath < right.normalizedPath) {
    return -1;
  }
  if (left.normalizedPath > right.normalizedPath) {
    return 1;
  }
  return 0;
}

function renderLocalNoteSyncStatus(status: LocalNoteSyncStatus): string {
  const line = `${status.normalizedPath} — ${LOCAL_NOTE_SYNC_STATE_LABEL[status.state]}`;
  if (status.state === "retrying") {
    return `${line} · Retry at: ${status.retryAtEpochMs ?? "unavailable"}${renderClosedReason(status.reason)}`;
  }
  if (status.state === "policy_blocked") {
    return `${line} · Policy revision: ${status.policyRevisionNumber ?? "unknown"}${renderClosedReason(status.reason)}`;
  }
  return line;
}

function renderClosedReason(reason: LocalNoteSyncStatus["reason"]): string {
  return reason === null ? "" : ` · Reason: ${reason}`;
}

/**
 * The closed vocab-to-display mapping of {@link LifecycleLocalFileState}.
 * The closed enum is the only source of labels; the plugin never renders
 * any path, locator, source ID, tombstone id or fingerprint.
 */
const LIFECYCLE_STATE_LABEL: Readonly<Record<LifecycleLocalFileState, string>> = {
  active: "Active",
  rename_pending: "Rename pending",
  move_pending: "Move pending",
  delete_pending: "Delete pending",
  restore_pending: "Restore pending",
  tombstoned: "Tombstoned",
  restored: "Restored",
  reconcile_required: "Reconcile required",
};

const LOCAL_NOTE_SYNC_STATE_LABEL: Readonly<Record<LocalNoteSyncState, string>> = {
  synced: "Synced",
  queued: "Queued",
  syncing: "Syncing",
  retrying: "Retrying",
  policy_blocked: "Policy blocked",
  conflict: "Conflict",
  reconcile_required: "Reconciliation required",
};
