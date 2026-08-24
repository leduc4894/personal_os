/**
 * Tests of the sanitized sync-diagnostics export and trail renderers (sync
 * error tracing design: export surface contract).
 *
 * The export block, the settings trail section and the derived stop-reason
 * tokens are pure functions over closed inputs: the status snapshot line,
 * the journal-store diagnostics inputs, aggregate counts and the durable
 * trail tail. These tests pin: the exact sanitized block shape (status
 * line, blocker guidance, diagnostics line, counts, trail tail with
 * ISO-8601 UTC timestamps and the opaque request-id token shape), the
 * last-five tail bound, the newest-per-kind stop-reason derivation in the
 * fixed kind order, and the forbidden-substrate guarantees — no path,
 * digest, credential, hostname or free-form string ever reaches an output
 * line or the module source.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import type { JournalFileStore } from "./persistence";
import {
  SYNC_COMPOSITION_READ_FAILURE_TOKENS,
  SYNC_DIAGNOSTICS_TRAIL_CONTRACT,
  SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
  createSyncDiagnosticsTrail,
  envelopeRequestId,
} from "./sync-diagnostics-trail";
import type { SyncDiagnosticToken, SyncDiagnosticTrailEntry } from "./sync-diagnostics-trail";
import {
  SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT,
  deriveSyncStopReasonTokens,
  renderSyncDiagnosticsExportBlock,
  renderSyncDiagnosticsTrailSection,
} from "./sync-diagnostics-export";

const REQUEST_ID = "66666666-6666-4666-8666-666666666666";

class PersistedTrailFileStore implements JournalFileStore {
  #bytes: ArrayBuffer;

  constructor(token: string) {
    this.#bytes = new TextEncoder().encode(
      JSON.stringify({
        contract: SYNC_DIAGNOSTICS_TRAIL_CONTRACT,
        entries: [
          {
            kind: "journal_failure",
            at_epoch_ms: 1_784_000_000_000,
            tokens: [token],
          },
        ],
      }),
    ).buffer as ArrayBuffer;
  }

  async exists(fileName: string): Promise<boolean> {
    return fileName === SYNC_DIAGNOSTICS_TRAIL_FILE_NAME;
  }

  async readBinary(): Promise<ArrayBuffer> {
    return this.#bytes.slice(0);
  }

  async writeBinary(_fileName: string, data: ArrayBuffer): Promise<void> {
    this.#bytes = data.slice(0);
  }

  async remove(): Promise<void> {
    return undefined;
  }
}

async function reloadPersistedTrailToken(token: string) {
  const trail = createSyncDiagnosticsTrail({
    fileStore: new PersistedTrailFileStore(token),
    nowEpochMs: () => 1_784_000_001_000,
  });
  await trail.load();
  return trail;
}

/** Build one trail entry the way the durable trail hands it to a renderer. */
function trailEntry(
  kind: SyncDiagnosticTrailEntry["kind"],
  atEpochMs: number,
  tokens: readonly SyncDiagnosticToken[],
): SyncDiagnosticTrailEntry {
  return { kind, atEpochMs, tokens };
}

// --- the export block --------------------------------------------------------------------------------

describe("renderSyncDiagnosticsExportBlock", () => {
  it("rejects a fabricated persisted reason before the sanitized export boundary", async () => {
    const trail = await reloadPersistedTrailToken("made_up_reason");
    const entries = trail.readEntries();
    const block = renderSyncDiagnosticsExportBlock({
      syncStatusLine: "Ready",
      syncBlockerGuidance: [],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: [],
        generationPublishFailureCount: 0,
        lastGenerationPublishFailureReasons: [],
      },
      trailEntryCount: entries.length,
      trailAppendFailureCount: trail.readAppendFailureCount(),
      trailTail: entries,
    });

    expect(block).not.toContain("made_up_reason");
    expect(block).toContain("trail_reset");
  });

  it("accepts and exports a declared token after persisted reload", async () => {
    const trail = await reloadPersistedTrailToken("lifecycle_reconcile_persist_failed");
    const entries = trail.readEntries();
    const block = renderSyncDiagnosticsExportBlock({
      syncStatusLine: "Ready",
      syncBlockerGuidance: [],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: [],
        generationPublishFailureCount: 0,
        lastGenerationPublishFailureReasons: [],
      },
      trailEntryCount: entries.length,
      trailAppendFailureCount: trail.readAppendFailureCount(),
      trailTail: entries,
    });

    expect(block).toContain("lifecycle_reconcile_persist_failed");
    expect(block).not.toContain("trail_reset");
  });

  it("builds the sanitized block from the status line, guidance, diagnostics, counts and trail tail", () => {
    const block = renderSyncDiagnosticsExportBlock({
      syncStatusLine: "Ready (3)",
      syncBlockerGuidance: [
        "Login required: open the existing browser login from the plugin settings. Queued work is kept unchanged.",
      ],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: ["journal_query_failed"],
        generationPublishFailureCount: 2,
        lastGenerationPublishFailureReasons: ["journal_generation_write_failed"],
      },
      trailEntryCount: 42,
      trailAppendFailureCount: 1,
      trailTail: [
        trailEntry("pass_outcome", 1_784_000_000_000, ["completed"]),
        trailEntry("wire_failure", 1_784_000_001_000, [
          "server_error",
          envelopeRequestId(REQUEST_ID),
        ]),
      ],
    });
    expect(block).toContain("obsidian_sync_diagnostics_export/v1");
    expect(block).toContain("Status: Ready (3)");
    expect(block).toContain(
      "Blocker: Login required: open the existing browser login from the plugin settings. Queued work is kept unchanged.",
    );
    expect(block).toContain("Pass failures: journal_query_failed");
    expect(block).toContain("Generation publish failures: 2 (journal_generation_write_failed)");
    expect(block).toContain("Trail entries: 42");
    expect(block).toContain("Trail append failures: 1");
    // Timestamps are pinned to ISO-8601 UTC, one per trail tail line.
    const firstTimestamp = new Date(1_784_000_000_000).toISOString();
    const secondTimestamp = new Date(1_784_000_001_000).toISOString();
    expect(block).toContain(`${firstTimestamp} · pass_outcome · completed`);
    expect(block).toContain(`${secondTimestamp} · wire_failure · server_error · request_id=${REQUEST_ID}`);
  });

  it("renders one ISO-8601 UTC timestamp per trail tail line", () => {
    const block = renderSyncDiagnosticsExportBlock({
      syncStatusLine: "Ready",
      syncBlockerGuidance: [],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: [],
        generationPublishFailureCount: 0,
        lastGenerationPublishFailureReasons: [],
      },
      trailEntryCount: 1,
      trailAppendFailureCount: 0,
      trailTail: [trailEntry("trail_reset", 1_784_000_000_000, [])],
    });
    const tailLine = block
      .split("\n")
      .find((line) => line.includes("trail_reset"));
    expect(tailLine).toBeDefined();
    expect(tailLine ?? "").toMatch(
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z · trail_reset$/,
    );
    // A token-free entry renders kind and timestamp only.
    expect(tailLine ?? "").not.toContain(" · · ");
  });

  it("keeps only the last five tail entries and reports the shown count", () => {
    const entries = Array.from({ length: 7 }, (_, index) =>
      trailEntry("pass_outcome", 1_784_000_000_000 + index * 1_000, ["completed"]),
    );
    const block = renderSyncDiagnosticsExportBlock({
      syncStatusLine: "Ready",
      syncBlockerGuidance: [],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: [],
        generationPublishFailureCount: 0,
        lastGenerationPublishFailureReasons: [],
      },
      trailEntryCount: 7,
      trailAppendFailureCount: 0,
      trailTail: entries,
    });
    expect(SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT).toBe(5);
    expect(block).toContain("Trail tail (last 5):");
    const renderedTailLines = block
      .split("\n")
      .filter((line) => line.includes("pass_outcome"));
    expect(renderedTailLines).toHaveLength(5);
    // The newest five survive; the two oldest do not.
    expect(renderedTailLines[0]).toContain(new Date(1_784_000_002_000).toISOString());
    expect(renderedTailLines.at(-1)).toContain(new Date(1_784_000_006_000).toISOString());
  });

  it("renders the no-journal and empty-trail states with closed text only", () => {
    const block = renderSyncDiagnosticsExportBlock({
      syncStatusLine: null,
      syncBlockerGuidance: [],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: [],
        generationPublishFailureCount: 0,
        lastGenerationPublishFailureReasons: [],
      },
      trailEntryCount: 0,
      trailAppendFailureCount: 0,
      trailTail: [],
    });
    expect(block).toContain("Status: Journal not running on this device");
    expect(block).toContain("No journal store failures observed.");
    expect(block).toContain("Trail entries: 0");
    expect(block).toContain("Trail append failures: 0");
    expect(block).toContain("Trail tail: none recorded");
    expect(block).not.toContain("Blocker:");
  });

  it("keeps the block free of path-shaped and credential-shaped substrings", () => {
    const block = renderSyncDiagnosticsExportBlock({
      syncStatusLine: "Offline — queued (2)",
      syncBlockerGuidance: ["Login required: open the existing browser login from the plugin settings. Queued work is kept unchanged."],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: ["journal_mutation_failed"],
        generationPublishFailureCount: 1,
        lastGenerationPublishFailureReasons: ["journal_generation_write_failed"],
      },
      trailEntryCount: 9,
      trailAppendFailureCount: 2,
      trailTail: [
        trailEntry("wire_failure", 1_784_000_000_000, [
          "network_timeout",
          envelopeRequestId(REQUEST_ID),
        ]),
      ],
    });
    for (const forbiddenText of [
      "notes/",
      ".md",
      "at1.",
      "Bearer",
      "authorization",
      "https://",
      "Error:",
      "file://",
    ]) {
      expect(block).not.toContain(forbiddenText);
    }
  });
});

// --- the settings trail section ----------------------------------------------------------------------

describe("renderSyncDiagnosticsTrailSection", () => {
  it("renders the stop reasons, the total count, the append-failure counter and the tail lines", () => {
    const section = renderSyncDiagnosticsTrailSection({
      stopReasonTokens: ["journal_query_failed", "server_error"],
      totalEntryCount: 42,
      appendFailureCount: 3,
      entries: [
        trailEntry("pass_outcome", 1_784_000_000_000, ["completed"]),
        trailEntry("wire_failure", 1_784_000_001_000, [
          "server_error",
          envelopeRequestId(REQUEST_ID),
        ]),
      ],
    });
    const lines = section.split("\n");
    expect(lines[0]).toBe("Stop reasons: journal_query_failed, server_error");
    expect(lines[1]).toBe("Trail entries: 42 · Append failures: 3");
    expect(lines[2]).toBe(
      `${new Date(1_784_000_000_000).toISOString()} · pass_outcome · completed`,
    );
    expect(lines[3]).toBe(
      `${new Date(1_784_000_001_000).toISOString()} · wire_failure · server_error · request_id=${REQUEST_ID}`,
    );
  });

  it("omits the stop-reason line and reports the empty state when nothing was recorded", () => {
    const section = renderSyncDiagnosticsTrailSection({
      stopReasonTokens: [],
      totalEntryCount: 0,
      appendFailureCount: 0,
      entries: [],
    });
    expect(section).not.toContain("Stop reasons:");
    expect(section).toContain("Trail entries: 0 · Append failures: 0");
    expect(section).toContain("No trail entries recorded yet.");
  });
});

// --- the derived stop-reason tokens -------------------------------------------------------------------

describe("deriveSyncStopReasonTokens", () => {
  it("derives the newest closed reason of each failure kind in the fixed kind order", () => {
    const tokens = deriveSyncStopReasonTokens([
      trailEntry("journal_failure", 1, ["journal_query_failed"]),
      trailEntry("wire_failure", 2, ["network_timeout"]),
      trailEntry("publish_failure", 3, ["journal_generation_write_failed"]),
      trailEntry("wire_failure", 4, ["server_error"]),
      trailEntry("journal_failure", 5, ["journal_mutation_failed"]),
      trailEntry("pass_outcome", 6, ["completed"]),
      trailEntry("trail_reset", 7, []),
    ]);
    expect(tokens).toEqual([
      "journal_mutation_failed",
      "journal_generation_write_failed",
      "server_error",
    ]);
  });

  it("returns empty when no failure-kind entry carries a closed token", () => {
    expect(
      deriveSyncStopReasonTokens([
        trailEntry("pass_outcome", 1, ["completed"]),
        trailEntry("wire_failure", 2, [envelopeRequestId(REQUEST_ID)]),
        // A self_check verdict (sync error tracing task 3) is never a stop
        // reason, not even the unreachable-origin one.
        trailEntry("self_check", 3, ["origin_unreachable", "network_offline"]),
        trailEntry("trail_reset", 4, []),
      ]),
    ).toEqual([]);
    expect(deriveSyncStopReasonTokens([])).toEqual([]);
  });
});

// --- the type-level closed vocabulary ------------------------------------------------------------------

describe("sync diagnostics export closed vocabulary (type level)", () => {
  it("exports the automatic snapshot admission token as a closed trail token", () => {
    expect(SYNC_COMPOSITION_READ_FAILURE_TOKENS).toContain(
      "automatic_snapshot_admission_failed",
    );
  });

  it("rejects a free-form token at compile time", () => {
    // A free-form string must not enter a rendered trail entry.
    const entry = trailEntry("wire_failure", 1, [
      // @ts-expect-error a free-form string must not enter a trail entry
      "edge block page after 12 seconds",
    ]);
    expect(entry.tokens).toHaveLength(1);
  });
});

// --- the privacy source contract -----------------------------------------------------------------------

describe("sync diagnostics export privacy source contract", () => {
  it("keeps the export module free of path-shaped and credential-shaped substrings", () => {
    const exportSource = readFileSync(
      new URL("./sync-diagnostics-export.ts", import.meta.url),
      "utf8",
    );
    for (const forbiddenText of [
      "console.",
      "fetch(",
      "requestUrl",
      ".md",
      "notes/",
      "at1.",
      "Bearer",
      "authorization",
      "https://",
    ]) {
      expect(exportSource).not.toContain(forbiddenText);
    }
  });
});
