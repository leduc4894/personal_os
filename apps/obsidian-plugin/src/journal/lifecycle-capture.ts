/**
 * The Child 5 plugin-side Vault event lifecycle adapter (Task 8).
 *
 * The adapter sits between the Obsidian `Vault` rename / delete events and
 * the durable lifecycle repository. Every rename (same parent) and move
 * (changed parent) freezes any pending content work for the file, then
 * persists one lifecycle event in the same `journal_events` +
 * `lifecycle_event_operands` + `local_files` transaction the lifecycle
 * repository already owns. A delete writes a tombstone event and leaves
 * the local mapping marked as `tombstoned` (and therefore available to
 * the explicit restore surface); an untracked path is a quiet no-op.
 *
 * The 250 ms per-path settle delay from `FILE_SETTLE_DELAY_MS` is
 * applied before fingerprinting on the create / modify path; the same
 * delay is used here for rename / move debounce so a burst of rapid
 * notifications collapses into one durable event.
 *
 * Coalescing (spec 7.2): lifecycle events MUST NOT coalesce with content
 * events — `LifecycleRepository.recordLifecycleEvent` already enforces
 * this. The adapter never calls the content capture surface.
 *
 * Privacy (spec 9): every failure surfaces as a closed `JournalStoreError`
 * reason; paths, digests and provider detail never enter a thrown error
 * or any log surface.
 */

import { normalizePolicyLocator } from "../exclusion-policy/evaluator";
import type { JournalStoreErrorReason } from "./sqlite-database";
import { journalStoreError } from "./sqlite-database";
import { FILE_SETTLE_DELAY_MS } from "./contracts";
import {
  createLifecycleEventOperands,
  type LifecycleEventOperands,
  type LifecycleJournalOperation,
} from "./lifecycle-contracts";
import type { LifecycleRepository } from "./lifecycle-repository";
import type { JournalRepository } from "./repository";
import { deriveFrozenFingerprint } from "./fingerprint";

// --- structural Vault file surface -----------------------------------------------------------

/**
 * The narrow Vault file shape the lifecycle adapter consumes: every
 * Obsidian `TFile` exposes these fields and tests use plain objects
 * with the same shape. The adapter never reads the byte payload off the
 * file — that flows through {@link LifecycleVaultReader}.
 */
export interface VaultTargetFile {
  readonly path: string;
  readonly parent: { readonly path: string } | null;
}

/**
 * The structural shape of a rename target: a regular `TFile` plus the
 * prior path the Vault event delivers alongside. `TAbstractFile` does
 * not carry the prior path so the adapter takes it explicitly.
 */
export type VaultRenameTarget = VaultTargetFile;

/**
 * The narrow read-only Vault slice the lifecycle adapter depends on:
 * just the current bytes of one regular file (for tombstone verification
 * on explicit restore) and nothing else. The lifecycle adapter never
 * reads paths — those flow through the durable `local_files` mapping.
 */
export interface LifecycleVaultReader {
  readRegularFileBytes(normalizedPath: string): Promise<Uint8Array | null>;
}

// --- results -------------------------------------------------------------------------------

/**
 * The settled outcome of one rename or move capture: the lifecycle
 * operation that was recorded, the file's plugin-local identity, the
 * UUIDv7 event id that landed in the journal and the optional
 * predecessor event id (null for a non-restore capture).
 */
export interface LifecycleRenameResult {
  readonly operation: Extract<LifecycleJournalOperation, "rename" | "move">;
  readonly localFileId: string;
  readonly eventId: string;
  readonly predecessorEventId: string | null;
}

/**
 * The settled outcome of one explicit or automatic restore: the
 * lifecycle operation, the local file id, the UUIDv7 event id and the
 * predecessor delete event id the restore was ordered after.
 */
export interface LifecycleRestoreResult {
  readonly operation: "restore";
  readonly localFileId: string;
  readonly eventId: string;
  readonly predecessorEventId: string;
}

/**
 * The settled outcome of one delete capture: a structured summary of
 * what the adapter persisted, exposed for tests and the queue driver.
 */
export interface LifecycleDeleteResult {
  readonly localFileId: string;
  readonly tombstoneId: string;
  readonly eventId: string;
}

// --- options -------------------------------------------------------------------------------

export interface LifecycleCaptureOptions {
  readonly repository: JournalRepository;
  readonly lifecycle: LifecycleRepository;
  readonly vaultReader: LifecycleVaultReader | (() => LifecycleVaultReader);
  /** Clock for event creation timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
  /** Identity mint for event ids and tombstone ids; defaults to `crypto.randomUUID`. */
  readonly createId?: () => string;
  /** Policy revision the lifecycle decision is taken under. */
  readonly policyRevision: number;
  /** Optional settle-delay override; defaults to {@link FILE_SETTLE_DELAY_MS}. */
  readonly settleDelayMs?: number;
}

// --- helpers -------------------------------------------------------------------------------

function isPositiveInteger(value: number): boolean {
  return Number.isInteger(value) && value > 0;
}

function parseReason(error: unknown): JournalStoreErrorReason | null {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = (error as { reason?: unknown }).reason;
    if (typeof reason === "string") {
      return reason as JournalStoreErrorReason;
    }
  }
  return null;
}

function isStoreError(error: unknown): boolean {
  return parseReason(error) !== null;
}

function parentOfPath(path: string): string {
  const lastSlash = path.lastIndexOf("/");
  return lastSlash === -1 ? "" : path.slice(0, lastSlash);
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}

// --- the adapter ---------------------------------------------------------------------------

/**
 * The Child 5 Vault event lifecycle adapter. Constructed by the plugin
 * composition root over the existing `JournalRepository` (the lifecycle
 * repository is reached via `repository.lifecycle`) and the
 * {@link LifecycleVaultReader} the plugin already exposes.
 *
 * The instance owns one closed per-path settle queue so a burst of
 * rapid rename notifications collapses into one durable event; every
 * method runs all SQL through the existing `runSerializedMutation`
 * writer so the Child 4 / Child 5 schema invariants never break.
 */
export class LifecycleCapture {
  readonly #repository: JournalRepository;
  readonly #lifecycle: LifecycleRepository;
  readonly #vaultReader: LifecycleVaultReader;
  readonly #nowEpochMs: () => number;
  readonly #createId: () => string;
  readonly #policyRevision: number;
  readonly #settleDelayMs: number;

  readonly #settleTimers = new Map<string, ReturnType<typeof setTimeout>>();
  readonly #settleWaiters = new Map<string, Set<() => void>>();
  #isDisposed = false;

  constructor(options: LifecycleCaptureOptions) {
    if (!isPositiveInteger(options.policyRevision)) {
      throw new TypeError("invalid policy revision");
    }
    if (options.settleDelayMs !== undefined && !isPositiveInteger(options.settleDelayMs)) {
      throw new TypeError("invalid settle delay");
    }
    this.#repository = options.repository;
    this.#lifecycle = options.lifecycle;
    this.#vaultReader =
      typeof options.vaultReader === "function" ? options.vaultReader() : options.vaultReader;
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    this.#createId = options.createId ?? (() => crypto.randomUUID());
    this.#policyRevision = options.policyRevision;
    this.#settleDelayMs = options.settleDelayMs ?? FILE_SETTLE_DELAY_MS;
  }

  /**
   * Observe one Vault rename notification. The `priorPath` is the
   * pre-rename locator (the durable row still references it); `file.path`
   * is the new locator and `file.parent.path` decides between `rename`
   * (same parent) and `move` (different parent). The per-path settle
   * delay is applied before the durable record so a burst of rapid
   * rename notifications collapses into one event.
   *
   * An untracked prior path resolves to `null` (no event minted); a
   * missing local source identity resolves to `null` after durably
   * flagging `reconcile_required` (fail-closed). A thrown journal
   * store error propagates to the caller.
   */
  captureRename(file: VaultRenameTarget, priorPath: string): Promise<LifecycleRenameResult | null> {
    if (this.#isDisposed) {
      return Promise.resolve(null);
    }
    const normalizedPrior = this.#normalizePathOrNull(priorPath);
    const normalizedNew = this.#normalizePathOrNull(file.path);
    if (normalizedPrior === null || normalizedNew === null) {
      return Promise.resolve(null);
    }
    const operation = this.#renameOperation(normalizedPrior, normalizedNew);
    const settleKey = `${operation}:${normalizedPrior}->${normalizedNew}`;
    return new Promise<LifecycleRenameResult | null>((resolve, reject) => {
      const waiters = this.#settleWaiters.get(settleKey) ?? new Set<() => void>();
      let settled = false;
      waiters.add(() => {
        if (settled) {
          return;
        }
        settled = true;
        this.#commitRename(operation, normalizedPrior, normalizedNew).then(
          (result) => {
            resolve(result);
          },
          (error: unknown) => {
            if (isStoreError(error)) {
              reject(error);
            } else {
              reject(journalStoreError("journal_mutation_failed"));
            }
          },
        );
      });
      this.#settleWaiters.set(settleKey, waiters);
      const running = this.#settleTimers.get(settleKey);
      if (running !== undefined) {
        clearTimeout(running);
      }
      this.#settleTimers.set(
        settleKey,
        setTimeout(() => {
          this.#settleTimers.delete(settleKey);
          const pending = this.#settleWaiters.get(settleKey);
          this.#settleWaiters.delete(settleKey);
          if (pending === undefined) {
            return;
          }
          for (const run of pending) {
            run();
          }
        }, this.#settleDelayMs),
      );
    });
  }

  /**
   * Observe one Vault delete notification. An untracked path is a
   * quiet no-op (no lifecycle event minted); a tracked path freezes
   * any pending content work, persists a delete lifecycle event in the
   * same transaction the lifecycle repository already owns, and marks
   * the local mapping as `tombstoned` so the explicit restore surface
   * can reach it. The `tombstoneId` is minted from the same identity
   * seam the lifecycle repository uses.
   */
  async captureDelete(file: VaultTargetFile, tombstoneId?: string): Promise<LifecycleDeleteResult | null> {
    if (this.#isDisposed) {
      return null;
    }
    const normalizedPath = this.#normalizePathOrNull(file.path);
    if (normalizedPath === null) {
      return null;
    }
    const localFile = this.#repository.readLocalFileByPath(normalizedPath);
    if (localFile === null) {
      return null;
    }
    if (localFile.sourceId === null || localFile.baseVersionId === null) {
      // Fail closed: missing identity — a later pass must reconcile the row.
      await this.#flagReconcileRequired().catch(() => undefined);
      return null;
    }
    await this.#repository.freezePendingForLocalFile(localFile.localFileId);
    const issuedTombstoneId = tombstoneId ?? this.#createId();
    const operands = this.#buildOperands({
      operation: "delete",
      sourceId: localFile.sourceId,
      expectedVersionId: localFile.baseVersionId,
      expectedLocator: normalizedPath,
      targetLocator: null,
      tombstoneId: issuedTombstoneId,
      predecessorEventId: null,
    });
    const result = await this.#lifecycle.recordLifecycleEvent(operands, {
      localFile,
      tombstoneId: issuedTombstoneId,
    });
    return {
      localFileId: localFile.localFileId,
      tombstoneId: issuedTombstoneId,
      eventId: result.eventId,
    };
  }

  /**
   * The user-driven restore surface: pick a retained `localFileId` and a
   * target path, then ask the adapter to record a `restore` event in
   * one transaction. The adapter first verifies the target path's bytes
   * still hash to the file's last committed content hash; a mismatch
   * rejects with `journal_mutation_failed` (the queue driver can route
   * the failure to the user).
   *
   * The `localFileId` must point at a tracked row whose
   * `lifecycle_state` is `tombstoned`; otherwise the restore is rejected
   * before any SQL runs.
   */
  async requestRestore(
    localFileId: string,
    targetPath: string,
  ): Promise<LifecycleRestoreResult> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    const normalizedTarget = this.#normalizePathOrNull(targetPath);
    if (normalizedTarget === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const localFile = this.#repository.readLocalFileByLocalFileId(localFileId);
    if (
      localFile === null ||
      localFile.sourceId === null ||
      localFile.baseVersionId === null ||
      localFile.observedFingerprint === null
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    // Tombstone must be retained: the file row exists with an open tombstone.
    const openTombstoneId = this.#readOpenTombstoneId(localFileId);
    if (openTombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    // Verify the target path's bytes still hash to the retained content.
    const targetBytes = await this.#vaultReader.readRegularFileBytes(normalizedTarget);
    if (targetBytes === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
    if (
      targetFingerprint.sha256 !== localFile.observedFingerprint.sha256 ||
      targetFingerprint.sizeBytes !== localFile.observedFingerprint.sizeBytes
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    // Predecessor: the most recent `delete` event on this file.
    const predecessorEventId = this.#readPredecessorDeleteEventId(localFileId);
    if (predecessorEventId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#repository.freezePendingForLocalFile(localFileId);
    const operands = this.#buildOperands({
      operation: "restore",
      sourceId: localFile.sourceId,
      expectedVersionId: localFile.baseVersionId,
      expectedLocator: null,
      targetLocator: normalizedTarget,
      tombstoneId: openTombstoneId,
      predecessorEventId,
    });
    const result = await this.#lifecycle.recordLifecycleEvent(operands, {
      localFile,
      tombstoneId: openTombstoneId,
    });
    return {
      operation: "restore",
      localFileId,
      eventId: result.eventId,
      predecessorEventId,
    };
  }

  /**
   * The automatic restore detector: when a Vault create/modify event
   * re-uses a path whose local mapping is tombstoned, the capture path
   * calls this before recording a fresh `create`. The detector rejects
   * (with `journal_mutation_failed`) unless BOTH conditions hold:
   *
   *   1. the `local_files` row is still mapped to a retained source id;
   *   2. the bytes at the path hash to the file's last committed
   *      content hash.
   *
   * On success the adapter records a `restore` event in one
   * transaction and consumes the tombstone via
   * {@link LifecycleRepository.consumeRestoreSuccessor}.
   */
  async detectAutomaticRestore(normalizedPath: string): Promise<LifecycleRestoreResult> {
    if (this.#isDisposed) {
      throw journalStoreError("journal_mutation_failed");
    }
    const cleanedPath = this.#normalizePathOrNull(normalizedPath);
    if (cleanedPath === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const localFile = this.#repository.readLocalFileByPath(cleanedPath);
    if (
      localFile === null ||
      localFile.sourceId === null ||
      localFile.baseVersionId === null ||
      localFile.observedFingerprint === null
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    const openTombstoneId = this.#readOpenTombstoneId(localFile.localFileId);
    if (openTombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const targetBytes = await this.#vaultReader.readRegularFileBytes(cleanedPath);
    if (targetBytes === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
    if (
      targetFingerprint.sha256 !== localFile.observedFingerprint.sha256 ||
      targetFingerprint.sizeBytes !== localFile.observedFingerprint.sizeBytes
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    const predecessorEventId = this.#readPredecessorDeleteEventId(localFile.localFileId);
    if (predecessorEventId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#repository.freezePendingForLocalFile(localFile.localFileId);
    const operands = this.#buildOperands({
      operation: "restore",
      sourceId: localFile.sourceId,
      expectedVersionId: localFile.baseVersionId,
      expectedLocator: null,
      targetLocator: cleanedPath,
      tombstoneId: openTombstoneId,
      predecessorEventId,
    });
    const result = await this.#lifecycle.recordLifecycleEvent(operands, {
      localFile,
      tombstoneId: openTombstoneId,
    });
    await this.#lifecycle.consumeRestoreSuccessor(localFile.localFileId);
    return {
      operation: "restore",
      localFileId: localFile.localFileId,
      eventId: result.eventId,
      predecessorEventId,
    };
  }

  /** Settle all queued rename observations and stop accepting new ones. */
  dispose(): void {
    this.#isDisposed = true;
    for (const timer of this.#settleTimers.values()) {
      clearTimeout(timer);
    }
    this.#settleTimers.clear();
    for (const [, waiters] of this.#settleWaiters) {
      for (const resolve of waiters) {
        resolve();
      }
    }
    this.#settleWaiters.clear();
  }

  // --- internals ---------------------------------------------------------------------------

  /** The rename operation token chosen by the parent-directory comparison. */
  #renameOperation(priorPath: string, newPath: string): "rename" | "move" {
    return parentOfPath(priorPath) === parentOfPath(newPath) ? "rename" : "move";
  }

  /**
   * Persist one rename or move lifecycle event. The freeze of pending
   * content events is recorded first so no later queue pass selects
   * the file; the lifecycle event then lands in the same transaction
   * the lifecycle repository already owns.
   */
  async #commitRename(
    operation: "rename" | "move",
    priorPath: string,
    newPath: string,
  ): Promise<LifecycleRenameResult | null> {
    let localFile;
    try {
      localFile = this.#repository.readLocalFileByPath(priorPath);
    } catch (error) {
      // A closed / unavailable store surfaces as journal_mutation_failed
      // so the caller treats the unload as a hard fail.
      if (isStoreError(error)) {
        throw journalStoreError("journal_mutation_failed");
      }
      throw error;
    }
    if (localFile === null) {
      return null;
    }
    if (localFile.sourceId === null || localFile.baseVersionId === null) {
      await this.#flagReconcileRequired().catch(() => undefined);
      return null;
    }
    await this.#repository.freezePendingForLocalFile(localFile.localFileId);
    const operands = this.#buildOperands({
      operation,
      sourceId: localFile.sourceId,
      expectedVersionId: localFile.baseVersionId,
      expectedLocator: priorPath,
      targetLocator: newPath,
      tombstoneId: null,
      predecessorEventId: null,
    });
    const result = await this.#lifecycle.recordLifecycleEvent(operands, {
      localFile,
    });
    return {
      operation,
      localFileId: localFile.localFileId,
      eventId: result.eventId,
      predecessorEventId: null,
    };
  }

  /** Build one validated operand record from the raw capture inputs. */
  #buildOperands(input: {
    readonly operation: LifecycleJournalOperation;
    readonly sourceId: string;
    readonly expectedVersionId: string;
    readonly expectedLocator: string | null;
    readonly targetLocator: string | null;
    readonly tombstoneId: string | null;
    readonly predecessorEventId: string | null;
  }): LifecycleEventOperands {
    return createLifecycleEventOperands({
      operation: input.operation,
      sourceId: input.sourceId,
      expectedVersionId: input.expectedVersionId,
      expectedLocator: input.expectedLocator,
      targetLocator: input.targetLocator,
      tombstoneId: input.tombstoneId,
      policyRevision: this.#policyRevision,
      predecessorEventId: input.predecessorEventId,
    });
  }

  /** Normalize one Vault path to the canonical locator, or drop it closed. */
  #normalizePathOrNull(path: string): string | null {
    if (typeof path !== "string") {
      return null;
    }
    try {
      return normalizePolicyLocator(path);
    } catch {
      return null;
    }
  }

  /** Read the open tombstone id of one tracked file from `local_files`. */
  #readOpenTombstoneId(localFileId: string): string | null {
    try {
      const row = this.#repository
        .lifecycle.database.readAll(
          `select open_tombstone_id from local_files where local_file_id = '${localFileId}';`,
        )[0]?.values[0]?.[0];
      return typeof row === "string" && row.length > 0 ? row : null;
    } catch {
      return null;
    }
  }

  /** Read the most recent `delete` event id of one tracked file, or null. */
  #readPredecessorDeleteEventId(localFileId: string): string | null {
    const events = this.#repository.readEventsByLocalFileId(localFileId);
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if (event !== undefined && event.operation === "delete") {
        return event.eventId;
      }
    }
    return null;
  }

  /** Set the journal `is_reconcile_required` flag durably (spec 6.4). */
  async #flagReconcileRequired(): Promise<void> {
    const database = this.#repository.lifecycle.database;
    const read = (sql: string): readonly unknown[] =>
      database.readAll(sql)[0]?.values[0] ?? [];
    await this.#repository.lifecycle.database.runSerializedMutation(async (session) => {
      const row = session.readRows("select is_reconcile_required from journal_meta where singleton_key = 1;")[0]?.values[0]?.[0];
      void read;
      if (row === 0) {
        session.exec("update journal_meta set is_reconcile_required = 1 where singleton_key = 1;");
      }
    });
  }
}

// Re-export to keep the public surface explicit.
export type { LifecycleJournalOperation } from "./lifecycle-contracts";
