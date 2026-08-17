import { describe, expect, it } from "vitest";

import { PolicyVerificationError } from "./contracts";
import { parseClosedJson, utf8ByteLength } from "./strict-json";

const LIMIT = 1024;

function parse(text: string): unknown {
  return parseClosedJson(text, { maximumBytes: LIMIT });
}

function rejectionReason(text: string): string {
  try {
    parse(text);
  } catch (error) {
    if (error instanceof PolicyVerificationError) {
      return error.reason;
    }
    throw error;
  }
  throw new Error("expected the closed parser to reject the input");
}

describe("parseClosedJson", () => {
  it("parses the closed value grammar exactly", () => {
    expect(parse("null")).toBeNull();
    expect(parse("true")).toBe(true);
    expect(parse("false")).toBe(false);
    expect(parse("0")).toBe(0);
    expect(parse("-1")).toBe(-1);
    expect(parse("9007199254740991")).toBe(9007199254740991);
    expect(parse("-9007199254740991")).toBe(-9007199254740991);
    expect(parse('"café"')).toBe("café");
    expect(parse('{"a": [1, {"b": "c"}]}')).toEqual({ a: [1, { b: "c" }] });
  });

  it("accepts surrounding JSON whitespace only", () => {
    expect(parse(' \r\n\t{"a":1} ')).toEqual({ a: 1 });
    expect(rejectionReason('{"a":1} x')).toBe("policy_response_malformed");
    expect(rejectionReason('{"a":1}{"b":2}')).toBe("policy_response_malformed");
    expect(rejectionReason("")).toBe("policy_response_malformed");
  });

  it("rejects duplicate object members before any schema or canonical step", () => {
    expect(rejectionReason('{"a":1,"a":2}')).toBe("policy_response_malformed");
    expect(rejectionReason('{"a":1,"b":2,"a":3}')).toBe("policy_response_malformed");
    // Escapes resolve before duplicate detection: "a" and "\u0061" collide.
    expect(rejectionReason('{"a":1,"\\u0061":2}')).toBe("policy_response_malformed");
  });

  it("rejects lone surrogates in escapes and raw text", () => {
    expect(rejectionReason('"\\ud800"')).toBe("policy_response_malformed");
    expect(rejectionReason('"\\udc00"')).toBe("policy_response_malformed");
    expect(rejectionReason('"ok\\ud800"')).toBe("policy_response_malformed");
    // A raw unpaired surrogate code unit (accepted by JSON.parse) is rejected.
    expect(rejectionReason(`"${String.fromCharCode(0xd800)}"`)).toBe("policy_response_malformed");
    // A valid surrogate pair survives.
    expect(parse('"\\ud83d\\ude00"')).toBe("😀");
  });

  it("rejects floats, exponents and non-I-JSON numbers", () => {
    for (const malformed of [
      "1.0",
      "1.5",
      "-0.5",
      "1e2",
      "1E2",
      "1e+2",
      "1e-2",
      "01",
      "-01",
      "+1",
      "1.",
      ".5",
      "NaN",
      "Infinity",
      "-Infinity",
    ]) {
      expect(rejectionReason(malformed)).toBe("policy_response_malformed");
    }
    expect(parse("-0")).toBe(-0);
  });

  it("rejects integers outside the IEEE 754 safe range", () => {
    expect(rejectionReason("9007199254740992")).toBe("policy_response_malformed");
    expect(rejectionReason("-9007199254740992")).toBe("policy_response_malformed");
  });

  it("rejects unescaped control characters and malformed escapes", () => {
    expect(rejectionReason('"a\u0007b"')).toBe("policy_response_malformed");
    expect(rejectionReason('"a\\x41b"')).toBe("policy_response_malformed");
    expect(rejectionReason("'single'")).toBe("policy_response_malformed");
    expect(rejectionReason("// comment")).toBe("policy_response_malformed");
    expect(rejectionReason('{"a":undefined}')).toBe("policy_response_malformed");
    expect(rejectionReason('{"a":}')).toBe("policy_response_malformed");
    expect(rejectionReason("[1,]")).toBe("policy_response_malformed");
  });

  it("bounds the accepted text by UTF-8 bytes", () => {
    const oversized = `"${"a".repeat(LIMIT)}"`;
    expect(rejectionReason(oversized)).toBe("policy_response_oversized");
    expect(parse(`"${"a".repeat(LIMIT - 2)}"`)).toBe("a".repeat(LIMIT - 2));
    // Multi-byte characters count as their encoded size, not code units.
    const repeated = "é".repeat(Math.ceil((LIMIT + 1) / 2));
    expect(rejectionReason(`"${repeated}"`)).toBe("policy_response_oversized");
  });

  it("bounds nesting depth", () => {
    const deep = "[".repeat(200) + "]".repeat(200);
    expect(rejectionReason(deep)).toBe("policy_response_malformed");
  });

  it("resolves the standard escape set exactly", () => {
    expect(parse('"\\b\\t\\n\\f\\r\\"\\\\\\/"')).toBe('\b\t\n\f\r"\\/');
    expect(parse('"\\u0041"')).toBe("A");
    expect(parse('"\\u00e9"')).toBe("é");
  });
});

describe("utf8ByteLength", () => {
  it("counts encoded bytes for ASCII, Latin-1 and astral text", () => {
    expect(utf8ByteLength("")).toBe(0);
    expect(utf8ByteLength("abc")).toBe(3);
    expect(utf8ByteLength("é")).toBe(2);
    expect(utf8ByteLength("😀")).toBe(4);
  });
});
