/**
 * Tests of the Conflict Inbox composition surface (Child 8 spec 5.2.6/6,
 * Task 9): the concrete canonical outcome applier binding over the atomic
 * Vault writer seam with echo markers, the journal-side locator resolution
 * of apply commands (including the sourceless locator_collision refusal),
 * the winner download through the EXISTING version-download surface, the
 * closed-unavailable verified-candidate uploader, the closed-token
 * diagnostics trail sink, the foreign-throw observer of the modal command
 * surface (the Task 8 M-1 carry) and the pending-apply status facts.
 * Privacy: every recorded diagnostic token is a closed vocabulary member
 * and no locator, digest or content ever reaches a status fact or a trail
 * token.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { VerifiedDownload } from "../device-sync/api";
import { DeviceSyncRepository } from "../device-sync/repository";
import type { VaultMutationSeam } from "../device-sync/atomic-vault-writer";
import { createSyncDiagnosticsTrail } from "../journal/sync-diagnostics-trail";
import type { JournalFileStore } from "../journal/persistence";
import type { JournalMeta } from "../journal/contracts";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase, journalStoreError } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import { ConflictApiError } from "./api";
import {
  CONFLICT_COMPOSITION_DIAGNOSTIC_REASONS,
  createConflictCanonicalOutcomeApplier,
  createConflictDiagnosticsTrailSink,
  createConflictVerifiedCandidateUploader,
  deriveConflictApplyStatusFacts,
  observeUnobservedConflictControllerFailures,
} from "./composition";
import { CanonicalApplyError, ConflictControllerError } from "./controller";
import type {
  CanonicalOutcomeApplyCommand,
  ConflictCompositionDiagnosticsSink,
  ConflictController,
  VerifiedCandidateUploader,
} from "./composition";
import type { PendingLocalApply } from "./contracts";

/** The real sql.js WebAssembly engine drives every composition test. */
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

const SOURCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const OTHER_SOURCE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const CONFLICT_ID = "11111111-1111-4111-8111-111111111111";
const RESOLUTION_EVENT_ID = "33333333-3333-4333-8333-333333333333";
const WINNER_VERSION_ID = "44444444-4444-4444-8444-444444444444";
const LOCATOR = "notes/a.md";
const MEDIA_TYPE = "text/markdown";

function createJournalMeta(): JournalMeta {
  return {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 0,
    lastVerifiedGeneration: 0,
    isReconcileRequired: false,
    recoveryState: "fresh_journal_created",
  };
}

/** Seed one tracked local file row that maps the source to its locator. */
function seedLocalFileRow(
  database: SqliteDatabase,
  sourceId: string,
  locator: string,
): Promise<void> {
  return database.runSerializedMutation((session) => {
    session.exec(
      [
        "insert into local_files (local_file_id, normalized_path, source_id,",
        "observed_sha256, observed_size_bytes, observed_media_type,",
        "base_version_id, policy_revision) values (",
        `'${sourceId}', '${locator}', '${sourceId}',`,
        `'${"a".repeat(64)}', 3, '${MEDIA_TYPE}', null, 0);`,
      ].join(" "),
    );
  });
}

/** The in-memory atomic Vault seam: staging, narrow replace, trash. */
function createInMemoryVaultSeam(
  initialFiles: ReadonlyMap<string, Uint8Array> = new Map(),
): VaultMutationSeam & {
  readonly files: Map<string, Uint8Array>;
  isFinalRenameCorrupt: boolean;
} {
  const files = new Map<string, Uint8Array>(initialFiles);
  const seam: VaultMutationSeam & { files: Map<string, Uint8Array>; isFinalRenameCorrupt: boolean } = {
    isFinalRenameCorrupt: false,
    files,
    async locatorExists(locator) {
      return files.has(locator);
    },
    async createFile(locator, bytes) {
      files.set(locator, new Uint8Array(bytes));
    },
    async readBytes(locator) {
      const bytes = files.get(locator);
      return bytes === undefined ? null : new Uint8Array(bytes);
    },
    async renameLocator(fromLocator, toLocator) {
      const bytes = files.get(fromLocator);
      if (bytes === undefined) {
        throw new Error("rename source absent");
      }
      files.delete(fromLocator);
      // Corrupt only the final rename-in of the staged sibling (the
      // temp-suffix edge), never the rollback retention edge.
      if (seam.isFinalRenameCorrupt && fromLocator.includes(".device-sync-tmp-")) {
        files.set(toLocator, new Uint8Array([99, 98, 97]));
        return;
      }
      files.set(toLocator, bytes);
    },
    async trashLocator(locator) {
      if (!files.has(locator)) {
        throw new Error("trash target absent");
      }
      files.delete(locator);
    },
  };
  return seam;
}

/** The recording verified-download seam (the EXISTING version surface). */
function createRecordingDownloader(
  winnerBytes: Uint8Array,
): {
  readonly download: (input: { sourceId: string; sourceVersionId: string }) => Promise<VerifiedDownload>;
  readonly inputLog: { readonly sourceId: string; readonly sourceVersionId: string }[];
} {
  const inputLog: { sourceId: string; sourceVersionId: string }[] = [];
  return {
    inputLog,
    download: async (input) => {
      inputLog.push(input);
      return {
        bytes: new Uint8Array(winnerBytes),
        declaredSha256: await sha256Hex(winnerBytes),
        sizeBytes: winnerBytes.byteLength,
        mediaType: MEDIA_TYPE,
      };
    },
  };
}

/** The recording diagnostics sink (the trail sink's test double). */
function createRecordingConflictDiagnosticsSink(): ConflictCompositionDiagnosticsSink & {
  readonly observedFailures: readonly { reason: string; contextReason: string | null }[];
} {
  const observedFailures: { reason: string; contextReason: string | null }[] = [];
  return {
    observedFailures,
    observeConflictFailure(reason) {
      observedFailures.push({ reason, contextReason: null });
    },
    observeConflictCompositionFailure(reason, contextReason) {
      observedFailures.push({ reason, contextReason: contextReason ?? null });
    },
  };
}

/** The minimal in-memory journal file store the trail sink needs. */
class FakeTrailFileStore implements JournalFileStore {
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

  async list(): Promise<readonly string[]> {
    return [...this.files.keys()];
  }
}

interface ApplierHarness {
  readonly database: SqliteDatabase;
  readonly deviceSyncRepository: DeviceSyncRepository;
  readonly seam: ReturnType<typeof createInMemoryVaultSeam>;
  readonly downloader: ReturnType<typeof createRecordingDownloader>;
  readonly applier: ReturnType<typeof createConflictCanonicalOutcomeApplier>;
  readonly diagnostics: ReturnType<typeof createRecordingConflictDiagnosticsSink>;
}

async function createApplierHarness(
  options: { readonly seedSourceRow?: boolean } = {},
): Promise<ApplierHarness> {
  const database = SqliteDatabase.createEmpty(engineModule, createJournalMeta());
  if (options.seedSourceRow !== false) {
    await seedLocalFileRow(database, SOURCE_ID, LOCATOR);
  }
  const deviceSyncRepository = new DeviceSyncRepository({ database });
  const seam = createInMemoryVaultSeam(
    new Map([[LOCATOR, new TextEncoder().encode("local candidate bytes")]]),
  );
  const winnerBytes = new TextEncoder().encode("# remote winner bytes\n");
  const downloader = createRecordingDownloader(winnerBytes);
  const diagnostics = createRecordingConflictDiagnosticsSink();
  const applier = createConflictCanonicalOutcomeApplier({
    database,
    repository: deviceSyncRepository,
    seam,
    downloadSourceVersion: downloader.download,
    diagnostics,
  });
  return { database, deviceSyncRepository, seam, downloader, applier, diagnostics };
}

function remoteVersionCommand(
  overrides: Partial<CanonicalOutcomeApplyCommand> = {},
): CanonicalOutcomeApplyCommand {
  return {
    conflictId: CONFLICT_ID,
    resolutionEventId: RESOLUTION_EVENT_ID,
    sourceId: SOURCE_ID,
    targetAction: "apply_remote_version",
    winnerVersionId: WINNER_VERSION_ID,
    winnerBytes: null,
    winnerMediaType: null,
    ...overrides,
  };
}

/** Read every durable echo marker row (the watcher capture's lookup view). */
function readEchoMarkerRows(
  database: SqliteDatabase,
): { eventSequence: number; sourceId: string; operation: string; priorLocator: string | null }[] {
  const result = database.readAll(
    "select event_sequence, source_id, operation, prior_locator from echo_markers " +
      "order by event_sequence asc;",
  );;
  return (result[0]?.values ?? []).map((row) => {
    const [eventSequence, sourceId, operation, priorLocator] = row as [
      number,
      string,
      string,
      string | null,
    ];
    return { eventSequence, sourceId, operation, priorLocator };
  });
}

// --- the applier binding -----------------------------------------------------------------------------

describe("Conflict canonical outcome applier (Task 9 composition)", () => {
  it("applies a downloaded remote winner at the journal-resolved locator", async () => {
    const harness = await createApplierHarness();
    await harness.applier.applyCanonicalOutcome(remoteVersionCommand());

    const applied = harness.seam.files.get(LOCATOR);
    expect(new TextDecoder().decode(applied ?? new Uint8Array())).toBe("# remote winner bytes\n");
    // The winner download rode the EXISTING version-based surface.
    expect(harness.downloader.inputLog).toEqual([
      { sourceId: SOURCE_ID, sourceVersionId: WINNER_VERSION_ID },
    ]);
    // No staging or rollback sibling survives the apply.
    expect([...harness.seam.files.keys()]).toEqual([LOCATOR]);
  });

  it("records the exact content echo marker the watcher capture consumes", async () => {
    const harness = await createApplierHarness();
    await harness.applier.applyCanonicalOutcome(remoteVersionCommand());

    const markers = readEchoMarkerRows(harness.database);
    expect(markers).toHaveLength(1);
    const marker = markers[0] as NonNullable<(typeof markers)[number]>;
    expect(marker.sourceId).toBe(SOURCE_ID);
    expect(marker.operation).toBe("updated");
    expect(marker.priorLocator).toBe(LOCATOR);
    // The exact-match consume the JournalCapture performs succeeds once.
    const winnerBytes = new TextEncoder().encode("# remote winner bytes\n");
    const fingerprint = {
      sha256: await sha256Hex(winnerBytes),
      sizeBytes: winnerBytes.byteLength,
      mediaType: MEDIA_TYPE,
    };
    await expect(
      harness.deviceSyncRepository.matchAndConsumeEcho({
        eventSequence: marker.eventSequence,
        sourceId: SOURCE_ID,
        operation: "updated",
        priorLocator: LOCATOR,
        targetLocator: null,
        fingerprint,
      }),
    ).resolves.toBe(true);
    expect(readEchoMarkerRows(harness.database)).toHaveLength(0);
  });

  it("mints the marker sequence from the disjoint high namespace on a collision", async () => {
    const harness = await createApplierHarness();
    // A foreign marker already occupies the ceiling of the disjoint
    // conflict namespace (a prior session's leftover): the retry mints the
    // next-lower sequence instead of failing the apply.
    await harness.deviceSyncRepository.recordEchoMarker({
      eventSequence: Number.MAX_SAFE_INTEGER,
      sourceId: OTHER_SOURCE_ID,
      operation: "updated",
      priorLocator: "other/note.md",
      targetLocator: null,
      finalFingerprint: null,
    });

    await harness.applier.applyCanonicalOutcome(remoteVersionCommand());

    const markers = readEchoMarkerRows(harness.database);
    expect(markers.map((entry) => entry.eventSequence)).toEqual([
      Number.MAX_SAFE_INTEGER - 1,
      Number.MAX_SAFE_INTEGER,
    ]);
  });

  it("applies the in-memory merged draft without any winner download (save_merged)", async () => {
    const harness = await createApplierHarness();
    const draft = new TextEncoder().encode("# edited merged draft\n");
    await harness.applier.applyCanonicalOutcome({
      conflictId: CONFLICT_ID,
      resolutionEventId: RESOLUTION_EVENT_ID,
      sourceId: SOURCE_ID,
      targetAction: "apply_resulting_version",
      winnerVersionId: WINNER_VERSION_ID,
      winnerBytes: draft,
      winnerMediaType: "text/markdown",
    });

    expect(new TextDecoder().decode(harness.seam.files.get(LOCATOR) ?? new Uint8Array())).toBe(
      "# edited merged draft\n",
    );
    expect(harness.downloader.inputLog).toEqual([]);
  });

  it("fails closed at winner_download for the sourceless locator_collision command", async () => {
    const harness = await createApplierHarness();
    const thrown = await harness.applier
      .applyCanonicalOutcome(
        remoteVersionCommand({ sourceId: null, targetAction: "apply_remote_tombstone" }),
      )
      .then(
        () => null,
        (error: unknown) => error,
      );
    expect(thrown).toBeInstanceOf(CanonicalApplyError);
    expect((thrown as CanonicalApplyError).stage).toBe("winner_download");
    // The Vault and the echo markers stay untouched.
    expect(new TextDecoder().decode(harness.seam.files.get(LOCATOR) ?? new Uint8Array())).toBe(
      "local candidate bytes",
    );
    expect(readEchoMarkerRows(harness.database)).toHaveLength(0);
  });

  it("fails closed at winner_download when no local file row maps the source", async () => {
    const harness = await createApplierHarness({ seedSourceRow: false });
    await expect(harness.applier.applyCanonicalOutcome(remoteVersionCommand())).rejects.toMatchObject(
      { stage: "winner_download" },
    );
    expect(harness.downloader.inputLog).toEqual([]);
  });

  it("fails closed at winner_download when the version download rejects", async () => {
    const harness = await createApplierHarness();
    const applier = createConflictCanonicalOutcomeApplier({
      database: harness.database,
      repository: harness.deviceSyncRepository,
      seam: harness.seam,
      downloadSourceVersion: async () => {
        throw new ConflictApiError("evidence_unavailable", false);
      },
      diagnostics: harness.diagnostics,
    });
    await expect(applier.applyCanonicalOutcome(remoteVersionCommand())).rejects.toMatchObject({
      stage: "winner_download",
    });
  });

  it("applies a remote tombstone through the trash path and marks the delete echo", async () => {
    const harness = await createApplierHarness();
    await harness.applier.applyCanonicalOutcome(
      remoteVersionCommand({ targetAction: "apply_remote_tombstone", winnerVersionId: null }),
    );

    expect(harness.seam.files.has(LOCATOR)).toBe(false);
    const markers = readEchoMarkerRows(harness.database);
    expect(markers).toHaveLength(1);
    expect(markers[0]?.operation).toBe("deleted");
    expect(markers[0]?.priorLocator).toBe(LOCATOR);
    // An exact re-apply after the trash completed is idempotent.
    await expect(
      harness.applier.applyCanonicalOutcome(
        remoteVersionCommand({ targetAction: "apply_remote_tombstone", winnerVersionId: null }),
      ),
    ).resolves.toBeUndefined();
  });

  it("parks vault_apply and restores the verified old bytes when the final verify fails", async () => {
    const harness = await createApplierHarness();
    harness.seam.isFinalRenameCorrupt = true;
    await expect(harness.applier.applyCanonicalOutcome(remoteVersionCommand())).rejects.toMatchObject(
      { stage: "vault_apply" },
    );

    // The rollback sibling restored the verified old bytes.
    expect(new TextDecoder().decode(harness.seam.files.get(LOCATOR) ?? new Uint8Array())).toBe(
      "local candidate bytes",
    );
  });

  it("consumes the echo marker best-effort when the vault apply fails", async () => {
    const harness = await createApplierHarness();
    harness.seam.isFinalRenameCorrupt = true;
    await expect(harness.applier.applyCanonicalOutcome(remoteVersionCommand())).rejects.toMatchObject(
      { stage: "vault_apply" },
    );
    expect(readEchoMarkerRows(harness.database)).toHaveLength(0);
  });

  it("observes the closed marker-consume failure token when the cleanup consume throws", async () => {
    const harness = await createApplierHarness();
    harness.seam.isFinalRenameCorrupt = true;
    const failingRepository = {
      recordEchoMarker: harness.deviceSyncRepository.recordEchoMarker.bind(
        harness.deviceSyncRepository,
      ),
      matchAndConsumeEcho: async () => {
        throw journalStoreError("journal_mutation_failed");
      },
    } as unknown as DeviceSyncRepository;
    const applier = createConflictCanonicalOutcomeApplier({
      database: harness.database,
      repository: failingRepository,
      seam: harness.seam,
      downloadSourceVersion: harness.downloader.download,
      diagnostics: harness.diagnostics,
    });

    await expect(applier.applyCanonicalOutcome(remoteVersionCommand())).rejects.toMatchObject({
      stage: "vault_apply",
    });
    expect(harness.diagnostics.observedFailures).toContainEqual({
      reason: "conflict_echo_marker_failed",
      contextReason: "journal_mutation_failed",
    });
  });
});

// --- the pending-apply status facts ------------------------------------------------------------------

function pendingRow(overrides: Partial<PendingLocalApply> = {}): PendingLocalApply {
  return {
    conflictId: CONFLICT_ID,
    resolutionEventId: RESOLUTION_EVENT_ID,
    targetAction: "apply_remote_version",
    safeReason: "vault_apply_failed",
    attemptCount: 1,
    nextEligibleRetryEpochMs: 1_784_000_100_000,
    createdAtEpochMs: 1_784_000_000_000,
    updatedAtEpochMs: 1_784_000_000_000,
    ...overrides,
  };
}

describe("Conflict apply status facts (Task 9 composition)", () => {
  it("counts every parked row and keeps only the closed safe-reason tokens", () => {
    const facts = deriveConflictApplyStatusFacts([
      pendingRow({ safeReason: "winner_download_failed" }),
      pendingRow({
        conflictId: "22222222-2222-4222-8222-222222222222",
        safeReason: "vault_apply_failed",
      }),
      pendingRow({ safeReason: "resolution_committed" }),
    ]);
    expect(facts.pendingLocalApplyCount).toBe(3);
    expect(facts.localApplySafeReasonTokens).toEqual([
      "winner_download_failed",
      "vault_apply_failed",
      "resolution_committed",
    ]);
  });

  it("keeps an attempt-capped row visible regardless of its retry timestamp", () => {
    // The Task 8 ruling: eligibility for RETRY gates on attemptCount >=
    // the cap, never on the timestamp alone — and the STATUS surface
    // counts the capped row too: the owed apply stays visible until a
    // human resolves it, whenever its next retry moment sits.
    const facts = deriveConflictApplyStatusFacts([
      pendingRow({ attemptCount: 5, nextEligibleRetryEpochMs: 9_000_000_000_000 }),
    ]);
    expect(facts.pendingLocalApplyCount).toBe(1);
    expect(facts.localApplySafeReasonTokens).toEqual(["vault_apply_failed"]);
  });
});

// --- the real verified-candidate uploader -----------------------------------------------------------

describe("Conflict verified candidate uploader (Task 10 binding)", () => {
  it("derives the digest locally and carries only the opaque reference back", async () => {
    const bytes = new TextEncoder().encode("# merged draft\n");
    const uploads: {
      conflictId: string;
      mediaType: string;
      sha256: string;
      byteLength: number;
    }[] = [];
    const api = {
      async uploadResolutionCandidate(input: {
        conflictId: string;
        bytes: Uint8Array;
        mediaType: string;
        sha256: string;
      }): Promise<string> {
        uploads.push({
          conflictId: input.conflictId,
          mediaType: input.mediaType,
          sha256: input.sha256,
          byteLength: input.bytes.byteLength,
        });
        return "33333333-3333-4333-8333-333333333334";
      },
    } as unknown as import("./api").ConflictApi;
    const uploader: VerifiedCandidateUploader = createConflictVerifiedCandidateUploader(api);

    const receipt = await uploader.uploadVerifiedCandidate({
      conflictId: "11111111-1111-4111-8111-111111111111",
      bytes,
      mediaType: "text/markdown",
    });

    expect(receipt.verifiedCandidateObjectId).toBe("33333333-3333-4333-8333-333333333334");
    expect(uploads).toHaveLength(1);
    expect(uploads[0]?.conflictId).toBe("11111111-1111-4111-8111-111111111111");
    expect(uploads[0]?.mediaType).toBe("text/markdown");
    expect(uploads[0]?.byteLength).toBe(bytes.byteLength);
    expect(uploads[0]?.sha256).toMatch(/^[0-9a-f]{64}$/);
  });

  it("maps a wire failure onto the controller's closed candidate-upload reason", async () => {
    const api = {
      async uploadResolutionCandidate(): Promise<string> {
        throw new ConflictApiError("dependency_unavailable", true);
      },
    } as unknown as import("./api").ConflictApi;
    const uploader: VerifiedCandidateUploader = createConflictVerifiedCandidateUploader(api);

    const thrown = await uploader
      .uploadVerifiedCandidate({
        conflictId: "11111111-1111-4111-8111-111111111111",
        bytes: new TextEncoder().encode("draft"),
        mediaType: "text/markdown",
      })
      .then(
        () => null,
        (error: unknown) => error,
      );
    expect(thrown).toBeInstanceOf(ConflictControllerError);
    expect((thrown as ConflictControllerError).reason).toBe("conflict_candidate_upload_failed");
  });
});

// --- the foreign-throw observer of the modal command surface (M-1) -----------------------------------

function createThrowingControllerFake(
  rejectionOf: () => Promise<never>,
): { controller: ConflictController; state: { resolveKeepRemoteCalls: number } } {
  const state = { resolveKeepRemoteCalls: 0 };
  const controller: ConflictController = {
    listOpenConflicts: () =>
      Promise.resolve({ conflicts: [], hasMore: false, nextExclusiveStartConflictId: null }),
    getConflictDetail: () => {
      throw new Error("unused");
    },
    buildMergeProposal: () => {
      throw new Error("unused");
    },
    resolveKeepRemote: () => {
      state.resolveKeepRemoteCalls += 1;
      return rejectionOf();
    },
    resolveKeepLocal: () => {
      throw new Error("unused");
    },
    resolveSaveMerged: () => {
      throw new Error("unused");
    },
    retryPendingLocalApplies: () => Promise.resolve(),
  };
  return { controller, state };
}

describe("Unobserved conflict controller failure observer (M-1)", () => {
  it("observes a repair-store throw with its closed store reason", async () => {
    const sink = createRecordingConflictDiagnosticsSink();
    const { controller, state } = createThrowingControllerFake(async () => {
      throw journalStoreError("journal_mutation_failed");
    });
    const observed = observeUnobservedConflictControllerFailures(controller, sink);

    await expect(observed.resolveKeepRemote(CONFLICT_ID)).rejects.toThrow();
    expect(state.resolveKeepRemoteCalls).toBe(1);
    expect(sink.observedFailures).toEqual([
      { reason: "conflict_repair_store_failed", contextReason: "journal_mutation_failed" },
    ]);
  });

  it("never double-observes the wire client's own closed failures", async () => {
    const sink = createRecordingConflictDiagnosticsSink();
    const { controller } = createThrowingControllerFake(async () => {
      throw new ConflictApiError("network_offline", true);
    });
    const observed = observeUnobservedConflictControllerFailures(controller, sink);

    await expect(observed.resolveKeepRemote(CONFLICT_ID)).rejects.toBeInstanceOf(ConflictApiError);
    expect(sink.observedFailures).toEqual([]);
  });
});

// --- the trail sink ----------------------------------------------------------------------------------

describe("Conflict diagnostics trail sink (Task 9 composition)", () => {
  it("appends one conflict_failure trail entry carrying the closed tokens", async () => {
    const trail = createSyncDiagnosticsTrail({ fileStore: new FakeTrailFileStore() });
    await trail.load();
    const sink = createConflictDiagnosticsTrailSink(trail);

    sink.observeConflictFailure("conflict_vault_apply_failed");
    sink.observeConflictCompositionFailure(
      "conflict_repair_store_failed",
      "journal_mutation_failed",
    );

    const entries = trail.readEntries();
    expect(entries.map((entry) => entry.kind)).toEqual(["conflict_failure", "conflict_failure"]);
    expect(entries[0]?.tokens).toEqual(["conflict_vault_apply_failed"]);
    expect(entries[1]?.tokens).toEqual(["conflict_repair_store_failed", "journal_mutation_failed"]);
    // Every token is a closed vocabulary member.
    const closedSet = new Set<string>(CONFLICT_COMPOSITION_DIAGNOSTIC_REASONS);
    closedSet.add("journal_mutation_failed");
    for (const entry of entries) {
      for (const token of entry.tokens) {
        expect(closedSet.has(String(token))).toBe(true);
      }
    }
  });
});
