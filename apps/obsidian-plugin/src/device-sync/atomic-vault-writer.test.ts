/**
 * Tests of the atomic Vault writer's safety pins (device cursor and
 * manifest reconciliation, task 10, spec 8.1, 11).
 *
 * Content applies stage verified bytes in a SAME-DIRECTORY temporary
 * sibling, verify them against the expected final fingerprint, perform a
 * narrow replace with retained rollback evidence, and verify the final
 * bytes at the target locator. Tombstones trash through the Vault trash
 * path (`Vault.trash(file, false)`) with NO hard-delete fallback — no
 * permanent-delete method is ever called. Every failure is a closed
 * stage + reason on {@link AtomicVaultWriterError}.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { FrozenFingerprint } from "../journal/contracts";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import type { PreparedRemoteApply, RemoteApplyTransition } from "./contracts";
import type { DeviceSyncRepository as DeviceSyncRepositoryPort } from "./contracts";
import {
  AtomicVaultWriterImpl,
  buildRollbackSiblingLocator,
  buildTempSiblingLocator,
  createStructuralVaultMutationSeam,
} from "./atomic-vault-writer";
import type {
  AtomicVaultWriterError,
  ContentApplyInput,
  StructuralVaultAdapterSurface,
  StructuralVaultSurface,
  VaultMutationSeam,
} from "./atomic-vault-writer";
import { DeviceSyncRepository } from "./repository";

/** The real sql.js WebAssembly engine drives every writer test (spec 6.1). */
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

const SOURCE_ID = "99999999-9999-4999-8999-999999999999";
const EVENT_ID = "88888888-8888-4888-8888-888888888888";
const TEMP_TOKEN = "77777777-7777-4777-8777-777777777777";
const EVENT_SEQUENCE = 1;

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
 * The fake Vault seam: an in-memory regular-file store that records every
 * method invocation so tests can pin the exact call surface (and prove no
 * permanent-delete method is ever called — the seam type has none, and
 * the recorded log may only ever contain the five mutating/reading
 * methods of the port).
 */
class FakeVaultSeam implements VaultMutationSeam {
  readonly files = new Map<string, Uint8Array>();
  readonly methodLog: string[] = [];
  readonly trashLog: string[] = [];
  readonly renameLog: { from: string; to: string }[] = [];
  /** Optional hook fired after each successful seam action. */
  onAction: ((method: string, from: string, to: string | null) => void) | null = null;
  /** Read ordinal (1-based) from which each locator's reads turn corrupted. */
  readonly corruptReadsFrom = new Map<string, number>();
  readonly #readCounts = new Map<string, number>();

  #record(method: string, from: string, to: string | null): void {
    this.methodLog.push(method);
    this.onAction?.(method, from, to);
  }

  setFileBytes(locator: string, bytes: Uint8Array): void {
    this.files.set(locator, bytes);
  }

  async locatorExists(locator: string): Promise<boolean> {
    this.methodLog.push("locatorExists");
    return this.files.has(locator);
  }

  async createFile(locator: string, bytes: Uint8Array): Promise<void> {
    if (this.files.has(locator)) {
      throw new Error(`seam refuses to create over ${locator}`);
    }
    this.files.set(locator, bytes);
    this.#record("createFile", locator, null);
  }

  async readBytes(locator: string): Promise<Uint8Array | null> {
    this.methodLog.push("readBytes");
    const bytes = this.files.get(locator);
    if (bytes === undefined) {
      return null;
    }
    const readCount = (this.#readCounts.get(locator) ?? 0) + 1;
    this.#readCounts.set(locator, readCount);
    const corruptFrom = this.corruptReadsFrom.get(locator);
    if (corruptFrom !== undefined && readCount === corruptFrom) {
      return new Uint8Array([...bytes, ...bytesOf("-corrupted")]);
    }
    return bytes;
  }

  async renameLocator(from: string, to: string): Promise<void> {
    const bytes = this.files.get(from);
    if (bytes === undefined) {
      throw new Error(`seam cannot rename absent ${from}`);
    }
    this.files.delete(from);
    this.files.set(to, bytes);
    this.renameLog.push({ from, to });
    this.#record("renameLocator", from, to);
  }

  async trashLocator(locator: string): Promise<void> {
    if (!this.files.delete(locator)) {
      throw new Error(`seam cannot trash absent ${locator}`);
    }
    this.trashLog.push(locator);
    this.#record("trashLocator", locator, null);
  }
}

function createHarness(
  wrapRepository: (
    repository: DeviceSyncRepository,
  ) => DeviceSyncRepositoryPort = (repository) => repository,
): {
  readonly repository: DeviceSyncRepository;
  readonly database: SqliteDatabase;
  readonly seam: FakeVaultSeam;
  readonly writer: AtomicVaultWriterImpl;
} {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  const repository = new DeviceSyncRepository({ database });
  const seam = new FakeVaultSeam();
  const writer = new AtomicVaultWriterImpl({ repository: wrapRepository(repository), seam });
  return { repository, database, seam, writer };
}

async function prepareContentApply(
  repository: DeviceSyncRepository,
  operation: PreparedRemoteApply["operation"] = "updated",
): Promise<{ readonly base: FrozenFingerprint; readonly next: FrozenFingerprint }> {
  const baseBytes = bytesOf("base content");
  const nextBytes = bytesOf("remote next content");
  const base = await fingerprintOf(baseBytes);
  const next = await fingerprintOf(nextBytes);
  await repository.prepareRemoteApply({
    eventSequence: 1,
    eventId: EVENT_ID,
    sourceId: SOURCE_ID,
    operation,
    priorLocator: operation === "created" || operation === "restored" ? null : "notes/a.md",
    targetLocator: operation === "created" || operation === "restored" ? "notes/a.md" : null,
    baseFingerprint: base,
    finalFingerprint: next,
    tempToken: TEMP_TOKEN,
    rollbackToken: null,
  });
  return { base, next };
}

function contentInput(
  base: FrozenFingerprint,
  next: FrozenFingerprint,
  overrides: Partial<ContentApplyInput> = {},
): ContentApplyInput {
  return {
    eventSequence: 1,
    operation: "updated",
    targetLocator: "notes/a.md",
    expectedFinalFingerprint: next,
    baseFingerprint: base,
    bytes: bytesOf("remote next content"),
    tempToken: TEMP_TOKEN,
    ...overrides,
  };
}

function writerErrorOf(promise: Promise<unknown>): Promise<AtomicVaultWriterError> {
  return promise.then(
    () => {
      throw new Error("expected the writer to reject");
    },
    (error: unknown) => {
      if (!(error instanceof Error && "stage" in error && "reason" in error)) {
        throw new Error(`expected an AtomicVaultWriterError, got ${String(error)}`);
      }
      return error as AtomicVaultWriterError;
    },
  );
}

// --- temporary sibling staging ---------------------------------------------------------------------

describe("AtomicVaultWriter temporary sibling staging", () => {
  it("stages the temporary sibling in the same directory and verifies its bytes", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    const stagedLocators: string[] = [];
    seam.onAction = (method, from) => {
      if (method === "createFile" && from.endsWith(".device-sync-tmp-".concat(TEMP_TOKEN))) {
        stagedLocators.push(from);
      }
    };

    const mutation = await writer.stageAndReplace(contentInput(base, next));

    // The staging sibling landed in the SAME directory with the pinned
    // hidden name, and the durable row recorded temp_verified + token.
    expect(stagedLocators).toEqual([buildTempSiblingLocator("notes/a.md", TEMP_TOKEN)]);
    expect(stagedLocators[0]?.startsWith("notes/")).toBe(true);
    expect(mutation.tempToken).toBe(TEMP_TOKEN);
    expect(repository.readUnfinishedApply()?.state).toBe("temp_verified");
    expect(repository.readUnfinishedApply()?.tempToken).toBe(TEMP_TOKEN);
  });

  it("keeps the verified old bytes in a same-directory rollback sibling after the replace", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));

    const mutation = await writer.stageAndReplace(contentInput(base, next));

    // Target carries the new verified bytes; the old bytes survive in the
    // rollback sibling until the applier's post-mutation cleanup.
    const rollbackLocator = buildRollbackSiblingLocator("notes/a.md", TEMP_TOKEN);
    expect(new TextDecoder().decode(seam.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "remote next content",
    );
    expect(new TextDecoder().decode(seam.files.get(rollbackLocator) ?? new Uint8Array())).toBe(
      "base content",
    );
    expect(seam.files.has(buildTempSiblingLocator("notes/a.md", TEMP_TOKEN))).toBe(false);
    expect(mutation.verifiedFingerprint).toEqual(next);
  });

  it("rejects a temp readback that does not hash to the expected final fingerprint", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    seam.corruptReadsFrom.set(buildTempSiblingLocator("notes/a.md", TEMP_TOKEN), 1);

    const error = await writerErrorOf(writer.stageAndReplace(contentInput(base, next)));

    expect(error.stage).toBe("verify_temp");
    expect(error.reason).toBe("device_apply_vault_failed");
    // Exact-temp cleanup: the unverifiable sibling is trashed, the target
    // bytes and the durable prepared row stay untouched.
    expect(seam.trashLog).toEqual([buildTempSiblingLocator("notes/a.md", TEMP_TOKEN)]);
    expect(new TextDecoder().decode(seam.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "base content",
    );
    expect(repository.readUnfinishedApply()?.state).toBe("prepared");
  });

  it("cleans a stale same-name temp sibling before staging a fresh one", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    const tempLocator = buildTempSiblingLocator("notes/a.md", TEMP_TOKEN);
    seam.setFileBytes(tempLocator, bytesOf("stale staged bytes"));

    await writer.stageAndReplace(contentInput(base, next));

    expect(seam.trashLog).toEqual([tempLocator]);
    expect(new TextDecoder().decode(seam.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "remote next content",
    );
  });
});

// --- the durable temp_verified ordering ---------------------------------------------------------------

describe("AtomicVaultWriter durable temp_verified ordering", () => {
  /**
   * A bound-method delegate over the real repository (the applier suite's
   * pattern): private fields keep working because every method runs with
   * the original receiver, and the plain object stays structurally
   * assignable to the writer's repository port while the wrapper records
   * every durable remote-apply transition.
   */
  function transitionRecordingRepository(
    repository: DeviceSyncRepository,
    transitions: RemoteApplyTransition[],
  ): DeviceSyncRepositoryPort {
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
    const originalTransition = repository.transitionRemoteApply.bind(repository);
    delegate["transitionRemoteApply"] = async (input: RemoteApplyTransition): Promise<void> => {
      await originalTransition(input);
      transitions.push(input);
    };
    return delegate as unknown as DeviceSyncRepositoryPort;
  }

  it("records temp_verified before the seam's first visible mutation", async () => {
    const transitions: RemoteApplyTransition[] = [];
    const durableStateAtFirstVisibleMutation: string[] = [];
    const { repository, seam, writer } = createHarness((real) =>
      transitionRecordingRepository(real, transitions),
    );
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    // The first VISIBLE mutation of an updated apply is the occupied
    // target moving aside to the hidden rollback sibling; the durable
    // row must already sit at temp_verified when it happens.
    seam.onAction = (method, from) => {
      const isTargetMovingAside = method === "renameLocator" && from === "notes/a.md";
      if (isTargetMovingAside && durableStateAtFirstVisibleMutation.length === 0) {
        durableStateAtFirstVisibleMutation.push(
          repository.readUnfinishedApply()?.state ?? "absent",
        );
      }
    };

    await writer.stageAndReplace(contentInput(base, next));

    expect(durableStateAtFirstVisibleMutation).toEqual(["temp_verified"]);
    expect(transitions).toContainEqual({
      eventSequence: EVENT_SEQUENCE,
      state: "temp_verified",
      tempToken: TEMP_TOKEN,
    });
  });

  it("records temp_verified before the rename-in that creates a created apply's target", async () => {
    const transitions: RemoteApplyTransition[] = [];
    const durableStateAtFirstVisibleMutation: string[] = [];
    const { repository, seam, writer } = createHarness((real) =>
      transitionRecordingRepository(real, transitions),
    );
    const { next } = await prepareContentApply(repository, "created");
    // The created target is absent: the first visible mutation is the
    // verified temp renaming IN to the target locator.
    seam.onAction = (method, _from, to) => {
      const isTargetRenameIn = method === "renameLocator" && to === "notes/a.md";
      if (isTargetRenameIn && durableStateAtFirstVisibleMutation.length === 0) {
        durableStateAtFirstVisibleMutation.push(
          repository.readUnfinishedApply()?.state ?? "absent",
        );
      }
    };

    await writer.stageAndReplace(
      contentInput(next, next, {
        operation: "created",
        baseFingerprint: null,
        bytes: bytesOf("remote next content"),
      }),
    );

    expect(durableStateAtFirstVisibleMutation).toEqual(["temp_verified"]);
  });
});

// --- occupied-target and base-fingerprint conflicts --------------------------------------------------

describe("AtomicVaultWriter occupied-target and base-fingerprint conflicts", () => {
  it("refuses a created apply whose target locator is already occupied", async () => {
    const { repository, seam, writer } = createHarness();
    const { next } = await prepareContentApply(repository, "created");
    seam.setFileBytes("notes/a.md", bytesOf("someone else lives here"));

    const error = await writerErrorOf(
      writer.stageAndReplace(
        contentInput(next, next, {
          operation: "created",
          baseFingerprint: null,
          bytes: bytesOf("remote next content"),
        }),
      ),
    );

    expect(error.stage).toBe("vault_mutation");
    expect(error.reason).toBe("device_manifest_target_occupied");
    expect(new TextDecoder().decode(seam.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "someone else lives here",
    );
    // The shape check refuses before anything is staged: no hidden bytes
    // are ever written (and nothing needs trashing) for a refused apply.
    expect(seam.trashLog).toEqual([]);
    expect(repository.readUnfinishedApply()?.state).toBe("prepared");
  });

  it("refuses an updated apply whose current bytes diverge from the pinned base fingerprint", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("locally diverged content"));

    const error = await writerErrorOf(writer.stageAndReplace(contentInput(base, next)));

    expect(error.stage).toBe("vault_mutation");
    expect(error.reason).toBe("device_manifest_local_diverged");
    expect(new TextDecoder().decode(seam.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "locally diverged content",
    );
    expect(repository.readUnfinishedApply()?.state).toBe("prepared");
  });

  it("refuses an updated apply whose target locator is absent", async () => {
    const { repository, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);

    const error = await writerErrorOf(writer.stageAndReplace(contentInput(base, next)));

    expect(error.stage).toBe("vault_mutation");
    expect(error.reason).toBe("device_manifest_local_diverged");
  });
});

// --- the shared primitive's failure mapping -----------------------------------------------------------

describe("AtomicVaultWriter shared-primitive failure mapping", () => {
  it("maps a refused staging write to the closed verify_temp stage and reason", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    seam.createFile = async (): Promise<void> => {
      throw new Error("vault write refused");
    };

    const error = await writerErrorOf(writer.stageAndReplace(contentInput(base, next)));

    expect(error.stage).toBe("verify_temp");
    expect(error.reason).toBe("device_apply_vault_failed");
    expect(error.retryable).toBe(true);
    // Nothing was staged, so nothing was mutated and the durable row
    // still sits at prepared.
    expect(new TextDecoder().decode(seam.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "base content",
    );
    expect(repository.readUnfinishedApply()?.state).toBe("prepared");
  });

  it("maps a failed base-proof readback to the closed local-diverged conflict reason", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    const originalReadBytes = seam.readBytes.bind(seam);
    seam.readBytes = async (locator: string): Promise<Uint8Array | null> => {
      if (locator === "notes/a.md") {
        throw new Error("vault read refused");
      }
      return await originalReadBytes(locator);
    };

    const error = await writerErrorOf(writer.stageAndReplace(contentInput(base, next)));

    // The base byte proof rides the shared primitive; its refusal keeps
    // the writer's divergence token so the applier settles it durably.
    expect(error.stage).toBe("vault_mutation");
    expect(error.reason).toBe("device_manifest_local_diverged");
    expect(error.retryable).toBe(false);
    expect(repository.readUnfinishedApply()?.state).toBe("prepared");
  });

  it("maps a refused replace to the closed vault_mutation stage with the durable proof standing", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    seam.renameLocator = async (): Promise<void> => {
      throw new Error("vault rename refused");
    };

    const error = await writerErrorOf(writer.stageAndReplace(contentInput(base, next)));

    expect(error.stage).toBe("vault_mutation");
    expect(error.reason).toBe("device_apply_vault_failed");
    expect(error.retryable).toBe(true);
    // The durable temp_verified proof precedes the refused replace, so
    // recovery resumes the staged bytes instead of re-staging them.
    expect(repository.readUnfinishedApply()?.state).toBe("temp_verified");
  });
});

// --- final verification and rollback restoration ------------------------------------------------------

describe("AtomicVaultWriter final verification and rollback", () => {
  it("restores the verified original bytes when the final verification fails", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    // The FINAL readback of the target (its second read; the first read
    // is the base verification) returns corrupted bytes.
    seam.corruptReadsFrom.set("notes/a.md", 2);

    const error = await writerErrorOf(writer.stageAndReplace(contentInput(base, next)));

    expect(error.stage).toBe("verify_final");
    expect(error.reason).toBe("device_apply_vault_failed");
    expect(error.restoredToBase).toBe(true);
    // The bad target bytes were trashed (preserved in .trash) and the
    // verified old bytes were restored through the rollback sibling.
    expect(new TextDecoder().decode(seam.files.get("notes/a.md") ?? new Uint8Array())).toBe(
      "base content",
    );
    expect(seam.files.has(buildRollbackSiblingLocator("notes/a.md", TEMP_TOKEN))).toBe(false);
    expect(repository.readUnfinishedApply()?.state).toBe("temp_verified");
  });

  it("keeps the ambiguous bytes when a failed final check has no rollback evidence", async () => {
    const { repository, seam, writer } = createHarness();
    const { next } = await prepareContentApply(repository, "created");
    seam.corruptReadsFrom.set("notes/a.md", 1);

    const error = await writerErrorOf(
      writer.stageAndReplace(
        contentInput(next, next, {
          operation: "created",
          baseFingerprint: null,
          bytes: bytesOf("remote next content"),
        }),
      ),
    );

    expect(error.stage).toBe("verify_final");
    expect(error.restoredToBase).toBe(false);
    // Preservation of ambiguous bytes: the unverified target content is
    // kept for human/policy resolution, never guessed away.
    expect(seam.files.has("notes/a.md")).toBe(true);
  });

  it("restores the prior locator when a rename's final verification fails", async () => {
    const { repository, seam, writer } = createHarness();
    const bytes = bytesOf("renamed content");
    const fingerprint = await fingerprintOf(bytes);
    await repository.prepareRemoteApply({
      eventSequence: 2,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "renamed",
      priorLocator: "notes/old.md",
      targetLocator: "notes/new.md",
      baseFingerprint: fingerprint,
      finalFingerprint: fingerprint,
      tempToken: null,
      rollbackToken: null,
    });
    seam.setFileBytes("notes/old.md", bytes);
    seam.corruptReadsFrom.set("notes/new.md", 1);

    const error = await writerErrorOf(
      writer.renameOrMove({
        eventSequence: 2,
        operation: "renamed",
        priorLocator: "notes/old.md",
        targetLocator: "notes/new.md",
        expectedFinalFingerprint: fingerprint,
      }),
    );

    expect(error.stage).toBe("verify_final");
    expect(error.restoredToBase).toBe(true);
    expect(new TextDecoder().decode(seam.files.get("notes/old.md") ?? new Uint8Array())).toBe(
      "renamed content",
    );
    expect(seam.files.has("notes/new.md")).toBe(false);
  });
});

// --- locator applies and tombstones -------------------------------------------------------------------

describe("AtomicVaultWriter rename, move and trash applies", () => {
  it("renames after verifying the prior bytes and an unoccupied target", async () => {
    const { repository, seam, writer } = createHarness();
    const bytes = bytesOf("renamed content");
    const fingerprint = await fingerprintOf(bytes);
    await repository.prepareRemoteApply({
      eventSequence: 1,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "renamed",
      priorLocator: "notes/old.md",
      targetLocator: "notes/new.md",
      baseFingerprint: fingerprint,
      finalFingerprint: fingerprint,
      tempToken: null,
      rollbackToken: null,
    });
    seam.setFileBytes("notes/old.md", bytes);

    const mutation = await writer.renameOrMove({
      eventSequence: 1,
      operation: "renamed",
      priorLocator: "notes/old.md",
      targetLocator: "notes/new.md",
      expectedFinalFingerprint: fingerprint,
    });

    expect(seam.files.has("notes/old.md")).toBe(false);
    expect(new TextDecoder().decode(seam.files.get("notes/new.md") ?? new Uint8Array())).toBe(
      "renamed content",
    );
    expect(mutation.verifiedFingerprint).toEqual(fingerprint);
  });

  it("refuses a rename onto an occupied target locator", async () => {
    const { repository, seam, writer } = createHarness();
    const bytes = bytesOf("renamed content");
    const fingerprint = await fingerprintOf(bytes);
    await repository.prepareRemoteApply({
      eventSequence: 1,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "moved",
      priorLocator: "notes/old.md",
      targetLocator: "folder/new.md",
      baseFingerprint: fingerprint,
      finalFingerprint: fingerprint,
      tempToken: null,
      rollbackToken: null,
    });
    seam.setFileBytes("notes/old.md", bytes);
    seam.setFileBytes("folder/new.md", bytesOf("occupied"));

    const error = await writerErrorOf(
      writer.renameOrMove({
        eventSequence: 1,
        operation: "moved",
        priorLocator: "notes/old.md",
        targetLocator: "folder/new.md",
        expectedFinalFingerprint: fingerprint,
      }),
    );

    expect(error.stage).toBe("vault_mutation");
    expect(error.reason).toBe("device_manifest_target_occupied");
    expect(new TextDecoder().decode(seam.files.get("notes/old.md") ?? new Uint8Array())).toBe(
      "renamed content",
    );
  });

  it("trashes the prior locator through the Vault trash path", async () => {
    const { repository, seam, writer } = createHarness();
    const bytes = bytesOf("doomed content");
    const fingerprint = await fingerprintOf(bytes);
    await repository.prepareRemoteApply({
      eventSequence: 1,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "deleted",
      priorLocator: "notes/doomed.md",
      targetLocator: null,
      baseFingerprint: fingerprint,
      finalFingerprint: null,
      tempToken: null,
      rollbackToken: null,
    });
    seam.setFileBytes("notes/doomed.md", bytes);

    const mutation = await writer.trash({
      eventSequence: 1,
      priorLocator: "notes/doomed.md",
      baseFingerprint: fingerprint,
    });

    expect(seam.files.has("notes/doomed.md")).toBe(false);
    expect(seam.trashLog).toEqual(["notes/doomed.md"]);
    expect(mutation.targetLocator).toBeNull();
    expect(mutation.verifiedFingerprint).toBeNull();
  });

  it("keeps the file when a tombstone's base fingerprint diverges", async () => {
    const { repository, seam, writer } = createHarness();
    const bytes = bytesOf("diverged content");
    const fingerprint = await fingerprintOf(bytesOf("something else"));
    await repository.prepareRemoteApply({
      eventSequence: 1,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "deleted",
      priorLocator: "notes/doomed.md",
      targetLocator: null,
      baseFingerprint: fingerprint,
      finalFingerprint: null,
      tempToken: null,
      rollbackToken: null,
    });
    seam.setFileBytes("notes/doomed.md", bytes);

    const error = await writerErrorOf(
      writer.trash({ eventSequence: 1, priorLocator: "notes/doomed.md", baseFingerprint: fingerprint }),
    );

    expect(error.stage).toBe("trash");
    expect(error.reason).toBe("device_manifest_local_diverged");
    expect(seam.files.has("notes/doomed.md")).toBe(true);
  });

  it("treats an already-absent prior locator as an idempotent completed tombstone", async () => {
    const { repository, writer } = createHarness();
    const fingerprint = await fingerprintOf(bytesOf("gone"));
    await repository.prepareRemoteApply({
      eventSequence: 1,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "deleted",
      priorLocator: "notes/doomed.md",
      targetLocator: null,
      baseFingerprint: fingerprint,
      finalFingerprint: null,
      tempToken: null,
      rollbackToken: null,
    });

    const mutation = await writer.trash({
      eventSequence: 1,
      priorLocator: "notes/doomed.md",
      baseFingerprint: fingerprint,
    });

    expect(mutation.targetLocator).toBeNull();
  });

  it("surfaces a failed target trash as the closed trash stage and reason", async () => {
    const { repository, seam, writer } = createHarness();
    const bytes = bytesOf("doomed content");
    const fingerprint = await fingerprintOf(bytes);
    await repository.prepareRemoteApply({
      eventSequence: 1,
      eventId: EVENT_ID,
      sourceId: SOURCE_ID,
      operation: "deleted",
      priorLocator: "notes/doomed.md",
      targetLocator: null,
      baseFingerprint: fingerprint,
      finalFingerprint: null,
      tempToken: null,
      rollbackToken: null,
    });
    seam.setFileBytes("notes/doomed.md", bytes);
    seam.trashLocator = async (): Promise<void> => {
      throw new Error("vault trash refused");
    };

    const error = await writerErrorOf(
      writer.trash({ eventSequence: 1, priorLocator: "notes/doomed.md", baseFingerprint: fingerprint }),
    );

    expect(error.stage).toBe("trash");
    expect(error.reason).toBe("device_apply_trash_failed");
    expect(seam.files.has("notes/doomed.md")).toBe(true);
  });

  it("calls no permanent-delete method across the whole apply surface", async () => {
    const { repository, seam, writer } = createHarness();
    const { base, next } = await prepareContentApply(repository);
    seam.setFileBytes("notes/a.md", bytesOf("base content"));
    await writer.stageAndReplace(contentInput(base, next));

    const permanentDeleteMethods = [
      "delete",
      "deleteFile",
      "remove",
      "hardDelete",
      "unlink",
      "rm",
    ];
    for (const method of permanentDeleteMethods) {
      expect(seam.methodLog.includes(method)).toBe(false);
    }
  });
});

// --- the structural Obsidian Vault binding ----------------------------------------------------------

describe("AtomicVaultWriter structural Vault binding", () => {
  interface StructuralCall {
    readonly method: string;
    readonly args: readonly unknown[];
  }

  function createStructuralFake(): {
    readonly vault: StructuralVaultSurface;
    readonly calls: StructuralCall[];
  } {
    const files = new Map<string, Uint8Array>();
    const calls: StructuralCall[] = [];
    const record = (method: string, ...args: readonly unknown[]): void => {
      calls.push({ method, args });
    };
    const vault = {
      getAbstractFileByPath(path: string) {
        record("getAbstractFileByPath", path);
        return files.has(path) ? { path } : null;
      },
      async createBinary(path: string, data: ArrayBuffer) {
        record("createBinary", path);
        files.set(path, new Uint8Array(data));
      },
      async readBinary(path: string) {
        record("readBinary", path);
        const bytes = files.get(path);
        if (bytes === undefined) {
          throw new Error("file not found");
        }
        return bytes.buffer.slice(
          bytes.byteOffset,
          bytes.byteOffset + bytes.byteLength,
        ) as ArrayBuffer;
      },
      async rename(file: { readonly path: string }, newPath: string) {
        record("rename", file, newPath);
        const bytes = files.get(file.path);
        if (bytes === undefined) {
          throw new Error("file not found");
        }
        files.delete(file.path);
        files.set(newPath, bytes);
      },
      async trash(file: { readonly path: string }, system: boolean) {
        record("trash", file, system);
        files.delete(file.path);
      },
      // Poison pills: any permanent-delete reach must fail the test loudly.
      async delete(file: { readonly path: string }): Promise<void> {
        record("delete", file);
        throw new Error("permanent delete must never be called");
      },
      async remove(file: { readonly path: string }): Promise<void> {
        record("remove", file);
        throw new Error("permanent remove must never be called");
      },
    };
    return { vault: vault as unknown as StructuralVaultSurface, calls };
  }

  it("binds the seam to the structural surface for staging, renaming and reading", async () => {
    const { vault, calls } = createStructuralFake();
    const seam = createStructuralVaultMutationSeam(vault);
    const bytes = bytesOf("through the structural surface");
    await seam.createFile("notes/typed.md", bytes);
    expect(await seam.locatorExists("notes/typed.md")).toBe(true);
    expect(new TextDecoder().decode((await seam.readBytes("notes/typed.md")) ?? new Uint8Array())).toBe(
      "through the structural surface",
    );
    await seam.renameLocator("notes/typed.md", "notes/renamed.md");
    expect(await seam.locatorExists("notes/typed.md")).toBe(false);
    const usedMethods = new Set(calls.map((call) => call.method));
    expect(usedMethods.has("delete")).toBe(false);
    expect(usedMethods.has("remove")).toBe(false);
  });

  it("always trashes with system=false and never reaches a permanent delete", async () => {
    const { vault, calls } = createStructuralFake();
    const seam = createStructuralVaultMutationSeam(vault);
    await seam.createFile("notes/doomed.md", bytesOf("doomed"));
    await seam.trashLocator("notes/doomed.md");

    const trashCalls = calls.filter((call) => call.method === "trash");
    expect(trashCalls.length).toBe(1);
    expect(trashCalls[0]?.args[1]).toBe(false);
    expect(calls.some((call) => call.method === "delete" || call.method === "remove")).toBe(false);
  });

  it("routes hidden siblings through the data adapter the Vault index cannot see", async () => {
    // The live Desktop gate (2026-08-27) proved the Vault index never lists
    // dot-prefixed paths — `createBinary` succeeds while `getAbstractFileByPath`
    // stays null and `readBinary` throws — so every hidden-sibling operation
    // must ride the adapter surface instead.
    const { vault, calls } = createStructuralFake();
    const adapterFiles = new Map<string, Uint8Array>();
    const adapterCalls: string[] = [];
    const adapter: StructuralVaultAdapterSurface = {
      async exists(path) {
        adapterCalls.push(`exists:${path}`);
        return adapterFiles.has(path);
      },
      async readBinary(path) {
        adapterCalls.push(`readBinary:${path}`);
        const bytes = adapterFiles.get(path);
        if (bytes === undefined) {
          throw new Error("adapter file not found");
        }
        return bytes.buffer.slice(
          bytes.byteOffset,
          bytes.byteOffset + bytes.byteLength,
        ) as ArrayBuffer;
      },
      async writeBinary(path, data) {
        adapterCalls.push(`writeBinary:${path}`);
        adapterFiles.set(path, new Uint8Array(data));
      },
      async rename(fromPath, toPath) {
        adapterCalls.push(`rename:${fromPath}`);
        const bytes = adapterFiles.get(fromPath);
        if (bytes === undefined) {
          throw new Error("adapter file not found");
        }
        adapterFiles.delete(fromPath);
        adapterFiles.set(toPath, bytes);
      },
      async remove(path) {
        adapterCalls.push(`remove:${path}`);
        adapterFiles.delete(path);
      },
    };
    const seam = createStructuralVaultMutationSeam(vault, adapter);
    const temp = "notes/.a.md.device-sync-tmp-token";
    const rollback = "notes/.a.md.device-sync-rbk-token";
    // The visible target exists on disk (the adapter sees it) and in the
    // Vault index — the narrow replace's first rename moves it to the
    // hidden rollback sibling through the adapter.
    await vault.createBinary("notes/a.md", bytesOf("base bytes").buffer as ArrayBuffer);
    adapterFiles.set("notes/a.md", bytesOf("base bytes"));

    // The Vault fake above happily tracks dot-paths; the seam must not ask
    // it to — every hidden-sibling operation rides the adapter.
    await seam.createFile(temp, bytesOf("staged"));
    expect(await seam.locatorExists(temp)).toBe(true);
    expect(new TextDecoder().decode((await seam.readBytes(temp)) ?? new Uint8Array())).toBe(
      "staged",
    );
    await seam.renameLocator("notes/a.md", rollback);
    await seam.renameLocator(temp, "notes/a.md");
    expect(await seam.locatorExists("notes/a.md")).toBe(true);
    await seam.trashLocator(rollback);

    const vaultMethodsOnHidden = calls.filter(
      (call) => call.args.some((arg) => typeof arg === "string" && arg.startsWith("notes/.")),
    );
    expect(vaultMethodsOnHidden).toEqual([]);
    expect(adapterCalls).toEqual([
      `writeBinary:${temp}`,
      `exists:${temp}`,
      `exists:${temp}`,
      `readBinary:${temp}`,
      `rename:notes/a.md`,
      `rename:${temp}`,
      `remove:${rollback}`,
    ]);
  });

  it("fails closed when a hidden sibling is touched without an adapter surface", async () => {
    const { vault } = createStructuralFake();
    const seam = createStructuralVaultMutationSeam(vault);
    await expect(seam.locatorExists("notes/.a.md.device-sync-tmp-token")).rejects.toThrow(
      "atomic vault writer failed: device_apply_vault_failed",
    );
  });

  it("serves an index-lagged visible target through the data adapter", async () => {
    // The live Desktop gate (2026-08-27) proved the Vault index lags an
    // adapter-level rename-in: `getAbstractFileByPath` stays null for a
    // moment after the bytes already sit on disk, so the final
    // verification of every apply's first attempt failed with
    // `device_apply_vault_failed` until the index caught up. The bytes on
    // disk are the truth: the existence check and the read of a
    // VISIBLE locator the index misses ride the raw adapter whenever one
    // is bound (without an adapter the index stays the only truth).
    const { vault } = createStructuralFake();
    const adapterFiles = new Map<string, Uint8Array>([
      ["notes/renamed-in.md", bytesOf("just renamed bytes")],
    ]);
    const adapter: StructuralVaultAdapterSurface = {
      async exists(path) {
        return adapterFiles.has(path);
      },
      async readBinary(path) {
        const fileBytes = adapterFiles.get(path);
        if (fileBytes === undefined) {
          throw new Error("adapter file not found");
        }
        return fileBytes.buffer.slice(
          fileBytes.byteOffset,
          fileBytes.byteOffset + fileBytes.byteLength,
        ) as ArrayBuffer;
      },
      async writeBinary(path, data) {
        adapterFiles.set(path, new Uint8Array(data));
      },
      async rename(fromPath, toPath) {
        const fileBytes = adapterFiles.get(fromPath);
        if (fileBytes === undefined) {
          throw new Error("adapter file not found");
        }
        adapterFiles.delete(fromPath);
        adapterFiles.set(toPath, fileBytes);
      },
      async remove(path) {
        adapterFiles.delete(path);
      },
    };
    const seam = createStructuralVaultMutationSeam(vault, adapter);
    // The Vault fake's index has never seen the file; the adapter has it.
    expect(vault.getAbstractFileByPath("notes/renamed-in.md")).toBeNull();
    expect(await seam.locatorExists("notes/renamed-in.md")).toBe(true);
    expect(new TextDecoder().decode((await seam.readBytes("notes/renamed-in.md")) ?? new Uint8Array())).toBe(
      "just renamed bytes",
    );
    // An absent locator stays absent through the adapter fallback.
    expect(await seam.locatorExists("notes/absent.md")).toBe(false);
    expect(await seam.readBytes("notes/absent.md")).toBeNull();
  });
});
