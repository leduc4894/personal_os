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

import {
  CONFLICT_LOCAL_REPAIR_ACTIONS,
  CONFLICT_LOCAL_REPAIR_SAFE_REASONS,
} from "../conflicts/contracts";
import {
  JOURNAL_RECOVERY_STATES,
  MAX_MULTIPART_PART_COUNT,
  MULTIPART_PART_SIZE_BYTES,
  MULTIPART_SAFE_REASON_TOKENS,
  MULTIPART_SESSION_STATES,
} from "./contracts";
import type { JournalMeta, JournalRecoveryState } from "./contracts";

// --- closed schema bookkeeping (spec 6.3) ----------------------------------------------

/**
 * The journal logical schema version this build writes and understands.
 * Version 2 added the `local_files`, `journal_events` and `journal_attempts`
 * records of spec 6.3 on top of the version-1 `journal_meta` skeleton.
 *
 * Version 3 (Child 5) extends the durable journal with lifecycle records
 * for `rename`, `move`, `delete` and `restore` operations: the
 * `lifecycle_event_operands` keyed extension table, the
 * `last_locator`/`open_tombstone_id`/`lifecycle_state` columns on
 * `local_files` and the closed `LIFECYCLE_JOURNAL_OPERATIONS` enum on
 * `journal_events.operation`. The migration is deterministic and runs
 * inside a single transaction; no child-4 row is lost.
 *
 * Version 4 (fix round 1) adds the `last_committed_sha256` /
 * `last_committed_size_bytes` / `last_committed_media_type` columns on
 * `local_files` so the lifecycle capture can verify a restore eligibility
 * against the bytes that the server last committed, not the bytes the
 * device most recently observed. The new columns are nullable; every
 * Child 5 row reads back with `last_committed_* = null` until the first
 * committed receipt lands.
 *
 * Version 5 (task 9 fix round 1 I1) adds the
 * `server_receipt_tombstone_id` column on `lifecycle_event_operands` so
 * the durable record of one committed `delete` carries the tombstone id
 * the server returned on the wire — the same id the follow-up `restore`
 * sends back. The restore driver is now guaranteed to read the
 * server-confirmed tombstone id from the predecessor's persisted
 * receipt, never from a locally-derived guess. The column is nullable;
 * every v4 row reads back with `server_receipt_tombstone_id = null`
 * until the first delete commits.
 *
 * Version 6 (explicit-restore target reservation) adds the nullable
 * `restore_prior_path` column on `local_files`. The reservation flow
 * writes the pre-reservation path there when it rebinds a tombstoned row
 * to an explicit restore target, so an explicit cancel can return the
 * row to its prior path and a committed restore can clear it. Every v5
 * row reads back with `restore_prior_path = null` until the first
 * reservation lands.
 *
 * Version 7 (device cursor and manifest reconciliation, task 8) adds the
 * five device-sync reconciliation tables of spec 8: the
 * `device_sync_state` singleton (local applied cursor, last
 * server-acknowledged cursor, monotonic Vault observation generation,
 * active repair barrier generation/reason and the resumable manifest run
 * checkpoint/final digest), `manifest_page_progress`,
 * `manifest_action_progress`, `remote_apply_operations` and
 * `echo_markers`. The v6 → v7 migration
 * (`migrateRestoreReservationJournalToDeviceSyncSchema`) is lossless:
 * every file mapping, pending event, lifecycle operand, tombstone,
 * restore reservation and attempt survives, both cursor values start at
 * zero, no barrier/apply/echo row exists and a pre-existing
 * `is_reconcile_required = 1` stays set.
 *
 * Version 8 (resumable multipart mobile upload, task 9) adds the
 * `multipart_upload_progress` table of child 7 spec 4.1: one row per
 * frozen outbound journal event carrying ONLY the safe transfer progress —
 * the opaque public session ID, the fixed part geometry and expiry, the
 * completed part-number JSON, the last observed closed session state and
 * the last closed safe reason. No URL, provider upload ID, ETag, staging
 * key, digest or locator ever persists. The v7 → v8 migration
 * (`migrateDeviceSyncJournalToMultipartProgressSchema`) is lossless: every
 * v7 row — file mapping, event, attempt, lifecycle operand, reservation,
 * cursor, barrier, apply and echo marker — survives unchanged and the new
 * table starts empty.
 *
 * Version 9 (conflict local repair, task 7, Child 8 spec 5.2.6/6) adds
 * the `conflict_local_repairs` table of the Conflict Inbox repair state:
 * one row per still-owed local apply, carrying ONLY the conflict UUID,
 * the resolution event identity, the closed target action, the closed
 * safe reason and the retry bookkeeping (attempt count, next eligible
 * retry epoch, created/updated epochs). No evidence bytes, merged draft
 * bytes, version id, path, locator or digest column exists — byte and
 * path storage are unrepresentable in the schema itself, and the CHECK
 * constraints pin the closed vocabularies of
 * `../conflicts/contracts.ts` (single source of truth). The v8 → v9
 * migration (`migrateMultipartProgressJournalToConflictRepairSchema`)
 * is lossless: every v8 row — file mapping, event, attempt, lifecycle
 * operand, reservation, cursor, barrier, manifest progress, remote
 * apply, echo marker and multipart progress — survives unchanged and
 * the new table starts empty.
 *
 * Version 10 (pending rename-chain recovery) adds two owner-bound tables:
 * `pending_rename_intents` stores the next canonical prior endpoint and
 * latest observed Vault endpoint for one local row, while
 * `pending_rename_intent_missing_file_deferrals` stores the separately
 * bounded, event-bound missing-file replay budget. The v9 -> v10 migration
 * is lossless and both tables start empty.
 */
export const JOURNAL_SCHEMA_VERSION = 10;

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
 *
 * The Child 5 lifecycle extension appends the last known locator, the open
 * tombstone id (when the source has been tombstoned but not yet restored)
 * and the closed `lifecycle_state`. Existing rows from a child-4 image
 * read back with `last_locator = null`, `open_tombstone_id = null` and
 * `lifecycle_state = 'active'`.
 *
 * Version 4 adds the `last_committed_*` triple of columns. The triple is
 * written ONLY by `recordCommittedReceipt` and `recordNoChangeReceipt` so
 * the lifecycle capture can verify a restore eligibility against the
 * bytes the server last committed — never against the mutable
 * `observed_fingerprint` that a fresh capture may have overwritten.
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
  policy_revision integer not null check (policy_revision >= 0),
  last_locator text,
  open_tombstone_id text,
  lifecycle_state text not null default 'active'
    check (lifecycle_state in ('active', 'rename_pending', 'move_pending',
      'delete_pending', 'restore_pending', 'tombstoned', 'restored',
      'reconcile_required')),
  last_committed_sha256 text,
  last_committed_size_bytes integer check (last_committed_size_bytes >= 0),
  last_committed_media_type text,
  restore_prior_path text
);
`;

/**
 * The durable create/update intent of spec 6.3/7.2, including the freeze
 * marker that makes the fingerprint immutable from the moment preflight
 * starts — a later save then needs a successor event, never an update.
 *
 * The Child 5 lifecycle extension broadens `operation` to also admit the
 * four closed lifecycle tokens (`rename`, `move`, `delete`, `restore`).
 * Lifecycle events reuse the same freeze marker: once preflight starts the
 * operands and the fingerprint never change, and a later lifecycle
 * mutation becomes a successor event (no lifecycle coalescing).
 */
const JOURNAL_EVENTS_DDL = `
create table if not exists journal_events (
  event_id text primary key,
  local_file_id text not null references local_files (local_file_id),
  idempotency_key text not null unique,
  operation text not null check (operation in ('create', 'update',
    'rename', 'move', 'delete', 'restore')),
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

/**
 * The Child 5 lifecycle operands keyed extension: every journal_events row
 * whose operation is a lifecycle token (`rename`, `move`, `delete`,
 * `restore`) carries exactly one `lifecycle_event_operands` row with the
 * `source_id`, the expected version id, the nullable expected / target
 * locator pointers, the nullable tombstone id, the policy revision under
 * which the decision was taken and the ordered predecessor event id when
 * one exists. Content events do not have a row here; the keyed extension
 * is the single source of truth for lifecycle operands.
 */
const LIFECYCLE_EVENT_OPERANDS_DDL = `
create table if not exists lifecycle_event_operands (
  event_id text primary key references journal_events (event_id),
  source_id text not null,
  expected_version_id text not null,
  expected_locator text,
  target_locator text,
  tombstone_id text,
  policy_revision integer not null check (policy_revision >= 1),
  predecessor_event_id text references journal_events (event_id),
  server_receipt_tombstone_id text
);
create index if not exists lifecycle_operands_predecessor_idx
  on lifecycle_event_operands (predecessor_event_id);
`;

// --- device-sync reconciliation schema (spec 8, task 8) --------------------------------------

/**
 * The zeroed device-sync reconciliation singleton of spec 8: the local
 * applied cursor, the last server-acknowledged cursor (never ahead of the
 * applied one), the monotonic Vault observation generation, the active
 * repair barrier generation/reason and the resumable manifest run
 * checkpoint/final digest. One row, enforced single by the singleton key;
 * every field is a count, closed token or opaque protocol identity — never
 * raw content, credentials or provider detail.
 */
const DEVICE_SYNC_STATE_DDL = `
create table if not exists device_sync_state (
  singleton_key integer primary key check (singleton_key = 1),
  applied_sequence integer not null check (applied_sequence >= 0),
  acknowledged_sequence integer not null check (acknowledged_sequence >= 0
    and acknowledged_sequence <= applied_sequence),
  observation_generation integer not null check (observation_generation >= 0),
  barrier_generation integer check (barrier_generation >= 0),
  barrier_reason text,
  active_manifest_run_id text,
  manifest_checkpoint_sequence integer check (manifest_checkpoint_sequence >= 0),
  manifest_final_digest text
);
`;

/** The contiguous zero-based accepted-page progress of one manifest run. */
const MANIFEST_PAGE_PROGRESS_DDL = `
create table if not exists manifest_page_progress (
  manifest_run_id text not null,
  page_number integer not null check (page_number >= 0),
  entry_count integer not null check (entry_count >= 0),
  page_digest text not null,
  primary key (manifest_run_id, page_number)
);
`;

/**
 * The per-action progress of one manifest run: the frozen planned action
 * kind plus whether the action has already reached its terminal-safe
 * local outcome. Same-index replays must carry the same planned kind.
 */
const MANIFEST_ACTION_PROGRESS_DDL = `
create table if not exists manifest_action_progress (
  manifest_run_id text not null,
  action_index integer not null check (action_index >= 0),
  action_kind text not null check (action_kind in ('upload', 'download',
    'apply_tombstone', 'conflict', 'no_change', 'excluded')),
  outcome text not null check (outcome in ('received', 'terminal_safe')),
  safe_reason_code text,
  primary key (manifest_run_id, action_index)
);
`;

/**
 * One crash-safe remote apply operation of spec 8.1: only local
 * correctness evidence — the server event identity, the operation-shaped
 * locator operands, the expected base/final fingerprints, the opaque
 * temporary/rollback tokens, the closed state and the nullable closed
 * error. No bytes, credential, object key, URL or provider response is
 * stored.
 */
const REMOTE_APPLY_OPERATIONS_DDL = `
create table if not exists remote_apply_operations (
  event_sequence integer primary key check (event_sequence >= 1),
  event_id text not null,
  source_id text not null,
  operation text not null check (operation in ('created', 'updated',
    'renamed', 'moved', 'deleted', 'restored')),
  prior_locator text,
  target_locator text,
  base_sha256 text,
  base_size_bytes integer check (base_size_bytes >= 0),
  base_media_type text,
  final_sha256 text,
  final_size_bytes integer check (final_size_bytes >= 0),
  final_media_type text,
  temp_token text,
  rollback_token text,
  state text not null check (state in ('prepared', 'temp_verified',
    'vault_mutated', 'locally_applied', 'server_acknowledged')),
  safe_error_code text
);
`;

/**
 * One exact echo marker of spec 8.2: the server event sequence, source,
 * operation and applicable prior/target locator operands plus the
 * expected final fingerprint. A watcher observation is suppressed only
 * when every applicable member matches; delete carries no final
 * fingerprint (its proof is the absent prior locator plus the retained
 * tombstone mapping).
 */
const ECHO_MARKERS_DDL = `
create table if not exists echo_markers (
  event_sequence integer primary key check (event_sequence >= 1),
  source_id text not null,
  operation text not null check (operation in ('created', 'updated',
    'renamed', 'moved', 'deleted', 'restored')),
  prior_locator text,
  target_locator text,
  final_sha256 text,
  final_size_bytes integer check (final_size_bytes >= 0),
  final_media_type text
);
`;

/** The zeroed singleton row every fresh v7 journal (and migration) seeds. */
const DEVICE_SYNC_STATE_SEED_SQL =
  "insert into device_sync_state (singleton_key, applied_sequence, acknowledged_sequence, observation_generation) values (1, 0, 0, 0);";

// --- multipart progress schema (child 7 spec 4.1, task 9) ----------------------------------------

/** Render one closed vocabulary as a SQL `in (...)` text list. */
function sqlTextList(values: readonly string[]): string {
  return values.map((value) => `'${value}'`).join(", ");
}

/**
 * The durable SAFE progress of one frozen outbound journal event's
 * multipart transfer (child 7 spec 4.1), keyed by the event ID: the opaque
 * public session ID, the fixed part geometry (an exactly-8-MiB ordinary
 * part, at most 13 parts), the session expiry, the completed part-number
 * JSON, the last observed closed session state and the last closed safe
 * reason. The check constraints mirror the frozen constants and closed
 * vocabularies of `contracts.ts`; the repository re-validates every value
 * (including the part-number JSON against the geometry) BEFORE any SQL
 * mutation runs. No column exists for a URL, provider upload ID, ETag,
 * staging key, digest, path or locator — none can ever persist.
 */
const MULTIPART_UPLOAD_PROGRESS_DDL = `
create table if not exists multipart_upload_progress (
  event_id text primary key references journal_events (event_id),
  session_id text not null,
  part_size_bytes integer not null check (part_size_bytes = ${MULTIPART_PART_SIZE_BYTES}),
  part_count integer not null check (part_count >= 1
    and part_count <= ${MAX_MULTIPART_PART_COUNT}),
  expires_at_epoch_ms integer not null check (expires_at_epoch_ms >= 0),
  completed_part_numbers_json text not null,
  session_state text not null check (session_state in (${sqlTextList(MULTIPART_SESSION_STATES)})),
  safe_reason text check (safe_reason is null
    or safe_reason in (${sqlTextList(MULTIPART_SAFE_REASON_TOKENS)}))
);
`;

// --- conflict local repair schema (task 7, Child 8 spec 5.2.6/6) --------------------------------------

/**
 * The durable no-byte repair fact of one resolved-but-not-yet-applied
 * conflict (Child 8 spec 5.2.6/6): the conflict UUID, the resolution
 * event identity, the closed target action, the closed safe reason and
 * the retry bookkeeping — nothing else. No column exists for evidence
 * bytes, merged draft bytes, a version id, a path, a locator or a
 * digest: byte and path storage are unrepresentable in the schema, and
 * the CHECK constraints pin the closed vocabularies of
 * `../conflicts/contracts.ts` (their single source of truth — the DDL
 * renders from the same runtime constants the repository validates
 * against). The repair worker re-reads the conflict detail over the
 * wire for the winner identity, so the row stays minimal.
 */
const CONFLICT_LOCAL_REPAIRS_DDL = `
create table if not exists conflict_local_repairs (
  conflict_id text primary key,
  resolution_event_id text not null,
  target_action text not null check (target_action in (${sqlTextList(CONFLICT_LOCAL_REPAIR_ACTIONS)})),
  safe_reason text not null check (safe_reason in (${sqlTextList(CONFLICT_LOCAL_REPAIR_SAFE_REASONS)})),
  attempt_count integer not null check (attempt_count >= 0),
  next_eligible_retry_epoch_ms integer check (next_eligible_retry_epoch_ms >= 0),
  created_at_epoch_ms integer not null check (created_at_epoch_ms >= 0),
  updated_at_epoch_ms integer not null check (updated_at_epoch_ms >= 0)
);
`;

// --- pending rename intent schema (untitled-transit rename-chain recovery) ----------------------

/** One durable, owner-proven rename chain per tracked local row. */
const PENDING_RENAME_INTENTS_DDL = `
create table if not exists pending_rename_intents (
  local_file_id text primary key
    references local_files (local_file_id) on delete cascade,
  prior_path text not null check (length(prior_path) > 0),
  current_path text not null check (length(current_path) > 0)
);
create unique index if not exists pending_rename_intents_current_path_uq
  on pending_rename_intents (current_path);
`;

/** The event-bound accepted parks 1..40 for an intent-owned missing file. */
const PENDING_RENAME_INTENT_MISSING_FILE_DEFERRALS_DDL = `
create table if not exists pending_rename_intent_missing_file_deferrals (
  local_file_id text primary key
    references pending_rename_intents (local_file_id) on delete cascade,
  event_id text not null unique references journal_events (event_id),
  deferred_attempt_count integer not null
    check (deferred_attempt_count between 1 and 40)
);
`;

const JOURNAL_SCHEMA_DDL = [
  JOURNAL_META_DDL,
  LOCAL_FILES_DDL,
  JOURNAL_EVENTS_DDL,
  JOURNAL_ATTEMPTS_DDL,
  LIFECYCLE_EVENT_OPERANDS_DDL,
  DEVICE_SYNC_STATE_DDL,
  MANIFEST_PAGE_PROGRESS_DDL,
  MANIFEST_ACTION_PROGRESS_DDL,
  REMOTE_APPLY_OPERATIONS_DDL,
  ECHO_MARKERS_DDL,
  MULTIPART_UPLOAD_PROGRESS_DDL,
  CONFLICT_LOCAL_REPAIRS_DDL,
  PENDING_RENAME_INTENTS_DDL,
  PENDING_RENAME_INTENT_MISSING_FILE_DEFERRALS_DDL,
].join("");

/**
 * The Child 4 schema version (2). The Child 4 → Child 5 migration
 * function lives in `lifecycle-contracts.ts`; this constant is exported
 * so the migration can pin the source version it accepts.
 */
export const CHILD_FOUR_SCHEMA_VERSION = 2;

/**
 * The Child 5 fix schema version (4). The v4 → v5 migration adds the
 * `server_receipt_tombstone_id` column on `lifecycle_event_operands`;
 * the function lives in `lifecycle-contracts.ts` and pins this constant
 * as the source version it accepts.
 */
export const CHILD_FIVE_FIX_SCHEMA_VERSION = 4;

/**
 * The server-receipt schema version (5). The v5 → v6 migration adds the
 * `restore_prior_path` column on `local_files`; the function lives in
 * `lifecycle-contracts.ts` and pins this constant as the source version
 * it accepts.
 */
export const SERVER_RECEIPT_SCHEMA_VERSION = 5;

/**
 * The restore-reservation schema version (6). The v6 → v7 migration
 * (`migrateRestoreReservationJournalToDeviceSyncSchema`, task 8) adds the
 * five device-sync reconciliation tables of spec 8; the function pins this
 * constant as the source version it accepts.
 */
export const RESTORE_RESERVATION_SCHEMA_VERSION = 6;

/**
 * The device-sync schema version (7). The v7 → v8 migration
 * (`migrateDeviceSyncJournalToMultipartProgressSchema`, task 9, child 7
 * spec 4.1) adds the `multipart_upload_progress` table; the function pins this
 * constant as the source version it accepts.
 */
export const DEVICE_SYNC_SCHEMA_VERSION = 7;

/**
 * The multipart-progress schema version (8). The v8 → v9 migration
 * (`migrateMultipartProgressJournalToConflictRepairSchema`, task 7,
 * Child 8 spec 5.2.6/6) adds the `conflict_local_repairs` table; the
 * function pins this constant as the source version it accepts.
 */
export const MULTIPART_PROGRESS_SCHEMA_VERSION = 8;

/** The v9 conflict-repair schema accepted by the v9 -> v10 migration. */
export const CONFLICT_REPAIR_SCHEMA_VERSION = 9;

// --- closed failure reasons ---------------------------------------------------------------

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
      database.#engine.exec(DEVICE_SYNC_STATE_SEED_SQL);
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

// --- the v6 → v7 device-sync schema migration (task 8, spec 8) ------------------------------

/**
 * The v6 → v7 migration DDL: the five device-sync tables of spec 8 plus
 * the zeroed state singleton and the schema bookkeeping bump. The
 * destination version is pinned to `7` here (NOT interpolated from
 * {@link JOURNAL_SCHEMA_VERSION}) so a future v7 → v8 migration can layer
 * on top of this block without rewriting this DDL.
 */
const DEVICE_SYNC_MIGRATION_DDL = [
  DEVICE_SYNC_STATE_DDL,
  MANIFEST_PAGE_PROGRESS_DDL,
  MANIFEST_ACTION_PROGRESS_DDL,
  REMOTE_APPLY_OPERATIONS_DDL,
  ECHO_MARKERS_DDL,
  DEVICE_SYNC_STATE_SEED_SQL,
  "update journal_meta set schema_version = 7 where singleton_key = 1;",
  "pragma user_version = 7;",
].join("");

function readUserVersionOf(engine: SqliteDatabaseEngine): number {
  const result = engine.exec("pragma user_version;");
  const value = result[0]?.values[0]?.[0];
  return typeof value === "number" ? value : Number.NaN;
}

/**
 * Whether one candidate v6 image carries the full journal surface the
 * migration builds on: the meta row and every v6 table (content, attempt
 * and lifecycle operands). A missing table is image corruption the
 * migration must fail closed on, never a silent partial upgrade.
 */
function restoreReservationJournalImageLooksValid(engine: SqliteDatabaseEngine): boolean {
  try {
    const metaRows = engine.exec(
      "select schema_version from journal_meta where singleton_key = 1;",
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
 * Migrate one restore-reservation (`pragma user_version = 6`) journal image
 * to the v7 device-sync schema in memory (task 8, spec 8). The migration
 * creates the five reconciliation tables, seeds the zeroed state singleton
 * and stamps the schema bookkeeping; every existing v6 row — file mapping,
 * journal event, attempt, lifecycle operand, tombstone and restore
 * reservation — survives untouched, and a pre-existing
 * `is_reconcile_required = 1` stays set. The input image is never mutated;
 * the function returns the upgraded image and runs the DDL inside one
 * `begin immediate ... commit` transaction so a torn migration leaves the
 * original image intact.
 */
export function migrateRestoreReservationJournalToDeviceSyncSchema(
  engineModule: SqliteEngineModule,
  image: ArrayLike<number>,
): Uint8Array {
  let engine: SqliteDatabaseEngine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    // Bytes that are not a SQLite image at all never execute a statement.
    throw journalStoreError("journal_image_invalid");
  }
  try {
    const currentVersion = readUserVersionOf(engine);
    if (currentVersion !== RESTORE_RESERVATION_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!restoreReservationJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(DEVICE_SYNC_MIGRATION_DDL);
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
  } catch (error) {
    // sql.js surfaces lazy open failures ("file is not a database") at the
    // first statement, not at construction: those bytes are not a journal
    // image. Closed store reasons pass through untouched.
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}

// --- the v7 → v8 multipart progress schema migration (task 9, child 7 spec 4.1) ---------------

/**
 * The v7 → v8 migration DDL: the `multipart_upload_progress` table of
 * child 7 spec 4.1 plus the schema bookkeeping bump. The destination
 * version is pinned to `8` here (NOT interpolated from
 * {@link JOURNAL_SCHEMA_VERSION}) so a future v8 → v9 migration can layer
 * on top of this block without rewriting this DDL.
 */
const MULTIPART_PROGRESS_MIGRATION_DDL = [
  MULTIPART_UPLOAD_PROGRESS_DDL,
  "update journal_meta set schema_version = 8 where singleton_key = 1;",
  "pragma user_version = 8;",
].join("");

/**
 * Whether one candidate v7 image carries the full journal surface the
 * migration builds on: the meta row, every pre-v7 table and all five
 * device-sync tables of spec 8. A missing table is image corruption the
 * migration must fail closed on, never a silent partial upgrade.
 */
function deviceSyncJournalImageLooksValid(engine: SqliteDatabaseEngine): boolean {
  try {
    const metaRows = engine.exec(
      "select schema_version from journal_meta where singleton_key = 1;",
    );
    if (metaRows[0]?.values[0] === undefined) {
      return false;
    }
    const tables = engine.exec(
      "select name from sqlite_master where type = 'table' order by name;",
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => row[0]);
    const required = [
      "device_sync_state",
      "echo_markers",
      "journal_attempts",
      "journal_events",
      "journal_meta",
      "lifecycle_event_operands",
      "local_files",
      "manifest_action_progress",
      "manifest_page_progress",
      "remote_apply_operations",
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
 * Migrate one device-sync (`pragma user_version = 7`) journal image to the
 * v8 multipart progress schema in memory (task 9, child 7 spec 4.1). The
 * migration creates the per-event safe progress table and stamps the
 * schema bookkeeping; every existing v7 row — file mapping, journal event,
 * attempt, lifecycle operand, restore reservation, cursor singleton,
 * barrier, manifest progress, remote apply and echo marker — survives
 * untouched, and the new table starts empty. The input image is never
 * mutated; the function returns the upgraded image and runs the DDL inside
 * one `begin immediate ... commit` transaction so a torn migration leaves
 * the original image intact.
 */
export function migrateDeviceSyncJournalToMultipartProgressSchema(
  engineModule: SqliteEngineModule,
  image: ArrayLike<number>,
): Uint8Array {
  let engine: SqliteDatabaseEngine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    // Bytes that are not a SQLite image at all never execute a statement.
    throw journalStoreError("journal_image_invalid");
  }
  try {
    const currentVersion = readUserVersionOf(engine);
    if (currentVersion !== DEVICE_SYNC_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!deviceSyncJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(MULTIPART_PROGRESS_MIGRATION_DDL);
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
  } catch (error) {
    // sql.js surfaces lazy open failures ("file is not a database") at the
    // first statement, not at construction: those bytes are not a journal
    // image. Closed store reasons pass through untouched.
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}

// --- the v8 → v9 conflict local repair schema migration (task 7, Child 8 spec 5.2.6/6) ----------

/**
 * The v8 → v9 migration DDL: the `conflict_local_repairs` table of
 * Child 8 spec 5.2.6/6 plus the schema bookkeeping bump. The
 * destination version is pinned to `9` here (NOT interpolated from
 * {@link JOURNAL_SCHEMA_VERSION}) so a future v9 → v10 migration can
 * layer on top of this block without rewriting this DDL.
 */
const CONFLICT_LOCAL_REPAIR_MIGRATION_DDL = [
  CONFLICT_LOCAL_REPAIRS_DDL,
  "update journal_meta set schema_version = 9 where singleton_key = 1;",
  "pragma user_version = 9;",
].join("");

/**
 * Whether one candidate v8 image carries the full journal surface the
 * migration builds on: the meta row, every pre-v7 table, all five
 * device-sync tables of spec 8 and the multipart progress table of
 * child 7 spec 4.1. A missing table is image corruption the migration
 * must fail closed on, never a silent partial upgrade.
 */
function multipartProgressJournalImageLooksValid(engine: SqliteDatabaseEngine): boolean {
  try {
    const metaRows = engine.exec(
      "select schema_version from journal_meta where singleton_key = 1;",
    );
    if (metaRows[0]?.values[0] === undefined) {
      return false;
    }
    const tables = engine.exec(
      "select name from sqlite_master where type = 'table' order by name;",
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => row[0]);
    const required = [
      "device_sync_state",
      "echo_markers",
      "journal_attempts",
      "journal_events",
      "journal_meta",
      "lifecycle_event_operands",
      "local_files",
      "manifest_action_progress",
      "manifest_page_progress",
      "multipart_upload_progress",
      "remote_apply_operations",
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
 * Migrate one multipart-progress (`pragma user_version = 8`) journal
 * image to the v9 conflict local repair schema in memory (task 7, Child
 * 8 spec 5.2.6/6). The migration creates the no-byte repair table and
 * stamps the schema bookkeeping; every existing v8 row — file mapping,
 * journal event, attempt, lifecycle operand, restore reservation,
 * cursor singleton, barrier, manifest progress, remote apply, echo
 * marker and multipart progress — survives untouched, and the new table
 * starts empty. The input image is never mutated; the function returns
 * the upgraded image and runs the DDL inside one
 * `begin immediate ... commit` transaction so a torn migration leaves
 * the original image intact.
 */
export function migrateMultipartProgressJournalToConflictRepairSchema(
  engineModule: SqliteEngineModule,
  image: ArrayLike<number>,
): Uint8Array {
  let engine: SqliteDatabaseEngine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    // Bytes that are not a SQLite image at all never execute a statement.
    throw journalStoreError("journal_image_invalid");
  }
  try {
    const currentVersion = readUserVersionOf(engine);
    if (currentVersion !== MULTIPART_PROGRESS_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!multipartProgressJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(CONFLICT_LOCAL_REPAIR_MIGRATION_DDL);
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
  } catch (error) {
    // sql.js surfaces lazy open failures ("file is not a database") at the
    // first statement, not at construction: those bytes are not a journal
    // image. Closed store reasons pass through untouched.
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}

// --- the v9 -> v10 pending rename intent schema migration ---------------------------------------

const PENDING_RENAME_INTENT_MIGRATION_DDL = [
  PENDING_RENAME_INTENTS_DDL,
  PENDING_RENAME_INTENT_MISSING_FILE_DEFERRALS_DDL,
  "update journal_meta set schema_version = 10 where singleton_key = 1;",
  "pragma user_version = 10;",
].join("");

/** Whether a candidate v9 image contains the complete conflict-repair surface. */
function conflictRepairJournalImageLooksValid(engine: SqliteDatabaseEngine): boolean {
  try {
    const metaRows = engine.exec(
      "select schema_version from journal_meta where singleton_key = 1;",
    );
    if (metaRows[0]?.values[0]?.[0] !== CONFLICT_REPAIR_SCHEMA_VERSION) {
      return false;
    }
    const tables = engine.exec(
      "select name from sqlite_master where type = 'table' order by name;",
    );
    const tableNames = new Set((tables[0]?.values ?? []).map((row) => String(row[0])));
    const required = [
      "conflict_local_repairs",
      "device_sync_state",
      "echo_markers",
      "journal_attempts",
      "journal_events",
      "journal_meta",
      "lifecycle_event_operands",
      "local_files",
      "manifest_action_progress",
      "manifest_page_progress",
      "multipart_upload_progress",
      "remote_apply_operations",
    ];
    return required.every((name) => tableNames.has(name));
  } catch {
    return false;
  }
}

/**
 * Losslessly migrate one complete v9 conflict-repair image to schema v10.
 * The input bytes are never mutated; both pending-rename tables begin empty
 * and both schema-version stamps change last in the same transaction.
 */
export function migrateConflictRepairJournalToPendingRenameIntentSchema(
  engineModule: SqliteEngineModule,
  image: ArrayLike<number>,
): Uint8Array {
  let engine: SqliteDatabaseEngine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    throw journalStoreError("journal_image_invalid");
  }
  try {
    if (readUserVersionOf(engine) !== CONFLICT_REPAIR_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!conflictRepairJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(PENDING_RENAME_INTENT_MIGRATION_DDL);
      engine.exec("commit;");
    } catch (error) {
      try {
        engine.exec("rollback;");
      } catch {
        // The closed mutation failure below is authoritative.
      }
      throw error instanceof JournalStoreError
        ? error
        : journalStoreError("journal_mutation_failed");
    }
    return engine.export();
  } catch (error) {
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}

/**
 * Guarded test-only v10 -> v9 downgrade. Production loading is forward-only;
 * this helper exists solely to prove the empty downgrade/upgrade contract.
 * Either pending-rename table being non-empty refuses before any mutation.
 */
export function downgradePendingRenameIntentJournalToConflictRepairSchemaForTest(
  engineModule: SqliteEngineModule,
  image: ArrayLike<number>,
): Uint8Array {
  let engine: SqliteDatabaseEngine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    throw journalStoreError("journal_image_invalid");
  }
  try {
    if (readUserVersionOf(engine) !== JOURNAL_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    const intentCount = engine.exec("select count(*) from pending_rename_intents;")[0]
      ?.values[0]?.[0];
    const deferralCount = engine.exec(
      "select count(*) from pending_rename_intent_missing_file_deferrals;",
    )[0]?.values[0]?.[0];
    if (intentCount !== 0 || deferralCount !== 0) {
      throw journalStoreError("journal_mutation_failed");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec("drop table pending_rename_intent_missing_file_deferrals;");
      engine.exec("drop index pending_rename_intents_current_path_uq;");
      engine.exec("drop table pending_rename_intents;");
      engine.exec(
        `update journal_meta set schema_version = ${CONFLICT_REPAIR_SCHEMA_VERSION} where singleton_key = 1;`,
      );
      engine.exec(`pragma user_version = ${CONFLICT_REPAIR_SCHEMA_VERSION};`);
      engine.exec("commit;");
    } catch (error) {
      try {
        engine.exec("rollback;");
      } catch {
        // The closed mutation failure below is authoritative.
      }
      throw error instanceof JournalStoreError
        ? error
        : journalStoreError("journal_mutation_failed");
    }
    return engine.export();
  } catch (error) {
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}
