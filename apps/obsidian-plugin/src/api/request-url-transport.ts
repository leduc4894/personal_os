import type { RequestUrlParam, RequestUrlResponse } from "obsidian";
import type { ApiTransport } from "@workspace/api-client";
import type { PolicyHttpTransport } from "../exclusion-policy/contracts";
import type { SyncHttpTransport } from "../journal/sync-api";

export type RequestUrlFunction = (
  request: RequestUrlParam,
) => Promise<RequestUrlResponse>;

// Obsidian always populates arrayBuffer (even as zero bytes), but the
// Response constructor rejects any non-null body for these statuses.
const NULL_BODY_STATUS_CODES = new Set([204, 205, 304]);

/**
 * Pure adapter from the fetch-shaped `ApiTransport` to Obsidian's
 * `requestUrl`. It imports Obsidian types only, so Vitest can exercise it
 * with an injected `RequestUrlFunction` without loading the Obsidian
 * runtime module.
 *
 * An in-flight abort cannot cancel Obsidian's underlying `requestUrl`;
 * operation code must bound concurrency and discard late results after
 * their deadlines. This adapter adds no automatic retry.
 */
export function createRequestUrlTransport(
  requestUrlFunction: RequestUrlFunction,
): ApiTransport {
  return async (input, init) => {
    const request = new Request(input, init);
    if (request.signal.aborted) {
      throw new DOMException("The request was aborted", "AbortError");
    }
    const headers: Record<string, string> = {};
    for (const [name, value] of request.headers.entries()) {
      const previousValue = headers[name];
      // Duplicate header names arrive as separate entries; join them the
      // way the Headers API combines values so the result is deterministic.
      headers[name] = previousValue === undefined ? value : `${previousValue}, ${value}`;
    }
    const param: RequestUrlParam = {
      url: request.url,
      method: request.method,
      headers,
      throw: false,
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      param.body = await request.arrayBuffer();
    }
    const result = await requestUrlFunction(param);
    const body = NULL_BODY_STATUS_CODES.has(result.status)
      ? null
      : result.arrayBuffer;
    return new Response(body, {
      status: result.status,
      headers: result.headers,
    });
  };
}

/**
 * The pure raw-body adapter for the small-file sync endpoints (spec 10):
 * one request with an exact `ArrayBuffer` byte body in, status and response
 * text out. It adds no automatic retry — the queue driver owns every retry
 * decision — and re-checks nothing about the bytes it passes through.
 */
export function createRequestUrlSyncTransport(
  requestUrlFunction: RequestUrlFunction,
): SyncHttpTransport {
  return async (request) => {
    const param: RequestUrlParam = {
      url: request.url,
      method: request.method,
      headers: { ...request.headers },
      throw: false,
      body: request.body,
    };
    const result = await requestUrlFunction(param);
    return { status: result.status, bodyText: result.text };
  };
}

/**
 * The pure GET adapter for policy keyset/snapshot fetches: authenticated
 * headers in, status/body text/entity tag out. It adds no retry and performs
 * no body parsing — the closed parser owns the response text.
 */
export function createRequestUrlPolicyHttpTransport(
  requestUrlFunction: RequestUrlFunction,
): PolicyHttpTransport {
  return async (request) => {
    const result = await requestUrlFunction({
      url: request.url,
      method: "GET",
      headers: { ...request.headers },
      throw: false,
    });
    const headers = result.headers ?? {};
    let etag: string | null = null;
    for (const name of Object.keys(headers)) {
      if (name.toLowerCase() === "etag") {
        etag = headers[name] ?? null;
        break;
      }
    }
    return { status: result.status, bodyText: result.text, etag };
  };
}
