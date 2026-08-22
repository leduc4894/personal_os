import { describe, expect, it } from "vitest";

import {
  AutomaticSnapshotCoordinator,
  type AutomaticSnapshotResult,
  type AutomaticSnapshotRunner,
} from "./automatic-snapshot";

interface CoordinatorHarness {
  readonly coordinator: AutomaticSnapshotCoordinator;
  readonly runner: SnapshotRunnerHarness;
  waitUntilFirstSnapshotStarted(): Promise<void>;
  waitForIdle(): Promise<void>;
}

interface SnapshotRunnerHarness extends AutomaticSnapshotRunner {
  readonly snapshotCallCount: number;
  readonly queuePassCallCount: number;
  blockFirstSnapshot(): void;
  releaseFirstSnapshot(result: AutomaticSnapshotResult): void;
}

function createCoordinatorHarness(): CoordinatorHarness {
  let snapshotCallCount = 0;
  let queuePassCallCount = 0;
  let isFirstSnapshotBlocked = false;
  let firstSnapshotResult: AutomaticSnapshotResult | null = null;
  let releaseFirstSnapshot: ((result: AutomaticSnapshotResult) => void) | null = null;

  const runner: SnapshotRunnerHarness = {
    get snapshotCallCount(): number {
      return snapshotCallCount;
    },
    get queuePassCallCount(): number {
      return queuePassCallCount;
    },
    blockFirstSnapshot(): void {
      isFirstSnapshotBlocked = true;
    },
    releaseFirstSnapshot(result: AutomaticSnapshotResult): void {
      releaseFirstSnapshot?.(result);
    },
    async runSnapshot(): Promise<AutomaticSnapshotResult> {
      snapshotCallCount += 1;
      if (snapshotCallCount === 1 && isFirstSnapshotBlocked) {
        return await new Promise<AutomaticSnapshotResult>((resolve) => {
          releaseFirstSnapshot = resolve;
        });
      }
      return { outcome: "completed", queuedEventCount: 0 };
    },
    async requestQueuePass(): Promise<void> {
      queuePassCallCount += 1;
    },
  };
  const coordinator = new AutomaticSnapshotCoordinator(runner);

  return {
    coordinator,
    runner,
    async waitUntilFirstSnapshotStarted(): Promise<void> {
      await waitUntil(() => snapshotCallCount === 1);
    },
    async waitForIdle(): Promise<void> {
      await waitUntil(() => snapshotCallCount === 2);
      await Promise.resolve();
    },
  };
}

async function waitUntil(predicate: () => boolean): Promise<void> {
  while (!predicate()) {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
}

describe("AutomaticSnapshotCoordinator", () => {
  it("runs exactly one follow-up snapshot for triggers received during a running snapshot", async () => {
    const harness = createCoordinatorHarness();
    harness.runner.blockFirstSnapshot();
    harness.coordinator.request("startup");
    await harness.waitUntilFirstSnapshotStarted();
    harness.coordinator.request("policy_accepted");
    harness.coordinator.request("policy_revision_advanced");
    harness.runner.releaseFirstSnapshot({ outcome: "completed", queuedEventCount: 1 });
    await harness.waitForIdle();
    expect(harness.runner.snapshotCallCount).toBe(2);
    expect(harness.runner.queuePassCallCount).toBe(1);
  });

  it("does not request a queue pass or run a follow-up after it stops during a snapshot", async () => {
    const harness = createCoordinatorHarness();
    harness.runner.blockFirstSnapshot();
    harness.coordinator.request("startup");
    await harness.waitUntilFirstSnapshotStarted();
    harness.coordinator.request("policy_accepted");
    harness.coordinator.stop();
    harness.runner.releaseFirstSnapshot({ outcome: "completed", queuedEventCount: 1 });
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(harness.runner.snapshotCallCount).toBe(1);
    expect(harness.runner.queuePassCallCount).toBe(0);
  });
});
