import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { JournalMeta } from "./contracts";
import {
  JOURNAL_GENERATION_FILE_PREFIX,
  JOURNAL_MANIFEST_FILE_NAME,
  JournalPersistence,
  MAX_BUFFERED_RECOVERY_PATHS,
  createVaultPluginJournalStore,
} from "./persistence";
import type {
  JournalFileStore,
  JournalGenerationManifest,
  VaultAdapterSurface,
} from "./persistence";
import type { SqliteEngineModule } from "./sqlite-database";

/** The real sql.js WebAssembly engine drives the whole generation protocol. */
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

function generationFileName(generationNumber: number): string {
  return `${JOURNAL_GENERATION_FILE_PREFIX}${generationNumber}`;
}

/** The fake journal directory: an in-memory map plus an access journal. */
class InMemoryJournalFileStore implements JournalFileStore {
  readonly files = new Map<string, ArrayBuffer>();
  readonly accessedFileNames: string[] = [];

  async exists(fileName: string): Promise<boolean> {
    this.accessedFileNames.push(`exists:${fileName}`);
    return this.files.has(fileName);
  }

  async readBinary(fileName: string): Promise<ArrayBuffer> {
    this.accessedFileNames.push(`read:${fileName}`);
    const data = this.files.get(fileName);
    if (data === undefined) {
      throw new Error("file not found");
    }
    return data.slice(0);
  }

  async writeBinary(fileName: string, data: ArrayBuffer): Promise<void> {
    this.accessedFileNames.push(`write:${fileName}`);
    this.files.set(fileName, data.slice(0));
  }

  async remove(fileName: string): Promise<void> {
    this.accessedFileNames.push(`remove:${fileName}`);
    this.files.delete(fileName);
  }
}

/**
 * The Vault fake of the final recovery scenario: a configured config
 * directory, a binary adapter over ordinary Vault files and a recording of
 * every adapter call, so the test can prove journal recovery never touches
 * Vault content.
 */
function createRecordingVaultFake(): {
  vault: VaultAdapterSurface;
  vaultFiles: Map<string, ArrayBuffer>;
  adapterCalls: string[];
} {
  const vaultFiles = new Map<string, ArrayBuffer>([
    ["notes/private-notes.md", new TextEncoder().encode("private note bytes").buffer as ArrayBuffer],
  ]);
  const adapterCalls: string[] = [];
  const vault: VaultAdapterSurface = {
    vault: {
      configDir: ".vault-config",
      adapter: {
        async exists(path: string): Promise<boolean> {
          adapterCalls.push(`exists:${path}`);
          return vaultFiles.has(path);
        },
        async readBinary(path: string): Promise<ArrayBuffer> {
          adapterCalls.push(`read:${path}`);
          const data = vaultFiles.get(path);
          if (data === undefined) {
            throw new Error("file not found");
          }
          return data.slice(0);
        },
        async writeBinary(path: string, data: ArrayBuffer): Promise<void> {
          adapterCalls.push(`write:${path}`);
          vaultFiles.set(path, data.slice(0));
        },
        async remove(path: string): Promise<void> {
          adapterCalls.push(`remove:${path}`);
          vaultFiles.delete(path);
        },
      },
    },
  };
  return { vault, vaultFiles, adapterCalls };
}

async function readManifest(store: JournalFileStore): Promise<JournalGenerationManifest> {
  const manifestBytes = await store.readBinary(JOURNAL_MANIFEST_FILE_NAME);
  return JSON.parse(new TextDecoder().decode(manifestBytes)) as JournalGenerationManifest;
}

async function openedPersistence(
  store: JournalFileStore,
): Promise<JournalPersistence> {
  const journal = new JournalPersistence({ fileStore: store, engineModule });
  await journal.open();
  return journal;
}

/** Open a fresh journal and commit once so the store holds g1 and g2. */
async function openWithTwoGenerations(): Promise<{
  store: InMemoryJournalFileStore;
  journal: JournalPersistence;
}> {
  const store = new InMemoryJournalFileStore();
  const journal = await openedPersistence(store);
  await journal.commitGeneration(() => undefined);
  return { store, journal };
}

describe("JournalPersistence first open and valid manifest load (spec 6.2)", () => {
  it("creates a fresh generation and manifest on an empty first open", async () => {
    const store = new InMemoryJournalFileStore();
    const journal = await openedPersistence(store);

    expect(journal.recoveryState).toBe("fresh_journal_created");
    expect(journal.verifiedGenerationNumber).toBe(1);
    expect(journal.isReconcileRequired).toBe(false);

    const manifest = await readManifest(store);
    expect(manifest.contract).toBe("obsidian_journal_manifest/v1");
    expect(manifest.current.generationNumber).toBe(1);
    expect(manifest.current.sizeBytes).toBeGreaterThan(0);
    expect(manifest.current.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(manifest.prior).toBeNull();

    const generationBytes = new Uint8Array(
      await store.readBinary(generationFileName(1)),
    );
    expect(generationBytes.byteLength).toBe(manifest.current.sizeBytes);
    expect(await sha256Hex(generationBytes)).toBe(manifest.current.sha256);

    expect(journal.readJournalMeta()).toEqual({
      schemaVersion: manifest.current.schemaVersion,
      dirtyGeneration: 1,
      lastVerifiedGeneration: 1,
      isReconcileRequired: false,
      recoveryState: "fresh_journal_created",
    } satisfies JournalMeta);
    journal.close();
  });

  it("loads a verified generation from a valid manifest on reopen", async () => {
    const { store, journal } = await openWithTwoGenerations();
    journal.close();

    const reopened = await openedPersistence(store);
    expect(reopened.recoveryState).toBe("verified_generation_loaded");
    expect(reopened.verifiedGenerationNumber).toBe(2);
    expect(reopened.isReconcileRequired).toBe(false);
    expect(reopened.readJournalMeta().lastVerifiedGeneration).toBe(2);
    reopened.close();
  });
});

describe("JournalPersistence generation fallbacks (spec 6.2)", () => {
  it("falls back to the prior verified generation on a torn newest image", async () => {
    const { store, journal } = await openWithTwoGenerations();
    const tornBytes = (await store.readBinary(generationFileName(2))).slice(0, 64);
    await store.writeBinary(generationFileName(2), tornBytes);
    journal.close();

    const reopened = await openedPersistence(store);
    expect(reopened.recoveryState).toBe("prior_generation_recovered");
    expect(reopened.verifiedGenerationNumber).toBe(1);
    expect(reopened.isReconcileRequired).toBe(false);
    reopened.close();
  });

  it("falls back to the prior verified generation on a digest mismatch", async () => {
    const { store, journal } = await openWithTwoGenerations();
    // A valid SQLite image of the wrong generation still fails verification.
    const wrongImage = await store.readBinary(generationFileName(1));
    await store.writeBinary(generationFileName(2), wrongImage);
    journal.close();

    const reopened = await openedPersistence(store);
    expect(reopened.recoveryState).toBe("prior_generation_recovered");
    expect(reopened.verifiedGenerationNumber).toBe(1);
    reopened.close();
  });

  it("falls back to the prior verified generation on a size-only mismatch", async () => {
    const { store, journal } = await openWithTwoGenerations();
    const manifest = await readManifest(store);
    const tampered: JournalGenerationManifest = {
      ...manifest,
      current: { ...manifest.current, sizeBytes: manifest.current.sizeBytes + 1 },
    };
    await store.writeBinary(
      JOURNAL_MANIFEST_FILE_NAME,
      new TextEncoder().encode(JSON.stringify(tampered)).buffer as ArrayBuffer,
    );
    journal.close();

    const reopened = await openedPersistence(store);
    expect(reopened.recoveryState).toBe("prior_generation_recovered");
    expect(reopened.verifiedGenerationNumber).toBe(1);
    reopened.close();
  });

  it("falls back to the prior verified generation when the newest file is missing", async () => {
    const { store, journal } = await openWithTwoGenerations();
    await store.remove(generationFileName(2));
    journal.close();

    const reopened = await openedPersistence(store);
    expect(reopened.recoveryState).toBe("prior_generation_recovered");
    expect(reopened.verifiedGenerationNumber).toBe(1);
    reopened.close();
  });

  it("rebuilds an empty reconcile-required journal when nothing verifies", async () => {
    const { vault, vaultFiles, adapterCalls } = createRecordingVaultFake();
    const store = createVaultPluginJournalStore(vault, "knowledge-workspace");
    await store.writeBinary(
      JOURNAL_MANIFEST_FILE_NAME,
      new TextEncoder().encode("{ this manifest is torn").buffer as ArrayBuffer,
    );
    await store.writeBinary(
      generationFileName(1),
      new TextEncoder().encode("not a sqlite image").buffer as ArrayBuffer,
    );

    const journal = await openedPersistence(store);
    expect(journal.recoveryState).toBe("empty_journal_rebuilt");
    expect(journal.isReconcileRequired).toBe(true);
    expect(journal.readJournalMeta().isReconcileRequired).toBe(true);

    // The supplied Vault fake stays untouched: every adapter call stays under
    // the configured plugin directory and no Vault content path is accessed.
    const pluginDirectory = ".vault-config/plugins/knowledge-workspace";
    const accessedPath = (call: string): string => call.slice(call.indexOf(":") + 1);
    for (const call of adapterCalls) {
      expect(accessedPath(call).startsWith(`${pluginDirectory}/`)).toBe(true);
    }
    expect(adapterCalls.some((call) => accessedPath(call).includes("notes/"))).toBe(false);
    expect(new Uint8Array(vaultFiles.get("notes/private-notes.md") ?? new ArrayBuffer(0)))
      .toEqual(new TextEncoder().encode("private note bytes"));

    // The rebuilt journal persists durably and survives a reopen.
    journal.close();
    const reopened = await openedPersistence(store);
    expect(reopened.recoveryState).toBe("verified_generation_loaded");
    expect(reopened.isReconcileRequired).toBe(true);
    reopened.close();
  });
});

describe("JournalPersistence probe failure handling (spec 6.2 fail-closed)", () => {
  /** Open a fresh journal and commit twice so g1 is retired and g2/g3 remain. */
  async function openWithRetiredFirstGeneration(): Promise<InMemoryJournalFileStore> {
    const store = new InMemoryJournalFileStore();
    const journal = await openedPersistence(store);
    await journal.commitGeneration(() => undefined);
    await journal.commitGeneration(() => undefined);
    journal.close();
    return store;
  }

  it("rejects open when the manifest existence probe errors", async () => {
    const store = await openWithRetiredFirstGeneration();
    const originalExists = store.exists.bind(store);
    store.exists = async (): Promise<boolean> => {
      throw new Error("transient adapter failure");
    };

    // An errored probe is not a verified-empty store: recovery must fail
    // closed instead of publishing a fresh generation-1 chain.
    await expect(openedPersistence(store)).rejects.toMatchObject({
      reason: "journal_store_unavailable",
    });

    // The verified chain survives the failed attempt untouched.
    store.exists = originalExists;
    const reopened = await openedPersistence(store);
    expect(reopened.recoveryState).toBe("verified_generation_loaded");
    expect(reopened.verifiedGenerationNumber).toBe(3);
    expect((await readManifest(store)).current.generationNumber).toBe(3);
    reopened.close();
  });

  it("rejects open when the artifact existence probe errors", async () => {
    const store = await openWithRetiredFirstGeneration();
    await store.remove(JOURNAL_MANIFEST_FILE_NAME);
    const originalExists = store.exists.bind(store);
    store.exists = async (fileName: string): Promise<boolean> => {
      if (fileName === generationFileName(1)) {
        throw new Error("transient adapter failure");
      }
      return originalExists(fileName);
    };

    await expect(openedPersistence(store)).rejects.toMatchObject({
      reason: "journal_store_unavailable",
    });

    // No fresh journal was published over the orphaned generations.
    store.exists = originalExists;
    expect(await store.exists(JOURNAL_MANIFEST_FILE_NAME)).toBe(false);
    expect(await store.exists(generationFileName(2))).toBe(true);
    expect(await store.exists(generationFileName(3))).toBe(true);
  });
});

describe("JournalPersistence generation protocol and retention (spec 6.2)", () => {
  it("verifies each written generation before publishing its manifest", async () => {
    const store = new InMemoryJournalFileStore();
    const journal = await openedPersistence(store);

    let hasFailedOnce = false;
    const originalWriteBinary = store.writeBinary.bind(store);
    store.writeBinary = async (fileName: string, data: ArrayBuffer): Promise<void> => {
      // Fail exactly the first generation-file write after open: nothing may
      // be published and the verified state must stay intact.
      if (!hasFailedOnce && fileName.startsWith(JOURNAL_GENERATION_FILE_PREFIX)) {
        hasFailedOnce = true;
        throw new Error("adapter write failed");
      }
      await originalWriteBinary(fileName, data);
    };

    await expect(journal.commitGeneration(() => undefined)).rejects.toMatchObject({
      reason: "journal_generation_write_failed",
    });

    expect(journal.verifiedGenerationNumber).toBe(1);
    const manifest = await readManifest(store);
    expect(manifest.current.generationNumber).toBe(1);
    journal.close();
  });

  it("retains exactly the current and one prior verified generation", async () => {
    const store = new InMemoryJournalFileStore();
    const journal = await openedPersistence(store);

    await journal.commitGeneration(() => undefined);
    await journal.commitGeneration(() => undefined);

    expect(await store.exists(generationFileName(1))).toBe(false);
    expect(await store.exists(generationFileName(2))).toBe(true);
    expect(await store.exists(generationFileName(3))).toBe(true);

    const manifest = await readManifest(store);
    expect(manifest.current.generationNumber).toBe(3);
    expect(manifest.prior?.generationNumber).toBe(2);
    expect(journal.verifiedGenerationNumber).toBe(3);
    journal.close();
  });

  it("serializes concurrent commits into strictly sequential generations", async () => {
    const store = new InMemoryJournalFileStore();
    const journal = await openedPersistence(store);

    const commits = await Promise.all([
      journal.commitGeneration(() => "first" as const),
      journal.commitGeneration(() => "second" as const),
    ]);

    expect(commits).toEqual(["first", "second"]);
    expect(journal.verifiedGenerationNumber).toBe(3);

    const manifest = await readManifest(store);
    expect(manifest.current.generationNumber).toBe(3);
    expect(manifest.prior?.generationNumber).toBe(2);

    const secondImage = new Uint8Array(await store.readBinary(generationFileName(3)));
    expect(await sha256Hex(secondImage)).toBe(manifest.current.sha256);
    journal.close();
  });
});

describe("JournalPersistence recovery notification buffer (spec 6.1)", () => {
  it("buffers distinct vault paths during recovery and drains them once", async () => {
    const store = new InMemoryJournalFileStore();
    const journal = new JournalPersistence({ fileStore: store, engineModule });
    journal.bufferVaultPathDuringRecovery("notes/a.md");
    journal.bufferVaultPathDuringRecovery("notes/b.md");
    journal.bufferVaultPathDuringRecovery("notes/a.md");

    await journal.open();
    expect(journal.hasRecoveryBufferOverflowed).toBe(false);

    const buffered = journal.drainBufferedVaultPaths();
    expect(buffered).toEqual(["notes/a.md", "notes/b.md"]);
    expect(journal.drainBufferedVaultPaths()).toEqual([]);
    journal.close();
  });

  it("sets reconcile_required durably when the buffer overflows", async () => {
    const store = new InMemoryJournalFileStore();
    const journal = new JournalPersistence({ fileStore: store, engineModule });
    for (let index = 0; index < MAX_BUFFERED_RECOVERY_PATHS; index += 1) {
      journal.bufferVaultPathDuringRecovery(`notes/file-${index}.md`);
    }
    expect(journal.hasRecoveryBufferOverflowed).toBe(false);

    journal.bufferVaultPathDuringRecovery("notes/one-path-too-many.md");
    expect(journal.hasRecoveryBufferOverflowed).toBe(true);

    await journal.open();
    expect(journal.isReconcileRequired).toBe(true);
    expect(journal.readJournalMeta().isReconcileRequired).toBe(true);

    // The overflowed path is dropped, never silently treated as captured.
    expect(journal.drainBufferedVaultPaths()).not.toContain("notes/one-path-too-many.md");

    // The flag stays sticky across further commits and a reopen.
    await journal.commitGeneration(() => undefined);
    expect(journal.isReconcileRequired).toBe(true);
    journal.close();

    const reopened = await openedPersistence(store);
    expect(reopened.isReconcileRequired).toBe(true);
    reopened.close();
  });
});

describe("JournalPersistence read-only query seam", () => {
  it("serves read-only queries on the opened database and fails closed after close", async () => {
    const { journal } = await openWithTwoGenerations();
    await journal.commitGeneration((session) => {
      session.exec("create table capture_probe (value text); insert into capture_probe values ('ok');");
    });

    const result = journal.readAll("select value from capture_probe;");
    expect(result[0]?.values).toEqual([["ok"]]);

    journal.close();
    let thrown: unknown = null;
    try {
      journal.readAll("select value from capture_probe;");
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toMatchObject({ reason: "journal_not_open" });
  });
});
