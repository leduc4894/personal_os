/**
 * Tests of the Conflict Inbox resolution controller (Child 8 spec 5.2.6,
 * Task 8).
 *
 * These tests pin the controller state machine: the server commit strictly
 * precedes the Vault apply, a failed atomic apply parks the canonical winner
 * as `local_apply_pending` with bounded retry bookkeeping and NEVER re-issues
 * the resolution, a lost commit acknowledgement replays the SAME resolution
 * event identity, byteless candidates offer no keep_local/save_merged, the
 * save_merged upload rides the injected verified-candidate uploader port
 * (no server route exists for it yet — Task 10), merge proposals decode only
 * supported text/Markdown within the bounds, and every diagnostics record is
 * a closed reason token — no raw content, path or digest ever reaches a
 * diagnostics fixture.
 */

import { describe, expect, it } from "vitest";

import { ConflictApiError } from "./api";
import type { ConflictApi } from "./api";
import { CanonicalApplyError, createConflictController } from "./controller";
import type {
  CanonicalOutcomeApplyCommand,
  CanonicalOutcomeApplier,
  ConflictController,
  ConflictControllerDiagnosticReason,
  ConflictDiagnosticsSink,
  ConflictMergeProposal,
  ConflictRepairStore,
  ConflictResolutionIdentityMinter,
  VerifiedCandidateUploader,
} from "./controller";
import {
  CONFLICT_CONTROLLER_DIAGNOSTIC_REASONS,
  CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS,
  CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS,
  CONFLICT_MERGE_UPLOAD_MEDIA_TYPE,
} from "./controller";
import type {
  ConflictDetail,
  ConflictEvidenceRole,
  ConflictPage,
  ConflictResolution,
  ConflictResolutionOutcome,
  PendingLocalApply,
  VerifiedConflictEvidence,
} from "./contracts";
import { MERGE_PROPOSAL_MAXIMUM_BYTES } from "./merge";
import type {
  CompleteLocalApplyInput,
  ParkPendingLocalApplyInput,
  RecordLocalApplyFailureInput,
} from "./repository";

// --- shared fixtures -------------------------------------------------------------------------------------

const CONFLICT_ID = "11111111-1111-4111-8111-111111111111";
const SOURCE_ID = "22222222-2222-4222-8222-222222222222";
const ORIGINATING_EVENT_ID = "33333333-3333-4333-8333-333333333333";
const ORIGINATING_DEVICE_ID = "44444444-4444-4444-8444-444444444444";
const BASE_VERSION_ID = "55555555-5555-4555-8555-555555555555";
const OBSERVED_REMOTE_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const VERIFIED_CANDIDATE_OBJECT_ID = "77777777-7777-4777-8777-777777777777";
const RESOLUTION_EVENT_ID = "88888888-8888-4888-8888-888888888888";
const IDEMPOTENCY_KEY = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const RESULTING_VERSION_ID = "99999999-9999-4999-8999-999999999999";
const SUCCESSOR_CONFLICT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

const NOW_EPOCH_MS = 1_750_000_000_000;

const SECRET_CONTENT = "# secret note content that must never leak into diagnostics";

function markdownEvidence(text: string): VerifiedConflictEvidence {
  return {
    bytes: new TextEncoder().encode(text),
    mediaType: "text/markdown",
    sizeBytes: new TextEncoder().encode(text).byteLength,
  };
}

function buildDetail(overrides?: Partial<ConflictDetail>): ConflictDetail {
  return {
    conflictId: CONFLICT_ID,
    sourceId: SOURCE_ID,
    conflictKind: "stale_content",
    status: "open",
    originatingEventId: ORIGINATING_EVENT_ID,
    originatingDeviceId: ORIGINATING_DEVICE_ID,
    baseVersionId: BASE_VERSION_ID,
    observedRemoteVersionId: OBSERVED_REMOTE_VERSION_ID,
    candidateKind: "content",
    verifiedCandidateObjectId: VERIFIED_CANDIDATE_OBJECT_ID,
    capturedAt: "2026-09-01T00:00:00Z",
    resolutionKind: null,
    resolutionEventId: null,
    resultingVersionId: null,
    successorConflictId: null,
    closedAt: null,
    choices: ["keep_remote", "keep_local", "save_merged"],
    ...overrides,
  };
}

function buildResolution(
  outcome: ConflictResolutionOutcome,
  overrides?: Partial<ConflictResolution>,
): ConflictResolution {
  return {
    outcome,
    conflictId: CONFLICT_ID,
    resolutionEventId: RESOLUTION_EVENT_ID,
    resolutionKind: "keep_remote",
    resultingVersionId: outcome === "resolved" ? null : RESULTING_VERSION_ID,
    successorConflictId: outcome === "stale_successor" ? SUCCESSOR_CONFLICT_ID : null,
    completedAt: "2026-09-02T00:00:00Z",
    ...overrides,
  };
}

// --- the fakes ---------------------------------------------------------------------------------------------

interface FakeConflictApiState {
  readonly api: ConflictApi;
  readonly resolveInputs: readonly unknown[];
  readonly detailRequests: readonly string[];
  readonly evidenceRequests: readonly string[];
  setDetail(detail: ConflictDetail): void;
  setResolution(resolution: ConflictResolution): void;
  setResolveFailure(failure: Error | null): void;
  setEvidence(role: ConflictEvidenceRole, value: VerifiedConflictEvidence | Error): void;
}

function createFakeApi(): FakeConflictApiState {
  const details = new Map<string, ConflictDetail>();
  const evidence = new Map<string, VerifiedConflictEvidence | Error>();
  const resolveInputs: unknown[] = [];
  const detailRequests: string[] = [];
  const evidenceRequests: string[] = [];
  let resolution: ConflictResolution = buildResolution("resolved");
  let resolveFailure: Error | null = null;
  const api: ConflictApi = {
    async listConflicts(): Promise<ConflictPage> {
      return { conflicts: [...details.values()], hasMore: false, nextExclusiveStartConflictId: null };
    },
    async getConflict(conflictId: string): Promise<ConflictDetail> {
      detailRequests.push(conflictId);
      const detail = details.get(conflictId);
      if (detail === undefined) {
        throw new ConflictApiError("conflict_not_found", false);
      }
      return detail;
    },
    async downloadConflictEvidence(input): Promise<VerifiedConflictEvidence> {
      evidenceRequests.push(`${input.conflictId}:${input.role}`);
      const value = evidence.get(input.role);
      if (value === undefined) {
        throw new ConflictApiError("evidence_unavailable", false);
      }
      if (value instanceof Error) {
        throw value;
      }
      return value;
    },
    async resolveConflict(input): Promise<ConflictResolution> {
      resolveInputs.push(input);
      if (resolveFailure !== null) {
        throw resolveFailure;
      }
      return resolution;
    },
  };
  return {
    api,
    get resolveInputs() {
      return resolveInputs;
    },
    get detailRequests() {
      return detailRequests;
    },
    get evidenceRequests() {
      return evidenceRequests;
    },
    setDetail(detail) {
      details.set(detail.conflictId, detail);
    },
    setResolution(next) {
      resolution = next;
    },
    setResolveFailure(failure) {
      resolveFailure = failure;
    },
    setEvidence(role, value) {
      evidence.set(role, value);
    },
  };
}

function createFakeRepairStore(): {
  store: ConflictRepairStore;
  rows: Map<string, PendingLocalApply>;
  parks: ParkPendingLocalApplyInput[];
  failures: RecordLocalApplyFailureInput[];
  completions: CompleteLocalApplyInput[];
} {
  const rows = new Map<string, PendingLocalApply>();
  const parks: ParkPendingLocalApplyInput[] = [];
  const failures: RecordLocalApplyFailureInput[] = [];
  const completions: CompleteLocalApplyInput[] = [];
  const store: ConflictRepairStore = {
    readPendingLocalApply(conflictId) {
      return rows.get(conflictId) ?? null;
    },
    readPendingLocalApplies() {
      return [...rows.values()].sort(
        (left, right) => left.createdAtEpochMs - right.createdAtEpochMs,
      );
    },
    async parkPendingLocalApply(input) {
      parks.push(input);
      rows.set(input.conflictId, {
        conflictId: input.conflictId,
        resolutionEventId: input.resolutionEventId,
        targetAction: input.targetAction,
        safeReason: input.safeReason,
        attemptCount: 0,
        nextEligibleRetryEpochMs: null,
        createdAtEpochMs: input.nowEpochMs,
        updatedAtEpochMs: input.nowEpochMs,
      });
    },
    async recordLocalApplyFailure(input) {
      failures.push(input);
      const existing = rows.get(input.conflictId);
      if (existing === undefined || existing.resolutionEventId !== input.resolutionEventId) {
        throw new Error("fake repair row mismatch");
      }
      rows.set(input.conflictId, {
        ...existing,
        safeReason: input.safeReason,
        attemptCount: existing.attemptCount + 1,
        nextEligibleRetryEpochMs: input.nextEligibleRetryEpochMs,
        updatedAtEpochMs: input.nowEpochMs,
      });
    },
    async completeLocalApply(input) {
      completions.push(input);
      rows.delete(input.conflictId);
    },
  };
  return { store, rows, parks, failures, completions };
}

function createFakeApplier(): {
  applier: CanonicalOutcomeApplier;
  commands: CanonicalOutcomeApplyCommand[];
  failWith(failure: Error | null): void;
} {
  const commands: CanonicalOutcomeApplyCommand[] = [];
  let failure: Error | null = null;
  return {
    applier: {
      async applyCanonicalOutcome(command) {
        commands.push(command);
        if (failure !== null) {
          throw failure;
        }
      },
    },
    commands,
    failWith(next) {
      failure = next;
    },
  };
}

function createFakeUploader(): {
  uploader: VerifiedCandidateUploader;
  uploads: { bytes: Uint8Array; mediaType: string }[];
  failWith(failure: Error | null): void;
} {
  const uploads: { bytes: Uint8Array; mediaType: string }[] = [];
  let failure: Error | null = null;
  return {
    uploader: {
      async uploadVerifiedCandidate(upload) {
        uploads.push({ bytes: upload.bytes, mediaType: upload.mediaType });
        if (failure !== null) {
          throw failure;
        }
        return { verifiedCandidateObjectId: VERIFIED_CANDIDATE_OBJECT_ID };
      },
    },
    uploads,
    failWith(next) {
      failure = next;
    },
  };
}

function createRecordingDiagnostics(): {
  sink: ConflictDiagnosticsSink;
  reasons: ConflictControllerDiagnosticReason[];
} {
  const reasons: ConflictControllerDiagnosticReason[] = [];
  return {
    sink: {
      observeConflictFailure(reason) {
        reasons.push(reason);
      },
    },
    reasons,
  };
}

function createSequentialIdentityMinter(): {
  minter: ConflictResolutionIdentityMinter;
  mintCount(): number;
} {
  let count = 0;
  return {
    minter: () => {
      count += 1;
      return { resolutionEventId: RESOLUTION_EVENT_ID, idempotencyKey: IDEMPOTENCY_KEY };
    },
    mintCount() {
      return count;
    },
  };
}

function buildController(
  apiState: FakeConflictApiState,
  repairState: ReturnType<typeof createFakeRepairStore>,
  applierState: ReturnType<typeof createFakeApplier>,
  uploaderState: ReturnType<typeof createFakeUploader>,
  diagnostics: ReturnType<typeof createRecordingDiagnostics>,
): { controller: ConflictController; identity: ReturnType<typeof createSequentialIdentityMinter> } {
  const identity = createSequentialIdentityMinter();
  const controller = createConflictController({
    api: apiState.api,
    repairStore: repairState.store,
    uploader: uploaderState.uploader,
    applier: applierState.applier,
    mintIdentity: identity.minter,
    clock: () => NOW_EPOCH_MS,
    diagnostics: diagnostics.sink,
  });
  return { controller, identity };
}

function createHarness() {
  const apiState = createFakeApi();
  apiState.setDetail(buildDetail());
  const repairState = createFakeRepairStore();
  const applierState = createFakeApplier();
  const uploaderState = createFakeUploader();
  const diagnostics = createRecordingDiagnostics();
  const harness = buildController(apiState, repairState, applierState, uploaderState, diagnostics);
  return { apiState, repairState, applierState, uploaderState, diagnostics, ...harness };
}

// --- the mandated parking invariant -----------------------------------------------------------------------

describe("conflict controller local apply parking (spec 5.2.6)", () => {
  it("parks a canonical winner as local_apply_pending when atomic Vault write fails", async () => {
    const harness = createHarness();
    harness.applierState.failWith(new CanonicalApplyError("vault_apply"));

    await expect(harness.controller.resolveKeepRemote(CONFLICT_ID)).resolves.toEqual({
      kind: "local_apply_pending",
    });

    // The resolution was issued exactly once and never re-issued.
    expect(harness.apiState.resolveInputs).toHaveLength(1);
    // The park happened the moment the canonical resolution committed.
    expect(harness.repairState.parks).toEqual([
      {
        conflictId: CONFLICT_ID,
        resolutionEventId: RESOLUTION_EVENT_ID,
        targetAction: "apply_remote_version",
        safeReason: "resolution_committed",
        nowEpochMs: NOW_EPOCH_MS,
      },
    ]);
    // The failed apply was recorded with the closed safe reason and the bounded backoff.
    expect(harness.repairState.failures).toEqual([
      {
        conflictId: CONFLICT_ID,
        resolutionEventId: RESOLUTION_EVENT_ID,
        safeReason: "vault_apply_failed",
        nowEpochMs: NOW_EPOCH_MS,
        nextEligibleRetryEpochMs: NOW_EPOCH_MS + CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS,
      },
    ]);
    expect(harness.repairState.completions).toEqual([]);
    expect(harness.diagnostics.reasons).toContain("conflict_vault_apply_failed");
  });

  it("parks as local_apply_pending when the winner download fails after the commit", async () => {
    const harness = createHarness();
    harness.applierState.failWith(new CanonicalApplyError("winner_download"));

    await expect(harness.controller.resolveKeepRemote(CONFLICT_ID)).resolves.toEqual({
      kind: "local_apply_pending",
    });
    expect(harness.apiState.resolveInputs).toHaveLength(1);
    expect(harness.repairState.failures[0]?.safeReason).toBe("winner_download_failed");
    expect(harness.diagnostics.reasons).toContain("conflict_winner_download_failed");
  });
});

// --- the explicit choice commands ------------------------------------------------------------------------

describe("conflict controller explicit choices (spec 5.2.2)", () => {
  it("resolves keep_remote end to end and completes the parked apply", async () => {
    const harness = createHarness();
    const result = await harness.controller.resolveKeepRemote(CONFLICT_ID);

    expect(result).toEqual({
      kind: "resolved_and_applied",
      resolution: buildResolution("resolved"),
    });
    expect(harness.applierState.commands).toEqual([
      {
        conflictId: CONFLICT_ID,
        resolutionEventId: RESOLUTION_EVENT_ID,
        sourceId: SOURCE_ID,
        targetAction: "apply_remote_version",
        winnerVersionId: OBSERVED_REMOTE_VERSION_ID,
        winnerBytes: null,
        winnerMediaType: null,
      },
    ]);
    expect(harness.repairState.completions).toEqual([
      { conflictId: CONFLICT_ID, resolutionEventId: RESOLUTION_EVENT_ID },
    ]);
  });

  it("applies a remote tombstone for keep_remote under edit_remote_delete", async () => {
    const harness = createHarness();
    harness.apiState.setDetail(
      buildDetail({ conflictKind: "edit_remote_delete", choices: ["keep_remote", "keep_local"] }),
    );
    await harness.controller.resolveKeepRemote(CONFLICT_ID);

    expect(harness.applierState.commands[0]?.targetAction).toBe("apply_remote_tombstone");
    expect(harness.applierState.commands[0]?.winnerBytes).toBeNull();
    expect(harness.applierState.commands[0]?.winnerVersionId).toBe(OBSERVED_REMOTE_VERSION_ID);
    expect(harness.repairState.parks[0]?.targetAction).toBe("apply_remote_tombstone");
  });

  it("applies a remote tombstone for the byteless delete_remote_edit keep_remote choice", async () => {
    const harness = createHarness();
    harness.apiState.setDetail(
      buildDetail({ conflictKind: "delete_remote_edit", candidateKind: "delete", choices: ["keep_remote"] }),
    );
    await harness.controller.resolveKeepRemote(CONFLICT_ID);

    expect(harness.applierState.commands[0]?.targetAction).toBe("apply_remote_tombstone");
  });

  it("refuses a choice the conflict's own choices do not admit, before any transport contact", async () => {
    const harness = createHarness();
    harness.apiState.setDetail(
      buildDetail({ conflictKind: "delete_remote_edit", candidateKind: "delete", choices: ["keep_remote"] }),
    );

    await expect(harness.controller.resolveKeepLocal(CONFLICT_ID)).rejects.toMatchObject({
      reason: "conflict_choice_unavailable",
    });
    await expect(
      harness.controller.resolveSaveMerged(CONFLICT_ID, "# merged"),
    ).rejects.toMatchObject({ reason: "conflict_choice_unavailable" });

    expect(harness.apiState.resolveInputs).toHaveLength(0);
    expect(harness.uploaderState.uploads).toHaveLength(0);
    expect(harness.diagnostics.reasons).toContain("conflict_choice_unavailable");
  });

  it("resolves keep_local by applying the resulting version", async () => {
    const harness = createHarness();
    harness.apiState.setDetail(buildDetail({ choices: ["keep_remote", "keep_local"] }));
    harness.apiState.setResolution(
      buildResolution("resolved", { resolutionKind: "keep_local", resultingVersionId: RESULTING_VERSION_ID }),
    );

    await harness.controller.resolveKeepLocal(CONFLICT_ID);

    expect(harness.applierState.commands[0]).toMatchObject({
      targetAction: "apply_resulting_version",
      winnerVersionId: RESULTING_VERSION_ID,
      winnerBytes: null,
    });
    expect(harness.repairState.parks[0]?.targetAction).toBe("apply_resulting_version");
  });

  it("parks before recording a winner that carries no version identity", async () => {
    const harness = createHarness();
    harness.apiState.setDetail(buildDetail({ choices: ["keep_remote", "keep_local"] }));
    harness.apiState.setResolution(
      buildResolution("resolved", { resolutionKind: "keep_local", resultingVersionId: null }),
    );

    await expect(harness.controller.resolveKeepLocal(CONFLICT_ID)).resolves.toEqual({
      kind: "local_apply_pending",
    });

    // The park lands first, so the failure records against the parked row.
    expect(harness.repairState.parks).toHaveLength(1);
    expect(harness.repairState.failures[0]?.safeReason).toBe("winner_download_failed");
    expect(harness.applierState.commands).toHaveLength(0);
    expect(harness.apiState.resolveInputs).toHaveLength(1);
  });

  it("resolves save_merged through the uploader port and applies the in-memory merged bytes", async () => {
    const harness = createHarness();
    harness.apiState.setResolution(
      buildResolution("resolved", { resolutionKind: "save_merged", resultingVersionId: RESULTING_VERSION_ID }),
    );
    const mergedText = `# merged\n${SECRET_CONTENT}`;

    const result = await harness.controller.resolveSaveMerged(CONFLICT_ID, mergedText);

    expect(result).toMatchObject({ kind: "resolved_and_applied" });
    expect(harness.uploaderState.uploads).toHaveLength(1);
    expect(harness.uploaderState.uploads[0]?.mediaType).toBe(CONFLICT_MERGE_UPLOAD_MEDIA_TYPE);
    expect(new TextDecoder().decode(harness.uploaderState.uploads[0]?.bytes ?? new Uint8Array())).toBe(
      mergedText,
    );
    expect(harness.apiState.resolveInputs[0]).toMatchObject({
      resolutionKind: "save_merged",
      verifiedCandidateObjectId: VERIFIED_CANDIDATE_OBJECT_ID,
    });
    expect(harness.applierState.commands[0]).toMatchObject({
      targetAction: "apply_resulting_version",
      winnerVersionId: RESULTING_VERSION_ID,
      winnerMediaType: CONFLICT_MERGE_UPLOAD_MEDIA_TYPE,
    });
    expect(
      new TextDecoder().decode(harness.applierState.commands[0]?.winnerBytes ?? new Uint8Array()),
    ).toBe(mergedText);
  });

  it("fails closed before any resolve when the verified candidate upload fails", async () => {
    const harness = createHarness();
    harness.uploaderState.failWith(new Error("upload transport failed"));

    await expect(harness.controller.resolveSaveMerged(CONFLICT_ID, "# merged")).rejects.toMatchObject({
      reason: "conflict_candidate_upload_failed",
    });
    expect(harness.apiState.resolveInputs).toHaveLength(0);
    expect(harness.repairState.parks).toHaveLength(0);
    expect(harness.diagnostics.reasons).toContain("conflict_candidate_upload_failed");
  });

  it("rejects an edited draft above the bounded proposal size without contacting any port", async () => {
    const harness = createHarness();
    const oversizedDraft = `# ${"m".repeat(MERGE_PROPOSAL_MAXIMUM_BYTES)}`;

    await expect(harness.controller.resolveSaveMerged(CONFLICT_ID, oversizedDraft)).rejects.toMatchObject({
      reason: "conflict_merge_bound_exceeded",
    });
    expect(harness.uploaderState.uploads).toHaveLength(0);
    expect(harness.apiState.resolveInputs).toHaveLength(0);
    expect(harness.diagnostics.reasons).toContain("conflict_merge_bound_exceeded");
  });

  it("returns the successor identity without applying when the server answers stale_successor", async () => {
    const harness = createHarness();
    harness.apiState.setResolution(buildResolution("stale_successor"));

    await expect(harness.controller.resolveKeepRemote(CONFLICT_ID)).resolves.toEqual({
      kind: "stale_successor",
      successorConflictId: SUCCESSOR_CONFLICT_ID,
    });
    expect(harness.repairState.parks).toHaveLength(0);
    expect(harness.applierState.commands).toHaveLength(0);
  });
});

// --- the resolution identity replay -----------------------------------------------------------------------

describe("conflict controller resolution identity (spec 7)", () => {
  it("replays the same resolution event identity after an ambiguous commit outcome", async () => {
    const harness = createHarness();
    harness.apiState.setResolveFailure(
      new ConflictApiError("commit_outcome_unknown", true),
    );

    await expect(harness.controller.resolveKeepRemote(CONFLICT_ID)).rejects.toBeInstanceOf(
      ConflictApiError,
    );
    await expect(harness.controller.resolveKeepRemote(CONFLICT_ID)).rejects.toBeInstanceOf(
      ConflictApiError,
    );

    expect(harness.identity.mintCount()).toBe(1);
    expect(harness.apiState.resolveInputs).toHaveLength(2);
    expect(harness.apiState.resolveInputs[0]).toMatchObject({
      resolutionEventId: RESOLUTION_EVENT_ID,
      idempotencyKey: IDEMPOTENCY_KEY,
    });
    expect(harness.apiState.resolveInputs[1]).toMatchObject({
      resolutionEventId: RESOLUTION_EVENT_ID,
      idempotencyKey: IDEMPOTENCY_KEY,
    });
  });

  it("mints a fresh identity after a terminal resolution failure", async () => {
    const harness = createHarness();
    harness.apiState.setResolveFailure(new ConflictApiError("conflict_state_invalid", false));

    await expect(harness.controller.resolveKeepRemote(CONFLICT_ID)).rejects.toBeInstanceOf(
      ConflictApiError,
    );
    harness.apiState.setResolveFailure(null);

    await harness.controller.resolveKeepRemote(CONFLICT_ID);

    expect(harness.identity.mintCount()).toBe(2);
    expect(harness.apiState.resolveInputs).toHaveLength(2);
  });
});

// --- the merge proposals ------------------------------------------------------------------------------------

describe("conflict controller merge proposals (spec 5.2.2)", () => {
  it("builds an editable proposal from verified text/Markdown evidence", async () => {
    const harness = createHarness();
    harness.apiState.setEvidence("base", markdownEvidence("# base\nshared\n"));
    harness.apiState.setEvidence("remote", markdownEvidence("# remote\nshared\n"));
    harness.apiState.setEvidence("candidate", markdownEvidence("# base\nshared\nlocal-edit\n"));

    const proposal = await harness.controller.buildMergeProposal(CONFLICT_ID);

    expect(proposal).toMatchObject({
      kind: "editable_merge",
      requiresUserReview: false,
      mediaType: "text/markdown",
    });
    expect((proposal as { mergedText?: string }).mergedText).toBe(
      "# remote\nshared\nlocal-edit\n",
    );
    expect(harness.apiState.evidenceRequests).toEqual([
      `${CONFLICT_ID}:base`,
      `${CONFLICT_ID}:remote`,
      `${CONFLICT_ID}:candidate`,
    ]);
  });

  it("answers manual choice required for binary media with no editor text", async () => {
    const harness = createHarness();
    harness.apiState.setEvidence("base", {
      bytes: new Uint8Array([0x00, 0x01]),
      mediaType: "application/octet-stream",
      sizeBytes: 2,
    });

    const proposal: ConflictMergeProposal = await harness.controller.buildMergeProposal(CONFLICT_ID);

    expect(proposal).toEqual({ kind: "manual_choice_required", reason: "media_unsupported" });
    expect(harness.diagnostics.reasons).toContain("conflict_media_unsupported");
  });

  it("answers manual choice required when an evidence role exceeds the merge bounds", async () => {
    const harness = createHarness();
    const oversized = `${"b".repeat(80)}\n`.repeat(4000);
    harness.apiState.setEvidence("base", markdownEvidence(oversized));
    harness.apiState.setEvidence("remote", markdownEvidence("# remote"));
    harness.apiState.setEvidence("candidate", markdownEvidence("# candidate"));

    const proposal = await harness.controller.buildMergeProposal(CONFLICT_ID);

    expect(proposal).toEqual({ kind: "manual_choice_required", reason: "merge_bound_exceeded" });
    expect(harness.diagnostics.reasons).toContain("conflict_merge_bound_exceeded");
  });

  it("answers manual choice required when an evidence download fails", async () => {
    const harness = createHarness();
    harness.apiState.setEvidence("base", markdownEvidence("# base"));

    const proposal = await harness.controller.buildMergeProposal(CONFLICT_ID);

    expect(proposal).toEqual({ kind: "manual_choice_required", reason: "evidence_role_unavailable" });
    expect(harness.diagnostics.reasons).toContain("conflict_evidence_unavailable");
  });

  it("answers manual choice required when the server admits no save_merged choice", async () => {
    const harness = createHarness();
    harness.apiState.setDetail(buildDetail({ choices: ["keep_remote"] }));

    const proposal = await harness.controller.buildMergeProposal(CONFLICT_ID);

    expect(proposal).toEqual({ kind: "manual_choice_required", reason: "merge_choice_not_admitted" });
    expect(harness.apiState.evidenceRequests).toEqual([]);
  });
});

// --- the bounded local-apply retry ------------------------------------------------------------------------------

describe("conflict controller pending apply retry (spec 5.2.6)", () => {
  function parkedRow(overrides?: Partial<PendingLocalApply>): PendingLocalApply {
    return {
      conflictId: CONFLICT_ID,
      resolutionEventId: RESOLUTION_EVENT_ID,
      targetAction: "apply_remote_version",
      safeReason: "vault_apply_failed",
      attemptCount: 1,
      nextEligibleRetryEpochMs: NOW_EPOCH_MS - 1,
      createdAtEpochMs: NOW_EPOCH_MS - 10_000,
      updatedAtEpochMs: NOW_EPOCH_MS - 1_000,
      ...overrides,
    };
  }

  it("retries a due pending apply without issuing another resolution", async () => {
    const harness = createHarness();
    harness.repairState.rows.set(
      CONFLICT_ID,
      parkedRow({ targetAction: "apply_resulting_version" }),
    );
    harness.apiState.setDetail(
      buildDetail({
        status: "resolved",
        resolutionKind: "keep_local",
        resolutionEventId: RESOLUTION_EVENT_ID,
        resultingVersionId: RESULTING_VERSION_ID,
        choices: [],
        closedAt: "2026-09-02T00:00:00Z",
      }),
    );

    await harness.controller.retryPendingLocalApplies();

    expect(harness.apiState.resolveInputs).toHaveLength(0);
    expect(harness.applierState.commands).toEqual([
      {
        conflictId: CONFLICT_ID,
        resolutionEventId: RESOLUTION_EVENT_ID,
        sourceId: SOURCE_ID,
        targetAction: "apply_resulting_version",
        winnerVersionId: RESULTING_VERSION_ID,
        winnerBytes: null,
        winnerMediaType: null,
      },
    ]);
    expect(harness.repairState.completions).toEqual([
      { conflictId: CONFLICT_ID, resolutionEventId: RESOLUTION_EVENT_ID },
    ]);
    expect(harness.repairState.rows.has(CONFLICT_ID)).toBe(false);
  });

  it("skips rows that are not yet eligible and rows whose attempts are exhausted", async () => {
    const harness = createHarness();
    harness.repairState.rows.set(
      "11111111-1111-4111-8111-111111111112",
      parkedRow({
        conflictId: "11111111-1111-4111-8111-111111111112",
        nextEligibleRetryEpochMs: NOW_EPOCH_MS + 60_000,
      }),
    );
    harness.repairState.rows.set(
      "11111111-1111-4111-8111-111111111113",
      parkedRow({
        conflictId: "11111111-1111-4111-8111-111111111113",
        attemptCount: CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS,
        nextEligibleRetryEpochMs: null,
      }),
    );

    await harness.controller.retryPendingLocalApplies();

    expect(harness.apiState.detailRequests).toHaveLength(0);
    expect(harness.applierState.commands).toHaveLength(0);
  });

  it("records exponential backoff on a retried apply failure", async () => {
    const harness = createHarness();
    harness.repairState.rows.set(CONFLICT_ID, parkedRow({ attemptCount: 1 }));
    harness.applierState.failWith(new CanonicalApplyError("vault_apply"));

    await harness.controller.retryPendingLocalApplies();

    expect(harness.repairState.failures).toEqual([
      {
        conflictId: CONFLICT_ID,
        resolutionEventId: RESOLUTION_EVENT_ID,
        safeReason: "vault_apply_failed",
        nowEpochMs: NOW_EPOCH_MS,
        nextEligibleRetryEpochMs: NOW_EPOCH_MS + 2 * CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS,
      },
    ]);
    expect(harness.repairState.completions).toHaveLength(0);
  });

  it("parks the final failed attempt and never retries a capped row again", async () => {
    const harness = createHarness();
    harness.repairState.rows.set(
      CONFLICT_ID,
      parkedRow({ attemptCount: CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS - 1 }),
    );
    harness.applierState.failWith(new CanonicalApplyError("vault_apply"));

    await harness.controller.retryPendingLocalApplies();

    expect(harness.diagnostics.reasons).toContain("conflict_apply_retry_exhausted");
    expect(harness.repairState.rows.get(CONFLICT_ID)?.attemptCount).toBe(
      CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS,
    );
    expect(harness.repairState.completions).toHaveLength(0);

    // The capped row is never eligible again: a second retry pass performs
    // no detail read and no apply attempt.
    await harness.controller.retryPendingLocalApplies();
    expect(harness.apiState.detailRequests).toHaveLength(1);
    expect(harness.applierState.commands).toHaveLength(1);
  });
});

// --- the diagnostics privacy invariant (spec 9) --------------------------------------------------------------------

describe("conflict controller diagnostics privacy (spec 9)", () => {
  it("records only closed reason tokens across failing flows — no raw content in diagnostics fixtures", async () => {
    const harness = createHarness();
    harness.apiState.setEvidence("base", markdownEvidence(SECRET_CONTENT));
    harness.apiState.setEvidence("remote", markdownEvidence(SECRET_CONTENT));
    harness.apiState.setEvidence("candidate", markdownEvidence(SECRET_CONTENT));

    // A binary-media proposal failure and a vault-apply parking failure.
    await harness.controller.buildMergeProposal(CONFLICT_ID);
    harness.applierState.failWith(new Error(`vault write of ${SECRET_CONTENT} failed`));
    await harness.controller.resolveKeepRemote(CONFLICT_ID);

    expect(harness.diagnostics.reasons.length).toBeGreaterThan(0);
    for (const reason of harness.diagnostics.reasons) {
      expect(CONFLICT_CONTROLLER_DIAGNOSTIC_REASONS).toContain(reason);
      expect(reason).not.toContain("secret");
    }
    const serialized = JSON.stringify(harness.diagnostics.reasons);
    expect(serialized).not.toContain("secret");
    expect(serialized).not.toContain(CONFLICT_ID);
  });
});
