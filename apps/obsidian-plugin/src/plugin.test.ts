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
    // Policy refresh happens only after a successful token refresh.
    const refreshIndex = pluginSource.indexOf("session.refresh");
    const policyRefreshIndex = pluginSource.indexOf("policySession.refresh");
    expect(refreshIndex).toBeGreaterThanOrEqual(0);
    expect(policyRefreshIndex).toBeGreaterThan(refreshIndex);
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

  it("registers exactly the three safe-source commands and no other", () => {
    expect(pluginSource).toContain('id: "sync-now"');
    expect(pluginSource).toContain('id: "sync-existing-files"');
    expect(pluginSource).toContain('id: "restore-selected-tombstone"');
    expect(pluginSource.match(/addCommand\(/g)?.length ?? 0).toBe(3);
    // The bounded snapshot scan runs only through the confirmed command.
    const commandIndex = pluginSource.indexOf("addCommand(");
    const scanCallbackIndex = pluginSource.indexOf("void this.#runExistingFilesScan()");
    expect(scanCallbackIndex).toBeGreaterThan(commandIndex);
    // Startup itself never invokes the scan; only the command callback does.
    expect(pluginSource.match(/void this\.#runExistingFilesScan\(\)/g)?.length ?? 0).toBe(1);
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

  it("Sync now schedules a driver pass without bypassing the one-active-request guarantee", () => {
    // The Sync now command MUST funnel through the bounded-pass wrapper
    // so the driver's one-active-request invariant and bounded retry
    // backoff are preserved.
    const syncNowIndex = pluginSource.indexOf('id: "sync-now"');
    const syncNowBodyStart = pluginSource.indexOf("{", syncNowIndex);
    const syncNowBodyEnd = pluginSource.indexOf("});", syncNowBodyStart);
    const syncNowBody = pluginSource.slice(syncNowBodyStart, syncNowBodyEnd);
    expect(syncNowBody).toContain("#runBoundedQueuePass");
    expect(syncNowBody).not.toContain("requestPass()");
    expect(syncNowBody).not.toContain("runPass()");
    // The Sync now callback is fire-and-forget; no awaiting, no synchronous
    // bookkeeping that could let a user-trigger bypass the bounded backoff.
    expect(syncNowBody).not.toContain("await ");
  });

  it("wires the bounded foreground queue driver behind the sync commands", () => {
    expect(pluginSource).toContain("new JournalQueueDriver(");
    expect(pluginSource).toContain("createJournalSyncApi(");
    expect(pluginSource).toContain("createObsidianSyncHttpTransport");
    // The driver runs over the SAME read-only vault reader as capture and
    // refreshes through the existing device token session.
    expect(pluginSource).toContain("fileBytesReader: vaultReader");
    expect(pluginSource).toContain("refreshAccessToken: () => session.refresh()");
    // Every pass trigger funnels through the single bounded-pass wrapper so
    // the status projection sees the active pass and each pass outcome.
    const triggerCount = pluginSource.match(/void this\.#runBoundedQueuePass\(\)/g)?.length ?? 0;
    expect(triggerCount).toBe(4); // load, create listener, modify listener, Sync now
    expect(pluginSource).not.toContain("queueDriver.requestPass()");
    // A Vault event's pass runs only after its own settled admission landed
    // (the 250 ms settle re-reads bytes); an event-time pass would find the
    // journal still empty and leave the event pending until the next trigger.
    expect(pluginSource.match(/notifyPathChanged\(file\.path\)\.then\(/g)?.length ?? 0).toBe(2);
    // The wrapper itself is never awaited by onload, a listener or a command.
    expect(pluginSource.match(/await this\.#runBoundedQueuePass\(/g)?.length ?? 0).toBe(0);
    // A Vault event triggers one bounded pass alongside capture.
    const createListenerIndex = pluginSource.indexOf('this.app.vault.on("create"');
    const passIndex = pluginSource.indexOf("void this.#runBoundedQueuePass()");
    expect(passIndex).toBeGreaterThan(createListenerIndex);
    // Plugin load after recovery is the first trigger, fire-and-forget.
    const recoveryIndex = pluginSource.indexOf("await persistence.open()");
    const loadPassIndex = pluginSource.indexOf(
      "void this.#runBoundedQueuePass();",
      pluginSource.indexOf("this.#queueDriver = queueDriver"),
    );
    expect(loadPassIndex).toBeGreaterThan(recoveryIndex);
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

  it("resolves explicit-restore modal acceptance before close can reject it", () => {
    // Obsidian invokes `onClose` synchronously from `close()`. Each modal
    // therefore has to publish its accepted value first; otherwise the
    // awaiting restore command observes the dismissal fallback (`null` or
    // `false`) and never dispatches the lifecycle event.
    expect(pluginSource).toContain("this.onChooseItem(item);\n        this.close();");
    expect(
      pluginSource.match(/this\.#accept\(this\.#inputValue\);\n\s+this\.close\(\);/g)?.length ?? 0,
    ).toBe(2);
    expect(pluginSource).toContain("this.#accept();\n            this.close();");
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
    const stopIndex = pluginSource.indexOf("this.#queueDriver?.stop()");
    const disposeIndex = pluginSource.indexOf("this.#capture?.dispose()");
    const flushIndex = pluginSource.indexOf("this.#journalPersistence?.attemptFinalFlush()");
    const closeIndex = pluginSource.indexOf("this.#journalPersistence?.close()");
    expect(stopIndex).toBeGreaterThanOrEqual(0);
    expect(disposeIndex).toBeGreaterThan(stopIndex);
    expect(flushIndex).toBeGreaterThan(disposeIndex);
    expect(closeIndex).toBeGreaterThan(flushIndex);
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
