import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { writeLiveAcceptancePhaseStatus } from "../test/support/live-acceptance-phase-status";

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    fs.rmSync(directory, { force: true, recursive: true });
  }
});

describe("live acceptance phase status", () => {
  it("writes only one closed lifecycle result code", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "live-phase-"));
    temporaryDirectories.push(directory);
    const statusFile = path.join(directory, "knowledge-ci-test.obsidian-live-phase.json");

    writeLiveAcceptancePhaseStatus(statusFile, "source_lifecycle_move_completed");

    expect(JSON.parse(fs.readFileSync(statusFile, "utf8"))).toEqual({
      result_code: "source_lifecycle_move_completed",
    });
  });

  it("rejects a non-allowlisted runtime value before writing", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "live-phase-"));
    temporaryDirectories.push(directory);
    const statusFile = path.join(directory, "knowledge-ci-test.obsidian-live-phase.json");

    expect(() =>
      writeLiveAcceptancePhaseStatus(statusFile, "private-diagnostic" as never),
    ).toThrow("live acceptance phase result code was invalid");
    expect(fs.existsSync(statusFile)).toBe(false);
  });
});
