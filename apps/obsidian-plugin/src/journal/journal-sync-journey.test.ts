/**
 * End-to-end journal sync journeys over the real durable stack (spec 6-10, 12).
 *
 * Every fixture drives the REAL portable journal — sql.js engine, verified
 * generation persistence, the repository over the single serialized commit
 * queue — plus the REAL queue driver and the REAL hand-mirrored sync client,
 * against an in-process server double at the raw transport boundary that
 * implements the served wire contract: identity-keyed operations, frozen
 * terminal results, exact replay, conflict and integrity verdicts. The
 * journeys pin the cross-boundary behaviors of the plan's fixture list:
 * offline create/update then reconnect, exact replay after a dropped
 * response, changed local bytes, a stale update base, lifecycle deferral,
 * the queue-cap `reconcile_required` flag and generation recovery.
 *
 * Privacy (spec 9): the double and the assertions never print paths,
 * digests, tokens or credential material.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { JournalEvent } from "./contracts";
import { MAX_PENDING_EVENTS } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import { JournalPersistence } from "./persistence";
import type { JournalFileStore } from "./persistence";
import { JournalQueueDriver } from "./queue-driver";
import { JournalRepository } from "./repository";
import type { JournalRepositoryDatabase } from "./repository";
import { projectJournalSyncStatus } from "./status";
import type { SqliteEngineModule } from "./sqlite-database";
import { createJournalSyncApi } from "./sync-api";
import type { SyncHttpRequest } from "./sync-api";

// --- the engine and the in-memory journal directory ----------------------------------------

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

// --- the in-process server double -----------------------------------------------------------

interface FrozenTerminal {
  readonly resultKind: "committed" | "no_change";
  readonly sourceId: string;
  readonly sourceVersionId: string;
  readonly contentVersion: number;
}

interface ReservedOperation {
  readonly identity: string;
  readonly sourceId: string | null;
  readonly baseVersionId: string | null;
  readonly sha256: string;
  readonly sizeBytes: number;
  state: "pending" | "receiving";
  terminal: FrozenTerminal | null;
}

/** Render one counter-derived canonical UUID text form. */
function countedUuid(prefixDigit: string, counter: number): string {
  return `${prefixDigit.repeat(8)}-${prefixDigit.repeat(4)}-4${prefixDigit.repeat(3)}-8${prefixDigit.repeat(3)}-${String(counter).padStart(12, "0")}`;
}

/**
 * The stateful wire-contract double behind the transport: identity-keyed
 * preflight with frozen terminal replay, server-side digest verification,
 * exactly-once publication and a current-version registry for update bases.
 * `isOffline` drops every request as an unreachable network; `dropResponses`
 * commits server-side and then drops the response bytes — the lost-response
 * condition of spec 10.3.
 */
class SyncServerDouble {
  readonly identityTerminals = new Map<string, FrozenTerminal>();
  readonly operations = new Map<string, ReservedOperation>();
  readonly currentVersions = new Map<string, string>();
  readonly preflightBodies: Record<string, unknown>[] = [];
  readonly receivedDigests: string[] = [];
  publications = 0;
  contentAttempts = 0;
  isOffline = false;
  dropResponses = false;
  interruptBeforePublicationOnce = false;
  #counter = 0;

  /** Optional mid-pass hook fired inside the next preflight handling. */
  onPreflight: (() => Promise<void> | void) | null = null;

  advanceCurrentVersion(sourceId: string, nextVersionId: string): void {
    this.currentVersions.set(sourceId, nextVersionId);
  }

  async handlePreflight(
    body: Record<string, unknown>,
  ): Promise<{ status: number; bodyText: string }> {
    this.preflightBodies.push(body);
    const hook = this.onPreflight;
    if (hook !== null) {
      this.onPreflight = null;
      await hook();
    }
    const identity = `${body["event_id"]}:${body["idempotency_key"]}`;
    const frozen = this.identityTerminals.get(identity);
    if (frozen !== undefined) {
      const outcome = frozen.resultKind === "committed" ? "committed_replay" : "no_change";
      return { status: 200, bodyText: successBody({ outcome, result: resultBody(frozen) }) };
    }
    const claimed = [...this.operations.values()].find(
      (operation) => operation.identity === identity && operation.state === "receiving",
    );
    if (claimed !== undefined) {
      return { status: 409, bodyText: errorBody("small_file_upload_state_invalid") };
    }
    if (body["operation"] === "update") {
      const sourceId = String(body["source_id"]);
      const current = this.currentVersions.get(sourceId);
      if (current !== undefined && current !== body["base_version_id"]) {
        return { status: 200, bodyText: successBody({ outcome: "conflict" }) };
      }
    }
    this.#counter += 1;
    const operationId = `${String(this.#counter).padStart(10, "0")}AbCdEfGhIjKlMnOpQrStUvWxYz`;
    this.operations.set(operationId, {
      identity,
      sourceId:
        body["operation"] === "update" ? String(body["source_id"]) : countedUuid("1", this.#counter),
      baseVersionId: body["operation"] === "update" ? String(body["base_version_id"]) : null,
      sha256: String(body["sha256"]),
      sizeBytes: Number(body["size_bytes"]),
      state: "pending",
      terminal: null,
    });
    return {
      status: 200,
      bodyText: successBody({
        outcome: "single_part_upload",
        operation_id: operationId,
        expires_at: "2026-08-18T10:00:00Z",
      }),
    };
  }

  async handleContent(
    operationId: string,
    bytes: Uint8Array,
  ): Promise<{ status: number; bodyText: string }> {
    this.contentAttempts += 1;
    const operation = this.operations.get(operationId);
    if (operation === undefined) {
      return { status: 404, bodyText: errorBody("small_file_operation_not_found") };
    }
    if (operation.terminal !== null) {
      return { status: 200, bodyText: successBody(resultBody(operation.terminal)) };
    }
    operation.state = "receiving";
    if (this.interruptBeforePublicationOnce) {
      this.interruptBeforePublicationOnce = false;
      throw new Error("content request interrupted after claim");
    }
    const digest = await sha256Hex(bytes);
    if (digest !== operation.sha256 || bytes.byteLength !== operation.sizeBytes) {
      return { status: 422, bodyText: errorBody("small_file_content_integrity_failed") };
    }
    const sourceId = operation.sourceId ?? countedUuid("1", 0);
    this.#counter += 1;
    const sourceVersionId = countedUuid("2", this.#counter);
    const priorTerminal = [...this.identityTerminals.values()].find(
      (terminal) => terminal.sourceId === sourceId,
    );
    const terminal: FrozenTerminal = {
      resultKind: "committed",
      sourceId,
      sourceVersionId,
      contentVersion: (priorTerminal?.contentVersion ?? 0) + 1,
    };
    this.currentVersions.set(sourceId, sourceVersionId);
    this.identityTerminals.set(operation.identity, terminal);
    operation.terminal = terminal;
    this.receivedDigests.push(digest);
    this.publications += 1;
    if (this.dropResponses) {
      throw new Error("response bytes were lost after the server committed");
    }
    return { status: 200, bodyText: successBody(resultBody(terminal)) };
  }
}

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

function resultBody(terminal: FrozenTerminal): Record<string, unknown> {
  return {
    result_kind: terminal.resultKind,
    source_id: terminal.sourceId,
    source_version_id: terminal.sourceVersionId,
    content_version: terminal.contentVersion,
    committed_at: "2026-08-18T00:00:00Z",
  };
}

// --- the journey harness --------------------------------------------------------------------

interface JourneyHarness {
  readonly repository: JournalRepository;
  readonly persistence: JournalPersistence;
  readonly driver: JournalQueueDriver;
  readonly server: SyncServerDouble;
  readonly store: InMemoryJournalFileStore;
  readonly vaultBytes: Map<string, Uint8Array>;
  advanceClock: (milliseconds: number) => void;
}

const EPOCH_BASE_MS = 1_784_000_000_000;

function bindRepository(persistence: JournalPersistence, shared: { clockMs: number; ids: number }) {
  const journalDatabase: JournalRepositoryDatabase = {
    runSerializedMutation(operation) {
      return persistence.commitGeneration(operation);
    },
    readAll(sql) {
      return persistence.readAll(sql);
    },
  };
  return new JournalRepository({
    database: journalDatabase,
    nowEpochMs: () => {
      shared.clockMs += 1;
      return shared.clockMs;
    },
    createId: () => {
      shared.ids += 1;
      return `00000000-0000-4000-8000-${String(shared.ids).padStart(12, "0")}`;
    },
  });
}

async function createJourneyHarness(): Promise<JourneyHarness> {
  const store = new InMemoryJournalFileStore();
  const server = new SyncServerDouble();
  const vaultBytes = new Map<string, Uint8Array>();
  const shared = { clockMs: EPOCH_BASE_MS, ids: 0 };
  let correlationCounter = 0;
  const persistence = new JournalPersistence({ fileStore: store, engineModule });
  await persistence.open();
  const repository = bindRepository(persistence, shared);
  const driver = new JournalQueueDriver({
    repository,
    syncApi: createJournalSyncApi({
      transport: async (request: SyncHttpRequest) => {
        if (server.isOffline) {
          throw new Error("network unreachable");
        }
        if (request.method === "PUT") {
          const operationId = request.url.split("/api/uploads/")[1]?.split("/")[0] ?? "";
          const bytes = new Uint8Array(request.body as ArrayBuffer);
          return server.handleContent(decodeURIComponent(operationId), bytes);
        }
        const body = JSON.parse(request.body as string) as Record<string, unknown>;
        return server.handlePreflight(body);
      },
      resolveOrigin: () => "https://sync.example.org",
      getAccessToken: () => "at1.journey-access",
    }),
    fileBytesReader: {
      readRegularFileBytes: async (normalizedPath) => vaultBytes.get(normalizedPath) ?? null,
    },
    refreshAccessToken: () => Promise.resolve(),
    nowEpochMs: () => shared.clockMs,
    createCorrelationId: () => `corr-${(correlationCounter += 1)}`,
    randomJitter: () => 0,
  });
  return {
    repository,
    persistence,
    driver,
    server,
    store,
    vaultBytes,
    advanceClock: (milliseconds) => {
      shared.clockMs += milliseconds;
    },
  };
}

/** Record one allowed capture of exactly these bytes and return the event. */
async function captureBytes(
  harness: JourneyHarness,
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

function localFileOf(harness: JourneyHarness, normalizedPath: string) {
  const localFile = harness.repository.readLocalFileByPath(normalizedPath);
  if (localFile === null) {
    throw new Error(`no tracked file for ${normalizedPath}`);
  }
  return localFile;
}

function eventsOfPath(
  harness: JourneyHarness,
  normalizedPath: string,
): readonly JournalEvent[] {
  return harness.repository.readEventsByLocalFileId(
    localFileOf(harness, normalizedPath).localFileId,
  );
}

/** Flip the first byte of one stored journal image, returning new bytes. */
async function corruptFirstByte(
  harness: JourneyHarness,
  fileName: string,
): Promise<ArrayBuffer> {
  const bytes = new Uint8Array(await harness.store.readBinary(fileName));
  const firstByte = bytes[0] ?? 1;
  bytes.set([firstByte ^ 0xff]);
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
}

// --- the journeys ---------------------------------------------------------------------------

describe("journal sync journeys over the durable stack", () => {
  it("processes offline creates and edits after reconnect through the generations", async () => {
    const harness = await createJourneyHarness();
    const encoder = new TextEncoder();
    harness.server.isOffline = true;
    await captureBytes(harness, "notes/offline-a.md", encoder.encode("offline content a"));
    await captureBytes(harness, "notes/offline-b.md", encoder.encode("offline content b"));

    const offlinePass = await harness.driver.runPass();
    // The retryable offline failure owns the pass end reason (fix round 1).
    expect(offlinePass.outcome).toBe("retry_scheduled");
    expect(eventsOfPath(harness, "notes/offline-a.md").map((event) => event.state)).toEqual([
      "waiting_retry",
    ]);
    expect(eventsOfPath(harness, "notes/offline-a.md")[0]?.safeError).toBe("network_offline");

    // Reconnect after the bounded backoff: both pending creates commit.
    harness.server.isOffline = false;
    harness.advanceClock(5_000);
    const reconnectPass = await harness.driver.runPass();
    expect(reconnectPass.outcome).toBe("completed");
    expect(reconnectPass.processedEventCount).toBe(2);
    expect(harness.server.publications).toBe(2);
    for (const path of ["notes/offline-a.md", "notes/offline-b.md"]) {
      const localFile = localFileOf(harness, path);
      expect(localFile.sourceId).not.toBeNull();
      expect(localFile.baseVersionId).not.toBeNull();
      expect(eventsOfPath(harness, path).at(-1)?.state).toBe("committed");
    }

    // A later edit rides the committed mapping as an honest update.
    await captureBytes(harness, "notes/offline-a.md", encoder.encode("offline content a, edited"));
    const updatePass = await harness.driver.runPass();
    expect(updatePass.outcome).toBe("completed");
    const updatePreflight = harness.server.preflightBodies.at(-1);
    expect(updatePreflight?.["operation"]).toBe("update");
    expect(updatePreflight?.["source_id"]).toBe(
      localFileOf(harness, "notes/offline-a.md").sourceId,
    );
    expect(eventsOfPath(harness, "notes/offline-a.md").at(-1)?.state).toBe("committed");
    expect(harness.server.publications).toBe(3);
  });

  it("replays exactly after the server committed but the response was lost", async () => {
    const harness = await createJourneyHarness();
    const bytes = new TextEncoder().encode("dropped response content");
    const event = await captureBytes(harness, "notes/dropped.md", bytes);
    harness.server.dropResponses = true;

    const droppedPass = await harness.driver.runPass();
    // The lost response ends as a retryable failure, not a drained pass.
    expect(droppedPass.outcome).toBe("retry_scheduled");
    // The server committed exactly once even though its response was lost.
    expect(harness.server.publications).toBe(1);
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("waiting_retry");

    harness.advanceClock(5_000);
    const replayPass = await harness.driver.runPass();
    expect(replayPass.outcome).toBe("completed");
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("committed");
    // The exact frozen receipt of the lost response, with no second upload:
    // the replay arrived through committed_replay on the same identity.
    const lastPreflight = harness.server.preflightBodies.at(-1);
    expect(lastPreflight?.["event_id"]).toBe(event.eventId);
    expect(lastPreflight?.["idempotency_key"]).toBe(event.idempotencyKey);
    expect(harness.server.publications).toBe(1);
    expect(harness.server.receivedDigests).toHaveLength(1);
    const localFile = localFileOf(harness, "notes/dropped.md");
    expect(localFile.sourceId).not.toBeNull();
    expect(localFile.baseVersionId).not.toBeNull();
  });

  it("resumes one claimed upload with the exact persisted operation token", async () => {
    const harness = await createJourneyHarness();
    const event = await captureBytes(
      harness,
      "notes/claimed-resume.md",
      new TextEncoder().encode("claimed upload resume"),
    );
    harness.server.interruptBeforePublicationOnce = true;

    await harness.driver.runPass();

    const interrupted = harness.repository.readEvent(event.eventId);
    expect(interrupted?.state).toBe("waiting_retry");
    expect(interrupted?.operationId).not.toBeNull();
    expect(harness.server.publications).toBe(0);
    expect(harness.server.operations.size).toBe(1);

    harness.advanceClock(5_000);
    const resumedPass = await harness.driver.runPass();

    expect(resumedPass.outcome).toBe("completed");
    expect(harness.repository.readEvent(event.eventId)?.state).toBe("committed");
    expect(harness.server.preflightBodies).toHaveLength(2);
    expect(harness.server.operations.size).toBe(1);
    expect(harness.server.contentAttempts).toBe(2);
    expect(harness.server.publications).toBe(1);
    expect(harness.server.receivedDigests).toHaveLength(1);
  });

  it("closes changed local bytes as integrity failed and commits the successor", async () => {
    const harness = await createJourneyHarness();
    const firstBytes = new TextEncoder().encode("first local bytes");
    const successorBytes = new TextEncoder().encode("successor local bytes");
    await captureBytes(harness, "notes/changed.md", firstBytes);

    // The file changes mid-pass, after the preflight froze the fingerprint:
    // the watcher records the successor event while the driver holds the
    // first observation's frozen identity.
    harness.server.onPreflight = async () => {
      harness.vaultBytes.set("notes/changed.md", successorBytes);
      await harness.repository.recordCapture({
        normalizedPath: "notes/changed.md",
        fingerprint: await deriveFrozenFingerprint(successorBytes),
        policyRevisionNumber: 2,
        admission: "policy_allowed",
      });
    };

    const changedPass = await harness.driver.runPass();
    expect(changedPass.outcome).toBe("completed");
    const states = eventsOfPath(harness, "notes/changed.md").map((event) => event.state);
    // The frozen event fails the client re-fingerprint; the successor commits.
    expect(states).toEqual(["integrity_failed", "committed"]);
    expect(harness.server.receivedDigests).toHaveLength(1);
    expect(harness.server.publications).toBe(1);
  });

  it("closes a stale update base as a terminal conflict with no upload", async () => {
    const harness = await createJourneyHarness();
    const bytes = new TextEncoder().encode("stale base content");
    await captureBytes(harness, "notes/stale.md", bytes);
    const createPass = await harness.driver.runPass();
    expect(createPass.outcome).toBe("completed");
    const localFile = localFileOf(harness, "notes/stale.md");
    expect(localFile.sourceId).not.toBeNull();

    // Another device advanced the current version past our committed base.
    harness.server.advanceCurrentVersion(localFile.sourceId ?? "", "22222222-2222-4222-8222-999999999999");
    await captureBytes(harness, "notes/stale.md", new TextEncoder().encode("stale base content 2"));
    const conflictPass = await harness.driver.runPass();
    expect(conflictPass.outcome).toBe("completed");

    const terminal = eventsOfPath(harness, "notes/stale.md").at(-1);
    expect(terminal?.state).toBe("blocked_conflict");
    expect(terminal?.safeError).toBe("blocked_conflict");
    // No content upload was ever attempted for the conflicted event.
    expect(harness.server.publications).toBe(1);
    expect(harness.server.receivedDigests).toHaveLength(1);
  });

  it("defers an event whose file vanished before the content stream", async () => {
    const harness = await createJourneyHarness();
    await captureBytes(harness, "notes/vanished.md", new TextEncoder().encode("vanishing content"));
    // The file disappears after capture: preflight opens the upload, the
    // byte read finds no regular file, and the event defers to lifecycle.
    harness.vaultBytes.delete("notes/vanished.md");

    const pass = await harness.driver.runPass();
    expect(pass.outcome).toBe("completed");
    const terminal = eventsOfPath(harness, "notes/vanished.md").at(-1);
    expect(terminal?.state).toBe("deferred_lifecycle");
    expect(terminal?.safeError).toBe("deferred_lifecycle");
    expect(harness.server.publications).toBe(0);
    expect(harness.server.receivedDigests).toHaveLength(0);
  });

  it("flags reconcile_required durably once the pending-event cap is reached", async () => {
    const harness = await createJourneyHarness();
    // Seed pending events directly inside one serialized generation commit,
    // exactly to one below the soft cap.
    await harness.persistence.commitGeneration((session) => {
      session.exec(
        Array.from(
          { length: MAX_PENDING_EVENTS - 1 },
          (_, index) =>
            `insert into local_files (local_file_id, normalized_path, source_id, observed_sha256, observed_size_bytes, observed_media_type, base_version_id, policy_revision) values ('30303030-3030-4030-8030-${String(index).padStart(12, "0")}', 'seed/cap-${index}.md', null, '${String(index).padStart(64, "0")}', 1, 'text/plain', null, 1);`,
        ).join(""),
      );
      session.exec(
        Array.from(
          { length: MAX_PENDING_EVENTS - 1 },
          (_, index) =>
            `insert into journal_events (event_id, local_file_id, idempotency_key, operation, sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count, next_eligible_retry_epoch_ms, safe_error, operation_id, created_at_epoch_ms) values ('31313131-3131-4131-8131-${String(index).padStart(12, "0")}', '30303030-3030-4030-8030-${String(index).padStart(12, "0")}', 'cap-key-${index}', 'create', '${String(index).padStart(64, "0")}', 1, 'text/plain', 'queued', 0, 0, null, null, null, ${index});`,
        ).join(""),
      );
      return undefined;
    });

    // The cap-reaching row still lands: the user's edit is never refused.
    const atLimit = await captureBytes(harness, "notes/at-cap.md", new TextEncoder().encode("cap"));
    expect(atLimit.state).toBe("queued");

    // The next new row is refused and the flag lands durably.
    const refused = await harness.repository.recordCapture({
      normalizedPath: "notes/over-cap.md",
      fingerprint: await deriveFrozenFingerprint(new TextEncoder().encode("over")),
      policyRevisionNumber: 2,
      admission: "policy_allowed",
    });
    expect(refused).toEqual({ outcome: "capture_refused", reason: "reconcile_required" });
    expect(harness.persistence.readJournalMeta().isReconcileRequired).toBe(true);
    expect(harness.persistence.isReconcileRequired).toBe(true);
    expect(harness.repository.readLocalFileByPath("notes/over-cap.md")).toBeNull();

    // The projection surfaces the hard stop the composition acts on.
    const snapshot = projectJournalSyncStatus({
      isReconcileRequired: harness.persistence.isReconcileRequired,
      eventStateErrorCounts: harness.repository.readEventStateErrorCounts(),
      hasAccessCredential: true,
      isQueuePassActive: false,
      lastQueuePassOutcome: null,
    });
    expect(snapshot.kind).toBe("reconcile_required");

    // The flag survives a further verified generation.
    await harness.repository.markEventTerminal(
      "31313131-3131-4131-8131-000000000000",
      "blocked_conflict",
      "blocked_conflict",
    );
    expect(harness.persistence.readJournalMeta().isReconcileRequired).toBe(true);
  });

  it("recovers the prior verified generation after a torn newest write", async () => {
    const harness = await createJourneyHarness();
    await captureBytes(harness, "notes/recovered.md", new TextEncoder().encode("recovery content"));
    const pass = await harness.driver.runPass();
    expect(pass.outcome).toBe("completed");
    expect(eventsOfPath(harness, "notes/recovered.md").at(-1)?.state).toBe("committed");
    const committedGeneration = harness.persistence.verifiedGenerationNumber;

    // One more durable mutation publishes a newer generation beyond the
    // committed state.
    await captureBytes(harness, "notes/recovered-next.md", new TextEncoder().encode("next"));
    const newestGeneration = harness.persistence.verifiedGenerationNumber;
    expect(newestGeneration).toBeGreaterThan(committedGeneration);

    // Tear the newest generation image; reopen must fall back to the prior
    // verified generation with the committed state intact.
    const tornName = `journal.sqlite.g${newestGeneration}`;
    await harness.store.writeBinary(tornName, await corruptFirstByte(harness, tornName));
    harness.persistence.close();

    const reopened = new JournalPersistence({ fileStore: harness.store, engineModule });
    await reopened.open();
    expect(reopened.recoveryState).toBe("prior_generation_recovered");
    expect(reopened.verifiedGenerationNumber).toBe(committedGeneration);

    // A repository bound over the recovered generation sees the committed
    // event and the unprocessed successor, and a pass over it continues.
    const shared = { clockMs: EPOCH_BASE_MS + 100_000, ids: 10_000 };
    const repository = bindRepository(reopened, shared);
    const localFile = repository.readLocalFileByPath("notes/recovered.md");
    expect(localFile).not.toBeNull();
    const recoveredEvents = repository.readEventsByLocalFileId(localFile?.localFileId ?? "");
    expect(recoveredEvents.at(-1)?.state).toBe("committed");
    expect(repository.readLocalFileByPath("notes/recovered-next.md")).toBeNull();
    reopened.close();
  });

  it("rebuilds an empty reconcile-required journal when nothing verifies", async () => {
    const harness = await createJourneyHarness();
    await captureBytes(harness, "notes/lost.md", new TextEncoder().encode("lost content"));
    const pass = await harness.driver.runPass();
    expect(pass.outcome).toBe("completed");

    // Corrupt EVERY retained generation image and the manifest.
    for (const fileName of [...harness.store.files.keys()]) {
      await harness.store.writeBinary(fileName, await corruptFirstByte(harness, fileName));
    }
    harness.persistence.close();

    const reopened = new JournalPersistence({ fileStore: harness.store, engineModule });
    await reopened.open();
    expect(reopened.recoveryState).toBe("empty_journal_rebuilt");
    expect(reopened.isReconcileRequired).toBe(true);
    // The Vault content itself is untouched by the rebuild.
    expect(harness.vaultBytes.get("notes/lost.md")).toBeDefined();
    reopened.close();
  });
});
