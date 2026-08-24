import { HttpResponse, type DefaultBodyType } from "msw";
import { setupServer } from "msw/node";
import { afterEach, beforeAll, afterAll, describe, expect, it } from "vitest";

import { createApiClient, type components } from "@workspace/api-client";

import { createNativeFetchTransport } from "./native-fetch-transport";
import { readCsrfTokenFromCookieSource } from "./authentication-client";
import {
  CSRF_COOKIE_VALUE,
  MOCK_API_BASE_URL,
  installMockCsrfCookie,
  mockApi,
} from "../testing/api-mock-builders";
import { createExclusionPolicyClient } from "./exclusion-policy-client";

/**
 * The exclusion-policy client tests pin the generated-client wiring only:
 * exact routes, credentials, the CSRF double-submit header on writes, the
 * dedicated publication idempotency header and envelope/error unwrapping.
 * Grammar and gating behavior live in the feature component tests.
 */

const server = setupServer();

type PolicyStatusData = components["schemas"]["ExclusionPolicyStatusData"];
type PolicyPreviewData = components["schemas"]["PolicyPreviewData"];
type PolicyPublicationData = components["schemas"]["PolicyPublicationData"];
type PolicyDraftData = components["schemas"]["PolicyDraftData"];

const REQUEST_ID = "5b34a3ca-8a30-4f6f-9b1e-1d2a1a1b9c30";

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

function statusData(): PolicyStatusData {
  return {
    active_policy_revision_id: null,
    active_revision_number: 0,
    draft: {
      base_policy_revision_id: null,
      draft_id: "0f9e8d7c-6b5a-4c3d-2e1f-0a1b2c3d4e5f",
      draft_version: 4,
      rules: [],
    },
    reconciliation: null,
    stale_running_previews: null,
  };
}

function previewData(state: PolicyPreviewData["status"]): PolicyPreviewData {
  return {
    base_policy_revision_id: null,
    consumed_at: null,
    counters: {
      indeterminate_count: 0,
      newly_allowed_count: 0,
      newly_excluded_count: 0,
      still_allowed_count: 0,
      still_excluded_count: 0,
    },
    created_at: "2026-08-17T08:00:00Z",
    draft_sha256: "a".repeat(64),
    draft_version: 4,
    expires_at: null,
    impact_digest: null,
    policy_draft_id: "0f9e8d7c-6b5a-4c3d-2e1f-0a1b2c3d4e5f",
    policy_preview_id: "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    ready_at: null,
    results: null,
    safe_error_code: null,
    source_checkpoint_event_sequence: 12,
    status: state,
  };
}

function publicationData(isReplay: boolean): PolicyPublicationData {
  return {
    is_replay: isReplay,
    parent_policy_revision_id: null,
    payload_sha256: "b".repeat(64),
    policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
    published_at: "2026-08-17T08:05:00Z",
    reconciliation_status: "pending",
    revision_number: 1,
    rule_count: 0,
    signing_key_id: "key-current",
    workspace_id: "9a8b7c6d-5e4f-4a3b-2c1d-0e9f8a7b6c5d",
  };
}

function createTestClient() {
  return createExclusionPolicyClient({
    apiClient: createApiClient({
      baseUrl: MOCK_API_BASE_URL,
      transport: createNativeFetchTransport(globalThis.fetch),
    }),
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("createExclusionPolicyClient", () => {
  it("reads the Admin status through the generated GET route and unwraps the envelope", async () => {
    const requests: string[] = [];
    server.use(
      mockApi("get", "/api/admin/exclusion-policy", ({ request }) => {
        requests.push(`${request.method} ${new URL(request.url).pathname}`);
        return dataEnvelope(statusData());
      }),
    );
    const result = await createTestClient().getExclusionPolicyStatus();
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.draft.draft_version).toBe(4);
    }
    expect(requests).toEqual(["GET /api/admin/exclusion-policy"]);
  });

  it("replaces the draft with CSRF protection and the exact expected version body", async () => {
    installMockCsrfCookie();
    const observed: { csrf: string | null; body: Record<string, unknown> }[] = [];
    server.use(
      mockApi("put", "/api/admin/exclusion-policy/draft", async ({ request }) => {
        observed.push({
          csrf: request.headers.get("x-csrf-token"),
          body: (await request.json()) as Record<string, unknown>,
        });
        const draft: PolicyDraftData = {
          base_policy_revision_id: null,
          draft_id: "0f9e8d7c-6b5a-4c3d-2e1f-0a1b2c3d4e5f",
          draft_version: 5,
          rules: [],
        };
        return dataEnvelope(draft);
      }),
    );
    const result = await createTestClient().replaceExclusionPolicyDraft({
      expectedDraftVersion: 4,
      rules: [
        {
          rule_id: "3b4c5d6e-7f8a-4b9c-0d1e-2f3a4b5c6d7e",
          rule_kind: "folder_prefix",
          folder_prefix: "private",
        },
      ],
    });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.draft_version).toBe(5);
    }
    expect(observed).toHaveLength(1);
    expect(observed[0]?.csrf).toBe(CSRF_COOKIE_VALUE);
    expect(observed[0]?.body).toEqual({
      expected_draft_version: 4,
      rules: [
        {
          rule_id: "3b4c5d6e-7f8a-4b9c-0d1e-2f3a4b5c6d7e",
          rule_kind: "folder_prefix",
          folder_prefix: "private",
        },
      ],
    });
    document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  });

  it("creates a preview with CSRF protection and returns the 202 envelope payload", async () => {
    installMockCsrfCookie();
    let observedCsrf: string | null = null;
    server.use(
      mockApi("post", "/api/admin/exclusion-policy/previews", ({ request }) => {
        observedCsrf = request.headers.get("x-csrf-token");
        return dataEnvelope(previewData("pending"), 202);
      }),
    );
    const result = await createTestClient().createExclusionPolicyPreview();
    expect(observedCsrf).toBe(CSRF_COOKIE_VALUE);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.status).toBe("pending");
    }
    document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  });

  it("reads a preview page and forwards the stable cursor as query parameters", async () => {
    let observedQuery: string | null = null;
    server.use(
      mockApi("get", "/api/admin/exclusion-policy/previews/1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d", ({ request }) => {
        observedQuery = new URL(request.url).search;
        return dataEnvelope(previewData("ready"));
      }),
    );
    const result = await createTestClient().getExclusionPolicyPreview({
      policyPreviewId: "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
      cursor: { impact_class: "newly_excluded", source_id: "4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a" },
    });
    expect(result.ok).toBe(true);
    expect(observedQuery).toBe(
      "?cursor_impact_class=newly_excluded&cursor_source_id=4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a",
    );
  });

  it("publishes with the dedicated idempotency header, CSRF and the exact binding body", async () => {
    installMockCsrfCookie();
    const observed: { idempotency: string | null; csrf: string | null; body: Record<string, unknown> }[] = [];
    server.use(
      mockApi("post", "/api/admin/exclusion-policy/publications", async ({ request }) => {
        observed.push({
          idempotency: request.headers.get("x-idempotency-key"),
          csrf: request.headers.get("x-csrf-token"),
          body: (await request.json()) as Record<string, unknown>,
        });
        return dataEnvelope(publicationData(observed.length > 1), observed.length === 1 ? 201 : 200);
      }),
    );
    const client = createTestClient();
    const request = {
      confirmation: "PUBLISH EXCLUSION POLICY",
      expected_active_policy_revision_id: null,
      expected_active_revision_number: 0,
      expected_draft_sha256: "a".repeat(64),
      expected_draft_version: 4,
      policy_draft_id: "0f9e8d7c-6b5a-4c3d-2e1f-0a1b2c3d4e5f",
      policy_preview_id: "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
      preview_impact_digest: "c".repeat(64),
    };
    const first = await client.publishExclusionPolicy({ request, idempotencyKey: "publish-once-001" });
    const replay = await client.publishExclusionPolicy({ request, idempotencyKey: "publish-once-001" });
    expect(first.ok && first.data.revision_number).toBe(1);
    expect(first.ok && first.data.is_replay).toBe(false);
    expect(replay.ok && replay.data.is_replay).toBe(true);
    expect(observed.map((entry) => entry.idempotency)).toEqual(["publish-once-001", "publish-once-001"]);
    expect(observed.every((entry) => entry.csrf === CSRF_COOKIE_VALUE)).toBe(true);
    expect(observed[0]?.body).toEqual(request);
    document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  });

  it("maps non-2xx envelopes and transport failures onto the closed result shape", async () => {
    server.use(
      mockApi("get", "/api/admin/exclusion-policy", () => errorEnvelope("exclusion_policy_not_initialized", 409)),
    );
    const failed = await createTestClient().getExclusionPolicyStatus();
    expect(failed.ok).toBe(false);
    if (!failed.ok) {
      expect(failed.error.code).toBe("exclusion_policy_not_initialized");
    }

    server.use(
      mockApi("get", "/api/admin/exclusion-policy", () => {
        throw new Error("network down");
      }),
    );
    const unreachable = await createTestClient().getExclusionPolicyStatus();
    expect(unreachable.ok).toBe(false);
    if (!unreachable.ok) {
      expect(unreachable.error.code).toBe("internal_error");
      expect(unreachable.error.retryable).toBe(true);
    }
  });
});
