import { signedHeaders } from "@/lib/service-signing";
import { serverEnv } from "@/lib/server-env";
import { isValidTwilioSignature } from "@/lib/twilio-signature";

const PATH = "/api/twilio/sms";
const WORKER_PATH = "/v1/sms/inbound";
const EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>';

/**
 * Twilio inbound-SMS webhook. Verifies Twilio's signature, then hands the message to the
 * worker, which stores it (deduplicated by MessageSid) and decides on any reply.
 */
export async function POST(request: Request) {
  const form = await request.formData();
  const params: Record<string, string> = {};
  form.forEach((value, key) => {
    if (typeof value === "string") params[key] = value;
  });

  // The URL Twilio signed is our public one; never trust the Host header for it.
  const publicUrl = new URL(PATH, serverEnv("PUBLIC_BASE_URL")).toString();
  const signature = request.headers.get("x-twilio-signature");
  if (!isValidTwilioSignature(serverEnv("TWILIO_AUTH_TOKEN"), publicUrl, params, signature)) {
    console.warn("twilio sms webhook: invalid signature");
    return new Response("invalid signature", { status: 403 });
  }

  const body = JSON.stringify({
    message_sid: params.MessageSid,
    from_number: params.From,
    body: params.Body ?? "",
  });
  const response = await fetch(new URL(WORKER_PATH, serverEnv("WORKER_URL")), {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...signedHeaders(serverEnv("SERVICE_HMAC_SECRET"), "POST", WORKER_PATH, body),
    },
    body,
    signal: AbortSignal.timeout(8_000),
  });
  if (!response.ok) {
    // 5xx makes Twilio log the failure and use its fallback URL if one is configured.
    console.error("twilio sms webhook: worker rejected message", response.status, await response.text());
    return new Response("worker unavailable", { status: 502 });
  }

  return new Response(EMPTY_TWIML, { headers: { "content-type": "text/xml" } });
}
