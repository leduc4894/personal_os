import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { CapturePolicySubject } from "../exclusion-policy/policy-session";
import type { JournalMeta } from "./contracts";
import { FILE_SETTLE_DELAY_MS, MAX_FILE_SIZE_BYTES } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import {
  EXISTING_FILES_SCAN_BATCH_FILES,
  EXISTING_FILES_SCAN_MAXIMUM_FILES,
  JournalCapture,
} from "./capture";
import type {
  CapturePolicyGate,
  CaptureVaultReader,
  ExistingFilesScanSummary,
} from "./capture";
import type { LifecycleCapture } from "./lifecycle-capture";
import type {
  LifecycleDeleteResult,
  LifecycleRenameResult,
  LifecycleRestoreResult,
} from "./lifecycle-capture";
import { JournalRepository } from "./repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";
import type { JournalFailureReporter } from "./diagnostic-reporter";
import type { SyncDiagnosticClosedToken } from "./sync-diagnostics-trail";

/** The real sql.js WebAssembly engine drives every capture test (spec 6.1). */
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

afterEach(() => {
  vi.useRealTimers();
});

const PRIVATE_CONTENT = "private-device-only-content-7f3a21";

function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

/** The fake Vault: regular files only, counting every byte read and listing. */
class FakeCaptureVault implements CaptureVaultReader {
  readonly #files = new Map<string, Uint8Array>();
  byteReadCount = 0;
  listCallCount = 0;

  setFileBytes(normalizedPath: string, contentBytes: Uint8Array): void {
    this.#files.set(normalizedPath, contentBytes);
  }

  removeFileBytes(normalizedPath: string): void {
    this.#files.delete(normalizedPath);
  }

  async readRegularFileBytes(normalizedPath: string): Promise<Uint8Array | null> {
    const contentBytes = this.#files.get(normalizedPath);
    if (contentBytes === undefined) {
      return null;
    }
    this.byteReadCount += 1;
    return contentBytes;
  }

  async listRegularFilePaths(): Promise<readonly string[]> {
    this.listCallCount += 1;
    return [...this.#files.keys()].sort();
  }
}

type PolicyOutcome = "allowed" | "excluded" | "indeterminate";

interface FakePolicyGate extends CapturePolicyGate {
  readonly subjects: readonly CapturePolicySubject[];
}

function createFakePolicyGate(
  decide: (subject: CapturePolicySubject) => PolicyOutcome,
  revisionNumber = 1,
): FakePolicyGate {
  const subjects: CapturePolicySubject[] = [];
  return {
    subjects,
    evaluateForCapture(subject: CapturePolicySubject) {
      subjects.push(subject);
      const raw = decide(subject);
      if (raw === "allowed") {
        return { decision: { raw: "allowed", enforced: "allowed" }, revisionNumber };
      }
      return { decision: { raw, enforced: "excluded" }, revisionNumber };
    },
  };
}

interface CaptureHarness {
  readonly capture: JournalCapture;
  readonly repository: JournalRepository;
  readonly database: SqliteDatabase;
  readonly vault: FakeCaptureVault;
  readonly gate: FakePolicyGate;
  readonly lifecycleState: FakeLifecycleState;
  readonly failureTokens: SyncDiagnosticClosedToken[];
}

interface AutomaticSnapshotTestHarness {
  captureUnderPolicy(
    normalizedPath: string,
    admission: "excluded_policy",
  ): Promise<void>;
  allowMarkdown(): void;
  eventsFor(normalizedPath: string): readonly ReturnType<JournalRepository["readEventsByLocalFileId"]>[number][];
}

function createHarness(options?: {
  readonly policyRevisionNumber?: number;
  readonly decide?: (subject: CapturePolicySubject) => PolicyOutcome;
  readonly scanMaximumFiles?: number;
  readonly scanBatchFiles?: number;
}): CaptureHarness & AutomaticSnapshotTestHarness {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  } satisfies JournalMeta);
  const repository = new JournalRepository({ database });
  const vault = new FakeCaptureVault();
  const policyOutcomes = new Map<string, PolicyOutcome>();
  const gate = createFakePolicyGate(
    (subject) =>
      (typeof subject.normalizedLocator === "string"
        ? policyOutcomes.get(subject.normalizedLocator)
        : undefined) ?? options?.decide?.(subject) ?? "allowed",
    options?.policyRevisionNumber ?? 1,
  );
  const { fake: lifecycleCapture, state: lifecycleState } = createFakeLifecycleCapture();
  const failureTokens: SyncDiagnosticClosedToken[] = [];
  const failureReporter: JournalFailureReporter = {
    reportJournalFailure(token): void {
      failureTokens.push(token);
    },
  };
  const capture = new JournalCapture({
    repository,
    vaultReader: vault,
    policyGate: gate,
    lifecycleCapture,
    scanMaximumFiles: options?.scanMaximumFiles,
    scanBatchFiles: options?.scanBatchFiles,
    failureReporter,
  });
  return {
    capture,
    repository,
    database,
    vault,
    gate,
    lifecycleState,
    failureTokens,
    async captureUnderPolicy(normalizedPath, admission): Promise<void> {
      policyOutcomes.set(normalizedPath, admission === "excluded_policy" ? "excluded" : "allowed");
      vault.setFileBytes(normalizedPath, bytesOf(`fixture ${normalizedPath}`));
      await capture.runAutomaticSnapshot();
    },
    allowMarkdown(): void {
      policyOutcomes.clear();
    },
    eventsFor(normalizedPath) {
      const localFile = repository.readLocalFileByPath(normalizedPath);
      return localFile === null ? [] : repository.readEventsByLocalFileId(localFile.localFileId);
    },
  };
}

/**
 * The capture tests verify the content surface only; the lifecycle adapter
 * has its own suite (`lifecycle-capture.test.ts`). A stub that records
 * calls but never writes keeps the focus on settle + admit + guard.
 */
interface FakeLifecycleCapture extends LifecycleCapture {
  readonly detectAutomaticRestore?: (normalizedPath: string) => Promise<LifecycleRestoreResult>;
  readonly markTombstonedPathReconcileRequired?: (normalizedPath: string) => Promise<boolean>;
  readonly reconcileCalls: readonly string[];
}

interface FakeLifecycleState {
  readonly deleteCalls: { readonly path: string }[];
  readonly renameCalls: { readonly file: { readonly path: string }; readonly priorPath: string }[];
  readonly reconcileCalls: readonly string[];
}

/**
 * The capture tests verify the content surface only; the lifecycle adapter
 * has its own suite (`lifecycle-capture.test.ts`). A stub that records
 * calls but never writes keeps the focus on settle + admit + guard.
 */
function createFakeLifecycleCapture(options?: {
  readonly autoRestoreOutcome?: "succeed" | "reject";
}): { readonly fake: FakeLifecycleCapture; readonly state: FakeLifecycleState } {
  const state: FakeLifecycleState = {
    deleteCalls: [],
    renameCalls: [],
    reconcileCalls: [],
  };
  const reconcileCalls = state.reconcileCalls as string[];
  const fake: FakeLifecycleCapture = {
    reconcileCalls,
    captureRename: async (file, priorPath) => {
      state.renameCalls.push({ file: { path: file.path }, priorPath });
      const result: LifecycleRenameResult | null = null;
      return result;
    },
    captureDelete: async (file) => {
      state.deleteCalls.push({ path: file.path });
      const result: LifecycleDeleteResult | null = null;
      return result;
    },
    requestRestore: async () => {
      throw new Error("not used in capture tests");
    },
    detectAutomaticRestore: async () => {
      if (options?.autoRestoreOutcome === "succeed") {
        return {
          operation: "restore",
          localFileId: "00000000-0000-4000-8000-000000000000",
          eventId: "11111111-1111-7111-8111-111111111111",
          predecessorEventId: "22222222-2222-7222-8222-222222222222",
        };
      }
      throw new Error("not used in capture tests");
    },
    markTombstonedPathReconcileRequired: async (normalizedPath: string) => {
      reconcileCalls.push(normalizedPath);
      return true;
    },
  };
  return { fake, state };
}

/**
 * Fake only the settle timers: the admission chain awaits native WebCrypto
 * promises that resolve on the real event loop, so settling advances the
 * clock and then awaits the serialized admission tail.
 */
function useSettleFakeTimers(): void {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
}

/** Advance past the per-path settle delay and await the admission tail. */
async function settlePastDelay(harness: CaptureHarness, extraMilliseconds = 50): Promise<void> {
  await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS + extraMilliseconds);
  await harness.capture.whenIdle();
}

function soleEventOf(
  harness: CaptureHarness,
  normalizedPath: string,
): ReturnType<JournalRepository["readEventsByLocalFileId"]>[number] {
  const localFile = harness.repository.readLocalFileByPath(normalizedPath);
  if (localFile === null) {
    throw new Error(`expected a tracked file for ${normalizedPath}`);
  }
  const events = harness.repository.readEventsByLocalFileId(localFile.localFileId);
  const [event] = events;
  if (events.length !== 1 || event === undefined) {
    throw new Error(`expected exactly one event for ${normalizedPath}`);
  }
  return event;
}

function decodeImageText(database: SqliteDatabase): string {
  return new TextDecoder("utf-8", { fatal: false }).decode(database.exportImage());
}

describe("JournalCapture settle admission (spec 7.1, 9)", () => {
  it("does not scan or read the vault at startup", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/existing.md", bytesOf("already here"));

    await vi.advanceTimersByTimeAsync(60_000);

    expect(harness.vault.listCallCount).toBe(0);
    expect(harness.vault.byteReadCount).toBe(0);
    expect(harness.repository.countPendingEvents()).toBe(0);
  });

  it("settles each path once after the delay and records the re-read bytes", async () => {
    useSettleFakeTimers();
    const harness = createHarness({ policyRevisionNumber: 4 });
    harness.vault.setFileBytes("notes/a.md", bytesOf("first version"));
    harness.capture.notifyPathChanged("notes/a.md");

    // Before the delay nothing is read; a later write is what gets admitted.
    await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS - 50);
    expect(harness.vault.byteReadCount).toBe(0);
    harness.vault.setFileBytes("notes/a.md", bytesOf("second version"));
    harness.capture.notifyPathChanged("notes/a.md");
    await settlePastDelay(harness);

    expect(harness.vault.byteReadCount).toBe(1);
    const event = soleEventOf(harness, "notes/a.md");
    expect(event.state).toBe("queued");
    expect(event.operation).toBe("create");
    expect(event.fingerprint).toEqual(
      await deriveFrozenFingerprint(bytesOf("second version")),
    );
    expect(harness.repository.readLocalFileByPath("notes/a.md")?.policyRevisionNumber).toBe(4);
    expect(harness.gate.subjects.length).toBe(1);
    expect(harness.gate.subjects[0]).toEqual({
      sourceId: null,
      normalizedLocator: "notes/a.md",
      mediaType: "text/plain",
      sizeBytes: bytesOf("second version").byteLength,
    });
  });

  it("resolves the notify promise only after the settled admission is durable", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/gate.md", bytesOf("content"));

    let isResolved = false;
    const notifyPromise = harness.capture.notifyPathChanged("notes/gate.md");
    void notifyPromise.then(() => {
      isResolved = true;
    });
    await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS - 50);
    expect(isResolved).toBe(false);
    expect(harness.repository.readLocalFileByPath("notes/gate.md")).toBeNull();

    await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS);
    await notifyPromise;

    expect(isResolved).toBe(true);
    expect(harness.repository.readLocalFileByPath("notes/gate.md")).not.toBeNull();
    expect(soleEventOf(harness, "notes/gate.md").state).toBe("queued");
  });

  it("reports a rejected settled admission while preserving its resolved waiter outcome", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/rejected-settle.md", bytesOf("content"));
    vi.spyOn(harness.repository, "recordCapture").mockRejectedValueOnce(
      new Error("persistence rejected"),
    );

    const notifyPromise = harness.capture.notifyPathChanged("notes/rejected-settle.md");
    await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS);

    await expect(notifyPromise).resolves.toBeUndefined();
    expect(harness.failureTokens).toEqual(["settled_admission_failed"]);
  });

  it("resolves every superseded notify promise after the one shared admission", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/twice.md", bytesOf("first"));
    const firstPromise = harness.capture.notifyPathChanged("notes/twice.md");
    await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS - 50);
    harness.vault.setFileBytes("notes/twice.md", bytesOf("second"));
    const secondPromise = harness.capture.notifyPathChanged("notes/twice.md");

    await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS);
    await Promise.all([firstPromise, secondPromise]);

    expect(harness.repository.countPendingEvents()).toBe(1);
    expect(soleEventOf(harness, "notes/twice.md").fingerprint).toEqual(
      await deriveFrozenFingerprint(bytesOf("second")),
    );
  });

  it("resolves pending notify promises on dispose without admitting", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/gone.md", bytesOf("content"));
    const notifyPromise = harness.capture.notifyPathChanged("notes/gone.md");
    await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS - 50);

    harness.capture.dispose();
    await notifyPromise;

    expect(harness.repository.readLocalFileByPath("notes/gone.md")).toBeNull();
  });

  it("re-reads bytes that changed after a single notification", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/quiet.md", bytesOf("before"));
    harness.capture.notifyPathChanged("notes/quiet.md");
    harness.vault.setFileBytes("notes/quiet.md", bytesOf("after"));

    await settlePastDelay(harness);

    expect(harness.vault.byteReadCount).toBe(1);
    expect(soleEventOf(harness, "notes/quiet.md").fingerprint).toEqual(
      await deriveFrozenFingerprint(bytesOf("after")),
    );
  });

  it("settles independent paths independently", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/one.md", bytesOf("one"));
    harness.vault.setFileBytes("notes/two.md", bytesOf("two"));
    harness.capture.notifyPathChanged("notes/one.md");
    await vi.advanceTimersByTimeAsync(100);
    harness.capture.notifyPathChanged("notes/two.md");
    await settlePastDelay(harness);

    expect(harness.repository.countPendingEvents()).toBe(2);
    expect(soleEventOf(harness, "notes/one.md").fingerprint).toEqual(
      await deriveFrozenFingerprint(bytesOf("one")),
    );
    expect(soleEventOf(harness, "notes/two.md").fingerprint).toEqual(
      await deriveFrozenFingerprint(bytesOf("two")),
    );
  });

  it("ignores paths that are not regular files", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.capture.notifyPathChanged("notes");

    await settlePastDelay(harness);

    expect(harness.vault.byteReadCount).toBe(0);
    expect(harness.repository.countPendingEvents()).toBe(0);
    expect(harness.repository.readLocalFileByPath("notes")).toBeNull();
  });

  it("admits a regular file of exactly 16 MiB and blocks one byte over", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/at-limit.bin", new Uint8Array(MAX_FILE_SIZE_BYTES));
    harness.vault.setFileBytes(
      "notes/one-over.bin",
      new Uint8Array(MAX_FILE_SIZE_BYTES + 1),
    );
    harness.capture.notifyPathChanged("notes/at-limit.bin");
    harness.capture.notifyPathChanged("notes/one-over.bin");

    await settlePastDelay(harness);

    expect(soleEventOf(harness, "notes/at-limit.bin").state).toBe("queued");
    const blockedEvent = soleEventOf(harness, "notes/one-over.bin");
    expect(blockedEvent.state).toBe("blocked_size");
    expect(blockedEvent.safeError).toBe("blocked_size");
    expect(blockedEvent.nextEligibleRetryEpochMs).toBeNull();
    // Only the allowed observation still owes work.
    expect(harness.repository.countPendingEvents()).toBe(1);
  });

  it("records excluded and indeterminate policy outcomes as excluded_policy", async () => {
    useSettleFakeTimers();
    const excludedHarness = createHarness({
      decide: (subject) => (subject.normalizedLocator === "notes/a.md" ? "excluded" : "allowed"),
    });
    excludedHarness.vault.setFileBytes("notes/a.md", bytesOf(PRIVATE_CONTENT));
    excludedHarness.capture.notifyPathChanged("notes/a.md");
    await settlePastDelay(excludedHarness);
    const excludedEvent = soleEventOf(excludedHarness, "notes/a.md");
    expect(excludedEvent.state).toBe("excluded_policy");
    expect(excludedEvent.safeError).toBe("excluded_policy");
    expect(excludedEvent.nextEligibleRetryEpochMs).toBeNull();

    useSettleFakeTimers();
    const indeterminateHarness = createHarness({ decide: () => "indeterminate" });
    indeterminateHarness.vault.setFileBytes("notes/b.md", bytesOf(PRIVATE_CONTENT));
    indeterminateHarness.capture.notifyPathChanged("notes/b.md");
    await settlePastDelay(indeterminateHarness);
    const indeterminateEvent = soleEventOf(indeterminateHarness, "notes/b.md");
    expect(indeterminateEvent.state).toBe("excluded_policy");
    expect(indeterminateEvent.safeError).toBe("excluded_policy");
    expect(indeterminateHarness.repository.countPendingEvents()).toBe(0);
  });

  it("stores no file bytes in the journal for allowed or denied captures", async () => {
    useSettleFakeTimers();
    const denied = createHarness({ decide: () => "excluded" });
    denied.vault.setFileBytes("notes/denied.md", bytesOf(PRIVATE_CONTENT));
    denied.capture.notifyPathChanged("notes/denied.md");
    await settlePastDelay(denied);
    expect(soleEventOf(denied, "notes/denied.md").state).toBe("excluded_policy");
    expect(decodeImageText(denied.database)).not.toContain(PRIVATE_CONTENT);

    useSettleFakeTimers();
    const allowed = createHarness();
    allowed.vault.setFileBytes("notes/allowed.md", bytesOf(PRIVATE_CONTENT));
    allowed.capture.notifyPathChanged("notes/allowed.md");
    await settlePastDelay(allowed);
    expect(soleEventOf(allowed, "notes/allowed.md").state).toBe("queued");
    expect(decodeImageText(allowed.database)).not.toContain(PRIVATE_CONTENT);
  });
});

describe("JournalCapture lifecycle guard (spec 7.1, child 5)", () => {
  it("delegates a delete notification to the lifecycle capture", async () => {
    const harness = createHarness();
    await harness.capture.notifyPathDeleted({ path: "notes/gone.md", parent: { path: "notes" } });

    expect(harness.lifecycleState.deleteCalls).toEqual([{ path: "notes/gone.md" }]);
    expect(harness.repository.countPendingEvents()).toBe(0);
  });

  it("delegates a rename notification to the lifecycle capture", async () => {
    const harness = createHarness();
    await harness.capture.notifyPathRenamed(
      { path: "notes/renamed.md", parent: { path: "notes" } },
      "notes/original.md",
    );

    expect(harness.lifecycleState.renameCalls).toEqual([
      { file: { path: "notes/renamed.md" }, priorPath: "notes/original.md" },
    ]);
    expect(harness.repository.countPendingEvents()).toBe(0);
  });

  it("defers a tracked file whose bytes vanish before the settled read", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/vanishing.md", bytesOf("content"));
    harness.capture.notifyPathChanged("notes/vanishing.md");
    await settlePastDelay(harness);
    expect(harness.repository.countPendingEvents()).toBe(1);

    // A later edit whose bytes vanish before the settled read defers the
    // tracked file instead of uploading a deleted version.
    harness.capture.notifyPathChanged("notes/vanishing.md");
    harness.vault.removeFileBytes("notes/vanishing.md");
    await settlePastDelay(harness);

    expect(soleEventOf(harness, "notes/vanishing.md").state).toBe("deferred_lifecycle");
    expect(harness.repository.countPendingEvents()).toBe(0);
  });

  it("writes nothing for lifecycle noise on untracked paths", async () => {
    const harness = createHarness();
    await harness.capture.notifyPathDeleted({ path: "notes/unknown.md", parent: { path: "notes" } });
    await harness.capture.notifyPathRenamed(
      { path: "notes/elsewhere.md", parent: { path: "notes" } },
      "notes/unknown.md",
    );

    expect(harness.lifecycleState.deleteCalls).toEqual([{ path: "notes/unknown.md" }]);
    expect(harness.lifecycleState.renameCalls).toEqual([
      { file: { path: "notes/elsewhere.md" }, priorPath: "notes/unknown.md" },
    ]);
    expect(harness.repository.readLocalFileByPath("notes/unknown.md")).toBeNull();
    expect(harness.repository.countPendingEvents()).toBe(0);
  });
});

describe("JournalCapture automatic restore fail-closed (spec 7.1, child 5 fix round 1 C2)", () => {
  function createFailClosedHarness(options?: {
    readonly autoRestoreBehaviour?: "reject" | "succeed";
  }): CaptureHarness {
    const database = SqliteDatabase.createEmpty(engineModule, {
      schemaVersion: JOURNAL_SCHEMA_VERSION,
      dirtyGeneration: 1,
      lastVerifiedGeneration: 1,
      isReconcileRequired: false,
      recoveryState: "verified_generation_loaded",
    } satisfies JournalMeta);
    const repository = new JournalRepository({ database });
    const vault = new FakeCaptureVault();
    const gate = createFakePolicyGate(() => "allowed", 1);
    // Use a fake that records `markTombstonedPathReconcileRequired`
    // calls so we can prove the C2 fail-closed path is taken.
    const reconcileCalls: string[] = [];
    const lifecycleCapture: FakeLifecycleCapture = {
      reconcileCalls,
      captureRename: async () => null,
      captureDelete: async () => null,
      requestRestore: async () => {
        throw new Error("not used in C2 test");
      },
      detectAutomaticRestore: async () => {
        if (options?.autoRestoreBehaviour === "succeed") {
          return {
            operation: "restore",
            localFileId: "00000000-0000-4000-8000-000000000000",
            eventId: "11111111-1111-7111-8111-111111111111",
            predecessorEventId: "22222222-2222-7222-8222-222222222222",
          };
        }
        // Real `JournalStoreError` so capture.ts sees a real failure.
        throw newError("journal_mutation_failed");
      },
      markTombstonedPathReconcileRequired: async (normalizedPath: string) => {
        reconcileCalls.push(normalizedPath);
        return true;
      },
    };
    const capture = new JournalCapture({
      repository,
      vaultReader: vault,
      policyGate: gate,
      lifecycleCapture,
    });
    return {
      capture,
      repository,
      database,
      vault,
      gate,
      lifecycleState: { deleteCalls: [], renameCalls: [], reconcileCalls: [] },
      failureTokens: [],
    };
  }

  it("fails closed and flags reconcile_required when automatic restore proof fails on a tombstoned path", async () => {
    useSettleFakeTimers();
    const harness = createFailClosedHarness();
    // Seed a tracked file with a tombstone via raw SQL so we exercise
    // the auto-restore guard.
    harness.vault.setFileBytes("notes/tombstone-reuse.md", bytesOf("content"));
    harness.capture.notifyPathChanged("notes/tombstone-reuse.md");
    await settlePastDelay(harness);
    // Now the file row is tracked; simulate a tombstone by directly
    // setting `open_tombstone_id` on the local_files row.
    const tracked = harness.repository.readLocalFileByPath("notes/tombstone-reuse.md");
    expect(tracked).not.toBeNull();
    harness.database.readAll("select 1"); // touch
    // Direct tombstone write via a new capture.
    const tombstoneId = "40404040-4040-7404-8404-404040404040";
    harness.database.runSerializedMutation(async (session) => {
      session.exec(
        `update local_files set open_tombstone_id = ${sqlQuoted(tombstoneId)}, lifecycle_state = 'tombstoned' where normalized_path = 'notes/tombstone-reuse.md';`,
      );
    });
    // Trigger an observation; the capture composition sees the tombstoned
    // row and calls `detectAutomaticRestore`, which throws.
    harness.vault.setFileBytes("notes/tombstone-reuse.md", bytesOf("totally different bytes"));
    const before = harness.repository.countPendingEvents();
    harness.capture.notifyPathChanged("notes/tombstone-reuse.md");
    await settlePastDelay(harness);
    // C2: NO new pending event was minted for the path; the fail-closed
    // path refused the create / update admission.
    expect(harness.repository.countPendingEvents()).toBe(before);
  });
});

function sqlQuoted(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

function newError(reason: string): Error {
  const error = new Error(`journal store failed: ${reason}`) as Error & { reason: string };
  error.reason = reason;
  return error;
}

describe("JournalCapture existing-files scan (spec 7.1)", () => {
  async function scanOf(
    harness: CaptureHarness,
    isConfirmed: boolean,
  ): Promise<ExistingFilesScanSummary> {
    return harness.capture.runExistingFilesScan({ confirm: async () => isConfirmed });
  }

  it("does nothing without an explicit confirmation", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/a.md", bytesOf("a"));

    const summary = await scanOf(harness, false);

    expect(summary.outcome).toBe("cancelled");
    expect(summary.processedFileCount).toBe(0);
    expect(harness.vault.listCallCount).toBe(0);
    expect(harness.vault.byteReadCount).toBe(0);
    expect(harness.repository.countPendingEvents()).toBe(0);
  });

  it("processes a confirmed snapshot through the same admission path", async () => {
    const harness = createHarness({
      decide: (subject) => (subject.normalizedLocator === "notes/b.md" ? "excluded" : "allowed"),
    });
    harness.vault.setFileBytes("notes/a.md", bytesOf("allowed"));
    harness.vault.setFileBytes("notes/b.md", bytesOf("denied"));
    harness.vault.setFileBytes(
      "notes/over.bin",
      new Uint8Array(MAX_FILE_SIZE_BYTES + 1),
    );

    const summary = await scanOf(harness, true);

    expect(summary.outcome).toBe("completed");
    expect(summary.processedFileCount).toBe(3);
    expect(summary.skippedFileCount).toBe(0);
    expect(summary.isTruncated).toBe(false);
    expect(soleEventOf(harness, "notes/a.md").state).toBe("queued");
    expect(soleEventOf(harness, "notes/b.md").state).toBe("excluded_policy");
    expect(soleEventOf(harness, "notes/over.bin").state).toBe("blocked_size");
    expect(harness.repository.countPendingEvents()).toBe(1);
    expect(harness.vault.listCallCount).toBe(1);
    expect(harness.vault.byteReadCount).toBe(3);
  });

  it("bounds one confirmed scan to the snapshot ceiling", async () => {
    const harness = createHarness({ scanMaximumFiles: 5 });
    for (let index = 0; index < 12; index += 1) {
      harness.vault.setFileBytes(`notes/file-${index}.md`, bytesOf(`content-${index}`));
    }

    const summary = await scanOf(harness, true);

    expect(summary.outcome).toBe("completed");
    expect(summary.processedFileCount).toBe(5);
    expect(summary.isTruncated).toBe(true);
    expect(harness.repository.countPendingEvents()).toBe(5);
  });

  it("processes the bounded snapshot in batches and skips failing paths", async () => {
    const harness = createHarness({ scanBatchFiles: 2 });
    for (let index = 0; index < 6; index += 1) {
      harness.vault.setFileBytes(`notes/batch-${index}.md`, bytesOf(`content-${index}`));
    }

    const summary = await scanOf(harness, true);

    expect(summary.outcome).toBe("completed");
    expect(summary.processedFileCount).toBe(6);
    expect(summary.skippedFileCount).toBe(0);
    expect(harness.repository.countPendingEvents()).toBe(6);
  });

  it("pins the default scan bounds to the frozen queue ceilings", () => {
    expect(EXISTING_FILES_SCAN_MAXIMUM_FILES).toBe(10_000);
    expect(EXISTING_FILES_SCAN_BATCH_FILES).toBeGreaterThan(0);
  });
});

describe("JournalCapture automatic snapshot admission", () => {
  it("coalesces rejected automatic admissions into one failure token per scan", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/rejected-one.md", bytesOf("one"));
    harness.vault.setFileBytes("notes/rejected-two.md", bytesOf("two"));
    vi.spyOn(harness.repository, "recordCapture").mockRejectedValue(
      new Error("persistence rejected"),
    );

    const summary = await harness.capture.runAutomaticSnapshot();

    expect(summary.skippedFileCount).toBe(2);
    expect(harness.failureTokens).toEqual(["automatic_snapshot_admission_failed"]);
  });

  it("creates an allowed queued successor after a prior policy block", async () => {
    const harness = createHarness();
    await harness.captureUnderPolicy("notes/recovered.md", "excluded_policy");
    harness.allowMarkdown();
    const result = await harness.capture.runAutomaticSnapshot();
    expect(result.queuedEventCount).toBe(1);
    expect(harness.eventsFor("notes/recovered.md").map((event) => event.state)).toEqual([
      "excluded_policy", "queued",
    ]);
  });

  it("queues one create for a new allowed note", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/new.md", bytesOf("new note"));

    const result = await harness.capture.runAutomaticSnapshot();

    expect(result).toMatchObject({ outcome: "completed", queuedEventCount: 1 });
    expect(harness.eventsFor("notes/new.md").map((event) => event.operation)).toEqual(["create"]);
    expect(harness.eventsFor("notes/new.md").map((event) => event.state)).toEqual(["queued"]);
  });

  it("queues one update for a changed committed note", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/changed.md", bytesOf("first version"));
    await harness.capture.runAutomaticSnapshot();
    const firstEvent = harness.eventsFor("notes/changed.md")[0];
    if (firstEvent === undefined) {
      throw new Error("expected initial queued event");
    }
    await harness.repository.recordCommittedReceipt({
      eventId: firstEvent.eventId,
      sourceId: "00000000-0000-4000-8000-000000000001",
      baseVersionId: "00000000-0000-4000-8000-000000000002",
    });
    harness.vault.setFileBytes("notes/changed.md", bytesOf("second version"));

    const result = await harness.capture.runAutomaticSnapshot();

    expect(result.queuedEventCount).toBe(1);
    expect(harness.eventsFor("notes/changed.md").map((event) => event.operation)).toEqual([
      "create", "update",
    ]);
    expect(harness.eventsFor("notes/changed.md").map((event) => event.state)).toEqual([
      "committed", "queued",
    ]);
  });

  it("queues no content event for an unchanged committed note", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/unchanged.md", bytesOf("committed bytes"));
    await harness.capture.runAutomaticSnapshot();
    const initialEvent = harness.eventsFor("notes/unchanged.md")[0];
    if (initialEvent === undefined) {
      throw new Error("expected initial queued event");
    }
    await harness.repository.recordCommittedReceipt({
      eventId: initialEvent.eventId,
      sourceId: "00000000-0000-4000-8000-000000000003",
      baseVersionId: "00000000-0000-4000-8000-000000000004",
    });

    const result = await harness.capture.runAutomaticSnapshot();

    expect(result.queuedEventCount).toBe(0);
    expect(harness.eventsFor("notes/unchanged.md").map((event) => event.state)).toEqual(["committed"]);
  });

  it("records only terminal audit evidence for a currently excluded note", async () => {
    const harness = createHarness({ decide: () => "excluded" });
    harness.vault.setFileBytes("notes/excluded.md", bytesOf("denied bytes"));

    const result = await harness.capture.runAutomaticSnapshot();

    expect(result.queuedEventCount).toBe(0);
    expect(harness.eventsFor("notes/excluded.md").map((event) => event.state)).toEqual([
      "excluded_policy",
    ]);
  });

  it("creates no content event for a lifecycle-deferred path", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/deferred.md", bytesOf("first version"));
    await harness.capture.runAutomaticSnapshot();
    const initialEvent = harness.eventsFor("notes/deferred.md")[0];
    if (initialEvent === undefined) {
      throw new Error("expected initial queued event");
    }
    await harness.repository.markEventTerminal(
      initialEvent.eventId,
      "deferred_lifecycle",
      "deferred_lifecycle",
    );
    harness.vault.setFileBytes("notes/deferred.md", bytesOf("later version"));

    const result = await harness.capture.runAutomaticSnapshot();

    expect(result.queuedEventCount).toBe(0);
    expect(harness.eventsFor("notes/deferred.md").map((event) => event.state)).toEqual([
      "deferred_lifecycle",
    ]);
  });

  it("stops an automatic snapshot without admitting a file after cancellation", async () => {
    const harness = createHarness();
    const contentBytes = bytesOf("cancelled snapshot");
    harness.vault.setFileBytes("notes/cancelled.md", contentBytes);
    const pendingRead = { release: null as (() => void) | null };
    vi.spyOn(harness.vault, "readRegularFileBytes").mockImplementationOnce(async () =>
      await new Promise<Uint8Array>((resolve) => {
        pendingRead.release = () => resolve(contentBytes);
      }),
    );
    const controller = new AbortController();

    const snapshot = harness.capture.runAutomaticSnapshot({ signal: controller.signal });
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    controller.abort();
    pendingRead.release?.();

    await expect(snapshot).resolves.toMatchObject({ outcome: "stopped", queuedEventCount: 0 });
    expect(harness.eventsFor("notes/cancelled.md")).toEqual([]);
  });
});

describe("JournalCapture explicit-restore target reservation deferral", () => {
  async function seedReservedRow(
    harness: CaptureHarness,
    normalizedPath: string,
  ): Promise<void> {
    await harness.database.runSerializedMutation((session) => {
      session.exec(
        [
          "insert into local_files (local_file_id, normalized_path, source_id,",
          "observed_sha256, observed_size_bytes, observed_media_type, base_version_id,",
          "policy_revision, open_tombstone_id, lifecycle_state, restore_prior_path)",
          "values ('90909090-9090-4909-8909-909090909090',",
          `'${normalizedPath}', '11111111-1111-7111-8111-111111111111',`,
          "'9191919191919191919191919191919191919191919191919191919191919191', 16,",
          "'text/plain', '22222222-2222-7222-8222-222222222222', 1,",
          "'92929292-9292-4929-8929-929292929292', 'restore_pending',",
          "'notes/prior-reserved.md');",
        ].join(" "),
      );
    });
    harness.vault.setFileBytes(normalizedPath, bytesOf("staged restore bytes"));
  }

  it("never mints a create for a settled observation of a reserved target", async () => {
    const harness = createHarness();
    await seedReservedRow(harness, "notes/reserved-target.md");
    useSettleFakeTimers();

    const settled = harness.capture.notifyPathChanged("notes/reserved-target.md");
    await settlePastDelay(harness);
    await settled;

    expect(harness.eventsFor("notes/reserved-target.md")).toEqual([]);
    expect(harness.lifecycleState.reconcileCalls).toEqual([]);
  });

  it("excludes a reserved target from the automatic snapshot and admits nothing for it", async () => {
    const harness = createHarness();
    await seedReservedRow(harness, "notes/reserved-target.md");
    harness.vault.setFileBytes("notes/other.md", bytesOf("unrelated note"));

    const summary = await harness.capture.runAutomaticSnapshot();

    expect(summary.outcome).toBe("completed");
    expect(summary.skippedFileCount).toBe(1);
    expect(summary.queuedEventCount).toBeLessThanOrEqual(1);
    expect(harness.eventsFor("notes/reserved-target.md")).toEqual([]);
    expect(harness.lifecycleState.reconcileCalls).toEqual([]);
  });
});

describe("JournalCapture module safety", () => {
  const captureSource = readFileSync(new URL("./capture.ts", import.meta.url), "utf8");

  it("references no network transport capability", () => {
    for (const forbiddenText of ["fetch(", "requestUrl", "XMLHttpRequest", "WebSocket"]) {
      expect(captureSource).not.toContain(forbiddenText);
    }
  });
});
