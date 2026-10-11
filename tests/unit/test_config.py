from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cosmic.core.config import load_settings
from cosmic.core.errors import ConfigError

KEY = base64.b64encode(b"k" * 32).decode()


def env(**extra: str) -> dict[str, str]:
    base = {
        "COSMIC_ENV": "LOCAL_DEVELOPMENT",
        "COSMIC_SECRET_KEY": KEY,
        "COSMIC_DB_APP_PASSWORD": "app-password-123",
        "COSMIC_DB_OWNER_PASSWORD": "owner-pw-1234",
    }
    base.update(extra)
    return base


def test_defaults_are_safe() -> None:
    s = load_settings(env())
    assert s.cookie_secure is True
    assert s.debug is False
    assert s.max_authority_mode == "PAPER_TRADING"
    assert s.ai_policy == "FREE_CLOUD_AI_ONLY"
    assert s.integration_status()["live_broker"] == "NOT_CONFIGURED"


@pytest.mark.parametrize("tier", ["LIVE", "LIVE_PENDING_APPROVAL"])
def test_live_environment_refused(tier: str) -> None:
    with pytest.raises(ConfigError, match="live broker adapter"):
        load_settings(env(COSMIC_ENV=tier))


def test_paid_or_local_ai_policy_refused() -> None:
    with pytest.raises(ConfigError, match="FREE_CLOUD_AI_ONLY"):
        load_settings(env(COSMIC_AI_POLICY="PAID_FALLBACK"))


def test_wildcard_trusted_proxy_refused() -> None:
    assert load_settings(env()).forwarded_allow_ips == "127.0.0.1"
    with pytest.raises(ConfigError, match="FORWARDED_ALLOW_IPS"):
        load_settings(env(COSMIC_FORWARDED_ALLOW_IPS="*"))


def test_production_like_tiers_reject_dev_settings() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings(
            env(
                COSMIC_ENV="PAPER_TRADING",
                COSMIC_COOKIE_SECURE="false",
                COSMIC_DEBUG="true",
                COSMIC_PAPER_FAULT_INJECTION="true",
                COSMIC_SECRET_KEY="insecure-dev-x",
            )
        )
    msg = str(exc.value)
    assert "COSMIC_DEBUG" in msg and "COSMIC_COOKIE_SECURE" in msg
    assert "FAULT_INJECTION" in msg and "development value" in msg


def test_staging_requires_secrets() -> None:
    with pytest.raises(ConfigError, match="COSMIC_DB_APP_PASSWORD is required"):
        load_settings(
            {
                "COSMIC_ENV": "STAGING",
                "COSMIC_SECRET_KEY": KEY,
                "COSMIC_DB_OWNER_PASSWORD": "x" * 20,
            }
        )


def test_runtime_processes_do_not_need_the_owner_password() -> None:
    s = load_settings(
        {
            "COSMIC_ENV": "PAPER_TRADING",
            "COSMIC_SECRET_KEY": KEY,
            "COSMIC_DB_APP_PASSWORD": "a" * 20,
        }
    )
    assert s.db_owner_password is None
    with pytest.raises(ConfigError, match="cosmic_owner"):
        s.owner_dsn()


def test_invalid_secret_key_length() -> None:
    with pytest.raises(ConfigError, match="32 bytes"):
        load_settings(env(COSMIC_SECRET_KEY=base64.b64encode(b"short").decode()))


def test_error_messages_never_contain_values() -> None:
    secret = "super-secret-value-should-not-leak"
    with pytest.raises(ConfigError) as exc:
        load_settings(env(COSMIC_DB_PORT=secret))
    assert secret not in str(exc.value)
    assert "db_port" in str(exc.value)


def test_secret_files(tmp_path: Path) -> None:
    f = tmp_path / "app_pw"
    f.write_text("from-file-password\n")
    s = load_settings(env(COSMIC_DB_APP_PASSWORD_FILE=str(f)))
    assert s.db_app_password is not None
    assert s.db_app_password.get_secret_value() == "from-file-password"
    empty = tmp_path / "empty"
    empty.write_text("")
    with pytest.raises(ConfigError, match="empty file"):
        load_settings(env(COSMIC_DB_APP_PASSWORD_FILE=str(empty)))
    with pytest.raises(ConfigError, match="unreadable"):
        load_settings(env(COSMIC_DB_APP_PASSWORD_FILE=str(tmp_path / "missing")))
    # Optional integrations: an empty file means NOT_CONFIGURED, not an error.
    s = load_settings(
        env(COSMIC_GROQ_API_KEY_FILE=str(empty), COSMIC_TELEGRAM_BOT_TOKEN_FILE=str(empty))
    )
    assert s.groq_api_key is None and s.integration_status()["telegram"] == "NOT_CONFIGURED"


def test_dsn_quotes_password_and_secret_values_listed() -> None:
    s = load_settings(env(COSMIC_DB_APP_PASSWORD="p@ss:w/rd#1"))
    assert "p%40ss%3Aw%2Frd%231" in s.app_dsn()
    assert "p@ss:w/rd#1" in s.secret_values()
    assert "app-password" not in repr(s)  # SecretStr hides values in repr
