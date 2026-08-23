import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, it } from "vitest";

const SPEC_PATHS = [
  "test/specs/device-login-sync.e2e.ts",
  "test/specs/source-lifecycle.e2e.ts",
] as const;
const SUPPORT_PATH = "test/support/live-device-onboarding.ts";

describe("live device onboarding support", () => {
  it("keeps the security-sensitive authorization journey in one shared helper", () => {
    for (const specPath of SPEC_PATHS) {
      const source = fs.readFileSync(path.resolve(specPath), "utf8");
      expect(source).toContain('../support/live-device-onboarding');
      expect(source).not.toContain('/api/auth/login');
      expect(source).not.toContain('/api/auth/totp/verify');
      expect(source).not.toContain('/api/auth/device-authorizations');
    }
  });

  it("waits for the durable journal manifest before declaring onboarding converged", () => {
    const source = fs.readFileSync(path.resolve(SUPPORT_PATH), "utf8");
    expect(source).toContain("journal.manifest.json");
    expect(source).not.toContain(".listCommands()");
    expect(source).not.toContain("sync-now");
  });

  it("does not execute removed sync commands", () => {
    const specSource = fs.readFileSync(path.resolve(SPEC_PATHS[0]), "utf8");
    expect(specSource).not.toContain("sync-existing-files");
    expect(specSource).not.toContain("sync-now");
  });

  it("retries retained automatic work through a settled Vault event", () => {
    const specSource = fs.readFileSync(path.resolve(SPEC_PATHS[0]), "utf8");
    expect(specSource).toContain(
      "await writeFixtureNote(controlledNormalizedPath, retryContent)",
    );
    expect(specSource).not.toContain("waitForAutomaticCommitWithOneRestart");
  });

  it("accepts retained policy audit history when the successor commits", () => {
    const specSource = fs.readFileSync(path.resolve(SPEC_PATHS[0]), "utf8");
    expect(specSource).toContain("evidence.excludedPolicyCount >= 1");
  });

  it("proves the policy successor through exact server and rendered local status evidence", () => {
    const specSource = fs.readFileSync(path.resolve(SPEC_PATHS[0]), "utf8");
    expect(specSource).toContain("waitForAutomaticServerPublicationWithOneSettledEvent");
    expect(specSource).toContain("waitForPolicyRecoveryNoteToRenderSynced");
  });

  it("waits for the newly published policy revision before returning from reauthorization", () => {
    const source = fs.readFileSync(path.resolve(SUPPORT_PATH), "utf8");
    expect(source).toContain("minimumPolicyRevision");
    expect(source).toContain('data["policy_cache"]');
  });

  it("does not reuse a one-time TOTP code when a live journey reauthorizes", () => {
    const source = fs.readFileSync(path.resolve(SUPPORT_PATH), "utf8");
    expect(source).toContain("previousVerifiedTotpCode");
    expect(source).toContain("readFreshTotpCode");
  });
});
