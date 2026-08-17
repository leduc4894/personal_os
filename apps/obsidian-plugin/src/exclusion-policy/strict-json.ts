/**
 * Repository-owned closed JSON parser for bounded policy responses.
 *
 * Plain `JSON.parse` is insufficient here: it silently accepts duplicate
 * object members (keeping the last), lone UTF-16 surrogates, floats and
 * out-of-range integers. This recursive-descent parser accepts only the
 * I-JSON-compatible closed grammar — `null`, booleans, safe-range integers,
 * arrays, and objects with unique string member names — and rejects
 * everything else with a single closed reason token BEFORE any schema
 * validation or canonicalization runs (spec 13.4 step 1).
 */

import { policyVerificationError } from "./contracts";

/** The closed value grammar reachable from policy responses. */
export type ClosedJsonValue =
  | null
  | boolean
  | number
  | string
  | readonly ClosedJsonValue[]
  | { readonly [member: string]: ClosedJsonValue };

const MAXIMUM_NESTING_DEPTH = 64;

interface ParserState {
  readonly text: string;
  position: number;
}

function malformed(): never {
  throw policyVerificationError("policy_response_malformed");
}

/** Count the UTF-8 encoded size of a string; lone surrogates count as 3. */
export function utf8ByteLength(value: string): number {
  let total = 0;
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit < 0x80) {
      total += 1;
    } else if (codeUnit < 0x800) {
      total += 2;
    } else if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const next = index + 1 < value.length ? value.charCodeAt(index + 1) : 0;
      if (next >= 0xdc00 && next <= 0xdfff) {
        total += 4;
        index += 1;
      } else {
        total += 3;
      }
    } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      total += 3;
    } else {
      total += 3;
    }
  }
  return total;
}

function skipWhitespace(state: ParserState): void {
  while (state.position < state.text.length) {
    const character = state.text[state.position];
    if (character === " " || character === "\t" || character === "\n" || character === "\r") {
      state.position += 1;
      continue;
    }
    return;
  }
}

function peek(state: ParserState): string {
  return state.text[state.position] ?? "";
}

function peekAt(state: ParserState, offset: number): string {
  return state.text[state.position + offset] ?? "";
}

function consume(state: ParserState, expected: string): void {
  if (state.text.startsWith(expected, state.position)) {
    state.position += expected.length;
    return;
  }
  malformed();
}

function parseValue(state: ParserState, depth: number): ClosedJsonValue {
  if (depth > MAXIMUM_NESTING_DEPTH) {
    malformed();
  }
  const character = peek(state);
  if (character === "{") {
    return parseObject(state, depth);
  }
  if (character === "[") {
    return parseArray(state, depth);
  }
  if (character === '"') {
    return parseString(state);
  }
  if (character === "t") {
    consume(state, "true");
    return true;
  }
  if (character === "f") {
    consume(state, "false");
    return false;
  }
  if (character === "n") {
    consume(state, "null");
    return null;
  }
  if (character === "-" || (character >= "0" && character <= "9")) {
    return parseInteger(state);
  }
  malformed();
}

function parseObject(state: ParserState, depth: number): { readonly [member: string]: ClosedJsonValue } {
  consume(state, "{");
  const members: Record<string, ClosedJsonValue> = {};
  skipWhitespace(state);
  if (peek(state) === "}") {
    state.position += 1;
    return members;
  }
  for (;;) {
    skipWhitespace(state);
    if (peek(state) !== '"') {
      malformed();
    }
    const name = parseString(state);
    if (Object.prototype.hasOwnProperty.call(members, name)) {
      malformed();
    }
    skipWhitespace(state);
    if (peek(state) !== ":") {
      malformed();
    }
    state.position += 1;
    skipWhitespace(state);
    members[name] = parseValue(state, depth + 1);
    skipWhitespace(state);
    const separator = peek(state);
    if (separator === ",") {
      state.position += 1;
      continue;
    }
    if (separator === "}") {
      state.position += 1;
      return members;
    }
    malformed();
  }
}

function parseArray(state: ParserState, depth: number): readonly ClosedJsonValue[] {
  consume(state, "[");
  const elements: ClosedJsonValue[] = [];
  skipWhitespace(state);
  if (peek(state) === "]") {
    state.position += 1;
    return elements;
  }
  for (;;) {
    skipWhitespace(state);
    elements.push(parseValue(state, depth + 1));
    skipWhitespace(state);
    const separator = peek(state);
    if (separator === ",") {
      state.position += 1;
      continue;
    }
    if (separator === "]") {
      state.position += 1;
      return elements;
    }
    malformed();
  }
}

const SHORT_ESCAPES: Readonly<Record<string, string>> = {
  '"': '"',
  "\\": "\\",
  "/": "/",
  b: "\b",
  f: "\f",
  n: "\n",
  r: "\r",
  t: "\t",
};

function isHexDigit(character: string): boolean {
  return (
    (character >= "0" && character <= "9") ||
    (character >= "a" && character <= "f") ||
    (character >= "A" && character <= "F")
  );
}

function readHex4(state: ParserState): number {
  let value = 0;
  for (let digit = 0; digit < 4; digit += 1) {
    const character = peek(state);
    if (!isHexDigit(character)) {
      malformed();
    }
    value = value * 16 + Number.parseInt(character, 16);
    state.position += 1;
  }
  return value;
}

function parseString(state: ParserState): string {
  consume(state, '"');
  let pieces = "";
  for (;;) {
    if (state.position >= state.text.length) {
      malformed();
    }
    const character = state.text[state.position];
    if (character === '"') {
      state.position += 1;
      return pieces;
    }
    if (character === "\\") {
      state.position += 1;
      const escape = peek(state);
      if (escape === "u") {
        state.position += 1;
        const first = readHex4(state);
        if (first >= 0xd800 && first <= 0xdbff) {
          // A high surrogate must be followed by its low half.
          if (peek(state) !== "\\" || peekAt(state, 1) !== "u") {
            malformed();
          }
          state.position += 2;
          const second = readHex4(state);
          if (second < 0xdc00 || second > 0xdfff) {
            malformed();
          }
          pieces += String.fromCharCode(first, second);
          continue;
        }
        if (first >= 0xdc00 && first <= 0xdfff) {
          malformed();
        }
        pieces += String.fromCharCode(first);
        continue;
      }
      const short = SHORT_ESCAPES[escape];
      if (short === undefined) {
        malformed();
      }
      pieces += short;
      state.position += 1;
      continue;
    }
    if (character === undefined) {
      malformed();
    }
    const codeUnit = character.charCodeAt(0);
    if (codeUnit < 0x20) {
      malformed();
    }
    if (codeUnit >= 0xd800 && codeUnit <= 0xdfff) {
      // Raw surrogate code units: a complete pair is required inline too.
      const next = state.text.charCodeAt(state.position + 1);
      if (codeUnit <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) {
        pieces += character + peekAt(state, 1);
        state.position += 2;
        continue;
      }
      malformed();
    }
    pieces += character;
    state.position += 1;
  }
}

const MAXIMUM_SAFE_INTEGER = 9007199254740991;
const MINIMUM_SAFE_INTEGER = -9007199254740991;

function parseInteger(state: ParserState): number {
  const start = state.position;
  if (peek(state) === "-") {
    state.position += 1;
  }
  const firstDigit = peek(state);
  if (!(firstDigit >= "0" && firstDigit <= "9")) {
    malformed();
  }
  if (firstDigit === "0") {
    state.position += 1;
  } else {
    while (peek(state) >= "0" && peek(state) <= "9") {
      state.position += 1;
    }
  }
  const next = peek(state);
  // Floats, exponents and leading zeros stay outside the closed grammar.
  if (next === "." || next === "e" || next === "E" || (next >= "0" && next <= "9")) {
    malformed();
  }
  const value = Number(state.text.slice(start, state.position));
  if (!Number.isSafeInteger(value) || value > MAXIMUM_SAFE_INTEGER || value < MINIMUM_SAFE_INTEGER) {
    malformed();
  }
  return value;
}

/**
 * Parse one bounded response text into the closed value grammar. Duplicate
 * members, lone surrogates, floats, non-JSON tokens and oversized text are
 * rejected with a closed reason before anything else sees the value.
 */
export function parseClosedJson(
  text: string,
  options: { readonly maximumBytes: number },
): ClosedJsonValue {
  if (utf8ByteLength(text) > options.maximumBytes) {
    throw policyVerificationError("policy_response_oversized");
  }
  const state: ParserState = { text, position: 0 };
  skipWhitespace(state);
  const value = parseValue(state, 1);
  skipWhitespace(state);
  if (state.position !== state.text.length) {
    malformed();
  }
  return value;
}
