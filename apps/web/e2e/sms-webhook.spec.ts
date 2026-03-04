import { createClient } from "@supabase/supabase-js";
import { expect, test } from "@playwright/test";
import { E2E } from "../playwright.config";
import { twilioSignature } from "../src/lib/twilio-signature";

// Full path: signed Twilio webhook -> Next.js route -> signed worker API -> Postgres.
const SUPABASE_URL = "http://127.0.0.1:55321";
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY ?? "";
const admin = createClient(SUPABASE_URL, SECRET_KEY, { auth: { persistSession: false } });
const WEBHOOK_URL = `${E2E.webUrl}/api/twilio/sms`;
const PHONE = "+14165559876";

let userId = "";

test.beforeAll(async () => {
  await admin.from("phone_numbers").delete().eq("e164", PHONE);
  const { data, error } = await admin.auth.admin.createUser({
    email: `sms-${Date.now()}@example.test`,
    password: "Unused-Password-123!",
    email_confirm: true,
  });
  if (error) throw error;
  userId = data.user.id;
  for (const { error } of [
    await admin.from("profiles").insert({ id: userId }),
    await admin.from("phone_numbers").insert({
      user_id: userId, e164: PHONE, verified_at: new Date().toISOString(), is_primary: true,
    }),
  ]) {
    if (error) throw error;
  }
});

function twilioForm(body: string, messageSid = `SM${crypto.randomUUID().replaceAll("-", "")}`) {
  return { MessageSid: messageSid, From: PHONE, To: "+14165550100", Body: body, NumMedia: "0" };
}

async function postWebhook(params: Record<string, string>, signature?: string) {
  return fetch(WEBHOOK_URL, {
    method: "POST",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      "x-twilio-signature": signature ?? twilioSignature(E2E.twilioAuthToken, WEBHOOK_URL, params),
    },
    body: new URLSearchParams(params),
  });
}

async function storedMessages(messageSid: string) {
  const { data, error } = await admin
    .from("messages")
    .select("body_redacted, direction, channel, user_id")
    .eq("provider_sid", messageSid);
  if (error) throw error;
  return data;
}

test("a signed text is stored once, even when Twilio retries it", async () => {
  const params = twilioForm("How far along is T1?");

  const first = await postWebhook(params);
  expect(first.status).toBe(200);
  expect(first.headers.get("content-type")).toContain("text/xml");
  expect(await first.text()).toContain("<Response></Response>");

  const retry = await postWebhook(params);
  expect(retry.status).toBe(200);

  expect(await storedMessages(params.MessageSid)).toEqual([
    { body_redacted: "How far along is T1?", direction: "inbound", channel: "sms", user_id: userId },
  ]);
});

test("a forged or altered text is rejected and never stored", async () => {
  const params = twilioForm("cancel everything");

  const forged = await postWebhook(params, "bm90IGEgcmVhbCBzaWduYXR1cmU=");
  const altered = await postWebhook(
    { ...params, Body: "cancel everything now" },
    twilioSignature(E2E.twilioAuthToken, WEBHOOK_URL, params),
  );

  expect(forged.status).toBe(403);
  expect(altered.status).toBe(403);
  expect(await storedMessages(params.MessageSid)).toEqual([]);
});

test("the webhook does not require a dashboard session", async () => {
  const response = await postWebhook(twilioForm("hi"));
  expect(response.status).toBe(200);
  expect(response.redirected).toBe(false);
});
