/**
 * The manifest action reconciler (device cursor and manifest
 * reconciliation, task 11, spec 12.1, 12.4, 7.3, 9.1).
 *
 * One reconciliation freezes the observation generation behind a repair
 * barrier (adopting an existing apply-blocker barrier instead of minting a
 * second one), starts or exactly resumes the server's manifest run,
 * uploads the stable ordered capture page by page — persisting every
 * accepted run/page/action receipt through the Task 8 repository — then
 * applies the planner's deterministic actions under the mandatory
 * pre-apply rechecks. Upload actions terminalize by durably recording or
 * reauthorizing an outbound journal event under the barrier; download and
 * tombstone actions apply through the Task 10 remote-apply state machine
 * with contiguous synthetic event sequences that never pass the run
 * checkpoint — a canonical-only download (no manifest entry) applies as a
 * synthetic create at the checkpoint locator its action wire carries
 * (task 11b), and a wire that carries none settles fail-closed with the
 * closed invalid-state reason; conflict/no-change/excluded actions persist
 * their blocker evidence without any network mutation. Once every action
 * is terminal-safe, the exact server completion is recorded, the local
 * cursors become the checkpoint C, the barrier and the
 * `reconcile_required` flag clear, and every outbound row — the ones
 * observed after G plus the planner uploads — becomes dispatchable again.
 *
 * A one-hour expiry or a policy advance discards ONLY the temporary run
 * progress and starts one new checkpoint-bound run inside the same call;
 * every local edit survives untouched. Every closed reconcile failure
 * surfaces exactly ONE `reconcile_failure` observation with its exact
 * stage (`start`, `page`, `finalize`, `actions`, `complete`) and closed
 * reason token through the Task 7 diagnostics facade — no `catch` returns
 * a retry without the preceding observation.
 *
 * Like the other device-sync modules this file imports no Node.js,
 * Electron or Obsidian file-system adapter API at module load time, so it
 * stays loadable on mobile.
 */

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { RepairActionRecheckInput, RepairActionRecheckOutcome } from "../journal/capture";
import type { JournalStoreErrorReason } from "../journal/sqlite-database";
import { JOURNAL_STORE_ERROR_REASONS } from "../journal/sqlite-database";
import type {
  ManifestActionProgressRecord,
  ManifestPageProgressRecord,
} from "../journal/repository";
import type { JournalCaptureResult, JournalRepository } from "../journal/repository";
import { DeviceSyncApiError, classifyDeviceSyncFailure } from "./api";
import type {
  AppendManifestPageInput,
  CompleteManifestInput,
  DeviceCursorReceipt,
  DeviceSyncEvent,
  FinalizeManifestInput,
  ManifestAction,
  ManifestActionPage,
  ManifestActionsInput,
  ManifestEntryInput,
  ManifestPageReceipt,
  ManifestRunReceipt,
  StartManifestInput,
  VerifiedDownload,
} from "./api";
import type {
  CompleteLocalRepair,
  DeviceSyncDiagnostics,
  DeviceSyncReason,
  DeviceSyncRepository,
  DeviceSyncState,
  ReconcileFailureStage,
} from "./contracts";
import type { ManifestCapture, ManifestEntry, ManifestPageDigestRecord } from "./manifest-capture";
import { computeManifestFinalDigest } from "./manifest-capture";
import type { RemoteEventApplier, VerifiedDownloader } from "./remote-event-applier";

// --- the reconcile contracts (brief task 11) ---------------------------------------------------------

/** Why a full reconciliation is requested (spec 9.1): closed vocabulary. */
export type ReconcileReason =
  | "onboarding"
  | "sqlite_rebuilt"
  | "cursor_gap"
  | "history_compacted"
  | "unknown_event"
  | "local_invariant"
  | "explicit_repair"
  | "periodic";

/**
 * The closed barrier reason each reconcile reason binds the repair
 * barrier to (the Task 8 barrier carries a `DeviceSyncReason`, spec 12.1):
 * the repair-family token that best names why local state must be
 * re-proven against the server. Frozen mapping, pinned row by row by
 * test.
 */
export const RECONCILE_BARRIER_REASONS: Readonly<Record<ReconcileReason, DeviceSyncReason>> = {
  onboarding: "device_manifest_state_invalid",
  sqlite_rebuilt: "journal_image_invalid",
  cursor_gap: "device_cursor_gap",
  history_compacted: "device_event_unavailable",
  unknown_event: "device_event_integrity_failed",
  local_invariant: "device_manifest_state_invalid",
  explicit_repair: "device_manifest_state_invalid",
  periodic: "device_manifest_state_invalid",
};

/** The outcome of one reconciliation run. */
export type ManifestReconcileOutcome =
  | { readonly kind: "completed"; readonly checkpointSequence: number }
  | { readonly kind: "retry"; readonly reason: DeviceSyncReason }
  | { readonly kind: "blocked"; readonly reason: DeviceSyncReason };

/** The manifest reconciler surface (brief task 11). */
export interface ManifestReconciler {
  reconcile(reason: ReconcileReason): Promise<ManifestReconcileOutcome>;
  resume(): Promise<ManifestReconcileOutcome>;
}

// --- the ports ----------------------------------------------------------------------------------------

/** The five manifest wire operations the reconciler drives (Task 9 client subset). */
export interface ManifestReconcilerWireApi {
  startManifest(input: StartManifestInput): Promise<ManifestRunReceipt>;
  appendManifestPage(input: AppendManifestPageInput): Promise<ManifestPageReceipt>;
  finalizeManifest(input: FinalizeManifestInput): Promise<ManifestRunReceipt>;
  listManifestActions(input: ManifestActionsInput): Promise<ManifestActionPage>;
  completeManifest(input: CompleteManifestInput): Promise<DeviceCursorReceipt>;
}

/** The durable outcome of one repair upload admission (spec 12.4). */
export type RepairUploadAdmission = "recorded" | "already_current" | "refused";

/**
 * The journal-facing surface the reconciler needs beyond the Task 8
 * repository: the repair completion (cursor advance, barrier clear,
 * `reconcile_required` clear, echo-marker sweep), the temporary-run
 * discard of an expired/policy-stale run, the durable page/action
 * progress reads of the exact resume, the pre-apply action target
 * recheck and the repair upload admission. `JournalRepository` plus
 * `JournalCapture` satisfy it through
 * {@link createManifestReconcilerJournal}.
 */
export interface ManifestReconcilerJournal {
  completeDeviceSyncRepair(input: CompleteLocalRepair): Promise<void>;
  discardActiveManifestRun(): Promise<void>;
  readManifestPageProgress(): readonly ManifestPageProgressRecord[];
  readManifestActionProgress(): readonly ManifestActionProgressRecord[];
  recheckManifestActionTarget(input: RepairActionRecheckInput): Promise<RepairActionRecheckOutcome>;
  admitRepairUpload(input: { readonly normalizedLocator: string }): Promise<RepairUploadAdmission>;
}

/** The capture-side admission surface of the repair journal port. */
export interface RepairAdmissionCapture {
  admitForRepair(normalizedPath: string): Promise<JournalCaptureResult | null>;
  recheckForRepair(input: RepairActionRecheckInput): Promise<RepairActionRecheckOutcome>;
}

/**
 * Compose the reconciler's journal port over the real journal repository
 * and capture coordinator: the SQL-backed progress/completion/discard
 * methods come from {@link JournalRepository}, the target recheck and the
 * upload admission from {@link RepairAdmissionCapture} (JournalCapture).
 */
export function createManifestReconcilerJournal(options: {
  readonly repository: JournalRepository;
  readonly capture: RepairAdmissionCapture;
}): ManifestReconcilerJournal {
  const repository = options.repository;
  const capture = options.capture;
  return {
    completeDeviceSyncRepair: (input) => repository.completeDeviceSyncRepair(input),
    discardActiveManifestRun: () => repository.discardActiveManifestRun(),
    readManifestPageProgress: () => repository.readManifestPageProgress(),
    readManifestActionProgress: () => repository.readManifestActionProgress(),
    recheckManifestActionTarget: (input) => capture.recheckForRepair(input),
    admitRepairUpload: async (input) => {
      const admission = await capture.admitForRepair(input.normalizedLocator);
      if (admission === null) {
        return "already_current";
      }
      if (admission.outcome === "capture_refused") {
        return "refused";
      }
      // event_recorded: durably created; event_coalesced: reauthorized.
      return "recorded";
    },
  };
}

export interface ManifestReconcilerOptions {
  readonly repository: DeviceSyncRepository;
  readonly api: ManifestReconcilerWireApi;
  readonly capture: ManifestCapture;
  readonly journal: ManifestReconcilerJournal;
  readonly applier: RemoteEventApplier;
  readonly diagnostics: DeviceSyncDiagnostics;
  /**
   * The verified-download seam used to prove a download action's exact
   * final fingerprint BEFORE the synthetic apply event is prepared (the
   * Task 10 applier requires the final fingerprint durably up front).
   */
  readonly downloader: VerifiedDownloader;
  /** Bounded action-page size; defaults to 100. */
  readonly actionPageLimit?: number | undefined;
}

// --- the closed failure plumbing -----------------------------------------------------------------------

/** The closed run-level steps: an outcome, or the request to restart the run checkpoint-bound. */
type RunStep =
  | ManifestReconcileOutcome
  | { readonly kind: "restart-run"; readonly stage: ReconcileFailureStage; readonly reason: DeviceSyncReason };

/** The closed reasons that invalidate the unfinished run itself (spec 7.3, 9.1). */
const RUN_RESTART_REASONS: ReadonlySet<string> = new Set([
  "device_manifest_expired",
  "device_manifest_policy_advanced",
]);

/** The placeholder committed time of a synthetic manifest apply event. */
const SYNTHETIC_EVENT_COMMITTED_AT = "1970-01-01T00:00:00Z";

interface ClosedFailure {
  readonly reason: DeviceSyncReason;
  readonly retryable: boolean;
  readonly requestId: string | null;
  readonly wireErrorCode: string | null;
}

/** The closed store reasons a repository throw may legitimately carry. */
const CLOSED_JOURNAL_STORE_ERROR_REASONS: ReadonlySet<string> = new Set<string>(
  JOURNAL_STORE_ERROR_REASONS,
);

/** The closed store reason of one repository throw, when it carries one. */
function storeReasonOf(error: unknown): JournalStoreErrorReason | null {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = (error as { reason?: unknown }).reason;
    // A foreign string is never adopted as a closed token: only the frozen
    // store-reason vocabulary may reach an outcome or a trail observation.
    if (typeof reason === "string" && CLOSED_JOURNAL_STORE_ERROR_REASONS.has(reason)) {
      return reason as JournalStoreErrorReason;
    }
  }
  return null;
}

/** Classify any thrown value onto the closed device-sync vocabulary with its correlation. */
function closedFailureOf(error: unknown): ClosedFailure {
  if (error instanceof DeviceSyncApiError) {
    return {
      reason: error.reason,
      retryable: error.retryable,
      requestId: error.requestId,
      wireErrorCode: error.wireErrorCode,
    };
  }
  const storeReason = storeReasonOf(error);
  if (storeReason !== null) {
    return { reason: storeReason, retryable: false, requestId: null, wireErrorCode: null };
  }
  const failure = classifyDeviceSyncFailure(error);
  return {
    reason: failure.reason,
    retryable: failure.retryable,
    requestId: failure.correlation?.requestId ?? null,
    wireErrorCode: failure.correlation?.wireErrorCode ?? null,
  };
}

/**
 * The deterministic event identity of one manifest action's synthetic
 * apply: a one-way digest of the run identity and action index shaped as
 * a UUID, so an exact resume after a crash mid-apply re-prepares the SAME
 * durable remote-apply row instead of contradicting it.
 */
async function deterministicActionEventId(
  manifestRunId: string,
  actionIndex: number,
): Promise<string> {
  const digest = await sha256Hex(
    new TextEncoder().encode(`manifest-action-event/v1:${manifestRunId}:${actionIndex}`),
  );
  const versionNibble = "4";
  const variantNibble = ((Number.parseInt(digest[16] ?? "0", 16) & 0x3) | 0x8).toString(16);
  return [
    digest.slice(0, 8),
    digest.slice(8, 12),
    `${versionNibble}${digest.slice(13, 16)}`,
    `${variantNibble}${digest.slice(17, 20)}`,
    digest.slice(20, 32),
  ].join("-");
}

// --- the reconciler ------------------------------------------------------------------------------------

/**
 * Build the manifest action reconciler. One instance holds no bytes and
 * no credential; every durable step persists through the injected
 * repositories and every failure surfaces exactly one closed
 * `reconcile_failure` observation.
 */
export function createManifestReconciler(options: ManifestReconcilerOptions): ManifestReconciler {
  const { repository, api, capture, journal, applier, diagnostics } = options;
  const downloader = options.downloader;
  const actionPageLimit = options.actionPageLimit ?? 100;

  /** Report exactly one closed observation, then map onto the run step. */
  function runFailure(stage: ReconcileFailureStage, error: unknown): RunStep {
    const failure = closedFailureOf(error);
    diagnostics.reconcileFailure(stage, failure.reason, {
      requestId: failure.requestId,
      wireErrorCode: failure.wireErrorCode,
    });
    if (RUN_RESTART_REASONS.has(failure.reason)) {
      return { kind: "restart-run", stage, reason: failure.reason };
    }
    return failure.retryable
      ? { kind: "retry", reason: failure.reason }
      : { kind: "blocked", reason: failure.reason };
  }

  /** One already-diagnosed closed verdict (the observation preceded this call). */
  function blocked(stage: ReconcileFailureStage, reason: DeviceSyncReason): RunStep {
    diagnostics.reconcileFailure(stage, reason);
    return { kind: "blocked", reason };
  }

  /** One diagnosed closed invalidation that restarts the run checkpoint-bound. */
  function restartRun(stage: ReconcileFailureStage, reason: DeviceSyncReason): RunStep {
    diagnostics.reconcileFailure(stage, reason);
    return { kind: "restart-run", stage, reason };
  }

  /** Collapse one run step onto the public outcome (a restart request outside the run loop is blocked). */
  function asOutcome(step: RunStep): ManifestReconcileOutcome {
    return step.kind === "restart-run" ? { kind: "blocked", reason: step.reason } : step;
  }

  function wireEntries(entries: readonly ManifestEntry[]): readonly ManifestEntryInput[] {
    return entries.map((entry) => ({
      localEntryId: entry.localEntryId,
      normalizedLocator: entry.normalizedLocator,
      fingerprint: entry.fingerprint,
      observationGeneration: entry.observationGeneration,
      ...(entry.knownSourceId === null ? {} : { knownSourceId: entry.knownSourceId }),
      ...(entry.knownVersionId === null ? {} : { knownVersionId: entry.knownVersionId }),
    }));
  }

  // --- one action ---------------------------------------------------------------------------------

  async function applyAction(
    manifestRunId: string,
    checkpointSequence: number,
    action: ManifestAction,
    entriesByLocalId: ReadonlyMap<string, ManifestEntry>,
    terminalActionIndexes: Set<number>,
  ): Promise<RunStep | null> {
    if (terminalActionIndexes.has(action.actionIndex)) {
      // Exact resume of an already-settled action: its durable outcome
      // stands and nothing re-runs.
      return null;
    }
    try {
      await repository.recordManifestAction({
        manifestRunId,
        actionIndex: action.actionIndex,
        actionKind: action.actionKind,
        outcome: "received",
        reason: action.reason,
      });
    } catch (error) {
      return runFailure("actions", error);
    }

    const terminal = async (reason: DeviceSyncReason | null): Promise<RunStep | null> => {
      try {
        await repository.recordManifestAction({
          manifestRunId,
          actionIndex: action.actionIndex,
          actionKind: action.actionKind,
          outcome: "terminal_safe",
          reason,
        });
      } catch (error) {
        return runFailure("actions", error);
      }
      if (reason !== null) {
        // Fix round 1 I3: the completion legitimately discards the run's
        // page/action progress rows, so one closed observation keeps the
        // settle reason readable on the trail afterwards — the canonical
        // `reconcile_failure` lane at the `actions` stage, no new kind.
        diagnostics.reconcileFailure("actions", reason);
      }
      terminalActionIndexes.add(action.actionIndex);
      return null;
    };

    if (
      action.actionKind === "conflict" ||
      action.actionKind === "no_change" ||
      action.actionKind === "excluded"
    ) {
      // Planner blockers persist their mapping/blocker evidence with no
      // network mutation (spec 12.4).
      return terminal(action.reason);
    }

    const entry =
      action.localEntryId === null ? null : (entriesByLocalId.get(action.localEntryId) ?? null);
    // Only a download may place through the wire's checkpoint locator; a
    // locator riding any other kind is never an entry substitute.
    const canonicalOnlyLocator =
      entry === null && action.actionKind === "download" ? action.checkpointLocator : null;
    if (entry === null && canonicalOnlyLocator === null) {
      if (action.actionKind === "download") {
        // Defensive fail-closed for a legacy/erroneous wire (task 11b): a
        // canonical-only download without its checkpoint locator can never
        // place bytes. The closed invalid-state reason settles it durably —
        // never a guess, and never the identity-ambiguous token, which
        // stays reserved for genuinely ambiguous identity proof.
        return terminal("device_manifest_state_invalid");
      }
      // A planned entry this capture cannot prove is a genuine identity
      // conflict, never a guess.
      return terminal("device_manifest_identity_ambiguous");
    }

    if (entry !== null) {
      let recheck: RepairActionRecheckOutcome;
      try {
        recheck = await journal.recheckManifestActionTarget({
          normalizedLocator: entry.normalizedLocator,
          entryFingerprint: entry.fingerprint,
          actionKind:
            action.actionKind === "upload"
              ? "upload"
              : action.actionKind === "apply_tombstone"
                ? "apply_tombstone"
                : "download",
        });
      } catch (error) {
        return runFailure("actions", error);
      }
      if (recheck.kind === "blocked") {
        // A stale action becomes a durable conflict/repair blocker without
        // invalidating any unrelated safe action (spec 12.4).
        return terminal(recheck.reason);
      }

      if (action.actionKind === "upload") {
        let admission: RepairUploadAdmission;
        try {
          admission = await journal.admitRepairUpload({
            normalizedLocator: entry.normalizedLocator,
          });
        } catch (error) {
          return runFailure("actions", error);
        }
        if (admission === "refused") {
          // The journal durably refused new outbound rows (its spec-6.4 soft
          // limits): the action cannot terminalize — the run stays blocked
          // with the barrier and progress retained for the next attempt.
          return blocked("actions", "journal_mutation_failed");
        }
        // recorded (created or reauthorized) and already_current both
        // terminalize: the outbound intent is durably proven.
        return terminal(null);
      }
    }

    if (action.sourceId === null) {
      return terminal("device_manifest_identity_ambiguous");
    }

    let state: DeviceSyncState;
    try {
      state = repository.readState();
    } catch (error) {
      return runFailure("actions", error);
    }
    const eventSequence = state.appliedSequence + 1;
    if (eventSequence > checkpointSequence) {
      // The local apply lattice cannot fit below the run checkpoint: the
      // run stays blocked (the one-hour expiry later starts a fresh,
      // further-ahead checkpoint that fits).
      return blocked("actions", "device_cursor_gap");
    }

    let eventId: string;
    try {
      eventId = await deterministicActionEventId(manifestRunId, action.actionIndex);
    } catch (error) {
      return runFailure("actions", error);
    }
    let event: DeviceSyncEvent;
    if (action.actionKind === "apply_tombstone") {
      // Reaching the synthetic events with a tombstone always implies a
      // proven entry (the guard above settled every entry-less action);
      // the re-guard keeps the invariant explicit for the type checker.
      if (entry === null) {
        return terminal("device_manifest_identity_ambiguous");
      }
      event = {
        eventId,
        eventSequence,
        operation: "deleted",
        sourceId: action.sourceId,
        originDeviceId: null,
        baseVersionId: null,
        currentVersionId: null,
        baseFingerprint: entry.fingerprint,
        currentFingerprint: null,
        priorLocator: entry.normalizedLocator,
        resultingLocator: null,
        tombstoneId: action.sourceTombstoneId,
        committedAt: SYNTHETIC_EVENT_COMMITTED_AT,
      };
    } else {
      if (action.sourceVersionId === null) {
        return terminal("device_manifest_identity_ambiguous");
      }
      // The download's exact final fingerprint is proven by the verified
      // download BEFORE the synthetic apply event is prepared (the Task 10
      // applier requires the final fingerprint durably up front).
      let verified: VerifiedDownload;
      try {
        verified = await downloader({
          sourceId: action.sourceId,
          sourceVersionId: action.sourceVersionId,
        });
      } catch (error) {
        return runFailure("actions", error);
      }
      const verifiedFingerprint = {
        sha256: verified.declaredSha256,
        sizeBytes: verified.sizeBytes,
        mediaType: verified.mediaType,
      };
      if (entry !== null) {
        // The per-entry catch-up download: the entry's own locator is where
        // the device already keeps the file, so the synthetic update proves
        // the pinned base bytes before replacing them.
        event = {
          eventId,
          eventSequence,
          operation: "updated",
          sourceId: action.sourceId,
          originDeviceId: null,
          baseVersionId: entry.knownVersionId,
          currentVersionId: action.sourceVersionId,
          baseFingerprint: entry.fingerprint,
          currentFingerprint: verifiedFingerprint,
          // The wire's update shape: the resulting locator is the content
          // target and no prior locator exists (an update changes no path).
          priorLocator: null,
          resultingLocator: entry.normalizedLocator,
          tombstoneId: null,
          committedAt: SYNTHETIC_EVENT_COMMITTED_AT,
        };
      } else {
        // The canonical-only download (task 11b): no entry exists and the
        // target's absence is the expected pre-mutation state, so a
        // synthetic create places the verified bytes at the wire's
        // checkpoint locator. The Task 10 state machine proves the target
        // unoccupied before any mutation — an untracked occupant settles as
        // a durable conflict instead of being clobbered (the entry recheck
        // above deliberately does not run: it would read the same absent
        // target as a stale entry).
        event = {
          eventId,
          eventSequence,
          operation: "created",
          sourceId: action.sourceId,
          originDeviceId: null,
          baseVersionId: null,
          currentVersionId: action.sourceVersionId,
          baseFingerprint: null,
          currentFingerprint: verifiedFingerprint,
          priorLocator: null,
          resultingLocator: canonicalOnlyLocator,
          tombstoneId: null,
          committedAt: SYNTHETIC_EVENT_COMMITTED_AT,
        };
      }
    }

    try {
      await applier.apply(event);
    } catch (error) {
      return runFailure("actions", error);
    }
    return terminal(null);
  }

  // --- one checkpoint-bound run -------------------------------------------------------------------

  async function runOne(barrierGeneration: number): Promise<RunStep> {
    let runReceipt: ManifestRunReceipt;
    try {
      runReceipt = await api.startManifest({ clientObservationGeneration: barrierGeneration });
    } catch (error) {
      return runFailure("start", error);
    }
    const manifestRunId = runReceipt.manifestRunId;
    const checkpointSequence = runReceipt.checkpointSequence;
    if (runReceipt.clientObservationGeneration !== barrierGeneration) {
      return blocked("start", "device_manifest_state_invalid");
    }
    let boundState: DeviceSyncState;
    try {
      boundState = repository.readState();
    } catch (error) {
      return runFailure("start", error);
    }
    if (
      (boundState.activeManifestRunId !== null &&
        boundState.activeManifestRunId !== manifestRunId) ||
      (boundState.manifestCheckpointSequence !== null &&
        boundState.manifestCheckpointSequence !== checkpointSequence)
    ) {
      return blocked("start", "device_manifest_state_invalid");
    }

    // --- capture and upload the ordered pages, exactly resuming the durable progress.
    let recordedPages: readonly ManifestPageProgressRecord[];
    try {
      recordedPages = journal.readManifestPageProgress();
    } catch (error) {
      return runFailure("page", error);
    }
    const recordedByNumber = new Map(recordedPages.map((page) => [page.pageNumber, page]));
    const entriesByLocalId = new Map<string, ManifestEntry>();
    const pages: ManifestPageDigestRecord[] = [];
    let lastPage: ManifestPageDigestRecord | null = null;
    let serverNextPageNumber = runReceipt.nextPageNumber;
    try {
      for await (const page of capture.capturePages(barrierGeneration)) {
        for (const entry of page.entries) {
          entriesByLocalId.set(entry.localEntryId, entry);
        }
        const record: ManifestPageDigestRecord = {
          pageNumber: page.pageNumber,
          entryCount: page.entries.length,
          pageDigest: page.pageDigest,
        };
        const recorded = recordedByNumber.get(page.pageNumber);
        if (recorded !== undefined) {
          if (
            recorded.entryCount !== record.entryCount ||
            recorded.pageDigest !== record.pageDigest
          ) {
            // The re-capture diverged from the durable page evidence: the
            // Vault moved beyond what the action rechecks absorb. Discard
            // the temporary run progress and start a fresh checkpoint-bound
            // run whose capture reflects the newer bytes.
            return restartRun("page", "device_manifest_page_replay_mismatch");
          }
          pages.push(recorded);
          lastPage = recorded;
          continue;
        }
        if (page.pageNumber < serverNextPageNumber) {
          // The server accepted this page inside the crash window before
          // the local receipt landed: record the exact evidence now.
          try {
            await repository.recordManifestPage({
              manifestRunId,
              pageNumber: record.pageNumber,
              entryCount: record.entryCount,
              pageDigest: record.pageDigest,
              checkpointSequence,
              finalDigest: null,
            });
          } catch (error) {
            return runFailure("page", error);
          }
          pages.push(record);
          lastPage = record;
          continue;
        }
        let receipt: ManifestPageReceipt;
        try {
          receipt = await api.appendManifestPage({
            manifestRunId,
            pageNumber: page.pageNumber,
            entries: wireEntries(page.entries),
            pageDigest: page.pageDigest,
          });
        } catch (error) {
          return runFailure("page", error);
        }
        if (
          receipt.acceptedEntryCount !== page.entries.length ||
          receipt.nextPageNumber !== page.pageNumber + 1 ||
          receipt.manifestRunId !== manifestRunId
        ) {
          return blocked("page", "device_manifest_state_invalid");
        }
        serverNextPageNumber = receipt.nextPageNumber;
        try {
          await repository.recordManifestPage({
            manifestRunId,
            pageNumber: record.pageNumber,
            entryCount: record.entryCount,
            pageDigest: record.pageDigest,
            checkpointSequence,
            finalDigest: null,
          });
        } catch (error) {
          return runFailure("page", error);
        }
        pages.push(record);
        lastPage = record;
      }
    } catch (error) {
      // Capture-stage closed failures — the 100,000-entry run cap, an
      // unreadable store — surface exactly like every other reconcile
      // failure, never as a raw escape out of reconcile()/resume().
      return runFailure("page", error);
    }

    // --- finalize with the total count and the canonical final digest.
    const totalEntryCount = pages.reduce((sum, page) => sum + page.entryCount, 0);
    let finalDigest: string;
    try {
      finalDigest = await computeManifestFinalDigest(pages);
    } catch (error) {
      return runFailure("page", error);
    }
    if (lastPage !== null) {
      // The final digest persists as the last page's exact replay receipt
      // (the Task 8 repository binds it with the identical page evidence).
      try {
        await repository.recordManifestPage({
          manifestRunId,
          pageNumber: lastPage.pageNumber,
          entryCount: lastPage.entryCount,
          pageDigest: lastPage.pageDigest,
          checkpointSequence,
          finalDigest,
        });
      } catch (error) {
        return runFailure("page", error);
      }
    }
    let plannedRun: ManifestRunReceipt;
    try {
      plannedRun = await api.finalizeManifest({ manifestRunId, totalEntryCount, finalDigest });
    } catch (error) {
      return runFailure("finalize", error);
    }
    if (plannedRun.manifestRunId !== manifestRunId || plannedRun.entryCount !== totalEntryCount) {
      return blocked("finalize", "device_manifest_digest_mismatch");
    }

    // --- apply every planned action to terminal-safe (spec 12.4).
    let settledActionIndexes: readonly number[];
    try {
      settledActionIndexes = journal
        .readManifestActionProgress()
        .filter((progress) => progress.outcome === "terminal_safe")
        .map((progress) => progress.actionIndex);
    } catch (error) {
      return runFailure("actions", error);
    }
    const terminalActionIndexes = new Set<number>(settledActionIndexes);
    let afterActionIndex: number | undefined = undefined;
    let hasMoreActions = true;
    while (hasMoreActions) {
      let actionPage: ManifestActionPage;
      try {
        actionPage = await api.listManifestActions({
          manifestRunId,
          ...(afterActionIndex === undefined ? {} : { afterActionIndex }),
          limit: actionPageLimit,
        });
      } catch (error) {
        return runFailure("actions", error);
      }
      if (actionPage.manifestRunId !== manifestRunId) {
        return blocked("actions", "device_manifest_state_invalid");
      }
      if (actionPage.actions.length === 0) {
        hasMoreActions = false;
        break;
      }
      for (const action of actionPage.actions) {
        const step = await applyAction(
          manifestRunId,
          checkpointSequence,
          action,
          entriesByLocalId,
          terminalActionIndexes,
        );
        if (step !== null) {
          return step;
        }
      }
      afterActionIndex = actionPage.actions[actionPage.actions.length - 1]?.actionIndex;
      hasMoreActions = actionPage.hasMore;
    }

    // --- record the exact server completion, then the local completion.
    let cursorReceipt: DeviceCursorReceipt;
    try {
      cursorReceipt = await api.completeManifest({ manifestRunId, finalDigest });
    } catch (error) {
      return runFailure("complete", error);
    }
    if (cursorReceipt.acknowledgedSequence < checkpointSequence) {
      return blocked("complete", "device_manifest_state_invalid");
    }
    try {
      await journal.completeDeviceSyncRepair({
        manifestRunId,
        checkpointSequence,
        barrierGeneration,
      });
    } catch (error) {
      return runFailure("complete", error);
    }
    return { kind: "completed", checkpointSequence };
  }

  /**
   * Run checkpoint-bound runs under one barrier: a run invalidated by the
   * one-hour expiry or a policy advance discards ONLY its temporary
   * progress — every local edit survives — and exactly one fresh run
   * starts inside this call; a second invalidation surfaces as blocked.
   */
  async function runCheckpointBoundRuns(barrierGeneration: number): Promise<ManifestReconcileOutcome> {
    for (let runAttempt = 0; runAttempt < 2; runAttempt += 1) {
      const step = await runOne(barrierGeneration);
      if (step.kind !== "restart-run") {
        return step;
      }
      if (runAttempt === 1) {
        // The fresh run hit the same closed invalidation: the reason was
        // already observed at its stage; stop instead of looping.
        return { kind: "blocked", reason: step.reason };
      }
      try {
        await journal.discardActiveManifestRun();
      } catch (error) {
        // The failing stage already observed its own failure; the discard
        // failure's observation joins it at the same stage, and a restart
        // request outside the run loop collapses onto blocked.
        return asOutcome(runFailure(step.stage, error));
      }
    }
    // Unreachable: the loop returns on its second attempt.
    return { kind: "blocked", reason: "device_manifest_state_invalid" };
  }

  async function reconcile(reason: ReconcileReason): Promise<ManifestReconcileOutcome> {
    let barrierGeneration: number | null;
    try {
      barrierGeneration = repository.readState().barrierGeneration;
    } catch (error) {
      return asOutcome(runFailure("start", error));
    }
    if (barrierGeneration === null) {
      try {
        barrierGeneration = await repository.nextObservationGeneration();
        await repository.startRepairBarrier({
          generation: barrierGeneration,
          reason: RECONCILE_BARRIER_REASONS[reason],
        });
      } catch (error) {
        return asOutcome(runFailure("start", error));
      }
    }
    // An existing barrier (an apply-blocker or a previous interrupted
    // run's) is adopted: exactly one barrier ever guards a reconciliation.
    return runCheckpointBoundRuns(barrierGeneration);
  }

  async function resume(): Promise<ManifestReconcileOutcome> {
    let barrierGeneration: number | null;
    try {
      barrierGeneration = repository.readState().barrierGeneration;
    } catch (error) {
      return asOutcome(runFailure("start", error));
    }
    if (barrierGeneration === null) {
      return asOutcome(blocked("start", "device_manifest_state_invalid"));
    }
    return runCheckpointBoundRuns(barrierGeneration);
  }

  return { reconcile, resume };
}
