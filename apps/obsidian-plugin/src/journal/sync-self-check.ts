/**
 * The bounded sync self-check (sync error tracing design: self-check
 * contract).
 *
 * One run executes three steps in order — the trail append-and-persist
 * probe, the credential-presence check and the origin-reachability probe —
 * and each step yields ONE closed verdict appended to the durable
 * diagnostics trail as a `self_check` entry. The module holds no capability
 * to mutate sync state: its only inputs are the trail port, a boolean
 * credential-presence reader and one injected origin probe, so no journal
 * event write, no preflight request and no policy read can happen here. The
 * origin probe runs exactly once (no retry loop) under a short bounded
 * timeout, and every outcome — including a hang — closes as a verdict.
 *
 * Privacy (spec 9): verdicts are closed tokens only. The origin hostname,
 * any status number and any response text never enter a verdict, a trail
 * entry or the summary line.
 */

import type { SyncApiFailureKind } from "./sync-api";
import type {
  SyncDiagnosticsTrail,
  SyncDiagnosticTrailEntry,
  SyncSelfCheckVerdictToken,
} from "./sync-diagnostics-trail";

/**
 * The bounded origin-probe timeout: a probe still unsettled past it closes
 * as the `network_timeout` verdict instead of hanging the command.
 */
export const SYNC_SELF_CHECK_ORIGIN_PROBE_TIMEOUT_MS = 5_000;

/**
 * The closed network kinds an unreachable origin may report. Both are
 * members of the sync failure vocabulary (`SyncApiFailureKind`), so they
 * type-check as trail tokens without widening that vocabulary.
 */
export type SyncSelfCheckNetworkKind = Extract<
  SyncApiFailureKind,
  "network_offline" | "network_timeout"
>;

/** The three fixed self-check steps, in execution order. */
export type SyncSelfCheckStepName = "trail_persist" | "credential_presence" | "origin_reachability";

/**
 * One step's closed outcome: the verdict token plus — only when the origin
 * was unreachable — the closed network kind label riding along with it.
 */
export interface SyncSelfCheckStepResult {
  readonly step: SyncSelfCheckStepName;
  readonly verdict: SyncSelfCheckVerdictToken;
  readonly networkKind: SyncSelfCheckNetworkKind | null;
}

/** The run's summary: the three step results in execution order. */
export interface SyncSelfCheckSummary {
  readonly steps: readonly SyncSelfCheckStepResult[];
}

export interface SyncSelfCheckOptions {
  readonly trail: SyncDiagnosticsTrail;
  /**
   * The boolean credential-presence reader: the access token VALUE never
   * enters the self-check, only this presence verdict.
   */
  readonly hasAccessCredential: () => boolean;
  /**
   * One origin probe: resolves when anything at the configured origin
   * answered, rejects when it could not be reached. The runner bounds it
   * with the timeout below and never retries it.
   */
  readonly probeOrigin: () => Promise<void>;
  /**
   * The bounded probe timeout; defaults to
   * {@link SYNC_SELF_CHECK_ORIGIN_PROBE_TIMEOUT_MS}.
   */
  readonly originProbeTimeoutMs?: number;
}

// --- step 1: the trail append-and-persist probe ------------------------------------------------------

/** Count the probe-marker entries currently readable in the trail. */
function countTrailProbeEntries(entries: readonly SyncDiagnosticTrailEntry[]): number {
  return entries.filter(
    (entry) => entry.kind === "self_check" && entry.tokens.includes("trail_probe"),
  ).length;
}

/**
 * The trail append-and-persist probe: append one `self_check` probe entry
 * through the SAME vault-adapter write path the journal sidecar uses, await
 * its coalesced persist, then verify the entry is readable back and no
 * persist failure was counted. The verdict entry is appended after the
 * outcome is known — an entry cannot carry the verdict of its own persist.
 */
async function runTrailPersistProbeStep(trail: SyncDiagnosticsTrail): Promise<SyncSelfCheckStepResult> {
  const appendFailureCountBefore = trail.readAppendFailureCount();
  const probeEntriesBefore = countTrailProbeEntries(trail.readEntries());
  await trail.append({ kind: "self_check", tokens: ["trail_probe"] });
  // The awaited append resolves after the persist attempt: the write path
  // holds iff the probe entry is readable back AND the swallowed-failure
  // counter did not move. A concurrent trail persist failure inside the
  // same window — or a probe-marker eviction at the 128-entry ring edge —
  // conservatively fails the probe; the write path is unhealthy either way.
  const isPersisted =
    countTrailProbeEntries(trail.readEntries()) === probeEntriesBefore + 1 &&
    trail.readAppendFailureCount() === appendFailureCountBefore;
  const verdict: SyncSelfCheckVerdictToken = isPersisted
    ? "trail_persist_ok"
    : "trail_persist_failed";
  await trail.append({ kind: "self_check", tokens: [verdict] });
  return { step: "trail_persist", verdict, networkKind: null };
}

// --- step 2: the credential-presence check -----------------------------------------------------------

/** The boolean credential-presence verdict; the token value never enters. */
async function runCredentialPresenceStep(
  trail: SyncDiagnosticsTrail,
  hasAccessCredential: () => boolean,
): Promise<SyncSelfCheckStepResult> {
  const verdict: SyncSelfCheckVerdictToken = hasAccessCredential()
    ? "credential_present"
    : "credential_absent";
  await trail.append({ kind: "self_check", tokens: [verdict] });
  return { step: "credential_presence", verdict, networkKind: null };
}

// --- step 3: the origin-reachability probe ------------------------------------------------------------

/**
 * Run one origin probe under the bounded timeout and close its outcome as a
 * network verdict: `null` when anything answered, `network_offline` when
 * the probe rejected, `network_timeout` when the bound expired first. The
 * race never rejects and never retries; the losing timer is cleared so no
 * dangling timer outlives the run.
 */
function probeOriginUnderTimeout(
  probeOrigin: () => Promise<void>,
  timeoutMs: number,
): Promise<SyncSelfCheckNetworkKind | null> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve("network_timeout"), timeoutMs);
    probeOrigin().then(
      () => {
        clearTimeout(timer);
        resolve(null);
      },
      () => {
        clearTimeout(timer);
        resolve("network_offline");
      },
    );
  });
}

/** The origin-reachability verdict with the closed network kind on failure. */
async function runOriginReachabilityStep(
  trail: SyncDiagnosticsTrail,
  probeOrigin: () => Promise<void>,
  timeoutMs: number,
): Promise<SyncSelfCheckStepResult> {
  const networkKind = await probeOriginUnderTimeout(probeOrigin, timeoutMs);
  const verdict: SyncSelfCheckVerdictToken =
    networkKind === null ? "origin_reachable" : "origin_unreachable";
  // The network kind label is itself a closed trail token, so it rides
  // along as the second token of the unreachable verdict.
  const tokens: readonly (SyncSelfCheckVerdictToken | SyncSelfCheckNetworkKind)[] =
    networkKind === null ? [verdict] : [verdict, networkKind];
  await trail.append({ kind: "self_check", tokens });
  return { step: "origin_reachability", verdict, networkKind };
}

// --- the run and the summary line ----------------------------------------------------------------------

/**
 * Run the three-step bounded self-check. The steps run strictly in order —
 * every step appends its `self_check` trail entry before the next one
 * starts — and no step can mutate sync state: the trail observes, the
 * credential read is a boolean, and the one origin probe is the only
 * network touch.
 */
export async function runSyncSelfCheck(
  options: SyncSelfCheckOptions,
): Promise<SyncSelfCheckSummary> {
  const originProbeTimeoutMs = options.originProbeTimeoutMs ?? SYNC_SELF_CHECK_ORIGIN_PROBE_TIMEOUT_MS;
  const steps: SyncSelfCheckStepResult[] = [
    await runTrailPersistProbeStep(options.trail),
    await runCredentialPresenceStep(options.trail, options.hasAccessCredential),
    await runOriginReachabilityStep(options.trail, options.probeOrigin, originProbeTimeoutMs),
  ];
  return { steps };
}

/**
 * Render the one-line self-check summary of the notice: the three closed
 * verdict tokens in step order, with the network kind label joined onto an
 * unreachable-origin verdict. Fixed English head plus closed tokens only —
 * never a hostname, status number, response text or any free-form string.
 */
export function renderSyncSelfCheckSummaryText(summary: SyncSelfCheckSummary): string {
  const stepTexts = summary.steps.map((step) =>
    step.networkKind === null ? step.verdict : `${step.verdict} · ${step.networkKind}`,
  );
  return `Sync self-check: ${stepTexts.join(" · ")}`;
}
