import { defineConfig, devices } from "@playwright/test";

// These tests exercise the real stack, not mocks -- start
// it first (docker compose up -d, plus the backend per scripts/start.ps1
// on the host), then `pnpm e2e`. `workers: 1` / `fullyParallel: false`
// because tests share real backend/DB state (an incident's retry history,
// a workflow's version list) rather than each getting an isolated
// container the way the Python suite's testcontainers-per-test do.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 1, // a real dev stack's latency varies run to run; one retry absorbs that without masking real failures
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3001",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
