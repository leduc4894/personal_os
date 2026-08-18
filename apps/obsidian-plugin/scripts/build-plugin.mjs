import { copyFile, mkdir, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";

const packageDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outputDirectory = path.join(packageDirectory, "dist");

// The vendored sql.js WebAssembly engine the journal loads lazily from the
// plugin directory (journal design 6.1): the bundle contract ships exactly
// main.js, manifest.json and this asset.
const SQLJS_WASM_ASSET_RELATIVE_PATH = "node_modules/sql.js/dist/sql-wasm.wasm";

await rm(outputDirectory, { recursive: true, force: true });
await mkdir(outputDirectory, { recursive: true });
await build({
  entryPoints: [path.join(packageDirectory, "src", "plugin.ts")],
  bundle: true,
  external: ["obsidian"],
  format: "cjs",
  platform: "browser",
  target: "es2022",
  sourcemap: false,
  outfile: path.join(outputDirectory, "main.js"),
});
await copyFile(
  path.join(packageDirectory, "manifest.json"),
  path.join(outputDirectory, "manifest.json"),
);
await copyFile(
  path.join(packageDirectory, SQLJS_WASM_ASSET_RELATIVE_PATH),
  path.join(outputDirectory, "sql-wasm.wasm"),
);
