/**
 * Tests of the Mobile-safe multipart upload runner (child 7 spec 4.3, 6.2, 8).
 *
 * Every test drives the REAL journal (sql.js engine, real repository, real
 * fingerprint derivation) and the REAL hand-mirrored sync client over a
 * scripted raw multipart server, with a fixed clock. The pinned behaviors:
 * resume calls status before any part URL, the frozen local file is opened
 * and re-fingerprinted before each unfinished range, exactly one part URL is
 * requested and consumed by one PUT whose response object is immediately
 * discarded, the platform concurrency semaphore caps part PUTs at three
 * (Desktop) and two (Mobile), a rejected part URL retries status then one
 * replacement URL, suspend/timeout/offline persist safe progress and rethrow
 * the existing retryable closed failure, and a changed local file requests
 * exact abort and surfaces the closed `multipart_local_content_changed`
 * token without touching the newer watcher event. No presigned URL, query
 * signature or provider detail ever reaches SQLite.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { JournalEvent, JournalMeta, MultipartProgressRecord } from "./contracts";
import { MULTIPART_PART_SIZE_BYTES } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import { MultipartUploadRunner } from "./multipart-upload";
import { JournalRepository } from "./repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";
import { createJournalSyncApi } from "./sync-api";
import type { SyncHttpRequest } from "./sync-api";

// --- shared fixtures ---------------------------------------------------------------------------

/** The real sql.js WebAssembly engine drives every runner test (spec 6.1). */
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
const R2_BASE = "https://r2.example.net";
const ACCESS_TOKEN = "at1.multipart-test-access";
const SESSION_ID = "bXVsdGlwYXJ0LXNlc3Npb24taWRlbnRpdHktMDEyMzQ1Njc4OTA";
const SOURCE_ID = "44444444-4444-4444-8444-444444444444";
const SOURCE_VERSION_ID = "55555555-5555-4555-8555-555555555555";
const CLOCK_EPOCH_MS = 1_784_000_000_000;
const EXPIRES_AT = "2026-08-29T00:00:00Z";
const NORMALIZED_PATH = "notes/big-asset.bin";
const DEFAULT_LAST_PART_BYTES = 1_048_576 + 123;

type RawResponse = { status: number; bodyText: string };

/** One scripted part-PUT directive: a raw response, a hanging request or a lost transport. */
type PartPutDirective = RawResponse | "hang" | "throw_offline";

interface MultipartCall {
  readonly kind: "create" | "status" | "part_url" | "part_put" | "complete" | "abort";
  readonly partNumber: number | null;
}

function successBody(data: unknown): string {
  return JSON.stringify({
    data,
    error: null,
    request_id: "66666666-6666-4666-8666-666666666666",
    warnings: [],
  });
}

function terminalResultBody(): Record<string, unknown> {
  return {
    result_kind: "committed",
    source_id: SOURCE_ID,
    source_version_id: SOURCE_VERSION_ID,
    content_version: 2,
    committed_at: "2026-08-28T00:00:00Z",
  };
}

/**
 * The scripted raw multipart server behind the real sync client: it routes
 * the five authenticated API endpoints plus the presigned part PUTs, tracks
 * the observable call order, the per-part URL issuance and PUT attempt
 * counts, the in-flight part-PUT concurrency and the server-observed
 * completed part set. Hook properties let each test script one behavior;
 * every hook stays inside the closed wire grammar.
 */
class ScriptedMultipartServer {
  readonly calls: MultipartCall[] = [];
  readonly createBodies: Record<string, unknown>[] = [];
  readonly partPutNumbersAtReceipt: number[] = [];
  readonly partPutUrls: string[] = [];
  readonly partPutRequests: SyncHttpRequest[] = [];
  readonly partUrlIssuanceCounts = new Map<number, number>();
  readonly partPutAttemptCounts = new Map<number, number>();
  /** The parts the server had already accepted before this run (seeded resume truth). */
  readonly presetCompletedParts = new Set<number>();
  #observedCompletedParts = new Set<number>();
  #activePartPuts = 0;
  #maximumActivePartPuts = 0;

  constructor(
    readonly partCount: number,
    readonly lastPartSizeBytes: number,
  ) {}

  get maximumActivePartPuts(): number {
    return this.#maximumActivePartPuts;
  }

  get serverCompletedParts(): readonly number[] {
    return [...new Set([...this.presetCompletedParts, ...this.#observedCompletedParts])].sort(
      (left, right) => left - right,
    );
  }

  /** Overridable create behavior; the default returns the plan geometry. */
  onCreate: (body: Record<string, unknown>) => RawResponse | Promise<RawResponse> = () => ({
    status: 200,
    bodyText: successBody({
      session_id: SESSION_ID,
      part_count: this.partCount,
      part_size_bytes: MULTIPART_PART_SIZE_BYTES,
      expires_at: EXPIRES_AT,
    }),
  });

  /** Overridable status behavior; the default reconciles the completed set. */
  onStatus: (completedParts: readonly number[]) => RawResponse | Promise<RawResponse> = (
    completedParts,
  ) => ({
    status: 200,
    bodyText: this.statusBodyOf("uploading", completedParts),
  });

  /** Overridable part-URL issuance; the default signs one fresh URL per call. */
  onPartUrl: (partNumber: number, issuance: number) => RawResponse | Promise<RawResponse> = (
    partNumber,
    issuance,
  ) => ({
    status: 200,
    bodyText: successBody({
      url: `${R2_BASE}/staging/${SESSION_ID}/part-${partNumber}?X-Amz-Signature=secret-${partNumber}-${issuance}&part=${partNumber}`,
      part_number: partNumber,
      offset_bytes: (partNumber - 1) * MULTIPART_PART_SIZE_BYTES,
      size_bytes: this.expectedPartSizeBytes(partNumber),
      expires_at: EXPIRES_AT,
    }),
  });

  /** Overridable part-PUT behavior; the default accepts the exact bytes. */
  onPartPut: (
    partNumber: number,
    attempt: number,
  ) => PartPutDirective | Promise<PartPutDirective> = () => ({ status: 200, bodyText: "" });

  /** Overridable completion; the default commits once every part landed. */
  onComplete: (completedParts: readonly number[]) => RawResponse | Promise<RawResponse> = (
    completedParts,
  ) =>
    completedParts.length === this.partCount
      ? {
          status: 200,
          bodyText: successBody({
            state: "committed",
            terminal_result: terminalResultBody(),
          }),
        }
      : { status: 200, bodyText: successBody({ state: "completing" }) };

  /** Overridable abort; the default accepts the cancellation. */
  onAbort: (completedParts: readonly number[]) => RawResponse | Promise<RawResponse> = (
    completedParts,
  ) => ({
    status: 200,
    bodyText: this.statusBodyOf("cancelling", completedParts),
  });

  statusBodyOf(state: string, completedParts: readonly number[]): string {
    return successBody({
      session_id: SESSION_ID,
      state,
      part_count: this.partCount,
      part_size_bytes: MULTIPART_PART_SIZE_BYTES,
      expires_at: EXPIRES_AT,
      completed_part_numbers: [...completedParts],
    });
  }

  expectedPartSizeBytes(partNumber: number): number {
    return partNumber < this.partCount ? MULTIPART_PART_SIZE_BYTES : this.lastPartSizeBytes;
  }

  /** Route one raw transport request onto the scripted multipart surface. */
  async respond(request: SyncHttpRequest): Promise<RawResponse> {
    const url = new URL(request.url);
    if (url.origin === R2_BASE && request.method === "PUT") {
      return this.#respondPartPut(request);
    }
    if (url.origin !== ORIGIN) {
      throw new Error("unexpected transport origin");
    }
    const sessionIdPath = `/api/uploads/multipart-sessions/${SESSION_ID}`;
    const partUrlMatch = url.pathname.match(
      /^\/api\/uploads\/multipart-sessions\/[^/]+\/parts\/(\d+)\/url$/,
    );
    if (request.method === "POST" && url.pathname === "/api/uploads/multipart-sessions") {
      this.calls.push({ kind: "create", partNumber: null });
      const body = JSON.parse(request.body as string) as Record<string, unknown>;
      this.createBodies.push(body);
      return this.onCreate(body);
    }
    if (request.method === "GET" && url.pathname === sessionIdPath) {
      this.calls.push({ kind: "status", partNumber: null });
      return this.onStatus(this.serverCompletedParts);
    }
    if (request.method === "POST" && partUrlMatch !== null) {
      const partNumber = Number(partUrlMatch[1]);
      const issuance = (this.partUrlIssuanceCounts.get(partNumber) ?? 0) + 1;
      this.partUrlIssuanceCounts.set(partNumber, issuance);
      this.calls.push({ kind: "part_url", partNumber });
      return this.onPartUrl(partNumber, issuance);
    }
    if (request.method === "POST" && url.pathname === `${sessionIdPath}/complete`) {
      this.calls.push({ kind: "complete", partNumber: null });
      return this.onComplete(this.serverCompletedParts);
    }
    if (request.method === "POST" && url.pathname === `${sessionIdPath}/abort`) {
      this.calls.push({ kind: "abort", partNumber: null });
      return this.onAbort(this.serverCompletedParts);
    }
    throw new Error("unexpected api route");
  }

  async #respondPartPut(request: SyncHttpRequest): Promise<RawResponse> {
    const partNumber = Number(new URL(request.url).searchParams.get("part"));
    this.calls.push({ kind: "part_put", partNumber });
    this.partPutNumbersAtReceipt.push(partNumber);
    this.partPutUrls.push(request.url);
    this.partPutRequests.push(request);
    const attempt = (this.partPutAttemptCounts.get(partNumber) ?? 0) + 1;
    this.partPutAttemptCounts.set(partNumber, attempt);
    this.#activePartPuts += 1;
    this.#maximumActivePartPuts = Math.max(this.#maximumActivePartPuts, this.#activePartPuts);
    try {
      const directive = await this.onPartPut(partNumber, attempt);
      if (directive === "throw_offline") {
        throw new Error("transport gone");
      }
      if (directive !== "hang") {
        if (directive.status >= 200 && directive.status < 300) {
          this.#observedCompletedParts.add(partNumber);
        }
        return directive;
      }
      await new Promise<void>(() => undefined);
      return { status: 200, bodyText: "" };
    } finally {
      this.#activePartPuts -= 1;
    }
  }
}

/** Deterministic non-trivial bytes: position-derived, no provider content. */
function deterministicBytes(totalSizeBytes: number): Uint8Array {
  const bytes = new Uint8Array(totalSizeBytes);
  for (let index = 0; index < totalSizeBytes; index += 1) {
    bytes[index] = (index * 31 + 7) & 0xff;
  }
  return bytes;
}

/** A full database dump for sentinel scans (the task 9 pattern). */
function databaseDump(database: SqliteDatabase): string {
  const parts: string[] = [new TextDecoder("latin1").decode(database.exportImage())];
  const tables = database.readAll(
    "select name from sqlite_master where type = 'table' order by name;",
  );
  for (const row of tables[0]?.values ?? []) {
    parts.push(JSON.stringify(database.readAll(`select * from ${String(row[0])};`)));
  }
  return parts.join("\n");
}

/** Poll until one observable condition holds; bounded so a stall fails loudly. */
async function waitUntil(condition: () => boolean, timeoutMs = 5_000): Promise<void> {
  const startedAt = Date.now();
  while (!condition()) {
    if (Date.now() - startedAt > timeoutMs) {
      throw new Error("condition not reached before the bounded wait expired");
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

/** One externally resolved latch. */
function createDeferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  const promise = new Promise<void>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

interface MultipartHarness {
  readonly repository: JournalRepository & { serializedState(): string };
  readonly runner: MultipartUploadRunner;
  readonly server: ScriptedMultipartServer;
  readonly vaultBytes: Map<string, Uint8Array>;
  readonly maxActivePartPuts: () => number;
  readonly partPutNumbers: () => readonly number[];
  readonly calls: () => readonly MultipartCall[];
}

function createHarness(options?: {
  readonly partCount?: number;
  readonly lastPartSizeBytes?: number;
  readonly requestTimeoutMs?: number;
  readonly beforeReadRegularFileBytes?: (readCount: number) => Promise<void>;
}): MultipartHarness {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  } satisfies JournalMeta);
  const partCount = options?.partCount ?? 3;
  const lastPartSizeBytes = options?.lastPartSizeBytes ?? DEFAULT_LAST_PART_BYTES;
  const repository = new JournalRepository({
    database,
    nowEpochMs: () => CLOCK_EPOCH_MS,
    createId: () => "77777777-7777-4777-8777-777777777777",
  });
  const server = new ScriptedMultipartServer(partCount, lastPartSizeBytes);
  const vaultBytes = new Map<string, Uint8Array>();
  let fileReadCount = 0;
  const totalSizeBytes = (partCount - 1) * MULTIPART_PART_SIZE_BYTES + lastPartSizeBytes;
  vaultBytes.set(NORMALIZED_PATH, deterministicBytes(totalSizeBytes));
  const syncApi = createJournalSyncApi({
    transport: (request) => server.respond(request),
    resolveOrigin: () => ORIGIN,
    getAccessToken: () => ACCESS_TOKEN,
  });
  const runner = new MultipartUploadRunner({
    repository,
    syncApi,
    fileBytesReader: {
      readRegularFileBytes: async (normalizedPath) => {
        fileReadCount += 1;
        await options?.beforeReadRegularFileBytes?.(fileReadCount);
        return vaultBytes.get(normalizedPath) ?? null;
      },
    },
    nowEpochMs: () => CLOCK_EPOCH_MS,
    requestTimeoutMs: options?.requestTimeoutMs ?? 5_000,
  });
  const repositoryWithDump = Object.assign(repository, {
    serializedState: () => databaseDump(database),
  });
  return {
    repository: repositoryWithDump,
    runner,
    server,
    vaultBytes,
    maxActivePartPuts: () => server.maximumActivePartPuts,
    partPutNumbers: () => [...server.partPutNumbersAtReceipt],
    calls: () => [...server.calls],
  };
}

/**
 * Capture the harness's allowed content event (and optionally seed durable
 * session progress) and return the frozen event the runner will resume.
 */
async function seedHarnessEvent(
  harness: MultipartHarness,
  options?: { readonly seedProgress?: readonly number[] },
): Promise<JournalEvent> {
  const bytes = harness.vaultBytes.get(NORMALIZED_PATH);
  if (bytes === undefined) {
    throw new Error("harness vault bytes missing");
  }
  const capture = await harness.repository.recordCapture({
    normalizedPath: NORMALIZED_PATH,
    fingerprint: await deriveFrozenFingerprint(bytes),
    policyRevisionNumber: 4,
    admission: "policy_allowed",
  });
  if (capture.outcome !== "event_recorded") {
    throw new Error("expected a recorded capture");
  }
  await harness.repository.markEventPreflightStarted(capture.event.eventId);
  if (options?.seedProgress !== undefined) {
    const record: MultipartProgressRecord = {
      eventId: capture.event.eventId,
      sessionId: SESSION_ID,
      partSizeBytes: MULTIPART_PART_SIZE_BYTES,
      partCount: harness.server.partCount,
      expiresAtEpochMs: Date.parse(EXPIRES_AT),
      completedPartNumbers: [...options.seedProgress],
      sessionState: "uploading",
      safeReason: null,
    };
    await harness.repository.saveMultipartProgress(record);
    // The seeded progress is also the server-side truth: the provider had
    // accepted exactly those parts before the interruption.
    for (const partNumber of options.seedProgress) {
      harness.server.presetCompletedParts.add(partNumber);
    }
  }
  return capture.event;
}

/** The index of the nth call of one closed kind/part pair, or -1. */
function nthCallIndex(
  calls: readonly MultipartCall[],
  kind: MultipartCall["kind"],
  partNumber: number | null,
  occurrence: number,
): number {
  let seen = 0;
  for (let index = 0; index < calls.length; index += 1) {
    const call = calls[index];
    if (call !== undefined && call.kind === kind && call.partNumber === partNumber) {
      seen += 1;
      if (seen === occurrence) {
        return index;
      }
    }
  }
  return -1;
}

// --- resume and scheduling (child 7 spec 4.3, 6.2) ----------------------------------------------

describe("multipart upload runner resume and scheduling (child 7 spec 4.3, 6.2)", () => {
  it("resumes only unfinished Mobile parts with maximum two active PUTs", async () => {
    const firstFileRead = createDeferred();
    const harness = createHarness({
      // Hold part 2 before it reaches the semaphore so part 3 proves that
      // receipt chronology is not a scheduler contract.
      beforeReadRegularFileBytes: (readCount) => (readCount === 1 ? firstFileRead.promise : Promise.resolve()),
    });
    const event = await seedHarnessEvent(harness, { seedProgress: [1] });
    // Gate both part PUTs so the two-permit Mobile semaphore must engage.
    const gates = new Map<number, { promise: Promise<void>; resolve: () => void }>();
    harness.server.onPartPut = (partNumber) => {
      const gate = createDeferred();
      gates.set(partNumber, gate);
      return gate.promise.then(() => ({ status: 200, bodyText: "" }));
    };
    const runPromise = harness.runner.run(event, "mobile");
    await waitUntil(() => harness.partPutNumbers()[0] === 3);
    firstFileRead.resolve();
    await waitUntil(() => gates.size === 2);
    for (const gate of gates.values()) {
      gate.resolve();
    }
    const result = await runPromise;

    expect(result.outcome).toBe("committed");
    expect(harness.maxActivePartPuts()).toBeLessThanOrEqual(2);
    expect(harness.maxActivePartPuts()).toBe(2);
    expect(harness.partPutNumbers()).toHaveLength(2);
    expect(harness.partPutNumbers().slice().sort((a, b) => a - b)).toEqual([2, 3]);
    // Status precedes every part URL on resume, and no create may run.
    const calls = harness.calls();
    expect(calls[0]?.kind).toBe("status");
    expect(calls.some((call) => call.kind === "create")).toBe(false);
    expect(nthCallIndex(calls, "status", null, 1)).toBeLessThan(
      nthCallIndex(calls, "part_url", 2, 1),
    );
    // Every completed part — reconciled and newly uploaded — persists ascending.
    const progress = harness.repository.readMultipartProgress(event.eventId);
    expect(progress?.completedPartNumbers).toEqual([1, 2, 3]);
  });

  it("never stores a presigned URL after a part PUT", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness);
    const result = await harness.runner.run(event, "desktop");

    expect(result.outcome).toBe("committed");
    expect(harness.partPutNumbers()).toHaveLength(3);
    expect(harness.repository.serializedState()).not.toContain("X-Amz-Signature");
    expect(harness.repository.serializedState()).not.toContain(R2_BASE);
  });

  it("issues one URL per part PUT and sends no credential with the part bytes", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness, { seedProgress: [1, 3] });
    const result = await harness.runner.run(event, "desktop");

    expect(result.outcome).toBe("committed");
    expect(harness.partPutNumbers()).toEqual([2]);
    expect(harness.server.partUrlIssuanceCounts.get(2)).toBe(1);
    // Every issued URL is consumed by exactly one PUT; the URLs stay unique.
    expect(new Set(harness.server.partPutUrls).size).toBe(harness.server.partPutUrls.length);
    // The presigned PUT carries no service credential.
    for (const request of harness.server.partPutRequests) {
      expect(request.headers["authorization"]).toBeUndefined();
    }
  });

  it("creates the bound session before any part URL when no progress exists", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness);
    const result = await harness.runner.run(event, "desktop");

    expect(result.outcome).toBe("committed");
    const calls = harness.calls();
    expect(calls[0]?.kind).toBe("create");
    expect(nthCallIndex(calls, "part_url", 1, 1)).toBeGreaterThan(0);
    const createBody = harness.server.createBodies[0];
    expect(createBody).toMatchObject({
      event_id: event.eventId,
      idempotency_key: event.idempotencyKey,
      operation: "create",
      local_file_id: event.localFileId,
      normalized_locator: NORMALIZED_PATH,
      sha256: event.fingerprint.sha256,
      size_bytes: event.fingerprint.sizeBytes,
      media_type: event.fingerprint.mediaType,
      policy_revision: 4,
    });
    expect(createBody).not.toHaveProperty("source_id");
    // The created session persists before the first byte moves.
    const progress = harness.repository.readMultipartProgress(event.eventId);
    expect(progress?.sessionId).toBeDefined();
    expect(progress?.partCount).toBe(3);
  });

  it("caps Desktop part PUTs at three", async () => {
    const harness = createHarness({ partCount: 4 });
    const event = await seedHarnessEvent(harness);
    const gates = new Map<number, { promise: Promise<void>; resolve: () => void }>();
    harness.server.onPartPut = (partNumber) => {
      const gate = createDeferred();
      gates.set(partNumber, gate);
      return gate.promise.then(() => ({ status: 200, bodyText: "" }));
    };
    const runPromise = harness.runner.run(event, "desktop");
    await waitUntil(() => gates.size === 3);
    expect(harness.maxActivePartPuts()).toBe(3);
    for (const gate of gates.values()) {
      gate.resolve();
    }
    // The fourth part enters only after one permit freed: release it too.
    await waitUntil(() => gates.size === 4);
    for (const gate of gates.values()) {
      gate.resolve();
    }
    const result = await runPromise;
    expect(result.outcome).toBe("committed");
    expect(harness.maxActivePartPuts()).toBeLessThanOrEqual(3);
  });

  it("replaces one rejected part URL through status and exactly one reissue", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness, { seedProgress: [1] });
    harness.server.onPartPut = (partNumber, attempt) =>
      partNumber === 2 && attempt === 1
        ? { status: 403, bodyText: "<Error><Code>AccessDenied</Code></Error>" }
        : { status: 200, bodyText: "" };

    const result = await harness.runner.run(event, "mobile");

    expect(result.outcome).toBe("committed");
    expect(harness.server.partUrlIssuanceCounts.get(2)).toBe(2);
    expect(harness.server.partUrlIssuanceCounts.get(3)).toBe(1);
    // The reconciliation status call sits strictly between the rejected PUT
    // and the replacement URL of the same part.
    const calls = harness.calls();
    const rejectedPutIndex = nthCallIndex(calls, "part_put", 2, 1);
    const reissueIndex = nthCallIndex(calls, "part_url", 2, 2);
    expect(rejectedPutIndex).toBeGreaterThanOrEqual(0);
    expect(reissueIndex).toBeGreaterThan(rejectedPutIndex);
    const callsBetween = calls.slice(rejectedPutIndex + 1, reissueIndex);
    expect(callsBetween.some((call) => call.kind === "status")).toBe(true);
  });

  it("parks under the closed url-rejected token when the replacement URL is also rejected", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness, { seedProgress: [1, 3] });
    harness.server.onPartPut = () => ({
      status: 403,
      bodyText: "<Error><Code>AccessDenied</Code></Error>",
    });

    await expect(harness.runner.run(event, "mobile")).rejects.toMatchObject({
      kind: "server_error",
    });
    // Exactly two URLs for the one unfinished part — the original and the
    // single replacement — and the closed token lands on the safe progress.
    expect(harness.server.partUrlIssuanceCounts.get(2)).toBe(2);
    const progress = harness.repository.readMultipartProgress(event.eventId);
    expect(progress?.safeReason).toBe("multipart_part_url_rejected");
    expect(progress?.completedPartNumbers).toEqual([1, 3]);
    expect(harness.calls().some((call) => call.kind === "complete")).toBe(false);
  });

  it("keeps durable progress and rethrows the closed retryable failure when offline", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness, { seedProgress: [1, 3] });
    harness.server.onPartPut = () => "throw_offline";

    await expect(harness.runner.run(event, "mobile")).rejects.toMatchObject({
      kind: "network_offline",
    });
    const progress = harness.repository.readMultipartProgress(event.eventId);
    expect(progress?.completedPartNumbers).toEqual([1, 3]);
    expect(progress?.safeReason).toBeNull();

    // A later foreground run resumes exactly the unfinished part.
    harness.server.onPartPut = () => ({ status: 200, bodyText: "" });
    const result = await harness.runner.run(event, "mobile");
    expect(result.outcome).toBe("committed");
    expect(harness.partPutNumbers()).toEqual([2, 2]);
  });

  it("classifies one stalled part PUT as a retryable timeout with progress retained", async () => {
    const harness = createHarness({ requestTimeoutMs: 60 });
    const event = await seedHarnessEvent(harness, { seedProgress: [1, 3] });
    harness.server.onPartPut = () => "hang";

    await expect(harness.runner.run(event, "mobile")).rejects.toMatchObject({
      kind: "network_timeout",
    });
    const progress = harness.repository.readMultipartProgress(event.eventId);
    expect(progress?.completedPartNumbers).toEqual([1, 3]);
    expect(progress?.sessionId).toBe(SESSION_ID);
  });

  it("stops new part work on suspend and rethrows the retryable closed failure", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness, { seedProgress: [1] });
    const suspendController = new AbortController();
    const gates = new Map<number, { promise: Promise<void>; resolve: () => void }>();
    harness.server.onPartPut = (partNumber) => {
      const gate = createDeferred();
      gates.set(partNumber, gate);
      return gate.promise.then(() => ({ status: 200, bodyText: "" }));
    };
    const runPromise = harness.runner.run(event, "mobile", {
      signal: suspendController.signal,
    });
    // Suspend fires once both permitted part PUTs are already in flight: the
    // in-flight parts still finish and persist; the completion never starts.
    await waitUntil(() => gates.size === 2);
    suspendController.abort();
    for (const gate of gates.values()) {
      gate.resolve();
    }
    await expect(runPromise).rejects.toMatchObject({ kind: "network_timeout" });
    const calls = harness.calls();
    expect(calls.some((call) => call.kind === "complete")).toBe(false);
    const progress = harness.repository.readMultipartProgress(event.eventId);
    expect(progress?.completedPartNumbers).toEqual([1, 2, 3]);
  });

  it("aborts the session and reports the closed change token when the frozen file changes", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness, { seedProgress: [1, 2] });
    harness.server.onStatus = (completedParts) => {
      // The user saves mid-resume: the bytes change after the status call.
      harness.vaultBytes.set(
        NORMALIZED_PATH,
        deterministicBytes(
          (harness.server.partCount - 1) * MULTIPART_PART_SIZE_BYTES +
            harness.server.lastPartSizeBytes +
            1,
        ),
      );
      return {
        status: 200,
        bodyText: harness.server.statusBodyOf("uploading", completedParts),
      };
    };

    const result = await harness.runner.run(event, "mobile");

    expect(result).toEqual({ outcome: "local_content_changed" });
    const calls = harness.calls();
    expect(calls.some((call) => call.kind === "part_url")).toBe(false);
    expect(calls.some((call) => call.kind === "part_put")).toBe(false);
    expect(calls.at(-1)?.kind).toBe("abort");
    const progress = harness.repository.readMultipartProgress(event.eventId);
    expect(progress?.safeReason).toBe("multipart_local_content_changed");
    expect(progress?.completedPartNumbers).toEqual([1, 2]);
  });

  it("returns the frozen committed receipt when status already reports the terminal result", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness, { seedProgress: [1] });
    harness.server.onStatus = () => ({
      status: 200,
      bodyText: successBody({
        session_id: SESSION_ID,
        state: "committed",
        part_count: harness.server.partCount,
        part_size_bytes: MULTIPART_PART_SIZE_BYTES,
        expires_at: EXPIRES_AT,
        completed_part_numbers: [1, 2, 3],
        terminal_result: terminalResultBody(),
      }),
    });

    const result = await harness.runner.run(event, "mobile");

    expect(result).toEqual({
      outcome: "committed",
      receipt: {
        sourceId: SOURCE_ID,
        sourceVersionId: SOURCE_VERSION_ID,
        contentVersion: 2,
      },
    });
    const calls = harness.calls();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.kind).toBe("status");
  });

  it("rejects a server plan whose geometry violates the frozen contract", async () => {
    const harness = createHarness();
    const event = await seedHarnessEvent(harness);
    harness.server.onCreate = () => ({
      status: 200,
      bodyText: successBody({
        session_id: SESSION_ID,
        part_count: harness.server.partCount,
        part_size_bytes: 4,
        expires_at: EXPIRES_AT,
      }),
    });

    await expect(harness.runner.run(event, "desktop")).rejects.toMatchObject({
      kind: "server_error",
    });
    expect(harness.partPutNumbers()).toEqual([]);
    expect(harness.repository.readMultipartProgress(event.eventId)).toBeNull();
  });
});
