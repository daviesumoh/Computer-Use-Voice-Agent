"""Worker configuration, read from WORKER_* environment variables (or a local .env)."""

import base64
import binascii
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="forbid": a WORKER_* variable the code doesn't use fails startup instead of lingering.
    model_config = SettingsConfigDict(env_prefix="WORKER_", env_file=".env", extra="forbid")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    logfire_token: SecretStr | None = None

    # Postgres DSN for the agent_worker role.
    database_url: SecretStr
    # Shared with the voice agent and web app; signs their requests to this API.
    service_hmac_secret: SecretStr = Field(min_length=32)

    # The Supabase project the dashboard signs in to; used to validate the owner's access token.
    supabase_url: str = Field(pattern=r"^https?://")
    supabase_publishable_key: str = Field(min_length=10)
    # Where the dashboard is served: the only origin allowed to call this API from a browser,
    # and the base of the links in approval and takeover texts.
    dashboard_url: str | None = Field(default=None, pattern=r"^https?://[^/]+$")

    # Twilio REST, authenticated with an API key (revocable) rather than the account auth token.
    twilio_account_sid: str = Field(pattern=r"^AC[0-9a-f]{32}$")
    twilio_api_key_sid: str = Field(pattern=r"^SK[0-9a-f]{32}$")
    twilio_api_key_secret: SecretStr
    twilio_from_number: str = Field(pattern=r"^\+[1-9][0-9]{6,14}$")

    # Browser tasks. Without a key the worker still runs; tasks then fail as "executor_unavailable".
    openai_api_key: SecretStr | None = None
    browser_model: str = "gpt-6.1-sol"
    # Retail sites refuse headless Chromium (see docs/decisions.md), so production runs headed
    # on a virtual display; headless is for development machines and tests.
    browser_headless: bool = False
    # Memory extraction and call summaries; with the OpenAI key above, also embeddings.
    memory_model: str = "gpt-4.1-mini"

    @field_validator("openai_api_key", "logfire_token", mode="before")
    @classmethod
    def blank_means_unset(cls, value: object) -> object:
        # A `NAME=` line in .env is a placeholder, not a key.
        return None if isinstance(value, str) and not value.strip() else value

    # AES-256 key (base64) for saved website logins. Without it, logins are not saved.
    session_key: SecretStr | None = None
    # Bitwarden Secrets Manager holds website passwords; all three or none.
    bitwarden_access_token: SecretStr | None = None
    bitwarden_organization_id: UUID | None = None
    bitwarden_project_id: UUID | None = None
    bitwarden_state_file: str = "/tmp/bitwarden-state"

    # Web push: the VAPID private key (its public half goes to the dashboard as
    # NEXT_PUBLIC_VAPID_PUBLIC_KEY) and a contact address; both or neither.
    vapid_private_key: SecretStr | None = None
    vapid_subject: str | None = Field(default=None, pattern=r"^(mailto:|https://)")

    @field_validator("session_key")
    @classmethod
    def session_key_is_32_bytes(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                decoded = base64.b64decode(value.get_secret_value(), validate=True)
            except binascii.Error as bad:
                raise ValueError("session_key must be base64") from bad
            if len(decoded) != 32:
                raise ValueError("session_key must decode to exactly 32 bytes")
        return value

    @model_validator(mode="after")
    def bitwarden_is_all_or_nothing(self) -> "Settings":
        push = [self.vapid_private_key, self.vapid_subject]
        if any(v is not None for v in push) and not all(v is not None for v in push):
            raise ValueError("set all of the VAPID settings or none")
        given = [
            self.bitwarden_access_token,
            self.bitwarden_organization_id,
            self.bitwarden_project_id,
        ]
        if any(v is not None for v in given) and not all(v is not None for v in given):
            raise ValueError("set all of the Bitwarden settings or none")
        return self

    @property
    def session_key_bytes(self) -> bytes | None:
        return base64.b64decode(self.session_key.get_secret_value()) if self.session_key else None

    def secret_values(self) -> list[str]:
        """Plaintext of every configured secret, so the log redactor can scrub them."""
        secrets = [
            self.logfire_token,
            self.database_url,
            self.service_hmac_secret,
            self.twilio_api_key_secret,
            self.openai_api_key,
            self.session_key,
            self.bitwarden_access_token,
            self.vapid_private_key,
        ]
        values = [s.get_secret_value() for s in secrets if s is not None]
        # Errors can quote the database password alone, without the rest of the URL.
        db_password = urlsplit(self.database_url.get_secret_value()).password
        if db_password:
            values.append(db_password)
        return values


def load_settings() -> Settings:
    """Settings from WORKER_* environment variables and .env; fails fast if any are missing."""
    return Settings()  # type: ignore[call-arg]  # pydantic-settings fills fields from the environment
