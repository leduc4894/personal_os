import {
  createApiClient,
  type ApiClient,
  type components,
} from "@workspace/api-client";

import { createNativeFetchTransport } from "./native-fetch-transport";
import {
  REQUEST_UNAVAILABLE_ERROR,
  unwrapEnvelope,
  type AuthenticationCallResult,
} from "./authentication-client";

export type SourceLifecycleDiagnosticsData =
  components["schemas"]["SourceLifecycleDiagnosticsData"];

/** The lifecycle rejection read the Admin lifecycle page needs. */
export interface SourceLifecycleReader {
  getRejectionDiagnostics(): Promise<AuthenticationCallResult<SourceLifecycleDiagnosticsData>>;
}

/**
 * The source-lifecycle browser client: it wraps only the generated API client
 * and reuses the shared envelope unwrapping and transport-failure closing of
 * the authentication client, so every Admin read surface closes
 * identically.
 */
export function createSourceLifecycleClient(options: {
  apiClient: ApiClient;
}): SourceLifecycleReader {
  const { apiClient } = options;
  return {
    async getRejectionDiagnostics() {
      try {
        return unwrapEnvelope<SourceLifecycleDiagnosticsData>(
          await apiClient.GET("/api/admin/source-lifecycle/rejections", {
            credentials: "include",
          }),
        );
      } catch {
        return { ok: false, error: REQUEST_UNAVAILABLE_ERROR };
      }
    },
  };
}

const BROWSER_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

let cachedBrowserClient: SourceLifecycleReader | null = null;

/**
 * Builds the client the browser lifecycle page uses: same-origin by default
 * and memoized so React default props keep one stable identity across
 * renders.
 */
export function createBrowserSourceLifecycleClient(): SourceLifecycleReader {
  cachedBrowserClient ??= createSourceLifecycleClient({
    apiClient: createApiClient({
      baseUrl: BROWSER_API_BASE_URL,
      transport: createNativeFetchTransport(),
    }),
  });
  return cachedBrowserClient;
}
