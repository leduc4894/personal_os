import { requestUrl } from "obsidian";
import type { ApiTransport } from "@workspace/api-client";
import { createRequestUrlTransport } from "./request-url-transport";

/**
 * The only module that imports the real Obsidian `requestUrl`. Tests target
 * the pure adapter with an injected function instead, so this binding stays
 * out of Vitest entirely.
 */
export function createObsidianApiTransport(): ApiTransport {
  return createRequestUrlTransport(requestUrl);
}
