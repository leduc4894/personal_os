/**
 * Committed regression suite: the lifecycle lane shares the pass's
 * one-per-pass credential refresh (fix round 4).
 *
 * The live defect: with a PENDING lifecycle event in the journal and a
 * stale (present-but-server-rejected 401) or null access credential,
 * every queue pass and every scheduled-retry firing died in the lifecycle
 * lane (`login_required`) BEFORE the content lane ran — so content
 * create/edit events never processed and the pass's ONE credential
 * refresh (`refreshAccessToken: () => session.refresh()`, wired at
 * plugin.ts) was NEVER called. The queue stalled forever and recovered
 * only through an external credential rotation; the identical stale
 * token self-healed in one pass when no lifecycle event was pending
 * (the control scenario below).
 *
 * The fix (controller ruling, spec 8's exactly-one-refresh-per-pass
 * discipline): when the lifecycle drain's `runOne` returns
 * `login_required` and the pass's refresh budget is unspent, the queue
 * driver consumes the budget, calls `refreshAccessToken()` once and
 * retries the lifecycle dispatch ONCE. A second `login_required` (or a
 * failed refresh) keeps the park-and-end-pass semantics — the budget is
 * shared with the content lane, which simply cannot refresh again that
 * pass.
 *
 * The harness reuses the patterns of automatic-vault-convergence.test.ts
 * (real sql.js journal, real repository, real capture set, real
 * JournalQueueDriver + lifecycle lane, real CoalescingQueuePassDispatcher,
 * real openapi-fetch lifecycle adapter, real hand-mirrored sync client) with
 * ONE widening: both server doubles validate the Bearer token exactly like
 * the real auth layer (stale token -> 401, healthy token -> healthy), and
 * `refreshAccessToken` models `session.refresh()` — when called it swaps in
 * a healthy credential for BOTH lanes (both read the same session object, per
 * plugin.ts:518/529/533).
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import {
  AutomaticSnapshotCoordinator,
  CoalescingQueuePassDispatcher,
} from "./automatic-snapshot";
import type { AutomaticSnapshotResult } from "./automatic-snapshot";
import { FILE_SETTLE_DELAY_MS } from "./contracts";
import type { JournalEvent } from "./contracts";
import { JournalCapture } from "./capture";
import { LifecycleCaptureImpl } from "./lifecycle-capture";
import { LifecycleDriverImpl } from "./lifecycle-driver";
import { createRequestUrlLifecycleApi } from "./lifecycle-api";
import { JournalPersistence } from "./persistence";
import type { JournalFileStore } from "./persistence";
import { JournalQueueDriver } from "./queue-driver";
import type { QueuePassOutcome, QueuePassSummary } from "./queue-driver";
import { JournalRepository } from "./repository";
import type { JournalRepositoryDatabase } from "./repository";
import { projectJournalSyncStatus, renderJournalSyncStatusText } from "./status";
import type { LifecycleBlockedReasonCode } from "./status";
import { createJournalSyncApi } from "./sync-api";
import type { SyncHttpRequest } from "./sync-api";

// --- the engine and the durable journal directory ---------------------------------------------

let engineModule: import("./sqlite-database").SqliteEngineModule;

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

class InMemoryJournalFileStore implements JournalFileStore {
  readonly files = new Map<string, ArrayBuffer>();

  async exists(fileName: string): Promise<boolean> {
    return this.files.has(fileName);
  }

  async readBinary(fileName: string): Promise<ArrayBuffer> {
    const data = this.files.get(fileName);
    if (data === undefined) {
      throw new Error("file not found");
    }
    return data.slice(0);
  }

  async writeBinary(fileName: string, data: ArrayBuffer): Promise<void> {
    this.files.set(fileName, data.slice(0));
  }

  async remove(fileName: string): Promise<void> {
    this.files.delete(fileName);
  }
}

// --- the token-validating in-process server doubles --------------------------------------------

const ORIGIN = "https://sync.example.org";
const HEALTHY_TOKEN = "at1.healthy-session-token";
const STALE_TOKEN = "at1.stale-expired-token";
const REFRESHED_TOKEN = "at1.refreshed-token";

/** The real auth layer shape: any presented token except the stale one passes. */
function acceptsToken(bearerHeader: string | null): boolean {
  return bearerHeader === `Bearer ${HEALTHY_TOKEN}` || bearerHeader === `Bearer ${REFRESHED_TOKEN}`;
}

function syncSuccessBody(data: unknown): string {
  return JSON.stringify({
    data,
    error: null,
    request_id: "66666666-6666-4666-8666-666666666666",
    warnings: [],
  });
}

function syncErrorBody(code: string): string {
  return JSON.stringify({
    data: null,
    error: { code, message: "registered safe message", details: {}, retryable: false },
    request_id: "66666666-6666-4666-8666-666666666666",
    warnings: [],
  });
}

interface FrozenTerminal {
  readonly resultKind: "committed" | "no_change";
  readonly sourceId: string;
  readonly sourceVersionId: string;
  readonly contentVersion: number;
}

/**
 * The stateful wire-contract double behind the content sync transport
 * (mirrors the convergence suite's double) plus Bearer validation: a request
 * presenting the stale token is answered 401 with the canonical error
 * envelope — exactly what the real auth middleware returns — while a healthy
 * token gets the full healthy behavior.
 */
class SyncServerDouble {
  readonly identityTerminals = new Map<string, FrozenTerminal>();
  readonly currentVersions = new Map<string, string>();
  readonly preflightBodies: Record<string, unknown>[] = [];
  readonly receivedDigests: string[] = [];
  /** Bearer tokens the content endpoint actually saw, oldest first. */
  readonly presentedTokens: (string | null)[] = [];
  readonly rejectedWith401Count = { count: 0 };
  publications = 0;
  #counter = 0;

  async handlePreflight(
    body: Record<string, unknown>,
    bearerHeader: string | null,
  ): Promise<{ status: number; bodyText: string }> {
    this.preflightBodies.push(body);
    this.presentedTokens.push(bearerHeader);
    if (!acceptsToken(bearerHeader ?? null)) {
      this.rejectedWith401Count.count += 1;
      return { status: 401, bodyText: syncErrorBody("device_credential_invalid") };
    }
    const identity = `${body["event_id"]}:${body["idempotency_key"]}`;
    const frozen = this.identityTerminals.get(identity);
    if (frozen !== undefined) {
      const outcome = frozen.resultKind === "committed" ? "committed_replay" : "no_change";
      return { status: 200, bodyText: syncSuccessBody({ outcome, result: frozen }) };
    }
    if (body["operation"] === "update") {
      const sourceId = String(body["source_id"]);
      const current = this.currentVersions.get(sourceId);
      if (current !== undefined && current !== body["base_version_id"]) {
        return { status: 200, bodyText: syncSuccessBody({ outcome: "conflict" }) };
      }
    }
    this.#counter += 1;
    const operationId = `${String(this.#counter).padStart(10, "0")}AbCdEfGhIjKlMnOpQrStUvWxYz`;
    this.operations.set(operationId, {
      identity,
      sourceId:
        body["operation"] === "update" ? String(body["source_id"]) : countedUuid("1", this.#counter),
      sha256: String(body["sha256"]),
      sizeBytes: Number(body["size_bytes"]),
      terminal: null,
    });
    return {
      status: 200,
      bodyText: syncSuccessBody({
        outcome: "single_part_upload",
        operation_id: operationId,
        expires_at: "2026-08-22T10:00:00Z",
      }),
    };
  }

  readonly operations = new Map<
    string,
    { identity: string; sourceId: string; sha256: string; sizeBytes: number; terminal: FrozenTerminal | null }
  >();

  async handleContent(
    operationId: string,
    bytes: Uint8Array,
    bearerHeader: string | null,
  ): Promise<{ status: number; bodyText: string }> {
    if (!acceptsToken(bearerHeader ?? null)) {
      this.rejectedWith401Count.count += 1;
      return { status: 401, bodyText: syncErrorBody("device_credential_invalid") };
    }
    const operation = this.operations.get(operationId);
    if (operation === undefined) {
      return { status: 404, bodyText: syncErrorBody("small_file_operation_not_found") };
    }
    if (operation.terminal !== null) {
      return { status: 200, bodyText: syncSuccessBody(this.#resultBody(operation.terminal)) };
    }
    const digest = await sha256HexOf(bytes);
    if (digest !== operation.sha256 || bytes.byteLength !== operation.sizeBytes) {
      return { status: 422, bodyText: syncErrorBody("small_file_content_integrity_failed") };
    }
    this.#counter += 1;
    const sourceVersionId = countedUuid("2", this.#counter);
    const priorContentVersion = [...this.identityTerminals.values()].find(
      (terminal) => terminal.sourceId === operation.sourceId,
    )?.contentVersion;
    const terminal: FrozenTerminal = {
      resultKind: "committed",
      sourceId: operation.sourceId,
      sourceVersionId,
      contentVersion: (priorContentVersion ?? 0) + 1,
    };
    this.currentVersions.set(operation.sourceId, sourceVersionId);
    this.identityTerminals.set(operation.identity, terminal);
    operation.terminal = terminal;
    this.receivedDigests.push(digest);
    this.publications += 1;
    return { status: 200, bodyText: syncSuccessBody(this.#resultBody(terminal)) };
  }

  #resultBody(terminal: FrozenTerminal): Record<string, unknown> {
    return {
      result_kind: terminal.resultKind,
      source_id: terminal.sourceId,
      source_version_id: terminal.sourceVersionId,
      content_version: terminal.contentVersion,
      committed_at: "2026-08-22T00:00:00Z",
    };
  }
}

async function sha256HexOf(bytes: Uint8Array): Promise<string> {
  const { sha256Hex } = await import("../exclusion-policy/canonical-json");
  return sha256Hex(bytes);
}

function countedUuid(prefixDigit: string, counter: number): string {
  return `${prefixDigit.repeat(8)}-${prefixDigit.repeat(4)}-4${prefixDigit.repeat(3)}-8${prefixDigit.repeat(3)}-${String(counter).padStart(12, "0")}`;
}

/**
 * The fetch-shaped lifecycle server double behind the REAL openapi-fetch
 * client: healthy commits for an accepted Bearer, a canonical 401 error
 * envelope for the stale one (the real server shape the lifecycle adapter's
 * status-only 401 mapping consumes — lifecycle-api.ts:221-224).
 */
class LifecycleServerDouble {
  readonly requestBodies: Record<string, unknown>[] = [];
  /** Bearer tokens the lifecycle endpoint actually saw, oldest first. */
  readonly presentedTokens: (string | null)[] = [];
  readonly rejectedWith401Count = { count: 0 };
  /** When set, the server rejects even freshly rotated tokens. */
  rejectAllTokens = false;
  #counter = 0;
  #sequence = 0;

  readonly fetch = async (
    input: RequestInfo | URL,
  ): Promise<Response> => {
    // openapi-fetch 0.17 builds a Request object and calls fetch(request).
    const request = input instanceof Request ? input : new Request(input);
    const body = JSON.parse((await request.text()) || "{}") as Record<string, unknown>;
    this.requestBodies.push(body);
    const bearer = request.headers.get("authorization");
    this.presentedTokens.push(bearer);
    if (this.rejectAllTokens || !acceptsToken(bearer)) {
      this.rejectedWith401Count.count += 1;
      return new Response(
        JSON.stringify({
          data: null,
          error: {
            code: "device_credential_invalid",
            message: "registered safe message",
            details: {},
            retryable: false,
          },
          request_id: "99999999-9999-4999-8999-999999999999",
          warnings: [],
        }),
        { status: 401, headers: { "content-type": "application/json" } },
      );
    }
    this.#counter += 1;
    this.#sequence += 1;
    const inner = {
      committed_at: new Date().toISOString(),
      event_id: body["event_id"],
      event_sequence: this.#sequence,
      resulting_locator: body["target_locator"] ?? null,
      source_id: body["source_id"],
      source_version_id: countedUuid("7", this.#counter),
      state: "active",
      tombstone_id: null,
    };
    return new Response(
      JSON.stringify({
        data: inner,
        error: null,
        request_id: "99999999-9999-4999-8999-999999999999",
        warnings: [],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
}

// --- the fake Vault -----------------------------------------------------------------------------

class FakeVault {
  readonly files = new Map<string, Uint8Array>();

  readonly reader = {
    readRegularFileBytes: async (normalizedPath: string): Promise<Uint8Array | null> =>
      this.files.get(normalizedPath) ?? null,
    listRegularFilePaths: async (): Promise<readonly string[]> =>
      [...this.files.keys()].sort(),
  };

  write(path: string, bytes: Uint8Array): void {
    this.files.set(path, bytes);
  }

  rename(priorPath: string, newPath: string): void {
    const bytes = this.files.get(priorPath);
    if (bytes === undefined) {
      throw new Error(`no vault file at ${priorPath}`);
    }
    this.files.delete(priorPath);
    this.files.set(newPath, bytes);
  }
}

// --- the session harness (mirrors plugin.ts #startJournalCapture) -------------------------------

interface Session {
  readonly persistence: JournalPersistence;
  readonly repository: JournalRepository;
  readonly capture: JournalCapture;
  readonly lifecycleCapture: LifecycleCaptureImpl;
  readonly queueDriver: JournalQueueDriver;
  readonly dispatcher: CoalescingQueuePassDispatcher;
  readonly coordinator: AutomaticSnapshotCoordinator;
  readonly snapshotResults: AutomaticSnapshotResult[];
  readonly passSummaries: QueuePassSummary[];
  readonly snapshotWork: Promise<void>[];
  readonly passWork: Promise<void>[];
  /** The live in-memory credential seam (`session.accessCredential`). */
  readonly credentialState: { token: string | null };
  /** The refresh-seam call counter (`session.refresh()` invocations). */
  readonly refreshCalls: { count: number };
  /** The refresh-seam controls: `shouldThrow` models a failing refresh. */
  readonly refreshControls: { shouldThrow: boolean };
  readonly vault: FakeVault;
  readonly syncServer: SyncServerDouble;
  readonly lifecycleServer: LifecycleServerDouble;
  readonly scheduledRetryPass: {
    isArmed(): boolean;
    targetEpochMs(): number | null;
  };
  awaitScheduledRetryPass(): Promise<void>;
  advanceClock(milliseconds: number): void;
  statusText(): string;
  newestEventStateOf(normalizedPath: string): string;
  eventsOf(normalizedPath: string): readonly JournalEvent[];
  requestStartup(): void;
  awaitAutomaticWork(): Promise<void>;
  unload(): Promise<void>;
}

const EPOCH_BASE_MS = 1_784_000_000_000;
let sessionCounter = 0;

async function createSession(): Promise<Session> {
  const store = new InMemoryJournalFileStore();
  const vault = new FakeVault();
  const syncServer = new SyncServerDouble();
  const lifecycleServer = new LifecycleServerDouble();
  const clock = { ms: EPOCH_BASE_MS + sessionCounter * 1_000_000 };
  const idClock = { next: sessionCounter * 10_000 };
  const createId = (): string => {
    idClock.next += 1;
    return `00000000-0000-4000-8000-${String(idClock.next).padStart(12, "0")}`;
  };
  sessionCounter += 1;

  // plugin.ts:204 `#session` — the single live credential object BOTH lanes
  // read afresh per request (plugin.ts:518 lifecycle, plugin.ts:529 content).
  const credentialState = { token: HEALTHY_TOKEN as string | null };
  // plugin.ts:533 `refreshAccessToken: () => session.refresh()`: the ONLY
  // mid-session credential rotation seam. When called it mints a fresh
  // healthy credential into the SAME session object both lanes read —
  // unless the refresh itself fails (`refreshControls.shouldThrow`).
  const refreshCalls = { count: 0 };
  const refreshControls = { shouldThrow: false };

  const persistence = new JournalPersistence({ fileStore: store, engineModule });
  await persistence.open();
  const journalDatabase: JournalRepositoryDatabase = {
    runSerializedMutation(operation) {
      return persistence.commitGeneration(operation);
    },
    readAll(sql) {
      return persistence.readAll(sql);
    },
  };
  const repository = new JournalRepository({
    database: journalDatabase,
    createId,
    nowEpochMs: () => clock.ms,
  });

  const lifecycleCapture = new LifecycleCaptureImpl({
    repository,
    lifecycle: repository.lifecycle,
    vaultReader: { readRegularFileBytes: vault.reader.readRegularFileBytes },
    createId,
    policyRevision: 1,
  });
  const capture = new JournalCapture({
    repository,
    vaultReader: vault.reader,
    policyGate: {
      evaluateForCapture: () => ({
        decision: { raw: "allowed" as const, enforced: "allowed" as const },
        revisionNumber: 2,
      }),
    },
    lifecycleCapture,
  });

  // plugin.ts:512-519: the lifecycle adapter has NO refresh path — it only
  // reads `session.accessCredential` per request.
  const lifecycleApi = createRequestUrlLifecycleApi({
    resolveBaseUrl: () => ORIGIN,
    transport: lifecycleServer.fetch,
    resolveAccessToken: () => credentialState.token,
  });
  const lifecycleDriver = new LifecycleDriverImpl({
    repository,
    lifecycle: repository.lifecycle,
    api: lifecycleApi,
    createCorrelationId: createId,
    randomJitter: () => 0,
    nowEpochMs: () => clock.ms,
  });

  let correlationClock = 0;
  const queueDriver = new JournalQueueDriver({
    repository,
    syncApi: createJournalSyncApi({
      transport: async (request: SyncHttpRequest) => {
        const bearer = request.headers["authorization"] ?? null;
        if (request.method === "PUT") {
          const operationId = request.url.split("/api/uploads/")[1]?.split("/")[0] ?? "";
          return syncServer.handleContent(
            decodeURIComponent(operationId),
            new Uint8Array(request.body as ArrayBuffer),
            bearer,
          );
        }
        return syncServer.handlePreflight(
          JSON.parse(request.body as string) as Record<string, unknown>,
          bearer,
        );
      },
      resolveOrigin: () => ORIGIN,
      getAccessToken: () => credentialState.token,
    }),
    fileBytesReader: { readRegularFileBytes: vault.reader.readRegularFileBytes },
    lifecycleDriver,
    refreshAccessToken: () => {
      refreshCalls.count += 1;
      if (refreshControls.shouldThrow) {
        return Promise.reject(new Error("session refresh failed"));
      }
      credentialState.token = REFRESHED_TOKEN;
      return Promise.resolve();
    },
    nowEpochMs: () => clock.ms,
    createCorrelationId: () => `corr-${(correlationClock += 1)}`,
    randomJitter: () => 0,
  });

  const passSummaries: QueuePassSummary[] = [];
  let lastQueuePassOutcome: QueuePassOutcome | null = null;
  const dispatcher = new CoalescingQueuePassDispatcher({
    runPass: async (): Promise<QueuePassSummary> => {
      if (persistence.isReconcileRequired) {
        queueDriver.stop();
      }
      let summary: QueuePassSummary;
      try {
        summary = await queueDriver.requestPass();
      } catch {
        summary = { outcome: "completed", processedEventCount: 0 };
      }
      if (summary.outcome !== "pass_already_running") {
        lastQueuePassOutcome = summary.outcome;
        if (summary.outcome !== "stopped") {
          armScheduledRetryPassTrigger();
        }
      }
      passSummaries.push(summary);
      return summary;
    },
  });

  // The plugin's one-shot scheduled retry trigger mirror (plugin.ts:856-886).
  const SCHEDULED_RETRY_PASS_SAFETY_MARGIN_MS = 250;
  let scheduledRetryPassTimer: ReturnType<typeof setTimeout> | null = null;
  let scheduledRetryPassTargetEpochMs: number | null = null;
  let scheduledRetryPassWork: Promise<void> = Promise.resolve();
  function armScheduledRetryPassTrigger(): void {
    let earliestRetryEpochMs: number | null = null;
    try {
      earliestRetryEpochMs = repository.readEarliestPendingRetryEpochMs();
    } catch {
      return;
    }
    if (earliestRetryEpochMs === null) {
      return;
    }
    const targetEpochMs = earliestRetryEpochMs + SCHEDULED_RETRY_PASS_SAFETY_MARGIN_MS;
    if (
      scheduledRetryPassTargetEpochMs !== null &&
      scheduledRetryPassTargetEpochMs <= targetEpochMs
    ) {
      return;
    }
    if (scheduledRetryPassTimer !== null) {
      clearTimeout(scheduledRetryPassTimer);
    }
    scheduledRetryPassTargetEpochMs = targetEpochMs;
    scheduledRetryPassTimer = setTimeout(() => {
      scheduledRetryPassTimer = null;
      scheduledRetryPassTargetEpochMs = null;
      scheduledRetryPassWork = dispatcher.request();
    }, Math.max(0, targetEpochMs - clock.ms));
  }
  function clearScheduledRetryPassTrigger(): void {
    if (scheduledRetryPassTimer !== null) {
      clearTimeout(scheduledRetryPassTimer);
      scheduledRetryPassTimer = null;
    }
    scheduledRetryPassTargetEpochMs = null;
  }

  const snapshotResults: AutomaticSnapshotResult[] = [];
  const snapshotWork: Promise<void>[] = [];
  const passWork: Promise<void>[] = [];
  const coordinator = new AutomaticSnapshotCoordinator({
    runSnapshot: (signal): Promise<AutomaticSnapshotResult> => {
      const work = (async (): Promise<AutomaticSnapshotResult> => {
        if (signal.aborted) {
          return { outcome: "skipped", queuedEventCount: 0 };
        }
        const snapshot = statusSnapshot();
        if (snapshot === null || snapshot.kind === "reconcile_required") {
          return { outcome: "stopped", queuedEventCount: 0 };
        }
        const summary = await capture.runAutomaticSnapshot({ signal });
        if (signal.aborted) {
          return { outcome: "stopped", queuedEventCount: 0 };
        }
        let queuedEventCount = summary.queuedEventCount;
        try {
          queuedEventCount = Math.max(
            summary.queuedEventCount,
            repository.countPendingEvents(),
          );
        } catch {
          // An unreadable journal keeps the scan's own count.
        }
        const result: AutomaticSnapshotResult = {
          outcome: summary.outcome === "completed" ? "completed" : "stopped",
          queuedEventCount,
        };
        snapshotResults.push(result);
        return result;
      })();
      snapshotWork.push(work.then(
        () => undefined,
        () => undefined,
      ));
      return work;
    },
    requestQueuePass: async (): Promise<void> => {
      const work = dispatcher.request();
      passWork.push(work);
      await work;
    },
  });

  function statusSnapshot() {
    return projectJournalSyncStatus({
      isReconcileRequired: persistence.isReconcileRequired,
      eventStateErrorCounts: repository.readEventStateErrorCounts(),
      lifecycleStateCounts: repository.readLifecycleStateCounts(),
      pendingLifecycleEventCount: repository.countPendingLifecycleEvents(),
      failedAttemptCount: repository.countFailedAttempts(),
      lifecycleBlockedReasonCodes: repository.readLifecycleBlockedReasonCodes() as readonly LifecycleBlockedReasonCode[],
      hasAccessCredential: credentialState.token !== null,
      isQueuePassActive: false,
      lastQueuePassOutcome,
    });
  }

  return {
    persistence,
    repository,
    capture,
    lifecycleCapture,
    queueDriver,
    dispatcher,
    coordinator,
    snapshotResults,
    passSummaries,
    snapshotWork,
    passWork,
    vault,
    syncServer,
    lifecycleServer,
    credentialState,
    refreshCalls,
    refreshControls,
    scheduledRetryPass: {
      isArmed: () => scheduledRetryPassTimer !== null,
      targetEpochMs: () => scheduledRetryPassTargetEpochMs,
    },
    awaitScheduledRetryPass: () => scheduledRetryPassWork,
    advanceClock: (milliseconds: number) => {
      clock.ms += milliseconds;
    },
    statusText: () => renderJournalSyncStatusText(statusSnapshot()),
    newestEventStateOf: (normalizedPath: string): string => {
      const events = sessionEventsOf(normalizedPath);
      return events.at(-1)?.state ?? "untracked";
    },
    eventsOf: sessionEventsOf,
    requestStartup: () => {
      coordinator.request("startup");
    },
    awaitAutomaticWork: async (): Promise<void> => {
      for (let round = 0; round < 4; round += 1) {
        await drainMicrotasks();
        const snapshots = snapshotWork.splice(0);
        if (snapshots.length > 0) {
          await Promise.all(snapshots);
        }
        await drainMicrotasks();
        const passes = passWork.splice(0);
        if (passes.length > 0) {
          await Promise.all(passes);
          continue;
        }
        if (snapshots.length === 0) {
          break;
        }
      }
      await drainMicrotasks();
    },
    unload: async (): Promise<void> => {
      clearScheduledRetryPassTrigger();
      await coordinator.stop();
      await dispatcher.stop();
      queueDriver.stop();
      lifecycleDriver.dispose();
      lifecycleCapture.dispose();
      capture.dispose();
      await capture.whenIdle();
      persistence.attemptFinalFlush();
      persistence.close();
    },
  };

  function sessionEventsOf(normalizedPath: string): readonly JournalEvent[] {
    const localFile = repository.readLocalFileByPath(normalizedPath);
    return localFile === null ? [] : repository.readEventsByLocalFileId(localFile.localFileId);
  }
}

/** Flush promise-only continuation chains (no real timers involved). */
async function drainMicrotasks(): Promise<void> {
  for (let tick = 0; tick < 30; tick += 1) {
    await Promise.resolve();
  }
}

/** The rename listener's capture half: notify, respect the settle delay, no pass. */
async function renameNoteSettledOnly(
  session: Session,
  priorPath: string,
  newPath: string,
): Promise<void> {
  session.vault.rename(priorPath, newPath);
  const slash = newPath.lastIndexOf("/");
  const parentPath = slash === -1 ? "" : newPath.slice(0, slash);
  const settled = session.capture.notifyPathRenamed(
    { path: newPath, parent: { path: parentPath } },
    priorPath,
  );
  await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS + 50);
  await settled;
}

/** The modify listener's capture half: settle + admit a (possibly new) file, no pass yet. */
async function captureWrite(session: Session, path: string, bytes: Uint8Array): Promise<void> {
  session.vault.write(path, bytes);
  const settled = session.capture.notifyPathChanged(path);
  await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS + 50);
  await settled;
}

async function createConvergedFixture(): Promise<Session> {
  const session = await createSession();
  const encoder = new TextEncoder();
  session.vault.write("notes/alpha.md", encoder.encode("alpha content"));
  session.vault.write("notes/beta.md", encoder.encode("beta content"));
  session.vault.write("notes/gamma.md", encoder.encode("gamma content"));
  session.requestStartup();
  await session.awaitAutomaticWork();
  return session;
}

// --- the scenarios ------------------------------------------------------------------------------

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("lifecycle lane credential refresh through the queue pass (fix round 4)", () => {
  it("heals a stale credential through the queue's own refresh when a pending rename stalls the pass", async () => {
    const session = await createConvergedFixture();
    const encoder = new TextEncoder();

    // A pending rename (its pass trigger was lost — the live journal
    // shape), then the credential goes stale (present, server-rejected
    // 401), then the user creates a brand-new note.
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    session.credentialState.token = STALE_TOKEN;
    const renameEvent = session
      .eventsOf("notes/alpha-renamed.md")
      .find((event) => event.operation === "rename");
    expect(renameEvent?.state).toBe("queued");
    await captureWrite(session, "notes/delta.md", encoder.encode("delta content"));
    const createEvent = session
      .eventsOf("notes/delta.md")
      .find((event) => event.operation === "create");
    expect(createEvent?.state).toBe("queued");

    // The modify listener's own pass trigger fires the bounded pass.
    await session.dispatcher.request();

    // THE FIX: the lifecycle lane's login_required verdict consumed the
    // pass's ONE shared refresh budget and rotated the credential — the
    // refresh seam FIRED (before the fix: never called; the pass died
    // login_required before the content lane ever ran).
    expect(session.refreshCalls.count).toBe(1);
    expect(session.lifecycleServer.rejectedWith401Count.count).toBe(1);
    expect(session.lifecycleServer.presentedTokens).toEqual([`Bearer ${STALE_TOKEN}`]);
    // The retry dispatch found no eligible lifecycle row (the parked
    // rename waits out its own bounded backoff), so the drain continued
    // to the content lane UNDER THE ROTATED CREDENTIAL: the create
    // committed in the SAME pass — no starvation.
    expect(session.passSummaries.at(-1)).toEqual({
      outcome: "completed",
      processedEventCount: 1,
    });
    expect(session.syncServer.presentedTokens.at(-1)).toBe(`Bearer ${REFRESHED_TOKEN}`);
    expect(session.syncServer.rejectedWith401Count.count).toBe(0);
    expect(session.repository.readEvent(createEvent?.eventId ?? "")?.state).toBe("committed");
    const parkedRename = session.repository.readEvent(renameEvent?.eventId ?? "");
    expect(parkedRename?.state).toBe("waiting_retry");
    expect(parkedRename?.safeError).toBe("login_required");
    expect(session.statusText()).toBe("Offline — queued (1)");

    // The parked rename converges on the next scheduled-trigger firing
    // (round 3's arming) under the healed credential — the queue fully
    // recovers with NO external credential rotation.
    expect(session.scheduledRetryPass.isArmed()).toBe(true);
    session.advanceClock(2_000);
    await vi.advanceTimersByTimeAsync(2_000);
    await session.awaitScheduledRetryPass();
    expect(session.lifecycleServer.presentedTokens).toEqual([
      `Bearer ${STALE_TOKEN}`,
      `Bearer ${REFRESHED_TOKEN}`,
    ]);
    expect(session.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(session.newestEventStateOf("notes/delta.md")).toBe("committed");
    expect(session.repository.countPendingEvents()).toBe(0);
    expect(session.statusText()).toBe("Ready");
    // Exactly one refresh across the whole episode — spec 8's budget.
    expect(session.refreshCalls.count).toBe(1);
  });

  it("parks and ends the pass login_required when the rotated credential is rejected again", async () => {
    const session = await createConvergedFixture();
    const encoder = new TextEncoder();
    const preflightsBefore = session.syncServer.preflightBodies.length;

    // Two pending renames: the first parks on the stale token; the retry
    // dispatch (after the refresh) reaches the second one — and the
    // server rejects the FRESH token too.
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNoteSettledOnly(session, "notes/beta.md", "notes/beta-renamed.md");
    session.credentialState.token = STALE_TOKEN;
    session.lifecycleServer.rejectAllTokens = true;
    await captureWrite(session, "notes/delta.md", encoder.encode("delta content"));
    const createEvent = session
      .eventsOf("notes/delta.md")
      .find((event) => event.operation === "create");
    expect(createEvent?.state).toBe("queued");
    const alphaRename = session
      .eventsOf("notes/alpha-renamed.md")
      .find((event) => event.operation === "rename");
    const betaRename = session
      .eventsOf("notes/beta-renamed.md")
      .find((event) => event.operation === "rename");

    await session.dispatcher.request();

    // The refresh rotated the credential, the retry dispatched the next
    // eligible rename, the server rejected the fresh token: the SECOND
    // login verdict parks and ends the pass (second-401 discipline) with
    // NO second refresh attempt.
    expect(session.passSummaries.at(-1)).toEqual({
      outcome: "login_required",
      processedEventCount: 0,
    });
    expect(session.refreshCalls.count).toBe(1);
    expect(session.lifecycleServer.presentedTokens).toEqual([
      `Bearer ${STALE_TOKEN}`,
      `Bearer ${REFRESHED_TOKEN}`,
    ]);
    expect(session.lifecycleServer.rejectedWith401Count.count).toBe(2);
    for (const renameEvent of [alphaRename, betaRename]) {
      const stored = session.repository.readEvent(renameEvent?.eventId ?? "");
      expect(stored?.state).toBe("waiting_retry");
      expect(stored?.safeError).toBe("login_required");
    }
    // The content lane never dispatched: the create stays queued.
    expect(session.repository.readEvent(createEvent?.eventId ?? "")?.state).toBe("queued");
    expect(session.syncServer.preflightBodies).toHaveLength(preflightsBefore);
    expect(session.scheduledRetryPass.isArmed()).toBe(true);
  });

  it("ends login_required without content dispatch when the refresh itself fails", async () => {
    const session = await createConvergedFixture();
    const encoder = new TextEncoder();
    const preflightsBefore = session.syncServer.preflightBodies.length;

    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    session.credentialState.token = STALE_TOKEN;
    await captureWrite(session, "notes/delta.md", encoder.encode("delta content"));
    const createEvent = session
      .eventsOf("notes/delta.md")
      .find((event) => event.operation === "create");
    const renameEvent = session
      .eventsOf("notes/alpha-renamed.md")
      .find((event) => event.operation === "rename");
    // The session refresh itself fails (revoked family, offline auth
    // endpoint): the budget is consumed, the pass ends login_required.
    session.refreshControls.shouldThrow = true;

    await session.dispatcher.request();

    expect(session.passSummaries.at(-1)).toEqual({
      outcome: "login_required",
      processedEventCount: 0,
    });
    expect(session.refreshCalls.count).toBe(1);
    expect(session.lifecycleServer.rejectedWith401Count.count).toBe(1);
    const parkedRename = session.repository.readEvent(renameEvent?.eventId ?? "");
    expect(parkedRename?.state).toBe("waiting_retry");
    expect(parkedRename?.safeError).toBe("login_required");
    expect(session.repository.readEvent(createEvent?.eventId ?? "")?.state).toBe("queued");
    expect(session.syncServer.preflightBodies).toHaveLength(preflightsBefore);
    expect(session.scheduledRetryPass.isArmed()).toBe(true);
  });

  it("control: the SAME stale token self-heals in one pass when no pending lifecycle event exists", async () => {
    const session = await createConvergedFixture();
    const encoder = new TextEncoder();
    const preflightsBefore = session.syncServer.preflightBodies.length;

    // No pending lifecycle event. The credential goes stale, then the user
    // creates a new note.
    session.credentialState.token = STALE_TOKEN;
    await captureWrite(session, "notes/delta.md", encoder.encode("delta content"));

    await session.dispatcher.request();

    // The content lane's preflight hit the server 401 (access_expired),
    // the one-per-pass refresh seam fired, the SAME request retried under
    // the rotated credential and committed — everything syncs in ONE
    // pass. This control pins that the content lane's discipline is
    // unchanged by the lifecycle-lane fix.
    expect(session.passSummaries.at(-1)).toEqual({
      outcome: "completed",
      processedEventCount: 1,
    });
    expect(session.refreshCalls.count).toBe(1);
    expect(session.syncServer.rejectedWith401Count.count).toBe(1);
    expect(session.syncServer.preflightBodies).toHaveLength(preflightsBefore + 2);
    expect(session.syncServer.presentedTokens.slice(-2)).toEqual([
      `Bearer ${STALE_TOKEN}`,
      `Bearer ${REFRESHED_TOKEN}`,
    ]);
    expect(session.newestEventStateOf("notes/delta.md")).toBe("committed");
    expect(session.repository.countPendingEvents()).toBe(0);
    expect(session.statusText()).toBe("Ready");
  });
});
