/**
 * The hand-mirrored Conflict Inbox wire client (Child 8 spec 6, Task 7).
 *
 * The generated workspace client `@workspace/api-client` is intentionally
 * NOT bundled into Obsidian, so this module mirrors exactly the four
 * Task 6 conflict operations — `GET /api/sync/conflicts`,
 * `GET /api/sync/conflicts/{conflict_id}`,
 * `GET /api/sync/conflicts/{conflict_id}/evidence/{role}` and
 * `POST /api/sync/conflicts/{conflict_id}/resolve` — behind the existing
 * `obsidian_sync` device Bearer credential over the binary-capable
 * `DeviceSyncHttpTransport` seam (the JSON calls read the canonical
 * envelope's text, the evidence download reads the exact bytes and
 * lower-cased headers). It adds no automatic retry and no interpretation
 * of provider detail: every server outcome maps onto one closed
 * {@link ConflictApiFailureKind} with a static message, and the retryable
 * verdicts (`commit_outcome_unknown`, `dependency_unavailable`, the
 * network family and unknown `server_error`) carry `canRetry` so the
 * caller never silently drops owed work — a replayed resolution event
 * identity is the server's own exact-replay contract.
 *
 * The evidence download is the plugin's verified read: a success status
 * must carry exact body bytes, an exact canonical `type/subtype`
 * content type and a content length that equals the byte count, else the
 * closed `evidence_download_invalid` verdict fails the whole read. The
 * evidence wire surface carries no digest header, so the length
 * verification is the boundary; decoding the bytes is Task 8's concern.
 *
 * NOTE on `save_merged` (Task 6 carry-over): the resolve body references
 * an already-uploaded verified candidate object — this client carries
 * the reference verbatim but provides NO upload operation, because the
 * server currently exposes no device route that produces a verified
 * candidate object for an open conflict's resolution (the small-file
 * conflict-capture receive is domain-level only). See the Task 7 report.
 *
 * Privacy (spec 9): failures carry one closed kind and a static message
 * only — status numbers, registry codes, response bodies, URLs, tokens,
 * paths and digests never reach a thrown error. The UUID-shaped
 * envelope `requestId` and the envelope's closed error-code string ride
 * a failure only for the diagnostics trail's gated closed-token
 * boundary, never the message.
 */

import type { DeviceSyncHttpTransport, DeviceSyncHttpResponse } from "../device-sync/api";
import type { SyncHttpRequest } from "../journal/sync-api";
import type {
  ConflictDetail,
  ConflictPage,
  ConflictResolution,
  ConflictResolveInput,
  ConflictEvidenceRole,
  VerifiedConflictEvidence,
} from "./contracts";
import {
  decodeConflictDetail,
  decodeConflictPage,
  decodeConflictResolution,
  isConflictEvidenceRole,
  validateConflictResolveInput,
} from "./contracts";

// --- the closed failure surface -----------------------------------------------------------------------

/**
 * The closed failure vocabulary of the conflict wire client. The
 * terminal verdicts name the server's closed `source_conflict_*`
 * registry block one-to-one; the two retryable conflict verdicts are the
 * dependency outage and the ambiguous commit outcome whose resolution
 * event identity replays exactly. `evidence_download_invalid` is the
 * client's own verified-read rejection.
 */
export const CONFLICT_API_FAILURE_KINDS = [
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "access_expired",
  "login_required",
  "policy_denied",
  "input_invalid",
  "conflict_not_found",
  "conflict_state_invalid",
  "conflict_idempotency_mismatch",
  "evidence_unavailable",
  "evidence_integrity_failed",
  "evidence_download_invalid",
  "dependency_unavailable",
  "commit_outcome_unknown",
] as const;

export type ConflictApiFailureKind = (typeof CONFLICT_API_FAILURE_KINDS)[number];

/**
 * One mapped conflict wire failure: the closed kind, its retryability
 * and — only when the failing body parsed as the canonical envelope —
 * the envelope's UUID-shaped request id and closed error-code string for
 * the diagnostics trail. `isCredentialAbsent` marks the one pre-contact
 * rejection: no access credential meant no transport was attempted. The
 * message is the static closed kind only.
 */
export class ConflictApiError extends Error {
  readonly kind: ConflictApiFailureKind;
  readonly canRetry: boolean;
  readonly requestId: string | null;
  readonly wireErrorCode: string | null;
  readonly isCredentialAbsent: boolean;

  constructor(
    kind: ConflictApiFailureKind,
    canRetry: boolean,
    requestId: string | null = null,
    wireErrorCode: string | null = null,
    isCredentialAbsent = false,
  ) {
    super(`conflict api failed: ${kind}`);
    this.name = "ConflictApiError";
    this.kind = kind;
    this.canRetry = canRetry;
    this.requestId = requestId;
    this.wireErrorCode = wireErrorCode;
    this.isCredentialAbsent = isCredentialAbsent;
  }
}

function conflictApiError(
  kind: ConflictApiFailureKind,
  canRetry: boolean,
  requestId: string | null = null,
  wireErrorCode: string | null = null,
  isCredentialAbsent = false,
): ConflictApiError {
  return new ConflictApiError(kind, canRetry, requestId, wireErrorCode, isCredentialAbsent);
}

// --- request inputs ------------------------------------------------------------------------------------

/** One bounded list query: the page size (1..200) and the stable cursor. */
export interface ConflictListInput {
  readonly limit?: number | undefined;
  readonly exclusiveStartConflictId?: string | null | undefined;
}

/** One verified evidence download request: the conflict and the closed role. */
export interface ConflictEvidenceInput {
  readonly conflictId: string;
  readonly role: ConflictEvidenceRole;
}

// --- envelope parsing --------------------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CANONICAL_MEDIA_TYPE_PATTERN = /^[a-z0-9]+\/[a-z0-9.-]+$/;
const NON_NEGATIVE_INTEGER_TEXT_PATTERN = /^(0|[1-9][0-9]*)$/;

interface WireEnvelope {
  readonly data: unknown;
  readonly error: { readonly code: unknown } | null;
  readonly request_id?: unknown;
}

/** The envelope's opaque request id, only when it is UUID-shaped. */
function parseEnvelopeRequestId(value: unknown): string | null {
  return typeof value === "string" && UUID_PATTERN.test(value) ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Parse one canonical response envelope. A malformed body and a body
 * with neither outcome both fail closed onto the retryable
 * `server_error`; an error envelope maps through the closed kind table.
 */
function parseEnvelope(status: number, bodyText: string): { data: unknown; requestId: string | null } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    throw mapWireFailure(status, null, null);
  }
  if (!isRecord(parsed)) {
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
 * The closed status/code mapping of the conflict registry: the eight
 * `source_conflict_*` codes map one-to-one onto their kinds (the two
 * retryable 503s carry `canRetry`), the shared credential/policy codes
 * keep their established verdicts, and every unknown status or code
 * fails safe onto the retryable `server_error` — an unmapped failure
 * must not silently drop owed work. The conflict codes resolve BEFORE
 * the status branches so a 403 policy denial never collapses onto the
 * login verdict.
 */
function mapWireFailure(
  status: number,
  code: string | null,
  requestId: string | null,
): ConflictApiError {
  switch (code) {
    case "source_conflict_not_found":
      return conflictApiError("conflict_not_found", false, requestId, code);
    case "source_conflict_state_invalid":
      return conflictApiError("conflict_state_invalid", false, requestId, code);
    case "source_conflict_idempotency_mismatch":
      return conflictApiError("conflict_idempotency_mismatch", false, requestId, code);
    case "source_conflict_evidence_unavailable":
      return conflictApiError("evidence_unavailable", false, requestId, code);
    case "source_conflict_evidence_integrity_failed":
      return conflictApiError("evidence_integrity_failed", false, requestId, code);
    case "source_conflict_input_invalid":
      return conflictApiError("input_invalid", false, requestId, code);
    case "source_conflict_dependency_unavailable":
      // Retryable store/engine outage; no conflict state changed.
      return conflictApiError("dependency_unavailable", true, requestId, code);
    case "source_conflict_commit_outcome_unknown":
      // Ambiguous commit outcome: the resolution event identity's exact
      // replay is the safe answer, so this verdict stays retryable.
      return conflictApiError("commit_outcome_unknown", true, requestId, code);
    case "exclusion_policy_denied":
      return conflictApiError("policy_denied", false, requestId, code);
    default:
      break;
  }
  if (status === 401) {
    return conflictApiError("access_expired", false, requestId, code);
  }
  if (status === 403) {
    // A genuine API 403 carries the canonical envelope with a closed
    // code; a mapped code answered above, so a residual coded 403 is the
    // scope/login verdict and an uncoded one (an edge block page in
    // front of the API) is a transient wire failure, never a login
    // verdict.
    return code === null
      ? conflictApiError("server_error", true, requestId, code)
      : conflictApiError("login_required", false, requestId, code);
  }
  if (status === 429) {
    return conflictApiError("network_rate_limited", true, requestId, code);
  }
  return conflictApiError("server_error", true, requestId, code);
}

// --- the client ------------------------------------------------------------------------------------------

export interface ConflictApiOptions {
  readonly transport: DeviceSyncHttpTransport;
  /** Resolved afresh per request so settings edits apply without a rebuild. */
  readonly resolveOrigin: () => string;
  /** The memory-only device access credential; null means login is required. */
  readonly getAccessToken: () => string | null;
}

export interface ConflictApi {
  /** Page the workspace's open conflicts in stable order. */
  listConflicts(input?: ConflictListInput): Promise<ConflictPage>;
  /** Read one conflict's safe metadata and its offered choices. */
  getConflict(conflictId: string): Promise<ConflictDetail>;
  /**
   * Verified read of one immutable evidence role's exact bytes: the
   * declared canonical media type and verified length ride along; the
   * caller (Task 8) decodes only supported text/Markdown.
   */
  downloadConflictEvidence(input: ConflictEvidenceInput): Promise<VerifiedConflictEvidence>;
  /**
   * Post one explicit resolution. The request is validated against the
   * server's field grammar BEFORE any transport contact; a
   * `save_merged` request carries only the verified object reference it
   * was given — this client offers no candidate upload (see the module
   * note on the Task 6 carry-over).
   */
  resolveConflict(input: ConflictResolveInput): Promise<ConflictResolution>;
}

/**
 * Build the hand-mirrored conflict client over one injected transport.
 * Every call resolves the origin and credential afresh, presents the
 * credential only in the dedicated Bearer header, and maps failures onto
 * the closed kind table with no logging of any content.
 */
export function createConflictApi(options: ConflictApiOptions): ConflictApi {
  const { transport, resolveOrigin, getAccessToken } = options;

  function requireAccessToken(): string {
    const accessToken = getAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      // Pre-contact rejection: no transport was attempted.
      throw conflictApiError("login_required", false, null, null, true);
    }
    return accessToken;
  }

  function requireUuidPath(value: string): string {
    if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
      throw conflictApiError("input_invalid", false, null, null);
    }
    return encodeURIComponent(value);
  }

  async function send(request: SyncHttpRequest): Promise<DeviceSyncHttpResponse> {
    try {
      return await transport(request);
    } catch (error) {
      const name = error instanceof Error ? error.name : "";
      if (name === "TimeoutError" || name === "AbortError") {
        throw conflictApiError("network_timeout", true);
      }
      throw conflictApiError("network_offline", true);
    }
  }

  /** Perform one JSON request and parse its canonical envelope. */
  async function performJson(
    accessToken: string,
    request: SyncHttpRequest,
  ): Promise<{ data: unknown }> {
    const response = await send({
      ...request,
      headers: { ...request.headers, authorization: `Bearer ${accessToken}` },
    });
    return parseEnvelope(response.status, response.bodyText);
  }

  return {
    async listConflicts(input = {}): Promise<ConflictPage> {
      const accessToken = requireAccessToken();
      const query = new URLSearchParams();
      if (input.limit !== undefined) {
        if (
          typeof input.limit !== "number" ||
          !Number.isInteger(input.limit) ||
          input.limit < 1 ||
          input.limit > 200
        ) {
          throw conflictApiError("input_invalid", false);
        }
        query.set("limit", String(input.limit));
      }
      if (input.exclusiveStartConflictId !== undefined && input.exclusiveStartConflictId !== null) {
        if (!UUID_PATTERN.test(input.exclusiveStartConflictId)) {
          throw conflictApiError("input_invalid", false);
        }
        query.set("exclusive_start_conflict_id", input.exclusiveStartConflictId);
      }
      const suffix = query.size > 0 ? `?${query.toString()}` : "";
      const { data } = await performJson(accessToken, {
        url: `${resolveOrigin()}/api/sync/conflicts${suffix}`,
        method: "GET",
        headers: { accept: "application/json" },
      });
      return decodeConflictPage(data);
    },

    async getConflict(conflictId): Promise<ConflictDetail> {
      const accessToken = requireAccessToken();
      const conflictPath = requireUuidPath(conflictId);
      const { data } = await performJson(accessToken, {
        url: `${resolveOrigin()}/api/sync/conflicts/${conflictPath}`,
        method: "GET",
        headers: { accept: "application/json" },
      });
      return decodeConflictDetail(data);
    },

    async downloadConflictEvidence(input): Promise<VerifiedConflictEvidence> {
      const accessToken = requireAccessToken();
      if (!isConflictEvidenceRole(input.role)) {
        throw conflictApiError("input_invalid", false);
      }
      const conflictPath = requireUuidPath(input.conflictId);
      const response = await send({
        url: `${resolveOrigin()}/api/sync/conflicts/${conflictPath}/evidence/${input.role}`,
        method: "GET",
        headers: { authorization: `Bearer ${accessToken}`, accept: "application/octet-stream" },
      });
      if (response.status !== 200) {
        // Pre-stream failures render the canonical JSON envelope; a body
        // that parses as a success envelope at a failure status is a
        // malformed outcome and fails closed.
        parseEnvelope(response.status, response.bodyText);
        throw conflictApiError("server_error", true);
      }
      const headers = response.headers;
      const mediaType = headers["content-type"];
      const declaredSizeText = headers["content-length"];
      const bodyBytes = response.bodyBytes;
      const declaredSize =
        typeof declaredSizeText === "string" && NON_NEGATIVE_INTEGER_TEXT_PATTERN.test(declaredSizeText)
          ? Number.parseInt(declaredSizeText, 10)
          : null;
      if (
        bodyBytes === null ||
        declaredSize === null ||
        declaredSize > Number.MAX_SAFE_INTEGER ||
        typeof mediaType !== "string" ||
        !CANONICAL_MEDIA_TYPE_PATTERN.test(mediaType)
      ) {
        throw conflictApiError("evidence_download_invalid", false);
      }
      const bytes = new Uint8Array(bodyBytes);
      // The verified read: the wire surface carries no digest header, so
      // the exact declared length is the boundary — a partial or
      // truncated body never reaches the caller.
      if (bytes.byteLength !== declaredSize) {
        throw conflictApiError("evidence_download_invalid", false);
      }
      return { bytes, mediaType, sizeBytes: declaredSize };
    },

    async resolveConflict(input): Promise<ConflictResolution> {
      const accessToken = requireAccessToken();
      try {
        validateConflictResolveInput(input);
      } catch {
        throw conflictApiError("input_invalid", false);
      }
      const reviewedRemoteVersionId = input.reviewedRemoteVersionId ?? null;
      const verifiedCandidateObjectId = input.verifiedCandidateObjectId ?? null;
      const wireBody = JSON.stringify({
        resolution_event_id: input.resolutionEventId,
        idempotency_key: input.idempotencyKey,
        resolution_kind: input.resolutionKind,
        ...(reviewedRemoteVersionId === null
          ? {}
          : { reviewed_remote_version_id: reviewedRemoteVersionId }),
        ...(verifiedCandidateObjectId === null
          ? {}
          : { verified_candidate_object_id: verifiedCandidateObjectId }),
      });
      const conflictPath = requireUuidPath(input.conflictId);
      const { data } = await performJson(accessToken, {
        url: `${resolveOrigin()}/api/sync/conflicts/${conflictPath}/resolve`,
        method: "POST",
        headers: {
          accept: "application/json",
          "content-type": "application/json",
        },
        body: wireBody,
      });
      return decodeConflictResolution(data);
    },
  };
}
