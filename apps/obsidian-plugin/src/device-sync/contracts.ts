/**
 * The closed device-sync failure vocabularies (device cursor and manifest
 * reconciliation, task 7) and journal state contracts (task 8, spec 8).
 *
 * This module is the portable contract surface every later device-sync
 * plugin task (pull, apply, reconcile, acknowledge) reports its failures
 * through. It owns the four closed reason families, the closed stage
 * vocabularies of the four trail failure kinds, the correlation record
 * of the diagnostics facade, the closed journal state vocabularies and
 * record shapes of the schema-v7 reconciliation state, and the
 * {@link DeviceSyncRepository} port. It carries NO runtime dependency on
 * the journal engine, Node, Electron or the Obsidian adapter — only
 * type-level imports — so the whole `src/device-sync` tree stays loadable
 * on mobile.
 *
 * Privacy (spec 9, 14): every member is a closed token. A path, locator,
 * digest, credential, hostname, provider detail or any other free-form
 * string can never type-check into these vocabularies, and the server
 * family is compile-time bound to the generated server error registry.
 */

import type { components } from "@workspace/api-client";

import type { FrozenFingerprint } from "../journal/contracts";
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
  "device_apply_recovery_abandoned",
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

// --- the journal state vocabularies (task 8, spec 8) ---------------------------------------------------

/**
 * The closed canonical device-event operations of the server registry:
 * every member is a registered `DeviceEventType`, so an unregistered
 * snake_case string fails `tsc --noEmit` here. Content operations
 * (`created`, `updated`) stage temporary bytes; the four lifecycle
 * operations apply locator/tombstone-shaped mutations.
 */
export const DEVICE_SYNC_EVENT_OPERATIONS = [
  "created",
  "updated",
  "renamed",
  "moved",
  "deleted",
  "restored",
] as const satisfies readonly components["schemas"]["DeviceEventType"][];

/** One canonical device event operation. */
export type DeviceEventOperation = (typeof DEVICE_SYNC_EVENT_OPERATIONS)[number];

/**
 * The closed remote-apply states of spec 8.1/11: `prepared` (durable
 * intent, no Vault effect), `temp_verified` (staging bytes verified —
 * content operations only), `vault_mutated` (the operation-shaped Vault
 * effect completed), `locally_applied` (terminal outcome plus cursor
 * recorded in one journal generation) and `server_acknowledged` (the
 * server cursor receipt landed).
 */
export const DEVICE_SYNC_REMOTE_APPLY_STATES = [
  "prepared",
  "temp_verified",
  "vault_mutated",
  "locally_applied",
  "server_acknowledged",
] as const;

/** One remote-apply operation state. */
export type DeviceSyncRemoteApplyState = (typeof DEVICE_SYNC_REMOTE_APPLY_STATES)[number];

/**
 * The closed deterministic manifest action kinds of spec 12.3: every
 * member is a registered `ManifestActionKind` of the generated server
 * error registry.
 */
export const MANIFEST_ACTION_KINDS = [
  "upload",
  "download",
  "apply_tombstone",
  "conflict",
  "no_change",
  "excluded",
] as const satisfies readonly components["schemas"]["ManifestActionKind"][];

/** One planned manifest action kind. */
export type ManifestActionKind = (typeof MANIFEST_ACTION_KINDS)[number];

/**
 * The closed terminal outcomes of one device event (spec 11): the local
 * cursor advances only on one of these — an applied remote change, a
 * proven self-origin no-op, a durable conflict, a handled tombstone or a
 * policy-excluded event.
 */
export const TERMINAL_DEVICE_EVENT_OUTCOMES = [
  "applied",
  "self_origin_no_op",
  "conflict",
  "tombstone_handled",
  "excluded",
] as const;

/** One terminal-safe device event outcome. */
export type TerminalDeviceEventOutcome = (typeof TERMINAL_DEVICE_EVENT_OUTCOMES)[number];

/**
 * The closed local progress outcomes of one planned manifest action:
 * `received` when the frozen action row has been read from the server,
 * `terminal_safe` once its local outcome is durably settled.
 */
export const MANIFEST_ACTION_PROGRESS_OUTCOMES = ["received", "terminal_safe"] as const;

/** One manifest action progress outcome. */
export type ManifestActionProgressOutcome = (typeof MANIFEST_ACTION_PROGRESS_OUTCOMES)[number];

// --- the reconciliation state and repository port (task 8) ---------------------------------------------

/**
 * The durable device-sync reconciliation state of the journal's
 * `device_sync_state` singleton (spec 8): the local applied cursor, the
 * last server-acknowledged cursor, the monotonic Vault observation
 * generation, the active repair barrier (closed reason readable through
 * status) and the resumable manifest run checkpoint/final digest.
 */
export interface DeviceSyncState {
  readonly appliedSequence: number;
  readonly acknowledgedSequence: number;
  readonly observationGeneration: number;
  readonly barrierGeneration: number | null;
  readonly barrierReason: DeviceSyncReason | null;
  readonly activeManifestRunId: string | null;
  readonly manifestCheckpointSequence: number | null;
  readonly manifestFinalDigest: string | null;
}

/** One explicit repair barrier start (spec 12.1): the frozen observation generation and its closed reason. */
export interface RepairBarrierInput {
  readonly generation: number;
  readonly reason: DeviceSyncReason;
}

/**
 * One accepted manifest page receipt (spec 7.3/12.1): the run identity,
 * the exact ordered page number/entry count/digest, the immutable run
 * checkpoint (bound with the first accepted page) and the finalized
 * digest once the run finalized (null before).
 */
export interface LocalManifestPageReceipt {
  readonly manifestRunId: string;
  readonly pageNumber: number;
  readonly entryCount: number;
  readonly pageDigest: string;
  readonly checkpointSequence: number;
  readonly finalDigest: string | null;
}

/** One planned manifest action's local progress (spec 12.4). */
export interface LocalManifestActionProgress {
  readonly manifestRunId: string;
  readonly actionIndex: number;
  readonly actionKind: ManifestActionKind;
  readonly outcome: ManifestActionProgressOutcome;
  readonly reason: DeviceSyncReason | null;
}

/**
 * The durable prepare of one remote apply operation (spec 8.1, 10.3):
 * persisted BEFORE any Vault mutation. Only local correctness evidence —
 * no bytes, credential, object key, URL or provider response.
 */
export interface PreparedRemoteApply {
  readonly eventSequence: number;
  readonly eventId: string;
  readonly sourceId: string;
  readonly operation: DeviceEventOperation;
  readonly priorLocator: string | null;
  readonly targetLocator: string | null;
  readonly baseFingerprint: FrozenFingerprint | null;
  readonly finalFingerprint: FrozenFingerprint | null;
  readonly tempToken: string | null;
  readonly rollbackToken: string | null;
}

/**
 * One remote-apply state transition (spec 8.1, 11): the target closed
 * state plus the opaque tokens that become durable with it (the staging
 * sibling token at `temp_verified`, the rollback token at
 * `vault_mutated`). Absent token fields leave the stored token untouched.
 */
export interface RemoteApplyTransition {
  readonly eventSequence: number;
  readonly state: DeviceSyncRemoteApplyState;
  readonly tempToken?: string | null | undefined;
  readonly rollbackToken?: string | null | undefined;
}

/** One terminal-safe device event outcome recorded with its cursor advance (spec 11). */
export interface TerminalDeviceEvent {
  readonly eventSequence: number;
  readonly outcome: TerminalDeviceEventOutcome;
  readonly reason: DeviceSyncReason | null;
}

/**
 * The completion of one repair run (spec 7.3, 12.4): the exact planned
 * run, its checkpoint and the barrier generation that started the
 * repair — all three must match the durable state.
 */
export interface CompleteLocalRepair {
  readonly manifestRunId: string;
  readonly checkpointSequence: number;
  readonly barrierGeneration: number;
}

/** One stored remote-apply operation read back for crash recovery (spec 8.1, 11). */
export interface RemoteApplyOperation {
  readonly eventSequence: number;
  readonly eventId: string;
  readonly sourceId: string;
  readonly operation: DeviceEventOperation;
  readonly priorLocator: string | null;
  readonly targetLocator: string | null;
  readonly baseFingerprint: FrozenFingerprint | null;
  readonly finalFingerprint: FrozenFingerprint | null;
  readonly tempToken: string | null;
  readonly rollbackToken: string | null;
  readonly state: DeviceSyncRemoteApplyState;
  readonly safeErrorCode: DeviceSyncReason | null;
}

/**
 * One exact echo marker (spec 8.2): binds the server event sequence,
 * source, operation, applicable prior/target locators and the expected
 * final fingerprint (null for delete — its proof is the absent prior
 * locator plus the retained tombstone mapping).
 */
export interface EchoMarker {
  readonly eventSequence: number;
  readonly sourceId: string;
  readonly operation: DeviceEventOperation;
  readonly priorLocator: string | null;
  readonly targetLocator: string | null;
  readonly finalFingerprint: FrozenFingerprint | null;
}

/**
 * One watcher/recovery observation offered for echo suppression
 * (spec 8.2): the event sequence the observation is attributed to plus
 * the operands an exact marker match requires. A member that is null
 * where the marker pins a value never matches.
 */
export interface VaultObservation {
  readonly eventSequence: number;
  readonly sourceId: string | null;
  readonly operation: DeviceEventOperation | null;
  readonly priorLocator: string | null;
  readonly targetLocator: string | null;
  readonly fingerprint: FrozenFingerprint | null;
}

/**
 * The durable device-sync reconciliation repository (task 8, spec 8, 11,
 * 12): every mutation runs inside the journal's single serialized writer;
 * a terminal event and its cursor advance land in one generation; an
 * invariant blocker persists a closed `barrierReason` readable through
 * status before the closed store failure propagates. Ordinary sql.js /
 * store errors propagate as their existing closed
 * `JournalStoreErrorReason` — the repository never catches them merely to
 * continue.
 */
export interface DeviceSyncRepository {
  readState(): DeviceSyncState;
  nextObservationGeneration(): Promise<number>;
  startRepairBarrier(input: RepairBarrierInput): Promise<void>;
  persistRepairBarrierReason(reason: DeviceSyncReason): Promise<void>;
  recordManifestPage(input: LocalManifestPageReceipt): Promise<void>;
  recordManifestAction(input: LocalManifestActionProgress): Promise<void>;
  prepareRemoteApply(input: PreparedRemoteApply): Promise<void>;
  abandonRemoteApply(eventSequence: number): Promise<void>;
  transitionRemoteApply(input: RemoteApplyTransition): Promise<void>;
  terminalizeEvent(input: TerminalDeviceEvent): Promise<void>;
  recordServerAcknowledgement(sequence: number): Promise<void>;
  completeRepair(input: CompleteLocalRepair): Promise<void>;
  readUnfinishedApply(): RemoteApplyOperation | null;
  recordEchoMarker(input: EchoMarker): Promise<void>;
  readEchoMarker(eventSequence: number): EchoMarker | null;
  matchAndConsumeEcho(input: VaultObservation): Promise<boolean>;
}
