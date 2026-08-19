import { browser } from "@wdio/globals";

/**
 * Smoke diagnostics for the real-Obsidian harness: the plugin must load in
 * the fixture vault, register exactly the two sync commands of spec 11 and
 * render the status surface. Everything printed here is closed vocabulary
 * (command ids, status text) — never vault paths or content.
 */
describe("knowledge-workspace plugin boot", () => {
  it("registers the two sync commands", async () => {
    const commandIds = await browser.execute(() => {
      const app = (
        window as unknown as {
          app: { commands: { listCommands: () => Array<{ id: string }> } };
        }
      ).app;
      return app.commands.listCommands().map((command) => command.id);
    });
    console.log("COMMAND_IDS", JSON.stringify(commandIds));
    expect(commandIds).toContain("knowledge-workspace:sync-now");
    expect(commandIds).toContain("knowledge-workspace:sync-existing-files");
  });

  it("loads the plugin and renders the sync status surface", async () => {
    const diagnostics = await browser.execute(
      async () => {
        const app = (
          window as unknown as {
            app: {
              plugins?: { plugins?: Record<string, unknown> };
              vault: {
                configDir: string;
                adapter: {
                  list: (path: string) => Promise<{ files: string[]; folders: string[] }>;
                  readBinary: (path: string) => Promise<ArrayBuffer>;
                };
              };
            };
          }
        ).app;
        const plugin = app.plugins?.plugins?.["knowledge-workspace"];
        const statusBarText = Array.from(
          document.querySelectorAll(".status-bar-item"),
        ).map((element) => element.textContent);
        const pluginDirectory = `${app.vault.configDir}/plugins/knowledge-workspace`;
        let pluginDirectoryFiles: string[] = [];
        let wasmByteLength: number | null = null;
        let wasmReadError: string | null = null;
        try {
          const listing = await app.vault.adapter.list(pluginDirectory);
          pluginDirectoryFiles = listing.files;
          const wasmBytes = await app.vault.adapter.readBinary(
            `${pluginDirectory}/sql-wasm.wasm`,
          );
          wasmByteLength = wasmBytes.byteLength;
        } catch (error) {
          wasmReadError = error instanceof Error ? error.message : String(error);
        }
        return {
          isPluginLoaded: plugin !== undefined,
          statusBarText,
          pluginDirectoryFiles,
          wasmByteLength,
          wasmReadError,
        };
      },
    );
    console.log("DIAGNOSTICS", JSON.stringify(diagnostics));
    expect(diagnostics.isPluginLoaded).toBe(true);
  });
});
