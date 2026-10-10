from __future__ import annotations

import json

import pytest

from cosmic.core.logs import configure_logging, get_logger
from cosmic.core.redaction import REDACTED, SECRETS, contains_secret, redact, scrub_text

BOT_TOKEN = "123456789:AAH" + "x" * 32


def test_key_based_redaction() -> None:
    data = {
        "password": "hunter2hunter2",
        "nested": {"api_key": "abc", "ok": "fine"},
        "Authorization": "Bearer z",
        "list": [{"session_token": "t"}],
    }
    out = redact(data)
    assert out["password"] == REDACTED
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["ok"] == "fine"
    assert out["Authorization"] == REDACTED
    assert out["list"][0]["session_token"] == REDACTED


@pytest.mark.parametrize(
    "secret",
    [
        BOT_TOKEN,
        "gsk_" + "a" * 40,
        "sk-or-v1-" + "b" * 40,
        "AIza" + "c" * 35,
        "postgresql+asyncpg://cosmic_app:" + "pw123" + "@db:5432/cosmic",
        "-----BEGIN " + "PRIVATE KEY-----",
    ],
)
def test_pattern_redaction(secret: str) -> None:
    text = f"error calling provider with {secret} in message"
    assert secret not in scrub_text(text)
    assert contains_secret(text)


def test_registered_values_redacted() -> None:
    SECRETS.register(["my-configured-secret-777"])
    assert scrub_text("value my-configured-secret-777 here") == f"value {REDACTED} here"
    assert contains_secret("xx my-configured-secret-777")


def test_logger_output_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    SECRETS.register(["registered-secret-abc123"])
    configure_logging("INFO", json_output=True, process="test")
    log = get_logger("t")
    log.info(
        "provider_error",
        detail=f"token={BOT_TOKEN} and registered-secret-abc123",
        password="hunter2hunter2",
    )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    event = json.loads(line)
    assert BOT_TOKEN not in line and "registered-secret-abc123" not in line
    assert "hunter2hunter2" not in line
    assert event["password"] == REDACTED
    assert event["process"] == "test"
