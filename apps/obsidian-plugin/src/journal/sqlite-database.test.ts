import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { JournalMeta } from "./contracts";
import {
  DEVICE_SYNC_SCHEMA_VERSION,
  JOURNAL_SCHEMA_VERSION,
  JournalStoreError,
  SqliteDatabase,
  migrateDeviceSyncJournalToMultipartProgressSchema,
  migrateMultipartProgressJournalToConflictRepairSchema,
} from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";

/**
 * The real sql.js WebAssembly engine drives every test here (spec 6.1): the
 * test reads the vendored wasm binary from node_modules because test files
 * may use Node, while plugin source never may.
 */
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

function createJournalMeta(overrides: Partial<JournalMeta> = {}): JournalMeta {
  return {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 0,
    lastVerifiedGeneration: 0,
    isReconcileRequired: false,
    recoveryState: "fresh_journal_created",
    ...overrides,
  };
}

describe("SqliteDatabase empty first open (spec 6.2, 6.3)", () => {
  it("creates the journal_meta schema and seeds the given meta row", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());

    expect(database.readSchemaVersion()).toBe(JOURNAL_SCHEMA_VERSION);
    expect(database.readJournalMeta()).toEqual(createJournalMeta());

    const metaRows = database.readAll("select count(*) as row_count from journal_meta");
    expect(metaRows[0]?.values[0]?.[0]).toBe(1);
    database.close();
  });

  it("keeps exactly one journal_meta row enforced by the singleton key", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());

    await expect(
      database.runSerializedMutation((session) => {
        session.exec("insert into journal_meta (singleton_key) values (2)");
      }),
    ).rejects.toMatchObject({
      name: "JournalStoreError",
      reason: "journal_mutation_failed",
    });
    database.close();
  });

  it("exports an image that reopens with the persisted meta intact", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
    await database.runSerializedMutation((session) => {
      session.writeJournalMeta(createJournalMeta({ dirtyGeneration: 4 }));
    });

    const exported = database.exportImage();
    database.close();

    const reopened = SqliteDatabase.openFromImage(engineModule, exported);
    expect(reopened.readJournalMeta()).toEqual(createJournalMeta({ dirtyGeneration: 4 }));
    expect(reopened.readSchemaVersion()).toBe(JOURNAL_SCHEMA_VERSION);
    reopened.close();
  });
});

describe("SqliteDatabase schema migration bookkeeping (spec 6.3)", () => {
  it("rejects an image stamped with a newer schema version", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
    const rawFutureEngine = new engineModule.Database(database.exportImage());
    rawFutureEngine.exec(`pragma user_version = ${JOURNAL_SCHEMA_VERSION + 1}`);
    const futureImage = rawFutureEngine.export();
    rawFutureEngine.close();
    database.close();

    expect(() => SqliteDatabase.openFromImage(engineModule, futureImage)).toThrow(
      JournalStoreError,
    );
    expect(() => SqliteDatabase.openFromImage(engineModule, futureImage)).toThrowError(
      expect.objectContaining({ reason: "journal_schema_unsupported" }),
    );
  });

  it("rejects an older schema version as unsupported, distinct from a non-journal image", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
    const rawOlderEngine = new engineModule.Database(database.exportImage());
    rawOlderEngine.exec(`pragma user_version = ${JOURNAL_SCHEMA_VERSION - 1}`);
    const olderImage = rawOlderEngine.export();
    rawOlderEngine.close();
    database.close();

    // An older journal lineage is a schema problem, never conflated with
    // bytes that are not a journal image at all.
    expect(() => SqliteDatabase.openFromImage(engineModule, olderImage)).toThrow(
      JournalStoreError,
    );
    expect(() => SqliteDatabase.openFromImage(engineModule, olderImage)).toThrowError(
      expect.objectContaining({ reason: "journal_schema_unsupported" }),
    );
  });

  it("rejects bytes that are not a SQLite journal image", () => {
    const garbage = new TextEncoder().encode("definitely not a sqlite image");
    expect(() => SqliteDatabase.openFromImage(engineModule, garbage)).toThrowError(
      expect.objectContaining({ reason: "journal_image_invalid" }),
    );
  });
});

describe("SqliteDatabase one serialized writer (spec 6.1)", () => {
  it("runs concurrent mutations strictly one at a time in submission order", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
    const events: string[] = [];
    let inFlightCount = 0;
    let maxInFlightCount = 0;

    const commits = Array.from({ length: 6 }, (_, index) =>
      database.runSerializedMutation(async (session) => {
        inFlightCount += 1;
        maxInFlightCount = Math.max(maxInFlightCount, inFlightCount);
        // Awaits inside the mutation must never let another mutation start.
        await Promise.resolve();
        events.push(`begin-${index}`);
        session.writeJournalMeta(
          createJournalMeta({ dirtyGeneration: session.readJournalMeta().dirtyGeneration + 1 }),
        );
        await Promise.resolve();
        events.push(`end-${index}`);
        inFlightCount -= 1;
        return index;
      }),
    );

    const results = await Promise.all(commits);
    expect(results).toEqual([0, 1, 2, 3, 4, 5]);
    expect(maxInFlightCount).toBe(1);
    expect(events).toEqual(
      Array.from({ length: 6 }, (_, index) => [`begin-${index}`, `end-${index}`]).flat(),
    );
    expect(database.readJournalMeta().dirtyGeneration).toBe(6);
    database.close();
  });

  it("keeps the writer usable and rolls back when a mutation throws", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta({ dirtyGeneration: 5 }));

    await expect(
      database.runSerializedMutation(async (session) => {
        session.writeJournalMeta(createJournalMeta({ dirtyGeneration: 99 }));
        throw new Error("boom with library detail");
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    expect(database.readJournalMeta().dirtyGeneration).toBe(5);

    await database.runSerializedMutation((session) => {
      session.writeJournalMeta(createJournalMeta({ dirtyGeneration: 6 }));
    });
    expect(database.readJournalMeta().dirtyGeneration).toBe(6);
    database.close();
  });

  it("never leaks the original failure detail through the closed reason", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());

    const failure = database.runSerializedMutation(() => {
      throw new Error("boom with library detail");
    });
    await expect(failure).rejects.toBeInstanceOf(JournalStoreError);
    await failure.catch((error: unknown) => {
      expect(String(error)).not.toContain("boom");
    });
    database.close();
  });
});

describe("SqliteDatabase closed meta validation (spec 6.3, 9)", () => {
  it("rejects a meta write with a state outside the closed recovery set", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());

    await expect(
      database.runSerializedMutation((session) => {
        session.writeJournalMeta(
          createJournalMeta({ recoveryState: "corrupt" as JournalMeta["recoveryState"] }),
        );
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(database.readJournalMeta().recoveryState).toBe("fresh_journal_created");
    database.close();
  });
});

describe("SqliteDatabase device-sync schema v7 (task 8, spec 8)", () => {
  it("creates the five device-sync tables and seeds the zeroed state singleton", () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());

    const tables = database.readAll(
      "select name from sqlite_master where type = 'table' order by name;",
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => String(row[0]));
    for (const table of [
      "device_sync_state",
      "manifest_page_progress",
      "manifest_action_progress",
      "remote_apply_operations",
      "echo_markers",
    ]) {
      expect(tableNames).toContain(table);
    }

    const stateRow = database.readAll(
      [
        "select applied_sequence, acknowledged_sequence, observation_generation,",
        "barrier_generation, barrier_reason, active_manifest_run_id,",
        "manifest_checkpoint_sequence, manifest_final_digest from device_sync_state",
        "where singleton_key = 1;",
      ].join(" "),
    )[0]?.values[0];
    expect(stateRow).toEqual([0, 0, 0, null, null, null, null, null]);
    database.close();
  });

  it("keeps the device-sync state row single", async () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());

    await expect(
      database.runSerializedMutation((session) => {
        session.exec("insert into device_sync_state (singleton_key) values (2)");
      }),
    ).rejects.toMatchObject({
      name: "JournalStoreError",
      reason: "journal_mutation_failed",
    });
    database.close();
  });
});

// --- conflict local repair schema v9 (task 7, Child 8 spec 5.2.6/6) -------------------------------

describe("SqliteDatabase conflict local repair schema v9 (task 7, Child 8 spec 6)", () => {
  it("creates the empty conflict_local_repairs table on a fresh journal", () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
    expect(database.readSchemaVersion()).toBe(JOURNAL_SCHEMA_VERSION);

    const tables = database.readAll(
      "select name from sqlite_master where type = 'table' order by name;",
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => String(row[0]));
    expect(tableNames).toContain("conflict_local_repairs");

    const repairRowCount = database.readAll(
      "select count(*) from conflict_local_repairs;",
    )[0]?.values[0]?.[0];
    expect(repairRowCount).toBe(0);
    database.close();
  });
});

describe("conflict local repair schema migration (v8 to v9)", () => {
  const LOCAL_FILE_ID = "12121212-1212-4121-8121-121212121212";
  const EVENT_ID = "56565656-5656-4565-8565-565656565656";
  const IDEMPOTENCY_KEY = "67676767-6767-4676-8676-676767676767";
  const SESSION_ID = "s".repeat(40);
  const CONFLICT_ID = "13131313-1313-4131-8131-131313131313";
  const RESOLUTION_EVENT_ID = "58585858-5858-4585-8585-585858585858";

  /**
   * Build one seeded v8 journal image: the current empty journal is
   * downgraded with a raw engine (the conflict repair table dropped,
   * bookkeeping pinned to 8) and then seeded with one tracked local file,
   * one committed content event and one multipart progress row — the
   * durable state a real v8 device carries across the upgrade.
   */
  function createSeededV8Image(): Uint8Array {
    const database = SqliteDatabase.createEmpty(
      engineModule,
      createJournalMeta({
        dirtyGeneration: 6,
        lastVerifiedGeneration: 6,
        recoveryState: "verified_generation_loaded",
      }),
    );
    const v9Image = database.exportImage();
    database.close();
    const engine = new engineModule.Database(v9Image);
    try {
      engine.exec("begin immediate;");
      engine.exec("drop table conflict_local_repairs;");
      engine.exec(
        "update journal_meta set schema_version = 8 where singleton_key = 1;",
      );
      engine.exec("pragma user_version = 8;");
      engine.exec(
        [
          "insert into local_files (local_file_id, normalized_path, source_id,",
          "observed_sha256, observed_size_bytes, observed_media_type, base_version_id,",
          `policy_revision) values ('${LOCAL_FILE_ID}', 'notes/asset.bin', null,`,
          `'${"b".repeat(64)}', 1024, 'application/octet-stream', null, 7);`,
        ].join(" "),
      );
      engine.exec(
        [
          "insert into journal_events (event_id, local_file_id, idempotency_key, operation,",
          "sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count,",
          `next_eligible_retry_epoch_ms, safe_error, operation_id, created_at_epoch_ms)`,
          `values ('${EVENT_ID}', '${LOCAL_FILE_ID}', '${IDEMPOTENCY_KEY}', 'update',`,
          `'${"b".repeat(64)}', 1024, 'application/octet-stream', 'committed', 1, 2,`,
          "null, null, null, 1784000000000);",
        ].join(" "),
      );
      engine.exec(
        [
          "insert into multipart_upload_progress (event_id, session_id, part_size_bytes,",
          "part_count, expires_at_epoch_ms, completed_part_numbers_json, session_state)",
          `values ('${EVENT_ID}', '${SESSION_ID}', 8388608, 1, 1784000900000, '[]', 'committed');`,
        ].join(" "),
      );
      engine.exec("commit;");
      return engine.export();
    } finally {
      engine.close();
    }
  }

  it("migrates v8 journal without storing candidate bytes or paths", () => {
    const v9Image = migrateMultipartProgressJournalToConflictRepairSchema(
      engineModule,
      createSeededV8Image(),
    );
    const database = SqliteDatabase.openFromImage(engineModule, v9Image);
    try {
      expect(database.readSchemaVersion()).toBe(9);
      // Seed one valid repair fact: sql.js answers a SELECT on an empty
      // table with no result set at all, so the column probe needs a row.
      database.readAll(
        [
          "insert into conflict_local_repairs (conflict_id, resolution_event_id,",
          "target_action, safe_reason, attempt_count, next_eligible_retry_epoch_ms,",
          "created_at_epoch_ms, updated_at_epoch_ms) values",
          `('${CONFLICT_ID}', '${RESOLUTION_EVENT_ID}', 'apply_remote_version',`,
          "'resolution_committed', 0, null, 1784000000000, 1784000000000);",
        ].join(" "),
      );
      expect(database.readAll("select * from conflict_local_repairs;")[0]?.columns).not.toContain(
        "bytes",
      );
    } finally {
      database.close();
    }
  });

  it("creates the repair table with exactly the no-byte fact columns", () => {
    const v9Image = migrateMultipartProgressJournalToConflictRepairSchema(
      engineModule,
      createSeededV8Image(),
    );
    const database = SqliteDatabase.openFromImage(engineModule, v9Image);
    try {
      const columns = (
        database.readAll("pragma table_info(conflict_local_repairs);")[0]?.values ?? []
      ).map((row) => String(row[1]));
      expect(columns).toEqual([
        "conflict_id",
        "resolution_event_id",
        "target_action",
        "safe_reason",
        "attempt_count",
        "next_eligible_retry_epoch_ms",
        "created_at_epoch_ms",
        "updated_at_epoch_ms",
      ]);
      for (const column of columns) {
        expect(column).not.toContain("bytes");
        expect(column).not.toContain("path");
      }
    } finally {
      database.close();
    }
  });

  it("migrates v8 to v9 preserving every v8 row unchanged and stamping v9", () => {
    const v9Image = migrateMultipartProgressJournalToConflictRepairSchema(
      engineModule,
      createSeededV8Image(),
    );
    const database = SqliteDatabase.openFromImage(engineModule, v9Image);
    try {
      expect(database.readSchemaVersion()).toBe(9);
      expect(database.readJournalMeta()).toEqual(
        createJournalMeta({
          schemaVersion: 9,
          dirtyGeneration: 6,
          lastVerifiedGeneration: 6,
          recoveryState: "verified_generation_loaded",
        }),
      );

      const localFileRow = database.readAll(
        ["select normalized_path, observed_sha256, observed_size_bytes, lifecycle_state", "from local_files", `where local_file_id = '${LOCAL_FILE_ID}';`].join(" "),
      )[0]?.values[0];
      expect(localFileRow).toEqual(["notes/asset.bin", "b".repeat(64), 1024, "active"]);

      const eventRow = database.readAll(
        ["select operation, state, attempt_count from journal_events", `where event_id = '${EVENT_ID}';`].join(" "),
      )[0]?.values[0];
      expect(eventRow).toEqual(["update", "committed", 2]);

      const progressRow = database.readAll(
        ["select session_id, session_state from multipart_upload_progress", `where event_id = '${EVENT_ID}';`].join(" "),
      )[0]?.values[0];
      expect(progressRow).toEqual([SESSION_ID, "committed"]);
    } finally {
      database.close();
    }
  });

  it("creates the repair table empty during the migration", () => {
    const v9Image = migrateMultipartProgressJournalToConflictRepairSchema(
      engineModule,
      createSeededV8Image(),
    );
    const database = SqliteDatabase.openFromImage(engineModule, v9Image);
    try {
      const repairRowCount = database.readAll(
        "select count(*) from conflict_local_repairs;",
      )[0]?.values[0]?.[0];
      expect(repairRowCount).toBe(0);
    } finally {
      database.close();
    }
  });

  it("rejects a v7 image as a migration source", () => {
    const v8Image = createSeededV8Image();
    const engine = new engineModule.Database(v8Image);
    engine.exec(
      "pragma user_version = 7; update journal_meta set schema_version = 7 where singleton_key = 1;",
    );
    const v7Image = engine.export();
    engine.close();

    expect(() =>
      migrateMultipartProgressJournalToConflictRepairSchema(engineModule, v7Image),
    ).toThrowError(expect.objectContaining({ reason: "journal_schema_unsupported" }));
  });

  it("rejects an already-v9 image as a migration source", () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
    const v9Image = database.exportImage();
    database.close();

    expect(() =>
      migrateMultipartProgressJournalToConflictRepairSchema(engineModule, v9Image),
    ).toThrowError(expect.objectContaining({ reason: "journal_schema_unsupported" }));
  });

  it("rejects a v8 image missing a required v8 table", () => {
    const v8Image = createSeededV8Image();
    const engine = new engineModule.Database(v8Image);
    engine.exec("drop table multipart_upload_progress;");
    const tornImage = engine.export();
    engine.close();

    expect(() =>
      migrateMultipartProgressJournalToConflictRepairSchema(engineModule, tornImage),
    ).toThrowError(expect.objectContaining({ reason: "journal_image_invalid" }));
  });

  it("rejects bytes that are not a SQLite journal image", () => {
    const garbage = new TextEncoder().encode("definitely not a sqlite image");
    expect(() =>
      migrateMultipartProgressJournalToConflictRepairSchema(engineModule, garbage),
    ).toThrowError(expect.objectContaining({ reason: "journal_image_invalid" }));
  });

  it("never mutates the input image", () => {
    const v8Image = createSeededV8Image();
    const first = migrateMultipartProgressJournalToConflictRepairSchema(engineModule, v8Image);
    const second = migrateMultipartProgressJournalToConflictRepairSchema(engineModule, v8Image);
    const probe = new engineModule.Database(v8Image);
    try {
      expect(probe.exec("pragma user_version;")[0]?.values[0]?.[0]).toBe(8);
    } finally {
      probe.close();
    }
    expect(first).toEqual(second);
  });
});

// --- multipart progress schema v8 (task 9, child 7 spec 4.1) --------------------------------------

describe("SqliteDatabase multipart progress schema v8 (task 9, spec 4.1)", () => {
  it("creates the empty multipart_upload_progress table on a fresh journal", () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
    expect(database.readSchemaVersion()).toBe(JOURNAL_SCHEMA_VERSION);

    const tables = database.readAll(
      "select name from sqlite_master where type = 'table' order by name;",
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => String(row[0]));
    expect(tableNames).toContain("multipart_upload_progress");

    const progressRowCount = database.readAll(
      "select count(*) from multipart_upload_progress;",
    )[0]?.values[0]?.[0];
    expect(progressRowCount).toBe(0);
    database.close();
  });
});

describe("multipart progress schema migration (v7 to v8)", () => {
  const LOCAL_FILE_ID = "11111111-1111-4111-8111-111111111111";
  const EVENT_ID = "55555555-5555-4555-8555-555555555555";
  const IDEMPOTENCY_KEY = "66666666-6666-4666-8666-666666666666";

  /**
   * Build one seeded v7 journal image: the current empty journal is
   * downgraded with a raw engine (the multipart progress table dropped,
   * bookkeeping pinned to 7) and then seeded with one tracked local file
   * and one in-flight uploading content event — the durable state a real
   * v7 device carries across the upgrade.
   */
  function createSeededV7Image(): Uint8Array {
    const database = SqliteDatabase.createEmpty(
      engineModule,
      createJournalMeta({
        dirtyGeneration: 4,
        lastVerifiedGeneration: 4,
        recoveryState: "verified_generation_loaded",
      }),
    );
    const v8Image = database.exportImage();
    database.close();
    const engine = new engineModule.Database(v8Image);
    try {
      engine.exec("begin immediate;");
      engine.exec("drop table multipart_upload_progress;");
      engine.exec(
        "update journal_meta set schema_version = 7 where singleton_key = 1;",
      );
      engine.exec("pragma user_version = 7;");
      engine.exec(
        [
          "insert into local_files (local_file_id, normalized_path, source_id,",
          "observed_sha256, observed_size_bytes, observed_media_type, base_version_id,",
          `policy_revision) values ('${LOCAL_FILE_ID}', 'notes/big-asset.bin', null,`,
          `'${"a".repeat(64)}', 20 * 1024 * 1024, 'application/octet-stream', null, 4);`,
        ].join(" "),
      );
      engine.exec(
        [
          "insert into journal_events (event_id, local_file_id, idempotency_key, operation,",
          "sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count,",
          `next_eligible_retry_epoch_ms, safe_error, operation_id, created_at_epoch_ms)`,
          `values ('${EVENT_ID}', '${LOCAL_FILE_ID}', '${IDEMPOTENCY_KEY}', 'create',`,
          `'${"a".repeat(64)}', 20 * 1024 * 1024, 'application/octet-stream', 'uploading', 1,`,
          "1, 1784000001000, null, 'operation-token', 1784000000000);",
        ].join(" "),
      );
      engine.exec("commit;");
      return engine.export();
    } finally {
      engine.close();
    }
  }

  /**
   * Read one mid-lineage image (the v8 result of the v7 → v8 migration is
   * no longer the version `SqliteDatabase.openFromImage` accepts since the
   * conflict-repair bump): the raw engine probes the bookkeeping and rows
   * directly without the current-version gate.
   */
  function readRawRows(image: Uint8Array, sql: string): readonly (readonly unknown[])[] {
    const engine = new engineModule.Database(image);
    try {
      return (engine.exec(sql)[0]?.values ?? []) as readonly (readonly unknown[])[];
    } finally {
      engine.close();
    }
  }

  it("migrates v7 to v8 preserving every v7 row unchanged and stamping v8", () => {
    const v7Image = createSeededV7Image();
    const v8Image = migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, v7Image);

    const versionRow = readRawRows(v8Image, "pragma user_version;");
    expect(versionRow[0]?.[0]).toBe(8);

    const metaRow = readRawRows(
      v8Image,
      [
        "select schema_version, dirty_generation, last_verified_generation,",
        "is_reconcile_required, recovery_state from journal_meta where singleton_key = 1;",
      ].join(" "),
    )[0];
    expect(metaRow).toEqual([8, 4, 4, 0, "verified_generation_loaded"]);

    const localFileRow = readRawRows(
      v8Image,
      [
        "select normalized_path, source_id, observed_sha256, observed_size_bytes,",
        "lifecycle_state from local_files",
        `where local_file_id = '${LOCAL_FILE_ID}';`,
      ].join(" "),
    )[0];
    expect(localFileRow).toEqual([
      "notes/big-asset.bin",
      null,
      "a".repeat(64),
      20 * 1024 * 1024,
      "active",
    ]);

    const eventRow = readRawRows(
      v8Image,
      [
        "select operation, state, attempt_count, is_fingerprint_frozen,",
        "next_eligible_retry_epoch_ms, operation_id from journal_events",
        `where event_id = '${EVENT_ID}';`,
      ].join(" "),
    )[0];
    expect(eventRow).toEqual(["create", "uploading", 1, 1, 1784000001000, "operation-token"]);

    // The device-sync singleton of v7 survives untouched.
    const stateRow = readRawRows(
      v8Image,
      [
        "select applied_sequence, acknowledged_sequence, observation_generation",
        "from device_sync_state where singleton_key = 1;",
      ].join(" "),
    )[0];
    expect(stateRow).toEqual([0, 0, 0]);
  });

  it("creates the multipart progress table empty during the migration", () => {
    const v8Image = migrateDeviceSyncJournalToMultipartProgressSchema(
      engineModule,
      createSeededV7Image(),
    );
    const progressRowCount = readRawRows(
      v8Image,
      "select count(*) from multipart_upload_progress;",
    )[0]?.[0];
    expect(progressRowCount).toBe(0);
  });

  it("rejects a v6 image as a migration source", () => {
    const v7Image = createSeededV7Image();
    const engine = new engineModule.Database(v7Image);
    engine.exec(
      "pragma user_version = 6; update journal_meta set schema_version = 6 where singleton_key = 1;",
    );
    const v6Image = engine.export();
    engine.close();

    expect(() =>
      migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, v6Image),
    ).toThrowError(expect.objectContaining({ reason: "journal_schema_unsupported" }));
  });

  it("rejects an already-v8 image as a migration source", () => {
    const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
    const v8Image = database.exportImage();
    database.close();

    expect(() =>
      migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, v8Image),
    ).toThrowError(expect.objectContaining({ reason: "journal_schema_unsupported" }));
  });

  it("rejects a v7 image missing a required v7 table", () => {
    const v7Image = createSeededV7Image();
    const engine = new engineModule.Database(v7Image);
    engine.exec("drop table journal_attempts;");
    const tornImage = engine.export();
    engine.close();

    expect(() =>
      migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, tornImage),
    ).toThrowError(expect.objectContaining({ reason: "journal_image_invalid" }));
  });

  it("rejects bytes that are not a SQLite journal image", () => {
    const garbage = new TextEncoder().encode("definitely not a sqlite image");
    expect(() =>
      migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, garbage),
    ).toThrowError(expect.objectContaining({ reason: "journal_image_invalid" }));
  });

  it("never mutates the input image", () => {
    const v7Image = createSeededV7Image();
    const first = migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, v7Image);
    const second = migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, v7Image);
    const probe = new engineModule.Database(v7Image);
    try {
      expect(probe.exec("pragma user_version;")[0]?.values[0]?.[0]).toBe(
        DEVICE_SYNC_SCHEMA_VERSION,
      );
    } finally {
      probe.close();
    }
    expect(first).toEqual(second);
  });
});
