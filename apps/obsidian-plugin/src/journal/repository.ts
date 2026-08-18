/**
 * The journal mutation/query implementation of spec 6.3, 6.4 and 7.2.
 *
 * The repository owns the durable records of the portable journal: the
 * per-file source mapping (`local_files`), the create/update intents with
 * their frozen fingerprints (`journal_events`) and the bounded, redacted
 * attempt ring (`journal_attempts`). It mints the random plugin-local file
 * identity on first sight of a path and one stable event/idempotency UUID
 * pair per recorded intent; the idempotency key is the replay identity later
 * tasks send to the server.
 *
 * Coalescing (spec 7.2): an unsent same-file event may take over a later
 * current fingerprint only while it stays `queued` or `waiting_retry` and
 * preflight has not started. From the moment preflight starts the
 * fingerprint is frozen and every later save becomes a successor event.
 *
 * Queue limits (spec 6.4): reaching 10,000 pending events or a 64 MiB
 * journal image durably sets `reconcile_required` and refuses only NEW
 * per-change rows — in-flight evidence is retained untouched and the user's
 * edit itself is never the thing refused.
 *
 * Privacy (spec 9): every failure surfaces as a closed `JournalStoreError`
 * reason; the attempted-event history carries closed labels and opaque
 * correlation IDs only — no path, digest, credential or provider detail ever
 * reaches a thrown error or a query result beyond the local journal itself.
 */

import type {
  FrozenFingerprint,
  JournalAttempt,
  JournalCaptureAdmission,
  JournalEvent,
  JournalEventState,
  JournalMeta,
  JournalNonRetryEventState,
  JournalOperation,
  JournalSafeErrorLabel,
  LocalFile,
} from "./contracts";
import {
  JOURNAL_CAPTURE_ADMISSIONS,
  JOURNAL_COALESCABLE_EVENT_STATES,
  JOURNAL_EVENT_STATES,
  JOURNAL_NON_RETRY_EVENT_STATES,
  JOURNAL_PENDING_EVENT_STATES,
  JOURNAL_SAFE_ERROR_LABELS,
  MAX_EVENT_ATTEMPT_HISTORY,
  MAX_JOURNAL_SIZE_BYTES,
  MAX_PENDING_EVENTS,
} from "./contracts";
import { isFrozenFingerprintShape } from "./fingerprint";
import { journalStoreError } from "./sqlite-database";
import type { SqliteMutationSession, SqliteQueryResult } from "./sqlite-database";

// --- structural database slice ---------------------------------------------------------------

/**
 * The structural journal database slice the repository mutates and queries:
 * `SqliteDatabase` satisfies it directly, and a persistence-backed
 * composition can substitute the verified-generation commit path.
 */
export interface JournalRepositoryDatabase {
  runSerializedMutation<T>(
    operation: (session: SqliteMutationSession) => T | Promise<T>,
  ): Promise<T>;
  readAll(sql: string): SqliteQueryResult[];
}

// --- inputs and results -------------------------------------------------------------------------

/** One settled capture observation offered to the journal (spec 7.1). */
export interface JournalCaptureInput {
  readonly normalizedPath: string;
  readonly fingerprint: FrozenFingerprint;
  readonly policyRevisionNumber: number;
  readonly admission: JournalCaptureAdmission;
}

/**
 * The outcome of recording one capture: a new event row, a coalesced
 * fingerprint replacement on the same unsent event, or a refusal that kept
 * every existing row and durably flagged `reconcile_required` (spec 6.4).
 */
export type JournalCaptureResult =
  | { readonly outcome: "event_recorded"; readonly event: JournalEvent; readonly localFile: LocalFile }
  | { readonly outcome: "event_coalesced"; readonly event: JournalEvent; readonly localFile: LocalFile }
  | { readonly outcome: "capture_refused"; readonly reason: "reconcile_required" };

/** The canonical receipt returned for one committed event (spec 7.2). */
export interface JournalCommittedReceiptInput {
  readonly eventId: string;
  readonly sourceId: string;
  readonly baseVersionId: string;
}

/** One bounded, redacted attempt-audit row (spec 6.3). */
export interface JournalAttemptInput {
  readonly eventId: string;
  readonly attemptedAtEpochMs: number;
  readonly outcomeLabel: JournalSafeErrorLabel;
  readonly requestCorrelationId: string;
}

export interface JournalRepositoryOptions {
  readonly database: JournalRepositoryDatabase;
  /** Identity mint; defaults to the platform `crypto.randomUUID`. */
  readonly createId?: () => string;
  /** Clock for event creation and attempt timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
}

// --- closed value validation -------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_REQUEST_CORRELATION_ID_LENGTH = 128;

/** Render one validated string as a SQL text literal (quotes doubled). */
function sqlText(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

function isUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
}

function isNonNegativeInteger(value: number): boolean {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isClosedToken(value: string, closedSet: readonly string[]): boolean {
  return typeof value === "string" && closedSet.includes(value);
}

/**
 * A journal path is a normalized Vault locator mirroring the policy locator
 * rules: NFC, relative, `/`-separated, free of control characters, drive or
 * scheme colons in the first segment and traversal segments. Anything else
 * fails closed before a single row is read or written.
 */
function validateNormalizedPath(normalizedPath: string): void {
  if (
    typeof normalizedPath !== "string" ||
    normalizedPath.length === 0 ||
    normalizedPath.normalize("NFC") !== normalizedPath ||
    normalizedPath.includes("\\") ||
    normalizedPath.startsWith("/") ||
    normalizedPath.endsWith("/")
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  for (const character of normalizedPath) {
    const codeUnit = character.charCodeAt(0);
    if (codeUnit < 0x20 || codeUnit === 0x7f) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  const segments = normalizedPath.split("/");
  if (segments[0]?.includes(":")) {
    throw journalStoreError("journal_mutation_failed");
  }
  for (const segment of segments) {
    if (segment === "" || segment === "." || segment === "..") {
      throw journalStoreError("journal_mutation_failed");
    }
  }
}

/** Validate one capture input completely; reject before any SQL runs. */
function validateCaptureInput(input: JournalCaptureInput): void {
  validateNormalizedPath(input.normalizedPath);
  if (!isFrozenFingerprintShape(input.fingerprint)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isNonNegativeInteger(input.policyRevisionNumber)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isClosedToken(input.admission, JOURNAL_CAPTURE_ADMISSIONS)) {
    throw journalStoreError("journal_mutation_failed");
  }
}

// --- row parsing --------------------------------------------------------------------------------

/** One stored journal event row including the internal freeze marker. */
interface StoredJournalEvent extends JournalEvent {
  readonly isFingerprintFrozen: boolean;
}

const LOCAL_FILE_COLUMNS = [
  "local_file_id",
  "normalized_path",
  "source_id",
  "observed_sha256",
  "observed_size_bytes",
  "observed_media_type",
  "base_version_id",
  "policy_revision",
] as const;

const JOURNAL_EVENT_COLUMNS = [
  "event_id",
  "local_file_id",
  "idempotency_key",
  "operation",
  "sha256",
  "size_bytes",
  "media_type",
  "state",
  "is_fingerprint_frozen",
  "attempt_count",
  "next_eligible_retry_epoch_ms",
  "safe_error",
  "operation_id",
  "created_at_epoch_ms",
] as const;

const JOURNAL_ATTEMPT_COLUMNS = [
  "event_id",
  "attempted_at_epoch_ms",
  "outcome_label",
  "request_correlation_id",
] as const;

function isNullableText(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isInteger(value) && value >= 0);
}

/** Parse one persisted local-file row; a contract violation is image corruption. */
function parseLocalFileRow(row: readonly unknown[]): LocalFile {
  const [
    localFileId,
    normalizedPath,
    sourceId,
    observedSha256,
    observedSizeBytes,
    observedMediaType,
    baseVersionId,
    policyRevision,
  ] = row;
  if (
    typeof localFileId !== "string" ||
    typeof normalizedPath !== "string" ||
    !isNullableText(sourceId) ||
    typeof observedSha256 !== "string" ||
    typeof observedSizeBytes !== "number" ||
    typeof observedMediaType !== "string" ||
    !isNullableText(baseVersionId) ||
    typeof policyRevision !== "number"
  ) {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    localFileId,
    normalizedPath,
    sourceId,
    observedFingerprint: {
      sha256: observedSha256,
      sizeBytes: observedSizeBytes,
      mediaType: observedMediaType,
    },
    baseVersionId,
    policyRevisionNumber: policyRevision,
  };
}

/** Parse one persisted event row; a contract violation is image corruption. */
function parseJournalEventRow(row: readonly unknown[]): StoredJournalEvent {
  const [
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    sha256,
    sizeBytes,
    mediaType,
    state,
    isFingerprintFrozen,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId,
  ] = row;
  if (
    typeof eventId !== "string" ||
    typeof localFileId !== "string" ||
    typeof idempotencyKey !== "string" ||
    (operation !== "create" && operation !== "update") ||
    typeof sha256 !== "string" ||
    typeof sizeBytes !== "number" ||
    typeof mediaType !== "string" ||
    typeof state !== "string" ||
    !isClosedToken(state, JOURNAL_EVENT_STATES) ||
    (isFingerprintFrozen !== 0 && isFingerprintFrozen !== 1) ||
    typeof attemptCount !== "number" ||
    !isNullableNonNegativeInteger(nextEligibleRetryEpochMs) ||
    !isNullableText(safeError) ||
    !isNullableText(operationId)
  ) {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    fingerprint: { sha256, sizeBytes, mediaType },
    state: state as JournalEventState,
    isFingerprintFrozen: isFingerprintFrozen === 1,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError: safeError as JournalSafeErrorLabel | null,
    operationId,
  };
}

/** Parse one persisted attempt row; a contract violation is image corruption. */
function parseJournalAttemptRow(row: readonly unknown[]): JournalAttempt {
  const [eventId, attemptedAtEpochMs, outcomeLabel, requestCorrelationId] = row;
  if (
    typeof eventId !== "string" ||
    typeof attemptedAtEpochMs !== "number" ||
    typeof outcomeLabel !== "string" ||
    !isClosedToken(outcomeLabel, JOURNAL_SAFE_ERROR_LABELS) ||
    typeof requestCorrelationId !== "string"
  ) {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    eventId,
    attemptedAtEpochMs,
    outcomeLabel: outcomeLabel as JournalSafeErrorLabel,
    requestCorrelationId,
  };
}

/** Strip the internal freeze marker from the public event shape. */
function toPublicEvent(event: StoredJournalEvent): JournalEvent {
  const { eventId, localFileId, idempotencyKey, operation, fingerprint, state, attemptCount, nextEligibleRetryEpochMs, safeError, operationId } = event;
  return {
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    fingerprint,
    state,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId,
  };
}

function firstRow(result: readonly SqliteQueryResult[]): readonly unknown[] | null {
  return result[0]?.values[0] ?? null;
}

function selectColumns(table: string, columns: readonly string[], suffix: string): string {
  return `select ${columns.join(", ")} from ${table} ${suffix};`;
}

// --- the repository --------------------------------------------------------------------------------

/**
 * The durable journal record store. Every mutation runs inside the single
 * serialized writer so each capture, transition, receipt and attempt lands
 * in exactly one transaction; every query is read-only.
 */
export class JournalRepository {
  readonly #database: JournalRepositoryDatabase;
  readonly #createId: () => string;
  readonly #nowEpochMs: () => number;

  constructor(options: JournalRepositoryOptions) {
    this.#database = options.database;
    this.#createId = options.createId ?? (() => crypto.randomUUID());
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
  }

  // --- capture (spec 6.3, 7.1, 7.2) ---------------------------------------------------------

  /**
   * Record one settled capture. A `policy_allowed` capture replaces the
   * fingerprint of the file's unfrozen `queued`/`waiting_retry` event
   * (coalescing, spec 7.2) or appends a new event — born terminal for the
   * two fail-closed blocked admissions. New rows are refused once either
   * queue soft limit is reached; the refusal flags `reconcile_required`
   * durably and preserves every existing row (spec 6.4).
   */
  async recordCapture(input: JournalCaptureInput): Promise<JournalCaptureResult> {
    validateCaptureInput(input);
    return this.#database.runSerializedMutation((session) => {
      const read = (sql: string): readonly SqliteQueryResult[] => session.readRows(sql);
      const exec = (sql: string): void => session.exec(sql);
      const existingFile = this.#readLocalFileRow(read, input.normalizedPath);
      const localFileId = existingFile?.localFileId ?? this.#createId();
      const operation: JournalOperation = existingFile?.sourceId != null ? "update" : "create";

      // Coalescing: only an unsent event whose preflight never started (spec 7.2).
      const coalescableEvent =
        input.admission === "policy_allowed"
          ? this.#readCoalescableEventRow(session, localFileId)
          : null;
      if (coalescableEvent !== null) {
        session.exec(
          [
            "update journal_events set",
            `operation = ${sqlText(operation)},`,
            `sha256 = ${sqlText(input.fingerprint.sha256)},`,
            `size_bytes = ${input.fingerprint.sizeBytes},`,
            `media_type = ${sqlText(input.fingerprint.mediaType)}`,
            `where event_id = ${sqlText(coalescableEvent.eventId)};`,
          ].join(" "),
        );
        this.#writeObservedFingerprint(exec, localFileId, input);
        return {
          outcome: "event_coalesced",
          event: toPublicEvent({ ...coalescableEvent, operation, fingerprint: input.fingerprint }),
          localFile: this.#requireLocalFileRow(read, input.normalizedPath),
        };
      }

      // New per-change row: both soft limits are checked first (spec 6.4).
      if (this.#isAtQueueLimit(session)) {
        this.#persistReconcileRequired(session);
        return { outcome: "capture_refused", reason: "reconcile_required" };
      }

      const initialState: JournalEventState =
        input.admission === "policy_allowed" ? "queued" : input.admission;
      const eventId = this.#createId();
      const idempotencyKey = this.#createId();
      if (existingFile === null) {
        session.exec(
          [
            "insert into local_files (local_file_id, normalized_path, source_id,",
            "observed_sha256, observed_size_bytes, observed_media_type, base_version_id,",
            `policy_revision) values (${sqlText(localFileId)},`,
            `${sqlText(input.normalizedPath)}, null,`,
            `${sqlText(input.fingerprint.sha256)}, ${input.fingerprint.sizeBytes},`,
            `${sqlText(input.fingerprint.mediaType)}, null, ${input.policyRevisionNumber});`,
          ].join(" "),
        );
      } else {
        this.#writeObservedFingerprint(exec, localFileId, input);
      }
      session.exec(
        [
          "insert into journal_events (event_id, local_file_id, idempotency_key, operation,",
          "sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count,",
          "safe_error, created_at_epoch_ms) values (",
          `${sqlText(eventId)}, ${sqlText(localFileId)}, ${sqlText(idempotencyKey)},`,
          `${sqlText(operation)}, ${sqlText(input.fingerprint.sha256)},`,
          `${input.fingerprint.sizeBytes}, ${sqlText(input.fingerprint.mediaType)},`,
          `${sqlText(initialState)},`,
          `${input.admission === "policy_allowed" ? 0 : 1}, 0,`,
          `${input.admission === "policy_allowed" ? "null" : sqlText(input.admission)},`,
          `${this.#nowEpochMs()});`,
        ].join(" "),
      );

      // Reaching either limit durably flags reconciliation; the row itself lands.
      if (this.#isAtQueueLimit(session)) {
        this.#persistReconcileRequired(session);
      }
      const event: JournalEvent = {
        eventId,
        localFileId,
        idempotencyKey,
        operation,
        fingerprint: input.fingerprint,
        state: initialState,
        attemptCount: 0,
        nextEligibleRetryEpochMs: null,
        safeError: input.admission === "policy_allowed" ? null : input.admission,
        operationId: null,
      };
      return {
        outcome: "event_recorded",
        event,
        localFile: this.#requireLocalFileRow(read, input.normalizedPath),
      };
    });
  }

  // --- event transitions (spec 7.2) ------------------------------------------------------------

  /**
   * Move one eligible event into `preflight`, freezing its fingerprint from
   * this moment on: the event and its fingerprint never change afterwards,
   * and any later save becomes a successor event (spec 7.2).
   */
  async markEventPreflightStarted(eventId: string): Promise<void> {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        eventId,
      );
      if (!isClosedToken(event.state, JOURNAL_COALESCABLE_EVENT_STATES)) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        `update journal_events set state = 'preflight', is_fingerprint_frozen = 1 where event_id = ${sqlText(eventId)};`,
      );
    });
  }

  /**
   * Record one retryable failure: the event returns to `waiting_retry` with
   * a closed safe error label and its next eligible retry time. Terminal
   * states never receive this transition (spec 7.2, 12).
   */
  async markEventWaitingRetry(
    eventId: string,
    safeError: JournalSafeErrorLabel,
    nextEligibleRetryEpochMs: number,
  ): Promise<void> {
    if (
      !isUuid(eventId) ||
      !isClosedToken(safeError, JOURNAL_SAFE_ERROR_LABELS) ||
      !isNonNegativeInteger(nextEligibleRetryEpochMs)
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        eventId,
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "update journal_events set state = 'waiting_retry',",
          "attempt_count = attempt_count + 1,",
          `next_eligible_retry_epoch_ms = ${nextEligibleRetryEpochMs},`,
          `safe_error = ${sqlText(safeError)}`,
          `where event_id = ${sqlText(eventId)};`,
        ].join(" "),
      );
    });
  }

  /**
   * Close one event in a terminal non-retry state with a closed safe error
   * label. Terminal rows stay queryable forever; they are never deleted and
   * never transition again (spec 6.4, 7.2).
   */
  async markEventTerminal(
    eventId: string,
    terminalState: JournalNonRetryEventState,
    safeError: JournalSafeErrorLabel,
  ): Promise<void> {
    if (
      !isUuid(eventId) ||
      !isClosedToken(terminalState, JOURNAL_NON_RETRY_EVENT_STATES) ||
      !isClosedToken(safeError, JOURNAL_SAFE_ERROR_LABELS)
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        eventId,
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "update journal_events set",
          `state = ${sqlText(terminalState)},`,
          "next_eligible_retry_epoch_ms = null,",
          `safe_error = ${sqlText(safeError)},`,
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText(eventId)};`,
        ].join(" "),
      );
    });
  }

  // --- receipts and attempts (spec 6.3, 7.2) ----------------------------------------------------

  /**
   * Persist the canonical receipt of one committed event: the event closes
   * as `committed` and its file takes the server-returned source and base
   * version identities. The observed fingerprint is left untouched — a
   * successor capture may already have observed newer bytes.
   */
  async recordCommittedReceipt(input: JournalCommittedReceiptInput): Promise<void> {
    if (
      !isUuid(input.eventId) ||
      !isUuid(input.sourceId) ||
      !isUuid(input.baseVersionId)
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        input.eventId,
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "update journal_events set state = 'committed',",
          "next_eligible_retry_epoch_ms = null, safe_error = null,",
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText(input.eventId)};`,
        ].join(" "),
      );
      session.exec(
        [
          "update local_files set",
          `source_id = ${sqlText(input.sourceId)},`,
          `base_version_id = ${sqlText(input.baseVersionId)}`,
          `where local_file_id = ${sqlText(event.localFileId)};`,
        ].join(" "),
      );
    });
  }

  /**
   * Append one redacted attempt-audit row and prune the per-event ring to
   * the most recent {@link MAX_EVENT_ATTEMPT_HISTORY} entries inside the
   * same transaction (spec 6.3).
   */
  async recordEventAttempt(input: JournalAttemptInput): Promise<void> {
    if (
      !isUuid(input.eventId) ||
      !isNonNegativeInteger(input.attemptedAtEpochMs) ||
      !isClosedToken(input.outcomeLabel, JOURNAL_SAFE_ERROR_LABELS) ||
      typeof input.requestCorrelationId !== "string" ||
      input.requestCorrelationId.length === 0 ||
      input.requestCorrelationId.length > MAX_REQUEST_CORRELATION_ID_LENGTH
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    for (const character of input.requestCorrelationId) {
      const codeUnit = character.charCodeAt(0);
      if (codeUnit < 0x20 || codeUnit > 0x7e) {
        throw journalStoreError("journal_mutation_failed");
      }
    }
    await this.#database.runSerializedMutation((session) => {
      this.#requireEventRow(
        (sql) => session.readRows(sql),
        input.eventId,
      );
      session.exec(
        [
          "insert into journal_attempts (event_id, attempted_at_epoch_ms, outcome_label,",
          "request_correlation_id) values (",
          `${sqlText(input.eventId)}, ${input.attemptedAtEpochMs},`,
          `${sqlText(input.outcomeLabel)}, ${sqlText(input.requestCorrelationId)});`,
        ].join(" "),
      );
      session.exec(
        [
          "delete from journal_attempts where event_id = ",
          `${sqlText(input.eventId)} and attempt_ordinal not in (`,
          "select attempt_ordinal from journal_attempts",
          `where event_id = ${sqlText(input.eventId)}`,
          `order by attempt_ordinal desc limit ${MAX_EVENT_ATTEMPT_HISTORY});`,
        ].join(" "),
      );
    });
  }

  // --- queries (spec 6.3, 9) -----------------------------------------------------------------------

  /** One event by identity, or null; the shape never includes internals. */
  readEvent(eventId: string): JournalEvent | null {
    const row = firstRow(
      this.#database.readAll(
        selectColumns("journal_events", JOURNAL_EVENT_COLUMNS, `where event_id = ${sqlText(eventId)}`),
      ),
    );
    return row === null ? null : toPublicEvent(parseJournalEventRow(row));
  }

  /** Every event of one file, oldest first — terminal history included. */
  readEventsByLocalFileId(localFileId: string): readonly JournalEvent[] {
    const result = this.#database.readAll(
      selectColumns(
        "journal_events",
        JOURNAL_EVENT_COLUMNS,
        `where local_file_id = ${sqlText(localFileId)} order by created_at_epoch_ms asc, rowid asc`,
      ),
    );
    return (result[0]?.values ?? []).map((row) => toPublicEvent(parseJournalEventRow(row)));
  }

  /** One tracked file by its normalized current path, or null. */
  readLocalFileByPath(normalizedPath: string): LocalFile | null {
    validateNormalizedPath(normalizedPath);
    return this.#readLocalFileRow((sql) => this.#database.readAll(sql), normalizedPath);
  }

  /** The bounded attempted-event history of one event, oldest first (redacted). */
  readEventAttemptHistory(eventId: string): readonly JournalAttempt[] {
    const result = this.#database.readAll(
      selectColumns(
        "journal_attempts",
        JOURNAL_ATTEMPT_COLUMNS,
        `where event_id = ${sqlText(eventId)} order by attempt_ordinal asc`,
      ),
    );
    return (result[0]?.values ?? []).map((row) => parseJournalAttemptRow(row));
  }

  /** The number of events that still owe work (the spec-6.4 pending count). */
  countPendingEvents(): number {
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText(state)).join(", ");
    const row = firstRow(
      this.#database.readAll(
        `select count(*) from journal_events where state in (${pendingStateList});`,
      ),
    );
    const count = row?.[0];
    return typeof count === "number" ? count : 0;
  }

  // --- internals ------------------------------------------------------------------------------------

  #readLocalFileRow(
    read: (sql: string) => readonly SqliteQueryResult[],
    normalizedPath: string,
  ): LocalFile | null {
    const row = firstRow(
      read(
        selectColumns(
          "local_files",
          LOCAL_FILE_COLUMNS,
          `where normalized_path = ${sqlText(normalizedPath)}`,
        ),
      ),
    );
    return row === null ? null : parseLocalFileRow(row);
  }

  #requireLocalFileRow(
    read: (sql: string) => readonly SqliteQueryResult[],
    normalizedPath: string,
  ): LocalFile {
    const localFile = this.#readLocalFileRow(read, normalizedPath);
    if (localFile === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    return localFile;
  }

  #requireEventRow(
    read: (sql: string) => readonly SqliteQueryResult[],
    eventId: string,
  ): StoredJournalEvent {
    const row = firstRow(
      read(selectColumns("journal_events", JOURNAL_EVENT_COLUMNS, `where event_id = ${sqlText(eventId)}`)),
    );
    if (row === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    return parseJournalEventRow(row);
  }

  #requireNonTerminalEvent(event: StoredJournalEvent): void {
    if (!isClosedToken(event.state, JOURNAL_PENDING_EVENT_STATES)) {
      throw journalStoreError("journal_mutation_failed");
    }
  }

  /** The newest same-file event still replaceable before preflight (spec 7.2). */
  #readCoalescableEventRow(
    session: SqliteMutationSession,
    localFileId: string,
  ): StoredJournalEvent | null {
    const coalescableStateList = JOURNAL_COALESCABLE_EVENT_STATES.map((state) => sqlText(state)).join(", ");
    const row = firstRow(
      session.readRows(
        selectColumns(
          "journal_events",
          JOURNAL_EVENT_COLUMNS,
          [
            `where local_file_id = ${sqlText(localFileId)}`,
            `and state in (${coalescableStateList})`,
            "and is_fingerprint_frozen = 0",
            "order by created_at_epoch_ms desc, rowid desc limit 1",
          ].join(" "),
        ),
      ),
    );
    return row === null ? null : parseJournalEventRow(row);
  }

  /** Whether either spec-6.4 soft limit is reached inside this transaction. */
  #isAtQueueLimit(session: SqliteMutationSession): boolean {
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText(state)).join(", ");
    const pendingRow = firstRow(
      session.readRows(
        `select count(*) from journal_events where state in (${pendingStateList});`,
      ),
    );
    const pageCountRow = firstRow(session.readRows("pragma page_count;"));
    const pageSizeRow = firstRow(session.readRows("pragma page_size;"));
    const pendingCount = pendingRow?.[0];
    const pageCount = pageCountRow?.[0];
    const pageSize = pageSizeRow?.[0];
    if (typeof pendingCount !== "number" || typeof pageCount !== "number" || typeof pageSize !== "number") {
      throw journalStoreError("journal_query_failed");
    }
    return pendingCount >= MAX_PENDING_EVENTS || pageCount * pageSize >= MAX_JOURNAL_SIZE_BYTES;
  }

  /** Durably flag the journal for child-6 reconciliation (spec 6.4, 12). */
  #persistReconcileRequired(session: SqliteMutationSession): void {
    const meta: JournalMeta = session.readJournalMeta();
    if (!meta.isReconcileRequired) {
      session.writeJournalMeta({ ...meta, isReconcileRequired: true });
    }
  }

  /** Refresh only the observed fingerprint columns of one tracked file. */
  #writeObservedFingerprint(
    exec: (sql: string) => void,
    localFileId: string,
    input: JournalCaptureInput,
  ): void {
    exec(
      [
        "update local_files set",
        `observed_sha256 = ${sqlText(input.fingerprint.sha256)},`,
        `observed_size_bytes = ${input.fingerprint.sizeBytes},`,
        `observed_media_type = ${sqlText(input.fingerprint.mediaType)},`,
        `policy_revision = ${input.policyRevisionNumber}`,
        `where local_file_id = ${sqlText(localFileId)};`,
      ].join(" "),
    );
  }
}
