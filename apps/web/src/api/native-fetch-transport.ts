import type { ApiTransport } from "@workspace/api-client";

export function createNativeFetchTransport(
  fetchImplementation: typeof globalThis.fetch = globalThis.fetch,
): ApiTransport {
  return (input, init) => fetchImplementation(input, init);
}
