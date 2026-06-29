import { createClient } from "@/lib/supabase/client";

const WORKER_URL = process.env.NEXT_PUBLIC_WORKER_URL!;

export type WorkerResult<T = unknown> =
  | { ok: true; data: T }
  | { ok: false; status: number; detail: string };

/** What the worker's `detail` means to a person. */
export function explain(result: { status: number; detail: string }): string {
  if (result.detail === "mfa_stale") return "Enter a fresh code from your authenticator app and try again.";
  if (result.detail === "mfa_required") return "Sign in with your authenticator code first.";
  if (result.status === 401) return "Your session expired. Sign in again.";
  return result.detail || `The request failed (${result.status}).`;
}

/** Proves the owner holds their authenticator now, which sensitive worker actions require. */
export async function confirmWithCode(code: string): Promise<string | null> {
  const mfa = createClient().auth.mfa;
  const { data, error } = await mfa.listFactors();
  if (error) return error.message;
  const factor = data.totp[0];
  if (!factor) return "No authenticator is set up for this account.";
  const verified = await mfa.challengeAndVerify({ factorId: factor.id, code: code.trim() });
  return verified.error ? verified.error.message : null;
}

async function accessToken(): Promise<string | null> {
  const { data } = await createClient().auth.getSession();
  return data.session?.access_token ?? null;
}

/** Calls the worker directly from the browser, authenticated as the signed-in owner. */
export async function callWorker<T = unknown>(
  path: string,
  body?: unknown,
): Promise<WorkerResult<T>> {
  const token = await accessToken();
  if (!token) return { ok: false, status: 401, detail: "not signed in" };
  const response = await fetch(`${WORKER_URL}${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (response.status === 204) return { ok: true, data: undefined as T };
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : "";
    return { ok: false, status: response.status, detail };
  }
  return { ok: true, data: payload as T };
}

/** The WebSocket address for a takeover token. */
export function takeoverSocketUrl(token: string): string {
  return `${WORKER_URL.replace(/^http/, "ws")}/v1/takeover/${token}`;
}
