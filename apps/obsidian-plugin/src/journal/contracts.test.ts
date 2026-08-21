import { describe, expect, it } from "vitest";

import {
  FILE_SETTLE_DELAY_MS,
  JOURNAL_EVENT_STATES,
  JOURNAL_NON_RETRY_EVENT_STATES,
  JOURNAL_RECOVERY_STATES,
  JOURNAL_SAFE_ERROR_LABELS,
  MAX_FILE_SIZE_BYTES,
  MAX_JOURNAL_SIZE_BYTES,
  MAX_PENDING_EVENTS,
  QUEUE_OUTCOMES,
} from "./contracts";
import type {
  FrozenFingerprint,
  JournalEvent,
  JournalEventState,
  JournalMeta,
  JournalOperation,
  JournalRecoveryState,
  JournalSafeErrorLabel,
  LocalFile,
  QueueOutcome,
} from "./contracts";

describe("JOURNAL_EVENT_STATES closed set (spec 7.2)", () => {
  it("is exactly the eleven spec-7.2 event states", () => {
    expect([...JOURNAL_EVENT_STATES]).toEqual([
      "queued",
      "preflight",
      "uploading",
      "committed",
      "no_change",
      "waiting_retry",
      "excluded_policy",
      "blocked_size",
      "blocked_conflict",
      "deferred_lifecycle",
      "integrity_failed",
    ]);
  });

  it("rejects unknown event states at the type level", () => {
    const state: JournalEventState = "queued";
    expect(state).toBe("queued");
    // @ts-expect-error an unknown state token must stay unassignable
    const unknownState: JournalEventState = "paused";
    expect(unknownState).toBe("paused");
  });

  it("pins the five terminal states that never retry", () => {
    expect([...JOURNAL_NON_RETRY_EVENT_STATES]).toEqual([
      "excluded_policy",
      "blocked_size",
      "blocked_conflict",
      "deferred_lifecycle",
      "integrity_failed",
    ]);
    for (const terminalState of JOURNAL_NON_RETRY_EVENT_STATES) {
      expect(JOURNAL_EVENT_STATES).toContain(terminalState);
    }
  });
});

describe("JOURNAL_SAFE_ERROR_LABELS closed set (spec 12)", () => {
  it("mirrors the spec-12 error and retry matrix plus the success token", () => {
    expect([...JOURNAL_SAFE_ERROR_LABELS]).toEqual([
      "network_offline",
      "network_timeout",
      "network_rate_limited",
      "server_error",
      "login_required",
      "excluded_policy",
      "blocked_size",
      "blocked_conflict",
      "deferred_lifecycle",
      "integrity_failed",
      "reconcile_required",
      "committed",
    ]);
  });

  it("rejects unknown or leaking error labels at the type level", () => {
    const label: JournalSafeErrorLabel = "network_offline";
    expect(label).toBe("network_offline");
    // @ts-expect-error an unknown, detailed or provider-shaped label must stay unassignable
    const unknownLabel: JournalSafeErrorLabel = "disk I/O error near vault path";
    expect(unknownLabel).toBe("disk I/O error near vault path");
  });
});

describe("QUEUE_OUTCOMES closed set (spec 8, 12)", () => {
  it("is exactly the closed outcome set of one bounded queue pass", () => {
    expect([...QUEUE_OUTCOMES]).toEqual([
      "committed",
      "committed_replay",
      "no_change",
      "retry_scheduled",
      "resumable_suspended",
      "login_required",
      "excluded_policy",
      "blocked_size",
      "blocked_conflict",
      "deferred_lifecycle",
      "integrity_failed",
    ]);
  });

  it("rejects unknown queue outcomes at the type level", () => {
    const outcome: QueueOutcome = "committed";
    expect(outcome).toBe("committed");
    // @ts-expect-error an unknown outcome must stay unassignable
    const unknownOutcome: QueueOutcome = "uploaded";
    expect(unknownOutcome).toBe("uploaded");
  });
});

describe("JOURNAL_RECOVERY_STATES closed set (spec 6.2)", () => {
  it("is exactly the closed recovery state set", () => {
    expect([...JOURNAL_RECOVERY_STATES]).toEqual([
      "fresh_journal_created",
      "verified_generation_loaded",
      "prior_generation_recovered",
      "empty_journal_rebuilt",
    ]);
  });

  it("rejects unknown recovery states at the type level", () => {
    const recoveryState: JournalRecoveryState = "verified_generation_loaded";
    expect(recoveryState).toBe("verified_generation_loaded");
    // @ts-expect-error an unknown recovery state must stay unassignable
    const unknownRecoveryState: JournalRecoveryState = "corrupt";
    expect(unknownRecoveryState).toBe("corrupt");
  });
});

describe("journal operations (spec 6.3, child 5)", () => {
  it("allows create and update from the content surface", () => {
    const createOperation: JournalOperation = "create";
    const updateOperation: JournalOperation = "update";
    expect([createOperation, updateOperation]).toEqual(["create", "update"]);
  });

  it("admits the four lifecycle operations once child 5 lands", () => {
    const renameOperation: JournalOperation = "rename";
    const moveOperation: JournalOperation = "move";
    const deleteOperation: JournalOperation = "delete";
    const restoreOperation: JournalOperation = "restore";
    expect([renameOperation, moveOperation, deleteOperation, restoreOperation]).toEqual([
      "rename",
      "move",
      "delete",
      "restore",
    ]);
  });

  it("still rejects unknown operation tokens at the type level", () => {
    // @ts-expect-error an unknown operation must stay unassignable
    const forbiddenOperation: JournalOperation = "merge";
    expect(forbiddenOperation).toBe("merge");
  });
});

describe("frozen journal limits (spec 3.1, 6.4, 7.1)", () => {
  it("freezes the 16 MiB single-part file ceiling", () => {
    // This mirrors the server's Python pair — MAX_SINGLE_PART_FILE_SIZE_BYTES
    // (src/personal_os/small_file_sync/contracts.py) and the migration's
    // _MAXIMUM_DECLARED_SIZE_BYTES — which a Python pin test holds equal.
    // TypeScript cannot import them, so the value assertion below is the
    // plugin-side half of the cross-language ceiling pin.
    expect(MAX_FILE_SIZE_BYTES).toBe(16_777_216);
  });

  it("freezes the 10,000 pending-event soft limit", () => {
    expect(MAX_PENDING_EVENTS).toBe(10_000);
  });

  it("freezes the 64 MiB journal size soft limit", () => {
    expect(MAX_JOURNAL_SIZE_BYTES).toBe(67_108_864);
  });

  it("freezes the 250 ms per-file settle delay", () => {
    expect(FILE_SETTLE_DELAY_MS).toBe(250);
  });
});

describe("journal record shapes (spec 6.3)", () => {
  it("types one frozen content fingerprint with exact units", () => {
    const fingerprint: FrozenFingerprint = {
      sha256: "0123456789abcdef".repeat(4),
      sizeBytes: 1_024,
      mediaType: "text/markdown",
    };
    expect(fingerprint.sizeBytes).toBe(1_024);
    expect(fingerprint.mediaType).toBe("text/markdown");
  });

  it("types one local file record with the spec-6.3 fields", () => {
    const localFile: LocalFile = {
      localFileId: "1fbd21b0-0000-4000-8000-000000000001",
      normalizedPath: "notes/project-overview.md",
      sourceId: null,
      observedFingerprint: {
        sha256: "0123456789abcdef".repeat(4),
        sizeBytes: 1_024,
        mediaType: "text/markdown",
      },
      baseVersionId: null,
      policyRevisionNumber: 7,
      lastCommittedFingerprint: null,
    };
    expect(localFile.sourceId).toBeNull();
    expect(localFile.policyRevisionNumber).toBe(7);
    expect(localFile.lastCommittedFingerprint).toBeNull();
  });

  it("types one journal event with the spec-6.3 fields", () => {
    const event: JournalEvent = {
      eventId: "1fbd21b0-0000-4000-8000-000000000002",
      localFileId: "1fbd21b0-0000-4000-8000-000000000001",
      idempotencyKey: "1fbd21b0-0000-4000-8000-000000000003",
      operation: "update",
      fingerprint: {
        sha256: "0123456789abcdef".repeat(4),
        sizeBytes: 1_024,
        mediaType: "text/markdown",
      },
      state: "waiting_retry",
      attemptCount: 1,
      nextEligibleRetryEpochMs: 1_784_000_000_000,
      safeError: "network_offline",
      operationId: null,
    };
    expect(event.state).toBe("waiting_retry");
    expect(event.safeError).toBe("network_offline");
    expect(event.operationId).toBeNull();
  });

  it("types the journal meta record with the spec-6.3 fields", () => {
    const meta: JournalMeta = {
      schemaVersion: 1,
      dirtyGeneration: 4,
      lastVerifiedGeneration: 3,
      isReconcileRequired: false,
      recoveryState: "verified_generation_loaded",
    };
    expect(meta.isReconcileRequired).toBe(false);
    expect(meta.recoveryState).toBe("verified_generation_loaded");
  });
});
