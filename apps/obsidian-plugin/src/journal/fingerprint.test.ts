import { describe, expect, it } from "vitest";

import { deriveFrozenFingerprint, isFrozenFingerprintShape } from "./fingerprint";
import type { FrozenFingerprint } from "./contracts";

/** One valid fingerprint with a distinguishable digest for local assertions. */
function testFingerprint(digestPrefix: string, sizeBytes = 32): FrozenFingerprint {
  return {
    sha256: `${digestPrefix}${"0".repeat(64 - digestPrefix.length)}`,
    sizeBytes,
    mediaType: "text/plain",
  };
}

describe("deriveFrozenFingerprint content identity (spec 6.3, 7.1)", () => {
  it("derives the exact lowercase SHA-256, byte size and text media type of text bytes", async () => {
    const markdownBytes = new TextEncoder().encode(
      "# Journal heading\n\nBody text with unicode: café ☕.\n",
    );

    const fingerprint = await deriveFrozenFingerprint(markdownBytes);

    expect(fingerprint).toEqual({
      sha256: "6c3eeaa1a1061b9286f6a8766bed4a6edb992bcfa033889d49fb938d1f7cfe00",
      sizeBytes: 54,
      mediaType: "text/plain",
    });
  });

  it("derives the well-known empty-content digest with the text media type", async () => {
    const fingerprint = await deriveFrozenFingerprint(new Uint8Array(0));

    expect(fingerprint).toEqual({
      sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      sizeBytes: 0,
      mediaType: "text/plain",
    });
  });

  it("sniffs image/png from the exact eight-byte PNG signature", async () => {
    const pngBytes = new Uint8Array([
      0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
      0x70, 0x61, 0x79, 0x6c, 0x6f, 0x61, 0x64,
    ]);

    const fingerprint = await deriveFrozenFingerprint(pngBytes);

    expect(fingerprint).toEqual({
      sha256: "399b7cc9a888d40c8a3d09a9cb47c5a8a20932bee65c45792a2e4f5513beb3b0",
      sizeBytes: 15,
      mediaType: "image/png",
    });
  });

  it("sniffs image/jpeg, image/gif, image/webp, application/pdf and video/mp4 magic", async () => {
    const jpeg = await deriveFrozenFingerprint(
      new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x70, 0x61, 0x79, 0x6c, 0x6f, 0x61, 0x64]),
    );
    expect(jpeg.mediaType).toBe("image/jpeg");
    expect(jpeg.sha256).toBe("3a120f76220a26fab7868669c8af6ff7ced052671b08f49a23e932da6a0a15cd");
    expect(jpeg.sizeBytes).toBe(11);

    const gif = await deriveFrozenFingerprint(
      new TextEncoder().encode("GIF89arest"),
    );
    expect(gif.mediaType).toBe("image/gif");
    expect(gif.sha256).toBe("351735ab80fbb7bd549ebc42d45ab5df4e7922f76d0bd260debd83d4755c499d");

    const webp = await deriveFrozenFingerprint(
      new Uint8Array([
        0x52, 0x49, 0x46, 0x46, 0x12, 0x00, 0x00, 0x00, 0x57, 0x45, 0x42, 0x50,
        0x56, 0x50, 0x38, 0x20, 0x64, 0x61, 0x74, 0x61,
      ]),
    );
    expect(webp.mediaType).toBe("image/webp");
    expect(webp.sha256).toBe("f67ae223680e180dba736ff08ef821171a67da677ebfa2f3ed6f098de7aea61c");

    const pdf = await deriveFrozenFingerprint(new TextEncoder().encode("%PDF-1.7\nstuff"));
    expect(pdf.mediaType).toBe("application/pdf");
    expect(pdf.sha256).toBe("1f7c7cc03c4ce07511c878d2d13493404531499989b5cfcdb437749dbbbf7e59");

    const mp4 = await deriveFrozenFingerprint(
      new Uint8Array([
        0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d,
        0x00, 0x00, 0x00, 0x00,
      ]),
    );
    expect(mp4.mediaType).toBe("video/mp4");
    expect(mp4.sha256).toBe("6a3516fe330ee9521ca254e36679041e20f749df244517d6570595ea1a199844");
  });

  it("falls back to text/plain for RIFF bytes that are not WEBP", async () => {
    const riffWaveBytes = new TextEncoder().encode("RIFFxxxxWAVE");

    const fingerprint = await deriveFrozenFingerprint(riffWaveBytes);

    expect(fingerprint.mediaType).toBe("text/plain");
    expect(fingerprint.sha256).toBe("93ba7260d8a7da7f1c380c0b6926af9997018a9dd9b5ce626e00600162a9cad8");
  });

  it("falls back to application/octet-stream for bytes that are not valid UTF-8", async () => {
    const binaryBytes = new Uint8Array([0xff, 0xfe, 0xfd, 0xfc, 0xfb]);

    const fingerprint = await deriveFrozenFingerprint(binaryBytes);

    expect(fingerprint.mediaType).toBe("application/octet-stream");
    expect(fingerprint.sizeBytes).toBe(5);
    expect(fingerprint.sha256).toBe("037f8e25672a345a36c520ef679ccdde7dac150641e797fe2c38e7f5c5c8d5e8");
  });
});

describe("isFrozenFingerprintShape closed validation (spec 6.3, 10.1)", () => {
  it("accepts a canonical lowercase fingerprint shape", () => {
    expect(isFrozenFingerprintShape(testFingerprint("ab"))).toBe(true);
    expect(isFrozenFingerprintShape({ sha256: "ab".repeat(32), sizeBytes: 0, mediaType: "image/png" })).toBe(true);
  });

  it("rejects non-hex, short, or uppercase digests", () => {
    expect(
      isFrozenFingerprintShape({ ...testFingerprint("ab"), sha256: "z" + "0".repeat(63) }),
    ).toBe(false);
    expect(
      isFrozenFingerprintShape({ ...testFingerprint("ab"), sha256: "AB".repeat(32) }),
    ).toBe(false);
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), sha256: "0".repeat(63) })).toBe(false);
  });

  it("rejects negative, fractional, or non-integer byte sizes", () => {
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), sizeBytes: -1 })).toBe(false);
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), sizeBytes: 1.5 })).toBe(false);
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), sizeBytes: Number.NaN })).toBe(false);
  });

  it("rejects non-canonical media types", () => {
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), mediaType: "image" })).toBe(false);
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), mediaType: "image/png/extra" })).toBe(false);
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), mediaType: "image/" })).toBe(false);
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), mediaType: "/png" })).toBe(false);
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), mediaType: "image/pn g" })).toBe(false);
    expect(isFrozenFingerprintShape({ ...testFingerprint("ab"), mediaType: "" })).toBe(false);
  });
});
