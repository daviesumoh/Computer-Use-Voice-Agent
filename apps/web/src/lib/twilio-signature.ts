import { createHmac, timingSafeEqual } from "node:crypto";

/**
 * Twilio's webhook signature: base64 HMAC-SHA1 over the full URL Twilio called followed by
 * every POST parameter as name+value, sorted by name. Keyed with the account Auth Token.
 * Checked against vectors produced by Twilio's own library (fixtures/twilio-signature-vectors.json).
 */
export function twilioSignature(authToken: string, url: string, params: Record<string, string>) {
  const payload = Object.keys(params)
    .sort()
    .reduce((acc, name) => acc + name + params[name], url);
  return createHmac("sha1", authToken).update(payload, "utf8").digest("base64");
}

export function isValidTwilioSignature(
  authToken: string,
  url: string,
  params: Record<string, string>,
  signature: string | null,
): boolean {
  if (!signature) return false;
  const expected = Buffer.from(twilioSignature(authToken, url, params));
  const given = Buffer.from(signature);
  return expected.length === given.length && timingSafeEqual(expected, given);
}
