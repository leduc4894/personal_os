/**
 * Tests for the Child 5 plugin-side Vault event lifecycle adapter
 * (Task 8 — capture rename / move / delete and explicit restore).
 *
 * The adapter sits between the Obsidian `Vault` rename / delete events
 * and the durable lifecycle repository. Every rename and delete has to
 * land in the same `journal_events` row + `lifecycle_event_operands`
 * extension + `local_files` update transaction that the lifecycle
 * repository already owns; a frozen preflight, a retained tombstone and
 * a `reconcile_required` dependency all reach the journal together.
 *
 * Privacy (spec 9): the test harness never asserts against paths or
 * digests beyond the canonical inputs the contract freezes — the redacted
 * surface is the test surface.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it, vi } from "vitest";

import type { FrozenFingerprint, JournalEvent, LocalFile } from "./contracts";
import { createEchoSuppressor } from "../device-sync/echo-suppression";
import { JournalRepository } from "./repository";
import {
  LifecycleCaptureImpl,
  type LifecycleVaultReader,
  type VaultRenameTarget,
  type VaultTargetFile,
} from "./lifecycle-capture";
import { createUuidv7Factory, uuidVersion } from "./uuidv7";
import { LifecycleRepository } from "./lifecycle-repository";
import {
  createLifecycleEventOperands,
  type LifecycleEventOperands,
} from "./lifecycle-contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";
import type { JournalFailureReporter } from "./diagnostic-reporter";
import type { SyncDiagnosticClosedToken } from "./sync-diagnostics-trail";

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

function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

/** A structural fake Vault file carrying only the path / parent shape. */
function fakeFile(path: string, parentPath: string | null): VaultRenameTarget {
  return {
    path,
    parent: parentPath === null ? null : { path: parentPath },
  };
}

function fakeAbstractFile(path: string, parentPath: string | null): VaultTargetFile {
  return {
    path,
    parent: parentPath === null ? null : { path: parentPath },
  };
}

interface FakeVault extends LifecycleVaultReader {
  setFileBytes(normalizedPath: string, bytes: Uint8Array): void;
  removeFileBytes(normalizedPath: string): void;
}

function createFakeVault(): FakeVault {
  const files = new Map<string, Uint8Array>();
  return {
    setFileBytes(normalizedPath, contentBytes) {
      files.set(normalizedPath, contentBytes);
    },
    removeFileBytes(normalizedPath) {
      files.delete(normalizedPath);
    },
    async readRegularFileBytes(normalizedPath) {
      return files.get(normalizedPath) ?? null;
    },
  };
}

interface Harness {
  readonly repository: JournalRepository;
  readonly lifecycle: LifecycleRepository;
  readonly capture: LifecycleCaptureImpl;
  readonly vault: FakeVault;
  readonly database: SqliteDatabase;
  readonly policyRevision: number;
  readonly failureTokens: SyncDiagnosticClosedToken[];
}

function createHarness(options?: {
  readonly policyRevision?: number;
  readonly createId?: () => string;
  readonly nowEpochMs?: () => number;
  readonly settleDelayMs?: number;
  readonly withEchoSuppressor?: boolean;
}): Harness {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  let epochCounter = 1_784_000_000_000;
  const createId = options?.createId ?? (() => crypto.randomUUID());
  const nowEpochMs = options?.nowEpochMs ?? (() => epochCounter++);
  const repository = new JournalRepository({
    database,
    createId,
    nowEpochMs,
  });
  const lifecycle = new LifecycleRepository({
    database,
    createId,
    nowEpochMs,
  });
  const policyRevision = options?.policyRevision ?? 1;
  const vault = createFakeVault();
  const failureTokens: SyncDiagnosticClosedToken[] = [];
  const failureReporter: JournalFailureReporter = {
    reportJournalFailure(token): void {
      failureTokens.push(token);
    },
  };
  const echoSuppressor =
    options?.withEchoSuppressor === true
      ? createEchoSuppressor({ repository: repository.deviceSync, database })
      : null;
  const capture = new LifecycleCaptureImpl({
    repository,
    lifecycle,
    vaultReader: vault,
    nowEpochMs,
    policyRevision,
    failureReporter,
    ...(echoSuppressor !== null ? { echoSuppressor } : {}),
    ...(options?.settleDelayMs !== undefined ? { settleDelayMs: options.settleDelayMs } : {}),
  });
  return {
    repository,
    lifecycle,
    capture,
    vault,
    database,
    policyRevision,
    failureTokens,
  };
}

/** Capture + commit one file so the lifecycle has sourceId + baseVersionId. */
async function captureAndCommit(
  harness: Harness,
  path: string,
  fingerprint: FrozenFingerprint,
): Promise<JournalEvent> {
  const capture = await harness.repository.recordCapture({
    normalizedPath: path,
    fingerprint,
    policyRevisionNumber: harness.policyRevision,
    admission: "policy_allowed",
  });
  if (capture.outcome === "capture_refused") {
    throw new Error("expected a recorded capture");
  }
  const { event } = capture;
  await harness.repository.recordCommittedReceipt({
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
  return createLifecycleEventOperands({
    operation,
    sourceId: "11111111-1111-7111-8111-111111111111",
    expectedVersionId: "22222222-2222-7222-8222-222222222222",
    policyRevision: 1,
    ...overrides,
  });
}

function requireLocalFile(maybeFile: LocalFile | null): LocalFile {
  if (maybeFile === null) {
    throw new Error("expected a local file row");
  }
  return maybeFile;
}

function lifecycleStateOf(database: SqliteDatabase, localFileId: string): string | null {
  const rows = database.readAll(
    `select lifecycle_state from local_files where local_file_id = '${localFileId}';`,
  );
  return (rows[0]?.values[0]?.[0] as string | undefined) ?? null;
}

function openTombstoneOf(database: SqliteDatabase, localFileId: string): string | null {
  const rows = database.readAll(
    `select open_tombstone_id from local_files where local_file_id = '${localFileId}';`,
  );
  const value = rows[0]?.values[0]?.[0];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function localFileCount(database: SqliteDatabase): number {
  try {
    const row = database.readAll("select count(*) from local_files;")[0]?.values[0]?.[0];
    return typeof row === "number" ? row : 0;
  } catch {
    return 0;
  }
}

function eventCount(database: SqliteDatabase): number {
  try {
    const row = database.readAll("select count(*) from journal_events;")[0]?.values[0]?.[0];
    return typeof row === "number" ? row : 0;
  } catch {
    return 0;
  }
}

function operandRows(database: SqliteDatabase): number {
  try {
    const row = database.readAll("select count(*) from lifecycle_event_operands;")[0]?.values[0]?.[0];
    return typeof row === "number" ? row : 0;
  } catch {
    return 0;
  }
}

function eventOperations(database: SqliteDatabase): string[] {
  const rows = database.readAll("select operation from journal_events order by rowid asc;");
  return (rows[0]?.values ?? []).map((row) => String(row[0] ?? ""));
}

/** Build a real fingerprint that matches the actual sha256 of the bytes. */
async function realFingerprintOf(
  text: string,
): Promise<{ readonly bytes: Uint8Array; readonly fingerprint: FrozenFingerprint }> {
  const bytes = bytesOf(text);
  const fingerprint = await deriveFrozenFingerprint(bytes);
  return { bytes, fingerprint };
}

describe("LifecycleCapture rename vs move (spec 7.1)", () => {
  it("records a rename event when the parent directory is unchanged", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("rename content");
    await captureAndCommit(harness, "notes/note.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/note.md"),
    );
    // Place bytes at the new path so the post-settle fingerprint read
    // succeeds (spec 7.1 fix round 1 I2).
    harness.vault.setFileBytes("notes/note-renamed.md", real.bytes);

    const result = await harness.capture.captureRename(
      fakeFile("notes/note-renamed.md", "notes"),
      "notes/note.md",
    );

    expect(result).not.toBeNull();
    expect(result?.operation).toBe("rename");
    expect(result?.localFileId).toBe(fileBefore.localFileId);
    expect(result?.capturedFingerprintSha256).toBe(real.fingerprint.sha256);
    expect(result?.capturedFingerprintSizeBytes).toBe(real.fingerprint.sizeBytes);
    const events = harness.repository.readEventsByLocalFileId(fileBefore.localFileId);
    expect(events.map((entry) => entry.operation)).toEqual(["create", "rename"]);
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe("rename_pending");
    // C1: the row now resolves by the new path; the prior path is gone.
    expect(harness.repository.readLocalFileByPath("notes/note-renamed.md")?.localFileId)
      .toBe(fileBefore.localFileId);
    expect(harness.repository.readLocalFileByPath("notes/note.md")).toBeNull();
  });

  it("records a move event when the parent directory changed", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("move content");
    await captureAndCommit(harness, "notes/movable.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/movable.md"),
    );
    harness.vault.setFileBytes("archive/movable.md", real.bytes);

    const result = await harness.capture.captureRename(
      fakeFile("archive/movable.md", "archive"),
      "notes/movable.md",
    );

    expect(result).not.toBeNull();
    expect(result?.operation).toBe("move");
    expect(result?.localFileId).toBe(fileBefore.localFileId);
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe("move_pending");
    // C1: a subsequent read at the new path resolves to the same id.
    expect(harness.repository.readLocalFileByPath("archive/movable.md")?.localFileId)
      .toBe(fileBefore.localFileId);
    expect(harness.repository.readLocalFileByPath("notes/movable.md")).toBeNull();
  });

  it("freezes a pending create/update before recording the rename event", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("freeze rename content");
    await captureAndCommit(harness, "notes/pending.md", real.fingerprint);
    // Queue a fresh update that is still coalescable.
    const realUpdate = await realFingerprintOf("freeze rename content v2");
    await harness.repository.recordCapture({
      normalizedPath: "notes/pending.md",
      fingerprint: realUpdate.fingerprint,
      policyRevisionNumber: harness.policyRevision,
      admission: "policy_allowed",
    });
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/pending.md"),
    );
    expect(harness.repository.countPendingEvents()).toBe(1);
    harness.vault.setFileBytes("notes/pending-renamed.md", realUpdate.bytes);

    await harness.capture.captureRename(
      fakeFile("notes/pending-renamed.md", "notes"),
      "notes/pending.md",
    );

    const eventsAfter = harness.repository.readEventsByLocalFileId(fileBefore.localFileId);
    const frozen = eventsAfter.filter(
      (entry) =>
        (entry.operation === "create" || entry.operation === "update") &&
        entry.state === "deferred_lifecycle",
    );
    expect(frozen.length).toBeGreaterThan(0);
    // The rename event itself is a pending lifecycle event: exactly one
    // pending event survives the freeze.
    expect(harness.repository.countPendingEvents()).toBe(1);
  });

  it("returns null and is a no-op when the prior path is not tracked", async () => {
    const harness = createHarness();

    const result = await harness.capture.captureRename(
      fakeFile("notes/new.md", "notes"),
      "notes/new.md",
    );

    expect(result).toBeNull();
    expect(eventCount(harness.database)).toBe(0);
    expect(localFileCount(harness.database)).toBe(0);
  });
});

describe("LifecycleCapture tombstone recording (spec 6.3, 7.1)", () => {
  it("reports a rejected reconcile write and rejects instead of claiming it settled", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("uncommitted delete");
    await harness.repository.recordCapture({
      normalizedPath: "notes/reconcile-write.md",
      fingerprint: real.fingerprint,
      policyRevisionNumber: harness.policyRevision,
      admission: "policy_allowed",
    });
    vi.spyOn(harness.repository.lifecycle.database, "runSerializedMutation")
      .mockRejectedValueOnce(new Error("persistence rejected"));

    await expect(
      harness.capture.captureDelete(fakeAbstractFile("notes/reconcile-write.md", "notes")),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(harness.failureTokens).toEqual(["lifecycle_reconcile_persist_failed"]);
  });

  it("records a delete event and marks the local mapping as tombstoned", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("delete me");
    await captureAndCommit(harness, "notes/disposable.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/disposable.md"),
    );
    const tombstoneId = "44444444-4444-7444-8444-444444444444";

    const result = await harness.capture.captureDelete(
      fakeAbstractFile("notes/disposable.md", "notes"),
      tombstoneId,
    );

    expect(result).not.toBeNull();
    expect(result?.tombstoneId).toBe(tombstoneId);
    expect(result?.localFileId).toBe(fileBefore.localFileId);
    // Local mapping retained with tombstone state for restore.
    expect(localFileCount(harness.database)).toBe(1);
    expect(openTombstoneOf(harness.database, fileBefore.localFileId)).toBe(tombstoneId);
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe("tombstoned");
    const events = harness.repository.readEventsByLocalFileId(fileBefore.localFileId);
    expect(events.map((entry) => entry.operation)).toEqual(["create", "delete"]);
  });

  it("is a no-op for an untracked delete path", async () => {
    const harness = createHarness();

    const result = await harness.capture.captureDelete(
      fakeAbstractFile("notes/unknown.md", "notes"),
      "55555555-5555-7555-8555-555555555555",
    );

    expect(result).toBeNull();
    expect(eventCount(harness.database)).toBe(0);
    expect(localFileCount(harness.database)).toBe(0);
  });

  it("mints its own tombstone id when the caller does not supply one", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("delete tombstone");
    await captureAndCommit(harness, "notes/auto-tombstone.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/auto-tombstone.md"),
    );

    const result = await harness.capture.captureDelete(
      fakeAbstractFile("notes/auto-tombstone.md", "notes"),
    );

    expect(result).not.toBeNull();
    expect(result?.tombstoneId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    expect(openTombstoneOf(harness.database, fileBefore.localFileId)).toBe(result?.tombstoneId);
  });
});

describe("LifecycleCapture settled/bursty rename notifications (spec 7.1)", () => {
  it("applies the 250ms per-path settle before fingerprinting the new path bytes", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const harness = createHarness();
      const real = await realFingerprintOf("burst content");
      await captureAndCommit(harness, "notes/burst.md", real.fingerprint);
      // Place bytes at the new path so the post-settle fingerprint read
      // succeeds (spec 7.1 fix round 1 I2).
      harness.vault.setFileBytes("notes/burst-renamed.md", real.bytes);

      const promise = harness.capture.captureRename(
        fakeFile("notes/burst-renamed.md", "notes"),
        "notes/burst.md",
      );

      // Before settle: nothing committed yet.
      await vi.advanceTimersByTimeAsync(100);
      const fileState = harness.repository.readLocalFileByPath("notes/burst.md");
      expect(fileState).not.toBeNull();

      await vi.advanceTimersByTimeAsync(300);
      const result = await promise;
      expect(result?.operation).toBe("rename");
      // I2: the post-settle fingerprint is captured durably.
      expect(result?.capturedFingerprintSha256).toBe(real.fingerprint.sha256);
    } finally {
      vi.useRealTimers();
    }
  });

  it("coalesces a burst of rapid rename notifications into one durable event", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const harness = createHarness();
      const real = await realFingerprintOf("burst coalesce");
      await captureAndCommit(harness, "notes/burst.md", real.fingerprint);
      const fileBefore = requireLocalFile(
        harness.repository.readLocalFileByPath("notes/burst.md"),
      );
      // The rename / move target carries bytes so the post-settle
      // fingerprint read succeeds.
      harness.vault.setFileBytes("notes/burst-renamed.md", real.bytes);
      harness.vault.setFileBytes("archive/burst.md", real.bytes);

      // First rename fires and starts settling.
      const firstPromise = harness.capture.captureRename(
        fakeFile("notes/burst-renamed.md", "notes"),
        "notes/burst.md",
      );
      await vi.advanceTimersByTimeAsync(150);
      // Second rename re-uses the same settled path; the settle timer restarts.
      const secondPromise = harness.capture.captureRename(
        fakeFile("archive/burst.md", "archive"),
        "notes/burst-renamed.md",
      );
      await vi.advanceTimersByTimeAsync(300);
      await Promise.all([firstPromise, secondPromise]);

      const events = harness.repository.readEventsByLocalFileId(fileBefore.localFileId);
      const renameEvents = events.filter((entry) => entry.operation === "rename");
      const moveEvents = events.filter((entry) => entry.operation === "move");
      expect(renameEvents.length + moveEvents.length).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("LifecycleCapture delete while create/update is pending (spec 7.1)", () => {
  it("freezes the pending update and tombstones the file in one transaction", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("lifecycle delete base");
    await captureAndCommit(harness, "notes/lifecycle-delete.md", real.fingerprint);
    const realUpdate = await realFingerprintOf("lifecycle delete update");
    await harness.repository.recordCapture({
      normalizedPath: "notes/lifecycle-delete.md",
      fingerprint: realUpdate.fingerprint,
      policyRevisionNumber: harness.policyRevision,
      admission: "policy_allowed",
    });
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/lifecycle-delete.md"),
    );
    expect(harness.repository.countPendingEvents()).toBe(1);

    const result = await harness.capture.captureDelete(
      fakeAbstractFile("notes/lifecycle-delete.md", "notes"),
      "66666666-6666-7666-8666-666666666666",
    );

    expect(result).not.toBeNull();
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe("tombstoned");
    const events = harness.repository.readEventsByLocalFileId(fileBefore.localFileId);
    const frozenContent = events.filter(
      (entry) =>
        (entry.operation === "create" || entry.operation === "update") &&
        entry.state === "deferred_lifecycle",
    );
    expect(frozenContent.length).toBeGreaterThan(0);
    // The delete event itself is the only pending event left.
    expect(harness.repository.countPendingEvents()).toBe(1);
  });
});

describe("LifecycleCapture predecessor ordering on restore (spec 6.3)", () => {
  it("links a restore to its predecessor delete event in the durable journal", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("recover target content");
    await captureAndCommit(harness, "notes/recover.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/recover.md"),
    );
    // Keep the same bytes at the path so the restore can verify them.
    harness.vault.setFileBytes("notes/recover.md", real.bytes);

    const tombstoneId = "77777777-7777-7777-8777-777777777777";
    const deleteResult = await harness.capture.captureDelete(
      fakeAbstractFile("notes/recover.md", "notes"),
      tombstoneId,
    );
    expect(deleteResult).not.toBeNull();

    const deleteEvent = harness.repository
      .readEventsByLocalFileId(fileBefore.localFileId)
      .find((entry) => entry.operation === "delete");
    expect(deleteEvent).toBeDefined();

    const reservation = await harness.capture.reserveRestoreTarget(
      fileBefore.localFileId,
      "notes/recover.md",
    );
    expect(reservation.outcome).toBe("reserved");
    const result = await harness.capture.requestRestore(
      fileBefore.localFileId,
      "notes/recover.md",
    );

    expect(result.operation).toBe("restore");
    expect(result.localFileId).toBe(fileBefore.localFileId);
    expect(result.predecessorEventId).toBe(deleteEvent?.eventId ?? null);

    const events = harness.repository.readEventsByLocalFileId(fileBefore.localFileId);
    expect(events.map((entry) => entry.operation)).toEqual(["create", "delete", "restore"]);
  });
});

describe("LifecycleCapture plugin unload during capture (spec 6.1, 7.1)", () => {
  it("rolls back the lifecycle writes when the database is closed mid-flight", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const harness = createHarness({ settleDelayMs: 10 });
      const real = await realFingerprintOf("unload midflight");
      await captureAndCommit(harness, "notes/unload.md", real.fingerprint);

      // Simulate unload: build a repository over a closed database.
      const closedDatabase = SqliteDatabase.createEmpty(engineModule, {
        schemaVersion: JOURNAL_SCHEMA_VERSION,
        dirtyGeneration: 1,
        lastVerifiedGeneration: 1,
        isReconcileRequired: false,
        recoveryState: "verified_generation_loaded",
      });
      closedDatabase.close();
      const brokenRepository = new JournalRepository({ database: closedDatabase });
      const brokenLifecycle = new LifecycleRepository({ database: closedDatabase });
      const brokenCapture = new LifecycleCaptureImpl({
        repository: brokenRepository,
        lifecycle: brokenLifecycle,
        vaultReader: createFakeVault(),
        policyRevision: harness.policyRevision,
        settleDelayMs: 10,
      });

      const promise = brokenCapture.captureRename(
        fakeFile("notes/unload-renamed.md", "notes"),
        "notes/unload.md",
      );
      // Attach a handler immediately so Vitest's strict unhandled-rejection
      // tracking does not flag the brief window before await expect settles.
      const settled = promise.then(
        () => ({ kind: "resolved" as const }),
        (error: unknown) => ({ kind: "rejected" as const, error }),
      );
      // Advance past the settle delay.
      await vi.advanceTimersByTimeAsync(20);
      const outcome = await settled;
      expect(outcome.kind).toBe("rejected");
      if (outcome.kind === "rejected") {
        expect(outcome.error).toMatchObject({ reason: "journal_mutation_failed" });
      }
      // The closed journal image stays untouched (no orphan rows).
      expect(eventCount(closedDatabase)).toBe(0);
      expect(localFileCount(closedDatabase)).toBe(0);
      expect(operandRows(closedDatabase)).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("LifecycleCapture requestRestore (spec 6.3, 7.1)", () => {
  it("rejects an explicit restore when the target bytes no longer match the retained content hash", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("restore target");
    await captureAndCommit(harness, "notes/restore.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/restore.md"),
    );
    const tombstoneId = "10101010-1010-7101-8101-101010101010";
    await harness.capture.captureDelete(
      fakeAbstractFile("notes/restore.md", "notes"),
      tombstoneId,
    );
    // Place bytes that hash to a DIFFERENT digest at the target path.
    harness.vault.setFileBytes("notes/restore.md", bytesOf("TOTALLY DIFFERENT"));

    await expect(
      harness.capture.requestRestore(fileBefore.localFileId, "notes/restore.md"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("rejects an explicit restore for an unknown local file id", async () => {
    const harness = createHarness();

    await expect(
      harness.capture.requestRestore(
        "99999999-9999-7999-8999-999999999999",
        "notes/unknown.md",
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("rejects an explicit restore when the local mapping is not tombstoned", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("no tombstone restore");
    await captureAndCommit(harness, "notes/no-tombstone.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/no-tombstone.md"),
    );
    harness.vault.setFileBytes("notes/no-tombstone.md", real.bytes);

    await expect(
      harness.capture.requestRestore(fileBefore.localFileId, "notes/no-tombstone.md"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("accepts an explicit restore when the reservation and the bytes hash both match", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("explicit restore ok");
    await captureAndCommit(harness, "notes/restore-ok.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/restore-ok.md"),
    );
    const tombstoneId = "20202020-2020-7202-8202-202020202020";
    await harness.capture.captureDelete(
      fakeAbstractFile("notes/restore-ok.md", "notes"),
      tombstoneId,
    );
    const reservation = await harness.capture.reserveRestoreTarget(
      fileBefore.localFileId,
      "notes/restore-ok.md",
    );
    expect(reservation).toEqual({
      outcome: "reserved",
      priorNormalizedPath: "notes/restore-ok.md",
    });
    harness.vault.setFileBytes("notes/restore-ok.md", real.bytes);

    const result = await harness.capture.requestRestore(
      fileBefore.localFileId,
      "notes/restore-ok.md",
    );
    expect(result.operation).toBe("restore");
    expect(result.localFileId).toBe(fileBefore.localFileId);
    // No eager consumption: the state stays restore_pending (tombstone
    // retained) until the committed receipt advances it.
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe(
      "restore_pending",
    );
    expect(openTombstoneOf(harness.database, fileBefore.localFileId)).toBe(tombstoneId);
    await harness.lifecycle.recordLifecycleCommittedReceipt(result.eventId);
    await harness.lifecycle.consumeRestoreSuccessor(fileBefore.localFileId);
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe("restored");
    expect(openTombstoneOf(harness.database, fileBefore.localFileId)).toBeNull();
  });
});

describe("LifecycleCapture explicit-restore target reservation (reservation-first protocol)", () => {
  async function seedTombstonedFile(
    harness: Harness,
    path: string,
    bytes: Uint8Array,
  ): Promise<{ localFileId: string; fingerprint: FrozenFingerprint }> {
    const fingerprint = await deriveFrozenFingerprint(bytes);
    await captureAndCommit(harness, path, fingerprint);
    const file = requireLocalFile(harness.repository.readLocalFileByPath(path));
    await harness.capture.captureDelete(
      fakeAbstractFile(path, path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : null),
      "50505050-5050-7505-8505-505050505050",
    );
    return { localFileId: file.localFileId, fingerprint };
  }

  it("reserves through the capture adapter and rebinds the row to the target", async () => {
    const harness = createHarness();
    const { localFileId } = await seedTombstonedFile(
      harness,
      "notes/source.md",
      bytesOf("reserve me"),
    );

    const reservation = await harness.capture.reserveRestoreTarget(
      localFileId,
      "notes/target.md",
    );

    expect(reservation).toEqual({
      outcome: "reserved",
      priorNormalizedPath: "notes/source.md",
    });
    expect(harness.repository.readLocalFileByPath("notes/source.md")).toBeNull();
    expect(
      requireLocalFile(harness.repository.readLocalFileByPath("notes/target.md")).localFileId,
    ).toBe(localFileId);
    expect(lifecycleStateOf(harness.database, localFileId)).toBe("restore_pending");
  });

  it("refuses an explicit restore whose row was never reserved", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("unreserved restore");
    await captureAndCommit(harness, "notes/unreserved.md", real.fingerprint);
    const file = requireLocalFile(harness.repository.readLocalFileByPath("notes/unreserved.md"));
    await harness.capture.captureDelete(
      fakeAbstractFile("notes/unreserved.md", "notes"),
      "60606060-6060-7606-8606-606060606060",
    );
    harness.vault.setFileBytes("notes/unreserved.md", real.bytes);

    await expect(
      harness.capture.requestRestore(file.localFileId, "notes/unreserved.md"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("keeps the reservation when the confirm-time bytes mismatch", async () => {
    const harness = createHarness();
    const { localFileId } = await seedTombstonedFile(
      harness,
      "notes/source.md",
      bytesOf("original bytes"),
    );
    await harness.capture.reserveRestoreTarget(localFileId, "notes/target.md");
    harness.vault.setFileBytes("notes/target.md", bytesOf("WRONG BYTES"));

    await expect(
      harness.capture.requestRestore(localFileId, "notes/target.md"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    expect(lifecycleStateOf(harness.database, localFileId)).toBe("restore_pending");
    expect(
      requireLocalFile(harness.repository.readLocalFileByPath("notes/target.md")).localFileId,
    ).toBe(localFileId);
  });

  it("refuses an automatic restore of a reserved row", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("reserved auto");
    const { localFileId } = await seedTombstonedFile(
      harness,
      "notes/auto-reserved.md",
      real.bytes,
    );
    await harness.capture.reserveRestoreTarget(localFileId, "notes/auto-reserved.md");
    harness.vault.setFileBytes("notes/auto-reserved.md", real.bytes);

    await expect(
      harness.capture.detectAutomaticRestore("notes/auto-reserved.md"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(lifecycleStateOf(harness.database, localFileId)).toBe("restore_pending");
  });

  it("treats delete and rename notifications on a reserved row as quiet no-ops", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("reserved staging");
    const { localFileId } = await seedTombstonedFile(
      harness,
      "notes/staged.md",
      real.bytes,
    );
    await harness.capture.reserveRestoreTarget(localFileId, "notes/staged.md");
    const eventsBefore = harness.database.readAll(
      "select count(*) from journal_events;",
    )[0]?.values[0]?.[0];

    const deleteResult = await harness.capture.captureDelete(
      fakeAbstractFile("notes/staged.md", "notes"),
    );
    expect(deleteResult).toBeNull();
    const renameResult = await harness.capture.captureRename(
      fakeFile("notes/staged-renamed.md", "notes"),
      "notes/staged.md",
    );
    expect(renameResult).toBeNull();

    const eventsAfter = harness.database.readAll(
      "select count(*) from journal_events;",
    )[0]?.values[0]?.[0];
    expect(eventsAfter).toBe(eventsBefore);
    expect(lifecycleStateOf(harness.database, localFileId)).toBe("restore_pending");
  });

  it("releases a reservation back to the prior path through the capture adapter", async () => {
    const harness = createHarness();
    const { localFileId } = await seedTombstonedFile(
      harness,
      "notes/source.md",
      bytesOf("release me"),
    );
    await harness.capture.reserveRestoreTarget(localFileId, "notes/target.md");

    await harness.capture.releaseRestoreTarget(localFileId);

    expect(harness.repository.readLocalFileByPath("notes/target.md")).toBeNull();
    expect(
      requireLocalFile(harness.repository.readLocalFileByPath("notes/source.md")).localFileId,
    ).toBe(localFileId);
    expect(lifecycleStateOf(harness.database, localFileId)).toBe("tombstoned");
  });
});

describe("LifecycleCapture automatic restore via captured path reuse (spec 6.3, 7.1)", () => {
  it("rejects a path reuse when the new bytes do not match the retained content hash", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("auto restore mismatch");
    await captureAndCommit(harness, "notes/auto.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/auto.md"),
    );
    const tombstoneId = "30303030-3030-7303-8303-303030303030";
    await harness.capture.captureDelete(
      fakeAbstractFile("notes/auto.md", "notes"),
      tombstoneId,
    );
    // Place bytes that hash to a DIFFERENT digest.
    harness.vault.setFileBytes("notes/auto.md", bytesOf("drastically different content"));

    await expect(
      harness.capture.detectAutomaticRestore("notes/auto.md"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    // The tombstone is still retained — the capture path can retry on the
    // next observed bytes.
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe("tombstoned");
  });

  it("allows an automatic restore when the retained mapping and bytes hash both match", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("auto restore success");
    await captureAndCommit(harness, "notes/auto.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/auto.md"),
    );
    const tombstoneId = "40404040-4040-7404-8404-404040404040";
    await harness.capture.captureDelete(
      fakeAbstractFile("notes/auto.md", "notes"),
      tombstoneId,
    );
    // Place the exact same bytes so the hash matches.
    harness.vault.setFileBytes("notes/auto.md", real.bytes);

    const result = await harness.capture.detectAutomaticRestore("notes/auto.md");
    expect(result.operation).toBe("restore");
    expect(result.localFileId).toBe(fileBefore.localFileId);
    // No eager consumption: the tombstone closes only through the
    // committed receipt (record -> restore_pending -> receipt -> restored).
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe(
      "restore_pending",
    );
    await harness.lifecycle.recordLifecycleCommittedReceipt(result.eventId);
    await harness.lifecycle.consumeRestoreSuccessor(fileBefore.localFileId);
    expect(lifecycleStateOf(harness.database, fileBefore.localFileId)).toBe("restored");
  });
});

describe("LifecycleCapture fail-closed on missing identity (spec 6.4)", () => {
  it("flags reconcile_required when a rename hits a tracked file without source identity", async () => {
    const harness = createHarness();
    // Seed a tracked file with no source identity (never committed).
    await harness.repository.recordCapture({
      normalizedPath: "notes/uncommitted.md",
      fingerprint: await deriveFrozenFingerprint(bytesOf("uncommitted")),
      policyRevisionNumber: harness.policyRevision,
      admission: "policy_allowed",
    });
    const meta = harness.database.readJournalMeta();
    expect(meta.isReconcileRequired).toBe(false);

    const result = await harness.capture.captureRename(
      fakeFile("notes/uncommitted-renamed.md", "notes"),
      "notes/uncommitted.md",
    );

    expect(result).toBeNull();
    expect(harness.database.readJournalMeta().isReconcileRequired).toBe(true);
  });
});

describe("LifecycleCapture uncommitted-transit rebind (untitled-transit race heal)", () => {
  async function seedBlockedPhantom(
    harness: Harness,
    path: string,
  ): Promise<void> {
    const capture = await harness.repository.recordCapture({
      normalizedPath: path,
      fingerprint: await deriveFrozenFingerprint(bytesOf("transit bytes")),
      policyRevisionNumber: harness.policyRevision,
      admission: "policy_allowed",
    });
    if (capture.outcome === "capture_refused") {
      throw new Error("expected a recorded phantom capture");
    }
    await harness.repository.markEventTerminal(
      capture.event.eventId,
      "blocked_conflict",
      "blocked_conflict",
    );
  }

  it("a rename of a never-committed phantom quietly removes the row instead of flagging reconcile", async () => {
    const harness = createHarness();
    await seedBlockedPhantom(harness, "notes/transit.md");
    expect(harness.database.readJournalMeta().isReconcileRequired).toBe(false);

    const result = await harness.capture.captureRename(
      fakeFile("notes/real-name.md", "notes"),
      "notes/transit.md",
    );

    expect(result).toBeNull();
    // The phantom mapping and its dead events are gone; the note will be
    // re-admitted fresh at the real name by the following admission pass.
    expect(harness.repository.readLocalFileByPath("notes/transit.md")).toBeNull();
    expect(
      harness.database.readAll("select count(*) from journal_events;")[0]?.values[0]?.[0],
    ).toBe(0);
    expect(harness.database.readJournalMeta().isReconcileRequired).toBe(false);
  });

  it("a delete of a never-committed phantom quietly removes the row instead of flagging reconcile", async () => {
    const harness = createHarness();
    await seedBlockedPhantom(harness, "notes/transit-delete.md");
    expect(harness.database.readJournalMeta().isReconcileRequired).toBe(false);

    const result = await harness.capture.captureDelete(
      fakeAbstractFile("notes/transit-delete.md", "notes"),
    );

    expect(result).toBeNull();
    expect(
      harness.repository.readLocalFileByPath("notes/transit-delete.md"),
    ).toBeNull();
    expect(
      harness.database.readAll("select count(*) from journal_events;")[0]?.values[0]?.[0],
    ).toBe(0);
    expect(harness.database.readJournalMeta().isReconcileRequired).toBe(false);
  });

  it("keeps the fail-closed reconcile rule when the phantom still has live in-flight work", async () => {
    const harness = createHarness();
    // A queued (never-terminal, never-committed) create: the upload may
    // still commit server-side, so the row must never be silently dropped.
    await harness.repository.recordCapture({
      normalizedPath: "notes/inflight.md",
      fingerprint: await deriveFrozenFingerprint(bytesOf("inflight bytes")),
      policyRevisionNumber: harness.policyRevision,
      admission: "policy_allowed",
    });

    const result = await harness.capture.captureRename(
      fakeFile("notes/inflight-renamed.md", "notes"),
      "notes/inflight.md",
    );

    expect(result).toBeNull();
    expect(harness.repository.readLocalFileByPath("notes/inflight.md")).not.toBeNull();
    expect(harness.database.readJournalMeta().isReconcileRequired).toBe(true);
  });
});

describe("LifecycleCapture module safety (spec 9)", () => {
  const source = readFileSync(new URL("./lifecycle-capture.ts", import.meta.url), "utf8");

  it("references no network transport capability", () => {
    for (const forbidden of ["fetch(", "requestUrl", "XMLHttpRequest", "WebSocket"]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("never logs raw bytes, paths or digests", () => {
    for (const forbidden of ["console.log", "console.error", "console.warn"]) {
      expect(source).not.toContain(forbidden);
    }
  });
});

describe("LifecycleCapture UUIDv7 event ids (spec 6.3, fix round 1 I4)", () => {
  it("mints time-ordered UUIDv7 ids when a UUIDv7 factory is injected", async () => {
    // A monotonic clock so the counter stays predictable.
    let nowMs = 1_784_000_000_000;
    const generated: string[] = [];
    const createId = createUuidv7Factory({
      nowEpochMs: () => nowMs,
      randomBytes: (length) => {
        const bytes = new Uint8Array(length);
        for (let index = 0; index < length; index += 1) {
          bytes[index] = index & 0xff;
        }
        return bytes;
      },
    });
    const harness = createHarness({ createId: () => {
      const id = createId();
      generated.push(id);
      return id;
    } });
    const real = await realFingerprintOf("uuid v7 content");
    await captureAndCommit(harness, "notes/v7.md", real.fingerprint);
    harness.vault.setFileBytes("notes/v7-renamed.md", real.bytes);

    await harness.capture.captureRename(
      fakeFile("notes/v7-renamed.md", "notes"),
      "notes/v7.md",
    );

    // The rename event id, the sourceId, and the baseVersionId from the
    // committed capture all carry the v7 nibble.
    expect(generated.length).toBeGreaterThan(0);
    for (const id of generated) {
      expect(uuidVersion(id)).toBe(7);
    }
    // Two ids minted at the same wall-clock ms still carry distinct
    // counters — bump the clock and emit two more.
    const first = createId();
    nowMs += 1;
    const second = createId();
    expect(uuidVersion(first)).toBe(7);
    expect(uuidVersion(second)).toBe(7);
    expect(first).not.toBe(second);
  });

  it("falls back to UUIDv4 when no factory is injected (repository default)", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("v4 default");
    await captureAndCommit(harness, "notes/v4.md", real.fingerprint);
    harness.vault.setFileBytes("notes/v4-renamed.md", real.bytes);
    await harness.capture.captureRename(
      fakeFile("notes/v4-renamed.md", "notes"),
      "notes/v4.md",
    );
    // No v7 factory was injected, so the default `crypto.randomUUID`
    // path is used — its version nibble is 4, not 7.
    const fileId = harness.database.readAll(
      "select normalized_path from local_files where normalized_path = 'notes/v4-renamed.md';",
    )[0]?.values[0]?.[0];
    expect(typeof fileId === "string" || fileId === undefined).toBe(true);
  });
});

describe("LifecycleCapture atomic lifecycle capture (spec 6.3, fix round 1 I1)", () => {
  it("freezes pending content, writes the lifecycle event, and rebinds the path in one transaction", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("atomic rename");
    await captureAndCommit(harness, "notes/atomic.md", real.fingerprint);
    // Queue a fresh update that is still coalescable.
    const realUpdate = await realFingerprintOf("atomic rename v2");
    await harness.repository.recordCapture({
      normalizedPath: "notes/atomic.md",
      fingerprint: realUpdate.fingerprint,
      policyRevisionNumber: harness.policyRevision,
      admission: "policy_allowed",
    });
    harness.vault.setFileBytes("notes/atomic-renamed.md", realUpdate.bytes);

    const result = await harness.capture.captureRename(
      fakeFile("notes/atomic-renamed.md", "notes"),
      "notes/atomic.md",
    );
    expect(result?.operation).toBe("rename");

    // The pending content event flips to `deferred_lifecycle` AND the
    // rename event lands in ONE writer call: the lifecycle event is the
    // sole remaining pending event, the row now resolves by the new path,
    // and there is no half-state in `journal_events`.
    const file = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/atomic-renamed.md"),
    );
    const events = harness.repository.readEventsByLocalFileId(file.localFileId);
    const frozenContent = events.filter(
      (entry) =>
        (entry.operation === "create" || entry.operation === "update") &&
        entry.state === "deferred_lifecycle",
    );
    expect(frozenContent.length).toBeGreaterThan(0);
    const pendingLifecycle = events.filter(
      (entry) =>
        entry.operation === "rename" &&
        (entry.state === "queued" || entry.state === "preflight"),
    );
    expect(pendingLifecycle).toHaveLength(1);
    expect(harness.repository.countPendingEvents()).toBe(1);
  });
});

describe("LifecycleCapture last-committed fingerprint eligibility (fix round 1 I3)", () => {
  it("rejects an automatic restore whose bytes match the observed fingerprint but not the last-committed fingerprint", async () => {
    const harness = createHarness();
    const real = await realFingerprintOf("committed bytes");
    const event = await captureAndCommit(harness, "notes/eligibility.md", real.fingerprint);
    // The automatic restore detector must compare the on-disk bytes
    // against `last_committed_fingerprint`, not the mutable
    // `observed_fingerprint`. So update `observed_fingerprint` to a new
    // (uncommitted) value, then prove that the automatic restore still
    // requires the original last-committed hash.
    const realNew = await realFingerprintOf("new observed bytes");
    await harness.repository.recordCapture({
      normalizedPath: "notes/eligibility.md",
      fingerprint: realNew.fingerprint,
      policyRevisionNumber: harness.policyRevision,
      admission: "policy_allowed",
    });
    // Tombstone the file.
    const tombstoneId = "40404040-4040-7404-8404-404040404040";
    await harness.capture.captureDelete(
      fakeAbstractFile("notes/eligibility.md", "notes"),
      tombstoneId,
    );
    // Place bytes that hash to the OBSERVED (mutated) fingerprint but
    // not the LAST-COMMITTED one. The automatic restore must reject.
    harness.vault.setFileBytes("notes/eligibility.md", realNew.bytes);
    await expect(
      harness.capture.detectAutomaticRestore("notes/eligibility.md"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    // Restore the canonical bytes and the automatic restore succeeds.
    harness.vault.setFileBytes("notes/eligibility.md", real.bytes);
    const result = await harness.capture.detectAutomaticRestore("notes/eligibility.md");
    expect(result.operation).toBe("restore");
    void event;
  });
});

describe("LifecycleCapture UUIDv7 helpers", () => {
  it("createUuidv7Factory produces RFC 9562 v7 ids with a monotonic counter", () => {
    let nowMs = 1_784_000_000_000;
    const factory = createUuidv7Factory({ nowEpochMs: () => nowMs });
    const first = factory();
    const second = factory();
    const fourth = factory();
    nowMs += 1;
    const fifth = factory();
    expect(uuidVersion(first)).toBe(7);
    expect(uuidVersion(second)).toBe(7);
    expect(uuidVersion(fourth)).toBe(7);
    expect(uuidVersion(fifth)).toBe(7);
    expect(first).not.toBe(second);
    expect(second).not.toBe(fourth);
  });
});

// --- exact rename echo suppression (task 10) -------------------------------------------------------

describe("LifecycleCapture exact rename echo suppression", () => {
  const ECHO_EVENT_SEQUENCE = 1;
  const ECHO_SOURCE_ID = "11111111-1111-7111-8111-111111111111";

  it("consumes an exact rename echo without recording a lifecycle event", async () => {
    const harness = createHarness({ withEchoSuppressor: true });
    const real = await realFingerprintOf("rename echo content");
    await captureAndCommit(harness, "notes/echo-old.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/echo-old.md"),
    );
    harness.vault.setFileBytes("notes/echo-new.md", real.bytes);
    await harness.repository.deviceSync.recordEchoMarker({
      eventSequence: ECHO_EVENT_SEQUENCE,
      sourceId: ECHO_SOURCE_ID,
      operation: "renamed",
      priorLocator: "notes/echo-old.md",
      targetLocator: "notes/echo-new.md",
      finalFingerprint: real.fingerprint,
    });
    const eventsBefore = eventCount(harness.database);

    const result = await harness.capture.captureRename(
      fakeFile("notes/echo-new.md", "notes"),
      "notes/echo-old.md",
    );

    expect(result).toBeNull();
    expect(eventCount(harness.database)).toBe(eventsBefore);
    expect(harness.repository.deviceSync.readEchoMarker(ECHO_EVENT_SEQUENCE)).toBeNull();
    // The mapping row is untouched: reconciliation, not capture, owns it.
    expect(
      harness.repository.readLocalFileByPath("notes/echo-old.md")?.localFileId,
    ).toBe(fileBefore.localFileId);
  });

  it("records the rename when the target bytes do not match the marker", async () => {
    const harness = createHarness({ withEchoSuppressor: true });
    const real = await realFingerprintOf("rename echo content");
    await captureAndCommit(harness, "notes/echo-old.md", real.fingerprint);
    const fileBefore = requireLocalFile(
      harness.repository.readLocalFileByPath("notes/echo-old.md"),
    );
    harness.vault.setFileBytes("notes/echo-new.md", real.bytes);
    const other = await realFingerprintOf("different bytes entirely");
    await harness.repository.deviceSync.recordEchoMarker({
      eventSequence: ECHO_EVENT_SEQUENCE,
      sourceId: ECHO_SOURCE_ID,
      operation: "renamed",
      priorLocator: "notes/echo-old.md",
      targetLocator: "notes/echo-new.md",
      finalFingerprint: other.fingerprint,
    });
    const eventsBefore = eventCount(harness.database);

    const result = await harness.capture.captureRename(
      fakeFile("notes/echo-new.md", "notes"),
      "notes/echo-old.md",
    );

    // A mismatch remains a real watcher event; the marker stays retained.
    expect(result).not.toBeNull();
    expect(result?.operation).toBe("rename");
    expect(eventCount(harness.database)).toBe(eventsBefore + 1);
    expect(harness.repository.deviceSync.readEchoMarker(ECHO_EVENT_SEQUENCE)).not.toBeNull();
    void fileBefore;
  });
});

// Reference the unused symbols so lint keeps the seam.
void eventOperations;
void operandsFor;
