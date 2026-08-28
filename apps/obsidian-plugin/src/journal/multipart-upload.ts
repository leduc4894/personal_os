/**
 * The Mobile-safe multipart upload runner (child 7 spec 4.3, 6.2, 8).
 *
 * One `run` resumes or opens the server-owned multipart session of one
 * frozen outbound journal event and drives only its unfinished parts to
 * completion. The client state machine is resumption-first: whenever
 * durable safe progress exists, the FIRST network call is session status —
 * never a part URL — so the provider-reconciled completed parts are known
 * before any new authorization is requested; a fresh event creates (or
 * exactly replays) its session first and persists the safe record before
 * the first byte moves.
 *
 * Mobile safety (spec 2.2, 4): no background execution is assumed. The
 * runner reopens the Vault file and re-checks the frozen fingerprint
 * before EVERY unfinished range and once more before requesting
 * completion, so a suspension between parts never transmits a stream an
 * HTTP runtime no longer preserves, and bytes of two file generations are
 * never mixed. Each part requests exactly ONE short-lived presigned URL,
 * consumes it with exactly one PUT and immediately discards the response
 * object; a rejected URL reconciles through status first, then exactly one
 * replacement URL. The platform enters only through the part-PUT
 * semaphore (three Desktop, two Mobile) and the suspend behavior.
 *
 * Interruption semantics (spec 8): suspend, timeout and offline persist
 * the safe progress that landed and rethrow the EXISTING retryable closed
 * failure so the queue's bounded backoff owns every retry decision. A
 * changed local file keeps the already observed progress under the closed
 * `multipart_local_content_changed` token, requests the exact abort when
 * online and reports the change so the queue terminalizes the OLD event —
 * the newer watcher event is its own journal row and never coalesces.
 *
 * Privacy (spec 5, 7): no presigned URL, query signature, provider
 * identity, ETag, staging key, digest or path is ever persisted, retained
 * after its single PUT, logged or carried on a thrown error.
 */

import type {
  JournalEvent,
  LocalFile,
  MultipartProgressRecord,
  MultipartSafeReasonToken,
  MultipartSessionState,
} from "./contracts";
import { MULTIPART_PART_SIZE_BYTES } from "./contracts";
import { deriveFrozenFingerprint } from "./fingerprint";
import type { QueueVaultFileReader } from "./queue-driver";
import type {
  JournalEventPreflightInput,
  JournalSyncApi,
  MultipartPartUrlAuthorization,
  MultipartSessionPlan,
  MultipartTerminalResult,
  SmallFileTerminalReceipt,
} from "./sync-api";
import { SyncApiError } from "./sync-api";

// --- platform concurrency (child 7 spec 4) --------------------------------------------------------

/** The platform classes the runner discriminates — nothing else reads `platform`. */
export type MultipartUploadPlatform = "desktop" | "mobile";

/** Desktop part-PUT concurrency: at most three parts (child 7 spec 4). */
export const MULTIPART_DESKTOP_PART_CONCURRENCY = 3;

/** Mobile part-PUT concurrency: at most two parts (child 7 spec 4). */
export const MULTIPART_MOBILE_PART_CONCURRENCY = 2;

/** The closed part-PUT permit count of one platform class. */
export function multipartPartConcurrency(platform: MultipartUploadPlatform): number {
  return platform === "mobile"
    ? MULTIPART_MOBILE_PART_CONCURRENCY
    : MULTIPART_DESKTOP_PART_CONCURRENCY;
}

/**
 * A minimal counting semaphore over the part PUTs: at most `limit` parts
 * hold one issued URL and one in-flight PUT at a time. FIFO wakeups keep
 * the unfinished parts ascending under contention.
 */
class PartPutSemaphore {
  readonly #limit: number;
  #activeCount = 0;
  readonly #waiters: (() => void)[] = [];

  constructor(limit: number) {
    this.#limit = limit;
  }

  async acquire(): Promise<void> {
    if (this.#activeCount >= this.#limit) {
      await new Promise<void>((resolve) => {
        this.#waiters.push(resolve);
      });
    }
    this.#activeCount += 1;
  }

  release(): void {
    this.#activeCount -= 1;
    this.#waiters.shift()?.();
  }
}

// --- ports, outcomes and run context ----------------------------------------------------------------

/**
 * The narrow repository port the runner needs: the durable SAFE progress
 * store of task 9 plus the tracked-file mapping. The runner performs NO
 * event-state transitions of its own — the queue driver owns every
 * terminal, retry and receipt mutation.
 */
export interface MultipartUploadRepositoryPort {
  readMultipartProgress(eventId: string): MultipartProgressRecord | null;
  saveMultipartProgress(record: MultipartProgressRecord): Promise<void>;
  clearMultipartProgress(eventId: string): Promise<void>;
  readLocalFileByLocalFileId(localFileId: string): LocalFile | null;
}

/**
 * The closed result of one runner pass: a frozen terminal receipt
 * (`committed` or `no_change`), the local verdicts that end the event
 * without any receipt (`local_content_changed`, `local_file_missing`), or
 * a continuable pass boundary. Every interruptive condition is a THROWN
 * retryable closed failure instead — the queue's existing failure matrix
 * owns it.
 */
export type MultipartUploadRunOutcome =
  | { readonly outcome: "committed"; readonly receipt: SmallFileTerminalReceipt }
  | { readonly outcome: "no_change"; readonly receipt: SmallFileTerminalReceipt }
  | { readonly outcome: "local_content_changed" }
  | { readonly outcome: "local_file_missing" }
  | { readonly outcome: "pass_deadline_reached" };

/**
 * The per-run context the queue driver injects: the pass's suspend signal
 * (aborted on unload/Mobile suspension) and the pass deadline epoch. Both
 * are optional so unit runs stay self-contained.
 */
export interface MultipartUploadRunContext {
  readonly signal?: AbortSignal | undefined;
  readonly passDeadlineEpochMs?: number | undefined;
}

export interface MultipartUploadRunnerOptions {
  readonly repository: MultipartUploadRepositoryPort;
  readonly syncApi: JournalSyncApi;
  readonly fileBytesReader: QueueVaultFileReader;
  readonly nowEpochMs: () => number;
  /** One transport request may hold at most this long; late results are discarded. */
  readonly requestTimeoutMs: number;
}

/** The verdict of one frozen-file check: usable bytes, a changed file or a vanished file. */
type FrozenFileCheck =
  | { readonly kind: "bytes"; readonly bytes: Uint8Array }
  | { readonly kind: "changed" }
  | { readonly kind: "missing" };

/** The mutable run-scoped stop state every part worker observes. */
interface RunStopState {
  hasContentChanged: boolean;
  deadlineReached: boolean;
  firstFailure: unknown;
}

/**
 * The server registry codes whose mapped failures mean the persisted
 * session can never continue: the runner clears its durable progress so
 * the queue's retry re-preflights the same frozen event and the server's
 * exact replay reopens one fresh session (child 7 spec 8, session-expiry
 * row).
 */
const SESSION_GONE_WIRE_CODES: ReadonlySet<string> = new Set([
  "multipart_session_not_found",
  "multipart_session_expired",
  "multipart_session_state_invalid",
]);

/** Race one request against the per-request timeout; a late result is discarded. */
function raceRequestTimeout<T>(request: Promise<T>, timeoutMs: number): Promise<T> {
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

// --- the runner ----------------------------------------------------------------------------------------

/**
 * The foreground multipart transfer runner of one frozen event. It is
 * never a daemon: one `run` ends at completion, a local verdict, a
 * continuable pass boundary, or a thrown retryable closed failure whose
 * bounded backoff the queue driver owns. The journal stays the only
 * durable truth; the safe progress store is the resume evidence.
 */
export class MultipartUploadRunner {
  readonly #repository: MultipartUploadRepositoryPort;
  readonly #syncApi: JournalSyncApi;
  readonly #fileBytesReader: QueueVaultFileReader;
  readonly #nowEpochMs: () => number;
  readonly #requestTimeoutMs: number;

  constructor(options: MultipartUploadRunnerOptions) {
    this.#repository = options.repository;
    this.#syncApi = options.syncApi;
    this.#fileBytesReader = options.fileBytesReader;
    this.#nowEpochMs = options.nowEpochMs;
    this.#requestTimeoutMs = options.requestTimeoutMs;
  }

  /**
   * Resume or open the frozen event's multipart session and drive only its
   * unfinished parts. `platform` selects the part-PUT semaphore limit and
   * nothing else.
   */
  async run(
    event: JournalEvent,
    platform: MultipartUploadPlatform,
    context: MultipartUploadRunContext = {},
  ): Promise<MultipartUploadRunOutcome> {
    try {
      return await this.#run(event, platform, context);
    } catch (error) {
      // A session-gone registry verdict (spec 8) means the persisted
      // session is dead: clear its safe progress so the queue's retry
      // re-preflights the same frozen event instead of resuming a session
      // that can never answer again. The cleanup is best effort — a store
      // failure never masks the original closed verdict.
      if (
        error instanceof SyncApiError &&
        error.wireErrorCode !== null &&
        SESSION_GONE_WIRE_CODES.has(error.wireErrorCode)
      ) {
        await this.#repository.clearMultipartProgress(event.eventId).catch(() => undefined);
      }
      throw error;
    }
  }

  async #run(
    event: JournalEvent,
    platform: MultipartUploadPlatform,
    context: MultipartUploadRunContext,
  ): Promise<MultipartUploadRunOutcome> {
    this.#throwIfSuspended(context.signal);
    const localFile = this.#repository.readLocalFileByLocalFileId(event.localFileId);
    if (localFile === null) {
      return { outcome: "local_file_missing" };
    }

    const persisted = this.#repository.readMultipartProgress(event.eventId);
    const stopState: RunStopState = {
      hasContentChanged: false,
      deadlineReached: false,
      firstFailure: null,
    };
    const completedPartNumbers = new Set<number>();
    let plan: MultipartSessionPlan;
    let safeReason: MultipartSafeReasonToken | null;

    if (persisted === null) {
      // Fresh frozen event: create (or exactly replay) the one bound
      // session, then persist the safe record BEFORE the first byte moves.
      plan = await this.#request(() => this.#syncApi.createMultipartUploadSession(
        this.#createSessionInput(event, localFile),
      ));
      safeReason = null;
      await this.#persistProgress(event, plan, completedPartNumbers, "created", safeReason);
    } else {
      // Resume: status FIRST, before any part URL (child 7 spec 6.2, 8) —
      // the provider-reconciled completed parts are known before any new
      // authorization is requested.
      const status = await this.#request(() =>
        this.#syncApi.getMultipartUploadSession(persisted.sessionId),
      );
      if (
        status.sessionId !== persisted.sessionId ||
        status.partCount !== persisted.partCount ||
        status.partSizeBytes !== persisted.partSizeBytes
      ) {
        throw new SyncApiError("server_error");
      }
      if (status.terminalResult !== null) {
        // A finished session replays its frozen terminal result (spec 6.3):
        // no part URL, no second completion.
        return terminalOutcomeOf(status.terminalResult);
      }
      await this.#applySessionVerdict(event.eventId, status.state);
      plan = status;
      safeReason = persisted.safeReason;
      for (const partNumber of persisted.completedPartNumbers) {
        completedPartNumbers.add(partNumber);
      }
      for (const partNumber of status.completedPartNumbers) {
        completedPartNumbers.add(partNumber);
      }
      await this.#persistProgress(event, plan, completedPartNumbers, status.state, safeReason);
    }

    // Drive every unfinished range under the platform semaphore; each part
    // reopens and re-checks the frozen file first (spec 4.3).
    const unfinishedPartNumbers: number[] = [];
    for (let partNumber = 1; partNumber <= plan.partCount; partNumber += 1) {
      if (!completedPartNumbers.has(partNumber)) {
        unfinishedPartNumbers.push(partNumber);
      }
    }
    if (unfinishedPartNumbers.length > 0) {
      const semaphore = new PartPutSemaphore(multipartPartConcurrency(platform));
      const workers = unfinishedPartNumbers.map((partNumber) =>
        this.#uploadOnePart({
          event,
          localFile,
          plan,
          partNumber,
          semaphore,
          stopState,
          completedPartNumbers,
          safeReasonRef: { current: safeReason },
          context,
        }),
      );
      await Promise.allSettled(workers);
      if (stopState.hasContentChanged) {
        return await this.#stopForLocalContentChange(event, plan, completedPartNumbers);
      }
      if (stopState.firstFailure !== null) {
        throw stopState.firstFailure;
      }
      this.#throwIfSuspended(context.signal);
      if (this.#isPastDeadline(context.passDeadlineEpochMs)) {
        stopState.deadlineReached = true;
        return { outcome: "pass_deadline_reached" };
      }
    }

    // Before requesting completion the frozen file is compared one final
    // time (spec 4.3); a changed file ends the session exactly like a
    // mid-part change.
    this.#throwIfSuspended(context.signal);
    const completionCheck = await this.#checkFrozenFile(event, localFile);
    if (completionCheck.kind === "changed") {
      return await this.#stopForLocalContentChange(event, plan, completedPartNumbers);
    }
    if (completionCheck.kind === "missing") {
      return { outcome: "local_file_missing" };
    }

    // Persist the completion intent before the network action it guards,
    // then claim the completion (spec 4.3, 6.3).
    await this.#persistProgress(event, plan, completedPartNumbers, "completing", null);
    const completion = await this.#request(() =>
      this.#syncApi.completeMultipartUploadSession(plan.sessionId),
    );
    if (completion.state === "committed") {
      if (completion.terminalReceipt === null) {
        throw new SyncApiError("server_error");
      }
      return terminalOutcomeOf(completion.terminalReceipt);
    }
    // A pending claim (`completing`/`verifying`/`promoting`) replays
    // through status on the next pass; every other state resolves through
    // the closed session verdict table.
    await this.#applySessionVerdict(event.eventId, completion.state);
    throw new SyncApiError("server_error");
  }

  // --- one part -----------------------------------------------------------------------------------

  /**
   * Upload exactly one unfinished part: re-check the frozen file, then
   * under the semaphore request ONE URL, PUT the exact derived range once
   * and immediately discard the response object. A rejected URL
   * reconciles through status and exactly one replacement URL (spec 6.2).
   * The worker never rejects: the first failure lands in the shared stop
   * state and ends the run after every sibling joined.
   */
  async #uploadOnePart(input: {
    readonly event: JournalEvent;
    readonly localFile: LocalFile;
    readonly plan: MultipartSessionPlan;
    readonly partNumber: number;
    readonly semaphore: PartPutSemaphore;
    readonly stopState: RunStopState;
    readonly completedPartNumbers: Set<number>;
    readonly safeReasonRef: { current: MultipartSafeReasonToken | null };
    readonly context: MultipartUploadRunContext;
  }): Promise<void> {
    const { event, localFile, plan, partNumber, semaphore, stopState, completedPartNumbers, safeReasonRef, context } =
      input;
    try {
      if (this.#isRunStopped(stopState, context)) {
        return;
      }
      // Open and check the frozen local file before this unfinished range
      // (spec 4.3): a Mobile suspension may have invalidated any stream
      // an earlier open preserved.
      const check = await this.#checkFrozenFile(event, localFile);
      if (check.kind === "changed") {
        stopState.hasContentChanged = true;
        return;
      }
      if (check.kind === "missing") {
        throw new SyncApiError("server_error");
      }
      const fileBytes = check.bytes;

      await semaphore.acquire();
      try {
        if (this.#isRunStopped(stopState, context)) {
          return;
        }
        if (this.#isPastDeadline(context.passDeadlineEpochMs)) {
          stopState.deadlineReached = true;
          return;
        }
        const authorization = await this.#request(() =>
          this.#syncApi.issueMultipartPartUrl({
            sessionId: plan.sessionId,
            partNumber,
          }),
        );
        this.#validatePartRange(authorization, partNumber, fileBytes.byteLength);
        const firstPut = await this.#request(() =>
          this.#syncApi.putMultipartPartBytes({
            url: authorization.url,
            contentBytes: fileBytes.subarray(
              authorization.offsetBytes,
              authorization.offsetBytes + authorization.sizeBytes,
            ),
          }),
        );
        if (firstPut === "uploaded") {
          await this.#recordPartCompletion(event, plan, completedPartNumbers, partNumber, safeReasonRef.current);
          return;
        }
        // The URL was rejected: status FIRST to reconcile what the provider
        // actually observed, then exactly ONE replacement URL (spec 6.2).
        const status = await this.#request(() =>
          this.#syncApi.getMultipartUploadSession(plan.sessionId),
        );
        for (const observedPartNumber of status.completedPartNumbers) {
          completedPartNumbers.add(observedPartNumber);
        }
        await this.#persistProgress(
          event,
          plan,
          completedPartNumbers,
          status.state,
          safeReasonRef.current,
        );
        if (status.terminalResult !== null) {
          // The provider proved every part while the URL was stale: the
          // run continues into the completion step with the reconciled set.
          return;
        }
        if (completedPartNumbers.has(partNumber)) {
          return;
        }
        const replacement = await this.#request(() =>
          this.#syncApi.issueMultipartPartUrl({
            sessionId: plan.sessionId,
            partNumber,
          }),
        );
        this.#validatePartRange(replacement, partNumber, fileBytes.byteLength);
        const secondPut = await this.#request(() =>
          this.#syncApi.putMultipartPartBytes({
            url: replacement.url,
            contentBytes: fileBytes.subarray(
              replacement.offsetBytes,
              replacement.offsetBytes + replacement.sizeBytes,
            ),
          }),
        );
        if (secondPut === "url_rejected") {
          safeReasonRef.current = "multipart_part_url_rejected";
          await this.#persistProgress(
            event,
            plan,
            completedPartNumbers,
            status.state,
            "multipart_part_url_rejected",
          );
          throw new SyncApiError("server_error");
        }
        await this.#recordPartCompletion(
          event,
          plan,
          completedPartNumbers,
          partNumber,
          safeReasonRef.current,
        );
      } finally {
        semaphore.release();
      }
    } catch (error) {
      if (stopState.firstFailure === null) {
        stopState.firstFailure = error;
      }
    }
  }

  // --- verdicts and progress ----------------------------------------------------------------------

  /**
   * The closed verdict of one observed session state: active states
   * return; a pending completion claim is the retryable
   * `multipart_completion_in_progress` replay; integrity and policy
   * verdicts map onto their terminal kinds; and a session that can never
   * accept work again clears its durable progress so the queue's retry
   * re-preflights the frozen event (spec 4.2, 8).
   */
  async #applySessionVerdict(eventId: string, state: MultipartSessionState): Promise<void> {
    switch (state) {
      case "created":
      case "uploading":
        return;
      case "completing":
      case "verifying":
      case "promoting":
        throw new SyncApiError("operation_retry_required");
      case "integrity_failed":
        throw new SyncApiError("integrity_failed");
      case "policy_denied":
        throw new SyncApiError("policy_denied");
      case "expired":
      case "cancelling":
      case "cleanup_pending":
      case "cleaned":
        await this.#repository.clearMultipartProgress(eventId);
        throw new SyncApiError("operation_retry_required");
      case "committed":
        // A committed session always carries its frozen terminal result
        // (handled before this verdict); reaching here means malformed
        // wire data.
        throw new SyncApiError("server_error");
    }
  }

  /**
   * Stop the changed session (spec 4.3, 8): keep the already observed
   * local progress under the closed `multipart_local_content_changed`
   * token, request the exact abort when online — best effort, because an
   * offline abort never blocks the closed verdict and the server's expiry
   * cleanup owns the orphaned staging resources — and report the change
   * so the queue terminalizes the OLD event while the newer watcher event
   * uploads separately.
   */
  async #stopForLocalContentChange(
    event: JournalEvent,
    plan: MultipartSessionPlan,
    completedPartNumbers: ReadonlySet<number>,
  ): Promise<MultipartUploadRunOutcome> {
    await this.#persistProgress(
      event,
      plan,
      completedPartNumbers,
      "uploading",
      "multipart_local_content_changed",
    );
    try {
      await this.#request(() => this.#syncApi.abortMultipartUploadSession(plan.sessionId));
    } catch {
      // Best-effort exact abort: the closed change verdict must survive an
      // offline abort request (spec 8, changed-file row).
    }
    return { outcome: "local_content_changed" };
  }

  /** Record one landed part into the durable safe progress, strictly ascending. */
  async #recordPartCompletion(
    event: JournalEvent,
    plan: MultipartSessionPlan,
    completedPartNumbers: Set<number>,
    partNumber: number,
    safeReason: MultipartSafeReasonToken | null,
  ): Promise<void> {
    completedPartNumbers.add(partNumber);
    await this.#persistProgress(event, plan, completedPartNumbers, "uploading", safeReason);
  }

  /** Persist one safe progress snapshot; the set sorts ascending before SQL. */
  async #persistProgress(
    event: JournalEvent,
    plan: MultipartSessionPlan,
    completedPartNumbers: ReadonlySet<number>,
    sessionState: MultipartSessionState,
    safeReason: MultipartSafeReasonToken | null,
  ): Promise<void> {
    await this.#repository.saveMultipartProgress({
      eventId: event.eventId,
      sessionId: plan.sessionId,
      partSizeBytes: plan.partSizeBytes,
      partCount: plan.partCount,
      expiresAtEpochMs: plan.expiresAtEpochMs,
      completedPartNumbers: [...completedPartNumbers].sort((left, right) => left - right),
      sessionState,
      safeReason,
    });
  }

  // --- frozen file and range guards ------------------------------------------------------------------

  /**
   * Open the current Vault bytes and compare them to the frozen journal
   * fingerprint: the exact byte size first, then the exact SHA-256. A
   * vanished file reports `missing`; a mismatched generation reports
   * `changed` and never lets its bytes enter the session.
   */
  async #checkFrozenFile(
    event: JournalEvent,
    localFile: LocalFile,
  ): Promise<FrozenFileCheck> {
    const bytes = await this.#fileBytesReader.readRegularFileBytes(localFile.normalizedPath);
    if (bytes === null) {
      return { kind: "missing" };
    }
    if (bytes.byteLength !== event.fingerprint.sizeBytes) {
      return { kind: "changed" };
    }
    const fingerprint = await deriveFrozenFingerprint(bytes);
    if (fingerprint.sha256 !== event.fingerprint.sha256) {
      return { kind: "changed" };
    }
    return { kind: "bytes", bytes };
  }

  /**
   * The server derives each range from the frozen geometry (spec 5): an
   * authorization whose part number, offset or window disagrees with the
   * local frozen bytes is malformed and never transmitted.
   */
  #validatePartRange(
    authorization: MultipartPartUrlAuthorization,
    partNumber: number,
    fileSizeBytes: number,
  ): void {
    const expectedOffsetBytes = (partNumber - 1) * MULTIPART_PART_SIZE_BYTES;
    const expectedSizeBytes = Math.min(
      MULTIPART_PART_SIZE_BYTES,
      fileSizeBytes - expectedOffsetBytes,
    );
    if (
      authorization.partNumber !== partNumber ||
      authorization.offsetBytes !== expectedOffsetBytes ||
      authorization.sizeBytes !== expectedSizeBytes ||
      authorization.offsetBytes + authorization.sizeBytes > fileSizeBytes
    ) {
      throw new SyncApiError("server_error");
    }
  }

  // --- small seams -------------------------------------------------------------------------------------

  /** The create call binds the same frozen operation the preflight decided (spec 5). */
  #createSessionInput(event: JournalEvent, localFile: LocalFile): JournalEventPreflightInput {
    const operation = localFile.sourceId === null ? "create" : "update";
    return {
      eventId: event.eventId,
      idempotencyKey: event.idempotencyKey,
      operation,
      localFileId: event.localFileId,
      sourceId: operation === "update" ? localFile.sourceId : null,
      baseVersionId: operation === "update" ? localFile.baseVersionId : null,
      normalizedLocator: localFile.normalizedPath,
      fingerprint: event.fingerprint,
      policyRevisionNumber: localFile.policyRevisionNumber,
    };
  }

  /** One transport request under the per-request timeout. */
  #request<T>(issue: () => Promise<T>): Promise<T> {
    return raceRequestTimeout(issue(), this.#requestTimeoutMs);
  }

  #throwIfSuspended(signal: AbortSignal | undefined): void {
    if (signal?.aborted === true) {
      // Suspend is a normal nonterminal interruption (spec 2.2): the safe
      // progress that landed stays durable and the queue's bounded backoff
      // resumes the SAME session through status on the next foreground run.
      throw new SyncApiError("network_timeout");
    }
  }

  #isPastDeadline(passDeadlineEpochMs: number | undefined): boolean {
    return passDeadlineEpochMs !== undefined && this.#nowEpochMs() >= passDeadlineEpochMs;
  }

  #isRunStopped(stopState: RunStopState, context: MultipartUploadRunContext): boolean {
    return (
      stopState.hasContentChanged ||
      stopState.firstFailure !== null ||
      stopState.deadlineReached ||
      context.signal?.aborted === true
    );
  }
}

/** The frozen terminal result maps onto the journal's receipt outcomes. */
function terminalOutcomeOf(result: MultipartTerminalResult): MultipartUploadRunOutcome {
  const receipt: SmallFileTerminalReceipt = {
    sourceId: result.sourceId,
    sourceVersionId: result.sourceVersionId,
    contentVersion: result.contentVersion,
  };
  if (result.resultKind === "no_change") {
    return { outcome: "no_change", receipt };
  }
  return { outcome: "committed", receipt };
}
