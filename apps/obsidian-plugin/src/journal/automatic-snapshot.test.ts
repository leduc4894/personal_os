import { describe, expect, it } from "vitest";

import {
  AutomaticSnapshotCoordinator,
  CoalescingQueuePassDispatcher,
  refreshVerifiedPolicyAndRequestSnapshot,
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

  it("waits for an active bounded queue pass and runs one coalesced follow-up", async () => {
    let runPassCallCount = 0;
    const firstPass = { release: null as (() => void) | null };
    const dispatcher = new CoalescingQueuePassDispatcher({
      runPass: async () => {
        runPassCallCount += 1;
        if (runPassCallCount === 1) {
          await new Promise<void>((resolve) => {
            firstPass.release = resolve;
          });
        }
        return { outcome: "completed", processedEventCount: 0 };
      },
    });

    const firstRequest = dispatcher.request();
    await waitUntil(() => runPassCallCount === 1);
    const overlappingRequest = dispatcher.request();
    firstPass.release?.();

    await Promise.all([firstRequest, overlappingRequest]);
    expect(runPassCallCount).toBe(2);
  });

  it("aborts the active snapshot and waits for it to quiesce when stopped", async () => {
    let started = false;
    let stopped = false;
    let queuePassCallCount = 0;
    const coordinator = new AutomaticSnapshotCoordinator({
      runSnapshot: async (signal) => {
        started = true;
        await new Promise<void>((resolve) => {
          signal.addEventListener("abort", () => resolve(), { once: true });
        });
        stopped = signal.aborted;
        return { outcome: "stopped", queuedEventCount: 0 };
      },
      requestQueuePass: async () => {
        queuePassCallCount += 1;
      },
    });

    coordinator.request("startup");
    await waitUntil(() => started);
    await coordinator.stop();

    expect(stopped).toBe(true);
    expect(queuePassCallCount).toBe(0);
  });

  it("requests a policy-revision snapshot only after a verified refresh advances the revision", async () => {
    let revisionNumber = 7;
    const reasons: string[] = [];

    await refreshVerifiedPolicyAndRequestSnapshot({
      readAcceptedRevisionNumber: () => revisionNumber,
      refresh: async () => {
        revisionNumber = 8;
      },
      requestSnapshot: (reason) => reasons.push(reason),
    });

    expect(reasons).toEqual(["policy_revision_advanced"]);
  });
});
