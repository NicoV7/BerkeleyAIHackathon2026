import { defineConfig, devices } from "@playwright/test";

// End-to-end test config. Specs live under ./e2e. Assumes the dev stack is
// already running at http://localhost:5173 at integration time (no webServer
// entry — uncomment below to have Playwright boot the dev server itself).
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://localhost:5173",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // webServer: {
  //   command: "pnpm dev",
  //   url: "http://localhost:5173",
  //   reuseExistingServer: !process.env.CI,
  // },
});
