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

  it("pins the production origin policy to HTTPS-only", () => {
    expect(pluginSource).toContain("ALLOW_LOOPBACK_HTTP_ORIGIN = false");
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
      "addCommand",
      "registerEvent",
      ".vault",
      "qrcode",
      "@workspace/",
    ]) {
      expect(pluginSource).not.toContain(forbiddenText);
    }
  });
});
