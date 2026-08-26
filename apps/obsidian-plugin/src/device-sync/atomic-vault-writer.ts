/**
 * The atomic Vault writer (device cursor and manifest reconciliation,
 * task 10, spec 8.1, 11).
 *
 * Content applies stage verified bytes in a SAME-DIRECTORY hidden
 * temporary sibling, verify them against the expected final fingerprint,
 * persist `temp_verified`, perform the narrow replace with a retained
 * rollback sibling (the verified old bytes), verify the final bytes,
 * and leave `vault_mutated` to the caller's single transition. Locator
 * applies rename after proving the prior bytes and an unoccupied
 * target; tombstones trash through the Vault trash path —
 * `Vault.trash(file, false)` — with NO hard-delete fallback anywhere.
 *
 * Crash recovery (`recover`) reconciles one durable
 * {@link RemoteApplyOperation} with the Vault: it cleans the exact
 * staging/rollback siblings the durable token names, resumes a verified
 * replace, restores the verified old bytes when the final proof fails,
 * and PRESERVES ambiguous bytes (blocked) instead of guessing. The
 * verified old or new bytes survive every crash point — never an
 * unverified in-place overwrite.
 *
 * The writer is Obsidian-agnostic: it drives an injected
 * {@link VaultMutationSeam}. The concrete Obsidian binding
 * ({@link createStructuralVaultMutationSeam}) is a plain structural
 * surface over the real `Vault` (no `obsidian` import), so Task 12 can
 * instantiate it with `app.vault` while this whole tree stays loadable
 * on mobile.
 *
 * Privacy (spec 9, 14): every failure is a closed stage + reason on
 * {@link AtomicVaultWriterError}; locators, digests and bytes never
 * reach a thrown message.
 */

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { FrozenFingerprint } from "../journal/contracts";
import type { JournalStoreErrorReason } from "../journal/sqlite-database";
import type {
  ApplyFailureStage,
  DeviceSyncReason,
  DeviceSyncRepository,
  RemoteApplyOperation,
} from "./contracts";

// --- the frozen sibling naming ------------------------------------------------------------------------

const TEMP_SIBLING_SUFFIX = "device-sync-tmp";
const ROLLBACK_SIBLING_SUFFIX = "device-sync-rb";

function parentPrefixOf(locator: string): string {
  const lastSlash = locator.lastIndexOf("/");
  return lastSlash === -1 ? "" : `${locator.slice(0, lastSlash)}/`;
}

function baseNameOf(locator: string): string {
  const lastSlash = locator.lastIndexOf("/");
  return lastSlash === -1 ? locator : locator.slice(lastSlash + 1);
}

/**
 * The same-directory hidden temporary sibling of one target locator:
 * `notes/.a.md.device-sync-tmp-<token>`. Same directory guarantees the
 * staging write never crosses a folder boundary the Vault owns.
 */
export function buildTempSiblingLocator(targetLocator: string, token: string): string {
  return `${parentPrefixOf(targetLocator)}.${baseNameOf(targetLocator)}.${TEMP_SIBLING_SUFFIX}-${token}`;
}

/** The same-directory hidden rollback sibling holding the verified old bytes. */
export function buildRollbackSiblingLocator(targetLocator: string, token: string): string {
  return `${parentPrefixOf(targetLocator)}.${baseNameOf(targetLocator)}.${ROLLBACK_SIBLING_SUFFIX}-${token}`;
}

// --- the apply inputs and outcomes ---------------------------------------------------------------------

/** One content apply: staged, verified, narrowly replaced, finally verified bytes. */
export interface ContentApplyInput {
  readonly eventSequence: number;
  readonly operation: "created" | "updated" | "restored";
  readonly targetLocator: string;
  readonly expectedFinalFingerprint: FrozenFingerprint;
  /** The pinned base fingerprint of the current bytes (updated only; null skips the in-place proof). */
  readonly baseFingerprint: FrozenFingerprint | null;
  readonly bytes: Uint8Array;
  /** The durable staging token (the prepared row's `tempToken`); it names both siblings. */
  readonly tempToken: string;
}

/** One locator apply: a rename (same parent) or move (changed parent) of verified bytes. */
export interface LocatorApplyInput {
  readonly eventSequence: number;
  readonly operation: "renamed" | "moved";
  readonly priorLocator: string;
  readonly targetLocator: string;
  readonly expectedFinalFingerprint: FrozenFingerprint;
}

/** One tombstone apply: trash the verified prior bytes through the Vault trash path. */
export interface TombstoneApplyInput {
  readonly eventSequence: number;
  readonly priorLocator: string;
  readonly baseFingerprint: FrozenFingerprint | null;
}

/** The verified evidence of one completed Vault mutation. */
export interface VerifiedVaultMutation {
  readonly targetLocator: string | null;
  readonly verifiedFingerprint: FrozenFingerprint | null;
  readonly tempToken: string | null;
  readonly rollbackToken: string | null;
}

/**
 * The outcome of one crash recovery: `clean` (the Vault sits at the
 * verified pre-mutation expectation — the event needs re-delivery),
 * `mutated` (the operation-shaped effect is verified complete — persist
 * `vault_mutated` and terminalize), `restored` (the verified old bytes
 * are back — settle the event as a conflict) or `blocked` (ambiguous
 * bytes preserved for human/policy resolution — raise the repair
 * barrier). `cleanupFailure` carries a closed reason when a best-effort
 * sibling cleanup failed without affecting the outcome.
 */
export type RemoteApplyRecovery =
  | {
      readonly kind: "clean";
      readonly eventSequence: number;
      readonly cleanupFailure: DeviceSyncReason | null;
    }
  | {
      readonly kind: "mutated";
      readonly eventSequence: number;
      readonly verifiedFingerprint: FrozenFingerprint | null;
      readonly rollbackToken: string | null;
      readonly cleanupFailure: DeviceSyncReason | null;
    }
  | {
      readonly kind: "restored";
      readonly eventSequence: number;
      readonly reason: DeviceSyncReason;
      readonly cleanupFailure: DeviceSyncReason | null;
    }
  | { readonly kind: "blocked"; readonly reason: DeviceSyncReason };

/** The writer port the remote event applier drives (brief task 10). */
export interface AtomicVaultWriter {
  stageAndReplace(input: ContentApplyInput): Promise<VerifiedVaultMutation>;
  renameOrMove(input: LocatorApplyInput): Promise<VerifiedVaultMutation>;
  trash(input: TombstoneApplyInput): Promise<VerifiedVaultMutation>;
  recover(input: RemoteApplyOperation): Promise<RemoteApplyRecovery>;
}

// --- the closed failure surface -------------------------------------------------------------------------

/** One closed writer failure: the exact stage, the closed reason, retryability and rollback evidence. */
export class AtomicVaultWriterError extends Error {
  readonly stage: ApplyFailureStage;
  readonly reason: DeviceSyncReason;
  readonly retryable: boolean;
  readonly restoredToBase: boolean;

  constructor(
    stage: ApplyFailureStage,
    reason: DeviceSyncReason,
    retryable: boolean,
    restoredToBase: boolean,
  ) {
    super(`atomic vault writer failed: ${reason}`);
    this.name = "AtomicVaultWriterError";
    this.stage = stage;
    this.reason = reason;
    this.retryable = retryable;
    this.restoredToBase = restoredToBase;
  }
}

function writerError(
  stage: ApplyFailureStage,
  reason: DeviceSyncReason,
  retryable: boolean,
  restoredToBase = false,
): AtomicVaultWriterError {
  return new AtomicVaultWriterError(stage, reason, retryable, restoredToBase);
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

/** Whether the bytes hash to exactly the pinned fingerprint (null bytes or fingerprint never match). */
async function hashesTo(
  bytes: Uint8Array | null,
  fingerprint: FrozenFingerprint | null,
): Promise<boolean> {
  if (bytes === null || fingerprint === null) {
    return false;
  }
  return bytes.byteLength === fingerprint.sizeBytes && (await sha256Hex(bytes)) === fingerprint.sha256;
}

// --- the injected Vault seam ---------------------------------------------------------------------------

/**
 * The narrow Vault mutation seam the writer drives: same-directory
 * staging, reading, renaming and trashing. There is deliberately NO
 * permanent-delete method — removed bytes always move to the Vault
 * trash through `trashLocator`.
 */
export interface VaultMutationSeam {
  createFile(locator: string, bytes: Uint8Array): Promise<void>;
  readBytes(locator: string): Promise<Uint8Array | null>;
  renameLocator(fromLocator: string, toLocator: string): Promise<void>;
  trashLocator(locator: string): Promise<void>;
  locatorExists(locator: string): Promise<boolean>;
}

/**
 * The structural slice of the Obsidian `Vault` the concrete binding
 * needs (typed structurally — no `obsidian` import — so Task 12 can
 * hand over `app.vault` directly and this module stays mobile-loadable).
 */
export interface StructuralVaultFile {
  readonly path: string;
}

export interface StructuralVaultSurface {
  getAbstractFileByPath(path: string): StructuralVaultFile | null;
  createBinary(path: string, data: ArrayBuffer): Promise<void>;
  readBinary(path: string): Promise<ArrayBuffer>;
  rename(file: StructuralVaultFile, newPath: string): Promise<void>;
  trash(file: StructuralVaultFile, system: boolean): Promise<void>;
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

/**
 * Bind the mutation seam to the structural Obsidian `Vault` surface.
 * Every removal goes through `Vault.trash(file, false)` — the system
 * trash is never used and no permanent-delete method exists on the
 * seam.
 */
export function createStructuralVaultMutationSeam(vault: StructuralVaultSurface): VaultMutationSeam {
  return {
    async locatorExists(locator: string): Promise<boolean> {
      return vault.getAbstractFileByPath(locator) !== null;
    },

    async createFile(locator: string, bytes: Uint8Array): Promise<void> {
      await vault.createBinary(locator, toArrayBuffer(bytes));
    },

    async readBytes(locator: string): Promise<Uint8Array | null> {
      if (vault.getAbstractFileByPath(locator) === null) {
        return null;
      }
      return new Uint8Array(await vault.readBinary(locator));
    },

    async renameLocator(fromLocator: string, toLocator: string): Promise<void> {
      const file = vault.getAbstractFileByPath(fromLocator);
      if (file === null) {
        throw writerError("vault_mutation", "device_apply_vault_failed", true);
      }
      await vault.rename(file, toLocator);
    },

    async trashLocator(locator: string): Promise<void> {
      const file = vault.getAbstractFileByPath(locator);
      if (file === null) {
        throw writerError("trash", "device_apply_trash_failed", true);
      }
      await vault.trash(file, false);
    },
  };
}

// --- the writer implementation --------------------------------------------------------------------------

export interface AtomicVaultWriterOptions {
  readonly repository: DeviceSyncRepository;
  readonly seam: VaultMutationSeam;
}

/** The target locator a content operation mutates (updated pins it as the prior locator). */
function contentTargetOf(operation: RemoteApplyOperation): string | null {
  if (operation.operation === "updated") {
    return operation.priorLocator;
  }
  if (operation.operation === "created" || operation.operation === "restored") {
    return operation.targetLocator;
  }
  return null;
}

/**
 * The seam-agnostic atomic writer. Composed by the plugin root (or a
 * test) over the durable {@link DeviceSyncRepository} and one
 * {@link VaultMutationSeam}; it owns no bytes, no credential and no
 * transport. Every durable state change flows through the repository's
 * single serialized writer.
 */
export class AtomicVaultWriterImpl implements AtomicVaultWriter {
  readonly #repository: DeviceSyncRepository;
  readonly #seam: VaultMutationSeam;

  constructor(options: AtomicVaultWriterOptions) {
    this.#repository = options.repository;
    this.#seam = options.seam;
  }

  async stageAndReplace(input: ContentApplyInput): Promise<VerifiedVaultMutation> {
    const target = input.targetLocator;
    const tempLocator = buildTempSiblingLocator(target, input.tempToken);

    // 1. Stage the bytes in the same-directory temporary sibling and
    //    verify them against the expected final fingerprint.
    try {
      if (await this.#seam.locatorExists(tempLocator)) {
        // Exact-temp cleanup of a stale sibling from an earlier attempt.
        await this.#seam.trashLocator(tempLocator);
      }
      await this.#seam.createFile(tempLocator, input.bytes);
    } catch (error) {
      throw this.#wrap(error, "verify_temp", "device_apply_vault_failed", true);
    }
    const stagedBytes = await this.#readOrNull(tempLocator, "verify_temp");
    if (!(await hashesTo(stagedBytes, input.expectedFinalFingerprint))) {
      await this.#trashQuietly(tempLocator);
      throw writerError("verify_temp", "device_apply_vault_failed", true);
    }

    // 2. Prove the pre-mutation expectation: an unoccupied target for
    //    created/restored, the pinned base bytes for updated.
    const isUpdate = input.operation === "updated";
    let hadTarget = false;
    if (isUpdate) {
      const currentBytes = await this.#readOrNull(target, "vault_mutation");
      hadTarget = currentBytes !== null;
      if (currentBytes === null) {
        await this.#trashQuietly(tempLocator);
        throw writerError("vault_mutation", "device_manifest_local_diverged", false);
      }
      if (input.baseFingerprint !== null && !(await hashesTo(currentBytes, input.baseFingerprint))) {
        await this.#trashQuietly(tempLocator);
        throw writerError("vault_mutation", "device_manifest_local_diverged", false);
      }
    } else if (await this.#seam.locatorExists(target)) {
      await this.#trashQuietly(tempLocator);
      throw writerError("vault_mutation", "device_manifest_target_occupied", false);
    }

    // 3. Persist temp_verified before any mutation (content operations
    //    only — the lattice bars `restored` from the temp state).
    if (input.operation === "created" || input.operation === "updated") {
      try {
        await this.#repository.transitionRemoteApply({
          eventSequence: input.eventSequence,
          state: "temp_verified",
          tempToken: input.tempToken,
        });
      } catch (error) {
        const reason = storeReasonOf(error) ?? "device_apply_vault_failed";
        throw writerError("verify_temp", reason, false);
      }
    }

    // 4. The narrow replace with retained rollback evidence: the old
    //    bytes move to the rollback sibling, the verified temp moves in.
    const rollbackLocator = isUpdate && hadTarget
      ? buildRollbackSiblingLocator(target, input.tempToken)
      : null;
    try {
      if (rollbackLocator !== null) {
        await this.#seam.renameLocator(target, rollbackLocator);
      }
      await this.#seam.renameLocator(tempLocator, target);
    } catch (error) {
      throw this.#wrap(error, "vault_mutation", "device_apply_vault_failed", true);
    }

    // 5. Verify the final bytes; on mismatch restore the verified old
    //    bytes through the rollback sibling.
    const finalBytes = await this.#readOrNull(target, "verify_final");
    if (!(await hashesTo(finalBytes, input.expectedFinalFingerprint))) {
      const restoredToBase = rollbackLocator !== null
        ? await this.#restoreRollback(target, rollbackLocator, input.baseFingerprint)
        : false;
      throw writerError("verify_final", "device_apply_vault_failed", false, restoredToBase);
    }

    return {
      targetLocator: target,
      verifiedFingerprint: input.expectedFinalFingerprint,
      tempToken: input.tempToken,
      rollbackToken: rollbackLocator !== null ? input.tempToken : null,
    };
  }

  async renameOrMove(input: LocatorApplyInput): Promise<VerifiedVaultMutation> {
    if (await this.#seam.locatorExists(input.targetLocator)) {
      throw writerError("vault_mutation", "device_manifest_target_occupied", false);
    }
    const priorBytes = await this.#readOrNull(input.priorLocator, "vault_mutation");
    if (priorBytes === null || !(await hashesTo(priorBytes, input.expectedFinalFingerprint))) {
      throw writerError("vault_mutation", "device_manifest_local_diverged", false);
    }
    try {
      await this.#seam.renameLocator(input.priorLocator, input.targetLocator);
    } catch (error) {
      throw this.#wrap(error, "vault_mutation", "device_apply_vault_failed", true);
    }
    const finalBytes = await this.#readOrNull(input.targetLocator, "verify_final");
    if (!(await hashesTo(finalBytes, input.expectedFinalFingerprint))) {
      // Rollback evidence: rename the target back to the prior locator.
      let restoredToBase = false;
      try {
        await this.#seam.renameLocator(input.targetLocator, input.priorLocator);
        restoredToBase = await hashesTo(
          await this.#seam.readBytes(input.priorLocator),
          input.expectedFinalFingerprint,
        );
      } catch {
        restoredToBase = false;
      }
      throw writerError("verify_final", "device_apply_vault_failed", false, restoredToBase);
    }
    return {
      targetLocator: input.targetLocator,
      verifiedFingerprint: input.expectedFinalFingerprint,
      tempToken: null,
      rollbackToken: null,
    };
  }

  async trash(input: TombstoneApplyInput): Promise<VerifiedVaultMutation> {
    const priorBytes = await this.#readOrNull(input.priorLocator, "trash");
    if (priorBytes === null) {
      // Idempotent: the prior locator is already gone (a retried apply
      // after a crash between the trash and the durable transition).
      return { targetLocator: null, verifiedFingerprint: null, tempToken: null, rollbackToken: null };
    }
    if (input.baseFingerprint !== null && !(await hashesTo(priorBytes, input.baseFingerprint))) {
      throw writerError("trash", "device_manifest_local_diverged", false);
    }
    try {
      await this.#seam.trashLocator(input.priorLocator);
    } catch (error) {
      throw this.#wrap(error, "trash", "device_apply_trash_failed", true);
    }
    return { targetLocator: null, verifiedFingerprint: null, tempToken: null, rollbackToken: null };
  }

  async recover(operation: RemoteApplyOperation): Promise<RemoteApplyRecovery> {
    switch (operation.state) {
      case "prepared":
        return this.#recoverPrepared(operation);
      case "temp_verified":
        return this.#recoverTempVerified(operation);
      case "vault_mutated":
        return this.#recoverVaultMutated(operation);
      case "locally_applied":
      case "server_acknowledged":
        return {
          kind: "clean",
          eventSequence: operation.eventSequence,
          cleanupFailure: await this.#cleanSiblingsOf(operation),
        };
    }
  }

  // --- internals ---------------------------------------------------------------------------------------

  async #readOrNull(
    locator: string,
    stage: ApplyFailureStage,
  ): Promise<Uint8Array | null> {
    try {
      return await this.#seam.readBytes(locator);
    } catch (error) {
      throw this.#wrap(error, stage, "device_apply_vault_failed", true);
    }
  }

  #wrap(
    error: unknown,
    stage: ApplyFailureStage,
    reason: DeviceSyncReason,
    retryable: boolean,
  ): AtomicVaultWriterError {
    if (error instanceof AtomicVaultWriterError) {
      return error;
    }
    return writerError(stage, reason, retryable);
  }

  /** Best-effort trash of one sibling; the outcome is never blocked by it. */
  async #trashQuietly(locator: string): Promise<void> {
    try {
      await this.#seam.trashLocator(locator);
    } catch {
      // A leftover hidden sibling is not data loss; the apply outcome
      // still surfaces through its own closed stage.
    }
  }

  /** Restore the verified old bytes from the rollback sibling; true only when the base proof passes again. */
  async #restoreRollback(
    target: string,
    rollbackLocator: string,
    baseFingerprint: FrozenFingerprint | null,
  ): Promise<boolean> {
    try {
      await this.#seam.trashLocator(target);
      await this.#seam.renameLocator(rollbackLocator, target);
    } catch {
      return false;
    }
    const restoredBytes = await this.#seam.readBytes(target);
    if (baseFingerprint === null) {
      return restoredBytes !== null;
    }
    return hashesTo(restoredBytes, baseFingerprint);
  }

  /** Best-effort cleanup of the durable siblings one operation names; returns a closed failure reason or null. */
  async #cleanSiblingsOf(operation: RemoteApplyOperation): Promise<DeviceSyncReason | null> {
    const target = contentTargetOf(operation);
    if (target === null || operation.tempToken === null) {
      return null;
    }
    let cleanupFailure: DeviceSyncReason | null = null;
    const tempLocator = buildTempSiblingLocator(target, operation.tempToken);
    const rollbackLocator = buildRollbackSiblingLocator(target, operation.tempToken);
    for (const sibling of [tempLocator, rollbackLocator]) {
      try {
        if (await this.#seam.locatorExists(sibling)) {
          await this.#seam.trashLocator(sibling);
        }
      } catch {
        cleanupFailure = "device_apply_vault_failed";
      }
    }
    return cleanupFailure;
  }

  async #recoverPrepared(operation: RemoteApplyOperation): Promise<RemoteApplyRecovery> {
    const blocked = { kind: "blocked", reason: "device_apply_recovery_ambiguous" } as const;
    const target = contentTargetOf(operation);
    if (target !== null) {
      const cleanupFailure = await this.#cleanSiblingsOf(operation);
      const targetBytes = await this.#seam.readBytes(target);
      if (operation.operation === "updated") {
        if (await hashesTo(targetBytes, operation.baseFingerprint)) {
          return { kind: "clean", eventSequence: operation.eventSequence, cleanupFailure };
        }
      }
      if (await hashesTo(targetBytes, operation.finalFingerprint)) {
        return {
          kind: "mutated",
          eventSequence: operation.eventSequence,
          verifiedFingerprint: operation.finalFingerprint,
          rollbackToken: operation.tempToken,
          cleanupFailure,
        };
      }
      return blocked;
    }
    if (operation.operation === "deleted") {
      const priorLocator = operation.priorLocator ?? "";
      const priorBytes = await this.#seam.readBytes(priorLocator);
      if (priorBytes === null) {
        return {
          kind: "mutated",
          eventSequence: operation.eventSequence,
          verifiedFingerprint: null,
          rollbackToken: null,
          cleanupFailure: null,
        };
      }
      if (
        operation.baseFingerprint === null ||
        (await hashesTo(priorBytes, operation.baseFingerprint))
      ) {
        return { kind: "clean", eventSequence: operation.eventSequence, cleanupFailure: null };
      }
      return blocked;
    }
    // renamed / moved: prove which side of the rename the Vault sits on.
    const priorLocator = operation.priorLocator ?? "";
    const targetLocator = operation.targetLocator ?? "";
    const priorExists = await this.#seam.locatorExists(priorLocator);
    const targetExists = await this.#seam.locatorExists(targetLocator);
    if (!priorExists && targetExists) {
      if (await hashesTo(await this.#seam.readBytes(targetLocator), operation.finalFingerprint)) {
        return {
          kind: "mutated",
          eventSequence: operation.eventSequence,
          verifiedFingerprint: operation.finalFingerprint,
          rollbackToken: null,
          cleanupFailure: null,
        };
      }
      return blocked;
    }
    if (priorExists && !targetExists) {
      if (await hashesTo(await this.#seam.readBytes(priorLocator), operation.finalFingerprint)) {
        return { kind: "clean", eventSequence: operation.eventSequence, cleanupFailure: null };
      }
      return blocked;
    }
    return blocked;
  }

  async #recoverTempVerified(operation: RemoteApplyOperation): Promise<RemoteApplyRecovery> {
    if (operation.operation !== "created" && operation.operation !== "updated") {
      // The lattice never persists `restored` at temp_verified: an
      // impossible durable image fails closed with the bytes preserved.
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    const target = contentTargetOf(operation);
    if (target === null || operation.tempToken === null) {
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    const tempLocator = buildTempSiblingLocator(target, operation.tempToken);
    const rollbackLocator = buildRollbackSiblingLocator(target, operation.tempToken);
    const tempExists = await this.#seam.locatorExists(tempLocator);
    const targetBytes = await this.#seam.readBytes(target);

    if (targetBytes !== null && (await hashesTo(targetBytes, operation.finalFingerprint))) {
      // The replace completed; only the durable proof is missing.
      return {
        kind: "mutated",
        eventSequence: operation.eventSequence,
        verifiedFingerprint: operation.finalFingerprint,
        rollbackToken: operation.tempToken,
        cleanupFailure: await this.#cleanSiblingsOf(operation),
      };
    }

    if (targetBytes !== null && (await hashesTo(targetBytes, operation.baseFingerprint))) {
      // The mutation never happened: the verified temp is durably staged.
      if (!tempExists) {
        return {
          kind: "restored",
          eventSequence: operation.eventSequence,
          reason: "device_apply_vault_failed",
          cleanupFailure: null,
        };
      }
      return this.#resumeReplace(operation, target, tempLocator, rollbackLocator);
    }

    if (targetBytes !== null) {
      // Unverifiable bytes at the target: restore the old bytes when the
      // rollback sibling survived, otherwise preserve and block.
      if (await this.#seam.locatorExists(rollbackLocator)) {
        if (await this.#restoreRollback(target, rollbackLocator, operation.baseFingerprint)) {
          return {
            kind: "restored",
            eventSequence: operation.eventSequence,
            reason: "device_apply_vault_failed",
            cleanupFailure: null,
          };
        }
      }
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }

    // The target is absent: the staged temp decides between resume and restore.
    if (tempExists) {
      return this.#resumeReplace(operation, target, tempLocator, rollbackLocator);
    }
    if (await this.#seam.locatorExists(rollbackLocator)) {
      if (await this.#restoreRollback(target, rollbackLocator, operation.baseFingerprint)) {
        return {
          kind: "restored",
          eventSequence: operation.eventSequence,
          reason: "device_apply_vault_failed",
          cleanupFailure: null,
        };
      }
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    if (operation.operation === "created") {
      // Nothing ever existed at the target and the staged bytes are gone:
      // the event settles as a conflict, never a guessed mutation.
      return {
        kind: "restored",
        eventSequence: operation.eventSequence,
        reason: "device_apply_vault_failed",
        cleanupFailure: null,
      };
    }
    // An updated target that vanished with no evidence is foreign divergence.
    return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
  }

  /** Resume the narrow replace from the durably verified staging bytes. */
  async #resumeReplace(
    operation: RemoteApplyOperation,
    target: string,
    tempLocator: string,
    rollbackLocator: string,
  ): Promise<RemoteApplyRecovery> {
    const isUpdate = operation.operation === "updated";
    try {
      if (isUpdate && (await this.#seam.locatorExists(target))) {
        await this.#seam.renameLocator(target, rollbackLocator);
      }
      await this.#seam.renameLocator(tempLocator, target);
    } catch (error) {
      throw this.#wrap(error, "recovery", "device_apply_vault_failed", true);
    }
    const finalBytes = await this.#seam.readBytes(target);
    if (!(await hashesTo(finalBytes, operation.finalFingerprint))) {
      if (isUpdate && (await this.#seam.locatorExists(rollbackLocator))) {
        if (await this.#restoreRollback(target, rollbackLocator, operation.baseFingerprint)) {
          return {
            kind: "restored",
            eventSequence: operation.eventSequence,
            reason: "device_apply_vault_failed",
            cleanupFailure: null,
          };
        }
      }
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    return {
      kind: "mutated",
      eventSequence: operation.eventSequence,
      verifiedFingerprint: operation.finalFingerprint,
      rollbackToken: operation.tempToken,
      cleanupFailure: await this.#cleanSiblingsOf(operation),
    };
  }

  async #recoverVaultMutated(operation: RemoteApplyOperation): Promise<RemoteApplyRecovery> {
    const cleanupFailure = await this.#cleanSiblingsOf(operation);
    if (operation.operation === "deleted") {
      return {
        kind: "mutated",
        eventSequence: operation.eventSequence,
        verifiedFingerprint: null,
        rollbackToken: null,
        cleanupFailure,
      };
    }
    const proofLocator =
      operation.operation === "renamed" || operation.operation === "moved"
        ? operation.targetLocator
        : contentTargetOf(operation);
    const proofBytes = proofLocator === null ? null : await this.#seam.readBytes(proofLocator);
    if (!(await hashesTo(proofBytes, operation.finalFingerprint))) {
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    return {
      kind: "mutated",
      eventSequence: operation.eventSequence,
      verifiedFingerprint: operation.finalFingerprint,
      rollbackToken: operation.tempToken,
      cleanupFailure,
    };
  }
}
