/**
 * The read-side schema surface of the journal's device-sync
 * reconciliation tables (device cursor and manifest reconciliation, task
 * 8, spec 8).
 *
 * This module owns parsing and validating the schema-v7 rows back into
 * the frozen contract shapes: the `device_sync_state` singleton, one
 * remote-apply operation and one echo marker. Every read fails closed as
 * the existing `journal_image_invalid` closed reason the moment a row
 * violates the closed vocabularies — a foreign state, operation or
 * barrier reason token is image corruption, never a passthrough value.
 *
 * Like the journal modules it depends on, this module imports no Node.js,
 * Electron or `FileSystemAdapter` API at module load time: the only
 * runtime dependency is the journal's own closed vocabulary constants
 * (sql.js-backed), so the whole `src/device-sync` tree stays loadable on
 * mobile.
 *
 * Privacy (spec 9): parsed records are local-only journal retention; the
 * closed tokens below are the only strings that may reach a status
 * surface.
 */

import type { FrozenFingerprint } from "../journal/contracts";
import { JOURNAL_STORE_ERROR_REASONS, journalStoreError } from "../journal/sqlite-database";
import type { SqliteQueryResult } from "../journal/sqlite-database";
import {
  DEVICE_SYNC_ACTION_REASONS,
  DEVICE_SYNC_EVENT_OPERATIONS,
  DEVICE_SYNC_LOCAL_REASONS,
  DEVICE_SYNC_REMOTE_APPLY_STATES,
  DEVICE_SYNC_SERVER_REASONS,
  DEVICE_SYNC_TRANSPORT_REASONS,
} from "./contracts";
import type {
  DeviceSyncReason,
  DeviceSyncState,
  EchoMarker,
  RemoteApplyOperation,
} from "./contracts";

// --- closed token validation ---------------------------------------------------------------------

/**
 * Every closed reason token of the {@link DeviceSyncReason} union: the
 * four device-sync families plus the journal store reasons. A persisted
 * reason outside this set is image corruption.
 */
export const DEVICE_SYNC_REASON_TOKENS: readonly string[] = [
  ...DEVICE_SYNC_SERVER_REASONS,
  ...DEVICE_SYNC_ACTION_REASONS,
  ...DEVICE_SYNC_TRANSPORT_REASONS,
  ...DEVICE_SYNC_LOCAL_REASONS,
  ...JOURNAL_STORE_ERROR_REASONS,
];

/** Whether one value carries a closed {@link DeviceSyncReason} token. */
export function isDeviceSyncReason(value: unknown): value is DeviceSyncReason {
  return typeof value === "string" && DEVICE_SYNC_REASON_TOKENS.includes(value);
}

/** Whether one value carries a closed device event operation token. */
export function isDeviceEventOperation(value: unknown): value is RemoteApplyOperation["operation"] {
  return (
    typeof value === "string" &&
    (DEVICE_SYNC_EVENT_OPERATIONS as readonly string[]).includes(value)
  );
}

/** Whether one value carries a closed remote-apply state token. */
export function isDeviceSyncRemoteApplyState(
  value: unknown,
): value is RemoteApplyOperation["state"] {
  return (
    typeof value === "string" &&
    (DEVICE_SYNC_REMOTE_APPLY_STATES as readonly string[]).includes(value)
  );
}

// --- the state singleton ---------------------------------------------------------------------------

/** The structural read seam every device-sync schema reader uses. */
export interface DeviceSyncSchemaReader {
  readAll(sql: string): SqliteQueryResult[];
}

const DEVICE_SYNC_STATE_COLUMNS = [
  "applied_sequence",
  "acknowledged_sequence",
  "observation_generation",
  "barrier_generation",
  "barrier_reason",
  "active_manifest_run_id",
  "manifest_checkpoint_sequence",
  "manifest_final_digest",
] as const;

function firstRow(result: readonly SqliteQueryResult[]): readonly unknown[] | null {
  return result[0]?.values[0] ?? null;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}

function isNullableText(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || isNonNegativeInteger(value);
}

/**
 * Parse one `device_sync_state` row into the frozen contract shape. A
 * contract violation (missing row, negative cursor, foreign barrier
 * reason) is image corruption and fails closed.
 */
export function parseDeviceSyncStateRow(row: readonly unknown[] | null): DeviceSyncState {
  if (row === null) {
    throwImageInvalid();
  }
  const [
    appliedSequence,
    acknowledgedSequence,
    observationGeneration,
    barrierGeneration,
    barrierReason,
    activeManifestRunId,
    manifestCheckpointSequence,
    manifestFinalDigest,
  ] = row;
  if (
    !isNonNegativeInteger(appliedSequence) ||
    !isNonNegativeInteger(acknowledgedSequence) ||
    acknowledgedSequence > appliedSequence ||
    !isNonNegativeInteger(observationGeneration) ||
    !isNullableNonNegativeInteger(barrierGeneration) ||
    (barrierReason !== null && !isDeviceSyncReason(barrierReason)) ||
    !isNullableText(activeManifestRunId) ||
    !isNullableNonNegativeInteger(manifestCheckpointSequence) ||
    !isNullableText(manifestFinalDigest)
  ) {
    throwImageInvalid();
  }
  return {
    appliedSequence,
    acknowledgedSequence,
    observationGeneration,
    barrierGeneration,
    barrierReason: barrierReason as DeviceSyncReason | null,
    activeManifestRunId,
    manifestCheckpointSequence,
    manifestFinalDigest,
  };
}

/**
 * Read the zeroed-or-advanced reconciliation state singleton of one
 * journal database (spec 8). Missing or foreign-valued state fails closed
 * as `journal_image_invalid`.
 */
export function readDeviceSyncState(reader: DeviceSyncSchemaReader): DeviceSyncState {
  const row = firstRow(
    reader.readAll(
      `select ${DEVICE_SYNC_STATE_COLUMNS.join(", ")} from device_sync_state where singleton_key = 1;`,
    ),
  );
  return parseDeviceSyncStateRow(row);
}

// --- remote apply operations ------------------------------------------------------------------------

const REMOTE_APPLY_COLUMNS = [
  "event_sequence",
  "event_id",
  "source_id",
  "operation",
  "prior_locator",
  "target_locator",
  "base_sha256",
  "base_size_bytes",
  "base_media_type",
  "final_sha256",
  "final_size_bytes",
  "final_media_type",
  "temp_token",
  "rollback_token",
  "state",
  "safe_error_code",
] as const;

function parseNullableFingerprint(
  sha256: unknown,
  sizeBytes: unknown,
  mediaType: unknown,
): FrozenFingerprint | null {
  if (sha256 === null && sizeBytes === null && mediaType === null) {
    return null;
  }
  if (typeof sha256 !== "string" || !isNonNegativeInteger(sizeBytes) || typeof mediaType !== "string") {
    throwImageInvalid();
  }
  return { sha256, sizeBytes, mediaType };
}

/** Parse one `remote_apply_operations` row; a contract violation is image corruption. */
export function parseRemoteApplyRow(row: readonly unknown[] | null): RemoteApplyOperation {
  if (row === null) {
    throwImageInvalid();
  }
  const [
    eventSequence,
    eventId,
    sourceId,
    operation,
    priorLocator,
    targetLocator,
    baseSha256,
    baseSizeBytes,
    baseMediaType,
    finalSha256,
    finalSizeBytes,
    finalMediaType,
    tempToken,
    rollbackToken,
    state,
    safeErrorCode,
  ] = row;
  if (
    !isPositiveInteger(eventSequence) ||
    typeof eventId !== "string" ||
    typeof sourceId !== "string" ||
    !isDeviceEventOperation(operation) ||
    !isNullableText(priorLocator) ||
    !isNullableText(targetLocator) ||
    !isNullableText(tempToken) ||
    !isNullableText(rollbackToken) ||
    !isDeviceSyncRemoteApplyState(state) ||
    (safeErrorCode !== null && !isDeviceSyncReason(safeErrorCode))
  ) {
    throwImageInvalid();
  }
  return {
    eventSequence,
    eventId,
    sourceId,
    operation,
    priorLocator,
    targetLocator,
    baseFingerprint: parseNullableFingerprint(baseSha256, baseSizeBytes, baseMediaType),
    finalFingerprint: parseNullableFingerprint(finalSha256, finalSizeBytes, finalMediaType),
    tempToken,
    rollbackToken,
    state,
    safeErrorCode: safeErrorCode as DeviceSyncReason | null,
  };
}

/** The column list of `remote_apply_operations` reads. */
export const REMOTE_APPLY_OPERATION_COLUMNS: readonly string[] = [...REMOTE_APPLY_COLUMNS];

// --- echo markers ------------------------------------------------------------------------------------

const ECHO_MARKER_COLUMN_LIST = [
  "event_sequence",
  "source_id",
  "operation",
  "prior_locator",
  "target_locator",
  "final_sha256",
  "final_size_bytes",
  "final_media_type",
] as const;

/** Parse one `echo_markers` row; a contract violation is image corruption. */
export function parseEchoMarkerRow(row: readonly unknown[] | null): EchoMarker {
  if (row === null) {
    throwImageInvalid();
  }
  const [
    eventSequence,
    sourceId,
    operation,
    priorLocator,
    targetLocator,
    finalSha256,
    finalSizeBytes,
    finalMediaType,
  ] = row;
  if (
    !isPositiveInteger(eventSequence) ||
    typeof sourceId !== "string" ||
    !isDeviceEventOperation(operation) ||
    !isNullableText(priorLocator) ||
    !isNullableText(targetLocator)
  ) {
    throwImageInvalid();
  }
  return {
    eventSequence,
    sourceId,
    operation,
    priorLocator,
    targetLocator,
    finalFingerprint: parseNullableFingerprint(finalSha256, finalSizeBytes, finalMediaType),
  };
}

/** The column list of `echo_markers` reads. */
export const ECHO_MARKER_COLUMNS: readonly string[] = [...ECHO_MARKER_COLUMN_LIST];

// --- internals ----------------------------------------------------------------------------------------

function throwImageInvalid(): never {
  throw journalStoreError("journal_image_invalid");
}
