import base64
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from worker.settings import load_settings

REQUIRED = {
    "WORKER_DATABASE_URL": "postgresql://agent_worker:db-pass-0123456789@db.example:5432/postgres",
    "WORKER_SERVICE_HMAC_SECRET": "h" * 32,
    "WORKER_TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "WORKER_TWILIO_API_KEY_SID": "SK" + "0" * 32,
    "WORKER_TWILIO_API_KEY_SECRET": "twilio-secret-abcdef",
    "WORKER_TWILIO_FROM_NUMBER": "+14165550100",
    "WORKER_SUPABASE_URL": "https://abc.supabase.co",
    "WORKER_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_abcdef",
}


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env from the developer's checkout
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    for name in ("WORKER_LOG_LEVEL", "WORKER_LOGFIRE_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_loads_required_values_with_defaults() -> None:
    settings = load_settings()
    assert settings.log_level == "INFO"
    assert settings.logfire_token is None


def test_missing_required_value_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_SERVICE_HMAC_SECRET")
    with pytest.raises(ValidationError, match="service_hmac_secret"):
        load_settings()


def test_short_hmac_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_SERVICE_HMAC_SECRET", "too-short")
    with pytest.raises(ValidationError, match="service_hmac_secret"):
        load_settings()


def test_secrets_hidden_from_repr_but_given_to_redactor() -> None:
    settings = load_settings()
    assert "twilio-secret-abcdef" not in repr(settings)
    assert "db-pass-0123456789" not in repr(settings)
    secrets = settings.secret_values()
    assert "twilio-secret-abcdef" in secrets
    assert "h" * 32 in secrets
    # The bare database password too: connection errors can quote it without the URL.
    assert "db-pass-0123456789" in secrets


def test_unknown_worker_variable_in_env_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("WORKER_LOG_LEVEL=DEBUG\nWORKER_OLD_FEATURE_FLAG=1\n")
    with pytest.raises(ValidationError, match="old_feature_flag"):
        load_settings()


def test_malformed_twilio_sid_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_TWILIO_ACCOUNT_SID", "not-a-sid")
    with pytest.raises(ValidationError, match="twilio_account_sid"):
        load_settings()


def test_browser_settings_default_to_a_headed_browser_and_no_model_key() -> None:
    settings = load_settings()
    assert settings.openai_api_key is None
    assert settings.browser_model == "gpt-6.1-sol"
    assert settings.browser_headless is False  # retail sites refuse headless Chromium


def test_the_openai_key_is_scrubbed_from_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_OPENAI_API_KEY", "sk-proj-abcdefghijklmnop")
    assert "sk-proj-abcdefghijklmnop" in load_settings().secret_values()


def test_a_blank_key_line_in_the_env_file_means_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_OPENAI_API_KEY", "")
    monkeypatch.setenv("WORKER_LOGFIRE_TOKEN", "  ")
    settings = load_settings()
    assert settings.openai_api_key is None
    assert settings.logfire_token is None


def test_the_session_key_must_be_exactly_32_bytes_of_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("WORKER_SESSION_KEY", good)
    assert load_settings().session_key_bytes == base64.b64decode(good)
    for bad in (base64.b64encode(os.urandom(16)).decode(), "not base64!!"):
        monkeypatch.setenv("WORKER_SESSION_KEY", bad)
        with pytest.raises(ValidationError, match="session_key"):
            load_settings()


def test_bitwarden_settings_are_all_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_BITWARDEN_ACCESS_TOKEN", "0.token.value")
    with pytest.raises(ValidationError, match="all of the Bitwarden settings"):
        load_settings()
    monkeypatch.setenv("WORKER_BITWARDEN_ORGANIZATION_ID", str(uuid4()))
    monkeypatch.setenv("WORKER_BITWARDEN_PROJECT_ID", str(uuid4()))
    assert load_settings().bitwarden_access_token is not None


def test_the_dashboard_url_must_be_a_bare_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_DASHBOARD_URL", "https://dash.example/")
    with pytest.raises(ValidationError, match="dashboard_url"):
        load_settings()


def test_vapid_settings_are_both_or_neither_and_the_contact_must_be_a_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_VAPID_PRIVATE_KEY", "private-key-value")
    with pytest.raises(ValidationError, match="all of the VAPID settings"):
        load_settings()
    monkeypatch.setenv("WORKER_VAPID_SUBJECT", "not-a-contact")
    with pytest.raises(ValidationError, match="vapid_subject"):
        load_settings()
    monkeypatch.setenv("WORKER_VAPID_SUBJECT", "mailto:owner@example.test")

    settings = load_settings()

    assert "private-key-value" in settings.secret_values()  # redacted from logs like the rest
