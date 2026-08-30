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

  it("injects the observed platform class into the multipart queue driver", () => {
    // Child 7 spec 4: Desktop earns three part-PUT permits, every
    // non-Desktop runtime stays under the hard two-permit Mobile cap. The
    // composition root must pass the REAL observed class — an unwired
    // construction silently violates the Mobile cap on real installs.
    expect(pluginSource).toContain(
      "function resolveMultipartPlatformClass(): MultipartUploadPlatform",
    );
    const resolverIndex = pluginSource.indexOf("function resolveMultipartPlatformClass");
    expect(resolverIndex).toBeGreaterThanOrEqual(0);
    const resolverBody = pluginSource.slice(resolverIndex, resolverIndex + 300);
    expect(resolverBody).toContain('return Platform.isDesktop ? "desktop" : "mobile";');
    const driverConstructionIndex = pluginSource.indexOf("new JournalQueueDriver({");
    const constructionBody = pluginSource.slice(
      driverConstructionIndex,
      driverConstructionIndex + 900,
    );
    expect(driverConstructionIndex).toBeGreaterThanOrEqual(0);
    expect(constructionBody).toContain("multipartPlatform: resolveMultipartPlatformClass()");
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
    // Sync error tracing task 2 adds the ONE sanitized export command,
    // task 3 the ONE bounded self-check command, and device cursor and
    // manifest reconciliation task 12 the ONE explicit repair command.
    expect(pluginSource).toContain('id: "copy-sync-diagnostics"');
    expect(pluginSource).toContain('id: "run-sync-self-check"');
    expect(pluginSource).toContain('id: "repair-sync"');
    expect(pluginSource).toContain('"Repair sync"');
    // Plugin hygiene (2026-08-16 §12) adds the ONE retry affordance.
    expect(pluginSource).toContain('id: "retry-connection"');
    expect(pluginSource.match(/addCommand\(/g)?.length ?? 0).toBe(5);
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

  it("wires the restore command through the reservation-first protocol", () => {
    const restoreMethodMatch = pluginSource.match(
      /async #runRestoreSelectedTombstone\(\): Promise<void> \{[\s\S]*?\n  \}\n/,
    );
    expect(restoreMethodMatch?.[0]).toBeTruthy();
    const body = restoreMethodMatch?.[0] ?? "";
    // The durable reservation lands at prompt-accept, strictly before the
    // confirm step records the restore event.
    const reserveIndex = body.indexOf("reserveRestoreTarget");
    const requestIndex = body.indexOf("requestRestore");
    expect(reserveIndex).toBeGreaterThanOrEqual(0);
    expect(requestIndex).toBeGreaterThan(reserveIndex);
    // Explicit cancel releases the reservation; refusals surface closed.
    expect(body).toContain("releaseRestoreTarget");
    expect(body).toContain("RESTORE_RESERVATION_REFUSAL_NOTICES");
    // Each refusal surfaces one trail token through the failure reporter.
    expect(body).toContain("reportJournalFailure");
    // After the record, exactly one bounded queue pass ships the event.
    expect(body).toContain("#boundedQueuePassDispatcher");
    // The three closed refusal tokens and their path-free Notice texts
    // exist at the composition source level.
    for (const refusalToken of [
      "restore_target_occupied",
      "restore_target_busy",
      "restore_already_pending",
    ]) {
      expect(pluginSource).toContain(refusalToken);
    }
  });

  it("renders the restore reservation refusals with closed Notice texts only", () => {
    const noticeLines = pluginSource
      .split("\n")
      .filter((line) => line.includes("new Notice(") || line.includes("    restore_"));
    expect(noticeLines.length).toBeGreaterThanOrEqual(4);
    for (const line of noticeLines) {
      // No path, locator or identifier interpolation ever reaches a Notice.
      expect(line).not.toContain("${");
    }
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
    expect(coordinatorBody).toContain("journalFailureReporter");

    const dispatcherIndex = pluginSource.indexOf("new CoalescingQueuePassDispatcher(");
    expect(dispatcherIndex).toBeGreaterThanOrEqual(0);
    const dispatcherBody = pluginSource.slice(dispatcherIndex, dispatcherIndex + 800);
    expect(dispatcherBody).toContain("journalFailureReporter");
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

  it("wires the multipart status aggregates into the live composition projection", () => {
    // Multipart task 11 fix: the closed multipart session-state histogram
    // and safe-reason codes MUST be read from the repository inside the
    // same fail-closed projection block and passed verbatim to the
    // projection — never a permanently zero/empty surface.
    const projectionHeaderIndex = pluginSource.indexOf(
      "#projectSyncStatus(): JournalSyncStatusSnapshot | null",
    );
    const projectionBody = pluginSource.slice(
      projectionHeaderIndex,
      projectionHeaderIndex + 2_600,
    );
    expect(projectionBody).toContain("repository.readMultipartSessionStateCounts()");
    expect(projectionBody).toContain("repository.readMultipartSafeReasonCodes()");
    // Assert the SHORTHAND pass-through inside the projectJournalSyncStatus
    // call (trailing comma), not the `let` declarations (colon + type): the
    // reads alone would still pass if the snapshot omitted the fields.
    const projectionCallIndex = projectionBody.indexOf("projectJournalSyncStatus({");
    expect(projectionCallIndex).toBeGreaterThanOrEqual(0);
    const projectionCallBody = projectionBody.slice(projectionCallIndex, projectionCallIndex + 900);
    expect(projectionCallBody).toContain("multipartSessionStateCounts,");
    expect(projectionCallBody).toContain("multipartSafeReasonCodes,");
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

  it("attaches a bounded rejection handler to the copy command (child six deferred remediation)", () => {
    const commandIndex = pluginSource.indexOf('id: "copy-sync-diagnostics"');
    const commandBody = pluginSource.slice(commandIndex, commandIndex + 400);
    // The fire-and-forget call carries a rejection handler, so a rejection
    // of the copy pipeline itself can never throw into UI processing.
    expect(commandBody).toContain("#copySyncDiagnostics().catch(");
    // The rejection reports through the established bounded diagnostics
    // pattern: ONE closed-token `self_check` trail entry.
    const recorderIndex = pluginSource.indexOf(
      "#recordDiagnosticsCopyFailureTrailEntry(): void",
    );
    expect(recorderIndex).toBeGreaterThanOrEqual(0);
    const recorderBody = pluginSource.slice(recorderIndex, recorderIndex + 900);
    expect(recorderBody).toContain('"self_check"');
    expect(recorderBody).toContain('"trail_persist_failed"');
    // Nothing is logged anywhere and the handler holds no clipboard data:
    // the closed token carries no detail of the failure.
    expect(commandBody).not.toContain("console.");
    expect(recorderBody).not.toContain("console.");
    expect(recorderBody).not.toContain("clipboard");
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
    // security-grade redacted lifecycle surface. The window grew with the
    // cleared-reason tombstone read (closed-reason surfacing C2 A3).
    const snapshotBuilderIndex = pluginSource.indexOf("getSnapshot: () => {");
    expect(snapshotBuilderIndex).toBeGreaterThanOrEqual(0);
    const snapshotBuilderBody = pluginSource.slice(
      snapshotBuilderIndex,
      snapshotBuilderIndex + 2_600,
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
    const deviceSyncStopIndex = pluginSource.indexOf("this.#syncCoordinator?.stop()");
    const automaticStopIndex = pluginSource.indexOf("this.#automaticSnapshotCoordinator?.stop()");
    const stopIndex = pluginSource.indexOf("this.#queueDriver?.stop()");
    const disposeIndex = pluginSource.indexOf("this.#capture?.dispose()");
    const flushIndex = pluginSource.indexOf("this.#journalPersistence?.attemptFinalFlush()");
    const closeIndex = pluginSource.indexOf("this.#journalPersistence?.close()");
    expect(deviceSyncStopIndex).toBeGreaterThanOrEqual(0);
    expect(automaticStopIndex).toBeGreaterThan(deviceSyncStopIndex);
    expect(stopIndex).toBeGreaterThan(automaticStopIndex);
    expect(disposeIndex).toBeGreaterThan(stopIndex);
    expect(flushIndex).toBeGreaterThan(disposeIndex);
    expect(closeIndex).toBeGreaterThan(flushIndex);
    expect(pluginSource).toContain(
      "Promise.all([deviceSyncCoordinatorStop, automaticSnapshotStop, boundedQueuePassStop, captureQuiescence])",
    );
    // Unload never awaits async journal work; the flush attempt stays
    // synchronous and bounded.
    expect(pluginSource).not.toContain("await this.#capture");
    expect(pluginSource).not.toContain("await this.#journalPersistence");
  });

  it("records the closed startup-failure stage instead of discarding it (closed-reason surfacing C1 P1)", () => {
    // The startup catch used to be a bare `catch {}`: whether the engine
    // load, the wasm read or the journal recovery failed reached nowhere.
    // It must now classify the failed stage into the closed token set and
    // surface the tokens on the trail AND the settings snapshot.
    const startMethodMatch = pluginSource.match(
      /async #startJournalCapture\(\): Promise<void> \{[\s\S]*?\n  \}\n/,
    );
    expect(startMethodMatch?.[0]).toBeTruthy();
    const startBody = startMethodMatch?.[0] ?? "";
    expect(startBody).toContain("catch (error)");
    expect(startBody).toContain("#lastStartupFailureTokens");
    expect(startBody).toContain("#appendStartupFailureTrailEntry");
    // The recorder appends the closed `startup_failure` trail kind.
    const recorderIndex = pluginSource.indexOf("#appendStartupFailureTrailEntry(tokens: readonly SyncDiagnosticClosedToken[]): void");
    expect(recorderIndex).toBeGreaterThanOrEqual(0);
    const recorderBody = pluginSource.slice(recorderIndex, recorderIndex + 700);
    expect(recorderBody).toContain('"startup_failure"');
    for (const stageToken of ["engine_load", "wasm_read", "journal_recovery"]) {
      expect(startBody).toContain(`"${stageToken}"`);
    }
    // The closed JournalStoreErrorReason rides along when the throw is a
    // store error; the exception text never enters the tokens.
    expect(startBody).toContain("JournalStoreError");
    expect(startBody).not.toContain("error.message");
    expect(startBody).not.toContain("String(error)");
    // The trail is created and loaded BEFORE the wasm read so every
    // startup stage can append its failure entry.
    const trailCreateIndex = startBody.indexOf("createSyncDiagnosticsTrail(");
    const wasmReadIndex = startBody.indexOf("#readJournalEngineWasmBinary()");
    expect(trailCreateIndex).toBeGreaterThanOrEqual(0);
    expect(wasmReadIndex).toBeGreaterThan(trailCreateIndex);
    // The settings snapshot carries the tokens (null before first failure).
    const snapshotIndex = pluginSource.indexOf("getSnapshot: () => {");
    const snapshotBody = pluginSource.slice(snapshotIndex, snapshotIndex + 3_400);
    expect(snapshotBody).toContain("lastStartupFailureTokens: this.#lastStartupFailureTokens");
  });

  it("renders the startup-failure tokens on the self-check journal-not-running verdict (closed-reason surfacing C1 P1)", () => {
    const methodMatch = pluginSource.match(
      /async #runSyncSelfCheck\(\): Promise<void> \{[\s\S]*?\n  \}\n/,
    );
    expect(methodMatch?.[0]).toBeTruthy();
    const methodBody = methodMatch?.[0] ?? "";
    expect(methodBody).toContain("renderSyncSelfCheckJournalNotRunningText");
    expect(methodBody).toContain("#lastStartupFailureTokens");
  });

  it("keeps the queue-pass wrapper summary honest on an unexpected requestPass throw (closed-reason surfacing C1 P2)", () => {
    const wrapperMatch = pluginSource.match(
      /async #runBoundedQueuePass\(\): Promise<QueuePassSummary> \{[\s\S]*?\n  \}\n/,
    );
    expect(wrapperMatch?.[0]).toBeTruthy();
    const wrapperBody = wrapperMatch?.[0] ?? "";
    const catchIndex = wrapperBody.indexOf("} catch {");
    expect(catchIndex).toBeGreaterThanOrEqual(0);
    const catchBody = wrapperBody.slice(catchIndex, catchIndex + 600);
    // The wrapper-level failure surfaces as the closed `pass_wrapper_failed`
    // outcome token — on the trail AND on the summary, never `completed`.
    expect(catchBody).toContain('"pass_outcome"');
    expect(catchBody).toContain("pass_wrapper_failed");
    expect(catchBody).not.toContain('outcome: "completed"');
    // The genuinely-idle driver-less early return stays `completed`-with-zero.
    const idleIndex = wrapperBody.indexOf("if (driver === null)");
    expect(idleIndex).toBeGreaterThanOrEqual(0);
    const idleBody = wrapperBody.slice(idleIndex, idleIndex + 200);
    expect(idleBody).toContain('outcome: "completed"');
  });

  it("carries the closed policy state on the settings snapshot (closed-reason surfacing C1 P3)", () => {
    const snapshotIndex = pluginSource.indexOf("getSnapshot: () => {");
    const snapshotBody = pluginSource.slice(snapshotIndex, snapshotIndex + 3_400);
    expect(snapshotBody).toContain("policyState: this.#policyState");
  });

  it("carries the durable tombstone cleared reason on the settings snapshot (closed-reason surfacing C2 A3)", () => {
    // A3: the terminal tombstone `ClearedReason` is durable in the SecretStorage
    // record but previously never reached the settings snapshot, so
    // "Revoked"/"Not connected" rendered no durable cause. The snapshot builder
    // must derive the closed enum value from the record — null-safe while no
    // tombstone exists, never a fake success token.
    const snapshotIndex = pluginSource.indexOf("getSnapshot: () => {");
    const snapshotBody = pluginSource.slice(snapshotIndex, snapshotIndex + 3_600);
    expect(snapshotBody).toContain("readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME)");
    expect(snapshotBody).toContain('state === "cleared"');
    expect(snapshotBody).toContain("clearedReason:");
    expect(snapshotBody).toContain("cleared_reason");
  });

  it("routes exceptional throws of the two fire-and-forget startup chains into the startup-failure path (closed-reason surfacing C1 P4)", () => {
    // Both startup chains keep their fire-and-forget shape (never awaited
    // in onload) but their catch handlers now feed the same startup_failure
    // recorder instead of discarding the throw.
    const resumeIndex = pluginSource.indexOf("void controller.resumePendingGrant()");
    expect(resumeIndex).toBeGreaterThanOrEqual(0);
    const resumeBody = pluginSource.slice(resumeIndex, resumeIndex + 300);
    expect(resumeBody).toContain(".catch(");
    expect(resumeBody).toContain("#recordStartupChainFailure");
    const refreshIndex = pluginSource.indexOf("void session.refresh()");
    expect(refreshIndex).toBeGreaterThanOrEqual(0);
    const refreshBody = pluginSource.slice(refreshIndex, refreshIndex + 700);
    expect(refreshBody).toContain(".catch(");
    expect(refreshBody).toContain("#recordStartupChainFailure");
  });

  it("records one bounded once-per-session token for the two swallowed composition reads (closed-reason surfacing C1 P5)", () => {
    // Pending-count read inside the automatic snapshot coordinator.
    const countIndex = pluginSource.indexOf("repository.countPendingEvents()");
    expect(countIndex).toBeGreaterThanOrEqual(0);
    const countBody = pluginSource.slice(countIndex, countIndex + 500);
    expect(countBody).toContain("#recordStatusReadFailureOnce");
    // Note-status read of the settings snapshot.
    const noteIndex = pluginSource.indexOf(
      "#readLocalNoteSyncStatuses(): readonly LocalNoteSyncStatus[]",
    );
    expect(noteIndex).toBeGreaterThanOrEqual(0);
    const noteBody = pluginSource.slice(noteIndex, noteIndex + 600);
    expect(noteBody).toContain("#recordNoteStatusReadFailureOnce");
    // Each site records AT MOST one trail entry per session (no per-render
    // spam) and rides the trail v2 composition_read_failure kind with its
    // closed stage and failure token (task 7 backlog remediation).
    const statusOnceIndex = pluginSource.indexOf("#recordStatusReadFailureOnce(): void");
    expect(statusOnceIndex).toBeGreaterThanOrEqual(0);
    const statusOnceBody = pluginSource.slice(statusOnceIndex, statusOnceIndex + 600);
    expect(statusOnceBody).toContain("#hasRecordedStatusReadFailure");
    expect(statusOnceBody).toContain("composition_read_failure");
    expect(statusOnceBody).toContain('"status_read"');
    expect(statusOnceBody).toContain('"status_read_failed"');
    const noteOnceIndex = pluginSource.indexOf("#recordNoteStatusReadFailureOnce(): void");
    expect(noteOnceIndex).toBeGreaterThanOrEqual(0);
    const noteOnceBody = pluginSource.slice(noteOnceIndex, noteOnceIndex + 600);
    expect(noteOnceBody).toContain("#hasRecordedNoteStatusReadFailure");
    expect(noteOnceBody).toContain("composition_read_failure");
    expect(noteOnceBody).toContain('"note_status_read"');
    expect(noteOnceBody).toContain('"note_status_read_failed"');
  });

  it("surfaces retry scheduling and sync-status composition read failures", () => {
    const retryReadIndex = pluginSource.indexOf("repository.readEarliestPendingRetryEpochMs()");
    expect(retryReadIndex).toBeGreaterThanOrEqual(0);
    const retryReadCatchBody = pluginSource.slice(retryReadIndex, retryReadIndex + 500);
    expect(retryReadCatchBody).toContain("#reportRetryScheduleReadFailureOnce");

    const statusReadIndex = pluginSource.indexOf("#projectSyncStatus(): JournalSyncStatusSnapshot | null");
    expect(statusReadIndex).toBeGreaterThanOrEqual(0);
    const statusReadCatchBody = pluginSource.slice(statusReadIndex, statusReadIndex + 2_000);
    expect(statusReadCatchBody).toContain("#reportSyncStatusReadFailureOnce");

    // Both once-per-session reporters ride the trail v2
    // composition_read_failure kind with their closed stage tokens.
    const retryOnceIndex = pluginSource.indexOf("#reportRetryScheduleReadFailureOnce(): void");
    expect(retryOnceIndex).toBeGreaterThanOrEqual(0);
    const retryOnceBody = pluginSource.slice(retryOnceIndex, retryOnceIndex + 700);
    expect(retryOnceBody).toContain("composition_read_failure");
    expect(retryOnceBody).toContain('"retry_schedule_read"');
    expect(retryOnceBody).toContain('"retry_schedule_read_failed"');
    const statusOnceIndex = pluginSource.indexOf("#reportSyncStatusReadFailureOnce(): void");
    expect(statusOnceIndex).toBeGreaterThanOrEqual(0);
    const statusOnceBody = pluginSource.slice(statusOnceIndex, statusOnceIndex + 700);
    expect(statusOnceBody).toContain("composition_read_failure");
    expect(statusOnceBody).toContain('"sync_status_read"');
    expect(statusOnceBody).toContain('"sync_status_read_failed"');
  });

  it("wires the durable trail into the lifecycle driver composition (trail v2 credential taxonomy)", () => {
    // The lifecycle lane's pre-contact credential absences report through
    // the same trail every other seam uses.
    const lifecycleDriverIndex = pluginSource.indexOf("new LifecycleDriverImpl({");
    expect(lifecycleDriverIndex).toBeGreaterThanOrEqual(0);
    const lifecycleDriverBody = pluginSource.slice(lifecycleDriverIndex, lifecycleDriverIndex + 900);
    expect(lifecycleDriverBody).toContain("diagnosticTrail");
  });

  it("composes the single device-sync coordinator after the journal and diagnostic trail are ready (task 12)", () => {
    // One coordinator owns every mutating foreground network phase: the
    // repository, wire client, applier and reconciler of tasks 8-11 bind
    // behind it, built only after the journal recovery and the trail load.
    const trailLoadIndex = pluginSource.indexOf("await diagnosticTrail.load()");
    const queueDriverIndex = pluginSource.indexOf("new JournalQueueDriver({");
    const coordinatorIndex = pluginSource.indexOf("createSyncCoordinator({");
    expect(trailLoadIndex).toBeGreaterThanOrEqual(0);
    expect(queueDriverIndex).toBeGreaterThan(trailLoadIndex);
    expect(coordinatorIndex).toBeGreaterThan(queueDriverIndex);
    const coordinatorBody = pluginSource.slice(coordinatorIndex, coordinatorIndex + 1_600);
    for (const requiredComposition of [
      "repository: deviceSyncRepository",
      "api: deviceSyncApi",
      "applier: remoteEventApplier",
      "reconciler: manifestReconciler",
      "outbound: boundedQueuePassDispatcher",
      "diagnostics: deviceSyncDiagnostics",
      "nowEpochMs: () => Date.now()",
      "isJournalReconcileRequired",
      "readManifestActionProgress",
    ]) {
      expect(coordinatorBody).toContain(requiredComposition);
    }
    // The Task 7 facade rides the SAME durable trail as every journal seam.
    const facadeIndex = pluginSource.indexOf("createDeviceSyncDiagnostics(");
    expect(facadeIndex).toBeGreaterThan(trailLoadIndex);
    expect(facadeIndex).toBeLessThan(coordinatorIndex);
    // The Task 9 wire client binds the Obsidian requestUrl surface through
    // the same transport family the other lanes use.
    expect(pluginSource).toContain("createObsidianDeviceSyncHttpTransport()");
    // The Task 10 vault seam binds through the structural app.vault
    // surface (no obsidian import inside src/device-sync); the composition
    // narrows Obsidian's richer member types at one named boundary.
    expect(pluginSource).toContain("createStructuralVaultMutationSeam(");
    expect(pluginSource).toContain("#createStructuralVaultSurfaceForDeviceSync()");
    // The outbound drain rides the SAME coalescing dispatcher every other
    // trigger uses; the coordinator never bypasses the bounded pass.
    expect(pluginSource).not.toContain("queueDriver.requestPass()");
  });

  it("registers the coordinator triggers: startup after layout, local commits, resume on visibility (task 12)", () => {
    // Startup convergence of the device cursor is requested inside the
    // layout-ready block, after the listener registration.
    const lastListenerIndex = pluginSource.lastIndexOf("this.registerEvent(");
    const startupRequestIndex = pluginSource.indexOf('this.#requestDeviceSyncCycle("startup")');
    expect(startupRequestIndex).toBeGreaterThan(lastListenerIndex);
    // Every settled Vault event listener forwards the local_commit trigger
    // to the coordinator alongside its bounded queue pass.
    expect(pluginSource.match(/#requestDeviceSyncCycle\("local_commit"\)/g)?.length ?? 0).toBe(4);
    // The resume trigger binds to the document visibility surface so a
    // device returning from suspension re-enters the cadence.
    expect(pluginSource).toContain('registerDomEvent(document, "visibilitychange"');
    expect(pluginSource).toContain('#requestDeviceSyncCycle("resume")');
    // The repair command routes through the same coordinator request.
    const repairCommandIndex = pluginSource.indexOf('id: "repair-sync"');
    expect(repairCommandIndex).toBeGreaterThanOrEqual(0);
    const repairCommandBody = pluginSource.slice(repairCommandIndex, repairCommandIndex + 260);
    expect(repairCommandBody).toContain('#requestDeviceSyncCycle("explicit_repair")');
  });

  it("persists the exchanged server device id and feeds it to the coordinator (fix round 1, blocker A)", () => {
    // The server mints origin_device_id as a uuid7 at grant exchange
    // (device_tokens.py) while client_instance_id is a client-minted v4
    // uuid — two disjoint namespaces. The self-origin evidence check must
    // bind the SERVER-minted device id the exchange delivers, persist it
    // so it survives restarts, and never fall back to client_instance_id.
    const exchangeIndex = pluginSource.indexOf("onExchange: async (exchange) => {");
    expect(exchangeIndex).toBeGreaterThanOrEqual(0);
    const exchangeBody = pluginSource.slice(exchangeIndex, exchangeIndex + 600);
    expect(exchangeBody).toContain("this.#settings.device_id = exchange.device_id");
    // The persist lands inside the awaited exchange handler, so the id is
    // durable before the Connected state.
    expect(exchangeBody).toContain("await this.#persistSettings()");
    // The persisted id round-trips through the settings document: a
    // restart re-parses it behind the same UUID gate as every identity.
    const normalizeIndex = pluginSource.indexOf("function normalizeSettings(");
    expect(normalizeIndex).toBeGreaterThanOrEqual(0);
    const normalizeBody = pluginSource.slice(normalizeIndex, normalizeIndex + 1_600);
    expect(normalizeBody).toContain('candidate["device_id"]');
    // The coordinator's self-origin identity is the server device id —
    // never the client_instance_id.
    const coordinatorIndex = pluginSource.indexOf("createSyncCoordinator({");
    expect(coordinatorIndex).toBeGreaterThanOrEqual(0);
    const coordinatorBody = pluginSource.slice(coordinatorIndex, coordinatorIndex + 1_800);
    expect(coordinatorBody).toContain("resolveOwnDeviceId: () => this.#settings.device_id");
    expect(coordinatorBody).not.toContain(
      "resolveOwnDeviceId: () => this.#settings.client_instance_id",
    );
  });

  it("normalizeSettings preserves a valid stored record name", () => {
    // Plugin hygiene (2026-08-16 §12): a valid stored record name
    // round-trips unchanged. The previous rewrite to the build-time
    // constant renamed every stored SecretStorage record on each load.
    const normalizeIndex = pluginSource.indexOf("function normalizeSettings(");
    expect(normalizeIndex).toBeGreaterThanOrEqual(0);
    const normalizeBody = pluginSource.slice(normalizeIndex, normalizeIndex + 1_700);
    expect(normalizeBody).toContain("isSecretRecordNameValid");
    expect(normalizeBody).toContain("secret_record_name: loadedRecordName,");
    expect(normalizeBody).not.toContain(
      "secret_record_name: loadedRecordName === null ? null : DEVICE_CREDENTIAL_RECORD_NAME,",
    );
  });

  it("reconcileCrashWindow saveData rejection is caught", () => {
    // Plugin hygiene (2026-08-16 §12): a settings-persist rejection during
    // the crash-window reconciliation must never abort onload — the closed
    // reason routes into the startup-failure trail path (the same journal
    // diagnostics surface the failure reporter feeds) and the bounded
    // startup chain continues.
    const reconcileIndex = pluginSource.indexOf("controller.reconcileCrashWindow()");
    expect(reconcileIndex).toBeGreaterThanOrEqual(0);
    const tryIndex = pluginSource.lastIndexOf("try {", reconcileIndex);
    expect(tryIndex).toBeGreaterThanOrEqual(0);
    expect(tryIndex).toBeLessThan(reconcileIndex);
    // The try block wraps exactly the reconciliation call.
    expect(reconcileIndex - tryIndex).toBeLessThan(120);
    const catchIndex = pluginSource.indexOf("} catch", reconcileIndex);
    expect(catchIndex).toBeGreaterThan(reconcileIndex);
    const catchBody = pluginSource.slice(catchIndex, catchIndex + 700);
    expect(catchBody).toContain("#recordStartupChainFailure(error)");
    // onload continues past the caught rejection.
    const refreshBranchIndex = pluginSource.indexOf(
      'if (startupAction === "refresh_credential")',
    );
    expect(refreshBranchIndex).toBeGreaterThan(catchIndex);
  });

  it("offers a retry affordance while offline with an active credential", () => {
    // Plugin hygiene (2026-08-16 §12): the offline dead-end (offline state
    // with a live credential, previously escapeable only by reloading the
    // plugin) gains ONE Retry connection command, enabled exactly in that
    // state; `canLogin` keeps its unchanged gating.
    const commandIndex = pluginSource.indexOf('id: "retry-connection"');
    expect(commandIndex).toBeGreaterThanOrEqual(0);
    const commandBody = pluginSource.slice(commandIndex, commandIndex + 500);
    expect(commandBody).toContain('"Retry connection"');
    expect(commandBody).toContain("checkCallback");
    expect(commandBody).toContain("#isRetryConnectionAvailable(secretStore)");
    expect(commandBody).toContain("#retryConnection(policySession, session)");
    // The gate is offline WITH an active credential.
    const gateDefinitionIndex = pluginSource.lastIndexOf(
      "#isRetryConnectionAvailable(",
    );
    expect(gateDefinitionIndex).toBeGreaterThan(commandIndex);
    const gateBody = pluginSource.slice(gateDefinitionIndex, gateDefinitionIndex + 400);
    expect(gateBody).toContain('"offline"');
    expect(gateBody).toContain("#resolveHasActiveCredential(secretStore)");
    // The retry re-invokes the bounded session refresh chain and routes an
    // exceptional rejection into the closed startup-failure trail path.
    const retryDefinitionIndex = pluginSource.indexOf("async #retryConnection(");
    expect(retryDefinitionIndex).toBeGreaterThanOrEqual(0);
    const retryBody = pluginSource.slice(retryDefinitionIndex, retryDefinitionIndex + 1_100);
    expect(retryBody).toContain("refreshVerifiedPolicyAndRequestSnapshot");
    expect(retryBody).toContain("#recordStartupChainFailure");
    // The settings tab exposes the same affordance as a button.
    expect(pluginSource).toContain("retryConnection: () =>");
  });

  it("carries the device-sync status onto the settings snapshot and the diagnostics export (task 12)", () => {
    const snapshotIndex = pluginSource.indexOf("getSnapshot: () => {");
    expect(snapshotIndex).toBeGreaterThanOrEqual(0);
    const snapshotBody = pluginSource.slice(snapshotIndex, snapshotIndex + 3_800);
    expect(snapshotBody).toContain("deviceSyncStatus:");
    // The read is fail-closed: a throwing projection never breaks the
    // settings render and reports the closed composition-read failure
    // instead of a stop reason.
    const readerIndex = pluginSource.indexOf("#readDeviceSyncStatus(): DeviceSyncStatus | null");
    expect(readerIndex).toBeGreaterThanOrEqual(0);
    const readerBody = pluginSource.slice(readerIndex, readerIndex + 900);
    expect(readerBody).toContain("#reportSyncStatusReadFailureOnce");
    // The export block renders the same closed status line.
    const builderIndex = pluginSource.indexOf("#buildSyncDiagnosticsExportBlock(): string");
    expect(builderIndex).toBeGreaterThanOrEqual(0);
    const builderBody = pluginSource.slice(builderIndex, builderIndex + 1_800);
    expect(builderBody).toContain("renderDeviceSyncStatusText");
    expect(builderBody).toContain("deviceSyncStatusLine");
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
