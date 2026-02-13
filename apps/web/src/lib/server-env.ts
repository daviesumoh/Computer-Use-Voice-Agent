import "server-only";

/** Server-side configuration. Throws on first use if a value is missing, naming it. */
export function serverEnv(name: "TWILIO_AUTH_TOKEN" | "PUBLIC_BASE_URL" | "WORKER_URL" | "SERVICE_HMAC_SECRET") {
  const value = process.env[name];
  if (!value) throw new Error(`Missing server environment variable ${name}`);
  return value;
}
