/**
 * Tests for the generated-client lifecycle API adapter (Task 9).
 *
 * The adapter sits between the lifecycle driver and the openapi-fetch
 * generated `commitSourceLifecycleEvent` POST. The contract under test:
 *
 *   - The request URL is the openapi-generated
 *     `/api/sources/lifecycle-events` path; no hand-wired fetch call
 *     may replace it.
 *   - The Authorization header carries `Bearer <access-token>`; the
 *     body never carries workspace, device or user identifiers (those
 *     derive from the resolved bearer context server-side).
 *   - One AbortSignal propagates into the request and aborts the
 *     transport before the response is mapped.
 *   - The success envelope maps to a closed typed `LifecycleResult`;
 *     the closed error envelope maps to a thrown `LifecycleApiError`
 *     whose `kind` is one of the closed safe failure kinds
 *     (`conflict | integrity | retry | login_required`).
 *
 * Privacy (spec 9): the test surface never asserts against path,
 * digest, locator, raw token, secret or provider detail. The transport
 * records raw request bytes only for shape verification.
 */

import { describe, expect, it } from "vitest";

import { createApiClient, type ApiClient, type ApiTransport } from "@workspace/api-client";

import {
  createLifecycleApi,
  LifecycleApiError,
  type LifecycleApiOptions,
  type LifecycleResult,
} from "./lifecycle-api";
import { type FrozenLifecycleEvent } from "./lifecycle-repository";
import {
  createLifecycleEventOperands,
  type LifecycleEventOperands,
} from "./lifecycle-contracts";

const SOURCE_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_ID = "22222222-2222-4222-8222-222222222222";
const EVENT_ID = "33333333-3333-4333-8333-333333333333";
const IDEMPOTENCY_KEY = "44444444-4444-4444-8444-444444444444";
const RESULT_TOMBSTONE_ID = "55555555-5555-4555-8555-555555555555";
const NEW_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const PREVIOUS_EVENT_ID = "77777777-7777-4777-8777-777777777777";
const REQUEST_ID = "88888888-8888-4888-8888-888888888888";
const ACCESS_TOKEN = "opaque-access-token-value";
const BASE_URL = "https://sync.example.org";

function operandsFor(
  overrides: Partial<LifecycleEventOperands> = {},
): LifecycleEventOperands {
  return createLifecycleEventOperands({
    operation: "rename",
    sourceId: SOURCE_ID,
    expectedVersionId: VERSION_ID,
    expectedLocator: "folder/note.md",
    targetLocator: "folder/note-renamed.md",
    tombstoneId: null,
    policyRevision: 7,
    predecessorEventId: null,
    capturedFingerprintSha256: null,
    capturedFingerprintSizeBytes: null,
    capturedFingerprintMediaType: null,
    ...overrides,
  });
}

function frozenEventFor(operands: LifecycleEventOperands): FrozenLifecycleEvent {
  return {
    event: {
      eventId: EVENT_ID,
      localFileId: "local-file-1",
      idempotencyKey: IDEMPOTENCY_KEY,
      operation: operands.operation,
      fingerprint: {
        sha256: "0".repeat(64),
        sizeBytes: 0,
        mediaType: "application/octet-stream",
      },
      state: "queued",
      attemptCount: 0,
      nextEligibleRetryEpochMs: null,
      safeError: null,
      operationId: null,
    },
    operands,
  };
}

interface RecordedRequest {
  readonly url: string;
  readonly method: string;
  readonly headers: Record<string, string>;
  readonly bodyText: string;
  readonly signal: AbortSignal | null;
}

interface ScriptedTransport {
  readonly requests: RecordedRequest[];
  install: (handler: (request: Request) => Promise<Response>) => void;
  readonly fetch: ApiTransport;
}

function createScriptedTransport(): ScriptedTransport {
  const requests: RecordedRequest[] = [];
  let handler: ((request: Request) => Promise<Response>) | null = null;
  const fetchFn: ApiTransport = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    const headers: Record<string, string> = {};
    for (const [name, value] of request.headers.entries()) {
      headers[name] = value;
    }
    const bodyText = await request.text();
    requests.push({
      url: request.url,
      method: request.method,
      headers,
      bodyText,
      signal: request.signal,
    });
    if (handler === null) {
      throw new Error("transport handler not installed");
    }
    return handler(request);
  }) as ApiTransport;
  return {
    requests,
    install: (next) => {
      handler = next;
    },
    fetch: fetchFn,
  };
}

interface Harness {
  readonly api: ReturnType<typeof createLifecycleApi>;
  readonly transport: ScriptedTransport;
  readonly requests: readonly RecordedRequest[];
}

function createHarness(options?: { readonly accessToken?: string | null }): Harness {
  const transport = createScriptedTransport();
  const apiClient: ApiClient = createApiClient({
    baseUrl: BASE_URL,
    transport: transport.fetch,
  });
  const explicitToken = options?.accessToken;
  const apiOptions: LifecycleApiOptions = {
    apiClient,
    resolveAccessToken: () => (explicitToken === undefined ? ACCESS_TOKEN : explicitToken),
  };
  return {
    api: createLifecycleApi(apiOptions),
    transport,
    requests: transport.requests,
  };
}

function successEnvelope(data: Record<string, unknown>): Response {
  return new Response(
    JSON.stringify({
      data,
      error: null,
      request_id: REQUEST_ID,
      warnings: [],
    }),
    {
      status: 200,
      headers: { "content-type": "application/json", "x-request-id": REQUEST_ID },
    },
  );
}

function errorEnvelope(status: number, code: string, retryable: boolean): Response {
  return new Response(
    JSON.stringify({
      data: null,
      error: {
        code,
        message: "registered safe message",
        details: {},
        retryable,
      },
      request_id: REQUEST_ID,
      warnings: [],
    }),
    {
      status,
      headers: { "content-type": "application/json" },
    },
  );
}

describe("lifecycle-api generated-client path and authentication", () => {
  it("uses the openapi-generated POST /api/sources/lifecycle-events path with bearer auth", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      successEnvelope({
        committed_at: "2026-08-20T00:00:00Z",
        event_id: EVENT_ID,
        event_sequence: 1,
        resulting_locator: "folder/note-renamed.md",
        source_id: SOURCE_ID,
        source_version_id: NEW_VERSION_ID,
        state: "active",
        tombstone_id: null,
      }),
    );
    const controller = new AbortController();
    const result = await harness.api.commit(frozenEventFor(operandsFor()), controller.signal);

    expect(result.eventId).toBe(EVENT_ID);
    expect(harness.requests).toHaveLength(1);
    const request = harness.requests[0];
    expect(request?.url).toBe(`${BASE_URL}/api/sources/lifecycle-events`);
    expect(request?.method).toBe("POST");
    expect(request?.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(request?.headers["content-type"]).toBe("application/json");
    expect(request?.headers["accept"]).toBe("application/json");
    expect(request?.signal).not.toBeNull();
  });

  it("ships the closed wire body and forbids workspace / device identifiers", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      successEnvelope({
        committed_at: "2026-08-20T00:00:00Z",
        event_id: EVENT_ID,
        event_sequence: 1,
        resulting_locator: "folder/note-renamed.md",
        source_id: SOURCE_ID,
        source_version_id: NEW_VERSION_ID,
        state: "active",
        tombstone_id: null,
      }),
    );
    await harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal);

    const body = JSON.parse(harness.requests[0]?.bodyText ?? "{}");
    expect(body).toEqual({
      event_id: EVENT_ID,
      idempotency_key: IDEMPOTENCY_KEY,
      source_id: SOURCE_ID,
      operation: "rename",
      expected_version_id: VERSION_ID,
      expected_locator: "folder/note.md",
      target_locator: "folder/note-renamed.md",
      tombstone_id: null,
      policy_revision: 7,
      client_timestamp: expect.any(String),
    });
    for (const forbidden of ["workspace_id", "device_id", "user_id", "client_instance_id"]) {
      expect(body).not.toHaveProperty(forbidden);
    }
  });

  it("supports restore with tombstone_id in the wire body and no expected_locator", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      successEnvelope({
        committed_at: "2026-08-20T00:00:00Z",
        event_id: EVENT_ID,
        event_sequence: 2,
        resulting_locator: "folder/note.md",
        source_id: SOURCE_ID,
        source_version_id: NEW_VERSION_ID,
        state: "active",
        tombstone_id: RESULT_TOMBSTONE_ID,
      }),
    );
    const operands = operandsFor({
      operation: "restore",
      expectedLocator: null,
      targetLocator: "folder/note.md",
      tombstoneId: RESULT_TOMBSTONE_ID,
      predecessorEventId: PREVIOUS_EVENT_ID,
    });
    await harness.api.commit(frozenEventFor(operands), new AbortController().signal);
    const body = JSON.parse(harness.requests[0]?.bodyText ?? "{}");
    expect(body.operation).toBe("restore");
    expect(body.tombstone_id).toBe(RESULT_TOMBSTONE_ID);
    expect(body.expected_locator).toBeNull();
    expect(body.target_locator).toBe("folder/note.md");
  });

  it("propagates AbortSignal into the request and maps an aborted request onto network_offline", async () => {
    const harness = createHarness();
    harness.transport.install(async () => {
      throw new DOMException("The request was aborted", "AbortError");
    });
    const controller = new AbortController();
    controller.abort();
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), controller.signal),
    ).rejects.toMatchObject({ kind: "network_offline" });
    expect(harness.requests[0]?.signal?.aborted).toBe(true);
  });

  it("rejects an unauthenticated call before the request runs", async () => {
    const harness = createHarness({ accessToken: null });
    harness.transport.install(async () => {
      throw new Error("transport must not run");
    });
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toBeInstanceOf(LifecycleApiError);
    expect(harness.requests).toHaveLength(0);
  });
});

describe("lifecycle-api response mapping", () => {
  it("maps a 200 success envelope to a typed LifecycleResult (rename -> active)", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      successEnvelope({
        committed_at: "2026-08-20T00:00:00Z",
        event_id: EVENT_ID,
        event_sequence: 7,
        resulting_locator: "folder/note-renamed.md",
        source_id: SOURCE_ID,
        source_version_id: NEW_VERSION_ID,
        state: "active",
        tombstone_id: null,
      }),
    );
    const result: LifecycleResult = await harness.api.commit(
      frozenEventFor(operandsFor()),
      new AbortController().signal,
    );
    expect(result).toEqual({
      committedAt: "2026-08-20T00:00:00Z",
      eventId: EVENT_ID,
      eventSequence: 7,
      resultingLocator: "folder/note-renamed.md",
      sourceId: SOURCE_ID,
      sourceVersionId: NEW_VERSION_ID,
      state: "active",
      tombstoneId: null,
    });
  });

  it("maps a 200 success envelope for a delete to state=deleted with tombstone_id", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      successEnvelope({
        committed_at: "2026-08-20T00:00:00Z",
        event_id: EVENT_ID,
        event_sequence: 9,
        resulting_locator: null,
        source_id: SOURCE_ID,
        source_version_id: NEW_VERSION_ID,
        state: "deleted",
        tombstone_id: RESULT_TOMBSTONE_ID,
      }),
    );
    const operands = operandsFor({
      operation: "delete",
      expectedLocator: "folder/note.md",
      targetLocator: null,
      tombstoneId: RESULT_TOMBSTONE_ID,
    });
    const result = await harness.api.commit(
      frozenEventFor(operands),
      new AbortController().signal,
    );
    expect(result.state).toBe("deleted");
    expect(result.tombstoneId).toBe(RESULT_TOMBSTONE_ID);
    expect(result.resultingLocator).toBeNull();
  });

  it("throws a typed conflict error on a 409 envelope", async () => {
    const harness = createHarness();
    harness.transport.install(async () => errorEnvelope(409, "source_lifecycle_conflict", false));
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "conflict" });
  });

  it("throws a typed integrity error on a 422 envelope", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      errorEnvelope(422, "source_lifecycle_integrity_failed", false),
    );
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "integrity" });
  });

  it("throws a typed rate-limited error on a 429 envelope", async () => {
    const harness = createHarness();
    harness.transport.install(async () => errorEnvelope(429, "rate_limited", true));
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "network_rate_limited" });
  });

  it("throws a typed server_error on a 5xx envelope", async () => {
    const harness = createHarness();
    harness.transport.install(async () => errorEnvelope(503, "service_unavailable", true));
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "server_error" });
  });

  it("throws a typed integrity_5xx error on a 5xx integrity envelope (task 9 fix round 1 I2)", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      errorEnvelope(500, "source_commit_outcome_unknown", false),
    );
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "integrity_5xx" });
  });

  it("throws a typed integrity_5xx error on a 503 with an integrity code", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      errorEnvelope(503, "source_idempotency_mismatch", false),
    );
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "integrity_5xx" });
  });

  it("falls back to server_error on a 5xx without an integrity code", async () => {
    const harness = createHarness();
    harness.transport.install(async () => errorEnvelope(502, "internal_error", true));
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "server_error" });
  });

  it("falls back to server_error on a 5xx with a non-record body", async () => {
    const harness = createHarness();
    harness.transport.install(async () => new Response("not-json", { status: 503 }));
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "server_error" });
  });

  it("throws a typed login_required error on a 401 envelope", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      errorEnvelope(401, "access_token_expired", false),
    );
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "login_required" });
  });

  it("throws a typed network_offline error when the transport fails", async () => {
    const harness = createHarness();
    harness.transport.install(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "network_offline" });
  });

  it("throws a typed server_error on a malformed JSON body", async () => {
    const harness = createHarness();
    harness.transport.install(async () =>
      new Response("not-json", {
        status: 502,
        headers: { "content-type": "text/plain" },
      }),
    );
    await expect(
      harness.api.commit(frozenEventFor(operandsFor()), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "server_error" });
  });
});
