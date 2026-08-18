/**
 * Tests of the closed sync status projection (spec 11).
 *
 * The projection is the small plugin UX surface: one of the six closed
 * status values with counts, derived from a closed, redacted journal
 * histogram plus credential and driver facts. Every test drives the REAL
 * journal (sql.js engine, real repository, real generation persistence)
 * wherever journal outcomes matter, so the pinned behaviors are: the exact
 * six-value status vocabulary of spec 11, the full blocker guidance table,
 * the login/offline/syncing priority, redaction by construction (no path,
 * digest or credential ever reaches the status or its telemetry fixture),
 * the reconcile-required hard stop that stops the driver with the child-6
 * message, and a suspended pass that stays resumable across close/reopen.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { JournalEvent, JournalMeta, JournalSafeErrorLabel } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import { JournalPersistence } from "./persistence";
import type { JournalFileStore } from "./persistence";
import { JournalQueueDriver } from "./queue-driver";
import type { QueuePassSummary } from "./queue-driver";
import { JournalRepository } from "./repository";
import type { JournalEventStateErrorCount } from "./repository";
import {
  SYNC_BLOCKER_GUIDANCE_TEXT,
  SYNC_STATUS_KINDS,
  SYNC_STATUS_TEXT,
  projectJournalSyncStatus,
  renderJournalSyncStatusText,
  syncBlockerGuidanceLines,
} from "./status";
import type { JournalSyncStatusInput } from "./status";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";
import { createJournalSyncApi } from "./sync-api";
import type { SyncHttpRequest } from "./sync-api";

/** The real sql.js WebAssembly engine drives every status test (spec 6.1). */
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

// --- pure-projection helpers ------------------------------------------------------------------

function stateCountRow(
  state: JournalEventStateErrorCount["state"],
  safeError: JournalSafeErrorLabel | null,
  eventCount = 1,
): JournalEventStateErrorCount {
  return { state, safeError, eventCount };
}

function projectInput(
  overrides: Partial<JournalSyncStatusInput> = {},
): JournalSyncStatusInput {
  return {
    isReconcileRequired: false,
    eventStateErrorCounts: [],
    hasAccessCredential: true,
    isQueuePassActive: false,
    lastQueuePassOutcome: null,
    ...overrides,
  };
}

// --- real-journal helpers ---------------------------------------------------------------------

const ORIGIN = "https://sync.example.org";
const ACCESS_TOKEN = "at1.status-test-access";
/** Any small body: born-terminal admissions are forced by the test parameter. */
const SMALL_HINT_BYTES = 8;
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

type RawResponse = { status: number; bodyText: string };

/** The fake journal directory: an in-memory map (same shape as persistence tests). */
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

function createEmptyRepository(isReconcileRequired = false): JournalRepository {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired,
    recoveryState: "verified_generation_loaded",
  } satisfies JournalMeta);
  let currentEpochMs = 1_784_000_000_000;
  return new JournalRepository({
    database,
    nowEpochMs: () => currentEpochMs++,
    createId: () => crypto.randomUUID(),
  });
}

/** Record one capture of exactly these bytes and return the event. */
async function captureBytes(
  repository: JournalRepository,
  normalizedPath: string,
  bytes: Uint8Array,
  admission: "policy_allowed" | "excluded_policy" = "policy_allowed",
): Promise<JournalEvent> {
  const capture = await repository.recordCapture({
    normalizedPath,
    fingerprint: await deriveFrozenFingerprint(bytes),
    policyRevisionNumber: 2,
    admission,
  });
  if (capture.outcome === "capture_refused") {
    throw new Error("expected a recorded capture");
  }
  return capture.event;
}

/** A repository bound to a persistence's verified-generation commit path. */
function createPersistenceRepository(persistence: JournalPersistence): JournalRepository {
  return new JournalRepository({
    database: {
      runSerializedMutation(operation) {
        return persistence.commitGeneration(operation);
      },
      readAll(sql) {
        return persistence.readAll(sql);
      },
    },
  });
}

/** A driver over any repository with a replaceable raw transport. */
function createDriver(
  repository: JournalRepository,
  vaultBytes: Map<string, Uint8Array>,
): {
  readonly driver: JournalQueueDriver;
  installTransport: (transport: (request: SyncHttpRequest) => Promise<RawResponse>) => void;
} {
  let activeTransport: ((request: SyncHttpRequest) => Promise<RawResponse>) | null = null;
  const driver = new JournalQueueDriver({
    repository,
    syncApi: createJournalSyncApi({
      transport: (request) => {
        const transport = activeTransport;
        if (transport === null) {
          throw new Error("no transport installed");
        }
        return transport(request);
      },
      resolveOrigin: () => ORIGIN,
      getAccessToken: () => ACCESS_TOKEN,
    }),
    fileBytesReader: {
      readRegularFileBytes: async (normalizedPath) => vaultBytes.get(normalizedPath) ?? null,
    },
    refreshAccessToken: () => Promise.resolve(),
    nowEpochMs: () => 1_784_000_000_000,
    createCorrelationId: () => `corr-${crypto.randomUUID()}`,
    randomJitter: () => 0,
  });
  return {
    driver,
    installTransport: (transport) => {
      activeTransport = transport;
    },
  };
}

async function runPass(driver: { runPass(): Promise<QueuePassSummary> }): Promise<QueuePassSummary> {
  return driver.runPass();
}

/** Wait until the polled condition holds (bounded, 2 s ceiling). */
async function waitUntil(condition: () => boolean): Promise<void> {
  const deadline = Date.now() + 2_000;
  while (!condition()) {
    if (Date.now() > deadline) {
      throw new Error("condition not reached before the deadline");
    }
    await new Promise((resolve) => {
      setTimeout(resolve, 5);
    });
  }
}

// --- the closed vocabulary and pure projection (spec 11) --------------------------------------

describe("sync status projection vocabulary (spec 11)", () => {
  it("renders exactly the six closed status values of spec 11", () => {
    expect(SYNC_STATUS_KINDS).toHaveLength(6);
    expect(SYNC_STATUS_TEXT).toEqual({
      ready: "Ready",
      syncing: "Syncing",
      offline_queued: "Offline — queued",
      login_required: "Login required",
      policy_blocked: "Policy blocked",
      reconcile_required: "Reconcile required",
    });
  });

  it("carries the exact blocker guidance of the spec-11 table", () => {
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.blocked_size).toContain("16 MiB");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.blocked_size).toContain("multipart");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.excluded_policy).toContain("policy");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.excluded_policy).toContain("authorized");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.blocked_conflict).toContain("No overwrite");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.blocked_conflict).toContain("conflict");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.deferred_lifecycle).toContain("No overwrite");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.deferred_lifecycle).toContain("lifecycle");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.login_required).toContain("browser login");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.login_required).toContain("unchanged");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.reconcile_required).toContain("reconciliation");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.reconcile_required).toContain("child 6");
  });
});

describe("sync status projection (spec 11)", () => {
  it("projects ready when the journal holds no owed work", () => {
    const snapshot = projectJournalSyncStatus(projectInput());
    expect(snapshot.kind).toBe("ready");
    expect(snapshot.pendingEventCount).toBe(0);
    expect(snapshot.blockers).toEqual([]);
  });

  it("projects syncing while a bounded pass is active", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({ isQueuePassActive: true, eventStateErrorCounts: [stateCountRow("queued", null)] }),
    );
    expect(snapshot.kind).toBe("syncing");
  });

  it("projects offline — queued when pending events wait on network retry", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({
        eventStateErrorCounts: [
          stateCountRow("waiting_retry", "network_offline", 2),
          stateCountRow("queued", null),
        ],
      }),
    );
    expect(snapshot.kind).toBe("offline_queued");
    expect(snapshot.pendingEventCount).toBe(3);
  });

  it("does not treat non-network retries as offline", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({
        eventStateErrorCounts: [stateCountRow("waiting_retry", "login_required")],
        hasAccessCredential: true,
        lastQueuePassOutcome: "completed",
      }),
    );
    expect(snapshot.kind).toBe("ready");
    expect(snapshot.pendingEventCount).toBe(1);
  });

  it("projects login required when work is owed but no access credential exists", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({
        eventStateErrorCounts: [stateCountRow("queued", null)],
        hasAccessCredential: false,
      }),
    );
    expect(snapshot.kind).toBe("login_required");
    expect(snapshot.blockers).toContain("login_required");
  });

  it("keeps login required after a pass ended login-required, until a credential exists", () => {
    const noCredential = projectJournalSyncStatus(
      projectInput({ hasAccessCredential: false, lastQueuePassOutcome: "login_required" }),
    );
    expect(noCredential.kind).toBe("login_required");
    const withCredential = projectJournalSyncStatus(
      projectInput({ hasAccessCredential: true, lastQueuePassOutcome: "login_required" }),
    );
    expect(withCredential.kind).not.toBe("login_required");
  });

  it("projects policy blocked when policy-blocked evidence exists", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({ eventStateErrorCounts: [stateCountRow("excluded_policy", "excluded_policy")] }),
    );
    expect(snapshot.kind).toBe("policy_blocked");
    expect(snapshot.blockers).toEqual(["excluded_policy"]);
  });

  it("exposes each blocker condition from the journal histogram", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({
        eventStateErrorCounts: [
          stateCountRow("blocked_size", "blocked_size"),
          stateCountRow("excluded_policy", "excluded_policy"),
          stateCountRow("blocked_conflict", "blocked_conflict"),
          stateCountRow("deferred_lifecycle", "deferred_lifecycle"),
        ],
      }),
    );
    expect(snapshot.blockers).toEqual([
      "blocked_size",
      "excluded_policy",
      "blocked_conflict",
      "deferred_lifecycle",
    ]);
  });

  it("projects reconcile required above every other condition", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({
        isReconcileRequired: true,
        eventStateErrorCounts: [
          stateCountRow("queued", null),
          stateCountRow("excluded_policy", "excluded_policy"),
        ],
        hasAccessCredential: false,
        isQueuePassActive: true,
      }),
    );
    expect(snapshot.kind).toBe("reconcile_required");
    expect(snapshot.blockers[0]).toBe("reconcile_required");
  });

  it("ranks login required above an active pass and offline retry", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({
        eventStateErrorCounts: [stateCountRow("waiting_retry", "network_offline")],
        hasAccessCredential: false,
        isQueuePassActive: true,
      }),
    );
    expect(snapshot.kind).toBe("login_required");
  });

  it("ranks an active pass above offline retry", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({
        eventStateErrorCounts: [stateCountRow("waiting_retry", "network_offline")],
        isQueuePassActive: true,
      }),
    );
    expect(snapshot.kind).toBe("syncing");
  });

  it("renders the small status text with counts only", () => {
    expect(renderJournalSyncStatusText(projectJournalSyncStatus(projectInput()))).toBe("Ready");
    expect(
      renderJournalSyncStatusText(
        projectJournalSyncStatus(
          projectInput({
            eventStateErrorCounts: [stateCountRow("waiting_retry", "network_offline", 3)],
          }),
        ),
      ),
    ).toBe("Offline — queued (3)");
    expect(
      renderJournalSyncStatusText(
        projectJournalSyncStatus(projectInput({ isReconcileRequired: true })),
      ),
    ).toBe("Reconcile required");
  });

  it("collects the blocker guidance lines in closed table order", () => {
    const snapshot = projectJournalSyncStatus(
      projectInput({
        isReconcileRequired: true,
        eventStateErrorCounts: [
          stateCountRow("queued", null),
          stateCountRow("blocked_size", "blocked_size"),
          stateCountRow("deferred_lifecycle", "deferred_lifecycle"),
        ],
        hasAccessCredential: false,
      }),
    );
    expect(syncBlockerGuidanceLines(snapshot)).toEqual([
      SYNC_BLOCKER_GUIDANCE_TEXT.reconcile_required,
      SYNC_BLOCKER_GUIDANCE_TEXT.login_required,
      SYNC_BLOCKER_GUIDANCE_TEXT.blocked_size,
      SYNC_BLOCKER_GUIDANCE_TEXT.deferred_lifecycle,
    ]);
  });
});

// --- the real journal behind the projection ---------------------------------------------------

describe("sync status over the real journal (spec 11)", () => {
  it("projects from the repository histogram after real journal outcomes", async () => {
    const repository = createEmptyRepository();
    const allowedEvent = await captureBytes(
      repository,
      "notes/queued.md",
      new TextEncoder().encode("queued content"),
    );
    await captureBytes(
      repository,
      "notes/excluded.md",
      new TextEncoder().encode("excluded content"),
      "excluded_policy",
    );
    await repository.markEventPreflightStarted(allowedEvent.eventId);
    await repository.markEventWaitingRetry(allowedEvent.eventId, "network_offline", 1);

    const histogram = repository.readEventStateErrorCounts();
    expect(histogram).toEqual(
      expect.arrayContaining([
        { state: "waiting_retry", safeError: "network_offline", eventCount: 1 },
        { state: "excluded_policy", safeError: "excluded_policy", eventCount: 1 },
      ]),
    );

    const snapshot = projectJournalSyncStatus(
      projectInput({ eventStateErrorCounts: histogram }),
    );
    expect(snapshot.kind).toBe("offline_queued");
    expect(snapshot.pendingEventCount).toBe(1);
    expect(snapshot.blockers).toEqual(["excluded_policy"]);
  });

  it("keeps raw paths, digests and credentials out of the status telemetry", async () => {
    const repository = createEmptyRepository();
    const secretPath = "notes/private-secret-diary.md";
    const secretBytes = new TextEncoder().encode("private secret content");
    const secretDigest = (await deriveFrozenFingerprint(secretBytes)).sha256;
    await captureBytes(repository, secretPath, secretBytes);
    await captureBytes(
      repository,
      "notes/blocked-secret.md",
      new Uint8Array(SMALL_HINT_BYTES),
      "excluded_policy",
    );

    const snapshot = projectJournalSyncStatus(
      projectInput({
        eventStateErrorCounts: repository.readEventStateErrorCounts(),
      }),
    );
    const telemetryFixture = `${JSON.stringify(snapshot)} ${renderJournalSyncStatusText(snapshot)} ${JSON.stringify(
      snapshot.blockers.map((blocker) => SYNC_BLOCKER_GUIDANCE_TEXT[blocker]),
    )}`;
    for (const forbidden of [secretPath, "secret", ".md", "notes/", secretDigest, ACCESS_TOKEN, "at1.", "rt1."]) {
      expect(telemetryFixture).not.toContain(forbidden);
    }
    expect(telemetryFixture.match(/[0-9a-f]{64}/i)).toBeNull();
  });
});

// --- the reconcile-required hard stop (spec 11 carried requirement) ----------------------------

describe("reconcile-required hard stop (spec 11)", () => {
  it("stops the driver with the child-6 guidance when the journal is reconcile-required", async () => {
    const repository = createEmptyRepository(true);
    const event = await captureBytes(
      repository,
      "notes/reconcile.md",
      new TextEncoder().encode("reconcile content"),
    );

    const snapshot = projectJournalSyncStatus(
      projectInput({
        isReconcileRequired: true,
        eventStateErrorCounts: repository.readEventStateErrorCounts(),
      }),
    );
    expect(snapshot.kind).toBe("reconcile_required");
    expect(snapshot.blockers).toContain("reconcile_required");
    expect(SYNC_BLOCKER_GUIDANCE_TEXT.reconcile_required).toContain("child 6");

    // The plugin wiring rule: the projection verdict stops the driver.
    const { driver, installTransport } = createDriver(repository, new Map());
    const requests: SyncHttpRequest[] = [];
    installTransport(async (request) => {
      requests.push(request);
      return { status: 200, bodyText: SINGLE_PART_BODY };
    });
    if (snapshot.kind === "reconcile_required") {
      driver.stop();
    }
    expect(driver.isStopped).toBe(true);
    const summary = await runPass(driver);
    expect(summary.outcome).toBe("stopped");
    expect(requests).toHaveLength(0);
    expect(repository.readEvent(event.eventId)?.state).toBe("queued");
  });
});

// --- safe unload and suspension (spec 8, 11) ---------------------------------------------------

describe("suspended pass stays resumable across unload (spec 8, 11)", () => {
  it("leaves the journal resumable when a pass is suspended mid-request", async () => {
    const store = new InMemoryJournalFileStore();
    const persistence = new JournalPersistence({ fileStore: store, engineModule });
    await persistence.open();
    const repository = createPersistenceRepository(persistence);
    const vaultBytes = new Map<string, Uint8Array>([
      ["notes/suspend.md", new TextEncoder().encode("suspended content")],
    ]);
    const suspendBytes = vaultBytes.get("notes/suspend.md");
    if (suspendBytes === undefined) {
      throw new Error("suspension fixture bytes missing");
    }
    const event = await captureBytes(repository, "notes/suspend.md", suspendBytes);

    // One hanging preflight: the pass suspends while the request is in flight.
    const gate: { resolvePreflight: ((response: RawResponse) => void) | null } = {
      resolvePreflight: null,
    };
    const { driver, installTransport } = createDriver(repository, vaultBytes);
    installTransport(
      (request) =>
        new Promise<RawResponse>((resolve) => {
          gate.resolvePreflight = (response) => {
            if (request.method === "PUT") {
              resolve({ status: 200, bodyText: COMMITTED_RECEIPT });
              return;
            }
            resolve(response);
          };
        }),
    );
    const pass = runPass(driver);
    await waitUntil(() => gate.resolvePreflight !== null);

    // Suspend (mobile) / unload: stop, then close the journal store.
    driver.stop();
    gate.resolvePreflight?.({ status: 200, bodyText: SINGLE_PART_BODY });
    const suspendedSummary = await pass;
    expect(suspendedSummary.outcome).toBe("stopped");
    expect(persistence.attemptFinalFlush()).toBe("final_generation_current");
    persistence.close();

    // The next session recovers the verified generation and finds the
    // interrupted event re-eligible — no torn terminal state exists.
    const reopened = new JournalPersistence({ fileStore: store, engineModule });
    await reopened.open();
    const resumedRepository = createPersistenceRepository(reopened);
    const stored = resumedRepository.readEvent(event.eventId);
    expect(stored?.state).toBe("preflight");
    const eligible = resumedRepository.readOldestEligibleEvent(1_784_000_000_000);
    expect(eligible?.eventId).toBe(event.eventId);
    const histogram = resumedRepository.readEventStateErrorCounts();
    expect(histogram).toEqual([{ state: "preflight", safeError: null, eventCount: 1 }]);

    // The next bounded pass finishes the same-identity work.
    const resumeHarness = createDriver(resumedRepository, vaultBytes);
    const resumedRequests: SyncHttpRequest[] = [];
    resumeHarness.installTransport(async (request) => {
      resumedRequests.push(request);
      return { status: 200, bodyText: request.method === "PUT" ? COMMITTED_RECEIPT : SINGLE_PART_BODY };
    });
    const resumedSummary = await runPass(resumeHarness.driver);
    expect(resumedSummary.outcome).toBe("completed");
    expect(resumedRequests).toHaveLength(2);
    expect(resumedRepository.readEvent(event.eventId)?.state).toBe("committed");

    const snapshot = projectJournalSyncStatus(
      projectInput({ eventStateErrorCounts: resumedRepository.readEventStateErrorCounts() }),
    );
    expect(snapshot.kind).toBe("ready");
    reopened.close();
  });
});
