/**
 * The local-only current-note status projection for the plugin settings UI.
 *
 * A status holds a normalized Vault path solely so the local renderer can
 * identify a note. This module performs no I/O, logging, telemetry or HTTP
 * serialization; callers must keep its path-bearing result on-device.
 */

import type { FrozenFingerprint, JournalEvent, JournalSafeErrorLabel } from "./contracts";

export type LocalNoteSyncState =
  | "synced"
  | "queued"
  | "syncing"
  | "retrying"
  | "policy_blocked"
  | "conflict"
  | "reconcile_required";

export interface LocalNoteSyncStatus {
  readonly normalizedPath: string;
  readonly state: LocalNoteSyncState;
  readonly policyRevisionNumber: number | null;
  readonly retryAtEpochMs: number | null;
  readonly reason: JournalSafeErrorLabel | null;
}

/** The local journal facts needed to derive one current note status. */
export interface LocalNoteSyncStatusInput {
  readonly normalizedPath: string;
  readonly policyRevisionNumber: number | null;
  readonly observedFingerprint: FrozenFingerprint;
  readonly latestEvent: JournalEvent | null;
  readonly isReconcileRequired: boolean;
}

/** Project one note from its latest durable journal event and current file mapping. */
export function projectLocalNoteSyncStatus(
  input: LocalNoteSyncStatusInput,
): LocalNoteSyncStatus {
  const base = {
    normalizedPath: input.normalizedPath,
    policyRevisionNumber: input.policyRevisionNumber,
  };
  if (input.isReconcileRequired || input.latestEvent === null) {
    return { ...base, state: "reconcile_required", retryAtEpochMs: null, reason: null };
  }

  const event = input.latestEvent;
  switch (event.state) {
    case "queued":
      return { ...base, state: "queued", retryAtEpochMs: null, reason: null };
    case "preflight":
    case "uploading":
      return { ...base, state: "syncing", retryAtEpochMs: null, reason: null };
    case "waiting_retry":
      return {
        ...base,
        state: "retrying",
        retryAtEpochMs: event.nextEligibleRetryEpochMs,
        reason: event.safeError,
      };
    case "excluded_policy":
      return { ...base, state: "policy_blocked", retryAtEpochMs: null, reason: event.safeError };
    case "blocked_conflict":
      return { ...base, state: "conflict", retryAtEpochMs: null, reason: event.safeError };
    case "committed":
    case "no_change":
      return fingerprintsMatch(event.fingerprint, input.observedFingerprint)
        ? { ...base, state: "synced", retryAtEpochMs: null, reason: null }
        : { ...base, state: "reconcile_required", retryAtEpochMs: null, reason: null };
    case "blocked_size":
    case "deferred_lifecycle":
    case "integrity_failed":
      return {
        ...base,
        state: "reconcile_required",
        retryAtEpochMs: null,
        reason: event.safeError,
      };
  }
}

function fingerprintsMatch(left: FrozenFingerprint, right: FrozenFingerprint): boolean {
  return (
    left.sha256 === right.sha256 &&
    left.sizeBytes === right.sizeBytes &&
    left.mediaType === right.mediaType
  );
}
