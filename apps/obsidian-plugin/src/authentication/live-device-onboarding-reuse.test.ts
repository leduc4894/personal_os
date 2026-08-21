import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, it } from "vitest";

const SPEC_PATHS = [
  "test/specs/device-login-sync.e2e.ts",
  "test/specs/source-lifecycle.e2e.ts",
] as const;

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
});
