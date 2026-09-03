/**
 * Two-device device-sync journeys through the PRODUCTION plugin stack
 * (device cursor and manifest reconciliation, task 13).
 *
 * Every service under test is the production implementation: the
 * `DeviceSyncRepository` and `JournalRepository` over a real sql.js
 * database, the production `JournalCapture`, `ManifestCapture`,
 * `AtomicVaultWriterImpl`, `RemoteEventApplier`, `ManifestReconciler`,
 * `SyncCoordinator` and the hand-mirrored `DeviceSyncApi` client whose
 * wire parsing is fully real. The only doubles are the ones the composition
 * itself injects: a fake clock and one-shot scheduler, an in-memory Vault
 * (the bytes double), a recording diagnostics trail, the capture-lane
 * policy gate, and an in-memory scripted HTTP server that speaks the exact
 * device-sync wire grammar and models device A — the other device of every
 * journey — committing canonical edits, renames, deletes and policy
 * advances at exact journey moments. Cursor, manifest, policy and identity
 * proof are never bypassed: they all run for real on both sides.
 *
 * The journeys pinned (the task-13 brief, step 1): a remote edit applies
 * through the verified download and never echoes back; an exact self-origin
 * echo is suppressed by evidence while a foreign echo applies; the
 * lifecycle events (rename, delete) apply to the Vault; a canonical commit
 * landing after a run's checkpoint stays outside the plan and the fence and
 * the next pull delivers it; a lost acknowledgement stays owed and is
 * retried before the next pull without a second apply; a cursor gap fails
 * closed with its readable reason and the manifest repair converges past
 * it; a mid-run policy advance restarts exactly one fresh checkpoint-bound
 * run; SQLite loss rebinds the capture without planning a duplicate
 * upload; and a local edit during the reconciliation settles as the closed
 * divergence reason while the edited bytes survive.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { FrozenFingerprint, JournalMeta } from "../journal/contracts";
import { JournalCapture } from "../journal/capture";
import type { CapturePolicyGate } from "../journal/capture";
import type { LifecycleCapture } from "../journal/lifecycle-capture";
import { JournalRepository } from "../journal/repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import type {
  SyncDiagnosticsTrail,
  SyncDiagnosticsTrailAppendInput,
  SyncDiagnosticTrailEntry,
} from "../journal/sync-diagnostics-trail";
import { AtomicVaultWriterImpl } from "./atomic-vault-writer";
import type { VaultMutationSeam } from "./atomic-vault-writer";
import { createDeviceSyncApi } from "./api";
import type { DeviceSyncHttpResponse, DeviceSyncHttpTransport } from "./api";
import type { DeviceSyncDiagnostics, DeviceSyncRepository } from "./contracts";
import { createDeviceSyncDiagnostics } from "./diagnostics";
import { computeManifestFinalDigest, createManifestCapture } from "./manifest-capture";
import {
  createManifestReconciler,
  createManifestReconcilerJournal,
} from "./manifest-reconciler";
import { createRemoteEventApplier } from "./remote-event-applier";
import { createSyncCoordinator } from "./sync-coordinator";
import type { SyncCoordinator } from "./sync-coordinator";

// Parallel-coverage headroom: the real-timer settling loops can exceed Vitest's
// 5 s default under full-suite load; this raises wall-clock budget only.
vi.setConfig({ testTimeout: 30_000 });

/** The real sql.js WebAssembly engine drives every journey (spec 6.1). */
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

const OWN_DEVICE_ID = "d0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d0d0";
const OTHER_DEVICE_ID = "e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1";

function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

async function fingerprintOf(bytes: Uint8Array): Promise<FrozenFingerprint> {
  return {
    sha256: await sha256Hex(bytes),
    sizeBytes: bytes.byteLength,
    mediaType: "text/markdown",
  };
}

/**
 * A deterministic UUID-shaped id from one seed: the seed's UTF-16 code units
 * expand to lowercase hex (so distinct seeds never collide the way stripped
 * characters would), padded/truncated to the 32 hex digits of a UUID with
 * the version and variant nibbles forced.
 */
/** One 32-bit FNV-1a round over the whole seed (offset personalizes it). */
function fnv1aOf(seed: string, offset: number): string {
  let hash = (0x811c9dc5 ^ offset) >>> 0;
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

/**
 * A deterministic UUID-shaped id from one seed: four personalized FNV-1a
 * rounds fold the WHOLE seed into 32 hex digits (a truncated code-unit
 * expansion would collide on shared prefixes), with the version and
 * variant nibbles forced.
 */
function uuidOf(seed: string): string {
  const digest =
    fnv1aOf(seed, 0) + fnv1aOf(seed, 1) + fnv1aOf(seed, 2) + fnv1aOf(seed, 3);
  return [
    digest.slice(0, 8),
    digest.slice(8, 12),
    `4${digest.slice(13, 16)}`,
    `8${digest.slice(17, 20)}`,
    digest.slice(20, 32),
  ].join("-");
}

// --- the fake clock and scheduler ---------------------------------------------------------

class FakeScheduler {
  nowEpochMs = 0;
  readonly #timers: { fireAtEpochMs: number; callback: () => void; isCancelled: boolean }[] = [];

  schedule(delayMs: number, callback: () => void): () => void {
    const timer = { fireAtEpochMs: this.nowEpochMs + delayMs, callback, isCancelled: false };
    this.#timers.push(timer);
    return () => {
      timer.isCancelled = true;
    };
  }

  /** Move the fake clock forward, firing every due timer to completion. */
  advance(milliseconds: number): void {
    this.nowEpochMs += milliseconds;
    let firedAny = true;
    while (firedAny) {
      firedAny = false;
      for (const timer of [...this.#timers]) {
        if (!timer.isCancelled && timer.fireAtEpochMs <= this.nowEpochMs) {
          timer.isCancelled = true;
          timer.callback();
          firedAny = true;
        }
      }
    }
  }
}

/**
 * Flush one fire-and-forget coordinator cycle to completion. The cycle's
 * chain crosses REAL macrotasks (the WebCrypto digests of the verified
 * download and the fingerprints), so pumping only microtasks would stall
 * it mid-download: every round yields one macrotask turn plus a microtask
 * drain, deterministically settling the whole cycle without touching the
 * fake clock.
 */
async function flushCycles(rounds = 80): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
    for (let micro = 0; micro < 50; micro += 1) {
      await Promise.resolve();
    }
  }
}

// --- the recording diagnostics trail (the plugin's trail surface) ------------------------

class RecordingTrail implements SyncDiagnosticsTrail {
  readonly entries: SyncDiagnosticTrailEntry[] = [];

  async load(): Promise<void> {
    return;
  }

  async append(input: SyncDiagnosticsTrailAppendInput): Promise<void> {
    this.entries.push({
      kind: input.kind,
      tokens: input.tokens,
    } as unknown as SyncDiagnosticTrailEntry);
  }

  readEntries(): readonly SyncDiagnosticTrailEntry[] {
    return this.entries;
  }

  readAppendFailureCount(): number {
    return 0;
  }

  /** Every rendered reason token, in order (the readable trail surface). */
  rendered(): string {
    return this.entries.map((entry) => `${entry.kind}:${entry.tokens.join("/")}`).join("\n");
  }
}

// --- the in-memory Vault (the bytes double) ------------------------------------------------

class InMemoryVault implements VaultMutationSeam {
  readonly #files = new Map<string, Uint8Array>();
  writeCount = 0;
  /**
   * Locators whose vault writes always throw (a locked/unwritable
   * target — the 2026-09-03 apply-wedge finding's fault injection): the
   * writer maps the throw onto the closed `device_apply_vault_failed`
   * family exactly like a real Vault refusal.
   */
  readonly failWritesAtLocator = new Set<string>();
  /**
   * File basenames whose writes always throw — the locked target AND its
   * dot-prefixed staging siblings (the 2026-09-03 apply-wedge finding's
   * live shape: the refusal sat at `verify_temp`, the staged hidden
   * sibling's write, so the durable row rests `prepared` and no mutation
   * ever ran). A real locked/unwritable file refuses every write shape
   * the atomic writer attempts for it.
   */
  readonly failWritesForBasenames = new Set<string>();

  /** Whether one locator's write is refused: the exact locator or a locked
   * basename's own hidden sibling (`.<basename>.<suffix>-<token>`). */
  #isWriteRefused(locator: string): boolean {
    if (this.failWritesAtLocator.has(locator)) {
      return true;
    }
    if (this.failWritesForBasenames.size === 0) {
      return false;
    }
    const lastSlash = locator.lastIndexOf("/");
    const baseName = lastSlash === -1 ? locator : locator.slice(lastSlash + 1);
    for (const refusedBaseName of this.failWritesForBasenames) {
      if (baseName === refusedBaseName || baseName.startsWith(`.${refusedBaseName}.`)) {
        return true;
      }
    }
    return false;
  }

  setFileBytes(locator: string, bytes: Uint8Array): void {
    this.#files.set(locator, bytes);
  }

  deleteFile(locator: string): void {
    this.#files.delete(locator);
  }

  fileBytes(locator: string): Uint8Array | null {
    return this.#files.get(locator) ?? null;
  }

  has(locator: string): boolean {
    return this.#files.has(locator);
  }

  async listRegularFilePaths(): Promise<readonly string[]> {
    return [...this.#files.keys()];
  }

  async readRegularFileBytes(normalizedPath: string): Promise<Uint8Array | null> {
    return this.#files.get(normalizedPath) ?? null;
  }

  async locatorExists(locator: string): Promise<boolean> {
    return this.#files.has(locator);
  }

  async readBytes(locator: string): Promise<Uint8Array | null> {
    return this.#files.get(locator) ?? null;
  }

  async createFile(locator: string, bytes: Uint8Array): Promise<void> {
    if (this.#isWriteRefused(locator)) {
      throw new Error("vault write refused");
    }
    this.writeCount += 1;
    this.#files.set(locator, bytes);
  }

  async renameLocator(fromLocator: string, toLocator: string): Promise<void> {
    if (this.#isWriteRefused(toLocator)) {
      throw new Error("vault write refused");
    }
    this.writeCount += 1;
    const bytes = this.#files.get(fromLocator);
    if (bytes === undefined) {
      throw new Error("vault cannot rename an absent file");
    }
    this.#files.delete(fromLocator);
    this.#files.set(toLocator, bytes);
  }

  async trashLocator(locator: string): Promise<void> {
    if (this.#isWriteRefused(locator)) {
      throw new Error("vault write refused");
    }
    this.writeCount += 1;
    if (!this.#files.delete(locator)) {
      throw new Error("vault cannot trash an absent file");
    }
  }
}

function allowedPolicyGate(): CapturePolicyGate {
  return {
    evaluateForCapture() {
      return { decision: { raw: "allowed", enforced: "allowed" }, revisionNumber: 1 };
    },
  };
}

function silentLifecycleCapture(): LifecycleCapture {
  return {
    captureRename: async () => null,
    captureDelete: async () => null,
    requestRestore: async () => {
      throw new Error("the journeys never request a restore");
    },
  };
}

// --- the scripted server (device A and the canonical state) --------------------------------

interface ServerFile {
  readonly sourceId: string;
  versionId: string;
  fingerprint: FrozenFingerprint;
  bytes: Uint8Array;
  isDeleted: boolean;
  tombstoneId: string | null;
  /** The version timeline in commit order — the checkpoint bound source. */
  readonly history: { sequence: number; versionId: string; fingerprint: FrozenFingerprint }[];
}

interface ServerEventWire {
  readonly event_id: string;
  readonly event_sequence: number;
  readonly event_type: string;
  readonly source_id: string;
  readonly origin_device_id: string | null;
  readonly base_version_id: string | null;
  readonly current_version_id: string | null;
  readonly base_fingerprint: { sha256: string; size_bytes: number; media_type: string } | null;
  readonly current_fingerprint: { sha256: string; size_bytes: number; media_type: string } | null;
  readonly prior_locator: string | null;
  readonly resulting_locator: string | null;
  readonly tombstone_id: string | null;
  readonly committed_at: string;
}

interface WireAction {
  readonly action_index: number;
  readonly action_kind: string;
  readonly local_entry_id: string | null;
  readonly source_id: string | null;
  readonly source_version_id: string | null;
  readonly source_locator_id: string | null;
  readonly source_tombstone_id: string | null;
  readonly reason: string | null;
  readonly checkpoint_locator: string | null;
}

/**
 * The in-memory device-sync server: speaks the exact wire grammar through
 * the transport seam, keeps the canonical file state device A commits to,
 * models the single manifest run with its checkpoint fence, and exposes the
 * fault and journey hooks the tests inject at exact moments.
 */
class ScriptedServer {
  readonly pulls: string[] = [];
  readonly acknowledgements: {
    readonly expectedPrevious: number;
    readonly appliedThrough: number;
  }[] = [];
  readonly starts: number[] = [];
  readonly completions: string[] = [];
  acknowledgedSequence = 0;
  /** Fail the NEXT pull with a transport-shaped rejection. */
  failNextPull = false;
  /** Record the NEXT acknowledgement server-side, then lose the response. */
  loseNextAcknowledgement = false;
  /** Reject the FIRST finalize call with the policy-advance envelope. */
  policyAdvanceOnFirstFinalize = false;
  /**
   * Fail every actions page read with a transient 500 while set — the
   * mid-run wedge moment: the run rests planned and bound until the test
   * clears the flag (a one-shot failure would let the coordinator's
   * back-scheduled retry race the test's vault edit).
   */
  failActionsRead = false;
  /** Hook awaited inside the first run start — the checkpoint race moment. */
  onFirstStart: (() => Promise<void> | void) | null = null;
  /** Hook invoked before the first action page is served — the edit moment. */
  onFirstActionsRead: (() => void) | null = null;

  #events: ServerEventWire[] = [];
  #versions = new Map<string, { bytes: Uint8Array; fingerprint: FrozenFingerprint }>();
  #files = new Map<string, ServerFile>();
  #deletedFiles = new Map<string, ServerFile>();
  #nextSequence = 1;
  #run: {
    manifestRunId: string;
    checkpointSequence: number;
    nextPageNumber: number;
    entryCount: number;
    generation: number;
    pages: { readonly pageNumber: number; readonly entryCount: number; readonly pageDigest: string }[];
    entries: { localEntryId: string; locator: string; fingerprint: FrozenFingerprint }[];
    actions: WireAction[];
  } | null = null;
  #pendingStartGeneration = 0;
  #lastActions: readonly WireAction[] = [];
  #servedFirstActionPage = false;

  /** Advance the sequence clock without committing events (the gap seed). */
  deferSequences(count: number): void {
    this.#nextSequence += count;
  }

  // -- device A's canonical commits ----------------------------------------------------

  async commitCreate(locator: string, bytes: Uint8Array): Promise<void> {
    const sourceId = uuidOf(`source${locator}`);
    const versionId = uuidOf(`version${locator}${this.#nextSequence}`);
    const fingerprint = await fingerprintOf(bytes);
    const eventSequence = this.#nextSequence;
    this.#nextSequence += 1;
    this.#versions.set(versionId, { bytes, fingerprint });
    this.#files.set(locator, {
      sourceId,
      versionId,
      fingerprint,
      bytes,
      isDeleted: false,
      tombstoneId: null,
      history: [{ sequence: eventSequence, versionId, fingerprint }],
    });
    this.#events.push({
      event_id: uuidOf(`event${eventSequence}`),
      event_sequence: eventSequence,
      event_type: "created",
      source_id: sourceId,
      origin_device_id: OTHER_DEVICE_ID,
      base_version_id: null,
      current_version_id: versionId,
      base_fingerprint: null,
      current_fingerprint: wireFingerprint(fingerprint),
      prior_locator: null,
      resulting_locator: locator,
      tombstone_id: null,
      committed_at: "2026-08-26T01:00:00Z",
    });
  }

  async commitUpdate(locator: string, bytes: Uint8Array): Promise<void> {
    const file = this.#files.get(locator);
    if (file === undefined) {
      throw new Error("server cannot update an absent file");
    }
    const versionId = uuidOf(`version${locator}${this.#nextSequence}`);
    const fingerprint = await fingerprintOf(bytes);
    const eventSequence = this.#nextSequence;
    this.#nextSequence += 1;
    file.versionId = versionId;
    file.fingerprint = fingerprint;
    file.bytes = bytes;
    this.#versions.set(versionId, { bytes, fingerprint });
    file.history.push({ sequence: eventSequence, versionId, fingerprint });
    this.#events.push({
      event_id: uuidOf(`event${eventSequence}`),
      event_sequence: eventSequence,
      event_type: "updated",
      source_id: file.sourceId,
      origin_device_id: OTHER_DEVICE_ID,
      base_version_id: null,
      current_version_id: versionId,
      base_fingerprint: null,
      current_fingerprint: wireFingerprint(fingerprint),
      prior_locator: null,
      resulting_locator: locator,
      tombstone_id: null,
      committed_at: "2026-08-26T01:00:00Z",
    });
  }

  async commitRename(fromLocator: string, toLocator: string): Promise<void> {
    const file = this.#files.get(fromLocator);
    if (file === undefined) {
      throw new Error("server cannot rename an absent file");
    }
    const eventSequence = this.#nextSequence;
    this.#nextSequence += 1;
    this.#files.delete(fromLocator);
    this.#files.set(toLocator, file);
    this.#events.push({
      event_id: uuidOf(`event${eventSequence}`),
      event_sequence: eventSequence,
      event_type: "renamed",
      source_id: file.sourceId,
      origin_device_id: OTHER_DEVICE_ID,
      base_version_id: file.versionId,
      current_version_id: file.versionId,
      base_fingerprint: null,
      current_fingerprint: wireFingerprint(file.fingerprint),
      prior_locator: fromLocator,
      resulting_locator: toLocator,
      tombstone_id: null,
      committed_at: "2026-08-26T01:00:00Z",
    });
  }

  async commitDelete(locator: string): Promise<void> {
    const file = this.#files.get(locator);
    if (file === undefined) {
      throw new Error("server cannot delete an absent file");
    }
    const eventSequence = this.#nextSequence;
    this.#nextSequence += 1;
    const tombstoneId = uuidOf(`tombstone${locator}`);
    file.isDeleted = true;
    file.tombstoneId = tombstoneId;
    this.#files.delete(locator);
    this.#deletedFiles.set(locator, file);
    this.#events.push({
      event_id: uuidOf(`event${eventSequence}`),
      event_sequence: eventSequence,
      event_type: "deleted",
      source_id: file.sourceId,
      origin_device_id: OTHER_DEVICE_ID,
      base_version_id: file.versionId,
      current_version_id: null,
      base_fingerprint: wireFingerprint(file.fingerprint),
      current_fingerprint: null,
      prior_locator: locator,
      resulting_locator: null,
      tombstone_id: tombstoneId,
      committed_at: "2026-08-26T01:00:00Z",
    });
  }

  /** The self-origin echo of our own committed upload (exact evidence). */
  async commitSelfOriginEcho(input: {
    readonly locator: string;
    readonly sourceId: string;
    readonly versionId: string;
    readonly fingerprint: FrozenFingerprint;
  }): Promise<void> {
    const eventSequence = this.#nextSequence;
    this.#nextSequence += 1;
    this.#events.push({
      event_id: uuidOf(`event${eventSequence}`),
      event_sequence: eventSequence,
      event_type: "created",
      source_id: input.sourceId,
      origin_device_id: OWN_DEVICE_ID,
      base_version_id: null,
      current_version_id: input.versionId,
      base_fingerprint: null,
      current_fingerprint: wireFingerprint(input.fingerprint),
      prior_locator: null,
      resulting_locator: input.locator,
      tombstone_id: null,
      committed_at: "2026-08-26T01:00:00Z",
    });
  }

  fileAt(locator: string): ServerFile | undefined {
    return this.#files.get(locator) ?? this.#deletedFiles.get(locator);
  }

  /** The file's live state as of the run checkpoint (never a later commit). */
  #fileStateAt(locator: string, checkpoint: number): ServerFile | undefined {
    const file = this.fileAt(locator);
    if (file === undefined) {
      return undefined;
    }
    let bounded: { sequence: number; versionId: string; fingerprint: FrozenFingerprint } | undefined;
    for (let index = file.history.length - 1; index >= 0; index -= 1) {
      const candidate = file.history[index];
      if (candidate !== undefined && candidate.sequence <= checkpoint) {
        bounded = candidate;
        break;
      }
    }
    if (bounded === undefined) {
      return undefined;
    }
    return {
      ...file,
      versionId: bounded.versionId,
      fingerprint: bounded.fingerprint,
    };
  }

  // -- the transport (the wire the real client parses) ----------------------------------

  readonly transport: DeviceSyncHttpTransport = async (request) => {
    const url = new URL(request.url);
    const path = url.pathname;
    if (request.method === "GET" && path === "/api/sync/events") {
      return this.#servePull();
    }
    if (request.method === "POST" && path === "/api/sync/cursor-acknowledgements") {
      return this.#serveAcknowledgement(request.body ?? "");
    }
    if (request.method === "POST" && path === "/api/sync/manifests") {
      const rawBody = request.body;
      const body = JSON.parse(typeof rawBody === "string" ? rawBody : "{}") as {
        client_observation_generation?: number;
      };
      this.#pendingStartGeneration = body.client_observation_generation ?? 0;
      return await this.#serveStart();
    }
    const pageMatch = /^\/api\/sync\/manifests\/([^/]+)\/pages\/(\d+)$/.exec(path);
    if (request.method === "PUT" && pageMatch !== null) {
      return this.#servePage(
        pageMatch[1] ?? "",
        Number.parseInt(pageMatch[2] ?? "0", 10),
        request.body,
      );
    }
    const finalizeMatch = /^\/api\/sync\/manifests\/([^/]+)\/finalize$/.exec(path);
    if (request.method === "POST" && finalizeMatch !== null) {
      return await this.#serveFinalize(request.body);
    }
    const actionsMatch = /^\/api\/sync\/manifests\/([^/]+)\/actions$/.exec(path);
    if (request.method === "GET" && actionsMatch !== null) {
      if (this.failActionsRead) {
        return errorResponse(500, "internal_error");
      }
      return this.#serveActions(url);
    }
    const completeMatch = /^\/api\/sync\/manifests\/([^/]+)\/complete$/.exec(path);
    if (request.method === "POST" && completeMatch !== null) {
      return this.#serveComplete();
    }
    const downloadMatch = /^\/api\/sources\/([^/]+)\/versions\/([^/]+)\/content$/.exec(path);
    if (request.method === "GET" && downloadMatch !== null) {
      return this.#serveDownload(downloadMatch[2] ?? "");
    }
    return errorResponse(404, "device_manifest_not_found");
  };

  #servePull(): DeviceSyncHttpResponse {
    this.pulls.push(`/api/sync/events@${this.acknowledgedSequence}`);
    if (this.failNextPull) {
      this.failNextPull = false;
      throw new TypeError("network is unreachable");
    }
    const events = this.#events.filter(
      (event) => event.event_sequence > this.acknowledgedSequence,
    );
    const maximum = this.#events.length > 0 ? this.#nextSequence - 1 : 0;
    return jsonResponse({
      acknowledged_sequence: this.acknowledgedSequence,
      delivered_through_sequence:
        events.at(-1)?.event_sequence ?? this.acknowledgedSequence,
      page_checkpoint_sequence: maximum,
      events,
      has_more: false,
    });
  }

  #serveAcknowledgement(body: string | ArrayBuffer | undefined): DeviceSyncHttpResponse {
    const parsed = JSON.parse(typeof body === "string" ? body : "{}") as {
      expected_previous_sequence: number;
      applied_through_sequence: number;
    };
    this.acknowledgements.push({
      expectedPrevious: parsed.expected_previous_sequence,
      appliedThrough: parsed.applied_through_sequence,
    });
    if (parsed.applied_through_sequence < this.acknowledgedSequence) {
      return errorResponse(409, "device_cursor_regression");
    }
    this.acknowledgedSequence = parsed.applied_through_sequence;
    if (this.loseNextAcknowledgement) {
      this.loseNextAcknowledgement = false;
      // The server durably recorded the acknowledgement; the response died.
      throw new TypeError("network is unreachable");
    }
    return jsonResponse({
      acknowledged_sequence: this.acknowledgedSequence,
      delivered_through_sequence: Math.max(this.acknowledgedSequence, this.#nextSequence - 1),
    });
  }

  async #serveStart(): Promise<DeviceSyncHttpResponse> {
    const generation = this.#pendingStartGeneration;
    this.starts.push(generation);
    const checkpoint = this.#events.length > 0 ? this.#nextSequence - 1 : 0;
    if (this.#run !== null && this.#run.generation !== generation) {
      // The real server expires the device's unfinished run when a start
      // carries a DIFFERENT observation generation (the client minted a
      // newer barrier and abandoned the run's progress) — the sanctioned
      // server-side invalidation the 2026-09-03 restart-asymmetry fix
      // keys on.
      this.#run = null;
    }
    if (this.#run === null) {
      this.#run = {
        manifestRunId: uuidOf(`run${this.starts.length}`),
        checkpointSequence: checkpoint,
        nextPageNumber: 0,
        entryCount: 0,
        pages: [],
        entries: [],
        actions: [],
        generation,
      };
    }
    if (this.starts.length === 1) {
      await this.onFirstStart?.();
    }
    return jsonResponse(this.#runReceipt("collecting"));
  }

  #runReceipt(state: string): Record<string, unknown> {
    if (this.#run === null) {
      throw new Error("no manifest run is open");
    }
    return {
      manifest_run_id: this.#run.manifestRunId,
      state,
      base_acknowledged_sequence: this.acknowledgedSequence,
      checkpoint_sequence: this.#run.checkpointSequence,
      policy_revision_number: 1,
      client_observation_generation: this.#run.generation,
      next_page_number: this.#run.nextPageNumber,
      entry_count: this.#run.entryCount,
      expires_at: "2026-08-26T02:00:00Z",
    };
  }

  #servePage(
    manifestRunId: string,
    pageNumber: number,
    body: string | ArrayBuffer | undefined,
  ): DeviceSyncHttpResponse {
    if (this.#run === null || this.#run.manifestRunId !== manifestRunId) {
      return errorResponse(404, "device_manifest_not_found");
    }
    if (pageNumber !== this.#run.nextPageNumber) {
      return errorResponse(409, "device_manifest_page_invalid");
    }
    const parsed = JSON.parse(typeof body === "string" ? body : "{}") as {
      entries: readonly {
        local_entry_id: string;
        normalized_locator: string;
        fingerprint: { sha256: string; size_bytes: number; media_type: string };
      }[];
      page_digest?: string;
    };
    this.#run.entries.push(
      ...parsed.entries.map((entry) => ({
        localEntryId: entry.local_entry_id,
        locator: entry.normalized_locator,
        fingerprint: {
          sha256: entry.fingerprint.sha256,
          sizeBytes: entry.fingerprint.size_bytes,
          mediaType: entry.fingerprint.media_type,
        } satisfies FrozenFingerprint,
      })),
    );
    // The real server RETAINS each accepted page's digest and verifies the
    // finalize digest against them — a resumed run whose fresh capture
    // contradicts the retained pages can never finalize (the 2026-09-03
    // restart-asymmetry finding).
    this.#run.pages.push({
      pageNumber,
      entryCount: parsed.entries.length,
      pageDigest: parsed.page_digest ?? "",
    });
    this.#run.entryCount += parsed.entries.length;
    this.#run.nextPageNumber += 1;
    return jsonResponse({
      manifest_run_id: manifestRunId,
      page_number: pageNumber,
      accepted_entry_count: parsed.entries.length,
      next_page_number: this.#run.nextPageNumber,
    });
  }

  async #serveFinalize(body: string | ArrayBuffer | undefined): Promise<DeviceSyncHttpResponse> {
    if (this.#run === null) {
      return errorResponse(404, "device_manifest_not_found");
    }
    if (this.policyAdvanceOnFirstFinalize) {
      this.policyAdvanceOnFirstFinalize = false;
      return errorResponse(409, "device_manifest_policy_advanced");
    }
    const parsed = JSON.parse(typeof body === "string" ? body : "{}") as {
      final_digest?: string;
    };
    const retainedDigest = await computeManifestFinalDigest(
      this.#run.pages.map((page) => ({
        pageNumber: page.pageNumber,
        entryCount: page.entryCount,
        pageDigest: page.pageDigest,
      })),
    );
    if (parsed.final_digest !== undefined && parsed.final_digest !== retainedDigest) {
      return errorResponse(409, "device_manifest_digest_mismatch");
    }
    this.#run.actions = this.#planActions(this.#run.checkpointSequence, this.#run.entries);
    this.#lastActions = this.#run.actions;
    return jsonResponse(this.#runReceipt("planned"));
  }

  /**
   * The deterministic planner over the checkpoint-bounded canonical state:
   * an entry matching the server file's current fingerprint is a no_change;
   * a diverged entry is the local-diverged conflict; a deleted file's exact
   * last fingerprint applies its tombstone; an unknown locator uploads; and
   * every live server file absent from the whole capture plans the
   * canonical-only download at its checkpoint locator.
   */
  #planActions(
    checkpoint: number,
    entries: readonly {
      localEntryId: string;
      locator: string;
      fingerprint: FrozenFingerprint;
    }[],
  ): WireAction[] {
    const actions: WireAction[] = [];
    const coveredLocators = new Set(entries.map((entry) => entry.locator));
    let actionIndex = 0;
    for (const entry of entries) {
      const file = this.#fileStateAt(entry.locator, checkpoint);
      if (file === undefined) {
        actions.push({
          action_index: actionIndex++,
          action_kind: "upload",
          local_entry_id: entry.localEntryId,
          source_id: null,
          source_version_id: null,
          source_locator_id: null,
          source_tombstone_id: null,
          reason: null,
          checkpoint_locator: null,
        });
        continue;
      }
      const fingerprintMatches =
        file.fingerprint.sha256 === entry.fingerprint.sha256 &&
        file.fingerprint.sizeBytes === entry.fingerprint.sizeBytes;
      if (file.isDeleted) {
        actions.push({
          action_index: actionIndex++,
          action_kind: "apply_tombstone",
          local_entry_id: entry.localEntryId,
          source_id: file.sourceId,
          source_version_id: null,
          source_locator_id: null,
          source_tombstone_id: file.tombstoneId,
          reason: null,
          checkpoint_locator: null,
        });
        continue;
      }
      actions.push(
        fingerprintMatches
          ? {
              action_index: actionIndex++,
              action_kind: "no_change",
              local_entry_id: entry.localEntryId,
              source_id: file.sourceId,
              source_version_id: file.versionId,
              source_locator_id: null,
              source_tombstone_id: null,
              reason: null,
              checkpoint_locator: null,
            }
          : {
              action_index: actionIndex++,
              action_kind: "conflict",
              local_entry_id: entry.localEntryId,
              source_id: null,
              source_version_id: null,
              source_locator_id: null,
              source_tombstone_id: null,
              reason: "device_manifest_local_diverged",
              checkpoint_locator: null,
            },
      );
    }
    for (const [locator, file] of this.#files) {
      if (file.isDeleted || coveredLocators.has(locator)) {
        continue;
      }
      const bounded = this.#fileStateAt(locator, checkpoint);
      if (bounded === undefined) {
        continue;
      }
      actions.push({
        action_index: actionIndex++,
        action_kind: "download",
        local_entry_id: null,
        source_id: bounded.sourceId,
        source_version_id: bounded.versionId,
        source_locator_id: null,
        source_tombstone_id: null,
        reason: null,
        checkpoint_locator: locator,
      });
    }
    return actions;
  }

  plannedActions(): readonly WireAction[] {
    return this.#lastActions;
  }

  #serveActions(url: URL): DeviceSyncHttpResponse {
    if (this.#run === null) {
      return errorResponse(404, "device_manifest_not_found");
    }
    if (!this.#servedFirstActionPage) {
      this.#servedFirstActionPage = true;
      this.onFirstActionsRead?.();
    }
    // The first page carries no cursor: action index 0 belongs to it (an
    // exclusive "after" default of 0 would silently drop it).
    const afterParam = url.searchParams.get("after_action_index");
    const after = afterParam === null ? -1 : Number.parseInt(afterParam, 10);
    const limit = Number.parseInt(url.searchParams.get("limit") ?? "100", 10);
    const actions = this.#run.actions.filter((action) => action.action_index > after);
    const page = actions.slice(0, limit);
    return jsonResponse({
      manifest_run_id: this.#run.manifestRunId,
      actions: page,
      has_more: actions.length > page.length,
    });
  }

  #serveComplete(): DeviceSyncHttpResponse {
    if (this.#run === null) {
      return errorResponse(404, "device_manifest_not_found");
    }
    this.completions.push(this.#run.manifestRunId);
    const checkpoint = this.#run.checkpointSequence;
    this.acknowledgedSequence = Math.max(this.acknowledgedSequence, checkpoint);
    this.#run = null;
    return jsonResponse({
      acknowledged_sequence: checkpoint,
      delivered_through_sequence: Math.max(checkpoint, this.#nextSequence - 1),
    });
  }

  readonly downloadRequests: string[] = [];

  #serveDownload(versionId: string): DeviceSyncHttpResponse {
    this.downloadRequests.push(versionId);
    const version = this.#versions.get(versionId);
    if (version !== undefined) {
      const copy = version.bytes.slice();
      return {
        status: 200,
        bodyText: "",
        bodyBytes: copy.buffer.slice(
          copy.byteOffset,
          copy.byteOffset + copy.byteLength,
        ) as ArrayBuffer,
        headers: {
          "x-content-sha256": version.fingerprint.sha256,
          "content-length": String(version.bytes.byteLength),
          "content-type": version.fingerprint.mediaType,
          "x-request-id": uuidOf(`download${versionId}`),
        },
      };
    }
    return errorResponse(404, "device_event_unavailable");
  }

  /**
   * Model the server's run idle-expiry: the device's unfinished run
   * surpassed its idle deadline with no client activity and left the
   * per-device unfinished slot FREE (a later start then MINTS a fresh run
   * instead of resuming). The real server expires quietly after ~15 idle
   * minutes — the 2026-09-03 live defect's exact freeing moment.
   */
  expireOpenRun(): void {
    this.#run = null;
  }
}

function wireFingerprint(fingerprint: FrozenFingerprint): {
  sha256: string;
  size_bytes: number;
  media_type: string;
} {
  return {
    sha256: fingerprint.sha256,
    size_bytes: fingerprint.sizeBytes,
    media_type: fingerprint.mediaType,
  };
}

function jsonResponse(data: unknown): DeviceSyncHttpResponse {
  return {
    status: 200,
    bodyText: JSON.stringify({ data, error: null, request_id: null }),
    bodyBytes: null,
    headers: { "content-type": "application/json" },
  };
}

function errorResponse(status: number, code: string): DeviceSyncHttpResponse {
  return {
    status,
    bodyText: JSON.stringify({ data: null, error: { code }, request_id: null }),
    bodyBytes: null,
    headers: { "content-type": "application/json" },
  };
}

// --- the production plugin stack over the injected doubles ---------------------------------

interface PluginStack {
  readonly coordinator: SyncCoordinator;
  readonly reconciler: ReturnType<typeof createManifestReconciler>;
  readonly journalRepository: JournalRepository;
  readonly deviceSyncRepository: DeviceSyncRepository;
  readonly vault: InMemoryVault;
  readonly trail: RecordingTrail;
  readonly diagnostics: DeviceSyncDiagnostics;
  readonly outboundRequests: { count: number };
  readonly scheduler: FakeScheduler;
  readonly capture: JournalCapture;
  readonly api: ReturnType<typeof createDeviceSyncApi>;
}

function buildPluginStack(server: ScriptedServer): PluginStack {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  } satisfies JournalMeta);
  const journalRepository = new JournalRepository({ database });
  const deviceSyncRepository = journalRepository.deviceSync;
  const vault = new InMemoryVault();
  const trail = new RecordingTrail();
  const diagnostics = createDeviceSyncDiagnostics(trail);
  const capture = new JournalCapture({
    repository: journalRepository,
    vaultReader: vault,
    policyGate: allowedPolicyGate(),
    lifecycleCapture: silentLifecycleCapture(),
  });
  const manifestCapture = createManifestCapture({
    vaultReader: vault,
    identityReader: { readLocalFileByPath: (path) => journalRepository.readLocalFileByPath(path) },
  });
  const api = createDeviceSyncApi({
    transport: server.transport,
    resolveOrigin: () => "https://sync.example.test",
    getAccessToken: () => "at1.device-access-token-value",
    diagnostics,
  });
  const writer = new AtomicVaultWriterImpl({ repository: deviceSyncRepository, seam: vault });
  const applier = createRemoteEventApplier({
    repository: deviceSyncRepository,
    writer,
    downloader: (input) => api.downloadSourceVersion(input),
    diagnostics,
  });
  const journal = createManifestReconcilerJournal({ repository: journalRepository, capture });
  const reconciler = createManifestReconciler({
    repository: deviceSyncRepository,
    api,
    capture: manifestCapture,
    journal,
    applier,
    diagnostics,
    downloader: (input) => api.downloadSourceVersion(input),
  });
  const scheduler = new FakeScheduler();
  const outboundRequests = { count: 0 };
  const coordinator = createSyncCoordinator({
    repository: deviceSyncRepository,
    api,
    applier,
    reconciler,
    outbound: {
      request: async () => {
        outboundRequests.count += 1;
      },
    },
    diagnostics,
    nowEpochMs: () => scheduler.nowEpochMs,
    scheduler: (delayMs, callback) => scheduler.schedule(delayMs, callback),
    randomJitter: () => 0.5,
    isJournalReconcileRequired: () => false,
    readManifestActionProgress: () => journalRepository.readManifestActionProgress(),
    resolveOwnDeviceId: () => OWN_DEVICE_ID,
    outboundEvidence: {
      readCommittedOutboundRowByLocator: (normalizedLocator) =>
        journalRepository.readLocalFileByPath(normalizedLocator),
    },
    discardExpiredManifestRun: () => journal.discardActiveManifestRun(),
  });
  return {
    coordinator,
    reconciler,
    journalRepository,
    deviceSyncRepository,
    vault,
    trail,
    diagnostics,
    outboundRequests,
    scheduler,
    capture,
    api,
  };
}

// --- the journeys ---------------------------------------------------------------------------

const LOCATOR = "notes/journey-remote-edit.md";
const RENAMED_FROM = "notes/journey-lifecycle-from.md";
const RENAMED_TO = "notes/journey-lifecycle-to.md";
const DELETED_LOCATOR = "notes/journey-deleted.md";

describe("device-sync two-device journeys (production stack)", () => {
  it("applies a remote edit through the verified download and never echoes it back", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const originalBytes = bytesOf("device A wrote the original note");
    await server.commitCreate(LOCATOR, originalBytes);

    stack.coordinator.request("startup");
    await flushCycles();

    // The remote edit applied through the real pull, download and writer.
    expect(stack.vault.fileBytes(LOCATOR)).toEqual(originalBytes);
    expect(stack.deviceSyncRepository.readState().appliedSequence).toBe(1);
    expect(stack.deviceSyncRepository.readState().acknowledgedSequence).toBe(1);
    expect(stack.outboundRequests.count).toBe(1);

    // The reconciliation of the matching capture plans exactly no_change:
    // the received edit never echoes back as an upload.
    stack.coordinator.request("explicit_repair");
    await flushCycles();
    const state = stack.deviceSyncRepository.readState();
    expect(state.barrierGeneration).toBeNull();
    expect(state.activeManifestRunId).toBeNull();
    expect(stack.vault.fileBytes(LOCATOR)).toEqual(originalBytes);
    expect(server.plannedActions().map((action) => action.action_kind)).toEqual(["no_change"]);
    expect(server.completions).toHaveLength(1);
  });

  it("suppresses an exact self-origin echo by evidence while a foreign echo applies", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const locator = "notes/journey-self-origin.md";
    const ownBytes = bytesOf("this device wrote and uploaded this note");
    stack.vault.setFileBytes(locator, ownBytes);

    // The durable outbound evidence: an admitted repair upload whose
    // committed receipt binds the canonical source and version.
    const admission = await stack.capture.admitForRepair(locator);
    expect(admission?.outcome).toBe("event_recorded");
    if (admission === null || admission.outcome !== "event_recorded") {
      throw new Error("the repair admission must record an event");
    }
    const sourceId = uuidOf(`source${locator}`);
    const versionId = uuidOf(`version${locator}1`);
    await stack.journalRepository.recordCommittedReceipt({
      eventId: admission.event.eventId,
      sourceId,
      baseVersionId: versionId,
    });
    // The evidence row is now the exact committed proof the suppression
    // matches against (including the journal's frozen media type).
    const evidence = stack.journalRepository.readLocalFileByPath(locator);
    if (
      evidence === null ||
      evidence.sourceId !== sourceId ||
      evidence.baseVersionId !== versionId ||
      evidence.lastCommittedFingerprint === null
    ) {
      throw new Error("the committed evidence row is incomplete");
    }

    // Device A's edit of a DIFFERENT file applies normally, while the
    // server echoes our own committed upload back at us.
    const foreignLocator = "notes/journey-foreign-origin.md";
    const foreignBytes = bytesOf("device A wrote this one");
    await server.commitCreate(foreignLocator, foreignBytes);
    await server.commitSelfOriginEcho({
      locator,
      sourceId,
      versionId,
      fingerprint: evidence.lastCommittedFingerprint,
    });

    stack.coordinator.request("startup");
    await flushCycles();

    // Both events settled; our own echo never re-applied (the vault write
    // count stays at the one foreign apply's stage+replace cost) and the
    // cursor advanced over the suppressed echo.
    expect(stack.vault.fileBytes(foreignLocator)).toEqual(foreignBytes);
    expect(stack.vault.fileBytes(locator)).toEqual(ownBytes);
    expect(stack.vault.writeCount).toBe(2);
    expect(stack.deviceSyncRepository.readState().appliedSequence).toBe(2);
    expect(stack.deviceSyncRepository.readState().acknowledgedSequence).toBe(2);
  });

  it("applies lifecycle rename and delete events to the Vault", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const bytes = bytesOf("the lifecycle note");
    await server.commitCreate(RENAMED_FROM, bytes);
    await server.commitRename(RENAMED_FROM, RENAMED_TO);
    const deletedBytes = bytesOf("the deleted note");
    await server.commitCreate(DELETED_LOCATOR, deletedBytes);
    await server.commitDelete(DELETED_LOCATOR);

    stack.coordinator.request("startup");
    await flushCycles();

    // The rename moved the file; the delete trashed it.
    expect(stack.vault.has(RENAMED_FROM)).toBe(false);
    expect(stack.vault.fileBytes(RENAMED_TO)).toEqual(bytes);
    expect(stack.vault.has(DELETED_LOCATOR)).toBe(false);
    const state = stack.deviceSyncRepository.readState();
    expect(state.appliedSequence).toBe(4);
    expect(state.acknowledgedSequence).toBe(4);
    expect(stack.trail.rendered()).not.toContain("reconcile_failure");
    expect(stack.trail.rendered()).not.toContain("apply_failure");
  });

  it("keeps a concurrent commit after the checkpoint outside the plan and fence", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const locator = "notes/journey-race.md";
    const checkpointBytes = bytesOf("the checkpoint version");
    const concurrentBytes = bytesOf("the concurrent post-checkpoint version");
    await server.commitCreate(locator, checkpointBytes);
    stack.vault.setFileBytes(locator, checkpointBytes);

    // The run's checkpoint binds at the FIRST start; the concurrent commit
    // lands strictly after it and must stay outside the plan.
    server.onFirstStart = () => server.commitUpdate(locator, concurrentBytes);

    stack.coordinator.request("explicit_repair");
    await flushCycles();

    // The plan covered the matching capture only (no_change); the
    // post-checkpoint commit joined no action and the run completed.
    const kinds = server.plannedActions().map((action) => action.action_kind);
    expect(kinds).toEqual(["no_change"]);
    expect(server.completions).toHaveLength(1);
    expect(stack.deviceSyncRepository.readState().barrierGeneration).toBeNull();

    // The next pull delivers the concurrent commit and applies it.
    stack.coordinator.request("pull_interval");
    await flushCycles();
    expect(stack.vault.fileBytes(locator)).toEqual(concurrentBytes);
    const finalState = stack.deviceSyncRepository.readState();
    expect(finalState.appliedSequence).toBe(2);
    expect(finalState.acknowledgedSequence).toBe(2);
  });

  it("retries a lost acknowledgement before the next pull without a second apply", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const bytes = bytesOf("the lost-ack note");
    await server.commitCreate(LOCATOR, bytes);

    server.loseNextAcknowledgement = true;
    stack.coordinator.request("startup");
    await flushCycles();

    // The apply landed exactly once; the acknowledgement response was lost.
    expect(stack.vault.fileBytes(LOCATOR)).toEqual(bytes);
    const writesAfterApply = stack.vault.writeCount;
    expect(writesAfterApply).toBe(2);
    expect(server.acknowledgements).toHaveLength(1);
    const owedState = stack.deviceSyncRepository.readState();
    expect(owedState.appliedSequence).toBe(1);
    expect(owedState.acknowledgedSequence).toBe(0);
    expect(stack.trail.rendered()).toContain("cursor_failure");
    expect(stack.trail.rendered()).toContain("network_offline");

    // The retry backoff fires; the owed debt is retried BEFORE the next
    // pull with the exact same acknowledgement and no re-apply.
    stack.scheduler.advance(60_000);
    await flushCycles();
    expect(server.acknowledgements).toHaveLength(2);
    expect(server.acknowledgements[1]).toEqual({ expectedPrevious: 0, appliedThrough: 1 });
    expect(stack.vault.writeCount).toBe(writesAfterApply);
    const settledState = stack.deviceSyncRepository.readState();
    expect(settledState.appliedSequence).toBe(1);
    expect(settledState.acknowledgedSequence).toBe(1);
  });

  it("fails a cursor gap closed with the readable reason and repairs past it", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    // The device missed sequences 1-4; the server's first event is 5.
    server.deferSequences(4);
    const bytes = bytesOf("the gap note");
    await server.commitCreate(LOCATOR, bytes);

    stack.coordinator.request("startup");
    await flushCycles();

    expect(stack.trail.rendered()).toContain("apply_failure");
    expect(stack.trail.rendered()).toContain("device_cursor_gap");
    expect(stack.deviceSyncRepository.readState().appliedSequence).toBe(0);

    // The manifest repair converges past the gap: the canonical-only
    // download (the file never applied) places the verified bytes at the
    // checkpoint locator and the completion moves the cursors past the gap.
    stack.coordinator.request("explicit_repair");
    await flushCycles();
    const state = stack.deviceSyncRepository.readState();
    expect(state.barrierGeneration).toBeNull();
    // The completion fence moved BOTH cursors to the run checkpoint: the
    // manifest repair proved the universe through C, gap included.
    expect(state.appliedSequence).toBe(5);
    expect(state.acknowledgedSequence).toBe(5);
    expect(stack.vault.fileBytes(LOCATOR)).toEqual(bytes);
    expect(server.completions).toHaveLength(1);
  });

  it("repairs a cursor gap created inside delete-and-recreate reconciliation", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const peerLocator = "notes/journey-recreated-peer.md";
    await server.commitCreate(LOCATOR, bytesOf("committed bytes"));
    await server.commitCreate(peerLocator, bytesOf("peer committed bytes"));
    stack.coordinator.request("startup");
    await flushCycles();

    // The delete-and-recreate: the committed file is deleted locally and the
    // same locator returns with different bytes, while a peer file keeps its
    // own unsynced edit so the run's plan settles one closed local
    // divergence. The uploaded recreate is durably proven: the admitted
    // repair upload whose committed receipt binds the canonical source and
    // version (the exact committed evidence of the echo below).
    stack.vault.deleteFile(LOCATOR);
    stack.vault.setFileBytes(LOCATOR, bytesOf("recreated bytes"));
    stack.vault.setFileBytes(peerLocator, bytesOf("peer edited bytes"));
    const admission = await stack.capture.admitForRepair(LOCATOR);
    const recreateSourceId = uuidOf(`source${LOCATOR}`);
    const recreateVersionId = uuidOf(`version${LOCATOR}2`);
    if (admission === null || admission.outcome !== "event_recorded") {
      throw new Error("the recreate admission must record an event");
    }
    await stack.journalRepository.recordCommittedReceipt({
      eventId: admission.event.eventId,
      sourceId: recreateSourceId,
      baseVersionId: recreateVersionId,
    });
    const evidence = stack.journalRepository.readLocalFileByPath(LOCATOR);
    if (
      evidence === null ||
      evidence.sourceId !== recreateSourceId ||
      evidence.baseVersionId !== recreateVersionId ||
      evidence.lastCommittedFingerprint === null
    ) {
      throw new Error("the committed recreate evidence row is incomplete");
    }
    const recreateFingerprint = evidence.lastCommittedFingerprint;

    // Inside the reconciliation — after the run's checkpoint bound at the
    // settled cursor — the server defers one sequence number, our uploaded
    // recreate commits across the gap (the self-origin echo the committed
    // evidence proves) and the tombstone of our delete upload lands on the
    // canonical source. The run's checkpoint stays frozen at the cursor
    // while the planned tombstone needs the sequence past it.
    server.onFirstStart = async () => {
      server.deferSequences(1);
      await server.commitSelfOriginEcho({
        locator: LOCATOR,
        sourceId: recreateSourceId,
        versionId: recreateVersionId,
        fingerprint: recreateFingerprint,
      });
      await server.commitDelete(LOCATOR);
    };

    stack.coordinator.request("explicit_repair");
    await flushCycles();
    expect(stack.deviceSyncRepository.readState().barrierReason).toBe("device_cursor_gap");
    expect(server.plannedActions().map((action) => action.action_kind)).toEqual(["conflict", "apply_tombstone"]);

    stack.coordinator.request("explicit_repair");
    await flushCycles();
    expect(stack.deviceSyncRepository.readState().barrierGeneration).toBeNull();

    // Convergence and idempotency: after the second repair no run stays
    // active and the cursors sit equal. One more explicit repair must stay
    // converged — the barrier never re-raises, no run is left open and
    // neither cursor regresses. A clean-state explicit repair always runs
    // one full reconcile that completes exactly one server run (the first
    // journey pins that shape), so the count grows by that one clean run
    // and never by a second stale-checkpoint close.
    const state = stack.deviceSyncRepository.readState();
    expect(state.activeManifestRunId).toBeNull();
    expect(state.appliedSequence).toBe(state.acknowledgedSequence);
    const completions = server.completions.length;
    stack.coordinator.request("explicit_repair");
    await flushCycles();
    const reconvergedState = stack.deviceSyncRepository.readState();
    expect(reconvergedState.barrierGeneration).toBeNull();
    expect(reconvergedState.activeManifestRunId).toBeNull();
    expect(reconvergedState.appliedSequence).toBe(reconvergedState.acknowledgedSequence);
    expect(reconvergedState.appliedSequence).toBe(state.appliedSequence);
    expect(server.completions).toHaveLength(completions + 1);
    // The converged retry's whole plan re-observes only the peer's preserved
    // closed divergence: no new conflict/repair action appears for the
    // recreated locator, whose tombstone settled and left the capture.
    expect(server.plannedActions().map((action) => action.action_kind)).toEqual(["conflict"]);
  });

  it("sheds a stale open-run binding after the server idle-expired it instead of stranding the fresh run", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const locatorA = "notes/journey-idle-a.md";
    const locatorB = "notes/journey-idle-b.md";
    await server.commitCreate(locatorA, bytesOf("idle a bytes"));
    await server.commitCreate(locatorB, bytesOf("idle b bytes"));
    stack.coordinator.request("startup");
    await flushCycles();
    // The pull delivered and acknowledged both events: the apply lattice
    // sits at the checkpoint a later run would freeze at.
    const converged = stack.deviceSyncRepository.readState();
    expect(converged.appliedSequence).toBe(2);
    expect(converged.acknowledgedSequence).toBe(2);

    // The vault drops both files (an ordinary local deletion): the manifest
    // now covers nothing while canonical still holds both sources, so the
    // next run plans two canonical-only downloads whose synthetic events
    // (applied+1 = 3) can never fit the quiet checkpoint (2).
    stack.vault.deleteFile(locatorA);
    stack.vault.deleteFile(locatorB);

    stack.coordinator.request("explicit_repair");
    await flushCycles();
    // The stuck shape of the 2026-09-03 live defect: the run rests open and
    // bound under a repair barrier, and the repair lane never auto-retries
    // it — the pending download's synthetic event can never fit the quiet
    // checkpoint.
    const stuck = stack.deviceSyncRepository.readState();
    expect(stuck.activeManifestRunId).not.toBeNull();
    expect(stuck.appliedSequence).toBe(2);
    const completionsWhileStuck = server.completions.length;

    // The server's run idle deadline frees the per-device slot (no client
    // activity can reach the wedged run). The next explicit repair must
    // shed the stale local binding and drive the fresh server run to a
    // closed state — never mint-and-refuse leaving an abandoned run.
    server.expireOpenRun();
    stack.coordinator.request("explicit_repair");
    await flushCycles();

    const repaired = stack.deviceSyncRepository.readState();
    // The repair converged through the canonical fence: the barrier and
    // the open-run binding are cleared, the cursors sit equal, and the
    // fresh server run COMPLETED (never abandoned `collecting`).
    expect(repaired.barrierGeneration).toBeNull();
    expect(repaired.activeManifestRunId).toBeNull();
    expect(repaired.appliedSequence).toBe(repaired.acknowledgedSequence);
    expect(server.completions.length).toBeGreaterThan(completionsWhileStuck);
    // The honest cursor-gap verdict stays readable on the trail; the
    // unreadable start-stage state_invalid flip never appears.
    expect(stack.trail.rendered()).not.toContain("start · device_manifest_state_invalid");
    expect(stack.trail.rendered()).toContain("device_cursor_gap");
  });

  it("invalidates a server run whose retained pages the fresh capture contradicts instead of wedging on digest mismatch", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const locator = "notes/journey-asymmetry.md";
    const originalBytes = bytesOf("asymmetry original bytes");
    await server.commitCreate(locator, originalBytes);
    stack.coordinator.request("startup");
    await flushCycles();
    expect(stack.vault.fileBytes(locator)).toEqual(originalBytes);

    // A repair run appends its page and finalizes, then its action reads
    // fail transiently — the run rests planned and bound with the
    // server's RETAINED page digest.
    server.failActionsRead = true;
    stack.coordinator.request("explicit_repair");
    await flushCycles();

    // The vault's file changes between the run's append and its resume:
    // the fresh capture now contradicts the retained page.
    stack.vault.setFileBytes(locator, bytesOf("asymmetry edited bytes"));
    server.failActionsRead = false;

    stack.coordinator.request("explicit_repair");
    await flushCycles();

    // DESIRED: the contradicted server run is invalidated (the restart
    // carries a new observation generation, which the server answers by
    // expiring the stale run) and a truly fresh run converges — the
    // edited entry's upload plan lands and the fence clears. TODAY: the
    // same-generation restart RESUMES the contradicted run, the fresh
    // capture's finalize rejects (device_manifest_digest_mismatch) and
    // the repair wedges under a growing barrier.
    const state = stack.deviceSyncRepository.readState();
    expect(state.barrierGeneration).toBeNull();
    expect(state.activeManifestRunId).toBeNull();
    expect(state.appliedSequence).toBe(state.acknowledgedSequence);
    expect(server.completions.length).toBeGreaterThan(0);
  });

  it("completes a manifest run past a repeatedly refused vault write instead of holding every other placement hostage", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const lockedLocator = "notes/journey-locked.md";
    const hostageLocator = "notes/journey-hostage.md";
    const lockedBytes = bytesOf("the locked note device A committed");
    const hostageBytes = bytesOf("the hostage note device A committed");
    await server.commitCreate(lockedLocator, lockedBytes);
    await server.commitCreate(hostageLocator, hostageBytes);

    // The locked file refuses every write shape of its atomic apply — the
    // staged hidden sibling included (the live wedge's `verify_temp`
    // refusal): the durable row rests `prepared` and no mutation ever ran.
    stack.vault.failWritesForBasenames.add("journey-locked.md");

    stack.coordinator.request("explicit_repair");
    await flushCycles();
    // The first refusal moved the run onto the retry backoff; firing it
    // runs the SECOND pass of the same bound run — the durable `received`
    // action progress row is the prior-attempt evidence.
    stack.scheduler.advance(60_000);
    await flushCycles();

    // DESIRED: the refused placement settles with its closed reason and
    // the run COMPLETES — the hostage placement delivers, the locked file
    // stays absent, the trail keeps the closed vault-failure reason
    // readable, and the cursors converge through the canonical fence.
    // TODAY: the cycle-start recovery of the leftover `prepared` row
    // abandons it and then dies refusing to mint a barrier the active run
    // already occupies, so the repair never resumes and everything parks.
    const state = stack.deviceSyncRepository.readState();
    expect(state.barrierGeneration).toBeNull();
    expect(state.activeManifestRunId).toBeNull();
    expect(state.appliedSequence).toBe(state.acknowledgedSequence);
    expect(stack.vault.fileBytes(hostageLocator)).toEqual(hostageBytes);
    expect(stack.vault.has(lockedLocator)).toBe(false);
    expect(stack.trail.rendered()).toContain("device_apply_vault_failed");
    expect(server.completions.length).toBeGreaterThan(0);

    // The lock clears and the parked placement re-converges through the
    // next explicit repair. One deferred canonical sequence gives the
    // fresh run's checkpoint room past the completion-settled cursor — a
    // perfectly quiet timeline cannot (the sheds-stale-binding journey
    // pins that standing fence property), so the journey models the
    // timeline moving ahead by one sequence this device owes nothing for.
    stack.vault.failWritesForBasenames.delete("journey-locked.md");
    server.deferSequences(1);
    stack.coordinator.request("explicit_repair");
    await flushCycles();

    expect(stack.vault.fileBytes(lockedLocator)).toEqual(lockedBytes);
    const converged = stack.deviceSyncRepository.readState();
    expect(converged.barrierGeneration).toBeNull();
    expect(converged.activeManifestRunId).toBeNull();
    expect(converged.appliedSequence).toBe(converged.acknowledgedSequence);
  });

  it("restarts exactly one fresh checkpoint-bound run after a policy advance", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    const bytes = bytesOf("the policy-advance note");
    await server.commitCreate(LOCATOR, bytes);
    stack.vault.setFileBytes(LOCATOR, bytes);
    server.policyAdvanceOnFirstFinalize = true;

    stack.coordinator.request("explicit_repair");
    await flushCycles();

    expect(server.starts).toHaveLength(2);
    expect(server.completions).toHaveLength(1);
    expect(stack.trail.rendered()).toContain("device_manifest_policy_advanced");
    const state = stack.deviceSyncRepository.readState();
    expect(state.barrierGeneration).toBeNull();
    expect(state.activeManifestRunId).toBeNull();
    expect(state.acknowledgedSequence).toBe(1);
  });

  it("rebinds after SQLite loss without planning a duplicate upload", async () => {
    const server = new ScriptedServer();
    const first = buildPluginStack(server);
    const bytes = bytesOf("the sqlite-loss note");
    await server.commitCreate(LOCATOR, bytes);
    first.coordinator.request("startup");
    await flushCycles();
    expect(first.vault.fileBytes(LOCATOR)).toEqual(bytes);

    // SQLite loss: the whole journal is rebuilt empty; the Vault keeps the
    // file; the server keeps the canonical source.
    const rebuilt = buildPluginStack(server);
    rebuilt.vault.setFileBytes(LOCATOR, bytes);

    const outcome = await rebuilt.reconciler.reconcile("sqlite_rebuilt");
    expect(outcome.kind).toBe("completed");

    // The capture rebound to the SAME canonical source: the plan carried a
    // no_change bound to that source and no upload for the file.
    const kinds = server.plannedActions().map((action) => action.action_kind);
    expect(kinds).toEqual(["no_change"]);
    expect(server.plannedActions()[0]?.source_id).toBe(uuidOf(`source${LOCATOR}`));
    expect(server.completions).toHaveLength(1);
    const state = rebuilt.deviceSyncRepository.readState();
    expect(state.barrierGeneration).toBeNull();
    expect(state.appliedSequence).toBe(1);
  });

  it("preserves a local edit made during the reconciliation as the closed conflict", async () => {
    const server = new ScriptedServer();
    const stack = buildPluginStack(server);
    // A local-only file the server never saw: the capture freezes it and
    // the plan is its upload.
    const localBytes = bytesOf("the local note the server never saw");
    stack.vault.setFileBytes(LOCATOR, localBytes);
    // One server file keeps the run's checkpoint above zero.
    await server.commitCreate("notes/journey-edit-during-server.md", bytesOf("server side"));

    // The edit during the reconciliation: the local file is deleted right
    // before the first action page is served — inside the same run.
    server.onFirstActionsRead = () => {
      stack.vault.deleteFile(LOCATOR);
    };

    const outcome = await stack.reconciler.reconcile("explicit_repair");
    expect(outcome.kind).toBe("completed");
    await flushCycles(2);

    // The deletion survives (no upload of vanished bytes); the stale
    // upload settled as the closed action-stale reason with exactly one
    // readable observation, and the run still completed and converged.
    expect(stack.vault.has(LOCATOR)).toBe(false);
    expect(stack.trail.rendered()).toContain("device_manifest_action_stale");
    const state = stack.deviceSyncRepository.readState();
    expect(state.barrierGeneration).toBeNull();
    expect(state.acknowledgedSequence).toBe(1);
  });
});
