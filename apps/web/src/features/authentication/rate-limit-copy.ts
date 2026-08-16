import type { ApiErrorBody } from "../../api/authentication-client";

/**
 * Bounded retry guidance for throttled authentication surfaces (spec 18.1).
 * Reads only the registered safe detail ``retry_after_seconds`` (spec 17 /
 * ``authentication_rate_limited``); every other detail stays unechoed.
 */
export function rateLimitedRetryMessage(error: ApiErrorBody): string | null {
  if (error.code !== "authentication_rate_limited") {
    return null;
  }
  const retryAfterSeconds = error.details["retry_after_seconds"];
  if (typeof retryAfterSeconds !== "number" || !Number.isFinite(retryAfterSeconds) || retryAfterSeconds <= 0) {
    return "Too many attempts. Try again shortly.";
  }
  const retryAfterMinutes = Math.max(1, Math.ceil(retryAfterSeconds / 60));
  return `Too many attempts. Try again in ${retryAfterMinutes} minute${retryAfterMinutes === 1 ? "" : "s"}.`;
}
