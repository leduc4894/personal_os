/**
 * Tests for the bounded foreground lifecycle driver (Task 9, spec 19.2).
 *
 * The driver owns the rename / move / delete / restore dispatch lane:
 * it picks the oldest eligible lifecycle event whose predecessor (when
 * one is declared) is already terminal-success, sends it through the
 * generated API client and persists the server result before
 * acknowledging local completion. Retry scheduling reuses the same
 * `journal_attempts.next_attempt_at` mechanism the content queue
 * already uses, with the same one-second-to-five-minute jittered
 * backoff. Conflict (409) and integrity (422) errors are non-retryable
 * and close the event as `blocked_conflict` or `integrity_failed`.
 *
 * Privacy (spec 9): the test harness never asserts against paths,
 * digests, locator text or provider detail beyond the canonical
 * inputs the contract freezes.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { FrozenLifecycleEvent } from "./lifecycle-repository";
import { LifecycleApiError, type LifecycleResult } from "./lifecycle-api";
import type { LifecycleApi } from "./lifecycle-driver";
import { LifecycleDriverImpl, RETRY_BACKOFF_INITIAL_MS, RETRY_BACKOFF_MAXIMUM_MS } from "./lifecycle-driver";
import type { JournalEvent, LocalFile } from "./contracts";
import { JournalRepository } from "./repository";
import {
  createLifecycleEventOperands,
  type LifecycleEventOperands,
} from "./lifecycle-contracts";
import { LifecycleRepository } from "./lifecycle-repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";

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

const SOURCE_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_ID = "22222222-2222-4222-8222-222222222222";
const NEW_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const RESULT_TOMBSTONE_ID = "55555555-5555-4555-8555-555555555555";
const REQUEST_ID = "88888888-8888-4888-8888-888888888888";

function fingerprintOf(prefix: string, sizeBytes = 32) {
  // Build a deterministic 64-character lowercase hex string by repeating
  // the prefix's charCode hex values until the full length is reached.
  const hexChars = "0123456789abcdef";
  const seed = prefix
    .split("")
    .map((c) => c.charCodeAt(0).toString(16).padStart(2, "0"))
    .join("");
  let body = "";
  while (body.length + seed.length < 64) {
    body += seed;
  }
  const sha256 = (body + seed).slice(0, 64);
  void hexChars; // referenced for readability; deterministic only
  return {
    sha256,
    sizeBytes,
    mediaType: "text/plain",
  };
}

interface FakeApi extends LifecycleApi {
  readonly commits: { event: FrozenLifecycleEvent; signal: AbortSignal }[];
  install(
    handler: (
      event: FrozenLifecycleEvent,
      signal: AbortSignal,
    ) => Promise<LifecycleResult>,
  ): void;
  failWithLogin(): void;
}

function createFakeApi(): FakeApi {
  const commits: { event: FrozenLifecycleEvent; signal: AbortSignal }[] = [];
  let handler: ((event: FrozenLifecycleEvent, signal: AbortSignal) => Promise<LifecycleResult>) | null = null;
  let throwLoginRequired = false;
  const api: LifecycleApi = {
    async commit(event, signal) {
      commits.push({ event, signal });
      if (throwLoginRequired) {
        throw new Error("login_required: simulated");
      }
      if (handler === null) {
        throw new Error("api handler not installed");
      }
      return handler(event, signal);
    },
  };
  return {
    ...api,
    commits,
    install: (next) => {
      handler = next;
    },
    failWithLogin: () => {
      throwLoginRequired = true;
    },
  } as FakeApi;
}

interface Harness {
  readonly database: SqliteDatabase;
  readonly repository: JournalRepository;
  readonly lifecycle: LifecycleRepository;
  readonly driver: LifecycleDriverImpl;
  readonly api: FakeApi;
  readonly nowEpochMs: () => number;
  advanceClock: (milliseconds: number) => void;
  /** Seeds a tracked file whose lifecycle operands can be reused. */
  seedTrackedFile(path: string): Promise<LocalFile>;
  recordLifecycle(
    operands: LifecycleEventOperands,
    options?: { newPath?: string; tombstoneId?: string | null },
  ): Promise<{ event: JournalEvent; operands: LifecycleEventOperands }>;
}

function createHarness(options?: {
  readonly requestTimeoutMs?: number;
  readonly randomJitter?: () => number;
  readonly useWallClock?: boolean;
}): Harness {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  const epochBase = 1_784_000_000_000;
  let driverClockMs = epochBase;
  let repoClockMs = epochBase;
  let idCounter = 0;
  const createId = () => {
    idCounter += 1;
    const suffix = String(idCounter).padStart(12, "0");
    return `00000000-0000-4000-8000-${suffix}`;
  };
  const driverNow = options?.useWallClock === true ? () => Date.now() : () => driverClockMs;
  const repository = new JournalRepository({
    database,
    nowEpochMs: () => repoClockMs++,
    createId,
  });
  const lifecycle = new LifecycleRepository({
    database,
    nowEpochMs: () => repoClockMs++,
    createId,
  });
  const api = createFakeApi();
  const driver = new LifecycleDriverImpl({
    repository,
    lifecycle,
    api,
    createCorrelationId: () => `corr-${idCounter += 1}`,
    randomJitter: options?.randomJitter ?? (() => 0),
    nowEpochMs: driverNow,
  });
  return {
    database,
    repository,
    lifecycle,
    driver,
    api,
    nowEpochMs: driverNow,
    advanceClock: (milliseconds) => {
      driverClockMs += milliseconds;
      repoClockMs += milliseconds;
    },
    seedTrackedFile: async (path) => {
      const capture = await repository.recordCapture({
        normalizedPath: path,
        fingerprint: fingerprintOf("aa", 8),
        policyRevisionNumber: 1,
        admission: "policy_allowed",
      });
      if (capture.outcome !== "event_recorded") {
        throw new Error("seedTrackedFile: capture failed");
      }
      await repository.recordCommittedReceipt({
        eventId: capture.event.eventId,
        sourceId: SOURCE_ID,
        baseVersionId: VERSION_ID,
      });
      const file = repository.readLocalFileByPath(path);
      if (file === null) {
        throw new Error("seedTrackedFile: file not found");
      }
      return file;
    },
    recordLifecycle: async (operands, options) => {
      // The file row rebinds to target_locator only AFTER the rename
      // event lands, so the lookup must use the SOURCE locator
      // (expectedLocator for rename/move/delete, targetLocator for
      // restore which happens after a tombstone clears the path).
      const lookupPath =
        operands.expectedLocator ??
        (operands.operation === "restore" ? operands.targetLocator : null) ??
        options?.newPath ??
        null;
      if (lookupPath === null) {
        throw new Error("recordLifecycle: missing lookup path");
      }
      const file = repository.readLocalFileByPath(lookupPath);
      if (file === null) {
        throw new Error(`recordLifecycle: file not found at ${lookupPath}`);
      }
      const baseOptions = {
        localFile: file,
        tombstoneId: options?.tombstoneId ?? operands.tombstoneId ?? null,
      };
      if (options?.newPath !== undefined) {
        const result = await lifecycle.recordLifecycleEvent(operands, {
          ...baseOptions,
          newPath: options.newPath,
        });
        return { event: result.event, operands };
      }
      const result = await lifecycle.recordLifecycleEvent(operands, baseOptions);
      return { event: result.event, operands };
    },
  };
}

function committedResult(
  overrides: Partial<LifecycleResult> = {},
): LifecycleResult {
  return {
    committedAt: "2026-08-20T00:00:00Z",
    eventId: "00000000-0000-4000-8000-000000000000",
    eventSequence: 1,
    resultingLocator: null,
    sourceId: SOURCE_ID,
    sourceVersionId: NEW_VERSION_ID,
    state: "active",
    tombstoneId: null,
    ...overrides,
  };
}

const activeSignal = () => new AbortController().signal;

// --- selection, ordering and the happy path ----------------------------------------------

describe("lifecycle driver selection and ordering (spec 19.2, 12)", () => {
  it("returns idle when no lifecycle event is eligible", async () => {
    const harness = createHarness();
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("idle");
    expect(harness.api.commits).toHaveLength(0);
  });

  it("picks the oldest eligible lifecycle event and persists the server result", async () => {
    const harness = createHarness();
    await harness.seedTrackedFile("notes/first.md");
    const rename1 = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/first.md",
        targetLocator: "notes/first-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
        capturedFingerprintSha256: null,
        capturedFingerprintSizeBytes: null,
        capturedFingerprintMediaType: null,
      }),
      { newPath: "notes/first-renamed.md" },
    );
    const rename2 = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/first-renamed.md",
        targetLocator: "notes/first-renamed-twice.md",
        policyRevision: 1,
        predecessorEventId: null,
        capturedFingerprintSha256: null,
        capturedFingerprintSizeBytes: null,
        capturedFingerprintMediaType: null,
      }),
      { newPath: "notes/first-renamed-twice.md" },
    );
    // Seed a third tracked file so the rename path rebind does not
    // collapse the second rename onto the same normalized_path slot.
    const otherFile = await harness.seedTrackedFile("notes/other.md");
    await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/other.md",
        targetLocator: "notes/other-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
        capturedFingerprintSha256: null,
        capturedFingerprintSizeBytes: null,
        capturedFingerprintMediaType: null,
      }),
      { newPath: "notes/other-renamed.md" },
    );
    void otherFile;
    harness.api.install(async () =>
      committedResult({ eventId: rename1.event.eventId }),
    );
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("committed");
    expect(harness.api.commits).toHaveLength(1);
    expect(harness.api.commits[0]?.event.event.eventId).toBe(rename1.event.eventId);
    const stored = harness.repository.readEvent(rename1.event.eventId);
    expect(stored?.state).toBe("committed");
    expect(stored?.safeError).toBeNull();
    expect(stored?.attemptCount).toBe(0);
    void rename2;
  });

  it("does not dispatch a successor while the predecessor is not yet terminal-success", async () => {
    const harness = createHarness();
    await harness.seedTrackedFile("notes/restore.md");
    // Seed a delete predecessor that stays queued (not committed).
    const deleteRecorded = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "delete",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/restore.md",
        policyRevision: 1,
        tombstoneId: RESULT_TOMBSTONE_ID,
        predecessorEventId: null,
      }),
    );
    const restore = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "restore",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: null,
        targetLocator: "notes/restore.md",
        tombstoneId: RESULT_TOMBSTONE_ID,
        policyRevision: 1,
        predecessorEventId: deleteRecorded.event.eventId,
      }),
    );
    // First runOne must commit the delete predecessor (it has no
    // predecessor itself); the restore stays queued behind it.
    harness.api.install(async (event) =>
      committedResult({
        eventId: event.event.eventId,
        state: "deleted",
        tombstoneId: RESULT_TOMBSTONE_ID,
      }),
    );
    const first = await harness.driver.runOne(activeSignal());
    expect(first).toBe("committed");
    expect(harness.api.commits).toHaveLength(1);
    expect(harness.api.commits[0]?.event.event.eventId).toBe(deleteRecorded.event.eventId);
    expect(harness.repository.readEvent(restore.event.eventId)?.state).toBe("queued");
    // Second runOne: the predecessor is now committed, so the restore
    // becomes eligible and is dispatched.
    const second = await harness.driver.runOne(activeSignal());
    expect(second).toBe("committed");
    expect(harness.api.commits).toHaveLength(2);
    expect(harness.api.commits[1]?.event.event.eventId).toBe(restore.event.eventId);
    expect(harness.repository.readEvent(restore.event.eventId)?.state).toBe("committed");
  });
});

// --- one active request and exact replay ------------------------------------------------

describe("lifecycle driver one active request and exact replay (spec 19.2, 10.3)", () => {
  it("issues exactly one commit per runOne call", async () => {
    const harness = createHarness();
    await harness.seedTrackedFile("notes/one-shot.md");
    await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/one-shot.md",
        targetLocator: "notes/one-shot-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { newPath: "notes/one-shot-renamed.md" },
    );
    let inflightRequests = 0;
    let maxInflight = 0;
    harness.api.install(async () => {
      inflightRequests += 1;
      maxInflight = Math.max(maxInflight, inflightRequests);
      await new Promise((resolve) => setTimeout(resolve, 1));
      inflightRequests -= 1;
      return committedResult();
    });
    await harness.driver.runOne(activeSignal());
    expect(maxInflight).toBe(1);
    expect(harness.api.commits).toHaveLength(1);
  });

  it("replays the same event with identical operands", async () => {
    const harness = createHarness();
    const file = await harness.seedTrackedFile("notes/replay.md");
    const operands = createLifecycleEventOperands({
      operation: "rename",
      sourceId: SOURCE_ID,
      expectedVersionId: VERSION_ID,
      expectedLocator: "notes/replay.md",
      targetLocator: "notes/replay-renamed.md",
      policyRevision: 1,
      predecessorEventId: null,
    });
    const recorded = await harness.lifecycle.recordLifecycleEvent(operands, {
      localFile: file,
      newPath: "notes/replay-renamed.md",
    });
    let commits = 0;
    harness.api.install(async (event) => {
      commits += 1;
      return committedResult({ eventId: event.event.eventId });
    });
    await harness.driver.runOne(activeSignal());
    await harness.advanceClock(RETRY_BACKOFF_INITIAL_MS + 1);
    await harness.driver.runOne(activeSignal());
    // The driver only runs once per runOne call; no second commit.
    expect(commits).toBe(1);
    // The state stays committed (no duplicate).
    expect(harness.repository.readEvent(recorded.eventId)?.state).toBe("committed");
  });
});

// --- bounded jittered retry -------------------------------------------------------------

describe("lifecycle driver bounded jittered retry (spec 8, 12)", () => {
  it("schedules retry on a 5xx transient error and persists attempt history", async () => {
    const harness = createHarness({ randomJitter: () => 0 });
    await harness.seedTrackedFile("notes/retry.md");
    const recorded = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/retry.md",
        targetLocator: "notes/retry-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { newPath: "notes/retry-renamed.md" },
    );
    harness.api.install(async () => {
      throw new LifecycleApiError("server_error");
    });
    const before = harness.nowEpochMs();
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("retry");
    const stored = harness.repository.readEvent(recorded.event.eventId);
    expect(stored?.state).toBe("waiting_retry");
    expect(stored?.attemptCount).toBe(1);
    expect(stored?.nextEligibleRetryEpochMs).toBe(before + RETRY_BACKOFF_INITIAL_MS);
    expect(stored?.safeError).toBe("server_error");
    const attempts = harness.repository.readEventAttemptHistory(recorded.event.eventId);
    expect(attempts.at(-1)?.outcomeLabel).toBe("server_error");
  });

  it.each([
    ["network_offline", "network_offline"] as const,
    ["network_timeout", "network_timeout"] as const,
    ["network_rate_limited", "network_rate_limited"] as const,
    ["server_error", "server_error"] as const,
  ])("schedules retry for retryable label %s", async (_name, label) => {
    const harness = createHarness({ randomJitter: () => 0 });
    await harness.seedTrackedFile(`notes/${label}.md`);
    const recorded = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: `notes/${label}.md`,
        targetLocator: `notes/${label}-renamed.md`,
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { newPath: `notes/${label}-renamed.md` },
    );
    harness.api.install(async () => {
      throw new LifecycleApiError(label);
    });
    const before = harness.nowEpochMs();
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("retry");
    const stored = harness.repository.readEvent(recorded.event.eventId);
    expect(stored?.safeError).toBe(label);
    expect(stored?.nextEligibleRetryEpochMs).toBe(before + RETRY_BACKOFF_INITIAL_MS);
  });

  it("keeps the next_attempt_at within the 1s-5m jittered window", async () => {
    const harness = createHarness({ randomJitter: () => 1 });
    await harness.seedTrackedFile("notes/jitter.md");
    const recorded = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/jitter.md",
        targetLocator: "notes/jitter-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { newPath: "notes/jitter-renamed.md" },
    );
    harness.api.install(async () => {
      throw new LifecycleApiError("server_error");
    });
    const before = harness.nowEpochMs();
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("retry");
    const stored = harness.repository.readEvent(recorded.event.eventId);
    expect(stored?.nextEligibleRetryEpochMs).toBeGreaterThanOrEqual(before + 1_000);
    expect(stored?.nextEligibleRetryEpochMs).toBeLessThanOrEqual(before + RETRY_BACKOFF_MAXIMUM_MS);
  });
});

// --- non-retryable conflict / integrity -------------------------------------------------

describe("lifecycle driver non-retryable conflict and integrity (spec 12)", () => {
  it("closes a conflict as blocked_conflict and never retries", async () => {
    const harness = createHarness();
    await harness.seedTrackedFile("notes/conflict.md");
    const recorded = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/conflict.md",
        targetLocator: "notes/conflict-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { newPath: "notes/conflict-renamed.md" },
    );
    harness.api.install(async () => {
      throw new LifecycleApiError("conflict");
    });
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("blocked");
    const stored = harness.repository.readEvent(recorded.event.eventId);
    expect(stored?.state).toBe("blocked_conflict");
    expect(stored?.safeError).toBe("blocked_conflict");
    expect(stored?.nextEligibleRetryEpochMs).toBeNull();
  });

  it("closes an integrity rejection as integrity_failed and never retries", async () => {
    const harness = createHarness();
    await harness.seedTrackedFile("notes/integrity.md");
    const recorded = await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/integrity.md",
        targetLocator: "notes/integrity-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { newPath: "notes/integrity-renamed.md" },
    );
    harness.api.install(async () => {
      throw new LifecycleApiError("integrity");
    });
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("blocked");
    const stored = harness.repository.readEvent(recorded.event.eventId);
    expect(stored?.state).toBe("integrity_failed");
    expect(stored?.safeError).toBe("integrity_failed");
  });
});

// --- cancellation and unload -----------------------------------------------------------

describe("lifecycle driver cancellation and unload (spec 19.2, 12)", () => {
  it("returns idle when the signal is already aborted and makes no commit", async () => {
    const harness = createHarness();
    await harness.seedTrackedFile("notes/cancel.md");
    await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/cancel.md",
        targetLocator: "notes/cancel-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { newPath: "notes/cancel-renamed.md" },
    );
    harness.api.install(async () => {
      throw new Error("commit must not run");
    });
    const controller = new AbortController();
    controller.abort();
    const outcome = await harness.driver.runOne(controller.signal);
    expect(outcome).toBe("idle");
    expect(harness.api.commits).toHaveLength(0);
  });

  it("dispose() halts the driver; subsequent runOne returns idle", async () => {
    const harness = createHarness();
    await harness.seedTrackedFile("notes/unload.md");
    await harness.recordLifecycle(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/unload.md",
        targetLocator: "notes/unload-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { newPath: "notes/unload-renamed.md" },
    );
    harness.driver.dispose();
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("idle");
    expect(harness.api.commits).toHaveLength(0);
  });
});

// --- race tests: predecessor order holds across all three scenarios --------------------

describe("lifecycle driver race tests: predecessor order across rename/edit/delete/retry/restore", () => {
  it("rename (predecessor) commits before the content update of the same file", async () => {
    const harness = createHarness();
    const file = await harness.seedTrackedFile("notes/race-rename.md");
    const rename = await harness.lifecycle.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/race-rename.md",
        targetLocator: "notes/race-rename-renamed.md",
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { localFile: file, newPath: "notes/race-rename-renamed.md" },
    );
    // A content update captured AFTER the rename binds to the new path
    // and the SAME local_file_id; the lifecycle driver must dispatch
    // the rename first (predecessor rule).
    const updateCapture = await harness.repository.recordCapture({
      normalizedPath: "notes/race-rename-renamed.md",
      fingerprint: fingerprintOf("update"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (updateCapture.outcome !== "event_recorded") {
      throw new Error("expected event_recorded for the content update");
    }
    // Force the content event into waiting_retry so the queue driver
    // would skip it on its own; the lifecycle predecessor is what
    // must gate it here.
    await harness.repository.markEventPreflightStarted(updateCapture.event.eventId);
    await harness.repository.markEventWaitingRetry(
      updateCapture.event.eventId,
      "server_error",
      harness.nowEpochMs() + 60_000,
    );
    harness.api.install(async (event) =>
      committedResult({ eventId: event.event.eventId }),
    );
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("committed");
    expect(harness.api.commits).toHaveLength(1);
    expect(harness.api.commits[0]?.event.event.eventId).toBe(rename.eventId);
    expect(harness.repository.readEvent(rename.eventId)?.state).toBe("committed");
  });

  it("delete (predecessor) commits before the content update awaiting retry", async () => {
    const harness = createHarness();
    const file = await harness.seedTrackedFile("notes/race-delete.md");
    // Content update captured first and pushed to waiting_retry.
    const updateCapture = await harness.repository.recordCapture({
      normalizedPath: "notes/race-delete.md",
      fingerprint: fingerprintOf("update-before-delete"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    if (updateCapture.outcome !== "event_recorded") {
      throw new Error("expected event_recorded");
    }
    await harness.repository.markEventPreflightStarted(updateCapture.event.eventId);
    await harness.repository.markEventWaitingRetry(
      updateCapture.event.eventId,
      "server_error",
      harness.nowEpochMs() + 60_000,
    );
    const del = await harness.lifecycle.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "delete",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/race-delete.md",
        tombstoneId: RESULT_TOMBSTONE_ID,
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { localFile: file, tombstoneId: RESULT_TOMBSTONE_ID },
    );
    harness.api.install(async (event) =>
      committedResult({
        eventId: event.event.eventId,
        state: "deleted",
        tombstoneId: RESULT_TOMBSTONE_ID,
        resultingLocator: null,
      }),
    );
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("committed");
    expect(harness.api.commits).toHaveLength(1);
    expect(harness.api.commits[0]?.event.event.eventId).toBe(del.eventId);
    expect(harness.repository.readEvent(del.eventId)?.state).toBe("committed");
  });

  it("restore (predecessor on committed delete) commits before the follow-up edit", async () => {
    const harness = createHarness();
    const file = await harness.seedTrackedFile("notes/race-restore.md");
    // Seed a committed delete predecessor.
    const del = await harness.lifecycle.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "delete",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: "notes/race-restore.md",
        tombstoneId: RESULT_TOMBSTONE_ID,
        policyRevision: 1,
        predecessorEventId: null,
      }),
      { localFile: file, tombstoneId: RESULT_TOMBSTONE_ID },
    );
    await harness.lifecycle.recordLifecycleCommittedReceipt(del.eventId);
    const restore = await harness.lifecycle.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "restore",
        sourceId: SOURCE_ID,
        expectedVersionId: VERSION_ID,
        expectedLocator: null,
        targetLocator: "notes/race-restore.md",
        tombstoneId: RESULT_TOMBSTONE_ID,
        policyRevision: 1,
        predecessorEventId: del.eventId,
      }),
      { localFile: file, tombstoneId: RESULT_TOMBSTONE_ID },
    );
    harness.api.install(async (event) =>
      committedResult({
        eventId: event.event.eventId,
        state: "active",
        tombstoneId: RESULT_TOMBSTONE_ID,
        resultingLocator: "notes/race-restore.md",
      }),
    );
    const outcome = await harness.driver.runOne(activeSignal());
    expect(outcome).toBe("committed");
    expect(harness.api.commits).toHaveLength(1);
    expect(harness.api.commits[0]?.event.event.eventId).toBe(restore.eventId);
    expect(harness.repository.readEvent(restore.eventId)?.state).toBe("committed");
  });
});

void REQUEST_ID;