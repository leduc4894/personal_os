/**
 * Tests of the device-sync closed reason and stage contracts (device cursor
 * and manifest reconciliation, task 7).
 *
 * These tests pin the exact closed vocabularies every later device-sync
 * plugin task (pull, apply, reconcile, ack) must surface its failures
 * through: the four reason families, the stage unions of the four trail
 * failure kinds, the correlation record and the diagnostics facade port.
 * A foreign string must not type-check into any of them, and the server
 * reasons must stay members of the generated server error registry.
 */

import type { components } from "@workspace/api-client";
import { describe, expect, it } from "vitest";

import {
  DEVICE_SYNC_ACTION_REASONS,
  DEVICE_SYNC_APPLY_STAGES,
  DEVICE_SYNC_COMPOSITION_READ_STAGES,
  DEVICE_SYNC_CREDENTIAL_STAGES,
  DEVICE_SYNC_CURSOR_STAGES,
  DEVICE_SYNC_LOCAL_REASONS,
  DEVICE_SYNC_RECONCILE_STAGES,
  DEVICE_SYNC_SERVER_REASONS,
  DEVICE_SYNC_TRANSPORT_REASONS,
} from "./contracts";
import type {
  ApplyFailureStage,
  CompositionReadStage,
  CredentialFailureStage,
  CursorFailureStage,
  DeviceSyncFailureCorrelation,
  DeviceSyncReason,
  ReconcileFailureStage,
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
