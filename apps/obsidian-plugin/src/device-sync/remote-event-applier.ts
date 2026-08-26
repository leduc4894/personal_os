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
 * Electron or `FileSystemAdapter` API at module load time, so it stays
 * loadable on mobile.
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
  apply(event: DeviceSyncEvent): Promise<TerminalDeviceEvent>;
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

  async function apply(event: DeviceSyncEvent): Promise<TerminalDeviceEvent> {
    const state = repository.readState();
    if (event.eventSequence <= state.appliedSequence) {
      // Idempotent replay of an already-settled event: the durable row
      // and cursor are the truth; nothing is re-run.
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
        if (recovery.kind !== "blocked" && recovery.cleanupFailure !== null) {
          diagnostics.applyFailure("trash", recovery.cleanupFailure);
        }
      } catch {
        // The apply is already durably mutated; a failed cleanup is a
        // leftover hidden sibling, never data loss.
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
      case "clean":
        // The Vault sits at the verified pre-mutation expectation; the
        // pull loop redelivers the event and re-applies it.
        return;
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

  return { recoverUnfinishedApply, apply };
}
