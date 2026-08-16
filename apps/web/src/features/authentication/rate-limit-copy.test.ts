import { describe, expect, it } from "vitest";

import type { ApiErrorBody } from "../../api/authentication-client";
import { rateLimitedRetryMessage } from "./rate-limit-copy";

function errorBody(code: string, details: Record<string, unknown> = {}): ApiErrorBody {
  return {
    code: code as ApiErrorBody["code"],
    details: details as ApiErrorBody["details"],
    message: `Simulated ${code} failure.`,
    retryable: false,
  };
}

describe("rateLimitedRetryMessage", () => {
  it("derives bounded retry copy from retry_after_seconds rounded up to minutes", () => {
    expect(rateLimitedRetryMessage(errorBody("authentication_rate_limited", { retry_after_seconds: 540 }))).toBe(
      "Too many attempts. Try again in 9 minutes.",
    );
    expect(rateLimitedRetryMessage(errorBody("authentication_rate_limited", { retry_after_seconds: 60 }))).toBe(
      "Too many attempts. Try again in 1 minute.",
    );
    expect(rateLimitedRetryMessage(errorBody("authentication_rate_limited", { retry_after_seconds: 61 }))).toBe(
      "Too many attempts. Try again in 2 minutes.",
    );
    expect(rateLimitedRetryMessage(errorBody("authentication_rate_limited", { retry_after_seconds: 1 }))).toBe(
      "Too many attempts. Try again in 1 minute.",
    );
  });

  it("falls back to guidance without a duration when the safe detail is absent or malformed", () => {
    expect(rateLimitedRetryMessage(errorBody("authentication_rate_limited"))).toBe(
      "Too many attempts. Try again shortly.",
    );
    expect(
      rateLimitedRetryMessage(errorBody("authentication_rate_limited", { retry_after_seconds: "soon" })),
    ).toBe("Too many attempts. Try again shortly.");
    expect(rateLimitedRetryMessage(errorBody("authentication_rate_limited", { retry_after_seconds: 0 }))).toBe(
      "Too many attempts. Try again shortly.",
    );
  });

  it("returns null for every other failure so callers keep their generic copy", () => {
    expect(rateLimitedRetryMessage(errorBody("authentication_failed"))).toBeNull();
    expect(
      rateLimitedRetryMessage(errorBody("authentication_failed", { retry_after_seconds: 540 })),
    ).toBeNull();
  });
});
