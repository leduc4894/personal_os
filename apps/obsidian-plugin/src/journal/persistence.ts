/**
 * Verified SQLite generations and recovery of the portable journal (spec 6.1, 6.2).
 *
 * Every committed transaction becomes the next immutable generation: the
 * database image is exported, digested, written as `journal.sqlite.g<n>`,
 * read back and verified byte-exactly, and only then published through a small
 * manifest naming the generation, its digest, size and schema version.
 * Startup accepts only a manifest whose named generation verifies; a torn,
 * missing or invalid newest write falls back to the newest prior verified
 * generation, and when nothing verifies the journal rebuilds empty with
 * `reconcile_required` while every Vault file stays untouched (spec 6.2).
 * Retention keeps the current and one prior verified generation and only ever
 * removes an older generation after the new manifest verified.
 *
 * All journal bytes flow through the narrow {@link JournalFileStore} port
 * bound to the Vault's configured plugin directory (spec 6.1) — never a
 * hard-coded config-directory name. Watcher path notifications arriving while
 * recovery is running are buffered in a bounded in-memory set; an overflow
 * durably sets `reconcile_required` instead of silently losing a mutation.
 *
 * Privacy (spec 9): thrown errors carry closed reason tokens only — no path,
 * digest, library exception or provider detail ever reaches diagnostics.
 */

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { JournalMeta, JournalRecoveryState } from "./contracts";
import {
  JOURNAL_SCHEMA_VERSION,
  JournalStoreError,
  type JournalStoreErrorReason,
  journalStoreError,
  SqliteDatabase,
} from "./sqlite-database";
import type {
  SqliteEngineModule,
  SqliteMutationSession,
  SqliteQueryResult,
} from "./sqlite-database";

// --- frozen file vocabulary and buffer bound (spec 6.1, 6.2) --------------------------

/** The immutable generation image file prefix: `journal.sqlite.g<n>`. */
export const JOURNAL_GENERATION_FILE_PREFIX = "journal.sqlite.g";

/** The manifest naming the current and one prior verified generation. */
export const JOURNAL_MANIFEST_FILE_NAME = "journal.manifest.json";

/** The manifest record contract identifier. */
export const JOURNAL_MANIFEST_CONTRACT = "obsidian_journal_manifest/v1";

/**
 * The bound of the in-memory path buffer held while recovery runs (spec 6.1).
 * Distinct paths coalesce; one path beyond the bound flips the journal to
 * `reconcile_required` instead of silently losing the notification.
 */
export const MAX_BUFFERED_RECOVERY_PATHS = 1_000;

const SHA256_HEX_PATTERN = /^[0-9a-f]{64}$/;

// --- verified generation records (spec 6.2) ------------------------------------------------

/** One verified persistence generation: number, exact size, digest and schema version. */
export interface VerifiedJournalGeneration {
  readonly generationNumber: number;
  readonly sizeBytes: number;
  readonly sha256: string;
  readonly schemaVersion: number;
}

/**
 * The published journal manifest: the current verified generation plus at
 * most one prior verified generation kept for fallback.
 */
export interface JournalGenerationManifest {
  readonly contract: typeof JOURNAL_MANIFEST_CONTRACT;
  readonly current: VerifiedJournalGeneration;
  readonly prior: VerifiedJournalGeneration | null;
}

// --- the narrow journal file port (spec 6.1) --------------------------------------------------

/**
 * The binary journal directory: file names are journal-local (generation
 * images and the manifest); the plugin binds this to its Vault plugin
 * directory through the Obsidian DataAdapter binary methods.
 */
export interface JournalFileStore {
  exists(fileName: string): Promise<boolean>;
  readBinary(fileName: string): Promise<ArrayBuffer>;
  writeBinary(fileName: string, data: ArrayBuffer): Promise<void>;
  remove(fileName: string): Promise<void>;
}

/**
 * The structural slice of the Obsidian App the journal store needs: the
 * Vault's configured config directory and the binary DataAdapter. The
 * composition root hands the App to {@link createVaultPluginJournalStore},
 * which narrows it immediately; nothing else ever sees this surface.
 */
export interface VaultAdapterSurface {
  readonly vault: {
    readonly configDir: string;
    readonly adapter: {
      exists(path: string): Promise<boolean>;
      readBinary(path: string): Promise<ArrayBuffer>;
      writeBinary(path: string, data: ArrayBuffer): Promise<void>;
      remove(path: string): Promise<void>;
    };
  };
}

/** Join Vault path segments with `/`, ignoring empty segments. */
function joinVaultPath(...segments: readonly string[]): string {
  return segments
    .flatMap((segment) => segment.split("/"))
    .filter((segment) => segment.length > 0)
    .join("/");
}

/**
 * Bind the journal file store to the Vault's configured plugin directory
 * (spec 6.1): the directory resolves from `Vault.configDir` and the plugin
 * manifest id, never a hard-coded config-directory name.
 */
export function createVaultPluginJournalStore(
  app: VaultAdapterSurface,
  pluginId: string,
): JournalFileStore {
  if (pluginId.trim().length === 0) {
    throw journalStoreError("journal_store_unavailable");
  }
  const { configDir, adapter } = app.vault;
  const pluginDirectory = joinVaultPath(configDir, "plugins", pluginId);
  return {
    exists: (fileName: string) => adapter.exists(joinVaultPath(pluginDirectory, fileName)),
    readBinary: (fileName: string) => adapter.readBinary(joinVaultPath(pluginDirectory, fileName)),
    writeBinary: (fileName: string, data: ArrayBuffer) =>
      adapter.writeBinary(joinVaultPath(pluginDirectory, fileName), data),
    remove: (fileName: string) => adapter.remove(joinVaultPath(pluginDirectory, fileName)),
  };
}

// --- manifest parsing and comparison ------------------------------------------------------------

function parseVerifiedGeneration(value: unknown): VerifiedJournalGeneration | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const candidate = value as Record<string, unknown>;
  const { generationNumber, sizeBytes, sha256, schemaVersion } = candidate;
  if (
    typeof generationNumber !== "number" ||
    !Number.isInteger(generationNumber) ||
    generationNumber < 1
  ) {
    return null;
  }
  if (typeof sizeBytes !== "number" || !Number.isInteger(sizeBytes) || sizeBytes < 1) {
    return null;
  }
  if (typeof sha256 !== "string" || !SHA256_HEX_PATTERN.test(sha256)) {
    return null;
  }
  if (schemaVersion !== JOURNAL_SCHEMA_VERSION) {
    return null;
  }
  return { generationNumber, sizeBytes, sha256, schemaVersion };
}

/** Parse manifest bytes; any malformed or foreign record answers null. */
function parseJournalManifest(bytes: Uint8Array): JournalGenerationManifest | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }
  const candidate = parsed as Record<string, unknown>;
  if (candidate["contract"] !== JOURNAL_MANIFEST_CONTRACT) {
    return null;
  }
  const current = parseVerifiedGeneration(candidate["current"]);
  if (current === null) {
    return null;
  }
  let prior: VerifiedJournalGeneration | null = null;
  if (candidate["prior"] !== null && candidate["prior"] !== undefined) {
    prior = parseVerifiedGeneration(candidate["prior"]);
    if (prior === null) {
      return null;
    }
  }
  return { contract: JOURNAL_MANIFEST_CONTRACT, current, prior };
}

function isSameVerifiedGeneration(
  left: VerifiedJournalGeneration,
  right: VerifiedJournalGeneration,
): boolean {
  return (
    left.generationNumber === right.generationNumber &&
    left.sizeBytes === right.sizeBytes &&
    left.sha256 === right.sha256 &&
    left.schemaVersion === right.schemaVersion
  );
}

function isSameJournalManifest(
  left: JournalGenerationManifest,
  right: JournalGenerationManifest,
): boolean {
  if (!isSameVerifiedGeneration(left.current, right.current)) {
    return false;
  }
  if (left.prior === null || right.prior === null) {
    return left.prior === null && right.prior === null;
  }
  return isSameVerifiedGeneration(left.prior, right.prior);
}

function toArrayBuffer(image: Uint8Array): ArrayBuffer {
  return image.buffer.slice(
    image.byteOffset,
    image.byteOffset + image.byteLength,
  ) as ArrayBuffer;
}

function generationFileName(generationNumber: number): string {
  return `${JOURNAL_GENERATION_FILE_PREFIX}${generationNumber}`;
}

// --- the journal persistence layer ---------------------------------------------------------------

/** The closed outcome of the unload-time final flush attempt (spec 11). */
export type JournalFinalFlushOutcome = "final_generation_current" | "commit_in_flight";

export interface JournalPersistenceOptions {
  readonly fileStore: JournalFileStore;
  readonly engineModule: SqliteEngineModule;
}

/**
 * The persistence lifecycle of the portable journal. One instance owns the
 * recovered working database, the verified generation chain and the single
 * serialized commit queue through which every durable mutation flows.
 */
/**
 * The bounded number of closed publish-failure reason tokens kept in
 * memory (fix round 5). The count itself is unbounded; only the token
 * ring is bounded.
 */
const MAX_GENERATION_PUBLISH_FAILURE_REASONS = 5;

export class JournalPersistence {
  readonly #fileStore: JournalFileStore;
  readonly #engineModule: SqliteEngineModule;
  #database: SqliteDatabase | null = null;
  #recoveryState: JournalRecoveryState | null = null;
  #verifiedGeneration: VerifiedJournalGeneration | null = null;
  #priorVerifiedGeneration: VerifiedJournalGeneration | null = null;
  #isReconcileRequired = false;
  /**
   * Fix round 5 diagnostics: the bounded in-memory record of generation
   * PUBLISH failures (the file-store/publish path after a committed
   * transaction). Closed reason tokens only — the live torn-publish
   * investigation needed exactly this surface to discriminate
   * environmental write failures from code defects.
   */
  #generationPublishFailureCount = 0;
  readonly #generationPublishFailureReasons: JournalStoreErrorReason[] = [];
  #hasRecoveryBufferOverflowed = false;
  #inFlightCommitCount = 0;
  readonly #bufferedVaultPaths = new Set<string>();
  #commitTail: Promise<unknown> = Promise.resolve();

  constructor(options: JournalPersistenceOptions) {
    this.#fileStore = options.fileStore;
    this.#engineModule = options.engineModule;
  }

  /**
   * Buffer one watcher path notification while recovery runs (spec 6.1).
   * Distinct paths coalesce; the buffer is bounded and one distinct path
   * beyond the bound flips the journal to `reconcile_required` durably
   * instead of silently losing the notification.
   */
  bufferVaultPathDuringRecovery(path: string): void {
    if (this.#database !== null || this.#bufferedVaultPaths.has(path)) {
      return;
    }
    if (this.#bufferedVaultPaths.size >= MAX_BUFFERED_RECOVERY_PATHS) {
      this.#hasRecoveryBufferOverflowed = true;
      this.#isReconcileRequired = true;
      return;
    }
    this.#bufferedVaultPaths.add(path);
  }

  /** Take every buffered path once; the buffer stays empty afterwards. */
  drainBufferedVaultPaths(): string[] {
    const paths = [...this.#bufferedVaultPaths];
    this.#bufferedVaultPaths.clear();
    return paths;
  }

  get hasRecoveryBufferOverflowed(): boolean {
    return this.#hasRecoveryBufferOverflowed;
  }

  get isReconcileRequired(): boolean {
    return this.#isReconcileRequired;
  }

  get recoveryState(): JournalRecoveryState {
    if (this.#recoveryState === null) {
      throw journalStoreError("journal_not_open");
    }
    return this.#recoveryState;
  }

  get verifiedGenerationNumber(): number {
    this.#requireOpenedDatabase();
    const verified = this.#verifiedGeneration;
    if (verified === null) {
      throw journalStoreError("journal_not_open");
    }
    return verified.generationNumber;
  }

  /** The current in-memory journal meta of the opened working database. */
  readJournalMeta(): JournalMeta {
    return this.#requireOpenedDatabase().readJournalMeta();
  }

  /**
   * One read-only query on the opened working database (journal-scoped SQL
   * only). This is the narrow read seam a repository composition uses for
   * its queries: mutations still flow exclusively through
   * {@link commitGeneration}, so the single-writer invariant is untouched.
   */
  readAll(sql: string): SqliteQueryResult[] {
    return this.#requireOpenedDatabase().readAll(sql);
  }

  /**
   * Run recovery (spec 6.2): accept only a manifest whose named generation
   * verifies, fall back to the newest prior verified generation, or rebuild
   * an empty `reconcile_required` journal when nothing verifies. Recovery
   * reads only journal-scoped files and never touches Vault content.
   */
  async open(): Promise<void> {
    if (this.#database !== null) {
      return;
    }
    const { manifest, isManifestPresent } = await this.#readManifestState();
    const recovered = await this.#recoverVerifiedDatabase(manifest);
    if (recovered !== null) {
      const { database, verifiedGeneration, recoveryState } = recovered;
      this.#isReconcileRequired ||= database.readJournalMeta().isReconcileRequired;
      await this.#refreshRecoveredMeta(database, verifiedGeneration, recoveryState);
      this.#database = database;
      this.#recoveryState = recoveryState;
      this.#verifiedGeneration = verifiedGeneration;
      // Only a loaded manifest.current keeps a trusted prior entry in the
      // retained window; after a fallback the verified generation IS the
      // oldest retained one until the next commit republishes.
      this.#priorVerifiedGeneration =
        manifest !== null && isSameVerifiedGeneration(manifest.current, verifiedGeneration)
          ? manifest.prior
          : null;
      return;
    }
    await this.#rebuildEmptyJournal(isManifestPresent);
  }

  /**
   * The single durable commit path: the operation's SQL transaction, the
   * generation export/verify/publish cycle and retention all run inside one
   * serialized queue, so concurrent commits produce strictly sequential
   * generations and a failed publish leaves the prior verified generation
   * intact.
   */
  async commitGeneration<T>(
    operation: (session: SqliteMutationSession) => T | Promise<T>,
  ): Promise<T> {
    this.#inFlightCommitCount += 1;
    const execution = this.#commitTail.then(() => this.#executeGenerationCommit(operation));
    this.#commitTail = execution.then(
      () => this.#finishTrackedCommit(),
      () => this.#finishTrackedCommit(),
    );
    return execution;
  }

  #finishTrackedCommit(): void {
    this.#inFlightCommitCount = Math.max(0, this.#inFlightCommitCount - 1);
  }

  /**
   * The synchronous, bounded final-flush attempt of safe unload (spec 11):
   * every journal mutation already persisted its own verified generation,
   * so the attempt reports whether the journal sits at its final generation
   * or a commit is still in flight. It starts no work, awaits nothing, and
   * never blocks unload on async generation publishing — an interrupted
   * commit simply recovers from the newest verified generation on reopen.
   */
  attemptFinalFlush(): JournalFinalFlushOutcome {
    return this.#inFlightCommitCount > 0 ? "commit_in_flight" : "final_generation_current";
  }

  close(): void {
    this.#database?.close();
    this.#database = null;
    this.#recoveryState = null;
    this.#verifiedGeneration = null;
    this.#priorVerifiedGeneration = null;
  }

  #requireOpenedDatabase(): SqliteDatabase {
    if (this.#database === null) {
      throw journalStoreError("journal_not_open");
    }
    return this.#database;
  }

  /**
   * The required lifecycle table names that must be present on every
   * verified generation (spec 6.3, child 5): the keyed operands extension
   * is part of the durable schema, so a torn / missing table is image
   * corruption that recovery must surface as `journal_image_invalid`
   * instead of silently passing verification.
   */
  static readonly #LIFECYCLE_REQUIRED_TABLES = [
    "journal_meta",
    "local_files",
    "journal_events",
    "journal_attempts",
    "lifecycle_event_operands",
  ] as const;

  /**
   * Read the manifest, distinguishing ABSENT from PRESENT-but-unverifiable:
   * a present manifest that fails to parse still proves journal artifacts
   * exist, which forces the rebuild path with `reconcile_required`. An
   * errored existence probe is neither: recovery fails closed instead of
   * reporting an absent store it could not actually observe.
   */
  async #readManifestState(): Promise<{
    isManifestPresent: boolean;
    manifest: JournalGenerationManifest | null;
  }> {
    let isManifestPresent: boolean;
    try {
      isManifestPresent = await this.#fileStore.exists(JOURNAL_MANIFEST_FILE_NAME);
    } catch {
      throw journalStoreError("journal_store_unavailable");
    }
    if (!isManifestPresent) {
      return { isManifestPresent: false, manifest: null };
    }
    try {
      const bytes = new Uint8Array(await this.#fileStore.readBinary(JOURNAL_MANIFEST_FILE_NAME));
      return { isManifestPresent: true, manifest: parseJournalManifest(bytes) };
    } catch {
      return { isManifestPresent: true, manifest: null };
    }
  }

  async #recoverVerifiedDatabase(
    manifest: JournalGenerationManifest | null,
  ): Promise<{
    database: SqliteDatabase;
    verifiedGeneration: VerifiedJournalGeneration;
    recoveryState: JournalRecoveryState;
  } | null> {
    const candidates: readonly (VerifiedJournalGeneration & {
      readonly recoveryState: JournalRecoveryState;
    })[] = manifest === null
      ? []
      : [
          { ...manifest.current, recoveryState: "verified_generation_loaded" },
          ...(manifest.prior === null
            ? []
            : [{ ...manifest.prior, recoveryState: "prior_generation_recovered" as const }]),
        ];
    for (const candidate of candidates) {
      const database = await this.#openVerifiedGeneration(candidate);
      if (database !== null) {
        return {
          database,
          verifiedGeneration: candidate,
          recoveryState: candidate.recoveryState,
        };
      }
    }
    return null;
  }

  /** Read one candidate generation back, verify it byte-exactly and open it. */
  async #openVerifiedGeneration(
    candidate: VerifiedJournalGeneration,
  ): Promise<SqliteDatabase | null> {
    try {
      const fileName = generationFileName(candidate.generationNumber);
      if (!(await this.#fileStore.exists(fileName))) {
        return null;
      }
      const imageBytes = new Uint8Array(await this.#fileStore.readBinary(fileName));
      if (imageBytes.byteLength !== candidate.sizeBytes) {
        return null;
      }
      if ((await sha256Hex(imageBytes)) !== candidate.sha256) {
        return null;
      }
      const database = SqliteDatabase.openFromImage(this.#engineModule, imageBytes);
      if (!JournalPersistence.#databaseHasLifecycleSurface(database)) {
        database.close();
        return null;
      }
      return database;
    } catch {
      return null;
    }
  }

  /**
   * Verify the lifecycle surface is intact on a freshly-opened verified
   * generation: every required table — including
   * `lifecycle_event_operands` — must be present. A missing table means
   * the generation is corrupt and recovery must fall back instead of
   * trusting it.
   */
  static #databaseHasLifecycleSurface(database: SqliteDatabase): boolean {
    try {
      const tables = database.readAll(
        "select name from sqlite_master where type = 'table' order by name;",
      );
      const present = new Set<string>(
        (tables[0]?.values ?? []).map((row) => String(row[0])),
      );
      for (const required of JournalPersistence.#LIFECYCLE_REQUIRED_TABLES) {
        if (!present.has(required)) {
          return false;
        }
      }
      return true;
    } catch {
      return false;
    }
  }

  /** Record the recovery outcome in the working copy without re-publishing. */
  async #refreshRecoveredMeta(
    database: SqliteDatabase,
    verifiedGeneration: VerifiedJournalGeneration,
    recoveryState: JournalRecoveryState,
  ): Promise<void> {
    const meta = database.readJournalMeta();
    if (
      meta.lastVerifiedGeneration === verifiedGeneration.generationNumber &&
      meta.recoveryState === recoveryState &&
      meta.isReconcileRequired === this.#isReconcileRequired
    ) {
      return;
    }
    await database.runSerializedMutation((session) => {
      session.writeJournalMeta({
        ...session.readJournalMeta(),
        lastVerifiedGeneration: verifiedGeneration.generationNumber,
        recoveryState,
        isReconcileRequired: this.#isReconcileRequired,
      });
    });
  }

  async #rebuildEmptyJournal(isManifestPresent: boolean): Promise<void> {
    const hasJournalArtifacts = isManifestPresent || (await this.#hasFirstGenerationFile());
    if (hasJournalArtifacts) {
      // Nothing verified but artifacts exist: preserve every Vault file and
      // mark the rebuilt journal for reconciliation (spec 6.2).
      this.#isReconcileRequired = true;
    }
    const recoveryState: JournalRecoveryState = hasJournalArtifacts
      ? "empty_journal_rebuilt"
      : "fresh_journal_created";
    const database = SqliteDatabase.createEmpty(this.#engineModule, {
      schemaVersion: JOURNAL_SCHEMA_VERSION,
      dirtyGeneration: 0,
      lastVerifiedGeneration: 0,
      isReconcileRequired: this.#isReconcileRequired,
      recoveryState,
    });
    this.#database = database;
    this.#recoveryState = recoveryState;
    await this.#executeGenerationCommit(() => undefined);
  }

  async #hasFirstGenerationFile(): Promise<boolean> {
    try {
      return await this.#fileStore.exists(generationFileName(1));
    } catch {
      // A probe error is not an absent generation file: fail closed so a
      // transient adapter failure can never masquerade as a fresh store.
      throw journalStoreError("journal_store_unavailable");
    }
  }

  async #executeGenerationCommit<T>(
    operation: (session: SqliteMutationSession) => T | Promise<T>,
  ): Promise<T> {
    const database = this.#requireOpenedDatabase();
    const nextGenerationNumber = (this.#verifiedGeneration?.generationNumber ?? 0) + 1;
    const result = await database.runSerializedMutation(async (session) => {
      const operationResult = await operation(session);
      // Merge, never clobber: a repository that set `reconcile_required`
      // inside this transaction keeps the flag through the meta rewrite
      // (spec 6.4), and the sticky in-memory view adopts it for every
      // later generation.
      const sessionMeta = session.readJournalMeta();
      this.#isReconcileRequired ||= sessionMeta.isReconcileRequired;
      session.writeJournalMeta({
        ...sessionMeta,
        dirtyGeneration: nextGenerationNumber,
        isReconcileRequired: this.#isReconcileRequired,
      });
      return operationResult;
    });
    try {
      await this.#publishGeneration(nextGenerationNumber);
    } catch (error) {
      this.#recordGenerationPublishFailure(error);
      throw error;
    }
    return result;
  }

  /**
   * The closed-token view of generation publish failures (fix round 5):
   * the total count plus the last bounded reason tokens, newest last.
   * In-memory only; closed vocabulary only.
   */
  readGenerationPublishFailureSummary(): {
    readonly count: number;
    readonly lastReasons: readonly JournalStoreErrorReason[];
  } {
    return {
      count: this.#generationPublishFailureCount,
      lastReasons: [...this.#generationPublishFailureReasons],
    };
  }

  /** Record one publish failure's closed reason, if it has one. */
  #recordGenerationPublishFailure(error: unknown): void {
    if (!(error instanceof JournalStoreError)) {
      return;
    }
    this.#generationPublishFailureCount += 1;
    this.#generationPublishFailureReasons.push(error.reason);
    if (
      this.#generationPublishFailureReasons.length > MAX_GENERATION_PUBLISH_FAILURE_REASONS
    ) {
      this.#generationPublishFailureReasons.shift();
    }
  }

  /**
   * The generation protocol of spec 6.2: write the image, read it back and
   * verify size/digest, publish the manifest, verify it, and only then
   * switch the verified chain and retire the older generation file.
   */
  async #publishGeneration(generationNumber: number): Promise<void> {
    const database = this.#requireOpenedDatabase();
    const image = database.exportImage();
    const sizeBytes = image.byteLength;
    const sha256 = await sha256Hex(image);
    const fileName = generationFileName(generationNumber);
    try {
      await this.#fileStore.writeBinary(fileName, toArrayBuffer(image));
      const readBack = new Uint8Array(await this.#fileStore.readBinary(fileName));
      if (readBack.byteLength !== sizeBytes || (await sha256Hex(readBack)) !== sha256) {
        throw journalStoreError("journal_generation_write_failed");
      }
    } catch (error) {
      throw error instanceof JournalStoreError
        ? error
        : journalStoreError("journal_generation_write_failed");
    }
    const manifest: JournalGenerationManifest = {
      contract: JOURNAL_MANIFEST_CONTRACT,
      current: { generationNumber, sizeBytes, sha256, schemaVersion: JOURNAL_SCHEMA_VERSION },
      prior: this.#verifiedGeneration,
    };
    await this.#writeVerifiedManifest(manifest);
    // Verified: switch the chain, refresh the working meta, then retire the
    // generation file that fell out of the retained window (best effort).
    // The meta rewrite keeps merging the reconcile flag so a repository-set
    // flag survives every later verified publication (spec 6.4).
    const retiredGeneration = this.#priorVerifiedGeneration;
    this.#priorVerifiedGeneration = this.#verifiedGeneration;
    this.#verifiedGeneration = manifest.current;
    await database.runSerializedMutation((session) => {
      const meta = session.readJournalMeta();
      this.#isReconcileRequired ||= meta.isReconcileRequired;
      session.writeJournalMeta({
        ...meta,
        dirtyGeneration: generationNumber,
        lastVerifiedGeneration: generationNumber,
        isReconcileRequired: this.#isReconcileRequired,
      });
    });
    if (retiredGeneration !== null) {
      try {
        await this.#fileStore.remove(generationFileName(retiredGeneration.generationNumber));
      } catch {
        // Best-effort retention cleanup (spec 6.2).
      }
    }
  }

  async #writeVerifiedManifest(manifest: JournalGenerationManifest): Promise<void> {
    const manifestBytes = new TextEncoder().encode(JSON.stringify(manifest));
    try {
      await this.#fileStore.writeBinary(
        JOURNAL_MANIFEST_FILE_NAME,
        toArrayBuffer(manifestBytes),
      );
      const readBackBytes = new Uint8Array(
        await this.#fileStore.readBinary(JOURNAL_MANIFEST_FILE_NAME),
      );
      const readBack = parseJournalManifest(readBackBytes);
      if (readBack === null || !isSameJournalManifest(readBack, manifest)) {
        throw journalStoreError("journal_manifest_invalid");
      }
    } catch (error) {
      throw error instanceof JournalStoreError
        ? error
        : journalStoreError("journal_manifest_invalid");
    }
  }
}
