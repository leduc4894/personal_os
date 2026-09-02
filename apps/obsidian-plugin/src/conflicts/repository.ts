/**
 * The durable no-byte conflict repair repository (Child 8 spec 5.2.6/6,
 * Task 7).
 *
 * The repository owns the `conflict_local_repairs` table of journal
 * schema v9: one row per open repair, carrying ONLY the conflict UUID,
 * the resolution event identity, the closed target action, the closed
 * safe reason and the retry bookkeeping. A row is parked the moment a
 * canonical resolution commits (the pre-apply crash window of spec
 * 5.2.6), retried by bounded local application without ever issuing
 * another resolution, and deleted once the local apply completed. The
 * repair worker re-reads the conflict detail over the wire for the
 * winner identity, so no version id, path, locator, evidence bytes or
 * merge draft is ever durable here — the member set of every input type
 * makes byte storage unrepresentable, and the table's CHECK constraints
 * pin the closed vocabularies as defense in depth.
 *
 * Every mutation flows through the journal's single serialized writer
 * (the device-sync repository precedent): an exact re-park under the
 * same resolution event refreshes the bookkeeping idempotently, while a
 * re-park or completion under a FOREIGN resolution event contradicts
 * the durable evidence and refuses with the closed
 * `journal_mutation_failed` store reason — the canonical resolution
 * identity never forks locally.
 *
 * Privacy (spec 9): every persisted value is a closed token, an opaque
 * canonical UUID or a non-negative epoch; every thrown failure is the
 * closed store reason. No raw content, path, credential or provider
 * detail can reach a row, an error or a status surface.
 *
 * Like the journal modules this module imports no Node.js, Electron or
 * Obsidian file-system adapter API at module load time, so it stays
 * loadable on mobile.
 */

import { journalStoreError } from "../journal/sqlite-database";
import type { SqliteMutationSession, SqliteQueryResult } from "../journal/sqlite-database";
import {
  CONFLICT_LOCAL_REPAIR_ACTIONS,
  CONFLICT_LOCAL_REPAIR_SAFE_REASONS,
} from "./contracts";
import type {
  ConflictLocalRepairAction,
  ConflictLocalRepairSafeReason,
  PendingLocalApply,
} from "./contracts";

// --- the structural database slice ---------------------------------------------------------------

/**
 * The narrow database seam the conflict repository depends on (the
 * device-sync repository precedent): the serialized mutation queue plus
 * the read-only query seam. `SqliteDatabase` and the
 * persistence-composed journal slice both satisfy it, so the repository
 * composes into the same single writer — no parallel SQL surface.
 */
export interface ConflictRepositoryDatabase {
  runSerializedMutation<T>(
    operation: (session: SqliteMutationSession) => T | Promise<T>,
  ): Promise<T>;
  readAll(sql: string): SqliteQueryResult[];
}

export interface ConflictRepositoryOptions {
  readonly database: ConflictRepositoryDatabase;
}

// --- closed value validation ---------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isClosedToken<T extends string>(value: unknown, closedSet: readonly T[]): value is T {
  return typeof value === "string" && (closedSet as readonly string[]).includes(value);
}

/** Render one validated string as a SQL text literal (quotes doubled). */
function sqlText(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

function firstRow(result: readonly SqliteQueryResult[]): readonly unknown[] | null {
  return result[0]?.values[0] ?? null;
}

/** The ordered column list of the repair table (its single read shape). */
const CONFLICT_LOCAL_REPAIR_COLUMNS = [
  "conflict_id",
  "resolution_event_id",
  "target_action",
  "safe_reason",
  "attempt_count",
  "next_eligible_retry_epoch_ms",
  "created_at_epoch_ms",
  "updated_at_epoch_ms",
] as const;

// --- input shapes -----------------------------------------------------------------------------------

/** Park (or idempotently re-park) one pending local apply fact. */
export interface ParkPendingLocalApplyInput {
  readonly conflictId: string;
  readonly resolutionEventId: string;
  readonly targetAction: ConflictLocalRepairAction;
  readonly safeReason: ConflictLocalRepairSafeReason;
  readonly nowEpochMs: number;
}

/** Record one failed local-apply attempt with its next eligible retry moment. */
export interface RecordLocalApplyFailureInput {
  readonly conflictId: string;
  readonly resolutionEventId: string;
  readonly safeReason: ConflictLocalRepairSafeReason;
  readonly nowEpochMs: number;
  readonly nextEligibleRetryEpochMs: number;
}

/** Complete (delete) the pending local apply of the matching resolution event. */
export interface CompleteLocalApplyInput {
  readonly conflictId: string;
  readonly resolutionEventId: string;
}

// --- row decoding --------------------------------------------------------------------------------------

/** Parse one stored repair row back into the frozen contract shape. */
function parsePendingLocalApplyRow(row: readonly unknown[]): PendingLocalApply {
  const [
    conflictId,
    resolutionEventId,
    targetAction,
    safeReason,
    attemptCount,
    nextEligibleRetryEpochMs,
    createdAtEpochMs,
    updatedAtEpochMs,
  ] = row;
  if (
    !isUuid(conflictId) ||
    !isUuid(resolutionEventId) ||
    !isClosedToken(targetAction, CONFLICT_LOCAL_REPAIR_ACTIONS) ||
    !isClosedToken(safeReason, CONFLICT_LOCAL_REPAIR_SAFE_REASONS) ||
    !isNonNegativeInteger(attemptCount) ||
    !(nextEligibleRetryEpochMs === null || isNonNegativeInteger(nextEligibleRetryEpochMs)) ||
    !isNonNegativeInteger(createdAtEpochMs) ||
    !isNonNegativeInteger(updatedAtEpochMs)
  ) {
    // A row outside the closed shape is image corruption the store layer
    // owns; the repository never renders it.
    throw journalStoreError("journal_query_failed");
  }
  return {
    conflictId,
    resolutionEventId,
    targetAction,
    safeReason,
    attemptCount,
    nextEligibleRetryEpochMs,
    createdAtEpochMs,
    updatedAtEpochMs,
  };
}

// --- the repository -------------------------------------------------------------------------------------

/**
 * The durable no-byte conflict repair record store. Every mutation flows
 * through the single serialized writer, one transaction at a time.
 */
export class ConflictRepository {
  readonly #database: ConflictRepositoryDatabase;

  constructor(options: ConflictRepositoryOptions) {
    this.#database = options.database;
  }

  /** One pending local apply by its conflict identity, or null (read-only). */
  readPendingLocalApply(conflictId: string): PendingLocalApply | null {
    if (!isUuid(conflictId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          `select ${CONFLICT_LOCAL_REPAIR_COLUMNS.join(", ")} from conflict_local_repairs`,
          `where conflict_id = ${sqlText(conflictId)};`,
        ].join(" "),
      ),
    );
    return row === null ? null : parsePendingLocalApplyRow(row);
  }

  /** Every pending local apply, oldest first (read-only). */
  readPendingLocalApplies(): readonly PendingLocalApply[] {
    const rows = this.#database.readAll(
      [
        `select ${CONFLICT_LOCAL_REPAIR_COLUMNS.join(", ")} from conflict_local_repairs`,
        "order by created_at_epoch_ms asc, conflict_id asc;",
      ].join(" "),
    );
    return (rows[0]?.values ?? []).map((row) => parsePendingLocalApplyRow(row));
  }

  /**
   * Park one pending local apply fact (spec 5.2.6). An exact re-park
   * under the SAME resolution event identity refreshes the safe reason
   * and bookkeeping idempotently (the crash/retry replay); a re-park
   * under a FOREIGN resolution event contradicts the durable canonical
   * outcome and refuses — the resolution identity never forks locally.
   */
  async parkPendingLocalApply(input: ParkPendingLocalApplyInput): Promise<void> {
    validateParkInput(input);
    await this.#database.runSerializedMutation((session) => {
      const existing = this.#readRepairRow(session, input.conflictId);
      if (existing === null) {
        session.exec(
          [
            "insert into conflict_local_repairs (conflict_id, resolution_event_id,",
            "target_action, safe_reason, attempt_count, next_eligible_retry_epoch_ms,",
            "created_at_epoch_ms, updated_at_epoch_ms) values (",
            `${sqlText(input.conflictId)}, ${sqlText(input.resolutionEventId)},`,
            `${sqlText(input.targetAction)}, ${sqlText(input.safeReason)},`,
            "0, null,",
            `${input.nowEpochMs}, ${input.nowEpochMs});`,
          ].join(" "),
        );
        return;
      }
      if (existing.resolutionEventId !== input.resolutionEventId) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update conflict_local_repairs set",
          `target_action = ${sqlText(input.targetAction)},`,
          `safe_reason = ${sqlText(input.safeReason)},`,
          `updated_at_epoch_ms = ${input.nowEpochMs}`,
          `where conflict_id = ${sqlText(input.conflictId)};`,
        ].join(" "),
      );
    });
  }

  /**
   * Record one failed local-apply attempt: the attempt count grows, the
   * closed safe reason updates and the next eligible retry moment parks
   * the row. A missing row or a foreign resolution event refuses.
   */
  async recordLocalApplyFailure(input: RecordLocalApplyFailureInput): Promise<void> {
    if (
      !isUuid(input.conflictId) ||
      !isUuid(input.resolutionEventId) ||
      !isClosedToken(input.safeReason, CONFLICT_LOCAL_REPAIR_SAFE_REASONS) ||
      !isNonNegativeInteger(input.nowEpochMs) ||
      !isNonNegativeInteger(input.nextEligibleRetryEpochMs)
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = this.#readRepairRow(session, input.conflictId);
      if (existing === null || existing.resolutionEventId !== input.resolutionEventId) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update conflict_local_repairs set",
          `safe_reason = ${sqlText(input.safeReason)},`,
          `attempt_count = ${existing.attemptCount + 1},`,
          `next_eligible_retry_epoch_ms = ${input.nextEligibleRetryEpochMs},`,
          `updated_at_epoch_ms = ${input.nowEpochMs}`,
          `where conflict_id = ${sqlText(input.conflictId)};`,
        ].join(" "),
      );
    });
  }

  /**
   * Complete one pending local apply by deleting its row. Only the
   * matching resolution event may complete the parked fact: a foreign
   * identity refuses and keeps the owed work visible.
   */
  async completeLocalApply(input: CompleteLocalApplyInput): Promise<void> {
    if (!isUuid(input.conflictId) || !isUuid(input.resolutionEventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = this.#readRepairRow(session, input.conflictId);
      if (existing === null || existing.resolutionEventId !== input.resolutionEventId) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(`delete from conflict_local_repairs where conflict_id = ${sqlText(input.conflictId)};`);
    });
  }

  // --- internals --------------------------------------------------------------------------------------

  #readRepairRow(
    session: SqliteMutationSession,
    conflictId: string,
  ): PendingLocalApply | null {
    const row = firstRow(
      session.readRows(
        [
          `select ${CONFLICT_LOCAL_REPAIR_COLUMNS.join(", ")} from conflict_local_repairs`,
          `where conflict_id = ${sqlText(conflictId)};`,
        ].join(" "),
      ),
    );
    return row === null ? null : parsePendingLocalApplyRow(row);
  }
}

/** Validate the park input against the closed vocabularies before any SQL. */
function validateParkInput(input: ParkPendingLocalApplyInput): void {
  if (
    !isUuid(input.conflictId) ||
    !isUuid(input.resolutionEventId) ||
    !isClosedToken(input.targetAction, CONFLICT_LOCAL_REPAIR_ACTIONS) ||
    !isClosedToken(input.safeReason, CONFLICT_LOCAL_REPAIR_SAFE_REASONS) ||
    !isNonNegativeInteger(input.nowEpochMs)
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
}
