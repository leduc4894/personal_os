import { NextResponse, type NextRequest, type NextProxy } from "next/server";

import { buildContentSecurityPolicy, createCspNonce } from "./security-headers";

/**
 * Next.js 16 proxy (the rename of middleware): mints one fresh CSP nonce per
 * request. The CSP travels on the request headers so App Router rendering
 * stamps the same policy onto the page, and it (plus the hardening headers)
 * is set on the outgoing response.
 */
const applySecurityProxy: NextProxy = (request: NextRequest) => {
  const nonce = createCspNonce();
  const development = process.env.NODE_ENV !== "production";
  const contentSecurityPolicy = buildContentSecurityPolicy(nonce, { development });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("Content-Security-Policy", contentSecurityPolicy);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", contentSecurityPolicy);
  response.headers.set("Referrer-Policy", "no-referrer");
  response.headers.set("X-Content-Type-Options", "nosniff");
  return response;
};

export default applySecurityProxy;

export const config = {
  /**
   * Everything except the API prefix, Next's static/image assets and common
   * metadata files.
   */
  matcher: [
    "/((?!api|_next/static|_next/image|favicon\\.ico|apple-icon|icon|manifest|robots\\.txt|sitemap\\.xml|.*\\.(?:png|svg|jpg|jpeg|gif|webp|ico|txt|xml|webmanifest)$).*)",
  ],
};
