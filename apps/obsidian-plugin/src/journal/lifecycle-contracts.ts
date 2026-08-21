/**
 * Closed contracts of the Child 5 plugin-side lifecycle journal extension.
 *
 * This module freezes the four lifecycle `JournalOperation` tokens
 * (`rename`, `move`, `delete`, `restore`) added on top of the existing
 * `create` / `update` content tokens (Child 4), the closed
 * `LifecycleLocalFileState` enum of eight values, the
 * `LifecycleEventOperands` shape and the Child 4 → Child 5 schema
 * migration. Like `contracts.ts`, it deliberately contains no runtime
 * behavior — no SQLite instantiation and no I/O.
 *
 * Privacy (spec 9): `expected_locator`, `target_locator` and source IDs
 * are local-only retention for recovery; the status / attempt projection
 * surface must NEVER include these strings — only safe codes and counts.
 */

import { JOURNAL_SCHEMA_VERSION } from "./sqlite-database";
import {
  CHILD_FOUR_SCHEMA_VERSION,
  JournalStoreError,
  journalStoreError,
} from "./sqlite-database";
import type { SqliteDatabaseEngine, SqliteEngineModule } from "./sqlite-database";

// --- frozen schema version (spec 6.3) --------------------------------------------------------

/**
 * The lifecycle schema version this build writes and understands.
 * Mirrors {@link JOURNAL_SCHEMA_VERSION} but is named here so consumers
 * outside the journal layer do not need to reach into the database
 * module to compare versions.
 */
export const LIFECYCLE_SCHEMA_VERSION = JOURNAL_SCHEMA_VERSION;

// --- closed lifecycle operations --------------------------------------------------------------

/**
 * The four lifecycle operations of Child 5: closed additions on top of the
 * `create` / `update` content operations of Child 4. A lifecycle event is
 * always paired with a row in `lifecycle_event_operands` (the keyed
 * extension) and is the only kind of event whose `operation` is one of
 * the tokens below.
 */
export const LIFECYCLE_JOURNAL_OPERATIONS = [
  "rename",
  "move",
  "delete",
  "restore",
] as const;

export type LifecycleJournalOperation = (typeof LIFECYCLE_JOURNAL_OPERATIONS)[number];

/** The merged operation token set of one journal event (Child 4 + Child 5). */
export type JournalLifecycleOperation =
  | "create"
  | "update"
  | "rename"
  | "move"
  | "delete"
  | "restore";

export function isLifecycleJournalOperation(
  value: unknown,
): value is LifecycleJournalOperation {
  return (
    typeof value === "string" &&
    (LIFECYCLE_JOURNAL_OPERATIONS as readonly string[]).includes(value)
  );
}

// --- closed lifecycle local-file states -------------------------------------------------------

/**
 * The closed `lifecycle_state` enum of one `local_files` row:
 *   - `active` — content surface is the source of truth.
 *   - `rename_pending` / `move_pending` / `delete_pending` /
 *     `restore_pending` — a lifecycle event has been recorded but the
 *     server has not yet acknowledged the locator / tombstone change.
 *   - `tombstoned` — a delete has been committed on the server and the
 *     local mapping is retained for recovery / restore.
 *   - `restored` — a restore has been committed; the source is live again.
 *   - `reconcile_required` — dependency evidence for the row is corrupt
 *     or missing; reconciliation owns recovery.
 */
export const LIFECYCLE_LOCAL_FILE_STATES = [
  "active",
  "rename_pending",
  "move_pending",
  "delete_pending",
  "restore_pending",
  "tombstoned",
  "restored",
  "reconcile_required",
] as const;

export type LifecycleLocalFileState = (typeof LIFECYCLE_LOCAL_FILE_STATES)[number];

export function isLifecycleLocalFileState(value: unknown): value is LifecycleLocalFileState {
  return (
    typeof value === "string" &&
    (LIFECYCLE_LOCAL_FILE_STATES as readonly string[]).includes(value)
  );
}

// --- the keyed operand record (spec 6.3) -----------------------------------------------------

/**
 * The closed operand shape of one lifecycle journal event. The record is
 * local-only retention for recovery; the `expected_locator` and
 * `target_locator` strings never reach the status / attempt projection
 * surface, the server or any diagnostic stream.
 *
 *   - `operation` — one of {@link LIFECYCLE_JOURNAL_OPERATIONS}.
 *   - `sourceId` — the canonical server source identity (UUIDv7).
 *   - `expectedVersionId` — the version the device observed before
 *     sending the lifecycle event.
 *   - `expectedLocator` — the locator the device believed it was
 *     operating on (nullable for `restore`).
 *   - `targetLocator` — the locator the device intends (nullable for
 *     `delete`; required for `rename`, `move`, `restore`).
 *   - `tombstoneId` — the server-side tombstone id (nullable for
 *     `rename`, `move`; required for `delete` / `restore`).
 *   - `policyRevision` — policy revision under which the decision was
 *     taken (>= 1).
 *   - `predecessorEventId` — ordered predecessor when the lifecycle
 *     event depends on a prior lifecycle event (e.g. `restore` after
 *     `delete`); null when the event has no predecessor.
 */
export interface LifecycleEventOperands {
  readonly operation: LifecycleJournalOperation;
  readonly sourceId: string;
  readonly expectedVersionId: string;
  readonly expectedLocator: string | null;
  readonly targetLocator: string | null;
  readonly tombstoneId: string | null;
  readonly policyRevision: number;
  readonly predecessorEventId: string | null;
}

/**
 * Construct one validated {@link LifecycleEventOperands}. The factory is
 * the only writer of the shape so every row carries consistent close-time
 * validation; an unknown operation token, a non-positive policy revision
 * or a missing source / version identity fails closed before any SQL
 * runs.
 */
export function createLifecycleEventOperands(
  draft: {
    readonly operation: LifecycleJournalOperation;
    readonly sourceId: string;
    readonly expectedVersionId: string;
    readonly expectedLocator?: string | null;
    readonly targetLocator?: string | null;
    readonly tombstoneId?: string | null;
    readonly policyRevision: number;
    readonly predecessorEventId?: string | null;
  },
): LifecycleEventOperands {
  if (!isLifecycleJournalOperation(draft.operation)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (
    typeof draft.sourceId !== "string" ||
    typeof draft.expectedVersionId !== "string"
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (
    typeof draft.policyRevision !== "number" ||
    !Number.isInteger(draft.policyRevision) ||
    draft.policyRevision < 1
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  return {
    operation: draft.operation,
    sourceId: draft.sourceId,
    expectedVersionId: draft.expectedVersionId,
    expectedLocator: draft.expectedLocator ?? null,
    targetLocator: draft.targetLocator ?? null,
    tombstoneId: draft.tombstoneId ?? null,
    policyRevision: draft.policyRevision,
    predecessorEventId: draft.predecessorEventId ?? null,
  };
}

// --- Child 4 → Child 5 schema migration -----------------------------------------------------

const LIFECYCLE_MIGRATION_DDL = [
  "alter table local_files add column last_locator text;",
  "alter table local_files add column open_tombstone_id text;",
  "alter table local_files add column lifecycle_state text not null default 'active';",
  "create table if not exists lifecycle_event_operands (",
  "  event_id text primary key references journal_events (event_id),",
  "  source_id text not null,",
  "  expected_version_id text not null,",
  "  expected_locator text,",
  "  target_locator text,",
  "  tombstone_id text,",
  "  policy_revision integer not null check (policy_revision >= 1),",
  "  predecessor_event_id text references journal_events (event_id)",
  ");",
  "create index if not exists lifecycle_operands_predecessor_idx",
  "  on lifecycle_event_operands (predecessor_event_id);",
  `update journal_meta set schema_version = ${JOURNAL_SCHEMA_VERSION} where singleton_key = 1;`,
  `pragma user_version = ${JOURNAL_SCHEMA_VERSION};`,
].join("");

function readUserVersion(engine: SqliteDatabaseEngine): number {
  const result = engine.exec("pragma user_version;");
  const value = result[0]?.values[0]?.[0];
  return typeof value === "number" ? value : Number.NaN;
}

function childFourJournalImageLooksValid(engine: SqliteDatabaseEngine): boolean {
  try {
    const metaRows = engine.exec(
      [
        "select schema_version, dirty_generation, last_verified_generation,",
        "is_reconcile_required, recovery_state from journal_meta",
        "where singleton_key = 1;",
      ].join(" "),
    );
    if (metaRows[0]?.values[0] === undefined) {
      return false;
    }
    const tables = engine.exec(
      "select name from sqlite_master where type = 'table' order by name;",
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => row[0]);
    const required = [
      "journal_attempts",
      "journal_events",
      "journal_meta",
      "local_files",
    ];
    for (const name of required) {
      if (!(tableNames as readonly unknown[]).includes(name)) {
        return false;
      }
    }
    return true;
  } catch {
    return false;
  }
}

/**
 * Migrate one Child 4 (`pragma user_version = 2`) journal image to the
 * Child 5 schema in memory. The function returns a fresh v3 image; the
 * input image is never mutated.
 *
 * The migration is deterministic (no time-dependent defaults, no env-
 * dependent branches) and runs inside one `begin immediate ... commit`
 * transaction so a torn migration leaves the original image untouched.
 * Re-opening the returned image with {@link SqliteDatabase.openFromImage}
 * proves every prior `local_files`, `journal_events`, `journal_attempts`
 * and manifest generation survives.
 */
export function migrateChildFourJournalToLifecycleSchema(
  engineModule: SqliteEngineModule,
  image: ArrayLike<number>,
): Uint8Array {
  const engine = new engineModule.Database(image);
  try {
    const currentVersion = readUserVersion(engine);
    if (currentVersion !== CHILD_FOUR_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!childFourJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(LIFECYCLE_MIGRATION_DDL);
      engine.exec("commit;");
    } catch (error) {
      try {
        engine.exec("rollback;");
      } catch {
        // Best-effort rollback: the closed reason below is the answer.
      }
      throw error instanceof JournalStoreError
        ? error
        : journalStoreError("journal_mutation_failed");
    }
    return engine.export();
  } finally {
    engine.close();
  }
}
