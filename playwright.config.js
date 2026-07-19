const { defineConfig, devices } = require("@playwright/test");

const python = process.env.PYTHON || (process.platform === "win32" ? ".venv\\Scripts\\python.exe" : "python");

module.exports = defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  use: {
    baseURL: "http://127.0.0.1:8765",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: { ...devices["Desktop Chrome"], channel: "chromium", viewport: { width: 1440, height: 900 } },
      grepInvert: /@mobile/,
    },
    {
      name: "mobile-chromium",
      use: { ...devices["Pixel 5"], channel: "chromium" },
      grep: /@mobile/,
    },
  ],
  webServer: {
    command: `"${python}" -m uvicorn app.main:app --host 127.0.0.1 --port 8765`,
    url: "http://127.0.0.1:8765/health",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    env: {
      ...process.env,
      OPENAI_API_KEY: "test-key",
      SESSION_DB_PATH: ":memory:",
      LANGUAGE: "zh",
    },
  },
});
