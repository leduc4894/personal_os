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

import { CoalescingQueuePassDispatcher } from "./automatic-snapshot";
import type { JournalEvent, JournalMeta, JournalSafeErrorLabel } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import {
  JournalQueueDriver,
  QUEUE_PASS_DEADLINE_MS,
  QUEUE_REQUEST_TIMEOUT_MS,
  RETRY_BACKOFF_INITIAL_MS,
  RETRY_BACKOFF_MAXIMUM_MS,
  computeRetryBackoffMs,
  parkFailureSiteToken,
} from "./queue-driver";
import type { QueuePassSummary } from "./queue-driver";
import { JournalRepository } from "./repository";
import { JOURNAL_SCHEMA_VERSION, journalStoreError, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";
import { createJournalSyncApi } from "./sync-api";
import { LifecycleDriverImpl, type LifecycleApi } from "./lifecycle-driver";
import { LifecycleApiError, type LifecycleResult } from "./lifecycle-api";
import { LifecycleRepository } from "./lifecycle-repository";
import {
  createLifecycleEventOperands,
  type LifecycleEventOperands,
} from "./lifecycle-contracts";
import type { SyncHttpRequest } from "./sync-api";
import type {
  SyncDiagnosticsTrail,
  SyncDiagnosticsTrailAppendInput,
  SyncDiagnosticTrailEntry,
} from "./sync-diagnostics-trail";

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
const OTHER_OPERATION_ID = "ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210_-Aa";
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

/**
 * The in-memory diagnostics trail recorder of the seam-wiring tests: every
 * append lands synchronously so the tests can pin the closed tokens of each
 * recorded entry without a file store.
 */
class RecordingDiagnosticsTrail implements SyncDiagnosticsTrail {
  readonly entries: SyncDiagnosticTrailEntry[] = [];
  readonly #appendBehavior: () => Promise<void>;

  constructor(appendBehavior: () => Promise<void> = () => Promise.resolve()) {
    this.#appendBehavior = appendBehavior;
  }

  async load(): Promise<void> {
    // The driver never loads the trail; the composition root does.
  }

  append(input: SyncDiagnosticsTrailAppendInput): Promise<void> {
    this.entries.push({ kind: input.kind, atEpochMs: 0, tokens: [...input.tokens] });
    return this.#appendBehavior();
  }

  readEntries(): readonly SyncDiagnosticTrailEntry[] {
    return [...this.entries];
  }

  readAppendFailureCount(): number {
    return 0;
  }
}

interface DriverHarness {
  readonly repository: JournalRepository;
  readonly driver: JournalQueueDriver;
  readonly diagnosticTrail: RecordingDiagnosticsTrail;
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
  readonly diagnosticTrailAppendBehavior?: () => Promise<void>;
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
  const diagnosticTrail = new RecordingDiagnosticsTrail(options?.diagnosticTrailAppendBehavior);
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
    diagnosticTrail,
  });
  return {
    repository,
    driver,
    diagnosticTrail,
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

  it("does not resume a stale token when the persisted operation changed during preflight", async () => {
    const harness = createHarness();
    const event = await captureBytes(
      harness,
      "notes/token-drift.md",
      new TextEncoder().encode("token drift"),
    );
    await harness.repository.markEventPreflightStarted(event.eventId);
    await harness.repository.markEventUploading(event.eventId, OPERATION_ID);
    const scripted = harness.installTransport({
      preflight: async () => {
        await harness.repository.markEventUploading(event.eventId, OTHER_OPERATION_ID);
        return {
          status: 409,
          bodyText: errorBody("small_file_upload_state_invalid"),
        };
      },
      content: async () => {
        throw new Error("a drifted operation token must not be resumed");
      },
    });

    await runPass(harness.driver);

    expect(harness.repository.readEvent(event.eventId)).toMatchObject({
      state: "waiting_retry",
      operationId: OTHER_OPERATION_ID,
    });
    expect(scripted.contentRequests).toHaveLength(0);
  });

  it("does not resume an exact token when the server says the operation is unknown", async () => {
    const harness = createHarness();
    const event = await captureBytes(
      harness,
      "notes/unknown-operation.md",
      new TextEncoder().encode("unknown operation"),
    );
    await harness.repository.markEventPreflightStarted(event.eventId);
    await harness.repository.markEventUploading(event.eventId, OPERATION_ID);
    const scripted = harness.installTransport({
      preflight: async () => ({
        status: 404,
        bodyText: errorBody("small_file_operation_not_found"),
      }),
      content: async () => {
        throw new Error("an unknown operation token must not be resumed");
      },
    });

    await runPass(harness.driver);

    expect(harness.repository.readEvent(event.eventId)).toMatchObject({
      state: "waiting_retry",
      operationId: OPERATION_ID,
    });
    expect(scripted.contentRequests).toHaveLength(0);
  });

  it("keeps policy-change preflight recovery ahead of a persisted upload token", async () => {
    const harness = createHarness();
    const event = await captureBytes(
      harness,
      "notes/policy-recovery.md",
      new TextEncoder().encode("policy recovery"),
    );
    await harness.repository.markEventPreflightStarted(event.eventId);
    await harness.repository.markEventUploading(event.eventId, OPERATION_ID);
    const scripted = harness.installTransport({
      preflight: async () => ({
        status: 200,
        bodyText: successBody({ outcome: "excluded" }),
      }),
      content: async () => {
        throw new Error("an excluded preflight must not resume content");
      },
    });

    await runPass(harness.driver);

    expect(harness.repository.readEvent(event.eventId)?.state).toBe("excluded_policy");
    expect(scripted.contentRequests).toHaveLength(0);
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
    const harness = createHarness({ passDeadlineMs: 10 });
    await captureBytes(harness, "notes/slow-one.md", new TextEncoder().encode("slow one"));
    await captureBytes(harness, "notes/slow-two.md", new TextEncoder().encode("slow two"));
    let preflightCalls = 0;
    let contentCalls = 0;
    harness.installTransport({
      preflight: async () => {
        preflightCalls += 1;
        // The clock crosses the deadline while the preflight is in flight,
        // so the pass must refuse the content request that follows.
        harness.advanceClock(11);
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => {
        contentCalls += 1;
        return { status: 200, bodyText: COMMITTED_RECEIPT };
      },
    });
    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("deadline_reached");
    expect(preflightCalls).toBe(1);
    expect(contentCalls).toBe(0);
    expect(QUEUE_PASS_DEADLINE_MS).toBe(60_000);
    expect(QUEUE_REQUEST_TIMEOUT_MS).toBe(30_000);
  });

  it("automatically continues eligible queued work after a pass deadline", async () => {
    const harness = createHarness({ passDeadlineMs: 10 });
    await captureBytes(harness, "notes/deadline-one.md", new TextEncoder().encode("one"));
    await captureBytes(harness, "notes/deadline-two.md", new TextEncoder().encode("two"));
    await captureBytes(harness, "notes/deadline-three.md", new TextEncoder().encode("three"));
    let preflightCalls = 0;
    harness.installTransport({
      preflight: async () => {
        preflightCalls += 1;
        harness.advanceClock(11);
        return { status: 200, bodyText: successBody({ outcome: "excluded" }) };
      },
    });
    const dispatcher = new CoalescingQueuePassDispatcher({
      runPass: () => harness.driver.runPass(),
    });

    await dispatcher.request();

    expect(preflightCalls).toBe(3);
    expect(harness.repository.countPendingEvents()).toBe(0);
  });

  it("does not start a follow-up pass after a retryable failure at the deadline", async () => {
    const harness = createHarness({ passDeadlineMs: 10 });
    const failedEvent = await captureBytes(
      harness,
      "notes/deadline-fail-one.md",
      new TextEncoder().encode("one"),
    );
    await captureBytes(harness, "notes/deadline-fail-two.md", new TextEncoder().encode("two"));
    let preflightCalls = 0;
    const scripted = harness.installTransport({
      preflight: async (body) => {
        preflightCalls += 1;
        if (body["normalized_locator"] === "notes/deadline-fail-one.md") {
          harness.advanceClock(11);
          throw new Error("socket gone");
        }
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
    });
    const dispatcher = new CoalescingQueuePassDispatcher({
      runPass: () => harness.driver.runPass(),
    });

    await dispatcher.request();

    expect(preflightCalls).toBe(1);
    expect(scripted.contentRequests).toHaveLength(0);
    const retried = harness.repository.readEvent(failedEvent.eventId);
    expect(retried?.state).toBe("waiting_retry");
    expect(retried?.nextEligibleRetryEpochMs).toBeGreaterThan(harness.nowEpochMs());
    const laterEvent = eventsOfPath(harness, "notes/deadline-fail-two.md")[0];
    expect(laterEvent?.state).toBe("queued");
  });

  it("ends the pass with retry_scheduled when a retryable failure stays inside the deadline", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/retry-inside.md", new TextEncoder().encode("retry inside"));
    harness.installTransport({
      preflight: async () => {
        throw new Error("socket gone");
      },
    });

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("retry_scheduled");
    expect(summary.processedEventCount).toBe(1);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("waiting_retry");
  });

  it("reports completed, not deadline_reached, when the deadline passes without remaining eligible work", async () => {
    const harness = createHarness({ passDeadlineMs: 10 });
    await captureBytes(harness, "notes/deadline-terminal.md", new TextEncoder().encode("terminal"));
    harness.installTransport({
      preflight: async () => {
        harness.advanceClock(11);
        return { status: 200, bodyText: successBody({ outcome: "excluded" }) };
      },
    });

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("completed");
    expect(harness.repository.countPendingEvents()).toBe(0);
  });
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

  it("parks an edge 403 with a non-API HTML body under server_error and ends the pass retry_scheduled (fix round 5)", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/edge-403.md", new TextEncoder().encode("edge blocked"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: "<!DOCTYPE html><html><body>Blocked by the edge firewall.</body></html>",
      }),
    });

    const summary = await runPass(harness.driver);
    // A wire failure, not a login verdict: the pass ends retry_scheduled
    // and the event sits in bounded network backoff — the queue survives
    // instead of starving behind a false login_required park.
    expect(summary.outcome).toBe("retry_scheduled");
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("waiting_retry");
    expect(stored?.safeError).toBe("server_error");
    expect(stored?.nextEligibleRetryEpochMs).not.toBeNull();
    expect(harness.refreshCalls.count).toBe(0);
    expect(harness.repository.countPendingEvents()).toBe(1);
  });

  it("keeps a genuine API 403 envelope as a login_required park with no refresh", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/api-403.md", new TextEncoder().encode("api denied"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: errorBody("authorization_scope_denied"),
      }),
    });

    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("login_required");
    const stored = harness.repository.readEvent(event.eventId);
    expect(stored?.state).toBe("waiting_retry");
    expect(stored?.safeError).toBe("login_required");
    // The content lane refreshes only access_expired (401): a genuine API
    // 403 login verdict never attempts a refresh — the 401 discipline of
    // the neighboring tests is untouched.
    expect(harness.refreshCalls.count).toBe(0);
  });
});

// --- journal-failure diagnostics (fix round 5) -----------------------------------------------------------

describe("queue driver journal-failure diagnostics (fix round 5)", () => {
  it("records the closed store-error reason of a mid-pass journal failure", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/journal-failure.md", new TextEncoder().encode("failure bytes"));
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
    });
    expect(harness.driver.readJournalFailureReasons()).toEqual([]);
    // The pass loop's bare catch (end_journal_failure) used to swallow the
    // closed reason entirely — the live park mystery was undiagnosable by
    // design.
    harness.repository.readOldestEligibleEvent = () => {
      throw journalStoreError("journal_query_failed");
    };
    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("completed");
    expect(harness.driver.readJournalFailureReasons()).toEqual(["journal_query_failed"]);
  });

  it("keeps only the last five reasons and records nothing for non-store errors", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/journal-failure-bound.md", new TextEncoder().encode("bound bytes"));
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
    });
    harness.repository.readOldestEligibleEvent = () => {
      throw journalStoreError("journal_store_unavailable");
    };
    for (let passIndex = 0; passIndex < 7; passIndex += 1) {
      await runPass(harness.driver);
    }
    harness.repository.readOldestEligibleEvent = () => {
      throw new Error("not a journal store error");
    };
    await runPass(harness.driver);
    // Bounded at five, closed tokens only: a non-store error adds nothing.
    expect(harness.driver.readJournalFailureReasons()).toEqual([
      "journal_store_unavailable",
      "journal_store_unavailable",
      "journal_store_unavailable",
      "journal_store_unavailable",
      "journal_store_unavailable",
    ]);
  });
});

// --- diagnostics trail wiring (sync error tracing task 1) ---------------------------------------------------

describe("queue driver diagnostics trail wiring (sync error tracing task 1)", () => {
  it("records wire_failure with the failure kind and pass_outcome when an edge 403 HTML page fails the pass", async () => {
    const harness = createHarness();
    const event = await captureBytes(harness, "notes/edge-403.md", new TextEncoder().encode("edge"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: "<!DOCTYPE html><html><body>blocked by the edge</body></html>",
      }),
    });

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("retry_scheduled");
    expect(harness.repository.readEvent(event.eventId)?.safeError).toBe("server_error");
    // The trail mirrors the pass exactly: one wire failure entry (the closed
    // failure kind; the HTML body carries no envelope request id) followed
    // by the pass outcome entry.
    expect(harness.diagnosticTrail.entries.map((entry) => entry.kind)).toEqual([
      "wire_failure",
      "pass_outcome",
    ]);
    expect(harness.diagnosticTrail.entries[0]?.tokens).toEqual(["server_error"]);
    expect(harness.diagnosticTrail.entries[1]?.tokens).toEqual(["retry_scheduled"]);
  });

  it("records journal_failure with the closed reason of a mid-pass store error", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/trail-store-error.md", new TextEncoder().encode("store"));
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
    });
    harness.repository.readOldestEligibleEvent = () => {
      throw journalStoreError("journal_query_failed");
    };

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("completed");
    expect(harness.diagnosticTrail.entries.map((entry) => entry.kind)).toEqual([
      "journal_failure",
      "pass_outcome",
    ]);
    expect(harness.diagnosticTrail.entries[0]?.tokens).toEqual(["journal_query_failed"]);
    expect(harness.diagnosticTrail.entries[1]?.tokens).toEqual(["completed"]);
  });

  it("carries the envelope request id on a wire failure the server envelope answered", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/api-403-trail.md", new TextEncoder().encode("denied"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: errorBody("authorization_scope_denied"),
      }),
    });

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("login_required");
    const wireFailure = harness.diagnosticTrail.entries[0];
    expect(wireFailure?.kind).toBe("wire_failure");
    // The failing envelope's opaque request id joins the client trail to the
    // server-side log of the same rejected request — and, since diagnostic
    // round U1, the parsed closed server error code rides between them.
    expect(wireFailure?.tokens).toEqual([
      "login_required",
      "authorization_scope_denied",
      { requestId: "66666666-6666-4666-8666-666666666666" },
    ]);
  });

  it("carries the closed server error code token on a login_required wire failure (diagnostic round U1)", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/api-403-code.md", new TextEncoder().encode("denied code"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: errorBody("exclusion_policy_denied"),
      }),
    });

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("login_required");
    const wireFailure = harness.diagnosticTrail.entries[0];
    expect(wireFailure?.kind).toBe("wire_failure");
    // The parsed server envelope code lands as one additional closed token
    // between the kind and the request id: the registry code that rejected
    // the request, whitelisted at the trail boundary by shape only.
    expect(wireFailure?.tokens).toEqual([
      "login_required",
      "exclusion_policy_denied",
      { requestId: "66666666-6666-4666-8666-666666666666" },
    ]);
  });

  it("records no code token on an edge HTML wire failure", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/edge-403-code.md", new TextEncoder().encode("edge"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: "<html>edge block</html>",
      }),
    });

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("retry_scheduled");
    const wireFailure = harness.diagnosticTrail.entries[0];
    expect(wireFailure?.kind).toBe("wire_failure");
    // No envelope, no closed code: the kind token already says server_error
    // and nothing extra is recorded.
    expect(wireFailure?.tokens).toEqual(["server_error"]);
  });

  it("samples the envelope request id into the pass outcome entry on the success path", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/trail-success.md", new TextEncoder().encode("success"));
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
      content: async () => ({ status: 200, bodyText: COMMITTED_RECEIPT }),
    });

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("completed");
    // No wire failure happened; the single pass outcome entry carries the
    // sampled request id of the pass's last successful request outcome.
    expect(harness.diagnosticTrail.entries).toHaveLength(1);
    expect(harness.diagnosticTrail.entries[0]?.kind).toBe("pass_outcome");
    expect(harness.diagnosticTrail.entries[0]?.tokens).toEqual([
      "completed",
      { requestId: "66666666-6666-4666-8666-666666666666" },
    ]);
  });

  it("never blocks the pass on a stalled trail append", async () => {
    const harness = createHarness({
      diagnosticTrailAppendBehavior: () => new Promise<void>(() => undefined),
    });
    await captureBytes(harness, "notes/stalled-trail.md", new TextEncoder().encode("stalled"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: "<html>edge block</html>",
      }),
    });

    // The stalled append must not block or break the pass: the driver calls
    // the trail fire-and-forget and the pass still ends retry_scheduled.
    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("retry_scheduled");
    expect(harness.diagnosticTrail.entries.map((entry) => entry.kind)).toEqual([
      "wire_failure",
      "pass_outcome",
    ]);
  });
});

// --- retry-park failure state capture (sync error tracing park diagnosis round) ------------------------------

describe("queue driver retry-park failure state capture (sync error tracing park diagnosis round)", () => {
  it("captures the closed reason and the terminal row state when a retry park fails", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/park-terminal.md", new TextEncoder().encode("park"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: errorBody("authorization_scope_denied"),
      }),
    });
    // The live-mystery shape: the row is flipped into a terminal state while
    // the park runs, so the REAL mutation's non-terminal guard throws the
    // closed `journal_mutation_failed` reason from pure in-memory data —
    // exactly what the live machine parks-fail with while the offline
    // reproduction of the same journal bytes parks cleanly.
    const originalPark = harness.repository.markEventWaitingRetry.bind(harness.repository);
    harness.repository.markEventWaitingRetry = async (
      eventId: string,
      safeError: JournalSafeErrorLabel,
      nextEligibleRetryEpochMs: number,
    ) => {
      await harness.repository.markEventTerminal(eventId, "blocked_conflict", "blocked_conflict");
      return originalPark(eventId, safeError, nextEligibleRetryEpochMs);
    };

    const summary = await runPass(harness.driver);

    // Pass semantics are unchanged: the rethrown park error still ends the
    // pass fail-closed (login_required because the wire verdict set it).
    expect(summary.outcome).toBe("login_required");
    expect(harness.driver.readJournalFailureReasons()).toEqual(["journal_mutation_failed"]);
    // The trail mirrors the pass: the wire failure, then the NEW park-site
    // entry carrying the closed reason AND the row state read back at the
    // throw moment, then the pre-existing pass-loop journal_failure tap.
    expect(harness.diagnosticTrail.entries.map((entry) => entry.kind)).toEqual([
      "wire_failure",
      "journal_failure",
      "journal_failure",
      "pass_outcome",
    ]);
    expect(harness.diagnosticTrail.entries[1]?.tokens).toEqual([
      "journal_mutation_failed",
      "state_blocked_conflict",
      "site_mutation_internal",
    ]);
    expect(harness.diagnosticTrail.entries[2]?.tokens).toEqual(["journal_mutation_failed"]);
  });

  it("appends site_mutation_internal when the park throws with valid arguments over a present pending row (diagnostic round U2)", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/park-valid-args.md", new TextEncoder().encode("valid"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: errorBody("authorization_scope_denied"),
      }),
    });
    // The live-mystery shape with VALID arguments: an injected repository
    // whose park simply throws the closed reason while the row sits in its
    // PENDING preflight state — every precondition the repository's own
    // argument validation checks holds (uuid id, closed safe label,
    // non-negative integer epoch), so the throw is consistent only with a
    // site INSIDE the serialized mutation.
    harness.repository.markEventWaitingRetry = () => {
      throw journalStoreError("journal_mutation_failed");
    };

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("login_required");
    expect(harness.driver.readJournalFailureReasons()).toEqual(["journal_mutation_failed"]);
    const parkEntry = harness.diagnosticTrail.entries.find(
      (entry) => entry.kind === "journal_failure" && entry.tokens.includes("state_preflight"),
    );
    expect(parkEntry?.tokens).toEqual([
      "journal_mutation_failed",
      "state_preflight",
      "site_mutation_internal",
    ]);
  });

  it("captures row_absent when the parked row is gone at the failure moment", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/park-absent.md", new TextEncoder().encode("absent"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: errorBody("authorization_scope_denied"),
      }),
    });
    harness.repository.markEventWaitingRetry = () => {
      // The row exists for the park's initial read but answers null when the
      // failure capture reads it back.
      harness.repository.readEvent = () => null;
      throw journalStoreError("journal_mutation_failed");
    };

    const summary = await runPass(harness.driver);

    expect(summary.outcome).toBe("login_required");
    const parkEntry = harness.diagnosticTrail.entries.find(
      (entry) => entry.tokens.includes("row_absent"),
    );
    expect(parkEntry?.kind).toBe("journal_failure");
    expect(parkEntry?.tokens).toEqual([
      "journal_mutation_failed",
      "row_absent",
      "site_mutation_internal",
    ]);
  });

  it("records no journal_failure entry when the retry park succeeds", async () => {
    const harness = createHarness();
    await captureBytes(harness, "notes/park-healthy.md", new TextEncoder().encode("healthy"));
    harness.installTransport({
      preflight: async () => ({
        status: 403,
        bodyText: errorBody("authorization_scope_denied"),
      }),
    });

    const summary = await runPass(harness.driver);

    // Control: a healthy park adds no journal_failure entry — the pass keeps
    // the plain login_required wire failure and pass outcome.
    expect(summary.outcome).toBe("login_required");
    expect(harness.driver.readJournalFailureReasons()).toEqual([]);
    expect(harness.diagnosticTrail.entries.map((entry) => entry.kind)).toEqual([
      "wire_failure",
      "pass_outcome",
    ]);
  });

  it("derives site_argument_validation for every argument shape the park's own validation would reject (diagnostic round U2)", () => {
    const validEventId = "00000000-0000-4000-8000-000000000001";
    const validEpochMs = 1_784_000_001_000;
    // Valid arguments (the repository's isUuid, closed-label membership and
    // non-negative-integer checks all hold): the throw site is inside the
    // serialized mutation.
    expect(parkFailureSiteToken(validEventId, "login_required", validEpochMs)).toBe(
      "site_mutation_internal",
    );
    // Each observable argument precondition the repository's own validation
    // would reject flips the discriminator to the argument-validation site.
    expect(parkFailureSiteToken("not-a-uuid", "login_required", validEpochMs)).toBe(
      "site_argument_validation",
    );
    expect(parkFailureSiteToken(validEventId, "edge block page", validEpochMs)).toBe(
      "site_argument_validation",
    );
    expect(parkFailureSiteToken(validEventId, "login_required", -1)).toBe(
      "site_argument_validation",
    );
    expect(parkFailureSiteToken(validEventId, "login_required", 1_784_000_000.5)).toBe(
      "site_argument_validation",
    );
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

// --- queue driver + lifecycle lane separation (task 9 fix round 1 I3) --------------------
//
// The queue driver interleaves the lifecycle lane with the content
// lane: each pass iteration drains the lifecycle lane to idle BEFORE
// the content lane sees its next event. The fix is required so a
// content event selection never falls through to a lifecycle event
// (the content lane does not know how to dispatch a lifecycle event
// through the lifecycle API).

interface LifecycleHandler {
  (event: FrozenLifecycleEventForTest): Promise<LifecycleResult>;
}

interface LifecycleDriverHarness {
  readonly repository: JournalRepository;
  readonly lifecycle: LifecycleRepository;
  readonly driver: JournalQueueDriver;
  readonly vaultBytes: Map<string, Uint8Array>;
  readonly lifecycleCommits: FrozenLifecycleEventForTest[];
  readonly preflightBodies: Record<string, unknown>[];
  /** The refresh-seam call counter (the shared per-pass budget). */
  readonly refreshCalls: { count: number };
  installLifecycleHandler: (handler: LifecycleHandler) => void;
  installTransport: (
    handlers: { preflight: PreflightHandler; content?: ContentHandler },
  ) => ScriptedTransport;
  /** Seed a tracked file with a committed create so the file has a known source id. */
  seedTrackedFile: (path: string, bytes: Uint8Array) => Promise<JournalEvent>;
  recordLifecycleEvent: (
    operands: LifecycleEventOperands,
    options?: { tombstoneId?: string | null; newPath?: string },
  ) => Promise<JournalEvent>;
  /** Record one content-event capture (create or update). */
  recordContent: (path: string, bytes: Uint8Array) => Promise<JournalEvent>;
}

type FrozenLifecycleEventForTest = {
  readonly event: { readonly eventId: string };
  readonly operands: { readonly operation: string };
};

function createHarnessWithLifecycle(): LifecycleDriverHarness {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  } satisfies JournalMeta);
  const epochBase = 1_784_000_000_000;
  const driverClockMs = epochBase;
  let idCounter = 0;
  const createId = () => {
    idCounter += 1;
    const suffix = String(idCounter).padStart(12, "0");
    return `00000000-0000-4000-8000-${suffix}`;
  };
  const repository = new JournalRepository({
    database,
    nowEpochMs: () => driverClockMs,
    createId,
  });
  const lifecycle = new LifecycleRepository({
    database,
    nowEpochMs: () => driverClockMs,
    createId,
  });
  const vaultBytes = new Map<string, Uint8Array>();
  const preflightBodies: Record<string, unknown>[] = [];
  const lifecycleCommits: FrozenLifecycleEventForTest[] = [];
  const refreshCalls = { count: 0 };
  let lifecycleHandler: LifecycleHandler | null = null;
  let activeTransport: ((request: SyncHttpRequest) => Promise<RawResponse>) | null = null;
  const syncApi = createJournalSyncApi({
    transport: (request) => {
      const transport = activeTransport;
      if (transport === null) {
        throw new Error("no transport installed");
      }
      return transport(request);
    },
    resolveOrigin: () => ORIGIN,
    getAccessToken: () => ACCESS_TOKEN,
  });
  const lifecycleApi: LifecycleApi = {
    async commit(event) {
      const frozen: FrozenLifecycleEventForTest = {
        event: { eventId: event.event.eventId },
        operands: { operation: event.operands.operation },
      };
      lifecycleCommits.push(frozen);
      if (lifecycleHandler === null) {
        throw new LifecycleApiError("server_error");
      }
      return lifecycleHandler(frozen);
    },
  };
  const lifecycleDriver = new LifecycleDriverImpl({
    repository,
    lifecycle,
    api: lifecycleApi,
    createCorrelationId: () => `corr-${idCounter}`,
    randomJitter: () => 0,
    nowEpochMs: () => driverClockMs,
  });
  const driver = new JournalQueueDriver({
    repository,
    syncApi,
    fileBytesReader: {
      readRegularFileBytes: async (normalizedPath) => vaultBytes.get(normalizedPath) ?? null,
    },
    lifecycleDriver,
    refreshAccessToken: () => {
      refreshCalls.count += 1;
      return Promise.resolve();
    },
    nowEpochMs: () => driverClockMs,
    createCorrelationId: () => `corr-${idCounter}`,
    randomJitter: () => 0,
  });
  return {
    repository,
    lifecycle,
    driver,
    vaultBytes,
    lifecycleCommits,
    preflightBodies,
    refreshCalls,
    installLifecycleHandler: (handler) => {
      lifecycleHandler = handler;
    },
    installTransport: (handlers) => {
      const scripted = createScriptedHandlers({
        ...handlers,
        preflight: async (body) => {
          preflightBodies.push(body);
          return await handlers.preflight(body);
        },
      });
      activeTransport = scripted.transport;
      return scripted;
    },
    seedTrackedFile: async (path, bytes) => {
      const capture = await repository.recordCapture({
        normalizedPath: path,
        fingerprint: await deriveFrozenFingerprint(bytes),
        policyRevisionNumber: 2,
        admission: "policy_allowed",
      });
      if (capture.outcome !== "event_recorded" && capture.outcome !== "event_coalesced") {
        throw new Error("expected a recorded capture");
      }
      await repository.recordCommittedReceipt({
        eventId: capture.event.eventId,
        sourceId: SOURCE_ID,
        baseVersionId: SOURCE_VERSION_ID,
      });
      return capture.event;
    },
    recordLifecycleEvent: async (operands, options) => {
      const lookupPath =
        operands.expectedLocator ??
        (operands.operation === "restore" ? operands.targetLocator : null) ??
        options?.newPath ??
        null;
      if (lookupPath === null) {
        throw new Error("recordLifecycleEvent: missing lookup path");
      }
      const file = repository.readLocalFileByPath(lookupPath);
      if (file === null) {
        throw new Error(`recordLifecycleEvent: file not found at ${lookupPath}`);
      }
      const baseOptions = {
        localFile: file,
        tombstoneId: options?.tombstoneId ?? operands.tombstoneId ?? null,
      };
      const result =
        options?.newPath !== undefined
          ? await lifecycle.recordLifecycleEvent(operands, { ...baseOptions, newPath: options.newPath })
          : await lifecycle.recordLifecycleEvent(operands, baseOptions);
      return result.event;
    },
    recordContent: async (path, bytes) => {
      vaultBytes.set(path, bytes);
      const capture = await repository.recordCapture({
        normalizedPath: path,
        fingerprint: await deriveFrozenFingerprint(bytes),
        policyRevisionNumber: 2,
        admission: "policy_allowed",
      });
      if (capture.outcome !== "event_recorded" && capture.outcome !== "event_coalesced") {
        throw new Error("expected a recorded capture");
      }
      return capture.event;
    },
  };
}

const LIFECYCLE_VERSION_ID = "77777777-7777-4777-8777-777777777777";

describe("queue driver + lifecycle lane separation (task 9 fix round 1 I3, M3)", () => {
  it("drains the lifecycle lane to idle before the content lane sees its next event", async () => {
    const harness = createHarnessWithLifecycle();
    // Seed the file with a committed create so the lifecycle events
    // can reference its source id and version.
    const seeded = await harness.seedTrackedFile(
      "notes/lifecycle-drain.md",
      new TextEncoder().encode("seed bytes"),
    );
    void seeded;
    // Queue two lifecycle events for the same file — both renames so
    // no predecessor rule blocks them. The content lane MUST not see
    // them.
    const rename1 = await harness.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: SOURCE_VERSION_ID,
        expectedLocator: "notes/lifecycle-drain.md",
        targetLocator: "notes/lifecycle-drain-renamed.md",
        policyRevision: 2,
        predecessorEventId: null,
      }),
      { newPath: "notes/lifecycle-drain-renamed.md" },
    );
    const rename2 = await harness.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: SOURCE_VERSION_ID,
        expectedLocator: "notes/lifecycle-drain-renamed.md",
        targetLocator: "notes/lifecycle-drain-renamed-twice.md",
        policyRevision: 2,
        predecessorEventId: null,
      }),
      { newPath: "notes/lifecycle-drain-renamed-twice.md" },
    );
    // Track the lifecycle calls so the test can pin the order.
    const dispatchedLifecycleIds: string[] = [];
    harness.installLifecycleHandler(async (event) => {
      dispatchedLifecycleIds.push(event.event.eventId);
      return {
        committedAt: "2026-08-20T00:00:00Z",
        eventId: event.event.eventId,
        eventSequence: 1,
        resultingLocator: null,
        sourceId: SOURCE_ID,
        sourceVersionId: LIFECYCLE_VERSION_ID,
        state: "active",
        tombstoneId: null,
      };
    });
    // Seed a content event for a separate file so the content lane
    // has work to do AFTER the lifecycle lane drains.
    const contentEvent = await harness.recordContent(
      "notes/content.md",
      new TextEncoder().encode("content bytes"),
    );
    harness.installTransport({
      preflight: async (body) => {
        expect(typeof body["event_id"]).toBe("string");
        return { status: 200, bodyText: SINGLE_PART_BODY };
      },
      content: async () => ({ status: 200, bodyText: COMMITTED_RECEIPT }),
    });

    const summary = await runPass(harness.driver);
    // The pass completed (the content event committed).
    expect(summary.outcome).toBe("completed");
    expect(summary.processedEventCount).toBe(1);
    // Both lifecycle events were dispatched, in oldest-first order.
    expect(dispatchedLifecycleIds).toEqual([rename1.eventId, rename2.eventId]);
    // The lifecycle events are terminal-success.
    expect(harness.repository.readEvent(rename1.eventId)?.state).toBe("committed");
    expect(harness.repository.readEvent(rename2.eventId)?.state).toBe("committed");
    // The content event is the next selection (terminal-success too).
    expect(harness.repository.readEvent(contentEvent.eventId)?.state).toBe("committed");
    // The content lane was reached ONCE — it never saw the lifecycle
    // events (I3 finding: the content lane cannot dispatch a lifecycle
    // event through the lifecycle API).
    expect(harness.preflightBodies).toHaveLength(1);
    expect(harness.preflightBodies[0]?.["event_id"]).toBe(contentEvent.eventId);
  });

  it("does not call into the content lane when the lifecycle lane has uncommitted events", async () => {
    const harness = createHarnessWithLifecycle();
    await harness.seedTrackedFile(
      "notes/lifecycle-only.md",
      new TextEncoder().encode("seed bytes"),
    );
    await harness.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: SOURCE_VERSION_ID,
        expectedLocator: "notes/lifecycle-only.md",
        targetLocator: "notes/lifecycle-only-renamed.md",
        policyRevision: 2,
        predecessorEventId: null,
      }),
      { newPath: "notes/lifecycle-only-renamed.md" },
    );
    let lifecycleDispatches = 0;
    harness.installLifecycleHandler(async () => {
      lifecycleDispatches += 1;
      return {
        committedAt: "2026-08-20T00:00:00Z",
        eventId: "00000000-0000-4000-8000-000000000000",
        eventSequence: 1,
        resultingLocator: null,
        sourceId: SOURCE_ID,
        sourceVersionId: LIFECYCLE_VERSION_ID,
        state: "active",
        tombstoneId: null,
      };
    });
    const installSpy = harness.installTransport({
      preflight: async () => {
        throw new Error("content lane must not run while lifecycle lane is non-idle");
      },
      content: async () => {
        throw new Error("content lane must not run while lifecycle lane is non-idle");
      },
    });
    void installSpy;
    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("completed");
    expect(summary.processedEventCount).toBe(0);
    expect(lifecycleDispatches).toBe(1);
  });

  it("ends the pass login_required after the SECOND lifecycle login verdict, parking the renames retryable", async () => {
    const harness = createHarnessWithLifecycle();
    await harness.seedTrackedFile(
      "notes/lifecycle-login.md",
      new TextEncoder().encode("seed bytes"),
    );
    const firstRename = await harness.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: SOURCE_VERSION_ID,
        expectedLocator: "notes/lifecycle-login.md",
        targetLocator: "notes/lifecycle-login-renamed.md",
        policyRevision: 2,
        predecessorEventId: null,
      }),
      { newPath: "notes/lifecycle-login-renamed.md" },
    );
    // A SECOND eligible lifecycle row: the post-refresh retry dispatches
    // it, and the server rejects the rotated credential again — only the
    // SECOND login verdict ends the pass (fix round 4's discipline; a
    // single refreshable verdict no longer does).
    const secondRename = await harness.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: SOURCE_VERSION_ID,
        expectedLocator: "notes/lifecycle-login-renamed.md",
        targetLocator: "notes/lifecycle-login-twice.md",
        policyRevision: 2,
        predecessorEventId: null,
      }),
      { newPath: "notes/lifecycle-login-twice.md" },
    );
    harness.installLifecycleHandler(async () => {
      throw new LifecycleApiError("login_required");
    });
    // A queued content event must stay UNTOUCHED: the pass ends at the
    // second lifecycle login verdict instead of dispatching content.
    const contentEvent = await harness.recordContent(
      "notes/lifecycle-login-content.md",
      new TextEncoder().encode("content bytes"),
    );
    harness.installTransport({
      preflight: async () => {
        throw new Error("content lane must not run after a lifecycle login_required verdict");
      },
      content: async () => {
        throw new Error("content lane must not run after a lifecycle login_required verdict");
      },
    });

    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("login_required");
    // The refresh budget was consumed once (the retry that met the second
    // verdict), never twice.
    expect(harness.refreshCalls.count).toBe(1);
    // Both renames are parked retryable under the login_required safe
    // label — never terminal blocked_conflict with zero server contact.
    for (const renameEvent of [firstRename, secondRename]) {
      const parked = harness.repository.readEvent(renameEvent.eventId);
      expect(parked?.state).toBe("waiting_retry");
      expect(parked?.safeError).toBe("login_required");
    }
    // The content lane never dispatched: the queued edit survives intact.
    expect(harness.preflightBodies).toHaveLength(0);
    expect(harness.repository.readEvent(contentEvent.eventId)?.state).toBe("queued");
  });

  it("refreshes once and retries the lifecycle dispatch after a login_required verdict (fix round 4)", async () => {
    const harness = createHarnessWithLifecycle();
    const seeded = await harness.seedTrackedFile(
      "notes/lifecycle-refresh.md",
      new TextEncoder().encode("seed bytes"),
    );
    void seeded;
    const firstRename = await harness.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: SOURCE_VERSION_ID,
        expectedLocator: "notes/lifecycle-refresh.md",
        targetLocator: "notes/lifecycle-refresh-renamed.md",
        policyRevision: 2,
        predecessorEventId: null,
      }),
      { newPath: "notes/lifecycle-refresh-renamed.md" },
    );
    // A SECOND eligible lifecycle row: the post-refresh retry dispatches
    // it (the first rename parks on the login verdict and waits out its
    // own backoff).
    const secondRename = await harness.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: SOURCE_VERSION_ID,
        expectedLocator: "notes/lifecycle-refresh-renamed.md",
        targetLocator: "notes/lifecycle-refresh-twice.md",
        policyRevision: 2,
        predecessorEventId: null,
      }),
      { newPath: "notes/lifecycle-refresh-twice.md" },
    );
    // The lifecycle endpoint rejects the credential until the pass's
    // refresh seam has rotated it.
    harness.installLifecycleHandler(async () => {
      if (harness.refreshCalls.count === 0) {
        throw new LifecycleApiError("login_required");
      }
      return {
        committedAt: "2026-08-20T00:00:00Z",
        eventId: "00000000-0000-4000-8000-000000000000",
        eventSequence: 1,
        resultingLocator: null,
        sourceId: SOURCE_ID,
        sourceVersionId: LIFECYCLE_VERSION_ID,
        state: "active",
        tombstoneId: null,
      };
    });
    const contentEvent = await harness.recordContent(
      "notes/lifecycle-refresh-content.md",
      new TextEncoder().encode("content bytes"),
    );
    harness.installTransport({
      preflight: async () => ({ status: 200, bodyText: SINGLE_PART_BODY }),
      content: async () => ({ status: 200, bodyText: COMMITTED_RECEIPT }),
    });

    const summary = await runPass(harness.driver);
    // The retry committed the second rename and the drain continued; the
    // content lane processed its event under the rotated credential.
    expect(summary.outcome).toBe("completed");
    expect(summary.processedEventCount).toBe(1);
    // Exactly ONE refresh through the shared per-pass budget.
    expect(harness.refreshCalls.count).toBe(1);
    // The first rename is parked retryable under login_required; the
    // second rename committed on the retried dispatch.
    const parkedFirst = harness.repository.readEvent(firstRename.eventId);
    expect(parkedFirst?.state).toBe("waiting_retry");
    expect(parkedFirst?.safeError).toBe("login_required");
    expect(harness.repository.readEvent(secondRename.eventId)?.state).toBe("committed");
    expect(harness.repository.readEvent(contentEvent.eventId)?.state).toBe("committed");
  });

  it("shares the refresh budget with the content lane: no second refresh after the lifecycle retry", async () => {
    const harness = createHarnessWithLifecycle();
    const seeded = await harness.seedTrackedFile(
      "notes/lifecycle-budget.md",
      new TextEncoder().encode("seed bytes"),
    );
    void seeded;
    const rename = await harness.recordLifecycleEvent(
      createLifecycleEventOperands({
        operation: "rename",
        sourceId: SOURCE_ID,
        expectedVersionId: SOURCE_VERSION_ID,
        expectedLocator: "notes/lifecycle-budget.md",
        targetLocator: "notes/lifecycle-budget-renamed.md",
        policyRevision: 2,
        predecessorEventId: null,
      }),
      { newPath: "notes/lifecycle-budget-renamed.md" },
    );
    harness.installLifecycleHandler(async () => {
      if (harness.refreshCalls.count === 0) {
        throw new LifecycleApiError("login_required");
      }
      return {
        committedAt: "2026-08-20T00:00:00Z",
        eventId: "00000000-0000-4000-8000-000000000000",
        eventSequence: 1,
        resultingLocator: null,
        sourceId: SOURCE_ID,
        sourceVersionId: LIFECYCLE_VERSION_ID,
        state: "active",
        tombstoneId: null,
      };
    });
    const contentEvent = await harness.recordContent(
      "notes/lifecycle-budget-content.md",
      new TextEncoder().encode("content bytes"),
    );
    // The content endpoint still answers 401 (mapped to access_expired):
    // the budget the lifecycle retry consumed means the content lane
    // CANNOT refresh again this pass — the second 401 ends the pass
    // login_required.
    harness.installTransport({
      preflight: async () => ({ status: 401, bodyText: errorBody("device_credential_invalid") }),
    });

    const summary = await runPass(harness.driver);
    expect(summary.outcome).toBe("login_required");
    expect(harness.refreshCalls.count).toBe(1);
    const parkedRename = harness.repository.readEvent(rename.eventId);
    expect(parkedRename?.state).toBe("waiting_retry");
    expect(parkedRename?.safeError).toBe("login_required");
    const parkedContent = harness.repository.readEvent(contentEvent.eventId);
    expect(parkedContent?.state).toBe("waiting_retry");
    expect(parkedContent?.safeError).toBe("login_required");
  });
});
