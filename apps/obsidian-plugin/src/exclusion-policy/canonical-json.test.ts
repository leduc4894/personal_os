import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { PolicyVerificationError } from "./contracts";
import { canonicalizeClosedJson, canonicalJsonBytes, sha256Hex } from "./canonical-json";

const FIXTURES_URL = new URL("../../../../tests/fixtures/exclusion_policy/", import.meta.url);

interface SignedFixture {
  readonly contract: string;
  readonly payload: string;
  readonly payload_sha256: string;
  readonly signature: { readonly algorithm: string; readonly key_id: string; readonly value: string };
  readonly signing_public_key: string;
}

function loadSignedFixture(name: string): SignedFixture {
  return JSON.parse(
    readFileSync(new URL(`${name}-golden.json`, FIXTURES_URL), "utf8"),
  ) as SignedFixture;
}

function canonicalizeRejection(value: unknown): string {
  try {
    canonicalizeClosedJson(value as never);
  } catch (error) {
    if (error instanceof PolicyVerificationError) {
      return error.reason;
    }
    throw error;
  }
  throw new Error("expected the canonical encoder to reject the value");
}

describe("canonicalizeClosedJson", () => {
  it("renders scalars, arrays and objects without insignificant whitespace", () => {
    expect(canonicalizeClosedJson(null)).toBe("null");
    expect(canonicalizeClosedJson(true)).toBe("true");
    expect(canonicalizeClosedJson(false)).toBe("false");
    expect(canonicalizeClosedJson(0)).toBe("0");
    expect(canonicalizeClosedJson(-12)).toBe("-12");
    expect(canonicalizeClosedJson("plain")).toBe('"plain"');
    expect(canonicalizeClosedJson([])).toBe("[]");
    expect(canonicalizeClosedJson({})).toBe("{}");
    expect(canonicalizeClosedJson({ b: 1, a: [true, null, "x"] })).toBe(
      '{"a":[true,null,"x"],"b":1}',
    );
  });

  it("sorts members by UTF-16 code units (RFC 8785 astral-before-FFFF quirk)", () => {
    // U+10000 encodes as D800 DC00, so it sorts BEFORE U+FFFF by code units.
    expect(canonicalizeClosedJson({ "\uFFFF": 1, "\u{10000}": 2 })).toBe(
      '{"\u{10000}":2,"￿":1}',
    );
    expect(canonicalizeClosedJson({ b: 1, a: 2, A: 3 })).toBe('{"A":3,"a":2,"b":1}');
  });

  it("escapes exactly the closed minimal set", () => {
    expect(canonicalizeClosedJson('a"b\\c')).toBe('"a\\"b\\\\c"');
    expect(canonicalizeClosedJson("\b\t\n\f\r")).toBe('"\\b\\t\\n\\f\\r"');
    expect(canonicalizeClosedJson("\u0000")).toBe('"\\u0000"');
    expect(canonicalizeClosedJson("\u001f")).toBe('"\\u001f"');
    expect(canonicalizeClosedJson("")).toBe(`"${String.fromCharCode(0x7f)}"`);
    expect(canonicalizeClosedJson("café")).toBe('"café"');
  });

  it("rejects values outside the closed grammar", () => {
    expect(canonicalizeRejection(1.5)).toBe("policy_value_unsupported");
    expect(canonicalizeRejection(Number.NaN)).toBe("policy_value_unsupported");
    expect(canonicalizeRejection(9007199254740992)).toBe("policy_value_unsupported");
    expect(canonicalizeRejection({ a: undefined })).toBe("policy_value_unsupported");
    expect(canonicalizeRejection("\ud800")).toBe("policy_value_unsupported");
    expect(canonicalizeRejection("café")).toBe("policy_value_unsupported");
  });

  it("reproduces the Python canonical bytes of both signed golden fixtures", () => {
    for (const name of ["keyset", "snapshot"]) {
      const fixture = loadSignedFixture(name);
      const parsed = JSON.parse(fixture.payload) as unknown;
      expect(canonicalizeClosedJson(parsed as never)).toBe(fixture.payload);
      expect(Array.from(canonicalJsonBytes(parsed as never))).toEqual(
        Array.from(new TextEncoder().encode(fixture.payload)),
      );
    }
  });
});

describe("sha256Hex", () => {
  it("hashes bytes to lowercase hex", async () => {
    expect(await sha256Hex(new TextEncoder().encode("abc"))).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    expect(await sha256Hex(new TextEncoder().encode("exclusion_policy_evaluator/v1"))).toBe(
      "8f174f9aa9a7a1580b377fa469a65c6e76801db66421404703b7aab38f50fbe1",
    );
  });
});
