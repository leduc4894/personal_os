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
    ["conflict", { outcome: "conflict" }, { outcome: "conflict" }],
  ])("parses the %s outcome into its closed shape", async (_name, data, expected) => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: successBody(data),
    }));
    await expect(createApi(transport).preflightJournalEvent(PREFLIGHT_INPUT)).resolves.toEqual(expected);
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
      "integrity_failed",
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
