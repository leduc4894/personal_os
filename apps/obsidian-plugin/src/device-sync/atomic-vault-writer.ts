/**
 * The atomic Vault writer (device cursor and manifest reconciliation,
 * task 10, spec 8.1, 11).
 *
 * Content applies prove the writer's own target-shape discipline first
 * (an occupied target for `updated`, an absent target for
 * `created`/`restored`), then delegate the whole byte discipline — stage
 * verified bytes in a SAME-DIRECTORY hidden temporary sibling, verify
 * them, prove the occupied target's pinned base, narrow-replace with a
 * retained rollback sibling, verify the final bytes — to the shared
 * mutation primitive (`stageVerifyAndReplaceVaultContent`). The durable
 * `temp_verified` transition stays the writer's own: it glues to the
 * primitive's FIRST VISIBLE mutation (immediately before the first
 * rename), so a crash before that point still finds the durable row at
 * `prepared` while every crash after it resumes from the verified
 * staging bytes. `vault_mutated` is left to the caller's single
 * transition. Locator applies rename after proving the prior bytes and
 * an unoccupied target; tombstones trash through the Vault trash path —
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
import {
  AtomicVaultMutationFailure,
  buildRollbackSiblingLocator,
  buildTempSiblingLocator,
  stageVerifyAndReplaceVaultContent,
} from "./atomic-vault-mutation";
import type { AtomicVaultMutationResult } from "./atomic-vault-mutation";
import type {
  ApplyFailureStage,
  DeviceSyncReason,
  DeviceSyncRepository,
  RemoteApplyOperation,
} from "./contracts";

// --- the frozen sibling naming ------------------------------------------------------------------------

// The sibling naming lives in the shared mutation primitive this writer
// consumes; the re-export keeps this module's public surface unchanged.
export { buildRollbackSiblingLocator, buildTempSiblingLocator };

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

/**
 * The raw filesystem slice of the Obsidian data adapter for the writer's
 * hidden siblings. The live Desktop gate proved the Vault's own index never
 * sees dot-prefixed paths (`createBinary` succeeds, `getAbstractFileByPath`
 * stays null, `readBinary` throws), so every hidden-sibling staging step
 * must ride the adapter — the one surface that can see them.
 */
export interface StructuralVaultAdapterSurface {
  exists(path: string): Promise<boolean>;
  readBinary(path: string): Promise<ArrayBuffer>;
  writeBinary(path: string, data: ArrayBuffer): Promise<void>;
  rename(fromPath: string, toPath: string): Promise<void>;
  remove(path: string): Promise<void>;
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
 * Whether one locator is one of the writer's own hidden siblings: the Vault
 * index never lists dot-prefixed paths, so they ride the adapter instead.
 * (The base-name slice is inlined here on purpose — the shared sibling
 * naming, including its own base-name helper, lives in the primitive.)
 */
function isHiddenSiblingLocator(locator: string): boolean {
  const lastSlash = locator.lastIndexOf("/");
  const baseName = lastSlash === -1 ? locator : locator.slice(lastSlash + 1);
  return baseName.startsWith(".");
}

/**
 * Bind the mutation seam to the structural Obsidian `Vault` surface. Every
 * removal of VISIBLE content goes through `Vault.trash(file, false)` — the
 * system trash is never used and no permanent-delete method exists for it.
 * The writer's own dot-prefixed hidden siblings were proven invisible to
 * the Vault index on the live wire: staging, verifying, renaming and
 * cleaning them ride the raw data adapter instead (an internal staging
 * file is not user content and has no Vault trash surface).
 */
export function createStructuralVaultMutationSeam(
  vault: StructuralVaultSurface,
  adapter?: StructuralVaultAdapterSurface,
): VaultMutationSeam {
  const adapterOf = (): StructuralVaultAdapterSurface => {
    if (adapter === undefined) {
      throw writerError("vault_mutation", "device_apply_vault_failed", true);
    }
    return adapter;
  };
  return {
    async locatorExists(locator: string): Promise<boolean> {
      if (isHiddenSiblingLocator(locator)) {
        return await adapterOf().exists(locator);
      }
      if (vault.getAbstractFileByPath(locator) !== null) {
        return true;
      }
      // The Vault index lags adapter-level renames on the real Electron
      // wire (the live Desktop gate proved a just-renamed-in target stays
      // index-invisible for a moment), so the authoritative existence
      // check for a visible locator is the raw adapter whenever one is
      // bound; without an adapter the index remains the only truth.
      return adapter === undefined ? false : await adapter.exists(locator);
    },

    async createFile(locator: string, bytes: Uint8Array): Promise<void> {
      if (isHiddenSiblingLocator(locator)) {
        await adapterOf().writeBinary(locator, toArrayBuffer(bytes));
        return;
      }
      await vault.createBinary(locator, toArrayBuffer(bytes));
    },

    async readBytes(locator: string): Promise<Uint8Array | null> {
      if (isHiddenSiblingLocator(locator)) {
        if (!(await adapterOf().exists(locator))) {
          return null;
        }
        return new Uint8Array(await adapterOf().readBinary(locator));
      }
      if (vault.getAbstractFileByPath(locator) !== null) {
        return new Uint8Array(await vault.readBinary(locator));
      }
      // The index-lagged read of a just-renamed-in target: the bytes on
      // disk are the truth, so the adapter serves the read whenever one is
      // bound (the live Desktop gate proved the final verification of an
      // adapter rename otherwise fails the first attempt and forces the
      // retry/recovery lattice to unstick it).
      if (adapter === undefined) {
        return null;
      }
      if (!(await adapter.exists(locator))) {
        return null;
      }
      return new Uint8Array(await adapter.readBinary(locator));
    },

    async renameLocator(fromLocator: string, toLocator: string): Promise<void> {
      if (isHiddenSiblingLocator(fromLocator) || isHiddenSiblingLocator(toLocator)) {
        // The narrow replace's staging edges (temp -> target, target ->
        // rollback): one side is a hidden sibling the Vault index cannot
        // name, so the rename rides the adapter for the whole edge.
        await adapterOf().rename(fromLocator, toLocator);
        return;
      }
      const file = vault.getAbstractFileByPath(fromLocator);
      if (file === null) {
        throw writerError("vault_mutation", "device_apply_vault_failed", true);
      }
      await vault.rename(file, toLocator);
    },

    async trashLocator(locator: string): Promise<void> {
      if (isHiddenSiblingLocator(locator)) {
        // An internal staging sibling is not user content and was never
        // Vault-visible: adapter removal is the only truthful cleanup.
        await adapterOf().remove(locator);
        return;
      }
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

    // 1. The writer's own target-shape discipline — STRICTER than the
    //    primitive's occupied-target base proof and retained here on
    //    purpose: an updated apply demands an OCCUPIED target (absence
    //    is local divergence), a created or restored apply demands an
    //    ABSENT target. The occupancy proof is existence-only; the
    //    pinned-base byte proof rides the primitive's prove-base step
    //    so the target keeps exactly one pre-replace byte read, and the
    //    refusal lands before anything is staged.
    const isUpdate = input.operation === "updated";
    if (isUpdate) {
      if (!(await this.#seam.locatorExists(target))) {
        throw writerError("vault_mutation", "device_manifest_local_diverged", false);
      }
    } else if (await this.#seam.locatorExists(target)) {
      throw writerError("vault_mutation", "device_manifest_target_occupied", false);
    }

    // 2. The durable temp_verified proof (content operations only — the
    //    lattice bars `restored` from the temp state) must land BETWEEN
    //    the staged-bytes verification and the first visible mutation,
    //    the same durable point the inline discipline used: a crash
    //    before it finds the row at `prepared` (abandon + clean), a
    //    crash after it resumes from the verified staging bytes. The
    //    primitive owns staging, verification and replace as ONE byte
    //    discipline, so the proof glues to the seam: the sequencing
    //    wrapper below fires the transition immediately before the
    //    primitive's first rename — always the first VISIBLE mutation —
    //    and never before the staged bytes were written and verified.
    let durableProofFailure: unknown = null;
    let hasDurableProof = input.operation === "restored";
    const seam = this.#seam;
    const sequencedSeam: VaultMutationSeam = {
      locatorExists: (locator) => seam.locatorExists(locator),
      createFile: (locator, bytes) => seam.createFile(locator, bytes),
      readBytes: async (locator) => {
        const bytes = await seam.readBytes(locator);
        if (bytes === null && !hasDurableProof && isUpdate && locator === target) {
          // The target of an UPDATED apply vanished mid-apply — between
          // the occupied-target shape check and the base proof, a window
          // that spans the whole staging write. The primitive would take
          // the created shape and silently skip the pinned-base proof;
          // refuse instead, exactly the divergence the base fingerprint
          // exists to catch. The !hasDurableProof gate matches only the
          // prove-base read: every later read of the target follows the
          // first rename (and the durable proof), and the primitive owns
          // the null handling of those. The throw maps to prove_base →
          // the divergence refusal below.
          throw writerError("vault_mutation", "device_manifest_local_diverged", false);
        }
        return bytes;
      },
      trashLocator: (locator) => seam.trashLocator(locator),
      renameLocator: async (fromLocator, toLocator) => {
        if (!hasDurableProof) {
          hasDurableProof = true;
          try {
            await this.#repository.transitionRemoteApply({
              eventSequence: input.eventSequence,
              state: "temp_verified",
              tempToken: input.tempToken,
            });
          } catch (error) {
            // Refuse the visible mutation; the mapped refusal surfaces
            // below with the writer's own verify_temp/store-reason
            // mapping, never as a replace failure.
            durableProofFailure = error;
            throw error;
          }
        }
        await seam.renameLocator(fromLocator, toLocator);
      },
    };

    // 3. The shared byte discipline owns every seam mutation from here:
    //    stage the hidden sibling (cleaning a stale sibling of exactly
    //    this token first), verify the staged bytes, prove the occupied
    //    target's pinned base, narrow-replace with retained rollback
    //    evidence, verify the final bytes — restoring the verified old
    //    bytes on mismatch. The base proof stays an updated-only
    //    discipline: created/restored targets were proven absent above.
    let mutated: AtomicVaultMutationResult;
    try {
      mutated = await stageVerifyAndReplaceVaultContent({
        seam: sequencedSeam,
        targetLocator: target,
        tempToken: input.tempToken,
        bytes: input.bytes,
        expectedFinalFingerprint: input.expectedFinalFingerprint,
        expectedBaseFingerprint: isUpdate ? input.baseFingerprint : null,
      });
    } catch (error) {
      if (durableProofFailure !== null) {
        const reason = storeReasonOf(durableProofFailure) ?? "device_apply_vault_failed";
        throw writerError("verify_temp", reason, false);
      }
      throw this.#mapMutationFailure(error);
    }

    return {
      targetLocator: target,
      verifiedFingerprint: input.expectedFinalFingerprint,
      tempToken: input.tempToken,
      rollbackToken: mutated.rollbackLocator !== null ? input.tempToken : null,
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

  /**
   * Map the shared primitive's private typed failure onto the writer's
   * closed stage/reason vocabulary — no new public tokens, the same
   * pairs the inline discipline raised. A prove-base refusal keeps the
   * divergence token (the applier settles it durably as a conflict);
   * the staged-bytes and replace refusals stay retryable vault failures.
   */
  #mapMutationFailure(error: unknown): AtomicVaultWriterError {
    if (error instanceof AtomicVaultMutationFailure) {
      switch (error.stage) {
        case "stage":
        case "verify_staged":
          return writerError("verify_temp", "device_apply_vault_failed", true);
        case "prove_base":
          return writerError("vault_mutation", "device_manifest_local_diverged", false);
        case "replace":
          return writerError("vault_mutation", "device_apply_vault_failed", true);
        case "verify_final":
          return writerError(
            "verify_final",
            "device_apply_vault_failed",
            false,
            error.restoredToBase,
          );
      }
    }
    // The primitive's contract allows no other escape; a foreign throw
    // still surfaces through the closed vault failure token, never raw.
    return writerError("vault_mutation", "device_apply_vault_failed", true);
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
      // A created/restored apply whose target is STILL ABSENT sits at its
      // verified pre-mutation expectation — the crash happened before any
      // rename-in (a completed rename-in would leave the final bytes,
      // caught above), so the event awaits redelivery. An updated apply
      // with an absent target is genuine divergence and stays blocked.
      if (operation.operation !== "updated" && targetBytes === null) {
        return { kind: "clean", eventSequence: operation.eventSequence, cleanupFailure };
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
