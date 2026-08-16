import { describe, expect, it } from "vitest";

import { buildContentSecurityPolicy, createCspNonce, createSecurityHeaders } from "./security-headers";

describe("buildContentSecurityPolicy", () => {
  it("builds the exact production policy around the per-response nonce", () => {
    expect(buildContentSecurityPolicy("nonce-value-1", { development: false })).toBe(
      "default-src 'self'; script-src 'self' 'nonce-nonce-value-1'; connect-src 'self'; " +
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; " +
        "form-action 'self'",
    );
  });

  it("adds only the next.js debug directive in development", () => {
    const developmentPolicy = buildContentSecurityPolicy("nonce-value-2", { development: true });
    expect(developmentPolicy).toBe(
      "default-src 'self'; script-src 'self' 'nonce-nonce-value-2' 'unsafe-eval'; connect-src 'self'; " +
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; " +
        "form-action 'self'",
    );
  });

  it("rejects nonce values that could inject policy directives", () => {
    expect(() => buildContentSecurityPolicy("a; script-src https://evil.example", { development: false })).toThrow();
    expect(() => buildContentSecurityPolicy("a'b", { development: false })).toThrow();
  });
});

describe("createSecurityHeaders", () => {
  it("returns the content security policy plus the hardening headers", () => {
    expect(createSecurityHeaders("nonce-value-3", { development: false })).toEqual({
      "Content-Security-Policy":
        "default-src 'self'; script-src 'self' 'nonce-nonce-value-3'; connect-src 'self'; " +
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; " +
        "form-action 'self'",
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
    });
  });
});

describe("createCspNonce", () => {
  it("creates unique url-safe nonces", () => {
    const first = createCspNonce();
    const second = createCspNonce();
    expect(first).toMatch(/^[A-Za-z0-9_-]{16,}$/);
    expect(second).toMatch(/^[A-Za-z0-9_-]{16,}$/);
    expect(first).not.toBe(second);
  });
});
