/**
 * Tests of the closed device-sync status projection (device cursor and
 * manifest reconciliation, task 12, spec 11).
 *
 * The projection is a pure function over closed inputs: the durable
 * device-sync state of the repository, the coordinator's live repair
 * facts and the active manifest run's action progress. It never sees a
 * path, locator, source id, tombstone id, credential or fingerprint, so
 * none can reach the settings or the export surface.
 */

import { describe, expect, it } from "vitest";

import type { DeviceSyncState } from "./contracts";
import type { ManifestActionProgressOutcome } from "./contracts";
import {
  DEVICE_SYNC_REPAIR_STATE_TEXT,
  projectDeviceSyncStatus,
  renderDeviceSyncStatusText,
} from "./status";
import type { DeviceSyncStatusInput } from "./status";

function stateOf(overrides: Partial<DeviceSyncState> = {}): DeviceSyncState {
  return {
    appliedSequence: 0,
    acknowledgedSequence: 0,
    observationGeneration: 0,
    barrierGeneration: null,
    barrierReason: null,
    activeManifestRunId: null,
    manifestCheckpointSequence: null,
    manifestFinalDigest: null,
    ...overrides,
  };
}

function inputOf(overrides: Partial<DeviceSyncStatusInput> = {}): DeviceSyncStatusInput {
  return {
    state: stateOf(),
    isRepairRunning: false,
    blockedRepairReason: null,
    isJournalReconcileRequired: false,
    manifestActions: [],
    ...overrides,
  };
}

describe("projectDeviceSyncStatus repair states", () => {
  it("projects ready when nothing is owed", () => {
    const status = projectDeviceSyncStatus(inputOf());
    expect(status).toEqual({
      appliedSequence: 0,
      acknowledgedSequence: 0,
      cursorLag: 0,
      repairState: "ready",
      reason: null,
      pendingActionCount: 0,
    });
  });

  it("projects required under an active barrier with its closed reason", () => {
    const status = projectDeviceSyncStatus(
      inputOf({
        state: stateOf({
          barrierGeneration: 3,
          barrierReason: "device_cursor_gap",
          activeManifestRunId: "77777777-7777-4777-8777-777777777777",
        }),
      }),
    );
    expect(status.repairState).toBe("required");
    expect(status.reason).toBe("device_cursor_gap");
  });

  it("projects required when only the journal reconcile flag is set", () => {
    const status = projectDeviceSyncStatus(
      inputOf({ isJournalReconcileRequired: true }),
    );
    expect(status.repairState).toBe("required");
    expect(status.reason).toBeNull();
  });

  it("projects running while a repair cycle is active", () => {
    const status = projectDeviceSyncStatus(
      inputOf({
        state: stateOf({ barrierGeneration: 2, barrierReason: "device_cursor_gap" }),
        isRepairRunning: true,
      }),
    );
    expect(status.repairState).toBe("running");
  });

  it("projects blocked with the closed blocked reason above required and running", () => {
    const status = projectDeviceSyncStatus(
      inputOf({
        state: stateOf({ barrierGeneration: 2, barrierReason: "device_cursor_gap" }),
        isRepairRunning: true,
        blockedRepairReason: "device_manifest_digest_mismatch",
      }),
    );
    expect(status.repairState).toBe("blocked");
    expect(status.reason).toBe("device_manifest_digest_mismatch");
  });

  it("clears the blocked verdict once the barrier is gone", () => {
    const status = projectDeviceSyncStatus(
      inputOf({ blockedRepairReason: "device_manifest_digest_mismatch" }),
    );
    expect(status.repairState).toBe("ready");
    expect(status.reason).toBeNull();
  });
});

describe("projectDeviceSyncStatus cursor lag", () => {
  it("counts locally applied events the server has not acknowledged", () => {
    const status = projectDeviceSyncStatus(
      inputOf({ state: stateOf({ appliedSequence: 7, acknowledgedSequence: 4 }) }),
    );
    expect(status.cursorLag).toBe(3);
    expect(status.appliedSequence).toBe(7);
    expect(status.acknowledgedSequence).toBe(4);
  });

  it("counts the manifest checkpoint ahead of the applied cursor while a run is active", () => {
    const status = projectDeviceSyncStatus(
      inputOf({
        state: stateOf({
          appliedSequence: 2,
          acknowledgedSequence: 1,
          manifestCheckpointSequence: 9,
        }),
      }),
    );
    // The documented definition: the delivered-or-checkpoint watermark
    // minus the acknowledged cursor, floored at zero.
    expect(status.cursorLag).toBe(8);
  });

  it("never reports a negative lag", () => {
    const status = projectDeviceSyncStatus(
      inputOf({
        state: stateOf({ appliedSequence: 3, acknowledgedSequence: 5 }),
      }),
    );
    expect(status.cursorLag).toBe(0);
  });
});

describe("projectDeviceSyncStatus pending actions", () => {
  it("counts the active run's actions that still owe attention", () => {
    const status = projectDeviceSyncStatus(
      inputOf({
        state: stateOf({ barrierGeneration: 1, activeManifestRunId: "77777777-7777-4777-8777-777777777777" }),
        manifestActions: [
          { actionIndex: 0, outcome: "terminal_safe", reason: null },
          { actionIndex: 1, outcome: "terminal_safe", reason: "device_manifest_local_diverged" },
          { actionIndex: 2, outcome: "received", reason: null },
          { actionIndex: 3, outcome: "received", reason: "device_manifest_action_stale" },
        ],
      }),
    );
    expect(status.pendingActionCount).toBe(3);
    // A durable settle (task 11b defensive settle) surfaces through the
    // reason, never by flipping repairState to blocked.
    expect(status.repairState).toBe("required");
    expect(status.reason).toBe("device_manifest_local_diverged");
  });

  it("prefers the barrier reason over a settled action reason", () => {
    const status = projectDeviceSyncStatus(
      inputOf({
        state: stateOf({
          barrierGeneration: 1,
          barrierReason: "device_cursor_gap",
          activeManifestRunId: "77777777-7777-4777-8777-777777777777",
        }),
        manifestActions: [
          { actionIndex: 0, outcome: "terminal_safe", reason: "device_manifest_action_stale" },
        ],
      }),
    );
    expect(status.reason).toBe("device_cursor_gap");
    expect(status.pendingActionCount).toBe(1);
  });

  it("settled reasons surface even with no outstanding received actions", () => {
    const status = projectDeviceSyncStatus(
      inputOf({
        state: stateOf({
          barrierGeneration: 1,
          activeManifestRunId: "77777777-7777-4777-8777-777777777777",
        }),
        manifestActions: [
          { actionIndex: 0, outcome: "terminal_safe", reason: "device_manifest_state_invalid" },
          { actionIndex: 1, outcome: "terminal_safe", reason: null },
        ],
      }),
    );
    expect(status.pendingActionCount).toBe(1);
    expect(status.reason).toBe("device_manifest_state_invalid");
  });
});

describe("renderDeviceSyncStatusText", () => {
  it("renders the closed state, reason, counts and cursor lag only", () => {
    const line = renderDeviceSyncStatusText({
      appliedSequence: 7,
      acknowledgedSequence: 4,
      cursorLag: 3,
      repairState: "required",
      reason: "device_cursor_gap",
      pendingActionCount: 2,
    });
    expect(line).toContain(DEVICE_SYNC_REPAIR_STATE_TEXT.required);
    expect(line).toContain("device_cursor_gap");
    expect(line).toContain("3");
    expect(line).toContain("2");
    for (const forbidden of [".md", "notes/", "at1.", "secret", "https://", "Error:"]) {
      expect(line).not.toContain(forbidden);
    }
  });

  it("renders the healthy state without a fake reason token", () => {
    const line = renderDeviceSyncStatusText({
      appliedSequence: 5,
      acknowledgedSequence: 5,
      cursorLag: 0,
      repairState: "ready",
      reason: null,
      pendingActionCount: 0,
    });
    expect(line).toContain(DEVICE_SYNC_REPAIR_STATE_TEXT.ready);
    expect(line).not.toContain("null");
  });

  it("renders one fixed label per closed repair state", () => {
    const labels = (["ready", "required", "running", "blocked"] as const).map(
      (state) => DEVICE_SYNC_REPAIR_STATE_TEXT[state],
    );
    expect(new Set(labels).size).toBe(4);
  });
});

describe("ManifestActionProgressOutcome surface guard", () => {
  it("keeps the closed progress outcome vocabulary", () => {
    const outcomes: readonly ManifestActionProgressOutcome[] = ["received", "terminal_safe"];
    expect(outcomes.length).toBe(2);
  });
});
