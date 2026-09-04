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

import type {
  FrozenFingerprint,
  JournalEvent,
  JournalEventState,
  JournalOperation,
  JournalSafeErrorLabel,
} from "./contracts";
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
import { isFrozenFingerprintShape } from "./fingerprint";
import {
  isLifecycleJournalOperation,
  isLifecycleLocalFileState,
  type LifecycleEventOperands,
  type LifecycleLocalFileState,
  type RestoreReservationResult,
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

/**
 * The closed server receipt of one committed lifecycle event. The
 * `tombstoneId` is the server-computed identity of the tombstone
 * row a committed `delete` allocates; it is non-null only for the
 * operations that allocate or reference a tombstone. The driver
 * forwards this receipt to {@link LifecycleRepository.recordLifecycleCommittedReceipt}
 * so the durable row carries exactly the wire-confirmed values
 * (task 9 fix round 1 I1).
 */
export interface LifecycleServerReceipt {
  readonly tombstoneId: string | null;
}

/** A committed prefix may leave one rebased durable rename successor owed. */
export interface LifecycleCommittedReceiptResolution {
  readonly pendingRenameIntentLocalFileId: string | null;
}

export interface LifecycleRepositoryOptions {
  readonly database: LifecycleRepositoryDatabase;
  /** Identity mint; defaults to the platform `crypto.randomUUID`. */
  readonly createId?: () => string;
  /** Clock for event creation timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
}

/** The one durable, composable rename chain owned by a stable local row. */
export interface PendingRenameIntent {
  readonly localFileId: string;
  readonly priorPath: string;
  readonly currentPath: string;
}

/** Closed outcomes of one watcher-ingress intent mutation. */
export type PendingRenameIntentMutationOutcome =
  | "created"
  | "unchanged"
  | "composed"
  | "cancelled"
  | "compensation_pending";

/** The observed edge carried into the serialized intent writer. */
export interface PendingRenameIntentMutationInput {
  readonly localFileId: string;
  readonly observedPriorPath: string;
  readonly observedCurrentPath: string;
}

/** The direct lifecycle terminalization outcome after its full owner transaction. */
export type IntentAwareLifecycleTerminalResolution = "no_intent" | "intent_reconciled";

/**
 * The durable writer committed a safe reconciliation result, but the
 * observed edge could not be composed into the owner's chain. This error is
 * raised only after that reconciliation transaction commits.
 */
export class PendingRenameIntentConflictError extends Error {
  readonly reason = "pending_rename_intent_conflict" as const;

  constructor() {
    super("pending rename intent conflict");
    this.name = "PendingRenameIntentConflictError";
  }
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

function validateNormalizedPath(normalizedPath: string): void {
  if (
    typeof normalizedPath !== "string" ||
    normalizedPath.length === 0 ||
    normalizedPath.normalize("NFC") !== normalizedPath ||
    normalizedPath.includes("\\") ||
    normalizedPath.startsWith("/") ||
    normalizedPath.endsWith("/")
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  for (const character of normalizedPath) {
    const codeUnit = character.charCodeAt(0);
    if (codeUnit < 0x20 || codeUnit === 0x7f) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  const segments = normalizedPath.split("/");
  if (segments[0]?.includes(":")) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    throw journalStoreError("journal_mutation_failed");
  }
}

function parsePendingRenameIntentRow(
  row: readonly unknown[],
): PendingRenameIntent {
  const [localFileId, priorPath, currentPath] = row;
  if (
    typeof localFileId !== "string" ||
    !isUuid(localFileId) ||
    typeof priorPath !== "string" ||
    priorPath.length === 0 ||
    typeof currentPath !== "string" ||
    currentPath.length === 0
  ) {
    throw journalStoreError("journal_image_invalid");
  }
  return { localFileId, priorPath, currentPath };
}

function parentPath(normalizedPath: string): string {
  const separatorIndex = normalizedPath.lastIndexOf("/");
  return separatorIndex < 0 ? "" : normalizedPath.slice(0, separatorIndex);
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
      // The record-time state stays pending: the tombstone closes and the
      // state advances to `restored` only through the committed receipt
      // (the driver path), never at record time.
      return "restore_pending";
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

  /** Read the durable rename chain owned by one stable local-file identity. */
  readPendingRenameIntentForLocalFile(
    localFileId: string,
  ): PendingRenameIntent | null {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          "select local_file_id, prior_path, current_path",
          "from pending_rename_intents",
          `where local_file_id = ${sqlText(localFileId)};`,
        ].join(" "),
      ),
    );
    return row === null ? null : parsePendingRenameIntentRow(row);
  }

  /** Read the unique durable owner of one latest observed Vault path. */
  readPendingRenameIntentByCurrentPath(
    currentPath: string,
  ): PendingRenameIntent | null {
    validateNormalizedPath(currentPath);
    const row = firstRow(
      this.#database.readAll(
        [
          "select local_file_id, prior_path, current_path",
          "from pending_rename_intents",
          `where current_path = ${sqlText(currentPath)};`,
        ].join(" "),
      ),
    );
    return row === null ? null : parsePendingRenameIntentRow(row);
  }

  /**
   * Read the intent reserving either endpoint. Current-path ownership is
   * checked first because it is the authoritative watcher-ingress edge.
   */
  readPendingRenameIntentOwningEndpoint(
    normalizedPath: string,
  ): PendingRenameIntent | null {
    validateNormalizedPath(normalizedPath);
    const rows = this.#database.readAll(
      [
        "select local_file_id, prior_path, current_path",
        "from pending_rename_intents",
        `where current_path = ${sqlText(normalizedPath)}`,
        `or prior_path = ${sqlText(normalizedPath)}`,
        "order by case when current_path =",
        `${sqlText(normalizedPath)} then 0 else 1 end, rowid asc limit 1;`,
      ].join(" "),
    );
    const row = firstRow(rows);
    return row === null ? null : parsePendingRenameIntentRow(row);
  }

  /** Enumerate the bounded durable intent set for restart re-arming. */
  readPendingRenameIntents(): readonly PendingRenameIntent[] {
    const result = this.#database.readAll(
      [
        "select local_file_id, prior_path, current_path",
        "from pending_rename_intents order by rowid asc;",
      ].join(" "),
    );
    return (result[0]?.values ?? []).map(parsePendingRenameIntentRow);
  }

  /**
   * Create or compose one observed rename edge under the stable local row.
   * The row path remains the canonical prior until an immutable lifecycle
   * prefix is materialized. An incompatible edge or reserved target commits
   * row-level reconciliation and only then raises the closed conflict token.
   */
  async recordOrComposePendingRenameIntent(
    input: PendingRenameIntentMutationInput,
  ): Promise<PendingRenameIntentMutationOutcome> {
    if (!isUuid(input.localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    validateNormalizedPath(input.observedPriorPath);
    validateNormalizedPath(input.observedCurrentPath);
    if (input.observedPriorPath === input.observedCurrentPath) {
      throw journalStoreError("journal_mutation_failed");
    }

    const outcome = await this.#database.runSerializedMutation((session) => {
      const localRow = firstRow(
        session.readRows(
          [
            "select normalized_path, lifecycle_state from local_files",
            `where local_file_id = ${sqlText(input.localFileId)};`,
          ].join(" "),
        ),
      );
      if (localRow === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [rowPath, lifecycleState] = localRow;
      if (typeof rowPath !== "string" || typeof lifecycleState !== "string") {
        throw journalStoreError("journal_image_invalid");
      }
      if (lifecycleState === "restore_pending") {
        throw journalStoreError("journal_mutation_failed");
      }

      const storedRow = firstRow(
        session.readRows(
          [
            "select local_file_id, prior_path, current_path",
            "from pending_rename_intents",
            `where local_file_id = ${sqlText(input.localFileId)};`,
          ].join(" "),
        ),
      );
      const stored = storedRow === null ? null : parsePendingRenameIntentRow(storedRow);

      const targetOwner = firstRow(
        session.readRows(
          [
            "select local_file_id from local_files",
            `where normalized_path = ${sqlText(input.observedCurrentPath)}`,
            `and local_file_id <> ${sqlText(input.localFileId)} limit 1;`,
          ].join(" "),
        ),
      );
      const targetIntentOwner = firstRow(
        session.readRows(
          [
            "select local_file_id from pending_rename_intents",
            `where current_path = ${sqlText(input.observedCurrentPath)}`,
            `and local_file_id <> ${sqlText(input.localFileId)} limit 1;`,
          ].join(" "),
        ),
      );
      const isTargetReserved = targetOwner !== null || targetIntentOwner !== null;

      if (stored === null) {
        if (rowPath !== input.observedPriorPath || isTargetReserved) {
          this.#reconcilePendingRenameIntentInSession(
            session,
            input.localFileId,
            rowPath,
          );
          return "conflict" as const;
        }
        session.exec(
          [
            "insert into pending_rename_intents",
            "(local_file_id, prior_path, current_path) values (",
            `${sqlText(input.localFileId)},`,
            `${sqlText(input.observedPriorPath)},`,
            `${sqlText(input.observedCurrentPath)});`,
          ].join(" "),
        );
        return "created" as const;
      }

      if (
        stored.priorPath === input.observedPriorPath &&
        stored.currentPath === input.observedCurrentPath
      ) {
        return "unchanged" as const;
      }
      if (stored.currentPath !== input.observedPriorPath || isTargetReserved) {
        this.#reconcilePendingRenameIntentInSession(
          session,
          input.localFileId,
          stored.currentPath,
        );
        return "conflict" as const;
      }

      if (input.observedCurrentPath === stored.priorPath) {
        const openPrefixCount = this.#readOpenRenamePrefixCountInSession(
          session,
          input.localFileId,
        );
        if (openPrefixCount === 0) {
          session.exec(
            `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(input.localFileId)};`,
          );
          session.exec(
            `delete from pending_rename_intents where local_file_id = ${sqlText(input.localFileId)};`,
          );
          return "cancelled" as const;
        }
        if (openPrefixCount !== 1) {
          this.#reconcilePendingRenameIntentInSession(
            session,
            input.localFileId,
            stored.currentPath,
          );
          return "conflict" as const;
        }
        session.exec(
          [
            "update pending_rename_intents set",
            `current_path = ${sqlText(stored.priorPath)}`,
            `where local_file_id = ${sqlText(input.localFileId)};`,
          ].join(" "),
        );
        return "compensation_pending" as const;
      }

      session.exec(
        [
          "update pending_rename_intents set",
          `current_path = ${sqlText(input.observedCurrentPath)}`,
          `where local_file_id = ${sqlText(input.localFileId)};`,
        ].join(" "),
      );
      return "composed" as const;
    });

    if (outcome === "conflict") {
      throw new PendingRenameIntentConflictError();
    }
    return outcome;
  }

  /**
   * Materialize the latest durable endpoints as one immutable lifecycle
   * prefix, freezing content and rebinding the local row in the same writer.
   * A row without committed source/base identity remains parked as an intent.
   */
  async recordPendingRenameLifecycleEvent(
    localFileId: string,
    capturedFingerprint: FrozenFingerprint,
  ): Promise<LifecycleRecordResult | null> {
    if (!isUuid(localFileId) || !isFrozenFingerprintShape(capturedFingerprint)) {
      throw journalStoreError("journal_mutation_failed");
    }
    const result = await this.#database.runSerializedMutation((session) => {
      const intentRow = firstRow(
        session.readRows(
          [
            "select local_file_id, prior_path, current_path",
            "from pending_rename_intents",
            `where local_file_id = ${sqlText(localFileId)};`,
          ].join(" "),
        ),
      );
      if (intentRow === null) {
        return { kind: "result", value: null } as const;
      }
      const intent = parsePendingRenameIntentRow(intentRow);
      const openPrefix = this.#readOpenRenamePrefixInSession(session, localFileId);
      if (openPrefix !== null) {
        if (openPrefix.operands.expectedLocator !== intent.priorPath) {
          this.#reconcilePendingRenameIntentInSession(
            session,
            localFileId,
            intent.currentPath,
          );
          return { kind: "conflict" } as const;
        }
        return { kind: "result", value: openPrefix.result } as const;
      }
      if (intent.priorPath === intent.currentPath) {
        this.#reconcilePendingRenameIntentInSession(
          session,
          localFileId,
          intent.currentPath,
        );
        return { kind: "conflict" } as const;
      }

      const localRow = firstRow(
        session.readRows(
          [
            "select normalized_path, source_id, observed_sha256, observed_size_bytes,",
            "observed_media_type, base_version_id, policy_revision, lifecycle_state,",
            "last_committed_sha256, last_committed_size_bytes, last_committed_media_type",
            "from local_files",
            `where local_file_id = ${sqlText(localFileId)};`,
          ].join(" "),
        ),
      );
      if (localRow === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [
        normalizedPath,
        sourceId,
        observedSha256,
        observedSizeBytes,
        observedMediaType,
        baseVersionId,
        policyRevision,
        lifecycleState,
        committedSha256,
        committedSizeBytes,
        committedMediaType,
      ] = localRow;
      if (lifecycleState === "restore_pending") {
        throw journalStoreError("journal_mutation_failed");
      }
      if (sourceId === null || baseVersionId === null) {
        return { kind: "result", value: null } as const;
      }
      if (
        typeof normalizedPath !== "string" ||
        typeof sourceId !== "string" ||
        typeof observedSha256 !== "string" ||
        typeof observedSizeBytes !== "number" ||
        typeof observedMediaType !== "string" ||
        typeof baseVersionId !== "string" ||
        typeof policyRevision !== "number" ||
        !Number.isInteger(policyRevision) ||
        policyRevision < 1
      ) {
        throw journalStoreError("journal_image_invalid");
      }
      const lastCommittedFingerprint =
        committedSha256 === null && committedSizeBytes === null && committedMediaType === null
          ? null
          : typeof committedSha256 === "string" &&
              typeof committedSizeBytes === "number" &&
              typeof committedMediaType === "string"
            ? {
                sha256: committedSha256,
                sizeBytes: committedSizeBytes,
                mediaType: committedMediaType,
              }
            : null;
      if (
        lastCommittedFingerprint === null &&
        (committedSha256 !== null || committedSizeBytes !== null || committedMediaType !== null)
      ) {
        throw journalStoreError("journal_image_invalid");
      }
      const localFile: LocalFile = {
        localFileId,
        normalizedPath,
        sourceId,
        observedFingerprint: {
          sha256: observedSha256,
          sizeBytes: observedSizeBytes,
          mediaType: observedMediaType,
        },
        baseVersionId,
        policyRevisionNumber: policyRevision,
        lastCommittedFingerprint,
      };
      const operation =
        parentPath(intent.priorPath) === parentPath(intent.currentPath)
          ? "rename"
          : "move";
      const operands: LifecycleEventOperands = {
        operation,
        sourceId,
        expectedVersionId: baseVersionId,
        expectedLocator: intent.priorPath,
        targetLocator: intent.currentPath,
        tombstoneId: null,
        policyRevision,
        predecessorEventId: null,
        capturedFingerprintSha256: capturedFingerprint.sha256,
        capturedFingerprintSizeBytes: capturedFingerprint.sizeBytes,
        capturedFingerprintMediaType: capturedFingerprint.mediaType,
      };

      this.#freezePendingForLocalFileInSession(session, localFileId);
      session.exec(
        [
          "delete from multipart_upload_progress where event_id in (",
          "select event_id from journal_events",
          `where local_file_id = ${sqlText(localFileId)}`,
          "and state = 'deferred_lifecycle');",
        ].join(" "),
      );
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`,
      );
      const value = this.#recordLifecycleEventInSession(session, {
        operands,
        localFile,
        tombstoneId: null,
        newPath: intent.currentPath,
        forceFailureAfterExec: false,
      });
      return { kind: "result", value } as const;
    });
    if (result.kind === "conflict") {
      throw new PendingRenameIntentConflictError();
    }
    return result.value;
  }

  /**
   * Atomically reparent the owner to its latest durable intent endpoint and
   * release the intent/counter reservation. Row-specific reconciliation owns
   * locator truth after this exit, so no stale endpoint may block admission.
   */
  async reparentAndClearPendingRenameIntent(localFileId: string): Promise<void> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      this.#reparentAndClearPendingRenameIntentInSession(session, localFileId);
    });
  }

  /**
   * Consume an exact echo marker and release its pending rename reservation
   * in the same writer. If the owner reparent cannot commit, the marker
   * deletion rolls back with it.
   */
  async consumePendingRenameEchoAndReparent(
    localFileId: string,
    consumeEchoInSession: (session: SqliteMutationSession) => boolean,
  ): Promise<boolean> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    return this.#database.runSerializedMutation((session) => {
      if (!consumeEchoInSession(session)) {
        return false;
      }
      this.#reparentAndClearPendingRenameIntentInSession(session, localFileId);
      return true;
    });
  }

  /**
   * The only direct terminal exit for a pending rename/move prefix. It keeps
   * attempt audit, event close, missing-file counter cleanup, owner reparent
   * and reconciliation transfer inside the same SQLite mutation.
   */
  async resolveIntentAwareLifecycleTerminal(input: {
    readonly eventId: string;
    readonly terminalState: "blocked_conflict" | "integrity_failed";
    readonly attemptedAtEpochMs: number;
    readonly requestCorrelationId: string;
  }): Promise<IntentAwareLifecycleTerminalResolution> {
    if (
      !isUuid(input.eventId) ||
      !isPositiveInteger(input.attemptedAtEpochMs) ||
      typeof input.requestCorrelationId !== "string" ||
      input.requestCorrelationId.length === 0 ||
      input.requestCorrelationId.length > 128
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    for (const character of input.requestCorrelationId) {
      const codeUnit = character.charCodeAt(0);
      if (codeUnit < 0x20 || codeUnit > 0x7e) {
        throw journalStoreError("journal_mutation_failed");
      }
    }
    return this.#database.runSerializedMutation((session) => {
      const row = firstRow(
        session.readRows(
          [
            "select je.event_id, je.local_file_id, je.idempotency_key, je.operation,",
            "je.sha256, je.size_bytes, je.media_type, je.state, je.attempt_count,",
            "je.next_eligible_retry_epoch_ms, je.safe_error, je.operation_id",
            "from journal_events je join lifecycle_event_operands leo",
            "on leo.event_id = je.event_id",
            `where je.event_id = ${sqlText(input.eventId)};`,
          ].join(" "),
        ),
      );
      if (row === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const event = parseStoredEventRow(row);
      if (
        (event.operation !== "rename" && event.operation !== "move") ||
        !JOURNAL_PENDING_EVENT_STATES.includes(event.state as never)
      ) {
        throw journalStoreError("journal_mutation_failed");
      }
      this.#recordLifecycleAttemptInSession(session, {
        eventId: event.eventId,
        attemptedAtEpochMs: input.attemptedAtEpochMs,
        outcomeLabel: input.terminalState,
        requestCorrelationId: input.requestCorrelationId,
      });
      session.exec(
        [
          "update journal_events set",
          `state = ${sqlText(input.terminalState)},`,
          "next_eligible_retry_epoch_ms = null,",
          `safe_error = ${sqlText(input.terminalState)},`,
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText(event.eventId)};`,
        ].join(" "),
      );
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText(event.eventId)};`,
      );
      const intent = firstRow(
        session.readRows(
          [
            "select current_path from pending_rename_intents",
            `where local_file_id = ${sqlText(event.localFileId)};`,
          ].join(" "),
        ),
      );
      if (intent === null) {
        return "no_intent" as const;
      }
      const currentPath = intent[0];
      if (typeof currentPath !== "string" || currentPath.length === 0) {
        throw journalStoreError("journal_image_invalid");
      }
      this.#reconcilePendingRenameIntentInSession(session, event.localFileId, currentPath);
      return "intent_reconciled" as const;
    });
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
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(input.localFile.localFileId)};`,
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
   * Reserve one explicit-restore target locator (the reservation-first
   * protocol of the explicit-restore target reservation spec): in ONE
   * transaction the tombstoned row is rebound to the target path and
   * enters `restore_pending`, with the pre-reservation path retained in
   * `restore_prior_path` for an explicit cancel and for the committed
   * receipt's cleanup.
   *
   * Target availability is the precondition the upstream server contract
   * demands ("an explicitly requested, available target locator"):
   *   - another row WITH a source identity at the target → refused
   *     `restore_target_occupied` (a converged fresh source or a genuine
   *     other note; both rows stay untouched);
   *   - a phantom row (no source identity) whose content events are all
   *     unsent (`queued` / `waiting_retry`) → the phantom mapping and its
   *     never-shipped events are released inside this same transaction
   *     (the `removeLocalMapping` cleanup shape) and the reservation
   *     proceeds — staged restore bytes must never converge as a fresh
   *     source;
   *   - a phantom row with any event in `preflight` / `uploading` →
   *     refused `restore_target_busy` (retry after the pass settles).
   *
   * A non-terminal `restore` event of the row refuses the reservation as
   * `restore_already_pending`. Re-reservation from `restore_pending`
   * preserves the original `restore_prior_path` (never chained). A row
   * without an open tombstone, source identity or stored predecessor
   * delete event fails closed as `journal_mutation_failed`.
   */
  async reserveRestoreTarget(
    localFileId: string,
    targetPath: string,
  ): Promise<RestoreReservationResult> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (typeof targetPath !== "string" || targetPath.length === 0) {
      throw journalStoreError("journal_mutation_failed");
    }
    return this.#database.runSerializedMutation((session) => {
      const row = firstRow(
        session.readRows(
          [
            "select lifecycle_state, open_tombstone_id, source_id, base_version_id,",
            "normalized_path, restore_prior_path from local_files",
            `where local_file_id = ${sqlText(localFileId)};`,
          ].join(" "),
        ),
      );
      if (row === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [lifecycleState, openTombstoneId, sourceId, baseVersionId, currentPath, existingPriorPath] =
        row;
      if (
        typeof lifecycleState !== "string" ||
        typeof currentPath !== "string" ||
        sourceId === null ||
        baseVersionId === null ||
        typeof sourceId !== "string" ||
        typeof baseVersionId !== "string" ||
        typeof openTombstoneId !== "string" ||
        openTombstoneId.length === 0
      ) {
        throw journalStoreError("journal_mutation_failed");
      }
      if (lifecycleState !== "tombstoned" && lifecycleState !== "restore_pending") {
        throw journalStoreError("journal_mutation_failed");
      }
      const predecessor = firstRow(
        session.readRows(
          [
            "select event_id from journal_events",
            `where local_file_id = ${sqlText(localFileId)}`,
            "and operation = 'delete' limit 1;",
          ].join(" "),
        ),
      );
      if (predecessor === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const inFlightRestore = firstRow(
        session.readRows(
          [
            "select journal_events.event_id from journal_events",
            "join lifecycle_event_operands",
            "on lifecycle_event_operands.event_id = journal_events.event_id",
            `where journal_events.local_file_id = ${sqlText(localFileId)}`,
            "and journal_events.operation = 'restore'",
            "and journal_events.state in ('queued', 'preflight', 'uploading', 'waiting_retry')",
            "limit 1;",
          ].join(" "),
        ),
      );
      if (inFlightRestore !== null) {
        return { outcome: "refused", reason: "restore_already_pending" };
      }
      const reservedIntentEndpoint = firstRow(
        session.readRows(
          [
            "select local_file_id from pending_rename_intents",
            `where prior_path = ${sqlText(targetPath)}`,
            `or current_path = ${sqlText(targetPath)}`,
            "limit 1;",
          ].join(" "),
        ),
      );
      if (reservedIntentEndpoint !== null) {
        return { outcome: "refused", reason: "restore_target_busy" };
      }
      const occupant = firstRow(
        session.readRows(
          [
            "select local_file_id, source_id from local_files",
            `where normalized_path = ${sqlText(targetPath)};`,
          ].join(" "),
        ),
      );
      if (occupant !== null && occupant[0] !== localFileId) {
        if (typeof occupant[1] === "string" && occupant[1].length > 0) {
          return { outcome: "refused", reason: "restore_target_occupied" };
        }
        const occupantId = String(occupant[0]);
        const inFlightUpload = firstRow(
          session.readRows(
            [
              "select event_id from journal_events",
              `where local_file_id = ${sqlText(occupantId)}`,
              "and state in ('preflight', 'uploading') limit 1;",
            ].join(" "),
          ),
        );
        if (inFlightUpload !== null) {
          return { outcome: "refused", reason: "restore_target_busy" };
        }
        // Release the phantom mapping and its never-shipped events in this
        // same transaction (the `removeLocalMapping` cleanup shape).
        session.exec(
          `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(occupantId)};`,
        );
        session.exec(
          `delete from pending_rename_intents where local_file_id = ${sqlText(occupantId)};`,
        );
        session.exec(
          `delete from journal_attempts where event_id in (select event_id from journal_events where local_file_id = ${sqlText(occupantId)});`,
        );
        session.exec(
          `delete from multipart_upload_progress where event_id in (select event_id from journal_events where local_file_id = ${sqlText(occupantId)});`,
        );
        session.exec(
          `delete from lifecycle_event_operands where event_id in (select event_id from journal_events where local_file_id = ${sqlText(occupantId)});`,
        );
        session.exec(
          `delete from journal_events where local_file_id = ${sqlText(occupantId)};`,
        );
        session.exec(
          `delete from local_files where local_file_id = ${sqlText(occupantId)};`,
        );
      }
      const priorPath =
        lifecycleState === "tombstoned" ||
        typeof existingPriorPath !== "string" ||
        existingPriorPath.length === 0
          ? currentPath
          : existingPriorPath;
      session.exec(
        [
          "update local_files set",
          `normalized_path = ${sqlText(targetPath)},`,
          "lifecycle_state = 'restore_pending',",
          `restore_prior_path = ${sqlText(priorPath)}`,
          `where local_file_id = ${sqlText(localFileId)};`,
        ].join(" "),
      );
      return { outcome: "reserved", priorNormalizedPath: priorPath };
    });
  }

  /**
   * Release one explicit-restore reservation (the explicit Cancel path of
   * the restore command): in ONE transaction the row returns to its
   * pre-reservation path, the state returns to `tombstoned` (the open
   * tombstone was retained through the reservation) and
   * `restore_prior_path` clears. A row that is not `restore_pending`, or
   * whose prior path was not retained, fails closed as
   * `journal_mutation_failed`.
   */
  async releaseRestoreTarget(localFileId: string): Promise<void> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const row = firstRow(
        session.readRows(
          [
            "select lifecycle_state, restore_prior_path from local_files",
            `where local_file_id = ${sqlText(localFileId)};`,
          ].join(" "),
        ),
      );
      if (row === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [lifecycleState, priorPath] = row;
      if (
        lifecycleState !== "restore_pending" ||
        typeof priorPath !== "string" ||
        priorPath.length === 0
      ) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update local_files set",
          `normalized_path = ${sqlText(priorPath)},`,
          "lifecycle_state = 'tombstoned',",
          "restore_prior_path = null",
          `where local_file_id = ${sqlText(localFileId)};`,
        ].join(" "),
      );
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
          [
            "select lf.normalized_path, pri.current_path from local_files lf",
            "left join pending_rename_intents pri",
            "on pri.local_file_id = lf.local_file_id",
            `where lf.local_file_id = ${sqlText(localFileId)};`,
          ].join(" "),
        ),
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [rowPath, intentCurrentPath] = existing;
      if (
        typeof rowPath !== "string" ||
        (intentCurrentPath !== null && typeof intentCurrentPath !== "string")
      ) {
        throw journalStoreError("journal_image_invalid");
      }
      this.#reconcilePendingRenameIntentInSession(
        session,
        localFileId,
        intentCurrentPath ?? rowPath,
      );
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
   * exact-replay rule; task 9 fix round 1 I1).
   *
   * The last_committed_* columns are intentionally left untouched:
   * a rename / move / delete / restore does not change file bytes,
   * so the prior `last_committed_*` triple stays provable for the
   * next restore-eligibility check.
   *
   * The nullable `serverReceipt` argument carries the server-
   * returned tombstone id when the operation is `delete` (or
   * `restore` reporting a server-derived tombstone identity). When
   * the receipt is omitted or its `tombstoneId` is null, the existing
   * `lifecycle_event_operands.tombstone_id` column is preserved (a
   * non-`delete` commit never overwrites the tombstone identity).
   */
  async recordLifecycleCommittedReceipt(
    eventId: string,
    serverReceipt: LifecycleServerReceipt | null = null,
  ): Promise<LifecycleCommittedReceiptResolution> {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (serverReceipt !== null) {
      const tombstoneId = serverReceipt.tombstoneId;
      if (tombstoneId !== null && !isUuid(tombstoneId)) {
        throw journalStoreError("journal_mutation_failed");
      }
    }
    return this.#database.runSerializedMutation((session) => {
      const event = firstRow(
        session.readRows(
          [
            "select je.event_id, je.local_file_id, je.operation, leo.target_locator",
            "from journal_events je join lifecycle_event_operands leo",
            "on leo.event_id = je.event_id",
            `where je.event_id = ${sqlText(eventId)};`,
          ].join(" "),
        ),
      );
      if (event === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [storedEventId, localFileId, operation, targetLocator] = event;
      if (
        typeof storedEventId !== "string" ||
        typeof localFileId !== "string" ||
        typeof operation !== "string"
      ) {
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
      let pendingRenameIntentLocalFileId: string | null = null;
      session.exec(
        [
          "update journal_events set state = 'committed',",
          "next_eligible_retry_epoch_ms = null,",
          "safe_error = null",
          `where event_id = ${sqlText(eventId)};`,
        ].join(" "),
      );
      // Persist the server-receipt tombstone id when the server
      // returned one. A delete receipt writes the server-computed
      // tombstone id (the server is the only authority over the
      // tombstone domain — spec 19.2 task 9 fix round 1 I1); a
      // restore receipt writes the same id the predecessor delete
      // returned so the durable row is exactly what the server
      // acknowledged across the pair.
      if (serverReceipt !== null && serverReceipt.tombstoneId !== null) {
        session.exec(
          [
            "update lifecycle_event_operands set",
            `server_receipt_tombstone_id = ${sqlText(serverReceipt.tombstoneId)}`,
            `where event_id = ${sqlText(eventId)};`,
          ].join(" "),
        );
      }
      // Lifecycle-state transitions for each closed operation.
      switch (operation) {
        case "rename":
        case "move":
          if (typeof targetLocator !== "string") {
            throw journalStoreError("journal_image_invalid");
          }
          session.exec(
            [
              "update local_files set",
              "lifecycle_state = 'active'",
              `where local_file_id = ${sqlText(localFileId)};`,
            ].join(" "),
          );
          // Fix round 2 D7: the rename/move committed server-side, so the
          // durable lifecycle-deferral marker — the terminal
          // `deferred_lifecycle` content rows the capture freeze created —
          // is released IN THIS TRANSACTION. Without the release the
          // capture guard (`any deferred_lifecycle row of the file`)
          // refuses the path forever and no automatic surface can ever
          // re-sync the note's content. The released rows' bounded attempt
          // audit goes with them (the same cleanup
          // `removeLocalMapping` applies). After the release, the next
          // snapshot/modify re-admits the path when its bytes diverge
          // from the last-committed fingerprint.
          session.exec(
            [
              "delete from journal_attempts where event_id in (",
              "select event_id from journal_events",
              `where local_file_id = ${sqlText(localFileId)}`,
              "and state = 'deferred_lifecycle');",
            ].join(" "),
          );
          session.exec(
            [
              "delete from journal_events",
              `where local_file_id = ${sqlText(localFileId)}`,
              "and state = 'deferred_lifecycle';",
            ].join(" "),
          );
          session.exec(
            `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`,
          );
          const pendingIntentRow = firstRow(
            session.readRows(
              [
                "select local_file_id, prior_path, current_path",
                "from pending_rename_intents",
                `where local_file_id = ${sqlText(localFileId)};`,
              ].join(" "),
            ),
          );
          if (pendingIntentRow !== null) {
            const pendingIntent = parsePendingRenameIntentRow(pendingIntentRow);
            if (pendingIntent.currentPath === targetLocator) {
              session.exec(
                `delete from pending_rename_intents where local_file_id = ${sqlText(localFileId)};`,
              );
            } else {
              session.exec(
                [
                  "update pending_rename_intents set",
                  `prior_path = ${sqlText(targetLocator)}`,
                  `where local_file_id = ${sqlText(localFileId)};`,
                ].join(" "),
              );
              pendingRenameIntentLocalFileId = localFileId;
            }
          }
          break;
        case "delete":
          // Tombstone row stays; the durable local_files row is
          // pruned by the capture path on tombstone commit.
          session.exec(
            `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`,
          );
          session.exec(
            `delete from pending_rename_intents where local_file_id = ${sqlText(localFileId)};`,
          );
          session.exec(
            [
              "update local_files set",
              "lifecycle_state = 'tombstoned'",
              `where local_file_id = ${sqlText(localFileId)};`,
            ].join(" "),
          );
          break;
        case "restore":
          // The committed receipt is the single durable moment the local
          // mapping may follow the restore to its target locator (the
          // upstream "a path is rebound only after the lifecycle result is
          // durable" rule): rebind to the event operands' target locator
          // and clear the reservation's prior-path memory in the SAME
          // transaction. Without the rebind the row would stay at the
          // prior path while the canonical locator belongs to the restored
          // source, and the next convergence of the staged bytes would
          // collide with it.
          session.exec(
            [
              "update local_files set",
              "normalized_path = (select target_locator from lifecycle_event_operands",
              `where event_id = ${sqlText(eventId)}),`,
              "lifecycle_state = 'restored',",
              "restore_prior_path = null",
              `where local_file_id = ${sqlText(localFileId)};`,
            ].join(" "),
          );
          break;
      }
      return { pendingRenameIntentLocalFileId };
    });
  }

  /**
   * Read the server-confirmed tombstone id of one committed lifecycle
   * event. The reader returns the `server_receipt_tombstone_id`
   * column the {@link recordLifecycleCommittedReceipt} mutator writes
   * on a successful `delete` commit. The restore driver uses this
   * read to override the operands-derived tombstone id on the wire
   * body so the server hears the same identity it returned on the
   * predecessor (task 9 fix round 1 I1).
   *
   * Returns `null` when the event has no persisted server receipt —
   * the predecessor's commit is still in flight, or the operation is
   * not `delete` / `restore`.
   */
  readServerReceiptTombstoneId(eventId: string): string | null {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          "select server_receipt_tombstone_id",
          `from lifecycle_event_operands where event_id = ${sqlText(eventId)};`,
        ].join(" "),
      ),
    );
    if (row === null) {
      return null;
    }
    const [stored] = row;
    if (stored === null) {
      return null;
    }
    if (typeof stored !== "string" || !isUuid(stored)) {
      throw journalStoreError("journal_image_invalid");
    }
    return stored;
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
          "select je.operation, leo.source_id, leo.expected_version_id, leo.expected_locator,",
          "leo.target_locator, leo.tombstone_id, leo.policy_revision, leo.predecessor_event_id",
          "from lifecycle_event_operands leo join journal_events je",
          "on je.event_id = leo.event_id",
          `where leo.event_id = ${sqlText(eventId)};`,
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

  #readOpenRenamePrefixCountInSession(
    session: SqliteMutationSession,
    localFileId: string,
  ): number {
    const row = firstRow(
      session.readRows(
        [
          "select count(*) from journal_events",
          `where local_file_id = ${sqlText(localFileId)}`,
          "and operation in ('rename', 'move')",
          "and state in ('queued', 'preflight', 'uploading', 'waiting_retry');",
        ].join(" "),
      ),
    );
    const count = row?.[0];
    if (typeof count !== "number" || !Number.isInteger(count) || count < 0) {
      throw journalStoreError("journal_image_invalid");
    }
    return count;
  }

  #readOpenRenamePrefixInSession(
    session: SqliteMutationSession,
    localFileId: string,
  ): {
    readonly result: LifecycleRecordResult;
    readonly operands: LifecycleEventOperands;
  } | null {
    const count = this.#readOpenRenamePrefixCountInSession(session, localFileId);
    if (count === 0) {
      return null;
    }
    if (count !== 1) {
      throw journalStoreError("journal_image_invalid");
    }
    const row = firstRow(
      session.readRows(
        [
          "select je.event_id, je.local_file_id, je.idempotency_key, je.operation,",
          "je.sha256, je.size_bytes, je.media_type, je.state, je.attempt_count,",
          "je.next_eligible_retry_epoch_ms, je.safe_error, je.operation_id,",
          "lf.lifecycle_state, leo.source_id, leo.expected_version_id,",
          "leo.expected_locator, leo.target_locator, leo.tombstone_id,",
          "leo.policy_revision, leo.predecessor_event_id",
          "from journal_events je",
          "join lifecycle_event_operands leo on leo.event_id = je.event_id",
          "join local_files lf on lf.local_file_id = je.local_file_id",
          `where je.local_file_id = ${sqlText(localFileId)}`,
          "and je.operation in ('rename', 'move')",
          "and je.state in ('queued', 'preflight', 'uploading', 'waiting_retry')",
          "order by je.created_at_epoch_ms asc, je.rowid asc limit 1;",
        ].join(" "),
      ),
    );
    if (row === null) {
      throw journalStoreError("journal_image_invalid");
    }
    const event = parseStoredEventRow(row.slice(0, 12));
    const lifecycleState = row[12];
    const operands = parseLifecycleOperandRow([
      row[3],
      row[13],
      row[14],
      row[15],
      row[16],
      row[17],
      row[18],
      row[19],
    ]);
    if (
      typeof lifecycleState !== "string" ||
      !isLifecycleLocalFileState(lifecycleState) ||
      event.operation !== operands.operation
    ) {
      throw journalStoreError("journal_image_invalid");
    }
    return {
      operands,
      result: {
        event,
        eventId: event.eventId,
        eventIdempotencyKey: event.idempotencyKey,
        lifecycleState,
      },
    };
  }

  #reconcilePendingRenameIntentInSession(
    session: SqliteMutationSession,
    localFileId: string,
    currentPath: string,
  ): void {
    session.exec(
      `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`,
    );
    session.exec(
      `delete from pending_rename_intents where local_file_id = ${sqlText(localFileId)};`,
    );
    session.exec(
      [
        "update local_files set",
        `normalized_path = ${sqlText(currentPath)},`,
        "lifecycle_state = 'reconcile_required',",
        "open_tombstone_id = null",
        `where local_file_id = ${sqlText(localFileId)};`,
      ].join(" "),
    );
    const meta = session.readJournalMeta();
    if (!meta.isReconcileRequired) {
      session.writeJournalMeta({ ...meta, isReconcileRequired: true });
    }
  }

  #reparentAndClearPendingRenameIntentInSession(
    session: SqliteMutationSession,
    localFileId: string,
  ): void {
    const row = firstRow(
      session.readRows(
        [
          "select lf.normalized_path, pri.current_path from local_files lf",
          "left join pending_rename_intents pri on pri.local_file_id = lf.local_file_id",
          `where lf.local_file_id = ${sqlText(localFileId)};`,
        ].join(" "),
      ),
    );
    if (row === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const [rowPath, currentPath] = row;
    if (
      typeof rowPath !== "string" ||
      (currentPath !== null && typeof currentPath !== "string")
    ) {
      throw journalStoreError("journal_image_invalid");
    }
    const finalPath = currentPath ?? rowPath;
    session.exec(
      `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`,
    );
    session.exec(
      `delete from pending_rename_intents where local_file_id = ${sqlText(localFileId)};`,
    );
    session.exec(
      [
        "update local_files set",
        `normalized_path = ${sqlText(finalPath)},`,
        "lifecycle_state = 'active',",
        "open_tombstone_id = null",
        `where local_file_id = ${sqlText(localFileId)};`,
      ].join(" "),
    );
  }

  #recordLifecycleAttemptInSession(
    session: SqliteMutationSession,
    input: {
      readonly eventId: string;
      readonly attemptedAtEpochMs: number;
      readonly outcomeLabel: JournalSafeErrorLabel;
      readonly requestCorrelationId: string;
    },
  ): void {
    session.exec(
      [
        "insert into journal_attempts (event_id, attempted_at_epoch_ms, outcome_label,",
        "request_correlation_id) values (",
        `${sqlText(input.eventId)}, ${input.attemptedAtEpochMs},`,
        `${sqlText(input.outcomeLabel)}, ${sqlText(input.requestCorrelationId)});`,
      ].join(" "),
    );
    session.exec(
      [
        "delete from journal_attempts where event_id =",
        `${sqlText(input.eventId)} and attempt_ordinal not in (`,
        "select attempt_ordinal from journal_attempts",
        `where event_id = ${sqlText(input.eventId)}`,
        `order by attempt_ordinal desc limit ${MAX_EVENT_ATTEMPT_HISTORY});`,
      ].join(" "),
    );
  }

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
