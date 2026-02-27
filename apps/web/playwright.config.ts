import { defineConfig } from "@playwright/test";

const WEB_PORT = 3100;
const WORKER_PORT = 8100;

// Shared by both servers and the tests. Test-only values; the local stack is never deployed.
export const E2E = {
  webUrl: `http://localhost:${WEB_PORT}`,
  twilioAuthToken: "e2e-twilio-auth-token",
  serviceHmacSecret: "e2e-service-hmac-secret-0123456789abcdef",
};

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false, // tests share one Supabase user and its MFA factor
  workers: 1,
  use: { baseURL: E2E.webUrl },
  webServer: [
    {
      command: `pnpm next dev --port ${WEB_PORT}`,
      url: `${E2E.webUrl}/login`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        TWILIO_AUTH_TOKEN: E2E.twilioAuthToken,
        PUBLIC_BASE_URL: E2E.webUrl,
        WORKER_URL: `http://127.0.0.1:${WORKER_PORT}`,
        SERVICE_HMAC_SECRET: E2E.serviceHmacSecret,
        NEXT_PUBLIC_WORKER_URL: `http://127.0.0.1:${WORKER_PORT}`,
      },
    },
    {
      command: `uv run --no-sync uvicorn --factory worker.api:build --port ${WORKER_PORT}`,
      cwd: "../../services/worker",
      url: `http://127.0.0.1:${WORKER_PORT}/healthz`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        PYTHONPATH: "src:../common/src",
        WORKER_DATABASE_URL: "postgresql://agent_worker:local-dev-only@127.0.0.1:55322/postgres",
        WORKER_SERVICE_HMAC_SECRET: E2E.serviceHmacSecret,
        WORKER_TWILIO_ACCOUNT_SID: `AC${"0".repeat(32)}`,
        WORKER_TWILIO_API_KEY_SID: `SK${"0".repeat(32)}`,
        WORKER_TWILIO_API_KEY_SECRET: "unused-in-e2e",
        WORKER_TWILIO_FROM_NUMBER: "+14165550100",
        WORKER_DASHBOARD_URL: E2E.webUrl,
        WORKER_SUPABASE_URL: "http://127.0.0.1:55321",
        WORKER_SUPABASE_PUBLISHABLE_KEY: process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? "unset",
      },
    },
  ],
});
