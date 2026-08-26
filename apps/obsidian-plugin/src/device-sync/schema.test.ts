/**
 * Tests of the device-sync journal schema surface: the lossless v6 → v7
 * migration and the zeroed reconciliation state singleton (device cursor and
 * manifest reconciliation, task 8, spec 8).
 *
 * Every test builds a real v6 image with the vendored sql.js engine — a
 * fully seeded restore-reservation journal (local file with tombstone and
 * restore reservation, committed lifecycle delete event, keyed operand row,
 * bounded attempt) — then requires the migration to preserve every row,
 * stamp schema v7, seed zero cursors and leave no barrier/apply/echo
 * marker. Foreign source versions and non-journal bytes fail closed.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import { migrateRestoreReservationJournalToDeviceSyncSchema, SqliteDatabase } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import { readDeviceSyncState } from "./schema";

/** The real sql.js WebAssembly engine drives every migration test (spec 6.1). */
let engineModule: SqliteEngineModule;

beforeAll(async () => {
  const wasmBytes = new Uint8Array(
    readFileSync(new URL("../../node_modules/sql.js/dist/sql-wasm.wasm", import.meta.url)),
  );
  const wasmBinary = wasmBytes.buffer.slice(
    wasmBytes.byteOffset,
    wasmBytes.byteOffset + wasmBytes.byteLength,
  ) as ArrayBuffer;
  engineModule = await initSqlJs({ wasmBinary });
});

const LOCAL_FILE_ID = "11111111-1111-4111-8111-111111111111";
const SOURCE_ID = "22222222-2222-4222-8222-222222222222";
const BASE_VERSION_ID = "33333333-3333-4333-8333-333333333333";
const TOMBSTONE_ID = "44444444-4444-4444-8444-444444444444";
const EVENT_ID = "55555555-5555-4555-8555-555555555555";
const IDEMPOTENCY_KEY = "66666666-6666-4666-8666-666666666666";

/**
 * Build one fully seeded v6 restore-reservation journal image: the v7 empty
 * journal is downgraded with a raw engine (device-sync tables dropped,
 * bookkeeping pinned to 6) and then seeded with one tombstoned local file
 * holding an open restore reservation, one committed lifecycle delete event,
 * its keyed operand row and one bounded attempt.
 */
function createSeededV6Image(options: { isReconcileRequired: boolean }): Uint8Array {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: 7,
    dirtyGeneration: 4,
    lastVerifiedGeneration: 4,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  const v7Image = database.exportImage();
  database.close();
  const engine = new engineModule.Database(v7Image);
  try {
    engine.exec("begin immediate;");
    engine.exec("drop table device_sync_state;");
    engine.exec("drop table manifest_page_progress;");
    engine.exec("drop table manifest_action_progress;");
    engine.exec("drop table remote_apply_operations;");
    engine.exec("drop table echo_markers;");
    engine.exec(
      `update journal_meta set schema_version = 6, is_reconcile_required = ${options.isReconcileRequired ? 1 : 0} where singleton_key = 1;`,
    );
    engine.exec("pragma user_version = 6;");
    engine.exec(
      [
        "insert into local_files (local_file_id, normalized_path, source_id,",
        "observed_sha256, observed_size_bytes, observed_media_type, base_version_id,",
        "policy_revision, last_locator, open_tombstone_id, lifecycle_state,",
        "last_committed_sha256, last_committed_size_bytes, last_committed_media_type,",
        `restore_prior_path) values ('${LOCAL_FILE_ID}', 'notes/kept.md', '${SOURCE_ID}',`,
        `'${"a".repeat(64)}', 120, 'text/markdown', '${BASE_VERSION_ID}', 4, 'notes/kept.md',`,
        `'${TOMBSTONE_ID}', 'restore_pending', '${"b".repeat(64)}', 120, 'text/markdown',`,
        "'notes/kept-prior.md');",
      ].join(" "),
    );
    engine.exec(
      [
        "insert into journal_events (event_id, local_file_id, idempotency_key, operation,",
        "sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count,",
        `next_eligible_retry_epoch_ms, safe_error, operation_id, created_at_epoch_ms)`,
        `values ('${EVENT_ID}', '${LOCAL_FILE_ID}', '${IDEMPOTENCY_KEY}', 'delete', '${"c".repeat(64)}',`,
        "0, 'application/octet-stream', 'committed', 1, 2, null, null, null, 1784000000000);",
      ].join(" "),
    );
    engine.exec(
      [
        "insert into lifecycle_event_operands (event_id, source_id, expected_version_id,",
        "expected_locator, target_locator, tombstone_id, policy_revision,",
        "predecessor_event_id, server_receipt_tombstone_id) values",
        `('${EVENT_ID}', '${SOURCE_ID}', '${BASE_VERSION_ID}', 'notes/kept.md', null,`,
        `'${TOMBSTONE_ID}', 4, null, '${TOMBSTONE_ID}');`,
      ].join(" "),
    );
    engine.exec(
      [
        "insert into journal_attempts (event_id, attempted_at_epoch_ms, outcome_label,",
        `request_correlation_id) values ('${EVENT_ID}', 1784000000001, 'committed', 'req-1');`,
      ].join(" "),
    );
    engine.exec("commit;");
    return engine.export();
  } finally {
    engine.close();
  }
}

/** Read one scalar of one raw image with a throwaway engine. */
function readRawScalar(image: Uint8Array, sql: string): unknown {
  const engine = new engineModule.Database(image);
  try {
    return engine.exec(sql)[0]?.values[0]?.[0];
  } finally {
    engine.close();
  }
}

// --- the v6 → v7 migration (task 8 step 1, spec 8) ---------------------------------------------------------

describe("device sync schema migration (v6 to v7)", () => {
  it("migrates v6 to v7 without clearing reconcile_required", () => {
    const v6Image = createSeededV6Image({ isReconcileRequired: true });
    const v7Image = migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, v6Image);
    const database = SqliteDatabase.openFromImage(engineModule, v7Image);
    try {
      expect(database.readSchemaVersion()).toBe(7);
      expect(readDeviceSyncState(database)).toEqual({
        appliedSequence: 0,
        acknowledgedSequence: 0,
        observationGeneration: 0,
        barrierGeneration: null,
        barrierReason: null,
        activeManifestRunId: null,
        manifestCheckpointSequence: null,
        manifestFinalDigest: null,
      });
      expect(database.readJournalMeta().isReconcileRequired).toBe(true);
      expect(database.readJournalMeta().schemaVersion).toBe(7);
    } finally {
      database.close();
    }
  });

  it("preserves every v6 local file, journal event, attempt, lifecycle operand and restore reservation", () => {
    const v6Image = createSeededV6Image({ isReconcileRequired: false });
    const v7Image = migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, v6Image);
    const database = SqliteDatabase.openFromImage(engineModule, v7Image);
    try {
      const localFileRow = database.readAll(
        [
          "select normalized_path, source_id, lifecycle_state, open_tombstone_id,",
          "restore_prior_path, last_committed_sha256 from local_files",
          `where local_file_id = '${LOCAL_FILE_ID}';`,
        ].join(" "),
      )[0]?.values[0];
      expect(localFileRow).toEqual([
        "notes/kept.md",
        SOURCE_ID,
        "restore_pending",
        TOMBSTONE_ID,
        "notes/kept-prior.md",
        "b".repeat(64),
      ]);

      const eventRow = database.readAll(
        [
          "select operation, state, attempt_count, is_fingerprint_frozen from journal_events",
          `where event_id = '${EVENT_ID}';`,
        ].join(" "),
      )[0]?.values[0];
      expect(eventRow).toEqual(["delete", "committed", 2, 1]);

      const operandRow = database.readAll(
        [
          "select source_id, expected_version_id, tombstone_id, server_receipt_tombstone_id",
          `from lifecycle_event_operands where event_id = '${EVENT_ID}';`,
        ].join(" "),
      )[0]?.values[0];
      expect(operandRow).toEqual([SOURCE_ID, BASE_VERSION_ID, TOMBSTONE_ID, TOMBSTONE_ID]);

      const attemptCount = database.readAll(
        `select count(*) from journal_attempts where event_id = '${EVENT_ID}';`,
      )[0]?.values[0]?.[0];
      expect(attemptCount).toBe(1);
    } finally {
      database.close();
    }
  });

  it("creates no barrier, manifest, remote apply or echo marker row", () => {
    const v6Image = createSeededV6Image({ isReconcileRequired: true });
    const v7Image = migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, v6Image);
    const database = SqliteDatabase.openFromImage(engineModule, v7Image);
    try {
      for (const table of [
        "manifest_page_progress",
        "manifest_action_progress",
        "remote_apply_operations",
        "echo_markers",
      ]) {
        const count = database.readAll(`select count(*) from ${table};`)[0]?.values[0]?.[0];
        expect(count, table).toBe(0);
      }
      const stateRows = database.readAll(
        "select count(*) from device_sync_state;",
      )[0]?.values[0]?.[0];
      expect(stateRows).toBe(1);
    } finally {
      database.close();
    }
  });

  it("rejects a v5 image as an unsupported migration source", () => {
    const v6Image = createSeededV6Image({ isReconcileRequired: false });
    const engine = new engineModule.Database(v6Image);
    engine.exec("pragma user_version = 5; update journal_meta set schema_version = 5 where singleton_key = 1;");
    const v5Image = engine.export();
    engine.close();

    expect(() => migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, v5Image)).toThrowError(
      expect.objectContaining({ reason: "journal_schema_unsupported" }),
    );
  });

  it("rejects an already-v7 image as a migration source", () => {
    const database = SqliteDatabase.createEmpty(engineModule, {
      schemaVersion: 7,
      dirtyGeneration: 1,
      lastVerifiedGeneration: 1,
      isReconcileRequired: false,
      recoveryState: "fresh_journal_created",
    });
    const v7Image = database.exportImage();
    database.close();

    expect(() => migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, v7Image)).toThrowError(
      expect.objectContaining({ reason: "journal_schema_unsupported" }),
    );
  });

  it("rejects bytes that are not a SQLite journal image", () => {
    const garbage = new TextEncoder().encode("definitely not a sqlite image");
    expect(() => migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, garbage)).toThrowError(
      expect.objectContaining({ reason: "journal_image_invalid" }),
    );
  });

  it("never mutates the input image", () => {
    const v6Image = createSeededV6Image({ isReconcileRequired: true });
    const first = migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, v6Image);
    const second = migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, v6Image);
    expect(readRawScalar(v6Image, "pragma user_version;")).toBe(6);
    expect(first).toEqual(second);
  });
});

// --- the fresh v7 state singleton ------------------------------------------------------------------------

describe("device sync state singleton (fresh v7 journal)", () => {
  it("seeds a zeroed reconciliation state row on a fresh v7 journal", () => {
    const database = SqliteDatabase.createEmpty(engineModule, {
      schemaVersion: 7,
      dirtyGeneration: 1,
      lastVerifiedGeneration: 1,
      isReconcileRequired: false,
      recoveryState: "fresh_journal_created",
    });
    try {
      expect(readDeviceSyncState(database)).toEqual({
        appliedSequence: 0,
        acknowledgedSequence: 0,
        observationGeneration: 0,
        barrierGeneration: null,
        barrierReason: null,
        activeManifestRunId: null,
        manifestCheckpointSequence: null,
        manifestFinalDigest: null,
      });
    } finally {
      database.close();
    }
  });

  it("fails closed when the singleton row is missing", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, {
      schemaVersion: 7,
      dirtyGeneration: 1,
      lastVerifiedGeneration: 1,
      isReconcileRequired: false,
      recoveryState: "fresh_journal_created",
    });
    try {
      await database.runSerializedMutation((session) => {
        session.exec("delete from device_sync_state;");
      });
      expect(() => readDeviceSyncState(database)).toThrowError(
        expect.objectContaining({ reason: "journal_image_invalid" }),
      );
    } finally {
      database.close();
    }
  });

  it("fails closed when the singleton carries a foreign barrier reason", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, {
      schemaVersion: 7,
      dirtyGeneration: 1,
      lastVerifiedGeneration: 1,
      isReconcileRequired: false,
      recoveryState: "fresh_journal_created",
    });
    try {
      await database.runSerializedMutation((session) => {
        session.exec(
          "update device_sync_state set barrier_generation = 0, barrier_reason = 'made_up_reason';",
        );
      });
      expect(() => readDeviceSyncState(database)).toThrowError(
        expect.objectContaining({ reason: "journal_image_invalid" }),
      );
    } finally {
      database.close();
    }
  });
});

// --- structural engine slice ------------------------------------------------------------------------------

describe("device sync schema reader slice", () => {
  it("reads through the structural readAll seam without a concrete SqliteDatabase", () => {
    const database = SqliteDatabase.createEmpty(engineModule, {
      schemaVersion: 7,
      dirtyGeneration: 1,
      lastVerifiedGeneration: 1,
      isReconcileRequired: false,
      recoveryState: "fresh_journal_created",
    });
    try {
      const reader: { readAll(sql: string): { columns: readonly string[]; values: readonly unknown[][] }[] } = {
        readAll: (sql) => database.readAll(sql) as unknown as { columns: readonly string[]; values: readonly unknown[][] }[],
      };
      expect(readDeviceSyncState(reader).appliedSequence).toBe(0);
    } finally {
      database.close();
    }
  });
});
