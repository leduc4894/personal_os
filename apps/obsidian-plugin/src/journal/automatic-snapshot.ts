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
  runPass(): Promise<void>;
}

/**
 * Coalesces foreground queue triggers without changing the queue driver's
 * one-active-request contract. A trigger arriving during a pass owns exactly
 * one follow-up pass once the active pass exits.
 */
export class CoalescingQueuePassDispatcher {
  readonly #runner: CoalescingQueuePassRunner;
  #hasFollowUpPass = false;
  #isStopped = false;
  #drainPromise: Promise<void> | null = null;

  constructor(runner: CoalescingQueuePassRunner) {
    this.#runner = runner;
  }

  request(): Promise<void> {
    if (this.#isStopped) {
      return Promise.resolve();
    }
    this.#hasFollowUpPass = true;
    if (this.#drainPromise === null) {
      const drainPromise = this.#drain().catch(() => undefined);
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
      await this.#runner.runPass();
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
  #hasFollowUpSnapshot = false;
  #isStopped = false;
  readonly #abortController = new AbortController();
  #drainPromise: Promise<void> | null = null;

  constructor(runner: AutomaticSnapshotRunner) {
    this.#runner = runner;
  }

  request(_reason: AutomaticSnapshotReason): void {
    if (this.#isStopped) return;
    this.#hasFollowUpSnapshot = true;
    if (this.#drainPromise === null) {
      const drainPromise = this.#drain().catch(() => undefined);
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
