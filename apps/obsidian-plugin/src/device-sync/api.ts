/**
 * The hand-mirrored device sync API client (device cursor and manifest
 * reconciliation, task 9).
 *
 * The generated workspace client `@workspace/api-client` is intentionally
 * NOT bundled into Obsidian, so this module mirrors exactly the eight
 * device-sync wire shapes of spec 7 — the event pull, the cursor
 * acknowledgement, the five manifest run operations and the verified binary
 * download — behind the existing `obsidian_sync` device Bearer credential
 * over the binary-capable `DeviceSyncHttpTransport` seam (the shared raw
 * request grammar of the journal lane, extended with the response bytes and
 * lower-cased headers the verified download verifies). It adds no automatic
 * retry and no interpretation of provider detail: every server outcome maps
 * onto one
 * closed {@link DeviceSyncReason}, and every failure reports exactly ONE
 * observation through the injected {@link DeviceSyncDiagnostics} facade
 * (the cursor/reconcile/apply lane and stage of the failing operation, or
 * `credential_failure/access_missing` when no access token kept the request
 * off the transport entirely).
 *
 * Privacy (spec 9, 14): failures carry one closed reason and a static
 * message only — status numbers, URLs, response bodies, tokens and digests
 * never reach a thrown error. The two correlation facts a failure may
 * carry — the failing envelope's UUID-shaped request id (or the verified
 * download's UUID-shaped `x-request-id` header) and the envelope's error
 * code string — are gated by construction and by the diagnostics trail's
 * own closed-token boundary. The verified download verifies the exact byte
 * length and SHA-256 of the response BEFORE its buffer is ever returned: a
 * partial or truncated body is always `device_download_integrity_failed`,
 * never success.
 */

import type { components } from "@workspace/api-client";

import { sha256Hex } from "../exclusion-policy/canonical-json";
import { isCanonicalMediaType } from "../exclusion-policy/evaluator";
import type { FrozenFingerprint } from "../journal/contracts";
import { FROZEN_FINGERPRINT_SHA256_PATTERN } from "../journal/fingerprint";
import type { SyncHttpRequest } from "../journal/sync-api";
import type {
  ApplyFailureStage,
  CursorFailureStage,
  DeviceEventOperation,
  DeviceSyncDiagnostics,
  DeviceSyncFailureCorrelation,
  DeviceSyncReason,
  ManifestActionKind,
  ReconcileFailureStage,
} from "./contracts";
import {
  DEVICE_SYNC_ACTION_REASONS,
  DEVICE_SYNC_EVENT_OPERATIONS,
  DEVICE_SYNC_SERVER_REASONS,
  MANIFEST_ACTION_KINDS,
} from "./contracts";

// --- the hand-mirrored wire vocabularies --------------------------------------------------------

/**
 * The closed manifest run lifecycle states of the server registry (spec
 * 6.2/7.3): every member is a registered `ManifestRunState`, so an
 * unregistered snake_case state fails `tsc --noEmit` here, not at runtime.
 */
export const MANIFEST_RUN_STATES = [
  "collecting",
  "planned",
  "applying",
  "completed",
  "expired",
  "failed",
] as const satisfies readonly components["schemas"]["ManifestRunState"][];

/** One manifest run lifecycle state. */
export type ManifestRunState = (typeof MANIFEST_RUN_STATES)[number];

/** One planner/apply blocker token of a manifest action (server registry). */
export type ManifestActionReason = (typeof DEVICE_SYNC_ACTION_REASONS)[number];

const DEVICE_SYNC_SERVER_REASON_SET: ReadonlySet<string> = new Set<string>(DEVICE_SYNC_SERVER_REASONS);
const DEVICE_EVENT_OPERATION_SET: ReadonlySet<string> = new Set<string>(DEVICE_SYNC_EVENT_OPERATIONS);
const MANIFEST_ACTION_KIND_SET: ReadonlySet<string> = new Set<string>(MANIFEST_ACTION_KINDS);
const MANIFEST_ACTION_REASON_SET: ReadonlySet<string> = new Set<string>(DEVICE_SYNC_ACTION_REASONS);
const MANIFEST_RUN_STATE_SET: ReadonlySet<string> = new Set<string>(MANIFEST_RUN_STATES);

// --- hand-mirrored wire value types (spec 7.1-7.4) ------------------------------------------------

/** One immutable canonical device event with its operation operands. */
export interface DeviceSyncEvent {
  readonly eventId: string;
  readonly eventSequence: number;
  readonly operation: DeviceEventOperation;
  readonly sourceId: string;
  readonly originDeviceId: string | null;
  readonly baseVersionId: string | null;
  readonly currentVersionId: string | null;
  readonly baseFingerprint: FrozenFingerprint | null;
  readonly currentFingerprint: FrozenFingerprint | null;
  readonly priorLocator: string | null;
  readonly resultingLocator: string | null;
  readonly tombstoneId: string | null;
  readonly committedAt: string;
}

/** One bounded pull page of immutable events after the acknowledged cursor. */
export interface DeviceEventPage {
  readonly acknowledgedSequence: number;
  readonly deliveredThroughSequence: number;
  readonly pageCheckpointSequence: number;
  readonly events: readonly DeviceSyncEvent[];
  readonly hasMore: boolean;
}

/** The frozen cursor watermarks of one device. */
export interface DeviceCursorReceipt {
  readonly acknowledgedSequence: number;
  readonly deliveredThroughSequence: number;
}

/** The frozen state of one manifest run. */
export interface ManifestRunReceipt {
  readonly manifestRunId: string;
  readonly state: ManifestRunState;
  readonly baseAcknowledgedSequence: number;
  readonly checkpointSequence: number;
  readonly policyRevisionNumber: number;
  readonly clientObservationGeneration: number;
  readonly nextPageNumber: number;
  readonly entryCount: number;
  readonly expiresAt: string;
}

/** The frozen acceptance of one manifest page. */
export interface ManifestPageReceipt {
  readonly manifestRunId: string;
  readonly pageNumber: number;
  readonly acceptedEntryCount: number;
  readonly nextPageNumber: number;
}

/**
 * One frozen deterministic action of a planned run. A `download` action
 * carries the checkpoint-active locator text the device places its bytes
 * at (task 11b); every other kind renders the field closed (null).
 */
export interface ManifestAction {
  readonly actionIndex: number;
  readonly actionKind: ManifestActionKind;
  readonly localEntryId: string | null;
  readonly sourceId: string | null;
  readonly sourceVersionId: string | null;
  readonly sourceLocatorId: string | null;
  readonly sourceTombstoneId: string | null;
  readonly reason: ManifestActionReason | null;
  readonly checkpointLocator: string | null;
}

/** One stable ordered page of frozen actions. */
export interface ManifestActionPage {
  readonly manifestRunId: string;
  readonly actions: readonly ManifestAction[];
  readonly hasMore: boolean;
}

/**
 * The verified download of one exact source version: the exact response
 * bytes after their declared length and SHA-256 were both verified, plus
 * the declared digest, size and canonical media type of the content
 * headers. No provider detail, object key or URL ever rides along.
 */
export interface VerifiedDownload {
  readonly bytes: Uint8Array;
  readonly declaredSha256: string;
  readonly sizeBytes: number;
  readonly mediaType: string;
}

// --- hand-mirrored request input types ------------------------------------------------------------

/** The strict cursor acknowledgement body (spec 7.2). */
export interface CursorAcknowledgementInput {
  readonly expectedPreviousSequence: number;
  readonly appliedThroughSequence: number;
}

/** The strict manifest run start body (spec 7.3). */
export interface StartManifestInput {
  readonly clientObservationGeneration: number;
}

/** One locally observed manifest entry of one page body (spec 7.3). */
export interface ManifestEntryInput {
  readonly localEntryId: string;
  readonly normalizedLocator: string;
  readonly fingerprint: FrozenFingerprint;
  readonly observationGeneration: number;
  readonly knownSourceId?: string | null | undefined;
  readonly knownVersionId?: string | null | undefined;
}

/** The exact next ordered page of one manifest run (spec 7.3). */
export interface AppendManifestPageInput {
  readonly manifestRunId: string;
  readonly pageNumber: number;
  readonly entries: readonly ManifestEntryInput[];
  readonly pageDigest: string;
}

/** The finalize body with its total count and final digest (spec 7.3). */
export interface FinalizeManifestInput {
  readonly manifestRunId: string;
  readonly totalEntryCount: number;
  readonly finalDigest: string;
}

/** The bounded action-page query of one planned run (spec 7.3). */
export interface ManifestActionsInput {
  readonly manifestRunId: string;
  readonly afterActionIndex?: number | undefined;
  readonly limit?: number | undefined;
}

/** The completion body with the exact planned run's final digest. */
export interface CompleteManifestInput {
  readonly manifestRunId: string;
  readonly finalDigest: string;
}

/** The verified download request: one exact source version identity. */
export interface DownloadSourceVersionInput {
  readonly sourceId: string;
  readonly sourceVersionId: string;
}

// --- the closed failure surface ---------------------------------------------------------------------

/** One classified device sync failure: the closed reason, retryability and gated correlation. */
export interface DeviceSyncFailure {
  readonly reason: DeviceSyncReason;
  readonly retryable: boolean;
  readonly correlation: DeviceSyncFailureCorrelation | undefined;
}

/**
 * One mapped device sync failure. The message is the static closed reason
 * only — the URL, a status number, a response body, a credential or any
 * provider detail can never reach it. `requestId` and `wireErrorCode` are
 * non-null only when the failing body parsed as the canonical envelope (or,
 * for the verified download's integrity failure, when the response carried
 * a UUID-shaped `x-request-id` header).
 */
export class DeviceSyncApiError extends Error implements DeviceSyncFailureCorrelation {
  readonly reason: DeviceSyncReason;
  readonly retryable: boolean;
  readonly requestId: string | null;
  readonly wireErrorCode: string | null;

  constructor(
    reason: DeviceSyncReason,
    retryable: boolean,
    requestId: string | null = null,
    wireErrorCode: string | null = null,
  ) {
    super(`device sync api failed: ${reason}`);
    this.name = "DeviceSyncApiError";
    this.reason = reason;
    this.retryable = retryable;
    this.requestId = requestId;
    this.wireErrorCode = wireErrorCode;
  }
}

function deviceSyncApiError(
  reason: DeviceSyncReason,
  retryable: boolean,
  requestId: string | null = null,
  wireErrorCode: string | null = null,
): DeviceSyncApiError {
  return new DeviceSyncApiError(reason, retryable, requestId, wireErrorCode);
}

/**
 * Classify any thrown value onto the closed device sync failure vocabulary.
 * A {@link DeviceSyncApiError} mirrors its own facts; a `TypeError`-class
 * failure — the fetch-style network rejection — maps onto the retryable
 * `network_offline`; and every unclassified value maps onto the retryable
 * `server_error`, the documented safe default: an unmapped failure must
 * never silently drop sync work. Nothing but a parsed canonical envelope
 * ever yields a correlation.
 */
export function classifyDeviceSyncFailure(error: unknown): DeviceSyncFailure {
  if (error instanceof DeviceSyncApiError) {
    return {
      reason: error.reason,
      retryable: error.retryable,
      correlation: { requestId: error.requestId, wireErrorCode: error.wireErrorCode },
    };
  }
  if (error instanceof TypeError) {
    return { reason: "network_offline", retryable: true, correlation: undefined };
  }
  return { reason: "server_error", retryable: true, correlation: undefined };
}

// --- envelope parsing and the closed status/code table ----------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const RFC_3339_TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const NON_NEGATIVE_INTEGER_TEXT_PATTERN = /^\d+$/;
const MAXIMUM_SAFE_INTEGER = 9007199254740991;

interface WireEnvelope {
  readonly data: unknown;
  readonly error: { readonly code: unknown } | null;
  readonly request_id?: unknown;
}

/** The envelope's opaque request id, only when it is UUID-shaped. */
function parseEnvelopeRequestId(value: unknown): string | null {
  return typeof value === "string" && UUID_PATTERN.test(value) ? value : null;
}

/**
 * Parse one canonical response envelope. A malformed body and a body with
 * neither outcome both fail closed as the retryable `server_error`; an
 * error envelope maps through the closed status/code table below.
 */
function parseEnvelope(status: number, bodyText: string): { data: unknown; requestId: string | null } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    throw mapWireFailure(status, null, null);
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw mapWireFailure(status, null, null);
  }
  const envelope = parsed as Partial<WireEnvelope>;
  const requestId = parseEnvelopeRequestId(envelope.request_id);
  if (envelope.error !== null && envelope.error !== undefined) {
    const code = typeof envelope.error.code === "string" ? envelope.error.code : null;
    throw mapWireFailure(status, code, requestId);
  }
  if (envelope.data === null || envelope.data === undefined) {
    throw mapWireFailure(status, null, requestId);
  }
  return { data: envelope.data, requestId };
}

/**
 * The closed status/code mapping of the device-sync surfaces. A registered
 * device-sync server code lands verbatim as its own closed reason (the
 * server family IS the reason vocabulary); the retryable dependency outage
 * is the one retryable member of that family. The remaining statuses map
 * like the journal lane: 401 is an expired credential, a genuine API 403
 * (canonical envelope with a closed code) is a login verdict while an edge
 * 403 without the envelope is a transient wire failure, 429 is rate
 * limiting, and every unclassified condition fails safe onto the
 * retryable `server_error` — an unmapped failure must never silently drop
 * sync work.
 */
function mapWireFailure(status: number, code: string | null, requestId: string | null): DeviceSyncApiError {
  if (code !== null && DEVICE_SYNC_SERVER_REASON_SET.has(code)) {
    const reason = code as DeviceSyncReason;
    return deviceSyncApiError(
      reason,
      reason === "device_sync_dependency_unavailable",
      requestId,
      code,
    );
  }
  if (status === 401) {
    return deviceSyncApiError("access_expired", false, requestId, code);
  }
  if (status === 403) {
    // Like the journal lane (fix round 5, finding A): a genuine API 403
    // carries the canonical envelope with a closed code, while a 403 whose
    // body does not parse as that envelope — an edge/middleware block page
    // — is a transient wire failure, so it stays retryable.
    return deviceSyncApiError(
      code === null ? "server_error" : "login_required",
      code === null,
      requestId,
      code,
    );
  }
  if (status === 429) {
    return deviceSyncApiError("network_rate_limited", true, requestId, code);
  }
  return deviceSyncApiError("server_error", true, requestId, code);
}

// --- strict member parsing --------------------------------------------------------------------------

function malformed(): DeviceSyncApiError {
  return deviceSyncApiError("server_error", true);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNonNegativeInteger(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 0 &&
    value <= MAXIMUM_SAFE_INTEGER
  );
}

function requireNonNegativeInteger(value: unknown): number {
  if (!isNonNegativeInteger(value)) {
    throw malformed();
  }
  return value;
}

function requireUuid(value: unknown): string {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
    throw malformed();
  }
  return value;
}

function requireTimestamp(value: unknown): string {
  if (typeof value !== "string" || !RFC_3339_TIMESTAMP_PATTERN.test(value)) {
    throw malformed();
  }
  return value;
}

function optionalUuid(value: unknown): string | null {
  return value === null || value === undefined ? null : requireUuid(value);
}

function optionalText(value: unknown): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "string") {
    throw malformed();
  }
  return value;
}

function requireClosedMember(value: unknown, members: ReadonlySet<string>): string {
  if (typeof value !== "string" || !members.has(value)) {
    throw malformed();
  }
  return value;
}

/** Parse one wire fingerprint; a violation of the closed shape is malformed. */
function parseWireFingerprint(value: unknown): FrozenFingerprint | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (!isRecord(value)) {
    throw malformed();
  }
  const sha256 = value["sha256"];
  const sizeBytes = value["size_bytes"];
  const mediaType = value["media_type"];
  if (
    typeof sha256 !== "string" ||
    !FROZEN_FINGERPRINT_SHA256_PATTERN.test(sha256) ||
    !isNonNegativeInteger(sizeBytes) ||
    typeof mediaType !== "string" ||
    !isCanonicalMediaType(mediaType)
  ) {
    throw malformed();
  }
  return { sha256, sizeBytes, mediaType };
}

function parseDeviceSyncEvent(value: unknown): DeviceSyncEvent {
  if (!isRecord(value)) {
    throw malformed();
  }
  return {
    eventId: requireUuid(value["event_id"]),
    eventSequence: requireNonNegativeInteger(value["event_sequence"]),
    operation: requireClosedMember(value["event_type"], DEVICE_EVENT_OPERATION_SET) as DeviceEventOperation,
    sourceId: requireUuid(value["source_id"]),
    originDeviceId: optionalUuid(value["origin_device_id"]),
    baseVersionId: optionalUuid(value["base_version_id"]),
    currentVersionId: optionalUuid(value["current_version_id"]),
    baseFingerprint: parseWireFingerprint(value["base_fingerprint"]),
    currentFingerprint: parseWireFingerprint(value["current_fingerprint"]),
    priorLocator: optionalText(value["prior_locator"]),
    resultingLocator: optionalText(value["resulting_locator"]),
    tombstoneId: optionalUuid(value["tombstone_id"]),
    committedAt: requireTimestamp(value["committed_at"]),
  };
}

function parseDeviceEventPage(data: unknown): DeviceEventPage {
  if (!isRecord(data)) {
    throw malformed();
  }
  const events = data["events"];
  if (!Array.isArray(events)) {
    throw malformed();
  }
  const hasMore = data["has_more"];
  if (typeof hasMore !== "boolean") {
    throw malformed();
  }
  return {
    acknowledgedSequence: requireNonNegativeInteger(data["acknowledged_sequence"]),
    deliveredThroughSequence: requireNonNegativeInteger(data["delivered_through_sequence"]),
    pageCheckpointSequence: requireNonNegativeInteger(data["page_checkpoint_sequence"]),
    events: events.map(parseDeviceSyncEvent),
    hasMore,
  };
}

function parseCursorReceipt(data: unknown): DeviceCursorReceipt {
  if (!isRecord(data)) {
    throw malformed();
  }
  return {
    acknowledgedSequence: requireNonNegativeInteger(data["acknowledged_sequence"]),
    deliveredThroughSequence: requireNonNegativeInteger(data["delivered_through_sequence"]),
  };
}

function parseManifestRunReceipt(data: unknown): ManifestRunReceipt {
  if (!isRecord(data)) {
    throw malformed();
  }
  return {
    manifestRunId: requireUuid(data["manifest_run_id"]),
    state: requireClosedMember(data["state"], MANIFEST_RUN_STATE_SET) as ManifestRunState,
    baseAcknowledgedSequence: requireNonNegativeInteger(data["base_acknowledged_sequence"]),
    checkpointSequence: requireNonNegativeInteger(data["checkpoint_sequence"]),
    policyRevisionNumber: requireNonNegativeInteger(data["policy_revision_number"]),
    clientObservationGeneration: requireNonNegativeInteger(data["client_observation_generation"]),
    nextPageNumber: requireNonNegativeInteger(data["next_page_number"]),
    entryCount: requireNonNegativeInteger(data["entry_count"]),
    expiresAt: requireTimestamp(data["expires_at"]),
  };
}

function parseManifestPageReceipt(data: unknown): ManifestPageReceipt {
  if (!isRecord(data)) {
    throw malformed();
  }
  return {
    manifestRunId: requireUuid(data["manifest_run_id"]),
    pageNumber: requireNonNegativeInteger(data["page_number"]),
    acceptedEntryCount: requireNonNegativeInteger(data["accepted_entry_count"]),
    nextPageNumber: requireNonNegativeInteger(data["next_page_number"]),
  };
}

function parseManifestAction(value: unknown): ManifestAction {
  if (!isRecord(value)) {
    throw malformed();
  }
  return {
    actionIndex: requireNonNegativeInteger(value["action_index"]),
    actionKind: requireClosedMember(value["action_kind"], MANIFEST_ACTION_KIND_SET) as ManifestActionKind,
    localEntryId: optionalText(value["local_entry_id"]),
    sourceId: optionalUuid(value["source_id"]),
    sourceVersionId: optionalUuid(value["source_version_id"]),
    sourceLocatorId: optionalUuid(value["source_locator_id"]),
    sourceTombstoneId: optionalUuid(value["source_tombstone_id"]),
    reason:
      value["reason"] === null || value["reason"] === undefined
        ? null
        : (requireClosedMember(value["reason"], MANIFEST_ACTION_REASON_SET) as ManifestActionReason),
    // The download placement locator parses with the same strictness as an
    // event locator: a string or nothing, never any other shape.
    checkpointLocator: optionalText(value["checkpoint_locator"]),
  };
}

function parseManifestActionPage(data: unknown): ManifestActionPage {
  if (!isRecord(data)) {
    throw malformed();
  }
  const actions = data["actions"];
  if (!Array.isArray(actions)) {
    throw malformed();
  }
  const hasMore = data["has_more"];
  if (typeof hasMore !== "boolean") {
    throw malformed();
  }
  return {
    manifestRunId: requireUuid(data["manifest_run_id"]),
    actions: actions.map(parseManifestAction),
    hasMore,
  };
}

// --- the client ------------------------------------------------------------------------------------

/**
 * One raw device sync HTTP response: the status, the response text of a
 * canonical JSON envelope, the exact response bytes of a binary body (null
 * when the body carried none) and the response headers with lower-cased
 * names — the binary members feed the verified download's length/digest
 * verification.
 */
export interface DeviceSyncHttpResponse {
  readonly status: number;
  readonly bodyText: string;
  readonly bodyBytes: ArrayBuffer | null;
  readonly headers: Readonly<Record<string, string>>;
}

/**
 * The binary-capable transport seam behind the device sync client: the
 * shared raw request grammar in (GET pulls included), one response out; a
 * thrown error means the endpoint could not be reached. The adapter adds no
 * retry — the caller owns every retry decision.
 */
export type DeviceSyncHttpTransport = (request: SyncHttpRequest) => Promise<DeviceSyncHttpResponse>;

/** The diagnostics lane and stage one failing operation reports through. */
type FailureLane =
  | { readonly kind: "cursor"; readonly stage: CursorFailureStage }
  | { readonly kind: "reconcile"; readonly stage: ReconcileFailureStage }
  | { readonly kind: "apply"; readonly stage: ApplyFailureStage };

export interface DeviceSyncApiOptions {
  readonly transport: DeviceSyncHttpTransport;
  /** Resolved afresh per request so settings edits apply without a rebuild. */
  readonly resolveOrigin: () => string;
  /** The memory-only device access credential; null means login is required. */
  readonly getAccessToken: () => string | null;
  /**
   * The optional mid-session credential rotation: a request that fails with
   * the closed `access_expired` verdict triggers exactly one refresh and
   * one retry with the rotated credential. Without the hook the 401 stays
   * terminal (the resident-app finding of the 2026-08-27 physical matrix:
   * a foreground app whose access credential expired must heal itself, not
   * silently stop syncing until a restart).
   */
  readonly refreshAccessToken?: () => Promise<void>;
  /** The mandatory diagnostics surface every closed failure reports through. */
  readonly diagnostics: DeviceSyncDiagnostics;
}

export interface DeviceSyncApi {
  pullEvents(): Promise<DeviceEventPage>;
  acknowledgeCursor(input: CursorAcknowledgementInput): Promise<DeviceCursorReceipt>;
  startManifest(input: StartManifestInput): Promise<ManifestRunReceipt>;
  appendManifestPage(input: AppendManifestPageInput): Promise<ManifestPageReceipt>;
  finalizeManifest(input: FinalizeManifestInput): Promise<ManifestRunReceipt>;
  listManifestActions(input: ManifestActionsInput): Promise<ManifestActionPage>;
  completeManifest(input: CompleteManifestInput): Promise<DeviceCursorReceipt>;
  downloadSourceVersion(input: DownloadSourceVersionInput): Promise<VerifiedDownload>;
}

/**
 * Build the hand-mirrored device sync client over one injected transport.
 * Every call resolves the origin and credential afresh, presents the
 * credential only in the dedicated Bearer header, maps failures onto the
 * closed reason table with a static message, and reports exactly one
 * diagnostics observation per failure. A missing access token is rejected
 * before any transport attempt as `credential_failure/access_missing`.
 */
export function createDeviceSyncApi(options: DeviceSyncApiOptions): DeviceSyncApi {
  const { transport, resolveOrigin, getAccessToken, refreshAccessToken, diagnostics } = options;

  function requireAccessToken(): string {
    const accessToken = getAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      // Pre-contact rejection: no transport was attempted, so the failure
      // reports as credential_failure on the trail, not a lane failure.
      diagnostics.credentialFailure("access_missing", "login_required");
      throw deviceSyncApiError("login_required", false, null, null);
    }
    return accessToken;
  }

  function report(lane: FailureLane, reason: DeviceSyncReason, correlation: DeviceSyncFailureCorrelation): void {
    if (lane.kind === "cursor") {
      diagnostics.cursorFailure(lane.stage, reason, correlation);
      return;
    }
    if (lane.kind === "reconcile") {
      diagnostics.reconcileFailure(lane.stage, reason, correlation);
      return;
    }
    diagnostics.applyFailure(lane.stage, reason, correlation);
  }

  /**
   * Classify one transport rejection: the deadline family (`TimeoutError`,
   * and the adapter's `AbortError` for an already-abandoned request) is a
   * reached-but-unanswered `network_timeout`; every other rejection — DNS,
   * refused, TLS, a TypeError from a fetch-shaped seam — is
   * `network_offline`. Both stay retryable.
   */
  function transportFailure(error: unknown): DeviceSyncApiError {
    const name = error instanceof Error ? error.name : "";
    if (name === "TimeoutError" || name === "AbortError") {
      return deviceSyncApiError("network_timeout", true);
    }
    return deviceSyncApiError("network_offline", true);
  }

  async function send(request: SyncHttpRequest): Promise<DeviceSyncHttpResponse> {
    try {
      return await transport(request);
    } catch (error) {
      throw transportFailure(error);
    }
  }

  /**
   * Run one operation inside its diagnostics lane: exactly one observation
   * per failure, and a foreign throwable from a hostile seam collapses
   * onto the closed retryable `server_error` instead of leaking out.
   */
  async function run<T>(lane: FailureLane, execute: (accessToken: string) => Promise<T>): Promise<T> {
    const accessToken = requireAccessToken();
    try {
      return await execute(accessToken);
    } catch (error) {
      let failure = error instanceof DeviceSyncApiError ? error : malformed();
      if (failure.reason === "access_expired" && refreshAccessToken !== undefined) {
        try {
          await refreshAccessToken();
          const rotatedToken = requireAccessToken();
          return await execute(rotatedToken);
        } catch (retryError) {
          // A failed refresh or a still-failing retry keeps the freshest
          // closed verdict: the retry's own failure when it produced one,
          // otherwise the original access_expired outcome.
          if (retryError instanceof DeviceSyncApiError) {
            failure = retryError;
          }
        }
      }
      report(lane, failure.reason, { requestId: failure.requestId, wireErrorCode: failure.wireErrorCode });
      throw failure;
    }
  }

  /** Perform one JSON request and parse its canonical envelope. */
  async function performJson(accessToken: string, request: SyncHttpRequest): Promise<{ data: unknown; requestId: string | null }> {
    const response = await send({
      ...request,
      headers: { ...request.headers, authorization: `Bearer ${accessToken}` },
    });
    return parseEnvelope(response.status, response.bodyText);
  }

  function jsonRequest(url: string, method: "GET" | "POST" | "PUT", body?: string): SyncHttpRequest {
    return {
      url,
      method,
      headers: {
        accept: "application/json",
        // The live Desktop gate proved the real server rejects a JSON body
        // without an explicit content type (422 before any handler ran):
        // every carrying request names its media type exactly like the
        // journal lane's preflight does.
        ...(body === undefined ? {} : { "content-type": "application/json" }),
      },
      ...(body === undefined ? {} : { body }),
    };
  }

  return {
    async pullEvents(): Promise<DeviceEventPage> {
      return run({ kind: "cursor", stage: "pull" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(`${resolveOrigin()}/api/sync/events`, "GET"),
        );
        return parseDeviceEventPage(data);
      });
    },

    async acknowledgeCursor(input: CursorAcknowledgementInput): Promise<DeviceCursorReceipt> {
      return run({ kind: "cursor", stage: "acknowledge" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(`${resolveOrigin()}/api/sync/cursor-acknowledgements`, "POST", JSON.stringify({
            expected_previous_sequence: input.expectedPreviousSequence,
            applied_through_sequence: input.appliedThroughSequence,
          })),
        );
        return parseCursorReceipt(data);
      });
    },

    async startManifest(input: StartManifestInput): Promise<ManifestRunReceipt> {
      return run({ kind: "reconcile", stage: "start" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(`${resolveOrigin()}/api/sync/manifests`, "POST", JSON.stringify({
            client_observation_generation: input.clientObservationGeneration,
          })),
        );
        return parseManifestRunReceipt(data);
      });
    },

    async appendManifestPage(input: AppendManifestPageInput): Promise<ManifestPageReceipt> {
      return run({ kind: "reconcile", stage: "page" }, async (accessToken) => {
        const wireBody = JSON.stringify({
          entries: input.entries.map((entry) => ({
            local_entry_id: entry.localEntryId,
            normalized_locator: entry.normalizedLocator,
            fingerprint: {
              sha256: entry.fingerprint.sha256,
              size_bytes: entry.fingerprint.sizeBytes,
              media_type: entry.fingerprint.mediaType,
            },
            observation_generation: entry.observationGeneration,
            ...(entry.knownSourceId === null || entry.knownSourceId === undefined
              ? {}
              : { known_source_id: entry.knownSourceId }),
            ...(entry.knownVersionId === null || entry.knownVersionId === undefined
              ? {}
              : { known_version_id: entry.knownVersionId }),
          })),
          page_digest: input.pageDigest,
        });
        const { data } = await performJson(
          accessToken,
          jsonRequest(
            `${resolveOrigin()}/api/sync/manifests/${encodeURIComponent(input.manifestRunId)}/pages/${input.pageNumber}`,
            "PUT",
            wireBody,
          ),
        );
        return parseManifestPageReceipt(data);
      });
    },

    async finalizeManifest(input: FinalizeManifestInput): Promise<ManifestRunReceipt> {
      return run({ kind: "reconcile", stage: "finalize" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(
            `${resolveOrigin()}/api/sync/manifests/${encodeURIComponent(input.manifestRunId)}/finalize`,
            "POST",
            JSON.stringify({ total_entry_count: input.totalEntryCount, final_digest: input.finalDigest }),
          ),
        );
        return parseManifestRunReceipt(data);
      });
    },

    async listManifestActions(input: ManifestActionsInput): Promise<ManifestActionPage> {
      return run({ kind: "reconcile", stage: "actions" }, async (accessToken) => {
        const query = new URLSearchParams();
        if (input.afterActionIndex !== undefined) {
          query.set("after_action_index", String(input.afterActionIndex));
        }
        if (input.limit !== undefined) {
          query.set("limit", String(input.limit));
        }
        const suffix = query.size > 0 ? `?${query.toString()}` : "";
        const { data } = await performJson(
          accessToken,
          jsonRequest(
            `${resolveOrigin()}/api/sync/manifests/${encodeURIComponent(input.manifestRunId)}/actions${suffix}`,
            "GET",
          ),
        );
        return parseManifestActionPage(data);
      });
    },

    async completeManifest(input: CompleteManifestInput): Promise<DeviceCursorReceipt> {
      return run({ kind: "reconcile", stage: "complete" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(
            `${resolveOrigin()}/api/sync/manifests/${encodeURIComponent(input.manifestRunId)}/complete`,
            "POST",
            JSON.stringify({ final_digest: input.finalDigest }),
          ),
        );
        return parseCursorReceipt(data);
      });
    },

    async downloadSourceVersion(input: DownloadSourceVersionInput): Promise<VerifiedDownload> {
      return run({ kind: "apply", stage: "download" }, async (accessToken) => {
        const response = await send({
          url: `${resolveOrigin()}/api/sources/${encodeURIComponent(input.sourceId)}/versions/${encodeURIComponent(input.sourceVersionId)}/content`,
          method: "GET",
          headers: { authorization: `Bearer ${accessToken}`, accept: "application/octet-stream" },
        });
        if (response.status !== 200) {
          // Pre-stream failures render the canonical JSON envelope; a body
          // that parses as a success envelope at a failure status is a
          // malformed outcome and fails closed.
          const { requestId } = parseEnvelope(response.status, response.bodyText);
          throw deviceSyncApiError("server_error", true, requestId, null);
        }
        const headers = response.headers;
        const declaredSha256 = headers["x-content-sha256"];
        const declaredSizeText = headers["content-length"];
        const mediaType = headers["content-type"];
        const requestId = parseEnvelopeRequestId(headers["x-request-id"]);
        const bodyBytes = response.bodyBytes;
        const declaredSize =
          typeof declaredSizeText === "string" && NON_NEGATIVE_INTEGER_TEXT_PATTERN.test(declaredSizeText)
            ? Number.parseInt(declaredSizeText, 10)
            : null;
        if (
          bodyBytes === null ||
          declaredSize === null ||
          declaredSize > MAXIMUM_SAFE_INTEGER ||
          typeof declaredSha256 !== "string" ||
          !FROZEN_FINGERPRINT_SHA256_PATTERN.test(declaredSha256) ||
          typeof mediaType !== "string" ||
          !isCanonicalMediaType(mediaType)
        ) {
          throw deviceSyncApiError("device_download_integrity_failed", false, requestId, null);
        }
        const bytes = new Uint8Array(bodyBytes);
        if (bytes.byteLength !== declaredSize || (await sha256Hex(bytes)) !== declaredSha256) {
          throw deviceSyncApiError("device_download_integrity_failed", false, requestId, null);
        }
        return { bytes, declaredSha256, sizeBytes: declaredSize, mediaType };
      });
    },
  };
}
