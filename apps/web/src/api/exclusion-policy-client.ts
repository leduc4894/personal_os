import {
  createApiClient,
  type ApiClient,
  type components,
} from "@workspace/api-client";

import { createNativeFetchTransport } from "./native-fetch-transport";
import {
  CSRF_HEADER_NAME,
  createAuthenticationClient,
  readCsrfTokenFromCookieSource,
  type ApiErrorBody,
  type AuthenticationCallResult,
  type AuthenticationClient,
} from "./authentication-client";

export type ExclusionPolicyStatusData = components["schemas"]["ExclusionPolicyStatusData"];
export type PolicyDraftData = components["schemas"]["PolicyDraftData"];
export type PolicyDraftReplaceRequest = components["schemas"]["PolicyDraftReplaceRequest"];
export type PolicyPreviewData = components["schemas"]["PolicyPreviewData"];
export type PolicyPreviewCursorData = components["schemas"]["PolicyPreviewCursorData"];
export type PolicyPublicationData = components["schemas"]["PolicyPublicationData"];
export type PolicyPublicationRequest = components["schemas"]["PolicyPublicationRequest"];

/**
 * The exclusion-policy browser client: it wraps only the generated API client
 * plus the existing authenticated fetch/CSRF behavior (spec 16.1/17). Every
 * method returns the route's data payload or its registered error, never a
 * raw status, and write methods attach the CSRF double-submit header; the
 * publication route carries its dedicated idempotency key header.
 */
export interface ExclusionPolicyClient extends AuthenticationClient {
  getExclusionPolicyStatus(): Promise<AuthenticationCallResult<ExclusionPolicyStatusData>>;
  replaceExclusionPolicyDraft(input: {
    expectedDraftVersion: number;
    rules: PolicyDraftReplaceRequest["rules"];
  }): Promise<AuthenticationCallResult<PolicyDraftData>>;
  createExclusionPolicyPreview(): Promise<AuthenticationCallResult<PolicyPreviewData>>;
  getExclusionPolicyPreview(input: {
    policyPreviewId: string;
    cursor?: PolicyPreviewCursorData | null;
  }): Promise<AuthenticationCallResult<PolicyPreviewData>>;
  publishExclusionPolicy(input: {
    request: PolicyPublicationRequest;
    idempotencyKey: string;
  }): Promise<AuthenticationCallResult<PolicyPublicationData>>;
}

/** The status read the policy editor needs. */
export interface PolicyStatusReading {
  getExclusionPolicyStatus(): Promise<AuthenticationCallResult<ExclusionPolicyStatusData>>;
}

/** The explicit full-list draft replacement the policy editor needs. */
export interface PolicyDraftReplacing {
  replaceExclusionPolicyDraft(input: {
    expectedDraftVersion: number;
    rules: PolicyDraftReplaceRequest["rules"];
  }): Promise<AuthenticationCallResult<PolicyDraftData>>;
}

/** The preview lifecycle operations the policy editor needs. */
export interface PolicyPreviewLifecycleClient {
  createExclusionPolicyPreview(): Promise<AuthenticationCallResult<PolicyPreviewData>>;
  getExclusionPolicyPreview(input: {
    policyPreviewId: string;
    cursor?: PolicyPreviewCursorData | null;
  }): Promise<AuthenticationCallResult<PolicyPreviewData>>;
}

/** The publication and recent re-authentication operations the dialog needs. */
export interface PolicyPublicationTriggerClient {
  publishExclusionPolicy(input: {
    request: PolicyPublicationRequest;
    idempotencyKey: string;
  }): Promise<AuthenticationCallResult<PolicyPublicationData>>;
  reauthenticate(input: {
    password: string;
    totpCode?: string | undefined;
  }): Promise<AuthenticationCallResult<components["schemas"]["SessionData"]>>;
}

/** The complete client surface the Admin policy page consumes. */
export type PolicyEditorClient = PolicyStatusReading &
  PolicyDraftReplacing &
  PolicyPreviewLifecycleClient &
  PolicyPublicationTriggerClient;

/**
 * Mirrors the authentication client's transport failure body so every client
 * surface closes identically when the API cannot be reached.
 */
const REQUEST_UNAVAILABLE_ERROR: ApiErrorBody = {
  code: "internal_error",
  details: {},
  message: "The request could not be completed. Check your connection and try again.",
  retryable: true,
};

function unwrapEnvelope<T>(payload: { data?: unknown; error?: unknown }): AuthenticationCallResult<T> {
  const envelope = (payload.data ?? payload.error ?? null) as
    | { data?: T | null; error?: ApiErrorBody | null }
    | null;
  if (
    envelope !== null &&
    typeof envelope === "object" &&
    envelope.data !== null &&
    envelope.data !== undefined
  ) {
    return { ok: true, data: envelope.data };
  }
  const error = envelope !== null && typeof envelope === "object" ? envelope.error : null;
  return { ok: false, error: error ?? REQUEST_UNAVAILABLE_ERROR };
}

export function createExclusionPolicyClient(options: {
  apiClient: ApiClient;
  /** Reads the CSRF cookie at request time; never persisted between requests. */
  readCsrfToken: () => string | null;
}): ExclusionPolicyClient {
  const { apiClient, readCsrfToken } = options;
  const authentication = createAuthenticationClient(options);

  function csrfHeaders(): Record<string, string> {
    const token = readCsrfToken();
    return token === null ? {} : { [CSRF_HEADER_NAME]: token };
  }

  async function call<T>(
    request: () => Promise<{ data?: unknown; error?: unknown }>,
  ): Promise<AuthenticationCallResult<T>> {
    try {
      return unwrapEnvelope<T>(await request());
    } catch {
      return { ok: false, error: REQUEST_UNAVAILABLE_ERROR };
    }
  }

  return {
    ...authentication,
    getExclusionPolicyStatus() {
      return call(() => apiClient.GET("/api/admin/exclusion-policy", { credentials: "include" }));
    },
    replaceExclusionPolicyDraft({ expectedDraftVersion, rules }) {
      return call(() =>
        apiClient.PUT("/api/admin/exclusion-policy/draft", {
          body: { expected_draft_version: expectedDraftVersion, rules },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    createExclusionPolicyPreview() {
      return call(() =>
        apiClient.POST("/api/admin/exclusion-policy/previews", {
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    getExclusionPolicyPreview({ policyPreviewId, cursor }) {
      return call(() =>
        apiClient.GET("/api/admin/exclusion-policy/previews/{policy_preview_id}", {
          params: {
            path: { policy_preview_id: policyPreviewId },
            ...(cursor !== null && cursor !== undefined
              ? { query: { cursor_impact_class: cursor.impact_class, cursor_source_id: cursor.source_id } }
              : {}),
          },
          credentials: "include",
        }),
      );
    },
    publishExclusionPolicy({ request, idempotencyKey }) {
      return call(() =>
        apiClient.POST("/api/admin/exclusion-policy/publications", {
          params: { header: { "X-Idempotency-Key": idempotencyKey } },
          body: request,
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
  };
}

const BROWSER_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

let cachedBrowserPolicyClient: ExclusionPolicyClient | null = null;

/**
 * Builds the client the browser policy page uses: same-origin by default and
 * memoized so React default props keep one stable identity across renders.
 */
export function createBrowserExclusionPolicyClient(): ExclusionPolicyClient {
  cachedBrowserPolicyClient ??= createExclusionPolicyClient({
    apiClient: createApiClient({
      baseUrl: BROWSER_API_BASE_URL,
      transport: createNativeFetchTransport(),
    }),
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
  return cachedBrowserPolicyClient;
}
