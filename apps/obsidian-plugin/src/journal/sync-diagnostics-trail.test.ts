/**
 * Tests of the durable closed-token sync diagnostics trail (sync error
 * tracing design: diagnostic trail event contract).
 *
 * The trail is the restart-surviving on-device record of sync failures:
 * closed kind tokens, closed vocabulary tokens and the one opaque envelope
 * request id — never a path, digest, credential, hostname or any free-form
 * string. These tests pin: the 128-entry eviction bound, the sidecar
 * persist/reload cycle through the journal file store port, the corrupt
 * sidecar reset (`trail_reset`), the type-level closed vocabulary, the
 * bounded per-entry token list, the swallowed-and-counted append failures,
 * and the serialized coalescing sidecar writes.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import type { JournalFileStore } from "./persistence";
import {
  MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES,
  MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES,
  MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY,
  SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
  SYNC_PARK_SITE_TOKENS,
  SYNC_SELF_CHECK_VERDICT_TOKENS,
  createSyncDiagnosticsTrail,
  envelopeErrorCode,
  envelopeRequestId,
} from "./sync-diagnostics-trail";

// --- the fake journal file store ----------------------------------------------------------------

/** The fake journal directory the trail persists its sidecar into. */
class FakeTrailFileStore implements JournalFileStore {
  readonly files = new Map<string, ArrayBuffer>();
  readonly accessedFileNames: string[] = [];
  writeCount = 0;
  existsThrows = false;
  writeThrows = false;
  #activeWriteCount = 0;
  overlapDetected = false;

  async exists(fileName: string): Promise<boolean> {
    this.accessedFileNames.push(`exists:${fileName}`);
    if (this.existsThrows) {
      throw new Error("existence probe failed");
    }
    return this.files.has(fileName);
  }

  async readBinary(fileName: string): Promise<ArrayBuffer> {
    this.accessedFileNames.push(`read:${fileName}`);
    const data = this.files.get(fileName);
    if (data === undefined) {
      throw new Error("file not found");
    }
    return data.slice(0);
  }

  async writeBinary(fileName: string, data: ArrayBuffer): Promise<void> {
    this.accessedFileNames.push(`write:${fileName}`);
    this.writeCount += 1;
    this.#activeWriteCount += 1;
    if (this.#activeWriteCount > 1) {
      this.overlapDetected = true;
    }
    try {
      if (this.writeThrows) {
        throw new Error("write failed");
      }
      this.files.set(fileName, data.slice(0));
    } finally {
      this.#activeWriteCount -= 1;
    }
  }

  async remove(fileName: string): Promise<void> {
    this.accessedFileNames.push(`remove:${fileName}`);
    this.files.delete(fileName);
  }
}

const REQUEST_ID = "66666666-6666-4666-8666-666666666666";

/** Create one loaded trail over a fresh fake store. */
async function createLoadedTrail(
  store: FakeTrailFileStore,
  nowEpochMs: () => number = () => 1_784_000_000_000,
) {
  const trail = createSyncDiagnosticsTrail({ fileStore: store, nowEpochMs });
  await trail.load();
  return trail;
}

// --- append and eviction --------------------------------------------------------------------------

describe("sync diagnostics trail append and eviction", () => {
  it("appends entries in order and evicts the oldest beyond the 128-entry cap", async () => {
    const store = new FakeTrailFileStore();
    let clockMs = 1_784_000_000_000;
    const trail = await createLoadedTrail(store, () => clockMs);
    for (let index = 0; index < MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES + 2; index += 1) {
      clockMs += 1;
      await trail.append({ kind: "pass_outcome", tokens: ["completed"] });
    }
    const entries = trail.readEntries();
    expect(entries).toHaveLength(MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES);
    // The two oldest entries were evicted; the oldest survivor is entry #2.
    expect(entries[0]?.atEpochMs).toBe(1_784_000_000_000 + 3);
    expect(entries.at(-1)?.atEpochMs).toBe(1_784_000_000_000 + MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES + 2);
    expect(MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES).toBe(128);
  });

  it("bounds the tokens of one entry", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    const tokens = [
      "network_offline",
      "network_timeout",
      "network_rate_limited",
      "server_error",
      "login_required",
      "blocked_size",
      "blocked_conflict",
      "integrity_failed",
      "deferred_lifecycle",
    ] as const;
    await trail.append({ kind: "wire_failure", tokens });
    expect(trail.readEntries()[0]?.tokens).toHaveLength(MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY);
    expect(MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY).toBe(8);
  });
});

// --- sidecar persistence ----------------------------------------------------------------------------

describe("sync diagnostics trail sidecar persistence", () => {
  it("persists through the journal file store so a reload survives", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "journal_failure", tokens: ["journal_query_failed"] });
    await trail.append({
      kind: "wire_failure",
      tokens: ["server_error", envelopeRequestId(REQUEST_ID)],
    });

    expect(store.files.has(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME)).toBe(true);
    expect(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME).toBe("sync-diagnostics-trail.json");

    const reloaded = await createLoadedTrail(store);
    const entries = reloaded.readEntries();
    expect(entries).toHaveLength(2);
    expect(entries[0]?.kind).toBe("journal_failure");
    expect(entries[0]?.tokens).toEqual(["journal_query_failed"]);
    expect(entries[1]?.kind).toBe("wire_failure");
    // The closed string tokens round-trip as strings; the opaque envelope
    // request id round-trips as the one object token shape.
    expect(entries[1]?.tokens[0]).toBe("server_error");
    expect(entries[1]?.tokens[1]).toEqual({ requestId: REQUEST_ID });
  });

  it("resets a corrupt sidecar to empty and records trail_reset", async () => {
    const store = new FakeTrailFileStore();
    store.files.set(
      SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
      new TextEncoder().encode("{ not the trail").buffer as ArrayBuffer,
    );
    const trail = await createLoadedTrail(store);
    expect(trail.readEntries()).toHaveLength(1);
    expect(trail.readEntries()[0]?.kind).toBe("trail_reset");
    expect(trail.readEntries()[0]?.tokens).toEqual([]);

    // The reset itself persisted: a second load keeps the trail_reset entry.
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries()).toHaveLength(1);
    expect(reloaded.readEntries()[0]?.kind).toBe("trail_reset");
  });

  it("treats a structurally foreign sidecar record as corrupt", async () => {
    for (const foreignBody of [
      JSON.stringify({ contract: "some_other_contract/v1", entries: [] }),
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v1",
        entries: [{ kind: "mystery_kind", at_epoch_ms: 1, tokens: [] }],
      }),
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v1",
        entries: [{ kind: "pass_outcome", at_epoch_ms: -5, tokens: [] }],
      }),
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v1",
        entries: [
          { kind: "pass_outcome", at_epoch_ms: 1, tokens: ["notes/leaked-path.md"] },
        ],
      }),
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v1",
        entries: [
          { kind: "pass_outcome", at_epoch_ms: 1, tokens: [{ request_id: "at1.leaked" }] },
        ],
      }),
    ]) {
      const store = new FakeTrailFileStore();
      store.files.set(
        SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
        new TextEncoder().encode(foreignBody).buffer as ArrayBuffer,
      );
      const trail = await createLoadedTrail(store);
      expect(trail.readEntries().map((entry) => entry.kind)).toEqual(["trail_reset"]);
    }
  });

  it("records nothing for an absent sidecar and never persists on a fresh load", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    expect(trail.readEntries()).toEqual([]);
    expect(store.writeCount).toBe(0);
    expect(store.accessedFileNames).toEqual([`exists:${SYNC_DIAGNOSTICS_TRAIL_FILE_NAME}`]);
  });

  it("records trail_reset when the sidecar exists but cannot be read", async () => {
    const store = new FakeTrailFileStore();
    store.files.set(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME, new ArrayBuffer(4));
    store.accessedFileNames.length = 0;
    const readBinary = store.readBinary.bind(store);
    store.readBinary = async () => {
      throw new Error("adapter failure");
    };
    void readBinary;
    const trail = await createLoadedTrail(store);
    expect(trail.readEntries().map((entry) => entry.kind)).toEqual(["trail_reset"]);
  });
});

// --- append failures never escape -------------------------------------------------------------------

describe("sync diagnostics trail append failure handling", () => {
  it("swallows persist failures into the bounded counter and never rejects", async () => {
    const store = new FakeTrailFileStore();
    store.writeThrows = true;
    const trail = await createLoadedTrail(store);
    await expect(trail.append({ kind: "pass_outcome", tokens: ["completed"] })).resolves.toBeUndefined();
    expect(trail.readEntries()).toHaveLength(1);
    expect(trail.readAppendFailureCount()).toBe(1);

    for (let index = 0; index < MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES + 10; index += 1) {
      await trail.append({ kind: "pass_outcome", tokens: ["stopped"] });
    }
    // The counter is bounded; the entry ring keeps its own bound.
    expect(trail.readAppendFailureCount()).toBe(MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES);
    expect(MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES).toBe(999);
    expect(trail.readEntries()).toHaveLength(MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES);
  });

  it("keeps working after the store recovers", async () => {
    const store = new FakeTrailFileStore();
    store.writeThrows = true;
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "pass_outcome", tokens: ["completed"] });
    expect(trail.readAppendFailureCount()).toBe(1);
    store.writeThrows = false;
    await trail.append({ kind: "wire_failure", tokens: ["server_error"] });
    expect(trail.readAppendFailureCount()).toBe(1);
    const reloaded = await createLoadedTrail(store);
    // The in-memory ring kept the un-persisted entry; the first persist after
    // recovery writes the whole ring, so nothing observed is lost.
    expect(reloaded.readEntries().map((entry) => entry.kind)).toEqual([
      "pass_outcome",
      "wire_failure",
    ]);
  });
});

// --- serialized, coalesced writes --------------------------------------------------------------------

describe("sync diagnostics trail write serialization", () => {
  it("serializes sidecar writes and coalesces synchronous appends", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    const appends: readonly Promise<void>[] = [
      trail.append({ kind: "pass_outcome", tokens: ["completed"] }),
      trail.append({ kind: "wire_failure", tokens: ["server_error"] }),
      trail.append({ kind: "journal_failure", tokens: ["journal_query_failed"] }),
    ];
    await Promise.all([...appends]);
    expect(store.overlapDetected).toBe(false);
    // The three synchronous appends coalesced: the in-flight persist of the
    // first entry is followed by ONE persist carrying all three.
    expect(store.writeCount).toBe(2);
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries().map((entry) => entry.kind)).toEqual([
      "pass_outcome",
      "wire_failure",
      "journal_failure",
    ]);
  });
});

// --- the self_check kind and verdict tokens (sync error tracing task 3) ------------------------------

describe("sync diagnostics trail self_check entries", () => {
  it("persists and reloads self_check entries carrying the fixed verdict tokens", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "self_check", tokens: ["trail_probe"] });
    await trail.append({ kind: "self_check", tokens: ["trail_persist_ok"] });
    await trail.append({ kind: "self_check", tokens: ["credential_present"] });
    await trail.append({ kind: "self_check", tokens: ["origin_reachable"] });
    await trail.append({
      kind: "self_check",
      tokens: ["origin_unreachable", "network_offline"],
    });
    const reloaded = await createLoadedTrail(store);
    expect(
      reloaded.readEntries().map((entry) => [entry.kind, ...entry.tokens]),
    ).toEqual([
      ["self_check", "trail_probe"],
      ["self_check", "trail_persist_ok"],
      ["self_check", "credential_present"],
      ["self_check", "origin_reachable"],
      ["self_check", "origin_unreachable", "network_offline"],
    ]);
  });

  it("pins the fixed self-check verdict token vocabulary", () => {
    expect(SYNC_SELF_CHECK_VERDICT_TOKENS).toEqual([
      "trail_probe",
      "trail_persist_ok",
      "trail_persist_failed",
      "credential_present",
      "credential_absent",
      "origin_reachable",
      "origin_unreachable",
    ]);
  });
});

// --- the server envelope error-code tokens (diagnostic round U1) ---------------------------------------

describe("sync diagnostics trail envelope error-code tokens (diagnostic round U1)", () => {
  it("admits a server envelope code by shape and nulls a non-conforming code", () => {
    // These tokens are SERVER envelope codes: the server registry's closed
    // error-code vocabulary (snake_case strings), not a client-side union.
    // The trail boundary whitelists them by the existing closed-token shape
    // only — anything else records nothing.
    expect(envelopeErrorCode("exclusion_policy_denied")).toBe("exclusion_policy_denied");
    expect(envelopeErrorCode("authorization_scope_denied")).toBe("authorization_scope_denied");
    expect(envelopeErrorCode("Just a moment...")).toBeNull();
    expect(envelopeErrorCode("edge/challenge fragment")).toBeNull();
    expect(envelopeErrorCode("")).toBeNull();
  });

  it("round-trips a server envelope code token through the sidecar as a string token", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    const codeToken = envelopeErrorCode("authorization_scope_denied");
    if (codeToken === null) {
      throw new Error("expected a closed code token");
    }
    await trail.append({
      kind: "wire_failure",
      tokens: ["login_required", codeToken, envelopeRequestId(REQUEST_ID)],
    });

    const reloaded = await createLoadedTrail(store);
    // The code survives the reload as a plain closed string token, validated
    // by the same sidecar CLOSED_TOKEN_PATTERN guard as every other token.
    expect(reloaded.readEntries()[0]?.tokens).toEqual([
      "login_required",
      "authorization_scope_denied",
      { requestId: REQUEST_ID },
    ]);
  });
});

// --- the park throw-site tokens (diagnostic round U2) --------------------------------------------------

describe("sync diagnostics trail park throw-site tokens (diagnostic round U2)", () => {
  it("pins the fixed two-token park throw-site vocabulary", () => {
    expect(SYNC_PARK_SITE_TOKENS).toEqual(["site_argument_validation", "site_mutation_internal"]);
  });
});

// --- the type-level closed vocabulary ----------------------------------------------------------------

describe("sync diagnostics trail closed vocabulary (type level)", () => {
  it("rejects a free-form token, an unknown kind and an unbranded request id at compile time", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    trail.append({
      kind: "wire_failure",
      // @ts-expect-error a free-form string must not enter a trail entry
      tokens: ["edge block page after 12 seconds"],
    }).catch(() => undefined);
    trail.append({
      // @ts-expect-error an unknown diagnostic kind must not type-check
      kind: "mystery_diagnostic",
      tokens: [],
    }).catch(() => undefined);
    trail.append({
      kind: "wire_failure",
      // @ts-expect-error the request id must enter through the branded opaque token
      tokens: [{ requestId: REQUEST_ID }],
    }).catch(() => undefined);
    // The closed vocabularies and the branded opaque token DO type-check.
    await trail.append({ kind: "pass_outcome", tokens: ["retry_scheduled"] });
    await trail.append({
      kind: "wire_failure",
      tokens: ["server_error", envelopeRequestId(REQUEST_ID)],
    });
    // The fixed self-check verdicts DO type-check as self_check tokens,
    // including the reused sync network kinds.
    await trail.append({ kind: "self_check", tokens: ["trail_persist_ok"] });
    await trail.append({
      kind: "self_check",
      tokens: ["origin_unreachable", "network_timeout"],
    });
    trail.append({
      kind: "self_check",
      // @ts-expect-error a free-form verdict must not enter a self_check entry
      tokens: ["the trail is fine"],
    }).catch(() => undefined);
  });
});

// --- the privacy source contract ----------------------------------------------------------------------

describe("sync diagnostics trail privacy source contract", () => {
  it("keeps the trail module free of path-shaped and credential-shaped substrings", () => {
    const trailSource = readFileSync(new URL("./sync-diagnostics-trail.ts", import.meta.url), "utf8");
    for (const forbiddenText of [
      "console.",
      "fetch(",
      "requestUrl",
      ".md",
      "notes/",
      "at1.",
      "Bearer",
      "authorization",
      "https://",
    ]) {
      expect(trailSource).not.toContain(forbiddenText);
    }
  });
});
