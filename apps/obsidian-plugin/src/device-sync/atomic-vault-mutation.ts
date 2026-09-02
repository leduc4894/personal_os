/**
 * The plugin-internal atomic Vault mutation primitive (conflict vault-apply
 * hardening, task 1): the one stage/verify/replace byte discipline the
 * device-sync writer and the conflict canonical applier share.
 *
 * Ordering (mirrors AtomicVaultWriterImpl): stage the bytes in the
 * same-directory hidden temporary sibling, verify them against the expected
 * final fingerprint, prove the optional base condition of an occupied
 * target, narrow-replace while retaining the verified rollback bytes,
 * verify the final bytes (restoring the verified old bytes when that proof
 * fails), and clean up only the exact opaque-token siblings the caller's
 * token names — never a scan, prefix or glob.
 *
 * The base proof binds only an OCCUPIED target: a pinned
 * `expectedBaseFingerprint` proves the target's current bytes, while the
 * policy for an absent target (divergence, creation) belongs to the
 * caller's own target-shape checks. A null base skips the proof entirely.
 *
 * The module is Obsidian-agnostic (it drives the injected
 * {@link VaultMutationSeam}) and owns no durable state: the durable
 * temp_verified transition stays with the device-sync writer, and the
 * retained rollback sibling of a SUCCESSFUL replace is left for the
 * caller's own recovery window (`rollbackLocator` names it).
 *
 * Privacy (spec 9, 14): the only failure is the private typed
 * {@link AtomicVaultMutationFailure} whose message carries the closed
 * stage token alone — locators, bytes and digests never reach a thrown
 * message, a log, or any diagnostics path.
 */

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { FrozenFingerprint } from "../journal/contracts";
import type { VaultMutationSeam } from "./atomic-vault-writer";

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

// --- the mutation input and outcome --------------------------------------------------------------------

export interface AtomicVaultMutationInput {
  readonly seam: VaultMutationSeam;
  readonly targetLocator: string;
  readonly tempToken: string;
  readonly bytes: Uint8Array;
  readonly expectedFinalFingerprint: FrozenFingerprint;
  /** The pinned base fingerprint of an occupied target; null skips the base proof. */
  readonly expectedBaseFingerprint: FrozenFingerprint | null;
}

export interface AtomicVaultMutationResult {
  /** The retained rollback sibling of a successful replace, or null when none was created. */
  readonly rollbackLocator: string | null;
  readonly restoredToBase: boolean;
}

/** The exact hidden siblings one target locator and opaque staging token name. */
export interface VaultSiblingCleanupInput {
  readonly targetLocator: string;
  readonly tempToken: string;
}

// --- the private typed failure -------------------------------------------------------------------------

/** The closed internal stage where the mutation discipline refused. */
export type AtomicVaultMutationStage =
  | "stage"
  | "verify_staged"
  | "prove_base"
  | "replace"
  | "verify_final";

/**
 * The private typed failure callers map onto their own closed error
 * vocabularies (the writer's stage/reason pairs, the conflict applier's
 * `vault_apply`). The message is the closed stage token alone; it never
 * carries a locator, bytes or a digest.
 */
export class AtomicVaultMutationFailure extends Error {
  readonly stage: AtomicVaultMutationStage;
  readonly restoredToBase: boolean;

  constructor(stage: AtomicVaultMutationStage, restoredToBase: boolean) {
    super(`atomic vault mutation failed: ${stage}`);
    this.name = "AtomicVaultMutationFailure";
    this.stage = stage;
    this.restoredToBase = restoredToBase;
  }
}

/** Whether the bytes hash to exactly the pinned fingerprint (null bytes never match). */
async function hashesTo(
  bytes: Uint8Array | null,
  fingerprint: FrozenFingerprint | null,
): Promise<boolean> {
  if (bytes === null || fingerprint === null) {
    return false;
  }
  return bytes.byteLength === fingerprint.sizeBytes && (await sha256Hex(bytes)) === fingerprint.sha256;
}

/** Best-effort trash of one sibling; the failure path it serves is never blocked by it. */
async function trashQuietly(seam: VaultMutationSeam, locator: string): Promise<void> {
  try {
    await seam.trashLocator(locator);
  } catch {
    // A leftover hidden sibling is not data loss; the refusing path still
    // surfaces through its own closed stage.
  }
}

/** Restore the verified old bytes from the rollback sibling; true only when the base proof passes again. */
async function restoreVerifiedOldBytes(
  seam: VaultMutationSeam,
  targetLocator: string,
  rollbackLocator: string,
  expectedBaseFingerprint: FrozenFingerprint | null,
): Promise<boolean> {
  try {
    await seam.trashLocator(targetLocator);
    await seam.renameLocator(rollbackLocator, targetLocator);
  } catch {
    return false;
  }
  let restoredBytes: Uint8Array | null;
  try {
    restoredBytes = await seam.readBytes(targetLocator);
  } catch {
    // An unreadable restoration cannot be proven — the bytes stay as they
    // sit and the caller maps the closed verify_final failure.
    return false;
  }
  if (expectedBaseFingerprint === null) {
    return restoredBytes !== null;
  }
  return hashesTo(restoredBytes, expectedBaseFingerprint);
}

// --- the primitive -------------------------------------------------------------------------------------

/**
 * Stage, verify and narrowly replace one target's bytes through the seam:
 * the verified staged sibling replaces the target while the verified old
 * bytes move to the retained rollback sibling, and a failed final proof
 * restores them before the closed failure is thrown. The retained rollback
 * sibling of a successful replace is reported for the caller's own
 * recovery window — it is NOT cleaned here.
 */
export async function stageVerifyAndReplaceVaultContent(
  input: AtomicVaultMutationInput,
): Promise<AtomicVaultMutationResult> {
  const { seam, targetLocator, tempToken } = input;
  const tempLocator = buildTempSiblingLocator(targetLocator, tempToken);

  // 1. Stage the bytes in the same-directory hidden sibling — after
  //    cleaning a stale sibling of exactly this token — and verify them
  //    against the expected final fingerprint.
  try {
    if (await seam.locatorExists(tempLocator)) {
      await seam.trashLocator(tempLocator);
    }
    await seam.createFile(tempLocator, input.bytes);
  } catch {
    throw new AtomicVaultMutationFailure("stage", false);
  }
  let stagedBytes: Uint8Array | null;
  try {
    stagedBytes = await seam.readBytes(tempLocator);
  } catch {
    throw new AtomicVaultMutationFailure("verify_staged", false);
  }
  if (!(await hashesTo(stagedBytes, input.expectedFinalFingerprint))) {
    await trashQuietly(seam, tempLocator);
    throw new AtomicVaultMutationFailure("verify_staged", false);
  }

  // 2. Prove the target/base condition: a pinned base proves the current
  //    bytes of an occupied target (null skips); an absent target simply
  //    takes the created shape with no rollback evidence.
  let currentBytes: Uint8Array | null;
  try {
    currentBytes = await seam.readBytes(targetLocator);
  } catch {
    throw new AtomicVaultMutationFailure("prove_base", false);
  }
  const hasTarget = currentBytes !== null;
  if (
    hasTarget &&
    input.expectedBaseFingerprint !== null &&
    !(await hashesTo(currentBytes, input.expectedBaseFingerprint))
  ) {
    await trashQuietly(seam, tempLocator);
    throw new AtomicVaultMutationFailure("prove_base", false);
  }

  // 3. The narrow replace with retained rollback evidence: the old bytes
  //    move to the rollback sibling, the verified temp moves in.
  const rollbackLocator = hasTarget ? buildRollbackSiblingLocator(targetLocator, tempToken) : null;
  try {
    if (rollbackLocator !== null) {
      await seam.renameLocator(targetLocator, rollbackLocator);
    }
    await seam.renameLocator(tempLocator, targetLocator);
  } catch {
    throw new AtomicVaultMutationFailure("replace", false);
  }

  // 4. Verify the final bytes; on mismatch restore the verified old bytes
  //    through the rollback sibling.
  let finalBytes: Uint8Array | null;
  try {
    finalBytes = await seam.readBytes(targetLocator);
  } catch {
    throw new AtomicVaultMutationFailure("verify_final", false);
  }
  if (!(await hashesTo(finalBytes, input.expectedFinalFingerprint))) {
    const restoredToBase =
      rollbackLocator !== null
        ? await restoreVerifiedOldBytes(
            seam,
            targetLocator,
            rollbackLocator,
            input.expectedBaseFingerprint,
          )
        : false;
    throw new AtomicVaultMutationFailure("verify_final", restoredToBase);
  }

  return { rollbackLocator, restoredToBase: false };
}

/**
 * Clean the exact hidden siblings one opaque token names for one target
 * locator — the temporary and the rollback sibling of that token, nothing
 * else. There is deliberately NO scan, prefix or glob: a sibling another
 * token owns always survives untouched. The cleanup is best-effort and
 * never throws; it returns whether every owned sibling is gone so the
 * caller can surface a refusal through its own closed diagnostics path.
 */
export async function cleanupExactVaultSiblings(
  seam: VaultMutationSeam,
  input: VaultSiblingCleanupInput,
): Promise<boolean> {
  const ownedSiblings = [
    buildTempSiblingLocator(input.targetLocator, input.tempToken),
    buildRollbackSiblingLocator(input.targetLocator, input.tempToken),
  ];
  let hasCleanupFailure = false;
  for (const sibling of ownedSiblings) {
    try {
      if (await seam.locatorExists(sibling)) {
        await seam.trashLocator(sibling);
      }
    } catch {
      hasCleanupFailure = true;
    }
  }
  return !hasCleanupFailure;
}
