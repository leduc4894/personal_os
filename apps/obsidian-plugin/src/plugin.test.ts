import { readFileSync } from "node:fs";
import * as ts from "typescript";
import { describe, expect, it } from "vitest";

const pluginPath = new URL("./plugin.ts", import.meta.url);
const pluginSource = readFileSync(pluginPath, "utf8");
const sourceFile = ts.createSourceFile("plugin.ts", pluginSource, ts.ScriptTarget.Latest, true);

// The plugin class imports the Obsidian runtime module, so this suite pins its
// source contract statically: the closed composition surface (spec 19), the
// bounded startup action, and no forbidden load-time capability.

const ALLOWED_OBSIDIAN_IMPORT_NAMES = new Set([
  "Plugin",
  "Platform",
  "requestUrl",
  "App",
  "PluginSettingTab",
  "Setting",
  "TFile",
  "TAbstractFile",
  "Modal",
  "Notice",
]);

function extractObsidianImportNames(source: string): string[] {
  const names: string[] = [];
  const importPattern = /import\s+(type\s+)?\{([^}]*)\}\s+from\s+"obsidian"/g;
  for (const match of source.matchAll(importPattern)) {
    for (const specifier of match[2]?.split(",") ?? []) {
      const name = specifier.trim().split(/\s+as\s+/)[0]?.trim();
      if (name) {
        names.push(name);
      }
    }
  }
  return names;
}

describe("Obsidian plugin composition root", () => {
  it("extends Plugin and keeps the lifecycle methods", () => {
    const pluginClass = sourceFile.statements.find(ts.isClassDeclaration);
    expect(pluginClass).toBeDefined();
    const methodNames = (pluginClass?.members.filter(ts.isMethodDeclaration) ?? []).map(
      (method) => method.name.getText(sourceFile),
    );
    expect(methodNames).toContain("onload");
    expect(methodNames).toContain("onunload");
  });

  it("imports only the closed Obsidian adapter surface", () => {
    const names = extractObsidianImportNames(pluginSource);
    expect(names).toEqual(expect.arrayContaining(["Plugin", "Platform", "requestUrl"]));
    for (const name of names) {
      expect(ALLOWED_OBSIDIAN_IMPORT_NAMES.has(name)).toBe(true);
    }
  });

  it("registers the authentication settings tab", () => {
    expect(pluginSource).toContain("addSettingTab");
    expect(pluginSource).toContain("DeviceAuthenticationSettingTab");
  });

  it("performs at most one bounded startup resume-or-refresh action", () => {
    expect(pluginSource).toContain("resolveStartupAction");
    expect(pluginSource).toContain("resumePendingGrant");
    expect(pluginSource).toContain("session.refresh");
    expect(pluginSource).toContain("readDeviceSecretRecord");
    // The startup block performs exactly its two bounded fire-and-forget
    // actions; the queue driver's per-pass refresh is a separate seam and
    // never runs at load time.
    const startupCalls =
      pluginSource.match(/void controller\.resumePendingGrant\(\)|void session\.refresh\(\)/g) ?? [];
    expect(startupCalls.length).toBe(2);
  });

  it("registers the settings tab before any fire-and-forget startup action", () => {
    const tabRegistrationIndex = pluginSource.indexOf("addSettingTab(");
    const startupActionIndex = pluginSource.search(/resumePendingGrant|session\.refresh/);
    expect(tabRegistrationIndex).toBeGreaterThanOrEqual(0);
    expect(startupActionIndex).toBeGreaterThan(tabRegistrationIndex);
    // The bounded startup task must never suspend onload awaiting the poll
    // loop; the spec-19 affordances (Cancel/Open browser again) stay reachable
    // while a pending grant resumes.
    expect(pluginSource).not.toContain("await controller.resumePendingGrant");
    expect(pluginSource).not.toContain("await session.refresh");
  });

  it("wires the policy session into the authenticated lifecycle", () => {
    expect(pluginSource).toContain("new PolicySession(");
    expect(pluginSource).toContain("createObsidianPolicyHttpTransport");
    expect(pluginSource).toContain("adoptOnboardingTrust");
    expect(pluginSource).toContain("policySession.refresh");
    // Initial policy trust is acquired only immediately after the
    // authenticated onboarding exchange completes.
    const exchangeIndex = pluginSource.indexOf("adoptExchange");
    const onboardingTrustIndex = pluginSource.indexOf("adoptOnboardingTrust");
    expect(exchangeIndex).toBeGreaterThanOrEqual(0);
    expect(onboardingTrustIndex).toBeGreaterThan(exchangeIndex);
    expect(pluginSource).toContain("await policySession.adoptOnboardingTrust()");
    expect(pluginSource).not.toContain("void policySession.adoptOnboardingTrust()");
    // Policy refresh happens only after a successful token refresh.
    const refreshIndex = pluginSource.indexOf("session.refresh");
    const policyRefreshIndex = pluginSource.indexOf("policySession.refresh");
    expect(refreshIndex).toBeGreaterThanOrEqual(0);
    expect(policyRefreshIndex).toBeGreaterThan(refreshIndex);
    const refreshFlowIndex = pluginSource.indexOf("void session.refresh()");
    const revisionRefreshIndex = pluginSource.indexOf(
      "refreshVerifiedPolicyAndRequestSnapshot",
      refreshFlowIndex,
    );
    expect(revisionRefreshIndex).toBeGreaterThan(refreshFlowIndex);
    expect(revisionRefreshIndex).toBeLessThan(policyRefreshIndex);
  });

  it("persists the policy cache inside the single plugin-data document", () => {
    expect(pluginSource).toContain("POLICY_CACHE_PLUGIN_DATA_KEY");
    // Settings persistence must preserve the policy cache member instead of
    // replacing the whole document.
    const persistIndex = pluginSource.indexOf("async #persistSettings");
    const persistBody = pluginSource.slice(persistIndex, persistIndex + 500);
    expect(persistBody).toContain("loadData()");
  });

  it("pins the production origin policy to HTTPS-only", () => {
    expect(pluginSource).toContain("ALLOW_LOOPBACK_HTTP_ORIGIN = false");
  });

  it("registers vault capture listeners only after journal recovery", () => {
    expect(pluginSource).toContain("await persistence.open()");
    const recoveryIndex = pluginSource.indexOf("await persistence.open()");
    const listenerIndex = pluginSource.indexOf("registerEvent(");
    expect(listenerIndex).toBeGreaterThan(recoveryIndex);
    const listenerCount = pluginSource.match(/registerEvent\(/g)?.length ?? 0;
    expect(listenerCount).toBe(4);
  });

  it("does not admit startup Vault events before a verified policy is available", () => {
    expect(pluginSource).toContain("#canCaptureVaultChanges");
    const createListenerIndex = pluginSource.indexOf('this.app.vault.on("create"');
    const createListenerBody = pluginSource.slice(createListenerIndex, createListenerIndex + 700);
    const modifyListenerIndex = pluginSource.indexOf('this.app.vault.on("modify"');
    const modifyListenerBody = pluginSource.slice(modifyListenerIndex, modifyListenerIndex + 700);
    expect(createListenerBody.indexOf("#canCaptureVaultChanges()")).toBeLessThan(
      createListenerBody.indexOf("notifyPathChanged"),
    );
    expect(modifyListenerBody.indexOf("#canCaptureVaultChanges()")).toBeLessThan(
      modifyListenerBody.indexOf("notifyPathChanged"),
    );
  });

  it("binds Vault listeners only after Obsidian has finished restoring the layout", () => {
    const recoveryIndex = pluginSource.indexOf("await persistence.open()");
    const layoutReadyIndex = pluginSource.indexOf("this.app.workspace.onLayoutReady");
    expect(layoutReadyIndex).toBeGreaterThan(recoveryIndex);
    const listenerIndex = pluginSource.indexOf('this.app.vault.on("create"');
    expect(listenerIndex).toBeGreaterThan(layoutReadyIndex);
  });

  it("removes manual sync commands and requests startup convergence", () => {
    expect(pluginSource).toContain("AutomaticSnapshotCoordinator");
    expect(pluginSource).not.toContain('id: "sync-now"');
    expect(pluginSource).not.toContain('id: "sync-existing-files"');
    expect(pluginSource).toContain('id: "restore-selected-tombstone"');
    // Sync error tracing task 2 adds the ONE sanitized export command and
    // task 3 the ONE bounded self-check command; the restore command stays
    // the only other explicit surface.
    expect(pluginSource).toContain('id: "copy-sync-diagnostics"');
    expect(pluginSource).toContain('id: "run-sync-self-check"');
    expect(pluginSource.match(/addCommand\(/g)?.length ?? 0).toBe(3);
    expect(pluginSource).not.toContain("#runExistingFilesScan");
    expect(pluginSource).not.toContain("#drainExistingFilesScanQueue");
    expect(pluginSource).not.toContain("#confirmExistingFilesScan");
    expect(pluginSource).toContain('automaticSnapshotCoordinator.request("startup")');
    const lastListenerIndex = pluginSource.lastIndexOf("this.registerEvent(");
    const startupRequestIndex = pluginSource.indexOf('automaticSnapshotCoordinator.request("startup")');
    expect(startupRequestIndex).toBeGreaterThan(lastListenerIndex);
    // The restore command narrowly routes through the captured
    // LifecycleCapture port so the brief's hash-verification invariant
    // and safe-code rejection path remain in one tested module.
    const restoreCommandIndex = pluginSource.indexOf('id: "restore-selected-tombstone"');
    const restoreBodyStart = pluginSource.indexOf("{", restoreCommandIndex);
    const restoreBodyEnd = pluginSource.indexOf("});", restoreBodyStart);
    const restoreBody = pluginSource.slice(restoreBodyStart, restoreBodyEnd);
    expect(restoreBody).toContain("#runRestoreSelectedTombstone");
    expect(restoreBody).not.toContain("setInterval");
    expect(restoreBody).not.toContain("registerInterval");
    // The restore composition calls the lifecycle capture port directly.
    const restoreMethodMatch = pluginSource.match(
      /async #runRestoreSelectedTombstone\(\): Promise<void> \{[\s\S]*?\n  \}\n/,
    );
    expect(restoreMethodMatch?.[0]).toBeTruthy();
    expect(restoreMethodMatch?.[0]).toContain("requestRestore");
    expect(restoreMethodMatch?.[0]).toContain("catch");
    expect(restoreMethodMatch?.[0]).toContain("#refreshSyncStatus");
  });

  it("requests policy convergence only after onboarding trust succeeds", () => {
    const trustIndex = pluginSource.indexOf("await policySession.adoptOnboardingTrust()");
    const requestIndex = pluginSource.indexOf('this.#requestAutomaticSnapshot("policy_accepted")');
    expect(trustIndex).toBeGreaterThanOrEqual(0);
    expect(requestIndex).toBeGreaterThan(trustIndex);
  });

  it("wires automatic snapshots through the safe capture and bounded queue seams", () => {
    expect(pluginSource).toContain("new JournalQueueDriver(");
    expect(pluginSource).toContain("new AutomaticSnapshotCoordinator(");
    expect(pluginSource).toContain("new CoalescingQueuePassDispatcher(");
    expect(pluginSource).toContain("createJournalSyncApi(");
    expect(pluginSource).toContain("createObsidianSyncHttpTransport");
    // The driver runs over the SAME read-only vault reader as capture and
    // refreshes through the existing device token session.
    expect(pluginSource).toContain("fileBytesReader: vaultReader");
    expect(pluginSource).toContain("refreshAccessToken: () => session.refresh()");
    // Vault events retain their settled-event queue trigger; automatic
    // snapshots await the exact same bounded wrapper rather than bypassing
    // the one-active-request queue driver.
    const triggerCount = pluginSource.match(/void this\.#runBoundedQueuePass\(\)/g)?.length ?? 0;
    expect(triggerCount).toBe(0);
    expect(pluginSource).not.toContain("queueDriver.requestPass()");
    // A Vault event's pass runs only after its own settled admission landed
    // (the 250 ms settle re-reads bytes); an event-time pass would find the
    // journal still empty and leave the event pending until the next trigger.
    expect(pluginSource.match(/notifyPathChanged\(file\.path\)\.then\(/g)?.length ?? 0).toBe(2);
    // The automatic coordinator's dispatcher owns the only awaited queue
    // request, preserving the existing bounded wrapper.
    expect(pluginSource).toContain("await boundedQueuePassDispatcher.request()");
    // A Vault event triggers one bounded pass alongside capture.
    const createListenerIndex = pluginSource.indexOf('this.app.vault.on("create"');
    const passIndex = pluginSource.indexOf("void boundedQueuePassDispatcher.request()");
    expect(passIndex).toBeGreaterThan(createListenerIndex);
    // Startup convergence is requested only after recovery and listener
    // installation, never by a direct foreground pass.
    const recoveryIndex = pluginSource.indexOf("await persistence.open()");
    const coordinatorIndex = pluginSource.indexOf("new AutomaticSnapshotCoordinator(");
    expect(coordinatorIndex).toBeGreaterThan(recoveryIndex);
    const coordinatorBody = pluginSource.slice(coordinatorIndex, coordinatorIndex + 2400);
    expect(coordinatorBody).toContain("#canCaptureVaultChanges()");
    expect(coordinatorBody).toContain('snapshot.kind === "reconcile_required"');
    expect(coordinatorBody).toContain("capture.runAutomaticSnapshot({ signal })");
    expect(coordinatorBody).toContain("await boundedQueuePassDispatcher.request()");
  });

  it("arms one cancellable scheduled retry pass after every pass that actually ran", () => {
    // Fix round 3 (extending fix round 2 D4): arming follows EVERY pass
    // end except `pass_already_running` — a `completed` pass can still
    // leave parked work behind (a lifecycle-lane retryable failure parks
    // its event while the content lane drains or idles). The armer
    // no-ops when no pending row carries a retry deadline, so
    // unconditional arming costs nothing otherwise. ONE cancellable
    // timer at the earliest pending retry deadline plus a small safety
    // margin; its single firing requests one bounded queue pass. Never a
    // repeating daemon loop, and unload cancels the outstanding timer.
    //
    // Fix round 4 (busy-loop closure): a `stopped` pass end is the ONE
    // further exclusion. The dispatcher is not stopped (only unload
    // stops it), so a stopped-pass timer would fire into the stopped
    // driver, produce another stopped pass, re-arm at a possibly-past
    // deadline (`setTimeout(0)`), and self-sustain (~250 passes/sec)
    // while the stopping condition persists — for example a
    // reconcile-required journal with a parked retry row.
    const passWrapperIndex = pluginSource.indexOf(
      "async #runBoundedQueuePass(): Promise<QueuePassSummary>",
    );
    expect(passWrapperIndex).toBeGreaterThanOrEqual(0);
    const wrapperBody = pluginSource.slice(passWrapperIndex, passWrapperIndex + 2_600);
    // The armer runs inside the only guards that matter: the invocation
    // that actually ran the pass (never a `pass_already_running`
    // bystander) and never a `stopped` pass end.
    const ranGuardIndex = wrapperBody.indexOf('summary.outcome !== "pass_already_running"');
    expect(ranGuardIndex).toBeGreaterThanOrEqual(0);
    const stoppedGuardIndex = wrapperBody.indexOf('summary.outcome !== "stopped"');
    expect(stoppedGuardIndex).toBeGreaterThan(ranGuardIndex);
    expect(wrapperBody.indexOf("#armScheduledRetryPassTrigger()")).toBeGreaterThan(
      stoppedGuardIndex,
    );
    // Arming is NOT gated on any other specific pass outcome:
    // completed / retry_scheduled / login_required / deadline_reached
    // all still arm.
    expect(wrapperBody).not.toContain('summary.outcome === "retry_scheduled"');
    expect(wrapperBody).not.toContain('summary.outcome === "login_required"');
    const triggerIndex = pluginSource.indexOf("#armScheduledRetryPassTrigger(): void");
    expect(triggerIndex).toBeGreaterThanOrEqual(0);
    const triggerBody = pluginSource.slice(triggerIndex, triggerIndex + 2_400);
    expect(triggerBody).toContain("repository.readEarliestPendingRetryEpochMs()");
    expect(triggerBody).toContain("SCHEDULED_RETRY_PASS_SAFETY_MARGIN_MS");
    expect(triggerBody).toContain("setTimeout");
    expect(triggerBody).toContain("clearTimeout");
    // At most ONE outstanding timer: an already-earlier target keeps the
    // existing timer, a sooner target re-arms it.
    expect(triggerBody).toContain("#scheduledRetryPassTargetEpochMs");
    // The timer's single firing requests one bounded pass through the
    // same dispatcher every other trigger uses.
    expect(triggerBody).toContain("void this.#boundedQueuePassDispatcher?.request()");
    // Unload cancels the outstanding timer before the coordinators stop.
    const unloadIndex = pluginSource.indexOf("override onunload(): void");
    const unloadBody = pluginSource.slice(unloadIndex, unloadIndex + 900);
    expect(unloadBody).toContain("#clearScheduledRetryPassTrigger()");
  });

  it("counts pre-existing pending events when the automatic snapshot requests its queue pass", () => {
    // Fix round 2 D2: the coordinator requests a pass only from the
    // snapshot's queued-event count, and the scan's own admission count is
    // lifecycle-blind (a rename changes no bytes, so admission records
    // nothing). The wrapper MUST surface the repository's post-snapshot
    // pending count — which includes lifecycle rows — whenever it exceeds
    // the scan's own admission count, or a restart with only pending
    // lifecycle work runs no pass at all.
    const coordinatorIndex = pluginSource.indexOf("new AutomaticSnapshotCoordinator(");
    expect(coordinatorIndex).toBeGreaterThanOrEqual(0);
    const coordinatorBody = pluginSource.slice(coordinatorIndex, coordinatorIndex + 2_400);
    expect(coordinatorBody).toContain("capture.runAutomaticSnapshot({ signal })");
    expect(coordinatorBody).toContain("repository.countPendingEvents()");
    // The pending count may only RAISE the reported count, never lower the
    // scan's own admission count.
    expect(coordinatorBody).toContain("Math.max(");
    expect(coordinatorBody).toContain("summary.queuedEventCount,");
  });

  it("chains a bounded queue pass after the settled rename and delete listener captures", () => {
    // Fix round 2 D1: rename and delete listeners MUST chain the same
    // fire-and-forget dispatcher request the create/modify listeners
    // chain. Without it, recorded lifecycle events sit `queued` forever —
    // no surface of their own ever drains them.
    const listenerBody = (marker: string): string => {
      const index = pluginSource.indexOf(marker);
      expect(index).toBeGreaterThanOrEqual(0);
      const nextRegistration = pluginSource.indexOf(
        "this.registerEvent(",
        index + marker.length,
      );
      return pluginSource.slice(index, nextRegistration === -1 ? index + 700 : nextRegistration);
    };
    const renameBody = listenerBody('this.app.vault.on("rename"');
    expect(renameBody).toContain("notifyPathRenamed");
    expect(renameBody.indexOf("notifyPathRenamed")).toBeLessThan(renameBody.indexOf(".then("));
    expect(renameBody).toContain("void boundedQueuePassDispatcher.request();");
    const deleteBody = listenerBody('this.app.vault.on("delete"');
    expect(deleteBody).toContain("notifyPathDeleted");
    expect(deleteBody.indexOf("notifyPathDeleted")).toBeLessThan(deleteBody.indexOf(".then("));
    expect(deleteBody).toContain("void boundedQueuePassDispatcher.request();");
    // Exactly the four vault listeners own the fire-and-forget request;
    // the coordinator's awaited request stays the only other trigger.
    expect(pluginSource.match(/void boundedQueuePassDispatcher\.request\(\);/g)?.length ?? 0).toBe(
      4,
    );
  });

  it("wires the lifecycle capture, lifecycle driver and lifecycle api behind the restore command", () => {
    expect(pluginSource).toContain("new LifecycleCaptureImpl(");
    expect(pluginSource).toContain("new LifecycleDriverImpl(");
    expect(pluginSource).toContain("createRequestUrlLifecycleApi(");
    // The lifecycle capture is composed into the JournalCapture (rename,
    // move, delete detection) AND stored on the plugin instance so the
    // explicit restore command can address it through the same port.
    expect(pluginSource).toContain("#lifecycleCapture");
    // The capture composes into the queue driver so the foreground pass
    // interleaves the lifecycle lane ahead of the content lane.
    expect(pluginSource).toContain("lifecycleDriver");
  });

  it("injects UUIDv7 identities into the production journal repository and lifecycle capture", () => {
    const repositoryIndex = pluginSource.indexOf("new JournalRepository(");
    expect(repositoryIndex).toBeGreaterThanOrEqual(0);
    const repositoryComposition = pluginSource.slice(repositoryIndex, repositoryIndex + 300);
    expect(repositoryComposition).toContain("createId: createJournalId");
    const captureIndex = pluginSource.indexOf("new LifecycleCaptureImpl(");
    expect(captureIndex).toBeGreaterThanOrEqual(0);
    const captureComposition = pluginSource.slice(captureIndex, captureIndex + 500);
    expect(captureComposition).toContain("createId: createJournalId");
  });

  it("never logs paths, locators, source IDs, tokens or fingerprints from the restore command", () => {
    // The error reporter of the restore command must surface only the
    // closed safe-code label of the journal store error; the raw failure
    // (and any underlying locator / source id / fingerprint / token) is
    // never written to console.
    const restoreIndex = pluginSource.indexOf('id: "restore-selected-tombstone"');
    const restoreBodyStart = pluginSource.indexOf("{", restoreIndex);
    const restoreBodyEnd = pluginSource.indexOf("});", restoreBodyStart);
    const restoreBody = pluginSource.slice(restoreBodyStart, restoreBodyEnd);
    expect(restoreBody).not.toContain("console.error");
    expect(restoreBody).not.toContain("console.log");
    expect(restoreBody).not.toContain("throw error");
    expect(restoreBody).not.toContain("requestRestore(error)");
    // The method body must swallow the lifecycle capture error so the
    // closed safe-code label is the only thing the surface sees.
    const restoreMethodMatch = pluginSource.match(
      /async #runRestoreSelectedTombstone\(\): Promise<void> \{[\s\S]*?\n  \}\n/,
    );
    expect(restoreMethodMatch?.[0]).toBeTruthy();
    expect(restoreMethodMatch?.[0]).not.toContain("console.error");
    expect(restoreMethodMatch?.[0]).not.toContain("console.log");
  });

  it("renders the closed sync status on a small status-bar surface", () => {
    expect(pluginSource).toContain("addStatusBarItem");
    expect(pluginSource).toContain("#refreshSyncStatus");
    expect(pluginSource).toContain("projectJournalSyncStatus");
    expect(pluginSource).toContain("renderJournalSyncStatusText");
    // The settings snapshot carries the SAME redacted projection (spec 11).
    expect(pluginSource).toContain("SYNC_STATUS_TEXT");
    expect(pluginSource).toContain("syncBlockerGuidanceLines");
  });

  it("wires the lifecycle state histogram into the live composition projection", () => {
    // Fix round 1 I1 (a): the lifecycle state histogram MUST be called
    // from `#projectSyncStatus()` and passed verbatim to the projection.
    const projectionHeaderIndex = pluginSource.indexOf(
      "#projectSyncStatus(): JournalSyncStatusSnapshot | null",
    );
    expect(projectionHeaderIndex).toBeGreaterThanOrEqual(0);
    const projectionBody = pluginSource.slice(
      projectionHeaderIndex,
      projectionHeaderIndex + 2_000,
    );
    expect(projectionBody).toContain("repository.readLifecycleStateCounts()");
    expect(projectionBody).toContain("lifecycleStateCounts:");
  });

  it("wires the pending lifecycle event count into the live composition projection", () => {
    // Fix round 1 I1 (b): the pending lifecycle event count MUST be
    // called from `#projectSyncStatus()` and passed verbatim to the
    // projection. Otherwise the count is dead code from the UI's view.
    const projectionHeaderIndex = pluginSource.indexOf(
      "#projectSyncStatus(): JournalSyncStatusSnapshot | null",
    );
    const projectionBody = pluginSource.slice(
      projectionHeaderIndex,
      projectionHeaderIndex + 2_000,
    );
    expect(projectionBody).toContain("repository.countPendingLifecycleEvents()");
    expect(projectionBody).toContain("pendingLifecycleEventCount:");
  });

  it("wires the failed attempt count into the live composition projection", () => {
    // Fix round 1 I1 (c): the failed-attempt count MUST be called from
    // `#projectSyncStatus()` and passed verbatim to the projection.
    const projectionHeaderIndex = pluginSource.indexOf(
      "#projectSyncStatus(): JournalSyncStatusSnapshot | null",
    );
    const projectionBody = pluginSource.slice(
      projectionHeaderIndex,
      projectionHeaderIndex + 2_000,
    );
    expect(projectionBody).toContain("repository.countFailedAttempts()");
    expect(projectionBody).toContain("failedAttemptCount:");
  });

  it("wires the blocked reason codes into the live composition projection", () => {
    // Fix round 1 I1 (d): the closed blocked reason codes MUST be called
    // from `#projectSyncStatus()` and passed verbatim to the projection.
    const projectionHeaderIndex = pluginSource.indexOf(
      "#projectSyncStatus(): JournalSyncStatusSnapshot | null",
    );
    const projectionBody = pluginSource.slice(
      projectionHeaderIndex,
      projectionHeaderIndex + 2_000,
    );
    expect(projectionBody).toContain("repository.readLifecycleBlockedReasonCodes()");
    expect(projectionBody).toContain("lifecycleBlockedReasonCodes:");
  });

  it("composes the durable diagnostics trail into the journal seams (sync error tracing task 1)", () => {
    // One trail sidecar (`sync-diagnostics-trail.json`) lives in the Vault
    // plugin directory through the SAME journal file store port, loads (and
    // resets, when corrupt) before any seam can append, and feeds BOTH the
    // persistence publish-failure tap and the queue driver wire/pass taps.
    const trailIndex = pluginSource.indexOf("createSyncDiagnosticsTrail(");
    expect(trailIndex).toBeGreaterThanOrEqual(0);
    const trailComposition = pluginSource.slice(trailIndex, trailIndex + 200);
    expect(trailComposition).toContain("this.createJournalFileStore()");
    expect(pluginSource).toContain("await diagnosticTrail.load()");
    const persistenceIndex = pluginSource.indexOf("new JournalPersistence({");
    expect(persistenceIndex).toBeGreaterThan(trailIndex);
    expect(pluginSource.slice(persistenceIndex, persistenceIndex + 220)).toContain(
      "diagnosticTrail",
    );
    const driverIndex = pluginSource.indexOf("new JournalQueueDriver({");
    expect(driverIndex).toBeGreaterThan(persistenceIndex);
    expect(pluginSource.slice(driverIndex, driverIndex + 700)).toContain("diagnosticTrail");
    // The corrupt-sidecar reset precedes every seam that appends.
    const loadIndex = pluginSource.indexOf("await diagnosticTrail.load()");
    expect(loadIndex).toBeGreaterThan(trailIndex);
    expect(loadIndex).toBeLessThan(persistenceIndex);
  });

  it("surfaces the journal store diagnostics through the settings snapshot (fix round 5)", () => {
    // The live park mystery (repro-park-not-landing.md): markEventWaitingRetry
    // commits never landed on the user's machine while sibling mutations
    // published fine, and runPass's bare catch discarded the closed reason.
    // The settings snapshot must surface BOTH closed-token diagnostic
    // surfaces: the queue driver's journal-failure ring and the
    // persistence generation-publish failure counter/ring.
    const snapshotIndex = pluginSource.indexOf("getSnapshot: () => {");
    expect(snapshotIndex).toBeGreaterThanOrEqual(0);
    // The window grew with the trail snapshot fields (sync error tracing
    // task 2); the pinned fix-round-5 reads must stay inside it.
    const snapshotBody = pluginSource.slice(snapshotIndex, snapshotIndex + 2_600);
    expect(snapshotBody).toContain("readJournalFailureReasons()");
    expect(snapshotBody).toContain("lastJournalFailureReasons:");
    expect(snapshotBody).toContain("readGenerationPublishFailureSummary()");
    expect(snapshotBody).toContain("generationPublishFailureCount:");
    expect(snapshotBody).toContain("lastGenerationPublishFailureReasons:");
  });

  it("registers the copy sync diagnostics command with a clipboard write and a modal fallback (sync error tracing task 2)", () => {
    expect(pluginSource).toContain('id: "copy-sync-diagnostics"');
    expect(pluginSource).toContain('"Copy sync diagnostics"');
    // The block is built ONLY by the pure closed-vocabulary renderer.
    expect(pluginSource).toContain("renderSyncDiagnosticsExportBlock");
    const commandIndex = pluginSource.indexOf('id: "copy-sync-diagnostics"');
    const commandBody = pluginSource.slice(commandIndex, commandIndex + 300);
    expect(commandBody).toContain("#copySyncDiagnostics");
    const methodIndex = pluginSource.indexOf("#copySyncDiagnostics(): Promise<void>");
    expect(methodIndex).toBeGreaterThanOrEqual(0);
    const methodBody = pluginSource.slice(methodIndex, methodIndex + 1_400);
    expect(methodBody).toContain("navigator.clipboard");
    expect(methodBody).toContain("writeText");
    // The clipboard-unavailable branch shows the SAME sanitized block in a
    // read-only modal; the block never reaches a console or a log.
    expect(methodBody).toContain("PreformattedTextModal");
    expect(methodBody).not.toContain("console.");
    // The builder reads only the closed snapshot surfaces.
    const builderIndex = pluginSource.indexOf("#buildSyncDiagnosticsExportBlock(): string");
    expect(builderIndex).toBeGreaterThanOrEqual(0);
    const builderBody = pluginSource.slice(builderIndex, builderIndex + 1_600);
    expect(builderBody).toContain("#projectSyncStatus()");
    expect(builderBody).toContain("readJournalFailureReasons()");
    expect(builderBody).toContain("readGenerationPublishFailureSummary()");
    expect(builderBody).toContain("readEntries()");
    expect(builderBody).toContain("readAppendFailureCount()");
    expect(builderBody).not.toContain("console.");
  });

  it("registers the bounded sync self-check command with closed verdicts only (sync error tracing task 3)", () => {
    expect(pluginSource).toContain('id: "run-sync-self-check"');
    expect(pluginSource).toContain('"Run sync self-check"');
    const commandIndex = pluginSource.indexOf('id: "run-sync-self-check"');
    const commandBody = pluginSource.slice(commandIndex, commandIndex + 300);
    expect(commandBody).toContain("#runSyncSelfCheck");
    const methodMatch = pluginSource.match(
      /async #runSyncSelfCheck\(\): Promise<void> \{[\s\S]*?\n  \}\n/,
    );
    expect(methodMatch?.[0]).toBeTruthy();
    const methodBody = methodMatch?.[0] ?? "";
    // The pure runner owns every verdict; the summary notice renders ONLY
    // its closed-token line.
    expect(methodBody).toContain("runSyncSelfCheck");
    expect(methodBody).toContain("renderSyncSelfCheckSummaryText");
    expect(methodBody).toContain("new Notice");
    // The self-check holds NO sync-mutating capability: no preflight
    // request, no queue pass, no policy read, no journal surface, no
    // logging surface.
    expect(methodBody).not.toContain("preflight");
    expect(methodBody).not.toContain("requestPass");
    expect(methodBody).not.toContain("policySession");
    expect(methodBody).not.toContain("queueDriver");
    expect(methodBody).not.toContain("repository");
    expect(methodBody).not.toContain("console.");
    // The credential verdict is the boolean presence only — the token value
    // never enters the self-check.
    expect(methodBody).toContain("accessCredential != null");
    // The probe reuses the existing requestUrl transport seam toward the
    // SAME resolved origin the sync client uses; the runner bounds it with
    // its short timeout and runs it exactly once (no retry loop here).
    expect(methodBody).toContain("createObsidianPolicyHttpTransport");
    expect(methodBody).toContain("parseServerOrigin");
    expect(methodBody).toContain("/api/health/live");
    expect(methodBody).not.toContain("setInterval");
    expect(methodBody).not.toContain("registerInterval");
    // The trail port plus the boolean reader plus the one probe are the
    // runner's entire capability surface.
    expect(methodBody).toContain("const trail = this.#diagnosticTrail");
    expect(methodBody).toContain("hasAccessCredential:");
    expect(methodBody).toContain("probeOrigin:");
  });

  it("carries the trail tail, counts and derived stop reasons on the settings snapshot (sync error tracing task 2)", () => {
    const snapshotIndex = pluginSource.indexOf("getSnapshot: () => {");
    expect(snapshotIndex).toBeGreaterThanOrEqual(0);
    const snapshotBody = pluginSource.slice(snapshotIndex, snapshotIndex + 3_000);
    expect(snapshotBody).toContain("deriveSyncStopReasonTokens");
    expect(snapshotBody).toContain("syncStopReasonTokens:");
    expect(snapshotBody).toContain("trailTailEntries:");
    expect(snapshotBody).toContain("trailEntryCount:");
    expect(snapshotBody).toContain("trailAppendFailureCount:");
  });

  it("retains the diagnostics trail for the export and settings surfaces and clears it on release", () => {
    expect(pluginSource).toContain("#diagnosticTrail");
    expect(pluginSource).toContain("this.#diagnosticTrail = diagnosticTrail;");
    const releaseIndex = pluginSource.indexOf("#releaseJournalResources(): void");
    expect(releaseIndex).toBeGreaterThanOrEqual(0);
    const releaseBody = pluginSource.slice(releaseIndex, releaseIndex + 900);
    expect(releaseBody).toContain("this.#diagnosticTrail = null");
  });

  it("folds the lifecycle state histogram onto the settings snapshot", () => {
    // Fix round 1 I1: the settings snapshot holds the same four lifecycle
    // fields so the settings tab can render the histogram and the blocked
    // reason codes list. Without this the operator can never see the
    // security-grade redacted lifecycle surface.
    const snapshotBuilderIndex = pluginSource.indexOf("getSnapshot: () => {");
    expect(snapshotBuilderIndex).toBeGreaterThanOrEqual(0);
    const snapshotBuilderBody = pluginSource.slice(
      snapshotBuilderIndex,
      snapshotBuilderIndex + 2_000,
    );
    expect(snapshotBuilderBody).toContain("lifecycleStateCounts");
    expect(snapshotBuilderBody).toContain("pendingLifecycleEventCount");
    expect(snapshotBuilderBody).toContain("failedAttemptCount");
    expect(snapshotBuilderBody).toContain("lifecycleBlockedReasonCodes");
  });

  it("keeps the projection fail-closed when the journal store throws on the lifecycle reads", () => {
    // Fix round 1 I1: the four new repository read calls live inside the
    // same `try { … } catch { return null }` block so an unreadable
    // journal still renders no status rather than a partial one.
    const projectionHeaderIndex = pluginSource.indexOf(
      "#projectSyncStatus(): JournalSyncStatusSnapshot | null",
    );
    const projectionBody = pluginSource.slice(
      projectionHeaderIndex,
      projectionHeaderIndex + 2_000,
    );
    // The try-block must contain all four new reads; the catch path must
    // still return null.
    const tryBlock = projectionBody.match(/try\s*\{[\s\S]*?\}\s*catch\s*\{[\s\S]*?return null/s);
    expect(tryBlock).not.toBeNull();
    const body = tryBlock?.[0] ?? "";
    expect(body).toContain("readLifecycleStateCounts");
    expect(body).toContain("countPendingLifecycleEvents");
    expect(body).toContain("countFailedAttempts");
    expect(body).toContain("readLifecycleBlockedReasonCodes");
    expect(body).toContain("return null");
  });

  it("stops the driver when the projection says reconcile required", () => {
    // The carried spec-11 requirement: reconcile_required is a hard stop —
    // the status refresh itself must stop the driver.
    const refreshIndex = pluginSource.indexOf("#refreshSyncStatus(): void");
    const projectionMethodIndex = pluginSource.indexOf(
      "#projectSyncStatus(): JournalSyncStatusSnapshot | null",
    );
    expect(refreshIndex).toBeGreaterThanOrEqual(0);
    expect(projectionMethodIndex).toBeGreaterThan(refreshIndex);
    const refreshBody = pluginSource.slice(refreshIndex, projectionMethodIndex);
    expect(refreshBody).toContain('snapshot.kind === "reconcile_required"');
    expect(refreshBody).toContain("this.#queueDriver?.stop()");
  });

  it("stops the queue driver, disposes listeners and attempts the journal flush before closing", () => {
    const automaticStopIndex = pluginSource.indexOf("this.#automaticSnapshotCoordinator?.stop()");
    const stopIndex = pluginSource.indexOf("this.#queueDriver?.stop()");
    const disposeIndex = pluginSource.indexOf("this.#capture?.dispose()");
    const flushIndex = pluginSource.indexOf("this.#journalPersistence?.attemptFinalFlush()");
    const closeIndex = pluginSource.indexOf("this.#journalPersistence?.close()");
    expect(automaticStopIndex).toBeGreaterThanOrEqual(0);
    expect(stopIndex).toBeGreaterThan(automaticStopIndex);
    expect(disposeIndex).toBeGreaterThan(stopIndex);
    expect(flushIndex).toBeGreaterThan(disposeIndex);
    expect(closeIndex).toBeGreaterThan(flushIndex);
    expect(pluginSource).toContain(
      "Promise.all([automaticSnapshotStop, boundedQueuePassStop, captureQuiescence])",
    );
    // Unload never awaits async journal work; the flush attempt stays
    // synchronous and bounded.
    expect(pluginSource).not.toContain("await this.#capture");
    expect(pluginSource).not.toContain("await this.#journalPersistence");
  });

  it("touches no forbidden runtime capability at load time", () => {
    for (const forbiddenText of [
      "node:",
      "electron",
      "FileSystemAdapter",
      "fetch(",
      "process.env",
      "setInterval",
      "registerInterval",
      "qrcode",
      "@workspace/",
    ]) {
      expect(pluginSource).not.toContain(forbiddenText);
    }
  });
});
