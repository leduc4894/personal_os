/**
 * The immutable content identity of one captured file version (spec 6.3, 7.1).
 *
 * A fingerprint is derived from exactly the observed bytes through the
 * browser-compatible WebCrypto SHA-256 (the same `sha256Hex` helper the
 * generation protocol uses): an exact lowercase digest, the exact byte size,
 * and one media type from a closed content-sniffing table with
 * `application/octet-stream` as the fail-closed fallback (spec 10.1: a
 * validated media type or `application/octet-stream`). Nothing here touches
 * paths, policy state or I/O — the value is pure, so it can be frozen into a
 * journal event the moment preflight starts (spec 7.2).
 *
 * Privacy (spec 9): the digest stays inside the local journal; it is never
 * emitted through diagnostics.
 */

import { sha256Hex } from "../exclusion-policy/canonical-json";
import { isCanonicalMediaType } from "../exclusion-policy/evaluator";
import type { FrozenFingerprint } from "./contracts";

/** The closed fail-closed media type when content sniffing cannot decide. */
export const FALLBACK_MEDIA_TYPE = "application/octet-stream";

/** Exact lowercase SHA-256 hex digests are the only accepted digests. */
export const FROZEN_FINGERPRINT_SHA256_PATTERN = /^[0-9a-f]{64}$/;

// --- closed content sniffing (spec 7.1, 10.1) --------------------------------------------

/** Compare `length` bytes of `content` against an ASCII literal at `offset`. */
function hasAsciiPrefix(content: Uint8Array, offset: number, literal: string): boolean {
  if (content.byteLength < offset + literal.length) {
    return false;
  }
  for (let index = 0; index < literal.length; index += 1) {
    if (content[offset + index] !== literal.charCodeAt(index)) {
      return false;
    }
  }
  return true;
}

/** Whether the bytes decode as strict UTF-8 (no replacement tolerance). */
function isStrictUtf8(content: Uint8Array): boolean {
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(content);
    return true;
  } catch {
    return false;
  }
}

/**
 * Derive one closed media type from the content bytes alone: the exact magic
 * signatures of vault-typical binary assets, then strict UTF-8 text, then
 * the fail-closed `application/octet-stream` fallback.
 */
export function sniffMediaType(content: Uint8Array): string {
  if (hasAsciiPrefix(content, 0, "\u0089PNG\r\n\u001a\n")) {
    return "image/png";
  }
  if (hasAsciiPrefix(content, 0, "\u00ff\u00d8\u00ff")) {
    return "image/jpeg";
  }
  if (hasAsciiPrefix(content, 0, "GIF87a") || hasAsciiPrefix(content, 0, "GIF89a")) {
    return "image/gif";
  }
  if (hasAsciiPrefix(content, 0, "RIFF") && hasAsciiPrefix(content, 8, "WEBP")) {
    return "image/webp";
  }
  if (hasAsciiPrefix(content, 0, "%PDF-")) {
    return "application/pdf";
  }
  if (hasAsciiPrefix(content, 4, "ftyp")) {
    return "video/mp4";
  }
  if (isStrictUtf8(content)) {
    return "text/plain";
  }
  return FALLBACK_MEDIA_TYPE;
}

// --- derivation and validation ------------------------------------------------------------

/**
 * Derive the frozen fingerprint of exactly these observed bytes: the exact
 * lowercase SHA-256 digest, the exact byte size and the sniffed media type
 * (spec 7.1 — the fingerprint is calculated before policy evaluation).
 */
export async function deriveFrozenFingerprint(contentBytes: Uint8Array): Promise<FrozenFingerprint> {
  return {
    sha256: await sha256Hex(contentBytes),
    sizeBytes: contentBytes.byteLength,
    mediaType: sniffMediaType(contentBytes),
  };
}

/**
 * Whether one value has the exact closed fingerprint shape: a 64-character
 * lowercase hex digest, a non-negative integer byte size and a canonical
 * media type. Anything else is rejected before it can enter a journal row.
 */
export function isFrozenFingerprintShape(value: FrozenFingerprint): boolean {
  return (
    typeof value.sha256 === "string" &&
    FROZEN_FINGERPRINT_SHA256_PATTERN.test(value.sha256) &&
    typeof value.sizeBytes === "number" &&
    Number.isInteger(value.sizeBytes) &&
    value.sizeBytes >= 0 &&
    typeof value.mediaType === "string" &&
    isCanonicalMediaType(value.mediaType)
  );
}
