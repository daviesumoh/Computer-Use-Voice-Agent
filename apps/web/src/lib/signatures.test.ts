import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { sign } from "./service-signing.ts";
import { isValidTwilioSignature, twilioSignature } from "./twilio-signature.ts";

type ServiceVector = {
  secret: string; timestamp: number; method: string; path: string; body_utf8: string; signature: string;
};
type TwilioVector = { auth_token: string; url: string; params: Record<string, string>; signature: string };

const serviceVectors: ServiceVector[] = JSON.parse(
  readFileSync(new URL("../../../../services/common/signing_vectors.json", import.meta.url), "utf8"),
);
const twilioVectors: TwilioVector[] = JSON.parse(
  readFileSync(new URL("./fixtures/twilio-signature-vectors.json", import.meta.url), "utf8"),
);

test("service signatures match the Python implementation's vectors", () => {
  assert.ok(serviceVectors.length > 0);
  for (const v of serviceVectors) {
    assert.equal(sign(v.secret, v.timestamp, v.method, v.path, v.body_utf8), v.signature);
  }
});

test("Twilio signatures match Twilio's own library", () => {
  for (const v of twilioVectors) {
    assert.equal(twilioSignature(v.auth_token, v.url, v.params), v.signature);
    assert.equal(isValidTwilioSignature(v.auth_token, v.url, v.params, v.signature), true);
  }
});

test("Twilio validation rejects tampering, wrong URL, wrong token and missing header", () => {
  const v = twilioVectors[1];
  const tampered = { ...v.params, Body: "cancel T3" };
  assert.equal(isValidTwilioSignature(v.auth_token, v.url, tampered, v.signature), false);
  assert.equal(isValidTwilioSignature(v.auth_token, `${v.url}?x=1`, v.params, v.signature), false);
  assert.equal(isValidTwilioSignature("other-token", v.url, v.params, v.signature), false);
  assert.equal(isValidTwilioSignature(v.auth_token, v.url, v.params, null), false);
  assert.equal(isValidTwilioSignature(v.auth_token, v.url, v.params, "short"), false);
});
