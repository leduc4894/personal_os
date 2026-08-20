/**
 * The hand-mirrored small-file sync API client (spec 10, 12).
 *
 * The generated workspace client `@workspace/api-client` is intentionally
 * NOT bundled into Obsidian, so this module mirrors exactly the two sync
 * wire shapes the plugin consumes — `POST /api/sync/journal-events/preflight`
 * and `PUT /api/uploads/{operation_id}/content` — behind the existing
 * `obsidian_sync` device Bearer credential and the canonical response
 * envelope. It adds no automatic retry and no interpretation of provider
 * detail: the plugin never receives an R2 key, presigned URL or canonical
 * object-store identity, and every server outcome maps onto a closed
 * plugin-side kind.
 *
 * Privacy (spec 2, 9): failures carry one closed {@link SyncApiFailureKind}
 * and a static message only — status numbers, registry codes, response
 * bodies, URLs, tokens, paths and digests never reach a thrown error or a
 * diagnostic surface. The content stream sends exactly the re-fingerprinted
 * Vault bytes and nothing else.
 */

import type { FrozenFingerprint, JournalOperation } from "./contracts";

// --- the raw sync transport port -------------------------------------------------------------

/** One raw sync HTTP request: a JSON text body or an exact byte buffer. */
export interface SyncHttpRequest {
  readonly url: string;
  readonly method: "POST" | "PUT";
  readonly headers: Readonly<Record<string, string>>;
  readonly body: string | ArrayBuffer;
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
 * two terminal integrity/size rejections. `operation_retry_required` covers
 * the server's closed upload-operation failures. Its safe resume flag
 * distinguishes a claimed receive from an unknown or expired token without
 * widening the cross-language failure vocabulary.
 */
export const SYNC_API_FAILURE_KINDS = [
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "access_expired",
  "login_required",
  "blocked_size",
  "integrity_failed",
  "operation_retry_required",
] as const;

export type SyncApiFailureKind = (typeof SYNC_API_FAILURE_KINDS)[number];

/**
 * One mapped sync failure: a closed kind and a static message. The message
 * never carries the URL, status, registry code, credential or any response
 * text, so a thrown error is redacted by construction.
 */
export class SyncApiError extends Error {
  readonly kind: SyncApiFailureKind;
  readonly canResumeClaimedOperation: boolean;

  constructor(kind: SyncApiFailureKind, canResumeClaimedOperation = false) {
    super(`sync api failed: ${kind}`);
    this.name = "SyncApiError";
    this.kind = kind;
    this.canResumeClaimedOperation = canResumeClaimedOperation;
  }
}

function syncApiError(
  kind: SyncApiFailureKind,
  canResumeClaimedOperation = false,
): SyncApiError {
  return new SyncApiError(kind, canResumeClaimedOperation);
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
 * Exactly one typed preflight outcome (spec 10.1 table): the only upload
 * continuation is `single_part_upload` with its opaque operation handle;
 * every other outcome finishes the event without uploading.
 */
export type JournalPreflightOutcome =
  | { readonly outcome: "single_part_upload"; readonly operationId: string }
  | { readonly outcome: "committed_replay"; readonly receipt: SmallFileTerminalReceipt }
  | { readonly outcome: "no_change"; readonly receipt: SmallFileTerminalReceipt }
  | { readonly outcome: "excluded" }
  | { readonly outcome: "conflict" };

/** The content-stream request: the preflight-bound handle and the exact bytes. */
export interface SmallFileContentUploadInput {
  readonly operationId: string;
  readonly contentBytes: Uint8Array;
}

// --- envelope parsing ---------------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const OPERATION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

interface WireEnvelope {
  readonly data: unknown;
  readonly error: { readonly code: unknown } | null;
}

/**
 * Parse one canonical response envelope. A malformed body and a body with
 * neither outcome both fail closed as the retryable `server_error`; an
 * error envelope maps through the closed status/code table.
 */
function parseEnvelope(status: number, bodyText: string): { data: unknown } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    throw mapWireFailure(status, null);
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw mapWireFailure(status, null);
  }
  const envelope = parsed as Partial<WireEnvelope>;
  if (envelope.error !== null && envelope.error !== undefined) {
    const code = typeof envelope.error.code === "string" ? envelope.error.code : null;
    throw mapWireFailure(status, code);
  }
  if (envelope.data === null || envelope.data === undefined) {
    throw mapWireFailure(status, null);
  }
  return { data: envelope.data };
}

/**
 * The closed status/code mapping of the spec-12 matrix. Unknown statuses
 * and codes fail safe onto the retryable `server_error`, never onto a
 * terminal state: an unmapped failure must not silently drop queued work.
 */
function mapWireFailure(status: number, code: string | null): SyncApiError {
  if (status === 401) {
    return syncApiError("access_expired");
  }
  if (status === 403) {
    return syncApiError("login_required");
  }
  if (status === 429) {
    return syncApiError("network_rate_limited");
  }
  switch (code) {
    case "small_file_size_limit_exceeded":
      return syncApiError("blocked_size");
    case "small_file_content_integrity_failed":
    case "small_file_operation_identity_mismatch":
      return syncApiError("integrity_failed");
    case "small_file_operation_not_found":
    case "small_file_operation_expired":
      return syncApiError("operation_retry_required");
    case "small_file_upload_state_invalid":
      return syncApiError("operation_retry_required", true);
    default:
      return syncApiError("server_error");
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
}

/**
 * Build the hand-mirrored sync client over one injected transport. Every
 * call resolves the origin and credential afresh, presents the credential
 * only in the dedicated Bearer header, and maps failures onto the closed
 * kind table with no logging of any content.
 */
export function createJournalSyncApi(options: JournalSyncApiOptions): JournalSyncApi {
  const { transport, resolveOrigin, getAccessToken } = options;

  function requireAccessToken(): string {
    const accessToken = getAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      throw syncApiError("login_required");
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
    return parseEnvelope(response.status, response.bodyText);
  }

  return {
    async preflightJournalEvent(input): Promise<JournalPreflightOutcome> {
      const accessToken = requireAccessToken();
      const wireBody: Record<string, unknown> = {
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
      const { data } = await perform({
        url: `${resolveOrigin()}/api/sync/journal-events/preflight`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify(wireBody),
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
        case "committed_replay":
          return { outcome: "committed_replay", receipt: parseTerminalReceipt(data["result"]) };
        case "no_change":
          return { outcome: "no_change", receipt: parseTerminalReceipt(data["result"]) };
        case "excluded":
          return { outcome: "excluded" };
        case "conflict":
          return { outcome: "conflict" };
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
  };
}
