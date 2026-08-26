import { requestUrl } from "obsidian";
import type { ApiTransport } from "@workspace/api-client";
import type { DeviceSyncHttpTransport } from "../device-sync/api";
import type { PolicyHttpTransport } from "../exclusion-policy/contracts";
import type { SyncHttpTransport } from "../journal/sync-api";
import {
  createRequestUrlDeviceSyncTransport,
  createRequestUrlPolicyHttpTransport,
  createRequestUrlSyncTransport,
  createRequestUrlTransport,
} from "./request-url-transport";

/**
 * The only module that imports the real Obsidian `requestUrl`. Tests target
 * the pure adapters in `./request-url-transport` with an injected function
 * instead, so these bindings stay out of Vitest entirely.
 */
export function createObsidianApiTransport(): ApiTransport {
  return createRequestUrlTransport(requestUrl);
}

/** Bind the policy keyset/snapshot GET transport to Obsidian's `requestUrl`. */
export function createObsidianPolicyHttpTransport(): PolicyHttpTransport {
  return createRequestUrlPolicyHttpTransport(requestUrl);
}

/** Bind the raw small-file sync transport to Obsidian's `requestUrl`. */
export function createObsidianSyncHttpTransport(): SyncHttpTransport {
  return createRequestUrlSyncTransport(requestUrl);
}

/**
 * Bind the binary-capable device-sync transport to Obsidian's `requestUrl`:
 * the device client rides the same adapter conventions (no retry, raw bytes
 * through) while its response seam additionally carries the exact bytes and
 * lower-cased headers the verified download verifies.
 */
export function createObsidianDeviceSyncHttpTransport(): DeviceSyncHttpTransport {
  return createRequestUrlDeviceSyncTransport(requestUrl);
}
