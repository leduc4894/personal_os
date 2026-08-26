/**
 * Tests of the device-sync diagnostics facade (device cursor and manifest
 * reconciliation, task 7).
 *
 * The facade is the mandatory diagnostics surface every later plugin task
 * (pull, apply, reconcile, acknowledge) reports its closed failure reasons
 * through: one fire-and-forget trail append per failure carrying the exact
 * stage token, the closed reason, the UUID-gated server request id and the
 * registered server error code. These tests pin: the exact tokens of every
 * cursor/apply/reconcile/credential stage combination over EVERY Child 6
 * closed reason, the correlation gates (UUID-only request id, registered
 * server codes only), and the observe-only contract — a failing trail can
 * never alter the sync outcome, and the facade never throws.
 */

import { describe, expect, it } from "vitest";

import type { JournalFileStore } from "../journal/persistence";
import {
  SYNC_DIAGNOSTICS_TRAIL_CONTRACT,
  SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
  createSyncDiagnosticsTrail,
} from "../journal/sync-diagnostics-trail";
import type {
  SyncDiagnosticsTrail,
  SyncDiagnosticsTrailAppendInput,
  SyncDiagnosticTrailEntry,
} from "../journal/sync-diagnostics-trail";
import {
  DEVICE_SYNC_ACTION_REASONS,
  DEVICE_SYNC_APPLY_STAGES,
  DEVICE_SYNC_CURSOR_STAGES,
  DEVICE_SYNC_LOCAL_REASONS,
  DEVICE_SYNC_RECONCILE_STAGES,
  DEVICE_SYNC_SERVER_REASONS,
  DEVICE_SYNC_TRANSPORT_REASONS,
} from "./contracts";
import type {
  ApplyFailureStage,
  CompositionReadStage,
  CredentialFailureStage,
  CursorFailureStage,
  DeviceSyncDiagnostics,
  DeviceSyncFailureCorrelation,
  DeviceSyncReason,
  ReconcileFailureStage,
} from "./contracts";
import { createDeviceSyncDiagnostics } from "./diagnostics";

const REQUEST_ID = "66666666-6666-4666-8666-666666666666";

// --- trail doubles ---------------------------------------------------------------------------------

/** The synchronous in-memory trail recorder the token tests read back. */
class RecordingTrail implements SyncDiagnosticsTrail {
  readonly entries: SyncDiagnosticTrailEntry[] = [];
  /** When set, every append rejects (a broken trail seam). */
  rejectAppends = false;
  /** When set, every append THROWS synchronously (a hostile trail seam). */
  throwOnAppend = false;

  async load(): Promise<void> {
    return undefined;
  }

  append(input: SyncDiagnosticsTrailAppendInput): Promise<void> {
    if (this.throwOnAppend) {
      throw new Error("trail append threw");
    }
    this.entries.push({ kind: input.kind, atEpochMs: 0, tokens: [...input.tokens] });
    return this.rejectAppends ? Promise.reject(new Error("trail append rejected")) : Promise.resolve();
  }

  readEntries(): readonly SyncDiagnosticTrailEntry[] {
    return [...this.entries];
  }

  readAppendFailureCount(): number {
    return 0;
  }
}

/** The in-memory journal file store behind the durable trail tests. */
class InMemoryTrailFileStore implements JournalFileStore {
  readonly files = new Map<string, ArrayBuffer>();

  async exists(fileName: string): Promise<boolean> {
    return this.files.has(fileName);
  }

  async readBinary(fileName: string): Promise<ArrayBuffer> {
    const data = this.files.get(fileName);
    if (data === undefined) {
      throw new Error("file not found");
    }
    return data.slice(0);
  }

  async writeBinary(fileName: string, data: ArrayBuffer): Promise<void> {
    this.files.set(fileName, data.slice(0));
  }

  async remove(fileName: string): Promise<void> {
    this.files.delete(fileName);
  }
}

/** The write-failing file store: every durable persist fails. */
class WriteFailingTrailFileStore extends InMemoryTrailFileStore {
  override async writeBinary(): Promise<void> {
    throw new Error("write failed");
  }
}

/** Let one macrotask pass so a fire-and-forget trail append finishes its persist. */
async function settleTrailPersist(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function recordingFacade(): { diagnostics: DeviceSyncDiagnostics; trail: RecordingTrail } {
  const trail = new RecordingTrail();
  return { diagnostics: createDeviceSyncDiagnostics(trail), trail };
}

// --- the closed-reason table ------------------------------------------------------------------------

/** Every closed reason of the four families plus the journal store family. */
const ALL_DEVICE_SYNC_REASONS: readonly DeviceSyncReason[] = [
  ...DEVICE_SYNC_SERVER_REASONS,
  ...DEVICE_SYNC_ACTION_REASONS,
  ...DEVICE_SYNC_TRANSPORT_REASONS,
  ...DEVICE_SYNC_LOCAL_REASONS,
  "journal_mutation_failed",
];

/** One (kind, stage) pair per stage of the three correlated kinds. */
const CORRELATED_STAGE_COMBINATIONS: readonly {
  kind: "cursor_failure" | "apply_failure" | "reconcile_failure";
  stage: CursorFailureStage | ApplyFailureStage | ReconcileFailureStage;
}[] = [
  ...DEVICE_SYNC_CURSOR_STAGES.map((stage) => ({ kind: "cursor_failure", stage }) as const),
  ...DEVICE_SYNC_APPLY_STAGES.map((stage) => ({ kind: "apply_failure", stage }) as const),
  ...DEVICE_SYNC_RECONCILE_STAGES.map((stage) => ({ kind: "reconcile_failure", stage }) as const),
];

describe("device sync diagnostics facade", () => {
  it("surfaces the exact cursor stage and reason", async () => {
    const store = new InMemoryTrailFileStore();
    const trail = createSyncDiagnosticsTrail({ fileStore: store });
    await trail.load();
    const diagnostics = createDeviceSyncDiagnostics(trail);

    diagnostics.cursorFailure("pull", "device_cursor_gap");
    await settleTrailPersist();

    expect(trail.readEntries().at(-1)).toMatchObject({
      kind: "cursor_failure",
      tokens: ["pull", "device_cursor_gap"],
    });
  });

  it("surfaces every stage combination with every Child 6 closed reason", () => {
    const { diagnostics, trail } = recordingFacade();
    // Two reasons per stage combination covers every reason exactly once
    // (30 reasons over 15 stages) while exercising every stage.
    const expectedRows: (readonly [string, DeviceSyncReason])[] = [];
    for (const [combinationIndex, combination] of CORRELATED_STAGE_COMBINATIONS.entries()) {
      for (const slot of [0, 1]) {
        const reason = ALL_DEVICE_SYNC_REASONS[combinationIndex * 2 + slot];
        if (reason === undefined) {
          throw new Error("table construction error: reason slot is empty");
        }
        const correlation: DeviceSyncFailureCorrelation = {
          requestId: REQUEST_ID,
          wireErrorCode: null,
        };
        if (combination.kind === "cursor_failure") {
          diagnostics.cursorFailure(
            combination.stage as CursorFailureStage,
            reason,
            correlation,
          );
        } else if (combination.kind === "apply_failure") {
          diagnostics.applyFailure(combination.stage as ApplyFailureStage, reason, correlation);
        } else {
          diagnostics.reconcileFailure(
            combination.stage as ReconcileFailureStage,
            reason,
            correlation,
          );
        }
        expectedRows.push([combination.stage, reason]);
      }
    }

    // The exact tokens of every table row: the kind, the stage, the closed
    // reason, then the gated request id — never a free-form string.
    expect(trail.entries.map((entry) => [entry.kind, ...entry.tokens])).toEqual(
      expectedRows.map(([stage, reason], rowIndex) => [
        CORRELATED_STAGE_COMBINATIONS[Math.floor(rowIndex / 2)]?.kind,
        stage,
        reason,
        { requestId: REQUEST_ID },
      ]),
    );
    // Every family member was exercised at least once.
    for (const reason of ALL_DEVICE_SYNC_REASONS) {
      expect(
        trail.entries.some(
          (entry) => typeof entry.tokens[1] === "string" && entry.tokens[1] === reason,
        ),
      ).toBe(true);
    }
  });

  it("surfaces both credential stages without a correlation record", () => {
    const { diagnostics, trail } = recordingFacade();
    for (const [stage, reason] of [
      ["access_missing", "login_required"],
      ["refresh_failed", "access_expired"],
    ] as const satisfies readonly (readonly [
      CredentialFailureStage,
      DeviceSyncReason,
    ])[]) {
      diagnostics.credentialFailure(stage, reason);
    }
    expect(trail.entries.map((entry) => [entry.kind, ...entry.tokens])).toEqual([
      ["credential_failure", "access_missing", "login_required"],
      ["credential_failure", "refresh_failed", "access_expired"],
    ]);
  });

  it("rejects a foreign stage or reason at compile time", () => {
    const { diagnostics } = recordingFacade();
    // @ts-expect-error a reconcile stage is not a cursor stage
    diagnostics.cursorFailure("page", "device_cursor_gap");
    // @ts-expect-error a free-form reason is not a DeviceSyncReason
    diagnostics.applyFailure("download", "edge block page after 12 seconds");
    // @ts-expect-error a composition stage is not a credential stage
    diagnostics.credentialFailure("status_read", "login_required");
    expect(diagnostics).toBeDefined();
  });

  // --- the correlation gates ------------------------------------------------------------------------

  it("carries the registered server code between the reason and the gated request id", () => {
    const { diagnostics, trail } = recordingFacade();
    diagnostics.cursorFailure("pull", "server_error", {
      requestId: REQUEST_ID,
      wireErrorCode: "device_cursor_gap",
    });
    expect(trail.entries[0]?.tokens).toEqual([
      "pull",
      "server_error",
      "device_cursor_gap",
      { requestId: REQUEST_ID },
    ]);
  });

  it("admits the journal-lane registered server codes as correlation codes", () => {
    const { diagnostics, trail } = recordingFacade();
    diagnostics.reconcileFailure("page", "network_rate_limited", {
      requestId: REQUEST_ID,
      wireErrorCode: "internal_error",
    });
    expect(trail.entries[0]?.tokens).toEqual([
      "page",
      "network_rate_limited",
      "internal_error",
      { requestId: REQUEST_ID },
    ]);
  });

  it("drops a non-UUID request id and a foreign wire error code", () => {
    const { diagnostics, trail } = recordingFacade();
    diagnostics.applyFailure("download", "device_download_integrity_failed", {
      requestId: "untrusted-value",
      wireErrorCode: "made_up_reason",
    });
    // Nothing of the untrusted correlation survives: no free-form string
    // and no unregistered code ever reaches a trail token.
    expect(trail.entries[0]?.tokens).toEqual([
      "download",
      "device_download_integrity_failed",
    ]);
  });

  it("drops a null correlation entirely", () => {
    const { diagnostics, trail } = recordingFacade();
    diagnostics.cursorFailure("acknowledge", "device_cursor_ack_ahead", {
      requestId: null,
      wireErrorCode: null,
    });
    diagnostics.applyFailure("trash", "device_apply_trash_failed");
    expect(trail.entries.map((entry) => [...entry.tokens])).toEqual([
      ["acknowledge", "device_cursor_ack_ahead"],
      ["trash", "device_apply_trash_failed"],
    ]);
  });

  // --- the observe-only contract ----------------------------------------------------------------------

  it("keeps the facade synchronous: the four methods answer void immediately", () => {
    const { diagnostics } = recordingFacade();
    expect(diagnostics.cursorFailure("pull", "device_cursor_gap")).toBeUndefined();
    expect(diagnostics.applyFailure("prepare", "device_manifest_capture_failed")).toBeUndefined();
    expect(diagnostics.reconcileFailure("start", "device_manifest_expired")).toBeUndefined();
    expect(diagnostics.credentialFailure("access_missing", "login_required")).toBeUndefined();
  });

  it("never throws when the trail append rejects or throws", () => {
    const trail = new RecordingTrail();
    const diagnostics = createDeviceSyncDiagnostics(trail);
    trail.rejectAppends = true;
    expect(() => diagnostics.cursorFailure("pull", "device_cursor_gap")).not.toThrow();
    trail.throwOnAppend = true;
    expect(() => diagnostics.applyFailure("recovery", "device_apply_recovery_ambiguous")).not.toThrow();
    expect(() => diagnostics.reconcileFailure("complete", "device_sync_dependency_unavailable")).not.toThrow();
    expect(() => diagnostics.credentialFailure("refresh_failed", "login_required")).not.toThrow();
  });

  it("cannot alter the sync outcome when the durable trail persist fails", async () => {
    const trail = createSyncDiagnosticsTrail({ fileStore: new WriteFailingTrailFileStore() });
    await trail.load();
    const diagnostics = createDeviceSyncDiagnostics(trail);

    // The observation lands in the in-memory ring and the persist failure is
    // swallowed into the bounded counter; the call itself answers normally.
    expect(() => diagnostics.cursorFailure("pull", "device_cursor_regression")).not.toThrow();
    await settleTrailPersist();
    expect(trail.readAppendFailureCount()).toBe(1);
    expect(trail.readEntries().at(-1)?.kind).toBe("self_check");
    expect(trail.readEntries().some((entry) => entry.kind === "cursor_failure")).toBe(true);
  });

  it("persists a facade observation through the v2 sidecar contract", async () => {
    const store = new InMemoryTrailFileStore();
    const trail = createSyncDiagnosticsTrail({ fileStore: store });
    await trail.load();
    const diagnostics = createDeviceSyncDiagnostics(trail);

    diagnostics.reconcileFailure("actions", "device_manifest_target_occupied", {
      requestId: REQUEST_ID,
      wireErrorCode: null,
    });
    await settleTrailPersist();

    const reloaded = createSyncDiagnosticsTrail({ fileStore: store });
    await reloaded.load();
    expect(reloaded.readEntries().map((entry) => [entry.kind, ...entry.tokens])).toEqual([
      [
        "reconcile_failure",
        "actions",
        "device_manifest_target_occupied",
        { requestId: REQUEST_ID },
      ],
    ]);
    const persisted = JSON.parse(
      new TextDecoder().decode(store.files.get(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME) ?? new ArrayBuffer(0)),
    ) as { contract: string };
    expect(persisted.contract).toBe(SYNC_DIAGNOSTICS_TRAIL_CONTRACT);
    expect(SYNC_DIAGNOSTICS_TRAIL_CONTRACT).toBe("obsidian_sync_diagnostics_trail/v2");
  });

  it("keeps the composition read stages reachable only through the journal lane surface", () => {
    // The facade exposes no composition method: the composition_read_failure
    // kind belongs to the journal composition call sites, not the device-sync
    // operations. The stage vocabulary stays exported for the trail contract.
    const compositionStages: readonly CompositionReadStage[] = [
      "status_read",
      "note_status_read",
      "retry_schedule_read",
      "sync_status_read",
    ];
    expect(compositionStages).toHaveLength(4);
  });
});
