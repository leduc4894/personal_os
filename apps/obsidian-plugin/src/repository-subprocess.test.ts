import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { runFromE2eRepositoryRoot } from "../test/support/repository-subprocess";

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    fs.rmSync(directory, { force: true, recursive: true });
  }
});

describe("repository subprocess contract", () => {
  it("runs from the repository owning an arbitrarily located E2E spec", async () => {
    const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "repository-command-"));
    temporaryDirectories.push(temporaryDirectory);
    const repositoryRoot = path.join(temporaryDirectory, "portable-checkout");
    const specPath = path.join(
      repositoryRoot,
      "apps",
      "obsidian-plugin",
      "test",
      "specs",
      "device-login-sync.e2e.ts",
    );
    fs.mkdirSync(path.dirname(specPath), { recursive: true });
    fs.writeFileSync(path.join(repositoryRoot, "controlled-marker"), "portable", "utf8");

    const { stdout } = await runFromE2eRepositoryRoot(
      process.execPath,
      [
        "-e",
        "process.stdout.write(require('node:fs').readFileSync('controlled-marker', 'utf8'))",
      ],
      pathToFileURL(specPath).href,
    );

    expect(stdout).toBe("portable");
  });

  it("terminates evidence commands at the caller-owned timeout", async () => {
    const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "repository-timeout-"));
    temporaryDirectories.push(temporaryDirectory);
    const specPath = path.join(
      temporaryDirectory,
      "portable-checkout",
      "apps",
      "obsidian-plugin",
      "test",
      "specs",
      "device-login-sync.e2e.ts",
    );
    fs.mkdirSync(path.dirname(specPath), { recursive: true });

    await expect(
      runFromE2eRepositoryRoot(
        process.execPath,
        ["-e", "setInterval(() => undefined, 1000)"],
        pathToFileURL(specPath).href,
        process.env,
        50,
      ),
    ).rejects.toMatchObject({ killed: true });
  });
});
