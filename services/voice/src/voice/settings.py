from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """VOICE_* variables. LiveKit, Deepgram and OpenAI read their own standard variables
    (LIVEKIT_URL/API_KEY/API_SECRET, DEEPGRAM_API_KEY, OPENAI_API_KEY)."""

    model_config = SettingsConfigDict(env_prefix="VOICE_", env_file=".env", extra="forbid")

    worker_url: str
    service_hmac_secret: SecretStr = Field(min_length=32)
    llm_model: str = "gpt-4.1"
    tts_voice: str = "aura-2-thalia-en"


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # pydantic-settings fills fields from the environment
