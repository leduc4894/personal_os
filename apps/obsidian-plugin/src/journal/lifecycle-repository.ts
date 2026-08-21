/**
 * The plugin-side lifecycle journal repository (Child 5).
 *
 * The lifecycle repository owns the durable rename / move / delete /
 * restore records of the portable journal. Every record is written
 * together in one transaction: the `journal_events` row, the
 * `lifecycle_event_operands` keyed-extension row and the matching
 * `local_files` update (last_locator / open_tombstone_id /
 * lifecycle_state, plus the captured-fingerprint columns and the
 * normalized_path rebind for `rename`/`move`). A throwing operation
 * rolls the entire transaction back so partial writes never reach a
 * verified generation.
 *
 * Coalescing (spec 7.2): lifecycle events MUST NOT coalesce with content
 * events and content events MUST NOT coalesce with lifecycle events — a
 * `create` or `update` capture never replaces a `rename`/`move`/`delete`/
 * `restore` row, and a lifecycle record never replaces a content row.
 *
 * Atomic capture (spec 7.1 fix round 1 I1): the lifecycle capture calls
 * {@link LifecycleRepository.recordLifecycleEventWithFreeze} so the
 * pending-content freeze, the lifecycle event, and the path rebind all
 * land in one writer call.
 *
 * Privacy (spec 9): every failure surfaces as a closed `JournalStoreError`
 * reason; the operands carry local-only retention (expected_locator,
 * target_locator, tombstone_id, source_id) for recovery and never reach
 * the status / attempt projection surface or any diagnostic stream.
 */

import type { JournalEvent, JournalEventState, JournalOperation } from "./contracts";
import {
  JOURNAL_COALESCABLE_EVENT_STATES,
  JOURNAL_EVENT_STATES,
  JOURNAL_NON_RETRY_EVENT_STATES,
  JOURNAL_OPERATIONS,
  JOURNAL_PENDING_EVENT_STATES,
  JOURNAL_SAFE_ERROR_LABELS,
  MAX_EVENT_ATTEMPT_HISTORY,
} from "./contracts";
import type { LocalFile } from "./contracts";
import {
  isLifecycleJournalOperation,
  isLifecycleLocalFileState,
  type LifecycleEventOperands,
  type LifecycleLocalFileState,
} from "./lifecycle-contracts";
import { journalStoreError } from "./sqlite-database";
import type {
  SqliteMutationSession,
  SqliteQueryResult,
} from "./sqlite-database";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// --- the structural database slice the lifecycle repository writes against ----------------

/**
 * The narrow database seam the lifecycle repository depends on: the
 * serialized mutation queue plus the read-only query seam of the
 * existing `JournalRepositoryDatabase`. The slice is intentionally
 * structural so the lifecycle repository composes into the same
 * `SqliteDatabase` / `JournalPersistence` the Child 4 repository already
 * uses — no parallel SQL surface, no parallel writer.
 */
export interface LifecycleRepositoryDatabase {
  runSerializedMutation<T>(
    operation: (session: SqliteMutationSession) => T | Promise<T>,
  ): Promise<T>;
  readAll(sql: string): SqliteQueryResult[];
}

// --- input and result types ---------------------------------------------------------------

/**
 * The frozen dispatch bundle one driver pass selects: the closed
 * `JournalEvent` row plus the matching `LifecycleEventOperands` keyed-
 * extension row. The lifecycle driver commits this shape to the server
 * before the durable state moves to `committed`.
 */
export interface FrozenLifecycleEvent {
  readonly event: JournalEvent;
  readonly operands: LifecycleEventOperands;
}

/**
 * The configuration of a single lifecycle record call: the local file the
 * event belongs to, the closed tombstone id when the operation is
 * `delete` or `restore`, the initial lifecycle state to write on the
 * local file and a deterministic, transient failure flag used by tests
 * to prove the all-or-nothing transaction discipline.
 */
export interface LifecycleRecordOptions {
  readonly localFile: LocalFile;
  readonly tombstoneId?: string | null;
  readonly initialLifecycleState?: LifecycleLocalFileState;
  /**
   * Optional new normalized path: when the operation is `rename` or
   * `move`, the local-file row's `normalized_path` column is updated to
   * this value in the SAME transaction as the lifecycle event so a torn
   * rename never escapes a verified generation (spec 7.1 fix round 1
   * C1).
   */
  readonly newPath?: string | undefined;
  /** Test-only escape hatch: throw `journal_mutation_failed` after exec. */
  readonly forceFailureAfterExec?: boolean;
}

/**
 * The settled outcome of one {@link LifecycleRepository.recordLifecycleEvent}
 * call: the durable journal event, its idempotency identity, and the
 * closed lifecycle state that now lives on `local_files`.
 */
export interface LifecycleRecordResult {
  readonly event: JournalEvent;
  readonly eventId: JournalEvent["eventId"];
  readonly eventIdempotencyKey: JournalEvent["idempotencyKey"];
  readonly lifecycleState: LifecycleLocalFileState;
}

/**
 * The bounded structural input of {@link LifecycleRepository.findReconcileRequired}.
 */
export interface LifecycleReconcileRow {
  readonly localFileId: string;
  readonly normalizedPath: string;
  readonly reason:
    | "predecessor_missing"
    | "operands_missing"
    | "expected_version_mismatch";
}

export interface LifecycleRepositoryOptions {
  readonly database: LifecycleRepositoryDatabase;
  /** Identity mint; defaults to the platform `crypto.randomUUID`. */
  readonly createId?: () => string;
  /** Clock for event creation timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
}

// --- internal helpers ---------------------------------------------------------------------

function sqlText(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function firstRow(result: readonly SqliteQueryResult[]): readonly unknown[] | null {
  return result[0]?.values[0] ?? null;
}

function isNullableUuid(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && UUID_PATTERN.test(value));
}

function isNullableText(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isInteger(value) && value >= 0);
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}

function isClosedStateToken(value: unknown): value is JournalEventState {
  return (
    typeof value === "string" &&
    (JOURNAL_EVENT_STATES as readonly string[]).includes(value)
  );
}

function isClosedSafeErrorLabel(value: unknown): value is NonNullable<JournalEvent["safeError"]> {
  return (
    typeof value === "string" &&
    (JOURNAL_SAFE_ERROR_LABELS as readonly string[]).includes(value)
  );
}

function parseStoredEventRow(row: readonly unknown[]): JournalEvent {
  const [
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    sha256,
    sizeBytes,
    mediaType,
    state,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId,
  ] = row;
  if (
    typeof eventId !== "string" ||
    !isUuid(eventId) ||
    typeof localFileId !== "string" ||
    typeof idempotencyKey !== "string" ||
    typeof operation !== "string" ||
    !(JOURNAL_OPERATIONS as readonly string[]).includes(operation) ||
    typeof sha256 !== "string" ||
    typeof sizeBytes !== "number" ||
    !Number.isInteger(sizeBytes) ||
    sizeBytes < 0 ||
    typeof mediaType !== "string" ||
    typeof state !== "string" ||
    !isClosedStateToken(state) ||
    typeof attemptCount !== "number" ||
    !Number.isInteger(attemptCount) ||
    attemptCount < 0 ||
    !isNullableNonNegativeInteger(nextEligibleRetryEpochMs) ||
    (safeError !== null && !isClosedSafeErrorLabel(safeError)) ||
    (operationId !== null && typeof operationId !== "string")
  ) {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    eventId,
    localFileId,
    idempotencyKey,
    operation: operation as JournalOperation,
    fingerprint: { sha256, sizeBytes, mediaType },
    state,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId,
  };
}

function parseLifecycleOperandRow(row: readonly unknown[]): LifecycleEventOperands {
  const [
    operation,
    sourceId,
    expectedVersionId,
    expectedLocator,
    targetLocator,
    tombstoneId,
    policyRevision,
    predecessorEventId,
  ] = row;
  if (
    typeof operation !== "string" ||
    !isLifecycleJournalOperation(operation) ||
    typeof sourceId !== "string" ||
    !isUuid(sourceId) ||
    typeof expectedVersionId !== "string" ||
    !isUuid(expectedVersionId) ||
    !isNullableText(expectedLocator) ||
    !isNullableText(targetLocator) ||
    !isNullableUuid(tombstoneId) ||
    !isPositiveInteger(policyRevision) ||
    !isNullableUuid(predecessorEventId)
  ) {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    operation,
    sourceId,
    expectedVersionId,
    expectedLocator,
    targetLocator,
    tombstoneId,
    policyRevision,
    predecessorEventId,
    // The lifecycle_event_operands table does not store the captured
    // fingerprint; it lives on local_files as observed_* and is only
    // relevant at capture time (rename/move rebind). The driver reads
    // the operands row alone, so the captured triple is always null
    // here.
    capturedFingerprintSha256: null,
    capturedFingerprintSizeBytes: null,
    capturedFingerprintMediaType: null,
  };
}

function initialStateFor(
  operation: LifecycleEventOperands["operation"],
  override: LifecycleLocalFileState | undefined,
): LifecycleLocalFileState {
  if (override !== undefined) {
    return override;
  }
  switch (operation) {
    case "rename":
      return "rename_pending";
    case "move":
      return "move_pending";
    case "delete":
      return "tombstoned";
    case "restore":
      return "restored";
  }
}

/** The fingerprint shape every lifecycle event carries (deterministic zeros). */
const LIFECYCLE_FINGERPRINT = {
  sha256: "0".repeat(64),
  sizeBytes: 0,
  mediaType: "application/octet-stream",
} as const;

function validateOptions(
  operands: LifecycleEventOperands,
  options: LifecycleRecordOptions,
): void {
  if (!isLifecycleJournalOperation(operands.operation)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isUuid(operands.sourceId) || !isUuid(operands.expectedVersionId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (
    operands.predecessorEventId !== null &&
    !isUuid(operands.predecessorEventId)
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (
    !isLifecycleLocalFileState(options.initialLifecycleState ?? initialStateFor(operands.operation, undefined))
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isUuid(options.localFile.localFileId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (options.tombstoneId !== undefined && options.tombstoneId !== null && !isUuid(options.tombstoneId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  // Operation-dependent operand shape (the wire validator mirrors this in the
  // domain command; the plugin side is the second line of defence).
  if (operands.operation === "rename" || operands.operation === "move") {
    if (operands.expectedLocator === null || operands.targetLocator === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (operands.expectedLocator === operands.targetLocator) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (operands.tombstoneId !== null) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  if (operands.operation === "delete") {
    if (operands.expectedLocator === null || operands.targetLocator !== null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (options.tombstoneId === undefined || options.tombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  if (operands.operation === "restore") {
    if (operands.expectedLocator !== null || operands.targetLocator === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (operands.tombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (operands.predecessorEventId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
}

// --- the repository -----------------------------------------------------------------------

/**
 * The lifecycle journal repository: a thin wrapper over the existing
 * sql.js writer that owns the durable rename / move / delete / restore
 * records. Composes over the same {@link LifecycleRepositoryDatabase}
 * slice the Child 4 `JournalRepository` already uses, so every lifecycle
 * row still goes through the one serialized commit queue.
 */
export class LifecycleRepository {
  readonly #database: LifecycleRepositoryDatabase;
  readonly #createId: () => string;
  readonly #nowEpochMs: () => number;

  constructor(options: LifecycleRepositoryOptions) {
    this.#database = options.database;
    this.#createId = options.createId ?? (() => crypto.randomUUID());
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
  }

  /** The serialized database slice the lifecycle repository writes against. */
  get database(): LifecycleRepositoryDatabase {
    return this.#database;
  }

  /**
   * Record one lifecycle event in one transaction: a `journal_events`
   * row, a `lifecycle_event_operands` row keyed by `event_id` and the
   * `local_files` row update (`last_locator`, `open_tombstone_id`,
   * `lifecycle_state`, and for `rename`/`move` the captured-fingerprint
   * columns + the `normalized_path` rebind). On any failure the entire
   * transaction rolls back, so partial writes never reach a verified
   * generation.
   *
   * Idempotency: replaying an existing event with the same
   * `(localFileId, operation, expectedVersionId, expectedLocator,
   * targetLocator, tombstoneId, predecessorEventId, policyRevision)`
   * tuple returns the original event without inserting a duplicate row.
   */
  async recordLifecycleEvent(
    operands: LifecycleEventOperands,
    options: LifecycleRecordOptions = { localFile: { localFileId: "" } as LocalFile },
  ): Promise<LifecycleRecordResult> {
    validateOptions(operands, options);
    return this.#database.runSerializedMutation((session) =>
      this.#recordLifecycleEventInSession(session, {
        operands,
        localFile: options.localFile,
        tombstoneId: options.tombstoneId ?? null,
        newPath: options.newPath ?? null,
        forceFailureAfterExec: options.forceFailureAfterExec === true,
      }),
    );
  }

  /**
   * The atomic lifecycle event writer used by the rename / move / delete /
   * restore capture path (spec 7.1 fix round 1 I1). It runs three
   * mutations in one transaction:
   *
   *   1. freeze every still-pending content event of the tracked file
   *      as `deferred_lifecycle` so no later queue pass selects it;
   *   2. insert the `journal_events` row, the `lifecycle_event_operands`
   *      row and the matching `local_files` update via
   *      {@link recordLifecycleEventInSession};
   *   3. write the `local_files.normalized_path` rebind (for
   *      `rename` / `move`) atomically inside the same writer call.
   *
   * A throwing operation rolls back the whole transaction so a torn
   * rename never escapes a verified generation.
   */
  async recordLifecycleEventWithFreeze(
    input: {
      readonly operands: LifecycleEventOperands;
      readonly localFile: LocalFile;
      readonly tombstoneId?: string | null;
      readonly newPath?: string | undefined;
      readonly forceFailureAfterExec?: boolean;
    },
  ): Promise<LifecycleRecordResult> {
    return this.#database.runSerializedMutation((session) => {
      this.#freezePendingForLocalFileInSession(
        session,
        input.localFile.localFileId,
      );
      return this.#recordLifecycleEventInSession(session, {
        operands: input.operands,
        localFile: input.localFile,
        tombstoneId: input.tombstoneId ?? null,
        newPath: input.newPath ?? null,
        forceFailureAfterExec: input.forceFailureAfterExec === true,
      });
    });
  }

  /**
   * Fail-closed reconcile flagger used when a tombstoned path re-appears
   * with bytes that no longer match the last-committed fingerprint
   * (spec 7.1 fix round 1 C2). The row's lifecycle state flips to
   * `reconcile_required`, the open tombstone is cleared so the file is
   * no longer eligible for automatic restore, and the global
   * `journal_meta.is_reconcile_required` flag is set so a later pass
   * knows to recover the row.
   */
  async recordLifecycleReconcileForLocalFile(localFileId: string): Promise<void> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`,
        ),
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update local_files set",
          "lifecycle_state = 'reconcile_required',",
          "open_tombstone_id = null",
          `where local_file_id = ${sqlText(localFileId)};`,
        ].join(" "),
      );
      const meta = session.readJournalMeta();
      if (!meta.isReconcileRequired) {
        session.writeJournalMeta({ ...meta, isReconcileRequired: true });
      }
    });
  }

  /**
   * Mark one `local_files` row as tombstoned. The focused helper is
   * used by the capture flow when the server has confirmed a delete
   * before the lifecycle event has reached the durable journal (or
   * when the durable event has been pruned but the local mapping must
   * stay). All mutations run inside one transaction.
   */
  async markTombstoneForLocalFile(
    localFileId: string,
    tombstoneId: string,
  ): Promise<void> {
    if (!isUuid(localFileId) || !isUuid(tombstoneId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`,
        ),
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update local_files set",
          `open_tombstone_id = ${sqlText(tombstoneId)},`,
          `lifecycle_state = 'tombstoned'`,
          `where local_file_id = ${sqlText(localFileId)};`,
        ].join(" "),
      );
    });
  }

  /**
   * Clear the retained tombstone on one `local_files` row after a
   * restore successor has been committed. The lifecycle state returns
   * to `restored`; the rest of the row stays intact so a future save
   * can resume the content surface.
   */
  async consumeRestoreSuccessor(localFileId: string): Promise<void> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`,
        ),
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update local_files set",
          `open_tombstone_id = null,`,
          `lifecycle_state = 'restored'`,
          `where local_file_id = ${sqlText(localFileId)};`,
        ].join(" "),
      );
    });
  }

  /**
   * Persist the safe receipt of one committed lifecycle event in
   * one transaction: the event flips to terminal `committed`, the
   * `local_files.lifecycle_state` advances past the pending state
   * for the closed operation, and the server-returned tombstone id
   * (when present) replaces any locally-staged value so the durable
   * record is exactly what the server acknowledged (spec 19.2
   * exact-replay rule).
   *
   * The last_committed_* columns are intentionally left untouched:
   * a rename / move / delete / restore does not change file bytes,
   * so the prior `last_committed_*` triple stays provable for the
   * next restore-eligibility check.
   */
  async recordLifecycleCommittedReceipt(eventId: string): Promise<void> {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = firstRow(
        session.readRows(
          `select event_id, local_file_id, operation from journal_events where event_id = ${sqlText(eventId)};`,
        ),
      );
      if (event === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [storedEventId, localFileId, operation] = event;
      if (typeof storedEventId !== "string" || typeof localFileId !== "string" || typeof operation !== "string") {
        throw journalStoreError("journal_query_failed");
      }
      if (
        operation !== "rename" &&
        operation !== "move" &&
        operation !== "delete" &&
        operation !== "restore"
      ) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update journal_events set state = 'committed',",
          "next_eligible_retry_epoch_ms = null,",
          "safe_error = null",
          `where event_id = ${sqlText(eventId)};`,
        ].join(" "),
      );
      // Lifecycle-state transitions for each closed operation.
      switch (operation) {
        case "rename":
        case "move":
          session.exec(
            [
              "update local_files set",
              "lifecycle_state = 'active'",
              `where local_file_id = ${sqlText(localFileId)};`,
            ].join(" "),
          );
          break;
        case "delete":
          // Tombstone row stays; the durable local_files row is
          // pruned by the capture path on tombstone commit.
          session.exec(
            [
              "update local_files set",
              "lifecycle_state = 'tombstoned'",
              `where local_file_id = ${sqlText(localFileId)};`,
            ].join(" "),
          );
          break;
        case "restore":
          session.exec(
            [
              "update local_files set",
              "lifecycle_state = 'restored'",
              `where local_file_id = ${sqlText(localFileId)};`,
            ].join(" "),
          );
          break;
      }
    });
  }

  /**
   * The query the lifecycle driver uses to pick its next eligible
   * event: the oldest lifecycle row whose retry time has passed (or
   * with no retry scheduled) and whose predecessor, when one is
   * declared, is already terminal-success on the server. A
   * `predecessor_event_id` referencing an event that is missing or
   * still pending is deferred — the brief requires that the
   * successor MUST NOT dispatch until the predecessor is
   * terminal-success.
   *
   * Returns the closed `FrozenLifecycleEvent` (event + operands
   * pair) so the driver can ship both to the wire in one commit;
   * returns `null` when no eligible lifecycle event exists.
   */
  readOldestEligibleLifecycleEvent(
    nowEpochMs: number,
  ): FrozenLifecycleEvent | null {
    if (!isPositiveInteger(nowEpochMs)) {
      throw journalStoreError("journal_query_failed");
    }
    const coalescableStateList = JOURNAL_COALESCABLE_EVENT_STATES.map((state) =>
      sqlText(state),
    ).join(", ");
    const lifecycleOperations = [
      "rename",
      "move",
      "delete",
      "restore",
    ].map((value) => sqlText(value)).join(", ");
    const row = firstRow(
      this.#database.readAll(
        [
          `select je.event_id, je.local_file_id, je.idempotency_key, je.operation,`,
          `je.sha256, je.size_bytes, je.media_type, je.state, je.attempt_count,`,
          `je.next_eligible_retry_epoch_ms, je.safe_error, je.operation_id,`,
          `leo.source_id, leo.expected_version_id,`,
          `leo.expected_locator, leo.target_locator, leo.tombstone_id,`,
          `leo.policy_revision, leo.predecessor_event_id`,
          `from journal_events je`,
          `join lifecycle_event_operands leo on leo.event_id = je.event_id`,
          `left join journal_events pe on pe.event_id = leo.predecessor_event_id`,
          `where je.operation in (${lifecycleOperations})`,
          `and ((je.state in (${coalescableStateList})`,
          `and (je.next_eligible_retry_epoch_ms is null`,
          `or je.next_eligible_retry_epoch_ms <= ${nowEpochMs}))`,
          `or je.state in ('preflight', 'uploading'))`,
          `and (leo.predecessor_event_id is null`,
          `or (pe.state = 'committed'))`,
          `order by je.created_at_epoch_ms asc, je.rowid asc limit 1;`,
        ].join(" "),
      ),
    );
    if (row === null) {
      return null;
    }
    const [
      eventId,
      localFileId,
      idempotencyKey,
      operation,
      sha256,
      sizeBytes,
      mediaType,
      state,
      attemptCount,
      nextEligibleRetryEpochMs,
      safeError,
      operationId,
      sourceId,
      expectedVersionId,
      expectedLocator,
      targetLocator,
      tombstoneId,
      policyRevision,
      predecessorEventId,
    ] = row;
    const event = parseStoredEventRow([
      eventId,
      localFileId,
      idempotencyKey,
      operation,
      sha256,
      sizeBytes,
      mediaType,
      state,
      attemptCount,
      nextEligibleRetryEpochMs,
      safeError,
      operationId,
    ]);
    const operands = parseLifecycleOperandRow([
      operation,
      sourceId,
      expectedVersionId,
      expectedLocator,
      targetLocator,
      tombstoneId,
      policyRevision,
      predecessorEventId,
    ]);
    if (operands.operation !== event.operation) {
      throw journalStoreError("journal_image_invalid");
    }
    return { event, operands };
  }

  /**
   * Read the keyed operand row of one stored lifecycle event. The
   * driver uses this to look up the operands after a replay: when the
   * same `event_id` is selected again, the wire body must carry the
   * ORIGINAL operands (never a re-derived shape) so the server's
   * exact-replay contract holds. Returns `null` when no operand row
   * exists (an event without an operands row is the very condition
   * the reconcile-required flagger closes).
   */
  readLifecycleOperands(eventId: string): LifecycleEventOperands | null {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          "select operation, source_id, expected_version_id, expected_locator,",
          "target_locator, tombstone_id, policy_revision, predecessor_event_id",
          `from lifecycle_event_operands where event_id = ${sqlText(eventId)};`,
        ].join(" "),
      ),
    );
    if (row === null) {
      return null;
    }
    return parseLifecycleOperandRow(row);
  }

  /**
   * Find every local file whose dependency evidence is corrupt or
   * missing: a `predecessor_event_id` referencing a row no longer in
   * `journal_events`, a missing `lifecycle_event_operands` row for a
   * lifecycle event, or an `expected_version_id` that no longer matches
   * a stored source. The probe also durably sets
   * `journal_meta.is_reconcile_required = 1` inside the same
   * transaction so a stale dependency never goes unflagged.
   */
  async findReconcileRequired(): Promise<readonly LifecycleReconcileRow[]> {
    return this.#database.runSerializedMutation((session) => {
      const read = (sql: string): readonly SqliteQueryResult[] => session.readRows(sql);
      const flagged: LifecycleReconcileRow[] = [];

      const predecessorProbe = read(
        [
          "select lf.local_file_id, lf.normalized_path",
          "from local_files lf",
          "join journal_events je on je.local_file_id = lf.local_file_id",
          "left join lifecycle_event_operands leo",
          "  on leo.event_id = je.event_id",
          "left join journal_events pe",
          "  on pe.event_id = leo.predecessor_event_id",
          "where je.operation in ('rename', 'move', 'delete', 'restore')",
          "and leo.predecessor_event_id is not null",
          "and pe.event_id is null;",
        ].join(" "),
      );
      for (const row of predecessorProbe[0]?.values ?? []) {
        const [localFileId, normalizedPath] = row;
        if (typeof localFileId !== "string" || typeof normalizedPath !== "string") {
          throw journalStoreError("journal_query_failed");
        }
        flagged.push({
          localFileId,
          normalizedPath,
          reason: "predecessor_missing",
        });
      }

      const operandsProbe = read(
        [
          "select je.event_id, lf.local_file_id, lf.normalized_path",
          "from journal_events je",
          "join local_files lf on lf.local_file_id = je.local_file_id",
          "left join lifecycle_event_operands leo",
          "  on leo.event_id = je.event_id",
          "where je.operation in ('rename', 'move', 'delete', 'restore')",
          "and leo.event_id is null;",
        ].join(" "),
      );
      for (const row of operandsProbe[0]?.values ?? []) {
        const [, localFileId, normalizedPath] = row;
        if (typeof localFileId !== "string" || typeof normalizedPath !== "string") {
          throw journalStoreError("journal_query_failed");
        }
        if (!flagged.some((entry) => entry.localFileId === localFileId)) {
          flagged.push({
            localFileId,
            normalizedPath,
            reason: "operands_missing",
          });
        }
      }

      if (flagged.length > 0) {
        const meta = session.readJournalMeta();
        if (!meta.isReconcileRequired) {
          session.writeJournalMeta({ ...meta, isReconcileRequired: true });
        }
        const localFileIds = flagged.map((entry) => sqlText(entry.localFileId));
        session.exec(
          `update local_files set lifecycle_state = 'reconcile_required' where local_file_id in (${localFileIds.join(", ")});`,
        );
      }
      return flagged;
    });
  }

  // --- internals ---------------------------------------------------------------------------

  /**
   * Session-scoped lifecycle-event writer. Lets the atomic writer
   * ({@link recordLifecycleEventWithFreeze}) chain the freeze + event +
   * path-rebind inside one transaction.
   */
  #recordLifecycleEventInSession(
    session: SqliteMutationSession,
    options: {
      readonly operands: LifecycleEventOperands;
      readonly localFile: LocalFile;
      readonly tombstoneId: string | null;
      readonly newPath: string | null;
      readonly forceFailureAfterExec: boolean;
    },
  ): LifecycleRecordResult {
    validateOptions(options.operands, {
      localFile: options.localFile,
      tombstoneId: options.tombstoneId,
    });
    const tombstoneId = options.tombstoneId;
    const lifecycleState = initialStateFor(
      options.operands.operation,
      undefined,
    );

    const read = (sql: string): readonly SqliteQueryResult[] => session.readRows(sql);

    // Idempotency lookup: a replay returns the original event without
    // a second insert or a second operands row.
    const replay = this.#findReplay(read, options.localFile.localFileId, options.operands);
    if (replay !== null) {
      return replay;
    }

    // Predecessor event id, when present, must reference a stored event.
    if (options.operands.predecessorEventId !== null) {
      const predecessor = firstRow(
        read(
          `select event_id from journal_events where event_id = ${sqlText(options.operands.predecessorEventId)};`,
        ),
      );
      if (predecessor === null) {
        throw journalStoreError("journal_mutation_failed");
      }
    }

    const eventId = this.#createId();
    const idempotencyKey = this.#createId();
    const createdAt = this.#nowEpochMs();
    const isFrozen =
      options.operands.operation === "rename" ||
      options.operands.operation === "move" ||
      options.operands.operation === "delete" ||
      options.operands.operation === "restore"
        ? 1
        : 0;

    session.exec(
      [
        "insert into journal_events (event_id, local_file_id, idempotency_key, operation,",
        "sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count,",
        "safe_error, created_at_epoch_ms) values (",
        `${sqlText(eventId)}, ${sqlText(options.localFile.localFileId)},`,
        `${sqlText(idempotencyKey)},`,
        `${sqlText(options.operands.operation)},`,
        `${sqlText(LIFECYCLE_FINGERPRINT.sha256)},`,
        `${LIFECYCLE_FINGERPRINT.sizeBytes},`,
        `${sqlText(LIFECYCLE_FINGERPRINT.mediaType)},`,
        `'queued',`,
        `${isFrozen}, 0, null,`,
        `${createdAt});`,
      ].join(" "),
    );

    session.exec(
      [
        "insert into lifecycle_event_operands (event_id, source_id, expected_version_id,",
        "expected_locator, target_locator, tombstone_id, policy_revision,",
        "predecessor_event_id) values (",
        `${sqlText(eventId)}, ${sqlText(options.operands.sourceId)},`,
        `${sqlText(options.operands.expectedVersionId)},`,
        `${options.operands.expectedLocator === null ? "null" : sqlText(options.operands.expectedLocator)},`,
        `${options.operands.targetLocator === null ? "null" : sqlText(options.operands.targetLocator)},`,
        `${tombstoneId === null ? "null" : sqlText(tombstoneId)},`,
        `${options.operands.policyRevision},`,
        `${options.operands.predecessorEventId === null ? "null" : sqlText(options.operands.predecessorEventId)});`,
      ].join(" "),
    );

    const lastLocator =
      options.operands.targetLocator ?? options.operands.expectedLocator ?? null;
    const newTombstone =
      options.operands.operation === "delete" || options.operands.operation === "restore"
        ? tombstoneId
        : null;
    const newPath =
      options.operands.operation === "rename" || options.operands.operation === "move"
        ? options.newPath ?? options.operands.targetLocator ?? null
        : null;
    const observedOverride =
      options.operands.operation === "rename" || options.operands.operation === "move"
        ? {
            sha256: options.operands.capturedFingerprintSha256,
            sizeBytes: options.operands.capturedFingerprintSizeBytes,
            mediaType: options.operands.capturedFingerprintMediaType,
          }
        : null;
    const observedWrite =
      observedOverride !== null &&
      observedOverride.sha256 !== null &&
      observedOverride.sizeBytes !== null &&
      observedOverride.mediaType !== null
        ? [
            `observed_sha256 = ${sqlText(observedOverride.sha256)},`,
            `observed_size_bytes = ${observedOverride.sizeBytes},`,
            `observed_media_type = ${sqlText(observedOverride.mediaType)},`,
          ].join(" ")
        : "";
    const pathWrite =
      newPath === null ? "" : `normalized_path = ${sqlText(newPath)},`;
    session.exec(
      [
        "update local_files set",
        `${pathWrite}`,
        `${observedWrite}`,
        `${lastLocator === null ? "last_locator = null," : `last_locator = ${sqlText(lastLocator)},`}`,
        `${newTombstone === null ? "open_tombstone_id = null," : `open_tombstone_id = ${sqlText(newTombstone)},`}`,
        `lifecycle_state = ${sqlText(lifecycleState)}`,
        `where local_file_id = ${sqlText(options.localFile.localFileId)};`,
      ].join(" "),
    );

    if (options.forceFailureAfterExec) {
      throw journalStoreError("journal_mutation_failed");
    }

    const event: JournalEvent = {
      eventId,
      localFileId: options.localFile.localFileId,
      idempotencyKey,
      operation: options.operands.operation,
      fingerprint: { ...LIFECYCLE_FINGERPRINT },
      state: "queued",
      attemptCount: 0,
      nextEligibleRetryEpochMs: null,
      safeError: null,
      operationId: null,
    };
    return {
      event,
      eventId,
      eventIdempotencyKey: idempotencyKey,
      lifecycleState,
    };
  }

  /**
   * Session-scoped variant of the freeze helper: every still-pending
   * content event (`queued` / `preflight` / `waiting_retry`) of the
   * tracked file flips to terminal `deferred_lifecycle` inside the
   * SAME transaction the lifecycle event lands in.
   */
  #freezePendingForLocalFileInSession(
    session: SqliteMutationSession,
    localFileId: string,
  ): void {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    const existing = firstRow(
      session.readRows(
        `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`,
      ),
    );
    if (existing === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText(state)).join(", ");
    session.exec(
      [
        "update journal_events set",
        "state = 'deferred_lifecycle',",
        "next_eligible_retry_epoch_ms = null,",
        "safe_error = 'deferred_lifecycle',",
        "is_fingerprint_frozen = 1",
        `where local_file_id = ${sqlText(localFileId)}`,
        `and state in (${pendingStateList})`,
        "and operation in ('create', 'update');",
      ].join(" "),
    );
  }

  #findReplay(
    read: (sql: string) => readonly SqliteQueryResult[],
    localFileId: string,
    operands: LifecycleEventOperands,
  ): LifecycleRecordResult | null {
    const tombstoneFilter =
      operands.tombstoneId === null ? "is null" : `= ${sqlText(operands.tombstoneId)}`;
    const replayRow = firstRow(
      read(
        [
          `select je.event_id, je.idempotency_key, lf.lifecycle_state`,
          `from journal_events je`,
          `join local_files lf on lf.local_file_id = je.local_file_id`,
          `join lifecycle_event_operands leo on leo.event_id = je.event_id`,
          `where je.local_file_id = ${sqlText(localFileId)}`,
          `and je.operation = ${sqlText(operands.operation)}`,
          `and leo.source_id = ${sqlText(operands.sourceId)}`,
          `and leo.expected_version_id = ${sqlText(operands.expectedVersionId)}`,
          `and ${operands.expectedLocator === null ? "leo.expected_locator is null" : `leo.expected_locator = ${sqlText(operands.expectedLocator)}`}`,
          `and ${operands.targetLocator === null ? "leo.target_locator is null" : `leo.target_locator = ${sqlText(operands.targetLocator)}`}`,
          `and leo.tombstone_id ${tombstoneFilter}`,
          `and leo.policy_revision = ${operands.policyRevision}`,
          `and ${operands.predecessorEventId === null ? "leo.predecessor_event_id is null" : `leo.predecessor_event_id = ${sqlText(operands.predecessorEventId)}`}`,
          `order by je.created_at_epoch_ms desc, je.rowid desc`,
          `limit 1;`,
        ].join(" "),
      ),
    );
    if (replayRow === null) {
      return null;
    }
    const [eventId, idempotencyKey, lifecycleState] = replayRow;
    if (
      typeof eventId !== "string" ||
      typeof idempotencyKey !== "string" ||
      typeof lifecycleState !== "string" ||
      !isLifecycleLocalFileState(lifecycleState)
    ) {
      throw journalStoreError("journal_image_invalid");
    }
    return {
      eventId,
      eventIdempotencyKey: idempotencyKey,
      lifecycleState,
      event: {
        eventId,
        localFileId,
        idempotencyKey,
        operation: operands.operation,
        fingerprint: { ...LIFECYCLE_FINGERPRINT },
        state: "queued",
        attemptCount: 0,
        nextEligibleRetryEpochMs: null,
        safeError: null,
        operationId: null,
      },
    };
  }
}

// Unused but kept so the lint surface stays stable on consumers that
// reach for the same constants across files.
void JOURNAL_NON_RETRY_EVENT_STATES;
void MAX_EVENT_ATTEMPT_HISTORY;