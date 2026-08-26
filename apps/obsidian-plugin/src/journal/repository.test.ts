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
