import { defineConfig } from "vitest/config";

export default defineConfig({
  // The app tsconfig keeps Next's `jsx: "preserve"`; tests need the automatic runtime.
  oxc: { jsx: { runtime: "automatic" } },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    setupFiles: ["./vitest.setup.ts"],
    passWithNoTests: false,
    coverage: { provider: "v8", reporter: ["text", "json-summary"] },
  },
});
