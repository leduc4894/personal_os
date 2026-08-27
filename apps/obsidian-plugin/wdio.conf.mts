import * as fs from "node:fs";
import * as path from "node:path";
import type { WebdriverIOConfig } from "wdio-obsidian-service";

/**
 * End-to-end configuration for the real-Obsidian plugin harness
 * (wdio-obsidian-service). The launcher downloads an isolated Obsidian
 * build into `.obsidian-cache` and opens a copy of the fixture vault —
 * never the operator's personal vault or Obsidian data directory.
 */
export const config: WebdriverIOConfig = {
  runner: "local",
  framework: "mocha",
  specs: ["./test/specs/**/*.e2e.ts"],
  maxInstances: 1,
  capabilities: [
    {
      browserName: "obsidian",
      browserVersion: "latest",
      "wdio:obsidianOptions": {
        installerVersion: "earliest",
        plugins: ["dist"],
        vault: "test/vaults/simple",
      },
    },
  ],
  services: ["obsidian"],
  reporters: ["obsidian"],
  cacheDir: path.resolve(".obsidian-cache"),
  // The device-sync reconciliation journey (Child 6 live gate) chains four
  // scenarios with two manifest repairs in one test; its per-test timeouts
  // stay authoritative, and the suite default covers the shorter journeys.
  mochaOpts: { ui: "bdd", timeout: 780_000 },
  logLevel: "warn",
  /**
   * The launcher installs only manifest.json, main.js, styles.css and
   * data.json of a local plugin (the standard Obsidian distribution set),
   * so the sql.js engine binary must ride along inside the fixture vault
   * the service copies to its temporary directory. The plugin settings
   * point the harness at the local disposable API server.
   */
  onPrepare: () => {
    const fixturePluginDirectory = path.resolve(
      "test/vaults/simple/.obsidian/plugins/knowledge-workspace",
    );
    fs.mkdirSync(fixturePluginDirectory, { recursive: true });
    fs.copyFileSync(
      path.resolve("dist/sql-wasm.wasm"),
      path.join(fixturePluginDirectory, "sql-wasm.wasm"),
    );
    fs.writeFileSync(
      path.join(fixturePluginDirectory, "data.json"),
      JSON.stringify({
        // The plugin refuses loopback HTTP origins (ALLOW_LOOPBACK_HTTP_ORIGIN
        // is false), so the harness vault points at the public API origin.
        server_origin: process.env.E2E_PLUGIN_ORIGIN ?? "https://api.ducinvest.com",
      }),
    );
  },
};
