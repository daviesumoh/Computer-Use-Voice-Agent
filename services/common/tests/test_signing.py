import json
from pathlib import Path

import pytest

from agent_common.signing import InvalidSignatureError, sign, signed_headers, verify

VECTORS = json.loads((Path(__file__).parents[1] / "signing_vectors.json").read_text())
SECRET = "test-secret"
BODY = b'{"body":"pause T3"}'


@pytest.mark.parametrize("vector", VECTORS)
def test_matches_shared_vectors(vector: dict[str, object]) -> None:
    # The TypeScript implementation in apps/web is checked against the same file.
    assert (
        sign(
            str(vector["secret"]),
            int(str(vector["timestamp"])),
            str(vector["method"]),
            str(vector["path"]),
            str(vector["body_utf8"]).encode(),
        )
        == vector["signature"]
    )


def verify_with(**overrides: object) -> None:
    timestamp = 1_794_380_000
    args: dict[str, object] = {
        "timestamp_header": str(timestamp),
        "signature_header": sign(SECRET, timestamp, "POST", "/v1/tasks", BODY),
        "method": "POST",
        "path": "/v1/tasks",
        "body": BODY,
        "now": timestamp + 10,
    }
    args.update(overrides)
    verify(SECRET, **args)  # type: ignore[arg-type]


def test_valid_request_passes() -> None:
    verify_with()


@pytest.mark.parametrize(
    "overrides",
    [
        {"body": b'{"body":"cancel T3"}'},  # tampered body
        {"path": "/v1/sms/inbound"},  # replayed against another endpoint
        {"method": "PUT"},
        {"now": 1_794_380_000 + 301},  # too old
        {"now": 1_794_380_000 - 301},  # from the future
        {"signature_header": "v1=" + "0" * 64},
        {"signature_header": None},
        {"timestamp_header": "not-a-number"},
    ],
)
def test_rejected_requests(overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidSignatureError):
        verify_with(**overrides)


def test_wrong_secret_is_rejected() -> None:
    headers = signed_headers("other-secret", "POST", "/v1/tasks", BODY)
    with pytest.raises(InvalidSignatureError, match="mismatch"):
        verify(
            SECRET,
            timestamp_header=headers["X-Signature-Timestamp"],
            signature_header=headers["X-Signature"],
            method="POST",
            path="/v1/tasks",
            body=BODY,
        )
