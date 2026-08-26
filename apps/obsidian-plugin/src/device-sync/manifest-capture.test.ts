/**
 * Tests of the stable ordered manifest capture (device cursor and manifest
 * reconciliation, task 11, spec 12.1, 6.3).
 *
 * Capture enumerates regular Vault files in normalized-locator order,
 * fingerprints settled current bytes, emits at most 500-entry pages, and
 * digests every page and the whole run with the versioned canonical-JSON
 * grammar — the final-digest grammar is the exact server wire contract,
 * pinned here against the server's golden vectors. Local entry IDs are
 * opaque (never the raw locator), the run total is capped at 100,000, and
 * a file that changes during enumeration keeps its manifest entry at the
 * frozen barrier generation while the newer watcher observation carries a
 * generation greater than G.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import { canonicalJsonBytes, sha256Hex } from "../exclusion-policy/canonical-json";
import { JournalRepository } from "../journal/repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import { DeviceSyncApiError } from "./api";
import {
  MANIFEST_PAGE_ENTRIES,
  MAX_MANIFEST_TOTAL_ENTRIES,
  computeManifestFinalDigest,
  computeManifestPageDigest,
  createManifestCapture,
} from "./manifest-capture";
import type { ManifestCapture, ManifestEntryPage } from "./manifest-capture";

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

function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

/** The fake Vault: regular files only, with hooks for mid-enumeration change. */
class FakeManifestVault {
  readonly #files = new Map<string, Uint8Array>();
  listCallCount = 0;
  /** Hook fired before each settled read; a throw or mutation models live edits. */
  onBeforeRead: ((normalizedPath: string) => void) | null = null;

  setFileBytes(normalizedPath: string, contentBytes: Uint8Array): void {
    this.#files.set(normalizedPath, contentBytes);
  }

  removeFileBytes(normalizedPath: string): void {
    this.#files.delete(normalizedPath);
  }

  async listRegularFilePaths(): Promise<readonly string[]> {
    this.listCallCount += 1;
    return [...this.#files.keys()];
  }

  async readRegularFileBytes(normalizedPath: string): Promise<Uint8Array | null> {
    this.onBeforeRead?.(normalizedPath);
    return this.#files.get(normalizedPath) ?? null;
  }
}

interface CaptureHarness {
  readonly capture: ManifestCapture;
  readonly repository: JournalRepository;
  readonly database: SqliteDatabase;
  readonly vault: FakeManifestVault;
}

function createHarness(): CaptureHarness {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  const repository = new JournalRepository({ database });
  const vault = new FakeManifestVault();
  const capture = createManifestCapture({
    vaultReader: vault,
    identityReader: { readLocalFileByPath: (path) => repository.readLocalFileByPath(path) },
  });
  return { capture, repository, database, vault };
}

async function pagesOf(
  capture: ManifestCapture,
  barrierGeneration: number,
): Promise<ManifestEntryPage[]> {
  const pages: ManifestEntryPage[] = [];
  for await (const page of capture.capturePages(barrierGeneration)) {
    pages.push(page);
  }
  return pages;
}

// --- the canonical-JSON digest grammar -----------------------------------------------------------

describe("manifest digest grammar (spec 6.3, 7.3)", () => {
  it("mirrors the pinned server golden vectors of the final digest", async () => {
    const twoPages = [
      { pageNumber: 0, entryCount: 2, pageDigest: "a".repeat(64) },
      { pageNumber: 1, entryCount: 3, pageDigest: "c".repeat(64) },
    ];
    expect(await computeManifestFinalDigest(twoPages)).toBe(
      "b048465d54c02d7191f0a736cbc36b2339dd881847292f6a4c6dfd5b27c9b430",
    );
    // The empty manifest still commits to the grammar envelope alone.
    expect(await computeManifestFinalDigest([])).toBe(
      "b53f908bd377e91b3784d07d32ed44ca068e8029760afc38fd71cb8a260a7b1d",
    );
  });

  it("digests the ordered pages independently of the given page order", async () => {
    const pages = [
      { pageNumber: 1, entryCount: 3, pageDigest: "c".repeat(64) },
      { pageNumber: 0, entryCount: 2, pageDigest: "a".repeat(64) },
    ];
    const ordered = [...pages].reverse();
    expect(await computeManifestFinalDigest(pages)).toBe(await computeManifestFinalDigest(ordered));
  });

  it("binds the full submitted page body in the page digest", async () => {
    const entry = {
      localEntryId: "me1-" + "1".repeat(64),
      normalizedLocator: "notes/one.md",
      fingerprint: { sha256: "2".repeat(64), sizeBytes: 12, mediaType: "text/plain" },
      observationGeneration: 9,
      knownSourceId: null,
      knownVersionId: null,
    };
    const expected = await sha256Hex(
      canonicalJsonBytes({
        version: 1,
        page: 3,
        entries: [
          {
            id: entry.localEntryId,
            locator: entry.normalizedLocator,
            sha256: entry.fingerprint.sha256,
            size_bytes: entry.fingerprint.sizeBytes,
            media_type: entry.fingerprint.mediaType,
            generation: entry.observationGeneration,
            known_source_id: null,
            known_version_id: null,
          },
        ],
      }),
    );
    expect(await computeManifestPageDigest(3, [entry])).toBe(expected);
    // Every bound member matters: a changed locator, size or known source id
    // changes the digest.
    expect(
      await computeManifestPageDigest(3, [{ ...entry, normalizedLocator: "notes/two.md" }]),
    ).not.toBe(expected);
    expect(
      await computeManifestPageDigest(3, [
        { ...entry, fingerprint: { ...entry.fingerprint, sizeBytes: 13 } },
      ]),
    ).not.toBe(expected);
    expect(
      await computeManifestPageDigest(3, [
        {
          ...entry,
          knownSourceId: "99999999-9999-4999-8999-999999999999",
        },
      ]),
    ).not.toBe(expected);
  });
});

// --- ordered capture -------------------------------------------------------------------------------

describe("ManifestCapture ordered capture (spec 12.1)", () => {
  it("enumerates entries in normalized-locator order regardless of vault order", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/zeta.md", bytesOf("zeta"));
    harness.vault.setFileBytes("alpha/one.md", bytesOf("one"));
    harness.vault.setFileBytes("notes/mid.md", bytesOf("mid"));

    const pages = await pagesOf(harness.capture, 4);

    expect(pages).toHaveLength(1);
    expect(pages[0]?.entries.map((entry) => entry.normalizedLocator)).toEqual([
      "alpha/one.md",
      "notes/mid.md",
      "notes/zeta.md",
    ]);
    expect(pages[0]?.pageNumber).toBe(0);
  });

  it("fingerprints the settled current bytes with the frozen barrier generation", async () => {
    const harness = createHarness();
    const bytes = bytesOf("settled bytes");
    harness.vault.setFileBytes("notes/a.md", bytes);

    const pages = await pagesOf(harness.capture, 7);
    const entry = pages[0]?.entries[0];

    expect(entry?.fingerprint.sha256).toBe(await sha256Hex(bytes));
    expect(entry?.fingerprint.sizeBytes).toBe(bytes.byteLength);
    expect(entry?.fingerprint.mediaType).toBe("text/plain");
    expect(entry?.observationGeneration).toBe(7);
  });

  it("carries the tracked mapping's known source and version evidence", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/tracked.md", bytesOf("tracked"));
    harness.vault.setFileBytes("notes/fresh.md", bytesOf("fresh"));
    const capture = await harness.repository.recordCapture({
      normalizedPath: "notes/tracked.md",
      fingerprint: {
        sha256: await sha256Hex(bytesOf("tracked")),
        sizeBytes: 7,
        mediaType: "text/plain",
      },
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(capture.outcome).toBe("event_recorded");
    await harness.repository.recordCommittedReceipt({
      eventId: capture.outcome === "event_recorded" ? capture.event.eventId : "",
      sourceId: "99999999-9999-4999-8999-999999999999",
      baseVersionId: "88888888-8888-4888-8888-888888888888",
    });

    const pages = await pagesOf(harness.capture, 1);
    const byLocator = new Map(pages[0]?.entries.map((entry) => [entry.normalizedLocator, entry]));

    expect(byLocator.get("notes/tracked.md")?.knownSourceId).toBe(
      "99999999-9999-4999-8999-999999999999",
    );
    expect(byLocator.get("notes/tracked.md")?.knownVersionId).toBe(
      "88888888-8888-4888-8888-888888888888",
    );
    expect(byLocator.get("notes/fresh.md")?.knownSourceId).toBeNull();
    expect(byLocator.get("notes/fresh.md")?.knownVersionId).toBeNull();
  });

  it(
    "splits entries into 500-entry pages and pins the frozen page bound",
    async () => {
      expect(MANIFEST_PAGE_ENTRIES).toBe(500);
      const harness = createHarness();
      for (let index = 0; index < 501; index += 1) {
        harness.vault.setFileBytes(
          `notes/file-${String(index).padStart(4, "0")}.md`,
          bytesOf(`f${index}`),
        );
      }

      const pages = await pagesOf(harness.capture, 1);

      expect(pages.map((page) => page.entries.length)).toEqual([500, 1]);
      expect(pages.map((page) => page.pageNumber)).toEqual([0, 1]);
      expect(pages[0]?.entries[499]?.normalizedLocator).toBe("notes/file-0499.md");
      expect(pages[1]?.entries[0]?.normalizedLocator).toBe("notes/file-0500.md");
    },
    // 501 real fingerprints through the sql.js journal: the vitest 5 s
    // default overflows under coverage-instrumented parallel runs, so the
    // bound gets the same explicit headroom as the 100,000-entry cap test.
    60_000,
  );

  it("yields exactly one empty page for an empty vault", async () => {
    const harness = createHarness();
    const pages = await pagesOf(harness.capture, 2);
    expect(pages).toHaveLength(1);
    expect(pages[0]?.pageNumber).toBe(0);
    expect(pages[0]?.entries).toEqual([]);
  });

  it("keeps opaque local entry ids stable across captures without the locator", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/private-name.md", bytesOf("content"));

    const first = await pagesOf(harness.capture, 1);
    const second = await pagesOf(harness.capture, 2);
    const firstId = first[0]?.entries[0]?.localEntryId;
    const secondId = second[0]?.entries[0]?.localEntryId;

    expect(firstId).toBe(secondId);
    expect(firstId).toMatch(/^me1-[0-9a-f]{64}$/);
    expect(firstId).not.toContain("private-name");
  });

  it(
    "caps the run at 100,000 entries and fails closed without leaking locators",
    async () => {
      expect(MAX_MANIFEST_TOTAL_ENTRIES).toBe(100_000);
      const vault = new FakeManifestVault();
      const listing: string[] = [];
      for (let index = 0; index <= MAX_MANIFEST_TOTAL_ENTRIES; index += 1) {
        const locator = `notes/over-${index}.md`;
        listing.push(locator);
        vault.setFileBytes(locator, bytesOf("x"));
      }
      let listedPaths: readonly string[] = listing;
      vault.listRegularFilePaths = async () => listedPaths;
      const stubIdentityReader = { readLocalFileByPath: () => null };
      const cappedCapture = createManifestCapture({
        vaultReader: vault,
        identityReader: stubIdentityReader,
      });

      let thrown: unknown = null;
      await pagesOf(cappedCapture, 1).catch((error: unknown) => {
        thrown = error;
      });

      expect(thrown).toBeInstanceOf(DeviceSyncApiError);
      expect((thrown as DeviceSyncApiError).reason).toBe("device_manifest_capture_failed");
      expect((thrown as DeviceSyncApiError).retryable).toBe(false);
      expect((thrown as Error).message).not.toContain("notes/over-");

      // Exactly the cap still captures: trimming one path succeeds.
      listedPaths = listing.slice(0, MAX_MANIFEST_TOTAL_ENTRIES);
      const pages = await pagesOf(cappedCapture, 1);
      expect(pages).toHaveLength(MAX_MANIFEST_TOTAL_ENTRIES / MANIFEST_PAGE_ENTRIES);
    },
    120_000,
  );

  it("represents a file changing during enumeration by a generation greater than G", async () => {
    const harness = createHarness();
    harness.vault.setFileBytes("notes/live.md", bytesOf("before"));
    // The observation generation sits at G = 4 before the enumeration.
    for (let index = 0; index < 4; index += 1) {
      await harness.repository.deviceSync.nextObservationGeneration();
    }
    let observationSettled: Promise<void> = Promise.resolve();
    harness.vault.onBeforeRead = (normalizedPath) => {
      harness.vault.onBeforeRead = null;
      // The watcher observes the concurrent edit: the observation mints a
      // generation past the frozen barrier G = 4.
      observationSettled = harness.repository.deviceSync
        .nextObservationGeneration()
        .then(() => harness.vault.setFileBytes(normalizedPath, bytesOf("after")));
    };

    const pages = await pagesOf(harness.capture, 4);
    await observationSettled;

    // The manifest entry stays frozen at the barrier generation...
    expect(pages[0]?.entries[0]?.observationGeneration).toBe(4);
    expect(
      await sha256Hex(bytesOf("before")),
    ).toBe(pages[0]?.entries[0]?.fingerprint.sha256);
    // ...while the observation generation has moved past G.
    const state = harness.repository.deviceSync.readState();
    expect(state.observationGeneration).toBeGreaterThan(4);
    expect(state.barrierGeneration).toBeNull();
  });
});
