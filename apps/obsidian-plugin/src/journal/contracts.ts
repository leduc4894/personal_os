/**
 * Closed contracts of the plugin-side portable sync journal (spec 6, 7, 12).
 *
 * This module freezes only the immutable vocabulary, record shapes and hard
 * limits of the journal: the closed event states, the safe error labels, the
 * queue outcomes, the recovery states, the logical records of spec 6.3 and
 * the constants of spec 3.1/6.4/7.1. It deliberately contains no runtime
 * behavior — no SQLite instantiation and no I/O. Later journal modules
 * (`sqlite-database.ts`, `persistence.ts`, `repository.ts`, `capture.ts`,
 * `queue-driver.ts`) build on this frozen vocabulary.
 *
 * Privacy (spec 2, 9): journal records keep paths and digests for local
 * recovery only, and the closed labels below are the only strings that may
 * reach diagnostics. No raw file bytes, access/refresh credential, raw
 * policy rule, provider identifier or secret ever enters a record.
 */

// --- frozen limits (spec 3.1, 6.4, 7.1) -----------------------------------------------

/** Hard ceiling on one regular uploaded file: 16 MiB (spec 3.1). */
export const MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024;

/** Soft ceiling on pending journal events: 10,000 rows (spec 6.4). */
export const MAX_PENDING_EVENTS = 10_000;

/** Soft ceiling on total journal SQLite data: 64 MiB (spec 6.4). */
export const MAX_JOURNAL_SIZE_BYTES = 64 * 1024 * 1024;

/** Per-file watcher settle delay before fingerprinting: 250 ms (spec 7.1). */
export const FILE_SETTLE_DELAY_MS = 250;

// --- frozen multipart geometry (child 7 spec 4) -----------------------------------------------

/**
 * Ordinary multipart part size: exactly 8 MiB (child 7 spec 4). Mirrors the
 * server constant `MULTIPART_PART_SIZE_BYTES` — every part except the final
 * one carries exactly this many bytes; the value is never negotiated. The
 * plugin never derives routing from it; it only validates the geometry the
 * server-owned plan already returned.
 */
export const MULTIPART_PART_SIZE_BYTES = 8 * 1024 * 1024;

/**
 * Maximum number of parts one multipart session's geometry may declare
 * (child 7 spec 4): the 100 MiB product maximum over the 8 MiB ordinary
 * part. Mirrors the server constant `MAX_MULTIPART_PART_COUNT`.
 */
export const MAX_MULTIPART_PART_COUNT = 13;

/**
 * The closed, redacted outcome of a bounded Vault snapshot capture. Explicit
 * scans may be cancelled before enumeration; automatic snapshots stop when
 * capture has been disposed. Only newly recorded or coalesced queued content
 * rows count as dispatchable work.
 */
export interface ExistingFilesScanSummary {
  readonly outcome: "completed" | "cancelled" | "stopped";
  readonly processedFileCount: number;
  readonly skippedFileCount: number;
  readonly queuedEventCount: number;
  readonly isTruncated: boolean;
}

/**
 * Most recent attempts retained per event in the bounded `journal_attempts`
 * ring (spec 6.3): older rows are pruned inside the same transaction, so the
 * audit trail stays closed and bounded.
 */
export const MAX_EVENT_ATTEMPT_HISTORY = 10;

// --- closed event states (spec 7.2) ----------------------------------------------------

/**
 * The closed journal event states of spec 7.2:
 * `queued -> preflight -> uploading -> committed | no_change`, with
 * `waiting_retry` re-entering `queued`, plus the terminal non-retry states.
 */
export const JOURNAL_EVENT_STATES = [
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
] as const;

export type JournalEventState = (typeof JOURNAL_EVENT_STATES)[number];

/**
 * The states that count toward the 10,000 pending-event soft limit (spec 6.4):
 * every state that still owes work, as opposed to the closed terminal states.
 */
export const JOURNAL_PENDING_EVENT_STATES = [
  "queued",
  "preflight",
  "uploading",
  "waiting_retry",
] as const satisfies readonly JournalEventState[];

export type JournalPendingEventState = (typeof JOURNAL_PENDING_EVENT_STATES)[number];

/**
 * The states in which an unsent same-file event may still be replaced by a
 * later current fingerprint (spec 7.2): `queued` or `waiting_retry` with a
 * preflight that has not started yet.
 */
export const JOURNAL_COALESCABLE_EVENT_STATES = [
  "queued",
  "waiting_retry",
] as const satisfies readonly JournalEventState[];

export type JournalCoalescableEventState = (typeof JOURNAL_COALESCABLE_EVENT_STATES)[number];

/** The five terminal states that never receive automatic retry (spec 7.2). */
export const JOURNAL_NON_RETRY_EVENT_STATES = [
  "excluded_policy",
  "blocked_size",
  "blocked_conflict",
  "deferred_lifecycle",
  "integrity_failed",
] as const satisfies readonly JournalEventState[];

export type JournalNonRetryEventState = (typeof JOURNAL_NON_RETRY_EVENT_STATES)[number];

// --- safe error labels (spec 12) ---------------------------------------------------------

/**
 * The closed safe error vocabulary mirroring the spec-12 error and retry
 * matrix. Diagnostics may carry these labels and nothing else: no library
 * exception, provider text, path, full digest or credential detail.
 *
 * Note: this list carries the error tokens AND the single success token
 * `committed`. The `JournalAttempt` audit row uses the success token
 * for a successful commit so the audit trail no longer labels a
 * successful send as `server_error` (task 9 fix round 1 M1).
 *
 * `multipart_local_content_changed` (child 7 spec 7) is the one multipart
 * token of {@link MULTIPART_SAFE_REASON_TOKENS} that terminates a journal
 * event: the frozen Vault file changed under an open multipart session, so
 * the old event closes without ever mixing bytes of two file generations
 * while the newer watcher event uploads separately.
 */
export const JOURNAL_SAFE_ERROR_LABELS = [
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
  "multipart_local_content_changed",
  "reconcile_required",
  "committed",
] as const;

export type JournalSafeErrorLabel = (typeof JOURNAL_SAFE_ERROR_LABELS)[number];

// --- queue outcomes (spec 8, 12) -----------------------------------------------------------

/**
 * The closed outcome set of processing one event inside a bounded foreground
 * queue pass: the success and replay receipts, the resumable outcomes
 * (retry, suspension, login) and the terminal blockers (spec 8, 12).
 */
export const QUEUE_OUTCOMES = [
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
] as const;

export type QueueOutcome = (typeof QUEUE_OUTCOMES)[number];

// --- closed multipart session vocabulary (child 7 spec 4.2, 7) ----------------------------------

/**
 * The closed server session states of the multipart lifecycle (child 7
 * spec 4.2) the safe progress record may persist: the last state the
 * session's status surface reported. `committed` and `cleaned` are the
 * terminal outcomes; `cleanup_pending` is a cleanup obligation, never
 * permission to reuse the session.
 */
export const MULTIPART_SESSION_STATES = [
  "created",
  "uploading",
  "completing",
  "verifying",
  "promoting",
  "committed",
  "cancelling",
  "expired",
  "integrity_failed",
  "policy_denied",
  "cleanup_pending",
  "cleaned",
] as const;

export type MultipartSessionState = (typeof MULTIPART_SESSION_STATES)[number];

/**
 * The closed multipart retry/status reason tokens (child 7 spec 7) the
 * durable progress record may carry as its last observed safe reason —
 * the same vocabulary the server registers once in its central error
 * registry. No provider text, URL, digest or other detail ever travels
 * with these tokens.
 */
export const MULTIPART_SAFE_REASON_TOKENS = [
  "multipart_session_not_found",
  "multipart_session_expired",
  "multipart_session_state_invalid",
  "multipart_part_invalid",
  "multipart_part_url_rejected",
  "multipart_provider_state_invalid",
  "multipart_completion_in_progress",
  "multipart_integrity_failed",
  "multipart_policy_denied",
  "multipart_cleanup_failed",
  "multipart_local_content_changed",
  "multipart_dependency_unavailable",
] as const;

export type MultipartSafeReasonToken = (typeof MULTIPART_SAFE_REASON_TOKENS)[number];

// --- recovery states (spec 6.2) --------------------------------------------------------------

/**
 * The closed journal recovery states of spec 6.2. `empty_journal_rebuilt`
 * always accompanies `isReconcileRequired: true` because no valid
 * generation survived.
 */
export const JOURNAL_RECOVERY_STATES = [
  "fresh_journal_created",
  "verified_generation_loaded",
  "prior_generation_recovered",
  "empty_journal_rebuilt",
] as const;

export type JournalRecoveryState = (typeof JOURNAL_RECOVERY_STATES)[number];

// --- logical records (spec 6.3) ----------------------------------------------------------------

/**
 * The closed capture admission outcomes of spec 7.1: a successful current
 * policy decision for a regular allowed file, or the two fail-closed blocks
 * (`blocked_size` above 16 MiB, `excluded_policy` for excluded/indeterminate
 * policy) that record a born-terminal event and never retry.
 */
export const JOURNAL_CAPTURE_ADMISSIONS = [
  "policy_allowed",
  "blocked_size",
  "excluded_policy",
] as const;

export type JournalCaptureAdmission = (typeof JOURNAL_CAPTURE_ADMISSIONS)[number];

/** The supported journal operations of this and the lifecycle child (child 5). */
export const JOURNAL_OPERATIONS = [
  "create",
  "update",
  "rename",
  "move",
  "delete",
  "restore",
] as const;

/** The supported journal operations of this and the lifecycle child (child 5). */
export type JournalOperation = (typeof JOURNAL_OPERATIONS)[number];

/**
 * The content identity of one event: exact lowercase SHA-256, exact byte
 * size and media type. The identity is immutable from the moment preflight
 * starts; a later save creates a successor event (spec 7.2).
 */
export interface FrozenFingerprint {
  readonly sha256: string;
  readonly sizeBytes: number;
  readonly mediaType: string;
}

/**
 * One tracked Vault file (spec 6.3): a random plugin-local identity (never
 * a canonical source locator), the normalized current path, the nullable
 * server `source_id` (null until a committed create receipt), the observed
 * fingerprint, the last committed base version and the policy revision the
 * observation was evaluated against.
 *
 * `lastCommittedFingerprint` is the provable bytes hash the server last
 * acknowledged for this source (spec 6.3, lifecycle child 5). It is updated
 * only by `recordCommittedReceipt` / `recordNoChangeReceipt` and is `null`
 * until the first commit receipt lands. The lifecycle capture reads this
 * column to verify restore eligibility; `observedFingerprint` is mutable
 * across pending captures and must never be used for that check.
 */
export interface LocalFile {
  readonly localFileId: string;
  readonly normalizedPath: string;
  readonly sourceId: string | null;
  readonly observedFingerprint: FrozenFingerprint;
  readonly baseVersionId: string | null;
  readonly policyRevisionNumber: number;
  readonly lastCommittedFingerprint: FrozenFingerprint | null;
}

/**
 * One durable create/update intent (spec 6.3): stable event and
 * idempotency UUIDs, the operation, the fingerprint frozen at preflight,
 * the closed state, the bounded retry schedule, the safe error label and
 * the nullable server upload operation ID.
 */
export interface JournalEvent {
  readonly eventId: string;
  readonly localFileId: string;
  readonly idempotencyKey: string;
  readonly operation: JournalOperation;
  readonly fingerprint: FrozenFingerprint;
  readonly state: JournalEventState;
  readonly attemptCount: number;
  readonly nextEligibleRetryEpochMs: number | null;
  readonly safeError: JournalSafeErrorLabel | null;
  readonly operationId: string | null;
}

/**
 * One bounded attempt-audit row of `journal_attempts` (spec 6.3): a
 * timestamp, one closed safe error label and an opaque request correlation
 * ID — and nothing else. Paths, digests and provider detail never enter this
 * record, so the attempted-event history is redacted by construction.
 */
export interface JournalAttempt {
  readonly eventId: string;
  readonly attemptedAtEpochMs: number;
  readonly outcomeLabel: JournalSafeErrorLabel;
  readonly requestCorrelationId: string;
}

/**
 * The journal-level metadata of spec 6.3: logical schema version, the dirty
 * (written, not yet verified) generation, the last verified persistence
 * generation, the reconcile flag and the safe recovery state.
 */
export interface JournalMeta {
  readonly schemaVersion: number;
  readonly dirtyGeneration: number;
  readonly lastVerifiedGeneration: number;
  readonly isReconcileRequired: boolean;
  readonly recoveryState: JournalRecoveryState;
}

/**
 * The durable SAFE progress of one frozen outbound journal event's
 * multipart transfer (child 7 spec 4.1): the opaque public session ID the
 * server issued, the fixed part geometry and expiry of its plan, the last
 * observed closed session state, the completed part-number set and the
 * last closed retry/status token — and nothing else. No presigned URL or
 * query signature, provider upload ID or ETag, staging key, digest, path
 * or locator is ever a member, so none can persist.
 */
export interface MultipartProgressRecord {
  /** The existing journal event this session is bound to. */
  readonly eventId: string;
  /** Opaque public session ID (printable base64url, never a raw UUID). */
  readonly sessionId: string;
  /** Ordinary part size: exactly {@link MULTIPART_PART_SIZE_BYTES}. */
  readonly partSizeBytes: number;
  /** Declared part count: 1 to {@link MAX_MULTIPART_PART_COUNT}. */
  readonly partCount: number;
  /** The session expiry the server plan returned. */
  readonly expiresAtEpochMs: number;
  /** Completed part numbers, strictly ascending, each 1 to `partCount`. */
  readonly completedPartNumbers: readonly number[];
  /** The last session state the status surface reported. */
  readonly sessionState: MultipartSessionState;
  /** The last closed retry/status token, or null before any closed path. */
  readonly safeReason: MultipartSafeReasonToken | null;
}
