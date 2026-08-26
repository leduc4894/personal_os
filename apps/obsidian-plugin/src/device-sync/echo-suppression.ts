/**
 * The exact echo suppressor (device cursor and manifest reconciliation,
 * task 10, spec 8.2).
 *
 * A watcher observation of the plugin's OWN remote apply is suppressed
 * only when EVERY applicable operand matches the durable marker exactly:
 * the server event sequence, the source, the operation, the applicable
 * prior/target locators and the expected final fingerprint. A mismatch
 * remains a real watcher event with the marker retained. Markers are
 * durable, so a restart snapshot proof may still consume an exact
 * marker; there is deliberately NO time-window wildcard — age alone can
 * never suppress or expire a marker.
 *
 * Like the other device-sync modules this file imports no Node.js,
 * Electron or Obsidian file-system adapter API at module load time, so it
 * stays loadable on mobile.
 */

import type { FrozenFingerprint } from "../journal/contracts";
import type { SqliteQueryResult } from "../journal/sqlite-database";
import type {
  DeviceEventOperation,
  DeviceSyncRepository,
  EchoMarker,
  VaultObservation,
} from "./contracts";
import type { DeviceSyncRepositoryDatabase } from "./repository";
import { ECHO_MARKER_COLUMNS, parseEchoMarkerRow } from "./schema";

// --- the watcher-facing observation shapes -----------------------------------------------------------

/** One settled create/modify observation offered for echo suppression. */
export interface WatcherContentObservation {
  readonly normalizedLocator: string;
  readonly sourceId: string | null;
  readonly fingerprint: FrozenFingerprint;
}

/** One rename/move observation offered for echo suppression. */
export interface WatcherRenameObservation {
  readonly priorLocator: string;
  readonly targetLocator: string;
  readonly sourceId: string | null;
  readonly fingerprint: FrozenFingerprint | null;
}

/** One delete observation offered for echo suppression. */
export interface WatcherDeleteObservation {
  readonly priorLocator: string;
  readonly sourceId: string | null;
}

/** The exact echo suppression surface the watcher capture integrates with. */
export interface EchoSuppressor {
  /** Offer one fully identified observation (the restart/recovery proof). */
  matchAndConsume(observation: VaultObservation): Promise<boolean>;
  /** Offer one settled content observation; true means it was our own apply's echo. */
  consumeContentObservation(observation: WatcherContentObservation): Promise<boolean>;
  /** Offer one rename/move observation; true means it was our own apply's echo. */
  consumeRenameObservation(observation: WatcherRenameObservation): Promise<boolean>;
  /** Offer one delete observation; true means it was our own apply's echo. */
  consumeDeleteObservation(observation: WatcherDeleteObservation): Promise<boolean>;
}

export interface EchoSuppressorOptions {
  /** The durable reconciliation repository (marker consume runs in its single writer). */
  readonly repository: DeviceSyncRepository;
  /** The same read seam the repository uses, for the locator-based marker lookup. */
  readonly database: DeviceSyncRepositoryDatabase;
}

/** Render one locator as a SQL text literal (quotes doubled). */
function sqlText(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

const CONTENT_MARKER_OPERATIONS: ReadonlySet<string> = new Set(["created", "updated", "restored"]);
const RENAME_MARKER_OPERATIONS: ReadonlySet<string> = new Set(["renamed", "moved"]);

/**
 * Build the exact echo suppressor. The locator-based watcher lookups
 * read the candidate markers read-only and then consume through
 * {@link DeviceSyncRepository.matchAndConsumeEcho}, so the exact match
 * and the delete land in one serialized mutation.
 */
export function createEchoSuppressor(options: EchoSuppressorOptions): EchoSuppressor {
  const { repository, database } = options;

  function readMarkersByLocator(locator: string): EchoMarker[] {
    const rows: readonly SqliteQueryResult[] = database.readAll(
      [
        `select ${ECHO_MARKER_COLUMNS.join(", ")} from echo_markers`,
        `where prior_locator = ${sqlText(locator)} or target_locator = ${sqlText(locator)}`,
        "order by event_sequence asc;",
      ].join(" "),
    );
    const markers: EchoMarker[] = [];
    for (const row of rows[0]?.values ?? []) {
      markers.push(parseEchoMarkerRow(row));
    }
    return markers;
  }

  async function consumeFirstExact(
    candidates: readonly EchoMarker[],
    buildObservation: (marker: EchoMarker) => VaultObservation,
  ): Promise<boolean> {
    for (const marker of candidates) {
      if (await repository.matchAndConsumeEcho(buildObservation(marker))) {
        return true;
      }
    }
    return false;
  }

  return {
    async matchAndConsume(observation: VaultObservation): Promise<boolean> {
      return repository.matchAndConsumeEcho(observation);
    },

    async consumeContentObservation(observation: WatcherContentObservation): Promise<boolean> {
      const candidates = readMarkersByLocator(observation.normalizedLocator).filter((marker) =>
        CONTENT_MARKER_OPERATIONS.has(marker.operation),
      );
      return consumeFirstExact(candidates, (marker) => ({
        eventSequence: marker.eventSequence,
        sourceId: observation.sourceId,
        operation: marker.operation as DeviceEventOperation,
        priorLocator: marker.priorLocator,
        targetLocator: marker.targetLocator,
        fingerprint: observation.fingerprint,
      }));
    },

    async consumeRenameObservation(observation: WatcherRenameObservation): Promise<boolean> {
      const candidates = readMarkersByLocator(observation.priorLocator).filter(
        (marker) =>
          RENAME_MARKER_OPERATIONS.has(marker.operation) &&
          marker.targetLocator === observation.targetLocator,
      );
      return consumeFirstExact(candidates, (marker) => ({
        eventSequence: marker.eventSequence,
        sourceId: observation.sourceId,
        operation: marker.operation as DeviceEventOperation,
        priorLocator: marker.priorLocator,
        targetLocator: marker.targetLocator,
        fingerprint: observation.fingerprint,
      }));
    },

    async consumeDeleteObservation(observation: WatcherDeleteObservation): Promise<boolean> {
      const candidates = readMarkersByLocator(observation.priorLocator).filter(
        (marker) => marker.operation === "deleted",
      );
      return consumeFirstExact(candidates, (marker) => ({
        eventSequence: marker.eventSequence,
        sourceId: observation.sourceId,
        operation: "deleted",
        priorLocator: marker.priorLocator,
        targetLocator: marker.targetLocator,
        fingerprint: null,
      }));
    },
  };
}
