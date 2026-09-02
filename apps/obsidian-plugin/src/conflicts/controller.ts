/**
 * The Conflict Inbox resolution controller (Child 8 spec 5.2, Task 8).
 *
 * The controller owns the resolution state machine; the modal owns no
 * domain logic. Every user choice follows the same canonical-first order
 * (spec 5.2.6): validate the choice against the server-admitted `choices`
 * array, commit the resolution over the wire, park the no-byte
 * `resolution_committed` fact the moment the canonical resolution commits,
 * then apply the returned canonical outcome through the injected atomic
 * applier seam. A Vault apply failure or a winner download failure parks
 * `local_apply_pending` with the closed safe reason and bounded retry
 * bookkeeping — the resolution is NEVER re-issued; the retry path re-reads
 * the conflict detail and re-applies only. A lost commit acknowledgement
 * replays the SAME resolution event identity (spec 7).
 *
 * The merged-draft journey rides the injected {@link VerifiedCandidateUploader}
 * port: no server wire today produces a `verified_candidate_object_id` for
 * an open conflict's resolution (Task 7 report §1), so unit tests drive
 * fakes and the real HTTP implementation lands with Task 10's server
 * surface. `keep_remote` and `keep_local` carry no candidate object and are
 * end-to-end real today. Merged/edit drafts live only in bounded ephemeral
 * memory until uploaded or discarded — never the journal (Task 7 made byte
 * storage unrepresentable; this module keeps it that way).
 *
 * The applier port is the plugin's existing atomic Vault writer +
 * echo-suppression seam: the Task 9 composition binds it over
 * `AtomicVaultWriterImpl` (staged, verified, narrowly replaced applies with
 * echo markers) — the controller never writes the Vault around it.
 *
 * Privacy (spec 9): every thrown failure is one closed reason with a
 * static message; the diagnostics sink receives closed reason tokens only.
 * No raw content, locator, path, digest or token ever reaches an error, a
 * diagnostic record or a durable row.
 */

import type { ConflictApi } from "./api";
import { ConflictApiError } from "./api";
import type {
  ConflictDetail,
  ConflictEvidenceRole,
  ConflictKind,
  ConflictLocalRepairAction,
  ConflictLocalRepairSafeReason,
  ConflictPage,
  ConflictResolution,
  ConflictResolutionKind,
  PendingLocalApply,
} from "./contracts";
import {
  MERGE_PROPOSAL_MAXIMUM_BYTES,
  computeBoundedThreeWayMerge,
  decodeConflictEvidenceText,
} from "./merge";
import type {
  CompleteLocalApplyInput,
  ParkPendingLocalApplyInput,
  RecordLocalApplyFailureInput,
} from "./repository";

// --- the closed diagnostics vocabulary -------------------------------------------------------------------

/**
 * The closed reason tokens the controller observes to its diagnostics
 * sink: one fire-and-forget token per failed or parked path, never a
 * message, a locator or any content. The first five are also the thrown
 * {@link ConflictControllerError} reasons; the last three name the parked
 * local-apply paths that never throw.
 */
export const CONFLICT_CONTROLLER_DIAGNOSTIC_REASONS = [
  "conflict_choice_unavailable",
  "conflict_evidence_unavailable",
  "conflict_media_unsupported",
  "conflict_text_undecodable",
  "conflict_merge_bound_exceeded",
  "conflict_candidate_upload_failed",
  "conflict_winner_download_failed",
  "conflict_vault_apply_failed",
  "conflict_apply_retry_exhausted",
] as const;

/** One closed controller diagnostics reason token. */
export type ConflictControllerDiagnosticReason =
  (typeof CONFLICT_CONTROLLER_DIAGNOSTIC_REASONS)[number];

/** The observe-only diagnostics sink (fire-and-forget, closed tokens only). */
export interface ConflictDiagnosticsSink {
  observeConflictFailure(reason: ConflictControllerDiagnosticReason): void;
}

/** The closed reason of one thrown controller rejection. */
export type ConflictControllerThrownReason = Extract<
  ConflictControllerDiagnosticReason,
  | "conflict_choice_unavailable"
  | "conflict_evidence_unavailable"
  | "conflict_merge_bound_exceeded"
  | "conflict_candidate_upload_failed"
>;

/** One thrown controller rejection: the closed reason and a static message. */
export class ConflictControllerError extends Error {
  readonly reason: ConflictControllerThrownReason;

  constructor(reason: ConflictControllerThrownReason) {
    super(`conflict controller failed: ${reason}`);
    this.name = "ConflictControllerError";
    this.reason = reason;
  }
}

// --- the verified candidate uploader port (Task 10 binds the real wire) -----------------------------------

/** One upload of edited merge bytes through the verified-candidate boundary. */
export interface VerifiedCandidateUpload {
  readonly conflictId: string;
  readonly bytes: Uint8Array;
  readonly mediaType: string;
}

/** The opaque receipt of one verified candidate upload. */
export interface VerifiedCandidateReceipt {
  readonly verifiedCandidateObjectId: string;
}

/**
 * The verified-candidate uploader PORT: turns the open conflict's identity,
 * its edited merge bytes and a media type descriptor into an opaque verified
 * object reference. Task 10 binds the real server surface (the conflict's
 * candidate route) behind this port; unit tests drive fakes.
 */
export interface VerifiedCandidateUploader {
  uploadVerifiedCandidate(upload: VerifiedCandidateUpload): Promise<VerifiedCandidateReceipt>;
}

/**
 * The canonical media type of a save_merged candidate: the server's
 * resolve grammar requires exactly `text/markdown` (Task 6 choice matrix).
 */
export const CONFLICT_MERGE_UPLOAD_MEDIA_TYPE = "text/markdown";

// --- the canonical outcome applier port (the atomic Vault writer seam) -------------------------------------

/** The closed stage at which one canonical apply failed. */
export type CanonicalApplyFailureStage = "winner_download" | "vault_apply";

/** One canonical apply failure: the closed stage and a static message. */
export class CanonicalApplyError extends Error {
  readonly stage: CanonicalApplyFailureStage;

  constructor(stage: CanonicalApplyFailureStage) {
    super(`canonical apply failed: ${stage}`);
    this.name = "CanonicalApplyError";
    this.stage = stage;
  }
}

/** One canonical outcome apply command the controller owes the Vault. */
export interface CanonicalOutcomeApplyCommand {
  readonly conflictId: string;
  readonly resolutionEventId: string;
  readonly sourceId: string | null;
  readonly targetAction: ConflictLocalRepairAction;
  /** The winner source version; null only for a tombstone target action. */
  readonly winnerVersionId: string | null;
  /** The winner bytes still in bounded memory, when available; null means the applier downloads the winner itself. */
  readonly winnerBytes: Uint8Array | null;
  readonly winnerMediaType: string | null;
}

/**
 * The canonical outcome applier PORT: applies the resolved winner to the
 * Vault atomically with echo suppression. The Task 9 composition binds
 * this over `AtomicVaultWriterImpl` (stage + verify + narrow replace, or
 * the trash path for tombstones) with the journal's echo markers — the
 * controller never writes the Vault around this seam.
 */
export interface CanonicalOutcomeApplier {
  applyCanonicalOutcome(command: CanonicalOutcomeApplyCommand): Promise<void>;
}

// --- the resolution identity -------------------------------------------------------------------------------

/** One fresh canonical resolution identity (event id + idempotency key). */
export interface ConflictResolutionIdentity {
  readonly resolutionEventId: string;
  readonly idempotencyKey: string;
}

/** The identity minter port; the default mints canonical UUIDs. */
export type ConflictResolutionIdentityMinter = () => ConflictResolutionIdentity;

/**
 * The default identity minter over the platform `crypto.randomUUID` — the
 * exact canonical lowercase-hyphenated UUID grammar the server's
 * idempotency key accepts.
 */
export function createUuidConflictResolutionIdentityMinter(): ConflictResolutionIdentityMinter {
  return () => ({
    resolutionEventId: crypto.randomUUID(),
    idempotencyKey: crypto.randomUUID(),
  });
}

/** The epoch clock port (milliseconds). */
export type ConflictEpochClock = () => number;

// --- the bounded local-apply retry policy (Task 7 deferral) --------------------------------------------------

/** The closed cap on local-apply attempts before the repair needs human attention. */
export const CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS = 5;

/** The base delay of the exponential backoff between failed local applies. */
export const CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS = 1_000;

/** The ceiling of the exponential backoff between failed local applies. */
export const CONFLICT_LOCAL_APPLY_RETRY_MAXIMUM_DELAY_MS = 60_000;

/**
 * The delay before the next eligible retry after `failedAttemptCount`
 * failed attempts: exponential from the base, capped at the ceiling.
 */
export function conflictLocalApplyRetryDelayMs(failedAttemptCount: number): number {
  if (failedAttemptCount < 1) {
    return CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS;
  }
  const exponential = CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS * 2 ** (failedAttemptCount - 1);
  return Math.min(exponential, CONFLICT_LOCAL_APPLY_RETRY_MAXIMUM_DELAY_MS);
}

// --- the repair store port ------------------------------------------------------------------------------------

/**
 * The narrow durable repair-store seam the controller depends on:
 * exactly the five `ConflictRepository` operations the state machine
 * uses, so a fake satisfies the port in tests while the Task 9
 * composition hands over the real repository.
 */
export interface ConflictRepairStore {
  readPendingLocalApply(conflictId: string): PendingLocalApply | null;
  readPendingLocalApplies(): readonly PendingLocalApply[];
  parkPendingLocalApply(input: ParkPendingLocalApplyInput): Promise<void>;
  recordLocalApplyFailure(input: RecordLocalApplyFailureInput): Promise<void>;
  completeLocalApply(input: CompleteLocalApplyInput): Promise<void>;
}

// --- the merge proposals ----------------------------------------------------------------------------------------

/** The closed reason a merge proposal cannot be offered for editing. */
export const CONFLICT_MERGE_UNAVAILABLE_REASONS = [
  "merge_bound_exceeded",
  "media_unsupported",
  "text_undecodable",
  "evidence_role_unavailable",
  "merge_choice_not_admitted",
] as const;

/** One closed merge-unavailable reason. */
export type ConflictMergeUnavailableReason = (typeof CONFLICT_MERGE_UNAVAILABLE_REASONS)[number];

/** One merge proposal: an editable bounded merge, or the safe manual-choice state. */
export type ConflictMergeProposal =
  | {
      readonly kind: "editable_merge";
      readonly mergedText: string;
      readonly requiresUserReview: boolean;
      readonly conflictingHunkCount: number;
      readonly mediaType: string;
    }
  | { readonly kind: "manual_choice_required"; readonly reason: ConflictMergeUnavailableReason };

// --- the resolution command results ------------------------------------------------------------------------------

/** The closed outcome of one explicit resolution command. */
export type ConflictResolutionCommandResult =
  | { readonly kind: "resolved_and_applied"; readonly resolution: ConflictResolution }
  | { readonly kind: "local_apply_pending" }
  | { readonly kind: "stale_successor"; readonly successorConflictId: string };

// --- the controller -----------------------------------------------------------------------------------------------

/** The Conflict Inbox behavior surface the modal drives. */
export interface ConflictController {
  listOpenConflicts(): Promise<ConflictPage>;
  getConflictDetail(conflictId: string): Promise<ConflictDetail>;
  /** Build one merge proposal on demand from the verified evidence (bounded memory only). */
  buildMergeProposal(conflictId: string): Promise<ConflictMergeProposal>;
  resolveKeepRemote(conflictId: string): Promise<ConflictResolutionCommandResult>;
  resolveKeepLocal(conflictId: string): Promise<ConflictResolutionCommandResult>;
  /** Upload the edited merge through the uploader port, resolve, then apply. */
  resolveSaveMerged(conflictId: string, editedText: string): Promise<ConflictResolutionCommandResult>;
  /** Retry every due parked local apply — local application only, never another resolution. */
  retryPendingLocalApplies(): Promise<void>;
}

export interface ConflictControllerOptions {
  readonly api: ConflictApi;
  readonly repairStore: ConflictRepairStore;
  readonly uploader: VerifiedCandidateUploader;
  readonly applier: CanonicalOutcomeApplier;
  readonly mintIdentity?: ConflictResolutionIdentityMinter | undefined;
  readonly clock?: ConflictEpochClock | undefined;
  readonly diagnostics?: ConflictDiagnosticsSink | null | undefined;
}

/** One winner derivation of a committed resolution. */
interface WinnerPlan {
  readonly targetAction: ConflictLocalRepairAction;
  readonly winnerVersionId: string | null;
  readonly winnerBytes: Uint8Array | null;
  readonly winnerMediaType: string | null;
}

/** The kinds whose keep_remote winner is the remote tombstone. */
function isRemoteTombstoneKind(kind: ConflictKind): boolean {
  return kind === "edit_remote_delete" || kind === "delete_remote_edit";
}

/** Derive the winner plan of a just-committed resolution (bytes only for an in-memory merged draft). */
function planWinnerOfResolution(
  detail: ConflictDetail,
  resolution: ConflictResolution,
  mergedBytes: Uint8Array | null,
): WinnerPlan {
  if (resolution.resolutionKind === "keep_remote") {
    if (isRemoteTombstoneKind(detail.conflictKind)) {
      return {
        targetAction: "apply_remote_tombstone",
        winnerVersionId: detail.observedRemoteVersionId,
        winnerBytes: null,
        winnerMediaType: null,
      };
    }
    return {
      targetAction: "apply_remote_version",
      winnerVersionId: detail.observedRemoteVersionId,
      winnerBytes: null,
      winnerMediaType: null,
    };
  }
  return {
    targetAction: "apply_resulting_version",
    winnerVersionId: resolution.resultingVersionId,
    winnerBytes: mergedBytes,
    winnerMediaType: mergedBytes === null ? null : CONFLICT_MERGE_UPLOAD_MEDIA_TYPE,
  };
}

/**
 * Derive the winner plan of a parked retry: the durable target action owns
 * the intent (it was pinned at park time), and the re-read conflict detail
 * supplies the winner identity — `observed_remote_version_id` for a remote
 * winner, `resulting_version_id` for the published resulting version (a
 * detail whose resulting version is not yet visible parks a
 * winner-download retry instead of guessing).
 */
function planWinnerOfParkedRetry(
  detail: ConflictDetail,
  parked: PendingLocalApply,
): WinnerPlan {
  switch (parked.targetAction) {
    case "apply_remote_tombstone":
      return {
        targetAction: "apply_remote_tombstone",
        winnerVersionId: detail.observedRemoteVersionId,
        winnerBytes: null,
        winnerMediaType: null,
      };
    case "apply_remote_version":
      return {
        targetAction: "apply_remote_version",
        winnerVersionId: detail.observedRemoteVersionId,
        winnerBytes: null,
        winnerMediaType: null,
      };
    case "apply_resulting_version":
      return {
        targetAction: "apply_resulting_version",
        winnerVersionId: detail.resultingVersionId,
        winnerBytes: null,
        winnerMediaType: null,
      };
  }
}

/** The safe reason of one canonical apply failure stage. */
function safeReasonOfApplyStage(stage: CanonicalApplyFailureStage): ConflictLocalRepairSafeReason {
  return stage === "winner_download" ? "winner_download_failed" : "vault_apply_failed";
}

/** The diagnostics token of one canonical apply failure stage. */
function diagnosticOfApplyStage(stage: CanonicalApplyFailureStage): ConflictControllerDiagnosticReason {
  return stage === "winner_download" ? "conflict_winner_download_failed" : "conflict_vault_apply_failed";
}

/** The closed failure stage of one thrown applier rejection (fail safe onto vault_apply). */
function applyStageOf(error: unknown): CanonicalApplyFailureStage {
  return error instanceof CanonicalApplyError && error.stage === "winner_download"
    ? "winner_download"
    : "vault_apply";
}

/**
 * Build the Conflict Inbox resolution controller. The controller holds no
 * durable bytes: drafts exist only inside one command's bounded memory,
 * and every durable fact flows through the no-byte repair store.
 */
export function createConflictController(
  options: ConflictControllerOptions,
): ConflictController {
  const { api, repairStore, uploader, applier } = options;
  const mintIdentity = options.mintIdentity ?? createUuidConflictResolutionIdentityMinter();
  const clock = options.clock ?? Date.now;
  const diagnostics = options.diagnostics ?? null;
  /** The in-flight resolution identities replayed after an ambiguous commit. */
  const inFlightIdentities = new Map<string, ConflictResolutionIdentity>();

  function observe(reason: ConflictControllerDiagnosticReason): void {
    diagnostics?.observeConflictFailure(reason);
  }

  /** Record one failed local apply attempt with the bounded backoff (or the exhausted park). */
  async function recordApplyFailure(
    conflictId: string,
    resolutionEventId: string,
    stage: CanonicalApplyFailureStage,
  ): Promise<void> {
    observe(diagnosticOfApplyStage(stage));
    const parked = repairStore.readPendingLocalApply(conflictId);
    const failedAttemptCount = (parked?.attemptCount ?? 0) + 1;
    const nowEpochMs = clock();
    if (failedAttemptCount >= CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS) {
      // The attempt cap is the gate: the row keeps its bookkeeping shape
      // (Task 7's contract) but the retry loop never picks a capped row
      // again — the repair needs human attention from here.
      observe("conflict_apply_retry_exhausted");
    }
    await repairStore.recordLocalApplyFailure({
      conflictId,
      resolutionEventId,
      safeReason: safeReasonOfApplyStage(stage),
      nowEpochMs,
      nextEligibleRetryEpochMs:
        nowEpochMs + conflictLocalApplyRetryDelayMs(failedAttemptCount),
    });
  }

  /** Apply the parked winner, then complete the repair or park the failure. */
  async function applyParkedWinner(
    detail: ConflictDetail,
    resolution: ConflictResolution,
    plan: WinnerPlan,
  ): Promise<ConflictResolutionCommandResult> {
    try {
      await applier.applyCanonicalOutcome({
        conflictId: detail.conflictId,
        resolutionEventId: resolution.resolutionEventId,
        sourceId: detail.sourceId,
        targetAction: plan.targetAction,
        winnerVersionId: plan.winnerVersionId,
        winnerBytes: plan.winnerBytes,
        winnerMediaType: plan.winnerMediaType,
      });
    } catch (error) {
      await recordApplyFailure(detail.conflictId, resolution.resolutionEventId, applyStageOf(error));
      return { kind: "local_apply_pending" };
    }
    await repairStore.completeLocalApply({
      conflictId: detail.conflictId,
      resolutionEventId: resolution.resolutionEventId,
    });
    return { kind: "resolved_and_applied", resolution };
  }

  /** Validate the choice, commit the resolution, then apply the canonical outcome. */
  async function resolveChoice(
    conflictId: string,
    resolutionKind: ConflictResolutionKind,
    editedText: string | null,
  ): Promise<ConflictResolutionCommandResult> {
    const detail = await api.getConflict(conflictId);
    if (!(detail.choices as readonly string[]).includes(resolutionKind)) {
      observe("conflict_choice_unavailable");
      throw new ConflictControllerError("conflict_choice_unavailable");
    }

    let verifiedCandidateObjectId: string | null = null;
    let mergedBytes: Uint8Array | null = null;
    if (resolutionKind === "save_merged") {
      if (editedText === null) {
        throw new ConflictControllerError("conflict_choice_unavailable");
      }
      const encodedDraft = new TextEncoder().encode(editedText);
      if (encodedDraft.byteLength > MERGE_PROPOSAL_MAXIMUM_BYTES) {
        observe("conflict_merge_bound_exceeded");
        throw new ConflictControllerError("conflict_merge_bound_exceeded");
      }
      try {
        const receipt = await uploader.uploadVerifiedCandidate({
          conflictId,
          bytes: encodedDraft,
          mediaType: CONFLICT_MERGE_UPLOAD_MEDIA_TYPE,
        });
        verifiedCandidateObjectId = receipt.verifiedCandidateObjectId;
        mergedBytes = encodedDraft;
      } catch {
        observe("conflict_candidate_upload_failed");
        throw new ConflictControllerError("conflict_candidate_upload_failed");
      }
    }

    const identity = inFlightIdentities.get(conflictId) ?? mintIdentity();
    inFlightIdentities.set(conflictId, identity);
    let resolution: ConflictResolution;
    try {
      resolution = await api.resolveConflict({
        conflictId,
        resolutionEventId: identity.resolutionEventId,
        idempotencyKey: identity.idempotencyKey,
        resolutionKind,
        reviewedRemoteVersionId: detail.observedRemoteVersionId,
        verifiedCandidateObjectId,
      });
    } catch (error) {
      // An ambiguous commit replays the SAME identity; a terminal failure
      // kills this identity so the next explicit command mints fresh.
      if (!(error instanceof ConflictApiError) || !error.canRetry) {
        inFlightIdentities.delete(conflictId);
      }
      throw error;
    }
    inFlightIdentities.delete(conflictId);

    if (resolution.outcome === "stale_successor") {
      return {
        kind: "stale_successor",
        successorConflictId: resolution.successorConflictId ?? conflictId,
      };
    }
    const plan = planWinnerOfResolution(detail, resolution, mergedBytes);
    // Park the committed fact the moment the canonical resolution commits
    // (the pre-apply crash window of spec 5.2.6): every later failure —
    // including a winner that cannot be identified — records against this
    // parked row, never a second resolution.
    await repairStore.parkPendingLocalApply({
      conflictId,
      resolutionEventId: resolution.resolutionEventId,
      targetAction: plan.targetAction,
      safeReason: "resolution_committed",
      nowEpochMs: clock(),
    });
    if (plan.winnerVersionId === null && plan.targetAction !== "apply_remote_tombstone") {
      // A content winner without an identity cannot be applied; the owed
      // work stays parked under the winner-download safe reason.
      await recordApplyFailure(conflictId, resolution.resolutionEventId, "winner_download");
      return { kind: "local_apply_pending" };
    }
    return applyParkedWinner(detail, resolution, plan);
  }

  return {
    async listOpenConflicts(): Promise<ConflictPage> {
      return api.listConflicts();
    },

    async getConflictDetail(conflictId: string): Promise<ConflictDetail> {
      return api.getConflict(conflictId);
    },

    async buildMergeProposal(conflictId: string): Promise<ConflictMergeProposal> {
      const detail = await api.getConflict(conflictId);
      if (!(detail.choices as readonly string[]).includes("save_merged")) {
        return { kind: "manual_choice_required", reason: "merge_choice_not_admitted" };
      }
      const roles: readonly ConflictEvidenceRole[] = ["base", "remote", "candidate"];
      const texts = new Map<ConflictEvidenceRole, string>();
      let candidateMediaType = CONFLICT_MERGE_UPLOAD_MEDIA_TYPE;
      for (const role of roles) {
        let evidence;
        try {
          evidence = await api.downloadConflictEvidence({ conflictId, role });
        } catch {
          observe("conflict_evidence_unavailable");
          return { kind: "manual_choice_required", reason: "evidence_role_unavailable" };
        }
        const decoded = decodeConflictEvidenceText(evidence.bytes, evidence.mediaType);
        if (decoded.kind !== "text") {
          if (decoded.kind === "bytes_exceeded") {
            observe("conflict_merge_bound_exceeded");
            return { kind: "manual_choice_required", reason: "merge_bound_exceeded" };
          }
          if (decoded.kind === "text_undecodable") {
            observe("conflict_text_undecodable");
            return { kind: "manual_choice_required", reason: "text_undecodable" };
          }
          observe("conflict_media_unsupported");
          return { kind: "manual_choice_required", reason: "media_unsupported" };
        }
        texts.set(role, decoded.text);
        if (role === "candidate") {
          candidateMediaType = evidence.mediaType;
        }
      }
      const baseText = texts.get("base") ?? "";
      const remoteText = texts.get("remote") ?? "";
      const localText = texts.get("candidate") ?? "";
      const merge = computeBoundedThreeWayMerge(baseText, remoteText, localText);
      if (merge.outcome === "bound_exceeded" || merge.mergedText === null) {
        observe("conflict_merge_bound_exceeded");
        return { kind: "manual_choice_required", reason: "merge_bound_exceeded" };
      }
      return {
        kind: "editable_merge",
        mergedText: merge.mergedText,
        requiresUserReview: merge.requiresUserReview,
        conflictingHunkCount: merge.conflictingHunkCount,
        mediaType: candidateMediaType,
      };
    },

    async resolveKeepRemote(conflictId: string): Promise<ConflictResolutionCommandResult> {
      return resolveChoice(conflictId, "keep_remote", null);
    },

    async resolveKeepLocal(conflictId: string): Promise<ConflictResolutionCommandResult> {
      return resolveChoice(conflictId, "keep_local", null);
    },

    async resolveSaveMerged(
      conflictId: string,
      editedText: string,
    ): Promise<ConflictResolutionCommandResult> {
      return resolveChoice(conflictId, "save_merged", editedText);
    },

    async retryPendingLocalApplies(): Promise<void> {
      const parkedRows = repairStore.readPendingLocalApplies();
      const nowEpochMs = clock();
      for (const parked of parkedRows) {
        if (
          parked.attemptCount >= CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS ||
          parked.nextEligibleRetryEpochMs === null ||
          parked.nextEligibleRetryEpochMs > nowEpochMs
        ) {
          continue;
        }
        let detail: ConflictDetail;
        try {
          detail = await api.getConflict(parked.conflictId);
        } catch {
          await recordApplyFailure(parked.conflictId, parked.resolutionEventId, "winner_download");
          continue;
        }
        const plan = planWinnerOfParkedRetry(detail, parked);
        if (plan.winnerVersionId === null && plan.targetAction !== "apply_remote_tombstone") {
          await recordApplyFailure(parked.conflictId, parked.resolutionEventId, "winner_download");
          continue;
        }
        try {
          await applier.applyCanonicalOutcome({
            conflictId: parked.conflictId,
            resolutionEventId: parked.resolutionEventId,
            sourceId: detail.sourceId,
            targetAction: plan.targetAction,
            winnerVersionId: plan.winnerVersionId,
            winnerBytes: null,
            winnerMediaType: null,
          });
        } catch (error) {
          await recordApplyFailure(parked.conflictId, parked.resolutionEventId, applyStageOf(error));
          continue;
        }
        await repairStore.completeLocalApply({
          conflictId: parked.conflictId,
          resolutionEventId: parked.resolutionEventId,
        });
      }
    },
  };
}
