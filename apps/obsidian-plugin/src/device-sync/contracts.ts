/**
 * The closed device-sync failure vocabularies (device cursor and manifest
 * reconciliation, task 7).
 *
 * This module is the portable contract surface every later device-sync
 * plugin task (pull, apply, reconcile, acknowledge) reports its failures
 * through. It owns the four closed reason families, the closed stage
 * vocabularies of the four trail failure kinds and the correlation record
 * of the diagnostics facade. It carries NO runtime dependency on the
 * journal engine, Node, Electron or the Obsidian adapter — only type-level
 * imports — so the whole `src/device-sync` tree stays loadable on mobile.
 *
 * Privacy (spec 9, 14): every member is a closed token. A path, locator,
 * digest, credential, hostname, provider detail or any other free-form
 * string can never type-check into these vocabularies, and the server
 * family is compile-time bound to the generated server error registry.
 */

import type { components } from "@workspace/api-client";

import type { JournalStoreErrorReason } from "../journal/sqlite-database";

// --- the four closed reason families ----------------------------------------------------------------

/**
 * The closed server-rejection reasons of the Task 6 device routes: every
 * member is a registered `ErrorCode` of the generated server error
 * registry, so an unregistered snake_case string fails `tsc --noEmit`
 * here, not at a diagnostic surface.
 */
export const DEVICE_SYNC_SERVER_REASONS = [
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
] as const satisfies readonly components["schemas"]["ErrorCode"][];

/**
 * The closed manifest action refusal reasons: why one reconciled manifest
 * row could not (or must not) become a local mutation. Members of the
 * server registry's `ManifestActionReason` vocabulary.
 */
export const DEVICE_SYNC_ACTION_REASONS = [
  "device_manifest_identity_ambiguous",
  "device_manifest_local_diverged",
  "device_manifest_target_occupied",
  "device_manifest_action_stale",
  "device_manifest_policy_excluded",
] as const;

/** The closed transport-layer failure reasons shared with the journal lanes. */
export const DEVICE_SYNC_TRANSPORT_REASONS = [
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "access_expired",
  "login_required",
] as const;

/** The closed local-side failure reasons of apply and manifest capture. */
export const DEVICE_SYNC_LOCAL_REASONS = [
  "device_apply_trash_failed",
  "device_apply_vault_failed",
  "device_apply_recovery_ambiguous",
  "device_manifest_capture_failed",
] as const;

/**
 * The one closed reason vocabulary of every device-sync failure surface: a
 * server rejection, a manifest action refusal, a transport condition, a
 * local apply/capture failure or a journal store reason. Nothing else.
 */
export type DeviceSyncReason =
  | (typeof DEVICE_SYNC_SERVER_REASONS)[number]
  | (typeof DEVICE_SYNC_ACTION_REASONS)[number]
  | (typeof DEVICE_SYNC_TRANSPORT_REASONS)[number]
  | (typeof DEVICE_SYNC_LOCAL_REASONS)[number]
  | JournalStoreErrorReason;

// --- the closed stage vocabularies (spec 14.1) --------------------------------------------------------

/** The closed stages of a cursor-pull or cursor-acknowledge failure. */
export const DEVICE_SYNC_CURSOR_STAGES = ["pull", "acknowledge"] as const;

/** The closed stages of a remote-event apply failure. */
export const DEVICE_SYNC_APPLY_STAGES = [
  "prepare",
  "download",
  "verify_temp",
  "vault_mutation",
  "verify_final",
  "local_commit",
  "recovery",
  "trash",
] as const;

/** The closed stages of a manifest reconciliation failure. */
export const DEVICE_SYNC_RECONCILE_STAGES = [
  "start",
  "page",
  "finalize",
  "actions",
  "complete",
] as const;

/** The closed stages of a credential failure (both pre-contact). */
export const DEVICE_SYNC_CREDENTIAL_STAGES = ["access_missing", "refresh_failed"] as const;

/**
 * The closed read-site stages of a composition read failure: the four
 * once-per-session journal composition reads of the plugin root whose
 * swallowed throws record the `composition_read_failure` trail kind.
 */
export const DEVICE_SYNC_COMPOSITION_READ_STAGES = [
  "status_read",
  "note_status_read",
  "retry_schedule_read",
  "sync_status_read",
] as const;

/** One cursor failure stage. */
export type CursorFailureStage = (typeof DEVICE_SYNC_CURSOR_STAGES)[number];

/** One apply failure stage. */
export type ApplyFailureStage = (typeof DEVICE_SYNC_APPLY_STAGES)[number];

/** One reconcile failure stage. */
export type ReconcileFailureStage = (typeof DEVICE_SYNC_RECONCILE_STAGES)[number];

/** One credential failure stage. */
export type CredentialFailureStage = (typeof DEVICE_SYNC_CREDENTIAL_STAGES)[number];

/** One composition read failure stage. */
export type CompositionReadStage = (typeof DEVICE_SYNC_COMPOSITION_READ_STAGES)[number];

// --- the failure correlation record -------------------------------------------------------------------

/**
 * The wire correlation facts one device-sync failure may carry alongside
 * its closed reason: the failing server envelope's opaque request id (only
 * the UUID-gated trail token wrapper may admit it) and the envelope's
 * registered server error code. Both are null when no canonical envelope
 * answered; a raw string value of either never reaches a trail token.
 */
export interface DeviceSyncFailureCorrelation {
  readonly requestId: string | null;
  readonly wireErrorCode: string | null;
}

// --- the diagnostics facade port -----------------------------------------------------------------------

/**
 * The mandatory diagnostics surface of every Child 6 plugin task: one
 * fire-and-forget observation per failure, each carrying the exact closed
 * stage and reason (plus the gated correlation facts where one exists).
 * The methods are synchronous and observe-only — a failing trail append
 * can never alter the sync outcome.
 */
export interface DeviceSyncDiagnostics {
  cursorFailure(
    stage: CursorFailureStage,
    reason: DeviceSyncReason,
    correlation?: DeviceSyncFailureCorrelation,
  ): void;
  applyFailure(
    stage: ApplyFailureStage,
    reason: DeviceSyncReason,
    correlation?: DeviceSyncFailureCorrelation,
  ): void;
  reconcileFailure(
    stage: ReconcileFailureStage,
    reason: DeviceSyncReason,
    correlation?: DeviceSyncFailureCorrelation,
  ): void;
  credentialFailure(stage: CredentialFailureStage, reason: DeviceSyncReason): void;
}
