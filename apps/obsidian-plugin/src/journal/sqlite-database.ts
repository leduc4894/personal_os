/**
 * The sql.js adapter and the one-writer transaction boundary of the portable
 * journal (spec 6.1, 6.3).
 *
 * The journal opens SQLite only through the vendored sql.js WebAssembly
 * package — never a Node built-in, Electron API, native SQLite driver or an
 * ORM — and every database mutation flows through ONE serialized async queue
 * wrapped in an explicit transaction. This module owns the full logical
 * schema of spec 6.3 (`journal_meta`, `local_files`, `journal_events` and
 * the bounded `journal_attempts`) and its `user_version` migration
 * bookkeeping; the repository builds records on top of the same serialized
 * writer.
 *
 * Privacy (spec 9): every failure surfaces as a closed reason token on
 * {@link JournalStoreError}. Library exceptions, SQL text, paths and digests
 * never enter a thrown error.
 */

import initSqlJs from "sql.js";

import { JOURNAL_RECOVERY_STATES } from "./contracts";
import type { JournalMeta, JournalRecoveryState } from "./contracts";

// --- closed schema bookkeeping (spec 6.3) ----------------------------------------------

/**
 * The journal logical schema version this build writes and understands.
 * Version 2 added the `local_files`, `journal_events` and `journal_attempts`
 * records of spec 6.3 on top of the version-1 `journal_meta` skeleton.
 */
export const JOURNAL_SCHEMA_VERSION = 2;

// --- closed failure reasons ---------------------------------------------------------------

/**
 * The closed failure vocabulary of the journal store. Diagnostics may carry
 * these tokens and nothing else.
 */
export const JOURNAL_STORE_ERROR_REASONS = [
  "journal_schema_unsupported",
  "journal_image_invalid",
  "journal_mutation_failed",
  "journal_query_failed",
  "journal_store_unavailable",
  "journal_generation_write_failed",
  "journal_manifest_invalid",
  "journal_not_open",
] as const;

export type JournalStoreErrorReason = (typeof JOURNAL_STORE_ERROR_REASONS)[number];

/** One journal store failure: a closed reason token and a static safe message. */
export class JournalStoreError extends Error {
  readonly reason: JournalStoreErrorReason;

  constructor(reason: JournalStoreErrorReason, message: string) {
    super(message);
    this.name = "JournalStoreError";
    this.reason = reason;
  }
}

/** Build the closed journal store failure for one reason token. */
export function journalStoreError(reason: JournalStoreErrorReason): JournalStoreError {
  return new JournalStoreError(reason, `journal store failed: ${reason}`);
}

// --- narrow sql.js engine surface ----------------------------------------------------------

/** One statement result of a read-only query. */
export interface SqliteQueryResult {
  readonly columns: readonly string[];
  readonly values: readonly (readonly unknown[])[];
}

/**
 * The structural slice of a sql.js database instance the journal depends on.
 * Keeping the dependency structural lets tests and production share the same
 * vendored engine without leaking the emscripten types outward.
 */
export interface SqliteDatabaseEngine {
  exec(sql: string): SqliteQueryResult[];
  export(): Uint8Array;
  close(): void;
}

export type SqliteDatabaseEngineConstructor = new (
  image?: ArrayLike<number> | null,
) => SqliteDatabaseEngine;

/** The loaded sql.js module exposing the database constructor. */
export interface SqliteEngineModule {
  readonly Database: SqliteDatabaseEngineConstructor;
}

/** The optional engine initialization inputs (wasm binary passthrough). */
export interface SqliteEngineLoadOptions {
  readonly wasmBinary: ArrayBuffer;
}

export type SqliteEngineLoader = (options?: SqliteEngineLoadOptions) => Promise<SqliteEngineModule>;

/**
 * The production engine loader: the pinned sql.js WebAssembly package is the
 * single permitted database engine (journal design 6.1). Loading stays lazy —
 * nothing initializes until the journal persistence layer first opens.
 */
export const loadVendoredSqliteEngine: SqliteEngineLoader = (options) => initSqlJs(options);

// --- journal schema (spec 6.3) ------------------------------------------------------------------

const JOURNAL_META_DDL = `
create table if not exists journal_meta (
  singleton_key integer primary key check (singleton_key = 1),
  schema_version integer not null,
  dirty_generation integer not null,
  last_verified_generation integer not null,
  is_reconcile_required integer not null check (is_reconcile_required in (0, 1)),
  recovery_state text not null
);
`;

/**
 * The per-file source-mapping record of spec 6.3: a random plugin-local
 * identity (never a canonical locator), the normalized current path, the
 * nullable server `source_id`, the observed fingerprint, the last committed
 * base version and the policy revision of the observation.
 */
const LOCAL_FILES_DDL = `
create table if not exists local_files (
  local_file_id text primary key,
  normalized_path text not null unique,
  source_id text,
  observed_sha256 text not null,
  observed_size_bytes integer not null check (observed_size_bytes >= 0),
  observed_media_type text not null,
  base_version_id text,
  policy_revision integer not null check (policy_revision >= 0)
);
`;

/**
 * The durable create/update intent of spec 6.3/7.2, including the freeze
 * marker that makes the fingerprint immutable from the moment preflight
 * starts — a later save then needs a successor event, never an update.
 */
const JOURNAL_EVENTS_DDL = `
create table if not exists journal_events (
  event_id text primary key,
  local_file_id text not null references local_files (local_file_id),
  idempotency_key text not null unique,
  operation text not null check (operation in ('create', 'update')),
  sha256 text not null,
  size_bytes integer not null check (size_bytes >= 0),
  media_type text not null,
  state text not null check (state in ('queued', 'preflight', 'uploading', 'committed',
    'no_change', 'waiting_retry', 'excluded_policy', 'blocked_size', 'blocked_conflict',
    'deferred_lifecycle', 'integrity_failed')),
  is_fingerprint_frozen integer not null check (is_fingerprint_frozen in (0, 1)),
  attempt_count integer not null check (attempt_count >= 0),
  next_eligible_retry_epoch_ms integer,
  safe_error text,
  operation_id text,
  created_at_epoch_ms integer not null check (created_at_epoch_ms >= 0)
);
create index if not exists journal_events_file_created_idx
  on journal_events (local_file_id, created_at_epoch_ms);
create index if not exists journal_events_state_idx on journal_events (state);
`;

/**
 * The bounded attempt-audit ring of spec 6.3: timestamp, closed outcome
 * label and opaque request correlation ID only. The repository prunes to the
 * most recent `MAX_EVENT_ATTEMPT_HISTORY` rows per event inside the same
 * transaction as every insert.
 */
const JOURNAL_ATTEMPTS_DDL = `
create table if not exists journal_attempts (
  attempt_ordinal integer primary key autoincrement,
  event_id text not null references journal_events (event_id),
  attempted_at_epoch_ms integer not null check (attempted_at_epoch_ms >= 0),
  outcome_label text not null,
  request_correlation_id text not null
);
create index if not exists journal_attempts_event_idx
  on journal_attempts (event_id, attempt_ordinal);
`;

const JOURNAL_SCHEMA_DDL = [
  JOURNAL_META_DDL,
  LOCAL_FILES_DDL,
  JOURNAL_EVENTS_DDL,
  JOURNAL_ATTEMPTS_DDL,
].join("");

function isJournalRecoveryState(value: unknown): value is JournalRecoveryState {
  return (
    typeof value === "string" && (JOURNAL_RECOVERY_STATES as readonly string[]).includes(value)
  );
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

/** Validate one meta value for writing; rejects anything outside the contract. */
function validateJournalMeta(meta: JournalMeta): void {
  if (meta.schemaVersion !== JOURNAL_SCHEMA_VERSION) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isNonNegativeInteger(meta.dirtyGeneration) || !isNonNegativeInteger(meta.lastVerifiedGeneration)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (typeof meta.isReconcileRequired !== "boolean" || !isJournalRecoveryState(meta.recoveryState)) {
    throw journalStoreError("journal_mutation_failed");
  }
}

/** Render one validated meta value as the single-row insert/update statement. */
function journalMetaWriteSql(meta: JournalMeta): string {
  validateJournalMeta(meta);
  const reconcileFlag = meta.isReconcileRequired ? 1 : 0;
  return [
    "update journal_meta set",
    `schema_version = ${meta.schemaVersion},`,
    `dirty_generation = ${meta.dirtyGeneration},`,
    `last_verified_generation = ${meta.lastVerifiedGeneration},`,
    `is_reconcile_required = ${reconcileFlag},`,
    `recovery_state = '${meta.recoveryState}'`,
    "where singleton_key = 1;",
  ].join(" ");
}

/** Parse the persisted meta row back into the frozen contract shape. */
function parseJournalMetaRow(row: readonly unknown[]): JournalMeta {
  const [
    schemaVersion,
    dirtyGeneration,
    lastVerifiedGeneration,
    isReconcileRequired,
    recoveryState,
  ] = row;
  const meta = {
    schemaVersion: schemaVersion as number,
    dirtyGeneration: dirtyGeneration as number,
    lastVerifiedGeneration: lastVerifiedGeneration as number,
    isReconcileRequired: isReconcileRequired === 1,
    recoveryState: recoveryState as JournalRecoveryState,
  };
  validateJournalMeta(meta);
  return meta;
}

// --- mutation session ---------------------------------------------------------------------------

/** The in-transaction surface one serialized mutation operates on. */
export interface SqliteMutationSession {
  /** Run SQL inside the open transaction (validated, journal-scoped use only). */
  exec(sql: string): void;
  /** Run a read-only query inside the open transaction and read its rows. */
  readRows(sql: string): SqliteQueryResult[];
  readJournalMeta(): JournalMeta;
  writeJournalMeta(meta: JournalMeta): void;
}

// --- the serialized database ----------------------------------------------------------------------

/**
 * One open journal database with the single serialized mutation queue of
 * spec 6.1. Concurrent {@link runSerializedMutation} calls execute strictly
 * one at a time in submission order, each inside one explicit transaction
 * that rolls back completely on failure.
 */
export class SqliteDatabase {
  readonly #engine: SqliteDatabaseEngine;
  #mutationTail: Promise<unknown> = Promise.resolve();

  private constructor(engine: SqliteDatabaseEngine) {
    this.#engine = engine;
  }

  /** Create an empty journal image stamped with the current schema version. */
  static createEmpty(
    engineModule: SqliteEngineModule,
    initialMeta: JournalMeta,
  ): SqliteDatabase {
    validateJournalMeta(initialMeta);
    const database = new SqliteDatabase(new engineModule.Database());
    try {
      database.#engine.exec(JOURNAL_SCHEMA_DDL);
      database.#engine.exec(
        [
          "insert into journal_meta (singleton_key, schema_version, dirty_generation,",
          "last_verified_generation, is_reconcile_required, recovery_state) values (1,",
          `${initialMeta.schemaVersion}, ${initialMeta.dirtyGeneration},`,
          `${initialMeta.lastVerifiedGeneration},`,
          `${initialMeta.isReconcileRequired ? 1 : 0}, '${initialMeta.recoveryState}');`,
        ].join(" "),
      );
      database.#engine.exec(`pragma user_version = ${JOURNAL_SCHEMA_VERSION};`);
      return database;
    } catch {
      database.close();
      throw journalStoreError("journal_mutation_failed");
    }
  }

  /**
   * Open a persisted journal image. The image must carry exactly the schema
   * version this build understands: an older or newer journal lineage fails
   * closed as `journal_schema_unsupported` (a migration problem, never
   * conflated with a non-journal image), while bytes that are not a journal
   * image at all fail as `journal_image_invalid` — in both cases without
   * executing any of the image's statements.
   */
  static openFromImage(
    engineModule: SqliteEngineModule,
    image: ArrayLike<number>,
  ): SqliteDatabase {
    let engine: SqliteDatabaseEngine | null = null;
    try {
      engine = new engineModule.Database(image);
      const schemaVersion = SqliteDatabase.#readSchemaVersionOf(engine);
      if (schemaVersion !== JOURNAL_SCHEMA_VERSION) {
        throw journalStoreError("journal_schema_unsupported");
      }
      const database = new SqliteDatabase(engine);
      // Reading the meta row proves the journal schema is really present.
      database.readJournalMeta();
      return database;
    } catch (error) {
      engine?.close();
      if (error instanceof JournalStoreError) {
        throw error;
      }
      throw journalStoreError("journal_image_invalid");
    }
  }

  static #readSchemaVersionOf(engine: SqliteDatabaseEngine): number {
    const result = engine.exec("pragma user_version;");
    const value = result[0]?.values[0]?.[0];
    return typeof value === "number" ? value : Number.NaN;
  }

  /** Run one read-only query and return its full result set. */
  readAll(sql: string): SqliteQueryResult[] {
    try {
      return this.#engine.exec(sql);
    } catch {
      throw journalStoreError("journal_query_failed");
    }
  }

  /** The persisted schema bookkeeping version of this image. */
  readSchemaVersion(): number {
    try {
      return SqliteDatabase.#readSchemaVersionOf(this.#engine);
    } catch {
      throw journalStoreError("journal_query_failed");
    }
  }

  /** Read the single journal meta row. */
  readJournalMeta(): JournalMeta {
    const result = this.readAll(
      [
        "select schema_version, dirty_generation, last_verified_generation,",
        "is_reconcile_required, recovery_state from journal_meta where singleton_key = 1;",
      ].join(" "),
    );
    const row = result[0]?.values[0];
    if (row === undefined) {
      throw journalStoreError("journal_image_invalid");
    }
    try {
      return parseJournalMetaRow(row);
    } catch {
      throw journalStoreError("journal_image_invalid");
    }
  }

  /** Export the current in-memory state as one portable database image. */
  exportImage(): Uint8Array {
    try {
      return this.#engine.export();
    } catch {
      throw journalStoreError("journal_query_failed");
    }
  }

  close(): void {
    this.#engine.close();
  }

  /**
   * The single serialized writer (spec 6.1): every mutation of this database
   * flows through this queue, one transaction at a time, in submission
   * order. A throwing operation rolls its transaction back completely and
   * never leaks the original failure detail.
   */
  async runSerializedMutation<T>(
    operation: (session: SqliteMutationSession) => T | Promise<T>,
  ): Promise<T> {
    const execution = this.#mutationTail.then(() => this.#executeInTransaction(operation));
    this.#mutationTail = execution.then(
      () => undefined,
      () => undefined,
    );
    return execution;
  }

  async #executeInTransaction<T>(
    operation: (session: SqliteMutationSession) => T | Promise<T>,
  ): Promise<T> {
    let hasTransactionBegun = false;
    try {
      this.#engine.exec("begin immediate;");
      hasTransactionBegun = true;
      const session: SqliteMutationSession = {
        exec: (sql: string): void => {
          this.#engine.exec(sql);
        },
        readRows: (sql: string): SqliteQueryResult[] => {
          try {
            return this.#engine.exec(sql);
          } catch {
            throw journalStoreError("journal_query_failed");
          }
        },
        readJournalMeta: (): JournalMeta => this.readJournalMeta(),
        writeJournalMeta: (meta: JournalMeta): void => {
          this.#engine.exec(journalMetaWriteSql(meta));
        },
      };
      const result = await operation(session);
      this.#engine.exec("commit;");
      return result;
    } catch (error) {
      if (hasTransactionBegun) {
        try {
          this.#engine.exec("rollback;");
        } catch {
          // Best-effort rollback: the closed reason below is the answer.
        }
      }
      throw error instanceof JournalStoreError
        ? error
        : journalStoreError("journal_mutation_failed");
    }
  }
}
