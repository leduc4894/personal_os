import { defineConfig } from "@playwright/test";

// The web port stays 3100 by default; hosts whose TCP range excludes it
// (Windows Hyper-V exclusion windows) override it with the environment
// variable instead of weakening the gate.
const WEB_PORT = Number(process.env.PLAYWRIGHT_WEB_PORT ?? 3100);
const WEB_BASE_URL = `http://127.0.0.1:${WEB_PORT}`;

export default defineConfig({
  testDir: "tests/end_to_end",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  // CI emits a redacted JUnit report (opaque case names and pass/fail
  // durations only) beside the GitHub reporter; never traces or bodies.
  reporter: process.env.CI
    ? [
        ["github"],
        ["junit", { outputFile: "test-results/playwright-junit.xml" }],
      ]
    : "list",
  use: {
    baseURL: WEB_BASE_URL,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `pnpm --filter @workspace/web-runtime run build && pnpm --filter @workspace/web-runtime exec next start --port ${WEB_PORT}`,
    url: `${WEB_BASE_URL}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
