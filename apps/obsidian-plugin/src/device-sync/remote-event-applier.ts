/**
 * The crash-safe remote event applier (device cursor and manifest
 * reconciliation, task 10, spec 8.1, 10.3, 11).
 *
 * One apply walks the durable state machine: persist `prepared` AND the
 * exact echo marker BEFORE any Vault mutation, download the verified
 * bytes, stage + verify + narrowly replace through the
 * {@link AtomicVaultWriter}, persist `vault_mutated`, clean the retained
 * rollback sibling best-effort, then terminalize the local outcome and
 * the cursor in ONE journal generation. A retryable failure never
 * advances the cursor — the durable row resumes after restart.
 *
 * Every catch site surfaces exactly ONE closed `apply_failure`
 * observation with its exact stage (`prepare`, `download`,
 * `verify_temp`, `vault_mutation`, `verify_final`, `local_commit`,
 * `recovery`, `trash`) before returning a conflict, starting a repair
 * barrier or rethrowing. A {@link DeviceSyncApiError} from the wire
 * client was already reported by its own lane, so the applier never
 * doubles it.
 *
 * Like the other device-sync modules this file imports no Node.js,
 * Electron or Obsidian file-system adapter API at module load time, so it
 * stays loadable on mobile.
 */

import type { FrozenFingerprint } from "../journal/contracts";
import type { JournalStoreErrorReason } from "../journal/sqlite-database";
import type { VerifiedDownload } from "./api";
import { DeviceSyncApiError, classifyDeviceSyncFailure } from "./api";
import type { DeviceSyncEvent, DownloadSourceVersionInput } from "./api";
import type {
  ApplyFailureStage,
  DeviceSyncDiagnostics,
  DeviceSyncReason,
  DeviceSyncRepository,
  EchoMarker,
  PreparedRemoteApply,
  RemoteApplyOperation,
  TerminalDeviceEvent,
  TerminalDeviceEventOutcome,
} from "./contracts";
import { AtomicVaultWriterError } from "./atomic-vault-writer";
import type { AtomicVaultWriter, RemoteApplyRecovery } from "./atomic-vault-writer";

// --- the port and options (brief task 10) -----------------------------------------------------------

export interface RemoteEventApplier {
  recoverUnfinishedApply(): Promise<void>;
  /**
   * Settle one repeatedly refused apply (the 2026-09-03 apply-wedge deep
   * fix): the caller PROVED a prior durable attempt of this same event
   * failed with the same closed vault-failure reason. The settle
   * reconciles the leftover Vault state through the crash-safe recovery,
   * then frees the lattice sequence the failed apply holds — a
   * proven-clean intent is abandoned (the sequence stays reusable), a
   * refusal the recovery itself meets closes with the cursor-advancing
   * conflict verdict — so the caller's manifest run continues past the
   * refused placement with its closed reason surfaced exactly once.
   */
  settleVaultFailedApply(
    event: DeviceSyncEvent,
    reason: DeviceSyncReason,
  ): Promise<TerminalDeviceEvent>;
  apply(event: DeviceSyncEvent, options?: RemoteEventApplyOptions): Promise<TerminalDeviceEvent>;
}

/**
 * The optional inputs of one apply. `verifiedDownload` carries a download
 * of the event's current version whose digest a caller already verified
 * (the manifest reconciler's fingerprint proof): when its declared
 * SHA-256 matches the event's current fingerprint, the apply reuses these
 * exact bytes instead of downloading the same version again.
 */
export interface RemoteEventApplyOptions {
  readonly verifiedDownload?: VerifiedDownload | null;
}

/** The verified-download seam: the Task 9 wire client's `downloadSourceVersion`. */
export type VerifiedDownloader = (input: DownloadSourceVersionInput) => Promise<VerifiedDownload>;

export interface RemoteEventApplierOptions {
  readonly repository: DeviceSyncRepository;
  readonly writer: AtomicVaultWriter;
  readonly downloader: VerifiedDownloader;
  readonly diagnostics: DeviceSyncDiagnostics;
}

// --- the operand mapping -------------------------------------------------------------------------------

interface EventLocators {
  readonly priorLocator: string | null;
  readonly targetLocator: string | null;
}

function locatorsOf(event: DeviceSyncEvent): EventLocators {
  switch (event.operation) {
    case "created":
    case "restored":
      return { priorLocator: null, targetLocator: event.resultingLocator };
    case "updated":
      return { priorLocator: event.resultingLocator, targetLocator: null };
    case "renamed":
    case "moved":
      return { priorLocator: event.priorLocator, targetLocator: event.resultingLocator };
    case "deleted":
      return { priorLocator: event.priorLocator, targetLocator: null };
  }
}

/** Whether the operation stages temporary content bytes. */
function isContentOperation(operation: DeviceSyncEvent["operation"]): boolean {
  return operation === "created" || operation === "updated" || operation === "restored";
}

/** The closed store reason of one repository throw, when it carries one. */
function storeReasonOf(error: unknown): JournalStoreErrorReason | null {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = (error as { reason?: unknown }).reason;
    if (typeof reason === "string") {
      return reason as JournalStoreErrorReason;
    }
  }
  return null;
}

/** The conflict-family reasons a writer failure settles as a durable conflict. */
function isConflictReason(reason: DeviceSyncReason): boolean {
  return reason === "device_manifest_target_occupied" || reason === "device_manifest_local_diverged";
}

// --- the applier --------------------------------------------------------------------------------------

/**
 * Build the durable remote event applier. The staging token is the
 * server event id — deterministic, opaque and idempotent, so an exact
 * re-prepare after a retryable failure always matches the durable row.
 */
export function createRemoteEventApplier(options: RemoteEventApplierOptions): RemoteEventApplier {
  const { repository, writer, downloader, diagnostics } = options;

  /** Map one thrown repository failure onto its closed barrier/store reason. */
  function closedStoreReasonOf(error: unknown): DeviceSyncReason {
    const storeReason = storeReasonOf(error);
    if (storeReason !== null) {
      try {
        const barrierReason = repository.readState().barrierReason;
        if (barrierReason !== null) {
          return barrierReason;
        }
      } catch {
        // The state read is best-effort context; the store reason stands.
      }
      return storeReason;
    }
    return "server_error";
  }

  function throwMapped(reason: DeviceSyncReason, retryable: boolean): never {
    throw new DeviceSyncApiError(reason, retryable);
  }

  async function terminalize(
    eventSequence: number,
    outcome: TerminalDeviceEventOutcome,
    reason: DeviceSyncReason | null,
    stage: ApplyFailureStage,
  ): Promise<TerminalDeviceEvent> {
    try {
      await repository.terminalizeEvent({ eventSequence, outcome, reason });
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure(stage, mapped);
      throwMapped(mapped, false);
    }
    return { eventSequence, outcome, reason };
  }

  async function apply(
    event: DeviceSyncEvent,
    options?: RemoteEventApplyOptions,
  ): Promise<TerminalDeviceEvent> {
    const state = repository.readState();
    if (event.eventSequence <= state.appliedSequence) {
      // Idempotent replay of an already-settled event: nothing is re-run,
      // and the replay answers with the durable row's settled outcome
      // while it is still locally known (a conflict closes with its
      // closed reason, a delete as a handled tombstone).
      const settled = repository.readUnfinishedApply();
      if (settled !== null && settled.eventSequence === event.eventSequence) {
        if (settled.safeErrorCode !== null) {
          return {
            eventSequence: event.eventSequence,
            outcome: "conflict",
            reason: settled.safeErrorCode,
          };
        }
        if (settled.operation === "deleted") {
          return { eventSequence: event.eventSequence, outcome: "tombstone_handled", reason: null };
        }
      }
      return { eventSequence: event.eventSequence, outcome: "applied", reason: null };
    }
    if (event.eventSequence !== state.appliedSequence + 1) {
      diagnostics.applyFailure("prepare", "device_cursor_gap");
      throwMapped("device_cursor_gap", false);
    }

    const locators = locatorsOf(event);
    const finalFingerprint: FrozenFingerprint | null = event.currentFingerprint;
    const baseFingerprint: FrozenFingerprint | null = event.baseFingerprint;
    const needsDownload = isContentOperation(event.operation);
    const contentTargetLocator =
      event.operation === "updated" ? locators.priorLocator : locators.targetLocator;
    const isLocatorOperation = event.operation === "renamed" || event.operation === "moved";
    const missingOperand =
      (event.operation === "deleted" && locators.priorLocator === null) ||
      (isLocatorOperation && (locators.priorLocator === null || locators.targetLocator === null)) ||
      (needsDownload &&
        (contentTargetLocator === null ||
          event.currentVersionId === null ||
          finalFingerprint === null)) ||
      (isLocatorOperation && finalFingerprint === null && baseFingerprint === null);
    if (missingOperand) {
      diagnostics.applyFailure("prepare", "device_event_unavailable");
      throwMapped("device_event_unavailable", false);
    }

    const tempToken = needsDownload ? event.eventId : null;
    const prepared: PreparedRemoteApply = {
      eventSequence: event.eventSequence,
      eventId: event.eventId,
      sourceId: event.sourceId,
      operation: event.operation,
      priorLocator: locators.priorLocator,
      targetLocator: locators.targetLocator,
      baseFingerprint,
      finalFingerprint: event.operation === "deleted" ? null : finalFingerprint,
      tempToken,
      rollbackToken: null,
    };
    const marker: EchoMarker = {
      eventSequence: event.eventSequence,
      sourceId: event.sourceId,
      operation: event.operation,
      priorLocator: locators.priorLocator,
      targetLocator: locators.targetLocator,
      finalFingerprint: event.operation === "deleted" ? null : finalFingerprint,
    };

    // Stage: prepare — the durable intent and the echo marker land BEFORE
    // any Vault mutation.
    try {
      await repository.prepareRemoteApply(prepared);
      await repository.recordEchoMarker(marker);
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("prepare", mapped);
      throwMapped(mapped, false);
    }

    // Stage: download — the verified bytes of the event's current version.
    let bytes: Uint8Array | null = null;
    if (needsDownload && event.currentVersionId !== null) {
      const predownloaded =
        options?.verifiedDownload != null &&
        options.verifiedDownload.declaredSha256 === event.currentFingerprint?.sha256
          ? options.verifiedDownload
          : null;
      if (predownloaded !== null) {
        // The caller proved this exact download already (the reconciler's
        // fingerprint proof): reusing the same bytes keeps the digest
        // proof and the applied bytes one object instead of two downloads
        // whose bytes could theoretically diverge.
        bytes = predownloaded.bytes;
      } else {
        try {
          const download = await downloader({
            sourceId: event.sourceId,
            sourceVersionId: event.currentVersionId,
          });
          bytes = download.bytes;
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            // The wire lane already reported exactly one observation.
            throw error;
          }
          const failure = classifyDeviceSyncFailure(error);
          diagnostics.applyFailure("download", failure.reason, failure.correlation);
          throw new DeviceSyncApiError(
            failure.reason,
            failure.retryable,
            failure.correlation?.requestId ?? null,
            failure.correlation?.wireErrorCode ?? null,
          );
        }
      }
    }

    // Stage: verify_temp / vault_mutation / verify_final / trash — inside
    // the writer, each surfaced with its own closed stage.
    const renameProof = finalFingerprint ?? baseFingerprint;
    try {
      if (event.operation === "deleted") {
        await writer.trash({
          eventSequence: event.eventSequence,
          priorLocator: locators.priorLocator ?? "",
          baseFingerprint,
        });
      } else if (isLocatorOperation && renameProof !== null) {
        await writer.renameOrMove({
          eventSequence: event.eventSequence,
          operation: event.operation,
          priorLocator: locators.priorLocator ?? "",
          targetLocator: locators.targetLocator ?? "",
          expectedFinalFingerprint: renameProof,
        });
      } else {
        if (contentTargetLocator === null || finalFingerprint === null || bytes === null) {
          // Unreachable after the operand guard; kept for the type checker.
          diagnostics.applyFailure("prepare", "device_event_unavailable");
          throwMapped("device_event_unavailable", false);
        }
        await writer.stageAndReplace({
          eventSequence: event.eventSequence,
          operation: event.operation as "created" | "updated" | "restored",
          targetLocator: contentTargetLocator,
          expectedFinalFingerprint: finalFingerprint,
          baseFingerprint: event.operation === "updated" ? baseFingerprint : null,
          bytes,
          tempToken: event.eventId,
        });
      }
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      if (error instanceof AtomicVaultWriterError) {
        diagnostics.applyFailure(error.stage, error.reason);
        if (!error.retryable && (isConflictReason(error.reason) || error.restoredToBase)) {
          // A proven conflict or a restored-to-base failure settles
          // durably: the cursor advances over the closed reason.
          return terminalize(event.eventSequence, "conflict", error.reason, "local_commit");
        }
        throwMapped(error.reason, error.retryable);
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("vault_mutation", mapped);
      throwMapped(mapped, false);
    }

    // Stage: persist the vault mutation proof (a repository failure here
    // surfaces at the vault_mutation stage; the Vault effect stands and
    // recovery re-proves it).
    try {
      await repository.transitionRemoteApply({
        eventSequence: event.eventSequence,
        state: "vault_mutated",
        rollbackToken: tempToken,
      });
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("vault_mutation", mapped);
      throwMapped(mapped, false);
    }

    // Best-effort cleanup of the retained rollback sibling through the
    // writer's recovery path; a failure surfaces at the trash stage and
    // never fails the durable apply.
    const mutatedRow = repository.readUnfinishedApply();
    if (mutatedRow !== null && mutatedRow.eventSequence === event.eventSequence) {
      try {
        const recovery = await writer.recover(mutatedRow);
        if (recovery.kind === "blocked") {
          // The durable proof no longer verifies — surface the closed
          // reason; the already-mutated apply still completes.
          diagnostics.applyFailure("recovery", recovery.reason);
        } else if (recovery.cleanupFailure !== null) {
          diagnostics.applyFailure("trash", recovery.cleanupFailure);
        }
      } catch (error) {
        // The apply is already durably mutated; a failed cleanup is a
        // leftover hidden sibling, never data loss — but it still
        // surfaces exactly one closed trash-stage observation.
        const cleanupReason =
          error instanceof AtomicVaultWriterError ? error.reason : "device_apply_vault_failed";
        diagnostics.applyFailure("trash", cleanupReason);
      }
    }

    // Stage: local_commit — the terminal outcome and the cursor advance
    // land in one journal generation.
    const outcome: TerminalDeviceEventOutcome =
      event.operation === "deleted" ? "tombstone_handled" : "applied";
    return terminalize(event.eventSequence, outcome, null, "local_commit");
  }

  async function recoverUnfinishedApply(): Promise<void> {
    const operation: RemoteApplyOperation | null = repository.readUnfinishedApply();
    if (operation === null) {
      return;
    }

    let recovery: RemoteApplyRecovery;
    try {
      recovery = await writer.recover(operation);
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      if (error instanceof AtomicVaultWriterError) {
        diagnostics.applyFailure(error.stage, error.reason);
        throwMapped(error.reason, error.retryable);
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("recovery", mapped);
      throwMapped(mapped, false);
    }

    if (recovery.kind !== "blocked" && recovery.cleanupFailure !== null) {
      diagnostics.applyFailure("trash", recovery.cleanupFailure);
    }

    switch (recovery.kind) {
      case "clean": {
        if (
          operation.state === "locally_applied" ||
          operation.state === "server_acknowledged"
        ) {
          // Post-commit clean: the event already terminalized locally and
          // only the server acknowledgement is owed — the coordinator's
          // acknowledgement phase owns that debt; nothing else to do.
          return;
        }
        // Pre-mutation clean (a `prepared` row proven at the verified
        // pre-mutation expectation): the pull NEVER redelivers an
        // already-delivered event (the server's delivered watermark
        // advanced when the page was sent), and the durable prepared row
        // would block both a fresh prepare and the reconciler's synthetic
        // apply at the same sequence. The live Desktop gate proved the old
        // "await redelivery" behavior as a permanent silent stall (and,
        // once a repair ran, as the endless `device_apply_recovery_ambiguous`
        // prepare-collision loop). The proven-clean intent is abandoned and
        // a repair barrier requires the manifest reconciliation that
        // re-converges the event.
        try {
          await repository.abandonRemoteApply(operation.eventSequence);
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const mapped = closedStoreReasonOf(error);
          diagnostics.applyFailure("recovery", mapped);
          throwMapped(mapped, false);
        }
        if (repository.readState().activeManifestRunId !== null) {
          // A BOUND manifest run already is the manifest reconciliation
          // the barrier below exists to force — its action re-attempt or
          // its canonical-only download re-converges the abandoned event —
          // and the mint is refused under the very run binding that
          // guarantees it. Abandoning quietly lets the run resume instead
          // of wedging the cycle-start recovery (the 2026-09-03
          // apply-lane failure's shape).
          return;
        }
        try {
          const generation = await repository.nextObservationGeneration();
          await repository.startRepairBarrier({
            generation,
            reason: "device_apply_recovery_abandoned",
          });
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const mapped = closedStoreReasonOf(error);
          diagnostics.applyFailure("recovery", mapped);
          throwMapped(mapped, false);
        }
        return;
      }
      case "mutated": {
        if (
          operation.state !== "vault_mutated" &&
          operation.state !== "locally_applied" &&
          operation.state !== "server_acknowledged"
        ) {
          try {
            await repository.transitionRemoteApply({
              eventSequence: operation.eventSequence,
              state: "vault_mutated",
              rollbackToken: recovery.rollbackToken,
            });
          } catch (error) {
            if (error instanceof DeviceSyncApiError) {
              throw error;
            }
            const mapped = closedStoreReasonOf(error);
            diagnostics.applyFailure("recovery", mapped);
            throwMapped(mapped, false);
          }
        }
        const outcome: TerminalDeviceEventOutcome =
          operation.operation === "deleted" ? "tombstone_handled" : "applied";
        await terminalize(operation.eventSequence, outcome, null, "recovery");
        return;
      }
      case "restored": {
        await terminalize(operation.eventSequence, "conflict", recovery.reason, "recovery");
        return;
      }
      case "blocked": {
        diagnostics.applyFailure("recovery", recovery.reason);
        try {
          const generation = await repository.nextObservationGeneration();
          await repository.startRepairBarrier({ generation, reason: recovery.reason });
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const mapped = closedStoreReasonOf(error);
          diagnostics.applyFailure("recovery", mapped);
          throwMapped(mapped, false);
        }
        return;
      }
    }
  }

  async function settleVaultFailedApply(
    event: DeviceSyncEvent,
    reason: DeviceSyncReason,
  ): Promise<TerminalDeviceEvent> {
    const leftover = repository.readRemoteApply(event.eventSequence);
    if (leftover === null) {
      // The refusal predated the durable prepare: nothing of this event is
      // held — the closed verdict stands without a lattice change.
      return { eventSequence: event.eventSequence, outcome: "conflict", reason };
    }
    if (
      leftover.eventId !== event.eventId ||
      leftover.state === "locally_applied" ||
      leftover.state === "server_acknowledged"
    ) {
      // A foreign row holding this sequence contradicts the caller's
      // durable attempt evidence, and an already-terminal row owes no
      // sequence: never free lattice evidence this settle does not own.
      diagnostics.applyFailure("recovery", "device_apply_recovery_ambiguous");
      throwMapped("device_apply_recovery_ambiguous", false);
    }

    let recovery: RemoteApplyRecovery;
    try {
      recovery = await writer.recover(leftover);
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      if (error instanceof AtomicVaultWriterError) {
        // The recovery met the same refusal (a `temp_verified` resume the
        // locked target refuses): the staged bytes are digest-verified,
        // the first visible mutation never completed, and the sequence
        // must still free for the run — close it with the
        // cursor-advancing conflict verdict (the repository's own
        // contract: a non-applied terminal outcome closes a dangling
        // pre-mutation row with its closed reason). The placement
        // re-converges through a later reconciliation once the refusal
        // clears.
        diagnostics.applyFailure(error.stage, error.reason);
        return terminalize(event.eventSequence, "conflict", error.reason, "recovery");
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("recovery", mapped);
      throwMapped(mapped, false);
    }

    if (recovery.kind !== "blocked" && recovery.cleanupFailure !== null) {
      diagnostics.applyFailure("trash", recovery.cleanupFailure);
    }

    switch (recovery.kind) {
      case "clean": {
        // Proven at the verified pre-mutation expectation: abandon the
        // intent together with its echo marker — the sequence stays
        // REUSABLE (the cursor does not burn it), so the run's remaining
        // placements keep their slots and the refused one re-converges
        // through a later repair. No barrier is minted: this settle only
        // runs inside a bound manifest run, which already is the
        // reconciliation the crash path's barrier exists to force.
        try {
          await repository.abandonRemoteApply(event.eventSequence);
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const mapped = closedStoreReasonOf(error);
          diagnostics.applyFailure("recovery", mapped);
          throwMapped(mapped, false);
        }
        return { eventSequence: event.eventSequence, outcome: "conflict", reason };
      }
      case "mutated": {
        // The refused write actually completed (or the recovery just
        // finished it from the verified staging bytes): the placement
        // holds — settle it as the applied truth, never a lossy verdict.
        if (leftover.state !== "vault_mutated") {
          try {
            await repository.transitionRemoteApply({
              eventSequence: event.eventSequence,
              state: "vault_mutated",
              rollbackToken: recovery.rollbackToken,
            });
          } catch (error) {
            if (error instanceof DeviceSyncApiError) {
              throw error;
            }
            const mapped = closedStoreReasonOf(error);
            diagnostics.applyFailure("recovery", mapped);
            throwMapped(mapped, false);
          }
        }
        const outcome: TerminalDeviceEventOutcome =
          leftover.operation === "deleted" ? "tombstone_handled" : "applied";
        return terminalize(event.eventSequence, outcome, null, "recovery");
      }
      case "restored": {
        return terminalize(event.eventSequence, "conflict", recovery.reason, "recovery");
      }
      case "blocked": {
        // Ambiguous preserved bytes: never free the sequence on a guess —
        // the caller's run blocks with the closed reason readable (the
        // coordinator's bounded-retry verdict keeps it from churning).
        diagnostics.applyFailure("recovery", recovery.reason);
        throwMapped(recovery.reason, false);
      }
    }
  }

  return { recoverUnfinishedApply, settleVaultFailedApply, apply };
}
