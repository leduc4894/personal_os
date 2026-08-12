import { readFileSync } from "node:fs";
import * as ts from "typescript";
import { describe, expect, it } from "vitest";

const pluginPath = new URL("./plugin.ts", import.meta.url);
const pluginSource = readFileSync(pluginPath, "utf8");
const sourceFile = ts.createSourceFile("plugin.ts", pluginSource, ts.ScriptTarget.Latest, true);

describe("Obsidian bootstrap lifecycle", () => {
  it("contains only empty load and unload methods", () => {
    const pluginClass = sourceFile.statements.find(ts.isClassDeclaration);
    expect(pluginClass).toBeDefined();
    const methods = pluginClass?.members.filter(ts.isMethodDeclaration) ?? [];
    const methodNames = methods.map((method) => method.name.getText(sourceFile));
    expect(methodNames).toEqual(["onload", "onunload"]);
    expect(methods.every((method) => method.body?.statements.length === 0)).toBe(true);
  });

  it("does not register product behavior or access runtime data", () => {
    for (const forbiddenText of [
      "addCommand",
      "addRibbonIcon",
      "registerEvent",
      ".vault",
      "requestUrl",
      "fetch(",
      "process.env",
    ]) {
      expect(pluginSource).not.toContain(forbiddenText);
    }
  });
});
