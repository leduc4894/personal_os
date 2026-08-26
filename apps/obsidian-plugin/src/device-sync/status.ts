/**
 * The closed device-sync status projection (device cursor and manifest
 * reconciliation, task 12, spec 11).
 *
 * The projection folds the durable device-sync state of the Task 8
 * repository, the coordinator's live repair facts and the active manifest
 * run's action progress into the closed {@link DeviceSyncStatus} shape the
 * settings tab and the sanitized export render. It is a pure function: no
 * I/O, no clock, no transport and no journal access.
 *
 * Redaction is by construction (spec 9, 11): the input carries closed
 * reason tokens, cursor watermarks and counts only, so no path, locator,
 * source id, tombstone id, fingerprint, credential or provider detail can
 * ever reach a status value, the settings line or the export block.
 *
 * The cursor lag definition (documented, task 12 ambiguity resolution):
 * the DELIVERED-OR-CHECKPOINT watermark minus the acknowledged cursor —
 * `max(appliedSequence, manifestCheckpointSequence ?? 0) -
 * acknowledgedSequence`, floored at zero. Both watermarks are exactly what
 * the repository exposes: `appliedSequence` is what this device has
 * delivered locally, `manifestCheckpointSequence` is the server-authorized
 * cursor an active repair run will settle both cursors at, and
 * `acknowledgedSequence` is the last cursor the server durably
 * acknowledged back.
 *
 * Like the other device-sync modules this file imports no Node.js,
 * Electron or Obsidian API at module load time, so it stays loadable on
 * mobile.
 */

import type {
  DeviceSyncReason,
  DeviceSyncState,
  ManifestActionProgressOutcome,
} from "./contracts";

// --- the closed status shape (brief task 12) ----------------------------------------------------------

/** The closed repair states of the device-sync surface, no others. */
export const DEVICE_SYNC_REPAIR_STATES = ["ready", "required", "running", "blocked"] as const;

/** One closed repair state. */
export type DeviceSyncRepairState = (typeof DEVICE_SYNC_REPAIR_STATES)[number];

/** The exact display label of each closed repair state. */
export const DEVICE_SYNC_REPAIR_STATE_TEXT: Readonly<Record<DeviceSyncRepairState, string>> = {
  ready: "Ready",
  required: "Required",
  running: "Running",
  blocked: "Blocked",
};

/**
 * The closed, redacted device-sync status of the settings and export
 * surfaces: the cursor watermarks, the cursor lag, the repair state, the
 * closed reason (when one exists) and the count of manifest actions that
 * still owe attention. This shape is the whole device-sync status surface.
 */
export interface DeviceSyncStatus {
  readonly appliedSequence: number;
  readonly acknowledgedSequence: number;
  readonly cursorLag: number;
  readonly repairState: DeviceSyncRepairState;
  readonly reason: DeviceSyncReason | null;
  readonly pendingActionCount: number;
}

// --- the closed input ----------------------------------------------------------------------------------

/** One manifest action progress row the projection counts (structural subset). */
export interface DeviceSyncManifestActionInput {
  readonly actionIndex: number;
  readonly outcome: ManifestActionProgressOutcome;
  readonly reason: DeviceSyncReason | null;
}

/** The closed, redacted projection input: durable state, live facts, action progress. */
export interface DeviceSyncStatusInput {
  /** The durable reconciliation state singleton of the Task 8 repository. */
  readonly state: DeviceSyncState;
  /** Whether a repair cycle is executing right now (the coordinator's fact). */
  readonly isRepairRunning: boolean;
  /** The closed reason of the last BLOCKED repair attempt, or null. */
  readonly blockedRepairReason: DeviceSyncReason | null;
  /** The journal's sticky `reconcile_required` flag. */
  readonly isJournalReconcileRequired: boolean;
  /** The active manifest run's action progress rows (empty without a run). */
  readonly manifestActions: readonly DeviceSyncManifestActionInput[];
}

// --- the projection ------------------------------------------------------------------------------------

/**
 * Project one closed device-sync status. Priority is fixed: a recorded
 * blocked repair verdict above a running repair, above an owed repair
 * (barrier, active run or journal reconcile flag), above `ready`. The
 * blocked verdict clears with the barrier: once the repository shows no
 * repair debt, a stale blocked fact renders `ready`, never a fake blocker.
 *
 * A durable manifest-action settle (the task 11b defensive settle) is a
 * SETTLE, not a run blocker: it surfaces through `pendingActionCount` and
 * the closed `reason` while the repair state stays what the repair debt
 * alone dictates.
 */
export function projectDeviceSyncStatus(input: DeviceSyncStatusInput): DeviceSyncStatus {
  const { state } = input;
  const isRepairOwed =
    state.barrierGeneration !== null ||
    state.activeManifestRunId !== null ||
    input.isJournalReconcileRequired;
  const repairState: DeviceSyncRepairState =
    input.blockedRepairReason !== null && isRepairOwed
      ? "blocked"
      : input.isRepairRunning && isRepairOwed
        ? "running"
        : isRepairOwed
          ? "required"
          : "ready";

  let pendingActionCount = 0;
  let settledReason: DeviceSyncReason | null = null;
  for (const action of input.manifestActions) {
    if (action.outcome !== "terminal_safe" || action.reason !== null) {
      pendingActionCount += 1;
    }
    if (action.outcome === "terminal_safe" && action.reason !== null) {
      settledReason = action.reason;
    }
  }

  const reason: DeviceSyncReason | null =
    (input.blockedRepairReason !== null && isRepairOwed ? input.blockedRepairReason : null) ??
    state.barrierReason ??
    settledReason;

  const deliveredOrCheckpointSequence = Math.max(
    state.appliedSequence,
    state.manifestCheckpointSequence ?? 0,
  );
  const cursorLag = Math.max(0, deliveredOrCheckpointSequence - state.acknowledgedSequence);

  return {
    appliedSequence: state.appliedSequence,
    acknowledgedSequence: state.acknowledgedSequence,
    cursorLag,
    repairState,
    reason,
    pendingActionCount,
  };
}

/**
 * Render the closed device-sync status line of the settings tab and the
 * sanitized export: the repair state label, the closed reason token in
 * parentheses when one exists, the cursor watermarks, the cursor lag and
 * the pending action count. Closed tokens, labels and counts only.
 */
export function renderDeviceSyncStatusText(status: DeviceSyncStatus): string {
  const stateLabel = DEVICE_SYNC_REPAIR_STATE_TEXT[status.repairState];
  const reasonText = status.reason === null ? "" : ` (${status.reason})`;
  return [
    `Repair: ${stateLabel}${reasonText}`,
    `Applied: ${status.appliedSequence}`,
    `Acknowledged: ${status.acknowledgedSequence}`,
    `Cursor lag: ${status.cursorLag}`,
    `Pending actions: ${status.pendingActionCount}`,
  ].join(" · ");
}
