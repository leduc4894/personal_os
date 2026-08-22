export type AutomaticSnapshotReason = "startup" | "policy_accepted" | "policy_revision_advanced";

export interface AutomaticSnapshotResult {
  readonly outcome: "completed" | "skipped" | "stopped";
  readonly queuedEventCount: number;
}

export interface AutomaticSnapshotRunner {
  runSnapshot(): Promise<AutomaticSnapshotResult>;
  requestQueuePass(): Promise<void>;
}

export class AutomaticSnapshotCoordinator {
  readonly #runner: AutomaticSnapshotRunner;
  #hasFollowUpSnapshot = false;
  #isRunning = false;
  #isStopped = false;

  constructor(runner: AutomaticSnapshotRunner) {
    this.#runner = runner;
  }

  request(_reason: AutomaticSnapshotReason): void {
    if (this.#isStopped) return;
    this.#hasFollowUpSnapshot = true;
    if (!this.#isRunning) void this.#drain();
  }

  stop(): void {
    this.#isStopped = true;
    this.#hasFollowUpSnapshot = false;
  }

  async #drain(): Promise<void> {
    this.#isRunning = true;
    try {
      while (!this.#isStopped && this.#hasFollowUpSnapshot) {
        this.#hasFollowUpSnapshot = false;
        const result = await this.#runner.runSnapshot();
        if (!this.#isStopped && result.outcome === "completed" && result.queuedEventCount > 0) {
          await this.#runner.requestQueuePass();
        }
      }
    } finally {
      this.#isRunning = false;
    }
  }
}
