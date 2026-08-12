import { copyFile, mkdir, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";

const packageDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outputDirectory = path.join(packageDirectory, "dist");

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
