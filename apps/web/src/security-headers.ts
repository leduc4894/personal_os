/**
 * Pure builders for the Web security-header contract (spec 20.2). The proxy
 * layer calls these per request with a fresh nonce; nothing here touches I/O.
 */

const NONCE_PATTERN = /^[A-Za-z0-9_-]+$/;

/** Generates one fresh, unguessable, URL-safe nonce. */
export function createCspNonce(): string {
  const randomBytes = new Uint8Array(18);
  crypto.getRandomValues(randomBytes);
  let binary = "";
  for (const byte of randomBytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function buildContentSecurityPolicy(
  nonce: string,
  options: { development: boolean },
): string {
  if (!NONCE_PATTERN.test(nonce)) {
    throw new Error("CSP nonce must be URL-safe without quotes or semicolons.");
  }
  const scriptSrc = `'self' 'nonce-${nonce}'${options.development ? " 'unsafe-eval'" : ""}`;
  return [
    "default-src 'self'",
    `script-src ${scriptSrc}`,
    "connect-src 'self'",
    "img-src 'self' data:",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
  ].join("; ");
}

export function createSecurityHeaders(
  nonce: string,
  options: { development: boolean },
): Readonly<Record<string, string>> {
  return {
    "Content-Security-Policy": buildContentSecurityPolicy(nonce, options),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
  };
}
