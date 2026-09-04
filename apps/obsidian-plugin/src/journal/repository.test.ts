import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type {
  FrozenFingerprint,
  JournalEvent,
  JournalMeta,
  JournalNonRetryEventState,
  JournalSafeErrorLabel,
  MultipartProgressRecord,
} from "./contracts";
import {
  MAX_EVENT_ATTEMPT_HISTORY,
  MAX_FILE_SIZE_BYTES,
  MAX_JOURNAL_SIZE_BYTES,
  MAX_MULTIPART_PART_COUNT,
  MAX_PENDING_EVENTS,
  MULTIPART_PART_SIZE_BYTES,
} from "./contracts";
import { JournalRepository } from "./repository";
import type { JournalRepositoryDatabase } from "./repository";
import {
  JOURNAL_SCHEMA_VERSION,
  SqliteDatabase,
  migrateConflictRepairJournalToPendingRenameIntentSchema,
  migrateDeviceSyncJournalToMultipartProgressSchema,
  migrateMultipartProgressJournalToConflictRepairSchema,
} from "./sqlite-database";
import type { SqliteEngineModule } from "./sqlite-database";

/** The real sql.js WebAssembly engine drives every repository test (spec 6.1). */
let engineModule: SqliteEngineModule;

beforeAll(async () => {
  const wasmBytes = new Uint8Array(
    readFileSync(new URL("../../node_modules/sql.js/dist/sql-wasm.wasm", import.meta.url)),
  );
  const wasmBinary = wasmBytes.buffer.slice(
    wasmBytes.byteOffset,
    wasmBytes.byteOffset + wasmBytes.byteLength,
  ) as ArrayBuffer;
  engineModule = await initSqlJs({ wasmBinary });
});

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** One valid fingerprint with a distinguishable digest prefix. */
function fingerprintOf(digestPrefix: string, sizeBytes = 32): FrozenFingerprint {
  return {
    sha256: `${digestPrefix}${"0".repeat(64 - digestPrefix.length)}`,
    sizeBytes,
    mediaType: "text/plain",
  };
}

/** One recorded allowed capture; the helper fails fast on a refused outcome. */
async function captureAllowed(
  repository: JournalRepository,
  normalizedPath: string,
  fingerprint: FrozenFingerprint,
): Promise<{ event: JournalEvent }> {
  const capture = await repository.recordCapture({
    normalizedPath,
    fingerprint,
    policyRevisionNumber: 1,
    admission: "policy_allowed",
  });
  if (capture.outcome === "capture_refused") {
    throw new Error("expected a recorded capture");
  }
  return { event: capture.event };
}

function createOpenedJournal(): { repository: JournalRepository; database: SqliteDatabase } {
  const database = SqliteDatabase.createEmpty(engineModule, {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    dirtyGeneration: 1,
    lastVerifiedGeneration: 1,
    isReconcileRequired: false,
    recoveryState: "verified_generation_loaded",
  } satisfies JournalMeta);
  let currentEpochMs = 1_784_000_000_000;
  const repository = new JournalRepository({
    database,
    nowEpochMs: () => currentEpochMs++,
  });
  return { repository, database };
}

describe("JournalRepository create capture (spec 6.3, 7.1)", () => {
  it("records a create event with no source ID for a first-sight path", async () => {
    const { repository } = createOpenedJournal();
    const capture = await repository.recordCapture({
      normalizedPath: "notes/new-note.md",
      fingerprint: fingerprintOf("11"),
      policyRevisionNumber: 7,
      admission: "policy_allowed",
    });
    expect(capture.outcome).toBe("event_recorded");
    if (capture.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    const { event, localFile } = capture;

    expect(event.operation).toBe("create");
    expect(event.state).toBe("queued");
    expect(event.attemptCount).toBe(0);
    expect(event.nextEligibleRetryEpochMs).toBeNull();
    expect(event.safeError).toBeNull();
    expect(event.operationId).toBeNull();
    expect(event.eventId).toMatch(UUID_PATTERN);
    expect(event.idempotencyKey).toMatch(UUID_PATTERN);
    expect(event.idempotencyKey).not.toBe(event.eventId);

    expect(localFile.localFileId).toBe(event.localFileId);
    expect(localFile.normalizedPath).toBe("notes/new-note.md");
    expect(localFile.sourceId).toBeNull();
    expect(localFile.baseVersionId).toBeNull();
    expect(localFile.observedFingerprint).toEqual(fingerprintOf("11"));
    expect(localFile.policyRevisionNumber).toBe(7);

    // The rows round-trip through the store with the same stable identity.
    expect(repository.readEvent(event.eventId)).toEqual(event);
    expect(repository.readLocalFileByPath("notes/new-note.md")).toEqual(localFile);
    expect(repository.readEventsByLocalFileId(localFile.localFileId)).toEqual([event]);
    expect(repository.countPendingEvents()).toBe(1);
  });

  it("stores a path containing quotes and SQL metacharacters only as literal data", async () => {
    const { repository } = createOpenedJournal();
    const hostilePath = "notes/a'; drop table journal_events;--.md";

    const { event } = await captureAllowed(repository, hostilePath, fingerprintOf("21"));

    expect(repository.readLocalFileByPath(hostilePath)?.normalizedPath).toBe(hostilePath);
    expect(repository.readEvent(event.eventId)).not.toBeNull();
    expect(repository.countPendingEvents()).toBe(1);
  });

  it("rejects paths that are not normalized vault locators without persisting anything", async () => {
    const { repository } = createOpenedJournal();
    const invalidPaths = [
      "/absolute/note.md",
      "notes/trailing/",
      "notes\\backslash.md",
      "notes:drive/note.md",
      "notes/co\u0301mbining.md",
      "notes/co\u0000ntrol.md",
      "",
    ];
    for (const normalizedPath of invalidPaths) {
      await expect(
        repository.recordCapture({
          normalizedPath,
          fingerprint: fingerprintOf("31"),
          policyRevisionNumber: 1,
          admission: "policy_allowed",
        }),
      ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    }
    expect(repository.countPendingEvents()).toBe(0);
    // Querying with a malformed locator fails closed too, never running SQL.
    expect(() => repository.readLocalFileByPath("/absolute/note.md")).toThrowError(
      expect.objectContaining({ reason: "journal_mutation_failed" }),
    );
  });

  it("rejects malformed fingerprints, revisions, and unknown admission tokens", async () => {
    const { repository } = createOpenedJournal();

    await expect(
      repository.recordCapture({
        normalizedPath: "notes/a.md",
        fingerprint: { sha256: "NOT-A-DIGEST", sizeBytes: 4, mediaType: "text/plain" },
        policyRevisionNumber: 1,
        admission: "policy_allowed",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await expect(
      repository.recordCapture({
        normalizedPath: "notes/a.md",
        fingerprint: fingerprintOf("41"),
        policyRevisionNumber: -1,
        admission: "policy_allowed",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await expect(
      repository.recordCapture({
        normalizedPath: "notes/a.md",
        fingerprint: fingerprintOf("41"),
        policyRevisionNumber: 1,
        admission: "maybe" as never,
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    expect(repository.countPendingEvents()).toBe(0);
  });
});

describe("JournalRepository update capture after a committed receipt (spec 6.3, 7.2)", () => {
  it("records an update event carrying the file's source and base version IDs", async () => {
    const { repository } = createOpenedJournal();
    const { event: createdEvent } = await captureAllowed(
      repository,
      "notes/known-note.md",
      fingerprintOf("51"),
    );

    await repository.recordCommittedReceipt({
      eventId: createdEvent.eventId,
      sourceId: "1fbd21b0-0000-4000-8000-0000000000aa",
      baseVersionId: "2fce31c1-0000-4000-8000-0000000000bb",
    });

    expect(repository.readEvent(createdEvent.eventId)?.state).toBe("committed");
    const committedFile = repository.readLocalFileByPath("notes/known-note.md");
    expect(committedFile?.sourceId).toBe("1fbd21b0-0000-4000-8000-0000000000aa");
    expect(committedFile?.baseVersionId).toBe("2fce31c1-0000-4000-8000-0000000000bb");
    expect(committedFile?.observedFingerprint).toEqual(fingerprintOf("51"));

    const update = await repository.recordCapture({
      normalizedPath: "notes/known-note.md",
      fingerprint: fingerprintOf("52"),
      policyRevisionNumber: 3,
      admission: "policy_allowed",
    });
    expect(update.outcome).toBe("event_recorded");
    if (update.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    expect(update.event.operation).toBe("update");
    expect(update.event.state).toBe("queued");
    expect(update.event.fingerprint).toEqual(fingerprintOf("52"));
    expect(repository.readEventsByLocalFileId(update.event.localFileId)).toHaveLength(2);
  });

  it("rejects receipts with malformed IDs or unknown events", async () => {
    const { repository } = createOpenedJournal();
    const { event } = await captureAllowed(repository, "notes/r.md", fingerprintOf("61"));

    await expect(
      repository.recordCommittedReceipt({
        eventId: event.eventId,
        sourceId: "not-a-uuid",
        baseVersionId: "2fce31c1-0000-4000-8000-0000000000bb",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await expect(
      repository.recordCommittedReceipt({
        eventId: "00000000-0000-4000-8000-0000000000ff",
        sourceId: "1fbd21b0-0000-4000-8000-0000000000aa",
        baseVersionId: "2fce31c1-0000-4000-8000-0000000000bb",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    expect(repository.readEvent(event.eventId)?.state).toBe("queued");
  });
});

describe("JournalRepository coalescing before preflight (spec 7.2)", () => {
  it("replaces the fingerprint of the same unsent queued event", async () => {
    const { repository } = createOpenedJournal();
    const first = await repository.recordCapture({
      normalizedPath: "notes/burst.md",
      fingerprint: fingerprintOf("71"),
      policyRevisionNumber: 2,
      admission: "policy_allowed",
    });
    const second = await repository.recordCapture({
      normalizedPath: "notes/burst.md",
      fingerprint: fingerprintOf("72"),
      policyRevisionNumber: 2,
      admission: "policy_allowed",
    });

    expect(first.outcome).toBe("event_recorded");
    expect(second.outcome).toBe("event_coalesced");
    if (second.outcome === "capture_refused") {
      throw new Error("expected a coalesced capture");
    }
    expect(second.event.fingerprint).toEqual(fingerprintOf("72"));
    expect(second.localFile.observedFingerprint).toEqual(fingerprintOf("72"));

    // Exactly one event survives the burst, carrying the newer bytes.
    expect(repository.readEventsByLocalFileId(second.event.localFileId)).toHaveLength(1);
    expect(repository.readEvent(second.event.eventId)?.fingerprint).toEqual(fingerprintOf("72"));
    expect(repository.countPendingEvents()).toBe(1);
  });

  it("replaces a waiting_retry event whose preflight never started, keeping the retry schedule", async () => {
    const { repository } = createOpenedJournal();
    const { event: firstEvent } = await captureAllowed(
      repository,
      "notes/retry.md",
      fingerprintOf("81"),
    );

    await repository.markEventWaitingRetry(
      firstEvent.eventId,
      "network_offline",
      1_784_000_060_000,
    );
    const second = await repository.recordCapture({
      normalizedPath: "notes/retry.md",
      fingerprint: fingerprintOf("82"),
      policyRevisionNumber: 2,
      admission: "policy_allowed",
    });

    expect(second.outcome).toBe("event_coalesced");
    if (second.outcome === "capture_refused") {
      throw new Error("expected a coalesced capture");
    }
    expect(second.event.eventId).toBe(firstEvent.eventId);
    expect(second.event.idempotencyKey).toBe(firstEvent.idempotencyKey);
    expect(second.event.fingerprint).toEqual(fingerprintOf("82"));
    expect(second.event.state).toBe("waiting_retry");
    expect(second.event.attemptCount).toBe(1);
    expect(second.event.safeError).toBe("network_offline");
    expect(second.event.nextEligibleRetryEpochMs).toBe(1_784_000_060_000);
    expect(repository.countPendingEvents()).toBe(1);
  });

  it("creates a successor event once preflight has frozen the fingerprint", async () => {
    const { repository } = createOpenedJournal();
    const { event: firstEvent } = await captureAllowed(
      repository,
      "notes/frozen.md",
      fingerprintOf("91"),
    );

    await repository.markEventPreflightStarted(firstEvent.eventId);
    const second = await repository.recordCapture({
      normalizedPath: "notes/frozen.md",
      fingerprint: fingerprintOf("92"),
      policyRevisionNumber: 2,
      admission: "policy_allowed",
    });

    expect(second.outcome).toBe("event_recorded");
    if (second.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    expect(second.event.eventId).not.toBe(firstEvent.eventId);
    expect(second.event.idempotencyKey).not.toBe(firstEvent.idempotencyKey);
    expect(second.event.state).toBe("queued");

    const frozenEvent = repository.readEvent(firstEvent.eventId);
    expect(frozenEvent?.state).toBe("preflight");
    expect(frozenEvent?.fingerprint).toEqual(fingerprintOf("91"));
    expect(frozenEvent?.idempotencyKey).toBe(firstEvent.idempotencyKey);

    // A frozen event that returns to waiting_retry still never changes; the
    // next save after the freeze is another successor, and freezing that
    // successor makes a third save yet another one (spec 7.2).
    await repository.markEventWaitingRetry(
      firstEvent.eventId,
      "network_timeout",
      1_784_000_030_000,
    );
    await repository.markEventPreflightStarted(second.event.eventId);
    const third = await repository.recordCapture({
      normalizedPath: "notes/frozen.md",
      fingerprint: fingerprintOf("93"),
      policyRevisionNumber: 2,
      admission: "policy_allowed",
    });
    expect(third.outcome).toBe("event_recorded");
    if (third.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    expect(third.event.eventId).not.toBe(second.event.eventId);

    const events = repository.readEventsByLocalFileId(firstEvent.localFileId);
    expect(events).toHaveLength(3);
    expect(events[0]?.fingerprint).toEqual(fingerprintOf("91"));
    expect(events[0]?.state).toBe("waiting_retry");
    expect(events[1]?.fingerprint).toEqual(fingerprintOf("92"));
    expect(events[1]?.state).toBe("preflight");
    expect(events[2]?.fingerprint).toEqual(fingerprintOf("93"));
    expect(events[2]?.state).toBe("queued");
  });

  it("records blocked captures as born-terminal events that never coalesce", async () => {
    const { repository } = createOpenedJournal();
    const { event: firstEvent } = await captureAllowed(
      repository,
      "notes/big-asset.bin",
      fingerprintOf("a1"),
    );

    const blocked = await repository.recordCapture({
      normalizedPath: "notes/big-asset.bin",
      fingerprint: {
        sha256: "a2".repeat(32),
        sizeBytes: MAX_FILE_SIZE_BYTES + 1,
        mediaType: "application/octet-stream",
      },
      policyRevisionNumber: 2,
      admission: "blocked_size",
    });
    expect(blocked.outcome).toBe("event_recorded");
    if (blocked.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    expect(blocked.event.state).toBe("blocked_size");
    expect(blocked.event.safeError).toBe("blocked_size");
    expect(blocked.event.operation).toBe("create");
    // The born-terminal safe error label survives the round-trip through the
    // store, not just the in-memory insert result.
    expect(repository.readEvent(blocked.event.eventId)).toEqual(blocked.event);

    // The queued unsent event keeps its own fingerprint; terminal rows are
    // not pending and never coalesce.
    expect(repository.readEvent(firstEvent.eventId)?.fingerprint).toEqual(fingerprintOf("a1"));
    expect(repository.countPendingEvents()).toBe(1);
    expect(repository.readEventsByLocalFileId(firstEvent.localFileId)).toHaveLength(2);

    const excluded = await repository.recordCapture({
      normalizedPath: "notes/denied.md",
      fingerprint: fingerprintOf("a3"),
      policyRevisionNumber: 2,
      admission: "excluded_policy",
    });
    if (excluded.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    expect(excluded.event.state).toBe("excluded_policy");
    expect(excluded.event.safeError).toBe("excluded_policy");
    expect(repository.countPendingEvents()).toBe(1);
  });
});

describe("JournalRepository terminal-state retention (spec 6.4, 7.2)", () => {
  it("keeps terminal events queryable and closed against further transitions", async () => {
    const { repository } = createOpenedJournal();
    const { event: firstEvent } = await captureAllowed(
      repository,
      "notes/retention.md",
      fingerprintOf("b1"),
    );
    await repository.recordCommittedReceipt({
      eventId: firstEvent.eventId,
      sourceId: "1fbd21b0-0000-4000-8000-0000000000aa",
      baseVersionId: "2fce31c1-0000-4000-8000-0000000000bb",
    });

    const secondEvent = await captureAllowed(repository, "notes/retention.md", fingerprintOf("b2"));
    await repository.markEventPreflightStarted(secondEvent.event.eventId);
    await repository.markEventTerminal(
      secondEvent.event.eventId,
      "blocked_conflict",
      "blocked_conflict",
    );

    const events = repository.readEventsByLocalFileId(firstEvent.localFileId);
    expect(events).toHaveLength(2);
    expect(events[0]?.state).toBe("committed");
    expect(events[0]?.fingerprint).toEqual(fingerprintOf("b1"));
    expect(events[1]?.state).toBe("blocked_conflict");
    expect(events[1]?.safeError).toBe("blocked_conflict");
    expect(repository.readEvent(secondEvent.event.eventId)).toEqual(events[1]);
    expect(repository.countPendingEvents()).toBe(0);

    // Terminal states never receive automatic retry or further transitions.
    for (const terminalEvent of events) {
      await expect(
        repository.markEventWaitingRetry(terminalEvent.eventId, "network_offline", 1),
      ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
      await expect(
        repository.markEventPreflightStarted(terminalEvent.eventId),
      ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    }

    // Terminal targets outside the closed non-retry set are rejected.
    await expect(
      repository.markEventTerminal(
        secondEvent.event.eventId,
        "committed" as JournalNonRetryEventState,
        "server_error",
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });
});

describe("JournalRepository intent-aware local-file-missing resolution", () => {
  it("parks an intent-owned content event without consuming its source identity or rename reservation", async () => {
    const { repository, database } = createOpenedJournal();
    const { event } = await captureAllowed(
      repository,
      "notes/untitled.md",
      fingerprintOf("de"),
    );
    await repository.lifecycle.recordOrComposePendingRenameIntent({
      localFileId: event.localFileId,
      observedPriorPath: "notes/untitled.md",
      observedCurrentPath: "archive/origin.md",
    });

    await expect(
      repository.resolveIntentAwareLocalFileMissing({
        eventId: event.eventId,
        attemptedAtEpochMs: 1_784_000_001_000,
        requestCorrelationId: "intent-missing-1",
        nextEligibleRetryEpochMs: 1_784_000_001_250,
      }),
    ).resolves.toEqual({ outcome: "waiting_for_rename" });

    expect(repository.readEvent(event.eventId)).toMatchObject({
      state: "waiting_retry",
      safeError: "deferred_lifecycle",
    });
    expect(
      repository.lifecycle.readPendingRenameIntentForLocalFile(event.localFileId),
    ).toMatchObject({ currentPath: "archive/origin.md" });
    expect(
      database.readAll(
        "select deferred_attempt_count from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(1);
  });

  it("takes reconciliation ownership only on the 41st matching missing-file resolution", async () => {
    const { repository, database } = createOpenedJournal();
    const { event } = await captureAllowed(repository, "notes/a.md", fingerprintOf("df"));
    await repository.lifecycle.recordOrComposePendingRenameIntent({
      localFileId: event.localFileId,
      observedPriorPath: "notes/a.md",
      observedCurrentPath: "archive/c.md",
    });

    for (let attempt = 1; attempt <= 40; attempt += 1) {
      await expect(
        repository.resolveIntentAwareLocalFileMissing({
          eventId: event.eventId,
          attemptedAtEpochMs: 1_784_000_010_000 + attempt,
          requestCorrelationId: `intent-attempt-${attempt}`,
          nextEligibleRetryEpochMs: 1_784_000_020_000 + attempt,
        }),
      ).resolves.toEqual({ outcome: "waiting_for_rename" });
    }
    expect(
      database.readAll(
        "select deferred_attempt_count from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(40);

    await expect(
      repository.resolveIntentAwareLocalFileMissing({
        eventId: event.eventId,
        attemptedAtEpochMs: 1_784_000_010_041,
        requestCorrelationId: "intent-attempt-41",
        nextEligibleRetryEpochMs: 1_784_000_020_041,
      }),
    ).resolves.toEqual({
      outcome: "reconcile_takeover",
      diagnosticReason: "pending_rename_intent_exhausted",
    });
    expect(repository.readEvent(event.eventId)).toMatchObject({
      state: "deferred_lifecycle",
      safeError: "deferred_lifecycle",
    });
    expect(repository.readLocalFileByLocalFileId(event.localFileId)).toMatchObject({
      normalizedPath: "archive/c.md",
    });
    expect(repository.lifecycle.readPendingRenameIntentForLocalFile(event.localFileId)).toBeNull();
    expect(
      database.readAll(
        "select count(*) from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(0);
    expect(database.readJournalMeta().isReconcileRequired).toBe(true);
  });

  it("takes the conflict exit before counter increment when another content event claims its retry budget", async () => {
    const { repository, database } = createOpenedJournal();
    const { event: first } = await captureAllowed(repository, "notes/a.md", fingerprintOf("e1"));
    await repository.markEventPreflightStarted(first.eventId);
    const { event: second } = await captureAllowed(repository, "notes/a.md", fingerprintOf("e2"));
    await repository.lifecycle.recordOrComposePendingRenameIntent({
      localFileId: first.localFileId,
      observedPriorPath: "notes/a.md",
      observedCurrentPath: "archive/c.md",
    });
    await repository.resolveIntentAwareLocalFileMissing({
      eventId: first.eventId,
      attemptedAtEpochMs: 1_784_000_030_000,
      requestCorrelationId: "intent-first",
      nextEligibleRetryEpochMs: 1_784_000_030_250,
    });

    await expect(
      repository.resolveIntentAwareLocalFileMissing({
        eventId: second.eventId,
        attemptedAtEpochMs: 1_784_000_030_001,
        requestCorrelationId: "intent-second",
        nextEligibleRetryEpochMs: 1_784_000_030_251,
      }),
    ).resolves.toEqual({
      outcome: "reconcile_takeover",
      diagnosticReason: "pending_rename_intent_conflict",
    });
    expect(repository.lifecycle.readPendingRenameIntentForLocalFile(first.localFileId)).toBeNull();
    expect(repository.readLocalFileByLocalFileId(first.localFileId)).toMatchObject({
      normalizedPath: "archive/c.md",
    });
    expect(
      database.readAll(
        "select count(*) from pending_rename_intent_missing_file_deferrals;",
      )[0]?.values[0]?.[0],
    ).toBe(0);
  });
});

describe("JournalRepository queue soft limits (spec 6.4)", () => {
  /** Seed `count` queued events directly inside one serialized transaction. */
  async function seedPendingEvents(database: SqliteDatabase, count: number): Promise<void> {
    await database.runSerializedMutation((session) => {
      session.exec(
        Array.from(
          { length: count },
          (_, index) =>
            `insert into local_files (local_file_id, normalized_path, source_id, observed_sha256, observed_size_bytes, observed_media_type, base_version_id, policy_revision) values ('seed-file-${index}', 'seed/file-${index}.md', null, '${String(index).padStart(64, "0")}', 1, 'text/plain', null, 1);`,
        ).join(""),
      );
      session.exec(
        Array.from(
          { length: count },
          (_, index) =>
            `insert into journal_events (event_id, local_file_id, idempotency_key, operation, sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count, next_eligible_retry_epoch_ms, safe_error, operation_id, created_at_epoch_ms) values ('seed-event-${index}', 'seed-file-${index}', 'seed-key-${index}', 'create', '${String(index).padStart(64, "0")}', 1, 'text/plain', 'queued', 0, 0, null, null, null, ${index});`,
        ).join(""),
      );
    });
  }

  it("allows exactly the pending-event cap, then refuses new rows durably", async () => {
    const { repository, database } = createOpenedJournal();
    await seedPendingEvents(database, MAX_PENDING_EVENTS - 1);
    expect(repository.countPendingEvents()).toBe(MAX_PENDING_EVENTS - 1);

    // The cap-reaching row still lands: the user's edit is never the one refused.
    const atLimit = await repository.recordCapture({
      normalizedPath: "notes/at-limit.md",
      fingerprint: fingerprintOf("c1"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(atLimit.outcome).toBe("event_recorded");
    expect(repository.countPendingEvents()).toBe(MAX_PENDING_EVENTS);
    expect(database.readJournalMeta().isReconcileRequired).toBe(true);

    // One row beyond the cap: refused, flagged, and nothing persisted for it.
    const refused = await repository.recordCapture({
      normalizedPath: "notes/beyond-limit.md",
      fingerprint: fingerprintOf("c2"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(refused).toEqual({ outcome: "capture_refused", reason: "reconcile_required" });
    expect(repository.countPendingEvents()).toBe(MAX_PENDING_EVENTS);
    expect(repository.readLocalFileByPath("notes/beyond-limit.md")).toBeNull();
    expect(database.readJournalMeta().isReconcileRequired).toBe(true);

    // Terminal blocked captures are refused too; coalescing still lands.
    const refusedBlocked = await repository.recordCapture({
      normalizedPath: "notes/beyond-limit.bin",
      fingerprint: fingerprintOf("c3"),
      policyRevisionNumber: 1,
      admission: "blocked_size",
    });
    expect(refusedBlocked.outcome).toBe("capture_refused");

    const coalesced = await repository.recordCapture({
      normalizedPath: "notes/at-limit.md",
      fingerprint: fingerprintOf("c4"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(coalesced.outcome).toBe("event_coalesced");
    expect(repository.countPendingEvents()).toBe(MAX_PENDING_EVENTS);

    // In-flight evidence stays fully intact.
    if (atLimit.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    expect(repository.readEvent(atLimit.event.eventId)?.fingerprint).toEqual(fingerprintOf("c4"));
    // Seeds ~2x MAX_PENDING_EVENTS rows through multi-megabyte SQL; under the
    // concurrent workspace test run plus coverage it exceeds the 5s default.
  }, 20_000);

  it("refuses new rows once the journal image reaches the size ceiling", async () => {
    const { repository, database } = createOpenedJournal();
    const inFlight = await repository.recordCapture({
      normalizedPath: "notes/in-flight.md",
      fingerprint: fingerprintOf("d1"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(inFlight.outcome).toBe("event_recorded");

    await database.runSerializedMutation((session) => {
      session.exec("create table journal_size_inflation (payload blob);");
      session.exec(
        `insert into journal_size_inflation (payload) values (zeroblob(${MAX_JOURNAL_SIZE_BYTES}));`,
      );
    });

    const refused = await repository.recordCapture({
      normalizedPath: "notes/after-inflation.md",
      fingerprint: fingerprintOf("d2"),
      policyRevisionNumber: 1,
      admission: "policy_allowed",
    });
    expect(refused).toEqual({ outcome: "capture_refused", reason: "reconcile_required" });
    expect(database.readJournalMeta().isReconcileRequired).toBe(true);
    expect(repository.readLocalFileByPath("notes/after-inflation.md")).toBeNull();

    // In-flight evidence survives the refusal untouched.
    if (inFlight.outcome === "capture_refused") {
      throw new Error("expected a recorded capture");
    }
    expect(repository.readEvent(inFlight.event.eventId)?.state).toBe("queued");
    expect(repository.countPendingEvents()).toBe(1);
  });
});

describe("JournalRepository bounded attempted-event history (spec 6.3, 9)", () => {
  it("keeps only the most recent attempts per event in a closed, redacted shape", async () => {
    const { repository } = createOpenedJournal();
    const sensitivePath = "notes/private 'quoted' note.md";
    const { event } = await captureAllowed(repository, sensitivePath, fingerprintOf("e1"));

    const attemptCount = MAX_EVENT_ATTEMPT_HISTORY + 3;
    for (let index = 0; index < attemptCount; index += 1) {
      await repository.recordEventAttempt({
        eventId: event.eventId,
        attemptedAtEpochMs: 1_784_000_000_000 + index,
        outcomeLabel: index % 2 === 0 ? "network_offline" : "server_error",
        requestCorrelationId: `request-${index}`,
      });
    }

    const history = repository.readEventAttemptHistory(event.eventId);
    expect(history).toHaveLength(MAX_EVENT_ATTEMPT_HISTORY);
    expect(history[0]?.requestCorrelationId).toBe("request-3");
    expect(history.at(-1)?.requestCorrelationId).toBe(`request-${attemptCount - 1}`);
    for (const attempt of history) {
      expect(["network_offline", "server_error"]).toContain(attempt.outcomeLabel);
      expect(attempt.eventId).toBe(event.eventId);
      expect(attempt.attemptedAtEpochMs).toBeGreaterThan(0);
    }

    // The redacted surface carries closed labels and opaque IDs only.
    const serializedHistory = JSON.stringify(history);
    expect(serializedHistory).not.toContain("notes/");
    expect(serializedHistory).not.toContain("private");
    expect(serializedHistory).not.toContain(fingerprintOf("e1").sha256);
    expect(serializedHistory).not.toContain(sensitivePath);
  });

  it("rejects non-closed outcome labels, malformed correlation IDs, and unknown events", async () => {
    const { repository } = createOpenedJournal();
    const { event } = await captureAllowed(repository, "notes/attempts.md", fingerprintOf("f1"));

    await repository.recordEventAttempt({
      eventId: event.eventId,
      attemptedAtEpochMs: 10,
      outcomeLabel: "network_timeout",
      requestCorrelationId: "request-kept",
    });

    await expect(
      repository.recordEventAttempt({
        eventId: event.eventId,
        attemptedAtEpochMs: 11,
        outcomeLabel: "sqlite disk I/O error at notes/attempts.md" as JournalSafeErrorLabel,
        requestCorrelationId: "request-1",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await expect(
      repository.recordEventAttempt({
        eventId: event.eventId,
        attemptedAtEpochMs: 12,
        outcomeLabel: "server_error",
        requestCorrelationId: "",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await expect(
      repository.recordEventAttempt({
        eventId: event.eventId,
        attemptedAtEpochMs: 13,
        outcomeLabel: "server_error",
        requestCorrelationId: "request-with-\u0000-control",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await expect(
      repository.recordEventAttempt({
        eventId: event.eventId,
        attemptedAtEpochMs: -1,
        outcomeLabel: "server_error",
        requestCorrelationId: "request-2",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await expect(
      repository.recordEventAttempt({
        eventId: "00000000-0000-4000-8000-0000000000ff",
        attemptedAtEpochMs: 14,
        outcomeLabel: "server_error",
        requestCorrelationId: "request-3",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    expect(repository.readEventAttemptHistory(event.eventId)).toEqual([
      {
        eventId: event.eventId,
        attemptedAtEpochMs: 10,
        outcomeLabel: "network_timeout",
        requestCorrelationId: "request-kept",
      },
    ]);
  });
});

describe("JournalRepository transition validation (spec 7.2)", () => {
  it("moves a queued event into preflight exactly once", async () => {
    const { repository } = createOpenedJournal();
    const { event } = await captureAllowed(repository, "notes/preflight.md", fingerprintOf("99"));

    await repository.markEventPreflightStarted(event.eventId);
    expect(repository.readEvent(event.eventId)?.state).toBe("preflight");

    await expect(repository.markEventPreflightStarted(event.eventId)).rejects.toMatchObject({
      reason: "journal_mutation_failed",
    });
    await expect(
      repository.markEventPreflightStarted("00000000-0000-4000-8000-0000000000ff"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("schedules waiting_retry with closed labels only", async () => {
    const { repository } = createOpenedJournal();
    const { event } = await captureAllowed(repository, "notes/retry-labels.md", fingerprintOf("98"));

    await repository.markEventWaitingRetry(event.eventId, "network_rate_limited", 1_784_000_030_000);
    const waiting = repository.readEvent(event.eventId);
    expect(waiting?.state).toBe("waiting_retry");
    expect(waiting?.attemptCount).toBe(1);
    expect(waiting?.safeError).toBe("network_rate_limited");
    expect(waiting?.nextEligibleRetryEpochMs).toBe(1_784_000_030_000);

    await expect(
      repository.markEventWaitingRetry(event.eventId, "connection refused by host" as JournalSafeErrorLabel, 1),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.markEventWaitingRetry(event.eventId, "server_error", Number.NaN),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(repository.readEvent(event.eventId)?.attemptCount).toBe(1);
  });
});

describe("JournalRepository queue selection and upload transitions (spec 8)", () => {
  it("selects the oldest eligible event: retry time gating and interrupted states", async () => {
    const { repository } = createOpenedJournal();
    const first = await captureAllowed(repository, "notes/select-first.md", fingerprintOf("71"));
    const second = await captureAllowed(repository, "notes/select-second.md", fingerprintOf("72"));
    const third = await captureAllowed(repository, "notes/select-third.md", fingerprintOf("73"));

    const nowEpochMs = 2_000_000_000_000;
    // Nothing is waiting: the oldest queued event wins.
    expect(repository.readOldestEligibleEvent(nowEpochMs)?.eventId).toBe(first.event.eventId);

    // The first event fails once and becomes ineligible until its retry time.
    await repository.markEventPreflightStarted(first.event.eventId);
    await repository.markEventWaitingRetry(first.event.eventId, "network_offline", nowEpochMs + 5_000);
    expect(repository.readOldestEligibleEvent(nowEpochMs)?.eventId).toBe(second.event.eventId);
    expect(repository.readOldestEligibleEvent(nowEpochMs + 5_000)?.eventId).toBe(first.event.eventId);

    // An interrupted pass leaves preflight/uploading rows: both stay eligible
    // for an exact same-identity replay on the next pass (spec 7.2, 10.3).
    await repository.markEventPreflightStarted(second.event.eventId);
    await repository.markEventUploading(second.event.eventId, "op-token-0123456789abcdef0123456789");
    expect(repository.readOldestEligibleEvent(nowEpochMs)?.eventId).toBe(second.event.eventId);
    const resumed = repository.readEvent(second.event.eventId);
    expect(resumed?.state).toBe("uploading");
    expect(resumed?.operationId).toBe("op-token-0123456789abcdef0123456789");

    // Terminal and future-retry rows are never selected.
    await repository.markEventTerminal(third.event.eventId, "excluded_policy", "excluded_policy");
    expect(repository.readOldestEligibleEvent(nowEpochMs + 5_000)?.eventId).toBe(first.event.eventId);
    expect(repository.readOldestEligibleEvent(Number.MAX_SAFE_INTEGER)?.eventId).toBe(first.event.eventId);
  });

  it("moves an event into uploading with its operation ID, closed against other states", async () => {
    const { repository } = createOpenedJournal();
    const { event } = await captureAllowed(repository, "notes/uploading.md", fingerprintOf("70"));

    // Only a prefrozen event may enter uploading.
    await expect(
      repository.markEventUploading(event.eventId, "op-token-0123456789abcdef0123456789"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    await repository.markEventPreflightStarted(event.eventId);
    await repository.markEventUploading(event.eventId, "op-token-0123456789abcdef0123456789");
    const uploading = repository.readEvent(event.eventId);
    expect(uploading?.state).toBe("uploading");
    expect(uploading?.operationId).toBe("op-token-0123456789abcdef0123456789");

    // A malformed token never lands.
    await expect(
      repository.markEventUploading(event.eventId, "short"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.markEventUploading(event.eventId, `bad chars ${"x".repeat(40)}`),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.markEventUploading("00000000-0000-4000-8000-0000000000ff", "op-token-0123456789abcdef0123456789"),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });

  it("reads one tracked file by its plugin-local identity", async () => {
    const { repository } = createOpenedJournal();
    const { event } = await captureAllowed(repository, "notes/by-id.md", fingerprintOf("69"));
    const localFile = repository.readLocalFileByLocalFileId(event.localFileId);
    expect(localFile?.normalizedPath).toBe("notes/by-id.md");
    expect(repository.readLocalFileByLocalFileId("00000000-0000-4000-8000-0000000000ff")).toBeNull();
  });

  it("persists the no-op receipt of a no_change close", async () => {
    const { repository } = createOpenedJournal();
    const { event } = await captureAllowed(repository, "notes/no-change.md", fingerprintOf("68"));

    await repository.markEventPreflightStarted(event.eventId);
    await repository.recordNoChangeReceipt({
      eventId: event.eventId,
      sourceId: "44444444-4444-4444-8444-444444444444",
      baseVersionId: "55555555-5555-4555-8555-555555555555",
    });
    const stored = repository.readEvent(event.eventId);
    expect(stored?.state).toBe("no_change");
    expect(stored?.safeError).toBeNull();
    const localFile = repository.readLocalFileByLocalFileId(event.localFileId);
    expect(localFile?.sourceId).toBe("44444444-4444-4444-8444-444444444444");
    expect(localFile?.baseVersionId).toBe("55555555-5555-4555-8555-555555555555");

    // The closed receipt shape stays validated.
    await expect(
      repository.recordNoChangeReceipt({
        eventId: "00000000-0000-4000-8000-0000000000ff",
        sourceId: "44444444-4444-4444-8444-444444444444",
        baseVersionId: "55555555-5555-4555-8555-555555555555",
      }),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
  });
});

describe("JournalRepository content-lane selection excludes lifecycle rows (lane discipline)", () => {
  it("never selects a queued or retry-waiting lifecycle event even when it is the oldest pending row", async () => {
    const { repository } = createOpenedJournal();
    // One tracked file whose committed create gives the rename operands a
    // real source identity to carry.
    const committed = await captureAllowed(repository, "notes/lane-note.md", fingerprintOf("85"));
    await repository.recordCommittedReceipt({
      eventId: committed.event.eventId,
      sourceId: "11111111-1111-4111-8111-111111111111",
      baseVersionId: "22222222-2222-4222-8222-222222222222",
    });
    const trackedFile = repository.readLocalFileByPath("notes/lane-note.md");
    expect(trackedFile).not.toBeNull();
    if (trackedFile === null) {
      throw new Error("expected a tracked file row");
    }

    // The queued rename is the OLDEST pending journal row …
    const rename = await repository.lifecycle.recordLifecycleEvent(
      {
        operation: "rename",
        sourceId: "11111111-1111-4111-8111-111111111111",
        expectedVersionId: "22222222-2222-4222-8222-222222222222",
        expectedLocator: "notes/lane-note.md",
        targetLocator: "notes/lane-note-renamed.md",
        tombstoneId: null,
        policyRevision: 1,
        predecessorEventId: null,
        capturedFingerprintSha256: null,
        capturedFingerprintSizeBytes: null,
        capturedFingerprintMediaType: null,
      },
      { localFile: trackedFile, newPath: "notes/lane-note-renamed.md" },
    );
    expect(rename.event.state).toBe("queued");
    // … and one LATER queued content event of another file exists.
    const content = await captureAllowed(repository, "notes/lane-content.md", fingerprintOf("86"));

    // The content lane must select the CONTENT row, never the lifecycle
    // row with its placeholder zeros fingerprint.
    const nowEpochMs = 2_000_000_000_000;
    expect(repository.readOldestEligibleEvent(nowEpochMs)?.eventId).toBe(content.event.eventId);

    // The exclusion is state-uniform: a retry-waiting lifecycle row stays
    // out of the content lane once its retry time has passed.
    await repository.markEventPreflightStarted(rename.event.eventId);
    await repository.markEventWaitingRetry(rename.event.eventId, "server_error", 1);
    expect(repository.readOldestEligibleEvent(nowEpochMs)?.eventId).toBe(content.event.eventId);

    // The lifecycle lane still owns the lifecycle row through its own
    // selector (the exclusion never starves the lifecycle lane).
    const frozen = repository.lifecycle.readOldestEligibleLifecycleEvent(nowEpochMs);
    expect(frozen?.event.eventId).toBe(rename.event.eventId);
  });

  it("selects the oldest content event when only content rows are pending", async () => {
    const { repository } = createOpenedJournal();
    const first = await captureAllowed(repository, "notes/lane-first.md", fingerprintOf("87"));
    const second = await captureAllowed(repository, "notes/lane-second.md", fingerprintOf("88"));
    expect(repository.readOldestEligibleEvent(2_000_000_000_000)?.eventId).toBe(
      first.event.eventId,
    );
    await repository.markEventTerminal(first.event.eventId, "excluded_policy", "excluded_policy");
    expect(repository.readOldestEligibleEvent(2_000_000_000_000)?.eventId).toBe(
      second.event.eventId,
    );
  });
});

describe("JournalRepository pending retry deadline reads (scheduled retry trigger)", () => {
  it("reads the earliest scheduled retry deadline among pending events only", async () => {
    const { repository } = createOpenedJournal();
    expect(repository.readEarliestPendingRetryEpochMs()).toBeNull();
    const first = await captureAllowed(repository, "notes/retry-deadline-first.md", fingerprintOf("91"));
    const second = await captureAllowed(repository, "notes/retry-deadline-second.md", fingerprintOf("92"));
    // Queued rows carry no retry deadline yet: nothing to schedule from.
    expect(repository.readEarliestPendingRetryEpochMs()).toBeNull();

    await repository.markEventPreflightStarted(first.event.eventId);
    await repository.markEventWaitingRetry(first.event.eventId, "network_offline", 5_000);
    await repository.markEventPreflightStarted(second.event.eventId);
    await repository.markEventWaitingRetry(second.event.eventId, "login_required", 3_000);
    expect(repository.readEarliestPendingRetryEpochMs()).toBe(3_000);

    // Terminal rows leave the pending set entirely; the earliest pending
    // deadline wins.
    await repository.markEventTerminal(second.event.eventId, "excluded_policy", "excluded_policy");
    expect(repository.readEarliestPendingRetryEpochMs()).toBe(5_000);
  });
});

describe("JournalRepository status histogram (spec 11)", () => {
  it("counts events by closed state and closed safe error label only", async () => {
    const { repository } = createOpenedJournal();
    expect(repository.readEventStateErrorCounts()).toEqual([]);

    const queued = await captureAllowed(repository, "notes/histogram-queued.md", fingerprintOf("81"));
    const retrying = await captureAllowed(repository, "notes/histogram-retry.md", fingerprintOf("82"));
    const conflicted = await captureAllowed(repository, "notes/histogram-conflict.md", fingerprintOf("83"));
    await repository.recordCapture({
      normalizedPath: "notes/histogram-excluded.md",
      fingerprint: fingerprintOf("84"),
      policyRevisionNumber: 1,
      admission: "excluded_policy",
    });

    await repository.markEventPreflightStarted(retrying.event.eventId);
    await repository.markEventWaitingRetry(retrying.event.eventId, "network_offline", 1);
    await repository.markEventTerminal(conflicted.event.eventId, "blocked_conflict", "blocked_conflict");

    const histogram = repository.readEventStateErrorCounts().map((row) => ({ ...row }));
    expect(histogram).toHaveLength(4);
    expect(histogram).toContainEqual({ state: "queued", safeError: null, eventCount: 1 });
    expect(histogram).toContainEqual({ state: "waiting_retry", safeError: "network_offline", eventCount: 1 });
    expect(histogram).toContainEqual({ state: "blocked_conflict", safeError: "blocked_conflict", eventCount: 1 });
    expect(histogram).toContainEqual({ state: "excluded_policy", safeError: "excluded_policy", eventCount: 1 });
    expect(queued.event.state).toBe("queued");
  });

  it("groups coalesced history into one row per state and error label", async () => {
    const { repository } = createOpenedJournal();
    for (const digestPrefix of ["91", "92", "93"]) {
      await captureAllowed(repository, `notes/histogram-${digestPrefix}.md`, fingerprintOf(digestPrefix));
    }
    const histogram = repository.readEventStateErrorCounts();
    expect(histogram).toEqual([{ state: "queued", safeError: null, eventCount: 3 }]);
  });

  it("omits a superseded terminal policy block from the current status histogram", async () => {
    const { repository } = createOpenedJournal();
    await repository.recordCapture({
      normalizedPath: "notes/re-admitted.md",
      fingerprint: fingerprintOf("a1"),
      policyRevisionNumber: 1,
      admission: "excluded_policy",
    });

    const successor = await repository.recordCapture({
      normalizedPath: "notes/re-admitted.md",
      fingerprint: fingerprintOf("b2"),
      policyRevisionNumber: 2,
      admission: "policy_allowed",
    });
    if (successor.outcome === "capture_refused") {
      throw new Error("expected the policy-allowed successor to be recorded");
    }

    expect(repository.readEventStateErrorCounts()).toEqual([
      { state: "queued", safeError: null, eventCount: 1 },
    ]);
    expect(repository.readEventsByLocalFileId(successor.localFile.localFileId)).toHaveLength(2);
  });

  it("reads the latest local note state rather than historical policy audit evidence", async () => {
    const { repository } = createOpenedJournal();
    const excluded = await repository.recordCapture({
      normalizedPath: "notes/current-status.md",
      fingerprint: fingerprintOf("c1"),
      policyRevisionNumber: 1,
      admission: "excluded_policy",
    });
    if (excluded.outcome === "capture_refused") {
      throw new Error("expected excluded audit evidence");
    }
    const successor = await repository.recordCapture({
      normalizedPath: "notes/current-status.md",
      fingerprint: fingerprintOf("d2"),
      policyRevisionNumber: 2,
      admission: "policy_allowed",
    });
    if (successor.outcome === "capture_refused") {
      throw new Error("expected a policy-allowed successor");
    }
    await repository.recordCommittedReceipt({
      eventId: successor.event.eventId,
      sourceId: "44444444-4444-4444-8444-444444444444",
      baseVersionId: "55555555-5555-4555-8555-555555555555",
    });

    const statuses = repository.readLocalNoteSyncStatuses();
    expect(statuses).toContainEqual(
      expect.objectContaining({ normalizedPath: "notes/current-status.md", state: "synced" }),
    );
    const aggregateTelemetry = JSON.stringify(repository.readEventStateErrorCounts());
    expect(aggregateTelemetry).not.toContain("notes/current-status.md");
  });
});

describe("JournalRepository device-sync composition (task 8)", () => {
  it("exposes the device-sync repository over the same database slice", async () => {
    const { repository } = createOpenedJournal();

    expect(repository.deviceSync.readState()).toEqual({
      appliedSequence: 0,
      acknowledgedSequence: 0,
      observationGeneration: 0,
      barrierGeneration: null,
      barrierReason: null,
      activeManifestRunId: null,
      manifestCheckpointSequence: null,
      manifestFinalDigest: null,
    });
    // The device-sync repository mutates through the same serialized writer:
    // one observation generation increment lands in the shared image.
    await repository.deviceSync.nextObservationGeneration();
    expect(repository.deviceSync.readState().observationGeneration).toBe(1);
  });

  it("honors a custom device-sync repository factory over the shared writer", () => {
    const { database } = createOpenedJournal();
    const stubDeviceSync = createOpenedJournal().repository.deviceSync;
    let observedDatabase: unknown = null;
    const composed = new JournalRepository({
      database,
      createDeviceSyncRepository: (deps) => {
        observedDatabase = deps.database;
        return stubDeviceSync;
      },
    });
    expect(composed.deviceSync).toBe(stubDeviceSync);
    expect(observedDatabase).toBe(database);
  });
});

describe("JournalRepository manifest repair completion (task 11, spec 12.4)", () => {
  const MANIFEST_RUN_ID = "018f47a0-7b00-7000-8000-0000000000b1";
  const MARKER_SOURCE_ID = "99999999-9999-4999-8999-999999999999";
  const PAGE_DIGEST = "a".repeat(64);

  interface SeededRepair {
    readonly repository: JournalRepository;
    readonly database: SqliteDatabase;
  }

  async function seedActiveRepairRun(flagReconcileRequired = true): Promise<SeededRepair> {
    const { repository, database } = createOpenedJournal();
    const deviceSync = repository.deviceSync;
    await deviceSync.startRepairBarrier({ generation: 0, reason: "device_cursor_gap" });
    await deviceSync.recordManifestPage({
      manifestRunId: MANIFEST_RUN_ID,
      pageNumber: 0,
      entryCount: 2,
      pageDigest: PAGE_DIGEST,
      checkpointSequence: 7,
      finalDigest: null,
    });
    await deviceSync.recordManifestPage({
      manifestRunId: MANIFEST_RUN_ID,
      pageNumber: 1,
      entryCount: 1,
      pageDigest: "b".repeat(64),
      checkpointSequence: 7,
      finalDigest: null,
    });
    await deviceSync.recordManifestAction({
      manifestRunId: MANIFEST_RUN_ID,
      actionIndex: 0,
      actionKind: "download",
      outcome: "received",
      reason: null,
    });
    await deviceSync.recordEchoMarker({
      eventSequence: 3,
      sourceId: MARKER_SOURCE_ID,
      operation: "created",
      priorLocator: null,
      targetLocator: "notes/swept.md",
      finalFingerprint: fingerprintOf("33"),
    });
    await deviceSync.recordEchoMarker({
      eventSequence: 9,
      sourceId: MARKER_SOURCE_ID,
      operation: "updated",
      priorLocator: "notes/retained.md",
      targetLocator: null,
      finalFingerprint: fingerprintOf("34"),
    });
    if (flagReconcileRequired) {
      await database.runSerializedMutation((session) => {
        session.writeJournalMeta({ ...session.readJournalMeta(), isReconcileRequired: true });
      });
    }
    return { repository, database };
  }

  it("advances both cursors to the checkpoint, clears the flag and discards run progress", async () => {
    const { repository, database } = await seedActiveRepairRun();

    await repository.completeDeviceSyncRepair({
      manifestRunId: MANIFEST_RUN_ID,
      checkpointSequence: 7,
      barrierGeneration: 0,
    });

    expect(repository.deviceSync.readState()).toEqual({
      appliedSequence: 7,
      acknowledgedSequence: 7,
      observationGeneration: 0,
      barrierGeneration: null,
      barrierReason: null,
      activeManifestRunId: null,
      manifestCheckpointSequence: null,
      manifestFinalDigest: null,
    });
    expect(database.readJournalMeta().isReconcileRequired).toBe(false);
    expect(repository.readManifestPageProgress()).toEqual([]);
    expect(repository.readManifestActionProgress()).toEqual([]);
  });

  it("sweeps acknowledged echo markers and retains the ones still owed", async () => {
    const { repository } = await seedActiveRepairRun();

    await repository.completeDeviceSyncRepair({
      manifestRunId: MANIFEST_RUN_ID,
      checkpointSequence: 7,
      barrierGeneration: 0,
    });

    // Sequence 3 sits at/below the acknowledged cursor 7: swept.
    expect(repository.deviceSync.readEchoMarker(3)).toBeNull();
    // Sequence 9 is still owed: retained.
    expect(repository.deviceSync.readEchoMarker(9)).not.toBeNull();
  });

  it("keeps the reconcile flag clear when it was never set", async () => {
    const { repository, database } = await seedActiveRepairRun(false);

    await repository.completeDeviceSyncRepair({
      manifestRunId: MANIFEST_RUN_ID,
      checkpointSequence: 7,
      barrierGeneration: 0,
    });

    expect(database.readJournalMeta().isReconcileRequired).toBe(false);
  });

  it("notifies the composition's reconcile-complete surface before clearing", async () => {
    const { database } = createOpenedJournal();
    const notifications: number[] = [];
    const repository = new JournalRepository({
      database,
      onDeviceSyncRepairComplete: () => notifications.push(1),
    });
    await repository.deviceSync.startRepairBarrier({ generation: 0, reason: "device_cursor_gap" });
    await repository.deviceSync.recordManifestPage({
      manifestRunId: MANIFEST_RUN_ID,
      pageNumber: 0,
      entryCount: 0,
      pageDigest: PAGE_DIGEST,
      checkpointSequence: 4,
      finalDigest: null,
    });

    await repository.completeDeviceSyncRepair({
      manifestRunId: MANIFEST_RUN_ID,
      checkpointSequence: 4,
      barrierGeneration: 0,
    });

    expect(notifications).toEqual([1]);
  });

  it("discards only the temporary run progress while keeping the barrier", async () => {
    const { repository } = await seedActiveRepairRun();

    await repository.discardActiveManifestRun();

    const state = repository.deviceSync.readState();
    expect(state.barrierGeneration).toBe(0);
    expect(state.barrierReason).toBe("device_cursor_gap");
    expect(state.activeManifestRunId).toBeNull();
    expect(state.manifestCheckpointSequence).toBeNull();
    expect(state.manifestFinalDigest).toBeNull();
    expect(repository.readManifestPageProgress()).toEqual([]);
    expect(repository.readManifestActionProgress()).toEqual([]);
    // Echo markers are not temporary run progress: untouched.
    expect(repository.deviceSync.readEchoMarker(3)).not.toBeNull();
  });

  it("reads the durable page and action progress of the active run", async () => {
    const { repository } = await seedActiveRepairRun();

    expect(repository.readManifestPageProgress()).toEqual([
      { pageNumber: 0, entryCount: 2, pageDigest: PAGE_DIGEST },
      { pageNumber: 1, entryCount: 1, pageDigest: "b".repeat(64) },
    ]);
    await repository.deviceSync.recordManifestAction({
      manifestRunId: MANIFEST_RUN_ID,
      actionIndex: 0,
      actionKind: "download",
      outcome: "terminal_safe",
      reason: "device_manifest_action_stale",
    });
    expect(repository.readManifestActionProgress()).toEqual([
      {
        actionIndex: 0,
        actionKind: "download",
        outcome: "terminal_safe",
        reason: "device_manifest_action_stale",
      },
    ]);
  });
});

// --- multipart upload progress persistence (task 9, child 7 spec 4.1) ------------------------------

describe("JournalRepository multipart progress persistence (task 9, spec 4.1)", () => {
  /** Opaque public session-ID-shaped text: printable base64url, not a UUID. */
  const MULTIPART_SESSION_ID = "bXVsdGlwYXJ0LXNlc3Npb24taWRlbnRpdHktMDEyMzQ1Njc4OTA";

  const V7_LOCAL_FILE_ID = "11111111-1111-4111-8111-111111111111";
  const V7_EVENT_ID = "55555555-5555-4555-8555-555555555555";
  const V7_IDEMPOTENCY_KEY = "66666666-6666-4666-8666-666666666666";

  /** One valid safe progress record bound to `eventId`. */
  function multipartProgressRecord(
    eventId: string,
    overrides: Partial<MultipartProgressRecord> = {},
  ): MultipartProgressRecord {
    return {
      eventId,
      sessionId: MULTIPART_SESSION_ID,
      partSizeBytes: MULTIPART_PART_SIZE_BYTES,
      partCount: 3,
      expiresAtEpochMs: 1_784_086_400_000,
      completedPartNumbers: [1],
      sessionState: "uploading",
      safeReason: null,
      ...overrides,
    };
  }

  /** One captured allowed content event with its progress already saved. */
  async function captureEventWithProgress(
    repository: JournalRepository,
    normalizedPath: string,
    overrides: Partial<MultipartProgressRecord> = {},
  ): Promise<MultipartProgressRecord> {
    const { event } = await captureAllowed(repository, normalizedPath, fingerprintOf("d1"));
    const record = multipartProgressRecord(event.eventId, overrides);
    await repository.saveMultipartProgress(record);
    return record;
  }

  /**
   * A full database dump for sentinel scans: the raw exported image bytes
   * decoded as text plus every table's rows rendered as JSON, so a sentinel
   * anywhere — live row, freelist page or schema text — is caught.
   */
  function databaseDump(database: SqliteDatabase): string {
    const parts: string[] = [new TextDecoder("latin1").decode(database.exportImage())];
    const tables = database.readAll(
      "select name from sqlite_master where type = 'table' order by name;",
    );
    for (const row of tables[0]?.values ?? []) {
      parts.push(JSON.stringify(database.readAll(`select * from ${String(row[0])};`)));
    }
    return parts.join("\n");
  }

  /**
   * A repository over a counting database wrapper: a test can prove a
   * rejection happened BEFORE any SQL mutation ran by asserting the
   * serialized-mutation counter never moved. The underlying database stays
   * available for dumps and raw session SQL.
   */
  function createCountingJournal(): {
    repository: JournalRepository;
    database: SqliteDatabase;
    readMutationCount: () => number;
  } {
    const { database } = createOpenedJournal();
    let mutationCount = 0;
    const countingDatabase: JournalRepositoryDatabase = {
      runSerializedMutation: (operation) => {
        mutationCount += 1;
        return database.runSerializedMutation(operation);
      },
      readAll: (sql) => database.readAll(sql),
    };
    const repository = new JournalRepository({
      database: countingDatabase,
      nowEpochMs: () => 1_784_000_000_000,
    });
    return { repository, database, readMutationCount: () => mutationCount };
  }

  /**
   * Build one seeded v7 journal image (a tracked file plus one in-flight
   * uploading event — the durable state a real v7 device carries across
   * the upgrade), migrate it in memory to the current schema and open it
   * as a live repository.
   */
  function openV7ThenMigrate(): {
    repository: JournalRepository;
    database: SqliteDatabase;
    eventId: string;
  } {
    const fresh = SqliteDatabase.createEmpty(engineModule, {
      schemaVersion: JOURNAL_SCHEMA_VERSION,
      dirtyGeneration: 4,
      lastVerifiedGeneration: 4,
      isReconcileRequired: false,
      recoveryState: "verified_generation_loaded",
    } satisfies JournalMeta);
    const currentImage = fresh.exportImage();
    fresh.close();
    const engine = new engineModule.Database(currentImage);
    let v7Image: Uint8Array;
    try {
      engine.exec("begin immediate;");
      engine.exec("drop table multipart_upload_progress;");
      engine.exec("update journal_meta set schema_version = 7 where singleton_key = 1;");
      engine.exec("pragma user_version = 7;");
      engine.exec(
        [
          "insert into local_files (local_file_id, normalized_path, source_id,",
          "observed_sha256, observed_size_bytes, observed_media_type, base_version_id,",
          `policy_revision) values ('${V7_LOCAL_FILE_ID}', 'notes/big-asset.bin', null,`,
          `'${"a".repeat(64)}', 20 * 1024 * 1024, 'application/octet-stream', null, 4);`,
        ].join(" "),
      );
      engine.exec(
        [
          "insert into journal_events (event_id, local_file_id, idempotency_key, operation,",
          "sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count,",
          "next_eligible_retry_epoch_ms, safe_error, operation_id, created_at_epoch_ms)",
          `values ('${V7_EVENT_ID}', '${V7_LOCAL_FILE_ID}', '${V7_IDEMPOTENCY_KEY}', 'create',`,
          `'${"a".repeat(64)}', 20 * 1024 * 1024, 'application/octet-stream', 'uploading', 1,`,
          "1, 1784000001000, null, null, 1784000000000);",
        ].join(" "),
      );
      engine.exec("commit;");
      v7Image = engine.export();
    } finally {
      engine.close();
    }
    const database = SqliteDatabase.openFromImage(
      engineModule,
      migrateConflictRepairJournalToPendingRenameIntentSchema(
        engineModule,
        migrateMultipartProgressJournalToConflictRepairSchema(
          engineModule,
          migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, v7Image),
        ),
      ),
    );
    const repository = new JournalRepository({
      database,
      nowEpochMs: () => 1_784_000_000_000,
    });
    return { repository, database, eventId: V7_EVENT_ID };
  }

  it("migrates v7 journal data and persists only safe multipart progress", async () => {
    const { repository, database, eventId } = openV7ThenMigrate();
    try {
      const record = multipartProgressRecord(eventId, { completedPartNumbers: [1, 2] });
      await repository.saveMultipartProgress(record);
      expect(await repository.readMultipartProgress(eventId)).toEqual(record);

      const dump = databaseDump(database);
      expect(dump).not.toContain("X-Amz-Signature");
      expect(dump).not.toContain("provider-upload-id");

      // The seeded v7 evidence survives the migration untouched: the
      // in-flight uploading event reads back with its frozen identity.
      const event = repository.readEvent(eventId);
      expect(event?.state).toBe("uploading");
      expect(event?.idempotencyKey).toBe(V7_IDEMPOTENCY_KEY);
      expect(repository.readLocalFileByLocalFileId(V7_LOCAL_FILE_ID)?.normalizedPath).toBe(
        "notes/big-asset.bin",
      );
    } finally {
      database.close();
    }
  });

  it("saves, updates and clears safe progress on the current journal", async () => {
    const { repository } = createOpenedJournal();
    const record = await captureEventWithProgress(repository, "notes/big-asset.bin");
    expect(await repository.readMultipartProgress(record.eventId)).toEqual(record);

    const updated = multipartProgressRecord(record.eventId, {
      completedPartNumbers: [1, 2, 3],
      sessionState: "completing",
      safeReason: "multipart_part_url_rejected",
    });
    await repository.saveMultipartProgress(updated);
    expect(await repository.readMultipartProgress(record.eventId)).toEqual(updated);

    await repository.clearMultipartProgress(record.eventId);
    expect(await repository.readMultipartProgress(record.eventId)).toBeNull();
    // Clearing is idempotent cleanup, never a failure.
    await repository.clearMultipartProgress(record.eventId);
    expect(await repository.readMultipartProgress(record.eventId)).toBeNull();
  });

  it("retains progress across suspend/offline waiting_retry transitions", async () => {
    const { repository } = createOpenedJournal();
    const record = await captureEventWithProgress(repository, "notes/big-asset.bin");

    await repository.markEventWaitingRetry(record.eventId, "network_offline", 1_784_000_060_000);

    expect(await repository.readMultipartProgress(record.eventId)).toEqual(record);
  });

  it("clears progress in the same mutation as committed, no-change and terminal outcomes", async () => {
    const { repository } = createOpenedJournal();
    const { event: committedEvent } = await captureAllowed(
      repository,
      "notes/committed.bin",
      fingerprintOf("d2"),
    );
    await repository.markEventPreflightStarted(committedEvent.eventId);
    await repository.saveMultipartProgress(multipartProgressRecord(committedEvent.eventId));
    await repository.recordCommittedReceipt({
      eventId: committedEvent.eventId,
      sourceId: "1fbd21b0-0000-4000-8000-0000000000aa",
      baseVersionId: "2fce31c1-0000-4000-8000-0000000000bb",
    });
    expect(await repository.readMultipartProgress(committedEvent.eventId)).toBeNull();

    const { event: noChangeEvent } = await captureAllowed(
      repository,
      "notes/no-change.bin",
      fingerprintOf("d3"),
    );
    await repository.markEventPreflightStarted(noChangeEvent.eventId);
    await repository.saveMultipartProgress(multipartProgressRecord(noChangeEvent.eventId));
    await repository.recordNoChangeReceipt({
      eventId: noChangeEvent.eventId,
      sourceId: "1fbd21b0-0000-4000-8000-0000000000aa",
      baseVersionId: "2fce31c1-0000-4000-8000-0000000000bb",
    });
    expect(await repository.readMultipartProgress(noChangeEvent.eventId)).toBeNull();

    const { event: terminalEvent } = await captureAllowed(
      repository,
      "notes/terminal.bin",
      fingerprintOf("d4"),
    );
    await repository.markEventPreflightStarted(terminalEvent.eventId);
    await repository.saveMultipartProgress(multipartProgressRecord(terminalEvent.eventId));
    await repository.markEventTerminal(terminalEvent.eventId, "integrity_failed", "integrity_failed");
    expect(await repository.readMultipartProgress(terminalEvent.eventId)).toBeNull();
  });

  it("clears progress when pending content is frozen for lifecycle and when the mapping is removed", async () => {
    const { repository } = createOpenedJournal();
    const frozen = await captureEventWithProgress(repository, "notes/frozen.bin");
    await repository.markEventPreflightStarted(frozen.eventId);
    await repository.freezePendingForLocalFile(
      (await repository.readEvent(frozen.eventId))?.localFileId ?? "",
    );
    expect(await repository.readMultipartProgress(frozen.eventId)).toBeNull();

    const removed = await captureEventWithProgress(repository, "notes/removed.bin");
    await repository.removeLocalMapping(
      (await repository.readEvent(removed.eventId))?.localFileId ?? "",
    );
    expect(await repository.readMultipartProgress(removed.eventId)).toBeNull();
  });

  it("rejects unknown and missing record fields before any SQL mutation", async () => {
    const { repository, readMutationCount } = createCountingJournal();
    const { event } = await captureAllowed(repository, "notes/a.bin", fingerprintOf("d5"));
    const mutationsBefore = readMutationCount();

    const unknownFieldRecord = {
      ...multipartProgressRecord(event.eventId),
      signedUrl: "https://r2.example/x?X-Amz-Signature=abc",
    } as never;
    await expect(repository.saveMultipartProgress(unknownFieldRecord)).rejects.toMatchObject({
      reason: "journal_mutation_failed",
    });

    const missingFieldRecord = {
      eventId: event.eventId,
      sessionId: MULTIPART_SESSION_ID,
      partSizeBytes: MULTIPART_PART_SIZE_BYTES,
      partCount: 3,
      expiresAtEpochMs: 1_784_086_400_000,
      completedPartNumbers: [1],
      sessionState: "uploading",
    } as never;
    await expect(repository.saveMultipartProgress(missingFieldRecord)).rejects.toMatchObject({
      reason: "journal_mutation_failed",
    });

    expect(readMutationCount()).toBe(mutationsBefore);
    expect(await repository.readMultipartProgress(event.eventId)).toBeNull();
  });

  it("rejects completed part numbers outside the frozen geometry before any SQL mutation", async () => {
    const { repository, readMutationCount } = createCountingJournal();
    const { event } = await captureAllowed(repository, "notes/b.bin", fingerprintOf("d6"));
    const mutationsBefore = readMutationCount();
    // Geometry is partCount 3: 0 is below one, 4 and 99 exceed it, 1.5 is
    // not a whole part number, and [1, 1], [2, 1], [1, 2, 2] repeat or
    // disorder numbers the server status order never carries.
    const invalidPartNumberSets: readonly (readonly number[])[] = [
      [0],
      [4],
      [99],
      [1.5],
      [1, 1],
      [2, 1],
      [1, 2, 2],
    ];
    for (const completedPartNumbers of invalidPartNumberSets) {
      await expect(
        repository.saveMultipartProgress(multipartProgressRecord(event.eventId, { completedPartNumbers })),
      ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    }

    expect(readMutationCount()).toBe(mutationsBefore);
    expect(await repository.readMultipartProgress(event.eventId)).toBeNull();
  });

  it("rejects hostile session IDs and unknown tokens before any SQL mutation, persisting nothing", async () => {
    const { repository, database, readMutationCount } = createCountingJournal();
    const { event } = await captureAllowed(repository, "notes/c.bin", fingerprintOf("d7"));
    const mutationsBefore = readMutationCount();

    const hostileSessionIds = [
      "https://r2.example/staging?X-Amz-Signature=secret",
      "provider-upload-id",
      "abc",
      "A".repeat(129),
      event.eventId,
    ];
    for (const sessionId of hostileSessionIds) {
      await expect(
        repository.saveMultipartProgress(multipartProgressRecord(event.eventId, { sessionId })),
      ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    }

    await expect(
      repository.saveMultipartProgress(
        multipartProgressRecord(event.eventId, { sessionState: "finished" as never }),
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.saveMultipartProgress(
        multipartProgressRecord(event.eventId, { safeReason: "provider_said_no" as never }),
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.saveMultipartProgress(
        multipartProgressRecord(event.eventId, { partSizeBytes: 4 * 1024 * 1024 }),
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.saveMultipartProgress(
        multipartProgressRecord(event.eventId, { partCount: MAX_MULTIPART_PART_COUNT + 1 }),
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.saveMultipartProgress(multipartProgressRecord(event.eventId, { partCount: 0 })),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    await expect(
      repository.saveMultipartProgress(
        multipartProgressRecord(event.eventId, { expiresAtEpochMs: -1 }),
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    expect(readMutationCount()).toBe(mutationsBefore);
    expect(await repository.readMultipartProgress(event.eventId)).toBeNull();
    const dump = databaseDump(database);
    expect(dump).not.toContain("X-Amz-Signature");
    expect(dump).not.toContain("provider-upload-id");
  });

  it("rejects progress for an unknown or terminal event", async () => {
    const { repository } = createOpenedJournal();
    await expect(
      repository.saveMultipartProgress(
        multipartProgressRecord("00000000-0000-4000-8000-0000000000ff"),
      ),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });

    const { event } = await captureAllowed(repository, "notes/terminal.bin", fingerprintOf("d8"));
    await repository.markEventTerminal(event.eventId, "blocked_size", "blocked_size");
    await expect(
      repository.saveMultipartProgress(multipartProgressRecord(event.eventId)),
    ).rejects.toMatchObject({ reason: "journal_mutation_failed" });
    expect(await repository.readMultipartProgress(event.eventId)).toBeNull();
  });

  it("fails closed on a corrupted persisted progress row", async () => {
    const { repository, database } = createOpenedJournal();
    const record = await captureEventWithProgress(repository, "notes/corrupt.bin");
    await database.runSerializedMutation((session) => {
      session.exec(
        [
          "update multipart_upload_progress set completed_part_numbers_json = '[99]'",
          `where event_id = '${record.eventId}';`,
        ].join(" "),
      );
    });

    expect(() => repository.readMultipartProgress(record.eventId)).toThrowError(
      expect.objectContaining({ reason: "journal_image_invalid" }),
    );
  });
});

// --- the closed multipart status aggregates (multipart task 11) ------------------------------------

describe("JournalRepository multipart status aggregates (multipart task 11)", () => {
  /** One opaque public session-ID-shaped value: printable base64url, not a UUID. */
  const AGGREGATE_SESSION_ID = "bXVsdGlwYXJ0LXNlc3Npb24taWRlbnRpdHktMDEyMzQ1Njc4OTA";

  /** One valid safe progress record for a freshly captured event. */
  async function captureWithProgress(
    repository: JournalRepository,
    normalizedPath: string,
    overrides: Partial<MultipartProgressRecord> = {},
  ): Promise<MultipartProgressRecord> {
    const { event } = await captureAllowed(repository, normalizedPath, fingerprintOf("e1"));
    const record: MultipartProgressRecord = {
      eventId: event.eventId,
      sessionId: AGGREGATE_SESSION_ID,
      partSizeBytes: MULTIPART_PART_SIZE_BYTES,
      partCount: 3,
      expiresAtEpochMs: 1_784_086_400_000,
      completedPartNumbers: [1],
      sessionState: "uploading",
      safeReason: null,
      ...overrides,
    };
    await repository.saveMultipartProgress(record);
    return record;
  }

  /** The zero histogram: every closed session state counts zero. */
  function zeroStateCounts(): Record<string, number> {
    return {
      created: 0,
      uploading: 0,
      completing: 0,
      verifying: 0,
      promoting: 0,
      committed: 0,
      cancelling: 0,
      expired: 0,
      integrity_failed: 0,
      policy_denied: 0,
      cleanup_pending: 0,
      cleaned: 0,
    };
  }

  it("answers the zero histogram and no reason codes on an empty journal", () => {
    const { repository } = createOpenedJournal();
    expect(repository.readMultipartSessionStateCounts()).toEqual(zeroStateCounts());
    expect(repository.readMultipartSafeReasonCodes()).toEqual([]);
  });

  it("aggregates counts by closed session state and closed safe reason", async () => {
    const { repository } = createOpenedJournal();
    await captureWithProgress(repository, "notes/asset-a.bin");
    await captureWithProgress(repository, "notes/asset-b.bin", {
      sessionState: "completing",
      safeReason: "multipart_part_url_rejected",
    });
    await captureWithProgress(repository, "notes/asset-c.bin", {
      sessionState: "uploading",
      safeReason: "multipart_local_content_changed",
    });

    expect(repository.readMultipartSessionStateCounts()).toEqual({
      ...zeroStateCounts(),
      uploading: 2,
      completing: 1,
    });
    expect([...repository.readMultipartSafeReasonCodes()].sort()).toEqual([
      "multipart_local_content_changed",
      "multipart_part_url_rejected",
    ]);
  });

  it("drops a cleared session from the aggregate histogram", async () => {
    const { repository } = createOpenedJournal();
    const record = await captureWithProgress(repository, "notes/asset-a.bin");
    expect(repository.readMultipartSessionStateCounts().uploading).toBe(1);
    await repository.clearMultipartProgress(record.eventId);
    expect(repository.readMultipartSessionStateCounts()).toEqual(zeroStateCounts());
    expect(repository.readMultipartSafeReasonCodes()).toEqual([]);
  });
});
