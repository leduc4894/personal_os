import { afterEach, describe, expect, it, vi } from "vitest";
import type { RequestUrlParam, RequestUrlResponse } from "obsidian";

import { createRequestUrlTransport } from "./request-url-transport";
import type { RequestUrlFunction } from "./request-url-transport";

function responseWithBytes(
  bytes: number[],
  headers: Record<string, string> = {},
): RequestUrlResponse {
  return {
    status: 206,
    headers,
    arrayBuffer: new Uint8Array(bytes).buffer,
    json: undefined,
    text: "",
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("createRequestUrlTransport", () => {
  it("adapts Request to requestUrl and preserves response bytes", async () => {
    const calls: RequestUrlParam[] = [];
    const requestUrlFunction: RequestUrlFunction = async (request) => {
      calls.push(request);
      return {
        status: 206,
        headers: { "content-type": "application/octet-stream" },
        arrayBuffer: new Uint8Array([4, 5, 6]).buffer,
        json: undefined,
        text: "",
      };
    };
    const transport = createRequestUrlTransport(requestUrlFunction);
    const response = await transport("https://api.invalid/api/object", {
      method: "PUT",
      headers: { "content-type": "application/octet-stream", "x-contract": "safe" },
      body: new Uint8Array([1, 2, 3]),
    });
    expect(calls[0]).toMatchObject({
      url: "https://api.invalid/api/object",
      method: "PUT",
      throw: false,
    });
    expect(new Uint8Array(calls[0]?.body as ArrayBuffer)).toEqual(
      new Uint8Array([1, 2, 3]),
    );
    expect(response.status).toBe(206);
    expect([...new Uint8Array(await response.arrayBuffer())]).toEqual([4, 5, 6]);
  });

  it("rejects an already-aborted request before requestUrl dispatch", async () => {
    const controller = new AbortController();
    controller.abort();
    const requestUrlFunction = vi.fn<RequestUrlFunction>();
    const transport = createRequestUrlTransport(requestUrlFunction);
    await expect(
      transport("https://api.invalid/api/health/live", { signal: controller.signal }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(requestUrlFunction).not.toHaveBeenCalled();
  });

  it("omits the body for GET and HEAD requests", async () => {
    const calls: RequestUrlParam[] = [];
    const requestUrlFunction: RequestUrlFunction = async (request) => {
      calls.push(request);
      return responseWithBytes([]);
    };
    const transport = createRequestUrlTransport(requestUrlFunction);
    await transport("https://api.invalid/api/object", { method: "GET" });
    await transport("https://api.invalid/api/object", { method: "HEAD" });
    expect(calls[0]?.body).toBeUndefined();
    expect(calls[1]?.body).toBeUndefined();
  });

  it("joins duplicate request header values deterministically", async () => {
    const calls: RequestUrlParam[] = [];
    const requestUrlFunction: RequestUrlFunction = async (request) => {
      calls.push(request);
      return responseWithBytes([]);
    };
    const transport = createRequestUrlTransport(requestUrlFunction);
    await transport("https://api.invalid/api/object", {
      headers: [
        ["x-duplicate", "first"],
        ["x-single", "safe-value"],
        ["x-duplicate", "second"],
      ],
    });
    expect(calls[0]?.headers).toEqual({
      "x-duplicate": "first, second",
      "x-single": "safe-value",
    });
  });

  it("preserves response status and headers", async () => {
    const requestUrlFunction: RequestUrlFunction = async () => ({
      status: 503,
      headers: {
        "content-type": "application/json",
        "x-request-id": "request-17",
      },
      arrayBuffer: new Uint8Array([7, 8]).buffer,
      json: undefined,
      text: "",
    });
    const transport = createRequestUrlTransport(requestUrlFunction);
    const response = await transport("https://api.invalid/api/object");
    expect(response.status).toBe(503);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(response.headers.get("x-request-id")).toBe("request-17");
    expect([...new Uint8Array(await response.arrayBuffer())]).toEqual([7, 8]);
  });

  it("never logs request or response content", async () => {
    const consoleSpies = [
      vi.spyOn(console, "log"),
      vi.spyOn(console, "info"),
      vi.spyOn(console, "debug"),
      vi.spyOn(console, "warn"),
      vi.spyOn(console, "error"),
    ];
    const requestUrlFunction: RequestUrlFunction = async () =>
      responseWithBytes([9, 9, 9], { "content-type": "application/octet-stream" });
    const transport = createRequestUrlTransport(requestUrlFunction);
    const response = await transport("https://api.invalid/api/object", {
      method: "POST",
      headers: { "x-contract": "secret-header-value" },
      body: "request-body-content",
    });
    await response.arrayBuffer();
    for (const spy of consoleSpies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });
});
