import { describe, expect, it } from "vitest";

import { createNativeFetchTransport } from "./native-fetch-transport";

describe("createNativeFetchTransport", () => {
  it("preserves method headers body status and response bytes", async () => {
    const calls: Request[] = [];
    const nativeFetch: typeof fetch = async (input, init) => {
      calls.push(new Request(input, init));
      return new Response(new Uint8Array([1, 2, 3]), {
        status: 202,
        headers: { "content-type": "application/octet-stream" },
      });
    };
    const transport = createNativeFetchTransport(nativeFetch);
    const response = await transport("https://api.invalid/api/test", {
      method: "PUT",
      headers: { "x-contract": "safe-value" },
      body: "payload",
    });
    expect(calls[0]?.method).toBe("PUT");
    expect(calls[0]?.headers.get("x-contract")).toBe("safe-value");
    expect(await calls[0]?.text()).toBe("payload");
    expect(response.status).toBe(202);
    expect([...new Uint8Array(await response.arrayBuffer())]).toEqual([1, 2, 3]);
  });
});
