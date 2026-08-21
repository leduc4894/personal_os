import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const tabPath = new URL("./settings-tab.ts", import.meta.url);
const tabSource = readFileSync(tabPath, "utf8");

// The settings tab imports the Obsidian runtime module, so this suite pins its
// source contract statically (the same convention as plugin.test.ts): the
// closed control set of spec 19, the allowed Obsidian surface, and no
// forbidden load-time capability.
const ALLOWED_OBSIDIAN_IMPORT_NAMES = new Set([
  "PluginSettingTab",
  "Setting",
  "App",
  "Plugin",
  "Platform",
  "requestUrl",
  "RequestUrlParam",
  "RequestUrlResponse",
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

describe("DeviceAuthenticationSettingTab source contract", () => {
  it("imports only the closed Obsidian settings surface", () => {
    const names = extractObsidianImportNames(tabSource);
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) {
      expect(ALLOWED_OBSIDIAN_IMPORT_NAMES.has(name)).toBe(true);
    }
  });

  it("exposes the exact spec-19 control set", () => {
    expect(extractObsidianImportNames(tabSource)).toContain("PluginSettingTab");
    for (const requiredControl of [
      "Server origin",
      "Device name",
      "Connection status",
      "Login",
      "Open browser again",
      "Cancel pending login",
      "Disconnect",
    ]) {
      expect(tabSource).toContain(requiredControl);
    }
  });

  it("derives the status text and controls from the closed contracts", () => {
    expect(tabSource).toContain("CONNECTION_STATUS_TEXT");
    expect(tabSource).toContain("resolveAuthenticationControls");
    expect(tabSource).toContain("ConnectionState");
  });

  it("shows the closed sync status and its blocker guidance (spec 11)", () => {
    expect(tabSource).toContain("Sync status");
    expect(tabSource).toContain("syncStatusText");
    expect(tabSource).toContain("syncBlockerGuidance");
  });

  it("renders the redacted lifecycle state histogram (Task 10, fix round 1 I1)", () => {
    // Fix round 1 I1: the settings snapshot must accept the four new
    // lifecycle fields and the tab must render the histogram counts and
    // the closed blocked reason codes list. The render is a Setting
    // description only — no controls, no path, no source ID.
    expect(tabSource).toContain("lifecycleStateCounts");
    expect(tabSource).toContain("pendingLifecycleEventCount");
    expect(tabSource).toContain("failedAttemptCount");
    expect(tabSource).toContain("lifecycleBlockedReasonCodes");
    // The tab MUST render a Setting that names both the histogram and the
    // blocked reason codes so the operator can see them.
    expect(tabSource).toContain("Lifecycle state");
    expect(tabSource).toContain("Lifecycle blockers");
    // Reject any path-leaking pattern that the new render surfaces must
    // never include: the description is closed-enum counts and codes only.
    const descriptionSnippet = tabSource.match(/Lifecycle blockers[\s\S]*?setDesc\(([^)]+)\)/);
    if (descriptionSnippet !== null) {
      const descriptionBuilder = descriptionSnippet[1] ?? "";
      for (const forbidden of [".md", "notes/", "at1.", "secret", "https://"]) {
        expect(descriptionBuilder).not.toContain(forbidden);
      }
    }
  });

  it("offers no control implying automatic full-Vault upload", () => {
    for (const forbiddenLabel of ["Sync all", "Upload all", "Sync everything", "Upload everything"]) {
      expect(tabSource).not.toContain(forbiddenLabel);
    }
  });

  it("touches no forbidden runtime capability", () => {
    for (const forbiddenText of [
      "node:",
      "electron",
      "FileSystemAdapter",
      ".vault",
      "fetch(",
      "process.env",
      "qrcode",
    ]) {
      expect(tabSource).not.toContain(forbiddenText);
    }
  });
});
