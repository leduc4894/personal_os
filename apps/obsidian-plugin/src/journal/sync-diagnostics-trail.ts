/**
 * The durable closed-token sync diagnostics trail (sync error tracing
 * design: diagnostic trail event contract).
 *
 * One trail entry is `{ kind, atEpochMs, tokens }`: a closed kind, a
 * timestamp and a bounded list of tokens drawn ONLY from the existing
 * closed vocabularies (`QueuePassOutcome`, `JournalSafeErrorLabel`,
 * `JournalStoreErrorReason`, `SyncApiFailureKind` labels,
 * `LifecycleRunOutcome`) plus the one opaque envelope request id, the
 * server envelope error-code tokens (SERVER ENVELOPE CODES: the server
 * error registry's closed `error.code` vocabulary, admitted by the closed
 * snake_case shape only through {@link envelopeErrorCode} — the plugin
 * mirrors no registry client-side) and the fixed self-check verdict
 * tokens of the `self_check` kind. A free-form string cannot enter an
 * entry at the type level, and the sidecar parser rejects any token that
 * is not a closed snake_case token or a well-formed request id record.
 *
 * The trail persists as ONE JSON sidecar (`sync-diagnostics-trail.json`)
 * through the journal file store port bound to the Vault's plugin
 * directory. A corrupt or unreadable sidecar resets the trail to empty
 * and records a `trail_reset` entry; the trail never blocks or breaks
 * the sync path — append failures are swallowed into a bounded counter.
 *
 * Privacy (spec 9): no path, digest, credential, hostname or provider
 * detail may reach an entry, the sidecar or a diagnostic surface.
 */

import type { JournalSafeErrorLabel } from "./contracts";
import type { LifecycleRunOutcome } from "./lifecycle-driver";
import type { JournalFileStore } from "./persistence";
import type { QueuePassOutcome } from "./queue-driver";
import type { SyncApiFailureKind } from "./sync-api";
import type { JournalStoreErrorReason } from "./sqlite-database";

// --- frozen bounds and file vocabulary ------------------------------------------------------------

/** The one JSON sidecar holding the trail, inside the Vault plugin directory. */
export const SYNC_DIAGNOSTICS_TRAIL_FILE_NAME = "sync-diagnostics-trail.json";

/** The sidecar record contract identifier. */
export const SYNC_DIAGNOSTICS_TRAIL_CONTRACT = "obsidian_sync_diagnostics_trail/v1";

/** The entry count cap; the oldest entries are evicted beyond it. */
export const MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES = 128;

/** The per-entry token count cap; tokens beyond it are dropped. */
export const MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY = 8;

/** The append-failure counter cap (the counter is surfaced, never unbounded). */
export const MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES = 999;

// --- the closed kind vocabulary -------------------------------------------------------------------

/** The closed diagnostic kinds the trail records. */
export const SYNC_DIAGNOSTIC_KINDS = [
  "wire_failure",
  "pass_outcome",
  "journal_failure",
  "publish_failure",
  "trail_reset",
  "self_check",
] as const;

export type SyncDiagnosticKind = (typeof SYNC_DIAGNOSTIC_KINDS)[number];

// --- the closed token vocabulary ------------------------------------------------------------------

/**
 * The closed string tokens a trail entry may carry: every existing closed
 * outcome/label/reason/kind vocabulary. A plain string literal that is not
 * one of these tokens does not type-check against a trail entry.
 */
export type SyncDiagnosticClosedToken =
  | QueuePassOutcome
  | JournalSafeErrorLabel
  | JournalStoreErrorReason
  | SyncApiFailureKind
  | LifecycleRunOutcome
  | SyncSelfCheckVerdictToken
  | SyncEventStateToken;

/**
 * The closed row-state tokens a `journal_failure` entry may carry when a
 * retry park fails (sync error tracing park diagnosis round): each
 * `state_*` token names the closed journal event state the parked row read
 * back in AT the failure moment, with the two closed fallback tokens
 * `row_absent` (the read-back answered null or itself threw) and
 * `reason_unknown` (the park error carried no closed store reason).
 */
export const SYNC_EVENT_STATE_TOKENS = [
  "state_queued",
  "state_waiting_retry",
  "state_preflight",
  "state_uploading",
  "state_blocked_conflict",
  "state_excluded_policy",
  "state_blocked_size",
  "state_deferred_lifecycle",
  "state_integrity_failed",
  "state_committed",
  "state_no_change",
  "row_absent",
  "reason_unknown",
] as const;

export type SyncEventStateToken = (typeof SYNC_EVENT_STATE_TOKENS)[number];

/**
 * The fixed self-check verdict tokens (sync error tracing task 3): the
 * trail-persist probe outcome, the boolean credential-presence verdict and
 * the origin-reachability verdict. The network label that may ride along
 * with `origin_unreachable` stays in the sync failure vocabulary
 * (`network_offline`, `network_timeout`) instead of being duplicated here.
 */
export const SYNC_SELF_CHECK_VERDICT_TOKENS = [
  "trail_probe",
  "trail_persist_ok",
  "trail_persist_failed",
  "credential_present",
  "credential_absent",
  "origin_reachable",
  "origin_unreachable",
] as const;

export type SyncSelfCheckVerdictToken = (typeof SYNC_SELF_CHECK_VERDICT_TOKENS)[number];

/**
 * The brand that keeps the opaque envelope request id out of the closed
 * string vocabulary: it may enter ONLY through
 * {@link envelopeRequestId}, never as a free-form string token.
 */
declare const envelopeRequestIdBrand: unique symbol;

/** The one opaque wire identifier a trail entry may carry. */
export interface SyncDiagnosticRequestIdToken {
  readonly [envelopeRequestIdBrand]: never;
  readonly requestId: string;
}

/** Wrap one server envelope request id as the opaque trail token. */
export function envelopeRequestId(requestId: string): SyncDiagnosticRequestIdToken {
  return { requestId } as SyncDiagnosticRequestIdToken;
}

/**
 * Wrap one server envelope error code as the closed trail token, or answer
 * null when the code is not shaped like a closed snake_case token
 * (diagnostic round U1). These tokens are SERVER ENVELOPE CODES — the
 * server error registry's closed `error.code` vocabulary — not a
 * client-side union: the plugin mirrors no registry, so the trail boundary
 * whitelists them by the existing `CLOSED_TOKEN_PATTERN` shape only,
 * exactly like the sidecar load path does for every closed token. A
 * non-conforming code (challenge text, path-shaped or free-form values)
 * records nothing.
 */
export function envelopeErrorCode(code: string): SyncDiagnosticClosedToken | null {
  return CLOSED_TOKEN_PATTERN.test(code) ? (code as SyncDiagnosticClosedToken) : null;
}

/** One token of a trail entry: a closed token or the opaque request id. */
export type SyncDiagnosticToken = SyncDiagnosticClosedToken | SyncDiagnosticRequestIdToken;

/** One trail entry: a closed kind, a timestamp and bounded closed tokens. */
export interface SyncDiagnosticTrailEntry {
  readonly kind: SyncDiagnosticKind;
  readonly atEpochMs: number;
  readonly tokens: readonly SyncDiagnosticToken[];
}

/** The append input of the trail port (the entry timestamp is the trail's). */
export interface SyncDiagnosticsTrailAppendInput {
  readonly kind: SyncDiagnosticKind;
  readonly tokens: readonly SyncDiagnosticToken[];
}

// --- the trail port --------------------------------------------------------------------------------

/**
 * The durable diagnostics trail port. `append` never rejects and never
 * throws: the seams call it fire-and-forget, so the trail observes sync
 * without ever acting on it or blocking it.
 */
export interface SyncDiagnosticsTrail {
  /** Load the sidecar; a corrupt or unreadable sidecar resets to empty. */
  load(): Promise<void>;
  /** Append one entry; resolves after the coalesced persist, never rejects. */
  append(input: SyncDiagnosticsTrailAppendInput): Promise<void>;
  /** The bounded entry view, oldest first. */
  readEntries(): readonly SyncDiagnosticTrailEntry[];
  /** The bounded count of swallowed append/persist failures. */
  readAppendFailureCount(): number;
}

export interface SyncDiagnosticsTrailOptions {
  readonly fileStore: JournalFileStore;
  /** Clock for entry timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
}

// --- sidecar parsing ------------------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * The structural guard for loaded closed tokens: every closed vocabulary
 * token is a lowercase snake_case word. Path-shaped, credential-shaped and
 * free-form text (slashes, dots, spaces, separators) cannot match.
 */
const CLOSED_TOKEN_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

const DIAGNOSTIC_KIND_SET: ReadonlySet<string> = new Set<string>(SYNC_DIAGNOSTIC_KINDS);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Parse one loaded token; a violation fails the whole sidecar. */
function parseTrailToken(value: unknown): SyncDiagnosticToken | null {
  if (typeof value === "string") {
    // The pattern check is the runtime guard; the cast re-enters the closed
    // vocabulary only after it passed (untrusted sidecar bytes).
    return CLOSED_TOKEN_PATTERN.test(value)
      ? (value as SyncDiagnosticClosedToken)
      : null;
  }
  if (isRecord(value)) {
    const requestId = value["request_id"];
    if (typeof requestId === "string" && UUID_PATTERN.test(requestId)) {
      return envelopeRequestId(requestId);
    }
  }
  return null;
}

/** Parse the sidecar bytes; any malformed or foreign record answers null. */
function parseTrailSidecar(bytes: Uint8Array): readonly SyncDiagnosticTrailEntry[] | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
  if (!isRecord(parsed) || parsed["contract"] !== SYNC_DIAGNOSTICS_TRAIL_CONTRACT) {
    return null;
  }
  const rawEntries = parsed["entries"];
  if (!Array.isArray(rawEntries)) {
    return null;
  }
  const entries: SyncDiagnosticTrailEntry[] = [];
  for (const rawEntry of rawEntries) {
    if (!isRecord(rawEntry)) {
      return null;
    }
    const kind = rawEntry["kind"];
    const atEpochMs = rawEntry["at_epoch_ms"];
    const rawTokens = rawEntry["tokens"];
    if (typeof kind !== "string" || !DIAGNOSTIC_KIND_SET.has(kind)) {
      return null;
    }
    const closedKind = kind as SyncDiagnosticKind;
    if (typeof atEpochMs !== "number" || !Number.isInteger(atEpochMs) || atEpochMs < 0) {
      return null;
    }
    if (!Array.isArray(rawTokens) || rawTokens.length > MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY) {
      return null;
    }
    const tokens: SyncDiagnosticToken[] = [];
    for (const rawToken of rawTokens) {
      const token = parseTrailToken(rawToken);
      if (token === null) {
        return null;
      }
      tokens.push(token);
    }
    entries.push({ kind: closedKind, atEpochMs, tokens });
  }
  return entries;
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
}

// --- the trail implementation ----------------------------------------------------------------------

class SyncDiagnosticsTrailImpl implements SyncDiagnosticsTrail {
  readonly #fileStore: JournalFileStore;
  readonly #nowEpochMs: () => number;
  #entries: SyncDiagnosticTrailEntry[] = [];
  #appendFailureCount = 0;
  #hasPendingPersist = false;
  #appendDrain: Promise<void> | null = null;

  constructor(options: SyncDiagnosticsTrailOptions) {
    this.#fileStore = options.fileStore;
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
  }

  async load(): Promise<void> {
    let isPresent: boolean;
    try {
      isPresent = await this.#fileStore.exists(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME);
    } catch {
      await this.#resetAfterUnreadableSidecar();
      return;
    }
    if (!isPresent) {
      return;
    }
    let bytes: Uint8Array;
    try {
      bytes = new Uint8Array(await this.#fileStore.readBinary(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME));
    } catch {
      await this.#resetAfterUnreadableSidecar();
      return;
    }
    const parsed = parseTrailSidecar(bytes);
    if (parsed === null) {
      await this.#resetAfterUnreadableSidecar();
      return;
    }
    this.#entries = parsed.slice(-MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES).map((entry) => ({ ...entry }));
  }

  append(input: SyncDiagnosticsTrailAppendInput): Promise<void> {
    this.#entries.push({
      kind: input.kind,
      atEpochMs: this.#nowEpochMs(),
      tokens: input.tokens.slice(0, MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY),
    });
    if (this.#entries.length > MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES) {
      this.#entries.splice(0, this.#entries.length - MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES);
    }
    this.#hasPendingPersist = true;
    if (this.#appendDrain === null) {
      this.#appendDrain = this.#drainPendingPersists();
    }
    return this.#appendDrain;
  }

  readEntries(): readonly SyncDiagnosticTrailEntry[] {
    return this.#entries.map((entry) => ({ ...entry, tokens: [...entry.tokens] }));
  }

  readAppendFailureCount(): number {
    return this.#appendFailureCount;
  }

  /** Reset to empty and durably record the reset; never throws. */
  #resetAfterUnreadableSidecar(): Promise<void> {
    this.#entries = [];
    return this.append({ kind: "trail_reset", tokens: [] });
  }

  /**
   * The single serialized persist loop: one write at a time, and appends
   * that arrive while a write is in flight coalesce into the next one.
   */
  async #drainPendingPersists(): Promise<void> {
    try {
      while (this.#hasPendingPersist) {
        this.#hasPendingPersist = false;
        try {
          await this.#fileStore.writeBinary(
            SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
            toArrayBuffer(this.#serializeEntries()),
          );
        } catch {
          this.#appendFailureCount = Math.min(
            MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES,
            this.#appendFailureCount + 1,
          );
        }
      }
    } finally {
      this.#appendDrain = null;
    }
  }

  #serializeEntries(): Uint8Array {
    const record = {
      contract: SYNC_DIAGNOSTICS_TRAIL_CONTRACT,
      entries: this.#entries.map((entry) => ({
        kind: entry.kind,
        at_epoch_ms: entry.atEpochMs,
        tokens: entry.tokens.map((token) =>
          typeof token === "string" ? token : { request_id: token.requestId },
        ),
      })),
    };
    return new TextEncoder().encode(JSON.stringify(record));
  }
}

/** Build the durable diagnostics trail over one journal file store. */
export function createSyncDiagnosticsTrail(options: SyncDiagnosticsTrailOptions): SyncDiagnosticsTrail {
  return new SyncDiagnosticsTrailImpl(options);
}
