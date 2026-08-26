/**
 * Tests of the device-sync closed reason and stage contracts (device cursor
 * and manifest reconciliation, tasks 7-8).
 *
 * These tests pin the exact closed vocabularies every later device-sync
 * plugin task (pull, apply, reconcile, ack) must surface its failures
 * through: the four reason families, the stage unions of the four trail
 * failure kinds, the correlation record and the diagnostics facade port.
 * Task 8 adds the journal state vocabularies and record shapes of the
 * schema-v7 reconciliation state. A foreign string must not type-check into
 * any of them, and the server reasons must stay members of the generated
 * server error registry.
 */

import type { components } from "@workspace/api-client";
import { describe, expect, it } from "vitest";

import {
  DEVICE_SYNC_ACTION_REASONS,
  DEVICE_SYNC_APPLY_STAGES,
  DEVICE_SYNC_COMPOSITION_READ_STAGES,
  DEVICE_SYNC_CREDENTIAL_STAGES,
  DEVICE_SYNC_CURSOR_STAGES,
  DEVICE_SYNC_EVENT_OPERATIONS,
  DEVICE_SYNC_LOCAL_REASONS,
  DEVICE_SYNC_RECONCILE_STAGES,
  DEVICE_SYNC_REMOTE_APPLY_STATES,
  DEVICE_SYNC_SERVER_REASONS,
  DEVICE_SYNC_TRANSPORT_REASONS,
  MANIFEST_ACTION_KINDS,
  MANIFEST_ACTION_PROGRESS_OUTCOMES,
  TERMINAL_DEVICE_EVENT_OUTCOMES,
} from "./contracts";
import type {
  ApplyFailureStage,
  CompositionReadStage,
  CompleteLocalRepair,
  CredentialFailureStage,
  CursorFailureStage,
  DeviceEventOperation,
  DeviceSyncFailureCorrelation,
  DeviceSyncReason,
  DeviceSyncRemoteApplyState,
  DeviceSyncState,
  EchoMarker,
  LocalManifestActionProgress,
  LocalManifestPageReceipt,
  ManifestActionKind,
  ManifestActionProgressOutcome,
  PreparedRemoteApply,
  ReconcileFailureStage,
  RemoteApplyOperation,
  RemoteApplyTransition,
  RepairBarrierInput,
  TerminalDeviceEvent,
  TerminalDeviceEventOutcome,
  VaultObservation,
} from "./contracts";

// --- the four reason families ----------------------------------------------------------------------

describe("device sync reason contracts", () => {
  it("pins the exact server reason set of the Task 6 device routes", () => {
    expect(DEVICE_SYNC_SERVER_REASONS).toEqual([
      "device_cursor_gap",
      "device_cursor_regression",
      "device_cursor_ack_ahead",
      "device_event_unavailable",
      "device_event_integrity_failed",
      "device_manifest_not_found",
      "device_manifest_expired",
      "device_manifest_state_invalid",
      "device_manifest_page_invalid",
      "device_manifest_page_replay_mismatch",
      "device_manifest_digest_mismatch",
      "device_manifest_policy_advanced",
      "device_download_integrity_failed",
      "device_sync_dependency_unavailable",
    ]);
  });

  it("pins the exact manifest action reason set", () => {
    expect(DEVICE_SYNC_ACTION_REASONS).toEqual([
      "device_manifest_identity_ambiguous",
      "device_manifest_local_diverged",
      "device_manifest_target_occupied",
      "device_manifest_action_stale",
      "device_manifest_policy_excluded",
    ]);
  });

  it("pins the exact transport reason set", () => {
    expect(DEVICE_SYNC_TRANSPORT_REASONS).toEqual([
      "network_offline",
      "network_timeout",
      "network_rate_limited",
      "server_error",
      "access_expired",
      "login_required",
    ]);
  });

  it("pins the exact local reason set", () => {
    expect(DEVICE_SYNC_LOCAL_REASONS).toEqual([
      "device_apply_trash_failed",
      "device_apply_vault_failed",
      "device_apply_recovery_ambiguous",
      "device_manifest_capture_failed",
    ]);
  });

  it("keeps the reason families disjoint", () => {
    const families = [
      DEVICE_SYNC_SERVER_REASONS,
      DEVICE_SYNC_ACTION_REASONS,
      DEVICE_SYNC_TRANSPORT_REASONS,
      DEVICE_SYNC_LOCAL_REASONS,
    ];
    for (const [familyIndex, family] of families.entries()) {
      for (const [otherIndex, other] of families.entries()) {
        if (familyIndex === otherIndex) {
          continue;
        }
        for (const reason of family) {
          expect(other).not.toContain(reason);
        }
      }
    }
  });

  it("registers every server reason in the generated server error registry at compile time", () => {
    // The compile-time bound: a reason outside the generated registry fails
    // `tsc --noEmit` here, exactly like the `satisfies` bound in contracts.
    const registeredServerReasons: readonly components["schemas"]["ErrorCode"][] =
      DEVICE_SYNC_SERVER_REASONS;
    expect(registeredServerReasons).toHaveLength(DEVICE_SYNC_SERVER_REASONS.length);
  });

  it("rejects a foreign reason at compile time", () => {
    const reason: DeviceSyncReason = "device_cursor_gap";
    expect(reason).toBe("device_cursor_gap");
    // The compile-time gate: the @ts-expect-error directive only holds when
    // the assignment below is a type error, so a widened vocabulary fails
    // `tsc --noEmit` right here.
    // @ts-expect-error a free-form string is not a DeviceSyncReason
    const foreign: DeviceSyncReason = "device_made_up_reason";
    void foreign;
  });
});

// --- the stage vocabularies -------------------------------------------------------------------------

describe("device sync stage contracts", () => {
  it("pins the exact cursor failure stages", () => {
    expect(DEVICE_SYNC_CURSOR_STAGES).toEqual(["pull", "acknowledge"]);
  });

  it("pins the exact apply failure stages", () => {
    expect(DEVICE_SYNC_APPLY_STAGES).toEqual([
      "prepare",
      "download",
      "verify_temp",
      "vault_mutation",
      "verify_final",
      "local_commit",
      "recovery",
      "trash",
    ]);
  });

  it("pins the exact reconcile failure stages", () => {
    expect(DEVICE_SYNC_RECONCILE_STAGES).toEqual([
      "start",
      "page",
      "finalize",
      "actions",
      "complete",
    ]);
  });

  it("pins the exact credential failure stages", () => {
    expect(DEVICE_SYNC_CREDENTIAL_STAGES).toEqual(["access_missing", "refresh_failed"]);
  });

  it("pins the exact composition read stages", () => {
    expect(DEVICE_SYNC_COMPOSITION_READ_STAGES).toEqual([
      "status_read",
      "note_status_read",
      "retry_schedule_read",
      "sync_status_read",
    ]);
  });

  it("rejects a foreign stage at compile time", () => {
    const cursorStage: CursorFailureStage = "pull";
    const applyStage: ApplyFailureStage = "vault_mutation";
    const reconcileStage: ReconcileFailureStage = "actions";
    const credentialStage: CredentialFailureStage = "refresh_failed";
    const compositionStage: CompositionReadStage = "status_read";
    expect([cursorStage, applyStage, reconcileStage, credentialStage, compositionStage]).toEqual([
      "pull",
      "vault_mutation",
      "actions",
      "refresh_failed",
      "status_read",
    ]);
    // The compile-time gate: the @ts-expect-error directive only holds when
    // the assignment below is a type error.
    // @ts-expect-error a wire kind is not a failure stage
    const foreignStage: ApplyFailureStage = "preflight";
    void foreignStage;
  });
});

// --- the correlation record ---------------------------------------------------------------------------

describe("device sync failure correlation contract", () => {
  it("carries only the nullable request id and wire error code", () => {
    const correlation: DeviceSyncFailureCorrelation = {
      requestId: "66666666-6666-4666-8666-666666666666",
      wireErrorCode: "device_cursor_gap",
    };
    expect(correlation).toEqual({
      requestId: "66666666-6666-4666-8666-666666666666",
      wireErrorCode: "device_cursor_gap",
    });
    const emptyCorrelation: DeviceSyncFailureCorrelation = {
      requestId: null,
      wireErrorCode: null,
    };
    expect(emptyCorrelation).toEqual({ requestId: null, wireErrorCode: null });
  });
});

// --- the journal state vocabularies (task 8) -------------------------------------------------------------

describe("device sync journal state vocabularies (task 8)", () => {
  it("pins the exact canonical device event operation set", () => {
    expect(DEVICE_SYNC_EVENT_OPERATIONS).toEqual([
      "created",
      "updated",
      "renamed",
      "moved",
      "deleted",
      "restored",
    ]);
  });

  it("registers every device event operation in the generated server registry at compile time", () => {
    const registeredOperations: readonly components["schemas"]["DeviceEventType"][] =
      DEVICE_SYNC_EVENT_OPERATIONS;
    expect(registeredOperations).toHaveLength(DEVICE_SYNC_EVENT_OPERATIONS.length);
  });

  it("pins the exact remote apply state set", () => {
    expect(DEVICE_SYNC_REMOTE_APPLY_STATES).toEqual([
      "prepared",
      "temp_verified",
      "vault_mutated",
      "locally_applied",
      "server_acknowledged",
    ]);
  });

  it("pins the exact manifest action kind set", () => {
    expect(MANIFEST_ACTION_KINDS).toEqual([
      "upload",
      "download",
      "apply_tombstone",
      "conflict",
      "no_change",
      "excluded",
    ]);
  });

  it("registers every manifest action kind in the generated server registry at compile time", () => {
    const registeredKinds: readonly components["schemas"]["ManifestActionKind"][] =
      MANIFEST_ACTION_KINDS;
    expect(registeredKinds).toHaveLength(MANIFEST_ACTION_KINDS.length);
  });

  it("pins the exact terminal device event outcome set", () => {
    expect(TERMINAL_DEVICE_EVENT_OUTCOMES).toEqual([
      "applied",
      "self_origin_no_op",
      "conflict",
      "tombstone_handled",
      "excluded",
    ]);
  });

  it("pins the exact manifest action progress outcome set", () => {
    expect(MANIFEST_ACTION_PROGRESS_OUTCOMES).toEqual(["received", "terminal_safe"]);
  });

  it("rejects a foreign state token at compile time", () => {
    const operation: DeviceEventOperation = "created";
    const applyState: DeviceSyncRemoteApplyState = "vault_mutated";
    const actionKind: ManifestActionKind = "apply_tombstone";
    const terminalOutcome: TerminalDeviceEventOutcome = "self_origin_no_op";
    const actionOutcome: ManifestActionProgressOutcome = "terminal_safe";
    expect([operation, applyState, actionKind, terminalOutcome, actionOutcome]).toEqual([
      "created",
      "vault_mutated",
      "apply_tombstone",
      "self_origin_no_op",
      "terminal_safe",
    ]);
    // The compile-time gate: the @ts-expect-error directives only hold when
    // each assignment below is a type error, so a widened vocabulary fails
    // `tsc --noEmit` right here.
    // @ts-expect-error a wire event type is not a DeviceEventOperation
    const foreignOperation: DeviceEventOperation = "create";
    // @ts-expect-error a made-up stage is not a remote apply state
    const foreignApplyState: DeviceSyncRemoteApplyState = "temp_staged";
    // @ts-expect-error a planner reason is not an action kind
    const foreignActionKind: ManifestActionKind = "device_manifest_action_stale";
    // @ts-expect-error a wire token is not a terminal outcome
    const foreignTerminalOutcome: TerminalDeviceEventOutcome = "committed";
    // @ts-expect-error a planner kind is not a progress outcome
    const foreignActionOutcome: ManifestActionProgressOutcome = "upload";
    void [foreignOperation, foreignApplyState, foreignActionKind, foreignTerminalOutcome, foreignActionOutcome];
  });
});

// --- the journal state record shapes (task 8) --------------------------------------------------------------

describe("device sync journal state records (task 8)", () => {
  it("carries exactly the reconciled cursor and barrier fields on DeviceSyncState", () => {
    const state: DeviceSyncState = {
      appliedSequence: 3,
      acknowledgedSequence: 2,
      observationGeneration: 14,
      barrierGeneration: null,
      barrierReason: null,
      activeManifestRunId: null,
      manifestCheckpointSequence: null,
      manifestFinalDigest: null,
    };
    expect(state).toEqual({
      appliedSequence: 3,
      acknowledgedSequence: 2,
      observationGeneration: 14,
      barrierGeneration: null,
      barrierReason: null,
      activeManifestRunId: null,
      manifestCheckpointSequence: null,
      manifestFinalDigest: null,
    });
  });

  it("carries the pinned operands of every repository input record", () => {
    const barrier: RepairBarrierInput = { generation: 14, reason: "device_cursor_gap" };
    const pageReceipt: LocalManifestPageReceipt = {
      manifestRunId: "77777777-7777-4777-8777-777777777777",
      pageNumber: 0,
      entryCount: 500,
      pageDigest: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      checkpointSequence: 3,
      finalDigest: null,
    };
    const actionProgress: LocalManifestActionProgress = {
      manifestRunId: pageReceipt.manifestRunId,
      actionIndex: 2,
      actionKind: "download",
      outcome: "terminal_safe",
      reason: null,
    };
    const prepared: PreparedRemoteApply = {
      eventSequence: 4,
      eventId: "88888888-8888-4888-8888-888888888888",
      sourceId: "99999999-9999-4999-8999-999999999999",
      operation: "updated",
      priorLocator: "notes/a.md",
      targetLocator: null,
      baseFingerprint: { sha256: "0".repeat(64), sizeBytes: 10, mediaType: "text/plain" },
      finalFingerprint: { sha256: "1".repeat(64), sizeBytes: 12, mediaType: "text/plain" },
      tempToken: null,
      rollbackToken: null,
    };
    const transition: RemoteApplyTransition = {
      eventSequence: 4,
      state: "vault_mutated",
      rollbackToken: "rollback-token",
    };
    const terminal: TerminalDeviceEvent = {
      eventSequence: 4,
      outcome: "applied",
      reason: null,
    };
    const completion: CompleteLocalRepair = {
      manifestRunId: pageReceipt.manifestRunId,
      checkpointSequence: 3,
      barrierGeneration: 14,
    };
    expect(barrier.generation).toBe(14);
    expect(pageReceipt.pageNumber).toBe(0);
    expect(actionProgress.actionKind).toBe("download");
    expect(prepared.operation).toBe("updated");
    expect(transition.state).toBe("vault_mutated");
    expect(terminal.outcome).toBe("applied");
    expect(completion.barrierGeneration).toBe(14);
  });

  it("carries the exact echo marker and vault observation operand shapes", () => {
    const marker: EchoMarker = {
      eventSequence: 4,
      sourceId: "99999999-9999-4999-8999-999999999999",
      operation: "updated",
      priorLocator: "notes/a.md",
      targetLocator: null,
      finalFingerprint: { sha256: "1".repeat(64), sizeBytes: 12, mediaType: "text/plain" },
    };
    const observation: VaultObservation = {
      eventSequence: 4,
      sourceId: marker.sourceId,
      operation: marker.operation,
      priorLocator: marker.priorLocator,
      targetLocator: null,
      fingerprint: marker.finalFingerprint,
    };
    const operation: RemoteApplyOperation = {
      eventSequence: 4,
      eventId: "88888888-8888-4888-8888-888888888888",
      sourceId: marker.sourceId,
      operation: "updated",
      priorLocator: "notes/a.md",
      targetLocator: null,
      baseFingerprint: null,
      finalFingerprint: marker.finalFingerprint,
      tempToken: null,
      rollbackToken: null,
      state: "prepared",
      safeErrorCode: null,
    };
    expect(marker.eventSequence).toBe(observation.eventSequence);
    expect(operation.state).toBe("prepared");
  });
});
