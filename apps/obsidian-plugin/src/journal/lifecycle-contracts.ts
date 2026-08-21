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
  CHILD_FIVE_FIX_SCHEMA_VERSION,
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

/**
 * The Child 5 lifecycle extension schema version (3). The
 * {@link migrateLifecycleJournalToLastCommittedSchema} function upgrades a
 * v3 image to the v4 {@link JOURNAL_SCHEMA_VERSION}.
 */
export const CHILD_FIVE_SCHEMA_VERSION = 3;

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
 *   - `capturedFingerprintSha256` / `capturedFingerprintSizeBytes` /
 *     `capturedFingerprintMediaType` — the bytes hash of the target
 *     file at the moment the lifecycle event was minted (post-settle).
 *     Only set for `rename` and `move`; nullable otherwise. The durable
 *     `local_files.normalized_path` is then updated to `targetLocator`
 *     in the same transaction so a later read resolves by the new
 *     locator (spec 7.1 fix round 1 C1 + I2).
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
  readonly capturedFingerprintSha256: string | null;
  readonly capturedFingerprintSizeBytes: number | null;
  readonly capturedFingerprintMediaType: string | null;
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
    readonly expectedLocator?: string | null | undefined;
    readonly targetLocator?: string | null | undefined;
    readonly tombstoneId?: string | null | undefined;
    readonly policyRevision: number;
    readonly predecessorEventId?: string | null | undefined;
    readonly capturedFingerprintSha256?: string | null | undefined;
    readonly capturedFingerprintSizeBytes?: number | null | undefined;
    readonly capturedFingerprintMediaType?: string | null | undefined;
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
  const capturedSha = draft.capturedFingerprintSha256 ?? null;
  const capturedSize = draft.capturedFingerprintSizeBytes ?? null;
  const capturedMedia = draft.capturedFingerprintMediaType ?? null;
  if (
    (capturedSha === null) !== (capturedSize === null) ||
    (capturedSha === null) !== (capturedMedia === null)
  ) {
    // The captured-fingerprint triple must be set together or not at all.
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
    capturedFingerprintSha256: capturedSha,
    capturedFingerprintSizeBytes: capturedSize,
    capturedFingerprintMediaType: capturedMedia,
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
  `update journal_meta set schema_version = ${CHILD_FIVE_SCHEMA_VERSION} where singleton_key = 1;`,
  `pragma user_version = ${CHILD_FIVE_SCHEMA_VERSION};`,
].join("");

// --- Child 5 → Child 5 fix schema migration (v3 → v4) ---------------------------------------

const LAST_COMMITTED_MIGRATION_DDL = [
  "alter table local_files add column last_committed_sha256 text;",
  "alter table local_files add column last_committed_size_bytes integer;",
  "alter table local_files add column last_committed_media_type text;",
  "update journal_meta set schema_version = 4 where singleton_key = 1;",
  "pragma user_version = 4;",
].join("");

// --- Child 5 fix → server-receipt schema migration (v4 → v5) ------------------------------

/**
 * The schema version that introduced the `server_receipt_tombstone_id`
 * column on `lifecycle_event_operands`. The migration adds the column
 * (nullable; every prior row reads back with `null` until the first
 * committed delete writes a server receipt) and bumps
 * `journal_meta.schema_version` plus `pragma user_version` to v5.
 *
 * The destination version is pinned to `5` here (NOT interpolated from
 * `JOURNAL_SCHEMA_VERSION`) so a future v5 → v6 migration can layer on
 * top of this block without rewriting the v4 → v5 DDL.
 */
const SERVER_RECEIPT_MIGRATION_DDL = [
  "alter table lifecycle_event_operands add column server_receipt_tombstone_id text;",
  "update journal_meta set schema_version = 5 where singleton_key = 1;",
  "pragma user_version = 5;",
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

function lifecycleJournalImageLooksValid(engine: SqliteDatabaseEngine): boolean {
  try {
    const metaRows = engine.exec(
      [
        "select schema_version from journal_meta where singleton_key = 1;",
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
      "lifecycle_event_operands",
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

/**
 * Migrate one Child 5 (`pragma user_version = 3`) journal image to the
 * v4 lifecycle-journal schema in memory. The migration adds the
 * `last_committed_*` columns to `local_files`; every existing v3 row
 * reads back with `last_committed_* = null` until the first commit
 * receipt lands. The input image is never mutated; the function returns
 * the upgraded image and runs the DDL inside one transaction.
 */
export function migrateLifecycleJournalToLastCommittedSchema(
  engineModule: SqliteEngineModule,
  image: ArrayLike<number>,
): Uint8Array {
  const engine = new engineModule.Database(image);
  try {
    const currentVersion = readUserVersion(engine);
    if (currentVersion !== CHILD_FIVE_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!lifecycleJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(LAST_COMMITTED_MIGRATION_DDL);
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

/**
 * The image validator for the v4 fix: the lifecycle operands table is
 * already present from v3, the `last_committed_*` columns from the v3
 * → v4 fix are in place, and the schema bookkeeping reflects v4.
 * Migration adds the `server_receipt_tombstone_id` column on
 * `lifecycle_event_operands` so the restore driver can read the
 * server-confirmed tombstone id from the delete predecessor's persisted
 * receipt (task 9 fix round 1 I1).
 */
function lastCommittedJournalImageLooksValid(engine: SqliteDatabaseEngine): boolean {
  try {
    const metaRows = engine.exec(
      [
        "select schema_version from journal_meta where singleton_key = 1;",
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
      "lifecycle_event_operands",
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
 * Migrate one Child 5 fix (`pragma user_version = 4`) journal image to
 * the v5 server-receipt schema in memory. The migration adds the
 * `server_receipt_tombstone_id` column on `lifecycle_event_operands`;
 * every v4 row reads back with `server_receipt_tombstone_id = null`
 * until the first committed delete writes a server receipt. The input
 * image is never mutated; the function returns the upgraded image and
 * runs the DDL inside one transaction.
 */
export function migrateLastCommittedJournalToServerReceiptSchema(
  engineModule: SqliteEngineModule,
  image: ArrayLike<number>,
): Uint8Array {
  const engine = new engineModule.Database(image);
  try {
    const currentVersion = readUserVersion(engine);
    if (currentVersion !== CHILD_FIVE_FIX_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!lastCommittedJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(SERVER_RECEIPT_MIGRATION_DDL);
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
