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
import type { LifecycleStateCounts, LifecycleBlockedReasonCode } from "../journal/status";
import { LIFECYCLE_LOCAL_FILE_STATES } from "../journal/lifecycle-contracts";
import type { LifecycleLocalFileState } from "../journal/lifecycle-contracts";
import type { LocalNoteSyncState, LocalNoteSyncStatus } from "../journal/note-status";

export interface DeviceAuthenticationSnapshot {
  readonly connectionState: ConnectionState;
  readonly statusDetail: string | null;
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
  /** Local-only per-note statuses; paths must never leave this settings tab. */
  readonly localNoteSyncStatuses: readonly LocalNoteSyncStatus[];
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

    const statusText = CONNECTION_STATUS_TEXT[snapshot.connectionState];
    const statusDescription =
      snapshot.statusDetail === null ? statusText : `${statusText} — ${snapshot.statusDetail}`;

    new Setting(containerEl)
      .setName("Connection status")
      .setDesc(statusDescription);

    // The small sync status of spec 11: one of the six closed values with
    // counts plus the fixed blocker guidance — display only, never an
    // automatic upload control, and never a full-Vault upload affordance.
    new Setting(containerEl)
      .setName("Sync status")
      .setDesc(syncStatusDescription(snapshot));

    // Task 10 / fix round 1 I1: the redacted lifecycle state histogram
    // reaches the settings tab here. Counts only, closed enum keys, no
    // path, no source ID, no tombstone id, no fingerprint.
    new Setting(containerEl)
      .setName("Lifecycle state")
      .setDesc(lifecycleStateCountsDescription(snapshot));

    new Setting(containerEl)
      .setName("Lifecycle blockers")
      .setDesc(lifecycleBlockedReasonCodesDescription(snapshot));

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

/** The status line plus each blocker guidance line, joined in closed order. */
function syncStatusDescription(snapshot: DeviceAuthenticationSnapshot): string {
  const lines: string[] = [];
  if (snapshot.syncStatusText !== null) {
    lines.push(snapshot.syncStatusText);
  }
  lines.push(...snapshot.syncBlockerGuidance);
  if (lines.length === 0) {
    return "Journal not running on this device";
  }
  return lines.join(" ");
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
