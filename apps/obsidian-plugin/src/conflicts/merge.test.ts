/**
 * Tests of the bounded three-way text merge (Child 8 spec 5.2.2, Task 8).
 *
 * These tests pin: the merge NEVER auto-resolves a conflicting hunk (a
 * conflicting hunk always renders conflict markers and demands explicit user
 * review), non-conflicting regions merge cleanly, identical edits collapse
 * into one copy, every byte/line bound fails safe onto the closed
 * "manual choice required" state with NO partial merged text, text decodes
 * only for the supported text/Markdown media types, and invalid UTF-8 never
 * decodes.
 */

import { describe, expect, it } from "vitest";

import {
  MERGE_CONFLICT_LOCAL_OPEN_MARKER,
  MERGE_CONFLICT_REMOTE_CLOSE_MARKER,
  MERGE_CONFLICT_SEPARATOR_MARKER,
  MERGE_INPUT_MAXIMUM_BYTES,
  MERGE_INPUT_MAXIMUM_LINES,
  MERGE_PROPOSAL_MAXIMUM_BYTES,
  MERGE_SUPPORTED_MEDIA_TYPES,
  computeBoundedThreeWayMerge,
  decodeConflictEvidenceText,
  isMergeSupportedMediaType,
} from "./merge";

/** Repeat one line the given count of times, joined with single newlines. */
function repeatLines(line: string, count: number): string {
  return Array.from({ length: count }, () => line).join("\n");
}

function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

// --- the mandated merge invariant ---------------------------------------------------------------------

describe("bounded three-way merge conflicting hunks (spec 5.2.2)", () => {
  it("never auto-resolves conflicting text hunks", () => {
    const base = "alpha\nbravo\ncharlie";
    const remote = "alpha\nbravo-remote\ncharlie";
    const local = "alpha\nbravo-local\ncharlie";
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.requiresUserReview).toBe(true);
    expect(merge.outcome).toBe("merged_with_conflicts");
    expect(merge.conflictingHunkCount).toBe(1);
    expect(merge.mergedText).toBe(
      [
        "alpha",
        MERGE_CONFLICT_LOCAL_OPEN_MARKER,
        "bravo-local",
        MERGE_CONFLICT_SEPARATOR_MARKER,
        "bravo-remote",
        MERGE_CONFLICT_REMOTE_CLOSE_MARKER,
        "charlie",
      ].join("\n"),
    );
  });

  it("marks every conflicting hunk when both sides change two separate regions", () => {
    const base = "one\ntwo\nthree\nfour\nfive\nsix";
    const remote = "ONE-remote\ntwo\nthree\nfour\nfive\nSIX-remote";
    const local = "ONE-local\ntwo\nthree\nfour\nfive\nSIX-local";
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.outcome).toBe("merged_with_conflicts");
    expect(merge.conflictingHunkCount).toBe(2);
    expect(merge.mergedText).toContain("ONE-local");
    expect(merge.mergedText).toContain("ONE-remote");
    expect(merge.mergedText).toContain("SIX-local");
    expect(merge.mergedText).toContain("SIX-remote");
  });

  it("treats adjacent edits on both sides as one conflicting hunk, never an interleaved merge", () => {
    const base = "line-a\nline-b";
    const remote = "remote-a\nremote-b";
    const local = "local-a\nlocal-b";
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.requiresUserReview).toBe(true);
    expect(merge.conflictingHunkCount).toBe(1);
    expect(merge.mergedText).toBe(
      [
        MERGE_CONFLICT_LOCAL_OPEN_MARKER,
        "local-a",
        "local-b",
        MERGE_CONFLICT_SEPARATOR_MARKER,
        "remote-a",
        "remote-b",
        MERGE_CONFLICT_REMOTE_CLOSE_MARKER,
      ].join("\n"),
    );
  });
});

// --- clean merges --------------------------------------------------------------------------------------

describe("bounded three-way merge clean regions (spec 5.2.2)", () => {
  it("merges non-conflicting regions cleanly without user review", () => {
    const base = "alpha\nbravo\ncharlie\ndelta";
    const remote = "ALPHA\nbravo\ncharlie\ndelta";
    const local = "alpha\nbravo\ncharlie\nDELTA";
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.outcome).toBe("merged_clean");
    expect(merge.requiresUserReview).toBe(false);
    expect(merge.conflictingHunkCount).toBe(0);
    expect(merge.mergedText).toBe("ALPHA\nbravo\ncharlie\nDELTA");
  });

  it("collapses identical remote and local edits into one copy", () => {
    const base = "alpha\nbravo\ncharlie";
    const remote = "alpha\nBRAVO\ncharlie";
    const local = "alpha\nBRAVO\ncharlie";
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.outcome).toBe("merged_clean");
    expect(merge.mergedText).toBe("alpha\nBRAVO\ncharlie");
  });

  it("returns the remote text when the local side is unchanged", () => {
    const base = "alpha\nbravo";
    const remote = "alpha\nbravo-remote";
    const merge = computeBoundedThreeWayMerge(base, remote, base);
    expect(merge.outcome).toBe("merged_clean");
    expect(merge.mergedText).toBe(remote);
  });

  it("returns the local text when the remote side is unchanged", () => {
    const base = "alpha\nbravo";
    const local = "alpha\nbravo-local";
    const merge = computeBoundedThreeWayMerge(base, base, local);
    expect(merge.outcome).toBe("merged_clean");
    expect(merge.mergedText).toBe(local);
  });

  it("merges distinct insertions from both sides cleanly", () => {
    const base = "alpha\nbravo\ncharlie\ndelta";
    const remote = "alpha\nremote-insert\nbravo\ncharlie\ndelta";
    const local = "alpha\nbravo\ncharlie\nlocal-insert\ndelta";
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.outcome).toBe("merged_clean");
    expect(merge.mergedText).toBe("alpha\nremote-insert\nbravo\ncharlie\nlocal-insert\ndelta");
  });

  it("marks differing insertions at the same position as a conflict", () => {
    const base = "alpha\nbravo";
    const remote = "alpha\nremote-insert\nbravo";
    const local = "alpha\nlocal-insert\nbravo";
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.requiresUserReview).toBe(true);
    expect(merge.conflictingHunkCount).toBe(1);
  });

  it("collapses identical insertions at the same position into one copy", () => {
    const base = "alpha\nbravo";
    const remote = "alpha\nshared-insert\nbravo";
    const local = "alpha\nshared-insert\nbravo";
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.outcome).toBe("merged_clean");
    expect(merge.mergedText).toBe("alpha\nshared-insert\nbravo");
  });
});

// --- the bounds -----------------------------------------------------------------------------------------

describe("bounded three-way merge bounds (spec 5.2.2)", () => {
  it("answers manual choice required with no merged text when an input exceeds the byte bound", () => {
    // 4000 lines x 81 bytes each = 323999 bytes: above the byte bound, below the line bound.
    const oversized = repeatLines("b".repeat(80), 4000);
    expect(new TextEncoder().encode(oversized).byteLength).toBeGreaterThan(
      MERGE_INPUT_MAXIMUM_BYTES,
    );
    const merge = computeBoundedThreeWayMerge(oversized, oversized, oversized);
    expect(merge.outcome).toBe("bound_exceeded");
    expect(merge.requiresUserReview).toBe(true);
    expect(merge.mergedText).toBeNull();
  });

  it("answers manual choice required when an input exceeds the line bound", () => {
    const tooManyLines = repeatLines("l", MERGE_INPUT_MAXIMUM_LINES + 1);
    const merge = computeBoundedThreeWayMerge(tooManyLines, tooManyLines, tooManyLines);
    expect(merge.outcome).toBe("bound_exceeded");
    expect(merge.requiresUserReview).toBe(true);
    expect(merge.mergedText).toBeNull();
  });

  it("answers manual choice required when the conflict-marked proposal would exceed the proposal byte bound", () => {
    // Both changed sides sit exactly at the per-input byte bound; their
    // conflict-marked proposal exceeds the proposal bound, so no partial
    // merge is ever offered.
    const base = repeatLines("x".repeat(63), 100);
    const remote = repeatLines("r".repeat(63), MERGE_INPUT_MAXIMUM_LINES);
    const local = repeatLines("l".repeat(63), MERGE_INPUT_MAXIMUM_LINES);
    expect(new TextEncoder().encode(remote).byteLength).toBeLessThanOrEqual(
      MERGE_INPUT_MAXIMUM_BYTES,
    );
    const merge = computeBoundedThreeWayMerge(base, remote, local);
    expect(merge.outcome).toBe("bound_exceeded");
    expect(merge.requiresUserReview).toBe(true);
    expect(merge.mergedText).toBeNull();
    expect(MERGE_PROPOSAL_MAXIMUM_BYTES).toBeGreaterThan(MERGE_INPUT_MAXIMUM_BYTES);
  });
});

// --- the supported media types and decoding --------------------------------------------------------------

describe("conflict evidence text decoding (spec 5.2.2)", () => {
  it("pins the supported text/Markdown media type vocabulary", () => {
    expect(MERGE_SUPPORTED_MEDIA_TYPES).toEqual(["text/markdown", "text/plain"]);
    expect(isMergeSupportedMediaType("text/markdown")).toBe(true);
    expect(isMergeSupportedMediaType("text/plain")).toBe(true);
    expect(isMergeSupportedMediaType("application/octet-stream")).toBe(false);
    expect(isMergeSupportedMediaType("image/png")).toBe(false);
    expect(isMergeSupportedMediaType("text/markdown; charset=utf-8")).toBe(false);
  });

  it("decodes supported media types to text", () => {
    const evidence = decodeConflictEvidenceText(bytesOf("# heading"), "text/markdown");
    expect(evidence).toEqual({ kind: "text", text: "# heading" });
  });

  it("refuses to decode unsupported media types", () => {
    const evidence = decodeConflictEvidenceText(bytesOf("plain"), "application/pdf");
    expect(evidence).toEqual({ kind: "media_unsupported" });
  });

  it("refuses to decode evidence above the byte bound", () => {
    const oversized = bytesOf(repeatLines("b".repeat(80), 4000));
    const evidence = decodeConflictEvidenceText(oversized, "text/markdown");
    expect(evidence).toEqual({ kind: "bytes_exceeded" });
  });

  it("refuses to decode invalid UTF-8 instead of guessing text", () => {
    const invalidUtf8 = new Uint8Array([0xff, 0xfe, 0xfd, 0x00]);
    const evidence = decodeConflictEvidenceText(invalidUtf8, "text/markdown");
    expect(evidence).toEqual({ kind: "text_undecodable" });
  });
});
