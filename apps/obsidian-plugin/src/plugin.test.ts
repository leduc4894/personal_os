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
    const startupCalls = pluginSource.match(/resumePendingGrant|session\.refresh/g) ?? [];
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

  it("registers exactly one command that alone runs the existing-files scan", () => {
    expect(pluginSource).toContain('id: "sync-existing-files"');
    expect(pluginSource.match(/addCommand\(/g)?.length ?? 0).toBe(1);
    // The bounded snapshot scan runs only through the confirmed command.
    const commandIndex = pluginSource.indexOf("addCommand(");
    const scanCallbackIndex = pluginSource.indexOf("void this.#runExistingFilesScan()");
    expect(scanCallbackIndex).toBeGreaterThan(commandIndex);
    // Startup itself never invokes the scan; only the command callback does.
    expect(pluginSource.match(/void this\.#runExistingFilesScan\(\)/g)?.length ?? 0).toBe(1);
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
