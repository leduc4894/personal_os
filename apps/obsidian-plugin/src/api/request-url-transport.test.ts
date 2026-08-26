import { afterEach, describe, expect, it, vi } from "vitest";
import type { RequestUrlParam, RequestUrlResponse } from "obsidian";

import {
  createRequestUrlDeviceSyncTransport,
  createRequestUrlPolicyHttpTransport,
  createRequestUrlTransport,
} from "./request-url-transport";
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

  it.each([204, 205, 304])(
    "returns an empty-bodied response for null-body status %i",
    async (status) => {
      const requestUrlFunction: RequestUrlFunction = async () => ({
        status,
        headers: {},
        arrayBuffer: new Uint8Array([]).buffer,
        json: undefined,
        text: "",
      });
      const transport = createRequestUrlTransport(requestUrlFunction);
      const response = await transport("https://api.invalid/api/object", {
        method: "DELETE",
      });
      expect(response.status).toBe(status);
      expect(response.body).toBeNull();
      expect((await response.arrayBuffer()).byteLength).toBe(0);
    },
  );

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

describe("createRequestUrlDeviceSyncTransport", () => {
  it("exposes the response bytes and lower-cased headers of a binary download", async () => {
    const calls: RequestUrlParam[] = [];
    const requestUrlFunction: RequestUrlFunction = async (request) => {
      calls.push(request);
      return {
        status: 200,
        headers: {
          "Content-Length": "38",
          "X-Content-SHA256": "a".repeat(64),
          "X-Request-ID": "77777777-7777-4777-8777-777777777777",
          "Content-Type": "text/markdown",
        },
        arrayBuffer: new Uint8Array([1, 2, 3, 4]).buffer,
        json: undefined,
        text: "",
      };
    };
    const transport = createRequestUrlDeviceSyncTransport(requestUrlFunction);
    const response = await transport({
      url: "https://vault.example.com/api/sources/a/versions/b/content",
      method: "GET",
      headers: { authorization: "Bearer at1-x", accept: "application/octet-stream" },
    });
    expect(response.status).toBe(200);
    expect(response.bodyText).toBe("");
    expect([...new Uint8Array(response.bodyBytes as ArrayBuffer)]).toEqual([1, 2, 3, 4]);
    expect(response.headers).toEqual({
      "content-length": "38",
      "x-content-sha256": "a".repeat(64),
      "x-request-id": "77777777-7777-4777-8777-777777777777",
      "content-type": "text/markdown",
    });
    expect(calls[0]?.body).toBeUndefined();
  });

  it("serves the decoded text of one JSON envelope response", async () => {
    const requestUrlFunction: RequestUrlFunction = async () => ({
      status: 409,
      headers: { "content-type": "application/json" },
      arrayBuffer: new Uint8Array([7, 8]).buffer,
      json: undefined,
      text: '{"request_id":"77777777-7777-4777-8777-777777777777","data":null}',
    });
    const transport = createRequestUrlDeviceSyncTransport(requestUrlFunction);
    const response = await transport({
      url: "https://vault.example.com/api/sync/events",
      method: "GET",
      headers: { authorization: "Bearer at1-x", accept: "application/json" },
    });
    expect(response.status).toBe(409);
    expect(response.bodyText).toContain("request_id");
    expect([...new Uint8Array(response.bodyBytes as ArrayBuffer)]).toEqual([7, 8]);
    expect(response.headers["content-type"]).toBe("application/json");
  });

  it("passes the exact byte body through and never logs content", async () => {
    const consoleSpies = [
      vi.spyOn(console, "log"),
      vi.spyOn(console, "info"),
      vi.spyOn(console, "debug"),
      vi.spyOn(console, "warn"),
      vi.spyOn(console, "error"),
    ];
    const requestUrlFunction: RequestUrlFunction = async () =>
      responseWithBytes([9, 9, 9], { "content-type": "application/octet-stream" });
    const transport = createRequestUrlDeviceSyncTransport(requestUrlFunction);
    const response = await transport({
      url: "https://vault.example.com/api/uploads/op/content",
      method: "PUT",
      headers: { authorization: "Bearer secret-token" },
      body: new Uint8Array([5, 6]).buffer,
    });
    expect(response.status).toBe(206);
    for (const spy of consoleSpies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });
});

describe("createRequestUrlPolicyHttpTransport", () => {
  it("issues GET requests with the given headers and no body", async () => {
    const calls: RequestUrlParam[] = [];
    const transport = createRequestUrlPolicyHttpTransport(async (request) => {
      calls.push(request);
      return {
        status: 200,
        headers: { etag: '"tag"' },
        arrayBuffer: new ArrayBuffer(0),
        json: undefined,
        text: "ok",
      };
    });
    const response = await transport({
      url: "https://vault.example.com/api/sync/exclusion-policy/snapshot",
      headers: { authorization: "Bearer at1-x", accept: "application/json" },
    });
    expect(response).toEqual({ status: 200, bodyText: "ok", etag: '"tag"' });
    expect(calls).toEqual([
      {
        url: "https://vault.example.com/api/sync/exclusion-policy/snapshot",
        method: "GET",
        headers: { authorization: "Bearer at1-x", accept: "application/json" },
        throw: false,
      },
    ]);
  });

  it("resolves the entity tag case-insensitively and tolerates its absence", async () => {
    const withHeader = (headers: Record<string, string>): RequestUrlFunction => {
      return async () => ({
        status: 200,
        headers,
        arrayBuffer: new ArrayBuffer(0),
        json: undefined,
        text: "",
      });
    };
    const upper = createRequestUrlPolicyHttpTransport(withHeader({ ETag: '"upper"' }));
    expect((await upper({ url: "https://x.example.com", headers: {} })).etag).toBe('"upper"');
    const lower = createRequestUrlPolicyHttpTransport(withHeader({ etag: '"lower"' }));
    expect((await lower({ url: "https://x.example.com", headers: {} })).etag).toBe('"lower"');
    const none = createRequestUrlPolicyHttpTransport(withHeader({}));
    expect((await none({ url: "https://x.example.com", headers: {} })).etag).toBeNull();
  });
});
