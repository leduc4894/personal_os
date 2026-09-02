import { HttpResponse, type DefaultBodyType, http, type RequestHandler } from "msw";

import type {
  RecoveryCodesData,
  RecoveryLimitedContext,
  SessionData,
  TotpEnrollmentData,
} from "../api/authentication-client";

/**
 * The origin MSW-intercepted API calls target during component tests. The
 * browser transport keeps cookies per-origin, so an explicit test origin keeps
 * every test hermetic.
 */
export const MOCK_API_BASE_URL = "http://api.test";

const REQUEST_ID = "5b34a3ca-8a30-4f6f-9b1e-1d2a1a1b9c10";

type EnvelopeData = SessionData | TotpEnrollmentData | RecoveryCodesData | RecoveryLimitedContext | null;

function envelope(data: EnvelopeData): Record<string, unknown> {
  return { data, error: null, request_id: REQUEST_ID, warnings: [] };
}

export function sessionData(state: SessionData["state"], overrides: Partial<SessionData> = {}): SessionData {
  return {
    absolute_expires_at: "2026-08-17T00:00:00Z",
    authenticated: state === "active",
    idle_expires_at: "2026-08-16T12:00:00Z",
    scopes: state === "active" ? ["device_administration_manage", "web_security_manage"] : [],
    state,
    ...overrides,
  };
}

export function sessionResponse(state: SessionData["state"]): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(envelope(sessionData(state)));
}

export function totpEnrollmentData(): TotpEnrollmentData {
  return {
    action: "start",
    dismissed_at: null,
    enrollment: {
      enrollment_id: "e26e0f1c-9884-4d84-a2c3-9d64a0b1f001",
      expires_at: "2026-08-16T09:10:00Z",
      provisioning_uri: "otpauth://totp/personal:owner?issuer=personal&secret=JBSWY3DPEHPK3PXP",
      secret: "JBSWY3DPEHPK3PXP",
    },
  };
}

export function totpEnrollmentResponse(): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(envelope(totpEnrollmentData()));
}

export function recoveryCodesData(): RecoveryCodesData {
  return {
    codes: ["ABCD-EFGH-IJKL", "MNOP-QRST-UVWX", "YZ23-4567-89AB"],
    revision: 3,
  };
}

export function recoveryCodesResponse(): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(envelope(recoveryCodesData()));
}

export function recoveryLimitedResponse(): HttpResponse<DefaultBodyType> {
  const context: RecoveryLimitedContext = {
    absolute_expires_at: "2026-08-16T12:00:00Z",
    idle_expires_at: "2026-08-16T09:00:00Z",
    permitted_actions: ["totp_replacement", "logout"],
    state: "recovery_limited",
  };
  return HttpResponse.json(envelope(context));
}

export function unauthenticatedResponse(): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(errorBody("authentication_required"), { status: 401 });
}

export function authenticationFailedResponse(): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(errorBody("authentication_failed"), { status: 401 });
}

export function enrollmentStateInvalidResponse(): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(errorBody("totp_enrollment_state_invalid"), { status: 409 });
}

export function recentAuthenticationRequiredResponse(): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(errorBody("recent_authentication_required"), { status: 403 });
}

export function errorBody(code: string): Record<string, unknown> {
  return {
    data: null,
    error: { code, details: {}, message: `Simulated ${code} failure.`, retryable: false },
    request_id: REQUEST_ID,
    warnings: [],
  };
}

/** The throttled exit the API renders with its registered safe retry detail. */
export function rateLimitedResponse(retryAfterSeconds: number): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(
    {
      data: null,
      error: {
        code: "authentication_rate_limited",
        details: { retry_after_seconds: retryAfterSeconds },
        message: "Simulated authentication_rate_limited failure.",
        retryable: false,
      },
      request_id: REQUEST_ID,
      warnings: [],
    },
    { status: 429 },
  );
}

export function errorResponse(code: string, status = 400): HttpResponse<DefaultBodyType> {
  return HttpResponse.json(errorBody(code), { status });
}

export const CSRF_COOKIE_VALUE = "csrf-round-trip-token";

/**
 * Registers the CSRF double-submit cookie inside the jsdom cookie jar. jsdom
 * rejects ``__Host-`` prefixed cookies over its http test origin, so tests use
 * the loopback cookie name; the production name parsing is covered by the pure
 * reader tests.
 */
export function installMockCsrfCookie(): void {
  document.cookie = `admin_csrf_local=${CSRF_COOKIE_VALUE}; path=/`;
}

type HttpHandler = typeof http.post;

/** Wraps a handler callback so unhandled JSON bodies surface parse failures. */
export function mockApi(
  method: keyof typeof http,
  path: string,
  resolver: Parameters<HttpHandler>[1],
): RequestHandler {
  const handler = http[method] as HttpHandler;
  return handler(`${MOCK_API_BASE_URL}${path}`, resolver);
}
