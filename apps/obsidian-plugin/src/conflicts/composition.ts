/**
 * The Conflict Inbox composition surface (Child 8 spec 5.2.6/6, Task 9).
 *
 * This module binds the Task 8 ports the controller depends on:
 *
 * - the concrete {@link CanonicalOutcomeApplier} over the atomic Vault
 *   writer's own seam discipline (`AtomicVaultWriterImpl`'s
 * same-directory hidden staging sibling, verified bytes, narrow replace
 * with a retained rollback sibling, and the Vault trash path for
 * tombstones) plus the journal's exact echo markers, so the plugin's own
 * apply is suppressed by the capture lane instead of re-uploaded;
 * - the journal-side locator resolution of one apply command (the
 * `local_files` mapping of the conflict's source), which refuses closed
 * at the winner stage for the sourceless `locator_collision` shape and
 * any unmapped or ambiguous source — no locator is ever derived from
 * wire data;
 * - the winner download of a null-bytes command through the EXISTING
 * version-based download surface (`downloadSourceVersion`: the conflict
 * evidence API is role-based, not version-based, so it cannot serve a
 * version winner);
 * - the interim verified-candidate uploader: no server route produces a
 * `verified_candidate_object_id` for an open conflict's resolution today
 * (Task 7 report §1), so the port fails closed with the controller's own
 * candidate-upload reason — Task 10's server surface swaps in the real
 * binding behind this exact port shape;
 * - the closed-token diagnostics trail sink, the foreign-throw observer
 * of the modal command surface (the Task 8 M-1 carry: a repair-store
 * throw the modal would render as `reason_unavailable` reaches the trail
 * as `conflict_repair_store_failed` with its closed store reason), and
 * the pending-apply status facts (counts and closed safe-reason tokens
 * only — never a locator, conflict id or timestamp on that surface).
 *
 * Privacy (spec 9): every thrown failure is the controller's closed
 * `CanonicalApplyError` staging; every observed diagnostic is one closed
 * token. No raw content, locator, digest or credential ever reaches a
 * thrown error, a status fact or a durable row through this module.
 */

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { DownloadSourceVersionInput, VerifiedDownload } from "../device-sync/api";
import {
  buildRollbackSiblingLocator,
  buildTempSiblingLocator,
} from "../device-sync/atomic-vault-writer";
import type { VaultMutationSeam } from "../device-sync/atomic-vault-writer";
import type { DeviceSyncRepository, EchoMarker, VaultObservation } from "../device-sync/contracts";
import type { FrozenFingerprint } from "../journal/contracts";
import { deriveFrozenFingerprint } from "../journal/fingerprint";
import type { SyncDiagnosticsTrail } from "../journal/sync-diagnostics-trail";
import { JOURNAL_STORE_ERROR_REASONS } from "../journal/sqlite-database";
import type { JournalStoreErrorReason } from "../journal/sqlite-database";
import { ConflictApiError } from "./api";
import { CONFLICT_CONTROLLER_DIAGNOSTIC_REASONS } from "./controller";
import { CanonicalApplyError, ConflictControllerError } from "./controller";
import type {
  CanonicalOutcomeApplier,
  CanonicalOutcomeApplyCommand,
  ConflictController,
  ConflictDiagnosticsSink,
  VerifiedCandidateReceipt,
  VerifiedCandidateUpload,
  VerifiedCandidateUploader,
} from "./controller";
import { CONFLICT_LOCAL_REPAIR_SAFE_REASONS } from "./contracts";
import type { ConflictLocalRepairSafeReason, PendingLocalApply } from "./contracts";
import type { ConflictRepositoryDatabase } from "./repository";

// The controller port types the composition re-exposes for its callers
// (one import site for the whole Task 9 composition surface).
export type {
  CanonicalOutcomeApplyCommand,
  ConflictController,
  VerifiedCandidateUploader,
} from "./controller";

// --- the closed composition diagnostics vocabulary -----------------------------------------------------

/**
 * The three closed reasons the Task 9 composition adds beyond the
 * controller's own nine: a repair-store mutation that threw out of a
 * controller command (the M-1 path), the fire-and-forget retry surface's
 * own rejection, and a failed best-effort echo-marker cleanup.
 */
export const CONFLICT_COMPOSITION_EXTRA_DIAGNOSTIC_REASONS = [
  "conflict_repair_store_failed",
  "conflict_apply_retry_failed",
  "conflict_echo_marker_failed",
] as const;

/** One closed composition-only conflict diagnostics reason. */
export type ConflictCompositionExtraDiagnosticReason =
  (typeof CONFLICT_COMPOSITION_EXTRA_DIAGNOSTIC_REASONS)[number];

/**
 * The closed reason vocabulary of the whole conflict composition: the
 * controller's nine tokens plus the three composition tokens. The
 * diagnostics trail admits exactly this vocabulary under its
 * `conflict_failure` kind.
 */
export const CONFLICT_COMPOSITION_DIAGNOSTIC_REASONS = [
  ...CONFLICT_CONTROLLER_DIAGNOSTIC_REASONS,
  ...CONFLICT_COMPOSITION_EXTRA_DIAGNOSTIC_REASONS,
] as const;

/** One closed conflict composition diagnostics reason. */
export type ConflictCompositionDiagnosticReason =
  (typeof CONFLICT_COMPOSITION_DIAGNOSTIC_REASONS)[number];

/**
 * The composition's diagnostics sink: the controller's own observe-only
 * surface plus the composition-only reasons with their optional closed
 * store-reason context token.
 */
export interface ConflictCompositionDiagnosticsSink extends ConflictDiagnosticsSink {
  observeConflictCompositionFailure(
    reason: ConflictCompositionExtraDiagnosticReason,
    contextReason?: JournalStoreErrorReason | null,
  ): void;
}

/** Build the closed-token trail sink of the conflict composition. */
export function createConflictDiagnosticsTrailSink(
  trail: SyncDiagnosticsTrail,
): ConflictCompositionDiagnosticsSink {
  return {
    observeConflictFailure(reason) {
      void trail.append({ kind: "conflict_failure", tokens: [reason] });
    },
    observeConflictCompositionFailure(reason, contextReason) {
      const tokens =
        contextReason === undefined || contextReason === null
          ? ([reason] as const)
          : ([reason, contextReason] as const);
      void trail.append({ kind: "conflict_failure", tokens: [...tokens] });
    },
  };
}

// --- the closed store-reason gate ----------------------------------------------------------------------

const CLOSED_STORE_REASONS: ReadonlySet<string> = new Set<string>(JOURNAL_STORE_ERROR_REASONS);

/** The closed store reason of one thrown value, when it carries one. */
function storeReasonOf(error: unknown): JournalStoreErrorReason | null {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = (error as { reason?: unknown }).reason;
    if (typeof reason === "string" && CLOSED_STORE_REASONS.has(reason)) {
      return reason as JournalStoreErrorReason;
    }
  }
  return null;
}

// --- the journal-side locator resolution ---------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Render one locator as a SQL text literal (quotes doubled). */
function sqlText(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/**
 * Resolve the target locator of one apply command: the current
 * `local_files` mapping of the conflict's source. The sourceless
 * `locator_collision` shape (`sourceId: null`), an unknown source and an
 * ambiguous mapping all answer null — the applier then refuses closed at
 * the winner stage instead of deriving a locator from wire data.
 */
function readLocalFileLocatorBySourceId(
  database: ConflictRepositoryDatabase,
  sourceId: string | null,
): string | null {
  if (sourceId === null || !UUID_PATTERN.test(sourceId)) {
    return null;
  }
  const rows = database.readAll(
    "select normalized_path from local_files " +
      `where source_id = ${sqlText(sourceId)} order by rowid asc;`,
  );
  const paths: string[] = [];
  for (const row of rows[0]?.values ?? []) {
    const [normalizedPath] = row as [string];
    if (typeof normalizedPath === "string") {
      paths.push(normalizedPath);
    }
  }
  return paths.length === 1 ? (paths[0] as string) : null;
}

// --- the disjoint echo-marker sequence namespace --------------------------------------------------------

/**
 * The ceiling of the conflict echo-marker sequence namespace: the server's
 * device event sequences grow contiguously from one, so the top of the
 * safe-integer range is a disjoint namespace a real cursor can never
 * realistically reach. The minter counts down from the ceiling; a session
 * that hits a leftover marker of a prior session retries on the next
 * lower value.
 */
export const CONFLICT_ECHO_MARKER_SEQUENCE_CEILING = Number.MAX_SAFE_INTEGER;

/** The default conflict echo-marker sequence minter (counts down the disjoint namespace). */
export function createConflictEchoMarkerSequenceMinter(): () => number {
  let nextSequence = CONFLICT_ECHO_MARKER_SEQUENCE_CEILING;
  return () => {
    const sequence = nextSequence;
    nextSequence -= 1;
    return sequence;
  };
}

/** The marker seed of one apply: everything but the minted sequence. */
interface EchoMarkerSeed {
  readonly sourceId: string;
  readonly operation: EchoMarker["operation"];
  readonly priorLocator: string | null;
  readonly targetLocator: string | null;
  readonly finalFingerprint: FrozenFingerprint | null;
}

/** The number of sequence-mint attempts before the marker persist fails the apply. */
const ECHO_MARKER_SEQUENCE_ATTEMPTS = 3;

/**
 * Record one echo marker from the disjoint conflict namespace. A refused
 * duplicate (a prior session's leftover at the same sequence) retries on
 * the next-lower mint exactly {@link ECHO_MARKER_SEQUENCE_ATTEMPTS}
 * times; any other failure rethrows.
 */
async function recordConflictEchoMarker(
  repository: DeviceSyncRepository,
  minter: () => number,
  seed: EchoMarkerSeed,
): Promise<EchoMarker> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < ECHO_MARKER_SEQUENCE_ATTEMPTS; attempt += 1) {
    const marker: EchoMarker = { eventSequence: minter(), ...seed };
    try {
      await repository.recordEchoMarker(marker);
      return marker;
    } catch (error) {
      lastError = error;
      if (storeReasonOf(error) !== "journal_mutation_failed") {
        throw error;
      }
    }
  }
  throw lastError;
}

// --- the concrete canonical outcome applier ----------------------------------------------------------

export interface ConflictCanonicalOutcomeApplierOptions {
  /** The journal's single serialized writer + read seam (locator resolution). */
  readonly database: ConflictRepositoryDatabase;
  /** The device-sync repository (the exact echo markers of spec 8.2). */
  readonly repository: DeviceSyncRepository;
  /** The atomic Vault mutation seam the device-sync writer already binds. */
  readonly seam: VaultMutationSeam;
  /**
   * The EXISTING version-based winner download (`downloadSourceVersion`).
   * The conflict evidence API is role-based, not version-based, so it
   * cannot serve a version winner.
   */
  readonly downloadSourceVersion: (
    input: DownloadSourceVersionInput,
  ) => Promise<VerifiedDownload>;
  /** The observe-only diagnostics sink of the composition's own paths. */
  readonly diagnostics?: ConflictCompositionDiagnosticsSink | null | undefined;
  /** The marker sequence minter; defaults to the disjoint high namespace. */
  readonly mintEchoMarkerSequence?: (() => number) | undefined;
}

/** Whether the bytes hash to exactly the pinned fingerprint. */
async function hashesTo(
  bytes: Uint8Array | null,
  fingerprint: FrozenFingerprint,
): Promise<boolean> {
  return (
    bytes !== null &&
    bytes.byteLength === fingerprint.sizeBytes &&
    (await sha256Hex(bytes)) === fingerprint.sha256
  );
}

/** Best-effort sibling cleanup; the apply outcome never blocks on it. */
async function trashQuietly(seam: VaultMutationSeam, locator: string): Promise<void> {
  try {
    await seam.trashLocator(locator);
  } catch {
    // A leftover hidden sibling is not data loss; the apply outcome
    // surfaces through its own closed stage.
  }
}

/** Read one locator's bytes, failing closed onto the vault_apply stage. */
async function readBytesOrFail(seam: VaultMutationSeam, locator: string): Promise<Uint8Array | null> {
  try {
    return await seam.readBytes(locator);
  } catch {
    throw new CanonicalApplyError("vault_apply");
  }
}

/**
 * Build the concrete canonical outcome applier. One apply resolves the
 * target locator from the journal, proves the winner bytes (the verified
 * version download or the in-memory merged draft), records the exact
 * echo marker BEFORE any Vault mutation, then walks the atomic writer's
 * own stage/verify/narrow-replace discipline (or the trash path for a
 * tombstone). A failure throws the controller's closed
 * {@link CanonicalApplyError} staging; a failed apply consumes its own
 * marker best-effort so it can never suppress a later real observation.
 */
export function createConflictCanonicalOutcomeApplier(
  options: ConflictCanonicalOutcomeApplierOptions,
): CanonicalOutcomeApplier {
  const { database, repository, seam, downloadSourceVersion } = options;
  const diagnostics = options.diagnostics ?? null;
  const mintEchoMarkerSequence =
    options.mintEchoMarkerSequence ?? createConflictEchoMarkerSequenceMinter();

  function observeMarkerCleanupFailure(error: unknown): void {
    diagnostics?.observeConflictCompositionFailure(
      "conflict_echo_marker_failed",
      storeReasonOf(error),
    );
  }

  /** Consume the apply's own marker best-effort after a failed mutation. */
  async function consumeMarkerQuietly(marker: EchoMarker): Promise<void> {
    const observation: VaultObservation = {
      eventSequence: marker.eventSequence,
      sourceId: marker.sourceId,
      operation: marker.operation,
      priorLocator: marker.priorLocator,
      targetLocator: marker.targetLocator,
      fingerprint: marker.finalFingerprint,
    };
    try {
      await repository.matchAndConsumeEcho(observation);
    } catch (error) {
      observeMarkerCleanupFailure(error);
    }
  }

  /**
   * Stage the bytes in the same-directory hidden sibling, verify them,
   * prove the target's current shape, then narrow-replace with a
   * retained rollback sibling and verify the final bytes — the
   * AtomicVaultWriterImpl discipline minus its device-event durable row
   * (a conflict apply owns no cursor sequence; its durable fact is the
   * no-byte parked repair row).
   */
  async function stageVerifyAndReplace(
    locator: string,
    tempToken: string,
    bytes: Uint8Array,
    fingerprint: FrozenFingerprint,
  ): Promise<void> {
    const tempLocator = buildTempSiblingLocator(locator, tempToken);
    // 1. Stage and verify the winner bytes in the hidden sibling.
    try {
      if (await seam.locatorExists(tempLocator)) {
        await seam.trashLocator(tempLocator);
      }
      await seam.createFile(tempLocator, bytes);
    } catch {
      throw new CanonicalApplyError("vault_apply");
    }
    if (!(await hashesTo(await readBytesOrFail(seam, tempLocator), fingerprint))) {
      await trashQuietly(seam, tempLocator);
      throw new CanonicalApplyError("vault_apply");
    }
    // 2. Prove the target's current shape: an occupied target keeps a
    //    rollback sibling of its verified old bytes; an absent target
    //    renames straight in (the created shape).
    const hadTarget = (await readBytesOrFail(seam, locator)) !== null;
    const rollbackLocator = hadTarget
      ? buildRollbackSiblingLocator(locator, tempToken)
      : null;
    // 3. The narrow replace.
    try {
      if (rollbackLocator !== null) {
        await seam.renameLocator(locator, rollbackLocator);
      }
      await seam.renameLocator(tempLocator, locator);
    } catch {
      throw new CanonicalApplyError("vault_apply");
    }
    // 4. Verify the final bytes; on mismatch restore the verified old
    //    bytes through the rollback sibling.
    if (!(await hashesTo(await readBytesOrFail(seam, locator), fingerprint))) {
      if (rollbackLocator !== null) {
        try {
          await seam.trashLocator(locator);
          await seam.renameLocator(rollbackLocator, locator);
        } catch {
          // The rollback sibling keeps the verified old bytes; the
          // closed vault_apply failure parks the owed repair.
        }
      }
      throw new CanonicalApplyError("vault_apply");
    }
    // 5. The retained rollback sibling's cleanup is best-effort.
    if (rollbackLocator !== null) {
      await trashQuietly(seam, rollbackLocator);
    }
  }

  /** Resolve the winner bytes and their exact fingerprint. */
  async function resolveWinner(
    command: CanonicalOutcomeApplyCommand,
  ): Promise<{ bytes: Uint8Array; fingerprint: FrozenFingerprint }> {
    if (command.winnerBytes !== null) {
      const bytes = command.winnerBytes;
      const fingerprint =
        command.winnerMediaType === null
          ? await deriveFrozenFingerprint(bytes)
          : {
              sha256: await sha256Hex(bytes),
              sizeBytes: bytes.byteLength,
              mediaType: command.winnerMediaType,
            };
      return { bytes, fingerprint };
    }
    if (command.sourceId === null || command.winnerVersionId === null) {
      throw new CanonicalApplyError("winner_download");
    }
    let download: VerifiedDownload;
    try {
      download = await downloadSourceVersion({
        sourceId: command.sourceId,
        sourceVersionId: command.winnerVersionId,
      });
    } catch {
      throw new CanonicalApplyError("winner_download");
    }
    return {
      bytes: download.bytes,
      fingerprint: {
        sha256: download.declaredSha256,
        sizeBytes: download.sizeBytes,
        mediaType: download.mediaType,
      },
    };
  }

  return {
    async applyCanonicalOutcome(command): Promise<void> {
      const locator = readLocalFileLocatorBySourceId(database, command.sourceId);
      if (locator === null) {
        // The sourceless locator_collision shape, an unknown source or an
        // ambiguous mapping: the winner cannot be placed without deriving
        // a locator from wire data, so the apply refuses closed at the
        // winner stage (the parked repair keeps the owed work visible).
        throw new CanonicalApplyError("winner_download");
      }
      const sourceId = command.sourceId as string;

      if (command.targetAction === "apply_remote_tombstone") {
        const priorBytes = await readBytesOrFail(seam, locator);
        if (priorBytes === null) {
          // Idempotent: the prior locator is already gone (a retried
          // apply after the trash landed).
          return;
        }
        let marker: EchoMarker;
        try {
          marker = await recordConflictEchoMarker(repository, mintEchoMarkerSequence, {
            sourceId,
            operation: "deleted",
            priorLocator: locator,
            targetLocator: null,
            finalFingerprint: null,
          });
        } catch {
          // The marker is a precondition of the mutation: persisting it
          // fails the apply closed before anything is trashed.
          throw new CanonicalApplyError("vault_apply");
        }
        try {
          await seam.trashLocator(locator);
        } catch {
          await consumeMarkerQuietly(marker);
          throw new CanonicalApplyError("vault_apply");
        }
        return;
      }

      const { bytes, fingerprint } = await resolveWinner(command);
      let marker: EchoMarker;
      try {
        marker = await recordConflictEchoMarker(repository, mintEchoMarkerSequence, {
          sourceId,
          operation: "updated",
          priorLocator: locator,
          targetLocator: null,
          finalFingerprint: fingerprint,
        });
      } catch {
        throw new CanonicalApplyError("vault_apply");
      }
      try {
        await stageVerifyAndReplace(locator, command.resolutionEventId, bytes, fingerprint);
      } catch (error) {
        await consumeMarkerQuietly(marker);
        throw error instanceof CanonicalApplyError
          ? error
          : new CanonicalApplyError("vault_apply");
      }
    },
  };
}

// --- the interim verified-candidate uploader ---------------------------------------------------------

/**
 * The interim verified-candidate uploader: no server route produces a
 * verified candidate object for an open conflict's resolution today
 * (Task 7 report §1), so the port fails closed with the controller's own
 * `conflict_candidate_upload_failed` reason — the controller observes
 * the closed token on its diagnostics sink and the modal renders it.
 * Task 10's server surface swaps in the real binding behind this exact
 * port shape; no HTTP call against a nonexistent route is invented here.
 */
export function createUnavailableVerifiedCandidateUploader(): VerifiedCandidateUploader {
  return {
    async uploadVerifiedCandidate(
      upload: VerifiedCandidateUpload,
    ): Promise<VerifiedCandidateReceipt> {
      void upload;
      throw new ConflictControllerError("conflict_candidate_upload_failed");
    },
  };
}

// --- the foreign-throw observer of the modal command surface (M-1) -----------------------------------

/** Run one controller command, observing the previously unobserved foreign throws. */
async function observeForeignThrow<T>(
  sink: ConflictCompositionDiagnosticsSink,
  run: () => Promise<T>,
): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (!(error instanceof ConflictApiError) && !(error instanceof ConflictControllerError)) {
      // The modal renders this family as `reason_unavailable` (its
      // fixed foreign fallback): the trail keeps the closed repair-store
      // reason instead (the Task 8 M-1 carry).
      sink.observeConflictCompositionFailure("conflict_repair_store_failed", storeReasonOf(error));
    }
    throw error;
  }
}

/**
 * Wrap one controller so every modal-facing command's foreign throw
 * (a repair-store refusal, an image-corruption failure, any unexpected
 * local error) surfaces one closed `conflict_repair_store_failed` trail
 * entry — the wire client's and the controller's own closed failures
 * already surface through their closed kinds and are never re-observed.
 */
export function observeUnobservedConflictControllerFailures(
  controller: ConflictController,
  sink: ConflictCompositionDiagnosticsSink,
): ConflictController {
  return {
    listOpenConflicts: () => observeForeignThrow(sink, () => controller.listOpenConflicts()),
    getConflictDetail: (conflictId) =>
      observeForeignThrow(sink, () => controller.getConflictDetail(conflictId)),
    buildMergeProposal: (conflictId) =>
      observeForeignThrow(sink, () => controller.buildMergeProposal(conflictId)),
    resolveKeepRemote: (conflictId) =>
      observeForeignThrow(sink, () => controller.resolveKeepRemote(conflictId)),
    resolveKeepLocal: (conflictId) =>
      observeForeignThrow(sink, () => controller.resolveKeepLocal(conflictId)),
    resolveSaveMerged: (conflictId, editedText) =>
      observeForeignThrow(sink, () => controller.resolveSaveMerged(conflictId, editedText)),
    retryPendingLocalApplies: () =>
      observeForeignThrow(sink, () => controller.retryPendingLocalApplies()),
  };
}

// --- the pending-apply status facts --------------------------------------------------------------------

/** The closed, redacted conflict-apply status facts: counts and closed tokens only. */
export interface ConflictApplyStatusFacts {
  /** How many parked local applies still owe their Vault apply. */
  readonly pendingLocalApplyCount: number;
  /** The closed safe-reason tokens observed across those rows, first-seen order. */
  readonly localApplySafeReasonTokens: readonly ConflictLocalRepairSafeReason[];
}

/**
 * Derive the status facts of the parked local applies: every row counts —
 * including an attempt-capped row, whose retry eligibility gates on the
 * attempt cap, never on the timestamp (the Task 8 ruling) — and only the
 * closed safe-reason vocabulary ever reaches the surface. No locator,
 * conflict id, resolution id or timestamp is carried.
 */
export function deriveConflictApplyStatusFacts(
  pending: readonly PendingLocalApply[],
): ConflictApplyStatusFacts {
  const tokens: ConflictLocalRepairSafeReason[] = [];
  for (const row of pending) {
    if (
      (CONFLICT_LOCAL_REPAIR_SAFE_REASONS as readonly string[]).includes(row.safeReason) &&
      !tokens.includes(row.safeReason)
    ) {
      tokens.push(row.safeReason);
    }
  }
  return { pendingLocalApplyCount: pending.length, localApplySafeReasonTokens: tokens };
}
