/**
 * The hand-mirrored small-file sync API client (spec 10, 12) and its
 * resumable multipart surface (child 7 spec 5).
 *
 * The generated workspace client `@workspace/api-client` is intentionally
 * NOT bundled into Obsidian, so this module mirrors exactly the sync wire
 * shapes the plugin consumes — `POST /api/sync/journal-events/preflight`,
 * `PUT /api/uploads/{operation_id}/content` and the five multipart session
 * endpoints — behind the existing `obsidian_sync` device Bearer credential
 * and the canonical response envelope. It adds no automatic retry and no
 * interpretation of provider detail: the plugin never receives an R2 key,
 * presigned URL or canonical object-store identity it did not ask for, and
 * every server outcome maps onto a closed plugin-side kind.
 *
 * The multipart part-PUT is the one transport call outside the envelope:
 * it transmits the exact part bytes to the one short-lived presigned URL
 * with NO service credential and NO application body beyond those bytes,
 * and its response object is consumed at the transport boundary — the
 * caller receives only the closed classification, never the response or
 * the URL.
 *
 * Privacy (spec 2, 9): failures carry one closed {@link SyncApiFailureKind}
 * and a static message only — status numbers, registry codes, response
 * bodies, URLs, tokens, paths and digests never reach a thrown error or a
 * diagnostic surface. The content stream sends exactly the re-fingerprinted
 * Vault bytes and nothing else.
 */

import type { components } from "@workspace/api-client";

import type {
  FrozenFingerprint,
  JournalOperation,
  MultipartSessionState,
} from "./contracts";
import {
  MAX_MULTIPART_PART_COUNT,
  MULTIPART_PART_SIZE_BYTES,
  MULTIPART_SESSION_STATES,
} from "./contracts";
import { isUuid } from "./repository";

// --- the raw sync transport port -------------------------------------------------------------

/**
 * One raw sync HTTP request: a JSON text body, an exact byte buffer or no
 * body at all (the GET pulls of the device-sync routes, whose client shares
 * this request grammar). The body member is optional so a GET request never
 * carries an empty-string payload the server could misread as a body.
 */
export interface SyncHttpRequest {
  readonly url: string;
  readonly method: "GET" | "POST" | "PUT";
  readonly headers: Readonly<Record<string, string>>;
  readonly body?: string | ArrayBuffer | undefined;
}

/** One raw sync HTTP response: the status and the response text. */
export interface SyncHttpResponse {
  readonly status: number;
  readonly bodyText: string;
}

/**
 * The pure transport seam behind the sync client: one request in, one
 * response out; a thrown error means the endpoint could not be reached. The
 * adapter adds no retry — the queue driver owns every retry decision.
 */
export type SyncHttpTransport = (request: SyncHttpRequest) => Promise<SyncHttpResponse>;

// --- closed failure kinds (spec 12) -------------------------------------------------------------

/**
 * The closed failure vocabulary of the sync client, mirroring the spec-12
 * retry matrix: the four retryable network conditions, the two credential
 * conditions the driver resolves through its one-per-pass refresh, and the
 * terminal integrity/size/conflict rejections. `blocked_conflict` carries
 * the server's typed, non-retryable business-conflict verdicts — the
 * create-time `source_locator_conflict` (a bound path already owned by a
 * foreign ACTIVE locator) — so the queue parks the event instead of
 * retrying a verdict that can never succeed. `operation_retry_required`
 * covers the server's closed upload-operation failures. Its safe resume flag
 * distinguishes a claimed receive from an unknown or expired token without
 * widening the cross-language failure vocabulary. `policy_denied` (child 7
 * spec 7) is the rechecked mid-transfer policy denial of the multipart
 * surface: a genuine API 403 carrying the closed `multipart_policy_denied`
 * envelope code that must never collapse onto the login verdict — the queue
 * terminalizes it exactly like a preflight `excluded` outcome.
 */
export const SYNC_API_FAILURE_KINDS = [
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "access_expired",
  "login_required",
  "blocked_size",
  "blocked_conflict",
  "integrity_failed",
  "policy_denied",
  "operation_retry_required",
] as const;

export type SyncApiFailureKind = (typeof SYNC_API_FAILURE_KINDS)[number];

/**
 * The canonical envelope error codes consumed by this hand-mirrored sync
 * client. The runtime list is checked against the generated API registry
 * type so diagnostics can whitelist known codes without admitting arbitrary
 * snake_case strings.
 */
export const SYNC_API_ENVELOPE_ERROR_CODES = [
  "device_credential_invalid",
  "authorization_scope_denied",
  "authentication_rate_limited",
  "internal_error",
  "database_connection_unavailable",
  "exclusion_policy_denied",
  "exclusion_policy_not_initialized",
  "exclusion_policy_signing_unavailable",
  "small_file_preflight_invalid",
  "small_file_size_limit_exceeded",
  "source_locator_conflict",
  "small_file_content_integrity_failed",
  "small_file_operation_identity_mismatch",
  "small_file_operation_not_found",
  "small_file_operation_expired",
  "small_file_upload_state_invalid",
  // The closed multipart registry block (child 7 spec 7) the multipart
  // surface consumes.
  "multipart_session_not_found",
  "multipart_session_expired",
  "multipart_session_state_invalid",
  "multipart_part_invalid",
  "multipart_part_url_rejected",
  "multipart_provider_state_invalid",
  "multipart_completion_in_progress",
  "multipart_integrity_failed",
  "multipart_policy_denied",
  "multipart_cleanup_failed",
  "multipart_local_content_changed",
  "multipart_dependency_unavailable",
] as const satisfies readonly components["schemas"]["ErrorCode"][];

export type SyncApiEnvelopeErrorCode =
  (typeof SYNC_API_ENVELOPE_ERROR_CODES)[number];

/**
 * One mapped sync failure: a closed kind and a static message. The message
 * never carries the URL, status, registry code, credential or any response
 * text, so a thrown error is redacted by construction. Two extra facts a
 * failure may carry, both only when the failing body parsed as the canonical
 * envelope: the envelope's opaque `requestId` (when the id is UUID-shaped)
 * so the client-side diagnostics trail can join a failure to the server-side
 * log of the same request, and the envelope's closed error-code string
 * `wireErrorCode` (diagnostic round U1) naming WHICH server registry code
 * rejected the request — round 5's discrimination already parsed it to tell
 * an API 403 from an edge 403, and now it survives the mapping instead of
 * being discarded. The code stays out of the static message; the diagnostics
 * trail boundary whitelists it against the declared runtime vocabulary
 * before it is ever recorded.
 *
 * `isCredentialAbsent` (trail v2 taxonomy, task 7) marks the ONE pre-contact
 * login rejection: the resolved access credential was missing or empty, so
 * the call failed BEFORE any transport was attempted. Callers classify that
 * failure as `credential_failure` on the diagnostics trail instead of a
 * wire failure; every mapped failure above keeps the flag false because an
 * HTTP attempt actually reached the transport.
 */
export class SyncApiError extends Error {
  readonly kind: SyncApiFailureKind;
  readonly canResumeClaimedOperation: boolean;
  readonly requestId: string | null;
  readonly wireErrorCode: string | null;
  readonly isCredentialAbsent: boolean;

  constructor(
    kind: SyncApiFailureKind,
    canResumeClaimedOperation = false,
    requestId: string | null = null,
    wireErrorCode: string | null = null,
    isCredentialAbsent = false,
  ) {
    super(`sync api failed: ${kind}`);
    this.name = "SyncApiError";
    this.kind = kind;
    this.canResumeClaimedOperation = canResumeClaimedOperation;
    this.requestId = requestId;
    this.wireErrorCode = wireErrorCode;
    this.isCredentialAbsent = isCredentialAbsent;
  }
}

function syncApiError(
  kind: SyncApiFailureKind,
  canResumeClaimedOperation = false,
  requestId: string | null = null,
  wireErrorCode: string | null = null,
  isCredentialAbsent = false,
): SyncApiError {
  return new SyncApiError(kind, canResumeClaimedOperation, requestId, wireErrorCode, isCredentialAbsent);
}

// --- hand-mirrored wire shapes (spec 10.1, 10.3) --------------------------------------------------

/** The plugin-side preflight intent: journal identity plus declared fingerprint. */
export interface JournalEventPreflightInput {
  readonly eventId: string;
  readonly idempotencyKey: string;
  readonly operation: JournalOperation;
  readonly localFileId: string;
  readonly sourceId: string | null;
  readonly baseVersionId: string | null;
  readonly normalizedLocator: string;
  readonly fingerprint: FrozenFingerprint;
  readonly policyRevisionNumber: number;
}

/**
 * The safe canonical receipt of one terminal result (spec 10.3): the
 * original source/version identity the plugin persists. It carries no
 * object key, provider detail or digest.
 */
export interface SmallFileTerminalReceipt {
  readonly sourceId: string;
  readonly sourceVersionId: string;
  readonly contentVersion: number;
}

/**
 * Exactly one typed preflight outcome (spec 10.1 table plus child 7 spec 4):
 * the only upload continuations are `single_part_upload` with its opaque
 * operation handle and `multipart_upload`, which opens no operation token
 * and carries no signed URL, key or provider detail — the runner obtains
 * the session through the dedicated create endpoint. Every other outcome
 * finishes the event without uploading. The child 8 `conflict` verdict
 * carries at most one of its two safe payloads: the capture grant
 * (`operationId`) whose verified candidate the journal uploads through the
 * conflict-content route, or the stored conflict identity
 * (`conflictId`) a same-identity replay answers after capture — neither
 * when no single-part verified-object transport could be granted.
 */
export type JournalPreflightOutcome =
  | { readonly outcome: "single_part_upload"; readonly operationId: string }
  | { readonly outcome: "multipart_upload" }
  | { readonly outcome: "committed_replay"; readonly receipt: SmallFileTerminalReceipt }
  | { readonly outcome: "no_change"; readonly receipt: SmallFileTerminalReceipt }
  | { readonly outcome: "excluded" }
  | {
      readonly outcome: "conflict";
      readonly operationId: string | null;
      readonly conflictId: string | null;
    };

/** The content-stream request: the preflight-bound handle and the exact bytes. */
export interface SmallFileContentUploadInput {
  readonly operationId: string;
  readonly contentBytes: Uint8Array;
}

/**
 * The opaque receipt of one conflict-candidate capture upload (child 8
 * spec 5.1): only the stored conflict identity crosses back — no receipt,
 * object key, provider detail or digest.
 */
export interface ConflictCaptureReceipt {
  readonly conflictId: string;
}

/** The capture-upload request: the granted capture handle and the exact bytes. */
export interface SmallFileConflictCandidateUploadInput {
  readonly operationId: string;
  readonly contentBytes: Uint8Array;
}

/**
 * The strict journal-event wire body shared by the preflight request and
 * the multipart session create (child 7 spec 5): the create call binds the
 * very same frozen operation the preflight decided, so both calls send the
 * identical member grammar.
 */
function buildJournalEventWireBody(input: JournalEventPreflightInput): Record<string, unknown> {
  return {
    event_id: input.eventId,
    idempotency_key: input.idempotencyKey,
    operation: input.operation,
    local_file_id: input.localFileId,
    ...(input.sourceId === null ? {} : { source_id: input.sourceId }),
    ...(input.baseVersionId === null ? {} : { base_version_id: input.baseVersionId }),
    normalized_locator: input.normalizedLocator,
    sha256: input.fingerprint.sha256,
    size_bytes: input.fingerprint.sizeBytes,
    media_type: input.fingerprint.mediaType,
    policy_revision: input.policyRevisionNumber,
  };
}

// --- hand-mirrored multipart wire shapes (child 7 spec 4, 5) -------------------------------------

/**
 * The server-owned plan of one permitted multipart transfer: the opaque
 * public session ID, the frozen geometry and the 24-hour expiry — and
 * nothing else. No signed URL, staging key, provider identity, ETag,
 * receipt or storage detail ever crosses with the plan.
 */
export interface MultipartSessionPlan {
  readonly sessionId: string;
  readonly partCount: number;
  readonly partSizeBytes: number;
  readonly expiresAtEpochMs: number;
}

/**
 * The frozen terminal result of one finished session: the canonical
 * receipt plus its closed result kind, so a committed and a no-change
 * promotion land on the journal exactly like their single-part peers.
 */
export interface MultipartTerminalResult extends SmallFileTerminalReceipt {
  readonly resultKind: "committed" | "no_change";
}

/**
 * The safe observable state of one multipart session: the plan members,
 * the closed server session state, the provider-reconciled completed part
 * numbers and — only once committed — the frozen terminal receipt.
 */
export interface MultipartSessionStatus extends MultipartSessionPlan {
  readonly state: MultipartSessionState;
  readonly completedPartNumbers: readonly number[];
  readonly terminalResult: MultipartTerminalResult | null;
}

/**
 * One short-lived presigned part authorization: exactly one bearer URL,
 * its own expiry, the numbered part and the exact derived byte window the
 * single PUT must transmit. The URL is the only field no other surface may
 * render: the runner uses it once, discards it and never persists it.
 */
export interface MultipartPartUrlAuthorization {
  readonly url: string;
  readonly partNumber: number;
  readonly offsetBytes: number;
  readonly sizeBytes: number;
  readonly expiresAtEpochMs: number;
}

/** The completion answer: the claim's persisted state and, once committed, the frozen result. */
export interface MultipartCompletion {
  readonly state: MultipartSessionState;
  readonly terminalReceipt: MultipartTerminalResult | null;
}

/** The closed classification of one presigned part PUT: accepted, or the URL itself rejected. */
export type MultipartPartPutResult = "uploaded" | "url_rejected";

/** One part-URL request: the session handle and the exact part number. */
export interface MultipartPartUrlRequest {
  readonly sessionId: string;
  readonly partNumber: number;
}

/** One presigned part PUT: the single-use URL and the exact part bytes. */
export interface MultipartPartPutRequest {
  readonly url: string;
  readonly contentBytes: Uint8Array;
}

/**
 * The opaque public session-ID grammar mirrored from the server boundary
 * (child 7 spec 5): printable URL-safe base64url of 32 to 128 characters,
 * never the raw canonical UUID form. Because `:`, `/`, `?`, `&` and `=`
 * are not base64url characters, no signed URL, query signature, provider
 * handle or other locator text can survive this grammar.
 */
const MULTIPART_SESSION_ID_WIRE_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

function isMultipartSessionIdWireShape(value: unknown): value is string {
  return typeof value === "string" && MULTIPART_SESSION_ID_WIRE_PATTERN.test(value) && !isUuid(value);
}

function parseExpiresAtEpochMs(value: unknown): number {
  if (typeof value !== "string") {
    throw syncApiError("server_error");
  }
  const expiresAtEpochMs = Date.parse(value);
  if (!Number.isFinite(expiresAtEpochMs) || expiresAtEpochMs < 0) {
    throw syncApiError("server_error");
  }
  return expiresAtEpochMs;
}

/** Parse the frozen geometry members shared by the plan and status shapes. */
function parseMultipartGeometry(data: Record<string, unknown>): MultipartSessionPlan {
  const { session_id: sessionId, part_count: partCount, part_size_bytes: partSizeBytes } = data;
  if (
    !isMultipartSessionIdWireShape(sessionId) ||
    typeof partCount !== "number" ||
    !Number.isInteger(partCount) ||
    partCount < 1 ||
    partCount > MAX_MULTIPART_PART_COUNT ||
    typeof partSizeBytes !== "number" ||
    !Number.isInteger(partSizeBytes) ||
    partSizeBytes !== MULTIPART_PART_SIZE_BYTES
  ) {
    throw syncApiError("server_error");
  }
  return {
    sessionId,
    partCount,
    partSizeBytes,
    expiresAtEpochMs: parseExpiresAtEpochMs(data["expires_at"]),
  };
}

function parseMultipartSessionState(value: unknown): MultipartSessionState {
  if (typeof value !== "string" || !(MULTIPART_SESSION_STATES as readonly string[]).includes(value)) {
    throw syncApiError("server_error");
  }
  return value as MultipartSessionState;
}

function parseMultipartTerminalResult(value: unknown): MultipartTerminalResult | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (!isRecord(value)) {
    throw syncApiError("server_error");
  }
  const resultKind = value["result_kind"];
  if (resultKind !== "committed" && resultKind !== "no_change") {
    throw syncApiError("server_error");
  }
  return { resultKind, ...parseTerminalReceipt(value) };
}

/** Parse one status payload: geometry, closed state, reconciled parts, terminal receipt. */
function parseMultipartSessionStatus(data: unknown): MultipartSessionStatus {
  if (!isRecord(data)) {
    throw syncApiError("server_error");
  }
  const geometry = parseMultipartGeometry(data);
  const completedPartNumbers = data["completed_part_numbers"];
  if (!Array.isArray(completedPartNumbers)) {
    throw syncApiError("server_error");
  }
  const seenPartNumbers = new Set<number>();
  for (const partNumber of completedPartNumbers) {
    if (
      typeof partNumber !== "number" ||
      !Number.isInteger(partNumber) ||
      partNumber < 1 ||
      partNumber > geometry.partCount ||
      seenPartNumbers.has(partNumber)
    ) {
      throw syncApiError("server_error");
    }
    seenPartNumbers.add(partNumber);
  }
  return {
    ...geometry,
    state: parseMultipartSessionState(data["state"]),
    completedPartNumbers: [...seenPartNumbers].sort((left, right) => left - right),
    terminalResult: parseMultipartTerminalResult(data["terminal_result"]),
  };
}

/** Parse one part-URL payload; a part-number mismatch is malformed. */
function parseMultipartPartUrl(data: unknown, requestedPartNumber: number): MultipartPartUrlAuthorization {
  if (!isRecord(data)) {
    throw syncApiError("server_error");
  }
  const { url, part_number: partNumber, offset_bytes: offsetBytes, size_bytes: sizeBytes } = data;
  if (
    typeof url !== "string" ||
    !(url.startsWith("https://") || url.startsWith("http://")) ||
    partNumber !== requestedPartNumber ||
    typeof offsetBytes !== "number" ||
    !Number.isInteger(offsetBytes) ||
    offsetBytes < 0 ||
    typeof sizeBytes !== "number" ||
    !Number.isInteger(sizeBytes) ||
    sizeBytes < 1
  ) {
    throw syncApiError("server_error");
  }
  return {
    url,
    partNumber,
    offsetBytes,
    sizeBytes,
    expiresAtEpochMs: parseExpiresAtEpochMs(data["expires_at"]),
  };
}

/** Parse one completion payload: only a committed claim may carry the frozen result. */
function parseMultipartCompletion(data: unknown): MultipartCompletion {
  if (!isRecord(data)) {
    throw syncApiError("server_error");
  }
  const state = parseMultipartSessionState(data["state"]);
  const terminalReceipt = parseMultipartTerminalResult(data["terminal_result"]);
  if (state !== "committed" && terminalReceipt !== null) {
    throw syncApiError("server_error");
  }
  if (state === "committed" && terminalReceipt === null) {
    throw syncApiError("server_error");
  }
  return { state, terminalReceipt };
}

// --- envelope parsing ---------------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const OPERATION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

interface WireEnvelope {
  readonly data: unknown;
  readonly error: { readonly code: unknown } | null;
  readonly request_id?: unknown;
}

/**
 * The envelope's opaque request id, only when it is UUID-shaped. Renamed in
 * the child six remediation so the name `envelopeRequestId` belongs to
 * exactly one plugin module — the diagnostics trail's gated token wrapper —
 * while this private parser extracts the raw wire member.
 */
function parseEnvelopeRequestId(value: unknown): string | null {
  return typeof value === "string" && UUID_PATTERN.test(value) ? value : null;
}

/**
 * Parse one canonical response envelope. A malformed body and a body with
 * neither outcome both fail closed as the retryable `server_error`; an
 * error envelope maps through the closed status/code table. The envelope's
 * UUID-shaped `request_id` is threaded out alongside the outcome (and onto
 * the mapped failure) for diagnostics correlation — no wire format change,
 * the member already exists on every canonical response.
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
 * The closed status/code mapping of the spec-12 matrix plus the multipart
 * registry block (child 7 spec 7). Unknown statuses and codes fail safe
 * onto the retryable `server_error`, never onto a terminal state: an
 * unmapped failure must not silently drop queued work.
 *
 * The multipart codes resolve BEFORE the status branches because
 * `multipart_policy_denied` answers 403 with the canonical envelope — a
 * closed code that must not fall onto the login verdict. The session-gone
 * trio (`not_found`/`expired`/`state_invalid`) maps onto the retryable
 * `operation_retry_required`: the runner clears the durable progress of
 * the dead session so the next pass re-preflights the same frozen event
 * and the server's exact replay reopens one fresh session.
 */
function mapWireFailure(status: number, code: string | null, requestId: string | null): SyncApiError {
  switch (code) {
    case "multipart_session_not_found":
    case "multipart_session_expired":
    case "multipart_session_state_invalid":
    case "multipart_completion_in_progress":
      return syncApiError("operation_retry_required", false, requestId, code);
    case "multipart_part_url_rejected":
    case "multipart_part_invalid":
    case "multipart_cleanup_failed":
    case "multipart_dependency_unavailable":
      return syncApiError("server_error", false, requestId, code);
    case "multipart_provider_state_invalid":
    case "multipart_integrity_failed":
      return syncApiError("integrity_failed", false, requestId, code);
    case "multipart_policy_denied":
      return syncApiError("policy_denied", false, requestId, code);
    default:
      break;
  }
  if (status === 401) {
    return syncApiError("access_expired", false, requestId, code);
  }
  if (status === 403) {
    // Fix round 5 (finding A): a genuine API 403 carries the canonical
    // envelope with a closed error code (parseEnvelope passes it through
    // here). A 403 whose body does NOT parse as that envelope — an
    // edge/middleware block page (an HTML challenge, or JSON without the
    // error member) in front of the API — is a transient wire failure,
    // not a login verdict: map it onto the retryable `server_error` so
    // the queue backs off and survives instead of parking the oldest
    // event under a false login_required and starving every pass behind
    // it. Diagnostic round U1: the parsed code is no longer discarded —
    // it threads onto the mapped failure for the diagnostics trail.
    return code === null
      ? syncApiError("server_error", false, requestId, code)
      : syncApiError("login_required", false, requestId, code);
  }
  if (status === 429) {
    return syncApiError("network_rate_limited", false, requestId, code);
  }
  switch (code) {
    case "small_file_size_limit_exceeded":
      return syncApiError("blocked_size", false, requestId, code);
    case "source_locator_conflict":
      // The server's typed create rejection (fix round 2026-08-23): the
      // bound path is already owned by a foreign ACTIVE locator — a
      // permanent business conflict, never a retryable condition.
      return syncApiError("blocked_conflict", false, requestId, code);
    case "small_file_content_integrity_failed":
    case "small_file_operation_identity_mismatch":
      return syncApiError("integrity_failed", false, requestId, code);
    case "small_file_operation_not_found":
    case "small_file_operation_expired":
      return syncApiError("operation_retry_required", false, requestId, code);
    case "small_file_upload_state_invalid":
      return syncApiError("operation_retry_required", true, requestId, code);
    default:
      return syncApiError("server_error", false, requestId, code);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Parse the receipt members of one terminal result; a violation is malformed. */
function parseTerminalReceipt(result: unknown): SmallFileTerminalReceipt {
  if (!isRecord(result)) {
    throw syncApiError("server_error");
  }
  const { source_id: sourceId, source_version_id: sourceVersionId, content_version: contentVersion } = result;
  if (
    typeof sourceId !== "string" ||
    !UUID_PATTERN.test(sourceId) ||
    typeof sourceVersionId !== "string" ||
    !UUID_PATTERN.test(sourceVersionId) ||
    typeof contentVersion !== "number" ||
    !Number.isInteger(contentVersion) ||
    contentVersion < 1
  ) {
    throw syncApiError("server_error");
  }
  return { sourceId, sourceVersionId, contentVersion };
}

// --- the sync client -------------------------------------------------------------------------------

export interface JournalSyncApiOptions {
  readonly transport: SyncHttpTransport;
  /** Resolved afresh per request so settings edits apply without a rebuild. */
  readonly resolveOrigin: () => string;
  /** The memory-only device access credential; null means login is required. */
  readonly getAccessToken: () => string | null;
}

export interface JournalSyncApi {
  preflightJournalEvent(input: JournalEventPreflightInput): Promise<JournalPreflightOutcome>;
  uploadSmallFileContent(input: SmallFileContentUploadInput): Promise<SmallFileTerminalReceipt>;
  /**
   * Retain one granted capture operation's candidate as conflict evidence
   * (child 8 spec 5.1): the answer is only the stored conflict identity,
   * and an exact replay of the same handle returns that identity unchanged.
   */
  uploadSmallFileConflictCandidate(
    input: SmallFileConflictCandidateUploadInput,
  ): Promise<ConflictCaptureReceipt>;
  /** Create or exactly replay the one multipart session bound to a frozen preflight operation. */
  createMultipartUploadSession(input: JournalEventPreflightInput): Promise<MultipartSessionPlan>;
  /** Observe the safe session state, reconciling the provider-observed completed parts. */
  getMultipartUploadSession(sessionId: string): Promise<MultipartSessionStatus>;
  /** Issue exactly one short-lived presigned part URL; the response is never persisted or logged. */
  issueMultipartPartUrl(input: MultipartPartUrlRequest): Promise<MultipartPartUrlAuthorization>;
  /**
   * Transmit the exact part bytes to the one presigned URL: no service
   * credential, no application body beyond the bytes, and the response
   * object is consumed here — the caller receives only the closed
   * classification of the single PUT.
   */
  putMultipartPartBytes(input: MultipartPartPutRequest): Promise<MultipartPartPutResult>;
  /** Claim completion; the answer is the persisted state or the frozen terminal result. */
  completeMultipartUploadSession(sessionId: string): Promise<MultipartCompletion>;
  /** Request the exact user/client cancellation of one session. */
  abortMultipartUploadSession(sessionId: string): Promise<MultipartSessionStatus>;
  /**
   * The last parsed envelope's opaque request id, when it carried one. The
   * driver samples this after each settled request — it holds at most one
   * active request, so the value is never ambiguous at the sample point.
   */
  readLastEnvelopeRequestId(): string | null;
}

/**
 * Build the hand-mirrored sync client over one injected transport. Every
 * call resolves the origin and credential afresh, presents the credential
 * only in the dedicated Bearer header, and maps failures onto the closed
 * kind table with no logging of any content.
 */
export function createJournalSyncApi(options: JournalSyncApiOptions): JournalSyncApi {
  const { transport, resolveOrigin, getAccessToken } = options;
  let lastEnvelopeRequestId: string | null = null;

  function requireAccessToken(): string {
    const accessToken = getAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      // Pre-contact rejection: no transport was attempted, so the callers
      // classify this as credential_failure on the trail, not wire_failure.
      throw syncApiError("login_required", false, null, null, true);
    }
    return accessToken;
  }

  async function perform(request: SyncHttpRequest): Promise<{ data: unknown }> {
    let response: SyncHttpResponse;
    try {
      response = await transport(request);
    } catch {
      throw syncApiError("network_offline");
    }
    try {
      const parsed = parseEnvelope(response.status, response.bodyText);
      lastEnvelopeRequestId = parsed.requestId;
      return parsed;
    } catch (error) {
      lastEnvelopeRequestId = error instanceof SyncApiError ? error.requestId : null;
      throw error;
    }
  }

  return {
    readLastEnvelopeRequestId: () => lastEnvelopeRequestId,

    async preflightJournalEvent(input): Promise<JournalPreflightOutcome> {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/sync/journal-events/preflight`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify(buildJournalEventWireBody(input)),
      });
      if (!isRecord(data) || typeof data["outcome"] !== "string") {
        throw syncApiError("server_error");
      }
      switch (data["outcome"]) {
        case "single_part_upload": {
          const operationId = data["operation_id"];
          if (typeof operationId !== "string" || !OPERATION_TOKEN_PATTERN.test(operationId)) {
            throw syncApiError("server_error");
          }
          return { outcome: "single_part_upload", operationId };
        }
        case "multipart_upload":
          // The multipart route opens no operation token and carries no
          // signed URL or storage identity (child 7 spec 4): the runner
          // obtains the session through the dedicated create endpoint.
          return { outcome: "multipart_upload" };
        case "committed_replay":
          return { outcome: "committed_replay", receipt: parseTerminalReceipt(data["result"]) };
        case "no_change":
          return { outcome: "no_change", receipt: parseTerminalReceipt(data["result"]) };
        case "excluded":
          return { outcome: "excluded" };
        case "conflict": {
          // At most one safe payload travels with the verdict: the capture
          // grant (an operation handle) or the stored conflict identity of a
          // same-identity replay — never both, and neither when no
          // single-part verified-object transport could be granted.
          const operationId = data["operation_id"];
          const conflictId = data["conflict_id"];
          const hasGrant =
            typeof operationId === "string" && OPERATION_TOKEN_PATTERN.test(operationId);
          const hasConflict = typeof conflictId === "string" && UUID_PATTERN.test(conflictId);
          if (!hasGrant && operationId !== undefined && operationId !== null) {
            throw syncApiError("server_error");
          }
          if (!hasConflict && conflictId !== undefined && conflictId !== null) {
            throw syncApiError("server_error");
          }
          return {
            outcome: "conflict",
            operationId: hasGrant ? operationId : null,
            conflictId: hasConflict ? conflictId : null,
          };
        }
        default:
          throw syncApiError("server_error");
      }
    },

    async uploadSmallFileContent(input): Promise<SmallFileTerminalReceipt> {
      const accessToken = requireAccessToken();
      const body = input.contentBytes.buffer.slice(
        input.contentBytes.byteOffset,
        input.contentBytes.byteOffset + input.contentBytes.byteLength,
      ) as ArrayBuffer;
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/${encodeURIComponent(input.operationId)}/content`,
        method: "PUT",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/octet-stream",
          accept: "application/json",
        },
        body,
      });
      if (!isRecord(data) || data["result_kind"] !== "committed") {
        throw syncApiError("server_error");
      }
      return parseTerminalReceipt(data);
    },

    async uploadSmallFileConflictCandidate(input): Promise<ConflictCaptureReceipt> {
      const accessToken = requireAccessToken();
      const body = input.contentBytes.buffer.slice(
        input.contentBytes.byteOffset,
        input.contentBytes.byteOffset + input.contentBytes.byteLength,
      ) as ArrayBuffer;
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/${encodeURIComponent(input.operationId)}/conflict-content`,
        method: "PUT",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/octet-stream",
          accept: "application/json",
        },
        body,
      });
      if (!isRecord(data)) {
        throw syncApiError("server_error");
      }
      const conflictId = data["conflict_id"];
      if (typeof conflictId !== "string" || !UUID_PATTERN.test(conflictId)) {
        throw syncApiError("server_error");
      }
      return { conflictId };
    },

    async createMultipartUploadSession(input): Promise<MultipartSessionPlan> {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify(buildJournalEventWireBody(input)),
      });
      if (!isRecord(data)) {
        throw syncApiError("server_error");
      }
      return parseMultipartGeometry(data);
    },

    async getMultipartUploadSession(sessionId): Promise<MultipartSessionStatus> {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions/${requireMultipartSessionIdPath(sessionId)}`,
        method: "GET",
        headers: {
          authorization: `Bearer ${accessToken}`,
          accept: "application/json",
        },
      });
      return parseMultipartSessionStatus(data);
    },

    async issueMultipartPartUrl(input): Promise<MultipartPartUrlAuthorization> {
      const accessToken = requireAccessToken();
      if (!Number.isInteger(input.partNumber) || input.partNumber < 1) {
        throw syncApiError("server_error");
      }
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions/${requireMultipartSessionIdPath(
          input.sessionId,
        )}/parts/${input.partNumber}/url`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          accept: "application/json",
        },
      });
      return parseMultipartPartUrl(data, input.partNumber);
    },

    async putMultipartPartBytes(input): Promise<MultipartPartPutResult> {
      // The presigned upload call (child 7 spec 6.2): no service credential,
      // no application request body beyond the exact part bytes and no
      // canonical envelope — the one transport call outside `perform`. The
      // response object is consumed entirely inside this scope: only the
      // closed classification below escapes, never the response, the URL
      // or any provider text.
      const body = input.contentBytes.buffer.slice(
        input.contentBytes.byteOffset,
        input.contentBytes.byteOffset + input.contentBytes.byteLength,
      ) as ArrayBuffer;
      let response: SyncHttpResponse;
      try {
        response = await transport({
          url: input.url,
          method: "PUT",
          headers: { "content-type": "application/octet-stream" },
          body,
        });
      } catch {
        throw syncApiError("network_offline");
      }
      if (response.status >= 200 && response.status < 300) {
        return "uploaded";
      }
      // A 401/403 from a part URL is never a source authorization result
      // (spec 6.2): it is the closed URL-rejection classification the
      // runner reconciles through status and one replacement URL.
      if (response.status === 401 || response.status === 403) {
        return "url_rejected";
      }
      throw syncApiError("server_error");
    },

    async completeMultipartUploadSession(sessionId): Promise<MultipartCompletion> {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions/${requireMultipartSessionIdPath(
          sessionId,
        )}/complete`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          accept: "application/json",
        },
      });
      return parseMultipartCompletion(data);
    },

    async abortMultipartUploadSession(sessionId): Promise<MultipartSessionStatus> {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions/${requireMultipartSessionIdPath(
          sessionId,
        )}/abort`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          accept: "application/json",
        },
      });
      return parseMultipartSessionStatus(data);
    },
  };

  /**
   * Validate one session handle against the closed wire grammar and render
   * it safely into a path segment; a locally corrupted handle fails closed
   * before any transport contact.
   */
  function requireMultipartSessionIdPath(sessionId: string): string {
    if (!isMultipartSessionIdWireShape(sessionId)) {
      throw syncApiError("server_error");
    }
    return encodeURIComponent(sessionId);
  }
}
