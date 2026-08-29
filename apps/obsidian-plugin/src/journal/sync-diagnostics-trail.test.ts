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
import { MULTIPART_SAFE_REASON_TOKENS } from "./contracts";
import {
  MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES,
  MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES,
  MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY,
  SYNC_COMPOSITION_READ_FAILURE_TOKENS,
  SYNC_DIAGNOSTIC_KINDS,
  SYNC_DIAGNOSTICS_TRAIL_CONTRACT,
  SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
  SYNC_MULTIPART_FAILURE_STAGE_TOKENS,
  SYNC_PARK_SITE_TOKENS,
  SYNC_SELF_CHECK_VERDICT_TOKENS,
  SYNC_STARTUP_STAGE_TOKENS,
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

  async list(): Promise<readonly string[]> {
    this.accessedFileNames.push("list");
    return [...this.files.keys()];
  }
}

const REQUEST_ID = "66666666-6666-4666-8666-666666666666";

/**
 * The request-id token of the canonical test UUID; a null gate answer fails
 * the calling test (the gate admits this exact canonical shape).
 */
function canonicalRequestIdToken() {
  const token = envelopeRequestId(REQUEST_ID);
  if (token === null) {
    throw new Error("expected a request-id token for the canonical test UUID");
  }
  return token;
}

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
      tokens: ["server_error", canonicalRequestIdToken()],
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

  it("rejects a fabricated snake_case reason token during persisted reload", async () => {
    const store = new FakeTrailFileStore();
    store.files.set(
      SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
      new TextEncoder().encode(
        JSON.stringify({
          contract: "obsidian_sync_diagnostics_trail/v1",
          entries: [
            {
              kind: "journal_failure",
              at_epoch_ms: 1_784_000_000_000,
              tokens: ["made_up_reason"],
            },
          ],
        }),
      ).buffer as ArrayBuffer,
    );

    const trail = await createLoadedTrail(store);

    expect(trail.readEntries()).toEqual([
      {
        kind: "trail_reset",
        atEpochMs: 1_784_000_000_000,
        tokens: [],
      },
    ]);
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries().map((entry) => entry.kind)).toEqual(["trail_reset"]);
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
    store.readBinary = async () => {
      throw new Error("adapter failure");
    };
    const trail = await createLoadedTrail(store);
    expect(trail.readEntries().map((entry) => entry.kind)).toEqual(["trail_reset"]);
  });
});

// --- the trail contract v2 (device cursor and manifest reconciliation task 7) --------------------------

/** One well-formed v1 sidecar: two known entries, nothing foreign. */
const V1_TRAIL_DOCUMENT = JSON.stringify({
  contract: "obsidian_sync_diagnostics_trail/v1",
  entries: [
    { kind: "pass_outcome", at_epoch_ms: 1_784_000_000_000, tokens: ["completed"] },
    {
      kind: "wire_failure",
      at_epoch_ms: 1_784_000_000_001,
      tokens: ["server_error", { request_id: REQUEST_ID }],
    },
  ],
});

const V1_FIRST_ENTRY = {
  kind: "pass_outcome",
  atEpochMs: 1_784_000_000_000,
  tokens: ["completed"],
} as const;

/** Seed one fake store with a raw sidecar body. */
function seededTrailStore(sidecarBody: string): FakeTrailFileStore {
  const store = new FakeTrailFileStore();
  store.files.set(
    SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
    new TextEncoder().encode(sidecarBody).buffer as ArrayBuffer,
  );
  return store;
}

/** Parse the persisted sidecar record of one fake store. */
function parsePersisted(store: FakeTrailFileStore): { contract: string; entries: unknown[] } {
  const bytes = store.files.get(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME);
  if (bytes === undefined) {
    throw new Error("expected a persisted trail sidecar");
  }
  return JSON.parse(new TextDecoder().decode(bytes)) as { contract: string; entries: unknown[] };
}

describe("sync diagnostics trail contract v2 (device cursor and manifest reconciliation task 7)", () => {
  it("pins the v2 contract identifier", () => {
    expect(SYNC_DIAGNOSTICS_TRAIL_CONTRACT).toBe("obsidian_sync_diagnostics_trail/v2");
  });

  it("loads v1 and rewrites known entries as v2", async () => {
    const store = seededTrailStore(V1_TRAIL_DOCUMENT);
    const trail = createSyncDiagnosticsTrail({ fileStore: store });
    await trail.load();
    await trail.append({ kind: "cursor_failure", tokens: ["pull", "device_cursor_gap"] });
    expect(parsePersisted(store).contract).toBe("obsidian_sync_diagnostics_trail/v2");
    expect(trail.readEntries()[0]).toEqual(V1_FIRST_ENTRY);
  });

  it("keeps every loaded v1 entry losslessly through the v2 rewrite", async () => {
    const store = seededTrailStore(V1_TRAIL_DOCUMENT);
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "credential_failure", tokens: ["access_missing", "login_required"] });
    const reloaded = await createLoadedTrail(store);
    // The v1 wire entry (closed kind, closed token, gated request id) and
    // the appended v2 entry survive byte-for-byte at the record level.
    expect(reloaded.readEntries()).toEqual([
      { kind: "pass_outcome", atEpochMs: 1_784_000_000_000, tokens: ["completed"] },
      {
        kind: "wire_failure",
        atEpochMs: 1_784_000_000_001,
        tokens: ["server_error", { requestId: REQUEST_ID }],
      },
      {
        kind: "credential_failure",
        atEpochMs: 1_784_000_000_000,
        tokens: ["access_missing", "login_required"],
      },
    ]);
  });

  it("admits the five new kinds into the closed kind vocabulary", () => {
    expect(SYNC_DIAGNOSTIC_KINDS).toEqual([
      "wire_failure",
      "pass_outcome",
      "journal_failure",
      "publish_failure",
      "trail_reset",
      "self_check",
      "startup_failure",
      "credential_failure",
      "cursor_failure",
      "apply_failure",
      "reconcile_failure",
      "composition_read_failure",
      "multipart_failure",
    ]);
  });

  it("persists and reloads every new kind with its stage and reason tokens", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "cursor_failure", tokens: ["acknowledge", "device_cursor_ack_ahead"] });
    await trail.append({
      kind: "apply_failure",
      tokens: ["vault_mutation", "device_apply_vault_failed"],
    });
    await trail.append({
      kind: "reconcile_failure",
      tokens: ["finalize", "device_manifest_digest_mismatch"],
    });
    await trail.append({ kind: "credential_failure", tokens: ["refresh_failed", "login_required"] });
    await trail.append({
      kind: "composition_read_failure",
      tokens: ["note_status_read", "note_status_read_failed"],
    });
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries().map((entry) => [entry.kind, ...entry.tokens])).toEqual([
      ["cursor_failure", "acknowledge", "device_cursor_ack_ahead"],
      ["apply_failure", "vault_mutation", "device_apply_vault_failed"],
      ["reconcile_failure", "finalize", "device_manifest_digest_mismatch"],
      ["credential_failure", "refresh_failed", "login_required"],
      ["composition_read_failure", "note_status_read", "note_status_read_failed"],
    ]);
  });

  it("admits every device-sync closed reason and stage as a trail token", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    // One member of each new family type-checks and round-trips as a token.
    await trail.append({ kind: "cursor_failure", tokens: ["pull", "device_sync_dependency_unavailable"] });
    await trail.append({
      kind: "apply_failure",
      tokens: ["verify_temp", "device_manifest_identity_ambiguous"],
    });
    await trail.append({
      kind: "reconcile_failure",
      tokens: ["page", "device_apply_recovery_ambiguous"],
    });
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries().map((entry) => [...entry.tokens])).toEqual([
      ["pull", "device_sync_dependency_unavailable"],
      ["verify_temp", "device_manifest_identity_ambiguous"],
      ["page", "device_apply_recovery_ambiguous"],
    ]);
  });

  it("resets a v2 sidecar carrying a foreign kind, stage or reason", async () => {
    for (const foreignBody of [
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v2",
        entries: [{ kind: "cursor_failing", at_epoch_ms: 1, tokens: ["pull", "device_cursor_gap"] }],
      }),
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v2",
        entries: [
          { kind: "cursor_failure", at_epoch_ms: 1, tokens: ["preflight", "device_cursor_gap"] },
        ],
      }),
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v2",
        entries: [
          { kind: "apply_failure", at_epoch_ms: 1, tokens: ["download", "device_made_up_reason"] },
        ],
      }),
    ]) {
      const store = seededTrailStore(foreignBody);
      const trail = await createLoadedTrail(store);
      expect(trail.readEntries().map((entry) => entry.kind)).toEqual(["trail_reset"]);
    }
  });
});

// --- append failures never escape -------------------------------------------------------------------

describe("sync diagnostics trail append failure handling", () => {
  it("swallows persist failures into the bounded counter and never rejects", async () => {
    const store = new FakeTrailFileStore();
    store.writeThrows = true;
    const trail = await createLoadedTrail(store);
    await expect(trail.append({ kind: "pass_outcome", tokens: ["completed"] })).resolves.toBeUndefined();
    expect(trail.readAppendFailureCount()).toBe(1);
    // Child six remediation: the swallowed failure also records ONE bounded
    // `self_check · trail_persist_failed` marker entry after the appended
    // entry, so the failure is readable on the trail surfaces.
    expect(trail.readEntries().map((entry) => entry.kind)).toEqual([
      "pass_outcome",
      "self_check",
    ]);

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
    // recovery writes the whole ring, so nothing observed is lost. The
    // persist-failure marker (child six remediation) rides along as an
    // honest durable record of the failed episode.
    expect(reloaded.readEntries().map((entry) => entry.kind)).toEqual([
      "pass_outcome",
      "self_check",
      "wire_failure",
    ]);
  });
});

// --- the persist-failure marker (child six deferred remediation) -----------------------------------------

describe("sync diagnostics trail persist-failure marker (child six deferred remediation)", () => {
  /** Render one entry's tokens as strings (the request-id token as its id) for assertions. */
  function tokenTexts(entry: { readonly tokens: readonly unknown[] }): readonly string[] {
    return entry.tokens.map((token) =>
      typeof token === "string" ? token : `request_id=${(token as { requestId: string }).requestId}`,
    );
  }

  it("records one bounded trail_persist_failed marker per failure episode", async () => {
    const store = new FakeTrailFileStore();
    store.writeThrows = true;
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "pass_outcome", tokens: ["completed"] });

    expect(trail.readAppendFailureCount()).toBe(1);
    expect(trail.readEntries().map((entry) => [entry.kind, ...tokenTexts(entry)])).toEqual([
      ["pass_outcome", "completed"],
      ["self_check", "trail_persist_failed"],
    ]);

    // Further failures inside the SAME episode count on the bounded counter
    // but never repeat the marker — one bounded token per episode.
    await trail.append({ kind: "pass_outcome", tokens: ["stopped"] });
    expect(trail.readAppendFailureCount()).toBe(2);
    expect(
      trail.readEntries().filter((entry) => entry.tokens.includes("trail_persist_failed")),
    ).toHaveLength(1);
  });

  it("rides the marker into the sidecar on the next successful persist and re-arms after recovery", async () => {
    const store = new FakeTrailFileStore();
    store.writeThrows = true;
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "pass_outcome", tokens: ["completed"] });
    store.writeThrows = false;
    await trail.append({ kind: "wire_failure", tokens: ["server_error"] });

    // The marker is an honest durable record: the first successful persist
    // after the failure writes the whole ring, marker included.
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries().map((entry) => entry.kind)).toEqual([
      "pass_outcome",
      "self_check",
      "wire_failure",
    ]);

    // A NEW failure after the recovery records a fresh marker episode.
    store.writeThrows = true;
    await trail.append({ kind: "pass_outcome", tokens: ["stopped"] });
    expect(
      trail.readEntries().filter((entry) => entry.tokens.includes("trail_persist_failed")),
    ).toHaveLength(2);
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
  it("admits a declared server envelope code and nulls foreign or non-conforming codes", () => {
    // These tokens are the plugin-consumed subset of the server registry's
    // closed error-code vocabulary. Shape alone never admits a foreign code.
    expect(envelopeErrorCode("exclusion_policy_denied")).toBe("exclusion_policy_denied");
    expect(envelopeErrorCode("authorization_scope_denied")).toBe("authorization_scope_denied");
    expect(envelopeErrorCode("completed")).toBeNull();
    expect(envelopeErrorCode("made_up_reason")).toBeNull();
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
      tokens: ["login_required", codeToken, canonicalRequestIdToken()],
    });

    const reloaded = await createLoadedTrail(store);
    // The code survives the reload as a declared closed string token.
    expect(reloaded.readEntries()[0]?.tokens).toEqual([
      "login_required",
      "authorization_scope_denied",
      { requestId: REQUEST_ID },
    ]);
  });
});

// --- the envelope request-id UUID gate (child six deferred remediation) ---------------------------------

describe("sync diagnostics trail envelope request-id UUID gate (child six deferred remediation)", () => {
  it("rejects a non-UUID request id before durable trail persistence", () => {
    expect(envelopeRequestId("untrusted-value")).toBeNull();
  });

  it("admits only canonical lowercase-hex UUIDs as request-id tokens", () => {
    // The one canonical shape the sidecar parser already accepts.
    expect(envelopeRequestId(REQUEST_ID)).toEqual({ requestId: REQUEST_ID });
    const lowercaseHexUuidWithLetters = "0abcdef0-0abc-4abc-8abc-0abcdef0abcd";
    expect(envelopeRequestId(lowercaseHexUuidWithLetters)).toEqual({
      requestId: lowercaseHexUuidWithLetters,
    });
    // Every non-canonical shape — free-form text, an injected fragment, a
    // UUID with uppercase hex, a braced/urn form, an empty value — is
    // rejected at the token boundary, before any entry exists.
    for (const nonUuidValue of [
      "untrusted-value",
      "notes/leaked-path.md · " + REQUEST_ID,
      lowercaseHexUuidWithLetters.toUpperCase(),
      `{${REQUEST_ID}}`,
      `urn:uuid:${REQUEST_ID}`,
      REQUEST_ID.slice(0, 35),
      `${REQUEST_ID} `,
      "",
    ]) {
      expect(envelopeRequestId(nonUuidValue)).toBeNull();
    }
  });

  it("round-trips the gated request-id token through the sidecar unchanged", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    const requestIdToken = envelopeRequestId(REQUEST_ID);
    if (requestIdToken === null) {
      throw new Error("expected a request-id token for a canonical UUID");
    }
    await trail.append({ kind: "pass_outcome", tokens: ["completed", requestIdToken] });
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries()[0]?.tokens).toEqual([
      "completed",
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
      tokens: ["server_error", canonicalRequestIdToken()],
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

// --- the startup_failure kind and composition tokens (closed-reason surfacing C1) ---------------------

describe("sync diagnostics trail startup_failure entries (closed-reason surfacing C1 P1)", () => {
  it("pins the fixed startup stage token vocabulary", () => {
    expect(SYNC_STARTUP_STAGE_TOKENS).toEqual([
      "engine_load",
      "wasm_read",
      "journal_recovery",
      "other",
    ]);
  });

  it("pins the fixed composition read-failure token vocabulary (C1 P5)", () => {
    expect(SYNC_COMPOSITION_READ_FAILURE_TOKENS).toEqual([
      "status_read_failed",
      "note_status_read_failed",
      "retry_schedule_read_failed",
      "sync_status_read_failed",
      "queue_drain_failed",
      "snapshot_drain_failed",
      "settled_admission_failed",
      "automatic_snapshot_admission_failed",
      "lifecycle_reconcile_persist_failed",
      "restore_reservation_persist_failed",
    ]);
  });

  it("accepts the explicit-restore reservation refusal tokens as closed trail tokens", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "journal_failure", tokens: ["restore_target_occupied"] });
    await trail.append({ kind: "journal_failure", tokens: ["restore_target_busy"] });
    await trail.append({
      kind: "journal_failure",
      tokens: ["restore_already_pending", "restore_reservation_persist_failed"],
    });
    expect(trail.readEntries().map((entry) => entry.tokens)).toEqual([
      ["restore_target_occupied"],
      ["restore_target_busy"],
      ["restore_already_pending", "restore_reservation_persist_failed"],
    ]);
  });

  it("admits the startup_failure kind into the closed kind vocabulary", () => {
    expect(SYNC_DIAGNOSTIC_KINDS).toContain("startup_failure");
  });

  it("persists and reloads a startup_failure entry carrying the stage and store reason tokens", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "startup_failure", tokens: ["engine_load"] });
    await trail.append({
      kind: "startup_failure",
      tokens: ["journal_recovery", "journal_schema_unsupported"],
    });
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries().map((entry) => [entry.kind, ...entry.tokens])).toEqual([
      ["startup_failure", "engine_load"],
      ["startup_failure", "journal_recovery", "journal_schema_unsupported"],
    ]);
  });

  it("accepts pass_wrapper_failed and the two read-failure tokens as closed trail tokens", async () => {
    // C1 P2/P5: the wrapper's honest pass outcome and the two bounded
    // read-swallow tokens type-check against the closed vocabulary.
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "pass_outcome", tokens: ["pass_wrapper_failed"] });
    await trail.append({ kind: "journal_failure", tokens: ["status_read_failed"] });
    await trail.append({ kind: "journal_failure", tokens: ["note_status_read_failed"] });
    expect(trail.readEntries().map((entry) => entry.tokens[0])).toEqual([
      "pass_wrapper_failed",
      "status_read_failed",
      "note_status_read_failed",
    ]);
  });
});

// --- the multipart_failure kind (resumable multipart mobile upload task 11) ---------------------------

describe("sync diagnostics trail multipart_failure entries (multipart task 11)", () => {
  it("pins the closed three-stage multipart failure vocabulary", () => {
    expect(SYNC_MULTIPART_FAILURE_STAGE_TOKENS).toEqual([
      "multipart_resume",
      "multipart_verify",
      "multipart_cleanup",
    ]);
  });

  it("admits the multipart_failure kind into the closed kind vocabulary", () => {
    expect(SYNC_DIAGNOSTIC_KINDS).toContain("multipart_failure");
  });

  it("persists and reloads a multipart_failure entry carrying its stage and closed reason", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({
      kind: "multipart_failure",
      tokens: ["multipart_resume", "multipart_session_expired"],
    });
    await trail.append({
      kind: "multipart_failure",
      tokens: ["multipart_verify", "multipart_part_url_rejected"],
    });
    await trail.append({
      kind: "multipart_failure",
      tokens: ["multipart_cleanup", "multipart_cleanup_failed"],
    });
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries().map((entry) => [entry.kind, ...entry.tokens])).toEqual([
      ["multipart_failure", "multipart_resume", "multipart_session_expired"],
      ["multipart_failure", "multipart_verify", "multipart_part_url_rejected"],
      ["multipart_failure", "multipart_cleanup", "multipart_cleanup_failed"],
    ]);
  });

  it("admits every closed multipart safe-reason token as a multipart_failure token", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    for (const reasonToken of MULTIPART_SAFE_REASON_TOKENS) {
      await trail.append({ kind: "multipart_failure", tokens: ["multipart_verify", reasonToken] });
    }
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries()).toHaveLength(MULTIPART_SAFE_REASON_TOKENS.length);
    expect(reloaded.readEntries().map((entry) => entry.tokens[1])).toEqual([
      ...MULTIPART_SAFE_REASON_TOKENS,
    ]);
  });

  it("accepts the closed wire failure kinds and reason_unknown alongside a multipart stage", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({ kind: "multipart_failure", tokens: ["multipart_cleanup", "network_offline"] });
    await trail.append({ kind: "multipart_failure", tokens: ["multipart_verify", "server_error"] });
    await trail.append({ kind: "multipart_failure", tokens: ["multipart_cleanup", "reason_unknown"] });
    const reloaded = await createLoadedTrail(store);
    expect(reloaded.readEntries().map((entry) => [...entry.tokens])).toEqual([
      ["multipart_cleanup", "network_offline"],
      ["multipart_verify", "server_error"],
      ["multipart_cleanup", "reason_unknown"],
    ]);
  });

  it("resets a sidecar carrying a foreign multipart stage token", async () => {
    for (const foreignBody of [
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v2",
        entries: [
          { kind: "multipart_failure", at_epoch_ms: 1, tokens: ["resume", "multipart_session_expired"] },
        ],
      }),
      JSON.stringify({
        contract: "obsidian_sync_diagnostics_trail/v2",
        entries: [
          { kind: "multipart_failure", at_epoch_ms: 1, tokens: ["multipart_nonsense"] },
        ],
      }),
    ]) {
      const store = seededTrailStore(foreignBody);
      const trail = await createLoadedTrail(store);
      expect(trail.readEntries().map((entry) => entry.kind)).toEqual(["trail_reset"]);
    }
  });

  it("persists multipart_failure records free of multipart identity sentinels", async () => {
    const store = new FakeTrailFileStore();
    const trail = await createLoadedTrail(store);
    await trail.append({
      kind: "multipart_failure",
      tokens: ["multipart_verify", "multipart_local_content_changed"],
    });
    await trail.append({ kind: "multipart_failure", tokens: ["multipart_cleanup", "network_offline"] });
    const sidecarText = new TextDecoder().decode(
      store.files.get(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME) ?? new ArrayBuffer(0),
    );
    for (const forbiddenText of [
      "sentinel-etag",
      "provider_upload_id",
      "staging",
      "X-Amz",
      "https://",
      "notes/",
      "signature",
      ".md",
      "uploadId",
    ]) {
      expect(sidecarText).not.toContain(forbiddenText);
    }
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

describe("sync error tracing runbook token contract", () => {
  it("documents every journal orchestration failure token for operators", () => {
    const runbookText = readFileSync(
      new URL("../../../../docs/operations/sync-error-tracing.md", import.meta.url),
      "utf8",
    );

    for (const token of SYNC_COMPOSITION_READ_FAILURE_TOKENS) {
      expect(runbookText).toContain(token);
    }
  });
});

describe("lifecycle runbook reservation token contract", () => {
  it("documents every explicit-restore reservation refusal token for operators", async () => {
    const runbookText = readFileSync(
      new URL(
        "../../../../docs/operations/source-locator-tombstone-lifecycle.md",
        import.meta.url,
      ),
      "utf8",
    );
    const { RESTORE_RESERVATION_REFUSALS } = await import("./lifecycle-contracts");
    for (const token of RESTORE_RESERVATION_REFUSALS) {
      expect(runbookText).toContain(token);
    }
  });
});
