/**
 * Repository tests for the Child 5 plugin-side lifecycle journal.
 *
 * The lifecycle repository extends the create/update surface of the
 * Child 4 repository with `rename`, `move`, `delete` and `restore`
 * intents. Every write must land in one transaction: the journal event
 * row, the lifecycle operands row and the local_files update must roll
 * back together on any failure. Terminal retention, exact replay, the
 * prohibition on lifecycle coalescing with content events and the
 * reconcile_required flag for corrupt dependency evidence are all pinned
 * here. A leakage contract asserts the status / attempt projections
 * never carry `expected_locator`, `target_locator` or source IDs.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { FrozenFingerprint, JournalEvent, LocalFile } from "./contracts";
import { JournalRepository } from "./repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";

import { LifecycleRepository } from "./lifecycle-repository";
import type { LifecycleEventOperands } from "./lifecycle-contracts";
import {
  LIFECYCLE_JOURNAL_OPERATIONS,
  LIFECYCLE_LOCAL_FILE_STATES,
} from "./lifecycle-contracts";

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

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function fingerprintOf(prefix: string, sizeBytes = 32): FrozenFingerprint {
  return {
    sha256: `${prefix}${"0".repeat(64 - prefix.length)}`,
    sizeBytes,
    mediaType: "text/plain",
  };
}

function createOpenedJournal(): {
  database: SqliteDatabase;
  repository: JournalRepository;
  lifecycle: LifecycleRepository;
} {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  let currentEpochMs = 1_784_000_000_000;
  const repository = new JournalRepository({
    database,
    nowEpochMs: () => currentEpochMs++,
  });
  const lifecycle = new LifecycleRepository({
    database,
    nowEpochMs: () => currentEpochMs++,
  });
  return { database, repository, lifecycle };
}

async function captureAndCommit(
  repository: JournalRepository,
  path: string,
  fingerprint: FrozenFingerprint,
): Promise<JournalEvent> {
  const capture = await repository.recordCapture({
    normalizedPath: path,
    fingerprint,
    policyRevisionNumber: 1,
    admission: "policy_allowed",
  });
  if (capture.outcome === "capture_refused") {
    throw new Error("expected a recorded capture");
  }
  const { event } = capture;
  await repository.recordCommittedReceipt({
    eventId: event.eventId,
    sourceId: "11111111-1111-7111-8111-111111111111",
    baseVersionId: "22222222-2222-7222-8222-222222222222",
  });
  return event;
}

function operandsFor(
  operation: LifecycleEventOperands["operation"],
  overrides: Partial<LifecycleEventOperands> = {},
): LifecycleEventOperands {
  const base: LifecycleEventOperands = {
    operation,
    sourceId: "11111111-1111-7111-8111-111111111111",
    expectedVersionId: "22222222-2222-7222-8222-222222222222",
    expectedLocator: null,
    targetLocator: null,
    tombstoneId: null,
    policyRevision: 1,
    predecessorEventId: null,
  };
  return { ...base, ...overrides };
}

function requireLocalFile(maybeFile: LocalFile | null): LocalFile {
  if (maybeFile === null) {
    throw new Error("expected a local file row");
  }
  return maybeFile;
}

describe("LifecycleRepository closed vocabulary", () => {
  it("exposes exactly the four lifecycle operations from the closed set", () => {
    expect([...LIFECYCLE_JOURNAL_OPERATIONS]).toEqual([
      "rename",
      "move",
      "delete",
      "restore",
    ]);
  });

  it("exposes exactly the eight lifecycle local file states", () => {
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

  it("rejects unknown operation tokens before any SQL runs", async () => {
    const { lifecycle } = createOpenedJournal();
    const fakeLocalFile: LocalFile = {
      localFileId: "11111111-1111-7111-8111-111111111111",
      normalizedPath: "notes/op.md",
      sourceId: null,
      observedFingerprint: {
        sha256: "0".repeat(64),
        sizeBytes: 0,
        mediaType: "application/octet-stream",
      },
      baseVersionId: null,
      policyRevisionNumber: 1,
    };
    await expect(
      lifecycle.recordLifecycleEvent(
        operandsFor("merge" as never),
        { localFile: fakeLocalFile },
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("rejects unknown lifecycle states before any SQL runs", async () => {
    const { lifecycle } = createOpenedJournal();
    const fakeLocalFile: LocalFile = {
      localFileId: "11111111-1111-7111-8111-111111111111",
      normalizedPath: "notes/state.md",
      sourceId: null,
      observedFingerprint: {
        sha256: "0".repeat(64),
        sizeBytes: 0,
        mediaType: "application/octet-stream",
      },
      baseVersionId: null,
      policyRevisionNumber: 1,
    };
    await expect(
      lifecycle.recordLifecycleEvent(
        operandsFor("rename", { policyRevision: 1 }),
        { localFile: fakeLocalFile, initialLifecycleState: "evicted" as never },
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });
});

describe("LifecycleRepository rename + move event insertion in one transaction", () => {
  it("records a rename event and updates last_locator + lifecycle_state atomically", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/note.md", fingerprintOf("aa"));
    const fileBefore = requireLocalFile(
      repository.readLocalFileByPath("notes/note.md"),
    );

    const result = await lifecycle.recordLifecycleEvent(
      operandsFor("rename", {
        expectedLocator: "notes/note.md",
        targetLocator: "notes/renamed.md",
      }),
      { localFile: fileBefore },
    );

    expect(result.event.operation).toBe("rename");
    expect(result.event.eventId).toMatch(UUID_PATTERN);
    expect(result.event.idempotencyKey).toMatch(UUID_PATTERN);
    expect(result.eventIdempotencyKey).toMatch(UUID_PATTERN);

    const fileAfter = requireLocalFile(
      repository.readLocalFileByPath("notes/note.md"),
    );
    expect(fileAfter.localFileId).toBe(fileBefore.localFileId);
    const refreshed = database.readAll(
      "select last_locator, open_tombstone_id, lifecycle_state from local_files where local_file_id = $id".replace(
        "$id",
        `'${fileBefore.localFileId}'`,
      ),
    );
    expect(refreshed[0]?.values[0]).toEqual([
      "notes/renamed.md",
      null,
      "rename_pending",
    ]);
    database.close();
  });

  it("records a move event and updates last_locator to the target", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/movable.md", fingerprintOf("ba"));
    const fileBefore = requireLocalFile(
      repository.readLocalFileByPath("notes/movable.md"),
    );

    const result = await lifecycle.recordLifecycleEvent(
      operandsFor("move", {
        expectedLocator: "notes/movable.md",
        targetLocator: "archive/movable.md",
      }),
      { localFile: fileBefore },
    );

    expect(result.event.operation).toBe("move");
    const refreshed = database.readAll(
      "select last_locator, lifecycle_state from local_files where local_file_id = $id".replace(
        "$id",
        `'${fileBefore.localFileId}'`,
      ),
    );
    expect(refreshed[0]?.values[0]).toEqual(["archive/movable.md", "move_pending"]);
    database.close();
  });

  it("rolls back journal_events + lifecycle_event_operands + local_files together on a failure", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/atomic.md", fingerprintOf("ca"));
    const fileBefore = requireLocalFile(
      repository.readLocalFileByPath("notes/atomic.md"),
    );

    const eventsBefore = database.readAll("select count(*) from journal_events;")[0]
      ?.values[0]?.[0];
    const operandsBefore = database.readAll(
      "select count(*) from lifecycle_event_operands;",
    )[0]?.values[0]?.[0];

    await expect(
      lifecycle.recordLifecycleEvent(
        operandsFor("rename", {
          expectedLocator: "notes/atomic.md",
          targetLocator: "notes/atomic-renamed.md",
        }),
        { localFile: fileBefore, forceFailureAfterExec: true },
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    expect(database.readAll("select count(*) from journal_events;")[0]?.values[0]?.[0]).toBe(
      eventsBefore,
    );
    expect(
      database.readAll("select count(*) from lifecycle_event_operands;")[0]?.values[0]?.[0],
    ).toBe(operandsBefore);
    expect(database.readAll("select lifecycle_state from local_files;")[0]?.values[0]).toEqual([
      "active",
    ]);
    database.close();
  });
});

describe("LifecycleRepository delete + restore", () => {
  it("records a delete event, retains the local mapping and sets the open tombstone id", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/disposable.md", fingerprintOf("da"));
    const fileBefore = requireLocalFile(
      repository.readLocalFileByPath("notes/disposable.md"),
    );

    const deleteResult = await lifecycle.recordLifecycleEvent(
      operandsFor("delete", { expectedLocator: "notes/disposable.md" }),
      {
        localFile: fileBefore,
        tombstoneId: "44444444-4444-7444-8444-444444444444",
      },
    );

    expect(deleteResult.event.operation).toBe("delete");
    const tombstoned = database.readAll(
      "select open_tombstone_id, lifecycle_state from local_files where local_file_id = $id".replace(
        "$id",
        `'${fileBefore.localFileId}'`,
      ),
    );
    expect(tombstoned[0]?.values[0]).toEqual([
      "44444444-4444-7444-8444-444444444444",
      "tombstoned",
    ]);

    const fileAfter = requireLocalFile(
      repository.readLocalFileByPath("notes/disposable.md"),
    );
    await lifecycle.markTombstoneForLocalFile(
      fileAfter.localFileId,
      "55555555-5555-7555-8555-555555555555",
    );
    const reTombstoned = database.readAll(
      "select open_tombstone_id from local_files where local_file_id = $id".replace(
        "$id",
        `'${fileBefore.localFileId}'`,
      ),
    );
    expect(reTombstoned[0]?.values[0]?.[0]).toBe(
      "55555555-5555-7555-8555-555555555555",
    );

    const restoreResult = await lifecycle.recordLifecycleEvent(
      operandsFor("restore", {
        targetLocator: "notes/disposable.md",
        tombstoneId: "55555555-5555-7555-8555-555555555555",
        predecessorEventId: deleteResult.event.eventId,
      }),
      { localFile: fileAfter },
    );
    expect(restoreResult.event.operation).toBe("restore");
    await lifecycle.consumeRestoreSuccessor(fileAfter.localFileId);
    const restored = database.readAll(
      "select open_tombstone_id, lifecycle_state from local_files where local_file_id = $id".replace(
        "$id",
        `'${fileBefore.localFileId}'`,
      ),
    );
    expect(restored[0]?.values[0]).toEqual([null, "restored"]);
    database.close();
  });
});

describe("LifecycleRepository ordered predecessor dependencies and replay", () => {
  it("requires predecessor_event_id for a restore that follows a delete", async () => {
    const { repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/replay.md", fingerprintOf("ea"));
    const file = requireLocalFile(repository.readLocalFileByPath("notes/replay.md"));
    await lifecycle.recordLifecycleEvent(
      operandsFor("delete", { expectedLocator: "notes/replay.md" }),
      {
        localFile: file,
        tombstoneId: "66666666-6666-7666-8666-666666666666",
      },
    );
    await expect(
      lifecycle.recordLifecycleEvent(
        operandsFor("restore", {
          targetLocator: "notes/replay.md",
          tombstoneId: "66666666-6666-7666-8666-666666666666",
          predecessorEventId: null,
        }),
        { localFile: file },
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("rejects a rename whose predecessor_event_id does not reference a stored event", async () => {
    const { repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/dependency.md", fingerprintOf("fa"));
    const file = requireLocalFile(repository.readLocalFileByPath("notes/dependency.md"));
    await expect(
      lifecycle.recordLifecycleEvent(
        operandsFor("rename", {
          expectedLocator: "notes/dependency.md",
          targetLocator: "notes/dependency-renamed.md",
          predecessorEventId: "99999999-9999-7999-8999-999999999999",
        }),
        { localFile: file },
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("replays an existing event identically when the same idempotency key is replayed", async () => {
    const { repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/replayable.md", fingerprintOf("0a"));
    const file = requireLocalFile(
      repository.readLocalFileByPath("notes/replayable.md"),
    );

    const operands = operandsFor("rename", {
      expectedLocator: "notes/replayable.md",
      targetLocator: "notes/replayable-renamed.md",
    });
    const first = await lifecycle.recordLifecycleEvent(operands, {
      localFile: file,
    });
    const second = await lifecycle.recordLifecycleEvent(operands, {
      localFile: file,
    });

    expect(second.eventId).toBe(first.eventId);
    expect(second.eventIdempotencyKey).toBe(first.eventIdempotencyKey);
    expect(second.event.fingerprint).toEqual(first.event.fingerprint);
    expect(
      repository.readLocalFileByPath("notes/replayable.md"),
    ).not.toBeNull();
  });

  it("keeps terminal lifecycle rows queryable forever (no auto-deletion)", async () => {
    const { repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/terminal.md", fingerprintOf("1a"));
    const file = requireLocalFile(repository.readLocalFileByPath("notes/terminal.md"));
    const { event } = await lifecycle.recordLifecycleEvent(
      operandsFor("delete", { expectedLocator: "notes/terminal.md" }),
      {
        localFile: file,
        tombstoneId: "77777777-7777-7777-8777-777777777777",
      },
    );
    const history = repository.readEventsByLocalFileId(file.localFileId);
    expect(history).toHaveLength(2);
    expect(history.map((entry) => entry.eventId)).toEqual([
      expect.stringMatching(UUID_PATTERN),
      event.eventId,
    ]);
  });
});

describe("LifecycleRepository coalescing prohibition", () => {
  it("never replaces a content event with a lifecycle event or vice-versa", async () => {
    const { repository, lifecycle } = createOpenedJournal();
    const createEvent = await captureAndCommit(
      repository,
      "notes/no-coalesce.md",
      fingerprintOf("2a"),
    );
    const file = requireLocalFile(repository.readLocalFileByPath("notes/no-coalesce.md"));

    const { event: lifecycleEvent } = await lifecycle.recordLifecycleEvent(
      operandsFor("rename", {
        expectedLocator: "notes/no-coalesce.md",
        targetLocator: "notes/no-coalesce-renamed.md",
      }),
      { localFile: file },
    );
    expect(lifecycleEvent.eventId).not.toBe(createEvent.eventId);
    const events = repository.readEventsByLocalFileId(file.localFileId);
    expect(events).toHaveLength(2);
    expect(events.map((entry) => entry.operation)).toEqual(["create", "rename"]);

    const coalescableCapture = await repository.recordCapture({
      normalizedPath: "notes/no-coalesce.md",
      fingerprint: fingerprintOf("2b"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(coalescableCapture.outcome).toBe("event_recorded");
    const eventsAfter = repository.readEventsByLocalFileId(file.localFileId);
    expect(eventsAfter).toHaveLength(3);
    expect(eventsAfter.map((entry) => entry.operation)).toEqual(["create", "rename", "update"]);
  });
});

describe("LifecycleRepository reconcile_required on corrupt dependency evidence", () => {
  it("marks reconcile_required and flags lifecycle_state when a predecessor_event_id points to a missing row", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/corrupt.md", fingerprintOf("3a"));
    const file = requireLocalFile(repository.readLocalFileByPath("notes/corrupt.md"));

    const deleteResult = await lifecycle.recordLifecycleEvent(
      operandsFor("delete", { expectedLocator: "notes/corrupt.md" }),
      {
        localFile: file,
        tombstoneId: "88888888-8888-7888-8888-888888888888",
      },
    );
    await lifecycle.recordLifecycleEvent(
      operandsFor("restore", {
        targetLocator: "notes/corrupt.md",
        tombstoneId: "88888888-8888-7888-8888-888888888888",
        predecessorEventId: deleteResult.eventId,
      }),
      { localFile: file },
    );
    database.readAll(
      `delete from journal_events where event_id = '${deleteResult.eventId}';`,
    );

    const reconcileRows = await lifecycle.findReconcileRequired();
    expect(reconcileRows.map((row) => row.localFileId)).toContain(file.localFileId);
    expect(
      reconcileRows.find((row) => row.localFileId === file.localFileId)?.reason,
    ).toBe("predecessor_missing");
    const meta = database.readJournalMeta();
    expect(meta.isReconcileRequired).toBe(true);
    const stateRow = database.readAll(
      `select lifecycle_state from local_files where local_file_id = '${file.localFileId}';`,
    );
    expect(stateRow[0]?.values[0]?.[0]).toBe("reconcile_required");
    database.close();
  });
});

describe("LifecycleRepository leakage contract", () => {
  it("status / attempt projections never expose expected_locator, target_locator or source IDs", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/leakage.md", fingerprintOf("4a"));
    const file = requireLocalFile(repository.readLocalFileByPath("notes/leakage.md"));
    const { event } = await lifecycle.recordLifecycleEvent(
      operandsFor("rename", {
        expectedLocator: "notes/leakage.md",
        targetLocator: "notes/leakage-renamed.md",
      }),
      { localFile: file },
    );

    const histogram = repository.readEventStateErrorCounts();
    const flatHistogram = JSON.stringify(histogram);
    expect(flatHistogram).not.toContain("notes/leakage.md");
    expect(flatHistogram).not.toContain("notes/leakage-renamed.md");
    expect(flatHistogram).not.toContain("11111111-1111-7111-8111-111111111111");

    await repository.recordEventAttempt({
      eventId: event.eventId,
      attemptedAtEpochMs: 1_784_000_001_000,
      outcomeLabel: "server_error",
      requestCorrelationId: "corr-leak",
    });
    const attempts = repository.readEventAttemptHistory(event.eventId);
    const flatAttempts = JSON.stringify(attempts);
    expect(flatAttempts).not.toContain("notes/leakage");
    expect(flatAttempts).not.toContain("11111111-1111-7111-8111-111111111111");

    const { projectJournalSyncStatus } = await import("./status");
    const status = projectJournalSyncStatus({
      isReconcileRequired: false,
      eventStateErrorCounts: histogram,
      hasAccessCredential: true,
      isQueuePassActive: false,
      lastQueuePassOutcome: null,
    });
    const flatStatus = JSON.stringify(status);
    expect(flatStatus).not.toContain("notes/leakage");
    expect(flatStatus).not.toContain("notes/leakage-renamed");
    expect(flatStatus).not.toContain("11111111-1111-7111-8111-111111111111");
    database.close();
  });
});
