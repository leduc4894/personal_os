/**
 * Repository-owned RFC 8785 canonical JSON encoder for signed policy bytes.
 *
 * This mirrors `src/personal_os/exclusion_policy/canonical_json.py` byte for
 * byte: members sort by the UTF-16 code units of their names, strings escape
 * only `"`, `\` and the C0 controls (short escapes where defined, lowercase
 * `\u00xx` otherwise) and emit every other code point as literal UTF-8, and
 * integers render as plain decimal. Floats, lone surrogates, non-NFC strings,
 * duplicate members and integers outside the IEEE 754 double-safe range are
 * rejected before any byte is produced. No generic JCS dependency exists; the
 * golden fixtures pin the cross-language parity.
 */

import { policyVerificationError } from "./contracts";
import type { ClosedJsonValue } from "./strict-json";

const MAXIMUM_SAFE_INTEGER = 9007199254740991;
const MINIMUM_SAFE_INTEGER = -9007199254740991;

const CONTROL_ESCAPES: Readonly<Record<number, string>> = {
  0x08: "\\b",
  0x09: "\\t",
  0x0a: "\\n",
  0x0c: "\\f",
  0x0d: "\\r",
};

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const next = index + 1 < value.length ? value.charCodeAt(index + 1) : 0;
      if (next >= 0xdc00 && next <= 0xdfff) {
        index += 1;
        continue;
      }
      return true;
    }
    if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function validateString(value: string): void {
  if (hasUnpairedSurrogate(value)) {
    throw policyVerificationError("policy_value_unsupported");
  }
  if (value.normalize("NFC") !== value) {
    throw policyVerificationError("policy_value_unsupported");
  }
}

function encodeString(value: string): string {
  let pieces = '"';
  for (const character of value) {
    if (character === '"' || character === "\\") {
      pieces += `\\${character}`;
      continue;
    }
    const codePoint = character.codePointAt(0);
    if (codePoint !== undefined && codePoint < 0x20) {
      pieces += CONTROL_ESCAPES[codePoint] ?? `\\u${codePoint.toString(16).padStart(4, "0")}`;
      continue;
    }
    pieces += character;
  }
  return `${pieces}"`;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function encodeInto(value: ClosedJsonValue, pieces: string[]): void {
  if (value === null) {
    pieces.push("null");
    return;
  }
  if (value === true) {
    pieces.push("true");
    return;
  }
  if (value === false) {
    pieces.push("false");
    return;
  }
  if (typeof value === "number") {
    if (!Number.isInteger(value) || value > MAXIMUM_SAFE_INTEGER || value < MINIMUM_SAFE_INTEGER) {
      throw policyVerificationError("policy_value_unsupported");
    }
    if (Object.is(value, -0)) {
      pieces.push("0");
      return;
    }
    pieces.push(value.toString(10));
    return;
  }
  if (typeof value === "string") {
    validateString(value);
    pieces.push(encodeString(value));
    return;
  }
  if (Array.isArray(value)) {
    pieces.push("[");
    for (let index = 0; index < value.length; index += 1) {
      if (index > 0) {
        pieces.push(",");
      }
      encodeInto(value[index], pieces);
    }
    pieces.push("]");
    return;
  }
  if (isPlainObject(value)) {
    const names = Object.keys(value);
    const seen = new Set<string>();
    for (const name of names) {
      if (seen.has(name)) {
        throw policyVerificationError("policy_value_unsupported");
      }
      seen.add(name);
    }
    // ECMAScript default string comparison IS UTF-16 code-unit order, which
    // is exactly the RFC 8785 member ordering.
    const ordered = [...names].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
    pieces.push("{");
    for (let position = 0; position < ordered.length; position += 1) {
      if (position > 0) {
        pieces.push(",");
      }
      const name = ordered[position];
      if (name === undefined) {
        throw policyVerificationError("policy_value_unsupported");
      }
      validateString(name);
      pieces.push(encodeString(name));
      pieces.push(":");
      encodeInto(value[name] as ClosedJsonValue, pieces);
    }
    pieces.push("}");
    return;
  }
  throw policyVerificationError("policy_value_unsupported");
}

/** Serialize one closed value to exact RFC 8785 canonical bytes. */
export function canonicalJsonBytes(value: ClosedJsonValue): Uint8Array {
  const pieces: string[] = [];
  encodeInto(value, pieces);
  return new TextEncoder().encode(pieces.join(""));
}

/** Serialize one closed value to the canonical JSON text form. */
export function canonicalizeClosedJson(value: ClosedJsonValue): string {
  return new TextDecoder().decode(canonicalJsonBytes(value));
}

/** Lowercase SHA-256 hex digest of exactly these bytes (WebCrypto). */
export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as unknown as ArrayBuffer);
  const raw = new Uint8Array(digest);
  let hexadecimal = "";
  for (const byte of raw) {
    hexadecimal += byte.toString(16).padStart(2, "0");
  }
  return hexadecimal;
}
