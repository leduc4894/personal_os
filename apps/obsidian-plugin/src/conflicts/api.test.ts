/**
 * Tests of the hand-mirrored Conflict Inbox wire client (Child 8 spec 6,
 * Task 7). These tests pin: exact bearer/header/body construction of the
 * four Task 6 operations, the strict decoding of every canonical success
 * envelope, the closed failure mapping of the conflict error registry
 * (including the retryable commit-outcome-unknown replay verdict), the
 * verified evidence download (exact bytes with their exact canonical
 * media type and verified length), the pre-transport input grammar
 * rejection, and the privacy invariant that no response text, status
 * number or URL ever reaches a thrown error message.
 */

import { describe, expect, it } from "vitest";

import type { DeviceSyncHttpTransport, DeviceSyncHttpResponse } from "../device-sync/api";
import type { SyncHttpRequest } from "../journal/sync-api";
import { createConflictApi } from "./api";
import type { ConflictResolveInput } from "./contracts";

// --- shared fixtures ---------------------------------------------------------------------------

const ORIGIN = "https://sync.example.org";
const ACCESS_TOKEN = "at1.test-access-credential";

const CONFLICT_ID = "11111111-1111-4111-8111-111111111111";
const SOURCE_ID = "22222222-2222-4222-8222-222222222222";
const ORIGINATING_EVENT_ID = "33333333-3333-4333-8333-333333333333";
const ORIGINATING_DEVICE_ID = "44444444-4444-4444-8444-444444444444";
const BASE_VERSION_ID = "55555555-5555-4555-8555-555555555555";
const OBSERVED_REMOTE_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const VERIFIED_CANDIDATE_OBJECT_ID = "77777777-7777-4777-8777-777777777777";
const RESOLUTION_EVENT_ID = "88888888-8888-4888-8888-888888888888";
const RESULTING_VERSION_ID = "99999999-9999-4999-8999-999999999999";
const SUCCESSOR_CONFLICT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const IDEMPOTENCY_KEY = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const REQUEST_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

const SUMMARY_WIRE = {
  conflict_id: CONFLICT_ID,
  source_id: SOURCE_ID,
  conflict_kind: "stale_content",
  status: "open",
  originating_event_id: ORIGINATING_EVENT_ID,
  originating_device_id: ORIGINATING_DEVICE_ID,
  base_version_id: BASE_VERSION_ID,
  observed_remote_version_id: OBSERVED_REMOTE_VERSION_ID,
  candidate_kind: "content",
  verified_candidate_object_id: VERIFIED_CANDIDATE_OBJECT_ID,
  captured_at: "2026-09-01T00:00:00Z",
  resolution_kind: null,
  resolution_event_id: null,
  resulting_version_id: null,
  successor_conflict_id: null,
  closed_at: null,
};

/** Build one canonical success envelope body around the given data. */
function successBody(data: unknown): string {
  return JSON.stringify({
    data,
    error: null,
    request_id: REQUEST_ID,
    warnings: [],
  });
}

/** Build one canonical error envelope body around the given code. */
function errorBody(code: string): string {
  return JSON.stringify({
    data: null,
    error: { code, message: "registered safe message", details: {}, retryable: false },
    request_id: REQUEST_ID,
    warnings: [],
  });
}

/**
 * A recording binary-capable transport: every request lands in the
 * journal of calls, and each scripted response answers in order.
 */
function createRecordingTransport(
  respond: (request: SyncHttpRequest, index: number) => Promise<DeviceSyncHttpResponse>,
): DeviceSyncHttpTransport & { readonly requests: SyncHttpRequest[] } {
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

function jsonResponse(status: number, bodyText: string): DeviceSyncHttpResponse {
  return { status, bodyText, bodyBytes: null, headers: {} };
}

function createApi(transport: DeviceSyncHttpTransport, accessToken: string | null = ACCESS_TOKEN) {
  return createConflictApi({
    transport,
    resolveOrigin: () => ORIGIN,
    getAccessToken: () => accessToken,
  });
}

const KEEP_REMOTE_INPUT: ConflictResolveInput = {
  conflictId: CONFLICT_ID,
  resolutionEventId: RESOLUTION_EVENT_ID,
  idempotencyKey: IDEMPOTENCY_KEY,
  resolutionKind: "keep_remote",
  reviewedRemoteVersionId: OBSERVED_REMOTE_VERSION_ID,
  verifiedCandidateObjectId: null,
};

// --- list and detail ----------------------------------------------------------------------------

describe("conflict api list construction (spec 6)", () => {
  it("sends one authenticated GET and decodes the bounded page", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(
        200,
        successBody({
          conflicts: [SUMMARY_WIRE],
          has_more: true,
          next_exclusive_start_conflict_id: SUCCESSOR_CONFLICT_ID,
        }),
      ),
    );
    const page = await createApi(transport).listConflicts({
      limit: 50,
      exclusiveStartConflictId: SUCCESSOR_CONFLICT_ID,
    });

    expect(transport.requests).toHaveLength(1);
    const request = transport.requests[0];
    expect(request?.method).toBe("GET");
    expect(request?.url).toBe(
      `${ORIGIN}/api/sync/conflicts?limit=50&exclusive_start_conflict_id=${SUCCESSOR_CONFLICT_ID}`,
    );
    expect(request?.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(request?.headers["accept"]).toBe("application/json");
    expect(request?.body).toBeUndefined();

    expect(page.hasMore).toBe(true);
    expect(page.nextExclusiveStartConflictId).toBe(SUCCESSOR_CONFLICT_ID);
    expect(page.conflicts[0]?.conflictId).toBe(CONFLICT_ID);
  });

  it("sends the bare list route without query members by default", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(200, successBody({ conflicts: [], has_more: false, next_exclusive_start_conflict_id: null })),
    );
    await createApi(transport).listConflicts();

    expect(transport.requests[0]?.url).toBe(`${ORIGIN}/api/sync/conflicts`);
  });

  it("rejects an out-of-bound page limit before any transport contact", async () => {
    const transport = createRecordingTransport(async () => jsonResponse(200, successBody({})));
    for (const limit of [0, -1, 201, 1.5]) {
      await expect(createApi(transport).listConflicts({ limit })).rejects.toMatchObject({
        kind: "input_invalid",
      });
    }
    expect(transport.requests).toHaveLength(0);
  });

  it("rejects a non-UUID continuation cursor before any transport contact", async () => {
    const transport = createRecordingTransport(async () => jsonResponse(200, successBody({})));
    await expect(
      createApi(transport).listConflicts({ exclusiveStartConflictId: "cursor" }),
    ).rejects.toMatchObject({ kind: "input_invalid" });
    expect(transport.requests).toHaveLength(0);
  });
});

describe("conflict api detail construction (spec 6)", () => {
  it("sends one authenticated GET and decodes the detail with its choices", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(200, successBody({ ...SUMMARY_WIRE, choices: ["keep_remote", "keep_local", "save_merged"] })),
    );
    const detail = await createApi(transport).getConflict(CONFLICT_ID);

    expect(transport.requests[0]?.url).toBe(`${ORIGIN}/api/sync/conflicts/${CONFLICT_ID}`);
    expect(transport.requests[0]?.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(detail.choices).toEqual(["keep_remote", "keep_local", "save_merged"]);
    expect(detail.verifiedCandidateObjectId).toBe(VERIFIED_CANDIDATE_OBJECT_ID);
  });

  it("rejects a non-UUID conflict id before any transport contact", async () => {
    const transport = createRecordingTransport(async () => jsonResponse(200, successBody({})));
    await expect(createApi(transport).getConflict("inbox-item-1")).rejects.toMatchObject({
      kind: "input_invalid",
    });
    expect(transport.requests).toHaveLength(0);
  });

  it("maps the unknown-conflict envelope onto the closed not-found kind", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(404, errorBody("source_conflict_not_found")),
    );
    await expect(createApi(transport).getConflict(CONFLICT_ID)).rejects.toMatchObject({
      kind: "conflict_not_found",
      requestId: REQUEST_ID,
      wireErrorCode: "source_conflict_not_found",
    });
  });
});

// --- verified evidence download -------------------------------------------------------------------

describe("conflict api verified evidence download (spec 6)", () => {
  const EVIDENCE_BYTES = new TextEncoder().encode("# merged draft bytes\n");

  function evidenceResponse(
    overrides: Partial<DeviceSyncHttpResponse> = {},
  ): DeviceSyncHttpResponse {
    return {
      status: 200,
      bodyText: "",
      bodyBytes: EVIDENCE_BYTES.slice().buffer as ArrayBuffer,
      headers: {
        "content-type": "text/markdown",
        "content-length": String(EVIDENCE_BYTES.byteLength),
      },
      ...overrides,
    };
  }

  it("returns the exact verified bytes with their canonical media type and length", async () => {
    const transport = createRecordingTransport(async (request) => {
      expect(request.url).toBe(`${ORIGIN}/api/sync/conflicts/${CONFLICT_ID}/evidence/candidate`);
      return evidenceResponse();
    });
    const evidence = await createApi(transport).downloadConflictEvidence({
      conflictId: CONFLICT_ID,
      role: "candidate",
    });

    expect(transport.requests[0]?.headers["accept"]).toBe("application/octet-stream");
    expect(transport.requests[0]?.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(evidence.bytes).toEqual(EVIDENCE_BYTES);
    expect(evidence.mediaType).toBe("text/markdown");
    expect(evidence.sizeBytes).toBe(EVIDENCE_BYTES.byteLength);
  });

  it("downloads each closed role on its own route segment", async () => {
    for (const role of ["base", "remote", "candidate"] as const) {
      const transport = createRecordingTransport(async () => evidenceResponse());
      await createApi(transport).downloadConflictEvidence({ conflictId: CONFLICT_ID, role });
      expect(transport.requests[0]?.url).toBe(
        `${ORIGIN}/api/sync/conflicts/${CONFLICT_ID}/evidence/${role}`,
      );
    }
  });

  it("fails closed when the declared length does not match the bytes", async () => {
    const transport = createRecordingTransport(async () =>
      evidenceResponse({ headers: { "content-type": "text/markdown", "content-length": "999" } }),
    );
    await expect(
      createApi(transport).downloadConflictEvidence({ conflictId: CONFLICT_ID, role: "base" }),
    ).rejects.toMatchObject({ kind: "evidence_download_invalid" });
  });

  it("fails closed when the media type header is missing or malformed", async () => {
    for (const headers of [
      {},
      { "content-type": "markdown" },
      { "content-type": "text/markdown; charset=utf-8" },
    ]) {
      const transport = createRecordingTransport(async () =>
        evidenceResponse({
          headers: { ...headers, "content-length": String(EVIDENCE_BYTES.byteLength) },
        }),
      );
      await expect(
        createApi(transport).downloadConflictEvidence({ conflictId: CONFLICT_ID, role: "base" }),
      ).rejects.toMatchObject({ kind: "evidence_download_invalid" });
    }
  });

  it("fails closed when a success status carries no body bytes", async () => {
    const transport = createRecordingTransport(async () => ({
      status: 200,
      bodyText: "",
      bodyBytes: null,
      headers: { "content-type": "text/markdown", "content-length": "1" },
    }));
    await expect(
      createApi(transport).downloadConflictEvidence({ conflictId: CONFLICT_ID, role: "base" }),
    ).rejects.toMatchObject({ kind: "evidence_download_invalid" });
  });

  it("maps the evidence-unavailable envelope onto its closed kind", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(404, errorBody("source_conflict_evidence_unavailable")),
    );
    await expect(
      createApi(transport).downloadConflictEvidence({ conflictId: CONFLICT_ID, role: "base" }),
    ).rejects.toMatchObject({ kind: "evidence_unavailable" });
  });

  it("maps the evidence integrity envelope onto its closed kind", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(422, errorBody("source_conflict_evidence_integrity_failed")),
    );
    await expect(
      createApi(transport).downloadConflictEvidence({ conflictId: CONFLICT_ID, role: "remote" }),
    ).rejects.toMatchObject({ kind: "evidence_integrity_failed" });
  });

  it("rejects an unknown role before any transport contact", async () => {
    const transport = createRecordingTransport(async () => evidenceResponse());
    await expect(
      createApi(transport).downloadConflictEvidence({
        conflictId: CONFLICT_ID,
        role: "local" as never,
      }),
    ).rejects.toMatchObject({ kind: "input_invalid" });
    expect(transport.requests).toHaveLength(0);
  });
});

// --- resolve ---------------------------------------------------------------------------------------

describe("conflict api resolve construction (spec 6)", () => {
  it("posts the strict resolution body and decodes the resolved outcome", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(
        200,
        successBody({
          outcome: "resolved",
          conflict_id: CONFLICT_ID,
          resolution_event_id: RESOLUTION_EVENT_ID,
          resolution_kind: "keep_remote",
          resulting_version_id: null,
          successor_conflict_id: null,
          completed_at: "2026-09-02T00:00:00Z",
        }),
      ),
    );
    const resolution = await createApi(transport).resolveConflict(KEEP_REMOTE_INPUT);

    expect(transport.requests).toHaveLength(1);
    const request = transport.requests[0];
    expect(request?.method).toBe("POST");
    expect(request?.url).toBe(`${ORIGIN}/api/sync/conflicts/${CONFLICT_ID}/resolve`);
    expect(request?.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(request?.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(request?.body as string)).toEqual({
      resolution_event_id: RESOLUTION_EVENT_ID,
      idempotency_key: IDEMPOTENCY_KEY,
      resolution_kind: "keep_remote",
      reviewed_remote_version_id: OBSERVED_REMOTE_VERSION_ID,
    });

    expect(resolution.outcome).toBe("resolved");
    expect(resolution.conflictId).toBe(CONFLICT_ID);
  });

  it("carries the verified object reference only under save_merged", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(
        200,
        successBody({
          outcome: "resolved",
          conflict_id: CONFLICT_ID,
          resolution_event_id: RESOLUTION_EVENT_ID,
          resolution_kind: "save_merged",
          resulting_version_id: RESULTING_VERSION_ID,
          successor_conflict_id: null,
          completed_at: "2026-09-02T00:00:00Z",
        }),
      ),
    );
    const resolution = await createApi(transport).resolveConflict({
      ...KEEP_REMOTE_INPUT,
      resolutionKind: "save_merged",
      verifiedCandidateObjectId: VERIFIED_CANDIDATE_OBJECT_ID,
    });

    expect(JSON.parse(transport.requests[0]?.body as string)).toEqual({
      resolution_event_id: RESOLUTION_EVENT_ID,
      idempotency_key: IDEMPOTENCY_KEY,
      resolution_kind: "save_merged",
      reviewed_remote_version_id: OBSERVED_REMOTE_VERSION_ID,
      verified_candidate_object_id: VERIFIED_CANDIDATE_OBJECT_ID,
    });
    expect(resolution.resultingVersionId).toBe(RESULTING_VERSION_ID);
  });

  it("decodes a stale successor outcome as a typed success", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(
        200,
        successBody({
          outcome: "stale_successor",
          conflict_id: CONFLICT_ID,
          resolution_event_id: RESOLUTION_EVENT_ID,
          resolution_kind: "keep_remote",
          resulting_version_id: null,
          successor_conflict_id: SUCCESSOR_CONFLICT_ID,
          completed_at: "2026-09-02T00:00:00Z",
        }),
      ),
    );
    const resolution = await createApi(transport).resolveConflict(KEEP_REMOTE_INPUT);
    expect(resolution.outcome).toBe("stale_successor");
    expect(resolution.successorConflictId).toBe(SUCCESSOR_CONFLICT_ID);
  });

  it("rejects a save_merged body without its verified object reference before transport", async () => {
    const transport = createRecordingTransport(async () => jsonResponse(200, successBody({})));
    await expect(
      createApi(transport).resolveConflict({
        ...KEEP_REMOTE_INPUT,
        resolutionKind: "save_merged",
        verifiedCandidateObjectId: null,
      }),
    ).rejects.toMatchObject({ kind: "input_invalid" });
    expect(transport.requests).toHaveLength(0);
  });

  it("maps the state-invalid, idempotency-mismatch and input-invalid envelopes", async () => {
    for (const [code, kind] of [
      ["source_conflict_state_invalid", "conflict_state_invalid"],
      ["source_conflict_idempotency_mismatch", "conflict_idempotency_mismatch"],
      ["source_conflict_input_invalid", "input_invalid"],
    ] as const) {
      const transport = createRecordingTransport(async () => jsonResponse(409, errorBody(code)));
      await expect(createApi(transport).resolveConflict(KEEP_REMOTE_INPUT)).rejects.toMatchObject({
        kind,
        wireErrorCode: code,
      });
    }
  });

  it("maps the retryable commit-outcome-unknown verdict as replay-safe", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(503, errorBody("source_conflict_commit_outcome_unknown")),
    );
    const error = (await createApi(transport)
      .resolveConflict(KEEP_REMOTE_INPUT)
      .catch((caught: unknown) => caught)) as { kind: string; canRetry: boolean };
    expect(error.kind).toBe("commit_outcome_unknown");
    expect(error.canRetry).toBe(true);
  });

  it("maps the retryable dependency outage", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(503, errorBody("source_conflict_dependency_unavailable")),
    );
    const error = (await createApi(transport)
      .resolveConflict(KEEP_REMOTE_INPUT)
      .catch((caught: unknown) => caught)) as { kind: string; canRetry: boolean };
    expect(error.kind).toBe("dependency_unavailable");
    expect(error.canRetry).toBe(true);
  });
});

// --- transport and credential failures ---------------------------------------------------------------

describe("conflict api failure mapping and privacy", () => {
  it("maps an expired access credential", async () => {
    const transport = createRecordingTransport(async () => jsonResponse(401, errorBody("device_credential_invalid")));
    await expect(createApi(transport).listConflicts()).rejects.toMatchObject({
      kind: "access_expired",
      requestId: REQUEST_ID,
    });
  });

  it("maps a scope denial behind a genuine API envelope as login-required", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(403, errorBody("authorization_scope_denied")),
    );
    await expect(createApi(transport).listConflicts()).rejects.toMatchObject({
      kind: "login_required",
    });
  });

  it("maps the policy denial envelope onto its own closed kind", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(403, errorBody("exclusion_policy_denied")),
    );
    await expect(createApi(transport).getConflict(CONFLICT_ID)).rejects.toMatchObject({
      kind: "policy_denied",
    });
  });

  it("maps a malformed success body onto the retryable server error", async () => {
    const transport = createRecordingTransport(async () => jsonResponse(200, "<html>not json</html>"));
    const error = (await createApi(transport)
      .listConflicts()
      .catch((caught: unknown) => caught)) as { kind: string; canRetry: boolean };
    expect(error.kind).toBe("server_error");
    expect(error.canRetry).toBe(true);
  });

  it("maps a transport rejection onto the retryable offline kind without response detail", async () => {
    const transport = createRecordingTransport(async () => {
      throw new Error("DNS lookup failed for secret-host.internal");
    });
    const error = (await createApi(transport)
      .listConflicts()
      .catch((caught: unknown) => caught)) as Error & { kind: string };
    expect(error.kind).toBe("network_offline");
    expect(String(error.message)).not.toContain("secret-host");
  });

  it("rejects a missing access credential before any transport attempt", async () => {
    const transport = createRecordingTransport(async () => jsonResponse(200, successBody({})));
    await expect(createApi(transport, null).listConflicts()).rejects.toMatchObject({
      kind: "login_required",
      isCredentialAbsent: true,
    });
    expect(transport.requests).toHaveLength(0);
  });

  it("never carries response text, status numbers or URLs in a thrown message", async () => {
    const transport = createRecordingTransport(async () =>
      jsonResponse(500, JSON.stringify({ data: null, error: { code: "internal_error" }, request_id: REQUEST_ID })),
    );
    const error = (await createApi(transport)
      .listConflicts()
      .catch((caught: unknown) => caught)) as Error;
    expect(String(error.message)).not.toContain("internal_error");
    expect(String(error.message)).not.toContain("500");
    expect(String(error.message)).not.toContain(ORIGIN);
  });
});
