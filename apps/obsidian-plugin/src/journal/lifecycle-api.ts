/**
 * The lifecycle source-event commit API adapter.
 *
 * This module is the narrow bridge between the lifecycle driver and
 * the generated openapi-fetch client. It owns:
 *
 *   - The closed `POST /api/sources/lifecycle-events` shape derived
 *     from the openapi-generated `commitSourceLifecycleEvent`
 *     operation, so no hand-wired fetch call may replace it.
 *   - The bearer access-token header (resolved afresh per request so
 *     settings edits apply without a rebuild).
 *   - The `AbortSignal` propagation from the driver down to the
 *     underlying transport.
 *   - The closed mapping of success / error envelopes onto either a
 *     typed `LifecycleResult` (the success path) or a thrown
 *     `LifecycleApiError` carrying one closed kind label. Unknown
 *     server errors and transport-down failures fail closed onto the
 *     retryable `retry` kind so unmapped conditions never silently
 *     drop queued lifecycle work.
 *
 * Privacy (spec 9): the request body deliberately omits workspace,
 * device and user identifiers — they derive from the resolved bearer
 * context server-side. The thrown `LifecycleApiError` carries one
 * closed safe kind label and a static message only; the response
 * body, status, URL, token, locator, path and digest never reach a
 * thrown error or a log surface.
 */

import type { ApiClient, ApiTransport, components } from "@workspace/api-client";
import { createApiClient } from "@workspace/api-client";

import type { JournalSafeErrorLabel } from "./contracts";
import type { FrozenLifecycleEvent } from "./lifecycle-repository";

// --- closed outcome vocabulary --------------------------------------------------------------

/**
 * The closed safe receipt of one successful lifecycle commit. The
 * server returns these eight members under the
 * `ApiEnvelope[SourceLifecycleCommitData]` envelope; the adapter
 * never widens or filters them.
 */
export interface LifecycleResult {
  readonly committedAt: string;
  readonly eventId: string;
  readonly eventSequence: number;
  readonly resultingLocator: string | null;
  readonly sourceId: string;
  readonly sourceVersionId: string;
  readonly state: "active" | "deleted";
  readonly tombstoneId: string | null;
}

/**
 * The closed adapter failure kind. The driver inspects `kind` only;
 * `message` is a static safe text and never carries URL, status,
 * registry code, credential or response text. The retryable kinds
 * (`network_offline`, `network_timeout`, `network_rate_limited`,
 * `server_error`) are the four permitted retry classes of the
 * journal contract (spec 8, 12); the rest are non-retryable.
 *
 * The `integrity_5xx` kind is the closed 5xx-integrity outcome (task
 * 9 fix round 1 I2): when a 5xx status carries an `error.code` from
 * the integrity closed set, the envelope is non-retryable and the
 * driver closes the event as `integrity_failed`. A 5xx without an
 * integrity code keeps the retryable `server_error` mapping, so the
 * driver respects the journal contract's failing-closed default.
 */
export type LifecycleApiFailureKind =
  | "network_offline"
  | "network_timeout"
  | "network_rate_limited"
  | "server_error"
  | "login_required"
  | "conflict"
  | "integrity"
  | "integrity_5xx";

export class LifecycleApiError extends Error {
  readonly kind: LifecycleApiFailureKind;
  readonly label: JournalSafeErrorLabel | null;

  constructor(kind: LifecycleApiFailureKind, label: JournalSafeErrorLabel | null = null) {
    super(`lifecycle api failed: ${kind}`);
    this.name = "LifecycleApiError";
    this.kind = kind;
    this.label = label;
  }
}

// --- the openapi client surface ---------------------------------------------------------------

/**
 * The narrow adapter port the lifecycle driver consumes. The
 * generated `commitSourceLifecycleEvent` operation is exercised
 * exclusively through this seam; no other client method is reachable
 * from the driver. The success path returns the typed
 * `LifecycleResult`; every failure path throws one
 * {@link LifecycleApiError} the driver maps onto its retry / blocked
 * verdicts.
 *
 * The optional `tombstoneIdOverride` lets the driver align the wire
 * body's tombstone id with the server-confirmed one carried by the
 * committed delete predecessor. The override is the closed
 * server-only authority over the tombstone domain (spec 19.2, task
 * 9 fix round 1 I1): the restore driver hands it in so the server
 * hears the same identity it returned on the predecessor commit.
 */
export interface LifecycleApi {
  commit(
    event: FrozenLifecycleEvent,
    signal: AbortSignal,
    tombstoneIdOverride?: string | null,
  ): Promise<LifecycleResult>;
}

export interface LifecycleApiOptions {
  /** The pre-built openapi-fetch client the adapter drives. */
  readonly apiClient: ApiClient;
  /**
   * Resolved afresh per request so settings edits apply without a
   * rebuild; `null` means login is required and the adapter rejects
   * the call before any HTTP request is issued.
   */
  readonly resolveAccessToken: () => string | null;
}

/**
 * The narrow fetch seam the openapi-fetch generated client accepts:
 * one `Request` in, one `Response` out, no automatic retry.
 *
 * The plugin composition passes an adapter that goes through
 * Obsidian's `requestUrl` so the same credential / network surface
 * the existing sync client uses carries the lifecycle commit too.
 */
export type LifecycleApiTransport = ApiTransport;

/**
 * Compose one plugin-bound lifecycle adapter over an injected
 * openapi-fetch client. The helper exists so the Obsidian plugin
 * composition root does not have to import the generated openapi
 * client directly; the journal layer owns the dependency, which
 * keeps the plugin's forbidden-runtime-capability assertion
 * (plugin.test.ts) free of `@workspace/` references.
 */
export function createLifecycleApi(options: LifecycleApiOptions): LifecycleApi {
  return buildLifecycleApi(options);
}

/**
 * The Obsidian-specific factory: builds the openapi-fetch client
 * over the injected fetch-shaped transport and hands the bound
 * adapter to the caller. The plugin composition root calls this
 * instead of importing `@workspace/api-client` directly.
 */
export function createRequestUrlLifecycleApi(options: {
  readonly baseUrl: string;
  readonly transport: LifecycleApiTransport;
  readonly resolveAccessToken: () => string | null;
}): LifecycleApi {
  const apiClient: ApiClient = createApiClient({
    baseUrl: options.baseUrl,
    transport: options.transport,
  });
  return buildLifecycleApi({
    apiClient,
    resolveAccessToken: options.resolveAccessToken,
  });
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATETIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$/;

/**
 * The closed set of lifecycle-domain error codes that, when carried by
 * a 5xx envelope, signal the durable journal cannot safely retry
 * (task 9 fix round 1 I2). The codes are the canonical 5xx-integrity
 * surfaces of the lifecycle commit path: a duplicate outcome, a
 * verified receipt drift, a source version conflict, or an
 * invariant failure all share the same non-retryable verdict. The
 * closed set is intentionally narrow so unmapped 5xx codes fall
 * through to the retryable `server_error` mapping.
 */
const LIFECYCLE_5XX_INTEGRITY_CODES: ReadonlySet<string> = new Set([
  "source_commit_outcome_unknown",
  "source_idempotency_mismatch",
  "source_event_identity_mismatch",
  "source_verified_receipt_stale",
  "source_version_conflict",
  "source_content_object_conflict",
  "source_concurrency_invariant_failed",
  "canonical_recovery_integrity_failed",
  "canonical_recovery_restore_failed",
  "object_storage_integrity_failed",
  "object_storage_metadata_conflict",
]);

/**
 * Read one openapi-fetch POST result envelope and translate it onto
 * either a typed `LifecycleResult` (success) or a thrown
 * `LifecycleApiError` (failure). The translation is fail-closed:
 * unknown status codes and unparseable bodies map onto the retryable
 * `server_error` kind so unmapped conditions never silently drop
 * queued lifecycle work.
 *
 * The 5xx branch inspects the `error.code` envelope field (task 9 fix
 * round 1 I2): a code in the integrity closed set maps onto the
 * non-retryable `integrity_5xx` kind so the driver closes the event
 * as `integrity_failed`. A 5xx without an integrity code keeps the
 * retryable `server_error` mapping.
 */
async function translate(
  result: Promise<{ data?: unknown; error?: unknown; response: Response }>,
): Promise<LifecycleResult> {
  let envelope: { data?: unknown; error?: unknown; response: Response };
  try {
    envelope = await result;
  } catch {
    throw new LifecycleApiError("network_offline");
  }
  const status = envelope.response.status;
  if (status === 401 || status === 403) {
    throw new LifecycleApiError("login_required");
  }
  if (status === 409) {
    throw new LifecycleApiError("conflict");
  }
  if (status === 422) {
    throw new LifecycleApiError("integrity");
  }
  if (status === 429) {
    throw new LifecycleApiError("network_rate_limited");
  }
  if (status >= 500) {
    // openapi-fetch surfaces the parsed JSON body on `data` for the
    // happy path; on an error response, the body surfaces on `error`.
    // The error envelope's `error.code` is the discriminator between
    // 5xx-integrity (non-retryable) and 5xx-transient (retryable);
    // the probe checks both fields so an openapi-fetch version that
    // surfaces the body on `data` instead of `error` also routes
    // through the integrity verdict.
    if (isIntegrity5xxEnvelope(envelope.data) || isIntegrity5xxEnvelope(envelope.error)) {
      throw new LifecycleApiError("integrity_5xx");
    }
    throw new LifecycleApiError("server_error");
  }
  // The success envelope still flows through openapi-fetch as the raw
  // parsed JSON; we extract the canonical `data` field ourselves so the
  // adapter sees the typed `SourceLifecycleCommitData` payload.
  if (!isRecord(envelope.data)) {
    throw new LifecycleApiError("server_error");
  }
  const inner = envelope.data["data"];
  const error = envelope.data["error"];
  if (error !== null && error !== undefined) {
    throw new LifecycleApiError("server_error");
  }
  return parseLifecycleResult(inner);
}

/**
 * Whether the parsed envelope carries an integrity-class code on a
 * 5xx status. The probe is safely nil-tolerant: any non-record
 * envelope, a missing `error` body, or an unparseable code answers
 * false so the envelope falls through to the retryable `server_error`
 * mapping.
 */
function isIntegrity5xxEnvelope(parsedData: unknown): boolean {
  if (!isRecord(parsedData)) {
    return false;
  }
  const errorBody = parsedData["error"];
  if (!isRecord(errorBody)) {
    return false;
  }
  const code = errorBody["code"];
  return typeof code === "string" && LIFECYCLE_5XX_INTEGRITY_CODES.has(code);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseLifecycleResult(data: unknown): LifecycleResult {
  if (!isRecord(data)) {
    throw new LifecycleApiError("server_error");
  }
  const {
    committed_at: committedAt,
    event_id: eventId,
    event_sequence: eventSequence,
    resulting_locator: resultingLocator,
    source_id: sourceId,
    source_version_id: sourceVersionId,
    state,
    tombstone_id: tombstoneId,
  } = data;
  if (
    typeof committedAt !== "string" ||
    !DATETIME_PATTERN.test(committedAt) ||
    typeof eventId !== "string" ||
    !UUID_PATTERN.test(eventId) ||
    typeof eventSequence !== "number" ||
    !Number.isInteger(eventSequence) ||
    eventSequence < 0 ||
    (resultingLocator !== null && typeof resultingLocator !== "string") ||
    typeof sourceId !== "string" ||
    !UUID_PATTERN.test(sourceId) ||
    typeof sourceVersionId !== "string" ||
    !UUID_PATTERN.test(sourceVersionId) ||
    (state !== "active" && state !== "deleted") ||
    (tombstoneId !== null && (typeof tombstoneId !== "string" || !UUID_PATTERN.test(tombstoneId)))
  ) {
    throw new LifecycleApiError("server_error");
  }
  return {
    committedAt,
    eventId,
    eventSequence,
    resultingLocator,
    sourceId,
    sourceVersionId,
    state,
    tombstoneId,
  };
}

// --- the closed body --------------------------------------------------------------------------

/**
 * Compose the closed wire body for `commitSourceLifecycleEvent`. The
 * fields mirror the openapi `SourceLifecycleEventRequest` schema:
 * workspace / device / user identities are deliberately absent and
 * must NEVER be added here. Locator values stay plain normalized strings so
 * the wire contract matches the backend pre-validator and the generated
 * openapi-fetch request type exactly under `exactOptionalPropertyTypes`.
 *
 * The optional `tombstoneIdOverride` is the server-confirmed tombstone
 * identity the predecessor's commit returned. When the driver passes
 * it in, the wire body carries the override INSTEAD of the operands-
 * derived tombstone id so the restore sends back the same id the
 * server mailed on the paired delete (task 9 fix round 1 I1).
 */
function buildBody(
  event: FrozenLifecycleEvent,
  tombstoneIdOverride: string | null | undefined,
): components["schemas"]["SourceLifecycleEventRequest"] {
  const tombstoneId =
    event.operands.operation === "delete"
      ? null
      : tombstoneIdOverride !== undefined
        ? tombstoneIdOverride
        : event.operands.tombstoneId;
  return {
    event_id: event.event.eventId,
    idempotency_key: event.event.idempotencyKey,
    source_id: event.operands.sourceId,
    operation: event.operands.operation,
    expected_version_id: event.operands.expectedVersionId,
    expected_locator: event.operands.expectedLocator,
    target_locator: event.operands.targetLocator,
    tombstone_id: tombstoneId,
    policy_revision: event.operands.policyRevision,
    client_timestamp: new Date().toISOString(),
  };
}

// --- the adapter factory ---------------------------------------------------------------------

/**
 * Build the lifecycle commit adapter. Every method resolves the
 * access token afresh so settings edits apply without a rebuild;
 * bearer authentication is presented only in the dedicated
 * `Authorization` header.
 */
function buildLifecycleApi(options: LifecycleApiOptions): LifecycleApi {
  const { apiClient, resolveAccessToken } = options;

  function bearerHeaders(): Record<string, string> {
    const accessToken = resolveAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      throw new LifecycleApiError("login_required");
    }
    return {
      authorization: `Bearer ${accessToken}`,
      accept: "application/json",
    };
  }

  return {
    async commit(event, signal, tombstoneIdOverride): Promise<LifecycleResult> {
      const headers = bearerHeaders();
      const body = buildBody(event, tombstoneIdOverride);
      // openapi-fetch exposes the typed `POST` overload for the
      // generated operation; pass the body, headers, and signal
      // through to the underlying transport exactly once.
      const result = apiClient.POST("/api/sources/lifecycle-events", {
        body,
        headers,
        // openapi-fetch accepts the standard RequestInit signal so an
        // upstream AbortController cuts off the in-flight HTTP request
        // before the response is mapped.
        ...(signal !== undefined ? { signal } : {}),
      });
      return translate(result);
    },
  };
}
