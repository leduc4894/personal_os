import type { QueuePassSummary } from "./queue-driver";
import type { JournalFailureReporter } from "./diagnostic-reporter";

export type AutomaticSnapshotReason = "startup" | "policy_accepted" | "policy_revision_advanced";

export interface AutomaticSnapshotResult {
  readonly outcome: "completed" | "skipped" | "stopped";
  readonly queuedEventCount: number;
}

export interface AutomaticSnapshotRunner {
  runSnapshot(signal: AbortSignal): Promise<AutomaticSnapshotResult>;
  requestQueuePass(): Promise<void>;
}

export interface CoalescingQueuePassRunner {
  runPass(): Promise<QueuePassSummary>;
}

/**
 * Coalesces foreground queue triggers without changing the queue driver's
 * one-active-request contract. A trigger arriving during a pass owns exactly
 * one follow-up pass once the active pass exits.
 */
export class CoalescingQueuePassDispatcher {
  readonly #runner: CoalescingQueuePassRunner;
  readonly #failureReporter: JournalFailureReporter | null;
  #hasFollowUpPass = false;
  #isStopped = false;
  #drainPromise: Promise<void> | null = null;

  constructor(
    runner: CoalescingQueuePassRunner,
    failureReporter: JournalFailureReporter | null = null,
  ) {
    this.#runner = runner;
    this.#failureReporter = failureReporter;
  }

  request(): Promise<void> {
    if (this.#isStopped) {
      return Promise.resolve();
    }
    this.#hasFollowUpPass = true;
    if (this.#drainPromise === null) {
      const drainPromise = this.#drain().catch(() => {
        this.#failureReporter?.reportJournalFailure("queue_drain_failed");
        return undefined;
      });
      this.#drainPromise = drainPromise;
      void drainPromise.finally(() => {
        if (this.#drainPromise === drainPromise) {
          this.#drainPromise = null;
        }
      });
    }
    return this.#drainPromise;
  }

  stop(): Promise<void> {
    this.#isStopped = true;
    this.#hasFollowUpPass = false;
    return this.#drainPromise ?? Promise.resolve();
  }

  async #drain(): Promise<void> {
    while (!this.#isStopped && this.#hasFollowUpPass) {
      this.#hasFollowUpPass = false;
      const summary = await this.#runner.runPass();
      if (!this.#isStopped && summary.outcome === "deadline_reached") {
        this.#hasFollowUpPass = true;
      }
    }
  }
}

export interface VerifiedPolicyRefreshOptions {
  readAcceptedRevisionNumber(): number | null;
  refresh(): Promise<void>;
  requestSnapshot(reason: AutomaticSnapshotReason): void;
}

/** Request re-admission only after a verified refresh advances policy. */
export async function refreshVerifiedPolicyAndRequestSnapshot(
  options: VerifiedPolicyRefreshOptions,
): Promise<void> {
  const previousRevisionNumber = options.readAcceptedRevisionNumber();
  await options.refresh();
  const acceptedRevisionNumber = options.readAcceptedRevisionNumber();
  if (
    previousRevisionNumber !== null &&
    acceptedRevisionNumber !== null &&
    acceptedRevisionNumber > previousRevisionNumber
  ) {
    options.requestSnapshot("policy_revision_advanced");
  }
}

export class AutomaticSnapshotCoordinator {
  readonly #runner: AutomaticSnapshotRunner;
  readonly #failureReporter: JournalFailureReporter | null;
  #hasFollowUpSnapshot = false;
  #isStopped = false;
  readonly #abortController = new AbortController();
  #drainPromise: Promise<void> | null = null;

  constructor(
    runner: AutomaticSnapshotRunner,
    failureReporter: JournalFailureReporter | null = null,
  ) {
    this.#runner = runner;
    this.#failureReporter = failureReporter;
  }

  request(reason: AutomaticSnapshotReason): void {
    void reason;
    if (this.#isStopped) return;
    this.#hasFollowUpSnapshot = true;
    if (this.#drainPromise === null) {
      const drainPromise = this.#drain().catch(() => {
        this.#failureReporter?.reportJournalFailure("snapshot_drain_failed");
        return undefined;
      });
      this.#drainPromise = drainPromise;
      void drainPromise.finally(() => {
        if (this.#drainPromise === drainPromise) {
          this.#drainPromise = null;
        }
      });
    }
  }

  stop(): Promise<void> {
    this.#isStopped = true;
    this.#hasFollowUpSnapshot = false;
    this.#abortController.abort();
    return this.#drainPromise ?? Promise.resolve();
  }

  async #drain(): Promise<void> {
    while (!this.#isStopped && this.#hasFollowUpSnapshot) {
      this.#hasFollowUpSnapshot = false;
      const result = await this.#runner.runSnapshot(this.#abortController.signal);
      if (!this.#isStopped && result.outcome === "completed" && result.queuedEventCount > 0) {
        await this.#runner.requestQueuePass();
      }
    }
  }
}

// --- the periodic reconciliation cadence (task 11, spec 9.1) -------------------------------

/**
 * The accumulated-foreground-active interval between periodic full
 * reconciliations (spec 9.1): six hours. The reconciler receives the
 * closed `periodic` reason when the cadence elapses.
 */
export const PERIODIC_RECONCILE_INTERVAL_MS = 6 * 60 * 60 * 1000;

/**
 * The six-hour accumulated-foreground-active cadence of the periodic full
 * reconciliation (task 11, spec 9.1). Foreground time accumulates in
 * increments; each elapsed interval requests ONE periodic reconciliation
 * (consuming exactly one interval, so the cadence repeats); a completed
 * reconciliation resets the accumulator. Suspend time never accumulates.
 */
export class ForegroundReconcileCadence {
  #accumulatedForegroundActiveMs = 0;

  /**
   * Accumulate foreground-active time; whether the six-hour cadence
   * elapsed (consuming exactly one interval per elapsed request).
   */
  recordForegroundActiveMs(deltaMilliseconds: number): boolean {
    if (
      typeof deltaMilliseconds !== "number" ||
      !Number.isInteger(deltaMilliseconds) ||
      deltaMilliseconds < 0
    ) {
      throw new TypeError("foreground-active delta must be a non-negative integer");
    }
    this.#accumulatedForegroundActiveMs += deltaMilliseconds;
    if (this.#accumulatedForegroundActiveMs < PERIODIC_RECONCILE_INTERVAL_MS) {
      return false;
    }
    this.#accumulatedForegroundActiveMs -= PERIODIC_RECONCILE_INTERVAL_MS;
    return true;
  }

  /** Reset the accumulator after a completed reconciliation. */
  reset(): void {
    this.#accumulatedForegroundActiveMs = 0;
  }

  /** The currently accumulated foreground-active milliseconds. */
  readAccumulatedForegroundActiveMs(): number {
    return this.#accumulatedForegroundActiveMs;
  }
}
