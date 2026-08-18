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
import { JournalRepository } from "./repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";

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
}

function createHarness(options?: {
  readonly policyRevisionNumber?: number;
  readonly decide?: (subject: CapturePolicySubject) => PolicyOutcome;
  readonly scanMaximumFiles?: number;
  readonly scanBatchFiles?: number;
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
  const gate = createFakePolicyGate(
    options?.decide ?? (() => "allowed"),
    options?.policyRevisionNumber ?? 1,
  );
  const capture = new JournalCapture({
    repository,
    vaultReader: vault,
    policyGate: gate,
    scanMaximumFiles: options?.scanMaximumFiles,
    scanBatchFiles: options?.scanBatchFiles,
  });
  return { capture, repository, database, vault, gate };
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

describe("JournalCapture lifecycle guard (spec 7.1)", () => {
  it("defers pending events when a tracked file disappears and blocks a path rebind", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/gone.md", bytesOf("content"));
    harness.capture.notifyPathChanged("notes/gone.md");
    await settlePastDelay(harness);
    expect(harness.repository.countPendingEvents()).toBe(1);

    await harness.capture.notifyPathDeleted("notes/gone.md");

    const deferredEvent = soleEventOf(harness, "notes/gone.md");
    expect(deferredEvent.state).toBe("deferred_lifecycle");
    expect(deferredEvent.safeError).toBe("deferred_lifecycle");
    expect(deferredEvent.nextEligibleRetryEpochMs).toBeNull();
    expect(harness.repository.countPendingEvents()).toBe(0);

    // A later observation of the same path never rebinds or re-queues it.
    harness.vault.setFileBytes("notes/gone.md", bytesOf("resurrected"));
    harness.capture.notifyPathChanged("notes/gone.md");
    await settlePastDelay(harness);
    expect(harness.vault.byteReadCount).toBe(1);
    expect(soleEventOf(harness, "notes/gone.md").state).toBe("deferred_lifecycle");
    expect(harness.repository.countPendingEvents()).toBe(0);
  });

  it("keeps lifecycle deferral durable across a capture restart", async () => {
    useSettleFakeTimers();
    const first = createHarness();
    first.vault.setFileBytes("notes/durable.md", bytesOf("content"));
    first.capture.notifyPathChanged("notes/durable.md");
    await settlePastDelay(first);
    await first.capture.notifyPathDeleted("notes/durable.md");

    // A fresh capture over the same journal rows still refuses the rebind.
    const restarted = new JournalCapture({
      repository: first.repository,
      vaultReader: first.vault,
      policyGate: createFakePolicyGate(() => "allowed"),
    });
    first.vault.setFileBytes("notes/durable.md", bytesOf("recreated"));
    restarted.notifyPathChanged("notes/durable.md");
    await vi.advanceTimersByTimeAsync(FILE_SETTLE_DELAY_MS + 50);
    await restarted.whenIdle();
    expect(first.vault.byteReadCount).toBe(1);
    expect(first.repository.countPendingEvents()).toBe(0);
  });

  it("defers both sides of a rename and guards the new path in session", async () => {
    useSettleFakeTimers();
    const harness = createHarness();
    harness.vault.setFileBytes("notes/original.md", bytesOf("content"));
    harness.capture.notifyPathChanged("notes/original.md");
    await settlePastDelay(harness);

    harness.vault.removeFileBytes("notes/original.md");
    harness.vault.setFileBytes("notes/renamed.md", bytesOf("content"));
    await harness.capture.notifyPathRenamed("notes/original.md", "notes/renamed.md");

    expect(soleEventOf(harness, "notes/original.md").state).toBe("deferred_lifecycle");
    expect(harness.repository.readLocalFileByPath("notes/renamed.md")).toBeNull();

    // Neither a modify event nor the scanner enqueues an inferred create.
    harness.capture.notifyPathChanged("notes/renamed.md");
    await settlePastDelay(harness);
    expect(harness.repository.readLocalFileByPath("notes/renamed.md")).toBeNull();
    expect(harness.repository.countPendingEvents()).toBe(0);

    const summary = await harness.capture.runExistingFilesScan({
      confirm: async () => true,
    });
    expect(summary.outcome).toBe("completed");
    expect(summary.processedFileCount).toBe(0);
    expect(summary.skippedFileCount).toBe(1);
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
    useSettleFakeTimers();
    const harness = createHarness();
    await harness.capture.notifyPathDeleted("notes/unknown.md");
    await harness.capture.notifyPathRenamed("notes/unknown.md", "notes/elsewhere.md");

    expect(harness.repository.readLocalFileByPath("notes/unknown.md")).toBeNull();
    expect(harness.repository.countPendingEvents()).toBe(0);
  });
});

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

describe("JournalCapture module safety", () => {
  const captureSource = readFileSync(new URL("./capture.ts", import.meta.url), "utf8");

  it("references no network transport capability", () => {
    for (const forbiddenText of ["fetch(", "requestUrl", "XMLHttpRequest", "WebSocket"]) {
      expect(captureSource).not.toContain(forbiddenText);
    }
  });
});
