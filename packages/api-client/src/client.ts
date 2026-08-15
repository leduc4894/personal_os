import createClient, { type Client } from "openapi-fetch";

import type { paths } from "./generated/schema";

export type ApiTransport = typeof globalThis.fetch;
export type ApiClient = Client<paths>;

export function createApiClient(options: {
  baseUrl: string;
  transport: ApiTransport;
}): ApiClient {
  return createClient<paths>({ baseUrl: options.baseUrl, fetch: options.transport });
}
