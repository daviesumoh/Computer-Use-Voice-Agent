"""HMAC signing for service-to-service requests (voice agent and web app -> worker).

The signature covers the timestamp, method, path and exact body bytes, so a captured request
can't be replayed against another endpoint or with a modified body. Replays of the identical
request within the skew window are possible; every signed endpoint is idempotent instead.
The TypeScript copy in apps/web must produce the same output: see signing_vectors.json.
"""

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Signature-Timestamp"
MAX_SKEW_SECONDS = 300
_VERSION = "v1"


class InvalidSignatureError(Exception):
    pass


def sign(secret: str, timestamp: int, method: str, path: str, body: bytes) -> str:
    message = f"{timestamp}\n{method.upper()}\n{path}\n".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"{_VERSION}={digest}"


def signed_headers(secret: str, method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = int(time.time())
    return {
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign(secret, timestamp, method, path, body),
    }


def verify(
    secret: str,
    *,
    timestamp_header: str | None,
    signature_header: str | None,
    method: str,
    path: str,
    body: bytes,
    now: float | None = None,
) -> None:
    """Raises InvalidSignatureError unless the request was signed with `secret` recently."""
    if not timestamp_header or not signature_header:
        raise InvalidSignatureError("missing signature headers")
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        raise InvalidSignatureError("timestamp is not an integer") from None
    current = time.time() if now is None else now
    if abs(current - timestamp) > MAX_SKEW_SECONDS:
        raise InvalidSignatureError("timestamp outside the allowed window")
    expected = sign(secret, timestamp, method, path, body)
    if not hmac.compare_digest(expected, signature_header):
        raise InvalidSignatureError("signature mismatch")
