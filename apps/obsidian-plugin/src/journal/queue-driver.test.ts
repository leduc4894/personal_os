/**
 * Tests of the bounded foreground queue driver (spec 8, 12).
 *
 * Every test drives the REAL journal (sql.js engine, real repository, real
 * fingerprint derivation) and the REAL hand-mirrored sync client over a
 * scripted raw transport, with fake clock, randomness and refresh seams.
 * The pinned behaviors: oldest-eligible-first selection with one active
 * content request, every state transition persisted before the next network
 * action, the frozen-fingerprint re-check before any byte is sent, exact
 * same-identity replay after a lost response, bounded jittered backoff for
 * offline/timeout/429/5xx, at most one refresh per pass with
 * queue-preserving login failure, and no run after unload/suspend with late
 * results discarded.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { JournalEvent, JournalMeta } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import {
  JournalQueueDriver,
  QUEUE_PASS_DEADLINE_MS,
  QUEUE_REQUEST_TIMEOUT_MS,
  RETRY_BACKOFF_INITIAL_MS,
  RETRY_BACKOFF_MAXIMUM_MS,
  computeRetryBackoffMs,
} from "./queue-driver";
import type { QueuePassSummary } from "./queue-driver";
import { JournalRepository } from "./repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";
import { createJournalSyncApi } from "./sync-api";
import type { SyncHttpRequest } from "./sync-api";

/** The real sql.js WebAssembly engine drives every driver test (spec 6.1). */
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

const ORIGIN = "https://sync.example.org";
const ACCESS_TOKEN = "at1.driver-test-access";
const OPERATION_ID = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-Zz";
const SOURCE_ID = "44444444-4444-4444-8444-444444444444";
const SOURCE_VERSION_ID = "55555555-5555-4555-8555-555555555555";

function successBody(data: unknown): string {
  return JSON.stringify({
    data,
    error: null,
    request_id: "66666666-6666-4666-8666-666666666666",
    warnings: [],
  });
}

function errorBody(code: string): string {
  return JSON.stringify({
    data: null,
    error: { code, message: "registered safe message", details: {}, retryable: false },
    request_id: "66666666-6666-4666-8666-666666666666",
    warnings: [],
  });
}

const COMMITTED_RECEIPT = successBody({
  result_kind: "committed",
  source_id: SOURCE_ID,
  source_version_id: SOURCE_VERSION_ID,
  content_version: 1,
  committed_at: "2026-08-18T00:00:00Z",
});

const SINGLE_PART_BODY = successBody({
  outcome: "single_part_upload",
  operation_id: OPERATION_ID,
  expires_at: "2026-08-18T01:00:00Z",
});

function committedReplayBody(): string {
  return successBody({
    outcome: "committed_replay",
    result: {
      result_kind: "committed",
      source_id: SOURCE_ID,
      source_version_id: SOURCE_VERSION_ID,
      content_version: 1,
      committed_at: "2026-08-18T00:00:00Z",
    },
  });
}

type RawResponse = { status: number; bodyText: string };

type PreflightHandler = (body: Record<string, unknown>) => Promise<RawResponse>;
type ContentHandler = (bytes: Uint8Array) => Promise<RawResponse>;

interface ScriptedTransport {
  readonly preflightRequests: SyncHttpRequest[];
  readonly preflightBodies: Record<string, unknown>[];
  readonly contentRequests: SyncHttpRequest[];
  readonly contentBytes: Uint8Array[];
  readonly maximumInFlightRequests: number;
}

/** The scripted raw transport behind the real sync client. */
function createScriptedHandlers(handlers: {
  preflight: PreflightHandler;
  content?: ContentHandler;
}): ScriptedTransport & {
  readonly transport: (request: SyncHttpRequest) => Promise<RawResponse>;
} {
  const preflightRequests: SyncHttpRequest[] = [];
  const preflightBodies: Record<string, unknown>[] = [];
  const contentRequests: SyncHttpRequest[] = [];
  const contentBytes: Uint8Array[] = [];
  let inFlightRequests = 0;
  let maximumInFlightRequests = 0;
  return {
    preflightRequests,
    preflightBodies,
    contentRequests,
    contentBytes,
    get maximumInFlightRequests(): number {
      return maximumInFlightRequests;
    },
    transport: async (request: SyncHttpRequest): Promise<RawResponse> => {
      inFlightRequests += 1;
      maximumInFlightRequests = Math.max(maximumInFlightRequests, inFlightRequests);
      try {
        if (request.method === "PUT") {
          const bytes = new Uint8Array(request.body as ArrayBuffer);
          contentRequests.push(request);
          contentBytes.push(bytes);
          const content = handlers.content;
          if (content === undefined) {
            throw new Error("unexpected content request");
          }
          return await content(bytes);
        }
        const body = JSON.parse(request.body as string) as Record<string, unknown>;
        preflightRequests.push(request);
        preflightBodies.push(body);
        return await handlers.preflight(body);
      } finally {
        inFlightRequests -= 1;
      }
    },
  };
}

interface DriverHarness {
  readonly repository: JournalRepository;
  readonly driver: JournalQueueDriver;
  readonly vaultBytes: Map<string, Uint8Array>;
  readonly refreshCalls: { count: number };
  setRefreshImplementation: (implementation: () => Promise<void>) => void;
  readonly nowEpochMs: () => number;
  advanceClock: (milliseconds: number) => void;
  installTransport: (
    handlers: { preflight: PreflightHandler; content?: ContentHandler },
  ) => ScriptedTransport;
}

function createHarness(options?: {
  readonly requestTimeoutMs?: number;
  readonly passDeadlineMs?: number;
  readonly useWallClock?: boolean;
}): DriverHarness {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  } satisfies JournalMeta);
  const epochBase = 1_784_000_000_000;
  let eventClockMs = epochBase;
  let driverClockMs = epochBase;
  let idCounter = 0;
  const repository = new JournalRepository({
    database,
    nowEpochMs: () => eventClockMs++,
    createId: () => {
      idCounter += 1;
      const suffix = String(idCounter).padStart(12, "0");
      return `00000000-0000-4000-8000-${suffix}`;
    },
  });
  const vaultBytes = new Map<string, Uint8Array>();
  const refreshCalls = { count: 0 };
  let refreshImplementation: () => Promise<void> = () => Promise.resolve();
  const accessToken: string | null = ACCESS_TOKEN;
  let activeTransport: ((request: SyncHttpRequest) => Promise<RawResponse>) | null = null;
  let correlationCounter = 0;
  const driverNowEpochMs = options?.useWallClock === true ? () => Date.now() : () => driverClockMs;
  const syncApi = createJournalSyncApi({
    transport: (request) => {
      const transport = activeTransport;
      if (transport === null) {
        throw new Error("no transport installed");
      }
      return transport(request);
    },
    resolveOrigin: () => ORIGIN,
    getAccessToken: () => accessToken,
  });
  const driver = new JournalQueueDriver({
    repository,
    syncApi,
    fileBytesReader: {
      readRegularFileBytes: async (normalizedPath) => vaultBytes.get(normalizedPath) ?? null,
    },
    refreshAccessToken: () => {
      refreshCalls.count += 1;
      return refreshImplementation();
    },
    nowEpochMs: driverNowEpochMs,
    createCorrelationId: () => `corr-${(correlationCounter += 1)}`,
    randomJitter: () => 0,
    requestTimeoutMs: options?.requestTimeoutMs,
    passDeadlineMs: options?.passDeadlineMs,
  });
  return {
    repository,
    driver,
    vaultBytes,
    refreshCalls,
    setRefreshImplementation: (implementation) => {
      refreshImplementation = implementation;
    },
    nowEpochMs: driverNowEpochMs,
    advanceClock: (milliseconds) => {
      driverClockMs += milliseconds;
    },
    installTransport: (handlers) => {
      const scripted = createScriptedHandlers(handlers);
      activeTransport = scripted.transport;
      return scripted;
    },
  };
}

/** Record one allowed capture of exactly these bytes and return the event. */
async function captureBytes(
  harness: DriverHarness,
  normalizedPath: string,
  bytes: Uint8Array,
): Promise<JournalEvent> {
  harness.vaultBytes.set(normalizedPath, bytes);
  const capture = await harness.repository.recordCapture({
    normalizedPath,
    fingerprint: await deriveFrozenFingerprint(bytes),
    policyRevisionNumber: 2,
    admission: "policy_allowed",
  });
  if (capture.outcome !== "event_recorded" && capture.outcome !== "event_coalesced") {
    throw new Error("expected a recorded capture");
  }
  return capture.event;
}

/** The events of one tracked path, oldest first. */
function eventsOfPath(harness: DriverHarness, normalizedPath: string): readonly JournalEvent[] {
  const localFile = harness.repository.readLocalFileByPath(normalizedPath);
  if (localFile === null) {
    throw new Error(`no tracked file for ${normalizedPath}`);
  }
  return harness.repository.readEventsByLocalFileId(localFile.localFileId);
}

async function runPass(driver: { runPass(): Promise<QueuePassSummary> }): Promise<QueuePassSummary> {
  return driver.runPass();
}

// --- selection, ordering and the happy path -------------------------------------------------------

describe("queue driver selection and the happy path (spec 8, 10)", () => {
  it("processes the oldest eligible event first with one active request", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/first.md", new TextEncoder().encode("first content"));
    await captureBytes(harness, "notes/second.md", new TextEncoder().encode("second content"));
    const callOrder: string[] = [];
    const scripted = harness.installTransport({
      preflight: async (body) => {
        callOrder.push(`preflight:${body["normalized_locator"]}`);
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => {
        callOrder.push("content");
        return { status: 200, bodyText: COMMITTED_RECEIPT };
      },
    });
    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("completed");
    expect(summary.processedEventCount).toBe(2);
    expect(callOrder).toEqual(["preflight:notes/first.md", "content", "preflight:notes/second.md", "content"]);
    expect(scripted.maximumInFlightRequests).toBe(1);
    for (const path of ["notes/first.md", "notes/second.md"]) {
      const localFile = harness.repository.readLocalFileByPath(path);
      expect(localFile?.sourceId).toBe(SOURCE_ID);
      expect(localFile?.baseVersionId).toBe(SOURCE_VERSION_ID);
      expect(eventsOfPath(harness, path).at(-1)?.state).toBe("committed");
    }
  });

  it("persists every state transition before the next network action", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/order.md", new TextEncoder().encode("order"));
    const observedStates: string[] = [];
    harness.installTransport({
      preflight: async () => {
        observedStates.push(harness.repository.readEvent(event.eventId)?.state ?? "missing");
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => {
        const current = harness.repository.readEvent(event.eventId);
        observedStates.push(current?.state ?? "missing");
        observedStates.push(current?.operationId ?? "missing");
        return { status: 200, bodyText: COMMITTED_RECEIPT };
      },
    });
    await runPass(harness.driver);
    expect(observedStates).toEqual(["preflight", "uploading", OPERATION_ID]);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("committed");
  });

  it("sends the frozen fingerprint and derives the wire operation from the file mapping", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/mapped.md", new TextEncoder().encode("mapped update bytes"));
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
      content: async () => ({ status: 200, bodyText: COMMITTED_RECEIPT }),
    });
    await runPass(harness.driver);
    expect(eventsOfPath(harness, "notes/mapped.md").at(-1)?.state).toBe("committed");

    // A later save of the same file becomes an update against that base.
    const second = await captureBytes(harness, "notes/mapped.md", new TextEncoder().encode("mapped update bytes v2"));
    expect(second.operation).toBe("update");
    const bodies: Record<string, unknown>[] = [];
    harness.installTransport({
      preflight: async (body) => {
        bodies.push(body);
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => ({ status: 200, bodyText: COMMITTED_RECEIPT }),
    });
    await runPass(harness.driver);
    expect(bodies[0]).toMatchObject({
      event_id: second.eventId,
      idempotency_key: second.idempotencyKey,
      operation: "update",
      source_id: SOURCE_ID,
      base_version_id: SOURCE_VERSION_ID,
      sha256: second.fingerprint.sha256,
      size_bytes: second.fingerprint.sizeBytes,
      media_type: second.fingerprint.mediaType,
      policy_revision: 2,
      normalized_locator: "notes/mapped.md",
      local_file_id: second.localFileId,
    });
  });

  it("skips events whose retry time has not passed and finishes quietly", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/waiting.md", new TextEncoder().encode("waiting"));
    const scripted = harness.installTransport({
      preflight: async () => {
        throw new Error("no preflight may run");
      },
    });
    await harness.repository.markEventPreflightStarted(event.eventId);
    await harness.repository.markEventWaitingRetry(event.eventId, "server_error", harness.nowEpochMs() + 60_000);
    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("completed");
    expect(summary.processedEventCount).toBe(0);
    expect(scripted.preflightRequests).toHaveLength(0);
  });
});

// --- terminal outcomes and receipts ----------------------------------------------------------------

describe("queue driver outcome mapping (spec 10.1 table, 12)", () => {
  it("closes an excluded preflight as excluded_policy without any upload", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/denied.md", new TextEncoder().encode("denied"));
    const scripted = harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: successBody({ outcome: "excluded" }) }),
      content: async () => {
        throw new Error("no upload may happen");
      },
    });
    await runPass(harness.driver);
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("excluded_policy");
    expect(stored?.safeError).toBe("excluded_policy");
    expect(scripted.contentRequests).toHaveLength(0);
    expect(harness.repository.readEventAttemptHistory(event.eventId).at(-1)?.outcomeLabel).toBe("excluded_policy");
  });

  it("closes a conflict preflight as blocked_conflict and retains local state", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/stale.md", new TextEncoder().encode("stale base"));
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: successBody({ outcome: "conflict" }) }),
    });
    await runPass(harness.driver);
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("blocked_conflict");
    expect(stored?.safeError).toBe("blocked_conflict");
    expect(harness.repository.readLocalFileByPath("notes/stale.md")?.sourceId).toBeNull();
  });

  it("persists the no-op receipt of a no_change preflight", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/same.md", new TextEncoder().encode("same bytes"));
    const scripted = harness.installTransport({
      preflight: async () => ({
        status: 200,
        bodyText: successBody({
          outcome: "no_change",
          result: {
            result_kind: "no_change",
            source_id: SOURCE_ID,
            source_version_id: SOURCE_VERSION_ID,
            content_version: 5,
            committed_at: "2026-08-18T00:00:00Z",
          },
        }),
      }),
      content: async () => {
        throw new Error("no upload may happen");
      },
    });
    await runPass(harness.driver);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("no_change");
    const localFile = harness.repository.readLocalFileByPath("notes/same.md");
    expect(localFile?.sourceId).toBe(SOURCE_ID);
    expect(localFile?.baseVersionId).toBe(SOURCE_VERSION_ID);
    expect(scripted.contentRequests).toHaveLength(0);
  });

  it("persists a committed_replay receipt without a second upload", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/replay.md", new TextEncoder().encode("replay bytes"));
    const scripted = harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: committedReplayBody() }),
      content: async () => {
        throw new Error("no upload may happen");
      },
    });
    await runPass(harness.driver);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("committed");
    const localFile = harness.repository.readLocalFileByPath("notes/replay.md");
    expect(localFile?.sourceId).toBe(SOURCE_ID);
    expect(localFile?.baseVersionId).toBe(SOURCE_VERSION_ID);
    expect(scripted.contentRequests).toHaveLength(0);
  });

  it("closes a server size rejection as blocked_size", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/big.md", new TextEncoder().encode("big"));
    harness.installTransport({
      preflight: async () => ({ status: 422, bodyText: errorBody("small_file_size_limit_exceeded") }),
    });
    await runPass(harness.driver);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("blocked_size");
  });

  it("closes an integrity rejection as integrity_failed", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/broken.md", new TextEncoder().encode("broken"));
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
      content: async () => ({ status: 422, bodyText: errorBody("small_file_content_integrity_failed") }),
    });
    await runPass(harness.driver);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("integrity_failed");
  });
});

// --- client re-fingerprint before send ----------------------------------------------------------------

describe("queue driver client re-fingerprint before send (spec 7.2, 10.2)", () => {
  it("sends nothing when the local bytes no longer match the frozen fingerprint", async () => {
    const harness = createHarness();
    const newerBytes = new TextEncoder().encode("newer bytes saved during preflight");
    const event = await captureBytes(harness, "notes/changed.md", new TextEncoder().encode("original bytes"));
    let successorEventId: string | null = null;
    harness.installTransport({
      preflight: async () => {
        // The user saves while the preflight is in flight: the successor is
        // recorded through the same capture path and the file bytes change.
        harness.vaultBytes.set("notes/changed.md", newerBytes);
        const successor = await harness.repository.recordCapture({
          normalizedPath: "notes/changed.md",
          fingerprint: await deriveFrozenFingerprint(newerBytes),
          policyRevisionNumber: 2,
          admission: "policy_allowed",
        });
        if (successor.outcome === "event_recorded") {
          successorEventId = successor.event.eventId;
        }
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => {
        throw new Error("stale bytes must never be streamed");
      },
    });
    await runPass(harness.driver);

    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("integrity_failed");
    expect(stored?.safeError).toBe("integrity_failed");
    const successor = successorEventId === null ? null : harness.repository.readEvent(successorEventId);
    expect(successor?.state).toBe("queued");
    expect(successor?.fingerprint.sha256).not.toBe(stored?.fingerprint.sha256);
  });

  it("defers the event when the file disappeared before the stream", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/gone.md", new TextEncoder().encode("gone"));
    const scripted = harness.installTransport({
      preflight: async () => {
        harness.vaultBytes.delete("notes/gone.md");
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => {
        throw new Error("no upload may happen");
      },
    });
    await runPass(harness.driver);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("deferred_lifecycle");
    expect(scripted.contentRequests).toHaveLength(0);
  });
});

// --- replay after a lost response ----------------------------------------------------------------------

describe("queue driver lost-response replay (spec 10.3)", () => {
  it("re-preflights with the same identity and finishes on the replay receipt", async () => {
    const harness = createHarness({ requestTimeoutMs: 60 });
    const event = await captureBytes(harness, "notes/lost.md", new TextEncoder().encode("lost response"));
    let contentCalls = 0;
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
      content: async () => {
        contentCalls += 1;
        // The server commits but the response is lost on the wire.
        return new Promise<RawResponse>(() => undefined) as Promise<RawResponse>;
      },
    });
    await runPass(harness.driver);

    const afterLoss = harness.repository.readEvent(event.eventId);
    expect(afterLoss?.state).toBe("waiting_retry");
    expect(afterLoss?.safeError).toBe("network_timeout");
    expect(afterLoss?.operationId).toBe(OPERATION_ID);
    expect(contentCalls).toBe(1);

    harness.advanceClock(RETRY_BACKOFF_INITIAL_MS + 1);
    const scripted = harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: committedReplayBody() }),
      content: async () => {
        throw new Error("the replay must not upload again");
      },
    });
    await runPass(harness.driver);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("committed");
    expect(scripted.contentRequests).toHaveLength(0);
    expect(scripted.preflightBodies[0]).toMatchObject({
      event_id: event.eventId,
      idempotency_key: event.idempotencyKey,
      sha256: event.fingerprint.sha256,
    });
  });

  it("resumes an event left in uploading state by an interrupted pass", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/interrupted.md", new TextEncoder().encode("interrupted"));
    await harness.repository.markEventPreflightStarted(event.eventId);
    await harness.repository.markEventUploading(event.eventId, OPERATION_ID);
    const scripted = harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: committedReplayBody() }),
      content: async () => {
        throw new Error("no upload may happen");
      },
    });
    await runPass(harness.driver);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("committed");
    expect(scripted.preflightBodies[0]).toMatchObject({
      event_id: event.eventId,
      idempotency_key: event.idempotencyKey,
    });
  });
});

// --- bounded jittered backoff ---------------------------------------------------------------------------

describe("queue driver retry backoff (spec 8, 12)", () => {
  it("keeps the event and schedules jittered exponential backoff on offline", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/offline.md", new TextEncoder().encode("offline"));
    harness.installTransport({
      preflight: async () => {
        throw new Error("socket gone");
      },
    });
    const before = harness.nowEpochMs();
    await runPass(harness.driver);
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("waiting_retry");
    expect(stored?.attemptCount).toBe(1);
    expect(stored?.nextEligibleRetryEpochMs).toBe(before + RETRY_BACKOFF_INITIAL_MS);
    expect(stored?.safeError).toBe("network_offline");
    const attempts = harness.repository.readEventAttemptHistory(event.eventId);
    expect(attempts).toHaveLength(1);
    expect(attempts[0]?.outcomeLabel).toBe("network_offline");
    expect(attempts[0]?.requestCorrelationId).toMatch(/^corr-\d+$/);
  });

  it.each<readonly [string, number | string, string]>([
    ["timeout", "network_timeout", "network_timeout"],
    ["429", 429, "network_rate_limited"],
    ["5xx", 500, "server_error"],
  ])("schedules bounded retry for %s", async (_name, statusOrKind, expectedLabel) => {
    const harness = createHarness({ requestTimeoutMs: 60 });
    const event = await captureBytes(harness, `notes/${expectedLabel}.md`, new TextEncoder().encode(expectedLabel));
    harness.installTransport({
      preflight: async () => {
        if (typeof statusOrKind === "number") {
          return { status: statusOrKind, bodyText: errorBody("internal_error") };
        }
        return new Promise<RawResponse>(() => undefined) as Promise<RawResponse>;
      },
    });
    await runPass(harness.driver);
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("waiting_retry");
    expect(stored?.safeError).toBe(expectedLabel);
    expect(stored?.nextEligibleRetryEpochMs).toBeGreaterThan(harness.nowEpochMs() - 1);
  });

  it("doubles the schedule per attempt and caps it at five minutes", () => {
    expect(computeRetryBackoffMs(1, () => 0)).toBe(1_000);
    expect(computeRetryBackoffMs(2, () => 0)).toBe(2_000);
    expect(computeRetryBackoffMs(3, () => 0)).toBe(4_000);
    expect(computeRetryBackoffMs(9, () => 0)).toBe(256_000);
    expect(computeRetryBackoffMs(10, () => 0)).toBe(RETRY_BACKOFF_MAXIMUM_MS);
    expect(computeRetryBackoffMs(20, () => 1)).toBe(RETRY_BACKOFF_MAXIMUM_MS);
    // Jitter is bounded: a quarter of the delay at most, still capped.
    expect(computeRetryBackoffMs(1, () => 1)).toBe(1_250);
    expect(computeRetryBackoffMs(2, () => 0.5)).toBe(2_250);
    expect(computeRetryBackoffMs(9, () => 1)).toBe(RETRY_BACKOFF_MAXIMUM_MS);
    expect(RETRY_BACKOFF_MAXIMUM_MS).toBe(300_000);
    expect(RETRY_BACKOFF_INITIAL_MS).toBe(1_000);
  });

  it("grows the schedule across consecutive failing passes", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/growing.md", new TextEncoder().encode("growing"));
    const failOffline = () =>
      harness.installTransport({
        preflight: async () => {
          throw new Error("socket gone");
        },
      });
    failOffline();
    await runPass(harness.driver);
    expect(harness.repository.readEvent(event.eventId)?.nextEligibleRetryEpochMs).toBe(
      1_784_000_000_000 + RETRY_BACKOFF_INITIAL_MS,
    );

    harness.advanceClock(RETRY_BACKOFF_INITIAL_MS);
    const secondStart = harness.nowEpochMs();
    failOffline();
    await runPass(harness.driver);
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.attemptCount).toBe(2);
    expect(stored?.nextEligibleRetryEpochMs).toBe(secondStart + 2 * RETRY_BACKOFF_INITIAL_MS);
  });

  it("enforces the request deadline because the transport is not abortable", async () => {
    const harness = createHarness({ requestTimeoutMs: 60 });
    const event = await captureBytes(harness, "notes/hanging.md", new TextEncoder().encode("hanging"));
    harness.installTransport({
      preflight: async () => new Promise<RawResponse>(() => undefined) as Promise<RawResponse>,
    });
    await runPass(harness.driver);
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("waiting_retry");
    expect(stored?.safeError).toBe("network_timeout");
  }, 10_000);

  it("keeps the pass bounded by its deadline", async () => {
    const harness = createHarness({ passDeadlineMs: 40, useWallClock: true });
    await captureBytes(harness, "notes/slow-one.md", new TextEncoder().encode("slow one"));
    await captureBytes(harness, "notes/slow-two.md", new TextEncoder().encode("slow two"));
    const delay = (milliseconds: number) =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, milliseconds);
      });
    let preflightCalls = 0;
    harness.installTransport({
      preflight: async () => {
        preflightCalls += 1;
        await delay(30);
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => {
        await delay(30);
        return { status: 200, bodyText: COMMITTED_RECEIPT };
      },
    });
    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("completed");
    expect(preflightCalls).toBe(1);
    expect(QUEUE_PASS_DEADLINE_MS).toBe(60_000);
    expect(QUEUE_REQUEST_TIMEOUT_MS).toBe(30_000);
  }, 10_000);
});

// --- one refresh maximum per pass and queue preservation --------------------------------------------------

describe("queue driver credential handling (spec 8, 12)", () => {
  it("refreshes once per pass and retries the request after a 401", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/expired.md", new TextEncoder().encode("expired access"));
    let preflightCalls = 0;
    harness.installTransport({
      preflight: async () => {
        preflightCalls += 1;
        return preflightCalls === 1
          ? { status: 401, bodyText: errorBody("device_credential_invalid") }
          : { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => ({ status: 200, bodyText: COMMITTED_RECEIPT }),
    });
    await runPass(harness.driver);
    expect(harness.refreshCalls.count).toBe(1);
    expect(preflightCalls).toBe(2);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("committed");
  });

  it("ends the pass after a second 401 without another refresh", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/twice.md", new TextEncoder().encode("second 401"));
    let preflightCalls = 0;
    harness.installTransport({
      preflight: async () => {
        preflightCalls += 1;
        return { status: 401, bodyText: errorBody("device_credential_invalid") };
      },
    });
    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("login_required");
    expect(preflightCalls).toBe(2);
    expect(harness.refreshCalls.count).toBe(1);
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("waiting_retry");
    expect(stored?.safeError).toBe("login_required");
    expect(harness.repository.countPendingEvents()).toBe(1);
  });

  it("preserves the whole queue when the refresh itself fails terminally", async () => {
    const harness = createHarness();
    harness.setRefreshImplementation(() => Promise.reject(new Error("device_token_reuse_detected")));
    const first = await captureBytes(harness, "notes/revoked-a.md", new TextEncoder().encode("revoked a"));
    const second = await captureBytes(harness, "notes/revoked-b.md", new TextEncoder().encode("revoked b"));
    let preflightCalls = 0;
    harness.installTransport({
      preflight: async () => {
        preflightCalls += 1;
        return { status: 401, bodyText: errorBody("device_credential_invalid") };
      },
    });
    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("login_required");
    expect(preflightCalls).toBe(1);
    expect(harness.refreshCalls.count).toBe(1);
    // The queue survives intact: every event stays in a safe retry state.
    const firstStored = harness.repository.readEvent(first.eventId);
    expect(firstStored?.state).toBe("waiting_retry");
    expect(firstStored?.safeError).toBe("login_required");
    expect(harness.repository.readEvent(second.eventId)?.state).toBe("queued");
    expect(harness.repository.countPendingEvents()).toBe(2);
  });
});

// --- no run after unload/suspend --------------------------------------------------------------------------

describe("queue driver suspension and unload (spec 8)", () => {
  it("runs nothing once stopped", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/stopped.md", new TextEncoder().encode("stopped"));
    const scripted = harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
      content: async () => ({ status: 200, bodyText: COMMITTED_RECEIPT }),
    });
    const driver = harness.driver;
    driver.stop();
    expect(driver.isStopped).toBe(true);
    const summary = await runPass(driver);
    expect(summary.outcome).toBe("stopped");
    await driver.requestPass();
    expect(scripted.preflightRequests).toHaveLength(0);
    expect(scripted.contentRequests).toHaveLength(0);
  });

  it("discards a late in-flight result after stop and leaves the event resumable", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/late.md", new TextEncoder().encode("late result"));
    const gate: { resolve: ((response: RawResponse) => void) | null } = { resolve: null };
    harness.installTransport({
      preflight: () =>
        new Promise<RawResponse>((resolve) => {
          gate.resolve = resolve;
        }),
      content: async () => {
        throw new Error("a discarded preflight must never upload");
      },
    });
    const driver = harness.driver;
    const pass = runPass(driver);
    await new Promise<void>((resolve) => {
      const poll = setInterval(() => {
        if (gate.resolve !== null) {
          clearInterval(poll);
          resolve();
        }
      }, 5);
    });
    driver.stop();
    gate.resolve?.({ status: 200, bodyText: SINGLE_PART_BODY });
    const summary = await pass;
    expect(summary.outcome).toBe("stopped");
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("preflight");
    expect(stored?.operationId).toBeNull();
  });

  it("ignores a new trigger while a pass is already running", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/busy.md", new TextEncoder().encode("busy"));
    const gate: { release: (() => void) | null } = { release: null };
    harness.installTransport({
      preflight: () =>
        new Promise<RawResponse>((resolve) => {
          gate.release = () => resolve({ status: 200, bodyText: successBody({ outcome: "excluded" }) });
        }),
    });
    const driver = harness.driver;
    const first = runPass(driver);
    await new Promise<void>((resolve) => {
      const poll = setInterval(() => {
        if (gate.release !== null) {
          clearInterval(poll);
          resolve();
        }
      }, 5);
    });
    const second = await driver.requestPass();
    expect(second.outcome).toBe("pass_already_running");
    gate.release?.();
    expect((await first).outcome).toBe("completed");
  });
});
