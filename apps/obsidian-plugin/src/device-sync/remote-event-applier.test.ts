/**
 * Tests of the crash-safe remote apply state machine (device cursor and
 * manifest reconciliation, task 10, spec 8.1, 10.3, 11).
 *
 * Every apply persists `prepared` plus the exact echo marker BEFORE any
 * Vault mutation; content applies verify staging bytes, persist
 * `temp_verified`, perform the narrow replace with rollback evidence,
 * verify the final bytes, persist `vault_mutated`, then terminalize the
 * cursor in one journal generation. The crash-injection matrix restarts
 * the journal from a real exported image at every crash point of
 * create / update / rename / move / delete / restore and requires exact
 * temp cleanup or resume, rollback restoration, preservation of
 * ambiguous bytes, and no cursor advancement on a retryable failure.
 * Every catch site surfaces exactly one closed `apply_failure` stage.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { FrozenFingerprint } from "../journal/contracts";
import { JournalPersistence } from "../journal/persistence";
import type { JournalFileStore } from "../journal/persistence";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase, journalStoreError } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import type { VerifiedDownload } from "./api";
import { DeviceSyncApiError } from "./api";
import type { DeviceSyncEvent, DownloadSourceVersionInput } from "./api";
import type {
  DeviceSyncDiagnostics,
  DeviceSyncRepository as DeviceSyncRepositoryPort,
} from "./contracts";
import { AtomicVaultWriterImpl, buildRollbackSiblingLocator, buildTempSiblingLocator } from "./atomic-vault-writer";
import type { VaultMutationSeam } from "./atomic-vault-writer";
import { createRemoteEventApplier } from "./remote-event-applier";
import type { RemoteEventApplier } from "./remote-event-applier";
import { DeviceSyncRepository } from "./repository";
import type { DeviceSyncRepositoryDatabase } from "./repository";

/** The real sql.js WebAssembly engine drives every applier test (spec 6.1). */
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
  BASE_FINGERPRINT = await fingerprintOf(BASE_BYTES);
  NEXT_FINGERPRINT = await fingerprintOf(NEXT_BYTES);
});

const SOURCE_ID = "99999999-9999-4999-8999-999999999999";
const EVENT_ID = "88888888-8888-4888-8888-888888888888";
const BASE_BYTES = new TextEncoder().encode("base content");
const NEXT_BYTES = new TextEncoder().encode("remote next content");
let BASE_FINGERPRINT: FrozenFingerprint;
let NEXT_FINGERPRINT: FrozenFingerprint;

function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

async function fingerprintOf(bytes: Uint8Array): Promise<FrozenFingerprint> {
  return { sha256: await sha256Hex(bytes), sizeBytes: bytes.byteLength, mediaType: "text/markdown" };
}

/** The simulated process death: after it fires, the harness restarts from an exported image. */
class CrashSignal extends Error {
  constructor() {
    super("simulated process death");
    this.name = "CrashSignal";
  }
}

// --- the fake Vault seam -----------------------------------------------------------------------------

class FakeVaultSeam implements VaultMutationSeam {
  readonly files = new Map<string, Uint8Array>();
  readonly trashLog: string[] = [];
  /** Hook fired after each successful seam action; a throw models a crash. */
  onAction: ((method: string, from: string, to: string | null) => void) | null = null;
  /** Locators whose seam calls must reject before mutating (a permanent local failure). */
  readonly failRenameFor = new Set<string>();
  readonly failTrashFor = new Set<string>();

  setFileBytes(locator: string, bytes: Uint8Array): void {
    this.files.set(locator, bytes);
  }

  #record(method: string, from: string, to: string | null): void {
    this.onAction?.(method, from, to);
  }

  async locatorExists(locator: string): Promise<boolean> {
    return this.files.has(locator);
  }

  async createFile(locator: string, bytes: Uint8Array): Promise<void> {
    this.files.set(locator, bytes);
    this.#record("createFile", locator, null);
  }

  async readBytes(locator: string): Promise<Uint8Array | null> {
    return this.files.get(locator) ?? null;
  }

  async renameLocator(from: string, to: string): Promise<void> {
    if (this.failRenameFor.has(from) || this.failRenameFor.has(to)) {
      throw new Error("vault rename failed");
    }
    const bytes = this.files.get(from);
    if (bytes === undefined) {
      throw new Error(`seam cannot rename absent ${from}`);
    }
    this.files.delete(from);
    this.files.set(to, bytes);
    this.#record("renameLocator", from, to);
  }

  async trashLocator(locator: string): Promise<void> {
    if (this.failTrashFor.has(locator)) {
      throw new Error("vault trash failed");
    }
    if (!this.files.delete(locator)) {
      throw new Error(`seam cannot trash absent ${locator}`);
    }
    this.trashLog.push(locator);
    this.#record("trashLocator", locator, null);
  }
}

// --- the diagnostics recorder ------------------------------------------------------------------------

class FakeDiagnostics implements DeviceSyncDiagnostics {
  readonly applyFailures: { readonly stage: string; readonly reason: string }[] = [];
  readonly otherFailures: string[] = [];

  applyFailure(stage: string, reason: string): void {
    this.applyFailures.push({ stage, reason });
  }

  cursorFailure(stage: string, reason: string): void {
    this.otherFailures.push(`cursor:${stage}:${reason}`);
  }

  reconcileFailure(stage: string, reason: string): void {
    this.otherFailures.push(`reconcile:${stage}:${reason}`);
  }

  credentialFailure(stage: string, reason: string): void {
    this.otherFailures.push(`credential:${stage}:${reason}`);
  }
}

// --- the harness --------------------------------------------------------------------------------------

interface ApplierHarness {
  readonly repository: DeviceSyncRepositoryPort;
  readonly database: SqliteDatabase;
  readonly vault: FakeVaultSeam;
  readonly applier: RemoteEventApplier;
  readonly diagnostics: FakeDiagnostics;
}

interface ApplierHarnessOptions {
  readonly seedFiles?: readonly { readonly locator: string; readonly bytes: Uint8Array }[];
  readonly downloader?: (
    input: DownloadSourceVersionInput,
  ) => Promise<VerifiedDownload>;
  readonly seamAction?: (method: string, from: string, to: string | null) => void;
  readonly repositoryOverrides?: (repository: DeviceSyncRepositoryPort) => DeviceSyncRepositoryPort;
}

function nextBytesDownload(): VerifiedDownload {
  return {
    bytes: NEXT_BYTES,
    declaredSha256: NEXT_FINGERPRINT.sha256,
    sizeBytes: NEXT_BYTES.byteLength,
    mediaType: "text/markdown",
  };
}

function createApplierHarness(options: ApplierHarnessOptions = {}): ApplierHarness {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  let repository: DeviceSyncRepositoryPort = new DeviceSyncRepository({ database });
  if (options.repositoryOverrides !== undefined) {
    repository = options.repositoryOverrides(repository);
  }
  const vault = new FakeVaultSeam();
  if (options.seamAction !== undefined) {
    vault.onAction = options.seamAction;
  }
  for (const seed of options.seedFiles ?? []) {
    vault.setFileBytes(seed.locator, seed.bytes);
  }
  const diagnostics = new FakeDiagnostics();
  const writer = new AtomicVaultWriterImpl({ repository, seam: vault });
  const applier = createRemoteEventApplier({
    repository,
    writer,
    downloader: options.downloader ?? (async (): Promise<VerifiedDownload> => nextBytesDownload()),
    diagnostics,
  });
  return { repository, database, vault, applier, diagnostics };
}

/** Restart the journal from the current image: a fresh repository, writer and applier over the SAME Vault. */
function restartApplier(harness: ApplierHarness): ApplierHarness {
  const image = harness.database.exportImage();
  const database = SqliteDatabase.openFromImage(engineModule, image);
  const repository = new DeviceSyncRepository({ database });
  // The restart disarms the previous session's crash hook.
  harness.vault.onAction = null;
  const diagnostics = new FakeDiagnostics();
  const writer = new AtomicVaultWriterImpl({ repository, seam: harness.vault });
  const applier = createRemoteEventApplier({
    repository,
    writer,
    downloader: async (): Promise<VerifiedDownload> => nextBytesDownload(),
    diagnostics,
  });
  return { repository, database, vault: harness.vault, applier, diagnostics };
}

/**
 * Prototype-delegating repository wrapper: the decorated methods run the
 * hook around the real call, everything else delegates to the real
 * repository so its private fields keep working.
 */
/**
 * A bound-method delegate over the real repository: private fields keep
 * working because every method runs with the original receiver, and the
 * returned plain object stays structurally assignable to the port.
 */
function delegateRepository(
  repository: DeviceSyncRepositoryPort,
): DeviceSyncRepositoryPort & Record<string, unknown> {
  const delegate: Record<string, unknown> = {};
  for (const key of Object.getOwnPropertyNames(Object.getPrototypeOf(repository))) {
    if (key === "constructor") {
      continue;
    }
    const value = (repository as unknown as Record<string, unknown>)[key];
    if (typeof value === "function") {
      delegate[key] = value.bind(repository);
    }
  }
  return delegate as DeviceSyncRepositoryPort & Record<string, unknown>;
}

function decorateRepository(
  repository: DeviceSyncRepositoryPort,
  decorate: {
    readonly prepareRemoteApply?: (input: unknown) => void;
    readonly afterTransitionRemoteApply?: (input: unknown) => void;
    readonly afterTerminalizeEvent?: (input: unknown) => void;
  },
): DeviceSyncRepositoryPort {
  const wrapper = delegateRepository(repository);
  if (decorate.prepareRemoteApply !== undefined) {
    const hook = decorate.prepareRemoteApply;
    const original = repository.prepareRemoteApply.bind(repository);
    wrapper["prepareRemoteApply"] = async (input: never): Promise<void> => {
      hook(input);
      await original(input);
    };
  }
  if (decorate.afterTransitionRemoteApply !== undefined) {
    const hook = decorate.afterTransitionRemoteApply;
    const original = repository.transitionRemoteApply.bind(repository);
    wrapper["transitionRemoteApply"] = async (input: never): Promise<void> => {
      await original(input);
      hook(input);
    };
  }
  if (decorate.afterTerminalizeEvent !== undefined) {
    const hook = decorate.afterTerminalizeEvent;
    const original = repository.terminalizeEvent.bind(repository);
    wrapper["terminalizeEvent"] = async (input: never): Promise<void> => {
      await original(input);
      hook(input);
    };
  }
  return wrapper;
}

/** A repository whose `terminalizeEvent` fails before any durable effect. */
function failingTerminalizeRepository(repository: DeviceSyncRepositoryPort): DeviceSyncRepositoryPort {
  const wrapper = delegateRepository(repository);
  wrapper["terminalizeEvent"] = async (): Promise<void> => {
    throw journalStoreError("journal_mutation_failed");
  };
  return wrapper;
}

// --- the device event fixtures -------------------------------------------------------------------------

function eventOf(overrides: Partial<DeviceSyncEvent> = {}): DeviceSyncEvent {
  return {
    eventId: EVENT_ID,
    eventSequence: 1,
    operation: "updated",
    sourceId: SOURCE_ID,
    originDeviceId: null,
    baseVersionId: "11111111-1111-4111-8111-111111111111",
    currentVersionId: "22222222-2222-4222-8222-222222222222",
    baseFingerprint: BASE_FINGERPRINT,
    currentFingerprint: NEXT_FINGERPRINT,
    // The REAL wire shape: an update carries its resulting locator (the
    // path active at the event's sequence) and never a prior locator.
    priorLocator: null,
    resultingLocator: "notes/a.md",
    tombstoneId: null,
    committedAt: "2026-08-26T00:00:00Z",
    ...overrides,
  };
}

const UPDATED_EVENT = (): DeviceSyncEvent => eventOf();
const CREATED_EVENT = (): DeviceSyncEvent =>
  eventOf({
    operation: "created",
    priorLocator: null,
    resultingLocator: "notes/created.md",
    baseFingerprint: null,
  });
const RESTORED_EVENT = (): DeviceSyncEvent =>
  eventOf({
    operation: "restored",
    priorLocator: null,
    resultingLocator: "notes/restored.md",
    tombstoneId: "33333333-3333-4333-8333-333333333333",
  });
const RENAMED_EVENT = (): DeviceSyncEvent =>
  eventOf({
    operation: "renamed",
    priorLocator: "notes/old.md",
    resultingLocator: "notes/new.md",
    currentFingerprint: BASE_FINGERPRINT,
  });
const MOVED_EVENT = (): DeviceSyncEvent =>
  eventOf({
    operation: "moved",
    priorLocator: "notes/old.md",
    resultingLocator: "archive/new.md",
    currentFingerprint: BASE_FINGERPRINT,
  });
const DELETED_EVENT = (): DeviceSyncEvent =>
  eventOf({
    operation: "deleted",
    priorLocator: "notes/doomed.md",
    resultingLocator: null,
    currentFingerprint: null,
    currentVersionId: null,
    tombstoneId: "44444444-4444-4444-8444-444444444444",
  });

function seedFilesOf(
  event: DeviceSyncEvent,
): readonly { readonly locator: string; readonly bytes: Uint8Array }[] {
  if (event.operation === "updated") {
    return [{ locator: "notes/a.md", bytes: BASE_BYTES }];
  }
  if (event.operation === "renamed" || event.operation === "moved") {
    return [{ locator: "notes/old.md", bytes: BASE_BYTES }];
  }
  if (event.operation === "deleted") {
    return [{ locator: "notes/doomed.md", bytes: BASE_BYTES }];
  }
  return [];
}

const tempLocatorOf = (targetLocator: string): string =>
  buildTempSiblingLocator(targetLocator, EVENT_ID);
const rollbackLocatorOf = (targetLocator: string): string =>
  buildRollbackSiblingLocator(targetLocator, EVENT_ID);

// --- the happy paths -------------------------------------------------------------------------------------

describe("RemoteEventApplier apply outcomes", () => {
  it.each([
    ["created", CREATED_EVENT],
    ["updated", UPDATED_EVENT],
    ["restored", RESTORED_EVENT],
  ] as const)("applies a remote %s event and advances the cursor once", async (_label, eventOf) => {
    const event = eventOf();
    const harness = createApplierHarness({ seedFiles: seedFilesOf(event) });
    const outcome = await harness.applier.apply(event);

    expect(outcome).toEqual({ eventSequence: 1, outcome: "applied", reason: null });
    expect(harness.repository.readState().appliedSequence).toBe(1);
    expect(harness.diagnostics.applyFailures).toEqual([]);
    expect(
      new TextDecoder().decode(harness.vault.files.get(event.resultingLocator ?? "") ?? new Uint8Array()),
    ).toBe("remote next content");
  });

  it.each([
    ["renamed", RENAMED_EVENT, "notes/old.md", "notes/new.md"],
    ["moved", MOVED_EVENT, "notes/old.md", "archive/new.md"],
  ] as const)(
    "applies a remote %s event and advances the cursor once",
    async (_label, eventOf, priorLocator, targetLocator) => {
      const event = eventOf();
      const harness = createApplierHarness({ seedFiles: seedFilesOf(event) });
      const outcome = await harness.applier.apply(event);

      expect(outcome).toEqual({ eventSequence: 1, outcome: "applied", reason: null });
      expect(harness.repository.readState().appliedSequence).toBe(1);
      expect(harness.vault.files.has(priorLocator)).toBe(false);
      expect(
        new TextDecoder().decode(harness.vault.files.get(targetLocator) ?? new Uint8Array()),
      ).toBe("base content");
    },
  );

  it("applies a remote delete event as a handled tombstone", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(DELETED_EVENT()) });
    const outcome = await harness.applier.apply(DELETED_EVENT());

    expect(outcome).toEqual({ eventSequence: 1, outcome: "tombstone_handled", reason: null });
    expect(harness.repository.readState().appliedSequence).toBe(1);
    expect(harness.vault.files.has("notes/doomed.md")).toBe(false);
  });

  it("retains the echo marker after the apply for the watcher to consume", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(UPDATED_EVENT()) });
    await harness.applier.apply(UPDATED_EVENT());
    expect(harness.repository.readEchoMarker(1)).not.toBeNull();
  });

  it("treats an already-applied sequence as an idempotent replay", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(UPDATED_EVENT()) });
    await harness.applier.apply(UPDATED_EVENT());

    const replay = await harness.applier.apply(UPDATED_EVENT());
    expect(replay.outcome).toBe("applied");
    expect(harness.repository.readState().barrierGeneration).toBeNull();
    expect(harness.diagnostics.applyFailures).toEqual([]);
  });

  it("replays a settled conflict with its durable outcome and closed reason", async () => {
    const harness = createApplierHarness({
      seedFiles: [{ locator: "notes/created.md", bytes: bytesOf("already occupied") }],
    });
    const first = await harness.applier.apply(CREATED_EVENT());
    expect(first.outcome).toBe("conflict");

    const replay = await harness.applier.apply(CREATED_EVENT());
    expect(replay).toEqual({
      eventSequence: 1,
      outcome: "conflict",
      reason: "device_manifest_target_occupied",
    });
    expect(harness.repository.readState().barrierGeneration).toBeNull();
  });

  it("replays a settled tombstone with its durable outcome", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(DELETED_EVENT()) });
    const first = await harness.applier.apply(DELETED_EVENT());
    expect(first.outcome).toBe("tombstone_handled");

    const replay = await harness.applier.apply(DELETED_EVENT());
    expect(replay).toEqual({ eventSequence: 1, outcome: "tombstone_handled", reason: null });
  });
});

// --- cursor guards and stage surfacing --------------------------------------------------------------------

describe("RemoteEventApplier cursor guards", () => {
  it("rejects an event beyond the contiguous cursor at the prepare stage", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(UPDATED_EVENT()) });
    const event = eventOf({ eventSequence: 3 });

    await expect(harness.applier.apply(event)).rejects.toMatchObject({
      reason: "device_cursor_gap",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "prepare", reason: "device_cursor_gap" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(0);
    expect(harness.repository.readState().barrierReason).toBeNull();
  });

  it("fails a genuinely locator-less update closed at the prepare stage", async () => {
    // The hydrated wire always carries the update's resulting locator
    // (task 12b); the missing-operand guard stays for the impossible
    // locator-less shape and never stages any mutation.
    const harness = createApplierHarness({ seedFiles: seedFilesOf(UPDATED_EVENT()) });
    const event = eventOf({ resultingLocator: null });

    await expect(harness.applier.apply(event)).rejects.toMatchObject({
      reason: "device_event_unavailable",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "prepare", reason: "device_event_unavailable" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(0);
    expect(harness.repository.readEchoMarker(1)).toBeNull();
  });
});

describe("RemoteEventApplier stage surfacing", () => {
  it("reports a prepare-stage failure and never mutates the Vault", async () => {
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(UPDATED_EVENT()),
      repositoryOverrides: (repository) =>
        decorateRepository(repository, {
          prepareRemoteApply: () => {
            throw new CrashSignal();
          },
        }),
    });

    await expect(harness.applier.apply(UPDATED_EVENT())).rejects.toBeInstanceOf(Error);
    expect(harness.diagnostics.applyFailures).toEqual([{ stage: "prepare", reason: "server_error" }]);
    expect(harness.repository.readState().appliedSequence).toBe(0);
    expect(harness.vault.files.size).toBe(1);
  });

  it("reports a download-stage failure once and keeps the cursor put", async () => {
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(UPDATED_EVENT()),
      downloader: () => Promise.reject(new TypeError("network down")),
    });

    await expect(harness.applier.apply(UPDATED_EVENT())).rejects.toMatchObject({
      reason: "network_offline",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "download", reason: "network_offline" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(0);
    expect(harness.repository.readUnfinishedApply()?.state).toBe("prepared");
    expect(harness.repository.readEchoMarker(1)).not.toBeNull();
  });

  it("does not double-report a download failure the wire client already reported", async () => {
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(UPDATED_EVENT()),
      downloader: () => Promise.reject(new DeviceSyncApiError("network_offline", true, null, null)),
    });

    await expect(harness.applier.apply(UPDATED_EVENT())).rejects.toMatchObject({
      reason: "network_offline",
    });
    expect(harness.diagnostics.applyFailures).toEqual([]);
  });

  it("reports a verify_temp failure when the staged bytes do not hash to the event fingerprint", async () => {
    const mismatchedBytes = bytesOf("mismatched download");
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(CREATED_EVENT()),
      downloader: async (): Promise<VerifiedDownload> => ({
        bytes: mismatchedBytes,
        declaredSha256: NEXT_FINGERPRINT.sha256,
        sizeBytes: mismatchedBytes.byteLength,
        mediaType: "text/markdown",
      }),
    });

    await expect(harness.applier.apply(CREATED_EVENT())).rejects.toMatchObject({
      reason: "device_apply_vault_failed",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "verify_temp", reason: "device_apply_vault_failed" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(0);
  });

  it("reports a vault_mutation occupied-target conflict and settles it as a durable conflict", async () => {
    const harness = createApplierHarness({
      seedFiles: [{ locator: "notes/created.md", bytes: bytesOf("already occupied") }],
    });

    const outcome = await harness.applier.apply(CREATED_EVENT());
    expect(outcome).toEqual({
      eventSequence: 1,
      outcome: "conflict",
      reason: "device_manifest_target_occupied",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "vault_mutation", reason: "device_manifest_target_occupied" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(1);
    expect(harness.repository.readUnfinishedApply()?.safeErrorCode).toBe(
      "device_manifest_target_occupied",
    );
  });

  it("reports a verify_final failure, restores the original bytes and settles a conflict", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(UPDATED_EVENT()) });
    // Corrupt exactly the final verification readback (the target's second
    // read; its first read is the base verification).
    const originalRead = harness.vault.readBytes.bind(harness.vault);
    let targetReadCount = 0;
    harness.vault.readBytes = async (locator: string): Promise<Uint8Array | null> => {
      const bytes = await originalRead(locator);
      if (locator === "notes/a.md" && bytes !== null) {
        targetReadCount += 1;
        if (targetReadCount === 2) {
          return new Uint8Array([...bytes, ...bytesOf("-corrupted")]);
        }
      }
      return bytes;
    };

    const outcome = await harness.applier.apply(UPDATED_EVENT());
    expect(outcome).toEqual({
      eventSequence: 1,
      outcome: "conflict",
      reason: "device_apply_vault_failed",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "verify_final", reason: "device_apply_vault_failed" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(1);
    expect(new TextDecoder().decode(harness.vault.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "base content",
    );
  });

  it("reports a local_commit failure and leaves the mutated apply recoverable", async () => {
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(UPDATED_EVENT()),
      repositoryOverrides: (repository) => failingTerminalizeRepository(repository),
    });

    await expect(harness.applier.apply(UPDATED_EVENT())).rejects.toMatchObject({
      reason: "journal_mutation_failed",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "local_commit", reason: "journal_mutation_failed" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(0);
    expect(harness.repository.readUnfinishedApply()?.state).toBe("vault_mutated");
  });

  it("reports a trash failure and keeps the tombstone retryable", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(DELETED_EVENT()) });
    harness.vault.failTrashFor.add("notes/doomed.md");

    await expect(harness.applier.apply(DELETED_EVENT())).rejects.toMatchObject({
      reason: "device_apply_trash_failed",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "trash", reason: "device_apply_trash_failed" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(0);
    expect(harness.vault.files.has("notes/doomed.md")).toBe(true);
  });

  it("reports a rollback-cleanup trash failure yet still completes the apply", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(UPDATED_EVENT()) });
    harness.vault.failTrashFor.add(rollbackLocatorOf("notes/a.md"));

    const outcome = await harness.applier.apply(UPDATED_EVENT());
    expect(outcome.outcome).toBe("applied");
    expect(harness.repository.readState().appliedSequence).toBe(1);
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "trash", reason: "device_apply_vault_failed" },
    ]);
  });

  it("surfaces a thrown cleanup seam failure at the trash stage and still completes", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(UPDATED_EVENT()) });
    // The rollback-sibling trash arms a readback failure inside the
    // post-mutation recovery pass: `writer.recover` itself throws.
    harness.vault.onAction = (method, from) => {
      if (method === "trashLocator" && from === rollbackLocatorOf("notes/a.md")) {
        harness.vault.readBytes = async (): Promise<Uint8Array | null> => {
          throw new Error("vault read failed during cleanup");
        };
      }
    };

    const outcome = await harness.applier.apply(UPDATED_EVENT());

    expect(outcome.outcome).toBe("applied");
    expect(harness.repository.readState().appliedSequence).toBe(1);
    expect(harness.repository.readUnfinishedApply()?.state).toBe("locally_applied");
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "trash", reason: "device_apply_vault_failed" },
    ]);
  });
});

// --- the crash-injection matrix -----------------------------------------------------------------------------

describe("RemoteEventApplier crash-injection matrix", () => {
  /** Run one apply whose controlled crash point rejects it; the durable state is what matters. */
  async function applyUntilCrash(harness: ApplierHarness, event: DeviceSyncEvent): Promise<void> {
    await harness.applier.apply(event).then(
      () => undefined,
      () => undefined,
    );
  }

  it("persists prepared and the echo marker before any Vault mutation", async () => {
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(UPDATED_EVENT()),
      seamAction: (method) => {
        if (method === "createFile") {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, UPDATED_EVENT());

    expect(harness.repository.readEchoMarker(1)).not.toBeNull();
    expect(harness.repository.readUnfinishedApply()?.state).toBe("prepared");
    expect(harness.repository.readState().appliedSequence).toBe(0);
  });

  it("crash after the temp staging of a created event: exact-temp cleanup, then clean resume", async () => {
    const event = CREATED_EVENT();
    const harness = createApplierHarness({
      seamAction: (method, from) => {
        if (method === "createFile" && from === tempLocatorOf("notes/created.md")) {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    // Exact-temp cleanup: the durable tempToken names the one sibling.
    expect(restarted.vault.files.has(tempLocatorOf("notes/created.md"))).toBe(false);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("prepared");
    expect(restarted.repository.readState().appliedSequence).toBe(0);
    // The absent target IS the verified pre-mutation expectation of a
    // create: recovery is clean — no barrier, no failure observation.
    expect(restarted.repository.readState().barrierReason).toBeNull();
    expect(restarted.diagnostics.applyFailures).toEqual([]);
    expect(restarted.diagnostics.otherFailures).toEqual([]);

    const outcome = await restarted.applier.apply(event);
    expect(outcome.outcome).toBe("applied");
    expect(restarted.repository.readState().appliedSequence).toBe(1);
  });

  it("crash at prepared before any staging of a created event: recovery is clean with no barrier", async () => {
    const event = CREATED_EVENT();
    const harness = createApplierHarness();
    // The durable crash point: prepared + marker landed, no Vault effect.
    await harness.repository.prepareRemoteApply({
      eventSequence: event.eventSequence,
      eventId: event.eventId,
      sourceId: event.sourceId,
      operation: "created",
      priorLocator: null,
      targetLocator: "notes/created.md",
      baseFingerprint: null,
      finalFingerprint: NEXT_FINGERPRINT,
      tempToken: event.eventId,
      rollbackToken: null,
    });

    await harness.applier.recoverUnfinishedApply();

    expect(harness.repository.readUnfinishedApply()?.state).toBe("prepared");
    expect(harness.repository.readState().appliedSequence).toBe(0);
    expect(harness.repository.readState().barrierReason).toBeNull();
    expect(harness.diagnostics.applyFailures).toEqual([]);
    expect(harness.diagnostics.otherFailures).toEqual([]);
  });

  it("crash after the rename-in of a created event: recovery completes and terminalizes", async () => {
    const event = CREATED_EVENT();
    const harness = createApplierHarness({
      seamAction: (method, _from, to) => {
        if (method === "renameLocator" && to === "notes/created.md") {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("locally_applied");
    expect(
      new TextDecoder().decode(restarted.vault.files.get("notes/created.md") ?? new Uint8Array()),
    ).toBe("remote next content");
    expect(restarted.vault.files.has(tempLocatorOf("notes/created.md"))).toBe(false);
  });

  it("crash after the temp staging of an updated event: original bytes preserved", async () => {
    const event = UPDATED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      seamAction: (method, from) => {
        if (method === "createFile" && from === tempLocatorOf("notes/a.md")) {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(new TextDecoder().decode(restarted.vault.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "base content",
    );
    expect(restarted.vault.files.has(tempLocatorOf("notes/a.md"))).toBe(false);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("prepared");
  });

  it("crash between the two renames of an updated event: recovery resumes the replace", async () => {
    const event = UPDATED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      seamAction: (method, _from, to) => {
        if (method === "renameLocator" && to === rollbackLocatorOf("notes/a.md")) {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    expect(harness.vault.files.has("notes/a.md")).toBe(false);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(new TextDecoder().decode(restarted.vault.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "remote next content",
    );
    expect(restarted.vault.files.has(rollbackLocatorOf("notes/a.md"))).toBe(false);
    expect(restarted.vault.files.has(tempLocatorOf("notes/a.md"))).toBe(false);
  });

  it("crash after the rename-in of an updated event: recovery verifies and cleans the rollback", async () => {
    const event = UPDATED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      seamAction: (method, _from, to) => {
        if (method === "renameLocator" && to === "notes/a.md") {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(new TextDecoder().decode(restarted.vault.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "remote next content",
    );
    expect(restarted.vault.files.has(rollbackLocatorOf("notes/a.md"))).toBe(false);
  });

  it("crash after the vault_mutated persist of an updated event: recovery terminalizes", async () => {
    const event = UPDATED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      repositoryOverrides: (repository) =>
        decorateRepository(repository, {
          afterTransitionRemoteApply: (input) => {
            const transition = input as { readonly state?: string };
            if (transition.state === "vault_mutated") {
              throw new CrashSignal();
            }
          },
        }),
    });

    await applyUntilCrash(harness, event);
    expect(harness.repository.readUnfinishedApply()?.state).toBe("vault_mutated");
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("locally_applied");
    expect(restarted.vault.files.has(rollbackLocatorOf("notes/a.md"))).toBe(false);
  });

  /** A repository decoration that crashes right after the vault_mutated persist. */
  function crashAfterVaultMutatedPersist(
    repository: DeviceSyncRepositoryPort,
  ): DeviceSyncRepositoryPort {
    return decorateRepository(repository, {
      afterTransitionRemoteApply: (input) => {
        const transition = input as { readonly state?: string };
        if (transition.state === "vault_mutated") {
          throw new CrashSignal();
        }
      },
    });
  }

  it("crash after the vault_mutated persist of a deleted event: recovery settles the tombstone", async () => {
    const event = DELETED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      repositoryOverrides: (repository) => crashAfterVaultMutatedPersist(repository),
    });

    await applyUntilCrash(harness, event);
    expect(harness.repository.readUnfinishedApply()?.state).toBe("vault_mutated");
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("locally_applied");
    expect(restarted.repository.readUnfinishedApply()?.safeErrorCode).toBeNull();
    expect(restarted.vault.files.has("notes/doomed.md")).toBe(false);
    expect(restarted.diagnostics.applyFailures).toEqual([]);
  });

  it("crash after the vault_mutated persist of a renamed event: recovery proves and terminalizes", async () => {
    const event = RENAMED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      repositoryOverrides: (repository) => crashAfterVaultMutatedPersist(repository),
    });

    await applyUntilCrash(harness, event);
    expect(harness.repository.readUnfinishedApply()?.state).toBe("vault_mutated");
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("locally_applied");
    expect(restarted.vault.files.has("notes/old.md")).toBe(false);
    expect(
      new TextDecoder().decode(restarted.vault.files.get("notes/new.md") ?? new Uint8Array()),
    ).toBe("base content");
    expect(restarted.diagnostics.applyFailures).toEqual([]);
  });

  it("crash after the vault_mutated persist of a moved event: recovery proves and terminalizes", async () => {
    const event = MOVED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      repositoryOverrides: (repository) => crashAfterVaultMutatedPersist(repository),
    });

    await applyUntilCrash(harness, event);
    expect(harness.repository.readUnfinishedApply()?.state).toBe("vault_mutated");
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("locally_applied");
    expect(restarted.vault.files.has("notes/old.md")).toBe(false);
    expect(restarted.vault.files.has("archive/new.md")).toBe(true);
    expect(restarted.diagnostics.applyFailures).toEqual([]);
  });

  it("crash after the local commit of an updated event, before server acknowledgement", async () => {
    const event = UPDATED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      repositoryOverrides: (repository) =>
        decorateRepository(repository, {
          afterTerminalizeEvent: () => {
            throw new CrashSignal();
          },
        }),
    });

    await applyUntilCrash(harness, event);
    expect(harness.repository.readState()).toMatchObject({
      appliedSequence: 1,
      acknowledgedSequence: 0,
    });

    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("locally_applied");

    await restarted.repository.recordServerAcknowledgement(1);
    await restarted.repository.recordServerAcknowledgement(1);
    expect(restarted.repository.readState().acknowledgedSequence).toBe(1);
    expect(restarted.repository.readUnfinishedApply()).toBeNull();
  });

  it("crash after the server acknowledgement: recovery finds nothing owed", async () => {
    const event = UPDATED_EVENT();
    const harness = createApplierHarness({ seedFiles: seedFilesOf(event) });
    await harness.applier.apply(event);
    await harness.repository.recordServerAcknowledgement(1);

    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();
    expect(restarted.repository.readUnfinishedApply()).toBeNull();
    expect(restarted.repository.readState().appliedSequence).toBe(1);
  });

  it("crash after the rename of a renamed event: recovery proves the mutation", async () => {
    const event = RENAMED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      seamAction: (method, _from, to) => {
        if (method === "renameLocator" && to === "notes/new.md") {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(restarted.vault.files.has("notes/old.md")).toBe(false);
    expect(
      new TextDecoder().decode(restarted.vault.files.get("notes/new.md") ?? new Uint8Array()),
    ).toBe("base content");
  });

  it("crash after the rename of a moved event: recovery proves the mutation", async () => {
    const event = MOVED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      seamAction: (method, _from, to) => {
        if (method === "renameLocator" && to === "archive/new.md") {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(restarted.vault.files.has("archive/new.md")).toBe(true);
  });

  it("crash after the trash of a deleted event: recovery terminalizes the tombstone", async () => {
    const event = DELETED_EVENT();
    const harness = createApplierHarness({
      seedFiles: seedFilesOf(event),
      seamAction: (method, from) => {
        if (method === "trashLocator" && from === "notes/doomed.md") {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("locally_applied");
    expect(restarted.vault.files.has("notes/doomed.md")).toBe(false);
  });

  it("crash after the temp staging of a restored event: exact-temp cleanup, then clean resume", async () => {
    const event = RESTORED_EVENT();
    const harness = createApplierHarness({
      seamAction: (method, from) => {
        if (method === "createFile" && from === tempLocatorOf("notes/restored.md")) {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();
    expect(restarted.vault.files.has(tempLocatorOf("notes/restored.md"))).toBe(false);
    expect(restarted.repository.readUnfinishedApply()?.state).toBe("prepared");
    // The absent target is the verified pre-mutation expectation of a
    // restore: recovery is clean — no barrier, no failure observation.
    expect(restarted.repository.readState().barrierReason).toBeNull();
    expect(restarted.diagnostics.applyFailures).toEqual([]);
    expect(restarted.diagnostics.otherFailures).toEqual([]);

    const outcome = await restarted.applier.apply(event);
    expect(outcome.outcome).toBe("applied");
  });

  it("crash after the rename-in of a restored event: recovery completes it", async () => {
    const event = RESTORED_EVENT();
    const harness = createApplierHarness({
      seamAction: (method, _from, to) => {
        if (method === "renameLocator" && to === "notes/restored.md") {
          throw new CrashSignal();
        }
      },
    });

    await applyUntilCrash(harness, event);
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();

    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(
      new TextDecoder().decode(restarted.vault.files.get("notes/restored.md") ?? new Uint8Array()),
    ).toBe("remote next content");
  });

  it("a retryable rename failure preserves verified old and new bytes and never advances the cursor", async () => {
    const event = UPDATED_EVENT();
    const harness = createApplierHarness({ seedFiles: seedFilesOf(event) });
    harness.vault.failRenameFor.add(tempLocatorOf("notes/a.md"));

    await expect(harness.applier.apply(event)).rejects.toMatchObject({
      reason: "device_apply_vault_failed",
    });
    expect(harness.diagnostics.applyFailures).toEqual([
      { stage: "vault_mutation", reason: "device_apply_vault_failed" },
    ]);
    expect(harness.repository.readState().appliedSequence).toBe(0);
    // The verified old bytes (rollback sibling) AND the verified new bytes
    // (temp sibling) both survive the failure.
    expect(
      new TextDecoder().decode(
        harness.vault.files.get(rollbackLocatorOf("notes/a.md")) ?? new Uint8Array(),
      ),
    ).toBe("base content");
    expect(
      new TextDecoder().decode(harness.vault.files.get(tempLocatorOf("notes/a.md")) ?? new Uint8Array()),
    ).toBe("remote next content");

    harness.vault.failRenameFor.clear();
    const restarted = restartApplier(harness);
    await restarted.applier.recoverUnfinishedApply();
    expect(restarted.repository.readState().appliedSequence).toBe(1);
    expect(new TextDecoder().decode(restarted.vault.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "remote next content",
    );
  });

  it("keeps ambiguous bytes preserved behind a repair barrier instead of guessing", async () => {
    const harness = createApplierHarness({ seedFiles: seedFilesOf(UPDATED_EVENT()) });
    await harness.repository.prepareRemoteApply({
      eventSequence: 1,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "updated",
      priorLocator: "notes/a.md",
      targetLocator: null,
      baseFingerprint: BASE_FINGERPRINT,
      finalFingerprint: NEXT_FINGERPRINT,
      tempToken: EVENT_ID,
      rollbackToken: null,
    });
    await harness.repository.transitionRemoteApply({
      eventSequence: 1,
      state: "temp_verified",
      tempToken: EVENT_ID,
    });
    harness.vault.setFileBytes("notes/a.md", bytesOf("mystery bytes"));

    await harness.applier.recoverUnfinishedApply();

    expect(harness.diagnostics.applyFailures).toContainEqual({
      stage: "recovery",
      reason: "device_apply_recovery_ambiguous",
    });
    expect(harness.repository.readState().barrierReason).toBe("device_apply_recovery_ambiguous");
    expect(new TextDecoder().decode(harness.vault.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "mystery bytes",
    );
    expect(harness.repository.readState().appliedSequence).toBe(0);
  });

  it("keeps an unverifiable created target preserved behind the barrier", async () => {
    const harness = createApplierHarness();
    await harness.repository.prepareRemoteApply({
      eventSequence: 1,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "created",
      priorLocator: null,
      targetLocator: "notes/created.md",
      baseFingerprint: null,
      finalFingerprint: NEXT_FINGERPRINT,
      tempToken: EVENT_ID,
      rollbackToken: null,
    });
    await harness.repository.transitionRemoteApply({
      eventSequence: 1,
      state: "temp_verified",
      tempToken: EVENT_ID,
    });
    harness.vault.setFileBytes("notes/created.md", bytesOf("unverifiable"));

    await harness.applier.recoverUnfinishedApply();

    expect(harness.repository.readState().barrierReason).toBe("device_apply_recovery_ambiguous");
    expect(
      new TextDecoder().decode(harness.vault.files.get("notes/created.md") ?? new Uint8Array()),
    ).toBe("unverifiable");
  });
});

// --- the JournalPersistence composition (task 8 carry-forward) ----------------------------------------------

describe("DeviceSyncRepository JournalPersistence composition", () => {
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

  /** The same journal slice adapter the plugin composition root builds (task 8 carry-forward). */
  function journalDatabaseOf(persistence: JournalPersistence): DeviceSyncRepositoryDatabase {
    return {
      runSerializedMutation(operation) {
        return persistence.commitGeneration(operation);
      },
      readAll(sql) {
        return persistence.readAll(sql);
      },
    };
  }

  it("keeps a persisted barrierReason across close/reopen through commitGeneration", async () => {
    const fileStore = new MemoryJournalFileStore();
    const persistence = new JournalPersistence({ fileStore, engineModule });
    await persistence.open();
    const repository = new DeviceSyncRepository({ database: journalDatabaseOf(persistence) });

    await expect(
      repository.terminalizeEvent({ eventSequence: 5, outcome: "self_origin_no_op", reason: null }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_cursor_gap");

    persistence.close();

    const reopenedPersistence = new JournalPersistence({ fileStore, engineModule });
    await reopenedPersistence.open();
    const reopenedRepository = new DeviceSyncRepository({
      database: journalDatabaseOf(reopenedPersistence),
    });

    const state = reopenedRepository.readState();
    expect(state.barrierGeneration).not.toBeNull();
    expect(state.barrierReason).toBe("device_cursor_gap");
    reopenedPersistence.close();
  });
});
