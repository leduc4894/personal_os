import { HttpResponse, type DefaultBodyType } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { createApiClient, type components } from "@workspace/api-client";

import { createNativeFetchTransport } from "./native-fetch-transport";
import { createSourceLifecycleClient } from "./source-lifecycle-client";
import { MOCK_API_BASE_URL, mockApi } from "../testing/api-mock-builders";

/**
 * The source-lifecycle client tests pin the generated-client wiring only:
 * the exact read-only diagnostics route, credentials and envelope/error
 * unwrapping through the shared helper. Rendering behavior lives in the
 * feature component tests.
 */

const server = setupServer();

type SourceLifecycleDiagnosticsData = components["schemas"]["SourceLifecycleDiagnosticsData"];

const REQUEST_ID = "5b34a3ca-8a30-4f6f-9b1e-1d2a1a1b9c31";

function dataEnvelope(data: unknown, status = 200): HttpResponse<DefaultBodyType> {
  return HttpResponse.json({ data, error: null, request_id: REQUEST_ID, warnings: [] }, { status });
}

function errorEnvelope(code: string, status: number): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(
    {
      data: null,
      error: { code, details: {}, message: `Simulated ${code} failure.`, retryable: false },
      request_id: REQUEST_ID,
      warnings: [],
    },
    { status },
  );
}

function diagnosticsData(): SourceLifecycleDiagnosticsData {
  return {
    commit_counters: [{ count: 3, operation: "rename", outcome: "committed" }],
    recent_rejections: [
      {
        at_epoch_ms: 1_750_000_000_000,
        error_code: "source_locator_conflict",
        operation: "restore",
      },
    ],
  };
}

function createTestClient() {
  return createSourceLifecycleClient({
    apiClient: createApiClient({
      baseUrl: MOCK_API_BASE_URL,
      transport: createNativeFetchTransport(globalThis.fetch),
    }),
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("createSourceLifecycleClient", () => {
  it("reads the rejection diagnostics through the generated GET route and unwraps the envelope", async () => {
    const requests: string[] = [];
    server.use(
      mockApi("get", "/api/admin/source-lifecycle/rejections", ({ request }) => {
        requests.push(`${request.method} ${new URL(request.url).pathname}`);
        return dataEnvelope(diagnosticsData());
      }),
    );
    const result = await createTestClient().getRejectionDiagnostics();
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.commit_counters[0]?.count).toBe(3);
      expect(result.data.recent_rejections[0]?.error_code).toBe("source_locator_conflict");
    }
    expect(requests).toEqual(["GET /api/admin/source-lifecycle/rejections"]);
  });

  it("maps a non-2xx error envelope onto the closed result shape", async () => {
    server.use(
      mockApi("get", "/api/admin/source-lifecycle/rejections", () =>
        errorEnvelope("authentication_required", 401),
      ),
    );
    const result = await createTestClient().getRejectionDiagnostics();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("authentication_required");
    }
  });

  it("maps a transport failure onto the closed retryable result shape", async () => {
    server.use(
      mockApi("get", "/api/admin/source-lifecycle/rejections", () => {
        throw new Error("network down");
      }),
    );
    const result = await createTestClient().getRejectionDiagnostics();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("internal_error");
      expect(result.error.retryable).toBe(true);
    }
  });
});
