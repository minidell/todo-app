import { defineConfig, devices } from '@playwright/test';

// E2E runs against a real Postgres database. Start with:
// docker compose up -d db
const backendPort = Number(process.env.E2E_BACKEND_PORT ?? 8010);
const backendTarget = `http://localhost:${backendPort}`;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: 'http://localhost:5173',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: [
    {
      command: `uv run uvicorn app.main:app --port ${backendPort}`,
      cwd: '../backend',
      url: `${backendTarget}/api/health`,
      reuseExistingServer: !process.env.CI,
      env: {
        // See .env.example for configuration
        DATABASE_URL:
          process.env.DATABASE_URL ??
          'postgresql+asyncpg://todo:todo@localhost:5433/todo',
        // Local E2E signing key — test-only, not a secret. Generate a real one
        // with `openssl rand -hex 32` for anything that is not this harness.
        JWT_SECRET:
          process.env.JWT_SECRET ??
          '0000000000000000000000000000000000000000000000000000000000000000',
        // Keeps argon2 hashing fast enough for E2E registration; the backend
        // only honours it when APP_ENV is dev.
        APP_ENV: process.env.APP_ENV ?? 'dev',
        TEST_ARGON2_FAST: process.env.TEST_ARGON2_FAST ?? '1',
        // The suite makes ~23 auth calls from one address. `reuseExistingServer`
        // keeps the in-process sliding window alive between runs, so the
        // production per-IP limit would 429 a second run inside 15 minutes.
        // The per-email limit still applies and stays exercised.
        AUTH_IP_RATE_LIMIT: process.env.AUTH_IP_RATE_LIMIT ?? '10000',
        // The backend refuses to start with AI_ENABLED=true and no
        // AI_AGENT_TOKEN, which is the default here — the AI stack is not part
        // of the standard E2E environment. Export AI_ENABLED=true together
        // with AI_AGENT_URL/AI_AGENT_TOKEN (and E2E_AI=1) to run ai.spec.ts.
        AI_ENABLED: process.env.AI_ENABLED ?? 'false',
      },
    },
    {
      command: 'npm run dev',
      url: 'http://localhost:5173',
      reuseExistingServer: !process.env.CI,
      env: { VITE_API_PROXY_TARGET: backendTarget },
    },
  ],
});
