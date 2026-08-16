import { defineConfig } from "@playwright/test";

const WEB_PORT = 3100;
const WEB_BASE_URL = `http://127.0.0.1:${WEB_PORT}`;

export default defineConfig({
  testDir: "tests/end_to_end",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
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
