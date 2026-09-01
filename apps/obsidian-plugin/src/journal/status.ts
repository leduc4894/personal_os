/**
 * The closed sync status projection of the minimal plugin UX (spec 11).
 *
 * The projection folds a redacted journal histogram plus two live facts
 * (credential existence, active pass) into exactly one of the six closed
 * status values of spec 11 with counts, and collects the closed blocker
 * conditions whose guidance the spec table fixes verbatim in content. It is
 * a pure function: no I/O, no clock, no transport, and no journal access.
 *
 * Redaction is by construction (spec 9, 11): the input carries closed event
 * states, closed safe error labels and counts only, so no path, digest,
 * credential, provider identifier or other Vault detail can reach a status
 * value, a blocker line or any telemetry-shaped serialization of this
 * module's output. The status surface is a display, never an automatic
 * upload control.
 *
 * The Task 10 lifecycle extension adds the closed redacted source-lifecycle
 * surface to the same projection: lifecycle-state histogram counts, the
 * closed set of lifecycle blocker codes, the count of pending lifecycle
 * events and the count of failed attempts. None of these ever include
 * paths, locators, source IDs, tokens, fingerprints, remote URLs or any
 * other Vault detail — the input carries only closed enum tokens and
 * counts, and the snapshot only ever reads those.
 *
 * The resumable multipart mobile-upload child (task 11) adds the closed
 * redacted multipart surface: a session-state histogram over the closed
 * `MultipartSessionState` vocabulary and the closed set of observed
 * multipart safe-reason tokens. The projection exposes the closed tokens
 * and counts only — never a session ID, staging key, provider upload ID,
 * ETag, presigned URL, digest, byte count or Vault path.
 *
 * The Conflict Inbox child (task 9) adds the closed redacted parked-apply
 * surface: the count of journal schema v9 `conflict_local_repairs` rows
 * that still owe their Vault apply plus the closed set of their
 * safe-reason tokens. The composed {@link renderJournalSyncStatus} line
 * appends the fixed `Conflict apply pending` fragment with the count —
 * never a locator, conflict id, resolution id or timestamp.
 */

import type {
  JournalEventState,
  MultipartSafeReasonToken,
  MultipartSessionState,
} from "./contracts";
import { JOURNAL_PENDING_EVENT_STATES, MULTIPART_SAFE_REASON_TOKENS, MULTIPART_SESSION_STATES } from "./contracts";
import type { LifecycleLocalFileState } from "./lifecycle-contracts";
import { LIFECYCLE_LOCAL_FILE_STATES } from "./lifecycle-contracts";
import type { QueuePassOutcome } from "./queue-driver";
import type { JournalEventStateErrorCount } from "./repository";
import type { ConflictLocalRepairSafeReason } from "../conflicts/contracts";
import { CONFLICT_LOCAL_REPAIR_SAFE_REASONS } from "../conflicts/contracts";

// --- the closed vocabulary of spec 11 ------------------------------------------------------------

/** The six closed sync status values of spec 11, no others. */
export const SYNC_STATUS_KINDS = [
  "ready",
  "syncing",
  "offline_queued",
  "login_required",
  "policy_blocked",
  "reconcile_required",
] as const;

export type JournalSyncStatusKind = (typeof SYNC_STATUS_KINDS)[number];

/** The exact display text of each closed status value (spec 11). */
export const SYNC_STATUS_TEXT: Readonly<Record<JournalSyncStatusKind, string>> = {
  ready: "Ready",
  syncing: "Syncing",
  offline_queued: "Offline — queued",
  login_required: "Login required",
  policy_blocked: "Policy blocked",
  reconcile_required: "Reconcile required",
};

/** The closed blocker conditions whose guidance spec 11 fixes. */
export const SYNC_BLOCKERS = [
  "blocked_size",
  "excluded_policy",
  "blocked_conflict",
  "deferred_lifecycle",
  "login_required",
  "reconcile_required",
] as const;

export type JournalSyncBlocker = (typeof SYNC_BLOCKERS)[number];

/**
 * The closed set of source-lifecycle blocked reason codes the status
 * surface exposes (Task 10, spec 6.3 + spec 11). Each code names one
 * blocking condition a lifecycle event owns — the projection never
 * reveals the underlying row, locator, source id, tombstone id,
 * fingerprint or any other raw value. The closed enum is the
 * authoritative vocabulary; no other string ever appears on this
 * surface.
 */
export const LIFECYCLE_BLOCKED_REASON_CODES = [
  "idempotency_conflict",
  "version_conflict",
  "locator_conflict",
  "tombstone_not_found",
  "tombstone_closed",
  "commit_outcome_unknown",
  "integrity_failed",
] as const;

export type LifecycleBlockedReasonCode = (typeof LIFECYCLE_BLOCKED_REASON_CODES)[number];

/**
 * The required blocker guidance of the spec-11 table. Each line explains
 * the boundary that owns resolution — the 16 MiB/multipart split, the
 * authorized-only policy refresh, the no-overwrite conflict/lifecycle
 * deferral, the queue-preserving browser login, and the child-6 repair of a
 * reconcile-required journal.
 */
export const SYNC_BLOCKER_GUIDANCE_TEXT: Readonly<Record<JournalSyncBlocker, string>> = {
  blocked_size:
    "This file is larger than the 16 MiB small-file limit and was not uploaded. Larger files arrive later through multipart upload.",
  excluded_policy:
    "This content is blocked by the sync policy. The policy is refreshed only through the authorized login flow, never from the sync status.",
  blocked_conflict:
    "No overwrite occurred: this change conflicts with the server version and resolution is owned by the later conflict flow.",
  deferred_lifecycle:
    "No overwrite occurred: rename/move/delete changes are owned by the later lifecycle flow.",
  login_required:
    "Login required: open the existing browser login from the plugin settings. Queued work is kept unchanged.",
  reconcile_required:
    "Sync stopped: journal reconciliation is required before syncing can continue. Run the plugin command \"Repair sync\" to reconcile this device; queued work resumes after the repair completes.",
};

/**
 * The safe error labels that mean "queued work is waiting" (spec 12): an
 * event in `waiting_retry` under one of these labels projects to
 * `Offline — queued`, not to a healthy ready state. Besides the four
 * network labels this includes `login_required` — the credential need
 * parks the event for a later pass, so while the work waits (even with a
 * credential present again) the surface keeps saying work is waiting
 * instead of rendering `Ready` with a count while nothing syncs.
 */
const WAITING_RETRY_SAFE_ERRORS: ReadonlySet<string> = new Set([
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "login_required",
]);

// --- the closed input and output ------------------------------------------------------------------

/**
 * The closed, redacted lifecycle-state histogram of one snapshot (Task 10):
 * one closed {@link LifecycleLocalFileState} key per state, the count of
 * tracked `local_files` rows that hold that state. Missing states report
 * zero so callers never have to defend against `undefined` keys. The map
 * shape is the only thing the surface ever exposes — no path, locator,
 * source id, tombstone id, fingerprint or any other row detail.
 */
export type LifecycleStateCounts = Readonly<Record<LifecycleLocalFileState, number>>;

/**
 * The closed, redacted multipart session-state histogram of one snapshot
 * (multipart task 11): one closed {@link MultipartSessionState} key per
 * state, the count of durable multipart progress records that hold that
 * state. Missing states report zero so callers never have to defend
 * against `undefined` keys. The map shape is the only thing the surface
 * ever exposes — no session ID, staging key, provider upload ID, ETag,
 * presigned URL, digest, byte count or Vault path ever reaches it.
 */
export type MultipartSessionStateCounts = Readonly<Record<MultipartSessionState, number>>;

/** The closed, redacted input of the projection: counts, labels, booleans. */
export interface JournalSyncStatusInput {
  /** The durable reconcile flag of the journal meta (spec 6.4). */
  readonly isReconcileRequired: boolean;
  /** The redacted event histogram of the journal (closed labels, counts). */
  readonly eventStateErrorCounts: readonly JournalEventStateErrorCount[];
  /** Whether a usable in-memory access credential exists right now. */
  readonly hasAccessCredential: boolean;
  /** Whether a bounded foreground pass is currently active. */
  readonly isQueuePassActive: boolean;
  /** The outcome of the last finished pass, or null before the first. */
  readonly lastQueuePassOutcome: QueuePassOutcome | null;
  /** Redacted lifecycle-state histogram (Task 10). */
  readonly lifecycleStateCounts?: LifecycleStateCounts;
  /** Number of pending lifecycle events that still owe work. */
  readonly pendingLifecycleEventCount?: number;
  /** Number of failed attempts in the bounded audit ring. */
  readonly failedAttemptCount?: number;
  /** Closed set of lifecycle blocker codes observed right now. */
  readonly lifecycleBlockedReasonCodes?: readonly LifecycleBlockedReasonCode[];
  /** Redacted multipart session-state histogram (multipart task 11). */
  readonly multipartSessionStateCounts?: MultipartSessionStateCounts;
  /** Closed set of observed multipart safe-reason tokens (multipart task 11). */
  readonly multipartSafeReasonCodes?: readonly MultipartSafeReasonToken[];
  /** Number of parked conflict local applies that still owe their Vault apply (conflict inbox task 9). */
  readonly conflictApplyPendingCount?: number;
  /** Closed set of observed parked-apply safe reasons (conflict inbox task 9). */
  readonly conflictApplySafeReasonTokens?: readonly ConflictLocalRepairSafeReason[];
}

/**
 * The closed, redacted status snapshot (spec 11): one of the six status
 * values, the count of events that still owe work, the closed blocker
 * conditions present right now and (Task 10) the redacted source-lifecycle
 * surface (state histogram, pending-event count, failed-attempt count and
 * closed blocker codes). This shape is the whole telemetry surface.
 */
export interface JournalSyncStatusSnapshot {
  readonly kind: JournalSyncStatusKind;
  readonly pendingEventCount: number;
  readonly blockers: readonly JournalSyncBlocker[];
  /** Redacted lifecycle-state histogram (Task 10). Defaults to zero counts. */
  readonly lifecycleStateCounts: LifecycleStateCounts;
  /** Number of pending lifecycle events. Zero when not tracked. */
  readonly pendingLifecycleEventCount: number;
  /** Number of failed attempts in the bounded audit ring. Zero when not tracked. */
  readonly failedAttemptCount: number;
  /** Closed set of lifecycle blocker codes. Empty when none observed. */
  readonly lifecycleBlockedReasonCodes: readonly LifecycleBlockedReasonCode[];
  /** Redacted multipart session-state histogram (multipart task 11). Defaults to zero counts. */
  readonly multipartSessionStateCounts: MultipartSessionStateCounts;
  /** Closed set of multipart safe-reason tokens. Empty when none observed. */
  readonly multipartSafeReasonCodes: readonly MultipartSafeReasonToken[];
  /**
   * Number of parked conflict local applies that still owe their Vault
   * apply (conflict inbox task 9). Zero when not tracked. Every parked
   * row counts — including an attempt-capped row whose retry eligibility
   * gates on the attempt cap, never on the timestamp (the Task 8 ruling).
   */
  readonly conflictApplyPendingCount: number;
  /**
   * Closed set of parked-apply safe-reason tokens observed right now
   * (conflict inbox task 9). Empty when none observed. No locator,
   * conflict id, resolution id or timestamp ever joins this surface.
   */
  readonly conflictApplySafeReasonTokens: readonly ConflictLocalRepairSafeReason[];
}

// --- the projection ---------------------------------------------------------------------------------

/** The zero-initialised lifecycle-state histogram (Task 10). */
const ZERO_LIFECYCLE_STATE_COUNTS: LifecycleStateCounts = LIFECYCLE_LOCAL_FILE_STATES.reduce(
  (acc, state) => {
    acc[state] = 0;
    return acc;
  },
  {} as Record<LifecycleLocalFileState, number>,
) as LifecycleStateCounts;

/** The zero-initialised multipart session-state histogram (multipart task 11). */
const ZERO_MULTIPART_STATE_COUNTS: MultipartSessionStateCounts = MULTIPART_SESSION_STATES.reduce(
  (acc, state) => {
    acc[state] = 0;
    return acc;
  },
  {} as Record<MultipartSessionState, number>,
) as MultipartSessionStateCounts;

/**
 * Project one closed status snapshot (spec 11). Priority is fixed:
 * `reconcile_required` (a hard stop that must idle the driver) above
 * `login_required` (work owed, no credential), above an active pass
 * (`syncing`), above network-waiting work (`offline_queued`), above policy
 * evidence (`policy_blocked`), above `ready`.
 *
 * The Task 10 lifecycle surface is purely additive: the projection
 * passes the closed histogram through verbatim, restricts the closed
 * blocker codes to the closed enum, and never lets a non-enum string
 * reach the snapshot. Every numeric count is validated to be a
 * non-negative integer; an invalid value falls through to zero rather
 * than poison the surface. The multipart surface (task 11) folds through
 * the same two closed redacted gates.
 */
export function projectJournalSyncStatus(input: JournalSyncStatusInput): JournalSyncStatusSnapshot {
  let pendingEventCount = 0;
  let hasWaitingRetryPending = false;
  let hasPolicyBlockedEvents = false;
  for (const row of input.eventStateErrorCounts) {
    if ((JOURNAL_PENDING_EVENT_STATES as readonly string[]).includes(row.state)) {
      pendingEventCount += row.eventCount;
    }
    if (row.state === "waiting_retry" && row.safeError !== null) {
      hasWaitingRetryPending ||= WAITING_RETRY_SAFE_ERRORS.has(row.safeError);
    }
    hasPolicyBlockedEvents ||= row.state === "excluded_policy" && row.eventCount > 0;
  }

  const blockers: JournalSyncBlocker[] = [];
  if (input.isReconcileRequired) {
    blockers.push("reconcile_required");
  }
  const isLoginRequired =
    !input.hasAccessCredential &&
    (pendingEventCount > 0 || input.lastQueuePassOutcome === "login_required");
  if (isLoginRequired) {
    blockers.push("login_required");
  }
  for (const [blocker, state] of [
    ["blocked_size", "blocked_size"],
    ["excluded_policy", "excluded_policy"],
    ["blocked_conflict", "blocked_conflict"],
    ["deferred_lifecycle", "deferred_lifecycle"],
  ] as const satisfies readonly (readonly [JournalSyncBlocker, JournalEventState])[]) {
    if (input.eventStateErrorCounts.some((row) => row.state === state && row.eventCount > 0)) {
      blockers.push(blocker);
    }
  }

  const kind: JournalSyncStatusKind = input.isReconcileRequired
    ? "reconcile_required"
    : isLoginRequired
      ? "login_required"
      : input.isQueuePassActive
        ? "syncing"
        : hasWaitingRetryPending
          ? "offline_queued"
          : hasPolicyBlockedEvents
            ? "policy_blocked"
            : "ready";
  // Task 10: the lifecycle surface is folded through the same closed
  // redacted gate. A non-numeric count is treated as zero; a non-enum
  // blocker code is dropped silently. The closed enum is the only
  // string surface ever exposed.
  const lifecycleStateCounts = normaliseLifecycleStateCounts(input.lifecycleStateCounts);
  const pendingLifecycleEventCount = normaliseNonNegativeCount(input.pendingLifecycleEventCount);
  const failedAttemptCount = normaliseNonNegativeCount(input.failedAttemptCount);
  const lifecycleBlockedReasonCodes = normaliseLifecycleBlockedReasonCodes(
    input.lifecycleBlockedReasonCodes,
  );
  const multipartSessionStateCounts = normaliseMultipartSessionStateCounts(
    input.multipartSessionStateCounts,
  );
  const multipartSafeReasonCodes = normaliseMultipartSafeReasonCodes(
    input.multipartSafeReasonCodes,
  );
  const conflictApplyPendingCount = normaliseNonNegativeCount(input.conflictApplyPendingCount);
  const conflictApplySafeReasonTokens = normaliseConflictApplySafeReasonTokens(
    input.conflictApplySafeReasonTokens,
  );
  return {
    kind,
    pendingEventCount,
    blockers,
    lifecycleStateCounts,
    pendingLifecycleEventCount,
    failedAttemptCount,
    lifecycleBlockedReasonCodes,
    multipartSessionStateCounts,
    multipartSafeReasonCodes,
    conflictApplyPendingCount,
    conflictApplySafeReasonTokens,
  };
}

/**
 * Return the closed lifecycle-state histogram: the input is preserved
 * verbatim when every value is a non-negative integer AND every key is
 * a closed enum token; otherwise the zero histogram is returned so the
 * surface never carries an invalid value.
 */
function normaliseLifecycleStateCounts(
  value: LifecycleStateCounts | undefined,
): LifecycleStateCounts {
  if (value === undefined) {
    return ZERO_LIFECYCLE_STATE_COUNTS;
  }
  const counts: Record<LifecycleLocalFileState, number> = { ...ZERO_LIFECYCLE_STATE_COUNTS };
  for (const state of LIFECYCLE_LOCAL_FILE_STATES) {
    const candidate = value[state];
    if (candidate === undefined) {
      continue;
    }
    if (!Number.isInteger(candidate) || candidate < 0) {
      return ZERO_LIFECYCLE_STATE_COUNTS;
    }
    counts[state] = candidate;
  }
  return counts;
}

/** Coerce a count input to a non-negative integer (zero on missing/invalid). */
function normaliseNonNegativeCount(value: number | undefined): number {
  if (value === undefined) {
    return 0;
  }
  if (!Number.isInteger(value) || value < 0) {
    return 0;
  }
  return value;
}

/** Restrict the lifecycle blocked-reason-code list to the closed enum. */
function normaliseLifecycleBlockedReasonCodes(
  value: readonly LifecycleBlockedReasonCode[] | undefined,
): readonly LifecycleBlockedReasonCode[] {
  if (value === undefined) {
    return [];
  }
  const seen = new Set<LifecycleBlockedReasonCode>();
  const filtered: LifecycleBlockedReasonCode[] = [];
  for (const candidate of value) {
    if (
      typeof candidate === "string" &&
      (LIFECYCLE_BLOCKED_REASON_CODES as readonly string[]).includes(candidate) &&
      !seen.has(candidate)
    ) {
      seen.add(candidate);
      filtered.push(candidate);
    }
  }
  return filtered;
}

/**
 * Return the closed multipart session-state histogram (multipart task 11):
 * the input is preserved verbatim when every value is a non-negative
 * integer AND every key is a closed enum token; otherwise the zero
 * histogram is returned so the surface never carries an invalid value.
 */
function normaliseMultipartSessionStateCounts(
  value: MultipartSessionStateCounts | undefined,
): MultipartSessionStateCounts {
  if (value === undefined) {
    return ZERO_MULTIPART_STATE_COUNTS;
  }
  const counts: Record<MultipartSessionState, number> = { ...ZERO_MULTIPART_STATE_COUNTS };
  for (const state of MULTIPART_SESSION_STATES) {
    const candidate = value[state];
    if (candidate === undefined) {
      continue;
    }
    if (!Number.isInteger(candidate) || candidate < 0) {
      return ZERO_MULTIPART_STATE_COUNTS;
    }
    counts[state] = candidate;
  }
  return counts;
}

/**
 * Restrict the multipart safe-reason list to the closed twelve-token
 * vocabulary, deduplicated in first-observed order (multipart task 11):
 * a foreign snake_case string is dropped silently at the closed gate —
 * the closed enum is the only string surface ever exposed.
 */
function normaliseMultipartSafeReasonCodes(
  value: readonly MultipartSafeReasonToken[] | undefined,
): readonly MultipartSafeReasonToken[] {
  if (value === undefined) {
    return [];
  }
  const seen = new Set<MultipartSafeReasonToken>();
  const filtered: MultipartSafeReasonToken[] = [];
  for (const candidate of value) {
    if (
      typeof candidate === "string" &&
      (MULTIPART_SAFE_REASON_TOKENS as readonly string[]).includes(candidate) &&
      !seen.has(candidate)
    ) {
      seen.add(candidate);
      filtered.push(candidate);
    }
  }
  return filtered;
}

/**
 * Restrict the parked-apply safe-reason list to the closed three-token
 * vocabulary of journal schema v9's `conflict_local_repairs`, deduplicated
 * in first-observed order (conflict inbox task 9): a foreign snake_case
 * string is dropped silently at the closed gate — the closed enum is the
 * only string surface ever exposed.
 */
function normaliseConflictApplySafeReasonTokens(
  value: readonly ConflictLocalRepairSafeReason[] | undefined,
): readonly ConflictLocalRepairSafeReason[] {
  if (value === undefined) {
    return [];
  }
  const seen = new Set<ConflictLocalRepairSafeReason>();
  const filtered: ConflictLocalRepairSafeReason[] = [];
  for (const candidate of value) {
    if (
      typeof candidate === "string" &&
      (CONFLICT_LOCAL_REPAIR_SAFE_REASONS as readonly string[]).includes(candidate) &&
      !seen.has(candidate)
    ) {
      seen.add(candidate);
      filtered.push(candidate);
    }
  }
  return filtered;
}

/**
 * Render the small status-bar text of spec 11: the exact status value plus
 * the pending count, and nothing else.
 */
export function renderJournalSyncStatusText(snapshot: JournalSyncStatusSnapshot): string {
  const countSuffix =
    snapshot.pendingEventCount > 0 ? ` (${snapshot.pendingEventCount})` : "";
  return `${SYNC_STATUS_TEXT[snapshot.kind]}${countSuffix}`;
}

/**
 * The composed human-readable status line of the small surfaces (the
 * status bar and the diagnostics export, conflict inbox task 9): the exact
 * spec-11 status text plus, only while parked conflict local applies owe
 * their Vault apply, the fixed `Conflict apply pending` fragment with the
 * parked count. Closed tokens and counts only — no locator, conflict id,
 * resolution id, timestamp or any other journal detail ever joins the
 * line (the parked rows' retry eligibility gates on the attempt cap, not
 * the timestamp, so the fragment reflects the owed work itself).
 */
export function renderJournalSyncStatus(snapshot: JournalSyncStatusSnapshot): string {
  const baseText = renderJournalSyncStatusText(snapshot);
  if (snapshot.conflictApplyPendingCount <= 0) {
    return baseText;
  }
  return `${baseText} · Conflict apply pending (${snapshot.conflictApplyPendingCount})`;
}

/**
 * The blocker guidance lines of one snapshot, in closed table order — the
 * helper the settings surface uses so guidance never carries journal detail.
 */
export function syncBlockerGuidanceLines(
  snapshot: JournalSyncStatusSnapshot,
): readonly string[] {
  return snapshot.blockers.map((blocker) => SYNC_BLOCKER_GUIDANCE_TEXT[blocker]);
}
