/**
 * The sanitized sync-diagnostics renderers (sync error tracing design:
 * export surface contract).
 *
 * Every renderer here is a pure function over closed inputs: the status
 * snapshot line, the journal-store diagnostics inputs, aggregate counts
 * and the durable trail tail. The output text carries ONLY closed tokens,
 * counts and ISO-8601 UTC timestamps — never a path, digest, credential,
 * hostname, provider detail or any free-form string (spec 9). The one
 * opaque value that may appear is the server envelope request id, the
 * correlation token the trail contract already admits.
 */

import type {
  SyncDiagnosticClosedToken,
  SyncDiagnosticKind,
  SyncDiagnosticToken,
  SyncDiagnosticTrailEntry,
} from "./sync-diagnostics-trail";
import type { JournalStoreErrorReason } from "./sqlite-database";

/** How many trail tail entries the settings section and the export render. */
export const SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT = 5;

/** The one contract identifier line of the export block. */
export const SYNC_DIAGNOSTICS_EXPORT_CONTRACT = "obsidian_sync_diagnostics_export/v1";

/**
 * The trail kinds whose newest closed token becomes a derived stop reason
 * on the settings snapshot, in the fixed rendering order.
 */
const STOP_REASON_KIND_ORDER: readonly SyncDiagnosticKind[] = [
  "journal_failure",
  "publish_failure",
  "wire_failure",
];

// --- the journal store diagnostics line (fix round 5) ----------------------------------------------

/** The closed inputs of the journal store diagnostics line. */
export interface JournalStoreDiagnosticsLineInput {
  readonly lastJournalFailureReasons: readonly JournalStoreErrorReason[];
  readonly generationPublishFailureCount: number;
  readonly lastGenerationPublishFailureReasons: readonly JournalStoreErrorReason[];
}

/**
 * Render the journal store diagnostics line (fix round 5): the closed
 * `JournalStoreErrorReason` tokens of swallowed pass-loop journal failures
 * plus the generation-publish failure count and its last closed reasons.
 * Closed vocabulary only — the line never carries a raw error message,
 * path, digest, credential or journal content.
 */
export function renderJournalStoreDiagnosticsLine(
  input: JournalStoreDiagnosticsLineInput,
): string {
  if (
    input.lastJournalFailureReasons.length === 0 &&
    input.generationPublishFailureCount === 0
  ) {
    return "No journal store failures observed.";
  }
  const parts: string[] = [];
  if (input.lastJournalFailureReasons.length > 0) {
    parts.push(`Pass failures: ${input.lastJournalFailureReasons.join(", ")}`);
  }
  if (input.generationPublishFailureCount > 0) {
    const reasons = input.lastGenerationPublishFailureReasons.join(", ");
    parts.push(
      `Generation publish failures: ${input.generationPublishFailureCount}` +
        (reasons.length > 0 ? ` (${reasons})` : ""),
    );
  }
  return parts.join("\n");
}

// --- the derived stop-reason tokens -----------------------------------------------------------------

/**
 * Derive the closed stop-reason tokens of the settings snapshot from the
 * durable trail: the NEWEST closed token of each failure kind
 * (`journal_failure`, `publish_failure`, `wire_failure`), in that fixed
 * order. Pass outcomes, resets and the opaque request-id token never
 * become stop reasons; an empty trail derives no tokens.
 */
export function deriveSyncStopReasonTokens(
  entries: readonly SyncDiagnosticTrailEntry[],
): readonly SyncDiagnosticClosedToken[] {
  const newestByKind: Partial<Record<SyncDiagnosticKind, SyncDiagnosticClosedToken>> = {};
  for (const entry of entries) {
    if (!(STOP_REASON_KIND_ORDER as readonly string[]).includes(entry.kind)) {
      continue;
    }
    let closedToken: SyncDiagnosticClosedToken | undefined;
    for (const token of entry.tokens) {
      if (typeof token === "string") {
        closedToken = token;
        break;
      }
    }
    if (closedToken !== undefined) {
      // Entries are oldest first, so the last assignment is the newest.
      newestByKind[entry.kind] = closedToken;
    }
  }
  return STOP_REASON_KIND_ORDER.map((kind) => newestByKind[kind]).filter(
    (token): token is SyncDiagnosticClosedToken => token !== undefined,
  );
}

// --- the trail entry lines --------------------------------------------------------------------------

/** Render one trail token: the closed token, or the opaque request id. */
function renderTrailToken(token: SyncDiagnosticToken): string {
  return typeof token === "string" ? token : `request_id=${token.requestId}`;
}

/** Render one trail tail line: ISO-8601 UTC timestamp, kind, tokens. */
function renderTrailEntryLine(entry: SyncDiagnosticTrailEntry): string {
  const timestampText = new Date(entry.atEpochMs).toISOString();
  const tokenText = entry.tokens.map(renderTrailToken).join(" · ");
  return tokenText.length > 0
    ? `${timestampText} · ${entry.kind} · ${tokenText}`
    : `${timestampText} · ${entry.kind}`;
}

// --- the settings trail section ---------------------------------------------------------------------

/** The closed inputs of the settings trail section. */
export interface SyncDiagnosticsTrailSectionInput {
  /**
   * The derived closed stop-reason tokens (may be empty). The input is the
   * existing readonly closed-token union — a free-form server value cannot
   * type-check into the settings section.
   */
  readonly stopReasonTokens: readonly SyncDiagnosticClosedToken[];
  /** The total durable trail entry count. */
  readonly totalEntryCount: number;
  /** The bounded swallowed append/persist failure count. */
  readonly appendFailureCount: number;
  /** The trail entries available for the tail render (oldest first). */
  readonly entries: readonly SyncDiagnosticTrailEntry[];
}

/**
 * Render the settings trail section (sync error tracing task 2): the
 * derived stop reasons, the total entry count with the bounded
 * append-failure counter, and the last five trail entry lines. Closed
 * tokens, counts and timestamps only — never a path, credential or raw
 * error text.
 */
export function renderSyncDiagnosticsTrailSection(
  input: SyncDiagnosticsTrailSectionInput,
): string {
  const lines: string[] = [];
  if (input.stopReasonTokens.length > 0) {
    lines.push(`Stop reasons: ${input.stopReasonTokens.join(", ")}`);
  }
  lines.push(
    `Trail entries: ${input.totalEntryCount} · Append failures: ${input.appendFailureCount}`,
  );
  const tail = input.entries.slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT);
  if (tail.length === 0) {
    lines.push("No trail entries recorded yet.");
  } else {
    lines.push(...tail.map(renderTrailEntryLine));
  }
  return lines.join("\n");
}

// --- the export block -------------------------------------------------------------------------------

/** The closed inputs of the sanitized export block. */
export interface SyncDiagnosticsExportInput {
  /** The rendered status-bar line (closed text plus count), or null. */
  readonly syncStatusLine: string | null;
  /** The fixed blocker guidance lines of the status snapshot. */
  readonly syncBlockerGuidance: readonly string[];
  /** The journal-store diagnostics inputs of the settings line. */
  readonly journalStoreDiagnostics: JournalStoreDiagnosticsLineInput;
  /** The total durable trail entry count. */
  readonly trailEntryCount: number;
  /** The bounded swallowed append/persist failure count. */
  readonly trailAppendFailureCount: number;
  /** The trail entries available for the tail render (oldest first). */
  readonly trailTail: readonly SyncDiagnosticTrailEntry[];
}

/**
 * Build the sanitized export block of the `Copy sync diagnostics`
 * command: the current status snapshot line, the blocker guidance, the
 * journal-store diagnostics line, the aggregate counts and the trail tail
 * (kind + ISO-8601 UTC timestamp + tokens only). The block is the whole
 * egress surface — closed tokens, counts and timestamps, nothing else.
 */
export function renderSyncDiagnosticsExportBlock(
  input: SyncDiagnosticsExportInput,
): string {
  const lines: string[] = [SYNC_DIAGNOSTICS_EXPORT_CONTRACT];
  lines.push(`Status: ${input.syncStatusLine ?? "Journal not running on this device"}`);
  for (const guidanceLine of input.syncBlockerGuidance) {
    lines.push(`Blocker: ${guidanceLine}`);
  }
  lines.push("Journal store diagnostics:");
  const diagnosticsLines = renderJournalStoreDiagnosticsLine(
    input.journalStoreDiagnostics,
  ).split("\n");
  for (const diagnosticsLine of diagnosticsLines) {
    lines.push(diagnosticsLine.length > 0 ? `  ${diagnosticsLine}` : diagnosticsLine);
  }
  lines.push(`Trail entries: ${input.trailEntryCount}`);
  lines.push(`Trail append failures: ${input.trailAppendFailureCount}`);
  const tail = input.trailTail.slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT);
  if (tail.length === 0) {
    lines.push("Trail tail: none recorded");
  } else {
    lines.push(`Trail tail (last ${tail.length}):`);
    lines.push(...tail.map(renderTrailEntryLine));
  }
  return lines.join("\n");
}
