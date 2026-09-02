import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "test/specs/source-conflict-resolution.e2e.ts"],
    passWithNoTests: false,
    coverage: { provider: "v8", reporter: ["text", "json-summary"] },
  },
});
