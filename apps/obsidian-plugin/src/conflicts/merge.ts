/**
 * The bounded, user-mediated three-way text merge (Child 8 spec 5.2.2,
 * Task 8).
 *
 * Text merge is user-mediated: a region both sides changed differently is
 * NEVER auto-resolved — it renders as a conflict-marked hunk and the whole
 * proposal carries `requiresUserReview === true`. Non-conflicting regions
 * (one side unchanged, disjoint edits, or identical edits) merge cleanly.
 * Binary media has no merge at all: evidence decodes to text only for the
 * supported text/Markdown media types, and invalid UTF-8 never decodes.
 *
 * Every bound is a named constant enforced BEFORE any merge work and again
 * on the finished proposal: one input role (base, remote or local) above
 * the byte or line bound, or a conflict-marked proposal above the proposal
 * byte bound, fails safe onto the closed `bound_exceeded` outcome with NO
 * merged text — a partial merge is never offered, the user faces the safe
 * "manual choice required" state (keep remote or keep local only).
 *
 * The module is pure computation over strings and byte arrays: no journal,
 * no transport, no credential, no logging — merged drafts live only in the
 * caller's bounded ephemeral memory (spec 6: the journal must never store
 * raw candidate bytes or merged drafts).
 */

// --- the named bounds (spec 5.2.2: bounded merge) -------------------------------------------------------

/** The per-role byte ceiling: base, remote and local each stay mergeable below this size. */
export const MERGE_INPUT_MAXIMUM_BYTES = 262_144;

/** The per-role line ceiling: each merge input stays mergeable below this line count. */
export const MERGE_INPUT_MAXIMUM_LINES = 4_096;

/**
 * The merged-proposal byte ceiling: a conflict-marked proposal combining
 * both changed sides above this size fails safe onto `bound_exceeded`, so
 * the bounded-memory draft promise holds for the output too.
 */
export const MERGE_PROPOSAL_MAXIMUM_BYTES = 524_288;

// --- the supported media types (spec 5.2.2) --------------------------------------------------------------

/**
 * The closed evidence media types the merge decodes: exactly the
 * text/Markdown family the Task 6 detail matrix admits (`save_merged`
 * requires a `text/markdown` candidate; `text/plain` renders read-only
 * diffs safely). Everything else — every binary media type — is binary
 * choice only.
 */
export const MERGE_SUPPORTED_MEDIA_TYPES = ["text/markdown", "text/plain"] as const;

/** Whether one canonical media type belongs to the mergeable text family. */
export function isMergeSupportedMediaType(mediaType: string): boolean {
  return (MERGE_SUPPORTED_MEDIA_TYPES as readonly string[]).includes(mediaType);
}

// --- the conflict markers ----------------------------------------------------------------------------------

/** Opens the local side of one conflict hunk. */
export const MERGE_CONFLICT_LOCAL_OPEN_MARKER = "<<<<<<< local";
/** Separates the local and remote sides of one conflict hunk. */
export const MERGE_CONFLICT_SEPARATOR_MARKER = "=======";
/** Closes the remote side of one conflict hunk. */
export const MERGE_CONFLICT_REMOTE_CLOSE_MARKER = ">>>>>>> remote";

// --- the verified-evidence text decoding --------------------------------------------------------------------

/** The closed outcome of decoding one verified evidence download for the merge. */
export type ConflictEvidenceTextDecode =
  | { readonly kind: "text"; readonly text: string }
  | { readonly kind: "media_unsupported" }
  | { readonly kind: "bytes_exceeded" }
  | { readonly kind: "text_undecodable" };

const FATAL_UTF8_DECODER = new TextDecoder("utf-8", { fatal: true });

/**
 * Decode one verified evidence download into mergeable text. Only the
 * supported text/Markdown media types decode; a byte count above the merge
 * input bound and invalid UTF-8 never produce text — each fails onto its
 * own closed outcome so the caller renders the safe manual-choice state.
 */
export function decodeConflictEvidenceText(
  bytes: Uint8Array,
  mediaType: string,
): ConflictEvidenceTextDecode {
  if (!isMergeSupportedMediaType(mediaType)) {
    return { kind: "media_unsupported" };
  }
  if (bytes.byteLength > MERGE_INPUT_MAXIMUM_BYTES) {
    return { kind: "bytes_exceeded" };
  }
  try {
    return { kind: "text", text: FATAL_UTF8_DECODER.decode(bytes) };
  } catch {
    return { kind: "text_undecodable" };
  }
}

// --- the bounded three-way merge -----------------------------------------------------------------------------

/** The closed outcome of one bounded merge computation. */
export type BoundedMergeOutcome = "merged_clean" | "merged_with_conflicts" | "bound_exceeded";

/** The frozen result of one bounded three-way merge computation. */
export interface BoundedMergeResult {
  /** The closed computation outcome. */
  readonly outcome: BoundedMergeOutcome;
  /**
   * Whether explicit user review is REQUIRED before this merge may be
   * saved: true exactly when conflicting hunks exist (they are never
   * auto-resolved) or a bound was exceeded (manual choice required).
   * A clean proposal still needs the user's explicit save command — the
   * controller never applies any proposal on its own.
   */
  readonly requiresUserReview: boolean;
  /**
   * The merged text (conflict-marked when hunks conflict), or null
   * whenever the outcome is `bound_exceeded` — a partial merge is never
   * offered.
   */
  readonly mergedText: string | null;
  /** How many hunks both sides changed differently. */
  readonly conflictingHunkCount: number;
}

const TEXT_ENCODER = new TextEncoder();

/** One contiguous alignment region between the base and one changed side. */
interface SideRegion {
  readonly isStable: boolean;
  readonly baseStart: number;
  readonly baseEnd: number;
  readonly otherStart: number;
  readonly otherEnd: number;
}

function splitLines(text: string): string[] {
  return text.split("\n");
}

function utf8ByteLength(text: string): number {
  return TEXT_ENCODER.encode(text).byteLength;
}

/**
 * The longest matching block between two line ranges (the difflib
 * algorithm over a line-content index; no autojunk). A zero-length block
 * means no common line exists in the window.
 */
function longestMatchingBlock(
  baseLines: readonly string[],
  baseFrom: number,
  baseTo: number,
  otherLines: readonly string[],
  otherFrom: number,
  otherTo: number,
  otherIndex: ReadonlyMap<string, readonly number[]>,
): { readonly baseStart: number; readonly otherStart: number; readonly length: number } {
  let bestBaseStart = baseFrom;
  let bestOtherStart = otherFrom;
  let bestLength = 0;
  let previousChain = new Map<number, number>();
  for (let baseIndex = baseFrom; baseIndex < baseTo; baseIndex += 1) {
    const chain = new Map<number, number>();
    const baseLine = baseLines[baseIndex];
    if (baseLine !== undefined) {
      const otherPositions = otherIndex.get(baseLine);
      if (otherPositions !== undefined) {
        for (const otherPosition of otherPositions) {
          if (otherPosition < otherFrom) {
            continue;
          }
          if (otherPosition >= otherTo) {
            break;
          }
          const runLength = (previousChain.get(otherPosition - 1) ?? 0) + 1;
          chain.set(otherPosition, runLength);
          if (runLength > bestLength) {
            bestLength = runLength;
            bestBaseStart = baseIndex - runLength + 1;
            bestOtherStart = otherPosition - runLength + 1;
          }
        }
      }
    }
    previousChain = chain;
  }
  return { baseStart: bestBaseStart, otherStart: bestOtherStart, length: bestLength };
}

/**
 * The contiguous alignment of one changed side against the base: stable
 * regions are common line blocks, unstable regions are the gaps between
 * them. The regions tile [0, base.length) x [0, other.length) exactly, so
 * any base range maps to its other-side range through them.
 */
function computeSideRegions(
  baseLines: readonly string[],
  otherLines: readonly string[],
): SideRegion[] {
  const otherIndex = new Map<string, number[]>();
  for (let index = 0; index < otherLines.length; index += 1) {
    const line = otherLines[index];
    if (line === undefined) {
      continue;
    }
    const positions = otherIndex.get(line);
    if (positions === undefined) {
      otherIndex.set(line, [index]);
    } else {
      positions.push(index);
    }
  }

  const queue: {
    readonly baseFrom: number;
    readonly baseTo: number;
    readonly otherFrom: number;
    readonly otherTo: number;
  }[] = [{ baseFrom: 0, baseTo: baseLines.length, otherFrom: 0, otherTo: otherLines.length }];
  const stableBlocks: { baseStart: number; otherStart: number; length: number }[] = [];
  while (queue.length > 0) {
    const window = queue.pop();
    if (window === undefined) {
      break;
    }
    const block = longestMatchingBlock(
      baseLines,
      window.baseFrom,
      window.baseTo,
      otherLines,
      window.otherFrom,
      window.otherTo,
      otherIndex,
    );
    if (block.length === 0) {
      continue;
    }
    stableBlocks.push(block);
    if (window.baseFrom < block.baseStart || window.otherFrom < block.otherStart) {
      queue.push({
        baseFrom: window.baseFrom,
        baseTo: block.baseStart,
        otherFrom: window.otherFrom,
        otherTo: block.otherStart,
      });
    }
    if (block.baseStart + block.length < window.baseTo ||
        block.otherStart + block.length < window.otherTo) {
      queue.push({
        baseFrom: block.baseStart + block.length,
        baseTo: window.baseTo,
        otherFrom: block.otherStart + block.length,
        otherTo: window.otherTo,
      });
    }
  }
  stableBlocks.sort((left, right) => left.baseStart - right.baseStart);

  const regions: SideRegion[] = [];
  let baseCursor = 0;
  let otherCursor = 0;
  for (const block of stableBlocks) {
    if (block.baseStart > baseCursor || block.otherStart > otherCursor) {
      regions.push({
        isStable: false,
        baseStart: baseCursor,
        baseEnd: block.baseStart,
        otherStart: otherCursor,
        otherEnd: block.otherStart,
      });
    }
    regions.push({
      isStable: true,
      baseStart: block.baseStart,
      baseEnd: block.baseStart + block.length,
      otherStart: block.otherStart,
      otherEnd: block.otherStart + block.length,
    });
    baseCursor = block.baseStart + block.length;
    otherCursor = block.otherStart + block.length;
  }
  if (baseCursor < baseLines.length || otherCursor < otherLines.length) {
    regions.push({
      isStable: false,
      baseStart: baseCursor,
      baseEnd: baseLines.length,
      otherStart: otherCursor,
      otherEnd: otherLines.length,
    });
  }
  return regions;
}

/** One unstable region of one changed side, carrying both coordinate ranges. */
interface SideHunk {
  readonly baseStart: number;
  readonly baseEnd: number;
  readonly otherStart: number;
  readonly otherEnd: number;
}

/** The unstable regions of one side, as the hunk list the region walk consumes. */
function unstableHunksOf(regions: readonly SideRegion[]): SideHunk[] {
  return regions
    .filter((region) => !region.isStable)
    .map((region) => ({
      baseStart: region.baseStart,
      baseEnd: region.baseEnd,
      otherStart: region.otherStart,
      otherEnd: region.otherEnd,
    }));
}

/**
 * Render one side's full line replacement across a review region: the
 * side's own changed spans plus the base text of any gaps between them.
 * Insertion hunks (zero base length) carry only their inserted lines.
 */
function sideRegionLines(
  baseLines: readonly string[],
  sideLines: readonly string[],
  sideHunks: readonly SideHunk[],
  regionStart: number,
  regionEnd: number,
): string[] {
  const lines: string[] = [];
  let baseCursor = regionStart;
  for (const hunk of sideHunks) {
    if (hunk.baseStart > baseCursor) {
      lines.push(...baseLines.slice(baseCursor, hunk.baseStart));
    }
    lines.push(...sideLines.slice(hunk.otherStart, hunk.otherEnd));
    baseCursor = hunk.baseEnd;
  }
  if (baseCursor < regionEnd) {
    lines.push(...baseLines.slice(baseCursor, regionEnd));
  }
  return lines;
}

/**
 * Compute the bounded three-way merge of base, remote and local text.
 *
 * Non-conflicting regions merge cleanly; a region both sides changed
 * differently renders as one conflict-marked hunk (local block, then the
 * remote block) and marks the whole proposal as requiring user review —
 * conflicting hunks are never auto-resolved. An input role above the byte
 * or line bound, or a proposal above the proposal byte bound, fails safe
 * onto `bound_exceeded` with no merged text.
 */
export function computeBoundedThreeWayMerge(
  base: string,
  remote: string,
  local: string,
): BoundedMergeResult {
  const inputs: readonly string[] = [base, remote, local];
  for (const input of inputs) {
    if (utf8ByteLength(input) > MERGE_INPUT_MAXIMUM_BYTES) {
      return boundExceeded();
    }
    if (splitLines(input).length > MERGE_INPUT_MAXIMUM_LINES) {
      return boundExceeded();
    }
  }

  const baseLines = splitLines(base);
  const remoteLines = splitLines(remote);
  const localLines = splitLines(local);
  const remoteHunks = unstableHunksOf(computeSideRegions(baseLines, remoteLines));
  const localHunks = unstableHunksOf(computeSideRegions(baseLines, localLines));

  /** One review region hunk tagged with the side it came from. */
  interface TaggedHunk extends SideHunk {
    readonly side: "remote" | "local";
  }
  const tagged: TaggedHunk[] = [
    ...remoteHunks.map((hunk): TaggedHunk => ({ ...hunk, side: "remote" })),
    ...localHunks.map((hunk): TaggedHunk => ({ ...hunk, side: "local" })),
  ];
  tagged.sort((left, right) =>
    left.baseStart - right.baseStart || (left.side === right.side ? 0 : left.side === "remote" ? -1 : 1),
  );

  const mergedLines: string[] = [];
  let conflictingHunkCount = 0;
  let baseCursor = 0;
  let hunkIndex = 0;

  while (hunkIndex < tagged.length) {
    const lead = tagged[hunkIndex];
    if (lead === undefined) {
      break;
    }
    if (lead.baseStart > baseCursor) {
      mergedLines.push(...baseLines.slice(baseCursor, lead.baseStart));
      baseCursor = lead.baseStart;
    }

    // Expand the region transitively: any hunk from either side that
    // overlaps OR directly abuts the region joins it, so adjacent edits
    // from both sides become one review region, never an interleaved merge.
    const regionStart = baseCursor;
    let regionEnd = lead.baseEnd;
    const regionHunks: TaggedHunk[] = [lead];
    hunkIndex += 1;
    for (;;) {
      const follower = tagged[hunkIndex];
      if (follower === undefined || follower.baseStart > regionEnd) {
        break;
      }
      regionEnd = Math.max(regionEnd, follower.baseEnd);
      regionHunks.push(follower);
      hunkIndex += 1;
    }
    baseCursor = regionEnd;

    const regionRemoteHunks = regionHunks.filter((hunk) => hunk.side === "remote");
    const regionLocalHunks = regionHunks.filter((hunk) => hunk.side === "local");
    if (regionRemoteHunks.length === 0) {
      mergedLines.push(
        ...sideRegionLines(baseLines, localLines, regionLocalHunks, regionStart, regionEnd),
      );
      continue;
    }
    if (regionLocalHunks.length === 0) {
      mergedLines.push(
        ...sideRegionLines(baseLines, remoteLines, regionRemoteHunks, regionStart, regionEnd),
      );
      continue;
    }

    const remoteRegionLines = sideRegionLines(
      baseLines,
      remoteLines,
      regionRemoteHunks,
      regionStart,
      regionEnd,
    );
    const localRegionLines = sideRegionLines(
      baseLines,
      localLines,
      regionLocalHunks,
      regionStart,
      regionEnd,
    );
    if (linesEqual(remoteRegionLines, localRegionLines)) {
      // Both sides changed identically: one shared copy, no conflict.
      mergedLines.push(...remoteRegionLines);
    } else {
      conflictingHunkCount += 1;
      mergedLines.push(
        MERGE_CONFLICT_LOCAL_OPEN_MARKER,
        ...localRegionLines,
        MERGE_CONFLICT_SEPARATOR_MARKER,
        ...remoteRegionLines,
        MERGE_CONFLICT_REMOTE_CLOSE_MARKER,
      );
    }
  }
  if (baseCursor < baseLines.length) {
    mergedLines.push(...baseLines.slice(baseCursor));
  }

  const mergedText = mergedLines.join("\n");
  if (utf8ByteLength(mergedText) > MERGE_PROPOSAL_MAXIMUM_BYTES) {
    return boundExceeded();
  }
  return {
    outcome: conflictingHunkCount > 0 ? "merged_with_conflicts" : "merged_clean",
    requiresUserReview: conflictingHunkCount > 0,
    mergedText,
    conflictingHunkCount,
  };
}

function boundExceeded(): BoundedMergeResult {
  return {
    outcome: "bound_exceeded",
    requiresUserReview: true,
    mergedText: null,
    conflictingHunkCount: 0,
  };
}

function linesEqual(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) {
      return false;
    }
  }
  return true;
}
