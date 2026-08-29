/**
 * Tests of the bounded sync self-check (sync error tracing design:
 * self-check contract).
 *
 * One run executes three steps in order — the trail append-and-persist
 * probe, the credential-presence check and the origin-reachability probe —
 * and each step yields ONE closed verdict token appended to the durable
 * trail as a `self_check` entry. These tests pin: the ok verdicts and their
 * entry order, the persist-failure verdict, the boolean credential verdicts,
 * the closed network verdicts (offline and bounded timeout), the exactly
 * once probe (no retry loop), the trail-sidecar-only write surface (no sync
 * state is ever mutated), and the forbidden-substrate guarantees — no path,
 * credential, hostname or free-form string ever reaches a verdict, a trail
 * entry or the summary line.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import type { JournalFileStore } from "./persistence";
import { SYNC_DIAGNOSTICS_TRAIL_FILE_NAME, createSyncDiagnosticsTrail } from "./sync-diagnostics-trail";
import type { SyncDiagnosticsTrail } from "./sync-diagnostics-trail";
import {
  SYNC_SELF_CHECK_ORIGIN_PROBE_TIMEOUT_MS,
  renderSyncSelfCheckJournalNotRunningText,
  renderSyncSelfCheckSummaryText,
  runSyncSelfCheck,
} from "./sync-self-check";

// --- the fake journal file store ----------------------------------------------------------------

/** The fake journal directory the trail persists its sidecar into. */
class FakeSelfCheckFileStore implements JournalFileStore {
  readonly files = new Map<string, ArrayBuffer>();
  readonly accessedFileNames: string[] = [];
  writeThrows = false;

  async exists(fileName: string): Promise<boolean> {
    this.accessedFileNames.push(`exists:${fileName}`);
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
    if (this.writeThrows) {
      throw new Error("write failed");
    }
    this.files.set(fileName, data.slice(0));
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

/** Create one loaded trail over a fresh fake store. */
async function createLoadedTrail(store: FakeSelfCheckFileStore): Promise<SyncDiagnosticsTrail> {
  const trail = createSyncDiagnosticsTrail({ fileStore: store });
  await trail.load();
  return trail;
}

/** Render the trail entries as `[kind, ...tokenTexts]` rows for assertions. */
function entryRows(trail: SyncDiagnosticsTrail): readonly (readonly string[])[] {
  return trail.readEntries().map((entry) => [
    entry.kind,
    ...entry.tokens.map((token) => (typeof token === "string" ? token : `request_id=${token.requestId}`)),
  ]);
}

// --- the step verdicts -----------------------------------------------------------------------------

describe("runSyncSelfCheck step verdicts", () => {
  it("records the ok verdicts of all three steps and appends one self_check entry per step plus the probe", async () => {
    const store = new FakeSelfCheckFileStore();
    const trail = await createLoadedTrail(store);
    const summary = await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => true,
      probeOrigin: async () => undefined,
    });
    expect(summary.steps).toEqual([
      { step: "trail_persist", verdict: "trail_persist_ok", networkKind: null },
      { step: "credential_presence", verdict: "credential_present", networkKind: null },
      { step: "origin_reachability", verdict: "origin_reachable", networkKind: null },
    ]);
    // Step order is the append order: the persist probe entry first, then
    // each step's verdict entry.
    expect(entryRows(trail)).toEqual([
      ["self_check", "trail_probe"],
      ["self_check", "trail_persist_ok"],
      ["self_check", "credential_present"],
      ["self_check", "origin_reachable"],
    ]);
    // The probe exercised the real sidecar write path: a reload of the SAME
    // store keeps every self_check entry.
    const reloaded = await createLoadedTrail(store);
    expect(entryRows(reloaded)).toEqual([
      ["self_check", "trail_probe"],
      ["self_check", "trail_persist_ok"],
      ["self_check", "credential_present"],
      ["self_check", "origin_reachable"],
    ]);
  });

  it("records trail_persist_failed when the sidecar write path fails", async () => {
    const store = new FakeSelfCheckFileStore();
    store.writeThrows = true;
    const trail = await createLoadedTrail(store);
    const summary = await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => true,
      probeOrigin: async () => undefined,
    });
    expect(summary.steps[0]).toEqual({
      step: "trail_persist",
      verdict: "trail_persist_failed",
      networkKind: null,
    });
    // The swallowed persist failure was counted, and the failed verdict
    // still landed in the in-memory ring for the surfaces to read.
    expect(trail.readAppendFailureCount()).toBeGreaterThan(0);
    expect(entryRows(trail)).toContainEqual(["self_check", "trail_persist_failed"]);
  });

  it("records credential_absent from the boolean presence reader only", async () => {
    const store = new FakeSelfCheckFileStore();
    const trail = await createLoadedTrail(store);
    const summary = await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => false,
      probeOrigin: async () => undefined,
    });
    expect(summary.steps[1]).toEqual({
      step: "credential_presence",
      verdict: "credential_absent",
      networkKind: null,
    });
    expect(entryRows(trail)).toContainEqual(["self_check", "credential_absent"]);
  });

  it("records the closed network_offline verdict when the origin probe rejects", async () => {
    const store = new FakeSelfCheckFileStore();
    const trail = await createLoadedTrail(store);
    let probeCallCount = 0;
    const summary = await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => true,
      probeOrigin: async () => {
        probeCallCount += 1;
        throw new Error("origin did not answer");
      },
    });
    expect(summary.steps[2]).toEqual({
      step: "origin_reachability",
      verdict: "origin_unreachable",
      networkKind: "network_offline",
    });
    // The network kind label rides along as the second closed token.
    expect(entryRows(trail)).toContainEqual([
      "self_check",
      "origin_unreachable",
      "network_offline",
    ]);
    // No retry loop: the probe ran exactly once even though it failed.
    expect(probeCallCount).toBe(1);
  });

  it("records the closed network_timeout verdict when the origin probe hangs past the bound", async () => {
    const store = new FakeSelfCheckFileStore();
    const trail = await createLoadedTrail(store);
    let probeCallCount = 0;
    const summary = await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => true,
      probeOrigin: () => {
        probeCallCount += 1;
        return new Promise<void>(() => undefined);
      },
      originProbeTimeoutMs: 10,
    });
    expect(summary.steps[2]).toEqual({
      step: "origin_reachability",
      verdict: "origin_unreachable",
      networkKind: "network_timeout",
    });
    expect(entryRows(trail)).toContainEqual([
      "self_check",
      "origin_unreachable",
      "network_timeout",
    ]);
    expect(probeCallCount).toBe(1);
    // The default probe bound stays short and bounded.
    expect(SYNC_SELF_CHECK_ORIGIN_PROBE_TIMEOUT_MS).toBe(5_000);
  });

  it("keeps every step running after an earlier step failed", async () => {
    const store = new FakeSelfCheckFileStore();
    store.writeThrows = true;
    const trail = await createLoadedTrail(store);
    const summary = await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => false,
      probeOrigin: async () => {
        throw new Error("origin did not answer");
      },
    });
    expect(summary.steps.map((step) => step.verdict)).toEqual([
      "trail_persist_failed",
      "credential_absent",
      "origin_unreachable",
    ]);
  });

  it("touches only the trail sidecar file, never any sync state", async () => {
    const store = new FakeSelfCheckFileStore();
    const trail = await createLoadedTrail(store);
    store.accessedFileNames.length = 0;
    await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => true,
      probeOrigin: async () => undefined,
    });
    // The self-check holds no capability to mutate sync state: the only file
    // surface it can reach is the diagnostics sidecar itself.
    expect(store.accessedFileNames.length).toBeGreaterThan(0);
    for (const accessedFileName of store.accessedFileNames) {
      expect(accessedFileName.endsWith(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME)).toBe(true);
    }
  });
});

// --- the summary line --------------------------------------------------------------------------------

describe("renderSyncSelfCheckSummaryText", () => {
  it("renders the ok summary as one line of closed verdict tokens", () => {
    const text = renderSyncSelfCheckSummaryText({
      steps: [
        { step: "trail_persist", verdict: "trail_persist_ok", networkKind: null },
        { step: "credential_presence", verdict: "credential_present", networkKind: null },
        { step: "origin_reachability", verdict: "origin_reachable", networkKind: null },
      ],
    });
    expect(text).toBe(
      "Sync self-check: trail_persist_ok · credential_present · origin_reachable",
    );
  });

  it("renders the unreachable origin with its closed network kind", () => {
    const text = renderSyncSelfCheckSummaryText({
      steps: [
        { step: "trail_persist", verdict: "trail_persist_ok", networkKind: null },
        { step: "credential_presence", verdict: "credential_absent", networkKind: null },
        { step: "origin_reachability", verdict: "origin_unreachable", networkKind: "network_timeout" },
      ],
    });
    expect(text).toBe(
      "Sync self-check: trail_persist_ok · credential_absent · origin_unreachable · network_timeout",
    );
    for (const forbiddenText of [
      "notes/",
      ".md",
      "at1.",
      "Bearer",
      "authorization",
      "https://",
      "Error:",
    ]) {
      expect(text).not.toContain(forbiddenText);
    }
  });
});

describe("renderSyncSelfCheckJournalNotRunningText (closed-reason surfacing C1 P1)", () => {
  it("renders the fixed journal-not-running line alone before any startup failure", () => {
    expect(renderSyncSelfCheckJournalNotRunningText(null)).toBe(
      "Sync self-check unavailable: journal not running on this device.",
    );
  });

  it("renders the closed startup-failure tokens on the journal-not-running verdict", () => {
    const singleTokenText = renderSyncSelfCheckJournalNotRunningText(["wasm_read"]);
    expect(singleTokenText).toContain(
      "Sync self-check unavailable: journal not running on this device.",
    );
    expect(singleTokenText).toContain("wasm_read");

    const multiTokenText = renderSyncSelfCheckJournalNotRunningText([
      "journal_recovery",
      "journal_schema_unsupported",
    ]);
    expect(multiTokenText).toContain("journal_recovery, journal_schema_unsupported");
    // Closed tokens only: no exception text, path, credential or any other
    // free-form detail ever rides along with the verdict.
    for (const forbiddenText of ["Error:", "notes/", ".md", "at1.", "https://"]) {
      expect(multiTokenText).not.toContain(forbiddenText);
    }
  });
});

// --- the privacy source contract ----------------------------------------------------------------------

describe("sync self-check privacy source contract", () => {
  it("keeps the self-check module free of path-shaped and credential-shaped substrings", () => {
    const selfCheckSource = readFileSync(new URL("./sync-self-check.ts", import.meta.url), "utf8");
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
      expect(selfCheckSource).not.toContain(forbiddenText);
    }
  });
});
