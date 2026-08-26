/**
 * Tests of the device-sync reconciliation repository invariants (device
 * cursor and manifest reconciliation, task 8, spec 8, 11, 12).
 *
 * Every mutation of {@link DeviceSyncRepository} runs inside the journal's
 * single serialized writer. These tests pin the durable invariants: cursor
 * monotonicity and contiguity, strictly incrementing observation
 * generations, exactly one active repair barrier, exact manifest page and
 * action replay, the legal remote-apply transition lattice, the terminal
 * event plus cursor landing in one serialized generation, acknowledgement
 * debt ordering, exact echo marker matching, and the closed local status
 * reasons every invariant blocker persists.
 */

import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type { FrozenFingerprint } from "../journal/contracts";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "../journal/sqlite-database";
import type { SqliteEngineModule } from "../journal/sqlite-database";
import { DeviceSyncRepository } from "./repository";
import type {
  EchoMarker,
  PreparedRemoteApply,
  VaultObservation,
} from "./contracts";

/** The real sql.js WebAssembly engine drives every repository test (spec 6.1). */
let engineModule: SqliteEngineModule;

beforeAll(async () => {
  const wasmBytes = new Uint8Array(
    readFileSync(new URL("../../node_modules/sql.js/dist/sql-wasm.wasm", import.meta.url)),
  );
  const wasmBinary = wasmBytes.buffer.slice(
    wasmBytes.byteOffset,
    wasmBytes.byteOffset + wasmBytes.byteLength,
  ) as ArrayBuffer;
  engineModule = await initSqlJs({ wasmBinary });
});

const SOURCE_ID = "99999999-9999-4999-8999-999999999999";
const EVENT_ID = "88888888-8888-4888-8888-888888888888";
const MANIFEST_RUN_ID = "77777777-7777-4777-8777-777777777777";
const SHA256_A = "a".repeat(64);
const SHA256_B = "b".repeat(64);
const SHA256_C = "c".repeat(64);
const PAGE_DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

function fingerprintOf(sha256: string, sizeBytes = 32): FrozenFingerprint {
  return { sha256, sizeBytes, mediaType: "text/markdown" };
}

function createRepository(): { repository: DeviceSyncRepository; database: SqliteDatabase } {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  });
  const repository = new DeviceSyncRepository({ database });
  return { repository, database };
}

function preparedApplyOf(
  eventSequence: number,
  operation: PreparedRemoteApply["operation"] = "updated",
): PreparedRemoteApply {
  return {
    eventSequence,
    eventId: EVENT_ID,
    sourceId: SOURCE_ID,
    operation,
    priorLocator: "notes/a.md",
    targetLocator: operation === "renamed" || operation === "moved" || operation === "restored" ? "notes/b.md" : null,
    baseFingerprint: fingerprintOf(SHA256_A, 10),
    finalFingerprint: operation === "deleted" ? null : fingerprintOf(SHA256_B, 12),
    tempToken: null,
    rollbackToken: null,
  };
}

function observationOfMarker(marker: EchoMarker, overrides: Partial<VaultObservation> = {}): VaultObservation {
  return {
    eventSequence: marker.eventSequence,
    sourceId: marker.sourceId,
    operation: marker.operation,
    priorLocator: marker.priorLocator,
    targetLocator: marker.targetLocator,
    fingerprint: marker.finalFingerprint,
    ...overrides,
  };
}

/** Drive one event fully through the apply machine: prepare, mutate, terminalize. */
async function applyEventThroughMachine(
  repository: DeviceSyncRepository,
  eventSequence: number,
  operation: PreparedRemoteApply["operation"] = "updated",
): Promise<void> {
  await repository.prepareRemoteApply(preparedApplyOf(eventSequence, operation));
  if (operation === "created" || operation === "updated") {
    await repository.transitionRemoteApply({
      eventSequence,
      state: "temp_verified",
      tempToken: `temp-${eventSequence}`,
    });
  }
  await repository.transitionRemoteApply({ eventSequence, state: "vault_mutated" });
  await repository.terminalizeEvent({ eventSequence, outcome: "applied", reason: null });
}

/** Start one barrier and record the first manifest page to bind the run. */
async function startBarrierWithRun(
  repository: DeviceSyncRepository,
  checkpointSequence: number,
): Promise<void> {
  const state = repository.readState();
  await repository.startRepairBarrier({ generation: state.observationGeneration, reason: "device_cursor_gap" });
  await repository.recordManifestPage({
    manifestRunId: MANIFEST_RUN_ID,
    pageNumber: 0,
    entryCount: 2,
    pageDigest: PAGE_DIGEST,
    checkpointSequence,
    finalDigest: null,
  });
}

// --- cursor monotonicity and contiguity (spec 11) ------------------------------------------------------------

describe("DeviceSyncRepository cursor invariants", () => {
  it("advances the applied cursor on the exact contiguous terminal event", async () => {
    const { repository } = createRepository();
    await applyEventThroughMachine(repository, 1);
    const state = repository.readState();
    expect(state.appliedSequence).toBe(1);
    expect(state.acknowledgedSequence).toBe(0);
  });

  it("rejects a terminal event beyond the contiguous cursor with the device_cursor_gap barrier", async () => {
    const { repository } = createRepository();
    await expect(
      repository.terminalizeEvent({ eventSequence: 3, outcome: "self_origin_no_op", reason: null }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    const state = repository.readState();
    expect(state.appliedSequence).toBe(0);
    expect(state.barrierGeneration).toBe(0);
    expect(state.barrierReason).toBe("device_cursor_gap");
  });

  it("rejects a terminal event at or below the applied cursor with the device_cursor_regression barrier", async () => {
    const { repository } = createRepository();
    await applyEventThroughMachine(repository, 1);

    await expect(
      repository.terminalizeEvent({ eventSequence: 1, outcome: "self_origin_no_op", reason: null }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    const state = repository.readState();
    expect(state.appliedSequence).toBe(1);
    expect(state.barrierReason).toBe("device_cursor_regression");
  });

  it("keeps the terminal outcome and the cursor advance in one serialized generation", async () => {
    const { repository } = createRepository();
    await repository.prepareRemoteApply(preparedApplyOf(1));
    await repository.transitionRemoteApply({ eventSequence: 1, state: "temp_verified", tempToken: "temp-1" });
    await repository.transitionRemoteApply({ eventSequence: 1, state: "vault_mutated", rollbackToken: "rb-1" });

    await repository.terminalizeEvent({ eventSequence: 1, outcome: "applied", reason: null });

    // One call moved both the apply row to locally_applied AND the cursor.
    const operation = repository.readUnfinishedApply();
    expect(operation).not.toBeNull();
    expect(operation?.state).toBe("locally_applied");
    expect(repository.readState().appliedSequence).toBe(1);
  });

  it("rejects an acknowledgement ahead of the applied cursor with the device_cursor_ack_ahead barrier", async () => {
    const { repository } = createRepository();
    await applyEventThroughMachine(repository, 1);

    await expect(repository.recordServerAcknowledgement(2)).rejects.toMatchObject({
      reason: "journal_mutation_failed",
    });
    const state = repository.readState();
    expect(state.acknowledgedSequence).toBe(0);
    expect(state.barrierReason).toBe("device_cursor_ack_ahead");
  });

  it("rejects an acknowledgement below the acknowledged cursor with the device_cursor_regression barrier", async () => {
    const { repository } = createRepository();
    await applyEventThroughMachine(repository, 1);
    await repository.recordServerAcknowledgement(1);

    await expect(repository.recordServerAcknowledgement(0)).rejects.toMatchObject({
      reason: "journal_mutation_failed",
    });
    const state = repository.readState();
    expect(state.acknowledgedSequence).toBe(1);
    expect(state.barrierReason).toBe("device_cursor_regression");
  });

  it("treats an exact acknowledgement replay as an idempotent no-op", async () => {
    const { repository } = createRepository();
    await applyEventThroughMachine(repository, 1);
    await repository.recordServerAcknowledgement(1);
    await repository.recordServerAcknowledgement(1);

    const state = repository.readState();
    expect(state.acknowledgedSequence).toBe(1);
    expect(state.barrierGeneration).toBeNull();
  });
});

// --- observation generation (spec 12.1) ----------------------------------------------------------------------

describe("DeviceSyncRepository observation generation invariants", () => {
  it("returns strictly incrementing observation generations", async () => {
    const { repository } = createRepository();
    expect(await repository.nextObservationGeneration()).toBe(1);
    expect(await repository.nextObservationGeneration()).toBe(2);
    const concurrent = await Promise.all([
      repository.nextObservationGeneration(),
      repository.nextObservationGeneration(),
    ]);
    expect([...concurrent].sort((a, b) => a - b)).toEqual([3, 4]);
    expect(repository.readState().observationGeneration).toBe(4);
  });

  it("keeps incrementing the generation under an active barrier", async () => {
    const { repository } = createRepository();
    await repository.startRepairBarrier({ generation: 0, reason: "device_event_unavailable" });
    expect(await repository.nextObservationGeneration()).toBe(1);
    expect(await repository.nextObservationGeneration()).toBe(2);
    expect(repository.readState().observationGeneration).toBe(2);
  });
});

// --- the repair barrier (spec 12.1, 12.4) ---------------------------------------------------------------------

describe("DeviceSyncRepository repair barrier invariants", () => {
  it("starts exactly one barrier pinned to the current observation generation", async () => {
    const { repository } = createRepository();
    await repository.nextObservationGeneration();
    await repository.startRepairBarrier({ generation: 1, reason: "device_cursor_gap" });

    const state = repository.readState();
    expect(state.barrierGeneration).toBe(1);
    expect(state.barrierReason).toBe("device_cursor_gap");
  });

  it("refuses a second active barrier", async () => {
    const { repository } = createRepository();
    await repository.startRepairBarrier({ generation: 0, reason: "device_cursor_gap" });
    await expect(
      repository.startRepairBarrier({ generation: 0, reason: "device_cursor_gap" }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierGeneration).toBe(0);
  });

  it("refuses a barrier generation that is not the current observation generation", async () => {
    const { repository } = createRepository();
    await repository.nextObservationGeneration();
    await expect(
      repository.startRepairBarrier({ generation: 0, reason: "device_cursor_gap" }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierGeneration).toBeNull();
  });

  it("refuses a foreign barrier reason", async () => {
    const { repository } = createRepository();
    await expect(
      repository.startRepairBarrier({ generation: 0, reason: "made_up_reason" as never }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierGeneration).toBeNull();
  });

  it("clears the barrier and manifest progress on completeRepair, advancing both cursors to the checkpoint", async () => {
    const { repository, database } = createRepository();
    await applyEventThroughMachine(repository, 1);
    await repository.recordServerAcknowledgement(1);
    await startBarrierWithRun(repository, 4);
    await repository.recordManifestAction({
      manifestRunId: MANIFEST_RUN_ID,
      actionIndex: 0,
      actionKind: "download",
      outcome: "terminal_safe",
      reason: null,
    });

    const barrierGeneration = repository.readState().barrierGeneration;
    if (barrierGeneration === null) {
      throw new Error("expected an active repair barrier");
    }
    await repository.completeRepair({
      manifestRunId: MANIFEST_RUN_ID,
      checkpointSequence: 4,
      barrierGeneration,
    });

    const state = repository.readState();
    expect(state.appliedSequence).toBe(4);
    expect(state.acknowledgedSequence).toBe(4);
    expect(state.barrierGeneration).toBeNull();
    expect(state.barrierReason).toBeNull();
    expect(state.activeManifestRunId).toBeNull();
    expect(state.manifestCheckpointSequence).toBeNull();
    expect(state.manifestFinalDigest).toBeNull();

    const pageCount = database.readAll(
      "select count(*) from manifest_page_progress;",
    )[0]?.values[0]?.[0];
    const actionCount = database.readAll(
      "select count(*) from manifest_action_progress;",
    )[0]?.values[0]?.[0];
    expect(pageCount).toBe(0);
    expect(actionCount).toBe(0);
  });

  it("refuses completeRepair for a foreign run or barrier generation", async () => {
    const { repository } = createRepository();
    await startBarrierWithRun(repository, 2);

    await expect(
      repository.completeRepair({
        manifestRunId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        checkpointSequence: 2,
        barrierGeneration: 0,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.completeRepair({
        manifestRunId: MANIFEST_RUN_ID,
        checkpointSequence: 2,
        barrierGeneration: 5,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierGeneration).toBe(0);
    expect(repository.readState().activeManifestRunId).toBe(MANIFEST_RUN_ID);
  });

  it("refuses a repair checkpoint below the applied cursor", async () => {
    const { repository } = createRepository();
    await applyEventThroughMachine(repository, 1);
    await startBarrierWithRun(repository, 0);

    await expect(
      repository.completeRepair({
        manifestRunId: MANIFEST_RUN_ID,
        checkpointSequence: 0,
        barrierGeneration: 0,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_cursor_regression");
    expect(repository.readState().appliedSequence).toBe(1);
  });
});

// --- manifest page and action progress (spec 7.3, 12) ---------------------------------------------------------

describe("DeviceSyncRepository manifest progress invariants", () => {
  it("binds the run and checkpoint on the first accepted page and replays it exactly", async () => {
    const { repository, database } = createRepository();
    await repository.startRepairBarrier({ generation: 0, reason: "device_cursor_gap" });
    await repository.recordManifestPage({
      manifestRunId: MANIFEST_RUN_ID,
      pageNumber: 0,
      entryCount: 2,
      pageDigest: PAGE_DIGEST,
      checkpointSequence: 5,
      finalDigest: null,
    });

    const state = repository.readState();
    expect(state.activeManifestRunId).toBe(MANIFEST_RUN_ID);
    expect(state.manifestCheckpointSequence).toBe(5);

    // Exact replay of the accepted page is a no-op.
    await repository.recordManifestPage({
      manifestRunId: MANIFEST_RUN_ID,
      pageNumber: 0,
      entryCount: 2,
      pageDigest: PAGE_DIGEST,
      checkpointSequence: 5,
      finalDigest: null,
    });
    const pageCount = database.readAll(
      "select count(*) from manifest_page_progress;",
    )[0]?.values[0]?.[0];
    expect(pageCount).toBe(1);
  });

  it("records only the exact next contiguous page", async () => {
    const { repository } = createRepository();
    await startBarrierWithRun(repository, 3);

    await expect(
      repository.recordManifestPage({
        manifestRunId: MANIFEST_RUN_ID,
        pageNumber: 2,
        entryCount: 1,
        pageDigest: PAGE_DIGEST,
        checkpointSequence: 3,
        finalDigest: null,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_manifest_page_invalid");

    await repository.recordManifestPage({
      manifestRunId: MANIFEST_RUN_ID,
      pageNumber: 1,
      entryCount: 1,
      pageDigest: SHA256_C,
      checkpointSequence: 3,
      finalDigest: null,
    });
    expect(repository.readState().manifestCheckpointSequence).toBe(3);
  });

  it("rejects a replayed page number with different evidence", async () => {
    const { repository } = createRepository();
    await startBarrierWithRun(repository, 3);

    await expect(
      repository.recordManifestPage({
        manifestRunId: MANIFEST_RUN_ID,
        pageNumber: 0,
        entryCount: 2,
        pageDigest: SHA256_C,
        checkpointSequence: 3,
        finalDigest: null,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_manifest_page_replay_mismatch");
  });

  it("rejects a page of a foreign run and a contradictory checkpoint", async () => {
    const { repository } = createRepository();
    await startBarrierWithRun(repository, 3);

    await expect(
      repository.recordManifestPage({
        manifestRunId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        pageNumber: 1,
        entryCount: 1,
        pageDigest: PAGE_DIGEST,
        checkpointSequence: 3,
        finalDigest: null,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_manifest_state_invalid");

    await expect(
      repository.recordManifestPage({
        manifestRunId: MANIFEST_RUN_ID,
        pageNumber: 1,
        entryCount: 1,
        pageDigest: PAGE_DIGEST,
        checkpointSequence: 9,
        finalDigest: null,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_manifest_state_invalid");
  });

  it("records the final digest once and rejects a contradictory final digest", async () => {
    const { repository } = createRepository();
    await startBarrierWithRun(repository, 3);

    await repository.recordManifestPage({
      manifestRunId: MANIFEST_RUN_ID,
      pageNumber: 1,
      entryCount: 0,
      pageDigest: PAGE_DIGEST,
      checkpointSequence: 3,
      finalDigest: SHA256_A,
    });
    expect(repository.readState().manifestFinalDigest).toBe(SHA256_A);

    await expect(
      repository.recordManifestPage({
        manifestRunId: MANIFEST_RUN_ID,
        pageNumber: 1,
        entryCount: 0,
        pageDigest: PAGE_DIGEST,
        checkpointSequence: 3,
        finalDigest: SHA256_B,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_manifest_digest_mismatch");
  });

  it("refuses pages without an active barrier", async () => {
    const { repository } = createRepository();
    await expect(
      repository.recordManifestPage({
        manifestRunId: MANIFEST_RUN_ID,
        pageNumber: 0,
        entryCount: 1,
        pageDigest: PAGE_DIGEST,
        checkpointSequence: 1,
        finalDigest: null,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_manifest_state_invalid");
  });

  it("upgrades action progress to terminal_safe and never downgrades it", async () => {
    const { repository, database } = createRepository();
    await startBarrierWithRun(repository, 3);

    await repository.recordManifestAction({
      manifestRunId: MANIFEST_RUN_ID,
      actionIndex: 0,
      actionKind: "upload",
      outcome: "received",
      reason: null,
    });
    await repository.recordManifestAction({
      manifestRunId: MANIFEST_RUN_ID,
      actionIndex: 0,
      actionKind: "upload",
      outcome: "terminal_safe",
      reason: null,
    });
    // A stale exact replay of the original receipt never downgrades.
    await repository.recordManifestAction({
      manifestRunId: MANIFEST_RUN_ID,
      actionIndex: 0,
      actionKind: "upload",
      outcome: "received",
      reason: null,
    });

    const row = database.readAll(
      "select action_kind, outcome from manifest_action_progress where action_index = 0;",
    )[0]?.values[0];
    expect(row).toEqual(["upload", "terminal_safe"]);
  });

  it("rejects an action re-recorded with a different kind", async () => {
    const { repository } = createRepository();
    await startBarrierWithRun(repository, 3);
    await repository.recordManifestAction({
      manifestRunId: MANIFEST_RUN_ID,
      actionIndex: 0,
      actionKind: "download",
      outcome: "received",
      reason: null,
    });

    await expect(
      repository.recordManifestAction({
        manifestRunId: MANIFEST_RUN_ID,
        actionIndex: 0,
        actionKind: "conflict",
        outcome: "received",
        reason: null,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_manifest_state_invalid");
  });

  it("refuses actions of a foreign run", async () => {
    const { repository } = createRepository();
    await startBarrierWithRun(repository, 3);
    await expect(
      repository.recordManifestAction({
        manifestRunId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        actionIndex: 0,
        actionKind: "download",
        outcome: "received",
        reason: null,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_manifest_state_invalid");
  });
});

// --- the remote apply state machine (spec 8.1, 11) --------------------------------------------------------------

describe("DeviceSyncRepository remote apply invariants", () => {
  it("prepares one durable operation and reads it back as the unfinished apply", async () => {
    const { repository } = createRepository();
    const prepared = preparedApplyOf(1, "created");
    await repository.prepareRemoteApply(prepared);

    const operation = repository.readUnfinishedApply();
    expect(operation).toEqual({
      ...prepared,
      state: "prepared",
      safeErrorCode: null,
    });
  });

  it("treats an exact re-prepare as idempotent and refuses a conflicting one", async () => {
    const { repository } = createRepository();
    await repository.prepareRemoteApply(preparedApplyOf(1));
    await repository.prepareRemoteApply(preparedApplyOf(1));

    await expect(
      repository.prepareRemoteApply(preparedApplyOf(1, "deleted")),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_apply_recovery_ambiguous");
    expect(repository.readUnfinishedApply()?.operation).toBe("updated");
  });

  it("walks the legal content chain and clears the unfinished apply at acknowledgement", async () => {
    const { repository } = createRepository();
    await applyEventThroughMachine(repository, 1);

    let operation = repository.readUnfinishedApply();
    expect(operation?.state).toBe("locally_applied");

    await repository.recordServerAcknowledgement(1);
    operation = repository.readUnfinishedApply();
    expect(operation).toBeNull();
  });

  it("lets lifecycle operations skip temp_verified but blocks content operations that skip it", async () => {
    const { repository } = createRepository();
    await repository.prepareRemoteApply(preparedApplyOf(1, "renamed"));
    await repository.transitionRemoteApply({ eventSequence: 1, state: "vault_mutated" });
    await repository.terminalizeEvent({ eventSequence: 1, outcome: "applied", reason: null });
    await repository.recordServerAcknowledgement(1);
    expect(repository.readUnfinishedApply()).toBeNull();

    await repository.prepareRemoteApply(preparedApplyOf(2, "created"));
    await expect(
      repository.transitionRemoteApply({ eventSequence: 2, state: "vault_mutated" }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_apply_recovery_ambiguous");
    expect(repository.readUnfinishedApply()?.state).toBe("prepared");
  });

  it("allows the recovery transition to locally_applied from a verified content temp", async () => {
    const { repository } = createRepository();
    await repository.prepareRemoteApply(preparedApplyOf(1, "updated"));
    await repository.transitionRemoteApply({ eventSequence: 1, state: "temp_verified", tempToken: "t-1" });
    await repository.transitionRemoteApply({ eventSequence: 1, state: "locally_applied" });

    expect(repository.readUnfinishedApply()?.state).toBe("locally_applied");
  });

  it("rejects backwards and unknown-sequence transitions", async () => {
    const { repository } = createRepository();
    await repository.prepareRemoteApply(preparedApplyOf(1));
    await repository.transitionRemoteApply({ eventSequence: 1, state: "temp_verified", tempToken: "t-1" });

    await expect(
      repository.transitionRemoteApply({ eventSequence: 1, state: "prepared" }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_apply_recovery_ambiguous");

    await expect(
      repository.transitionRemoteApply({ eventSequence: 7, state: "vault_mutated" }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readUnfinishedApply()?.state).toBe("temp_verified");
  });

  it("requires the vault_mutated proof for an applied terminal outcome", async () => {
    const { repository } = createRepository();
    await repository.prepareRemoteApply(preparedApplyOf(1));

    await expect(
      repository.terminalizeEvent({ eventSequence: 1, outcome: "applied", reason: null }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().barrierReason).toBe("device_apply_recovery_ambiguous");
    expect(repository.readState().appliedSequence).toBe(0);
  });

  it("closes a dangling prepared row with its closed reason on a conflict terminal outcome", async () => {
    const { repository } = createRepository();
    await repository.prepareRemoteApply(preparedApplyOf(1));

    await repository.terminalizeEvent({
      eventSequence: 1,
      outcome: "conflict",
      reason: "device_manifest_target_occupied",
    });

    const operation = repository.readUnfinishedApply();
    expect(operation?.state).toBe("locally_applied");
    expect(operation?.safeErrorCode).toBe("device_manifest_target_occupied");
    expect(repository.readState().appliedSequence).toBe(1);
  });

  it("terminalizes self-origin no-op outcomes without a prepared operation row", async () => {
    const { repository } = createRepository();
    await repository.terminalizeEvent({ eventSequence: 1, outcome: "self_origin_no_op", reason: null });
    expect(repository.readState().appliedSequence).toBe(1);
    expect(repository.readUnfinishedApply()).toBeNull();
  });

  it("rejects a foreign apply state or terminal outcome", async () => {
    const { repository } = createRepository();
    await repository.prepareRemoteApply(preparedApplyOf(1));
    await expect(
      repository.transitionRemoteApply({ eventSequence: 1, state: "temp_staged" as never }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.terminalizeEvent({ eventSequence: 1, outcome: "committed" as never, reason: null }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readState().appliedSequence).toBe(0);
  });
});

// --- exact echo marker matching (spec 8.2) ---------------------------------------------------------------------

describe("DeviceSyncRepository echo marker invariants", () => {
  const marker: EchoMarker = {
    eventSequence: 1,
    sourceId: SOURCE_ID,
    operation: "updated",
    priorLocator: "notes/a.md",
    targetLocator: null,
    finalFingerprint: fingerprintOf(SHA256_B, 12),
  };

  it("records and reads back one exact marker", async () => {
    const { repository } = createRepository();
    await repository.recordEchoMarker(marker);
    expect(repository.readEchoMarker(1)).toEqual(marker);
    expect(repository.readEchoMarker(2)).toBeNull();
  });

  it("consumes the marker only on the exact operand match", async () => {
    const { repository } = createRepository();
    await repository.recordEchoMarker(marker);

    expect(await repository.matchAndConsumeEcho(observationOfMarker(marker))).toBe(true);
    expect(repository.readEchoMarker(1)).toBeNull();
    expect(await repository.matchAndConsumeEcho(observationOfMarker(marker))).toBe(false);
  });

  it("retains the marker when the fingerprint differs", async () => {
    const { repository } = createRepository();
    await repository.recordEchoMarker(marker);

    expect(
      await repository.matchAndConsumeEcho(
        observationOfMarker(marker, { fingerprint: fingerprintOf(SHA256_C, 12) }),
      ),
    ).toBe(false);
    expect(repository.readEchoMarker(1)).toEqual(marker);
  });

  it("retains the marker when a locator operand differs", async () => {
    const { repository } = createRepository();
    await repository.recordEchoMarker(marker);

    expect(
      await repository.matchAndConsumeEcho(
        observationOfMarker(marker, { priorLocator: "notes/other.md" }),
      ),
    ).toBe(false);
    expect(repository.readEchoMarker(1)).toEqual(marker);
  });

  it("matches a delete-shaped marker on its locators alone", async () => {
    const { repository } = createRepository();
    const deleteMarker: EchoMarker = {
      eventSequence: 1,
      sourceId: SOURCE_ID,
      operation: "deleted",
      priorLocator: "notes/a.md",
      targetLocator: null,
      finalFingerprint: null,
    };
    await repository.recordEchoMarker(deleteMarker);

    expect(
      await repository.matchAndConsumeEcho(observationOfMarker(deleteMarker)),
    ).toBe(true);
  });

  it("refuses a conflicting duplicate marker for one event sequence", async () => {
    const { repository } = createRepository();
    await repository.recordEchoMarker(marker);
    await expect(
      repository.recordEchoMarker({ ...marker, priorLocator: "notes/other.md" }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    // An exact duplicate stays a no-op.
    await repository.recordEchoMarker(marker);
    expect(repository.readEchoMarker(1)).toEqual(marker);
  });
});
