/**
 * Tests of the durable no-byte conflict repair repository (Child 8 spec
 * 5.2.6/6, Task 7). These tests pin: the park/read/attempt/complete flow
 * over the real sql.js journal schema v9 `conflict_local_repairs` table,
 * the idempotent same-resolution-event replay, the refusal of a foreign
 * resolution event against a parked row, the closed vocabulary and UUID
 * validation before any SQL runs, and the schema-level guarantee that no
 * column of the repair table can ever hold bytes or paths.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { JournalMeta } from "../journal/contracts";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import { ConflictRepository } from "./repository";
import type { PendingLocalApply } from "./contracts";

/** The real sql.js WebAssembly engine drives every repository test. */
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

const CONFLICT_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_CONFLICT_ID = "22222222-2222-4222-8222-222222222222";
const RESOLUTION_EVENT_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_RESOLUTION_EVENT_ID = "44444444-4444-4444-8444-444444444444";

function createJournalMeta(): JournalMeta {
  return {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 0,
    lastVerifiedGeneration: 0,
    isReconcileRequired: false,
    recoveryState: "fresh_journal_created",
  };
}

/** One repository over a fresh journal whose serialized writer is the seam. */
function createRepository(): { repository: ConflictRepository; database: SqliteDatabase } {
  const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
  return { repository: new ConflictRepository({ database }), database };
}

const PARK_INPUT = {
  conflictId: CONFLICT_ID,
  resolutionEventId: RESOLUTION_EVENT_ID,
  targetAction: "apply_remote_version" as const,
  safeReason: "resolution_committed" as const,
  nowEpochMs: 1_784_000_000_000,
};

describe("ConflictRepository park and read (spec 5.2.6)", () => {
  it("parks one pending local apply and reads it back exactly", async () => {
    const { repository } = createRepository();
    await repository.parkPendingLocalApply(PARK_INPUT);

    expect(repository.readPendingLocalApply(CONFLICT_ID)).toEqual({
      conflictId: CONFLICT_ID,
      resolutionEventId: RESOLUTION_EVENT_ID,
      targetAction: "apply_remote_version",
      safeReason: "resolution_committed",
      attemptCount: 0,
      nextEligibleRetryEpochMs: null,
      createdAtEpochMs: PARK_INPUT.nowEpochMs,
      updatedAtEpochMs: PARK_INPUT.nowEpochMs,
    } satisfies PendingLocalApply);
  });

  it("reads the pending applies oldest first", async () => {
    const { repository } = createRepository();
    await repository.parkPendingLocalApply({ ...PARK_INPUT, nowEpochMs: 1_784_000_000_000 });
    await repository.parkPendingLocalApply({
      ...PARK_INPUT,
      conflictId: OTHER_CONFLICT_ID,
      nowEpochMs: 1_784_000_000_000 - 1_000,
    });

    const pending = repository.readPendingLocalApplies();
    expect(pending.map((row) => row.conflictId)).toEqual([OTHER_CONFLICT_ID, CONFLICT_ID]);
  });

  it("answers null for an unknown conflict", () => {
    const { repository } = createRepository();
    expect(repository.readPendingLocalApply(CONFLICT_ID)).toBeNull();
    expect(repository.readPendingLocalApplies()).toEqual([]);
  });

  it("re-parks the same resolution event idempotently with refreshed bookkeeping", async () => {
    const { repository } = createRepository();
    await repository.parkPendingLocalApply(PARK_INPUT);
    await repository.parkPendingLocalApply({
      ...PARK_INPUT,
      safeReason: "vault_apply_failed",
      nowEpochMs: PARK_INPUT.nowEpochMs + 5_000,
    });

    const pending = repository.readPendingLocalApplies();
    expect(pending).toHaveLength(1);
    expect(pending[0]?.safeReason).toBe("vault_apply_failed");
    expect(pending[0]?.resolutionEventId).toBe(RESOLUTION_EVENT_ID);
    expect(pending[0]?.updatedAtEpochMs).toBe(PARK_INPUT.nowEpochMs + 5_000);
    expect(pending[0]?.createdAtEpochMs).toBe(PARK_INPUT.nowEpochMs);
  });

  it("refuses a re-park under a foreign resolution event and keeps the original row", async () => {
    const { repository } = createRepository();
    await repository.parkPendingLocalApply(PARK_INPUT);

    await expect(
      repository.parkPendingLocalApply({
        ...PARK_INPUT,
        resolutionEventId: OTHER_RESOLUTION_EVENT_ID,
      }),
    ).rejects.toMatchObject({ name: "JournalStoreError", reason: "journal_mutation_failed" });

    const pending = repository.readPendingLocalApplies();
    expect(pending).toHaveLength(1);
    expect(pending[0]?.resolutionEventId).toBe(RESOLUTION_EVENT_ID);
  });
});

describe("ConflictRepository retry bookkeeping (spec 5.2.6)", () => {
  it("records one failed attempt with the next eligible retry moment", async () => {
    const { repository } = createRepository();
    await repository.parkPendingLocalApply(PARK_INPUT);

    await repository.recordLocalApplyFailure({
      conflictId: CONFLICT_ID,
      resolutionEventId: RESOLUTION_EVENT_ID,
      safeReason: "vault_apply_failed",
      nowEpochMs: PARK_INPUT.nowEpochMs + 5_000,
      nextEligibleRetryEpochMs: PARK_INPUT.nowEpochMs + 65_000,
    });

    expect(repository.readPendingLocalApply(CONFLICT_ID)).toMatchObject({
      attemptCount: 1,
      safeReason: "vault_apply_failed",
      nextEligibleRetryEpochMs: PARK_INPUT.nowEpochMs + 65_000,
    });
  });

  it("counts every further attempt", async () => {
    const { repository } = createRepository();
    await repository.parkPendingLocalApply(PARK_INPUT);
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      await repository.recordLocalApplyFailure({
        conflictId: CONFLICT_ID,
        resolutionEventId: RESOLUTION_EVENT_ID,
        safeReason: "winner_download_failed",
        nowEpochMs: PARK_INPUT.nowEpochMs + attempt,
        nextEligibleRetryEpochMs: PARK_INPUT.nowEpochMs + attempt * 60_000,
      });
    }

    expect(repository.readPendingLocalApply(CONFLICT_ID)).toMatchObject({
      attemptCount: 3,
      safeReason: "winner_download_failed",
    });
  });

  it("refuses an attempt record for a missing or foreign-resolution row", async () => {
    const { repository } = createRepository();
    await expect(
      repository.recordLocalApplyFailure({
        conflictId: CONFLICT_ID,
        resolutionEventId: RESOLUTION_EVENT_ID,
        safeReason: "vault_apply_failed",
        nowEpochMs: PARK_INPUT.nowEpochMs,
        nextEligibleRetryEpochMs: PARK_INPUT.nowEpochMs + 60_000,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await repository.parkPendingLocalApply(PARK_INPUT);
    await expect(
      repository.recordLocalApplyFailure({
        conflictId: CONFLICT_ID,
        resolutionEventId: OTHER_RESOLUTION_EVENT_ID,
        safeReason: "vault_apply_failed",
        nowEpochMs: PARK_INPUT.nowEpochMs,
        nextEligibleRetryEpochMs: PARK_INPUT.nowEpochMs + 60_000,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });
});

describe("ConflictRepository completion (spec 5.2.6)", () => {
  it("completes the pending apply of the matching resolution event", async () => {
    const { repository } = createRepository();
    await repository.parkPendingLocalApply(PARK_INPUT);

    await repository.completeLocalApply({
      conflictId: CONFLICT_ID,
      resolutionEventId: RESOLUTION_EVENT_ID,
    });

    expect(repository.readPendingLocalApply(CONFLICT_ID)).toBeNull();
    expect(repository.readPendingLocalApplies()).toEqual([]);
  });

  it("refuses completion under a foreign resolution event", async () => {
    const { repository } = createRepository();
    await repository.parkPendingLocalApply(PARK_INPUT);

    await expect(
      repository.completeLocalApply({
        conflictId: CONFLICT_ID,
        resolutionEventId: OTHER_RESOLUTION_EVENT_ID,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readPendingLocalApply(CONFLICT_ID)).not.toBeNull();
  });

  it("refuses completion of an unknown row", async () => {
    const { repository } = createRepository();
    await expect(
      repository.completeLocalApply({
        conflictId: CONFLICT_ID,
        resolutionEventId: RESOLUTION_EVENT_ID,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });
});

describe("ConflictRepository closed-input validation (spec 9)", () => {
  it("refuses a non-UUID conflict or resolution identity before any SQL", async () => {
    const { repository, database } = createRepository();
    await expect(
      repository.parkPendingLocalApply({ ...PARK_INPUT, conflictId: "conflict-1" }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.parkPendingLocalApply({ ...PARK_INPUT, resolutionEventId: "" }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readPendingLocalApplies()).toEqual([]);
    expect(
      database.readAll("select count(*) from conflict_local_repairs;")[0]?.values[0]?.[0],
    ).toBe(0);
    database.close();
  });

  it("refuses target actions and safe reasons outside the closed vocabularies", async () => {
    const { repository } = createRepository();
    await expect(
      repository.parkPendingLocalApply({
        ...PARK_INPUT,
        targetAction: "wipe_vault" as never,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.parkPendingLocalApply({
        ...PARK_INPUT,
        safeReason: "because the disk was full" as never,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readPendingLocalApplies()).toEqual([]);
  });

  it("refuses negative or fractional epochs", async () => {
    const { repository } = createRepository();
    for (const nowEpochMs of [-1, 1.5]) {
      await expect(
        repository.parkPendingLocalApply({ ...PARK_INPUT, nowEpochMs }),
      ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    }
    expect(repository.readPendingLocalApplies()).toEqual([]);
  });
});

describe("conflict_local_repairs schema shape (no-byte guarantee)", () => {
  it("declares no column that could hold bytes or paths", () => {
    const { repository, database } = createRepository();
    void repository;

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
      expect(column).not.toContain("locator");
    }
    database.close();
  });
});
