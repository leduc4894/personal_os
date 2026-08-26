/**
 * Tests of the single foreground sync coordinator (device cursor and
 * manifest reconciliation, task 12, spec 11, 12).
 *
 * These tests pin the composition contract of {@link createSyncCoordinator}:
 *
 * - ONE coordinator owns every mutating foreground network phase, so
 *   recovery, repair, the outbound drain, the inbound pull and the cursor
 *   acknowledgement never overlap (the phase probe's maximum concurrent
 *   mutation count is exactly 1) and simultaneous triggers coalesce.
 * - Each cycle is bounded: recovery, repair-if-required, an eligible
 *   outbound drain, ONE inbound page, the local acknowledgement and at
 *   most one follow-up request.
 * - The fake clock/scheduler pins the cadence: a 30 second foreground
 *   pull, a reconciliation every six accumulated foreground-active
 *   hours, cancellable jittered exponential retry backoff between one
 *   second and five minutes, unload cancellation and the one-hour
 *   manifest expiry after suspension.
 * - A self-origin device id alone never suppresses an event: only the
 *   exact event/source/version/fingerprint evidence closes the matching
 *   outbound row before the cursor advances, and a lost acknowledgement
 *   stays owed and is retried before another pull.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { DeviceSyncApiError } from "./api";
import type { DeviceEventPage, DeviceSyncEvent } from "./api";
import type {
  DeviceSyncDiagnostics,
  DeviceSyncReason,
  DeviceSyncRepository,
  DeviceSyncState,
  EchoMarker,
  RemoteApplyOperation,
  TerminalDeviceEvent,
} from "./contracts";
import {
  DEVICE_SYNC_MANIFEST_EXPIRY_AFTER_SUSPEND_MS,
  DEVICE_SYNC_PULL_INTERVAL_MS,
  DEVICE_SYNC_RECONCILE_ACCUMULATED_ACTIVE_MS,
  createSyncCoordinator,
} from "./sync-coordinator";
import type { SyncCoordinator, SyncTrigger } from "./sync-coordinator";
import type { ManifestReconcileOutcome, ManifestReconciler, ReconcileReason } from "./manifest-reconciler";
import type { RemoteEventApplier } from "./remote-event-applier";

// --- the deterministic clock and scheduler ----------------------------------------------------------

/** The manual scheduler: advancing the fake clock fires due one-shot timers in order. */
class FakeScheduler {
  nowEpochMs = 0;
  readonly #timers: {
    fireAtEpochMs: number;
    callback: () => void;
    isCancelled: boolean;
  }[] = [];

  get outstandingTimerCount(): number {
    return this.#timers.filter((timer) => !timer.isCancelled).length;
  }

  schedule(delayMs: number, callback: () => void): () => void {
    const timer = { fireAtEpochMs: this.nowEpochMs + delayMs, callback, isCancelled: false };
    this.#timers.push(timer);
    return () => {
      timer.isCancelled = true;
    };
  }

  /** Move the fake clock forward, firing every due timer (and every timer a fired timer re-arms) to completion. */
  advance(milliseconds: number): void {
    this.nowEpochMs += milliseconds;
    let firedAny = true;
    while (firedAny) {
      firedAny = false;
      for (const timer of [...this.#timers]) {
        if (!timer.isCancelled && timer.fireAtEpochMs <= this.nowEpochMs) {
          timer.isCancelled = true;
          timer.callback();
          firedAny = true;
        }
      }
    }
  }

  /**
   * Simulate a suspended device: the clock moves but the frozen timers do
   * not fire, exactly like a backgrounded Obsidian mobile session.
   */
  advanceWhileSuspended(milliseconds: number): void {
    this.nowEpochMs += milliseconds;
  }
}

/**
 * Flush the microtask queue so fire-and-forget cycles settle
 * deterministically. Each tracked phase costs a couple of microtask hops
 * (the call plus its `finally`), so the default covers several full
 * cycles.
 */
async function flushMicrotasks(rounds = 100): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await Promise.resolve();
  }
}

/** A manually resolved deferred (the ES2022 target has no `Promise.withResolvers`). */
function createDeferred(): { promise: Promise<void>; resolve(): void } {
  let resolve!: () => void;
  const promise = new Promise<void>((resolver) => {
    resolve = resolver;
  });
  return { promise, resolve };
}

// --- the fake seams -----------------------------------------------------------------------------------

const SOURCE_ID = "99999999-9999-4999-8999-999999999999";
const OWN_DEVICE_ID = "d0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d0d0";
const OTHER_DEVICE_ID = "e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1";
const EVENT_ID = "88888888-8888-4888-8888-888888888888";
const COMMITTED_VERSION_ID = "12345678-1234-4123-8123-123456781234";
const FINGERPRINT_B = { sha256: "b".repeat(64), sizeBytes: 12, mediaType: "text/markdown" };

class FakeDeviceSyncRepository implements DeviceSyncRepository {
  state: DeviceSyncState = {
    appliedSequence: 0,
    acknowledgedSequence: 0,
    observationGeneration: 0,
    barrierGeneration: null,
    barrierReason: null,
    activeManifestRunId: null,
    manifestCheckpointSequence: null,
    manifestFinalDigest: null,
  };
  readonly terminalizedEvents: TerminalDeviceEvent[] = [];
  readonly acknowledgedSequences: number[] = [];
  failTerminalizeWithReason: string | null = null;
  failAcknowledgeWithReason: string | null = null;
  /**
   * Fails the NEXT state read that sees an owed acknowledgement debt
   * (applied > acknowledged) — exactly the reads the coordinator's
   * acknowledgement path performs.
   */
  failReadStateWhenAckOwedWithReason: string | null = null;

  readState(): DeviceSyncState {
    if (
      this.failReadStateWhenAckOwedWithReason !== null &&
      this.state.appliedSequence > this.state.acknowledgedSequence
    ) {
      const reason = this.failReadStateWhenAckOwedWithReason;
      this.failReadStateWhenAckOwedWithReason = null;
      throw Object.assign(new Error("store"), { reason });
    }
    return { ...this.state };
  }

  async terminalizeEvent(input: TerminalDeviceEvent): Promise<void> {
    if (this.failTerminalizeWithReason !== null) {
      const reason = this.failTerminalizeWithReason;
      this.failTerminalizeWithReason = null;
      throw Object.assign(new Error("store"), { reason });
    }
    this.terminalizedEvents.push(input);
    this.state = { ...this.state, appliedSequence: input.eventSequence };
  }

  async recordServerAcknowledgement(sequence: number): Promise<void> {
    if (this.failAcknowledgeWithReason !== null) {
      const reason = this.failAcknowledgeWithReason;
      this.failAcknowledgeWithReason = null;
      throw Object.assign(new Error("store"), { reason });
    }
    this.acknowledgedSequences.push(sequence);
    this.state = { ...this.state, acknowledgedSequence: sequence };
  }

  async nextObservationGeneration(): Promise<number> {
    this.state = { ...this.state, observationGeneration: this.state.observationGeneration + 1 };
    return this.state.observationGeneration;
  }

  async startRepairBarrier(): Promise<void> {
    this.state = { ...this.state, barrierGeneration: this.state.observationGeneration };
  }

  async recordManifestPage(): Promise<void> {
    return undefined;
  }

  async recordManifestAction(): Promise<void> {
    return undefined;
  }

  async prepareRemoteApply(): Promise<void> {
    return undefined;
  }

  async transitionRemoteApply(): Promise<void> {
    return undefined;
  }

  async completeRepair(): Promise<void> {
    this.state = {
      ...this.state,
      barrierGeneration: null,
      barrierReason: null,
      activeManifestRunId: null,
      manifestCheckpointSequence: null,
      manifestFinalDigest: null,
    };
  }

  readUnfinishedApply(): RemoteApplyOperation | null {
    return null;
  }

  async recordEchoMarker(): Promise<void> {
    return undefined;
  }

  readEchoMarker(): EchoMarker | null {
    return null;
  }

  async matchAndConsumeEcho(): Promise<boolean> {
    return false;
  }
}

class FakeWireApi {
  readonly pages: DeviceEventPage[] = [];
  pullCount = 0;
  readonly acknowledgements: {
    expectedPreviousSequence: number;
    appliedThroughSequence: number;
  }[] = [];
  pullError: DeviceSyncApiError | null = null;
  acknowledgeError: DeviceSyncApiError | null = null;

  async pullEvents(): Promise<DeviceEventPage> {
    this.pullCount += 1;
    if (this.pullError !== null) {
      throw this.pullError;
    }
    const page = this.pages.shift() ?? {
      acknowledgedSequence: 0,
      deliveredThroughSequence: 0,
      pageCheckpointSequence: 0,
      events: [],
      hasMore: false,
    };
    return page;
  }

  async acknowledgeCursor(input: {
    expectedPreviousSequence: number;
    appliedThroughSequence: number;
  }): Promise<{ acknowledgedSequence: number; deliveredThroughSequence: number }> {
    this.acknowledgements.push(input);
    if (this.acknowledgeError !== null) {
      throw this.acknowledgeError;
    }
    return {
      acknowledgedSequence: input.appliedThroughSequence,
      deliveredThroughSequence: input.appliedThroughSequence,
    };
  }
}

class FakeApplier implements RemoteEventApplier {
  readonly appliedEvents: DeviceSyncEvent[] = [];
  recoveryCount = 0;
  recoveryError: Error | null = null;
  /** Gates every apply so tests can fire triggers mid-cycle. */
  applyGate: Promise<void> | null = null;

  constructor(private readonly repository: FakeDeviceSyncRepository) {}

  async recoverUnfinishedApply(): Promise<void> {
    this.recoveryCount += 1;
    if (this.recoveryError !== null) {
      throw this.recoveryError;
    }
  }

  async apply(event: DeviceSyncEvent): Promise<TerminalDeviceEvent> {
    if (this.applyGate !== null) {
      await this.applyGate;
    }
    this.appliedEvents.push(event);
    this.repository.state = {
      ...this.repository.state,
      appliedSequence: event.eventSequence,
    };
    return { eventSequence: event.eventSequence, outcome: "applied", reason: null };
  }
}

class FakeReconciler implements ManifestReconciler {
  readonly reconcileReasons: ReconcileReason[] = [];
  resumeCount = 0;
  outcome: ManifestReconcileOutcome = { kind: "completed", checkpointSequence: 0 };

  constructor(private readonly repository: FakeDeviceSyncRepository) {}

  async reconcile(reason: ReconcileReason): Promise<ManifestReconcileOutcome> {
    this.reconcileReasons.push(reason);
    if (this.outcome.kind === "completed") {
      this.repository.state = {
        ...this.repository.state,
        barrierGeneration: null,
        barrierReason: null,
        activeManifestRunId: null,
        manifestCheckpointSequence: null,
        manifestFinalDigest: null,
      };
    }
    return this.outcome;
  }

  async resume(): Promise<ManifestReconcileOutcome> {
    this.resumeCount += 1;
    if (this.outcome.kind === "completed") {
      this.repository.state = {
        ...this.repository.state,
        barrierGeneration: null,
        barrierReason: null,
        activeManifestRunId: null,
        manifestCheckpointSequence: null,
        manifestFinalDigest: null,
      };
    }
    return this.outcome;
  }
}

class FakeOutboundLane {
  requestCount = 0;
  requestError: Error | null = null;

  async request(): Promise<void> {
    this.requestCount += 1;
    if (this.requestError !== null) {
      throw this.requestError;
    }
  }
}

class RecordingDiagnostics implements DeviceSyncDiagnostics {
  readonly observations: {
    lane: string;
    stage: string;
    reason: DeviceSyncReason;
  }[] = [];

  cursorFailure(stage: string, reason: DeviceSyncReason): void {
    this.observations.push({ lane: "cursor", stage, reason });
  }

  applyFailure(stage: string, reason: DeviceSyncReason): void {
    this.observations.push({ lane: "apply", stage, reason });
  }

  reconcileFailure(stage: string, reason: DeviceSyncReason): void {
    this.observations.push({ lane: "reconcile", stage, reason });
  }

  credentialFailure(stage: string, reason: DeviceSyncReason): void {
    this.observations.push({ lane: "credential", stage, reason });
  }
}

function eventOf(overrides: Partial<DeviceSyncEvent> & { eventSequence: number }): DeviceSyncEvent {
  const { eventSequence, ...restOverrides } = overrides;
  return {
    eventId: EVENT_ID,
    eventSequence,
    operation: "updated",
    sourceId: SOURCE_ID,
    originDeviceId: null,
    baseVersionId: null,
    currentVersionId: null,
    baseFingerprint: null,
    currentFingerprint: FINGERPRINT_B,
    priorLocator: "notes/a.md",
    resultingLocator: "notes/a.md",
    tombstoneId: null,
    committedAt: "2026-01-01T00:00:00Z",
    ...restOverrides,
  };
}

function pageOf(events: readonly DeviceSyncEvent[], hasMore = false): DeviceEventPage {
  const last = events[events.length - 1];
  return {
    acknowledgedSequence: 0,
    deliveredThroughSequence: last?.eventSequence ?? 0,
    pageCheckpointSequence: last?.eventSequence ?? 0,
    events,
    hasMore,
  };
}

interface CommittedRow {
  readonly sourceId: string | null;
  readonly baseVersionId: string | null;
  readonly lastCommittedFingerprint: {
    readonly sha256: string;
    readonly sizeBytes: number;
    readonly mediaType: string;
  } | null;
}

interface Harness {
  readonly coordinator: SyncCoordinator;
  readonly scheduler: FakeScheduler;
  readonly repository: FakeDeviceSyncRepository;
  readonly api: FakeWireApi;
  readonly applier: FakeApplier;
  readonly reconciler: FakeReconciler;
  readonly outbound: FakeOutboundLane;
  readonly diagnostics: RecordingDiagnostics;
  readonly phaseProbe: {
    readonly phases: readonly string[];
    readonly maximumConcurrentMutations: number;
  };
  readonly discardExpiredManifestRunCalls: () => number;
}

function createHarness(options: {
  readonly ownDeviceId?: string | null;
  readonly committedRowByLocator?: CommittedRow | null;
  readonly isJournalReconcileRequired?: boolean;
} = {}): Harness {
  const scheduler = new FakeScheduler();
  const repository = new FakeDeviceSyncRepository();
  const api = new FakeWireApi();
  const applier = new FakeApplier(repository);
  const reconciler = new FakeReconciler(repository);
  const outbound = new FakeOutboundLane();
  const diagnostics = new RecordingDiagnostics();
  let discardCalls = 0;

  // The phase probe wraps every mutating foreground phase so the tests can
  // assert both the pinned ordering and the no-overlap invariant.
  const probeState = {
    phases: [] as string[],
    currentMutations: 0,
    maximumConcurrentMutations: 0,
  };
  function trackedPhase<T>(phase: string, run: () => Promise<T>): Promise<T> {
    probeState.currentMutations += 1;
    probeState.maximumConcurrentMutations = Math.max(
      probeState.maximumConcurrentMutations,
      probeState.currentMutations,
    );
    probeState.phases.push(phase);
    return run().finally(() => {
      probeState.currentMutations -= 1;
    });
  }

  const coordinator = createSyncCoordinator({
    repository,
    api: {
      pullEvents: () => trackedPhase("pull", () => api.pullEvents()),
      acknowledgeCursor: (input) => trackedPhase("acknowledge", () => api.acknowledgeCursor(input)),
    },
    applier: {
      recoverUnfinishedApply: () => trackedPhase("recovery", () => applier.recoverUnfinishedApply()),
      apply: (event) => trackedPhase(`apply:${event.eventSequence}`, () => applier.apply(event)),
    },
    reconciler: {
      reconcile: (reason) =>
        trackedPhase("repair:reconcile", () => reconciler.reconcile(reason)),
      resume: () => trackedPhase("repair:resume", () => reconciler.resume()),
    },
    outbound: {
      request: () => trackedPhase("outbound", () => outbound.request()),
    },
    diagnostics,
    nowEpochMs: () => scheduler.nowEpochMs,
    scheduler: (delayMs, callback) => scheduler.schedule(delayMs, callback),
    randomJitter: () => 0,
    isJournalReconcileRequired: () => options.isJournalReconcileRequired ?? false,
    resolveOwnDeviceId: () => options.ownDeviceId ?? null,
    outboundEvidence: {
      readCommittedOutboundRowByLocator: () => options.committedRowByLocator ?? null,
    },
    discardExpiredManifestRun: () => {
      discardCalls += 1;
      repository.state = {
        ...repository.state,
        activeManifestRunId: null,
        manifestCheckpointSequence: null,
        manifestFinalDigest: null,
      };
      return Promise.resolve();
    },
  });

  return {
    coordinator,
    scheduler,
    repository,
    api,
    applier,
    reconciler,
    outbound,
    diagnostics,
    phaseProbe: {
      phases: probeState.phases,
      get maximumConcurrentMutations(): number {
        return probeState.maximumConcurrentMutations;
      },
    },
    discardExpiredManifestRunCalls: () => discardCalls,
  };
}

// --- Step 1: single-phase serialization and ordering --------------------------------------------------

describe("SyncCoordinator single-phase serialization (task 12 step 1)", () => {
  it("serializes all mutating phases", async () => {
    const harness = createHarness();
    harness.coordinator.request("startup");
    harness.coordinator.request("local_commit");
    harness.coordinator.request("explicit_repair");
    await flushMicrotasks();
    expect(harness.phaseProbe.maximumConcurrentMutations).toBe(1);
  });

  it("runs one bounded cycle in the pinned order: recovery, repair, outbound, pull, ack", async () => {
    const harness = createHarness({ isJournalReconcileRequired: true });
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.phaseProbe.phases).toEqual([
      "recovery",
      "repair:reconcile",
      "outbound",
      "pull",
      "apply:1",
      "acknowledge",
    ]);
    // The journal reconcile flag without a barrier reconciles once with the
    // local-invariant reason.
    expect(harness.reconciler.reconcileReasons).toEqual(["local_invariant"]);
  });

  it("resumes the repair before the outbound drain and unlocks the drain on completion", async () => {
    const harness = createHarness();
    harness.repository.state = {
      ...harness.repository.state,
      barrierGeneration: 3,
      barrierReason: "device_cursor_gap",
    };
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.reconciler.resumeCount).toBe(1);
    // The completed repair cleared the barrier, so the outbound rows are
    // dispatchable again inside the SAME cycle.
    expect(harness.outbound.requestCount).toBe(1);
    expect(harness.phaseProbe.phases).toEqual([
      "recovery",
      "repair:resume",
      "outbound",
      "pull",
      "apply:1",
      "acknowledge",
    ]);
  });

  it("skips the outbound drain while a blocked repair keeps the barrier active", async () => {
    const harness = createHarness();
    harness.repository.state = {
      ...harness.repository.state,
      barrierGeneration: 3,
      barrierReason: "device_cursor_gap",
    };
    harness.reconciler.outcome = { kind: "blocked", reason: "device_manifest_digest_mismatch" };
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    // The barrier survives the blocked repair, so outbound rows stay
    // frozen — while the inbound pull still applies server truth.
    expect(harness.reconciler.resumeCount).toBe(1);
    expect(harness.outbound.requestCount).toBe(0);
    expect(harness.phaseProbe.phases).toEqual([
      "recovery",
      "repair:resume",
      "pull",
      "apply:1",
      "acknowledge",
    ]);
  });

  it("coalesces simultaneous triggers into one follow-up cycle", async () => {
    const harness = createHarness();
    const gate = createDeferred();
    harness.applier.applyGate = gate.promise;
    harness.api.pages.push(
      pageOf([eventOf({ eventSequence: 1 })]),
      pageOf([eventOf({ eventSequence: 2 })]),
    );
    harness.coordinator.request("startup");
    await flushMicrotasks(3);
    // The first cycle is gated inside its apply; the two later triggers
    // coalesce into ONE pending cycle.
    harness.coordinator.request("local_commit");
    harness.coordinator.request("pull_interval");
    gate.resolve();
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(2);
    expect(harness.phaseProbe.phases.filter((phase) => phase === "recovery").length).toBe(2);
  });

  it("applies one full page per cycle and acknowledges once at the end", async () => {
    const harness = createHarness();
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 }), eventOf({ eventSequence: 2 })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.applier.appliedEvents.map((event) => event.eventSequence)).toEqual([1, 2]);
    expect(harness.api.acknowledgements).toEqual([
      { expectedPreviousSequence: 0, appliedThroughSequence: 2 },
    ]);
    expect(harness.repository.state.appliedSequence).toBe(2);
    expect(harness.repository.state.acknowledgedSequence).toBe(2);
  });

  it("chains at most one follow-up request after a page that has more", async () => {
    const harness = createHarness();
    harness.api.pages.push(
      pageOf([eventOf({ eventSequence: 1 })], true),
      pageOf([eventOf({ eventSequence: 2 })], true),
    );
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    // One trigger cycle plus exactly one follow-up — never an unbounded
    // chain; the third page waits for the next cadence tick.
    expect(harness.api.pullCount).toBe(2);
    harness.scheduler.advance(DEVICE_SYNC_PULL_INTERVAL_MS);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(3);
  });
});

// --- Step 2: cadence, backoff and suspension (fake clock) ---------------------------------------------

describe("SyncCoordinator cadence, backoff and suspension (task 12 step 2)", () => {
  it("pulls on the foreground cadence every 30 seconds and not before", async () => {
    const harness = createHarness();
    harness.coordinator.request("startup");
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(1);
    harness.scheduler.advance(DEVICE_SYNC_PULL_INTERVAL_MS - 1);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(1);
    harness.scheduler.advance(1);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(2);
  });

  it("reconciles after six accumulated foreground-active hours", async () => {
    const harness = createHarness();
    harness.coordinator.request("startup");
    await flushMicrotasks();
    // Healthy state: nothing owes a repair, so the accumulated ticks run
    // plain pull cycles only.
    harness.scheduler.advance(DEVICE_SYNC_RECONCILE_ACCUMULATED_ACTIVE_MS);
    await flushMicrotasks();
    expect(harness.reconciler.reconcileReasons).toEqual([]);
    // A barrier makes the repair owed; the next six accumulated hours fire
    // the periodic reconciliation exactly once and reset the accumulator.
    harness.repository.state = {
      ...harness.repository.state,
      barrierGeneration: 5,
      barrierReason: "device_cursor_gap",
    };
    harness.scheduler.advance(DEVICE_SYNC_RECONCILE_ACCUMULATED_ACTIVE_MS);
    await flushMicrotasks();
    expect(harness.reconciler.reconcileReasons).toContain("periodic");
    expect(
      harness.reconciler.reconcileReasons.filter((reason) => reason === "periodic").length,
    ).toBe(1);
  });

  it("schedules a cancellable jittered exponential retry between 1s and 5m", async () => {
    const harness = createHarness();
    harness.coordinator.request("startup");
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(1);
    // While the backoff schedule owns the retry cadence, the pull cadence
    // pauses, so every count below is driven by the backoff alone.
    harness.api.pullError = new DeviceSyncApiError("network_offline", true);
    // Attempt 1 fails on the first cadence tick; its retry waits exactly
    // one second (the injected jitter is zero).
    harness.scheduler.advance(DEVICE_SYNC_PULL_INTERVAL_MS);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(2);
    harness.scheduler.advance(999);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(2);
    harness.scheduler.advance(1);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(3);
    // Attempt 2 doubles the backoff to two seconds.
    harness.scheduler.advance(1_999);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(3);
    harness.scheduler.advance(1);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(4);
    // Attempt 3 doubles again to four seconds.
    harness.scheduler.advance(3_999);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(4);
    harness.scheduler.advance(1);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(5);
    // The exponential schedule keeps doubling until the five-minute cap.
    for (const [delayMs, expectedPullCount] of [
      [8_000, 6],
      [16_000, 7],
      [32_000, 8],
      [64_000, 9],
      [128_000, 10],
      [256_000, 11],
    ] as const) {
      harness.scheduler.advance(delayMs);
      await flushMicrotasks();
      expect(harness.api.pullCount).toBe(expectedPullCount);
    }
    // The capped retry waits exactly five minutes.
    harness.scheduler.advance(299_999);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(11);
    harness.scheduler.advance(1);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(12);
    // A success resets both the backoff and the pull cadence.
    harness.api.pullError = null;
    harness.scheduler.advance(300_000);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(13);
    harness.api.pullError = new DeviceSyncApiError("network_offline", true);
    harness.scheduler.advance(30_000);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(14);
    harness.scheduler.advance(1_000);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(15);
  });

  it("never retries a login-required failure on the backoff schedule", async () => {
    const harness = createHarness();
    harness.coordinator.request("startup");
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(1);
    harness.api.pullError = new DeviceSyncApiError("login_required", false);
    harness.scheduler.advance(DEVICE_SYNC_PULL_INTERVAL_MS);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(2);
    // No retry timer is outstanding: only the next cadence tick pulls again.
    expect(harness.scheduler.outstandingTimerCount).toBe(1);
    harness.scheduler.advance(1_000);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(2);
  });

  it("keeps the pull cadence paused while a backoff owns the retry schedule (fix round 1, minor 3)", async () => {
    const harness = createHarness();
    harness.coordinator.request("startup");
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(1);
    harness.api.pullError = new DeviceSyncApiError("network_offline", true);
    harness.scheduler.advance(DEVICE_SYNC_PULL_INTERVAL_MS);
    await flushMicrotasks();
    // The failed cycle paused the cadence: only the 1 s backoff retry is
    // outstanding.
    expect(harness.api.pullCount).toBe(2);
    expect(harness.scheduler.outstandingTimerCount).toBe(1);
    // A local commit arriving during the outage must NOT re-arm the
    // paused pull tick — the backoff owns the retry schedule.
    harness.coordinator.request("local_commit");
    expect(harness.scheduler.outstandingTimerCount).toBe(1);
    // The commit's own cycle fails too (the outage persists), so the
    // backoff doubles and stays the only timer.
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(3);
    expect(harness.scheduler.outstandingTimerCount).toBe(1);
    harness.scheduler.advance(2_000);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(4);
    // The first success resumes the cadence anchored at the success, and
    // exactly one pull tick exists again.
    harness.api.pullError = null;
    harness.scheduler.advance(4_000);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(5);
    expect(harness.scheduler.outstandingTimerCount).toBe(1);
    harness.scheduler.advance(DEVICE_SYNC_PULL_INTERVAL_MS - 1);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(5);
    harness.scheduler.advance(1);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(6);
  });

  it("stops cleanly on unload: timers cancel and the running cycle settles", async () => {
    const harness = createHarness();
    const gate = createDeferred();
    harness.applier.applyGate = gate.promise;
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 })]));
    harness.coordinator.request("startup");
    await flushMicrotasks(3);
    let isStopped = false;
    void harness.coordinator.stop().then(() => {
      isStopped = true;
    });
    await flushMicrotasks(3);
    // The stop waits for the gated cycle without deadlocking.
    expect(isStopped).toBe(false);
    gate.resolve();
    await flushMicrotasks();
    expect(isStopped).toBe(true);
    expect(harness.scheduler.outstandingTimerCount).toBe(0);
    const pullsBefore = harness.api.pullCount;
    harness.coordinator.request("local_commit");
    harness.scheduler.advance(DEVICE_SYNC_RECONCILE_ACCUMULATED_ACTIVE_MS);
    await flushMicrotasks();
    expect(harness.api.pullCount).toBe(pullsBefore);
  });

  it("discards an active manifest run only after a suspension of one hour or more", async () => {
    const harness = createHarness();
    harness.reconciler.outcome = { kind: "blocked", reason: "device_cursor_gap" };
    harness.repository.state = {
      ...harness.repository.state,
      barrierGeneration: 2,
      barrierReason: "device_cursor_gap",
      activeManifestRunId: "77777777-7777-4777-8777-777777777777",
      manifestCheckpointSequence: 4,
    };
    harness.coordinator.request("startup");
    await flushMicrotasks();
    // The blocked repair keeps the barrier AND the run active.
    expect(harness.discardExpiredManifestRunCalls()).toBe(0);
    // A suspension shorter than one hour keeps the run resumable: the
    // frozen cadence never fires, so no cycle runs while suspended.
    harness.scheduler.advanceWhileSuspended(DEVICE_SYNC_MANIFEST_EXPIRY_AFTER_SUSPEND_MS - 1);
    harness.coordinator.request("resume");
    await flushMicrotasks();
    expect(harness.discardExpiredManifestRunCalls()).toBe(0);
    expect(harness.reconciler.resumeCount).toBeGreaterThanOrEqual(1);
    // A suspension of one hour or more expires the run before the resume
    // cycle runs.
    harness.scheduler.advanceWhileSuspended(DEVICE_SYNC_MANIFEST_EXPIRY_AFTER_SUSPEND_MS);
    harness.repository.state = {
      ...harness.repository.state,
      activeManifestRunId: "77777777-7777-4777-8777-777777777777",
    };
    harness.coordinator.request("resume");
    await flushMicrotasks();
    expect(harness.discardExpiredManifestRunCalls()).toBe(1);
  });
});

// --- Step 3: self-origin suppression and cursor acknowledgement --------------------------------------

describe("SyncCoordinator self-origin and acknowledgement (task 12 step 3)", () => {
  it("never suppresses on the self-origin device id alone", async () => {
    const harness = createHarness({ ownDeviceId: OWN_DEVICE_ID, committedRowByLocator: null });
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1, originDeviceId: OWN_DEVICE_ID })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.applier.appliedEvents.map((event) => event.eventSequence)).toEqual([1]);
    expect(harness.repository.terminalizedEvents).toEqual([]);
  });

  it("closes the matching outbound row before cursor advancement on exact evidence", async () => {
    const harness = createHarness({
      ownDeviceId: OWN_DEVICE_ID,
      committedRowByLocator: {
        sourceId: SOURCE_ID,
        baseVersionId: COMMITTED_VERSION_ID,
        lastCommittedFingerprint: FINGERPRINT_B,
      },
    });
    harness.api.pages.push(
      pageOf([
        eventOf({
          eventSequence: 1,
          originDeviceId: OWN_DEVICE_ID,
          currentVersionId: COMMITTED_VERSION_ID,
          currentFingerprint: FINGERPRINT_B,
        }),
      ]),
    );
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    // The exact event/source/version/fingerprint evidence terminalizes the
    // self-origin no-op WITHOUT any Vault mutation, then the cursor advance
    // and the acknowledgement still land.
    expect(harness.applier.appliedEvents).toEqual([]);
    expect(harness.repository.terminalizedEvents).toEqual([
      { eventSequence: 1, outcome: "self_origin_no_op", reason: null },
    ]);
    expect(harness.repository.state.appliedSequence).toBe(1);
    expect(harness.repository.state.acknowledgedSequence).toBe(1);
  });

  it("requires every evidence member: a diverging version still applies", async () => {
    const harness = createHarness({
      ownDeviceId: OWN_DEVICE_ID,
      committedRowByLocator: {
        sourceId: SOURCE_ID,
        baseVersionId: COMMITTED_VERSION_ID,
        lastCommittedFingerprint: FINGERPRINT_B,
      },
    });
    harness.api.pages.push(
      pageOf([
        eventOf({
          eventSequence: 1,
          originDeviceId: OWN_DEVICE_ID,
          currentVersionId: "87654321-4321-4321-8321-876543218765",
          currentFingerprint: FINGERPRINT_B,
        }),
      ]),
    );
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.applier.appliedEvents.map((event) => event.eventSequence)).toEqual([1]);
  });

  it("requires the self-origin origin: exact evidence of another device still applies", async () => {
    const harness = createHarness({
      ownDeviceId: OWN_DEVICE_ID,
      committedRowByLocator: {
        sourceId: SOURCE_ID,
        baseVersionId: COMMITTED_VERSION_ID,
        lastCommittedFingerprint: FINGERPRINT_B,
      },
    });
    harness.api.pages.push(
      pageOf([
        eventOf({
          eventSequence: 1,
          originDeviceId: OTHER_DEVICE_ID,
          currentVersionId: COMMITTED_VERSION_ID,
          currentFingerprint: FINGERPRINT_B,
        }),
      ]),
    );
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.applier.appliedEvents.map((event) => event.eventSequence)).toEqual([1]);
  });

  it("never matches a client-instance uuid against the server's uuid7 device origin", async () => {
    // Fix round 1 (blocker A): the server mints origin_device_id as a
    // uuid7 at grant exchange while client_instance_id is a client-minted
    // v4 uuid — two disjoint namespaces. Exact evidence alone must never
    // close the row across them.
    const harness = createHarness({
      ownDeviceId: "2f0c7d1e-6b3a-4c8e-9d2f-1a2b3c4d5e6f",
      committedRowByLocator: {
        sourceId: SOURCE_ID,
        baseVersionId: COMMITTED_VERSION_ID,
        lastCommittedFingerprint: FINGERPRINT_B,
      },
    });
    const serverMintedDeviceId = "018f6b2e-7a1e-7abc-9def-0123456789ab";
    harness.api.pages.push(
      pageOf([
        eventOf({
          eventSequence: 1,
          originDeviceId: serverMintedDeviceId,
          currentVersionId: COMMITTED_VERSION_ID,
          currentFingerprint: FINGERPRINT_B,
        }),
      ]),
    );
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.applier.appliedEvents.map((event) => event.eventSequence)).toEqual([1]);
    expect(harness.repository.terminalizedEvents).toEqual([]);
  });

  it("keeps a lost acknowledgement owed and retries it before another pull", async () => {
    const harness = createHarness();
    harness.api.acknowledgeError = new DeviceSyncApiError("network_offline", true);
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    // The event applied and terminalized locally, but the server
    // acknowledgement was lost: the debt stays visible in the status.
    expect(harness.repository.state.appliedSequence).toBe(1);
    expect(harness.repository.state.acknowledgedSequence).toBe(0);
    const statusAfterLoss = harness.coordinator.readStatus();
    expect(statusAfterLoss.cursorLag).toBe(1);
    // The retry cycle acknowledges the owed cursor BEFORE pulling again.
    harness.api.acknowledgeError = null;
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 }), eventOf({ eventSequence: 2 })]));
    harness.scheduler.advance(1_000);
    await flushMicrotasks();
    const phaseLog = harness.phaseProbe.phases;
    const retryCycleStart = phaseLog.indexOf("recovery", 1);
    const retryAckIndex = phaseLog.indexOf("acknowledge", retryCycleStart);
    const retryPullIndex = phaseLog.indexOf("pull", retryCycleStart);
    expect(retryCycleStart).toBeGreaterThan(-1);
    expect(retryAckIndex).toBeGreaterThan(-1);
    expect(retryAckIndex).toBeLessThan(retryPullIndex);
    // The already-settled replay of event 1 is skipped; only event 2 applies.
    expect(harness.applier.appliedEvents.map((event) => event.eventSequence)).toEqual([1, 2]);
    expect(harness.repository.state.acknowledgedSequence).toBe(2);
  });

  it("reports the closed acknowledge stage when the local acknowledgement store fails", async () => {
    const harness = createHarness();
    harness.repository.failAcknowledgeWithReason = "journal_mutation_failed";
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.diagnostics.observations).toContainEqual({
      lane: "cursor",
      stage: "acknowledge",
      reason: "journal_mutation_failed",
    });
  });

  it("reports the closed acknowledge stage when the ack-path state read fails (fix round 1, minor 5)", async () => {
    const harness = createHarness();
    harness.repository.failReadStateWhenAckOwedWithReason = "journal_query_failed";
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    // The failing read belongs to the acknowledgement path (it sees the
    // owed debt after the apply), so its observation names the
    // acknowledge stage — never the pull stage.
    expect(harness.diagnostics.observations).toContainEqual({
      lane: "cursor",
      stage: "acknowledge",
      reason: "journal_query_failed",
    });
    expect(
      harness.diagnostics.observations.some(
        (observation) =>
          observation.lane === "cursor" && observation.reason === "journal_query_failed" && observation.stage !== "acknowledge",
      ),
    ).toBe(false);
    // The store error is non-retryable: no backoff timer joins the
    // cadence after the failed cycle.
    expect(harness.scheduler.outstandingTimerCount).toBe(1);
  });

  it("reports the closed local-commit stage when a self-origin settle fails", async () => {
    const harness = createHarness({
      ownDeviceId: OWN_DEVICE_ID,
      committedRowByLocator: {
        sourceId: SOURCE_ID,
        baseVersionId: COMMITTED_VERSION_ID,
        lastCommittedFingerprint: FINGERPRINT_B,
      },
    });
    harness.repository.failTerminalizeWithReason = "journal_mutation_failed";
    harness.api.pages.push(
      pageOf([
        eventOf({
          eventSequence: 1,
          originDeviceId: OWN_DEVICE_ID,
          currentVersionId: COMMITTED_VERSION_ID,
          currentFingerprint: FINGERPRINT_B,
        }),
      ]),
    );
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.diagnostics.observations).toContainEqual({
      lane: "apply",
      stage: "local_commit",
      reason: "journal_mutation_failed",
    });
    // The settle failed closed: the cursor never advanced.
    expect(harness.repository.state.appliedSequence).toBe(0);
  });
});

// --- repair states -------------------------------------------------------------------------------------

describe("SyncCoordinator repair scheduling (task 12)", () => {
  it("forces an explicit repair even when nothing is owed", async () => {
    const harness = createHarness();
    harness.coordinator.request("explicit_repair");
    await flushMicrotasks();
    expect(harness.reconciler.reconcileReasons).toEqual(["explicit_repair"]);
  });

  it("records a blocked repair as a readable state and never auto-retries it", async () => {
    const harness = createHarness();
    harness.repository.state = {
      ...harness.repository.state,
      barrierGeneration: 2,
      barrierReason: "device_cursor_gap",
    };
    harness.reconciler.outcome = { kind: "blocked", reason: "device_manifest_state_invalid" };
    harness.coordinator.request("startup");
    await flushMicrotasks();
    const blockedStatus = harness.coordinator.readStatus();
    expect(blockedStatus.repairState).toBe("blocked");
    expect(blockedStatus.reason).toBe("device_manifest_state_invalid");
    // Cadence ticks keep pulling but never retry the blocked repair.
    harness.scheduler.advance(DEVICE_SYNC_PULL_INTERVAL_MS * 3);
    await flushMicrotasks();
    expect(harness.reconciler.resumeCount).toBe(1);
    // An explicit repair clears the blocked verdict and retries.
    harness.reconciler.outcome = { kind: "completed", checkpointSequence: 7 };
    harness.coordinator.request("explicit_repair");
    await flushMicrotasks();
    expect(harness.reconciler.reconcileReasons).toEqual(["explicit_repair"]);
    expect(harness.coordinator.readStatus().repairState).toBe("ready");
  });

  it("retries a retryable repair outcome on the backoff schedule", async () => {
    const harness = createHarness();
    harness.repository.state = {
      ...harness.repository.state,
      barrierGeneration: 2,
      barrierReason: "device_cursor_gap",
    };
    harness.reconciler.outcome = { kind: "retry", reason: "network_offline" };
    harness.coordinator.request("startup");
    await flushMicrotasks();
    expect(harness.reconciler.resumeCount).toBe(1);
    harness.scheduler.advance(1_000);
    await flushMicrotasks();
    expect(harness.reconciler.resumeCount).toBe(2);
  });

  it("skips redelivered events the local cursor already settled", async () => {
    const harness = createHarness();
    harness.api.pages.push(pageOf([eventOf({ eventSequence: 1 }), eventOf({ eventSequence: 2 })]));
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    const applyCount = harness.applier.appliedEvents.length;
    expect(applyCount).toBe(2);
    // A later page redelivers the settled events; neither re-applies.
    harness.api.pages.push(
      pageOf([eventOf({ eventSequence: 1 }), eventOf({ eventSequence: 2 })]),
    );
    harness.coordinator.request("pull_interval");
    await flushMicrotasks();
    expect(harness.applier.appliedEvents.length).toBe(2);
  });
});

// --- read-only guards -----------------------------------------------------------------------------------

describe("SyncCoordinator source-level mobile-loadability guard (task 12)", () => {
  it("imports no Node, Electron or Obsidian runtime capability at module load", () => {
    const source = readFileSync(new URL("./sync-coordinator.ts", import.meta.url), "utf8");
    for (const forbiddenText of [
      "node:",
      "electron",
      "FileSystemAdapter",
      "from \"obsidian\"",
      "fetch(",
      "process.env",
    ]) {
      expect(source).not.toContain(forbiddenText);
    }
  });

  it("exposes only the pinned trigger vocabulary", () => {
    const triggers: readonly SyncTrigger[] = [
      "startup",
      "resume",
      "local_commit",
      "pull_interval",
      "periodic_reconcile",
      "explicit_repair",
    ];
    expect(triggers.length).toBe(6);
    for (const trigger of triggers) {
      expect(() => harnessTriggerAcceptance(trigger)).not.toThrow();
    }
  });
});

/** The coordinator accepts every closed trigger token (compile-time union exercise). */
function harnessTriggerAcceptance(trigger: SyncTrigger): SyncTrigger {
  return trigger;
}
