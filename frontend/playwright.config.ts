import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  use: { baseURL: process.env.LYNEWRIGHT_SCREENSHOT_BASE_URL ?? "http://127.0.0.1:5173", trace: "retain-on-failure" },
  outputDir: process.env.LYNEWRIGHT_E2E_OUTPUT_DIR ?? "./test-results",
});
