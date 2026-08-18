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
 */

import type { JournalEventState } from "./contracts";
import { JOURNAL_PENDING_EVENT_STATES } from "./contracts";
import type { QueuePassOutcome } from "./queue-driver";
import type { JournalEventStateErrorCount } from "./repository";

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
    "Sync stopped: journal reconciliation is required before syncing can continue. Repair and reconciliation are owned by child 6.",
};

/**
 * The safe error labels that mean "queued work is waiting on the network"
 * (spec 12): an event in `waiting_retry` under one of these labels projects
 * to `Offline — queued`, not to a healthy ready state.
 */
const NETWORK_RETRY_SAFE_ERRORS: ReadonlySet<string> = new Set([
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
]);

// --- the closed input and output ------------------------------------------------------------------

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
}

/**
 * The closed, redacted status snapshot (spec 11): one of the six status
 * values, the count of events that still owe work, and the closed blocker
 * conditions present right now. This shape is the whole telemetry surface.
 */
export interface JournalSyncStatusSnapshot {
  readonly kind: JournalSyncStatusKind;
  readonly pendingEventCount: number;
  readonly blockers: readonly JournalSyncBlocker[];
}

// --- the projection ---------------------------------------------------------------------------------

/**
 * Project one closed status snapshot (spec 11). Priority is fixed:
 * `reconcile_required` (a hard stop that must idle the driver) above
 * `login_required` (work owed, no credential), above an active pass
 * (`syncing`), above network-waiting work (`offline_queued`), above policy
 * evidence (`policy_blocked`), above `ready`.
 */
export function projectJournalSyncStatus(input: JournalSyncStatusInput): JournalSyncStatusSnapshot {
  let pendingEventCount = 0;
  let hasNetworkRetryPending = false;
  let hasPolicyBlockedEvents = false;
  for (const row of input.eventStateErrorCounts) {
    if ((JOURNAL_PENDING_EVENT_STATES as readonly string[]).includes(row.state)) {
      pendingEventCount += row.eventCount;
    }
    if (row.state === "waiting_retry" && row.safeError !== null) {
      hasNetworkRetryPending ||= NETWORK_RETRY_SAFE_ERRORS.has(row.safeError);
    }
    hasPolicyBlockedEvents ||= row.state === "excluded_policy";
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
        : hasNetworkRetryPending
          ? "offline_queued"
          : hasPolicyBlockedEvents
            ? "policy_blocked"
            : "ready";
  return { kind, pendingEventCount, blockers };
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
 * The blocker guidance lines of one snapshot, in closed table order — the
 * helper the settings surface uses so guidance never carries journal detail.
 */
export function syncBlockerGuidanceLines(
  snapshot: JournalSyncStatusSnapshot,
): readonly string[] {
  return snapshot.blockers.map((blocker) => SYNC_BLOCKER_GUIDANCE_TEXT[blocker]);
}
