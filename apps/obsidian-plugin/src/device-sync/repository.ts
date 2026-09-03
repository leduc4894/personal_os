/**
 * The durable device-sync reconciliation repository (device cursor and
 * manifest reconciliation, task 8, spec 8, 11, 12).
 *
 * The repository owns the schema-v7 reconciliation tables of the portable
 * journal: the `device_sync_state` singleton (applied/acknowledged
 * cursors, monotonic observation generation, repair barrier, resumable
 * manifest run), the manifest page/action progress of one repair run,
 * the crash-safe `remote_apply_operations` lattice and the exact
 * `echo_markers`. Every mutation runs inside the journal's single
 * serialized writer; a terminal event and its cursor advance land in one
 * generation (spec 11), and a page/action replay must be exact (spec 6.3).
 *
 * Invariant blockers (a cursor gap or regression, an acknowledgement
 * ahead of the applied cursor, a manifest replay mismatch, an illegal
 * remote-apply transition) persist the closed `barrierReason` inside the
 * same transaction — readable through status — and then reject with the
 * closed `journal_mutation_failed` store reason. Ordinary sql.js / store
 * errors propagate as their existing closed `JournalStoreErrorReason`;
 * the repository never catches them merely to continue.
 *
 * Privacy (spec 9): rows keep locators, digests and opaque tokens for
 * local recovery only; every persisted reason is a closed
 * `DeviceSyncReason` token and every thrown failure a closed store
 * reason — no raw content, credential or provider detail ever reaches a
 * row, error or status surface.
 *
 * Like the journal modules this module imports no Node.js, Electron or
 * Obsidian file-system adapter API at module load time, so it stays
 * loadable on mobile.
 */

import type { FrozenFingerprint } from "../journal/contracts";
import { isFrozenFingerprintShape } from "../journal/fingerprint";
import { journalStoreError } from "../journal/sqlite-database";
import type { SqliteMutationSession, SqliteQueryResult } from "../journal/sqlite-database";
import {
  DEVICE_SYNC_EVENT_OPERATIONS,
  DEVICE_SYNC_REMOTE_APPLY_STATES,
  MANIFEST_ACTION_KINDS,
  MANIFEST_ACTION_PROGRESS_OUTCOMES,
  TERMINAL_DEVICE_EVENT_OUTCOMES,
} from "./contracts";
import type {
  CompleteLocalRepair,
  DeviceSyncReason,
  DeviceSyncRepository as DeviceSyncRepositoryPort,
  DeviceSyncRemoteApplyState,
  DeviceSyncState,
  DeviceEventOperation,
  EchoMarker,
  LocalManifestActionProgress,
  LocalManifestPageReceipt,
  PreparedRemoteApply,
  RemoteApplyOperation,
  RemoteApplyTransition,
  RepairBarrierInput,
  TerminalDeviceEvent,
  VaultObservation,
} from "./contracts";
import {
  ECHO_MARKER_COLUMNS,
  REMOTE_APPLY_OPERATION_COLUMNS,
  isDeviceSyncReason,
  parseEchoMarkerRow,
  parseRemoteApplyRow,
  readDeviceSyncState,
} from "./schema";
import type { DeviceSyncSchemaReader } from "./schema";

// --- the structural database slice ---------------------------------------------------------------

/**
 * The narrow database seam the device-sync repository depends on (the
 * lifecycle-repository precedent): the serialized mutation queue plus the
 * read-only query seam. `SqliteDatabase` and the persistence-composed
 * journal slice both satisfy it, so the repository composes into the
 * same single writer — no parallel SQL surface.
 */
export interface DeviceSyncRepositoryDatabase {
  runSerializedMutation<T>(
    operation: (session: SqliteMutationSession) => T | Promise<T>,
  ): Promise<T>;
  readAll(sql: string): SqliteQueryResult[];
}

export interface DeviceSyncRepositoryOptions {
  readonly database: DeviceSyncRepositoryDatabase;
}

// --- closed value validation ---------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256_HEX_PATTERN = /^[0-9a-f]{64}$/;
const MAX_OPAQUE_TOKEN_LENGTH = 256;

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}

function isSha256Hex(value: unknown): value is string {
  return typeof value === "string" && SHA256_HEX_PATTERN.test(value);
}

function isClosedToken(value: unknown, closedSet: readonly string[]): boolean {
  return typeof value === "string" && closedSet.includes(value);
}

/** A locator operand: null or a non-empty control-character-free string. */
function isNullableLocator(value: unknown): value is string | null {
  if (value === null) {
    return true;
  }
  if (typeof value !== "string" || value.length === 0) {
    return false;
  }
  return Array.from(value).every((character) => {
    const codeUnit = character.charCodeAt(0);
    return codeUnit >= 0x20 && codeUnit !== 0x7f;
  });
}

/** An opaque local token (staging sibling / rollback handle). */
function isNullableOpaqueToken(value: unknown): value is string | null {
  if (value === null) {
    return true;
  }
  return isNullableLocator(value) && (value as string).length <= MAX_OPAQUE_TOKEN_LENGTH;
}

function isNullableFingerprint(value: unknown): value is FrozenFingerprint | null {
  if (value === null) {
    return true;
  }
  if (typeof value !== "object") {
    return false;
  }
  return isFrozenFingerprintShape(value as FrozenFingerprint);
}

/** Render one validated string as a SQL text literal (quotes doubled). */
function sqlText(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/** Render one nullable string as a SQL text literal. */
function sqlNullableText(value: string | null): string {
  return value === null ? "null" : sqlText(value);
}

function firstRow(result: readonly SqliteQueryResult[]): readonly unknown[] | null {
  return result[0]?.values[0] ?? null;
}

function isSameFingerprint(
  left: FrozenFingerprint | null,
  right: FrozenFingerprint | null,
): boolean {
  if (left === null || right === null) {
    return left === right;
  }
  return (
    left.sha256 === right.sha256 &&
    left.sizeBytes === right.sizeBytes &&
    left.mediaType === right.mediaType
  );
}

/** Whether the operation stages temporary bytes (spec 8.1: content applies only). */
function isContentApplyOperation(operation: DeviceEventOperation): boolean {
  return operation === "created" || operation === "updated";
}

/**
 * The legal remote-apply transition lattice of spec 8.1/11. Content
 * operations verify staged bytes before any mutation; the four lifecycle
 * operations have no temp stage. Recovery may mark `locally_applied`
 * from the last verified pre-mutation state once the operation-shaped
 * final proof matches (spec 11 recovery table).
 */
function isLegalRemoteApplyTransition(
  operation: DeviceEventOperation,
  from: DeviceSyncRemoteApplyState,
  to: DeviceSyncRemoteApplyState,
): boolean {
  const isContentApply = isContentApplyOperation(operation);
  switch (to) {
    case "prepared":
      // `prepared` is insert-only; nothing transitions back to it.
      return false;
    case "temp_verified":
      return from === "prepared" && isContentApply;
    case "vault_mutated":
      return (from === "prepared" && !isContentApply) || from === "temp_verified";
    case "locally_applied":
      return (
        from === "vault_mutated" ||
        (from === "temp_verified" && isContentApply) ||
        (from === "prepared" && !isContentApply)
      );
    case "server_acknowledged":
      return from === "locally_applied";
  }
}

// --- input validation ------------------------------------------------------------------------------

function validateRepairBarrierInput(input: RepairBarrierInput): void {
  if (!isNonNegativeInteger(input.generation) || !isDeviceSyncReason(input.reason)) {
    throw journalStoreError("journal_mutation_failed");
  }
}

function validateManifestPageReceipt(input: LocalManifestPageReceipt): void {
  if (
    !isUuid(input.manifestRunId) ||
    !isNonNegativeInteger(input.pageNumber) ||
    !isNonNegativeInteger(input.entryCount) ||
    !isSha256Hex(input.pageDigest) ||
    !isNonNegativeInteger(input.checkpointSequence) ||
    (input.finalDigest !== null && !isSha256Hex(input.finalDigest))
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
}

function validateManifestActionProgress(input: LocalManifestActionProgress): void {
  if (
    !isUuid(input.manifestRunId) ||
    !isNonNegativeInteger(input.actionIndex) ||
    !isClosedToken(input.actionKind, MANIFEST_ACTION_KINDS) ||
    !isClosedToken(input.outcome, MANIFEST_ACTION_PROGRESS_OUTCOMES) ||
    (input.reason !== null && !isDeviceSyncReason(input.reason))
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
}

function validatePreparedRemoteApply(input: PreparedRemoteApply): void {
  if (
    !isPositiveInteger(input.eventSequence) ||
    !isUuid(input.eventId) ||
    !isUuid(input.sourceId) ||
    !isClosedToken(input.operation, DEVICE_SYNC_EVENT_OPERATIONS) ||
    !isNullableLocator(input.priorLocator) ||
    !isNullableLocator(input.targetLocator) ||
    !isNullableFingerprint(input.baseFingerprint) ||
    !isNullableFingerprint(input.finalFingerprint) ||
    !isNullableOpaqueToken(input.tempToken) ||
    !isNullableOpaqueToken(input.rollbackToken)
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
}

function validateRemoteApplyTransition(input: RemoteApplyTransition): void {
  if (
    !isPositiveInteger(input.eventSequence) ||
    !isClosedToken(input.state, DEVICE_SYNC_REMOTE_APPLY_STATES) ||
    (input.tempToken !== undefined && !isNullableOpaqueToken(input.tempToken)) ||
    (input.rollbackToken !== undefined && !isNullableOpaqueToken(input.rollbackToken))
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
}

function validateTerminalDeviceEvent(input: TerminalDeviceEvent): void {
  if (
    !isPositiveInteger(input.eventSequence) ||
    !isClosedToken(input.outcome, TERMINAL_DEVICE_EVENT_OUTCOMES) ||
    (input.reason !== null && !isDeviceSyncReason(input.reason))
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
}

function validateCompleteLocalRepair(input: CompleteLocalRepair): void {
  if (
    !isUuid(input.manifestRunId) ||
    !isNonNegativeInteger(input.checkpointSequence) ||
    !isNonNegativeInteger(input.barrierGeneration)
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
}

function validateEchoMarker(input: EchoMarker): void {
  if (
    !isPositiveInteger(input.eventSequence) ||
    !isUuid(input.sourceId) ||
    !isClosedToken(input.operation, DEVICE_SYNC_EVENT_OPERATIONS) ||
    !isNullableLocator(input.priorLocator) ||
    !isNullableLocator(input.targetLocator) ||
    !isNullableFingerprint(input.finalFingerprint)
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
}

// --- the repository --------------------------------------------------------------------------------

/**
 * The durable device-sync reconciliation record store. Implements the
 * {@link DeviceSyncRepositoryPort} contract of `./contracts`; every
 * mutation flows through the single serialized writer, one transaction
 * at a time.
 */
export class DeviceSyncRepository implements DeviceSyncRepositoryPort {
  readonly #database: DeviceSyncRepositoryDatabase;

  constructor(options: DeviceSyncRepositoryOptions) {
    this.#database = options.database;
  }

  /** The durable reconciliation state singleton (read-only). */
  readState(): DeviceSyncState {
    return readDeviceSyncState(this.#database);
  }

  /**
   * Increment and return the next monotonic Vault observation generation
   * (spec 12.1). Observations continue under an active barrier and always
   * receive generations greater than the barrier's frozen one.
   */
  async nextObservationGeneration(): Promise<number> {
    return this.#database.runSerializedMutation((session) => {
      const state = this.#readState(session);
      const nextGeneration = state.observationGeneration + 1;
      session.exec(
        `update device_sync_state set observation_generation = ${nextGeneration} where singleton_key = 1;`,
      );
      return nextGeneration;
    });
  }

  /**
   * Start the one active repair barrier (spec 12.1): the barrier freezes
   * the CURRENT observation generation, so a second barrier, a run still
   * in progress, or a generation that is not current is refused.
   */
  async startRepairBarrier(input: RepairBarrierInput): Promise<void> {
    validateRepairBarrierInput(input);
    await this.#database.runSerializedMutation((session) => {
      const state = this.#readState(session);
      if (
        state.barrierGeneration !== null ||
        state.activeManifestRunId !== null ||
        input.generation !== state.observationGeneration
      ) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update device_sync_state set",
          `barrier_generation = ${input.generation},`,
          `barrier_reason = ${sqlText(input.reason)}`,
          "where singleton_key = 1;",
        ].join(" "),
      );
    });
  }

  /**
   * Refine the ACTIVE repair barrier's closed reason after the reconciler
   * diagnosed one itself (the apply lattice outrunning the run checkpoint)
   * — the same durable verdict the applier's prepare-path gap already
   * persists, so the resting state stays readable through status and a
   * later resume's recovery branch can key on it. Refused when no barrier
   * is active (the verdict is meaningless without one).
   */
  async persistRepairBarrierReason(reason: DeviceSyncReason): Promise<void> {
    await this.#database.runSerializedMutation((session) => {
      const state = this.#readState(session);
      if (state.barrierGeneration === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update device_sync_state set",
          `barrier_reason = ${sqlText(reason)}`,
          "where singleton_key = 1;",
        ].join(" "),
      );
    });
  }

  /**
   * Advance the ACTIVE repair barrier to a fresh observation generation
   * (the 2026-09-03 restart-asymmetry fix): the next manifest start
   * carries the new generation, which the server answers by expiring the
   * device's unfinished run — the sanctioned invalidation of server-side
   * run evidence the client just contradicted. Returns the new barrier
   * generation. Refused when no barrier is active.
   */
  async advanceRepairBarrierGeneration(reason: DeviceSyncReason): Promise<number> {
    return this.#database.runSerializedMutation((session) => {
      const state = this.#readState(session);
      if (state.barrierGeneration === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const nextGeneration = state.observationGeneration + 1;
      session.exec(
        [
          "update device_sync_state set",
          `observation_generation = ${nextGeneration},`,
          `barrier_generation = ${nextGeneration},`,
          `barrier_reason = ${sqlText(reason)}`,
          "where singleton_key = 1;",
        ].join(" "),
      );
      return nextGeneration;
    });
  }

  /**
   * Record one accepted manifest page receipt (spec 7.3, 12.1): the run
   * and its checkpoint bind with the first accepted page, pages land in
   * exact contiguous order, and a replayed page number must carry the
   * exact same evidence.
   */
  async recordManifestPage(input: LocalManifestPageReceipt): Promise<void> {
    validateManifestPageReceipt(input);
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (state.barrierGeneration === null) {
        return block("device_manifest_state_invalid");
      }
      if (state.activeManifestRunId === null) {
        if (input.pageNumber !== 0) {
          return block("device_manifest_page_invalid");
        }
      } else {
        if (input.manifestRunId !== state.activeManifestRunId) {
          return block("device_manifest_state_invalid");
        }
        if (
          state.manifestCheckpointSequence !== null &&
          input.checkpointSequence !== state.manifestCheckpointSequence
        ) {
          return block("device_manifest_state_invalid");
        }
      }
      if (input.finalDigest !== null) {
        if (state.manifestFinalDigest === null) {
          session.exec(
            `update device_sync_state set manifest_final_digest = ${sqlText(input.finalDigest)} where singleton_key = 1;`,
          );
        } else if (state.manifestFinalDigest !== input.finalDigest) {
          return block("device_manifest_digest_mismatch");
        }
      }
      const existingPage = firstRow(
        session.readRows(
          [
            "select entry_count, page_digest from manifest_page_progress",
            `where manifest_run_id = ${sqlText(input.manifestRunId)}`,
            `and page_number = ${input.pageNumber};`,
          ].join(" "),
        ),
      );
      if (existingPage !== null) {
        const [entryCount, pageDigest] = existingPage;
        if (entryCount !== input.entryCount || pageDigest !== input.pageDigest) {
          return block("device_manifest_page_replay_mismatch");
        }
        return;
      }
      const maxPageRow = firstRow(
        session.readRows(
          [
            "select max(page_number) from manifest_page_progress",
            `where manifest_run_id = ${sqlText(input.manifestRunId)};`,
          ].join(" "),
        ),
      );
      const expectedPageNumber =
        (isNonNegativeInteger(maxPageRow?.[0]) ? (maxPageRow?.[0] as number) : -1) + 1;
      if (input.pageNumber !== expectedPageNumber) {
        return block("device_manifest_page_invalid");
      }
      session.exec(
        [
          "insert into manifest_page_progress (manifest_run_id, page_number,",
          "entry_count, page_digest) values (",
          `${sqlText(input.manifestRunId)}, ${input.pageNumber},`,
          `${input.entryCount}, ${sqlText(input.pageDigest)});`,
        ].join(" "),
      );
      if (state.activeManifestRunId === null) {
        session.exec(
          [
            "update device_sync_state set",
            `active_manifest_run_id = ${sqlText(input.manifestRunId)},`,
            `manifest_checkpoint_sequence = ${input.checkpointSequence}`,
            "where singleton_key = 1;",
          ].join(" "),
        );
      }
    });
  }

  /**
   * Record one planned manifest action's local progress (spec 12.4): the
   * frozen action kind of an action index never changes, progress only
   * upgrades to `terminal_safe`, and a stale receipt never downgrades it.
   */
  async recordManifestAction(input: LocalManifestActionProgress): Promise<void> {
    validateManifestActionProgress(input);
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (state.activeManifestRunId !== input.manifestRunId) {
        return block("device_manifest_state_invalid");
      }
      const existingAction = firstRow(
        session.readRows(
          [
            "select action_kind, outcome from manifest_action_progress",
            `where manifest_run_id = ${sqlText(input.manifestRunId)}`,
            `and action_index = ${input.actionIndex};`,
          ].join(" "),
        ),
      );
      if (existingAction === null) {
        session.exec(
          [
            "insert into manifest_action_progress (manifest_run_id, action_index,",
            "action_kind, outcome, safe_reason_code) values (",
            `${sqlText(input.manifestRunId)}, ${input.actionIndex},`,
            `${sqlText(input.actionKind)}, ${sqlText(input.outcome)},`,
            `${sqlNullableText(input.reason)});`,
          ].join(" "),
        );
        return;
      }
      const [actionKind, outcome] = existingAction;
      if (actionKind !== input.actionKind) {
        return block("device_manifest_state_invalid");
      }
      if (input.outcome === "terminal_safe" && outcome !== "terminal_safe") {
        session.exec(
          [
            "update manifest_action_progress set outcome = 'terminal_safe',",
            `safe_reason_code = ${sqlNullableText(input.reason)}`,
            `where manifest_run_id = ${sqlText(input.manifestRunId)}`,
            `and action_index = ${input.actionIndex};`,
          ].join(" "),
        );
      }
      // An exact or stale replay of an already-recorded receipt is a no-op:
      // a terminal-safe action never downgrades.
    });
  }

  /**
   * Persist the durable prepare of one remote apply operation (spec 8.1,
   * 10.3) BEFORE any Vault mutation. An exact re-prepare of the same
   * still-`prepared` operation is idempotent; a conflicting re-prepare
   * contradicts the durable evidence and blocks.
   */
  async prepareRemoteApply(input: PreparedRemoteApply): Promise<void> {
    validatePreparedRemoteApply(input);
    await this.#runBlockedMutation((session, block) => {
      const existing = this.#readRemoteApplyRow(session, input.eventSequence);
      if (existing !== null) {
        if (existing.state !== "prepared" || !isSamePreparedOperation(existing, input)) {
          return block("device_apply_recovery_ambiguous");
        }
        return;
      }
      const baseFingerprint = input.baseFingerprint;
      const finalFingerprint = input.finalFingerprint;
      session.exec(
        [
          "insert into remote_apply_operations (event_sequence, event_id, source_id,",
          "operation, prior_locator, target_locator, base_sha256, base_size_bytes,",
          "base_media_type, final_sha256, final_size_bytes, final_media_type,",
          "temp_token, rollback_token, state) values (",
          `${input.eventSequence}, ${sqlText(input.eventId)}, ${sqlText(input.sourceId)},`,
          `${sqlText(input.operation)}, ${sqlNullableText(input.priorLocator)},`,
          `${sqlNullableText(input.targetLocator)},`,
          `${sqlNullableText(baseFingerprint?.sha256 ?? null)},`,
          `${baseFingerprint === null ? "null" : baseFingerprint.sizeBytes},`,
          `${sqlNullableText(baseFingerprint?.mediaType ?? null)},`,
          `${sqlNullableText(finalFingerprint?.sha256 ?? null)},`,
          `${finalFingerprint === null ? "null" : finalFingerprint.sizeBytes},`,
          `${sqlNullableText(finalFingerprint?.mediaType ?? null)},`,
          `${sqlNullableText(input.tempToken)}, ${sqlNullableText(input.rollbackToken)},`,
          "'prepared');",
        ].join(" "),
      );
    });
  }

  /**
   * Abandon one `prepared` remote apply intent together with its echo
   * marker. The caller must have PROVEN the Vault sits at the operation's
   * verified pre-mutation expectation first (the crash-safe recovery's
   * clean verdict): nothing was mutated, so the durable intent and its
   * suppression marker are safe to drop — which unblocks both a fresh
   * prepare and the reconciler's synthetic apply at the same sequence (the
   * server never redelivers an already-delivered event, so the abandoned
   * intent is re-converged through manifest reconciliation instead).
   */
  async abandonRemoteApply(eventSequence: number): Promise<void> {
    if (!isNonNegativeInteger(eventSequence)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const operation = this.#readRemoteApplyRow(session, eventSequence);
      if (operation === null || operation.state !== "prepared") {
        // Only a proven-clean prepared intent may be abandoned: any later
        // state carries a Vault effect the durable lattice still owns.
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        `delete from remote_apply_operations where event_sequence = ${eventSequence};`,
      );
      session.exec(`delete from echo_markers where event_sequence = ${eventSequence};`);
    });
  }

  /**
   * Transition one remote apply operation along the legal lattice of
   * spec 8.1/11, persisting the opaque staging/rollback tokens that
   * become durable with the state. Illegal, backwards or unknown-
   * sequence transitions contradict the durable evidence and block.
   */
  async transitionRemoteApply(input: RemoteApplyTransition): Promise<void> {
    validateRemoteApplyTransition(input);
    await this.#runBlockedMutation((session, block) => {
      const operation = this.#readRemoteApplyRow(session, input.eventSequence);
      if (operation === null) {
        return block("device_apply_recovery_ambiguous");
      }
      if (!isLegalRemoteApplyTransition(operation.operation, operation.state, input.state)) {
        return block("device_apply_recovery_ambiguous");
      }
      const tokenAssignments: string[] = [];
      if (input.tempToken !== undefined) {
        tokenAssignments.push(`temp_token = ${sqlNullableText(input.tempToken)}`);
      }
      if (input.rollbackToken !== undefined) {
        tokenAssignments.push(`rollback_token = ${sqlNullableText(input.rollbackToken)}`);
      }
      session.exec(
        [
          "update remote_apply_operations set",
          `state = ${sqlText(input.state)}`,
          ...(tokenAssignments.length > 0 ? [`, ${tokenAssignments.join(", ")}`] : []),
          `where event_sequence = ${input.eventSequence};`,
        ].join(" "),
      );
    });
  }

  /**
   * Record one terminal-safe device event outcome AND advance the local
   * applied cursor in the SAME serialized generation (spec 11): an
   * `applied` outcome requires the durable `vault_mutated` proof (or an
   * already-`locally_applied` recovery row), and any other terminal
   * outcome closes a dangling prepared row with its closed reason. A
   * non-contiguous sequence is a gap or regression blocker — the cursor
   * never advances on it.
   */
  async terminalizeEvent(input: TerminalDeviceEvent): Promise<void> {
    validateTerminalDeviceEvent(input);
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (input.eventSequence > state.appliedSequence + 1) {
        return block("device_cursor_gap");
      }
      if (input.eventSequence <= state.appliedSequence) {
        return block("device_cursor_regression");
      }
      const operation = this.#readRemoteApplyRow(session, input.eventSequence);
      if (input.outcome === "applied") {
        if (
          operation === null ||
          operation.state === "prepared" ||
          operation.state === "temp_verified"
        ) {
          return block("device_apply_recovery_ambiguous");
        }
        if (operation.state === "vault_mutated") {
          session.exec(
            `update remote_apply_operations set state = 'locally_applied' where event_sequence = ${input.eventSequence};`,
          );
        }
      } else if (
        operation !== null &&
        operation.state !== "locally_applied" &&
        operation.state !== "server_acknowledged"
      ) {
        session.exec(
          [
            "update remote_apply_operations set state = 'locally_applied',",
            `safe_error_code = ${sqlNullableText(input.reason)}`,
            `where event_sequence = ${input.eventSequence};`,
          ].join(" "),
        );
      }
      session.exec(
        `update device_sync_state set applied_sequence = ${input.eventSequence} where singleton_key = 1;`,
      );
    });
  }

  /**
   * Record the server's cursor acknowledgement (spec 7.2, 11). The
   * acknowledgement never runs ahead of the applied cursor (the debt
   * invariant), never regresses, and an exact replay is an idempotent
   * no-op. Every locally-applied operation through the acknowledged
   * sequence is marked server-acknowledged.
   */
  async recordServerAcknowledgement(sequence: number): Promise<void> {
    if (!isNonNegativeInteger(sequence)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (sequence < state.acknowledgedSequence) {
        return block("device_cursor_regression");
      }
      if (sequence === state.acknowledgedSequence) {
        return;
      }
      if (sequence > state.appliedSequence) {
        return block("device_cursor_ack_ahead");
      }
      session.exec(
        `update device_sync_state set acknowledged_sequence = ${sequence} where singleton_key = 1;`,
      );
      session.exec(
        [
          "update remote_apply_operations set state = 'server_acknowledged'",
          `where event_sequence <= ${sequence} and state = 'locally_applied';`,
        ].join(" "),
      );
    });
  }

  /**
   * Complete one repair run (spec 7.3, 12.4): the exact planned run, its
   * checkpoint and the barrier generation that started the repair must
   * all match the durable state. One transaction advances both cursors
   * to the checkpoint (the server authorized its cursor to `C` in the
   * same completion), clears the barrier and run fields, discards the
   * run's temporary page/action progress and settles every locally
   * applied operation through the checkpoint.
   */
  async completeRepair(input: CompleteLocalRepair): Promise<void> {
    validateCompleteLocalRepair(input);
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (
        state.barrierGeneration === null ||
        state.barrierGeneration !== input.barrierGeneration ||
        state.activeManifestRunId !== input.manifestRunId
      ) {
        return block("device_manifest_state_invalid");
      }
      if (input.checkpointSequence < state.appliedSequence) {
        return block("device_cursor_regression");
      }
      session.exec(
        [
          "update device_sync_state set",
          `applied_sequence = ${input.checkpointSequence},`,
          `acknowledged_sequence = ${input.checkpointSequence},`,
          "barrier_generation = null, barrier_reason = null,",
          "active_manifest_run_id = null, manifest_checkpoint_sequence = null,",
          "manifest_final_digest = null",
          "where singleton_key = 1;",
        ].join(" "),
      );
      session.exec(
        `delete from manifest_page_progress where manifest_run_id = ${sqlText(input.manifestRunId)};`,
      );
      session.exec(
        `delete from manifest_action_progress where manifest_run_id = ${sqlText(input.manifestRunId)};`,
      );
      session.exec(
        [
          "update remote_apply_operations set state = 'server_acknowledged'",
          `where event_sequence <= ${input.checkpointSequence} and state = 'locally_applied';`,
        ].join(" "),
      );
    });
  }

  /**
   * The oldest remote apply operation that still owes work (spec 11):
   * anything not yet server-acknowledged, lowest event sequence first.
   */
  readUnfinishedApply(): RemoteApplyOperation | null {
    const row = firstRow(
      this.#database.readAll(
        [
          `select ${REMOTE_APPLY_OPERATION_COLUMNS.join(", ")} from remote_apply_operations`,
          "where state != 'server_acknowledged'",
          "order by event_sequence asc limit 1;",
        ].join(" "),
      ),
    );
    return row === null ? null : parseRemoteApplyRow(row);
  }

  /**
   * One remote apply operation by its exact event sequence, or null
   * (read-only). The settle path of a repeatedly refused vault write
   * addresses the failed event's OWN row this way — the oldest-unfinished
   * read can name an earlier still-unacknowledged row instead.
   */
  readRemoteApply(eventSequence: number): RemoteApplyOperation | null {
    if (!isPositiveInteger(eventSequence)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          `select ${REMOTE_APPLY_OPERATION_COLUMNS.join(", ")} from remote_apply_operations`,
          `where event_sequence = ${eventSequence};`,
        ].join(" "),
      ),
    );
    return row === null ? null : parseRemoteApplyRow(row);
  }

  /**
   * Record one exact echo marker (spec 8.2) before the Vault mutation.
   * An exact duplicate is a no-op; a conflicting duplicate for one event
   * sequence contradicts the immutable server event and is refused.
   */
  async recordEchoMarker(input: EchoMarker): Promise<void> {
    validateEchoMarker(input);
    await this.#database.runSerializedMutation((session) => {
      const existing = this.#readEchoMarkerRow(session, input.eventSequence);
      if (existing !== null) {
        if (!isSameEchoMarker(existing, input)) {
          throw journalStoreError("journal_mutation_failed");
        }
        return;
      }
      const finalFingerprint = input.finalFingerprint;
      session.exec(
        [
          "insert into echo_markers (event_sequence, source_id, operation,",
          "prior_locator, target_locator, final_sha256, final_size_bytes,",
          "final_media_type) values (",
          `${input.eventSequence}, ${sqlText(input.sourceId)}, ${sqlText(input.operation)},`,
          `${sqlNullableText(input.priorLocator)}, ${sqlNullableText(input.targetLocator)},`,
          `${sqlNullableText(finalFingerprint?.sha256 ?? null)},`,
          `${finalFingerprint === null ? "null" : finalFingerprint.sizeBytes},`,
          `${sqlNullableText(finalFingerprint?.mediaType ?? null)});`,
        ].join(" "),
      );
    });
  }

  /** One exact echo marker by its event sequence, or null (read-only). */
  readEchoMarker(eventSequence: number): EchoMarker | null {
    if (!isPositiveInteger(eventSequence)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        `select ${ECHO_MARKER_COLUMNS.join(", ")} from echo_markers where event_sequence = ${eventSequence};`,
      ),
    );
    return row === null ? null : parseEchoMarkerRow(row);
  }

  /**
   * Match one watcher/recovery observation against the exact echo marker
   * of its event sequence (spec 8.2): every applicable member — source,
   * operation, prior/target locator, expected final fingerprint — must
   * match. Only an exact match consumes the marker; a mismatch keeps it.
   */
  async matchAndConsumeEcho(input: VaultObservation): Promise<boolean> {
    if (!isPositiveInteger(input.eventSequence)) {
      throw journalStoreError("journal_mutation_failed");
    }
    return this.#database.runSerializedMutation((session) => {
      const marker = this.#readEchoMarkerRow(session, input.eventSequence);
      if (marker === null || !isExactEchoMatch(marker, input)) {
        return false;
      }
      session.exec(`delete from echo_markers where event_sequence = ${input.eventSequence};`);
      return true;
    });
  }

  // --- internals --------------------------------------------------------------------------------------

  #readState(session: SqliteMutationSession): DeviceSyncState {
    const reader: DeviceSyncSchemaReader = {
      readAll: (sql: string): SqliteQueryResult[] => session.readRows(sql),
    };
    return readDeviceSyncState(reader);
  }

  #readRemoteApplyRow(
    session: SqliteMutationSession,
    eventSequence: number,
  ): RemoteApplyOperation | null {
    const row = firstRow(
      session.readRows(
        [
          `select ${REMOTE_APPLY_OPERATION_COLUMNS.join(", ")} from remote_apply_operations`,
          `where event_sequence = ${eventSequence};`,
        ].join(" "),
      ),
    );
    return row === null ? null : parseRemoteApplyRow(row);
  }

  #readEchoMarkerRow(session: SqliteMutationSession, eventSequence: number): EchoMarker | null {
    const row = firstRow(
      session.readRows(
        `select ${ECHO_MARKER_COLUMNS.join(", ")} from echo_markers where event_sequence = ${eventSequence};`,
      ),
    );
    return row === null ? null : parseEchoMarkerRow(row);
  }

  /**
   * Run one serialized mutation whose INVARIANT blockers persist the
   * closed barrier reason inside the same transaction and then reject
   * with the closed `journal_mutation_failed` store reason: the blocker
   * stays readable through status while the failure still propagates.
   * Ordinary store errors roll the whole transaction back untouched.
   */
  async #runBlockedMutation(
    operation: (
      session: SqliteMutationSession,
      block: (reason: DeviceSyncReason) => void,
    ) => void | Promise<void>,
  ): Promise<void> {
    let blockedReason: DeviceSyncReason | null = null;
    await this.#database.runSerializedMutation((session) =>
      operation(session, (reason: DeviceSyncReason): void => {
        const state = this.#readState(session);
        const barrierGeneration = state.barrierGeneration ?? state.observationGeneration;
        session.exec(
          [
            "update device_sync_state set",
            `barrier_generation = ${barrierGeneration},`,
            `barrier_reason = ${sqlText(reason)}`,
            "where singleton_key = 1;",
          ].join(" "),
        );
        blockedReason = reason;
      }),
    );
    if (blockedReason !== null) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
}

// --- exact-match helpers ------------------------------------------------------------------------------

/** Whether one stored still-`prepared` operation equals the re-prepared input exactly. */
function isSamePreparedOperation(
  stored: RemoteApplyOperation,
  input: PreparedRemoteApply,
): boolean {
  return (
    stored.eventId === input.eventId &&
    stored.sourceId === input.sourceId &&
    stored.operation === input.operation &&
    stored.priorLocator === input.priorLocator &&
    stored.targetLocator === input.targetLocator &&
    isSameFingerprint(stored.baseFingerprint, input.baseFingerprint) &&
    isSameFingerprint(stored.finalFingerprint, input.finalFingerprint) &&
    stored.tempToken === input.tempToken &&
    stored.rollbackToken === input.rollbackToken
  );
}

function isSameEchoMarker(stored: EchoMarker, input: EchoMarker): boolean {
  return (
    stored.sourceId === input.sourceId &&
    stored.operation === input.operation &&
    stored.priorLocator === input.priorLocator &&
    stored.targetLocator === input.targetLocator &&
    isSameFingerprint(stored.finalFingerprint, input.finalFingerprint)
  );
}

/**
 * The exact echo match of spec 8.2: every member the marker pins — the
 * source, the operation, each non-null locator operand and the expected
 * final fingerprint — must equal the observation's. A null observation
 * member where the marker pins a value never matches.
 */
function isExactEchoMatch(marker: EchoMarker, observation: VaultObservation): boolean {
  if (observation.sourceId === null || observation.sourceId !== marker.sourceId) {
    return false;
  }
  if (observation.operation === null || observation.operation !== marker.operation) {
    return false;
  }
  if (marker.priorLocator !== null && observation.priorLocator !== marker.priorLocator) {
    return false;
  }
  if (marker.targetLocator !== null && observation.targetLocator !== marker.targetLocator) {
    return false;
  }
  if (
    marker.finalFingerprint !== null &&
    !isSameFingerprint(observation.fingerprint, marker.finalFingerprint)
  ) {
    return false;
  }
  return true;
}
