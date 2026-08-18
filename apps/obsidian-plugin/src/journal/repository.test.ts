import { readFileSync } from "node:fs";
import initSqlJs from "sql.js";
import { beforeAll, describe, expect, it } from "vitest";

import type {
  FrozenFingerprint,
  JournalEvent,
  JournalMeta,
  JournalNonRetryEventState,
  JournalSafeErrorLabel,
} from "./contracts";
import {
  MAX_EVENT_ATTEMPT_HISTORY,
  MAX_FILE_SIZE_BYTES,
  MAX_JOURNAL_SIZE_BYTES,
  MAX_PENDING_EVENTS,
} from "./contracts";
import { JournalRepository } from "./repository";
import { JOURNAL_SCHEMA_VERSION, SqliteDatabase } from "./sqlite-database";
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
  });

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
