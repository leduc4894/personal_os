import type { RequestUrlParam, RequestUrlResponse } from "obsidian";
import type { ApiTransport } from "@workspace/api-client";

export type RequestUrlFunction = (
  request: RequestUrlParam,
) => Promise<RequestUrlResponse>;

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
    return new Response(result.arrayBuffer, {
      status: result.status,
      headers: result.headers,
    });
  };
}
