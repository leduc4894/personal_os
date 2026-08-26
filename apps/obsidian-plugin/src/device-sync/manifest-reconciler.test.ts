/**
 * Tests of the manifest action reconciliation (device cursor and manifest
 * reconciliation, task 11, spec 12.1, 12.4, 7.3).
 *
 * Every action rechecks the current path/fingerprint, the occupied target,
 * the policy revision, the restore reservation and any newer local journal
 * event BEFORE it applies: a stale action becomes a durable conflict while
 * unrelated safe actions stay valid. Upload actions terminalize by durably
 * recording or reauthorizing an outbound journal event under the barrier;
 * once every action is terminal-safe the exact server completion lands, the
 * local cursors become the checkpoint C, the barrier and the
 * `reconcile_required` flag clear, and rows observed after G plus the
 * planner uploads become dispatchable again. A one-hour expiry or a policy
 * advance discards only the temporary run progress and starts a new
 * checkpoint-bound run. Every closed reconcile failure surfaces exactly one
 * `reconcile_failure` observation with its exact stage token.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { CapturePolicySubject } from "../exclusion-policy/policy-session";
import { JournalCapture } from "../journal/capture";
import type { CapturePolicyGate } from "../journal/capture";
import type { FrozenFingerprint, JournalMeta } from "../journal/contracts";
import { MAX_PENDING_EVENTS } from "../journal/contracts";
import { JournalPersistence } from "../journal/persistence";
import type { JournalFileStore } from "../journal/persistence";
import { JournalRepository } from "../journal/repository";
import type { ManifestActionProgressRecord } from "../journal/repository";
import {
  JOURNAL_SCHEMA_VERSION,
  SqliteDatabase,
  journalStoreError,
} from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import type { LifecycleCapture } from "../journal/lifecycle-capture";
import { AtomicVaultWriterImpl } from "./atomic-vault-writer";
import type { VaultMutationSeam } from "./atomic-vault-writer";
import { DeviceSyncApiError } from "./api";
import type {
  AppendManifestPageInput,
  CompleteManifestInput,
  DeviceCursorReceipt,
  FinalizeManifestInput,
  ManifestAction,
  ManifestActionPage,
  ManifestActionsInput,
  ManifestPageReceipt,
  ManifestRunReceipt,
  StartManifestInput,
  VerifiedDownload,
} from "./api";
import type {
  DeviceSyncDiagnostics,
  DeviceSyncRepository as DeviceSyncRepositoryPort,
  DeviceSyncState,
  ReconcileFailureStage,
} from "./contracts";
import {
    MAX_MANIFEST_TOTAL_ENTRIES,
    computeManifestFinalDigest,
    createManifestCapture,
  } from "./manifest-capture";
import {
  RECONCILE_BARRIER_REASONS,
  createManifestReconciler,
  createManifestReconcilerJournal,
} from "./manifest-reconciler";
import type {
  ManifestReconciler,
  ManifestReconcilerJournal,
  ManifestReconcilerWireApi,
} from "./manifest-reconciler";
import { createRemoteEventApplier } from "./remote-event-applier";

/** The real sql.js WebAssembly engine drives every reconciler test (spec 6.1). */
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

const RUN_ID = "018f47a0-7b00-7000-8000-0000000000a1";
const SECOND_RUN_ID = "018f47a0-7b00-7000-8000-0000000000a2";
const SOURCE_ID = "99999999-9999-4999-8999-999999999999";
const SOURCE_VERSION_ID = "77777777-7777-4777-8777-777777777777";
const CHECKPOINT_SEQUENCE = 5;
const SECOND_CHECKPOINT_SEQUENCE = 9;
const STALE_BYTES = new TextEncoder().encode("stale local bytes");
const FRESH_BYTES = new TextEncoder().encode("fresh remote bytes");

function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

async function fingerprintOf(bytes: Uint8Array): Promise<FrozenFingerprint> {
  return { sha256: await sha256Hex(bytes), sizeBytes: bytes.byteLength, mediaType: "text/plain" };
}

async function entryIdOfLocator(locator: string): Promise<string> {
  return "me1-" + (await sha256Hex(new TextEncoder().encode(`manifest-entry/v1:${locator}`)));
}

// --- the fakes ---------------------------------------------------------------------------------------

/** The fake Vault seam: regular files plus a count of every mutation. */
class FakeReconcilerVault implements VaultMutationSeam {
  readonly #files = new Map<string, Uint8Array>();
  writeCount = 0;

  setFileBytes(locator: string, bytes: Uint8Array): void {
    this.#files.set(locator, bytes);
  }

  fileBytes(locator: string): Uint8Array | null {
    return this.#files.get(locator) ?? null;
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
    this.writeCount += 1;
    this.#files.set(locator, bytes);
  }

  async renameLocator(fromLocator: string, toLocator: string): Promise<void> {
    this.writeCount += 1;
    const bytes = this.#files.get(fromLocator);
    if (bytes === undefined) {
      throw new Error("seam cannot rename absent file");
    }
    this.#files.delete(fromLocator);
    this.#files.set(toLocator, bytes);
  }

  async trashLocator(locator: string): Promise<void> {
    this.writeCount += 1;
    if (!this.#files.delete(locator)) {
      throw new Error("seam cannot trash absent file");
    }
  }
}

class RecordingDiagnostics implements DeviceSyncDiagnostics {
  readonly reconcileFailures: { readonly stage: string; readonly reason: string }[] = [];
  readonly others: string[] = [];

  reconcileFailure(stage: ReconcileFailureStage, reason: string): void {
    this.reconcileFailures.push({ stage, reason });
  }

  applyFailure(stage: string, reason: string): void {
    this.others.push(`apply:${stage}:${reason}`);
  }

  cursorFailure(stage: string, reason: string): void {
    this.others.push(`cursor:${stage}:${reason}`);
  }

  credentialFailure(stage: string, reason: string): void {
    this.others.push(`credential:${stage}:${reason}`);
  }
}

function allowedPolicyGate(): CapturePolicyGate {
  return {
    evaluateForCapture() {
      return { decision: { raw: "allowed", enforced: "allowed" }, revisionNumber: 1 };
    },
  };
}

function locatorExcludingPolicyGate(excludedLocator: string): CapturePolicyGate {
  return {
    evaluateForCapture(subject: CapturePolicySubject) {
      if (subject.normalizedLocator === excludedLocator) {
        return { decision: { raw: "excluded", enforced: "excluded" }, revisionNumber: 2 };
      }
      return { decision: { raw: "allowed", enforced: "allowed" }, revisionNumber: 2 };
    },
  };
}

function silentLifecycleCapture(): LifecycleCapture {
  return {
    captureRename: async () => null,
    captureDelete: async () => null,
    requestRestore: async () => {
      throw new Error("not used in reconciler tests");
    },
  };
}

interface ScriptedFailure {
  readonly stage: ReconcileFailureStage;
  readonly error: DeviceSyncApiError;
  /** Fail on the Nth call of the stage (1-based); 1 by default. */
  readonly occurrence: number;
}

interface ScriptedRun {
  readonly manifestRunId: string;
  readonly checkpointSequence: number;
}

/** The scripted manifest wire API: exact receipts, recordable failures. */
class FakeManifestApi implements ManifestReconcilerWireApi {
  readonly starts: StartManifestInput[] = [];
  readonly appendedPages: AppendManifestPageInput[] = [];
  readonly finalizations: FinalizeManifestInput[] = [];
  readonly actionReads: ManifestActionsInput[] = [];
  readonly completions: CompleteManifestInput[] = [];
  plan: readonly ManifestAction[] = [];
  /** Hook awaited at each run start — observes state between runs. */
  onStart: ((startCount: number) => Promise<void> | void) | null = null;
  /** Hook awaited at each action-page read — models edits after the capture. */
  onActionRead: (() => Promise<void> | void) | null = null;
  /**
   * Hook awaited at the completion call — the last moment before the
   * journal's completion discards the temporary page/action progress.
   */
  onComplete: (() => Promise<void> | void) | null = null;
  #failure: ScriptedFailure | null = null;
  #failureStageCalls = new Map<ReconcileFailureStage, number>();
  #runs: readonly ScriptedRun[] = [{ manifestRunId: RUN_ID, checkpointSequence: CHECKPOINT_SEQUENCE }];
  /** The next expected page number per run id — one unfinished run resumes exactly. */
  readonly #nextPageByRun = new Map<string, number>();

  scriptRuns(runs: readonly ScriptedRun[]): void {
    this.#runs = runs;
  }

  failOnceAt(
    stage: ReconcileFailureStage,
    error: DeviceSyncApiError,
    occurrence = 1,
  ): void {
    this.#failure = { stage, error, occurrence };
  }

  #shouldFail(stage: ReconcileFailureStage): DeviceSyncApiError | null {
    const failure = this.#failure;
    if (failure === null || failure.stage !== stage) {
      return null;
    }
    const calls = (this.#failureStageCalls.get(stage) ?? 0) + 1;
    this.#failureStageCalls.set(stage, calls);
    if (calls !== failure.occurrence) {
      return null;
    }
    this.#failure = null;
    return failure.error;
  }

  #run(): ScriptedRun {
    const index = Math.min(this.starts.length, this.#runs.length) - 1;
    return this.#runs[index] ?? { manifestRunId: RUN_ID, checkpointSequence: CHECKPOINT_SEQUENCE };
  }

  async startManifest(input: StartManifestInput): Promise<ManifestRunReceipt> {
    const failure = this.#shouldFail("start");
    if (failure !== null) {
      throw failure;
    }
    this.starts.push(input);
    await this.onStart?.(this.starts.length);
    const run = this.#run();
    return {
      manifestRunId: run.manifestRunId,
      state: "collecting",
      baseAcknowledgedSequence: 0,
      checkpointSequence: run.checkpointSequence,
      policyRevisionNumber: 1,
      clientObservationGeneration: input.clientObservationGeneration,
      nextPageNumber: this.#nextPageByRun.get(run.manifestRunId) ?? 0,
      entryCount: 0,
      expiresAt: "2026-08-26T01:00:00Z",
    };
  }

  async appendManifestPage(input: AppendManifestPageInput): Promise<ManifestPageReceipt> {
    const failure = this.#shouldFail("page");
    if (failure !== null) {
      throw failure;
    }
    const expectedPageNumber = this.#nextPageByRun.get(input.manifestRunId) ?? 0;
    if (
      input.manifestRunId !== this.#run().manifestRunId ||
      input.pageNumber > expectedPageNumber
    ) {
      throw new DeviceSyncApiError("device_manifest_page_invalid", false);
    }
    this.appendedPages.push(input);
    this.#nextPageByRun.set(input.manifestRunId, expectedPageNumber + 1);
    return {
      manifestRunId: input.manifestRunId,
      pageNumber: input.pageNumber,
      acceptedEntryCount: input.entries.length,
      nextPageNumber: input.pageNumber + 1,
    };
  }

  async finalizeManifest(input: FinalizeManifestInput): Promise<ManifestRunReceipt> {
    const failure = this.#shouldFail("finalize");
    if (failure !== null) {
      throw failure;
    }
    this.finalizations.push(input);
    const run = this.#run();
    return {
      manifestRunId: input.manifestRunId,
      state: "planned",
      baseAcknowledgedSequence: 0,
      checkpointSequence: run.checkpointSequence,
      policyRevisionNumber: 1,
      clientObservationGeneration: this.starts[0]?.clientObservationGeneration ?? 0,
      nextPageNumber: this.#nextPageByRun.get(input.manifestRunId) ?? 0,
      entryCount: input.totalEntryCount,
      expiresAt: "2026-08-26T01:00:00Z",
    };
  }

  async listManifestActions(input: ManifestActionsInput): Promise<ManifestActionPage> {
    const failure = this.#shouldFail("actions");
    if (failure !== null) {
      throw failure;
    }
    this.actionReads.push(input);
    await this.onActionRead?.();
    return { manifestRunId: input.manifestRunId, actions: this.plan, hasMore: false };
  }

  async completeManifest(input: CompleteManifestInput): Promise<DeviceCursorReceipt> {
    const failure = this.#shouldFail("complete");
    if (failure !== null) {
      throw failure;
    }
    await this.onComplete?.();
    this.completions.push(input);
    const checkpoint = this.#run().checkpointSequence;
    return {
      acknowledgedSequence: checkpoint,
      deliveredThroughSequence: checkpoint,
    };
  }
}

// --- the harness --------------------------------------------------------------------------------------

interface ReconcilerHarness {
  readonly reconciler: ManifestReconciler;
  readonly journalRepository: JournalRepository;
  readonly deviceSync: DeviceSyncRepositoryPort;
  readonly database: SqliteDatabase;
  readonly vault: FakeReconcilerVault;
  readonly api: FakeManifestApi;
  readonly diagnostics: RecordingDiagnostics;
  readonly journal: ManifestReconcilerJournal;
}

interface HarnessOptions {
  readonly files?: readonly { readonly locator: string; readonly bytes: Uint8Array }[];
  readonly policyGate?: CapturePolicyGate;
  readonly onDeviceSyncRepairComplete?: () => void;
  readonly database?: SqliteDatabase;
  /** Test-only page bound: keeps multi-page scenarios small (default 2). */
  readonly entriesPerPage?: number;
}

function createReconcilerHarness(options: HarnessOptions = {}): ReconcilerHarness {
  const database =
    options.database ??
    SqliteDatabase.createEmpty(engineModule, {
      schemaVersion: JOURNAL_SCHEMA_VERSION,
      dirtyGeneration: 1,
      lastVerifiedGeneration: 1,
      isReconcileRequired: false,
      recoveryState: "verified_generation_loaded",
    } satisfies JournalMeta);
  const journalRepository = new JournalRepository({
    database,
    ...(options.onDeviceSyncRepairComplete === undefined
      ? {}
      : { onDeviceSyncRepairComplete: options.onDeviceSyncRepairComplete }),
  });
  const deviceSync = journalRepository.deviceSync;
  const vault = new FakeReconcilerVault();
  for (const seed of options.files ?? []) {
    vault.setFileBytes(seed.locator, seed.bytes);
  }
  const gate = options.policyGate ?? allowedPolicyGate();
  const captureCoordinator = new JournalCapture({
    repository: journalRepository,
    vaultReader: vault,
    policyGate: gate,
    lifecycleCapture: silentLifecycleCapture(),
  });
  const manifestCapture = createManifestCapture({
    vaultReader: vault,
    identityReader: { readLocalFileByPath: (path) => journalRepository.readLocalFileByPath(path) },
    entriesPerPage: options.entriesPerPage ?? 2,
  });
  const diagnostics = new RecordingDiagnostics();
  const verifiedFreshDownload = async (): Promise<VerifiedDownload> => ({
    bytes: FRESH_BYTES,
    declaredSha256: await sha256Hex(FRESH_BYTES),
    sizeBytes: FRESH_BYTES.byteLength,
    mediaType: "text/plain",
  });
  const writer = new AtomicVaultWriterImpl({ repository: deviceSync, seam: vault });
  const applier = createRemoteEventApplier({
    repository: deviceSync,
    writer,
    downloader: verifiedFreshDownload,
    diagnostics,
  });
  const journal = createManifestReconcilerJournal({
    repository: journalRepository,
    capture: captureCoordinator,
  });
  const api = new FakeManifestApi();
  const reconciler = createManifestReconciler({
    repository: deviceSync,
    api,
    capture: manifestCapture,
    journal,
    applier,
    diagnostics,
    downloader: verifiedFreshDownload,
  });
  return { reconciler, journalRepository, deviceSync, database, vault, api, diagnostics, journal };
}

function downloadAction(localEntryId: string, actionIndex = 0): ManifestAction {
  return {
    actionIndex,
    actionKind: "download",
    localEntryId,
    sourceId: SOURCE_ID,
    sourceVersionId: SOURCE_VERSION_ID,
    sourceLocatorId: null,
    sourceTombstoneId: null,
    reason: null,
  };
}

/**
 * Snapshot the durable action progress at the completion call — the last
 * moment before the journal's completion legitimately discards the run's
 * temporary progress rows (spec 7.3).
 */
function captureActionProgressAtCompletion(
  harness: ReconcilerHarness,
): { readonly read: () => readonly ManifestActionProgressRecord[] } {
  let snapshot: readonly ManifestActionProgressRecord[] = [];
  harness.api.onComplete = () => {
    snapshot = harness.journal.readManifestActionProgress();
  };
  return { read: () => snapshot };
}

// --- the happy path -----------------------------------------------------------------------------------

describe("ManifestReconciler happy path (spec 12.1, 12.4)", () => {
  it("completes a run at the exact checkpoint and clears the barrier and flag", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/synced.md", bytes: STALE_BYTES }],
    });
    const localEntryId = await entryIdOfLocator("notes/synced.md");
    harness.api.plan = [
      {
        actionIndex: 0,
        actionKind: "no_change",
        localEntryId,
        sourceId: SOURCE_ID,
        sourceVersionId: SOURCE_VERSION_ID,
        sourceLocatorId: null,
        sourceTombstoneId: null,
        reason: null,
      },
    ];

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome).toEqual({ kind: "completed", checkpointSequence: CHECKPOINT_SEQUENCE });
    const state = harness.deviceSync.readState();
    expect(state.appliedSequence).toBe(CHECKPOINT_SEQUENCE);
    expect(state.acknowledgedSequence).toBe(CHECKPOINT_SEQUENCE);
    expect(state.barrierGeneration).toBeNull();
    expect(state.barrierReason).toBeNull();
    expect(state.activeManifestRunId).toBeNull();
    expect(harness.database.readJournalMeta().isReconcileRequired).toBe(false);
    // The exact server completion recorded the run's final digest.
    expect(harness.api.completions).toHaveLength(1);
    expect(harness.api.completions[0]?.manifestRunId).toBe(RUN_ID);
    expect(harness.api.finalizations[0]?.finalDigest).toBe(
      await computeManifestFinalDigest(
        harness.api.appendedPages.map((page) => ({
          pageNumber: page.pageNumber,
          entryCount: page.entries.length,
          pageDigest: page.pageDigest,
        })),
      ),
    );
    expect(harness.diagnostics.reconcileFailures).toEqual([]);
  });

  it("starts a barrier at the current generation and binds the run checkpoint", async () => {
    const harness = createReconcilerHarness();
    let observedBarrier: { generation: number; reason: string } | null = null;
    harness.api.onActionRead = () => {
      const state = harness.deviceSync.readState();
      observedBarrier = {
        generation: state.barrierGeneration ?? -1,
        reason: state.barrierReason ?? "",
      };
    };

    await harness.reconciler.reconcile("cursor_gap");

    const state = harness.deviceSync.readState();
    expect(state.observationGeneration).toBe(1);
    expect(observedBarrier).toEqual({ generation: 1, reason: "device_cursor_gap" });
  });

  it("adopts an existing barrier instead of minting a new generation", async () => {
    const harness = createReconcilerHarness();
    for (let index = 0; index < 14; index += 1) {
      await harness.deviceSync.nextObservationGeneration();
    }
    await harness.deviceSync.startRepairBarrier({ generation: 14, reason: "device_cursor_gap" });
    let observedReason: string | null = null;
    harness.api.onActionRead = () => {
      observedReason = harness.deviceSync.readState().barrierReason;
    };

    const outcome = await harness.reconciler.reconcile("local_invariant");

    expect(outcome).toEqual({ kind: "completed", checkpointSequence: CHECKPOINT_SEQUENCE });
    expect(harness.api.starts[0]?.clientObservationGeneration).toBe(14);
    expect(observedReason).toBe("device_cursor_gap");
    expect(harness.deviceSync.readState().observationGeneration).toBe(14);
  });

  it("resumes exactly from the durable page progress after a retryable page failure", async () => {
    const files = [1, 2, 3].map((index) => ({
      locator: `notes/file-${index}.md`,
      bytes: bytesOf(`content ${index}`),
    }));
    const harness = createReconcilerHarness({ files, entriesPerPage: 1 });
    harness.api.failOnceAt(
      "page",
      new DeviceSyncApiError("network_offline", true, null, null),
      2,
    );

    const first = await harness.reconciler.reconcile("explicit_repair");
    expect(first).toEqual({ kind: "retry", reason: "network_offline" });
    expect(harness.api.appendedPages.map((page) => page.pageNumber)).toEqual([0]);
    const interrupted = harness.deviceSync.readState();
    expect(interrupted.activeManifestRunId).toBe(RUN_ID);
    expect(interrupted.barrierGeneration).not.toBeNull();

    const second = await harness.reconciler.resume();
    expect(second).toEqual({ kind: "completed", checkpointSequence: CHECKPOINT_SEQUENCE });
    // Only the missing pages were re-sent: page 0 was already durable.
    expect(harness.api.appendedPages.map((page) => page.pageNumber)).toEqual([0, 1, 2]);
  });
});

// --- action rechecks (spec 12.4, step 2) --------------------------------------------------------------

describe("ManifestReconciler action rechecks (spec 12.4)", () => {
  it("preserves an edit observed after the barrier and never writes the vault", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/edit.md", bytes: STALE_BYTES }],
    });
    for (let index = 0; index < 14; index += 1) {
      await harness.deviceSync.nextObservationGeneration();
    }
    await harness.deviceSync.startRepairBarrier({
      generation: 14,
      reason: "device_cursor_gap",
    });
    const localEntryId = await entryIdOfLocator("notes/edit.md");
    harness.api.plan = [downloadAction(localEntryId)];
    const progressAtCompletion = captureActionProgressAtCompletion(harness);
    // The watcher admits the newer local edit exactly when the planner's
    // action page is read: the action is now stale against the journal.
    let editAdmitted = false;
    harness.api.onActionRead = async () => {
      if (editAdmitted) {
        return;
      }
      editAdmitted = true;
      await harness.journalRepository.recordCapture({
        normalizedPath: "notes/edit.md",
        fingerprint: await fingerprintOf(FRESH_BYTES),
        policyRevisionNumber: 1,
        admission: "policy_allowed",
      });
      harness.vault.setFileBytes("notes/edit.md", FRESH_BYTES);
    };

    const outcome = await harness.reconciler.resume();

    expect(outcome).toEqual({ kind: "completed", checkpointSequence: CHECKPOINT_SEQUENCE });
    expect(harness.vault.writeCount).toBe(0);
    expect(
      new TextDecoder().decode(harness.vault.fileBytes("notes/edit.md") ?? new Uint8Array()),
    ).toBe("fresh remote bytes");
    // The local edit survives as pending outbound work.
    const trackedFile = harness.journalRepository.readLocalFileByPath("notes/edit.md");
    expect(trackedFile).not.toBeNull();
    const events = harness.journalRepository.readEventsByLocalFileId(trackedFile?.localFileId ?? "");
    expect(events.some((event) => event.state === "queued")).toBe(true);
    // The stale download became a durable conflict blocker.
    expect(progressAtCompletion.read()).toEqual([
      {
        actionIndex: 0,
        actionKind: "download",
        outcome: "terminal_safe",
        reason: "device_manifest_action_stale",
      },
    ]);
  });

  it("marks a locally diverged download terminal without overwriting the newer bytes", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/diverged.md", bytes: STALE_BYTES }],
    });
    const localEntryId = await entryIdOfLocator("notes/diverged.md");
    harness.api.plan = [downloadAction(localEntryId)];
    const progressAtCompletion = captureActionProgressAtCompletion(harness);
    let diverged = false;
    harness.api.onActionRead = () => {
      if (diverged) {
        return;
      }
      diverged = true;
      harness.vault.setFileBytes("notes/diverged.md", FRESH_BYTES);
    };

    const outcome = await harness.reconciler.reconcile("periodic");

    expect(outcome.kind).toBe("completed");
    expect(harness.vault.writeCount).toBe(0);
    expect(progressAtCompletion.read()[0]?.reason).toBe("device_manifest_local_diverged");
  });

  it("refuses a download whose target holds a restore reservation", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/reserved.md", bytes: STALE_BYTES }],
    });
    const localEntryId = await entryIdOfLocator("notes/reserved.md");
    harness.api.plan = [downloadAction(localEntryId)];
    const progressAtCompletion = captureActionProgressAtCompletion(harness);
    // The tracked mapping must exist before a lifecycle reservation can
    // hold its target (the manifest capture itself creates no mapping).
    const trackedCapture = await harness.journalRepository.recordCapture({
      normalizedPath: "notes/reserved.md",
      fingerprint: await fingerprintOf(STALE_BYTES),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(trackedCapture.outcome).toBe("event_recorded");
    const trackedFile = harness.journalRepository.readLocalFileByPath("notes/reserved.md");
    expect(trackedFile).not.toBeNull();
    await harness.database.runSerializedMutation((session) => {
      session.exec(
        `update local_files set lifecycle_state = 'restore_pending' where local_file_id = '${trackedFile?.localFileId}';`,
      );
    });

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome.kind).toBe("completed");
    expect(harness.vault.writeCount).toBe(0);
    expect(progressAtCompletion.read()[0]?.reason).toBe("device_manifest_target_occupied");
  });

  it("closes a policy-excluded download without any download request", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/excluded.md", bytes: STALE_BYTES }],
      policyGate: locatorExcludingPolicyGate("notes/excluded.md"),
    });
    const localEntryId = await entryIdOfLocator("notes/excluded.md");
    harness.api.plan = [downloadAction(localEntryId)];
    const progressAtCompletion = captureActionProgressAtCompletion(harness);

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome.kind).toBe("completed");
    expect(harness.vault.writeCount).toBe(0);
    expect(progressAtCompletion.read()[0]?.reason).toBe("device_manifest_policy_excluded");
  });

  it("settles a canonical-only download as a durable identity conflict", async () => {
    const harness = createReconcilerHarness();
    const progressAtCompletion = captureActionProgressAtCompletion(harness);
    harness.api.plan = [
      {
        actionIndex: 0,
        actionKind: "download",
        localEntryId: null,
        sourceId: SOURCE_ID,
        sourceVersionId: SOURCE_VERSION_ID,
        sourceLocatorId: "12345678-1234-4781-8123-123456789012",
        sourceTombstoneId: null,
        reason: null,
      },
    ];

    const outcome = await harness.reconciler.reconcile("onboarding");

    expect(outcome.kind).toBe("completed");
    expect(harness.vault.writeCount).toBe(0);
    expect(progressAtCompletion.read()[0]?.reason).toBe("device_manifest_identity_ambiguous");
  });

  it("keeps unrelated safe actions valid while one action is stale", async () => {
    const harness = createReconcilerHarness({
      files: [
        { locator: "notes/stale.md", bytes: STALE_BYTES },
        { locator: "notes/clean.md", bytes: STALE_BYTES },
      ],
    });
    const staleEntryId = await entryIdOfLocator("notes/stale.md");
    const cleanEntryId = await entryIdOfLocator("notes/clean.md");
    harness.api.plan = [downloadAction(staleEntryId, 0), downloadAction(cleanEntryId, 1)];
    const progressAtCompletion = captureActionProgressAtCompletion(harness);
    let diverged = false;
    harness.api.onActionRead = () => {
      if (diverged) {
        return;
      }
      diverged = true;
      harness.vault.setFileBytes("notes/stale.md", FRESH_BYTES);
    };

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome).toEqual({ kind: "completed", checkpointSequence: CHECKPOINT_SEQUENCE });
    const progress = progressAtCompletion.read();
    expect(progress[0]?.reason).toBe("device_manifest_local_diverged");
    expect(progress[1]?.outcome).toBe("terminal_safe");
    expect(progress[1]?.reason).toBeNull();
    expect(
      new TextDecoder().decode(harness.vault.fileBytes("notes/clean.md") ?? new Uint8Array()),
    ).toBe("fresh remote bytes");
  });
});

// --- uploads and barrier release (spec 12.4, step 3) ---------------------------------------------------

describe("ManifestReconciler uploads and barrier release (spec 12.4)", () => {
  it("terminalizes an upload by durably recording the outbound event under the barrier", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/upload.md", bytes: FRESH_BYTES }],
    });
    const localEntryId = await entryIdOfLocator("notes/upload.md");
    harness.api.plan = [
      {
        actionIndex: 0,
        actionKind: "upload",
        localEntryId,
        sourceId: null,
        sourceVersionId: null,
        sourceLocatorId: null,
        sourceTombstoneId: null,
        reason: null,
      },
    ];
    const progressAtCompletion = captureActionProgressAtCompletion(harness);

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome.kind).toBe("completed");
    const trackedFile = harness.journalRepository.readLocalFileByPath("notes/upload.md");
    expect(trackedFile).not.toBeNull();
    const events = harness.journalRepository.readEventsByLocalFileId(trackedFile?.localFileId ?? "");
    expect(events.filter((event) => event.state === "queued")).toHaveLength(1);
    expect(progressAtCompletion.read()[0]?.outcome).toBe("terminal_safe");
  });

  it("reauthorizes an upload when the watcher already recorded the outbound event", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/reauth.md", bytes: FRESH_BYTES }],
    });
    const localEntryId = await entryIdOfLocator("notes/reauth.md");
    harness.api.plan = [
      {
        actionIndex: 0,
        actionKind: "upload",
        localEntryId,
        sourceId: null,
        sourceVersionId: null,
        sourceLocatorId: null,
        sourceTombstoneId: null,
        reason: null,
      },
    ];
    // The watcher admitted the same newer bytes during the run: the planner
    // upload must reauthorize exactly that one row, not a second one.
    const progressAtCompletion = captureActionProgressAtCompletion(harness);
    let admitted = false;
    harness.api.onActionRead = async () => {
      if (admitted) {
        return;
      }
      admitted = true;
      await harness.journalRepository.recordCapture({
        normalizedPath: "notes/reauth.md",
        fingerprint: await fingerprintOf(FRESH_BYTES),
        policyRevisionNumber: 1,
        admission: "policy_allowed",
      });
    };

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome.kind).toBe("completed");
    const trackedFile = harness.journalRepository.readLocalFileByPath("notes/reauth.md");
    const events = harness.journalRepository.readEventsByLocalFileId(trackedFile?.localFileId ?? "");
    expect(events.filter((event) => event.state === "queued")).toHaveLength(1);
    expect(progressAtCompletion.read()[0]?.outcome).toBe("terminal_safe");
  });

  it("makes rows observed after G and planner uploads dispatchable after completion", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/upload.md", bytes: FRESH_BYTES }],
    });
    const localEntryId = await entryIdOfLocator("notes/upload.md");
    harness.api.plan = [
      {
        actionIndex: 0,
        actionKind: "upload",
        localEntryId,
        sourceId: null,
        sourceVersionId: null,
        sourceLocatorId: null,
        sourceTombstoneId: null,
        reason: null,
      },
    ];
    // An edit observed while the barrier is active: watcher capture
    // continues under the barrier and the row must survive to dispatch.
    let observed = false;
    harness.api.onActionRead = async () => {
      if (observed) {
        return;
      }
      observed = true;
      await harness.journalRepository.recordCapture({
        normalizedPath: "notes/post-barrier-edit.md",
        fingerprint: await fingerprintOf(bytesOf("post barrier")),
        policyRevisionNumber: 1,
        admission: "policy_allowed",
      });
      harness.vault.setFileBytes("notes/post-barrier-edit.md", bytesOf("post barrier"));
    };

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome.kind).toBe("completed");
    expect(harness.deviceSync.readState().barrierGeneration).toBeNull();
    // Both the post-barrier edit and the planner upload became dispatchable.
    expect(harness.journalRepository.readOldestEligibleEvent(Date.now())).not.toBeNull();
    for (const locator of ["notes/upload.md", "notes/post-barrier-edit.md"]) {
      const file = harness.journalRepository.readLocalFileByPath(locator);
      const events = harness.journalRepository.readEventsByLocalFileId(file?.localFileId ?? "");
      expect(events.some((event) => event.state === "queued")).toBe(true);
    }
  });
});

// --- expiry and policy advance (spec 9.1, 7.3) --------------------------------------------------------

describe("ManifestReconciler run expiry and policy advance", () => {
  it.each([
    ["device_manifest_expired"],
    ["device_manifest_policy_advanced"],
  ] as const)(
    "discards only the run progress on %s and starts a new checkpoint-bound run",
    async (reason) => {
      const harness = createReconcilerHarness({
        files: [{ locator: "notes/synced.md", bytes: STALE_BYTES }],
      });
      harness.api.scriptRuns([
        { manifestRunId: RUN_ID, checkpointSequence: CHECKPOINT_SEQUENCE },
        { manifestRunId: SECOND_RUN_ID, checkpointSequence: SECOND_CHECKPOINT_SEQUENCE },
      ]);
      // A local edit made before the run must survive the discard.
      await harness.journalRepository.recordCapture({
        normalizedPath: "notes/local-edit.md",
        fingerprint: await fingerprintOf(bytesOf("local edit")),
        policyRevisionNumber: 1,
        admission: "policy_allowed",
      });
      harness.api.failOnceAt("page", new DeviceSyncApiError(reason, false, null, null));
      let stateAtSecondStart: DeviceSyncState | null = null;
      const readStateAtSecondStart = (): DeviceSyncState | null => stateAtSecondStart;
      harness.api.onStart = (startCount) => {
        if (startCount >= 2) {
          stateAtSecondStart = harness.deviceSync.readState();
        }
      };

      const outcome = await harness.reconciler.reconcile("explicit_repair");

      expect(outcome).toEqual({ kind: "completed", checkpointSequence: SECOND_CHECKPOINT_SEQUENCE });
      expect(harness.api.starts).toHaveLength(2);
      // The exact temporary run progress was discarded before the new run.
      expect(readStateAtSecondStart()?.activeManifestRunId).toBeNull();
      expect(readStateAtSecondStart()?.barrierGeneration).not.toBeNull();
      expect(harness.journal.readManifestPageProgress()).toHaveLength(0);
      // The local edit survived untouched.
      const editFile = harness.journalRepository.readLocalFileByPath("notes/local-edit.md");
      const editEvents = harness.journalRepository.readEventsByLocalFileId(editFile?.localFileId ?? "");
      expect(editEvents.some((event) => event.state === "queued")).toBe(true);
      // The closed reason was recorded exactly once on the reconcile surface.
      expect(harness.diagnostics.reconcileFailures).toContainEqual({ stage: "page", reason });
    },
  );
});

// --- the closed diagnostics surface (step 6) ------------------------------------------------------------

describe("ManifestReconciler closed failure surface (spec 14.1)", () => {
  it.each([
    ["start", "network_timeout", true],
    ["page", "network_offline", true],
    ["finalize", "network_rate_limited", true],
    ["actions", "network_timeout", true],
    ["complete", "network_offline", true],
  ] as const)(
    "reports one %s-stage reconcile failure with the exact reason token",
    async (stage, reason, retryable) => {
      const harness = createReconcilerHarness({
        files: [{ locator: "notes/synced.md", bytes: STALE_BYTES }],
      });
      harness.api.failOnceAt(stage, new DeviceSyncApiError(reason, retryable, null, null));

      const outcome = await harness.reconciler.reconcile("explicit_repair");

      expect(outcome).toEqual(
        retryable === true
          ? { kind: "retry", reason }
          : { kind: "blocked", reason },
      );
      expect(harness.diagnostics.reconcileFailures).toContainEqual({ stage, reason });
    },
  );

  it("maps a non-retryable wire failure onto a blocked outcome", async () => {
    const harness = createReconcilerHarness();
    harness.api.failOnceAt("start", new DeviceSyncApiError("device_manifest_state_invalid", false));

    const outcome = await harness.reconciler.reconcile("periodic");

    expect(outcome).toEqual({ kind: "blocked", reason: "device_manifest_state_invalid" });
    expect(harness.diagnostics.reconcileFailures).toEqual([
      { stage: "start", reason: "device_manifest_state_invalid" },
    ]);
  });

  it("reports a repository completion failure at the complete stage instead of swallowing it", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/synced.md", bytes: STALE_BYTES }],
    });
    const journal = harness.journal as ManifestReconcilerJournal & {
      completeDeviceSyncRepair: (input: unknown) => Promise<void>;
    };
    const original = journal.completeDeviceSyncRepair.bind(journal);
    let failedOnce = false;
    journal.completeDeviceSyncRepair = async (input: unknown) => {
      if (!failedOnce) {
        failedOnce = true;
        throw journalStoreError("journal_mutation_failed");
      }
      return original(input);
    };

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome).toEqual({ kind: "blocked", reason: "journal_mutation_failed" });
    expect(harness.diagnostics.reconcileFailures).toContainEqual({
      stage: "complete",
      reason: "journal_mutation_failed",
    });
  });

  it("never lets a raw locator reach a diagnostics token", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/very-secret-locator.md", bytes: STALE_BYTES }],
    });
    const localEntryId = await entryIdOfLocator("notes/very-secret-locator.md");
    harness.api.plan = [downloadAction(localEntryId)];
    let diverged = false;
    harness.api.onActionRead = () => {
      if (diverged) {
        return;
      }
      diverged = true;
      harness.vault.setFileBytes("notes/very-secret-locator.md", FRESH_BYTES);
    };
    harness.api.failOnceAt("complete", new DeviceSyncApiError("network_offline", true));

    await harness.reconciler.reconcile("explicit_repair");

    const tokens = [
      ...harness.diagnostics.reconcileFailures.map((failure) => `${failure.stage}:${failure.reason}`),
      ...harness.diagnostics.others,
    ];
    for (const token of tokens) {
      expect(token).not.toContain("very-secret-locator");
    }
  });
});


// --- closed capture-stage failures (fix round 1 I1) ----------------------------------------------------

describe("ManifestReconciler closed capture-stage failures (fix round 1 I1)", () => {
  it(
    "surfaces the 100,000-entry capture cap as one blocked page-stage observation",
    async () => {
      const sharedBytes = bytesOf("x");
      const files = Array.from(
        { length: MAX_MANIFEST_TOTAL_ENTRIES + 1 },
        (_, index) => ({ locator: `notes/cap-${index}.md`, bytes: sharedBytes }),
      );
      const harness = createReconcilerHarness({ files });

      const outcome = await harness.reconciler.reconcile("explicit_repair");

      expect(outcome).toEqual({ kind: "blocked", reason: "device_manifest_capture_failed" });
      expect(harness.diagnostics.reconcileFailures).toEqual([
        { stage: "page", reason: "device_manifest_capture_failed" },
      ]);
      // No page was sent and the barrier stays retained for the next attempt.
      expect(harness.api.appendedPages).toEqual([]);
      expect(harness.deviceSync.readState().barrierGeneration).not.toBeNull();
      expect(harness.deviceSync.readState().activeManifestRunId).toBeNull();
    },
    30_000,
  );

  it("surfaces an unreadable page-progress read as one closed page-stage observation", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/synced.md", bytes: STALE_BYTES }],
    });
    const journal = harness.journal as ManifestReconcilerJournal & {
      readManifestPageProgress: () => readonly ManifestActionProgressRecord[];
    };
    journal.readManifestPageProgress = () => {
      throw journalStoreError("journal_query_failed");
    };

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome).toEqual({ kind: "blocked", reason: "journal_query_failed" });
    expect(harness.diagnostics.reconcileFailures).toEqual([
      { stage: "page", reason: "journal_query_failed" },
    ]);
  });

  it("surfaces an unreadable reconciliation state as one closed start-stage observation", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/synced.md", bytes: STALE_BYTES }],
    });
    (harness.deviceSync as unknown as { readState: () => DeviceSyncState }).readState = () => {
      throw journalStoreError("journal_image_invalid");
    };

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome).toEqual({ kind: "blocked", reason: "journal_image_invalid" });
    expect(harness.diagnostics.reconcileFailures).toEqual([
      { stage: "start", reason: "journal_image_invalid" },
    ]);
  });
});

// --- upload refusal and the synthetic-sequence lattice (fix round 1 I2) ---------------------------------

describe("ManifestReconciler upload refusal and apply lattice (fix round 1 I2)", () => {
  /** Seed `count` queued events directly inside one serialized transaction. */
  async function seedPendingEvents(
    harness: ReconcilerHarness,
    count: number,
  ): Promise<void> {
    await harness.database.runSerializedMutation((session) => {
      session.exec(
        Array.from(
          { length: count },
          (_, index) =>
            `insert into local_files (local_file_id, normalized_path, source_id, observed_sha256, observed_size_bytes, observed_media_type, base_version_id, policy_revision) values ('seed-file-${index}', 'seed/file-${index}.md', null, '${String(index).padStart(64, "0")}', 1, 'text/plain', null, 1);`,
        ).join(""),
      );
      session.exec(
        Array.from(
          { length: count },
          (_, index) =>
            `insert into journal_events (event_id, local_file_id, idempotency_key, operation, sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count, next_eligible_retry_epoch_ms, safe_error, operation_id, created_at_epoch_ms) values ('seed-event-${index}', 'seed-file-${index}', 'seed-key-${index}', 'create', '${String(index).padStart(64, "0")}', 1, 'text/plain', 'queued', 0, 0, null, null, null, ${index});`,
        ).join(""),
      );
    });
  }

  it(
    "blocks the run when the journal refuses the upload at its queue limits",
    async () => {
      const harness = createReconcilerHarness({
        files: [{ locator: "notes/upload.md", bytes: FRESH_BYTES }],
      });
      await seedPendingEvents(harness, MAX_PENDING_EVENTS);
      const localEntryId = await entryIdOfLocator("notes/upload.md");
      harness.api.plan = [
        {
          actionIndex: 0,
          actionKind: "upload",
          localEntryId,
          sourceId: null,
          sourceVersionId: null,
          sourceLocatorId: null,
          sourceTombstoneId: null,
          reason: null,
        },
      ];

      const outcome = await harness.reconciler.reconcile("explicit_repair");

      expect(outcome).toEqual({ kind: "blocked", reason: "journal_mutation_failed" });
      expect(harness.diagnostics.reconcileFailures).toEqual([
        { stage: "actions", reason: "journal_mutation_failed" },
      ]);
      // The barrier and the durable page progress stay retained: the next
      // attempt resumes instead of restarting from scratch.
      expect(harness.deviceSync.readState().barrierGeneration).not.toBeNull();
      expect(harness.deviceSync.readState().activeManifestRunId).toBe(RUN_ID);
      // No outbound event was recorded for the refused upload.
      const trackedFile = harness.journalRepository.readLocalFileByPath("notes/upload.md");
      expect(trackedFile).toBeNull();
    },
    20_000,
  );

  it("blocks a download whose synthetic sequence cannot fit below the checkpoint", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/late.md", bytes: STALE_BYTES }],
    });
    harness.api.scriptRuns([{ manifestRunId: RUN_ID, checkpointSequence: 1 }]);
    // The local cursor already sits at the checkpoint: the next contiguous
    // synthetic sequence (2) would pass it.
    await harness.deviceSync.terminalizeEvent({
      eventSequence: 1,
      outcome: "excluded",
      reason: null,
    });
    const localEntryId = await entryIdOfLocator("notes/late.md");
    harness.api.plan = [downloadAction(localEntryId)];

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome).toEqual({ kind: "blocked", reason: "device_cursor_gap" });
    expect(harness.diagnostics.reconcileFailures).toEqual([
      { stage: "actions", reason: "device_cursor_gap" },
    ]);
    expect(harness.vault.writeCount).toBe(0);
    expect(harness.deviceSync.readState().appliedSequence).toBe(1);
  });

  it("pins every reconcile reason's closed barrier-reason mapping", () => {
    expect(RECONCILE_BARRIER_REASONS).toEqual({
      onboarding: "device_manifest_state_invalid",
      sqlite_rebuilt: "journal_image_invalid",
      cursor_gap: "device_cursor_gap",
      history_compacted: "device_event_unavailable",
      unknown_event: "device_event_integrity_failed",
      local_invariant: "device_manifest_state_invalid",
      explicit_repair: "device_manifest_state_invalid",
      periodic: "device_manifest_state_invalid",
    });
  });
});

// --- settle-reason observations (fix round 1 I3) --------------------------------------------------------

describe("ManifestReconciler settle-reason observations (fix round 1 I3)", () => {
  it("leaves a closed actions-stage observation for a canonical-only settle that survives completion", async () => {
    const harness = createReconcilerHarness();
    const progressAtCompletion = captureActionProgressAtCompletion(harness);
    harness.api.plan = [
      {
        actionIndex: 0,
        actionKind: "download",
        localEntryId: null,
        sourceId: SOURCE_ID,
        sourceVersionId: SOURCE_VERSION_ID,
        sourceLocatorId: "12345678-1234-4781-8123-123456789012",
        sourceTombstoneId: null,
        reason: null,
      },
    ];

    const outcome = await harness.reconciler.reconcile("onboarding");

    expect(outcome.kind).toBe("completed");
    expect(progressAtCompletion.read()[0]?.reason).toBe("device_manifest_identity_ambiguous");
    // The progress rows are legitimately discarded at completion; the one
    // closed observation is the durable readable record that remains.
    expect(harness.journal.readManifestActionProgress()).toEqual([]);
    expect(harness.diagnostics.reconcileFailures).toEqual([
      { stage: "actions", reason: "device_manifest_identity_ambiguous" },
    ]);
  });

  it("observes the settle reason of a stale download without a second observation", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/late-edit.md", bytes: STALE_BYTES }],
    });
    const localEntryId = await entryIdOfLocator("notes/late-edit.md");
    harness.api.plan = [downloadAction(localEntryId)];
    let admitted = false;
    harness.api.onActionRead = async () => {
      if (admitted) {
        return;
      }
      admitted = true;
      await harness.journalRepository.recordCapture({
        normalizedPath: "notes/late-edit.md",
        fingerprint: await fingerprintOf(FRESH_BYTES),
        policyRevisionNumber: 1,
        admission: "policy_allowed",
      });
    };

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome.kind).toBe("completed");
    expect(harness.diagnostics.reconcileFailures).toEqual([
      { stage: "actions", reason: "device_manifest_action_stale" },
    ]);
    expect(harness.vault.writeCount).toBe(0);
  });
});

// --- foreign reason rejection (fix round 1 minor) --------------------------------------------------------

describe("ManifestReconciler foreign reason rejection (fix round 1 minor)", () => {
  it("never adopts a foreign reason string from a thrown store-like error", async () => {
    const harness = createReconcilerHarness({
      files: [{ locator: "notes/synced.md", bytes: STALE_BYTES }],
    });
    const journal = harness.journal as ManifestReconcilerJournal & {
      completeDeviceSyncRepair: (input: unknown) => Promise<void>;
    };
    journal.completeDeviceSyncRepair = async () => {
      throw { reason: "definitely-not-a-closed-token" };
    };

    const outcome = await harness.reconciler.reconcile("explicit_repair");

    expect(outcome).toEqual({ kind: "retry", reason: "server_error" });
    expect(harness.diagnostics.reconcileFailures).toEqual([
      { stage: "complete", reason: "server_error" },
    ]);
  });
});

// --- the JournalPersistence composition (carry-forward) -------------------------------------------------

class MemoryJournalFileStore implements JournalFileStore {
  readonly files = new Map<string, ArrayBuffer>();

  async exists(fileName: string): Promise<boolean> {
    return this.files.has(fileName);
  }

  async readBinary(fileName: string): Promise<ArrayBuffer> {
    const bytes = this.files.get(fileName);
    if (bytes === undefined) {
      throw new Error(`absent ${fileName}`);
    }
    return bytes;
  }

  async writeBinary(fileName: string, data: ArrayBuffer): Promise<void> {
    this.files.set(fileName, data);
  }

  async remove(fileName: string): Promise<void> {
    this.files.delete(fileName);
  }
}

describe("ManifestReconciler JournalPersistence composition", () => {
  /**
   * The same journal slice adapter the plugin composition root builds (the
   * Task 8 composition precedent): only the serialized mutation queue and
   * the read seam are ever exercised through it, so the structural cast
   * names exactly the two members that matter.
   */
  function persistenceDatabase(
    persistence: JournalPersistence,
  ): SqliteDatabase {
    return {
      runSerializedMutation: (operation: (session: never) => unknown) =>
        persistence.commitGeneration(operation as never),
      readAll: (sql: string) => persistence.readAll(sql),
    } as unknown as SqliteDatabase;
  }

  async function flagReconcileRequired(persistence: JournalPersistence): Promise<void> {
    await persistence.commitGeneration((session) => {
      session.writeJournalMeta({ ...session.readJournalMeta(), isReconcileRequired: true });
    });
  }

  it("clears journal_meta.is_reconcile_required through the sticky merge and survives reopen", async () => {
    const fileStore = new MemoryJournalFileStore();
    const persistence = new JournalPersistence({ fileStore, engineModule });
    await persistence.open();
    const harness = createReconcilerHarness({
      database: persistenceDatabase(persistence),
      onDeviceSyncRepairComplete: () => persistence.markReconcileComplete(),
    });
    await flagReconcileRequired(persistence);
    expect(persistence.readJournalMeta().isReconcileRequired).toBe(true);

    const outcome = await harness.reconciler.reconcile("sqlite_rebuilt");
    expect(outcome.kind).toBe("completed");
    expect(persistence.readJournalMeta().isReconcileRequired).toBe(false);

    persistence.close();
    const reopened = new JournalPersistence({ fileStore, engineModule });
    await reopened.open();
    expect(reopened.readJournalMeta().isReconcileRequired).toBe(false);
    reopened.close();
  });

  it("keeps the flag sticky without the reconcile-complete surface (the regression control)", async () => {
    const fileStore = new MemoryJournalFileStore();
    const persistence = new JournalPersistence({ fileStore, engineModule });
    await persistence.open();
    const harness = createReconcilerHarness({ database: persistenceDatabase(persistence) });
    await flagReconcileRequired(persistence);

    const outcome = await harness.reconciler.reconcile("sqlite_rebuilt");
    expect(outcome.kind).toBe("completed");
    // Without the persistence clear surface the sticky merge re-clobbers
    // the repository-transaction clear — exactly the Task 8 carry-forward.
    expect(persistence.readJournalMeta().isReconcileRequired).toBe(true);
    persistence.close();
  });
});
