/**
 * Committed regression suite: automatic vault convergence after rename + edit.
 *
 * The live defect under test: a converged vault (all notes committed,
 * `Ready (0)`) renamed notes and edited one note inside the open Vault; the
 * status bar settled at `Ready (n)` and nothing ever synced, and a full
 * Obsidian restart did not fix it. `Ready` (not `Offline — queued`) means
 * pending journal events sat in `queued`/`preflight`/`uploading` (or a
 * non-waiting `waiting_retry` label) with no queue pass ever processing
 * them.
 *
 * Every scenario drives the REAL plugin wiring over the REAL durable stack —
 * sql.js engine, verified-generation persistence, the real JournalRepository
 * + LifecycleRepository over one serialized writer, the real JournalCapture +
 * LifecycleCaptureImpl settle/admission paths, the real JournalQueueDriver
 * with the lifecycle lane, the real CoalescingQueuePassDispatcher and
 * AutomaticSnapshotCoordinator, the real hand-mirrored sync client and the
 * real openapi-fetch lifecycle adapter — against in-process server doubles
 * at the raw transport boundaries with HEALTHY responses (per-call failure
 * injection where a scenario needs it) and fake settle timers. The harness
 * mirrors `plugin.ts#startJournalCapture` exactly; when the plugin wiring
 * changes, the harness changes with it.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";

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

// --- the healthy in-process server doubles -----------------------------------------------------

const ORIGIN = "https://sync.example.org";
const ACCESS_TOKEN = "at1.convergence-suite-access";

/** Render one counter-derived canonical UUID text form. */
function countedUuid(prefixDigit: string, counter: number): string {
  return `${prefixDigit.repeat(8)}-${prefixDigit.repeat(4)}-4${prefixDigit.repeat(3)}-8${prefixDigit.repeat(3)}-${String(counter).padStart(12, "0")}`;
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
 * (mirrors the journey suite's double): identity-keyed preflight, frozen
 * terminal replay, server-side digest verification, exactly-once
 * publication and a current-version registry for update bases. Healthy by
 * default.
 */
class SyncServerDouble {
  readonly identityTerminals = new Map<string, FrozenTerminal>();
  readonly currentVersions = new Map<string, string>();
  readonly preflightBodies: Record<string, unknown>[] = [];
  readonly receivedDigests: string[] = [];
  /** Injectable per-call preflight failure script, mirroring the lifecycle double. */
  nextPreflightResponse: ((callIndex: number) => LifecycleScriptedResponse | null) | null = null;
  publications = 0;
  #counter = 0;

  async handlePreflight(
    body: Record<string, unknown>,
  ): Promise<{ status: number; bodyText: string }> {
    this.preflightBodies.push(body);
    const scripted = this.nextPreflightResponse?.(this.preflightBodies.length - 1) ?? null;
    if (scripted !== null && scripted.status !== 200) {
      return { status: scripted.status, bodyText: syncErrorBody(scripted.code) };
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
  ): Promise<{ status: number; bodyText: string }> {
    const operation = this.operations.get(operationId);
    if (operation === undefined) {
      return { status: 404, bodyText: syncErrorBody("small_file_operation_not_found") };
    }
    if (operation.terminal !== null) {
      return { status: 200, bodyText: syncSuccessBody(this.#resultBody(operation.terminal)) };
    }
    const digest = await sha256Hex(bytes);
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

/** One scripted lifecycle verdict: a status plus a closed error code. */
interface LifecycleScriptedResponse {
  readonly status: number;
  readonly code: string;
}

/**
 * The fetch-shaped lifecycle server double behind the REAL openapi-fetch
 * client: healthy commits by default, with an injectable per-call script
 * for failure scenarios. The server-side memory (request history) survives
 * plugin restarts exactly like the real backend.
 */
class LifecycleServerDouble {
  readonly requestBodies: Record<string, unknown>[] = [];
  nextResponse: ((callIndex: number) => LifecycleScriptedResponse | null) | null = null;
  #counter = 0;
  #sequence = 0;

  readonly fetch = async (
    input: RequestInfo | URL,
  ): Promise<Response> => {
    // openapi-fetch 0.17 builds a Request object and calls fetch(request).
    const request = input instanceof Request ? input : new Request(input);
    const body = JSON.parse((await request.text()) || "{}") as Record<string, unknown>;
    this.requestBodies.push(body);
    const scripted = this.nextResponse?.(this.requestBodies.length - 1) ?? null;
    if (scripted !== null && scripted.status !== 200) {
      return new Response(
        JSON.stringify({
          data: null,
          error: {
            code: scripted.code,
            message: "registered safe message",
            details: {},
            retryable: false,
          },
          request_id: "99999999-9999-4999-8999-999999999999",
          warnings: [],
        }),
        { status: scripted.status, headers: { "content-type": "application/json" } },
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

/** The in-memory Vault: the plugin's read-only vault reader over live bytes. */
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
  readonly vault: FakeVault;
  readonly syncServer: SyncServerDouble;
  readonly lifecycleServer: LifecycleServerDouble;
  /**
   * The plugin's one-shot scheduled retry trigger mirror (fix round 2 D4):
   * whether the timer is armed and for which deadline.
   */
  readonly scheduledRetryPass: {
    isArmed(): boolean;
    targetEpochMs(): number | null;
  };
  /** Resolve once the armed trigger fired and its bounded pass settled. */
  awaitScheduledRetryPass(): Promise<void>;
  advanceClock(milliseconds: number): void;
  /** The rendered status-bar text under the plugin's exact projection. */
  statusText(): string;
  /** The newest journal-event state of one path (per-file current status). */
  newestEventStateOf(normalizedPath: string): string;
  eventsOf(normalizedPath: string): readonly JournalEvent[];
  requestStartup(): void;
  awaitAutomaticWork(): Promise<void>;
  unload(): Promise<void>;
}

interface SessionBindings {
  readonly store: InMemoryJournalFileStore;
  readonly vault: FakeVault;
  readonly syncServer: SyncServerDouble;
  readonly lifecycleServer: LifecycleServerDouble;
  /** Fresh instances per session; the underlying journal persists. */
  readonly isRestart: boolean;
  /**
   * The in-memory access credential the session starts with. null models
   * the real restart race: the fire-and-forget token refresh has not yet
   * minted an access credential when the startup snapshot runs.
   */
  readonly initialAccessToken?: string | null;
}

const EPOCH_BASE_MS = 1_784_000_000_000;
let sessionCounter = 0;

async function createSession(bindings: SessionBindings): Promise<Session> {
  const { store, vault, syncServer, lifecycleServer } = bindings;
  const clock = { ms: EPOCH_BASE_MS + sessionCounter * 1_000_000 };
  const idClock = { next: sessionCounter * 10_000 };
  const createId = (): string => {
    idClock.next += 1;
    return `00000000-0000-4000-8000-${String(idClock.next).padStart(12, "0")}`;
  };

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

  const lifecycleApi = createRequestUrlLifecycleApi({
    baseUrl: ORIGIN,
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
        if (request.method === "PUT") {
          const operationId = request.url.split("/api/uploads/")[1]?.split("/")[0] ?? "";
          return syncServer.handleContent(
            decodeURIComponent(operationId),
            new Uint8Array(request.body as ArrayBuffer),
          );
        }
        return syncServer.handlePreflight(JSON.parse(request.body as string) as Record<string, unknown>);
      },
      resolveOrigin: () => ORIGIN,
      getAccessToken: () => credentialState.token,
    }),
    fileBytesReader: { readRegularFileBytes: vault.reader.readRegularFileBytes },
    lifecycleDriver,
    refreshAccessToken: () => Promise.resolve(),
    nowEpochMs: () => clock.ms,
    createCorrelationId: () => `corr-${(correlationClock += 1)}`,
    randomJitter: () => 0,
  });

  const passSummaries: QueuePassSummary[] = [];
  let lastQueuePassOutcome: QueuePassOutcome | null = null;
  const credentialState = {
    token: bindings.initialAccessToken === undefined ? ACCESS_TOKEN : bindings.initialAccessToken,
  };
  const dispatcher = new CoalescingQueuePassDispatcher({
    runPass: async (): Promise<QueuePassSummary> => {
      let summary: QueuePassSummary;
      try {
        summary = await queueDriver.requestPass();
      } catch {
        summary = { outcome: "completed", processedEventCount: 0 };
      }
      if (summary.outcome !== "pass_already_running") {
        lastQueuePassOutcome = summary.outcome;
        // Fix round 3 mirror: arm after EVERY pass that actually ran —
        // a completed pass can still leave parked retry work behind
        // (lifecycle-lane retryable failure). The armer no-ops when no
        // pending row carries a retry deadline.
        armScheduledRetryPassTrigger();
      }
      passSummaries.push(summary);
      return summary;
    },
  });

  // The plugin's one-shot scheduled retry trigger mirror (fix round 2 D4,
  // widened in fix round 3): every pass that actually ran arms ONE timer
  // at the earliest pending retry deadline plus a small safety margin; the
  // timer's single firing requests one bounded dispatcher pass. The armer
  // no-ops when no pending row carries a retry deadline. At most one timer
  // is outstanding; unload cancels it.
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
        // The plugin wrapper's pending-count fallback (fix round 2 D2
        // mirror): pre-existing pending rows (lifecycle included) still
        // owe a pass even when the scan's own admission count is zero.
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

  sessionCounter += 1;

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

/**
 * The rename listener's capture half: notify, respect the settle delay, no
 * pass. Scenarios use this to model a rename whose pass trigger was lost
 * (for example a raced unload) while keeping the durable event recorded.
 */
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

/**
 * The rename listener sequence (fix round 2 D1 mirror): the settled capture
 * is followed by exactly one bounded dispatcher pass, fire-and-forget.
 */
async function renameNote(session: Session, priorPath: string, newPath: string): Promise<void> {
  await renameNoteSettledOnly(session, priorPath, newPath);
  await session.dispatcher.request();
}

/** The modify listener's capture half: settle + admit, no pass yet. */
async function captureEdit(session: Session, path: string, bytes: Uint8Array): Promise<void> {
  session.vault.write(path, bytes);
  const settled = session.capture.notifyPathChanged(path);
  await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS + 50);
  await settled;
}

/** The modify listener's pass half: exactly one bounded pass after admission. */
async function triggerModifyPass(session: Session): Promise<void> {
  await session.dispatcher.request();
}

async function createConvergedFixture(): Promise<{
  bindings: SessionBindings;
  session: Session;
}> {
  const store = new InMemoryJournalFileStore();
  const vault = new FakeVault();
  const encoder = new TextEncoder();
  vault.write("notes/alpha.md", encoder.encode("alpha content"));
  vault.write("notes/beta.md", encoder.encode("beta content"));
  vault.write("notes/gamma.md", encoder.encode("gamma content"));
  const bindings: SessionBindings = {
    store,
    vault,
    syncServer: new SyncServerDouble(),
    lifecycleServer: new LifecycleServerDouble(),
    isRestart: false,
  };
  const session = await createSession(bindings);
  session.requestStartup();
  await session.awaitAutomaticWork();
  return { bindings, session };
}

// --- the scenarios ------------------------------------------------------------------------------

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("automatic vault convergence after rename + edit", () => {
  it("converges three notes through the startup snapshot and one dispatcher pass", async () => {
    const { session } = await createConvergedFixture();

    expect(session.snapshotResults).toEqual([
      { outcome: "completed", queuedEventCount: 3 },
    ]);
    expect(session.passSummaries).toEqual([
      { outcome: "completed", processedEventCount: 3 },
    ]);
    expect(session.syncServer.publications).toBe(3);
    for (const path of ["notes/alpha.md", "notes/beta.md", "notes/gamma.md"]) {
      expect(session.repository.readLocalFileByPath(path)?.sourceId).not.toBeNull();
      expect(session.newestEventStateOf(path)).toBe("committed");
    }
    expect(session.repository.countPendingEvents()).toBe(0);
    expect(session.repository.countPendingLifecycleEvents()).toBe(0);
    expect(session.statusText()).toBe("Ready");
  });

  it("drains renames through the rename listener's own queue pass trigger", async () => {
    const { session } = await createConvergedFixture();
    const passesBefore = session.passSummaries.length;

    // The full rename listener sequence: settled capture + its own bounded
    // pass. No edit and no manual trigger is involved.
    await renameNote(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNote(session, "notes/beta.md", "notes/beta-renamed.md");

    // Each settled rename recording is followed by a pass that drains the
    // lifecycle lane: the durable rename events never sit queued.
    expect(session.passSummaries.length).toBeGreaterThan(passesBefore);
    expect(session.lifecycleServer.requestBodies).toHaveLength(2);
    expect(session.lifecycleServer.requestBodies.map((body) => body["operation"])).toEqual([
      "rename",
      "rename",
    ]);
    expect(session.repository.readLocalFileByPath("notes/alpha.md")).toBeNull();
    expect(session.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(session.newestEventStateOf("notes/beta-renamed.md")).toBe("committed");
    expect(session.repository.countPendingEvents()).toBe(0);
    expect(session.repository.countPendingLifecycleEvents()).toBe(0);
    expect(session.statusText()).toBe("Ready");
  });

  it("drains the queued renames and commits the edit when a healthy pass follows the edit", async () => {
    const { session } = await createConvergedFixture();
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNoteSettledOnly(session, "notes/beta.md", "notes/beta-renamed.md");

    // The modify listener sequence: capture the edit, then request one pass.
    await captureEdit(
      session,
      "notes/gamma.md",
      new TextEncoder().encode("gamma content edited"),
    );
    await triggerModifyPass(session);

    // With a healthy lifecycle endpoint the pass's lifecycle lane drains
    // both queued renames first, then the content lane commits the edit.
    expect(session.passSummaries.at(-1)).toEqual({
      outcome: "completed",
      processedEventCount: 1,
    });
    expect(session.lifecycleServer.requestBodies).toHaveLength(2);
    expect(session.lifecycleServer.requestBodies.map((body) => body["operation"])).toEqual([
      "rename",
      "rename",
    ]);
    expect(session.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(session.newestEventStateOf("notes/beta-renamed.md")).toBe("committed");
    expect(session.newestEventStateOf("notes/gamma.md")).toBe("committed");
    expect(session.repository.countPendingEvents()).toBe(0);
    expect(session.syncServer.publications).toBe(4);
    expect(session.statusText()).toBe("Ready");
  });

  it("drains lifecycle-only pending work after restart (the startup snapshot counts it)", async () => {
    const { bindings, session } = await createConvergedFixture();
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNoteSettledOnly(session, "notes/beta.md", "notes/beta-renamed.md");
    expect(session.repository.countPendingEvents()).toBe(2);

    // Full Obsidian restart: unload, then a fresh composition over the SAME
    // durable journal, same vault, same server memories.
    await session.unload();
    const restarted = await createSession({ ...bindings, isRestart: true });
    restarted.requestStartup();
    await restarted.awaitAutomaticWork();

    // The scan itself records nothing (renames change no bytes, every
    // fingerprint still matches its last-committed triple), but the
    // snapshot wrapper surfaces the two pre-existing pending lifecycle
    // rows through the repository's pending count …
    expect(restarted.snapshotResults).toEqual([
      { outcome: "completed", queuedEventCount: 2 },
    ]);
    // … so the coordinator requests the one pass that drains the
    // lifecycle lane: the restart never strands queued renames.
    expect(restarted.passSummaries.length).toBe(1);
    expect(restarted.lifecycleServer.requestBodies).toHaveLength(2);
    expect(restarted.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(restarted.newestEventStateOf("notes/beta-renamed.md")).toBe("committed");
    expect(restarted.repository.countPendingEvents()).toBe(0);
    expect(restarted.repository.countPendingLifecycleEvents()).toBe(0);
    expect(restarted.statusText()).toBe("Ready");
  });

  it("drains after restart when a pending content edit also exists (the snapshot coalesces it)", async () => {
    const { bindings, session } = await createConvergedFixture();
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNoteSettledOnly(session, "notes/beta.md", "notes/beta-renamed.md");
    // The live "edit never synced" state: the edit event exists but its
    // pass trigger was lost (raced unload), so it sits queued alongside the
    // two queued renames — three pending journal events.
    await captureEdit(
      session,
      "notes/gamma.md",
      new TextEncoder().encode("gamma content edited"),
    );
    expect(session.repository.countPendingEvents()).toBe(3);
    expect(session.statusText()).toBe("Ready (3)");

    await session.unload();
    const restarted = await createSession({ ...bindings, isRestart: true });
    restarted.requestStartup();
    await restarted.awaitAutomaticWork();

    // The snapshot re-admits gamma, its bytes diverge from the committed
    // fingerprint, the queued edit coalesces — and the wrapper surfaces
    // every pre-existing pending row (the two queued renames included),
    // so the reported count covers all three and requests the one pass
    // that drains everything.
    expect(restarted.snapshotResults).toEqual([
      { outcome: "completed", queuedEventCount: 3 },
    ]);
    expect(restarted.passSummaries.at(-1)).toEqual({
      outcome: "completed",
      processedEventCount: 1,
    });
    expect(restarted.lifecycleServer.requestBodies).toHaveLength(2);
    expect(restarted.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(restarted.newestEventStateOf("notes/beta-renamed.md")).toBe("committed");
    expect(restarted.newestEventStateOf("notes/gamma.md")).toBe("committed");
    expect(restarted.repository.countPendingEvents()).toBe(0);
    expect(restarted.statusText()).toBe("Ready");
  });

  it("keeps a queued rename in the lifecycle lane after one retryable lifecycle failure (no lane crossing)", async () => {
    const { session } = await createConvergedFixture();
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNoteSettledOnly(session, "notes/beta.md", "notes/beta-renamed.md");
    const alphaRename = session.eventsOf("notes/alpha-renamed.md").at(-1);
    const betaRename = session.eventsOf("notes/beta-renamed.md").at(-1);
    expect(alphaRename?.operation).toBe("rename");
    expect(betaRename?.operation).toBe("rename");

    // ONE transient 5xx on the FIRST lifecycle commit only.
    session.lifecycleServer.nextResponse = (callIndex) =>
      callIndex === 0 ? { status: 500, code: "internal_error" } : null;

    await captureEdit(
      session,
      "notes/gamma.md",
      new TextEncoder().encode("gamma content edited"),
    );
    await triggerModifyPass(session);

    // The lifecycle lane retry-schedules the first rename (bounded backoff)
    // and exits; the content lane MUST select the next CONTENT event — the
    // queued second rename belongs to the lifecycle lane and must never
    // reach the content preflight with the lifecycle zeros fingerprint.
    const crossedPreflight = session.syncServer.preflightBodies.find(
      (body) => body["event_id"] === betaRename?.eventId,
    );
    expect(crossedPreflight).toBeUndefined();
    expect(session.repository.readEvent(betaRename?.eventId ?? "")?.state).not.toBe(
      "integrity_failed",
    );
    // The same pass's next iteration drains the second rename through the
    // LIFECYCLE lane (still eligible: it never failed).
    expect(session.lifecycleServer.requestBodies).toHaveLength(2);
    expect(session.lifecycleServer.requestBodies.map((body) => body["operation"])).toEqual([
      "rename",
      "rename",
    ]);
    expect(session.repository.readEvent(betaRename?.eventId ?? "")?.state).toBe("committed");
    // The first rename sits in bounded network backoff with no follow-up
    // pass trigger inside this pass (retry exits never auto-continue) …
    expect(session.repository.readEvent(alphaRename?.eventId ?? "")?.state).toBe(
      "waiting_retry",
    );
    // … while the edit itself did commit.
    expect(session.newestEventStateOf("notes/gamma.md")).toBe("committed");
    expect(session.repository.countPendingEvents()).toBe(1);
    expect(session.statusText()).toBe("Offline — queued (1)");
  });

  it("drains a parked content retry through the one-shot scheduled retry trigger", async () => {
    const { session } = await createConvergedFixture();
    // ONE transient 5xx on the edit's content preflight only.
    const preflightCallsBefore = session.syncServer.preflightBodies.length;
    session.syncServer.nextPreflightResponse = (callIndex) =>
      callIndex === preflightCallsBefore ? { status: 500, code: "internal_error" } : null;
    await captureEdit(
      session,
      "notes/gamma.md",
      new TextEncoder().encode("gamma content edited"),
    );
    await triggerModifyPass(session);

    // The pass ends retry_scheduled with the edit parked in bounded
    // backoff (the no-overtake discipline of fix round 1 stays) …
    const editEvent = session
      .eventsOf("notes/gamma.md")
      .find((event) => event.operation === "update");
    expect(editEvent?.state).toBe("waiting_retry");
    expect(editEvent?.safeError).toBe("server_error");
    expect(session.passSummaries.at(-1)?.outcome).toBe("retry_scheduled");
    // … which armed the plugin-level one-shot scheduled trigger at the
    // parked event's retry deadline plus the safety margin (fix round 2
    // D4): exactly one outstanding timer, never a repeating loop.
    expect(session.scheduledRetryPass.isArmed()).toBe(true);
    expect(session.scheduledRetryPass.targetEpochMs()).toBe(
      (editEvent?.nextEligibleRetryEpochMs ?? 0) + 250,
    );
    // Once the deadline passes the single timer fires ONE bounded pass
    // that commits the parked edit — no manual command, no daemon.
    session.advanceClock(2_000);
    await vi.advanceTimersByTimeAsync(2_000);
    await session.awaitScheduledRetryPass();
    expect(session.newestEventStateOf("notes/gamma.md")).toBe("committed");
    expect(session.repository.countPendingEvents()).toBe(0);
    expect(session.scheduledRetryPass.isArmed()).toBe(false);
    expect(session.statusText()).toBe("Ready");
  });

  it("re-admits a frozen edit after its rename commits (durable deferral release)", async () => {
    const { session } = await createConvergedFixture();
    const encoder = new TextEncoder();
    const publicationsBefore = session.syncServer.publications;

    // The user saves note A (its queued update event exists), then renames
    // the SAME note: the rename transaction freezes the still-pending
    // content event as terminal deferred_lifecycle.
    await captureEdit(session, "notes/alpha.md", encoder.encode("alpha content v2"));
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await triggerModifyPass(session);

    // The rename drained; its SERVER-SIDE COMMIT released the durable
    // deferral marker in the same transaction (fix round 2 D7): the frozen
    // edit row is gone and the capture guard no longer refuses the path.
    expect(
      session.eventsOf("notes/alpha-renamed.md").find((event) => event.operation === "update"),
    ).toBeUndefined();
    expect(session.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(session.lifecycleServer.requestBodies).toHaveLength(1);
    expect(session.syncServer.publications).toBe(publicationsBefore);

    // The user edits the renamed note again: the modify surface admits the
    // path and the edit syncs — the note can never fall out of content
    // sync just because a rename once froze it.
    await captureEdit(session, "notes/alpha-renamed.md", encoder.encode("alpha content v3"));
    await triggerModifyPass(session);
    expect(session.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(session.syncServer.publications).toBe(publicationsBefore + 1);
    expect(session.repository.countPendingEvents()).toBe(0);
    expect(session.statusText()).toBe("Ready");
  });

  it("re-admits a frozen edit through the restart snapshot after its rename commits", async () => {
    const { bindings, session } = await createConvergedFixture();
    const encoder = new TextEncoder();
    const publicationsBefore = session.syncServer.publications;

    // The frozen edit's bytes (v2) are still on disk when the rename
    // commits and releases the deferral marker — no further edit happens.
    await captureEdit(session, "notes/alpha.md", encoder.encode("alpha content v2"));
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await triggerModifyPass(session);
    expect(session.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");

    // A full restart cannot strand the diverging bytes either: the release
    // is durable, so the startup snapshot re-admits the path (v2 bytes
    // diverge from the last-committed v1 fingerprint) and its pass syncs.
    await session.unload();
    const restarted = await createSession({ ...bindings, isRestart: true });
    restarted.requestStartup();
    await restarted.awaitAutomaticWork();
    expect(restarted.snapshotResults).toEqual([
      { outcome: "completed", queuedEventCount: 1 },
    ]);
    expect(restarted.passSummaries.length).toBe(1);
    expect(restarted.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(restarted.syncServer.publications).toBe(publicationsBefore + 1);
    expect(restarted.repository.countPendingEvents()).toBe(0);
    expect(restarted.statusText()).toBe("Ready");
  });

  it("arms the one-shot retry trigger after a completed pass that parked a lifecycle retry", async () => {
    const { session } = await createConvergedFixture();
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    // ONE transient 5xx on the rename's lifecycle commit; the content lane
    // has nothing queued, so the pass ends `completed` — the stranded
    // shape fix round 3 closes.
    session.lifecycleServer.nextResponse = () => ({ status: 500, code: "internal_error" });
    await session.dispatcher.request();

    const alphaRename = session.eventsOf("notes/alpha-renamed.md").at(-1);
    expect(alphaRename?.state).toBe("waiting_retry");
    expect(alphaRename?.safeError).toBe("server_error");
    expect(session.passSummaries.at(-1)?.outcome).toBe("completed");
    // The COMPLETED pass still armed the one-shot scheduled trigger at the
    // parked retry's deadline plus the safety margin: stranded parked work
    // recovers by itself, no unrelated trigger needed.
    expect(session.scheduledRetryPass.isArmed()).toBe(true);
    expect(session.scheduledRetryPass.targetEpochMs()).toBe(
      (alphaRename?.nextEligibleRetryEpochMs ?? 0) + 250,
    );
    // The server is healthy again: the timer's single firing drains the
    // parked retry and the queue converges.
    session.lifecycleServer.nextResponse = null;
    session.advanceClock(2_000);
    await vi.advanceTimersByTimeAsync(2_000);
    await session.awaitScheduledRetryPass();
    expect(session.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(session.repository.countPendingEvents()).toBe(0);
    expect(session.scheduledRetryPass.isArmed()).toBe(false);
    expect(session.statusText()).toBe("Ready");
  });

  it("keeps the deferral marker when a rename terminally fails server-side (fail-closed)", async () => {
    const { bindings, session } = await createConvergedFixture();
    const encoder = new TextEncoder();
    const publicationsBefore = session.syncServer.publications;

    // The pending edit exists when the rename freezes it …
    await captureEdit(session, "notes/alpha.md", encoder.encode("alpha content v2"));
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    // … and the server-side verdict is a TERMINAL conflict (409): the
    // rename closes blocked_conflict. The D7 release runs only on a
    // committed rename receipt — a terminally-failed rename KEEPS the
    // deferral marker, and repair stays owned by child 6.
    session.lifecycleServer.nextResponse = () => ({
      status: 409,
      code: "source_version_conflict",
    });
    await triggerModifyPass(session);
    const renameEvent = session
      .eventsOf("notes/alpha-renamed.md")
      .find((event) => event.operation === "rename");
    expect(renameEvent?.state).toBe("blocked_conflict");
    const deferred = session
      .eventsOf("notes/alpha-renamed.md")
      .find((event) => event.operation === "update");
    expect(deferred?.state).toBe("deferred_lifecycle");

    // The capture surface still refuses the path: no new event can exist …
    const eventsBefore = session.eventsOf("notes/alpha-renamed.md").length;
    await captureEdit(session, "notes/alpha-renamed.md", encoder.encode("alpha content v3"));
    await triggerModifyPass(session);
    expect(session.eventsOf("notes/alpha-renamed.md")).toHaveLength(eventsBefore);
    expect(session.syncServer.publications).toBe(publicationsBefore);

    // … and a full restart skips the path too (the snapshot refuses it,
    // no pending rows exist, no pass runs) — the fail-closed guidance is
    // preserved until child 6 repairs the journal.
    await session.unload();
    const restarted = await createSession({ ...bindings, isRestart: true });
    restarted.requestStartup();
    await restarted.awaitAutomaticWork();
    expect(restarted.snapshotResults).toEqual([
      { outcome: "completed", queuedEventCount: 0 },
    ]);
    expect(restarted.passSummaries.length).toBe(0);
    expect(
      restarted
        .eventsOf("notes/alpha-renamed.md")
        .find((event) => event.operation === "update")?.state,
    ).toBe("deferred_lifecycle");
    expect(restarted.syncServer.publications).toBe(publicationsBefore);
  });

  it("parks queued renames retryable when the restart pass races the credential (no terminal kill)", async () => {
    const { bindings, session } = await createConvergedFixture();
    await renameNoteSettledOnly(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNoteSettledOnly(session, "notes/beta.md", "notes/beta-renamed.md");
    await captureEdit(
      session,
      "notes/gamma.md",
      new TextEncoder().encode("gamma content edited"),
    );
    expect(session.repository.countPendingEvents()).toBe(3);

    // Restart BEFORE the fire-and-forget token refresh minted an access
    // credential: the startup snapshot's pass runs with a null credential.
    await session.unload();
    const restarted = await createSession({
      ...bindings,
      isRestart: true,
      initialAccessToken: null,
    });
    restarted.requestStartup();
    await restarted.awaitAutomaticWork();

    // The snapshot still counted the diverging edit (and surfaced every
    // pre-existing pending row through the wrapper's pending count) and
    // requested the pass …
    expect(restarted.snapshotResults).toEqual([
      { outcome: "completed", queuedEventCount: 3 },
    ]);
    // … the lifecycle adapter rejected pre-HTTP on the missing credential
    // and the driver PARKED the first rename retryable under the
    // login_required safe label — never terminal blocked_conflict — with
    // zero requests reaching the server, and the pass ended login_required
    // before the content lane dispatched anything.
    expect(restarted.passSummaries.at(-1)?.outcome).toBe("login_required");
    expect(restarted.lifecycleServer.requestBodies).toHaveLength(0);
    const alphaRename = restarted
      .eventsOf("notes/alpha-renamed.md")
      .find((event) => event.operation === "rename");
    expect(alphaRename?.state).toBe("waiting_retry");
    expect(alphaRename?.safeError).toBe("login_required");
    // The second rename and the edit survive untouched, waiting for a
    // trigger under a valid credential.
    const betaRename = restarted
      .eventsOf("notes/beta-renamed.md")
      .find((event) => event.operation === "rename");
    expect(betaRename?.state).toBe("queued");
    const editEvent = restarted
      .eventsOf("notes/gamma.md")
      .find((event) => event.operation === "update");
    expect(editEvent?.state).toBe("queued");
    expect(restarted.syncServer.preflightBodies).toHaveLength(3);

    // The background refresh completes a moment later: the status bar must
    // render the honest waiting surface — `Offline — queued` with the
    // pending count — instead of a healthy `Ready` while nothing syncs.
    restarted.credentialState.token = ACCESS_TOKEN;
    expect(restarted.repository.countPendingEvents()).toBe(3);
    expect(restarted.statusText()).toBe("Offline — queued (3)");

    // The one-shot scheduled retry trigger (fix round 2 D4) was armed by
    // the login_required pass end; once the credential returned and the
    // parked rename's backoff elapsed, its single firing drains
    // everything — the live post-restart `Ready (n)` strand recovers
    // with no user action.
    expect(restarted.scheduledRetryPass.isArmed()).toBe(true);
    restarted.advanceClock(2_000);
    await vi.advanceTimersByTimeAsync(2_000);
    await restarted.awaitScheduledRetryPass();
    expect(restarted.newestEventStateOf("notes/alpha-renamed.md")).toBe("committed");
    expect(restarted.newestEventStateOf("notes/beta-renamed.md")).toBe("committed");
    expect(restarted.newestEventStateOf("notes/gamma.md")).toBe("committed");
    expect(restarted.repository.countPendingEvents()).toBe(0);
    expect(restarted.scheduledRetryPass.isArmed()).toBe(false);
    expect(restarted.statusText()).toBe("Ready");
  });
});
