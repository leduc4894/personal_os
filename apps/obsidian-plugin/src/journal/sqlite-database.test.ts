import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { JournalMeta } from "./contracts";
import {
  JOURNAL_SCHEMA_VERSION,
  JournalStoreError,
  SqliteDatabase,
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
