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
  publications = 0;
  #counter = 0;

  async handlePreflight(
    body: Record<string, unknown>,
  ): Promise<{ status: number; bodyText: string }> {
    this.preflightBodies.push(body);
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
      }
      passSummaries.push(summary);
      return summary;
    },
  });

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
        const result: AutomaticSnapshotResult = {
          outcome: summary.outcome === "completed" ? "completed" : "stopped",
          queuedEventCount: summary.queuedEventCount,
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

/** The rename listener sequence: notify, respect the settle delay, no pass. */
async function renameNote(session: Session, priorPath: string, newPath: string): Promise<void> {
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

  it("drains the queued renames and commits the edit when a healthy pass follows the edit", async () => {
    const { session } = await createConvergedFixture();
    await renameNote(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNote(session, "notes/beta.md", "notes/beta-renamed.md");

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

  it("drains after restart when a pending content edit also exists (the snapshot coalesces it)", async () => {
    const { bindings, session } = await createConvergedFixture();
    await renameNote(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNote(session, "notes/beta.md", "notes/beta-renamed.md");
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
    // fingerprint, the queued edit coalesces — the scan counts one queued
    // event and requests the one pass that drains everything.
    expect(restarted.snapshotResults).toEqual([
      { outcome: "completed", queuedEventCount: 1 },
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
    await renameNote(session, "notes/alpha.md", "notes/alpha-renamed.md");
    await renameNote(session, "notes/beta.md", "notes/beta-renamed.md");
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
});
