/**
 * Tests of the internal atomic Vault mutation primitive (conflict
 * vault-apply hardening, task 1).
 *
 * The primitive owns the shared stage/verify/replace byte discipline over
 * an injected {@link VaultMutationSeam}: stage the bytes in the
 * same-directory hidden temporary sibling, verify them against the
 * expected final fingerprint, prove the optional pinned base of an
 * occupied target, narrow-replace while retaining the verified rollback
 * bytes, and verify the final bytes (restoring the old bytes when the
 * proof fails). Exact-token cleanup removes only the siblings the opaque
 * token names — a sibling with a different token always survives. Every
 * failure is the private typed failure whose message carries only the
 * closed stage token: locators, bytes and digests never leak.
 */

import { describe, expect, it } from "vitest";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import type { FrozenFingerprint } from "../journal/contracts";
import type { VaultMutationSeam } from "./atomic-vault-writer";
import {
  buildRollbackSiblingLocator,
  buildTempSiblingLocator,
  cleanupExactVaultSiblings,
  stageVerifyAndReplaceVaultContent,
} from "./atomic-vault-mutation";
import type { AtomicVaultMutationFailure } from "./atomic-vault-mutation";

const TARGET_LOCATOR = "notes/a.md";
const TEMP_TOKEN = "owned-token";

function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function decodedTextOf(bytes: Uint8Array | undefined): string {
  return new TextDecoder().decode(bytes ?? new Uint8Array());
}

async function fingerprintOf(bytes: Uint8Array): Promise<FrozenFingerprint> {
  return {
    sha256: await sha256Hex(bytes),
    sizeBytes: bytes.byteLength,
    mediaType: "text/markdown",
  };
}

/**
 * The fake Vault seam: an in-memory regular-file store whose reads can be
 * corrupted at one exact ordinal so tests can fail any single
 * verification step, and whose trash can be refused per locator so the
 * best-effort cleanup contract stays pinnable.
 */
class FakeVaultSeam implements VaultMutationSeam {
  readonly files = new Map<string, Uint8Array>();
  readonly trashLog: string[] = [];
  /** Read ordinal (1-based) at which each locator's one read turns corrupted. */
  readonly corruptReadsFrom = new Map<string, number>();
  /** Locators whose trash is refused (the cleanup-failure path). */
  readonly trashRefusedFor = new Set<string>();
  readonly #readCounts = new Map<string, number>();

  async locatorExists(locator: string): Promise<boolean> {
    return this.files.has(locator);
  }

  async createFile(locator: string, bytes: Uint8Array): Promise<void> {
    if (this.files.has(locator)) {
      throw new Error(`seam refuses to create over ${locator}`);
    }
    this.files.set(locator, bytes);
  }

  async readBytes(locator: string): Promise<Uint8Array | null> {
    const bytes = this.files.get(locator);
    if (bytes === undefined) {
      return null;
    }
    const readCount = (this.#readCounts.get(locator) ?? 0) + 1;
    this.#readCounts.set(locator, readCount);
    const corruptFrom = this.corruptReadsFrom.get(locator);
    if (corruptFrom !== undefined && readCount === corruptFrom) {
      return new Uint8Array([...bytes, ...bytesOf("-corrupted")]);
    }
    return bytes;
  }

  async renameLocator(fromLocator: string, toLocator: string): Promise<void> {
    const bytes = this.files.get(fromLocator);
    if (bytes === undefined) {
      throw new Error(`seam cannot rename absent ${fromLocator}`);
    }
    this.files.delete(fromLocator);
    this.files.set(toLocator, bytes);
  }

  async trashLocator(locator: string): Promise<void> {
    if (this.trashRefusedFor.has(locator)) {
      throw new Error("seam refuses the trash");
    }
    if (!this.files.delete(locator)) {
      throw new Error(`seam cannot trash absent ${locator}`);
    }
    this.trashLog.push(locator);
  }
}

function failureOf(promise: Promise<unknown>): Promise<AtomicVaultMutationFailure> {
  return promise.then(
    () => {
      throw new Error("expected the mutation primitive to reject");
    },
    (error: unknown) => {
      if (!(error instanceof Error && "stage" in error && "restoredToBase" in error)) {
        throw new Error(`expected an AtomicVaultMutationFailure, got ${String(error)}`);
      }
      return error as AtomicVaultMutationFailure;
    },
  );
}

// --- stage, verify and narrow-replace -------------------------------------------------------------------

describe("AtomicVaultMutation stage, verify and narrow-replace", () => {
  it("replaces into an absent target with no rollback sibling and cleans only its own token", async () => {
    const seam = new FakeVaultSeam();
    const foreignSibling = "notes/.a.md.device-sync-tmp-other-token";
    seam.files.set(foreignSibling, bytesOf("foreign staged bytes"));

    const result = await stageVerifyAndReplaceVaultContent({
      seam,
      targetLocator: TARGET_LOCATOR,
      tempToken: TEMP_TOKEN,
      bytes: bytesOf("remote bytes"),
      expectedFinalFingerprint: await fingerprintOf(bytesOf("remote bytes")),
      expectedBaseFingerprint: await fingerprintOf(bytesOf("local bytes")),
    });

    // An absent target names no rollback sibling, and the consumed
    // temporary sibling is already gone through the rename-in.
    expect(result.rollbackLocator).toBeNull();
    expect(result.restoredToBase).toBe(false);
    expect(decodedTextOf(seam.files.get(TARGET_LOCATOR))).toBe("remote bytes");

    const isClean = await cleanupExactVaultSiblings(seam, {
      targetLocator: TARGET_LOCATOR,
      tempToken: TEMP_TOKEN,
    });
    expect(isClean).toBe(true);
    expect(await seam.locatorExists(buildTempSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN))).toBe(false);
    // The sibling another token owns survives the cleanup untouched.
    expect(await seam.locatorExists(foreignSibling)).toBe(true);
  });

  it("retains the verified old bytes in the rollback sibling of an occupied target", async () => {
    const seam = new FakeVaultSeam();
    seam.files.set(TARGET_LOCATOR, bytesOf("local bytes"));

    const result = await stageVerifyAndReplaceVaultContent({
      seam,
      targetLocator: TARGET_LOCATOR,
      tempToken: TEMP_TOKEN,
      bytes: bytesOf("remote bytes"),
      expectedFinalFingerprint: await fingerprintOf(bytesOf("remote bytes")),
      expectedBaseFingerprint: await fingerprintOf(bytesOf("local bytes")),
    });

    const rollbackLocator = buildRollbackSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN);
    expect(result.rollbackLocator).toBe(rollbackLocator);
    expect(decodedTextOf(seam.files.get(TARGET_LOCATOR))).toBe("remote bytes");
    expect(decodedTextOf(seam.files.get(rollbackLocator))).toBe("local bytes");
    expect(seam.files.has(buildTempSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN))).toBe(false);
  });

  it("skips the base proof when expectedBaseFingerprint is null", async () => {
    const seam = new FakeVaultSeam();
    seam.files.set(TARGET_LOCATOR, bytesOf("unpinned current bytes"));

    const result = await stageVerifyAndReplaceVaultContent({
      seam,
      targetLocator: TARGET_LOCATOR,
      tempToken: TEMP_TOKEN,
      bytes: bytesOf("remote bytes"),
      expectedFinalFingerprint: await fingerprintOf(bytesOf("remote bytes")),
      expectedBaseFingerprint: null,
    });

    // No base proof means an occupied target of ANY current shape still
    // replaces; its old bytes stay retained in the rollback sibling.
    expect(result.rollbackLocator).toBe(buildRollbackSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN));
    expect(decodedTextOf(seam.files.get(TARGET_LOCATOR))).toBe("remote bytes");
    expect(
      decodedTextOf(seam.files.get(buildRollbackSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN))),
    ).toBe("unpinned current bytes");
  });

  it("restores the verified old bytes when the final verification fails", async () => {
    const seam = new FakeVaultSeam();
    seam.files.set(TARGET_LOCATOR, bytesOf("local bytes"));
    // The target's reads: the base proof first, the FINAL readback second
    // — the second one returns corrupted bytes.
    seam.corruptReadsFrom.set(TARGET_LOCATOR, 2);

    const failure = await failureOf(
      stageVerifyAndReplaceVaultContent({
        seam,
        targetLocator: TARGET_LOCATOR,
        tempToken: TEMP_TOKEN,
        bytes: bytesOf("remote bytes"),
        expectedFinalFingerprint: await fingerprintOf(bytesOf("remote bytes")),
        expectedBaseFingerprint: await fingerprintOf(bytesOf("local bytes")),
      }),
    );

    expect(failure.stage).toBe("verify_final");
    expect(failure.restoredToBase).toBe(true);
    // The closed message names only the stage token — no locator, bytes
    // or digest ever reaches it.
    expect(failure.message).toBe("atomic vault mutation failed: verify_final");
    // The verified old bytes are back at the target; both siblings are gone.
    expect(decodedTextOf(seam.files.get(TARGET_LOCATOR))).toBe("local bytes");
    expect(seam.files.has(buildRollbackSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN))).toBe(false);
    expect(seam.files.has(buildTempSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN))).toBe(false);
  });

  it("keeps the ambiguous final bytes when no rollback evidence exists", async () => {
    const seam = new FakeVaultSeam();
    // The target's first READ with bytes present is the final readback of
    // the created shape (the absent-target prove read never counts).
    seam.corruptReadsFrom.set(TARGET_LOCATOR, 1);

    const failure = await failureOf(
      stageVerifyAndReplaceVaultContent({
        seam,
        targetLocator: TARGET_LOCATOR,
        tempToken: TEMP_TOKEN,
        bytes: bytesOf("remote bytes"),
        expectedFinalFingerprint: await fingerprintOf(bytesOf("remote bytes")),
        expectedBaseFingerprint: null,
      }),
    );

    expect(failure.stage).toBe("verify_final");
    expect(failure.restoredToBase).toBe(false);
    // Preservation of ambiguous bytes: the unverified target content is
    // kept, never guessed away.
    expect(seam.files.has(TARGET_LOCATOR)).toBe(true);
  });

  it("refuses an occupied target whose bytes diverge from the pinned base", async () => {
    const seam = new FakeVaultSeam();
    seam.files.set(TARGET_LOCATOR, bytesOf("diverged current bytes"));

    const failure = await failureOf(
      stageVerifyAndReplaceVaultContent({
        seam,
        targetLocator: TARGET_LOCATOR,
        tempToken: TEMP_TOKEN,
        bytes: bytesOf("remote bytes"),
        expectedFinalFingerprint: await fingerprintOf(bytesOf("remote bytes")),
        expectedBaseFingerprint: await fingerprintOf(bytesOf("local bytes")),
      }),
    );

    expect(failure.stage).toBe("prove_base");
    // Exact-staging cleanup: only the primitive's own sibling is trashed;
    // the diverged target bytes stay untouched.
    expect(seam.trashLog).toEqual([buildTempSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN)]);
    expect(decodedTextOf(seam.files.get(TARGET_LOCATOR))).toBe("diverged current bytes");
  });

  it("rejects staged bytes that fail the final fingerprint proof", async () => {
    const seam = new FakeVaultSeam();
    seam.corruptReadsFrom.set(buildTempSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN), 1);

    const failure = await failureOf(
      stageVerifyAndReplaceVaultContent({
        seam,
        targetLocator: TARGET_LOCATOR,
        tempToken: TEMP_TOKEN,
        bytes: bytesOf("remote bytes"),
        expectedFinalFingerprint: await fingerprintOf(bytesOf("remote bytes")),
        expectedBaseFingerprint: null,
      }),
    );

    expect(failure.stage).toBe("verify_staged");
    expect(seam.trashLog).toEqual([buildTempSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN)]);
    expect(seam.files.has(TARGET_LOCATOR)).toBe(false);
  });
});

// --- the exact-token sibling cleanup --------------------------------------------------------------------

describe("cleanupExactVaultSiblings exact-token scope", () => {
  it("removes the owned rollback sibling while other-token siblings survive", async () => {
    const seam = new FakeVaultSeam();
    const ownedRollback = buildRollbackSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN);
    const foreignRollback = buildRollbackSiblingLocator(TARGET_LOCATOR, "other-token");
    const foreignTemp = buildTempSiblingLocator(TARGET_LOCATOR, "other-token");
    seam.files.set(ownedRollback, bytesOf("owned old bytes"));
    seam.files.set(foreignRollback, bytesOf("foreign old bytes"));
    seam.files.set(foreignTemp, bytesOf("foreign staged bytes"));

    const isClean = await cleanupExactVaultSiblings(seam, {
      targetLocator: TARGET_LOCATOR,
      tempToken: TEMP_TOKEN,
    });

    expect(isClean).toBe(true);
    expect(seam.trashLog).toEqual([ownedRollback]);
    expect(await seam.locatorExists(foreignRollback)).toBe(true);
    expect(await seam.locatorExists(foreignTemp)).toBe(true);
  });

  it("stays best-effort and reports a refused removal without throwing", async () => {
    const seam = new FakeVaultSeam();
    const ownedRollback = buildRollbackSiblingLocator(TARGET_LOCATOR, TEMP_TOKEN);
    seam.files.set(ownedRollback, bytesOf("owned old bytes"));
    seam.trashRefusedFor.add(ownedRollback);

    const isClean = await cleanupExactVaultSiblings(seam, {
      targetLocator: TARGET_LOCATOR,
      tempToken: TEMP_TOKEN,
    });

    expect(isClean).toBe(false);
    expect(await seam.locatorExists(ownedRollback)).toBe(true);
  });
});
