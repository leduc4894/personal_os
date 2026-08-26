/**
 * The single foreground sync coordinator (device cursor and manifest
 * reconciliation, task 12, spec 11, 12).
 *
 * ONE coordinator owns every mutating foreground network phase of the
 * device-sync stack: crash recovery of an unfinished apply, the
 * repair-if-required manifest reconciliation, the eligible outbound drain,
 * ONE inbound pull page, the local cursor acknowledgement and at most one
 * follow-up request. Because the drain loop is strictly sequential, no two
 * mutating phases of two cycles can ever overlap — the phase probe of the
 * tests pins `maximumConcurrentMutations === 1`.
 *
 * Cadence (fake-clock pinned): the foreground pull tick runs every 30
 * seconds while the plugin is active; a reconciliation is requested after
 * six accumulated foreground-ACTIVE hours (suspended time accumulates
 * nothing because frozen timers never tick); a retryable failure moves the
 * schedule onto a cancellable jittered exponential backoff between one
 * second and five minutes (the queue lane's frozen schedule) and pauses
 * the pull tick until the first success; `stop()` cancels every timer and
 * awaits the running cycle; and a suspension of one hour or more expires
 * an active manifest run before the resume cycle runs it (the server-side
 * one-hour expiry remains the authority when no discard seam is wired).
 *
 * Self-origin suppression is evidence-based ONLY: the origin device id
 * alone never suppresses an event. The exact event/source/version/
 * fingerprint evidence — the tracked outbound row committed at the same
 * source, the same version and the same final fingerprint — closes the
 * matching outbound row with a `self_origin_no_op` terminal outcome BEFORE
 * the cursor advances; every weaker match walks the full crash-safe apply
 * machine instead. A lost acknowledgement stays owed: the debt is retried
 * BEFORE the next pull of the same cycle chain.
 *
 * The coordinator catches only typed failures: every phase reports its own
 * closed observation through the Task 7 diagnostics facade, and the
 * repository calls and state reads the coordinator itself owns report
 * their exact stage — the acknowledgement path (the owed-debt state read
 * and the acknowledgement record) reports `acknowledge`, the self-origin
 * settle reports `local_commit`, and every other bookkeeping state read
 * reports `pull`. Failures schedule a retry or settle into the readable
 * repair state of the status projection — never a silent stop.
 *
 * Like the other device-sync modules this file imports no Node.js,
 * Electron, Obsidian or `obsidian` API at module load time, so it stays
 * loadable on mobile. The composition root injects `Date.now` and the real
 * one-shot timers; tests inject a fake clock and scheduler.
 */

import type { FrozenFingerprint } from "../journal/contracts";
import type { JournalStoreErrorReason } from "../journal/sqlite-database";
import { computeRetryBackoffMs } from "../journal/queue-driver";
import { DeviceSyncApiError, classifyDeviceSyncFailure } from "./api";
import type {
  CursorAcknowledgementInput,
  DeviceCursorReceipt,
  DeviceEventPage,
  DeviceSyncEvent,
} from "./api";
import type {
  CursorFailureStage,
  DeviceSyncDiagnostics,
  DeviceSyncReason,
  DeviceSyncRepository,
  DeviceSyncState,
} from "./contracts";
import type { ManifestReconciler, ReconcileReason } from "./manifest-reconciler";
import type { RemoteEventApplier } from "./remote-event-applier";
import { projectDeviceSyncStatus } from "./status";
import type { DeviceSyncManifestActionInput, DeviceSyncStatus } from "./status";

// --- the pinned interface (brief task 12) --------------------------------------------------------------

/** The closed trigger vocabulary of the coordinator, exactly six members. */
export type SyncTrigger =
  | "startup"
  | "resume"
  | "local_commit"
  | "pull_interval"
  | "periodic_reconcile"
  | "explicit_repair";

/** The composed coordinator surface. */
export interface SyncCoordinator {
  request(trigger: SyncTrigger): void;
  stop(): Promise<void>;
  readStatus(): DeviceSyncStatus;
}

// --- the frozen cadence bounds -------------------------------------------------------------------------

/** The foreground pull tick: every 30 seconds while the plugin is active. */
export const DEVICE_SYNC_PULL_INTERVAL_MS = 30_000;

/** A reconciliation is requested after six accumulated foreground-active hours. */
export const DEVICE_SYNC_RECONCILE_ACCUMULATED_ACTIVE_MS = 6 * 60 * 60 * 1_000;

/** An active manifest run expires after a suspension of one hour or more. */
export const DEVICE_SYNC_MANIFEST_EXPIRY_AFTER_SUSPEND_MS = 60 * 60 * 1_000;

// --- the injected seams --------------------------------------------------------------------------------

/** The two wire operations the coordinator itself drives (the Task 9 client subset). */
export interface SyncCoordinatorWireApi {
  pullEvents(): Promise<DeviceEventPage>;
  acknowledgeCursor(input: CursorAcknowledgementInput): Promise<DeviceCursorReceipt>;
}

/**
 * The bounded outbound drain: the composition passes the SAME coalescing
 * queue-pass dispatcher every other foreground trigger uses, so the
 * coordinator never bypasses the journal driver's one-active-request
 * contract.
 */
export interface SyncCoordinatorOutboundLane {
  request(): Promise<void>;
}

/**
 * The one-shot timer seam: a call signature. `schedule` returns the
 * canceller of exactly one future firing; the composition passes real
 * `setTimeout`/`clearTimeout`, the tests a manual clock. No repeating
 * timer primitive exists on the seam — the cadence re-arms itself after
 * every tick.
 */
export interface SyncScheduler {
  (delayMs: number, callback: () => void): () => void;
}

/** Bind the seam to the host's real one-shot timers. */
export function createRealSyncScheduler(): SyncScheduler {
  return (delayMs: number, callback: () => void): (() => void) => {
    const handle = setTimeout(callback, delayMs);
    return () => {
      clearTimeout(handle);
    };
  };
}

/**
 * The tracked outbound row one pulled event is matched against for the
 * exact self-origin evidence (the journal's `local_files` projection:
 * committed source, version and final fingerprint, no path).
 */
export interface CommittedOutboundRow {
  readonly sourceId: string | null;
  readonly baseVersionId: string | null;
  readonly lastCommittedFingerprint: FrozenFingerprint | null;
}

/** The evidence reader seam (structural subset of the journal repository). */
export interface OutboundEvidenceReader {
  readCommittedOutboundRowByLocator(normalizedLocator: string): CommittedOutboundRow | null;
}

export interface SyncCoordinatorOptions {
  readonly repository: DeviceSyncRepository;
  readonly api: SyncCoordinatorWireApi;
  readonly applier: RemoteEventApplier;
  readonly reconciler: ManifestReconciler;
  readonly outbound: SyncCoordinatorOutboundLane;
  readonly diagnostics: DeviceSyncDiagnostics;
  /** Clock for cadence, backoff and suspension measurement; no default. */
  readonly nowEpochMs: () => number;
  /** One-shot timer seam; defaults to the host's real timers. */
  readonly scheduler?: SyncScheduler | undefined;
  /** Random source for the bounded retry jitter; defaults to `Math.random`. */
  readonly randomJitter?: (() => number) | undefined;
  /** The journal's sticky `reconcile_required` flag; defaults to never. */
  readonly isJournalReconcileRequired?: (() => boolean) | undefined;
  /** The active manifest run's action progress rows; defaults to none. */
  readonly readManifestActionProgress?: (() => readonly DeviceSyncManifestActionInput[]) | undefined;
  /**
   * The stable device identity of this plugin instance. A null (or
   * non-matching) origin device id never suppresses an event — only the
   * exact committed evidence can.
   */
  readonly resolveOwnDeviceId?: (() => string | null) | undefined;
  /** The outbound evidence reader of the exact self-origin proof. */
  readonly outboundEvidence?: OutboundEvidenceReader | undefined;
  /**
   * Discards an active manifest run's temporary progress after a
   * suspension of one hour or more (the reconciler journal port's
   * `discardActiveManifestRun`). Optional: without it the resume relies on
   * the server-side one-hour expiry restart inside the reconciler.
   */
  readonly discardExpiredManifestRun?: (() => Promise<void>) | undefined;
}

// --- the closed failure plumbing ------------------------------------------------------------------------

/** The closed store reason of one repository throw, when it carries one. */
function storeReasonOf(error: unknown): JournalStoreErrorReason | null {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = (error as { reason?: unknown }).reason;
    if (typeof reason === "string") {
      return reason as JournalStoreErrorReason;
    }
  }
  return null;
}

/** One classified cycle failure: a closed reason and whether a backoff retry is owed. */
interface ClassifiedFailure {
  readonly reason: DeviceSyncReason;
  readonly retryable: boolean;
}

function classifyCycleFailure(error: unknown): ClassifiedFailure {
  if (error instanceof DeviceSyncApiError) {
    return { reason: error.reason, retryable: error.retryable };
  }
  const failure = classifyDeviceSyncFailure(error);
  return { reason: failure.reason, retryable: failure.retryable };
}

// --- the coordinator ------------------------------------------------------------------------------------

/**
 * Build the single foreground sync coordinator. The returned surface is
 * fire-and-forget: `request` coalesces triggers into the sequential drain
 * and never awaits a cycle, `stop` cancels every timer and resolves once
 * the running cycle settled, and `readStatus` projects the closed status
 * synchronously (a failing repository read propagates to the caller, which
 * owns the composition-read failure surfacing).
 */
export function createSyncCoordinator(options: SyncCoordinatorOptions): SyncCoordinator {
  const {
    repository,
    api,
    applier,
    reconciler,
    outbound,
    diagnostics,
    nowEpochMs,
  } = options;
  const scheduler = options.scheduler ?? createRealSyncScheduler();
  const randomJitter = options.randomJitter ?? Math.random;

  // --- the coordinator state (all mutation happens inside the drain) ----

  let isStopped = false;
  let drainPromise: Promise<void> | null = null;
  let hasPendingCycle = false;
  let hasPendingExplicitRepair = false;
  let hasPendingPeriodicReconcile = false;
  let hasFollowUpCycle = false;
  let isRepairRunning = false;
  let blockedRepairReason: DeviceSyncReason | null = null;
  let failureAttemptCount = 0;
  let cadenceCanceller: (() => void) | null = null;
  /** The scheduled fire time of the outstanding (or last) cadence tick. */
  let cadenceNextEpochMs: number | null = null;
  let retryCanceller: (() => void) | null = null;
  let accumulatedActiveMs = 0;
  let lastActivityEpochMs: number | null = null;
  let hasExpiredSuspension = false;

  // --- the cadence ---------------------------------------------------------------------------------

  function cancelCadenceTimer(): void {
    if (cadenceCanceller !== null) {
      cadenceCanceller();
      cadenceCanceller = null;
    }
    // A cancelled cadence re-anchors at the present when it re-arms.
    cadenceNextEpochMs = null;
  }

  function cancelRetryTimer(): void {
    if (retryCanceller !== null) {
      retryCanceller();
      retryCanceller = null;
    }
  }

  /**
   * Arm the foreground pull tick if it is not already armed and no retry
   * backoff owns the schedule — an armed backoff (a retryable failure)
   * PAUSED the cadence, and a mid-outage trigger (a local commit) must
   * not silently resume it; the first successful cycle re-arms the tick
   * anchored at the present. The tick anchors at its own scheduled time —
   * after a firing the next tick sits at `last tick + 30 s`, so a clock
   * that moved ahead (a fast test clock, a late foreground callback)
   * catches up instead of drifting.
   */
  function armCadenceTimer(): void {
    if (isStopped || cadenceCanceller !== null || retryCanceller !== null) {
      return;
    }
    const nowEpoch = nowEpochMs();
    const fireAtEpochMs =
      cadenceNextEpochMs === null
        ? nowEpoch + DEVICE_SYNC_PULL_INTERVAL_MS
        : cadenceNextEpochMs + DEVICE_SYNC_PULL_INTERVAL_MS;
    cadenceNextEpochMs = fireAtEpochMs;
    cadenceCanceller = scheduler(Math.max(0, fireAtEpochMs - nowEpoch), () => {
      cadenceCanceller = null;
      accumulatedActiveMs += DEVICE_SYNC_PULL_INTERVAL_MS;
      if (accumulatedActiveMs >= DEVICE_SYNC_RECONCILE_ACCUMULATED_ACTIVE_MS) {
        // Six accumulated foreground-active hours: the next repair
        // decision gets the periodic reconciliation opportunity.
        accumulatedActiveMs = 0;
        hasPendingPeriodicReconcile = true;
      }
      requestSync("pull_interval");
      armCadenceTimer();
    });
  }

  /**
   * Schedule the cancellable jittered exponential retry (1 s .. 5 min, the
   * queue lane's frozen schedule) and pause the pull tick: while the
   * backoff owns the retry schedule, the cadence must not multiply the
   * attempts.
   */
  function scheduleRetryBackoff(): void {
    failureAttemptCount += 1;
    cancelRetryTimer();
    cancelCadenceTimer();
    const delayMs = computeRetryBackoffMs(failureAttemptCount, randomJitter);
    retryCanceller = scheduler(delayMs, () => {
      retryCanceller = null;
      requestSync("pull_interval");
    });
  }

  // --- the cycle -----------------------------------------------------------------------------------

  /**
   * Read the durable state for one phase of the cycle. A failing read
   * reports its closed observation at the cursor stage OF THE PHASE THAT
   * READ IT: the acknowledgement path reports `acknowledge`, every other
   * bookkeeping read (repair gating, drain eligibility, event settling)
   * reports `pull` — the cursor bookkeeping those reads serve.
   */
  function readState(stage: CursorFailureStage): DeviceSyncState {
    try {
      return repository.readState();
    } catch (error) {
      const reason = storeReasonOf(error) ?? "server_error";
      diagnostics.cursorFailure(stage, reason);
      throw new DeviceSyncApiError(reason, false);
    }
  }

  /**
   * Acknowledge the owed cursor: every locally applied sequence the server
   * has not yet acknowledged back. A lost acknowledgement stays owed — the
   * debt is retried here BEFORE the next pull.
   */
  async function acknowledgeOwedCursor(): Promise<void> {
    const state = readState("acknowledge");
    if (state.appliedSequence <= state.acknowledgedSequence) {
      return;
    }
    const receipt = await api.acknowledgeCursor({
      expectedPreviousSequence: state.acknowledgedSequence,
      appliedThroughSequence: state.appliedSequence,
    });
    // The server's durable cursor — never ahead of what this device
    // applied (the debt invariant) — becomes the local acknowledgement.
    const acknowledged = Math.min(receipt.acknowledgedSequence, state.appliedSequence);
    if (acknowledged <= state.acknowledgedSequence) {
      return;
    }
    try {
      await repository.recordServerAcknowledgement(acknowledged);
    } catch (error) {
      const reason = storeReasonOf(error) ?? "server_error";
      diagnostics.cursorFailure("acknowledge", reason);
      throw new DeviceSyncApiError(reason, false);
    }
  }

  /** Whether the exact committed-outbound evidence proves one event a self-origin no-op. */
  function isProvenSelfOriginNoOp(event: DeviceSyncEvent): boolean {
    const resolveOwnDeviceId = options.resolveOwnDeviceId;
    const evidenceReader = options.outboundEvidence;
    if (resolveOwnDeviceId === undefined || evidenceReader === undefined) {
      return false;
    }
    const ownDeviceId = resolveOwnDeviceId();
    if (
      ownDeviceId === null ||
      ownDeviceId.length === 0 ||
      event.originDeviceId === null ||
      event.originDeviceId !== ownDeviceId
    ) {
      // The origin device id alone never suppresses — and a foreign origin
      // never closes a local row either.
      return false;
    }
    // The evidence locator is the wire's own echo target: every content
    // operation (create, update, restore — and a rename/move's new path)
    // carries it as the resulting locator, while a delete carries only the
    // prior locator it removed. An update never carries a prior locator
    // (it changes no path), so keying its branch there would leave the
    // dominant own-upload echo unsuppressed.
    const normalizedLocator =
      event.operation === "deleted" ? event.priorLocator : event.resultingLocator;
    if (normalizedLocator === null || event.currentVersionId === null || event.currentFingerprint === null) {
      return false;
    }
    const row = evidenceReader.readCommittedOutboundRowByLocator(normalizedLocator);
    if (row === null || row.sourceId !== event.sourceId || row.baseVersionId !== event.currentVersionId) {
      return false;
    }
    const committed = row.lastCommittedFingerprint;
    return (
      committed !== null &&
      committed.sha256 === event.currentFingerprint.sha256 &&
      committed.sizeBytes === event.currentFingerprint.sizeBytes &&
      committed.mediaType === event.currentFingerprint.mediaType
    );
  }

  /**
   * Close the matching outbound row and advance the cursor over the
   * proven self-origin no-op — one serialized terminal event, no Vault
   * mutation.
   */
  async function terminalizeSelfOriginNoOp(event: DeviceSyncEvent): Promise<void> {
    try {
      await repository.terminalizeEvent({
        eventSequence: event.eventSequence,
        outcome: "self_origin_no_op",
        reason: null,
      });
    } catch (error) {
      const reason = storeReasonOf(error) ?? "server_error";
      diagnostics.applyFailure("local_commit", reason);
      throw new DeviceSyncApiError(reason, false);
    }
  }

  /**
   * Run the repair-if-required phase. The explicit and periodic trigger
   * facts are consumed HERE — at the repair decision — so an opportunity
   * that lands while an earlier cycle is still running applies to the
   * first repair decision made after it. A blocked verdict settles into
   * the readable repair state and never auto-retries — only an explicit
   * repair request (or the barrier clearing elsewhere) retries it.
   */
  async function runRepairIfRequired(): Promise<"none" | "settled" | "retry"> {
    const state = readState("pull");
    const isRepairOwed =
      state.barrierGeneration !== null ||
      state.activeManifestRunId !== null ||
      (options.isJournalReconcileRequired?.() ?? false);
    if (!hasPendingExplicitRepair && (!isRepairOwed || blockedRepairReason !== null)) {
      // The coalesced opportunity is consumed even when no repair runs:
      // nothing owed means the periodic check just passed.
      hasPendingPeriodicReconcile = false;
      return "none";
    }
    const isExplicitRepair = hasPendingExplicitRepair;
    const isPeriodicReconcile = hasPendingPeriodicReconcile;
    hasPendingExplicitRepair = false;
    hasPendingPeriodicReconcile = false;
    // The reason mapping: an explicit repair names itself; the periodic
    // opportunity names itself (adopting an existing barrier); the
    // journal's reconcile flag without a barrier reconciles the local
    // invariant; an interrupted run or apply-blocker barrier resumes.
    const reconcileReason: ReconcileReason | null = isExplicitRepair
      ? "explicit_repair"
      : isPeriodicReconcile
        ? "periodic"
        : state.barrierGeneration === null && state.activeManifestRunId === null
          ? "local_invariant"
          : null;
    isRepairRunning = true;
    let outcome;
    try {
      outcome =
        reconcileReason === null
          ? await reconciler.resume()
          : await reconciler.reconcile(reconcileReason);
    } finally {
      isRepairRunning = false;
    }
    if (outcome.kind === "retry") {
      return "retry";
    }
    if (outcome.kind === "blocked") {
      blockedRepairReason = outcome.reason;
      return "settled";
    }
    blockedRepairReason = null;
    return "settled";
  }

  /**
   * One bounded cycle, strictly sequential: expiry discard, recovery,
   * repair-if-required, the eligible outbound drain, the owed
   * acknowledgement, ONE inbound page, the local acknowledgement and at
   * most one armed follow-up (a follow-up cycle never arms another).
   */
  async function runCycle(trigger: { readonly isFollowUp: boolean }): Promise<void> {
    lastActivityEpochMs = nowEpochMs();
    let shouldArmFollowUp = false;
    try {
      if (hasExpiredSuspension) {
        // A suspension of one hour or more expired any active manifest
        // run; its temporary progress is discarded before the resume so
        // the reconciler starts a fresh checkpoint-bound run under the
        // SAME barrier.
        hasExpiredSuspension = false;
        const suspendedState = readState("pull");
        if (
          suspendedState.activeManifestRunId !== null &&
          options.discardExpiredManifestRun !== undefined
        ) {
          await options.discardExpiredManifestRun();
        }
      }
      // Phase: recovery — settle any crash-interrupted apply first.
      await applier.recoverUnfinishedApply();
      // Phase: repair-if-required.
      const repairOutcome = await runRepairIfRequired();
      if (repairOutcome === "retry") {
        scheduleRetryBackoff();
        return;
      }
      // Phase: eligible outbound drain — dispatchable only outside an
      // active repair (a surviving barrier keeps every outbound row
      // frozen; the reconciliation planner owns their uploads).
      const postRepairState = readState("pull");
      if (
        postRepairState.barrierGeneration === null &&
        postRepairState.activeManifestRunId === null
      ) {
        await outbound.request();
      }
      // Phase: the owed acknowledgement — a lost acknowledgement is
      // retried BEFORE another pull.
      await acknowledgeOwedCursor();
      // Phase: ONE inbound page.
      const page = await api.pullEvents();
      for (const event of page.events) {
        const state = readState("pull");
        if (event.eventSequence <= state.appliedSequence) {
          // An already-settled redelivery is an idempotent skip.
          continue;
        }
        if (isProvenSelfOriginNoOp(event)) {
          await terminalizeSelfOriginNoOp(event);
        } else {
          await applier.apply(event);
        }
      }
      // Phase: the local acknowledgement of everything this cycle applied.
      await acknowledgeOwedCursor();
      shouldArmFollowUp = page.hasMore && !trigger.isFollowUp;
    } catch (error) {
      // Only typed failures reach here: every phase already reported its
      // own closed observation through the diagnostics facade. A
      // retryable failure moves the schedule onto the backoff; a
      // non-retryable one (a missing credential, a closed store) leaves
      // the cadence in charge of the next attempt.
      if (classifyCycleFailure(error).retryable) {
        scheduleRetryBackoff();
      }
      return;
    } finally {
      lastActivityEpochMs = nowEpochMs();
    }
    // The cycle succeeded: reset the backoff and resume the pull tick.
    failureAttemptCount = 0;
    cancelRetryTimer();
    armCadenceTimer();
    if (shouldArmFollowUp) {
      hasFollowUpCycle = true;
    }
  }

  // --- the sequential drain ------------------------------------------------------------------------

  function startDrain(): void {
    if (drainPromise !== null || isStopped) {
      return;
    }
    const runningDrain = drain().catch(() => undefined);
    drainPromise = runningDrain;
    void runningDrain.finally(() => {
      if (drainPromise === runningDrain) {
        drainPromise = null;
      }
    });
  }

  async function drain(): Promise<void> {
    while (!isStopped && (hasPendingCycle || hasFollowUpCycle)) {
      const isFollowUp = hasFollowUpCycle && !hasPendingCycle;
      hasFollowUpCycle = false;
      hasPendingCycle = false;
      // The explicit/periodic trigger facts stay pending until the
      // repair decision inside the cycle consumes them — an opportunity
      // landing mid-cycle applies to that cycle's repair.
      await runCycle({ isFollowUp });
    }
  }

  // --- the public surface --------------------------------------------------------------------------

  function requestSync(trigger: SyncTrigger): void {
    if (isStopped) {
      return;
    }
    const nowEpoch = nowEpochMs();
    // Suspension detection: an idle gap of one hour or more (no cycle and
    // no cadence tick — the frozen timers of a suspended session) expires
    // any active manifest run before this trigger's cycle runs.
    if (
      lastActivityEpochMs !== null &&
      nowEpoch - lastActivityEpochMs >= DEVICE_SYNC_MANIFEST_EXPIRY_AFTER_SUSPEND_MS
    ) {
      hasExpiredSuspension = true;
    }
    if (trigger === "explicit_repair") {
      // The one user-owned retry of a blocked repair.
      blockedRepairReason = null;
      hasPendingExplicitRepair = true;
    }
    if (trigger === "periodic_reconcile") {
      hasPendingPeriodicReconcile = true;
    }
    hasPendingCycle = true;
    armCadenceTimer();
    startDrain();
  }

  return {
    request: requestSync,

    stop(): Promise<void> {
      isStopped = true;
      cancelCadenceTimer();
      cancelRetryTimer();
      return drainPromise ?? Promise.resolve();
    },

    readStatus(): DeviceSyncStatus {
      return projectDeviceSyncStatus({
        state: repository.readState(),
        isRepairRunning,
        blockedRepairReason,
        isJournalReconcileRequired: options.isJournalReconcileRequired?.() ?? false,
        manifestActions: options.readManifestActionProgress?.() ?? [],
      });
    },
  };
}
