import { describe, expect, it } from "vitest";

import { type ApiTransport, createApiClient } from "./client";

const REQUEST_ID = "00000000-0000-4000-8000-000000000000";

describe("createApiClient", () => {
  it("passes request and response through the injected transport", async () => {
    const requests: Request[] = [];
    const transport: ApiTransport = async (input, init) => {
      requests.push(new Request(input, init));
      return Response.json(
        {
          request_id: REQUEST_ID,
          data: { status: "live", service: "api" },
          warnings: [],
          error: null,
        },
        {
          status: 200,
          headers: { "x-request-id": REQUEST_ID },
        },
      );
    };
    const client = createApiClient({ baseUrl: "https://api.invalid", transport });
    const { data, error, response } = await client.GET("/api/health/live");
    expect(error).toBeUndefined();
    expect(data?.data).toEqual({ status: "live", service: "api" });
    expect(response.headers.get("x-request-id")).toBe(REQUEST_ID);
    expect(requests[0]?.url).toBe("https://api.invalid/api/health/live");
  });
});
