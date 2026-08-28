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
  MultipartProgressRecord,
} from "./contracts";
import {
  LIFECYCLE_LOCAL_FILE_STATES,
  type LifecycleLocalFileState,
} from "./lifecycle-contracts";
import { LifecycleRepository as JournalLifecycleRepository } from "./lifecycle-repository";
import { DeviceSyncRepository } from "../device-sync/repository";
import type {
  CompleteLocalRepair,
  DeviceSyncReason,
  DeviceSyncRepository as DeviceSyncRepositoryPort,
  ManifestActionKind,
  ManifestActionProgressOutcome,
} from "../device-sync/contracts";
import {
  MANIFEST_ACTION_KINDS,
  MANIFEST_ACTION_PROGRESS_OUTCOMES,
} from "../device-sync/contracts";
import { isDeviceSyncReason, readDeviceSyncState } from "../device-sync/schema";
import {
  JOURNAL_CAPTURE_ADMISSIONS,
  JOURNAL_COALESCABLE_EVENT_STATES,
  JOURNAL_EVENT_STATES,
  JOURNAL_NON_RETRY_EVENT_STATES,
  JOURNAL_OPERATIONS,
  JOURNAL_PENDING_EVENT_STATES,
  JOURNAL_SAFE_ERROR_LABELS,
  MAX_EVENT_ATTEMPT_HISTORY,
  MAX_JOURNAL_SIZE_BYTES,
  MAX_MULTIPART_PART_COUNT,
  MAX_PENDING_EVENTS,
  MULTIPART_PART_SIZE_BYTES,
  MULTIPART_SAFE_REASON_TOKENS,
  MULTIPART_SESSION_STATES,
} from "./contracts";
import { isFrozenFingerprintShape } from "./fingerprint";
import {
  projectLocalNoteSyncStatus,
  type LocalNoteSyncStatus,
} from "./note-status";
import { JournalStoreError, journalStoreError } from "./sqlite-database";
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

/**
 * One durable manifest page progress row of the active repair run (task
 * 11, spec 7.3): the ordered page number, its accepted entry count and
 * page digest — the exact-resume evidence of the pages already accepted
 * server-side.
 */
export interface ManifestPageProgressRecord {
  readonly pageNumber: number;
  readonly entryCount: number;
  readonly pageDigest: string;
}

/**
 * One durable manifest action progress row of the active repair run
 * (task 11, spec 12.4): the planned action index/kind, its local progress
 * outcome and the closed reason a terminal-safe blocker settled under.
 */
export interface ManifestActionProgressRecord {
  readonly actionIndex: number;
  readonly actionKind: ManifestActionKind;
  readonly outcome: ManifestActionProgressOutcome;
  readonly reason: DeviceSyncReason | null;
}

/** One bounded, redacted attempt-audit row (spec 6.3). */
export interface JournalAttemptInput {
  readonly eventId: string;
  readonly attemptedAtEpochMs: number;
  readonly outcomeLabel: JournalSafeErrorLabel;
  readonly requestCorrelationId: string;
}

/**
 * One redacted histogram row of the status projection (spec 11): a closed
 * event state, the closed safe error label its rows carry (null for none)
 * and how many events fall in that group — counts and closed labels only.
 */
export interface JournalEventStateErrorCount {
  readonly state: JournalEventState;
  readonly safeError: JournalSafeErrorLabel | null;
  readonly eventCount: number;
}

export interface JournalRepositoryOptions {
  readonly database: JournalRepositoryDatabase;
  /** Identity mint; defaults to the platform `crypto.randomUUID`. */
  readonly createId?: () => string;
  /** Clock for event creation and attempt timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
  /**
   * Optional lifecycle factory; the shared facade constructs the
   * {@link LifecycleRepository} over the same `database` slice so the
   * child-5 surface composes into the existing writer without a
   * parallel SQL channel. Defaults to the canonical lifecycle
   * repository wired against `database` and the inherited identity /
   * clock seams.
   */
  readonly createLifecycleRepository?: (deps: {
    readonly database: JournalRepositoryDatabase;
    readonly createId: () => string;
    readonly nowEpochMs: () => number;
  }) => JournalLifecycleRepository;
  /**
   * Optional device-sync factory (task 8); the shared facade constructs
   * the {@link DeviceSyncRepositoryPort} over the same `database` slice so
   * the schema-v7 reconciliation state composes into the existing writer
   * without a parallel SQL channel. Defaults to the canonical
   * device-sync repository wired against `database`.
   */
  readonly createDeviceSyncRepository?: (deps: {
    readonly database: JournalRepositoryDatabase;
  }) => DeviceSyncRepositoryPort;
  /**
   * The reconcile-complete notification of the device repair completion
   * (task 11, spec 12.4): invoked BEFORE the completion commits so a
   * persistence-composed journal can honor the repository-transaction
   * clear of `journal_meta.is_reconcile_required` (the composition root
   * wires `JournalPersistence.markReconcileComplete` here).
   */
  readonly onDeviceSyncRepairComplete?: () => void;
}

// --- closed value validation -------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_REQUEST_CORRELATION_ID_LENGTH = 128;

/**
 * The opaque upload operation handle grammar mirrored from the server's
 * `UploadOperationToken`: printable URL-safe base64url of 32 to 128
 * characters. The token is an implementation handle, never a canonical
 * identity, so no UUID form is special-cased locally.
 */
const OPERATION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

/** Whether one value carries the opaque operation handle grammar. */
function isOperationTokenShape(value: string): boolean {
  return typeof value === "string" && OPERATION_TOKEN_PATTERN.test(value);
}

/** Render one validated string as a SQL text literal (quotes doubled). */
function sqlText(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/**
 * Whether one string is a canonical lowercase UUID — the exact shape
 * {@link JournalRepository.markEventWaitingRetry}'s own argument
 * validation checks. Exported (diagnostic round U2) so the queue driver's
 * park throw-site discriminator re-checks the SAME precondition outside
 * the repository, with identical semantics.
 */
export function isUuid(value: string): boolean {
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

// --- closed multipart progress value validation (child 7 spec 4.1, task 9) ----------------------

/**
 * The opaque public multipart session-ID grammar mirrored from the
 * server's `MultipartUploadSessionId`: printable URL-safe base64url of 32
 * to 128 characters. The grammar deliberately refuses the raw canonical
 * UUID form so a session ID can never be a journal identity or canonical
 * database UUID in disguise — and because `:`, `/`, `?`, `&` and `=` are
 * not base64url characters, no signed URL, query signature, provider
 * handle or other locator text can survive it.
 */
const MULTIPART_SESSION_ID_MIN_LENGTH = 32;
const MULTIPART_SESSION_ID_MAX_LENGTH = 128;
const MULTIPART_SESSION_ID_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

/** Whether one value carries the opaque public session handle grammar. */
function isMultipartSessionIdShape(value: string): boolean {
  return (
    typeof value === "string" &&
    value.length >= MULTIPART_SESSION_ID_MIN_LENGTH &&
    value.length <= MULTIPART_SESSION_ID_MAX_LENGTH &&
    MULTIPART_SESSION_ID_PATTERN.test(value) &&
    !isUuid(value)
  );
}

/** The exact own-key set one safe progress record may carry — no more, no less. */
const MULTIPART_PROGRESS_RECORD_KEYS = [
  "completedPartNumbers",
  "eventId",
  "expiresAtEpochMs",
  "partCount",
  "partSizeBytes",
  "safeReason",
  "sessionId",
  "sessionState",
] as const satisfies readonly (keyof MultipartProgressRecord)[];

/**
 * Validate one multipart progress record completely (child 7 spec 4.1)
 * BEFORE any SQL runs: exactly the contract's own keys (unknown and
 * missing fields both reject), the event UUID, the opaque session-ID
 * grammar, the frozen geometry (an exactly-8-MiB ordinary part, 1 to 13
 * parts), a non-negative expiry, strictly ascending whole completed part
 * numbers each inside the geometry, a closed session state and a closed
 * (or absent) safe reason. Nothing outside this closed shape can reach
 * SQLite, so no URL, provider identity, digest or locator can persist.
 */
function validateMultipartProgressRecord(record: MultipartProgressRecord): void {
  if (typeof record !== "object" || record === null) {
    throw journalStoreError("journal_mutation_failed");
  }
  const keys = Object.keys(record).sort();
  const expectedKeys = [...MULTIPART_PROGRESS_RECORD_KEYS].sort();
  if (
    keys.length !== expectedKeys.length ||
    keys.some((key, index) => key !== expectedKeys[index])
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isUuid(record.eventId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isMultipartSessionIdShape(record.sessionId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (
    typeof record.partSizeBytes !== "number" ||
    !Number.isInteger(record.partSizeBytes) ||
    record.partSizeBytes !== MULTIPART_PART_SIZE_BYTES
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (
    typeof record.partCount !== "number" ||
    !Number.isInteger(record.partCount) ||
    record.partCount < 1 ||
    record.partCount > MAX_MULTIPART_PART_COUNT
  ) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isNonNegativeInteger(record.expiresAtEpochMs)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!Array.isArray(record.completedPartNumbers)) {
    throw journalStoreError("journal_mutation_failed");
  }
  let previousPartNumber = 0;
  for (const partNumber of record.completedPartNumbers) {
    if (
      typeof partNumber !== "number" ||
      !Number.isInteger(partNumber) ||
      partNumber < 1 ||
      partNumber > record.partCount ||
      partNumber <= previousPartNumber
    ) {
      throw journalStoreError("journal_mutation_failed");
    }
    previousPartNumber = partNumber;
  }
  if (!isClosedToken(record.sessionState, MULTIPART_SESSION_STATES)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (record.safeReason !== null && !isClosedToken(record.safeReason, MULTIPART_SAFE_REASON_TOKENS)) {
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
  "last_committed_sha256",
  "last_committed_size_bytes",
  "last_committed_media_type",
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
    lastCommittedSha256,
    lastCommittedSizeBytes,
    lastCommittedMediaType,
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
  const lastCommittedFingerprint =
    typeof lastCommittedSha256 === "string" &&
    typeof lastCommittedSizeBytes === "number" &&
    typeof lastCommittedMediaType === "string"
      ? {
          sha256: lastCommittedSha256,
          sizeBytes: lastCommittedSizeBytes,
          mediaType: lastCommittedMediaType,
        }
      : null;
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
    lastCommittedFingerprint,
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
    !isClosedToken(String(operation), [...JOURNAL_OPERATIONS]) ||
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
    operation: operation as JournalOperation,
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

/**
 * Parse one persisted multipart progress row back into the frozen record
 * shape (child 7 spec 4.1). The completed part numbers re-validate against
 * the stored geometry through the same closed validator mutations use; a
 * row that violates the contract — a torn JSON array, an out-of-geometry
 * part number, a foreign token — is image corruption and fails closed as
 * `journal_image_invalid`.
 */
function parseMultipartProgressRow(
  row: readonly unknown[],
  eventId: string,
): MultipartProgressRecord {
  const [
    sessionId,
    partSizeBytes,
    partCount,
    expiresAtEpochMs,
    completedPartNumbersJson,
    sessionState,
    safeReason,
  ] = row;
  let completedPartNumbers: unknown;
  if (typeof completedPartNumbersJson !== "string") {
    throw journalStoreError("journal_image_invalid");
  }
  try {
    completedPartNumbers = JSON.parse(completedPartNumbersJson);
  } catch {
    throw journalStoreError("journal_image_invalid");
  }
  const candidate = {
    eventId,
    sessionId,
    partSizeBytes,
    partCount,
    expiresAtEpochMs,
    completedPartNumbers,
    sessionState,
    safeReason,
  } as MultipartProgressRecord;
  try {
    validateMultipartProgressRecord(candidate);
  } catch (error) {
    if (error instanceof JournalStoreError) {
      throw journalStoreError("journal_image_invalid");
    }
    throw error;
  }
  return candidate;
}

/** Parse one latest-event join row before passing only closed fields to the local UI projection. */
function parseLocalNoteSyncStatusRow(
  row: readonly unknown[],
  isReconcileRequired: boolean,
): LocalNoteSyncStatus {
  const [
    normalizedPath,
    policyRevisionNumber,
    observedSha256,
    observedSizeBytes,
    observedMediaType,
    lastCommittedSha256,
    lastCommittedSizeBytes,
    lastCommittedMediaType,
    ...eventRow
  ] = row;
  if (
    typeof normalizedPath !== "string" ||
    typeof policyRevisionNumber !== "number" ||
    !Number.isInteger(policyRevisionNumber) ||
    policyRevisionNumber < 0 ||
    typeof observedSha256 !== "string" ||
    typeof observedSizeBytes !== "number" ||
    !Number.isInteger(observedSizeBytes) ||
    observedSizeBytes < 0 ||
    typeof observedMediaType !== "string"
  ) {
    throw journalStoreError("journal_image_invalid");
  }
  const lastCommittedFingerprint =
    typeof lastCommittedSha256 === "string" &&
    typeof lastCommittedSizeBytes === "number" &&
    typeof lastCommittedMediaType === "string"
      ? {
          sha256: lastCommittedSha256,
          sizeBytes: lastCommittedSizeBytes,
          mediaType: lastCommittedMediaType,
        }
      : null;
  const latestEvent = eventRow[0] === null
    ? null
    : parseJournalEventRow(eventRow);
  if (latestEvent === null && eventRow.some((value) => value !== null)) {
    throw journalStoreError("journal_image_invalid");
  }
  return projectLocalNoteSyncStatus({
    normalizedPath,
    policyRevisionNumber,
    observedFingerprint: {
      sha256: observedSha256,
      sizeBytes: observedSizeBytes,
      mediaType: observedMediaType,
    },
    lastCommittedFingerprint,
    latestEvent,
    isReconcileRequired,
  });
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
  readonly #lifecycle: JournalLifecycleRepository;
  readonly #deviceSync: DeviceSyncRepositoryPort;
  readonly #onDeviceSyncRepairComplete: (() => void) | null;

  constructor(options: JournalRepositoryOptions) {
    this.#database = options.database;
    this.#createId = options.createId ?? (() => crypto.randomUUID());
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    if (options.createLifecycleRepository) {
      this.#lifecycle = options.createLifecycleRepository({
        database: this.#database,
        createId: this.#createId,
        nowEpochMs: this.#nowEpochMs,
      });
    } else {
      this.#lifecycle = new JournalLifecycleRepository({
        database: this.#database,
        createId: this.#createId,
        nowEpochMs: this.#nowEpochMs,
      });
    }
    this.#deviceSync = options.createDeviceSyncRepository
      ? options.createDeviceSyncRepository({ database: this.#database })
      : new DeviceSyncRepository({ database: this.#database });
    this.#onDeviceSyncRepairComplete = options.onDeviceSyncRepairComplete ?? null;
  }

  /** The lifecycle repository wired against the same writer. */
  get lifecycle(): JournalLifecycleRepository {
    return this.#lifecycle;
  }

  /**
   * The device-sync reconciliation repository (task 8) wired against the
   * same writer: cursor, barrier, manifest progress, remote apply and
   * echo state persist through the single serialized queue.
   */
  get deviceSync(): DeviceSyncRepositoryPort {
    return this.#deviceSync;
  }

  // --- device-sync manifest reconciliation (task 11, spec 12.4, 8.2) ----------------------

  /**
   * Complete one device repair run and clear the journal's
   * `reconcile_required` flag (spec 12.4): the composition's
   * reconcile-complete notification fires FIRST (so a persistence-composed
   * journal honors the clear through its sticky merge), the device-sync
   * completion advances both cursors to the checkpoint and clears the
   * barrier/run fields, and one following transaction clears the flag and
   * retires every echo marker at or below the newly acknowledged cursor
   * (spec 8.2 — no time-based expiry exists).
   */
  async completeDeviceSyncRepair(input: CompleteLocalRepair): Promise<void> {
    this.#onDeviceSyncRepairComplete?.();
    await this.#deviceSync.completeRepair(input);
    await this.#database.runSerializedMutation((session) => {
      const meta = session.readJournalMeta();
      if (meta.isReconcileRequired) {
        session.writeJournalMeta({ ...meta, isReconcileRequired: false });
      }
      session.exec(
        [
          "delete from echo_markers where event_sequence <=",
          "(select acknowledged_sequence from device_sync_state where singleton_key = 1);",
        ].join(" "),
      );
    });
  }

  /**
   * Discard ONLY the temporary run progress of the active manifest run
   * (spec 7.3, 9.1 — a one-hour expiry or a policy advance): the active
   * run fields and every page/action progress row clear while the repair
   * barrier, the cursors and every local edit stay untouched, so the next
   * run starts checkpoint-bound from the current barrier.
   */
  async discardActiveManifestRun(): Promise<void> {
    await this.#database.runSerializedMutation((session) => {
      const state = readDeviceSyncState({ readAll: (sql) => session.readRows(sql) });
      if (state.barrierGeneration === null) {
        // Without an active barrier there is no run to discard: fail
        // closed instead of wiping progress of a completed journal.
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update device_sync_state set active_manifest_run_id = null,",
          "manifest_checkpoint_sequence = null, manifest_final_digest = null",
          "where singleton_key = 1;",
        ].join(" "),
      );
      session.exec("delete from manifest_page_progress;");
      session.exec("delete from manifest_action_progress;");
    });
  }

  /** One recorded page of the active manifest run: ordered number, entry count, digest. */
  readManifestPageProgress(): readonly ManifestPageProgressRecord[] {
    const state = this.#deviceSync.readState();
    if (state.activeManifestRunId === null) {
      return [];
    }
    const result = this.#database.readAll(
      [
        "select page_number, entry_count, page_digest from manifest_page_progress",
        `where manifest_run_id = ${sqlText(state.activeManifestRunId)}`,
        "order by page_number asc;",
      ].join(" "),
    );
    return (result[0]?.values ?? []).map((row) => {
      const [pageNumber, entryCount, pageDigest] = row;
      if (
        typeof pageNumber !== "number" ||
        !Number.isInteger(pageNumber) ||
        pageNumber < 0 ||
        typeof entryCount !== "number" ||
        !Number.isInteger(entryCount) ||
        entryCount < 0 ||
        typeof pageDigest !== "string"
      ) {
        throw journalStoreError("journal_image_invalid");
      }
      return { pageNumber, entryCount, pageDigest };
    });
  }

  /** One recorded action progress row of the active manifest run (ordered by action index). */
  readManifestActionProgress(): readonly ManifestActionProgressRecord[] {
    const state = this.#deviceSync.readState();
    if (state.activeManifestRunId === null) {
      return [];
    }
    const result = this.#database.readAll(
      [
        "select action_index, action_kind, outcome, safe_reason_code from manifest_action_progress",
        `where manifest_run_id = ${sqlText(state.activeManifestRunId)}`,
        "order by action_index asc;",
      ].join(" "),
    );
    return (result[0]?.values ?? []).map((row) => {
      const [actionIndex, actionKind, outcome, reason] = row;
      if (
        typeof actionIndex !== "number" ||
        !Number.isInteger(actionIndex) ||
        actionIndex < 0 ||
        typeof actionKind !== "string" ||
        !(MANIFEST_ACTION_KINDS as readonly string[]).includes(actionKind) ||
        typeof outcome !== "string" ||
        !(MANIFEST_ACTION_PROGRESS_OUTCOMES as readonly string[]).includes(outcome) ||
        (reason !== null && !isDeviceSyncReason(reason))
      ) {
        throw journalStoreError("journal_image_invalid");
      }
      return {
        actionIndex,
        actionKind: actionKind as ManifestActionKind,
        outcome: outcome as ManifestActionProgressOutcome,
        reason: reason as ManifestActionProgressRecord["reason"],
      };
    });
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
   * Move one prefrozen event into `uploading`, persisting the opaque server
   * upload operation ID before the content stream may start (spec 7.2, 10.1:
   * every state transition lands before the next network action). The token
   * grammar mirrors the server's opaque operation handle: printable
   * URL-safe base64url of 32 to 128 characters.
   */
  async markEventUploading(eventId: string, operationId: string): Promise<void> {
    if (!isUuid(eventId) || !isOperationTokenShape(operationId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        eventId,
      );
      if (event.state !== "preflight" && event.state !== "uploading") {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update journal_events set state = 'uploading',",
          `operation_id = ${sqlText(operationId)},`,
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText(eventId)};`,
        ].join(" "),
      );
    });
  }

  /**
   * Close one event in a terminal non-retry state with a closed safe error
   * label. Terminal rows stay queryable forever; they are never deleted and
   * never transition again (spec 6.4, 7.2). Any multipart progress of the
   * event clears in this same mutation: the terminal outcome (with its
   * closed label) is the durable evidence, and progress of a session that
   * can never dispatch again must not linger or resurrect.
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
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText(eventId)};`,
      );
    });
  }

  // --- lifecycle orchestration helpers (child 5) ------------------------------------------------

  /**
   * Freeze every still-pending content event (`queued` / `preflight` /
   * `waiting_retry`) of one tracked file as a terminal `deferred_lifecycle`
   * row, in one transaction. Lifecycle events are never touched: a
   * `rename` / `move` / `delete` / `restore` row already owns its own
   * durable identity and must not be replaced by a content freeze.
   *
   * The lifecycle capture calls this BEFORE recording a rename / move /
   * delete so every later queue pass ignores the file's outstanding
   * content work without ever queuing more.
   */
  async freezePendingForLocalFile(localFileId: string): Promise<void> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`,
        ),
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText(state)).join(", ");
      session.exec(
        [
          "update journal_events set",
          "state = 'deferred_lifecycle',",
          "next_eligible_retry_epoch_ms = null,",
          "safe_error = 'deferred_lifecycle',",
          "is_fingerprint_frozen = 1",
          `where local_file_id = ${sqlText(localFileId)}`,
          `and state in (${pendingStateList})`,
          "and operation in ('create', 'update');",
        ].join(" "),
      );
      // Frozen content events never dispatch again, so their multipart
      // progress clears in the same mutation (child 7 spec 4.1).
      session.exec(
        [
          "delete from multipart_upload_progress where event_id in (",
          "select event_id from journal_events",
          `where local_file_id = ${sqlText(localFileId)}`,
          "and state = 'deferred_lifecycle'",
          "and operation in ('create', 'update'));",
        ].join(" "),
      );
    });
  }

  /**
   * Remove one tracked `local_files` row together with every dependent
   * event / operand row in one transaction. The lifecycle capture calls
   * this AFTER a tombstone event has been recorded, so the durable
   * operand row keeps the tombstone reference for restore even though
   * the local mapping row itself is gone.
   */
  async removeLocalMapping(localFileId: string): Promise<void> {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`,
        ),
      );
      if (existing === null) {
        return;
      }
      session.exec(
        `delete from journal_attempts where event_id in (select event_id from journal_events where local_file_id = ${sqlText(localFileId)});`,
      );
      session.exec(
        `delete from multipart_upload_progress where event_id in (select event_id from journal_events where local_file_id = ${sqlText(localFileId)});`,
      );
      session.exec(
        `delete from lifecycle_event_operands where event_id in (select event_id from journal_events where local_file_id = ${sqlText(localFileId)});`,
      );
      session.exec(
        `delete from journal_events where local_file_id = ${sqlText(localFileId)};`,
      );
      session.exec(
        `delete from local_files where local_file_id = ${sqlText(localFileId)};`,
      );
    });
  }

  // --- receipts and attempts (spec 6.3, 7.2) ----------------------------------------------------

  /**
   * Persist the canonical receipt of one committed event: the event closes
   * as `committed` and its file takes the server-returned source and base
   * version identities. The observed fingerprint is left untouched — a
   * successor capture may already have observed newer bytes — but the
   * provable `last_committed_*` triple is updated from the event's frozen
   * fingerprint so the lifecycle capture can verify a later restore
   * eligibility against bytes the server actually acknowledged.
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
      // The frozen committed receipt is the durable evidence; the
      // multipart progress of the finished session clears in this same
      // mutation (child 7 spec 4.1).
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText(input.eventId)};`,
      );
      session.exec(
        [
          "update local_files set",
          `source_id = ${sqlText(input.sourceId)},`,
          `base_version_id = ${sqlText(input.baseVersionId)},`,
          `last_committed_sha256 = ${sqlText(event.fingerprint.sha256)},`,
          `last_committed_size_bytes = ${event.fingerprint.sizeBytes},`,
          `last_committed_media_type = ${sqlText(event.fingerprint.mediaType)}`,
          `where local_file_id = ${sqlText(event.localFileId)};`,
        ].join(" "),
      );
    });
  }

  /**
   * Persist the safe no-op receipt of one `no_change` preflight (spec 7.2,
   * 10.1): the event closes as `no_change` and its file adopts the confirmed
   * current server base — no bytes were uploaded and nothing retries. The
   * `last_committed_*` triple is updated from the event's frozen fingerprint
   * so the lifecycle capture can verify restore eligibility against the
   * server's acknowledgement. Any multipart progress clears in the same
   * mutation: a no-change outcome leaves no session work owed.
   */
  async recordNoChangeReceipt(input: JournalCommittedReceiptInput): Promise<void> {
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
          "update journal_events set state = 'no_change',",
          "next_eligible_retry_epoch_ms = null, safe_error = null,",
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText(input.eventId)};`,
        ].join(" "),
      );
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText(input.eventId)};`,
      );
      session.exec(
        [
          "update local_files set",
          `source_id = ${sqlText(input.sourceId)},`,
          `base_version_id = ${sqlText(input.baseVersionId)},`,
          `last_committed_sha256 = ${sqlText(event.fingerprint.sha256)},`,
          `last_committed_size_bytes = ${event.fingerprint.sizeBytes},`,
          `last_committed_media_type = ${sqlText(event.fingerprint.mediaType)}`,
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

  // --- multipart safe progress (child 7 spec 4.1, task 9) ----------------------------------------

  /**
   * Persist the SAFE progress of one frozen event's multipart transfer
   * (child 7 spec 4.1): the opaque public session ID, the fixed geometry
   * and expiry of the server plan, the completed part-number set, the last
   * observed closed session state and the last closed retry/status token.
   * The whole record is validated against the closed contract BEFORE any
   * SQL runs — unknown fields, hostile session IDs, out-of-geometry part
   * numbers and foreign tokens never reach SQLite, so no URL, provider
   * identity, staging key or digest can persist. The row lands in the
   * same serialized mutation the event's dispatch state lives in, bound
   * to a known nonterminal event; a terminal event never resurrects
   * progress (its frozen outcome is the durable evidence instead).
   */
  async saveMultipartProgress(record: MultipartProgressRecord): Promise<void> {
    validateMultipartProgressRecord(record);
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        record.eventId,
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "insert or replace into multipart_upload_progress (event_id, session_id,",
          "part_size_bytes, part_count, expires_at_epoch_ms, completed_part_numbers_json,",
          "session_state, safe_reason) values (",
          `${sqlText(record.eventId)}, ${sqlText(record.sessionId)},`,
          `${record.partSizeBytes}, ${record.partCount}, ${record.expiresAtEpochMs},`,
          `${sqlText(JSON.stringify(record.completedPartNumbers))},`,
          `${sqlText(record.sessionState)},`,
          `${record.safeReason === null ? "null" : sqlText(record.safeReason)});`,
        ].join(" "),
      );
    });
  }

  /**
   * Read the durable safe progress bound to one journal event, or null
   * when the event carries no session. A persisted row that violates the
   * closed contract fails closed as image corruption.
   */
  readMultipartProgress(eventId: string): MultipartProgressRecord | null {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          "select session_id, part_size_bytes, part_count, expires_at_epoch_ms,",
          "completed_part_numbers_json, session_state, safe_reason",
          `from multipart_upload_progress where event_id = ${sqlText(eventId)};`,
        ].join(" "),
      ),
    );
    return row === null ? null : parseMultipartProgressRow(row, eventId);
  }

  /**
   * Clear the durable safe progress of one event (child 7 spec 4.1): the
   * idempotent exact-key cleanup the runner issues when a session is
   * superseded or its evidence is no longer owed. The journal event
   * itself is never touched — cancellation and interruption retain it.
   */
  async clearMultipartProgress(eventId: string): Promise<void> {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText(eventId)};`,
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

  /**
   * The oldest CONTENT event one queue pass may select (spec 8): the
   * earliest `queued`/`waiting_retry` row whose retry time has passed,
   * where an event left in `preflight`/`uploading` by an interrupted pass
   * stays eligible for the exact same-identity replay of spec 10.3.
   *
   * Lane discipline: rows carrying lifecycle operands (`rename`, `move`,
   * `delete`, `restore`) are NEVER selected here — their placeholder
   * zeros fingerprint belongs to the lifecycle dispatch lane, and a
   * content-lane dispatch of such a row would terminally destroy the
   * lifecycle intent through the content re-fingerprint check. The
   * lifecycle lane selects through its own
   * {@link JournalLifecycleRepository.readOldestEligibleLifecycleEvent}
   * selector; this exclusion is enforced structurally with a `NOT
   * EXISTS` probe on `lifecycle_event_operands` (no schema change).
   */
  readOldestEligibleEvent(nowEpochMs: number): JournalEvent | null {
    if (!isNonNegativeInteger(nowEpochMs)) {
      throw journalStoreError("journal_query_failed");
    }
    const coalescableStateList = JOURNAL_COALESCABLE_EVENT_STATES.map((state) => sqlText(state)).join(", ");
    const row = firstRow(
      this.#database.readAll(
        [
          `select ${JOURNAL_EVENT_COLUMNS.join(", ")} from journal_events`,
          `where ((state in (${coalescableStateList})`,
          "and (next_eligible_retry_epoch_ms is null",
          `or next_eligible_retry_epoch_ms <= ${nowEpochMs}))`,
          "or state in ('preflight', 'uploading'))",
          "and not exists (",
          "select 1 from lifecycle_event_operands content_lane_exclusion",
          "where content_lane_exclusion.event_id = journal_events.event_id)",
          "order by created_at_epoch_ms asc, rowid asc limit 1;",
        ].join(" "),
      ),
    );
    return row === null ? null : toPublicEvent(parseJournalEventRow(row));
  }

  /** One tracked file by its plugin-local identity, or null. */
  readLocalFileByLocalFileId(localFileId: string): LocalFile | null {
    if (typeof localFileId !== "string" || localFileId.length === 0) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        selectColumns(
          "local_files",
          LOCAL_FILE_COLUMNS,
          `where local_file_id = ${sqlText(localFileId)}`,
        ),
      ),
    );
    return row === null ? null : parseLocalFileRow(row);
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

  /**
   * The earliest scheduled retry deadline among pending events, or null
   * when no pending event waits on a retry time (a `queued` row is
   * immediately eligible and carries no deadline). The plugin's one-shot
   * scheduled retry trigger uses this deadline — plus a small safety
   * margin — to time the single follow-up pass it arms after a pass ends
   * `retry_scheduled` or `login_required`.
   */
  readEarliestPendingRetryEpochMs(): number | null {
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText(state)).join(", ");
    const row = firstRow(
      this.#database.readAll(
        [
          "select min(next_eligible_retry_epoch_ms) from journal_events",
          `where state in (${pendingStateList})`,
          "and next_eligible_retry_epoch_ms is not null;",
        ].join(" "),
      ),
    );
    const earliest = row?.[0];
    if (earliest === null || earliest === undefined) {
      return null;
    }
    if (typeof earliest !== "number" || !Number.isInteger(earliest) || earliest < 0) {
      throw journalStoreError("journal_image_invalid");
    }
    return earliest;
  }

  /**
   * The redacted event histogram of the status projection (spec 11): the
   * newest row for each local file, grouped by closed state and closed safe
   * error label. A later capture supersedes every predecessor outcome for
   * current status purposes while immutable audit evidence remains in the
   * journal — never a path, digest, credential or other row detail.
   */
  readEventStateErrorCounts(): readonly JournalEventStateErrorCount[] {
    const result = this.#database.readAll(
      [
        "select current_event.state, current_event.safe_error, count(*) from journal_events current_event",
        "where not exists (",
        "select 1 from journal_events successor_event",
        "where successor_event.local_file_id = current_event.local_file_id",
        "and successor_event.rowid > current_event.rowid",
        ")",
        "group by current_event.state, current_event.safe_error;",
      ].join(" "),
    );
    return (result[0]?.values ?? []).map((row) => {
      const [state, safeError, eventCount] = row;
      if (
        typeof state !== "string" ||
        !isClosedToken(state, JOURNAL_EVENT_STATES) ||
        !isNullableText(safeError) ||
        (safeError !== null && !isClosedToken(safeError, JOURNAL_SAFE_ERROR_LABELS)) ||
        typeof eventCount !== "number" ||
        !Number.isInteger(eventCount) ||
        eventCount < 0
      ) {
        throw journalStoreError("journal_image_invalid");
      }
      return {
        state: state as JournalEventState,
        safeError: safeError as JournalSafeErrorLabel | null,
        eventCount,
      };
    });
  }

  /**
   * The current local-only status of every tracked note, in deterministic
   * normalized-path order. The newest journal event is selected per local
   * file; immutable predecessor events remain available through audit reads
   * but cannot become a present UI blocker or reach aggregate status.
   */
  readLocalNoteSyncStatuses(): readonly LocalNoteSyncStatus[] {
    const reconcileRow = firstRow(
      this.#database.readAll(
        "select is_reconcile_required from journal_meta where singleton_key = 1;",
      ),
    );
    const isReconcileRequired = reconcileRow?.[0];
    if (isReconcileRequired !== 0 && isReconcileRequired !== 1) {
      throw journalStoreError("journal_image_invalid");
    }
    const result = this.#database.readAll(
      [
        "select local_file.normalized_path, local_file.policy_revision,",
        "local_file.observed_sha256, local_file.observed_size_bytes, local_file.observed_media_type,",
        "local_file.last_committed_sha256, local_file.last_committed_size_bytes, local_file.last_committed_media_type,",
        `current_event.${JOURNAL_EVENT_COLUMNS.join(", current_event.")}`,
        "from local_files local_file",
        "left join journal_events current_event on current_event.rowid = (",
        "select candidate_event.rowid from journal_events candidate_event",
        "where candidate_event.local_file_id = local_file.local_file_id",
        "order by candidate_event.rowid desc limit 1",
        ")",
        "order by local_file.normalized_path asc;",
      ].join(" "),
    );
    return (result[0]?.values ?? []).map((row) =>
      parseLocalNoteSyncStatusRow(row, isReconcileRequired === 1),
    );
  }

  /**
   * The redacted lifecycle-state histogram of the status projection
   * (Task 10, spec 6.3): the closed {@link LifecycleLocalFileState} of
   * each tracked `local_files` row, counted per state. The closed enum
   * is the only thing that reaches the status surface — no path,
   * source id, locator, tombstone id, fingerprint or any other row
   * detail ever escapes the read.
   */
  readLifecycleStateCounts(): Readonly<Record<LifecycleLocalFileState, number>> {
    const counts: Record<LifecycleLocalFileState, number> = {
      active: 0,
      rename_pending: 0,
      move_pending: 0,
      delete_pending: 0,
      restore_pending: 0,
      tombstoned: 0,
      restored: 0,
      reconcile_required: 0,
    };
    const result = this.#database.readAll(
      "select lifecycle_state, count(*) from local_files group by lifecycle_state;",
    );
    for (const row of result[0]?.values ?? []) {
      const [state, count] = row;
      if (typeof state !== "string" || !isClosedToken(state, LIFECYCLE_LOCAL_FILE_STATES)) {
        throw journalStoreError("journal_image_invalid");
      }
      if (
        typeof count !== "number" ||
        !Number.isInteger(count) ||
        count < 0
      ) {
        throw journalStoreError("journal_image_invalid");
      }
      counts[state as LifecycleLocalFileState] = count;
    }
    return counts;
  }

  /**
   * The number of lifecycle events that still owe work (Task 10): the
   * oldest eligible lifecycle event count surfaced as a status
   * affordance. The number is derived from the same closed pending-event
   * vocabulary the content queue uses, restricted to the four lifecycle
   * operations so the count never leaks a content event.
   */
  countPendingLifecycleEvents(): number {
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText(state)).join(", ");
    const row = firstRow(
      this.#database.readAll(
        [
          `select count(*) from journal_events`,
          `where state in (${pendingStateList})`,
          `and operation in ('rename', 'move', 'delete', 'restore');`,
        ].join(" "),
      ),
    );
    const count = row?.[0];
    return typeof count === "number" ? count : 0;
  }

  /**
   * The number of failed attempts in the bounded `journal_attempts`
   * ring (Task 10, spec 6.3): every row whose closed `outcome_label`
   * is anything other than the success token (`committed`) counts as
   * one failed attempt. The number never leaks a path, digest, source
   * id or credential; only closed labels and correlation IDs reach
   * the audit ring.
   */
  countFailedAttempts(): number {
    const row = firstRow(
      this.#database.readAll(
        "select count(*) from journal_attempts where outcome_label != 'committed';",
      ),
    );
    const count = row?.[0];
    return typeof count === "number" ? count : 0;
  }

  /**
   * The redacted lifecycle blocked-reason-code list of the status
   * projection (Task 10, spec 6.3): the closed set of reasons any
   * lifecycle event currently owns that block its forward progress.
   * The mapping is derived from the existing telemetry (event states
   * + bounded attempt outcome labels); no path, digest, locator,
   * source id, tombstone id or credential ever escapes the read.
   */
  readLifecycleBlockedReasonCodes(): readonly string[] {
    const codes = new Set<string>();
    const blockedEvents = this.#database.readAll(
      [
        "select safe_error from journal_events",
        "where operation in ('rename', 'move', 'delete', 'restore')",
        "and safe_error = 'integrity_failed';",
      ].join(" "),
    );
    for (const row of blockedEvents[0]?.values ?? []) {
      const [safeError] = row;
      if (typeof safeError === "string") {
        codes.add(safeError);
      }
    }
    return Array.from(codes);
  }

  /**
   * The retained restorable rows the explicit restore surface addresses
   * (Task 10, spec 6.3 + 7.1; the explicit-restore target reservation
   * spec extends the set). The read returns every tracked file row that
   * still holds an open tombstone — `tombstoned`, or `restore_pending`
   * (a durable reservation or an in-flight restore event the operator
   * can resume) — identified by the plugin-local `localFileId`. No path,
   * source id, tombstone id, locator or fingerprint reaches the caller;
   * the picker constructs its display label from the safe identifier
   * alone.
   */
  readRestorableLocalFileIds(): readonly string[] {
    const rows = this.#database.readAll(
      [
        "select local_file_id from local_files",
        "where lifecycle_state in ('tombstoned', 'restore_pending')",
        "and open_tombstone_id is not null",
        "order by normalized_path asc;",
      ].join(" "),
    );
    return (rows[0]?.values ?? [])
      .map((row) => row[0])
      .filter((value): value is string => typeof value === "string" && value.length > 0);
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
    // Coalesce discipline (Child 4 + Child 5): only the content surface
    // (`create`, `update`) is coalescable; a queued lifecycle event for the
    // same file is never replaced by a later content capture and vice-versa.
    const coalescableOperationList = ["create", "update"]
      .map((operation) => sqlText(operation))
      .join(", ");
    // A file with any recorded lifecycle event must not have its content
    // events coalesced away — the lifecycle surface freezes the file's
    // locator evidence and a coalesced content capture would silently drop
    // the queued lifecycle intent. The probe reads only one indexed row.
    const lifecycleProbe = firstRow(
      session.readRows(
        `select 1 from journal_events where local_file_id = ${sqlText(localFileId)} and operation in ('rename', 'move', 'delete', 'restore') limit 1;`,
      ),
    );
    if (lifecycleProbe !== null) {
      return null;
    }
    const row = firstRow(
      session.readRows(
        selectColumns(
          "journal_events",
          JOURNAL_EVENT_COLUMNS,
          [
            `where local_file_id = ${sqlText(localFileId)}`,
            `and state in (${coalescableStateList})`,
            `and operation in (${coalescableOperationList})`,
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
