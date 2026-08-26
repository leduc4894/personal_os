/**
 * Tests of the exact echo suppression surface (device cursor and manifest
 * reconciliation, task 10, spec 8.2).
 *
 * Echo suppression is exact or it does not happen: a watcher observation
 * is consumed only when EVERY applicable operand — the server event
 * sequence, the source, the operation, the applicable prior/target
 * locators and the expected final fingerprint — matches the durable
 * marker. A mismatch remains a real watcher event with the marker
 * retained. Restart snapshot proof may consume an exact marker; elapsed
 * time may not — there is no time-window wildcard anywhere in the
 * module.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { FrozenFingerprint } from "../journal/contracts";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import type { EchoMarker, VaultObservation } from "./contracts";
import { createEchoSuppressor } from "./echo-suppression";
import type { EchoSuppressor } from "./echo-suppression";
import { DeviceSyncRepository } from "./repository";

/** The real sql.js WebAssembly engine drives every suppression test (spec 6.1). */
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
const OTHER_SOURCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const EVENT_SEQUENCE = 1;
const SHA256_A = "a".repeat(64);
const SHA256_B = "b".repeat(64);
const SHA256_C = "c".repeat(64);

function fingerprintOf(sha256: string, sizeBytes = 12): FrozenFingerprint {
  return { sha256, sizeBytes, mediaType: "text/markdown" };
}

function createHarness(): {
  readonly suppressor: EchoSuppressor;
  readonly repository: DeviceSyncRepository;
  readonly database: SqliteDatabase;
} {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  const repository = new DeviceSyncRepository({ database });
  const suppressor = createEchoSuppressor({ repository, database });
  return { suppressor, repository, database };
}

const EXPECTED_MARKER: EchoMarker = {
  eventSequence: EVENT_SEQUENCE,
  sourceId: SOURCE_ID,
  operation: "updated",
  priorLocator: "notes/echo.md",
  targetLocator: null,
  finalFingerprint: fingerprintOf(SHA256_B, 12),
};

function observationOfMarker(
  marker: EchoMarker,
  overrides: Partial<VaultObservation> = {},
): VaultObservation {
  return {
    eventSequence: marker.eventSequence,
    sourceId: marker.sourceId,
    operation: marker.operation,
    priorLocator: marker.priorLocator,
    targetLocator: marker.targetLocator,
    fingerprint: marker.finalFingerprint,
    ...overrides,
  };
}

// --- exact operand matching (spec 8.2) ---------------------------------------------------------

describe("EchoSuppressor exact operand matching", () => {
  it("consumes the marker exactly once when every operand matches", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);

    expect(await suppressor.matchAndConsume(observationOfMarker(EXPECTED_MARKER))).toBe(true);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toBeNull();
    expect(await suppressor.matchAndConsume(observationOfMarker(EXPECTED_MARKER))).toBe(false);
  });

  it("does not suppress the same path with different bytes", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);
    expect(
      await suppressor.matchAndConsume(
        observationOfMarker(EXPECTED_MARKER, { fingerprint: fingerprintOf(SHA256_C, 12) }),
      ),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).not.toBeNull();
  });

  it("keeps the marker when the observation carries a different source", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);
    expect(
      await suppressor.matchAndConsume(
        observationOfMarker(EXPECTED_MARKER, { sourceId: OTHER_SOURCE_ID }),
      ),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(EXPECTED_MARKER);
  });

  it("keeps the marker when the observation carries a different operation", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);
    expect(
      await suppressor.matchAndConsume(observationOfMarker(EXPECTED_MARKER, { operation: "created" })),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(EXPECTED_MARKER);
  });

  it("keeps the marker when a locator operand differs", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);
    expect(
      await suppressor.matchAndConsume(
        observationOfMarker(EXPECTED_MARKER, { priorLocator: "notes/other.md" }),
      ),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(EXPECTED_MARKER);
  });

  it("never matches an observation without a source or operation", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);
    expect(
      await suppressor.matchAndConsume(
        observationOfMarker(EXPECTED_MARKER, { sourceId: null, operation: null }),
      ),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(EXPECTED_MARKER);
  });

  it("answers false for an event sequence without a marker", async () => {
    const { suppressor } = createHarness();
    expect(await suppressor.matchAndConsume(observationOfMarker(EXPECTED_MARKER))).toBe(false);
  });
});

// --- restart proof and the absence of a time window ----------------------------------------------

describe("EchoSuppressor restart proof", () => {
  it("consumes an exact marker persisted before a journal restart", async () => {
    const first = createHarness();
    await first.repository.recordEchoMarker(EXPECTED_MARKER);

    // Restart: a fresh database image, repository and suppressor — the
    // marker survives only because it is durable.
    const reopenedDatabase = SqliteDatabase.openFromImage(
      engineModule,
      first.database.exportImage(),
    );
    const reopenedRepository = new DeviceSyncRepository({ database: reopenedDatabase });
    const reopenedSuppressor = createEchoSuppressor({
      repository: reopenedRepository,
      database: reopenedDatabase,
    });

    expect(
      await reopenedSuppressor.matchAndConsume(observationOfMarker(EXPECTED_MARKER)),
    ).toBe(true);
    expect(reopenedRepository.readEchoMarker(EVENT_SEQUENCE)).toBeNull();
  });

  it("keeps a mismatched marker across a restart instead of expiring it", async () => {
    const first = createHarness();
    await first.repository.recordEchoMarker(EXPECTED_MARKER);

    const reopenedDatabase = SqliteDatabase.openFromImage(
      engineModule,
      first.database.exportImage(),
    );
    const reopenedRepository = new DeviceSyncRepository({ database: reopenedDatabase });
    const reopenedSuppressor = createEchoSuppressor({
      repository: reopenedRepository,
      database: reopenedDatabase,
    });

    expect(
      await reopenedSuppressor.matchAndConsume(
        observationOfMarker(EXPECTED_MARKER, { fingerprint: fingerprintOf(SHA256_C, 12) }),
      ),
    ).toBe(false);
    expect(reopenedRepository.readEchoMarker(EVENT_SEQUENCE)).toEqual(EXPECTED_MARKER);
  });

  it("admits no time-window wildcard — elapsed time can never consume a marker", () => {
    const moduleSource = readFileSync(
      new URL("./echo-suppression.ts", import.meta.url),
      "utf-8",
    );
    for (const forbiddenText of ["Date.now", "performance.now", "setTimeout", "elapsed"]) {
      expect(moduleSource).not.toContain(forbiddenText);
    }
  });
});

// --- the watcher-facing locator observations ------------------------------------------------------

describe("EchoSuppressor watcher content observations", () => {
  it("consumes the exact marker for a settled create/modify observation", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);

    expect(
      await suppressor.consumeContentObservation({
        normalizedLocator: "notes/echo.md",
        sourceId: SOURCE_ID,
        fingerprint: fingerprintOf(SHA256_B, 12),
      }),
    ).toBe(true);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toBeNull();
  });

  it("keeps the marker when the settled bytes differ", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);

    expect(
      await suppressor.consumeContentObservation({
        normalizedLocator: "notes/echo.md",
        sourceId: SOURCE_ID,
        fingerprint: fingerprintOf(SHA256_A, 10),
      }),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(EXPECTED_MARKER);
  });

  it("keeps the marker for an untracked path (no source identity)", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(EXPECTED_MARKER);

    expect(
      await suppressor.consumeContentObservation({
        normalizedLocator: "notes/echo.md",
        sourceId: null,
        fingerprint: fingerprintOf(SHA256_B, 12),
      }),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(EXPECTED_MARKER);
  });

  it("never consumes a lifecycle marker through a content observation", async () => {
    const { suppressor, repository } = createHarness();
    const renameMarker: EchoMarker = {
      eventSequence: EVENT_SEQUENCE,
      sourceId: SOURCE_ID,
      operation: "renamed",
      priorLocator: "notes/old.md",
      targetLocator: "notes/new.md",
      finalFingerprint: fingerprintOf(SHA256_B, 12),
    };
    await repository.recordEchoMarker(renameMarker);

    expect(
      await suppressor.consumeContentObservation({
        normalizedLocator: "notes/new.md",
        sourceId: SOURCE_ID,
        fingerprint: fingerprintOf(SHA256_B, 12),
      }),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(renameMarker);
  });
});

describe("EchoSuppressor watcher rename observations", () => {
  const renameMarker: EchoMarker = {
    eventSequence: EVENT_SEQUENCE,
    sourceId: SOURCE_ID,
    operation: "renamed",
    priorLocator: "notes/old.md",
    targetLocator: "notes/new.md",
    finalFingerprint: fingerprintOf(SHA256_B, 12),
  };

  it("consumes the exact marker for a rename observation with matching bytes", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(renameMarker);

    expect(
      await suppressor.consumeRenameObservation({
        priorLocator: "notes/old.md",
        targetLocator: "notes/new.md",
        sourceId: SOURCE_ID,
        fingerprint: fingerprintOf(SHA256_B, 12),
      }),
    ).toBe(true);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toBeNull();
  });

  it("keeps the marker when the renamed bytes do not match", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(renameMarker);

    expect(
      await suppressor.consumeRenameObservation({
        priorLocator: "notes/old.md",
        targetLocator: "notes/new.md",
        sourceId: SOURCE_ID,
        fingerprint: fingerprintOf(SHA256_C, 12),
      }),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(renameMarker);
  });

  it("keeps the marker when the target locator differs", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(renameMarker);

    expect(
      await suppressor.consumeRenameObservation({
        priorLocator: "notes/old.md",
        targetLocator: "notes/elsewhere.md",
        sourceId: SOURCE_ID,
        fingerprint: fingerprintOf(SHA256_B, 12),
      }),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(renameMarker);
  });
});

describe("EchoSuppressor watcher delete observations", () => {
  const deleteMarker: EchoMarker = {
    eventSequence: EVENT_SEQUENCE,
    sourceId: SOURCE_ID,
    operation: "deleted",
    priorLocator: "notes/gone.md",
    targetLocator: null,
    finalFingerprint: null,
  };

  it("consumes the exact marker for a delete observation of a tracked source", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(deleteMarker);

    expect(
      await suppressor.consumeDeleteObservation({
        priorLocator: "notes/gone.md",
        sourceId: SOURCE_ID,
      }),
    ).toBe(true);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toBeNull();
  });

  it("keeps the marker when the deleted path maps to a different source", async () => {
    const { suppressor, repository } = createHarness();
    await repository.recordEchoMarker(deleteMarker);

    expect(
      await suppressor.consumeDeleteObservation({
        priorLocator: "notes/gone.md",
        sourceId: OTHER_SOURCE_ID,
      }),
    ).toBe(false);
    expect(repository.readEchoMarker(EVENT_SEQUENCE)).toEqual(deleteMarker);
  });
});

// --- the mobile boundary ----------------------------------------------------------------------------

describe("device-sync mobile boundary", () => {
  it("imports no Node.js, Electron or FileSystemAdapter capability at module load time", () => {
    const moduleNames = [
      "api",
      "contracts",
      "diagnostics",
      "echo-suppression",
      "atomic-vault-writer",
      "remote-event-applier",
      "repository",
      "schema",
    ];
    for (const moduleName of moduleNames) {
      const moduleSource = readFileSync(
        new URL(`./${moduleName}.ts`, import.meta.url),
        "utf-8",
      );
      // Only import/export statements carry module-load capability;
      // prose in doc comments is free to NAME the forbidden surface.
      const importLines = moduleSource
        .split("\n")
        .filter((line) => /^\s*(import|export)\b/.test(line));
      expect(importLines.length, `${moduleName}.ts must import something`).toBeGreaterThan(0);
      for (const importLine of importLines) {
        for (const forbiddenText of [
          '"node:',
          '"electron',
          '"fs',
          '"obsidian"',
          "FileSystemAdapter",
        ]) {
          expect(
            importLine.includes(forbiddenText),
            `${moduleName}.ts import "${importLine.trim()}" must not contain ${forbiddenText}`,
          ).toBe(false);
        }
      }
    }
  });
});
