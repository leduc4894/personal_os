/**
 * Strict-type tests for the plugin-side lifecycle journal extension of
 * Child 5. The brief freezes four new `JournalOperation` tokens
 * (`rename`, `move`, `delete`, `restore`) added on top of the existing
 * `create`/`update` surface, an explicit `LifecycleLocalFileState` enum of
 * eight closed values, and the `LifecycleEventOperands` shape that carries
 * `source_id`, version evidence, locator pointers, tombstone id and the
 * ordered predecessor. The migration to Child 5 must preserve every prior
 * row in `local_files`, `journal_events`, `journal_attempts` and the
 * manifest generation: no child-4 evidence may be lost in the upgrade.
 */

import { beforeAll, describe, expect, it } from "vitest";

import type { JournalMeta } from "./contracts";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";

describe("LIFECYCLE_JOURNAL_OPERATIONS closed set (child 5)", () => {
  it("adds exactly rename, move, delete and restore on top of create/update", async () => {
    const { LIFECYCLE_JOURNAL_OPERATIONS } = await import("./lifecycle-contracts");
    expect([...LIFECYCLE_JOURNAL_OPERATIONS]).toEqual([
      "rename",
      "move",
      "delete",
      "restore",
    ]);
  });

  it("keeps the create/update tokens the JournalOperation union already admits", async () => {
    const { LIFECYCLE_JOURNAL_OPERATIONS } = await import("./lifecycle-contracts");
    const { JOURNAL_OPERATIONS } = await import("./contracts");
    // The closed union now extends; create and update remain and the four
    // lifecycle tokens are folded in.
    expect([...JOURNAL_OPERATIONS]).toEqual([
      "create",
      "update",
      "rename",
      "move",
      "delete",
      "restore",
    ]);
    // The four lifecycle tokens are NOT in the dedicated lifecycle-only set
    // boundary test below — they are defined together with the content ones.
    for (const token of LIFECYCLE_JOURNAL_OPERATIONS) {
      expect((JOURNAL_OPERATIONS as readonly string[]).includes(token)).toBe(true);
    }
  });

  it("folds the union without removing create/update from JournalOperation", async () => {
    const { LIFECYCLE_JOURNAL_OPERATIONS } = await import("./lifecycle-contracts");
    const { JOURNAL_OPERATIONS } = await import("./contracts");
    type ContentOp = (typeof JOURNAL_OPERATIONS)[number];
    type LifecycleOp = (typeof LIFECYCLE_JOURNAL_OPERATIONS)[number];
    type Combined = ContentOp | LifecycleOp;
    const createOp: Combined = "create";
    const renameOp: Combined = "rename";
    expect(createOp).toBe("create");
    expect(renameOp).toBe("rename");
    // The closed union must reject unknown tokens at compile time.
    // @ts-expect-error an unknown operation must stay unassignable
    const unknownOp: Combined = "merge";
    expect(unknownOp).toBe("merge");
    // Reference the runtime exports so the eslint rule for unused-vars
    // sees a real value-typed use of both imported constants.
    expect(LIFECYCLE_JOURNAL_OPERATIONS.length).toBeGreaterThan(0);
    expect(JOURNAL_OPERATIONS.length).toBeGreaterThan(0);
  });
});

describe("LIFECYCLE_LOCAL_FILE_STATES closed set (child 5)", () => {
  it("is exactly the eight closed lifecycle local-file states", async () => {
    const { LIFECYCLE_LOCAL_FILE_STATES } = await import("./lifecycle-contracts");
    expect([...LIFECYCLE_LOCAL_FILE_STATES]).toEqual([
      "active",
      "rename_pending",
      "move_pending",
      "delete_pending",
      "restore_pending",
      "tombstoned",
      "restored",
      "reconcile_required",
    ]);
  });

  it("rejects unknown lifecycle states at the type level", async () => {
    const { LIFECYCLE_LOCAL_FILE_STATES } = await import("./lifecycle-contracts");
    type LifecycleLocalFileState = (typeof LIFECYCLE_LOCAL_FILE_STATES)[number];
    const tombstoned: LifecycleLocalFileState = "tombstoned";
    expect(tombstoned).toBe("tombstoned");
    // @ts-expect-error an unknown lifecycle state must stay unassignable
    const unknown: LifecycleLocalFileState = "evicted";
    expect(unknown).toBe("evicted");
    expect(LIFECYCLE_LOCAL_FILE_STATES.length).toBeGreaterThan(0);
  });
});

describe("LifecycleEventOperands shape (child 5)", () => {
  it("requires source_id, expected_version_id and policy_revision >= 1", async () => {
    const { LIFECYCLE_JOURNAL_OPERATIONS } = await import("./lifecycle-contracts");
    expect(LIFECYCLE_JOURNAL_OPERATIONS).toBeDefined();
    const { createLifecycleEventOperands } = await import("./lifecycle-contracts");
    const operands = createLifecycleEventOperands({
      operation: "delete",
      sourceId: "11111111-1111-7111-8111-111111111111",
      expectedVersionId: "22222222-2222-7222-8222-222222222222",
      expectedLocator: "notes/gone.md",
      policyRevision: 1,
    });
    expect(operands.sourceId).toBe("11111111-1111-7111-8111-111111111111");
    expect(operands.expectedVersionId).toBe("22222222-2222-7222-8222-222222222222");
    expect(operands.policyRevision).toBe(1);
    expect(operands.expectedLocator).toBe("notes/gone.md");
    expect(operands.targetLocator).toBeNull();
    expect(operands.tombstoneId).toBeNull();
    expect(operands.predecessorEventId).toBeNull();
  });

  it("accepts a non-null predecessor_event_id when ordered dependencies exist", async () => {
    const { createLifecycleEventOperands } = await import("./lifecycle-contracts");
    const operands = createLifecycleEventOperands({
      operation: "restore",
      sourceId: "11111111-1111-7111-8111-111111111111",
      expectedVersionId: "22222222-2222-7222-8222-222222222222",
      targetLocator: "notes/restored.md",
      tombstoneId: "44444444-4444-7444-8444-444444444444",
      policyRevision: 2,
      predecessorEventId: "33333333-3333-7333-8333-333333333333",
    });
    expect(operands.targetLocator).toBe("notes/restored.md");
    expect(operands.policyRevision).toBe(2);
    expect(operands.predecessorEventId).toBe("33333333-3333-7333-8333-333333333333");
    expect(operands.tombstoneId).toBe("44444444-4444-7444-8444-444444444444");
  });

  it("rejects policy_revision below the spec floor", async () => {
    const { createLifecycleEventOperands } = await import("./lifecycle-contracts");
    expect(() =>
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: "11111111-1111-7111-8111-111111111111",
        expectedVersionId: "22222222-2222-7222-8222-222222222222",
        expectedLocator: "notes/a.md",
        targetLocator: "notes/b.md",
        policyRevision: 0,
      }),
    ).toThrowError(expect.objectContaining({ reason: "journal_mutation_failed" }));
  });
});

describe("schema migration from Child 4 to Child 5", () => {
  let engineModule: SqliteEngineModule;
  beforeAll(async () => {
    const { readFileSync } = await import("node:fs");
    const initSqlJs = (await import("sql.js")).default;
    const wasmBytes = new Uint8Array(
      readFileSync(
        new URL("../../node_modules/sql.js/dist/sql-wasm.wasm", import.meta.url),
      ),
    );
    const wasmBinary = wasmBytes.buffer.slice(
      wasmBytes.byteOffset,
      wasmBytes.byteOffset + wasmBytes.byteLength,
    ) as ArrayBuffer;
    engineModule = await initSqlJs({ wasmBinary });
  });

  it("bumps JOURNAL_SCHEMA_VERSION to 3 with lifecycle tables present", async () => {
    const { LIFECYCLE_SCHEMA_VERSION } = await import("./lifecycle-contracts");
    expect(LIFECYCLE_SCHEMA_VERSION).toBe(JOURNAL_SCHEMA_VERSION);
    expect(LIFECYCLE_SCHEMA_VERSION).toBe(4);
    expect(JOURNAL_SCHEMA_VERSION).toBe(4);
  });

  it("migrates a Child 4 journal through v3 to v4 without losing any prior row", async () => {
    const { migrateChildFourJournalToLifecycleSchema, migrateLifecycleJournalToLastCommittedSchema } = await import(
      "./lifecycle-contracts"
    );
    // Seed a Child 4 journal by hand (no lifecycle columns) using raw sql.js.
    const seedDatabase = new engineModule.Database();
    seedDatabase.exec(`
      create table journal_meta (
        singleton_key integer primary key check (singleton_key = 1),
        schema_version integer not null,
        dirty_generation integer not null,
        last_verified_generation integer not null,
        is_reconcile_required integer not null check (is_reconcile_required in (0, 1)),
        recovery_state text not null
      );
      insert into journal_meta values (1, 2, 4, 3, 0, 'verified_generation_loaded');

      create table local_files (
        local_file_id text primary key,
        normalized_path text not null unique,
        source_id text,
        observed_sha256 text not null,
        observed_size_bytes integer not null check (observed_size_bytes >= 0),
        observed_media_type text not null,
        base_version_id text,
        policy_revision integer not null check (policy_revision >= 0)
      );
      insert into local_files values
        ('f1f1f1f1-0000-4000-8000-000000000001', 'notes/a.md', null,
         'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 12, 'text/plain', null, 1),
        ('f1f1f1f1-0000-4000-8000-000000000002', 'notes/b.md', null,
         'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 34, 'text/plain', null, 1);

      create table journal_events (
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
      insert into journal_events values
        ('e1e1e1e1-0000-4000-8000-000000000001', 'f1f1f1f1-0000-4000-8000-000000000001',
         'k1k1k1k1-0000-4000-8000-000000000001', 'create',
         'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 12, 'text/plain',
         'committed', 1, 0, null, null, null, 1784000000000),
        ('e1e1e1e1-0000-4000-8000-000000000002', 'f1f1f1f1-0000-4000-8000-000000000002',
         'k1k1k1k1-0000-4000-8000-000000000002', 'create',
         'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 34, 'text/plain',
         'queued', 0, 0, null, null, null, 1784000000001);

      create table journal_attempts (
        attempt_ordinal integer primary key autoincrement,
        event_id text not null references journal_events (event_id),
        attempted_at_epoch_ms integer not null check (attempted_at_epoch_ms >= 0),
        outcome_label text not null,
        request_correlation_id text not null
      );
      insert into journal_attempts (event_id, attempted_at_epoch_ms, outcome_label, request_correlation_id)
        values ('e1e1e1e1-0000-4000-8000-000000000001', 1784000000010, 'server_error', 'corr-1');

      pragma user_version = 2;
    `);
    const childFourImage = seedDatabase.export();
    seedDatabase.close();

    const v3Image = migrateChildFourJournalToLifecycleSchema(engineModule, childFourImage);
    const v4Image = migrateLifecycleJournalToLastCommittedSchema(engineModule, v3Image);

    const reopened = SqliteDatabase.openFromImage(engineModule, v4Image);
    const meta = reopened.readJournalMeta() satisfies JournalMeta;
    expect(meta.schemaVersion).toBe(JOURNAL_SCHEMA_VERSION);
    expect(meta.dirtyGeneration).toBe(4);
    expect(meta.lastVerifiedGeneration).toBe(3);
    expect(meta.recoveryState).toBe("verified_generation_loaded");

    // Every prior row of the three journal tables survives the migration.
    const files = reopened.readAll("select local_file_id, normalized_path from local_files order by normalized_path;");
    expect(files[0]?.values).toEqual([
      ["f1f1f1f1-0000-4000-8000-000000000001", "notes/a.md"],
      ["f1f1f1f1-0000-4000-8000-000000000002", "notes/b.md"],
    ]);
    const events = reopened.readAll(
      "select event_id, operation, state from journal_events order by created_at_epoch_ms;",
    );
    expect(events[0]?.values).toEqual([
      ["e1e1e1e1-0000-4000-8000-000000000001", "create", "committed"],
      ["e1e1e1e1-0000-4000-8000-000000000002", "create", "queued"],
    ]);
    const attempts = reopened.readAll(
      "select event_id, outcome_label from journal_attempts order by attempt_ordinal;",
    );
    expect(attempts[0]?.values).toEqual([
      ["e1e1e1e1-0000-4000-8000-000000000001", "server_error"],
    ]);

    // The new lifecycle surface is present and empty.
    const lifecycleRows = reopened.readAll(
      "select count(*) from lifecycle_event_operands;",
    );
    expect(lifecycleRows[0]?.values[0]?.[0]).toBe(0);
    const stateCheck = reopened.readAll(
      "select local_file_id, lifecycle_state, last_locator, open_tombstone_id, last_committed_sha256 from local_files order by normalized_path;",
    );
    expect(stateCheck[0]?.values).toEqual([
      ["f1f1f1f1-0000-4000-8000-000000000001", "active", null, null, null],
      ["f1f1f1f1-0000-4000-8000-000000000002", "active", null, null, null],
    ]);
    // The schema version has advanced to v4.
    expect(reopened.readSchemaVersion()).toBe(4);
    reopened.close();
  });

  it("rejects a Child 4 migration of an image that is not at v2", async () => {
    const { migrateChildFourJournalToLifecycleSchema } = await import(
      "./lifecycle-contracts"
    );
    const seedDatabase = new engineModule.Database();
    seedDatabase.exec(`
      create table journal_meta (
        singleton_key integer primary key check (singleton_key = 1),
        schema_version integer not null,
        dirty_generation integer not null,
        last_verified_generation integer not null,
        is_reconcile_required integer not null check (is_reconcile_required in (0, 1)),
        recovery_state text not null
      );
      insert into journal_meta values (1, 1, 1, 1, 0, 'verified_generation_loaded');
      pragma user_version = 1;
    `);
    const image = seedDatabase.export();
    seedDatabase.close();
    expect(() => migrateChildFourJournalToLifecycleSchema(engineModule, image))
      .toThrowError(expect.objectContaining({ reason: "journal_schema_unsupported" }));
  });
});
