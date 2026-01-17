"""JSON logging with secret redaction applied before any handler writes a record."""

import json
import logging
import sys

import logfire

from worker.settings import Settings

REDACTED = "[REDACTED]"

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD_RECORD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

# Shorter values are too likely to collide with ordinary text to redact safely.
_MIN_SECRET_LENGTH = 8


class SecretRedactor(logging.Filter):
    """Replaces known secret values anywhere in a record: message, args, extras, traceback."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Longest first, so a secret containing another secret is replaced whole.
        self._secrets = sorted(
            {s for s in secrets if len(s) >= _MIN_SECRET_LENGTH}, key=len, reverse=True
        )

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.scrub(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = self.scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_ATTRS and isinstance(value, str):
                setattr(record, key, self.scrub(value))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update((k, v) for k, v in vars(record).items() if k not in _STANDARD_RECORD_ATTRS)
        if record.exc_text:
            entry["exc"] = record.exc_text
        return json.dumps(entry, default=str)


# Libraries that log the content of what passes through them at DEBUG. The takeover socket
# carries the owner's keystrokes (passwords included) and the dashboard's tokens travel in
# headers, so these stay quiet however verbose the application is.
_CONTENT_LOGGERS = ("websockets", "uvicorn.protocols", "httpx", "httpcore", "openai")


def quiet_content_loggers() -> None:
    for name in _CONTENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(settings: Settings) -> None:
    quiet_content_loggers()
    redactor = SecretRedactor(settings.secret_values())

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(JsonFormatter())
    handlers: list[logging.Handler] = [stdout]

    if settings.logfire_token is not None:
        logfire.configure(
            token=settings.logfire_token.get_secret_value(),
            service_name="worker",
            console=False,
        )
        handlers.append(logfire.LogfireLoggingHandler())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level)
    for handler in handlers:
        # On each handler (not the root logger) so records from child loggers are covered too.
        handler.addFilter(redactor)
        root.addHandler(handler)
