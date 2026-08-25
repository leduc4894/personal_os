import { describe, expect, it } from "vitest";

import type { FrozenFingerprint, JournalEvent, JournalSafeErrorLabel } from "./contracts";
import { projectLocalNoteSyncStatus } from "./note-status";

const FINGERPRINT: FrozenFingerprint = {
  sha256: "a".repeat(64),
  sizeBytes: 42,
  mediaType: "text/markdown",
};

function event(
  state: JournalEvent["state"],
  overrides: Partial<JournalEvent> = {},
): JournalEvent {
  return {
    eventId: "11111111-1111-4111-8111-111111111111",
    localFileId: "22222222-2222-4222-8222-222222222222",
    idempotencyKey: "33333333-3333-4333-8333-333333333333",
    operation: "create",
    fingerprint: FINGERPRINT,
    state,
    attemptCount: 0,
    nextEligibleRetryEpochMs: null,
    safeError: null,
    operationId: null,
    ...overrides,
  };
}

function projectInput(
  latestEvent: JournalEvent,
  overrides: {
    readonly isReconcileRequired?: boolean;
    readonly observedFingerprint?: FrozenFingerprint;
    readonly policyRevisionNumber?: number;
    readonly lastCommittedFingerprint?: FrozenFingerprint | null;
  } = {},
) {
  return {
    normalizedPath: "notes/current.md",
    policyRevisionNumber: overrides.policyRevisionNumber ?? 7,
    observedFingerprint: overrides.observedFingerprint ?? FINGERPRINT,
    lastCommittedFingerprint: overrides.lastCommittedFingerprint ?? null,
    latestEvent,
    isReconcileRequired: overrides.isReconcileRequired ?? false,
  };
}

describe("local note sync status projection", () => {
  it.each([
    ["queued work", event("queued"), "queued"],
    ["preflight work", event("preflight"), "syncing"],
    ["uploading work", event("uploading"), "syncing"],
    ["the latest policy exclusion", event("excluded_policy", { safeError: "excluded_policy" }), "policy_blocked"],
    ["the latest conflict", event("blocked_conflict", { safeError: "blocked_conflict" }), "conflict"],
  ] as const)("projects %s", (_description, latestEvent, expectedState) => {
    expect(projectLocalNoteSyncStatus(projectInput(latestEvent))).toMatchObject({
      normalizedPath: "notes/current.md",
      state: expectedState,
      policyRevisionNumber: 7,
      retryAtEpochMs: null,
    });
  });

  it("keeps the closed retry reason and retry deadline", () => {
    const status = projectLocalNoteSyncStatus(
      projectInput(event("waiting_retry", {
        nextEligibleRetryEpochMs: 1_784_000_000_123,
        safeError: "network_offline" satisfies JournalSafeErrorLabel,
      })),
    );

    expect(status).toMatchObject({
      state: "retrying",
      retryAtEpochMs: 1_784_000_000_123,
      reason: "network_offline",
    });
  });

  it("shows synced only after the latest committed fingerprint matches the current file", () => {
    expect(projectLocalNoteSyncStatus(projectInput(event("committed")))).toMatchObject({
      state: "synced",
      reason: null,
    });
    expect(
      projectLocalNoteSyncStatus(
        projectInput(event("committed"), {
          observedFingerprint: { ...FINGERPRINT, sha256: "b".repeat(64) },
        }),
      ),
    ).toMatchObject({ state: "reconcile_required" });
  });

  it("verifies a lifecycle-committed note against the row's last-committed fingerprint, not the event placeholder", () => {
    // A committed lifecycle event (rename / move / delete / restore) carries
    // the deterministic zeros placeholder fingerprint, so comparing it with
    // the real observed bytes always mismatched and mislabelled every
    // lifecycle-settled note as reconciliation required (observed on the
    // physical device 2026-08-25: a restored note showed the wrong label
    // while its canonical state was correct). The verdict must instead
    // compare the observed bytes with the row's last-committed fingerprint.
    const lifecycleZeros: FrozenFingerprint = {
      sha256: "0".repeat(64),
      sizeBytes: 0,
      mediaType: "application/octet-stream",
    };
    for (const operation of ["rename", "move", "delete", "restore"] as const) {
      const lifecycleEvent = event("committed", {
        operation,
        fingerprint: lifecycleZeros,
      });
      expect(
        projectLocalNoteSyncStatus(
          projectInput(lifecycleEvent, { lastCommittedFingerprint: FINGERPRINT }),
        ),
      ).toMatchObject({ state: "synced" });
      expect(
        projectLocalNoteSyncStatus(
          projectInput(lifecycleEvent, {
            lastCommittedFingerprint: { ...FINGERPRINT, sha256: "b".repeat(64) },
          }),
        ),
      ).toMatchObject({ state: "reconcile_required" });
    }
  });

  it("prioritises the durable reconciliation stop", () => {
    expect(
      projectLocalNoteSyncStatus(
        projectInput(event("queued"), { isReconcileRequired: true }),
      ),
    ).toMatchObject({ state: "reconcile_required" });
  });
});
