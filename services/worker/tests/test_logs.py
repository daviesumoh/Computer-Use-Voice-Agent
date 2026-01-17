import io
import json
import logging

import pytest

from worker.logs import REDACTED, JsonFormatter, SecretRedactor, quiet_content_loggers

CANARY = "sk-canary-7f3a9c1e5b"


@pytest.fixture
def capture() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactor([CANARY]))
    # A child logger, as real modules use; the filter must still apply via the handler.
    logger = logging.getLogger("worker.test.child")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_secret_in_message_and_args_is_redacted(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    logger.info("token=%s and again %s", CANARY, f"prefix{CANARY}suffix")

    (entry,) = lines(stream)
    assert CANARY not in stream.getvalue()
    assert entry["msg"] == f"token={REDACTED} and again prefix{REDACTED}suffix"


def test_secret_in_extra_fields_is_redacted(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    logger.warning("provider call failed", extra={"request_body": f'{{"key": "{CANARY}"}}'})

    (entry,) = lines(stream)
    assert CANARY not in stream.getvalue()
    assert entry["request_body"] == f'{{"key": "{REDACTED}"}}'


def test_secret_in_exception_traceback_is_redacted(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    try:
        raise RuntimeError(f"auth failed for {CANARY}")
    except RuntimeError:
        logger.exception("login step failed")

    (entry,) = lines(stream)
    assert CANARY not in stream.getvalue()
    assert "RuntimeError: auth failed for [REDACTED]" in str(entry["exc"])


def test_overlapping_secrets_are_redacted_whole() -> None:
    redactor = SecretRedactor(["abcdefgh", "abcdefgh-ijklmnop"])
    assert redactor.scrub("x abcdefgh-ijklmnop y") == f"x {REDACTED} y"


def test_short_values_are_not_treated_as_secrets() -> None:
    # A 4-char "secret" would mangle ordinary words in every log line.
    redactor = SecretRedactor(["true"])
    assert redactor.scrub("enabled=true") == "enabled=true"


def test_two_handlers_both_see_redacted_record() -> None:
    first, second = io.StringIO(), io.StringIO()
    redactor = SecretRedactor([CANARY])
    logger = logging.getLogger("worker.test.two_handlers")
    logger.handlers = []
    logger.propagate = False
    for stream in (first, second):
        handler = logging.StreamHandler(stream)
        handler.addFilter(redactor)
        logger.addHandler(handler)

    logger.error("value %s", CANARY)

    assert CANARY not in first.getvalue()
    assert CANARY not in second.getvalue()
    assert REDACTED in second.getvalue()


def test_libraries_that_log_message_contents_stay_quiet_even_in_debug() -> None:
    # The takeover socket carries keystrokes; the websockets library logs frames at DEBUG.
    for name in ("websockets", "httpx", "openai"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    quiet_content_loggers()
    for name in ("websockets.server", "websockets.client", "httpx", "httpcore.http11", "openai"):
        assert not logging.getLogger(name).isEnabledFor(logging.DEBUG), name
