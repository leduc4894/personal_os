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
 * notifications collapses into one durable event. After the settle
 * completes, the rename / move path reads the target file bytes and the
 * frozen fingerprint is recorded in the SAME transaction as the
 * lifecycle event so a half-finished rename never escapes a verified
 * generation.
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
import type { EchoSuppressor } from "../device-sync/echo-suppression";
import type { JournalStoreErrorReason } from "./sqlite-database";
import { journalStoreError } from "./sqlite-database";
import { FILE_SETTLE_DELAY_MS, JOURNAL_PENDING_EVENT_STATES } from "./contracts";
import {
  createLifecycleEventOperands,
  type LifecycleEventOperands,
  type LifecycleJournalOperation,
  type RestoreReservationResult,
} from "./lifecycle-contracts";
import {
  PendingRenameIntentConflictError,
  type LifecycleRepository,
} from "./lifecycle-repository";
import type { JournalRepository } from "./repository";
import { deriveFrozenFingerprint } from "./fingerprint";
import type { JournalFailureReporter } from "./diagnostic-reporter";

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
  readonly capturedFingerprintSha256: string;
  readonly capturedFingerprintSizeBytes: number;
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

// --- the public port (brief requirement) ----------------------------------------------------

/**
 * The required Vault event lifecycle port the brief freezes. The
 * {@link LifecycleCaptureImpl} class satisfies this interface; tests that
 * compose against the port never have to know which class implements
 * it. The interface exposes only the three required methods — the
 * settle / fingerprinting helpers, the dispose hook, the automatic
 * restore detector and the reconcile-required flagger stay on the
 * class so a port fake never needs them.
 */
export interface LifecycleCapture {
  captureRename(
    file: VaultRenameTarget,
    priorPath: string,
    context?: LifecycleRenameCaptureContext,
  ): Promise<LifecycleRenameResult | null>;
  captureDelete(
    file: VaultTargetFile,
    tombstoneId?: string,
  ): Promise<LifecycleDeleteResult | null>;
  requestRestore(
    localFileId: string,
    targetPath: string,
  ): Promise<LifecycleRestoreResult>;
}

/** Synchronous owner handoff for the caller's delayed rename-tail admission. */
export interface LifecycleRenameCaptureContext {
  readonly onOwnerResolved?: ((localFileId: string) => void) | undefined;
}

// --- options -------------------------------------------------------------------------------

export interface LifecycleCaptureOptions {
  readonly repository: JournalRepository;
  readonly lifecycle: LifecycleRepository;
  readonly vaultReader: LifecycleVaultReader | (() => LifecycleVaultReader);
  /** Clock for event creation timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
  /**
   * Identity mint for event ids and tombstone ids; defaults to
   * `crypto.randomUUID` (UUIDv4). Inject a UUIDv7 factory
   * (see {@link createUuidv7Factory}) so the durable journal carries
   * time-ordered ids.
   */
  readonly createId?: () => string;
  /** Policy revision the lifecycle decision is taken under. */
  readonly policyRevision: number;
  /** Optional settle-delay override; defaults to {@link FILE_SETTLE_DELAY_MS}. */
  readonly settleDelayMs?: number;
  /**
   * Bounded re-arm attempts for a settle that lands while the row's create
   * upload is still in flight (identity not yet landed). Defaults to
   * {@link SETTLE_DEFERRAL_ATTEMPTS}. Each attempt waits one settle delay.
   */
  readonly settleDeferralAttempts?: number;
  /** Closed-token reporter owned by the plugin composition root. */
  readonly failureReporter?: JournalFailureReporter | null;
  /**
   * The exact echo suppressor (device cursor child 6, task 10): a
   * rename/move notification that exactly matches the durable marker of
   * our own remote apply is consumed and never recorded as a lifecycle
   * event. Absent means nothing is suppressed.
   */
  readonly echoSuppressor?: EchoSuppressor | null;
}

// --- helpers -------------------------------------------------------------------------------

/**
 * The bounded settle-deferral attempt budget: a rename or delete settle that
 * lands while the row's create upload is still in flight re-arms up to this
 * many times (each waiting one settle delay) before the fail-closed
 * reconcile flag. The live defect (2026-09-03): an operator renaming a note
 * seconds after creating it — the create receipt had not landed yet —
 * hard-stopped the whole journal instead of waiting out the upload.
 */
export const SETTLE_DEFERRAL_ATTEMPTS = 40;

/**
 * The closed settle outcome meaning "the observation raced a still-in-flight
 * create; re-arm the settle" — never a journal mutation, never a flag. The
 * class-internal sentinel never escapes the capture module.
 */
const SETTLE_DEFERRED: unique symbol = Symbol("settle-deferred");

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
 *
 * Implements the required {@link LifecycleCapture} port; the helper
 * methods (`detectAutomaticRestore`, `markTombstonedPathReconcileRequired`,
 * `dispose`) stay on the class only.
 */
export class LifecycleCaptureImpl implements LifecycleCapture {
  readonly #repository: JournalRepository;
  readonly #lifecycle: LifecycleRepository;
  readonly #vaultReader: LifecycleVaultReader;
  readonly #nowEpochMs: () => number;
  readonly #createId: () => string;
  readonly #policyRevision: number;
  readonly #settleDelayMs: number;
  readonly #settleDeferralAttempts: number;
  readonly #failureReporter: JournalFailureReporter | null;
  readonly #echoSuppressor: EchoSuppressor | null;

  readonly #settleTimers = new Map<string, ReturnType<typeof setTimeout>>();
  readonly #settleWaiters = new Map<string, Set<() => void>>();
  readonly #pendingRenameTimers = new Map<string, ReturnType<typeof setTimeout>>();
  readonly #pendingRenameWaiters = new Map<
    string,
    Set<{
      readonly resolve: (result: LifecycleRenameResult | null) => void;
      readonly reject: (error: unknown) => void;
    }>
  >();
  readonly #pendingRenameDeferralBudget = new Map<string, number>();
  readonly #ownerBoundRenamePredecessors = new Map<
    string,
    {
      readonly localFileId: string;
      readonly ownedRowPath: string;
      readonly observationToken: symbol;
    }
  >();
  readonly #pendingRenameMutationTails = new Map<string, Promise<void>>();
  readonly #deleteDeferralTimers = new Map<string, ReturnType<typeof setTimeout>>();
  #isDisposed = false;

  constructor(options: LifecycleCaptureOptions) {
    if (!isPositiveInteger(options.policyRevision)) {
      throw new TypeError("invalid policy revision");
    }
    if (options.settleDelayMs !== undefined && !isPositiveInteger(options.settleDelayMs)) {
      throw new TypeError("invalid settle delay");
    }
    if (
      options.settleDeferralAttempts !== undefined &&
      !isPositiveInteger(options.settleDeferralAttempts)
    ) {
      throw new TypeError("invalid settle deferral attempts");
    }
    this.#repository = options.repository;
    this.#lifecycle = options.lifecycle;
    this.#vaultReader =
      typeof options.vaultReader === "function" ? options.vaultReader() : options.vaultReader;
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    this.#createId = options.createId ?? (() => crypto.randomUUID());
    this.#policyRevision = options.policyRevision;
    this.#settleDelayMs = options.settleDelayMs ?? FILE_SETTLE_DELAY_MS;
    this.#settleDeferralAttempts = options.settleDeferralAttempts ?? SETTLE_DEFERRAL_ATTEMPTS;
    this.#failureReporter = options.failureReporter ?? null;
    this.#echoSuppressor = options.echoSuppressor ?? null;
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
   *
   * After the settle completes, the target file bytes are read and
   * fingerprinted; the lifecycle event and the `local_files` path
   * rebind land in one transaction (spec 7.1 fix round 1 I1 + I2).
   */
  captureRename(
    file: VaultRenameTarget,
    priorPath: string,
    context?: LifecycleRenameCaptureContext,
  ): Promise<LifecycleRenameResult | null> {
    if (this.#isDisposed) {
      return Promise.resolve(null);
    }
    const normalizedPrior = this.#normalizePathOrNull(priorPath);
    const normalizedNew = this.#normalizePathOrNull(file.path);
    if (normalizedPrior === null || normalizedNew === null) {
      return Promise.resolve(null);
    }
    let intentOwner: {
      readonly localFile: import("./contracts").LocalFile;
      readonly hasPendingIntent: boolean;
    } | null;
    try {
      intentOwner = this.#resolvePendingRenameOwner(normalizedPrior);
    } catch (error) {
      if (isStoreError(error)) {
        return Promise.reject(journalStoreError("journal_mutation_failed"));
      }
      return Promise.reject(journalStoreError("journal_mutation_failed"));
    }
    if (intentOwner !== null) {
      context?.onOwnerResolved?.(intentOwner.localFile.localFileId);
    }
    if (
      intentOwner !== null &&
      (intentOwner.hasPendingIntent ||
        (intentOwner.localFile.sourceId === null &&
          intentOwner.localFile.baseVersionId === null &&
          this.#hasInFlightEvent(intentOwner.localFile.localFileId)))
    ) {
      return this.#capturePendingRenameObservation(
        intentOwner.localFile.localFileId,
        normalizedPrior,
        normalizedNew,
      );
    }
    const operation = this.#renameOperation(normalizedPrior, normalizedNew);
    const settleKey = `${operation}:${normalizedPrior}->${normalizedNew}`;
    return new Promise<LifecycleRenameResult | null>((resolve, reject) => {
      let deferralBudget = this.#settleDeferralAttempts;
      const armSettleTimer = (): void => {
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
      };
      const settleFailed = (error: unknown): void => {
        if (isStoreError(error)) {
          reject(error);
        } else {
          reject(journalStoreError("journal_mutation_failed"));
        }
      };
      const attempt = (): void => {
        if (this.#isDisposed) {
          resolve(null);
          return;
        }
        this.#commitRenameWithRebind(operation, normalizedPrior, normalizedNew).then(
          (result) => {
            if (result !== SETTLE_DEFERRED) {
              resolve(result);
              return;
            }
            // The observation raced a still-in-flight create (identity not
            // yet landed): re-arm the settle instead of flagging the whole
            // journal. The create either lands its receipt (the re-armed
            // settle then records normally), terminalizes failed (the
            // uncommitted-transit heal then owns the row) or never settles
            // (the exhausted budget keeps the fail-closed flag).
            if (deferralBudget <= 0) {
              void this.#flagReconcileRequiredOrReport().then(
                () => resolve(null),
                settleFailed,
              );
              return;
            }
            deferralBudget -= 1;
            const rePending = this.#settleWaiters.get(settleKey) ?? new Set<() => void>();
            rePending.add(attempt);
            this.#settleWaiters.set(settleKey, rePending);
            armSettleTimer();
          },
          settleFailed,
        );
      };
      const waiters = this.#settleWaiters.get(settleKey) ?? new Set<() => void>();
      waiters.add(attempt);
      this.#settleWaiters.set(settleKey, waiters);
      const running = this.#settleTimers.get(settleKey);
      if (running !== undefined) {
        clearTimeout(running);
      }
      armSettleTimer();
    });
  }

  /**
   * Resolve a watcher edge only through its durable owner proof: the local
   * row at the observed prior endpoint, or the one intent whose current
   * endpoint equals that prior. A bare path miss never manufactures an
   * owner, which preserves the provenance boundary for rapid A -> B -> C.
   */
  #resolvePendingRenameOwner(normalizedPrior: string): {
    readonly localFile: import("./contracts").LocalFile;
    readonly hasPendingIntent: boolean;
  } | null {
    const direct = this.#repository.readLocalFileByPath(normalizedPrior);
    if (direct !== null) {
      return {
        localFile: direct,
        hasPendingIntent:
          this.#readPendingRenameIntentForLocalFileOrReport(direct.localFileId) !== null,
      };
    }
    const intent = this.#readPendingRenameIntentByCurrentPathOrReport(normalizedPrior);
    if (intent !== null) {
      const owner = this.#repository.readLocalFileByLocalFileId(intent.localFileId);
      if (owner === null) {
        return null;
      }
      return { localFile: owner, hasPendingIntent: true };
    }
    const predecessor = this.#ownerBoundRenamePredecessors.get(normalizedPrior);
    if (predecessor === undefined) {
      return null;
    }
    const owner = this.#repository.readLocalFileByLocalFileId(predecessor.localFileId);
    if (owner === null || owner.normalizedPath !== predecessor.ownedRowPath) {
      return null;
    }
    return { localFile: owner, hasPendingIntent: true };
  }

  /** Persist the owned edge before any settle delay, then coalesce by owner. */
  async #capturePendingRenameObservation(
    localFileId: string,
    observedPriorPath: string,
    observedCurrentPath: string,
  ): Promise<LifecycleRenameResult | null> {
    if (this.#readLifecycleState(localFileId) === "restore_pending") {
      return null;
    }
    const owner = this.#repository.readLocalFileByLocalFileId(localFileId);
    if (owner === null) {
      return null;
    }
    const observationToken = Symbol("owner-bound-rename-observation");
    this.#ownerBoundRenamePredecessors.set(observedCurrentPath, {
      localFileId,
      ownedRowPath: owner.normalizedPath,
      observationToken,
    });
    const previousMutation = this.#pendingRenameMutationTails.get(localFileId);
    const mutation = (previousMutation ?? Promise.resolve()).then(async () => {
      await this.#lifecycle.recordOrComposePendingRenameIntent({
        localFileId,
        observedPriorPath,
        observedCurrentPath,
      });
    });
    this.#pendingRenameMutationTails.set(localFileId, mutation);
    try {
      await mutation;
    } catch (error) {
      if (error instanceof PendingRenameIntentConflictError) {
        this.#failureReporter?.reportJournalFailure("pending_rename_intent_conflict");
        return null;
      }
      this.#failureReporter?.reportJournalFailure("pending_rename_intent_persist_failed");
      if (isStoreError(error)) {
        throw error;
      }
      throw journalStoreError("journal_mutation_failed");
    } finally {
      if (this.#pendingRenameMutationTails.get(localFileId) === mutation) {
        this.#pendingRenameMutationTails.delete(localFileId);
      }
      const predecessor = this.#ownerBoundRenamePredecessors.get(observedCurrentPath);
      if (predecessor?.observationToken === observationToken) {
        this.#ownerBoundRenamePredecessors.delete(observedCurrentPath);
      }
    }
    return this.#schedulePendingRenameMaterialization(localFileId);
  }

  /** Arm one owner-scoped timer; each new linked observation resets it. */
  #schedulePendingRenameMaterialization(
    localFileId: string,
  ): Promise<LifecycleRenameResult | null> {
    return new Promise<LifecycleRenameResult | null>((resolve, reject) => {
      const waiters = this.#pendingRenameWaiters.get(localFileId) ?? new Set();
      waiters.add({ resolve, reject });
      this.#pendingRenameWaiters.set(localFileId, waiters);
      const previousTimer = this.#pendingRenameTimers.get(localFileId);
      if (previousTimer !== undefined) {
        clearTimeout(previousTimer);
      }
      if (!this.#pendingRenameDeferralBudget.has(localFileId)) {
        this.#pendingRenameDeferralBudget.set(localFileId, this.#settleDeferralAttempts);
      }
      this.#pendingRenameTimers.set(
        localFileId,
        setTimeout(() => {
          this.#pendingRenameTimers.delete(localFileId);
          void this.#settlePendingRenameIntent(localFileId);
        }, this.#settleDelayMs),
      );
    });
  }

  /** Re-read current endpoints and materialize at most one immutable prefix. */
  async #settlePendingRenameIntent(localFileId: string): Promise<void> {
    const resolveAll = (result: LifecycleRenameResult | null): void => {
      const waiters = this.#pendingRenameWaiters.get(localFileId);
      this.#pendingRenameWaiters.delete(localFileId);
      this.#pendingRenameDeferralBudget.delete(localFileId);
      for (const waiter of waiters ?? []) {
        waiter.resolve(result);
      }
    };
    const rejectAll = (error: unknown): void => {
      const waiters = this.#pendingRenameWaiters.get(localFileId);
      this.#pendingRenameWaiters.delete(localFileId);
      this.#pendingRenameDeferralBudget.delete(localFileId);
      for (const waiter of waiters ?? []) {
        waiter.reject(error);
      }
    };
    if (this.#isDisposed) {
      resolveAll(null);
      return;
    }
    try {
      let intent;
      try {
        intent = this.#lifecycle.readPendingRenameIntentForLocalFile(localFileId);
      } catch (error) {
        this.#reportPendingRenameIntentReadFailure();
        rejectAll(isStoreError(error) ? error : journalStoreError("journal_query_failed"));
        return;
      }
      if (intent === null) {
        resolveAll(null);
        return;
      }
      const localFile = this.#repository.readLocalFileByLocalFileId(localFileId);
      if (localFile === null || this.#readLifecycleState(localFileId) === "restore_pending") {
        resolveAll(null);
        return;
      }
      if (localFile.sourceId === null || localFile.baseVersionId === null) {
        if (this.#hasInFlightEvent(localFileId)) {
          const remainingBudget = this.#pendingRenameDeferralBudget.get(localFileId) ?? 0;
          if (remainingBudget <= 0) {
            await this.#flagReconcileRequiredOrReport();
            resolveAll(null);
            return;
          }
          this.#pendingRenameDeferralBudget.set(localFileId, remainingBudget - 1);
          this.#pendingRenameTimers.set(
            localFileId,
            setTimeout(() => {
              this.#pendingRenameTimers.delete(localFileId);
              void this.#settlePendingRenameIntent(localFileId);
            }, this.#settleDelayMs),
          );
          return;
        }
        if (this.#isUncommittedTransitRow(localFileId)) {
          await this.#lifecycle.reparentAndClearPendingRenameIntent(localFileId);
          resolveAll(null);
          return;
        }
        await this.#flagReconcileRequiredOrReport();
        resolveAll(null);
        return;
      }
      const targetBytes = await this.#vaultReader.readRegularFileBytes(intent.currentPath);
      if (targetBytes === null) {
        // A queue-owned content event may still exact-replay its receipt; do
        // not terminalize it or discard the owner reservation from a timer.
        resolveAll(null);
        return;
      }
      const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
      if (this.#echoSuppressor !== null) {
        const observation = {
          priorLocator: intent.priorPath,
          targetLocator: intent.currentPath,
          sourceId: localFile.sourceId,
          fingerprint: targetFingerprint,
        };
        const echoSuppressor = this.#echoSuppressor;
        const consumed = await this.#lifecycle.consumePendingRenameEchoAndReparent(
          localFileId,
          (session) => echoSuppressor.consumeRenameObservationInSession(session, observation),
        );
        if (consumed) {
          resolveAll(null);
          return;
        }
      }
      const prefix = await this.#lifecycle.recordPendingRenameLifecycleEvent(
        localFileId,
        targetFingerprint,
      );
      if (prefix === null) {
        resolveAll(null);
        return;
      }
      const operands = this.#lifecycle.readLifecycleOperands(prefix.eventId);
      if (operands === null || (operands.operation !== "rename" && operands.operation !== "move")) {
        await this.#flagReconcileRequiredOrReport();
        resolveAll(null);
        return;
      }
      resolveAll({
        operation: operands.operation,
        localFileId,
        eventId: prefix.eventId,
        predecessorEventId: null,
        capturedFingerprintSha256: targetFingerprint.sha256,
        capturedFingerprintSizeBytes: targetFingerprint.sizeBytes,
      });
    } catch (error) {
      if (error instanceof PendingRenameIntentConflictError) {
        this.#failureReporter?.reportJournalFailure("pending_rename_intent_conflict");
      } else {
        this.#failureReporter?.reportJournalFailure("pending_rename_intent_persist_failed");
      }
      rejectAll(isStoreError(error) ? error : journalStoreError("journal_mutation_failed"));
    }
  }

  #readPendingRenameIntentForLocalFileOrReport(localFileId: string) {
    try {
      return this.#lifecycle.readPendingRenameIntentForLocalFile(localFileId);
    } catch (error) {
      this.#reportPendingRenameIntentReadFailure();
      throw error;
    }
  }

  #readPendingRenameIntentByCurrentPathOrReport(normalizedPath: string) {
    try {
      return this.#lifecycle.readPendingRenameIntentByCurrentPath(normalizedPath);
    } catch (error) {
      this.#reportPendingRenameIntentReadFailure();
      throw error;
    }
  }

  #reportPendingRenameIntentReadFailure(): void {
    this.#failureReporter?.reportJournalFailure("pending_rename_intent_read_failed");
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
  async captureDelete(
    file: VaultTargetFile,
    tombstoneId?: string,
  ): Promise<LifecycleDeleteResult | null> {
    if (this.#isDisposed) {
      return null;
    }
    const normalizedPath = this.#normalizePathOrNull(file.path);
    if (normalizedPath === null) {
      return null;
    }
    let localFile = this.#repository.readLocalFileByPath(normalizedPath);
    // A pending rename owns both its prior and current durable endpoints,
    // while the local row remains at the prior path until the lifecycle
    // prefix is materialized.  A delete watcher can therefore legitimately
    // arrive at the current endpoint before that rebind.  Keep the
    // observation in the existing bounded delete ladder and let a later
    // retry re-read the owner after the prefix receipt.
    if (localFile === null) {
      const pendingIntent = this.#lifecycle.readPendingRenameIntentOwningEndpoint(
        normalizedPath,
      );
      if (pendingIntent !== null) {
        localFile = this.#repository.readLocalFileByLocalFileId(pendingIntent.localFileId);
        if (localFile !== null) {
          this.#scheduleDeleteDeferralRetry(normalizedPath, tombstoneId);
        }
        return null;
      }
    } else {
      const pendingIntent = this.#lifecycle.readPendingRenameIntentOwningEndpoint(normalizedPath);
      if (pendingIntent !== null && pendingIntent.localFileId === localFile.localFileId) {
      // The row still sits at an intent-owned prior endpoint.  Resolve the
      // rename prefix first so the delete operands use the committed target.
      this.#scheduleDeleteDeferralRetry(normalizedPath, tombstoneId);
      return null;
      }
    }
    if (localFile === null) {
      return null;
    }
    // A reserved row (or one whose restore event is in flight) owns the
    // path: deleting or renaming the staged bytes mid-flow is operator
    // staging action, not a tracked lifecycle transition — a quiet no-op.
    if (this.#readLifecycleState(localFile.localFileId) === "restore_pending") {
      return null;
    }
    if (localFile.sourceId === null || localFile.baseVersionId === null) {
      if (this.#isUncommittedTransitRow(localFile.localFileId)) {
        // Same uncommitted-transit heal as rename: an unsynced note the
        // operator deletes carries no canonical evidence — remove the
        // phantom mapping quietly instead of hard-stopping the journal.
        await this.#repository.removeLocalMapping(localFile.localFileId);
        return null;
      }
      if (this.#hasInFlightEvent(localFile.localFileId)) {
        // The create upload is still in flight (the 2026-09-03 live-defect
        // window, delete variant): consume the observation and retry it
        // bounded — the row is neither droppable nor flaggable while the
        // upload's outcome is pending.
        this.#scheduleDeleteDeferralRetry(normalizedPath, tombstoneId);
        return null;
      }
      // Fail closed: missing identity — a later pass must reconcile the row.
      await this.#flagReconcileRequiredOrReport();
      return null;
    }
    const issuedTombstoneId = tombstoneId ?? this.#createId();
    const result = await this.#lifecycle.recordLifecycleEventWithFreeze({
      operands: this.#buildOperands({
        operation: "delete",
        sourceId: localFile.sourceId,
        expectedVersionId: localFile.baseVersionId,
        expectedLocator: normalizedPath,
        targetLocator: null,
        tombstoneId: issuedTombstoneId,
        predecessorEventId: null,
      }),
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
   * Reserve one explicit-restore target locator (the reservation-first
   * protocol): the durable reservation lands the moment the restore
   * command accepts the target path, BEFORE any bytes are staged, so the
   * convergence lane can never ship the staged restore bytes as a fresh
   * source at the target. Delegates to
   * {@link LifecycleRepository.reserveRestoreTarget}; refusals come back
   * as the closed result shape (never a throw) and a persistence failure
   * rethrows the closed store reason after one
   * `restore_reservation_persist_failed` trail token.
   */
  async reserveRestoreTarget(
    localFileId: string,
    targetPath: string,
  ): Promise<RestoreReservationResult> {
    if (this.#isDisposed || !isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    const normalizedTarget = this.#normalizePathOrNull(targetPath);
    if (normalizedTarget === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    try {
      return await this.#lifecycle.reserveRestoreTarget(localFileId, normalizedTarget);
    } catch (error) {
      this.#failureReporter?.reportJournalFailure("restore_reservation_persist_failed");
      if (isStoreError(error)) {
        throw error;
      }
      throw journalStoreError("journal_mutation_failed");
    }
  }

  /**
   * Release one explicit-restore reservation (the restore command's
   * explicit Cancel path): the row returns to its pre-reservation path
   * and `tombstoned` state. Modal dismissal never releases — a dangling
   * reservation stays durable and resumable through the picker.
   */
  async releaseRestoreTarget(localFileId: string): Promise<void> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    try {
      await this.#lifecycle.releaseRestoreTarget(localFileId);
    } catch (error) {
      if (isStoreError(error)) {
        throw error;
      }
      throw journalStoreError("journal_mutation_failed");
    }
  }

  /**
   * The user-driven restore surface (confirm step of the
   * reservation-first protocol): the row must already be reserved —
   * `restore_pending` and rebound to the target path by
   * {@link reserveRestoreTarget} — before this method runs. The adapter
   * verifies the target path's bytes still hash to the file's last
   * committed content hash; a mismatch rejects with
   * `journal_mutation_failed` and the reservation stays resumable. The
   * tombstone is NEVER consumed here: only the committed receipt
   * advances the row past `restore_pending`.
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
    const localFile = this.#repository.readLocalFileByPath(normalizedTarget);
    if (
      localFile === null ||
      localFile.localFileId !== localFileId ||
      localFile.sourceId === null ||
      localFile.baseVersionId === null ||
      localFile.lastCommittedFingerprint === null
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    // Strict confirm-on-reserved: the row must sit at the target path in
    // `restore_pending` with the tombstone retained.
    if (this.#readLifecycleState(localFileId) !== "restore_pending") {
      throw journalStoreError("journal_mutation_failed");
    }
    const openTombstoneId = this.#readOpenTombstoneId(localFileId);
    if (openTombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    // Verify the target path's bytes still hash to the committed content
    // — the last-committed fingerprint, NOT the mutable observed one.
    const targetBytes = await this.#vaultReader.readRegularFileBytes(normalizedTarget);
    if (targetBytes === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
    if (
      targetFingerprint.sha256 !== localFile.lastCommittedFingerprint.sha256 ||
      targetFingerprint.sizeBytes !== localFile.lastCommittedFingerprint.sizeBytes
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    const predecessorEventId = this.#readPredecessorDeleteEventId(localFileId);
    if (predecessorEventId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const result = await this.#lifecycle.recordLifecycleEventWithFreeze({
      operands: this.#buildOperands({
        operation: "restore",
        sourceId: localFile.sourceId,
        expectedVersionId: localFile.baseVersionId,
        expectedLocator: null,
        targetLocator: normalizedTarget,
        tombstoneId: openTombstoneId,
        predecessorEventId,
      }),
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
   *   2. the bytes at the path hash to the file's LAST COMMITTED
   *      fingerprint (never the mutable observed fingerprint).
   *
   * On success the adapter records a `restore` event in one
   * transaction and consumes the tombstone via
   * {@link LifecycleRepository.consumeRestoreSuccessor}.
   *
   * Class-only helper (not on the {@link LifecycleCapture} port): the
   * capture composition uses it to detect automatic restores before
   * minting a fresh create.
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
      localFile.lastCommittedFingerprint === null
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    // Defense in depth: a row whose target is already reserved (or whose
    // restore event is in flight) belongs to the explicit flow — a second
    // restore would race the first into the closed tombstone family.
    if (this.#readLifecycleState(localFile.localFileId) === "restore_pending") {
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
      targetFingerprint.sha256 !== localFile.lastCommittedFingerprint.sha256 ||
      targetFingerprint.sizeBytes !== localFile.lastCommittedFingerprint.sizeBytes
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    const predecessorEventId = this.#readPredecessorDeleteEventId(localFile.localFileId);
    if (predecessorEventId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const result = await this.#lifecycle.recordLifecycleEventWithFreeze({
      operands: this.#buildOperands({
        operation: "restore",
        sourceId: localFile.sourceId,
        expectedVersionId: localFile.baseVersionId,
        expectedLocator: null,
        targetLocator: cleanedPath,
        tombstoneId: openTombstoneId,
        predecessorEventId,
      }),
      localFile,
      tombstoneId: openTombstoneId,
    });
    // No eager tombstone consumption: the record leaves the row at
    // `restore_pending` and only the committed receipt (through the
    // lifecycle driver) advances it to `restored`.
    return {
      operation: "restore",
      localFileId: localFile.localFileId,
      eventId: result.eventId,
      predecessorEventId,
    };
  }

  /**
   * Fail-closed reconcile flag: a tombstoned path that re-appeared with
   * bytes that do NOT match the last-committed fingerprint must NOT be
   * re-captured as a fresh create. Instead the lifecycle capture
   * durably flags the row as `reconcile_required`, drops the open
   * tombstone so the file is not eligible for automatic restore, and
   * returns `true`. The capture composition then refuses the create /
   * update admission (spec 7.1 fix round 1 C2).
   */
  async markTombstonedPathReconcileRequired(normalizedPath: string): Promise<boolean> {
    const cleanedPath = this.#normalizePathOrNull(normalizedPath);
    if (cleanedPath === null) {
      return false;
    }
    const localFile = this.#repository.readLocalFileByPath(cleanedPath);
    if (localFile === null) {
      return false;
    }
    const openTombstoneId = this.#readOpenTombstoneId(localFile.localFileId);
    if (openTombstoneId === null) {
      return false;
    }
    await this.#lifecycle.recordLifecycleReconcileForLocalFile(localFile.localFileId);
    return true;
  }

  /**
   * Re-arm every durable chain after journal recovery before automatic
   * snapshot admission or outbound dispatch. Enumeration failure is surfaced
   * as a closed read token and propagates so composition can fail closed.
   */
  async resumePendingRenameIntents(): Promise<void> {
    let intents;
    try {
      intents = this.#lifecycle.readPendingRenameIntents();
    } catch (error) {
      this.#failureReporter?.reportJournalFailure("pending_rename_intent_read_failed");
      if (isStoreError(error)) {
        throw error;
      }
      throw journalStoreError("journal_query_failed");
    }
    for (const intent of intents) {
      void this.#schedulePendingRenameMaterialization(intent.localFileId).catch((error: unknown) => {
        if (isStoreError(error)) {
          return;
        }
        this.#failureReporter?.reportJournalFailure("pending_rename_intent_persist_failed");
      });
    }
  }

  /** Re-arm one rebased successor after its immutable prefix receipt commits. */
  rearmPendingRenameIntent(localFileId: string): void {
    if (this.#isDisposed || !isUuid(localFileId)) {
      return;
    }
    void this.#schedulePendingRenameMaterialization(localFileId).catch(() => undefined);
  }

  /** Settle all queued rename observations and stop accepting new ones. */
  dispose(): void {
    this.#isDisposed = true;
    for (const timer of this.#settleTimers.values()) {
      clearTimeout(timer);
    }
    this.#settleTimers.clear();
    for (const timer of this.#deleteDeferralTimers.values()) {
      clearTimeout(timer);
    }
    this.#deleteDeferralTimers.clear();
    for (const [, waiters] of this.#settleWaiters) {
      for (const resolve of waiters) {
        resolve();
      }
    }
    this.#settleWaiters.clear();
    for (const timer of this.#pendingRenameTimers.values()) {
      clearTimeout(timer);
    }
    this.#pendingRenameTimers.clear();
    for (const [, waiters] of this.#pendingRenameWaiters) {
      for (const waiter of waiters) {
        waiter.resolve(null);
      }
    }
    this.#pendingRenameWaiters.clear();
    this.#pendingRenameDeferralBudget.clear();
    this.#ownerBoundRenamePredecessors.clear();
    this.#pendingRenameMutationTails.clear();
  }

  /**
   * Retry one delete observation whose row was mid-create-flight, bounded
   * by {@link LifecycleCaptureOptions.settleDeferralAttempts}: each retry
   * waits one settle delay and re-runs the whole delete classification
   * (identity may have landed, the create may have terminalized failed —
   * the transit heal then owns the row — or the budget exhausts and the
   * fail-closed reconcile flag keeps its meaning for genuine pathology).
   */
  #scheduleDeleteDeferralRetry(normalizedPath: string, tombstoneId?: string): void {
    const running = this.#deleteDeferralTimers.get(normalizedPath);
    if (running !== undefined) {
      // A retry for this path is already pending: the delete watcher
      // delivered a duplicate of the same observation.
      return;
    }
    const attempts = { remaining: this.#settleDeferralAttempts };
    const timer = setTimeout(() => {
      this.#deleteDeferralTimers.delete(normalizedPath);
      void this.#retryDeferredDelete(normalizedPath, tombstoneId, attempts);
    }, this.#settleDelayMs);
    this.#deleteDeferralTimers.set(normalizedPath, timer);
  }

  async #retryDeferredDelete(
    normalizedPath: string,
    tombstoneId: string | undefined,
    attempts: { remaining: number },
  ): Promise<void> {
    if (this.#isDisposed) {
      return;
    }
    let localFile = this.#repository.readLocalFileByPath(normalizedPath);
    const pendingIntent = this.#lifecycle.readPendingRenameIntentOwningEndpoint(normalizedPath);
    if (
      pendingIntent !== null &&
      (localFile === null || pendingIntent.localFileId === localFile.localFileId)
    ) {
      // The intent still reserves this endpoint.  The prefix may not yet
      // have been materialized, or its receipt may not yet have rebound and
      // cleared the intent; either way, recording a delete now would use the
      // wrong locator.  Retry through the same bounded ladder.
      localFile = this.#repository.readLocalFileByLocalFileId(pendingIntent.localFileId);
      if (localFile === null) {
        return;
      }
      if (attempts.remaining <= 0) {
        await this.#flagReconcileRequiredOrReport().catch(() => undefined);
        return;
      }
      attempts.remaining -= 1;
      const timer = setTimeout(() => {
        this.#deleteDeferralTimers.delete(normalizedPath);
        void this.#retryDeferredDelete(normalizedPath, tombstoneId, attempts);
      }, this.#settleDelayMs);
      this.#deleteDeferralTimers.set(normalizedPath, timer);
      return;
    }
    if (localFile === null) {
      // The row left (a concurrent observation healed or rebound it).
      return;
    }
    if (localFile.sourceId !== null && localFile.baseVersionId !== null) {
      // Identity landed: run the ordinary delete capture to its end.
      await this.captureDelete(
        { path: normalizedPath, parent: null },
        tombstoneId,
      ).catch(() => undefined);
      return;
    }
    if (this.#isUncommittedTransitRow(localFile.localFileId)) {
      await this.#repository.removeLocalMapping(localFile.localFileId).catch(() => undefined);
      return;
    }
    if (!this.#hasInFlightEvent(localFile.localFileId)) {
      await this.#flagReconcileRequiredOrReport().catch(() => undefined);
      return;
    }
    if (attempts.remaining <= 0) {
      await this.#flagReconcileRequiredOrReport().catch(() => undefined);
      return;
    }
    attempts.remaining -= 1;
    const timer = setTimeout(() => {
      this.#deleteDeferralTimers.delete(normalizedPath);
      void this.#retryDeferredDelete(normalizedPath, tombstoneId, attempts);
    }, this.#settleDelayMs);
    this.#deleteDeferralTimers.set(normalizedPath, timer);
  }

  // --- internals ---------------------------------------------------------------------------

  /**
   * Whether one tracked row is an uncommitted transit mapping: no source
   * identity, nothing in flight (`queued` / `preflight` / `uploading` /
   * `waiting_retry`) and nothing ever committed — a phantom whose only
   * history is dead terminal events (typically a create that closed
   * `blocked_conflict` on the vault's untitled-transit name). Such a row
   * carries no canonical evidence, so a rename or delete of it is
   * operator transit action, not corruption: the mapping is quietly
   * removed and the file re-admits fresh at its real name. A row with
   * live in-flight work or any committed history keeps the fail-closed
   * `reconcile_required` rule (an upload may still commit server-side).
   */
  #isUncommittedTransitRow(localFileId: string): boolean {
    const events = this.#repository.readEventsByLocalFileId(localFileId);
    if (events.length === 0) {
      return true;
    }
    const pendingStates: ReadonlySet<string> = new Set(JOURNAL_PENDING_EVENT_STATES);
    return events.every(
      (event) =>
        !pendingStates.has(event.state) &&
        event.state !== "committed" &&
        event.state !== "no_change",
    );
  }

  /**
   * Whether any content event of the row is still in a pending (in-flight)
   * state — an upload whose outcome, and with it the row's identity, has
   * not landed yet. The settle-deferral branch of the rename/delete capture
   * keys on this: the row is neither droppable transit (work is live) nor
   * flaggable corruption (the outcome is merely pending).
   */
  #hasInFlightEvent(localFileId: string): boolean {
    const pendingStates: ReadonlySet<string> = new Set(JOURNAL_PENDING_EVENT_STATES);
    return this.#repository
      .readEventsByLocalFileId(localFileId)
      .some((event) => pendingStates.has(event.state));
  }

  /** The rename operation token chosen by the parent-directory comparison. */
  #renameOperation(priorPath: string, newPath: string): "rename" | "move" {
    return parentOfPath(priorPath) === parentOfPath(newPath) ? "rename" : "move";
  }

  /**
   * Persist one rename or move lifecycle event AND rebind the
   * `local_files.normalized_path` to the new locator — both inside
   * the same transaction so a torn rename never leaves a row pointing
   * at the old path. After the per-path settle, the target file bytes
   * are fingerprinted and the fingerprint rides along on the operand.
   */
  async #commitRenameWithRebind(
    operation: "rename" | "move",
    priorPath: string,
    newPath: string,
  ): Promise<LifecycleRenameResult | null | typeof SETTLE_DEFERRED> {
    let localFile;
    try {
      localFile = this.#repository.readLocalFileByPath(priorPath);
    } catch (error) {
      if (isStoreError(error)) {
        throw journalStoreError("journal_mutation_failed");
      }
      throw error;
    }
    if (localFile === null) {
      return null;
    }
    // Same reserved-row guard as delete: the reservation owns the path.
    if (this.#readLifecycleState(localFile.localFileId) === "restore_pending") {
      return null;
    }
    if (localFile.sourceId === null || localFile.baseVersionId === null) {
      if (this.#isUncommittedTransitRow(localFile.localFileId)) {
        // Untitled-transit heal: nothing canonical ever existed for this
        // row — drop the phantom mapping so the file re-admits fresh at
        // its real name (the capture composition follows the rename with
        // a settle admission of the new path).
        await this.#repository.removeLocalMapping(localFile.localFileId);
        return null;
      }
      if (this.#hasInFlightEvent(localFile.localFileId)) {
        // The row's create upload is still in flight — its identity has
        // not landed yet, but the upload may still commit server-side, so
        // the row must never be dropped NOR flagged: defer to the caller's
        // bounded settle re-arm (the 2026-09-03 live-defect window).
        return SETTLE_DEFERRED;
      }
      await this.#flagReconcileRequiredOrReport();
      return null;
    }
    // I2: read the target file bytes after the settle and fingerprint
    // them; the frozen fingerprint rides on the operand.
    const targetBytes = await this.#vaultReader.readRegularFileBytes(newPath);
    if (targetBytes === null) {
      await this.#flagReconcileRequiredOrReport();
      return null;
    }
    const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
    // Exact echo suppression (device cursor child 6, task 10): a rename
    // whose prior/target locators, source identity and fingerprint match
    // the durable marker of our own remote apply is consumed here — a
    // mismatch keeps the marker and records the real lifecycle event.
    if (this.#echoSuppressor !== null) {
      const consumed = await this.#echoSuppressor.consumeRenameObservation({
        priorLocator: priorPath,
        targetLocator: newPath,
        sourceId: localFile.sourceId,
        fingerprint: targetFingerprint,
      });
      if (consumed) {
        return null;
      }
    }
    const result = await this.#lifecycle.recordLifecycleEventWithFreeze({
      operands: this.#buildOperands({
        operation,
        sourceId: localFile.sourceId,
        expectedVersionId: localFile.baseVersionId,
        expectedLocator: priorPath,
        targetLocator: newPath,
        tombstoneId: null,
        predecessorEventId: null,
        capturedFingerprintSha256: targetFingerprint.sha256,
        capturedFingerprintSizeBytes: targetFingerprint.sizeBytes,
        capturedFingerprintMediaType: targetFingerprint.mediaType,
      }),
      localFile,
      newPath,
    });
    return {
      operation,
      localFileId: localFile.localFileId,
      eventId: result.eventId,
      predecessorEventId: null,
      capturedFingerprintSha256: targetFingerprint.sha256,
      capturedFingerprintSizeBytes: targetFingerprint.sizeBytes,
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
    readonly capturedFingerprintSha256?: string;
    readonly capturedFingerprintSizeBytes?: number;
    readonly capturedFingerprintMediaType?: string;
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
      capturedFingerprintSha256: input.capturedFingerprintSha256,
      capturedFingerprintSizeBytes: input.capturedFingerprintSizeBytes,
      capturedFingerprintMediaType: input.capturedFingerprintMediaType,
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

  /** Read the closed `lifecycle_state` of one tracked file, or null. */
  #readLifecycleState(localFileId: string): string | null {
    try {
      const row = this.#repository
        .lifecycle.database.readAll(
          `select lifecycle_state from local_files where local_file_id = '${localFileId}';`,
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
    await this.#repository.lifecycle.database.runSerializedMutation(async (session) => {
      const row = session.readRows(
        "select is_reconcile_required from journal_meta where singleton_key = 1;",
      )[0]?.values[0]?.[0];
      if (row === 0) {
        session.exec(
          "update journal_meta set is_reconcile_required = 1 where singleton_key = 1;",
        );
      }
    });
  }

  /** Surface a failed flag write, then reject so callers cannot treat it as settled. */
  async #flagReconcileRequiredOrReport(): Promise<void> {
    await this.#flagReconcileRequired().catch((error: unknown) => {
      this.#failureReporter?.reportJournalFailure("lifecycle_reconcile_persist_failed");
      if (isStoreError(error)) {
        throw error;
      }
      throw journalStoreError("journal_mutation_failed");
    });
  }
}

// The class is exported under its concrete name `LifecycleCaptureImpl`
// to keep the brief's required `LifecycleCapture` port name free for
// the interface. Callers that need the concrete class reach for
// `LifecycleCaptureImpl`; consumers that compose against the port
// import the `LifecycleCapture` interface.

// Re-export to keep the public surface explicit.
export type { LifecycleJournalOperation } from "./lifecycle-contracts";
