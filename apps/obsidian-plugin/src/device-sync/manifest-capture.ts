/**
 * The stable ordered manifest capture (device cursor and manifest
 * reconciliation, task 11, spec 12.1, 6.3, 7.3).
 *
 * Capture enumerates the Vault's regular files in normalized-locator
 * order, fingerprints each file's settled current bytes, and emits the
 * entries in ordered pages of at most {@link MANIFEST_PAGE_ENTRIES} — every
 * page digested, and the whole run finalized, with the one versioned
 * canonical-JSON grammar. The final-digest grammar is the exact server wire
 * contract (RFC 8785 canonical JSON of
 * `{"pages":[{"digest","entries","page"}…],"version":1}` sorted by page
 * number), so the plugin and the server agree byte for byte; the page
 * digest binds the full submitted page body of the plugin's own grammar.
 *
 * Local entry IDs are opaque one-way digests of the normalized locator —
 * deterministic across a run's exact resume (a restart re-enumerates and
 * re-digests the identical page), never the raw locator itself, and never
 * rendered into any diagnostic surface. The run total is capped at
 * {@link MAX_MANIFEST_TOTAL_ENTRIES}: a Vault beyond the cap fails closed
 * as the non-retryable `device_manifest_capture_failed` before a single
 * page is emitted. Entries carry the frozen barrier generation G; a file
 * that changes during enumeration is NOT re-frozen — the later watcher
 * observation keeps its own generation greater than G and every action
 * rechecks the current bytes before any mutation (spec 12.1).
 *
 * Privacy (spec 9, 14): thrown failures carry the closed reason token
 * only — no path, locator, digest or provider detail ever reaches a
 * thrown error. Like the other device-sync modules this file imports no
 * Node.js, Electron or `FileSystemAdapter` API at module load time, so it
 * stays loadable on mobile.
 */

import { normalizePolicyLocator } from "../exclusion-policy/evaluator";
import { canonicalJsonBytes, sha256Hex } from "../exclusion-policy/canonical-json";
import type { ClosedJsonValue } from "../exclusion-policy/strict-json";
import type { FrozenFingerprint, LocalFile } from "../journal/contracts";
import { deriveFrozenFingerprint } from "../journal/fingerprint";
import { DeviceSyncApiError } from "./api";

// --- the frozen capture bounds (spec 6.2, 6.3) -----------------------------------------------------

/** Entries per ordered manifest page — the frozen server page bound. */
export const MANIFEST_PAGE_ENTRIES = 500;

/** The total-entry cap of one manifest run — the frozen server run bound. */
export const MAX_MANIFEST_TOTAL_ENTRIES = 100_000;

// --- the capture contracts ---------------------------------------------------------------------------

/**
 * The narrow read-only Vault slice manifest capture needs: the snapshot of
 * current regular-file paths and the settled bytes of one regular file
 * (`null` when the path is not, or is no longer, a regular file). No Vault
 * write ever flows through this port.
 */
export interface ManifestCaptureVaultReader {
  listRegularFilePaths(): Promise<readonly string[]>;
  readRegularFileBytes(normalizedPath: string): Promise<Uint8Array | null>;
}

/** The tracked-mapping read capture joins its known source evidence onto. */
export interface ManifestIdentityReader {
  readLocalFileByPath(normalizedPath: string): LocalFile | null;
}

/** One locally observed manifest entry (spec 12.1): opaque ID, locator, settled fingerprint, evidence. */
export interface ManifestEntry {
  readonly localEntryId: string;
  readonly normalizedLocator: string;
  readonly fingerprint: FrozenFingerprint;
  readonly observationGeneration: number;
  readonly knownSourceId: string | null;
  readonly knownVersionId: string | null;
}

/** One ordered page of entries with its canonical-JSON page digest. */
export interface ManifestEntryPage {
  readonly pageNumber: number;
  readonly entries: readonly ManifestEntry[];
  readonly pageDigest: string;
}

/** The stable ordered capture surface the manifest reconciler drives (brief task 11). */
export interface ManifestCapture {
  capturePages(barrierGeneration: number): AsyncIterable<ManifestEntryPage>;
}

export interface ManifestCaptureOptions {
  readonly vaultReader: ManifestCaptureVaultReader;
  readonly identityReader: ManifestIdentityReader;
  /**
   * Page bound override; defaults to {@link MANIFEST_PAGE_ENTRIES}. Exists
   * so tests can exercise multi-page runs without 500 fixtures — the
   * default is pinned by test to the frozen server bound.
   */
  readonly entriesPerPage?: number | undefined;
}

// --- the canonical-JSON digest grammar (spec 6.3, 7.3) ------------------------------------------------

/** The opaque local entry ID: a one-way digest of the normalized locator, stable across a run. */
export async function buildManifestLocalEntryId(normalizedLocator: string): Promise<string> {
  const digest = await sha256Hex(
    new TextEncoder().encode(`manifest-entry/v1:${normalizedLocator}`),
  );
  return `me1-${digest}`;
}

/**
 * The page digest: the SHA-256 over the RFC 8785 canonical JSON of the
 * plugin's versioned page grammar — every member of the submitted entry
 * body (id, locator, fingerprint triple, observation generation, known
 * source/version evidence) is bound, so an exact resume re-digests the
 * identical page byte for byte.
 */
export async function computeManifestPageDigest(
  pageNumber: number,
  entries: readonly ManifestEntry[],
): Promise<string> {
  const payload: ClosedJsonValue = {
    version: 1,
    page: pageNumber,
    entries: entries.map((entry) => ({
      id: entry.localEntryId,
      locator: entry.normalizedLocator,
      sha256: entry.fingerprint.sha256,
      size_bytes: entry.fingerprint.sizeBytes,
      media_type: entry.fingerprint.mediaType,
      generation: entry.observationGeneration,
      known_source_id: entry.knownSourceId,
      known_version_id: entry.knownVersionId,
    })),
  };
  return sha256Hex(canonicalJsonBytes(payload));
}

/** One recorded page of a run: the ordered page number, its entry count and digest. */
export interface ManifestPageDigestRecord {
  readonly pageNumber: number;
  readonly entryCount: number;
  readonly pageDigest: string;
}

/**
 * The final digest: the SHA-256 over the RFC 8785 canonical JSON of
 * `{"pages":[{"digest":…,"entries":…,"page":…}…],"version":1}` with the
 * pages sorted by page number — the exact server wire grammar of
 * `compute_manifest_final_digest`, so the finalize verification, any later
 * replay and the completion body agree byte for byte.
 */
export async function computeManifestFinalDigest(
  pages: readonly ManifestPageDigestRecord[],
): Promise<string> {
  const ordered = [...pages].sort((left, right) => left.pageNumber - right.pageNumber);
  const payload: ClosedJsonValue = {
    version: 1,
    pages: ordered.map((page) => ({
      page: page.pageNumber,
      entries: page.entryCount,
      digest: page.pageDigest,
    })),
  };
  return sha256Hex(canonicalJsonBytes(payload));
}

// --- the capture coordinator ---------------------------------------------------------------------------

/** Normalize one Vault path to the canonical locator, or drop it closed. */
function normalizeLocatorOrNull(path: string): string | null {
  if (typeof path !== "string") {
    return null;
  }
  try {
    return normalizePolicyLocator(path);
  } catch {
    return null;
  }
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

/**
 * Build the stable ordered manifest capture. One instance holds no bytes,
 * no credential and no transport; the enumeration reads the settled bytes
 * exactly once per file and never writes the Vault.
 */
export function createManifestCapture(options: ManifestCaptureOptions): ManifestCapture {
  const vaultReader = options.vaultReader;
  const identityReader = options.identityReader;
  const entriesPerPage =
    options.entriesPerPage !== undefined && isPositiveInteger(options.entriesPerPage)
      ? options.entriesPerPage
      : MANIFEST_PAGE_ENTRIES;

  async function* capturePages(barrierGeneration: number): AsyncIterable<ManifestEntryPage> {
    const snapshotPaths = await vaultReader.listRegularFilePaths();
    const normalizedLocators = [
      ...new Set(
        snapshotPaths
          .map((path) => normalizeLocatorOrNull(path))
          .filter((locator): locator is string => locator !== null),
      ),
    ].sort();
    if (normalizedLocators.length > MAX_MANIFEST_TOTAL_ENTRIES) {
      // A Vault beyond the run cap cannot be proven by one manifest: fail
      // closed before a single page exists (the closed reason only — the
      // locator list never reaches the thrown error).
      throw new DeviceSyncApiError("device_manifest_capture_failed", false, null, null);
    }

    let pageNumber = 0;
    let batch: ManifestEntry[] = [];
    for (const normalizedLocator of normalizedLocators) {
      const contentBytes = await vaultReader.readRegularFileBytes(normalizedLocator);
      if (contentBytes === null) {
        // The file vanished between listing and read: it is not represented
        // — the watcher's lifecycle observation owns the vanished path.
        continue;
      }
      const fingerprint = await deriveFrozenFingerprint(contentBytes);
      const trackedFile = identityReader.readLocalFileByPath(normalizedLocator);
      batch.push({
        localEntryId: await buildManifestLocalEntryId(normalizedLocator),
        normalizedLocator,
        fingerprint,
        observationGeneration: barrierGeneration,
        knownSourceId: trackedFile?.sourceId ?? null,
        knownVersionId: trackedFile?.baseVersionId ?? null,
      });
      if (batch.length >= entriesPerPage) {
        yield {
          pageNumber,
          entries: batch,
          pageDigest: await computeManifestPageDigest(pageNumber, batch),
        };
        pageNumber += 1;
        batch = [];
      }
    }
    // The empty Vault still emits exactly one empty page 0: the run's first
    // accepted page binds the run identity and checkpoint locally (spec
    // 7.3), and the empty final digest still commits to the grammar.
    if (batch.length > 0 || pageNumber === 0) {
      yield {
        pageNumber,
        entries: batch,
        pageDigest: await computeManifestPageDigest(pageNumber, batch),
      };
    }
  }

  return { capturePages };
}
