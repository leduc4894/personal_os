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
 * login. `excluded_policy`, `blocked_size`, `blocked_conflict`,
 * `deferred_lifecycle` and `integrity_failed` never retry automatically.
 *
 * Privacy (spec 9): the driver emits closed outcome labels and opaque
 * correlation IDs only — no path, digest, credential or provider detail
 * reaches a thrown error, a journal row or a diagnostic surface.
 */

import type { JournalEvent, JournalSafeErrorLabel, LocalFile } from "./contracts";
import { MAX_FILE_SIZE_BYTES } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import type { LifecycleDriver } from "./lifecycle-driver";
import type { JournalRepository } from "./repository";
import type { JournalPreflightOutcome, JournalSyncApi, SmallFileTerminalReceipt } from "./sync-api";
import { SyncApiError } from "./sync-api";

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
  return Math.min(RETRY_BACKOFF_MAXIMUM_MS, exponentialDelayMs + jitterMs);
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

/** One bounded pass refresh outcome; closed vocabulary. */
export type QueuePassOutcome = "completed" | "stopped" | "login_required" | "pass_already_running";

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
}

type PassContinuation = "continue" | "end_pass";

/** The per-pass credential budget: at most one refresh, one login verdict. */
interface RefreshBudget {
  hasRefreshed: boolean;
  requiresLogin: boolean;
}

/** Extract the closed failure kind of a thrown sync error, if any. */
function syncFailureKind(error: unknown): string | null {
  const kind = (error as { kind?: unknown } | null)?.kind;
  return typeof kind === "string" ? kind : null;
}

function canResumeClaimedOperation(error: unknown): boolean {
  return (error as { canResumeClaimedOperation?: unknown } | null)?.canResumeClaimedOperation === true;
}

// --- the driver ----------------------------------------------------------------------------------------

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
  #isStopped = false;
  #isPassRunning = false;

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
  }

  /** Whether the driver was stopped for unload/suspension and runs nothing new. */
  get isStopped(): boolean {
    return this.#isStopped;
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
   * required, or the driver stops. When a lifecycle driver is wired in,
   * the pass interleaves: it first drains one ready lifecycle event
   * (whose predecessor, when declared, is terminal-success), then
   * processes one content event. The two lanes never have an active
   * mutating request in flight at the same time.
   */
  async runPass(): Promise<QueuePassSummary> {
    if (this.#isStopped) {
      return { outcome: "stopped", processedEventCount: 0 };
    }
    if (this.#isPassRunning) {
      return { outcome: "pass_already_running", processedEventCount: 0 };
    }
    this.#isPassRunning = true;
    const passDeadlineEpochMs = this.#nowEpochMs() + this.#passDeadlineMs;
    const refreshBudget: RefreshBudget = { hasRefreshed: false, requiresLogin: false };
    let processedEventCount = 0;
    let passOutcome: QueuePassOutcome = "completed";
    try {
      while (!this.#isStopped && this.#nowEpochMs() < passDeadlineEpochMs) {
        // Drain one lifecycle event first so a content event whose
        // predecessor is a queued lifecycle operation does not dispatch
        // before the predecessor commits.
        if (this.#lifecycleDriver !== null && !this.#isStopped) {
          try {
            await this.#lifecycleDriver.runOne(this.#passAbortController.signal);
          } catch {
            // The lifecycle driver swallows its own errors and returns
            // closed outcomes; a thrown error here is fail-closed.
          }
        }
        let continuation: PassContinuation;
        try {
          const event = this.#repository.readOldestEligibleEvent(this.#nowEpochMs());
          if (event === null) {
            break;
          }
          continuation = await this.#processEvent(event, passDeadlineEpochMs, refreshBudget);
        } catch {
          // A journal failure mid-pass fails closed: the pass ends with the
          // journal as the durable truth and never crashes its trigger.
          break;
        }
        processedEventCount += 1;
        if (continuation === "end_pass") {
          passOutcome = refreshBudget.requiresLogin ? "login_required" : "completed";
          break;
        }
      }
      if (this.#isStopped) {
        passOutcome = "stopped";
      }
      return { outcome: passOutcome, processedEventCount };
    } finally {
      this.#isPassRunning = false;
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
        return "end_pass";
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
        return "end_pass";
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
            return "end_pass";
          }
          failure = resumeError;
        }
      }
      const continuation = await this.#handleFailure(event.eventId, failure, correlationId, refreshBudget);
      return continuation;
    }
  }

  #claimedResumeOperationId(event: JournalEvent, error: unknown): string | null {
    if (
      syncFailureKind(error) !== "operation_retry_required" ||
      !canResumeClaimedOperation(error) ||
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
    if (this.#isStopped || this.#isPastDeadline(passDeadlineEpochMs)) {
      return "end_pass";
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
      return "end_pass";
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
        throw error;
      }
      if (this.#isStopped) {
        throw error;
      }
      return await this.#raceTimeout(issue(), timeoutMs);
    }
    return firstAttempt;
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
    switch (kind) {
      case "access_expired":
      case "login_required":
        // Second 401 or a credential the pass may not use: retain the whole
        // queue untouched beyond the safe retry state (spec 8, 12).
        refreshBudget.requiresLogin = true;
        await this.#scheduleRetry(eventId, "login_required", correlationId);
        return "end_pass";
      case "blocked_size":
        await this.#closeTerminal(eventId, "blocked_size", "blocked_size", correlationId);
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
        // this pass — every later event would face the same condition.
        const safeError = this.#safeRetryLabel(kind);
        await this.#scheduleRetry(eventId, safeError, correlationId);
        return "end_pass";
      }
    }
  }

  #safeRetryLabel(kind: string | null): JournalSafeErrorLabel {
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
    await this.#repository.markEventWaitingRetry(
      eventId,
      safeError,
      this.#nowEpochMs() + computeRetryBackoffMs(nextAttemptCount, this.#randomJitter),
    );
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
