/**
 * Tests of the hand-mirrored small-file sync wire shapes (spec 10, 12).
 *
 * The generated workspace client is deliberately not bundled into Obsidian,
 * so `sync-api.ts` mirrors only the two sync operations. These tests pin:
 * exact bearer/header/body construction (JSON preflight with the strict
 * snake_case body of spec 10.1, raw single-part content stream of spec
 * 10.2), the closed typed-outcome parsing of the canonical envelope, the
 * closed failure mapping of the spec-12 retry matrix, the absence of any
 * automatic retry, and the raw `ArrayBuffer` request path of the
 * `requestUrl` adapter.
 */

import { describe, expect, it } from "vitest";

import { createRequestUrlSyncTransport } from "../api/request-url-transport";
import type { SyncHttpRequest, SyncHttpTransport } from "./sync-api";
import {
  createJournalSyncApi,
  SYNC_API_FAILURE_KINDS,
} from "./sync-api";
import type { JournalPreflightOutcome } from "./sync-api";

// --- shared fixtures ---------------------------------------------------------------------------

const ORIGIN = "https://sync.example.org";
const ACCESS_TOKEN = "at1.test-access-credential";

const PREFLIGHT_INPUT = {
  eventId: "11111111-1111-4111-8111-111111111111",
  idempotencyKey: "22222222-2222-4222-8222-222222222222",
  operation: "create" as const,
  localFileId: "33333333-3333-4333-8333-333333333333",
  sourceId: null,
  baseVersionId: null,
  normalizedLocator: "notes/one.md",
  fingerprint: {
    sha256: `${"a".repeat(64)}`,
    sizeBytes: 12,
    mediaType: "text/plain",
  },
  policyRevisionNumber: 4,
};

const SOURCE_ID = "44444444-4444-4444-8444-444444444444";
const SOURCE_VERSION_ID = "55555555-5555-4555-8555-555555555555";
const OPERATION_ID = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-AbCd";

/** Build one canonical success envelope body around the given data. */
function successBody(data: unknown): string {
  return JSON.stringify({
    data,
    error: null,
    request_id: "66666666-6666-4666-8666-666666666666",
    warnings: [],
  });
}

/** Build one canonical error envelope body around the given code. */
function errorBody(code: string): string {
  return JSON.stringify({
    data: null,
    error: { code, message: "registered safe message", details: {}, retryable: false },
    request_id: "66666666-6666-4666-8666-666666666666",
    warnings: [],
  });
}

const RECEIPT = {
  result_kind: "committed",
  source_id: SOURCE_ID,
  source_version_id: SOURCE_VERSION_ID,
  content_version: 3,
  committed_at: "2026-08-18T00:00:00Z",
};

/** A recording transport: every request lands in the journal of calls. */
function createRecordingTransport(
  respond: (request: SyncHttpRequest, index: number) => Promise<{ status: number; bodyText: string }>,
): SyncHttpTransport & { readonly requests: SyncHttpRequest[] } {
  const requests: SyncHttpRequest[] = [];
  return Object.assign(
    async (request: SyncHttpRequest) => {
      const index = requests.length;
      requests.push(request);
      return respond(request, index);
    },
    { requests },
  );
}

function createApi(transport: SyncHttpTransport) {
  return createJournalSyncApi({
    transport,
    resolveOrigin: () => ORIGIN,
    getAccessToken: () => ACCESS_TOKEN,
  });
}

// --- bearer, header and body construction ------------------------------------------------------

describe("journal sync api preflight request construction (spec 10.1)", () => {
  it("sends one authenticated JSON POST with the exact strict body", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody({ outcome: "single_part_upload", operation_id: OPERATION_ID, expires_at: "2026-08-18T01:00:00Z" }),
    }));
    await createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT);

    expect(transport.requests).toHaveLength(1);
    const request = transport.requests[0];
    if (request === undefined) {
      throw new Error("expected one preflight request");
    }
    expect(request.url).toBe(`${ORIGIN}/api/sync/journal-events/preflight`);
    expect(request.method).toBe("POST");
    expect(request.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(request.headers["content-type"]).toBe("application/json");
    expect(request.headers["accept"]).toBe("application/json");
    expect(JSON.parse(request.body as string)).toEqual({
      event_id: PREFLIGHT_INPUT.eventId,
      idempotency_key: PREFLIGHT_INPUT.idempotencyKey,
      operation: "create",
      local_file_id: PREFLIGHT_INPUT.localFileId,
      normalized_locator: "notes/one.md",
      sha256: "a".repeat(64),
      size_bytes: 12,
      media_type: "text/plain",
      policy_revision: 4,
    });
  });

  it("omits source and base members for a create and sends both for an update", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody({ outcome: "excluded" }),
    }));
    const api = createApi(transport);
    await api.preflightJournalEvent(PREFLIGHT_INPUT);
    const createBody = JSON.parse(transport.requests[0]?.body as string) as Record<string, unknown>;
    expect(createBody).not.toHaveProperty("source_id");
    expect(createBody).not.toHaveProperty("base_version_id");

    await api.preflightJournalEvent({
      ...PREFLIGHT_INPUT,
      operation: "update",
      sourceId: SOURCE_ID,
      baseVersionId: SOURCE_VERSION_ID,
    });
    const updateBody = JSON.parse(transport.requests[1]?.body as string) as Record<string, unknown>;
    expect(updateBody["source_id"]).toBe(SOURCE_ID);
    expect(updateBody["base_version_id"]).toBe(SOURCE_VERSION_ID);
    expect(updateBody["operation"]).toBe("update");
  });

  it("never issues a request without an access credential", async () => {
    const transport = createRecordingTransport(async () => ({ status: 200, bodyText: successBody({ outcome: "excluded" }) }));
    const api = createJournalSyncApi({
      transport,
      resolveOrigin: () => ORIGIN,
      getAccessToken: () => null,
    });
    await expect(api.preflightJournalEvent(PREFLIGHT_INPUT)).rejects.toMatchObject({
      kind: "login_required",
    });
    expect(transport.requests).toHaveLength(0);
  });
});

describe("journal sync api content request construction (spec 10.2)", () => {
  it("streams the exact bytes as one raw authenticated PUT", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(RECEIPT),
    }));
    const contentBytes = new Uint8Array([1, 2, 3, 250, 0, 128]);
    await createApi(transport).uploadSmallFileContent({
      operationId: OPERATION_ID,
      contentBytes,
    });

    expect(transport.requests).toHaveLength(1);
    const request = transport.requests[0];
    if (request === undefined) {
      throw new Error("expected one content request");
    }
    expect(request.url).toBe(`${ORIGIN}/api/uploads/${OPERATION_ID}/content`);
    expect(request.method).toBe("PUT");
    expect(request.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(request.headers["content-type"]).toBe("application/octet-stream");
    expect(request.headers["accept"]).toBe("application/json");
    expect(request.body).toBeInstanceOf(ArrayBuffer);
    expect(new Uint8Array(request.body as ArrayBuffer)).toEqual(contentBytes);
  });

  it("encodes an operation token that carries URL characters safely", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(RECEIPT),
    }));
    const trickyOperationId = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-+/+==";
    await createApi(transport).uploadSmallFileContent({
      operationId: trickyOperationId,
      contentBytes: new Uint8Array([9]),
    });
    expect(transport.requests[0]?.url).toBe(
      `${ORIGIN}/api/uploads/${encodeURIComponent(trickyOperationId)}/content`,
    );
  });
});

// --- typed outcome parsing -----------------------------------------------------------------------

describe("journal sync api outcome parsing (spec 10.1 table)", () => {
  it.each<readonly [string, unknown, JournalPreflightOutcome]>([
    [
      "single_part_upload",
      { outcome: "single_part_upload", operation_id: OPERATION_ID, expires_at: "2026-08-18T01:00:00Z" },
      { outcome: "single_part_upload", operationId: OPERATION_ID },
    ],
    [
      "committed_replay",
      { outcome: "committed_replay", result: RECEIPT },
      {
        outcome: "committed_replay",
        receipt: { sourceId: SOURCE_ID, sourceVersionId: SOURCE_VERSION_ID, contentVersion: 3 },
      },
    ],
    [
      "no_change",
      {
        outcome: "no_change",
        result: { ...RECEIPT, result_kind: "no_change" },
      },
      {
        outcome: "no_change",
        receipt: { sourceId: SOURCE_ID, sourceVersionId: SOURCE_VERSION_ID, contentVersion: 3 },
      },
    ],
    ["excluded", { outcome: "excluded" }, { outcome: "excluded" }],
    [
      "conflict with a capture grant",
      { outcome: "conflict", operation_id: OPERATION_ID, expires_at: "2026-08-18T01:00:00Z" },
      { outcome: "conflict", operationId: OPERATION_ID, conflictId: null },
    ],
    [
      "conflict with the replayed identity",
      { outcome: "conflict", conflict_id: SOURCE_ID },
      { outcome: "conflict", operationId: null, conflictId: SOURCE_ID },
    ],
    [
      "bare conflict without a grant",
      { outcome: "conflict" },
      { outcome: "conflict", operationId: null, conflictId: null },
    ],
  ])("parses the %s outcome into its closed shape", async (_name, data, expected) => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(data),
    }));
    await expect(createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT)).resolves.toEqual(expected);
  });

  it("parses the opaque conflict identity of one capture upload", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody({
        conflict_id: SOURCE_ID,
        source_id: SOURCE_ID,
        observed_remote_version_id: SOURCE_VERSION_ID,
        captured_at: "2026-08-18T00:00:00Z",
      }),
    }));
    await expect(
      createApi(transport).uploadSmallFileConflictCandidate({
        operationId: OPERATION_ID,
        contentBytes: new Uint8Array(4),
      }),
    ).resolves.toEqual({ conflictId: SOURCE_ID });
    expect(transport.requests[0]?.url).toBe(
      `${ORIGIN}/api/uploads/${OPERATION_ID}/conflict-content`,
    );
    expect(transport.requests[0]?.method).toBe("PUT");
  });

  it("fails a malformed capture receipt closed", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody({ conflict_id: "not-a-uuid" }),
    }));
    await expect(
      createApi(transport).uploadSmallFileConflictCandidate({
        operationId: OPERATION_ID,
        contentBytes: new Uint8Array(4),
      }),
    ).rejects.toMatchObject({ kind: "server_error" });
  });

  it("parses the content receipt of one committed stream", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(RECEIPT),
    }));
    await expect(
      createApi(transport).uploadSmallFileContent({ operationId: OPERATION_ID, contentBytes: new Uint8Array(4) }),
    ).resolves.toEqual({
      sourceId: SOURCE_ID,
      sourceVersionId: SOURCE_VERSION_ID,
      contentVersion: 3,
    });
  });

  it("fails closed on an unknown outcome or a malformed envelope", async () => {
    for (const bodyText of [
      "not json at all",
      successBody({ outcome: "mystery_outcome" }),
      successBody({ outcome: "single_part_upload" }),
      successBody({ outcome: "committed_replay" }),
      successBody({ outcome: "committed_replay", result: { ...RECEIPT, source_id: "not-a-uuid" } }),
      JSON.stringify({ data: null, error: null, request_id: "x", warnings: [] }),
    ]) {
      const transport = createRecordingTransport(async () => ({ status: 200, bodyText }));
      await expect(createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT)).rejects.toMatchObject({
        kind: "server_error",
      });
    }
  });
});

// --- closed failure mapping (spec 12) -------------------------------------------------------------

describe("journal sync api failure mapping (spec 12)", () => {
  it.each<readonly [number, string | null, string]>([
    [401, "device_credential_invalid", "access_expired"],
    [401, null, "access_expired"],
    [403, "authorization_scope_denied", "login_required"],
    // Fix round 5 (finding A): a 403 whose body does NOT parse as the API
    // envelope with a closed error code (an edge/middleware block page) is
    // a wire failure, not a login verdict — the retryable server_error.
    [403, null, "server_error"],
    [429, "authentication_rate_limited", "network_rate_limited"],
    [429, null, "network_rate_limited"],
    [500, "internal_error", "server_error"],
    [503, "database_connection_unavailable", "server_error"],
    [422, "small_file_size_limit_exceeded", "blocked_size"],
    [422, "small_file_content_integrity_failed", "integrity_failed"],
    [409, "small_file_operation_identity_mismatch", "integrity_failed"],
    [404, "small_file_operation_not_found", "operation_retry_required"],
    [410, "small_file_operation_expired", "operation_retry_required"],
    [409, "small_file_upload_state_invalid", "operation_retry_required"],
    // The server's typed create rejection (fix round 2026-08-23): a create
    // whose bound path already has a foreign ACTIVE locator is a permanent
    // business conflict — terminal, never retried.
    [409, "source_locator_conflict", "blocked_conflict"],
    // Policy SYSTEM failures (policy-observability remediation C1): the
    // preflight boundary propagates the typed system errors instead of
    // collapsing them into the 200 `excluded` shape — a missing active
    // signed policy renders as 409 and corrupt signing material as 503.
    // Both carry no dedicated plugin verdict, so they fall through to the
    // retryable `server_error` family: the queue keeps the event under
    // bounded backoff and no queued work is dropped.
    [409, "exclusion_policy_not_initialized", "server_error"],
    [503, "exclusion_policy_signing_unavailable", "server_error"],
    [422, "small_file_preflight_invalid", "server_error"],
    [418, null, "server_error"],
  ])(
    "maps status %s code %s onto the closed kind %s",
    async (status, code, expectedKind) => {
      const transport = createRecordingTransport(async () => ({
        status,
        bodyText: code === null ? "unreadable body" : errorBody(code),
      }));
      await expect(createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT)).rejects.toMatchObject({
        kind: expectedKind,
      });
    },
  );

  it("maps an edge 403 with an HTML challenge body onto the retryable server_error", async () => {
    // Fix round 5 (finding A): the live plugin syncs through a Cloudflare
    // tunnel whose edge intermittently answers 403 with a NON-API HTML
    // challenge/block page. That is a transient wire failure — mapping it
    // onto login_required parked the oldest event under a false login
    // verdict and killed every pass (the whole queue starved behind it).
    const htmlBody = [
      "<!DOCTYPE html><html><head><title>Just a moment...</title></head>",
      "<body>You are being blocked by the edge firewall.</body></html>",
    ].join("");
    const transport = createRecordingTransport(async () => ({
      status: 403,
      bodyText: htmlBody,
    }));
    await expect(createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT)).rejects.toMatchObject({
      kind: "server_error",
    });
  });

  it("keeps a genuine API 403 envelope (parsed, closed code) as login_required", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 403,
      bodyText: errorBody("authorization_scope_denied"),
    }));
    await expect(createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT)).rejects.toMatchObject({
      kind: "login_required",
    });
  });

  it("maps a thrown transport onto network_offline", async () => {
    const transport: SyncHttpTransport = async () => {
      throw new Error("socket gone");
    };
    await expect(createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT)).rejects.toMatchObject({
      kind: "network_offline",
    });
  });

  it("keeps the failure vocabulary closed and the messages free of wire detail", async () => {
    expect(SYNC_API_FAILURE_KINDS).toEqual([
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
    ]);
    const transport = createRecordingTransport(async () => ({
      status: 500,
      bodyText: errorBody("internal_error"),
    }));
    const failure = await createApi(transport)
      .preflightJournalEvent(PREFLIGHT_INPUT)
      .catch((error: unknown) => error);
    expect(failure).toMatchObject({ kind: "server_error" });
    const message = (failure as { message: string }).message;
    expect(message).toBe("sync api failed: server_error");
    for (const forbiddenDetail of [
      ORIGIN,
      ACCESS_TOKEN,
      "internal_error",
      "registered safe message",
      PREFLIGHT_INPUT.eventId,
    ]) {
      expect(message).not.toContain(forbiddenDetail);
    }
  });

  it("marks only claimed-state operation failures as safe for exact-token resume", async () => {
    const claimedTransport = createRecordingTransport(async () => ({
      status: 409,
      bodyText: errorBody("small_file_upload_state_invalid"),
    }));
    const unknownTransport = createRecordingTransport(async () => ({
      status: 404,
      bodyText: errorBody("small_file_operation_not_found"),
    }));

    await expect(createApi(claimedTransport).preflightJournalEvent(PREFLIGHT_INPUT)).rejects.toMatchObject({
      kind: "operation_retry_required",
      canResumeClaimedOperation: true,
    });
    await expect(createApi(unknownTransport).preflightJournalEvent(PREFLIGHT_INPUT)).rejects.toMatchObject({
      kind: "operation_retry_required",
      canResumeClaimedOperation: false,
    });
  });

  it("adds no automatic retry of its own", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 500,
      bodyText: errorBody("internal_error"),
    }));
    await createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT).catch(() => undefined);
    await createApi(transport)
      .uploadSmallFileContent({ operationId: OPERATION_ID, contentBytes: new Uint8Array(2) })
      .catch(() => undefined);
    expect(transport.requests).toHaveLength(2);
  });
});

// --- envelope request id correlation (sync error tracing task 1) ----------------------------------

describe("journal sync api envelope request id correlation (sync error tracing task 1)", () => {
  it("exposes the envelope request id of a successful outcome", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody({ outcome: "excluded" }),
    }));
    const api = createApi(transport);
    expect(api.readLastEnvelopeRequestId()).toBeNull();
    await api.preflightJournalEvent(PREFLIGHT_INPUT);
    expect(api.readLastEnvelopeRequestId()).toBe("66666666-6666-4666-8666-666666666666");
  });

  it("carries the envelope request id on the mapped failure", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 403,
      bodyText: errorBody("authorization_scope_denied"),
    }));
    const api = createApi(transport);
    const failure = await api
      .preflightJournalEvent(PREFLIGHT_INPUT)
      .catch((error: unknown) => error);
    expect(failure).toMatchObject({
      kind: "login_required",
      requestId: "66666666-6666-4666-8666-666666666666",
    });
    expect(api.readLastEnvelopeRequestId()).toBe("66666666-6666-4666-8666-666666666666");
  });

  it("carries the closed server error code on a login_required-class 403 envelope", async () => {
    // Diagnostic round U1: round 5's discrimination already PARSED the
    // envelope's error.code to tell an API 403 from an edge 403 but
    // discarded it. The closed code string now threads onto the mapped
    // failure (alongside the request id) so the diagnostics trail can name
    // WHICH server registry code rejected the request.
    const transport = createRecordingTransport(async () => ({
      status: 403,
      bodyText: errorBody("exclusion_policy_denied"),
    }));
    const failure = await createApi(transport)
      .preflightJournalEvent(PREFLIGHT_INPUT)
      .catch((error: unknown) => error);
    expect(failure).toMatchObject({
      kind: "login_required",
      wireErrorCode: "exclusion_policy_denied",
      requestId: "66666666-6666-4666-8666-666666666666",
    });
  });

  it("keeps the wire error code null when the failing body is not the API envelope", async () => {
    // The edge HTML 403 carries no closed error code: nothing extra threads
    // onto the failure (the kind token already says server_error).
    const transport = createRecordingTransport(async () => ({
      status: 403,
      bodyText: "<!DOCTYPE html><html><body>edge block</body></html>",
    }));
    const failure = await createApi(transport)
      .preflightJournalEvent(PREFLIGHT_INPUT)
      .catch((error: unknown) => error);
    expect(failure).toMatchObject({
      kind: "server_error",
      wireErrorCode: null,
      requestId: null,
    });
  });

  it("keeps a non-uuid or absent request id out of the correlation surface", async () => {
    const foreignBody = JSON.stringify({
      data: { outcome: "excluded" },
      error: null,
      request_id: "opaque-free-form",
      warnings: [],
    });
    const transport = createRecordingTransport(async () => ({ status: 200, bodyText: foreignBody }));
    const api = createApi(transport);
    await api.preflightJournalEvent(PREFLIGHT_INPUT);
    expect(api.readLastEnvelopeRequestId()).toBeNull();

    const htmlTransport = createRecordingTransport(async () => ({
      status: 403,
      bodyText: "<html>edge block</html>",
    }));
    const htmlApi = createApi(htmlTransport);
    const failure = await htmlApi
      .preflightJournalEvent(PREFLIGHT_INPUT)
      .catch((error: unknown) => error);
    expect(failure).toMatchObject({ kind: "server_error", requestId: null });
    expect(htmlApi.readLastEnvelopeRequestId()).toBeNull();
  });
});

// --- the multipart surface (child 7 spec 5) ---------------------------------------------------------

const MULTIPART_SESSION_ID = "bXVsdGlwYXJ0LXNlc3Npb24taWRlbnRpdHktMDEyMzQ1Njc4OTA";
const MULTIPART_R2_URL =
  "https://r2.example.net/staging/session/part-2?X-Amz-Signature=secret-2-1&part=2";
const MULTIPART_EXPIRES_AT = "2026-08-29T00:00:00Z";

/** One canonical multipart session plan data member set. */
function multipartPlanBody(overrides: Record<string, unknown> = {}): unknown {
  return {
    session_id: MULTIPART_SESSION_ID,
    part_count: 3,
    part_size_bytes: 8 * 1024 * 1024,
    expires_at: MULTIPART_EXPIRES_AT,
    ...overrides,
  };
}

/** One canonical multipart session status data member set. */
function multipartStatusBody(overrides: Record<string, unknown> = {}): unknown {
  return {
    session_id: MULTIPART_SESSION_ID,
    state: "uploading",
    part_count: 3,
    part_size_bytes: 8 * 1024 * 1024,
    expires_at: MULTIPART_EXPIRES_AT,
    completed_part_numbers: [1],
    terminal_result: null,
    ...overrides,
  };
}

/** One canonical presigned part authorization data member set. */
function multipartPartUrlBody(overrides: Record<string, unknown> = {}): unknown {
  return {
    url: MULTIPART_R2_URL,
    part_number: 2,
    offset_bytes: 8 * 1024 * 1024,
    size_bytes: 8 * 1024 * 1024,
    expires_at: MULTIPART_EXPIRES_AT,
    ...overrides,
  };
}

describe("journal sync api multipart surface (child 7 spec 5)", () => {
  it("sends one authenticated create with the exact preflight-shaped body", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(multipartPlanBody()),
    }));
    await createApi(transport).createMultipartUploadSession(PREFLIGHT_INPUT);

    expect(transport.requests).toHaveLength(1);
    const request = transport.requests[0];
    if (request === undefined) {
      throw new Error("expected one create request");
    }
    expect(request.url).toBe(`${ORIGIN}/api/uploads/multipart-sessions`);
    expect(request.method).toBe("POST");
    expect(request.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(request.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(request.body as string)).toEqual({
      event_id: PREFLIGHT_INPUT.eventId,
      idempotency_key: PREFLIGHT_INPUT.idempotencyKey,
      operation: "create",
      local_file_id: PREFLIGHT_INPUT.localFileId,
      normalized_locator: "notes/one.md",
      sha256: "a".repeat(64),
      size_bytes: 12,
      media_type: "text/plain",
      policy_revision: 4,
    });
  });

  it("parses the session plan of one create", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(multipartPlanBody()),
    }));
    await expect(
      createApi(transport).createMultipartUploadSession(PREFLIGHT_INPUT),
    ).resolves.toEqual({
      sessionId: MULTIPART_SESSION_ID,
      partCount: 3,
      partSizeBytes: 8 * 1024 * 1024,
      expiresAtEpochMs: Date.parse(MULTIPART_EXPIRES_AT),
    });
  });

  it("sends one authenticated session status GET and parses the reconciliation", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(multipartStatusBody()),
    }));
    await expect(
      createApi(transport).getMultipartUploadSession(MULTIPART_SESSION_ID),
    ).resolves.toEqual({
      sessionId: MULTIPART_SESSION_ID,
      state: "uploading",
      partCount: 3,
      partSizeBytes: 8 * 1024 * 1024,
      expiresAtEpochMs: Date.parse(MULTIPART_EXPIRES_AT),
      completedPartNumbers: [1],
      terminalResult: null,
    });
    const request = transport.requests[0];
    expect(request?.method).toBe("GET");
    expect(request?.url).toBe(
      `${ORIGIN}/api/uploads/multipart-sessions/${MULTIPART_SESSION_ID}`,
    );
    expect(request?.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(request?.body).toBeUndefined();
  });

  it("issues exactly one part URL through one authenticated POST", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(multipartPartUrlBody()),
    }));
    await expect(
      createApi(transport).issueMultipartPartUrl({
        sessionId: MULTIPART_SESSION_ID,
        partNumber: 2,
      }),
    ).resolves.toEqual({
      url: MULTIPART_R2_URL,
      partNumber: 2,
      offsetBytes: 8 * 1024 * 1024,
      sizeBytes: 8 * 1024 * 1024,
      expiresAtEpochMs: Date.parse(MULTIPART_EXPIRES_AT),
    });
    const request = transport.requests[0];
    expect(request?.method).toBe("POST");
    expect(request?.url).toBe(
      `${ORIGIN}/api/uploads/multipart-sessions/${MULTIPART_SESSION_ID}/parts/2/url`,
    );
    expect(request?.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
  });

  it("requests completion and parses the frozen terminal result", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody({
        state: "committed",
        terminal_result: {
          result_kind: "committed",
          source_id: SOURCE_ID,
          source_version_id: SOURCE_VERSION_ID,
          content_version: 3,
          committed_at: "2026-08-18T00:00:00Z",
        },
      }),
    }));
    await expect(
      createApi(transport).completeMultipartUploadSession(MULTIPART_SESSION_ID),
    ).resolves.toEqual({
      state: "committed",
      terminalReceipt: {
        resultKind: "committed",
        sourceId: SOURCE_ID,
        sourceVersionId: SOURCE_VERSION_ID,
        contentVersion: 3,
      },
    });
    const request = transport.requests[0];
    expect(request?.method).toBe("POST");
    expect(request?.url).toBe(
      `${ORIGIN}/api/uploads/multipart-sessions/${MULTIPART_SESSION_ID}/complete`,
    );
  });

  it("requests abort through one authenticated POST and parses the cancel state", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(multipartStatusBody({ state: "cancelling" })),
    }));
    await expect(
      createApi(transport).abortMultipartUploadSession(MULTIPART_SESSION_ID),
    ).resolves.toMatchObject({ state: "cancelling" });
    const request = transport.requests[0];
    expect(request?.method).toBe("POST");
    expect(request?.url).toBe(
      `${ORIGIN}/api/uploads/multipart-sessions/${MULTIPART_SESSION_ID}/abort`,
    );
  });

  it("PUTs the exact part bytes to the presigned URL with no credential", async () => {
    const transport = createRecordingTransport(async () => ({ status: 200, bodyText: "" }));
    await expect(
      createApi(transport).putMultipartPartBytes({
        url: MULTIPART_R2_URL,
        contentBytes: new Uint8Array([4, 5, 6]),
      }),
    ).resolves.toBe("uploaded");
    const request = transport.requests[0];
    if (request === undefined) {
      throw new Error("expected one part PUT");
    }
    expect(request.url).toBe(MULTIPART_R2_URL);
    expect(request.method).toBe("PUT");
    expect(request.headers["authorization"]).toBeUndefined();
    expect(request.body).toBeInstanceOf(ArrayBuffer);
    expect(new Uint8Array(request.body as ArrayBuffer)).toEqual(new Uint8Array([4, 5, 6]));
  });

  it("classifies a rejected presigned URL as url_rejected and other failures closed", async () => {
    for (const [status, expectation] of [
      [403, "url_rejected"],
      [401, "url_rejected"],
    ] as const) {
      const rejected = createRecordingTransport(async () => ({ status, bodyText: "denied" }));
      await expect(
        createApi(rejected).putMultipartPartBytes({
          url: MULTIPART_R2_URL,
          contentBytes: new Uint8Array(2),
        }),
      ).resolves.toBe(expectation);
    }
    const failed = createRecordingTransport(async () => ({ status: 500, bodyText: "boom" }));
    await expect(
      createApi(failed).putMultipartPartBytes({
        url: MULTIPART_R2_URL,
        contentBytes: new Uint8Array(2),
      }),
    ).rejects.toMatchObject({ kind: "server_error" });
    const offline: SyncHttpTransport = async () => {
      throw new Error("socket gone");
    };
    await expect(
      createApi(offline).putMultipartPartBytes({
        url: MULTIPART_R2_URL,
        contentBytes: new Uint8Array(2),
      }),
    ).rejects.toMatchObject({ kind: "network_offline" });
  });

  it("parses the multipart_upload preflight outcome without any storage handle", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody({ outcome: "multipart_upload" }),
    }));
    await expect(createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT)).resolves.toEqual({
      outcome: "multipart_upload",
    });
  });

  it.each<readonly [number, string, string]>([
    [404, "multipart_session_not_found", "operation_retry_required"],
    [410, "multipart_session_expired", "operation_retry_required"],
    [409, "multipart_session_state_invalid", "operation_retry_required"],
    [409, "multipart_completion_in_progress", "operation_retry_required"],
    [503, "multipart_part_url_rejected", "server_error"],
    [422, "multipart_part_invalid", "server_error"],
    [503, "multipart_cleanup_failed", "server_error"],
    [503, "multipart_dependency_unavailable", "server_error"],
    [422, "multipart_provider_state_invalid", "integrity_failed"],
    [422, "multipart_integrity_failed", "integrity_failed"],
    // A policy denial is a genuine API 403 carrying the canonical envelope:
    // it must never collapse onto the login verdict.
    [403, "multipart_policy_denied", "policy_denied"],
  ])("maps status %s code %s onto kind %s", async (status, code, expectedKind) => {
    const transport = createRecordingTransport(async () => ({
      status,
      bodyText: errorBody(code),
    }));
    await expect(
      createApi(transport).getMultipartUploadSession(MULTIPART_SESSION_ID),
    ).rejects.toMatchObject({ kind: expectedKind, wireErrorCode: code });
  });

  it("fails closed on malformed multipart wire data", async () => {
    const malformedBodies = [
      successBody(multipartPlanBody({ session_id: "not-a-session-id" })),
      successBody(multipartPlanBody({ part_count: 0 })),
      successBody(multipartPlanBody({ part_size_bytes: 4 })),
      successBody(multipartPlanBody({ expires_at: "not-a-date" })),
      successBody(multipartStatusBody({ state: "mystery_state" })),
      successBody(multipartStatusBody({ completed_part_numbers: [0] })),
      successBody(multipartStatusBody({ completed_part_numbers: [2, 2] })),
      successBody(multipartPartUrlBody({ part_number: 3 })),
      successBody({ state: "committed" }),
    ];
    for (const [index, bodyText] of malformedBodies.entries()) {
      const transport = createRecordingTransport(async () => ({ status: 200, bodyText }));
      const api = createApi(transport);
      const attempt =
        index < 4
          ? api.createMultipartUploadSession(PREFLIGHT_INPUT)
          : index < 7
            ? api.getMultipartUploadSession(MULTIPART_SESSION_ID)
            : index === 7
              ? api.issueMultipartPartUrl({ sessionId: MULTIPART_SESSION_ID, partNumber: 2 })
              : api.completeMultipartUploadSession(MULTIPART_SESSION_ID);
      await expect(attempt).rejects.toMatchObject({ kind: "server_error" });
    }
  });
});

// --- the raw requestUrl adapter --------------------------------------------------------------------

describe("raw requestUrl sync transport adapter", () => {
  it("passes method, headers and the raw ArrayBuffer body through once", async () => {
    const calls: unknown[] = [];
    const body = new Uint8Array([0, 255, 1, 254]).buffer as ArrayBuffer;
    const transport = createRequestUrlSyncTransport(async (param) => {
      calls.push(param);
      return { status: 200, text: successBody(RECEIPT), headers: {}, arrayBuffer: new ArrayBuffer(0), json: undefined };
    });
    const response = await transport({
      url: `${ORIGIN}/api/uploads/${OPERATION_ID}/content`,
      method: "PUT",
      headers: { authorization: `Bearer ${ACCESS_TOKEN}`, "content-type": "application/octet-stream" },
      body,
    });
    expect(response).toEqual({ status: 200, bodyText: successBody(RECEIPT) });
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({
      url: `${ORIGIN}/api/uploads/${OPERATION_ID}/content`,
      method: "PUT",
      throw: false,
      body,
    });
    expect((calls[0] as { headers: Record<string, string> }).headers).toEqual({
      authorization: `Bearer ${ACCESS_TOKEN}`,
      "content-type": "application/octet-stream",
    });
  });

  it("passes a string body through for JSON requests and propagates failures", async () => {
    const calls: unknown[] = [];
    const transport = createRequestUrlSyncTransport(async (param) => {
      calls.push(param);
      return { status: 201, text: "created", headers: {}, arrayBuffer: new ArrayBuffer(0), json: undefined };
    });
    const response = await transport({
      url: `${ORIGIN}/api/sync/journal-events/preflight`,
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    expect(response).toEqual({ status: 201, bodyText: "created" });
    expect(calls[0]).toMatchObject({ method: "POST", body: "{}" });

    const failing = createRequestUrlSyncTransport(async () => {
      throw new Error("offline");
    });
    await expect(
      failing({
        url: ORIGIN,
        method: "POST",
        headers: {},
        body: "{}",
      }),
    ).rejects.toThrow("offline");
  });
});
