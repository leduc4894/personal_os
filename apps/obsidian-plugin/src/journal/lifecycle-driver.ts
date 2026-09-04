/**
 * The bounded foreground lifecycle source-event driver (Task 9, spec 19.2).
 *
 * The driver owns the rename / move / delete / restore dispatch lane
 * alongside the existing content queue. It runs only through an
 * explicit {@link LifecycleDriverImpl.runOne} call — the plugin
 * composition or the {@link JournalQueueDriver} pass triggers one
 * bounded run at a time. One call selects the oldest eligible
 * lifecycle event whose predecessor (when one is declared) is already
 * terminal-success, sends it through the generated API client and
 * persists the server result before acknowledging local completion.
 *
 * Persist-before-network (spec 7.2, 10.3): the durable event row,
 * the keyed operand row and the matching `local_files` update all
 * landed when the lifecycle capture recorded the event (spec 7.1 fix
 * round 1 I1). This driver never re-issues the network action on a
 * row that has not yet been durably recorded; the queue's exact
 * replay follows the same `(event_id, idempotency_key)` pair the
 * capture minted, and the server's `committedEvent` envelope returns
 * either the original receipt or reopens the flow (spec 10.3).
 *
 * Retry policy (spec 8, 12): the driver reuses the journal contract's
 * four permitted retry classes — offline, timeout, 429 and 5xx
 * transient — with a one-second-to-five-minute jittered exponential
 * backoff persisted on `journal_attempts.next_attempt_at`. Conflict
 * (409), integrity (422) and 5xx integrity outcomes are
 * non-retryable; they close the event as `blocked_conflict` or
 * `integrity_failed` — terminal conflict verdicts are reserved for
 * actual server-side conflict responses. The `login_required` failure
 * (including the pre-HTTP missing credential of a startup refresh
 * race) PARKS the event retryable as `waiting_retry` under the
 * `login_required` safe label — the same discipline as the content
 * lane — and reports `login_required` so the queue pass ends with the
 * credential need surfaced and the queue survives untouched for the
 * next login.
 *
 * Cancellation / unload: the driver owns one internal
 * {@link AbortController}; `dispose()` aborts it and every in-flight
 * `api.commit` is cancelled through the propagated signal. The
 * caller's `runOne(signal)` is combined with the disposal signal so
 * either side can cancel a run.
 *
 * Privacy (spec 9): the driver emits closed outcome tokens, opaque
 * correlation IDs and safe error labels only — no path, digest,
 * credential, locator text or provider detail reaches a thrown
 * error, a journal row or a diagnostic surface.
 */

import type { JournalSafeErrorLabel } from "./contracts";
import { LifecycleApiError, type LifecycleApiError as LifecycleApiErrorType } from "./lifecycle-api";
import type { LifecycleResult } from "./lifecycle-api";
import { LifecycleRepository, type FrozenLifecycleEvent } from "./lifecycle-repository";
import type { JournalRepository } from "./repository";
import type { SyncDiagnosticsTrail } from "./sync-diagnostics-trail";

// --- retry bounds (spec 8) --------------------------------------------------------------

/** The first retry delay after one failed attempt: one second. */
export const RETRY_BACKOFF_INITIAL_MS = 1_000;

/** The retry delay ceiling: five minutes. */
export const RETRY_BACKOFF_MAXIMUM_MS = 300_000;

/** The bounded jitter fraction added on top of the exponential delay. */
export const RETRY_BACKOFF_JITTER_FRACTION = 0.25;

/**
 * The exponential retry schedule of spec 8: `initial * 2^(attempt-1)`
 * capped at five minutes, plus a bounded jitter fraction of the
 * delay, capped again. The injected randomness keeps tests
 * deterministic.
 *
 * The result is rounded to a whole millisecond BEFORE the outer cap (the
 * sibling of the queue-lane fix): production runs this lane with the real
 * `Math.random` seam, whose untidy fractions make the jitter product a
 * float, and a fractional backoff would reach
 * `markEventWaitingRetry` as a non-integer `nextEligibleRetryEpochMs` —
 * rejected by its argument validation as `journal_mutation_failed`, so
 * no lifecycle retry park would ever land. `Math.min` applied after
 * `Math.round` keeps the rounded result from ever exceeding the
 * five-minute ceiling.
 */
export function computeLifecycleRetryBackoffMs(
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

// --- ports and outcomes ----------------------------------------------------------------

/**
 * The narrow transport port the driver consumes. The generated
 * `commitSourceLifecycleEvent` POST is the only method reachable from
 * the driver; success returns the typed receipt and every failure
 * surfaces as a thrown {@link LifecycleApiError} carrying one closed
 * safe failure kind.
 */
export interface LifecycleApi {
  commit(
    event: FrozenLifecycleEvent,
    signal: AbortSignal,
    tombstoneIdOverride?: string | null,
  ): Promise<LifecycleResult>;
}

/**
 * The closed bounded-pass outcome vocabulary. `login_required` marks a
 * run that PARKED its event retryable under the `login_required` safe
 * label: the queue pass must end with its own `login_required` outcome
 * so the credential need surfaces while the durable intent survives.
 */
export type LifecycleRunOutcome =
  | "idle"
  | "committed"
  | "blocked"
  | "retry"
  | "login_required";

/**
 * The lifecycle driver port the queue composition consumes. The
 * `dispose()` hook halts the driver so plugin unload or mobile
 * suspension can never leave an in-flight commit hanging.
 */
export interface LifecycleDriver {
  runOne(signal: AbortSignal): Promise<LifecycleRunOutcome>;
  dispose(): void;
}

export interface LifecycleDriverOptions {
  readonly repository: JournalRepository;
  readonly lifecycle: LifecycleRepository;
  readonly api: LifecycleApi;
  /** Opaque request correlation ID mint; defaults to `crypto.randomUUID`. */
  readonly createCorrelationId?: () => string;
  /** Random source for bounded retry jitter; defaults to `Math.random`. */
  readonly randomJitter?: () => number;
  /** Clock for deadlines, retries and attempt timestamps; defaults to `Date.now`. */
  readonly nowEpochMs?: () => number;
  /**
   * The optional durable diagnostics trail (trail v2 taxonomy, task 7).
   * The driver appends fire-and-forget and the trail never rejects, so a
   * pre-contact credential absence is observed without ever blocking or
   * breaking the dispatch lane.
   */
  readonly diagnosticTrail?: SyncDiagnosticsTrail | undefined;
}

// --- the driver --------------------------------------------------------------------------

/**
 * The bounded foreground lifecycle source-event driver. Holds one
 * internal `AbortController` for the disposal signal; the caller's
 * `runOne(signal)` is combined with the disposal signal via
 * `AbortSignal.any` so either side cancels a run.
 */
export class LifecycleDriverImpl implements LifecycleDriver {
  readonly #repository: JournalRepository;
  readonly #lifecycle: LifecycleRepository;
  readonly #api: LifecycleApi;
  readonly #createCorrelationId: () => string;
  readonly #randomJitter: () => number;
  readonly #nowEpochMs: () => number;
  readonly #diagnosticTrail: SyncDiagnosticsTrail | null;
  readonly #disposeController: AbortController;
  #isDisposed = false;

  constructor(options: LifecycleDriverOptions) {
    this.#repository = options.repository;
    this.#lifecycle = options.lifecycle;
    this.#api = options.api;
    this.#createCorrelationId = options.createCorrelationId ?? (() => crypto.randomUUID());
    this.#randomJitter = options.randomJitter ?? (() => Math.random());
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    this.#diagnosticTrail = options.diagnosticTrail ?? null;
    this.#disposeController = new AbortController();
  }

  /** Whether the driver was disposed for unload / suspension. */
  get isDisposed(): boolean {
    return this.#isDisposed;
  }

  /**
   * Halt the driver: every subsequent {@link runOne} returns `"idle"`
   * and any in-flight commit is aborted via the combined
   * `AbortSignal`.
   */
  dispose(): void {
    if (this.#isDisposed) {
      return;
    }
    this.#isDisposed = true;
    this.#disposeController.abort();
  }

  /**
   * Run one bounded pass over the lifecycle lane: select the oldest
   * eligible event, send it through the generated API client,
   * persist the server result, then return the closed outcome. The
   * call never throws; every thrown `LifecycleApiError` is mapped
   * onto the closed `{ outcome, label }` vocabulary so the queue
   * composition can keep moving.
   */
  async runOne(signal: AbortSignal): Promise<LifecycleRunOutcome> {
    if (this.#isDisposed) {
      return "idle";
    }
    const combinedSignal = combineSignals(signal, this.#disposeController.signal);
    if (combinedSignal.aborted) {
      return "idle";
    }

    const frozen = this.#lifecycle.readOldestEligibleLifecycleEvent(this.#nowEpochMs());
    if (frozen === null) {
      return "idle";
    }

    if (combinedSignal.aborted) {
      return "idle";
    }

    // The restore succession MUST source the tombstone id exclusively
    // from the committed delete predecessor's server receipt (task 9
    // fix round 1 I1). The local operands row may have staged a
    // different id at capture time; the persisted server receipt is
    // the only authority over the tombstone domain, so any override
    // beats the operands-derived value.
    const tombstoneIdOverride = this.#resolveRestoreTombstoneOverride(frozen);

    const correlationId = this.#createCorrelationId();
    let result: LifecycleResult;
    try {
      result = await this.#api.commit(frozen, combinedSignal, tombstoneIdOverride);
    } catch (error) {
      if (this.#isDisposed || combinedSignal.aborted) {
        return "idle";
      }
      return this.#mapApiError(frozen, error, correlationId);
    }

    if (this.#isDisposed || combinedSignal.aborted) {
      // The server returned but we were cancelled in flight: discard
      // the late result rather than apply it to the journal.
      return "idle";
    }

    // Persist the server result BEFORE acknowledging local completion.
    await this.#repository.recordEventAttempt({
      eventId: frozen.event.eventId,
      attemptedAtEpochMs: this.#nowEpochMs(),
      outcomeLabel: "committed",
      requestCorrelationId: correlationId,
    });
    const serverReceipt = result.tombstoneId === null ? null : { tombstoneId: result.tombstoneId };
    await this.#lifecycle.recordLifecycleCommittedReceipt(frozen.event.eventId, serverReceipt);
    // A committed restore successor consumes the retained tombstone so
    // the file returns to the active content surface (spec 7.1 fix
    // round 1 C2).
    if (frozen.operands.operation === "restore") {
      await this.#lifecycle.consumeRestoreSuccessor(frozen.event.localFileId);
    }
    return "committed";
  }

  /**
   * Resolve the tombstone id the wire body must carry for one
   * lifecycle event. A restore event with a server-receipt-bearing
   * delete predecessor MUST send the server-confirmed id; any other
   * operation (or a restore whose predecessor has no persisted server
   * receipt yet) falls through to the operands-derived value.
   */
  #resolveRestoreTombstoneOverride(frozen: FrozenLifecycleEvent): string | null | undefined {
    if (frozen.operands.operation !== "restore") {
      return undefined;
    }
    const predecessorId = frozen.operands.predecessorEventId;
    if (predecessorId === null) {
      return undefined;
    }
    const predecessorReceipt = this.#lifecycle.readServerReceiptTombstoneId(predecessorId);
    if (predecessorReceipt === null) {
      return undefined;
    }
    return predecessorReceipt;
  }

  // --- error mapping --------------------------------------------------------------------

  /**
   * Append ONE `credential_failure` trail entry when the login rejection
   * happened BEFORE any transport contact — the adapter's marked
   * missing-credential throw (trail v2 taxonomy, task 7). A
   * server-answerable 401/403 records nothing here: contact happened, so
   * the failure belongs to the wire taxonomy of the lanes that observed
   * it. Fire-and-forget, never blocking the dispatch.
   */
  #recordCredentialAbsenceTrailEntry(apiError: LifecycleApiErrorType): void {
    if (this.#diagnosticTrail === null || !apiError.isCredentialAbsent) {
      return;
    }
    void this.#diagnosticTrail.append({
      kind: "credential_failure",
      tokens: ["access_missing", "login_required"],
    });
  }

  async #mapApiError(
    frozen: FrozenLifecycleEvent,
    error: unknown,
    correlationId: string,
  ): Promise<LifecycleRunOutcome> {
    if (!(error instanceof LifecycleApiError)) {
      return "retry";
    }
    const apiError: LifecycleApiErrorType = error;
    switch (apiError.kind) {
      case "conflict":
        await this.#closeTerminal(frozen.event.eventId, "blocked_conflict", correlationId);
        return "blocked";
      case "integrity":
      case "integrity_5xx":
        // 422 (integrity) and 5xx with an integrity code (integrity_5xx)
        // share the same non-retryable outcome: the durable journal cannot
        // safely retry. The spec-19.2 row-7 invariant is the same
        // regardless of the status code that announced it (task 9 fix
        // round 1 I2).
        await this.#closeTerminal(frozen.event.eventId, "integrity_failed", correlationId);
        return "blocked";
      case "login_required":
        // Login is required (including the pre-HTTP missing credential
        // of a startup refresh race): PARK the event retryable under the
        // `login_required` safe label — the same discipline as the
        // content lane — and report the closed `login_required` outcome
        // so the queue pass ends login_required. A terminal
        // `blocked_conflict` verdict is reserved for actual server-side
        // conflict responses and must never destroy a durable lifecycle
        // intent without any server contact.
        this.#recordCredentialAbsenceTrailEntry(apiError);
        await this.#scheduleRetry(frozen.event.eventId, "login_required", correlationId);
        return "login_required";
      case "network_offline":
      case "network_timeout":
      case "network_rate_limited":
      case "server_error":
        await this.#scheduleRetry(frozen.event.eventId, retryLabelForKind(apiError.kind), correlationId);
        return "retry";
      default: {
        // Exhaustiveness check: every closed kind is handled.
        const _exhaustive: never = apiError.kind;
        void _exhaustive;
        return "retry";
      }
    }
  }

  async #closeTerminal(
    eventId: string,
    terminalState: "blocked_conflict" | "integrity_failed",
    correlationId: string,
  ): Promise<void> {
    let resolution: "no_intent" | "intent_reconciled";
    try {
      resolution = await this.#lifecycle.resolveIntentAwareLifecycleTerminal({
        eventId,
        terminalState,
        attemptedAtEpochMs: this.#nowEpochMs(),
        requestCorrelationId: correlationId,
      });
    } catch (error) {
      void this.#diagnosticTrail?.append({
        kind: "journal_failure",
        tokens: ["lifecycle_reconcile_persist_failed"],
      });
      throw error;
    }
    if (resolution === "intent_reconciled") {
      void this.#diagnosticTrail?.append({
        kind: "journal_failure",
        tokens: ["pending_rename_intent_lifecycle_rejected"],
      });
      await this.#startRepairBarrier();
    }
  }

  /** Start or retain repair after the lifecycle resolver transfers locator ownership. */
  async #startRepairBarrier(): Promise<void> {
    try {
      const generation = await this.#repository.deviceSync.nextObservationGeneration();
      await this.#repository.deviceSync.startRepairBarrier({
        generation,
        reason: "device_manifest_target_occupied",
      });
    } catch {
      // An already-active barrier already carries the required repair obligation.
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
      this.#nowEpochMs() + computeLifecycleRetryBackoffMs(nextAttemptCount, this.#randomJitter),
    );
  }
}

// --- helpers -----------------------------------------------------------------------------

function retryLabelForKind(kind: LifecycleApiError["kind"]): JournalSafeErrorLabel {
  switch (kind) {
    case "network_offline":
      return "network_offline";
    case "network_timeout":
      return "network_timeout";
    case "network_rate_limited":
      return "network_rate_limited";
    case "server_error":
      return "server_error";
    // The integrity outcomes are non-retryable; the driver never
    // calls `retryLabelForKind` for them. The exhaustive `default`
    // branch keeps the closed-set pinning even if a future kind is
    // added without label coverage.
    case "integrity":
    case "integrity_5xx":
    case "conflict":
    case "login_required":
    default:
      return "server_error";
  }
}

/**
 * Combine one external signal with the driver's disposal signal so
 * either side cuts off the in-flight HTTP request. `AbortSignal.any`
 * is the standard ES2022 seam.
 */
function combineSignals(a: AbortSignal, b: AbortSignal): AbortSignal {
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any([a, b]);
  }
  // Manual fallback (no native AbortSignal.any): wrap the two
  // signals into one that aborts when either aborts. The driver
  // passes this combined signal to the API call.
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (a.aborted || b.aborted) {
    controller.abort();
    return controller.signal;
  }
  a.addEventListener("abort", onAbort, { once: true });
  b.addEventListener("abort", onAbort, { once: true });
  return controller.signal;
}
