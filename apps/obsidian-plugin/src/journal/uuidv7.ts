/**
 * Library-free RFC 9562 §5.7 UUIDv7 generator for the lifecycle capture.
 *
 * UUIDv7 packs the Unix epoch milliseconds in the high 48 bits, a 12-bit
 * sub-millisecond counter, and the same version / variant bits as a v4
 * UUID. The generator below:
 *
 *   - reads the wall clock via the injected `nowEpochMs` (defaults to
 *     `Date.now` so callers can pin the clock in tests);
 *   - counts every generation in the current millisecond so two ids
 *     minted back-to-back in the same ms carry distinct counters and the
 *     sequence stays monotonic on a single thread;
 *   - uses `crypto.getRandomValues` for the random bits so the output is
 *     indistinguishable from `crypto.randomUUID()` to a casual observer.
 *
 * The lifecycle capture injects this factory so the durable lifecycle
 * ids are time-ordered; the repository defaults to `crypto.randomUUID()`
 * for backward compatibility. No production dependency is added.
 */

const HEX_DIGITS = "0123456789abcdef";

function toHex(byte: number): string {
  const high = HEX_DIGITS[(byte >>> 4) & 0x0f] ?? "0";
  const low = HEX_DIGITS[byte & 0x0f] ?? "0";
  return high + low;
}

function randomBytes(length: number): Uint8Array {
  const buffer = new Uint8Array(length);
  crypto.getRandomValues(buffer);
  return buffer;
}

export interface Uuidv7FactoryOptions {
  /** Clock for the high 48 bits; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
  /** Random source for the trailing bits; defaults to `crypto.getRandomValues`. */
  readonly randomBytes?: (length: number) => Uint8Array;
}

/**
 * Build one UUIDv7 generator. The returned function mints RFC 9562 v7
 * ids; an exhaustive version probe can read the high nibble of the
 * third group (`xxxx-xxxx-7xxx-...`) to prove the version.
 */
export function createUuidv7Factory(
  options: Uuidv7FactoryOptions = {},
): () => string {
  const nowEpochMs = options.nowEpochMs ?? (() => Date.now());
  const random = options.randomBytes ?? randomBytes;
  let lastTimestampMs = -1;
  let counter = 0;
  return function nextUuidv7(): string {
    let timestampMs = nowEpochMs();
    if (timestampMs === lastTimestampMs) {
      counter = (counter + 1) & 0xfff;
      if (counter === 0) {
        // The counter overflowed: roll the clock forward one millisecond
        // so the same id is never minted twice.
        timestampMs += 1;
      }
    } else {
      lastTimestampMs = timestampMs;
      counter = 0;
    }
    const tailBytes = random(10);
    const byte0 = tailBytes[0] ?? 0;
    const byte1 = tailBytes[1] ?? 0;
    const byte2 = tailBytes[2] ?? 0;
    const byte3 = tailBytes[3] ?? 0;
    const byte4 = tailBytes[4] ?? 0;
    const byte5 = tailBytes[5] ?? 0;
    const byte6 = tailBytes[6] ?? 0;
    const byte7 = tailBytes[7] ?? 0;
    const bytes = new Uint8Array(16);
    // 48-bit big-endian timestamp in bytes[0..5].
    bytes[0] = (timestampMs / 0x10000000000) & 0xff;
    bytes[1] = (timestampMs / 0x100000000) & 0xff;
    bytes[2] = (timestampMs >>> 24) & 0xff;
    bytes[3] = (timestampMs >>> 16) & 0xff;
    bytes[4] = (timestampMs >>> 8) & 0xff;
    bytes[5] = timestampMs & 0xff;
    // bytes[6]: version (0x7) in the high nibble + 4 bits of counter.
    bytes[6] = 0x70 | ((counter >>> 8) & 0x0f);
    // bytes[7]: low 8 bits of counter.
    bytes[7] = counter & 0xff;
    // bytes[8]: variant (0b10xx_xxxx) + 6 bits of random.
    bytes[8] = 0x80 | (byte0 & 0x3f);
    // bytes[9..15]: random tail.
    bytes[9] = byte1;
    bytes[10] = byte2;
    bytes[11] = byte3;
    bytes[12] = byte4;
    bytes[13] = byte5;
    bytes[14] = byte6;
    bytes[15] = byte7;
    void tailBytes;
    let hex = "";
    for (let index = 0; index < 16; index += 1) {
      const byte = bytes[index] ?? 0;
      if (index === 4 || index === 6 || index === 8 || index === 10) {
        hex += "-";
      }
      hex += toHex(byte);
    }
    return hex;
  };
}

/**
 * Read the UUID version nibble: the high nibble of the third group's
 * first byte (`xxxxxxxx-xxxx-Vxxx-...`). Useful in assertions; the brief
 * asks the tests to prove the lifecycle ids are v7.
 */
export function uuidVersion(value: string): number {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    return 0;
  }
  const thirdGroup = value.split("-")[2] ?? "";
  const high = parseInt(thirdGroup[0] ?? "0", 16);
  return Number.isFinite(high) ? high : 0;
}