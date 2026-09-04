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
import { beforeAll, describe, expect, it, vi} from "vitest";

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

// Parallel-coverage headroom: this file's tests
// scan real projection surfaces, which inflates well past the 5 s default when the full
// suite runs `vitest run --coverage` on a loaded machine (observed:
// "Test timed out in 5000ms" with every test passing standalone). The
// raised bound is wall-clock headroom only — no assertion is weakened.
vi.setConfig({ testTimeout: 30_000 });

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
    capturedFingerprintSha256: null,
    capturedFingerprintSizeBytes: null,
    capturedFingerprintMediaType: null,
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
      lastCommittedFingerprint: null,
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
      lastCommittedFingerprint: null,
    };
    await expect(
      lifecycle.recordLifecycleEvent(
        operandsFor("rename", { policyRevision: 1 }),
        { localFile: fakeLocalFile, initialLifecycleState: "evicted" as never },
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });
});

describe("LifecycleRepository durable pending rename intents", () => {
  it("creates one owner intent, treats an exact replay as unchanged, and composes A -> B -> C", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const capture = await repository.recordCapture({
      normalizedPath: "notes/a.md",
      fingerprint: fingerprintOf("10"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (capture.outcome === "capture_refused") {
      throw new Error("capture must be admitted");
    }

    await expect(
      lifecycle.recordOrComposePendingRenameIntent({
        localFileId: capture.localFile.localFileId,
        observedPriorPath: "notes/a.md",
        observedCurrentPath: "notes/b.md",
      }),
    ).resolves.toBe("created");
    await expect(
      lifecycle.recordOrComposePendingRenameIntent({
        localFileId: capture.localFile.localFileId,
        observedPriorPath: "notes/a.md",
        observedCurrentPath: "notes/b.md",
      }),
    ).resolves.toBe("unchanged");
    await expect(
      lifecycle.recordOrComposePendingRenameIntent({
        localFileId: capture.localFile.localFileId,
        observedPriorPath: "notes/b.md",
        observedCurrentPath: "notes/c.md",
      }),
    ).resolves.toBe("composed");

    expect(
      lifecycle.readPendingRenameIntentForLocalFile(capture.localFile.localFileId),
    ).toEqual({
      localFileId: capture.localFile.localFileId,
      priorPath: "notes/a.md",
      currentPath: "notes/c.md",
    });
    expect(lifecycle.readPendingRenameIntentByCurrentPath("notes/c.md")).toEqual(
      lifecycle.readPendingRenameIntentForLocalFile(capture.localFile.localFileId),
    );
    expect(lifecycle.readPendingRenameIntentOwningEndpoint("notes/a.md")?.localFileId).toBe(
      capture.localFile.localFileId,
    );
    expect(lifecycle.readPendingRenameIntents()).toHaveLength(1);
    database.close();
  });

  it("cancels an unmaterialized return to the prior endpoint without persisting equal endpoints", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const capture = await repository.recordCapture({
      normalizedPath: "notes/a.md",
      fingerprint: fingerprintOf("11"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (capture.outcome === "capture_refused") {
      throw new Error("capture must be admitted");
    }
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: capture.localFile.localFileId,
      observedPriorPath: "notes/a.md",
      observedCurrentPath: "notes/b.md",
    });

    await expect(
      lifecycle.recordOrComposePendingRenameIntent({
        localFileId: capture.localFile.localFileId,
        observedPriorPath: "notes/b.md",
        observedCurrentPath: "notes/a.md",
      }),
    ).resolves.toBe("cancelled");
    expect(
      lifecycle.readPendingRenameIntentForLocalFile(capture.localFile.localFileId),
    ).toBeNull();
    expect(repository.readLocalFileByLocalFileId(capture.localFile.localFileId)?.normalizedPath).toBe(
      "notes/a.md",
    );
    database.close();
  });

  it("materializes the latest endpoints atomically, freezes content, and rebases a later target on prefix receipt", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/a.md", fingerprintOf("12"));
    const localFile = requireLocalFile(repository.readLocalFileByPath("notes/a.md"));
    const successorCapture = await repository.recordCapture({
      normalizedPath: "notes/a.md",
      fingerprint: fingerprintOf("13"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (successorCapture.outcome === "capture_refused") {
      throw new Error("capture must be admitted");
    }
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: localFile.localFileId,
      observedPriorPath: "notes/a.md",
      observedCurrentPath: "notes/b.md",
    });

    const prefix = await lifecycle.recordPendingRenameLifecycleEvent(
      localFile.localFileId,
      fingerprintOf("14"),
    );
    expect(prefix).not.toBeNull();
    expect(prefix?.eventId).toMatch(UUID_PATTERN);
    expect(prefix?.event.operation).toBe("rename");
    expect(lifecycle.readLifecycleOperands(prefix?.eventId ?? "")?.expectedLocator).toBe(
      "notes/a.md",
    );
    expect(lifecycle.readLifecycleOperands(prefix?.eventId ?? "")?.targetLocator).toBe(
      "notes/b.md",
    );
    expect(repository.readEvent(successorCapture.event.eventId)?.state).toBe(
      "deferred_lifecycle",
    );
    expect(repository.readLocalFileByLocalFileId(localFile.localFileId)?.normalizedPath).toBe(
      "notes/b.md",
    );
    expect(lifecycle.readPendingRenameIntentForLocalFile(localFile.localFileId)).toMatchObject({
      priorPath: "notes/a.md",
      currentPath: "notes/b.md",
    });

    await expect(
      lifecycle.recordOrComposePendingRenameIntent({
        localFileId: localFile.localFileId,
        observedPriorPath: "notes/b.md",
        observedCurrentPath: "archive/c.md",
      }),
    ).resolves.toBe("composed");
    await lifecycle.recordLifecycleCommittedReceipt(prefix?.eventId ?? "");
    expect(lifecycle.readPendingRenameIntentForLocalFile(localFile.localFileId)).toEqual({
      localFileId: localFile.localFileId,
      priorPath: "notes/b.md",
      currentPath: "archive/c.md",
    });
    database.close();
  });

  it("clears a bound missing-file counter in the generic lifecycle freeze transaction", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/freeze-a.md", fingerprintOf("12a"));
    const owner = requireLocalFile(repository.readLocalFileByPath("notes/freeze-a.md"));
    const update = await repository.recordCapture({
      normalizedPath: "notes/freeze-a.md",
      fingerprint: fingerprintOf("12b"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (update.outcome === "capture_refused") {
      throw new Error("expected update capture");
    }
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: owner.localFileId,
      observedPriorPath: "notes/freeze-a.md",
      observedCurrentPath: "notes/freeze-b.md",
    });
    await repository.resolveIntentAwareLocalFileMissing({
      eventId: update.event.eventId,
      attemptedAtEpochMs: 1_784_000_000_500,
      requestCorrelationId: "generic-freeze-counter",
      nextEligibleRetryEpochMs: 1_784_000_000_750,
    });

    await lifecycle.recordLifecycleEventWithFreeze({
      operands: operandsFor("delete", {
        expectedLocator: "notes/freeze-a.md",
        tombstoneId: "31313131-3131-4131-8131-313131313131",
      }),
      localFile: owner,
      tombstoneId: "31313131-3131-4131-8131-313131313131",
    });

    expect(repository.readEvent(update.event.eventId)?.state).toBe("deferred_lifecycle");
    expect(
      database.readAll(
        "select count(*) from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(0);
    expect(lifecycle.readPendingRenameIntentForLocalFile(owner.localFileId)).not.toBeNull();
    database.close();
  });

  it("rolls back a generic lifecycle freeze together with its counter cleanup", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/freeze-rollback-a.md", fingerprintOf("12c"));
    const owner = requireLocalFile(repository.readLocalFileByPath("notes/freeze-rollback-a.md"));
    const update = await repository.recordCapture({
      normalizedPath: "notes/freeze-rollback-a.md",
      fingerprint: fingerprintOf("12d"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (update.outcome === "capture_refused") {
      throw new Error("expected update capture");
    }
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: owner.localFileId,
      observedPriorPath: "notes/freeze-rollback-a.md",
      observedCurrentPath: "notes/freeze-rollback-b.md",
    });
    await repository.resolveIntentAwareLocalFileMissing({
      eventId: update.event.eventId,
      attemptedAtEpochMs: 1_784_000_000_600,
      requestCorrelationId: "generic-freeze-rollback",
      nextEligibleRetryEpochMs: 1_784_000_000_850,
    });

    await expect(
      lifecycle.recordLifecycleEventWithFreeze({
        operands: operandsFor("delete", {
          expectedLocator: "notes/freeze-rollback-a.md",
          tombstoneId: "32323232-3232-4232-8232-323232323232",
        }),
        localFile: owner,
        tombstoneId: "32323232-3232-4232-8232-323232323232",
        forceFailureAfterExec: true,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readEvent(update.event.eventId)?.state).toBe("waiting_retry");
    expect(
      database.readAll(
        "select deferred_attempt_count from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(1);
    expect(lifecycle.readPendingRenameIntentForLocalFile(owner.localFileId)).not.toBeNull();
    database.close();
  });

  it("keeps equal endpoints as compensation pending only while one immutable prefix is open", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/a.md", fingerprintOf("15"));
    const localFile = requireLocalFile(repository.readLocalFileByPath("notes/a.md"));
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: localFile.localFileId,
      observedPriorPath: "notes/a.md",
      observedCurrentPath: "notes/b.md",
    });
    await lifecycle.recordPendingRenameLifecycleEvent(localFile.localFileId, fingerprintOf("16"));

    await expect(
      lifecycle.recordOrComposePendingRenameIntent({
        localFileId: localFile.localFileId,
        observedPriorPath: "notes/b.md",
        observedCurrentPath: "notes/a.md",
      }),
    ).resolves.toBe("compensation_pending");
    expect(lifecycle.readPendingRenameIntentForLocalFile(localFile.localFileId)).toEqual({
      localFileId: localFile.localFileId,
      priorPath: "notes/a.md",
      currentPath: "notes/a.md",
    });
    database.close();
  });

  it("sends a target collision or incompatible chain to row reconciliation without stealing the endpoint", async () => {
    for (const variant of ["collision", "incompatible"] as const) {
      const { database, repository, lifecycle } = createOpenedJournal();
      await captureAndCommit(repository, "notes/a.md", fingerprintOf("17"));
      const owner = requireLocalFile(repository.readLocalFileByPath("notes/a.md"));
      if (variant === "collision") {
        await captureAndCommit(repository, "notes/occupied.md", fingerprintOf("18"));
      }
      await lifecycle.recordOrComposePendingRenameIntent({
        localFileId: owner.localFileId,
        observedPriorPath: "notes/a.md",
        observedCurrentPath: "notes/b.md",
      });

      await expect(
        lifecycle.recordOrComposePendingRenameIntent({
          localFileId: owner.localFileId,
          observedPriorPath: variant === "collision" ? "notes/b.md" : "notes/unlinked.md",
          observedCurrentPath: variant === "collision" ? "notes/occupied.md" : "notes/c.md",
        }),
      ).rejects.toMatchObject({ reason: "pending_rename_intent_conflict" });
      expect(lifecycle.readPendingRenameIntentForLocalFile(owner.localFileId)).toBeNull();
      expect(repository.readLocalFileByLocalFileId(owner.localFileId)).toMatchObject({
        normalizedPath: "notes/b.md",
      });
      expect(
        database.readAll(
          `select lifecycle_state from local_files where local_file_id = '${owner.localFileId}';`,
        )[0]?.values[0]?.[0],
      ).toBe("reconcile_required");
      expect(database.readJournalMeta().isReconcileRequired).toBe(true);
      database.close();
    }
  });

  it("allows a vacated prior endpoint as a legal path-swap target", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/swap-a.md", fingerprintOf("1a"));
    await captureAndCommit(repository, "notes/swap-b.md", fingerprintOf("1b"));
    const firstOwner = requireLocalFile(repository.readLocalFileByPath("notes/swap-a.md"));
    const secondOwner = requireLocalFile(repository.readLocalFileByPath("notes/swap-b.md"));

    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: firstOwner.localFileId,
      observedPriorPath: "notes/swap-a.md",
      observedCurrentPath: "notes/swap-temp.md",
    });
    await lifecycle.recordPendingRenameLifecycleEvent(
      firstOwner.localFileId,
      fingerprintOf("1c"),
    );
    expect(repository.readLocalFileByPath("notes/swap-a.md")).toBeNull();

    await expect(
      lifecycle.recordOrComposePendingRenameIntent({
        localFileId: secondOwner.localFileId,
        observedPriorPath: "notes/swap-b.md",
        observedCurrentPath: "notes/swap-a.md",
      }),
    ).resolves.toBe("created");
    expect(lifecycle.readPendingRenameIntentForLocalFile(secondOwner.localFileId)).toEqual({
      localFileId: secondOwner.localFileId,
      priorPath: "notes/swap-b.md",
      currentPath: "notes/swap-a.md",
    });
    expect(database.readJournalMeta().isReconcileRequired).toBe(false);
    database.close();
  });

  it("refuses restore-pending owners and intent-owned phantom restore targets", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const ownerCapture = await repository.recordCapture({
      normalizedPath: "notes/owner.md",
      fingerprint: fingerprintOf("19"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (ownerCapture.outcome === "capture_refused") {
      throw new Error("capture must be admitted");
    }
    database.readAll(
      `update local_files set lifecycle_state = 'restore_pending' where local_file_id = '${ownerCapture.localFile.localFileId}';`,
    );
    await expect(
      lifecycle.recordOrComposePendingRenameIntent({
        localFileId: ownerCapture.localFile.localFileId,
        observedPriorPath: "notes/owner.md",
        observedCurrentPath: "notes/new.md",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    database.readAll(
      `update local_files set lifecycle_state = 'active' where local_file_id = '${ownerCapture.localFile.localFileId}';`,
    );
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: ownerCapture.localFile.localFileId,
      observedPriorPath: "notes/owner.md",
      observedCurrentPath: "notes/reserved.md",
    });
    const restored = await captureAndCommit(repository, "notes/tombstone.md", fingerprintOf("20"));
    const restoreOwner = requireLocalFile(repository.readLocalFileByLocalFileId(restored.localFileId));
    await lifecycle.recordLifecycleEvent(
      operandsFor("delete", { expectedLocator: "notes/tombstone.md" }),
      {
        localFile: restoreOwner,
        tombstoneId: "33333333-3333-4333-8333-333333333333",
      },
    );
    expect(await lifecycle.reserveRestoreTarget(restoreOwner.localFileId, "notes/reserved.md")).toEqual({
      outcome: "refused",
      reason: "restore_target_busy",
    });
    expect(
      lifecycle.readPendingRenameIntentForLocalFile(ownerCapture.localFile.localFileId),
    ).not.toBeNull();
    database.close();
  });

  it("atomically terminalizes an intent-owned lifecycle prefix into reconciliation at its latest local endpoint", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/a.md", fingerprintOf("21"));
    const owner = requireLocalFile(repository.readLocalFileByPath("notes/a.md"));
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: owner.localFileId,
      observedPriorPath: "notes/a.md",
      observedCurrentPath: "notes/b.md",
    });
    const prefix = await lifecycle.recordPendingRenameLifecycleEvent(owner.localFileId, fingerprintOf("22"));
    if (prefix === null) {
      throw new Error("expected a materialized lifecycle prefix");
    }
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: owner.localFileId,
      observedPriorPath: "notes/b.md",
      observedCurrentPath: "archive/c.md",
    });
    const stalledUpdate = await repository.recordCapture({
      normalizedPath: "notes/b.md",
      fingerprint: fingerprintOf("22b"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (stalledUpdate.outcome === "capture_refused") {
      throw new Error("expected stalled update capture");
    }
    await repository.resolveIntentAwareLocalFileMissing({
      eventId: stalledUpdate.event.eventId,
      attemptedAtEpochMs: 1_784_000_001_250,
      requestCorrelationId: "lifecycle-terminal-counter",
      nextEligibleRetryEpochMs: 1_784_000_001_500,
    });
    expect(
      database.readAll(
        "select deferred_attempt_count from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(1);

    await expect(
      lifecycle.resolveIntentAwareLifecycleTerminal({
        eventId: prefix.eventId,
        terminalState: "blocked_conflict",
        attemptedAtEpochMs: 1_784_000_001_000,
        requestCorrelationId: "lifecycle-terminal-1",
      }),
    ).resolves.toBe("intent_reconciled");
    expect(repository.readEvent(prefix.eventId)).toMatchObject({
      state: "blocked_conflict",
      safeError: "blocked_conflict",
    });
    expect(repository.readLocalFileByLocalFileId(owner.localFileId)).toMatchObject({
      normalizedPath: "archive/c.md",
    });
    expect(lifecycle.readPendingRenameIntentForLocalFile(owner.localFileId)).toBeNull();
    expect(
      database.readAll(
        "select count(*) from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(0);
    expect(database.readJournalMeta().isReconcileRequired).toBe(true);
    expect(repository.readEventAttemptHistory(prefix.eventId)).toEqual([
      expect.objectContaining({ outcomeLabel: "blocked_conflict" }),
    ]);
    database.close();
  });
});

describe("LifecycleRepository rename + move event insertion in one transaction", () => {
  it("records a rename event and rebinds normalized_path + lifecycle_state atomically", async () => {
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
      { localFile: fileBefore, newPath: "notes/renamed.md" },
    );

    expect(result.event.operation).toBe("rename");
    expect(result.event.eventId).toMatch(UUID_PATTERN);
    expect(result.event.idempotencyKey).toMatch(UUID_PATTERN);
    expect(result.eventIdempotencyKey).toMatch(UUID_PATTERN);

    // C1: the row now resolves by the new path; the prior path is gone.
    expect(repository.readLocalFileByPath("notes/note.md")).toBeNull();
    const fileAfter = requireLocalFile(
      repository.readLocalFileByPath("notes/renamed.md"),
    );
    expect(fileAfter.localFileId).toBe(fileBefore.localFileId);
    const refreshed = database.readAll(
      "select normalized_path, last_locator, open_tombstone_id, lifecycle_state from local_files where local_file_id = $id".replace(
        "$id",
        `'${fileBefore.localFileId}'`,
      ),
    );
    expect(refreshed[0]?.values[0]).toEqual([
      "notes/renamed.md",
      "notes/renamed.md",
      null,
      "rename_pending",
    ]);
    database.close();
  });

  it("records a move event and rebinds normalized_path to the target", async () => {
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
      { localFile: fileBefore, newPath: "archive/movable.md" },
    );

    expect(result.event.operation).toBe("move");
    expect(repository.readLocalFileByPath("notes/movable.md")).toBeNull();
    const refreshed = database.readAll(
      "select normalized_path, last_locator, lifecycle_state from local_files where local_file_id = $id".replace(
        "$id",
        `'${fileBefore.localFileId}'`,
      ),
    );
    expect(refreshed[0]?.values[0]).toEqual(["archive/movable.md", "archive/movable.md", "move_pending"]);
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
      newPath: "notes/replayable-renamed.md",
    });
    const second = await lifecycle.recordLifecycleEvent(operands, {
      localFile: file,
      newPath: "notes/replayable-renamed.md",
    });

    expect(second.eventId).toBe(first.eventId);
    expect(second.eventIdempotencyKey).toBe(first.eventIdempotencyKey);
    expect(second.event.fingerprint).toEqual(first.event.fingerprint);
    // C1: the path was rebound on the first record; the prior path is
    // permanently retired; a replay returns the same event without
    // re-asserting the rebind.
    expect(
      repository.readLocalFileByPath("notes/replayable.md"),
    ).toBeNull();
    expect(
      repository.readLocalFileByPath("notes/replayable-renamed.md")?.localFileId,
    ).toBe(file.localFileId);
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

describe("LifecycleRepository deferral release on rename/move commit (fix round 2 D7)", () => {
  it("clears any remaining intent and bound counter when a delete receipt tombstones the owner", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/delete-receipt.md", fingerprintOf("d6"));
    const owner = requireLocalFile(repository.readLocalFileByPath("notes/delete-receipt.md"));
    const pending = await repository.recordCapture({
      normalizedPath: "notes/delete-receipt.md",
      fingerprint: fingerprintOf("d7"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (pending.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    await lifecycle.recordOrComposePendingRenameIntent({
      localFileId: owner.localFileId,
      observedPriorPath: "notes/delete-receipt.md",
      observedCurrentPath: "archive/delete-receipt.md",
    });
    await repository.resolveIntentAwareLocalFileMissing({
      eventId: pending.event.eventId,
      attemptedAtEpochMs: 1_784_000_002_000,
      requestCorrelationId: "delete-receipt-counter",
      nextEligibleRetryEpochMs: 1_784_000_002_250,
    });
    const deleteEvent = await lifecycle.recordLifecycleEvent(
      operandsFor("delete", {
        expectedLocator: "notes/delete-receipt.md",
        tombstoneId: "99999999-9999-4999-8999-999999999999",
      }),
      {
        localFile: owner,
        tombstoneId: "99999999-9999-4999-8999-999999999999",
      },
    );
    expect(lifecycle.readPendingRenameIntentForLocalFile(owner.localFileId)).not.toBeNull();
    expect(
      database.readAll(
        "select deferred_attempt_count from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(1);

    await lifecycle.recordLifecycleCommittedReceipt(deleteEvent.eventId);

    expect(repository.readEvent(deleteEvent.eventId)?.state).toBe("committed");
    expect(
      database.readAll(
        `select lifecycle_state from local_files where local_file_id = '${owner.localFileId}';`,
      )[0]?.values[0]?.[0],
    ).toBe("tombstoned");
    expect(lifecycle.readPendingRenameIntentForLocalFile(owner.localFileId)).toBeNull();
    expect(
      database.readAll(
        "select count(*) from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(0);
    database.close();
  });

  it("releases the durable lifecycle-deferral marker in the rename commit transaction", async () => {
    const { repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/deferred-note.md", fingerprintOf("b1"));
    // A still-pending content edit exists when the rename freezes it.
    const pending = await repository.recordCapture({
      normalizedPath: "notes/deferred-note.md",
      fingerprint: fingerprintOf("b2"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (pending.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    const file = requireLocalFile(repository.readLocalFileByPath("notes/deferred-note.md"));
    const rename = await lifecycle.recordLifecycleEventWithFreeze({
      operands: operandsFor("rename", {
        expectedLocator: "notes/deferred-note.md",
        targetLocator: "notes/deferred-note-renamed.md",
      }),
      localFile: file,
      newPath: "notes/deferred-note-renamed.md",
    });
    // The freeze flipped the pending content op terminal deferred_lifecycle
    // — the durable marker the capture guard reads.
    const deferred = repository.readEvent(pending.event.eventId);
    expect(deferred?.state).toBe("deferred_lifecycle");
    expect(deferred?.safeError).toBe("deferred_lifecycle");

    // The server-side rename commit releases the marker IN THE SAME
    // TRANSACTION that records the committed receipt: the guard can never
    // refuse the path forever.
    await lifecycle.recordLifecycleCommittedReceipt(rename.eventId);
    expect(repository.readEvent(pending.event.eventId)).toBeNull();
    expect(repository.readEvent(rename.eventId)?.state).toBe("committed");
    // The rebound row survives the release; only the deferral marker rows
    // are cleared.
    const fileAfter = repository.readLocalFileByPath("notes/deferred-note-renamed.md");
    expect(fileAfter?.sourceId).not.toBeNull();
  });

  it("keeps deferral markers for files whose rename has not committed", async () => {
    const { repository, lifecycle } = createOpenedJournal();
    await captureAndCommit(repository, "notes/deferred-other.md", fingerprintOf("b3"));
    const pending = await repository.recordCapture({
      normalizedPath: "notes/deferred-other.md",
      fingerprint: fingerprintOf("b4"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (pending.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    const file = requireLocalFile(repository.readLocalFileByPath("notes/deferred-other.md"));
    const rename = await lifecycle.recordLifecycleEventWithFreeze({
      operands: operandsFor("rename", {
        expectedLocator: "notes/deferred-other.md",
        targetLocator: "notes/deferred-other-renamed.md",
      }),
      localFile: file,
      newPath: "notes/deferred-other-renamed.md",
    });
    // A DIFFERENT file's rename commit must not release this file's
    // deferral marker: the release is scoped to the committed event's own
    // local file.
    await captureAndCommit(repository, "notes/deferred-unrelated.md", fingerprintOf("b5"));
    const unrelated = requireLocalFile(
      repository.readLocalFileByPath("notes/deferred-unrelated.md"),
    );
    const unrelatedRename = await lifecycle.recordLifecycleEventWithFreeze({
      operands: operandsFor("rename", {
        expectedLocator: "notes/deferred-unrelated.md",
        targetLocator: "notes/deferred-unrelated-renamed.md",
      }),
      localFile: unrelated,
      newPath: "notes/deferred-unrelated-renamed.md",
    });
    await lifecycle.recordLifecycleCommittedReceipt(unrelatedRename.eventId);
    void rename;
    expect(repository.readEvent(pending.event.eventId)?.state).toBe("deferred_lifecycle");
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
      { localFile: file, newPath: "notes/no-coalesce-renamed.md" },
    );
    expect(lifecycleEvent.eventId).not.toBe(createEvent.eventId);
    const events = repository.readEventsByLocalFileId(file.localFileId);
    expect(events).toHaveLength(2);
    expect(events.map((entry) => entry.operation)).toEqual(["create", "rename"]);

    // C1: the rename rebound the path; the new capture must use the new
    // path so the durable row continues to the same local_file_id.
    const coalescableCapture = await repository.recordCapture({
      normalizedPath: "notes/no-coalesce-renamed.md",
      fingerprint: fingerprintOf("2b"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(coalescableCapture.outcome).toBe("event_recorded");
    if (coalescableCapture.outcome === "event_recorded") {
      expect(coalescableCapture.localFile.localFileId).toBe(file.localFileId);
    }
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

describe("LifecycleRepository explicit-restore target reservation", () => {
  async function seedTombstonedFile(
    repository: JournalRepository,
    lifecycle: LifecycleRepository,
    path: string,
  ): Promise<{ localFileId: string; deleteEventId: string }> {
    await captureAndCommit(repository, path, fingerprintOf("c1"));
    const file = requireLocalFile(repository.readLocalFileByPath(path));
    const deleteResult = await lifecycle.recordLifecycleEvent(
      operandsFor("delete", { expectedLocator: path }),
      {
        localFile: file,
        tombstoneId: "66666666-6666-4666-8666-666666666666",
      },
    );
    return { localFileId: file.localFileId, deleteEventId: deleteResult.event.eventId };
  }

  it("reserves a free target: rebinds the row, keeps the tombstone, records the prior path", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId } = await seedTombstonedFile(
      repository,
      lifecycle,
      "notes/restore-me.md",
    );

    const result = await lifecycle.reserveRestoreTarget(
      localFileId,
      "notes/restore-target.md",
    );

    expect(result).toEqual({
      outcome: "reserved",
      priorNormalizedPath: "notes/restore-me.md",
    });
    expect(repository.readLocalFileByPath("notes/restore-me.md")).toBeNull();
    const reserved = database.readAll(
      "select normalized_path, lifecycle_state, open_tombstone_id, restore_prior_path from local_files;",
    );
    expect(reserved[0]?.values[0]).toEqual([
      "notes/restore-target.md",
      "restore_pending",
      "66666666-6666-4666-8666-666666666666",
      "notes/restore-me.md",
    ]);
    database.close();
  });

  it("preserves the original prior path across a re-reservation to a new target", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId } = await seedTombstonedFile(
      repository,
      lifecycle,
      "notes/restore-me.md",
    );
    await lifecycle.reserveRestoreTarget(localFileId, "notes/first-target.md");

    const result = await lifecycle.reserveRestoreTarget(
      localFileId,
      "notes/second-target.md",
    );

    expect(result).toEqual({
      outcome: "reserved",
      priorNormalizedPath: "notes/restore-me.md",
    });
    const reserved = database.readAll(
      "select normalized_path, restore_prior_path from local_files;",
    );
    expect(reserved[0]?.values[0]).toEqual([
      "notes/second-target.md",
      "notes/restore-me.md",
    ]);
    database.close();
  });

  it("refuses a target occupied by another tracked source row and leaves both rows untouched", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId } = await seedTombstonedFile(
      repository,
      lifecycle,
      "notes/restore-me.md",
    );
    await captureAndCommit(repository, "notes/occupied.md", fingerprintOf("c2"));

    const result = await lifecycle.reserveRestoreTarget(localFileId, "notes/occupied.md");

    expect(result).toEqual({ outcome: "refused", reason: "restore_target_occupied" });
    const tombstonedRow = database.readAll(
      "select normalized_path, lifecycle_state, restore_prior_path from local_files where local_file_id = $id".replace(
        "$id",
        `'${localFileId}'`,
      ),
    );
    expect(tombstonedRow[0]?.values[0]).toEqual(["notes/restore-me.md", "tombstoned", null]);
    expect(repository.readLocalFileByPath("notes/occupied.md")).not.toBeNull();
    database.close();
  });

  it("refuses a reservation while a restore event of the row is still non-terminal", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId, deleteEventId } = await seedTombstonedFile(
      repository,
      lifecycle,
      "notes/restore-me.md",
    );
    await lifecycle.reserveRestoreTarget(localFileId, "notes/restore-target.md");
    const reservedFile = requireLocalFile(
      repository.readLocalFileByPath("notes/restore-target.md"),
    );
    await lifecycle.recordLifecycleEvent(
      operandsFor("restore", {
        targetLocator: "notes/restore-target.md",
        tombstoneId: "66666666-6666-4666-8666-666666666666",
        predecessorEventId: deleteEventId,
      }),
      {
        localFile: reservedFile,
        tombstoneId: "66666666-6666-4666-8666-666666666666",
      },
    );

    const result = await lifecycle.reserveRestoreTarget(
      localFileId,
      "notes/another-target.md",
    );

    expect(result).toEqual({ outcome: "refused", reason: "restore_already_pending" });
    const row = database.readAll(
      "select normalized_path, lifecycle_state from local_files;",
    );
    expect(row[0]?.values[0]).toEqual(["notes/restore-target.md", "restore_pending"]);
    database.close();
  });

  it("releases a queued phantom create at the target inside the reservation transaction", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId } = await seedTombstonedFile(
      repository,
      lifecycle,
      "notes/restore-me.md",
    );
    const phantomCapture = await repository.recordCapture({
      normalizedPath: "notes/staged.md",
      fingerprint: fingerprintOf("c3"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (phantomCapture.outcome === "capture_refused") {
      throw new Error("expected a recorded phantom capture");
    }
    const phantomFileId = phantomCapture.localFile.localFileId;

    const result = await lifecycle.reserveRestoreTarget(localFileId, "notes/staged.md");

    expect(result).toEqual({
      outcome: "reserved",
      priorNormalizedPath: "notes/restore-me.md",
    });
    const phantomRow = database.readAll(
      "select count(*) from local_files where local_file_id = $id".replace(
        "$id",
        `'${phantomFileId}'`,
      ),
    );
    expect(phantomRow[0]?.values[0]?.[0]).toBe(0);
    const phantomEvents = database.readAll(
      "select count(*) from journal_events where local_file_id = $id".replace(
        "$id",
        `'${phantomFileId}'`,
      ),
    );
    expect(phantomEvents[0]?.values[0]?.[0]).toBe(0);
    const reservedRow = database.readAll(
      "select normalized_path, lifecycle_state, restore_prior_path from local_files where local_file_id = $id".replace(
        "$id",
        `'${localFileId}'`,
      ),
    );
    expect(reservedRow[0]?.values[0]).toEqual([
      "notes/staged.md",
      "restore_pending",
      "notes/restore-me.md",
    ]);
    database.close();
  });

  it("refuses a target whose phantom create upload is already in flight", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId } = await seedTombstonedFile(
      repository,
      lifecycle,
      "notes/restore-me.md",
    );
    const phantomCapture = await repository.recordCapture({
      normalizedPath: "notes/inflight.md",
      fingerprint: fingerprintOf("c4"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (phantomCapture.outcome === "capture_refused") {
      throw new Error("expected a recorded phantom capture");
    }
    await database.runSerializedMutation((session) => {
      session.exec(
        "update journal_events set state = 'uploading' where event_id = $id".replace(
          "$id",
          `'${phantomCapture.event.eventId}'`,
        ),
      );
    });

    const result = await lifecycle.reserveRestoreTarget(localFileId, "notes/inflight.md");

    expect(result).toEqual({ outcome: "refused", reason: "restore_target_busy" });
    expect(repository.readLocalFileByPath("notes/inflight.md")).not.toBeNull();
    const row = database.readAll(
      "select normalized_path, lifecycle_state from local_files where local_file_id = $id".replace(
        "$id",
        `'${localFileId}'`,
      ),
    );
    expect(row[0]?.values[0]).toEqual(["notes/restore-me.md", "tombstoned"]);
    database.close();
  });

  it("releases an explicit reservation back to the prior path in one transaction", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId } = await seedTombstonedFile(
      repository,
      lifecycle,
      "notes/restore-me.md",
    );
    await lifecycle.reserveRestoreTarget(localFileId, "notes/restore-target.md");

    await lifecycle.releaseRestoreTarget(localFileId);

    const row = database.readAll(
      "select normalized_path, lifecycle_state, restore_prior_path, open_tombstone_id from local_files;",
    );
    expect(row[0]?.values[0]).toEqual([
      "notes/restore-me.md",
      "tombstoned",
      null,
      "66666666-6666-4666-8666-666666666666",
    ]);
    expect(repository.readLocalFileByPath("notes/restore-target.md")).toBeNull();
    database.close();
  });

  it("lists tombstoned and reserved rows as restorable, never restored ones", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId } = await seedTombstonedFile(
      repository,
      lifecycle,
      "notes/restore-me.md",
    );
    expect(repository.readRestorableLocalFileIds()).toEqual([localFileId]);

    await lifecycle.reserveRestoreTarget(localFileId, "notes/restore-target.md");
    expect(repository.readRestorableLocalFileIds()).toEqual([localFileId]);

    await lifecycle.consumeRestoreSuccessor(localFileId);
    expect(repository.readRestorableLocalFileIds()).toEqual([]);
    database.close();
  });
});

describe("LifecycleRepository restore record and commit path binding", () => {
  async function seedReservedRestore(
    repository: JournalRepository,
    lifecycle: LifecycleRepository,
    priorPath: string,
    targetPath: string,
  ): Promise<{ localFileId: string; restoreEventId: string }> {
    await captureAndCommit(repository, priorPath, fingerprintOf("d1"));
    const file = requireLocalFile(repository.readLocalFileByPath(priorPath));
    const deleteResult = await lifecycle.recordLifecycleEvent(
      operandsFor("delete", { expectedLocator: priorPath }),
      {
        localFile: file,
        tombstoneId: "77777777-7777-4777-8777-777777777777",
      },
    );
    await lifecycle.reserveRestoreTarget(file.localFileId, targetPath);
    const reservedFile = requireLocalFile(repository.readLocalFileByPath(targetPath));
    const restoreResult = await lifecycle.recordLifecycleEvent(
      operandsFor("restore", {
        targetLocator: targetPath,
        tombstoneId: "77777777-7777-4777-8777-777777777777",
        predecessorEventId: deleteResult.event.eventId,
      }),
      {
        localFile: reservedFile,
        tombstoneId: "77777777-7777-4777-8777-777777777777",
      },
    );
    return { localFileId: file.localFileId, restoreEventId: restoreResult.event.eventId };
  }

  it("records a restore event as restore_pending with the tombstone retained (never restored)", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId } = await seedReservedRestore(
      repository,
      lifecycle,
      "notes/prior.md",
      "notes/target.md",
    );

    const row = database.readAll(
      "select normalized_path, lifecycle_state, open_tombstone_id from local_files where local_file_id = $id".replace(
        "$id",
        `'${localFileId}'`,
      ),
    );
    expect(row[0]?.values[0]).toEqual([
      "notes/target.md",
      "restore_pending",
      "77777777-7777-4777-8777-777777777777",
    ]);
    database.close();
  });

  it("rebinds normalized_path to the restore target and clears the prior path on the committed receipt", async () => {
    const { database, repository, lifecycle } = createOpenedJournal();
    const { localFileId, restoreEventId } = await seedReservedRestore(
      repository,
      lifecycle,
      "notes/prior.md",
      "notes/target.md",
    );

    await lifecycle.recordLifecycleCommittedReceipt(restoreEventId);
    await lifecycle.consumeRestoreSuccessor(localFileId);

    const row = database.readAll(
      "select normalized_path, lifecycle_state, open_tombstone_id, restore_prior_path from local_files where local_file_id = $id".replace(
        "$id",
        `'${localFileId}'`,
      ),
    );
    expect(row[0]?.values[0]).toEqual(["notes/target.md", "restored", null, null]);
    expect(repository.readLocalFileByPath("notes/prior.md")).toBeNull();
    expect(
      requireLocalFile(repository.readLocalFileByPath("notes/target.md")).localFileId,
    ).toBe(localFileId);
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
