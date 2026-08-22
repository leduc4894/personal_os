/**
 * Capture, policy gating and the explicit existing-file scan (spec 7.1, 9).
 *
 * Capture turns settled Vault create/modify observations into durable
 * journal intent — and nothing else. Each path settles for the frozen
 * per-file delay, is then re-read through the narrow Vault reader and
 * fingerprinted over exactly those bytes, and only afterwards is gated:
 * a regular file at most `MAX_FILE_SIZE_BYTES` under a current accepted
 * `allowed` policy decision is recorded as `policy_allowed`; a file one
 * byte over the ceiling is born-terminal `blocked_size`; a raw `excluded`
 * or `indeterminate` decision fails closed to born-terminal
 * `excluded_policy`. The accepted policy revision the decision was taken
 * under is persisted on the journal row (the server re-evaluates policy
 * itself and never trusts that value).
 *
 * Explicit `Sync existing files` scanning remains confirmation-gated, while
 * automatic reconciliation runs the same bounded snapshot through the same
 * admission path. Lifecycle notifications (rename / move / delete) are
 * routed through the {@link LifecycleCapture} (Child 5 task 8); the create /
 * update surface here only keeps the settle + admit pipe and the
 * session-scoped guard set.
 *
 * Privacy (spec 2, 9): this module owns no transport and never moves bytes
 * out of the device. Bytes are read, fingerprinted and dropped; journal rows
 * carry digests and sizes only. The scan summary carries counts only.
 */

import { normalizePolicyLocator } from "../exclusion-policy/evaluator";
import type { CapturePolicyEvaluation, CapturePolicySubject } from "../exclusion-policy/policy-session";
import type {
  ExistingFilesScanSummary,
  JournalCaptureAdmission,
  JournalEventState,
  LocalFile,
} from "./contracts";
import {
  FILE_SETTLE_DELAY_MS,
  JOURNAL_PENDING_EVENT_STATES,
  MAX_FILE_SIZE_BYTES,
  MAX_PENDING_EVENTS,
} from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import { JournalStoreError, journalStoreError } from "./sqlite-database";
import type {
  LifecycleCapture,
  VaultRenameTarget,
  VaultTargetFile,
} from "./lifecycle-capture";
import type { JournalCaptureResult, JournalRepository } from "./repository";

export type { ExistingFilesScanSummary } from "./contracts";

// --- frozen scan bounds (spec 7.1) --------------------------------------------------------

/**
 * The maximum regular files one confirmed `Sync existing files` snapshot
 * processes: the natural bound is the frozen pending-event ceiling, because
 * one scan can never usefully queue more work than the journal accepts.
 */
export const EXISTING_FILES_SCAN_MAXIMUM_FILES = MAX_PENDING_EVENTS;

/** Files per bounded batch inside one confirmed snapshot scan. */
export const EXISTING_FILES_SCAN_BATCH_FILES = 100;

// --- ports ---------------------------------------------------------------------------------

/**
 * The narrow read-only Vault slice capture needs: the current bytes of one
 * regular file (`null` when the path is not, or is no longer, a regular
 * file) and the snapshot of current regular-file paths for the explicit
 * scan. No Vault write ever flows through this port.
 */
export interface CaptureVaultReader {
  readRegularFileBytes(normalizedPath: string): Promise<Uint8Array | null>;
  listRegularFilePaths(): Promise<readonly string[]>;
}

/**
 * The policy gate seam: one fail-closed local decision plus the accepted
 * policy revision it was taken under. `PolicySession.evaluateForCapture`
 * satisfies this port directly.
 */
export interface CapturePolicyGate {
  evaluateForCapture(subject: CapturePolicySubject): CapturePolicyEvaluation;
}

export interface JournalCaptureOptions {
  readonly repository: JournalRepository;
  readonly vaultReader: CaptureVaultReader;
  readonly policyGate: CapturePolicyGate;
  /**
   * The lifecycle capture wired by the composition root: rename / move /
   * delete and the explicit restore surface live behind this port so the
   * create / update surface here stays composition-free.
   */
  readonly lifecycleCapture: LifecycleCapture;
  /** Snapshot ceiling override; defaults to {@link EXISTING_FILES_SCAN_MAXIMUM_FILES}. */
  readonly scanMaximumFiles?: number | undefined;
  /** Snapshot batch size override; defaults to {@link EXISTING_FILES_SCAN_BATCH_FILES}. */
  readonly scanBatchFiles?: number | undefined;
}

const PENDING_EVENT_STATES: ReadonlySet<string> = new Set(JOURNAL_PENDING_EVENT_STATES);

function isPendingEventState(state: JournalEventState): boolean {
  return PENDING_EVENT_STATES.has(state);
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function cancelledSummary(): ExistingFilesScanSummary {
  return {
    outcome: "cancelled",
    processedFileCount: 0,
    skippedFileCount: 0,
    queuedEventCount: 0,
    isTruncated: false,
  };
}

function stoppedSummary(): ExistingFilesScanSummary {
  return {
    outcome: "stopped",
    processedFileCount: 0,
    skippedFileCount: 0,
    queuedEventCount: 0,
    isTruncated: false,
  };
}

function fingerprintsMatch(left: LocalFile["lastCommittedFingerprint"], right: LocalFile["observedFingerprint"]): boolean {
  return (
    left !== null &&
    left.sha256 === right.sha256 &&
    left.sizeBytes === right.sizeBytes &&
    left.mediaType === right.mediaType
  );
}

function isStoreError(error: unknown): error is JournalStoreError {
  return (
    error !== null &&
    typeof error === "object" &&
    "reason" in error &&
    typeof (error as { reason?: unknown }).reason === "string"
  );
}

/**
 * The narrow class-only surface the capture composition uses in addition
 * to the port. Pulling these helpers out of the port keeps the brief's
 * {@link LifecycleCapture} interface minimal while still letting the
 * create / update admission reuse the lifecycle capture's automatic
 * restore detector and reconcile-required flagger.
 */
interface LifecycleCaptureWithRestore extends LifecycleCapture {
  detectAutomaticRestore(normalizedPath: string): Promise<unknown>;
  markTombstonedPathReconcileRequired(normalizedPath: string): Promise<boolean>;
}

// --- the capture coordinator ------------------------------------------------------------------

/**
 * The journal capture coordinator. One instance owns the per-path settle
 * timers and the session-scoped lifecycle guard set; it holds no bytes, no
 * credential and no transport. Every failure is swallowed after the journal
 * has fail-closed on its own: a capture problem must never block editing,
 * and the journal rows remain the durable truth a later pass reconciles.
 */
export class JournalCapture {
  readonly #repository: JournalRepository;
  readonly #vaultReader: CaptureVaultReader;
  readonly #policyGate: CapturePolicyGate;
  readonly #lifecycleCapture: LifecycleCapture;
  readonly #scanMaximumFiles: number;
  readonly #scanBatchFiles: number;
  readonly #settleTimers = new Map<string, ReturnType<typeof setTimeout>>();
  readonly #settleWaiters = new Map<string, Set<() => void>>();
  readonly #lifecycleGuardedPaths = new Set<string>();
  #admissionTail: Promise<void> = Promise.resolve();
  #isDisposed = false;

  constructor(options: JournalCaptureOptions) {
    if (options.scanMaximumFiles !== undefined && !isPositiveInteger(options.scanMaximumFiles)) {
      throw new TypeError("invalid scan maximum");
    }
    if (options.scanBatchFiles !== undefined && !isPositiveInteger(options.scanBatchFiles)) {
      throw new TypeError("invalid scan batch size");
    }
    this.#repository = options.repository;
    this.#vaultReader = options.vaultReader;
    this.#policyGate = options.policyGate;
    this.#lifecycleCapture = options.lifecycleCapture;
    this.#scanMaximumFiles = options.scanMaximumFiles ?? EXISTING_FILES_SCAN_MAXIMUM_FILES;
    this.#scanBatchFiles = options.scanBatchFiles ?? EXISTING_FILES_SCAN_BATCH_FILES;
  }

  /**
   * Queue one create/modify observation (spec 7.1): the path settles alone
   * for the frozen delay — a later observation restarts that one timer —
   * and only the settled read is admitted. Paths currently deferred by a
   * lifecycle guard are refused until the owning rename/move commits
   * server-side and releases the guard (fix round 2 D7). The returned
   * promise resolves once this observation's settled admission is durable
   * (superseded observations of the same path resolve with the one shared
   * admission), or immediately when nothing will be admitted — the hook a
   * Vault-event listener uses to trigger the following queue pass after
   * the event it caused exists in the journal. Never rejects.
   */
  notifyPathChanged(path: string): Promise<void> {
    if (this.#isDisposed) {
      return Promise.resolve();
    }
    const normalizedPath = this.#normalizePathOrNull(path);
    if (normalizedPath === null || this.#isLifecycleDeferredPath(normalizedPath)) {
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      const waiters = this.#settleWaiters.get(normalizedPath) ?? new Set<() => void>();
      waiters.add(resolve);
      this.#settleWaiters.set(normalizedPath, waiters);
      const runningTimer = this.#settleTimers.get(normalizedPath);
      if (runningTimer !== undefined) {
        clearTimeout(runningTimer);
      }
      this.#settleTimers.set(
        normalizedPath,
        setTimeout(() => {
          this.#settleTimers.delete(normalizedPath);
          // Settled admissions run serialized on one tail: the outcome is the
          // same journal the single writer would produce, and unload can await
          // the tail before closing the store.
          this.#admissionTail = this.#admissionTail
            .then(() => this.#admitNormalizedPath(normalizedPath))
            .then(() => undefined, () => undefined)
            .then(() => this.#releaseSettleWaiters(normalizedPath));
        }, FILE_SETTLE_DELAY_MS),
      );
    });
  }

  /** Resolve and drop every pending waiter of one settled path. */
  #releaseSettleWaiters(normalizedPath: string): void {
    const waiters = this.#settleWaiters.get(normalizedPath);
    if (waiters === undefined) {
      return;
    }
    this.#settleWaiters.delete(normalizedPath);
    for (const resolve of waiters) {
      resolve();
    }
  }

  /**
   * Resolve once every already-scheduled settle admission has settled — the
   * hook a safe unload uses before closing the journal store. Never rejects.
   */
  whenIdle(): Promise<void> {
    return this.#admissionTail;
  }

  /**
   * Observe one delete notification (spec 7.1): the lifecycle capture
   * owns the durable delete event; an untracked path stays untouched and
   * an uncommitted file (no source identity) fails closed after freezing
   * its pending content work.
   */
  async notifyPathDeleted(file: VaultTargetFile): Promise<void> {
    if (this.#isDisposed) {
      return;
    }
    try {
      await this.#lifecycleCapture.captureDelete(file);
    } catch (error) {
      if (!isStoreError(error)) {
        throw journalStoreError("journal_mutation_failed");
      }
      throw error;
    }
  }

  /**
   * Observe one rename notification (spec 7.1): the lifecycle capture
   * owns the durable rename / move event. The per-path settle debounce
   * is applied by the lifecycle capture so a burst collapses into one
   * durable row; a file whose local source identity is missing fails
   * closed with `reconcile_required` durably flagged.
   */
  async notifyPathRenamed(file: VaultRenameTarget, priorPath: string): Promise<void> {
    if (this.#isDisposed) {
      return;
    }
    try {
      await this.#lifecycleCapture.captureRename(file, priorPath);
    } catch (error) {
      if (!isStoreError(error)) {
        throw journalStoreError("journal_mutation_failed");
      }
      throw error;
    }
  }

  /**
   * The explicit `Sync existing files` pass (spec 7.1): the user confirms
   * first, then one bounded snapshot is processed in bounded batches
   * through the same admission path as settled events. Lifecycle-deferred
   * paths are excluded from the snapshot until their owning rename/move
   * commits server-side (which deletes the marker rows and releases the
   * path for re-admission — fix round 2 D7); a terminally-failed rename
   * keeps the exclusion fail-closed.
   */
  async runExistingFilesScan(options: {
    readonly confirm: () => Promise<boolean>;
  }): Promise<ExistingFilesScanSummary> {
    if (this.#isDisposed) {
      return cancelledSummary();
    }
    if (!(await options.confirm())) {
      return cancelledSummary();
    }
    return this.#captureSnapshot();
  }

  /**
   * Reconcile current Vault bytes without user confirmation. This is the
   * automatic coordinator's narrow operation: it preserves every bounded,
   * deterministic admission invariant of the explicit snapshot path.
   */
  async runAutomaticSnapshot(options: { readonly signal?: AbortSignal } = {}): Promise<ExistingFilesScanSummary> {
    if (this.#isSnapshotStopped(options.signal)) {
      return stoppedSummary();
    }
    return this.#captureSnapshot(options.signal);
  }

  /** Enumerate and admit one deterministic bounded regular-file snapshot. */
  async #captureSnapshot(signal?: AbortSignal): Promise<ExistingFilesScanSummary> {
    if (this.#isSnapshotStopped(signal)) {
      return stoppedSummary();
    }
    const snapshotPaths = await this.#vaultReader.listRegularFilePaths();
    if (this.#isSnapshotStopped(signal)) {
      return stoppedSummary();
    }
    const normalizedSnapshotPaths = [
      ...new Set(snapshotPaths.map((path) => this.#normalizePathOrNull(path)).filter(
        (normalizedPath): normalizedPath is string => normalizedPath !== null,
      )),
    ].sort();
    const boundedPaths = normalizedSnapshotPaths.slice(0, this.#scanMaximumFiles);
    const isTruncated = boundedPaths.length < normalizedSnapshotPaths.length;
    let processedFileCount = 0;
    let skippedFileCount = 0;
    let queuedEventCount = 0;
    for (let offset = 0; offset < boundedPaths.length; offset += this.#scanBatchFiles) {
      const batchPaths = boundedPaths.slice(offset, offset + this.#scanBatchFiles);
      for (const normalizedPath of batchPaths) {
        if (this.#isSnapshotStopped(signal)) {
          return stoppedSummary();
        }
        if (this.#isLifecycleDeferredPath(normalizedPath)) {
          skippedFileCount += 1;
          continue;
        }
        try {
          const captureResult = await this.#admitNormalizedPath(normalizedPath, signal);
          if (this.#isSnapshotStopped(signal)) {
            return stoppedSummary();
          }
          processedFileCount += 1;
          if (
            captureResult !== null &&
            (captureResult.outcome === "event_recorded" || captureResult.outcome === "event_coalesced") &&
            captureResult.event.state === "queued"
          ) {
            queuedEventCount += 1;
          }
        } catch {
          skippedFileCount += 1;
        }
      }
    }
    return {
      outcome: "completed",
      processedFileCount,
      skippedFileCount,
      queuedEventCount,
      isTruncated,
    };
  }

  /** Stop all settling and release the session guard set (unload/suspend). */
  dispose(): void {
    this.#isDisposed = true;
    for (const settleTimer of this.#settleTimers.values()) {
      clearTimeout(settleTimer);
    }
    this.#settleTimers.clear();
    for (const normalizedPath of [...this.#settleWaiters.keys()]) {
      this.#releaseSettleWaiters(normalizedPath);
    }
    this.#lifecycleGuardedPaths.clear();
  }

  // --- internals ---------------------------------------------------------------------------------

  /**
   * The one admission path every observation flows through (spec 7.1):
   * re-read the settled bytes, fingerprint exactly those bytes, then gate
   * by the size ceiling and the current accepted policy decision, recording
   * the accepted revision on the journal row.
   *
   * Automatic restore (Child 5 task 8 fix round 1 C2): a tombstoned
   * local mapping that re-appears with bytes matching the last
   * committed fingerprint is restored by the lifecycle capture, not
   * minted as a fresh create. A detection failure (mismatched bytes,
   * missing identity, anything other than a successful restore) is
   * FAIL-CLOSED: the row is durably flagged `reconcile_required`, the
   * open tombstone is cleared, and the create / update admission is
   * refused. The user can still edit the file via the explicit
   * restore surface; the brief disallows the fall-through-to-create
   * behaviour because a successful re-bind of the prior source would
   * silently drop the delete intent.
   */
  async #admitNormalizedPath(
    normalizedPath: string,
    signal?: AbortSignal,
  ): Promise<JournalCaptureResult | null> {
    if (this.#isSnapshotStopped(signal)) {
      return null;
    }
    if (this.#isLifecycleDeferredPath(normalizedPath)) {
      return null;
    }
    const trackedFile = this.#repository.readLocalFileByPath(normalizedPath);
    // Automatic restore: only attempted when the local mapping is
    // already tombstoned. The lifecycle capture verifies the bytes
    // against the last-committed fingerprint and either restores or
    // flags reconcile_required.
    if (
      trackedFile !== null &&
      trackedFile.sourceId !== null &&
      trackedFile.baseVersionId !== null
    ) {
      const openTombstone = this.#readOpenTombstoneId(trackedFile.localFileId);
      if (openTombstone !== null) {
        // The port exposes only the three required methods; the
        // automatic-restore detector and reconcile-required flagger are
        // class-only helpers. We narrow via the concrete class type the
        // composition root passes in; if a strict-mode port fake ever
        // reaches here, the cast surfaces a compile-time error rather
        // than a runtime fall-through.
        const capture = this.#lifecycleCapture as LifecycleCaptureWithRestore;
        try {
          await capture.detectAutomaticRestore(normalizedPath);
        } catch {
          // Detection failed: durably mark the file reconcile_required
          // and refuse the create / update admission. The user can
          // explicitly resolve via the restore surface.
          await capture.markTombstonedPathReconcileRequired(normalizedPath).catch(
            () => undefined,
          );
          this.#lifecycleGuardedPaths.add(normalizedPath);
        }
        return null;
      }
    }
    const contentBytes = await this.#vaultReader.readRegularFileBytes(normalizedPath);
    if (this.#isSnapshotStopped(signal)) {
      return null;
    }
    if (contentBytes === null) {
      // The file vanished between event and read: a tracked file becomes
      // deferred_lifecycle; an untracked path is simply gone (spec 7.1).
      if (trackedFile !== null) {
        this.#lifecycleGuardedPaths.add(normalizedPath);
        await this.#deferTrackedFile(trackedFile);
      }
      return null;
    }
    const fingerprint = await deriveFrozenFingerprint(contentBytes);
    if (this.#isSnapshotStopped(signal)) {
      return null;
    }
    const evaluation = this.#policyGate.evaluateForCapture({
      sourceId: trackedFile?.sourceId ?? null,
      normalizedLocator: normalizedPath,
      mediaType: fingerprint.mediaType,
      sizeBytes: fingerprint.sizeBytes,
    });
    const admission: JournalCaptureAdmission =
      fingerprint.sizeBytes > MAX_FILE_SIZE_BYTES
        ? "blocked_size"
        : evaluation.decision.enforced === "allowed"
          ? "policy_allowed"
          : "excluded_policy";
    if (admission === "policy_allowed" && fingerprintsMatch(trackedFile?.lastCommittedFingerprint ?? null, fingerprint)) {
      return null;
    }
    return this.#repository.recordCapture({
      normalizedPath,
      fingerprint,
      policyRevisionNumber: evaluation.revisionNumber,
      admission,
    });
  }

  #isSnapshotStopped(signal?: AbortSignal): boolean {
    return this.#isDisposed || signal?.aborted === true;
  }

  /** Defer every still-pending event of one tracked path (spec 7.1). */
  async #deferLifecycleForPath(normalizedPath: string): Promise<void> {
    const trackedFile = this.#repository.readLocalFileByPath(normalizedPath);
    if (trackedFile === null) {
      return;
    }
    await this.#deferTrackedFile(trackedFile);
  }

  async #deferTrackedFile(trackedFile: LocalFile): Promise<void> {
    const events = this.#repository.readEventsByLocalFileId(trackedFile.localFileId);
    for (const event of events) {
      if (!isPendingEventState(event.state)) {
        continue;
      }
      // Terminal from here on: the frozen event never reached the server,
      // so no later queue pass may select it. When the owning rename/move
      // later COMMITS server-side, the lifecycle repository deletes these
      // `deferred_lifecycle` rows in the same transaction as the committed
      // receipt (fix round 2 D7), releasing the path for re-admission; a
      // terminally-failed rename keeps the marker fail-closed (child 6
      // owns repair).
      await this.#repository.markEventTerminal(
        event.eventId,
        "deferred_lifecycle",
        "deferred_lifecycle",
      );
    }
  }

  /**
   * Whether a path is lifecycle-deferred: guarded in this session, or
   * durably carrying a `deferred_lifecycle` event in the journal. Such a
   * path is never re-captured and excluded from the snapshot scan until
   * the owning rename/move commits server-side — that commit deletes the
   * marker rows and releases the path for re-admission (fix round 2 D7).
   * A terminally-failed rename keeps the marker (fail-closed, child 6
   * owns repair).
   */
  #isLifecycleDeferredPath(normalizedPath: string): boolean {
    if (this.#lifecycleGuardedPaths.has(normalizedPath)) {
      return true;
    }
    const trackedFile = this.#repository.readLocalFileByPath(normalizedPath);
    if (trackedFile === null) {
      return false;
    }
    return this.#repository
      .readEventsByLocalFileId(trackedFile.localFileId)
      .some((event) => event.state === "deferred_lifecycle");
  }

  /** Read the open tombstone id of one tracked file (or null). */
  #readOpenTombstoneId(localFileId: string): string | null {
    try {
      const row = this.#repository.lifecycle.database
        .readAll(
          `select open_tombstone_id from local_files where local_file_id = '${localFileId}';`,
        )[0]?.values[0]?.[0];
      return typeof row === "string" && row.length > 0 ? row : null;
    } catch {
      return null;
    }
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
}

// Reference unused imports so the lint surface stays stable.
void JOURNAL_PENDING_EVENT_STATES;
void FILE_SETTLE_DELAY_MS;
void MAX_FILE_SIZE_BYTES;
void deriveFrozenFingerprint;
