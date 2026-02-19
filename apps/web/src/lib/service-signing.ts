import { createHmac } from "node:crypto";

// Must match services/common/src/agent_common/signing.py; both are checked against
// services/common/signing_vectors.json.
export const SIGNATURE_HEADER = "X-Signature";
export const TIMESTAMP_HEADER = "X-Signature-Timestamp";

export function sign(secret: string, timestamp: number, method: string, path: string, body: string) {
  const message = `${timestamp}\n${method.toUpperCase()}\n${path}\n${body}`;
  return `v1=${createHmac("sha256", secret).update(message, "utf8").digest("hex")}`;
}

export function signedHeaders(secret: string, method: string, path: string, body: string) {
  const timestamp = Math.floor(Date.now() / 1000);
  return {
    [TIMESTAMP_HEADER]: String(timestamp),
    [SIGNATURE_HEADER]: sign(secret, timestamp, method, path, body),
  };
}
