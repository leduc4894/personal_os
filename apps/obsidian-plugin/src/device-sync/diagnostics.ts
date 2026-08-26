/**
 * The device-sync diagnostics facade (device cursor and manifest
 * reconciliation, task 7).
 *
 * One thin synchronous facade over the durable closed-token diagnostics
 * trail: every method appends exactly ONE fire-and-forget trail entry —
 * the closed kind, the closed stage token, the closed reason, then the
 * gated correlation facts (the registered server error code between the
 * reason and the UUID-gated request id). The facade is observe-only: a
 * stalled, rejecting or throwing trail can never alter the sync outcome,
 * and the methods never throw.
 *
 * Privacy (spec 9, 14): no path, locator, digest, credential, hostname or
 * provider detail can reach a trail token — the correlation gate admits
 * only a canonical UUID request id and a registered server error code, and
 * a foreign value of either records nothing.
 */

import { SYNC_API_ENVELOPE_ERROR_CODES } from "../journal/sync-api";
import type { SyncApiEnvelopeErrorCode } from "../journal/sync-api";
import type {
  SyncDiagnosticKind,
  SyncDiagnosticToken,
  SyncDiagnosticsTrail,
} from "../journal/sync-diagnostics-trail";
import { envelopeRequestId } from "../journal/sync-diagnostics-trail";
import type {
  ApplyFailureStage,
  CredentialFailureStage,
  CursorFailureStage,
  DeviceSyncDiagnostics,
  DeviceSyncFailureCorrelation,
  DeviceSyncReason,
  ReconcileFailureStage,
} from "./contracts";
import { DEVICE_SYNC_SERVER_REASONS } from "./contracts";

/**
 * The registered server error codes a device-sync correlation may carry:
 * the Task 6 device-sync server family plus the journal-lane envelope
 * subset the sync client already whitelists. A code outside the union —
 * an edge challenge fragment, a free-form string, an unregistered future
 * code — records nothing.
 */
const REGISTERED_SERVER_ERROR_CODE_SET: ReadonlySet<string> = new Set<string>([
  ...DEVICE_SYNC_SERVER_REASONS,
  ...SYNC_API_ENVELOPE_ERROR_CODES,
]);

/** One registered server error code admitted as a closed trail token. */
type RegisteredServerErrorCode =
  | (typeof DEVICE_SYNC_SERVER_REASONS)[number]
  | SyncApiEnvelopeErrorCode;

/** Whether one raw wire value is a registered server error code. */
function isRegisteredServerErrorCode(value: string): value is RegisteredServerErrorCode {
  return REGISTERED_SERVER_ERROR_CODE_SET.has(value);
}

/**
 * Build the closed token list of one failure observation: the stage, the
 * closed reason, then the gated correlation facts. An untrusted request id
 * or an unregistered code is dropped entirely — never recorded, rendered
 * or logged.
 */
function buildFailureTokens(
  stage: SyncDiagnosticToken,
  reason: DeviceSyncReason,
  correlation: DeviceSyncFailureCorrelation | undefined,
): SyncDiagnosticToken[] {
  const tokens: SyncDiagnosticToken[] = [stage, reason];
  if (correlation !== undefined) {
    if (
      correlation.wireErrorCode !== null &&
      isRegisteredServerErrorCode(correlation.wireErrorCode)
    ) {
      tokens.push(correlation.wireErrorCode);
    }
    if (correlation.requestId !== null) {
      const requestIdToken = envelopeRequestId(correlation.requestId);
      if (requestIdToken !== null) {
        tokens.push(requestIdToken);
      }
    }
  }
  return tokens;
}

/** The observe-only append: never throws, never rejects, never blocks. */
function appendObservation(
  trail: SyncDiagnosticsTrail,
  kind: SyncDiagnosticKind,
  tokens: readonly SyncDiagnosticToken[],
): void {
  try {
    void trail.append({ kind, tokens }).catch(() => undefined);
  } catch {
    // A broken trail seam is a diagnostics defect, never a sync outcome:
    // the observation is dropped and the sync operation continues.
  }
}

/**
 * Build the mandatory diagnostics surface over one durable trail. Every
 * later Child 6 plugin catch path reports through this facade so the exact
 * stage and reason of every closed failure reaches a readable surface.
 */
export function createDeviceSyncDiagnostics(trail: SyncDiagnosticsTrail): DeviceSyncDiagnostics {
  return {
    cursorFailure(
      stage: CursorFailureStage,
      reason: DeviceSyncReason,
      correlation?: DeviceSyncFailureCorrelation,
    ): void {
      appendObservation(trail, "cursor_failure", buildFailureTokens(stage, reason, correlation));
    },
    applyFailure(
      stage: ApplyFailureStage,
      reason: DeviceSyncReason,
      correlation?: DeviceSyncFailureCorrelation,
    ): void {
      appendObservation(trail, "apply_failure", buildFailureTokens(stage, reason, correlation));
    },
    reconcileFailure(
      stage: ReconcileFailureStage,
      reason: DeviceSyncReason,
      correlation?: DeviceSyncFailureCorrelation,
    ): void {
      appendObservation(
        trail,
        "reconcile_failure",
        buildFailureTokens(stage, reason, correlation),
      );
    },
    credentialFailure(stage: CredentialFailureStage, reason: DeviceSyncReason): void {
      appendObservation(trail, "credential_failure", [stage, reason]);
    },
  };
}
