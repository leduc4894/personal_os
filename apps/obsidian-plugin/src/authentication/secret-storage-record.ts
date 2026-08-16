/**
 * The crash-safe versioned SecretStorage record of the plugin credential
 * lifecycle (spec 11.1, 11.4, 13.3, 14.2, 19).
 *
 * One well-known record name holds exactly one JSON value at a time: the
 * pending-grant polling secret during onboarding, the active refresh record
 * between rotations, or a credential-free tombstone after a terminal
 * outcome. Obsidian's SecretStorage offers `setSecret`/`getSecret` only —
 * there is no delete, so clearing means overwriting with the tombstone and
 * this module never claims to remove the key.
 */

import { DeviceAuthError } from "./contracts";
import type { SecretStorageRecordAdapter } from "./contracts";

/**
 * The well-known record name. Lowercase ASCII letters, digits and dashes only,
 * satisfying the Obsidian SecretStorage identifier grammar (spec 19).
 */
export const DEVICE_CREDENTIAL_RECORD_NAME = "knowledge-workspace-device-credential";

const SECRET_RECORD_NAME_PATTERN = /^[a-z0-9-]+$/;

/** Whether one record name satisfies the SecretStorage identifier grammar. */
export function isSecretRecordNameValid(recordName: string): boolean {
  return SECRET_RECORD_NAME_PATTERN.test(recordName);
}

/** The closed cleared reasons of the tombstone (spec 11.4, 13.5, 14.2). */
export const CLEARED_REASONS = [
  "grant_denied",
  "grant_expired",
  "login_cancelled",
  "grant_invalid",
  "token_reuse",
  "credential_invalid",
  "device_revoked",
  "self_disconnect",
] as const;

export type ClearedReason = (typeof CLEARED_REASONS)[number];

export interface PendingGrantSecretRecord {
  readonly record_version: 1;
  readonly state: "pending_grant";
  readonly polling_secret: string;
}

export interface ActiveDeviceSecretRecord {
  readonly record_version: 1;
  readonly state: "active";
  readonly refresh_credential: string;
  readonly refresh_generation: number;
  readonly pending_rotation_id: string | null;
}

export interface ClearedSecretRecord {
  readonly record_version: 1;
  readonly state: "cleared";
  readonly cleared_reason: ClearedReason;
}

export type DeviceSecretRecord =
  | PendingGrantSecretRecord
  | ActiveDeviceSecretRecord
  | ClearedSecretRecord;

const RECORD_VERSION = 1;

function isClearedReason(value: unknown): value is ClearedReason {
  return (
    typeof value === "string" && (CLEARED_REASONS as readonly string[]).includes(value)
  );
}

/**
 * Strictly parse one stored JSON value into a closed record. Corrupt, foreign
 * or future values parse to null — callers preserve the raw value and treat
 * the record as absent rather than guessing.
 */
export function parseDeviceSecretRecord(value: string | null): DeviceSecretRecord | null {
  if (typeof value !== "string") {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value) as unknown;
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const candidate = parsed as Record<string, unknown>;
  if (candidate["record_version"] !== RECORD_VERSION) {
    return null;
  }
  if (candidate["state"] === "pending_grant") {
    if (typeof candidate["polling_secret"] !== "string") {
      return null;
    }
    return {
      record_version: RECORD_VERSION,
      state: "pending_grant",
      polling_secret: candidate["polling_secret"],
    };
  }
  if (candidate["state"] === "active") {
    if (typeof candidate["refresh_credential"] !== "string") {
      return null;
    }
    if (typeof candidate["refresh_generation"] !== "number") {
      return null;
    }
    const pendingRotationId = candidate["pending_rotation_id"];
    if (
      pendingRotationId !== null &&
      typeof pendingRotationId !== "string"
    ) {
      return null;
    }
    return {
      record_version: RECORD_VERSION,
      state: "active",
      refresh_credential: candidate["refresh_credential"],
      refresh_generation: candidate["refresh_generation"],
      pending_rotation_id: pendingRotationId,
    };
  }
  if (candidate["state"] === "cleared") {
    if (!isClearedReason(candidate["cleared_reason"])) {
      return null;
    }
    return {
      record_version: RECORD_VERSION,
      state: "cleared",
      cleared_reason: candidate["cleared_reason"],
    };
  }
  return null;
}

/** Read the current record through the adapter (null when absent/corrupt). */
export function readDeviceSecretRecord(
  store: SecretStorageRecordAdapter,
  recordName: string,
): DeviceSecretRecord | null {
  return parseDeviceSecretRecord(store.getSecret(recordName));
}

/**
 * Write one serialized record and verify it by reading it back. The Obsidian
 * API documents no atomic-durability semantics, so the readback gate is the
 * plugin's own guarantee before any network action (spec 13.3); a mismatch
 * aborts the caller with a local error instead of proceeding.
 */
function writeVerifiedRecord(
  store: SecretStorageRecordAdapter,
  recordName: string,
  record: DeviceSecretRecord,
): void {
  const serialized = JSON.stringify(record);
  store.setSecret(recordName, serialized);
  if (store.getSecret(recordName) !== serialized) {
    throw new DeviceAuthError("secret_storage_unverified", {
      status: 0,
      message: "the credential record could not be verified after writing",
      isLocal: true,
    });
  }
}

/** Persist the pending-grant polling secret before the browser opens (11.1). */
export function writePendingGrantRecord(
  store: SecretStorageRecordAdapter,
  recordName: string,
  pollingSecret: string,
): void {
  writeVerifiedRecord(store, recordName, {
    record_version: RECORD_VERSION,
    state: "pending_grant",
    polling_secret: pollingSecret,
  });
}

/** Persist the complete active record, including the pending rotation identity. */
export function writeActiveDeviceRecord(
  store: SecretStorageRecordAdapter,
  recordName: string,
  fields: {
    refresh_credential: string;
    refresh_generation: number;
    pending_rotation_id: string | null;
  },
): void {
  writeVerifiedRecord(store, recordName, {
    record_version: RECORD_VERSION,
    state: "active",
    refresh_credential: fields.refresh_credential,
    refresh_generation: fields.refresh_generation,
    pending_rotation_id: fields.pending_rotation_id,
  });
}

/**
 * Overwrite the record with the credential-free tombstone (spec 11.4, 13.3,
 * 14.2). This clears the stored value's secrets; it never claims to delete
 * the SecretStorage key itself.
 */
export function writeClearedTombstone(
  store: SecretStorageRecordAdapter,
  recordName: string,
  clearedReason: ClearedReason,
): void {
  writeVerifiedRecord(store, recordName, {
    record_version: RECORD_VERSION,
    state: "cleared",
    cleared_reason: clearedReason,
  });
}
