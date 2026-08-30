import {
  createApiClient,
  type ApiClient,
  type components,
} from "@workspace/api-client";

import { createNativeFetchTransport } from "./native-fetch-transport";

export type SessionData = components["schemas"]["SessionData"];
export type TotpEnrollmentData = components["schemas"]["TotpEnrollmentData"];
export type TotpEnrollmentOfferData = components["schemas"]["TotpEnrollmentOfferData"];
export type RecoveryCodesData = components["schemas"]["RecoveryCodesData"];
export type RecoveryLimitedContext = components["schemas"]["RecoveryLimitedContext"];
export type ApiErrorBody = components["schemas"]["ApiErrorBody"];

/** Every operation result: either the route's data payload or its registry error. */
export type AuthenticationCallResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: ApiErrorBody };

export interface AuthenticationClient {
  login(input: { username: string; password: string }): Promise<AuthenticationCallResult<SessionData>>;
  getSession(): Promise<AuthenticationCallResult<SessionData>>;
  logout(): Promise<AuthenticationCallResult<SessionData>>;
  reauthenticate(input: {
    password: string;
    totpCode?: string | undefined;
  }): Promise<AuthenticationCallResult<SessionData>>;
  changePassword(input: { newPassword: string }): Promise<AuthenticationCallResult<SessionData>>;
  verifyTotpChallenge(input: { code: string }): Promise<AuthenticationCallResult<SessionData>>;
  startTotpEnrollment(): Promise<AuthenticationCallResult<TotpEnrollmentData>>;
  dismissInitialTotpOffer(): Promise<AuthenticationCallResult<TotpEnrollmentData>>;
  verifyTotpEnrollment(input: {
    enrollmentId: string;
    code: string;
  }): Promise<AuthenticationCallResult<RecoveryCodesData>>;
  startTotpRecovery(input: {
    password: string;
    recoveryCode: string;
  }): Promise<AuthenticationCallResult<RecoveryLimitedContext>>;
  regenerateTotpRecoveryCodes(input: {
    password: string;
    totpCode: string;
  }): Promise<AuthenticationCallResult<RecoveryCodesData>>;
  disableTotp(input: { password: string; totpCode: string }): Promise<AuthenticationCallResult<SessionData>>;
}

/**
 * The API sets the production ``__Host-`` CSRF cookie and a plain loopback
 * name for local development; both are read at request time only.
 */
export const CSRF_COOKIE_NAMES = ["__Host-admin_csrf", "admin_csrf_local"] as const;

export const CSRF_HEADER_NAME = "x-csrf-token";

/** Parses the CSRF token out of a ``document.cookie`` snapshot. */
export function readCsrfTokenFromCookieSource(cookieSource: string): string | null {
  for (const cookieName of CSRF_COOKIE_NAMES) {
    const match = cookieSource.match(new RegExp(`(?:^|;\\s*)${escapeRegExp(cookieName)}=([^;]*)`));
    if (match) {
      return decodeURIComponent(match[1] ?? "");
    }
  }
  return null;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface ApiEnvelope<T> {
  readonly data: T | null;
  readonly error: ApiErrorBody | null;
}

/**
 * The safe generic body every transport-closed call returns when the API
 * cannot be reached or answers outside the envelope contract.
 */
export const REQUEST_UNAVAILABLE_ERROR: ApiErrorBody = {
  code: "internal_error",
  details: {},
  message: "The request could not be completed. Check your connection and try again.",
  retryable: true,
};

/** Unwraps an API envelope into a call result; shared by every client surface. */
export function unwrapEnvelope<T>(
  payload: { data?: unknown; error?: unknown },
): AuthenticationCallResult<T> {
  const envelope = (payload.data ?? payload.error ?? null) as ApiEnvelope<T> | null;
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

export function createAuthenticationClient(options: {
  apiClient: ApiClient;
  /** Reads the CSRF cookie at request time; never persisted between requests. */
  readCsrfToken: () => string | null;
}): AuthenticationClient {
  const { apiClient, readCsrfToken } = options;

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
    login({ username, password }) {
      return call(() =>
        apiClient.POST("/api/auth/login", {
          body: { username, password },
          credentials: "include",
        }),
      );
    },
    getSession() {
      return call(() => apiClient.GET("/api/auth/session", { credentials: "include" }));
    },
    logout() {
      return call(() =>
        apiClient.POST("/api/auth/logout", { credentials: "include", headers: csrfHeaders() }),
      );
    },
    reauthenticate({ password, totpCode }) {
      return call(() =>
        apiClient.POST("/api/auth/reauthenticate", {
          body: { password, totp_code: totpCode ?? null },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    changePassword({ newPassword }) {
      return call(() =>
        apiClient.PUT("/api/auth/password", {
          body: { new_password: newPassword },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    verifyTotpChallenge({ code }) {
      return call(() =>
        apiClient.POST("/api/auth/totp/verify", {
          body: { code },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    startTotpEnrollment() {
      return call(() =>
        apiClient.POST("/api/auth/totp/enrollments", {
          body: { action: "start" },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    dismissInitialTotpOffer() {
      return call(() =>
        apiClient.POST("/api/auth/totp/enrollments", {
          body: { action: "dismiss_initial_offer" },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    verifyTotpEnrollment({ enrollmentId, code }) {
      return call(() =>
        apiClient.POST("/api/auth/totp/enrollments/{enrollment_id}/verify", {
          body: { code },
          params: { path: { enrollment_id: enrollmentId } },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    startTotpRecovery({ password, recoveryCode }) {
      return call(() =>
        apiClient.POST("/api/auth/totp/recovery", {
          body: { password, recovery_code: recoveryCode },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    regenerateTotpRecoveryCodes({ password, totpCode }) {
      return call(() =>
        apiClient.POST("/api/auth/totp/recovery-codes/regenerate", {
          body: { password, totp_code: totpCode },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
    disableTotp({ password, totpCode }) {
      return call(() =>
        apiClient.DELETE("/api/auth/totp", {
          body: { password, totp_code: totpCode },
          credentials: "include",
          headers: csrfHeaders(),
        }),
      );
    },
  };
}

const BROWSER_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

let cachedBrowserClient: AuthenticationClient | null = null;

/**
 * Builds the client the browser pages use: same-origin by default. The
 * instance is memoized so React default props keep one stable identity —
 * effects keyed on the client must not re-fire on every render.
 */
export function createBrowserAuthenticationClient(): AuthenticationClient {
  cachedBrowserClient ??= createAuthenticationClient({
    apiClient: createApiClient({
      baseUrl: BROWSER_API_BASE_URL,
      transport: createNativeFetchTransport(),
    }),
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
  return cachedBrowserClient;
}
