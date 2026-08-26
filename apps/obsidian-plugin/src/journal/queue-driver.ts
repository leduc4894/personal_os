/**
 * The bounded foreground sync queue driver (spec 8, 12).
 *
 * One pass starts only through an explicit trigger — plugin load after safe
 * recovery, a Vault event or `Sync now` — selects the oldest eligible event
 * and holds at most ONE active content request at a time. It is never a
 * daemon: the pass owns a deadline, ends before plugin unload or mobile
 * suspension, and once stopped no new pass starts and every late
 * `requestUrl` result is discarded because the underlying request cannot
 * be aborted.
 *
 * Persist-before-network (spec 7.2, 10.1): every journal state transition
 * lands before the network action it guards — `preflight` freezes the
 * fingerprint before the preflight request, `uploading` persists the opaque
 * operation handle before the content stream, and the client re-fingerprints
 * the current bytes against the frozen digest before one byte is sent. A
 * lost response returns the event to bounded jittered backoff; the next
 * pass re-preflights with the SAME event/idempotency identity so the
 * server's exact replay either returns the original receipt or reopens the
 * flow (spec 10.3). If that preflight reports that the server already owns
 * the claimed receive, only the unchanged operation handle persisted for
 * this frozen event may resume the content request.
 *
 * Credentials (spec 8): one pass refreshes the access credential at most
 * once; a second 401, a revoked family or a failed refresh ends the pass
 * with `login_required` while the queue survives untouched for the next
 * login. The SAME per-pass budget covers both lanes (fix round 4): the
 * lifecycle drain's `login_required` verdict consumes the budget, rotates
 * the credential and retries its dispatch once before ending the pass —
 * the lane runs first, so without this the content lane's requests (and
 * with them the only refresh seam) would never run while lifecycle work
 * stays pending. `excluded_policy`, `blocked_size`, `blocked_conflict`,
 * `deferred_lifecycle` and `integrity_failed` never retry automatically.
 *
 * Privacy (spec 9): the driver emits closed outcome labels and opaque
 * correlation IDs only — no path, digest, credential or provider detail
 * reaches a thrown error, a journal row or a diagnostic surface.
 */

import type { JournalEvent, JournalEventState, JournalSafeErrorLabel, LocalFile } from "./contracts";
import { JOURNAL_SAFE_ERROR_LABELS, MAX_FILE_SIZE_BYTES } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import type { LifecycleDriver, LifecycleRunOutcome } from "./lifecycle-driver";
import { isUuid } from "./repository";
import type { JournalRepository } from "./repository";
import { JournalStoreError, type JournalStoreErrorReason } from "./sqlite-database";
import type { JournalPreflightOutcome, JournalSyncApi, SmallFileTerminalReceipt } from "./sync-api";
import { SyncApiError } from "./sync-api";
import type { SyncApiFailureKind } from "./sync-api";
import type {
  SyncDiagnosticsTrail,
  SyncDiagnosticToken,
  SyncEventStateToken,
  SyncParkSiteToken,
} from "./sync-diagnostics-trail";
import { envelopeErrorCode, envelopeRequestId } from "./sync-diagnostics-trail";

// --- frozen bounds (spec 8) -----------------------------------------------------------------------

/** One foreground pass runs at most this long before it must end. */
export const QUEUE_PASS_DEADLINE_MS = 60_000;

/** One network request may hold at most this long; late results are discarded. */
export const QUEUE_REQUEST_TIMEOUT_MS = 30_000;

/** The first retry delay after one failed attempt: one second (spec 8). */
export const RETRY_BACKOFF_INITIAL_MS = 1_000;

/** The retry delay ceiling: five minutes (spec 8). */
export const RETRY_BACKOFF_MAXIMUM_MS = 300_000;

/** The bounded jitter fraction added on top of the exponential delay. */
export const RETRY_BACKOFF_JITTER_FRACTION = 0.25;

/**
 * The exponential retry schedule of spec 8: `initial * 2^(attempt-1)`
 * capped at five minutes, plus a bounded jitter fraction of the delay,
 * capped again. The injected randomness keeps tests deterministic.
 *
 * The result is rounded to a whole millisecond BEFORE the outer cap: with
 * the real `Math.random` seam the jitter product is a float, and a
 * fractional backoff would reach `markEventWaitingRetry` as a
 * non-integer `nextEligibleRetryEpochMs` — rejected by its argument
 * validation as `journal_mutation_failed`, so no retry park would ever
 * land. `Math.min` applied after `Math.round` keeps the rounded result
 * from ever exceeding the five-minute ceiling.
 */
export function computeRetryBackoffMs(
  attemptCount: number,
  randomJitter: () => number,
): number {
  if (!Number.isInteger(attemptCount) || attemptCount < 1) {
    throw new TypeError("attempt count must be a positive integer");
  }
  const exponent = Math.min(attemptCount - 1, 30);
  const exponentialDelayMs = Math.min(
    RETRY_BACKOFF_MAXIMUM_MS,
    RETRY_BACKOFF_INITIAL_MS * 2 ** exponent,
  );
  const jitterMs = exponentialDelayMs * RETRY_BACKOFF_JITTER_FRACTION * randomJitter();
  return Math.min(RETRY_BACKOFF_MAXIMUM_MS, Math.round(exponentialDelayMs + jitterMs));
}

// --- ports and summaries -----------------------------------------------------------------------------

/**
 * The narrow read-only Vault slice the driver needs to re-fingerprint and
 * stream one file: the current bytes of a regular file, or null when the
 * path is not (or is no longer) a regular file.
 */
export interface QueueVaultFileReader {
  readRegularFileBytes(normalizedPath: string): Promise<Uint8Array | null>;
}

/**
 * One bounded pass refresh outcome; closed vocabulary. `retry_scheduled`
 * marks a pass ended by a retryable failure: the failed event sits in
 * bounded backoff, so such a pass is never continuable automatically.
 * `pass_wrapper_failed` (closed-reason surfacing C1 P2) is produced ONLY
 * by the composition's pass wrapper when `requestPass` itself threw —
 * the driver never returns it, and a wrapper-failed pass never renders
 * as `completed`.
 */
export type QueuePassOutcome =
  | "completed"
  | "deadline_reached"
  | "stopped"
  | "login_required"
  | "retry_scheduled"
  | "pass_already_running"
  | "pass_wrapper_failed";

/** The closed summary of one bounded pass: an outcome and a count only. */
export interface QueuePassSummary {
  readonly outcome: QueuePassOutcome;
  readonly processedEventCount: number;
}

export interface JournalQueueDriverOptions {
  readonly repository: JournalRepository;
  readonly syncApi: JournalSyncApi;
  readonly fileBytesReader: QueueVaultFileReader;
  /**
   * Optional lifecycle driver. When provided, the content pass
   * interleaves: it first calls `lifecycleDriver.runOne(signal)` to
   * drain one ready lifecycle event (predecessor-must-be-committed
   * rule), then processes one content event. The two lanes never
   * have an active mutating request in flight at the same time, and
   * the predecessor ordering invariant from the lifecycle contract
   * (spec 19.2) is enforced deterministically.
   */
  readonly lifecycleDriver?: LifecycleDriver;
  /** Rotates the access credential once; a rejection ends the pass as login required. */
  readonly refreshAccessToken: () => Promise<void>;
  /** Clock for deadlines, retries and attempt timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
  /** Opaque request correlation IDs; defaults to `crypto.randomUUID`. */
  readonly createCorrelationId?: () => string;
  /** Random source for bounded retry jitter; defaults to `Math.random`. */
  readonly randomJitter?: () => number;
  /** Pass deadline override; defaults to {@link QUEUE_PASS_DEADLINE_MS}. */
  readonly passDeadlineMs?: number | undefined;
  /** Request timeout override; defaults to {@link QUEUE_REQUEST_TIMEOUT_MS}. */
  readonly requestTimeoutMs?: number | undefined;
  /**
   * The optional durable diagnostics trail. The driver appends
   * fire-and-forget and the trail never rejects, so the trail observes
   * wire failures, journal failures and pass outcomes without ever
   * blocking or breaking the sync path.
   */
  readonly diagnosticTrail?: SyncDiagnosticsTrail | undefined;
}

/**
 * Why one processed event ended the pass. Only the natural deadline
 * boundary stays continuable by the dispatcher: failure, retry and
 * journal-failure exits must never trigger an automatic follow-up pass,
 * because the failed event sits in bounded backoff while a later queued
 * event would otherwise be sent out of order.
 */
type PassEndReason =
  | "end_stopped"
  | "end_deadline_boundary"
  | "end_login_required"
  | "end_retry_scheduled"
  | "end_journal_failure";

type PassContinuation = "continue" | PassEndReason;

/**
 * The bounded in-memory ring of closed journal-failure reason tokens (fix
 * round 5): the pass loop's fail-closed catch used to discard the closed
 * `JournalStoreErrorReason` entirely, making environmental commit failures
 * (the live park mystery) undiagnosable by design. Closed tokens only —
 * never a raw error message, path, digest or credential.
 */
const MAX_JOURNAL_FAILURE_REASON_HISTORY = 5;

/** The per-pass credential budget: at most one refresh, one login verdict. */
interface RefreshBudget {
  hasRefreshed: boolean;
  requiresLogin: boolean;
}

/**
 * The one safe narrowing predicate of thrown sync failures (child six
 * deferred remediation): only a real `SyncApiError` instance carries the
 * closed `SyncApiFailureKind`, so the kind is extracted through
 * `instanceof` narrowing — never duck-typed `.kind` member access on an
 * unknown value — and the answer is the CLOSED kind union, not `string`.
 */
function syncFailureKind(error: unknown): SyncApiFailureKind | null {
  return error instanceof SyncApiError ? error.kind : null;
}

/**
 * The fixed switch of the park-failure state capture (sync error tracing
 * park diagnosis round): one closed journal event state maps to exactly one
 * closed `state_*` trail token. No free-form strings.
 */
function journalEventStateToken(state: JournalEventState): SyncEventStateToken {
  switch (state) {
    case "queued":
      return "state_queued";
    case "waiting_retry":
      return "state_waiting_retry";
    case "preflight":
      return "state_preflight";
    case "uploading":
      return "state_uploading";
    case "blocked_conflict":
      return "state_blocked_conflict";
    case "excluded_policy":
      return "state_excluded_policy";
    case "blocked_size":
      return "state_blocked_size";
    case "deferred_lifecycle":
      return "state_deferred_lifecycle";
    case "integrity_failed":
      return "state_integrity_failed";
    case "committed":
      return "state_committed";
    case "no_change":
      return "state_no_change";
  }
}

// --- the driver ----------------------------------------------------------------------------------------

/**
 * The park-failure throw-site discriminator (diagnostic round U2): ONE more
 * closed token naming WHY a `markEventWaitingRetry` throw is consistent
 * with which site, derived entirely OUTSIDE the repository from the values
 * the driver already holds in scope. `site_argument_validation` when any
 * precondition the repository's own argument validation would reject is
 * observable (a non-uuid event id per the exported repository `isUuid`, a
 * safe error outside the closed labels, or a retry epoch that is not a
 * non-negative integer); otherwise `site_mutation_internal` — the
 * arguments were valid, and together with the entry's row-present/state
 * token the throw happened inside the serialized mutation. Pure and
 * side-effect free; never touches the repository.
 */
export function parkFailureSiteToken(
  eventId: string,
  safeError: string,
  nextEligibleRetryEpochMs: number,
): SyncParkSiteToken {
  const areParkArgumentsValid =
    isUuid(eventId) &&
    (JOURNAL_SAFE_ERROR_LABELS as readonly string[]).includes(safeError) &&
    Number.isInteger(nextEligibleRetryEpochMs) &&
    nextEligibleRetryEpochMs >= 0;
  return areParkArgumentsValid ? "site_mutation_internal" : "site_argument_validation";
}

/**
 * The bounded foreground queue pass driver. One instance holds the pass
 * state (running, stopped, refresh budget); the journal remains the only
 * durable truth, so an interrupted pass resumes through the ordinary
 * eligibility of `preflight`/`uploading` rows.
 */
export class JournalQueueDriver {
  readonly #repository: JournalRepository;
  readonly #syncApi: JournalSyncApi;
  readonly #fileBytesReader: QueueVaultFileReader;
  readonly #lifecycleDriver: LifecycleDriver | null;
  readonly #passAbortController: AbortController;
  readonly #refreshAccessToken: () => Promise<void>;
  readonly #nowEpochMs: () => number;
  readonly #createCorrelationId: () => string;
  readonly #randomJitter: () => number;
  readonly #passDeadlineMs: number;
  readonly #requestTimeoutMs: number;
  readonly #diagnosticTrail: SyncDiagnosticsTrail | null;
  /** The pass's last successful request outcome's envelope request id. */
  #lastPassWireRequestId: string | null = null;
  #isStopped = false;
  #isPassRunning = false;
  readonly #journalFailureReasons: JournalStoreErrorReason[] = [];

  constructor(options: JournalQueueDriverOptions) {
    this.#repository = options.repository;
    this.#syncApi = options.syncApi;
    this.#fileBytesReader = options.fileBytesReader;
    this.#lifecycleDriver = options.lifecycleDriver ?? null;
    this.#passAbortController = new AbortController();
    this.#refreshAccessToken = options.refreshAccessToken;
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    this.#createCorrelationId = options.createCorrelationId ?? (() => crypto.randomUUID());
    this.#randomJitter = options.randomJitter ?? (() => Math.random());
    this.#passDeadlineMs = options.passDeadlineMs ?? QUEUE_PASS_DEADLINE_MS;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? QUEUE_REQUEST_TIMEOUT_MS;
    this.#diagnosticTrail = options.diagnosticTrail ?? null;
  }

  /** Whether the driver was stopped for unload/suspension and runs nothing new. */
  get isStopped(): boolean {
    return this.#isStopped;
  }

  /**
   * Whether outbound dispatch is paused by an active repair barrier
   * (task 11, spec 12.1): a reconciliation run freezes observation
   * generation G and holds every outbound row until the completion
   * releases them. An unreadable reconciliation state fails CLOSED — no
   * dispatch may race an unknown barrier — and the closed store reason
   * surfaces through the existing bounded ring and `journal_failure`
   * trail entry instead of being swallowed.
   */
  #isOutboundDispatchPaused(): boolean {
    try {
      return this.#repository.deviceSync.readState().barrierGeneration !== null;
    } catch (error) {
      this.#recordJournalFailureReason(error);
      this.#recordJournalFailureTrailEntry(error);
      return true;
    }
  }

  /**
   * Stop the driver (plugin unload / mobile suspension): no new pass
   * starts, and any in-flight `requestUrl` result arriving afterwards is
   * discarded rather than applied to the journal. The pass-scoped
   * AbortController is aborted so the lifecycle lane (when wired in)
   * also cancels cleanly.
   */
  stop(): void {
    this.#isStopped = true;
    this.#passAbortController.abort();
  }

  /**
   * The foreground trigger entry (load, Vault event, `Sync now`): runs one
   * bounded pass unless the driver is stopped or a pass is already active —
   * a trigger never queues a second pass and never recurses.
   */
  async requestPass(): Promise<QueuePassSummary> {
    if (this.#isPassRunning) {
      return { outcome: "pass_already_running", processedEventCount: 0 };
    }
    return this.runPass();
  }

  /**
   * Run one bounded pass: the oldest eligible event at a time, one active
   * content request, until the queue drains, the deadline passes, login is
   * required, a retryable failure ends the pass with `retry_scheduled`, or
   * the driver stops. When a lifecycle driver is wired in, the pass
   * interleaves: it first drains the lifecycle lane to IDLE (spec 19.2
   * predecessor rule, task 9 fix round 1 I3), then processes one content
   * event. The two lanes never have an active mutating request in flight at
   * the same time, and the content lane never sees a lifecycle event
   * because the lane filter is enforced by draining the lifecycle lane
   * before each content selection.
   */
  async runPass(): Promise<QueuePassSummary> {
    if (this.#isStopped) {
      return { outcome: "stopped", processedEventCount: 0 };
    }
    if (this.#isPassRunning) {
      return { outcome: "pass_already_running", processedEventCount: 0 };
    }
    this.#isPassRunning = true;
    this.#lastPassWireRequestId = null;
    const passDeadlineEpochMs = this.#nowEpochMs() + this.#passDeadlineMs;
    const refreshBudget: RefreshBudget = { hasRefreshed: false, requiresLogin: false };
    let processedEventCount = 0;
    let passOutcome: QueuePassOutcome = "completed";
    let passEndReason: PassEndReason | null = null;
    try {
      while (!this.#isStopped && this.#nowEpochMs() < passDeadlineEpochMs) {
        // The repair barrier pauses outbound dispatch (spec 12.1): both
        // lanes hold until the reconciliation completes and releases
        // every row — watcher capture keeps recording meanwhile.
        if (this.#isOutboundDispatchPaused()) {
          break;
        }
        // Drain the lifecycle lane to IDLE before each content-lane
        // selection. The previous one-call-per-iteration design let
        // the content lane re-select a queued lifecycle event when
        // two lifecycle events were queued — the content lane then
        // tried to send it through the wrong API. Looping until
        // `runOne` returns "idle" keeps the predecessor rule
        // deterministic and avoids the lane-crossing dispatch.
        let lifecycleLoginRequired = false;
        if (this.#lifecycleDriver !== null && !this.#isStopped) {
          let lifecycleOutcome: LifecycleRunOutcome = "idle";
          do {
            try {
              lifecycleOutcome = await this.#lifecycleDriver.runOne(
                this.#passAbortController.signal,
              );
            } catch {
              // The lifecycle driver swallows its own errors and
              // returns closed outcomes; a thrown error here is
              // fail-closed.
              break;
            }
            if (this.#isStopped || this.#nowEpochMs() >= passDeadlineEpochMs) {
              break;
            }
            if (lifecycleOutcome === "login_required") {
              // The lifecycle lane parked its event retryable under the
              // `login_required` safe label. This lane runs FIRST, so
              // ending the pass here would also starve the content lane's
              // requests — and with them the pass's only credential
              // refresh (fix round 4: a stale server-rejected 401 or a
              // missing access credential funnels into this verdict, and
              // with pending lifecycle work every pass died here while
              // `refreshAccessToken` never fired — an infinite stall that
              // recovered only through an external rotation). Give the
              // shared one-per-pass refresh budget (spec 8) its chance:
              // consume it, rotate the credential once, and retry the
              // lifecycle dispatch ONCE. The budget is shared with the
              // content lane, which simply cannot refresh again this
              // pass.
              if (refreshBudget.hasRefreshed) {
                // A second login verdict with the budget spent: keep the
                // park-and-end-pass semantics (second-401 discipline).
                lifecycleLoginRequired = true;
                break;
              }
              refreshBudget.hasRefreshed = true;
              let didRefresh = false;
              try {
                await this.#refreshAccessToken();
                didRefresh = true;
              } catch {
                refreshBudget.requiresLogin = true;
                this.#recordCredentialRefreshFailureTrailEntry();
              }
              if (this.#isStopped) {
                break;
              }
              if (!didRefresh) {
                // The refresh itself failed: the parked event stays
                // retryable and the pass ends login_required with the
                // queue preserved for the next login.
                lifecycleLoginRequired = true;
                break;
              }
              try {
                lifecycleOutcome = await this.#lifecycleDriver.runOne(
                  this.#passAbortController.signal,
                );
              } catch {
                break;
              }
              if (this.#isStopped || this.#nowEpochMs() >= passDeadlineEpochMs) {
                break;
              }
              if (lifecycleOutcome === "login_required") {
                // The rotated credential was rejected again: the retry's
                // own dispatch parked its event; end the pass
                // login_required (second-401 discipline).
                lifecycleLoginRequired = true;
                break;
              }
              // A committed retry — or any other closed outcome — falls
              // through to the normal drain discipline below (the while
              // condition decides whether the drain continues).
            }
          } while (lifecycleOutcome === "committed");
          if (this.#isStopped) {
            break;
          }
          if (lifecycleLoginRequired) {
            passEndReason = "end_login_required";
            break;
          }
        }
        let continuation: PassContinuation;
        try {
          const event = this.#repository.readOldestEligibleEvent(this.#nowEpochMs());
          if (event === null) {
            break;
          }
          continuation = await this.#processEvent(event, passDeadlineEpochMs, refreshBudget);
        } catch (error) {
          // A journal failure mid-pass fails closed: the pass ends with the
          // journal as the durable truth and never crashes its trigger. The
          // failure endures — no deadline conversion, no automatic follow-up.
          // Fix round 5: the swallowed error's closed reason token lands in
          // the bounded diagnostic ring so the failure is diagnosable.
          // Sync error tracing task 1: the same closed reason also lands on
          // the durable trail as a `journal_failure` entry.
          this.#recordJournalFailureReason(error);
          this.#recordJournalFailureTrailEntry(error);
          passEndReason = "end_journal_failure";
          break;
        }
        processedEventCount += 1;
        if (continuation !== "continue") {
          passEndReason = continuation;
          break;
        }
      }
      if (this.#isStopped) {
        passOutcome = "stopped";
      } else {
        switch (passEndReason) {
          case "end_login_required":
            passOutcome = "login_required";
            break;
          case "end_retry_scheduled":
            // The retryable failure owns the pass end reason even when it
            // happens exactly at the deadline: the failed event is in
            // bounded backoff, so an automatic follow-up pass would only
            // pick a LATER queued event and break the failure discipline.
            passOutcome = "retry_scheduled";
            break;
          case "end_journal_failure":
            // Keep the closed summary a journal failure produced before any
            // deadline conversion; it must never become continuable.
            passOutcome = refreshBudget.requiresLogin ? "login_required" : "completed";
            break;
          case "end_deadline_boundary":
            // The deadline guard inside the content stream: a natural
            // deadline boundary whose `uploading` event stays eligible, so
            // the pass remains continuable while eligible work remains.
            passOutcome = this.#hasEligibleEventNow() ? "deadline_reached" : "completed";
            break;
          case null:
            // While-condition exit (a natural deadline boundary) or a
            // drained queue. A deadline is a bounded-pass boundary, not
            // proof that the durable queue drained; the dispatcher uses
            // this closed outcome to start a fresh bounded pass without
            // waiting for another external trigger — but only when the
            // deadline actually passed AND eligible work still remains.
            if (
              this.#nowEpochMs() >= passDeadlineEpochMs &&
              this.#hasEligibleEventNow()
            ) {
              passOutcome = "deadline_reached";
            }
            break;
          case "end_stopped":
            // Stop guards land here only when the after-loop `stopped`
            // handling above did not already cover them; kept exhaustive.
            passOutcome = "stopped";
            break;
        }
      }
      return { outcome: passOutcome, processedEventCount };
    } finally {
      // Sync error tracing task 1: every pass that actually ran appends ONE
      // `pass_outcome` entry to the durable trail — the closed outcome plus
      // the sampled envelope request id of the pass's last successful
      // request outcome (the success-path correlation to server logs). The
      // append is fire-and-forget: a stalled or failing trail must never
      // delay or break the pass.
      this.#recordPassOutcomeTrailEntry(passOutcome);
      this.#isPassRunning = false;
    }
  }

  /**
   * The bounded closed-token view of the journal failures the pass loop's
   * fail-closed catch swallowed (fix round 5). Newest last; at most
   * {@link MAX_JOURNAL_FAILURE_REASON_HISTORY} tokens; in-memory only.
   */
  readJournalFailureReasons(): readonly JournalStoreErrorReason[] {
    return [...this.#journalFailureReasons];
  }

  /** Record one swallowed journal failure's closed reason, if it has one. */
  #recordJournalFailureReason(error: unknown): void {
    if (!(error instanceof JournalStoreError)) {
      return;
    }
    this.#journalFailureReasons.push(error.reason);
    if (this.#journalFailureReasons.length > MAX_JOURNAL_FAILURE_REASON_HISTORY) {
      this.#journalFailureReasons.shift();
    }
  }

  /**
   * Append one `journal_failure` trail entry with the swallowed store
   * error's closed reason (sync error tracing task 1). Closed tokens only;
   * fire-and-forget, never blocking the pass.
   */
  #recordJournalFailureTrailEntry(error: unknown): void {
    if (!(error instanceof JournalStoreError)) {
      return;
    }
    void this.#diagnosticTrail?.append({ kind: "journal_failure", tokens: [error.reason] });
  }

  /**
   * Append one failure trail entry for one failed wire request outcome
   * that reached the failure hook (sync error tracing task 1). Trail v2
   * taxonomy (task 7): a credential absence BEFORE any transport contact —
   * the sync client's pre-contact `login_required` rejection — records the
   * `credential_failure` kind with the closed `access_missing` stage; it is
   * never a wire failure, because no HTTP attempt reached the transport.
   * Every other `SyncApiError` keeps the `wire_failure` kind: the closed
   * failure kind, plus the failing envelope's opaque request id when the
   * server sent a CANONICAL UUID (the trail's constructor gate nulls any
   * non-conforming value — the rejected value records nothing). A local
   * (non-wire) failure records nothing. Diagnostic round U1: when the
   * failing body parsed as the canonical envelope, its closed server error
   * code rides along as one additional closed token between the kind and
   * the request id — whitelisted at the trail boundary against the declared
   * runtime vocabulary, so a null code (an edge HTML body), a foreign code,
   * or a non-conforming code records nothing extra.
   */
  #recordWireFailureTrailEntry(error: unknown): void {
    if (this.#diagnosticTrail === null || !(error instanceof SyncApiError)) {
      return;
    }
    if (error.isCredentialAbsent) {
      void this.#diagnosticTrail.append({
        kind: "credential_failure",
        tokens: ["access_missing", error.kind],
      });
      return;
    }
    const tokens: SyncDiagnosticToken[] = [error.kind];
    if (error.wireErrorCode !== null) {
      const errorCodeToken = envelopeErrorCode(error.wireErrorCode);
      if (errorCodeToken !== null) {
        tokens.push(errorCodeToken);
      }
    }
    if (error.requestId !== null) {
      const requestIdToken = envelopeRequestId(error.requestId);
      if (requestIdToken !== null) {
        tokens.push(requestIdToken);
      }
    }
    void this.#diagnosticTrail.append({ kind: "wire_failure", tokens });
  }

  /**
   * Append one `credential_failure` trail entry for a failed credential
   * refresh (trail v2 taxonomy, task 7): the refresh seam threw before any
   * retried transport contact, so the swallowed failure surfaces as the
   * closed `refresh_failed` stage instead of disappearing into the pass's
   * login verdict. Fire-and-forget, never blocking the pass.
   */
  #recordCredentialRefreshFailureTrailEntry(): void {
    void this.#diagnosticTrail?.append({
      kind: "credential_failure",
      tokens: ["refresh_failed", "login_required"],
    });
  }

  /**
   * Append one `pass_outcome` trail entry (sync error tracing task 1): the
   * closed pass outcome plus the sampled request id of the pass's last
   * successful request outcome, when the server sent a canonical UUID (a
   * non-conforming sampled value is omitted by the trail's constructor
   * gate).
   */
  #recordPassOutcomeTrailEntry(outcome: QueuePassOutcome): void {
    if (this.#diagnosticTrail === null) {
      return;
    }
    const tokens: SyncDiagnosticToken[] = [outcome];
    if (this.#lastPassWireRequestId !== null) {
      const requestIdToken = envelopeRequestId(this.#lastPassWireRequestId);
      if (requestIdToken !== null) {
        tokens.push(requestIdToken);
      }
    }
    void this.#diagnosticTrail.append({ kind: "pass_outcome", tokens });
  }

  /**
   * Fail-closed eligibility re-probe for the deadline conversion: the pass
   * may report `deadline_reached` only when an eligible event still remains.
   * A throwing journal means no follow-up pass, and the probe never escapes
   * `runPass`.
   */
  #hasEligibleEventNow(): boolean {
    try {
      return this.#repository.readOldestEligibleEvent(this.#nowEpochMs()) !== null;
    } catch {
      return false;
    }
  }

  // --- one event -------------------------------------------------------------------------------------

  async #processEvent(
    event: JournalEvent,
    passDeadlineEpochMs: number,
    refreshBudget: RefreshBudget,
  ): Promise<PassContinuation> {
    const correlationId = this.#createCorrelationId();
    try {
      if (event.state === "queued" || event.state === "waiting_retry") {
        // Freeze before the network action this transition guards (spec 7.2).
        await this.#repository.markEventPreflightStarted(event.eventId);
      }

      const outcome = await this.#requestWithDeadline(
        () => this.#sendPreflight(event),
        passDeadlineEpochMs,
        refreshBudget,
      );
      if (this.#isStopped) {
        return "end_stopped";
      }
      switch (outcome.outcome) {
        case "excluded":
          await this.#closeTerminal(event.eventId, "excluded_policy", "excluded_policy", correlationId);
          return "continue";
        case "conflict":
          await this.#closeTerminal(event.eventId, "blocked_conflict", "blocked_conflict", correlationId);
          return "continue";
        case "committed_replay":
          await this.#repository.recordCommittedReceipt({
            eventId: event.eventId,
            sourceId: outcome.receipt.sourceId,
            baseVersionId: outcome.receipt.sourceVersionId,
          });
          return "continue";
        case "no_change":
          await this.#repository.recordNoChangeReceipt({
            eventId: event.eventId,
            sourceId: outcome.receipt.sourceId,
            baseVersionId: outcome.receipt.sourceVersionId,
          });
          return "continue";
        case "single_part_upload":
          return await this.#streamContent(event, outcome.operationId, passDeadlineEpochMs, refreshBudget, correlationId);
      }
    } catch (error) {
      if (this.#isStopped) {
        return "end_stopped";
      }
      let failure = error;
      const resumeOperationId = this.#claimedResumeOperationId(event, error);
      if (resumeOperationId !== null) {
        try {
          return await this.#streamContent(
            event,
            resumeOperationId,
            passDeadlineEpochMs,
            refreshBudget,
            correlationId,
          );
        } catch (resumeError) {
          if (this.#isStopped) {
            return "end_stopped";
          }
          failure = resumeError;
        }
      }
      const continuation = await this.#handleFailure(event.eventId, failure, correlationId, refreshBudget);
      return continuation;
    }
  }

  #claimedResumeOperationId(event: JournalEvent, error: unknown): string | null {
    // The same instanceof narrowing: the resume flag is read only off a
    // real `SyncApiError`, never duck-typed off an unknown value.
    if (
      !(error instanceof SyncApiError) ||
      error.kind !== "operation_retry_required" ||
      !error.canResumeClaimedOperation ||
      (event.state !== "uploading" && event.state !== "waiting_retry") ||
      event.operationId === null
    ) {
      return null;
    }
    const persisted = this.#repository.readEvent(event.eventId);
    return persisted?.operationId === event.operationId ? event.operationId : null;
  }

  async #streamContent(
    event: JournalEvent,
    operationId: string,
    passDeadlineEpochMs: number,
    refreshBudget: RefreshBudget,
    correlationId: string,
  ): Promise<PassContinuation> {
    // The operation handle lands before the content request it guards.
    await this.#repository.markEventUploading(event.eventId, operationId);
    if (this.#isStopped) {
      return "end_stopped";
    }
    if (this.#isPastDeadline(passDeadlineEpochMs)) {
      // A natural deadline boundary: the `uploading` event stays eligible
      // unconditionally, so the pass remains continuable by the dispatcher.
      return "end_deadline_boundary";
    }

    const localFile = this.#repository.readLocalFileByLocalFileId(event.localFileId);
    if (localFile === null) {
      await this.#closeTerminal(event.eventId, "deferred_lifecycle", "deferred_lifecycle", correlationId);
      return "continue";
    }
    const contentBytes = await this.#fileBytesReader.readRegularFileBytes(localFile.normalizedPath);
    if (contentBytes === null) {
      await this.#closeTerminal(event.eventId, "deferred_lifecycle", "deferred_lifecycle", correlationId);
      return "continue";
    }
    if (contentBytes.byteLength > MAX_FILE_SIZE_BYTES) {
      await this.#closeTerminal(event.eventId, "blocked_size", "blocked_size", correlationId);
      return "continue";
    }
    // Client re-fingerprint (spec 7.2, 10.2): stream only bytes that still
    // match the frozen digest and size; otherwise the successor event —
    // already recorded by capture — represents the newer bytes.
    const currentFingerprint = await deriveFrozenFingerprint(contentBytes);
    if (
      currentFingerprint.sha256 !== event.fingerprint.sha256 ||
      currentFingerprint.sizeBytes !== event.fingerprint.sizeBytes
    ) {
      await this.#closeTerminal(event.eventId, "integrity_failed", "integrity_failed", correlationId);
      return "continue";
    }

    const receipt = await this.#requestWithDeadline(
      () => this.#syncApi.uploadSmallFileContent({ operationId, contentBytes }),
      passDeadlineEpochMs,
      refreshBudget,
    );
    if (this.#isStopped) {
      return "end_stopped";
    }
    await this.#persistCommittedReceipt(event.eventId, receipt);
    return "continue";
  }

  // --- network seam -----------------------------------------------------------------------------------

  /**
   * Issue one request under the driver deadline: `requestUrl` cannot be
   * aborted, so the await races a timer and a late result after the timeout
   * (or after stop) is discarded rather than applied.
   */
  async #requestWithDeadline<T>(
    issue: () => Promise<T>,
    passDeadlineEpochMs: number,
    refreshBudget: RefreshBudget,
  ): Promise<T> {
    const timeoutMs = Math.max(
      1,
      Math.min(this.#requestTimeoutMs, passDeadlineEpochMs - this.#nowEpochMs()),
    );
    let firstAttempt: T;
    try {
      firstAttempt = await this.#raceTimeout(issue(), timeoutMs);
    } catch (error) {
      const kind = syncFailureKind(error);
      if (kind !== "access_expired" || refreshBudget.hasRefreshed) {
        throw error;
      }
      // One refresh maximum per pass (spec 8): rotate once, then retry the
      // same request a single time with the fresh credential.
      refreshBudget.hasRefreshed = true;
      try {
        await this.#refreshAccessToken();
      } catch {
        refreshBudget.requiresLogin = true;
        this.#recordCredentialRefreshFailureTrailEntry();
        throw error;
      }
      if (this.#isStopped) {
        throw error;
      }
      const retried = await this.#raceTimeout(issue(), timeoutMs);
      this.#sampleSuccessfulWireRequestId();
      return retried;
    }
    this.#sampleSuccessfulWireRequestId();
    return firstAttempt;
  }

  /**
   * Sample the envelope request id of the request outcome that just
   * settled successfully (sync error tracing task 1). The pass holds at
   * most one active request, so the accessor is unambiguous here; failure
   * outcomes carry their own id on the thrown error instead.
   */
  #sampleSuccessfulWireRequestId(): void {
    this.#lastPassWireRequestId = this.#syncApi.readLastEnvelopeRequestId();
  }

  #raceTimeout<T>(request: Promise<T>, timeoutMs: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      let hasSettled = false;
      const timeoutHandle = setTimeout(() => {
        if (hasSettled) {
          return;
        }
        hasSettled = true;
        reject(new SyncApiError("network_timeout"));
      }, timeoutMs);
      request.then(
        (value) => {
          if (hasSettled) {
            return;
          }
          hasSettled = true;
          clearTimeout(timeoutHandle);
          resolve(value);
        },
        (error) => {
          if (hasSettled) {
            return;
          }
          hasSettled = true;
          clearTimeout(timeoutHandle);
          reject(error);
        },
      );
    });
  }

  async #sendPreflight(event: JournalEvent): Promise<JournalPreflightOutcome> {
    const localFile = this.#requireLocalFile(event);
    // The wire operation derives from the CURRENT file mapping so a
    // predecessor commit turns a pending create into an honest update.
    const operation = localFile.sourceId === null ? "create" : "update";
    return this.#syncApi.preflightJournalEvent({
      eventId: event.eventId,
      idempotencyKey: event.idempotencyKey,
      operation,
      localFileId: event.localFileId,
      sourceId: operation === "update" ? localFile.sourceId : null,
      baseVersionId: operation === "update" ? localFile.baseVersionId : null,
      normalizedLocator: localFile.normalizedPath,
      fingerprint: event.fingerprint,
      policyRevisionNumber: localFile.policyRevisionNumber,
    });
  }

  #requireLocalFile(event: JournalEvent): LocalFile {
    const localFile = this.#repository.readLocalFileByLocalFileId(event.localFileId);
    if (localFile === null) {
      throw new SyncApiError("server_error");
    }
    return localFile;
  }

  #isPastDeadline(passDeadlineEpochMs: number): boolean {
    return this.#nowEpochMs() >= passDeadlineEpochMs;
  }

  // --- failure and receipt helpers ----------------------------------------------------------------------

  async #handleFailure(
    eventId: string,
    error: unknown,
    correlationId: string,
    refreshBudget: RefreshBudget,
  ): Promise<PassContinuation> {
    const kind = syncFailureKind(error);
    // Sync error tracing task 1 (comment corrected in the child six
    // remediation): every failed wire outcome that REACHES this failure
    // hook lands on the durable trail with its closed kind label (and the
    // failing envelope's canonical request id) before the ordinary
    // retry/terminal handling. Not every failed wire outcome reaches this
    // hook: an `access_expired` whose one refresh-budget retry then
    // succeeds is recovered inside `#requestWithDeadline` and never
    // surfaces as a failure, and an `operation_retry_required` whose
    // claimed-operation resume succeeds is settled by `#streamContent`
    // instead — neither records a `wire_failure` entry.
    this.#recordWireFailureTrailEntry(error);
    switch (kind) {
      case "access_expired":
      case "login_required":
        // Second 401 or a credential the pass may not use: retain the whole
        // queue untouched beyond the safe retry state (spec 8, 12).
        refreshBudget.requiresLogin = true;
        await this.#scheduleRetry(eventId, "login_required", correlationId);
        return "end_login_required";
      case "blocked_size":
        await this.#closeTerminal(eventId, "blocked_size", "blocked_size", correlationId);
        return "continue";
      case "blocked_conflict":
        // The server's typed, non-retryable business-conflict verdict (for
        // example the create-time `source_locator_conflict`): park the event
        // terminally so the queue moves on instead of retrying a verdict
        // that can never succeed.
        await this.#closeTerminal(eventId, "blocked_conflict", "blocked_conflict", correlationId);
        return "continue";
      case "integrity_failed":
        await this.#closeTerminal(eventId, "integrity_failed", "integrity_failed", correlationId);
        return "continue";
      case "network_offline":
      case "network_timeout":
      case "network_rate_limited":
      case "server_error":
      case "operation_retry_required":
      default: {
        // Retryable: keep the event with bounded jittered backoff and end
        // this pass — every later event would face the same condition. The
        // closed `retry_scheduled` outcome preserves that end reason even
        // when the deadline passes simultaneously, so no automatic
        // follow-up pass may send a later queued event meanwhile.
        const safeError = this.#safeRetryLabel(kind);
        await this.#scheduleRetry(eventId, safeError, correlationId);
        return "end_retry_scheduled";
      }
    }
  }

  #safeRetryLabel(kind: SyncApiFailureKind | null): JournalSafeErrorLabel {
    switch (kind) {
      case "network_offline":
        return "network_offline";
      case "network_timeout":
        return "network_timeout";
      case "network_rate_limited":
        return "network_rate_limited";
      default:
        // Server/operation failures and any unmapped local failure all keep
        // the event under the bounded retry of the spec-12 "keep event" row;
        // nothing is dropped and nothing retries blindly.
        return "server_error";
    }
  }

  async #scheduleRetry(
    eventId: string,
    safeError: JournalSafeErrorLabel,
    correlationId: string,
  ): Promise<void> {
    const event = this.#repository.readEvent(eventId);
    if (event === null) {
      return;
    }
    await this.#repository.recordEventAttempt({
      eventId,
      attemptedAtEpochMs: this.#nowEpochMs(),
      outcomeLabel: safeError,
      requestCorrelationId: correlationId,
    });
    const nextAttemptCount = event.attemptCount + 1;
    // Hoisted (not recomputed) so the park-failure recorder below can re-check
    // the exact epoch value the repository was given — evaluation order and
    // semantics are unchanged.
    const nextEligibleRetryEpochMs =
      this.#nowEpochMs() + computeRetryBackoffMs(nextAttemptCount, this.#randomJitter);
    try {
      await this.#repository.markEventWaitingRetry(eventId, safeError, nextEligibleRetryEpochMs);
    } catch (error) {
      // Park failure state capture (sync error tracing park diagnosis
      // round): the park is pure in-memory SQL, yet on the live machine it
      // throws the closed `journal_mutation_failed` reason while the offline
      // reproduction over the same journal bytes parks cleanly — so the
      // in-memory row state at the throw moment must differ. Capture it
      // BEFORE rethrowing; the rethrow itself is unchanged, so the pass
      // still fails closed through the pass-loop catch (ring entry and the
      // pre-existing journal_failure tap) with identical pass semantics.
      this.#recordParkFailureTrailEntry(eventId, safeError, nextEligibleRetryEpochMs, error);
      throw error;
    }
  }

  /**
   * Append one `journal_failure` trail entry from the retry-park throw site
   * (sync error tracing park diagnosis round): the park error's closed store
   * reason — or the closed `reason_unknown` token for a non-store error —
   * plus the closed state token of the row read back AT the failure moment,
   * where a null or throwing read-back records `row_absent`, plus (diagnostic
   * round U2) the closed throw-site token derived by re-checking the park
   * arguments the repository was given. Closed tokens only; fire-and-forget,
   * never blocking the pass.
   */
  #recordParkFailureTrailEntry(
    eventId: string,
    safeError: JournalSafeErrorLabel,
    nextEligibleRetryEpochMs: number,
    error: unknown,
  ): void {
    if (this.#diagnosticTrail === null) {
      return;
    }
    const reasonToken: SyncDiagnosticToken =
      error instanceof JournalStoreError ? error.reason : "reason_unknown";
    void this.#diagnosticTrail.append({
      kind: "journal_failure",
      tokens: [
        reasonToken,
        this.#readEventStateTokenAtFailure(eventId),
        parkFailureSiteToken(eventId, safeError, nextEligibleRetryEpochMs),
      ],
    });
  }

  /** The parked row's closed state token read back at the failure moment. */
  #readEventStateTokenAtFailure(eventId: string): SyncEventStateToken {
    try {
      const event = this.#repository.readEvent(eventId);
      if (event === null) {
        return "row_absent";
      }
      return journalEventStateToken(event.state);
    } catch {
      return "row_absent";
    }
  }

  async #closeTerminal(
    eventId: string,
    terminalState: "excluded_policy" | "blocked_size" | "blocked_conflict" | "deferred_lifecycle" | "integrity_failed",
    safeError: JournalSafeErrorLabel,
    correlationId: string,
  ): Promise<void> {
    await this.#repository.recordEventAttempt({
      eventId,
      attemptedAtEpochMs: this.#nowEpochMs(),
      outcomeLabel: safeError,
      requestCorrelationId: correlationId,
    });
    await this.#repository.markEventTerminal(eventId, terminalState, safeError);
  }

  async #persistCommittedReceipt(
    eventId: string,
    receipt: SmallFileTerminalReceipt,
  ): Promise<void> {
    await this.#repository.recordCommittedReceipt({
      eventId,
      sourceId: receipt.sourceId,
      baseVersionId: receipt.sourceVersionId,
    });
  }
}
